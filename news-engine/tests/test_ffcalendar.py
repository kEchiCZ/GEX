"""Párování položek kalendáře FF s uloženými řádky a posun 1:1 (#1298, varianta A2).

Tvary jsou z produkce 16.–17. 11. 2026: Cleveland Fed 16. 11. uložený živě ve 14:48,
FF ho po zveřejnění posunul na 14:45 i ve widget feedu; ADP Weekly 10. 11. má
na stránce FF dvě položky (13:14 a 13:15), dřívější FF přidává až dodatečně.
"""

import datetime as dt
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import ensure_sentiment_schema
from gexlens_news.ffcalendar import CalendarRow, drop_rescheduled, match_feed, match_group
from gexlens_news.model import NewsEvent
from gexlens_news.store import NewsWriter

CLEVELAND = "USD Cleveland Fed Inflation Expectations"
ADP = "USD ADP Weekly Employment Change"
STORED = dt.datetime(2026, 11, 16, 14, 48, tzinfo=dt.UTC)
MOVED = dt.datetime(2026, 11, 16, 14, 45, tzinfo=dt.UTC)
ADP_EARLY = dt.datetime(2026, 11, 10, 13, 14, tzinfo=dt.UTC)
ADP_CURRENT = dt.datetime(2026, 11, 10, 13, 15, tzinfo=dt.UTC)


def ff(title: str, at: dt.datetime, *, ff_id: int | None = None) -> NewsEvent:
    return NewsEvent(
        ts_event=at,
        ts_ingested=at - dt.timedelta(days=1),
        source="forexfactory",
        kind="scheduled",
        title=title,
        source_uid=f"USD|{title}|{at.isoformat()}",
        raw={"id": ff_id} if ff_id is not None else {},
    )


def row(row_id: int, title: str, at: dt.datetime, ff_id: str | None = None) -> CalendarRow:
    return CalendarRow(id=row_id, title=title, ts_event=at, ff_id=ff_id)


def test_feed_item_matches_row_by_minute_and_by_ff_id() -> None:
    by_minute = match_group([ff(CLEVELAND, STORED)], [row(1, CLEVELAND, STORED)])
    assert (by_minute.unmatched, by_minute.rows_left) == ([], [])

    # Backfill z 29. 7. nese FF id; stránka FF mezitím čas posunula
    by_id = match_group([ff(CLEVELAND, MOVED, ff_id=7)], [row(1, CLEVELAND, STORED, "7")])
    assert [r.id for _, r in by_id.matched] == [1]
    assert by_id.rescheduled is None


def test_one_row_gone_and_one_item_new_is_a_reschedule() -> None:
    match = match_group([ff(CLEVELAND, MOVED)], [row(1, CLEVELAND, STORED)])
    assert match.rescheduled is not None
    stored, event = match.rescheduled
    assert (stored.id, event.ts_event) == (1, MOVED)


def test_added_second_release_is_not_a_reschedule() -> None:
    """ADP Weekly: k uloženému 13:15 přibyla položka 13:14, uložený zůstal ve feedu."""
    match = match_group([ff(ADP, ADP_EARLY), ff(ADP, ADP_CURRENT)], [row(1, ADP, ADP_CURRENT)])
    assert [e.ts_event for e in match.unmatched] == [ADP_EARLY]
    assert match.rows_left == []
    assert match.rescheduled is None


def test_uneven_group_is_unclear_not_a_reschedule() -> None:
    match = match_group(
        [ff(ADP, ADP_EARLY), ff(ADP, ADP_CURRENT)], [row(1, ADP, ADP_EARLY.replace(minute=0))]
    )
    assert (len(match.unmatched), len(match.rows_left)) == (2, 1)
    assert match.rescheduled is None


def test_match_feed_groups_by_series_and_utc_day() -> None:
    other_day = STORED + dt.timedelta(days=1)
    matches = match_feed(
        [ff(CLEVELAND, MOVED), ff(CLEVELAND, other_day), ff(ADP, ADP_CURRENT)],
        [row(1, CLEVELAND, STORED)],
    )
    assert len(matches) == 3
    assert [m.rescheduled is not None for m in matches.values()] == [False, True, False]


def make_engine(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    return engine


def test_drop_rescheduled_keeps_new_releases_and_other_sources(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    NewsWriter(engine).write([ff(CLEVELAND, STORED), ff(ADP, ADP_CURRENT)])
    headline = NewsEvent(
        ts_event=MOVED,
        ts_ingested=MOVED,
        source="rss_news",
        kind="headline",
        title="Cleveland Fed inflation expectations rise",
    )
    feed = [ff(CLEVELAND, MOVED), ff(ADP, ADP_EARLY), ff(ADP, ADP_CURRENT), headline]

    kept, moves = drop_rescheduled(engine, feed)

    assert [(e.title, e.ts_event) for e in kept] == [
        (ADP, ADP_EARLY),
        (ADP, ADP_CURRENT),
        (headline.title, MOVED),
    ]
    assert [(r.ts_event, e.ts_event) for r, e in moves] == [(STORED, MOVED)]


def test_drop_rescheduled_without_calendar_does_not_query(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    headline = NewsEvent(
        ts_event=MOVED, ts_ingested=MOVED, source="rss_news", kind="headline", title="x"
    )
    engine.dispose()
    (tmp_path / "news.sqlite").unlink()
    # Bez řádku kalendáře se do DB nesahá — smazaná DB nevadí
    assert drop_rescheduled(engine, [headline]) == ([headline], [])
