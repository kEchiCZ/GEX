"""Testy sebekontroly setup detektoru (#309): agregace, Wilson, verdikt."""

import pytest

from gexlens_engine.compute.setupstats import (
    ClosedSetup,
    SetupParamsStats,
    aggregate,
    degraded,
    format_report,
    wilson_lower_bound,
)

PARAMS = SetupParamsStats()


def closed(template: str, direction: str, r: float) -> ClosedSetup:
    return ClosedSetup(
        template=template,
        direction=direction,
        status="closed_target" if r > 0 else "closed_stop",
        outcome_r=r,
    )


# ── Wilson ─────────────────────────────────────────────────────────


def test_wilson_lower_bound_punishes_small_samples() -> None:
    # 55 % z 20 pokusů je nerozlišitelné od mince → dolní mez pod 0.5
    assert wilson_lower_bound(11, 20) < 0.5
    # Stejný podíl při velkém n už drží nad polovinou
    assert wilson_lower_bound(550, 1000) > 0.5
    # Krajní případy
    assert wilson_lower_bound(0, 0) == 0.0
    assert wilson_lower_bound(0, 10) == 0.0
    assert wilson_lower_bound(10, 10) == pytest.approx(0.722, abs=0.01)


# ── Agregace ───────────────────────────────────────────────────────


def test_aggregate_splits_by_template_and_direction() -> None:
    rows = [
        closed("wall_bounce", "long", 2.0),
        closed("wall_bounce", "short", -1.0),
        closed("failed_break", "short", -1.0),
        closed("failed_break", "short", -1.0),
    ]
    report = aggregate(rows)

    assert report.overall.n == 4
    assert report.overall.wins == 1
    assert report.overall.sum_r == pytest.approx(-1.0)
    assert report.overall.avg_r == pytest.approx(-0.25)
    assert report.overall.hit_rate == pytest.approx(0.25)

    # Nejhorší šablona i konkrétní směr — to je to, co má alert pojmenovat
    assert report.worst_template is not None
    assert report.worst_template.label == "failed_break"
    assert report.worst_template.sum_r == pytest.approx(-2.0)
    assert report.worst_direction is not None
    assert report.worst_direction.label == "failed_break short"


def test_aggregate_empty_is_neutral() -> None:
    report = aggregate([])
    assert report.overall.n == 0
    assert report.overall.avg_r == 0.0
    assert report.overall.hit_rate == 0.0
    assert report.worst_template is None
    assert not degraded(report, PARAMS)


# ── Verdikt ────────────────────────────────────────────────────────


def test_degraded_needs_both_drawdown_and_sample_size() -> None:
    # Reálný vzor 20.–27. 7.: 166 uzavřených, ΣR −43,5
    bleeding = [closed("failed_break", "short", -1.0) for _ in range(40)]
    assert degraded(aggregate(bleeding), PARAMS)

    # Stejně hluboký propad, ale z pár obchodů → verdikt nepadne
    few = [closed("failed_break", "short", -6.0) for _ in range(3)]
    assert aggregate(few).overall.sum_r <= PARAMS.max_drawdown_r
    assert not degraded(aggregate(few), PARAMS)

    # Dost vzorků, ale mělký propad → v pořádku
    shallow = [closed("wall_bounce", "long", -0.1) for _ in range(20)]
    assert not degraded(aggregate(shallow), PARAMS)

    # Ziskový detektor nikdy
    winning = [closed("wall_bounce", "long", 1.5) for _ in range(20)]
    assert not degraded(aggregate(winning), PARAMS)


def test_format_report_names_worst_direction() -> None:
    rows = [closed("failed_break", "short", -1.0) for _ in range(12)]
    rows.append(closed("wall_bounce", "long", 2.0))
    text = format_report(aggregate(rows), 7)
    assert "13 uzavřených" in text
    assert "Wilson LB" in text
    assert "failed_break short" in text
