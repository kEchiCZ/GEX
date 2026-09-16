"""Simulátor paper účtu (#1187 fáze 1, ADR-0040): fily proti 1min barům.

`PaperBroker.on_minute` běží per symbol po cyklu pipeline: čekající ordery
plní proti barům minuty (konvence v `compute.paper`), otevřené pozice hlídá
stop/cíl (stop-first v jednom baru), ruční zavření a kill switch provede
na open dalšího baru, v settle seance vše zavře (denní ordery). Uzavřený
obchod zapíše do deníku (typ `obchod`, tag `paper`) — vstup kouče (#933).
Chyba čehokoli tady nesmí shodit sběr dat — volající balí do try/except.
"""

import asyncio
import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from gexlens_engine.compute.paper import (
    POINT_VALUES,
    Exit,
    PaperOrder,
    evaluate_open,
    excursions,
    fill_entry,
    pnl_points,
    r_multiple,
)
from gexlens_engine.compute.settle import settle_ts, trading_session_date
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.runtime import PublisherLike
from gexlens_engine.storage.paper_store import PaperRepository

logger = logging.getLogger(__name__)


@dataclass
class PaperBroker:
    symbol: str
    repository: PaperRepository
    publisher: PublisherLike
    #: Poplatek za kontrakt a obchod (round-trip) v jednotkách účtu (ADR-0038: 10 $)
    fee_per_contract_usd: float = 10.0
    slippage_ticks: int = 1
    _settled_for: dt.date | None = field(default=None, init=False)

    async def on_minute(
        self, now: dt.datetime, spot: float, bars: Sequence[Bar], runtime: object | None = None
    ) -> None:
        orders = await asyncio.to_thread(self.repository.active, self.symbol)
        if not orders:
            self._settled_for = None
            return
        session_day = trading_session_date(now)
        settle = settle_ts(session_day)
        points: list[Bar] = (
            sorted(bars, key=lambda bar: bar.ts)
            if bars
            else [Bar(ts=now, open=spot, high=spot, low=spot, close=spot, volume=0.0)]
        )
        for order in orders:
            try:
                await self._process(order, points, now, settle)
            except Exception:
                logger.exception("Paper order #%d %s selhal — pokračuji", order.id, self.symbol)

    async def _process(
        self, order: PaperOrder, points: Sequence[Bar], now: dt.datetime, settle: dt.datetime
    ) -> None:
        current = order
        for bar in points:
            if bar.ts >= settle:
                break
            if current.status == "working":
                if current.close_requested:
                    await self._cancel(current, now, "manual")
                    return
                fill = fill_entry(current, bar, slippage_ticks=self.slippage_ticks)
                if fill is None:
                    continue
                current = replace(current, status="open", fill_price=fill.price, filled_ts=fill.ts)
                await asyncio.to_thread(
                    self.repository.update_order,
                    current.id,
                    status="open",
                    fill_price=fill.price,
                    filled_ts=fill.ts,
                )
                await self._publish(
                    current,
                    "filled",
                    f"Paper fill #{current.id} {current.side.upper()} "
                    f"{current.qty}× {current.symbol} @ {fill.price:g}",
                )
                # Tentýž bar může hned zasáhnout stop/cíl (konzervativně stop-first)
            if current.status == "open":
                current = excursions(current, bar)
                if current.close_requested:
                    await self._close(
                        current, Exit(price=bar.open, ts=bar.ts, reason="manual"), now
                    )
                    return
                exit_ = evaluate_open(current, bar, slippage_ticks=self.slippage_ticks)
                if exit_ is not None:
                    await self._close(current, exit_, now)
                    return
        if current.status == "open":
            await asyncio.to_thread(
                self.repository.update_order, current.id, mfe=current.mfe, mae=current.mae
            )
        # Settle seance: denní ordery končí — pozice na close, čekající zrušit
        if now >= settle and points:
            last = points[-1]
            if current.status == "open":
                await self._close(current, Exit(price=last.close, ts=now, reason="settle"), now)
            elif current.status == "working":
                await self._cancel(current, now, "settle")

    async def _cancel(self, order: PaperOrder, now: dt.datetime, reason: str) -> None:
        await asyncio.to_thread(
            self.repository.update_order,
            order.id,
            status="cancelled",
            closed_ts=now,
            exit_reason=reason,
        )
        await self._publish(order, "cancelled", f"Paper order #{order.id} zrušen ({reason})")

    async def _close(self, order: PaperOrder, exit_: Exit, now: dt.datetime) -> None:
        point_value = POINT_VALUES.get(order.symbol, 0.0)
        points = pnl_points(order, exit_.price)
        fees = self.fee_per_contract_usd * order.qty
        pnl = points * order.qty * point_value - fees
        result_r = r_multiple(order, exit_.price)
        await asyncio.to_thread(
            self.repository.update_order,
            order.id,
            status="closed",
            closed_ts=exit_.ts,
            exit_price=exit_.price,
            exit_reason=exit_.reason,
            pnl_usd=pnl,
            fees_usd=fees,
            r_multiple=result_r,
            mfe=order.mfe,
            mae=order.mae,
        )
        stored = await asyncio.to_thread(self.repository.get_order, order.id)
        if stored is not None:
            label = {
                "stop": "stop",
                "target": "cíl",
                "manual": "ruční výstup",
                "settle": "settle seance",
                "kill": "kill switch",
            }.get(exit_.reason, exit_.reason)
            text_body = (
                f"Paper obchod #{order.id} {order.side.upper()} {order.qty}× {order.symbol}: "
                f"vstup {order.fill_price:g}, výstup {exit_.price:g} ({label}), "
                f"{result_r:+.2f} R, {pnl:+.0f} $ po poplatcích."
            )
            context = {
                "paper_order_id": order.id,
                "exit_reason": exit_.reason,
                "r_multiple": result_r,
                "pnl_usd": pnl,
                "fees_usd": fees,
                "mfe": order.mfe,
                "mae": order.mae,
                **(stored.get("context") or {}),
            }
            try:
                await asyncio.to_thread(
                    self.repository.journal_trade, stored, text_body=text_body, context=context
                )
            except Exception:
                logger.exception("Zápis paper obchodu #%d do deníku selhal", order.id)
        await self._publish(
            order,
            "closed",
            f"Paper obchod #{order.id} uzavřen: {exit_.reason}, {result_r:+.2f} R, {pnl:+.0f} $",
        )

    async def _publish(self, order: PaperOrder, event: str, message: str) -> None:
        await self.publisher.publish(
            "alerts",
            {
                "kind": "paper",
                "event": event,
                "symbol": order.symbol,
                "message": message,
                "ts": dt.datetime.now(dt.UTC).timestamp(),
            },
        )
        await self.publisher.publish(f"paper.{order.symbol}", {"event": event, "id": order.id})
