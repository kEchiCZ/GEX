"""Lag event loopu během FA jobů po finalitě OI (#1695) — měření na hostiteli, jen čtení.

Spustí `collect_fa_validation` a výpočet ρ (`_collect_rho`) nad jedním dnem
z produkčních partic ve vlákně (`asyncio.to_thread`, jako engine) a souběžně
sondu lagu smyčky (`sleep(0.05)`, maximum zpoždění). Pak rozloží řádkový
postup z doby před opravou (čtení parquetu, `to_pylist`, smyčka v Pythonu)
na kroky, změří lag každého kroku a porovná ho s variantou v pyarrow.

Naměřeno 10. 11. 2026 (ES 9. 11.): před opravou FA validace lag 4,03 s a ρ
4,34 s, z toho `to_pylist` `ts_min` 4,60 s v jednom volání. Po opravě je lag
FA validace 0,09 s a ρ 0,01 s.

Nic nezapisuje: PG jen s `default_transaction_read_only=on`, dedup a zápisy
repozitářů jsou v podtřídách vypnuté (každá expirace se spočítá jako poprvé).
`--steps-only` měří jen kroky jedné partice.

Použití (mimo RTH, hlavní venv, pracovní adresář bez `.env`):
    PGPW=... GEXLENS_DATA_DIR=".../GEX/data" PYTHONPATH=engine/src \
        .venv/Scripts/python.exe -X utf8 scripts/measure_fa_loop_lag_1695.py ES 2026-11-09
"""

import asyncio
import datetime as dt
import logging
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote_plus

import pyarrow.parquet as pq
from sqlalchemy import create_engine, text

from gexlens_engine.compute.marketclock import daily_close_utc
from gexlens_engine.config import Settings
from gexlens_engine.storage import fa_calibration
from gexlens_engine.storage.fa_calibration import FaAlphaRepository
from gexlens_engine.storage.fa_validation import (
    FaValidationRepository,
    collect_fa_validation,
)
from gexlens_engine.storage.oi_archive import OIEodRepository


class NoDedupFaValidation(FaValidationRepository):
    """Každá expirace se počítá jako poprvé; bod se neukládá."""

    def exists(self, symbol: str, expiry: str, day: dt.date) -> bool:
        return False

    def upsert(self, record: object) -> None:  # type: ignore[override]
        return None


class NoDedupAlpha(FaAlphaRepository):
    """Bod ρ seance před `today` se spočítá; historie ani stav se nezapisují."""

    def last_history_day(self, symbol: str) -> dt.date | None:
        return None

    def history_exists(self, symbol: str, day: dt.date) -> bool:
        return False

    def record(self, *args: object, **kwargs: object) -> None:  # type: ignore[override]
        return None

    def set_state(self, *args: object, **kwargs: object) -> None:  # type: ignore[override]
        return None


class Probe:
    """Zpoždění probuzení ze `sleep(0.05)` = jak dlouho smyčka nedostala slovo."""

    def __init__(self) -> None:
        self.max_lag = 0.0
        self.stalls: list[tuple[str, float]] = []
        self._stop = False

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stop:
            start = loop.time()
            await asyncio.sleep(0.05)
            lag = loop.time() - start - 0.05
            self.max_lag = max(self.max_lag, lag)
            if lag >= 0.25:
                self.stalls.append((dt.datetime.now(dt.UTC).strftime("%H:%M:%S.%f")[:-3], lag))

    def stop(self) -> None:
        self._stop = True

    def reset(self) -> None:
        self.max_lag = 0.0
        self.stalls = []


async def measured(
    probe: Probe, label: str, func: object, *args: object, **kwargs: object
) -> object:
    probe.reset()
    start = time.perf_counter()
    result = await asyncio.to_thread(func, *args, **kwargs)  # type: ignore[arg-type]
    took = time.perf_counter() - start
    top = sorted(probe.stalls, key=lambda s: -s[1])[:5]
    print(
        f"[{label}] trvání {took:.2f} s, lag smyčky max {probe.max_lag:.2f} s, "
        f"stallů ≥ 0,25 s {len(probe.stalls)}, nejdelší {[(t, round(v, 2)) for t, v in top]}",
        flush=True,
    )
    return result


def steps_volumes(path: Path, day: dt.date) -> dict[str, float]:
    """`volumes_at_cutoff` po krocích — časy jednotlivých kroků."""
    times: dict[str, float] = {}
    t = time.perf_counter()
    table = pq.read_table(path, columns=["ts_min", "strike", "right", "volume"])
    times["read_table"] = time.perf_counter() - t
    times["rows"] = float(table.num_rows)
    columns = []
    for name in ("ts_min", "strike", "right", "volume"):
        t = time.perf_counter()
        columns.append(table.column(name).to_pylist())
        times[f"to_pylist {name}"] = time.perf_counter() - t
    t = time.perf_counter()
    cutoff = daily_close_utc(day)
    latest: dict[tuple[float, str], tuple[dt.datetime, float]] = {}
    for ts, strike, right, volume in zip(*columns, strict=True):
        if ts is None or ts > cutoff:
            continue
        key = (float(strike), str(right))
        current = latest.get(key)
        if current is None or ts >= current[0]:
            latest[key] = (ts, float(volume) if volume is not None else 0.0)
    times["python loop"] = time.perf_counter() - t
    return times


def volumes_arrow(path: Path, day: dt.date) -> dict[tuple[float, str], float]:
    """Totéž co `volumes_at_cutoff` v pyarrow compute (varianta B, GIL uvolněn)."""
    import pyarrow as pa
    import pyarrow.compute as pc

    cutoff = daily_close_utc(day)
    table = pq.read_table(path, columns=["ts_min", "strike", "right", "volume"])
    ts = table.column("ts_min")
    mask = pc.and_(pc.is_valid(ts), pc.less_equal(ts, pa.scalar(cutoff, type=ts.type)))
    table = table.filter(mask).sort_by([("ts_min", "ascending")])
    grouped = table.group_by(["strike", "right"], use_threads=False).aggregate([("volume", "last")])
    strikes = grouped.column("strike").to_pylist()
    rights = grouped.column("right").to_pylist()
    volumes = grouped.column("volume_last").to_pylist()
    return {
        (float(s), str(r)): float(v) if v is not None else 0.0
        for s, r, v in zip(strikes, rights, volumes, strict=True)
    }


async def main(symbol: str, today: dt.date) -> None:
    settings = Settings()
    password = quote_plus(os.environ["PGPW"])
    engine = create_engine(
        f"postgresql+psycopg://gexlens:{password}@localhost:55432/gexlens",
        connect_args={"options": "-c default_transaction_read_only=on"},
    )
    with engine.connect() as conn:
        ro = conn.execute(text("SHOW default_transaction_read_only")).scalar()
    print(f"PG default_transaction_read_only = {ro}", flush=True)
    if ro != "on":
        raise SystemExit("PG není read-only — konec")
    oi = OIEodRepository(engine)
    fa = NoDedupFaValidation(engine)
    alpha = NoDedupAlpha(engine)

    probe = Probe()
    probe_task = asyncio.create_task(probe.run())
    await asyncio.sleep(1.0)
    print(f"[klid] lag smyčky max {probe.max_lag:.3f} s", flush=True)

    if "--steps-only" in sys.argv:
        return await steps_only(probe, probe_task, settings, symbol, today)
    records = await measured(
        probe,
        f"FA validace {symbol} {today}",
        collect_fa_validation,
        symbol,
        settings.snapshots_dir,
        oi,
        fa,
        today,
        publication_utc=settings.oi_publication_utc,
    )
    print(f"  bodů {len(records)}", flush=True)  # type: ignore[arg-type]

    await measured(
        probe,
        f"ρ {symbol} {today}",
        fa_calibration._collect_rho,
        symbol,
        settings.snapshots_dir,
        oi,
        alpha,
        today,
        publication_utc=settings.oi_publication_utc,
    )

    await steps_only(probe, probe_task, settings, symbol, today)


async def steps_only(
    probe: Probe, probe_task: asyncio.Task[None], settings: Settings, symbol: str, today: dt.date
) -> None:
    previous = fa_calibration._previous_session(today)
    # Největší partice předchozího dne = nejhorší krok volumes_at_cutoff
    paths = [
        p / f"{previous}.parquet"
        for p in (settings.snapshots_dir / symbol).iterdir()
        if (p / f"{previous}.parquet").exists()
    ]
    biggest = max(paths, key=lambda p: p.stat().st_size)
    steps = await measured(
        probe,
        f"kroky volumes_at_cutoff {biggest.parent.name}/{biggest.name}",
        steps_volumes,
        biggest,
        previous,
    )
    for name, value in steps.items():  # type: ignore[union-attr]
        print(f"  {name}: {value:.3f}" if name != "rows" else f"  rows: {value:.0f}", flush=True)

    arrow = await measured(probe, "varianta pyarrow compute", volumes_arrow, biggest, previous)
    from gexlens_engine.storage.fa_validation import volumes_at_cutoff

    reference = volumes_at_cutoff(biggest, previous)
    print(f"  shoda s volumes_at_cutoff: {arrow == reference} ({len(reference)} stran)", flush=True)

    # Jednotlivý to_pylist ts_min: jedno volání C bez uvolnění GIL?
    table = pq.read_table(biggest, columns=["ts_min"])
    await measured(probe, "jen to_pylist ts_min", lambda: table.column("ts_min").to_pylist())

    probe.stop()
    await probe_task


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d %(name)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("sqlalchemy").setLevel(logging.WARNING)
    asyncio.run(main(sys.argv[1], dt.date.fromisoformat(sys.argv[2])))
