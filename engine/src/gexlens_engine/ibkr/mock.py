"""Mock IBKR klienta pro testy (CLAUDE.md pravidlo 4: CI nikdy proti live API).

Implementuje `IBClientLike` rozhraní a umožňuje simulovat výpadky spojení,
selhání connectu a zamrzlý heartbeat.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass
class MockOptionChain:
    """Testovací obdoba ib_async.OptionChain (atributy v camelCase jako v ib_async)."""

    exchange: str
    tradingClass: str
    multiplier: str
    expirations: list[str] = field(default_factory=list)
    strikes: list[float] = field(default_factory=list)


class MockIB:
    """Testovací náhrada ib_async.IB pro ConnectionManager a ChainDiscovery."""

    def __init__(
        self,
        *,
        fail_connects: int = 0,
        option_chains: Sequence[MockOptionChain] = (),
    ) -> None:
        # Kolik prvních connectAsync pokusů má selhat (simulace neběžícího TWS)
        self.fail_connects = fail_connects
        self.option_chains = list(option_chains)
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.market_data_type_requests: list[int] = []
        self.sec_def_requests: list[tuple[str, str, str, int]] = []
        self.heartbeat_hang = False
        self._connected = False

    # Jména metod záměrně kopírují camelCase API ib_async
    async def connectAsync(
        self,
        host: str,
        port: int,
        clientId: int,
        timeout: float,
    ) -> object:
        self.connect_calls += 1
        if self.connect_calls <= self.fail_connects:
            raise ConnectionRefusedError(f"mock: TWS na {host}:{port} neběží")
        self._connected = True
        return self

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._connected = False

    def isConnected(self) -> bool:
        return self._connected

    def reqMarketDataType(self, marketDataType: int) -> None:
        self.market_data_type_requests.append(marketDataType)

    async def reqCurrentTimeAsync(self) -> object:
        if self.heartbeat_hang:
            await asyncio.sleep(3600)
        if not self._connected:
            raise ConnectionError("mock: odpojeno")
        return 0

    async def reqSecDefOptParamsAsync(
        self,
        underlyingSymbol: str,
        futFopExchange: str,
        underlyingSecType: str,
        underlyingConId: int,
    ) -> Sequence[MockOptionChain]:
        self.sec_def_requests.append(
            (underlyingSymbol, futFopExchange, underlyingSecType, underlyingConId)
        )
        return list(self.option_chains)

    def drop_connection(self) -> None:
        """Simulace výpadku TWS (kill) — spojení zmizí bez rozloučení."""
        self._connected = False
