"""Snímky řetězu QQQ 0DTE a 1DTE pro plán opce QQQ (ADR-0062 K4 C, F1a #1740, F1c #1768).

Plán opce QQQ vzniká v 9:15 ET se scénářem dne a v 9:31 ET se přecení (K4 C, epika #1738).
Tento modul pro něj za obchodní seanci pořídí dva snímky řetězu QQQ z tastytrade (`SLOTS`).
IBKR market data lines se nepoužívají žádné. Plán z výsledku sestaví F2b #1742
(`compute/option_plan.build_option_plan`), přecenění F2c #1769.

Průběh snímku 9:31 ET (přecenění, F1a):
1. **Náběh v 9:30:30 ET** (`lead_s` před hranicí): řetěz z REST (`SymbolMap.chain`) a odběr
   podkladu QQQ. Kotace se tak na hranici už nečeká.
2. **Hranice 9:31:00 ET:** vezme se mid QQQ a spot NQ (spot pipeline: IBKR → 5s bar → tasty).
   Je to stav na konci minuty 9:30, tedy poměr NQ/QQQ z poslední uzavřené minuty před
   hranicí (ADR-0062, Odvozeno) a zároveň spot obou téže minuty (#1740).
3. **Pásmo ±3 %** kolem této ceny v 0DTE a 1DTE (ADR-0062, Odvozeno) se odebere jako vlastní
   účel `plan_chain` v rozpočtu DXLink. Čerpá rezervu ad-hoc (`tasty/budget.py`), takže
   pokrytí, které trader vidí, neubírá.
4. **Čekání na úplnost**, nejdéle `QUOTE_TIMEOUT_S` od hranice. Řádky tak nesou štítek minuty
   hranice. Úplnost (K4 bod 7, rozhodnuto 9. 11.: B) posuzuje, co plán z řetězu čte:
   - 0DTE: ATM straddle na strike nejbližším spotu (`option_plan.em_gap`);
   - 1DTE: pro call i put kontrakt s |delta| nejblíž 0,25 s oběma sousedními striky
     (`option_plan.variant_gaps`);
   - OI aspoň u `MIN_OI_SHARE` kontraktů pásma v každé expiraci.
   Podíl kotací za celé pásmo jde jen do logu. Měření 9. 11. ukázalo, že v 9:15 chybí ITM
   kontrakty (|delta| 0,69–0,95), které plán nečte.
5. **Zápis** přes `build_snapshot_rows` do standardních partic `snapshots/QQQ/{exp}/{den}.parquet`
   (OI bez hodnoty do `oimissing`, #465), pak **uvolnění subskripce**. Řádky obou expirací
   vzniknou v jednom okamžiku před zápisem, takže `stale_age` není záporné.

Point-in-time v 9:31: kotace přijatá před US openem se nepočítá (před openem jsou ceny zamrzlé
z close nebo se spreadem 4–6 %, ADR-0062 K4). Totéž platí pro OI: cache stavy nemaže a dnešní
0DTE byla včera 1DTE, takže OI ze Summary přijatého před openem by neslo starší publikaci. Řádek
ho proto dostane jako chybějící (0.0 + `oimissing`, #465).

Snímek 9:15 ET (vznik plánu, F1c) se liší ve třech věcech (Rozhodnuto 5. 11. v #1738, Q1 A a Q2 A):
- **Kotace a greeks jen po aktualizaci.** Feed čas kotace nenese (`bidTime` = 0, sonda #1768)
  a DXLink při odběru pošle poslední známý stav, před openem i ten z close. 23. 9. a 1. 10.
  přišly všechny kotace v jednom okamžiku a pak se nezměnily. Počítá se proto jen kontrakt,
  jehož kotace se po odběru změnila aspoň dvakrát (event z odběru + aktualizace,
  `quote_changes`). Greeks bez aktualizace nahradí IV z mid (#547).
- **Pásmo už v náběhu 9:13:30 ET** z první živé ceny QQQ. Na aktualizace tak zbývá 90 s do
  hranice, tedy limit stáří kotace `MAX_AGE_S`.
- **OI jen ze Summary s `dayId` = den seance.** Srovnání s OI po openu (9:31) ověří, že nese
  dnešní publikaci.

Co se do limitu nesejde, vrátí snímek jako neúplný s důvodem (`reasons`). Naměřené řádky se
zapíšou i tak a odhad místo nich nevzniká (K4 a). Kontrakty pásma bez řádku nese log podle
důvodu (`ExpirySnapshot.missing`, K4 bod 7, E), aby šlo poznat, zda chyběla aktualizace po
odběru, nebo kotace zestárla nad `MAX_AGE_S`.
"""

import asyncio
import datetime as dt
import logging
import math
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Protocol

from gexlens_engine.compute.marketclock import is_market_closed
from gexlens_engine.compute.option_plan import em_gap, variant_gaps
from gexlens_engine.compute.settle import ET_TZ, is_trading_session, us_open_ts
from gexlens_engine.compute.spot import SpotReading
from gexlens_engine.storage.parquet_store import OiMissingRow, SnapshotRow, SnapshotWriter
from gexlens_engine.tasty.adhoc import ADHOC_MAX_AGE_S, equity_expiry_open
from gexlens_engine.tasty.extended import build_snapshot_rows
from gexlens_engine.tasty.provider import (
    ContractStates,
    TastyChainCache,
    TastyContractState,
    TastyGreeks,
    TastyQuote,
)
from gexlens_engine.tasty.symbols import ChainSymbols, SymbolMap

logger = logging.getLogger(__name__)

#: Podklad plánu a instrument scénáře, z jehož ceny se počítá poměr (ADR-0062 bod 2)
PRODUCT = "QQQ"
REFERENCE = "NQ"
#: Účel v rozpočtu DXLink (`tasty/budget.EVENTS_BY_PURPOSE`)
PURPOSE = "plan_chain"
#: Pásmo striků kolem spotu QQQ v % (ADR-0062, Odvozeno: |delta| 0,25 1DTE ležela 2. 11. 1,3 %)
BAND_PCT = 3.0
#: Limit čekání na kotace od hranice; snímek tak zůstane v minutě hranice
QUOTE_TIMEOUT_S = 40.0
#: Podíl kontraktů pásma s OI v každé expiraci, od kterého je snímek úplný (K4 bod 7)
MIN_OI_SHARE = 0.9
#: Perioda kontroly kotací během čekání
POLL_S = 0.5
#: Stáří kotace, po kterém se nepočítá (shodné s ad-hoc pohledem)
MAX_AGE_S = ADHOC_MAX_AGE_S
#: Změny stavu po odběru, od kterých je kotace (greeks) před openem živá: event z odběru,
#: který může nést stav z close, a aspoň jedna aktualizace (#1768)
LIVE_CHANGES = 2
#: dxFeed `dayId` = počet dní od tohoto data
_DAY_ID_EPOCH = dt.date(1970, 1, 1)
#: Důvod chybějícího řádku, když kotace platí: bid 0, ask pod bidem nebo greeks nejdou
#: dopočítat z mid (`build_snapshot_rows`)
ROW_REJECTED = "kotace bez řádku (bid, ask nebo IV z mid)"


@dataclass(frozen=True)
class SnapshotSlot:
    """Jeden snímek seance (ADR-0062 K4 C): hranice vůči US openu, náběh a pravidla čerstvosti."""

    #: Hranice v ET pro log a důvody
    label: str
    #: Hranice = US open + `from_open`
    from_open: dt.timedelta
    #: Náběh před hranicí v sekundách
    lead_s: float
    #: Před openem: čerstvost podle aktualizace po odběru a `dayId`, pásmo v náběhu (#1768)
    pre_open: bool


#: Vznik plánu se scénářem dne (F1c #1768): náběh 9:13:30 ET, 90 s na aktualizace (`MAX_AGE_S`)
PLAN_SLOT = SnapshotSlot("9:15", dt.timedelta(minutes=-15), MAX_AGE_S, pre_open=True)
#: Přecenění po US openu (F1a #1740, F2c #1769): náběh 9:30:30 ET
REPRICE_SLOT = SnapshotSlot("9:31", dt.timedelta(minutes=1), 30.0, pre_open=False)
#: Snímky seance v časovém pořadí
SLOTS: tuple[SnapshotSlot, ...] = (PLAN_SLOT, REPRICE_SLOT)


def snapshot_boundary(session: dt.date, slot: SnapshotSlot) -> dt.datetime:
    """Hranice snímku seance v UTC: 9:15, nebo 9:31 ET podle `slot`."""
    return us_open_ts(session) + slot.from_open


def next_snapshot(now: dt.datetime) -> tuple[dt.date, SnapshotSlot, dt.datetime]:
    """Příští snímek: seance, snímek a začátek jeho náběhu; hranice leží po `now`.

    Víkend a svátek NYSE snímky nemají (`is_trading_session`, ADR-0046). Zmeškaná hranice se
    nedohání: start enginu mezi 9:15 a 9:31 ET pořídí jen snímek 9:31, start po 9:31 ET čeká
    na další seanci.
    """
    if now.tzinfo is None:
        raise ValueError(f"now musí nést časovou zónu: {now!r}")
    day = now.astimezone(ET_TZ).date()
    for _ in range(14):
        if is_trading_session(day):
            for slot in SLOTS:
                boundary = snapshot_boundary(day, slot)
                if boundary > now:
                    return day, slot, boundary - dt.timedelta(seconds=slot.lead_s)
        day += dt.timedelta(days=1)
    raise RuntimeError(f"14 dní od {now:%Y-%m-%d} bez obchodní seance — chyba kalendáře")


def missed_slots(now: dt.datetime) -> tuple[dt.date, tuple[SnapshotSlot, ...]] | None:
    """Dnešní seance a její snímky, jejichž hranice už proběhla (start enginu po nich), jinak None.

    Snímek se nedohání (`next_snapshot`), takže v tomto běhu enginu tyto snímky seance nemá.
    """
    if now.tzinfo is None:
        raise ValueError(f"now musí nést časovou zónu: {now!r}")
    day = now.astimezone(ET_TZ).date()
    if not is_trading_session(day):
        return None
    missed = tuple(slot for slot in SLOTS if snapshot_boundary(day, slot) <= now)
    return (day, missed) if missed else None


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
    #: Kontrakty pásma s čerstvou kotací podle pravidel snímku (9:31 po openu, 9:15 po odběru)
    quoted: int
    #: Kontrakty pásma s platným OI (9:31 Summary po openu, 9:15 Summary dne seance)
    with_oi: int
    #: Řádky partice (tvar `option_plan.ChainRowLike`); bez platné kotace nebo greeks řádek není
    rows: tuple[SnapshotRow, ...]
    #: Řádky bez OI (v řádku 0.0, v partici `oimissing`, #465)
    oi_missing: int
    #: Kontrakty pásma bez řádku podle důvodu, např. ("jen event z odběru", ("671P", …)) (E)
    missing: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @property
    def quoted_share(self) -> float:
        return self.quoted / self.contracts if self.contracts else 0.0

    @property
    def oi_share(self) -> float:
        return self.with_oi / self.contracts if self.contracts else 0.0


@dataclass(frozen=True)
class PlanChainSnapshot:
    """Výsledek snímku pro F2b #1742 a F2c #1769; neúplný snímek nese důvody v `reasons`."""

    session: dt.date
    slot: SnapshotSlot
    started_at: dt.datetime
    #: Okamžik vzorku cen: hranice snímku, nebo první platná kotace QQQ po ní
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
        parts = [
            f"Snímek řetězu {PRODUCT} {self.slot.label} ET pro plán (#1740, #1768) "
            f"seance {self.session}"
        ]
        for label, snap in (("0DTE", self.zero_dte), ("1DTE", self.one_dte)):
            if snap is None:
                parts.append(f"{label} —")
            else:
                gaps = "; ".join(f"{why} {' '.join(names)}" for why, names in snap.missing)
                parts.append(
                    f"{label} {snap.expiry}: kotace {snap.quoted}/{snap.contracts}, "
                    f"OI {snap.with_oi}/{snap.contracts}, {len(snap.rows)} řádků"
                    + (f" (bez řádku: {gaps})" if gaps else "")
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


class _Freshness(Protocol):
    """Co snímek počítá jako čerstvé. Řádky staví z `states()`, tedy jen z toho, co projde."""

    #: Název OI v důvodu neúplnosti
    oi_label: str

    def mark(self, streamers: Iterable[str]) -> None: ...

    def quote(self, streamer: str, now: dt.datetime) -> bool: ...

    def why(self, streamer: str, now: dt.datetime) -> str:
        """Proč kotace neplatí; volá se jen, když `quote` vrátí False (E)."""
        ...

    def oi(self, streamer: str) -> bool: ...

    def states(self) -> ContractStates: ...

    def max_age_s(self, ts: dt.datetime) -> float: ...


@dataclass
class _AfterOpen:
    """Snímek 9:31 ET (F1a #1740): platí, co přišlo po US openu."""

    cache: TastyChainCache
    not_before: dt.datetime
    oi_label: str = "OI po openu"

    def mark(self, streamers: Iterable[str]) -> None:
        """Příjem po openu stačí, stav při odběru se nepamatuje."""

    def quote(self, streamer: str, now: dt.datetime) -> bool:
        return _fresh_quote(self.cache.state(streamer), now, self.not_before)

    def why(self, streamer: str, now: dt.datetime) -> str:
        state = self.cache.state(streamer)
        if state is None or state.quote.updated_at is None:
            return "bez kotace"
        if state.quote.updated_at < self.not_before:
            return "kotace před openem"
        return f"starší {MAX_AGE_S:.0f} s"

    def oi(self, streamer: str) -> bool:
        return _fresh_oi(self.cache.state(streamer), self.not_before)

    def states(self) -> ContractStates:
        return self.cache

    def max_age_s(self, ts: dt.datetime) -> float:
        # Greeks a kotace pro řádky jen přijaté po openu
        return min(MAX_AGE_S, (ts - self.not_before).total_seconds())


@dataclass
class _PreOpen:
    """Snímek 9:15 ET (F1c #1768): kotace a greeks po aktualizaci, OI dne seance."""

    cache: TastyChainCache
    session: dt.date
    oi_label: str = "OI dne seance"
    #: Počty změn kotace a greeks v okamžiku odběru, per streamer
    _base: dict[str, tuple[int, int]] = field(default_factory=dict)

    def mark(self, streamers: Iterable[str]) -> None:
        """Zapamatuje stav před odběrem; event z odběru přijde až po probuzení reconcileru."""
        for streamer in streamers:
            if streamer not in self._base:
                state = self.cache.state(streamer)
                self._base[streamer] = (
                    (state.quote_changes, state.greeks_changes) if state is not None else (0, 0)
                )

    def _live(self, streamer: str, state: TastyContractState | None) -> tuple[bool, bool]:
        base = self._base.get(streamer)
        if state is None or base is None:
            return False, False
        return (
            state.quote_changes - base[0] >= LIVE_CHANGES,
            state.greeks_changes - base[1] >= LIVE_CHANGES,
        )

    def quote(self, streamer: str, now: dt.datetime) -> bool:
        state = self.cache.state(streamer)
        if state is None or state.quote.updated_at is None or not self._live(streamer, state)[0]:
            return False
        return (now - state.quote.updated_at).total_seconds() <= MAX_AGE_S

    def why(self, streamer: str, now: dt.datetime) -> str:
        state = self.cache.state(streamer)
        base = self._base.get(streamer)
        if state is None or base is None or state.quote.updated_at is None:
            return "bez eventu po odběru"
        changes = state.quote_changes - base[0]
        if changes <= 0:
            return "bez eventu po odběru"
        if changes < LIVE_CHANGES:
            return "jen event z odběru"
        return f"starší {MAX_AGE_S:.0f} s"

    def oi(self, streamer: str) -> bool:
        state = self.cache.state(streamer)
        return (
            state is not None
            and state.summary.open_interest is not None
            and state.summary.day_id == (self.session - _DAY_ID_EPOCH).days
        )

    def state(self, streamer_symbol: str) -> TastyContractState | None:
        """Pohled na cache pro řádky: kotace a greeks bez aktualizace po odběru chybí."""
        state = self.cache.state(streamer_symbol)
        if state is None:
            return None
        quote_live, greeks_live = self._live(streamer_symbol, state)
        return replace(
            state,
            quote=state.quote if quote_live else TastyQuote(),
            greeks=state.greeks if greeks_live else TastyGreeks(),
        )

    def states(self) -> ContractStates:
        return self

    def max_age_s(self, ts: dt.datetime) -> float:
        return MAX_AGE_S


@dataclass(frozen=True)
class _Assessment:
    """Posouzení snímku v okamžiku `ts`: řádky a OI bez hodnoty per expirace, důvody neúplnosti."""

    ts: dt.datetime
    snaps: dict[str, ExpirySnapshot]
    oi_missing: dict[str, list[OiMissingRow]]
    reasons: tuple[str, ...]


@dataclass
class PlanChainSnapshotter:
    """Jednorázový snímek řetězu QQQ; úlohy v 9:15 a 9:31 ET pouští tasty větev enginu."""

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

    async def run(self, session: dt.date, slot: SnapshotSlot) -> PlanChainSnapshot | None:
        """Snímek seance; při zavřeném trhu None (jen INFO), subskripce se vždy uvolní.

        Volá se v náběhu (`next_snapshot`); start po hranici vrátí neúplný snímek s důvodem
        a nic neodebírá.
        """
        if not is_trading_session(session):
            raise ValueError(f"snímek řetězu {PRODUCT} jen v obchodní seanci, {session} ji nemá")
        now = self.clock()
        if is_market_closed(now):
            logger.info(
                "Snímek řetězu %s %s ET: trh zavřený %s — nespouští se", PRODUCT, slot.label, now
            )
            return None
        try:
            return await self._run(session, slot, now)
        finally:
            if self._symbols:
                self._symbols.clear()
                self._notify()

    async def _run(
        self, session: dt.date, slot: SnapshotSlot, started_at: dt.datetime
    ) -> PlanChainSnapshot:
        boundary = snapshot_boundary(session, slot)
        deadline = boundary + dt.timedelta(seconds=QUOTE_TIMEOUT_S)
        rules: _Freshness = (
            _PreOpen(self.cache, session)
            if slot.pre_open
            else _AfterOpen(self.cache, us_open_ts(session))
        )
        reasons: list[str] = []
        empty = PlanChainSnapshot(
            session=session,
            slot=slot,
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
            # Probuzení po hranici (uspaný hostitel, start uprostřed náběhu po hranici): vzorek
            # cen by nebyl stavem hranice a snímek by nesl jinou minutu než plán
            reasons.append(f"hranice {slot.label} ET zmeškána (start {started_at:%H:%M:%S} UTC)")
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

        rules.mark([underlying])
        self._symbols = {underlying}
        self._notify()
        band: dict[str, frozenset[str]] | None = None
        if slot.pre_open:
            # Pásmo už v náběhu (#1768): kotace se počítá až po aktualizaci, ta potřebuje čas
            early, _ = await self._wait_spot(underlying, rules, deadline)
            if early is None:
                reasons.append(f"kotace {PRODUCT} nedorazila do {deadline:%H:%M:%S} UTC")
                return replace(empty, reasons=tuple(reasons))
            band = band_streamers(chain, expiries, early)
            self._subscribe(underlying, band, rules)
        await self._sleep_until(boundary)
        spot, spot_ts = await self._wait_spot(underlying, rules, deadline)
        if spot is None or spot_ts is None:
            reasons.append(f"kotace {PRODUCT} nedorazila do {deadline:%H:%M:%S} UTC")
            return replace(empty, reasons=tuple(reasons))
        reading = self.reference_spot()
        spot_nq = reading.live
        if spot_nq is None or not math.isfinite(spot_nq) or spot_nq <= 0:
            spot_nq = None
            reasons.append(f"spot {REFERENCE} chybí ({reading.describe(spot_ts)})")
        ratio = spot_nq / spot if spot_nq is not None else None

        if band is None:
            band = band_streamers(chain, expiries, spot)
            self._subscribe(underlying, band, rules)
        chains = {
            expiry: ChainSymbols(
                product=chain.product,
                day=chain.day,
                by_contract={
                    key: streamer
                    for key, streamer in chain.by_contract.items()
                    if key[0] == expiry and streamer in band[expiry]
                },
            )
            for expiry in expiries
        }
        while True:
            assessed = self._assess(chains, zero, underlying, spot, rules, slot)
            if not assessed.reasons or assessed.ts >= deadline:
                break
            await self.sleep(POLL_S)

        reasons.extend(assessed.reasons)
        day = assessed.ts.replace(second=0, microsecond=0).date()
        for expiry, snap in assessed.snaps.items():
            if not snap.rows:
                continue
            await asyncio.to_thread(self.writer.write_minute, PRODUCT, expiry, day, list(snap.rows))
            if assessed.oi_missing[expiry]:
                await asyncio.to_thread(
                    self.writer.write_oi_missing, PRODUCT, expiry, day, assessed.oi_missing[expiry]
                )
        return replace(
            empty,
            reasons=tuple(reasons),
            spot_ts=spot_ts,
            spot=spot,
            spot_nq=spot_nq,
            nq_source=reading.source if spot_nq is not None else None,
            ratio=ratio,
            ts=assessed.ts,
            zero_dte=assessed.snaps.get(zero) if zero is not None else None,
            one_dte=assessed.snaps.get(one) if one is not None else None,
        )

    def _assess(
        self,
        chains: dict[str, ChainSymbols],
        zero: str | None,
        underlying: str,
        spot: float,
        rules: _Freshness,
        slot: SnapshotSlot,
    ) -> _Assessment:
        """Řádky všech expirací a důvody neúplnosti v jednom okamžiku, bez await (K4 bod 7, B).

        EM se posuzuje ke `spot` hranice, tedy k ceně, se kterou plán vznikne.
        """
        ts = self.clock()
        ts_min = ts.replace(second=0, microsecond=0)
        build_spot = self._mid(underlying, ts, rules) or spot
        max_age_s = rules.max_age_s(ts)
        states = rules.states()
        limit = f"do {QUOTE_TIMEOUT_S:.0f} s od {slot.label} ET"
        snaps: dict[str, ExpirySnapshot] = {}
        oi_missing_of: dict[str, list[OiMissingRow]] = {}
        reasons: list[str] = []
        for expiry, band_chain in chains.items():
            contracts = band_chain.by_contract
            rows, oi_missing = build_snapshot_rows(
                band_chain,
                expiry,
                states,
                ts_min=ts_min,
                spot=build_spot,
                now_utc=ts,
                max_age_s=max_age_s,
            )
            rows, oi_missing = self._drop_stale_oi(band_chain, rows, oi_missing, rules)
            present = {(row.strike, row.right) for row in rows}
            quoted = 0
            missing: dict[str, list[str]] = {}
            for (_expiry, strike, right), streamer in sorted(contracts.items()):
                fresh = rules.quote(streamer, ts)
                quoted += fresh
                if (strike, right) not in present:
                    why = ROW_REJECTED if fresh else rules.why(streamer, ts)
                    missing.setdefault(why, []).append(f"{strike:g}{right}")
            with_oi = sum(1 for streamer in contracts.values() if rules.oi(streamer))
            snap = ExpirySnapshot(
                expiry=expiry,
                contracts=len(contracts),
                quoted=quoted,
                with_oi=with_oi,
                rows=tuple(rows),
                oi_missing=len(oi_missing),
                missing=tuple((why, tuple(names)) for why, names in missing.items()),
            )
            snaps[expiry] = snap
            oi_missing_of[expiry] = oi_missing
            label = "0DTE" if expiry == zero else "1DTE"
            if snap.oi_share < MIN_OI_SHARE:
                reasons.append(
                    f"{label} {expiry}: {rules.oi_label} {with_oi}/{len(contracts)} "
                    f"({snap.oi_share:.0%}) {limit}"
                )
            if not rows:
                reasons.append(f"{label} {expiry}: žádný platný řádek")
                continue
            if expiry == zero:
                calls = {strike for (_e, strike, right) in contracts if right == "C"}
                puts = {strike for (_e, strike, right) in contracts if right == "P"}
                gap = em_gap(rows, calls & puts, spot)
                gaps: tuple[str, ...] = (gap,) if gap is not None else ()
            else:
                ladder: dict[str, set[float]] = {"C": set(), "P": set()}
                for _e, strike, right in contracts:
                    ladder.setdefault(right, set()).add(strike)
                gaps = variant_gaps(rows, ladder)
            reasons.extend(f"{label} {expiry}: {gap} {limit}" for gap in gaps)
        return _Assessment(ts=ts, snaps=snaps, oi_missing=oi_missing_of, reasons=tuple(reasons))

    def _notify(self) -> None:
        try:
            self.on_change()
        except Exception:
            logger.exception("Snímek řetězu %s: probuzení plánu subskripce selhalo", PRODUCT)

    def _subscribe(
        self, underlying: str, band: dict[str, frozenset[str]], rules: _Freshness
    ) -> None:
        """Odebere pásmo; stav před odběrem si pravidla zapamatují dřív, než přijde event."""
        streamers = {s for members in band.values() for s in members}
        rules.mark(streamers)
        self._symbols = {underlying, *streamers}
        self._notify()

    async def _sleep_until(self, moment: dt.datetime) -> None:
        wait = (moment - self.clock()).total_seconds()
        if wait > 0:
            await self.sleep(wait)

    def _mid(self, streamer: str, now: dt.datetime, rules: _Freshness) -> float | None:
        state = self.cache.state(streamer)
        if state is None or not rules.quote(streamer, now):
            return None
        bid, ask = state.quote.bid, state.quote.ask
        if bid is None or ask is None or bid <= 0 or ask < bid:
            return None
        return (bid + ask) / 2.0

    async def _wait_spot(
        self, underlying: str, rules: _Freshness, deadline: dt.datetime
    ) -> tuple[float | None, dt.datetime | None]:
        while True:
            now = self.clock()
            mid = self._mid(underlying, now, rules)
            if mid is not None:
                return mid, now
            if now >= deadline:
                return None, None
            await self.sleep(POLL_S)

    def _drop_stale_oi(
        self,
        chain: ChainSymbols,
        rows: list[SnapshotRow],
        oi_missing: list[OiMissingRow],
        rules: _Freshness,
    ) -> tuple[list[SnapshotRow], list[OiMissingRow]]:
        """OI, které pravidla snímku nepřijmou (starší publikace), = chybějící OI (#465)."""
        streamer_of = {(strike, right): s for (_e, strike, right), s in chain.by_contract.items()}
        missing = list(oi_missing)
        known = {(row.strike, row.right) for row in oi_missing}
        kept: list[SnapshotRow] = []
        for row in rows:
            key = (row.strike, row.right)
            if key not in known and not rules.oi(streamer_of[key]):
                row = replace(row, oi=0.0)
                missing.append(OiMissingRow(ts_min=row.ts_min, strike=row.strike, right=row.right))
            kept.append(row)
        return kept, missing
