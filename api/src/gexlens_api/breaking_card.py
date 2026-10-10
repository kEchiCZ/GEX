"""Karta „Breaking news“ — výběr a dopad při čtení (E-6.28b, ADR-0059 bod 4 a 8).

API tu počítá, ne jen čte storage. Výjimka má precedens ve významnosti při čtení
(ADR-0045 bod 5) a rozhodl ji vlastník 10. 10. 2026 (varianta A, #1385
Rozhodnuto): push z news-enginu by potřeboval novou smyčku a stav po načtení
stránky by API stejně muselo spočítat. Všechna pravidla jsou sdílené čisté
funkce (`gexlens_news.breaking`, `reactions`, `compute/news_tier`), nic se
nekopíruje.

* **Výběr:** zprávy s `importance ≥ 2` (nadmnožina) viditelné v `at`
  (`ts_ingested ≤ at`), přesné pravidlo `breaking.is_breaking` s efektivním
  tierem z prvního doručení a kopií `news_event_sources` viditelných v `at`.
* **Dopad ES/NQ** z minutových barů `derived/{sym}/bars`: bez `at` (živě) se
  bere i rozpracovaná minuta, kterou engine zapisuje každý cyklus; s `at`
  (replay) jen uzavřené minuty, aby poslední minuta nenesla budoucí cenu.
* **Kontaminace K1** jako `ReactionJob._contaminating`: jiný event s importance
  ≥ 2 a jinou kategorií v okně, známý v `at`.
"""

import bisect
import datetime as dt
import threading
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.engine import Engine

from gexlens_engine.compute.marketclock import is_market_closed
from gexlens_engine.compute.news_significance import SIGNIFICANT_MIN_IMPORTANCE, is_key
from gexlens_engine.compute.news_tier import SourceCopy, effective_tier
from gexlens_engine.storage.sentiment import news_event_sources, news_events
from gexlens_news import breaking
from gexlens_news.bars import BarsRepository
from gexlens_news.reaction_job import CLOSURE_LOOKBACK_DAYS, CONTAMINATION_MIN_IMPORTANCE
from gexlens_news.reactions import SIGMA_LOOKBACK_MIN, Bar, contaminates

SYMBOLS = ("ES", "NQ")
#: Partic barů v paměti: 2 symboly × (okno karty + základ až 5 dní zpět + kraje)
BARS_CACHE_DAYS = 24

_MINUTE = dt.timedelta(minutes=1)
#: Základ = poslední bar před zprávou až přes celé zavření, stejně jako
#: `ReactionJob` (po otevření Globexu je to páteční close)
_BASE_LOOKBACK = dt.timedelta(days=CLOSURE_LOOKBACK_DAYS)
#: Hodina barů před zprávou pro σ výchylky (ADR-0043) a bar základu
_SIGMA_BEFORE = dt.timedelta(minutes=SIGMA_LOOKBACK_MIN + 2)
_BARS_AFTER = dt.timedelta(minutes=breaking.CARD_WINDOW_MIN + 1)


class FreshBars(BarsRepository):
    """Partice barů s cache podle mtime — engine dnešní partici přepisuje každý cyklus.

    Bind mount stojí ~20 ms na soubor (lessons-learned), mtime je levný `stat`.
    Endpoint běží ve vláknech FastAPI, proto zámek.
    """

    def __init__(self, data_dir: Path, capacity: int = BARS_CACHE_DAYS) -> None:
        super().__init__(data_dir)
        self._capacity = capacity
        self._cache: dict[tuple[str, dt.date], tuple[float, list[Bar]]] = {}
        self._lock = threading.Lock()

    def load_day(self, symbol: str, day: dt.date) -> list[Bar]:
        try:
            mtime = self._path(symbol, day).stat().st_mtime
        except FileNotFoundError:
            return []
        key = (symbol, day)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached[0] == mtime:
                return cached[1]
        bars = super().load_day(symbol, day)
        with self._lock:
            if key not in self._cache and len(self._cache) >= self._capacity:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = (mtime, bars)
        return bars


#: Zafixovaných dopadů v paměti (položka × symbol); karta jich má nejvýš stovky
IMPACT_MEMO_SIZE = 4096


class CardCache:
    """Stav karty mezi dotazy: partice barů podle mtime a zafixované dopady.

    Zafixovaný dopad (po 5. minutě nebo při zavřeném trhu) závisí jen na barech
    kolem zprávy a na kontaminujících eventech. Klíčem je proto jejich obsah:
    přepíše-li engine bar nebo doplní-li díru, klíč se změní a dopad se spočítá
    znovu. Výpočet dopadu tvořil ~2/3 odpovědi (měřeno 10. 10., 50 položek).
    """

    def __init__(self, data_dir: Path) -> None:
        self.bars = FreshBars(data_dir)
        self._impacts: dict[tuple[object, ...], breaking.CardImpact] = {}
        self._lock = threading.Lock()

    def impact(
        self,
        ts_event: dt.datetime,
        bars: Sequence[Bar],
        *,
        at: dt.datetime,
        other_event_ts: Sequence[dt.datetime],
        market_closed: bool,
    ) -> breaking.CardImpact:
        window_end = ts_event + dt.timedelta(minutes=breaking.CARD_WINDOW_MIN)
        if not market_closed and at < window_end:
            # Běžící okno se mění s každou minutou — nepamatuje se
            return breaking.card_impact(
                ts_event, bars, at=at, other_event_ts=other_event_ts, market_closed=False
            )
        key = (
            ts_event,
            market_closed,
            tuple(other_event_ts),
            tuple((bar.ts, bar.high, bar.low, bar.close) for bar in bars),
        )
        elapsed = max(0, int((at - ts_event) / _MINUTE))
        with self._lock:
            cached = self._impacts.get(key)
        if cached is not None:
            return replace(cached, elapsed_min=elapsed)
        result = breaking.card_impact(
            ts_event, bars, at=at, other_event_ts=other_event_ts, market_closed=market_closed
        )
        with self._lock:
            if len(self._impacts) >= IMPACT_MEMO_SIZE:
                self._impacts.pop(next(iter(self._impacts)))
            self._impacts[key] = result
        return result


def _utc(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def _iso(value: dt.datetime) -> str:
    return _utc(value).isoformat()


def breaking_card(
    engine: Engine,
    cache: CardCache,
    *,
    at: dt.datetime | None,
    now: dt.datetime,
    hours: int,
    limit: int,
) -> dict[str, Any]:
    """Položky karty v čase `at` (None = teď, živě), nejnovější první."""
    live = at is None
    moment = now if at is None else _utc(at)
    since = moment - dt.timedelta(hours=hours)
    with engine.connect() as conn:
        events = conn.execute(
            select(
                news_events.c.id,
                news_events.c.ts_event,
                news_events.c.ts_ingested,
                news_events.c.source,
                news_events.c.kind,
                news_events.c.category,
                news_events.c.importance,
                news_events.c.title,
                news_events.c.content_tier,
            )
            .where(
                news_events.c.ts_event >= since,
                news_events.c.ts_event <= moment,
                news_events.c.ts_ingested <= moment,
                news_events.c.importance >= SIGNIFICANT_MIN_IMPORTANCE,
            )
            .order_by(desc(news_events.c.ts_event), desc(news_events.c.id))
        ).fetchall()
        ids = [int(row.id) for row in events]
        copy_rows = (
            conn.execute(
                select(news_event_sources)
                .where(
                    news_event_sources.c.event_id.in_(ids),
                    news_event_sources.c.fetched_at <= moment,
                )
                .order_by(news_event_sources.c.fetched_at)
            ).fetchall()
            if ids
            else []
        )
    copies: dict[int, list[Any]] = defaultdict(list)
    for row in copy_rows:
        copies[int(row.event_id)].append(row)

    selected = []
    for row in events:
        ts_event, ts_ingested = _utc(row.ts_event), _utc(row.ts_ingested)
        plain = [
            SourceCopy(c.content_tier, _utc(c.published_at), _utc(c.fetched_at))
            for c in copies[int(row.id)]
        ]
        tier = effective_tier(row.content_tier, plain, at=moment)
        if breaking.is_breaking(
            tier, row.kind, row.importance, row.category, ingest_delay=ts_ingested - ts_event
        ):
            selected.append((row, ts_event, ts_ingested, tier))
        if len(selected) >= limit:
            break

    items: list[dict[str, Any]] = []
    if selected:
        first = min(item[1] for item in selected)
        last = max(item[1] for item in selected)
        others = _contaminating(engine, first, last + _BARS_AFTER, moment)
        series = {symbol: _series(cache.bars, symbol, first, moment, live) for symbol in SYMBOLS}
        stamps = {symbol: [bar.ts for bar in bars] for symbol, bars in series.items()}
        for row, ts_event, ts_ingested, tier in selected:
            items.append(
                _item(
                    row,
                    ts_event,
                    ts_ingested,
                    tier,
                    copies[int(row.id)],
                    series,
                    stamps,
                    others,
                    moment,
                    cache,
                )
            )
    return {
        "as_of": moment.isoformat(),
        "live": live,
        "market_closed": is_market_closed(moment),
        "items": items,
    }


def _series(
    bars: BarsRepository, symbol: str, first: dt.datetime, moment: dt.datetime, live: bool
) -> list[Bar]:
    """Bary symbolu od hodiny před nejstarší zprávou do `moment`.

    Základ dopadu je poslední bar před zprávou až 5 dní zpět (jako `ReactionJob`).
    Hlubší historii je potřeba dočíst jen tehdy, když řada nemá bar před nejstarší
    zprávou (zpráva těsně po zavření). Pětidenní řada pokaždé by odpověď
    zpomalila ~3× (měřeno 10. 10.).
    """
    series = _visible(bars.load_range(symbol, first - _SIGMA_BEFORE, moment), moment, live)
    if not series or series[0].ts >= first:
        series = _visible(bars.load_range(symbol, first - _BASE_LOOKBACK, moment), moment, live)
    return series


def _visible(series: Sequence[Bar], moment: dt.datetime, live: bool) -> list[Bar]:
    """Bary existující v `moment`: živě i rozpracovaná minuta, v replayi jen uzavřené."""
    if live:
        return [bar for bar in series if bar.ts <= moment]
    return [bar for bar in series if bar.ts + _MINUTE <= moment]


def _around(
    series: Sequence[Bar], stamps: Sequence[dt.datetime], ts_event: dt.datetime
) -> list[Bar]:
    """Bary pro dopad jedné zprávy: hodina před ní (σ), poslední bar před ní (základ,
    i přes zavření) a okno karty. Celá řada by výpočet zpomalila ~10× (měřeno 10. 10.)."""
    before = bisect.bisect_left(stamps, ts_event)
    start = min(bisect.bisect_left(stamps, ts_event - _SIGMA_BEFORE), max(0, before - 1))
    end = bisect.bisect_left(stamps, ts_event + _BARS_AFTER)
    return list(series[start:end])


def _contaminating(
    engine: Engine, start: dt.datetime, end: dt.datetime, moment: dt.datetime
) -> list[tuple[dt.datetime, str | None]]:
    """Kandidáti kontaminace K1 v rozsahu karty, známí v `moment` (point-in-time)."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(news_events.c.ts_event, news_events.c.category).where(
                news_events.c.ts_event > start,
                news_events.c.ts_event <= end,
                news_events.c.ts_ingested <= moment,
                news_events.c.importance >= CONTAMINATION_MIN_IMPORTANCE,
            )
        ).fetchall()
    return [(_utc(row.ts_event), row.category) for row in rows]


def _item(
    row: Any,
    ts_event: dt.datetime,
    ts_ingested: dt.datetime,
    tier: int | None,
    copies: Sequence[Any],
    series: dict[str, list[Bar]],
    stamps: dict[str, list[dt.datetime]],
    others: Sequence[tuple[dt.datetime, str | None]],
    moment: dt.datetime,
    cache: CardCache,
) -> dict[str, Any]:
    sources = [
        {
            "source": row.source,
            "content_tier": row.content_tier,
            "published_at": ts_event.isoformat(),
            "fetched_at": ts_ingested.isoformat(),
            "delay_s": (ts_ingested - ts_event).total_seconds(),
        },
        *(
            {
                "source": copy.source,
                "content_tier": copy.content_tier,
                "published_at": _iso(copy.published_at),
                "fetched_at": _iso(copy.fetched_at),
                "delay_s": (_utc(copy.fetched_at) - ts_event).total_seconds(),
            }
            for copy in copies
        ),
    ]
    window_end = ts_event + dt.timedelta(minutes=breaking.CARD_WINDOW_MIN)
    contaminating = [
        ts
        for ts, category in others
        if ts_event < ts < window_end and contaminates(row.category, category)
    ]
    closed = is_market_closed(ts_event)
    impact = {
        symbol: asdict(
            cache.impact(
                ts_event,
                _around(bars, stamps[symbol], ts_event),
                at=moment,
                other_event_ts=contaminating,
                market_closed=closed,
            )
        )
        for symbol, bars in series.items()
    }
    return {
        "id": int(row.id),
        "ts_event": ts_event.isoformat(),
        "ts_ingested": ts_ingested.isoformat(),
        "title": row.title,
        "source": row.source,
        "kind": row.kind,
        "category": row.category,
        "importance": row.importance,
        "is_key": is_key(row.kind, row.importance, row.category),
        "group": breaking.card_group(row.category, row.source),
        "theme": breaking.theme(str(row.title or "")),
        "effective_tier": tier,
        "confirmed": breaking.is_confirmed(tier),
        "sources": sources,
        "impact": impact,
    }
