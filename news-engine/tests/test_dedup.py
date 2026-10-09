"""Testy deduplikace (#273, #274): rolling okno, kopie z jiných zdrojů, fuzzy, priming.

Kopie z jiného zdroje se ukládají do `news_event_sources` (ADR-0059 bod 3, #1489).
"""

import datetime as dt
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Row

from gexlens_engine.ibkr.newsticks import NewsTickCollector
from gexlens_engine.storage.sentiment import (
    ensure_sentiment_schema,
    news_event_sources,
    news_events,
)
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
    raw: dict[str, object] | None = None,
) -> NewsEvent:
    return NewsEvent(
        ts_event=at,
        ts_ingested=ingested or at,
        source=source,
        kind=kind,
        title=title,
        source_uid=f"{source}-{title}",
        raw=raw or {},
    )


# ── Rolling okno ───────────────────────────────────────────────────


def test_rolling_window_beats_fixed_buckets_across_the_boundary() -> None:
    """Jádro #273: 13:59:58 a 14:00:01 patří k sobě, buckety by je rozdělily."""
    dedup = RollingDeduplicator(window_minutes=10)
    before = event("Fed holds rates", "finnhub", at=TS)
    after = event("Fed holds rates", "rss_news", at=TS + dt.timedelta(seconds=3))

    result = dedup.process([before, after])
    assert [e.source for e in result.events] == ["finnhub"]
    assert len(result.copies) == 1


def test_same_story_outside_window_is_a_new_event() -> None:
    dedup = RollingDeduplicator(window_minutes=10)
    first = event("Stocks close higher", "finnhub", at=TS)
    much_later = event("Stocks close higher", "rss_news", at=TS + dt.timedelta(minutes=45))

    result = dedup.process([first, much_later])
    assert len(result.events) == 2  # po 45 minutách je to nová zpráva
    assert result.copies == []


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
    assert result.copies == []


def test_normalized_title_matches_across_wording() -> None:
    dedup = RollingDeduplicator(window_minutes=10)
    result = dedup.process(
        [
            event("The Fed holds rates!", "finnhub", at=TS),
            event("Fed holds rates", "rss_news", at=TS + dt.timedelta(seconds=30)),
        ]
    )
    assert len(result.events) == 1
    assert len(result.copies) == 1


def test_copy_carries_event_and_first_hash() -> None:
    """Kopie nese vlastní event (zdroj, časy) a hash prvního výskytu pro zápis."""
    dedup = RollingDeduplicator(window_minutes=10)
    fast = event("Payrolls beat", "finnhub", at=TS, ingested=TS)
    slow = event(
        "Payrolls beat",
        "rss_news",
        at=TS + dt.timedelta(seconds=5),
        ingested=TS + dt.timedelta(seconds=42),
    )
    result = dedup.process([fast, slow])

    assert [c.event for c in result.copies] == [slow]
    assert result.copies[0].first_hash == fast.dedup_hash


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
    assert len(result.copies) == 1


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
    assert len(result.copies) == 1


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
    assert result.copies == []


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


def test_fuzzy_copy_points_to_first_occurrence() -> None:
    """Kopie míří na první výskyt i při fuzzy shodě (jiný titulek, jiný hash)."""
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
    result = dedup.process([first, reworded])

    assert [c.event.source for c in result.copies] == ["rss_cnbc"]
    assert result.copies[0].first_hash == first.dedup_hash != reworded.dedup_hash


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


def stored_events(inner: NewsWriter) -> list[Row[tuple[int, str]]]:
    with inner._engine.connect() as conn:  # noqa: SLF001 — kontrola uloženého tvaru
        return list(conn.execute(select(news_events.c.id, news_events.c.source)))


def stored_copies(inner: NewsWriter) -> list[Row[tuple[int, str]]]:
    with inner._engine.connect() as conn:  # noqa: SLF001 — kontrola uloženého tvaru
        return list(conn.execute(select(news_event_sources).order_by(news_event_sources.c.source)))


NEWSDESK: dict[str, object] = {"author": "Benzinga Newsdesk"}


def test_copy_from_second_batch_is_recorded_with_its_tier_and_times(tmp_path: Path) -> None:
    """Jádro #1489: každý collector zapisuje vlastní dávku — kopie z druhé dávky
    se uloží k prvnímu doručení (dřív končila jen v paměti, audit #1473 3.2).
    """
    writer, inner = make_writer(tmp_path)
    assert writer.write([event("Fed holds rates", "rss_news", at=TS, ingested=TS)]) == 1
    copy_at = TS - dt.timedelta(seconds=1)  # Benzinga publikovala dřív, přišla později
    copy_in = TS + dt.timedelta(seconds=20)
    newsdesk = event("Fed holds rates", "alpaca", at=copy_at, ingested=copy_in, raw=NEWSDESK)
    assert writer.write([newsdesk]) == 0

    [first] = stored_events(inner)
    assert first.source == "rss_news"  # první doručení zůstává, ts_event se nepřepisuje
    [copy] = stored_copies(inner)
    assert copy.event_id == first.id
    assert (copy.source, copy.source_uid, copy.content_tier) == (
        "alpaca",
        "alpaca-Fed holds rates",
        2,
    )
    assert copy.published_at.replace(tzinfo=dt.UTC) == copy_at
    assert copy.fetched_at.replace(tzinfo=dt.UTC) == copy_in


def test_fuzzy_copy_from_another_batch_is_recorded(tmp_path: Path) -> None:
    writer, inner = make_writer(tmp_path)
    writer.write(
        [
            event(
                "Medicare is about to change a program that held down the cost of premiums",
                "rss_news",
            )
        ]
    )
    writer.write(
        [
            event(
                "Medicare is about to change a drug program that held down the cost of premiums",
                "finnhub",
                at=TS + dt.timedelta(minutes=2),
            )
        ]
    )
    [first] = stored_events(inner)
    assert [(c.event_id, c.source, c.content_tier) for c in stored_copies(inner)] == [
        (first.id, "finnhub", 3)
    ]


def test_copies_are_idempotent_and_same_source_leaves_no_row(tmp_path: Path) -> None:
    """Opakovaná kopie zdroje nic nepřidá (PK event × zdroj); týž zdroj = duplicita."""
    writer, inner = make_writer(tmp_path)
    writer.write([event("Oil spikes", "rss_news", at=TS)])
    writer.write([event("Oil spikes", "rss_news", at=TS + dt.timedelta(minutes=1))])
    assert stored_copies(inner) == []

    first_copy_in = TS + dt.timedelta(minutes=2)
    writer.write([event("Oil spikes", "finnhub", at=TS, ingested=first_copy_in)])
    writer.write([event("Oil spikes", "finnhub", at=TS, ingested=TS + dt.timedelta(minutes=9))])
    [copy] = stored_copies(inner)
    assert copy.fetched_at.replace(tzinfo=dt.UTC) == first_copy_in  # platí nejdřívější


def test_priming_from_db_prevents_duplicates_after_restart(tmp_path: Path) -> None:
    """Po restartu je okno prázdné — bez priming by se čerstvý sběr zapsal znovu."""
    writer, inner = make_writer(tmp_path)
    assert writer.write([event("Breaking story", "finnhub", at=TS)]) == 1

    # Nový proces nad toutéž DB
    restarted = DedupingWriter(inner, window_minutes=10)
    primed = restarted.prime_from_db(TS + dt.timedelta(minutes=2))
    assert primed == 1

    # Tatáž story z jiného zdroje je teď kopie prvního doručení z DB
    assert restarted.write([event("Breaking story", "rss_news", at=TS)]) == 0
    assert inner.count() == 1
    [first] = stored_events(inner)
    assert [(c.event_id, c.source) for c in stored_copies(inner)] == [(first.id, "rss_news")]


def test_dedup_hash_still_guards_against_double_write(tmp_path: Path) -> None:
    """Poslední pojistka: i kdyby okno selhalo, UNIQUE v DB zápis nepustí —
    a kopii z jiného zdroje zaznamená (ADR-0059 bod 3).
    """
    writer, inner = make_writer(tmp_path)
    assert writer.write([event("Guarded", "finnhub", at=TS)]) == 1
    # Obejití dedupu (jiná instance bez okna) — DB duplicitu odmítne sama
    assert inner.write([event("Guarded", "rss_news", at=TS)]) == 0
    assert inner.count() == 1
    [first] = stored_events(inner)
    assert [(c.event_id, c.source) for c in stored_copies(inner)] == [(first.id, "rss_news")]
    # Týž zdroj jako první doručení = opakovaný fetch, bez záznamu
    assert inner.write([event("Guarded", "finnhub", at=TS)]) == 0
    assert len(stored_copies(inner)) == 1


def test_same_day_copy_outside_window_is_recorded_via_dedup_hash(tmp_path: Path) -> None:
    """Republikace po 7 h téhož dne: okno (6 h) ji nevidí, zachytí ji `dedup_hash`."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    inner = NewsWriter(engine)
    writer = DedupingWriter(inner)
    morning = dt.datetime(2026, 10, 9, 6, 0, tzinfo=dt.UTC)
    assert writer.write([event("Treasury yields climb", "rss_news", at=morning)]) == 1
    later = morning + dt.timedelta(hours=7)
    assert writer.write([event("Treasury yields climb", "finnhub", at=later)]) == 0
    [first] = stored_events(inner)
    assert [(c.event_id, c.source) for c in stored_copies(inner)] == [(first.id, "finnhub")]


def test_copy_without_stored_first_delivery_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Selhal-li zápis prvního doručení, kopie se nezapíše a jde do logu."""
    _, inner = make_writer(tmp_path)
    unwritten = RollingDeduplicator(window_minutes=10).process(
        [event("Lost story", "rss_news"), event("Lost story", "finnhub")]
    )
    with caplog.at_level(logging.WARNING):
        assert inner.write_copies(unwritten.copies) == 0
    assert "bez prvního doručení" in caplog.text
    assert stored_copies(inner) == []


def test_news_copy_of_ibkr_tape_is_recorded(tmp_path: Path) -> None:
    """IBKR pásku zapisuje engine mimo rolling dedup; zpráva z news-engine se
    shodným titulkem téhož dne je kopie prvního doručení z pásky (ADR-0059 bod 3).
    """
    writer, inner = make_writer(tmp_path)
    tape = NewsTickCollector(inner._engine)  # noqa: SLF001 — sdílená DB obou procesů
    tick = SimpleNamespace(
        headline="!BRFG Fed holds rates",
        providerCode="BRFG",
        articleId="b1",
        timeStamp=int(TS.timestamp()),
        extraData="",
    )
    [stored] = tape.write([tick], now=TS)

    newsdesk = event(
        "Fed holds rates",
        "alpaca",
        at=TS + dt.timedelta(seconds=1),
        ingested=TS + dt.timedelta(seconds=2),
        raw=NEWSDESK,
    )
    assert writer.write([newsdesk]) == 0
    assert [(c.event_id, c.source, c.content_tier) for c in stored_copies(inner)] == [
        (stored.id, "alpaca", 2)
    ]
