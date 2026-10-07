"""Simulátor paper účtu (#1187 fáze 1, ADR-0040): fily proti 1min barům.

`PaperBroker.on_minute` běží per symbol po cyklu pipeline: čekající ordery
plní proti barům minuty (konvence v `compute.paper`), otevřené pozice hlídá
stop/cíl (stop-first v jednom baru), ruční zavření a kill switch provede
na open dalšího baru, v settle seance vše zavře (denní ordery).

Po cestě ceny jako živé setupy (#1345, pravidla `compute/setups.walk_setup_path`):
jen bary, nikdy spot (dřív cyklus bez barů hodnotil rovnou čáru zamrzlého spotu
— výpadek 3. 9. 14:17–15:30 UTC by pozici se stopem v díře zapsal jako výhru),
v pořadí od posledního vyhodnoceného baru orderu, díru v živé dávce dotáhne
z partic (`bar_reader`) a na nedoplněnou čeká `PATH_GAP_WAIT`. V settle seance
zavře pozici za close baru končícího v settle, `closed_ts` = settle. Uzavřený
obchod zapíše do deníku (typ `obchod`, tag `paper`) — vstup kouče (#933).
Chyba čehokoli tady nesmí shodit sběr dat — volající balí do try/except.
"""

import asyncio
import datetime as dt
import logging
from collections.abc import Callable, Sequence
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
from gexlens_engine.compute.setups import PATH_GAP_WAIT, last_expected_minute, missing_minutes
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.runtime import PublisherLike
from gexlens_engine.storage.paper_store import PaperRepository
from gexlens_engine.ticker import symbol_root

logger = logging.getLogger(__name__)


_MINUTE = dt.timedelta(minutes=1)
#: Bary z partic jen do minuty now − 2 — vzor `setups.STORED_BAR_LAG` (#1320)
STORED_BAR_LAG = dt.timedelta(minutes=2)


@dataclass
class _OrderPath:
    """Kam až je cesta ceny orderu vyhodnocená (#1345) a na jakou díru čeká."""

    last_ts: dt.datetime
    last_close: float | None = None
    waiting_gap: tuple[dt.datetime, dt.datetime] | None = None
    blocked_since: dt.datetime | None = None


@dataclass
class PaperBroker:
    symbol: str
    repository: PaperRepository
    publisher: PublisherLike
    #: Poplatek za kontrakt a obchod (round-trip) v jednotkách účtu (ADR-0038: 10 $)
    fee_per_contract_usd: float = 10.0
    slippage_ticks: int = 1
    #: Bary z partic pro díry v živé dávce (#1345, týž zdroj jako SetupEngine)
    bar_reader: Callable[[dt.datetime, dt.datetime], Sequence[Bar]] | None = None
    _paths: dict[int, _OrderPath] = field(default_factory=dict, init=False)

    async def on_minute(
        self, now: dt.datetime, spot: float, bars: Sequence[Bar], runtime: object | None = None
    ) -> None:
        orders = await asyncio.to_thread(self.repository.active, self.symbol)
        live_ids = {order.id for order in orders}
        for stale in set(self._paths) - live_ids:
            del self._paths[stale]
        if not orders:
            return
        session_day = trading_session_date(now)
        settle = settle_ts(session_day)
        paths = {order.id: self._path_of(order) for order in orders}
        live = sorted(bars, key=lambda bar: bar.ts)
        # Partice první, živá dávka po ní: táž minuta z živé dávky vyhrává
        by_ts = {bar.ts: bar for bar in [*await self._stored_bars(now, live, paths), *live]}
        points = [by_ts[ts] for ts in sorted(by_ts)]
        for order in orders:
            try:
                await self._process(order, points, now, settle)
            except Exception:
                logger.exception("Paper order #%d %s selhal — pokračuji", order.id, self.symbol)

    def _path_of(self, order: PaperOrder) -> _OrderPath:
        """Stav cesty orderu; po restartu od vzniku (čekající) nebo od baru fillu.

        Přehrání už viděných barů je bezpečné: fill i výstup jsou nad týmiž bary
        deterministické a MFE/MAE se berou maximem.
        """
        path = self._paths.get(order.id)
        if path is not None:
            return path
        if order.status == "open" and order.filled_ts is not None:
            start = order.filled_ts - _MINUTE  # bar fillu se hodnotí znovu (stop/cíl)
        else:
            created = order.created_ts or dt.datetime.now(dt.UTC)
            # Bar minuty vzniku začal před orderem — první je až následující
            start = created.replace(second=0, microsecond=0)
        path = self._paths[order.id] = _OrderPath(last_ts=start)
        return path

    async def _stored_bars(
        self, now: dt.datetime, live: Sequence[Bar], paths: dict[int, _OrderPath]
    ) -> list[Bar]:
        """Bary z partic, když živá dávka na cestu některého orderu nenavazuje."""
        if self.bar_reader is None:
            return []
        horizon = now.replace(second=0, microsecond=0) - STORED_BAR_LAG
        behind: list[dt.datetime] = []
        for path in paths.values():
            if path.last_ts >= horizon or missing_minutes(path.last_ts, horizon + _MINUTE) is None:
                continue
            following = next((bar for bar in live if bar.ts > path.last_ts), None)
            if following is not None and missing_minutes(path.last_ts, following.ts) is None:
                continue
            behind.append(path.last_ts)
        if not behind:
            return []
        try:
            return list(await asyncio.to_thread(self.bar_reader, min(behind), horizon))
        except Exception:
            logger.exception("Paper %s: čtení barů z partic selhalo — čeká se", self.symbol)
            return []

    @staticmethod
    def _forced_until(path: _OrderPath, now: dt.datetime) -> dt.datetime | None:
        """Konec díry, na kterou order čekal `PATH_GAP_WAIT` — tu už smí přejít."""
        if (
            path.waiting_gap is not None
            and path.blocked_since is not None
            and now - path.blocked_since >= PATH_GAP_WAIT
        ):
            return path.waiting_gap[1]
        return None

    @staticmethod
    def _block(path: _OrderPath, gap: tuple[dt.datetime, dt.datetime], now: dt.datetime) -> None:
        if path.blocked_since is None:
            path.waiting_gap, path.blocked_since = gap, now

    async def _process(
        self, order: PaperOrder, points: Sequence[Bar], now: dt.datetime, settle: dt.datetime
    ) -> None:
        path = self._path_of(order)
        force_until = self._forced_until(path, now)
        current = order
        if current.status == "working" and current.close_requested:
            # Zrušení čekajícího orderu cenu nepotřebuje — hned, ne až s barem
            await self._cancel(current, now, "manual")
            return
        for bar in points:
            if bar.ts <= path.last_ts:
                continue
            if bar.ts >= settle:
                break
            hole = missing_minutes(path.last_ts, bar.ts)
            if hole is not None and (force_until is None or hole[0] > force_until):
                # Díra v cestě: nic se nevymýšlí, order počká na doplnění
                self._block(path, hole, now)
                await self._store_excursions(current)
                return
            path.last_ts, path.last_close = bar.ts, bar.close
            path.waiting_gap = path.blocked_since = None
            if current.status == "working":
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
        await self._store_excursions(current)
        # Settle seance: denní ordery končí — pozice za close baru končícího
        # v settle (`closed_ts` = settle), čekající zrušit. Chybí-li bary až
        # k němu, je to díra jako každá jiná.
        if now < settle:
            return
        if current.status == "working":
            await self._cancel(current, now, "settle")
            return
        end = last_expected_minute(settle)
        tail = missing_minutes(path.last_ts, end + _MINUTE) if end is not None else None
        if tail is not None and (force_until is None or tail[0] > force_until):
            self._block(path, tail, now)
            return
        if current.status == "open":
            assert current.fill_price is not None
            price = path.last_close if path.last_close is not None else current.fill_price
            await self._close(current, Exit(price=price, ts=settle, reason="settle"), now)

    async def _store_excursions(self, order: PaperOrder) -> None:
        if order.status == "open":
            await asyncio.to_thread(
                self.repository.update_order, order.id, mfe=order.mfe, mae=order.mae
            )

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
        point_value = POINT_VALUES.get(symbol_root(order.symbol), 0.0)
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
                **await asyncio.to_thread(self.repository.stop_move_summary, stored),
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
