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
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.engine import Engine

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
from gexlens_engine.ibkr.scheduler import SweepMetrics
from gexlens_engine.ibkr.underlying import Bar, BarsStallDetector
from gexlens_engine.runtime import EngineRuntime, PublisherLike
from gexlens_engine.setups import SetupEngine
from gexlens_engine.storage.fa_validation import FaValidationRepository, collect_fa_validation
from gexlens_engine.storage.meta import meta_metadata, settings_table, watchlist_table
from gexlens_engine.storage.oi_archive import OIArchiver, OIEodRepository

logger = logging.getLogger(__name__)

# OI retry každých ~30 minutových cyklů (CME publikuje OI jednou denně ráno, ADR-0001)
OI_RETRY_CYCLES = 30
# Cooldown po selhání setupu instrumentu (např. neznámý symbol) — cyklů do dalšího pokusu
SETUP_RETRY_CYCLES = 30
# Watchdog minutového cyklu (#219): sweep bez timeoutu umí po výpadku IBKR viset
# navždy (future se nikdy nevyřeší) a zastavit celý orchestrátor. Běžný sweep
# trvá 0.5–35 s; strop je velkorysý, aby nezabíjel legitimní první sweep.
CYCLE_TIMEOUT_S = 240.0


class InstrumentSetupError(RuntimeError):
    """Instrument nejde nastartovat (neznámý symbol, chybí FOP řetězec, …)."""


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


def clamp_strike_range(value: object, settings: Settings) -> float | None:
    """Validní nová šířka pásma z runtime nastavení; None = beze změny/nevalidní.

    Meze: minimálně 50 bodů (smysluplné pásmo), maximálně polovina
    strike_range_max_points (invariant konfigurace: max ≥ 2× šířka).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        points = float(value)
    except ValueError:
        return None
    clamped = min(max(points, 50.0), settings.strike_range_max_points / 2)
    return None if clamped == settings.strike_range_points else clamped


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
    # OI archiv pokrývá i další expirace (ΔOI vs. včera); None = jen aktivní řetěz
    archive_contracts: Sequence[OptionContractSpec] | None = None
    # Sekundární runtime následující expirace (čtení positioningu příští seance)
    next_runtime: EngineRuntime | None = None
    # Setup detektor (ADR-0004) — None = vypnuto
    setup_engine: SetupEngine | None = None
    # Denní FA validace po OI archivu (#232) — None = vypnuto
    fa_repository: FaValidationRepository | None = None
    # Hlídání tiché ztráty 5s barů (#221); default z konfigurace v __post_init__
    stall_detector: BarsStallDetector | None = None
    # Re-backfill dnešních barů po návratu streamu (#221); None = backfill nezapojen
    backfill_today: Callable[[], Awaitable[None]] | None = None
    _cycles_since_oi: int = field(default=0, repr=False)
    _minute_count: int = field(default=0, repr=False)
    _last_spot: float = field(default=float("nan"), repr=False)
    _backfill_task: asyncio.Task[None] | None = field(default=None, repr=False)
    # Vol koncentrace (#208): už ohlášené strany (expirace, strike, right) —
    # jeden alert per leader; pipeline se denně překlápí, reset je přirozený
    _vol_alerted: set[tuple[str, float, str]] = field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        if self.stall_detector is None:
            self.stall_detector = BarsStallDetector(self.settings.bars_stall_alert_minutes)

    async def try_archive_oi(self, today: dt.date) -> bool:
        """Denní OI archiv; při úplném selhání alert do UI (ADR-0001 v2)."""
        if today in self.oi_repository.days(self.symbol):
            await self._run_fa_validation(today)
            return True
        contracts = self.archive_contracts or self.runtime.contracts
        try:
            result = await self.archiver.archive_day(contracts, today)
        except Exception:
            # Selhání archivace nesmí zabít pipeline (#215: MES CardinalityViolation
            # shodil celý řetěz do cooldownu) — sběr běží dál s volume fallbackem,
            # retry po OI_RETRY_CYCLES cyklech
            logger.exception("OI archivace %s selhala — pokračuje se bez OI", self.symbol)
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
        await self._run_fa_validation(today)
        return True

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
                        f"(α=0.4, ADR-0011)"
                    ),
                    "ts": dt.datetime.now(dt.UTC).timestamp(),
                },
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
        if not self.oi_available:
            self._cycles_since_oi += 1
            if self._cycles_since_oi >= OI_RETRY_CYCLES:
                self._cycles_since_oi = 0
                self.oi_available = await self.try_archive_oi(now.date())

        spot = self._current_spot()

        # Auto-rozšíření denní obálky (ADR-0002)
        expansion = self.discovery.maybe_expand(self.info, self.band, spot)
        if expansion.expanded:
            self.band = expansion.band
            self.runtime.contracts = build_contracts(
                _underlying_for(self.symbol, self.info), self.info, self.band
            )
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
        await self._watch_bars(now, spot, bars, forming)
        metrics = await self.runtime.run_cycle(now, spot, bars, forming)

        # Setup detektor (ADR-0004) — jeho pád nesmí shodit sběr dat
        if self.setup_engine is not None:
            try:
                await self.setup_engine.on_minute(now, spot, bars, self.runtime)
            except Exception:
                logger.exception("Setup detektor %s selhal — pokračuji", self.symbol)

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
