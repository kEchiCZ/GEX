"""IbkrProvider (#613): čistá extrakce — stejné třídy, sdílená kvalifikační cache."""

from ib_async import IB

from gexlens_engine.adapters import (
    IbHistoricalClient,
    IbkrProvider,
    IbOIFetcher,
    IbQuoteStreamer,
)
from gexlens_engine.ibkr.lines import LineGauge
from gexlens_engine.provider import MarketDataProviderLike


def test_ibkr_provider_je_mechanicka_extrakce() -> None:
    """Provider vydává TYTÉŽ třídy jako dosavadní přímá konstrukce (bit-identita)."""
    gauge = LineGauge(lambda: 0)
    provider: MarketDataProviderLike = IbkrProvider(IB(), gauge)
    assert provider.name == "ibkr"

    streamer = provider.quote_streamer()
    assert isinstance(streamer, IbQuoteStreamer)
    # Sdílená instance: sweep i OI snímek používají JEDNU kvalifikační cache
    assert provider.quote_streamer() is streamer
    fetcher = provider.oi_fetcher()
    assert isinstance(fetcher, IbOIFetcher)
    assert fetcher._streamer is streamer
    # Gauge linek (#630) protéká do streameru beze změny
    assert streamer._line_gauge is gauge

    historical = provider.historical(object())
    assert isinstance(historical, IbHistoricalClient)
