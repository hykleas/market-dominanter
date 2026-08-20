"""Unit tests for the scorer's FIFO trade matching and tx parsing.

Both are pure functions with no network access, so they run anywhere:

    python -m pytest tests/ -q
    python tests/test_fifo.py        # pytest kurulu degilse
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from wallet_scorer import (  # noqa: E402
    ClosedTrade, WalletEvent, compute_metrics, match_fifo, parse_wallet_tx,
)

MINT = "So1aNaMint1111111111111111111111111111111111"
OTHER = "So1aNaMint2222222222222222222222222222222222"
WALLET = "Wa11et11111111111111111111111111111111111111"
WSOL = "So11111111111111111111111111111111111111112"


def buy(ts, sol, tokens, mint=MINT):
    return WalletEvent(ts=ts, mint=mint, side="buy", sol=sol, tokens=tokens)


def sell(ts, sol, tokens, mint=MINT):
    return WalletEvent(ts=ts, mint=mint, side="sell", sol=sol, tokens=tokens)


def approx(a, b, tol=1e-9):
    return abs(a - b) <= tol


# --------------------------------------------------------------------------- #
# match_fifo
# --------------------------------------------------------------------------- #
def test_single_round_trip():
    trades = match_fifo([buy(100, 1.0, 1000), sell(200, 1.5, 1000)])
    assert len(trades) == 1
    t = trades[0]
    assert approx(t.cost_sol, 1.0) and approx(t.proceeds_sol, 1.5)
    assert approx(t.pnl_sol, 0.5) and approx(t.pnl_pct, 50.0)
    assert t.hold_seconds == 100


def test_partial_sell_splits_cost_proportionally():
    """Selling 40% of a lot must consume 40% of its cost, not all of it."""
    trades = match_fifo([buy(100, 1.0, 1000), sell(150, 0.6, 400)])
    assert len(trades) == 1
    assert approx(trades[0].cost_sol, 0.4)
    assert approx(trades[0].proceeds_sol, 0.6)
    assert approx(trades[0].pnl_sol, 0.2)


def test_fifo_order_consumes_oldest_lot_first():
    """Two lots at different prices; one sell must eat the OLDER one first."""
    trades = match_fifo([buy(100, 1.0, 1000), buy(150, 3.0, 1000), sell(200, 2.0, 1000)])
    assert len(trades) == 1
    assert approx(trades[0].cost_sol, 1.0)      # cheap lot, not the 3.0 one
    assert trades[0].entry_ts == 100


def test_sell_spanning_two_lots_produces_two_trades():
    trades = match_fifo([buy(100, 1.0, 1000), buy(150, 3.0, 1000), sell(200, 6.0, 1500)])
    assert len(trades) == 2
    assert approx(trades[0].cost_sol, 1.0)
    assert approx(trades[0].proceeds_sol, 4.0)   # 1000/1500 of 6.0
    assert approx(trades[1].cost_sol, 1.5)       # half of the 3.0 lot
    assert approx(trades[1].proceeds_sol, 2.0)
    assert approx(sum(t.proceeds_sol for t in trades), 6.0)


def test_unmatched_sell_is_ignored():
    """A sell with no preceding buy (position opened before our window, or an
    airdrop) must NOT invent a zero cost basis - that would print a fake win."""
    trades = match_fifo([sell(200, 5.0, 1000)])
    assert trades == []


def test_open_position_is_not_counted():
    trades = match_fifo([buy(100, 1.0, 1000)])
    assert trades == []


def test_mints_do_not_cross_contaminate():
    trades = match_fifo([buy(100, 1.0, 1000, MINT), buy(110, 9.0, 1000, OTHER),
                         sell(200, 2.0, 1000, MINT)])
    assert len(trades) == 1
    assert trades[0].mint == MINT
    assert approx(trades[0].cost_sol, 1.0)


def test_events_are_sorted_and_buy_wins_ties():
    """Same-timestamp buy+sell: the buy must be booked first, or the round trip
    is silently dropped as an unmatched sell."""
    trades = match_fifo([sell(100, 1.5, 1000), buy(100, 1.0, 1000)])
    assert len(trades) == 1
    assert approx(trades[0].pnl_sol, 0.5)


def test_loss_is_negative():
    trades = match_fifo([buy(100, 1.0, 1000), sell(200, 0.1, 1000)])
    assert approx(trades[0].pnl_sol, -0.9)
    assert approx(trades[0].pnl_pct, -90.0)


# --------------------------------------------------------------------------- #
# parse_wallet_tx
# --------------------------------------------------------------------------- #
def _tx(sol_pre, sol_post, token_pre, token_post, fee=5000, mint=MINT, err=None):
    def balances(amount):
        if amount is None:
            return []
        return [{"owner": WALLET, "mint": mint, "uiTokenAmount": {"uiAmount": amount}}]
    return {
        "blockTime": 1700000000,
        "meta": {"err": err, "fee": fee,
                 "preBalances": [sol_pre], "postBalances": [sol_post],
                 "preTokenBalances": balances(token_pre),
                 "postTokenBalances": balances(token_post)},
        "transaction": {"message": {"accountKeys": [{"pubkey": WALLET, "signer": True}]},
                        "signatures": ["sig"]},
    }


def test_parse_buy():
    # 1 SOL out (plus fee), 1000 tokens in.
    event = parse_wallet_tx(_tx(2_000_000_000, 1_000_000_000 - 5000, None, 1000), WALLET)
    assert event is not None
    assert event.side == "buy" and event.mint == MINT
    assert approx(event.tokens, 1000)
    assert approx(event.sol, 1.0)     # fee added back: trade size, not wallet delta


def test_parse_sell():
    event = parse_wallet_tx(_tx(1_000_000_000, 2_000_000_000 - 5000, 1000, 0), WALLET)
    assert event is not None
    assert event.side == "sell"
    assert approx(event.sol, 1.0)


def test_failed_tx_is_skipped():
    assert parse_wallet_tx(_tx(2_000_000_000, 1_000_000_000, None, 1000, err={"x": 1}), WALLET) is None


def test_plain_transfer_is_not_a_trade():
    """Tokens in with no SOL out is an airdrop/transfer, not a buy."""
    assert parse_wallet_tx(_tx(1_000_000_000, 1_000_000_000 - 5000, None, 1000), WALLET) is None


def test_other_wallet_is_ignored():
    assert parse_wallet_tx(_tx(2_000_000_000, 1_000_000_000, None, 1000), "SomeoneElse") is None


def test_wsol_counts_as_cash_not_as_a_token():
    """Wrapping SOL moves a WSOL token balance; treating it as the traded token
    would classify every Jupiter swap as a WSOL trade."""
    tx = {
        "blockTime": 1700000000,
        "meta": {"err": None, "fee": 5000,
                 "preBalances": [2_000_000_000], "postBalances": [2_000_000_000 - 5000],
                 "preTokenBalances": [{"owner": WALLET, "mint": WSOL,
                                       "uiTokenAmount": {"uiAmount": 1.0}}],
                 "postTokenBalances": [{"owner": WALLET, "mint": MINT,
                                        "uiTokenAmount": {"uiAmount": 1000}}]},
        "transaction": {"message": {"accountKeys": [{"pubkey": WALLET}]},
                        "signatures": ["sig"]},
    }
    event = parse_wallet_tx(tx, WALLET)
    assert event is not None
    assert event.mint == MINT and event.side == "buy"
    assert approx(event.sol, 1.0)      # the unwrapped WSOL is the SOL spent


# --------------------------------------------------------------------------- #
# compute_metrics
# --------------------------------------------------------------------------- #
def test_metrics_win_rate_and_consistency():
    events = [buy(100, 1.0, 1000), sell(200, 2.0, 1000),
              buy(300, 0.5, 500, OTHER), sell(400, 0.1, 500, OTHER)]
    trades = match_fifo(events)
    m = compute_metrics(WALLET, events, trades, {}, window_start=0)
    assert m.trade_count_30d == 2
    assert approx(m.win_rate, 50.0)
    assert approx(m.total_pnl_sol, 0.6)          # +1.0 and -0.4
    assert m.unique_tokens_30d == 2
    assert m.buy_count == 2
    assert m.buy_size_consistency > 0.0          # 1.0 vs 0.5 -> not a script
    assert m.median_entry_delay is None          # no launch data supplied


def test_metrics_flags_a_constant_size_script():
    events = []
    for i in range(6):
        events.append(buy(100 + i * 10, 1.0, 1000, MINT + str(i)))
        events.append(sell(105 + i * 10, 1.1, 1000, MINT + str(i)))
    m = compute_metrics(WALLET, events, match_fifo(events), {}, window_start=0)
    assert approx(m.buy_size_consistency, 0.0)   # identical sizes every time
    assert approx(m.median_hold_seconds, 5.0)


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
