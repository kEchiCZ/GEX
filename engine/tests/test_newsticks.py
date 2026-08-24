"""Testy broker headlines z ticku 292 (#291): normalizace, dedup, zápis."""

import datetime as dt
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from ib_async import IB
from sqlalchemy import create_engine, select

import gexlens_engine.__main__ as engine_main
from gexlens_engine.ibkr.connection import ConnectionManager, ConnectionState
from gexlens_engine.ibkr.newsticks import (
    BROAD_TAPE_TICKS,
    NewsTickCollector,
    broad_tape_providers,
    clean_headline,
    normalize_tick,
    subscribe_broad_tape,
    tape_symbol,
    tick_time,
)
from gexlens_engine.runtime import PublisherLike
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


class FakeClient:
    """Zrcadlí `ib.client`; `fail_on` simuluje providera bez `_ALL` pásky."""

    def __init__(self, fail_on: set[str] | None = None) -> None:
        self.calls: list[tuple[int, str, str]] = []
        self._next_id = 100
        self._fail_on = fail_on or set()

    def getReqId(self) -> int:
        self._next_id += 1
        return self._next_id

    def reqMktData(
        self,
        reqId: int,
        contract: Any,
        genericTickList: str,
        snapshot: bool,
        regulatorySnapshot: bool,
        mktDataOptions: list[Any],
    ) -> None:
        if contract in self._fail_on:
            raise RuntimeError(f"provider {contract} neexistuje")
        self.calls.append((reqId, contract, genericTickList))


# ── Broad tape (#334) ──────────────────────────────────────────────


def test_tape_symbol_builds_provider_wide_feed() -> None:
    assert tape_symbol("BRFG") == "BRFG:BRFG_ALL"
    assert tape_symbol("DJ-RTG") == "DJ-RTG:DJ-RTG_ALL"


def test_broad_tape_providers_normalizuje_dow_jones_kanaly() -> None:
    """#546 + #734: přesně to, co IBKR vrátilo na produkci 17. 8. 2026.

    Pět DJ-* kanálů je pro broad tape neplatných (Warning 321) → kořen `DJ`.
    Jenže kontrakt `DJ:DJ_ALL` neexistuje (Error 200, změřeno #734) a
    `BRFUPDN` taky ne — oba mrtvé kořeny se přeskakují, ať start negeneruje
    chyby, které vypadají jako porucha. Reálně tečou jen BRFG a DJNL.
    """
    z_ibkr = ["BRFG", "BRFUPDN", "DJ-N", "DJ-RT", "DJ-RTA", "DJ-RTE", "DJ-RTG", "DJNL"]

    assert broad_tape_providers(z_ibkr) == ["BRFG", "DJNL"]


def test_broad_tape_providers_nechava_kody_bez_pomlcky() -> None:
    assert broad_tape_providers(["BRFG", "DJNL", "BZ", "FLY"]) == ["BRFG", "DJNL", "BZ", "FLY"]


def test_broad_tape_providers_zahodi_prazdne_a_duplicity() -> None:
    """Duplicity vzniknou normalizací (BZ-PRO i BZ-X → BZ); pořadí se drží."""
    assert broad_tape_providers(["BZ-PRO", "", "  ", "BZ-X", "BRFG", "BZ"]) == ["BZ", "BRFG"]


def test_broad_tape_providers_preskoci_mrtve_pasky() -> None:
    """#734: BRFUPDN a DJ mají změřený Error 200 — do subskripce nejdou."""
    assert broad_tape_providers(["BRFUPDN", "DJ", "DJ-N"]) == []


def test_subscribe_broad_tape_uses_mdoff_and_covers_every_provider() -> None:
    """`mdoff` je nutné — bez něj IBKR chce ke kontraktu kotace, které NEWS nemá."""
    client = FakeClient()
    providers = ["BRFG", "DJ-RTG", "DJNL"]

    subscribed = subscribe_broad_tape(client, providers, make_contract=tape_symbol)

    assert subscribed == providers
    assert [call[1] for call in client.calls] == [
        "BRFG:BRFG_ALL",
        "DJ-RTG:DJ-RTG_ALL",
        "DJNL:DJNL_ALL",
    ]
    assert {call[2] for call in client.calls} == {BROAD_TAPE_TICKS}
    # Každá páska musí mít vlastní reqId, jinak by si je IBKR přepsala
    assert len({call[0] for call in client.calls}) == len(providers)


def test_subscribe_broad_tape_survives_provider_without_tape() -> None:
    """`BRFUPDN` pásku nemá — nesmí sebrat zbytek s sebou."""
    client = FakeClient(fail_on={"BRFUPDN:BRFUPDN_ALL"})

    subscribed = subscribe_broad_tape(
        client, ["BRFG", "BRFUPDN", "DJNL"], make_contract=tape_symbol
    )

    assert subscribed == ["BRFG", "DJNL"]


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

    assert len(collector.write(ticks, now=NOW)) == 1
    # IBKR seznam je kumulativní — druhé čtení téhož ticku nesmí zapsat znovu
    assert collector.write(ticks, now=NOW) == []
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
    assert len(collector.write(ticks, now=NOW)) == 1
    assert collector.write(ticks, now=NOW) == []


def test_written_headline_carries_id_and_ws_payload(tmp_path: Path) -> None:
    """Push do WS smí jít jen z toho, co v DB opravdu přibylo (#335)."""
    collector, _ = make(tmp_path)

    written = collector.write([FakeTick("!DJ-RTG Fed holds rates", articleId="x1")], now=NOW)

    assert len(written) == 1
    payload = written[0].as_news_row()
    assert payload["id"] == written[0].id > 0
    assert payload["title"] == "Fed holds rates"
    assert payload["kind"] == "broker"
    # Kategorii doplní až news-engine; UI ji zobrazí jako „Nezařazeno"
    assert payload["category"] is None
    assert payload["ts_event"] == NOW.isoformat()


def test_market_closed_odpovida_case_zpravy(tmp_path: Path) -> None:
    """Sobotní titulek se nesmí uložit, jako by trh běžel (#339)."""
    collector, engine = make(tmp_path)
    sobota = dt.datetime(2026, 7, 25, 23, 0, tzinfo=dt.UTC)  # 18:00 CT

    collector.write(
        [FakeTick("Breaking weekend story", timeStamp=int(sobota.timestamp()))], now=NOW
    )

    with engine.connect() as conn:  # type: ignore[attr-defined]
        row = conn.execute(select(news_events)).fetchone()
    assert row is not None
    assert row.market_closed is True


def test_empty_input_is_noop(tmp_path: Path) -> None:
    collector, _ = make(tmp_path)
    assert collector.write([], now=NOW) == []
    assert collector.write([FakeTick("")], now=NOW) == []


# ── Start broker news bez spojení (#778) ─────────────────────────────


class _FakeTickEvent:
    def __init__(self) -> None:
        self.handlers: list[Any] = []

    def __iadd__(self, handler: Any) -> "_FakeTickEvent":
        self.handlers.append(handler)
        return self


class _NullPublisher(PublisherLike):
    async def status(self, **fields: object) -> None:
        pass

    async def publish(self, channel: str, data: dict[str, object]) -> None:
        pass


def _fake_ib(providers: Callable[[], Awaitable[list[Any]]]) -> Any:
    return SimpleNamespace(tickNewsEvent=_FakeTickEvent(), reqNewsProvidersAsync=providers)


async def test_start_bez_spojeni_odber_odlozi_a_nespadne() -> None:
    """#778: engine startující bez TWS (#756) nesmí spadnout na eager odběru —
    19. 8. ConnectionError odsud vzal celý main() a proces zůstal viset."""

    async def providers() -> list[Any]:
        raise AssertionError("na nepřipojeného klienta se nesmí sahat")

    callbacks: list[Any] = []
    manager = SimpleNamespace(state=ConnectionState.RECONNECTING, on_resubscribe=callbacks.append)
    ib = _fake_ib(providers)

    await engine_main._start_broker_news(
        cast(IB, ib),
        cast(ConnectionManager, manager),
        cast(NewsTickCollector, None),
        _NullPublisher(),
    )

    assert len(callbacks) == 1  # záruka odběru: proběhne s prvním (re)connectem
    assert len(ib.tickNewsEvent.handlers) == 1  # příjem ticků je zapojený už teď


async def test_pad_spojeni_behem_eager_odberu_nepropadne() -> None:
    """Race #778: stav byl connected, ale spojení spadlo před odběrem —
    ConnectionError nesmí zabít main(), odběr obnoví reconnect callback."""

    async def providers() -> list[Any]:
        raise ConnectionError("Not connected")

    manager = SimpleNamespace(state=ConnectionState.CONNECTED, on_resubscribe=lambda callback: None)

    await engine_main._start_broker_news(
        cast(IB, _fake_ib(providers)),
        cast(ConnectionManager, manager),
        cast(NewsTickCollector, None),
        _NullPublisher(),
    )


# ── Plné znění článků přes reqNewsArticle (#743) ────────────────────


class FakeArticleClient:
    """reqNewsArticleAsync stub: HTML článek pro BRFG, výjimka pro DJNL."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def reqNewsArticleAsync(self, provider: str, article_id: str, _opts=None):
        self.calls.append((provider, article_id))
        if provider == "DJNL":
            raise RuntimeError("article nedostupný")

        class Article:
            articleType = 1  # HTML
            articleText = "<p>Fed poprvé <b>naznačil</b> pauzu.</p>"

        return Article()


async def test_article_fetcher_doplni_body_a_neprepisuje(tmp_path) -> None:
    from sqlalchemy import create_engine as ce

    from gexlens_engine.ibkr.newsticks import ArticleFetcher
    from gexlens_engine.storage.sentiment import ensure_sentiment_schema, news_events

    db = ce(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(db)
    now = dt.datetime(2026, 8, 24, 6, 0, tzinfo=dt.UTC)
    with db.begin() as conn:
        for i, (provider, body) in enumerate([("BRFG", None), ("DJNL", None), ("BRFG", "už má")]):
            conn.execute(
                news_events.insert().values(
                    ts_event=now,
                    ts_ingested=now,
                    source=f"ibkr_{provider.lower()}",
                    kind="broker",
                    title=f"Titulek {i}",
                    body=body,
                    symbols=[],
                    market_closed=False,
                    dedup_hash=f"h{i}",
                    raw={"provider": provider, "article_id": f"a{i}"},
                )
            )
    client = FakeArticleClient()
    fetcher = ArticleFetcher(client, db, spacing_s=0.0)

    filled = await fetcher.catch_up()

    assert filled == 1  # BRFG bez body doplněn; DJNL selhal; třetí body už měl
    with db.connect() as conn:
        rows = {r.title: r.body for r in conn.execute(news_events.select()).fetchall()}
    assert rows["Titulek 0"] == "Fed poprvé naznačil pauzu."  # HTML pryč
    assert rows["Titulek 1"] is None  # selhání nechává díru, nevymýšlí
    assert rows["Titulek 2"] == "už má"  # existující body se NIKDY nepřepisuje
    assert ("BRFG", "a2") not in client.calls  # s body se ani nefetchuje
