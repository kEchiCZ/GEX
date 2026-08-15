"""Shadow porovnání IBKR × tasty (#613) — nic nepublikuje, jen měří.

Porovnává se na minutovém gridu enginu: v okamžiku uzavřené minuty se vezme
IBKR sweep cache a k ní poslední tasty stav s explicitním max stářím
(timestamp normalizace konstrukcí — dva STAVY k témuž referenčnímu času,
ne dva proudy; konvence ADR-0015). Obě strany starší než práh se zapisují
jako NULL — report z toho počítá podíl chybějících vzorků.

Kill switch: GEXLENS_TASTY_SHADOW=false — smyčka se vůbec nespustí; vypnutí
za běhu = restart není potřeba, flag se čte při startu enginu a shadow lze
zabít i samostatně (vlastní task, pád nesmí ohrozit sběr — každá výjimka
se loguje a smyčka jede dál).
"""

import asyncio
import contextlib
import datetime as dt
import logging
import time
from collections.abc import Callable, Sequence

from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.ibkr.scheduler import CachedQuote
from gexlens_engine.storage.feed_comparison import ComparisonRow, FeedComparisonRepository
from gexlens_engine.tasty.provider import TastyChainCache, TastyContractState
from gexlens_engine.tasty.symbols import ChainSymbols

logger = logging.getLogger(__name__)

#: Max stáří hodnoty vůči referenční minutě, aby vstoupila do porovnání.
#: IBKR rotace obnoví kontrakt ~1× za minutu, tasty streamuje průběžně —
#: 120 s drží konvenci stale prahů (ADR-0015) a měří data, ne rozdíl hodin.
MAX_AGE_MS = 120_000

#: Porovnávaná pole: (název, ibkr hodnota ze snapshotu, tasty hodnota ze stavu)
_FIELDS: list[
    tuple[str, Callable[[CachedQuote], float | None], Callable[[TastyContractState], float | None]]
] = [
    ("bid", lambda c: c.snapshot.bid, lambda s: s.quote.bid),
    ("ask", lambda c: c.snapshot.ask, lambda s: s.quote.ask),
    ("iv", lambda c: c.snapshot.iv or None, lambda s: s.greeks.iv),
    ("delta", lambda c: c.snapshot.delta, lambda s: s.greeks.delta),
    ("gamma", lambda c: c.snapshot.gamma, lambda s: s.greeks.gamma),
]


def contract_label(spec: OptionContractSpec) -> str:
    return f"{spec.symbol} {spec.expiry} {spec.strike:g}{spec.right}"


def compare_minute(
    ts: dt.datetime,
    ibkr_quotes: dict[OptionContractSpec, CachedQuote],
    tasty_cache: TastyChainCache,
    chains: dict[str, ChainSymbols],
    *,
    now_monotonic: float,
    now_utc: dt.datetime,
    max_age_ms: int = MAX_AGE_MS,
    oi_ibkr: dict[tuple[str, str, float, str], float] | None = None,
) -> list[ComparisonRow]:
    """Řádky porovnání jedné minuty — čistá funkce (testy bez sítě).

    Stáří IBKR strany se měří od monotonic času sweep cache, tasty strany od
    UTC času posledního eventu; obě se normalizují na ms vůči TÉŽE referenci
    (okamžik porovnání), takže report měří rozdíl dat, ne rozdíl hodin.
    """
    rows: list[ComparisonRow] = []
    for spec, cached in ibkr_quotes.items():
        # Mapa per produkt (ES i NQ mají vlastní chain endpoint) — bez toho
        # by se druhý instrument tiše nikdy neporovnal
        chain = chains.get(spec.symbol)
        if chain is None:
            continue
        streamer = chain.streamer_symbol(spec)
        if streamer is None:
            continue  # kontrakt mimo tasty chain — report to uvidí jinde
        state = tasty_cache.state(streamer)
        age_ibkr_ms = int((now_monotonic - cached.updated_at) * 1000)
        ibkr_fresh = age_ibkr_ms <= max_age_ms and not cached.stale
        label = contract_label(spec)
        for field_name, ibkr_value, tasty_value in _FIELDS:
            value_ibkr = ibkr_value(cached) if ibkr_fresh else None
            value_tasty: float | None = None
            age_tasty_ms: int | None = None
            if state is not None:
                source = state.quote if field_name in ("bid", "ask") else state.greeks
                if source.updated_at is not None:
                    age_tasty_ms = int((now_utc - source.updated_at).total_seconds() * 1000)
                    if age_tasty_ms <= max_age_ms:
                        value_tasty = tasty_value(state)
            if value_ibkr is None and value_tasty is None:
                continue  # obě strany mrtvé — nulová informace, jen objem
            rows.append(
                ComparisonRow(
                    ts=ts,
                    symbol=label,
                    field=field_name,
                    value_ibkr=value_ibkr,
                    value_tasty=value_tasty,
                    age_ibkr_ms=age_ibkr_ms if ibkr_fresh else None,
                    age_tasty_ms=age_tasty_ms,
                )
            )
        # OI (#664): denní veličina — stáří se negatuje (Summary chodí při
        # subskripci a při změně, archiv IBKR je denní snímek). IBKR strana
        # z oi_eod, ne ze sweep cache; věk obou stran se nechává NULL.
        oi_value_ibkr = None if oi_ibkr is None else oi_ibkr.get(
            (spec.symbol, spec.expiry, spec.strike, spec.right)
        )  # fmt: skip
        oi_value_tasty = None if state is None else state.summary.open_interest
        if oi_value_ibkr is not None or oi_value_tasty is not None:
            rows.append(
                ComparisonRow(
                    ts=ts,
                    symbol=label,
                    field="oi",
                    value_ibkr=oi_value_ibkr,
                    value_tasty=oi_value_tasty,
                    age_ibkr_ms=None,
                    age_tasty_ms=None,
                )
            )
    return rows


class ShadowComparator:
    """Minutová smyčka shadow porovnání — vlastní task vedle pipelines.

    `contracts_source` vrací aktuální množinu (spec → CachedQuote) přes
    všechny pipelines; `chain_source` denní mapu symbolů. Pád jedné iterace
    smyčku nezabíjí (sběr IBKR nesmí být ohrožen ničím z shadow větve).
    """

    def __init__(
        self,
        repository: FeedComparisonRepository,
        tasty_cache: TastyChainCache,
        contracts_source: Callable[[], dict[OptionContractSpec, CachedQuote]],
        chain_source: Callable[[], dict[str, ChainSymbols]],
        *,
        interval_s: float = 60.0,
        oi_source: Callable[[], dict[tuple[str, str, float, str], float]] | None = None,
    ) -> None:
        self._repository = repository
        self._cache = tasty_cache
        self._contracts_source = contracts_source
        self._chain_source = chain_source
        self._interval_s = interval_s
        # OI porovnání (#664): sync čtení denního archivu — volá se přes to_thread
        self._oi_source = oi_source
        #: Diagnostika zátěže: řádků zapsáno od startu
        self.rows_written = 0

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._interval_s)
            if stop.is_set():
                return
            try:
                await self._tick()
            except Exception:
                logger.exception("Shadow porovnání selhalo — příští minuta jede dál")

    async def _tick(self) -> None:
        chains = self._chain_source()
        if not chains:
            return
        ts = dt.datetime.now(dt.UTC).replace(second=0, microsecond=0)
        oi_ibkr = None if self._oi_source is None else await asyncio.to_thread(self._oi_source)
        rows = compare_minute(
            ts,
            self._contracts_source(),
            self._cache,
            chains,
            now_monotonic=time.monotonic(),
            now_utc=dt.datetime.now(dt.UTC),
            oi_ibkr=oi_ibkr,
        )
        await asyncio.to_thread(self._repository.insert_many, rows)
        self.rows_written += len(rows)
        if rows and self.rows_written % (len(rows) * 5) < len(rows):
            logger.info(
                "Shadow: %d řádků tuto minutu (%d celkem, tasty sleduje %d symbolů)",
                len(rows),
                self.rows_written,
                self._cache.symbols_tracked(),
            )


def shadow_symbols(contracts: Sequence[OptionContractSpec], chain: ChainSymbols) -> set[str]:
    """Streamer symboly pro stejnou množinu kontraktů, jakou drží IBKR."""
    symbols = set()
    for spec in contracts:
        streamer = chain.streamer_symbol(spec)
        if streamer is not None:
            symbols.add(streamer)
    return symbols
