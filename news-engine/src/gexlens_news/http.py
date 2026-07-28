"""HTTP vrstva collectorů: conditional GET a šetrné hlavičky (SPEC kap. 1).

Conditional GET (`ETag` / `If-Modified-Since`) je důvod, proč můžou RSS zdroje
jet à 60 s a přesto nikoho nezatěžovat: nezměněný feed vrátí prázdnou 304.
Bez toho by latenční požadavek „headline → DB < 60 s" (kap. 10) nešlo splnit
šetrně.
"""

import logging
from dataclasses import dataclass, field
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)

# Některé zdroje (CNN, Reddit) holý klient odmítají — ADR-0014 to změřil.
# Prohlížečová hlavička není obcházení ochrany, jen splnění jejich očekávání.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT_S = 20.0


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
