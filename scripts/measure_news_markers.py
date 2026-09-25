"""Měření `GET /news/markers` nad produkčními zprávami (#1290) — jen čtení.

Staví jen router SentimentLensu (tentýž kód jako API, včetně GZip middleware)
nad read-only spojením na produkční PG a pro každou seanci měří: počet řádků,
čas odpovědi (SQL + render JSON), velikost JSON a přenesenou velikost s gzip.
Volitelně jednou i dosavadní zdroj markerů grafu `/news?limit=100` (feed
s reakcemi a indexem tématu) pro srovnání.

Seance = obchodní den [17:00 CT D−1, 17:00 CT D) jako ve frontendu
(`sessionBoundsUtc`) i v API (ADR-0023).

Spuštění (z hostitele, PG publikované na 55432; URL se nikdy nevypisuje):
    uv run --env-file .env python scripts/measure_news_markers.py \\
        --dates 2026-09-16 2026-09-24 [--repeat 3] [--feed]

URL: `--db`, jinak `GEXLENS_HOST_DATABASE_URL`, jinak z `GEXLENS_PG_PASSWORD`
(sdílené s `measure_news_anomaly.py`).
"""

from __future__ import annotations

import argparse
import datetime as dt
import statistics
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.gzip import GZipMiddleware  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from measure_news_anomaly import database_url, read_only_engine  # noqa: E402

from gexlens_api.sentiment_routes import build_sentiment_router  # noqa: E402

SESSION_TZ = ZoneInfo("America/Chicago")


def session_bounds(date: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """[17:00 CT D−1, 17:00 CT D) v UTC — protějšek `sessionBoundsUtc`."""
    previous = date - dt.timedelta(days=1)
    open_local = dt.datetime.combine(previous, dt.time(17), tzinfo=SESSION_TZ)
    close_local = dt.datetime.combine(date, dt.time(17), tzinfo=SESSION_TZ)
    return open_local.astimezone(dt.UTC), close_local.astimezone(dt.UTC)


def timed_get(
    client: TestClient, path: str, params: dict[str, str], repeat: int
) -> tuple[list[float], int, int, int]:
    """Časy (ms), počet řádků, velikost JSON a přenesené bajty s gzip."""
    times: list[float] = []
    rows = raw_bytes = wire_bytes = 0
    for _ in range(repeat):
        started = time.perf_counter()
        response = client.get(path, params=params, headers={"Accept-Encoding": "gzip"})
        times.append((time.perf_counter() - started) * 1000)
        response.raise_for_status()
        rows = len(response.json()["news"])
        raw_bytes = len(response.content)
        wire_bytes = response.num_bytes_downloaded
    return times, rows, raw_bytes, wire_bytes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dates", nargs="+", required=True, help="data seancí YYYY-MM-DD")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--db", default=None)
    parser.add_argument(
        "--feed", action="store_true", help="změř i dosavadní /news?limit=100 (jednou)"
    )
    args = parser.parse_args()

    engine = read_only_engine(database_url(args.db))
    app = FastAPI()
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)
    app.include_router(build_sentiment_router(lambda: engine, ROOT / "data"))
    client = TestClient(app)

    print("| seance | řádků | medián ms | min ms | JSON kB | gzip kB | B/řádek gzip |")
    print("|---|---:|---:|---:|---:|---:|---:|")
    for raw_date in args.dates:
        start, end = session_bounds(dt.date.fromisoformat(raw_date))
        times, rows, raw_bytes, wire_bytes = timed_get(
            client,
            "/news/markers",
            {"from": start.isoformat(), "to": end.isoformat()},
            args.repeat,
        )
        per_row = wire_bytes / rows if rows else 0.0
        print(
            f"| {raw_date} | {rows} | {statistics.median(times):.0f} | {min(times):.0f} | "
            f"{raw_bytes / 1024:.0f} | {wire_bytes / 1024:.0f} | {per_row:.0f} |"
        )

    # Proklik z upozornění: výčet id (typicky 1–5 zpráv)
    start, end = session_bounds(dt.date.fromisoformat(args.dates[-1]))
    sample = client.get("/news/markers", params={"from": start.isoformat(), "to": end.isoformat()})
    ids = ",".join(str(row["id"]) for row in sample.json()["news"][:5])
    if ids:
        times, rows, _raw, _wire = timed_get(client, "/news/markers", {"ids": ids}, args.repeat)
        print(f"\nids ({rows} zpráv): medián {statistics.median(times):.0f} ms")

    if args.feed:
        times, rows, raw_bytes, wire_bytes = timed_get(
            client, "/news", {"limit": "100", "symbol": "ES"}, 1
        )
        print(
            f"\n/news?limit=100 (dosavadní zdroj markerů): {rows} řádků, {times[0]:.0f} ms, "
            f"JSON {raw_bytes / 1024:.0f} kB, gzip {wire_bytes / 1024:.0f} kB"
        )


if __name__ == "__main__":
    main()
