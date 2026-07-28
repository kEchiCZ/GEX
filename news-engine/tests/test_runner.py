"""Testy skeletonu news-engine (#270): kontrakt, izolace chyb, degraded, status."""

import asyncio
import datetime as dt
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import create_engine

from gexlens_engine.storage.sentiment import ensure_sentiment_schema
from gexlens_news.collectors import DEGRADED_AFTER_FAILURES, CollectorHealth
from gexlens_news.model import NewsEvent, RawItem, normalize_title
from gexlens_news.runner import CollectorRunner
from gexlens_news.store import NewsWriter

TS = dt.datetime(2026, 7, 28, 12, 0, tzinfo=dt.UTC)


class FakeCollector:
    """Zdroj s řízeným chováním — počet položek, výjimky, rozbitá normalizace."""

    def __init__(
        self,
        name: str = "fake",
        *,
        items: int = 1,
        fail: bool = False,
        bad_normalize: bool = False,
        interval_s: float = 60.0,
    ) -> None:
        self._name = name
        self.items = items
        self.fail = fail
        self.bad_normalize = bad_normalize
        self._interval_s = interval_s
        self.fetch_calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def interval_s(self) -> float:
        return self._interval_s

    async def fetch(self) -> Sequence[RawItem]:
        self.fetch_calls += 1
        if self.fail:
            raise TimeoutError("zdroj neodpověděl")
        return [
            RawItem(source=self._name, payload={"i": i, "title": f"Titulek {i}"}, fetched_at=TS)
            for i in range(self.items)
        ]

    def normalize(self, item: RawItem) -> NewsEvent | None:
        if self.bad_normalize:
            raise ValueError("nečekaný tvar payloadu")
        return NewsEvent(
            ts_event=TS - dt.timedelta(seconds=30),
            ts_ingested=TS,
            source=self._name,
            kind="headline",
            title=str(item.payload["title"]),
            raw=item.payload,
        )


def collect_written(events: Sequence[NewsEvent]) -> int:
    return len(events)


# ── Kontrakt a normalizace ─────────────────────────────────────────


def test_dedup_hash_ignores_case_punctuation_and_stopwords() -> None:
    """SPEC 3.3: tatáž story z různých zdrojů má mít shodný hash."""
    a = NewsEvent(TS, TS, "finnhub", "headline", "The Fed holds rates!")
    b = NewsEvent(TS, TS, "rss_cnbc", "headline", "Fed holds rates")
    assert a.dedup_hash == b.dedup_hash
    different = NewsEvent(TS, TS, "finnhub", "headline", "Fed cuts rates")
    assert different.dedup_hash != a.dedup_hash
    # Hash je jen z titulku — časové okno řeší rolling dedup (#273), ne hash
    later = NewsEvent(TS + dt.timedelta(hours=5), TS, "finnhub", "headline", "Fed holds rates")
    assert later.dedup_hash == a.dedup_hash


def test_normalize_title_survives_diacritics_and_empty_result() -> None:
    assert normalize_title("Česká národní banka") == "ceska narodni banka"
    # Titulek složený jen ze stopslov nesmí skončit prázdný (kolidoval by se všemi)
    assert normalize_title("the and of") == "the and of"


# ── Izolace chyb a degraded stavy ──────────────────────────────────


async def test_failing_source_never_kills_the_run_and_degrades() -> None:
    """SPEC 3.2/kap. 10: chyba zdroje nesmí shodit engine, jen ho degradovat."""
    broken = FakeCollector("broken", fail=True)
    runner = CollectorRunner([broken], collect_written)

    for _ in range(DEGRADED_AFTER_FAILURES - 1):
        assert await runner.run_once(broken) == 0
        assert runner.health["broken"].state != "degraded"  # jedno selhání nic nehlásí

    assert await runner.run_once(broken) == 0
    health = runner.health["broken"]
    assert health.state == "degraded"
    assert "TimeoutError" in (health.last_error or "")
    assert health.backoff_multiplier > 1  # mrtvý zdroj se neptá každou minutu


async def test_recovery_clears_degraded_state() -> None:
    collector = FakeCollector("flaky", fail=True)
    runner = CollectorRunner([collector], collect_written)
    for _ in range(DEGRADED_AFTER_FAILURES):
        await runner.run_once(collector)
    assert runner.health["flaky"].state == "degraded"

    collector.fail = False
    assert await runner.run_once(collector) == 1
    health = runner.health["flaky"]
    assert health.state == "ok"
    assert health.consecutive_failures == 0
    assert health.backoff_multiplier == 1


async def test_broken_normalize_skips_item_not_batch() -> None:
    """Rozbitá položka nesmí zahodit celou dávku — zbytek musí projít."""
    collector = FakeCollector("mixed", items=3)
    runner = CollectorRunner([collector], collect_written)
    assert await runner.run_once(collector) == 3

    collector.bad_normalize = True
    # Všechny položky selžou na normalizaci → 0 zapsaných, ale sběr je „ok"
    assert await runner.run_once(collector) == 0
    assert runner.health["mixed"].state == "ok"


async def test_writer_failure_is_isolated_too() -> None:
    def exploding(_events: Sequence[NewsEvent]) -> int:
        raise RuntimeError("DB je dole")

    collector = FakeCollector("src")
    runner = CollectorRunner([collector], exploding)
    assert await runner.run_once(collector) == 0
    assert runner.health["src"].consecutive_failures == 1


async def test_one_slow_source_does_not_block_others() -> None:
    """SPEC 3.1: task per zdroj — mrtvý zdroj nesmí zastavit ostatní."""
    healthy = FakeCollector("healthy", interval_s=0.01)
    broken = FakeCollector("broken", fail=True, interval_s=0.01)
    runner = CollectorRunner([healthy, broken], collect_written)

    stop = asyncio.Event()
    task = asyncio.create_task(runner.run(stop=stop))
    await asyncio.sleep(0.05)
    stop.set()
    await task

    assert healthy.fetch_calls > 1
    assert runner.health["healthy"].state == "ok"
    assert runner.health["broken"].last_error is not None


def test_status_puts_degraded_first() -> None:
    runner = CollectorRunner([FakeCollector("a"), FakeCollector("b")], collect_written)
    runner.health["b"] = CollectorHealth(name="b", state="degraded")
    assert [h.name for h in runner.status()] == ["b", "a"]


# ── Zápis ──────────────────────────────────────────────────────────


def test_writer_is_idempotent_on_dedup_hash(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    writer = NewsWriter(engine)

    events = [
        NewsEvent(TS, TS, "finnhub", "headline", "Fed holds rates", symbols=["ES"]),
        NewsEvent(TS, TS, "finnhub", "headline", "ECB signals cut", symbols=["ES"]),
    ]
    assert writer.write(events) == 2
    # Tatáž story z jiného zdroje o minutu později se nezapíše podruhé
    duplicate = NewsEvent(
        TS + dt.timedelta(minutes=1), TS, "rss_cnbc", "headline", "The Fed holds rates"
    )
    assert writer.write([duplicate]) == 0
    assert writer.count() == 2
    assert writer.write([]) == 0
