"""Kolektor respektování pásma EM (#872) — po vzoru `volregime`/`gammacliff`.

Po settle seance klasifikuje den vůči pásmu expected move; při prvním běhu
doplní dostupnou historii. Zdroje per seance:

* **EM**: snapshoty 0DTE řetězu (mid v první minutě US seance, retence
  14 dní), fallback close prémie ATM straddlu z věčného `oi_eod` (#519 —
  včerejší závěr jako pre-open odhad, od 13. 8.). Starší seance bez obou
  zdrojů se poctivě vynechají — žádný default (zásada ADR-0028).
* **Průběh dne**: bary do settle (věčný archiv).
* **Gamma kontext (#876)**: podíl minut se spotem pod měřeným flipem
  z levels partic (retence 14 dní → u starších NULL).
"""

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path

import pyarrow.parquet as pq
from sqlalchemy import select
from sqlalchemy.engine import Engine

from gexlens_engine.compute.emrespect import (
    SOURCE_CLOSE_PREM,
    SOURCE_STRADDLE,
    EmReference,
    EmRespect,
    StraddleQuote,
    classify,
    negative_share,
    straddle_em,
)
from gexlens_engine.compute.settle import (
    ET_TZ,
    session_time_utc,
    settle_ts,
    trading_session_date,
)
from gexlens_engine.storage.emrespect_store import EmRespectRepository
from gexlens_engine.storage.oi_archive import oi_eod_table

logger = logging.getLogger(__name__)

#: Odklad po settle — bary poslední minuty musí stihnout dorazit (vzor #713).
SETTLE_GRACE_MINUTES = 5

#: US open 9:30 ET — referenční minuta EM (zrcadlo frontend usOpenMs, #674).
US_OPEN_LOCAL = dt.time(9, 30)


@dataclass(frozen=True)
class _SessionBars:
    high: float
    low: float
    close: float
    #: close per minuta (jen do settle) — spoty pro straddle i negative share
    spots: dict[dt.datetime, float]


def _session_files(base: Path, session: dt.date) -> list[Path]:
    """Partice kalendářních dnů, které seance pokrývá (Globex začíná D−1)."""
    days = (session - dt.timedelta(days=1), session)
    return [
        base / f"{day.isoformat()}.parquet"
        for day in days
        if (base / f"{day.isoformat()}.parquet").exists()
    ]  # noqa: E501


def load_session_bars(data_dir: Path, symbol: str, session: dt.date) -> _SessionBars | None:
    """High/low/close seance do settle + spot per minuta; None bez barů."""
    boundary = settle_ts(session)
    spots: dict[dt.datetime, float] = {}
    high: float | None = None
    low: float | None = None
    close: float | None = None
    last_ts: dt.datetime | None = None
    for path in _session_files(data_dir / "derived" / symbol / "bars", session):
        try:
            table = pq.read_table(path, columns=["ts_min", "high", "low", "close"])
        except Exception:
            logger.exception("Bars partice %s nečitelná — přeskočena", path)
            continue
        for record in table.to_pylist():
            ts = record["ts_min"]
            if ts is None or trading_session_date(ts) != session or ts > boundary:
                continue
            spots[ts] = float(record["close"])
            high = max(high, float(record["high"])) if high is not None else float(record["high"])
            low = min(low, float(record["low"])) if low is not None else float(record["low"])
            if last_ts is None or ts > last_ts:
                last_ts = ts
                close = float(record["close"])
    if high is None or low is None or close is None:
        return None
    return _SessionBars(high=high, low=low, close=close, spots=spots)


def load_flips(data_dir: Path, symbol: str, session: dt.date) -> dict[dt.datetime, float]:
    """Měřený flip per minuta seance (do settle); prázdné = levels nejsou."""
    boundary = settle_ts(session)
    flips: dict[dt.datetime, float] = {}
    for path in _session_files(data_dir / "derived" / symbol / "levels", session):
        try:
            table = pq.read_table(path, columns=["ts_min", "flip"])
        except Exception:
            logger.exception("Levels partice %s nečitelná — přeskočena", path)
            continue
        for record in table.to_pylist():
            ts = record["ts_min"]
            flip = record["flip"]
            if ts is None or flip is None or trading_session_date(ts) != session or ts > boundary:
                continue
            flips[ts] = float(flip)
    return flips


def straddle_reference(
    data_dir: Path, symbol: str, session: dt.date, spots: dict[dt.datetime, float]
) -> EmReference | None:
    """EM z 0DTE snapshotů: první minuta ≥ US open s validním straddlem."""
    expiry = session.strftime("%Y%m%d")
    path = data_dir / "snapshots" / symbol / expiry / f"{session.isoformat()}.parquet"
    if not path.exists():
        return None
    try:
        table = pq.read_table(path, columns=["ts_min", "strike", "right", "bid", "ask"])
    except Exception:
        logger.exception("Snapshot partice %s nečitelná — EM ze straddlu není", path)
        return None
    us_open = session_time_utc(session, US_OPEN_LOCAL.hour, US_OPEN_LOCAL.minute, ET_TZ)
    by_minute: dict[dt.datetime, dict[float, dict[str, float]]] = {}
    for record in table.to_pylist():
        ts = record["ts_min"]
        if ts is None or ts < us_open:
            continue
        bid = float(record["bid"] or 0.0)
        ask = float(record["ask"] or 0.0)
        mid = (bid + ask) / 2 if bid > 0 and ask > 0 else 0.0
        strikes = by_minute.setdefault(ts, {})
        sides = strikes.setdefault(float(record["strike"]), {})
        sides[str(record["right"])] = mid
    for ts in sorted(by_minute):
        spot = spots.get(ts)
        if spot is None:
            continue
        quotes = [
            StraddleQuote(strike=strike, call_mid=sides.get("C", 0.0), put_mid=sides.get("P", 0.0))
            for strike, sides in by_minute[ts].items()
        ]
        hit = straddle_em(quotes, spot)
        if hit is not None:
            atm_strike, em_points = hit
            return EmReference(
                ts=ts,
                source=SOURCE_STRADDLE,
                anchor=spot,
                atm_strike=atm_strike,
                em_points=em_points,
            )
    return None


def close_prem_reference(db: Engine, symbol: str, session: dt.date) -> EmReference | None:
    """Fallback: závěrečné prémie ATM straddlu z věčného `oi_eod` (#519).

    Kotva = `und_price` snímku; ATM = strike nejblíž kotvě s close prémií
    obou stran. Je to PRE-OPEN odhad (včerejší závěr), ne open straddle —
    proto se zdroj ukládá a statistiky ho umí oddělit.
    """
    expiry = session.strftime("%Y%m%d")
    stmt = select(
        oi_eod_table.c.strike,
        oi_eod_table.c.right,
        oi_eod_table.c.close_prem,
        oi_eod_table.c.und_price,
    ).where(
        oi_eod_table.c.symbol == symbol,
        oi_eod_table.c.expiry == expiry,
        oi_eod_table.c.date == session,
        oi_eod_table.c.close_prem.is_not(None),
    )
    with db.connect() as conn:
        rows = conn.execute(stmt).fetchall()
    anchors = [float(row.und_price) for row in rows if row.und_price is not None]
    if not anchors:
        return None
    anchor = anchors[0]
    by_strike: dict[float, dict[str, float]] = {}
    for row in rows:
        prem = float(row.close_prem)
        if prem <= 0:
            continue
        # Víc trading classes téže expirace (#736): bere se první nenulová
        by_strike.setdefault(float(row.strike), {}).setdefault(str(row.right), prem)
    quotes = [
        StraddleQuote(strike=strike, call_mid=sides.get("C", 0.0), put_mid=sides.get("P", 0.0))
        for strike, sides in by_strike.items()
    ]
    hit = straddle_em(quotes, anchor)
    if hit is None:
        return None
    atm_strike, em_points = hit
    return EmReference(
        ts=None, source=SOURCE_CLOSE_PREM, anchor=anchor, atm_strike=atm_strike, em_points=em_points
    )


def compute_session(data_dir: Path, db: Engine, symbol: str, session: dt.date) -> EmRespect | None:
    """Klasifikace jedné seance; None, když chybí bary nebo oba zdroje EM."""
    bars = load_session_bars(data_dir, symbol, session)
    if bars is None:
        return None
    reference = straddle_reference(data_dir, symbol, session, bars.spots)
    if reference is None:
        reference = close_prem_reference(db, symbol, session)
    if reference is None:
        return None
    share = negative_share(bars.spots, load_flips(data_dir, symbol, session))
    return classify(
        session_date=session,
        symbol=symbol,
        reference=reference,
        high=bars.high,
        low=bars.low,
        close=bars.close,
        negative_gamma_share=share,
    )


@dataclass
class EmRespectCollector:
    """Jednou po settle klasifikuje seanci; při prvním běhu doplní historii."""

    symbol: str
    repository: EmRespectRepository
    db: Engine
    data_dir: Path
    #: Kolik seancí zpět zkusit při backfillu (snapshoty 14 dní + oi_eod od 13. 8.)
    backfill_days: int = 45

    _evaluated_for: dt.date | None = field(default=None, init=False)
    _backfilled: bool = field(default=False, init=False)

    async def on_minute(self, now: dt.datetime) -> None:
        session = trading_session_date(now)
        boundary = settle_ts(session) + dt.timedelta(minutes=SETTLE_GRACE_MINUTES)
        if not self._backfilled:
            self._backfilled = True
            await asyncio.to_thread(self._backfill, now, session)
        if now < boundary or self._evaluated_for == session:
            return
        self._evaluated_for = session  # jeden pokus per seance i při chybě
        await asyncio.to_thread(self._run, now, session)

    def _run(self, now: dt.datetime, session: dt.date) -> None:
        record = compute_session(self.data_dir, self.db, self.symbol, session)
        if record is None:
            logger.info("%s %s: EM respect nejde spočítat (bez barů nebo EM)", self.symbol, session)
            return
        self.repository.upsert(record, now)
        logger.info(
            "%s %s: EM respect — close %s pásma (EM %.1f b, rozsah %.2f× EM)",
            self.symbol,
            session,
            "uvnitř" if record.close_in_band else "MIMO",
            record.reference.em_points,
            record.range_vs_em,
        )

    def _backfill(self, now: dt.datetime, current_session: dt.date) -> None:
        existing = self.repository.existing_dates(self.symbol)
        written = 0
        for offset in range(1, self.backfill_days + 1):
            session = current_session - dt.timedelta(days=offset)
            if session.weekday() >= 5 or session in existing:
                continue
            record = compute_session(self.data_dir, self.db, self.symbol, session)
            if record is None:
                continue
            self.repository.upsert(record, now)
            written += 1
        if written:
            logger.info("%s: EM respect backfill — %d seancí", self.symbol, written)
