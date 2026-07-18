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
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.engine import Engine

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
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.runtime import EngineRuntime, PublisherLike
from gexlens_engine.setups import SetupEngine
from gexlens_engine.storage.meta import meta_metadata, settings_table, watchlist_table
from gexlens_engine.storage.oi_archive import OIArchiver, OIEodRepository

logger = logging.getLogger(__name__)

# OI retry každých ~30 minutových cyklů (CME publikuje OI jednou denně ráno, ADR-0001)
OI_RETRY_CYCLES = 30
# Cooldown po selhání setupu instrumentu (např. neznámý symbol) — cyklů do dalšího pokusu
SETUP_RETRY_CYCLES = 30


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
    on_stop: Callable[[], None] = lambda: None
    spot: float = 0.0
    oi_available: bool = False
    # OI archiv pokrývá i další expirace (ΔOI vs. včera); None = jen aktivní řetěz
    archive_contracts: Sequence[OptionContractSpec] | None = None
    # Sekundární runtime následující expirace (čtení positioningu příští seance)
    next_runtime: EngineRuntime | None = None
    # Setup detektor (ADR-0004) — None = vypnuto
    setup_engine: SetupEngine | None = None
    _cycles_since_oi: int = field(default=0, repr=False)
    _minute_count: int = field(default=0, repr=False)

    async def try_archive_oi(self, today: dt.date) -> bool:
        """Denní OI archiv; při úplném selhání alert do UI (ADR-0001 v2)."""
        if today in self.oi_repository.days(self.symbol):
            return True
        contracts = self.archive_contracts or self.runtime.contracts
        result = await self.archiver.archive_day(contracts, today)
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
        return True

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
        metrics = await self.runtime.run_cycle(now, spot, bars)

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
            except Exception:
                logger.exception(
                    "Sekundární cyklus %s %s selhal — pokračuji",
                    self.symbol,
                    self.next_runtime.expiry,
                )
        self._minute_count += 1
        return metrics

    def stop(self) -> None:
        """Odhlášení market dat podkladu (kontrakty řetězce rotuje scheduler sám)."""
        try:
            self.on_stop()
        except Exception:
            logger.exception("Stop pipeline %s selhal — pokračuji", self.symbol)


def _underlying_for(symbol: str, info: ExpiryInfo) -> Underlying:
    """Minimální podklad pro build_contracts — po discovery stačí symbol a burza."""
    return Underlying(symbol=symbol, sec_type="FUT", exchange=info.exchange, con_id=0)


async def gather_metrics(
    pipelines: Sequence[InstrumentPipeline], now: dt.datetime
) -> list[tuple[str, SweepMetrics | None]]:
    """Sekvenční cykly všech pipeline (špička lines = jedna dávka; SPEC kap. 8 odolnost)."""
    results: list[tuple[str, SweepMetrics | None]] = []
    for pipeline in pipelines:
        try:
            results.append((pipeline.symbol, await pipeline.run_minute(now)))
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
