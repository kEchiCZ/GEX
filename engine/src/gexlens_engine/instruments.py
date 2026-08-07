"""Multi-instrument vrstva enginu (ADR-0003): pipeline per podklad řízená watchlistem.

Cílová sada instrumentů = základ z konfigurace (GEXLENS_SYMBOLS) + watchlist z DB
(uživatel přidává tickery v sidebaru). Orchestrátor v `__main__` každý cyklus
plánuje start/stop pipeline; sweepy běží sekvenčně, takže špička market data
lines zůstává jedna dávka (batch_size) bez ohledu na počet instrumentů.

Podporované podklady: futures s FOP řetězcem (ES, NQ, RTY, CL, …). Akcie/indexy
zatím ne — discovery podkladu hledá jen futures kontrakty (ADR-0003).
"""

import asyncio
import datetime as dt
import logging
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.engine import Engine

from gexlens_engine.compute.setups import SETUP_MECHANICS_VERSION
from gexlens_engine.compute.setupstats import (
    SetupParamsStats,
    aggregate,
    degraded,
    format_report,
)
from gexlens_engine.compute.volleaders import detect_concentration
from gexlens_engine.config import Settings
from gexlens_engine.ibkr.discovery import (
    ChainDiscovery,
    ExpiryInfo,
    OptionContractSpec,
    StrikeBand,
    Underlying,
    build_contracts,
)
from gexlens_engine.ibkr.newsticks import NewsTickCollector, NewsTickLike
from gexlens_engine.ibkr.scheduler import (
    GreeksStallDetector,
    RepairStallDetector,
    SweepMetrics,
)
from gexlens_engine.ibkr.underlying import Bar, BarsStallDetector
from gexlens_engine.runtime import EngineRuntime, PublisherLike
from gexlens_engine.setups import SetupEngine
from gexlens_engine.storage.fa_calibration import FaAlphaRepository, collect_alpha_calibration
from gexlens_engine.storage.fa_validation import FaValidationRepository, collect_fa_validation
from gexlens_engine.storage.meta import meta_metadata, settings_table, watchlist_table
from gexlens_engine.storage.oi_archive import OIArchiver, OIEodRepository
from gexlens_engine.t6 import T6Collector
from gexlens_engine.tendency import TendencyEngine

logger = logging.getLogger(__name__)

# OI retry každých ~30 minutových cyklů (CME publikuje OI jednou denně ráno, ADR-0001)
OI_RETRY_CYCLES = 30
# Strop odkladu po opakovaném selhání setupu (neznámý symbol) — cyklů do dalšího pokusu
SETUP_RETRY_CYCLES = 30
# První odklad (#457): dočasná příčina (přetažená market data konkurenční relací)
# stojí jednu minutu, ne půl hodiny. Eskaluje se až opakováním.
SETUP_RETRY_FIRST_CYCLES = 1
# Watchdog minutového cyklu (#219): sweep bez timeoutu umí po výpadku IBKR viset
# navždy (future se nikdy nevyřeší) a zastavit celý orchestrátor. Běžný sweep
# trvá 0.5–35 s; strop je velkorysý, aby nezabíjel legitimní první sweep.
CYCLE_TIMEOUT_S = 240.0


class InstrumentSetupError(RuntimeError):
    """Instrument nejde nastartovat (neznámý symbol, chybí FOP řetězec, …)."""


class SetupCooldown:
    """Odklad dalšího pokusu o setup instrumentu po selhání.

    Chrání před zakládáním pipeline u symbolu, který nastartovat NEJDE (neznámý
    ticker, chybějící řetězec) — takový pokus by jinak každou minutu pálil
    `reqContractDetails` + discovery ze sdíleného pacing budgetu a publikoval
    stejný alert `instrument_error`.

    Odklad se ale eskaluje, nezačíná na stropu (#457): 1 → 2 → 4 → … → `max_cycles`.
    Dočasná příčina (přetažená market data konkurenční relací) tak stojí jednu
    minutu, kdežto trvale vadný symbol se sám utlumí na původních 30 cyklů.
    Úspěšný setup eskalaci nuluje.

    Rozpadlé spojení chybou symbolu není (#455): po přepojení musí setup dostat
    šanci hned, jinak uživatel opraví konfiguraci a graf zůstane prázdný celý
    cooldown. Proto `clear()` — volá se po reconnectu i po změně parametrů
    spojení ze Settings.
    """

    def __init__(
        self,
        max_cycles: int = SETUP_RETRY_CYCLES,
        first_cycles: int = SETUP_RETRY_FIRST_CYCLES,
    ) -> None:
        self._max_cycles = max_cycles
        self._first_cycles = first_cycles
        self._remaining: dict[str, int] = {}
        # Délka PŘÍŠTÍHO odkladu; drží se i po vypršení, jinak by se série
        # selhání nikdy neeskalovala (každý pokus by začínal od jedničky)
        self._next_cycles: dict[str, int] = {}

    def penalize(self, symbol: str) -> int:
        """Odloží symbol a vrátí počet cyklů (kvůli logu)."""
        delay = self._next_cycles.get(symbol, self._first_cycles)
        self._remaining[symbol] = delay
        self._next_cycles[symbol] = min(delay * 2, self._max_cycles)
        return delay

    def succeeded(self, symbol: str) -> None:
        """Setup prošel — další případné selhání začíná zase od nejkratšího odkladu."""
        self._remaining.pop(symbol, None)
        self._next_cycles.pop(symbol, None)

    def tick(self) -> None:
        """Odečte cyklus; symbol s vyčerpaným odkladem je zase způsobilý."""
        for symbol in list(self._remaining):
            self._remaining[symbol] -= 1
            if self._remaining[symbol] <= 0:
                del self._remaining[symbol]

    def blocked(self, symbol: str) -> bool:
        return symbol in self._remaining

    def clear(self) -> tuple[str, ...]:
        """Zruší odklady i eskalaci; vrací uvolněné symboly kvůli logu.

        Eskalace se maže taky — série selhání patřila starému spojení a na novém
        o ničem nevypovídá.
        """
        released = tuple(self._remaining)
        self._remaining.clear()
        self._next_cycles.clear()
        return released


def expiry_expired(expiry: str, today: dt.date) -> bool:
    """True, když expirace (YYYYMMDD) už proběhla — pipeline se musí překlopit.

    0DTE řetěz: po vypršení denní expirace by sweep běžel nad mrtvými kontrakty;
    orchestrátor pipeline zastaví a další cyklus ji založí znovu (discovery
    vybere novou nejbližší expiraci). Nečitelný formát → False (nerozbíjet běh).
    """
    try:
        expiry_date = dt.datetime.strptime(expiry, "%Y%m%d").date()
    except ValueError:
        logger.warning("Nečitelná expirace %r — roll se přeskakuje", expiry)
        return False
    return expiry_date < today


class TickerLike(Protocol):
    """Minimální podoba ib_async.Ticker pro čtení spotu podkladu."""

    @property
    def last(self) -> float: ...

    def marketPrice(self) -> float: ...


def parse_multiplier(raw: str | None) -> float:
    """Multiplikátor kontraktu z IBKR (string, např. "50"); nevalidní → 1.0 s varováním."""
    if raw is None or not str(raw).strip():
        return 1.0
    try:
        return float(str(raw).strip())
    except ValueError:
        logger.warning("Nečitelný multiplikátor %r — používám 1.0", raw)
        return 1.0


def merge_symbols(base: Sequence[str], watchlist: Sequence[str]) -> list[str]:
    """Cílová sada instrumentů: základ z konfigurace první, pak watchlist; dedup, uppercase."""
    seen: list[str] = []
    for raw in [*base, *watchlist]:
        symbol = raw.strip().upper()
        if symbol and symbol not in seen:
            seen.append(symbol)
    return seen


@dataclass(frozen=True)
class InstrumentPlan:
    """Plán změn běžících pipeline pro jeden cyklus."""

    start: list[str]
    stop: list[str]
    # Nad strop max_instruments — neběží a UI o tom ví (alert řeší orchestrátor)
    skipped: list[str]


def plan_instruments(
    running: Collection[str], desired: Sequence[str], max_instruments: int
) -> InstrumentPlan:
    """Rozdíl mezi běžícími a cílovými instrumenty s respektem ke stropu.

    Priorita při stropu = pořadí v `desired` (základ z konfigurace je první).
    """
    capped = list(desired[:max_instruments])
    skipped = [symbol for symbol in desired[max_instruments:]]
    start = [symbol for symbol in capped if symbol not in running]
    stop = [symbol for symbol in running if symbol not in capped]
    return InstrumentPlan(start=start, stop=stop, skipped=skipped)


class WatchlistReader:
    """Čtení watchlistu z metadata DB (tabulku vlastní engine — SPEC 5.3)."""

    def __init__(self, db: Engine) -> None:
        self._db = db

    def ensure_schema(self) -> None:
        meta_metadata.create_all(self._db)

    def symbols(self) -> list[str]:
        with self._db.connect() as conn:
            rows = conn.execute(
                select(watchlist_table.c.symbol).order_by(watchlist_table.c.id)
            ).fetchall()
        return [str(row[0]) for row in rows]

    def setting(self, key: str) -> object | None:
        """Runtime hodnota ze settings tabulky (UI ukládá přes PUT /settings)."""
        with self._db.connect() as conn:
            row = conn.execute(
                select(settings_table.c.value).where(settings_table.c.key == key)
            ).fetchone()
        return None if row is None else row[0]

    def settings_map(self, keys: Sequence[str]) -> dict[str, object]:
        """Víc klíčů najednou (#438) — jeden dotaz místo N na každý poll cyklus."""
        if not keys:
            return {}
        with self._db.connect() as conn:
            rows = conn.execute(
                select(settings_table.c.key, settings_table.c.value).where(
                    settings_table.c.key.in_(list(keys))
                )
            ).fetchall()
        return {str(row[0]): row[1] for row in rows}


@dataclass
class InstrumentPipeline:
    """Běžící pipeline jednoho podkladu: řetězec, obálka, runtime, OI archiv, bary.

    Všechny závislosti jsou injektované — pipeline je testovatelná nad mocky;
    produkční sestavení nad ib_async dělá `create_pipeline` v `__main__`.
    """

    symbol: str
    settings: Settings
    publisher: PublisherLike
    discovery: ChainDiscovery
    info: ExpiryInfo
    band: StrikeBand
    runtime: EngineRuntime
    archiver: OIArchiver
    oi_repository: OIEodRepository
    ticker: TickerLike
    minute_bars: list[Bar]
    # Rozdělaná minuta z agregátoru 5s barů (ADR-0005); None = zdroj ji neposkytuje
    forming_bar: Callable[[], Bar | None] = lambda: None
    on_stop: Callable[[], None] = lambda: None
    spot: float = 0.0
    oi_available: bool = False
    # Snímek OI je definitivní (#463): pořízený po publikačním okně a potvrzený
    # druhým nezměněným čtením. Dokud ne, engine ho po okně obnovuje — jinak by
    # se celý den držela předpublikační čísla, která vypadají jako platná data.
    oi_final: bool = False
    # OI archiv pokrývá i další expirace (ΔOI vs. včera); None = jen aktivní řetěz
    archive_contracts: Sequence[OptionContractSpec] | None = None
    # Sekundární runtime následující expirace (čtení positioningu příští seance)
    next_runtime: EngineRuntime | None = None
    # Obálka sekundáru se musí roztahovat stejně jako aktivní (#442) — bez toho
    # zamrzla na hodnotě ze startu pipeline a při trendovém dni cena utekla nad
    # horní strike (3. 8.: pásmo 28290–28680, cena 28925 → graf uříznutý)
    next_info: ExpiryInfo | None = None
    next_band: StrikeBand | None = None
    # Setup detektor (ADR-0004) — None = vypnuto
    setup_engine: SetupEngine | None = None
    # Indikátor tendence (#350) — None = vypnuto
    tendency_engine: TendencyEngine | None = None
    # Sběrač kandidátů T6 (#256) — None = vypnuto
    t6_collector: T6Collector | None = None
    # Denní FA validace po OI archivu (#232) — None = vypnuto
    fa_repository: FaValidationRepository | None = None
    # Ranní kalibrace α po OI archivu (#232 fáze 2) — None = vypnuto
    alpha_repository: FaAlphaRepository | None = None
    # Hlídání tiché ztráty 5s barů (#221); default z konfigurace v __post_init__
    stall_detector: BarsStallDetector | None = None
    # Hlídání tiché ztráty Greeks (#306); default z konfigurace v __post_init__
    greeks_detector: GreeksStallDetector | None = None
    # Trvale selhávající repair kontraktů (#547); default v __post_init__
    repair_detector: RepairStallDetector | None = None
    # Broker headlines z ticku 292 (#291); None = live news vypnuté
    news_ticks: NewsTickCollector | None = None
    read_news_ticks: Callable[[], Sequence[NewsTickLike]] | None = None
    # Re-backfill dnešních barů po návratu streamu (#221); None = backfill nezapojen
    backfill_today: Callable[[], Awaitable[None]] | None = None
    _cycles_since_oi: int = field(default=0, repr=False)
    # Den, ke kterému patří `oi_available`/`oi_final` (#494): pipeline symbolu
    # s nedenní nejbližší expirací přežije půlnoc a bez resetu by včerejší
    # finalita blokovala archivaci nového dne navždy.
    _oi_day: dt.date | None = field(default=None, repr=False)
    _minute_count: int = field(default=0, repr=False)
    _last_spot: float = field(default=float("nan"), repr=False)
    _backfill_task: asyncio.Task[None] | None = field(default=None, repr=False)
    # Vol koncentrace (#208): už ohlášené strany (expirace, strike, right) —
    # jeden alert per leader; pipeline se denně překlápí, reset je přirozený
    _vol_alerted: set[tuple[str, float, str]] = field(default_factory=set, repr=False)
    # Sebekontrola setupů (#309): den posledního běhu a zda je hlášené zhoršení
    _selfcheck_day: dt.date | None = field(default=None, repr=False)
    _selfcheck_degraded: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        if self.stall_detector is None:
            self.stall_detector = BarsStallDetector(self.settings.bars_stall_alert_minutes)
        if self.greeks_detector is None:
            self.greeks_detector = GreeksStallDetector(
                self.settings.greeks_stall_share, self.settings.greeks_stall_cycles
            )
        if self.repair_detector is None:
            self.repair_detector = RepairStallDetector()

    async def _archive_new_strikes(
        self,
        previous: Sequence[OptionContractSpec],
        current: Sequence[OptionContractSpec],
        today: dt.date,
    ) -> None:
        """Doarchivuje OI striků, které přibyly rozšířením obálky (#465).

        Denní archiv pokrývá pásmo z okamžiku archivace; auto-rozšíření (ADR-0002)
        ho během dne posouvá za cenou. Bez doplnění mají nové striky `get_oi`
        None, tedy v grafu nulu — přitom je nikdo nezměřil. `current` je
        explicitní, aby stejná cesta pokryla aktivní i sekundární řetěz (#494).

        Selhání nesmí shodit cyklus: OI je doplněk, sběr kotací běží dál.
        """
        known = {(spec.strike, spec.right) for spec in previous}
        fresh = [spec for spec in current if (spec.strike, spec.right) not in known]
        if not fresh:
            return
        try:
            result = await self.archiver.archive_day(fresh, today)
        except Exception:
            logger.exception("Doarchivace nových striků %s selhala — zkusí se příště", self.symbol)
            return
        logger.info(
            "Obálka %s se rozšířila o %d kontraktů: %d s OI, %d bez",
            self.symbol,
            len(fresh),
            result.written,
            len(result.missing),
        )

    def _archive_universe(self) -> list[OptionContractSpec]:
        """Kontrakty pro denní archiv: snímek z discovery ∪ aktuální obálky (#494).

        `archive_contracts` je statický snímek z `initial_band` při založení
        pipeline; expanze obálky (aktivní i sekundární) během dne přidává
        striky, které v něm nejsou. Post-publikační obnova je musí číst taky,
        jinak nové striky jedou celý den na předpublikačním OI.
        """
        universe: list[OptionContractSpec] = list(self.archive_contracts or ())
        seen = {(spec.expiry, spec.strike, spec.right) for spec in universe}
        sources: list[Sequence[OptionContractSpec]] = [self.runtime.contracts]
        if self.next_runtime is not None:
            sources.append(self.next_runtime.contracts)
        for source in sources:
            for spec in source:
                key = (spec.expiry, spec.strike, spec.right)
                if key not in seen:
                    seen.add(key)
                    universe.append(spec)
        return universe

    def _oi_refresh_due(self, now: dt.datetime) -> bool:
        """Má se existující snímek dne přečíst znovu? (#463)

        Před publikačním oknem nemá smysl číst — IBKR finální čísla ještě nemá.
        Po okně se čte, dokud dvě po sobě jdoucí čtení nedají totéž (`oi_final`);
        potvrzení se do DB neukládá, takže po restartu proběhne jedno kontrolní
        čtení navíc. To je levnější než další stav v schématu.
        """
        return now >= self.settings.oi_publication_utc(now.date()) and not self.oi_final

    async def try_archive_oi(self, today: dt.date, now: dt.datetime | None = None) -> bool:
        """Denní OI archiv; při úplném selhání alert do UI (ADR-0001 v2).

        Po #463 archivace nekončí prvním úspěchem: snímek z doby před publikací
        IBKR nese neúplná čísla (4. 8. 2026 tak celý den běžel GEX na půlnočním
        stavu, kde put strana měla Σ OI 1 877 proti 29 282 na call straně).
        `now` je injektovatelné kvůli testům — rozhodnutí o obnově závisí na
        hodině, takže by test jinak platil jen část dne.
        """
        now = now or dt.datetime.now(dt.UTC)
        self._oi_day = today
        # Platný denní archiv už existuje → případné selhání níže je jen
        # neúspěšná OBNOVA, ne ztráta OI (#494) — jede se dál na starším snímku
        has_snapshot = today in self.oi_repository.days(self.symbol)
        if has_snapshot and not self._oi_refresh_due(now):
            await self._run_fa_validation(today)
            await self._run_alpha_calibration(today)
            return True
        captured = self.oi_repository.captured_at(self.symbol, today)
        if captured is not None and captured < self.settings.oi_publication_utc(captured.date()):
            logger.info(
                "OI archiv %s %s je z %s UTC, tedy před publikací — obnovuji",
                self.symbol,
                today,
                captured.strftime("%H:%M"),
            )
        contracts = self._archive_universe()
        try:
            result = await self.archiver.archive_day(contracts, today, now=now)
        except Exception:
            # Selhání archivace nesmí zabít pipeline (#215: MES CardinalityViolation
            # shodil celý řetěz do cooldownu) — sběr běží dál s volume fallbackem,
            # retry po OI_RETRY_CYCLES cyklech
            logger.exception("OI archivace %s selhala — pokračuje se bez OI", self.symbol)
            if has_snapshot:
                return await self._report_refresh_failed(today)
            await self.publisher.publish(
                "alerts",
                {
                    "kind": "oi_missing",
                    "symbol": self.symbol,
                    "message": f"OI archivace {self.symbol} selhala — GEX/OI vrstvy zatím "
                    "bez OI, další pokus za 30 min (detail v logu enginu)",
                    "ts": dt.datetime.now(dt.UTC).timestamp(),
                },
            )
            return False
        logger.info(
            "OI archiv %s %s: %d zapsáno, %d chybí",
            self.symbol,
            today,
            result.written,
            len(result.missing),
        )
        if result.written == 0:
            if has_snapshot:
                return await self._report_refresh_failed(today)
            await self.publisher.publish(
                "alerts",
                {
                    "kind": "oi_missing",
                    "symbol": self.symbol,
                    "message": f"OI pro {self.symbol} z IBKR nedorazilo — GEX/OI vrstvy "
                    "zatím bez OI, další pokus za 30 min (CME publikuje OI ráno)",
                    "ts": dt.datetime.now(dt.UTC).timestamp(),
                },
            )
            return False
        # Finální je snímek pořízený po publikačním okně, jehož hodnoty se proti
        # předchozímu čtení nezměnily — jedno čtení po okně nestačí, publikace
        # může doběhnout zrovna mezi dvěma dávkami sweepu
        if now >= self.settings.oi_publication_utc(now.date()) and not result.changed:
            self.oi_final = True
            logger.info("OI archiv %s %s je finální (dvě shodná čtení)", self.symbol, today)
        await self._run_fa_validation(today)
        await self._run_alpha_calibration(today)
        await self._run_setup_selfcheck(today)
        return True

    async def _report_refresh_failed(self, today: dt.date) -> bool:
        """Neúspěšná post-publikační OBNOVA při existujícím denním archivu (#494).

        Přechodný výpadek fetche po publikaci nesmí shodit `oi_available` ani
        hlásit „GEX/OI vrstvy bez OI" — platný (byť starší) snímek dne existuje
        a vrstvy z něj jedou dál. `oi_final` zůstává False, takže se obnova
        zopakuje dalším retry cyklem.
        """
        logger.warning(
            "Obnova OI archivu %s %s selhala — jede se na starším snímku, další pokus za 30 min",
            self.symbol,
            today,
        )
        await self.publisher.publish(
            "alerts",
            {
                "kind": "oi_refresh_failed",
                "symbol": self.symbol,
                "message": f"Obnova OI pro {self.symbol} po publikačním okně selhala — "
                "jede se na starším snímku dne, další pokus za 30 min",
                "ts": dt.datetime.now(dt.UTC).timestamp(),
            },
        )
        return True

    async def _run_setup_selfcheck(self, today: dt.date) -> None:
        """Denní sebekontrola detektoru (#309): alert, když za okno prodělává.

        Běží po ranním OI archivu, jednou za den. Zdravý stav jde jen do logu —
        denní „vše v pořádku" alert by zvonek naučil ignorovat. Selhání nesmí
        zabít pipeline (stejně jako FA validace).
        """
        if self.setup_engine is None:
            return
        if self._selfcheck_day == today:
            return
        params = SetupParamsStats(
            window_days=self.settings.setup_selfcheck_days,
            min_samples=self.settings.setup_selfcheck_min_samples,
            max_drawdown_r=self.settings.setup_selfcheck_max_drawdown_r,
        )
        since = dt.datetime.combine(
            today - dt.timedelta(days=params.window_days), dt.time.min, tzinfo=dt.UTC
        )
        try:
            # Jen aktuální mechanika (#311): míchat výsledky různých systémů by
            # znamenalo alertovat na něco, co už neexistuje
            rows = await asyncio.to_thread(
                partial(
                    self.setup_engine.repository.closed_since,
                    self.symbol,
                    since,
                    mechanics_version=SETUP_MECHANICS_VERSION,
                )
            )
        except Exception:
            logger.exception("Sebekontrola setupů %s selhala — zkusí se zítra", self.symbol)
            return
        self._selfcheck_day = today
        report = aggregate(rows)
        summary = format_report(report, params.window_days)
        is_bad = degraded(report, params)
        logger.info("Sebekontrola setupů %s — %s", self.symbol, summary)
        if is_bad and not self._selfcheck_degraded:
            self._selfcheck_degraded = True
            logger.error("Setup detektor %s prodělává: %s", self.symbol, summary)
            await self.publisher.publish(
                "alerts",
                {
                    "kind": "setup_degraded",
                    "symbol": self.symbol,
                    "message": f"Setup detektor {self.symbol} prodělává — {summary}. "
                    "Zvaž vypnutí nejhorší šablony "
                    "(GEXLENS_SETUP_DISABLED_TEMPLATES) nebo úpravu prahů.",
                    "ts": dt.datetime.now(dt.UTC).timestamp(),
                },
            )
        elif not is_bad and self._selfcheck_degraded:
            self._selfcheck_degraded = False
            await self.publisher.publish(
                "alerts",
                {
                    "kind": "setup_recovered",
                    "symbol": self.symbol,
                    "message": f"Setup detektor {self.symbol} se vrátil nad práh — {summary}",
                    "ts": dt.datetime.now(dt.UTC).timestamp(),
                },
            )

    async def _run_fa_validation(self, today: dt.date) -> None:
        """Denní FA validace (#232): open-ratio bod za včerejší volume vs. dnešní ΔOI.

        Běží po úspěšném OI archivu; selhání nesmí zabít pipeline — bod se
        dopočítá při dalším pokusu (idempotentní dedup v tabulce fa_validation).
        """
        if self.fa_repository is None:
            return
        try:
            records = await asyncio.to_thread(
                collect_fa_validation,
                self.symbol,
                self.settings.snapshots_dir,
                self.oi_repository,
                self.fa_repository,
                today,
            )
        except Exception:
            logger.exception("FA validace %s selhala — zkusí se při dalším OI cyklu", self.symbol)
            return
        alpha = self._current_alpha()
        for record in records:
            point = record.point
            logger.info(
                "FA validace %s %s %s→%s: open-ratio %.3f, spearman %.3f, "
                "silent %.3f, volume %.0f, |ΔOI| %.0f",
                self.symbol,
                record.expiry,
                record.day,
                record.next_day,
                point.open_ratio,
                point.spearman,
                point.silent_share,
                point.volume_sum,
                point.doi_abs_sum,
            )
            await self.publisher.publish(
                "alerts",
                {
                    "kind": "fa_validation",
                    "symbol": self.symbol,
                    "message": (
                        f"FA validace {self.symbol} {record.expiry} ({record.day}): "
                        f"open-ratio {point.open_ratio:.2f}, korelace {point.spearman:.2f} "
                        f"(α={alpha:.2f}, ADR-0011)"
                    ),
                    "ts": dt.datetime.now(dt.UTC).timestamp(),
                },
            )

    def _current_alpha(self) -> float:
        """Aktuálně platná α symbolu: kalibrovaná z runtime, jinak konfigurace."""
        if self.runtime.flow_alpha is not None:
            return self.runtime.flow_alpha
        return self.settings.flow_oi_alpha

    async def _run_alpha_calibration(self, today: dt.date) -> None:
        """Ranní kalibrace α (#232 fáze 2): včerejší netflow vs. skutečné ΔOI.

        Běží po FA validaci nad týmiž archivy; selhání nesmí zabít pipeline —
        bod se dopočítá při dalším OI cyklu (idempotentní dedup v historii).
        I bez nového bodu se uložená α propíše do runtime (start enginu).
        """
        if self.alpha_repository is None:
            return
        try:
            result = await asyncio.to_thread(
                collect_alpha_calibration,
                self.symbol,
                self.settings.derived_dir,
                self.oi_repository,
                self.alpha_repository,
                today,
            )
            if result is None:
                state = await asyncio.to_thread(self.alpha_repository.get, self.symbol)
                if state is not None and self.runtime.flow_alpha != state.alpha:
                    self.runtime.flow_alpha = state.alpha
                    logger.info(
                        "α %s obnovena z kalibrace: %.3f (%d dnů)",
                        self.symbol,
                        state.alpha,
                        state.days,
                    )
                return
        except Exception:
            logger.exception("Kalibrace α %s selhala — zkusí se při dalším OI cyklu", self.symbol)
            return
        self.runtime.flow_alpha = result.alpha_after
        point = result.point
        logger.info(
            "Kalibrace α %s %s (%s): medián %.3f (buy %s / sell %s, %d stran) → α %.3f (%d dnů)",
            self.symbol,
            result.expiry,
            result.day,
            point.ratio_median,
            "—" if point.ratio_buy is None else f"{point.ratio_buy:.3f}",
            "—" if point.ratio_sell is None else f"{point.ratio_sell:.3f}",
            point.samples,
            result.alpha_after,
            result.days,
        )
        await self.publisher.publish(
            "alerts",
            {
                "kind": "fa_calibration",
                "symbol": self.symbol,
                "message": (
                    f"Kalibrace FA α {self.symbol}: denní medián ΔOI/net "
                    f"{point.ratio_median:.2f} ({point.samples} stran) → "
                    f"α = {result.alpha_after:.2f} po {result.days} dnech"
                ),
                "ts": dt.datetime.now(dt.UTC).timestamp(),
            },
        )

    async def _expand_secondary(self, spot: float, today: dt.date) -> None:
        """Roztažení obálky sekundární expirace (#442).

        Sekundár dostával pásmo jen jednou při startu pipeline, takže při
        trendovém dni cena utekla nad horní strike a jeho heatmapa i zdi se
        nad tou hranicí přestaly kreslit. Rozšiřuje se stejnou logikou jako
        aktivní řetěz; `capped` se u něj nealertuje — o stropu obálky
        informuje už alert aktivního řetězu a druhý by jen zdvojoval.
        Nové striky se doarchivují stejně jako u aktivního řetězu (#494) —
        bez toho by měly `get_oi` None celý den.
        """
        if self.next_runtime is None or self.next_info is None or self.next_band is None:
            return
        expansion = self.discovery.maybe_expand(self.next_info, self.next_band, spot)
        if not expansion.expanded:
            return
        self.next_band = expansion.band
        previous_contracts = self.next_runtime.contracts
        self.next_runtime.contracts = build_contracts(
            _underlying_for(self.symbol, self.next_info), self.next_info, self.next_band
        )
        await self._archive_new_strikes(previous_contracts, self.next_runtime.contracts, today)
        logger.info(
            "Obálka sekundární expirace %s %s rozšířena na %g–%g (%d kontraktů)",
            self.symbol,
            self.next_info.expiry,
            self.next_band.low,
            self.next_band.high,
            len(self.next_runtime.contracts),
        )

    def _current_spot(self) -> float:
        last = self.ticker.last
        if last == last:  # není NaN
            self.spot = last
            return last
        market = self.ticker.marketPrice()
        if market == market:
            self.spot = market
        return self.spot

    async def run_minute(self, now: dt.datetime) -> SweepMetrics:
        """Jeden minutový cyklus instrumentu: OI retry, expanze obálky, runtime cyklus."""
        # Nový den (#494): finalita i dostupnost OI patřily včerejšku. Pipeline
        # s nedenní nejbližší expirací přežije půlnoc a bez resetu by se nový
        # den nikdy nearchivoval (oi_final=True vypíná retry blok níže).
        today = now.date()
        if self._oi_day is not None and self._oi_day != today:
            self.oi_available = False
            self.oi_final = False
            self._cycles_since_oi = OI_RETRY_CYCLES  # archivuj hned, ne až za 30 min
        self._oi_day = today
        # Retry běží nejen když OI chybí, ale i dokud snímek není finální (#463):
        # předpublikační čísla jsou nenulová, takže se bez toho nikdy neobnoví
        if not self.oi_available or self._oi_refresh_due(now):
            self._cycles_since_oi += 1
            if self._cycles_since_oi >= OI_RETRY_CYCLES:
                self._cycles_since_oi = 0
                self.oi_available = await self.try_archive_oi(today, now)

        spot = self._current_spot()

        # Auto-rozšíření denní obálky (ADR-0002) — aktivní i sekundární řetěz (#442)
        await self._expand_secondary(spot, today)
        expansion = self.discovery.maybe_expand(self.info, self.band, spot)
        if expansion.expanded:
            self.band = expansion.band
            previous_contracts = self.runtime.contracts
            self.runtime.contracts = build_contracts(
                _underlying_for(self.symbol, self.info), self.info, self.band
            )
            # Striky přibylé posunem pásma nemá denní archiv pokryté (#465):
            # 4. 8. tak NQ vyjelo 11 striků nad archivované pásmo a všechny
            # měly v grafu nulové OI. Doarchivují se hned, ne až zítra.
            await self._archive_new_strikes(previous_contracts, self.runtime.contracts, today)
            if expansion.capped:
                await self.publisher.publish(
                    "alerts",
                    {
                        "kind": "band_capped",
                        "symbol": self.symbol,
                        "message": f"Obálka strikes {self.symbol} na stropu — "
                        "vzdálený okraj se posouvá",
                        "ts": now.timestamp(),
                    },
                )

        bars = list(self.minute_bars)
        self.minute_bars.clear()
        forming = self.forming_bar()
        # Hlídání barů PŘED cyklem — alert musí odejít, i kdyby sweep selhal (#221)
        await self._collect_news_ticks(now)
        await self._watch_bars(now, spot, bars, forming)
        metrics = await self.runtime.run_cycle(now, spot, bars, forming)
        await self._watch_greeks(now, metrics)
        await self._watch_repair(now, metrics)

        # Setup detektor (ADR-0004) — jeho pád nesmí shodit sběr dat
        if self.setup_engine is not None:
            try:
                await self.setup_engine.on_minute(now, spot, bars, self.runtime)
            except Exception:
                logger.exception("Setup detektor %s selhal — pokračuji", self.symbol)
        # Indikátor tendence (#350) — stejný kontrakt: pád nesmí shodit sběr
        if self.tendency_engine is not None:
            try:
                await self.tendency_engine.on_minute(now, spot, self.runtime)
            except Exception:
                logger.exception("Indikátor tendence %s selhal — pokračuji", self.symbol)
        # Sběrač kandidátů T6 (#256) — sám se hlídá na jeden běh denně
        if self.t6_collector is not None:
            try:
                await self.t6_collector.on_minute(now, spot, self.runtime)
            except Exception:
                logger.exception("T6 sběrač %s selhal — pokračuji", self.symbol)

        # Následující expirace v nižší kadenci; její pád nesmí shodit aktivní řetěz
        if (
            self.next_runtime is not None
            and self._minute_count % self.settings.next_expiry_sweep_every == 0
        ):
            try:
                await self.next_runtime.run_cycle(now, spot, [])
                await self._check_vol_concentration(now)
            except Exception:
                logger.exception(
                    "Sekundární cyklus %s %s selhal — pokračuji",
                    self.symbol,
                    self.next_runtime.expiry,
                )
        self._minute_count += 1
        return metrics

    async def _collect_news_ticks(self, now: dt.datetime) -> None:
        """Broker headlines z ticku 292 (#291) — zápis do sdílené news_events.

        Selhání nesmí zabít cyklus: zprávy jsou nadstavba, sběr opčních dat má
        přednost.
        """
        if self.news_ticks is None or self.read_news_ticks is None:
            return
        try:
            ticks = self.read_news_ticks()
            if ticks:
                await asyncio.to_thread(self.news_ticks.write, ticks, now=now)
        except Exception:
            logger.exception("Zápis IBKR headlines selhal — pokračuji")

    async def _watch_greeks(self, now: dt.datetime, metrics: SweepMetrics) -> None:
        """Tichá ztráta Greeks (#306): alert, když sweep dlouho nedokáže obnovit kotace.

        27. 7. přestala TWS počítat modelGreeks pro ATM striky; ceny a OI chodily
        dál, takže se výpadek nijak neprojevil a cache 15 hodin servírovala zmrzlá
        čísla. Metriky to celou dobu hlásily (`repair_count` 61 z 222), jen se na
        ně nikdo nedíval — proto alert.
        """
        detector = self.greeks_detector
        if detector is None:
            return
        event = detector.observe(total=metrics.total, stale=metrics.stale_count)
        if event is None:
            return
        share = metrics.stale_count / metrics.total if metrics.total else 0.0
        if event == "stalled":
            logger.error(
                "Greeks %s nechodí pro %d z %d kontraktů (%.0f %%) ≥ %d sweepů — "
                "TWS je přestala počítat; zvaž restart TWS",
                self.symbol,
                metrics.stale_count,
                metrics.total,
                share * 100,
                self.settings.greeks_stall_cycles,
            )
            await self.publisher.publish(
                "alerts",
                {
                    "kind": "greeks_stalled",
                    "symbol": self.symbol,
                    "message": f"TWS nedodává Greeks pro {metrics.stale_count} z "
                    f"{metrics.total} kontraktů {self.symbol} ({share:.0%}) — dotčené "
                    "striky se přestaly počítat do GEX, zdí i Max Painu, aby výpočty "
                    "nestály na zmrzlých datech. Pomáhá restart TWS.",
                    "ts": now.timestamp(),
                },
            )
        elif event == "recovered":
            logger.info(
                "Greeks %s zase chodí (stale %d/%d)",
                self.symbol,
                metrics.stale_count,
                metrics.total,
            )
            await self.publisher.publish(
                "alerts",
                {
                    "kind": "greeks_recovered",
                    "symbol": self.symbol,
                    "message": f"Greeks {self.symbol} zase chodí — striky se vrátily "
                    "do GEX a úrovní",
                    "ts": now.timestamp(),
                },
            )

    async def _watch_repair(self, now: dt.datetime, metrics: SweepMetrics) -> None:
        """Trvale selhávající repair (#547): alert `strikes_stalled` + recovery.

        7. 8. TWS celou seanci nedodávala modelGreeks pro ATM pásmo NQ QN1 —
        repair běžel hodiny à 4 s bez jediného úspěchu a bez jediné hlášky.
        Kontrakty s ≥ `repair_stall_rounds` neúspěšnými koly nese scheduler
        v metrikách; tady se z hrany dělá alert (vzor bars_stalled).
        """
        detector = self.repair_detector
        if detector is None:
            return
        event = detector.observe(metrics.stalled_count)
        if event is None:
            return
        if event == "stalled":
            logger.error(
                "Repair %s: %d kontraktů se nedaří obnovit ≥ %d kol — TWS pro ně "
                "trvale nedodává kompletní data; retry běží s backoffem, "
                "zvaž restart TWS",
                self.symbol,
                metrics.stalled_count,
                self.settings.repair_stall_rounds,
            )
            await self.publisher.publish(
                "alerts",
                {
                    "kind": "strikes_stalled",
                    "symbol": self.symbol,
                    "message": f"TWS dlouhodobě nedodává kompletní data pro "
                    f"{metrics.stalled_count} striků {self.symbol} — repair běží "
                    "s backoffem; striky s živými kotacemi jedou na dopočtených "
                    "Greeks (BS z mid ceny). Pomáhá restart TWS.",
                    "ts": now.timestamp(),
                },
            )
        elif event == "recovered":
            logger.info("Repair %s: zaseknuté striky se zotavily", self.symbol)
            await self.publisher.publish(
                "alerts",
                {
                    "kind": "strikes_recovered",
                    "symbol": self.symbol,
                    "message": f"Striky {self.symbol} se zotavily — TWS zase dodává "
                    "kompletní data, dopočtené Greeks skončily",
                    "ts": now.timestamp(),
                },
            )

    async def _watch_bars(
        self, now: dt.datetime, spot: float, bars: Sequence[Bar], forming: Bar | None
    ) -> None:
        """Tichá ztráta 5s barů (#221): alert při výpadku, po návratu re-backfill díry.

        Bar aktivita = uzavřené minuty NEBO rozdělaná agregace aktuální minuty;
        zaseknutý agregátor drží starou rozdělanou minutu, ta se nepočítá.
        """
        detector = self.stall_detector
        if detector is None:
            return
        bar_activity = bool(bars) or (forming is not None and forming.ts == now)
        spot_moving = (
            spot == spot and self._last_spot == self._last_spot and spot != self._last_spot
        )
        self._last_spot = spot
        event = detector.observe(bar_activity=bar_activity, spot_moving=spot_moving)
        if event == "stalled":
            logger.error(
                "Real-time bary %s nechodí ≥ %d min při živém spotu — mrtvý "
                "reqRealTimeBars stream (výpadek TWS farem?); svíčky se nekreslí, "
                "zvaž restart TWS",
                self.symbol,
                self.settings.bars_stall_alert_minutes,
            )
            await self.publisher.publish(
                "alerts",
                {
                    "kind": "bars_stalled",
                    "symbol": self.symbol,
                    "message": f"Svíčky {self.symbol} se přestaly kreslit — real-time "
                    f"bary z TWS nechodí ≥ {self.settings.bars_stall_alert_minutes} min, "
                    "spot přitom žije (mrtvé TWS farmy?). Pomáhá restart TWS; díra se "
                    "po návratu doplní backfillem.",
                    "ts": now.timestamp(),
                },
            )
        elif event == "recovered":
            logger.info("Real-time bary %s zase chodí — díra se doplní backfillem", self.symbol)
            await self.publisher.publish(
                "alerts",
                {
                    "kind": "bars_recovered",
                    "symbol": self.symbol,
                    "message": f"Real-time bary {self.symbol} zase chodí — díra ve "
                    "svíčkách se doplňuje backfillem",
                    "ts": now.timestamp(),
                },
            )
            if self.backfill_today is not None:
                # Na pozadí: backfill čeká na PacingGuard a nesmí blokovat cyklus
                # (watchdog CYCLE_TIMEOUT_S by ho jinak zabil i se sweepem)
                task: asyncio.Task[None] = asyncio.ensure_future(self.backfill_today())
                task.add_done_callback(self._log_backfill_result)
                self._backfill_task = task

    async def _check_vol_concentration(self, now: dt.datetime) -> None:
        """Alert na neobvyklou koncentraci volume na příští expiraci (#208).

        Alanův event-workflow: jeden dominantní strike zítřejšího řetězu =
        úroveň, kde se trh zajišťuje na event. Jeden alert per leader
        (nová dominantní strana se ohlásí znovu).
        """
        runtime = self.next_runtime
        if runtime is None:
            return
        volumes = {
            (spec.strike, spec.right): float(cached.snapshot.volume or 0.0)
            for spec, cached in runtime.scheduler.quotes().items()
        }
        found = detect_concentration(
            volumes,
            ratio=self.settings.vol_leader_ratio,
            min_volume=self.settings.vol_leader_min_volume,
        )
        if found is None:
            return
        key = (runtime.expiry, found.strike, found.right)
        if key in self._vol_alerted:
            return
        self._vol_alerted.add(key)
        label = f"{found.strike:g}{found.right}"
        # Interpretační dovětek jen když poloha vůči spotu odpovídá čtení z issue
        if found.right == "P" and found.strike < self.spot:
            hint = " Dominantní put pod trhem — pojistka/magnet pro negativní scénář."
        elif found.right == "C" and found.strike > self.spot:
            hint = " Dominantní call nad trhem — strop pro pozitivní scénář."
        else:
            hint = ""
        await self.publisher.publish(
            "alerts",
            {
                "kind": "vol_concentration",
                "symbol": self.symbol,
                "message": f"Neobvyklá koncentrace na expiraci {runtime.expiry}: "
                f"{label} — {found.volume:,.0f} kontraktů "
                f"({found.ratio:.1f}× medián top 10).{hint}",
                "ts": now.timestamp(),
            },
        )
        logger.info(
            "Vol koncentrace %s %s: %s (%.1fx medián)",
            self.symbol,
            runtime.expiry,
            label,
            found.ratio,
        )

    def _log_backfill_result(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Re-backfill %s po výpadku barů selhal: %s", self.symbol, exc)

    def stop(self) -> None:
        """Odhlášení market dat podkladu (kontrakty řetězce rotuje scheduler sám)."""
        if self._backfill_task is not None:
            self._backfill_task.cancel()
        try:
            self.on_stop()
        except Exception:
            logger.exception("Stop pipeline %s selhal — pokračuji", self.symbol)


def _underlying_for(symbol: str, info: ExpiryInfo) -> Underlying:
    """Minimální podklad pro build_contracts — po discovery stačí symbol a burza."""
    return Underlying(symbol=symbol, sec_type="FUT", exchange=info.exchange, con_id=0)


async def gather_metrics(
    pipelines: Sequence[InstrumentPipeline],
    now: dt.datetime,
    *,
    timeout_s: float = CYCLE_TIMEOUT_S,
) -> list[tuple[str, SweepMetrics | None]]:
    """Sekvenční cykly všech pipeline (špička lines = jedna dávka; SPEC kap. 8 odolnost).

    Každý cyklus běží pod watchdog timeoutem (#219) — zaseknutý await na mrtvém
    IBKR spojení jinak zastaví celý orchestrátor navždy, zatímco spot stream
    běží dál a engine vypadá zdravě."""
    results: list[tuple[str, SweepMetrics | None]] = []
    for pipeline in pipelines:
        try:
            metrics = await asyncio.wait_for(pipeline.run_minute(now), timeout=timeout_s)
            results.append((pipeline.symbol, metrics))
        except TimeoutError:
            logger.error(
                "Cyklus %s nedoběhl do %g s (visící IBKR await?) — zrušen, pokračuji",
                pipeline.symbol,
                timeout_s,
            )
            results.append((pipeline.symbol, None))
        except Exception:
            logger.exception("Cyklus %s selhal — pokračuji dalším instrumentem", pipeline.symbol)
            results.append((pipeline.symbol, None))
    return results


def aggregate_status(
    results: Sequence[tuple[str, SweepMetrics | None]],
) -> dict[str, object]:
    """Agregovaný status pipeline přes instrumenty (stavová lišta ukazuje součty)."""
    valid = [metrics for _, metrics in results if metrics is not None]
    return {
        "greeks_complete": sum(m.greeks_complete for m in valid),
        "greeks_total": sum(m.total for m in valid),
        "repair_count": sum(m.stale_count for m in valid),
        "lines_utilization": max((m.lines_utilization for m in valid), default=0.0),
        "symbols": ",".join(symbol for symbol, _ in results),
    }


async def read_watchlist(reader: WatchlistReader) -> list[str]:
    """Watchlist z DB mimo event loop (sync SQLAlchemy)."""
    return await asyncio.to_thread(reader.symbols)
