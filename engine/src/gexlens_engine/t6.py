"""Sběrač kandidátních dnů T6 „Premarket squeeze" (#256) — ŽÁDNÝ setup.

Šablona T6 se nestaví, dokud není statistika (past z #252: neladit z jednoho
dne). Tenhle sběrač jen automatizuje, co issue navrhuje dělat ručně: po dni
s výrazně nižším close ráno před US open spočítá metriky vzorce, zapíše je do
tabulky a upozorní zvonkem. Kvalitativní soud (konal se squeeze?) zůstává na
uživateli; po ~5 výskytech se u issue rozhodne o stavbě šablony.
"""

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path

from gexlens_engine.compute.setups import gex_regime, max_pain_strike
from gexlens_engine.runtime import EngineRuntime, PublisherLike
from gexlens_engine.storage.oi_archive import OIEodRepository
from gexlens_engine.storage.t6_store import T6Repository

logger = logging.getLogger(__name__)

# Vyhodnocení běží v prvním cyklu po tomto čase — před US open (13:30 UTC),
# aby metriky zachytily premarket, ne už otevřený trh
EVALUATE_AFTER_UTC = dt.time(13, 25)
# Trigger: včerejší close-to-close pod prahem (záporné procento)
DEFAULT_TRIGGER_PCT = -1.0


def drop_trigger(previous_close: float, last_close: float, threshold_pct: float) -> bool:
    """Zavřela poslední seance výrazně níž? (close-to-close ≤ práh v %)."""
    if previous_close <= 0:
        return False
    change_pct = (last_close / previous_close - 1) * 100
    return change_pct <= threshold_pct


def put_oi_increase_below(
    today: dict[tuple[float, str], float],
    previous: dict[tuple[float, str], float],
    spot: float,
) -> float:
    """Σ kladných ΔOI putů pod spotem — podpis „čerstvá put masa" (kap. 18)."""
    total = 0.0
    for (strike, right), oi in today.items():
        if right != "P" or strike >= spot:
            continue
        delta = oi - previous.get((strike, right), 0.0)
        if delta > 0:
            total += delta
    return total


@dataclass(frozen=True)
class DailyCloses:
    """Poslední dva denní closy podkladu (D−1 a D−2) pro trigger."""

    last_day: dt.date
    last_close: float
    previous_close: float


def read_daily_closes(data_dir: Path, symbol: str, today: dt.date) -> DailyCloses | None:
    """Closy posledních dvou seancí z parquet archivu barů (derived/{sym}/bars)."""
    bars_dir = data_dir / "derived" / symbol / "bars"
    if not bars_dir.exists():
        return None
    days: list[dt.date] = []
    for path in bars_dir.glob("*.parquet"):
        try:
            day = dt.date.fromisoformat(path.stem)
        except ValueError:
            continue
        if day < today:
            days.append(day)
    if len(days) < 2:
        return None
    days.sort()

    import pyarrow.parquet as pq

    def last_close(day: dt.date) -> float | None:
        try:
            table = pq.read_table(bars_dir / f"{day.isoformat()}.parquet", columns=["close"])
        except Exception:
            return None
        if table.num_rows == 0:
            return None
        return float(table.column("close")[-1].as_py())

    last = last_close(days[-1])
    previous = last_close(days[-2])
    if last is None or previous is None:
        return None
    return DailyCloses(last_day=days[-1], last_close=last, previous_close=previous)


@dataclass
class T6Collector:
    """Jednou denně před US open vyhodnotí kandidáta; jinak nedělá nic."""

    symbol: str
    repository: T6Repository
    oi_repository: OIEodRepository
    publisher: PublisherLike
    data_dir: Path
    trigger_pct: float = DEFAULT_TRIGGER_PCT

    def __post_init__(self) -> None:
        self._evaluated_for: dt.date | None = None

    async def on_minute(self, now: dt.datetime, spot: float, runtime: EngineRuntime) -> None:
        today = now.date()
        if self._evaluated_for == today or now.time() < EVALUATE_AFTER_UTC:
            return
        self._evaluated_for = today  # jeden pokus denně i při chybě — žádné bušení

        closes = read_daily_closes(self.data_dir, self.symbol, today)
        if closes is None or not drop_trigger(
            closes.previous_close, closes.last_close, self.trigger_pct
        ):
            return

        change_pct = (closes.last_close / closes.previous_close - 1) * 100
        # ΔOI putů pod spotem: dnešní ranní archiv vs. předchozí den
        oi_today: dict[tuple[float, str], float] = {}
        oi_previous: dict[tuple[float, str], float] = {}
        expiry = runtime.expiry
        today_records = self.oi_repository.values_for(self.symbol, expiry, today)
        if today_records:
            oi_today = {(r.strike, r.right): r.oi for r in today_records}
            previous_day = self.oi_repository.latest_day_before(self.symbol, expiry, today)
            if previous_day is not None:
                oi_previous = {
                    (r.strike, r.right): r.oi
                    for r in self.oi_repository.values_for(self.symbol, expiry, previous_day)
                }
        put_mass = put_oi_increase_below(oi_today, oi_previous, spot) if oi_today else None

        levels = runtime.last_gex_levels
        regime = gex_regime(spot, levels.flip, levels.total_gex) if levels is not None else None
        max_pain = max_pain_strike(oi_today) if oi_today else None
        overnight_pct = (spot / closes.last_close - 1) * 100 if closes.last_close > 0 else None

        self.repository.upsert(
            symbol=self.symbol,
            day=today,
            trigger_close_pct=change_pct,
            overnight_move_pct=overnight_pct,
            put_oi_increase=put_mass,
            gex_regime=regime,
            max_pain=max_pain,
            spot=spot,
            evaluated_at=now,
        )
        message = (
            f"Kandidát T6 (#256): včera {change_pct:+.1f} %, premarket {overnight_pct:+.2f} % "
            f"od close, ΔOI putů pod cenou {put_mass:+.0f}, režim {regime or '—'}"
            + (f", Max Pain {max_pain:.0f}" if max_pain is not None else "")
            if overnight_pct is not None and put_mass is not None
            else f"Kandidát T6 (#256): včera {change_pct:+.1f} % — zkontroluj premarket vzorec"
        )
        await self.publisher.publish(
            "alerts",
            {
                "kind": "t6_candidate",
                "symbol": self.symbol,
                "message": message,
                "ts": now.timestamp(),
            },
        )
        logger.info("T6 kandidát %s %s zapsán", self.symbol, today)
