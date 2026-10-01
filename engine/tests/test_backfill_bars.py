"""Oprava dnů skriptem `scripts/backfill_bars.py --days` nad fixture particí v tmp (#1320).

IBKR se nevolá: spojení, rozlišení kontraktu a historický klient jsou podvržené
v namespace skriptu (načítá se přes importlib, vzor test_recompute_setup_outcomes).
Ověřuje se celý průchod `run_days`: stráž kontraktu, plán, upsert a pojistka,
že měřené minuty zůstanou.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest

from gexlens_engine.config import Settings
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.storage.parquet_store import (
    BAR_SOURCE_HISTORICAL,
    BAR_SOURCE_LIVE,
    BAR_SOURCE_RECONSTRUCTED,
    SnapshotWriter,
)

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backfill_bars.py"
DAY = dt.date(2026, 9, 10)  # roll týden: kandidáti 202609, 202612
START = dt.datetime(2026, 9, 10, 14, 0, tzinfo=dt.UTC)
MINUTE = dt.timedelta(minutes=1)


@pytest.fixture(scope="module")
def mod() -> Any:
    spec = importlib.util.spec_from_file_location("backfill_bars", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def price(i: int) -> float:
    return 7700.0 + 0.25 * i


def bar(i: int, close: float, source: str | None) -> Bar:
    return Bar(
        ts=START + i * MINUTE,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=10.0,
        source=source,
    )


#: Fixture partice 14:00–14:29: živé 0–9 a 20–29, tasty 10–14, díra 15–16,
#: dřívější doplnění ibkr_hist 17–19 (s vlastní hodnotou, aby šla poznat)
LIVE = [*range(0, 10), *range(20, 30)]
TASTY = list(range(10, 15))
HOLE = [15, 16]
OLD_HIST = [17, 18, 19]


def seed_partition(data: Path) -> Path:
    writer = SnapshotWriter(Settings(data_dir=data))
    rows = (
        [bar(i, price(i), None) for i in LIVE]  # živá cesta → writer zapíše `ibkr`
        + [bar(i, price(i) + 0.5, BAR_SOURCE_RECONSTRUCTED) for i in TASTY]
        + [bar(i, price(i) - 1.0, BAR_SOURCE_HISTORICAL) for i in OLD_HIST]
    )
    return writer.write_bars("ES", DAY, rows)


def read(path: Path) -> dict[int, tuple[float, str | None]]:
    out: dict[int, tuple[float, str | None]] = {}
    for row in pq.read_table(path).to_pylist():
        ts = row["ts_min"].astimezone(dt.UTC)
        out[int((ts - START) / MINUTE)] = (row["close"], row["source"])
    return out


class FakeIB:
    def disconnect(self) -> None:
        return None


def install_fakes(
    mod: Any, monkeypatch: pytest.MonkeyPatch, by_contract: dict[str, Sequence[Bar]]
) -> list[str]:
    """Podvrhne IBKR: kontrakt = YYYYMM, historie = bary podle kontraktu."""
    requested: list[str] = []

    async def connect(port: int, settings: Settings) -> FakeIB:
        return FakeIB()

    async def resolve_contract(ib: object, symbol: str, contract_month: str) -> str:
        return contract_month

    class FakeHistorical:
        def __init__(self, ib: object, contract: str) -> None:
            self._contract = contract

        async def fetch_day_bars(self, symbol: str, day: dt.date) -> list[Bar]:
            requested.append(self._contract)
            # Historický klient původ nerazí — razítko dává UnderlyingBackfiller
            return [
                Bar(ts=b.ts, open=b.open, high=b.high, low=b.low, close=b.close, volume=b.volume)
                for b in by_contract.get(self._contract, [])
            ]

    monkeypatch.setattr(mod, "connect", connect)
    monkeypatch.setattr(mod, "resolve_contract", resolve_contract)
    monkeypatch.setattr(mod, "IbHistoricalClient", FakeHistorical)
    monkeypatch.setattr(mod, "THROTTLE_S", 0.0)
    return requested


def ibkr_day(offset: float = 0.0) -> list[Bar]:
    """IBKR historical celého okna; živé minuty o tick jinde, ať je vidět, že zůstaly."""
    return [bar(i, price(i) + 0.25 + offset, None) for i in range(30)]


async def run(mod: Any, data: Path, *, replace_tasty: bool, dry_run: bool = False) -> int:
    result: int = await mod.run_days(
        ["ES"],
        [DAY],
        Settings(data_dir=data),
        4001,
        replace_tasty=replace_tasty,
        dry_run=dry_run,
    )
    return result


async def test_replace_tasty_replaces_reconstruction_and_keeps_live(
    mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = seed_partition(tmp_path)
    before = read(path)
    # První kandidát (U6) je tu „jiný kontrakt“ o +1 % — stráž ho musí odmítnout
    requested = install_fakes(
        mod, monkeypatch, {"202609": ibkr_day(offset=77.0), "202612": ibkr_day()}
    )

    assert await run(mod, tmp_path, replace_tasty=True) == 0

    after = read(path)
    assert requested == ["202609", "202612"]
    for i in LIVE:  # měřené minuty beze změny hodnoty i původu
        assert after[i] == before[i] == (price(i), BAR_SOURCE_LIVE)
    for i in TASTY + HOLE:  # rekonstrukce i díra = bar IBKR historical
        assert after[i] == (price(i) + 0.25, BAR_SOURCE_HISTORICAL)
    for i in OLD_HIST:  # dřívější doplnění plán nepřepisuje
        assert after[i] == before[i] == (price(i) - 1.0, BAR_SOURCE_HISTORICAL)
    assert sorted(after) == list(range(30))


async def test_without_replace_tasty_only_holes_are_filled(
    mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = seed_partition(tmp_path)
    before = read(path)
    install_fakes(mod, monkeypatch, {"202609": ibkr_day()})

    assert await run(mod, tmp_path, replace_tasty=False) == 0

    after = read(path)
    for i in TASTY:
        assert after[i] == before[i]
        assert after[i][1] == BAR_SOURCE_RECONSTRUCTED
    for i in HOLE:
        assert after[i] == (price(i) + 0.25, BAR_SOURCE_HISTORICAL)


async def test_dry_run_writes_nothing(
    mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = seed_partition(tmp_path)
    content = path.read_bytes()
    install_fakes(mod, monkeypatch, {"202609": ibkr_day()})

    assert await run(mod, tmp_path, replace_tasty=True, dry_run=True) == 0
    assert path.read_bytes() == content


async def test_wrong_contracts_or_no_measured_reference_write_nothing(
    mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = seed_partition(tmp_path)
    content = path.read_bytes()
    # Oba kandidáti mimo toleranci → den se nezapíše, návratový kód 1
    install_fakes(
        mod, monkeypatch, {"202609": ibkr_day(offset=77.0), "202612": ibkr_day(offset=-77.0)}
    )
    assert await run(mod, tmp_path, replace_tasty=True) == 1
    assert path.read_bytes() == content

    # Partice bez měřené minuty: kontrakt nelze ověřit → nezapisuje se
    only_tasty = tmp_path / "only-tasty"
    SnapshotWriter(Settings(data_dir=only_tasty)).write_bars(
        "ES", DAY, [bar(i, price(i), BAR_SOURCE_RECONSTRUCTED) for i in range(30)]
    )
    tasty_path = only_tasty / "derived" / "ES" / "bars" / f"{DAY.isoformat()}.parquet"
    tasty_content = tasty_path.read_bytes()
    install_fakes(mod, monkeypatch, {"202609": ibkr_day()})
    assert await run(mod, only_tasty, replace_tasty=True) == 1
    assert tasty_path.read_bytes() == tasty_content


def test_write_bars_keeps_live_minute_against_historical(tmp_path: Path) -> None:
    """Pojistka pod plánem: `write_bars` (keep_existing, `bar_source_rank`)."""
    path = seed_partition(tmp_path)
    writer = SnapshotWriter(Settings(data_dir=tmp_path))
    # Celý den IBKR historical bez plánu: živá minuta zůstane, tasty se nahradí,
    # stejný původ (ibkr_hist) vyhraje pozdější zápis
    writer.write_bars("ES", DAY, [bar(i, 1.0, BAR_SOURCE_HISTORICAL) for i in range(30)])
    after = read(path)
    assert all(after[i] == (price(i), BAR_SOURCE_LIVE) for i in LIVE)
    assert all(after[i] == (1.0, BAR_SOURCE_HISTORICAL) for i in TASTY + HOLE + OLD_HIST)


async def test_block_next_to_other_measured_contract_is_not_written(
    mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Smíšená partice: engine měřil U6 a pak (roll novým discovery) Z6 (#1320).

    Medián dne vybere U6 (většina měřených minut), blok tasty uvnitř úseku Z6
    se proto nesmí zapsat barem U6 — jinak skok o basis jako #1232.
    """
    u6_live = [*range(0, 10), *range(13, 20)]
    z6_live = [*range(20, 25), 28, 29]
    rows = (
        [bar(i, price(i), None) for i in u6_live]
        + [bar(i, price(i) + 77.0, None) for i in z6_live]
        + [bar(i, price(i) + 0.5, BAR_SOURCE_RECONSTRUCTED) for i in (10, 11, 12)]
        + [bar(i, price(i) + 77.5, BAR_SOURCE_RECONSTRUCTED) for i in (25, 26, 27)]
    )
    path = SnapshotWriter(Settings(data_dir=tmp_path)).write_bars("ES", DAY, rows)
    before = read(path)
    requested = install_fakes(
        mod, monkeypatch, {"202609": ibkr_day(), "202612": ibkr_day(offset=77.0)}
    )

    # Odmítnutý blok = návratový kód 1, i když zbytek dne se zapsal
    assert await run(mod, tmp_path, replace_tasty=True) == 1

    after = read(path)
    assert requested == ["202609"]
    for i in (10, 11, 12):  # blok uvnitř U6 = bar IBKR historical U6
        assert after[i] == (price(i) + 0.25, BAR_SOURCE_HISTORICAL)
    for i in (25, 26, 27):  # blok uvnitř Z6 zůstává rekonstrukcí
        assert after[i] == before[i]
        assert after[i][1] == BAR_SOURCE_RECONSTRUCTED
    for i in u6_live + z6_live:
        assert after[i] == before[i]
        assert after[i][1] == BAR_SOURCE_LIVE
