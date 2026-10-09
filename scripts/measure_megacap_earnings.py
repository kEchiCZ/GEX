"""Předregistrované měření výjimky mega caps pro kartu breaking news (ADR-0059 bod 4, #1491).

Otázka: hýbou titulky o výsledcích mega caps Nasdaqem víc než ostatní titulky
kategorie `EARNINGS`? Když ano, zpráva tier 1–2 o výsledcích mega caps smí na
kartu, přestože je `EARNINGS` z významnosti vyřazené (ADR-0045, #1291).

Postup zapsaný v #1491 před spuštěním:

* data: `news_events.category = 'EARNINGS'` × `news_reactions` NQ, okno 5 min
  bez kontaminace (`cont_5` false), s `range_5`, bez odložených reakcí
  (`deferred_min`; dynamika gapu po uzavírce je jiná, ADR-0043, #1311);
* éra: primárně jen `ts_event ≥ 2026-07-28` (živý sběr) — backfill Alpaca
  před tímto datem bral jen mega caps a indexová ETF, srovnávací skupina by
  byla vychýlená; plná historie jen sekundárně;
* skupiny: mega = `symbols` ∩ `MEGA_CAP` (backfill Alpaca), ostatní = zbytek;
* statistika: rozdíl mediánů `range_5` (mega − ostatní), 95% percentilový
  bootstrap, 10 000 opakování, pevný seed; |`ret_5`| sekundárně;
* jednotka: titulek (znění ADR) a kontrola robustnosti přes unikátní minutu
  okna NQ — titulky téže minuty mají tytéž bary, takže nejsou nezávislá
  měření; minuta s titulkem mega cap patří jen do skupiny mega;
* kritérium: n ≥ 30 v každé skupině a dolní mez CI > 0 v obou jednotkách;
  neshoda jednotek → rozhodne vlastník.

Spojení je jen pro čtení (`default_transaction_read_only`, transakce končí
rollbackem). Spuštění z hostitele (URL se nikdy nevypisuje):
    uv run python scripts/measure_megacap_earnings.py
URL z `--db`, `GEXLENS_NEWS_DATABASE_URL`, `GEXLENS_HOST_DATABASE_URL`, nebo
`GEXLENS_PG_PASSWORD`. Výstup je Markdown na stdout.
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from alpaca_news_backfill import MEGA_CAP  # noqa: E402
from reclassify_news_rules import database_url, make_engine  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402

from gexlens_engine.storage.sentiment import news_events, news_reactions  # noqa: E402

#: Začátek živého sběru (#1392 éry korpusu); dřív jen filtrovaný backfill
LIVE_FROM = dt.datetime(2026, 7, 28, tzinfo=dt.UTC)
SYMBOL = "NQ"
CATEGORY = "EARNINGS"
MIN_N = 30
RESAMPLES = 10_000
CONFIDENCE = 0.95
SEED = 1491
#: Bootstrap po blocích — matice indexů R × n by u ~3 000 titulků měla 240 MB
CHUNK = 500


@dataclass(frozen=True)
class Reaction:
    """Reakce NQ v okně 5 min na jeden titulek `EARNINGS`."""

    ts_event: dt.datetime
    symbols: frozenset[str]
    range_bp: float
    ret_bp: float


@dataclass(frozen=True)
class Comparison:
    """Rozdíl mediánů mega − ostatní s percentilovým bootstrap CI."""

    n_mega: int
    n_other: int
    median_mega: float
    median_other: float
    diff: float
    ci_low: float
    ci_high: float

    @property
    def passes(self) -> bool:
        return self.n_mega >= MIN_N and self.n_other >= MIN_N and self.ci_low > 0


def is_mega(symbols: Iterable[str]) -> bool:
    return not MEGA_CAP.isdisjoint(symbols)


def split_headlines(
    rows: Sequence[Reaction], value: Callable[[Reaction], float]
) -> tuple[list[float], list[float]]:
    """Jednotka titulek: každý titulek je jedno měření."""
    mega = [value(row) for row in rows if is_mega(row.symbols)]
    other = [value(row) for row in rows if not is_mega(row.symbols)]
    return mega, other


def split_windows(
    rows: Sequence[Reaction], value: Callable[[Reaction], float]
) -> tuple[list[float], list[float]]:
    """Jednotka minuta okna NQ: titulky téže minuty mají (téměř) tytéž bary.

    Minuta s titulkem mega cap patří jen do skupiny mega; hodnotou minuty je
    medián hodnot jejích titulků. Přiblížení: okno začíná přesně v `ts_event`
    (`reactions.compute_reactions`), takže titulek v hh:mm:00 má okno o bar
    dřív než titulek v hh:mm:01–59 (rozdíl v reportu #1491).
    """
    minutes: dict[dt.datetime, list[Reaction]] = {}
    for row in rows:
        minutes.setdefault(row.ts_event.replace(second=0, microsecond=0), []).append(row)
    mega: list[float] = []
    other: list[float] = []
    for members in minutes.values():
        target = mega if any(is_mega(row.symbols) for row in members) else other
        target.append(float(np.median([value(row) for row in members])))
    return mega, other


def bootstrap_median_diff(
    mega: Sequence[float],
    other: Sequence[float],
    *,
    resamples: int = RESAMPLES,
    seed: int = SEED,
    confidence: float = CONFIDENCE,
) -> Comparison:
    """Rozdíl mediánů a jeho percentilový bootstrap CI (obě skupiny se převzorkují)."""
    if not mega or not other:
        nan = float("nan")
        return Comparison(len(mega), len(other), nan, nan, nan, nan, nan)
    a = np.asarray(mega, dtype=float)
    b = np.asarray(other, dtype=float)
    rng = np.random.default_rng(seed)
    diffs = np.empty(resamples)
    for start in range(0, resamples, CHUNK):
        size = min(CHUNK, resamples - start)
        sample_a = np.median(a[rng.integers(0, a.size, size=(size, a.size))], axis=1)
        sample_b = np.median(b[rng.integers(0, b.size, size=(size, b.size))], axis=1)
        diffs[start : start + size] = sample_a - sample_b
    diffs.sort()
    alpha = 1 - confidence
    low = diffs[math.floor(alpha / 2 * resamples)]
    high = diffs[math.ceil((1 - alpha / 2) * resamples) - 1]
    median_a, median_b = float(np.median(a)), float(np.median(b))
    return Comparison(
        a.size, b.size, median_a, median_b, median_a - median_b, float(low), float(high)
    )


def verdict(comparisons: Sequence[Comparison]) -> str:
    """Kritérium platí jen ve všech jednotkách; neshoda je na vlastníkovi."""
    results = {comparison.passes for comparison in comparisons}
    if results == {True}:
        return "**splněno** — výjimka mega caps se zavádí"
    if results == {False}:
        return "**nesplněno** — výjimka mega caps se nezavádí (ADR-0059 varianta A)"
    return "**neshoda jednotek** — rozhodne vlastník"


def load_reactions(engine: Engine, *, since: dt.datetime | None) -> list[Reaction]:
    reaction = news_reactions.c
    stmt = (
        select(
            news_events.c.ts_event,
            news_events.c.symbols,
            reaction.range_5,
            reaction.ret_5,
        )
        .join(news_reactions, news_reactions.c.event_id == news_events.c.id)
        .where(
            news_events.c.category == CATEGORY,
            reaction.symbol == SYMBOL,
            reaction.cont_5.is_(False),
            reaction.range_5.is_not(None),
            reaction.ret_5.is_not(None),
            reaction.deferred_min.is_not(True),
        )
    )
    if since is not None:
        stmt = stmt.where(news_events.c.ts_event >= since)
    with engine.connect() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(text("SET TRANSACTION READ ONLY"))
        rows = conn.execute(stmt).fetchall()
        conn.rollback()
    return [
        Reaction(
            ts_event=row.ts_event if row.ts_event.tzinfo else row.ts_event.replace(tzinfo=dt.UTC),
            symbols=frozenset(str(symbol) for symbol in row.symbols or []),
            range_bp=float(row.range_5),
            ret_bp=float(row.ret_5),
        )
        for row in rows
    ]


def _fmt(value: float) -> str:
    return "—" if math.isnan(value) else f"{value:.2f}"


def report(rows: Sequence[Reaction], title: str) -> tuple[str, list[Comparison]]:
    """Tabulka jednotka × metrika; vrací i srovnání `range_5` pro verdikt."""

    def abs_ret(row: Reaction) -> float:
        return abs(row.ret_bp)

    def range_bp(row: Reaction) -> float:
        return row.range_bp

    lines = [
        f"### {title}",
        "",
        "| jednotka | metrika | n mega | n ostatní | medián mega (bp) | medián ostatní (bp) "
        "| rozdíl (bp) | 95% CI | kritérium |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    criterion: list[Comparison] = []
    for unit, split in (("titulek", split_headlines), ("minuta okna", split_windows)):
        for metric, value in (("range_5", range_bp), ("|ret_5|", abs_ret)):
            comparison = bootstrap_median_diff(*split(rows, value))
            if metric == "range_5":
                criterion.append(comparison)
            mark = ("✓" if comparison.passes else "✗") if metric == "range_5" else "(sekundární)"
            lines.append(
                f"| {unit} | {metric} | {comparison.n_mega} | {comparison.n_other} "
                f"| {_fmt(comparison.median_mega)} | {_fmt(comparison.median_other)} "
                f"| {_fmt(comparison.diff)} | [{_fmt(comparison.ci_low)}; "
                f"{_fmt(comparison.ci_high)}] | {mark} |"
            )
    return "\n".join(lines), criterion


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--db", help="SQLAlchemy URL; jinak z prostředí (nevypisuje se)")
    args = parser.parse_args(argv)
    engine = make_engine(database_url(args.db), read_only=True)
    as_of = dt.datetime.now(dt.UTC).replace(microsecond=0)
    print(f"`as_of` = {as_of.isoformat()}, seed {SEED}, {RESAMPLES} opakování, "
          f"mega caps = {len(MEGA_CAP)} tickerů\n")  # fmt: skip
    live_table, live_criterion = report(
        load_reactions(engine, since=LIVE_FROM),
        f"Primárně: živý sběr od {LIVE_FROM.date().isoformat()}",
    )
    print(live_table + "\n")
    print(f"Verdikt: {verdict(live_criterion)}\n")
    history = load_reactions(engine, since=None)
    full_table, _ = report(history, "Sekundárně: celá historie")
    print(full_table + "\n")
    # Rozpad celé historie: backfill bral jen mega caps a indexová ETF
    backfill = [row for row in history if row.ts_event < LIVE_FROM]
    backfill_table, _ = report(
        backfill, f"Sekundárně: jen backfill (před {LIVE_FROM.date().isoformat()})"
    )
    print(backfill_table)


if __name__ == "__main__":
    main()
