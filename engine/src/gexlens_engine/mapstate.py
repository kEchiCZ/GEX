"""Kolektor stavu „tenká mapa" (#1245) — po vzoru `volregime`/`gammacliff`.

Každou minutu vyhodnotí stav mapy z toho, co runtime už spočítal (levels,
dominance zdí, Dyn GEX profil, max pain), drží ho pro `/status` a v US RTH
sbírá vzorky; po settle zapíše agregát seance do `map_state`. Při prvním
běhu doplní historii z partic `levels`, `walldom`, `gexprofile` a `bars`,
aby relativní prahy měly vzorek hned, ne za měsíc sbírání.
"""

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path

import pyarrow.parquet as pq

from gexlens_engine.compute.gexfield import GexProfile, gamma_at_price
from gexlens_engine.compute.mapstate import (
    WINDOW_SESSIONS,
    MapHistory,
    MapInputs,
    MapState,
    SessionMapStats,
    history_thresholds,
    map_state,
    session_stats,
)
from gexlens_engine.compute.marketclock import outside_us_rth
from gexlens_engine.compute.settle import settle_ts, trading_session_date
from gexlens_engine.runtime import EngineRuntime
from gexlens_engine.storage.mapstate_store import MapStateRepository

logger = logging.getLogger(__name__)

#: Odklad po settle — poslední minutový zápis levels musí dosednout.
SETTLE_GRACE_MINUTES = 5


def _rth_minutes(session: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """Hranice US RTH seance v UTC (od otevření do settle)."""
    open_utc = settle_ts(session) - dt.timedelta(hours=6, minutes=30)
    return open_utc, settle_ts(session)


def read_session_stats(data_dir: Path, symbol: str, session: dt.date) -> SessionMapStats | None:
    """Agregát seance z partic řetězu expirujícího v den seance (backfill).

    Gamma u ceny se dopočítá z `gexprofile` × close baru téže minuty; bez
    profilu (data před ADR-0009) zůstane None — řada se pak plní až za běhu.
    `thin_share` je None: v době seance stav nikdo nevyhodnocoval.
    """
    expiry_dir = data_dir / "derived" / symbol / session.strftime("%Y%m%d")
    levels_path = expiry_dir / "levels" / f"{session.isoformat()}.parquet"
    if not levels_path.exists():
        return None
    start, end = _rth_minutes(session)
    try:
        levels = pq.read_table(levels_path, columns=["ts_min", "total_gex"]).to_pylist()
    except Exception:
        logger.exception("Levels partice %s nečitelná — seance se přeskočí", levels_path)
        return None
    gex_abs = [
        abs(float(row["total_gex"]))
        for row in levels
        if row["ts_min"] is not None
        and start <= row["ts_min"] < end
        and row["total_gex"] is not None
    ]
    dominance = _read_dominance(
        expiry_dir / "walldom" / f"{session.isoformat()}.parquet", start, end
    )
    gamma_abs = _read_gamma_abs(
        expiry_dir / "gexprofile" / f"{session.isoformat()}.parquet",
        data_dir / "derived" / symbol / "bars" / f"{session.isoformat()}.parquet",
        start,
        end,
    )
    return session_stats(session, symbol, gex_abs, gamma_abs, dominance, thin_flags=[])


def _read_dominance(path: Path, start: dt.datetime, end: dt.datetime) -> list[float]:
    """Per minuta silnější z obou dominancí — podmínka slabých zdí chce obě pod prahem."""
    if not path.exists():
        return []
    try:
        rows = pq.read_table(path, columns=["ts_min", "call_wall_dom", "put_wall_dom"]).to_pylist()
    except Exception:
        logger.exception("Walldom partice %s nečitelná — dominance se nedoplní", path)
        return []
    out: list[float] = []
    for row in rows:
        ts = row["ts_min"]
        if ts is None or not (start <= ts < end):
            continue
        doms = [float(d) for d in (row["call_wall_dom"], row["put_wall_dom"]) if d is not None]
        if doms:
            out.append(max(doms))
    return out


def _read_gamma_abs(
    profile_path: Path, bars_path: Path, start: dt.datetime, end: dt.datetime
) -> list[float]:
    if not profile_path.exists() or not bars_path.exists():
        return []
    try:
        profiles = pq.read_table(profile_path).to_pylist()
        bars = pq.read_table(bars_path, columns=["ts_min", "close"]).to_pylist()
    except Exception:
        logger.exception("Profil/bars partice %s nečitelná — gamma se nedoplní", profile_path)
        return []
    close_by_min = {
        row["ts_min"]: float(row["close"])
        for row in bars
        if row["ts_min"] is not None and row["close"] is not None
    }
    out: list[float] = []
    for row in profiles:
        ts = row["ts_min"]
        if ts is None or not (start <= ts < end) or ts not in close_by_min or not row["values"]:
            continue
        profile = GexProfile(
            ts_min=ts,
            grid_start=float(row["grid_start"]),
            grid_step=float(row["grid_step"]),
            values=tuple(float(v) for v in row["values"]),
        )
        gamma = gamma_at_price(profile, close_by_min[ts])
        if gamma is not None:
            out.append(abs(gamma))
    return out


def levels_sessions(data_dir: Path, symbol: str) -> list[dt.date]:
    """Seance, pro které existuje levels partice řetězu expirujícího téhož dne."""
    base = data_dir / "derived" / symbol
    days: list[dt.date] = []
    if not base.exists():
        return days
    for expiry_dir in base.iterdir():
        name = expiry_dir.name
        if not (name.isdigit() and len(name) == 8):
            continue
        day = dt.date(int(name[:4]), int(name[4:6]), int(name[6:8]))
        if (expiry_dir / "levels" / f"{day.isoformat()}.parquet").exists():
            days.append(day)
    return sorted(days)


@dataclass
class MapStateCollector:
    """Minutový stav mapy + agregát seance po settle; backfill historie při startu."""

    symbol: str
    repository: MapStateRepository
    data_dir: Path
    backfill: bool = True

    #: Poslední vyhodnocený stav — čte ho /status a feature log.
    state: MapState | None = field(default=None, init=False)
    _history_for: dt.date | None = field(default=None, init=False)
    _history: MapHistory | None = field(default=None, init=False)
    _backfilled: bool = field(default=False, init=False)
    _written_for: dt.date | None = field(default=None, init=False)
    _samples_for: dt.date | None = field(default=None, init=False)
    _gex_abs: list[float] = field(default_factory=list, init=False)
    _gamma_abs: list[float] = field(default_factory=list, init=False)
    _dominance: list[float] = field(default_factory=list, init=False)
    _thin_flags: list[bool] = field(default_factory=list, init=False)

    async def on_minute(
        self, now: dt.datetime, spot: float, runtime: EngineRuntime, max_pain: float | None
    ) -> None:
        session = trading_session_date(now)
        if self.backfill and not self._backfilled:
            self._backfilled = True  # jeden pokus per proces i při chybě
            await asyncio.to_thread(self._backfill, session, now)
        if self._history_for != session:
            self._history = await asyncio.to_thread(self._load_history, session)
            self._history_for = session
        if self._samples_for != session:
            self._samples_for = session
            self._gex_abs, self._gamma_abs, self._dominance, self._thin_flags = [], [], [], []

        levels = runtime.last_gex_levels
        profile = runtime.last_profile
        inputs = MapInputs(
            ts_min=now,
            spot=spot,
            total_gex=levels.total_gex if levels else None,
            gamma_at_price=gamma_at_price(profile, spot) if profile else None,
            call_wall_dom=levels.call_wall_dom if levels else None,
            put_wall_dom=levels.put_wall_dom if levels else None,
            flip=levels.flip if levels else None,
            call_wall=levels.call_wall if levels else None,
            put_wall=levels.put_wall if levels else None,
            max_pain=max_pain,
        )
        state = map_state(inputs, self._history)
        self.state = state
        runtime.thin_map = state.thin

        if not outside_us_rth(now):
            if state.gex_abs is not None:
                self._gex_abs.append(state.gex_abs)
            if state.gamma_abs is not None:
                self._gamma_abs.append(state.gamma_abs)
            doms = [d for d in (inputs.call_wall_dom, inputs.put_wall_dom) if d is not None]
            if doms:
                self._dominance.append(max(doms))
            self._thin_flags.append(state.thin)

        boundary = settle_ts(session) + dt.timedelta(minutes=SETTLE_GRACE_MINUTES)
        if now >= boundary and self._written_for != session:
            self._written_for = session  # jeden pokus per seance i při chybě
            record = session_stats(
                session,
                self.symbol,
                self._gex_abs,
                self._gamma_abs,
                self._dominance,
                self._thin_flags,
            )
            if record is not None:
                await asyncio.to_thread(self.repository.upsert, record, now)
                logger.info(
                    "%s: stav mapy seance %s — medián |GEX| %.3g, tenká mapa %s minut",
                    self.symbol,
                    session,
                    record.gex_abs_median,
                    f"{record.thin_share:.0%}" if record.thin_share is not None else "—",
                )

    def _load_history(self, session: dt.date) -> MapHistory:
        rows = self.repository.history_before(self.symbol, session, window=WINDOW_SESSIONS)
        return history_thresholds(
            [row.gex_abs_median for row in rows], [row.gamma_abs_median for row in rows]
        )

    def _backfill(self, session: dt.date, now: dt.datetime) -> None:
        existing = self.repository.existing_dates(self.symbol)
        written = 0
        for day in levels_sessions(self.data_dir, self.symbol):
            if day >= session or day in existing:
                continue
            record = read_session_stats(self.data_dir, self.symbol, day)
            if record is None:
                continue
            self.repository.upsert(record, now)
            written += 1
        if written:
            logger.info("%s: stav mapy — backfill %d seancí z partic", self.symbol, written)
