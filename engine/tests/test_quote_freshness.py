"""Čerstvost kotací a náhrobky při requestu (#1088).

ib_async drží Ticker per conId napříč subskripcemi; bez kontroly čerstvosti
prošel kontrakt, pro který TWS nic neposlala, jako kompletní se starými čísly.
"""

import asyncio
import datetime as dt
from types import SimpleNamespace
from typing import Any, cast

import pytest
from ib_async import IB

from gexlens_engine.adapters import IbOIFetcher, IbQuoteStreamer
from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.ibkr.scheduler import PartialQuote, QuoteSnapshot
from gexlens_engine.ibkr.subscription import ReqIdTombstones

SPEC = OptionContractSpec(
    symbol="ES",
    sec_type="FOP",
    expiry="20260910",
    strike=7600.0,
    right="C",
    exchange="CME",
    trading_class="E2C",
    multiplier="50",
)
OLD_TIME = dt.datetime(2026, 9, 9, 8, 0, tzinfo=dt.UTC)


class _Ticker:
    """Ticker se STARÝMI hodnotami z minulé subskripce — přesně stav v ib_async."""

    def __init__(self, contract: Any) -> None:
        self.contract = contract
        self.time = OLD_TIME
        self.bid = 10.0
        self.ask = 10.5
        self.last = 10.25
        self.volume = 42.0
        self.close = 9.0
        self.callOpenInterest = 1234.0
        self.putOpenInterest = 0.0
        self.modelGreeks: SimpleNamespace | None = SimpleNamespace(
            impliedVol=0.2, delta=0.5, gamma=0.01, theta=-0.1, vega=0.2, undPrice=7600.0
        )


class _FakeIB:
    """Falešné IB: reqMktData vrací tentýž Ticker jako ib_async (reuse), registr reqId."""

    def __init__(self, contract: Any) -> None:
        self.contract = contract
        self.ticker = _Ticker(contract)
        self.next_req_id = 100
        self.wrapper = SimpleNamespace(ticker2ReqId={"mktData": {}})
        self.cancelled: list[Any] = []

    def reqMktData(self, contract: Any, *_args: Any) -> _Ticker:
        self.wrapper.ticker2ReqId["mktData"][self.ticker] = self.next_req_id
        self.next_req_id += 1
        return self.ticker

    def cancelMktData(self, contract: Any) -> None:
        self.wrapper.ticker2ReqId["mktData"].pop(self.ticker, None)
        self.cancelled.append(contract)

    async def qualifyContractsAsync(self, contract: Any) -> list[Any]:
        return [self.contract]

    def deliver_tick(self) -> None:
        """TWS poslala tick — wrapper posune `time` (tcpDataProcessed)."""
        self.ticker.time = OLD_TIME + dt.timedelta(seconds=30)


def _contract() -> SimpleNamespace:
    return SimpleNamespace(
        conId=1, symbol="ES", localSymbol="E2CU6", right="C", strike=7600.0,
        lastTradeDateOrContractMonth="20260910", exchange="CME",
    )  # fmt: skip


@pytest.mark.asyncio
async def test_stare_hodnoty_bez_ticku_neprojdou() -> None:
    """Bez nového ticku po subskripci = žádná kotace, i když ticker hodnoty má."""
    ib = _FakeIB(_contract())
    streamer = IbQuoteStreamer(cast(IB, ib))
    result = await streamer.fetch_quote(SPEC, timeout_s=0.6)
    assert result is None
    assert ib.cancelled  # odhlášeno i při neúspěchu


@pytest.mark.asyncio
async def test_cerstvy_tick_kotaci_pusti() -> None:
    ib = _FakeIB(_contract())
    streamer = IbQuoteStreamer(cast(IB, ib))

    async def tws() -> None:
        await asyncio.sleep(0.3)
        ib.deliver_tick()

    task = asyncio.create_task(tws())
    result = await streamer.fetch_quote(SPEC, timeout_s=2.0)
    await task
    assert isinstance(result, QuoteSnapshot)
    assert result.bid == 10.0 and result.delta == 0.5


@pytest.mark.asyncio
async def test_castecna_kotace_jen_kdyz_je_cerstva() -> None:
    """#547 fallback (bid/ask bez greeks) platí jen pro čerstvé hodnoty."""
    ib = _FakeIB(_contract())
    ib.ticker.modelGreeks = None
    streamer = IbQuoteStreamer(cast(IB, ib))
    assert await streamer.fetch_quote(SPEC, timeout_s=0.6) is None
    ib.deliver_tick()
    result = await streamer.fetch_quote(SPEC, timeout_s=0.6)
    # deliver_tick proběhl PŘED requestem → stále stará hodnota času
    assert result is None
    ib.ticker.time = OLD_TIME + dt.timedelta(seconds=60)  # simuluje tick po requestu

    async def tws() -> None:
        await asyncio.sleep(0.3)
        ib.ticker.time = OLD_TIME + dt.timedelta(seconds=90)

    task = asyncio.create_task(tws())
    result = await streamer.fetch_quote(SPEC, timeout_s=2.0)
    await task
    assert isinstance(result, PartialQuote)


@pytest.mark.asyncio
async def test_nahrobek_se_zapise_pri_requestu() -> None:
    """Každý request dostane náhrobek hned — i 80 requestů v jedné milisekundě."""
    ib = _FakeIB(_contract())
    stones = ReqIdTombstones(ttl_s=900.0, throttle_s=1.0)
    streamer = IbQuoteStreamer(cast(IB, ib), tombstones=stones)
    await asyncio.gather(*(streamer.fetch_quote(SPEC, timeout_s=0.3) for _ in range(3)))
    for req_id in (100, 101, 102):
        hit = stones.lookup(req_id, now=1.0)
        assert hit is not None, req_id
        label, symbol = hit
        assert "E2CU6" in label and symbol == "ES"


@pytest.mark.asyncio
async def test_oi_snapshot_vyzaduje_cerstvy_tick() -> None:
    ib = _FakeIB(_contract())
    streamer = IbQuoteStreamer(cast(IB, ib))
    fetcher = IbOIFetcher(cast(IB, ib), streamer)
    assert await fetcher.fetch_snapshot(SPEC, timeout_s=0.6) is None

    async def tws() -> None:
        await asyncio.sleep(0.3)
        ib.deliver_tick()

    task = asyncio.create_task(tws())
    snapshot = await fetcher.fetch_snapshot(SPEC, timeout_s=2.0)
    await task
    assert snapshot is not None and snapshot.oi == 1234.0
