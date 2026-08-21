"""TastyProvider (#613): živá cache řetězce z DXLink eventů — shadow fáze.

Na rozdíl od IBKR rotace drží tasty CELOU množinu kontraktů subskribovanou
trvale (sonda #612: 6 000+ symbolů bez degradace) a cache nese poslední
Quote/Greeks/Summary per symbol + počítadla TimeAndSale. V shadow fázi z ní
čte výhradně porovnávací smyčka — nic jiného (zadání: ani řádek IBKR cesty
se nemění, nic se nepublikuje).
"""

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


def _number(value: object) -> float | None:
    """dxFeed posílá NaN jako string „NaN" a prázdno jako null — obojí None."""
    if isinstance(value, int | float):
        return float(value) if value == value else None
    if isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError:
            return None
        return parsed if parsed == parsed else None
    return None


@dataclass
class TastyQuote:
    bid: float | None = None
    ask: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    updated_at: dt.datetime | None = None


@dataclass
class TastyGreeks:
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    theo_price: float | None = None
    updated_at: dt.datetime | None = None


@dataclass
class TastySummary:
    open_interest: float | None = None
    updated_at: dt.datetime | None = None


@dataclass
class TastyContractState:
    """Poslední známý stav jednoho streamer symbolu."""

    quote: TastyQuote = field(default_factory=TastyQuote)
    greeks: TastyGreeks = field(default_factory=TastyGreeks)
    summary: TastySummary = field(default_factory=TastySummary)
    #: TimeAndSale počítadla (pokrytí agresora pro report #612/#615)
    trades: int = 0
    trades_with_aggressor: int = 0


class TastyChainCache:
    """Cache stavů per streamer symbol; plní ji callback z DxLinkStream."""

    def __init__(self, clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC)) -> None:
        self._clock = clock
        self._states: dict[str, TastyContractState] = {}
        #: Čas posledního přijatého eventu (#706) — „jak čerstvá větev je"
        self.last_event_at: dt.datetime | None = None

    def state(self, streamer_symbol: str) -> TastyContractState | None:
        return self._states.get(streamer_symbol)

    def symbols_tracked(self) -> int:
        return len(self._states)

    def field_counts(self) -> dict[str, int]:
        """Diagnostika pokrytí (#623): kolik symbolů má quote/greeks/OI a Σ trades."""
        quotes = greeks = summary = trades = 0
        for state in self._states.values():
            if state.quote.updated_at is not None:
                quotes += 1
            if state.greeks.updated_at is not None:
                greeks += 1
            if state.summary.open_interest is not None:
                summary += 1
            trades += state.trades
        return {"quotes": quotes, "greeks": greeks, "summary": summary, "trades": trades}

    def on_event(self, event_type: str, values: list[object]) -> None:
        """EventCallback pro DxLinkStream — pořadí polí dle stream.EVENT_FIELDS."""
        symbol = str(values[0]) if values else ""
        if not symbol:
            return
        state = self._states.setdefault(symbol, TastyContractState())
        now = self._clock()
        self.last_event_at = now
        if event_type == "Quote":
            state.quote = TastyQuote(
                bid=_number(values[1]),
                ask=_number(values[2]),
                bid_size=_number(values[3]),
                ask_size=_number(values[4]),
                updated_at=now,
            )
        elif event_type == "Greeks":
            state.greeks = TastyGreeks(
                iv=_number(values[1]),
                delta=_number(values[2]),
                gamma=_number(values[3]),
                theta=_number(values[4]),
                vega=_number(values[5]),
                theo_price=_number(values[6]),
                updated_at=now,
            )
        elif event_type == "Summary":
            state.summary = TastySummary(open_interest=_number(values[1]), updated_at=now)
        elif event_type == "TimeAndSale":
            state.trades += 1
            aggressor = values[4] if len(values) > 4 else None
            if isinstance(aggressor, str) and aggressor.upper() in ("BUY", "SELL"):
                state.trades_with_aggressor += 1
