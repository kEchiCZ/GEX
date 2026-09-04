"""Plánovač a zápis reakcí (#276, SPEC 3.1 „reaction scheduler").

Reakce se počítají až po uzavření nejdelšího okna — dřív by měření bylo
useknuté. Job proto bere eventy starší než `max(windows)` minut, které ještě
reakce nemají, a dopočítá je. Běží periodicky i jako noční sanity průchod,
takže výpadek nic neztratí (archiv barů je věčný, S4).
"""

import datetime as dt
import logging
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import and_, exists, insert, not_, select, update
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
)

logger = logging.getLogger(__name__)

# Importance, od které event kontaminuje cizí okno (SPEC 5.1)
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

    def _pending_events(self, now: dt.datetime, limit: int) -> list[tuple[int, dt.datetime]]:
        """Eventy s uzavřeným nejdelším oknem a bez reakcí.

        „Bez reakcí" = bez JAKÉHOKOLI řádku, ne jen bez minutové fáze: event,
        kterému minutová fáze nenašla bary a denní fáze ho pak změřila
        (~27 k historických dvojic před pokrytím minutových barů), by se jinak
        vybíral znovu každý cyklus — stejná past jako #655.
        """
        ready_before = now - dt.timedelta(minutes=max(self._windows))
        measured = exists().where(news_reactions.c.event_id == news_events.c.id)
        stmt = (
            select(news_events.c.id, news_events.c.ts_event)
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

    def _contaminating(self, around: dt.datetime) -> list[dt.datetime]:
        """Časy jiných high-impact eventů, které můžou spadnout do oken."""
        span = dt.timedelta(minutes=max(self._windows) + 1)
        stmt = select(news_events.c.ts_event).where(
            and_(
                news_events.c.ts_event > around,
                news_events.c.ts_event <= around + span,
                news_events.c.importance >= CONTAMINATION_MIN_IMPORTANCE,
            )
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [_as_utc(row.ts_event) for row in rows]

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
            select(news_events.c.id, news_events.c.ts_event)
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
        return written

    def run(self, now: dt.datetime, *, limit: int = 200) -> int:
        """Dopočítá reakce; vrací počet zapsaných řádků (minutová + denní okna)."""
        pending = self._pending_events(now, limit)
        if not pending:
            return self._run_daily(now, limit=limit)
        written = 0
        baselines = {symbol: self._baseline_for(symbol, now.date()) for symbol in self._symbols}
        for event_id, ts_event in pending:
            others = self._contaminating(ts_event)
            rows: list[tuple[str, dict[str, object], int]] = []
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
                if reactions:
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
                if reactions:
                    rows.append((symbol, _phase_values(reactions, regime, now), len(reactions)))
            if rows:
                with self._engine.begin() as conn:
                    for symbol, values, count in rows:
                        _write_phase(conn, event_id, symbol, values)
                        written += count
                    self._correct_market_closed(conn, event_id, closed_flags)
        if written:
            logger.info("Reakce: zapsáno %d oken pro %d eventů", written, len(pending))
        return written + self._run_daily(now, limit=limit)

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

    def _baseline_for(self, symbol: str, today: dt.date) -> dict[dt.time, VolumeBaseline] | None:
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
