"""Vyhodnocení scénářů dne (#1173) — kolektor po settle, vzor GammaCliffCollector.

Jednou po settle každé seance projde scénáře symbolu, jejichž termín uplynul
a nemají výsledek; bary bere z partic `derived/{sym}/bars/` od okamžiku
vzniku (výhradně PO `created_at`) do settle termínu — i přes více seancí.
EM pro odchylku v násobcích EM je z `em_respect` seance vzniku (bez řádku
None, ne odhad). Výsledek jde do `scenarios.result` a alertem do zvonku
(a přes něj i na Telegram, kategorie info).
"""

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
from sqlalchemy import select

from gexlens_engine.compute.scenario import Bar, PathPoint, evaluate_scenario
from gexlens_engine.compute.settle import settle_ts, trading_session_date
from gexlens_engine.storage.emrespect_store import em_respect_table
from gexlens_engine.storage.scenarios_store import ScenarioRow, ScenariosRepository

logger = logging.getLogger(__name__)

SETTLE_GRACE_MINUTES = 15


def load_bars_between(
    data_dir: Path, symbol: str, start: dt.datetime, end: dt.datetime
) -> list[Bar]:
    """1m bary podkladu v (start, end] napříč denními particemi (UTC den baru)."""
    base = data_dir / "derived" / symbol / "bars"
    bars: list[Bar] = []
    day = start.date()
    while day <= end.date():
        path = base / f"{day.isoformat()}.parquet"
        if path.exists():
            try:
                table = pq.read_table(path, columns=["ts_min", "high", "low", "close"])
            except Exception:
                logger.exception("Bars partice %s nečitelná — přeskočena", path)
                table = None
            if table is not None:
                for record in table.to_pylist():
                    ts = record["ts_min"]
                    if ts is None or ts <= start or ts > end:
                        continue
                    bars.append(
                        Bar(
                            ts=ts,
                            high=float(record["high"]),
                            low=float(record["low"]),
                            close=float(record["close"]),
                        )
                    )
        day += dt.timedelta(days=1)
    bars.sort(key=lambda bar: bar.ts)
    return bars


def path_points(raw: list[dict[str, Any]]) -> list[PathPoint]:
    points: list[PathPoint] = []
    for item in raw:
        try:
            ts = dt.datetime.fromisoformat(str(item["ts"]))
            price = float(item["price"])
        except (KeyError, TypeError, ValueError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.UTC)
        points.append(PathPoint(ts=ts, price=price))
    return points


@dataclass
class ScenarioCollector:
    """Jednou po settle vyhodnotí scénáře symbolu po termínu (#1173)."""

    symbol: str
    repository: ScenariosRepository
    db: Any
    data_dir: Path
    publisher: Any = None

    _evaluated_for: dt.date | None = field(default=None, init=False)

    async def on_minute(self, now: dt.datetime) -> None:
        session = trading_session_date(now)
        boundary = settle_ts(session) + dt.timedelta(minutes=SETTLE_GRACE_MINUTES)
        if now < boundary or self._evaluated_for == session:
            return
        self._evaluated_for = session  # jeden pokus per seance i při chybě
        results = await asyncio.to_thread(self.run, now)
        for row, result in results:
            if self.publisher is None:
                continue
            summary = (
                f"Scénář #{row.id} ({row.day.isoformat()} → {row.deadline.isoformat()}): "
                f"{result['verdict']}, cíl 1 {'ano' if result['hit1'] else 'ne'}"
                + (
                    f", cíl 2 {'ano' if result['hit2'] else 'ne'}"
                    if result.get("hit2") is not None
                    else ""
                )
                + (
                    f", max. odchylka {result['max_dev_pts']:g} b"
                    if result.get("max_dev_pts") is not None
                    else ""
                )
            )
            await self.publisher.publish(
                "alerts",
                {
                    "kind": "scenario_result",
                    "symbol": self.symbol,
                    "message": summary,
                    "scenario_id": row.id,
                    "ts": now.timestamp(),
                },
            )

    def _em_points(self, session: dt.date) -> float | None:
        stmt = select(em_respect_table.c.em_points).where(
            em_respect_table.c.symbol == self.symbol,
            em_respect_table.c.session_date == session,
        )
        try:
            with self.db.connect() as conn:
                value = conn.execute(stmt).scalar_one_or_none()
        except Exception:
            logger.exception("EM pro scénář %s %s nečitelné", self.symbol, session)
            return None
        return float(value) if value is not None else None

    def run(self, now: dt.datetime) -> list[tuple[ScenarioRow, dict[str, Any]]]:
        """Blokující průchod — volat přes to_thread. Vrací (scénář, výsledek)."""
        done: list[tuple[ScenarioRow, dict[str, Any]]] = []
        for row in self.repository.pending(self.symbol, now):
            bars = load_bars_between(self.data_dir, self.symbol, row.created_at, row.deadline_ts)
            result = evaluate_scenario(
                bars,
                entry=row.entry,
                targets=row.targets,
                path=path_points(row.path),
                em_points=self._em_points(row.day),
            )
            if result is None:
                # Bez barů (výpadek sběru přes celé okno) — označit, ať se
                # nezkouší donekonečna; výsledek NULL = „nešlo posoudit"
                logger.warning(
                    "%s: scénář #%d bez barů v okně %s–%s — bez výsledku",
                    self.symbol,
                    row.id,
                    row.created_at.isoformat(),
                    row.deadline_ts.isoformat(),
                )
                self.repository.record_result(row.id, None, now)
                continue
            payload = result.as_dict()
            self.repository.record_result(row.id, payload, now)
            logger.info(
                "%s: scénář #%d vyhodnocen — %s (cíl 1 %s, %d barů)",
                self.symbol,
                row.id,
                result.verdict,
                "ano" if result.hit1 else "ne",
                result.bars,
            )
            done.append((row, payload))
        return done
