"""Ad-hoc pohled na symbol přes tastytrade (#521, varianta C — rozhodnutí 27. 8.).

Uživatel si vyhledá libovolný CME produkt a dostane jeho positioning BEZ
zásahu do IBKR market data lines (strop 100 je vyčerpaný watchlistem) a bez
restartu enginu. Mechanika = extended expirace (#616): chain z tasty, kotace
a greeks z dxFeed (BS z mid), minutové snapshoty přes `build_snapshot_rows`
do standardních partic — frontend pak symbol vykreslí existující cestou
(/instruments, /replay), jen bez flows/CumΔ (ty nese výhradně IBKR).

Životní cyklus: UI zapíše požadavek do `adhoc_view` (DB je most UI→engine
jako u watchlistu) a při otevřeném pohledu prodlužuje `requested_ts`;
bez prodloužení viewer pohled po TTL uklidí — subskripce vypadnou diffem
`set_symbols` (AC: po zavření se kapacita uvolní). Bary vznikají z mid
kotace front future (volume 0 — jsou to kotace, ne obchody; poctivě
dokumentováno v manuálu).
"""

import datetime as dt
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import delete, select
from sqlalchemy.engine import Engine

from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.storage.meta import adhoc_view_table
from gexlens_engine.storage.parquet_store import SnapshotWriter
from gexlens_engine.tasty.extended import build_snapshot_rows
from gexlens_engine.tasty.provider import TastyChainCache
from gexlens_engine.tasty.symbols import ChainSymbols, SymbolMap

logger = logging.getLogger(__name__)

#: Bez prodloužení requested_ts se pohled uklidí (frontend pinguje à 1 min)
ADHOC_TTL_S = 180.0
#: Pásmo strik kolem spotu — celý řetěz by u ES znamenal stovky subskripcí
ADHOC_BAND_PCT = 8.0
#: Stáří kotace, po kterém se do snapshotu nezapisuje (shodné s extended)
ADHOC_MAX_AGE_S = 90.0


@dataclass
class _ActiveView:
    product: str
    chain: ChainSymbols
    expiry: str
    front_streamer: str | None
    # Minutová OHLC agregace z mid kotace front future (vzorkuje spot_tick)
    bar_open: float | None = None
    bar_high: float = 0.0
    bar_low: float = 0.0
    bar_last: float = 0.0


@dataclass
class AdhocViewer:
    """Drží aktivní ad-hoc pohledy; smyčky volá tasty větev enginu."""

    db: Engine
    symbol_map: SymbolMap
    cache: TastyChainCache
    writer: SnapshotWriter
    #: Produkty s plnou IBKR pipeline — ad-hoc se pro ně nezakládá
    is_watched: Callable[[str], bool]

    _views: dict[str, _ActiveView] = field(default_factory=dict, init=False)

    async def refresh(self, now: dt.datetime) -> None:
        """Sladí aktivní pohledy s tabulkou požadavků (à ~30 s)."""
        with self.db.connect() as conn:
            rows = conn.execute(
                select(adhoc_view_table.c.symbol, adhoc_view_table.c.requested_ts)
            ).fetchall()
        wanted: dict[str, dt.datetime] = {}
        stale: list[str] = []
        for row in rows:
            requested = row.requested_ts
            if requested.tzinfo is None:
                requested = requested.replace(tzinfo=dt.UTC)
            if (now - requested).total_seconds() > ADHOC_TTL_S:
                stale.append(str(row.symbol))
            else:
                wanted[str(row.symbol)] = requested
        if stale:
            with self.db.begin() as conn:
                conn.execute(delete(adhoc_view_table).where(adhoc_view_table.c.symbol.in_(stale)))
        for product in list(self._views):
            if product not in wanted:
                self._views.pop(product)
                logger.info("Ad-hoc pohled %s uklizen (bez prodloužení)", product)
        for product in wanted:
            if product in self._views or self.is_watched(product):
                continue
            try:
                chain = await self.symbol_map.chain(product, now.date())
            except Exception:
                logger.exception("Ad-hoc %s: chain z tasty selhal — požadavek zůstává", product)
                continue
            expiries = sorted({expiry for (expiry, _s, _r) in chain.by_contract})
            today_key = now.date().strftime("%Y%m%d")
            upcoming = [expiry for expiry in expiries if expiry >= today_key]
            if not upcoming:
                logger.warning("Ad-hoc %s: chain bez budoucí expirace — přeskočeno", product)
                continue
            front = await self.symbol_map.front_future(product)
            self._views[product] = _ActiveView(
                product=product, chain=chain, expiry=upcoming[0], front_streamer=front
            )
            logger.info(
                "Ad-hoc pohled %s ZALOŽEN (#521 C): expirace %s, front %s — jen tastytrade",
                product,
                upcoming[0],
                front,
            )

    def streamers(self) -> set[str]:
        """Symboly k subskripci: nejbližší expirace v pásmu kolem spotu + front."""
        symbols: set[str] = set()
        for view in self._views.values():
            spot = self._front_mid(view)
            for (expiry, strike, _right), streamer in view.chain.by_contract.items():
                if expiry != view.expiry:
                    continue
                if spot is not None and abs(strike - spot) / spot * 100.0 > ADHOC_BAND_PCT:
                    continue
                symbols.add(streamer)
            if view.front_streamer:
                symbols.add(view.front_streamer)
        return symbols

    def sample_spot(self) -> None:
        """Vzorek mid kotace front future do rozdělané minuty (à ~5 s)."""
        for view in self._views.values():
            mid = self._front_mid(view)
            if mid is None:
                continue
            if view.bar_open is None:
                view.bar_open = view.bar_high = view.bar_low = mid
            view.bar_high = max(view.bar_high, mid)
            view.bar_low = min(view.bar_low, mid)
            view.bar_last = mid

    async def write_minute(self, ts_min: dt.datetime, now_utc: dt.datetime) -> int:
        """Minutová uzávěrka: snapshoty řetězu + kotační bar podkladu."""
        import asyncio

        written = 0
        for view in self._views.values():
            spot = self._front_mid(view) or view.bar_last
            if not spot or not math.isfinite(spot) or spot <= 0:
                continue
            rows, oi_missing = build_snapshot_rows(
                view.chain,
                view.expiry,
                self.cache,
                ts_min=ts_min,
                spot=spot,
                now_utc=now_utc,
                max_age_s=ADHOC_MAX_AGE_S,
            )
            day = ts_min.date()
            if rows:
                await asyncio.to_thread(
                    self.writer.write_minute, view.product, view.expiry, day, rows
                )
                written += len(rows)
                if oi_missing:
                    await asyncio.to_thread(
                        self.writer.write_oi_missing, view.product, view.expiry, day, oi_missing
                    )
            if view.bar_open is not None:
                bar = Bar(
                    ts=ts_min,
                    open=view.bar_open,
                    high=view.bar_high,
                    low=view.bar_low,
                    close=view.bar_last,
                    volume=0.0,  # kotace, ne obchody — viz docstring
                )
                await asyncio.to_thread(self.writer.write_bars, view.product, day, [bar])
                view.bar_open = None
        return written

    def active(self) -> list[str]:
        """Aktivní produkty pro /status (UI badge zdroje)."""
        return sorted(self._views)

    def _front_mid(self, view: _ActiveView) -> float | None:
        if not view.front_streamer:
            return None
        state = self.cache.state(view.front_streamer)
        if state is None:
            return None
        bid, ask = state.quote.bid, state.quote.ask
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            return None
        return (bid + ask) / 2
