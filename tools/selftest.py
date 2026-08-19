"""End-to-end self test: metrics -> rules -> paper buy -> monitor -> paper sell."""
import asyncio, sys, os
sys.path.insert(0, r"C:\Users\Lenovo\market-fucker\backend")
os.environ["PAPER_TRADING"] = "1"

import analyzer, database as db, rpc, state, trader

BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


async def main():
    db.DB_PATH = db.Path(os.path.join(os.path.dirname(__file__), "selftest.db"))
    db.init_db()
    trader.load_wallet()
    print("paper mode:", trader.is_paper())

    print("\n-- dexscreener --")
    pair = await analyzer.fetch_dexscreener(BONK)
    print("pair found:", bool(pair), "dex:", (pair or {}).get("dexId"))

    print("\n-- metrics (deep on-chain) --")
    m = await analyzer.collect_metrics(BONK, deep=True)
    for k in ("name", "price_usd", "market_cap", "volume_5m", "liquidity_usd",
              "supply", "top10", "dev", "bundler", "sniper", "lp_burned", "freeze_authority"):
        print("  %-16s %s" % (k, getattr(m, k)))

    print("\n-- rules (default settings) --")
    res = analyzer.evaluate(m, state.settings)
    print("  passed:", res.passed, "|", res.reason_text)

    print("\n-- rules (relaxed, should pass) --")
    cfg = state.Settings(min_mcap=0, max_mcap=1e15, min_volume_5m=-1, max_top10=101,
                         max_bundler=101, max_sniper=101, max_dev_holdings=101)
    m2 = analyzer.Metrics(mint=BONK, name=m.name, price_usd=m.price_usd or 1e-5,
                          market_cap=m.market_cap or 1000, volume_5m=m.volume_5m or 1000,
                          bundler=0.0, sniper=0.0, dev=0.0, top10=0.0, lp_burned=True)
    res2 = analyzer.evaluate(m2, cfg)
    print("  passed:", res2.passed, "|", res2.reason_text)

    print("\n-- paper buy --")
    state.settings.auto_buy_sol = 0.05
    pos_id = await trader.buy(BONK, m.name or "BONK", price_hint=m2.price_usd)
    print("  position id:", pos_id)
    print("  open:", [(p["id"], p["name"], p["entry_price"], p["amount_token"]) for p in db.get_open_positions()])

    print("\n-- monitor tick --")
    pos = db.get_position(pos_id)
    await trader._check_position(pos)
    print("  after tick:", trader.position_payload(db.get_position(pos_id)))

    print("\n-- stop loss trigger (fake entry 2x price) --")
    db.update_position_price(pos_id, 0)
    with db._lock:
        db._connect().execute("UPDATE positions SET entry_price=? WHERE id=?",
                              ((m2.price_usd or 1e-5) * 2, pos_id))
        db._connect().commit()
    await trader._check_position(db.get_position(pos_id))
    print("  open positions now:", len(db.get_open_positions()))
    print("  trades:", [(t["name"], round(t["pnl_percent"], 2), round(t["pnl_sol"], 5)) for t in db.get_trades()])
    print("  stats:", db.stats())

    await asyncio.gather(rpc.close(), analyzer.close(), trader.close())


asyncio.run(main())
