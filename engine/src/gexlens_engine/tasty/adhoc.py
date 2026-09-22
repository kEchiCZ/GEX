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

import asyncio
import datetime as dt
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import delete, select
from sqlalchemy.engine import Engine

from gexlens_engine.compute.settle import (
    session_bounds,
    settle_ts,
    soq_ts,
    trading_session_date,
)
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.storage.meta import adhoc_view_table
from gexlens_engine.storage.parquet_store import SnapshotWriter
from gexlens_engine.tasty.candles import CandleFetcher, CandleRange
from gexlens_engine.tasty.extended import build_snapshot_rows
from gexlens_engine.tasty.provider import TastyChainCache
from gexlens_engine.tasty.symbols import ChainSymbols, SymbolMap
from gexlens_engine.ticker import is_futures, symbol_root

logger = logging.getLogger(__name__)

#: Bez prodloužení requested_ts se pohled uklidí (frontend pinguje à 1 min)
ADHOC_TTL_S = 180.0
#: Pásmo strik kolem spotu — celý řetěz by u ES znamenal stovky subskripcí
ADHOC_BAND_PCT = 8.0
#: Stáří kotace, po kterém se do snapshotu nezapisuje (shodné s extended)
ADHOC_MAX_AGE_S = 90.0
#: První snapshot hned po založení (#206): stačí, když má kotaci tenhle podíl
#: kontraktů pásma — čekat na minutovou hranici by heatmapu zdrželo až o minutu
FIRST_SNAPSHOT_MIN_SHARE = 0.2
#: Strop striků na stranu od spotu (#206 fáze 2): SPX má 14 634 striků, ±8 %
#: by bylo přes 400 symbolů — rate limit streamu; nejbližší ke spotu vyhrávají
ADHOC_MAX_STRIKES_PER_SIDE = 120
#: Indexy s AM vypořádáním 3. pátek (SPX/NDX/RUT/DJX/XSP měsíční, VIX středa)
INDEX_ROOTS = frozenset({"SPX", "NDX", "RUT", "DJX", "XSP", "VIX"})


def _third_friday(day: dt.date) -> bool:
    return day.weekday() == 4 and 15 <= day.day <= 21


def equity_expiry_open(expiry: str, root: str, now: dt.datetime) -> bool:
    """Je equity/indexová expirace ještě živá? (#206 fáze 2)

    Futures řeší `expiry_expired` (jen kvartální SOQ); u akcií/ETF/indexů
    expiruje 0DTE v 16:00 ET a po close by pohled jinak celý večer stál na
    mrtvém řetězu (18. 9. 2026 SPY vybral 20260918 v 23:09 CEST). Měsíční
    indexové série (3. pátek) se vypořádávají ráno v 9:30 ET (AM), týdenní
    v 16:00 ET.
    """
    try:
        day = dt.datetime.strptime(expiry, "%Y%m%d").date()
    except ValueError:
        return False
    if day < now.date():
        return False
    if day > now.date():
        return True
    cutoff = soq_ts(day) if root in INDEX_ROOTS and _third_friday(day) else settle_ts(day)
    return now < cutoff


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
    #: První snapshot už zapsán (#206) — do té doby se zkouší každý tick
    first_written: bool = False


@dataclass
class AdhocViewer:
    """Drží aktivní ad-hoc pohledy; smyčky volá tasty větev enginu."""

    db: Engine
    symbol_map: SymbolMap
    cache: TastyChainCache
    writer: SnapshotWriter
    #: Produkty s plnou IBKR pipeline — ad-hoc se pro ně nezakládá
    is_watched: Callable[[str], bool]
    #: Svíčky podkladu do minulosti při založení (#206): graf ceny hned, ne od
    #: první minuty pohledu; None = bez backfillu (testy, vypnutá tasty)
    candles: CandleFetcher | None = None
    #: Zavolá se, když pohled vznikne nebo zmizí (#206): probudí reconciler
    #: subskripce, aby striky pohledu nečekaly na jeho šedesátisekundový tik
    on_change: Callable[[], None] | None = None

    _views: dict[str, _ActiveView] = field(default_factory=dict, init=False)
    #: Běžící backfilly svíček — reference drží úlohu naživu (#499)
    _backfill_tasks: set[asyncio.Task[None]] = field(default_factory=set, init=False)

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
                self._notify_change()
            elif self.is_watched(product):
                # Produkt mezitím dostal plnou IBKR pipeline (4. 9.: pohled NQ
                # vznikl po startu enginu, než se pipeline postavily, a pak
                # přežíval díky pingům z UI) — ad-hoc ustupuje, řádek pryč,
                # ať UI nehlásí „ad-hoc · tastytrade" nad IBKR daty
                self._views.pop(product)
                wanted.pop(product, None)
                with self.db.begin() as conn:
                    conn.execute(
                        delete(adhoc_view_table).where(adhoc_view_table.c.symbol == product)
                    )
                logger.info("Ad-hoc pohled %s uklizen — produkt má plnou pipeline", product)
                self._notify_change()
        for product in wanted:
            if product in self._views or self.is_watched(product):
                continue
            # Na kritické cestě jsou jen dva REST dotazy (chain a front future),
            # a ty jdou vedle sebe. Backfill svíček je doplněk a založení pohledu
            # NESMÍ blokovat (#206): 22. 9. vypršel u QQQ jeho šedesátisekundový
            # timeout a pohled kvůli tomu vznikl o minutu později.
            chain_result, front_result = await asyncio.gather(
                self.symbol_map.chain(product, now.date()),
                self.symbol_map.front_future(product),
                return_exceptions=True,
            )
            if isinstance(chain_result, BaseException):
                logger.error(
                    "Ad-hoc %s: chain z tasty selhal (%s: %s) — požadavek zůstává",
                    product,
                    type(chain_result).__name__,
                    chain_result,
                )
                continue
            chain = chain_result
            front = None if isinstance(front_result, BaseException) else front_result
            expiries = sorted({expiry for (expiry, _s, _r) in chain.by_contract})
            today_key = now.date().strftime("%Y%m%d")
            upcoming = [expiry for expiry in expiries if expiry >= today_key]
            if not is_futures(product):
                # Akcie/ETF/index (#206 fáze 2): dnešní 0DTE po 16:00 ET (index AM
                # série po 9:30 ET) je mrtvá — vzít další živou expiraci
                root = symbol_root(product)
                upcoming = [e for e in upcoming if equity_expiry_open(e, root, now)]
            if not upcoming:
                logger.warning("Ad-hoc %s: chain bez budoucí expirace — přeskočeno", product)
                continue
            self._views[product] = _ActiveView(
                product=product, chain=chain, expiry=upcoming[0], front_streamer=front
            )
            logger.info(
                "Ad-hoc pohled %s ZALOŽEN (#521 C): expirace %s, front %s — jen tastytrade",
                product,
                upcoming[0],
                front,
            )
            if front and self.candles is not None:
                self._spawn_backfill(product, front, now)
            self._notify_change()

    def _notify_change(self) -> None:
        """Změna množiny pohledů — reconciler subskripce se má probudit (#206)."""
        if self.on_change is None:
            return
        try:
            self.on_change()
        except Exception:
            logger.exception("Ad-hoc: probuzení plánu subskripce selhalo")

    def _spawn_backfill(self, product: str, streamer: str, now: dt.datetime) -> None:
        """Svíčky podkladu na pozadí (#206) — pohled na ně nečeká."""

        async def run() -> None:
            try:
                await self._backfill_candles(product, streamer, now)
            except Exception:
                logger.exception(
                    "Ad-hoc %s: backfill svíček selhal — pohled jede bez historie", product
                )

        task = asyncio.create_task(run())
        self._backfill_tasks.add(task)
        task.add_done_callback(self._backfill_tasks.discard)

    async def _backfill_candles(self, product: str, streamer: str, now: dt.datetime) -> None:
        """Svíčky seance do minulosti z dxFeed Candle (#206): cena hned, s objemem."""
        since, _ = session_bounds(trading_session_date(now))
        bars = await self.candles.fetch(  # type: ignore[union-attr]
            CandleRange(streamer_symbol=streamer, since=since, until=now)
        )
        if not bars:
            return
        by_day: dict[dt.date, list[object]] = {}
        for bar in bars:
            by_day.setdefault(bar.ts.date(), []).append(bar)
        for day, day_bars in sorted(by_day.items()):
            await asyncio.to_thread(self.writer.write_bars, product, day, day_bars)  # type: ignore[arg-type]
        logger.info("Ad-hoc %s: %d svíček podkladu doplněno od %s", product, len(bars), since)

    async def write_first_snapshot(self, now: dt.datetime) -> int:
        """První snapshot hned, jakmile má pásmo dost kotací (#206) — bez čekání na minutu."""
        written = 0
        for view in self._views.values():
            if view.first_written:
                continue
            wanted = self._band_streamers(view)
            fresh = sum(1 for streamer in wanted if self.cache.state(streamer) is not None)
            if not wanted or fresh < max(2, int(len(wanted) * FIRST_SNAPSHOT_MIN_SHARE)):
                continue
            view.first_written = True
            written += await self._write_view(view, now.replace(second=0, microsecond=0), now)
        return written

    def _band_streamers(self, view: _ActiveView) -> set[str]:
        spot = self._front_mid(view)
        picked: list[tuple[float, str]] = []
        for (expiry, strike, _right), streamer in view.chain.by_contract.items():
            if expiry != view.expiry:
                continue
            if spot is not None and abs(strike - spot) / spot * 100.0 > ADHOC_BAND_PCT:
                continue
            picked.append((abs(strike - spot) if spot is not None else 0.0, streamer))
        # Strop počtu (#206 fáze 2): nejbližší striky ke spotu, C i P = 2 symboly per strike
        picked.sort(key=lambda item: item[0])
        return {streamer for _distance, streamer in picked[: ADHOC_MAX_STRIKES_PER_SIDE * 4]}

    def streamers(self) -> set[str]:
        """Symboly k subskripci: nejbližší expirace v pásmu kolem spotu + front."""
        symbols: set[str] = set()
        for view in self._views.values():
            symbols |= self._band_streamers(view)
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
            written += await self._write_view(view, ts_min, now_utc)
            view.first_written = True
            if view.bar_open is not None:
                bar = Bar(
                    ts=ts_min,
                    open=view.bar_open,
                    high=view.bar_high,
                    low=view.bar_low,
                    close=view.bar_last,
                    volume=0.0,  # kotace, ne obchody — viz docstring
                )
                await asyncio.to_thread(self.writer.write_bars, view.product, ts_min.date(), [bar])
                view.bar_open = None
        return written

    async def _write_view(
        self, view: _ActiveView, ts_min: dt.datetime, now_utc: dt.datetime
    ) -> int:
        import asyncio

        spot = self._front_mid(view) or view.bar_last
        if not spot or not math.isfinite(spot) or spot <= 0:
            return 0
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
        if not rows:
            return 0
        await asyncio.to_thread(self.writer.write_minute, view.product, view.expiry, day, rows)
        if oi_missing:
            await asyncio.to_thread(
                self.writer.write_oi_missing, view.product, view.expiry, day, oi_missing
            )
        return len(rows)

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
