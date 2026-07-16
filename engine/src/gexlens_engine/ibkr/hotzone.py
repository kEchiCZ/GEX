"""HotZoneCollector (SPEC 3.4 + R2): tick-by-tick sběr a klasifikace agresora v hot zóně.

Hot zóna je dynamická množina kontraktů ATM ± `hot_zone_width` strikes × C/P
s trvalými streamy (nerotuje se). Počet souběžných tick-by-tick streamů je
omezen účtem (ADR-0001: 5) — kolektor množinu degraduje od ATM ven a stav
reportuje. Každý trade je okamžitě klasifikován Lee–Ready pravidlem; přesně
na midu rozhoduje tick test (směr poslední změny ceny kontraktu).
"""

import enum
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from gexlens_engine.config import Settings
from gexlens_engine.ibkr.discovery import OptionContractSpec

logger = logging.getLogger(__name__)


class StreamLimitError(RuntimeError):
    """Účet odmítl další tick-by-tick stream (TWS error 10190)."""


class TradeSide(enum.Enum):
    BUY = "buy"
    SELL = "sell"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ClassifiedTrade:
    """Trade s okamžitou klasifikací agresora (vstup pro CumΔ a ticks Parquet)."""

    spec: OptionContractSpec
    price: float
    size: float
    ts: float
    side: TradeSide


@dataclass(frozen=True)
class HotZoneStatus:
    """Stav hot zóny pro UI: požadovaná vs. skutečná šířka a degradace."""

    requested_width: int
    active_streams: int
    stream_budget: int
    degraded: bool


@dataclass
class _ContractState:
    """Klasifikační stav jednoho kontraktu: poslední kotace a tick test."""

    bid: float | None = None
    ask: float | None = None
    last_price: float | None = None
    last_tick_sign: int = 0  # směr poslední nenulové změny ceny


class HotZoneClientLike:
    """Rozhraní klienta tick-by-tick streamů (mock: MockHotZoneClient).

    Subskripce pokrývá reqTickByTickData(AllLast) + trvalý reqMktData stream
    kontraktu (SPEC 3.4); data tečou zpět přes HotZoneCollector.on_trade/on_quote.
    """

    async def subscribe_ticks(self, spec: OptionContractSpec) -> None:
        raise NotImplementedError

    async def unsubscribe_ticks(self, spec: OptionContractSpec) -> None:
        raise NotImplementedError


TradeCallback = Callable[[ClassifiedTrade], None]
StatusCallback = Callable[[HotZoneStatus], None]


def classify_lee_ready(price: float, bid: float, ask: float, last_tick_sign: int) -> TradeSide:
    """Lee–Ready: cena ≥ ask → buy, ≤ bid → sell, jinak vs. mid; na midu tick test."""
    if price >= ask:
        return TradeSide.BUY
    if price <= bid:
        return TradeSide.SELL
    mid = (bid + ask) / 2
    if price > mid:
        return TradeSide.BUY
    if price < mid:
        return TradeSide.SELL
    if last_tick_sign > 0:
        return TradeSide.BUY
    if last_tick_sign < 0:
        return TradeSide.SELL
    return TradeSide.UNKNOWN


class HotZoneCollector:
    """Spravuje množinu trvalých tick-by-tick streamů kolem ATM a klasifikuje trades."""

    def __init__(self, client: HotZoneClientLike, settings: Settings) -> None:
        self._client = client
        self._settings = settings
        self._stream_budget = settings.tick_by_tick_max_streams
        self._active: set[OptionContractSpec] = set()
        self._states: dict[OptionContractSpec, _ContractState] = {}
        self._last_spot: float | None = None
        self._trade_callbacks: list[TradeCallback] = []
        self._status_callbacks: list[StatusCallback] = []

    @property
    def active_contracts(self) -> set[OptionContractSpec]:
        return set(self._active)

    @property
    def status(self) -> HotZoneStatus:
        return HotZoneStatus(
            requested_width=self._settings.hot_zone_width,
            active_streams=len(self._active),
            stream_budget=self._stream_budget,
            degraded=self._is_degraded(),
        )

    def on_trade_classified(self, callback: TradeCallback) -> None:
        self._trade_callbacks.append(callback)

    def on_status(self, callback: StatusCallback) -> None:
        self._status_callbacks.append(callback)

    async def rebalance(self, contracts: Sequence[OptionContractSpec], spot: float) -> None:
        """Přepočet množiny při pohybu spotu o ≥ 1 strike krok (SPEC 3.4).

        Kontrakty, které v zóně zůstávají, si běžící stream ponechají —
        odhlašují se jen ty, které vypadly z okraje.
        """
        strikes = sorted({c.strike for c in contracts})
        if len(strikes) < 2:
            return
        step = min(b - a for a, b in zip(strikes, strikes[1:], strict=False))
        if self._last_spot is not None and abs(spot - self._last_spot) < step:
            return
        self._last_spot = spot

        desired = self._desired_contracts(contracts, strikes, spot)
        for spec in self._active - desired:
            await self._client.unsubscribe_ticks(spec)
            self._active.discard(spec)
            self._states.pop(spec, None)
        for spec in sorted(desired - self._active, key=lambda c: abs(c.strike - spot)):
            if len(self._active) >= self._stream_budget:
                break
            try:
                await self._client.subscribe_ticks(spec)
            except StreamLimitError:
                # Účet povolil méně streamů, než čekáme — degraduj budget na realitu
                self._stream_budget = len(self._active)
                logger.warning(
                    "Tick-by-tick limit dosažen: budget degradován na %d streamů",
                    self._stream_budget,
                )
                break
            self._active.add(spec)
        self._emit_status()

    def on_quote(self, spec: OptionContractSpec, bid: float, ask: float) -> None:
        """Průběžný bid/ask z trvalého reqMktData streamu hot zóny."""
        state = self._states.setdefault(spec, _ContractState())
        state.bid = bid
        state.ask = ask

    def on_trade(self, spec: OptionContractSpec, price: float, size: float, ts: float) -> None:
        """Trade z reqTickByTickData(AllLast): okamžitá klasifikace a emise."""
        state = self._states.setdefault(spec, _ContractState())
        if state.bid is None or state.ask is None:
            side = TradeSide.UNKNOWN  # bez kotace nelze klasifikovat — explicitně neznámé
        else:
            side = classify_lee_ready(price, state.bid, state.ask, state.last_tick_sign)
        if state.last_price is not None and price != state.last_price:
            state.last_tick_sign = 1 if price > state.last_price else -1
        state.last_price = price

        trade = ClassifiedTrade(spec=spec, price=price, size=size, ts=ts, side=side)
        for callback in self._trade_callbacks:
            callback(trade)

    def _desired_contracts(
        self,
        contracts: Sequence[OptionContractSpec],
        strikes: list[float],
        spot: float,
    ) -> set[OptionContractSpec]:
        """ATM ± hot_zone_width strikes, ořezané na stream budget po párech C/P od ATM ven."""
        atm_index = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
        low = max(0, atm_index - self._settings.hot_zone_width)
        high = atm_index + self._settings.hot_zone_width
        zone_strikes = sorted(strikes[low : high + 1], key=lambda k: abs(k - spot))

        by_strike: dict[float, list[OptionContractSpec]] = {}
        for contract in contracts:
            by_strike.setdefault(contract.strike, []).append(contract)

        desired: set[OptionContractSpec] = set()
        for strike in zone_strikes:
            pair = by_strike.get(strike, [])
            if len(desired) + len(pair) > self._stream_budget:
                break
            desired.update(pair)
        return desired

    def _is_degraded(self) -> bool:
        return (
            self._stream_budget < self._settings.tick_by_tick_max_streams
            or self._stream_budget < 2 * (2 * self._settings.hot_zone_width + 1)
        )

    def _emit_status(self) -> None:
        status = self.status
        if status.degraded:
            logger.info(
                "Hot zóna degradována: %d/%d streamů (požadovaná šířka ATM±%d)",
                status.active_streams,
                status.stream_budget,
                status.requested_width,
            )
        for callback in self._status_callbacks:
            callback(status)
