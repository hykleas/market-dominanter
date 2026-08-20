"""Paper wallet + tier/trailing strategy self test with scripted prices."""
import asyncio, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
HERE = Path(__file__).parent

import state
state.SETTINGS_FILE = HERE / "selftest_settings.json"
import analyzer, database as db, trader

db.DB_PATH = HERE / "selftest2.db"
for f in ("selftest2.db", "selftest2.db-wal", "selftest2.db-shm", "selftest_settings.json"):
    (HERE / f).unlink(missing_ok=True)

PRICE = {"token": 0.001}
MINT = "TestMint1111111111111111111111111111111pump"


async def fake_price(mint):
    return 100.0 if mint == state.WSOL_MINT else PRICE["token"]


analyzer.current_price = fake_price


def show(tag):
    pos = db.get_open_positions()
    print("  %-22s bakiye=%.6f SOL | acik=%d %s" % (
        tag, state.settings.paper_balance_sol, len(pos),
        [(p["id"], "t1" if p["tier1_sold"] else "-", "t2" if p["tier2_sold"] else "-",
          "t3" if p["tier3_sold"] else "-", "sold%%=%.0f" % p["total_sold_percent"],
          "ath=%.5f" % p["ath_price"], "trail=%.5f" % p["trailing_stop_price"]) for p in pos]))


async def tick(pos_id):
    pos = db.get_position(pos_id)
    if pos and pos["status"] == "open":
        await trader._check_position(pos)


async def main():
    db.init_db()
    trader.load_wallet()
    s = state.settings
    s.paper_trading = True
    s.auto_buy_sol = 0.05
    s.tier1_x, s.tier2_x, s.tier3_x = 2.0, 5.0, 10.0
    s.tier1_pct = s.tier2_pct = s.tier3_pct = 25.0
    s.trailing_stop = 30.0
    s.stop_loss = 30.0

    print("== paper cuzdan ==")
    w = await trader.reset_paper_wallet(20.0)
    print("  fon:", round(w["balance_sol"], 6), "SOL @ $100/SOL =", "$%.2f" % (w["balance_sol"] * 100))
    assert abs(w["balance_sol"] - 0.2) < 1e-9, w
    print("  is_paper:", trader.is_paper(), "| can_go_live:", trader.can_go_live())

    print("\n== alim (fee dusulerek) ==")
    pid = await trader.buy(MINT, "TESTCOIN", price_hint=0.001)
    pos = db.get_position(pid)
    print("  token:", pos["amount_token"], "| entry:", pos["entry_price"],
          "| fees:", round(pos["fees_sol"], 6), "| is_paper:", pos["is_paper"])
    assert abs(pos["amount_token"] - 4950.0) < 1e-6
    assert abs(state.settings.paper_balance_sol - (0.2 - 0.051005)) < 1e-9
    show("alim sonrasi")

    print("\n== tier 1 (2x) ==")
    PRICE["token"] = 0.002
    await tick(pid); show("2x")
    p = db.get_position(pid)
    assert p["tier1_sold"] == 1 and abs(p["total_sold_percent"] - 25) < 1e-6

    print("\n== tier 2 (5x) + tier 3 (10x) ==")
    PRICE["token"] = 0.005
    await tick(pid); show("5x")
    PRICE["token"] = 0.01
    await tick(pid); show("10x")
    p = db.get_position(pid)
    assert p["tier2_sold"] == 1 and p["tier3_sold"] == 1
    assert abs(p["total_sold_percent"] - 75) < 1e-6

    print("\n== ATH 20x, sonra -30% -> trailing stop ==")
    PRICE["token"] = 0.02
    await tick(pid); show("20x (ATH)")
    p = db.get_position(pid)
    assert abs(p["ath_price"] - 0.02) < 1e-12 and abs(p["trailing_stop_price"] - 0.014) < 1e-12
    PRICE["token"] = 0.0139
    await tick(pid); show("13.9x (trail)")
    p = db.get_position(pid)
    assert p["status"] == "closed", p["status"]

    print("\n== islem gecmisi ==")
    for t in reversed(db.get_trades()):
        print("  %-13s %%%-4.0f exit=%.5f pnl=%+.5f SOL (%.0f%%) paper=%d"
              % (t["tier"], t["sold_percent"], t["exit_price"], t["pnl_sol"], t["pnl_percent"], t["is_paper"]))
    trades = db.get_trades()
    assert len(trades) == 4, len(trades)
    assert {t["tier"] for t in trades} == {"tier1", "tier2", "tier3", "trailing_stop"}
    print("  paper cuzdan:", {k: round(v, 6) if isinstance(v, float) else v
                              for k, v in trader.paper_wallet().items()})

    print("\n== stop loss (tier oncesi -35%) ==")
    PRICE["token"] = 0.001
    pid2 = await trader.buy(MINT, "TESTCOIN2", price_hint=0.001)
    PRICE["token"] = 0.00065
    await tick(pid2)
    p2 = db.get_position(pid2)
    last = db.get_trades()[0]
    print("  durum:", p2["status"], "| tier:", last["tier"], "| pnl:", round(last["pnl_sol"], 6))
    assert p2["status"] == "closed" and last["tier"] == "stop_loss"

    print("\n== stop loss tier1'den SONRA calismamali ==")
    PRICE["token"] = 0.001
    pid3 = await trader.buy(MINT, "TESTCOIN3", price_hint=0.001)
    PRICE["token"] = 0.002          # tier1
    await tick(pid3)
    PRICE["token"] = 0.0006         # entry'nin -40%'i, ama tier1 alindi
    await tick(pid3)
    p3 = db.get_position(pid3)
    tiers = [t["tier"] for t in db.get_trades() if t["exit_time"] >= p3["timestamp"]]
    print("  durum:", p3["status"], "| tetiklenen:", tiers)
    assert "stop_loss" not in tiers, tiers
    assert p3["status"] == "closed" and "trailing_stop" in tiers

    print("\n== toplam ==")
    print("  stats(paper):", {k: round(v, 6) if isinstance(v, float) else v
                              for k, v in db.stats(is_paper=True).items()})
    print("  bakiye: %.6f SOL (baslangic 0.2)" % state.settings.paper_balance_sol)
    print("\nTUM TESTLER GECTI")


asyncio.run(main())
