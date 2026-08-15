"""SubscriptionScheduler (SPEC 3.3): rotační sweep opčního řetězce v dávkách.

Cyklus dávky: subskribuj → čekej na kompletní sadu (bid/ask/last/volume + Greeks)
nebo timeout → ulož do cache → odsubskribuj → další dávka. Kontrakty bez
kompletních dat jdou do repair fronty s retry a exponenciálním backoffem per
kontrakt (#547); po vyčerpání pokusů jsou označeny jako stale se stářím.
ATM ± `atm_sweep_width` strikes se sweepuje každý cyklus, křídla každý
`wings_sweep_every`-tý cyklus.

Fallback greeks (#547): když kotace strike tečou, ale TWS trvale nedodává
modelGreeks (7. 8.: celé ATM pásmo NQ QN1 0DTE), dopočítá scheduler po
`greeks_fallback_sweeps` sweepech vlastní BS greeks z mid ceny a strike označí
zdrojem `computed` — snapshot se zapíše místo celodenní díry.
"""

import asyncio
import datetime as dt
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from gexlens_engine.compute.gexfield import fallback_greeks
from gexlens_engine.compute.settle import settle_ts
from gexlens_engine.config import Settings
from gexlens_engine.ibkr.discovery import OptionContractSpec

logger = logging.getLogger(__name__)

# Zdroj Greeks v cache (#547): TWS model vs. vlastní BS dopočet z mid ceny
GREEKS_SOURCE_MODEL = "model"
GREEKS_SOURCE_COMPUTED = "computed"


@dataclass(frozen=True)
class QuoteSnapshot:
    """Kompletní sada dat jednoho kontraktu z jedné subskripce (SPEC 3.3)."""

    bid: float
    ask: float
    last: float
    volume: float
    iv: float
    delta: float
    gamma: float
    theta: float
    vega: float


@dataclass(frozen=True)
class PartialQuote:
    """Kotace bez TWS Greeks (#547): bid/ask/last/volume dorazily, modelGreeks ne.

    Streamer ji vrací místo None, když subskripce žije, ale opční model TWS
    mlčí — scheduler z ní po `greeks_fallback_sweeps` sweepech dopočítá vlastní
    BS greeks místo věčně nekompletního striku.
    """

    bid: float
    ask: float
    last: float
    volume: float


@dataclass
class CachedQuote:
    """Poslední kompletní data kontraktu; stale = poslední sweep je nedokázal obnovit."""

    snapshot: QuoteSnapshot
    updated_at: float
    stale: bool = False
    # Zdroj Greeks (#547): "model" = TWS modelGreeks, "computed" = vlastní BS dopočet
    source: str = GREEKS_SOURCE_MODEL

    def age_s(self, now: float) -> float:
        return now - self.updated_at


@dataclass(frozen=True)
class SweepMetrics:
    """Metriky jednoho sweepu pro stavovou lištu (SPEC 3.7: Greeks X/Y, Repair).

    Lines % tu není (#630): obsazené linky jsou vlastnost účtu napříč pipeline,
    měří je `ibkr.lines.LineGauge` a do statusu je dává orchestrátor.
    """

    total: int
    greeks_complete: int
    repair_count: int
    stale_count: int
    sweep_duration_s: float
    # Striky s vlastními dopočtenými greeks (#547) — TWS model je nedodává
    computed_greeks: int = 0
    # OI aktivního řetězu (#664): kolik kontraktů má OI z archivu IBKR, kolik
    # doplnil tasty fill a kolik zůstalo bez hodnoty — plní run_cycle po sweepu
    oi_present: int = 0
    oi_filled: int = 0
    oi_missing: int = 0
    # Kontrakty s ≥ repair_stall_rounds neúspěšnými repair koly (#547) — vstup
    # alertu strikes_stalled v pipeline
    stalled_count: int = 0


def repair_delay_s(rounds: int, base_s: float, max_s: float) -> float:
    """Odklad dalšího repair pokusu po `rounds` neúspěšných kolech (#547).

    První kolo bez odkladu (okamžitý retry v témže sweepu), dál exponenciálně
    se stropem: 0 → base → 2·base → 4·base → … → max_s. Bez backoffu mlel
    repair trvale vadné kontrakty à 4 s celé hodiny bez jediného úspěchu.
    """
    if rounds <= 1:
        return 0.0
    return min(base_s * 2.0 ** (rounds - 2), max_s)


class GreeksStallDetector:
    """Detekce tichého výpadku Greeks (#306) — obdoba `BarsStallDetector` (#221).

    27. 7. přestala TWS ve 22:34 počítat `modelGreeks` pro ATM striky; ceny a OI
    chodily dál, takže se výpadek nijak neprojevil. Engine kontrakty správně
    označil za stale, ale cache dál servírovala poslední známou kotaci, takže
    se 15 hodin zapisovala zmrzlá čísla a nikdo si toho nevšiml.

    Podíl stale kontraktů nad prahem po `stall_cycles` po sobě jdoucích sweepech
    ohlásí `"stalled"`, návrat pod práh `"recovered"` — obojí právě jednou.
    Sweep bez kontraktů se nehodnotí (není z čeho).
    """

    def __init__(self, stale_share: float, stall_cycles: int) -> None:
        self._stale_share = stale_share
        self._stall_cycles = stall_cycles
        self._bad_cycles = 0
        self._stalled = False

    @property
    def stalled(self) -> bool:
        return self._stalled

    def observe(self, *, total: int, stale: int) -> str | None:
        if total <= 0:
            return None
        if stale / total <= self._stale_share:
            self._bad_cycles = 0
            if self._stalled:
                self._stalled = False
                return "recovered"
            return None
        self._bad_cycles += 1
        if not self._stalled and self._bad_cycles >= self._stall_cycles:
            self._stalled = True
            return "stalled"
        return None


class RepairStallDetector:
    """Trvale selhávající repair (#547) — hrana pro alert `strikes_stalled`.

    7. 8. TWS celou seanci nedodávala modelGreeks pro ATM pásmo NQ; repair běžel
    hodiny à 4 s bez jediného úspěchu a bez jediné hlášky. Počet kontraktů
    s ≥ `repair_stall_rounds` neúspěšnými koly nese scheduler v metrikách;
    tady se z něj dělá "stalled"/"recovered" právě jednou (vzor bars_stalled).
    """

    def __init__(self) -> None:
        self._stalled = False

    @property
    def stalled(self) -> bool:
        return self._stalled

    def observe(self, stalled_count: int) -> str | None:
        if stalled_count > 0 and not self._stalled:
            self._stalled = True
            return "stalled"
        if stalled_count == 0 and self._stalled:
            self._stalled = False
            return "recovered"
        return None


class QuoteStreamerLike(Protocol):
    """Zdroj kotací: subskribuje kontrakt, počká na kompletní sadu nebo timeout.

    Vrací None při nekompletních datech; `PartialQuote`, když kotace tečou, ale
    TWS model Greeks nedodal (#547). Testy používají
    `gexlens_engine.ibkr.mock.MockQuoteStreamer`; produkční implementace nad
    ib_async reqMktData je `adapters.IbQuoteStreamer`.
    """

    async def fetch_quote(
        self, spec: OptionContractSpec, timeout_s: float
    ) -> QuoteSnapshot | PartialQuote | None: ...


class SubscriptionScheduler:
    """Rotuje subskripce řetězce v dávkách a udržuje in-memory cache kotací."""

    def __init__(
        self,
        streamer: QuoteStreamerLike,
        settings: Settings,
        *,
        clock: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._streamer = streamer
        self._settings = settings
        # Injektovatelné hodiny kvůli testům backoffu (#547): monotonic pro
        # odklady, UTC pro τ do expirace ve fallback greeks
        self._clock = clock
        self._utc_now = utc_now or (lambda: dt.datetime.now(dt.UTC))
        self._cache: dict[OptionContractSpec, CachedQuote] = {}
        self._stale: set[OptionContractSpec] = set()
        self._cycle = 0
        # Repair backoff per kontrakt (#547): počet neúspěšných kol a čas,
        # kdy je kontrakt zase na řadě
        self._fail_rounds: dict[OptionContractSpec, int] = {}
        self._due_at: dict[OptionContractSpec, float] = {}
        # Po kolika sweepech v řadě chybí TWS greeks (vstup fallback prahu)
        self._no_greeks_sweeps: dict[OptionContractSpec, int] = {}
        # Částečné kotace aktuálního sweepu (kotace ano, greeks ne)
        self._partials: dict[OptionContractSpec, PartialQuote] = {}
        self.last_metrics: SweepMetrics | None = None

    @property
    def cycle(self) -> int:
        return self._cycle

    @property
    def stale_contracts(self) -> set[OptionContractSpec]:
        """Kontrakty, které poslední sweep nedokázal obnovit (pro UI a stale_age)."""
        return set(self._stale)

    def quote(self, spec: OptionContractSpec) -> CachedQuote | None:
        return self._cache.get(spec)

    def quotes(self) -> dict[OptionContractSpec, CachedQuote]:
        return dict(self._cache)

    async def sweep(self, contracts: Sequence[OptionContractSpec], spot: float) -> SweepMetrics:
        """Jeden kompletní sweep: výběr dle priority, dávky, repair s backoffem, metriky."""
        start = self._clock()
        selected = self._select_contracts(contracts, spot)
        self._prune_state(contracts)
        self._partials.clear()

        # Kontrakty v backoffu se nefetchují (#547) — trvale vadný strike nesmí
        # každý sweep pálit subskripci + timeout; jeho cache a stale stav se drží
        now = self._clock()
        due = [spec for spec in selected if self._due_at.get(spec, 0.0) <= now]
        deferred = [spec for spec in selected if self._due_at.get(spec, 0.0) > now]

        incomplete = await self._fetch_in_batches(due)
        repair_count = len(incomplete) + len(deferred)

        # Repair fronta: retry jen kontraktů, které jsou po backoffu na řadě.
        # Log jde jednou za sweep (#547) — dřív „Repair: retrying" spamoval
        # každé kolo (3× během 10 s), hodiny v kuse.
        if incomplete:
            logger.info(
                "Repair: %d nekompletních striků (v backoffu čeká dalších %d)",
                len(incomplete),
                len(deferred),
            )
        attempts_left = self._settings.repair_max_attempts
        while incomplete and attempts_left > 0:
            now = self._clock()
            due_retry = [spec for spec in incomplete if self._due_at.get(spec, 0.0) <= now]
            if not due_retry:
                break
            failed = set(await self._fetch_in_batches(due_retry))
            due_set = set(due_retry)
            incomplete = [spec for spec in incomplete if spec not in due_set or spec in failed]
            attempts_left -= 1

        # Fallback vlastní greeks (#547): kotace tečou, TWS model mlčí → po
        # `greeks_fallback_sweeps` sweepech se greeks dopočítají BS modelem
        # z mid ceny. Kraje (cena pod vnitřní hodnotou, nekonvergující IV)
        # nechávají strike nekompletní — vymyšlené hodnoty jsou horší než díra.
        computed_now: set[OptionContractSpec] = set()
        for spec in incomplete:
            streak = self._no_greeks_sweeps.get(spec, 0) + 1
            self._no_greeks_sweeps[spec] = streak
            partial = self._partials.get(spec)
            if partial is None or streak < self._settings.greeks_fallback_sweeps:
                continue
            snapshot = self._fallback_snapshot(spec, partial, spot)
            if snapshot is None:
                continue
            self._cache[spec] = CachedQuote(
                snapshot=snapshot, updated_at=self._clock(), source=GREEKS_SOURCE_COMPUTED
            )
            self._stale.discard(spec)
            computed_now.add(spec)
        if computed_now:
            logger.info(
                "Fallback greeks: %d striků dopočteno BS modelem z mid ceny "
                "(TWS model je nedodává)",
                len(computed_now),
            )
        incomplete = [spec for spec in incomplete if spec not in computed_now]

        # Po vyčerpání pokusů: stale označení (stáří nese cache záznam, pokud
        # existuje). Odložené kontrakty se nehodnotí — nebyl pokus, stav se drží.
        for spec in incomplete:
            self._stale.add(spec)
            cached = self._cache.get(spec)
            if cached is not None:
                cached.stale = True

        self._cycle += 1
        stale_count = len(incomplete) + sum(1 for spec in deferred if spec in self._stale)
        stall_rounds = self._settings.repair_stall_rounds
        metrics = SweepMetrics(
            total=len(selected),
            greeks_complete=len(selected) - stale_count,
            repair_count=repair_count,
            stale_count=stale_count,
            sweep_duration_s=self._clock() - start,
            computed_greeks=sum(
                1
                for spec in selected
                if spec not in self._stale
                and (cached := self._cache.get(spec)) is not None
                and cached.source == GREEKS_SOURCE_COMPUTED
            ),
            stalled_count=sum(1 for rounds in self._fail_rounds.values() if rounds >= stall_rounds),
        )
        self.last_metrics = metrics
        return metrics

    def _select_contracts(
        self, contracts: Sequence[OptionContractSpec], spot: float
    ) -> list[OptionContractSpec]:
        """Priorita sweepu: ATM ± atm_sweep_width strikes vždy, křídla každý k-tý cyklus."""
        strikes = sorted({c.strike for c in contracts})
        if not strikes:
            return []
        atm_index = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
        low = max(0, atm_index - self._settings.atm_sweep_width)
        high = atm_index + self._settings.atm_sweep_width
        atm_strikes = set(strikes[low : high + 1])
        include_wings = self._cycle % self._settings.wings_sweep_every == 0
        return [c for c in contracts if include_wings or c.strike in atm_strikes]

    def _prune_state(self, contracts: Sequence[OptionContractSpec]) -> None:
        """Zahodí repair stav kontraktů mimo aktuální řetěz (#547).

        Posun pásma nebo roll expirace by jinak nechal mrtvé kontrakty
        v `_fail_rounds` navždy a alert strikes_stalled by se nikdy nezotavil.
        """
        universe = set(contracts)
        for state in (self._fail_rounds, self._due_at, self._no_greeks_sweeps):
            for spec in [spec for spec in state if spec not in universe]:
                del state[spec]

    async def _fetch_in_batches(
        self, specs: Sequence[OptionContractSpec]
    ) -> list[OptionContractSpec]:
        """Stáhne kotace po dávkách batch_size; vrátí kontrakty bez kompletních dat."""
        incomplete: list[OptionContractSpec] = []
        batch_size = self._settings.batch_size
        for offset in range(0, len(specs), batch_size):
            batch = specs[offset : offset + batch_size]
            results = await asyncio.gather(*(self._fetch_one(spec) for spec in batch))
            now = self._clock()
            for spec, result in zip(batch, results, strict=True):
                if isinstance(result, QuoteSnapshot):
                    self._cache[spec] = CachedQuote(snapshot=result, updated_at=now)
                    self._stale.discard(spec)
                    self._clear_repair_state(spec)
                    continue
                if isinstance(result, PartialQuote):
                    self._partials[spec] = result
                self._register_failure(spec, now)
                incomplete.append(spec)
        return incomplete

    def _register_failure(self, spec: OptionContractSpec, now: float) -> None:
        """Neúspěšné kolo: eskaluje backoff kontraktu (#547)."""
        rounds = self._fail_rounds.get(spec, 0) + 1
        self._fail_rounds[spec] = rounds
        self._due_at[spec] = now + repair_delay_s(
            rounds,
            self._settings.repair_backoff_base_s,
            self._settings.repair_backoff_max_s,
        )

    def _clear_repair_state(self, spec: OptionContractSpec) -> None:
        """TWS zase dodává kompletní data — backoff i fallback čítače končí."""
        self._fail_rounds.pop(spec, None)
        self._due_at.pop(spec, None)
        self._no_greeks_sweeps.pop(spec, None)

    def _fallback_snapshot(
        self, spec: OptionContractSpec, partial: PartialQuote, spot: float
    ) -> QuoteSnapshot | None:
        """BS greeks z mid ceny (#547); None = nejde poctivě dopočítat.

        τ z expirace přes sdílenou settle konvenci (#511). Nevalidní kotace,
        cena mimo no-arbitrage pásmo nebo nekonvergující IV nechávají strike
        nekompletní.
        """
        if not (partial.bid > 0.0 and partial.ask >= partial.bid and spot > 0.0):
            return None
        try:
            expiry_date = dt.datetime.strptime(spec.expiry, "%Y%m%d").date()
        except ValueError:
            logger.warning("Nečitelná expirace %r — fallback greeks se přeskakuje", spec.expiry)
            return None
        mid = (partial.bid + partial.ask) / 2.0
        greeks = fallback_greeks(
            spot=spot,
            strike=spec.strike,
            right=spec.right,
            mid=mid,
            settle=settle_ts(expiry_date),
            now=self._utc_now(),
        )
        if greeks is None:
            return None
        return QuoteSnapshot(
            bid=partial.bid,
            ask=partial.ask,
            last=partial.last if partial.last > 0.0 else mid,
            volume=partial.volume,
            iv=greeks.iv,
            delta=greeks.delta,
            gamma=greeks.gamma,
            theta=greeks.theta,
            vega=greeks.vega,
        )

    async def _fetch_one(self, spec: OptionContractSpec) -> QuoteSnapshot | PartialQuote | None:
        try:
            return await self._streamer.fetch_quote(spec, self._settings.batch_timeout_s)
        except Exception:
            # Chyba streamu = nekompletní kontrakt; nesmí shodit celý sweep (SPEC kap. 8)
            logger.exception("fetch_quote selhal pro %s", spec)
            return None
