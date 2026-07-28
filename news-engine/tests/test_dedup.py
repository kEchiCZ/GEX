"""Testy deduplikace (#273): rolling okno, cross-source merge, priming."""

import datetime as dt
from pathlib import Path

from sqlalchemy import create_engine, select

from gexlens_engine.storage.sentiment import ensure_sentiment_schema, news_events
from gexlens_news.dedup import RollingDeduplicator
from gexlens_news.model import NewsEvent
from gexlens_news.pipeline import DedupingWriter
from gexlens_news.store import NewsWriter

TS = dt.datetime(2026, 7, 28, 13, 59, 58, tzinfo=dt.UTC)


def event(
    title: str, source: str, *, at: dt.datetime = TS, ingested: dt.datetime | None = None
) -> NewsEvent:
    return NewsEvent(
        ts_event=at,
        ts_ingested=ingested or at,
        source=source,
        kind="headline",
        title=title,
        source_uid=f"{source}-{title}",
    )


# ── Rolling okno ───────────────────────────────────────────────────


def test_rolling_window_beats_fixed_buckets_across_the_boundary() -> None:
    """Jádro #273: 13:59:58 a 14:00:01 patří k sobě, buckety by je rozdělily."""
    dedup = RollingDeduplicator(window_minutes=10)
    before = event("Fed holds rates", "finnhub", at=TS)
    after = event("Fed holds rates", "rss_news", at=TS + dt.timedelta(seconds=3))

    result = dedup.process([before, after])
    assert [e.source for e in result.events] == ["finnhub"]
    assert result.merged == 1


def test_same_story_outside_window_is_a_new_event() -> None:
    dedup = RollingDeduplicator(window_minutes=10)
    first = event("Stocks close higher", "finnhub", at=TS)
    much_later = event("Stocks close higher", "rss_news", at=TS + dt.timedelta(minutes=45))

    result = dedup.process([first, much_later])
    assert len(result.events) == 2  # po 45 minutách je to nová zpráva
    assert result.merged == 0


def test_repeated_fetch_of_same_source_is_a_duplicate_not_a_merge() -> None:
    """Týž zdroj v okně = znovu stažený feed, ne potvrzení jiným zdrojem."""
    dedup = RollingDeduplicator(window_minutes=10)
    result = dedup.process(
        [
            event("ECB signals cut", "rss_news", at=TS),
            event("ECB signals cut", "rss_news", at=TS + dt.timedelta(minutes=1)),
        ]
    )
    assert len(result.events) == 1
    assert result.duplicates == 1
    assert result.merged == 0


def test_normalized_title_matches_across_wording() -> None:
    dedup = RollingDeduplicator(window_minutes=10)
    result = dedup.process(
        [
            event("The Fed holds rates!", "finnhub", at=TS),
            event("Fed holds rates", "rss_news", at=TS + dt.timedelta(seconds=30)),
        ]
    )
    assert len(result.events) == 1
    assert result.merged == 1


def test_merge_records_source_and_latency() -> None:
    """Latence per zdroj je podklad pro budoucí prioritizaci (SPEC 3.3)."""
    dedup = RollingDeduplicator(window_minutes=10)
    fast = event("Payrolls beat", "finnhub", at=TS, ingested=TS)
    slow = event(
        "Payrolls beat",
        "rss_news",
        at=TS + dt.timedelta(seconds=5),
        ingested=TS + dt.timedelta(seconds=42),
    )
    dedup.process([fast, slow])

    merged = dedup.merged_sources(fast)
    assert len(merged) == 1
    assert merged[0]["source"] == "rss_news"
    assert merged[0]["latency_s"] == 42.0


def test_key_ignores_day_so_midnight_stories_merge() -> None:
    """Na rozdíl od dedup_hash klíč okna nezná datum — story přes půlnoc splyne."""
    dedup = RollingDeduplicator(window_minutes=10)
    before_midnight = dt.datetime(2026, 7, 28, 23, 59, 30, tzinfo=dt.UTC)
    result = dedup.process(
        [
            event("Overnight selloff", "finnhub", at=before_midnight),
            event("Overnight selloff", "rss_news", at=before_midnight + dt.timedelta(minutes=2)),
        ]
    )
    assert len(result.events) == 1
    assert result.merged == 1


# ── Zápisová cesta ─────────────────────────────────────────────────


def make_writer(tmp_path: Path) -> tuple[DedupingWriter, NewsWriter]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    inner = NewsWriter(engine)
    return DedupingWriter(inner, window_minutes=10), inner


def test_deduping_writer_persists_merged_sources(tmp_path: Path) -> None:
    writer, inner = make_writer(tmp_path)
    written = writer.write(
        [
            event("Fed holds rates", "finnhub", at=TS, ingested=TS),
            event(
                "Fed holds rates",
                "rss_news",
                at=TS + dt.timedelta(seconds=3),
                ingested=TS + dt.timedelta(seconds=20),
            ),
        ]
    )
    assert written == 1
    assert writer.merged_total == 1

    with inner._engine.connect() as conn:  # noqa: SLF001 — kontrola uloženého tvaru
        row = conn.execute(select(news_events.c.source, news_events.c.raw)).fetchone()
    assert row is not None
    assert row.source == "finnhub"  # nejrychlejší zdroj zůstává
    merged = row.raw["merged_sources"]
    assert merged[0]["source"] == "rss_news"
    assert merged[0]["latency_s"] == 20.0


def test_priming_from_db_prevents_duplicates_after_restart(tmp_path: Path) -> None:
    """Po restartu je okno prázdné — bez priming by se čerstvý sběr zapsal znovu."""
    writer, inner = make_writer(tmp_path)
    assert writer.write([event("Breaking story", "finnhub", at=TS)]) == 1

    # Nový proces nad toutéž DB
    restarted = DedupingWriter(inner, window_minutes=10)
    primed = restarted.prime_from_db(TS + dt.timedelta(minutes=2))
    assert primed == 1

    # Tatáž story z jiného zdroje se teď sloučí místo zápisu
    assert restarted.write([event("Breaking story", "rss_news", at=TS)]) == 0
    assert restarted.merged_total == 1
    assert inner.count() == 1


def test_dedup_hash_still_guards_against_double_write(tmp_path: Path) -> None:
    """Poslední pojistka: i kdyby okno selhalo, UNIQUE v DB zápis nepustí."""
    writer, inner = make_writer(tmp_path)
    assert writer.write([event("Guarded", "finnhub", at=TS)]) == 1
    # Obejití dedupu (jiná instance bez okna) — DB duplicitu odmítne sama
    assert inner.write([event("Guarded", "rss_news", at=TS)]) == 0
    assert inner.count() == 1
