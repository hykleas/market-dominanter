"""KATMAN 1 - aday cuzdanlari gecmis on-chain verisiyle skorla.

`wallets_candidates.json` icindeki adaylarin son N gunluk islem gecmisi cekilir,
token bazinda alim/satim FIFO ile eslestirilir ve her cuzdan icin bir metrik
seti hesaplanir. Bot/farmer imzasi tasiyanlar elenir, kalanlar skorlanip
`wallets` tablosuna yazilir.

CLI:
    python -m backend.wallet_scorer                 # tum adaylari skorla
    python -m backend.wallet_scorer --only ADRES    # tek cuzdan
    python -m backend.wallet_scorer --no-ai         # AI katmanini atla

Batch bir gecede calisabilir - acele yok. Her RPC cagrisi mevcut pacer'dan
(RPC_RPS) gecer, 429 gelirse rpc.call kendisi bekleyip yeniden dener, yani
hiz limiti batch'i cokertmez, sadece yavaslatir.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# `python -m backend.wallet_scorer` ile calistirildiginda sys.path repo koku
# olur ve duz `import rpc` cozulmez; main.py ile ayni numara.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import database as db  # noqa: E402
import rpc  # noqa: E402
import state  # noqa: E402

log = logging.getLogger("market-dominanter")

ROOT = Path(__file__).resolve().parent.parent
CANDIDATES_FILE = ROOT / "wallets_candidates.json"

# Launch zamani cikarmak icin cuzdanin dokundugu tum mint'ler yerine bir ornek
# alinir: medyan icin fazlasiyla yeterli, RPC butcesi icin ise sart.
LAUNCH_SAMPLE_SIZE = 25
LAUNCH_PAGE_BUDGET = 6      # mint basina en fazla bu kadar imza sayfasi
RUG_PNL_PCT = -80.0
SIG_PAGE = 1000


# --------------------------------------------------------------------------- #
# Veri tipleri
# --------------------------------------------------------------------------- #
@dataclass
class WalletEvent:
    """Bir cuzdanin tek bir islemde yaptigi net token hareketi."""
    ts: float
    mint: str
    side: str            # "buy" | "sell"
    sol: float           # her zaman pozitif: alimda odenen, satista alinan
    tokens: float        # her zaman pozitif
    signature: str = ""


@dataclass
class ClosedTrade:
    mint: str
    entry_ts: float
    exit_ts: float
    cost_sol: float
    proceeds_sol: float
    tokens: float

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
class WalletMetrics:
    address: str
    trade_count_30d: int = 0
    unique_tokens_30d: int = 0
    buy_count: int = 0
    win_rate: float = 0.0
    median_pnl_pct: float = 0.0
    total_pnl_sol: float = 0.0
    median_hold_seconds: float = 0.0
    median_entry_delay: Optional[float] = None
    buy_size_consistency: float = 0.0
    active_days_30d: int = 0
    longest_silence_hours: float = 0.0
    rug_hit_rate: float = 0.0
    weekly_pnl_std: float = 0.0
    rejected: Optional[str] = None
    suspicious: Optional[str] = None
    score: float = 0.0

    def as_row(self) -> Dict[str, Any]:
        return {
            "win_rate": self.win_rate,
            "total_pnl_sol": self.total_pnl_sol,
            "median_hold_seconds": self.median_hold_seconds,
            "median_entry_delay": self.median_entry_delay,
            "trade_count_30d": self.trade_count_30d,
            "rug_hit_rate": self.rug_hit_rate,
            "is_suspicious": 1 if self.suspicious else 0,
        }


# --------------------------------------------------------------------------- #
# Islem cozumleme  (saf fonksiyonlar - test edilebilir)
# --------------------------------------------------------------------------- #
def parse_wallet_tx(tx: Dict[str, Any], wallet: str) -> Optional[WalletEvent]:
    """Bir transaction'i cuzdan acisindan tek bir alim/satim olayina indirger.

    Program-spesifik instruction cozumleme YAPILMAZ; sadece bakiye farklarina
    bakilir. Boylece pump.fun, Jupiter, Raydium, PumpSwap - hepsi ayni kodla
    calisir ve yeni bir dex ciktiginda kod bozulmaz.
    """
    if not tx or (tx.get("meta") or {}).get("err"):
        return None
    meta = tx.get("meta") or {}
    message = ((tx.get("transaction") or {}).get("message") or {})
    keys = message.get("accountKeys") or []

    index = None
    for i, key in enumerate(keys):
        pubkey = key.get("pubkey") if isinstance(key, dict) else key
        if pubkey == wallet:
            index = i
            break
    if index is None:
        return None

    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    if index >= len(pre) or index >= len(post):
        return None
    sol_delta = (post[index] - pre[index]) / state.LAMPORTS_PER_SOL
    # Ucreti yalnizca fee payer (ilk imzalayan) oder; onun icin islem buyuklugu
    # ucret kadar sasar.
    if index == 0:
        sol_delta += (meta.get("fee") or 0) / state.LAMPORTS_PER_SOL

    token_delta: Dict[str, float] = {}
    for entries, sign in ((meta.get("preTokenBalances") or [], -1.0),
                          (meta.get("postTokenBalances") or [], 1.0)):
        for bal in entries:
            if bal.get("owner") != wallet:
                continue
            mint = bal.get("mint")
            if not mint:
                continue
            try:
                amount = float((bal.get("uiTokenAmount") or {}).get("uiAmount") or 0.0)
            except (TypeError, ValueError):
                amount = 0.0
            token_delta[mint] = token_delta.get(mint, 0.0) + sign * amount

    # Sarilmis SOL token degil, nakit tarafidir.
    sol_delta += token_delta.pop(state.WSOL_MINT, 0.0)
    token_delta = {m: d for m, d in token_delta.items() if abs(d) > 1e-9}
    if not token_delta:
        return None

    mint, delta = max(token_delta.items(), key=lambda kv: abs(kv[1]))
    ts = float(tx.get("blockTime") or 0.0)
    signature = ""
    sigs = (tx.get("transaction") or {}).get("signatures") or []
    if sigs:
        signature = sigs[0]

    if delta > 0 and sol_delta < 0:
        return WalletEvent(ts=ts, mint=mint, side="buy", sol=abs(sol_delta),
                           tokens=abs(delta), signature=signature)
    if delta < 0 and sol_delta > 0:
        return WalletEvent(ts=ts, mint=mint, side="sell", sol=abs(sol_delta),
                           tokens=abs(delta), signature=signature)
    # Airdrop, transfer, LP islemi vb. - trade degil.
    return None


def match_fifo(events: Iterable[WalletEvent]) -> List[ClosedTrade]:
    """Alimlari satislarla FIFO eslestirip kapanmis trade'leri dondurur.

    Her mint kendi kuyrugunu tutar. Kismi satis, alim lotunu kismen tuketir ve
    maliyet token orani kadar bolunur. Karsiliksiz satis (bot bakmaya
    basladigindan onceki bir alimin satisi, ya da airdrop satisi) sessizce
    atlanir - uydurma bir maliyet bazi kar/zarari tamamen bozardi.

    Saf fonksiyon: agdan bagimsiz, testi tests/test_fifo.py icinde.
    """
    lots: Dict[str, List[List[float]]] = {}   # mint -> [[tokens, cost_sol, ts], ...]
    trades: List[ClosedTrade] = []

    for event in sorted(events, key=lambda e: (e.ts, 0 if e.side == "buy" else 1)):
        if event.tokens <= 0:
            continue
        if event.side == "buy":
            lots.setdefault(event.mint, []).append([event.tokens, event.sol, event.ts])
            continue

        queue = lots.get(event.mint) or []
        remaining = event.tokens
        # Satistan gelen SOL, tuketilen token orani kadar bolusturulur.
        proceeds_per_token = event.sol / event.tokens if event.tokens > 0 else 0.0
        while remaining > 1e-12 and queue:
            lot = queue[0]
            take = min(lot[0], remaining)
            cost_share = lot[1] * (take / lot[0]) if lot[0] > 0 else 0.0
            trades.append(ClosedTrade(
                mint=event.mint, entry_ts=lot[2], exit_ts=event.ts,
                cost_sol=cost_share, proceeds_sol=proceeds_per_token * take, tokens=take,
            ))
            lot[0] -= take
            lot[1] -= cost_share
            remaining -= take
            if lot[0] <= 1e-12:
                queue.pop(0)
    return trades


def _median(values: List[float]) -> float:
    return statistics.median(values) if values else 0.0


def compute_metrics(address: str, events: List[WalletEvent], trades: List[ClosedTrade],
                    entry_delays: Dict[str, float], window_start: float) -> WalletMetrics:
    """Ham olay/trade listesinden cuzdan metriklerini cikarir. Saf fonksiyon."""
    m = WalletMetrics(address=address)
    buys = [e for e in events if e.side == "buy"]
    m.trade_count_30d = len(trades)
    m.buy_count = len(buys)
    m.unique_tokens_30d = len({e.mint for e in events})

    if trades:
        m.win_rate = sum(1 for t in trades if t.pnl_sol > 0) / len(trades) * 100.0
        m.median_pnl_pct = _median([t.pnl_pct for t in trades])
        m.total_pnl_sol = sum(t.pnl_sol for t in trades)
        m.rug_hit_rate = sum(1 for t in trades if t.pnl_pct <= RUG_PNL_PCT) / len(trades) * 100.0

    # Tutus suresi: her mint icin ilk alim -> ilk satis.
    first_buy: Dict[str, float] = {}
    first_sell: Dict[str, float] = {}
    for e in sorted(events, key=lambda e: e.ts):
        if e.side == "buy":
            first_buy.setdefault(e.mint, e.ts)
        else:
            first_sell.setdefault(e.mint, e.ts)
    holds = [first_sell[mint] - first_buy[mint] for mint in first_buy
             if mint in first_sell and first_sell[mint] >= first_buy[mint]]
    m.median_hold_seconds = _median(holds)

    delays = [d for d in entry_delays.values() if d is not None and d >= 0]
    m.median_entry_delay = _median(delays) if delays else None

    # Varyasyon katsayisi: script'ler hep ayni miktari basar, insan basmaz.
    # 2'den az alimda tanimsizdir; 0.0 birakmak cuzdani yanlislikla "script"
    # damgalatirdi, o yuzden filtre ayrica buy_count'a bakar.
    sizes = [e.sol for e in buys if e.sol > 0]
    if len(sizes) >= 2:
        mean = statistics.fmean(sizes)
        m.buy_size_consistency = (statistics.pstdev(sizes) / mean) if mean > 0 else 0.0

    days = {datetime.fromtimestamp(e.ts).date() for e in events if e.ts > 0}
    m.active_days_30d = len(days)
    stamps = sorted(e.ts for e in events if e.ts > 0)
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    m.longest_silence_hours = (max(gaps) / 3600.0) if gaps else 0.0

    # Haftalik PnL dagilimi -> tutarlilik girdisi.
    weekly: Dict[int, float] = {}
    for t in trades:
        bucket = int((t.exit_ts - window_start) // (7 * 86400))
        weekly[bucket] = weekly.get(bucket, 0.0) + t.pnl_sol
    m.weekly_pnl_std = statistics.pstdev(list(weekly.values())) if len(weekly) >= 2 else 0.0
    return m


# --------------------------------------------------------------------------- #
# Bot / farmer eleme
# --------------------------------------------------------------------------- #
def apply_filters(m: WalletMetrics, cfg: Optional[state.Settings] = None) -> WalletMetrics:
    """Hard filtreler + supheli isaretleme. Metrigi yerinde gunceller."""
    cfg = cfg or state.settings

    if m.median_entry_delay is not None and m.median_entry_delay < 3.0:
        m.rejected = "MEV/sniper botu (medyan giris gecikmesi %.1fsn < 3sn)" % m.median_entry_delay
    elif m.median_hold_seconds < 30.0 and m.trade_count_30d > 500:
        m.rejected = ("scalper script (medyan tutus %.0fsn < 30sn, %d trade > 500)"
                      % (m.median_hold_seconds, m.trade_count_30d))
    elif m.buy_count >= 2 and m.buy_size_consistency < 0.05:
        m.rejected = "script (alim boyutu varyasyonu %.3f < 0.05)" % m.buy_size_consistency
    elif m.trade_count_30d < int(cfg.scorer_min_trades):
        m.rejected = ("ornekle yetersiz (%d kapanmis trade < %d)"
                      % (m.trade_count_30d, int(cfg.scorer_min_trades)))

    # Otomatik eleme YOK - panelde kirmizi gosterilir, takip karari kullanicinin.
    if m.win_rate > 85.0:
        m.suspicious = "win rate %%%.1f > %%85 - insider ya da farmer tuzagi olabilir" % m.win_rate
    return m


def score_population(metrics: List[WalletMetrics]) -> None:
    """score = win_rate*0.4 + normalize(total_pnl)*0.3 + consistency*0.3

    Normalizasyon populasyona goredir, o yuzden tek tek degil toplu hesaplanir.
    Elenmis cuzdanlar skora dahil edilmez (aksi halde min/max'i bozarlardi).
    """
    alive = [m for m in metrics if not m.rejected]
    if not alive:
        return

    def _norm(values: List[float]) -> List[float]:
        lo, hi = min(values), max(values)
        if hi - lo < 1e-12:
            return [0.5] * len(values)
        return [(v - lo) / (hi - lo) for v in values]

    pnl_norm = _norm([m.total_pnl_sol for m in alive])
    # Haftalik PnL std'nin tersi: dusuk std = tutarli.
    consistency_norm = _norm([1.0 / (1.0 + m.weekly_pnl_std) for m in alive])
    for m, pnl, cons in zip(alive, pnl_norm, consistency_norm):
        m.score = (m.win_rate / 100.0) * 0.4 + pnl * 0.3 + cons * 0.3


# --------------------------------------------------------------------------- #
# Zincir erisimi
# --------------------------------------------------------------------------- #
@dataclass
class ActivityProbe:
    """Cuzdanin ne kadar yogun oldugu - SADECE imza sayarak olculur."""
    signatures: int = 0
    covered_window: bool = False   # pencerenin basina ulasildi mi
    oldest_ts: float = 0.0
    newest_ts: float = 0.0

    @property
    def tx_per_day(self) -> float:
        span = max(self.newest_ts - self.oldest_ts, 1.0)
        return self.signatures / (span / 86400.0)


async def probe_activity(wallet: str, since: float, max_pages: int = 40) -> ActivityProbe:
    """Islem YOGUNLUGUNU olc, islemleri COZMEDEN.

    getSignaturesForAddress sayfa basina 1000 imza dondurur; getTransaction ise
    imza basina bir cagridir. Yani yogunluk olcmek 1000 kat ucuz.

    Bu on tarama olmadan sistem, gunde binlerce islem yapan bir botu tespit
    etmek icin once o botun 3000 islemini tek tek cozuyordu: cuzdan basina 6-10
    dakika, sonunda "bu bir bot" demek icin. Simdi ayni cevap birkac saniyede.
    """
    probe = ActivityProbe()
    before: Optional[str] = None
    for _ in range(max_pages):
        page = await rpc.get_signatures(wallet, limit=SIG_PAGE, before=before)
        if not page:
            probe.covered_window = True
            break
        for sig in page:
            block_time = float(sig.get("blockTime") or 0)
            if block_time <= 0:
                continue
            probe.newest_ts = max(probe.newest_ts, block_time)
            probe.oldest_ts = block_time if probe.oldest_ts == 0 else min(probe.oldest_ts, block_time)
        probe.signatures += len(page)
        oldest = float(page[-1].get("blockTime") or 0)
        if oldest and oldest < since:
            probe.covered_window = True
            break
        if len(page) < SIG_PAGE:
            probe.covered_window = True
            break
        before = page[-1]["signature"]
    return probe


async def fetch_wallet_events(wallet: str, since: float, max_signatures: int
                              ) -> Tuple[List[WalletEvent], bool]:
    """(olaylar, pencere_tam_kapsandi_mi).

    Ikinci deger KRITIK: False ise imza butcesi `since`'e ulasmadan doldu, yani
    olaylar pencerenin yalnizca EN YENI ucunu kapsar. Bunu yok saymak sessizce
    yanlis cevap uretir - gunde binlerce islem yapan bir cuzdanda 3000 imza
    sadece son gunu kapsar ve "15 gun once hic islem yapmamis" gibi gorunur.
    """
    signatures: List[Dict[str, Any]] = []
    before: Optional[str] = None
    complete = False
    while len(signatures) < max_signatures:
        page = await rpc.get_signatures(wallet, limit=SIG_PAGE, before=before)
        if not page:
            complete = True
            break
        signatures.extend(page)
        oldest = page[-1]
        before = oldest.get("signature")
        if float(oldest.get("blockTime") or 0) < since:
            complete = True     # pencerenin basina varildi
            break
        if len(page) < SIG_PAGE:
            complete = True     # cuzdanin tum gecmisi bu kadar
            break

    events: List[WalletEvent] = []
    for sig in signatures:
        block_time = float(sig.get("blockTime") or 0)
        if block_time and block_time < since:
            continue
        if sig.get("err"):
            continue
        tx = await rpc.get_transaction(sig["signature"])
        event = parse_wallet_tx(tx, wallet) if tx else None
        if event and event.ts >= since:
            events.append(event)
    return events, complete


async def mint_launch_time(mint: str) -> Optional[float]:
    """Mint'in ilk isleminin zamani, ya da butce icinde bulunamazsa None.

    None DONMEK ONEMLI: bulunamayan launch zamanini 0 kabul etmek her cuzdana
    devasa bir giris gecikmesi yazar ve MEV filtresini tamamen ise yaramaz hale
    getirirdi.
    """
    before: Optional[str] = None
    oldest: Optional[float] = None
    for _ in range(LAUNCH_PAGE_BUDGET):
        page = await rpc.get_signatures(mint, limit=SIG_PAGE, before=before)
        if not page:
            break
        last = page[-1]
        block_time = float(last.get("blockTime") or 0)
        if block_time:
            oldest = block_time
        if len(page) < SIG_PAGE:
            return oldest      # sayfa dolmadi -> gercekten en eskisi
        before = last.get("signature")
    return None                # butce doldu, en eskisine ulasilamadi


async def entry_delays_for(wallet_events: List[WalletEvent]) -> Dict[str, float]:
    """Her mint icin: launch -> cuzdanin ilk alimi (saniye)."""
    first_buy: Dict[str, float] = {}
    for e in sorted(wallet_events, key=lambda e: e.ts):
        if e.side == "buy":
            first_buy.setdefault(e.mint, e.ts)

    sample = sorted(first_buy.items(), key=lambda kv: kv[1], reverse=True)[:LAUNCH_SAMPLE_SIZE]
    delays: Dict[str, float] = {}
    for mint, bought_at in sample:
        launch = await mint_launch_time(mint)
        if launch and bought_at >= launch:
            delays[mint] = bought_at - launch
    return delays


# --------------------------------------------------------------------------- #
# Opsiyonel AI siniflandirma
# --------------------------------------------------------------------------- #
CLASSIFY_SYSTEM = (
    "Sen bir on-chain trading davranis analistisin. Sana bir Solana cuzdaninin "
    "30 gunluk trade metrikleri ve son islemlerinin zaman damgali listesi "
    "verilecek. Cuzdanin ARKASINDA kim oldugunu siniflandir.\n\n"
    "Siniflar:\n"
    "  discretionary - insan; degisken boyut, degisken tutus, duzensiz saatler\n"
    "  script        - otomatik bot; sabit boyut, sabit tutus, 7/24 aktif\n"
    "  farmer        - hacim/airdrop ciftcisi; cok islem, anlamsiz kar\n"
    "  insider       - launch'lara imkansiz erken giren, asiri yuksek isabet\n\n"
    "SADECE su JSON'u dondur, baska hicbir sey yazma:\n"
    '{"classification": "...", "confidence": 0.0, "reasoning": "..."}'
)


async def classify_with_ai(m: WalletMetrics, events: List[WalletEvent]) -> Optional[Dict[str, Any]]:
    """Claude ile davranis siniflandirmasi. Anahtar yoksa sessizce None doner."""
    if not os.getenv("ANTHROPIC_API_KEY", "").strip():
        return None
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        log.info("anthropic paketi kurulu degil - AI siniflandirma atlandi")
        return None

    recent = sorted(events, key=lambda e: e.ts, reverse=True)[:20]
    lines = [
        "%s  %-4s  %-44s  %.4f SOL"
        % (datetime.fromtimestamp(e.ts).strftime("%Y-%m-%d %H:%M:%S"), e.side, e.mint, e.sol)
        for e in recent
    ]
    prompt = (
        "Cuzdan: %s\n\n"
        "METRIKLER (son 30 gun)\n"
        "  kapanmis trade      : %d\n"
        "  farkli token        : %d\n"
        "  win rate            : %%%.1f\n"
        "  medyan PnL          : %%%.1f\n"
        "  toplam PnL          : %.4f SOL\n"
        "  medyan tutus        : %.0f sn\n"
        "  medyan giris gecikme: %s\n"
        "  alim boyutu var.kat.: %.4f\n"
        "  aktif gun           : %d / 30\n"
        "  en uzun sessizlik   : %.1f saat\n"
        "  rug orani           : %%%.1f\n\n"
        "SON 20 ISLEM\n%s"
        % (m.address, m.trade_count_30d, m.unique_tokens_30d, m.win_rate, m.median_pnl_pct,
           m.total_pnl_sol, m.median_hold_seconds,
           ("%.1f sn" % m.median_entry_delay) if m.median_entry_delay is not None else "bilinmiyor",
           m.buy_size_consistency, m.active_days_30d, m.longest_silence_hours,
           m.rug_hit_rate, "\n".join(lines) or "  (islem yok)")
    )

    try:
        client = AsyncAnthropic()
        response = await client.messages.create(
            model="claude-opus-5",
            max_tokens=2000,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            system=CLASSIFY_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        log.warning("AI siniflandirma basarisiz (%s): %s", m.address[:8], type(exc).__name__)
        return None

    text = "".join(b.text for b in response.content if b.type == "text").strip()
    try:
        start, end = text.index("{"), text.rindex("}") + 1
        data = json.loads(text[start:end])
    except (ValueError, json.JSONDecodeError):
        log.warning("AI yaniti JSON degil (%s)", m.address[:8])
        return None
    if data.get("classification") not in ("discretionary", "script", "farmer", "insider"):
        return None
    return data


# --------------------------------------------------------------------------- #
# Orkestrasyon
# --------------------------------------------------------------------------- #
def load_candidates() -> List[Dict[str, Any]]:
    if not CANDIDATES_FILE.exists():
        log.error("%s bulunamadi", CANDIDATES_FILE)
        return []
    try:
        raw = json.loads(CANDIDATES_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        log.error("%s okunamadi: %s", CANDIDATES_FILE.name, exc)
        return []
    out = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            out.append({"address": item, "source": "?", "note": ""})
        elif isinstance(item, dict) and item.get("address"):
            out.append(item)
    return out


async def score_wallets(candidates: Optional[List[Dict[str, Any]]] = None,
                        use_ai: bool = True) -> List[WalletMetrics]:
    cfg = state.settings
    candidates = candidates if candidates is not None else load_candidates()
    if not candidates:
        state.bus.log("Aday cuzdan yok - wallets_candidates.json doldurun", "warn")
        return []

    window = float(cfg.scorer_lookback_days) * 86400.0
    since = time.time() - window
    results: List[WalletMetrics] = []
    payloads: Dict[str, List[WalletEvent]] = {}

    state.bus.log("Cuzdan skorlama basladi: %d aday, %d gun geriye"
                  % (len(candidates), int(cfg.scorer_lookback_days)), "info")

    for i, candidate in enumerate(candidates, 1):
        address = candidate["address"]
        state.bus.log("[%d/%d] %s taraniyor..." % (i, len(candidates), address[:10]), "info")
        try:
            # UCUZ ON TARAMA: yogunluk, islemler cozulmeden olculur. Bot'u
            # dakikalar yerine saniyeler icinde eler.
            probe = await probe_activity(address, since)
            if probe.tx_per_day > cfg.scorer_max_tx_per_day:
                metrics = WalletMetrics(address=address)
                metrics.rejected = ("bot yogunlugu (%.0f islem/gun > %.0f) - islemler "
                                    "cozulmedi" % (probe.tx_per_day, cfg.scorer_max_tx_per_day))
                results.append(metrics)
                db.upsert_wallet(address, source=candidate.get("source"),
                                 note=candidate.get("note"))
                state.bus.log("%s ELENDI: %s" % (address[:10], metrics.rejected), "warn")
                continue

            events, complete = await fetch_wallet_events(
                address, since, int(cfg.scorer_max_signatures))
            delays = await entry_delays_for(events)
            trades = match_fifo(events)
            metrics = compute_metrics(address, events, trades, delays, since)
            apply_filters(metrics, cfg)
        except Exception as exc:
            log.error("Cuzdan taranamadi (%s): %s", address, exc)
            state.bus.log("Cuzdan taranamadi: %s (%s)" % (address[:10], type(exc).__name__), "error")
            continue

        if not complete:
            # Sessizce gecilmemeli: kismi pencere, "eskiden hic islem yapmamis"
            # gibi gorunur ve cuzdani haksiz yere eler.
            metrics.rejected = ("imza butcesi (%d) pencereyi kapsamadi - metrikler "
                                "sadece son gunlere ait" % int(cfg.scorer_max_signatures))
            state.bus.log("%s ELENDI: %s" % (address[:10], metrics.rejected), "warn")
        payloads[address] = events
        results.append(metrics)
        db.upsert_wallet(address, source=candidate.get("source"), note=candidate.get("note"))
        state.bus.log(
            "%s -> %d trade, win %%%.1f, PnL %.3f SOL%s"
            % (address[:10], metrics.trade_count_30d, metrics.win_rate, metrics.total_pnl_sol,
               (" | ELENDI: " + metrics.rejected) if metrics.rejected else ""),
            "warn" if metrics.rejected else "success")

    score_population(results)

    for metrics in results:
        classification, confidence, reasoning = None, None, None
        if metrics.rejected:
            classification, confidence, reasoning = "script", 1.0, metrics.rejected
        elif use_ai:
            verdict = await classify_with_ai(metrics, payloads.get(metrics.address, []))
            if verdict:
                classification = verdict.get("classification")
                confidence = float(verdict.get("confidence") or 0.0)
                reasoning = str(verdict.get("reasoning") or "")[:2000]

        db.upsert_wallet(
            metrics.address,
            score=None if metrics.rejected else metrics.score,
            classification=classification,
            ai_confidence=confidence,
            ai_reasoning=reasoning,
            last_scored_at=datetime.now().isoformat(timespec="seconds"),
            **metrics.as_row())

    results.sort(key=lambda m: (m.rejected is not None, -m.score))
    state.bus.publish("wallets", db.get_wallets())
    state.bus.log("Skorlama bitti: %d/%d cuzdan gecti"
                  % (sum(1 for m in results if not m.rejected), len(results)), "success")
    return results


def _print_table(results: List[WalletMetrics]) -> None:
    print("\n%-46s %6s %7s %8s %9s %7s  %s"
          % ("CUZDAN", "SKOR", "WIN%", "PNL SOL", "TRADE", "TUTUS", "DURUM"))
    print("-" * 110)
    for m in results:
        status = m.rejected or (("SUPHELI: " + m.suspicious) if m.suspicious else "OK")
        print("%-46s %6.3f %6.1f%% %8.3f %9d %6.0fs  %s"
              % (m.address, m.score, m.win_rate, m.total_pnl_sol,
                 m.trade_count_30d, m.median_hold_seconds, status))
    print()


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
        state.reload_env()
    except ImportError:
        pass

    if not state.HELIUS_API_KEY:
        print("HELIUS_API_KEY yok - public RPC islem gecmisi icin yeterli degil.")
        return

    args = sys.argv[1:]
    use_ai = "--no-ai" not in args
    candidates = None
    if "--only" in args:
        address = args[args.index("--only") + 1]
        candidates = [{"address": address, "source": "cli", "note": ""}]

    db.init_db()
    try:
        results = await score_wallets(candidates, use_ai=use_ai)
        _print_table(results)
        print("Takibe almak icin panelden ya da:  POST /api/wallets/<adres>/follow")
    finally:
        await rpc.close()


if __name__ == "__main__":
    asyncio.run(_main())
