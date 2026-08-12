"""Testy SubscriptionScheduleru (issue #7): dávky, repair fronta, stale, priorita ATM/křídla.

#547 přidává repair backoff per kontrakt a fallback vlastních BS greeks — testy
používají FakeClock, aby se odklady daly řídit deterministicky.
"""

import datetime as dt

from gexlens_engine.config import Settings
from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.ibkr.mock import MockQuoteStreamer
from gexlens_engine.ibkr.scheduler import (
    GREEKS_SOURCE_COMPUTED,
    GREEKS_SOURCE_MODEL,
    GreeksStallDetector,
    PartialQuote,
    QuoteSnapshot,
    RepairStallDetector,
    SubscriptionScheduler,
    repair_delay_s,
)

SPOT = 7600.0
# Pevné „teď" pro fallback greeks: týden před expirací řetězce (τ > 0)
UTC_NOW = dt.datetime(2026, 7, 10, 14, 0, tzinfo=dt.UTC)


class FakeClock:
    """Deterministický monotonic čas pro testy backoffu (#547)."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def chain_360() -> list[OptionContractSpec]:
    """Simulovaný řetězec 360 kontraktů: 180 strikes (krok 5) × C/P (AC issue #7)."""
    strikes = [float(k) for k in range(7155, 8055, 5)]
    assert len(strikes) == 180
    return [
        OptionContractSpec(
            symbol="ES",
            sec_type="FOP",
            expiry="20260716",
            strike=strike,
            right=right,
            exchange="CME",
            trading_class="E3D",
            multiplier="50",
        )
        for strike in strikes
        for right in ("C", "P")
    ]


async def test_full_sweep_caches_everything_under_90s() -> None:
    contracts = chain_360()
    streamer = MockQuoteStreamer(delay_s=0.01)
    scheduler = SubscriptionScheduler(streamer, Settings())

    metrics = await scheduler.sweep(contracts, SPOT)

    assert metrics.total == 360  # cyklus 0 zahrnuje křídla
    assert metrics.greeks_complete == 360
    assert metrics.repair_count == 0
    assert metrics.stale_count == 0
    assert metrics.sweep_duration_s <= 90  # AC: kompletní sweep ≤ 90 s
    assert len(scheduler.quotes()) == 360
    assert scheduler.stale_contracts == set()


async def test_batching_respects_batch_size_and_lines() -> None:
    contracts = chain_360()
    streamer = MockQuoteStreamer(delay_s=0.005)
    scheduler = SubscriptionScheduler(streamer, Settings())

    await scheduler.sweep(contracts, SPOT)

    assert streamer.max_concurrent <= 80  # dávka nikdy nepřekročí batch_size


async def test_incomplete_contracts_repair_and_recover() -> None:
    contracts = chain_360()
    flaky = {spec: 1 for spec in contracts[:10]}  # 10 kontraktů selže na 1. pokus
    streamer = MockQuoteStreamer(fail_first=flaky)
    scheduler = SubscriptionScheduler(streamer, Settings())

    metrics = await scheduler.sweep(contracts, SPOT)

    # AC: nekompletní kontrakty končí v repair frontě a po retry se doplní
    assert metrics.repair_count == 10
    assert metrics.greeks_complete == 360
    assert metrics.stale_count == 0
    assert len(streamer.fetch_calls) == 360 + 10
    assert scheduler.stale_contracts == set()


async def test_persistent_failures_marked_stale_after_max_attempts() -> None:
    contracts = chain_360()
    dead = set(contracts[:4])
    streamer = MockQuoteStreamer(always_fail=dead)
    settings = Settings(repair_max_attempts=3)
    scheduler = SubscriptionScheduler(streamer, settings, clock=FakeClock())

    metrics = await scheduler.sweep(contracts, SPOT)

    assert metrics.repair_count == 4
    assert metrics.stale_count == 4
    assert metrics.greeks_complete == 356
    assert scheduler.stale_contracts == dead
    # 1 pokus v dávce + 1 okamžitý repair; další kola až po backoffu (#547)
    dead_calls = [spec for spec in streamer.fetch_calls if spec in dead]
    assert len(dead_calls) == 4 * (1 + 1)


async def test_stale_flag_clears_after_recovery() -> None:
    contracts = chain_360()
    # 2 ATM kontrakty (sweepují se každý cyklus) selžou celý první sweep
    # (dávka + okamžitý repair), pak se v dalším sweepu zotaví
    atm = [spec for spec in contracts if spec.strike == SPOT]
    assert len(atm) == 2
    flaky = {spec: 2 for spec in atm}
    streamer = MockQuoteStreamer(fail_first=flaky)
    clock = FakeClock()
    scheduler = SubscriptionScheduler(streamer, Settings(), clock=clock)

    first = await scheduler.sweep(contracts, SPOT)
    assert first.stale_count == 2

    clock.advance(60.0)  # minutová kadence — backoff 4 s dávno vypršel
    second = await scheduler.sweep(contracts, SPOT)
    assert second.stale_count == 0
    assert scheduler.stale_contracts == set()
    cached = scheduler.quote(atm[0])
    assert cached is not None
    assert not cached.stale
    assert cached.source == GREEKS_SOURCE_MODEL


async def test_wings_swept_only_every_kth_cycle() -> None:
    contracts = chain_360()
    streamer = MockQuoteStreamer()
    settings = Settings(wings_sweep_every=3, atm_sweep_width=30)
    scheduler = SubscriptionScheduler(streamer, settings)

    m0 = await scheduler.sweep(contracts, SPOT)
    m1 = await scheduler.sweep(contracts, SPOT)
    m2 = await scheduler.sweep(contracts, SPOT)
    m3 = await scheduler.sweep(contracts, SPOT)

    assert m0.total == 360  # cyklus 0: ATM + křídla
    # Cykly 1–2: jen ATM ± 30 strikes = 61 strikes × C/P
    assert m1.total == 61 * 2
    assert m2.total == 61 * 2
    assert m3.total == 360  # cyklus 3: opět křídla


async def test_streamer_exception_does_not_kill_sweep() -> None:
    contracts = chain_360()[:8]

    class ExplodingStreamer(MockQuoteStreamer):
        async def fetch_quote(
            self, spec: OptionContractSpec, timeout_s: float
        ) -> QuoteSnapshot | PartialQuote | None:
            if spec is contracts[0]:
                raise RuntimeError("mock: pacing violation")
            return await super().fetch_quote(spec, timeout_s)

    streamer = ExplodingStreamer()
    scheduler = SubscriptionScheduler(streamer, Settings())

    metrics = await scheduler.sweep(contracts, SPOT)

    # Výjimka streamu = nekompletní kontrakt, sweep běží dál (SPEC kap. 8: odolnost)
    assert metrics.greeks_complete == 7
    assert metrics.stale_count == 1


# ── Detekce tiché ztráty Greeks (#306) ─────────────────────────────


def test_greeks_stall_detector_reports_once_and_recovers() -> None:
    """Podíl stale nad prahem po N sweepech → "stalled"; návrat → "recovered"."""
    detector = GreeksStallDetector(stale_share=0.1, stall_cycles=3)

    # 27 % stale, ale ještě ne dost cyklů
    assert detector.observe(total=222, stale=61) is None
    assert detector.observe(total=222, stale=61) is None
    assert detector.observe(total=222, stale=61) == "stalled"
    assert detector.stalled
    # Alert jde právě jednou, i když stav trvá
    assert detector.observe(total=222, stale=61) is None

    assert detector.observe(total=228, stale=0) == "recovered"
    assert not detector.stalled
    assert detector.observe(total=228, stale=0) is None


def test_greeks_stall_detector_ignores_noise_and_empty_sweeps() -> None:
    detector = GreeksStallDetector(stale_share=0.1, stall_cycles=3)
    # Občasný výpadek pod prahem sérii nezaloží
    for _ in range(10):
        assert detector.observe(total=100, stale=5) is None
    # Prázdný sweep není z čeho hodnotit a nesmí sérii ani založit, ani zrušit
    assert detector.observe(total=100, stale=50) is None
    assert detector.observe(total=0, stale=0) is None
    assert detector.observe(total=100, stale=50) is None
    assert detector.observe(total=100, stale=50) == "stalled"


# ── Repair backoff per kontrakt (#547) ─────────────────────────────


def test_repair_delay_roste_exponencialne_se_stropem() -> None:
    """Posloupnost odkladů: okamžitý retry, pak 4 → 8 → 16 → … → strop 300 s."""
    delays = [repair_delay_s(rounds, 4.0, 300.0) for rounds in range(1, 11)]
    assert delays == [0.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0, 256.0, 300.0, 300.0]


async def test_backoff_odklada_pokusy_mrtveho_kontraktu() -> None:
    """#547: mrtvý kontrakt se nefetchuje každý sweep, ale až po vypršení odkladu."""
    contracts = chain_360()
    dead = {spec for spec in contracts if spec.strike == SPOT}
    assert len(dead) == 2
    streamer = MockQuoteStreamer(always_fail=dead)
    clock = FakeClock()
    scheduler = SubscriptionScheduler(streamer, Settings(), clock=clock)

    def dead_calls() -> int:
        return sum(1 for spec in streamer.fetch_calls if spec in dead)

    await scheduler.sweep(contracts, SPOT)
    assert dead_calls() == 2 * 2  # dávka + okamžitý repair, pak due = +4 s

    # Čas se nehnul → kontrakt je v backoffu, sweep ho přeskočí (stále stale)
    metrics = await scheduler.sweep(contracts, SPOT)
    assert dead_calls() == 2 * 2
    assert metrics.stale_count == 2

    clock.advance(4.0)  # 3. kolo je na řadě → 1 pokus, další due = +8 s
    await scheduler.sweep(contracts, SPOT)
    assert dead_calls() == 2 * 3

    clock.advance(4.0)  # jen 4 s z 8 → pořád v backoffu
    await scheduler.sweep(contracts, SPOT)
    assert dead_calls() == 2 * 3

    clock.advance(4.0)  # 8 s uplynulo → 4. kolo
    await scheduler.sweep(contracts, SPOT)
    assert dead_calls() == 2 * 4


async def test_stalled_count_po_n_kolech_a_navrat() -> None:
    """#547: po repair_stall_rounds neúspěšných kolech metriky hlásí zaseknuté striky."""
    contracts = chain_360()
    dead = {spec for spec in contracts if spec.strike == SPOT and spec.right == "C"}
    streamer = MockQuoteStreamer(always_fail=dead)
    clock = FakeClock()
    scheduler = SubscriptionScheduler(streamer, Settings(repair_stall_rounds=3), clock=clock)

    first = await scheduler.sweep(contracts, SPOT)
    assert first.stalled_count == 0  # 2 kola nestačí

    clock.advance(60.0)
    second = await scheduler.sweep(contracts, SPOT)
    assert second.stalled_count == 1  # 3. kolo neúspěšné

    # TWS se zotaví → stav kontraktu se čistí a stalled_count padá na nulu
    streamer.always_fail.clear()
    clock.advance(60.0)
    third = await scheduler.sweep(contracts, SPOT)
    assert third.stalled_count == 0
    assert third.stale_count == 0


def test_repair_stall_detector_hrana_prave_jednou() -> None:
    """Alert strikes_stalled jde právě jednou; recovery při návratu na nulu."""
    detector = RepairStallDetector()
    assert detector.observe(0) is None
    assert detector.observe(74) == "stalled"
    assert detector.stalled
    assert detector.observe(74) is None  # anti-spam
    assert detector.observe(30) is None  # pořád zaseknuté, jen méně
    assert detector.observe(0) == "recovered"
    assert not detector.stalled
    assert detector.observe(0) is None


# ── Fallback vlastní greeks (#547) ─────────────────────────────────


async def test_fallback_greeks_po_n_sweepech_bez_tws_modelu() -> None:
    """Kotace tečou, TWS model mlčí → po N sweepech BS dopočet z mid ceny."""
    contracts = chain_360()
    atm = [spec for spec in contracts if spec.strike == SPOT]
    streamer = MockQuoteStreamer(partial_greeks=set(atm))
    clock = FakeClock()
    scheduler = SubscriptionScheduler(
        streamer,
        Settings(greeks_fallback_sweeps=2),
        clock=clock,
        utc_now=lambda: UTC_NOW,
    )

    first = await scheduler.sweep(contracts, SPOT)
    assert first.stale_count == 2  # 1. sweep: ještě se čeká na TWS model
    assert first.computed_greeks == 0

    clock.advance(60.0)
    second = await scheduler.sweep(contracts, SPOT)
    assert second.stale_count == 0
    assert second.computed_greeks == 2
    assert second.greeks_complete == second.total

    call = next(spec for spec in atm if spec.right == "C")
    put = next(spec for spec in atm if spec.right == "P")
    cached_call = scheduler.quote(call)
    cached_put = scheduler.quote(put)
    assert cached_call is not None and cached_put is not None
    assert cached_call.source == GREEKS_SOURCE_COMPUTED
    assert not cached_call.stale
    # BS hodnoty ze skutečné inverze: IV > 0, ATM call delta ~0.5, gamma > 0
    assert cached_call.snapshot.iv > 0.0
    assert 0.4 < cached_call.snapshot.delta < 0.6
    assert cached_call.snapshot.gamma > 0.0
    assert -0.6 < cached_put.snapshot.delta < -0.4
    # Kotace se přebírají z částečného snapshotu
    assert cached_call.snapshot.bid == 10.0
    assert cached_call.snapshot.ask == 10.5


async def test_fallback_nekonverguje_strike_zustava_nekompletni() -> None:
    """Kraj (#547): mid pod vnitřní hodnotou → žádné vymyšlené hodnoty, stale."""
    contracts = chain_360()
    itm = {spec for spec in contracts if spec.strike == 7500.0 and spec.right == "C"}
    # Mock mid 10.25 « vnitřní hodnota 100 → IV inverze musí odmítnout
    streamer = MockQuoteStreamer(partial_greeks=itm)
    clock = FakeClock()
    scheduler = SubscriptionScheduler(
        streamer,
        Settings(greeks_fallback_sweeps=1),
        clock=clock,
        utc_now=lambda: UTC_NOW,
    )

    metrics = await scheduler.sweep(contracts, SPOT)

    assert metrics.computed_greeks == 0
    assert metrics.stale_count == 1
    assert scheduler.stale_contracts == itm
    assert scheduler.quote(next(iter(itm))) is None


async def test_fallback_ustoupi_kdyz_se_tws_model_vrati() -> None:
    """Návrat TWS modelu: zdroj greeks se vrací na model a stav repair se čistí."""
    contracts = chain_360()
    atm_call = next(spec for spec in contracts if spec.strike == SPOT and spec.right == "C")
    # První 2 pokusy jen kotace bez greeks, od 3. plná sada z TWS
    streamer = MockQuoteStreamer(partial_first={atm_call: 2})
    clock = FakeClock()
    scheduler = SubscriptionScheduler(
        streamer,
        Settings(greeks_fallback_sweeps=1),
        clock=clock,
        utc_now=lambda: UTC_NOW,
    )

    first = await scheduler.sweep(contracts, SPOT)
    assert first.computed_greeks == 1  # dávka + repair bez modelu → fallback
    cached = scheduler.quote(atm_call)
    assert cached is not None
    assert cached.source == GREEKS_SOURCE_COMPUTED

    clock.advance(60.0)
    second = await scheduler.sweep(contracts, SPOT)
    assert second.computed_greeks == 0
    assert second.stalled_count == 0
    cached = scheduler.quote(atm_call)
    assert cached is not None
    assert cached.source == GREEKS_SOURCE_MODEL
    assert cached.snapshot.iv == 0.15  # hodnoty zase z TWS modelu
