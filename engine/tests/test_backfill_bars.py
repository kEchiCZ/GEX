"""Oprava dnů (`--days`, #1320) a minut (`--replace-minutes`, #1885) skriptem
`scripts/backfill_bars.py` nad fixture particí v tmp.

IBKR se nevolá: spojení, rozlišení kontraktu a historický klient jsou podvržené
v namespace skriptu (načítá se přes importlib, vzor test_recompute_setup_outcomes).
Ověřuje se celý průchod `run_days` a `run_minutes`: stráž kontraktu, plán,
upsert a pojistka, že ostatní měřené minuty zůstanou.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import logging
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
    BAR_SOURCE_TASTY_TRADES,
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


def bar(i: int, close: float, source: str | None, volume: float = 10.0) -> Bar:
    return Bar(
        ts=START + i * MINUTE,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=volume,
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
            # Historický klient původ nerazí — razítko dává UnderlyingBackfiller;
            # vrací jen UTC den `day` jako skutečný klient
            return [
                Bar(ts=b.ts, open=b.open, high=b.high, low=b.low, close=b.close, volume=b.volume)
                for b in by_contract.get(self._contract, [])
                if b.ts.astimezone(dt.UTC).date() == day
            ]

    monkeypatch.setattr(mod, "connect", connect)
    monkeypatch.setattr(mod, "resolve_contract", resolve_contract)
    monkeypatch.setattr(mod, "IbHistoricalClient", FakeHistorical)
    monkeypatch.setattr(mod, "THROTTLE_S", 0.0)
    return requested


def ibkr_day(offset: float = 0.0) -> list[Bar]:
    """IBKR historical celého okna; živé minuty o tick jinde, ať je vidět, že zůstaly."""
    return [bar(i, price(i) + 0.25 + offset, None) for i in range(30)]


async def run(
    mod: Any,
    data: Path,
    *,
    replace_tasty: bool,
    replace_wrong_contract: bool = False,
    dry_run: bool = False,
) -> int:
    result: int = await mod.run_days(
        ["ES"],
        [DAY],
        Settings(data_dir=data),
        4001,
        replace_tasty=replace_tasty,
        replace_wrong_contract=replace_wrong_contract,
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


async def test_replace_wrong_contract_rewrites_only_historical_out_of_tolerance(
    mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """9. 9. 2026: starší `ibkr_hist` ze Z6 mezi měřenými minutami U6 (#1320, #1232).

    S `--replace-wrong-contract` se přepíše jen `ibkr_hist` mimo toleranci;
    měřená minuta (i o basis jinde), `ibkr_hist` v toleranci a rekonstrukce
    bez `--replace-tasty` zůstávají. Druhý běh partici nezmění (idempotence).
    """
    wrong = [10, 11, 12, 13, 14]
    near = [15, 16, 17]
    live = [i for i in range(30) if i not in (*wrong, *near, 20, 25)]
    rows = (
        [bar(i, price(i), None) for i in live]
        + [bar(i, price(i) + 77.0, BAR_SOURCE_HISTORICAL) for i in wrong]  # Z6, +1 %
        + [bar(i, price(i) - 1.0, BAR_SOURCE_HISTORICAL) for i in near]  # v toleranci
        + [bar(20, price(20) + 0.5, BAR_SOURCE_RECONSTRUCTED)]
        + [bar(25, price(25) + 77.0, None)]  # měřená minuta mimo kontrakt dne
    )
    path = SnapshotWriter(Settings(data_dir=tmp_path)).write_bars("ES", DAY, rows)
    before = read(path)
    install_fakes(mod, monkeypatch, {"202609": ibkr_day()})

    # Bez volby zůstává dřívější doplnění beze změny
    assert await run(mod, tmp_path, replace_tasty=False) == 0
    assert read(path) == before

    assert await run(mod, tmp_path, replace_tasty=False, replace_wrong_contract=True) == 0
    after = read(path)
    for i in wrong:
        assert after[i] == (price(i) + 0.25, BAR_SOURCE_HISTORICAL)
    for i in [*live, 25, *near, 20]:
        assert after[i] == before[i]
    assert after[25][1] == BAR_SOURCE_LIVE
    assert after[20][1] == BAR_SOURCE_RECONSTRUCTED

    content = path.read_bytes()
    assert await run(mod, tmp_path, replace_tasty=False, replace_wrong_contract=True) == 0
    assert path.read_bytes() == content


async def test_replace_wrong_contract_keeps_session_after_daily_pause(
    mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Konec dne po denní pauze z jiného kontraktu se nepřepíše (#1320).

    Rolovací den: engine měřil U6 do 20:59 UTC, s novou seancí (22:00 UTC)
    Z6 a konec partice je `ibkr_hist` ze Z6. Medián dne vybere U6; jediná
    měřená minuta vedle bloku je z předchozí seance, takže kontrakt bloku
    nejde ověřit — blok se odmítne, návratový kód 1, partice beze změny.
    """
    u6_live = list(range(410, 420))  # 20:50–20:59
    z6_hist = list(range(480, 490))  # 22:00–22:09
    rows = [bar(i, price(i), None) for i in u6_live] + [
        bar(i, price(i) + 77.0, BAR_SOURCE_HISTORICAL) for i in z6_hist
    ]
    path = SnapshotWriter(Settings(data_dir=tmp_path)).write_bars("ES", DAY, rows)
    content = path.read_bytes()
    install_fakes(
        mod, monkeypatch, {"202609": [bar(i, price(i), None) for i in [*u6_live, *z6_hist]]}
    )

    assert await run(mod, tmp_path, replace_tasty=False, replace_wrong_contract=True) == 1
    assert path.read_bytes() == content
    assert "blok 22:00–22:09 (10 min) se nezapíše" in caplog.text
    assert "žádná měřená minuta téže seance" in caplog.text


PRIOR_EVENING = list(range(-850, -840))  # 9. 9. 23:50–23:59, měřeno
PRIOR_UNMEASURED = list(range(-860, -850))  # 9. 9. 23:40–23:49, v partici chybí
HOLE_AT_MIDNIGHT = list(range(-840, -830))  # 10. 9. 00:00–00:09
EVENING = list(range(530, 540))  # 10. 9. 22:50–22:59 = seance 11. 9.


@pytest.mark.parametrize(
    ("offsets", "expected_requests"),
    [
        ({"202609": 0.0}, ["202609", "202609"]),
        # Front podle expirace nesedí, ověří se další kvartál — i večer se stáhne z něj
        ({"202609": 77.0, "202612": 0.0}, ["202609", "202612", "202612"]),
    ],
)
async def test_session_start_is_verified_from_prior_partition(
    mod: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    offsets: dict[str, float],
    expected_requests: list[str],
) -> None:
    """Díra od půlnoci se doplní podle měřeného večera předchozí partice (#1390).

    Jako ES 14. 7. 2026: engine měřil jen 22:50–22:59 UTC (seance dalšího dne)
    a večer předtím. Seance dne začala 9. 9. v 17:00 CT, takže stranu před
    blokem 00:00–00:09 ověří až partice 9. 9. Skript pro ni stáhne předchozí
    den z ověřeného kontraktu (ne z prvního kandidáta) a do předchozí partice
    nic nezapíše — ani minuty, které v ní chybějí.
    """
    caplog.set_level(logging.INFO, logger="backfill_bars")
    writer = SnapshotWriter(Settings(data_dir=tmp_path))
    prior_path = writer.write_bars(
        "ES", DAY - dt.timedelta(days=1), [bar(i, price(i), None) for i in PRIOR_EVENING]
    )
    prior_content = prior_path.read_bytes()
    path = writer.write_bars("ES", DAY, [bar(i, price(i), None) for i in EVENING])
    minutes = [*PRIOR_UNMEASURED, *PRIOR_EVENING, *HOLE_AT_MIDNIGHT, *EVENING]
    requested = install_fakes(
        mod,
        monkeypatch,
        {
            month: [bar(i, price(i) + offset, None) for i in minutes]
            for month, offset in offsets.items()
        },
    )

    assert await run(mod, tmp_path, replace_tasty=False) == 0
    assert requested == expected_requests
    assert prior_path.read_bytes() == prior_content
    rows = read(path)
    assert sorted(rows) == [*HOLE_AT_MIDNIGHT, *EVENING]
    assert all(rows[i] == (price(i), BAR_SOURCE_HISTORICAL) for i in HOLE_AT_MIDNIGHT)
    assert all(rows[i] == (price(i), BAR_SOURCE_LIVE) for i in EVENING)
    verified = expected_requests[-1]
    assert (
        f"večer předchozí partice — 10 měřených minut seance, 20 barů z {verified}" in caplog.text
    )


#: `--replace-minutes` (#1885): neúplné měřené bary 5 a 6 (první minuta po
#: reconnectu, provizorní bar přes bar z tasty), minuta 7 má v historii týž
#: objem, minuta 8 je bar z tisků tasty mezi nimi a vyjmenovaná není
PARTIAL = {5: 2.0, 6: 3.0}
SAME_VOLUME = 7
TASTY_NEIGHBOUR = 8


def seed_partial(data: Path) -> Path:
    rows = [
        bar(i, price(i), None, PARTIAL.get(i, 10.0))
        for i in range(30)
        if i not in (TASTY_NEIGHBOUR, 15)  # 15 = díra
    ] + [bar(TASTY_NEIGHBOUR, price(TASTY_NEIGHBOUR) + 0.5, BAR_SOURCE_TASTY_TRADES)]
    return SnapshotWriter(Settings(data_dir=data)).write_bars("ES", DAY, rows)


def full_history(offset: dict[int, float] | None = None) -> list[Bar]:
    """Celé okno z IBKR historical: o tick jinde a s plným objemem 40 (minuta 7: 10)."""
    offset = offset or {}
    return [
        bar(i, price(i) + 0.25 + offset.get(i, 0.0), None, 10.0 if i == SAME_VOLUME else 40.0)
        for i in range(30)
    ]


def read_full(path: Path) -> dict[int, tuple[float, float, str | None]]:
    out: dict[int, tuple[float, float, str | None]] = {}
    for row in pq.read_table(path).to_pylist():
        ts = row["ts_min"].astimezone(dt.UTC)
        out[int((ts - START) / MINUTE)] = (row["close"], row["volume"], row["source"])
    return out


async def run_minutes(
    mod: Any, data: Path, minutes: Sequence[int], *, dry_run: bool = False
) -> int:
    result: int = await mod.run_minutes(
        {("ES", DAY): [START + i * MINUTE for i in minutes]},
        Settings(data_dir=data),
        4001,
        dry_run=dry_run,
    )
    return result


async def test_replace_minutes_overrides_rank_only_for_listed_minutes(
    mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Měřený `ibkr` (rank 3) ustoupí `ibkr_hist` (rank 2) jen ve vyjmenovaných minutách.

    Sousední měřené minuty, bar z tasty mezi nimi i díra zůstanou, i když je
    historie má jinak. Minuta se shodným objemem zůstane. Druhý běh nic nezmění.
    """
    path = seed_partial(tmp_path)
    before = read_full(path)
    install_fakes(mod, monkeypatch, {"202609": full_history()})

    assert await run_minutes(mod, tmp_path, [5, 6, SAME_VOLUME]) == 0

    after = read_full(path)
    for i in PARTIAL:
        assert before[i] == (price(i), PARTIAL[i], BAR_SOURCE_LIVE)
        assert after[i] == (price(i) + 0.25, 40.0, BAR_SOURCE_HISTORICAL)
    for i in sorted(set(before) - set(PARTIAL)):  # vč. 4, 7, 8 a 9 vedle přepsaných
        assert after[i] == before[i]
    assert after[TASTY_NEIGHBOUR][2] == BAR_SOURCE_TASTY_TRADES
    assert 15 not in after

    content = path.read_bytes()
    assert await run_minutes(mod, tmp_path, [5, 6, SAME_VOLUME]) == 0
    assert path.read_bytes() == content


async def test_replace_minutes_rejects_hole_missing_history_and_other_contract(
    mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Díra, minuta bez historie a close o basis jinde se nezapíšou, návratový kód 1.

    Platná vyjmenovaná minuta téže partice se přepíše.
    """
    path = seed_partial(tmp_path)
    before = read_full(path)
    history = [b for b in full_history(offset={6: 77.0}) if b.ts != START + 12 * MINUTE]
    install_fakes(mod, monkeypatch, {"202609": history})

    assert await run_minutes(mod, tmp_path, [5, 6, 12, 15]) == 1

    after = read_full(path)
    assert after[5] == (price(5) + 0.25, 40.0, BAR_SOURCE_HISTORICAL)
    for i in (6, 12):
        assert after[i] == before[i]
    assert 15 not in after
    assert "2026-09-10T14:06" in caplog.text and "jiný kontrakt než 202609" in caplog.text
    assert "IBKR minutu z 202609 nevrátil" in caplog.text
    assert "minuta v partici chybí" in caplog.text


async def test_replace_minutes_dry_run_lists_before_and_after(
    mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="backfill_bars")
    path = seed_partial(tmp_path)
    content = path.read_bytes()
    install_fakes(mod, monkeypatch, {"202609": full_history()})

    assert await run_minutes(mod, tmp_path, [5], dry_run=True) == 0

    assert path.read_bytes() == content
    assert (
        "ES 2026-09-10T14:05: ibkr O 7701.25 H 7701.25 L 7701.25 C 7701.25 V 2 → "
        "ibkr_hist O 7701.5 H 7701.5 L 7701.5 C 7701.5 V 40 — nezapisuje se (dry-run)"
    ) in caplog.text


def test_parse_minutes_groups_by_symbol_and_utc_partition(mod: Any) -> None:
    parsed = mod.parse_minutes(
        "ES:2026-11-24T05:26, nq:2026-11-24T05:26,ES:2026-11-24T05:26,ES:2026-11-25T00:00,"
    )
    utc = dt.UTC
    assert parsed == {
        ("ES", dt.date(2026, 11, 24)): [dt.datetime(2026, 11, 24, 5, 26, tzinfo=utc)],
        ("ES", dt.date(2026, 11, 25)): [dt.datetime(2026, 11, 25, 0, 0, tzinfo=utc)],
        ("NQ", dt.date(2026, 11, 24)): [dt.datetime(2026, 11, 24, 5, 26, tzinfo=utc)],
    }
    for raw in ("ES 2026-11-24T05:26", "ES:2026-11-24 05:26", ":2026-11-24T05:26"):
        with pytest.raises(argparse.ArgumentTypeError):
            mod.parse_minutes(raw)


@pytest.mark.parametrize(
    "extra",
    [
        # Partici včerejška drží engine v paměti (BUFFER_KEEP_DAYS)
        [],
        ["--days", "2026-09-10"],
    ],
)
def test_main_refuses_recent_partition_and_mixed_modes(
    mod: Any, monkeypatch: pytest.MonkeyPatch, extra: list[str]
) -> None:
    yesterday = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1)
    minute = f"ES:{yesterday.isoformat()}T05:26" if not extra else "ES:2026-09-10T14:05"
    monkeypatch.setattr(sys, "argv", ["backfill_bars.py", "--replace-minutes", minute, *extra])
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 2
