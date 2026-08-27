"""Uživatelské seznamy zdrojů (#578): seed, čtení, resolve DID, enabled mapa."""

import json
from dataclasses import dataclass

from sqlalchemy import create_engine, insert, select, update

from gexlens_engine.storage.meta import ensure_meta_schema, settings_table
from gexlens_engine.storage.sentiment import ensure_sentiment_schema, seed_news_sources
from gexlens_engine.storage.sentiment import news_sources as news_sources_table
from gexlens_news.user_sources import (
    DEFAULT_BLUESKY_AUTHORS,
    SETTING_BLUESKY_AUTHORS,
    SETTING_REDDIT_SUBREDDITS,
    SETTING_RSS_EXTRA,
    BlueskyAuthorResolver,
    read_list_setting,
    reddit_rss_urls,
    seed_user_source_settings,
    source_enabled_map,
)


def make_db():  # noqa: ANN201 — sqlite fixture
    engine = create_engine("sqlite+pysqlite:///:memory:")
    ensure_meta_schema(engine)
    ensure_sentiment_schema(engine)
    return engine


def test_seed_nevraci_smazane_defaulty() -> None:
    engine = make_db()
    assert seed_user_source_settings(engine) == 3
    # Uživatel promaže defaulty na jediný účet — další seed NIC nevrací
    with engine.begin() as conn:
        conn.execute(
            update(settings_table)
            .where(settings_table.c.key == SETTING_BLUESKY_AUTHORS)
            .values(value=["did:plc:jenjeden"])
        )
    assert seed_user_source_settings(engine) == 0
    assert read_list_setting(engine, SETTING_BLUESKY_AUTHORS) == ["did:plc:jenjeden"]
    # Reddit default zůstal netknutý
    assert read_list_setting(engine, SETTING_REDDIT_SUBREDDITS) == ["wallstreetbets", "stocks"]
    assert read_list_setting(engine, SETTING_RSS_EXTRA) == []
    assert len(DEFAULT_BLUESKY_AUTHORS) >= 5


def test_read_list_setting_hrany() -> None:
    engine = make_db()
    assert read_list_setting(engine, "neexistuje") == []
    with engine.begin() as conn:
        conn.execute(insert(settings_table).values(key="rozbite", value={"ne": "seznam"}))
        conn.execute(insert(settings_table).values(key="mezery", value=["  a ", "", "b"]))
    assert read_list_setting(engine, "rozbite") == []  # cizí tvar → prázdno + log
    assert read_list_setting(engine, "mezery") == ["a", "b"]


def test_source_enabled_map_cte_registr() -> None:
    engine = make_db()
    seed_news_sources(engine)
    with engine.begin() as conn:
        conn.execute(
            update(news_sources_table)
            .where(news_sources_table.c.source == "reddit_rss")
            .values(enabled=False)
        )
    mapping = source_enabled_map(engine)
    assert mapping["reddit_rss"] is False
    assert mapping["bluesky"] is True
    with engine.connect() as conn:
        rows = conn.execute(select(news_sources_table.c.source)).fetchall()
    assert ("rss_user",) in [tuple(row) for row in rows]  # seed řádek pro vlastní feedy


def test_reddit_rss_urls_escapuje() -> None:
    assert reddit_rss_urls(["wallstreetbets", "r/stocks"]) == (
        "https://www.reddit.com/r/wallstreetbets/hot/.rss?limit=25",
        "https://www.reddit.com/r/stocks/hot/.rss?limit=25",
    )


@dataclass
class _Response:
    status: int
    text: str
    not_modified: bool = False


class _FakeFetcher:
    def __init__(self, mapping: dict[str, str | int]) -> None:
        self._mapping = mapping
        self.calls: list[str] = []

    async def get(self, url: str, *, headers: dict[str, str] | None = None) -> _Response:
        self.calls.append(url)
        handle = url.rsplit("=", 1)[-1]
        value = self._mapping.get(handle)
        if isinstance(value, int):
            return _Response(status=value, text="")
        if value is None:
            return _Response(status=400, text="")
        return _Response(status=200, text=json.dumps({"did": value}))


async def test_resolver_did_primo_handle_pres_api_a_cache() -> None:
    fetcher = _FakeFetcher({"cnbc.com": "did:plc:cnbc", "rozbity.example": 400})
    resolver = BlueskyAuthorResolver(fetcher)
    dids = await resolver.resolve(["did:plc:primo", "@cnbc.com", "rozbity.example", " ", ""])
    assert dids == frozenset({"did:plc:primo", "did:plc:cnbc"})
    # Druhé kolo jde z cache — žádné nové HTTP requesty (ani pro selhaný handle)
    calls_before = len(fetcher.calls)
    dids2 = await resolver.resolve(["cnbc.com", "rozbity.example"])
    assert dids2 == frozenset({"did:plc:cnbc"})
    assert len(fetcher.calls) == calls_before
