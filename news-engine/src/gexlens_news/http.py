"""HTTP vrstva collectorů: conditional GET a šetrné hlavičky (SPEC kap. 1).

Conditional GET (`ETag` / `If-Modified-Since`) je důvod, proč můžou RSS zdroje
jet à 60 s a přesto nikoho nezatěžovat: nezměněný feed vrátí prázdnou 304.
Bez toho by latenční požadavek „headline → DB < 60 s" (kap. 10) nešlo splnit
šetrně.
"""

import ipaddress
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# Některé zdroje (CNN, Reddit) holý klient odmítají — ADR-0014 to změřil.
# Prohlížečová hlavička není obcházení ochrany, jen splnění jejich očekávání.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT_S = 20.0

# M4 (#552): strop těla odpovědi. Feed je nedůvěryhodný vstup — kompromitovaný
# server vrátí gigabajtové tělo a `response.text` ho celé natáhne do paměti.
# Timeout je per-operace ČTENÍ, takže slow-drip velké odpovědi ho obejde: každý
# chunk dorazí včas, jen jich je bez konce. Proto se čte streamovaně a počítá.
# 8 MB je řádově nad největším reálným feedem (FF kalendář ~1 MB).
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

# L1 (#552): redirect smí zůstat zapnutý (zdroje ho běžně používají), ale
# hijacknutý server by jinak mohl přesměrovat na interní adresu (`http://api:8000`,
# metadata endpoint VPS providera) a tělo interní odpovědi by skončilo přes `raw`
# v DB. Povolené je jen http/https a hostitel mimo loopback a privátní rozsahy.
MAX_REDIRECTS = 5


class ResponseTooLarge(Exception):
    """Tělo odpovědi překročilo `MAX_RESPONSE_BYTES` — obsah se zahazuje."""


class UnsafeRedirect(Exception):
    """Cíl přesměrování míří mimo veřejný internet (SSRF), požadavek se ruší."""


def is_public_url(url: str) -> bool:
    """Je URL na veřejném internetu? (L1 — obrana proti SSRF přes redirect.)

    Odmítá jiná schémata než http/https a hostitele v loopbacku, privátních,
    link-local a dalších neveřejných rozsazích — tam vedou metadata endpointy
    cloudových providerů (169.254.169.254) i naše vlastní služby v Docker síti.
    Doménová jména se nerozlišují (DNS rebinding je mimo rozsah téhle obrany);
    chrání se před přímým přesměrováním na IP a na známá interní jména.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").strip().rstrip(".").lower()
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".internal"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Doménové jméno: nechává se projít (viz docstring), interní jména
        # našich služeb (`api`, `postgres`) nemají tečku a spadnou sem —
        # proto se odmítá i jednoslovný host bez tečky.
        return "." in host
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


async def read_limited(response: httpx.Response, *, limit: int = MAX_RESPONSE_BYTES) -> str:
    """Načte tělo streamovaně a nad limitem vyhodí `ResponseTooLarge`.

    `Content-Length` se kontroluje jako první (levné odmítnutí), ale nestačí:
    chunked odpověď ho nemá a lhát se v něm dá taky. Rozhoduje proto skutečný
    počet přečtených bajtů.
    """
    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise ResponseTooLarge(f"Content-Length {declared} > {limit}")
        except ValueError:
            pass  # nečitelná hlavička — rozhodne skutečné čtení níž

    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > limit:
            raise ResponseTooLarge(f"tělo přesáhlo {limit} B")
        chunks.append(chunk)
    body = b"".join(chunks)
    encoding = response.encoding or "utf-8"
    return body.decode(encoding, errors="replace")


# S10: klíč v query stringu se nesmí dostat do logů ani do uložených chyb.
# httpx dává celé URL do textu výjimky, takže bez tohohle by token skončil
# v `CollectorHealth.last_error` a odtud v UI.
_SECRET_PARAM = re.compile(r"((?:token|api[_-]?key|apikey|key|secret)=)[^&\s\"']+", re.I)

# S10 (#553): token může být i segment cesty (`/feed/<token>.xml`). Maskuje se
# dlouhý segment bez pomlček a teček ([A-Za-z0-9_]{20,}) — slugy článků tečky
# a pomlčky mají, tokeny ne. Lookbehind vylučuje `//` (host) a `:/` (schéma),
# aplikuje se jen na texty s `://`, aby se nemrzačil běžný text.
_SECRET_PATH_SEGMENT = re.compile(r"(?<![:/])/[A-Za-z0-9_]{20,}(?=[./?#&\s\"']|$)")


def strip_secrets(text: str) -> str:
    """Nahradí hodnoty citlivých query parametrů (a tokeny v URL cestě) hvězdičkami.

    Náhrada je lambdou, ne backreferencí: escapování v replacement stringu se
    snadno rozbije a tichá chyba by znamenala, že klíč projde do logu.
    """
    cleaned = _SECRET_PARAM.sub(lambda match: f"{match.group(1)}***", text)
    if "://" in cleaned:
        cleaned = _SECRET_PATH_SEGMENT.sub("/***", cleaned)
    return cleaned


def sanitize_raw(value: Any) -> Any:
    """Rekurzivní sanitizace `raw` payloadu před zápisem do DB (S10, #553).

    Prochází dict/list/str; každý string projde `strip_secrets`. Vrací novou
    strukturu, vstup nemění. Ostatní typy prochází beze změny — jediný nosič
    tajemství je text (URL v položkách feedů).
    """
    if isinstance(value, str):
        return strip_secrets(value)
    if isinstance(value, dict):
        return {key: sanitize_raw(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_raw(item) for item in value]
    return value


def read_limited_cffi(response: Any, *, limit: int = MAX_RESPONSE_BYTES) -> str:
    """Totéž co `read_limited`, ale pro **synchronní** curl_cffi odpověď (M4).

    CNN F&G a FF kalendář jdou přes curl_cffi kvůli Chrome TLS fingerprintu
    (ADR-0014, #277), takže httpx cestu sdílet nemůžou. Volající musí předat
    odpověď získanou se `stream=True`, jinak je tělo stažené už při requestu
    a limit by přišel pozdě.
    """
    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise ResponseTooLarge(f"Content-Length {declared} > {limit}")
        except ValueError:
            pass

    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_content():
        size += len(chunk)
        if size > limit:
            raise ResponseTooLarge(f"tělo přesáhlo {limit} B")
        chunks.append(chunk)
    encoding = getattr(response, "encoding", None) or "utf-8"
    return b"".join(chunks).decode(encoding, errors="replace")


@dataclass(frozen=True)
class Response:
    """Odpověď zdroje; `not_modified` = 304, tělo je prázdné a nemá se parsovat."""

    status: int
    text: str
    not_modified: bool = False


class Fetcher(Protocol):
    async def get(self, url: str, *, headers: dict[str, str] | None = None) -> Response: ...


@dataclass
class ConditionalFetcher:
    """HTTP klient s pamětí validátorů per URL.

    Uchovává `ETag` a `Last-Modified` z minulé odpovědi a posílá je zpět;
    server pak u nezměněného obsahu odpoví 304 bez těla.
    """

    client: httpx.AsyncClient
    _etags: dict[str, str] = field(default_factory=dict, repr=False)
    _modified: dict[str, str] = field(default_factory=dict, repr=False)

    async def get(self, url: str, *, headers: dict[str, str] | None = None) -> Response:
        request_headers = {"User-Agent": BROWSER_UA, **(headers or {})}
        if etag := self._etags.get(url):
            request_headers["If-None-Match"] = etag
        if modified := self._modified.get(url):
            request_headers["If-Modified-Since"] = modified

        response = await self._fetch_following_redirects(url, request_headers)
        try:
            if response.status_code == 304:
                return Response(status=304, text="", not_modified=True)
            response.raise_for_status()
            # Tělo se čte streamovaně s limitem (M4) — až PO raise_for_status,
            # ať se chybová odpověď nestahuje zbytečně celá
            text = await read_limited(response)
        finally:
            await response.aclose()
        if tag := response.headers.get("etag"):
            self._etags[url] = tag
        if last := response.headers.get("last-modified"):
            self._modified[url] = last
        return Response(status=response.status_code, text=text)

    async def _fetch_following_redirects(self, url: str, headers: dict[str, str]) -> httpx.Response:
        """Redirecty se sledují ručně, aby šel ověřit KAŽDÝ cíl (L1).

        `follow_redirects=True` v httpx skáče samo a naše kontrola by viděla
        až konečnou odpověď — tou dobou je interní adresa dávno stažená.
        """
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            if not is_public_url(current):
                raise UnsafeRedirect(f"Cíl mimo veřejný internet: {strip_secrets(current)}")
            request = self.client.build_request("GET", current, headers=headers)
            response = await self.client.send(request, stream=True, follow_redirects=False)
            # 304 je 3xx, ale NENÍ přesměrování — je to odpověď na conditional
            # GET a musí projít ven netknutá, jinak by se každý nezměněný feed
            # tvářil jako „přesměrování bez cíle".
            if response.status_code == 304 or not response.is_redirect:
                return response
            location = response.headers.get("location", "")
            await response.aclose()
            if not location:
                raise UnsafeRedirect(f"Přesměrování bez cíle: {strip_secrets(current)}")
            current = str(response.url.join(location))
        raise UnsafeRedirect(f"Překročen limit {MAX_REDIRECTS} přesměrování: {strip_secrets(url)}")


def make_fetcher(timeout_s: float = DEFAULT_TIMEOUT_S) -> ConditionalFetcher:
    return ConditionalFetcher(client=httpx.AsyncClient(timeout=timeout_s))
