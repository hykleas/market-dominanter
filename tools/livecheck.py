"""Helius anahtari ile canli dogrulama: RPC + websocket + gercek coin analizi."""
import asyncio, sys, time
from pathlib import Path

ROOT = Path(r"C:\Users\Lenovo\market-fucker")
sys.path.insert(0, str(ROOT / "backend"))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import state
state.reload_env()
import analyzer, bot, rpc

BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


async def main():
    key = state.HELIUS_API_KEY
    print("anahtar:", (key[:8] + "...") if key else "YOK", "| uzunluk:", len(key))

    print("\n== RPC testleri (public RPC'de reddedilen cagrilar) ==")
    t0 = time.time()
    supply = await rpc.get_token_supply(BONK)
    print("  getTokenSupply      :", "OK" if supply else "FAIL", "| decimals:", (supply or {}).get("decimals"))
    largest = await rpc.get_token_largest_accounts(BONK)
    print("  getTokenLargestAccounts:", "OK" if largest else "FAIL", "| hesap:", len(largest))
    bal = await rpc.get_balance_sol("11111111111111111111111111111111")
    print("  getBalance          : OK | %.4f sn" % (time.time() - t0))

    print("\n== pump.fun websocket dinleme (60sn) ==")
    found = []
    seen_sigs = 0

    async def listen():
        nonlocal seen_sigs
        import json, websockets
        sub = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                          "params": [{"mentions": [state.PUMP_FUN_PROGRAM]}, {"commitment": "processed"}]})
        async with websockets.connect(state.ws_url(), ping_interval=20) as ws:
            await ws.send(sub)
            print("  baglandi, abone olundu")
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("method") != "logsNotification":
                    continue
                value = msg["params"]["result"]["value"]
                seen_sigs += 1
                if value.get("err") or not bot._is_create(value.get("logs") or []):
                    continue
                mint, creator = await bot._extract_mint(value["signature"])
                if mint and mint not in [m for m, _ in found]:
                    found.append((mint, creator))
                    print("  yeni launch #%d: %s (kurucu %s...)" % (len(found), mint, (creator or "?")[:8]))
                    if len(found) >= 3:
                        return

    try:
        await asyncio.wait_for(listen(), timeout=60)
    except asyncio.TimeoutError:
        print("  sure doldu")
    print("  toplam log bildirimi:", seen_sigs, "| bulunan yeni coin:", len(found))

    if not found:
        print("\nCoin yakalanamadi (sakin bir an olabilir).")
        await asyncio.gather(rpc.close(), analyzer.close())
        return

    mint, creator = found[0]
    print("\n== gercek coin analizi: %s ==" % mint)
    print("  10sn bekleniyor (analyze_delay)...")
    t0 = time.time()
    result = await analyzer.analyze(mint, creator=creator, delay=10)
    m = result.metrics
    print("  analiz suresi: %.1f sn" % (time.time() - t0))
    for k in ("name", "price_usd", "market_cap", "volume_5m", "liquidity_usd", "dex_id",
              "supply", "top10", "dev", "bundler", "sniper", "lp_burned", "freeze_authority"):
        print("    %-16s %s" % (k, getattr(m, k)))
    print("  SONUC:", "BOUGHT" if result.passed else "REJECTED", "|", result.reason_text)

    await asyncio.gather(rpc.close(), analyzer.close())


asyncio.run(main())
