"""Kural motorunun esik davranisi (ag erisimi yok).

    python -m pytest tests/ -q
    python tests/test_rules.py        # pytest kurulu degilse
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import state  # noqa: E402
from analyzer import Metrics, evaluate  # noqa: E402

MINT = "So1aNaMint1111111111111111111111111111111111"


def clean_metrics(**over):
    """Butun kurallari gecen bir coin; testler tek alani bozar."""
    base = dict(
        mint=MINT, price_usd=1e-5, market_cap=30_000.0, volume_5m=500.0,
        supply=1e9, total_supply=1e9, curve_sol=10.0, curve_complete=False,
        lp_burned=True, bundler=1.0, sniper=1.0, dev=1.0, top10=5.0,
    )
    base.update(over)
    return Metrics(**base)


def cfg(**over):
    return state.Settings(**over)


def test_baseline_passes():
    assert evaluate(clean_metrics(), cfg()).passed


def test_zero_threshold_disables_volume_rule():
    """min_volume_5m = 0 "kurali kapat" demek.

    Karsilastirma <= iken esigi 0'a cekmek kurali kapatmiyordu: hacmi 0 olan
    her coin eleniyordu (21 Agustos gecesi 4661 kararin 4406'sinda).
    """
    res = evaluate(clean_metrics(volume_5m=0.0), cfg(min_volume_5m=0.0))
    assert "volume" not in res.codes
    assert res.passed

    # curve verisi yokken de ayni: esik kapaliysa hacim esigi uygulanmaz,
    # ama verinin varligi hala aranir.
    res = evaluate(clean_metrics(volume_5m=0.0, curve_sol=None), cfg(min_volume_5m=0.0))
    assert "volume" not in res.codes


def test_positive_threshold_still_filters():
    res = evaluate(clean_metrics(volume_5m=100.0), cfg(min_volume_5m=250.0))
    assert "volume" in res.codes

    # esigin tam ustunde/esitinde olan gecer
    assert "volume" not in evaluate(clean_metrics(volume_5m=250.0), cfg(min_volume_5m=250.0)).codes


def test_missing_volume_still_fails_when_curve_absent():
    """Eksik veri = FAIL kurali hacim esigi kapaliyken de gecerli."""
    res = evaluate(clean_metrics(volume_5m=None, curve_sol=None), cfg(min_volume_5m=0.0))
    assert "no_volume" in res.codes
    assert not res.passed


def test_analyze_delay_default_is_measured_value():
    """40sn'de coinlerin %95'i launch tabaninda -> 0 alim (bkz. GECE-2026-08-21)."""
    assert state.Settings().analyze_delay == 90.0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("TUM TESTLER GECTI")
