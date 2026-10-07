"""Sběrač výskytů kandidáta T9 (#577, fáze 1): zóna, orchestrace, uzávěrky.

Rozhodovací logiku (přechody, akceptace, síla pásma) hlídá
`test_damping_ceiling.py` nad čistou funkcí; tady se testuje jen to, co
sběrač přidává — zápis do `setup_probes`, vyhodnocení otevřených sond a
timeout podle expirace runtime.
"""

import datetime as dt
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import create_engine, select

from gexlens_engine.compute.bandregime import BAND_METRICS_VERSION, band_zone
from gexlens_engine.compute.gexfield import GexProfile
from gexlens_engine.compute.settle import expiry_settle
from gexlens_engine.compute.setups import ProbeParams, band_position
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.probes import T9ProbeCollector
from gexlens_engine.runtime import EngineRuntime
from gexlens_engine.storage.probes_store import ProbeRepository, setup_probes

# Poledne UTC — bezpečně před settle (20:00/21:00 UTC), timeout nezasahuje
NOON = dt.datetime(2026, 8, 27, 12, 0, tzinfo=dt.UTC)
ACCEPT = ProbeParams().acceptance_minutes


def profile() -> GexProfile:
    """Hrb 140–160 na mřížce 100–190: zóna All ≈ 135–165, jádro na 150."""
    return GexProfile(
        ts_min=NOON,
        grid_start=100.0,
        grid_step=10.0,
        values=(0.0, 0.0, 0.0, 0.0, 8.0, 10.0, 8.0, 0.0, 0.0, 0.0),
    )


def bar(
    close: float, high: float | None = None, low: float | None = None, at: dt.datetime = NOON
) -> Bar:
    """Bar minuty před cyklem `at` (dávka cyklu N nese bar N−1)."""
    return Bar(
        ts=at - dt.timedelta(minutes=1),
        open=close,
        high=high if high is not None else close,
        low=low if low is not None else close,
        close=close,
        volume=100.0,
    )


def runtime_with_profile(
    expiry: str = "20260827", trading_class: str | None = None
) -> EngineRuntime:
    return cast(
        EngineRuntime,
        SimpleNamespace(
            last_profile=profile(),
            expiry=expiry,
            symbol="ES",
            trading_class=trading_class,
            settle=lambda: expiry_settle(expiry, trading_class, "ES"),
        ),
    )


def make_collector(
    partition: dict[dt.datetime, Bar] | None = None,
) -> tuple[T9ProbeCollector, ProbeRepository]:
    """Sběrač; `partition` = bary „z partic“ podle minuty (díry v živé dávce, #1345)."""
    repository = ProbeRepository(create_engine("sqlite+pysqlite:///:memory:"))
    repository.ensure_schema()

    def reader(since: dt.datetime, until: dt.datetime) -> list[Bar]:
        return [b for ts, b in sorted((partition or {}).items()) if since < ts <= until]

    collector = T9ProbeCollector(
        symbol="ES", repository=repository, bar_reader=reader if partition is not None else None
    )
    return collector, repository


def closed_at(row: dict[str, object]) -> dt.datetime:
    """`closed_ts` řádku sondy jako UTC (sqlite vrací naivní čas)."""
    value = cast(dt.datetime, row["closed_ts"])
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def flat_partition(start: dt.datetime, end: dt.datetime, close: float) -> dict[dt.datetime, Bar]:
    """Souvislé bary [start, end) — partice, kterou živá dávka nepokryla."""
    out: dict[dt.datetime, Bar] = {}
    ts = start
    while ts < end:
        out[ts] = Bar(ts=ts, open=close, high=close, low=close, close=close, volume=1.0)
        ts += dt.timedelta(minutes=1)
    return out


def rows(repository: ProbeRepository) -> list[dict[str, object]]:
    with repository._engine.connect() as conn:
        return [dict(row._mapping) for row in conn.execute(select(setup_probes))]


async def settle_below_then_enter(
    collector: T9ProbeCollector, runtime: EngineRuntime, start: dt.datetime = NOON
) -> dt.datetime:
    """Usazení pod pásmem, pak akceptovaný vstup dovnitř; vrací poslední minutu."""
    for offset in range(ACCEPT):
        await collector.on_minute(
            start + dt.timedelta(minutes=offset - ACCEPT),
            120.0,
            [bar(120.0, at=start + dt.timedelta(minutes=offset - ACCEPT))],
            runtime,
        )
    minute = start
    for _ in range(ACCEPT):
        minute += dt.timedelta(minutes=1)
        await collector.on_minute(minute, 138.0, [bar(138.0, at=minute)], runtime)
    return minute


def test_band_position_mapping() -> None:
    zone = band_zone(profile(), 120.0)
    assert zone is not None
    assert band_position(None, 120.0) == "unknown"
    assert band_position(zone, zone.all_low - 1) == "below"
    assert band_position(zone, zone.center) == "in"
    assert band_position(zone, zone.all_high + 1) == "above"


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
    # Ověření předpokladů scénáře nad geometrií — ne nad čísly z hlavy
    reference = band_zone(profile(), 120.0)
    assert reference is not None
    assert band_position(reference, 120.0) == "below"
    assert band_position(band_zone(profile(), 138.0), 138.0) == "in"
    minute = await settle_below_then_enter(collector, runtime)
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
    context = cast(dict[str, object], probe["context"])
    assert context["expiry"] == "20260827"
    assert context["band_metrics_version"] == BAND_METRICS_VERSION
    assert "transition_ts" in context
    # Bar protne cíl → uzávěrka stejnou mechanikou jako živé setupy
    minute += dt.timedelta(minutes=1)
    await collector.on_minute(minute, 150.0, [bar(150.0, high=zone.center + 1, at=minute)], runtime)
    closed = rows(repository)[0]
    assert closed["status"] == "closed_target"
    assert float(cast(float, closed["outcome_r"])) > 0
    assert float(cast(float, closed["mfe"])) >= zone.center + 1 - 138.0


async def test_okamzite_vraceny_prechod_se_zahazuje() -> None:
    """Návrat pod hranu před akceptací = žádný výskyt (podmínka 3 z #577)."""
    collector, repository = make_collector()
    runtime = runtime_with_profile()
    for offset in range(ACCEPT):
        await collector.on_minute(
            NOON + dt.timedelta(minutes=offset - ACCEPT),
            120.0,
            [bar(120.0, at=NOON + dt.timedelta(minutes=offset - ACCEPT))],
            runtime,
        )
    await collector.on_minute(
        NOON + dt.timedelta(minutes=1),
        138.0,
        [bar(138.0, at=NOON + dt.timedelta(minutes=1))],
        runtime,
    )
    await collector.on_minute(
        NOON + dt.timedelta(minutes=2),
        120.0,
        [bar(120.0, at=NOON + dt.timedelta(minutes=2))],
        runtime,
    )
    for offset in range(3, 3 + ACCEPT):
        await collector.on_minute(
            NOON + dt.timedelta(minutes=offset),
            120.0,
            [bar(120.0, at=NOON + dt.timedelta(minutes=offset))],
            runtime,
        )
    assert rows(repository) == []


async def test_exit_probe_zrcadlo() -> None:
    """Výpad z pásma dolů = momentum SHORT; návrat nad hranu = stop."""
    collector, repository = make_collector()
    runtime = runtime_with_profile()
    # Usazená výchozí poloha uvnitř pásma
    for offset in range(ACCEPT):
        await collector.on_minute(
            NOON + dt.timedelta(minutes=offset - ACCEPT),
            140.0,
            [bar(140.0, at=NOON + dt.timedelta(minutes=offset - ACCEPT))],
            runtime,
        )
    minute = NOON
    for _ in range(ACCEPT):
        minute += dt.timedelta(minutes=1)
        await collector.on_minute(minute, 128.0, [bar(128.0, at=minute)], runtime)
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
    await collector.on_minute(
        minute, 136.0, [bar(136.0, high=zone.all_low + 1, at=minute)], runtime
    )
    closed = rows(repository)[0]
    assert closed["status"] == "closed_stop"
    assert float(cast(float, closed["outcome_r"])) == pytest.approx(-1.0)


async def test_timeout_na_settle_expirace() -> None:
    """Settle expirace sondy uzavírá timeoutem za close baru končícího v settle,
    `closed_ts` = settle — jako živé setupy (#1345). Díru mezi poslední živou
    minutou a večerním cyklem dotáhne z partic."""
    partition = flat_partition(NOON, NOON.replace(hour=21), 139.5)
    collector, repository = make_collector(partition)
    runtime = runtime_with_profile(expiry="20260827")
    await settle_below_then_enter(collector, runtime)
    assert rows(repository)[0]["status"] == "active"
    evening = NOON.replace(hour=21, minute=30)  # po settle 20:00 UTC
    await collector.on_minute(evening, 139.0, [bar(139.0, at=evening)], runtime)
    closed = rows(repository)[0]
    assert closed["status"] == "closed_timeout"
    settle = expiry_settle("20260827")
    assert settle is not None
    assert closed_at(closed) == settle
    # Výstup = close baru 19:59 z partice (139,5), ne cena večerního cyklu
    entry = float(cast(float, closed["entry"]))
    stop = float(cast(float, closed["stop"]))
    assert float(cast(float, closed["outcome_r"])) == pytest.approx(
        (139.5 - entry) / (entry - stop)
    )


async def test_stop_v_dire_zive_davky_se_neztrati() -> None:
    """#1345: cyklus bez barů (výpadek streamu) dřív sondu přeskočil a stop
    v díře se ztratil; teď ho cesta najde v partici s časem jeho baru."""
    collector, repository = make_collector({})
    runtime = runtime_with_profile()
    minute = await settle_below_then_enter(collector, runtime)
    probe = rows(repository)[0]
    crash_ts = minute + dt.timedelta(minutes=2)
    low = float(cast(float, probe["stop"])) - 1
    collector.bar_reader = lambda since, until: [
        b
        for b in (
            Bar(ts=minute, open=138, high=138, low=138, close=138, volume=1),
            Bar(
                ts=minute + dt.timedelta(minutes=1),
                open=138,
                high=138,
                low=137,
                close=137,
                volume=1,
            ),
            Bar(ts=crash_ts, open=137, high=137, low=low, close=low, volume=1),
        )
        if since < b.ts <= until
    ]
    later = crash_ts + dt.timedelta(minutes=5)
    await collector.on_minute(later, 139.0, [], runtime)  # cyklus bez barů
    closed = rows(repository)[0]
    assert closed["status"] == "closed_stop"
    assert closed_at(closed) == crash_ts
    assert float(cast(float, closed["outcome_r"])) == pytest.approx(-1.0)


async def test_nedoplnena_dira_ceka_a_pak_se_prejde() -> None:
    """Díra bez partice: sonda čeká `PATH_GAP_WAIT`, pak hodnotí bez ní (jako setupy)."""
    from gexlens_engine.compute.setups import PATH_GAP_WAIT

    partition: dict[dt.datetime, Bar] = {}
    collector, repository = make_collector(partition)
    runtime = runtime_with_profile()
    minute = await settle_below_then_enter(collector, runtime)
    zone = band_zone(profile(), 138.0)
    assert zone is not None
    after_gap = minute + dt.timedelta(minutes=5)
    target_bar = [bar(150.0, high=zone.center + 1, at=after_gap)]
    # Bar cíle engine zapíše do partice; minuty díry v ní nejsou
    partition[target_bar[0].ts] = target_bar[0]
    await collector.on_minute(after_gap, 150.0, target_bar, runtime)
    assert rows(repository)[0]["status"] == "active"  # čeká na díru
    later = after_gap + PATH_GAP_WAIT
    await collector.on_minute(later, 150.0, [bar(150.0, at=later)], runtime)
    closed = rows(repository)[0]
    assert closed["status"] == "closed_target"
    assert closed_at(closed) == after_gap - dt.timedelta(minutes=1)


async def test_sonda_po_rollu_prezije_settle_kalendarniho_dne() -> None:
    """Runtime už jede na zítřejší expiraci → večerní sonda čeká na zítřejší settle.

    Do zavedení čisté funkce se sondy zavíraly settlem kalendářního dne, takže
    každý výskyt po 20:00 UTC skončil další minutou jako timeout (2 z 11 řádků
    produkce). Timeout se řídí expirací runtime, stejně jako `SetupEngine`.
    """
    collector, repository = make_collector()
    runtime = runtime_with_profile(expiry="20260828")
    evening = NOON.replace(hour=22, minute=0)  # Globex po rollu, 27. 8.
    await settle_below_then_enter(collector, runtime, start=evening)
    await collector.on_minute(
        evening + dt.timedelta(minutes=10),
        139.0,
        [bar(139.0, at=evening + dt.timedelta(minutes=10))],
        runtime,
    )
    assert rows(repository)[0]["status"] == "active"
    next_settle = expiry_settle("20260828")
    assert next_settle is not None
    await collector.on_minute(next_settle, 139.0, [bar(139.0, at=next_settle)], runtime)
    assert rows(repository)[0]["status"] == "closed_timeout"


async def test_sonda_nevznikne_po_settle_expirace() -> None:
    """Invariant `born_after_settle` jako setupy (#1324, #1331).

    Pipeline proběhne ještě cyklem minuty settle a teprve pak roluje — výskyt
    v něm se vztahuje k vypršelému řetězu a timeout by ho zavřel hned.
    """
    collector, repository = make_collector()
    runtime = runtime_with_profile(expiry="20260827")
    settle = expiry_settle("20260827")
    assert settle == dt.datetime(2026, 8, 27, 20, 0, tzinfo=dt.UTC)
    # Akceptovaný vstup padne přesně na minutu settle
    await settle_below_then_enter(collector, runtime, start=settle - dt.timedelta(minutes=ACCEPT))
    assert rows(repository) == []
    # Týž průběh minutu před settle sondu otevře — brání jen hranice
    collector, repository = make_collector()
    early = settle - dt.timedelta(minutes=ACCEPT + 1)
    await settle_below_then_enter(collector, runtime, start=early)
    assert [row["status"] for row in rows(repository)] == ["active"]


async def test_sonda_nad_necitelnou_expiraci_nevznikne() -> None:
    """Bez settle by sonda nikdy nedostala timeout — neotevře se (nahlas)."""
    collector, repository = make_collector()
    await settle_below_then_enter(collector, runtime_with_profile(expiry="neplatná"))
    assert rows(repository) == []


async def test_kvartalni_patek_sonda_tydenni_serie_zije_do_odpoledne() -> None:
    """#1366: na kvartální datum běží po rollu týdenní série nového kontraktu
    (EW3, settle 16:00 ET) — sonda v 11:00 ET vznikne, nese trading class
    a timeout dostane v 16:00 ET. Nad standardní třídou (ES, SOQ 9:30 ET) ne."""
    late_morning = dt.datetime(2026, 9, 18, 15, 0, tzinfo=dt.UTC)  # 11:00 EDT, po SOQ
    collector, repository = make_collector(
        flat_partition(late_morning, dt.datetime(2026, 9, 18, 21, 0, tzinfo=dt.UTC), 139.5)
    )
    weekly = runtime_with_profile(expiry="20260918", trading_class="EW3")
    await settle_below_then_enter(collector, weekly, start=late_morning)
    opened = rows(repository)
    assert [row["status"] for row in opened] == ["active"]
    assert cast(dict[str, object], opened[0]["context"])["trading_class"] == "EW3"
    evening = dt.datetime(2026, 9, 18, 20, 30, tzinfo=dt.UTC)
    await collector.on_minute(evening, 139.0, [bar(139.0, at=evening)], weekly)
    closed = rows(repository)[0]
    assert closed["status"] == "closed_timeout"
    assert closed_at(closed) == dt.datetime(2026, 9, 18, 20, 0, tzinfo=dt.UTC)  # 16:00 EDT

    collector, repository = make_collector()
    standard = runtime_with_profile(expiry="20260918", trading_class="ES")
    await settle_below_then_enter(collector, standard, start=late_morning)
    assert rows(repository) == []
