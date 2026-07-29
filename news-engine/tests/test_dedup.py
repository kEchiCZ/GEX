"""Testy deduplikace (#273, #274): rolling okno, cross-source merge, fuzzy, priming."""

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
    title: str,
    source: str,
    *,
    at: dt.datetime = TS,
    ingested: dt.datetime | None = None,
    kind: str = "headline",
) -> NewsEvent:
    return NewsEvent(
        ts_event=at,
        ts_ingested=ingested or at,
        source=source,
        kind=kind,
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


# ── Šířka okna (#351) ──────────────────────────────────────────────


def test_default_window_catches_republication_across_midnight() -> None:
    """Jádro #351: republikace s Δt 24 min přes půlnoc UTC (reálný případ
    „Iran launches surprise ballistic missile attack" 23:5x → 00:1x) prošla
    10min oknem i denním dedup_hashem. Výchozí okno ji musí zahodit.
    """
    dedup = RollingDeduplicator()
    before_midnight = dt.datetime(2026, 7, 28, 23, 52, 0, tzinfo=dt.UTC)
    result = dedup.process(
        [
            event(
                "Iran launches surprise ballistic missile attack on U.S. forces",
                "rss_news",
                at=before_midnight,
            ),
            event(
                "Iran launches surprise ballistic missile attack on U.S. forces",
                "rss_news",
                at=before_midnight + dt.timedelta(minutes=24),
            ),
        ]
    )
    assert len(result.events) == 1
    assert result.duplicates == 1


def test_daily_recurring_title_stays_a_new_event() -> None:
    """Denní rubrika se stejným titulkem (Δt ≈ 24 h) je nové vydání, ne
    duplicita — výchozí okno na ni nesmí dosáhnout.
    """
    dedup = RollingDeduplicator()
    result = dedup.process(
        [
            event("Basic Materials Roundup: Market Talk", "rss_news", at=TS),
            event(
                "Basic Materials Roundup: Market Talk",
                "rss_news",
                at=TS + dt.timedelta(hours=24),
            ),
        ]
    )
    assert len(result.events) == 2
    assert result.duplicates == 0


# ── Fuzzy vrstva (#274) ────────────────────────────────────────────
# Titulky v testech jsou skutečné páry z provozních dat 28.–29. 7. 2026,
# na kterých byl práh J ≥ 0.9 změřen (ADR-0016).


def test_reformulated_story_merges_across_sources() -> None:
    """Jádro #274: totéž s drobnou obměnou znění je jedna story (J≈0.93)."""
    dedup = RollingDeduplicator(window_minutes=10)
    result = dedup.process(
        [
            event(
                "Gold prices today: Gold remains below $4,100 ahead of Fed meeting",
                "rss_yahoo",
                at=TS,
            ),
            event(
                "Gold prices today: Gold remains below $4,100 ahead of Fed meeting tomorrow",
                "rss_cnbc",
                at=TS + dt.timedelta(minutes=2),
            ),
        ]
    )
    assert len(result.events) == 1
    assert result.merged == 1


def test_reformulated_repeat_from_same_source_is_duplicate() -> None:
    dedup = RollingDeduplicator(window_minutes=10)
    result = dedup.process(
        [
            event(
                "SK Hynix second-quarter profit surges 557% to a new high — but misses estimates",
                "rss_yahoo",
            ),
            event(
                "SK Hynix second-quarter profit surges to a new high — but misses estimates",
                "rss_yahoo",
                at=TS + dt.timedelta(minutes=1),
            ),
        ]
    )
    assert len(result.events) == 1
    assert result.duplicates == 1
    assert result.merged == 0


def test_template_titles_of_different_companies_stay_apart() -> None:
    """Šablonové titulky (J≈0.67) jsou hluboko pod prahem — nesmí splynout."""
    dedup = RollingDeduplicator(window_minutes=10)
    result = dedup.process(
        [
            event("Astrazeneca Q2 Earnings Call Highlights", "rss_yahoo"),
            event(
                "PayPal Q2 Earnings Call Highlights",
                "rss_yahoo",
                at=TS + dt.timedelta(minutes=1),
            ),
        ]
    )
    assert len(result.events) == 2


def test_scheduled_events_never_merge_fuzzy() -> None:
    """Kalendářní položky nejsou reformulace — fuzzy se na ně nepouští vůbec.

    Titulky jsou syntetické s J = 0.9 (9 z 10 tokenů), aby test prokázal
    guard na `kind`, ne jen podprahovou podobnost.
    """
    dedup = RollingDeduplicator(window_minutes=10)
    base = "alpha beta gamma delta epsilon zeta eta theta iota"
    result = dedup.process(
        [
            event(base, "forexfactory", kind="scheduled"),
            event(
                f"{base} kappa",
                "forexfactory",
                at=TS + dt.timedelta(minutes=1),
                kind="scheduled",
            ),
        ]
    )
    assert len(result.events) == 2

    # Kontrolní vzorek: tytéž titulky jako headline splynou
    headline_dedup = RollingDeduplicator(window_minutes=10)
    headline_result = headline_dedup.process(
        [
            event(base, "rss_yahoo"),
            event(f"{base} kappa", "rss_cnbc", at=TS + dt.timedelta(minutes=1)),
        ]
    )
    assert len(headline_result.events) == 1


def test_fuzzy_merge_records_source_for_written_event() -> None:
    """Sloučený zdroj se ukládá k prvnímu výskytu — i při fuzzy shodě."""
    dedup = RollingDeduplicator(window_minutes=10)
    first = event(
        "Medicare is about to change a program that held down the cost of premiums",
        "rss_yahoo",
        at=TS,
        ingested=TS,
    )
    reworded = event(
        "Medicare is about to change a drug program that held down the cost of premiums",
        "rss_cnbc",
        at=TS + dt.timedelta(minutes=2),
        ingested=TS + dt.timedelta(minutes=2),
    )
    dedup.process([first, reworded])

    merged = dedup.merged_sources(first)
    assert len(merged) == 1
    assert merged[0]["source"] == "rss_cnbc"


def test_fuzzy_layer_can_be_disabled() -> None:
    dedup = RollingDeduplicator(window_minutes=10, jaccard_threshold=None)
    result = dedup.process(
        [
            event(
                "SK Hynix second-quarter profit surges 557% to a new high — but misses estimates",
                "rss_yahoo",
            ),
            event(
                "SK Hynix second-quarter profit surges to a new high — but misses estimates",
                "rss_cnbc",
                at=TS + dt.timedelta(minutes=1),
            ),
        ]
    )
    assert len(result.events) == 2


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
