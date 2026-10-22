"""Replay backtestu a walk-forwardu po cestě ceny (#1345, #1369) — `scripts/backtest_setups.py`.

Skript žije ve `scripts/`, načítá se přes importlib (vzor test_recompute_setup_outcomes).
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pandas as pd
import pytest
from sqlalchemy import create_engine
from test_setups import VOLUME_PLAN, FakeRuntime, QuotesRuntime, RecordingPublisher, quote

import gexlens_engine.setups as live_setups
from gexlens_engine.compute.bandregime import band_zone
from gexlens_engine.compute.gexfield import GexProfile
from gexlens_engine.compute.settle import expiry_settle, is_trading_session, trading_session_date
from gexlens_engine.compute.setups import (
    Direction,
    MinuteInputs,
    ProbeMinute,
    ProbeParams,
    SetupCandidate,
    SetupParams,
    SetupTemplate,
)
from gexlens_engine.compute.walkforward import WalkForwardParams, walk_forward
from gexlens_engine.ibkr.scheduler import CachedQuote
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.runtime import EngineRuntime
from gexlens_engine.setups import SetupEngine
from gexlens_engine.storage.oi_archive import OIEodRepository
from gexlens_engine.storage.setups_store import SetupsRepository

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backtest_setups.py"
EXPIRY = "20260917"  # čtvrtek, settle 20:00 UTC
_SETTLE = expiry_settle(EXPIRY)
assert _SETTLE is not None
SETTLE: dt.datetime = _SETTLE
UTC = dt.UTC
MINUTE = dt.timedelta(minutes=1)


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("backtest_setups", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def minute(
    ts: dt.datetime, close: float, high: float | None = None, low: float | None = None
) -> MinuteInputs:
    return MinuteInputs(
        ts=ts,
        open=close,
        high=high if high is not None else close,
        low=low if low is not None else close,
        close=close,
        flip=None,
        call_wall=None,
        put_wall=None,
        max_pain=None,
        cum_delta=0.0,
        call_flow=0.0,
        put_flow=0.0,
        opt_vol=0.0,
        minutes_to_expiry=None,
    )


def _one_setup_at(module: Any, created: dt.datetime) -> None:
    """Detektor vrátí jeden long setup v minutě `created` (entry 100, cíl 110, stop 95)."""

    def fake_detect_all(history: list[MinuteInputs], params: SetupParams) -> list[SetupCandidate]:
        if history[-1].ts != created:
            return []
        return [
            SetupCandidate(
                template=SetupTemplate.WALL_BOUNCE,
                direction=Direction.LONG,
                entry=100.0,
                target=110.0,
                stop=95.0,
                confidence=50,
                reason="test",
            )
        ]

    module.detect_all = fake_detect_all


def test_replay_setup_otevreny_v_settle_konci_timeoutem() -> None:
    """#1369: setup bez zásahu úrovně skončí v settle timeoutem za close baru
    končícího v settle — dřív zůstal `active` bez R, živě ho uzavře timeout."""
    module = _load()
    created = SETTLE - dt.timedelta(minutes=10)
    _one_setup_at(module, created)
    minutes = [
        minute(created + dt.timedelta(minutes=offset), 100.0 + offset * 0.2)
        for offset in range(0, 15)  # běží i po settle (mrtvý řetěz se nehodnotí)
    ]
    rows = module.replay(minutes, SetupParams(), EXPIRY, "ES")
    assert [(row["outcome"], row["closed"]) for row in rows] == [("closed_timeout", SETTLE)]
    # Close baru 19:59 (končí v settle) = 100 + 9 × 0,2; risk 5 b
    assert rows[0]["r"] == pytest.approx(9 * 0.2 / 5)


def test_replay_kvartalni_patek_tydenni_serie_do_odpoledne() -> None:
    """#1366: partice třídu řetězu nenesou — kořenový ticker sbíral na kvartální
    datum po rollu týdenní sérii (16:00 ET), setup po SOQ vznikne a timeout
    dostane odpoledne; pinovaný expirující kontrakt končí v SOQ."""
    module = _load()
    soq = dt.datetime(2026, 9, 18, 13, 30, tzinfo=dt.UTC)
    afternoon = dt.datetime(2026, 9, 18, 20, 0, tzinfo=dt.UTC)
    assert module.expiry_end("ES", "20260918") == afternoon
    assert module.expiry_end("ESU6", "20260918") == soq
    created = afternoon - dt.timedelta(minutes=10)
    _one_setup_at(module, created)
    minutes = [minute(created + dt.timedelta(minutes=offset), 100.0) for offset in range(15)]
    rows = module.replay(minutes, SetupParams(), "20260918", "ES")
    assert [(row["outcome"], row["closed"]) for row in rows] == [("closed_timeout", afternoon)]
    assert module.replay(minutes, SetupParams(), "20260918", "ESU6") == []


def test_replay_stop_za_dirou_v_datech() -> None:
    """Díra v minutách (trh běžel, bar chybí) se offline přejde a spočítá;
    stop po díře platí s časem svého baru."""
    module = _load()
    created = dt.datetime(2026, 9, 17, 14, 0, tzinfo=dt.UTC)
    _one_setup_at(module, created)
    minutes = [
        minute(created, 100.0),
        minute(created + dt.timedelta(minutes=1), 101.0),
        # 14:02–14:04 chybí
        minute(created + dt.timedelta(minutes=5), 96.0, low=94.0),
    ]
    rows = module.replay(minutes, SetupParams(), EXPIRY, "ES")
    assert rows[0]["outcome"] == "closed_stop"
    assert rows[0]["closed"] == created + dt.timedelta(minutes=5)
    assert rows[0]["gaps"] == 1


def _profile(ts: dt.datetime) -> GexProfile:
    """Hrb 140–160 na mřížce 100–190: zóna All ≈ 135–165 (vzor test_probes)."""
    return GexProfile(
        ts_min=ts,
        grid_start=100.0,
        grid_step=10.0,
        values=(0.0, 0.0, 0.0, 0.0, 8.0, 10.0, 8.0, 0.0, 0.0, 0.0),
    )


def test_replay_sond_stop_v_minute_bez_profilu_a_timeout_v_settle() -> None:
    """#1345: minuta bez profilu (zóna None) patří cestě ceny — dřív se vynechala
    a stop v ní se ztratil; sonda bez zásahu končí v settle, ne posledním barem dat."""
    module = _load()
    accept = ProbeParams().acceptance_minutes
    start = SETTLE - dt.timedelta(minutes=60)

    def probe_minute(
        offset: int, close: float, low: float | None = None, zone: bool = True
    ) -> ProbeMinute:
        ts = start + dt.timedelta(minutes=offset)
        return ProbeMinute(
            ts=ts,
            high=close,
            low=low if low is not None else close,
            close=close,
            zone=band_zone(_profile(ts), close) if zone else None,
        )

    entering = [probe_minute(i, 120.0) for i in range(accept)] + [
        probe_minute(accept + i, 138.0) for i in range(accept)
    ]
    rows = module.replay_probes(entering, ProbeParams(), SETTLE)
    assert [row["outcome"] for row in rows] == ["closed_timeout"]
    assert rows[0]["closed"] == SETTLE

    # Táž sonda, ale další minuta bez profilu propadne pod stop
    crash = probe_minute(2 * accept, 100.0, low=100.0, zone=False)
    rows = module.replay_probes([*entering, crash], ProbeParams(), SETTLE)
    assert [row["outcome"] for row in rows] == ["closed_stop"]
    assert rows[0]["closed"] == crash.ts - dt.timedelta(minutes=1)


def test_walkforward_predava_symbol_do_replaye(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1081: walk-forward přehrává týmž `replay` se symbolem řetězu — po #1366
    volání bez symbolu shodilo noční běh (TypeError) a `scripts/` mypy nehlídá.
    Kvartální pátek: kořenový ticker má týdenní sérii do odpoledne, pinovaný
    expirující kontrakt končí v SOQ."""
    monkeypatch.delitem(sys.modules, "backtest_setups", raising=False)
    spec = importlib.util.spec_from_file_location(
        "walkforward_setups", SCRIPT.with_name("walkforward_setups.py")
    )
    assert spec is not None and spec.loader is not None
    module: Any = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    bt = module.bt

    afternoon = dt.datetime(2026, 9, 18, 20, 0, tzinfo=dt.UTC)
    start = afternoon - dt.timedelta(minutes=70)
    created = afternoon - dt.timedelta(minutes=10)
    minutes = [
        minute(start + dt.timedelta(minutes=offset), 100.0 + max(0, offset - 60) * 0.2)
        for offset in range(75)
    ]
    for symbol in ("ES", "ESU6"):
        (tmp_path / symbol / "20260918").mkdir(parents=True)
        (tmp_path / symbol / "bars").mkdir()  # nečíselné adresáře nejsou expirace
    monkeypatch.setattr(bt, "DATA", str(tmp_path))
    monkeypatch.setattr(bt, "build_minutes", lambda symbol, expiry, repo, prints, skip: minutes)
    _one_setup_at(bt, created)

    day = dt.date(2026, 9, 18)
    # Timeout za close baru 19:59 = 100 + 9 × 0,2; risk 5 b
    assert module.daily_series("ES", {"baseline": SetupParams()}, None) == {
        "baseline": {day: pytest.approx(9 * 0.2 / 5)}
    }
    assert module.daily_series("ESU6", {"baseline": SetupParams()}, None) == {
        "baseline": {day: 0.0}
    }


def _walkforward(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delitem(sys.modules, "backtest_setups", raising=False)
    spec = importlib.util.spec_from_file_location(
        "walkforward_setups", SCRIPT.with_name("walkforward_setups.py")
    )
    assert spec is not None and spec.loader is not None
    module: Any = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_walkforward_v6_bere_seance_od_stinu_a_predava_mechaniku(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1590: seance před 21. 9. 2026 nemají CumΔ z tisků — walk-forward v6 je nepoužije."""
    module = _walkforward(monkeypatch)
    bt = module.bt
    for expiry in ("20260918", "20260921", "20260922"):
        (tmp_path / "ES" / expiry).mkdir(parents=True)
    monkeypatch.setattr(bt, "DATA", str(tmp_path))
    calls: list[tuple[str, bool]] = []

    def fake_build(
        symbol: str, expiry: str, repo: object, prints: bool, skip: object
    ) -> list[MinuteInputs]:
        calls.append((expiry, prints))
        return []

    monkeypatch.setattr(bt, "build_minutes", fake_build)
    module.daily_series("ES", {"baseline": SetupParams()}, None, prints=True, start=bt.SHADOW_FROM)
    assert calls == [("20260921", True), ("20260922", True)]
    calls.clear()
    module.daily_series("ES", {"baseline": SetupParams()}, None, prints=False)
    assert calls == [("20260918", False), ("20260921", False), ("20260922", False)]


def test_walkforward_v6_odmitne_from_pred_stinem(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _walkforward(monkeypatch)
    with pytest.raises(SystemExit):
        module.main(["--db", "sqlite://", "--mechanics", "6", "--from", "2026-09-18"])


# ── Okno aktivního řetězu (#1464) ───────────────────────────────────────────


def _write(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path)


def _partitions(
    root: Path,
    levels: dict[str, list[dt.datetime]],
    close: Callable[[dt.datetime], float] = lambda ts: 100.0,
) -> None:
    """Bary ES a levels expirací: rozhodovací minuta N = bar N − 1 + levels N.

    Bary v partici UTC dne svých minut jako v produkci; partice cizího dne je
    rozbitá — `build_minutes` ji číst nesmí (#1464)."""
    derived = root / "derived" / "ES"
    decisions = sorted({ts for minutes in levels.values() for ts in minutes})
    prices = [close(ts) for ts in decisions]
    bars = pd.DataFrame(
        {
            "ts_min": [ts - MINUTE for ts in decisions],
            "open": prices,
            "high": prices,
            "low": prices,
            "close": prices,
        }
    )
    for day, part in bars.groupby(bars["ts_min"].dt.strftime("%Y-%m-%d")):
        _write(derived / "bars" / f"{day}.parquet", part)
    (derived / "bars" / "2026-01-05.parquet").write_bytes(b"cizi den, ne parquet")
    for expiry, minutes in levels.items():
        _write(
            derived / expiry / "levels" / "levels.parquet",
            pd.DataFrame(
                {
                    "ts_min": minutes,
                    "flip": 100.0,
                    "call_wall": 110.0,
                    "put_wall": 90.0,
                    "total_gex": 1.0,
                }
            ),
        )


def _at(day: int, hh: int, mm: int, month: int = 10) -> dt.datetime:
    return dt.datetime(2026, month, day, hh, mm, tzinfo=UTC)


def test_build_minutes_sekundarni_minuty_pred_rollem_a_od_settle_neprehrava(
    tmp_path: Path,
) -> None:
    """Středa 21. 10.: sekundár psal E už 20. 10. odpoledne (kadence 3 min), živý
    engine ji má od settle 20261020 (20:00 UTC, roll #1331) do vlastního settle."""
    module = _load()
    module.set_data_root(str(tmp_path))
    secondary = [_at(20, 14, 0), _at(20, 14, 3), _at(20, 14, 6)]
    active = [_at(20, 20, 0), _at(20, 20, 1), _at(21, 19, 58), _at(21, 19, 59)]
    after = [_at(21, 20, 0), _at(21, 20, 1)]
    _partitions(tmp_path, {"20261021": secondary + active + after})
    assert module.active_window("ES", "20261021") == (_at(20, 20, 0), _at(21, 20, 0))
    skip = module.WindowSkip()
    minutes = module.build_minutes("ES", "20261021", None, skip=skip)
    assert [m.ts for m in minutes] == active
    assert skip.minutes == len(secondary) + len(after)
    # Čas do expirace i timeout dál podle settle E
    assert minutes[-1].minutes_to_expiry == pytest.approx(1.0)


def test_okno_pondelni_expirace_od_patku_a_po_svatku() -> None:
    """Sobota a neděle nejsou obchodní dny (`is_trading_session`): pondělní
    expirace je aktivní od pátečního settle. Po Labor Day (7. 9.) od pátku 4. 9."""
    module = _load()
    friday, saturday, sunday, monday = (dt.date(2026, 10, d) for d in (16, 17, 18, 19))
    assert is_trading_session(friday) and is_trading_session(monday)
    assert not is_trading_session(saturday) and not is_trading_session(sunday)
    assert module.previous_expiry("20261019") == "20261016"
    assert module.previous_expiry("20261020") == "20261019"
    assert module.previous_expiry("20260908") == "20260904"
    assert module.active_window("ES", "20261019") == (_at(16, 20, 0), _at(19, 20, 0))
    # Týden konce letního času v USA: páteční settle 20:00 UTC, pondělní 21:00 UTC
    assert module.active_window("ES", "20261102") == (_at(30, 20, 0), _at(2, 21, 0, month=11))


def test_build_minutes_pondelni_expirace_bere_patecni_vecer_a_nedeli(tmp_path: Path) -> None:
    module = _load()
    module.set_data_root(str(tmp_path))
    friday_secondary = _at(16, 19, 30)  # před settle 20261016 = sekundár
    inside = [_at(16, 20, 30), _at(18, 22, 30), _at(19, 14, 0)]
    _partitions(tmp_path, {"20261019": [friday_secondary, *inside]})
    skip = module.WindowSkip()
    assert [m.ts for m in module.build_minutes("ES", "20261019", None, skip=skip)] == inside
    assert skip.minutes == 1


def test_build_minutes_pred_1331_vecer_sekundaru_vynecha_do_aktivni_kadence(
    tmp_path: Path,
) -> None:
    """Úterý 6. 10. (před #1331): po settle 20261005 psal E jen sekundár à 3 min,
    živý řetěz roloval až o půlnoci UTC — replay začne první souvislou kadencí."""
    module = _load()
    module.set_data_root(str(tmp_path))
    evening = [_at(5, 20, 30), _at(5, 20, 33), _at(5, 20, 36)]
    active = [_at(6, 0, 0), _at(6, 0, 1), _at(6, 0, 2), _at(6, 0, 3), _at(6, 19, 59)]
    _partitions(tmp_path, {"20261006": evening + active})
    assert module.active_window("ES", "20261006")[0] == _at(5, 20, 0)
    skip = module.WindowSkip()
    assert [m.ts for m in module.build_minutes("ES", "20261006", None, skip=skip)] == active
    assert skip.minutes == len(evening)


def test_option_flows_po_dnech_nese_posledni_objem_kontraktu_do_dalsiho_dne(
    tmp_path: Path,
) -> None:
    """Partice snapshotů se čtou po dnech: přírůstek první minuty dne počítá
    proti poslednímu objemu kontraktu z předchozí partice, i přes víkend (v5;
    v6 viz `test_option_flows_v6_proti_maximu_seance_nese_stav_do_dalsiho_dne`)."""
    module = _load()
    module.set_data_root(str(tmp_path))
    folder = tmp_path / "snapshots" / "ES" / "20261019"
    friday, sunday = _at(16, 19, 59), _at(18, 22, 0)
    _write(
        folder / "2026-10-16.parquet",
        pd.DataFrame(
            {
                "ts_min": [friday - MINUTE, friday],
                "strike": [5000.0, 5000.0],
                "right": ["C", "C"],
                "volume": [10.0, 12.0],
                "delta": [0.5, 0.5],
            }
        ),
    )
    _write(
        folder / "2026-10-18.parquet",
        pd.DataFrame(
            {
                "ts_min": [sunday, sunday, sunday + MINUTE],
                "strike": [5000.0, 4900.0, 4900.0],
                "right": ["C", "P", "P"],
                "volume": [15.0, 7.0, 9.0],
                "delta": [0.5, -0.25, -0.25],
            }
        ),
    )
    flows = module.option_flows("ES", "20261019", prints=False).set_index("ts_min")
    # Call 5000 v neděli: 15 − 12 z pátku; put 4900 se objevil poprvé → bez přírůstku
    assert flows.loc[sunday, "call_flow"] == pytest.approx(3 * 0.5)
    assert flows.loc[sunday, "put_flow"] == 0.0
    assert flows.loc[sunday, "opt_vol"] == pytest.approx(3.0)
    assert flows.loc[sunday + MINUTE, "put_flow"] == pytest.approx(2 * 0.25)
    assert flows.loc[friday, "opt_vol"] == pytest.approx(2.0)


def _snapshot_partitions(
    root: Path, expiry: str, cycles: list[tuple[dt.datetime, dict[Any, CachedQuote]]]
) -> None:
    """Snapshoty expirace z kotací cyklů (řádek = kontrakt s kotací) po UTC dnech jako engine."""
    rows = [
        (ts, contract.strike, contract.right, cached.snapshot.volume, cached.snapshot.delta)
        for ts, quotes in cycles
        for contract, cached in quotes.items()
    ]
    frame = pd.DataFrame(rows, columns=["ts_min", "strike", "right", "volume", "delta"])
    folder = root / "snapshots" / "ES" / expiry
    for day, part in frame.groupby(frame["ts_min"].dt.strftime("%Y-%m-%d")):
        _write(folder / f"{day}.parquet", part)


def _live_flows(
    tmp_path: Path, cycles: list[tuple[dt.datetime, dict[Any, CachedQuote]]]
) -> list[tuple[float, float, float]]:
    """Živý `SetupEngine._flows` pod v6 nad týmiž kotacemi, cyklus = minuta s bary."""
    repository = SetupsRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 's.db'}"))
    repository.ensure_schema()
    engine = SetupEngine(
        symbol="ES",
        repository=repository,
        oi_repository=OIEodRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'o.db'}")),
        publisher=RecordingPublisher(),
        cumdelta_prints=True,
    )
    runtime = QuotesRuntime()
    flows: list[tuple[float, float, float]] = []
    for ts, quotes in cycles:
        runtime.quotes = quotes
        flows.append(engine._flows(cast(EngineRuntime, runtime), trading_session_date(ts)))
    return flows


def _replay_flows(
    module: Any, cycles: list[tuple[dt.datetime, Any]], **kwargs: Any
) -> list[tuple[float, float, float]]:
    flows = module.option_flows("ES", "20261021", prints=True, **kwargs).set_index("ts_min")
    rows: list[tuple[float, float, float]] = []
    for ts, _ in cycles:
        if ts not in flows.index:  # minuta bez řádku s objemem = merge left → 0 v `build_minutes`
            rows.append((0.0, 0.0, 0.0))
            continue
        row = flows.loc[ts]
        rows.append((float(row["call_flow"]), float(row["put_flow"]), float(row["opt_vol"])))
    return rows


def test_option_flows_v6_proti_maximu_seance_nese_stav_do_dalsiho_dne(tmp_path: Path) -> None:
    """#1602 AC: řady „Hotovo když“ přes půlnoc UTC (dvě partice, táž seance 21. 10.).
    Do dalšího dne jde maximum i mezera: NULL ve 23:59 a 130 v 00:00 = minuta po mezeře."""
    module = _load()
    module.set_data_root(str(tmp_path))
    runtime = QuotesRuntime()
    first = _at(20, 23, 58)
    cycles = [(first + i * MINUTE, dict(runtime.step(i).quotes)) for i in range(5)]
    _snapshot_partitions(tmp_path, "20261021", cycles)
    expected = [(0.0, 0.0, 0.0), (0.0, 0.25, 1.0), (0.0, 0.0, 0.0), (8.0, 0.0, 20.0)]
    expected.append((5.0, 0.25, 11.0))
    replayed = _replay_flows(module, cycles)
    assert replayed == [pytest.approx(row) for row in expected]
    assert replayed == [pytest.approx(row) for row in _live_flows(tmp_path, cycles)]


def test_option_flows_v6_parita_se_zivym_enginem_seance_mezera_a_okno(tmp_path: Path) -> None:
    """Parita replay ↔ živý `SetupEngine._flows` pod v6 (#1602): roll seance v 17:00 CT
    (22:00 UTC) referenci maže, chybějící řádek kontraktu je mezera a okno aktivního
    řetězu (`start`) začíná prázdným stavem — dřívější minuty živý engine nečetl."""
    module = _load()
    module.set_data_root(str(tmp_path))
    call, put = list(VOLUME_PLAN)[:2]
    start = _at(20, 21, 57)
    plan: list[tuple[dt.datetime, dict[Any, CachedQuote]]] = [
        (_at(20, 21, 56), {call: quote(500.0, 0.5), put: quote(10.0, -0.2)}),  # před oknem
        (start, {call: quote(1000.0, 0.5), put: quote(50.0, -0.2)}),
        (_at(20, 21, 58), {put: quote(55.0, -0.2)}),  # call bez řádku
        (_at(20, 21, 59), {call: quote(1400.0, 0.5), put: quote(53.0, -0.2)}),
        (_at(20, 22, 0), {call: quote(1405.0, 0.5), put: quote(3.0, -0.2)}),  # seance 21. 10.
        (_at(20, 22, 1), {call: quote(1410.0, 0.5), put: quote(None, -0.2)}),
        (_at(20, 22, 2), {call: quote(1408.0, 0.5), put: quote(9.0, -0.2)}),
        (_at(20, 22, 3), {call: quote(1412.0, 0.5), put: quote(10.0, -0.2)}),
    ]
    _snapshot_partitions(tmp_path, "20261021", plan)
    cycles = plan[1:]
    replayed = _replay_flows(module, cycles, start=start)
    assert [row[2] for row in replayed] == [0.0, 5.0, 0.0, 0.0, 5.0, 0.0, 3.0]
    assert replayed == [pytest.approx(row) for row in _live_flows(tmp_path, cycles)]


def test_walkforward_vynecha_expiraci_bez_settle_podle_okamziku_ne_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Noční běh ve 23:45 SELČ (21:45 UTC) je před settle zítřejší expirace;
    rozhoduje settle proti času běhu v UTC, i v týdnu posunu času (#1568)."""
    module = _walkforward(monkeypatch)
    bt = module.bt
    for expiry in ("20261021", "20261022", "20261023", "20261026", "20261102"):
        (tmp_path / "ES" / expiry).mkdir(parents=True)
    monkeypatch.setattr(bt, "DATA", str(tmp_path))
    calls: list[str] = []

    def fake_build(
        symbol: str, expiry: str, repo: object, prints: bool, skip: object
    ) -> list[MinuteInputs]:
        calls.append(expiry)
        return []

    monkeypatch.setattr(bt, "build_minutes", fake_build)

    def run(now: dt.datetime) -> tuple[list[str], list[str]]:
        calls.clear()
        skip = bt.WindowSkip()
        module.daily_series("ES", {"baseline": SetupParams()}, None, now=now, skip=skip)
        return list(calls), skip.unsettled

    assert run(_at(22, 8, 45)) == (["20261021"], ["20261022", "20261023", "20261026", "20261102"])
    # Settle nastal přesně v okamžiku běhu
    assert run(_at(22, 20, 0))[0] == ["20261021", "20261022"]
    # Po 26. 10. platí v Evropě zimní čas, v USA letní: settle 20261026 = 20:00 UTC
    assert run(_at(26, 19, 59))[0] == ["20261021", "20261022", "20261023"]
    assert run(_at(26, 20, 0))[0] == ["20261021", "20261022", "20261023", "20261026"]
    # 2. 11. už zimní čas i v USA: settle 21:00 UTC, běh ve 20:30 UTC je před ním
    assert "20261102" not in run(_at(2, 20, 30, month=11))[0]
    # Hlavička reportu: vynechané minuty a expirace bez settle k času běhu
    note = module.skip_note(
        "ES", bt.WindowSkip(minutes=12, unsettled=["20261023"]), _at(22, 21, 45)
    )
    assert note.startswith("ES: mimo okno aktivního řetězu")
    assert "vynecháno 12 minut" in note
    assert "bez settle k 2026-10-22 21:45 UTC (1): 20261023" in note


def _sessions(first: dt.date, count: int) -> list[dt.date]:
    days: list[dt.date] = []
    day = first
    while len(days) < count:
        if is_trading_session(day):
            days.append(day)
        day += dt.timedelta(days=1)
    return days


def test_report_d_konci_dnem_d_a_fold_se_v_reportu_d1_neprepocita(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1464 (nález z #1459): report D smí končit posledním OOS dnem D, ne D+1 —
    D+1 je v době běhu jen sekundár a pár aktivních minut. Bez změny kódu a store
    má každý fold reportu D v reportu D+1 tutéž OOS ΣR."""
    module = _walkforward(monkeypatch)
    bt = module.bt
    days = _sessions(dt.date(2026, 9, 17), 26)  # 17. 9. – 22. 10.
    day_d, day_d1 = days[-2], days[-1]
    assert (day_d, day_d1) == (dt.date(2026, 10, 21), dt.date(2026, 10, 22))
    for day in days:
        (tmp_path / "ES" / day.strftime("%Y%m%d")).mkdir(parents=True)
    monkeypatch.setattr(bt, "DATA", str(tmp_path))
    run_at: list[dt.datetime] = []
    monkeypatch.setattr(
        bt, "build_minutes", lambda symbol, expiry, repo, prints, skip: [expiry] * 60
    )
    alternative = SetupParams(cooldown_minutes=20)

    def fake_replay(
        minutes: list[str], params: SetupParams, expiry: str, symbol: str
    ) -> list[dict[str, float]]:
        day = module.session_date(expiry)
        r = 1.0 + 0.1 * days.index(day) if params is alternative else 1.0
        if day == day_d1 and run_at[-1] < bt.expiry_end(symbol, expiry):
            r = -5.0  # neuzavřená seance: sekundární minuty a setupy bez výsledku
        return [{"r": r}]

    monkeypatch.setattr(bt, "replay", fake_replay)
    candidates = {"baseline": SetupParams(), "alt": alternative}

    def report(now: dt.datetime) -> Any:
        run_at.append(now)
        series = module.daily_series("ES", candidates, None, now=now)
        return walk_forward(series, baseline="baseline", params=WalkForwardParams())

    # Noční běh D ve 23:45 SELČ = 21:45 UTC
    report_d = report(dt.datetime.combine(day_d, dt.time(21, 45), UTC))
    report_d1 = report(dt.datetime.combine(day_d1, dt.time(21, 45), UTC))
    assert max(report_d.oos) == day_d
    assert max(report_d1.oos) == day_d1
    folds_d1 = {(f.in_start, f.out_start, f.out_end): f for f in report_d1.folds}
    for fold in report_d.folds:
        again = folds_d1[(fold.in_start, fold.out_start, fold.out_end)]
        assert (again.out_sum_r, again.baseline_out_sum_r) == (
            fold.out_sum_r,
            fold.baseline_out_sum_r,
        )


def _crossing(history: list[MinuteInputs], params: SetupParams) -> list[SetupCandidate]:
    """Detektor nad historií: long, když close překročí 105 (cíl +1, stop −20)."""
    if len(history) < 2 or not history[-2].close < 105.0 <= history[-1].close:
        return []
    close = history[-1].close
    return [
        SetupCandidate(
            template=SetupTemplate.WALL_BOUNCE,
            direction=Direction.LONG,
            entry=close,
            target=close + 1.0,
            stop=close - 20.0,
            confidence=50,
            reason="test",
        )
    ]


async def test_replay_vcerejska_ma_tytez_setupy_jako_zivy_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regresní parita #1464: replay expirace E (`build_minutes` + `replay`) dá
    tytéž setupy (počet a časy vzniku) jako živý `SetupEngine`, který E dostane
    až po rollu v settle předchozí expirace. Sekundární minuty 20. 10. odpoledne
    by v replayi otevřely setup, který živě nikdy nevznikl, a zablokoval šablonu."""
    module = _load()
    module.set_data_root(str(tmp_path))
    expiry = "20261021"
    roll, settle = _at(20, 20, 0), _at(21, 20, 0)
    pattern = (100.0, 106.0, 110.0, 101.0)

    def close(ts: dt.datetime) -> float:
        return pattern[int((ts - roll) / MINUTE) % len(pattern)]

    secondary = [_at(20, 14, 0) + 3 * i * MINUTE for i in range(20)]
    active = [roll + i * MINUTE for i in range(48)] + [
        settle - (30 - i) * MINUTE for i in range(30)
    ]
    _partitions(tmp_path, {expiry: secondary + active + [settle]}, close)
    monkeypatch.setattr(module, "detect_all", _crossing)
    monkeypatch.setattr(live_setups, "detect_all", _crossing)

    repository = SetupsRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 's.sqlite'}"))
    repository.ensure_schema()
    oi_repo = OIEodRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'oi.sqlite'}"))
    oi_repo.ensure_schema()
    engine = SetupEngine(
        symbol="ES",
        repository=repository,
        oi_repository=oi_repo,
        publisher=RecordingPublisher(),
        params=SetupParams(),
    )
    fake = FakeRuntime()
    fake.expiry = expiry
    runtime = cast(EngineRuntime, fake)
    for now in active:  # pipeline E běží od rollu: bar N − 1 + stav N
        price = close(now)
        bar = Bar(ts=now - MINUTE, open=price, high=price, low=price, close=price, volume=1.0)
        await engine.on_minute(now, price, [bar], runtime)
    live = sorted(
        (
            row["template"],
            row["direction"],
            pd.to_datetime(row["created_ts"], utc=True).to_pydatetime(),
        )
        for row in repository.list_for("ES")
    )

    rows = module.replay(module.build_minutes("ES", expiry, None), SetupParams(), expiry, "ES")
    replayed = sorted((row["template"], row["direction"], row["created"]) for row in rows)
    assert len(live) >= 4
    assert replayed == live
