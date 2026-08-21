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


def test_missing_curve_is_a_failure():
    """curve verisi yoksa talep kontrolu yapilamaz -> FAIL.

    Eskiden sessizce atlaniyordu: 429 yiyen coin curve_low/curve_high'i hic
    gormeden geciyordu ("The Boo Dog", 21 Agustos, alindi -> 95sn'de -%67).
    """
    res = evaluate(clean_metrics(curve_sol=None), cfg())
    assert "no_curve" in res.codes
    assert not res.passed

    # ... ama curve dolup DEX'e gecmisse esikler zaten anlamsiz, hacme bakilir
    res = evaluate(clean_metrics(curve_sol=99.0, curve_complete=True), cfg())
    assert "curve_high" not in res.codes
    assert res.passed


def test_migrated_coin_still_needs_volume_data():
    """Eksik veri = FAIL kurali hacim esigi kapaliyken de gecerli."""
    res = evaluate(clean_metrics(volume_5m=None, curve_sol=50.0, curve_complete=True),
                   cfg(min_volume_5m=0.0))
    assert "no_volume" in res.codes
    assert not res.passed


def test_unknown_strategy_mode_is_rejected(tmp_path, monkeypatch):
    """Bilinmeyen mod = hicbir motor alim yapmaz, sessizce.

    21 Agustos gecesi mod "sniper" yazilmisti: legacy_sniper "arsivde" deyip
    alimi atliyor, copy_engine de bosta duruyordu.
    """
    monkeypatch.setattr(state, "SETTINGS_FILE", tmp_path / "settings.json")
    s = state.Settings()
    s.update({"strategy_mode": "sniper"})
    assert s.strategy_mode == "copy"          # eski deger korunur
    s.update({"strategy_mode": "LEGACY "})    # bosluk/buyuk harf tolere edilir
    assert s.strategy_mode == "legacy"
    assert set(state.STRATEGY_MODES) == {"copy", "legacy"}


def test_analyze_delay_default_is_measured_value():
    """40sn'de coinlerin %95'i launch tabaninda -> 0 alim (bkz. GECE-2026-08-21)."""
    assert state.Settings().analyze_delay == 90.0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("TUM TESTLER GECTI")
