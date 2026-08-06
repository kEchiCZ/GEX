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

from gexlens_engine.compute.settle import settle_ts
from gexlens_engine.compute.setups import gex_regime, max_pain_strike
from gexlens_engine.runtime import EngineRuntime, PublisherLike
from gexlens_engine.storage.oi_archive import OIEodRepository
from gexlens_engine.storage.t6_store import T6_CONVENTION_VERSION, T6Repository

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
    """Closy posledních dvou seancí z parquet archivu barů (derived/{sym}/bars).

    Den seance končí settle US seance, ne UTC půlnocí (#498): close dne D je
    poslední bar s `ts_min` PŘED settle(D). Bary po settle patří následující
    seanci a do closu dne D se nepočítají. Dny bez baru před settle (neděle —
    Globex otevírá až po settle hranici, svátky) se přeskočí.
    """
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
    days.sort(reverse=True)

    import pyarrow.parquet as pq

    def session_close(day: dt.date) -> float | None:
        """Close poslední minuty před settle hranicí dne, nebo None."""
        try:
            table = pq.read_table(
                bars_dir / f"{day.isoformat()}.parquet", columns=["ts_min", "close"]
            )
        except Exception:
            return None
        boundary = settle_ts(day)
        best_ts: dt.datetime | None = None
        best_close: float | None = None
        for ts, close in zip(
            table.column("ts_min").to_pylist(), table.column("close").to_pylist(), strict=True
        ):
            if ts >= boundary:
                continue
            if best_ts is None or ts > best_ts:
                best_ts = ts
                best_close = float(close)
        return best_close

    closes: list[tuple[dt.date, float]] = []
    for day in days:
        close = session_close(day)
        if close is None:
            continue
        closes.append((day, close))
        if len(closes) == 2:
            break
    if len(closes) < 2:
        return None
    (last_day, last), (_, previous) = closes
    return DailyCloses(last_day=last_day, last_close=last, previous_close=previous)


def recompute_stale_candidates(
    repository: T6Repository, data_dir: Path, trigger_pct: float = DEFAULT_TRIGGER_PCT
) -> int:
    """Přepočet kandidátů uložených starou konvencí UTC půlnoci (#498).

    Trigger i overnight gap se přepočítají z věčného archivu 1min barů podle
    settle konvence, aby se kalibrace neučila ze dvou režimů. Záznamy, pro
    které bary chybí, se odstraní s logem. Běží při startu enginu; dotkne se
    jen řádků s `convention_version` < aktuální — opakovaný start je no-op.
    Vrací počet přepočtených řádků.
    """
    stale = repository.list_stale(T6_CONVENTION_VERSION)
    updated = 0
    for row in stale:
        symbol = str(row["symbol"])
        day: dt.date = row["day"]
        closes = read_daily_closes(data_dir, symbol, day)
        if closes is None:
            logger.warning(
                "T6 přepočet (#498): %s %s bez dostupných barů — kandidát odstraněn",
                symbol,
                day,
            )
            repository.delete(symbol=symbol, day=day)
            continue
        change_pct = (closes.last_close / closes.previous_close - 1) * 100
        spot = float(row["spot"])
        overnight_pct = (spot / closes.last_close - 1) * 100 if closes.last_close > 0 else None
        if not drop_trigger(closes.previous_close, closes.last_close, trigger_pct):
            # Řádek zůstává (ruční verdikt u issue už může existovat), jen se
            # poctivě přepíše — kalibrace pozná slabý trigger z hodnoty
            logger.warning(
                "T6 přepočet (#498): %s %s pod settle konvencí netriggeruje (%.2f %%) — ponechán",
                symbol,
                day,
                change_pct,
            )
        repository.upsert(
            symbol=symbol,
            day=day,
            trigger_close_pct=change_pct,
            overnight_move_pct=overnight_pct,
            put_oi_increase=row["put_oi_increase"],
            gex_regime=row["gex_regime"],
            max_pain=row["max_pain"],
            spot=spot,
            evaluated_at=row["evaluated_at"],
        )
        updated += 1
    if stale:
        logger.info(
            "T6 přepočet (#498): %d kandidátů přepočteno na settle konvenci, %d odstraněno",
            updated,
            len(stale) - updated,
        )
    return updated


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
