"""Testy OIArchiveru (issue #9): dva dny v archivu, idempotence, chybějící OI, real PG."""

import datetime as dt
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from gexlens_engine.config import Settings
from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.ibkr.mock import MockOIFetcher
from gexlens_engine.storage.oi_archive import OIArchiver, OIEodRepository

DAY_1 = dt.date(2026, 7, 15)
DAY_2 = dt.date(2026, 7, 16)


def contracts(count: int = 6) -> list[OptionContractSpec]:
    strikes = [7590.0 + 5 * i for i in range(count // 2)]
    return [
        OptionContractSpec(
            symbol="ES",
            sec_type="FOP",
            expiry="20260716",
            strike=strike,
            right=right,
            exchange="CME",
            trading_class="E3D",
            multiplier="50",
        )
        for strike in strikes
        for right in ("C", "P")
    ]


@pytest.fixture
def repository(tmp_path: Path) -> OIEodRepository:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'test.db'}")
    repo = OIEodRepository(engine)
    repo.ensure_schema()
    return repo


async def test_two_days_both_archived(repository: OIEodRepository) -> None:
    specs = contracts()
    fetcher = MockOIFetcher({spec: 1000.0 + i for i, spec in enumerate(specs)})
    archiver = OIArchiver(repository, fetcher, Settings())

    result_1 = await archiver.archive_day(specs, DAY_1)
    result_2 = await archiver.archive_day(specs, DAY_2)

    # AC: po dvou simulovaných dnech obsahuje oi_eod oba dny
    assert repository.days("ES") == [DAY_1, DAY_2]
    assert result_1.written == 6
    assert result_2.written == 6
    assert repository.count_for_day("ES", DAY_1) == 6
    assert repository.count_for_day("ES", DAY_2) == 6


async def test_rerun_same_day_is_idempotent_upsert(repository: OIEodRepository) -> None:
    specs = contracts()
    archiver = OIArchiver(repository, MockOIFetcher(dict.fromkeys(specs, 500.0)), Settings())
    await archiver.archive_day(specs, DAY_1)

    # Druhý běh týž den s novými hodnotami → update, žádné duplicity
    archiver_updated = OIArchiver(
        repository, MockOIFetcher(dict.fromkeys(specs, 750.0)), Settings()
    )
    await archiver_updated.archive_day(specs, DAY_1)

    assert repository.count_for_day("ES", DAY_1) == 6
    assert repository.get_oi("ES", DAY_1, specs[0].strike, specs[0].right) == 750.0


async def test_missing_oi_reported_not_written(repository: OIEodRepository) -> None:
    specs = contracts()
    values = {spec: 100.0 for spec in specs[:4]}  # poslední 2 kontrakty OI nedodají
    archiver = OIArchiver(repository, MockOIFetcher(values), Settings())

    result = await archiver.archive_day(specs, DAY_1)

    assert result.written == 4
    assert set(result.missing) == set(specs[4:])
    assert repository.count_for_day("ES", DAY_1) == 4


async def test_fetcher_exception_counts_as_missing(repository: OIEodRepository) -> None:
    specs = contracts(count=2)

    class ExplodingFetcher(MockOIFetcher):
        async def fetch_oi(self, spec: OptionContractSpec, timeout_s: float) -> float | None:
            if spec == specs[0]:
                raise RuntimeError("mock: timeout")
            return 42.0

    archiver = OIArchiver(repository, ExplodingFetcher(), Settings())
    result = await archiver.archive_day(specs, DAY_1)

    assert result.written == 1
    assert result.missing == (specs[0],)


@pytest.mark.skipif(
    not os.environ.get("GEXLENS_TEST_PG_DSN"),
    reason="GEXLENS_TEST_PG_DSN nenastaveno (integrace s reálným PostgreSQL)",
)
async def test_upsert_on_real_postgres() -> None:
    engine = create_engine(os.environ["GEXLENS_TEST_PG_DSN"])
    repo = OIEodRepository(engine)
    repo.ensure_schema()
    specs = contracts()
    archiver = OIArchiver(repo, MockOIFetcher(dict.fromkeys(specs, 111.0)), Settings())

    await archiver.archive_day(specs, DAY_1)
    await archiver.archive_day(specs, DAY_1)  # idempotence na PG ON CONFLICT

    assert repo.count_for_day("ES", DAY_1) == 6
    assert DAY_1 in repo.days("ES")
