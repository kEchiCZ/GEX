"""Roll v settle bez mezery v kalibrovaném řetězu D+1 (#1460, varianta C, ADR-0057 bod 6).

Celý den nad skutečnými objekty: vzorky `TradeBranchMonitor` každou minutu,
tisky přes trasy (`route_trade`), cykly runtime sekundáru a po rollu aktivního
runtime, který převezme tracker sekundáru (`EngineRuntime.take_over`), a ranní
kalibrace α nad zapsanými particemi. Tisky mezi posledním zápisem sekundáru
a novou pipeline jsou v netu a bod α vznikne; restart v rollu je mezera.
Mechaniku v6 volí explicitní zdroj trackeru (`SOURCE_AGGRESSOR`).
"""

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from gexlens_engine.compute.cumdelta import SOURCE_AGGRESSOR, CumDeltaTracker
from gexlens_engine.compute.marketclock import daily_close_utc
from gexlens_engine.compute.settle import session_bounds, settle_ts, trading_session_date
from gexlens_engine.config import Settings
from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.ibkr.mock import MockQuoteStreamer
from gexlens_engine.ibkr.scheduler import SubscriptionScheduler
from gexlens_engine.runtime import EngineRuntime, PublisherLike
from gexlens_engine.storage.fa_calibration import (
    AlphaCalibrationResult,
    FaAlphaRepository,
    collect_alpha_calibration,
    netflow_at_cutoff,
)
from gexlens_engine.storage.oi_archive import OIEodRepository, OIRecord
from gexlens_engine.storage.parquet_store import SnapshotWriter
from gexlens_engine.tasty.trade_branch import TradeBranchMonitor, build_routes, route_trade

MIN = dt.timedelta(minutes=1)
STRIKES = [7500.0 + 10 * i for i in range(5)]
SPOT = 7520.0


class NullPublisher(PublisherLike):
    async def status(self, **fields: object) -> None:
        return None

    async def publish(self, channel: str, data: dict[str, object]) -> None:
        return None


@dataclass
class Pipe:
    runtime: EngineRuntime
    next_runtime: EngineRuntime | None


@dataclass(frozen=True)
class RollDay:
    session: dt.date  # den D, jehož settle roluje
    expiry_d1: str  # sekundár celou seanci D = kalibrovaný řetěz (#1580)
    expiry_d2: str
    today: dt.date  # den ranní kalibrace


def streamer_of(spec: OptionContractSpec) -> str:
    return f"./E{spec.expiry}{spec.right}{spec.strike:.0f}:XCME"


def _specs(expiry: str) -> list[OptionContractSpec]:
    return [
        OptionContractSpec("ES", "FOP", expiry, strike, right, "CME", "E3D", "50")
        for strike in STRIKES
        for right in ("C", "P")
    ]


def _runtime(
    settings: Settings, repo: OIEodRepository, expiry: str, *, secondary: bool
) -> EngineRuntime:
    return EngineRuntime(
        settings=settings,
        scheduler=SubscriptionScheduler(MockQuoteStreamer(), settings),
        writer=SnapshotWriter(settings),
        oi_repository=repo,
        publisher=NullPublisher(),
        symbol="ES",
        expiry=expiry,
        multiplier=50.0,
        contracts=_specs(expiry),
        cum_delta=CumDeltaTracker(multiplier=50.0, source=SOURCE_AGGRESSOR),
        push_status=False,
        secondary=secondary,
    )


async def _simulate(
    tmp_path: Path, day: RollDay, *, handover: bool = True
) -> tuple[Settings, AlphaCalibrationResult | None]:
    """Den D od pauzy před seancí do řezu 16:00 CT; tisky 100 kontraktů BUY na call stranu.

    Tisky: 50 hodinu po openu, 20 po posledním zápisu sekundáru (minuta před
    settle), 10 mezi zastavením staré a založením nové pipeline, 10 před prvním
    vzorkem nové pipeline a 10 pět minut po settle.
    """
    settings = Settings(data_dir=tmp_path)
    db = create_engine(f"sqlite+pysqlite:///{tmp_path / 'db.sqlite'}")
    repo = OIEodRepository(db)
    repo.ensure_schema()
    alpha_repo = FaAlphaRepository(db)
    alpha_repo.ensure_schema()

    session_open = session_bounds(day.session)[0]
    start = session_open - 30 * MIN  # pauza nebo víkend: hranice prvního okna známá
    roll = settle_ts(day.session)
    cutoff = daily_close_utc(day.session)
    old_pipe = Pipe(
        runtime=_runtime(settings, repo, day.session.strftime("%Y%m%d"), secondary=False),
        next_runtime=_runtime(settings, repo, day.expiry_d1, secondary=True),
    )
    secondary = old_pipe.next_runtime
    assert secondary is not None
    routes = {"ES": build_routes(old_pipe, streamer_of)}
    pipe: Pipe | None = old_pipe
    monitor = TradeBranchMonitor()
    clock = [start]

    def sample(now: dt.datetime) -> None:
        clock[0] = now
        assert pipe is not None
        monitor.sample(
            now,
            {"ES": pipe},
            routes,
            connected=True,
            reconnects=0,
            last_event_among=lambda _streamers: clock[0],
        )

    def prints(now: dt.datetime, size: float) -> None:
        for strike in STRIKES:
            spec = OptionContractSpec("ES", "FOP", day.expiry_d1, strike, "C", "CME", "E3D", "50")
            target = route_trade(routes["ES"], pipe, streamer_of(spec))
            assert target is not None
            tracker, routed = target
            tracker.add_dx_trade(routed, size, "BUY", 0.5, session_date=trading_session_date(now))

    schedule = {session_open + 60 * MIN: 50.0, roll - MIN: 20.0, roll + 5 * MIN: 10.0}
    secondary_cycles = {session_open + 90 * MIN, roll - 2 * MIN}
    active: EngineRuntime | None = None
    active_cycles = {roll + MIN, roll + 2 * MIN, cutoff - MIN, cutoff}
    now = start
    while now <= cutoff:
        if now in schedule:
            prints(now, schedule[now])
        if now == roll + MIN:
            # Další iterace orchestrátoru: nová pipeline nad D+1 (#1460, varianta C)
            active = _runtime(settings, repo, day.expiry_d1, secondary=False)
            if handover:
                active.take_over(secondary)
            pipe = Pipe(active, _runtime(settings, repo, day.expiry_d2, secondary=True))
            prints(now, 10.0)  # tisk před prvním vzorkem nové pipeline
        sample(now)
        if now in secondary_cycles:
            await secondary.run_cycle(now, SPOT, [])
        if active is not None and now in active_cycles:
            await active.run_cycle(now, SPOT, [])
            if now == roll + MIN:
                assert pipe is not None
                routes = {"ES": build_routes(pipe, streamer_of)}  # obnova plánu
        if now == roll:
            # Konec cyklu rollu: stará pipeline se zastaví, trasy zůstávají
            pipe = None
            prints(now, 10.0)
        now += MIN

    for strike in STRIKES:
        repo.upsert_many(
            [OIRecord("ES", day.expiry_d1, strike, "C", day.session, 1000.0)],
            settings.oi_publication_utc(day.session) + dt.timedelta(hours=1),
        )
        repo.upsert_many(
            [OIRecord("ES", day.expiry_d1, strike, "C", day.today, 1040.0)],
            settings.oi_publication_utc(day.today) + dt.timedelta(hours=1),
        )
    result = collect_alpha_calibration(
        "ES",
        settings.derived_dir,
        repo,
        alpha_repo,
        day.today,
        publication_utc=settings.oi_publication_utc,
        cumdelta_prints=True,
    )
    return settings, result


ROLL_DAYS = [
    # Všední den v létě: settle 20:00 UTC, řez 21:00 UTC (roll 19. 10. 2026)
    RollDay(dt.date(2026, 10, 19), "20261020", "20261021", dt.date(2026, 10, 20)),
    # Pátek: D+1 je pondělní expirace, kalibrace v pondělí
    RollDay(dt.date(2026, 10, 23), "20261026", "20261027", dt.date(2026, 10, 26)),
    # Seance pondělí: open v neděli 22:00 UTC, netflow v nedělní i pondělní partici
    RollDay(dt.date(2026, 10, 26), "20261027", "20261028", dt.date(2026, 10, 27)),
    # Zimní čas: settle 21:00 UTC, řez 22:00 UTC
    RollDay(dt.date(2026, 11, 4), "20261105", "20261106", dt.date(2026, 11, 5)),
    # Zkrácená seance po Thanksgiving: settle 13:00 ET = 18:00 UTC, trh do 12:15 CT
    RollDay(dt.date(2026, 11, 27), "20261130", "20261201", dt.date(2026, 11, 30)),
]


@pytest.mark.parametrize(
    "day", ROLL_DAYS, ids=["vsedni-den", "patek", "nedelni-open", "zimni-cas", "zkracena"]
)
async def test_roll_predani_bez_mezery_a_bod_alfa(tmp_path: Path, day: RollDay) -> None:
    settings, result = await _simulate(tmp_path, day)
    exp_dir = settings.derived_dir / "ES" / day.expiry_d1
    assert not (exp_dir / "netgap").exists()
    netflow = netflow_at_cutoff(
        exp_dir / "netflow" / f"{day.session.isoformat()}.parquet",
        day.session,
        sources=frozenset({SOURCE_AGGRESSOR}),
    )
    assert {key: net for key, net in netflow.items() if key[1] == "C"} == {
        (strike, "C"): 100.0 for strike in STRIKES
    }
    assert result is not None and result.day == day.session and result.expiry == day.expiry_d1
    assert result.point.ratio_median == pytest.approx(0.4)


async def test_restart_v_rollu_je_mezera_a_bod_neda(tmp_path: Path) -> None:
    """Bez předání (restart procesu, pipeline nevznikla hned po rollu) je první minuta mezera."""
    day = ROLL_DAYS[0]
    settings, result = await _simulate(tmp_path, day, handover=False)
    netgap = settings.derived_dir / "ES" / day.expiry_d1 / "netgap"
    assert netgap.exists()
    assert result is None
