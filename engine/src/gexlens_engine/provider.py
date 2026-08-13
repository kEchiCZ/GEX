"""Rozhraní datového providera (#613, M7 fáze 1 — ADR-0025).

Svazek EXISTUJÍCÍCH per-oblast protokolů (kotace, OI snímek, historické bary)
— žádný nový monolit. Stávající IBKR třídy se do něj balí mechanicky beze
změny logiky (`adapters.IbkrProvider`); druhou implementací bude TastyProvider
(shadow mód). Dokud shadow fáze neskončí, jediný provider zapojený do výpočtů
je IBKR — tohle rozhraní chování nemění, jen pojmenovává, co datová cesta
reálně poskytuje.

Historické bary jsou tovární metoda: klient se váže na konkrétní front future
(instance per pipeline, viz `IbHistoricalClient`).
"""

from typing import Protocol

from gexlens_engine.ibkr.scheduler import QuoteStreamerLike
from gexlens_engine.ibkr.underlying import HistoricalClientLike
from gexlens_engine.storage.oi_archive import OIFetcherLike


class MarketDataProviderLike(Protocol):
    """Zdroj tržních dat pro pipeline jednoho podkladu.

    `front` u historických barů je kontrakt podkladu v podobě, které daný
    provider rozumí (IBKR: kvalifikovaný ib_async Contract) — protokol ho
    záměrně nechává netypovaný, ať na sobě implementace nezávisejí.
    """

    @property
    def name(self) -> str:
        """Identifikace zdroje v logu a v porovnávací tabulce (ibkr/tasty)."""
        ...

    def quote_streamer(self) -> QuoteStreamerLike:
        """Sdílený zdroj kotací řetězce (rotační sweep)."""
        ...

    def oi_fetcher(self) -> OIFetcherLike:
        """Denní snímek řetězce (OI + IV/greeks/prémie, #519)."""
        ...

    def historical(self, front: object) -> HistoricalClientLike:
        """Klient 1min barů podkladu vázaný na front kontrakt."""
        ...
