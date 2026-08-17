"""Odolnost vůči nepřátelskému nebo rozbitému feedu (#552, nálezy #542 M4/L1–L4).

Nic z toho dnes neteče — všechno ale stálo na předpokladu „feed se chová
slušně". Feed je nedůvěryhodný vstup a s nasazením na VPS (#539) relevance
roste. Testy proto simulují právě ty vstupy, kterými by se slušný feed nikdy
nevyznačoval.
"""

import datetime as dt
import xml.etree.ElementTree as ElementTree
from typing import Any

import httpx
import pytest

from gexlens_news.collectors.rss import (
    MAX_ITEMS_PER_FEED,
    TITLE_LIMIT,
    RssCollector,
    parse_items,
)
from gexlens_news.http import (
    MAX_RESPONSE_BYTES,
    ConditionalFetcher,
    ResponseTooLarge,
    UnsafeRedirect,
    is_public_url,
    read_limited,
)
from gexlens_news.model import RawItem

NOW = dt.datetime(2026, 8, 17, 12, 0, tzinfo=dt.UTC)


def _feed(items_xml: str) -> str:
    return f"<rss><channel>{items_xml}</channel></rss>"


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ── M4: strop velikosti odpovědi ───────────────────────────────────


@pytest.mark.asyncio
async def test_velke_telo_se_zahodi_misto_nacteni_do_pameti() -> None:
    """Gigabajtové tělo nesmí skončit v paměti — čte se streamovaně s limitem."""
    huge = b"x" * (MAX_RESPONSE_BYTES + 1024)

    def handler(request: httpx.Request) -> httpx.Response:
        # Bez Content-Length (chunked) — limit musí platit i tak
        return httpx.Response(200, content=huge, headers={"transfer-encoding": "chunked"})

    fetcher = ConditionalFetcher(client=_client(handler))
    with pytest.raises(ResponseTooLarge):
        await fetcher.get("https://feed.example.com/rss")


@pytest.mark.asyncio
async def test_lzive_content_length_neobejde_limit() -> None:
    """Deklarovaná délka se kontroluje, ale rozhoduje skutečný počet bajtů."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"y" * (MAX_RESPONSE_BYTES + 10),
            headers={"content-length": "10"},  # lež
        )

    response = httpx.Response(200)
    assert response is not None  # sanity
    fetcher = ConditionalFetcher(client=_client(handler))
    with pytest.raises(ResponseTooLarge):
        await fetcher.get("https://feed.example.com/rss")


@pytest.mark.asyncio
async def test_content_length_nad_limitem_odmitne_hned() -> None:
    """Levné odmítnutí: deklarovaná délka nad stropem → tělo se ani nečte."""

    class FakeResponse:
        headers = {"content-length": str(MAX_RESPONSE_BYTES + 1)}
        encoding = "utf-8"

        async def aiter_bytes(self) -> Any:
            raise AssertionError("tělo se nemělo číst vůbec")
            yield b""  # pragma: no cover

    with pytest.raises(ResponseTooLarge):
        await read_limited(FakeResponse())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_normalni_odpoved_prochazi_beze_zmeny() -> None:
    """Limit nesmí rozbít běžný provoz."""
    body = _feed("<item><title>Fed drží sazby</title></item>")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"etag": "abc"})

    fetcher = ConditionalFetcher(client=_client(handler))
    response = await fetcher.get("https://feed.example.com/rss")

    assert response.status == 200
    assert "Fed drží sazby" in response.text


@pytest.mark.asyncio
async def test_304_se_nemeni() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(304)

    fetcher = ConditionalFetcher(client=_client(handler))
    response = await fetcher.get("https://feed.example.com/rss")

    assert response.not_modified is True
    assert response.text == ""


# ── L1: redirect na interní adresy (SSRF) ──────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/status",
        "http://localhost/internal",
        "http://169.254.169.254/latest/meta-data/",  # metadata endpoint VPS
        "http://10.0.0.5/admin",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://api:8000/internal/status",  # naše služba v Docker síti
        "http://postgres:5432/",
        "file:///etc/passwd",
        "gopher://evil.example.com/",
        "http://[::1]/",
    ],
)
def test_neverejne_cile_jsou_odmitnute(url: str) -> None:
    assert is_public_url(url) is False


@pytest.mark.parametrize(
    "url",
    ["https://feeds.reuters.com/rss", "http://www.federalreserve.gov/feed", "https://8.8.8.8/x"],
)
def test_verejne_cile_prochazeji(url: str) -> None:
    assert is_public_url(url) is True


@pytest.mark.asyncio
async def test_redirect_na_interni_adresu_se_nenasleduje() -> None:
    """Hijacknutý feed nesmí přesměrovat na metadata endpoint a dostat tělo do DB."""
    hops: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(str(request.url))
        if request.url.host == "feed.example.com":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/"})
        return httpx.Response(200, text="TAJNÉ CREDENTIALS")

    fetcher = ConditionalFetcher(client=_client(handler))
    with pytest.raises(UnsafeRedirect):
        await fetcher.get("https://feed.example.com/rss")

    # Interní adresa se nesmí ani jednou stáhnout
    assert all("169.254.169.254" not in hop for hop in hops)


@pytest.mark.asyncio
async def test_redirectova_smycka_konci_chybou() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://feed.example.com/next"})

    fetcher = ConditionalFetcher(client=_client(handler))
    with pytest.raises(UnsafeRedirect):
        await fetcher.get("https://feed.example.com/rss")


@pytest.mark.asyncio
async def test_bezny_redirect_na_verejny_cil_projde() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rss":
            return httpx.Response(301, headers={"location": "https://cdn.example.com/rss.xml"})
        return httpx.Response(200, text=_feed("<item><title>Zpráva</title></item>"))

    fetcher = ConditionalFetcher(client=_client(handler))
    response = await fetcher.get("https://feed.example.com/rss")

    assert "Zpráva" in response.text


# ── L2: strop položek a čas pod kontrolou feedu ────────────────────


class _StaticFetcher:
    def __init__(self, text: str) -> None:
        self._text = text

    async def get(self, url: str, *, headers: dict[str, str] | None = None) -> Any:
        from gexlens_news.http import Response

        return Response(status=200, text=self._text)


@pytest.mark.asyncio
async def test_tisic_unikatnich_titulku_se_orizne() -> None:
    """Dedup chrání proti opakování, ne proti tisícům UNIKÁTNÍCH položek."""
    items = "".join(f"<item><title>Zpráva {i}</title><guid>g{i}</guid></item>" for i in range(1000))
    collector = RssCollector(
        "hostile", ["https://feed.example.com/rss"], _StaticFetcher(_feed(items)), clock=lambda: NOW
    )

    fetched = await collector.fetch()

    assert len(fetched) == MAX_ITEMS_PER_FEED


def test_event_z_budoucnosti_neobsadi_vrchol_llm_fronty() -> None:
    """AC: „ts_event v budoucnosti se neřadí na vrchol LLM fronty".

    `ts_event` je plně pod kontrolou feedu a fronta se řadila `ts_event.desc()`,
    takže položka datovaná do roku 2030 by na vrcholu seděla trvale a vyžrala
    denní rozpočet Gemini. Hodnota se ale nepřepisuje (u plánovaných událostí je
    budoucí čas legitimní) — řadí se podle MENŠÍHO z (ts_event, ts_ingested).
    """
    from sqlalchemy import create_engine, insert

    from gexlens_engine.storage.sentiment import ensure_sentiment_schema, news_events
    from gexlens_news.llm_classifier import LlmClassificationJob

    engine = create_engine("sqlite://")
    ensure_sentiment_schema(engine)
    with engine.begin() as conn:
        conn.execute(
            insert(news_events),
            [
                {
                    "ts_event": dt.datetime(2030, 1, 1, tzinfo=dt.UTC),  # podvržený čas
                    "ts_ingested": NOW - dt.timedelta(hours=3),
                    "source": "hostile",
                    "kind": "headline",
                    "title": "Zpráva z roku 2030",
                    "dedup_hash": "h1",
                },
                {
                    "ts_event": NOW,  # legitimní čerstvá zpráva
                    "ts_ingested": NOW,
                    "source": "reuters",
                    "kind": "headline",
                    "title": "Fed drží sazby",
                    "dedup_hash": "h2",
                },
            ],
        )

    job = LlmClassificationJob(engine, client=None, batch_limit=10)  # type: ignore[arg-type]
    pending = job._pending(high_impact_only=False)

    assert [row.title for row in pending][0] == "Fed drží sazby"


# ── L3: dlouhý titulek ─────────────────────────────────────────────


def test_megabajtovy_titulek_se_orizne() -> None:
    """`news_events.title` je Text bez limitu a titulek jde do Gemini promptu."""
    collector = RssCollector("hostile", [], _StaticFetcher(""), clock=lambda: NOW)
    item = RawItem(source="hostile", payload={"title": "A" * 1_000_000}, fetched_at=NOW)

    event = collector.normalize(item)

    assert event is not None
    assert len(event.title) == TITLE_LIMIT


def test_llm_prompt_orizne_i_titulek_ze_stareho_radku() -> None:
    """Řádky zapsané PŘED touhle opravou můžou mít titulek libovolně dlouhý."""
    from gexlens_news.llm_classifier import TITLE_LIMIT as PROMPT_LIMIT
    from gexlens_news.llm_classifier import build_prompt

    prompt = build_prompt([{"id": 1, "title": "B" * 50_000, "summary": None}])

    assert "B" * PROMPT_LIMIT in prompt
    assert "B" * (PROMPT_LIMIT + 1) not in prompt


# ── L4: XML parser ─────────────────────────────────────────────────


def test_expat_umi_branit_amplifikaci() -> None:
    """Rozhodnutí „stdlib místo defusedxml" stojí na verzi expatu (#552 L4).

    Billion laughs je mitigovaný od expatu 2.4. Bez tohohle testu by byl ten
    předpoklad jen v komentáři a tichá výměna base image by ho zrušila.
    """
    import pyexpat

    assert pyexpat.version_info >= (2, 4, 0), (
        f"expat {pyexpat.version_info} je pod 2.4 — buď vrátit novější base image, "
        "nebo přejít na defusedxml (#552 L4)"
    )


def _billion_laughs(levels: int) -> str:
    """Klasická amplifikační bomba: každá úroveň desetinásobí předchozí."""
    entities = '<!ENTITY lol "lol">' + "".join(
        f'<!ENTITY lol{i} "' + (f"&lol{i - 1};" if i > 2 else "&lol;") * 10 + '">'
        for i in range(2, levels + 1)
    )
    return (
        f'<?xml version="1.0"?><!DOCTYPE x [{entities}]>'
        f"<rss><channel><item><title>&lol{levels};</title></item></channel></rss>"
    )


def test_billion_laughs_neprojde() -> None:
    """Kontrola chování, ne jen verze — bomba se nesmí rozvinout do paměti.

    Změřeno (expat 2.7.x): úroveň 6 se ještě rozvine na 300 kB, od úrovně 8
    expat požadavek utne. Klasická bomba má 9 úrovní; test bere 9, aby ověřoval
    práh, ne shodou okolností podlimitní vstup. Menší rozvinutí je neškodné a
    navíc ho zastropuje MAX_RESPONSE_BYTES (M4).
    """
    with pytest.raises(ElementTree.ParseError, match="amplification"):
        parse_items(_billion_laughs(9))


def test_externi_entita_se_nenacte() -> None:
    """XXE: expat externí entity neresolvuje a ElementTree pro ně nemá rozhraní."""
    payload = """<?xml version="1.0"?>
    <!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
    <rss><channel><item><title>&xxe;</title></item></channel></rss>"""

    with pytest.raises(ElementTree.ParseError):
        parse_items(payload)
