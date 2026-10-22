"""Testy OIArchiveru (issue #9): dva dny v archivu, idempotence, chybějící OI, real PG."""

import asyncio
import datetime as dt
import logging
import os
import time
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine

from gexlens_engine.config import Settings
from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.ibkr.mock import MockOIFetcher
from gexlens_engine.storage.oi_archive import (
    ContractSnapshot,
    OIArchiver,
    OIEodRepository,
    OIMiss,
    OIRecord,
    oi_eod_table,
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
    assert first.changed_values == len(specs)  # všechny hodnoty jsou nové
    assert repository.captured_at("ES", DAY_1) == ranni

    # Publikace doběhla — jiné hodnoty, tedy ještě není ustáleno
    po_publikaci = dt.datetime(2026, 7, 15, 12, 30, tzinfo=dt.UTC)
    archiver_new = OIArchiver(repository, MockOIFetcher(dict.fromkeys(specs, 1500.0)), Settings())
    second = await archiver_new.archive_day(specs, DAY_1, now=po_publikaci)

    assert second.changed is True
    assert (second.changed_values, second.missing_archived) == (len(specs), 0)
    assert repository.get_oi("ES", DAY_1, specs[0].strike, specs[0].right) == 1500.0
    assert repository.captured_at("ES", DAY_1) == po_publikaci

    # Třetí čtení dá totéž → ustáleno
    third = await archiver_new.archive_day(
        specs, DAY_1, now=dt.datetime(2026, 7, 15, 13, 0, tzinfo=dt.UTC)
    )

    assert third.changed is False
    assert (third.changed_values, third.missing_archived) == (0, 0)


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
    # Důvod nefinality do logu průchodu (#1650): hodnoty sedí, chybí archivovaný
    assert (result.changed_values, result.missing_archived) == (0, 1)


async def test_pruchod_bez_hodnot_spocita_chybejici_drive_archivovane(
    repository: OIEodRepository,
) -> None:
    """#1650 (D2): ani průchod bez jediného OI neztratí počet dřív archivovaných,
    které chybí — symbol pro srovnání s archivem dá chybějící kontrakt."""
    specs = contracts()
    archiver = OIArchiver(repository, MockOIFetcher(dict.fromkeys(specs, 500.0)), Settings())
    await archiver.archive_day(specs, DAY_1)

    result = await OIArchiver(repository, MockOIFetcher(), Settings()).archive_day(specs, DAY_1)

    assert result.written == 0
    assert (result.changed_values, result.missing_archived) == (0, len(specs))
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
    assert (result.changed_values, result.missing_archived) == (0, 0)


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

    # Přestavěný PK (#736): upsert přes 6sloupcový klíč musí na staré sqlite
    # tabulce projít — bez rebuild migrace by ON CONFLICT nenašel constraint
    record = OIRecord("ES", "20260716", 7600.0, "C", DAY_1, 200.0, trading_class="E1A")
    repo.upsert_many([record])
    repo.upsert_many([OIRecord("ES", "20260716", 7600.0, "C", DAY_1, 210.0, trading_class="E1A")])
    assert repo.get_oi("ES", DAY_1, 7600.0, "C") == 123.0 + 210.0  # '' + série, idempotentně


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
        ) -> ContractSnapshot | OIMiss:
            if spec == specs[0]:
                raise RuntimeError("mock: timeout")
            return ContractSnapshot(oi=42.0)

    archiver = OIArchiver(repository, ExplodingFetcher(), Settings())
    result = await archiver.archive_day(specs, DAY_1)

    assert result.written == 1
    assert result.missing == (specs[0],)


def _batch_lines(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if ": dávka " in r.getMessage()]


async def test_davka_loguje_pozici_serie_a_duvody_chybeni(
    repository: OIEodRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """#1650 (D1): řádek dávky nese pozici v průchodu, přečteno/celkem po sériích
    v pořadí průchodu a důvod každého chybějícího kontraktu."""
    caplog.set_level(logging.INFO, logger="gexlens_engine.storage.oi_archive")
    specs = contracts()
    next_series = OptionContractSpec(
        symbol="ES",
        sec_type="FOP",
        expiry="20260717",
        strike=7600.0,
        right="C",
        exchange="CME",
        trading_class="E3E",
        multiplier="50",
    )
    fetcher = MockOIFetcher(
        dict.fromkeys(specs[:3], 500.0),
        misses={specs[3]: OIMiss.NO_OI, specs[4]: OIMiss.UNQUALIFIED},
    )

    class ExplodingFetcher(MockOIFetcher):
        async def fetch_snapshot(
            self, spec: OptionContractSpec, timeout_s: float
        ) -> ContractSnapshot | OIMiss:
            if spec == specs[5]:
                raise RuntimeError("mock: chyba čtení")
            return await fetcher.fetch_snapshot(spec, timeout_s)

    archiver = OIArchiver(repository, ExplodingFetcher(), Settings())
    records, missing = await archiver.fetch_batch(
        [*specs, next_series], DAY_1, offset=80, total=1344
    )

    assert len(records) == 3
    assert missing == [*specs[3:], next_series]
    (line,) = [r.getMessage() for r in _batch_lines(caplog)]
    assert line.startswith(
        "OI archiv ES 2026-07-15: dávka 81–87/1344 (E3D 20260716 3/6, E3E 20260717 0/1) — "
        "přečteno 3, bez ticku 1, tick bez OI 1, nekvalifikováno 1, chyba 1; "
    )
    assert "lag smyčky max" in line


async def test_lag_smycky_zachyti_blokujici_praci_behem_davky(
    repository: OIEodRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """#1650, hypotéza 1: synchronní práce ve vlákně smyčky během dávky (ticky
    se mezitím nezpracují) se ukáže v lagu smyčky; dávka bez ní lag nemá."""
    caplog.set_level(logging.INFO, logger="gexlens_engine.storage.oi_archive")
    specs = contracts(count=2)

    class BlockingFetcher(MockOIFetcher):
        async def fetch_snapshot(
            self, spec: OptionContractSpec, timeout_s: float
        ) -> ContractSnapshot | OIMiss:
            await asyncio.sleep(0.05)
            time.sleep(0.6)  # CPU práce v témže vlákně jako smyčka
            return ContractSnapshot(oi=1.0)

    archiver = OIArchiver(repository, BlockingFetcher(), Settings())
    await archiver.fetch_batch(specs[:1], DAY_1, offset=0, total=2)
    quiet = OIArchiver(repository, MockOIFetcher(dict.fromkeys(specs, 1.0)), Settings())
    await quiet.fetch_batch(specs[1:], DAY_1, offset=1, total=2)

    lags = [r.args[-1] for r in _batch_lines(caplog) if isinstance(r.args, tuple)]
    assert len(lags) == 2 and all(isinstance(lag, float) for lag in lags)
    blocked, healthy = (float(cast(float, lag)) for lag in lags)
    assert blocked >= 0.3
    assert healthy < 0.1


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


# ── Původ hodnoty a stráž tasty zápisu (#1605) ─────────────────────

GUARD_EXPIRY = "20260716"
UTC = dt.UTC


def _row_state(
    engine: Engine, symbol: str, strike: float, right: str
) -> tuple[float, float | None, float | None, dt.datetime | None, dt.datetime | None]:
    """(OI, iv, close_prem, captured_ts, tasty_captured_ts) řádku; časy v UTC."""
    c = oi_eod_table.c
    with engine.connect() as conn:
        row = conn.execute(
            select(c.oi, c.iv, c.close_prem, c.captured_ts, c.tasty_captured_ts).where(
                c.symbol == symbol,
                c.expiry == GUARD_EXPIRY,
                c.strike == strike,
                c["right"] == right,
                c.date == DAY_1,
            )
        ).one()

    def utc(value: dt.datetime | None) -> dt.datetime | None:
        if value is None:
            return None
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    return (
        float(row.oi),
        row.iv,
        row.close_prem,
        utc(row.captured_ts),
        utc(row.tasty_captured_ts),
    )


def _assert_tasty_guard(repository: OIEodRepository, engine: Engine, symbol: str) -> None:
    """Scénář stráže `upsert_many` — sdílený sqlite i PostgreSQL variantou."""

    def record(strike: float, oi: float, **snapshot: float) -> OIRecord:
        return OIRecord(
            symbol, GUARD_EXPIRY, strike, "P", DAY_1, oi, trading_class="E3D", **snapshot
        )

    ibkr_at = dt.datetime(2026, 7, 15, 13, 1, tzinfo=UTC)
    midnight = dt.datetime(2026, 7, 15, 0, 0, tzinfo=UTC)
    after_pub = dt.datetime(2026, 7, 15, 13, 30, tzinfo=UTC)

    # A = IBKR řádek s greeks, B a C = tasty řádky z 00:00
    assert repository.upsert_many([record(7500.0, 100.0, iv=0.2, close_prem=5.0)], ibkr_at) == 1
    assert (
        repository.upsert_many(
            [record(7400.0, 250.0), record(7300.0, 300.0)], tasty_captured_ts=midnight
        )
        == 2
    )

    # Tasty po publikaci: A jiná hodnota (nedotknutelné), B stejná, C změna, D nový
    written = repository.upsert_many(
        [record(7500.0, 999.0), record(7400.0, 250.0), record(7300.0, 310.0), record(7200.0, 40.0)],
        tasty_captured_ts=after_pub,
    )

    assert written == 2  # C přepsán + D vložen; A ani B se nepočítají
    assert _row_state(engine, symbol, 7500.0, "P") == (100.0, 0.2, 5.0, ibkr_at, None)
    assert _row_state(engine, symbol, 7400.0, "P") == (250.0, None, None, None, midnight)
    assert _row_state(engine, symbol, 7300.0, "P") == (310.0, None, None, None, after_pub)
    assert _row_state(engine, symbol, 7200.0, "P") == (40.0, None, None, None, after_pub)

    # Tasty → IBKR: IBKR přepíše hodnotu i snímek, tasty čas zůstane (oba časy)
    ibkr_late = dt.datetime(2026, 7, 15, 14, 0, tzinfo=UTC)
    assert repository.upsert_many([record(7300.0, 320.0, iv=0.3)], ibkr_late) == 1
    assert _row_state(engine, symbol, 7300.0, "P") == (320.0, 0.3, None, ibkr_late, after_pub)
    # …a od té chvíle je řádek pro tasty nedotknutelný
    late = dt.datetime(2026, 7, 15, 15, 0, tzinfo=UTC)
    assert repository.upsert_many([record(7300.0, 330.0)], tasty_captured_ts=late) == 0
    assert _row_state(engine, symbol, 7300.0, "P")[0] == 320.0


def test_tasty_zapis_neprepise_ibkr_radek_a_nezmenenou_hodnotu(tmp_path: Path) -> None:
    """#1605: tasty upsert nad IBKR řádkem dřív přepsal OI a vynuloval greeks
    (řádek se pak dál tvářil jako IBKR měření). Stráž v SQL to drží i při souběhu."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'guard.db'}")
    repository = OIEodRepository(engine)
    repository.ensure_schema()
    _assert_tasty_guard(repository, engine, "ES")


@pytest.mark.skipif(
    not os.environ.get("GEXLENS_TEST_PG_DSN"),
    reason="GEXLENS_TEST_PG_DSN nenastaveno (integrace s reálným PostgreSQL)",
)
def test_tasty_straz_a_mereni_na_real_postgres() -> None:
    """Totéž na PG: počet zapsaných (RETURNING) nepočítá řádky,
    které stráž nepustila, a HAVING měřených stran projde i s timestamptz.
    Vlastní symboly, úklid jen vlastních řádků podle klíče."""
    engine = create_engine(os.environ["GEXLENS_TEST_PG_DSN"])
    repository = OIEodRepository(engine)
    repository.ensure_schema()
    guard_symbol, measured_symbol = "ZZ1605", "ZZ1605M"
    own_rows = oi_eod_table.delete().where(
        oi_eod_table.c.symbol.in_([guard_symbol, measured_symbol]),
        oi_eod_table.c.expiry == GUARD_EXPIRY,
        oi_eod_table.c.date == DAY_1,
    )
    with engine.begin() as conn:
        conn.execute(own_rows)  # zbytek po přerušeném běhu
    try:
        _assert_tasty_guard(repository, engine, guard_symbol)
        _assert_measured(repository, measured_symbol)
    finally:
        with engine.begin() as conn:
            conn.execute(own_rows)


async def test_tasty_radek_nebrani_finalite(repository: OIEodRepository) -> None:
    """#1605: kontrakt, který IBKR nedodá a archiv ho má jen z tasty, není
    „dřív archivované čtení" — finalitě nebrání (jinak by FA joby neběžely)."""
    specs = contracts()
    repository.upsert_many(
        [
            OIRecord(
                "ES",
                specs[0].expiry,
                specs[0].strike,
                specs[0].right,
                DAY_1,
                80.0,
                trading_class=specs[0].trading_class or "",
            )
        ],
        tasty_captured_ts=dt.datetime(2026, 7, 15, 0, 0, tzinfo=UTC),
    )
    archiver = OIArchiver(repository, MockOIFetcher(dict.fromkeys(specs[1:], 500.0)), Settings())

    await archiver.archive_day(specs, DAY_1)
    result = await archiver.archive_day(specs, DAY_1)

    assert specs[0] in result.missing
    assert result.changed is False  # dvě shodná IBKR čtení → ustáleno


async def test_prvni_ibkr_cteni_klice_s_tasty_hodnotou_je_zmena(
    repository: OIEodRepository,
) -> None:
    """#1605: tasty řádek se stejnou hodnotou není předchozí IBKR čtení — jinak
    by snímek vyšel finální po jediném IBKR čtení."""
    specs = contracts()
    repository.upsert_many(
        [
            OIRecord(
                "ES", s.expiry, s.strike, s.right, DAY_1, 500.0, trading_class=s.trading_class or ""
            )
            for s in specs
        ],
        tasty_captured_ts=dt.datetime(2026, 7, 15, 12, 5, tzinfo=UTC),
    )
    archiver = OIArchiver(repository, MockOIFetcher(dict.fromkeys(specs, 500.0)), Settings())

    first = await archiver.archive_day(specs, DAY_1)
    second = await archiver.archive_day(specs, DAY_1)

    assert first.changed is True
    assert second.changed is False


def test_snapshot_ibkr_only_vynecha_tasty_radky(repository: OIEodRepository) -> None:
    """Srovnání OI IBKR × tasty (#664) nesmí brát tasty řádek jako IBKR hodnotu."""
    repository.upsert_many(
        [OIRecord("ES", GUARD_EXPIRY, 7500.0, "C", DAY_1, 100.0)],
        dt.datetime(2026, 7, 15, 13, 1, tzinfo=UTC),
    )
    repository.upsert_many(
        [OIRecord("ES", GUARD_EXPIRY, 7400.0, "P", DAY_1, 250.0)],
        tasty_captured_ts=dt.datetime(2026, 7, 15, 13, 5, tzinfo=UTC),
    )

    assert repository.snapshot("ES", DAY_1, ibkr_only=True) == {(GUARD_EXPIRY, 7500.0, "C"): 100.0}
    assert len(repository.snapshot("ES", DAY_1)) == 2  # bez filtru beze změny


def test_measured_values_for_bere_jen_ibkr_mereni_po_prahu(repository: OIEodRepository) -> None:
    """#1605: strana se počítá, jen když všechny její řádky změřilo IBKR od prahu;
    s `include_tasty` (B+) i tasty řádek obnovený od prahu."""
    _assert_measured(repository, "ES")
    with pytest.raises(ValueError, match="časového pásma"):
        repository.measured_values_for(
            "ES", GUARD_EXPIRY, DAY_1, since=dt.datetime(2026, 7, 15, 12, 0)
        )


def _assert_measured(repository: OIEodRepository, symbol: str) -> None:
    """Scénář `measured_values_for` — sdílený sqlite i PostgreSQL variantou."""
    pub = dt.datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    midnight = dt.datetime(2026, 7, 15, 0, 0, tzinfo=UTC)

    def record(strike: float, oi: float, trading_class: str = "E3D") -> OIRecord:
        return OIRecord(symbol, GUARD_EXPIRY, strike, "C", DAY_1, oi, trading_class=trading_class)

    repository.upsert_many([record(7400.0, 10.0)], tasty_captured_ts=midnight)  # jen tasty
    repository.upsert_many(
        [record(7500.0, 20.0)], dt.datetime(2026, 7, 15, 5, 1, tzinfo=UTC)
    )  # IBKR před publikací
    repository.upsert_many([record(7600.0, 30.0)], tasty_captured_ts=midnight)
    repository.upsert_many([record(7600.0, 35.0)], pub + dt.timedelta(minutes=1))  # tasty → IBKR
    repository.upsert_many([record(7700.0, 40.0)], pub)  # IBKR přesně na prahu
    repository.upsert_many([record(7700.0, 4.0, "EW3")], pub)  # druhá série, změřená
    repository.upsert_many([record(7800.0, 50.0)], pub)
    repository.upsert_many([record(7800.0, 5.0, "EW3")], tasty_captured_ts=midnight)  # smíšené
    after_pub = pub + dt.timedelta(hours=8)  # večerní blok tasty
    repository.upsert_many([record(7900.0, 60.0)], tasty_captured_ts=after_pub)  # tasty po prahu
    repository.upsert_many([record(8000.0, 70.0)], pub)
    repository.upsert_many(
        [record(8000.0, 7.0, "EW3")], tasty_captured_ts=after_pub
    )  # smíšené, obě série změřené
    # IBKR čtení z cyklu před prahem přes tasty zápis po prahu: IBKR řádek se
    # posuzuje jen podle `captured_ts`, ani s `include_tasty` se nepočítá
    repository.upsert_many([record(8100.0, 80.0)], tasty_captured_ts=pub + dt.timedelta(minutes=1))
    repository.upsert_many([record(8100.0, 81.0)], pub - dt.timedelta(minutes=1))

    # Práh v jiném pásmu se převede na UTC (sqlite porovnává text)
    since = pub.astimezone(dt.timezone(dt.timedelta(hours=-5)))
    measured = repository.measured_values_for(symbol, GUARD_EXPIRY, DAY_1, since=since)
    with_tasty = repository.measured_values_for(
        symbol, GUARD_EXPIRY, DAY_1, since=since, include_tasty=True
    )

    assert {(r.strike, r.right): r.oi for r in measured} == {
        (7600.0, "C"): 35.0,
        (7700.0, "C"): 44.0,  # Σ přes série
    }
    # Tasty před prahem (7400, smíšené 7800) ani IBKR před prahem (7500, 8100) ne
    assert {(r.strike, r.right): r.oi for r in with_tasty} == {
        (7600.0, "C"): 35.0,
        (7700.0, "C"): 44.0,
        (7900.0, "C"): 60.0,
        (8000.0, "C"): 77.0,
    }


def test_archive_version_nese_posledni_zapis_obou_zdroju(repository: OIEodRepository) -> None:
    """#1605: klíč cache čtenářů se hne IBKR i tasty zápisem téže expirace, ne jiné."""
    assert repository.archive_version("ES", GUARD_EXPIRY, DAY_1) == (None, None)
    ibkr_at = dt.datetime(2026, 7, 15, 13, 1, tzinfo=UTC)
    repository.upsert_many([OIRecord("ES", GUARD_EXPIRY, 7500.0, "C", DAY_1, 100.0)], ibkr_at)
    tasty_at = dt.datetime(2026, 7, 15, 13, 5, tzinfo=UTC)
    repository.upsert_many(
        [OIRecord("ES", GUARD_EXPIRY, 7400.0, "P", DAY_1, 250.0)], tasty_captured_ts=tasty_at
    )
    # Zápis jiné expirace (sekundár) verzi nemění
    repository.upsert_many(
        [OIRecord("ES", "20260717", 7400.0, "P", DAY_1, 1.0)],
        tasty_captured_ts=dt.datetime(2026, 7, 15, 14, 0, tzinfo=UTC),
    )

    assert repository.archive_version("ES", GUARD_EXPIRY, DAY_1) == (ibkr_at, tasty_at)
