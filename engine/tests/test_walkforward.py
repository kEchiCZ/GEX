"""Walk-forward (#794 fáze 3, ADR-0034): golden folds, zábrany návrhu, Sharpe, report."""

import datetime as dt
import json
from pathlib import Path
from typing import cast

import pytest

from gexlens_engine.compute.walkforward import (
    WalkForwardParams,
    annualized_sharpe,
    paired_test,
    render_markdown,
    walk_forward,
)


def _golden() -> dict[str, object]:
    path = Path(__file__).parent / "golden" / "walkforward_794.json"
    return cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))


def _series(raw: dict[str, dict[str, float]]) -> dict[str, dict[dt.date, float]]:
    return {
        name: {dt.date.fromisoformat(day): float(value) for day, value in values.items()}
        for name, values in raw.items()
    }


def test_walk_forward_proti_golden() -> None:
    golden = _golden()
    params = WalkForwardParams(**cast(dict[str, object], golden["params"]))  # type: ignore[arg-type]
    result = walk_forward(
        _series(cast(dict[str, dict[str, float]], golden["series"])),
        baseline=cast(str, golden["baseline"]),
        params=params,
    )
    expected = cast(dict[str, object], golden["expected"])
    folds = cast(list[dict[str, object]], expected["folds"])
    assert len(result.folds) == len(folds)
    for fold, want in zip(result.folds, folds, strict=True):
        in_range = cast(list[str], want["in"])
        out_range = cast(list[str], want["out"])
        assert (fold.in_start.isoformat(), fold.in_end.isoformat()) == tuple(in_range)
        assert (fold.out_start.isoformat(), fold.out_end.isoformat()) == tuple(out_range)
        assert fold.chosen == want["chosen"]
        assert fold.in_metric == pytest.approx(cast(float, want["in_metric"]))
        assert fold.out_sum_r == pytest.approx(cast(float, want["out_sum_r"]))
        assert fold.baseline_out_sum_r == pytest.approx(cast(float, want["baseline_out_sum_r"]))
    assert result.oos_days == expected["oos_days"]
    assert sum(result.oos.values()) == pytest.approx(cast(float, expected["oos_sum_r"]))
    assert sum(result.oos_baseline.values()) == pytest.approx(
        cast(float, expected["oos_baseline_sum_r"])
    )
    assert result.mean_diff_per_day == pytest.approx(cast(float, expected["mean_diff_per_day"]))
    assert result.t_stat == pytest.approx(cast(float, expected["t_stat"]))
    assert result.p_value == pytest.approx(cast(float, expected["p_value"]))
    assert result.alpha_bonferroni == pytest.approx(cast(float, expected["alpha_bonferroni"]))
    assert result.chosen_share == expected["chosen_share"]
    assert result.proposal is None
    assert "nic se nenavrhuje" in result.verdict


def test_sharpe_proti_golden() -> None:
    check = cast(dict[str, object], _golden()["sharpe_check"])
    values = cast(list[float], check["values"])
    assert annualized_sharpe(values) == pytest.approx(cast(float, check["expected"]), abs=1e-3)
    assert annualized_sharpe([1.0]) is None  # jeden den
    assert annualized_sharpe([2.0, 2.0, 2.0]) is None  # nulová σ


def test_paired_test_hranicni_pripady() -> None:
    assert paired_test([0.5]) == (None, None)
    assert paired_test([0.0, 0.0, 0.0]) == (0.0, 1.0)
    t_stat, p_value = paired_test([0.5, 0.5, 0.5])
    assert t_stat == float("inf") and p_value == 0.0
    t_stat, p_value = paired_test([1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0])
    assert t_stat is not None and p_value is not None
    assert t_stat > 0 and 0.0 < p_value < 1.0


def _days(count: int, start: dt.date = dt.date(2026, 6, 1)) -> list[dt.date]:
    days: list[dt.date] = []
    day = start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += dt.timedelta(days=1)
    return days


def test_navrh_vznikne_jen_pri_splneni_vsech_zabran() -> None:
    """Kandidát stabilně lepší o 0,5 R/den → návrh; totéž s malým vzorkem, malým
    zlepšením nebo nestabilní volbou → žádný návrh (ADR-0034)."""
    days = _days(60)
    base = {day: 1.0 + (0.3 if index % 2 else -0.3) for index, day in enumerate(days)}
    better = {day: value + 0.5 for day, value in base.items()}
    worse = {day: value - 0.5 for day, value in base.items()}
    params = WalkForwardParams(in_sample_days=10, out_sample_days=5, metric="sharpe")

    result = walk_forward(
        {"base": base, "lepší": better, "horší": worse}, baseline="base", params=params
    )
    assert result.proposal == "lepší"
    assert result.chosen_share["lepší"] == 1.0
    assert result.mean_diff_per_day == pytest.approx(0.5)
    assert result.verdict.startswith("NÁVRH: lepší")
    # Bonferroni na 2 alternativy
    assert result.alpha_bonferroni == pytest.approx(0.025)

    # Málo OOS dnů → nic (60 dnů − 10 IS = 50 OOS, práh 100)
    short = walk_forward(
        {"base": base, "lepší": better},
        baseline="base",
        params=WalkForwardParams(in_sample_days=10, out_sample_days=5, min_oos_days=100),
    )
    assert short.proposal is None and "OOS seancí" in short.verdict

    # Zlepšení pod prahem → nic
    tiny = {day: value + 0.05 for day, value in base.items()}
    small = walk_forward({"base": base, "málo": tiny}, baseline="base", params=params)
    assert small.proposal is None and "pod prahem" in small.verdict

    # Shoda metriky → zůstává baseline (churn je náklad)
    same = walk_forward({"base": base, "kopie": dict(base)}, baseline="base", params=params)
    assert all(fold.chosen == "base" for fold in same.folds)
    assert same.proposal is None


def test_walk_forward_validace_a_report() -> None:
    days = _days(8)
    base = {day: 1.0 for day in days}
    with pytest.raises(ValueError, match="Baseline"):
        walk_forward({"base": base}, baseline="jiná")
    with pytest.raises(ValueError, match="in_sample_days"):
        walk_forward({"base": base}, baseline="base", params=WalkForwardParams(in_sample_days=1))
    with pytest.raises(ValueError, match="Neznámá metrika"):
        walk_forward(
            {"base": base},
            baseline="base",
            params=WalkForwardParams(in_sample_days=4, out_sample_days=2, metric="median"),
        )
    # Dva dny = žádný fold → poctivý verdikt
    tiny = walk_forward(
        {"base": {days[0]: 1.0, days[1]: 1.0}},
        baseline="base",
        params=WalkForwardParams(in_sample_days=4, out_sample_days=2),
    )
    assert tiny.folds == () and "jediný fold" in tiny.verdict

    result = walk_forward(
        {"base": base, "A": {day: 2.0 for day in days}},
        baseline="base",
        params=WalkForwardParams(in_sample_days=4, out_sample_days=2, metric="sum_r"),
    )
    report = render_markdown({"ES": result}, title="Walk-forward test")
    assert report.startswith("# Walk-forward test")
    assert "| ES | base |" in report
    assert "## ES — folds" in report
    assert "Podíl foldů:" in report
