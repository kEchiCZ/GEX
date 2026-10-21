"""Zdroj CumΔ v replayi backtestu a walk-forwardu (#1590, ADR-0057 bod 7).

v5 čte midpoint řadu `flow`, v6 tisky: seance se zdrojem `aggressor` živou 1A,
seance od 21. 9. 2026 do nasazení stín `cumdelta_dx`, starší seance mezeru.
Skript žije ve `scripts/`, načítá se přes importlib (vzor test_backtest_replay).
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import math
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from gexlens_engine.compute.settle import is_trading_session, trading_session_date

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backtest_setups.py"
UTC = dt.UTC


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("backtest_setups", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bt = _load()


def ts(day: str, hhmm: str) -> pd.Timestamp:
    return pd.Timestamp(f"{day} {hhmm}", tz="UTC")


def _shadow(labels: list[pd.Timestamp], levels: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"ts_min": labels, "cum_ring": levels, "cum_hot": [0.0] * len(labels)})


def _values(frame: pd.DataFrame) -> dict[pd.Timestamp, float]:
    return dict(zip(frame["ts_min"], frame["cum_delta"], strict=True))


# ── Stín: posun štítku, přeskok, díra, mezera z printed NULL ───────────────

DAY = "2026-10-14"  # středa, 14:00 UTC = 09:00 CDT, trh otevřený
SESSION = dt.date(2026, 10, 14)


def test_stitek_stinu_patri_rozhodnuti_v_dalsi_minute() -> None:
    labels = [ts(DAY, "14:00"), ts(DAY, "14:01"), ts(DAY, "14:02")]
    rows = bt.shadow_rows(_shadow(labels, [10.0, 12.0, 15.0]), SESSION, set())
    # Štítek L nese stav k L + φ (φ 0–60 s) → rozhodnutí v L + 1, bez look-ahead (#1586)
    assert _values(rows) == {
        ts(DAY, "14:01"): 10.0,
        ts(DAY, "14:02"): 12.0,
        ts(DAY, "14:03"): 15.0,
    }


def test_preskoceny_stitek_je_posledni_kumulativ() -> None:
    labels = [ts(DAY, "14:00"), ts(DAY, "14:02"), ts(DAY, "14:03")]
    rows = _values(bt.shadow_rows(_shadow(labels, [10.0, 14.0, 15.0]), SESSION, set()))
    assert rows[ts(DAY, "14:02")] == 10.0  # štítek 14:01 chybí → poslední kumulativ
    assert rows[ts(DAY, "14:03")] == 14.0
    assert not any(math.isnan(value) for value in rows.values())


def test_dira_dvou_stitku_za_otevreneho_trhu_je_mezera_i_s_prvnim_po_ni() -> None:
    labels = [ts(DAY, "14:00"), ts(DAY, "14:03"), ts(DAY, "14:04")]
    rows = _values(bt.shadow_rows(_shadow(labels, [10.0, 30.0, 31.0]), SESSION, set()))
    assert rows[ts(DAY, "14:01")] == 10.0
    # štítky 14:01 a 14:02 chybí (restart) a 14:03 nese tisky až od restartu
    for label in ("14:01", "14:02", "14:03"):
        assert math.isnan(rows[ts(DAY, label) + pd.Timedelta(minutes=1)])
    assert rows[ts(DAY, "14:05")] == 31.0


def test_dira_v_denni_pauze_neni_mezera() -> None:
    # 21:00–22:00 UTC = 16:00–17:00 CDT, pauza Globexu: mezera se nezapisuje
    labels = [ts(DAY, "20:59"), ts(DAY, "21:30"), ts(DAY, "21:31")]
    rows = _values(bt.shadow_rows(_shadow(labels, [5.0, 5.0, 5.0]), SESSION, set()))
    assert not any(math.isnan(value) for value in rows.values())


def test_minuta_bez_trade_vetve_dela_mezeru_ze_dvou_stitku() -> None:
    labels = [ts(DAY, f"14:0{m}") for m in range(4)]
    gaps = {ts(DAY, "14:02")}  # flow.printed_volume NULL v minutě 14:02
    rows = _values(bt.shadow_rows(_shadow(labels, [1.0, 2.0, 3.0, 4.0]), SESSION, gaps))
    # obsah štítku L leží v [L − 1, L + 1) → zasažené štítky 14:01 a 14:02
    assert rows[ts(DAY, "14:01")] == 1.0
    assert math.isnan(rows[ts(DAY, "14:02")])
    assert math.isnan(rows[ts(DAY, "14:03")])
    assert rows[ts(DAY, "14:04")] == 4.0


def test_stitek_po_preskoku_kontroluje_i_minutu_pred_sebou() -> None:
    labels = [ts(DAY, "14:00"), ts(DAY, "14:02"), ts(DAY, "14:03")]
    gaps = {ts(DAY, "14:01")}  # obsah štítku 14:02 po přeskoku sahá do [14:00, 14:01)
    rows = _values(bt.shadow_rows(_shadow(labels, [10.0, 14.0, 15.0]), SESSION, gaps))
    assert math.isnan(rows[ts(DAY, "14:01")])  # štítek 14:00 (L + 1 v mezeře)
    assert rows[ts(DAY, "14:02")] == 10.0  # doplněný přeskok = poslední kumulativ
    assert math.isnan(rows[ts(DAY, "14:03")])  # štítek 14:02 (L − 1 po přeskoku)
    assert rows[ts(DAY, "14:04")] == 15.0


def test_posledni_stitek_seance_nepatri_do_dalsi() -> None:
    # 21:59 UTC = 16:59 CDT → rozhodnutí 22:00 UTC = 17:00 CDT = seance 15. 10.
    labels = [ts(DAY, "21:58"), ts(DAY, "21:59")]
    rows = bt.shadow_rows(_shadow(labels, [1.0, 2.0]), SESSION, set())
    assert list(rows["ts_min"]) == [ts(DAY, "21:59")]


# ── Řada symbolu z partic: hranice zdrojů ──────────────────────────────────


def _write(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path)


def _flow(rows: list[tuple[pd.Timestamp, float | None, str | None, float | None]]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_min": [row[0] for row in rows],
            "cum_delta": [row[1] for row in rows],
            "source": [row[2] for row in rows],
            "printed_volume": [row[3] for row in rows],
        }
    )


@pytest.fixture
def data_root(tmp_path: Path) -> Any:
    previous = bt.ROOT
    bt.set_data_root(str(tmp_path))
    bt._CUM.clear()
    yield tmp_path
    bt.set_data_root(previous)
    bt._CUM.clear()


def test_v6_seance_pred_21_9_mezera_od_nedele_vecer_stin(data_root: Path) -> None:
    friday, saturday, sunday, monday = (dt.date(2026, 9, d) for d in (18, 19, 20, 21))
    assert is_trading_session(friday) and is_trading_session(monday)
    assert not is_trading_session(saturday) and not is_trading_session(sunday)
    derived = data_root / "derived" / "ES"
    # Pátek 18. 9. stín má, ale je před SHADOW_FROM → mezera
    _write(
        derived / "cumdelta_dx" / "2026-09-18.parquet", _shadow([ts("2026-09-18", "14:00")], [7.0])
    )
    # Neděle 20. 9. 23:00 UTC = 18:00 CDT patří seanci pondělí 21. 9.
    sunday_evening = ts("2026-09-20", "23:00")
    assert trading_session_date(sunday_evening.to_pydatetime()) == monday
    minute = pd.Timedelta(minutes=1)
    _write(
        derived / "cumdelta_dx" / "2026-09-21.parquet",
        _shadow([sunday_evening, sunday_evening + minute], [3.0, 9.0]),
    )
    _write(
        derived / "flow" / "2026-09-18.parquet",
        _flow([(ts("2026-09-18", "14:01"), -50.0, "midpoint", 100.0)]),
    )
    series = bt.build_cum_series("ES", prints=True)
    values = _values(series)
    assert ts("2026-09-18", "14:01") not in values
    assert values[sunday_evening + minute] == 3.0
    assert values[sunday_evening + 2 * minute] == 9.0
    frame = pd.DataFrame({"ts_min": [ts("2026-09-18", "14:01"), sunday_evening + 2 * minute]})
    attached = bt.attach_cum_delta(frame, series, prints=True)
    assert math.isnan(attached["cum_delta"][0])  # pátek před 21. 9. = mezera
    assert attached["cum_delta"][1] == 9.0
    assert list(attached["cum_gap_before"]) == [False, True]  # hranice seance


def test_v6_bump_v_sobotu_patek_ze_stinu_pondeli_z_1a(data_root: Path) -> None:
    derived = data_root / "derived" / "ES"
    friday = "2026-10-16"
    _write(derived / "cumdelta_dx" / f"{friday}.parquet", _shadow([ts(friday, "14:00")], [4.0]))
    _write(
        derived / "flow" / f"{friday}.parquet",
        _flow([(ts(friday, "14:00"), -80.0, "midpoint", 10.0)]),
    )
    # Nasazení v6 v sobotu 17. 10. (trh zavřený): sobotní řádky už nesou aggressor
    _write(
        derived / "flow" / "2026-10-17.parquet",
        _flow([(ts("2026-10-17", "12:00"), 0.0, "aggressor", 0.0)]),
    )
    # Neděle 18. 10. 22:00 UTC = 17:00 CDT otevírá seanci pondělí 19. 10.
    _write(
        derived / "flow" / "2026-10-18.parquet",
        _flow(
            [
                (ts("2026-10-18", "22:00"), 0.0, "aggressor", 0.0),
                (ts("2026-10-18", "22:01"), 6.0, "aggressor", 5.0),
            ]
        ),
    )
    _write(
        derived / "flow" / "2026-10-19.parquet",
        _flow(
            [
                (ts("2026-10-19", "14:00"), 11.0, "aggressor", 5.0),
                (ts("2026-10-19", "14:01"), None, "aggressor", None),  # mezera 1A
                (ts("2026-10-19", "14:02"), 12.0, "aggressor", 5.0),
            ]
        ),
    )
    # Stín pondělí existuje, ale seance má 1A → stín se nečte
    _write(
        derived / "cumdelta_dx" / "2026-10-19.parquet", _shadow([ts("2026-10-19", "14:00")], [99.0])
    )
    series = bt.build_cum_series("ES", prints=True)
    frame = pd.DataFrame(
        {
            "ts_min": [
                ts(friday, "14:01"),
                ts("2026-10-18", "22:01"),
                ts("2026-10-19", "14:00"),
                ts("2026-10-19", "14:02"),
                ts("2026-10-19", "14:05"),
            ]
        }
    )
    attached = bt.attach_cum_delta(frame, series, prints=True)
    assert list(attached["cum_delta"])[:2] == [4.0, 6.0]  # pátek stín, neděle večer 1A
    assert attached["cum_delta"][2] == 11.0
    assert attached["cum_delta"][3] == 12.0
    assert attached["cum_delta"][4] == 12.0  # chybějící řádek = poslední hodnota seance
    # 14:01 (NULL 1A) leží mezi řádky 14:00 a 14:02 historie
    assert list(attached["cum_gap_before"]) == [False, True, False, True, False]


def test_v6_minuta_jineho_zdroje_v_seanci_1a_je_mezera(data_root: Path) -> None:
    derived = data_root / "derived" / "NQ"
    # Návrat pinem image uprostřed seance: midpoint řádek v seanci s 1A
    _write(
        derived / "flow" / "2026-11-03.parquet",
        _flow(
            [
                (ts("2026-11-03", "15:00"), 5.0, "aggressor", 1.0),
                (ts("2026-11-03", "15:01"), -400.0, "midpoint", 1.0),
            ]
        ),
    )
    values = _values(bt.build_cum_series("NQ", prints=True))
    assert values[ts("2026-11-03", "15:00")] == 5.0
    assert math.isnan(values[ts("2026-11-03", "15:01")])


def test_v6_printed_null_v_flow_dela_mezeru_ve_stinu(data_root: Path) -> None:
    derived = data_root / "derived" / "ES"
    labels = [ts(DAY, f"14:0{m}") for m in range(4)]
    _write(derived / "cumdelta_dx" / f"{DAY}.parquet", _shadow(labels, [1.0, 2.0, 3.0, 4.0]))
    _write(
        derived / "flow" / f"{DAY}.parquet",
        _flow([(label, -1.0, "midpoint", None if label == labels[2] else 3.0) for label in labels]),
    )
    values = _values(bt.build_cum_series("ES", prints=True))
    assert [values[label + pd.Timedelta(minutes=1)] for label in (labels[0], labels[3])] == [
        1.0,
        4.0,
    ]
    assert math.isnan(values[ts(DAY, "14:02")]) and math.isnan(values[ts(DAY, "14:03")])


def test_v6_printvol_null_dela_mezeru_ve_stinu(data_root: Path) -> None:
    derived = data_root / "derived" / "ES"
    labels = [ts(DAY, f"14:0{m}") for m in range(4)]
    _write(derived / "cumdelta_dx" / f"{DAY}.parquet", _shadow(labels, [1.0, 2.0, 3.0, 4.0]))
    _write(
        derived / "20261014" / "printvol" / f"{DAY}.parquet",
        pd.DataFrame(
            {
                "ts_min": [labels[0]],
                "strike": [6700.0],
                "right": ["C"],
                "volume_delta": [3.0],
                "printed": [None],
                "structured": [None],
            }
        ),
    )
    values = _values(bt.build_cum_series("ES", prints=True))
    assert math.isnan(values[ts(DAY, "14:01")])  # štítek 14:00
    assert values[ts(DAY, "14:02")] == 2.0


def test_v5_cte_jen_midpoint(data_root: Path) -> None:
    derived = data_root / "derived" / "ES"
    _write(
        derived / "flow" / f"{DAY}.parquet",
        _flow(
            [
                (ts(DAY, "14:00"), -10.0, None, None),  # partice před sloupcem source = midpoint
                (ts(DAY, "14:01"), -12.0, "midpoint", 3.0),
                (ts(DAY, "14:02"), 7.0, "aggressor", 3.0),
            ]
        ),
    )
    series = bt.build_cum_series("ES", prints=False)
    values = _values(series)
    assert values[ts(DAY, "14:00")] == -10.0 and values[ts(DAY, "14:01")] == -12.0
    assert math.isnan(values[ts(DAY, "14:02")])
    # v5 nezná hranici seance jako mezeru (živý engine v5 ji také nenese)
    frame = pd.DataFrame({"ts_min": [ts(DAY, "14:01"), ts("2026-10-15", "14:00")]})
    attached = bt.attach_cum_delta(frame, series, prints=False)
    assert list(attached["cum_gap_before"]) == [False, True]  # mezera 14:02 mezi řádky
    frame = pd.DataFrame({"ts_min": [ts(DAY, "14:00"), ts(DAY, "14:01")]})
    assert list(bt.attach_cum_delta(frame, series, prints=False)["cum_gap_before"]) == [
        False,
        False,
    ]


def test_bez_rady_je_cumdelta_mezera() -> None:
    frame = pd.DataFrame({"ts_min": [ts(DAY, "14:00"), ts(DAY, "14:01")]})
    attached = bt.attach_cum_delta(frame, None, prints=True)
    assert attached["cum_delta"].isna().all()
    assert list(attached["cum_gap_before"]) == [False, False]


def test_build_minutes_mezera_je_none_a_nese_ji_dalsi_radek(data_root: Path) -> None:
    derived = data_root / "derived" / "ES"
    bars = [ts(DAY, f"14:0{m}") for m in range(4)]  # bar N−1 → rozhodnutí v N
    _write(
        derived / "bars" / f"{DAY}.parquet",
        pd.DataFrame({"ts_min": bars, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}),
    )
    decisions = [bar + pd.Timedelta(minutes=1) for bar in bars]  # 14:01–14:04
    # Minuta 14:03 nemá levels → do historie nejde, její mezera přejde na 14:04
    _write(
        derived / "20261014" / "levels" / f"{DAY}.parquet",
        pd.DataFrame(
            {
                "ts_min": [decisions[0], decisions[1], decisions[3]],
                "flip": 100.0,
                "call_wall": 110.0,
                "put_wall": 90.0,
                "total_gex": 1.0,
            }
        ),
    )
    _write(
        derived / "flow" / f"{DAY}.parquet",
        _flow(
            [
                (decisions[0], 5.0, "aggressor", 1.0),
                (decisions[1], None, "aggressor", None),
                (decisions[2], None, "aggressor", None),
                (decisions[3], 6.0, "aggressor", 1.0),
            ]
        ),
    )
    minutes = bt.build_minutes("ES", "20261014", None, prints=True)
    assert [m.ts.strftime("%H:%M") for m in minutes] == ["14:01", "14:02", "14:04"]
    assert [m.cum_delta for m in minutes] == [5.0, None, 6.0]
    assert [m.cum_gap_before for m in minutes] == [False, False, True]


def test_v6_shadow_only_cte_stin_i_v_seanci_s_1a(data_root: Path) -> None:
    derived = data_root / "derived" / "ES"
    monday = "2026-11-02"
    _write(derived / "cumdelta_dx" / f"{monday}.parquet", _shadow([ts(monday, "15:00")], [3.0]))
    _write(
        derived / "flow" / f"{monday}.parquet",
        _flow(
            [
                (ts(monday, "15:00"), 8.0, "aggressor", 1.0),
                (ts(monday, "15:01"), 9.0, "aggressor", 1.0),
            ]
        ),
    )
    assert _values(bt.build_cum_series("ES", prints=True))[ts(monday, "15:01")] == 9.0
    shadow = _values(bt.build_cum_series("ES", prints=True, shadow_only=True))
    assert shadow == {ts(monday, "15:01"): 3.0}  # štítek 15:00 → minuta 15:01


def test_v6_nasazeni_ve_stredu_v_pauze_seance_d_ze_stinu(data_root: Path) -> None:
    wednesday, thursday = dt.date(2026, 10, 28), dt.date(2026, 10, 29)
    assert is_trading_session(wednesday) and is_trading_session(thursday)
    derived = data_root / "derived" / "ES"
    day = wednesday.isoformat()
    _write(
        derived / "cumdelta_dx" / f"{day}.parquet",
        _shadow([ts(day, "14:00"), ts(day, "14:01")], [4.0, 6.0]),
    )
    # RTH seance D z midpointu, nasazení v6 v pauze 21:30 UTC = 16:30 CDT (ještě seance D)
    pause = ts(day, "21:30")
    assert trading_session_date(pause.to_pydatetime()) == wednesday
    _write(
        derived / "flow" / f"{day}.parquet",
        _flow(
            [
                (ts(day, "14:00"), -70.0, "midpoint", 2.0),
                (ts(day, "14:01"), -72.0, "midpoint", 2.0),
                (pause, 0.0, "aggressor", 0.0),
                # 22:00 UTC = 17:00 CDT otevírá seanci čtvrtka
                (ts(day, "22:00"), 1.0, "aggressor", 1.0),
            ]
        ),
    )
    values = _values(bt.build_cum_series("ES", prints=True))
    assert values[ts(day, "14:01")] == 4.0  # seance D ze stínu, RTH není mezera
    assert values[ts(day, "14:02")] == 6.0
    assert values[ts(day, "22:00")] == 1.0  # seance D + 1 z 1A
    assert trading_session_date(ts(day, "22:00").to_pydatetime()) == thursday


def test_v6_printvol_null_pod_tisky_mezeru_neznaci(data_root: Path) -> None:
    derived = data_root / "derived" / "ES"
    labels = [ts(DAY, f"14:0{m}") for m in range(4)]
    _write(derived / "cumdelta_dx" / f"{DAY}.parquet", _shadow(labels, [1.0, 2.0, 3.0, 4.0]))
    # Engine v6: mezera 1A je jen `cum_delta` NULL (14:02); printvol NULL ve 14:00 nese
    # až bar s přírůstkem po mezeře v dřívější minutě, mezeru téže minuty neznačí
    _write(
        derived / "flow" / f"{DAY}.parquet",
        _flow([(label, None if label == labels[2] else 1.0, "aggressor", 1.0) for label in labels]),
    )
    _write(
        derived / "20261014" / "printvol" / f"{DAY}.parquet",
        pd.DataFrame(
            {
                "ts_min": [labels[0]],
                "strike": [6700.0],
                "right": ["C"],
                "volume_delta": [3.0],
                "printed": [None],
                "structured": [None],
            }
        ),
    )
    values = _values(bt.build_cum_series("ES", prints=True, shadow_only=True))
    assert values[ts(DAY, "14:01")] == 1.0
    assert math.isnan(values[ts(DAY, "14:02")]) and math.isnan(values[ts(DAY, "14:03")])
    assert values[ts(DAY, "14:04")] == 4.0


def test_v5_chybejici_radek_je_mezera_bez_dosazeni(data_root: Path) -> None:
    derived = data_root / "derived" / "ES"
    _write(
        derived / "flow" / f"{DAY}.parquet",
        _flow([(ts(DAY, "14:00"), -10.0, "midpoint", 3.0)]),
    )
    frame = pd.DataFrame({"ts_min": [ts(DAY, "14:00"), ts(DAY, "14:01")]})
    v5 = bt.attach_cum_delta(frame, bt.build_cum_series("ES", prints=False), prints=False)
    assert v5["cum_delta"][0] == -10.0
    assert math.isnan(v5["cum_delta"][1])  # v5 jako před #1590: jen řádek téže minuty
