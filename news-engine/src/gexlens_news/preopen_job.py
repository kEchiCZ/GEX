"""Předobchodní upozornění na zprávy za zavřený trh (#1291 Q2, ADR-0043) — IO adaptér.

Rozhodování (kdy, co, text) žije v čistých funkcích `preopen.py`; tady je jen
čtení zpráv z PG, posledního baru a úrovní z parquet archivu a stav etap.

* Mimo etapy (4 h a 15 min před otevřením Globexu po víkendu) job nic nečte
  — celý běh je pár výpočtů nad rozvrhem.
* Stav etap a ohlášených zpráv žije v PG tabulce `settings` (klíč
  `news_preopen_state`, vzor `drift_state`) a zapisuje se PŘED vrácením
  payloadů: restart news-enginu v neděli večer souhrn nezopakuje a aktualizace
  pozná, co hlavní souhrn už obsáhl. Pád mezi zápisem a publikací = ztráta
  jednoho upozornění, nikdy duplicita. Watermark „start procesu“ (jako
  u `AnomalyJob`) by tu nestačil: po restartu ve 21:00 by aktualizace nevěděla,
  které zprávy hlavní souhrn vyjmenoval.
* ES a NQ zvlášť: vlastní poslední bar (začátek zavření, close) a úrovně.
"""

import datetime as dt
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow.parquet as pq
from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine

from gexlens_engine.compute.coach_setups import LOCAL_TZ
from gexlens_engine.compute.settle import trading_session_date
from gexlens_engine.storage.meta import settings_table
from gexlens_news.anomaly_job import select_events
from gexlens_news.bars import BarsRepository
from gexlens_news.clusters import ClusterEvent
from gexlens_news.predictions import DEFAULT_PRIMARY_WINDOW_MIN
from gexlens_news.preopen import (
    CLOSURE_LOOKBACK,
    LEVEL_FIELDS,
    PreopenState,
    SessionLevels,
    announced_ids,
    build_preopen,
    due_stages,
    follows_long_closure,
    last_levels,
    scheduled_close,
    upcoming_open,
)

logger = logging.getLogger(__name__)

#: Klíč v PG `settings` — píše ho jen news-engine (API ho zapsat nedovolí)
PREOPEN_SETTINGS_KEY = "news_preopen_state"
_MINUTE = dt.timedelta(minutes=1)


class PreopenJob:
    """Zprávy za víkend → `news_preopen` pro ES a NQ před otevřením Globexu."""

    def __init__(
        self,
        engine: Engine,
        bars: BarsRepository,
        *,
        symbols: Sequence[str] = ("ES", "NQ"),
        window_min: int = DEFAULT_PRIMARY_WINDOW_MIN,
        tz: ZoneInfo = LOCAL_TZ,
    ) -> None:
        self._engine = engine
        self._bars = bars
        self._derived = bars.data_dir / "derived"
        self._symbols = list(symbols)
        # Zprávy z posledních minut před zavřením už reakce nezměří (okno
        # přesahuje zavření) — patří proto do souhrnu
        self._window = dt.timedelta(minutes=window_min)
        self._tz = tz

    # ── IO ────────────────────────────────────────────────────────
    def load_events(
        self, start: dt.datetime, end: dt.datetime, now: dt.datetime
    ) -> list[ClusterEvent]:
        return select_events(self._engine, start, end, now)

    def load_state(self, opening: dt.datetime) -> PreopenState:
        stmt = select(settings_table.c.value).where(settings_table.c.key == PREOPEN_SETTINGS_KEY)
        with self._engine.connect() as conn:
            row = conn.execute(stmt).first()
        return PreopenState.from_json(row.value if row is not None else None, opening)

    def store_state(self, state: PreopenState) -> None:
        payload = state.to_json()
        with self._engine.begin() as conn:
            updated = conn.execute(
                update(settings_table)
                .where(settings_table.c.key == PREOPEN_SETTINGS_KEY)
                .values(value=payload)
            )
            if updated.rowcount == 0:
                conn.execute(insert(settings_table).values(key=PREOPEN_SETTINGS_KEY, value=payload))

    def load_levels(
        self, symbol: str, day: dt.date, until: dt.datetime, first_expiry: dt.date
    ) -> SessionLevels | None:
        """Úrovně nejbližší expirace ≥ `first_expiry` z partice dne `day`.

        Po víkendu platí expirace příští seance (pondělní 0DTE), ne páteční —
        ta po settle zanikla. Engine počítá i příští expiraci, takže partice
        `derived/{sym}/{expirace}/levels/{den}.parquet` existuje už v pátek.
        """
        candidates: list[tuple[dt.date, Path]] = []
        for path in (self._derived / symbol).glob(f"*/levels/{day.isoformat()}.parquet"):
            try:
                expiry = dt.datetime.strptime(path.parent.parent.name, "%Y%m%d").date()
            except ValueError:
                continue
            if expiry >= first_expiry:
                candidates.append((expiry, path))
        for expiry, path in sorted(candidates):
            try:
                rows = pq.read_table(path, columns=["ts_min", *LEVEL_FIELDS]).to_pylist()
            except Exception:
                logger.exception("Levels partice %s nečitelná — zkusím další expiraci", path)
                continue
            found = last_levels(rows, until, expiry)
            if found is not None:
                return found
        return None

    def closure(
        self, symbol: str, opening: dt.datetime, now: dt.datetime
    ) -> tuple[dt.datetime, float | None]:
        """Začátek zavření před `opening` a poslední close — z posledního baru.

        Svátek, který zkrátí páteční seanci, se projeví tady (dřívější bar);
        bez barů (nový archiv, výpadek) zavření podle rozvrhu bez close.
        """
        bars = self._bars.load_range(symbol, opening - CLOSURE_LOOKBACK, now)
        if bars:
            return bars[-1].ts + _MINUTE, bars[-1].close
        logger.warning(
            "Předobchodní upozornění %s: bez barů za %d dní — zavření podle rozvrhu",
            symbol,
            CLOSURE_LOOKBACK.days,
        )
        return scheduled_close(opening), None

    def window_start(self, closed_at: dt.datetime) -> dt.datetime:
        """Zprávy z posledních minut před zavřením reakce nezměří — patří do souhrnu."""
        return closed_at - self._window

    # ── běh ───────────────────────────────────────────────────────
    def run(self, now: dt.datetime) -> list[dict[str, Any]]:
        """Payloady `news_preopen` za tento běh (většinou žádné)."""
        opening = upcoming_open(now)
        if not follows_long_closure(opening) or not due_stages(now, opening, ()):
            return []
        state = self.load_state(opening)
        stages = due_stages(now, opening, state.done)
        if not stages:
            return []
        payloads: list[dict[str, Any]] = []
        for symbol in self._symbols:
            closed_at, last_close = self.closure(symbol, opening, now)
            since = self.window_start(closed_at)
            events = self.load_events(since, now, now)
            levels = self.load_levels(
                symbol,
                (closed_at - _MINUTE).date(),
                closed_at,
                trading_session_date(opening),
            )
            announced = state.announced.setdefault(symbol, set())
            for stage in stages:
                payload = build_preopen(
                    stage,
                    symbol,
                    opening,
                    since=since,
                    events=events,
                    announced=announced,
                    levels=levels,
                    last_close=last_close,
                    now=now,
                    tz=self._tz,
                )
                if payload is not None:
                    payloads.append(payload)
                announced.update(announced_ids(events))
        state.done.update(stages)
        # Stav PŘED vrácením payloadů: po restartu se etapa nezopakuje
        self.store_state(state)
        logger.info(
            "Předobchodní upozornění (otevření %s, etapy %s): %d upozornění",
            opening.isoformat(),
            ", ".join(stages),
            len(payloads),
        )
        return payloads
