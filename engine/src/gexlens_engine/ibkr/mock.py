"""Mock IBKR klienta pro testy (CLAUDE.md pravidlo 4: CI nikdy proti live API).

Implementuje `IBClientLike` rozhraní a umožňuje simulovat výpadky spojení,
selhání connectu a zamrzlý heartbeat.
"""

import asyncio
import datetime as dt
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field

from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.ibkr.hotzone import HotZoneClientLike, StreamLimitError
from gexlens_engine.ibkr.scheduler import QuoteSnapshot
from gexlens_engine.ibkr.underlying import Bar


class PacingViolationError(RuntimeError):
    """Mock obdoba IBKR pacing violation (error 162/420)."""


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


class MockQuoteStreamer:
    """Mock zdroje kotací pro SubscriptionScheduler.

    `fail_first` určuje, kolik prvních pokusů daného kontraktu vrátí nekompletní
    data (None); `always_fail` kontrakty nedodají data nikdy. `delay_s` simuluje
    latenci subskripce.
    """

    def __init__(
        self,
        *,
        fail_first: dict[OptionContractSpec, int] | None = None,
        always_fail: set[OptionContractSpec] | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self.fail_first = dict(fail_first or {})
        self.always_fail = set(always_fail or ())
        self.delay_s = delay_s
        self.fetch_calls: list[OptionContractSpec] = []
        self.max_concurrent = 0
        self._concurrent = 0
        self._attempts: dict[OptionContractSpec, int] = {}

    async def fetch_quote(self, spec: OptionContractSpec, timeout_s: float) -> QuoteSnapshot | None:
        self.fetch_calls.append(spec)
        self._concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self._concurrent)
        try:
            if self.delay_s:
                await asyncio.sleep(min(self.delay_s, timeout_s))
            else:
                await asyncio.sleep(0)  # předání řízení, ať se dávka reálně prokládá
            if spec in self.always_fail:
                return None
            attempt = self._attempts[spec] = self._attempts.get(spec, 0) + 1
            if attempt <= self.fail_first.get(spec, 0):
                return None
            return QuoteSnapshot(
                bid=10.0,
                ask=10.5,
                last=10.25,
                volume=100.0,
                iv=0.15,
                delta=0.5 if spec.right == "C" else -0.5,
                gamma=0.01,
                theta=-0.5,
                vega=1.2,
            )
        finally:
            self._concurrent -= 1


class MockHotZoneClient(HotZoneClientLike):
    """Mock tick-by-tick klienta pro HotZoneCollector.

    `stream_limit` simuluje tvrdý limit účtu — subskripce nad limit vyhodí
    StreamLimitError (obdoba TWS error 10190).
    """

    def __init__(self, *, stream_limit: int | None = None) -> None:
        self.stream_limit = stream_limit
        self.active: set[OptionContractSpec] = set()
        self.subscribe_calls: list[OptionContractSpec] = []
        self.unsubscribe_calls: list[OptionContractSpec] = []

    async def subscribe_ticks(self, spec: OptionContractSpec) -> None:
        self.subscribe_calls.append(spec)
        if self.stream_limit is not None and len(self.active) >= self.stream_limit:
            raise StreamLimitError("mock: error 10190 — max tick-by-tick requests reached")
        self.active.add(spec)

    async def unsubscribe_ticks(self, spec: OptionContractSpec) -> None:
        self.unsubscribe_calls.append(spec)
        self.active.discard(spec)


class MockOIFetcher:
    """Mock zdroje OI pro OIArchiver: hodnoty per kontrakt, chybějící vrací None."""

    def __init__(self, values: dict[OptionContractSpec, float] | None = None) -> None:
        self.values = dict(values or {})
        self.fetch_calls: list[OptionContractSpec] = []

    async def fetch_oi(self, spec: OptionContractSpec, timeout_s: float) -> float | None:
        self.fetch_calls.append(spec)
        await asyncio.sleep(0)
        return self.values.get(spec)


class MockHistoricalClient:
    """Mock historical dat s tvrdou simulací IBKR pacing limitů.

    Každý request nad `max_requests` v klouzavém okně `window_s` vyhodí
    PacingViolationError — přesně to, čemu má PacingGuard zabránit.
    """

    def __init__(
        self,
        *,
        max_requests: int = 60,
        window_s: float = 600.0,
        bars_per_day: int = 3,
    ) -> None:
        self.max_requests = max_requests
        self.window_s = window_s
        self.bars_per_day = bars_per_day
        self.calls: list[tuple[str, dt.date]] = []
        self._request_times: deque[float] = deque()

    async def fetch_day_bars(self, symbol: str, day: dt.date) -> Sequence[Bar]:
        now = time.monotonic()
        while self._request_times and now - self._request_times[0] >= self.window_s:
            self._request_times.popleft()
        if len(self._request_times) >= self.max_requests:
            raise PacingViolationError("mock: historical pacing violation")
        self._request_times.append(now)
        self.calls.append((symbol, day))
        await asyncio.sleep(0)
        start = dt.datetime.combine(day, dt.time(13, 30), tzinfo=dt.UTC)
        return [
            Bar(
                ts=start + dt.timedelta(minutes=i),
                open=100.0 + i,
                high=101.0 + i,
                low=99.0 + i,
                close=100.5 + i,
                volume=10.0,
            )
            for i in range(self.bars_per_day)
        ]
