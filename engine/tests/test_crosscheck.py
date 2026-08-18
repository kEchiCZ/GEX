"""Křížová kontrola feedů (#517 fáze A) — akceptační kritéria nad mockem.

Zásadní test je `test_sweep_rotace_nedela_falesny_poplach`: rotační artefakt
(každou třetí minutu ~58 % kontraktů „IBKR mrtvé") je NORMÁLNÍ provoz, ne
porucha. Naivní práh na okamžitém podílu by alertoval 24/7 — přesně proto má
detektor podmínku M minut v řadě.
"""

import datetime as dt
import time

import pytest

from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.ibkr.scheduler import CachedQuote, QuoteSnapshot
from gexlens_engine.tasty.crosscheck import CrossCheckDetector, MinuteTally
from gexlens_engine.tasty.mock import feed_greeks, feed_quote
from gexlens_engine.tasty.monitor import compare_minute
from gexlens_engine.tasty.provider import TastyChainCache
from gexlens_engine.tasty.symbols import ChainSymbols

TS = dt.datetime(2026, 8, 13, 14, 0, tzinfo=dt.UTC)


def tally(
    *, ibkr_dead: int = 0, tasty_dead: int = 0, both_dead: int = 0, ok: int = 0
) -> MinuteTally:
    return MinuteTally(
        contracts=ibkr_dead + tasty_dead + both_dead + ok,
        both_fresh=ok,
        ibkr_only_dead=ibkr_dead,
        tasty_only_dead=tasty_dead,
        both_dead=both_dead,
    )


def test_ibkr_mrtve_tasty_cerstve_spusti_alert() -> None:
    """AC1: „IBKR stale + tasty fresh" po M minutách → alert."""
    detector = CrossCheckDetector(share_threshold=0.7, minutes_threshold=3)
    dead = tally(ibkr_dead=90, ok=10)

    first = detector.observe(dead)
    second = detector.observe(dead)
    assert (first.alert, second.alert) == (False, False)
    assert first.state == "ok"  # série ještě neuzrála

    third = detector.observe(dead)
    assert third.alert is True
    assert third.state == "ibkr_suspect"
    assert third.streak == 3
    assert "IBKR mlčí na 90 %" in third.message


def test_oba_zdroje_mrtve_je_ticho() -> None:
    """AC2: tichý trh se nealertuje — hlavní zdroj falešných poplachů."""
    detector = CrossCheckDetector(share_threshold=0.7, minutes_threshold=3)
    quiet = tally(both_dead=95, ok=5)
    verdicts = [detector.observe(quiet) for _ in range(10)]

    assert all(v.state == "quiet" for v in verdicts)
    assert not any(v.alert for v in verdicts)


def test_tasty_mrtve_hlasi_tlumene_bez_alertu() -> None:
    """AC3: sekundární zdroj se hlásí TLUMENĚ — stav ano, alert kanál ne.

    Přehrání 3 051 minut historie dalo 41 epizod, všechny v hodinách 21–06 UTC:
    dxFeed je event-driven (v klidu neposílá nic), IBKR sweep poll-driven (vrací
    poslední kotaci pořád). V noci a v denní pauze CME je to normální stav, do
    alert kanálu by šlo 41 planých poplachů.
    """
    detector = CrossCheckDetector(share_threshold=0.7, minutes_threshold=3)
    dead = tally(tasty_dead=80, ok=20)
    verdicts = [detector.observe(dead) for _ in range(30)]

    assert verdicts[2].state == "tasty_suspect"
    assert "tastytrade mlčí" in verdicts[2].message
    assert not any(v.alert for v in verdicts)  # ani jeden alert za 30 minut


def test_sweep_rotace_nedela_falesny_poplach() -> None:
    """AC4: naměřený vzor provozu (#517 baseline) nesmí alertovat ani jednou.

    Reálná sekvence z 14. 8. 15:00–15:39 UTC: každou třetí minutu vyskočí podíl
    „IBKR mrtvé" na ~58 %, mezi tím 0–11 %. 3 016 minut čisté historie, nejdelší
    série nad 70 % byly 2 minuty — tři v řadě nikdy.
    """
    detector = CrossCheckDetector(share_threshold=0.7, minutes_threshold=3)
    mereny_vzor = [0.580, 0.000, 0.000, 0.582, 0.089, 0.089, 0.576, 0.082, 0.082, 0.580]

    alerts = []
    for _ in range(20):  # dvacet cyklů rotace = 200 minut provozu
        for share in mereny_vzor:
            dead = int(round(share * 100))
            verdict = detector.observe(tally(ibkr_dead=dead, ok=100 - dead))
            if verdict.alert:
                alerts.append(verdict)

    assert alerts == []


def test_malo_kontraktu_nedava_vyrok() -> None:
    """Přestavba pipeline: pár kontraktů → `insufficient`, série se nuluje."""
    detector = CrossCheckDetector(share_threshold=0.7, minutes_threshold=3, min_contracts=20)
    detector.observe(tally(ibkr_dead=90, ok=10))
    detector.observe(tally(ibkr_dead=90, ok=10))
    thin = detector.observe(tally(ibkr_dead=5))  # jen 5 kontraktů

    assert thin.state == "insufficient"
    assert thin.alert is False
    # Série začíná znovu — dvě minuty před přestavbou se nesčítají s dalšími
    assert detector.observe(tally(ibkr_dead=90, ok=10)).alert is False


def test_cooldown_neopakuje_tyz_alert_kazdou_minutu() -> None:
    """Výpadek farmy trvá desítky minut; alert kanál dostane jeden, ne třicet."""
    detector = CrossCheckDetector(share_threshold=0.7, minutes_threshold=3, cooldown_minutes=15)
    dead = tally(ibkr_dead=90, ok=10)

    alerts = [detector.observe(dead) for _ in range(20)]
    fired = [i for i, v in enumerate(alerts) if v.alert]

    assert fired == [2, 17]  # první na třetí minutě, další až po cooldownu


def test_uzdraveni_rearmuje_alert() -> None:
    """Po čisté sérii se další výpadek ohlásí hned, ne až po cooldownu."""
    detector = CrossCheckDetector(share_threshold=0.7, minutes_threshold=3, cooldown_minutes=15)
    dead = tally(ibkr_dead=90, ok=10)
    healthy = tally(ok=100)

    for _ in range(3):
        detector.observe(dead)
    for _ in range(3):  # tři čisté minuty = re-arm
        detector.observe(healthy)

    for _ in range(2):
        detector.observe(dead)
    assert detector.observe(dead).alert is True


def test_tally_pocita_i_kontrakty_s_obema_stranami_mrtvymi() -> None:
    """Kategorie „obojí mrtvé" nejde dopočítat z feed_comparison — řádky
    takových kontraktů se nezapisují. Musí ji spočítat compare_minute."""
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
    chain = ChainSymbols(
        product="ES", day=TS.date(), by_contract={("20260813", 7775.0, "C"): "./E2DQ26C7775:XCME"}
    )
    # Obě strany mrtvé: IBKR stale flag + tasty cache bez jediného eventu
    stale = {spec: CachedQuote(snapshot=snapshot, updated_at=now_mono - 5.0, stale=True)}
    empty = TastyChainCache(clock=lambda: TS)

    result = compare_minute(TS, stale, empty, {"ES": chain}, now_monotonic=now_mono, now_utc=TS)

    assert result.rows == []  # nulová informace se nezapisuje
    assert result.tally.contracts == 1
    assert result.tally.both_dead == 1
    assert result.tally.ibkr_only_dead == 0


def test_tally_oznaci_zivou_tasty_pri_mrtvem_ibkr() -> None:
    """Situace, kvůli které fáze A vznikla: IBKR mlčí, tasty teče."""
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
    chain = ChainSymbols(
        product="ES", day=TS.date(), by_contract={("20260813", 7775.0, "C"): "./E2DQ26C7775:XCME"}
    )
    stale = {spec: CachedQuote(snapshot=snapshot, updated_at=now_mono - 5.0, stale=True)}
    cache = TastyChainCache(clock=lambda: TS - dt.timedelta(seconds=3))
    feed_quote(cache, "./E2DQ26C7775:XCME", 18.1, 18.6)
    feed_greeks(cache, "./E2DQ26C7775:XCME", 0.127, 0.53, 0.0113)

    result = compare_minute(TS, stale, cache, {"ES": chain}, now_monotonic=now_mono, now_utc=TS)

    assert result.tally.ibkr_only_dead == 1
    assert result.tally.ibkr_dead_share == 1.0


@pytest.mark.parametrize("share", [0.0, 0.5, 1.0])
def test_podily_jsou_bezpecne_pri_nule_kontraktu(share: float) -> None:
    empty = MinuteTally()
    assert empty.ibkr_dead_share == 0.0
    assert empty.tasty_dead_share == 0.0
    assert empty.both_dead_share == 0.0


def test_vypnuty_zapis_nemeni_tally_ani_o_kontrakt() -> None:
    """#763: konec měření nesmí změnit chování detektoru ani fallbacků.

    Tally je jediné, co z tohohle průchodu jde do produkce — verdikt detektoru
    přes něj spouští fallback řetězu i návrat zpět (#614 fáze 2b). Kdyby se
    s vypnutým zápisem lišil byť o jeden kontrakt, znamenalo by to, že se
    aplikace po dojetí shadow začne rozhodovat jinak. Tenhle test je proto
    tvrdá podmínka celého rozdělení flagu.
    """
    now_mono = time.monotonic()
    specs = [
        OptionContractSpec(
            symbol="ES",
            sec_type="FOP",
            expiry="20260813",
            strike=strike,
            right=right,
            exchange="CME",
            trading_class="E2D",
            multiplier="50",
        )
        for strike in (7770.0, 7775.0, 7780.0)
        for right in ("C", "P")
    ]
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
    chain = ChainSymbols(
        product="ES",
        day=TS.date(),
        by_contract={
            ("20260813", spec.strike, spec.right): f"./E2DQ26{spec.right}{spec.strike:g}:XCME"
            for spec in specs
        },
    )
    # Míchaný stav: část kontraktů živá, část stale — ať tally není triviální
    quotes = {
        spec: CachedQuote(snapshot=snapshot, updated_at=now_mono - 5.0, stale=index % 2 == 0)
        for index, spec in enumerate(specs)
    }
    cache = TastyChainCache(clock=lambda: TS)
    for spec in specs[:4]:
        streamer = chain.streamer_symbol(spec)
        assert streamer is not None
        cache.on_event("Quote", [streamer, 18.0, 18.6, 3, 4])
        cache.on_event("Greeks", [streamer, 0.126, 0.53, 0.0113, -13.1, 1.2, 18.3])
    oi = {("ES", "20260813", spec.strike, spec.right): 1000.0 for spec in specs}

    se_zapisem = compare_minute(
        TS, quotes, cache, {"ES": chain}, now_monotonic=now_mono, now_utc=TS, oi_ibkr=oi
    )
    bez_zapisu = compare_minute(
        TS,
        quotes,
        cache,
        {"ES": chain},
        now_monotonic=now_mono,
        now_utc=TS,
        oi_ibkr=oi,
        collect_rows=False,
    )

    assert bez_zapisu.tally == se_zapisem.tally
    assert se_zapisem.rows  # kontrola, že test měří na neprázdném vzorku
    assert bez_zapisu.rows == []
