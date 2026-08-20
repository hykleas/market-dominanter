"""Unit tests for the backtester's pure simulation (no network).

    python tests/test_backtest.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import state  # noqa: E402
from backtester import simulate_wallet, split_events  # noqa: E402
from wallet_scorer import ClosedTrade, WalletEvent  # noqa: E402

WALLET = "Wa11et11111111111111111111111111111111111111"
NOW = 1_800_000_000.0
DAY = 86400.0
SPLIT = NOW - 15 * DAY


def cfg(**over):
    c = state.Settings()
    c.copy_size_sol = 0.1
    c.min_leader_buy_sol = 0.5
    c.fee_platform_pct = 1.0
    c.fee_network_sol = 0.000005
    c.fee_priority_sol = 0.001
    c.paper_slippage_pct = 1.5
    c.hard_stop_pct = 35.0
    c.time_stop_minutes = 45.0
    c.time_stop_min_pnl = 10.0
    for k, v in over.items():
        setattr(c, k, v)
    return c


def trade(entry_ts, pnl_pct, cost=1.0, hold=600.0, mint="M1"):
    """Lider islemi: verilen getiriyi uretecek sekilde kurulur."""
    return ClosedTrade(mint=mint, entry_ts=entry_ts, exit_ts=entry_ts + hold,
                       cost_sol=cost, proceeds_sol=cost * (1 + pnl_pct / 100.0), tokens=1000)


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


# --------------------------------------------------------------------------- #
# Pencere secimi  -  walk-forward'in kalbi
# --------------------------------------------------------------------------- #
def test_trades_before_the_split_are_not_counted():
    """Secim doneminde yapilmis islem PnL'e girmemeli; yoksa cuzdani secmek
    icin kullandigimiz veriyle onu test etmis oluruz (survivorship bias)."""
    sims, skipped = simulate_wallet(WALLET, [trade(SPLIT - DAY, 500.0)], cfg(), SPLIT, NOW)
    assert sims == []
    assert skipped.get("test_penceresi_disi") == 1


def test_trades_after_the_split_are_counted():
    sims, _ = simulate_wallet(WALLET, [trade(SPLIT + DAY, 50.0)], cfg(), SPLIT, NOW)
    assert len(sims) == 1


def test_future_trades_are_excluded():
    sims, skipped = simulate_wallet(WALLET, [trade(NOW + DAY, 50.0)], cfg(), SPLIT, NOW)
    assert sims == [] and skipped.get("test_penceresi_disi") == 1


def test_split_events_partitions_on_timestamp():
    events = [WalletEvent(ts=SPLIT - 10, mint="M", side="buy", sol=1, tokens=1),
              WalletEvent(ts=SPLIT + 10, mint="M", side="sell", sol=2, tokens=1)]
    early, late = split_events(events, SPLIT)
    assert len(early) == 1 and len(late) == 1


# --------------------------------------------------------------------------- #
# Kapilar
# --------------------------------------------------------------------------- #
def test_dust_buys_are_skipped():
    sims, skipped = simulate_wallet(WALLET, [trade(SPLIT + DAY, 100.0, cost=0.2)],
                                    cfg(min_leader_buy_sol=0.5), SPLIT, NOW)
    assert sims == [] and skipped.get("dust") == 1


def test_our_size_is_independent_of_the_leader_size():
    """Lider 50 SOL girse de biz copy_size_sol kadar gireriz."""
    sims, _ = simulate_wallet(WALLET, [trade(SPLIT + DAY, 0.0, cost=50.0)],
                              cfg(copy_size_sol=0.1), SPLIT, NOW)
    assert approx(sims[0].size_sol, 0.1)


# --------------------------------------------------------------------------- #
# Ucret ve slipaj
# --------------------------------------------------------------------------- #
def test_entry_fee_is_inside_the_cost_basis():
    """19 Agustos'taki muhasebe hatasi burada tekrarlanmamali: giris sabit
    ucreti maliyete dahil olmali."""
    sims, _ = simulate_wallet(WALLET, [trade(SPLIT + DAY, 0.0)], cfg(), SPLIT, NOW)
    s = sims[0]
    assert s.cost_sol > s.size_sol
    assert approx(s.cost_sol, 0.1 + 0.000005 + 0.001)


def test_a_flat_leader_trade_loses_money_on_fees():
    """Lider basabas cikmissa biz ucret + slipaj yuzunden zarar etmeliyiz."""
    sims, _ = simulate_wallet(WALLET, [trade(SPLIT + DAY, 0.0)], cfg(), SPLIT, NOW)
    assert sims[0].pnl_sol < 0


def test_break_even_needs_a_real_gain():
    """0.1 SOL pozisyonda basabas ~%6-7 civari olmali (0.01'de ~%25 idi)."""
    losing, _ = simulate_wallet(WALLET, [trade(SPLIT + DAY, 4.0)], cfg(), SPLIT, NOW)
    winning, _ = simulate_wallet(WALLET, [trade(SPLIT + DAY, 12.0)], cfg(), SPLIT, NOW)
    assert losing[0].pnl_sol < 0 < winning[0].pnl_sol


def test_latency_reduces_pnl():
    base, _ = simulate_wallet(WALLET, [trade(SPLIT + DAY, 100.0)], cfg(), SPLIT, NOW,
                              latency_sec=0.0)
    slow, _ = simulate_wallet(WALLET, [trade(SPLIT + DAY, 100.0)], cfg(), SPLIT, NOW,
                              latency_sec=60.0)
    assert slow[0].pnl_sol < base[0].pnl_sol


def test_latency_hurts_fast_trades_far_more_than_slow_ones():
    """Ayni %50 getiri: 2 dakikada kazanilmissa 8 saniye gecikme hareketin
    %6.7'sini yer; 8 saatte kazanilmissa neredeyse hicbir sey."""
    fast, _ = simulate_wallet(WALLET, [trade(SPLIT + DAY, 50.0, hold=120)],
                              cfg(), SPLIT, NOW, latency_sec=8.0)
    slow, _ = simulate_wallet(WALLET, [trade(SPLIT + DAY, 50.0, hold=8 * 3600)],
                              cfg(time_stop_minutes=10**9), SPLIT, NOW, latency_sec=8.0)
    assert slow[0].pnl_sol > fast[0].pnl_sol
    # Yavas islemde kayip ihmal edilebilir olmali
    assert abs(slow[0].leader_pnl_pct - 50.0) < 1e-6


def test_latency_cannot_exceed_the_whole_move():
    """Tutus, gecikmeden kisaysa getirinin tamami kacar - eksiye donmez."""
    sims, _ = simulate_wallet(WALLET, [trade(SPLIT + DAY, 500.0, hold=2)],
                              cfg(), SPLIT, NOW, latency_sec=8.0)
    # Hareketin tamami kacirildi: geriye yalnizca ucret+slipaj zarari kalir
    assert sims[0].pnl_sol < 0


# --------------------------------------------------------------------------- #
# Guvenlik aglari
# --------------------------------------------------------------------------- #
def test_hard_stop_caps_the_loss():
    """Lider -%90 yemis olsa bile hard stop bizi -%35 civarinda durdurmali."""
    sims, _ = simulate_wallet(WALLET, [trade(SPLIT + DAY, -90.0)], cfg(hard_stop_pct=35.0),
                              SPLIT, NOW)
    s = sims[0]
    assert s.exit_reason == "hard_stop"
    assert s.pnl_pct > -50.0        # ucretlerle birlikte -%35'in biraz altinda


def test_time_stop_fires_on_a_long_flat_hold():
    sims, _ = simulate_wallet(WALLET, [trade(SPLIT + DAY, 2.0, hold=3 * 3600)],
                              cfg(time_stop_minutes=45, time_stop_min_pnl=10), SPLIT, NOW)
    s = sims[0]
    assert s.exit_reason == "time_stop"
    assert approx(s.hold_seconds, 45 * 60)


def test_a_big_winner_is_not_time_stopped():
    sims, _ = simulate_wallet(WALLET, [trade(SPLIT + DAY, 300.0, hold=5 * 3600)],
                              cfg(), SPLIT, NOW)
    assert sims[0].exit_reason == "leader_exit"


def test_proceeds_never_go_negative():
    sims, _ = simulate_wallet(WALLET, [trade(SPLIT + DAY, -99.9)], cfg(hard_stop_pct=95.0),
                              SPLIT, NOW)
    assert sims[0].proceeds_sol >= 0.0


# --------------------------------------------------------------------------- #
# Toplam
# --------------------------------------------------------------------------- #
def test_multiple_trades_accumulate():
    leader = [trade(SPLIT + DAY, 200.0, mint="A"), trade(SPLIT + 2 * DAY, -50.0, mint="B"),
              trade(SPLIT + 3 * DAY, 0.0, mint="C")]
    sims, _ = simulate_wallet(WALLET, leader, cfg(), SPLIT, NOW)
    assert len(sims) == 3
    assert sims[0].pnl_sol > 0 and sims[1].pnl_sol < 0
    assert all(s.wallet == WALLET for s in sims)


def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for test in tests:
        try:
            test()
            print("  PASS  %s" % test.__name__)
        except AssertionError as exc:
            failed += 1
            print("  FAIL  %s  %s" % (test.__name__, exc))
        except Exception as exc:
            failed += 1
            print("  ERROR %s  %s: %s" % (test.__name__, type(exc).__name__, exc))
    print("\n%d/%d gecti" % (len(tests) - failed, len(tests)))
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
