"""Fallback celého opčního řetězu na tastytrade (#614 fáze 2b).

Fáze 2a přepnula jen cenu podkladu, takže při souběhu s mobilem (error 10197)
běžela cena, ale heatmapa i GEX zůstaly zmrzlé — ty stojí na řetězu. Tady se
testuje, že se při výpadku IBKR převezme i řetěz, že se vrátí zpět a že se
nikdy nemíchají hodnoty ze dvou zdrojů (ADR-0025 pravidlo 2).
"""

import datetime as dt

from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.ibkr.scheduler import FEED_TASTY
from gexlens_engine.tasty.chain_fallback import (
    ChainFallback,
    tasty_chain_quotes,
)
from gexlens_engine.tasty.crosscheck import CrossCheckVerdict, MinuteTally
from gexlens_engine.tasty.provider import TastyChainCache
from gexlens_engine.tasty.symbols import ChainSymbols

NOW = dt.datetime(2026, 8, 18, 14, 30, tzinfo=dt.UTC)


def verdict(state: str, *, streak: int = 0) -> CrossCheckVerdict:
    return CrossCheckVerdict(
        state=state,  # type: ignore[arg-type]
        tally=MinuteTally(contracts=200),
        streak=streak,
        alert=False,
        message="test",
    )


def source_of(fallback: ChainFallback) -> str:
    """Aktivní zdroj jako prostý `str` — jinak si mypy po prvním porovnání
    zúží typ na jediný `Literal` a další porovnání označí za nemožné."""
    return fallback.active_source


def spec(strike: float, right: str = "C") -> OptionContractSpec:
    return OptionContractSpec(
        symbol="ES",
        sec_type="FOP",
        expiry="20260818",
        strike=strike,
        right=right,
        exchange="CME",
        trading_class="E1A",
        multiplier="50",
    )


def chain_for(specs: list[OptionContractSpec]) -> ChainSymbols:
    return ChainSymbols(
        product="ES",
        day=NOW.date(),
        by_contract={
            (s.expiry, s.strike, s.right): f"./E1AQ26{s.right}{s.strike:g}:XCME" for s in specs
        },
    )


class FrozenClock:
    """Posunovatelné hodiny cache — eventy musí jít „orazítkovat" různým časem,
    aby šlo testovat stáří hodnot."""

    def __init__(self, at: dt.datetime) -> None:
        self.at = at

    def __call__(self) -> dt.datetime:
        return self.at


def fill_cache(
    cache: TastyChainCache,
    clock: FrozenClock,
    chain: ChainSymbols,
    specs: list[OptionContractSpec],
    *,
    quote_at: dt.datetime = NOW,
    greeks_at: dt.datetime | None = None,
) -> None:
    """Naplní cache tak, jak by ji naplnil DXLink stream."""
    for s in specs:
        streamer = chain.streamer_symbol(s)
        assert streamer is not None
        clock.at = quote_at
        cache.on_event("Quote", [streamer, 10.0, 10.5, 3, 4])
        clock.at = greeks_at or quote_at
        cache.on_event("Greeks", [streamer, 0.18, 0.42, 0.0031, -1.2, 0.9, 10.2])


# ── rozhodování o zdroji ────────────────────────────────────────────────────


def test_pri_zdravem_ibkr_zustava_ibkr() -> None:
    fallback = ChainFallback()

    decision = fallback.observe(verdict("ok"))

    assert decision.source == "ibkr"
    assert decision.switched is False


def test_ibkr_suspect_prepne_retez_na_tasty() -> None:
    """Jádro fáze 2b: detektor #517 A říká „mlčí jen IBKR" → řetěz z tasty."""
    fallback = ChainFallback()

    decision = fallback.observe(verdict("ibkr_suspect", streak=3))

    assert decision.source == "tasty"
    assert decision.switched is True
    assert "tastytrade" in decision.message


def test_prepnuti_se_hlasi_jen_jednou() -> None:
    """`switched` je hrana — jinak by alert chodil každou minutu výpadku."""
    fallback = ChainFallback()

    first = fallback.observe(verdict("ibkr_suspect", streak=3))
    second = fallback.observe(verdict("ibkr_suspect", streak=4))

    assert first.switched is True
    assert second.switched is False
    assert second.source == "tasty"


def test_navrat_az_po_cele_ciste_serii() -> None:
    fallback = ChainFallback(recover_minutes=5)
    fallback.observe(verdict("ibkr_suspect", streak=3))

    for _ in range(4):
        assert fallback.observe(verdict("ok")).source == "tasty"

    back = fallback.observe(verdict("ok"))
    assert back.source == "ibkr"
    assert back.switched is True


def test_minuta_nad_prahem_serii_navratu_nuluje() -> None:
    """`state == "ok"` sám nestačí.

    Detektor vrací „ok" i pro minutu NAD prahem, která zatím nenaplnila sérii
    do alertu (`streak > 0`). Návrat na takové minutě by řetěz přepnul přesně
    ve chvíli, kdy se IBKR začíná kazit znovu.
    """
    fallback = ChainFallback(recover_minutes=3)
    fallback.observe(verdict("ibkr_suspect", streak=3))

    fallback.observe(verdict("ok"))
    fallback.observe(verdict("ok"))
    fallback.observe(verdict("ok", streak=1))  # zase nad prahem → reset
    fallback.observe(verdict("ok"))
    fallback.observe(verdict("ok"))

    assert source_of(fallback) == "tasty"
    assert fallback.observe(verdict("ok")).switched is True


def test_tichy_trh_nevypada_jako_uzdraveni() -> None:
    """Když mlčí oba zdroje, o zdraví IBKR to neříká nic — fallback drží."""
    fallback = ChainFallback(recover_minutes=2)
    fallback.observe(verdict("ibkr_suspect", streak=3))

    for _ in range(10):
        assert fallback.observe(verdict("quiet")).source == "tasty"

    assert source_of(fallback) == "tasty"


def test_malo_kontraktu_navrat_nespusti() -> None:
    """Přestavba pipeline (`insufficient`) není důkaz, že se IBKR zotavil."""
    fallback = ChainFallback(recover_minutes=2)
    fallback.observe(verdict("ibkr_suspect", streak=3))

    fallback.observe(verdict("ok"))
    fallback.observe(verdict("insufficient"))
    fallback.observe(verdict("ok"))

    assert source_of(fallback) == "tasty"


# ── skládání kotací z tasty ─────────────────────────────────────────────────


def test_retez_se_posklada_z_tasty_stavu() -> None:
    specs = [spec(5900.0), spec(5950.0, "P")]
    chain = chain_for(specs)
    clock = FrozenClock(NOW)
    cache = TastyChainCache(clock)
    fill_cache(cache, clock, chain, specs)

    quotes = tasty_chain_quotes(
        specs, chain, cache, now_utc_ts=NOW.timestamp(), now_monotonic=1000.0
    )

    assert set(quotes) == set(specs)
    snapshot = quotes[specs[0]].snapshot
    assert snapshot.bid == 10.0
    assert snapshot.gamma == 0.0031
    assert quotes[specs[0]].feed == FEED_TASTY


def test_objem_a_posledni_cena_zustavaji_nezmerene() -> None:
    """Tasty je v sémantice IBKR nedodá — nula by lhala (#465, ADR-0025 pravidlo 2)."""
    specs = [spec(5900.0)]
    chain = chain_for(specs)
    clock = FrozenClock(NOW)
    cache = TastyChainCache(clock)
    fill_cache(cache, clock, chain, specs)

    quotes = tasty_chain_quotes(
        specs, chain, cache, now_utc_ts=NOW.timestamp(), now_monotonic=1000.0
    )

    assert quotes[specs[0]].snapshot.volume is None
    assert quotes[specs[0]].snapshot.last is None


def test_kontrakt_bez_greeks_se_vynecha_cely() -> None:
    """Bez mergování: kotaci z tasty a greeks odjinud dohromady nikdy."""
    specs = [spec(5900.0)]
    chain = chain_for(specs)
    cache = TastyChainCache(FrozenClock(NOW))
    streamer = chain.streamer_symbol(specs[0])
    assert streamer is not None
    cache.on_event("Quote", [streamer, 10.0, 10.5, 3, 4])  # greeks nedorazily

    quotes = tasty_chain_quotes(
        specs, chain, cache, now_utc_ts=NOW.timestamp(), now_monotonic=1000.0
    )

    assert quotes == {}


def test_zastarala_hodnota_se_nepouzije() -> None:
    specs = [spec(5900.0)]
    chain = chain_for(specs)
    clock = FrozenClock(NOW)
    cache = TastyChainCache(clock)
    fill_cache(cache, clock, chain, specs, quote_at=NOW - dt.timedelta(minutes=5))

    quotes = tasty_chain_quotes(
        specs, chain, cache, now_utc_ts=NOW.timestamp(), now_monotonic=1000.0
    )

    assert quotes == {}


def test_stari_kotace_se_propise_do_updated_at() -> None:
    """Tvrdit u minutu staré hodnoty nulové stáří by obešlo ochranu #306."""
    specs = [spec(5900.0)]
    chain = chain_for(specs)
    clock = FrozenClock(NOW)
    cache = TastyChainCache(clock)
    fill_cache(cache, clock, chain, specs, quote_at=NOW - dt.timedelta(seconds=45))

    quotes = tasty_chain_quotes(
        specs, chain, cache, now_utc_ts=NOW.timestamp(), now_monotonic=1000.0
    )

    assert quotes[specs[0]].age_s(1000.0) == 45.0


def test_kontrakt_mimo_tasty_chain_se_preskoci() -> None:
    specs = [spec(5900.0), spec(9999.0)]
    chain = chain_for([specs[0]])  # druhý strike v mapě není
    clock = FrozenClock(NOW)
    cache = TastyChainCache(clock)
    fill_cache(cache, clock, chain, [specs[0]])

    quotes = tasty_chain_quotes(
        specs, chain, cache, now_utc_ts=NOW.timestamp(), now_monotonic=1000.0
    )

    assert set(quotes) == {specs[0]}


def test_bez_chain_mapy_se_nevrati_nic() -> None:
    """Než dorazí denní mapa symbolů, fallback nemá z čeho stavět."""
    assert (
        tasty_chain_quotes(
            [spec(5900.0)], None, TastyChainCache(), now_utc_ts=NOW.timestamp(), now_monotonic=1.0
        )
        == {}
    )
