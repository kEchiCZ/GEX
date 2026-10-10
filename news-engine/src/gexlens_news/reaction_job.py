"""Plánovač a zápis reakcí (#276, SPEC 3.1 „reaction scheduler").

Reakce se počítají až po uzavření nejdelšího okna — dřív by měření bylo
useknuté. Job proto bere eventy starší než `max(windows)` minut, které ještě
reakce nemají, a dopočítá je. Běží periodicky i jako noční sanity průchod,
takže výpadek nic neztratí (archiv barů je věčný, S4).

Minutová fáze se doměřuje dvěma spouštěči jedné metody (`complete_minute`,
#1494, rozhodnutí vlastníka 10. 10.: A + D):

* **D** — denní fáze doměří minutovou zprávám, které zapisuje. Bez toho denní
  fronta (eventy starší 16 dní) zapsala řádek eventu, který minutová fronta
  ještě neměřila, a ten z ní navždy vypadl (backfill 17. 8., 27 968 párů);
* **A** — jednou za hodinu eventy posledních 3 dní, kterým symbol chybí
  (výpadek barů jednoho symbolu v T+60, který `ibkr_hist` později doplní).
  Hodinová brána brání pasti #655: pár bez barů se zkusí nejvýš ~72×, pak ho
  převezme D.
"""

import datetime as dt
import logging
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import and_, exists, func, insert, not_, select, update
from sqlalchemy.engine import Connection, Engine

from gexlens_engine.compute.settle import settle_ts, trading_session_date
from gexlens_engine.compute.setups import gex_regime
from gexlens_engine.storage.sentiment import (
    REACTION_DAILY_WINDOWS,
    REACTION_WINDOWS,
    ReactionWindow,
    news_events,
    news_reactions,
    reaction_row_values,
)
from gexlens_news.bars import BarsRepository
from gexlens_news.reactions import (
    DAILY_WINDOW_DAYS,
    DEFAULT_WINDOWS,
    MIN_BASELINE_SESSIONS,
    MIN_MINUTE_SAMPLES,
    MINUTES_PER_TRADING_DAY,
    Reaction,
    SessionDaily,
    VolumeBaseline,
    build_volume_baseline,
    compute_daily_reactions,
    compute_reactions,
    contaminates,
)

logger = logging.getLogger(__name__)

# Importance, od které event kontaminuje cizí okno (SPEC 5.1); jen jiná
# kategorie (`reactions.contaminates`, K1 ADR-0045)
CONTAMINATION_MIN_IMPORTANCE = 2
# Jak daleko zpět hledat poslední obchodovaný bar před zprávou. Musí pokrýt
# nejdelší zavření: pátek 16:00 CT → neděle 17:00 CT, a k tomu svátek navíc.
CLOSURE_LOOKBACK_DAYS = 5
# Totéž dopředu — první obchodovaný bar po víkendové zprávě (SPEC 5.1 deferred)
CLOSURE_LOOKAHEAD_DAYS = 5
# Denní okna (#564): event je zralý, až jde uzavřít NEJDELŠÍ okno — všechna
# denní okna se zapisují najednou (parciální zápis by rozbil pending dotaz).
# 10 obchodních dní ≈ 14 kalendářních + rezerva na svátky.
DAILY_READY_CALENDAR_DAYS = 16
# Doměření minutové fáze po výpadku symbolu (A, #1494): jak často, jak daleko
# zpět a kolik eventů naráz. Hodina = pár bez barů se za 3 dny zkusí nejvýš ~72×
MINUTE_RETRY_EVERY = dt.timedelta(hours=1)
MINUTE_RETRY_LOOKBACK = dt.timedelta(days=3)
MINUTE_RETRY_LIMIT = 200
# Dávka dotazu na dokončené páry (IN seznam)
MINUTE_GAP_BATCH = 500


class LevelsRegimeReader:
    """GEX režim v čase eventu z levels parquet enginu (#402).

    Partice `derived/{sym}/{expiry}/levels/{date}.parquet` — pro ES/NQ je
    aktivní expirace dne zpravidla den sám (denní expirace); fallback vezme
    kteroukoli expiraci, která partici toho dne má. Mimo 90denní retenci
    (ADR-0022) levels nejsou → None a podmíněná větev prostě roste od nasazení.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._cache: dict[tuple[str, dt.date], list[tuple[dt.datetime, float | None, float]]] = {}

    def _day_rows(self, symbol: str, day: dt.date) -> list[tuple[dt.datetime, float | None, float]]:
        key = (symbol, day)
        if key in self._cache:
            return self._cache[key]
        rows: list[tuple[dt.datetime, float | None, float]] = []
        base = self._data_dir / "derived" / symbol
        candidates = []
        preferred = base / day.strftime("%Y%m%d") / "levels" / f"{day.isoformat()}.parquet"
        if preferred.exists():
            candidates.append(preferred)
        else:
            candidates.extend(sorted(base.glob(f"*/levels/{day.isoformat()}.parquet")))
        if candidates:
            try:
                import pyarrow.parquet as pq

                table = pq.read_table(candidates[0], columns=["ts_min", "flip", "total_gex"])
                for record in table.to_pylist():
                    rows.append(
                        (
                            record["ts_min"],
                            float(record["flip"]) if record["flip"] is not None else None,
                            float(record["total_gex"] or 0.0),
                        )
                    )
                rows.sort(key=lambda item: item[0])
            except Exception:
                logger.exception("Levels partice %s nečitelná — režim bez dat", candidates[0])
        self._cache[key] = rows
        return rows

    def regime_at(self, symbol: str, ts_event: dt.datetime, spot: float | None) -> str | None:
        if spot is None:
            return None
        rows = self._day_rows(symbol, ts_event.date())
        last: tuple[dt.datetime, float | None, float] | None = None
        for row in rows:
            if row[0] <= ts_event:
                last = row
            else:
                break
        if last is None:
            return None
        return gex_regime(spot, last[1], last[2])


@dataclass(frozen=True)
class MinuteRow:
    """Minutová fáze jednoho symbolu: sloupce širokého řádku a počet oken."""

    symbol: str
    values: dict[str, object]
    windows: int


@dataclass(frozen=True)
class MinuteMeasurement:
    """Minutová fáze eventu: řádky změřených symbolů a jejich příznaky deferred (#339)."""

    rows: list[MinuteRow]
    closed_flags: list[bool]


@dataclass(frozen=True)
class MinuteGap:
    """Event s aspoň jedním řádkem reakcí, kterému chybí minutová fáze symbolu (#1494)."""

    event_id: int
    ts_event: dt.datetime
    category: str | None
    missing: frozenset[str]


@dataclass(frozen=True)
class MinuteFill:
    """Výsledek doměření: které chybějící symboly šly změřit a kolik oken (zapsaných)."""

    gap: MinuteGap
    filled: frozenset[str]
    windows: int


def minute_gaps(
    engine: Engine,
    *,
    symbols: Sequence[str],
    ready_before: dt.datetime,
    since: dt.datetime | None = None,
    event_ids: Collection[int] | None = None,
    limit: int | None = None,
    batch: int = MINUTE_GAP_BATCH,
) -> Iterator[MinuteGap]:
    """Eventy s aspoň jedním řádkem reakcí a neúplnou minutovou fází, podle času.

    Event bez řádku vůbec patří běžné frontě (`_pending_events`); tombstone
    `daily_uncomputable` (#655) řádek nemá, takže sem nepatří. Jen eventy
    s uzavřeným nejdelším minutovým oknem (`ready_before`).
    """
    incomplete = (
        select(news_reactions.c.event_id)
        .where(news_reactions.c.symbol.in_(symbols))
        .group_by(news_reactions.c.event_id)
        .having(func.count(news_reactions.c.computed_at_min) < len(symbols))
        .subquery()
    )
    stmt = (
        select(news_events.c.id, news_events.c.ts_event, news_events.c.category)
        .join(incomplete, incomplete.c.event_id == news_events.c.id)
        .where(news_events.c.ts_event <= ready_before)
        .order_by(news_events.c.ts_event, news_events.c.id)
    )
    if since is not None:
        stmt = stmt.where(news_events.c.ts_event >= since)
    if event_ids is not None:
        stmt = stmt.where(news_events.c.id.in_(list(event_ids)))
    if limit is not None:
        stmt = stmt.limit(limit)
    with engine.connect() as conn:
        events = conn.execute(stmt).fetchall()
    for start in range(0, len(events), batch):
        chunk = events[start : start + batch]
        ids = [int(row.id) for row in chunk]
        with engine.connect() as conn:
            complete = conn.execute(
                select(news_reactions.c.event_id, news_reactions.c.symbol).where(
                    news_reactions.c.event_id.in_(ids),
                    news_reactions.c.computed_at_min.is_not(None),
                )
            ).fetchall()
        done: dict[int, set[str]] = {}
        for row in complete:
            done.setdefault(int(row.event_id), set()).add(row.symbol)
        for row in chunk:
            missing = frozenset(set(symbols) - done.get(int(row.id), set()))
            if missing:
                yield MinuteGap(int(row.id), _as_utc(row.ts_event), row.category, missing)


class ReactionJob:
    """Dopočítá chybějící reakce pro symboly, které měříme (SPEC 6.5: ES i NQ)."""

    def __init__(
        self,
        engine: Engine,
        bars: BarsRepository,
        *,
        symbols: Sequence[str] = ("ES", "NQ"),
        windows: Sequence[int] = DEFAULT_WINDOWS,
        daily_window_days: Sequence[int] = DAILY_WINDOW_DAYS,
    ) -> None:
        self._engine = engine
        self._bars = bars
        self._symbols = list(symbols)
        self._windows = list(windows)
        # Denní okna v obchodních dnech (#564); () denní fázi vypíná
        self._daily_window_days = list(daily_window_days)
        # Široký řádek (#998) má sloupce jen pro známá okna — jiná konfigurace
        # by neměla kam psát; lepší spadnout při startu než při prvním zápisu
        unknown = set(self._windows) - set(REACTION_WINDOWS)
        unknown |= {d * MINUTES_PER_TRADING_DAY for d in self._daily_window_days} - set(
            REACTION_DAILY_WINDOWS
        )
        if unknown:
            raise ValueError(f"Reakční okna bez sloupce v news_reactions: {sorted(unknown)}")
        # Cache denních sérií per symbol: (počet partic, série) — přestaví se
        # jen když přibude nová partice, jinak by job četl stovky souborů denně
        self._daily_series_cache: dict[str, tuple[int, list[SessionDaily]]] = {}
        # GEX režim reakce (#402) — levels čteme ze stejného data_dir jako bary
        self._regime_reader = LevelsRegimeReader(bars.data_dir)
        # Poslední doměření po výpadku symbolu (A, #1494); None = ještě nebylo
        self._minute_retry_at: dt.datetime | None = None

    def _pending_events(
        self, now: dt.datetime, limit: int
    ) -> list[tuple[int, dt.datetime, str | None]]:
        """Eventy s uzavřeným nejdelším oknem a bez reakcí.

        „Bez reakcí" = bez JAKÉHOKOLI řádku, ne jen bez minutové fáze: event,
        kterému minutová fáze nenašla bary a denní fáze ho pak změřila
        (~27 k historických dvojic před pokrytím minutových barů), by se jinak
        vybíral znovu každý cyklus — stejná past jako #655.
        """
        ready_before = now - dt.timedelta(minutes=max(self._windows))
        measured = exists().where(news_reactions.c.event_id == news_events.c.id)
        stmt = (
            select(news_events.c.id, news_events.c.ts_event, news_events.c.category)
            .where(
                news_events.c.ts_event <= ready_before,
                not_(measured),
                # #655: trvale nespočitatelné eventy (před pokrytím barů) se
                # nevybírají — bez filtru se týchž ~4 800 mrtvých eventů
                # přescanovávalo každý cyklus donekonečna
                news_events.c.daily_uncomputable.is_not(True),
            )
            .order_by(news_events.c.ts_event.desc())
            .limit(limit)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [(int(row.id), _as_utc(row.ts_event), row.category) for row in rows]

    def _contaminating(self, around: dt.datetime, category: str | None) -> list[dt.datetime]:
        """Časy jiných významných eventů jiné kategorie, které můžou spadnout do oken."""
        span = dt.timedelta(minutes=max(self._windows) + 1)
        stmt = select(news_events.c.ts_event, news_events.c.category).where(
            and_(
                news_events.c.ts_event > around,
                news_events.c.ts_event <= around + span,
                news_events.c.importance >= CONTAMINATION_MIN_IMPORTANCE,
            )
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [_as_utc(row.ts_event) for row in rows if contaminates(category, row.category)]

    def _daily_sessions(self, symbol: str) -> list[SessionDaily]:
        """Denní agregáty Globex seancí z bars partic (#564), s cache per běh.

        Bary se přiřazují seanci přes `trading_session_date` (večer partice D
        patří seanci D+1, ADR-0023) a ořezávají na settle — close je settle
        close. Přestavuje se jen když přibude partice (1× denně), jinak by
        každý běh četl stovky souborů.
        """
        partition_days = self._bars.sessions(symbol)
        cached = self._daily_series_cache.get(symbol)
        if cached is not None and cached[0] == len(partition_days):
            return cached[1]
        highs: dict[dt.date, float] = {}
        lows: dict[dt.date, float] = {}
        last: dict[dt.date, tuple[dt.datetime, float]] = {}
        settle_by_day: dict[dt.date, dt.datetime] = {}
        for day in partition_days:
            for bar in self._bars.load_day(symbol, day):
                session = trading_session_date(bar.ts)
                boundary = settle_by_day.setdefault(session, settle_ts(session))
                if bar.ts > boundary:
                    continue  # po settle (15:00–16:00 CT) — mimo denní agregát
                highs[session] = max(highs.get(session, bar.high), bar.high)
                lows[session] = min(lows.get(session, bar.low), bar.low)
                previous = last.get(session)
                if previous is None or bar.ts >= previous[0]:
                    last[session] = (bar.ts, bar.close)
        series = [
            SessionDaily(
                day=session,
                settle_ts=settle_by_day[session],
                close=last[session][1],
                high=highs[session],
                low=lows[session],
            )
            for session in sorted(last)
        ]
        self._daily_series_cache[symbol] = (len(partition_days), series)
        return series

    def _pending_daily_events(self, now: dt.datetime, limit: int) -> list[tuple[int, dt.datetime]]:
        """Eventy bez denních oken, u kterých už šlo uzavřít i nejdelší okno."""
        ready_before = now - dt.timedelta(days=DAILY_READY_CALENDAR_DAYS)
        measured = exists().where(
            news_reactions.c.event_id == news_events.c.id,
            news_reactions.c.computed_at_daily.is_not(None),
        )
        stmt = (
            select(news_events.c.id, news_events.c.ts_event, news_events.c.category)
            .where(
                news_events.c.ts_event <= ready_before,
                not_(measured),
                # #655: trvale nespočitatelné eventy (před pokrytím barů) se
                # nevybírají — bez filtru se týchž ~4 800 mrtvých eventů
                # přescanovávalo každý cyklus donekonečna
                news_events.c.daily_uncomputable.is_not(True),
            )
            .order_by(news_events.c.ts_event.desc())
            .limit(limit)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [(int(row.id), _as_utc(row.ts_event)) for row in rows]

    def _run_daily(self, now: dt.datetime, *, limit: int) -> int:
        """Denní okna (#564): 1d/2d/5d/10d obchodních dní, zápis až kompletní.

        Všechna denní okna eventu se zapisují najednou (pending dotaz stojí na
        „event nemá ŽÁDNÉ denní okno"); event s ještě neuzavřeným oknem se
        přeskočí a vezme příští běh. Symbol bez barů kolem eventu (starší než
        archiv) nepřispívá — stejná konvence jako minutová fáze.
        """
        if not self._daily_window_days:
            return 0
        pending = self._pending_daily_events(now, limit)
        if not pending:
            return 0
        series = {symbol: self._daily_sessions(symbol) for symbol in self._symbols}
        written = 0
        measured_events = 0
        measured_ids: list[int] = []
        uncomputable: list[int] = []
        for event_id, ts_event in pending:
            rows: list[tuple[str, dict[str, object], int]] = []
            wait_for_close = False
            for symbol in self._symbols:
                bars = self._bars.load_range(
                    symbol,
                    ts_event - dt.timedelta(days=CLOSURE_LOOKBACK_DAYS),
                    ts_event + dt.timedelta(days=CLOSURE_LOOKAHEAD_DAYS),
                )
                reactions = compute_daily_reactions(
                    ts_event,
                    bars,
                    series[symbol],
                    window_days=self._daily_window_days,
                )
                if not reactions:
                    continue  # symbol bez základní ceny — trvalé, nic nepíšeme
                if len(reactions) < len(self._daily_window_days):
                    wait_for_close = True
                    break
                spot_at_event: float | None = None
                for bar in bars:
                    if bar.ts <= ts_event:
                        spot_at_event = float(bar.close)
                    else:
                        break
                regime = self._regime_reader.regime_at(symbol, ts_event, spot_at_event)
                rows.append((symbol, _phase_values(reactions, regime, now), len(reactions)))
            if wait_for_close:
                continue  # dočasné — nejdelší okno se uzavře v příštích dnech
            if not rows:
                # Žádný symbol nemá základní cenu → bary pro tohle období
                # neexistují a existovat nebudou (archiv sahá 2 roky zpět,
                # IBKR limit). Tombstone (#655): event se přestane vybírat.
                uncomputable.append(event_id)
                continue
            with self._engine.begin() as conn:
                for symbol, values, count in rows:
                    # Denní fáze doplňuje řádek minutové fáze (UPDATE); řádek
                    # ještě nemusí existovat — historický event před pokrytím
                    # minutových barů dostává jen denní okna
                    _write_phase(conn, event_id, symbol, values)
                    written += count
            measured_events += 1
            measured_ids.append(event_id)
        if uncomputable:
            with self._engine.begin() as conn:
                conn.execute(
                    update(news_events)
                    .where(news_events.c.id.in_(uncomputable))
                    .values(daily_uncomputable=True)
                )
            logger.info(
                "Denní okna (#655): %d eventů před pokrytím barů označeno jako "
                "trvale nespočitatelné",
                len(uncomputable),
            )
        if written:
            logger.info(
                "Denní okna (#564): zapsáno %d oken pro %d eventů", written, measured_events
            )
        if measured_ids:
            # D (#1494): event, kterému denní fáze právě zapsala řádek, by
            # z minutové fronty („bez jakéhokoli řádku“) vypadl — doměří se teď
            gaps = minute_gaps(
                self._engine,
                symbols=self._symbols,
                ready_before=now - dt.timedelta(minutes=max(self._windows)),
                event_ids=measured_ids,
            )
            completed = self._complete_logged(gaps, now, "denní fáze")
            written += completed
        return written

    def run(self, now: dt.datetime, *, limit: int = 200) -> int:
        """Dopočítá reakce; vrací počet zapsaných oken (minutová + denní fáze)."""
        pending = self._pending_events(now, limit)
        written = 0
        if pending:
            baselines = {symbol: self.baseline_for(symbol, now.date()) for symbol in self._symbols}
            for event_id, ts_event, category in pending:
                measurement = self.measure_minute(ts_event, category, baselines, now)
                if measurement.rows:
                    written += self.write_minute(event_id, measurement)
            if written:
                logger.info("Reakce: zapsáno %d oken pro %d eventů", written, len(pending))
        # Až po nových eventech, aby je doměřování nezdrželo
        written += self._retry_minute(now)
        return written + self._run_daily(now, limit=limit)

    def _retry_minute(self, now: dt.datetime) -> int:
        """A (#1494): jednou za hodinu doměří minutovou fázi eventům posledních 3 dní.

        Symbol, který v T+60 neměl bary (výpadek, restart), dostal jen druhý
        symbol; event pak z minutové fronty vypadl. Bary později doplní
        `ibkr_hist` — tady se reakce dopočítá do hodiny. Pár bez barů se zkouší
        jen jednou za hodinu (past #655), po 3 dnech ho převezme denní fáze (D).
        """
        if self._minute_retry_at is not None and now - self._minute_retry_at < MINUTE_RETRY_EVERY:
            return 0
        self._minute_retry_at = now
        gaps = minute_gaps(
            self._engine,
            symbols=self._symbols,
            ready_before=now - dt.timedelta(minutes=max(self._windows)),
            since=now - MINUTE_RETRY_LOOKBACK,
            limit=MINUTE_RETRY_LIMIT,
        )
        return self._complete_logged(gaps, now, "výpadek symbolu")

    def _complete_logged(self, gaps: Iterable[MinuteGap], now: dt.datetime, reason: str) -> int:
        fills = [fill for fill in self.complete_minute(gaps, now) if fill.filled]
        windows = sum(fill.windows for fill in fills)
        if fills:
            logger.info(
                "Minutová fáze doměřena (#1494, %s): %d oken pro %d eventů",
                reason,
                windows,
                len(fills),
            )
        return windows

    def complete_minute(
        self, gaps: Iterable[MinuteGap], now: dt.datetime, *, write: bool = True
    ) -> Iterator[MinuteFill]:
        """Doměří minutovou fázi symbolům, které ji nemají (#1494); nic nepřepisuje.

        Měří všechny symboly (`market_closed` se opraví ze všech, #339), zapíše
        jen chybějící. Baseline objemu (`vol_z`) k obchodnímu dni eventu
        (point-in-time); eventy jdou podle času, takže se drží jen poslední den
        (všechny dny by držely ~270 MB). `write=False` = dry-run skriptu.
        Sdílí ho denní fáze (D), hodinové doměření (A) a
        `scripts/backfill_minute_reactions.py`.
        """
        baseline_day: dt.date | None = None
        baselines: dict[str, dict[dt.time, VolumeBaseline] | None] = {}
        for gap in gaps:
            day = trading_session_date(gap.ts_event)
            if day != baseline_day:
                baseline_day = day
                baselines = {symbol: self.baseline_for(symbol, day) for symbol in self._symbols}
            measurement = self.measure_minute(gap.ts_event, gap.category, baselines, now)
            filled = frozenset(row.symbol for row in measurement.rows) & gap.missing
            if not filled:
                yield MinuteFill(gap, filled, 0)
                continue
            if write:
                windows = self.write_minute(gap.event_id, measurement, only_symbols=gap.missing)
            else:
                windows = sum(row.windows for row in measurement.rows if row.symbol in filled)
            yield MinuteFill(gap, filled, windows)

    def measure_minute(
        self,
        ts_event: dt.datetime,
        category: str | None,
        baselines: Mapping[str, Mapping[dt.time, VolumeBaseline] | None],
        now: dt.datetime,
    ) -> MinuteMeasurement:
        """Minutová fáze jednoho eventu pro všechny symboly; nic nezapisuje.

        Sdílí ji běžný průchod (`run`) i doplnění minutové fáze po pozdějším
        backfillu barů (`scripts/backfill_minute_reactions.py`, #1494).
        """
        others = self._contaminating(ts_event, category)
        rows: list[MinuteRow] = []
        # Zavřený trh podle skutečně obchodovaných barů, per symbol (#339)
        closed_flags: list[bool] = []
        for symbol in self._symbols:
            window_end = ts_event + dt.timedelta(minutes=max(self._windows) + 1)
            # Dozadu přes celé zavření, dopředu k prvnímu obchodovanému baru
            # — jinak deferred okno nemá základní cenu ani cíl (#339)
            bars = self._bars.load_range(
                symbol,
                ts_event - dt.timedelta(days=CLOSURE_LOOKBACK_DAYS),
                window_end + dt.timedelta(days=CLOSURE_LOOKAHEAD_DAYS),
            )
            reactions = compute_reactions(
                ts_event,
                bars,
                windows=self._windows,
                other_event_ts=others,
                baseline=baselines[symbol],
            )
            if not reactions:
                continue
            # `deferred` je na event stejné ve všech oknech
            closed_flags.append(reactions[0].deferred)
            # GEX režim v čase eventu (#402): spot = poslední bar ≤ ts_event
            spot_at_event: float | None = None
            for bar in bars:
                if bar.ts <= ts_event:
                    spot_at_event = float(bar.close)
                else:
                    break
            regime = self._regime_reader.regime_at(symbol, ts_event, spot_at_event)
            rows.append(MinuteRow(symbol, _phase_values(reactions, regime, now), len(reactions)))
        return MinuteMeasurement(rows, closed_flags)

    def write_minute(
        self,
        event_id: int,
        measurement: MinuteMeasurement,
        *,
        only_symbols: Collection[str] | None = None,
    ) -> int:
        """Zapíše minutovou fázi a opraví `market_closed`; vrací počet oken.

        `only_symbols` omezí zápis na symboly bez minutové fáze (doplnění
        #1494 nepřepisuje existující měření); `market_closed` se počítá ze
        všech změřených symbolů.
        """
        written = 0
        with self._engine.begin() as conn:
            for row in measurement.rows:
                if only_symbols is None or row.symbol in only_symbols:
                    _write_phase(conn, event_id, row.symbol, row.values)
                    written += row.windows
            self._correct_market_closed(conn, event_id, measurement.closed_flags)
        return written

    @staticmethod
    def _correct_market_closed(conn: Connection, event_id: int, closed_flags: list[bool]) -> None:
        """Opraví `market_closed` podle skutečně obchodovaných barů (#339).

        Při zápisu zprávy se hodnota odhaduje z rozvrhu Globexu, který nezná
        svátky ani neplánované halty — na Vánoce by tvrdil „otevřeno". Bary
        jsou proti tomu měření, ne kalendář: nezastarají a pokryjí i zkrácené
        seance. Proto se hodnota tady přepíše na naměřenou.

        Zavřeno jen tehdy, když **žádný** ze sledovaných symbolů neobchodoval.
        Díra v datech jednoho symbolu není zavřený trh a nesmí ho předstírat.
        """
        if not closed_flags:
            return
        conn.execute(
            update(news_events)
            .where(news_events.c.id == event_id)
            .values(market_closed=all(closed_flags))
        )

    def baseline_for(self, symbol: str, today: dt.date) -> dict[dt.time, VolumeBaseline] | None:
        sessions = self._bars.recent_sessions(symbol, today, MIN_BASELINE_SESSIONS)
        if len(sessions) < MIN_BASELINE_SESSIONS:
            # Archiv se teprve plní (#275 spuštěn 28. 7.) — do té doby vol_z None
            logger.debug(
                "Volume baseline %s zatím z %d seancí (potřeba %d) — vol_z bude None",
                symbol,
                len(sessions),
                MIN_BASELINE_SESSIONS,
            )
            return None
        baseline = build_volume_baseline(sessions)
        # Pokrytí musí být vidět (#1001): do té doby volume_z_score mlčky
        # vracel None a nikdo nevěděl, že baseline nikdy nevyhověla
        covered = sum(1 for stats in baseline.values() if stats.sessions >= MIN_MINUTE_SAMPLES)
        logger.info(
            "Volume baseline %s: %d seancí (%s – %s), %d/%d minut dne s ≥ %d vzorky",
            symbol,
            len(sessions),
            trading_session_date(sessions[0][0].ts).isoformat() if sessions[0] else "?",
            trading_session_date(sessions[-1][0].ts).isoformat() if sessions[-1] else "?",
            covered,
            len(baseline),
            MIN_MINUTE_SAMPLES,
        )
        return baseline


def _phase_values(
    reactions: Sequence[Reaction], regime: str | None, now: dt.datetime
) -> dict[str, object]:
    """Sloupce jedné fáze širokého řádku (#998) z naměřených oken symbolu."""
    return reaction_row_values(
        [
            ReactionWindow(
                window_min=reaction.window_min,
                ret_bp=reaction.ret_bp,
                range_bp=reaction.range_bp,
                vol_z=reaction.vol_z,
                contaminated=reaction.contaminated,
                deferred=reaction.deferred,
                gex_regime=regime,
                computed_at=now,
                closure_open=reaction.closure_open,
            )
            for reaction in reactions
        ]
    )


def _write_phase(conn: Connection, event_id: int, symbol: str, values: dict[str, object]) -> None:
    """Zapíše sloupce fáze do řádku (event, symbol): UPDATE existujícího, jinak INSERT.

    Dialektově neutrální upsert (SQLite v testech, PG v provozu) — hledání
    po PK je levné a obě fáze tak sdílejí jednu cestu zápisu.
    """
    key = and_(news_reactions.c.event_id == event_id, news_reactions.c.symbol == symbol)
    present = conn.execute(select(news_reactions.c.event_id).where(key)).first()
    if present is None:
        conn.execute(insert(news_reactions).values(event_id=event_id, symbol=symbol, **values))
    else:
        conn.execute(update(news_reactions).where(key).values(**values))


def _as_utc(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
