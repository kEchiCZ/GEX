"""Testy SubscriptionScheduleru (issue #7): dávky, repair fronta, stale, priorita ATM/křídla."""

from gexlens_engine.config import Settings
from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.ibkr.mock import MockQuoteStreamer
from gexlens_engine.ibkr.scheduler import QuoteSnapshot, SubscriptionScheduler

SPOT = 7600.0


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

    metrics = await scheduler.sweep(contracts, SPOT)

    assert streamer.max_concurrent <= 80  # dávka nikdy nepřekročí batch_size
    assert metrics.lines_utilization == 80 / 100


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
    scheduler = SubscriptionScheduler(streamer, settings)

    metrics = await scheduler.sweep(contracts, SPOT)

    assert metrics.repair_count == 4
    assert metrics.stale_count == 4
    assert metrics.greeks_complete == 356
    assert scheduler.stale_contracts == dead
    # 1 pokus v dávce + 3 repair pokusy na každý mrtvý kontrakt
    dead_calls = [spec for spec in streamer.fetch_calls if spec in dead]
    assert len(dead_calls) == 4 * (1 + 3)


async def test_stale_flag_clears_after_recovery() -> None:
    contracts = chain_360()
    # 2 ATM kontrakty (sweepují se každý cyklus) selžou v celém prvním sweepu
    # (1 + 3 repair pokusy), pak se zotaví
    atm = [spec for spec in contracts if spec.strike == SPOT]
    assert len(atm) == 2
    flaky = {spec: 4 for spec in atm}
    streamer = MockQuoteStreamer(fail_first=flaky)
    scheduler = SubscriptionScheduler(streamer, Settings())

    first = await scheduler.sweep(contracts, SPOT)
    assert first.stale_count == 2

    second = await scheduler.sweep(contracts, SPOT)
    assert second.stale_count == 0
    assert scheduler.stale_contracts == set()
    cached = scheduler.quote(atm[0])
    assert cached is not None
    assert not cached.stale


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
        ) -> QuoteSnapshot | None:
            if spec is contracts[0]:
                raise RuntimeError("mock: pacing violation")
            return await super().fetch_quote(spec, timeout_s)

    streamer = ExplodingStreamer()
    scheduler = SubscriptionScheduler(streamer, Settings())

    metrics = await scheduler.sweep(contracts, SPOT)

    # Výjimka streamu = nekompletní kontrakt, sweep běží dál (SPEC kap. 8: odolnost)
    assert metrics.greeks_complete == 7
    assert metrics.stale_count == 1
