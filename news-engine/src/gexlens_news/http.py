"""HTTP vrstva collectorů: conditional GET a šetrné hlavičky (SPEC kap. 1).

Conditional GET (`ETag` / `If-Modified-Since`) je důvod, proč můžou RSS zdroje
jet à 60 s a přesto nikoho nezatěžovat: nezměněný feed vrátí prázdnou 304.
Bez toho by latenční požadavek „headline → DB < 60 s" (kap. 10) nešlo splnit
šetrně.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)

# Některé zdroje (CNN, Reddit) holý klient odmítají — ADR-0014 to změřil.
# Prohlížečová hlavička není obcházení ochrany, jen splnění jejich očekávání.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT_S = 20.0

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

        response = await self.client.get(url, headers=request_headers, follow_redirects=True)
        if response.status_code == 304:
            return Response(status=304, text="", not_modified=True)
        response.raise_for_status()
        if tag := response.headers.get("etag"):
            self._etags[url] = tag
        if last := response.headers.get("last-modified"):
            self._modified[url] = last
        return Response(status=response.status_code, text=response.text)


def make_fetcher(timeout_s: float = DEFAULT_TIMEOUT_S) -> ConditionalFetcher:
    return ConditionalFetcher(client=httpx.AsyncClient(timeout=timeout_s))
