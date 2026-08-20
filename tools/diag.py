"""bundler/sniper neden None donuyor: adim adim teshis."""
import asyncio, sys, traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import logging
logging.getLogger("httpx").setLevel(logging.WARNING)

import state
state.reload_env()
import analyzer, pumpfun, rpc

MINTS = {
    "The Inventor (sakin)": "8gFAdbKMgDAktorfavwVhjQDQHE5YjPoKdsCQP7331Ce",
    "Newbie (calisan)": "5kxBt3VJUTTqn4N7AkR3coXNRd5t3jgcyitkQuAJpump",
    "The Jumping (yogun)": "66d6qtf85nwji2UnMTvWL6qzckRKdKUDEMJkW6LRpump",
}


async def main():
    for label, mint in MINTS.items():
        print("\n=== %s ===" % label)
        try:
            page = await rpc.get_signatures(mint, limit=100)
            print("  ilk sayfa imza:", len(page))
            sigs, complete = await analyzer._oldest_signatures(mint)
            print("  _oldest_signatures -> %d imza, complete=%s" % (len(sigs), complete))
            if sigs:
                launch = sigs[0]
                lt = launch.get("blockTime") or 0
                window = [s for s in sigs if s.get("slot") == launch.get("slot")
                          or (s.get("blockTime") or 0) - lt <= analyzer.SNIPER_WINDOW_SEC]
                print("  launch slot:", launch.get("slot"), "| pencere islem sayisi:", len(window),
                      "| MAX_EARLY_TX:", analyzer.MAX_EARLY_TX)
                print("  ilk tx sig:", launch["signature"][:16], "...")
                tx = await rpc.get_transaction(launch["signature"])
                print("  ilk tx cekilebildi:", bool(tx),
                      "| postTokenBalances:", len(((tx or {}).get("meta") or {}).get("postTokenBalances") or []))
            b, sn, lt, trunc = await analyzer._early_buyers(mint)
            print("  _early_buyers -> bundled=%s sniped=%s truncated=%s" % (b, sn, trunc))
            total, circ, holders = await analyzer._real_holders(mint)
            print("  _real_holders -> total=%s circulating=%s holders=%d" % (total, circ, len(holders)))
            curve = await pumpfun.read_curve(mint)
            if curve:
                print("  bonding curve -> %.4f SOL icerde, fiyat %.10f SOL, complete=%s"
                      % (curve.sol_in_curve, curve.price_sol or 0.0, curve.complete))
            else:
                print("  bonding curve -> okunamadi (migrate olmus olabilir)")
        except Exception:
            traceback.print_exc()
    await rpc.close()


asyncio.run(main())
