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

from gexlens_engine.briefing_verdicts import BriefingVerdictCollector
from gexlens_engine.compute.cumdelta import CumDeltaTracker
from gexlens_engine.compute.facalibration import FA_MIN_DAYS
from gexlens_engine.compute.gexforward import ForwardContract, forward_field
from gexlens_engine.compute.marketclock import is_market_closed
from gexlens_engine.compute.settle import (
    expiry_settle,
    is_trading_session,
)
from gexlens_engine.compute.setups import SETUP_MECHANICS_VERSION
from gexlens_engine.compute.setupstats import (
    SetupParamsStats,
    aggregate,
    degraded,
    format_report,
)
from gexlens_engine.compute.spot import SPOT_AGE_GATE, SPOT_WATCH_GRACE, SpotReading
from gexlens_engine.compute.volleaders import detect_concentration
from gexlens_engine.config import Settings
from gexlens_engine.emrespect import EmRespectCollector
from gexlens_engine.gammacliff import GammaCliffCollector
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
from gexlens_engine.ivrank import IvRankCollector
from gexlens_engine.mapstate import MapStateCollector
from gexlens_engine.paper import PaperBroker
from gexlens_engine.probes import T9ProbeCollector
from gexlens_engine.runtime import EngineRuntime, PublisherLike
from gexlens_engine.scenario_auto import ScenarioGenerator
from gexlens_engine.scenarios import ScenarioCollector
from gexlens_engine.setups import SetupEngine
from gexlens_engine.storage.fa_calibration import (
    FaAlphaRepository,
    FaAlphaState,
    calibration_source,
    collect_alpha_calibration,
    live_alpha,
)
from gexlens_engine.storage.fa_validation import FaValidationRepository, collect_fa_validation
from gexlens_engine.storage.meta import ensure_meta_schema, settings_table, watchlist_table
from gexlens_engine.storage.oi_archive import (
    ArchiveResult,
    OIArchiver,
    OIEodRepository,
    OIRecord,
)
from gexlens_engine.t6 import T6Collector
from gexlens_engine.tendency import TendencyEngine
from gexlens_engine.verdict_writer import DayVerdictWriter
from gexlens_engine.volregime import VolRegimeCollector

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
# OI archiv z minutového cyklu po dávkách ve zbytku minuty (#1646): rezerva
# před startem dalšího cyklu — dávka může přetáhnout `batch_timeout_s` o
# kvalifikaci kontraktů a odhlášení subskripcí (22. 10. 2026: Ø 3,6 s na dávku)
OI_ARCHIVE_RESERVE_S = 5.0


@dataclass
class OIArchivePass:
    """Rozpracovaný průchod OI archivu dne, čtený po dávkách mezi cykly (#1646).

    `started` je minuta otevření: k ní se vztahuje čas pořízení, finalita i
    upozornění, stejně jako u čtení celého průchodu naráz.
    """

    day: dt.date
    started: dt.datetime
    #: Platný denní archiv už existuje → selhání je jen neúspěšná OBNOVA (#494)
    has_snapshot: bool
    contracts: tuple[OptionContractSpec, ...]
    offset: int = 0
    records: list[OIRecord] = field(default_factory=list)
    missing: list[OptionContractSpec] = field(default_factory=list)
    #: Chybějící kontrakty průchodu už jednou dočteny (#1674)
    reread: bool = False


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


def greeks_watch_applies(
    expiry: str,
    now: dt.datetime,
    trading_class: str | None = None,
    symbol: str | None = None,
) -> bool:
    """Má se u téhle řady vůbec hlídat výpadek Greeks? (#959)

    Po settle se expirující řetěz přestane kotovat, takže Greeks logicky
    přestanou chodit. Hlídka to hlásila jako poruchu — každý den po settle,
    s radou „zvaž restart TWS", která nic neřeší. Pravidelný falešný poplach
    je horší než žádný: uživatel si na něj zvykne a mine se pak i ten pravý.

    Od #1331 pipeline roluje přímo v settle (`expiry_expired`) dřív, než
    minutový cyklus doběhne, takže aktivní řetěz po settle nehlídá nikdo.
    Brána zůstává jako pojistka invariantu — sdílí s rollem jednu hranici.

    Nečitelná expirace → True (hlídat; nerozbíjet běh kvůli formátu).
    """
    settle = expiry_settle(expiry, trading_class, symbol)
    if settle is None:
        logger.warning("Nečitelná expirace %r — hlídka Greeks se nechává zapnutá", expiry)
        return True
    return now < settle


def expiry_expired(
    expiry: str,
    now: dt.datetime,
    trading_class: str | None = None,
    symbol: str | None = None,
) -> bool:
    """True, když expirace (YYYYMMDD) už proběhla — pipeline se musí překlopit.

    Hranice je settle EXPIRACE (`settle.expiry_settle`, ADR-0039 bod 2):
    16:00 ET, standardní kvartální třída SOQ 9:30 ET (#1189) — týdenní série
    na kvartální datum (EW3/QN3) až odpoledne, proto trading class (#1366).
    Do #1331 se běžná expirace
    rolovala až s novým kalendářním dnem v UTC, takže pipeline běžela 3–4 h
    nad vypršelým řetězem (setupy, sondy T9, tendence i heatmapa). Orchestrátor
    vypršelou pipeline zastaví a týž cyklus ji založí znovu; discovery i cache
    vypršelé expirace vynechají, takže se nevrátí na tutéž. Nečitelný formát
    → False (nerozbíjet běh).
    """
    settle = expiry_settle(expiry, trading_class, symbol)
    if settle is None:
        logger.warning("Nečitelná expirace %r — roll se přeskakuje", expiry)
        return False
    return now >= settle


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
        ensure_meta_schema(self._db)

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
    # Odkud teče cena podkladu (#614 fáze 2a): "ibkr" | "tasty" | "none".
    # Status ho agreguje do UI — tiché přepnutí zdroje zakazuje ADR-0025
    # pravidlo 5, takže uživatel musí poznat, odkud se dívá.
    spot_source: Callable[[], str] = lambda: "ibkr"
    # Cena z aktivního fallbacku (#614): během výpadku IBKR je ticker zamrzlý
    # a cyklus by počítal nad cenou z doby výpadku; None = fallback neběží
    spot_override: Callable[[], float | None] = lambda: None
    # Spot se stářím (#1600, #1623; ADR-0057 doplněk 23. 10.): pod v6 hlasuje verdikt
    # dne a tendence jen z živé ceny (IBKR → 5s bar → tasty, `compute/spot`), stará
    # cena je chybějící vstup. None = zdroj nenapojený (testy) — chování v5
    spot_reading: Callable[[], SpotReading] | None = None
    spot_age_gate: bool = SPOT_AGE_GATE
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
    # Gamma útes po expiraci (#576, fáze 1 jen měření) — None = vypnuto
    gamma_cliff: GammaCliffCollector | None = None
    # Stav „tenká mapa" (#1245, fáze 1 jen měření) — None = vypnuto
    map_state: MapStateCollector | None = None
    # Scénář dne (#1173) — vyhodnocení po termínu, jednou po settle
    scenario_collector: ScenarioCollector | None = None
    # Paper účet (#1187 fáze 1): fily orderů proti barům minuty — None = vypnuto
    paper_broker: PaperBroker | None = None
    # Verdikt dne (#1595) — engine jediný zapisovatel, okno před US openem
    verdict_writer: DayVerdictWriter | None = None
    # Automatický scénář z verdiktu dne (#1173 A) — jednou před US openem
    scenario_generator: ScenarioGenerator | None = None
    # Volatilitní režim z barů (ADR-0028, #713) — None = vypnuto
    vol_regime: VolRegimeCollector | None = None
    # Respektování pásma EM (#872, D3) — None = vypnuto
    em_respect: EmRespectCollector | None = None
    #: Vyhodnocení verdiktů dne po settle (#1091)
    briefing_verdicts: BriefingVerdictCollector | None = None
    # IV Rank — tři denní řady IV (#871) — None = vypnuto
    iv_rank: IvRankCollector | None = None
    # Sběr výskytů kandidáta T9 (#577, fáze 1 jen měření) — None = vypnuto
    t9_probes: T9ProbeCollector | None = None
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
    # Obnova mrtvého reqRealTimeBars streamu po stall (#1082); None = jen alert
    restart_bars: Callable[[], Awaitable[None]] | None = None
    # Živý fallback barů z dxFeed Candle během stall (#614 doplněk): doplní
    # minuty [since, until) chybějící v particích; None = bez tasty větve
    bar_gap_fill: Callable[[dt.datetime, dt.datetime], Awaitable[int]] | None = None
    _cycles_since_oi: int = field(default=0, repr=False)
    # Den, ke kterému patří `oi_available`/`oi_final` (#494): pipeline symbolu
    # s nedenní nejbližší expirací přežije půlnoc a bez resetu by včerejší
    # finalita blokovala archivaci nového dne navždy.
    _oi_day: dt.date | None = field(default=None, repr=False)
    # Den, za který už odešel OI alert (#1307): selhání se hlásí hranově —
    # jednou za den a znovu až po úspěšném čtení. Opakovat tutéž větu à 30 min
    # je spam, který uživatele naučí zvonek ignorovat.
    _oi_alerted_day: dt.date | None = field(default=None, repr=False)
    # Stav rozvrhu CME z minulého cyklu (#1307) — z něj hrana otevření trhu.
    # None před prvním cyklem: start v zavřeném trhu (víkend, pauza Globexu =
    # okno nasazení) není hrana zavření a první archiv nezahodí (#1674)
    _market_closed: bool | None = field(default=None, repr=False)
    # Poslední stav živosti spotu pod v6 — log jen na přechodu, ne každou minutu;
    # hlídá se až `SPOT_WATCH_GRACE` po startu a po každém otevření trhu
    _spot_live: bool = field(default=True, repr=False)
    _spot_watch_from: dt.datetime | None = field(default=None, repr=False)
    _minute_count: int = field(default=0, repr=False)
    _last_spot: float = field(default=float("nan"), repr=False)
    _backfill_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _gap_fill_task: asyncio.Task[int] | None = field(default=None, repr=False)
    #: OI archiv čtený po dávkách mezi cykly (#1646, první archiv dne #1674); None = neběží
    _oi_pass: OIArchivePass | None = field(default=None, repr=False)
    #: Joby po průchodu (FA, sebekontrola, Forward GEX) na pozadí (#1646)
    _oi_jobs_task: asyncio.Task[None] | None = field(default=None, repr=False)
    # Kolik barů zatím doplnil tasty fallback během běžícího stall (do logu při zotavení)
    _gap_filled: int = field(default=0, repr=False)
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

    def _oi_published(self, now: dt.datetime) -> bool:
        """Publikoval už CME OI pro UTC den `now`? (#1307)

        CME publikuje jen v obchodní dny (po–pá) a do publikačního okna
        (`oi_publication_utc`, 07:00 CT = horní mez naměřeného příchodu, #463).
        Za sobotu a neděli nepublikuje nic: sobotní snímek je beze změny kopie
        pátku (26. 9. 2026: obnova à 30 min od 07:00 CT, 0 zapsáno) a nová
        čísla dodá IBKR nejdřív v neděli po otevření Globexu (oi_eod 13. a
        20. 9. 2026: zápisy až po 17:00 CT). Pro víkendový UTC den tak „po
        okně" nenastane nikdy. Totéž o svátku (#1308): CME nepublikuje, obnova neběží.
        """
        day = now.date()
        return is_trading_session(day) and now >= self.settings.oi_publication_utc(day)

    def _oi_refresh_due(self, now: dt.datetime) -> bool:
        """Má se existující snímek dne přečíst znovu? (#463)

        Před publikačním oknem nemá smysl číst — IBKR finální čísla ještě nemá.
        Po okně se čte, dokud dvě po sobě jdoucí čtení nedají totéž (`oi_final`);
        potvrzení se do DB neukládá, takže po restartu proběhne jedno kontrolní
        čtení navíc. To je levnější než další stav v schématu.

        Víkendový UTC den se neobnovuje vůbec (#1307, `_oi_published`) — první
        obnova po víkendu je v pondělí po okně. Při zavřeném trhu (denní pauza,
        páteční večer) se neobnovuje taky; po denní pauze jde obnova hned
        v 17:00 CT, protože `_on_market_open` natáhne čítač retry.
        """
        return self._oi_published(now) and not self.oi_final and not is_market_closed(now)

    async def start_oi_archive(self, today: dt.date, now: dt.datetime | None = None) -> None:
        """Otevře průchod OI archivu dne; dávky čte `advance_oi_archive` (#1646, #1674).

        Jediná cesta pro první archiv po založení pipeline (start enginu, roll
        v settle) i pro retry, čtení nového UTC dne a obnovu z minutového cyklu.
        První archiv dřív četl celý průchod naráz jako task na pozadí (#1208),
        souběžně se sweepy a s prvním archivem druhého symbolu (#1649): 23. 10.
        2026 po rollu ve 20:01 UTC stála smyčka asyncio až 8,08 s a hlídač
        heartbeatu shodil zdravé spojení (#1650). Po dávkách mezi cykly přijde
        první OI po startu za 2–4 min, ale bez souběhu; do té doby jede pipeline
        s `oi_available=False` (volume fallback, jako při selhání archivu).
        Platný snímek dne bez obnovy = OI je k dispozici hned.

        Po #463 archivace nekončí prvním úspěchem: snímek z doby před publikací
        IBKR nese neúplná čísla (4. 8. 2026 tak celý den běžel GEX na půlnočním
        stavu, kde put strana měla Σ OI 1 877 proti 29 282 na call straně).
        `now` je injektovatelné kvůli testům — rozhodnutí o obnově závisí na
        hodině, takže by test jinak platil jen část dne.

        Selhání otevření (PG, disk) nesmí shodit založení pipeline ani cyklus
        (#215): pipeline jede dál s volume fallbackem a retry přijde za
        `OI_RETRY_CYCLES` cyklů — dřív to za první archiv hlídal task na pozadí.
        """
        now = now or dt.datetime.now(dt.UTC)
        try:
            self._oi_pass = await self._open_oi_archive(today, now)
        except Exception:
            logger.exception(
                "OI archiv %s %s se neotevřel — retry v minutovém cyklu", self.symbol, today
            )
            return
        if self._oi_pass is None:
            self.oi_available = True
            return
        logger.info(
            "OI archiv %s %s: průchod %d kontraktů po dávkách mezi cykly",
            self.symbol,
            today,
            len(self._oi_pass.contracts),
        )

    async def _open_oi_archive(self, today: dt.date, now: dt.datetime) -> OIArchivePass | None:
        """Začátek průchodu OI archivu dne; None = platný snímek, číst není co."""
        self._oi_day = today
        # Platný denní archiv už existuje → případné selhání čtení je jen
        # neúspěšná OBNOVA, ne ztráta OI (#494) — jede se dál na starším snímku
        has_snapshot = today in self.oi_repository.days(self.symbol)
        if has_snapshot and not self._oi_refresh_due(now):
            await self._run_fa_jobs(today)
            return None
        captured = self.oi_repository.captured_at(self.symbol, today)
        if captured is not None and captured < self.settings.oi_publication_utc(captured.date()):
            logger.info(
                "OI archiv %s %s je z %s UTC, tedy před publikací — obnovuji",
                self.symbol,
                today,
                captured.strftime("%H:%M"),
            )
        return OIArchivePass(
            day=today,
            started=now,
            has_snapshot=has_snapshot,
            contracts=tuple(self._archive_universe()),
        )

    async def advance_oi_archive(self, deadline: float, clock: Callable[[], float]) -> None:
        """Dávky rozpracovaného OI archivu do `deadline` (čas smyčky, #1646).

        Celý průchod ES (~1 400 kontraktů, 18 dávek) běžel v minutovém cyklu
        ~100 s a minuta ES i NQ chyběla à 31 min od 12:30 UTC. Teď každý krok
        průchodu — dávka i závěrečný zápis — začne, jen když se do zbytku
        minuty vejde `batch_timeout_s`, a průchod pokračuje v dalších minutách.
        Zápis a finalita až po celém průchodu; joby po archivu jdou na pozadí.

        Před zápisem jedna dávka znovu přečte kontrakty, které průchod nedodal
        (#1674). 23. 10. 2026 nesl první průchod ES po publikaci 64 kontraktů
        „tick bez OI“ (pozdní tick 101) a průchody NQ 2–9 náhodných výpadků
        z IBKR; každý chybějící dřív archivovaný kontrakt drží snímek nefinální
        a FA i kalibrace α NQ doběhly o 1,5 h později. Víc chybějících než
        `batch_size` je systémový výpadek (farma, celé expirace), ten jedno
        dočtení nespraví — průchod se zapíše bez něj a obnova jede dál.
        """
        archive = self._oi_pass
        if archive is None:
            return
        size = self.settings.batch_size
        read = 0
        while True:
            if clock() + self.settings.batch_timeout_s > deadline:
                if read:
                    logger.info(
                        "OI archiv %s %s: přečteno %d/%d kontraktů, pokračuje v další minutě",
                        self.symbol,
                        archive.day,
                        archive.offset,
                        len(archive.contracts),
                    )
                return
            if archive.offset < len(archive.contracts):
                batch = archive.contracts[archive.offset : archive.offset + size]
                records, missing = await self.archiver.fetch_batch(
                    batch, archive.day, offset=archive.offset, total=len(archive.contracts)
                )
                archive.records.extend(records)
                archive.missing.extend(missing)
                archive.offset += len(batch)
                read += len(batch)
            elif not archive.reread and 0 < len(archive.missing) <= size:
                archive.reread = True
                logger.info(
                    "OI archiv %s %s: dočtení %d chybějících kontraktů průchodu (#1674)",
                    self.symbol,
                    archive.day,
                    len(archive.missing),
                )
                records, archive.missing = await self.archiver.fetch_batch(
                    archive.missing, archive.day, offset=0, total=len(archive.missing)
                )
                archive.records.extend(records)
            else:
                break
        self._oi_pass = None
        try:
            result = await self.archiver.commit(
                archive.records, archive.missing, archive.day, now=archive.started
            )
        except Exception:
            self.oi_available = await self._oi_archive_failed(archive)
            return
        self.oi_available = await self._oi_archive_done(archive, result)
        if result.written:
            self._start_oi_jobs(archive.day)

    def _start_oi_jobs(self, today: dt.date) -> None:
        """Joby po průchodu z minutového cyklu na pozadí (#1646).

        IBKR nečtou, takže se lines se sweepem nesčítají; FA validace s kalibrací
        α ale trvala v den finality u ES 22 s (20. 10. 2026) a ke dvěma sweepům
        by se do minuty nevešla vždy. Platí i pro první archiv dne (#1674).
        """
        task = asyncio.create_task(self._after_oi_archive(today), name=f"oi-jobs-{self.symbol}")
        task.add_done_callback(self._log_oi_jobs_result)
        self._oi_jobs_task = task

    async def refresh_forward_after_tasty(self, written: int, now: dt.datetime) -> bool:
        """Forward GEX znovu po obnově tasty křídel po finalitě (#1607); True = přepočteno.

        Forward pole (#519) stojí na celém řetězu dne včetně tasty křídel mimo
        obálku IBKR (~55 % Σ OI 0DTE, #1605). Do finality ho přepočítá každý IBKR
        průchod. Po finalitě už žádný nepřijde, takže obnovu křídel (`wide_oi_pass`
        po výpadku tasty přes publikaci) by pole nevidělo až do rollu. Běžící joby
        po archivu zapisují týž soubor a čtou řetěz dřív, proto se nejdřív dočkají
        (`asyncio.wait` nepropustí zrušení jobu, jen vlastní).
        """
        today = now.date()
        if written <= 0 or not self.oi_final or self._oi_day != today:
            return False
        if self._oi_jobs_task is not None:
            await asyncio.wait({self._oi_jobs_task})
        await self._write_forward_field(today)
        return True

    def _log_oi_jobs_result(self, task: asyncio.Task[None]) -> None:
        if task is self._oi_jobs_task:
            self._oi_jobs_task = None
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Joby po OI archivu %s selhaly", self.symbol, exc_info=exc)

    def drop_oi_archive(self, reason: str) -> None:
        """Zahodí rozpracovaný průchod (nový UTC den, zavření trhu, stop, chyba)."""
        archive = self._oi_pass
        if archive is None:
            return
        self._oi_pass = None
        logger.info(
            "OI archiv %s %s: rozpracovaný průchod zahozen (%d/%d kontraktů) — %s",
            self.symbol,
            archive.day,
            archive.offset,
            len(archive.contracts),
            reason,
        )

    async def _oi_archive_failed(self, archive: OIArchivePass) -> bool:
        """Čtení nebo zápis průchodu selhal; volá se z obsluhy výjimky."""
        # Selhání archivace nesmí zabít pipeline (#215: MES CardinalityViolation
        # shodil celý řetěz do cooldownu) — sběr běží dál s volume fallbackem,
        # retry po OI_RETRY_CYCLES cyklech
        logger.exception("OI archivace %s selhala — pokračuje se bez OI", self.symbol)
        if archive.has_snapshot:
            return await self._report_refresh_failed(archive.day, archive.started)
        await self._publish_oi_alert(
            "oi_missing",
            f"OI archivace {self.symbol} selhala — GEX/OI vrstvy zatím bez OI, další "
            "pokus za 30 min (detail v logu enginu)",
            archive.day,
            archive.started,
        )
        return False

    async def _oi_archive_done(self, archive: OIArchivePass, result: ArchiveResult) -> bool:
        """Výsledek celého průchodu: log, upozornění a finalita (joby až po něm)."""
        today, now, has_snapshot = archive.day, archive.started, archive.has_snapshot
        coverage = result.written / max(1, result.written + len(result.missing))
        # Důvod nefinality (#1650): finální je jen průchod bez změněné hodnoty,
        # bez chybějícího dřív archivovaného kontraktu a s pokrytím nad prahem
        logger.info(
            "OI archiv %s %s: %d zapsáno, %d chybí — pokrytí %.1f %%, změněno %d, "
            "chybí dřív archivovaných %d",
            self.symbol,
            today,
            result.written,
            len(result.missing),
            coverage * 100,
            result.changed_values,
            result.missing_archived,
        )
        if result.written == 0:
            if has_snapshot:
                return await self._report_refresh_failed(today, now)
            await self._publish_oi_alert(
                "oi_missing",
                f"OI pro {self.symbol} z IBKR nedorazilo — GEX/OI vrstvy zatím bez OI, "
                "další pokus za 30 min",
                today,
                now,
            )
            return False
        # Úspěšné čtení re-armuje hranový OI alert (#1307) — další selhání
        # téhož dne je nová epizoda a ohlásí se znovu
        self._oi_alerted_day = None
        # Finální je snímek pořízený po publikačním okně, jehož hodnoty se proti
        # předchozímu čtení nezměnily — jedno čtení po okně nestačí, publikace
        # může doběhnout zrovna mezi dvěma dávkami sweepu
        if self._oi_published(now) and not result.changed:
            # Shoda čtení nestačí (#664): 12. 8. dvě shodná čtení 4 kontraktů
            # ze 160 zastavila obnovu 0DTE na celý den — řídký snímek finální
            # být nesmí, obnova jede dál, dokud CME nedopublikuje
            if coverage >= self.settings.oi_final_min_coverage:
                self.oi_final = True
                logger.info("OI archiv %s %s je finální (dvě shodná čtení)", self.symbol, today)
            else:
                logger.info(
                    "OI archiv %s %s: dvě shodná čtení, ale pokrytí %.0f %% je pod prahem "
                    "%.0f %% — obnova pokračuje",
                    self.symbol,
                    today,
                    coverage * 100,
                    self.settings.oi_final_min_coverage * 100,
                )
        return True

    async def _after_oi_archive(self, today: dt.date) -> None:
        """Joby nad (znovu)pořízeným archivem dne: FA a α, sebekontrola, Forward GEX."""
        await self._run_fa_jobs(today)
        await self._run_setup_selfcheck(today)
        await self._write_forward_field(today)

    async def _write_forward_field(self, today: dt.date) -> None:
        """Forward GEX (#519): pole per budoucí den z denního snímku řetězce.

        Počítá se po (znovu)pořízení OI archivu — OI se přes den nemění, takže
        per minuta nemá smysl. Selhání nesmí shodit archivační cestu: forward
        je odvozenina, sběr běží dál.
        """
        try:
            chain = await asyncio.to_thread(self.oi_repository.chain_for_day, self.symbol, today)
            contracts = [
                ForwardContract(
                    expiry=record.expiry,
                    strike=record.strike,
                    right=record.right,
                    oi=record.oi,
                    iv=record.iv,
                )
                for record in chain
            ]
            strikes = sorted({record.strike for record in chain})
            if len(strikes) < 2:
                return
            strike_step = min(b - a for a, b in zip(strikes, strikes[1:], strict=False) if b > a)
            field = await asyncio.to_thread(
                lambda: forward_field(
                    contracts,
                    today=today,
                    grid_start=strikes[0],
                    grid_stop=strikes[-1],
                    grid_step=strike_step / 2.0,
                    multiplier=self.runtime.multiplier,
                )
            )
            if field is None:
                return
            computed_ts = dt.datetime.now(dt.UTC)
            rows = [
                {
                    "day": block.date.isoformat(),
                    "grid_start": field.grid_start,
                    "grid_step": field.grid_step,
                    "values": list(block.values),
                    "dropped_expiries": list(block.dropped_expiries),
                    "dropped_share": block.dropped_share
                    if block.dropped_share is not None
                    else float("nan"),
                    "iv_fallback_share": field.iv_fallback_share,
                    "computed_ts": computed_ts,
                }
                for block in field.days
            ]
            await asyncio.to_thread(self.runtime.writer.write_gexforward, self.symbol, today, rows)
            measured = sum(1 for c in contracts if c.iv is not None)
            logger.info(
                "Forward GEX %s %s: %d dnů, %d kontraktů (%d s měřenou IV, fallback %.0f %%)",
                self.symbol,
                today,
                len(field.days),
                len(contracts),
                measured,
                field.iv_fallback_share * 100,
            )
        except Exception:
            logger.exception("Forward GEX %s se nespočítal — zkusí se po další archivaci", today)

    async def _report_refresh_failed(self, today: dt.date, now: dt.datetime) -> bool:
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
        await self._publish_oi_alert(
            "oi_refresh_failed",
            f"Obnova OI pro {self.symbol} po publikačním okně selhala — jede se na starším "
            "snímku dne, další pokus za 30 min",
            today,
            now,
        )
        return True

    async def _publish_oi_alert(
        self, kind: str, message: str, today: dt.date, now: dt.datetime
    ) -> None:
        """OI alert jen tehdy, když se OI čeká, a hranově (#1307).

        Čeká se při otevřeném trhu v obchodní den CME po publikačním okně
        (`_oi_published`). Jinak selhání čtení není porucha a jde jen do logu:
        zavřený trh (víkend, denní pauza), víkendový UTC den (neděle 17:00 až
        19:00 CT — CME v neděli nepublikuje) a noc před oknem, kdy IBKR
        dodává předpublikační čísla. Hlášení před oknem by navíc spotřebovalo
        hranu dne a skutečná porucha „okno proběhlo, OI pořád nedorazilo" by
        se už neohlásila.

        Po okně se ohlásí první selhání dne, další až po úspěšném čtení
        (`_oi_alerted_day` se nuluje v `_oi_archive_done`). Do #1307 chodila
        tatáž věta každých 30 min pro ES i NQ (Telegram deduplikuje jen
        10 min, zvonek vůbec).
        """
        if is_market_closed(now):
            logger.info(
                "%s %s: trh je zavřený (rozvrh CME) — bez upozornění, jen log: %s",
                kind,
                self.symbol,
                message,
            )
            return
        if not self._oi_published(now):
            logger.info(
                "%s %s: CME pro tento den OI ještě nepublikoval (před oknem nebo víkend) "
                "— bez upozornění, jen log: %s",
                kind,
                self.symbol,
                message,
            )
            return
        if self._oi_alerted_day == today:
            logger.info(
                "%s %s: dnes už ohlášeno, další neúspěšný pokus jen do logu", kind, self.symbol
            )
            return
        self._oi_alerted_day = today
        await self.publisher.publish(
            "alerts",
            {
                "kind": kind,
                "symbol": self.symbol,
                "message": message,
                "ts": now.timestamp(),
            },
        )

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

    async def _run_fa_jobs(self, today: dt.date) -> None:
        """FA validace a kalibrace α jen nad FINÁLNÍM snímkem dne (#1314).

        Obě porovnávají dnešní OI archiv s předchozím obchodním dnem a jejich
        denní dedup bod zamkne. Dřív běžely hned po prvním archivu dne v 00:00
        UTC, tedy nad OI před publikací CME (07:00 CT) — ΔOI ≈ 0 (`doi_abs` 0
        u ES/NQ 22.–24. 9.), medián α 0 a dedup pak nedovolil bod přepočítat
        po publikaci. Finální je snímek po publikačním okně se dvěma shodnými
        čteními (`oi_final`); víkend ani pre-publikační snímek finální nejsou.
        Do té doby se jen propíše uložená α do runtime (start enginu).
        """
        if not self.oi_final:
            await self._restore_alpha()
            return
        await self._run_fa_validation(today)
        await self._run_alpha_calibration(today)

    async def _restore_alpha(self) -> None:
        """Uložená α symbolu do runtime — bez nového kalibračního bodu."""
        if self.alpha_repository is None:
            return
        try:
            state = await asyncio.to_thread(self.alpha_repository.get, self.symbol)
        except Exception:
            logger.exception("α %s se nepodařilo načíst — platí konfigurace", self.symbol)
            return
        self._apply_alpha(state)

    def _cum_tracker(self) -> CumDeltaTracker:
        """CumΔ tracker aktivního runtime — jeho zdroj je jediná pravda o v5/v6."""
        tracker = self.runtime.cum_delta
        assert tracker is not None  # nastaven v EngineRuntime.__post_init__
        return tracker

    def _apply_alpha(self, state: FaAlphaState | None) -> None:
        """Faktor pro FA vrstvu podle stavu kalibrace (`live_alpha`).

        Pod mechanikou v5 se propíše uložená α (bez stavu platí konfigurace).
        Pod v6 (ADR-0057 bod 6, ADR-0061) jen ρ z objemu s ≥ `FA_MIN_DAYS` dny,
        jinak 0 = FA vypnutá („nekalibrováno n/5“), i bez stavu.
        """
        prints = self._cum_tracker().prints
        alpha = live_alpha(state, prints=prints)
        if alpha is None or self.runtime.flow_alpha == alpha:
            return
        self.runtime.flow_alpha = alpha
        if state is not None and alpha == state.alpha:
            logger.info(
                "α %s obnovena z kalibrace: %.3f (%d dnů)",
                self.symbol,
                state.alpha,
                state.days,
            )
        else:
            days = (
                state.days
                if state is not None and state.source == calibration_source(prints=prints)
                else 0
            )
            logger.info(
                "FA %s vypnutá do kalibrace ρ z objemu: %d/%d dnů (ADR-0061)",
                self.symbol,
                days,
                FA_MIN_DAYS,
            )

    async def _run_fa_validation(self, today: dt.date) -> None:
        """Denní FA validace (#232): open-ratio bod za včerejší volume vs. dnešní ΔOI.

        Běží po finálním OI archivu (`_run_fa_jobs`, #1314); selhání nesmí
        zabít pipeline — bod se dopočítá při dalším pokusu (idempotentní dedup
        v tabulce fa_validation).
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
                publication_utc=self.settings.oi_publication_utc,
            )
        except Exception:
            logger.exception("FA validace %s selhala — zkusí se při dalším OI cyklu", self.symbol)
            return
        alpha = self._current_alpha()
        factor = "ρ" if self._cum_tracker().prints else "α"
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
                        f"({factor}={alpha:.2f}, ADR-0011)"
                    ),
                    "ts": dt.datetime.now(dt.UTC).timestamp(),
                },
            )

    def _current_alpha(self) -> float:
        """Aktuálně platná α symbolu: kalibrovaná z runtime, jinak konfigurace (v6: 0)."""
        return self.runtime.fa_alpha()

    async def _run_alpha_calibration(self, today: dt.date) -> None:
        """Ranní kalibrace FA (#232 fáze 2): v5 α z netflow, v6 ρ z objemu (ADR-0061).

        Běží po FA validaci nad týmiž archivy, jen nad finálním snímkem
        (`_run_fa_jobs`, #1314); selhání nesmí zabít pipeline — bod se
        dopočítá při dalším OI cyklu (idempotentní dedup v historii).
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
                publication_utc=self.settings.oi_publication_utc,
                snapshots_dir=self.settings.snapshots_dir,
                cumdelta_prints=self._cum_tracker().prints,
            )
        except Exception:
            logger.exception("Kalibrace FA %s selhala — zkusí se při dalším OI cyklu", self.symbol)
            return
        if result is None:
            await self._restore_alpha()
            return
        prints = self._cum_tracker().prints
        # Pod v6 jde ρ do FA až po FA_MIN_DAYS dnech (ADR-0057 bod 6, ADR-0061)
        self.runtime.flow_alpha = live_alpha(
            FaAlphaState(
                symbol=self.symbol,
                alpha=result.alpha_after,
                days=result.days,
                updated_at=dt.datetime.now(dt.UTC),
                source=calibration_source(prints=prints),
            ),
            prints=prints,
        )
        point = result.point
        # Pod v6 je faktor ρ = medián ΔOI/objem seance (ADR-0061), pod v5 α = ΔOI/net
        factor, ratio = ("ρ", "ΔOI/objem") if prints else ("α", "ΔOI/net")
        logger.info(
            "Kalibrace %s %s %s (%s): medián %.3f (buy %s / sell %s, %d stran) → %s %.3f (%d dnů)",
            factor,
            self.symbol,
            result.expiry,
            result.day,
            point.ratio_median,
            "—" if point.ratio_buy is None else f"{point.ratio_buy:.3f}",
            "—" if point.ratio_sell is None else f"{point.ratio_sell:.3f}",
            point.samples,
            factor,
            result.alpha_after,
            result.days,
        )
        await self.publisher.publish(
            "alerts",
            {
                "kind": "fa_calibration",
                "symbol": self.symbol,
                "message": (
                    f"Kalibrace FA {factor} {self.symbol}: denní medián {ratio} "
                    f"{point.ratio_median:.2f} ({point.samples} stran) → "
                    f"{factor} = {result.alpha_after:.2f} po {result.days} dnech"
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
        override = self.spot_override()
        if override is not None and override == override:
            self.spot = override
            return override
        last = self.ticker.last
        if last == last:  # není NaN
            self.spot = last
            return last
        market = self.ticker.marketPrice()
        if market == market:
            self.spot = market
        return self.spot

    def _live_spot(
        self, spot: float, market_closed: bool, now: dt.datetime
    ) -> tuple[float | None, str | None]:
        """Cena, ze které smí hlasovat verdikt a tendence, a důvod, když chybí.

        Pod v5 (nebo bez napojeného zdroje) je to spot cyklu jako dosud. Pod v6
        (#1600) rozhoduje stáří zdrojů (`compute/spot.resolve_spot`): GEX výpočet
        a obálka dál běží s nejčerstvější známou cenou (`self.spot`), hlasy ne.
        Při zavřeném trhu a `SPOT_WATCH_GRACE` po startu a otevření se chybějící
        spot nehlásí a stav živosti se nemění (#1307): výpadek, který začal
        o víkendu, varuje až po odkladu po nedělním otevření, ne nikdy.
        """
        if not self.spot_age_gate or self.spot_reading is None:
            return spot, None
        if market_closed:
            self._spot_watch_from = None  # odklad začne prvním cyklem otevřeného trhu
        elif self._spot_watch_from is None:
            self._spot_watch_from = now + SPOT_WATCH_GRACE
        reading = self.spot_reading()
        if reading.price is not None:
            self.spot = reading.price
        if reading.fresh:
            if not self._spot_live:
                logger.info("Spot %s: živá cena zpět (%s)", self.symbol, reading.source)
            self._spot_live = True
            return reading.price, None
        reason = reading.describe(dt.datetime.now(dt.UTC))
        # Zavřený trh = žádná upozornění na chybějící data (AGENTS.md, #1307)
        watching = self._spot_watch_from is not None and now >= self._spot_watch_from
        if not watching:
            return None, reason
        if self._spot_live:
            logger.warning(
                "Spot %s: žádný zdroj nemá živou cenu (%s) — verdikt a tendence bez ceny",
                self.symbol,
                reason,
            )
        self._spot_live = False
        return None, reason

    async def run_minute(self, now: dt.datetime) -> SweepMetrics:
        """Jeden minutový cyklus instrumentu: OI retry, expanze obálky, runtime cyklus."""
        # Očekávají se data? (#1307) Jeden predikát z rozvrhu CME (ADR-0023) pro
        # OI retry i všechny hlídače níž; hrana zavřeno → otevřeno se zjišťuje
        # jen tady, ještě před sweepem, aby první sweep seance začal čistý.
        market_closed = is_market_closed(now)
        if self._market_closed and not market_closed:
            self._on_market_open()
        elif market_closed and self._market_closed is False:
            # Obnova se při zavřeném trhu nečte (#1307); po otevření jde nová hned.
            # Čtení nového UTC dne otevřené v zavřeném trhu hranu nemá a doběhne,
            # stejně jako první archiv po startu v zavřeném trhu (#1674)
            self.drop_oi_archive("trh se zavřel")
        self._market_closed = market_closed
        # Nový den (#494): finalita i dostupnost OI patřily včerejšku. Pipeline
        # s nedenní nejbližší expirací přežije půlnoc a bez resetu by se nový
        # den nikdy nearchivoval (oi_final=True vypíná retry blok níže).
        today = now.date()
        new_day = self._oi_day is not None and self._oi_day != today
        if new_day:
            self.oi_available = False
            self.oi_final = False
            self._cycles_since_oi = OI_RETRY_CYCLES  # archivuj hned, ne až za 30 min
            # Čtení po půlnoci UTC patří novému dni, ne rozpracovanému včerejšku
            self.drop_oi_archive("nový UTC den")
        self._oi_day = today
        # Retry běží nejen když OI chybí, ale i dokud snímek není finální (#463):
        # předpublikační čísla jsou nenulová, takže se bez toho nikdy neobnoví.
        # Dokud se čte rozpracovaný průchod (i první archiv po založení, #1674),
        # retry čeká — dva archivy téhož dne naráz by se přepisovaly.
        # Při zavřeném trhu (#1307) se neopakuje ani neobnovuje: nic nového se
        # nepublikuje a každý pokus by jen selhal (sobota 26. 9.: 0 zapsáno
        # à 30 min). Výjimkou je jediné čtení na začátku nového UTC dne
        # (půlnoc UTC padá do zavřeného trhu jen v pátek a v sobotu večer CT):
        # runtime čte OI podle UTC dne, takže bez něj by víkendová mapa OI
        # neměla vůbec. Na hraně otevření natáhne čítač `_on_market_open`,
        # takže chybějící OI (nebo nefinální snímek po okně) se čte hned.
        # Cyklus průchod jen otevře; dávky čte `advance_oi_archives` po sweepech
        # ve zbytku minuty (#1646) — celý průchod ES tu dřív stál ~100 s.
        if self._oi_pass is None and (not self.oi_available or self._oi_refresh_due(now)):
            self._cycles_since_oi += 1
            if self._cycles_since_oi >= OI_RETRY_CYCLES and (not market_closed or new_day):
                self._cycles_since_oi = 0
                await self.start_oi_archive(today, now)

        spot = self._current_spot()
        live_spot, spot_missing = self._live_spot(spot, market_closed, now)
        spot = self.spot

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

        # Stav mapy (#1245) — PŘED detektorem, ať feature log nese stav téže
        # minuty; max pain sdílí s tendencí (obnova jen při novém snímku OI).
        # Pod v6 jen z živé ceny (#1657): bez ní je stav minuty neznámý
        if self.map_state is not None:
            try:
                await self.map_state.on_minute(
                    now,
                    live_spot,
                    self.runtime,
                    self.tendency_engine.max_pain if self.tendency_engine is not None else None,
                )
            except Exception:
                logger.exception("Stav mapy %s selhal — pokračuji", self.symbol)
        # Setup detektor (ADR-0004) — jeho pád nesmí shodit sběr dat
        if self.setup_engine is not None:
            try:
                await self.setup_engine.on_minute(now, spot, bars, self.runtime)
            except Exception:
                logger.exception("Setup detektor %s selhal — pokračuji", self.symbol)
        # Kandidát T9 (#577, fáze 1) — tichý sběr, stejný kontrakt pádu
        if self.t9_probes is not None:
            try:
                await self.t9_probes.on_minute(now, spot, bars, self.runtime)
            except Exception:
                logger.exception("T9 probe sběr %s selhal — pokračuji", self.symbol)
        # Indikátor tendence (#350) — stejný kontrakt: pád nesmí shodit sběr
        if self.tendency_engine is not None:
            try:
                await self.tendency_engine.on_minute(now, live_spot, self.runtime)
            except Exception:
                logger.exception("Indikátor tendence %s selhal — pokračuji", self.symbol)
        # Sběrač kandidátů T6 (#256) — sám se hlídá na jeden běh denně; pod v6
        # jen z živé ceny (#1657), bez ní čeká nejvýš do US openu
        if self.t6_collector is not None:
            try:
                await self.t6_collector.on_minute(now, live_spot, self.runtime)
            except Exception:
                logger.exception("T6 sběrač %s selhal — pokračuji", self.symbol)
        # Gamma útes (#576) — jednou po settle; pád nesmí shodit sběr
        if self.gamma_cliff is not None:
            try:
                await self.gamma_cliff.on_minute(now)
            except Exception:
                logger.exception("Gamma útes %s selhal — pokračuji", self.symbol)
        # Verdikt dne (#1595) — PO tendenci a stavu mapy (hlasují hodnotou téže
        # minuty), PŘED scénářem; pád nesmí shodit sběr dat
        pre_open = None
        if self.verdict_writer is not None:
            try:
                pre_open = await self.verdict_writer.on_minute(
                    now, live_spot, self.runtime, spot_missing
                )
            except Exception:
                logger.exception("Verdikt dne %s selhal — pokračuji", self.symbol)
        # Automatický scénář dne (#1173 A) — jednou před US openem z verdiktu téže minuty
        if self.scenario_generator is not None:
            try:
                await self.scenario_generator.on_minute(now, pre_open, self.runtime)
            except Exception:
                logger.exception("Automatický scénář %s selhal — pokračuji", self.symbol)
        # Scénáře dne (#1173) — jednou po settle, jen scénáře po termínu
        if self.scenario_collector is not None:
            try:
                await self.scenario_collector.on_minute(now)
            except Exception:
                logger.exception("Vyhodnocení scénářů %s selhalo — pokračuji", self.symbol)
        # Paper účet (#1187): fily proti barům minuty — pád nesmí shodit sběr dat
        if self.paper_broker is not None:
            try:
                await self.paper_broker.on_minute(now, spot, bars, self.runtime)
            except Exception:
                logger.exception("Paper účet %s selhal — pokračuji", self.symbol)
        # Volatilitní režim (ADR-0028) — jednou po settle, čte jen bary
        if self.vol_regime is not None:
            try:
                await self.vol_regime.on_minute(now)
            except Exception:
                logger.exception("Volatilitní režim %s selhal — pokračuji", self.symbol)
        # Respektování pásma EM (#872) — jednou po settle; pád nesmí shodit sběr
        if self.em_respect is not None:
            try:
                await self.em_respect.on_minute(now)
            except Exception:
                logger.exception("EM respect %s selhal — pokračuji", self.symbol)
        # Verdikty dne (#1091) — jednou po settle, čte jen bary a em_respect
        if self.briefing_verdicts is not None:
            try:
                await self.briefing_verdicts.on_minute(now)
            except Exception:
                logger.exception("Vyhodnocení verdiktů %s selhalo — pokračuji", self.symbol)
        # IV Rank (#871) — jednou po settle; historický request, žádná linka
        if self.iv_rank is not None:
            try:
                await self.iv_rank.on_minute(now)
            except Exception:
                logger.exception("IV rank %s selhal — pokračuji", self.symbol)

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

    def _on_market_open(self) -> None:
        """Hrana zavřeno → otevřeno (#1307): zahodí stav hlídačů ze zavřeného trhu.

        Sweep běží i o víkendu a v denní pauze; TWS mimo seanci nedodává
        modelGreeks ani kompletní kotace, takže scheduler nasbírá repair kola
        a backoff. Bez resetu by striky začaly seanci v backoffu a s hotovým
        `strikes_stalled`. Čítače BS fallbacku zůstávají — kontrakt bez greeks
        od TWS dostane BS dopočet z čerstvých kotací hned prvním sweepem.
        Detektory se nulují spolu se schedulerem — `recovered` k poplachu
        z minulé seance by po vynulování kol přišel naprázdno.

        Čítač retry OI se natáhne: při zavřeném trhu obnova neběží a čítač se
        nezvyšuje, takže by první obnova po denní pauze přišla až za
        OI_RETRY_CYCLES minut. Pokus jde jen tam, kde je co číst — OI chybí,
        nebo snímek po publikačním okně ještě není finální.
        """
        self._cycles_since_oi = OI_RETRY_CYCLES
        self.runtime.scheduler.reset_repair_state()
        if self.next_runtime is not None:
            self.next_runtime.scheduler.reset_repair_state()
        if self.greeks_detector is not None:
            self.greeks_detector.reset()
        if self.repair_detector is not None:
            self.repair_detector.reset()
        logger.info(
            "Trh %s otevřel (rozvrh CME) — repair a backoff stav ze zavřeného trhu vynulován",
            self.symbol,
        )

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
        # Zavřený trh (#1307): TWS mimo seanci modelGreeks nepočítá, chybějící
        # Greeks nejsou porucha — detektor se nekrmí (stav z doby před zavřením
        # vynuluje hrana otevření v `run_minute`). 26. 9. 2026 tak odešlo
        # „Greeks ES nechodí pro 89 z 122" tři minuty po sobotním rollu.
        if is_market_closed(now):
            return
        # Po settle expirující řady se nehlídá vůbec (#959) — detektor se ani
        # nekrmí, jinak by si zapamatoval „stalled" a po rollu vystřelil
        # „recovered" k poplachu, který nikdy neodešel
        if not greeks_watch_applies(
            self.runtime.expiry, now, self.runtime.trading_class, self.runtime.symbol
        ):
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
        # Zavřený trh (#1307): repair kola mimo seanci selhávají z definice —
        # pátek 16:20 CT a znovu po sobotním rollu tak chodilo „TWS dlouhodobě
        # nedodává kompletní data pro 62 striků". Detektor se nekrmí; kola
        # nasbíraná při zavřeném trhu zahodí hrana otevření v `run_minute`.
        if is_market_closed(now):
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

        Počítá se při otevřeném trhu i bez pohybu spotu (#1347): stojící stream
        i spot je výpadek, ne klid. Spot rozliší jen text upozornění.

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
        # Zavřený trh (#1307): „zavřený trh = spot se nehýbe" neplatí v pre-openu
        # (neděle 16:00–17:00 CT, denní pauza) — indikativní kotace i mid z tasty
        # fallbacku se hýbou, TRADES bary ale nevznikají. Detektor se nekrmí a
        # stream se neobnovuje; stav se NEnuluje, aby stall z běžící seance po
        # otevření skončil `recovered` a re-backfillem díry.
        if is_market_closed(now):
            return
        # Otevřený trh (výše) = bary se čekají i při stojícím spotu (#1347)
        event = detector.observe(bar_activity=bar_activity, market_open=True)
        if event == "stalled":
            spot_note = "spot přitom žije" if spot_moving else "stojí i spot (zamrzlý stream)"
            logger.error(
                "Real-time bary %s nechodí ≥ %d min při otevřeném trhu, %s — mrtvý "
                "reqRealTimeBars stream (výpadek TWS farem / Error 1100?); "
                "svíčky se nekreslí, stream se obnovuje",
                self.symbol,
                self.settings.bars_stall_alert_minutes,
                spot_note,
            )
            await self.publisher.publish(
                "alerts",
                {
                    "kind": "bars_stalled",
                    "symbol": self.symbol,
                    "message": f"Svíčky {self.symbol} se přestaly kreslit — real-time "
                    f"bary z TWS nechodí ≥ {self.settings.bars_stall_alert_minutes} min "
                    f"při otevřeném trhu, {spot_note}. Engine stream obnovuje sám; když "
                    "to nepomůže, pomáhá restart TWS. Díra se po návratu doplní backfillem.",
                    "ts": now.timestamp(),
                },
            )
            await self._restart_bars_stream()
        elif event == "still_stalled":
            # Obnova nepomohla — další pokus, alert se neopakuje (anti-spam)
            logger.warning(
                "Real-time bary %s nechodí ani po obnově streamu — zkouším znovu",
                self.symbol,
            )
            await self._restart_bars_stream()
        elif event == "recovered":
            logger.info(
                "Real-time bary %s zase chodí — díra se doplní backfillem (tasty zatím "
                "doplnila %d barů)",
                self.symbol,
                self._gap_filled,
            )
            self._gap_filled = 0
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
        # Dokud stall trvá, svíčky doplňuje tasty (#614 doplněk) — každý cyklus
        # minuty, které v particích chybí; měřené bary při návratu IBKR vyhrají
        if detector.stalled and self.bar_gap_fill is not None:
            self._schedule_gap_fill(now)

    def _schedule_gap_fill(self, now: dt.datetime) -> None:
        """Jedno doplnění naráz; okno = poslední stall práh + rezerva, ne celý den
        (celodenní díry patří backfillu při návratu, tohle drží graf živý)."""
        if self._gap_fill_task is not None and not self._gap_fill_task.done():
            return
        if self.bar_gap_fill is None:
            return
        until = now.replace(second=0, microsecond=0)
        since = until - dt.timedelta(minutes=max(self.settings.bars_stall_alert_minutes, 3) + 5)
        task: asyncio.Task[int] = asyncio.ensure_future(self.bar_gap_fill(since, until))
        task.add_done_callback(self._log_gap_fill_result)
        self._gap_fill_task = task

    def _log_gap_fill_result(self, task: asyncio.Task[int]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning("Doplnění barů %s z tasty během stall selhalo: %s", self.symbol, exc)
            return
        filled = task.result()
        if filled and self._gap_filled == 0:
            logger.warning(
                "Svíčky %s během výpadku IBKR doplňuje tastytrade (#614) — úsek je "
                "v UI označený jako doplněný, ne měřený",
                self.symbol,
            )
        self._gap_filled += filled

    async def _restart_bars_stream(self) -> None:
        """Zruší mrtvý reqRealTimeBars stream a založí nový (#1082).

        Selhání obnovy nesmí shodit minutový cyklus — zaloguje se a další
        pokus přijde po dalším prahu detektoru (`still_stalled`).
        """
        if self.restart_bars is None:
            return
        try:
            await self.restart_bars()
        except Exception:
            logger.exception("Obnova real-time barů %s selhala", self.symbol)

    async def _check_vol_concentration(self, now: dt.datetime) -> None:
        """Alert na neobvyklou koncentraci volume na příští expiraci (#208).

        Alanův event-workflow: jeden dominantní strike zítřejšího řetězu =
        úroveň, kde se trh zajišťuje na event. Jeden alert per leader
        (nová dominantní strana se ohlásí znovu).

        Při zavřeném trhu (#1309) se nehledá: alert tvrdí „trh se TEĎ
        zajišťuje", ale objem kotací je zmrzlý z poslední seance a po
        sobotním rollu se nový sekundární řetěz plní ze snímků postupně.
        26. 9. 2026 00:05 UTC (tři hodiny po páteční uzávěrce) tak odešlo
        „ES 20260929: 7900C 19,7× medián" nad 10 snímky ze 122 kontraktů
        s páteční volume a v 01:05 další leader nad 39 ze 160. Sekundární
        sweep běží dál (víkendová mapa), jen detektor ne.
        """
        runtime = self.next_runtime
        if runtime is None or is_market_closed(now):
            return
        # Aktivní zdroj řetězu (#614 fáze 2b): za fallbacku objem chybí, takže
        # `min_volume` alert utne. Čtení přímo ze sweep cache by naopak nechalo
        # vyhlásit koncentraci nad zmrzlými čísly z doby před výpadkem.
        volumes = {
            (spec.strike, spec.right): float(cached.snapshot.volume or 0.0)
            for spec, cached in runtime.current_quotes().items()
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

    def roll_handover(self) -> EngineRuntime | None:
        """Sekundár, jehož tracker v rollu v settle převezme nová pipeline (#1460, varianta C).

        Jen pod tisky (ADR-0057 bod 6, doplněk 20. 10.): pod midpointem dává
        net sekundáru bar větev a navazuje se z partice, předání by `take_over`
        odmítl. None = pipeline bez sekundáru nebo mechanika v5.
        """
        secondary = self.next_runtime
        if secondary is None or secondary.cum_delta is None or not secondary.cum_delta.prints:
            return None
        return secondary

    def stop(self) -> None:
        """Odhlášení market dat podkladu (kontrakty řetězce rotuje scheduler sám)."""
        if self._backfill_task is not None:
            self._backfill_task.cancel()
        if self._gap_fill_task is not None:
            self._gap_fill_task.cancel()
        if self._oi_jobs_task is not None:
            self._oi_jobs_task.cancel()
        self.drop_oi_archive("pipeline končí")
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


async def advance_oi_archives(
    pipelines: Sequence[InstrumentPipeline],
    next_cycle: float,
    *,
    clock: Callable[[], float] | None = None,
    timeout_s: float = CYCLE_TIMEOUT_S,
) -> None:
    """Rozpracované OI archivy ve zbytku minuty, až po sweepech všech pipeline (#1646).

    Hlavní smyčka volá na konci iterace: dávka archivu tak nikdy neběží
    souběžně se sweepem (100 lines, ADR-0001 bod 4; `batch_size` je jeden
    strop pro obojí) a nezdrží cyklus další minuty. `next_cycle` je start
    příští iterace v čase smyčky (`loop.time()`); dávky končí nejpozději
    OI_ARCHIVE_RESERVE_S před ním. Visící await jedné pipeline hlídá
    watchdog jako v `gather_metrics`, pád ani timeout nezastaví ostatní.
    """
    now = clock or asyncio.get_running_loop().time
    deadline = next_cycle - OI_ARCHIVE_RESERVE_S
    for pipeline in pipelines:
        try:
            await asyncio.wait_for(pipeline.advance_oi_archive(deadline, now), timeout=timeout_s)
        except TimeoutError:
            logger.error(
                "OI archiv %s nedoběhl do %g s (visící IBKR await?)", pipeline.symbol, timeout_s
            )
            pipeline.drop_oi_archive("watchdog")
        except Exception:
            logger.exception("OI archiv %s po dávkách selhal", pipeline.symbol)
            pipeline.drop_oi_archive("chyba")


def aggregate_status(
    results: Sequence[tuple[str, SweepMetrics | None]],
) -> dict[str, object]:
    """Agregovaný status pipeline přes instrumenty (stavová lišta ukazuje součty)."""
    valid = [metrics for _, metrics in results if metrics is not None]
    return {
        "greeks_complete": sum(m.greeks_complete for m in valid),
        "greeks_total": sum(m.total for m in valid),
        "repair_count": sum(m.stale_count for m in valid),
        # OI pokrytí aktivních řetězů (#664): archiv IBKR / tasty fill / díra
        "oi_present": sum(m.oi_present for m in valid),
        "oi_filled": sum(m.oi_filled for m in valid),
        "oi_missing": sum(m.oi_missing for m in valid),
        "symbols": ",".join(symbol for symbol, _ in results),
    }


async def read_watchlist(reader: WatchlistReader) -> list[str]:
    """Watchlist z DB mimo event loop (sync SQLAlchemy)."""
    return await asyncio.to_thread(reader.symbols)
