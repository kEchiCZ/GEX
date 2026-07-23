"""Produkční adaptéry nad ib_async pro runtime (mimo CI — CLAUDE.md pravidlo 4).

Implementují protokoly z scheduleru/OI archivu/hot zóny nad skutečným TWS/Gateway
spojením a HTTP publisher do API serveru.
"""

import asyncio
import datetime as dt
import logging
import math
from typing import Any, cast

import httpx
from ib_async import IB, Contract, FuturesOption, Option

from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.ibkr.scheduler import QuoteSnapshot
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.runtime import PublisherLike

logger = logging.getLogger(__name__)


def spec_to_contract(spec: OptionContractSpec) -> Contract:
    if spec.sec_type == "FOP":
        return FuturesOption(
            spec.symbol,
            spec.expiry,
            spec.strike,
            spec.right,
            spec.exchange,
            tradingClass=spec.trading_class,
        )
    return Option(spec.symbol, spec.expiry, spec.strike, spec.right, spec.exchange)


def _valid(value: float | None) -> bool:
    return value is not None and not math.isnan(value)


class IbQuoteStreamer:
    """QuoteStreamerLike nad reqMktData: subskribce → kompletní sada → odsubskribce."""

    def __init__(self, ib: IB) -> None:
        self._ib = ib
        self._qualified: dict[OptionContractSpec, Contract] = {}

    async def _contract(self, spec: OptionContractSpec) -> Contract | None:
        cached = self._qualified.get(spec)
        if cached is not None:
            return cached
        results = cast(
            "list[Contract | None]", await self._ib.qualifyContractsAsync(spec_to_contract(spec))
        )
        first = results[0] if results else None
        if first is None or not first.conId:
            return None
        self._qualified[spec] = first
        return first

    async def fetch_quote(self, spec: OptionContractSpec, timeout_s: float) -> QuoteSnapshot | None:
        contract = await self._contract(spec)
        if contract is None:
            return None
        ticker = self._ib.reqMktData(contract, "", False, False)
        try:
            deadline = asyncio.get_running_loop().time() + timeout_s
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.25)
                greeks = ticker.modelGreeks
                if greeks is None:
                    continue
                delta = greeks.delta
                gamma = greeks.gamma
                quotes_ok = _valid(ticker.bid) and _valid(ticker.ask)
                if not (quotes_ok and _valid(delta) and _valid(gamma)):
                    continue
                assert delta is not None and gamma is not None  # _valid výše
                iv = greeks.impliedVol
                theta = greeks.theta
                vega = greeks.vega
                return QuoteSnapshot(
                    bid=ticker.bid,
                    ask=ticker.ask,
                    last=ticker.last if _valid(ticker.last) else (ticker.bid + ticker.ask) / 2,
                    volume=ticker.volume if _valid(ticker.volume) else 0.0,
                    iv=iv if iv is not None and _valid(iv) else 0.0,
                    delta=delta,
                    gamma=gamma,
                    theta=theta if theta is not None and _valid(theta) else 0.0,
                    vega=vega if vega is not None and _valid(vega) else 0.0,
                )
            return None
        finally:
            self._ib.cancelMktData(contract)


class IbOIFetcher:
    """OIFetcherLike: generic tick 101 (call/put OI) — funguje pro OPT i FOP.

    Tick 588 (futures OI) na FOP kontraktech nedodává nic (změřeno živě,
    issue #65/ADR-0001); tick 101 vrací callOpenInterest/putOpenInterest.
    Hodnota se čte podle strany kontraktu — druhá strana bývá validní 0.0
    a nesmí se zaměnit.
    """

    def __init__(self, ib: IB, streamer: IbQuoteStreamer) -> None:
        self._ib = ib
        self._streamer = streamer

    async def fetch_oi(self, spec: OptionContractSpec, timeout_s: float) -> float | None:
        contract = await self._streamer._contract(spec)  # sdílená kvalifikační cache
        if contract is None:
            return None
        ticker = self._ib.reqMktData(contract, "101", False, False)
        try:
            deadline = asyncio.get_running_loop().time() + timeout_s
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.25)
                value = (
                    getattr(ticker, "callOpenInterest", None)
                    if spec.right == "C"
                    else getattr(ticker, "putOpenInterest", None)
                )
                if _valid(value):
                    return float(value)  # type: ignore[arg-type]
            return None
        finally:
            self._ib.cancelMktData(contract)


class IbHistoricalClient:
    """HistoricalClientLike nad reqHistoricalData: 1min bary jednoho dne (SPEC 3.6).

    Kontrakt podkladu (front future) dodává konstrukce — instance per pipeline;
    rate limit requestů drží PacingGuard nad tímto klientem, ne klient sám.
    """

    _REQUEST_TIMEOUT_S = 60.0

    def __init__(self, ib: IB, contract: Contract) -> None:
        self._ib = ib
        self._contract = contract

    async def fetch_day_bars(self, symbol: str, day: dt.date) -> list[Bar]:
        # endDateTime = půlnoc UTC následujícího dne; "1 D" může přetéct do
        # sousedních dní, filtr níže drží jen požadovaný den. Timeout chrání
        # před visícím awaitem na mrtvé HMDS farmě (#221) — přesně ten stav,
        # kvůli kterému se backfill spouští.
        end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0, 0), tzinfo=dt.UTC)
        raw = await asyncio.wait_for(
            self._ib.reqHistoricalDataAsync(
                self._contract,
                endDateTime=end,
                durationStr="1 D",
                barSizeSetting="1 min",
                whatToShow="TRADES",
                useRTH=False,
                formatDate=2,
            ),
            timeout=self._REQUEST_TIMEOUT_S,
        )
        bars: list[Bar] = []
        for item in raw:
            ts = item.date
            if not isinstance(ts, dt.datetime):
                continue  # denní bar (date) sem nepatří
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt.UTC)
            if ts.astimezone(dt.UTC).date() != day:
                continue
            bars.append(
                Bar(
                    ts=ts.astimezone(dt.UTC),
                    open=item.open,
                    high=item.high,
                    low=item.low,
                    close=item.close,
                    volume=float(item.volume),
                )
            )
        return bars


class HttpPublisher(PublisherLike):
    """Push stavu a kanálů do API serveru přes interní ingest endpoints."""

    def __init__(self, api_base: str) -> None:
        self._client = httpx.AsyncClient(base_url=api_base, timeout=5.0)

    async def status(self, **fields: Any) -> None:
        try:
            await self._client.post("/internal/status", json=fields)
        except httpx.HTTPError as exc:
            logger.warning("Push stavu do API selhal: %s", exc)

    async def publish(self, channel: str, data: dict[str, Any]) -> None:
        try:
            await self._client.post("/internal/publish", json={"channel": channel, "data": data})
        except httpx.HTTPError as exc:
            logger.warning("Publish %s do API selhal: %s", channel, exc)

    async def close(self) -> None:
        await self._client.aclose()
