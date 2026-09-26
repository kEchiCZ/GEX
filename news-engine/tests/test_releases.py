"""Ohlášené releasy (#1296): řady, headline shluku, rodina, znaménko překvapení, FF dopad."""

import datetime as dt
from pathlib import Path

from sqlalchemy import create_engine, insert

from gexlens_engine.storage.sentiment import ensure_sentiment_schema, news_events
from gexlens_news.releases import (
    ReleaseEvent,
    cluster_releases,
    family_of,
    impact_of,
    load_releases,
)

UTC = dt.UTC
T = dt.datetime(2026, 9, 11, 12, 30, tzinfo=UTC)


def event(
    event_id: int,
    title: str,
    *,
    impact: str | None = "high",
    at: dt.datetime = T,
    forecast: float | None = None,
    actual: float | None = None,
) -> ReleaseEvent:
    return ReleaseEvent(
        id=event_id, ts=at, title=title, impact=impact, forecast=forecast, actual=actual
    )


def headline_and_family(*titles: str) -> tuple[str, str | None]:
    (cluster,) = cluster_releases(event(i, title) for i, title in enumerate(titles, 1))
    return cluster.headline.series, cluster.family


def test_headline_a_rodina_shluku() -> None:
    assert headline_and_family(
        "USD CPI y/y", "USD CPI m/m", "USD Core CPI m/m", "USD Core CPI y/y"
    ) == ("Core CPI m/m", "CPI")
    assert headline_and_family("USD Unemployment Claims", "USD Retail Sales m/m") == (
        "Retail Sales m/m",
        "RETAIL",
    )
    assert headline_and_family("USD Retail Sales m/m", "USD Core PPI m/m") == (
        "Core PPI m/m",
        "PPI",
    )
    assert headline_and_family("USD FOMC Statement", "USD Federal Funds Rate") == (
        "Federal Funds Rate",
        "FOMC",
    )
    assert headline_and_family("USD Average Hourly Earnings m/m", "USD Unemployment Rate") == (
        "Unemployment Rate",
        "NFP",
    )
    assert headline_and_family("USD Advance GDP q/q", "USD Unemployment Claims")[1] is None
    assert family_of("Neznámá řada") is None


def test_shluk_po_minute_a_jen_high_medium() -> None:
    clusters = cluster_releases(
        [
            event(1, "USD Core PPI m/m", at=T + dt.timedelta(seconds=3)),
            event(2, "USD PPI m/m", impact="medium"),
            event(3, "USD Building Permits", impact="low"),
            event(4, "USD ISM Services PMI", at=T + dt.timedelta(minutes=90)),
        ]
    )
    assert [cluster.ts for cluster in clusters] == [T, T + dt.timedelta(minutes=90)]
    assert [item.id for item in clusters[0].events] == [1, 2]
    assert clusters[0].has_high
    # High před Medium u téže řady
    (same,) = cluster_releases(
        [event(5, "USD CPI m/m", impact="medium"), event(6, "USD CPI m/m", impact="high")]
    )
    assert same.headline.id == 6


def test_znamenko_prekvapeni_s_polaritou() -> None:
    assert event(1, "USD Core CPI m/m", forecast=0.3, actual=0.4).surprise_sign() == 1
    assert event(1, "USD Core CPI m/m", forecast=0.3, actual=0.2).surprise_sign() == -1
    assert event(1, "USD Core CPI m/m", forecast=0.3, actual=0.3).surprise_sign() == 0
    # Vyšší nezaměstnanost = slabší ekonomika
    assert event(1, "USD Unemployment Rate", forecast=4.2, actual=4.3).surprise_sign() == -1
    assert event(1, "USD Core CPI m/m", forecast=None, actual=0.3).surprise_sign() is None
    assert event(1, "USD Neznámá", forecast=1.0, actual=2.0).surprise_sign() is None


def test_impact_z_raw() -> None:
    assert impact_of({"impact": "High"}) == "high"
    assert impact_of({"impactName": "medium"}) == "medium"
    assert impact_of({"impactName": "high", "impact": "Low"}) == "high"
    assert impact_of({}) is None
    assert impact_of(None) is None


def test_load_releases_z_db(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)

    def row(
        event_id: int, title: str, raw: dict[str, str], kind: str = "scheduled"
    ) -> dict[str, object]:
        return {
            "id": event_id,
            "ts_event": T,
            "ts_ingested": T - dt.timedelta(days=2),
            "source": "forexfactory",
            "kind": kind,
            "title": title,
            "symbols": [],
            "market_closed": False,
            "dedup_hash": f"h{event_id}",
            "forecast": 0.3,
            "raw": raw,
        }

    with engine.begin() as conn:
        conn.execute(
            insert(news_events),
            [
                row(1, "USD Core CPI m/m", {"impact": "High", "forecast": "0.3%"}),
                row(2, "USD CPI y/y", {"impactName": "medium"}),
                row(3, "USD Building Permits", {"impact": "Low"}),
                row(4, "EUR CPI m/m", {"impact": "High"}),
                row(5, "USD Core CPI m/m", {"impact": "High"}, kind="headline"),
            ],
        )
    events = load_releases(engine, T - dt.timedelta(minutes=1), T + dt.timedelta(minutes=1))
    assert [item.id for item in events] == [1, 2]
    assert events[0].forecast_text == "0.3%" and events[0].forecast == 0.3
    assert events[0].ts.tzinfo is not None
