"""Rozsah kontaminace K1 u odložené reakce (#1659) — jen čtení.

Odložená reakce (`deferred_min`) měří minutová okna od prvního obchodovaného
baru po uzavírce (`closure_open_ts`, #1574), K1 ale hledá kontaminující eventy
jen v (zpráva, zpráva + 61 min] (`ReactionJob._contaminating`). Skript pro každé
změřené minutové okno odložené reakce spočítá příznak K1 ve třech variantách:

* **C** — dnešní stav: jiná významná zpráva v (zpráva, zpráva + 61 min]
  a před koncem okna (`window_contaminated`); sloupec `cont_<w>` by měl sedět;
* **A** — celá uzavírka: jiná významná zpráva v (zpráva, začátek + okno);
* **B** — 61 min po zprávě + okno po otevření: C ∪ [začátek, začátek + okno).

Všechny varianty berou jen zprávy před koncem okna (pozdější výnos neovlivnily).

„Jiná významná“ = importance ≥ 2 a jiná kategorie (`contaminates`). Pak
odložené buckety režimu `all` týmiž funkcemi jako noční `ModelStatsJob`
(sloučení vzorků jedné uzavírky, #1311) a gate (`gate_open`).

Spuštění z hostitele (URL se nikdy nevypisuje, PG jen čtením):
    uv run python scripts/measure_deferred_contamination.py
URL z `--db`, `GEXLENS_NEWS_DATABASE_URL`, `GEXLENS_HOST_DATABASE_URL`, nebo
`GEXLENS_PG_PASSWORD`. Výstup je Markdown na stdout.
"""

from __future__ import annotations

import argparse
import bisect
import datetime as dt
import sys
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from reclassify_news_rules import database_url, make_engine  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402

from gexlens_engine.compute.signal_gate import gate_open  # noqa: E402
from gexlens_engine.storage.sentiment import news_events, news_reactions  # noqa: E402
from gexlens_news.contamination import CONTAMINATION_SPAN, Contaminators  # noqa: E402
from gexlens_news.model_stats import (  # noqa: E402
    BucketKey,
    BucketStats,
    ReactionSample,
    aggregate_by_regime,
)
from gexlens_news.reactions import DEFAULT_WINDOWS, contaminates  # noqa: E402

VARIANTS = ("C", "A", "B")


@dataclass(frozen=True)
class DeferredWindow:
    """Jedno změřené minutové okno odložené reakce s příznaky K1 všech variant."""

    event_id: int
    symbol: str
    window_min: int
    ts_event: dt.datetime
    closure_open: dt.datetime
    stored: bool
    flags: dict[str, bool]
    sample: ReactionSample


def others_between(
    contaminators: Contaminators,
    category: str | None,
    lo: dt.datetime,
    hi: dt.datetime,
    *,
    lo_inclusive: bool = False,
    hi_inclusive: bool = False,
) -> bool:
    """Je v (lo, hi) kontaminující event? Meze `*_inclusive` = uzavřené."""
    start = (bisect.bisect_left if lo_inclusive else bisect.bisect_right)(contaminators.times, lo)
    end = (bisect.bisect_right if hi_inclusive else bisect.bisect_left)(contaminators.times, hi)
    return any(
        contaminates(category, contaminators.categories[index]) for index in range(start, end)
    )


def variant_flags(
    contaminators: Contaminators,
    category: str | None,
    ts_event: dt.datetime,
    start: dt.datetime,
    window_min: int,
) -> dict[str, bool]:
    """Příznak K1 okna [start, start + okno) podle variant C, A a B.

    Všechny varianty berou jen zprávy před koncem okna — pozdější výnos
    neovlivnily (`window_contaminated`).
    """
    end = start + dt.timedelta(minutes=window_min)
    span_end = ts_event + CONTAMINATION_SPAN
    after_news = others_between(
        contaminators, category, ts_event, min(span_end, end), hi_inclusive=span_end < end
    )
    in_window = (
        others_between(contaminators, category, start, end, lo_inclusive=True)
        if start > ts_event
        else others_between(contaminators, category, ts_event, end)
    )
    return {
        "C": after_news,
        "A": others_between(contaminators, category, ts_event, end),
        "B": after_news or in_window,
    }


def load_contaminators(engine: Engine) -> Contaminators:
    stmt = select(news_events.c.ts_event, news_events.c.category, news_events.c.importance)
    with engine.connect() as conn:
        return Contaminators.build(
            (row.ts_event, row.category, row.importance) for row in conn.execute(stmt)
        )


def iter_windows(
    engine: Engine, contaminators: Contaminators, skipped: Counter[str]
) -> Iterator[DeferredWindow]:
    """Odložená minutová okna vzestupně podle času zprávy (pořadí `ModelStatsJob`).

    Řádky bez `closure_open_ts` nemají začátek oken; počítají se do `skipped`.
    """
    stmt = (
        select(
            news_events.c.category,
            news_events.c.importance,
            news_events.c.surprise_z,
            news_events.c.sentiment_dir,
            news_events.c.ts_event,
            news_reactions.c.event_id,
            news_reactions.c.symbol,
            news_reactions.c.closure_open_ts,
            news_reactions.c.regime_min,
            *(news_reactions.c[f"ret_{w}"] for w in DEFAULT_WINDOWS),
            *(news_reactions.c[f"cont_{w}"] for w in DEFAULT_WINDOWS),
        )
        .select_from(
            news_reactions.join(news_events, news_events.c.id == news_reactions.c.event_id)
        )
        .where(news_reactions.c.deferred_min.is_(True))
        .order_by(news_events.c.ts_event, news_events.c.id)
    )
    with engine.connect() as conn:
        for row in conn.execution_options(yield_per=5000).execute(stmt).mappings():
            if row["closure_open_ts"] is None:
                skipped["closure"] += 1
                continue
            surprise = float(row["surprise_z"]) if row["surprise_z"] is not None else None
            for window_min in DEFAULT_WINDOWS:
                ret_bp = row[f"ret_{window_min}"]
                if ret_bp is None:
                    continue
                flags = variant_flags(
                    contaminators,
                    row["category"],
                    row["ts_event"],
                    row["closure_open_ts"],
                    window_min,
                )
                yield DeferredWindow(
                    event_id=int(row["event_id"]),
                    symbol=str(row["symbol"]),
                    window_min=window_min,
                    ts_event=row["ts_event"],
                    closure_open=row["closure_open_ts"],
                    stored=bool(row[f"cont_{window_min}"]),
                    flags=flags,
                    sample=ReactionSample(
                        category=row["category"],
                        importance=row["importance"],
                        surprise_z=surprise,
                        sentiment_dir=row["sentiment_dir"],
                        symbol=str(row["symbol"]),
                        window_min=window_min,
                        ret_bp=float(ret_bp),
                        contaminated=False,
                        deferred=True,
                        gex_regime=row["regime_min"],
                        ts_event=row["ts_event"],
                        closure_open=row["closure_open_ts"],
                    ),
                )


def buckets(windows: Iterable[DeferredWindow], variant: str) -> list[BucketStats]:
    """Odložené buckety režimu `all` s kontaminací podle varianty."""
    samples = (
        ReactionSample(**{**w.sample.__dict__, "contaminated": w.flags[variant]}) for w in windows
    )
    return [stats for regime, stats in aggregate_by_regime(samples) if regime == "all"]


def report(windows: Sequence[DeferredWindow], skipped: Counter[str]) -> list[str]:
    lines = ["# Kontaminace K1 odložené reakce (#1659)", ""]
    total = len(windows)
    closures = len({(w.symbol, w.closure_open) for w in windows})
    reactions = len({(w.event_id, w.symbol) for w in windows})
    lines.append(
        f"Odložená minutová okna: {total} (reakcí {reactions}, uzavírek × symbol {closures})."
    )
    parity = sum(1 for w in windows if w.flags["C"] != w.stored)
    lines.append(
        f"Parita C × uložené `cont_<w>`: {total - parity}/{total} shodných ({parity} rozdílných)."
    )
    lines.append(f"Odložených řádků bez `closure_open_ts` (přeskočeno): {skipped['closure']}.")
    lines += [
        "",
        "| varianta | kontaminovaná okna | % | přepnuto proti C |",
        "|---|---:|---:|---:|",
    ]
    for variant in VARIANTS:
        flagged = sum(1 for w in windows if w.flags[variant])
        switched = sum(1 for w in windows if w.flags[variant] and not w.flags["C"])
        lines.append(
            f"| {variant} | {flagged} | {100 * flagged / max(1, total):.1f} | {switched} |"
        )
    per_window = Counter((w.window_min, v) for w in windows for v in VARIANTS if w.flags[v])
    lines += ["", "| okno | C | A | B |", "|---:|---:|---:|---:|"]
    for window_min in DEFAULT_WINDOWS:
        n = sum(1 for w in windows if w.window_min == window_min)
        counts = " | ".join(f"{per_window[(window_min, v)]}/{n}" for v in VARIANTS)
        lines.append(f"| {window_min} | {counts} |")
    stats = {variant: buckets(windows, variant) for variant in VARIANTS}
    lines += [
        "",
        "Odložené buckety (režim `all`, n = měření po sloučení uzavírek):",
        "",
        "| varianta | bucketů | Σ n | n ≥ 30 | gate otevřený |",
        "|---|---:|---:|---:|---:|",
    ]
    for variant in VARIANTS:
        items = stats[variant]
        lines.append(
            f"| {variant} | {len(items)} | {sum(s.n for s in items)} | "
            f"{sum(1 for s in items if s.n >= 30)} | "
            f"{sum(1 for s in items if gate_open(s.n, s.hit_rate_lb, s.ret_mean_bp))} |"
        )
    by_variant = {v: {s.key: s for s in stats[v]} for v in VARIANTS}
    opened = {
        s.key
        for variant in VARIANTS
        for s in stats[variant]
        if gate_open(s.n, s.hit_rate_lb, s.ret_mean_bp)
    }
    if opened:
        lines += [
            "",
            "Buckety s otevřeným gate aspoň v jedné variantě "
            "(n, střední výnos v bp, Wilson LB, gate):",
            "",
            "| bucket | C | A | B |",
            "|---|---|---|---|",
        ]

        def max_n(key: BucketKey) -> int:
            return max(by_variant[v][key].n for v in VARIANTS if key in by_variant[v])

        for key in sorted(opened, key=max_n, reverse=True):
            cells = []
            for variant in VARIANTS:
                item = by_variant[variant].get(key)
                if item is None:
                    cells.append("n 0")
                    continue
                gate = gate_open(item.n, item.hit_rate_lb, item.ret_mean_bp)
                lb = "—" if item.hit_rate_lb is None else f"{item.hit_rate_lb:.3f}"
                cells.append(
                    f"n {item.n}, {item.ret_mean_bp:+.1f} bp, LB {lb}{' **gate**' if gate else ''}"
                )
            name = (
                f"{key.category}|{key.importance} {key.surprise_bucket} "
                f"{key.symbol} {key.window_min} min"
            )
            lines.append(f"| {name} | {' | '.join(cells)} |")
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", help="SQLAlchemy URL (jinak z prostředí)")
    args = parser.parse_args(argv)
    engine = make_engine(database_url(args.db), read_only=True)
    contaminators = load_contaminators(engine)
    skipped: Counter[str] = Counter()
    windows = list(iter_windows(engine, contaminators, skipped))
    print("\n".join(report(windows, skipped)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
