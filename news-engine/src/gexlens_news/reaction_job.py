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

from sqlalchemy import and_, insert, not_, select, update
from sqlalchemy.engine import Connection, Engine

from gexlens_engine.compute.setups import gex_regime
from gexlens_engine.storage.sentiment import news_events, news_reactions
from gexlens_news.bars import BarsRepository
from gexlens_news.reactions import (
    DEFAULT_WINDOWS,
    MIN_BASELINE_SESSIONS,
    VolumeBaseline,
    build_volume_baseline,
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


class LevelsRegimeReader:
    """GEX režim v čase eventu z levels parquet enginu (#402).

    Partice `derived/{sym}/{expiry}/levels/{date}.parquet` — pro ES/NQ je
    aktivní expirace dne zpravidla den sám (denní expirace); fallback vezme
    kteroukoli expiraci, která partici toho dne má. Mimo 14denní retenci
    levels nejsou → None a podmíněná větev prostě roste od nasazení.
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
    ) -> None:
        self._engine = engine
        self._bars = bars
        self._symbols = list(symbols)
        self._windows = list(windows)
        # GEX režim reakce (#402) — levels čteme ze stejného data_dir jako bary
        self._regime_reader = LevelsRegimeReader(bars.data_dir)

    def _pending_events(self, now: dt.datetime, limit: int) -> list[tuple[int, dt.datetime]]:
        """Eventy s uzavřeným nejdelším oknem a bez reakcí."""
        ready_before = now - dt.timedelta(minutes=max(self._windows))
        measured = select(news_reactions.c.event_id).distinct()
        stmt = (
            select(news_events.c.id, news_events.c.ts_event)
            .where(
                news_events.c.ts_event <= ready_before,
                not_(news_events.c.id.in_(measured)),
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

    def run(self, now: dt.datetime, *, limit: int = 200) -> int:
        """Dopočítá reakce; vrací počet zapsaných řádků."""
        pending = self._pending_events(now, limit)
        if not pending:
            return 0
        written = 0
        baselines = {symbol: self._baseline_for(symbol, now.date()) for symbol in self._symbols}
        for event_id, ts_event in pending:
            others = self._contaminating(ts_event)
            rows: list[dict[str, object]] = []
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
                for reaction in reactions:
                    rows.append(
                        {
                            "event_id": event_id,
                            "symbol": symbol,
                            "window_min": reaction.window_min,
                            "ret_bp": reaction.ret_bp,
                            "range_bp": reaction.range_bp,
                            "vol_z": reaction.vol_z,
                            "contaminated": reaction.contaminated,
                            "deferred": reaction.deferred,
                            "gex_regime": regime,
                            "computed_at": now,
                        }
                    )
            if rows:
                with self._engine.begin() as conn:
                    conn.execute(insert(news_reactions), rows)
                    self._correct_market_closed(conn, event_id, closed_flags)
                written += len(rows)
        if written:
            logger.info("Reakce: zapsáno %d oken pro %d eventů", written, len(pending))
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
        return build_volume_baseline(sessions)


def _as_utc(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
