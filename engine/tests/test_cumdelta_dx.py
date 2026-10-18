"""Stínové CumΔ z TimeAndSale (#615 fáze 3): zóny a pokrytí strany."""

import datetime as dt
from pathlib import Path

import pytest

from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.tasty.cumdelta_dx import DxCumDeltaShadow, plan_dx_flush

TS = dt.datetime(2026, 8, 27, 14, 0, tzinfo=dt.UTC)


def spec(strike: float, right: str = "C") -> OptionContractSpec:
    return OptionContractSpec(
        symbol="ES",
        sec_type="FOP",
        expiry="20260827",
        strike=strike,
        right=right,
        exchange="CME",
        trading_class="E4D",
        multiplier="50",
    )


def make_shadow(spot: float = 7600.0) -> DxCumDeltaShadow:
    shadow = DxCumDeltaShadow(multiplier=50.0)
    # Žebřík 7500–7700 à 5 b: ATM 7600, hot ±1 strike, prstenec ±15
    universe = {f".ES{int(strike)}C": spec(strike) for strike in range(7500, 7705, 5)}
    shadow.set_universe(universe)
    shadow.set_spot(spot)
    shadow.roll_session(TS.date())
    return shadow


def test_cely_retez_je_jedna_rada_bez_zon() -> None:
    """ADR-0032 doplněk: každý strike univerza se počítá do hlavní řady, hot je 0."""
    shadow = make_shadow()
    shadow.on_trade(".ES7600C", size=2, aggressor="BUY", delta=0.5)  # ATM
    shadow.on_trade(".ES7610C", size=3, aggressor="BUY", delta=0.4)
    shadow.on_trade(".ES7680C", size=9, aggressor="BUY", delta=0.1)  # 16 striků od ATM
    row = shadow.close_minute(TS)
    assert row.flow_hot == 0.0
    assert row.flow_ring == pytest.approx((2 * 0.5 + 3 * 0.4 + 9 * 0.1) * 50.0)
    assert row.trades == 3
    # symbol mimo univerzum se nepočítá vůbec
    shadow.on_trade(".ES7800C", size=1, aggressor="SELL", delta=0.2)
    assert shadow.close_minute(TS).trades == 0


def test_vsechny_trady_prstence_jdou_do_jedne_rady() -> None:
    """Spread legy se nerozlišují (3. 9. 2026): CME příznak nenese, řada je jedna."""
    shadow = make_shadow()
    shadow.on_trade(".ES7610C", size=4, aggressor="BUY", delta=0.5)
    shadow.on_trade(".ES7615C", size=6, aggressor="SELL", delta=0.5)
    row = shadow.close_minute(TS)
    assert row.flow_ring == (4 - 6) * 0.5 * 50.0
    assert row.trades == 2
    assert row.volume == 10


def test_neznama_strana_se_nepocita_do_toku_ale_meri_se() -> None:
    """Bez aggressorSide žádný tok — pokrytí rozhoduje o midpoint fallbacku."""
    shadow = make_shadow()
    shadow.on_trade(".ES7610C", size=5, aggressor=None, delta=0.5)
    shadow.on_trade(".ES7610C", size=5, aggressor="UNDEFINED", delta=0.5)
    row = shadow.close_minute(TS)
    assert row.flow_ring == 0.0
    assert row.unknown_side == 2
    assert row.trades == 2 and row.volume == 10


def test_bez_spotu_nebo_delty_se_trade_zahazuje_a_pocita() -> None:
    """Díra v měření se přizná (dropped_no_context), tok se nevymýšlí.

    Bez spotu se od doplňku ADR-0032 počítá dál — zóny nejsou; bez delty ne."""
    shadow = make_shadow()
    shadow.set_spot(None)
    shadow.on_trade(".ES7610C", size=1, aggressor="BUY", delta=0.5)
    shadow.set_spot(7600.0)
    shadow.on_trade(".ES7610C", size=1, aggressor="BUY", delta=None)
    row = shadow.close_minute(TS)
    assert row.flow_ring == pytest.approx(1 * 0.5 * 50.0)
    assert row.dropped_no_context == 1


def test_kumulativy_a_roll_session() -> None:
    shadow = make_shadow()
    shadow.on_trade(".ES7610C", size=2, aggressor="BUY", delta=0.5)
    shadow.close_minute(TS)
    shadow.on_trade(".ES7610C", size=2, aggressor="BUY", delta=0.5)
    row = shadow.close_minute(TS + dt.timedelta(minutes=1))
    assert row.cum_ring == 2 * (2 * 0.5 * 50.0)
    # Nový den = reset (stejně jako živé CumΔ)
    assert shadow.roll_session(TS.date() + dt.timedelta(days=1))
    assert shadow.close_minute(TS + dt.timedelta(days=1)).cum_ring == 0.0
    # Týž den podruhé nic neresetuje
    assert not shadow.roll_session(TS.date() + dt.timedelta(days=1))


def test_write_dx_flow_roundtrip(tmp_path: Path) -> None:
    """Minutová řada se ukládá do derived/{sym}/cumdelta_dx a čte zpět 1:1."""
    import pyarrow.parquet as pq

    from gexlens_engine.config import Settings
    from gexlens_engine.storage.parquet_store import SnapshotWriter

    settings = Settings(data_dir=tmp_path)
    writer = SnapshotWriter(settings)
    shadow = make_shadow()
    shadow.on_trade(".ES7610C", size=4, aggressor="BUY", delta=0.5)
    row = shadow.close_minute(TS)

    path = writer.write_dx_flow("ES", TS.date(), [row])

    assert path == settings.derived_dir / "ES" / "cumdelta_dx" / "2026-08-27.parquet"
    record = pq.read_table(path).to_pylist()[0]
    assert record["flow_ring"] == 4 * 0.5 * 50.0
    assert record["volume"] == 4.0
    assert "spread_volume" not in record and "cum_ring_outright" not in record
    assert record["ts_min"] == TS


def test_seed_navaze_kumulativ_po_restartu(tmp_path: Path) -> None:
    """#1070: nový akumulátor pokračuje tam, kde předchozí běh skončil — kumulativy
    z posledního řádku, denní čítače součtem; roll na další seanci seed zahodí."""
    from gexlens_engine.config import Settings
    from gexlens_engine.storage.parquet_store import SnapshotWriter

    writer = SnapshotWriter(Settings(data_dir=tmp_path))
    first = make_shadow()
    first.roll_session(TS.date())
    first.on_trade(".ES7610C", size=2, aggressor="BUY", delta=0.5)  # +50
    first.on_trade(".ES7610C", size=1, aggressor=None, delta=0.5)  # neznámá strana
    writer.write_dx_flow("ES", TS.date(), [first.close_minute(TS)])
    first.on_trade(".ES7610C", size=4, aggressor="SELL", delta=0.5)  # −100
    writer.write_dx_flow("ES", TS.date(), [first.close_minute(TS + dt.timedelta(minutes=1))])
    assert first.day_stats()["cum_ring"] == -50.0

    # „Restart": čerstvý akumulátor, první uzávěrka seance
    second = make_shadow()  # helper seanci už nastavil; v enginu vrací první roll True
    second.roll_session(TS.date())
    seed = writer.dx_flow_seed("ES", TS.date())
    assert seed is not None
    assert seed.ts_min == TS + dt.timedelta(minutes=1)
    assert seed.cum_ring == -50.0
    assert (seed.trades, seed.unknown_side, seed.volume) == (3, 1, 7.0)
    second.seed(seed)
    stats = second.day_stats()
    assert stats["cum_ring"] == -50.0
    assert stats["trades"] == 3.0
    assert stats["unknown_side_share"] == pytest.approx(1 / 3)
    assert stats["seeded_from_ts"] == (TS + dt.timedelta(minutes=1)).isoformat()
    # Další minuta navazuje, ne od nuly
    second.on_trade(".ES7610C", size=2, aggressor="BUY", delta=0.5)
    assert second.close_minute(TS + dt.timedelta(minutes=2)).cum_ring == 0.0

    # Nová seance = reset včetně příznaku navázání
    assert second.roll_session(TS.date() + dt.timedelta(days=1))
    assert second.day_stats()["seeded_from_ts"] is None
    assert second.day_stats()["cum_ring"] == 0.0

    # Bez partice není z čeho navazovat
    assert writer.dx_flow_seed("NQ", TS.date()) is None


def test_write_dx_flow_upsertuje_tutez_minutu(tmp_path: Path) -> None:
    """#1070: restart uprostřed minuty nesmí minutu zdvojit."""
    import pyarrow.parquet as pq

    from gexlens_engine.config import Settings
    from gexlens_engine.storage.parquet_store import SnapshotWriter

    writer = SnapshotWriter(Settings(data_dir=tmp_path))
    shadow = make_shadow()
    shadow.on_trade(".ES7610C", size=1, aggressor="BUY", delta=0.5)
    writer.write_dx_flow("ES", TS.date(), [shadow.close_minute(TS)])
    shadow.on_trade(".ES7610C", size=3, aggressor="BUY", delta=0.5)
    path = writer.write_dx_flow("ES", TS.date(), [shadow.close_minute(TS)])
    rows = pq.read_table(path).to_pylist()
    assert len(rows) == 1
    assert rows[0]["volume"] == 3.0


# --- #1586: uzávěrka stínu pod tisky na hranici minuty ---------------------

MIN = dt.timedelta(minutes=1)


def test_plan_dx_flush_ceka_do_hranice_minuty() -> None:
    """Štítek = příští celá minuta, čeká se do ní podle nástěnných hodin."""
    plan = plan_dx_flush(TS + dt.timedelta(seconds=17, milliseconds=300), None)
    assert plan.minute == TS + MIN
    assert plan.wait_s == pytest.approx(42.7)
    assert plan.skipped == 0
    # Přesně na hranici po minulé uzávěrce: zavřít hned, nic nechybí
    exact = plan_dx_flush(TS + MIN, TS)
    assert (exact.minute, exact.wait_s, exact.skipped) == (TS + MIN, 0.0, 0)


def test_plan_dx_flush_predcasne_probuzeni_neprepise_minuly_radek() -> None:
    """Probuzení pár ms před hranicí: floor(now) by dal minulou minutu a upsert by
    přepsal její řádek — cíl leží vždy po štítku minulé uzávěrky."""
    plan = plan_dx_flush(TS + MIN - dt.timedelta(milliseconds=5), TS + MIN)
    assert plan.minute == TS + 2 * MIN
    assert plan.wait_s == pytest.approx(60.005)
    assert plan.skipped == 0


def test_plan_dx_flush_hlasi_hranice_bez_radku() -> None:
    """Práce přetekla přes hranici, nebo kontejner stál: hranice bez řádku se vrátí
    jako `skipped` (warning v enginu), štítek se neposune před `now`."""
    overrun = plan_dx_flush(TS + 2 * MIN + dt.timedelta(milliseconds=300), TS + MIN)
    assert (overrun.minute, overrun.skipped) == (TS + 3 * MIN, 1)
    frozen = plan_dx_flush(TS + 11 * MIN + dt.timedelta(seconds=5), TS + MIN)
    assert (frozen.minute, frozen.skipped) == (TS + 12 * MIN, 10)


def test_plan_dx_flush_hodiny_posunute_zpet() -> None:
    """NTP krok zpět: cíl dál po minulém štítku, jen se čeká déle."""
    plan = plan_dx_flush(TS - dt.timedelta(seconds=30), TS + MIN)
    assert plan.minute == TS + 2 * MIN
    assert plan.wait_s == pytest.approx(150.0)
    assert plan.skipped == 0


def test_plan_dx_flush_odmitne_naivni_cas() -> None:
    with pytest.raises(ValueError, match="časovou zónu"):
        plan_dx_flush(dt.datetime(2026, 8, 27, 14, 0, 17), None)


def _work_s(round_no: int) -> float:
    """Zápis partice roste během dne (append_and_write přepisuje celou partici)."""
    return 0.2 + 2.8 * round_no / 1380


def test_pomaly_zapis_stitky_jdou_po_sobe_bez_diry() -> None:
    """#1586 „Jak ověřit“: zápis 0,2–3 s a probuzení o 2 ms pozdě nebo 4 ms dřív —
    celá seance (1380 min) štítků na mřížce minut bez díry a bez opakování."""
    now = TS + dt.timedelta(seconds=17, milliseconds=300)
    last: dt.datetime | None = None
    labels: list[dt.datetime] = []
    for round_no in range(1380):
        plan = plan_dx_flush(now, last)
        assert plan.skipped == 0
        jitter = dt.timedelta(milliseconds=-4 if round_no % 7 == 0 else 2)
        last = plan.minute
        labels.append(last)
        now = plan.minute + jitter + dt.timedelta(seconds=_work_s(round_no))
    assert labels == [TS + (k + 1) * MIN for k in range(1380)]

    # Kontrola citlivosti: smyčka v5 (60 s po práci, štítek floor(now)) nad týmž
    # časováním štítky přeskakuje — přesně nález #1586
    legacy_now = TS + dt.timedelta(seconds=17, milliseconds=300)
    legacy: list[dt.datetime] = []
    for round_no in range(1380):
        legacy_now += dt.timedelta(seconds=60 + _work_s(round_no))
        legacy.append(legacy_now.replace(second=0, microsecond=0))
    assert any(b - a > MIN for a, b in zip(legacy, legacy[1:], strict=False))


def test_jedno_preteceni_vynecha_jednu_hranici_a_mrizka_pokracuje() -> None:
    """Zápis jednou trvá 61 s: jedna hranice bez řádku, pak zase každá minuta."""
    now = TS + dt.timedelta(seconds=10)
    last: dt.datetime | None = None
    labels: list[dt.datetime] = []
    skipped: list[int] = []
    for round_no in range(6):
        plan = plan_dx_flush(now, last)
        skipped.append(plan.skipped)
        last = plan.minute
        labels.append(last)
        now = plan.minute + dt.timedelta(seconds=61 if round_no == 2 else 0.5)
    assert labels == [TS + k * MIN for k in (1, 2, 3, 5, 6, 7)]
    assert skipped == [0, 0, 0, 1, 0, 0]


def test_restart_v_minute_stopu_minuta_pokracuje_do_dalsi_hranice(tmp_path: Path) -> None:
    """#1586 bod 3 a #1070 pod v6: stop zapíše řádek N se štítkem cílové hranice
    (obsah [N−1, stop)). Restart před N nemá řádek N upsertem přepsat — engine
    naváže a uzavře až N+1. Kumulativ i denní čítače zůstanou přesné."""
    import pyarrow.parquet as pq

    from gexlens_engine.config import Settings
    from gexlens_engine.storage.parquet_store import SnapshotWriter

    writer = SnapshotWriter(Settings(data_dir=tmp_path))
    first = make_shadow()
    first.on_trade(".ES7610C", size=2, aggressor="BUY", delta=0.5)  # +50
    writer.write_dx_flow("ES", TS.date(), [first.close_minute(TS)])
    # Stop v 14:00:20: smyčka cílila na 14:01, uzávěrka po stopu nese jeho štítek
    stop = plan_dx_flush(TS + dt.timedelta(seconds=20), TS)
    assert stop.minute == TS + MIN
    first.on_trade(".ES7610C", size=1, aggressor="SELL", delta=0.5)  # −25
    writer.write_dx_flow("ES", TS.date(), [first.close_minute(stop.minute)])

    # Restart v 14:00:40, ještě před hranicí 14:01
    second = DxCumDeltaShadow(multiplier=50.0)
    second.set_universe({f".ES{int(k)}C": spec(k) for k in range(7500, 7705, 5)})
    second.set_spot(7600.0)
    plan = plan_dx_flush(TS + dt.timedelta(seconds=40), None)
    assert plan.minute == TS + MIN
    assert second.roll_session(TS.date())
    seed = writer.dx_flow_seed("ES", TS.date())
    assert seed is not None and seed.ts_min >= plan.minute  # engine: navázat a neuzavírat
    second.seed(seed)
    second.on_trade(".ES7610C", size=4, aggressor="BUY", delta=0.5)  # +100 v 14:00:50
    nxt = plan_dx_flush(TS + MIN + dt.timedelta(milliseconds=3), plan.minute)
    assert (nxt.minute, nxt.skipped) == (TS + 2 * MIN, 0)
    second.on_trade(".ES7610C", size=2, aggressor="SELL", delta=0.5)  # −50 v 14:01:30
    writer.write_dx_flow("ES", TS.date(), [second.close_minute(nxt.minute)])

    rows = pq.read_table(writer.dx_flow_path("ES", TS.date())).to_pylist()
    assert [r["ts_min"] for r in rows] == [TS, TS + MIN, TS + 2 * MIN]
    assert [r["flow_ring"] for r in rows] == [50.0, -25.0, 50.0]
    assert [r["cum_ring"] for r in rows] == [50.0, 25.0, 75.0]
    # Řádek za restartem nese [restart, N+1): přírůstek kumulativu = jeho tok
    assert rows[2]["cum_ring"] - rows[1]["cum_ring"] == rows[2]["flow_ring"]
    assert second.day_stats()["trades"] == float(sum(r["trades"] for r in rows)) == 4.0
