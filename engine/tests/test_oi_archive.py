"""Testy OIArchiveru (issue #9): dva dny v archivu, idempotence, chybějící OI, real PG."""

import datetime as dt
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from gexlens_engine.config import Settings
from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.ibkr.mock import MockOIFetcher
from gexlens_engine.storage.oi_archive import (
    ContractSnapshot,
    OIArchiver,
    OIEodRepository,
    OIRecord,
)

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
    # Multi-expirační archiv (ΔOI): expiry filtr vrací hodnotu správného řetězu
    repository.upsert_many(
        [OIRecord("ES", "20990101", specs[0].strike, specs[0].right, DAY_1, 111.0)]
    )
    assert (
        repository.get_oi("ES", DAY_1, specs[0].strike, specs[0].right, expiry="20990101") == 111.0
    )
    # Bez filtru se bere nejbližší expirace (deterministicky, žádný MultipleResultsFound)
    assert repository.get_oi("ES", DAY_1, specs[0].strike, specs[0].right) == 750.0


async def test_captured_ts_zaznamenan_a_changed_hlasi_ustaleni(
    repository: OIEodRepository,
) -> None:
    """#463: archiv nese čas pořízení a druhé shodné čtení hlásí `changed=False`."""
    specs = contracts()
    ranni = dt.datetime(2026, 7, 15, 0, 5, tzinfo=dt.UTC)
    archiver = OIArchiver(repository, MockOIFetcher(dict.fromkeys(specs, 500.0)), Settings())

    first = await archiver.archive_day(specs, DAY_1, now=ranni)

    assert first.changed is True  # první snímek dne je vždy změna
    assert repository.captured_at("ES", DAY_1) == ranni

    # Publikace doběhla — jiné hodnoty, tedy ještě není ustáleno
    po_publikaci = dt.datetime(2026, 7, 15, 12, 30, tzinfo=dt.UTC)
    archiver_new = OIArchiver(repository, MockOIFetcher(dict.fromkeys(specs, 1500.0)), Settings())
    second = await archiver_new.archive_day(specs, DAY_1, now=po_publikaci)

    assert second.changed is True
    assert repository.get_oi("ES", DAY_1, specs[0].strike, specs[0].right) == 1500.0
    assert repository.captured_at("ES", DAY_1) == po_publikaci

    # Třetí čtení dá totéž → ustáleno
    third = await archiver_new.archive_day(
        specs, DAY_1, now=dt.datetime(2026, 7, 15, 13, 0, tzinfo=dt.UTC)
    )

    assert third.changed is False


async def test_vypadek_drive_archivovaneho_kontraktu_brani_finalite(
    repository: OIEodRepository,
) -> None:
    """#494 (4): neúplné potvrzovací čtení nesmí prohlásit snímek za finální.

    Kontrakt, který archiv už má a aktuální čtení ho nedodalo, nejde potvrdit
    jako nezměněný — `changed` musí zůstat True."""
    specs = contracts()
    archiver = OIArchiver(repository, MockOIFetcher(dict.fromkeys(specs, 500.0)), Settings())
    await archiver.archive_day(specs, DAY_1)

    # Potvrzovací čtení: první (dřív archivovaný) kontrakt OI nedodal, zbytek beze změny
    partial = OIArchiver(repository, MockOIFetcher(dict.fromkeys(specs[1:], 500.0)), Settings())
    result = await partial.archive_day(specs, DAY_1)

    assert specs[0] in result.missing
    assert result.changed is True


async def test_trvale_chybejici_strike_nebrani_ustaleni(repository: OIEodRepository) -> None:
    """#494 (4): strike bez OI v obou čteních (nikdy archivovaný) finalitu neblokuje —
    jinak by se archiv obnovoval donekonečna."""
    specs = contracts()
    values = dict.fromkeys(specs[1:], 500.0)  # první kontrakt OI nedodá nikdy
    archiver = OIArchiver(repository, MockOIFetcher(values), Settings())

    await archiver.archive_day(specs, DAY_1)
    result = await archiver.archive_day(specs, DAY_1)

    assert specs[0] in result.missing
    assert result.changed is False  # dvě shodná čtení → ustáleno


def test_captured_at_bez_archivu_je_none(repository: OIEodRepository) -> None:
    assert repository.captured_at("ES", DAY_1) is None


async def test_migrace_doplni_captured_ts_stare_tabulce(tmp_path: Path) -> None:
    """Řádky z doby před #463 mají NULL — berou se jako předpublikační."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE oi_eod (symbol VARCHAR(16), expiry VARCHAR(8), strike FLOAT,"
                ' "right" VARCHAR(1), date DATE, oi FLOAT,'
                ' PRIMARY KEY (symbol, expiry, strike, "right", date))'
            )
        )
        conn.execute(
            text("INSERT INTO oi_eod VALUES ('ES', '20260716', 7600.0, 'C', '2026-07-15', 123.0)")
        )

    repo = OIEodRepository(engine)
    repo.ensure_schema()  # nesmí spadnout na existující tabulce bez sloupce

    assert repo.get_oi("ES", DAY_1, 7600.0, "C") == 123.0
    assert repo.captured_at("ES", DAY_1) is None


async def test_duplicate_series_merged_by_sum(repository: OIEodRepository) -> None:
    """#215 → #736: série téže expirace mají VLASTNÍ řádky, čtení je sčítá.

    Dřív se slévaly do jednoho (obrana proti CardinalityViolation); od #736
    nese klíč trading_class, takže se zapíší obě a Σ dělá čtecí strana."""
    base = OptionContractSpec(
        symbol="MES",
        sec_type="FOP",
        expiry="20260722",
        strike=7500.0,
        right="C",
        exchange="CME",
        trading_class="MS4C",
        multiplier="5",
    )
    twin = OptionContractSpec(
        symbol="MES",
        sec_type="FOP",
        expiry="20260722",
        strike=7500.0,
        right="C",
        exchange="CME",
        trading_class="EX4C",  # jiná série, stejný klíč archivu
        multiplier="5",
    )
    fetcher = MockOIFetcher({base: 100.0, twin: 50.0})
    archiver = OIArchiver(repository, fetcher, Settings())

    result = await archiver.archive_day([base, twin], DAY_1)

    assert result.written == 2  # každá série má vlastní řádek (#736)
    assert repository.count_for_day("MES", DAY_1) == 2
    assert repository.get_oi("MES", DAY_1, 7500.0, "C") == 150.0  # čtení Σ obou sérií


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
        async def fetch_snapshot(
            self, spec: OptionContractSpec, timeout_s: float
        ) -> ContractSnapshot | None:
            if spec == specs[0]:
                raise RuntimeError("mock: timeout")
            return ContractSnapshot(oi=42.0)

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


async def test_snapshot_hodnoty_se_ulozi_a_prectou(repository: OIEodRepository) -> None:
    """#519: IV/greeks/prémie z ranního průchodu projdou až do chain_for_day."""
    specs = contracts(count=2)
    fetcher = MockOIFetcher(
        snapshots={
            specs[0]: ContractSnapshot(
                oi=100.0,
                iv=0.21,
                delta=0.55,
                gamma=0.002,
                theta=-1.2,
                vega=3.4,
                close_prem=12.5,
                und_price=7500.25,
            ),
            specs[1]: ContractSnapshot(oi=50.0),  # greeks nedorazily → NULL
        }
    )
    archiver = OIArchiver(repository, fetcher, Settings())
    result = await archiver.archive_day(specs, DAY_1)
    assert result.written == 2

    chain = repository.chain_for_day("ES", DAY_1)
    by_strike = {(r.strike, r.right): r for r in chain}
    full = by_strike[(specs[0].strike, specs[0].right)]
    assert full.iv == 0.21
    assert full.delta == 0.55
    assert full.close_prem == 12.5
    assert full.und_price == 7500.25
    bare = by_strike[(specs[1].strike, specs[1].right)]
    assert bare.oi == 50.0
    assert bare.iv is None and bare.delta is None
