"""Upozornění na mimořádnou reakci trhu na zprávy (#1291, ADR-0043) — IO adaptér.

Rozhodování žije v čistých funkcích `clusters.py` a `reactions.py`; tady je
jen čtení zpráv z PG a barů z parquet archivu, cache baseline a stav dedupu.
Nahrazuje pravidlo #295 „reakce jedné zprávy nad p90 bucketu", které za
14 dní vyrobilo 596 upozornění, z 94 % na šum s importance 1: kontaminace oken
blokovala skutečné makro releasy a pouštěla poslední titulek před klidem.

* Každý běh znovu postaví shluky ze zpráv za posledních `LOOKBACK` minut
  (bezestavové vůči datům) — pozdě dorazivší zpráva shluk doplní.
* Bary ES a NQ se čtou zvlášť; baseline výchylek se staví jednou za seanci
  (per symbol, z 20 předchozích seancí) a drží v paměti.
* Dedup: starty ohlášených shluků per symbol v paměti (cooldown 15 min).
  Watermark = start procesu: shluk hotový před startem se nehodnotí, po
  restartu tak nevzniknou duplicity (riziko je ztráta, ne duplicita).
* Zprávy za zavřený trh se neměří (chybí bar před zprávou); víkend pokryje
  předobchodní upozornění (`preopen_job.py`), denní pauza se nehlásí.

Job nic nezapisuje; `news_reactions` se v této cestě nepoužívá.
"""

import datetime as dt
import logging
from collections.abc import Sequence
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.engine import Engine

from gexlens_engine.compute.coach_setups import LOCAL_TZ
from gexlens_engine.compute.settle import trading_session_date
from gexlens_engine.storage.sentiment import news_events
from gexlens_news.bars import BarsRepository
from gexlens_news.clusters import (
    CLUSTER_SPAN,
    COOLDOWN,
    LOOKBACK,
    ClusterEvent,
    build_clusters,
    evaluate_clusters,
)
from gexlens_news.predictions import DEFAULT_PRIMARY_WINDOW_MIN
from gexlens_news.reactions import (
    MIN_BASELINE_SESSIONS,
    SIGMA_LOOKBACK_MIN,
    Excursion,
    build_excursion_baseline,
)

logger = logging.getLogger(__name__)

#: Rezerva při čtení barů před nejstarším hodnoceným shlukem
_BARS_MARGIN = dt.timedelta(minutes=2)

Baseline = dict[int, list[Excursion]]


#: Sloupce `news_events`, ze kterých se staví `ClusterEvent` — jediný zdroj
#: i pro replay v `scripts/measure_news_anomaly.py` (vlastní SELECT tam jednou
#: zapomněl `sentiment_dir` a simulace ukázala samé ⚪). Význam scheduled stojí
#: na FF impactu ze surového payloadu (`raw.impact`), ne na `importance` — tu
#: pravidlový klasifikátor přepisuje regexem.
EVENT_COLUMNS = (
    news_events.c.id,
    news_events.c.ts_event,
    news_events.c.kind,
    news_events.c.title,
    news_events.c.importance,
    news_events.c.category,
    news_events.c.sentiment_dir,
    news_events.c.raw["impact"].as_string().label("ff_impact"),
    news_events.c.raw["curated"].as_boolean().label("curated"),
)


def event_from_row(row: Any) -> ClusterEvent:
    """Řádek se sloupci `EVENT_COLUMNS` → `ClusterEvent`."""
    return ClusterEvent(
        id=int(row.id),
        ts_event=as_utc(row.ts_event),
        kind=str(row.kind),
        title=str(row.title),
        importance=int(row.importance) if row.importance is not None else None,
        category=str(row.category) if row.category is not None else None,
        ff_impact=str(row.ff_impact) if row.ff_impact is not None else None,
        curated=bool(row.curated),
        direction=int(row.sentiment_dir) if row.sentiment_dir is not None else None,
    )


def select_events(
    engine: Engine, start: dt.datetime, end: dt.datetime, now: dt.datetime
) -> list[ClusterEvent]:
    """Zprávy s `ts_event` v [start, end], které už v `now` byly v DB."""
    stmt = select(*EVENT_COLUMNS).where(
        news_events.c.ts_event >= start,
        news_events.c.ts_event <= end,
        news_events.c.ts_ingested <= now,
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).fetchall()
    return [event_from_row(row) for row in rows]


class AnomalyJob:
    """Shluky zpráv × mimořádná výchylka ES/NQ → payloady pro kanál `alerts`."""

    def __init__(
        self,
        engine: Engine,
        bars: BarsRepository,
        *,
        started_at: dt.datetime | None = None,
        symbols: Sequence[str] = ("ES", "NQ"),
        window_min: int = DEFAULT_PRIMARY_WINDOW_MIN,
        tz: ZoneInfo = LOCAL_TZ,
    ) -> None:
        self._engine = engine
        self._bars = bars
        self._symbols = list(symbols)
        self._window = window_min
        self._tz = tz
        # Watermark: shluky hotové před startem procesu se nehodnotí
        self._started_at = started_at or dt.datetime.now(dt.UTC)
        self._announced: dict[str, list[dt.datetime]] = {symbol: [] for symbol in self._symbols}
        self._baselines: dict[str, tuple[dt.date, Baseline | None]] = {}

    # ── IO ────────────────────────────────────────────────────────
    def load_events(
        self, start: dt.datetime, end: dt.datetime, now: dt.datetime
    ) -> list[ClusterEvent]:
        return select_events(self._engine, start, end, now)

    def baseline(self, symbol: str, now: dt.datetime) -> Baseline | None:
        """Baseline výchylek z 20 seancí před seancí `now`, cachovaná per seance."""
        session = trading_session_date(now)
        cached = self._baselines.get(symbol)
        if cached is not None and cached[0] == session:
            return cached[1]
        sessions = self._bars.recent_sessions(symbol, session, MIN_BASELINE_SESSIONS)
        baseline: Baseline | None = None
        if sessions:
            baseline = build_excursion_baseline(sessions, self._window)
        # Pokrytí musí být vidět (vzor #1001): bez baseline se neohlásí nic
        logger.info(
            "Baseline výchylek %s (seance %s): %d seancí, %d minut dne, %d vzorků",
            symbol,
            session.isoformat(),
            len(sessions),
            len(baseline or {}),
            sum(len(items) for items in (baseline or {}).values()),
        )
        self._baselines[symbol] = (session, baseline)
        return baseline

    # ── běh ───────────────────────────────────────────────────────
    def run(self, now: dt.datetime) -> list[dict[str, Any]]:
        """Payloady `news_anomaly` za tento běh."""
        events = self.load_events(now - LOOKBACK - CLUSTER_SPAN, now, now)
        clusters = build_clusters(events)
        if not clusters:
            return []
        alerts: list[dict[str, Any]] = []
        for symbol in self._symbols:
            bars = self._bars.load_range(
                symbol,
                now - LOOKBACK - dt.timedelta(minutes=SIGMA_LOOKBACK_MIN) - _BARS_MARGIN,
                now,
            )
            outcome = evaluate_clusters(
                clusters,
                symbol,
                bars,
                self.baseline(symbol, now),
                self._announced[symbol],
                now=now,
                not_before=self._started_at,
                window=self._window,
                tz=self._tz,
            )
            if outcome.unmeasurable:
                logger.info(
                    "Reakce na zprávy %s: %d shluků při otevřeném trhu nejde změřit "
                    "(chybí bary nebo baseline)",
                    symbol,
                    outcome.unmeasurable,
                )
            self._announced[symbol] = [
                start
                for start in (*self._announced[symbol], *outcome.announced)
                if start >= now - LOOKBACK - COOLDOWN
            ]
            alerts.extend(outcome.alerts)
        if alerts:
            logger.info(
                "Reakce na zprávy: %d upozornění (%s)",
                len(alerts),
                ", ".join(str(alert["symbol"]) for alert in alerts),
            )
        return alerts


def as_utc(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
