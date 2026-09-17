"""Paper účet uvnitř GEXLens (#1187 fáze 1, ADR-0040) — čisté funkce.

Ordery zadává uživatel v aplikaci, fily simuluje engine proti živé ceně
(1min bary). Konvence jsou záměrně konzervativní, stejné jako u setupů:

- **market** se plní na open dalšího baru + 1 tick proti (slippage);
- **limit** se plní, když bar úroveň protne (gap přes úroveň = fill na open);
- **stop vstup** se plní na max(úroveň, open) + tick proti;
- v jednom baru, který zasáhne stop i cíl, vyhrává **stop** (`evaluate_bar`);
- výstup na stop nese 1 tick slippage proti, cíl se plní přesně;
- vše je denní: v settle seance se pozice zavřou na close a čekající
  ordery zruší.

Účet se vede v jednotkách plného kontraktu (ADR-0038: 1 kontrakt zde =
1 mikro reálně, dolary ×10).
"""

import datetime as dt
from dataclasses import dataclass, replace
from typing import Literal

from gexlens_engine.compute.setups import Direction, Outcome, evaluate_bar
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.ticker import symbol_root

#: Hodnota bodu plného kontraktu (CME) — sizing a P/L; neznámý symbol = order se odmítne
POINT_VALUES: dict[str, float] = {
    "ES": 50.0,
    "NQ": 20.0,
    "RTY": 50.0,
    "YM": 5.0,
    "MES": 5.0,
    "MNQ": 2.0,
    "M2K": 5.0,
    "MYM": 0.5,
}
#: Minimální krok ceny — slippage 1 tick u market/stop filů
TICK_SIZES: dict[str, float] = {
    "ES": 0.25,
    "NQ": 0.25,
    "RTY": 0.1,
    "YM": 1.0,
    "MES": 0.25,
    "MNQ": 0.25,
    "M2K": 0.1,
    "MYM": 1.0,
}

Side = Literal["long", "short"]
OrderType = Literal["market", "limit", "stop"]
Status = Literal["working", "open", "closed", "cancelled", "rejected"]
ExitReason = Literal["stop", "target", "manual", "settle", "kill"]


@dataclass(frozen=True)
class PaperOrder:
    id: int
    symbol: str
    side: Side
    qty: int
    order_type: OrderType
    entry_price: float
    stop_price: float
    target_price: float | None
    status: Status
    fill_price: float | None = None
    filled_ts: dt.datetime | None = None
    close_requested: bool = False
    mfe: float = 0.0
    mae: float = 0.0

    @property
    def direction(self) -> Direction:
        return Direction.LONG if self.side == "long" else Direction.SHORT

    @property
    def sign(self) -> float:
        return 1.0 if self.side == "long" else -1.0


@dataclass(frozen=True)
class Fill:
    price: float
    ts: dt.datetime


@dataclass(frozen=True)
class Exit:
    price: float
    ts: dt.datetime
    reason: ExitReason


def tick_of(symbol: str) -> float:
    return TICK_SIZES.get(symbol_root(symbol), 0.25)


def fill_entry(order: PaperOrder, bar: Bar, *, slippage_ticks: int = 1) -> Fill | None:
    """Vstupní fill čekajícího orderu proti baru; None = bar úroveň neprotnul."""
    tick = tick_of(order.symbol) * slippage_ticks
    adverse = tick * order.sign  # long platí víc, short dostane míň
    if order.order_type == "market":
        return Fill(price=bar.open + adverse, ts=bar.ts)
    level = order.entry_price
    if order.order_type == "limit":
        if order.side == "long":
            if bar.low <= level:
                return Fill(price=min(level, bar.open), ts=bar.ts)
        elif bar.high >= level:
            return Fill(price=max(level, bar.open), ts=bar.ts)
        return None
    # stop vstup: průraz úrovně ve směru obchodu
    if order.side == "long":
        if bar.high >= level:
            return Fill(price=max(level, bar.open) + adverse, ts=bar.ts)
    elif bar.low <= level:
        return Fill(price=min(level, bar.open) + adverse, ts=bar.ts)
    return None


def evaluate_open(order: PaperOrder, bar: Bar, *, slippage_ticks: int = 1) -> Exit | None:
    """Výstup otevřené pozice barem: stop (s tickem proti) má přednost před cílem."""
    assert order.fill_price is not None
    target = order.target_price
    # Bez cíle se posílá nedosažitelný cíl — evaluate_bar chce číslo
    far = order.fill_price + order.sign * 1e9
    outcome = evaluate_bar(
        order.direction,
        order.fill_price,
        target if target is not None else far,
        order.stop_price,
        bar.high,
        bar.low,
    )
    if outcome is Outcome.STOP:
        tick = tick_of(order.symbol) * slippage_ticks
        # Gap přes stop: plní se na open, ne na stopu
        base = (
            min(order.stop_price, bar.open)
            if order.side == "long"
            else max(order.stop_price, bar.open)
        )
        return Exit(price=base - tick * order.sign, ts=bar.ts, reason="stop")
    if outcome is Outcome.TARGET and target is not None:
        return Exit(price=target, ts=bar.ts, reason="target")
    return None


def excursions(order: PaperOrder, bar: Bar) -> PaperOrder:
    """MFE/MAE v bodech (kladné = ve prospěch / v neprospěch pozice)."""
    assert order.fill_price is not None
    favourable = (
        (bar.high - order.fill_price) if order.side == "long" else (order.fill_price - bar.low)
    )
    adverse = (
        (order.fill_price - bar.low) if order.side == "long" else (bar.high - order.fill_price)
    )
    return replace(order, mfe=max(order.mfe, favourable), mae=max(order.mae, adverse))


def pnl_points(order: PaperOrder, exit_price: float) -> float:
    assert order.fill_price is not None
    return (exit_price - order.fill_price) * order.sign


def r_multiple(order: PaperOrder, exit_price: float) -> float:
    """R = výsledek / plánovaný risk (|entry − stop| z fillu)."""
    assert order.fill_price is not None
    risk = abs(order.fill_price - order.stop_price)
    if risk <= 0:
        return 0.0
    return pnl_points(order, exit_price) / risk


def validate_levels(
    side: Side, order_type: OrderType, entry: float, stop: float, target: float | None
) -> str | None:
    """Text chyby, nebo None. Stop musí ležet proti směru, cíl ve směru."""
    if entry <= 0 or stop <= 0:
        return "ceny musí být kladné"
    if side == "long":
        if stop >= entry:
            return "stop musí být pod entry (long)"
        if target is not None and target <= entry:
            return "cíl musí být nad entry (long)"
    else:
        if stop <= entry:
            return "stop musí být nad entry (short)"
        if target is not None and target >= entry:
            return "cíl musí být pod entry (short)"
    return None
