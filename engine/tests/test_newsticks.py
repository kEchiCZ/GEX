"""Testy broker headlines z ticku 292 (#291): normalizace, dedup, zápis."""

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select

from gexlens_engine.ibkr.newsticks import (
    NewsTickCollector,
    clean_headline,
    normalize_tick,
    tick_time,
)
from gexlens_engine.storage.sentiment import ensure_sentiment_schema, news_events

NOW = dt.datetime(2026, 7, 28, 14, 0, tzinfo=dt.UTC)


@dataclass
class FakeTick:
    """Zrcadlí `ib_async.NewsTick`; `timeStamp` smí chybět (starší providery)."""

    headline: str
    providerCode: str = "DJ-RTG"
    articleId: str = "a1"
    timeStamp: Any = int(NOW.timestamp())
    extraData: str = ""


# ── Normalizace ────────────────────────────────────────────────────


def test_clean_headline_strips_provider_markup() -> None:
    """IBKR posílá interní značky — do DB patří text, jinak se dedup nesejde."""
    assert clean_headline("!DJ-RTG Fed holds rates steady") == "Fed holds rates steady"
    assert clean_headline("{A:1}Stocks rally") == "Stocks rally"
    assert clean_headline("{A:1}{B:2}Double prefix") == "Double prefix"
    assert clean_headline("  Plain headline  ") == "Plain headline"
    assert clean_headline("!ONLYPROVIDER") == ""


def test_tick_time_handles_epoch_seconds_and_millis() -> None:
    """IBKR posílá epoch int, ne datetime — a jednotka se liší per provider.

    Špatně určená jednotka by zprávu posunula o desítky let, takže by vypadla
    z osy grafu i z reakčních oken.
    """
    epoch_s = int(NOW.timestamp())
    assert tick_time(epoch_s, now=NOW) == NOW
    assert tick_time(epoch_s * 1000, now=NOW) == NOW
    # Nečitelné hodnoty padají na čas přijetí, ne na výjimku
    assert tick_time(None, now=NOW) == NOW
    assert tick_time(0, now=NOW) == NOW
    assert tick_time("nesmysl", now=NOW) == NOW
    # Naivní datetime se doplní na UTC
    assert tick_time(NOW.replace(tzinfo=None), now=NOW) == NOW


def test_normalize_tick_falls_back_to_receive_time() -> None:
    """Bez timestampu je lepší čas přijetí než zahozená zpráva."""
    headline = normalize_tick(FakeTick("Fed holds", timeStamp=None), now=NOW)
    assert headline is not None
    assert headline.ts_event == NOW
    assert headline.source == "ibkr_dj-rtg"


def test_normalize_tick_skips_empty_headline() -> None:
    assert normalize_tick(FakeTick(""), now=NOW) is None
    assert normalize_tick(FakeTick("!DJ-RTG"), now=NOW) is None


# ── Zápis ──────────────────────────────────────────────────────────


def make(tmp_path: Path) -> tuple[NewsTickCollector, object]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    return NewsTickCollector(engine), engine


def test_writes_broker_headlines_once(tmp_path: Path) -> None:
    collector, engine = make(tmp_path)
    ticks = [FakeTick("!DJ-RTG Fed holds rates", articleId="x1")]

    assert collector.write(ticks, now=NOW) == 1
    # IBKR seznam je kumulativní — druhé čtení téhož ticku nesmí zapsat znovu
    assert collector.write(ticks, now=NOW) == 0
    assert collector.count() == 1

    with engine.connect() as conn:  # type: ignore[attr-defined]
        row = conn.execute(select(news_events)).fetchone()
    assert row is not None
    assert row.kind == "broker"
    assert row.source == "ibkr_dj-rtg"
    assert row.title == "Fed holds rates"
    assert row.source_uid == "x1"
    # Kategorii a důležitost doplní klasifikátor v news-engine
    assert row.category is None
    assert row.raw["provider"] == "DJ-RTG"


def test_same_story_from_rss_and_broker_is_one_row(tmp_path: Path) -> None:
    """Sdílený dedup_hash: tatáž zpráva přijatá dvěma cestami je jeden záznam."""
    collector, engine = make(tmp_path)
    collector.write([FakeTick("!DJ-RTG Fed holds rates", articleId="x1")], now=NOW)

    # Simulace téže story z RSS (jiný zdroj, jiné uid, stejný titulek a den)
    from gexlens_engine.compute.newstext import dedup_hash

    with engine.begin() as conn:  # type: ignore[attr-defined]
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        stmt = (
            sqlite_insert(news_events)
            .values(
                ts_event=NOW,
                ts_ingested=NOW,
                source="rss_news",
                kind="headline",
                title="The Fed holds rates",
                symbols=[],
                market_closed=False,
                dedup_hash=dedup_hash("The Fed holds rates", NOW),
                raw={},
            )
            .on_conflict_do_nothing(index_elements=[news_events.c.dedup_hash])
        )
        conn.execute(stmt)

    with engine.connect() as conn:  # type: ignore[attr-defined]
        total = conn.execute(select(news_events)).fetchall()
    assert len(total) == 1  # stopslovo „The" hash nemění


def test_ticks_without_article_id_dedup_by_hash(tmp_path: Path) -> None:
    collector, _ = make(tmp_path)
    ticks = [FakeTick("Breaking story", articleId="")]
    assert collector.write(ticks, now=NOW) == 1
    assert collector.write(ticks, now=NOW) == 0


def test_empty_input_is_noop(tmp_path: Path) -> None:
    collector, _ = make(tmp_path)
    assert collector.write([], now=NOW) == 0
    assert collector.write([FakeTick("")], now=NOW) == 0
