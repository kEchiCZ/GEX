"""Kontrola seance pod v6 (#1403, #1602) — `scripts/check_session_v6_1403.py`.

Skript žije ve `scripts/`, načítá se přes importlib (vzor test_measure_fa_ratio_1612).
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_session_v6_1403.py"
UTC = dt.UTC
WINTER = dt.date(2026, 11, 2)  # pondělí po konci letního času v USA
SUMMER = dt.date(2026, 10, 30)  # pátek, USA ještě v letním čase
TS = pa.timestamp("us", tz="UTC")


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("check_session_v6_1403", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _at(day: dt.date, hour: int, minute: int) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def test_okno_bez_casu_je_rth_seance_v_zime_i_v_lete() -> None:
    module = _load()
    assert module.window(WINTER, None, None) == (_at(WINTER, 14, 30), _at(WINTER, 21, 0))
    assert module.window(SUMMER, None, None) == (_at(SUMMER, 13, 30), _at(SUMMER, 20, 0))


def test_okno_z_casu_lezi_ve_24_h_od_otevreni_seance() -> None:
    module = _load()
    prev = WINTER - dt.timedelta(days=1)
    # Celá seance v zimě: od otevření 23:00 D−1 do konce obchodu 22:00 D
    assert module.window(WINTER, dt.time(23, 0), dt.time(22, 0)) == (
        _at(prev, 23, 0),
        _at(WINTER, 22, 0),
    )
    # Denní pauza: konec 23:00 je otevření další seance, ne začátek této
    assert module.window(WINTER, dt.time(22, 0), dt.time(23, 0)) == (
        _at(WINTER, 22, 0),
        _at(WINTER, 23, 0),
    )
    # V létě otevírá Globex ve 22:00 UTC
    assert module.window(SUMMER, dt.time(22, 0), None)[0] == _at(
        SUMMER - dt.timedelta(days=1), 22, 0
    )


def test_prazdne_okno_je_chyba() -> None:
    module = _load()
    with pytest.raises(ValueError, match="prázdné okno"):
        module.window(WINTER, dt.time(15, 0), dt.time(14, 30))


@pytest.mark.parametrize("day", [dt.date(2026, 10, 31), dt.date(2026, 11, 1)])
def test_sobota_a_nedele_nejsou_seance(day: dt.date) -> None:
    module = _load()
    # Nedělní Globex od 23:00 UTC patří pondělní seanci (WINTER výše), ne neděli
    with pytest.raises(ValueError, match="není obchodní den"):
        module.window(day, None, None)


def test_pauza_nema_ocekavane_minuty() -> None:
    module = _load()
    lo, hi = module.window(WINTER, dt.time(22, 0), dt.time(23, 0))
    assert module.open_minutes(lo, hi) == []
    assert len(module.open_minutes(*module.window(WINTER, None, None))) == 390


def _row(
    minute: int, volume: float | None, strike: float = 6800.0, age: float | None = 0.0
) -> dict[str, Any]:
    return {
        "ts_min": _at(WINTER, 15, minute),
        "strike": strike,
        "right": "C",
        "volume": volume,
        "stale_age": age,
    }


def test_prirustky_proti_maximu_seance_a_minuta_po_mezere() -> None:
    module = _load()
    # Řada z #1602: pokles a návrat k maximu dají 0, započte se jen objem nad maximem
    rows = [_row(m, v) for m, v in enumerate([1000.0, 800.0, 760.0, 1000.0, 1010.0])]
    increments, declines = module.increments_by_minute(rows, 900.0)
    assert increments == {_at(WINTER, 15, 4): 10.0}
    assert declines == 2
    # NULL v minutě 1 = mezera: minuta 2 jen založí stav, minuta 3 měří
    other = [_row(m, 5.0, strike=6900.0) for m in range(4)]
    gap = [_row(0, 100.0), _row(1, None), _row(2, 130.0), _row(3, 150.0)]
    increments, _ = module.increments_by_minute(gap + other, 900.0)
    assert increments == {_at(WINTER, 15, 3): 20.0}


def test_zmrzla_kotace_je_mezera_jako_v_enginu() -> None:
    module = _load()
    # Engine kotaci starší než quote_max_age_s do trackeru nepustí (runtime, #306):
    # minuta 1 je mezera, minuta 2 jen založí stav, měří až minuta 3
    other = [_row(m, 5.0, strike=6900.0) for m in range(4)]
    rows = [_row(0, 100.0), _row(1, 120.0, age=901.0), _row(2, 130.0), _row(3, 150.0)]
    increments, _ = module.increments_by_minute(rows + other, 900.0)
    assert increments == {_at(WINTER, 15, 3): 20.0}
    # Stáří NULL (partice před #306) kotaci nevyřadí
    assert module.fresh_volume(_row(0, 7.0, age=None), 900.0) == 7.0


def _write(path: Path, columns: dict[str, pa.Array]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table(columns), path)


def _snapshot(path: Path, ts: list[dt.datetime], volume: list[float], age: list[float]) -> None:
    _write(
        path,
        {
            "ts_min": pa.array(ts, TS),
            "strike": pa.array([6800.0] * len(ts)),
            "right": pa.array(["C"] * len(ts)),
            "volume": pa.array(volume, pa.float64()),
            "stale_age": pa.array(age, pa.float64()),
        },
    )


def test_kontrola_symbolu_nad_particemi(tmp_path: Path) -> None:
    module = _load()
    expiry = "20261102"
    day = WINTER.isoformat()
    minutes = [_at(WINTER, 15, m) for m in range(4)]
    derived = tmp_path / "derived" / "ES"
    _write(derived / expiry / "levels" / f"{day}.parquet", {"ts_min": pa.array(minutes[:3], TS)})
    # Partice flow ve starém tvaru bez sloupce `source` (před #615): zdroj None, ne pád
    _write(
        derived / "flow" / f"{day}.parquet",
        {
            "ts_min": pa.array(minutes, TS),
            "flow_delta": pa.array([10.0, None, -250.0, 4.0], pa.float64()),
            "cum_delta": pa.array([10.0, None, -240.0, -236.0], pa.float64()),
        },
    )
    # Před oknem (otevření seance) založí stav; v okně přírůstek 5, pokles, zmrzlá kotace
    _snapshot(
        tmp_path / "snapshots" / "ES" / expiry / f"{(WINTER - dt.timedelta(days=1))}.parquet",
        [_at(WINTER - dt.timedelta(days=1), 23, 0)],
        [100.0],
        [0.0],
    )
    _snapshot(
        tmp_path / "snapshots" / "ES" / expiry / f"{day}.parquet",
        minutes[:3],
        [105.0, 104.0, 120.0],
        [0.0, 0.0, 901.0],
    )
    _write(
        derived / expiry / "printvol" / f"{day}.parquet",
        {"ts_min": pa.array(minutes[:1], TS), "volume_delta": pa.array([5.0], pa.float64())},
    )
    lines = module.check_symbol(
        tmp_path, "ES", expiry, WINTER, minutes[0], minutes[-1] + dt.timedelta(minutes=1), 900.0
    )
    assert lines[0] == "ES levels: 3/4 minut, chybí 1 ['15:03']"
    assert "NULL cum_delta 1 ['15:01']" in lines[1] and "{None: 4}" in lines[1]
    assert "max |flow_delta| -250 v 15:02" in lines[1]
    assert lines[2] == "ES snapshots 20261102: minut s volume 2/4, chybí 2 ['15:02', '15:03']"
    assert "Σ printvol 5 × Σ přírůstků od maxima 5 (poklesů feedu v seanci 1)" in lines[3]
    assert lines[3].endswith("minut s rozdílem 0 []")
