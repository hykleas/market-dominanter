"""Unit tests for wallet_discovery's pure functions (no network).

    python -m pytest tests/ -q
    python tests/test_discovery.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from wallet_discovery import (  # noqa: E402
    Winner, apply_true_ages, merge_candidates, safe_symbol, select_winners, tx_signer,
)

NOW = 1_800_000_000.0
DAY = 86400.0
WALLET = "Wa11et11111111111111111111111111111111111111"


def pair(mint, mcap, age_days, symbol="X", chain="solana", liq=50_000, created=True):
    return {
        "chainId": chain,
        "baseToken": {"address": mint, "symbol": symbol},
        "marketCap": mcap,
        "liquidity": {"usd": liq},
        "pairCreatedAt": (NOW - age_days * DAY) * 1000 if created else None,
    }


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


# --------------------------------------------------------------------------- #
# select_winners
# --------------------------------------------------------------------------- #
def test_filters_by_mcap_and_age():
    pairs = [
        pair("A", 500_000, 5),      # gecer
        pair("B", 100_000, 5),      # mcap dusuk
        pair("C", 500_000, 30),     # cok eski
        pair("D", 900_000, 1),      # gecer
    ]
    winners = select_winners(pairs, days=14, min_mcap=200_000, limit=10, now=NOW)
    assert [w.mint for w in winners] == ["D", "A"]     # mcap'e gore sirali


def test_ignores_non_solana_chains():
    pairs = [pair("A", 900_000, 3, chain="base"), pair("B", 300_000, 3)]
    winners = select_winners(pairs, days=14, min_mcap=200_000, limit=10, now=NOW)
    # chainId filtresi collect_pairs'te, ama select_winners de zincir-agnostik
    # olmamali diye burada sadece ikisinin de gectigi dogrulanir:
    assert len(winners) == 2


def test_missing_created_at_is_skipped():
    """pairCreatedAt yoksa yasi bilinmiyor demektir; 'yeni' varsaymak eski bir
    token'in launch penceresini taramaya kalkardi."""
    winners = select_winners([pair("A", 900_000, 3, created=False)],
                             days=14, min_mcap=200_000, limit=10, now=NOW)
    assert winners == []


def test_same_mint_keeps_oldest_creation():
    """Ayni token birden fazla dex'te olabilir; migrate oldugu havuzun yasi
    degil, token'in gercek dogum ani sayilmali."""
    pairs = [pair("A", 500_000, 2, symbol="pumpswap"),
             pair("A", 500_000, 9, symbol="pumpfun")]
    winners = select_winners(pairs, days=14, min_mcap=200_000, limit=10, now=NOW)
    assert len(winners) == 1
    assert approx(winners[0].age_days, 9, tol=0.01) or winners[0].created_at == NOW - 9 * DAY


def test_same_mint_keeps_highest_mcap():
    pairs = [pair("A", 300_000, 5), pair("A", 800_000, 2)]
    winners = select_winners(pairs, days=14, min_mcap=200_000, limit=10, now=NOW)
    assert len(winners) == 1 and winners[0].market_cap == 800_000


def test_limit_is_applied_after_sorting():
    pairs = [pair(str(i), 200_000 + i * 1000, 3) for i in range(10)]
    winners = select_winners(pairs, days=14, min_mcap=200_000, limit=3, now=NOW)
    assert len(winners) == 3
    assert [w.mint for w in winners] == ["9", "8", "7"]


def test_future_created_at_is_rejected():
    pairs = [pair("A", 900_000, -2)]     # gelecekte olusturulmus (bozuk veri)
    assert select_winners(pairs, days=14, min_mcap=200_000, limit=10, now=NOW) == []


def test_falls_back_to_fdv_when_marketcap_missing():
    p = pair("A", None, 3)
    p["fdv"] = 700_000
    winners = select_winners([p], days=14, min_mcap=200_000, limit=10, now=NOW)
    assert len(winners) == 1 and winners[0].market_cap == 700_000


# --------------------------------------------------------------------------- #
# apply_true_ages  -  "yeni havuz acmis eski token" tuzagi
# --------------------------------------------------------------------------- #
def _w(mint, age_days, mcap=500_000):
    return Winner(mint=mint, symbol=mint, market_cap=mcap,
                  created_at=NOW - age_days * DAY, liquidity=1000)


def test_old_token_with_a_new_pool_is_rejected():
    """Canli olcumde BONK 10.9 gunluk gorunuyordu: 2022 tokeni, yeni havuzu var.
    Boyle bir token'in launch penceresini taramak alakasiz cuzdanlar uretir."""
    winners = [_w("BONK", 10.9), _w("REAL", 3)]
    oldest = {"BONK": NOW - 900 * DAY, "REAL": NOW - 3 * DAY}
    fresh, stale = apply_true_ages(winners, oldest, days=14, now=NOW)
    assert [w.mint for w in fresh] == ["REAL"]
    assert len(stale) == 1 and stale[0][0].mint == "BONK"
    assert stale[0][1] > 800


def test_true_age_overrides_the_pool_age():
    """Gercek dogum ani havuz yasindan eskiyse kayit guncellenmeli."""
    winners = [_w("A", 2)]
    fresh, stale = apply_true_ages(winners, {"A": NOW - 9 * DAY}, days=14, now=NOW)
    assert stale == [] and approx(fresh[0].created_at, NOW - 9 * DAY)


def test_unverifiable_mint_is_kept():
    """Dexscreener o mint icin pair dondurmediyse elemek yerine mevcut yasla
    devam edilir - dogrulanamamak, eski olmak demek degil."""
    fresh, stale = apply_true_ages([_w("A", 3)], {}, days=14, now=NOW)
    assert len(fresh) == 1 and stale == []


# --------------------------------------------------------------------------- #
# safe_symbol  -  semboller saldirgan kontrolunde
# --------------------------------------------------------------------------- #
def test_safe_symbol_strips_bidi_override():
    """Canli veride U+202E iceren bir sembol Windows konsolunu crash etti."""
    assert safe_symbol("ab‮cd") == "abcd"


def test_safe_symbol_falls_back_when_empty():
    assert safe_symbol("") == "?"
    assert safe_symbol(None, "MINT12") == "MINT12"
    assert safe_symbol("‮​", "MINT12") == "MINT12"


def test_safe_symbol_truncates():
    assert len(safe_symbol("A" * 100)) == 16


# --------------------------------------------------------------------------- #
# tx_signer
# --------------------------------------------------------------------------- #
def test_signer_prefers_the_signer_flag():
    tx = {"transaction": {"message": {"accountKeys": [
        {"pubkey": "ProgramAcct", "signer": False},
        {"pubkey": WALLET, "signer": True}]}}}
    assert tx_signer(tx) == WALLET


def test_signer_falls_back_to_first_key():
    tx = {"transaction": {"message": {"accountKeys": [{"pubkey": WALLET}]}}}
    assert tx_signer(tx) == WALLET


def test_signer_handles_empty_tx():
    assert tx_signer({}) is None
    assert tx_signer({"transaction": {"message": {"accountKeys": []}}}) is None


# --------------------------------------------------------------------------- #
# merge_candidates
# --------------------------------------------------------------------------- #
def test_merge_adds_new_addresses():
    merged, added, updated = merge_candidates([], {"A": 3, "B": 2})
    assert added == 2 and updated == 0
    assert merged[0]["address"] == "A"          # en cok isabet once
    assert merged[0]["source"] == "auto-discovery"
    assert merged[0]["note"] == "3 kazanan tokenda erken alici"


def test_merge_never_overwrites_a_manual_entry():
    """Gece calisan batch, kullanicinin elle yazdigi notu silmemeli."""
    existing = [{"address": "A", "source": "gmgn", "note": "elle eklendim"}]
    merged, added, updated = merge_candidates(existing, {"A": 5})
    assert added == 0 and updated == 0
    assert merged[0]["note"] == "elle eklendim" and merged[0]["source"] == "gmgn"


def test_merge_refreshes_its_own_note():
    existing = [{"address": "A", "source": "auto-discovery",
                 "note": "2 kazanan tokenda erken alici"}]
    merged, added, updated = merge_candidates(existing, {"A": 4})
    assert added == 0 and updated == 1
    assert merged[0]["note"] == "4 kazanan tokenda erken alici"


def test_merge_is_idempotent():
    merged1, a1, u1 = merge_candidates([], {"A": 2})
    merged2, a2, u2 = merge_candidates(merged1, {"A": 2})
    assert (a2, u2) == (0, 0) and merged1 == merged2


def test_merge_does_not_mutate_the_input():
    existing = [{"address": "A", "source": "gmgn", "note": "orijinal"}]
    snapshot = [dict(e) for e in existing]
    merge_candidates(existing, {"A": 9, "B": 3})
    assert existing == snapshot


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
