"""Čas zveřejnění zdrojů tier 1 (ADR-0059 bod 5, #1515).

`RssCollector(verified_time=True)` bere čas položky jen věrohodný, jinak čas
prvního stažení s původem v `raw.ts_source`. Položku bez věrohodného času
z prvního stažení po startu nezapíše a `NewsWriter` nezapíše znovu položku
s časem stažení, kterou zdroj už v DB má.
"""

import datetime as dt
import logging
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select

from gexlens_engine.storage.sentiment import ensure_sentiment_schema, news_events
from gexlens_news.collectors.rss import RssCollector
from gexlens_news.http import Response
from gexlens_news.model import NewsEvent
from gexlens_news.store import NewsWriter

URL = "https://www.federalreserve.gov/feeds/testimony.xml"
START = dt.datetime(2026, 10, 12, 14, 0, tzinfo=dt.UTC)


def _item(uid: str, title: str, pub_date: str | None) -> str:
    pub = f"<pubDate>{pub_date}</pubDate>" if pub_date else ""
    return f"<item><title>{title}</title><guid>{uid}</guid>{pub}</item>"


def _feed(*items: str) -> str:
    return '<?xml version="1.0"?><rss version="2.0"><channel>' + "".join(items) + "</channel></rss>"


#: Výřez živého feedu testimony z 11. 10. 2026 — 3 z 15 položek mají rok 1899
DATED = _item(
    "t-warsh", "Warsh, Semiannual Monetary Policy Report", "Tue, 14 Jul 2026 12:30:00 GMT"
)
Y1899 = _item("t-barr", "Barr, Supervision and Regulation", "Sat, 30 Dec 1899 15:00:00 GMT")
UNDATED = _item("t-new", "Jefferson, Financial Stability", None)
FUTURE = _item("t-future", "Cook, Payments Innovation", "Thu, 01 Jan 2099 12:00:00 GMT")


class SequenceFetcher:
    """Vrací feedy v pořadí volání; poslední se opakuje."""

    def __init__(self, *bodies: str) -> None:
        self._bodies = list(bodies)
        self.calls = 0

    async def get(self, url: str, *, headers: dict[str, str] | None = None) -> Response:
        body = self._bodies[min(self.calls, len(self._bodies) - 1)]
        self.calls += 1
        return Response(200, body)


class Clock:
    def __init__(self, now: dt.datetime) -> None:
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now


def _collector(fetcher: SequenceFetcher, clock: Clock, *, verified: bool = True) -> RssCollector:
    return RssCollector("fed_rss", [URL], fetcher, verified_time=verified, clock=clock)


async def _cycle(collector: RssCollector) -> list[NewsEvent]:
    items = await collector.fetch()
    return [e for e in (collector.normalize(i) for i in items) if e is not None]


async def test_rok_1899_z_prvniho_stazeni_se_nezapise(caplog: pytest.LogCaptureFixture) -> None:
    clock = Clock(START)
    collector = _collector(SequenceFetcher(_feed(DATED, Y1899)), clock)
    with caplog.at_level(logging.WARNING):
        events = await _cycle(collector)
        # Druhý cyklus: tatáž položka se znovu nezapíše a log se neopakuje
        clock.now = START + dt.timedelta(minutes=1)
        again = await _cycle(collector)
    assert [e.source_uid for e in events] == ["t-warsh"]
    assert [e.source_uid for e in again] == ["t-warsh"]
    assert sum("Barr" in r.getMessage() for r in caplog.records) == 1


async def test_verohodny_cas_polozky_je_item() -> None:
    collector = _collector(SequenceFetcher(_feed(DATED)), Clock(START))
    (event,) = await _cycle(collector)
    assert event.ts_event == dt.datetime(2026, 7, 14, 12, 30, tzinfo=dt.UTC)
    assert event.raw["ts_source"] == "item"


async def test_nova_polozka_bez_casu_dostane_cas_prvniho_stazeni() -> None:
    clock = Clock(START)
    fetcher = SequenceFetcher(_feed(DATED), _feed(UNDATED, FUTURE, DATED))
    collector = _collector(fetcher, clock)
    await _cycle(collector)

    first_seen = START + dt.timedelta(minutes=5)
    clock.now = first_seen
    events = {e.source_uid: e for e in await _cycle(collector)}
    for uid in ("t-new", "t-future"):
        assert events[uid].ts_event == first_seen
        assert events[uid].ts_ingested == first_seen
        assert events[uid].raw["ts_source"] == "fetch"

    # Feed se změní jinde: čas zůstává z prvního stažení, `dedup_hash` taky
    clock.now = first_seen + dt.timedelta(hours=30)
    later = {e.source_uid: e for e in await _cycle(collector)}
    assert later["t-new"].ts_event == first_seen
    assert later["t-new"].dedup_hash == events["t-new"].dedup_hash


async def test_zestarly_budouci_cas_nezmeni_cas_stazeni_ani_po_restartu(tmp_path: Path) -> None:
    """Položka s budoucím časem dostane čas stažení; když čas zestárne, nezapíše se znovu."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    writer = NewsWriter(engine)
    soon = _item(
        "t-soon", "Lagarde, Hearing at the European Parliament", "Tue, 13 Oct 2026 00:30:00 GMT"
    )

    clock = Clock(START)
    first = _collector(SequenceFetcher(_feed(DATED), _feed(soon, DATED)), clock)
    writer.write(await _cycle(first))
    clock.now = START + dt.timedelta(minutes=1)  # 12. 10. 14:01, položka hlásí 13. 10.
    (fetched,) = [e for e in await _cycle(first) if e.source_uid == "t-soon"]
    assert fetched.raw["ts_source"] == "fetch"
    assert writer.write([fetched]) == 1

    # Týž proces o den později: čas položky už je minulý, platí ale čas stažení
    clock.now = START + dt.timedelta(days=1)
    (again,) = [e for e in await _cycle(first) if e.source_uid == "t-soon"]
    assert (again.ts_event, again.raw["ts_source"]) == (fetched.ts_event, "fetch")

    # Nový proces: položka chyběla v prvním stažení, pak přišla s věrohodným časem
    second = _collector(SequenceFetcher(_feed(DATED), _feed(soon, DATED)), clock)
    await _cycle(second)
    (item_time,) = [e for e in await _cycle(second) if e.source_uid == "t-soon"]
    assert item_time.raw["ts_source"] == "item"
    assert item_time.dedup_hash != fetched.dedup_hash  # jiný den v `ts_event`
    assert writer.write([item_time]) == 0


async def test_budouci_cas_v_prvnim_stazeni_se_nezapise() -> None:
    collector = _collector(SequenceFetcher(_feed(FUTURE, DATED)), Clock(START))
    assert [e.source_uid for e in await _cycle(collector)] == ["t-warsh"]


async def test_prvni_stazeni_se_pocita_az_po_uspechu() -> None:
    """Výpadek feedu při startu: první úspěšné stažení je teprve to další."""

    class FlakyFetcher(SequenceFetcher):
        async def get(self, url: str, *, headers: dict[str, str] | None = None) -> Response:
            if self.calls == 0:
                self.calls += 1
                raise TimeoutError("neodpovídá")
            return await super().get(url, headers=headers)

    clock = Clock(START)
    collector = _collector(FlakyFetcher(_feed(UNDATED), _feed(UNDATED)), clock)
    with pytest.raises(RuntimeError):
        await collector.fetch()
    assert await _cycle(collector) == []


async def test_ostatni_rss_beze_zmeny() -> None:
    """Bez `verified_time` platí dál `published or fetched_at` (#552 L2)."""
    collector = _collector(
        SequenceFetcher(_feed(Y1899, FUTURE, UNDATED)), Clock(START), verified=False
    )
    events = {e.source_uid: e for e in await _cycle(collector)}
    assert events["t-barr"].ts_event.year == 1899
    assert events["t-future"].ts_event.year == 2099
    assert events["t-new"].ts_event == START
    assert all("ts_source" not in e.raw for e in events.values())


async def test_restart_v_jiny_den_polozku_s_casem_stazeni_nezapise_znovu(tmp_path: Path) -> None:
    """Položka chyběla v prvním stažení po restartu a pak se vrátila (výpadek feedu)."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    writer = NewsWriter(engine)

    clock = Clock(START)
    first = _collector(SequenceFetcher(_feed(DATED), _feed(UNDATED, DATED)), clock)
    assert writer.write(await _cycle(first)) == 1  # t-warsh
    clock.now = START + dt.timedelta(minutes=1)
    assert writer.write(await _cycle(first)) == 1  # t-new s časem stažení

    # Nový proces o den později: feed položku v prvním stažení nenese
    clock.now = START + dt.timedelta(days=1)
    second = _collector(SequenceFetcher(_feed(DATED), _feed(UNDATED, DATED)), clock)
    assert writer.write(await _cycle(second)) == 0
    clock.now = START + dt.timedelta(days=1, minutes=1)
    events = await _cycle(second)
    assert any(e.source_uid == "t-new" and e.ts_event.day == 13 for e in events)
    assert writer.write(events) == 0

    with engine.connect() as conn:
        rows = conn.execute(
            select(news_events.c.ts_event).where(news_events.c.source_uid == "t-new")
        ).all()
    assert len(rows) == 1


async def test_build_collectors_zapina_cas_zverejneni_u_zdroju_tier_1() -> None:
    from gexlens_news.__main__ import build_collectors
    from gexlens_news.config import NewsSettings

    settings = NewsSettings(_env_file=None, finnhub_api_key="")  # type: ignore[call-arg]
    feed = _feed(DATED, Y1899)
    collectors = {
        c.name: c
        for c in build_collectors(settings, SequenceFetcher(feed), reddit_subreddits=[])
        if isinstance(c, RssCollector)
    }
    assert {"fed_rss", "ecb", "whitehouse"} <= collectors.keys()
    for name in ("fed_rss", "ecb", "whitehouse"):
        events = await _cycle(collectors[name])
        # Každý feed zdroje vrací týž výřez: rok 1899 se nezapíše, čas položky ano
        assert {e.source_uid for e in events} == {"t-warsh"}, name
        assert all(e.raw["ts_source"] == "item" for e in events), name
    # Ostatní RSS beze změny
    rss_news = await _cycle(collectors["rss_news"])
    assert {e.source_uid for e in rss_news} == {"t-warsh", "t-barr"}

    disabled = build_collectors(
        settings, SequenceFetcher(feed), enabled={"ecb": False, "whitehouse": False}
    )
    assert not {"ecb", "whitehouse"} & {c.name for c in disabled}
