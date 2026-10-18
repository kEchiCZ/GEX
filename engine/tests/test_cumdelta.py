"""Testy CumΔ (issue #17): golden bar větve a tisků dxFeed, reset, persistence."""

import datetime as dt
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from gexlens_engine.compute.cumdelta import (
    SOURCE_AGGRESSOR,
    SOURCE_MIDPOINT,
    BarBreakdown,
    CumDeltaTracker,
    FlowRow,
    flow_window_sum,
    midpoint_sign,
    stored_sources,
)
from gexlens_engine.config import Settings
from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.storage.parquet_store import SnapshotWriter

GOLDEN_PATH = Path(__file__).parent / "golden" / "cumdelta_basic.json"


def load_golden() -> dict[str, object]:
    data: dict[str, object] = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    return data


def spec(right: str, strike: float = 7600.0) -> OptionContractSpec:
    return OptionContractSpec(
        symbol="ES",
        sec_type="FOP",
        expiry="20260716",
        strike=strike,
        right=right,
        exchange="CME",
        trading_class="E3D",
        multiplier="50",
    )


def sample(tracker: CumDeltaTracker, *, running: bool, market_closed: bool = False) -> None:
    """Vzorek orchestrátoru: stav trade větve a v témž okamžiku řez tisků (#1460)."""
    tracker.observe(running=running, market_closed=market_closed)
    tracker.seal()


def start_measured(tracker: CumDeltaTracker) -> None:
    """Start za zavřeného trhu (hranice známá) s běžící trade větví → měřená minuta.

    Tisky před voláním patří řádku této minuty, tisky po něm dalšímu (řez ve vzorku).
    """
    sample(tracker, running=True, market_closed=True)
    tracker.begin_cycle()
    assert tracker.minute_measured


def next_cycle(tracker: CumDeltaTracker, *samples: bool) -> None:
    """Vzorky „běží trade větev" za otevřeného trhu a začátek dalšího cyklu."""
    for running in samples:
        sample(tracker, running=running)
    tracker.begin_cycle()


def test_restore_net_volume_pricita_k_zivemu_mereni() -> None:
    """#232, #1460: uložený základ se přičte k toku naměřenému po restartu, nic se nezahodí."""
    tracker = CumDeltaTracker(multiplier=50.0, source=SOURCE_MIDPOINT)
    tracker.add_bar(spec("C"), 100.0, 10.3, 10.0, 10.4, delta=0.5)  # první bar jen zakládá stav
    tracker.add_bar(spec("C"), 110.0, 10.3, 10.0, 10.4, delta=0.5)  # last nad midem → +10

    tracker.restore_net_volume({spec("C"): 999.0, spec("P"): -40.0})

    assert tracker.net_volume(spec("C")) == 1009.0  # základ + tok po restartu
    assert tracker.net_volume(spec("P")) == -40.0  # chybějící klíč se doplní
    assert tracker.net_volumes() == {spec("C"): 1009.0, spec("P"): -40.0}


def test_restore_net_volume_zachova_tisky_pred_prvnim_cyklem() -> None:
    """#1460: tisky zdroje aggressor přijdou dřív než seed v prvním cyklu — základ je doplní."""
    tracker = CumDeltaTracker(multiplier=50.0, source=SOURCE_AGGRESSOR)
    tracker.add_dx_trade(spec("C"), 3.0, "BUY", 0.5)
    tracker.add_dx_trade(spec("P"), 2.0, "SELL", -0.5)
    sample(tracker, running=True)  # vzorek před cyklem přesune tisky do denního netu

    tracker.restore_net_volume({spec("C"): 20.0, spec("P"): 5.0})

    assert tracker.net_volumes() == {spec("C"): 23.0, spec("P"): 3.0}


def test_stored_sources_rozlisi_zdroj_radku() -> None:
    """#1460: NULL (partice před sloupcem) = midpoint; hybrid `dxfeed` nepatří nikomu."""
    assert stored_sources(SOURCE_MIDPOINT) == frozenset({None, SOURCE_MIDPOINT})
    assert stored_sources(SOURCE_AGGRESSOR) == frozenset({SOURCE_AGGRESSOR})
    assert "dxfeed" not in stored_sources(SOURCE_MIDPOINT) | stored_sources(SOURCE_AGGRESSOR)
    with pytest.raises(ValueError, match="Neznámý zdroj"):
        stored_sources("dxfeed")


def test_golden_bar_branch_midpoint_test() -> None:
    """AC: bar-based větev (ΔVol × midpoint test) testována zvlášť."""
    golden = load_golden()
    branch = golden["bar_branch"]
    assert isinstance(branch, dict)
    tracker = CumDeltaTracker(
        multiplier=float(golden["multiplier"]),  # type: ignore[arg-type]
        source=SOURCE_MIDPOINT,
    )
    contract = spec("C")

    for bar in branch["bars"]:
        flow = tracker.add_bar(
            contract,
            cumulative_volume=bar["volume"],
            last=bar["last"],
            bid=bar["bid"],
            ask=bar["ask"],
            delta=float(branch["delta"]),
        )
        assert flow == pytest.approx(bar["expected_flow"])

    assert tracker.cum_delta == pytest.approx(branch["expected_cum_delta"])


def test_aggressor_bar_neda_tok_ani_net() -> None:
    """ADR-0057 bod 1–2: pod tisky bar jen rozkládá objem; kontrakt bez tisku je mimo tok."""
    tracker = CumDeltaTracker(multiplier=50.0, source=SOURCE_AGGRESSOR)
    tracker.add_dx_trade(spec("C"), size=2.0, aggressor="BUY", delta=0.5)  # +50
    start_measured(tracker)
    contract = spec("P")
    assert tracker.add_bar(contract, 100.0, last=10.0, bid=10.0, ask=10.4, delta=-0.3) == 0.0
    # put bez tisku: midpoint by dal −1 × 20 × (−0.3) × 50 = +300, pod tisky nic
    assert tracker.add_bar(contract, 120.0, last=10.0, bid=10.0, ask=10.4, delta=-0.3) == 0.0
    assert tracker.cum_delta == pytest.approx(50.0)
    assert tracker.net_volumes() == {spec("C"): 2.0}
    breakdown = tracker.take_breakdowns()[contract]
    assert (breakdown.printed, breakdown.structured) == (0.0, 20.0)
    assert tracker.day_stats()["fallback_volume"] == 20.0  # objem bez tisku se dál měří


def test_midpoint_sign() -> None:
    assert midpoint_sign(10.3, 10.0, 10.4) == 1
    assert midpoint_sign(10.1, 10.0, 10.4) == -1
    assert midpoint_sign(10.2, 10.0, 10.4) == 0  # přesně na midu → bez klasifikace


def test_daily_reset_clears_state() -> None:
    tracker = CumDeltaTracker(multiplier=50.0, source=SOURCE_MIDPOINT)
    contract = spec("C")
    tracker.add_bar(contract, 100.0, 10.3, 10.0, 10.4, delta=0.5)
    tracker.add_bar(contract, 130.0, 10.3, 10.0, 10.4, delta=0.5)
    assert tracker.cum_delta != 0.0

    tracker.reset()

    assert tracker.cum_delta == 0.0
    # Po resetu je první bar zase „první" — nezapočítá starý objem přes noc
    flow = tracker.add_bar(contract, 30.0, 10.3, 10.0, 10.4, delta=0.5)
    assert flow == 0.0


def test_net_volume_per_contract() -> None:  # ADR-0011, #222
    """Čistý klasifikovaný objem (buy − sell, kontrakty) per spec, bar větev i tisky."""
    tracker = CumDeltaTracker(multiplier=50.0, source=SOURCE_MIDPOINT)
    call = spec("C")
    # Bar větev: last nad midem (+30), pod midem (−20); první bar jen zakládá stav
    tracker.add_bar(call, 100.0, 10.3, 10.0, 10.4, delta=0.5)
    tracker.add_bar(call, 130.0, 10.3, 10.0, 10.4, delta=0.5)  # +30
    tracker.add_bar(call, 150.0, 10.0, 10.0, 10.4, delta=0.5)  # last pod midem → −20
    assert tracker.net_volume(call) == 10.0
    assert tracker.net_volume(spec("C", strike=9999.0)) == 0.0  # neznámý kontrakt
    tracker.reset()
    assert tracker.net_volume(call) == 0.0

    # Tisky dxFeed (zdroj aggressor): buy 5, sell 2, tisk bez strany nepřispívá
    prints = CumDeltaTracker(multiplier=50.0, source=SOURCE_AGGRESSOR)
    put = spec("P")
    prints.add_dx_trade(put, size=5.0, aggressor="BUY", delta=-0.4)
    prints.add_dx_trade(put, size=2.0, aggressor="SELL", delta=-0.4)
    prints.add_dx_trade(put, size=9.0, aggressor="UNDEFINED", delta=-0.4)
    assert prints.net_volume(put) == 0.0  # do denního až řezem ve vzorku (#1460)
    prints.seal()
    assert prints.net_volume(put) == 3.0
    prints.reset()
    assert prints.net_volume(put) == 0.0


def test_volume_decrease_is_ignored_not_negative() -> None:
    tracker = CumDeltaTracker(multiplier=50.0, source=SOURCE_MIDPOINT)
    contract = spec("C")
    tracker.add_bar(contract, 100.0, 10.3, 10.0, 10.4, delta=0.5)
    flow = tracker.add_bar(contract, 80.0, 10.3, 10.0, 10.4, delta=0.5)
    assert flow == 0.0
    assert tracker.cum_delta == 0.0


def test_close_minute_series_and_persistence(tmp_path: Path) -> None:
    """SPEC 4.5/5.1: minutová řada flowΔ/CumΔ se ukládá do derived/{sym}/flow/."""
    tracker = CumDeltaTracker(multiplier=50.0, source=SOURCE_MIDPOINT)
    tracker.add_bar(spec("C"), 100.0, 10.3, 10.0, 10.4, delta=0.5)  # první bar jen zakládá stav
    tracker.add_bar(spec("C"), 102.0, 10.3, 10.0, 10.4, delta=0.5)  # +50
    row_1 = tracker.close_minute(dt.datetime(2026, 7, 16, 15, 0, tzinfo=dt.UTC))
    tracker.add_bar(spec("C"), 103.0, 10.0, 10.0, 10.4, delta=0.5)  # last pod midem → -25
    row_2 = tracker.close_minute(dt.datetime(2026, 7, 16, 15, 1, tzinfo=dt.UTC))

    assert row_1 == FlowRow(
        dt.datetime(2026, 7, 16, 15, 0, tzinfo=dt.UTC), 50.0, 50.0, source="midpoint"
    )
    assert row_2.flow_delta == pytest.approx(-25.0)
    assert row_2.cum_delta == pytest.approx(25.0)

    writer = SnapshotWriter(Settings(data_dir=tmp_path))
    path = writer.write_flow("ES", dt.date(2026, 7, 16), [row_1, row_2])

    assert path == tmp_path / "derived" / "ES" / "flow" / "2026-07-16.parquet"
    frame = pd.read_parquet(path)
    # CVD podkladu (#829) je součástí schématu; bez tasty větve zůstává NULL
    assert list(frame.columns) == [
        "ts_min",
        "flow_delta",
        "cum_delta",
        "futures_cvd_delta",
        "futures_cvd",
        "source",
        # Pokrytí trade větví za minutu (#1071)
        "printed_volume",
        "unknown_volume",
        "structured_volume",
        "fallback_volume",
        "dropped_no_delta",
    ]
    assert list(frame["cum_delta"]) == [50.0, 25.0]
    assert frame["futures_cvd"].isna().all()
    assert list(frame["source"]) == ["midpoint", "midpoint"]  # ADR-0032: zdroj znaménka v řadě


def test_roll_session_resets_only_on_boundary() -> None:
    """#638: první volání jen fixuje seanci; reset až při přechodu na nový den."""
    tracker = CumDeltaTracker(multiplier=50.0, source=SOURCE_MIDPOINT)
    tracker.add_bar(spec("C"), 100.0, 10.3, 10.0, 10.4, delta=0.5)  # první bar jen zakládá stav
    tracker.add_bar(spec("C"), 102.0, 10.3, 10.0, 10.4, delta=0.5)  # +50
    assert tracker.roll_session(dt.date(2026, 7, 20)) is False  # start procesu, bez resetu
    assert tracker.cum_delta == pytest.approx(50.0)
    assert tracker.roll_session(dt.date(2026, 7, 20)) is False  # táž seance
    assert tracker.cum_delta == pytest.approx(50.0)
    assert tracker.roll_session(dt.date(2026, 7, 21)) is True  # hranice → reset
    assert tracker.cum_delta == 0.0
    assert tracker.net_volumes() == {}


def test_restore_cum_navazuje_zaklad_po_restartu() -> None:
    """#638: seed z flow partice přičítá základ, tok po restartu se neztrácí."""
    tracker = CumDeltaTracker(multiplier=50.0, source=SOURCE_MIDPOINT)
    tracker.add_bar(spec("C"), 100.0, 10.3, 10.0, 10.4, delta=0.5)  # první bar jen zakládá stav
    tracker.add_bar(spec("C"), 101.0, 10.3, 10.0, 10.4, delta=0.5)  # +25 po restartu
    tracker.restore_cum(1000.0)
    assert tracker.cum_delta == pytest.approx(1025.0)


# ── Trade větev z dxFeed (ADR-0032, ADR-0057) ─────────────────────────
DXFEED_GOLDEN_PATH = Path(__file__).parent / "golden" / "cumdelta_dxfeed.json"


def load_dxfeed_golden() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(DXFEED_GOLDEN_PATH.read_text(encoding="utf-8"))
    return data


def run_dxfeed_golden(source: str) -> tuple[CumDeltaTracker, list[float]]:
    """Minuty jako v runtime: tisky → vzorek s řezem → `begin_cycle` → bar → `close_minute`.

    `dx_active` bloku = běží trade větev: pod `midpoint` okamžitý příznak (v5),
    pod tisky vzorek verdiktu minuty (#1460). Engine startuje v minutě 1 za
    otevřeného trhu, takže ta je pod tisky mezera — nemá tisk ani přírůstek.
    """
    golden = load_dxfeed_golden()
    tracker = CumDeltaTracker(multiplier=float(golden["multiplier"]), source=source)
    running = bool(golden[source]["dx_active"])
    tracker.dx_active = running
    contract = spec("C")
    delta = float(golden["delta"])
    bar_flows: list[float] = []
    for bar in golden["bars"]:
        for item in golden["trades"]:
            if item["minute"] == bar["minute"]:
                flow = tracker.add_dx_trade(
                    contract, size=item["size"], aggressor=item["aggressor"], delta=delta
                )
                expected = item["expected_flow_aggressor"] if tracker.prints else 0.0
                assert flow == pytest.approx(expected)
        sample(tracker, running=running)
        tracker.begin_cycle()
        bar_flows.append(
            tracker.add_bar(
                contract,
                cumulative_volume=bar["volume"],
                last=bar["last"],
                bid=bar["bid"],
                ask=bar["ask"],
                delta=delta,
            )
        )
        tracker.close_minute(dt.datetime(2026, 9, 8, 14, bar["minute"], tzinfo=dt.UTC))
    return tracker, bar_flows


@pytest.mark.parametrize("source", [SOURCE_AGGRESSOR, SOURCE_MIDPOINT])
def test_golden_trade_vetev_aggressor_vs_midpoint(source: str) -> None:
    """Golden (AGENTS.md): tytéž tisky + bary, dva zdroje znaménka, CumΔ a net spočtené ručně."""
    golden = load_dxfeed_golden()
    expected = golden[source]
    assert isinstance(expected, dict)
    tracker, bar_flows = run_dxfeed_golden(source)

    assert bar_flows == pytest.approx(expected["expected_bar_flows"])
    assert tracker.cum_delta == pytest.approx(expected["expected_cum_delta"])
    assert tracker.net_volume(spec("C")) == pytest.approx(expected["expected_net_volume"])
    stats = tracker.day_stats()
    assert stats["source"] == source
    for key, value in expected["expected_coverage"].items():
        assert stats[key] == pytest.approx(value), key


@pytest.mark.parametrize(("source", "net"), [(SOURCE_AGGRESSOR, 5.0), (SOURCE_MIDPOINT, 0.0)])
def test_tisk_bez_delty_je_mimo_tok(source: str, net: float) -> None:
    """ADR-0057 bod 2: tisk bez delty jde mimo tok; pod tisky jeho strana nese čistý objem."""
    tracker = CumDeltaTracker(multiplier=50.0, source=source)
    contract = spec("C")
    assert tracker.add_dx_trade(contract, size=5.0, aggressor="BUY", delta=None) == 0.0
    assert tracker.add_dx_trade(contract, size=4.0, aggressor="UNDEFINED", delta=None) == 0.0
    tracker.seal()  # pod tisky jde net do denního až řezem; midpoint řez nezná
    assert tracker.cum_delta == 0.0
    assert tracker.net_volume(contract) == net
    stats = tracker.day_stats()
    assert stats["dropped_no_delta"] == 2
    assert stats["printed_volume"] == 0.0 and stats["unknown_volume"] == 0.0


def test_aggressor_net_volume_nese_stranu_od_burzy() -> None:
    """ADR-0011, ADR-0057: čistý objem per kontrakt jen z tisků (buy − sell); bar ho nemění."""
    tracker = CumDeltaTracker(multiplier=50.0, source=SOURCE_AGGRESSOR)
    contract = spec("P")
    tracker.add_bar(contract, 10.0, last=5.0, bid=5.0, ask=5.4, delta=-0.3)
    tracker.add_dx_trade(contract, size=4.0, aggressor="SELL", delta=-0.3)
    tracker.add_dx_trade(contract, size=1.0, aggressor="BUY", delta=-0.3)
    tracker.seal()
    tracker.add_bar(contract, 15.0, last=5.0, bid=5.0, ask=5.4, delta=-0.3)
    assert tracker.net_volume(contract) == pytest.approx(-3.0)


def test_close_minute_aggressor_nese_zdroj_a_tok_z_tisku() -> None:
    """ADR-0057 bod 8: řádek nese zdroj `aggressor`, tok minuty jen z tisků se stranou."""
    tracker = CumDeltaTracker(multiplier=50.0, source=SOURCE_AGGRESSOR)
    tracker.add_dx_trade(spec("C"), size=2.0, aggressor="BUY", delta=0.5)  # +50
    start_measured(tracker)
    ts = dt.datetime(2026, 10, 19, 14, 0, tzinfo=dt.UTC)
    assert tracker.close_minute(ts) == FlowRow(
        ts,
        50.0,
        50.0,
        source=SOURCE_AGGRESSOR,
        printed_volume=2.0,
        unknown_volume=0.0,
        structured_volume=0.0,
        fallback_volume=0.0,
        dropped_no_delta=0,
    )


@pytest.mark.parametrize("source", ["tick", "dxfeed"])
def test_tracker_odmitne_neznamy_zdroj(source: str) -> None:
    """Hybrid `dxfeed` zrušil ADR-0057 (bod 10), tick větev K1a."""
    with pytest.raises(ValueError):
        CumDeltaTracker(multiplier=50.0, source=source)


def test_reset_vynuluje_pokryti_i_tisky_od_baru() -> None:
    tracker = CumDeltaTracker(multiplier=50.0, source=SOURCE_AGGRESSOR)
    start_measured(tracker)  # reset seance verdikt minuty nemaže
    contract = spec("C")
    tracker.add_volume(contract, 100.0)
    tracker.add_dx_trade(contract, size=3.0, aggressor="BUY", delta=0.4)
    tracker.reset()
    assert tracker.day_stats()["printed_volume"] == 0.0
    assert tracker.net_volumes() == {}
    # po resetu první bar jen zakládá stav — tisky před resetem se nepřenesou
    assert tracker.add_volume(contract, 103.0) == 0.0
    assert tracker.add_volume(contract, 110.0) == 7.0
    assert tracker.take_breakdowns()[contract].printed == 0.0


# ── Rozklad přírůstku objemu (#1007 krok 2) ───────────────────────────
def test_breakdown_rozlozi_prirustek_na_tisky_a_strukturu() -> None:
    tracker = CumDeltaTracker(multiplier=50.0, source=SOURCE_MIDPOINT)
    tracker.dx_active = True
    contract = spec("C")
    tracker.add_bar(contract, 100.0, last=10.3, bid=10.0, ask=10.4, delta=0.4)
    tracker.add_dx_trade(contract, size=6.0, aggressor="BUY", delta=0.4)
    tracker.add_dx_trade(contract, size=1.0, aggressor="UNDEFINED", delta=0.4)
    tracker.add_bar(contract, 115.0, last=10.3, bid=10.0, ask=10.4, delta=0.4)

    breakdown = tracker.take_breakdowns()[contract]
    assert breakdown.volume_delta == 15.0
    assert breakdown.printed == 7.0  # tisky se stranou i bez
    assert breakdown.structured == 8.0  # zbytek bez tisku
    assert tracker.take_breakdowns() == {}  # odběr vyprázdní


def test_breakdown_bez_trade_vetve_je_null_ne_nula() -> None:
    """Tasty odpojené → printed/structured None; nula by tvrdila 100 % strukturu."""
    tracker = CumDeltaTracker(multiplier=50.0, source=SOURCE_MIDPOINT)
    contract = spec("P")
    tracker.add_bar(contract, 10.0, last=5.0, bid=5.0, ask=5.4, delta=-0.3)
    tracker.add_bar(contract, 14.0, last=5.0, bid=5.0, ask=5.4, delta=-0.3)
    breakdown = tracker.take_breakdowns()[contract]
    assert breakdown.volume_delta == 4.0
    assert breakdown.printed is None and breakdown.structured is None


def test_breakdown_jen_pri_prirustku() -> None:
    tracker = CumDeltaTracker(multiplier=50.0, source=SOURCE_MIDPOINT)
    tracker.dx_active = True
    contract = spec("C")
    tracker.add_bar(contract, 100.0, last=10.3, bid=10.0, ask=10.4, delta=0.4)
    tracker.add_bar(contract, 100.0, last=10.3, bid=10.0, ask=10.4, delta=0.4)
    assert tracker.take_breakdowns() == {}


# ── Pokrytí per minuta do řady flow (#1071) ───────────────────────────
def test_pokryti_se_meri_v_rezimu_midpoint_a_jde_do_flow_row_po_minutach() -> None:
    """#1071: v režimu midpoint se pokrytí měří stejně jako pod aggressor (co by trade
    větev pokryla) a FlowRow nese přírůstek za minutu, ne denní kumulativ."""
    tracker = CumDeltaTracker(multiplier=50.0, source=SOURCE_MIDPOINT)
    tracker.dx_active = True
    c = spec("C")
    tracker.add_bar(c, 100.0, last=10.3, bid=10.0, ask=10.4, delta=0.4)
    # minuta 1: 6 tisků se stranou + 1 bez strany, přírůstek baru 15 → struktura 8
    tracker.add_dx_trade(c, size=6.0, aggressor="BUY", delta=0.4)
    tracker.add_dx_trade(c, size=1.0, aggressor="UNDEFINED", delta=0.4)
    tracker.add_dx_trade(c, size=2.0, aggressor="BUY", delta=None)  # bez delty
    tracker.add_bar(c, 115.0, last=10.3, bid=10.0, ask=10.4, delta=0.4)
    row_1 = tracker.close_minute(dt.datetime(2026, 9, 8, 14, 0, tzinfo=dt.UTC))
    assert (row_1.printed_volume, row_1.unknown_volume) == (6.0, 1.0)
    assert row_1.structured_volume == 8.0
    assert row_1.fallback_volume == 0.0
    assert row_1.dropped_no_delta == 1
    # tok v režimu midpoint je dál z celého přírůstku baru — zdroj znaménka se nemění
    assert row_1.flow_delta == pytest.approx(15 * 0.4 * 50.0)
    # minuta 2: bar bez jediného tisku → fallback 5, ostatní přírůstky 0
    tracker.add_bar(c, 120.0, last=10.3, bid=10.0, ask=10.4, delta=0.4)
    row_2 = tracker.close_minute(dt.datetime(2026, 9, 8, 14, 1, tzinfo=dt.UTC))
    assert (row_2.printed_volume, row_2.structured_volume) == (0.0, 0.0)
    assert row_2.fallback_volume == 5.0
    assert row_2.dropped_no_delta == 0
    # denní součet ve /status zůstává kumulativní
    stats = tracker.day_stats()
    assert stats["structured_volume"] == 8.0 and stats["fallback_volume"] == 5.0
    # podíl tisků se stranou: z klasifikovaného objemu 6 / (6+1+5), z celého
    # přírůstku včetně struktury 6 / (6+1+5+8) — #1460, /status
    assert stats["printed_share"] == pytest.approx(0.5)
    assert stats["printed_share_total"] == pytest.approx(0.3)
    assert CumDeltaTracker(multiplier=50.0).day_stats()["printed_share_total"] == 0.0


def test_pokryti_bez_trade_vetve_je_null() -> None:
    """Tasty odpojené v režimu midpoint → sloupce NULL, ne nuly (nula = „nic nepokryto")."""
    tracker = CumDeltaTracker(multiplier=50.0, source=SOURCE_MIDPOINT)
    c = spec("P")
    tracker.add_bar(c, 10.0, last=5.0, bid=5.0, ask=5.4, delta=-0.3)
    tracker.add_bar(c, 14.0, last=5.0, bid=5.0, ask=5.4, delta=-0.3)
    row = tracker.close_minute(dt.datetime(2026, 9, 8, 14, 0, tzinfo=dt.UTC))
    assert row.printed_volume is None and row.fallback_volume is None
    assert row.dropped_no_delta is None
    assert tracker.day_stats()["fallback_volume"] == 0.0  # bez větve se fallback neměří


# ── Model mezery trade větve (#1460 krok 4, ADR-0057 bod 3) ───────────
GAP_TS = dt.datetime(2026, 10, 20, 14, 0, tzinfo=dt.UTC)


def test_mezera_je_null_a_cumdelta_pokracuje_od_merene_urovne() -> None:
    """Minuta s mezerou: flowΔ, CumΔ i pokrytí NULL; její tisky se do CumΔ nikdy nepřičtou."""
    tracker = CumDeltaTracker(multiplier=50.0, source=SOURCE_AGGRESSOR)
    c = spec("C")
    tracker.add_dx_trade(c, size=2.0, aggressor="BUY", delta=0.5)  # +50
    start_measured(tracker)
    assert tracker.close_minute(GAP_TS).cum_delta == 50.0

    tracker.add_dx_trade(c, size=4.0, aggressor="BUY", delta=0.5)  # částečná minuta
    next_cycle(tracker, False)  # trade větev spadla
    assert tracker.cum_delta == 150.0  # průběžně včetně neuzavřených tisků
    gap = tracker.close_minute(GAP_TS + dt.timedelta(minutes=1))
    assert gap == FlowRow(GAP_TS + dt.timedelta(minutes=1), None, None, source=SOURCE_AGGRESSOR)
    assert tracker.cum_delta == 50.0

    tracker.add_dx_trade(c, size=1.0, aggressor="SELL", delta=0.5)
    next_cycle(tracker, True)  # hraniční vzorek byl „neběží" → i tahle minuta je mezera
    assert tracker.close_minute(GAP_TS + dt.timedelta(minutes=2)).flow_delta is None

    tracker.add_dx_trade(c, size=1.0, aggressor="SELL", delta=0.5)  # −25
    next_cycle(tracker, True)
    row = tracker.close_minute(GAP_TS + dt.timedelta(minutes=3))
    assert (row.flow_delta, row.cum_delta) == (-25.0, 25.0)  # od poslední měřené úrovně 50


def test_mezera_vrati_net_castecne_minuty_ale_ne_seed() -> None:
    """Restart za otevřeného trhu: první minuta je mezera, net i CumΔ zůstanou na seedu.

    Snapshot s návratem celého stavu by vrátil i základ z partic (`restore_*`)
    nastavený uprostřed téže minuty.
    """
    tracker = CumDeltaTracker(multiplier=50.0, source=SOURCE_AGGRESSOR)
    c, p = spec("C"), spec("P")
    # Tisky před prvním vzorkem po startu
    tracker.add_dx_trade(c, size=3.0, aggressor="BUY", delta=0.5)
    tracker.add_dx_trade(c, size=4.0, aggressor="BUY", delta=None)  # bez delty, jen net
    tracker.add_dx_trade(p, size=2.0, aggressor="SELL", delta=-0.5)
    next_cycle(tracker, True)  # první vzorek po startu: hranice neznámá
    assert not tracker.minute_measured
    tracker.restore_cum(1000.0)
    tracker.restore_net_volume({c: 20.0})
    assert tracker.net_volumes() == {c: 27.0, p: -2.0}  # FA a netflow vidí net do řezu

    assert tracker.close_minute(GAP_TS).cum_delta is None

    assert tracker.net_volumes() == {c: 20.0, p: 0.0}
    assert tracker.cum_delta == 1000.0
    stats = tracker.day_stats()  # denní pokrytí bez minuty s mezerou
    assert stats["printed_volume"] == 0.0 and stats["dropped_no_delta"] == 0.0


def test_pokryti_a_rozklad_jen_z_merenych_minut() -> None:
    """Rozklad v mezeře je NULL; tisky od baru zasažené mezerou se do rozkladu nepočítají."""
    tracker = CumDeltaTracker(multiplier=50.0, source=SOURCE_AGGRESSOR)
    c, p = spec("C"), spec("P")
    start_measured(tracker)
    tracker.add_volume(c, 100.0)
    tracker.add_volume(p, 50.0)
    tracker.close_minute(GAP_TS)

    tracker.add_dx_trade(c, size=3.0, aggressor="BUY", delta=0.5)
    tracker.add_dx_trade(p, size=3.0, aggressor="BUY", delta=-0.5)
    next_cycle(tracker, False)  # mezera: C dostane bar, P ne (zmrzlá kotace)
    tracker.add_volume(c, 110.0)
    assert tracker.take_breakdowns()[c] == BarBreakdown(10.0, None, None)
    tracker.close_minute(GAP_TS + dt.timedelta(minutes=1))

    next_cycle(tracker, True)  # druhá minuta mezery: C dostane bar i v ní
    tracker.add_volume(c, 110.0)
    tracker.close_minute(GAP_TS + dt.timedelta(minutes=2))
    tracker.add_dx_trade(c, size=2.0, aggressor="BUY", delta=0.5)
    tracker.add_dx_trade(p, size=1.0, aggressor="SELL", delta=-0.5)
    next_cycle(tracker, True)
    tracker.add_volume(c, 115.0)  # od baru v mezeře jen měřené tisky
    tracker.add_volume(p, 60.0)  # od baru před mezerou: tisky neúplné
    breakdowns = tracker.take_breakdowns()
    assert breakdowns[c] == BarBreakdown(5.0, 2.0, 3.0)
    assert breakdowns[p] == BarBreakdown(10.0, None, None)
    row = tracker.close_minute(GAP_TS + dt.timedelta(minutes=3))
    assert (row.printed_volume, row.structured_volume, row.fallback_volume) == (3.0, 3.0, 0.0)

    next_cycle(tracker, True)
    tracker.add_volume(p, 64.0)  # další bar P už je čistý
    assert tracker.take_breakdowns()[p] == BarBreakdown(4.0, 0.0, 4.0)
    stats = tracker.day_stats()
    assert (stats["printed_volume"], stats["structured_volume"]) == (3.0, 3.0)
    assert stats["fallback_volume"] == 4.0


def test_zavreny_trh_mezeru_nedela() -> None:
    """Víkend, pauza i start enginu za zavřeného trhu: řádek je měřená nula, ne NULL."""
    tracker = CumDeltaTracker(multiplier=50.0, source=SOURCE_AGGRESSOR)
    sample(tracker, running=False, market_closed=True)  # start v pauze, tasty odpojené
    tracker.begin_cycle()
    row = tracker.close_minute(GAP_TS)
    assert (row.flow_delta, row.cum_delta, row.printed_volume) == (0.0, 0.0, 0.0)

    sample(tracker, running=False, market_closed=True)
    tracker.begin_cycle()
    assert tracker.close_minute(GAP_TS + dt.timedelta(minutes=1)).flow_delta == 0.0
    # Open: hraniční vzorek z pauzy platí, rozhoduje stav trade větve v openu
    next_cycle(tracker, True)
    assert tracker.minute_measured
    tracker.close_minute(GAP_TS + dt.timedelta(minutes=2))
    # Zavření trhu: minuta před ním bez trade větve je mezera i se vzorkem z pauzy
    next_cycle(tracker, False)
    tracker.close_minute(GAP_TS + dt.timedelta(minutes=3))
    sample(tracker, running=False, market_closed=True)
    tracker.begin_cycle()
    assert not tracker.minute_measured


def test_sekundar_sbira_and_pres_vsechny_vzorky_okna() -> None:
    """Cyklus po N minutách: jediný vzorek „neběží" v okně udělá mezeru z celého okna."""
    tracker = CumDeltaTracker(multiplier=50.0, source=SOURCE_AGGRESSOR)
    start_measured(tracker)
    tracker.close_minute(GAP_TS)
    next_cycle(tracker, True, False, True)
    assert not tracker.minute_measured
    tracker.close_minute(GAP_TS + dt.timedelta(minutes=3))
    next_cycle(tracker, True, True, True)
    assert tracker.minute_measured


def test_cyklus_bez_vzorku_nebo_neuzavrena_minuta_je_mezera() -> None:
    """Bez vzorku od minulého cyklu nic nedokládá běh; spadlý cyklus nese svůj verdikt dál."""
    tracker = CumDeltaTracker(multiplier=50.0, source=SOURCE_AGGRESSOR)
    tracker.begin_cycle()
    assert not tracker.minute_measured
    tracker.close_minute(GAP_TS)
    start_measured(tracker)
    tracker.close_minute(GAP_TS + dt.timedelta(minutes=1))

    tracker.begin_cycle()  # orchestrátor tento cyklus nevzorkoval → mezera
    assert not tracker.minute_measured
    tracker.add_dx_trade(spec("C"), size=4.0, aggressor="BUY", delta=0.5)
    next_cycle(tracker, True)  # cyklus spadl před uzávěrkou: minuta s tisky mezery trvá
    assert not tracker.minute_measured
    assert tracker.close_minute(GAP_TS + dt.timedelta(minutes=3)).cum_delta is None
    next_cycle(tracker, True)
    assert tracker.minute_measured
    assert tracker.cum_delta == 0.0


def test_reset_seance_verdikt_a_vzorky_nemaze() -> None:
    """Open 17:00 CT: vzorek přijde před resetem v `run_cycle` a reset ho nesmí zahodit."""
    tracker = CumDeltaTracker(multiplier=50.0, source=SOURCE_AGGRESSOR)
    start_measured(tracker)
    tracker.close_minute(GAP_TS)
    tracker.roll_session(dt.date(2026, 10, 20))
    sample(tracker, running=True)  # vzorek a řez openu (orchestrátor)
    assert tracker.roll_session(dt.date(2026, 10, 21))  # reset kumulativů v run_cycle
    tracker.begin_cycle()
    assert tracker.minute_measured


def test_midpoint_mezeru_nezna() -> None:
    """Mechanika v5: bez vzorků je minuta měřená a řádek nese čísla jako dřív."""
    tracker = CumDeltaTracker(multiplier=50.0, source=SOURCE_MIDPOINT)
    tracker.begin_cycle()
    assert tracker.minute_measured
    tracker.add_bar(spec("C"), 100.0, 10.3, 10.0, 10.4, delta=0.5)
    tracker.add_bar(spec("C"), 110.0, 10.3, 10.0, 10.4, delta=0.5)  # +250
    row = tracker.close_minute(GAP_TS)
    assert (row.flow_delta, row.cum_delta, row.printed_volume) == (250.0, 250.0, None)


# ── Řez tisků ve vzorku (#1460, review 1a) ─────────────────────────────
def test_tisky_po_rezu_patri_dalsimu_radku() -> None:
    """Řádek M nese tisky (vzorek M−1, vzorek M] — tok, net i tiskové pokrytí."""
    tracker = CumDeltaTracker(multiplier=50.0, source=SOURCE_AGGRESSOR)
    c = spec("C")
    tracker.add_dx_trade(c, size=2.0, aggressor="BUY", delta=0.5)  # +50 před vzorkem M
    start_measured(tracker)
    tracker.add_dx_trade(c, size=1.0, aggressor="SELL", delta=0.5)  # −25 během cyklu M
    assert tracker.net_volumes() == {c: 2.0}  # FA a netflow cyklu M jen do řezu
    row = tracker.close_minute(GAP_TS)
    assert (row.flow_delta, row.cum_delta, row.printed_volume) == (50.0, 50.0, 2.0)
    assert tracker.cum_delta == 25.0  # průběžně i s neuzavřeným tiskem

    next_cycle(tracker, True)
    row = tracker.close_minute(GAP_TS + dt.timedelta(minutes=1))
    assert (row.flow_delta, row.cum_delta, row.printed_volume) == (-25.0, 25.0, 1.0)
    assert tracker.net_volumes() == {c: 1.0}


def test_mezera_vrati_jen_usek_do_rezu_bez_rezu_i_otevreny() -> None:
    """Mezera zahodí tisky do řezu, tisky po řezu nese další řádek. Cyklus bez
    vzorku (a řezu) nedokládá nic ani u otevřeného úseku — zahodí se i ten."""
    tracker = CumDeltaTracker(multiplier=50.0, source=SOURCE_AGGRESSOR)
    c = spec("C")
    start_measured(tracker)
    tracker.close_minute(GAP_TS)
    tracker.add_dx_trade(c, size=4.0, aggressor="BUY", delta=0.5)  # minuta s pádem streamu
    tracker.observe(running=False, market_closed=False)  # reconnect mezi vzorky (monitor)
    sample(tracker, running=True)  # stream v okamžiku vzorku zase běží
    tracker.begin_cycle()
    assert not tracker.minute_measured
    tracker.add_dx_trade(c, size=2.0, aggressor="BUY", delta=0.5)  # po řezu, +50
    assert tracker.close_minute(GAP_TS + dt.timedelta(minutes=1)).cum_delta is None
    assert tracker.net_volumes() == {c: 0.0}
    assert tracker.day_stats()["printed_volume"] == 2.0  # otevřený úsek zůstává

    next_cycle(tracker, True)  # hraniční vzorek „běží" → měřená
    row = tracker.close_minute(GAP_TS + dt.timedelta(minutes=2))
    assert (row.flow_delta, row.cum_delta, row.printed_volume) == (50.0, 50.0, 2.0)
    assert tracker.net_volumes() == {c: 2.0}

    tracker.add_dx_trade(c, size=1.0, aggressor="SELL", delta=0.5)
    tracker.begin_cycle()  # vzorek selhal: žádný vzorek ani řez
    assert not tracker.minute_measured
    tracker.add_dx_trade(c, size=3.0, aggressor="SELL", delta=0.5)
    assert tracker.close_minute(GAP_TS + dt.timedelta(minutes=3)).cum_delta is None
    next_cycle(tracker, True)
    row = tracker.close_minute(GAP_TS + dt.timedelta(minutes=4))
    assert (row.flow_delta, row.cum_delta, row.printed_volume) == (0.0, 50.0, 0.0)
    assert tracker.net_volumes() == {c: 2.0}
    assert tracker.day_stats()["printed_volume"] == 2.0


# ── Tisky nové seance před rollem trackeru (#1460, review 1a) ─────────
#: Pondělí 19. 10. 2026 16:59 CDT (pauza, seance 19. 10.) a open 17:00 CDT (seance 20. 10.)
PAUSE_LAST = dt.datetime(2026, 10, 19, 21, 59, tzinfo=dt.UTC)
OPEN_TS = dt.datetime(2026, 10, 19, 22, 0, tzinfo=dt.UTC)
SESSION_D = dt.date(2026, 10, 19)
SESSION_NEXT = dt.date(2026, 10, 20)


def _pause_minute_with_next_session_print(tracker: CumDeltaTracker) -> FlowRow:
    """Cyklus 16:59 CT; tisk po 17:00:00 přijde dřív, než cyklus minutu uzavře."""
    tracker.roll_session(SESSION_D)
    sample(tracker, running=False, market_closed=True)
    tracker.begin_cycle()
    tracker.add_dx_trade(spec("C"), 2.0, "BUY", 0.5, session_date=SESSION_NEXT)  # +50
    return tracker.close_minute(PAUSE_LAST)


def test_tisky_nove_seance_pred_rollem_nese_prvni_radek() -> None:
    """Roll v `run_cycle` přijde až po sweepu: tisky od openu nesmaže, nese je řádek 17:00."""
    tracker = CumDeltaTracker(multiplier=50.0, source=SOURCE_AGGRESSOR)
    c = spec("C")
    row = _pause_minute_with_next_session_print(tracker)
    assert (row.flow_delta, row.printed_volume) == (0.0, 0.0)
    assert tracker.net_volumes() == {}  # ani v netu seance D

    sample(tracker, running=True)  # vzorek a řez openu 17:00
    tracker.add_dx_trade(c, 1.0, "SELL", 0.5, session_date=SESSION_NEXT)  # po řezu, −25
    assert tracker.roll_session(SESSION_NEXT)
    tracker.begin_cycle()
    assert tracker.minute_measured
    assert tracker.net_volumes() == {c: 2.0}
    row = tracker.close_minute(OPEN_TS)
    assert (row.flow_delta, row.cum_delta, row.printed_volume) == (50.0, 50.0, 2.0)

    next_cycle(tracker, True)
    row = tracker.close_minute(OPEN_TS + dt.timedelta(minutes=1))
    assert (row.flow_delta, row.cum_delta) == (-25.0, 25.0)
    assert tracker.net_volumes() == {c: 1.0}


def test_tisky_nove_seance_v_mezere_openu_se_vrati() -> None:
    """Mezera v 17:00: tisky od openu se vrátí stejně jako ostatní tisky minuty."""
    tracker = CumDeltaTracker(multiplier=50.0, source=SOURCE_AGGRESSOR)
    _pause_minute_with_next_session_print(tracker)
    sample(tracker, running=False)  # open bez trade větve
    assert tracker.roll_session(SESSION_NEXT)
    tracker.begin_cycle()
    assert not tracker.minute_measured
    assert tracker.close_minute(OPEN_TS).cum_delta is None
    assert tracker.net_volumes() == {spec("C"): 0.0}
    assert tracker.day_stats()["printed_volume"] == 0.0


def test_midpoint_seanci_tisku_necte() -> None:
    """Mechanika v5 beze změny: tisk jde do pokrytí hned a reset seance ho smaže jako dřív."""
    tracker = CumDeltaTracker(multiplier=50.0, source=SOURCE_MIDPOINT)
    tracker.roll_session(SESSION_D)
    tracker.add_dx_trade(spec("C"), 2.0, "BUY", 0.5, session_date=SESSION_NEXT)
    assert tracker.day_stats()["printed_volume"] == 2.0
    assert tracker.roll_session(SESSION_NEXT)
    assert tracker.day_stats()["printed_volume"] == 0.0


# --- flow_window_sum: sklon CumΔ za okno v seanci (#1460 krok 3, ADR-0057 bod 4) ---

MIN = dt.timedelta(minutes=1)
#: Úterý 20. 10. 2026 v RTH (CDT) — okno daleko od hranic seance
WINDOW_END = dt.datetime(2026, 10, 20, 14, 0, tzinfo=dt.UTC)
#: Open seance 20. 10. = pondělí 19. 10. 17:00 CDT
OPEN_CDT = dt.datetime(2026, 10, 19, 22, 0, tzinfo=dt.UTC)
#: Open seance 4. 11. = úterý 3. 11. 17:00 CST (po konci letního času v USA)
OPEN_CST = dt.datetime(2026, 11, 3, 23, 0, tzinfo=dt.UTC)

FlowPoints = list[tuple[dt.datetime, float | None]]


def flow_rows(start: dt.datetime, end: dt.datetime, value: float) -> FlowPoints:
    """Řádky flow po minutách v [start, end] se stejnou hodnotou flowΔ."""
    rows: FlowPoints = []
    ts = start
    while ts <= end:
        rows.append((ts, value))
        ts += MIN
    return rows


def window_rows() -> FlowPoints:
    """Hraniční řádek 13:50 (100, do součtu nepatří) + okno 13:51–14:00 s flowΔ 1–10."""
    rows: FlowPoints = [(WINDOW_END - 10 * MIN, 100.0)]
    rows += [(WINDOW_END - (10 - i) * MIN, float(i)) for i in range(1, 11)]
    return rows


def test_flow_window_sum_secte_jen_okno() -> None:
    """Σ řádků (end−10, end]; hraniční řádek, starší řádky ani řádky po end se nečtou."""
    rows = window_rows() + [(WINDOW_END - 11 * MIN, 1000.0), (WINDOW_END + MIN, 1000.0)]
    assert flow_window_sum(reversed(rows), WINDOW_END) == 55.0  # pořadí řádků nehraje roli
    chicago = [(ts.astimezone(ZoneInfo("America/Chicago")), v) for ts, v in rows]
    assert flow_window_sum(chicago, WINDOW_END) == 55.0  # okamžik, ne zóna zápisu
    assert flow_window_sum(rows, WINDOW_END, minutes=3) == 27.0  # hranice 13:57 (7)


@pytest.mark.parametrize(
    ("minutes_before_end", "change", "expected"),
    [
        pytest.param(5, "null", None, id="NULL uvnitr okna"),
        pytest.param(0, "null", None, id="NULL v end"),
        pytest.param(10, "null", 55.0, id="NULL na hranici jen dokladuje uzaverku"),
        pytest.param(5, "drop", 50.0, id="chybejici radek uvnitr nevadi"),
        pytest.param(0, "drop", None, id="chybi end"),
        pytest.param(10, "drop", None, id="chybi hranice"),
        pytest.param(5, "dup", None, id="duplicita v okne"),
        pytest.param(10, "dup", 55.0, id="duplicita hranice nevadi"),
    ],
)
def test_flow_window_sum_neuplne_okno_je_none(
    minutes_before_end: int, change: str, expected: float | None
) -> None:
    """Neúplné okno → None (ADR-0057 bod 4); chybějící řádek uvnitř přejde do dalšího řádku."""
    target = WINDOW_END - minutes_before_end * MIN
    rows: FlowPoints = []
    for ts, value in window_rows():
        if ts != target:
            rows.append((ts, value))
        elif change == "null":
            rows.append((ts, None))
        elif change == "dup":
            rows += [(ts, value), (ts, value)]
    assert flow_window_sum(rows, WINDOW_END) == expected


def test_flow_window_sum_neprekroci_zacatek_seance() -> None:
    """#1581: okno nesahá přes reset CumΔ v 17:00 CT; od 10. minuty seance hodnota."""
    rows = flow_rows(OPEN_CDT - 10 * MIN, OPEN_CDT - MIN, 100.0)
    rows += flow_rows(OPEN_CDT, OPEN_CDT + 10 * MIN, 1.0)
    assert flow_window_sum(rows, OPEN_CDT + 3 * MIN) is None  # 22:03 UTC
    assert flow_window_sum(rows, OPEN_CDT + 9 * MIN) == 10.0  # 22:09 UTC: hranicí je open
    assert flow_window_sum(rows, OPEN_CDT + 10 * MIN) == 10.0  # hranice 22:00 už v seanci


def test_flow_window_sum_od_openu_chce_radek_prvni_minuty_seance() -> None:
    """Uzávěrkou před oknem od openu je roll; bez řádku 22:00 roll proběhl později a tok chybí."""
    rows = flow_rows(OPEN_CDT - 10 * MIN, OPEN_CDT - MIN, 100.0)
    rows += flow_rows(OPEN_CDT + MIN, OPEN_CDT + 9 * MIN, 1.0)
    assert flow_window_sum(rows, OPEN_CDT + 9 * MIN) is None


def test_flow_window_sum_pres_pulnoc_utc_cte_obe_partice() -> None:
    """00:05 UTC: okno i hraniční řádek leží v partici předchozího UTC dne (#1002, #1390)."""
    midnight = dt.datetime(2026, 10, 20, 0, 0, tzinfo=dt.UTC)
    previous_day = flow_rows(midnight - 5 * MIN, midnight - MIN, 2.0)  # hranice 23:55 + 4 × 2
    today = flow_rows(midnight, midnight + 5 * MIN, 1.0)  # 6 × 1
    assert flow_window_sum(today + previous_day, midnight + 5 * MIN) == 14.0
    assert flow_window_sum(today, midnight + 5 * MIN) is None  # jen dnešní partice nestačí


def test_flow_window_sum_pauza_s_nulami_da_nulu() -> None:
    """Za zavřeného trhu nese řada nuly → sklon 0.0 (hlas 0), ne None."""
    end = dt.datetime(2026, 10, 20, 21, 30, tzinfo=dt.UTC)  # 16:30 CDT, pauza Globexu
    assert flow_window_sum(flow_rows(end - 10 * MIN, end, 0.0), end) == 0.0


def test_flow_window_sum_zacatek_seance_v_cst() -> None:
    """Po konci letního času (1. 11. 2026) je open 23:00 UTC, ne 22:00 UTC."""
    rows = flow_rows(OPEN_CST - 70 * MIN, OPEN_CST + 10 * MIN, 1.0)
    assert flow_window_sum(rows, OPEN_CST - 57 * MIN) == 10.0  # 22:03 UTC = 16:03 CST, pauza
    assert flow_window_sum(rows, OPEN_CST + 3 * MIN) is None  # 23:03 UTC
    assert flow_window_sum(rows, OPEN_CST + 9 * MIN) == 10.0  # 23:09 UTC


def test_flow_window_sum_odmitne_neplatny_konec_okna() -> None:
    """Naivní nebo necelá minuta je chyba volajícího, ne tiché None."""
    with pytest.raises(ValueError, match="časovou zónu"):
        flow_window_sum([], WINDOW_END.replace(tzinfo=None))
    with pytest.raises(ValueError, match="celá minuta"):
        flow_window_sum([], WINDOW_END.replace(second=30))
    with pytest.raises(ValueError, match="≥ 1"):
        flow_window_sum([], WINDOW_END, minutes=0)
    with pytest.raises(TypeError):  # naivní ts řádku se nesmí tiše vynechat
        flow_window_sum([(WINDOW_END.replace(tzinfo=None), 1.0)], WINDOW_END)
