"""Duzeltme sonrasi canli kalibrasyon: launch imzasi ile analiz."""
import asyncio, json, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import logging
logging.getLogger("httpx").setLevel(logging.WARNING)

import state
state.reload_env()
import analyzer, bot, rpc
import websockets

WANT = 6
LISTEN_SEC = 90
FOUND = []


async def collect():
    sub = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                      "params": [{"mentions": [state.PUMP_FUN_PROGRAM]}, {"commitment": "processed"}]})
    async with websockets.connect(state.ws_url(), ping_interval=20) as ws:
        await ws.send(sub)
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("method") != "logsNotification":
                continue
            value = msg["params"]["result"]["value"]
            if value.get("err") or not bot._is_create(value.get("logs") or []):
                continue
            sig = value["signature"]
            mint, creator = await bot._extract_mint(sig)
            if mint and mint not in [m for m, _, _, _ in FOUND]:
                FOUND.append((mint, creator, sig, time.time()))
                print("  yakalandi: %s" % mint)
                if len(FOUND) >= WANT:
                    return


async def main():
    print("== %dsn launch yakalama ==" % LISTEN_SEC)
    try:
        await asyncio.wait_for(collect(), timeout=LISTEN_SEC)
    except asyncio.TimeoutError:
        pass
    if not FOUND:
        print("coin yok"); return

    print("\n== analiz (launch + 20sn, launch imzasi ile) ==")
    sem = asyncio.Semaphore(2)

    async def run(item):
        mint, creator, sig, seen = item
        async with sem:
            wait = max(0.0, state.settings.analyze_delay - (time.time() - seen))
            t0 = time.time()
            res = await analyzer.analyze(mint, creator=creator, delay=wait, launch_signature=sig)
            return mint, res, time.time() - t0 - wait

    results = await asyncio.gather(*(run(f) for f in FOUND), return_exceptions=True)

    print("\n%-14s %-8s %-8s %-7s %-7s %-8s %-7s %-5s %s" %
          ("isim", "mcap$", "5m hac$", "top10%", "dev%", "bundler%", "sniper%", "sure", "sonuc"))
    missing = 0
    reasons = {}
    for item in results:
        if isinstance(item, BaseException):
            print("  hata:", repr(item)); continue
        mint, res, secs = item
        m = res.metrics
        f = lambda v, d=1: ("%.*f" % (d, v)) if isinstance(v, (int, float)) else "YOK"
        if m.bundler is None or m.sniper is None:
            missing += 1
        print("%-14s %-8s %-8s %-7s %-7s %-8s %-7s %-13s %-5.1f %s" % (
            (m.name or "?")[:14], f(m.market_cap, 0), f(m.curve_sol, 2), f(m.top10),
            f(m.dev), f(m.bundler), f(m.sniper), m.source, secs,
            "PASS" if res.passed else "FAIL"))
        if not res.passed:
            print("               %s" % res.reason_text)
        for r in res.reasons:
            reasons[r.split(" ")[0] if "verisi yok" not in r else r] = \
                reasons.get(r.split(" ")[0] if "verisi yok" not in r else r, 0) + 1

    n = len([r for r in results if not isinstance(r, BaseException)])
    print("\nbundler/sniper eksik: %d/%d" % (missing, n))
    print("elenme sebepleri:")
    for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print("  %-26s %d/%d" % (k, v, n))
    print("RPC istatistik:", rpc.stats)
    await asyncio.gather(rpc.close(), analyzer.close())


asyncio.run(main())
