"""Sběr výskytů kandidáta T9 (#577, fáze 1): zóna, akceptace, hypotetické obchody."""

import datetime as dt
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import create_engine, select

from gexlens_engine.compute.bandregime import band_zone
from gexlens_engine.compute.gexfield import GexProfile
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.probes import ACCEPTANCE_MINUTES, T9ProbeCollector, zone_position
from gexlens_engine.runtime import EngineRuntime
from gexlens_engine.storage.probes_store import ProbeRepository, setup_probes

# Poledne UTC — bezpečně před settle (20:00/21:00 UTC), timeout nezasahuje
NOON = dt.datetime(2026, 8, 27, 12, 0, tzinfo=dt.UTC)


def profile() -> GexProfile:
    """Hrb 140–160 na mřížce 100–190: zóna All ≈ 135–165, jádro na 150."""
    return GexProfile(
        ts_min=NOON,
        grid_start=100.0,
        grid_step=10.0,
        values=(0.0, 0.0, 0.0, 0.0, 8.0, 10.0, 8.0, 0.0, 0.0, 0.0),
    )


def bar(close: float, high: float | None = None, low: float | None = None) -> Bar:
    return Bar(
        ts=NOON,
        open=close,
        high=high if high is not None else close,
        low=low if low is not None else close,
        close=close,
        volume=100.0,
    )


def runtime_with_profile() -> EngineRuntime:
    return cast(EngineRuntime, SimpleNamespace(last_profile=profile()))


def make_collector() -> tuple[T9ProbeCollector, ProbeRepository]:
    repository = ProbeRepository(create_engine("sqlite+pysqlite:///:memory:"))
    repository.ensure_schema()
    return T9ProbeCollector(symbol="ES", repository=repository), repository


def rows(repository: ProbeRepository) -> list[dict[str, object]]:
    with repository._engine.connect() as conn:
        return [dict(row._mapping) for row in conn.execute(select(setup_probes))]


def test_zone_position_mapping() -> None:
    zone = band_zone(profile(), 120.0)
    assert zone is not None
    assert zone_position(None, 120.0) == "unknown"
    assert zone_position(zone, zone.all_low - 1) == "below"
    assert zone_position(zone, zone.center) == "in"
    assert zone_position(zone, zone.all_high + 1) == "above"


def test_band_zone_geometry() -> None:
    """Hrany v cenách, střed a síla nad hlavou — kotvy T9 (žádné body/ATR)."""
    zone = band_zone(profile(), 120.0)
    assert zone is not None
    assert 130 < zone.all_low < 140
    assert 160 < zone.all_high < 170
    assert zone.width == pytest.approx(zone.all_high - zone.all_low)
    assert zone.center == pytest.approx((zone.all_low + zone.all_high) / 2)
    # Cena pod pásmem: celé jádro leží nad hlavou
    assert zone.strength_above == pytest.approx(1.0)


async def test_ceiling_probe_vznik_a_target() -> None:
    """Outside → transition s akceptací otevře LONG na střed zóny; target uzavře."""
    collector, repository = make_collector()
    runtime = runtime_with_profile()
    # Usazená výchozí poloha pod pásmem (streak ≥ akceptace)
    for offset in range(ACCEPTANCE_MINUTES):
        await collector.on_minute(
            NOON + dt.timedelta(minutes=offset - ACCEPTANCE_MINUTES), 120.0, [bar(120.0)], runtime
        )
    # Ověření předpokladů scénáře nad geometrií — ne nad čísly z hlavy
    reference = band_zone(profile(), 120.0)
    assert reference is not None
    assert zone_position(reference, 120.0) == "below"
    assert zone_position(band_zone(profile(), 138.0), 138.0) == "in"
    minute = NOON
    for _ in range(ACCEPTANCE_MINUTES):
        minute += dt.timedelta(minutes=1)
        await collector.on_minute(minute, 138.0, [bar(138.0)], runtime)
    stored = rows(repository)
    assert len(stored) == 1
    probe = stored[0]
    assert probe["template"] == "t9_ceiling"
    assert probe["direction"] == "long"
    assert probe["status"] == "active"
    zone = band_zone(profile(), 138.0)
    assert zone is not None
    assert probe["entry"] == pytest.approx(138.0)
    assert probe["target"] == pytest.approx(zone.center)
    assert probe["stop"] == pytest.approx(zone.all_low - 0.25 * zone.width)
    # Bar protne cíl → uzávěrka stejnou mechanikou jako živé setupy
    minute += dt.timedelta(minutes=1)
    await collector.on_minute(minute, 150.0, [bar(150.0, high=zone.center + 1)], runtime)
    closed = rows(repository)[0]
    assert closed["status"] == "closed_target"
    assert float(cast(float, closed["outcome_r"])) > 0


async def test_okamzite_vraceny_prechod_se_zahazuje() -> None:
    """Návrat pod hranu před akceptací = žádný výskyt (podmínka 3 z #577)."""
    collector, repository = make_collector()
    runtime = runtime_with_profile()
    for offset in range(ACCEPTANCE_MINUTES):
        await collector.on_minute(
            NOON + dt.timedelta(minutes=offset - ACCEPTANCE_MINUTES), 120.0, [bar(120.0)], runtime
        )
    await collector.on_minute(NOON + dt.timedelta(minutes=1), 138.0, [bar(138.0)], runtime)
    await collector.on_minute(NOON + dt.timedelta(minutes=2), 120.0, [bar(120.0)], runtime)
    for offset in range(3, 3 + ACCEPTANCE_MINUTES):
        await collector.on_minute(NOON + dt.timedelta(minutes=offset), 120.0, [bar(120.0)], runtime)
    assert rows(repository) == []


async def test_exit_probe_zrcadlo() -> None:
    """Výpad z pásma dolů = momentum SHORT; návrat nad hranu = stop."""
    collector, repository = make_collector()
    runtime = runtime_with_profile()
    # Usazená výchozí poloha uvnitř pásma
    for offset in range(ACCEPTANCE_MINUTES):
        await collector.on_minute(
            NOON + dt.timedelta(minutes=offset - ACCEPTANCE_MINUTES), 140.0, [bar(140.0)], runtime
        )
    minute = NOON
    for _ in range(ACCEPTANCE_MINUTES):
        minute += dt.timedelta(minutes=1)
        await collector.on_minute(minute, 128.0, [bar(128.0)], runtime)
    stored = rows(repository)
    assert len(stored) == 1
    probe = stored[0]
    assert probe["template"] == "t9_exit"
    assert probe["direction"] == "short"
    zone = band_zone(profile(), 128.0)
    assert zone is not None
    assert probe["stop"] == pytest.approx(zone.all_low)
    assert probe["target"] == pytest.approx(128.0 - zone.width)
    # Návrat nad hranu → stop, R = −1 (risk = stop − entry)
    minute += dt.timedelta(minutes=1)
    await collector.on_minute(minute, 136.0, [bar(136.0, high=zone.all_low + 1)], runtime)
    closed = rows(repository)[0]
    assert closed["status"] == "closed_stop"
    assert float(cast(float, closed["outcome_r"])) == pytest.approx(-1.0)


async def test_timeout_na_settle() -> None:
    """Settle uzavírá otevřené proby za close — jako živé setupy."""
    collector, repository = make_collector()
    runtime = runtime_with_profile()
    for offset in range(ACCEPTANCE_MINUTES):
        await collector.on_minute(
            NOON + dt.timedelta(minutes=offset - ACCEPTANCE_MINUTES), 120.0, [bar(120.0)], runtime
        )
    minute = NOON
    for _ in range(ACCEPTANCE_MINUTES):
        minute += dt.timedelta(minutes=1)
        await collector.on_minute(minute, 138.0, [bar(138.0)], runtime)
    assert rows(repository)[0]["status"] == "active"
    evening = NOON.replace(hour=21, minute=30)  # po settle v létě i zimě
    await collector.on_minute(evening, 139.0, [bar(139.0)], runtime)
    closed = rows(repository)[0]
    assert closed["status"] == "closed_timeout"
    assert closed["closed_ts"] is not None
