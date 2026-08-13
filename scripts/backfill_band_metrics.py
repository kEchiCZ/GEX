"""Backfill pásmových metrik (#575 fáze 1) k historickým setupům.

Pro setupy bez `band_sharpness` v context JSON dohledá Dyn profil minuty
vzniku (partice `derived/{sym}/{exp}/gexprofile/`, retence ~90 dní) a doplní
obě varianty ostrosti + hloubku toutéž čistou funkcí jako živá cesta.
Idempotentní — už doplněné setupy přeskakuje.

Spuštění (v kontejneru enginu nebo lokálně s GEXLENS_DATABASE_URL):
    python scripts/backfill_band_metrics.py [--days 90]
"""

import argparse
import datetime as dt
import sys
from pathlib import Path

import pyarrow.parquet as pq
from sqlalchemy import create_engine, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "src"))

from gexlens_engine.compute.bandregime import band_context  # noqa: E402
from gexlens_engine.compute.gexfield import GexProfile  # noqa: E402
from gexlens_engine.config import load_settings  # noqa: E402
from gexlens_engine.storage.setups_store import SetupsRepository, setups_table  # noqa: E402


def profile_at(derived_dir: Path, symbol: str, expiry: str, ts: dt.datetime) -> GexProfile | None:
    """Poslední profil s ts_min <= ts; hledá partici dne a den okolo (UTC hranice)."""
    best: GexProfile | None = None
    best_ts: dt.datetime | None = None
    for offset in (0, -1, 1):
        day = (ts + dt.timedelta(days=offset)).date()
        path = derived_dir / symbol / expiry / "gexprofile" / f"{day.isoformat()}.parquet"
        if not path.exists():
            continue
        for row in pq.read_table(path).to_pylist():
            row_ts = row["ts_min"]
            if row_ts.tzinfo is None:
                row_ts = row_ts.replace(tzinfo=dt.UTC)
            if row_ts <= ts and (best_ts is None or row_ts > best_ts):
                best_ts = row_ts
                best = GexProfile(
                    ts_min=row_ts,
                    grid_start=float(row["grid_start"]),
                    grid_step=float(row["grid_step"]),
                    values=tuple(float(v) for v in row["values"]),
                )
    return best


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill band metrik (#575)")
    parser.add_argument("--days", type=int, default=90)
    args = parser.parse_args()

    settings = load_settings()
    engine = create_engine(settings.database_url)
    repository = SetupsRepository(engine)
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=args.days)
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                setups_table.c.id,
                setups_table.c.symbol,
                setups_table.c.expiry,
                setups_table.c.created_ts,
                setups_table.c.entry,
                setups_table.c.context,
            ).where(setups_table.c.created_ts >= since)
        ).fetchall()

    written = 0
    skipped = 0
    missing_profile = 0
    for row in rows:
        if "band_sharpness" in (row.context or {}):
            skipped += 1
            continue
        created = row.created_ts if row.created_ts.tzinfo else row.created_ts.replace(tzinfo=dt.UTC)
        profile = profile_at(settings.derived_dir, row.symbol, row.expiry, created)
        extra = band_context(profile, float(row.entry))
        if not extra:
            missing_profile += 1
            continue
        repository.enrich_context(int(row.id), extra)
        written += 1
    print(
        f"Backfill band metrik: {written} doplněno, {skipped} už mělo, "
        f"{missing_profile} bez profilu/mimo mřížku (z {len(rows)} setupů)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
