"""Sken skoků ≥ 60 bp v archivu minutových barů ES a NQ (E-0.2, #1390).

Skript `scripts/scan_bar_jumps.py` se načítá přes importlib; práh skoku
a týden rollu bere z `scripts/measure_hard_data_1381.py`, který se proto
registruje do `sys.modules` pod svým jménem (bez úpravy `sys.path`).
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
from sqlalchemy import create_engine, insert

from gexlens_engine.storage.sentiment import ensure_sentiment_schema, news_events

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
MINUTE = dt.timedelta(minutes=1)
#: Úterý 13. 10. 2026 10:00 UTC — mimo týden rollu, trh otevřený
TUESDAY = dt.datetime(2026, 10, 13, 10, 0, tzinfo=dt.UTC)


def _load_script(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def scan() -> Any:
    _load_script("measure_hard_data_1381")
    return _load_script("scan_bar_jumps")


def _calm(scan: Any, start: dt.datetime, n: int, level: float, source: str = "ibkr") -> list[Any]:
    """Klidný trh: close kmitá o čtvrt bodu (~0,3 bp)."""
    return [scan.Bar(start + k * MINUTE, level + 0.25 * (k % 2), source) for k in range(n)]


def _volatile(scan: Any, start: dt.datetime, n: int, level: float) -> list[Any]:
    """Rozkolísaný trh: close skáče o ±0,2 % každou minutu (~20 bp)."""
    return [
        scan.Bar(start + k * MINUTE, level * (1.002 if k % 2 else 1.0), "NULL") for k in range(n)
    ]


def _shifted(bars: list[Any], factor: float, source: str) -> list[Any]:
    return [type(b)(b.ts, b.close * factor, source) for b in bars]


def test_jediny_skok_v_seanci_je_skutecny_pohyb(scan: Any) -> None:
    bars = _calm(scan, TUESDAY, 40, 7000.0)
    bars += _calm(scan, TUESDAY + 40 * MINUTE, 20, 7056.0)  # +80 bp
    jumps = scan.find_jumps("ES", bars)
    scan.mark_wrong_contract(jumps, bars)
    assert [(round(j.bp), j.boundary, j.gap_minutes, j.cause) for j in jumps] == [
        (80, False, 1, "")
    ]


def test_vraceny_blok_v_klidnem_trhu_je_spatny_kontrakt(scan: Any) -> None:
    # 9. 9. 2026: blok ibkr_hist z Z6 o +87 bp mezi měřenými bary U6 (#1299)
    before = _calm(scan, TUESDAY, 40, 7000.0)
    block = _shifted(_calm(scan, TUESDAY + 40 * MINUTE, 30, 7000.0), 1.0087, "ibkr_hist")
    after = _calm(scan, TUESDAY + 70 * MINUTE, 20, 7001.0)
    bars = before + block + after
    jumps = scan.find_jumps("ES", bars)
    scan.mark_wrong_contract(jumps, bars)
    assert [j.cause for j in jumps] == [scan.CAUSE_WRONG_CONTRACT] * 2
    assert jumps[0].partner == block[0].ts + 30 * MINUTE
    assert jumps[1].partner == block[0].ts
    assert (jumps[0].prev.source, jumps[0].bar.source) == ("ibkr", "ibkr_hist")


def test_vraceny_pohyb_v_rozkolisanem_trhu_neni_spatny_kontrakt(scan: Any) -> None:
    # Cla 7. 4. 2025: +108 bp a za 9 min −97 bp — skutečný pohyb, trh kolem kmitá
    before = _volatile(scan, TUESDAY, 40, 7000.0)
    block = _shifted(_volatile(scan, TUESDAY + 40 * MINUTE, 10, 7000.0), 1.009, "NULL")
    after = _volatile(scan, TUESDAY + 50 * MINUTE, 20, 7000.0)
    bars = before + block + after
    jumps = scan.find_jumps("ES", bars)
    scan.mark_wrong_contract(jumps, bars)
    assert len(jumps) == 2
    assert all(j.cause == "" and j.partner is None for j in jumps)


def test_vraceny_skok_jine_velikosti_neni_par(scan: Any) -> None:
    before = _calm(scan, TUESDAY, 40, 7000.0)
    block = _calm(scan, TUESDAY + 40 * MINUTE, 10, 7070.0)  # +100 bp
    after = _calm(scan, TUESDAY + 50 * MINUTE, 10, 7028.0)  # −59 bp…
    after += _calm(scan, TUESDAY + 60 * MINUTE, 10, 6985.0)  # …a −61 bp: zbytek > 10 %
    bars = before + block + after
    jumps = scan.find_jumps("ES", bars)
    scan.mark_wrong_contract(jumps, bars)
    assert [j.partner for j in jumps] == [None, None]


def test_roll_na_hranici_seance_v_tydnu_rollu(scan: Any, tmp_path: Path) -> None:
    # 16. 9. 2026 20:59 UTC konec seance 16. 9.; 22:00 UTC otevření seance 17. 9.
    # (týden rollu září 2026) — engine přešel z U6 na Z6 o +89 bp
    close = dt.datetime(2026, 9, 16, 20, 30, tzinfo=dt.UTC)
    bars = _calm(scan, close, 30, 6600.0) + _calm(
        scan, dt.datetime(2026, 9, 16, 22, 0, tzinfo=dt.UTC), 10, 6659.0
    )
    jumps = scan.find_jumps("ES", bars)
    scan.mark_wrong_contract(jumps, bars)
    (jump,) = jumps
    assert jump.boundary and jump.session == dt.date(2026, 9, 17)
    assert jump.gap_minutes == 61 and jump.missing_open_minutes == 0
    scan.classify(jumps, _news_db(tmp_path, []))
    assert jump.cause == scan.CAUSE_ROLL


def test_zaporny_skok_v_tydnu_rollu_neni_roll(scan: Any, tmp_path: Path) -> None:
    # Další kontrakt je v contangu výš — pokles o 102 bp při nedělním otevření
    # v týdnu rollu je pohyb trhu, ne přechod na další kontrakt
    friday = dt.datetime(2026, 9, 11, 20, 30, tzinfo=dt.UTC)
    sunday_open = dt.datetime(2026, 9, 13, 22, 0, tzinfo=dt.UTC)
    bars = _calm(scan, friday, 30, 24000.0) + _calm(scan, sunday_open, 10, 23755.0)
    jumps = scan.find_jumps("NQ", bars)
    scan.classify(jumps, _news_db(tmp_path, []))
    assert [(j.session, j.boundary, j.cause) for j in jumps] == [
        (dt.date(2026, 9, 14), True, scan.CAUSE_REAL)
    ]


def test_vikend_neni_dira_a_obchodni_den_je_pondeli(scan: Any) -> None:
    friday = dt.datetime(2026, 10, 9, 20, 30, tzinfo=dt.UTC)
    sunday_open = dt.datetime(2026, 10, 11, 22, 0, tzinfo=dt.UTC)
    bars = _calm(scan, friday, 30, 7000.0) + _calm(scan, sunday_open, 10, 7049.0)
    (jump,) = scan.find_jumps("ES", bars)
    # Neděle 22:00 UTC patří obchodnímu dni pondělí; mezera je jen zavřený trh
    assert jump.session == dt.date(2026, 10, 12)
    assert jump.boundary and jump.missing_open_minutes == 0
    assert scan._gap(jump) == "2941 (zavřeno)"


def test_dlouha_dira_za_otevreneho_trhu_je_pricina(scan: Any, tmp_path: Path) -> None:
    # 14.–16. 7. 2026 (#1300): pohyb přes díru nejde časově určit ani přiřadit zprávě
    bars = _calm(scan, TUESDAY, 10, 7000.0) + _calm(scan, TUESDAY + 40 * MINUTE, 10, 7049.0)
    jumps = scan.find_jumps("ES", bars)
    conn = _news_db(tmp_path, [(TUESDAY + 30 * MINUTE, "scheduled", 3, "CPI m/m")])
    scan.classify(jumps, conn)
    (jump,) = jumps
    assert not jump.boundary and jump.missing_open_minutes == 30
    assert scan._gap(jump) == "31 (díra 30 min)"
    assert jump.cause == scan.CAUSE_HOLE and jump.news == "10:30 st. 0: CPI m/m"


def test_kratka_dira_nezakryje_pricinu(scan: Any, tmp_path: Path) -> None:
    bars = _calm(scan, TUESDAY, 10, 7000.0) + _calm(scan, TUESDAY + 20 * MINUTE, 10, 7049.0)
    jumps = scan.find_jumps("ES", bars)
    scan.classify(jumps, _news_db(tmp_path, []))
    assert scan._gap(jumps[0]) == "11 (díra 10 min)"
    assert jumps[0].cause == scan.CAUSE_REAL


def _news_db(tmp_path: Path, events: list[tuple[dt.datetime, str, int, str]]) -> Any:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    with engine.begin() as conn:
        for index, (ts, kind, importance, title) in enumerate(events):
            conn.execute(
                insert(news_events).values(
                    ts_event=ts,
                    ts_ingested=ts,
                    source="test",
                    kind=kind,
                    importance=importance,
                    title=title,
                    symbols=[],
                    market_closed=False,
                    dedup_hash=f"scan-{index}",
                    raw={},
                )
            )
    return engine.connect()


def test_vyznamna_zprava_v_okne_je_pricina(scan: Any, tmp_path: Path) -> None:
    jump_ts = TUESDAY + 40 * MINUTE
    bars = _calm(scan, TUESDAY, 40, 7000.0) + _calm(scan, jump_ts, 10, 6944.0)  # −80 bp
    jumps = scan.find_jumps("ES", bars)
    conn = _news_db(
        tmp_path,
        [
            (jump_ts - 25 * MINUTE, "scheduled", 3, "mimo okno"),
            (jump_ts - 5 * MINUTE, "scheduled", 3, "CPI m/m"),
            (jump_ts - MINUTE, "headline", 1, "šum s importance 1"),
        ],
    )
    scan.classify(jumps, conn)
    assert jumps[0].cause == scan.CAUSE_NEWS
    assert jumps[0].news == "10:35 st. 0: CPI m/m"


def test_bez_vyznamne_zpravy_je_skutecny_pohyb(scan: Any, tmp_path: Path) -> None:
    jump_ts = TUESDAY + 40 * MINUTE
    bars = _calm(scan, TUESDAY, 40, 7000.0) + _calm(scan, jump_ts, 10, 6944.0)
    jumps = scan.find_jumps("ES", bars)
    scan.classify(jumps, _news_db(tmp_path, [(jump_ts, "headline", 1, "šum")]))
    assert jumps[0].cause == scan.CAUSE_REAL and jumps[0].news == ""


def _partition(data: Path, symbol: str, bars: list[tuple[dt.datetime, float, str | None]]) -> None:
    by_day: dict[dt.date, list[tuple[dt.datetime, float, str | None]]] = {}
    for bar in bars:
        by_day.setdefault(bar[0].date(), []).append(bar)
    for day, rows in by_day.items():
        path = data / "derived" / symbol / "bars" / f"{day.isoformat()}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        columns: dict[str, Any] = {
            "ts_min": pa.array([r[0] for r in rows], type=pa.timestamp("us", tz="UTC")),
            "close": [r[1] for r in rows],
        }
        if any(r[2] is not None for r in rows):
            columns["source"] = [r[2] for r in rows]
        pq.write_table(pa.table(columns), path)


def test_cli_cte_partice_a_vypise_report(
    scan: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = tmp_path / "data"
    # Starší partice bez sloupce source (NULL = živá cesta), 12. 10. klidný den
    monday = dt.datetime(2026, 10, 12, 10, 0, tzinfo=dt.UTC)
    _partition(data, "ES", [(monday + k * MINUTE, 7000.0, None) for k in range(5)])
    # 13. 10.: blok jiného kontraktu (+87 bp, ibkr_hist) mezi měřenými bary
    rows: list[tuple[dt.datetime, float, str | None]] = [
        (TUESDAY + k * MINUTE, 7000.0 + 0.25 * (k % 2), "ibkr") for k in range(40)
    ]
    rows += [(TUESDAY + k * MINUTE, 7061.0, "ibkr_hist") for k in range(40, 50)]
    rows += [(TUESDAY + k * MINUTE, 7000.5, "ibkr") for k in range(50, 60)]
    _partition(data, "ES", rows)
    url = f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}"
    _news_db(tmp_path, []).close()

    assert scan.main(["--db", url, "--data", str(data), "--symbols", "ES"]) == 0
    out = capsys.readouterr().out
    assert "- ES: 65 minut" in out
    assert "| 2026-10-13 | ES | 2026-10-13 10:40 | +87 | 1 | ibkr → ibkr_hist |" in out
    assert "špatný kontrakt (pár 10:50)" in out
    assert "| špatný kontrakt | 2 |" in out


def test_load_bars_mereny_bar_prebije_doplneny(scan: Any, tmp_path: Path) -> None:
    data = tmp_path / "data"
    # Táž minuta ve dvou particích (UTC den vs. starý zápis do sousední):
    # živá cesta přebije ibkr_hist bez ohledu na pořadí souborů
    ts = dt.datetime(2026, 10, 13, 0, 0, tzinfo=dt.UTC)
    _partition(data, "ES", [(ts, 7000.0, "ibkr")])
    stale = data / "derived" / "ES" / "bars" / "2026-10-14.parquet"
    pq.write_table(
        pa.table(
            {
                "ts_min": pa.array([ts], type=pa.timestamp("us", tz="UTC")),
                "close": [7061.0],
                "source": ["ibkr_hist"],
            }
        ),
        stale,
    )
    (bar,) = scan.load_bars(data / "derived" / "ES" / "bars")
    assert (bar.close, bar.source) == (7000.0, "ibkr")
