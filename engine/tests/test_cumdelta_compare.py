"""Testy srovnání CumΔ dxFeed (stín) vs. midpoint (živá) nad syntetickými particemi (#1018)."""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from gexlens_engine.compute.settle import session_bounds
from gexlens_engine.storage.cumdelta_compare import (
    KNOWN_UNUSABLE,
    MIN_COMPLETE_MINUTES,
    available_sessions,
    best_lag,
    chain_breaks,
    compare_series,
    compare_session,
    pearson,
    summarize,
)
from gexlens_engine.storage.parquet_store import DX_FLOW_SCHEMA, FLOW_SCHEMA

SESSION = dt.date(2026, 9, 10)  # čtvrtek, seance 9. 9. 22:00 UTC → 10. 9. 22:00 UTC
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "compare_cumdelta_sources.py"


def _minutes(count: int) -> list[dt.datetime]:
    start, _ = session_bounds(SESSION)
    return [start + dt.timedelta(minutes=i) for i in range(count)]


def _flows(count: int) -> list[float]:
    # Deterministický, nekonstantní tok se střídavým znaménkem, ať mají řady varianci
    return [((i * 37) % 11 - 5) * 10.0 for i in range(count)]


def _cumulative(flows: list[float]) -> list[float]:
    total = 0.0
    out: list[float] = []
    for f in flows:
        total += f
        out.append(total)
    return out


def write_dx(
    derived: Path,
    symbol: str,
    ts: list[dt.datetime],
    flows: list[float],
    *,
    hot_flows: list[float] | None = None,
) -> None:
    hot = hot_flows or [0.0] * len(flows)
    rows = [
        {
            "ts_min": t,
            "flow_ring": f,
            "cum_ring": c,
            "flow_hot": h,
            "cum_hot": hc,
            "trades": 3,
            "unknown_side": 1 if i % 100 == 0 else 0,
            "volume": 30.0,
            "dropped_no_context": 0,
        }
        for i, (t, f, c, h, hc) in enumerate(
            zip(ts, flows, _cumulative(flows), hot, _cumulative(hot), strict=True)
        )
    ]
    path = derived / symbol / "cumdelta_dx" / f"{SESSION.isoformat()}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=DX_FLOW_SCHEMA), path)


def write_live(derived: Path, symbol: str, ts: list[dt.datetime], flows: list[float]) -> None:
    """Živá řada po UTC dnech — seance leží ve dvou particích (D−1 večer + D)."""
    by_day: dict[dt.date, list[dict[str, object]]] = {}
    for t, f, c in zip(ts, flows, _cumulative(flows), strict=True):
        by_day.setdefault(t.date(), []).append(
            {
                "ts_min": t,
                "flow_delta": f,
                "cum_delta": c,
                "futures_cvd_delta": None,
                "futures_cvd": None,
                "source": "midpoint",
            }
        )
    for day, rows in by_day.items():
        path = derived / symbol / "flow" / f"{day.isoformat()}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.Table.from_pylist(rows, schema=FLOW_SCHEMA), path)


def test_identical_series_match_perfectly(tmp_path: Path) -> None:
    ts = _minutes(1200)
    flows = _flows(1200)
    write_dx(tmp_path, "ES", ts, flows)
    write_live(tmp_path, "ES", ts, flows)

    result = compare_session(tmp_path, "ES", SESSION)

    assert result.minutes_common == 1200
    assert result.minutes_dx_only == 0 and result.minutes_live_only == 0
    assert result.total.max_abs_dev == 0.0
    assert result.max_abs_dev_pct == 0.0
    assert result.total.corr_levels == 1.0
    assert result.total.corr_increments == 1.0
    assert result.total.sign_disagree_share == 0.0
    assert result.sign_agree_close is True
    assert result.unusable_reason is None
    assert result.zoned is False
    assert result.rth.minutes + result.off_rth.minutes == 1200
    assert result.rth.minutes > 0 and result.off_rth.minutes > 0
    assert result.dx_trades == 3 * 1200
    assert result.dx_unknown_side == 12
    assert result.total.range_ratio == 1.0
    assert result.total.max_abs_dev_norm == 0.0
    assert result.dx_chain_breaks == 0 and result.live_chain_breaks == 0


def test_scaled_series_keep_shape_metrics(tmp_path: Path) -> None:
    """Tisková řada má menší měřítko než objemová — korelace a tvar to nesmí trestat."""
    ts = _minutes(1200)
    flows = _flows(1200)
    write_dx(tmp_path, "ES", ts, [f * 0.1 for f in flows])
    write_live(tmp_path, "ES", ts, flows)

    result = compare_session(tmp_path, "ES", SESSION)

    assert result.total.range_ratio is not None and abs(result.total.range_ratio - 0.1) < 1e-9
    assert result.total.corr_levels is not None and abs(result.total.corr_levels - 1.0) < 1e-12
    assert result.total.max_abs_dev_norm is not None and result.total.max_abs_dev_norm < 1e-6
    assert result.total.max_abs_dev is not None and result.total.max_abs_dev > 0.0


def test_shadow_restart_is_detected_and_rechained() -> None:
    """Restart stínu bez navázání: kumulativ skočí na nulu, přírůstky pokračují."""
    ts = _minutes(1200)
    flows = _flows(1200)
    live_cum = _cumulative(flows)
    live = {t: (c, f) for t, c, f in zip(ts, live_cum, flows, strict=True)}
    dx: dict[dt.datetime, tuple[float, float]] = {}
    total = 0.0
    for i, (t, f) in enumerate(zip(ts, flows, strict=True)):
        if i == 600:
            total = 0.0  # restart enginu — stín začíná od nuly
        total += f
        dx[t] = (total, f)

    assert chain_breaks(dx) == 1
    assert chain_breaks(live) == 0

    stored = compare_series("ES", SESSION, dx, live)
    assert stored.dx_chain_breaks == 1
    assert "přerušený řetěz dx 1× / live 0×" in stored.notes
    assert stored.total.max_abs_dev is not None and stored.total.max_abs_dev > 0.0
    assert stored.total.corr_increments == 1.0  # přírůstky restart nepoznají

    rechained = compare_series("ES", SESSION, dx, live, rechain_series=True)
    assert rechained.rechained is True
    assert rechained.dx_chain_breaks == 1  # měří se nad uloženými řadami
    assert rechained.total.max_abs_dev == 0.0
    assert rechained.total.corr_levels == 1.0
    assert rechained.sign_agree_close is True


def test_lag_scan_finds_minute_offset() -> None:
    """Živá řada o minutu zpožděná za stínem → nejlepší posun k=+1, při k=0 slabší."""
    ts = _minutes(1200)
    flows = _flows(1200)
    dx = {t: (c, f) for t, c, f in zip(ts, _cumulative(flows), flows, strict=True)}
    delayed = [0.0, *flows[:-1]]
    live = {t: (c, f) for t, c, f in zip(ts, _cumulative(delayed), delayed, strict=True)}

    result = compare_series("ES", SESSION, dx, live)

    by_lag = dict(result.corr_increments_by_lag)
    assert result.best_lag == 1
    assert by_lag[1] is not None and abs(by_lag[1] - 1.0) < 1e-12
    assert by_lag[0] is not None and by_lag[0] < 0.9
    assert result.total.corr_increments == by_lag[0]
    assert best_lag(()) is None
    assert best_lag(((0, None), (1, None))) is None
    assert best_lag(((-1, 0.5), (0, 0.5), (1, 0.5))) == 0  # při shodě vyhrává menší |k|


def test_flipped_sign_disagrees_on_close(tmp_path: Path) -> None:
    ts = _minutes(1200)
    flows = _flows(1200)
    write_dx(tmp_path, "ES", ts, [-f for f in flows])
    write_live(tmp_path, "ES", ts, flows)

    result = compare_session(tmp_path, "ES", SESSION)

    assert result.sign_agree_close is False
    assert result.total.corr_levels == -1.0
    assert result.total.corr_increments == -1.0
    assert result.total.sign_disagree_share == 1.0
    assert result.close_dx is not None and result.close_live is not None
    assert result.close_dx == -result.close_live
    # max |Δ| = 2·max|live| a rozsah živé řady = max − min → % > 100 je možné
    assert result.total.max_abs_dev == 2 * max(abs(c) for c in _cumulative(flows))


def test_zoned_partition_sums_ring_and_hot(tmp_path: Path) -> None:
    ts = _minutes(1200)
    flows = _flows(1200)
    ring = [f * 0.6 for f in flows]
    hot = [f * 0.4 for f in flows]
    write_dx(tmp_path, "NQ", ts, ring, hot_flows=hot)
    write_live(tmp_path, "NQ", ts, flows)

    result = compare_session(tmp_path, "NQ", SESSION)

    assert result.zoned is True
    assert "zóny ATM±15 (před #1013)" in result.notes
    assert result.total.max_abs_dev is not None and result.total.max_abs_dev < 1e-6
    assert result.total.corr_levels is not None and abs(result.total.corr_levels - 1.0) < 1e-12


def test_short_session_is_flagged_unusable(tmp_path: Path) -> None:
    ts = _minutes(MIN_COMPLETE_MINUTES - 1)
    flows = _flows(len(ts))
    write_dx(tmp_path, "ES", ts, flows)
    write_live(tmp_path, "ES", ts, flows)

    result = compare_session(tmp_path, "ES", SESSION)

    assert result.unusable_reason is not None
    assert "neúplná" in result.unusable_reason
    summary = summarize([r for r in [result] if r.unusable_reason is None], "ES")
    assert summary.sessions == 0 and summary.median_corr_levels is None


def test_known_unusable_sessions_are_named() -> None:
    assert dt.date(2026, 9, 4) in KNOWN_UNUSABLE
    assert dt.date(2026, 9, 7) in KNOWN_UNUSABLE
    assert dt.date(2026, 9, 8) in KNOWN_UNUSABLE


def test_pearson_edge_cases() -> None:
    assert pearson([], []) is None
    assert pearson([1.0], [1.0]) is None
    assert pearson([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) is None  # nulová variance
    assert pearson([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]) == 1.0


def test_available_sessions_reads_partition_names(tmp_path: Path) -> None:
    ts = _minutes(10)
    write_dx(tmp_path, "ES", ts, _flows(10))
    (tmp_path / "ES" / "cumdelta_dx" / "smetí.parquet").touch()
    assert available_sessions(tmp_path, "ES") == [SESSION]
    assert available_sessions(tmp_path, "NQ") == []


def test_script_prints_markdown_and_json(tmp_path: Path) -> None:
    ts = _minutes(1200)
    flows = _flows(1200)
    write_dx(tmp_path, "ES", ts, flows)
    write_live(tmp_path, "ES", ts, flows)

    common = [sys.executable, str(SCRIPT), "--derived", str(tmp_path), "--symbols", "ES"]
    md = subprocess.run([*common], capture_output=True, text=True, encoding="utf-8", check=False)
    assert md.returncode == 0, md.stderr
    assert "## Per seance" in md.stdout
    assert (
        f"| {SESSION.isoformat()} | ES | 1200 (0 / 0) | 0 | 0.0 % | 0.0 % | 1.000 | 1.000 | 1.000 |"
        in md.stdout
    )
    assert "| ES | 1 | 1/1 |" in md.stdout
    assert "| 1.000 (k=+0) |" in md.stdout

    js = subprocess.run(
        [*common, "--json"], capture_output=True, text=True, encoding="utf-8", check=False
    )
    assert js.returncode == 0, js.stderr
    assert '"sign_agree_close": true' in js.stdout
    assert f'"session": "{SESSION.isoformat()}"' in js.stdout


def test_live_coverage_is_summed_from_flow_partition(tmp_path: Path) -> None:
    """#1071: pokrytí z živé partice — podíl tisků, fallback v RTH, zahozené tisky;
    partice bez sloupců (před #1071) dává None místo nul."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    import pytest

    from gexlens_engine.storage.cumdelta_compare import load_live_series

    derived = tmp_path / "derived"
    session = dt.date(2026, 9, 10)
    # 14:00 UTC = RTH (léto), 02:00 UTC = mimo RTH
    rth = dt.datetime(2026, 9, 10, 14, 0, tzinfo=dt.UTC)
    night = dt.datetime(2026, 9, 10, 2, 0, tzinfo=dt.UTC)
    rows = [
        {
            "ts_min": rth,
            "flow_delta": 1.0,
            "cum_delta": 1.0,
            "printed_volume": 60.0,
            "unknown_volume": 10.0,
            "structured_volume": 5.0,
            "fallback_volume": 30.0,
            "dropped_no_delta": 2,
        },
        {
            "ts_min": night,
            "flow_delta": 1.0,
            "cum_delta": 2.0,
            "printed_volume": 20.0,
            "unknown_volume": 0.0,
            "structured_volume": 0.0,
            "fallback_volume": 80.0,
            "dropped_no_delta": 1,
        },
        {"ts_min": rth + dt.timedelta(minutes=1), "flow_delta": 0.0, "cum_delta": 2.0},
    ]
    path = derived / "ES" / "flow" / f"{session.isoformat()}.parquet"
    path.parent.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=FLOW_SCHEMA), path)

    series, coverage = load_live_series(derived, "ES", session)
    assert len(series) == 3
    assert coverage is not None
    assert coverage.printed_share == pytest.approx(80 / 200)  # (60+20) / (80+10+110)
    assert coverage.fallback_share_rth == pytest.approx(30 / 100)  # jen minuta 14:00
    assert coverage.dropped_no_delta == 3

    # Partice před #1071: sloupce chybí → None
    old = derived / "NQ" / "flow" / f"{session.isoformat()}.parquet"
    old.parent.mkdir(parents=True)
    legacy_schema = pa.schema(
        [
            ("ts_min", pa.timestamp("us", tz="UTC")),
            ("flow_delta", pa.float64()),
            ("cum_delta", pa.float64()),
        ]
    )
    pq.write_table(
        pa.Table.from_pylist(
            [{"ts_min": rth, "flow_delta": 1.0, "cum_delta": 1.0}], schema=legacy_schema
        ),
        old,
    )
    _, none_coverage = load_live_series(derived, "NQ", session)
    assert none_coverage is None
