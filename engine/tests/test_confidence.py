"""Kalibrovaná confidence z track recordu (#794 fáze 2B): golden koše, fallback, repo."""

import datetime as dt
import json
from pathlib import Path
from typing import cast

from sqlalchemy import create_engine

from gexlens_engine.compute.confidence import (
    CalibrationRow,
    ConfidenceTable,
    build_confidence_table,
)
from gexlens_engine.compute.setups import SETUP_MECHANICS_VERSION
from gexlens_engine.storage.setups_store import SetupsRepository


def _golden() -> dict[str, list[dict[str, object]]]:
    path = Path(__file__).parent / "golden" / "confidence_calibration_794.json"
    return cast(dict[str, list[dict[str, object]]], json.loads(path.read_text(encoding="utf-8")))


def _rows(
    symbol: str, template: str, regime: str | None, wins: int, n: int
) -> list[CalibrationRow]:
    return [
        CalibrationRow(symbol=symbol, template=template, gex_regime=regime, win=i < wins)
        for i in range(n)
    ]


def test_wilson_kose_proti_golden() -> None:
    """Confidence = zaokrouhlená Wilsonova dolní mez × 100; pod minimem vzorku konstanta."""
    for case in _golden()["buckets"]:
        wins, n = int(cast(int, case["wins"])), int(cast(int, case["n"]))
        table = build_confidence_table(_rows("ES", "wall_bounce", "positive", wins, n))
        value, source = table.confidence("ES", "wall_bounce", "positive", fallback=45)
        assert value == case["expected_confidence"], f"wins={wins} n={n} → {value} ({source})"
        if n >= table.min_samples:
            assert source == f"wilson ES·wall_bounce·positive n={n}"
        else:
            assert source == "constant"


def test_kose_od_nejkonkretnejsiho_k_nejobecnejsimu() -> None:
    """Symbol × šablona × režim má přednost; když nestačí, padá se na širší koše."""
    rows = (
        _rows("ES", "failed_break", "negative", 20, 35)  # konkrétní koš stačí
        + _rows("NQ", "failed_break", "negative", 5, 10)  # NQ konkrétní nestačí (10)
        + _rows("NQ", "failed_break", "positive", 10, 25)  # NQ bez režimu = 35 → stačí
    )
    table = build_confidence_table(rows)
    # ES negativní: vlastní koš 20/35 → 41
    assert table.confidence("ES", "failed_break", "negative", 55) == (
        41,
        "wilson ES·failed_break·negative n=35",
    )
    # NQ negativní: vlastní koš 10 < 30 → šablona × režim přes symboly = 25/45
    value, source = table.confidence("NQ", "failed_break", "negative", 55)
    assert source == "wilson failed_break·negative n=45" and 0 < value < 100
    # NQ pozitivní: koš 10/25 nestačí, šablona × režim 10/25 nestačí → symbol × šablona 15/35
    value, source = table.confidence("NQ", "failed_break", "positive", 55)
    assert source == "wilson NQ·failed_break n=35"
    # Neznámá šablona → konstanta
    assert table.confidence("ES", "max_pain_pin", "positive", 60) == (60, "constant")
    # Vlastní minimum vzorku (parametr store)
    strict = build_confidence_table(rows, min_samples=100)
    assert strict.confidence("ES", "failed_break", "negative", 55) == (55, "constant")
    # Prázdná tabulka = všude konstanta
    assert ConfidenceTable().confidence("ES", "failed_break", None, 50) == (50, "constant")


def test_repository_closed_for_calibration(tmp_path: Path) -> None:
    """Jen uzavřené setupy aktuální mechaniky; výhra = closed_target; režim z contextu."""
    db = create_engine(f"sqlite+pysqlite:///{tmp_path / 'setups.sqlite'}")
    repo = SetupsRepository(db)
    repo.ensure_schema()
    ts = dt.datetime(2026, 9, 9, 14, 0, tzinfo=dt.UTC)

    def make(template: str, regime: str | None, status: str) -> int:
        context: dict[str, object] = {"gex_regime": regime} if regime else {}
        setup_id = repo.create(
            symbol="ES",
            expiry="20260909",
            template=template,
            direction="long",
            created_ts=ts,
            entry=6500.0,
            target=6510.0,
            stop=6495.0,
            confidence=55,
            reason="test",
            context=context,
        )
        if status != "active":
            repo.close(
                setup_id,
                status=status,
                closed_ts=ts + dt.timedelta(minutes=5),
                outcome_r=1.0 if status == "closed_target" else -1.0,
                mfe=1.0,
                mae=0.5,
            )
        return setup_id

    make("failed_break", "negative", "closed_target")
    make("failed_break", "negative", "closed_stop")
    make("failed_break", None, "closed_timeout")
    make("wall_bounce", "positive", "active")  # aktivní se nepočítá

    rows = repo.closed_for_calibration(mechanics_version=SETUP_MECHANICS_VERSION)
    assert len(rows) == 3
    assert sum(row.win for row in rows) == 1
    assert {row.gex_regime for row in rows} == {"negative", None}
    assert repo.closed_for_calibration(mechanics_version=SETUP_MECHANICS_VERSION + 1) == []
