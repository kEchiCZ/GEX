"""Párování položek kalendáře FF s uloženými řádky (#1298, ADR-0018 dodatek 17. 11.).

Klíč řádku kalendáře je řada + minuta releasu (`compute.newstext.scheduled_key`), FF `id`
nese jen historická stránka. FF ale čas položky po zveřejnění posouvá (tentative
releasy, aukce, BOJ: 2026-10-30 02:30 → 03:02), a to i ve widget feedu. Sám klíč
s minutou by z posunu udělal druhý řádek a původní by zůstal bez actual.

Posun se proto pozná ve skupině „řada + den UTC“ proti **celé** stránce nebo feedu:
položka se s řádkem páruje podle FF `id`, pak podle minuty. Zbude-li ve skupině právě
jeden řádek, který ve feedu není, a právě jedna položka bez řádku, je to tentýž release
s posunutým časem (posun 1:1). Nový řádek se nezakládá a actual dostane původní řádek,
stejně jako před #1298 přes denní klíč. Čas řádku se nemění (správný čas posunutých
releasů je samostatné issue). Jiný nepoměr ve skupině (2:1, 1:2) je nejasný: živý zápis
položky zapíše, ať se release neztratí.

Vstup musí být celá stránka nebo feed týdne: řádek „chybí ve feedu“ jen tehdy, když
feed pokrývá jeho den. Den UTC, který leží na dvou týdnech FF (sobota večer ET), by
se mohl spárovat falešně; řada se dvěma releasy v tu dobu v datech není.
"""

import datetime as dt
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine

from gexlens_engine.compute.newstext import normalize_title, scheduled_key
from gexlens_engine.storage.sentiment import news_events
from gexlens_news.model import NewsEvent
from gexlens_news.store import FF_SOURCE, SCHEDULED_KIND

Group = tuple[str, dt.date]


def _utc(value: dt.datetime) -> dt.datetime:
    return value.astimezone(dt.UTC) if value.tzinfo else value.replace(tzinfo=dt.UTC)


def calendar_group(title: str, ts_event: dt.datetime) -> Group:
    """Skupina „řada + den UTC“ (= identita denního klíče před #1298)."""
    return normalize_title(title), _utc(ts_event).date()


def is_calendar_event(event: NewsEvent) -> bool:
    return event.source == FF_SOURCE and event.kind == SCHEDULED_KIND


@dataclass(frozen=True)
class CalendarRow:
    """Uložený řádek kalendáře, jak ho párování potřebuje."""

    id: int
    title: str
    ts_event: dt.datetime
    ff_id: str | None


@dataclass
class GroupMatch:
    """Výsledek párování jedné skupiny „řada + den“."""

    matched: list[tuple[NewsEvent, CalendarRow]] = field(default_factory=list)
    #: Položky feedu bez řádku
    unmatched: list[NewsEvent] = field(default_factory=list)
    #: Řádky, které ve feedu nejsou
    rows_left: list[CalendarRow] = field(default_factory=list)

    @property
    def rescheduled(self) -> tuple[CalendarRow, NewsEvent] | None:
        """Posun 1:1: z feedu zmizel jeden řádek a přibyla jedna položka."""
        if len(self.unmatched) == 1 and len(self.rows_left) == 1:
            return self.rows_left[0], self.unmatched[0]
        return None


def match_group(feed: Sequence[NewsEvent], rows: Sequence[CalendarRow]) -> GroupMatch:
    """Spáruje položky jedné skupiny s řádky: nejdřív podle FF `id`, pak podle minuty."""
    result = GroupMatch(rows_left=list(rows))
    for event in feed:
        ff_id = event.raw.get("id")
        key = scheduled_key(event.title, event.ts_event)
        hit = next(
            (r for r in result.rows_left if ff_id is not None and r.ff_id == str(ff_id)),
            None,
        ) or next(
            (r for r in result.rows_left if scheduled_key(r.title, r.ts_event) == key),
            None,
        )
        if hit is None:
            result.unmatched.append(event)
        else:
            result.rows_left.remove(hit)
            result.matched.append((event, hit))
    return result


def match_feed(feed: Iterable[NewsEvent], rows: Iterable[CalendarRow]) -> dict[Group, GroupMatch]:
    """Párování po skupinách; táž položka dvakrát (týž klíč) je jeden release."""
    by_group: dict[Group, dict[str, NewsEvent]] = defaultdict(dict)
    for event in feed:
        by_group[calendar_group(event.title, event.ts_event)].setdefault(
            scheduled_key(event.title, event.ts_event), event
        )
    rows_by_group: dict[Group, list[CalendarRow]] = defaultdict(list)
    for row in rows:
        rows_by_group[calendar_group(row.title, row.ts_event)].append(row)
    return {
        group: match_group(
            sorted(by_group.get(group, {}).values(), key=lambda e: e.ts_event),
            rows_by_group.get(group, []),
        )
        for group in sorted(set(by_group) | set(rows_by_group), key=lambda g: (g[1], g[0]))
    }


def load_rows(conn: Connection, start: dt.datetime, end: dt.datetime) -> list[CalendarRow]:
    """Řádky kalendáře FF s `ts_event` v [start, end]."""
    rows = conn.execute(
        select(
            news_events.c.id, news_events.c.title, news_events.c.ts_event, news_events.c.raw
        ).where(
            news_events.c.source == FF_SOURCE,
            news_events.c.kind == SCHEDULED_KIND,
            news_events.c.ts_event >= start,
            news_events.c.ts_event <= end,
        )
    ).fetchall()
    out = []
    for row in rows:
        raw = row.raw if isinstance(row.raw, dict) else {}
        ff_id = raw.get("id")
        out.append(
            CalendarRow(
                id=int(row.id),
                title=str(row.title),
                ts_event=_utc(row.ts_event),
                ff_id=str(ff_id) if ff_id is not None else None,
            )
        )
    return out


def rows_for_feed(conn: Connection, feed: Sequence[NewsEvent]) -> list[CalendarRow]:
    """Řádky celých dnů UTC, které feed pokrývá (prázdný feed = žádné řádky)."""
    if not feed:
        return []
    days = [_utc(event.ts_event).date() for event in feed]
    start = dt.datetime.combine(min(days), dt.time(), tzinfo=dt.UTC)
    end = dt.datetime.combine(max(days), dt.time.max, tzinfo=dt.UTC)
    return load_rows(conn, start, end)


def rescheduled_pairs(
    conn: Connection, events: Sequence[NewsEvent]
) -> list[tuple[CalendarRow, NewsEvent]]:
    """Posuny 1:1 ve feedu kalendáře FF proti uloženým řádkům (ostatní eventy se ignorují)."""
    feed = [event for event in events if is_calendar_event(event)]
    matches = match_feed(feed, rows_for_feed(conn, feed))
    return [pair for match in matches.values() if (pair := match.rescheduled) is not None]


def drop_rescheduled(
    engine: Engine, events: Sequence[NewsEvent]
) -> tuple[list[NewsEvent], list[tuple[CalendarRow, NewsEvent]]]:
    """Dávka bez položek, které jsou jen posunutým časem uloženého řádku (živý zápis).

    Vrací (dávka k zápisu, posuny). Dávka bez kalendáře FF se do DB neptá.
    """
    if not any(is_calendar_event(event) for event in events):
        return list(events), []
    with engine.connect() as conn:
        pairs = rescheduled_pairs(conn, events)
    if not pairs:
        return list(events), []
    moved = {scheduled_key(event.title, event.ts_event) for _, event in pairs}
    kept = [
        event
        for event in events
        if not (is_calendar_event(event) and scheduled_key(event.title, event.ts_event) in moved)
    ]
    return kept, pairs
