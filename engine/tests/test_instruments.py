"""Testy multi-instrument vrstvy (ADR-0003): plánování, watchlist, pipeline nad mocky."""

import asyncio
import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pandas as pd
import pytest
from sqlalchemy import create_engine, insert

from gexlens_engine.compute.cumdelta import CumDeltaTracker
from gexlens_engine.config import Settings
from gexlens_engine.ibkr.discovery import (
    ChainDiscovery,
    ExpiryInfo,
    OptionContractSpec,
    Underlying,
    build_contracts,
    select_band,
)
from gexlens_engine.ibkr.mock import MockIB, MockOIFetcher, MockQuoteStreamer
from gexlens_engine.ibkr.scheduler import SubscriptionScheduler, SweepMetrics
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.instruments import (
    SETUP_RETRY_CYCLES,
    SETUP_RETRY_FIRST_CYCLES,
    InstrumentPipeline,
    SetupCooldown,
    WatchlistReader,
    aggregate_status,
    gather_metrics,
    merge_symbols,
    parse_multiplier,
    plan_instruments,
)
from gexlens_engine.runtime import EngineRuntime, PublisherLike
from gexlens_engine.setups import SetupEngine
from gexlens_engine.storage.fa_calibration import FaAlphaRepository
from gexlens_engine.storage.meta import settings_table, watchlist_table
from gexlens_engine.storage.oi_archive import (
    ContractSnapshot,
    OIArchiver,
    OIEodRepository,
    OIRecord,
)
from gexlens_engine.storage.parquet_store import NetFlowRow, SnapshotWriter
from gexlens_engine.storage.setups_store import SetupsRepository

TS = dt.datetime(2026, 7, 17, 15, 0, tzinfo=dt.UTC)


# ── Čisté funkce ───────────────────────────────────────────────────


def test_parse_multiplier() -> None:
    assert parse_multiplier("50") == 50.0
    assert parse_multiplier("20") == 20.0
    assert parse_multiplier("") == 1.0
    assert parse_multiplier(None) == 1.0
    assert parse_multiplier("nesmysl") == 1.0  # varování, engine nesmí spadnout


def test_merge_symbols_dedupe_uppercase_base_first() -> None:
    assert merge_symbols(["ES"], ["nq", "ES", " cl "]) == ["ES", "NQ", "CL"]
    assert merge_symbols(["ES", "NQ"], []) == ["ES", "NQ"]
    assert merge_symbols([], ["es"]) == ["ES"]


def test_expiry_expired_roll() -> None:
    from gexlens_engine.instruments import expiry_expired

    today = dt.date(2026, 7, 18)
    assert expiry_expired("20260717", today) is True  # včerejší 0DTE → roll
    assert expiry_expired("20260718", today) is False  # dnešní žije
    assert expiry_expired("20260720", today) is False
    assert expiry_expired("nesmysl", today) is False  # nečitelný formát neshazuje běh


def test_plan_instruments_start_stop_and_cap() -> None:
    plan = plan_instruments(running=["ES", "NQ"], desired=["ES", "CL"], max_instruments=3)
    assert plan.start == ["CL"]
    assert plan.stop == ["NQ"]
    assert plan.skipped == []

    # Strop: priorita = pořadí v desired (základ z konfigurace první)
    capped = plan_instruments(running=[], desired=["ES", "NQ", "CL", "GC"], max_instruments=2)
    assert capped.start == ["ES", "NQ"]
    assert capped.skipped == ["CL", "GC"]

    # Instrument nad stropem, který běžel, se zastaví
    over = plan_instruments(running=["GC"], desired=["ES", "NQ", "GC"], max_instruments=2)
    assert over.stop == ["GC"]


# ── Cooldown setupu (#455) ─────────────────────────────────────────


def test_setup_cooldown_blokuje_az_do_vyprseni() -> None:
    cooldown = SetupCooldown(max_cycles=8, first_cycles=3)
    cooldown.penalize("ES")

    assert cooldown.blocked("ES")
    assert not cooldown.blocked("NQ")  # netrestaný symbol jde založit hned

    for _ in range(2):
        cooldown.tick()
        assert cooldown.blocked("ES")

    cooldown.tick()
    assert not cooldown.blocked("ES")


def test_setup_cooldown_eskaluje_a_zastavi_se_na_stropu() -> None:
    """#457: dočasná příčina stojí 1 cyklus, trvale vadný symbol se utlumí na strop."""
    cooldown = SetupCooldown(max_cycles=8, first_cycles=1)

    assert cooldown.penalize("ES") == 1
    assert cooldown.penalize("ES") == 2
    assert cooldown.penalize("ES") == 4
    assert cooldown.penalize("ES") == 8
    assert cooldown.penalize("ES") == 8  # strop se nepřekročí


def test_setup_cooldown_prvni_odklad_pousti_hned_dalsi_cyklus() -> None:
    """Regrese #457: po jednom selhání se musí zkusit znovu za minutu, ne za 30."""
    cooldown = SetupCooldown()

    assert cooldown.penalize("ES") == SETUP_RETRY_FIRST_CYCLES
    assert cooldown.blocked("ES")

    cooldown.tick()

    assert not cooldown.blocked("ES")


def test_setup_cooldown_uspech_nuluje_eskalaci() -> None:
    cooldown = SetupCooldown(max_cycles=8, first_cycles=1)
    cooldown.penalize("ES")
    cooldown.penalize("ES")  # eskalováno na 4

    cooldown.succeeded("ES")

    assert not cooldown.blocked("ES")
    assert cooldown.penalize("ES") == 1  # série začíná znovu od nejkratšího


def test_setup_cooldown_eskalace_je_per_symbol() -> None:
    cooldown = SetupCooldown(max_cycles=8, first_cycles=1)
    cooldown.penalize("ES")
    cooldown.penalize("ES")

    assert cooldown.penalize("NQ") == 1  # cizí série NQ netrestá


def test_setup_cooldown_clear_uvolni_vse_a_hlasi_symboly() -> None:
    """Regrese #455: po reconnectu musí pipeline naskočit hned, ne za 30 minut."""
    cooldown = SetupCooldown(max_cycles=SETUP_RETRY_CYCLES)
    cooldown.penalize("ES")
    cooldown.penalize("ES")  # eskalovaná série ze starého spojení
    cooldown.penalize("NQ")

    released = cooldown.clear()

    assert set(released) == {"ES", "NQ"}  # volající to loguje
    assert not cooldown.blocked("ES")
    assert not cooldown.blocked("NQ")
    assert cooldown.clear() == ()  # opakované volání je no-op
    assert cooldown.penalize("ES") == SETUP_RETRY_FIRST_CYCLES  # eskalace taky pryč


def test_setup_cooldown_tick_na_prazdnem_nespadne() -> None:
    cooldown = SetupCooldown()
    cooldown.tick()
    assert not cooldown.blocked("ES")


# ── Watchlist z DB ─────────────────────────────────────────────────


def test_watchlist_reader_roundtrip(tmp_path: Path) -> None:
    db = create_engine(f"sqlite+pysqlite:///{tmp_path / 'meta.sqlite'}")
    reader = WatchlistReader(db)
    reader.ensure_schema()
    assert reader.symbols() == []  # prázdná tabulka, žádná chyba
    assert reader.setting("strike_range_points") is None

    with db.begin() as conn:
        conn.execute(insert(watchlist_table).values(symbol="ES"))
        conn.execute(insert(watchlist_table).values(symbol="NQ"))
        conn.execute(insert(settings_table).values(key="strike_range_points", value=400))
    assert reader.symbols() == ["ES", "NQ"]
    assert reader.setting("strike_range_points") == 400


def test_oi_prev_day_queries(tmp_path: Path) -> None:
    """ΔOI vs. včera: poslední archivovaný den před datem + hodnoty daného dne."""
    repository = OIEodRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'oi.sqlite'}"))
    repository.ensure_schema()
    day1, day2 = dt.date(2026, 7, 16), dt.date(2026, 7, 17)
    repository.upsert_many([OIRecord("ES", "20260717", 7500.0, "P", day1, 100.0)])
    repository.upsert_many([OIRecord("ES", "20260717", 7500.0, "P", day2, 150.0)])

    assert repository.latest_day_before("ES", "20260717", day2) == day1
    assert repository.latest_day_before("ES", "20260717", day1) is None
    values = repository.values_for("ES", "20260717", day1)
    assert len(values) == 1
    assert values[0].strike == 7500.0 and values[0].right == "P" and values[0].oi == 100.0


# ── Pipeline nad mocky ─────────────────────────────────────────────


class RecordingPublisher(PublisherLike):
    def __init__(self) -> None:
        self.statuses: list[dict[str, object]] = []
        self.messages: list[tuple[str, dict[str, object]]] = []

    async def status(self, **fields: object) -> None:
        self.statuses.append(fields)

    async def publish(self, channel: str, data: dict[str, object]) -> None:
        self.messages.append((channel, data))


class FakeTicker:
    def __init__(self, last: float) -> None:
        self.last = last

    def marketPrice(self) -> float:
        return self.last


def make_pipeline(
    symbol: str,
    spot: float,
    settings: Settings,
    writer: SnapshotWriter,
    oi_repository: OIEodRepository,
    publisher: RecordingPublisher,
    *,
    oi_available: bool = True,
) -> InstrumentPipeline:
    strikes = tuple(spot + offset for offset in (-10.0, 0.0, 10.0))
    info = ExpiryInfo(
        trading_class=f"{symbol}0",
        expiry="20260717",
        exchange="CME",
        multiplier="50",
        strikes=strikes,
    )
    underlying = Underlying(symbol=symbol, sec_type="FUT", exchange="CME", con_id=1)
    band = select_band(info.strikes, spot, settings.strike_range_points)
    contracts = build_contracts(underlying, info, band)
    oi_repository.upsert_many(
        [OIRecord(symbol, info.expiry, c.strike, c.right, TS.date(), 500.0) for c in contracts]
    )
    runtime = EngineRuntime(
        settings=settings,
        scheduler=SubscriptionScheduler(MockQuoteStreamer(), settings),
        writer=writer,
        oi_repository=oi_repository,
        publisher=publisher,
        symbol=symbol,
        expiry=info.expiry,
        multiplier=50.0,
        contracts=contracts,
        cum_delta=CumDeltaTracker(multiplier=50.0),
        push_status=False,
    )
    return InstrumentPipeline(
        symbol=symbol,
        settings=settings,
        publisher=publisher,
        discovery=ChainDiscovery(MockIB(), settings),
        info=info,
        band=band,
        runtime=runtime,
        archiver=OIArchiver(oi_repository, MockOIFetcher(), settings),
        oi_repository=oi_repository,
        ticker=FakeTicker(spot),
        minute_bars=[],
        spot=spot,
        oi_available=oi_available,
    )


async def test_rozsirena_obalka_doarchivuje_nove_striky(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#465: 4. 8. vyjelo NQ 11 striků nad archivované pásmo a všechny měly v grafu nulu."""
    settings, writer, repository, publisher = env
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    today = TS.date()
    puvodni = list(pipeline.runtime.contracts)

    # Cena utekla nahoru → obálka se rozšířila o strike, který archiv nezná
    novy = OptionContractSpec("ES", "FOP", "20260717", 7650.0, "C", "CME", "ES0", "50")
    pipeline.runtime.contracts = [*puvodni, novy]
    pipeline.archiver = OIArchiver(repository, MockOIFetcher({novy: 321.0}), settings)

    await pipeline._archive_new_strikes(puvodni, pipeline.runtime.contracts, today)

    assert repository.get_oi("ES", today, 7650.0, "C") == 321.0
    # Původní striky se nepřepisují — doarchivace se týká jen přírůstku
    assert repository.get_oi("ES", today, puvodni[0].strike, puvodni[0].right) == 500.0


async def test_doarchivace_bez_pridanych_striku_nedela_nic(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    settings, writer, repository, publisher = env
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)

    class ExplodingArchiver(OIArchiver):
        async def archive_day(self, contracts, day, now=None):  # type: ignore[no-untyped-def]
            raise AssertionError("archivace se nesmí volat, když nic nepřibylo")

    pipeline.archiver = ExplodingArchiver(repository, MockOIFetcher(), settings)

    await pipeline._archive_new_strikes(
        list(pipeline.runtime.contracts), pipeline.runtime.contracts, TS.date()
    )


def test_oi_refresh_je_potreba_az_po_publikacnim_okne(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#463: před oknem se nečte (data nejsou), po okně dokud není potvrzeno."""
    settings, writer, repository, publisher = env
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    okno = settings.oi_publication_utc(dt.date(2026, 8, 4))

    pred_oknem = okno - dt.timedelta(minutes=90)
    po_okne = okno + dt.timedelta(minutes=5)

    assert pipeline._oi_refresh_due(pred_oknem) is False
    assert pipeline._oi_refresh_due(po_okne) is True

    # Potvrzený snímek se už neobnovuje
    pipeline.oi_final = True
    assert pipeline._oi_refresh_due(po_okne) is False

    # #511: okno je burzovní čas (7:00 America/Chicago) — v zimě 13:00 UTC,
    # takže 12:30 UTC je PŘED oknem, ačkoli v létě by už bylo po něm
    pipeline.oi_final = False
    assert pipeline._oi_refresh_due(dt.datetime(2026, 1, 15, 12, 30, tzinfo=dt.UTC)) is False
    assert pipeline._oi_refresh_due(dt.datetime(2026, 1, 15, 13, 5, tzinfo=dt.UTC)) is True


async def test_predpublikacni_snimek_se_po_okne_prepise(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """Regrese #463: 4. 8. 2026 držel půlnoční snímek celý den (put Σ OI 1 877 vs 29 282 call)."""
    settings, writer, repository, publisher = env
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    today = TS.date()
    specs = pipeline.runtime.contracts

    # Půlnoční archivace uspěla, ale s předpublikačními čísly
    repository.upsert_many(
        [OIRecord("ES", spec.expiry, spec.strike, spec.right, today, 2.0) for spec in specs],
        dt.datetime(2026, 8, 4, 0, 5, tzinfo=dt.UTC),
    )
    assert today in repository.days("ES")

    # Po okně dodá IBKR kompletní hodnoty — mock fetcher vrací 500.0
    pipeline.archiver = OIArchiver(repository, MockOIFetcher(dict.fromkeys(specs, 500.0)), settings)
    po_okne = settings.oi_publication_utc(dt.date(2026, 8, 4)) + dt.timedelta(minutes=5)
    assert await pipeline.try_archive_oi(today, po_okne) is True

    assert repository.get_oi("ES", today, specs[0].strike, specs[0].right) == 500.0
    assert pipeline.oi_final is False  # jedno čtení po okně nestačí

    # Druhé čtení dá totéž → snímek je potvrzený a dál se neobnovuje
    assert await pipeline.try_archive_oi(today, po_okne) is True
    assert pipeline.oi_final is True

    # A před oknem se předpublikační snímek nepřepisuje (data ještě nejsou)
    pipeline.oi_final = False
    pipeline.archiver = OIArchiver(repository, MockOIFetcher(dict.fromkeys(specs, 9.0)), settings)
    pred_oknem = settings.oi_publication_utc(dt.date(2026, 8, 4)) - dt.timedelta(hours=2)
    assert await pipeline.try_archive_oi(today, pred_oknem) is True
    assert repository.get_oi("ES", today, specs[0].strike, specs[0].right) == 500.0


async def test_ridky_snimek_nesmi_byt_finalni(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#664: 12. 8. dvě shodná čtení 4 kontraktů ze 160 (CME 0DTE ještě
    nepublikoval) prohlásila snímek za finální a obnova stála celý den."""
    settings, writer, repository, publisher = env
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    today = TS.date()
    specs = list(pipeline.runtime.contracts)
    po_okne = settings.oi_publication_utc(today) + dt.timedelta(minutes=5)

    # OI dodají jen 2 kontrakty — dvě shodná čtení, ale řídké pokrytí
    pipeline.archiver = OIArchiver(
        repository, MockOIFetcher(dict.fromkeys(specs[:2], 40.0)), settings
    )
    assert await pipeline.try_archive_oi(today, po_okne) is True
    assert await pipeline.try_archive_oi(today, po_okne) is True
    assert pipeline.oi_final is False  # pokrytí pod prahem — obnova musí běžet dál
    assert pipeline._oi_refresh_due(po_okne) is True

    # Publikace doběhla → plné čtení; dvě shodná čtení teprve teď finalizují
    pipeline.archiver = OIArchiver(repository, MockOIFetcher(dict.fromkeys(specs, 500.0)), settings)
    assert await pipeline.try_archive_oi(today, po_okne) is True
    assert pipeline.oi_final is False  # první plné čtení se od řídkého liší
    assert await pipeline.try_archive_oi(today, po_okne) is True
    assert pipeline.oi_final is True


async def test_obnova_po_okne_cte_i_striky_pridane_expanzi(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#494 (1): obálka se rozšíří před publikačním oknem — obnova po okně musí
    přečíst i nové striky, ne jen statický snímek `archive_contracts`."""
    settings, writer, repository, publisher = env
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    today = TS.date()
    puvodni = list(pipeline.runtime.contracts)
    pipeline.archive_contracts = puvodni  # statický snímek z doby založení pipeline

    # Expanze před oknem: nový strike se doarchivuje předpublikačními čísly
    novy = OptionContractSpec("ES", "FOP", "20260717", 7650.0, "C", "CME", "ES0", "50")
    pipeline.runtime.contracts = [*puvodni, novy]
    pipeline.archiver = OIArchiver(repository, MockOIFetcher({novy: 2.0}), settings)
    await pipeline._archive_new_strikes(puvodni, pipeline.runtime.contracts, today)
    assert repository.get_oi("ES", today, 7650.0, "C") == 2.0

    # Po okně dodá IBKR finální čísla — obnova musí pokrýt i strike z expanze
    vsechny = list(pipeline.runtime.contracts)
    pipeline.archiver = OIArchiver(
        repository, MockOIFetcher(dict.fromkeys(vsechny, 500.0)), settings
    )
    po_okne = settings.oi_publication_utc(dt.date(2026, 7, 17)) + dt.timedelta(minutes=5)
    assert await pipeline.try_archive_oi(today, po_okne) is True

    assert repository.get_oi("ES", today, 7650.0, "C") == 500.0


async def test_expanze_sekundaru_doarchivuje_nove_striky(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#494 (2): expanze obálky sekundární expirace musí nové striky doarchivovat
    stejně jako aktivní řetěz — jinak mají `get_oi` None celý den."""
    settings, writer, repository, publisher = env
    spot = 7500.0
    pipeline = make_pipeline("ES", spot, settings, writer, repository, publisher)

    strikes = tuple(spot + offset for offset in range(-400, 401, 10))
    next_info = ExpiryInfo(
        trading_class="ES1",
        expiry="20260718",
        exchange="CME",
        multiplier="50",
        strikes=strikes,
    )
    next_band = select_band(next_info.strikes, spot, settings.strike_range_points)
    underlying = Underlying(symbol="ES", sec_type="FUT", exchange="CME", con_id=1)
    pipeline.next_info = next_info
    pipeline.next_band = next_band
    pipeline.next_runtime = EngineRuntime(
        settings=settings,
        scheduler=SubscriptionScheduler(MockQuoteStreamer(), settings),
        writer=writer,
        oi_repository=repository,
        publisher=publisher,
        symbol="ES",
        expiry=next_info.expiry,
        multiplier=50.0,
        contracts=build_contracts(underlying, next_info, next_band),
        cum_delta=CumDeltaTracker(multiplier=50.0),
        push_status=False,
        secondary=True,
    )
    original_high = next_band.high

    class ConstOIFetcher(MockOIFetcher):
        """OI pro libovolný kontrakt — nové striky vzniknou až expanzí."""

        async def fetch_snapshot(
            self, spec: OptionContractSpec, timeout_s: float
        ) -> ContractSnapshot | None:
            return ContractSnapshot(oi=77.0)

    pipeline.archiver = OIArchiver(repository, ConstOIFetcher(), settings)

    await pipeline._expand_secondary(spot=original_high - 5.0, today=TS.date())

    assert pipeline.next_runtime is not None
    nejvyssi = max(c.strike for c in pipeline.next_runtime.contracts)
    assert nejvyssi > original_high
    assert repository.get_oi("ES", TS.date(), nejvyssi, "C", expiry="20260718") == 77.0


async def test_pulnoc_resetuje_finalitu_a_archivuje_novy_den(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#494 (3): pipeline s nedenní expirací přežije půlnoc — včerejší `oi_final`
    nesmí blokovat archivaci nového dne."""
    settings, writer, repository, publisher = env
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    specs = list(pipeline.runtime.contracts)
    pipeline.archiver = OIArchiver(repository, MockOIFetcher(dict.fromkeys(specs, 500.0)), settings)
    day1 = TS.date()

    # Den 1: čtení po okně shodné s archivem (make_pipeline uložil 500.0) → finální
    po_okne = settings.oi_publication_utc(dt.date(2026, 7, 17)) + dt.timedelta(minutes=5)
    assert await pipeline.try_archive_oi(day1, po_okne) is True
    assert pipeline.oi_final is True

    # Přechod přes půlnoc: finalita se resetuje a nový den se archivuje hned
    pulnoc = dt.datetime(2026, 7, 18, 0, 0, tzinfo=dt.UTC)
    await pipeline.run_minute(pulnoc)

    assert pipeline.oi_final is False  # finalita patřila včerejšku
    assert pipeline.oi_available is True
    assert dt.date(2026, 7, 18) in repository.days("ES")  # nový den archivovaný


async def test_neuspesna_obnova_jede_na_starsim_snimku(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#494 (5): výpadek fetche při post-publikační obnově nesmí shodit
    `oi_available` ani hlásit „bez OI" — platný denní archiv existuje."""
    settings, writer, repository, publisher = env
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    today = TS.date()
    po_okne = settings.oi_publication_utc(dt.date(2026, 7, 17)) + dt.timedelta(minutes=5)

    class ExplodingArchiver:
        async def archive_day(self, contracts: object, day: dt.date, now: object = None) -> object:
            raise RuntimeError("mock: přechodný výpadek IBKR")

    pipeline.archiver = ExplodingArchiver()  # type: ignore[assignment]

    assert await pipeline.try_archive_oi(today, po_okne) is True  # starší snímek platí dál
    alerts = [d for ch, d in publisher.messages if ch == "alerts"]
    assert alerts[-1]["kind"] == "oi_refresh_failed"
    assert "starším snímku" in str(alerts[-1]["message"])
    assert pipeline.oi_final is False  # obnova se zopakuje dalším retry cyklem

    # Totéž pro fetch, který nespadne, ale nic nevrátí (written == 0)
    pipeline.archiver = OIArchiver(repository, MockOIFetcher(), settings)
    assert await pipeline.try_archive_oi(today, po_okne) is True
    alerts = [d for ch, d in publisher.messages if ch == "alerts"]
    assert alerts[-1]["kind"] == "oi_refresh_failed"


@pytest.fixture
def env(tmp_path: Path) -> tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher]:
    settings = Settings(data_dir=tmp_path / "data")
    repository = OIEodRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'db.sqlite'}"))
    repository.ensure_schema()
    return settings, SnapshotWriter(settings), repository, RecordingPublisher()


async def test_two_pipelines_write_separate_symbol_partitions(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    settings, writer, repository, publisher = env
    es = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    nq = make_pipeline("NQ", 24000.0, settings, writer, repository, publisher)

    results = await gather_metrics([es, nq], TS)

    day = TS.date().isoformat()
    es_rows = pd.read_parquet(settings.snapshots_dir / "ES" / "20260717" / f"{day}.parquet")
    nq_rows = pd.read_parquet(settings.snapshots_dir / "NQ" / "20260717" / f"{day}.parquet")
    assert len(es_rows) == 6 and len(nq_rows) == 6
    assert es_rows["oi"].iloc[0] == 500.0

    # Agregovaný status: součty přes instrumenty
    status = aggregate_status(results)
    assert status["greeks_total"] == 12
    assert status["greeks_complete"] == 12
    assert status["symbols"] == "ES,NQ"

    # Live kanály per symbol
    channels = [channel for channel, _ in publisher.messages]
    assert "levels.ES.20260717" in channels
    assert "levels.NQ.20260717" in channels


async def test_pipeline_failure_does_not_stop_others(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    settings, writer, repository, publisher = env
    healthy = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    broken = make_pipeline("NQ", 24000.0, settings, writer, repository, publisher)

    async def boom(now: dt.datetime) -> SweepMetrics:
        raise RuntimeError("simulovaný pád")

    broken.run_minute = boom  # type: ignore[method-assign]

    results = await gather_metrics([broken, healthy], TS)
    assert results[0] == ("NQ", None)
    assert results[1][0] == "ES" and results[1][1] is not None
    status = aggregate_status(results)
    assert status["greeks_total"] == 6  # jen zdravý instrument


async def test_next_expiry_secondary_runtime(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """Sekundární řetěz: snapshots+levels své expirace, žádný flow/bary, kadence 1/k."""
    settings, writer, repository, publisher = env
    settings.next_expiry_sweep_every = 2
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)

    next_info = ExpiryInfo(
        trading_class="EW1",
        expiry="20260720",
        exchange="CME",
        multiplier="50",
        strikes=(7590.0, 7600.0, 7610.0),
    )
    underlying = Underlying(symbol="ES", sec_type="FUT", exchange="CME", con_id=1)
    next_band = select_band(next_info.strikes, 7600.0, settings.strike_range_points)
    pipeline.next_runtime = EngineRuntime(
        settings=settings,
        scheduler=SubscriptionScheduler(MockQuoteStreamer(), settings),
        writer=writer,
        oi_repository=repository,
        publisher=publisher,
        symbol="ES",
        expiry=next_info.expiry,
        multiplier=50.0,
        contracts=build_contracts(underlying, next_info, next_band),
        push_status=False,
        secondary=True,
    )

    for minute in range(3):  # kadence 1/2 → sekundární běží v minutách 0 a 2
        await pipeline.run_minute(TS + dt.timedelta(minutes=minute))

    day = TS.date().isoformat()
    primary = pd.read_parquet(settings.snapshots_dir / "ES" / "20260717" / f"{day}.parquet")
    secondary = pd.read_parquet(settings.snapshots_dir / "ES" / "20260720" / f"{day}.parquet")
    assert len(primary) == 18  # 3 minuty × 6 kontraktů
    assert len(secondary) == 12  # 2 běhy × 6 kontraktů
    levels_next = pd.read_parquet(
        settings.derived_dir / "ES" / "20260720" / "levels" / f"{day}.parquet"
    )
    assert len(levels_next) == 2
    # Flow patří jen aktivní expiraci — 3 řádky (žádná duplikace sekundárním během)
    flow = pd.read_parquet(settings.derived_dir / "ES" / "flow" / f"{day}.parquet")
    assert len(flow) == 3
    channels = [channel for channel, _ in publisher.messages]
    assert "levels.ES.20260720" in channels


async def test_oi_missing_alert_and_retry_counter(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    settings, writer, repository, publisher = env
    # Jiný den než upsert v make_pipeline → OI archiv pro dnešek chybí
    pipeline = make_pipeline("CL", 80.0, settings, writer, repository, publisher)

    ok = await pipeline.try_archive_oi(dt.date(2026, 7, 18))  # MockOIFetcher bez hodnot
    assert ok is False
    alerts = [data for channel, data in publisher.messages if channel == "alerts"]
    assert alerts and alerts[-1]["kind"] == "oi_missing"
    assert alerts[-1]["symbol"] == "CL"


async def test_watchdog_prerusi_visici_cyklus(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#219: cyklus visící na mrtvém IBKR await nesmí zastavit orchestrátor."""
    settings, writer, repository, publisher = env
    stuck = make_pipeline("NQ", 24000.0, settings, writer, repository, publisher)
    healthy = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)

    async def hang(now: dt.datetime) -> SweepMetrics:
        await asyncio.sleep(3600)
        raise AssertionError("nedosažitelné")

    stuck.run_minute = hang  # type: ignore[method-assign]

    results = dict(await gather_metrics([stuck, healthy], TS, timeout_s=0.2))

    assert results["NQ"] is None  # timeout → neúspěšný cyklus, žádné viset
    assert results["ES"] is not None  # smyčka pokračovala dalším instrumentem


async def test_bars_stall_alert_and_recovery_backfill(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#221: bary nechodí při živém spotu → alert; po návratu recovery + re-backfill."""
    settings, writer, repository, publisher = env
    settings.bars_stall_alert_minutes = 2
    settings.level_alert_near_steps = 0.0  # zdi fixture jsou u spotu — nešumět (#675)
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    backfills: list[bool] = []

    async def fake_backfill() -> None:
        backfills.append(True)

    pipeline.backfill_today = fake_backfill
    ticker = pipeline.ticker
    assert isinstance(ticker, FakeTicker)

    # Cyklus 0: první spot (žádný předchozí) → pohyb neznámý, čítač stojí
    await pipeline.run_minute(TS)
    # Cykly 1–2: spot žije, bary žádné → po prahu 2 min alert
    ticker.last = 7601.0
    await pipeline.run_minute(TS + dt.timedelta(minutes=1))
    ticker.last = 7602.0
    await pipeline.run_minute(TS + dt.timedelta(minutes=2))

    alerts = [data for channel, data in publisher.messages if channel == "alerts"]
    assert [a["kind"] for a in alerts] == ["bars_stalled"]
    assert alerts[0]["symbol"] == "ES"

    # Další tichý cyklus alert neopakuje (anti-spam)
    ticker.last = 7603.0
    await pipeline.run_minute(TS + dt.timedelta(minutes=3))
    alerts = [data for channel, data in publisher.messages if channel == "alerts"]
    assert len(alerts) == 1

    # Návrat barů: recovery alert + re-backfill dnešního dne na pozadí
    ticker.last = 7604.0
    now = TS + dt.timedelta(minutes=4)
    pipeline.minute_bars.append(
        Bar(ts=now, open=7603.0, high=7605.0, low=7602.0, close=7604.0, volume=10.0)
    )
    await pipeline.run_minute(now)
    assert pipeline._backfill_task is not None
    await pipeline._backfill_task

    alerts = [data for channel, data in publisher.messages if channel == "alerts"]
    assert [a["kind"] for a in alerts] == ["bars_stalled", "bars_recovered"]
    assert backfills == [True]


async def test_strikes_stalled_alert_a_recovery(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#547: repair kola nad prahem → alert strikes_stalled s hintem; návrat → recovery."""
    settings, writer, repository, publisher = env
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)

    def metrics(stalled: int) -> SweepMetrics:
        return SweepMetrics(
            total=96,
            greeks_complete=96 - stalled,
            repair_count=stalled,
            stale_count=0,
            sweep_duration_s=1.0,
            computed_greeks=stalled,
            stalled_count=stalled,
        )

    await pipeline._watch_repair(TS, metrics(74))
    alerts = [data for channel, data in publisher.messages if channel == "alerts"]
    assert [a["kind"] for a in alerts] == ["strikes_stalled"]
    assert alerts[0]["symbol"] == "ES"
    assert "restart TWS" in str(alerts[0]["message"])

    # Stav trvá → žádný další alert (anti-spam)
    await pipeline._watch_repair(TS + dt.timedelta(minutes=1), metrics(74))
    alerts = [data for channel, data in publisher.messages if channel == "alerts"]
    assert len(alerts) == 1

    # TWS zase dodává → recovery právě jednou
    await pipeline._watch_repair(TS + dt.timedelta(minutes=2), metrics(0))
    await pipeline._watch_repair(TS + dt.timedelta(minutes=3), metrics(0))
    alerts = [data for channel, data in publisher.messages if channel == "alerts"]
    assert [a["kind"] for a in alerts] == ["strikes_stalled", "strikes_recovered"]


async def test_no_stall_alert_when_market_quiet(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#221: zavřený trh (spot stojí) — chybějící bary nesmí spouštět alert."""
    settings, writer, repository, publisher = env
    settings.bars_stall_alert_minutes = 2
    settings.level_alert_near_steps = 0.0  # zdi fixture jsou u spotu — nešumět (#675)
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)

    for minute in range(5):  # FakeTicker drží stejnou cenu → spot se nehýbe
        await pipeline.run_minute(TS + dt.timedelta(minutes=minute))

    assert not [data for channel, data in publisher.messages if channel == "alerts"]


async def test_level_proximity_alert_z_pipeline(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#675: zdi fixture leží krok striků od spotu → alert vystřelí, ale jen jednou."""
    settings, writer, repository, publisher = env
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)

    await pipeline.run_minute(TS)
    alerts = [data for channel, data in publisher.messages if channel == "alerts"]
    assert alerts and all(a["kind"] == "level_proximity" for a in alerts)
    assert all(a["symbol"] == "ES" for a in alerts)

    # Cena zůstává v zóně → další minuty mlčí (re-arm hystereze)
    await pipeline.run_minute(TS + dt.timedelta(minutes=1))
    assert len([data for channel, data in publisher.messages if channel == "alerts"]) == len(alerts)


async def test_vol_concentration_alert_once_per_leader(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#208: dominantní strana příští expirace → jeden alert; nový leader → další."""
    from types import SimpleNamespace

    settings, writer, repository, publisher = env
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)

    def sec_spec(strike: float, right: str) -> OptionContractSpec:
        return OptionContractSpec("ES", "FOP", "20260718", strike, right, "CME", "E4D", "50")

    def cached(volume: float) -> SimpleNamespace:
        return SimpleNamespace(snapshot=SimpleNamespace(volume=volume))

    quotes = {
        sec_spec(7450.0, "P"): cached(4100),
        sec_spec(7500.0, "P"): cached(900),
        sec_spec(7580.0, "C"): cached(800),
        sec_spec(7400.0, "P"): cached(700),
    }
    pipeline.next_runtime = SimpleNamespace(  # type: ignore[assignment]
        expiry="20260718",
        scheduler=SimpleNamespace(quotes=lambda: quotes),
        # Aktivní zdroj řetězu (#614 fáze 2b): bez fallbacku je to sweep cache
        current_quotes=lambda: quotes,
    )

    await pipeline._check_vol_concentration(TS)
    await pipeline._check_vol_concentration(TS)  # anti-spam: týž leader jen jednou

    alerts = [
        data
        for channel, data in publisher.messages
        if channel == "alerts" and data["kind"] == "vol_concentration"
    ]
    assert len(alerts) == 1
    first = str(alerts[0]["message"])
    assert "7450P" in first
    assert "pojistka" in first  # put pod spotem 7600 → dovětek

    quotes[sec_spec(7650.0, "C")] = cached(20000)  # nový dominantní leader
    await pipeline._check_vol_concentration(TS)
    alerts = [
        data
        for channel, data in publisher.messages
        if channel == "alerts" and data["kind"] == "vol_concentration"
    ]
    assert len(alerts) == 2
    second = str(alerts[1]["message"])
    assert "7650C" in second
    assert "strop" in second  # call nad spotem → dovětek


async def test_archive_failure_does_not_kill_pipeline(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#215 (MES): výjimka z archivace → alert + False, žádné probublání do cooldownu."""
    settings, writer, repository, publisher = env
    pipeline = make_pipeline("MES", 7500.0, settings, writer, repository, publisher)

    class ExplodingArchiver:
        async def archive_day(self, contracts: object, day: dt.date) -> object:
            raise RuntimeError("mock: CardinalityViolation")

    pipeline.archiver = ExplodingArchiver()  # type: ignore[assignment]

    ok = await pipeline.try_archive_oi(dt.date(2026, 7, 18))

    assert ok is False  # pipeline žije dál, retry po OI_RETRY_CYCLES
    alerts = [data for channel, data in publisher.messages if channel == "alerts"]
    assert alerts and alerts[-1]["kind"] == "oi_missing"
    assert alerts[-1]["symbol"] == "MES"


async def test_setup_selfcheck_alerts_on_drawdown_and_recovers(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher], tmp_path: Path
) -> None:
    """#309: prodělávající detektor se ozve sám; zdravý stav zvonek neruší."""
    settings, writer, repository, publisher = env
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    setups = SetupsRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'setups.sqlite'}"))
    setups.ensure_schema()
    pipeline.setup_engine = cast(SetupEngine, SimpleNamespace(repository=setups, on_minute=None))

    today = TS.date()

    def add(status: str, outcome_r: float, *, minutes: int) -> None:
        setup_id = setups.create(
            symbol="ES",
            expiry="20260717",
            template="failed_break",
            direction="short",
            created_ts=TS,
            entry=7600.0,
            target=7580.0,
            stop=7610.0,
            confidence=55,
            reason="test",
            context={},
        )
        setups.close(
            setup_id,
            status=status,
            closed_ts=TS + dt.timedelta(minutes=minutes),
            outcome_r=outcome_r,
            mfe=0.0,
            mae=0.0,
        )

    # 15 stopů = ΣR −15 pod prahem −10 při dostatku vzorků
    for i in range(15):
        add("closed_stop", -1.0, minutes=i)
    await pipeline._run_setup_selfcheck(today)
    alerts = [d for ch, d in publisher.messages if ch == "alerts"]
    assert alerts[-1]["kind"] == "setup_degraded"
    assert "failed_break short" in str(alerts[-1]["message"])

    # Druhý běh téhož dne nic neposílá (jednou za den) …
    before = len(publisher.messages)
    await pipeline._run_setup_selfcheck(today)
    assert len(publisher.messages) == before
    # …a opakované zhoršení další den taky ne (alert právě jednou)
    await pipeline._run_setup_selfcheck(today + dt.timedelta(days=1))
    assert len(publisher.messages) == before

    # Návrat nad práh → recovered právě jednou
    for i in range(30):
        add("closed_target", 1.5, minutes=100 + i)
    await pipeline._run_setup_selfcheck(today + dt.timedelta(days=2))
    alerts = [d for ch, d in publisher.messages if ch == "alerts"]
    assert alerts[-1]["kind"] == "setup_recovered"
    before = len(publisher.messages)
    await pipeline._run_setup_selfcheck(today + dt.timedelta(days=3))
    assert len(publisher.messages) == before


def test_closed_since_ignores_active_and_older_window(tmp_path: Path) -> None:
    """#309: okno se řídí časem uzavření, běžící setupy do bilance nepatří."""
    setups = SetupsRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'setups.sqlite'}"))
    setups.ensure_schema()

    def new_setup() -> int:
        return setups.create(
            symbol="ES",
            expiry="20260717",
            template="wall_bounce",
            direction="long",
            created_ts=TS - dt.timedelta(days=30),  # vznik dávno před oknem
            entry=7600.0,
            target=7620.0,
            stop=7590.0,
            confidence=55,
            reason="test",
            context={},
        )

    inside = new_setup()
    setups.close(inside, status="closed_target", closed_ts=TS, outcome_r=2.0, mfe=0.0, mae=0.0)
    outside = new_setup()
    setups.close(
        outside,
        status="closed_stop",
        closed_ts=TS - dt.timedelta(days=10),
        outcome_r=-1.0,
        mfe=0.0,
        mae=0.0,
    )
    new_setup()  # zůstává active

    rows = setups.closed_since("ES", TS - dt.timedelta(days=7))
    assert len(rows) == 1
    assert rows[0].outcome_r == 2.0
    assert setups.closed_since("NQ", TS - dt.timedelta(days=7)) == []


async def test_secondary_expiry_band_expands_with_price(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """Obálka sekundární expirace musí sledovat cenu (#442).

    Do #442 ji `create_pipeline` nastavil jednou při startu a `maybe_expand` se
    na ni nevolal — 3. 8. tak zamrzla na 28290–28680, cena vyrostla na 28925
    a heatmapa i zdi následující expirace se nad horním strikem přestaly kreslit.
    """
    settings, writer, oi_repository, publisher = env
    spot = 7500.0
    pipeline = make_pipeline("ES", spot, settings, writer, oi_repository, publisher)

    # Sekundár se stejnou nabídkou strikes jako aktivní řetěz, ale širší osou
    strikes = tuple(spot + offset for offset in range(-400, 401, 10))
    next_info = ExpiryInfo(
        trading_class="ES1",
        expiry="20260718",
        exchange="CME",
        multiplier="50",
        strikes=strikes,
    )
    next_band = select_band(next_info.strikes, spot, settings.strike_range_points)
    underlying = Underlying(symbol="ES", sec_type="FUT", exchange="CME", con_id=1)
    pipeline.next_info = next_info
    pipeline.next_band = next_band
    pipeline.next_runtime = EngineRuntime(
        settings=settings,
        scheduler=SubscriptionScheduler(MockQuoteStreamer(), settings),
        writer=writer,
        oi_repository=oi_repository,
        publisher=publisher,
        symbol="ES",
        expiry=next_info.expiry,
        multiplier=50.0,
        contracts=build_contracts(underlying, next_info, next_band),
        cum_delta=CumDeltaTracker(multiplier=50.0),
        push_status=False,
        secondary=True,
    )
    original_high = next_band.high

    # Cena vyběhne k hornímu okraji pásma — stejná situace jako 3. 8.
    await pipeline._expand_secondary(spot=original_high - 5.0, today=TS.date())

    assert pipeline.next_band is not None
    assert pipeline.next_band.high > original_high
    # Kontrakty sekundáru se musí přestavět, jinak se nová šířka nikam nepropíše
    assert max(c.strike for c in pipeline.next_runtime.contracts) > original_high


async def test_secondary_expansion_noop_without_secondary_runtime(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """Bez sekundáru (vypnutý sweep_next_expiry) se nesmí nic stát."""
    settings, writer, oi_repository, publisher = env
    pipeline = make_pipeline("ES", 7500.0, settings, writer, oi_repository, publisher)

    await pipeline._expand_secondary(spot=9999.0, today=TS.date())  # nesmí spadnout

    assert pipeline.next_band is None


async def test_kalibrace_alfa_po_oi_archivu_nastavi_runtime(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#232 fáze 2: ranní kalibrace propíše α do runtime a pošle informační alert."""
    settings, writer, oi_repository, publisher = env
    pipeline = make_pipeline("ES", 7600.0, settings, writer, oi_repository, publisher)
    # Souborová sqlite — ":memory:" má tabulky jen pro jedno spojení
    alpha_repo = FaAlphaRepository(
        create_engine(f"sqlite+pysqlite:///{settings.data_dir.parent / 'alpha.sqlite'}")
    )
    alpha_repo.ensure_schema()
    pipeline.alpha_repository = alpha_repo

    today = TS.date()
    prev = today - dt.timedelta(days=1)
    # Včerejší archiv (dnešní s OI 500 založil make_pipeline): ΔOI = +40 per strana
    contracts = list(pipeline.runtime.contracts)
    oi_repository.upsert_many(
        [OIRecord("ES", "20260717", c.strike, c.right, prev, 460.0) for c in contracts]
    )
    # Včerejší netflow: +100 kontraktů na každé straně → poměr 40/100 = 0.4
    ts = dt.datetime.combine(prev, dt.time(20, 0), tzinfo=dt.UTC)
    writer.write_netflow(
        "ES",
        "20260717",
        prev,
        [
            NetFlowRow(ts_min=ts, strike=c.strike, right=c.right, net_volume=100.0)
            for c in contracts
        ],
    )

    await pipeline._run_alpha_calibration(today)

    assert pipeline.runtime.flow_alpha == pytest.approx(0.4)

    def calibration_alerts() -> list[dict[str, object]]:
        return [
            data
            for channel, data in publisher.messages
            if channel == "alerts" and data.get("kind") == "fa_calibration"
        ]

    assert len(calibration_alerts()) == 1
    assert "0.40" in str(calibration_alerts()[0]["message"])

    # Idempotence: druhý běh bod nepřidá ani nezdvojí alert
    await pipeline._run_alpha_calibration(today)
    assert len(calibration_alerts()) == 1

    # Start enginu další den: nový pipeline bez nového bodu dostane uloženou α
    fresh = make_pipeline("ES", 7600.0, settings, writer, oi_repository, publisher)
    fresh.alpha_repository = alpha_repo
    await fresh._run_alpha_calibration(today)
    assert fresh.runtime.flow_alpha == pytest.approx(0.4)


# ── Hlídka Greeks po settle (#959) ─────────────────────────────────


def test_greeks_hlidka_po_settle_vypnuta() -> None:
    """Po settle se expirující řetěz přestane kotovat — není to porucha (#959)."""
    from gexlens_engine.instruments import greeks_watch_applies

    # Letní čas: settle 16:00 ET = 20:00 UTC
    pred = dt.datetime(2026, 8, 31, 19, 59, tzinfo=dt.UTC)
    po = dt.datetime(2026, 8, 31, 20, 1, tzinfo=dt.UTC)

    assert greeks_watch_applies("20260831", pred) is True
    assert greeks_watch_applies("20260831", po) is False


def test_greeks_hlidka_bezi_pro_budouci_expiraci() -> None:
    """Sekundární řada zítřejší expirace se hlídat MUSÍ — settle má až zítra."""
    from gexlens_engine.instruments import greeks_watch_applies

    po_settle_dneska = dt.datetime(2026, 8, 31, 20, 1, tzinfo=dt.UTC)
    assert greeks_watch_applies("20260901", po_settle_dneska) is True


def test_greeks_hlidka_respektuje_zimni_cas() -> None:
    """V zimě je settle 21:00 UTC — pevná hodina by hlídku hodinu zabíjela."""
    from gexlens_engine.instruments import greeks_watch_applies

    assert (
        greeks_watch_applies("20261215", dt.datetime(2026, 12, 15, 20, 30, tzinfo=dt.UTC)) is True
    )
    assert (
        greeks_watch_applies("20261215", dt.datetime(2026, 12, 15, 21, 1, tzinfo=dt.UTC)) is False
    )


def test_greeks_hlidka_pri_necitelne_expiraci_zustava() -> None:
    """Vadný formát nesmí hlídku umlčet — radši falešný poplach než slepota."""
    from gexlens_engine.instruments import greeks_watch_applies

    assert greeks_watch_applies("nesmysl", dt.datetime(2026, 8, 31, 23, 0, tzinfo=dt.UTC)) is True
