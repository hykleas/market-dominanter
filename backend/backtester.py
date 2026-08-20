"""Kopya stratejisini cuzdanlarin GECMIS islemleri uzerinde test et.

Ileriye dogru test (paper modda bekleyip 100 islem biriktirmek) 1-2 hafta surer.
Ayni orneklem zaten zincirde duruyor: takip edilecek cuzdanlarin son 30 gunde
ne aldigi, ne zaman sattigi. Bot bunlari kopyalasaydi ne olurdu - burasi onu
hesaplar. Dakikalar icinde.

WALK-FORWARD (survivorship bias'a karsi)
----------------------------------------
Cuzdanlari KAZANDIKLARI icin seciyoruz. Ayni donemde kopyalamayi test etmek,
gelecegi bilerek bahis yapmaktir ve cikan rakam yalandir. O yuzden pencere
ikiye bolunur:

    30 gun once ────────── 15 gun once ────────── bugun
      SECIM donemi            TEST donemi
      (cuzdan burada elenir)   (kopyalama burada olculur)

Cuzdan yalnizca SECIM doneminin metrikleriyle elenir; PnL yalnizca TEST
doneminden sayilir. Secim aninda test donemi henuz olmamistir.

NELERI MODELLEYEMIYOR (sonucu okurken bunlari bil)
--------------------------------------------------
* Likidite/curve kapilari: gecmisteki likiditeyi ucuza yeniden kuramayiz, o
  kapilar test edilmez. Gercekte bir kisim sinyal daha elenir.
* Sinyal gecikmesi: gercekte lideri 3-8 saniye sonra gorursun ve fiyat kacmis
  olur. `--latency-penalty` ile kotumser bir slipaj cezasi eklenir.
* Basarisiz islemler, MEV, gercek zincir slipaji yok.

Yani buradan cikan rakam GERCEGIN UST SINIRIDIR. Burada zarar ediyorsa canlida
kesinlikle zarar eder; burada kar ediyorsa canlida "belki" kar eder.

CLI:
    python -m backend.backtester                        # takipteki cuzdanlar
    python -m backend.backtester --wallet ADRES         # tek cuzdan
    python -m backend.backtester --split-days 15        # bolme noktasi
    python -m backend.backtester --all-candidates       # aday dosyasindaki hepsi
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import database as db  # noqa: E402
import rpc  # noqa: E402
import state  # noqa: E402
from wallet_scorer import (  # noqa: E402
    ClosedTrade, WalletEvent, apply_filters, compute_metrics, fetch_wallet_events,
    load_candidates, match_fifo, probe_activity, probe_trading_style,
)

log = logging.getLogger("market-dominanter")


@dataclass
class SimTrade:
    """Botun kopyaladigi tek bir pozisyonun simulasyonu."""
    wallet: str
    mint: str
    entry_ts: float
    exit_ts: float
    size_sol: float          # pozisyon buyuklugu (ucret haric)
    cost_sol: float          # cuzdandan cikan TOPLAM (pozisyon + giris ucreti)
    proceeds_sol: float      # cikista eline gecen NET
    leader_pnl_pct: float    # liderin ayni islemdeki getirisi
    exit_reason: str

    @property
    def pnl_sol(self) -> float:
        return self.proceeds_sol - self.cost_sol

    @property
    def pnl_pct(self) -> float:
        return (self.pnl_sol / self.cost_sol * 100.0) if self.cost_sol > 0 else 0.0

    @property
    def hold_seconds(self) -> float:
        return max(self.exit_ts - self.entry_ts, 0.0)


@dataclass
class BacktestResult:
    trades: List[SimTrade] = field(default_factory=list)
    skipped: Dict[str, int] = field(default_factory=dict)
    wallets_used: List[str] = field(default_factory=list)
    wallets_rejected: List[Tuple[str, str]] = field(default_factory=list)
    select_start: float = 0.0
    split_ts: float = 0.0
    end_ts: float = 0.0

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl_sol for t in self.trades)

    @property
    def total_cost(self) -> float:
        return sum(t.cost_sol for t in self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.pnl_sol > 0)

    @property
    def win_rate(self) -> float:
        return (self.wins / len(self.trades) * 100.0) if self.trades else 0.0

    @property
    def total_fees(self) -> float:
        return sum(t.cost_sol - t.size_sol for t in self.trades)


# --------------------------------------------------------------------------- #
# Simulasyon  (saf fonksiyon - testi tests/test_backtest.py)
# --------------------------------------------------------------------------- #
def simulate_wallet(wallet: str, leader_trades: Sequence[ClosedTrade],
                    cfg: state.Settings, split_ts: float, end_ts: float,
                    latency_penalty_pct: float = 2.0
                    ) -> Tuple[List[SimTrade], Dict[str, int]]:
    """Bir liderin kapanmis islemlerini kopyalamayi simule et.

    Her lider islemi icin: biz de girer, LIDER CIKINCA cikaririz. Cikisi lidere
    devretmek stratejinin ozu, o yuzden tier semasi yok. Iki guvenlik agi:
    hard stop ve zaman stopu - ama gecmis fiyat serisi elimizde olmadigi icin
    hard stop ancak liderin KENDI kaybina bakarak yaklasik uygulanabilir.
    """
    sims: List[SimTrade] = []
    skipped: Dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for trade in leader_trades:
        if not (split_ts <= trade.entry_ts <= end_ts):
            skip("test_penceresi_disi")
            continue
        if trade.cost_sol < cfg.min_leader_buy_sol:
            skip("dust")
            continue

        size = float(cfg.copy_size_sol)
        platform_fee = size * cfg.fee_platform_pct / 100.0
        fixed_fee = cfg.fee_network_sol + cfg.fee_priority_sol
        cost = size + fixed_fee

        # Liderin getirisi bizim de getirimiz; uzerine gecikme cezasi ve
        # cift yonlu slipaj biner.
        gross_mult = 1.0 + trade.pnl_pct / 100.0
        slip = (cfg.paper_slippage_pct / 100.0) * 2.0 + latency_penalty_pct / 100.0
        gross_mult *= max(1.0 - slip, 0.0)

        exit_reason = "leader_exit"
        held = trade.hold_seconds

        # Guvenlik aglari. Gecmis fiyat serisi yok, o yuzden yalnizca liderin
        # gerceklesmis sonucundan cikarim yapiliyor - yaklasiktir.
        if (gross_mult - 1.0) * 100.0 <= -abs(cfg.hard_stop_pct):
            gross_mult = 1.0 - abs(cfg.hard_stop_pct) / 100.0
            exit_reason = "hard_stop"
        elif held > cfg.time_stop_minutes * 60.0 and trade.pnl_pct < cfg.time_stop_min_pnl:
            held = cfg.time_stop_minutes * 60.0
            exit_reason = "time_stop"

        gross = (size - platform_fee) * gross_mult
        proceeds = max(gross - gross * cfg.fee_platform_pct / 100.0 - fixed_fee, 0.0)

        sims.append(SimTrade(
            wallet=wallet, mint=trade.mint, entry_ts=trade.entry_ts,
            exit_ts=trade.entry_ts + held, size_sol=size, cost_sol=cost,
            proceeds_sol=proceeds, leader_pnl_pct=trade.pnl_pct, exit_reason=exit_reason))

    return sims, skipped


def split_events(events: Sequence[WalletEvent], split_ts: float
                 ) -> Tuple[List[WalletEvent], List[WalletEvent]]:
    """(secim donemi olaylari, test donemi olaylari)."""
    early = [e for e in events if e.ts < split_ts]
    late = [e for e in events if e.ts >= split_ts]
    return early, late


# --------------------------------------------------------------------------- #
# Orkestrasyon
# --------------------------------------------------------------------------- #
async def backtest(addresses: Sequence[str], split_days: float = 15.0,
                   latency_penalty_pct: float = 2.0,
                   skip_selection_filter: bool = False,
                   lookback_days: Optional[float] = None) -> BacktestResult:
    cfg = state.settings
    now = time.time()
    # Yogun cuzdanlarda 30 gun imza butcesine sigmiyor; pencereyi kisaltmak
    # olcumu mumkun kilar (daha az veri, ama VERI).
    lookback = float(lookback_days if lookback_days else cfg.scorer_lookback_days)
    select_start = now - lookback * 86400.0
    split_ts = now - split_days * 86400.0

    result = BacktestResult(select_start=select_start, split_ts=split_ts, end_ts=now)
    if split_ts <= select_start:
        state.bus.log("--split-days, scorer_lookback_days'ten kucuk olmali", "error")
        return result

    state.bus.log("Backtest: %d cuzdan | secim %s .. %s | test %s .. simdi"
                  % (len(addresses), _d(select_start), _d(split_ts), _d(split_ts)), "info")

    for i, address in enumerate(addresses, 1):
        state.bus.log("[%d/%d] %s inceleniyor..." % (i, len(addresses), address[:10]), "info")
        try:
            # Once ucuz on tarama: yogunluk ve pencerenin kapsanip kapsanmadigi.
            # Bu adim olmadan bot'lar 10 dakika islem cozdurup sonunda yine
            # eleniyordu - ve butce dolduysa "hic islem yapmamis" gibi
            # gorunuyorlardi.
            probe = await probe_activity(address, select_start)
            if probe.tx_per_day > cfg.scorer_max_tx_per_day:
                reason = "bot yogunlugu (%.0f islem/gun)" % probe.tx_per_day
                result.wallets_rejected.append((address, reason))
                state.bus.log("%s ELENDI: %s" % (address[:10], reason), "warn")
                continue
            # Prob zaten pencerede kac imza oldugunu biliyor. Butce yetmeyecekse
            # bunu SIMDI bil: yoksa 3000 islemi tek tek cozup (5 dakika) sonunda
            # ayni sonuca varilir.
            if probe.signatures > int(cfg.scorer_max_signatures) or not probe.covered_window:
                reason = ("imza butcesi yetmiyor - pencerede %d+ imza var, tavan %d "
                          "(%.0f islem/gun)" % (probe.signatures,
                                                int(cfg.scorer_max_signatures),
                                                probe.tx_per_day))
                result.wallets_rejected.append((address, reason))
                state.bus.log("%s ELENDI: %s" % (address[:10], reason), "warn")
                continue

            # STIL TARAMASI: kopyalanamaz cuzdani tam gecmisini cekmeden ele.
            style = await probe_trading_style(address, int(cfg.scorer_style_sample))
            if style.trades and style.median_hold_seconds < cfg.scorer_min_hold_seconds:
                reason = ("kopyalanamaz - medyan tutus %.0f sn < %.0f sn (%d islem ornegi)"
                          % (style.median_hold_seconds, cfg.scorer_min_hold_seconds,
                             style.trades))
                result.wallets_rejected.append((address, reason))
                state.bus.log("%s ELENDI: %s" % (address[:10], reason), "warn")
                continue

            events, complete = await fetch_wallet_events(address, select_start,
                                                         int(cfg.scorer_max_signatures))
            if not complete:
                reason = "imza butcesi (%d) pencereyi kapsamadi" % int(cfg.scorer_max_signatures)
                result.wallets_rejected.append((address, reason))
                state.bus.log("%s ELENDI: %s" % (address[:10], reason), "warn")
                continue
        except Exception as exc:
            log.error("Cuzdan cekilemedi (%s): %s", address, exc)
            result.wallets_rejected.append((address, "cekilemedi: %s" % type(exc).__name__))
            continue

        early, _late = split_events(events, split_ts)

        # SECIM: yalnizca eski yarinin metrikleriyle. Test donemi kullanilmaz.
        if not skip_selection_filter:
            metrics = compute_metrics(address, early, match_fifo(early), {}, select_start)
            # Ornekle esigi yarim pencereye gore olceklenir.
            half = max(int(cfg.scorer_min_trades * (split_ts - select_start)
                           / max(now - select_start, 1.0)), 3)
            metrics.trade_count_30d = len(match_fifo(early))
            apply_filters(metrics, cfg)
            if metrics.rejected and "ornekle yetersiz" not in metrics.rejected:
                result.wallets_rejected.append((address, metrics.rejected))
                state.bus.log("%s ELENDI (secim donemi): %s" % (address[:10], metrics.rejected), "warn")
                continue
            if metrics.trade_count_30d < half:
                reason = "secim doneminde %d trade < %d" % (metrics.trade_count_30d, half)
                result.wallets_rejected.append((address, reason))
                state.bus.log("%s ELENDI: %s" % (address[:10], reason), "warn")
                continue

        # TEST: tum olaylardan FIFO kur, sonra giris zamani test doneminde
        # olanlari al. Tum olaylar kullanilir cunku bir lot secim doneminde
        # alinip test doneminde satilmis olabilir.
        all_trades = match_fifo(events)
        sims, skipped = simulate_wallet(address, all_trades, cfg, split_ts, now,
                                        latency_penalty_pct)
        for reason, count in skipped.items():
            result.skipped[reason] = result.skipped.get(reason, 0) + count
        result.trades.extend(sims)
        result.wallets_used.append(address)
        state.bus.log("%s -> test doneminde %d kopyalanabilir islem"
                      % (address[:10], len(sims)), "success" if sims else "warn")

    result.trades.sort(key=lambda t: t.entry_ts)
    return result


def _d(ts: float) -> str:
    return time.strftime("%d %b", time.localtime(ts))


# --------------------------------------------------------------------------- #
# Rapor
# --------------------------------------------------------------------------- #
def report(result: BacktestResult) -> None:
    trades = result.trades
    print("\n" + "=" * 72)
    print("BACKTEST SONUCU")
    print("=" * 72)
    print("Secim donemi : %s .. %s  (cuzdanlar burada elendi)"
          % (_d(result.select_start), _d(result.split_ts)))
    print("Test donemi  : %s .. %s  (PnL burada olculdu)"
          % (_d(result.split_ts), _d(result.end_ts)))
    print("Cuzdan       : %d kullanildi, %d elendi"
          % (len(result.wallets_used), len(result.wallets_rejected)))

    if not trades:
        print("\nTest doneminde kopyalanacak islem cikmadi.")
        if result.skipped:
            print("Atlama sebepleri:", dict(sorted(result.skipped.items(),
                                                   key=lambda kv: -kv[1])))
        if result.wallets_rejected:
            print("\nElenen cuzdanlar:")
            for address, reason in result.wallets_rejected[:10]:
                print("  %-46s %s" % (address, reason))
        print()
        return

    pnls = [t.pnl_pct for t in trades]
    holds = [t.hold_seconds for t in trades]
    print("\nISLEM        : %d  (kazanan %d, kaybeden %d)"
          % (len(trades), result.wins, len(trades) - result.wins))
    print("BASARI ORANI : %%%.1f" % result.win_rate)
    print("TOPLAM PnL   : %+.4f SOL  (yatirilan %.4f SOL uzerinde %%%+.1f)"
          % (result.total_pnl, result.total_cost,
             result.total_pnl / result.total_cost * 100.0 if result.total_cost else 0.0))
    print("ODENEN UCRET : %.4f SOL  (sermayenin %%%.1f'i)"
          % (result.total_fees,
             result.total_fees / result.total_cost * 100.0 if result.total_cost else 0.0))
    print("MEDYAN ISLEM : %%%+.1f   |  en iyi %%%+.0f  en kotu %%%+.0f"
          % (statistics.median(pnls), max(pnls), min(pnls)))
    print("MEDYAN TUTUS : %.0f dakika" % (statistics.median(holds) / 60.0))

    reasons: Dict[str, int] = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    print("CIKIS SEBEBI : " + ", ".join("%s=%d" % kv for kv in sorted(reasons.items())))
    if result.skipped:
        print("ATLANAN      : " + ", ".join(
            "%s=%d" % kv for kv in sorted(result.skipped.items(), key=lambda kv: -kv[1])))

    print("\nCUZDAN BAZINDA")
    print("%-46s %6s %8s %8s" % ("CUZDAN", "ISLEM", "PnL SOL", "BASARI"))
    print("-" * 72)
    for address in result.wallets_used:
        own = [t for t in trades if t.wallet == address]
        if not own:
            continue
        pnl = sum(t.pnl_sol for t in own)
        wr = sum(1 for t in own if t.pnl_sol > 0) / len(own) * 100.0
        print("%-46s %6d %+8.4f %7.0f%%" % (address, len(own), pnl, wr))

    print("\n" + "-" * 72)
    if result.total_pnl > 0:
        print("Bu rakam GERCEGIN UST SINIRIDIR: likidite kapilari, basarisiz islemler")
        print("ve gercek zincir slipaji modellenmedi. Canlida bundan DAHA IYI olmaz.")
    else:
        print("Ust sinir bile zararda. Canlida daha kotu olur - parametreleri")
        print("degistirmeden gercek cuzdana gecme.")
    print()


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m backend.backtester",
        description="Kopya stratejisini cuzdanlarin gecmis islemleri uzerinde test et.")
    p.add_argument("--wallet", action="append", default=None, help="tek cuzdan (tekrarlanabilir)")
    p.add_argument("--all-candidates", action="store_true",
                   help="wallets_candidates.json icindeki tum adresler")
    p.add_argument("--split-days", type=float, default=15.0,
                   help="son N gun TEST donemi, oncesi SECIM donemi")
    p.add_argument("--latency-penalty", type=float, default=2.0,
                   help="sinyal gecikmesi icin %% slipaj cezasi")
    p.add_argument("--lookback-days", type=float, default=None,
                   help="toplam pencere (varsayilan: scorer_lookback_days)")
    p.add_argument("--max-signatures", type=int, default=None,
                   help="cuzdan basina imza tavani (varsayilan: scorer_max_signatures)")
    p.add_argument("--set", action="append", default=None, metavar="AYAR=DEGER",
                   help="ayari gecici olarak degistir (tekrarlanabilir), or. min_leader_buy_sol=0.05")
    p.add_argument("--no-selection-filter", action="store_true",
                   help="secim donemi elemesini atla (ham potansiyeli gormek icin)")
    return p.parse_args(argv)


async def _main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
        state.reload_env()
    except ImportError:
        pass

    args = _parse_args(argv)
    if not state.HELIUS_API_KEY:
        print("HELIUS_API_KEY yok - islem gecmisi cekilemez.")
        return 1

    db.init_db()
    if args.wallet:
        addresses = list(args.wallet)
    elif args.all_candidates:
        addresses = [c["address"] for c in load_candidates()]
    else:
        addresses = [w["address"] for w in db.get_wallets(followed_only=True)]
        if not addresses:
            print("Takip edilen cuzdan yok. --all-candidates ya da --wallet kullanin.")
            return 1

    try:
        if args.max_signatures:
            state.settings.scorer_max_signatures = int(args.max_signatures)
        for pair in (args.set or []):
            if "=" not in pair:
                print("--set AYAR=DEGER biciminde olmali: %s" % pair)
                return 1
            key, value = pair.split("=", 1)
            # Kalici settings.json'i kirletmeden, yalnizca bu calistirma icin.
            before = getattr(state.settings, key.strip(), None)
            if before is None:
                print("bilinmeyen ayar: %s" % key)
                return 1
            setattr(state.settings, key.strip(), type(before)(value))
            print("ayar: %s = %s (onceki %s)" % (key.strip(), value, before))
        result = await backtest(addresses, args.split_days, args.latency_penalty,
                                skip_selection_filter=args.no_selection_filter,
                                lookback_days=args.lookback_days)
        report(result)
    finally:
        await rpc.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
