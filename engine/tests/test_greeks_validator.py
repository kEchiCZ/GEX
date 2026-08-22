"""Testy greeks validátoru (#614 finále): prahy 2× p95, hystereze, min. vzorek."""

import time

from test_crosscheck import TS

from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.ibkr.scheduler import CachedQuote, QuoteSnapshot
from gexlens_engine.tasty.greeks_validator import GreeksValidator, is_suspicious
from gexlens_engine.tasty.monitor import compare_minute
from gexlens_engine.tasty.provider import TastyChainCache
from gexlens_engine.tasty.symbols import ChainSymbols


def test_is_suspicious_prahy_per_symbol() -> None:
    """ES práh Δdelta 0,04: 0,01 je norma, 0,16 (zamrzlé greeks) podezřelé."""
    base = {"gamma_ibkr": 0.001, "gamma_tasty": 0.001, "iv_ibkr": 0.12, "iv_tasty": 0.12}
    assert is_suspicious("ES", delta_ibkr=0.42, delta_tasty=0.43, **base) is False
    assert is_suspicious("ES", delta_ibkr=0.42, delta_tasty=0.58, **base) is True
    # NQ má volnější deltu (0,10) — táž odchylka 0,08 na NQ projde
    assert is_suspicious("NQ", delta_ibkr=0.42, delta_tasty=0.50, **base) is False


def test_is_suspicious_nekompletni_par_se_nevyhodnocuje() -> None:
    """Díra tasty greeks (#810) nesmí vypadat jako neshoda modelů."""
    assert (
        is_suspicious(
            "ES",
            delta_ibkr=0.42,
            delta_tasty=None,
            gamma_ibkr=0.001,
            gamma_tasty=0.001,
            iv_ibkr=0.12,
            iv_tasty=0.12,
        )
        is None
    )


def test_validator_hystereze_a_cooldown() -> None:
    """Alert až po 3 minutách nad prahem; pak cooldown, žádný spam."""
    validator = GreeksValidator(share_threshold=0.20, minutes_threshold=3, cooldown_minutes=15)
    dirty = ({"ES": 100}, {"ES": 48})

    fired = [bool(validator.observe(*dirty)) for _ in range(20)]

    assert fired.index(True) == 2  # třetí minuta
    assert sum(fired) == 2  # druhý až po cooldownu (minuta 18)
    assert validator.last_shares["ES"] == 0.48


def test_validator_maly_vzorek_nevyhodnocuje_ani_nenuluje() -> None:
    """Víkendová minuta (pár kontraktů) sérii nesmí resetovat."""
    validator = GreeksValidator(share_threshold=0.20, minutes_threshold=3)
    validator.observe({"ES": 100}, {"ES": 50})
    validator.observe({"ES": 100}, {"ES": 50})
    validator.observe({"ES": 5}, {"ES": 5})  # pod MIN_PAIRS — přeskočí se

    assert validator.status_fields() == {}  # malý vzorek podíl neukazuje
    assert bool(validator.observe({"ES": 100}, {"ES": 50})) is True  # 3. platná minuta


def test_compare_minute_pocita_greeks_pary() -> None:
    """Kompletní pár nad prahem → suspicious; kvóty bez greeks se nepočítají."""
    now_mono = time.monotonic()
    spec = OptionContractSpec(
        symbol="ES",
        sec_type="FOP",
        expiry="20260813",
        strike=7775.0,
        right="C",
        exchange="CME",
        trading_class="E2D",
        multiplier="50",
    )
    snapshot = QuoteSnapshot(
        bid=18.0,
        ask=18.5,
        last=18.25,
        volume=120.0,
        iv=0.125,
        delta=0.52,
        gamma=0.0112,
        theta=-13.0,
        vega=1.1,
    )
    streamer = "./E2DQ26C7775:XCME"
    chain = ChainSymbols(
        product="ES", day=TS.date(), by_contract={("20260813", 7775.0, "C"): streamer}
    )
    quotes = {spec: CachedQuote(snapshot=snapshot, updated_at=now_mono, stale=False)}
    cache = TastyChainCache(clock=lambda: TS)
    # Tasty greeks zamrzlé jinde: delta 0,52 vs. 0,70 → nad ES prahem 0,04
    cache.on_event("Greeks", [streamer, 0.13, 0.70, 0.0111, -13.0, 1.1, 18.2])

    result = compare_minute(TS, quotes, cache, {"ES": chain}, now_monotonic=now_mono, now_utc=TS)

    assert result.greeks_checked == {"ES": 1}
    assert result.greeks_suspicious == {"ES": 1}
