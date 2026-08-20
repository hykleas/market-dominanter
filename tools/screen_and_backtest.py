"""Tara -> ele -> backtest zinciri, tek komutta.

Elle secilmis cuzdanlari (ya da kesfin urettiklerini) once UCUZ taramadan
gecirir, kopyalanabilir profildekileri ayirir ve yalnizca onlari backtest eder.

Neden zincir: pahali adim backtest (cuzdan basina dakikalar). Onune ucuz tarama
koymak, kopyalanamaz bir cuzdana hic vakit harcamamayi saglar. Olculen ornek:
medyan tutusu 0 dakika olan bir cuzdan taramada 14 saniyede elendi; backtest
ile ayni sonuca varmak 5 dakika suruyordu.

    python tools/screen_and_backtest.py                 # source=axiom olanlar
    python tools/screen_and_backtest.py --source all
    python tools/screen_and_backtest.py --screen-only
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import rpc  # noqa: E402
import state  # noqa: E402

state.reload_env()

from backtester import backtest, report  # noqa: E402
from wallet_scorer import probe_activity, probe_trading_style  # noqa: E402

CANDIDATES = ROOT / "wallets_candidates.json"


def load(source: str) -> List[Dict[str, Any]]:
    data = json.loads(CANDIDATES.read_text(encoding="utf-8"))
    rows = [x for x in data if isinstance(x, dict) and x.get("address")]
    if source != "all":
        rows = [x for x in rows if x.get("source") == source]
    return rows


async def screen(rows: List[Dict[str, Any]]) -> Tuple[List[str], List[Tuple[str, str]]]:
    """(aday adresler, [(adres, red sebebi)])."""
    cfg = state.settings
    since = time.time() - 30 * 86400
    good: List[str] = []
    bad: List[Tuple[str, str]] = []

    print("%-14s %10s %11s %11s %8s  %s"
          % ("CUZDAN", "ISLEM/GUN", "TUTUS", "MED ALIM", "TRADE", "KARAR"))
    print("-" * 76)
    for row in rows:
        address = row["address"]
        try:
            activity = await probe_activity(address, since)
        except Exception as exc:
            bad.append((address, "taranamadi: %s" % type(exc).__name__))
            continue

        if activity.signatures == 0:
            print("%-14s %10s %11s %11s %8s  ISLEM YOK" % (address[:12], "-", "-", "-", "-"))
            bad.append((address, "hic islem yok"))
            continue
        if activity.tx_per_day > cfg.scorer_max_tx_per_day:
            print("%-14s %10.0f %11s %11s %8s  BOT (hiz)"
                  % (address[:12], activity.tx_per_day, "-", "-", "-"))
            bad.append((address, "bot hizi (%.0f islem/gun)" % activity.tx_per_day))
            continue

        style = await probe_trading_style(address, int(cfg.scorer_style_sample))
        if not style.trades:
            print("%-14s %10.0f %11s %11s %8d  TRADE YOK"
                  % (address[:12], activity.tx_per_day, "-", "-", 0))
            bad.append((address, "ornekte kapanmis trade yok"))
            continue

        hold_ok = style.median_hold_seconds >= cfg.scorer_min_hold_seconds
        verdict = "*** ADAY ***" if hold_ok else "BOT (tutus)"
        print("%-14s %10.0f %8.0f sn %8.3f SOL %8d  %s"
              % (address[:12], activity.tx_per_day, style.median_hold_seconds,
                 style.median_buy_sol, style.trades, verdict))
        if hold_ok:
            good.append(address)
        else:
            bad.append((address, "medyan tutus %.0f sn < %.0f sn"
                        % (style.median_hold_seconds, cfg.scorer_min_hold_seconds)))
    return good, bad


async def main() -> int:
    logging.basicConfig(level=logging.ERROR)
    parser = argparse.ArgumentParser(prog="tools/screen_and_backtest.py")
    parser.add_argument("--source", default="axiom", help="aday kaynagi ('all' hepsi)")
    parser.add_argument("--screen-only", action="store_true", help="backtest yapma")
    parser.add_argument("--lookback-days", type=float, default=14.0)
    parser.add_argument("--split-days", type=float, default=6.0)
    parser.add_argument("--max-signatures", type=int, default=9000)
    parser.add_argument("--min-leader-buy", type=float, default=0.005)
    args = parser.parse_args()

    if not state.HELIUS_API_KEY:
        print("HELIUS_API_KEY yok.")
        return 1

    rows = load(args.source)
    if not rows:
        print("'%s' kaynagi icin aday yok." % args.source)
        return 1
    print("\n%d cuzdan taraniyor (kaynak: %s)\n" % (len(rows), args.source))

    try:
        good, bad = await screen(rows)
        print("\n%d/%d cuzdan aday profilinde" % (len(good), len(rows)))
        for address in good:
            print("   ", address)
        if not good:
            print("\nAday cikmadi - backtest atlandi.")
            return 0
        if args.screen_only:
            return 0

        state.settings.scorer_max_signatures = int(args.max_signatures)
        state.settings.min_leader_buy_sol = float(args.min_leader_buy)
        print("\nBacktest basliyor (%d cuzdan)...\n" % len(good))
        result = await backtest(good, split_days=args.split_days,
                                skip_selection_filter=True,
                                lookback_days=args.lookback_days)
        report(result)
    finally:
        await rpc.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
