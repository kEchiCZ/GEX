"""Testy skriptu přepočtu výsledků setupů z barů (#1320) nad fixture DB a particemi.

Skript žije ve `scripts/`, načítá se přes importlib (vzor test_measure_sentiment_episodes).
Každý případ má vlastní den (a tedy vlastní partici), ať se cesty cen nepletou.
"""

from __future__ import annotations

import csv
import datetime as dt
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine

from gexlens_engine.config import Settings
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.storage.parquet_store import (
    BAR_SOURCE_HISTORICAL,
    BAR_SOURCE_RECONSTRUCTED,
    SnapshotWriter,
)
from gexlens_engine.storage.setups_store import SetupsRepository

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "recompute_setup_outcomes.py"
MINUTE = dt.timedelta(minutes=1)


@pytest.fixture(scope="module")
def mod() -> Any:
    spec = importlib.util.spec_from_file_location("recompute_setup_outcomes", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def utc(day: str, hhmm: str) -> dt.datetime:
    return dt.datetime.fromisoformat(f"{day}T{hhmm}:00+00:00")


def series(
    start: dt.datetime,
    end: dt.datetime,
    price: float,
    *,
    source: str | None = None,
    overrides: dict[dt.datetime, tuple[float, float, float]] | None = None,
) -> list[Bar]:
    """Minutové bary [start, end] kolem `price` (±2 b); `overrides` = (high, low, close)."""
    bars: list[Bar] = []
    ts = start
    while ts <= end:
        high, low, close = (overrides or {}).get(ts, (price + 2, price - 2, price))
        bars.append(
            Bar(ts=ts, open=close, high=high, low=low, close=close, volume=10.0, source=source)
        )
        ts += MINUTE
    return bars


class Fixture:
    """sqlite DB se setupy + partice barů; id případů v `ids`."""

    def __init__(self, tmp_path: Path) -> None:
        self.url = f"sqlite+pysqlite:///{tmp_path / 'setups.sqlite'}"
        self.repository = SetupsRepository(create_engine(self.url))
        self.repository.ensure_schema()
        self.data = tmp_path / "data"
        self.writer = SnapshotWriter(Settings(data_dir=self.data))
        self.ids: dict[str, int] = {}

    def setup(
        self,
        name: str,
        *,
        symbol: str,
        expiry: str,
        created: dt.datetime,
        entry: float,
        target: float,
        stop: float,
        status: str,
        closed: dt.datetime,
        outcome_r: float,
    ) -> None:
        setup_id = self.repository.create(
            symbol=symbol,
            expiry=expiry,
            template="trend_continuation",
            direction="long",
            created_ts=created,
            entry=entry,
            target=target,
            stop=stop,
            confidence=55,
            reason="test",
            context={"tradeable": True, "contracts": 2, "gex_regime": "positive"},
        )
        self.repository.close(
            setup_id, status=status, closed_ts=closed, outcome_r=outcome_r, mfe=1.0, mae=2.0
        )
        self.ids[name] = setup_id


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    f = Fixture(tmp_path)
    # A — mezera 3. 9. (NQ 1003): v DB cíl v 15:30, bary mají stop ve 14:56
    day = "2026-09-03"
    f.setup(
        "gap_stop",
        symbol="NQ",
        expiry="20260903",
        created=utc(day, "14:15"),
        entry=29300.0,
        target=29450.0,
        stop=29250.0,
        status="closed_target",
        closed=utc(day, "15:30"),
        outcome_r=3.0,
    )
    f.writer.write_bars_by_day(
        "NQ",
        series(
            utc(day, "14:13"),
            utc(day, "19:59"),
            29300.0,
            overrides={utc(day, "14:56"): (29310.0, 29240.0, 29290.0)},
        ),
    )
    # B — timeout po restartu ve 20:50 za pozdní cenu; close 19:59 = 29320 → +0,4 R
    day = "2026-09-02"
    f.setup(
        "late_timeout",
        symbol="NQ",
        expiry="20260902",
        created=utc(day, "18:00"),
        entry=29300.0,
        target=29450.0,
        stop=29250.0,
        status="closed_timeout",
        closed=utc(day, "20:50"),
        outcome_r=-0.2,
    )
    f.writer.write_bars_by_day(
        "NQ",
        series(
            utc(day, "17:58"),
            utc(day, "20:50"),
            29300.0,
            overrides={utc(day, "19:59"): (29322.0, 29318.0, 29320.0)},
        ),
    )
    # C — beze změny: stop v DB i v barech ve 10:30
    day = "2026-09-01"
    f.setup(
        "same",
        symbol="NQ",
        expiry="20260901",
        created=utc(day, "10:00"),
        entry=29300.0,
        target=29450.0,
        stop=29250.0,
        status="closed_stop",
        closed=utc(day, "10:30"),
        outcome_r=-1.0,
    )
    f.writer.write_bars_by_day(
        "NQ",
        series(
            utc(day, "09:58"),
            utc(day, "11:00"),
            29300.0,
            overrides={utc(day, "10:30"): (29300.0, 29245.0, 29260.0)},
        ),
    )
    # D — vznik po settle (#1324)
    f.setup(
        "after_settle",
        symbol="NQ",
        expiry="20260903",
        created=utc("2026-09-03", "20:30"),
        entry=29500.0,
        target=29600.0,
        stop=29450.0,
        status="closed_timeout",
        closed=utc("2026-09-03", "20:31"),
        outcome_r=0.1,
    )
    # E — díra 10:05–10:06 před zásahem stopu → neověřitelný
    day = "2026-08-31"
    f.setup(
        "hole",
        symbol="ES",
        expiry="20260831",
        created=utc(day, "10:00"),
        entry=7500.0,
        target=7530.0,
        stop=7490.0,
        status="closed_target",
        closed=utc(day, "10:20"),
        outcome_r=3.0,
    )
    bars = series(
        utc(day, "09:58"),
        utc(day, "10:30"),
        7500.0,
        overrides={utc(day, "10:20"): (7531.0, 7499.0, 7530.0)},
    )
    f.writer.write_bars_by_day(
        "ES", [b for b in bars if b.ts not in (utc(day, "10:05"), utc(day, "10:06"))]
    )
    # F — různé zdroje: IBKR do 10:10, pak rekonstrukce z tasty se stopem → neověřitelný
    day = "2026-08-28"
    f.setup(
        "mixed",
        symbol="ES",
        expiry="20260828",
        created=utc(day, "10:00"),
        entry=7500.0,
        target=7530.0,
        stop=7490.0,
        status="closed_target",
        closed=utc(day, "10:40"),
        outcome_r=3.0,
    )
    f.writer.write_bars_by_day("ES", series(utc(day, "09:58"), utc(day, "10:10"), 7500.0))
    f.writer.write_bars_by_day(
        "ES",
        series(
            utc(day, "10:11"),
            utc(day, "10:40"),
            7500.0,
            source=BAR_SOURCE_RECONSTRUCTED,
            overrides={utc(day, "10:15"): (7501.0, 7489.0, 7495.0)},
        ),
    )
    # G — kvartální datum expirace (18. 9.: týdenní EW3/QN3 vs. SOQ) → neověřitelný
    f.setup(
        "quarterly",
        symbol="ES",
        expiry="20260918",
        created=utc("2026-09-18", "09:00"),
        entry=7700.0,
        target=7730.0,
        stop=7690.0,
        status="closed_timeout",
        closed=utc("2026-09-18", "20:02"),
        outcome_r=0.5,
    )
    # H — cesta přeskočí na bary jiného kontraktu (+300 b, #1232) → neověřitelný
    day = "2026-08-27"
    f.setup(
        "contract",
        symbol="ES",
        expiry="20260827",
        created=utc(day, "10:00"),
        entry=7500.0,
        target=7530.0,
        stop=7490.0,
        status="closed_stop",
        closed=utc(day, "10:05"),
        outcome_r=-1.0,
    )
    f.writer.write_bars_by_day("ES", series(utc(day, "09:58"), utc(day, "10:02"), 7500.0))
    f.writer.write_bars_by_day(
        "ES", series(utc(day, "10:03"), utc(day, "10:30"), 7800.0, source=BAR_SOURCE_HISTORICAL)
    )
    # I — výsledek sedí, engine zásah zjistil pozdě (spot po výpadku): jen čas
    day = "2026-08-26"
    f.setup(
        "late_close",
        symbol="NQ",
        expiry="20260826",
        created=utc(day, "12:53"),
        entry=29300.0,
        target=29450.0,
        stop=29250.0,
        status="closed_stop",
        closed=utc(day, "13:33"),
        outcome_r=-1.0,
    )
    f.writer.write_bars_by_day(
        "NQ",
        series(
            utc(day, "12:51"),
            utc(day, "13:40"),
            29300.0,
            source=BAR_SOURCE_HISTORICAL,
            overrides={utc(day, "13:30"): (29300.0, 29240.0, 29260.0)},
        ),
    )
    # J — celá cesta z rekonstrukce tasty (jiný zdroj než IBKR) → neověřitelný
    day = "2026-08-25"
    f.setup(
        "tasty_only",
        symbol="ES",
        expiry="20260825",
        created=utc(day, "10:00"),
        entry=7500.0,
        target=7530.0,
        stop=7490.0,
        status="closed_target",
        closed=utc(day, "10:40"),
        outcome_r=3.0,
    )
    f.writer.write_bars_by_day(
        "ES",
        series(
            utc(day, "09:58"),
            utc(day, "10:40"),
            7500.0,
            source=BAR_SOURCE_RECONSTRUCTED,
            overrides={utc(day, "10:15"): (7501.0, 7489.0, 7495.0)},
        ),
    )
    # K — vznik nad zamrzlým spotem (ES 1004 3. 9.): vstup 7480 = close baru 09:30,
    # trh v minutě vzniku na 7498–7502 → vstup mimo bary, nezapisuje se
    day = "2026-08-24"
    f.setup(
        "frozen",
        symbol="ES",
        expiry="20260824",
        created=utc(day, "10:00"),
        entry=7480.0,
        target=7510.0,
        stop=7470.0,
        status="closed_target",
        closed=utc(day, "10:20"),
        outcome_r=3.0,
    )
    f.writer.write_bars_by_day(
        "ES",
        series(
            utc(day, "09:25"),
            utc(day, "10:30"),
            7500.0,
            overrides={utc(day, "09:30"): (7482.0, 7478.0, 7480.0)},
        ),
    )
    return f


def read_csv(out: Path) -> dict[int, dict[str, str]]:
    (path,) = out.glob("recompute-setup-outcomes-*.csv")
    with path.open(encoding="utf-8") as handle:
        return {int(row["id"]): row for row in csv.DictReader(handle)}


def snapshot(fx: Fixture) -> list[dict[str, Any]]:
    return sorted(
        [*fx.repository.list_for("NQ"), *fx.repository.list_for("ES")], key=lambda r: r["id"]
    )


def test_dry_run_reportuje_rozdily_a_nic_nezapise(mod: Any, fx: Fixture, tmp_path: Path) -> None:
    before = snapshot(fx)
    out = tmp_path / "out"
    assert mod.main(["--db", fx.url, "--data", str(fx.data), "--out", str(out)]) == 0
    assert snapshot(fx) == before  # dry-run do DB nesahá

    rows = read_csv(out)
    ids = fx.ids
    verdicts = {name: rows[setup_id]["verdict"] for name, setup_id in ids.items()}
    assert verdicts == {
        "gap_stop": "opravit",
        "late_timeout": "opravit",
        "same": "beze změny",
        "after_settle": "po settle",
        "hole": "neověřitelný",
        "mixed": "neověřitelný",
        "quarterly": "neověřitelný",
        "contract": "neověřitelný",
        "late_close": "opravit čas",
        "tasty_only": "neověřitelný",
        "frozen": "vstup mimo bary",
    }
    gap = rows[ids["gap_stop"]]
    assert (gap["old_status"], gap["new_status"]) == ("closed_target", "closed_stop")
    assert (gap["old_r"], gap["new_r"]) == ("+3.000", "-1.000")
    assert gap["new_closed_ts"] == "2026-09-03T14:56:00+00:00"
    # 1 kontrakt NQ: (−1 − 3) R × 50 b × 20 $
    assert float(gap["usd_diff_1c"]) == pytest.approx(-4000.0)
    late = rows[ids["late_timeout"]]
    assert late["new_status"] == "closed_timeout"
    assert float(late["new_r"]) == pytest.approx(0.4)  # close 19:59 = 29320
    assert late["new_closed_ts"] == "2026-09-02T20:00:00+00:00"  # settle, ne 20:50
    # Díra: neověřitelný, návrh přes díru jen v reportu (cíl 10:20)
    hole = rows[ids["hole"]]
    assert "10:05–10:06" in hole["reason"]
    assert (hole["new_status"], hole["new_closed_ts"]) == (
        "closed_target",
        "2026-08-31T10:20:00+00:00",
    )
    assert "tasty_candle" in rows[ids["mixed"]]["reason"]
    assert "tasty_candle" in rows[ids["tasty_only"]]["reason"]
    # Návrh u neověřitelného zůstává v reportu (stop z tasty), jen se nezapisuje
    assert rows[ids["mixed"]]["new_status"] == "closed_stop"
    assert "jiný kontrakt" in rows[ids["contract"]]["reason"]
    assert "kvartální" in rows[ids["quarterly"]]["reason"]
    late_close = rows[ids["late_close"]]
    assert late_close["new_closed_ts"] == "2026-08-26T13:30:00+00:00"
    assert float(late_close["usd_diff_1c"]) == pytest.approx(0.0)
    # Vstup nad zamrzlým spotem: neexistující cena se nezapisuje, důvod ukáže zdroj
    frozen = rows[ids["frozen"]]["reason"]
    assert "mimo bary" in frozen and "close baru 09:30" in frozen and "zamrzlý spot" in frozen
    # MFE/MAE návrhu ze stejné cesty ceny: stop ⇒ MAE ≥ riziko (1003: MAE 5 b při riziku 50 b)
    assert (gap["old_mae"], gap["new_mae"]) == ("2.00", "60.00")
    for row in rows.values():
        if row["verdict"] in ("opravit", "opravit čas") and row["new_status"] == "closed_stop":
            assert float(row["new_mae"]) >= 50.0 - 1e-6, row["id"]

    (md,) = out.glob("recompute-setup-outcomes-*.md")
    text = md.read_text(encoding="utf-8")
    assert "dry-run — do DB nic nezapsáno" in text
    assert "| opravit | 2 |" in text
    assert "| opravit čas | 1 |" in text
    assert "| vstup mimo bary | 1 |" in text
    assert f"| {ids['gap_stop']} | NQ | cíl → stop | +3.000 → -1.000 |" in text
    assert "## Vstup mimo bary (nezapisují se, rozhodne uživatel)" in text


def test_apply_zapise_outcome_correction_a_nic_nesmaze(
    mod: Any, fx: Fixture, tmp_path: Path
) -> None:
    before = {row["id"]: row for row in snapshot(fx)}
    approved = approve(mod, fx, tmp_path)
    out = tmp_path / "out"
    args = [
        *("--db", fx.url, "--data", str(fx.data), "--out", str(out)),
        *("--apply", "--yes", "--approved", str(approved)),
    ]
    assert mod.main(args) == 0

    after = {row["id"]: row for row in snapshot(fx)}
    assert after.keys() == before.keys()  # nic nesmazáno
    gap = after[fx.ids["gap_stop"]]
    assert gap["status"] == "closed_stop"
    assert gap["outcome_r"] == pytest.approx(-1.0)
    assert str(gap["closed_ts"]).startswith("2026-09-03T14:56")
    correction = gap["context"]["outcome_correction"]
    assert correction["old_status"] == "closed_target"
    assert correction["old_outcome_r"] == pytest.approx(3.0)
    assert correction["old_closed_ts"].startswith("2026-09-03T15:30")
    assert correction["corrected_ts"] and "#1320" in correction["reason"]
    # Ostatní kontext zůstává; MFE/MAE z téže cesty jako výsledek, původní v auditu
    assert gap["context"]["tradeable"] is True and gap["context"]["contracts"] == 2
    assert (gap["mfe"], gap["mae"]) == (pytest.approx(10.0), pytest.approx(60.0))
    assert gap["mae"] >= gap["entry"] - gap["stop"]  # stop ⇒ MAE ≥ riziko
    assert (correction["old_mfe"], correction["old_mae"]) == (1.0, 2.0)
    late = after[fx.ids["late_timeout"]]
    assert late["status"] == "closed_timeout" and late["outcome_r"] == pytest.approx(0.4)
    # Oprava jen času: status a R zůstávají, audit nese původní čas
    late_close = after[fx.ids["late_close"]]
    assert (late_close["status"], late_close["outcome_r"]) == ("closed_stop", -1.0)
    assert str(late_close["closed_ts"]).startswith("2026-08-26T13:30")
    assert late_close["context"]["outcome_correction"]["old_closed_ts"].startswith(
        "2026-08-26T13:33"
    )
    # Neopravované řádky se nezměnily
    unchanged = (
        "same",
        "after_settle",
        "hole",
        "mixed",
        "quarterly",
        "contract",
        "tasty_only",
        "frozen",
    )
    for name in unchanged:
        assert after[fx.ids[name]] == before[fx.ids[name]], name

    # Opakovaný běh je idempotentní: nic k opravě, původní hodnoty zůstávají
    assert mod.main(args) == 0
    again = {row["id"]: row for row in snapshot(fx)}
    assert again[fx.ids["gap_stop"]]["context"]["outcome_correction"]["old_status"] == (
        "closed_target"
    )
    assert again[fx.ids["gap_stop"]]["status"] == "closed_stop"


def approve(mod: Any, fx: Fixture, tmp_path: Path) -> Path:
    """Dry-run, jehož CSV uživatel schválil — vstup `--approved`."""
    out = tmp_path / "dry"
    assert mod.main(["--db", fx.url, "--data", str(fx.data), "--out", str(out)]) == 0
    (path,) = out.glob("recompute-setup-outcomes-*.csv")
    return path


def test_apply_zapise_jen_schvaleny_report(mod: Any, fx: Fixture, tmp_path: Path) -> None:
    """Přepočet se od schváleného dry-runu liší (mezitím doplněné bary) → nic se nezapíše."""
    before = snapshot(fx)
    approved = approve(mod, fx, tmp_path)
    rows = list(csv.DictReader(approved.open(encoding="utf-8")))
    for row in rows:
        if int(row["id"]) == fx.ids["gap_stop"]:
            row["new_r"] = "+3.000"  # uživatel schválil jiný výsledek, než by se zapsal
    with approved.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    args = [
        *("--db", fx.url, "--data", str(fx.data), "--out", str(tmp_path / "o")),
        *("--apply", "--yes", "--approved", str(approved)),
    ]
    assert mod.main(args) == 1
    assert snapshot(fx) == before
    # Neinteraktivní zápis bez schváleného reportu nejde vůbec
    with pytest.raises(SystemExit):
        mod.main(["--db", fx.url, "--data", str(fx.data), "--apply", "--yes"])
    assert snapshot(fx) == before


def test_apply_bez_souhlasu_nezapise(
    mod: Any, fx: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = snapshot(fx)
    monkeypatch.setattr("builtins.input", lambda _prompt: "ne")
    args = ["--db", fx.url, "--data", str(fx.data), "--out", str(tmp_path / "o"), "--apply"]
    assert mod.main(args) == 1
    assert snapshot(fx) == before


def test_opakovana_oprava_drzi_puvodni_hodnoty(fx: Fixture) -> None:
    """Druhá oprava téhož řádku nepřepíše původní (engine) hodnoty v kontextu."""
    setup_id = fx.ids["same"]
    now = dt.datetime(2026, 9, 30, 8, 0, tzinfo=dt.UTC)
    assert fx.repository.correct_outcome(
        setup_id,
        status="closed_target",
        closed_ts=utc("2026-09-01", "10:20"),
        outcome_r=3.0,
        mfe=150.0,
        mae=4.0,
        reason="první",
        corrected_ts=now,
    )
    assert fx.repository.correct_outcome(
        setup_id,
        status="closed_timeout",
        closed_ts=utc("2026-09-01", "20:00"),
        outcome_r=0.2,
        mfe=20.0,
        mae=6.0,
        reason="druhá",
        corrected_ts=now + dt.timedelta(hours=1),
    )
    row = next(r for r in fx.repository.list_for("NQ") if r["id"] == setup_id)
    correction = row["context"]["outcome_correction"]
    assert (correction["old_status"], correction["old_outcome_r"]) == ("closed_stop", -1.0)
    assert (correction["old_mfe"], correction["old_mae"]) == (1.0, 2.0)
    assert correction["reason"] == "druhá"
    assert (row["status"], row["mfe"], row["mae"]) == ("closed_timeout", 20.0, 6.0)


def test_bezici_expirace_bez_zasahu_je_neoveritelna(mod: Any) -> None:
    """Expirace ještě běží a bary úroveň nezasáhly: DB (stop) nelze potvrdit ani vyvrátit."""
    created = utc("2026-09-30", "14:00")
    row = mod.SetupRow(
        id=1,
        symbol="ES",
        expiry="20260930",
        direction="long",
        created_ts=created,
        entry=7500.0,
        target=7530.0,
        stop=7490.0,
        status="closed_stop",
        closed_ts=created + 30 * MINUTE,
        outcome_r=-1.0,
    )
    bars = series(created - 2 * MINUTE, created + 40 * MINUTE, 7500.0)

    def load(_symbol: str, since: dt.datetime, until: dt.datetime) -> list[Bar]:
        return [bar for bar in bars if since < bar.ts <= until]

    result = mod.recompute(row, load, created + 42 * MINUTE)
    assert result.verdict == "neověřitelný"
    assert "expirace ještě běží" in result.reason


def test_cesta_zacina_za_barem_vstupu_z_kontextu(mod: Any) -> None:
    """Bar vstupu z kontextu (#1320): opožděný cyklus nesl bar N, entry je jeho close.

    Bar N proběhl před vstupem — jeho low pod stopem setup nezavírá. Bez
    `entry_bar_ts` by přepočet začal za N−1 jako engine u setupu nad spotem
    a zapsal falešný stop.
    """
    created = utc("2026-09-29", "14:00")
    bars = series(
        created - 5 * MINUTE,
        created + 30 * MINUTE,
        7500.0,
        overrides={
            created - 2 * MINUTE: (7502.0, 7496.0, 7497.0),
            created - MINUTE: (7502.0, 7498.0, 7499.0),
            created: (7501.0, 7489.0, 7500.0),  # bar vstupu: low pod stopem, close = entry
            created + 10 * MINUTE: (7531.0, 7499.0, 7505.0),
        },
    )

    def load(_symbol: str, since: dt.datetime, until: dt.datetime) -> list[Bar]:
        return [bar for bar in bars if since < bar.ts <= until]

    def row(entry_bar: dt.datetime | None) -> Any:
        return mod.SetupRow(
            id=1,
            symbol="ES",
            expiry="20260929",
            direction="long",
            created_ts=created,
            entry=7500.0,
            target=7530.0,
            stop=7490.0,
            status="closed_target",
            closed_ts=created + 10 * MINUTE,
            outcome_r=3.0,
            entry_bar_ts=entry_bar,
        )

    now = utc("2026-09-30", "08:00")
    result = mod.recompute(row(created), load, now)
    assert result.verdict == "beze změny"
    assert result.new_mae == pytest.approx(2.0)  # low 7489 baru vstupu se nepočítá
    # Starší řádek bez kontextu: bar vstupu se dohledá podle close == entry
    assert mod.recompute(row(None), load, now).verdict == "beze změny"
