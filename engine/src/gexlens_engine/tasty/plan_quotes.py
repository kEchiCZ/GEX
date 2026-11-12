"""Odběr kontraktů plánu opce QQQ a podkladu od vzniku plánu do close (ADR-0062 K2 A, F1b #1747).

Vyhodnocení plánu (F3 #1748) potřebuje naměřené kotace vybraných kontraktů v minutě spouště,
SL a TP (ADR-0062 K3 bod 9: vstup za ask, výstup za bid) a 1m bary podkladu QQQ (spoušť
vstupu A z 5min close). Řetěz QQQ se jinak odebírá jen v krátkých snímcích 9:15 a 9:31 ET
(`plan_chain`), takže tento modul drží po celý den odběr jen toho, co plán čte:

- **Kontrakty** = sjednocení variant platného plánu řádku (`option_plans_store.effective_plan`)
  přes hlavní plán a oba stíny. Stíny mají stejné kontrakty (K10 B: `_pick_variants` pravidla
  nečte), takže jde o hlavní a záložní variantu. U plánu sestaveného celého v 9:31 ET jsou to
  kontrakty z `plan.repricing.plan`; plán z 9:15 bez opce kontrakty nemá.
- **Podklad QQQ** od chvíle, kdy seance má plán, i když je bez opce: plán sestavený v 9:31 ET
  potřebuje bary od US openu (5min bary od 9:30 ET).

Okno: od prvního plánu seance (vzniká v 9:15 ET se scénářem dne) do `settle_ts` seance
(16:00 ET, ve zkrácené seanci 13:00 ET). Po close se odběr uvolní. Plán se čte z PG každých
`PLAN_REFRESH_S`, takže se odběr sám přepne na kontrakty plánu sestaveného v 9:31 ET
a restart enginu ho obnoví z plánu dne v PG. Víkend a svátek plán nemají
(`compute/settle.is_trading_session`).

Výstup za každou dokončenou minutu (štítek minuty, vzorek v jejím závěru):
- `derived/QQQ/option_quotes/{den}.parquet` (`SnapshotWriter.write_option_quotes`): řádek na
  kontrakt s bid, ask, mid, IV, deltou a stářím kotace. Chybějící nebo starší kotace než
  `MAX_AGE_S` = řádek bez cen se stářím, ne forward-fill. IV a delta jen z dxFeed Greeks.
- `derived/QQQ/bars/{den}.parquet`: bar z mid kotace podkladu vzorkovaný à `TICK_S`, zápis
  shodný s ad-hoc pohledem (`AdhocViewer.write_minute`). Objem 0, jsou to kotace, ne obchody.
  Minuta, ve které odběr začal, se nezapíše: neúplný vzorek by dal falešné high/low.

Minutový cyklus na nic nečeká (AGENTS.md, #1337, #1646): vlastní úloha v tasty větvi, PG
i Parquet ve vlákně. IBKR lines 0, rozpočet DXLink: účel `plan_quotes` z rezervy ad-hoc
(`tasty/budget.py`), 2 kontrakty a podklad × Quote + Greeks = 6 položek.

Hlídač mezer hlásí změnu množiny kontraktů (a podkladu) bez kotace úrovní
`plan_chain.missing_data_level`: při zavřeném trhu jen INFO (`compute/marketclock`, AGENTS.md).
"""

import asyncio
import contextlib
import datetime as dt
import logging
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from gexlens_engine.compute.settle import is_trading_session, settle_ts, trading_session_date
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.option_plan_job import variant_keys
from gexlens_engine.storage.option_plans_store import OptionPlansRepository, effective_plan
from gexlens_engine.storage.parquet_store import OptionQuoteRow, SnapshotWriter
from gexlens_engine.tasty.plan_chain import (
    MAX_AGE_S,
    PLAN_SLOT,
    PRODUCT,
    missing_data_level,
    snapshot_boundary,
)
from gexlens_engine.tasty.provider import ContractStates, TastyContractState
from gexlens_engine.tasty.symbols import ChainSymbols

logger = logging.getLogger(__name__)

#: Účel v rozpočtu DXLink (`tasty/budget.EVENTS_BY_PURPOSE`)
PURPOSE = "plan_quotes"
#: Perioda vzorku mid podkladu do baru a kontroly přechodu minuty
TICK_S = 5.0
#: Perioda čtení plánů seance z PG: plán v 9:15 ET, plán sestavený v 9:31 ET, restart
PLAN_REFRESH_S = 30.0
#: Po hranici minuty se zápis spustí s touto rezervou, ať vzorek leží v závěru minuty
BOUNDARY_SLACK_S = 0.05

#: Kontrakt jako klíč `ChainSymbols.by_contract`: (expiry YYYYMMDD, strike, right)
Contract = tuple[str, float, str]


class _Chains(Protocol):
    """Co odběr potřebuje od `SymbolMap`: řetěz dne a symbol podkladu."""

    async def chain(self, product: str, today: dt.date) -> ChainSymbols: ...

    async def front_future(self, product: str) -> str | None: ...


def plan_contracts(plans: Iterable[Mapping[str, Any]]) -> frozenset[Contract]:
    """Kontrakty platných plánů seance: hlavní plán a stíny (K10 B) bez duplicit.

    Platný plán řádku vybírá jen `effective_plan` (ADR-0062 K4 bod 10), plán bez opce
    kontrakty nemá.
    """
    contracts: set[Contract] = set()
    for plan in plans:
        effective = effective_plan(plan)
        for part in (effective["main"], *effective["shadows"].values()):
            if part is None:
                continue
            for key in variant_keys(part):
                contracts.add((key.expiry.strftime("%Y%m%d"), key.strike, key.right))
    return frozenset(contracts)


def _age_s(updated_at: dt.datetime | None, now: dt.datetime) -> float | None:
    return None if updated_at is None else (now - updated_at).total_seconds()


def quote_row(
    ts_min: dt.datetime,
    contract: Contract,
    state: TastyContractState | None,
    now: dt.datetime,
) -> OptionQuoteRow:
    """Řádek kotace kontraktu v okamžiku `now` se štítkem minuty `ts_min`; čistá funkce.

    Ceny jen z kotace nejvýš `MAX_AGE_S` staré s ask ≥ bid ≥ 0 a kladným ask, jinak NULL
    (mezera se stářím, ne forward-fill). IV a delta jen z čerstvých dxFeed Greeks.
    """
    expiry, strike, right = contract
    quote_age = _age_s(state.quote.updated_at, now) if state is not None else None
    bid = ask = mid = iv = delta = None
    if state is not None and quote_age is not None and quote_age <= MAX_AGE_S:
        q_bid, q_ask = state.quote.bid, state.quote.ask
        if q_bid is not None and q_ask is not None and 0.0 <= q_bid <= q_ask and q_ask > 0.0:
            bid, ask, mid = q_bid, q_ask, (q_bid + q_ask) / 2.0
    if state is not None:
        greeks_age = _age_s(state.greeks.updated_at, now)
        g_iv, g_delta = state.greeks.iv, state.greeks.delta
        if (
            greeks_age is not None
            and greeks_age <= MAX_AGE_S
            and g_iv is not None
            and g_delta is not None
            and math.isfinite(g_iv)
            and g_iv > 0.0
            and math.isfinite(g_delta)
        ):
            iv, delta = g_iv, g_delta
    return OptionQuoteRow(
        ts_min=ts_min,
        expiry=expiry,
        strike=strike,
        right=right,
        bid=bid,
        ask=ask,
        mid=mid,
        iv=iv,
        delta=delta,
        quote_age_s=quote_age,
    )


def _name(contract: Contract) -> str:
    return f"{contract[1]:g}{contract[2]}"


def _floor_minute(ts: dt.datetime) -> dt.datetime:
    return ts.replace(second=0, microsecond=0)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass
class _MinuteBar:
    """OHLC z mid kotace podkladu v rozdělané minutě (vzor `adhoc._ActiveView`)."""

    minute: dt.datetime
    open: float
    high: float
    low: float
    close: float

    def add(self, mid: float) -> None:
        self.high = max(self.high, mid)
        self.low = min(self.low, mid)
        self.close = mid


@dataclass
class PlanQuoteRecorder:
    """Odběr kontraktů plánu opce QQQ a podkladu; `run` pouští tasty větev enginu."""

    repository: OptionPlansRepository
    symbol_map: _Chains
    cache: ContractStates
    writer: SnapshotWriter
    #: Probudí reconciler subskripce (vzor `AdhocViewer.on_change`, #206)
    on_change: Callable[[], None]
    clock: Callable[[], dt.datetime] = _utcnow

    _session: dt.date | None = field(default=None, init=False)
    #: Kontrakty plánu → streamer (None = kontrakt v řetězu tasty není, řádky bez cen)
    _contracts: dict[Contract, str | None] = field(default_factory=dict, init=False)
    _underlying: str | None = field(default=None, init=False)
    #: První úplná minuta streameru (minuta po začátku jeho odběru)
    _since: dict[str, dt.datetime] = field(default_factory=dict, init=False)
    _last_refresh: dt.datetime | None = field(default=None, init=False)
    _last_minute: dt.datetime | None = field(default=None, init=False)
    _bar: _MinuteBar | None = field(default=None, init=False)
    #: Kontrakty (a podklad) bez kotace v poslední zapsané minutě — hlídač hlásí jen změnu
    _gaps: frozenset[str] = field(default_factory=frozenset, init=False)

    def streamers(self) -> set[str]:
        """Symboly účelu `plan_quotes`; mimo okno plánu prázdné."""
        symbols = {streamer for streamer in self._contracts.values() if streamer is not None}
        if self._underlying is not None:
            symbols.add(self._underlying)
        return symbols

    @property
    def session(self) -> dt.date | None:
        """Seance, jejíž plán se odebírá; None = mimo okno."""
        return self._session

    async def run(self, stop: asyncio.Event) -> None:
        """Tik à `TICK_S` a těsně po hranici minuty, dokud se nezastaví tasty větev."""
        while not stop.is_set():
            now = self.clock()
            to_boundary = 60.0 - (now.second + now.microsecond / 1e6) + BOUNDARY_SLACK_S
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=min(TICK_S, to_boundary))
            if stop.is_set():
                return
            try:
                await self.tick()
            except Exception:
                logger.exception("Odběr plánu %s (#1747) selhal — další tik jede dál", PRODUCT)

    async def tick(self, now: dt.datetime | None = None) -> None:
        """Dokončená minuta → zápis, pak plán seance (okno, kontrakty), pak vzorek podkladu."""
        now = now or self.clock()
        minute = _floor_minute(now)
        if self._last_minute is not None and minute > self._last_minute:
            await self.write_minute(self._last_minute, now)
        self._last_minute = minute
        await self._refresh(now)
        self.sample_spot(now)

    def sample_spot(self, now: dt.datetime) -> None:
        """Vzorek mid podkladu do baru rozdělané minuty; zastaralá kotace se nevzorkuje."""
        if self._underlying is None:
            return
        mid = self._mid(self._underlying, now)
        if mid is None:
            return
        minute = _floor_minute(now)
        if self._bar is None or self._bar.minute != minute:
            self._bar = _MinuteBar(minute, mid, mid, mid, mid)
        else:
            self._bar.add(mid)

    async def write_minute(self, ts_min: dt.datetime, now: dt.datetime) -> int:
        """Řádky kotací a bar podkladu minuty `ts_min` (vzorek v `now`); vrací počet řádků.

        Idempotentní: opakovaný zápis minuty přepíše řádky téhož klíče (upsert).
        """
        session = self._session
        if session is None or ts_min >= settle_ts(session):
            return 0
        rows = [
            quote_row(ts_min, contract, self.cache.state(streamer) if streamer else None, now)
            for contract, streamer in sorted(self._contracts.items())
            if streamer is None or self._since.get(streamer, ts_min) <= ts_min
        ]
        if rows:
            await asyncio.to_thread(self.writer.write_option_quotes, PRODUCT, session, rows)
        gaps = {
            _name((row.expiry, row.strike, row.right)): (
                "bez kotace" if row.quote_age_s is None else f"stáří {row.quote_age_s:.0f} s"
            )
            for row in rows
            if row.bid is None
        }
        underlying = self._underlying
        if underlying is not None and self._since.get(underlying, ts_min) <= ts_min:
            bar = self._bar
            if bar is not None and bar.minute == ts_min:
                done = Bar(
                    ts=ts_min,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=0.0,  # kotace, ne obchody (vzor ad-hoc pohledu)
                )
                await asyncio.to_thread(self.writer.write_bars_by_day, PRODUCT, [done])
            else:
                gaps[f"podklad {PRODUCT}"] = "bez kotace"
        self._watch(ts_min, gaps, now)
        return len(rows)

    def _watch(self, ts_min: dt.datetime, gaps: Mapping[str, str], now: dt.datetime) -> None:
        """Hlídač mezer: hlásí jen změnu množiny; při zavřeném trhu jen INFO (AGENTS.md, #1307)."""
        names = frozenset(gaps)
        if names == self._gaps:
            return
        self._gaps = names
        if names:
            logger.log(
                missing_data_level(now),
                "Odběr plánu %s (#1747): minuta %s UTC bez kotace — %s",
                PRODUCT,
                ts_min.strftime("%H:%M"),
                ", ".join(f"{name} ({gaps[name]})" for name in sorted(names)),
            )
        else:
            logger.info(
                "Odběr plánu %s (#1747): od minuty %s UTC kotace zase tečou",
                PRODUCT,
                ts_min.strftime("%H:%M"),
            )

    async def _refresh(self, now: dt.datetime) -> None:
        """Okno a kontrakty z plánů seance v PG (à `PLAN_REFRESH_S`); po close uvolní."""
        session = trading_session_date(now)
        if not is_trading_session(session) or not (
            snapshot_boundary(session, PLAN_SLOT) <= now < settle_ts(session)
        ):
            self._release(now)
            return
        if (
            self._session == session
            and self._last_refresh is not None
            and (now - self._last_refresh).total_seconds() < PLAN_REFRESH_S
        ):
            return
        self._last_refresh = now
        rows = await asyncio.to_thread(self.repository.plans_of_day, session)
        if not rows:
            self._release(now)
            return
        contracts = plan_contracts(row.plan for row in rows)
        if self._session == session and set(self._contracts) == contracts:
            return
        try:
            chain = await self.symbol_map.chain(PRODUCT, session)
            underlying = await self.symbol_map.front_future(PRODUCT)
        except Exception as error:
            # Bez řetězu nejde streamer kontraktu najít; zkusí se příštím čtením plánu
            logger.log(
                missing_data_level(now),
                "Odběr plánu %s (#1747): řetěz z tasty selhal (%s: %s) — zkusí se za %.0f s",
                PRODUCT,
                type(error).__name__,
                error,
                PLAN_REFRESH_S,
            )
            return
        self._subscribe(session, contracts, chain, underlying, now)

    def _subscribe(
        self,
        session: dt.date,
        contracts: frozenset[Contract],
        chain: ChainSymbols,
        underlying: str | None,
        now: dt.datetime,
    ) -> None:
        before = self.streamers()
        if session != self._session:
            self._since.clear()
            self._bar = None
            self._gaps = frozenset()
        self._session = session
        self._contracts = {contract: chain.by_contract.get(contract) for contract in contracts}
        self._underlying = underlying
        after = self.streamers()
        first_full = _floor_minute(now) + dt.timedelta(minutes=1)
        for streamer in after - before:
            self._since[streamer] = first_full
        for streamer in before - after:
            self._since.pop(streamer, None)
        absent = sorted(_name(c) for c, streamer in self._contracts.items() if streamer is None)
        if absent:
            logger.log(
                missing_data_level(now),
                "Odběr plánu %s (#1747): kontrakty %s v řetězu tasty nejsou — minuty bez cen",
                PRODUCT,
                ", ".join(absent),
            )
        if underlying is None:
            logger.log(
                missing_data_level(now),
                "Odběr plánu %s (#1747): podklad v tasty chybí — bary se nezapíšou",
                PRODUCT,
            )
        logger.info(
            "Odběr plánu %s (#1747) seance %s: kontrakty %s, podklad %s, do %s UTC",
            PRODUCT,
            session,
            ", ".join(sorted(_name(c) for c in contracts)) or "žádné (plán bez opce)",
            underlying or "—",
            settle_ts(session).strftime("%H:%M"),
        )
        if after != before:
            self._notify()

    def _release(self, now: dt.datetime) -> None:
        """Mimo okno plánu (po close, před plánem, víkend): odběr pryč, stav seance pryč."""
        if self._session is None and not self._contracts and self._underlying is None:
            return
        had = self.streamers()
        logger.info("Odběr plánu %s (#1747) seance %s uvolněn", PRODUCT, self._session)
        self._session = None
        self._contracts = {}
        self._underlying = None
        self._since.clear()
        self._bar = None
        self._gaps = frozenset()
        self._last_refresh = None
        if had:
            self._notify()

    def _mid(self, streamer: str, now: dt.datetime) -> float | None:
        state = self.cache.state(streamer)
        if state is None:
            return None
        age = _age_s(state.quote.updated_at, now)
        bid, ask = state.quote.bid, state.quote.ask
        if age is None or age > MAX_AGE_S or bid is None or ask is None or bid <= 0 or ask < bid:
            return None
        return (bid + ask) / 2.0

    def _notify(self) -> None:
        try:
            self.on_change()
        except Exception:
            logger.exception("Odběr plánu %s: probuzení plánu subskripce selhalo", PRODUCT)
