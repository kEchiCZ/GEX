"""BLS releasy s časem z kalendáře FF (E-6.25c, #1516, ADR-0059 bod 5 a 7).

`ts_event` položky BLS je čas releasu z řádku kalendáře téhož dne v ET
s přesným titulkem podle mapování; čas položky (39 min před releasem) se
nepoužije nikdy. Bez shody platí čas prvního stažení s pojistkami E-6.25a.
Kontakt v User-Agent dostane jen BLS, převedený na ASCII.
"""

import datetime as dt
import functools
import logging
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import ensure_sentiment_schema, news_events
from gexlens_news.classifier import BLS_RELEASE_CLASSES, bls_release
from gexlens_news.collectors.bls import (
    BLS_RELEASE_TITLES,
    BLS_RSS_URLS,
    bls_release_time,
    release_day,
)
from gexlens_news.collectors.rss import RssCollector
from gexlens_news.http import Response, contact_user_agent
from gexlens_news.model import NewsEvent, RawItem
from gexlens_news.store import NewsWriter, calendar_release_time

CPI = "https://www.bls.gov/feed/cpi.rss"
EMPSIT = "https://www.bls.gov/feed/empsit.rss"


def _entry(uid: str, title: str, published: str) -> str:
    return (
        f"<entry><title>{title}</title><link href='https://www.bls.gov/{uid}.htm'/>"
        f"<id>{uid}</id><published>{published}</published><updated>{published}</updated>"
        "<category>News Release</category></entry>"
    )


def _atom(*entries: str) -> str:
    """Tvar živého feedu BLS (Atom, 11. 10. 2026)."""
    return (
        "<?xml version='1.0' encoding='UTF-8'?><feed xmlns=\"http://www.w3.org/2005/Atom\">"
        "<id>bls.gov:feed</id><title>BLS</title><updated>2026-10-02T07:51:08-04:00</updated>"
        + "".join(entries)
        + "</feed>"
    )


#: Výřezy živých feedů BLS z 11. 10. 2026 — čas položky 39 min před releasem
CPI_SEP = _entry(
    "cpi-2026_09_11__07_50_40",
    "CPI for all items increases 0.4% in August; gasoline rises",
    "2026-09-11T07:50:40.968-04:00",
)
CPI_JAN = _entry(
    "cpi-2026_01_13__07_50_31",
    "CPI for all items rises 0.3% in December; shelter and food up",
    "2026-01-13T07:50:31.120-05:00",
)
NFP_OCT = _entry(
    "empsit-2026_10_02__07_51_08",
    "Both payroll employment (+29,000) and unemployment rate (4.2%) change little in September",
    "2026-10-02T07:51:08.289-04:00",
)
NFP_SEP = _entry(
    "empsit-2026_09_04__07_51_08",
    "Payroll employment increases by 162,000 in August; unemployment rate unchanged at 4.1%",
    "2026-09-04T07:51:08.695-04:00",
)
#: Den po NFP 2. 10. (pátek) 12:31 UTC — release proběhl ve 12:30
RELEASED = dt.datetime(2026, 10, 2, 12, 31, tzinfo=dt.UTC)


class FeedFetcher:
    """Vrací feedy podle URL v pořadí volání (poslední se opakuje) a pamatuje si hlavičky."""

    def __init__(self, feeds: dict[str, list[str]]) -> None:
        self._feeds = feeds
        self._calls: dict[str, int] = {}
        self.headers: dict[str, dict[str, str] | None] = {}

    async def get(self, url: str, *, headers: dict[str, str] | None = None) -> Response:
        self.headers[url] = headers
        bodies = self._feeds.get(url) or [_atom()]
        call = self._calls.get(url, 0)
        self._calls[url] = call + 1
        return Response(200, bodies[min(call, len(bodies) - 1)])


class Clock:
    def __init__(self, now: dt.datetime) -> None:
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    return engine


def _ff(engine: Engine, title: str, ts_event: dt.datetime) -> None:
    """Řádek kalendáře FF, jak ho zapisuje `ForexFactoryCollector`."""
    NewsWriter(engine).write(
        [
            NewsEvent(
                ts_event=ts_event,
                ts_ingested=ts_event - dt.timedelta(days=5),
                source="forexfactory",
                source_uid=f"{title}|{ts_event.isoformat()}",
                kind="scheduled",
                title=title,
            )
        ]
    )


def _bls(engine: Engine, fetcher: FeedFetcher, clock: Clock, urls: list[str]) -> RssCollector:
    return RssCollector(
        "bls",
        urls,
        fetcher,
        release_time=bls_release_time(functools.partial(calendar_release_time, engine)),
        clock=clock,
    )


async def _cycle(collector: RssCollector) -> list[NewsEvent]:
    items = await collector.fetch()
    return [e for e in (collector.normalize(i) for i in items) if e is not None]


async def test_cpi_v_8_30_et_je_12_30_utc_v_lete_a_13_30_utc_v_zime(engine: Engine) -> None:
    _ff(engine, "USD CPI m/m", dt.datetime(2026, 9, 11, 12, 30, tzinfo=dt.UTC))  # EDT
    _ff(engine, "USD CPI m/m", dt.datetime(2026, 1, 13, 13, 30, tzinfo=dt.UTC))  # EST
    fetcher = FeedFetcher({CPI: [_atom(CPI_SEP, CPI_JAN)]})
    collector = _bls(engine, fetcher, Clock(RELEASED), [CPI])

    events = {e.source_uid: e for e in await _cycle(collector)}

    sep = events["cpi-2026_09_11__07_50_40"]
    jan = events["cpi-2026_01_13__07_50_31"]
    assert sep.ts_event == dt.datetime(2026, 9, 11, 12, 30, tzinfo=dt.UTC)
    assert jan.ts_event == dt.datetime(2026, 1, 13, 13, 30, tzinfo=dt.UTC)
    assert sep.raw["ts_source"] == jan.raw["ts_source"] == "calendar"


async def test_kolize_bere_jen_mapovany_titulek_teho_dne_v_et(engine: Engine) -> None:
    """V den NFP jsou v kalendáři i projev FOMC, ADP, PMI a Core CPI — vezme se jen NFP."""
    day = dt.datetime(2026, 10, 2, tzinfo=dt.UTC)
    _ff(engine, "USD FOMC Member Waller Speaks", day.replace(hour=12))  # 8:00 ET
    _ff(engine, "USD ADP Non-Farm Employment Change", day.replace(hour=12, minute=15))
    _ff(engine, "USD Final Manufacturing PMI", day.replace(hour=13, minute=45))  # 9:45 ET
    _ff(engine, "USD Core CPI m/m", day.replace(hour=12, minute=30))
    _ff(engine, "USD Non-Farm Employment Change", day.replace(hour=12, minute=30))
    collector = _bls(engine, FeedFetcher({EMPSIT: [_atom(NFP_OCT)]}), Clock(RELEASED), [EMPSIT])

    (event,) = await _cycle(collector)

    assert event.ts_event == day.replace(hour=12, minute=30)
    assert event.raw["ts_source"] == "calendar"
    # Podřetězec titulku nestačí: CPI m/m se nespáruje s Core CPI m/m
    assert calendar_release_time(engine, "USD CPI m/m", dt.date(2026, 10, 2)) is None


def test_den_kalendare_se_ohranicuje_v_et(engine: Engine) -> None:
    """22:00 EST 5. 11. je v UTC už 6. 11. — patří ke dni 5. 11. v ET."""
    evening = dt.datetime(2026, 11, 6, 3, 0, tzinfo=dt.UTC)
    _ff(engine, "USD JOLTS Job Openings", evening)
    assert calendar_release_time(engine, "USD JOLTS Job Openings", dt.date(2026, 11, 6)) is None
    assert calendar_release_time(engine, "USD JOLTS Job Openings", dt.date(2026, 11, 5)) == evening


async def test_restart_v_jiny_den_nezmeni_ts_event_ani_dedup_hash(engine: Engine) -> None:
    _ff(engine, "USD Non-Farm Employment Change", dt.datetime(2026, 10, 2, 12, 30, tzinfo=dt.UTC))
    writer = NewsWriter(engine)
    clock = Clock(RELEASED)
    feeds = {EMPSIT: [_atom(NFP_OCT, NFP_SEP)]}
    first = await _cycle(_bls(engine, FeedFetcher(feeds), clock, [EMPSIT]))
    (release,) = [e for e in first if e.source_uid == "empsit-2026_10_02__07_51_08"]
    assert writer.write([release]) == 1

    # Nový proces o tři dny později: první stažení po startu nese tutéž položku
    clock.now = RELEASED + dt.timedelta(days=3)
    second = await _cycle(_bls(engine, FeedFetcher(feeds), clock, [EMPSIT]))
    (again,) = [e for e in second if e.source_uid == release.source_uid]

    assert (again.ts_event, again.dedup_hash) == (release.ts_event, release.dedup_hash)
    assert writer.write([again]) == 0
    with engine.connect() as conn:
        count = conn.execute(
            select(func.count())
            .select_from(news_events)
            .where(news_events.c.source == "bls", news_events.c.source_uid == release.source_uid)
        ).scalar()
    assert count == 1


async def test_bez_radku_ff_plati_cas_stazeni_s_pojistkami(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """Položka z prvního stažení se nezapíše; nová dostane čas stažení, ne čas položky."""
    clock = Clock(RELEASED)
    fetcher = FeedFetcher({EMPSIT: [_atom(NFP_SEP), _atom(NFP_OCT, NFP_SEP)]})
    collector = _bls(engine, fetcher, clock, [EMPSIT])
    with caplog.at_level(logging.WARNING):
        assert await _cycle(collector) == []
    assert any("bez věrohodného času" in r.getMessage() for r in caplog.records)

    clock.now = RELEASED + dt.timedelta(minutes=1)
    (event,) = await _cycle(collector)

    assert event.source_uid == "empsit-2026_10_02__07_51_08"
    # Ne čas položky 11:51:08 UTC — ten předbíhá release o 39 min
    assert event.ts_event == clock.now
    assert event.raw["ts_source"] == "fetch"


async def test_kalendar_se_pta_jednou_a_cas_plati_do_konce_behu(engine: Engine) -> None:
    """Přidělený čas z kalendáře drží `dedup_hash`, i když se kalendář během běhu změní."""
    calls: list[tuple[str, dt.date]] = []
    answers = [dt.datetime(2026, 10, 2, 12, 30, tzinfo=dt.UTC), None]

    def lookup(title: str, day: dt.date) -> dt.datetime | None:
        calls.append((title, day))
        return answers[min(len(calls) - 1, len(answers) - 1)]

    clock = Clock(RELEASED)
    collector = RssCollector(
        "bls",
        [EMPSIT],
        FeedFetcher({EMPSIT: [_atom(NFP_OCT)]}),
        release_time=bls_release_time(lookup),
        clock=clock,
    )
    (first,) = await _cycle(collector)
    clock.now = RELEASED + dt.timedelta(hours=1)
    (second,) = await _cycle(collector)

    assert calls == [("USD Non-Farm Employment Change", dt.date(2026, 10, 2))]
    assert (second.ts_event, second.raw["ts_source"]) == (first.ts_event, "calendar")


def test_den_releasu_v_et_z_casu_polozky_zalozne_ze_stazeni() -> None:
    # 23:50 ET je v UTC už další den — rozhoduje den v ET
    late = RawItem(
        source="bls",
        payload={"feed": CPI, "published": "2026-09-10T23:50:00-04:00"},
        fetched_at=dt.datetime(2026, 9, 11, 4, 0, tzinfo=dt.UTC),
    )
    assert release_day(late) == dt.date(2026, 9, 10)
    # Bez věrohodného času (rok 1899) den stažení v ET
    undated = RawItem(
        source="bls",
        payload={"feed": CPI, "published": "1899-12-30T00:00:00Z"},
        fetched_at=dt.datetime(2026, 10, 2, 3, 0, tzinfo=dt.UTC),
    )
    assert release_day(undated) == dt.date(2026, 10, 1)


def test_mapovani_feedu_pokryva_klasifikaci() -> None:
    """Každý feed BLS má titulek v kalendáři i kategorii s importance — jeden výčet releasů."""
    assert BLS_RELEASE_TITLES.keys() == BLS_RELEASE_CLASSES.keys()
    # `bls_latest.rss` drží jen 1 položku — feedy po releasech
    assert {bls_release(url) for url in BLS_RSS_URLS} == {"empsit", "cpi", "ppi", "jolts", "eci"}


@pytest.mark.parametrize(
    ("contact", "user_agent"),
    [
        ("Jiří Novák <jiri@example.cz>", "GEXLens research Jiri Novak <jiri@example.cz>"),
        ("  ops@example.com\r\n", "GEXLens research ops@example.com"),
        ("", None),
        ("   ", None),
        ("数据", None),  # nic převoditelného na ASCII = nenakonfigurován
    ],
)
def test_kontakt_se_prevede_na_ascii(contact: str, user_agent: str | None) -> None:
    assert contact_user_agent(contact) == user_agent


async def test_build_collectors_bls_jen_s_kontaktem_a_kontakt_jen_bls(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    from gexlens_news.__main__ import build_collectors
    from gexlens_news.config import NewsSettings

    calendar = functools.partial(calendar_release_time, engine)
    bare = NewsSettings(_env_file=None, finnhub_api_key="", ua_contact="")  # type: ignore[call-arg]
    with caplog.at_level(logging.INFO):
        names = {c.name for c in build_collectors(bare, FeedFetcher({}), calendar=calendar)}
    assert "bls" not in names
    assert any("BLS nenakonfigurován" in r.getMessage() for r in caplog.records)
    assert bare.enabled_sources["bls"] is False

    settings = NewsSettings(  # type: ignore[call-arg]
        _env_file=None, finnhub_api_key="", ua_contact="Jiří <jiri@example.cz>"
    )
    assert settings.enabled_sources["bls"] is True
    fetcher = FeedFetcher({})
    collectors = {
        c.name: c
        for c in build_collectors(settings, fetcher, reddit_subreddits=[], calendar=calendar)
        if isinstance(c, RssCollector)
    }
    await collectors["bls"].fetch()
    await collectors["fed_rss"].fetch()
    for url in BLS_RSS_URLS:
        assert fetcher.headers[url] == {"User-Agent": "GEXLens research Jiri <jiri@example.cz>"}
    assert fetcher.headers["https://www.federalreserve.gov/feeds/speeches.xml"] is None

    with pytest.raises(ValueError, match="kalendář"):
        build_collectors(settings, FeedFetcher({}))
    disabled = build_collectors(settings, FeedFetcher({}), enabled={"bls": False})
    assert "bls" not in {c.name for c in disabled}
