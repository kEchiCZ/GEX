"""Kouč v2 (#1201): segmenty seance, profil denní doby, příznaky setupů, doporučení."""

import datetime as dt
from typing import Any

from gexlens_engine.compute.coach_setups import (
    SetupItem,
    session_segment,
    setup_flags,
    setup_from_row,
    setup_recommendations,
    setups_report,
    time_of_day_profile,
)

DAY = dt.date(2026, 9, 16)


def at(hour_et: int, minute: int = 0) -> dt.datetime:
    # Letní čas: ET = UTC−4
    return dt.datetime(DAY.year, DAY.month, DAY.day, hour_et + 4, minute, tzinfo=dt.UTC)


def item(**overrides: Any) -> SetupItem:
    base: dict[str, Any] = {
        "id": 1,
        "symbol": "ES",
        "template": "wall_bounce",
        "direction": "long",
        "created_ts": at(10, 0),
        "closed_ts": at(11, 0),
        "status": "closed_target",
        "outcome_r": 1.0,
        "entry": 7600.0,
        "target": 7616.0,
        "stop": 7592.0,
        "confidence": 50,
        "gex_regime": "positive",
        "band_class": "inside",
        "tradeable": True,
        "affordable": True,
        "trade_block": None,
        "mfe": 17.0,
        "mae": 3.0,
    }
    base.update(overrides)
    return SetupItem(**base)


def test_segmenty_seance_z_et_hranic() -> None:
    assert session_segment(at(3, 0)) == "globex"
    assert session_segment(at(8, 30)) == "premarket"
    assert session_segment(at(9, 30)) == "open30"
    assert session_segment(at(9, 59)) == "open30"
    assert session_segment(at(10, 0)) == "dopoledne"
    assert session_segment(at(12, 30)) == "poledne"
    assert session_segment(at(14, 30)) == "power"
    assert session_segment(at(15, 45)) == "close30"
    assert session_segment(at(16, 30)) == "after_close"


def test_profil_denni_doby_a_okna() -> None:
    # 25 obchodů v open30 Ø −0,4 R, 25 v dopoledne Ø +0,5 R, 5 v power (pod vzorkem)
    points = [(at(9, 35), -0.4)] * 25 + [(at(10, 30), 0.5)] * 25 + [(at(14, 40), 3.0)] * 5
    profile = time_of_day_profile(points)
    assert profile.best_segment == "dopoledne" and profile.worst_segment == "open30"
    # Hodiny lokálně (Praha = ET + 6): 9:35 ET → 15, 10:30 ET → 16
    assert profile.best_hour == 16 and profile.worst_hour == 15
    payload = profile.as_dict()
    assert payload["segments"]["power"]["n"] == 5 and payload["segments"]["power"]["avg_r"] == 3.0
    assert payload["hours"]["15"]["win_rate"] == 0.0 and payload["hours"]["16"]["win_lb"] > 0.8


def test_priznaky_setupu() -> None:
    clean = setup_flags(item())
    assert clean == []
    bad = item(
        direction="short",
        gex_regime="negative",
        band_class="outside",
        affordable=False,
        target=7608.0,
        status="closed_timeout",
    )
    kinds = [f.kind for f in setup_flags(bad)]
    # short v negativní gammě je kontra-režim (fade momenta) — viz is_counter_regime
    assert "outside_band" in kinds and "unaffordable" in kinds and "low_rr" in kinds
    assert "timeout" in kinds
    # bad_window jen s profilem a vzorkem ≥ 30 v prodělečném segmentu
    points = [(at(9, 35), -0.5)] * 30
    profile = time_of_day_profile(points)
    flagged = setup_flags(item(created_ts=at(9, 40)), profile)
    assert [f.kind for f in flagged] == ["bad_window"]


def test_doporuceni_s_vzorkem_a_report() -> None:
    items = (
        [item(id=i, created_ts=at(9, 35), outcome_r=-0.5) for i in range(30)]
        + [
            item(id=100 + i, created_ts=at(10, 30), outcome_r=0.6, template="failed_break")
            for i in range(30)
        ]
        + [item(id=200 + i, created_ts=at(12, 0), outcome_r=0.0) for i in range(5)]
    )
    recs = setup_recommendations(items)
    kinds = {(r.kind, r.scope, r.key) for r in recs}
    assert ("avoid", "template_segment", "wall_bounce|open30") in kinds
    assert ("focus", "template_segment", "failed_break|dopoledne") in kinds
    assert ("avoid", "segment", "open30") in kinds and ("focus", "segment", "dopoledne") in kinds
    assert recs[0].bucket.n == 30 and "n=30" in recs[0].text
    report = setups_report(items)
    assert report["n"] == 65 and report["by_template"]["wall_bounce"]["n"] == 35
    assert report["time"]["worst_segment"] == "open30" and report["flags"]["bad_window"]["n"] == 30
    assert len(report["setups"]) == 50 and report["recommendations"]


def test_setup_from_row() -> None:
    row = {
        "id": 7,
        "symbol": "NQ",
        "template": "max_pain_pin",
        "direction": "short",
        "created_ts": "2026-09-16T14:00:00+00:00",
        "closed_ts": None,
        "status": "closed_stop",
        "outcome_r": -1.0,
        "entry": 29000.0,
        "target": 28950.0,
        "stop": 29025.0,
        "confidence": 61,
        "context": {
            "gex_regime": "negative",
            "band_class": "no_zone",
            "tradeable": False,
            "trade_block": "gate",
        },
    }
    parsed = setup_from_row(row)
    assert parsed is not None and parsed.planned_rr == 2.0 and parsed.hour == 16
    assert parsed.tradeable is False and parsed.affordable is None and parsed.trade_block == "gate"
    assert setup_from_row({**row, "status": "active", "outcome_r": None}) is None
