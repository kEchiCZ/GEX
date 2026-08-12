"""S10 (#553): raw payloady se čistí od tokenů PŘED zápisem do DB.

Dosud existovalo jen čištění chybových hlášek (`CollectorHealth.last_error`);
tady se testuje zápisová cesta obou tabulek — `news_events.raw` (NewsWriter)
i `crowd_sentiment.raw` (CrowdWriter) — a rozšíření masky na token v cestě URL.
"""

import datetime as dt
import json
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select

from gexlens_engine.storage.sentiment import crowd_sentiment, ensure_sentiment_schema, news_events
from gexlens_news.crowd import CrowdPoint, CrowdWriter
from gexlens_news.http import sanitize_raw, strip_secrets
from gexlens_news.model import NewsEvent
from gexlens_news.store import NewsWriter

TS = dt.datetime(2026, 8, 12, 14, 0, tzinfo=dt.UTC)

# Tvar tokenu (21 znaků [A-Za-z0-9_], bez pomlček/teček), skládaný za běhu —
# literál s vysokou entropií by flagoval gitleaks v CI jako uniklé tajemství
TOKEN = "Ab0" * 7


# ── strip_secrets: query + path ────────────────────────────────────


def test_strip_secrets_query_parametry() -> None:
    masked = strip_secrets(f"https://ex.com/rss?apikey={TOKEN}&x=1")
    assert TOKEN not in masked
    assert "apikey=***" in masked


def test_strip_secrets_token_v_ceste() -> None:
    # `/feed/<token>.xml` — přesně případ z S10
    masked = strip_secrets(f"https://ex.com/feed/{TOKEN}.xml")
    assert TOKEN not in masked
    assert masked == "https://ex.com/feed/***.xml"


def test_strip_secrets_nemrzaci_slug_ani_host() -> None:
    # Slug má pomlčky, host tečky — obojí mimo masku; text bez :// se nemění
    slug = "https://ex.com/my-very-long-article-title-2026/index.html"
    assert strip_secrets(slug) == slug
    host = "https://veryverylongsubdomainname.example.com/feed.xml"
    assert strip_secrets(host) == host
    plain = f"headline se slovem {TOKEN} bez URL"
    assert strip_secrets(plain) == plain


# ── sanitize_raw: rekurze, imutabilita ─────────────────────────────


def test_sanitize_raw_rekurzivne_a_bez_mutace() -> None:
    raw: dict[str, Any] = {
        "feed": f"https://ex.com/rss?token={TOKEN}",
        "items": [{"link": f"https://ex.com/feed/{TOKEN}.xml"}, 42, None],
        "count": 7,
    }
    cleaned = sanitize_raw(raw)
    assert TOKEN not in json.dumps(cleaned)
    assert cleaned["count"] == 7
    assert cleaned["items"][1] == 42
    # Vstup zůstal nedotčený (collector s ním může dál pracovat)
    feed: str = raw["feed"]
    assert TOKEN in feed


# ── zápisové cesty: token nesmí do DB ──────────────────────────────


def test_news_writer_cisti_raw(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    event = NewsEvent(
        ts_event=TS,
        ts_ingested=TS,
        source="rss_news",
        kind="headline",
        title="Fed holds rates",
        source_uid="rss-1",
        raw={
            "feed": f"https://ex.com/rss?token={TOKEN}",
            "link": f"https://ex.com/feed/{TOKEN}.xml",
        },
    )
    assert NewsWriter(engine).write([event]) == 1
    with engine.connect() as conn:
        stored = conn.execute(select(news_events.c.raw)).scalar_one()
    assert TOKEN not in json.dumps(stored)
    assert "token=***" in stored["feed"]


def test_crowd_writer_cisti_raw(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'crowd.sqlite'}")
    ensure_sentiment_schema(engine)
    point = CrowdPoint(
        ts=TS,
        source="stocktwits",
        metric="bull_ratio",
        value=0.61,
        raw={"url": f"https://api.ex.com/feed?api_key={TOKEN}"},
    )
    assert CrowdWriter(engine).write([point]) == 1
    with engine.connect() as conn:
        stored = conn.execute(select(crowd_sentiment.c.raw)).scalar_one()
    assert TOKEN not in json.dumps(stored)
