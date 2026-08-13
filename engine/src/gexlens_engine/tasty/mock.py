"""Mock vrstva tasty větve pro CI (#613) — živé API se v testech NIKDY nevolá.

Stejné pravidlo jako pro IBKR (CLAUDE.md bod 4): testy krmí cache a stream
syntetickými eventy ve formátu COMPACT polí (stream.EVENT_FIELDS).
"""

from gexlens_engine.tasty.provider import TastyChainCache


class MockTokenSource:
    """TokenSource s počítadlem — testy reconnect logiky."""

    def __init__(self, url: str = "wss://mock.invalid/realtime", token: str = "mock") -> None:
        self.url = url
        self.token = token
        self.calls = 0

    async def __call__(self) -> tuple[str, str]:
        self.calls += 1
        return self.url, self.token


def feed_quote(
    cache: TastyChainCache,
    symbol: str,
    bid: float,
    ask: float,
    bid_size: float = 1.0,
    ask_size: float = 1.0,
) -> None:
    cache.on_event("Quote", [symbol, bid, ask, bid_size, ask_size])


def feed_greeks(
    cache: TastyChainCache,
    symbol: str,
    iv: float,
    delta: float,
    gamma: float,
    theta: float = 0.0,
    vega: float = 0.0,
    theo: float = 0.0,
) -> None:
    cache.on_event("Greeks", [symbol, iv, delta, gamma, theta, vega, theo])


def feed_summary(cache: TastyChainCache, symbol: str, open_interest: float) -> None:
    cache.on_event("Summary", [symbol, open_interest, None, None])


def feed_trade(cache: TastyChainCache, symbol: str, aggressor: str | None) -> None:
    cache.on_event(
        "TimeAndSale", [symbol, 0, 1.0, 1.0, aggressor if aggressor else "UNDEFINED", False, False]
    )
