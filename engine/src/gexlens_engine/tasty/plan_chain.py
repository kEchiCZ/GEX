"""Snímek řetězu QQQ 0DTE a 1DTE pro plán opce QQQ (ADR-0062 K4 A, F1a #1740, epika #1738).

Plán opce QQQ vzniká v 9:31 ET (K4 A). Tento modul pro něj jednou za obchodní seanci pořídí
řetěz QQQ z tastytrade. IBKR market data lines se nepoužívají žádné. Plán pak z výsledku
sestaví F2b #1742 (`compute/option_plan.build_option_plan`).

Průběh jednoho snímku:
1. **Náběh v 9:30:30 ET** (`LEAD_S` před hranicí): řetěz z REST (`SymbolMap.chain`) a odběr
   podkladu QQQ. Kotace se tak na hranici už nečeká.
2. **Hranice 9:31:00 ET:** vezme se mid QQQ a spot NQ (spot pipeline: IBKR → 5s bar → tasty).
   Je to stav na konci minuty 9:30, tedy poměr NQ/QQQ z poslední uzavřené minuty před vznikem
   plánu (ADR-0062, Odvozeno) a zároveň spot obou téže minuty (#1740).
3. **Pásmo ±3 %** kolem této ceny v 0DTE a 1DTE (ADR-0062, Odvozeno) se odebere jako vlastní
   účel `plan_chain` v rozpočtu DXLink. Čerpá rezervu ad-hoc (`tasty/budget.py`), takže
   pokrytí, které trader vidí, neubírá.
4. **Čekání na kotace**, dokud kotaci nemá aspoň `MIN_QUOTED_SHARE` kontraktů pásma v každé
   expiraci, nejdéle `QUOTE_TIMEOUT_S` od hranice. Řádky tak nesou štítek minuty 9:31.
5. **Zápis** přes `build_snapshot_rows` do standardních partic `snapshots/QQQ/{exp}/{den}.parquet`
   (OI bez hodnoty do `oimissing`, #465), pak **uvolnění subskripce**.

Point-in-time: kotace přijatá před US openem se nepočítá (před openem jsou ceny zamrzlé z close
nebo se spreadem 4–6 %, ADR-0062 K4). Totéž platí pro OI: cache stavy nemaže a dnešní 0DTE
byla včera 1DTE, takže OI ze Summary přijatého před openem by neslo starší publikaci. Řádek ho
proto dostane jako chybějící (0.0 + `oimissing`, #465). Co se do limitu nesejde, vrátí snímek
jako neúplný s důvodem (`reasons`). Naměřené řádky se zapíšou i tak a odhad místo nich
nevzniká (K4 a).
"""

import asyncio
import datetime as dt
import logging
import math
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field, replace

from gexlens_engine.compute.marketclock import is_market_closed
from gexlens_engine.compute.settle import ET_TZ, is_trading_session, us_open_ts
from gexlens_engine.compute.spot import SpotReading
from gexlens_engine.storage.parquet_store import OiMissingRow, SnapshotRow, SnapshotWriter
from gexlens_engine.tasty.adhoc import ADHOC_MAX_AGE_S, equity_expiry_open
from gexlens_engine.tasty.extended import build_snapshot_rows
from gexlens_engine.tasty.provider import TastyChainCache, TastyContractState
from gexlens_engine.tasty.symbols import ChainSymbols, SymbolMap

logger = logging.getLogger(__name__)

#: Podklad plánu a instrument scénáře, z jehož ceny se počítá poměr (ADR-0062 bod 2)
PRODUCT = "QQQ"
REFERENCE = "NQ"
#: Účel v rozpočtu DXLink (`tasty/budget.EVENTS_BY_PURPOSE`)
PURPOSE = "plan_chain"
#: Pásmo striků kolem spotu QQQ v % (ADR-0062, Odvozeno: |delta| 0,25 1DTE ležela 2. 11. 1,3 %)
BAND_PCT = 3.0
#: Hranice snímku = minuta po US openu (9:31 ET, K4 A)
SNAPSHOT_AFTER_OPEN = dt.timedelta(minutes=1)
#: Náběh před hranicí: REST řetěz a kotace podkladu QQQ (start v 9:30:30 ET, po openu)
LEAD_S = 30.0
#: Limit čekání na kotace od hranice; snímek tak zůstane v minutě 9:31
QUOTE_TIMEOUT_S = 40.0
#: Podíl kontraktů pásma s kotací a s OI v každé expiraci, od kterého snímek dál nečeká
MIN_QUOTED_SHARE = 0.9
#: Perioda kontroly kotací během čekání
POLL_S = 0.5
#: Stáří kotace, po kterém se nepočítá (shodné s ad-hoc pohledem)
MAX_AGE_S = ADHOC_MAX_AGE_S


def snapshot_boundary(session: dt.date) -> dt.datetime:
    """Hranice snímku seance: 9:31 ET v UTC (K4 A)."""
    return us_open_ts(session) + SNAPSHOT_AFTER_OPEN


def next_snapshot(now: dt.datetime) -> tuple[dt.date, dt.datetime]:
    """Seance příštího snímku a začátek jeho náběhu; hranice leží po `now`.

    Víkend a svátek NYSE snímek nemají (`is_trading_session`, ADR-0046). Zmeškaná hranice
    (start enginu po 9:31 ET) se nedohání a snímek čeká na další seanci.
    """
    if now.tzinfo is None:
        raise ValueError(f"now musí nést časovou zónu: {now!r}")
    day = now.astimezone(ET_TZ).date()
    for _ in range(14):
        if is_trading_session(day) and snapshot_boundary(day) > now:
            return day, snapshot_boundary(day) - dt.timedelta(seconds=LEAD_S)
        day += dt.timedelta(days=1)
    raise RuntimeError(f"14 dní od {now:%Y-%m-%d} bez obchodní seance — chyba kalendáře")


def missed_session(now: dt.datetime) -> dt.date | None:
    """Dnešní seance, jejíž hranice 9:31 ET už proběhla (start enginu po ní), jinak None.

    Snímek se nedohání (`next_snapshot`), takže v tomto běhu enginu seance snímek nemá.
    """
    if now.tzinfo is None:
        raise ValueError(f"now musí nést časovou zónu: {now!r}")
    day = now.astimezone(ET_TZ).date()
    if is_trading_session(day) and snapshot_boundary(day) <= now:
        return day
    return None


def pick_expiries(
    chain: ChainSymbols, session: dt.date, now: dt.datetime
) -> tuple[str | None, str | None]:
    """(0DTE, 1DTE) seance z řetězu; chybějící expirace = None.

    - 0DTE = expirace s datem seance, dokud žije (`equity_expiry_open`: po 16:00 ET ne).
    - 1DTE = nejbližší expirace s datem po seanci, které je obchodní seancí
      (pátek → pondělí, svátek NYSE se přeskočí).
    """
    session_key = session.strftime("%Y%m%d")
    expiries = sorted({expiry for (expiry, _strike, _right) in chain.by_contract})
    zero = (
        session_key
        if session_key in expiries and equity_expiry_open(session_key, PRODUCT, now)
        else None
    )
    one: str | None = None
    for expiry in expiries:
        if expiry <= session_key:
            continue
        try:
            day = dt.datetime.strptime(expiry, "%Y%m%d").date()
        except ValueError:
            logger.warning("Snímek řetězu %s: nečitelná expirace %r — přeskakuji", PRODUCT, expiry)
            continue
        if is_trading_session(day):
            one = expiry
            break
    return zero, one


def band_streamers(
    chain: ChainSymbols, expiries: Iterable[str], spot: float, band_pct: float = BAND_PCT
) -> dict[str, frozenset[str]]:
    """Streamery kontraktů v pásmu ±`band_pct` % kolem spotu, per expirace."""
    limit = spot * band_pct / 100.0
    picked: dict[str, set[str]] = {expiry: set() for expiry in expiries}
    for (expiry, strike, _right), streamer in chain.by_contract.items():
        if expiry in picked and abs(strike - spot) <= limit:
            picked[expiry].add(streamer)
    return {expiry: frozenset(streamers) for expiry, streamers in picked.items()}


def missing_data_level(now: dt.datetime) -> int:
    """Úroveň logu chybějících dat: při zavřeném trhu jen INFO (AGENTS.md, #1307)."""
    return logging.INFO if is_market_closed(now) else logging.WARNING


@dataclass(frozen=True)
class ExpirySnapshot:
    """Jedna expirace snímku: pásmo, pokrytí kotacemi a zapsané řádky."""

    expiry: str
    #: Kontrakty pásma ±3 %
    contracts: int
    #: Kontrakty pásma s čerstvou kotací po US openu
    quoted: int
    #: Kontrakty pásma s OI ze Summary přijatého po US openu
    with_oi: int
    #: Řádky partice (tvar `option_plan.ChainRowLike`); bez platné kotace nebo greeks řádek není
    rows: tuple[SnapshotRow, ...]
    #: Řádky bez OI (v řádku 0.0, v partici `oimissing`, #465)
    oi_missing: int

    @property
    def quoted_share(self) -> float:
        return self.quoted / self.contracts if self.contracts else 0.0

    @property
    def oi_share(self) -> float:
        return self.with_oi / self.contracts if self.contracts else 0.0


@dataclass(frozen=True)
class PlanChainSnapshot:
    """Výsledek snímku pro F2b #1742; neúplný snímek nese důvody v `reasons`."""

    session: dt.date
    started_at: dt.datetime
    #: Okamžik vzorku cen: hranice 9:31 ET, nebo první kotace QQQ po ní
    spot_ts: dt.datetime | None
    #: Mid QQQ ve `spot_ts`
    spot: float | None
    spot_nq: float | None
    nq_source: str | None
    #: NQ/QQQ ve `spot_ts`
    ratio: float | None
    #: Okamžik sestavení řádků; jeho minuta je štítek řádků
    ts: dt.datetime | None
    zero_dte: ExpirySnapshot | None
    one_dte: ExpirySnapshot | None
    reasons: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.reasons

    def describe(self) -> str:
        """Řádek do logu: pokrytí obou expirací, ceny a stav."""
        parts = [f"Snímek řetězu {PRODUCT} pro plán (#1740) seance {self.session}"]
        for label, snap in (("0DTE", self.zero_dte), ("1DTE", self.one_dte)):
            if snap is None:
                parts.append(f"{label} —")
            else:
                parts.append(
                    f"{label} {snap.expiry}: kotace {snap.quoted}/{snap.contracts}, "
                    f"OI {snap.with_oi}/{snap.contracts}, {len(snap.rows)} řádků"
                )
        parts.append(
            f"{PRODUCT} {_fmt(self.spot, 2)}, {REFERENCE} {_fmt(self.spot_nq, 2)} "
            f"({self.nq_source or '—'}), poměr {_fmt(self.ratio, 4)}"
        )
        state = "úplný" if self.complete else "NEÚPLNÝ: " + "; ".join(self.reasons)
        return " · ".join(parts) + f" — {state}"


def _fmt(value: float | None, digits: int) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _fresh_quote(
    state: TastyContractState | None, now: dt.datetime, not_before: dt.datetime
) -> bool:
    """Kotace přišla po US openu a není starší než `MAX_AGE_S`."""
    if state is None or state.quote.updated_at is None:
        return False
    updated = state.quote.updated_at
    return updated >= not_before and (now - updated).total_seconds() <= MAX_AGE_S


def _fresh_oi(state: TastyContractState | None, not_before: dt.datetime) -> bool:
    """OI ze Summary přijatého po US openu; Summary je denní agregát, stáří se dál nehlídá."""
    if state is None or state.summary.updated_at is None:
        return False
    return state.summary.open_interest is not None and state.summary.updated_at >= not_before


@dataclass
class PlanChainSnapshotter:
    """Jednorázový snímek řetězu QQQ; úlohu v 9:31 ET pouští tasty větev enginu."""

    symbol_map: SymbolMap
    cache: TastyChainCache
    writer: SnapshotWriter
    #: Spot NQ v okamžiku hranice (`SpotReading.live`, jinak důvod)
    reference_spot: Callable[[], SpotReading]
    #: Probudí reconciler subskripce (vzor `AdhocViewer.on_change`, #206)
    on_change: Callable[[], None]
    clock: Callable[[], dt.datetime] = _utcnow
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    _symbols: set[str] = field(default_factory=set, init=False)

    def streamers(self) -> set[str]:
        """Symboly účelu `plan_chain`; mimo snímek prázdné."""
        return set(self._symbols)

    async def run(self, session: dt.date) -> PlanChainSnapshot | None:
        """Snímek seance; při zavřeném trhu None (jen INFO), subskripce se vždy uvolní.

        Volá se v náběhu (`next_snapshot`); start po hranici 9:31 ET vrátí neúplný snímek
        s důvodem a nic neodebírá.
        """
        if not is_trading_session(session):
            raise ValueError(f"snímek řetězu {PRODUCT} jen v obchodní seanci, {session} ji nemá")
        now = self.clock()
        if is_market_closed(now):
            logger.info("Snímek řetězu %s (#1740): trh zavřený %s — nespouští se", PRODUCT, now)
            return None
        try:
            return await self._run(session, now)
        finally:
            if self._symbols:
                self._symbols.clear()
                self._notify()

    async def _run(self, session: dt.date, started_at: dt.datetime) -> PlanChainSnapshot:
        boundary = snapshot_boundary(session)
        deadline = boundary + dt.timedelta(seconds=QUOTE_TIMEOUT_S)
        not_before = us_open_ts(session)
        reasons: list[str] = []
        empty = PlanChainSnapshot(
            session=session,
            started_at=started_at,
            spot_ts=None,
            spot=None,
            spot_nq=None,
            nq_source=None,
            ratio=None,
            ts=None,
            zero_dte=None,
            one_dte=None,
            reasons=(),
        )
        if started_at > boundary:
            # Probuzení po hranici (uspaný hostitel, start uprostřed náběhu po 9:31): vzorek
            # cen by nebyl stavem konce minuty 9:30 a snímek by nesl jinou minutu než plán
            reasons.append(f"hranice 9:31 ET zmeškána (start {started_at:%H:%M:%S} UTC)")
            return replace(empty, reasons=tuple(reasons))

        try:
            chain = await self.symbol_map.chain(PRODUCT, session)
            underlying = await self.symbol_map.front_future(PRODUCT)
        except Exception as error:
            reasons.append(f"řetěz {PRODUCT} z tasty selhal ({type(error).__name__}: {error})")
            return replace(empty, reasons=tuple(reasons))
        if underlying is None:
            reasons.append(f"podklad {PRODUCT} v tasty chybí")
            return replace(empty, reasons=tuple(reasons))
        zero, one = pick_expiries(chain, session, started_at)
        if zero is None:
            reasons.append(f"expirace 0DTE {session:%Y%m%d} v řetězu chybí")
        if one is None:
            reasons.append("expirace 1DTE po seanci v řetězu chybí")
        expiries = [expiry for expiry in (zero, one) if expiry is not None]
        if not expiries:
            return replace(empty, reasons=tuple(reasons))

        self._symbols = {underlying}
        self._notify()
        await self._sleep_until(boundary)
        spot, spot_ts = await self._wait_spot(underlying, not_before, deadline)
        if spot is None or spot_ts is None:
            reasons.append(f"kotace {PRODUCT} nedorazila do {deadline:%H:%M:%S} UTC")
            return replace(empty, reasons=tuple(reasons))
        reading = self.reference_spot()
        spot_nq = reading.live
        if spot_nq is None or not math.isfinite(spot_nq) or spot_nq <= 0:
            spot_nq = None
            reasons.append(f"spot {REFERENCE} chybí ({reading.describe(spot_ts)})")
        ratio = spot_nq / spot if spot_nq is not None else None

        band = band_streamers(chain, expiries, spot)
        self._symbols = {underlying, *(s for streamers in band.values() for s in streamers)}
        self._notify()
        await self._wait_quotes(band, not_before, deadline)

        ts = self.clock()
        ts_min = ts.replace(second=0, microsecond=0)
        build_spot = self._mid(underlying, ts, not_before) or spot
        max_age_s = min(MAX_AGE_S, (ts - not_before).total_seconds())
        snaps: dict[str, ExpirySnapshot] = {}
        for expiry in expiries:
            streamers = band[expiry]
            quoted = self._quoted(streamers, ts, not_before)
            with_oi = sum(1 for s in streamers if _fresh_oi(self.cache.state(s), not_before))
            band_chain = ChainSymbols(
                product=chain.product,
                day=chain.day,
                by_contract={
                    key: streamer
                    for key, streamer in chain.by_contract.items()
                    if key[0] == expiry and streamer in streamers
                },
            )
            rows, oi_missing = build_snapshot_rows(
                band_chain,
                expiry,
                self.cache,
                ts_min=ts_min,
                spot=build_spot,
                now_utc=ts,
                max_age_s=max_age_s,
            )
            rows, oi_missing = self._drop_stale_oi(band_chain, rows, oi_missing, not_before)
            label = "0DTE" if expiry == zero else "1DTE"
            snap = ExpirySnapshot(
                expiry=expiry,
                contracts=len(streamers),
                quoted=quoted,
                with_oi=with_oi,
                rows=tuple(rows),
                oi_missing=len(oi_missing),
            )
            snaps[expiry] = snap
            if snap.quoted_share < MIN_QUOTED_SHARE:
                reasons.append(
                    f"{label} {expiry}: kotace {quoted}/{len(streamers)} "
                    f"({snap.quoted_share:.0%}) do {QUOTE_TIMEOUT_S:.0f} s od 9:31 ET"
                )
            if snap.oi_share < MIN_QUOTED_SHARE:
                reasons.append(
                    f"{label} {expiry}: OI po openu {with_oi}/{len(streamers)} "
                    f"({snap.oi_share:.0%}) do {QUOTE_TIMEOUT_S:.0f} s od 9:31 ET"
                )
            if not rows:
                reasons.append(f"{label} {expiry}: žádný platný řádek")
                continue
            day = ts_min.date()
            await asyncio.to_thread(self.writer.write_minute, PRODUCT, expiry, day, rows)
            if oi_missing:
                await asyncio.to_thread(
                    self.writer.write_oi_missing, PRODUCT, expiry, day, oi_missing
                )
        return replace(
            empty,
            reasons=tuple(reasons),
            spot_ts=spot_ts,
            spot=spot,
            spot_nq=spot_nq,
            nq_source=reading.source if spot_nq is not None else None,
            ratio=ratio,
            ts=ts,
            zero_dte=snaps.get(zero) if zero is not None else None,
            one_dte=snaps.get(one) if one is not None else None,
        )

    def _notify(self) -> None:
        try:
            self.on_change()
        except Exception:
            logger.exception("Snímek řetězu %s: probuzení plánu subskripce selhalo", PRODUCT)

    async def _sleep_until(self, moment: dt.datetime) -> None:
        wait = (moment - self.clock()).total_seconds()
        if wait > 0:
            await self.sleep(wait)

    def _mid(self, streamer: str, now: dt.datetime, not_before: dt.datetime) -> float | None:
        state = self.cache.state(streamer)
        if state is None or not _fresh_quote(state, now, not_before):
            return None
        bid, ask = state.quote.bid, state.quote.ask
        if bid is None or ask is None or bid <= 0 or ask < bid:
            return None
        return (bid + ask) / 2.0

    async def _wait_spot(
        self, underlying: str, not_before: dt.datetime, deadline: dt.datetime
    ) -> tuple[float | None, dt.datetime | None]:
        while True:
            now = self.clock()
            mid = self._mid(underlying, now, not_before)
            if mid is not None:
                return mid, now
            if now >= deadline:
                return None, None
            await self.sleep(POLL_S)

    def _quoted(self, streamers: Iterable[str], now: dt.datetime, not_before: dt.datetime) -> int:
        return sum(1 for s in streamers if _fresh_quote(self.cache.state(s), now, not_before))

    def _ready(self, streamers: Iterable[str], now: dt.datetime, not_before: dt.datetime) -> int:
        """Kontrakty s čerstvou kotací i s OI po openu."""
        ready = 0
        for streamer in streamers:
            state = self.cache.state(streamer)
            if _fresh_quote(state, now, not_before) and _fresh_oi(state, not_before):
                ready += 1
        return ready

    def _drop_stale_oi(
        self,
        chain: ChainSymbols,
        rows: list[SnapshotRow],
        oi_missing: list[OiMissingRow],
        not_before: dt.datetime,
    ) -> tuple[list[SnapshotRow], list[OiMissingRow]]:
        """OI ze Summary před openem (starší publikace) = chybějící OI, ne hodnota (#465)."""
        streamer_of = {(strike, right): s for (_e, strike, right), s in chain.by_contract.items()}
        missing = list(oi_missing)
        known = {(row.strike, row.right) for row in oi_missing}
        kept: list[SnapshotRow] = []
        for row in rows:
            key = (row.strike, row.right)
            if key not in known and not _fresh_oi(self.cache.state(streamer_of[key]), not_before):
                row = replace(row, oi=0.0)
                missing.append(OiMissingRow(ts_min=row.ts_min, strike=row.strike, right=row.right))
            kept.append(row)
        return kept, missing

    async def _wait_quotes(
        self, band: dict[str, frozenset[str]], not_before: dt.datetime, deadline: dt.datetime
    ) -> None:
        """Čeká na `MIN_QUOTED_SHARE` kontraktů s kotací i OI v každé expiraci, do `deadline`."""
        while True:
            now = self.clock()
            if all(
                streamers
                and self._ready(streamers, now, not_before) / len(streamers) >= MIN_QUOTED_SHARE
                for streamers in band.values()
            ):
                return
            if now >= deadline:
                return
            await self.sleep(POLL_S)
