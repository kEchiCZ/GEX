"""Plánovač a zápis reakcí (#276, SPEC 3.1 „reaction scheduler").

Reakce se počítají až po uzavření nejdelšího okna — dřív by měření bylo
useknuté. Job proto bere eventy starší než `max(windows)` minut, které ještě
reakce nemají, a dopočítá je. Běží periodicky i jako noční sanity průchod,
takže výpadek nic neztratí (archiv barů je věčný, S4).
"""

import datetime as dt
import logging
from collections.abc import Sequence

from sqlalchemy import and_, insert, not_, select
from sqlalchemy.engine import Engine

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
            for symbol in self._symbols:
                window_end = ts_event + dt.timedelta(minutes=max(self._windows) + 1)
                # Načítáme i dopředu, aby deferred (víkend) našel první bar
                bars = self._bars.load_range(
                    symbol, ts_event - dt.timedelta(minutes=30), window_end + dt.timedelta(days=4)
                )
                for reaction in compute_reactions(
                    ts_event,
                    bars,
                    windows=self._windows,
                    other_event_ts=others,
                    baseline=baselines[symbol],
                ):
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
                            "computed_at": now,
                        }
                    )
            if rows:
                with self._engine.begin() as conn:
                    conn.execute(insert(news_reactions), rows)
                written += len(rows)
        if written:
            logger.info("Reakce: zapsáno %d oken pro %d eventů", written, len(pending))
        return written

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
