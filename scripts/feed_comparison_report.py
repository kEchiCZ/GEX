"""Vyhodnocení shadow sběru (#613): odchylky a stáří per pole.

Z `feed_comparison` spočítá per pole: n, medián a p95 |odchylky|, podíl
vzorků s chybějící stranou, medián a p95 rozdílu stáří. Čísla jsou vstup
pro prahy hystereze fáze 2 (#614) — bez nich by se prahy střílely od boku.

Spuštění:  python scripts/feed_comparison_report.py [--days 7]
Prostředí: GEXLENS_DATABASE_URL (stejné jako engine).
"""

import argparse
import datetime as dt
import statistics
import sys
from pathlib import Path

from sqlalchemy import create_engine, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "src"))

from gexlens_engine.config import load_settings  # noqa: E402
from gexlens_engine.storage.feed_comparison import feed_comparison_table  # noqa: E402


def p95(values: list[float]) -> float | None:
    if len(values) < 20:
        return None
    return sorted(values)[int(len(values) * 0.95)]


def main() -> int:
    parser = argparse.ArgumentParser(description="Report shadow porovnání feedů (#613)")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()

    settings = load_settings()
    engine = create_engine(settings.database_url)
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=args.days)
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                feed_comparison_table.c.field,
                feed_comparison_table.c.value_ibkr,
                feed_comparison_table.c.value_tasty,
                feed_comparison_table.c.delta,
                feed_comparison_table.c.age_ibkr_ms,
                feed_comparison_table.c.age_tasty_ms,
            ).where(feed_comparison_table.c.ts >= since)
        ).fetchall()

    by_field: dict[str, list] = {}
    for row in rows:
        by_field.setdefault(str(row.field), []).append(row)

    print(f"feed_comparison za poslednich {args.days} dni: {len(rows)} radku")
    print("pole      n        med|d|      p95|d|    chybi_ib  chybi_ty  stari_med_ms  stari_p95_ms")
    for field_name in sorted(by_field):
        items = by_field[field_name]
        deltas = [abs(float(r.delta)) for r in items if r.delta is not None]
        missing_ibkr = sum(1 for r in items if r.value_ibkr is None) / len(items)
        missing_tasty = sum(1 for r in items if r.value_tasty is None) / len(items)
        age_diffs = [
            float(r.age_tasty_ms - r.age_ibkr_ms)
            for r in items
            if r.age_ibkr_ms is not None and r.age_tasty_ms is not None
        ]

        def fmt(value: float | None, spec: str = ".5f") -> str:
            return format(value, spec) if value is not None else "-"

        med_delta = statistics.median(deltas) if deltas else None
        med_age = statistics.median(age_diffs) if age_diffs else None
        print(
            f"{field_name:<9} {len(items):<8} {fmt(med_delta):<11} {fmt(p95(deltas)):<9} "
            f"{missing_ibkr:<9.1%} {missing_tasty:<9.1%} "
            f"{fmt(med_age, '.0f'):<13} {fmt(p95(age_diffs), '.0f')}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
