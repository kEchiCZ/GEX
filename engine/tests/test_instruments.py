"""Testy multi-instrument vrstvy (ADR-0003): plánování, watchlist, pipeline nad mocky."""

import asyncio
import datetime as dt
import logging
import math
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pandas as pd
import pytest
from sqlalchemy import create_engine, insert

from gexlens_engine.compute.cumdelta import SOURCE_AGGRESSOR, SOURCE_MIDPOINT, CumDeltaTracker
from gexlens_engine.compute.facalibration import FA_MIN_DAYS, AlphaCalibrationPoint
from gexlens_engine.compute.marketclock import is_market_closed
from gexlens_engine.compute.spot import SpotReading
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
    OI_ARCHIVE_RESERVE_S,
    OI_RETRY_CYCLES,
    SETUP_RETRY_CYCLES,
    SETUP_RETRY_FIRST_CYCLES,
    InstrumentPipeline,
    SetupCooldown,
    WatchlistReader,
    advance_oi_archives,
    aggregate_status,
    gather_metrics,
    merge_symbols,
    parse_multiplier,
    plan_instruments,
)
from gexlens_engine.runtime import EngineRuntime, PublisherLike
from gexlens_engine.setups import SetupEngine
from gexlens_engine.storage.fa_calibration import SOURCE_VOLUME, FaAlphaRepository
from gexlens_engine.storage.meta import settings_table, watchlist_table
from gexlens_engine.storage.oi_archive import (
    ArchiveResult,
    ContractSnapshot,
    OIArchiver,
    OIEodRepository,
    OIMiss,
    OIRecord,
)
from gexlens_engine.storage.parquet_store import NetFlowRow, SnapshotWriter
from gexlens_engine.storage.setups_store import SetupsRepository

TS = dt.datetime(2026, 7, 17, 15, 0, tzinfo=dt.UTC)
# Otevřený trh v pondělí (10:00 CDT) — OI alerty se od #1307 hlásí jen při
# otevřeném trhu, takže test nesmí záviset na dni, kdy běží (sobota = ticho)
OPEN_MONDAY = dt.datetime(2026, 7, 20, 15, 0, tzinfo=dt.UTC)


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


def test_expiry_expired_roll_v_settle_0dte() -> None:
    """Roll 0DTE v settle 16:00 ET, ne o půlnoci UTC (#1331)."""
    from gexlens_engine.instruments import expiry_expired

    settle = dt.datetime(2026, 7, 17, 20, 0, tzinfo=dt.UTC)  # léto: 16:00 EDT
    assert expiry_expired("20260717", settle - dt.timedelta(minutes=1)) is False
    assert expiry_expired("20260717", settle) is True  # roll přesně v settle
    # Dřívější okno 3–4 h nad mrtvým řetězem do půlnoci UTC už neexistuje
    assert expiry_expired("20260717", dt.datetime(2026, 7, 17, 22, 0, tzinfo=dt.UTC)) is True
    assert expiry_expired("20260718", settle) is False  # zítřejší žije
    assert expiry_expired("nesmysl", settle) is False  # nečitelný formát neshazuje běh


def test_expiry_expired_patek_pred_vikendem() -> None:
    """Páteční expirace roluje v pátečním settle — další je pondělní, ne až v sobotu."""
    from gexlens_engine.compute.settle import expiry_settle
    from gexlens_engine.discovery_cache import CachedDiscovery, FrontFuture
    from gexlens_engine.ibkr.discovery import ExpiryInfo
    from gexlens_engine.instruments import expiry_expired

    friday_settle = dt.datetime(2026, 9, 25, 20, 0, tzinfo=dt.UTC)
    assert expiry_settle("20260925") == friday_settle
    assert expiry_expired("20260925", friday_settle - dt.timedelta(seconds=1)) is False
    assert expiry_expired("20260925", friday_settle) is True
    cached = CachedDiscovery(
        symbol="ES",
        stored_at=friday_settle,
        front=FrontFuture("ES", 1, "CME", "50", "20261218", "ESZ6", "ES"),
        expiries=tuple(
            ExpiryInfo("EW4", expiry, "CME", "50", (6600.0,))
            for expiry in ("20260925", "20260928", "20260929")
        ),
    )
    # Discovery po rollu: páteční vynechá, začne pondělní (Globex v neděli večer)
    assert [info.expiry for info in cached.unexpired(friday_settle)] == ["20260928", "20260929"]
    sunday_open = dt.datetime(2026, 9, 27, 22, 0, tzinfo=dt.UTC)
    assert cached.unexpired(sunday_open)[0].expiry == "20260928"


def test_expiry_expired_dst() -> None:
    """Settle 16:00 ET podle IANA zóny (#511): léto 20:00 UTC, zima 21:00 UTC."""
    from gexlens_engine.instruments import expiry_expired

    # Pátek 30. 10. (EDT) vs. pondělí 2. 11. po přechodu na zimní čas 1. 11.
    assert expiry_expired("20261030", dt.datetime(2026, 10, 30, 20, 0, tzinfo=dt.UTC)) is True
    winter = dt.datetime(2026, 11, 2, 21, 0, tzinfo=dt.UTC)
    assert expiry_expired("20261102", winter - dt.timedelta(minutes=1)) is False
    assert expiry_expired("20261102", winter) is True
    # Jaro: US přechází 8. 3. 2026 — pondělí 9. 3. už v letním čase (20:00 UTC)
    spring = dt.datetime(2026, 3, 9, 20, 0, tzinfo=dt.UTC)
    assert expiry_expired("20260309", spring - dt.timedelta(minutes=1)) is False
    assert expiry_expired("20260309", spring) is True
    # Pátek 6. 3. ještě v zimním čase (21:00 UTC)
    assert expiry_expired("20260306", dt.datetime(2026, 3, 6, 20, 30, tzinfo=dt.UTC)) is False


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

    assert repository.latest_trading_day_before("ES", "20260717", day2) == day1
    assert repository.latest_trading_day_before("ES", "20260717", day1) is None
    values = repository.values_for("ES", "20260717", day1)
    assert len(values) == 1
    assert values[0].strike == 7500.0 and values[0].right == "P" and values[0].oi == 100.0


def test_oi_prev_trading_day_skips_weekend_keys(tmp_path: Path) -> None:
    """#1309: archiv má klíče i pro sobotu a neděli (UTC den pořízení), základ
    ΔOI je ale poslední OBCHODNÍ den — pondělí se srovnává s pátkem, ne s nedělí,
    jejíž archiv po otevření Globexu nese už pondělní čísla (21. 9. 2026: ΔOI 0)."""
    repository = OIEodRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'oi.sqlite'}"))
    repository.ensure_schema()
    friday, saturday, sunday = dt.date(2026, 9, 18), dt.date(2026, 9, 19), dt.date(2026, 9, 20)
    monday, tuesday = dt.date(2026, 9, 21), dt.date(2026, 9, 22)
    for day in (friday, saturday, sunday, monday, tuesday):
        repository.upsert_many([OIRecord("ES", "20260925", 6700.0, "P", day, 100.0)])

    assert repository.latest_trading_day_before("ES", "20260925", monday) == friday
    assert repository.latest_trading_day_before("ES", "20260925", sunday) == friday
    assert repository.latest_trading_day_before("ES", "20260925", saturday) == friday
    # Regrese pracovního dne: úterý proti pondělí
    assert repository.latest_trading_day_before("ES", "20260925", tuesday) == monday
    # Před dnem jen víkendové klíče → žádný základ, ne víkendová kopie
    for day in (saturday, sunday, monday):
        repository.upsert_many([OIRecord("ES", "20260928", 6700.0, "P", day, 50.0)])
    assert repository.latest_trading_day_before("ES", "20260928", monday) is None


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
    # IBKR snímek dne z čtení od 00:00 UTC (`captured_ts`, #463); o přeskočení
    # čtení rozhoduje jen IBKR snímek, tasty řádky ne (#1609)
    oi_repository.upsert_many(
        [OIRecord(symbol, info.expiry, c.strike, c.right, TS.date(), 500.0) for c in contracts],
        dt.datetime.combine(TS.date(), dt.time(0, 5), tzinfo=dt.UTC),
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
        cum_delta=CumDeltaTracker(multiplier=50.0, source=SOURCE_MIDPOINT),
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


async def run_minute_oi(pipeline: InstrumentPipeline, at: dt.datetime) -> SweepMetrics:
    """Iterace hlavní smyčky pro jednu pipeline: cyklus, pak OI archiv ve zbytku minuty (#1646).

    Bez hodin smyčky (start další iterace v nekonečnu) se otevřený průchod přečte
    a zapíše v téže minutě; joby po archivu běží na pozadí, test na ně počká.
    """
    metrics = await pipeline.run_minute(at)
    await advance_oi_archives([pipeline], math.inf)
    if pipeline._oi_jobs_task is not None:
        await pipeline._oi_jobs_task
    return metrics


async def archive_pass(
    pipeline: InstrumentPipeline, today: dt.date, now: dt.datetime | None = None
) -> bool:
    """Celý průchod OI archivu dne bez minutového cyklu (#1674): otevření, dávky
    bez časového limitu a joby po archivu; vrací `oi_available` po průchodu."""
    await pipeline.start_oi_archive(today, now)
    await pipeline.advance_oi_archive(math.inf, lambda: 0.0)
    if pipeline._oi_jobs_task is not None:
        await pipeline._oi_jobs_task
    return pipeline.oi_available


def test_roll_handover_jen_sekundar_pod_tisky(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#1460 varianta C: tracker předává jen sekundár pod tisky; v5 a bez sekundáru nic."""
    settings, writer, repository, publisher = env
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    assert pipeline.roll_handover() is None
    secondary = replace(pipeline.runtime, expiry="20260720", secondary=True)
    secondary.cum_delta = CumDeltaTracker(multiplier=50.0, source=SOURCE_MIDPOINT)
    pipeline.next_runtime = secondary
    assert pipeline.roll_handover() is None
    secondary.cum_delta = CumDeltaTracker(multiplier=50.0, source=SOURCE_AGGRESSOR)
    assert pipeline.roll_handover() is secondary


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
    assert await archive_pass(pipeline, today, po_okne) is True

    assert repository.get_oi("ES", today, specs[0].strike, specs[0].right) == 500.0
    assert pipeline.oi_final is False  # jedno čtení po okně nestačí

    # Druhé čtení dá totéž → snímek je potvrzený a dál se neobnovuje
    assert await archive_pass(pipeline, today, po_okne) is True
    assert pipeline.oi_final is True

    # A před oknem se předpublikační snímek nepřepisuje (data ještě nejsou)
    pipeline.oi_final = False
    pipeline.archiver = OIArchiver(repository, MockOIFetcher(dict.fromkeys(specs, 9.0)), settings)
    pred_oknem = settings.oi_publication_utc(dt.date(2026, 8, 4)) - dt.timedelta(hours=2)
    assert await archive_pass(pipeline, today, pred_oknem) is True
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
    assert await archive_pass(pipeline, today, po_okne) is True
    assert await archive_pass(pipeline, today, po_okne) is True
    assert pipeline.oi_final is False  # pokrytí pod prahem — obnova musí běžet dál
    assert pipeline._oi_refresh_due(po_okne) is True

    # Publikace doběhla → plné čtení; dvě shodná čtení teprve teď finalizují
    pipeline.archiver = OIArchiver(repository, MockOIFetcher(dict.fromkeys(specs, 500.0)), settings)
    assert await archive_pass(pipeline, today, po_okne) is True
    assert pipeline.oi_final is False  # první plné čtení se od řídkého liší
    assert await archive_pass(pipeline, today, po_okne) is True
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
    assert await archive_pass(pipeline, today, po_okne) is True

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
        cum_delta=CumDeltaTracker(multiplier=50.0, source=SOURCE_MIDPOINT),
        push_status=False,
        secondary=True,
    )
    original_high = next_band.high

    class ConstOIFetcher(MockOIFetcher):
        """OI pro libovolný kontrakt — nové striky vzniknou až expanzí."""

        async def fetch_snapshot(
            self, spec: OptionContractSpec, timeout_s: float
        ) -> ContractSnapshot | OIMiss:
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

    # Den 1: čtení po okně shodné s předchozím IBKR čtením → finální. make_pipeline
    # zapisuje bez času pořízení a takový řádek není IBKR čtení (#1605), proto
    # předchozí čtení (500.0) s časem tady
    po_okne = settings.oi_publication_utc(dt.date(2026, 7, 17)) + dt.timedelta(minutes=5)
    repository.upsert_many(
        [
            OIRecord("ES", s.expiry, s.strike, s.right, day1, 500.0, trading_class=s.trading_class)
            for s in specs
        ],
        po_okne - dt.timedelta(minutes=1),
    )
    assert await archive_pass(pipeline, day1, po_okne) is True
    assert pipeline.oi_final is True

    # Přechod přes půlnoc: finalita se resetuje a nový den se archivuje hned
    pulnoc = dt.datetime(2026, 7, 18, 0, 0, tzinfo=dt.UTC)
    await run_minute_oi(pipeline, pulnoc)

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

    class ExplodingFetcher(MockOIFetcher):
        async def fetch_snapshot(
            self, spec: OptionContractSpec, timeout_s: float
        ) -> ContractSnapshot | OIMiss:
            raise RuntimeError("mock: přechodný výpadek IBKR")

    pipeline.archiver = OIArchiver(repository, ExplodingFetcher(), settings)

    assert await archive_pass(pipeline, today, po_okne) is True  # starší snímek platí dál
    alerts = [d for ch, d in publisher.messages if ch == "alerts"]
    assert alerts[-1]["kind"] == "oi_refresh_failed"
    assert "starším snímku" in str(alerts[-1]["message"])
    assert pipeline.oi_final is False  # obnova se zopakuje dalším retry cyklem

    # Totéž pro fetch, který nespadne, ale nic nevrátí (written == 0): týž den
    # je už ohlášený, další selhání jde jen do logu (#1307 hranově)
    pipeline.archiver = OIArchiver(repository, MockOIFetcher(), settings)
    assert await archive_pass(pipeline, today, po_okne) is True
    alerts = [d for ch, d in publisher.messages if ch == "alerts"]
    assert [a["kind"] for a in alerts] == ["oi_refresh_failed"]
    assert pipeline.oi_final is False  # obnova běží dál, jen bez opakovaného alertu


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
        cum_delta=CumDeltaTracker(multiplier=50.0, source=SOURCE_MIDPOINT),
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

    # MockOIFetcher bez hodnot
    ok = await archive_pass(pipeline, OPEN_MONDAY.date(), OPEN_MONDAY)
    assert ok is False
    alerts = [data for channel, data in publisher.messages if channel == "alerts"]
    assert alerts and alerts[-1]["kind"] == "oi_missing"
    assert alerts[-1]["symbol"] == "CL"


async def test_prvni_archiv_po_zalozeni_jen_otevre_pruchod(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#1674: založení pipeline (start enginu, roll) průchod jen otevře — IBKR
    nečte a nečeká; platný snímek bez obnovy je k dispozici hned (#1208)."""
    settings, writer, repository, publisher = env
    pipeline, archiver, _ = timed_pipeline(env, 700.0, oi_available=False)
    po_okne = settings.oi_publication_utc(TS.date()) + dt.timedelta(hours=1)

    await pipeline.start_oi_archive(po_okne.date(), po_okne)
    assert pipeline._oi_pass is not None and pipeline._oi_pass.offset == 0
    assert archiver.batches == [] and archiver.calls == []
    assert pipeline.oi_available is False  # volume fallback do konce průchodu

    # Snímek dne před publikačním oknem obnovu nepotřebuje — OI je hned
    fresh = make_pipeline(
        "NQ", 24000.0, settings, writer, repository, publisher, oi_available=False
    )
    pred_oknem = settings.oi_publication_utc(TS.date()) - dt.timedelta(hours=2)
    await fresh.start_oi_archive(TS.date(), pred_oknem)
    assert fresh._oi_pass is None
    assert fresh.oi_available is True


async def test_selhani_otevreni_prvniho_archivu_neshodi_zalozeni(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1674 + #215: chyba PG při otevření prvního archivu nesmí shodit
    `create_pipeline` (start ani roll) — dřív ji spolkl task na pozadí. Pipeline
    jede s volume fallbackem a retry z minutového cyklu průchod otevře."""
    settings, _, _, _ = env
    pipeline, archiver, _ = timed_pipeline(env, 700.0, oi_available=False)
    po_okne = settings.oi_publication_utc(TS.date()) + dt.timedelta(hours=1)
    days = pipeline.oi_repository.days

    def broken_days(symbol: str) -> list[dt.date]:
        raise RuntimeError("mock: PG nedostupné")

    monkeypatch.setattr(pipeline.oi_repository, "days", broken_days)
    await pipeline.start_oi_archive(po_okne.date(), po_okne)
    assert pipeline._oi_pass is None
    assert pipeline.oi_available is False

    monkeypatch.setattr(pipeline.oi_repository, "days", days)
    pipeline._cycles_since_oi = OI_RETRY_CYCLES
    await pipeline.run_minute(po_okne)
    assert pipeline._oi_pass is not None
    assert archiver.batches == []


async def test_prvni_archiv_po_davkach_bez_soubehu_se_sweepem_ani_symbolem(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1674 (#1649): první archiv po startu a rollu četl celý průchod naráz na
    pozadí souběžně se sweepy i s prvním archivem druhého symbolu — 23. 10. po
    rollu ve 20:01 UTC stála smyčka 8,08 s a hlídač heartbeatu shodil spojení.
    Teď ho čte zbytek minuty po sweepech jako obnovu (#1646): ES celý, pak NQ,
    minutový cyklus ho znovu neotevře a žádná iterace nepřetáhne start další."""
    settings, writer, repository, publisher = env
    sweeping = [False]
    es, es_archiver, clock = timed_pipeline(env, 700.0, oi_available=False, sweeping=sweeping)
    nq = make_pipeline("NQ", 24000.0, settings, writer, repository, publisher, oi_available=False)
    nq_fetcher = SweepGuardFetcher(dict.fromkeys(nq.runtime.contracts, 900.0), sweeping)
    nq_archiver = TimedArchiver(repository, nq_fetcher, settings, clock)
    nq.archiver = nq_archiver
    po_okne = settings.oi_publication_utc(TS.date()) + dt.timedelta(hours=1)

    async def sweep(
        ts_min: dt.datetime, spot: float, bars: object, forming_bar: object = None
    ) -> SweepMetrics:
        sweeping[0] = True
        for _ in range(5):  # pustí ke slovu úlohy na pozadí
            await asyncio.sleep(0)
        clock.t += 20.0
        sweeping[0] = False
        return SweepMetrics(
            total=6, greeks_complete=6, repair_count=0, stale_count=0, sweep_duration_s=20.0
        )

    for pipeline in (es, nq):
        monkeypatch.setattr(pipeline.runtime, "run_cycle", sweep)
        await pipeline.start_oi_archive(po_okne.date(), po_okne)
    for minute in range(5):
        cycle_start = 60.0 * minute
        clock.t = cycle_start
        for pipeline in (es, nq):
            await pipeline.run_minute(po_okne + dt.timedelta(minutes=minute))
        await advance_oi_archives([es, nq], cycle_start + 60.0, clock=clock)
        assert clock.t <= cycle_start + 60.0 - OI_ARCHIVE_RESERVE_S
    for pipeline in (es, nq):
        if pipeline._oi_jobs_task is not None:
            await pipeline._oi_jobs_task

    # ES 3 + 3 dávky, zápis ve třetí minutě; NQ začne až po něm
    assert es_archiver.batches == [40.0, 44.0, 48.0, 100.0, 104.0, 108.0]
    assert nq_archiver.batches == [161.0, 165.0, 169.0, 220.0, 224.0, 228.0]
    assert es_archiver.calls == [po_okne.date()] and nq_archiver.calls == [po_okne.date()]
    for pipeline, fetcher in ((es, es_archiver._fetcher), (nq, nq_fetcher)):
        assert isinstance(fetcher, SweepGuardFetcher)
        assert fetcher.during_sweep == []  # žádné čtení z IBKR souběžně se sweepem
        assert pipeline._oi_pass is None
        assert pipeline.oi_available is True


async def test_prvni_archiv_po_startu_v_zavrenem_trhu_dobehne(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#1674: start v zavřeném trhu (víkend, pauza Globexu = okno nasazení) není
    hrana zavření — první cyklus průchod nezahodí a OI dne se dočte."""
    pipeline, archiver, clock = timed_pipeline(env, 640.0, oi_available=False)
    assert is_market_closed(SAT_NOON)
    await pipeline.start_oi_archive(SAT_NOON.date(), SAT_NOON)
    assert pipeline._oi_pass is not None  # sobotní den snímek nemá

    await pipeline.run_minute(SAT_NOON + dt.timedelta(minutes=1))
    assert pipeline._oi_pass is not None
    await advance_oi_archives([pipeline], math.inf, clock=clock)
    assert archiver.calls == [SAT_NOON.date()]
    assert pipeline.oi_available is True


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

    async def quick(now: dt.datetime) -> SweepMetrics:
        # Zdravý cyklus jako stub: test měří, že visící pipeline nezastaví
        # smyčku, ne rychlost skutečného cyklu — ten při zátěži CI/notebooku
        # 0,2 s neplnil a test padal náhodně (14. 9. 2026, 1 z 5 běhů)
        await asyncio.sleep(0)
        return SweepMetrics(
            total=1, greeks_complete=1, repair_count=0, stale_count=0, sweep_duration_s=0.0
        )

    stuck.run_minute = hang  # type: ignore[method-assign]
    healthy.run_minute = quick  # type: ignore[method-assign]

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


async def test_bars_stall_restarts_stream(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#1082: po stall se stream obnoví sám; bez návratu barů další pokus bez alertu."""
    settings, writer, repository, publisher = env
    settings.bars_stall_alert_minutes = 2
    settings.level_alert_near_steps = 0.0
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    restarts: list[int] = []

    async def fake_restart() -> None:
        restarts.append(len(restarts) + 1)
        if len(restarts) == 1:
            # První obnova selže (TWS ještě bez farem) — cyklus nesmí spadnout
            raise RuntimeError("Not connected")

    pipeline.restart_bars = fake_restart
    ticker = pipeline.ticker
    assert isinstance(ticker, FakeTicker)

    await pipeline.run_minute(TS)
    for minute in range(1, 7):
        ticker.last = 7600.0 + minute
        await pipeline.run_minute(TS + dt.timedelta(minutes=minute))

    # Práh 2 min: stall v cyklu 2, další pokusy v cyklech 4 a 6
    assert restarts == [1, 2, 3]
    alerts = [data for channel, data in publisher.messages if channel == "alerts"]
    assert [a["kind"] for a in alerts] == ["bars_stalled"]
    assert "obnovuje sám" in str(alerts[0]["message"])


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


@pytest.mark.parametrize(
    "start",
    [
        dt.datetime(2026, 7, 18, 15, 0, tzinfo=dt.UTC),  # sobota
        dt.datetime(2026, 7, 15, 21, 5, tzinfo=dt.UTC),  # středa 16:05 CDT, denní pauza
        dt.datetime(2026, 7, 19, 21, 30, tzinfo=dt.UTC),  # neděle 16:30 CDT, před otevřením
    ],
)
async def test_no_stall_alert_when_market_quiet(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
    start: dt.datetime,
) -> None:
    """#221, #1347: zavřený trh — chybějící bary nesmí spouštět alert (ani teď, kdy
    se při otevřeném trhu ticho počítá i bez pohybu spotu)."""
    settings, writer, repository, publisher = env
    settings.bars_stall_alert_minutes = 2
    settings.level_alert_near_steps = 0.0  # zdi fixture jsou u spotu — nešumět (#675)
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)

    for minute in range(5):
        await pipeline.run_minute(start + dt.timedelta(minutes=minute))

    assert not [
        data
        for channel, data in publisher.messages
        if channel == "alerts" and data["kind"] == "bars_stalled"
    ]


async def test_zamrzly_spot_pri_otevrenem_trhu_je_vypadek_baru(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#1347: 3. 9. 14:17–15:29 UTC stál stream i spot (O=H=L=C 72 min) — detektor
    bez pohybu spotu nepočítal, takže nevznikl alert, re-backfill ani doplnění."""
    settings, writer, repository, publisher = env
    settings.bars_stall_alert_minutes = 2
    settings.level_alert_near_steps = 0.0
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)

    for minute in range(3):  # FakeTicker drží stejnou cenu, bary nechodí
        await pipeline.run_minute(TS + dt.timedelta(minutes=minute))

    stalled = [
        data
        for channel, data in publisher.messages
        if channel == "alerts" and data["kind"] == "bars_stalled"
    ]
    assert len(stalled) == 1
    assert "stojí i spot" in str(stalled[0]["message"])


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


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (dt.datetime(2026, 9, 26, 0, 5, tzinfo=dt.UTC), 0),  # sobota po rollu (26. 9. 00:05)
        (dt.datetime(2026, 9, 27, 21, 59, tzinfo=dt.UTC), 0),  # neděle před otevřením
        (dt.datetime(2026, 9, 23, 21, 30, tzinfo=dt.UTC), 0),  # středa, denní pauza
        (dt.datetime(2026, 9, 27, 22, 0, tzinfo=dt.UTC), 1),  # nedělní otevření 17:00 CDT
        (dt.datetime(2026, 9, 24, 15, 0, tzinfo=dt.UTC), 1),  # čtvrtek v seanci
    ],
)
async def test_vol_concentration_jen_pri_otevrenem_trhu(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
    now: dt.datetime,
    expected: int,
) -> None:
    """#1309: 26. 9. 00:05 UTC, tři hodiny po páteční uzávěrce, odešlo
    „ES 20260929: 7900C 19,7× medián" — nový sekundární řetěz po sobotním
    rollu nad 10 snímky s páteční volume. Při zavřeném trhu se nehledá,
    v seanci (i hned po nedělním otevření) beze změny."""
    from types import SimpleNamespace

    settings, writer, repository, publisher = env
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)

    def cached(volume: float) -> SimpleNamespace:
        return SimpleNamespace(snapshot=SimpleNamespace(volume=volume))

    quotes = {
        OptionContractSpec("ES", "FOP", "20260929", 7900.0, "C", "CME", "E5B", "50"): cached(9000),
        OptionContractSpec("ES", "FOP", "20260929", 7500.0, "P", "CME", "E5B", "50"): cached(400),
        OptionContractSpec("ES", "FOP", "20260929", 7580.0, "C", "CME", "E5B", "50"): cached(300),
        OptionContractSpec("ES", "FOP", "20260929", 7400.0, "P", "CME", "E5B", "50"): cached(350),
    }
    pipeline.next_runtime = SimpleNamespace(  # type: ignore[assignment]
        expiry="20260929", current_quotes=lambda: quotes
    )

    await pipeline._check_vol_concentration(now)

    assert len(alerts_of(publisher, {"vol_concentration"})) == expected


async def test_archive_failure_does_not_kill_pipeline(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#215 (MES): výjimka z archivace → alert + False, žádné probublání do cooldownu."""
    settings, writer, repository, publisher = env
    pipeline = make_pipeline("MES", 7500.0, settings, writer, repository, publisher)

    class ExplodingArchiver(OIArchiver):
        async def commit(
            self,
            records: Sequence[OIRecord],
            missing: Sequence[OptionContractSpec],
            day: dt.date,
            now: dt.datetime | None = None,
        ) -> ArchiveResult:
            raise RuntimeError("mock: CardinalityViolation")

    pipeline.archiver = ExplodingArchiver(repository, MockOIFetcher(), settings)

    ok = await archive_pass(pipeline, OPEN_MONDAY.date(), OPEN_MONDAY)

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
        cum_delta=CumDeltaTracker(multiplier=50.0, source=SOURCE_MIDPOINT),
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
    # Archivy obou dnů s IBKR měřením po publikaci (#1605; make_pipeline zapisuje
    # dnešní OI 500 z čtení ve 00:05 UTC, tedy před publikací): ΔOI = +40 per strana
    contracts = list(pipeline.runtime.contracts)
    oi_repository.upsert_many(
        [OIRecord("ES", "20260717", c.strike, c.right, prev, 460.0) for c in contracts],
        settings.oi_publication_utc(prev) + dt.timedelta(hours=1),
    )
    oi_repository.upsert_many(
        [OIRecord("ES", "20260717", c.strike, c.right, today, 500.0) for c in contracts],
        settings.oi_publication_utc(today) + dt.timedelta(hours=1),
    )
    # Včerejší netflow: +100 kontraktů na každé straně → poměr 40/100 = 0.4
    ts = dt.datetime.combine(prev, dt.time(20, 0), tzinfo=dt.UTC)
    writer.write_netflow(
        "ES",
        "20260717",
        prev,
        [
            NetFlowRow(
                ts_min=ts,
                strike=c.strike,
                right=c.right,
                net_volume=100.0,
                source=SOURCE_MIDPOINT,
            )
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


async def test_v6_fa_jede_az_po_peti_dnech_kalibrace_rho(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """ADR-0057 bod 6, ADR-0061 bod 4: pod v6 α z midpointu, α z tisků ani výchozí α FA
    nepustí; až ρ z objemu s ≥ 5 dny."""
    settings, writer, oi_repository, publisher = env
    pipeline = make_pipeline("ES", 7600.0, settings, writer, oi_repository, publisher)
    pipeline.runtime.cum_delta = CumDeltaTracker(multiplier=50.0, source=SOURCE_AGGRESSOR)
    alpha_repo = FaAlphaRepository(
        create_engine(f"sqlite+pysqlite:///{settings.data_dir.parent / 'alpha.sqlite'}")
    )
    alpha_repo.ensure_schema()
    pipeline.alpha_repository = alpha_repo

    await pipeline._restore_alpha()  # bez stavu: pod v5 by platila konfigurace (0,4)
    assert pipeline.runtime.flow_alpha == 0.0
    point = AlphaCalibrationPoint(samples=20, ratio_median=0.5, ratio_buy=None, ratio_sell=None)
    day = TS.date() - dt.timedelta(days=1)
    alpha_repo.record("ES", day, "20260717", point, 0.505, 17, source=SOURCE_MIDPOINT)
    await pipeline._restore_alpha()
    assert pipeline.runtime.flow_alpha == 0.0  # stav z midpointu = nekalibrováno
    alpha_repo.record("ES", day, "20260717", point, 0.3, FA_MIN_DAYS, source=SOURCE_AGGRESSOR)
    await pipeline._restore_alpha()
    assert pipeline.runtime.flow_alpha == 0.0  # α z tisků (ADR-0057) model A nahradil
    alpha_repo.record("ES", day, "20260717", point, 0.3, FA_MIN_DAYS - 1, source=SOURCE_VOLUME)
    await pipeline._restore_alpha()
    assert pipeline.runtime.flow_alpha == 0.0
    alpha_repo.record("ES", day, "20260717", point, 0.3, FA_MIN_DAYS, source=SOURCE_VOLUME)
    await pipeline._restore_alpha()
    assert pipeline.runtime.flow_alpha == pytest.approx(0.3)


async def test_kalibrace_alfa_az_nad_finalnim_snimkem(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#1314: půlnoční (předpublikační) archiv kalibraci nespustí a bod nezamkne.

    Dřív běžela kalibrace hned po prvním archivu dne v 00:00 UTC nad OI před
    publikací CME — ΔOI ≈ 0, medián kolem 0 a denní dedup pak nedovolil bod
    přepočítat po publikaci (NQ α = 0, FA vrstva vypnutá).
    """
    settings, writer, oi_repository, publisher = env
    pipeline = make_pipeline("ES", 7600.0, settings, writer, oi_repository, publisher)
    alpha_repo = FaAlphaRepository(
        create_engine(f"sqlite+pysqlite:///{settings.data_dir.parent / 'alpha.sqlite'}")
    )
    alpha_repo.ensure_schema()
    pipeline.alpha_repository = alpha_repo

    today = TS.date()  # pátek 17. 7.
    prev = today - dt.timedelta(days=1)
    contracts = list(pipeline.runtime.contracts)
    # Včerejšek s IBKR měřením po publikaci (#1605, varianta B+: měřená i strana D)
    oi_repository.upsert_many(
        [OIRecord("ES", "20260717", c.strike, c.right, prev, 460.0) for c in contracts],
        settings.oi_publication_utc(prev) + dt.timedelta(hours=1),
    )
    ts = dt.datetime.combine(prev, dt.time(20, 0), tzinfo=dt.UTC)
    writer.write_netflow(
        "ES",
        "20260717",
        prev,
        [
            NetFlowRow(
                ts_min=ts,
                strike=c.strike,
                right=c.right,
                net_volume=100.0,
                source=SOURCE_MIDPOINT,
            )
            for c in contracts
        ],
    )
    # Půlnoční snímek dne: CME ještě nepublikovala, jen pár stran se pohnulo
    oi_repository.upsert_many(
        [
            OIRecord("ES", "20260717", c.strike, c.right, today, 461.0 if i % 4 == 0 else 460.0)
            for i, c in enumerate(contracts)
        ],
        dt.datetime(2026, 7, 17, 0, 5, tzinfo=dt.UTC),
    )
    okno = settings.oi_publication_utc(today)
    assert await archive_pass(pipeline, today, okno - dt.timedelta(hours=2)) is True
    assert pipeline.oi_final is False
    assert alpha_repo.history_exists("ES", prev) is False  # bod nezamčený
    assert pipeline.runtime.flow_alpha is None

    # Po publikaci: dvě shodná čtení (mock 500) → finální snímek → poctivý bod 40/100
    specs = pipeline.runtime.contracts
    pipeline.archiver = OIArchiver(
        oi_repository, MockOIFetcher(dict.fromkeys(specs, 500.0)), settings
    )
    po_okne = okno + dt.timedelta(minutes=5)
    assert await archive_pass(pipeline, today, po_okne) is True
    assert alpha_repo.history_exists("ES", prev) is False  # jedno čtení nestačí
    assert await archive_pass(pipeline, today, po_okne) is True
    assert pipeline.oi_final is True
    assert pipeline.runtime.flow_alpha == pytest.approx(0.4)


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


async def test_bars_stall_dopnuje_tasty_a_spot_override_hyba_detektorem(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#614 doplněk: IBKR mlčí celý (ticker zamrzlý), spot jede z tasty přes
    `spot_override` → hlídač barů stall pozná a každý cyklus doplní minuty z tasty;
    po návratu barů se doplňování zastaví."""
    settings, writer, repository, publisher = env
    settings.bars_stall_alert_minutes = 2
    settings.level_alert_near_steps = 0.0
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    fills: list[tuple[dt.datetime, dt.datetime]] = []
    tasty_price = 7600.0

    async def fake_fill(since: dt.datetime, until: dt.datetime) -> int:
        fills.append((since, until))
        return 3

    pipeline.bar_gap_fill = fake_fill
    pipeline.spot_override = lambda: tasty_price
    ticker = pipeline.ticker
    assert isinstance(ticker, FakeTicker)
    ticker.last = float("nan")  # IBKR ticker mlčí

    await pipeline.run_minute(TS)
    for minute in range(1, 5):
        tasty_price = 7600.0 + minute  # spot z tasty se hýbe, IBKR ticker ne
        await pipeline.run_minute(TS + dt.timedelta(minutes=minute))
        if pipeline._gap_fill_task is not None:
            await pipeline._gap_fill_task

    # Cyklus pipeline počítá nad tasty cenou, ne nad zamrzlým tickerem
    assert pipeline.spot == pytest.approx(7604.0)
    # Stall v cyklu 1 (#1347: ticho se počítá od prvního cyklu otevřeného trhu);
    # doplnění v cyklech 1–4 s klouzavým oknem do aktuální minuty
    assert len(fills) == 4
    assert fills[-1][1] == TS + dt.timedelta(minutes=4)
    # Okno = max(práh, 3) + 5 min rezervy — jen čerstvá díra, ne celý den
    assert fills[-1][0] == fills[-1][1] - dt.timedelta(minutes=8)
    assert pipeline._gap_filled == 12
    alerts = [data for channel, data in publisher.messages if channel == "alerts"]
    assert [a["kind"] for a in alerts] == ["bars_stalled"]

    # Návrat barů → recovery, počítadlo doplněných se nuluje, další cyklus už nedoplňuje
    now = TS + dt.timedelta(minutes=5)
    pipeline.minute_bars.append(
        Bar(ts=now, open=7603.0, high=7605.0, low=7602.0, close=7604.0, volume=10.0)
    )
    tasty_price = 7605.0
    await pipeline.run_minute(now)
    assert pipeline._gap_filled == 0
    assert len(fills) == 4


# ── Zavřený trh podle rozvrhu CME (#1307) ──────────────────────────
#
# Září 2026 je CDT (UTC−5): pátek 25. 9. zavírá v 16:00 CT = 21:00 UTC,
# neděle 27. 9. otevírá v 17:00 CT = 22:00 UTC, publikační okno OI 07:00 CT
# = 12:00 UTC. 1. 11. 2026 končí DST — denní pauza 16:00–17:00 CST je pak
# 22:00–23:00 UTC.

FRI_CLOSE = dt.datetime(2026, 9, 25, 21, 0, tzinfo=dt.UTC)
SAT_ROLL = dt.datetime(2026, 9, 26, 0, 0, tzinfo=dt.UTC)
SAT_NOON = dt.datetime(2026, 9, 26, 12, 30, tzinfo=dt.UTC)
SUN_PREOPEN = dt.datetime(2026, 9, 27, 21, 59, tzinfo=dt.UTC)
SUN_OPEN = dt.datetime(2026, 9, 27, 22, 0, tzinfo=dt.UTC)
MON_MIDNIGHT = dt.datetime(2026, 9, 28, 0, 0, tzinfo=dt.UTC)
OI_KINDS = {"oi_missing", "oi_refresh_failed"}
# Expirace po víkendu — hlídka Greeks (#959) pro ni platí celý víkend
MONDAY_EXPIRY = "20260928"


class CountingArchiver(OIArchiver):
    """Skutečný archiver nad mock fetcherem, který si pamatuje každý průchod čtení z IBKR.

    Průchod končí zápisem `commit` — celý naráz (`archive_day`) i po dávkách
    mezi cykly (#1646); `times` drží minutu jeho otevření.
    """

    def __init__(
        self, repository: OIEodRepository, fetcher: MockOIFetcher, settings: Settings
    ) -> None:
        super().__init__(repository, fetcher, settings)
        self.calls: list[dt.date] = []
        self.times: list[dt.datetime | None] = []

    async def commit(
        self,
        records: Sequence[OIRecord],
        missing: Sequence[OptionContractSpec],
        day: dt.date,
        now: dt.datetime | None = None,
    ) -> ArchiveResult:
        self.calls.append(day)
        self.times.append(now)
        return await super().commit(records, missing, day, now=now)


def stub_sweep(pipeline: InstrumentPipeline, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sweep bez IO pro minutové sekvence OI (#1307).

    Plný `run_cycle` stojí ~70 ms na minutu; OI průchod otevírá `run_minute`
    a čte `advance_oi_archives` po něm (#1646), takže sekvence přes celý víkend
    jde minutu po minutě bez ručního natahování čítače. Hlídače dostanou
    zdravé metriky.
    """

    async def cycle(
        ts_min: dt.datetime, spot: float, bars: object, forming_bar: object = None
    ) -> SweepMetrics:
        return SweepMetrics(
            total=6, greeks_complete=6, repair_count=0, stale_count=0, sweep_duration_s=0.0
        )

    monkeypatch.setattr(pipeline.runtime, "run_cycle", cycle)


async def run_minutes(pipeline: InstrumentPipeline, start: dt.datetime, end: dt.datetime) -> None:
    """Minutové cykly v [start, end) — jako hlavní smyčka enginu."""
    at = start
    while at < end:
        await run_minute_oi(pipeline, at)
        at += dt.timedelta(minutes=1)


def alerts_of(publisher: RecordingPublisher, kinds: set[str]) -> list[dict[str, object]]:
    return [d for ch, d in publisher.messages if ch == "alerts" and d["kind"] in kinds]


def seed_oi(
    repository: OIEodRepository,
    pipeline: InstrumentPipeline,
    day: dt.date,
    captured: dt.datetime,
) -> None:
    repository.upsert_many(
        [
            OIRecord(pipeline.symbol, spec.expiry, spec.strike, spec.right, day, 500.0)
            for spec in pipeline.runtime.contracts
        ],
        captured,
    )


def stalled_metrics(stale: int, stalled: int) -> SweepMetrics:
    return SweepMetrics(
        total=96,
        greeks_complete=96 - stale,
        repair_count=stale,
        stale_count=stale,
        sweep_duration_s=1.0,
        stalled_count=stalled,
    )


async def test_sobota_oi_neobnovuje_ani_nehlasi(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#1307: 26. 9. se od 12:30 UTC à 30 min „obnovoval" sobotní OI archiv.

    Snímek z 00:01 UTC je formálně „před publikací" (okno 07:00 CT se počítalo
    pro každý den), jenže CME za víkend nic nepublikuje a IBKR v sobotu nic
    nevrátí — obnova nemohla uspět a každý pokus odešel jako upozornění.
    """
    settings, writer, repository, publisher = env
    settings.level_alert_near_steps = 0.0
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    day = SAT_NOON.date()
    seed_oi(repository, pipeline, day, SAT_ROLL + dt.timedelta(minutes=1))
    archiver = CountingArchiver(repository, MockOIFetcher(), settings)  # sobota: 0 zapsáno
    pipeline.archiver = archiver
    pipeline._oi_day = day

    assert settings.oi_publication_utc(day) <= SAT_NOON  # okno „uplynulo"…
    assert pipeline._oi_refresh_due(SAT_NOON) is False  # …trh je ale zavřený
    for at in (SAT_NOON, SAT_NOON + dt.timedelta(minutes=31), SAT_NOON + dt.timedelta(hours=3)):
        pipeline._cycles_since_oi = OI_RETRY_CYCLES  # retry by byl na řadě
        await run_minute_oi(pipeline, at)

    assert archiver.calls == []
    assert alerts_of(publisher, OI_KINDS) == []
    # První archiv po restartu enginu v sobotu uložený snímek jen použije
    assert await archive_pass(pipeline, day, SAT_NOON) is True
    assert archiver.calls == []


async def test_vikend_oi_jen_tiche_cteni_prvni_upozorneni_v_pondeli_po_okne(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1307: OI chybí celý víkend a IBKR nic nevrací — minutu po minutě od
    páteční uzávěrky do pondělí po publikačním okně.

    Víkend: tiché čtení na začátku UTC dne (runtime čte OI podle UTC dne) a
    po nedělním otevření retry, ale žádné upozornění — CME v sobotu ani
    v neděli nepublikuje. V noci na pondělí jde o předpublikační stav, taky
    jen log. První (a jediné) upozornění je první selhání po 07:00 CT.
    """
    settings, writer, repository, publisher = env
    settings.level_alert_near_steps = 0.0
    pipeline = make_pipeline(
        "ES", 7600.0, settings, writer, repository, publisher, oi_available=False
    )
    stub_sweep(pipeline, monkeypatch)
    archiver = CountingArchiver(repository, MockOIFetcher(), settings)  # IBKR nic nevrací
    pipeline.archiver = archiver
    pipeline._oi_day = FRI_CLOSE.date()
    monday = MON_MIDNIGHT.date()
    window = settings.oi_publication_utc(monday)

    await run_minutes(pipeline, FRI_CLOSE, SUN_OPEN)
    # Pátek večer a sobota: jediné čtení v půlnoci UTC, v neděli taky
    assert archiver.times == [SAT_ROLL, SAT_ROLL + dt.timedelta(days=1)]

    await run_minutes(pipeline, SUN_OPEN, MON_MIDNIGHT)
    # Otevření v 17:00 CT: chybějící OI se čte hned, pak à 30 min
    assert archiver.times[2] == SUN_OPEN
    assert len(archiver.times) == 6  # 22:00, 22:30, 23:00, 23:30

    await run_minutes(pipeline, MON_MIDNIGHT, window + dt.timedelta(hours=2))
    assert archiver.times[6] == MON_MIDNIGHT  # nový UTC den se čte hned

    alerts = alerts_of(publisher, OI_KINDS)
    assert [a["kind"] for a in alerts] == ["oi_missing"]
    first_after_window = next(t for t in archiver.times if t is not None and t >= window)
    assert alerts[0]["ts"] == first_after_window.timestamp()


async def test_nedele_oi_neobnovuje_pondeli_po_okne_selhani_hlasi(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1307: v neděli CME nepublikuje, takže se snímek neděle neobnovuje ani
    po otevření v 17:00 CT (dřív tu padalo `oi_refresh_failed`). Nová čísla
    přečte čtení nového UTC dne v 19:00 CT; obnova běží až v pondělí po okně
    a její selhání se ohlásí jednou."""
    settings, writer, repository, publisher = env
    settings.level_alert_near_steps = 0.0
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    stub_sweep(pipeline, monkeypatch)
    sunday = SUN_OPEN.date()
    seed_oi(repository, pipeline, sunday, dt.datetime(2026, 9, 27, 0, 1, tzinfo=dt.UTC))
    specs = list(pipeline.runtime.contracts)
    delivering = CountingArchiver(repository, MockOIFetcher(dict.fromkeys(specs, 640.0)), settings)
    pipeline.archiver = delivering
    pipeline._oi_day = sunday
    window = settings.oi_publication_utc(MON_MIDNIGHT.date())

    await run_minutes(pipeline, dt.datetime(2026, 9, 27, 0, 5, tzinfo=dt.UTC), window)
    assert delivering.times == [MON_MIDNIGHT]  # neděle bez obnovy, i po otevření
    assert repository.get_oi("ES", MON_MIDNIGHT.date(), specs[0].strike, specs[0].right) == 640.0

    failing = CountingArchiver(repository, MockOIFetcher(), settings)
    pipeline.archiver = failing
    await run_minutes(pipeline, window, window + dt.timedelta(hours=2))
    assert len(failing.times) >= 3  # obnova à 30 min běží dál
    assert [a["kind"] for a in alerts_of(publisher, OI_KINDS)] == ["oi_refresh_failed"]


async def test_selhani_oi_pred_oknem_nespotrebuje_upozorneni_dne(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1307: hrana OI alertu je per UTC den, který začíná v 19:00 CT. Když
    se ohlásilo už půlnoční (předpublikační) selhání, skutečná porucha „okno
    proběhlo a OI pořád nic" se celý den neohlásila. Pondělí 28. 9.: selhání
    à 30 min od 00:00 do 19:20 UTC → jediné upozornění, první po oknu."""
    settings, writer, repository, publisher = env
    settings.level_alert_near_steps = 0.0
    pipeline = make_pipeline(
        "ES", 7600.0, settings, writer, repository, publisher, oi_available=False
    )
    stub_sweep(pipeline, monkeypatch)
    archiver = CountingArchiver(repository, MockOIFetcher(), settings)
    pipeline.archiver = archiver
    pipeline._oi_day = SUN_OPEN.date()
    window = settings.oi_publication_utc(MON_MIDNIGHT.date())

    await run_minutes(pipeline, MON_MIDNIGHT, window)
    assert archiver.times[0] == MON_MIDNIGHT
    assert alerts_of(publisher, OI_KINDS) == []  # noc před oknem: jen log

    await run_minutes(pipeline, window, dt.datetime(2026, 9, 28, 19, 20, tzinfo=dt.UTC))
    after_window = [t for t in archiver.times if t is not None and t >= window]
    assert len(after_window) >= 14
    alerts = alerts_of(publisher, OI_KINDS)
    assert [a["kind"] for a in alerts] == ["oi_missing"]
    assert alerts[0]["ts"] == after_window[0].timestamp()


async def test_po_denni_pauze_obnovi_nefinalni_oi_hned_v_17_ct(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1307: v denní pauze obnova neběží a čítač retry stojí — bez natažení
    na hraně otevření by první obnova nefinálního snímku přišla až v 17:29 CT."""
    settings, writer, repository, publisher = env
    settings.level_alert_near_steps = 0.0
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    stub_sweep(pipeline, monkeypatch)
    tuesday = dt.date(2026, 9, 29)
    seed_oi(repository, pipeline, tuesday, dt.datetime(2026, 9, 29, 12, 5, tzinfo=dt.UTC))
    specs = list(pipeline.runtime.contracts)
    archiver = CountingArchiver(repository, MockOIFetcher(dict.fromkeys(specs, 700.0)), settings)
    pipeline.archiver = archiver
    pipeline._oi_day = tuesday
    pause_open = dt.datetime(2026, 9, 29, 22, 0, tzinfo=dt.UTC)  # 17:00 CDT

    # 15:45–16:59 CDT: čítač nedoběhne před pauzou, v pauze se nečte
    await run_minutes(pipeline, dt.datetime(2026, 9, 29, 20, 45, tzinfo=dt.UTC), pause_open)
    assert archiver.times == []

    await run_minutes(pipeline, pause_open, pause_open + dt.timedelta(minutes=5))
    assert archiver.times == [pause_open]
    assert alerts_of(publisher, OI_KINDS) == []


async def test_otevreny_trh_selhani_oi_hlasi_hranove(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """Regrese #1307: při OTEVŘENÉM trhu se selhání obnovy hlásí (nic se
    neumlčuje) — jen ne tatáž věta à 30 min: jednou, znovu až po úspěchu."""
    settings, writer, repository, publisher = env
    settings.level_alert_near_steps = 0.0
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    specs = list(pipeline.runtime.contracts)
    po_okne = settings.oi_publication_utc(TS.date()) + dt.timedelta(minutes=5)
    assert not is_market_closed(po_okne)
    failing = CountingArchiver(repository, MockOIFetcher(), settings)
    pipeline.archiver = failing

    async def retry(at: dt.datetime) -> None:
        pipeline._cycles_since_oi = OI_RETRY_CYCLES
        await run_minute_oi(pipeline, at)

    await retry(po_okne)
    await retry(po_okne + dt.timedelta(minutes=31))
    assert len(failing.calls) == 2  # retry běží dál
    assert [a["kind"] for a in alerts_of(publisher, OI_KINDS)] == ["oi_refresh_failed"]

    # Úspěšné čtení re-armuje; další selhání je nová epizoda
    pipeline.archiver = CountingArchiver(
        repository, MockOIFetcher(dict.fromkeys(specs, 700.0)), settings
    )
    await retry(po_okne + dt.timedelta(minutes=62))
    pipeline.archiver = failing
    await retry(po_okne + dt.timedelta(minutes=93))
    assert [a["kind"] for a in alerts_of(publisher, OI_KINDS)] == [
        "oi_refresh_failed",
        "oi_refresh_failed",
    ]


async def test_vanoce_bez_obnovy_oi_i_bez_alertu(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#1308: na Vánoce (pátek 25. 12. 2026) CME nepublikuje OI a Globex je
    zavřený — obnova archivu ani upozornění nemají co hlásit. Do #1308 rozvrh
    svátek neznal a obnova selhávala celý den (#1307 ji zbrzdil hranovým alertem)."""
    settings, writer, repository, publisher = env
    settings.level_alert_near_steps = 0.0
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    day = dt.date(2026, 12, 25)
    seed_oi(repository, pipeline, day, dt.datetime(2026, 12, 25, 0, 5, tzinfo=dt.UTC))
    archiver = CountingArchiver(repository, MockOIFetcher(), settings)
    pipeline.archiver = archiver
    pipeline._oi_day = day

    start = dt.datetime(2026, 12, 25, 13, 5, tzinfo=dt.UTC)  # po okně 07:00 CST
    assert is_market_closed(start)
    assert pipeline._oi_published(start) is False
    for step in range(10):
        pipeline._cycles_since_oi = OI_RETRY_CYCLES
        await run_minute_oi(pipeline, start + dt.timedelta(minutes=31 * step))

    assert archiver.calls == []
    assert alerts_of(publisher, OI_KINDS) == []


@pytest.mark.parametrize(
    "now",
    [
        dt.datetime(2026, 9, 25, 21, 20, tzinfo=dt.UTC),  # pátek 16:20 CDT, po uzávěrce
        dt.datetime(2026, 9, 26, 0, 3, tzinfo=dt.UTC),  # sobota po rollu
        dt.datetime(2026, 9, 27, 21, 30, tzinfo=dt.UTC),  # neděle 16:30 CDT, pre-open
        dt.datetime(2026, 9, 23, 21, 30, tzinfo=dt.UTC),  # středa 16:30 CDT, denní pauza
    ],
)
async def test_zavreny_trh_greeks_ani_striky_nehlasi(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
    now: dt.datetime,
) -> None:
    """#1307: mimo seanci TWS nedodává modelGreeks ani kompletní kotace —
    26. 9. 00:03 „Greeks ES nechodí pro 89 z 122", 00:20 „TWS dlouhodobě
    nedodává kompletní data pro 62 striků NQ"."""
    settings, writer, repository, publisher = env
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    pipeline.runtime.expiry = MONDAY_EXPIRY

    for minute in range(settings.greeks_stall_cycles + 2):
        at = now + dt.timedelta(minutes=minute)
        await pipeline._watch_greeks(at, stalled_metrics(stale=78, stalled=62))
        await pipeline._watch_repair(at, stalled_metrics(stale=78, stalled=62))

    assert alerts_of(publisher, {"greeks_stalled", "strikes_stalled"}) == []


async def test_otevreny_trh_greeks_a_striky_hlasi(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """Regrese #1307: v běžící seanci (čtvrtek 24. 9. 10:00 CDT) se výpadek
    Greeks i trvale selhávající repair hlásí beze změny."""
    settings, writer, repository, publisher = env
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    pipeline.runtime.expiry = MONDAY_EXPIRY
    now = dt.datetime(2026, 9, 24, 15, 0, tzinfo=dt.UTC)

    for minute in range(settings.greeks_stall_cycles):
        at = now + dt.timedelta(minutes=minute)
        await pipeline._watch_greeks(at, stalled_metrics(stale=78, stalled=62))
        await pipeline._watch_repair(at, stalled_metrics(stale=78, stalled=62))

    kinds = {str(a["kind"]) for a in alerts_of(publisher, {"greeks_stalled", "strikes_stalled"})}
    assert kinds == {"greeks_stalled", "strikes_stalled"}
    assert len(alerts_of(publisher, {"greeks_stalled", "strikes_stalled"})) == 2


async def test_otevreni_zahodi_repair_stav_ze_zavreneho_trhu(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#1307: přes víkend scheduler nasbírá repair kola a backoff. Bez resetu
    na hraně otevření by striky začaly seanci v backoffu a první sweep
    v 17:00 CT by hned hlásil `strikes_stalled`. (Že čítače BS fallbacku
    reset přežijí, ověřuje chováním `test_scheduler`.)"""
    settings, writer, repository, publisher = env
    settings.level_alert_near_steps = 0.0
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    specs = list(pipeline.runtime.contracts)
    # TWS dál nedodává (první minuty seance) — rozhoduje jen víkendová historie
    scheduler = SubscriptionScheduler(MockQuoteStreamer(always_fail=set(specs)), settings)
    pipeline.runtime.scheduler = scheduler

    await pipeline.run_minute(SUN_PREOPEN)  # zavřeno — sweep běží, hlídače ne
    far = 1e9
    scheduler._fail_rounds = dict.fromkeys(specs, settings.repair_stall_rounds + 5)
    scheduler._due_at = dict.fromkeys(specs, far)

    await pipeline.run_minute(SUN_OPEN)

    assert max(scheduler._fail_rounds.values()) < settings.repair_stall_rounds
    assert all(due < far for due in scheduler._due_at.values())  # nic v backoffu
    assert alerts_of(publisher, {"strikes_stalled", "greeks_stalled"}) == []


async def test_preopen_bary_nehlasi_po_otevreni_ano(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#1307: neděle 16:00–17:00 CT — indikativní kotace se hýbou, TRADES bary
    nevznikají; 13. a 20. 9. z toho byl `bars_stalled` a obnova streamu.
    Po otevření se hlídání obnoví."""
    settings, writer, repository, publisher = env
    settings.bars_stall_alert_minutes = 2
    settings.level_alert_near_steps = 0.0
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    restarts: list[int] = []

    async def fake_restart() -> None:
        restarts.append(len(restarts) + 1)

    pipeline.restart_bars = fake_restart
    ticker = pipeline.ticker
    assert isinstance(ticker, FakeTicker)

    for minute in range(5):  # 16:55–16:59 CDT, spot se hýbe, bary ne
        ticker.last = 7600.0 + minute
        await pipeline.run_minute(SUN_PREOPEN - dt.timedelta(minutes=4 - minute))
    assert alerts_of(publisher, {"bars_stalled"}) == []
    assert restarts == []

    for minute in range(2):  # 17:00 a 17:01 CDT — bary pořád nechodí
        ticker.last = 7610.0 + minute
        await pipeline.run_minute(SUN_OPEN + dt.timedelta(minutes=minute))
    assert [a["kind"] for a in alerts_of(publisher, {"bars_stalled"})] == ["bars_stalled"]
    assert restarts == [1]


def test_prechod_dst_denni_pauza_v_zime_22_az_23_utc(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#1307: 1. 11. 2026 končí DST — denní pauza 16:00–17:00 CST je 22:00 až
    23:00 UTC; pevný posun by obnovu OI pustil o hodinu dřív do zavřeného trhu."""
    settings, writer, repository, publisher = env
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)

    # Pondělí 2. 11. (CST): 22:30 UTC je v pauze, 23:00 UTC po otevření
    assert pipeline._oi_refresh_due(dt.datetime(2026, 11, 2, 22, 30, tzinfo=dt.UTC)) is False
    assert pipeline._oi_refresh_due(dt.datetime(2026, 11, 2, 23, 0, tzinfo=dt.UTC)) is True
    # Pondělí 26. 10. (CDT): 22:30 UTC = 17:30 CT, pauza už skončila
    assert pipeline._oi_refresh_due(dt.datetime(2026, 10, 26, 22, 30, tzinfo=dt.UTC)) is True
    # Neděle 1. 11. po otevření (23:00 UTC = 17:00 CST): CME v neděli nepublikuje
    assert pipeline._oi_refresh_due(dt.datetime(2026, 11, 1, 23, 0, tzinfo=dt.UTC)) is False


class LoopClock:
    """Čas smyčky enginu v testu (#1646): posouvá ho jen sweep a kroky archivu."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class TimedArchiver(CountingArchiver):
    """Dávka čtení trvá na hodinách smyčky 4 s (22. 10.: Ø 3,6 s), zápis 1 s."""

    def __init__(
        self,
        repository: OIEodRepository,
        fetcher: MockOIFetcher,
        settings: Settings,
        clock: LoopClock,
    ) -> None:
        super().__init__(repository, fetcher, settings)
        self.clock = clock
        self.batches: list[float] = []

    async def fetch_batch(
        self, batch: Sequence[OptionContractSpec], day: dt.date, *, offset: int, total: int
    ) -> tuple[list[OIRecord], list[OptionContractSpec]]:
        self.batches.append(self.clock.t)
        result = await super().fetch_batch(batch, day, offset=offset, total=total)
        self.clock.t += 4.0
        return result

    async def commit(
        self,
        records: Sequence[OIRecord],
        missing: Sequence[OptionContractSpec],
        day: dt.date,
        now: dt.datetime | None = None,
    ) -> ArchiveResult:
        result = await super().commit(records, missing, day, now=now)
        self.clock.t += 1.0
        return result


class SweepGuardFetcher(MockOIFetcher):
    """Čtení OI z IBKR, které si zapíše, když zrovna běží sweep (100 lines, ADR-0001 bod 4).

    Výjimka by se ztratila v `OIArchiver._fetch_one` (chyba čtení = chybějící
    kontrakt), proto se souběh sbírá do seznamu, který test ověří.
    """

    def __init__(self, values: dict[OptionContractSpec, float], sweeping: list[bool]) -> None:
        super().__init__(values)
        self.sweeping = sweeping
        self.during_sweep: list[OptionContractSpec] = []

    async def fetch_snapshot(
        self, spec: OptionContractSpec, timeout_s: float
    ) -> ContractSnapshot | OIMiss:
        if self.sweeping[0]:
            self.during_sweep.append(spec)
        return await super().fetch_snapshot(spec, timeout_s)


def timed_pipeline(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
    oi: float,
    *,
    oi_available: bool = True,
    sweeping: list[bool] | None = None,
) -> tuple[InstrumentPipeline, TimedArchiver, LoopClock]:
    """ES se 6 kontrakty po jednom v dávce — průchod má 6 dávek à 4 s."""
    settings, writer, repository, publisher = env
    settings.level_alert_near_steps = 0.0
    settings.batch_size = 1
    pipeline = make_pipeline(
        "ES", 7600.0, settings, writer, repository, publisher, oi_available=oi_available
    )
    values = dict.fromkeys(pipeline.runtime.contracts, oi)
    fetcher = MockOIFetcher(values) if sweeping is None else SweepGuardFetcher(values, sweeping)
    clock = LoopClock()
    archiver = TimedArchiver(repository, fetcher, settings, clock)
    pipeline.archiver = archiver
    return pipeline, archiver, clock


async def test_obnova_oi_po_davkach_neblokuje_cyklus_ani_sweep(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1646: obnova OI archivu po publikaci stála v cyklu ~100 s a 22. 10. chyběla
    minuta ES i NQ à 31 min (12:30, 13:01 … 14:35 UTC). Iterace hlavní smyčky:
    sweep 40 s, pak dávky ve zbytku minuty — žádná iterace nepřetáhne start další,
    dávka nikdy neběží se sweepem a zápis přijde až po celém průchodu."""
    settings, _, repository, _ = env
    sweeping = [False]
    pipeline, archiver, clock = timed_pipeline(env, 700.0, sweeping=sweeping)
    po_okne = settings.oi_publication_utc(TS.date()) + dt.timedelta(hours=1)
    sweeps: list[float] = []

    async def sweep(
        ts_min: dt.datetime, spot: float, bars: object, forming_bar: object = None
    ) -> SweepMetrics:
        sweeping[0] = True
        sweeps.append(clock.t)
        for _ in range(5):  # pustí ke slovu úlohy na pozadí (joby po archivu)
            await asyncio.sleep(0)
        clock.t += 40.0
        sweeping[0] = False
        return SweepMetrics(
            total=6, greeks_complete=6, repair_count=0, stale_count=0, sweep_duration_s=40.0
        )

    monkeypatch.setattr(pipeline.runtime, "run_cycle", sweep)
    pipeline._cycles_since_oi = OI_RETRY_CYCLES  # obnova je na řadě
    for minute in range(4):
        cycle_start = 60.0 * minute
        clock.t = cycle_start
        await pipeline.run_minute(po_okne + dt.timedelta(minutes=minute))
        await advance_oi_archives([pipeline], cycle_start + 60.0, clock=clock)
        # Iterace končí před rezervou — cyklus další minuty startuje včas
        assert clock.t <= cycle_start + 60.0 - OI_ARCHIVE_RESERVE_S
        if minute == 0:
            assert pipeline._oi_pass is not None and pipeline._oi_pass.offset == 3
            assert archiver.calls == []  # nic se nezapsalo před koncem průchodu
    if pipeline._oi_jobs_task is not None:
        await pipeline._oi_jobs_task

    assert sweeps == [0.0, 60.0, 120.0, 180.0]  # každá minuta má svůj cyklus
    # 3 + 3 dávky ve dvou minutách, zápis ve třetí (na konci druhé se nevešel)
    assert archiver.batches == [40.0, 44.0, 48.0, 100.0, 104.0, 108.0]
    assert archiver.calls == [po_okne.date()]
    assert archiver.times == [po_okne]  # k minutě otevření, jako čtení naráz
    assert pipeline._oi_pass is None
    fetcher = archiver._fetcher
    assert isinstance(fetcher, SweepGuardFetcher)
    assert fetcher.during_sweep == []  # žádné čtení z IBKR souběžně se sweepem
    for spec in pipeline.runtime.contracts:
        assert repository.get_oi("ES", po_okne.date(), spec.strike, spec.right) == 700.0


async def test_finalita_oi_az_po_celem_pruchodu(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#1646: dvě shodná čtení (#463) se srovnávají až po celém průchodu — část
    přečtená v jedné minutě nesmí snímek prohlásit za finální ani nic zapsat."""
    settings, _, repository, _ = env
    pipeline, archiver, clock = timed_pipeline(env, 500.0)
    day = TS.date()
    po_okne = settings.oi_publication_utc(day) + dt.timedelta(minutes=5)
    # Předchozí IBKR čtení po okně se stejnými hodnotami (#1605: řádek s časem)
    repository.upsert_many(
        [
            OIRecord("ES", s.expiry, s.strike, s.right, day, 500.0, trading_class=s.trading_class)
            for s in pipeline.runtime.contracts
        ],
        po_okne - dt.timedelta(minutes=1),
    )
    pipeline._cycles_since_oi = OI_RETRY_CYCLES
    await pipeline.run_minute(po_okne)
    assert pipeline._oi_pass is not None

    # Do zbytku minuty se vejdou dvě dávky
    await advance_oi_archives([pipeline], 8.0 + OI_ARCHIVE_RESERVE_S, clock=clock)
    assert pipeline._oi_pass is not None and pipeline._oi_pass.offset == 2
    assert pipeline.oi_final is False
    assert archiver.calls == []

    await advance_oi_archives([pipeline], math.inf, clock=clock)
    assert archiver.calls == [day]
    assert pipeline.oi_final is True
    assert pipeline.oi_available is True
    assert pipeline._oi_jobs_task is not None  # FA a Forward GEX na pozadí
    await pipeline._oi_jobs_task


async def test_log_pruchodu_po_davkach_nese_pozici_davek_a_duvod_nefinality(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#1650: řádek dávky nese pozici v celém průchodu i v další minutě (D1),
    řádek průchodu důvod nefinality — změněné hodnoty, chybějící dřív archivované
    a pokrytí (D2)."""
    settings, _, repository, _ = env
    pipeline, archiver, clock = timed_pipeline(env, 700.0)
    day = TS.date()
    po_okne = settings.oi_publication_utc(day) + dt.timedelta(minutes=5)
    specs = list(pipeline.runtime.contracts)
    repository.upsert_many(
        [
            OIRecord("ES", s.expiry, s.strike, s.right, day, 500.0, trading_class=s.trading_class)
            for s in specs
        ],
        po_okne - dt.timedelta(minutes=1),
    )
    fetcher = archiver._fetcher
    assert isinstance(fetcher, MockOIFetcher)
    del fetcher.values[specs[0]]  # dřív archivovaný kontrakt teď OI nedodá
    pipeline._cycles_since_oi = OI_RETRY_CYCLES
    await pipeline.run_minute(po_okne)
    caplog.set_level(logging.INFO, logger="gexlens_engine")

    await advance_oi_archives([pipeline], 8.0 + OI_ARCHIVE_RESERVE_S, clock=clock)
    await advance_oi_archives([pipeline], math.inf, clock=clock)

    messages = [r.getMessage() for r in caplog.records]
    positions = [m.split(": dávka ")[1].split(" ")[0] for m in messages if ": dávka " in m]
    # Po průchodu jedna dávka dočtení chybějícího kontraktu (#1674)
    assert positions == [f"{i}–{i}/{len(specs)}" for i in range(1, len(specs) + 1)] + ["1–1/1"]
    assert f"OI archiv ES {day}: dočtení 1 chybějících kontraktů průchodu (#1674)" in messages
    n = len(specs)
    pruchod = f"OI archiv ES {day}: {n - 1} zapsáno, 1 chybí — pokrytí {100 * (n - 1) / n:.1f} %"
    assert f"{pruchod}, změněno {n - 1}, chybí dřív archivovaných 1" in messages
    assert pipeline.oi_final is False


class LateTickFetcher(MockOIFetcher):
    """Tick 101 vybraných kontraktů dorazí až na druhé čtení (#1650, hypotéza 4)."""

    def __init__(
        self, values: dict[OptionContractSpec, float], late: Sequence[OptionContractSpec]
    ) -> None:
        super().__init__(values)
        self.late = set(late)

    async def fetch_snapshot(
        self, spec: OptionContractSpec, timeout_s: float
    ) -> ContractSnapshot | OIMiss:
        if spec in self.late:
            self.late.discard(spec)
            self.fetch_calls.append(spec)
            return OIMiss.NO_OI
        return await super().fetch_snapshot(spec, timeout_s)


async def test_docteni_chybejicich_kontraktu_dovoli_finalitu(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#1674: 23. 10. držely NQ nefinální 4, 2 a 9 náhodně chybějících dřív
    archivovaných kontraktů (FA a α o 1,5 h později). Jedna dávka dočtení před
    zápisem je přečte a průchod se shodnými hodnotami je finální."""
    settings, _, repository, _ = env
    pipeline, archiver, clock = timed_pipeline(env, 500.0)
    settings.batch_size = 2
    day = TS.date()
    po_okne = settings.oi_publication_utc(day) + dt.timedelta(minutes=5)
    specs = list(pipeline.runtime.contracts)
    repository.upsert_many(
        [
            OIRecord("ES", s.expiry, s.strike, s.right, day, 500.0, trading_class=s.trading_class)
            for s in specs
        ],
        po_okne - dt.timedelta(minutes=1),
    )
    late = [specs[1], specs[4]]
    fetcher = LateTickFetcher(dict.fromkeys(specs, 500.0), late)
    archiver = TimedArchiver(repository, fetcher, settings, clock)
    pipeline.archiver = archiver

    assert await archive_pass(pipeline, day, po_okne) is True
    assert archiver.batches == [0.0, 4.0, 8.0, 12.0]  # 3 dávky průchodu + 1 dočtení
    assert fetcher.fetch_calls[len(specs) :] == late
    assert pipeline.oi_final is True


async def test_docteni_jen_jednou_a_ne_nad_davku(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#1674: dočtení je jedna dávka. Kontrakt, který nedodá ani podruhé, zůstane
    chybět (obnova jede dál); víc chybějících než `batch_size` je systémový
    výpadek a dočítat se nezačne."""
    settings, _, _, _ = env
    pipeline, archiver, _ = timed_pipeline(env, 700.0)
    po_okne = settings.oi_publication_utc(TS.date()) + dt.timedelta(minutes=5)
    specs = list(pipeline.runtime.contracts)
    fetcher = archiver._fetcher
    assert isinstance(fetcher, MockOIFetcher)
    del fetcher.values[specs[0]]
    assert await archive_pass(pipeline, TS.date(), po_okne) is True
    assert fetcher.fetch_calls == [*specs, specs[0]]  # jen jedno dočtení

    del fetcher.values[specs[1]]  # 2 chybějící > batch_size 1
    fetcher.fetch_calls.clear()
    assert await archive_pass(pipeline, TS.date(), po_okne) is True
    assert fetcher.fetch_calls == specs


async def test_rozpracovany_pruchod_oi_se_zahodi_novy_den(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#1646: čtení po půlnoci UTC patří novému dni — rozpracovaný včerejšek se
    nezapíše a nový den se začne číst hned od první dávky."""
    pipeline, archiver, clock = timed_pipeline(env, 700.0, oi_available=False)
    before = dt.datetime(2026, 7, 20, 23, 58, tzinfo=dt.UTC)  # 18:58 CDT, trh otevřený
    pipeline._oi_day = before.date()
    pipeline._cycles_since_oi = OI_RETRY_CYCLES
    await pipeline.run_minute(before)
    await advance_oi_archives([pipeline], 4.0 + OI_ARCHIVE_RESERVE_S, clock=clock)
    assert pipeline._oi_pass is not None and pipeline._oi_pass.offset == 1

    await pipeline.run_minute(dt.datetime(2026, 7, 21, 0, 0, tzinfo=dt.UTC))
    assert pipeline._oi_pass is not None
    assert pipeline._oi_pass.day == dt.date(2026, 7, 21)
    assert pipeline._oi_pass.offset == 0
    assert archiver.calls == []  # včerejšek se nezapsal


async def test_rozpracovany_pruchod_oi_se_zahodi_pri_zavreni_trhu(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#1646 + #1307: obnova se při zavřeném trhu nečte — průchod rozpracovaný
    před denní pauzou se zahodí a po otevření v 17:00 CT začne znovu."""
    pipeline, archiver, clock = timed_pipeline(env, 700.0)
    tuesday = dt.date(2026, 9, 29)
    pipeline._oi_day = tuesday
    pred_pauzou = dt.datetime(2026, 9, 29, 20, 58, tzinfo=dt.UTC)  # 15:58 CDT
    assert pipeline._oi_refresh_due(pred_pauzou) is True
    pipeline._cycles_since_oi = OI_RETRY_CYCLES
    await pipeline.run_minute(pred_pauzou)
    await advance_oi_archives([pipeline], 4.0 + OI_ARCHIVE_RESERVE_S, clock=clock)
    assert pipeline._oi_pass is not None and pipeline._oi_pass.offset == 1

    pause = dt.datetime(2026, 9, 29, 21, 0, tzinfo=dt.UTC)  # 16:00 CDT
    assert is_market_closed(pause)
    await pipeline.run_minute(pause)
    assert pipeline._oi_pass is None
    await pipeline.run_minute(pause + dt.timedelta(minutes=30))
    assert pipeline._oi_pass is None  # v pauze se neotevírá

    await pipeline.run_minute(dt.datetime(2026, 9, 29, 22, 0, tzinfo=dt.UTC))  # 17:00 CDT
    assert pipeline._oi_pass is not None and pipeline._oi_pass.offset == 0
    assert archiver.calls == []


async def test_cteni_noveho_dne_v_zavrenem_trhu_dobehne(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#1646 + #1307: jediné čtení v půlnoci UTC o víkendu (runtime čte OI podle
    UTC dne) se otevře v zavřeném trhu — hrana zavření nenastane, takže průchod
    po dávkách doběhne i v dalších minutách."""
    pipeline, archiver, clock = timed_pipeline(env, 640.0)
    await pipeline.run_minute(SAT_ROLL - dt.timedelta(minutes=1))  # pá 23:59 UTC, zavřeno
    await pipeline.run_minute(SAT_ROLL)
    await advance_oi_archives([pipeline], 4.0 + OI_ARCHIVE_RESERVE_S, clock=clock)
    assert pipeline._oi_pass is not None and pipeline._oi_pass.offset == 1

    await pipeline.run_minute(SAT_ROLL + dt.timedelta(minutes=1))
    assert pipeline._oi_pass is not None  # zavřený trh průchod nezahodil
    await advance_oi_archives([pipeline], math.inf, clock=clock)
    assert archiver.calls == [SAT_ROLL.date()]
    assert pipeline.oi_available is True


async def test_stop_pipeline_zahodi_rozpracovany_pruchod_oi(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#1646: roll v settle pipeline zastaví a plán ji založí znovu s prvním
    archivem na pozadí (#1208) — rozpracovaný průchod starého řetězu končí s ní."""
    settings, _, _, _ = env
    pipeline, archiver, clock = timed_pipeline(env, 700.0)
    po_okne = settings.oi_publication_utc(TS.date()) + dt.timedelta(hours=1)
    pipeline._cycles_since_oi = OI_RETRY_CYCLES
    await pipeline.run_minute(po_okne)
    assert pipeline._oi_pass is not None

    pipeline.stop()
    assert pipeline._oi_pass is None
    await advance_oi_archives([pipeline], math.inf, clock=clock)
    assert archiver.batches == [] and archiver.calls == []


async def test_chyba_davky_oi_nezastavi_ostatni_pipeline(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#1646: pád dávky jedné pipeline zahodí jen její průchod; druhá dočte."""
    settings, writer, repository, publisher = env
    healthy, archiver, clock = timed_pipeline(env, 700.0)
    broken = make_pipeline("NQ", 24000.0, settings, writer, repository, publisher)

    class BrokenArchiver(OIArchiver):
        async def fetch_batch(
            self, batch: Sequence[OptionContractSpec], day: dt.date, *, offset: int, total: int
        ) -> tuple[list[OIRecord], list[OptionContractSpec]]:
            raise RuntimeError("mock: neočekávaná chyba dávky")

    broken.archiver = BrokenArchiver(repository, MockOIFetcher(), settings)
    po_okne = settings.oi_publication_utc(TS.date()) + dt.timedelta(hours=1)
    for pipeline in (broken, healthy):
        pipeline._cycles_since_oi = OI_RETRY_CYCLES
        await pipeline.run_minute(po_okne)
        assert pipeline._oi_pass is not None

    await advance_oi_archives([broken, healthy], math.inf, clock=clock)
    assert broken._oi_pass is None
    assert healthy._oi_pass is None
    assert archiver.calls == [po_okne.date()]


async def test_selhani_zapisu_pruchodu_po_davkach_hlasi_jako_cteni_naraz(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#1646 + #494: selhání zápisu po celém průchodu je neúspěšná obnova —
    starší snímek platí, hlásí se hranově."""
    settings, _, _, publisher = env
    pipeline, archiver, clock = timed_pipeline(env, 700.0)

    async def broken_commit(*args: object, **kwargs: object) -> ArchiveResult:
        raise RuntimeError("mock: PG nedostupné")

    archiver.commit = broken_commit  # type: ignore[method-assign]
    po_okne = settings.oi_publication_utc(TS.date()) + dt.timedelta(hours=1)
    pipeline._cycles_since_oi = OI_RETRY_CYCLES
    await pipeline.run_minute(po_okne)
    await advance_oi_archives([pipeline], math.inf, clock=clock)

    assert pipeline._oi_pass is None
    assert pipeline.oi_available is True  # platný starší snímek dne
    assert pipeline.oi_final is False  # obnova se zopakuje dalším retry
    assert [a["kind"] for a in alerts_of(publisher, OI_KINDS)] == ["oi_refresh_failed"]


async def test_watchdog_zahodi_visici_davku_oi_a_pusti_dalsi_pipeline(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#1646 + #219: dávka visící na mrtvém IBKR await nesmí zastavit smyčku."""
    settings, writer, repository, publisher = env
    healthy, archiver, clock = timed_pipeline(env, 700.0)
    stuck = make_pipeline("NQ", 24000.0, settings, writer, repository, publisher)

    class HangingArchiver(OIArchiver):
        async def fetch_batch(
            self, batch: Sequence[OptionContractSpec], day: dt.date, *, offset: int, total: int
        ) -> tuple[list[OIRecord], list[OptionContractSpec]]:
            await asyncio.sleep(3600)
            raise AssertionError("nedosažitelné")

    stuck.archiver = HangingArchiver(repository, MockOIFetcher(), settings)
    po_okne = settings.oi_publication_utc(TS.date()) + dt.timedelta(hours=1)
    for pipeline in (stuck, healthy):
        pipeline._cycles_since_oi = OI_RETRY_CYCLES
        await pipeline.run_minute(po_okne)

    await advance_oi_archives([stuck, healthy], math.inf, clock=clock, timeout_s=0.05)
    assert stuck._oi_pass is None
    assert archiver.calls == [po_okne.date()]


def test_v6_zivy_spot_ze_zdroju_se_starim(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#1600 (pod v6): verdikt a tendence hlasují jen z živé ceny; stará cena je
    chybějící vstup s důvodem, GEX výpočet dál s nejčerstvější známou cenou.
    Varuje se jednou, až po odkladu od startu; pod v5 se nic nemění."""
    settings, writer, repository, publisher = env
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    tuesday = dt.datetime(2026, 10, 20, 15, 0, tzinfo=dt.UTC)  # otevřený trh
    readings = iter(
        [
            SpotReading(7601.25, tuesday, "bar", True),
            SpotReading(7601.25, tuesday - dt.timedelta(seconds=240), "bar", False),
            SpotReading(7601.25, tuesday - dt.timedelta(seconds=300), "bar", False),
            SpotReading(7601.25, tuesday - dt.timedelta(seconds=360), "bar", False),
            SpotReading(7602.0, tuesday, "ibkr", True),
        ]
    )
    pipeline.spot_reading = lambda: next(readings)
    pipeline.spot_age_gate = True
    caplog.set_level(logging.INFO, logger="gexlens_engine")
    minute = dt.timedelta(minutes=1)

    assert pipeline._live_spot(7600.0, False, tuesday) == (7601.25, None)
    assert pipeline.spot == 7601.25
    # V odkladu po startu chybí spot bez varování
    live, reason = pipeline._live_spot(7600.0, False, tuesday + minute)
    assert live is None and reason is not None
    assert reason.startswith("spot: poslední cena 7601.25 (bar) stará")
    assert pipeline.spot == 7601.25  # GEX výpočet s poslední známou cenou
    assert pipeline._live_spot(7600.0, False, tuesday + 3 * minute)[0] is None  # varuje
    assert pipeline._live_spot(7600.0, False, tuesday + 4 * minute)[0] is None  # už ne
    assert pipeline._live_spot(7600.0, False, tuesday + 5 * minute) == (7602.0, None)
    warnings = [r for r in caplog.records if "žádný zdroj nemá živou cenu" in r.getMessage()]
    assert len(warnings) == 1
    assert any("živá cena zpět (ibkr)" in r.getMessage() for r in caplog.records)

    # Pod v5 spot cyklu jako dosud
    pipeline.spot_age_gate = False
    assert pipeline._live_spot(7600.0, False, tuesday) == (7600.0, None)


@pytest.mark.parametrize(
    ("label", "outage_from"),
    [
        ("víkend", dt.datetime(2026, 10, 18, 20, 0, tzinfo=dt.UTC)),
        ("denní pauza", dt.datetime(2026, 10, 20, 21, 10, tzinfo=dt.UTC)),
    ],
)
def test_v6_vypadek_spotu_ze_zavreneho_trhu_varuje_az_po_otevreni(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
    caplog: pytest.LogCaptureFixture,
    label: str,
    outage_from: dt.datetime,
) -> None:
    """#1307 + #1600: při zavřeném trhu se chybějící spot nehlásí a stav se nemění,
    v odkladu 2 min po otevření taky ne; trvá-li výpadek déle, varuje jednou."""
    settings, writer, repository, publisher = env
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    pipeline.spot_reading = lambda: SpotReading(7600.0, outage_from, "ibkr", False)
    pipeline.spot_age_gate = True
    caplog.set_level(logging.INFO, logger="gexlens_engine")
    ts = outage_from
    opened: dt.datetime | None = None
    while opened is None or ts <= opened + dt.timedelta(minutes=5):
        closed = is_market_closed(ts)
        if opened is None and not closed:
            opened = ts
        assert pipeline._live_spot(7600.0, closed, ts)[0] is None, label
        warned = [r for r in caplog.records if "žádný zdroj nemá živou cenu" in r.getMessage()]
        expected = 0 if opened is None or ts < opened + dt.timedelta(minutes=2) else 1
        assert len(warned) == expected, (label, ts)
        ts += dt.timedelta(minutes=1)


class _SpotRecorder:
    """Spotřebitel minutového cyklu, který si zapamatuje předanou cenu."""

    def __init__(self) -> None:
        self.spots: list[float | None] = []

    async def on_minute(self, now: dt.datetime, spot: float | None, *args: object) -> None:
        self.spots.append(spot)


async def test_v6_stav_mapy_a_t6_dostanou_zivou_cenu(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#1657: pod v6 dostanou stav mapy a T6 živou cenu (`live_spot`), bez ní None
    — ne nejčerstvější známou cenu; pod v5 spot cyklu jako dosud."""
    settings, writer, repository, publisher = env
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    map_state, t6 = _SpotRecorder(), _SpotRecorder()
    pipeline.map_state = cast(Any, map_state)
    pipeline.t6_collector = cast(Any, t6)
    pipeline.spot_age_gate = True
    stale = SpotReading(7601.25, TS - dt.timedelta(minutes=10), "bar", False)
    readings = iter([SpotReading(7601.25, TS, "ibkr", True), stale])
    pipeline.spot_reading = lambda: next(readings)

    await pipeline.run_minute(TS)
    await pipeline.run_minute(TS + dt.timedelta(minutes=1))
    assert map_state.spots == [7601.25, None]
    assert t6.spots == [7601.25, None]

    pipeline.spot_age_gate = False
    await pipeline.run_minute(TS + dt.timedelta(minutes=2))
    assert map_state.spots[-1] == t6.spots[-1] == pipeline.spot


def _tasty_rows(pipeline: InstrumentPipeline, repository: OIEodRepository, day: dt.date) -> None:
    """Řádky dne jen z tasty Summary (#828): `tasty_captured_ts`, bez IBKR `captured_ts`."""
    repository.upsert_many(
        [
            OIRecord(pipeline.symbol, c.expiry, c.strike, c.right, day, 321.0)
            for c in pipeline.runtime.contracts
        ],
        tasty_captured_ts=dt.datetime.combine(day, dt.time(0, 0, 10), tzinfo=dt.UTC),
    )


async def test_den_jen_s_tasty_radky_cte_ibkr_od_pulnoci_a_zapise_forward(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1609: tasty zapíše řádky nového UTC dne ve 00:00:10 dřív než cyklus. O
    přeskočení čtení rozhoduje jen IBKR snímek (`captured_ts`), takže čtení od
    00:00 proběhne i před publikací a Forward GEX dne vznikne (dřív `days: []`
    až do obnovy po publikaci)."""
    settings, writer, repository, publisher = env
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    day = dt.date(2026, 7, 16)  # den před expirací řetězu 20260717
    _tasty_rows(pipeline, repository, day)
    assert day in repository.days("ES") and repository.captured_at("ES", day) is None

    specs = pipeline.runtime.contracts
    pipeline.archiver = OIArchiver(repository, MockOIFetcher(dict.fromkeys(specs, 500.0)), settings)
    forward: list[dt.date] = []

    async def record_forward(today: dt.date) -> None:
        forward.append(today)  # pole samo testuje compute/gexforward (mock nemá IV)

    monkeypatch.setattr(pipeline, "_write_forward_field", record_forward)
    midnight = dt.datetime(2026, 7, 16, 0, 1, tzinfo=dt.UTC)  # otevřený trh, před publikací
    assert pipeline._oi_refresh_due(midnight) is False
    assert await archive_pass(pipeline, day, midnight) is True
    assert repository.captured_at("ES", day) is not None  # IBKR řádky dne
    assert repository.get_oi("ES", day, specs[0].strike, specs[0].right) == 500.0
    assert forward == [day]  # Forward GEX dne po čtení od 00:00, ne až po publikaci

    # S IBKR snímkem dne se před publikací znovu nečte (beze změny)
    await pipeline.start_oi_archive(day, midnight + dt.timedelta(minutes=30))
    assert pipeline._oi_pass is None


async def test_den_jen_s_tasty_radky_selhani_po_okne_hlasi_obnovu_ne_chybejici_oi(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#1609: den s tasty řádky má OI (tasty Summary), takže selhání IBKR po
    publikaci je neúspěšná obnova („starší snímek“), ne „GEX/OI vrstvy bez OI“."""
    settings, writer, repository, publisher = env
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    day = dt.date(2026, 7, 16)
    _tasty_rows(pipeline, repository, day)

    class ExplodingFetcher(MockOIFetcher):
        async def fetch_snapshot(
            self, spec: OptionContractSpec, timeout_s: float
        ) -> ContractSnapshot | OIMiss:
            raise RuntimeError("mock: výpadek IBKR")

    pipeline.archiver = OIArchiver(repository, ExplodingFetcher(), settings)
    po_okne = settings.oi_publication_utc(day) + dt.timedelta(minutes=5)
    assert await archive_pass(pipeline, day, po_okne) is True
    kinds = [d["kind"] for ch, d in publisher.messages if ch == "alerts"]
    assert kinds == ["oi_refresh_failed"]


async def test_novy_utc_den_v_sobotu_s_tasty_radky_otevre_pruchod(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#1609 na produkčním případu: víkendové UTC dny mají dnes jen tasty řádky
    (ne 25. 10. 2026 tasty 00:00:10, cyklus pozdě). Nový UTC den v sobotu 00:00
    (zavřený trh, `new_day`) otevře průchod z minutového cyklu i tak."""
    settings, writer, repository, publisher = env
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    saturday = dt.datetime(2026, 7, 18, 0, 0, tzinfo=dt.UTC)
    assert is_market_closed(saturday)
    _tasty_rows(pipeline, repository, saturday.date())
    pipeline._oi_day = saturday.date() - dt.timedelta(days=1)  # běžel páteční UTC den

    await pipeline.run_minute(saturday)
    assert pipeline._oi_pass is not None
    assert pipeline._oi_pass.day == saturday.date()
    assert pipeline._oi_pass.has_snapshot is True  # alerty berou tasty řádky jako OI dne


async def test_den_jen_s_tasty_radky_bez_zapisu_ibkr_obnovi_ulozenou_alfu(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#1609 (ověřovatel): dřív zkratka na dni jen s tasty řádky propsala uloženou
    α (`_run_fa_jobs` → `_restore_alpha`). Průchod, který z IBKR nic nezapíše
    (sobota přes den), joby po archivu nespustí — α se musí propsat i tak."""
    settings, writer, repository, publisher = env
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    alpha_repo = FaAlphaRepository(
        create_engine(f"sqlite+pysqlite:///{settings.data_dir.parent / 'alpha1609.sqlite'}")
    )
    alpha_repo.ensure_schema()
    alpha_repo.set_state("ES", 0.27, 12)
    pipeline.alpha_repository = alpha_repo
    day = dt.date(2026, 7, 16)
    _tasty_rows(pipeline, repository, day)

    # Výchozí MockOIFetcher nevrací nic → 0 zapsáno
    assert await archive_pass(pipeline, day, dt.datetime(2026, 7, 16, 0, 1, tzinfo=dt.UTC))
    assert pipeline.runtime.flow_alpha == pytest.approx(0.27)


def _forward_computed_ts(settings: Settings, day: dt.date) -> object:
    path = settings.derived_dir / "ES" / "gexforward" / f"{day.isoformat()}.parquet"
    return pd.read_parquet(path)["computed_ts"].iloc[0]


async def test_forward_gex_po_finalite_prepocita_po_obnove_tasty_kridel(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#1607: po finalitě už IBKR průchod nepřijde. Obnova tasty křídel
    (`wide_oi_pass` po výpadku tasty přes publikaci) proto Forward GEX přepočítá
    sama, jinak by pole zůstalo do rollu na předchozí publikaci."""
    settings, writer, repository, publisher = env
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    day = dt.date(2026, 7, 16)  # den před expirací řetězu 20260717
    repository.upsert_many(
        [
            OIRecord("ES", c.expiry, c.strike, c.right, day, 400.0, iv=0.2)
            for c in pipeline.runtime.contracts
        ],
        dt.datetime(2026, 7, 16, 12, 30, tzinfo=dt.UTC),
    )
    now = dt.datetime(2026, 7, 16, 15, 0, tzinfo=dt.UTC)
    pipeline.oi_final, pipeline._oi_day = True, day
    await pipeline._write_forward_field(day)
    first = _forward_computed_ts(settings, day)
    path = settings.derived_dir / "ES" / "gexforward" / f"{day.isoformat()}.parquet"
    values_before = pd.read_parquet(path)["values"].iloc[0].tolist()

    # tasty obnoví křídlo mimo obálku IBKR (strike bez IBKR řádku, #828)
    wing = OIRecord("ES", "20260717", 7620.0, "C", day, 5_000.0)
    written = repository.upsert_many([wing], tasty_captured_ts=now)
    assert written == 1
    assert await pipeline.refresh_forward_after_tasty(written, now) is True
    assert _forward_computed_ts(settings, day) != first
    assert pd.read_parquet(path)["values"].iloc[0].tolist() != values_before  # pole vidí křídlo


@pytest.mark.parametrize(
    ("label", "written", "final", "oi_day"),
    [
        ("tasty nic nezapsalo", 0, True, dt.date(2026, 7, 16)),
        ("před finalitou přepočítá další IBKR průchod", 3, False, dt.date(2026, 7, 16)),
        ("finalita patří včerejšku", 3, True, dt.date(2026, 7, 15)),
    ],
)
async def test_forward_gex_bez_finality_ani_zapisu_se_neprepocita(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    written: int,
    final: bool,
    oi_day: dt.date,
) -> None:
    settings, writer, repository, publisher = env
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    calls: list[dt.date] = []

    async def record(today: dt.date) -> None:
        calls.append(today)

    monkeypatch.setattr(pipeline, "_write_forward_field", record)
    pipeline.oi_final, pipeline._oi_day = final, oi_day
    now = dt.datetime(2026, 7, 16, 15, 0, tzinfo=dt.UTC)
    assert await pipeline.refresh_forward_after_tasty(written, now) is False, label
    assert calls == [], label


async def test_forward_gex_po_tasty_pocka_na_bezici_joby_po_archivu(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Joby po archivu (FA, Forward GEX) zapisují týž soubor a řetěz čtou dřív —
    přepočet po tasty jde až po nich, jinak by ho přepsaly starším stavem. Na pozadí
    ve slotu jobů: extended smyčka, která volá, na FA a α nečeká."""
    settings, writer, repository, publisher = env
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    order: list[str] = []

    async def jobs() -> None:
        await asyncio.sleep(0.05)
        order.append("joby")

    async def record(today: dt.date) -> None:
        order.append("forward")

    monkeypatch.setattr(pipeline, "_write_forward_field", record)
    day = dt.date(2026, 7, 16)
    pipeline.oi_final, pipeline._oi_day = True, day
    pipeline._oi_jobs_task = asyncio.create_task(jobs())
    now = dt.datetime(2026, 7, 16, 15, 0, tzinfo=dt.UTC)
    assert await pipeline.refresh_forward_after_tasty(5, now) is True
    assert order == []  # volající nečekal
    follow_up = pipeline._oi_jobs_task
    assert follow_up is not None
    await follow_up
    assert order == ["joby", "forward"]
    assert pipeline._oi_jobs_task is None  # slot uvolněn po doběhnutí


def _wings(
    repository: OIEodRepository, day: dt.date, changed: dt.datetime, oi: float = 900.0
) -> None:
    """Tasty křídla expirace 20260717 mimo obálku IBKR (#828) se změnou v `changed`."""
    repository.upsert_many(
        [
            OIRecord("ES", "20260717", strike, "C", day, oi + strike - 7700.0)
            for strike in (7700.0, 7710.0)
        ],
        tasty_captured_ts=changed,
    )


def _wide(settings: Settings) -> Settings:
    return settings.model_copy(update={"tasty_enabled": True, "tasty_wide_oi": True})


@pytest.mark.parametrize(
    ("label", "now", "previous", "previous_oi", "expected"),
    [
        ("po okně, křídla jiná než včera", (2026, 7, 16, 14), (2026, 7, 15), 800.0, True),
        ("po okně, křídla stejná jako včera", (2026, 7, 16, 14), (2026, 7, 15), 900.0, False),
        ("pondělí proti pátku (víkend se přeskočí)", (2026, 7, 13, 14), (2026, 7, 10), 800.0, True),
        ("před publikačním oknem", (2026, 7, 16, 9), (2026, 7, 15), 900.0, None),
        ("sobota", (2026, 7, 18, 14), (2026, 7, 17), 900.0, None),
        ("neděle po otevření", (2026, 7, 19, 22), (2026, 7, 17), 900.0, None),
        ("svátek NYSE (pá 3. 7.)", (2026, 7, 3, 14), (2026, 7, 2), 900.0, None),
    ],
)
async def test_stari_tasty_kridel_ve_statusu(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
    label: str,
    now: tuple[int, int, int, int],
    previous: tuple[int, int, int],
    previous_oi: float,
    expected: bool | None,
) -> None:
    """#1611 (Rozhodnuto 26. 10., a): `wide_oi_published` = křídla se liší od téže
    expirace předchozího obchodního dne; None, když publikaci dne nečekáme."""
    settings, writer, repository, publisher = env
    pipeline = make_pipeline("ES", 7600.0, _wide(settings), writer, repository, publisher)
    at = dt.datetime(*now, 30, tzinfo=dt.UTC)
    before = dt.date(*previous)
    evening = dt.datetime.combine(before, dt.time(20, 1), tzinfo=dt.UTC)
    _wings(repository, before, evening, previous_oi)
    if label.startswith("pondělí"):
        # Sobota a neděle nesou kopii se stejnými hodnotami jako pondělí — srovnání je přeskočí
        for weekend in (dt.date(2026, 7, 11), dt.date(2026, 7, 12)):
            _wings(repository, weekend, dt.datetime.combine(weekend, dt.time(0, 1), tzinfo=dt.UTC))
    last = at.replace(hour=0, minute=1)
    _wings(repository, at.date(), last)
    await pipeline._watch_wide_oi(at)
    assert pipeline.wide_oi_state == (last, expected), label


async def test_stari_tasty_kridel_bez_kridel_bez_predchoziho_dne_a_bez_tasty_se_nemeri(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """Den bez křídel (ad-hoc pohled) ani expirace bez předchozího obchodního dne
    nejsou „zastaralé“, ale neměří se; bez tasty se stav nepočítá vůbec."""
    settings, writer, repository, publisher = env
    now = dt.datetime(2026, 7, 16, 14, 0, tzinfo=dt.UTC)
    pipeline = make_pipeline("ES", 7600.0, _wide(settings), writer, repository, publisher)
    await pipeline._watch_wide_oi(now)
    assert pipeline.wide_oi_state == (None, None)
    last = dt.datetime(2026, 7, 16, 0, 1, tzinfo=dt.UTC)
    _wings(repository, now.date(), last)
    await pipeline._watch_wide_oi(now)
    state: object = pipeline.wide_oi_state  # mypy zúžil typ po prvním assertu
    assert state == (last, None)  # 15. 7. expirace v archivu není
    for update in ({"tasty_wide_oi": False}, {"tasty_enabled": False}):
        off = make_pipeline(
            "ES", 7600.0, settings.model_copy(update=update), writer, repository, publisher
        )
        await off._watch_wide_oi(now)
        assert off.wide_oi_state is None, update
    assert [d for ch, d in publisher.messages if ch == "alerts"] == []


def test_posledni_zmena_kridel_pomine_radek_prepsany_z_ibkr(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """Řádek přepsaný z IBKR si nechá starý tasty čas (stráž #1605) — křídlo už to
    není, a proto `wings_last_change` místo `archive_version[1]`."""
    settings, writer, repository, publisher = env
    day = dt.date(2026, 7, 16)
    _wings(repository, day, dt.datetime(2026, 7, 16, 2, 0, tzinfo=dt.UTC))
    repository.upsert_many(
        [OIRecord("ES", "20260717", 7700.0, "C", day, 950.0)],
        tasty_captured_ts=dt.datetime(2026, 7, 16, 5, 0, tzinfo=dt.UTC),
    )  # tasty změna 05:00 …
    repository.upsert_many(
        [OIRecord("ES", "20260717", 7700.0, "C", day, 960.0)],
        dt.datetime(2026, 7, 16, 12, 30, tzinfo=dt.UTC),
    )  # … a pak ho přepsal IBKR (řádek nese oba časy)
    assert repository.wings_last_change("ES", "20260717", day) == dt.datetime(
        2026, 7, 16, 2, 0, tzinfo=dt.UTC
    )
    assert repository.archive_version("ES", "20260717", day)[1] == dt.datetime(
        2026, 7, 16, 5, 0, tzinfo=dt.UTC
    )


async def test_srovnani_kridel_jen_pri_nove_zmene(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Podmínka vlastníka (Rozhodnuto 26. 10.): srovnání s předchozím dnem nesmí
    zatížit cyklus — běží jen při nové změně křídel, jinak jen dotaz na čas změny."""
    settings, writer, repository, publisher = env
    pipeline = make_pipeline("ES", 7600.0, _wide(settings), writer, repository, publisher)
    yesterday = dt.datetime(2026, 7, 15, 20, 1, tzinfo=dt.UTC)
    _wings(repository, dt.date(2026, 7, 15), yesterday, 800.0)
    _wings(repository, dt.date(2026, 7, 16), dt.datetime(2026, 7, 16, 0, 1, tzinfo=dt.UTC))
    calls: list[dt.date] = []
    original = repository.wings_changed_vs

    def counting(symbol: str, expiry: str, day: dt.date, previous: dt.date) -> tuple[int, int]:
        calls.append(previous)
        return original(symbol, expiry, day, previous)

    monkeypatch.setattr(repository, "wings_changed_vs", counting)
    now = dt.datetime(2026, 7, 16, 14, 0, tzinfo=dt.UTC)
    for minute in range(5):
        await pipeline._watch_wide_oi(now + dt.timedelta(minutes=minute))
    assert calls == [dt.date(2026, 7, 15)]
    later = dt.datetime(2026, 7, 16, 14, 7, tzinfo=dt.UTC)
    _wings(repository, dt.date(2026, 7, 16), later, 1000.0)
    await pipeline._watch_wide_oi(now + dt.timedelta(minutes=8))
    assert len(calls) == 2


async def test_alert_kridla_neobnovena_po_publikaci_jednou_za_den(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#1611 (Rozhodnuto 26. 10., A): alert v publikace + 60 min, jen při otevřeném
    trhu v obchodní den, nejvýš jednou za den."""
    settings, writer, repository, publisher = env
    settings = _wide(settings)
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    day = dt.date(2026, 7, 16)
    _wings(repository, dt.date(2026, 7, 15), dt.datetime(2026, 7, 15, 20, 1, tzinfo=dt.UTC))
    _wings(repository, day, dt.datetime(2026, 7, 16, 0, 1, tzinfo=dt.UTC))  # beze změny
    publication = settings.oi_publication_utc(day)

    def alerts() -> list[dict[str, object]]:
        return [d for ch, d in publisher.messages if ch == "alerts"]

    await pipeline._watch_wide_oi(publication + dt.timedelta(minutes=59))
    assert alerts() == []  # do 60 min po okně se nehlásí
    await pipeline._watch_wide_oi(publication + dt.timedelta(minutes=60))
    await pipeline._watch_wide_oi(publication + dt.timedelta(minutes=61))
    assert [a["kind"] for a in alerts()] == ["wide_oi_stale"]
    assert "00:01 UTC" in str(alerts()[0]["message"])


async def test_alert_kridel_v_denni_pauze_nejde(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """AGENTS.md: zavřený trh = žádná upozornění na chybějící data. Denní pauza
    v obchodní den po okně je jediný zavřený trh, kde `_oi_published` platí."""
    settings, writer, repository, publisher = env
    pipeline = make_pipeline("ES", 7600.0, _wide(settings), writer, repository, publisher)
    pause = dt.datetime(2026, 7, 16, 21, 10, tzinfo=dt.UTC)
    assert is_market_closed(pause) and pipeline._oi_published(pause)
    _wings(repository, dt.date(2026, 7, 15), dt.datetime(2026, 7, 15, 20, 1, tzinfo=dt.UTC))
    _wings(repository, pause.date(), dt.datetime(2026, 7, 16, 0, 1, tzinfo=dt.UTC))
    await pipeline._watch_wide_oi(pause)
    assert pipeline.wide_oi_state is not None and pipeline.wide_oi_state[1] is False
    assert [d for ch, d in publisher.messages if ch == "alerts"] == []


def test_wide_oi_status_serializuje_stav_per_symbol(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """`/status` nese `wide_oi_last_change_ts` (ISO nebo null) a `wide_oi_published`
    per symbol; pipeline bez stavu klíč nemá a bez stavu vůbec se klíče nepošlou."""
    from gexlens_engine.__main__ import _wide_oi_status

    settings, writer, repository, publisher = env
    es = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    nq = make_pipeline("NQ", 24000.0, settings, writer, repository, publisher)
    assert _wide_oi_status([es, nq]) == {}
    es.wide_oi_state = (dt.datetime(2026, 7, 16, 2, 0, tzinfo=dt.UTC), False)
    nq.wide_oi_state = (None, None)
    assert _wide_oi_status([es, nq]) == {
        "wide_oi_last_change_ts": {"ES": "2026-07-16T02:00:00+00:00", "NQ": None},
        "wide_oi_published": {"ES": False, "NQ": None},
    }
