"""Trade větev CumΔ (#1460, ADR-0057 body 3 a 6): trasy tisků, predikát běhu, vzorky.

Mechaniku v6 volí explicitní zdroj trackeru (`SOURCE_AGGRESSOR`), v5 midpoint —
žádný monkeypatch `CUMDELTA_PRINTS`.
"""

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass, field

import pytest

from gexlens_engine.compute.cumdelta import SOURCE_AGGRESSOR, SOURCE_MIDPOINT, CumDeltaTracker
from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.tasty.trade_branch import (
    BRANCH_CLOSED,
    BRANCH_GAP,
    BRANCH_OFF,
    BRANCH_RUNNING,
    TradeBranchMonitor,
    TradeRoutes,
    build_routes,
    observe_without_branch,
    route_trade,
    trade_branch_running,
)

SEC = dt.timedelta(seconds=1)
MIN = dt.timedelta(minutes=1)
#: Úterý 20. 10. 2026 11:00 ET — US RTH (CDT)
RTH = dt.datetime(2026, 10, 20, 15, 0, tzinfo=dt.UTC)
#: Úterý 20. 10. 2026 3:00 UTC = pondělí 22:00 CT — Globex noc seance úterý
NIGHT = dt.datetime(2026, 10, 20, 3, 0, tzinfo=dt.UTC)
#: Sobota 24. 10. 2026
SATURDAY = dt.datetime(2026, 10, 24, 15, 0, tzinfo=dt.UTC)
#: Úterý 20. 10. 2026 16:30 CT — denní pauza
PAUSE = dt.datetime(2026, 10, 20, 21, 30, tzinfo=dt.UTC)
#: Denní open úterý 17:00 CT (CDT) — seance středy
REOPEN = dt.datetime(2026, 10, 20, 22, 0, tzinfo=dt.UTC)
#: Pátek 23. 10. 2026 15:59 CT — poslední event týdne
FRIDAY_LAST = dt.datetime(2026, 10, 23, 20, 59, tzinfo=dt.UTC)
#: Nedělní open 17:00 CT: 25. 10. v CDT, 1. 11. (den přechodu) už v CST
SUNDAY_OPEN_CDT = dt.datetime(2026, 10, 25, 22, 0, tzinfo=dt.UTC)
SUNDAY_OPEN_CST = dt.datetime(2026, 11, 1, 23, 0, tzinfo=dt.UTC)

ACTIVE = "20261020"
NEXT = "20261021"
AFTER_NEXT = "20261022"


def spec(expiry: str, right: str = "C", strike: float = 6700.0) -> OptionContractSpec:
    return OptionContractSpec(
        symbol="ES",
        sec_type="FOP",
        expiry=expiry,
        strike=strike,
        right=right,
        exchange="CME",
        trading_class="E3D",
        multiplier="50",
    )


def streamer_of(contract: OptionContractSpec) -> str | None:
    return f"./E{contract.expiry}{contract.right}{contract.strike:.0f}:XCME"


@dataclass
class FakeRuntime:
    expiry: str
    contracts: list[OptionContractSpec]
    cum_delta: CumDeltaTracker | None


@dataclass
class FakePipeline:
    runtime: FakeRuntime
    next_runtime: FakeRuntime | None = None


def runtime(expiry: str, source: str = SOURCE_AGGRESSOR) -> FakeRuntime:
    return FakeRuntime(
        expiry=expiry,
        contracts=[spec(expiry, "C"), spec(expiry, "P")],
        cum_delta=CumDeltaTracker(multiplier=50.0, source=source),
    )


def pipeline(
    active: str = ACTIVE, nxt: str | None = NEXT, source: str = SOURCE_AGGRESSOR
) -> FakePipeline:
    return FakePipeline(
        runtime=runtime(active, source),
        next_runtime=runtime(nxt, source) if nxt is not None else None,
    )


def tracker_of(rt: FakeRuntime | None) -> CumDeltaTracker:
    assert rt is not None and rt.cum_delta is not None
    return rt.cum_delta


@dataclass
class Branch:
    """Stav tasty větve pro `TradeBranchMonitor.sample` — připojení, reconnecty, eventy."""

    connected: bool = True
    reconnects: int = 0
    last_event: dict[str, dt.datetime] = field(default_factory=dict)

    def last_event_among(self, streamers: Iterable[str]) -> dt.datetime | None:
        times = [self.last_event[s] for s in streamers if s in self.last_event]
        return max(times) if times else None

    def touch(self, routes: TradeRoutes, at: dt.datetime) -> None:
        """Event (např. Quote) pro každý streamer obou částí tras v čase `at`."""
        for streamer in [*routes.active, *routes.next]:
            self.last_event[streamer] = at


def sample(
    monitor: TradeBranchMonitor,
    now: dt.datetime,
    pipes: dict[str, FakePipeline],
    routes: dict[str, TradeRoutes],
    branch: Branch,
) -> dict[str, str]:
    return monitor.sample(
        now,
        pipes,
        routes,
        connected=branch.connected,
        reconnects=branch.reconnects,
        last_event_among=branch.last_event_among,
    )


# ── Predikát „trade větev běží" ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("now", "silence_s", "running"),
    [
        (RTH, 179, True),
        (RTH, 181, False),
        (NIGHT, 599, True),
        (NIGHT, 601, False),
    ],
    ids=["rth-179s", "rth-181s", "globex-599s", "globex-601s"],
)
def test_prah_ticha_rth_180_s_globex_600_s(now: dt.datetime, silence_s: int, running: bool) -> None:
    """Prahy stávajícího hlídače ticha (#1228), bez nové konstanty (ADR-0057 bod 3)."""
    last = now - silence_s * SEC
    assert trade_branch_running(now, connected=True, owned=True, last_event_at=last) is running


def test_bez_spojeni_nebo_vlastnika_trasy_nebezi() -> None:
    fresh = RTH - SEC
    assert not trade_branch_running(RTH, connected=False, owned=True, last_event_at=fresh)
    assert not trade_branch_running(RTH, connected=True, owned=False, last_event_at=fresh)
    # Bez jediného eventu uprostřed seance se ticho měří od openu → mezera
    assert not trade_branch_running(RTH, connected=True, owned=True, last_event_at=None)


@pytest.mark.parametrize("open_ts", [SUNDAY_OPEN_CDT, SUNDAY_OPEN_CST], ids=["cdt", "cst"])
def test_nedelni_open_s_poslednim_eventem_z_patku_bezi(open_ts: dt.datetime) -> None:
    """Ticho se měří nejdřív od openu seance — páteční event mezeru v openu nedá."""

    def running(now: dt.datetime, last: dt.datetime | None) -> bool:
        return trade_branch_running(now, connected=True, owned=True, last_event_at=last)

    assert running(open_ts + 30 * SEC, FRIDAY_LAST)
    assert running(open_ts + 30 * SEC, None)
    assert running(open_ts + 599 * SEC, FRIDAY_LAST)
    # Univerzum od openu mlčí déle než práh Globexu → mezera
    assert not running(open_ts + 601 * SEC, FRIDAY_LAST)
    assert running(open_ts + 601 * SEC, open_ts + 5 * MIN)


def test_denni_open_17_ct_s_eventem_z_pauzy_bezi() -> None:
    last_before_pause = REOPEN - 60 * MIN  # 16:00 CT
    assert trade_branch_running(
        REOPEN + 2 * MIN, connected=True, owned=True, last_event_at=last_before_pause
    )
    assert not trade_branch_running(
        REOPEN + 601 * SEC, connected=True, owned=True, last_event_at=last_before_pause
    )


# ── Vzorky trackerům (TradeBranchMonitor) ───────────────────────────────


def warm_up(
    monitor: TradeBranchMonitor,
    now: dt.datetime,
    pipes: dict[str, FakePipeline],
    routes: dict[str, TradeRoutes],
    branch: Branch,
) -> None:
    """Start trackerů za otevřeného trhu (první minuta je mezera) a jedna měřená minuta."""
    for offset in (0, 1):
        for routes_part in routes.values():
            branch.touch(routes_part, now + offset * MIN)
        sample(monitor, now + offset * MIN, pipes, routes, branch)
        for pipe in pipes.values():
            for rt in (pipe.runtime, pipe.next_runtime):
                tracker_of(rt).begin_cycle()
                # Hranice prvního okna po startu procesu není známá → mezera
                assert tracker_of(rt).minute_measured is (offset == 1)
                tracker_of(rt).close_minute(now + offset * MIN)


@pytest.mark.parametrize("now", [SATURDAY, PAUSE], ids=["sobota", "pauza"])
def test_zavreny_trh_stav_closed_a_minuta_bez_mezery(now: dt.datetime) -> None:
    """Víkend a denní pauza: odpojený stream ani ticho mezeru nedají (#968, #1307)."""
    pipe = pipeline()
    pipes = {"ES": pipe}
    routes = {"ES": build_routes(pipe, streamer_of)}
    branch = Branch(connected=False)
    states = sample(TradeBranchMonitor(), now, pipes, routes, branch)
    assert states == {"ES": BRANCH_CLOSED}
    for rt in (pipe.runtime, pipe.next_runtime):
        tracker_of(rt).begin_cycle()
        assert tracker_of(rt).minute_measured
        assert tracker_of(rt).close_minute(now).flow_delta == 0.0


def test_nedelni_open_po_vikendu_prvni_minuta_merena() -> None:
    """Vzorek ze soboty je hraniční vzorek openu; páteční event mezeru nedá."""
    pipe = pipeline(active="20261026", nxt="20261027")
    pipes = {"ES": pipe}
    routes = {"ES": build_routes(pipe, streamer_of)}
    branch = Branch()
    for streamer in [*routes["ES"].active, *routes["ES"].next]:
        branch.last_event[streamer] = FRIDAY_LAST
    monitor = TradeBranchMonitor()
    assert sample(monitor, SUNDAY_OPEN_CDT - MIN, pipes, routes, branch) == {"ES": BRANCH_CLOSED}
    assert sample(monitor, SUNDAY_OPEN_CDT + 30 * SEC, pipes, routes, branch) == {
        "ES": BRANCH_RUNNING
    }
    for rt in (pipe.runtime, pipe.next_runtime):
        tracker_of(rt).begin_cycle()
        assert tracker_of(rt).minute_measured


def test_mlcici_univerzum_za_otevreneho_trhu_je_mezera() -> None:
    pipe = pipeline()
    pipes = {"ES": pipe}
    routes = {"ES": build_routes(pipe, streamer_of)}
    branch = Branch()
    monitor = TradeBranchMonitor()
    warm_up(monitor, RTH, pipes, routes, branch)
    # Poslední event 181 s před vzorkem (RTH práh 180 s)
    now = RTH + 5 * MIN
    branch.touch(routes["ES"], now - 181 * SEC)
    assert sample(monitor, now, pipes, routes, branch) == {"ES": BRANCH_GAP}
    primary = tracker_of(pipe.runtime)
    primary.begin_cycle()
    assert not primary.minute_measured
    assert primary.close_minute(now).cum_delta is None


def test_reconnect_mezi_vzorky_je_mezera_jen_jedne_minuty() -> None:
    """Pád streamu mezi vzorky: tisky z výpadku chybí, i když teď stream zase běží."""
    pipe = pipeline()
    pipes = {"ES": pipe}
    routes = {"ES": build_routes(pipe, streamer_of)}
    branch = Branch()
    monitor = TradeBranchMonitor()
    warm_up(monitor, RTH, pipes, routes, branch)
    primary, secondary = tracker_of(pipe.runtime), tracker_of(pipe.next_runtime)

    now = RTH + 2 * MIN
    branch.reconnects = 1  # pád a nové připojení během minuty
    branch.touch(routes["ES"], now)
    assert sample(monitor, now, pipes, routes, branch) == {"ES": BRANCH_GAP}
    primary.begin_cycle()
    assert not primary.minute_measured
    primary.close_minute(now)

    # Další minuta už je celá s běžícím streamem — měřená
    now += MIN
    branch.touch(routes["ES"], now)
    assert sample(monitor, now, pipes, routes, branch) == {"ES": BRANCH_RUNNING}
    primary.begin_cycle()
    assert primary.minute_measured
    # Sekundár s cyklem po N minutách: výpadek v okně je mezera celého okna
    secondary.begin_cycle()
    assert not secondary.minute_measured


def test_pad_mezi_vzorkem_a_uzaverkou_nese_dalsi_radek() -> None:
    """Uzávěrka minuty běží až po sweepu: pád streamu po vzorku M patří řádku M+1.

    Řádek M zůstane měřený jen s tisky do řezu ve vzorku M; řádek M+1 (vzorek
    vidí změnu reconnectů) dostane NULL a vrátí net jen tisků po řezu.
    """
    pipe = pipeline()
    pipes = {"ES": pipe}
    routes = {"ES": build_routes(pipe, streamer_of)}
    branch = Branch()
    monitor = TradeBranchMonitor()
    warm_up(monitor, RTH, pipes, routes, branch)
    primary = tracker_of(pipe.runtime)
    call = spec(ACTIVE, "C")

    now = RTH + 2 * MIN
    primary.add_dx_trade(call, size=2.0, aggressor="BUY", delta=0.5)  # +50 před vzorkem M
    branch.touch(routes["ES"], now)
    assert sample(monitor, now, pipes, routes, branch) == {"ES": BRANCH_RUNNING}
    primary.begin_cycle()
    assert primary.minute_measured
    branch.reconnects = 1  # pád a nové připojení během sweepu cyklu M
    primary.add_dx_trade(call, size=4.0, aggressor="BUY", delta=0.5)  # po reconnectu
    row = primary.close_minute(now)
    assert (row.flow_delta, row.cum_delta, row.printed_volume) == (50.0, 50.0, 2.0)
    assert primary.net_volumes() == {call: 2.0}

    now += MIN
    branch.touch(routes["ES"], now)
    assert sample(monitor, now, pipes, routes, branch) == {"ES": BRANCH_GAP}
    primary.begin_cycle()
    assert not primary.minute_measured
    assert primary.close_minute(now).cum_delta is None
    assert primary.net_volumes() == {call: 2.0}  # vrátily se jen tisky po řezu
    assert primary.cum_delta == 50.0


def test_po_rollu_zastarale_trasy_jsou_mezera_do_obnovy() -> None:
    """Trasy patří vypršelému řetězu (ADR-0057 bod 3): mezera, i když eventy chodí."""
    old = pipeline(active=ACTIVE, nxt=NEXT)
    stale = {"ES": build_routes(old, streamer_of)}
    rolled = pipeline(active=NEXT, nxt=AFTER_NEXT)
    pipes = {"ES": rolled}
    branch = Branch()
    branch.touch(stale["ES"], RTH)
    monitor = TradeBranchMonitor()
    assert sample(monitor, RTH, pipes, stale, branch) == {"ES": BRANCH_GAP}

    fresh = {"ES": build_routes(rolled, streamer_of)}
    branch.touch(fresh["ES"], RTH + MIN)
    assert sample(monitor, RTH + MIN, pipes, fresh, branch) == {"ES": BRANCH_RUNNING}
    branch.touch(fresh["ES"], RTH + 2 * MIN)
    sample(monitor, RTH + 2 * MIN, pipes, fresh, branch)
    for rt in (rolled.runtime, rolled.next_runtime):
        tracker_of(rt).begin_cycle()
        # Okno obsahuje vzorek se zastaralými trasami → ještě mezera
        assert not tracker_of(rt).minute_measured
        tracker_of(rt).close_minute(RTH + 2 * MIN)
    branch.touch(fresh["ES"], RTH + 3 * MIN)
    sample(monitor, RTH + 3 * MIN, pipes, fresh, branch)
    for rt in (rolled.runtime, rolled.next_runtime):
        tracker_of(rt).begin_cycle()
        assert tracker_of(rt).minute_measured


def test_sekundar_meri_vlastni_univerzum_a_and_pres_okno() -> None:
    """Sekundár dostává vzorek každou minutu; jeho čerstvost je z jeho univerza."""
    pipe = pipeline()
    pipes = {"ES": pipe}
    routes = {"ES": build_routes(pipe, streamer_of)}
    branch = Branch()
    monitor = TradeBranchMonitor()
    warm_up(monitor, RTH, pipes, routes, branch)
    secondary = tracker_of(pipe.next_runtime)
    for minute in (2, 3, 4):
        now = RTH + minute * MIN
        for streamer in routes["ES"].active:
            branch.last_event[streamer] = now
        if minute != 3:  # v minutě 3 sekundár mlčí 181 s (RTH práh)
            for streamer in routes["ES"].next:
                branch.last_event[streamer] = now
        else:
            for streamer in routes["ES"].next:
                branch.last_event[streamer] = now - 181 * SEC
        # Aktivní řetěz běží celou dobu → stav aktivního řetězu running
        assert sample(monitor, now, pipes, routes, branch) == {"ES": BRANCH_RUNNING}
    secondary.begin_cycle()
    assert not secondary.minute_measured
    secondary.close_minute(RTH + 4 * MIN)
    for minute in (5, 6, 7):
        branch.touch(routes["ES"], RTH + minute * MIN)
        sample(monitor, RTH + minute * MIN, pipes, routes, branch)
    secondary.begin_cycle()
    assert secondary.minute_measured


def test_symbol_bez_tras_je_mezera() -> None:
    pipe = pipeline(nxt=None)
    states = sample(TradeBranchMonitor(), RTH, {"ES": pipe}, {}, Branch())
    assert states == {"ES": BRANCH_GAP}


def test_bez_tasty_vetve_stav_off() -> None:
    """Bez tasty větve trade větev neběží: za otevřeného trhu mezera, za zavřeného nula."""
    pipe = pipeline()
    assert observe_without_branch(RTH, {"ES": pipe}) == {"ES": BRANCH_OFF}
    tracker_of(pipe.runtime).begin_cycle()
    assert not tracker_of(pipe.runtime).minute_measured

    closed = pipeline()
    assert observe_without_branch(SATURDAY, {"ES": closed}) == {"ES": BRANCH_OFF}
    for rt in (closed.runtime, closed.next_runtime):
        tracker_of(rt).begin_cycle()
        assert tracker_of(rt).minute_measured


def test_midpoint_vzorky_neovlivni() -> None:
    """Mechanika v5: mezera trade větve minutu midpointu nezmění (verdikt nečte)."""
    pipe = pipeline(source=SOURCE_MIDPOINT)
    states = sample(TradeBranchMonitor(), RTH, {"ES": pipe}, {}, Branch(connected=False))
    assert states == {"ES": BRANCH_GAP}  # stav v /status je vidět i pod v5
    for rt in (pipe.runtime, pipe.next_runtime):
        tracker_of(rt).begin_cycle()
        assert tracker_of(rt).minute_measured


# ── Trasy tisků ─────────────────────────────────────────────────────────


def test_build_routes_rozdeli_aktivni_a_sekundarni_cast() -> None:
    """Stín `cumdelta_dx` dostává jen aktivní část (vstup replaye, ADR-0057 bod 7)."""
    pipe = pipeline()
    pipe.runtime.contracts.append(spec(ACTIVE, "C", 9999.0))

    def without_far(contract: OptionContractSpec) -> str | None:
        return None if contract.strike == 9999.0 else streamer_of(contract)

    routes = build_routes(pipe, without_far)
    assert routes.active_expiry == ACTIVE and routes.next_expiry == NEXT
    assert sorted(routes.active.values(), key=lambda s: s.right) == [
        spec(ACTIVE, "C"),
        spec(ACTIVE, "P"),
    ]
    assert {s.expiry for s in routes.next.values()} == {NEXT}
    assert not set(routes.active) & set(routes.next)

    alone = build_routes(pipeline(nxt=None), streamer_of)
    assert alone.next_expiry is None and alone.next == {}


def test_route_trade_v6_aktivni_i_sekundarni_retez() -> None:
    """Tisk sekundáru jde do jeho trackeru — čistý objem sekundáru je vstup α (bod 6)."""
    pipe = pipeline()
    routes = build_routes(pipe, streamer_of)
    primary, secondary = tracker_of(pipe.runtime), tracker_of(pipe.next_runtime)

    target = route_trade(routes, pipe, streamer_of(spec(ACTIVE, "C")) or "")
    assert target == (primary, spec(ACTIVE, "C"))
    target = route_trade(routes, pipe, streamer_of(spec(NEXT, "P")) or "")
    assert target is not None and target[0] is secondary
    tracker, contract = target
    tracker.add_dx_trade(contract, size=7.0, aggressor="SELL", delta=-0.3)
    secondary.seal()  # net jde do denního řezem ve vzorku
    assert secondary.net_volume(spec(NEXT, "P")) == -7.0
    assert primary.net_volumes() == {}


def test_route_trade_v6_zastarale_trasy_po_rollu_zahodi() -> None:
    """Po rollu nesmí tisky D+1 ze staré sekundární části padnout do nového aktivního."""
    stale = build_routes(pipeline(active=ACTIVE, nxt=NEXT), streamer_of)
    rolled = pipeline(active=NEXT, nxt=AFTER_NEXT)
    assert route_trade(stale, rolled, streamer_of(spec(ACTIVE, "C")) or "") is None
    assert route_trade(stale, rolled, streamer_of(spec(NEXT, "C")) or "") is None


def test_route_trade_v5_doruci_jako_pred_1460() -> None:
    """Mechanika v5: jen aktivní část do aktivního trackeru, i nad zastaralými trasami.

    Sekundární část nedoručuje nikam — ani po rollu tisky D+1 do nového
    sekundárního trackeru D+2 se specem jiné expirace (review 1a).
    """
    stale = build_routes(pipeline(active=ACTIVE, nxt=NEXT, source=SOURCE_MIDPOINT), streamer_of)
    rolled = pipeline(active=NEXT, nxt=AFTER_NEXT, source=SOURCE_MIDPOINT)
    target = route_trade(stale, rolled, streamer_of(spec(ACTIVE, "C")) or "")
    assert target == (tracker_of(rolled.runtime), spec(ACTIVE, "C"))
    assert route_trade(stale, rolled, streamer_of(spec(NEXT, "C")) or "") is None

    pipe = pipeline(source=SOURCE_MIDPOINT)
    routes = build_routes(pipe, streamer_of)
    assert route_trade(routes, pipe, streamer_of(spec(NEXT, "C")) or "") is None
    target = route_trade(routes, pipe, streamer_of(spec(ACTIVE, "P")) or "")
    assert target == (tracker_of(pipe.runtime), spec(ACTIVE, "P"))
    tracker, contract = target
    tracker.add_dx_trade(contract, size=5.0, aggressor="BUY", delta=-0.4)
    # Midpoint: tisk jen měří pokrytí, čistý objem dává bar větev
    assert tracker.net_volumes() == {}
    assert tracker.cum_delta == 0.0
    assert tracker.day_stats()["printed_volume"] == 5.0


def test_route_trade_neznamy_streamer_a_bez_sekundaru() -> None:
    pipe = pipeline()
    routes = build_routes(pipe, streamer_of)
    assert route_trade(routes, pipe, "./neznamy") is None
    alone = pipeline(nxt=None)
    assert route_trade(routes, alone, streamer_of(spec(NEXT, "C")) or "") is None
    no_tracker = pipeline()
    no_tracker.runtime.cum_delta = None
    assert route_trade(routes, no_tracker, streamer_of(spec(ACTIVE, "C")) or "") is None
