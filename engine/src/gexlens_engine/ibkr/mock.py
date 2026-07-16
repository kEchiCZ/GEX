"""Mock IBKR klienta pro testy (CLAUDE.md pravidlo 4: CI nikdy proti live API).

Implementuje `IBClientLike` rozhraní a umožňuje simulovat výpadky spojení,
selhání connectu a zamrzlý heartbeat.
"""

import asyncio


class MockIB:
    """Testovací náhrada ib_async.IB pro ConnectionManager."""

    def __init__(self, *, fail_connects: int = 0) -> None:
        # Kolik prvních connectAsync pokusů má selhat (simulace neběžícího TWS)
        self.fail_connects = fail_connects
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.market_data_type_requests: list[int] = []
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

    def drop_connection(self) -> None:
        """Simulace výpadku TWS (kill) — spojení zmizí bez rozloučení."""
        self._connected = False
