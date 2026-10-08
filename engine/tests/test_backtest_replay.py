"""Replay backtestu a walk-forwardu po cestě ceny (#1345, #1369) — `scripts/backtest_setups.py`.

Skript žije ve `scripts/`, načítá se přes importlib (vzor test_recompute_setup_outcomes).
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from gexlens_engine.compute.bandregime import band_zone
from gexlens_engine.compute.gexfield import GexProfile
from gexlens_engine.compute.settle import expiry_settle
from gexlens_engine.compute.setups import (
    Direction,
    MinuteInputs,
    ProbeMinute,
    ProbeParams,
    SetupCandidate,
    SetupParams,
    SetupTemplate,
)

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backtest_setups.py"
EXPIRY = "20260917"  # čtvrtek, settle 20:00 UTC
_SETTLE = expiry_settle(EXPIRY)
assert _SETTLE is not None
SETTLE: dt.datetime = _SETTLE


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
    monkeypatch.setattr(bt, "build_minutes", lambda symbol, expiry, repo: minutes)
    _one_setup_at(bt, created)

    day = dt.date(2026, 9, 18)
    # Timeout za close baru 19:59 = 100 + 9 × 0,2; risk 5 b
    assert module.daily_series("ES", {"baseline": SetupParams()}, None) == {
        "baseline": {day: pytest.approx(9 * 0.2 / 5)}
    }
    assert module.daily_series("ESU6", {"baseline": SetupParams()}, None) == {
        "baseline": {day: 0.0}
    }
