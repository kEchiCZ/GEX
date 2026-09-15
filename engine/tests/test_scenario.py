"""Scénář dne (#1173): cíle z geometrie, vyhodnocení, úložiště a kolektor po settle."""

import datetime as dt
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import create_engine

from gexlens_engine.compute.scenario import (
    Bar,
    PathPoint,
    evaluate_scenario,
    targets_from_path,
)
from gexlens_engine.scenarios import ScenarioCollector, load_bars_between
from gexlens_engine.storage.emrespect_store import EmRespectRepository, em_respect_table
from gexlens_engine.storage.scenarios_store import ScenariosRepository

T0 = dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.UTC)


def _ts(minutes: int) -> dt.datetime:
    return T0 + dt.timedelta(minutes=minutes)


def test_targets_z_geometrie_obraty_a_konec() -> None:
    # Cesta: 7600 → 7680 (obrat) → 7600 (konec); chvění ±1 b se slučuje
    path = [
        PathPoint(_ts(0), 7600),
        PathPoint(_ts(30), 7640),
        PathPoint(_ts(60), 7680),
        PathPoint(_ts(61), 7679.5),
        PathPoint(_ts(90), 7640),
        PathPoint(_ts(120), 7600),
    ]
    assert targets_from_path(path, entry=7600) == [7680.0, 7600.0]
    # Šipka = jen koncový bod
    assert targets_from_path([PathPoint(_ts(0), 7600), PathPoint(_ts(60), 7650)], 7600) == [7650.0]
    assert targets_from_path([], 7600) == []


def _bars(closes: list[tuple[int, float, float, float]]) -> list[Bar]:
    return [Bar(ts=_ts(m), high=h, low=lo, close=c) for m, h, lo, c in closes]


def test_vyhodnoceni_hit_partial_miss_a_odchylka() -> None:
    path = [PathPoint(_ts(0), 7600), PathPoint(_ts(60), 7680), PathPoint(_ts(120), 7600)]
    # Cíl 1 (7680) v 50. minutě, cíl 2 (7600) v 110. — pořadí drží
    bars = _bars([(10, 7620, 7595, 7610), (50, 7682, 7660, 7675), (110, 7640, 7598, 7605)])
    result = evaluate_scenario(bars, entry=7600, targets=[7680, 7600], path=path, em_points=40.0)
    assert result is not None
    assert result.hit1 and result.hit2 and result.order_ok is True
    assert result.verdict == "hit" and result.hit1_ts == _ts(50)
    # Odchylka close od cesty: min 10 → cesta 7613,3 vs 7610 → 3,3; min 50 → 7666,7 vs 7675 → 8,3;
    # min 110 → 7613,3 vs 7605 → 8,3 → max ≈ 8,33 b = 0,208 EM
    assert result.max_dev_pts == pytest.approx(8.33, abs=0.01)
    assert result.max_dev_em == pytest.approx(0.208, abs=0.001)

    # Cíl 2 (návrat k 7600) padl jen PŘED cílem 1, po něm už ne → partial, pořadí neplatí
    swapped = _bars([(10, 7605, 7590, 7598), (50, 7682, 7660, 7675)])
    partial = evaluate_scenario(
        swapped, entry=7600, targets=[7680, 7600], path=path, em_points=None
    )
    assert partial is not None and partial.verdict == "partial" and partial.order_ok is False
    assert partial.hit2 is False and partial.max_dev_em is None

    # Cíl 1 nikdy → miss; bez barů None
    miss = evaluate_scenario(
        _bars([(10, 7620, 7590, 7600)]), entry=7600, targets=[7680], path=path, em_points=None
    )
    assert miss is not None and miss.verdict == "miss" and miss.hit2 is None
    assert evaluate_scenario([], entry=7600, targets=[7680], path=path, em_points=None) is None


def _write_bars(data_dir: Path, symbol: str, day: dt.date, rows: list[Bar]) -> None:
    base = data_dir / "derived" / symbol / "bars"
    base.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(
        [
            {
                "ts_min": bar.ts,
                "open": bar.close,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
            }
            for bar in rows
        ],
        schema=pa.schema(
            [
                ("ts_min", pa.timestamp("us", tz="UTC")),
                ("open", pa.float64()),
                ("high", pa.float64()),
                ("low", pa.float64()),
                ("close", pa.float64()),
            ]
        ),
    )
    pq.write_table(table, base / f"{day.isoformat()}.parquet")


def test_kolektor_hodnoti_jen_po_terminu_a_jen_bary_po_vzniku(tmp_path: Path) -> None:
    db = create_engine(f"sqlite+pysqlite:///{tmp_path / 'db.sqlite'}")
    repo = ScenariosRepository(db)
    repo.ensure_schema()
    EmRespectRepository(db).ensure_schema()
    with db.begin() as conn:
        conn.execute(
            em_respect_table.insert().values(
                session_date=T0.date(),
                symbol="ES",
                ref_ts=T0,
                em_source="straddle",
                anchor=7600.0,
                atm_strike=7600.0,
                em_points=40.0,
                high=7700.0,
                low=7500.0,
                close=7650.0,
                close_in_band=True,
                touch_upper=False,
                touch_lower=False,
                range_vs_em=5.0,
                version=1,
                computed_at=T0,
            )
        )
    # Bar PŘED vznikem scénáře by cíl zasáhl — nesmí se počítat
    _write_bars(
        tmp_path,
        "ES",
        T0.date(),
        _bars([(-30, 7690, 7600, 7685), (20, 7620, 7590, 7610), (40, 7681, 7650, 7670)]),
    )
    deadline_ts = dt.datetime(2026, 9, 15, 20, 0, tzinfo=dt.UTC)
    scenario_id = repo.create(
        symbol="ES",
        day=T0.date(),
        created_at=T0,
        deadline=T0.date(),
        deadline_ts=deadline_ts,
        entry=7600.0,
        targets=[7680.0],
        path=[
            {"ts": _ts(0).isoformat(), "price": 7600},
            {"ts": _ts(60).isoformat(), "price": 7680},
        ],
        annotation_id=None,
        note=None,
    )
    collector = ScenarioCollector(symbol="ES", repository=repo, db=db, data_dir=tmp_path)
    # Před termínem: nic
    assert collector.run(T0 + dt.timedelta(hours=1)) == []
    assert repo.get(scenario_id) is not None and repo.get(scenario_id).result is None  # type: ignore[union-attr]
    # Po termínu: jeden výsledek, jen z barů po vzniku (2 bary), cíl 1 v 40. minutě
    done = collector.run(deadline_ts + dt.timedelta(minutes=20))
    assert len(done) == 1
    row, result = done[0]
    assert row.id == scenario_id
    assert (
        result["hit1"] is True and result["bars"] == 2 and result["hit1_ts"] == _ts(40).isoformat()
    )
    assert result["max_dev_em"] is not None  # EM z em_respect seance vzniku
    stored = repo.get(scenario_id)
    assert stored is not None and stored.evaluated_at is not None and stored.result == result
    # Druhý průchod už nic nehodnotí; statistiky a disk
    assert collector.run(deadline_ts + dt.timedelta(hours=2)) == []
    stats = repo.stats("ES")
    assert stats["n"] == 1 and stats["hit1_rate"] == 1.0 and stats["n_second"] == 0
    assert repo.disk_usage() == {"bytes": 0, "scenarios": 1, "images": 0}
    bars = load_bars_between(tmp_path, "ES", T0, deadline_ts)
    assert [bar.ts for bar in bars] == [_ts(20), _ts(40)]
