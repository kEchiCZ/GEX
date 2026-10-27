"""Průzkum #1334: pustí některé kritérium brány šablon setupy s kladným čistým R? — jen čtení.

Navazuje na `measure_template_gate_1334.py` (stejný vzorek, okno a čisté R
ADR-0030). Rodina kritérií je pevná předem (`FAMILY`); každé se hodnotí nad celým
out-of-sample, nad polovinami H1/H2 a vnořeně: kritérium s nejvyšší Σ čistého R
puštěných setupů v H1 a jeho výsledek v H2. Kladné Σ jen v celém OOS je výběr
zpětně z týchž dat, ne důkaz. Druhá tabulka dělí setupy podle nákladu v R
(hrubé − čisté R), který je známý už při vzniku ze stopu.

Spuštění z hostitele (URL se nikdy nevypisuje, PG jen čtením):
    uv run python scripts/scan_template_gate_1334.py [--from 2026-09-30] [--to 2026-10-27]
        [--split 2026-10-14]
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import sys
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from measure_template_gate_1334 import DEFAULT_FROM, cell_rows, row_net  # noqa: E402
from reclassify_news_rules import database_url, make_engine  # noqa: E402

from gexlens_engine.compute.risk import expectancy_lower_bound, gate_window_start  # noqa: E402
from gexlens_engine.compute.settle import trading_session_date  # noqa: E402
from gexlens_engine.compute.setups import SETUP_MECHANICS_VERSION, SetupParams  # noqa: E402
from gexlens_engine.storage.setup_params_store import SetupParamsRepository  # noqa: E402
from gexlens_engine.storage.setups_store import SetupsRepository  # noqa: E402

#: Hranice polovin OOS: H1 = [--from, split), H2 = [split, --to]
DEFAULT_SPLIT = dt.date(2026, 10, 14)


@dataclass(frozen=True)
class Event:
    template: str
    symbol: str
    created_ts: dt.datetime
    session: dt.date
    gross: float
    net: float
    #: (closed_ts, čisté R) řádků buňky uzavřených před vznikem, vzestupně
    cell_history: tuple[tuple[dt.datetime, float], ...]
    #: totéž pro šablonu přes všechny symboly
    template_history: tuple[tuple[dt.datetime, float], ...]

    @property
    def cost_r(self) -> float:
        return self.gross - self.net


Criterion = Callable[[Event], bool]


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _window(history: Sequence[tuple[dt.datetime, float]], created: dt.datetime, days: int):
    start = gate_window_start(created, days)
    return [value for closed, value in history if closed >= start]


def avg_above(days: int, min_n: int, theta: float, *, pooled: bool = False) -> Criterion:
    def test(event: Event) -> bool:
        history = event.template_history if pooled else event.cell_history
        values = _window(history, event.created_ts, days)
        return len(values) >= min_n and _mean(values) > theta

    return test


def lb_above(days: int, min_n: int, theta: float) -> Criterion:
    def test(event: Event) -> bool:
        values = _window(event.cell_history, event.created_ts, days)
        bound = expectancy_lower_bound(values) if len(values) >= min_n else None
        return bound is not None and bound > theta

    return test


def last_trades_positive(count: int) -> Criterion:
    def test(event: Event) -> bool:
        values = [value for _, value in event.cell_history][-count:]
        return len(values) >= count and _mean(values) > 0

    return test


def avg_not_above(days: int, min_n: int) -> Criterion:
    def test(event: Event) -> bool:
        values = _window(event.cell_history, event.created_ts, days)
        return len(values) >= min_n and _mean(values) <= 0

    return test


FAMILY: tuple[tuple[str, Criterion], ...] = (
    ("vše (bez brány)", lambda event: True),
    ("LB čistě > 0", lb_above(60, 30, 0.0)),
    ("LB čistě > −0,1", lb_above(60, 30, -0.1)),
    ("LB čistě > −0,2", lb_above(60, 30, -0.2)),
    ("LB čistě > −0,3", lb_above(60, 30, -0.3)),
    ("Ø čistě > 0", avg_above(60, 30, 0.0)),
    ("Ø čistě > +0,1", avg_above(60, 30, 0.1)),
    ("Ø čistě > +0,2", avg_above(60, 30, 0.2)),
    ("Ø čistě > 0, n ≥ 10", avg_above(60, 10, 0.0)),
    ("Ø čistě > 0, n ≥ 20", avg_above(60, 20, 0.0)),
    ("Ø čistě 20 seancí > 0, n ≥ 10", avg_above(20, 10, 0.0)),
    ("Ø čistě posledních 10 obchodů > 0", last_trades_positive(10)),
    ("Ø čistě posledních 20 obchodů > 0", last_trades_positive(20)),
    ("šablona přes symboly Ø čistě > 0", avg_above(60, 30, 0.0, pooled=True)),
    ("Ø čistě ≤ 0, n ≥ 10 (obráceně)", avg_not_above(60, 10)),
)

#: Hranice pásem nákladu v R
COST_EDGES = (0.0, 0.05, 0.10, 0.15, 0.25, 0.50, math.inf)


def build_events(
    realized, params: SetupParams, *, date_from: dt.date, date_to: dt.date | None
) -> list[Event]:
    cells = sorted({(row.template, row.symbol) for row in realized})
    rows_by_cell = {cell: cell_rows(realized, cell[0], cell[1], params) for cell in cells}
    nets = {id(row): row_net(row) for rows in rows_by_cell.values() for row in rows}
    events: list[Event] = []
    for (template, symbol), rows in rows_by_cell.items():
        template_rows = [
            row for (other, _), group in rows_by_cell.items() if other == template for row in group
        ]
        for row in rows:
            own = nets[id(row)]
            if row.created_ts is None or own is None:
                continue
            session = trading_session_date(row.created_ts)
            if session < date_from or (date_to is not None and session > date_to):
                continue

            def history(source, created=row.created_ts):
                return tuple(
                    sorted(
                        (other.closed_ts, value)
                        for other in source
                        if other.closed_ts < created and (value := nets[id(other)]) is not None
                    )
                )

            events.append(
                Event(
                    template=template,
                    symbol=symbol,
                    created_ts=row.created_ts,
                    session=session,
                    gross=row.outcome_r,
                    net=own,
                    cell_history=history(rows),
                    template_history=history(template_rows),
                )
            )
    return sorted(events, key=lambda event: event.created_ts)


def summary(values: Sequence[float]) -> str:
    """„n · Σ · Ø ± půlšířka 95% intervalu“ s desetinnou čárkou."""
    n = len(values)
    if n == 0:
        return "0"
    total = sum(values)
    mean = total / n
    head = f"{n} · Σ {total:+.1f} · Ø {mean:+.2f}"
    if n >= 2:
        sd = math.sqrt(sum((value - mean) ** 2 for value in values) / (n - 1))
        head += f" ± {1.96 * sd / math.sqrt(n):.2f}"
    return head.replace(".", ",")


def report(events: Sequence[Event], split: dt.date) -> list[str]:
    first = [event for event in events if event.session < split]
    second = [event for event in events if event.session >= split]
    by_cell_session: dict[tuple[str, str, dt.date], list[Event]] = defaultdict(list)
    for event in events:
        by_cell_session[(event.template, event.symbol, event.session)].append(event)
    out = [
        "## Průzkum kritérií brány šablon (#1334), čisté R",
        "",
        f"- Setupů {len(events)} (H1 {len(first)} do {split - dt.timedelta(days=1)}, "
        f"H2 {len(second)} od {split}); seancí {len({e.session for e in events})}.",
        f"- Σ hrubě {sum(e.gross for e in events):+.1f} R, "
        f"Σ čistě {sum(e.net for e in events):+.1f} R, "
        f"Σ nákladů {sum(e.cost_r for e in events):.1f} R".replace(".", ","),
        "",
        "| kritérium | pustí, celé OOS | buňko-seance | pustí v H1 | pustí v H2 |",
        "|---|---|---|---|---|",
    ]
    best: tuple[str, float, list[float]] | None = None
    for label, test in FAMILY:
        in_first = [e.net for e in first if test(e)]
        in_second = [e.net for e in second if test(e)]
        cell_sessions = [
            sum(e.net for e in group) for group in by_cell_session.values() if test(group[0])
        ]
        out.append(
            f"| {label} | {summary([e.net for e in events if test(e)])} "
            f"| {summary(cell_sessions)} | {summary(in_first)} | {summary(in_second)} |"
        )
        if (
            label != FAMILY[0][0]
            and len(in_first) >= 10
            and (best is None or sum(in_first) > best[1])
        ):
            best = (label, sum(in_first), in_second)
    if best is not None:
        out += [
            "",
            f"Vnořeně: nejlepší v H1 = {best[0]} (Σ {best[1]:+.1f} R; ".replace(".", ",")
            + f"v H2 {summary(best[2])}).",
        ]
    out += [
        "",
        "### Náklad v R při vzniku × výsledek",
        "",
        "| náklad v R | čistě, celé OOS | Ø hrubě | čistě H1 | čistě H2 |",
        "|---|---|---|---|---|",
    ]
    for low, high in zip(COST_EDGES, COST_EDGES[1:], strict=False):
        band = [e for e in events if low <= e.cost_r < high]
        if not band:
            continue
        upper = "∞" if math.isinf(high) else f"{high:.2f}"
        out.append(
            f"| {low:.2f}–{upper} | {summary([e.net for e in band])} "
            f"| {_mean([e.gross for e in band]):+.2f} "
            f"| {summary([e.net for e in band if e.session < split])} "
            f"| {summary([e.net for e in band if e.session >= split])} |".replace(".", ",")
        )
    out += [
        "",
        "### Buňky po polovinách",
        "",
        "| buňka | Ø náklad R | Σ hrubě | čistě H1 | čistě H2 |",
        "|---|---|---|---|---|",
    ]
    by_cell: dict[tuple[str, str], list[Event]] = defaultdict(list)
    for event in events:
        by_cell[(event.template, event.symbol)].append(event)
    for (template, symbol), group in sorted(by_cell.items()):
        out.append(
            f"| {template} {symbol} | {_mean([e.cost_r for e in group]):.2f} "
            f"| {sum(e.gross for e in group):+.1f} ".replace(".", ",")
            + f"| {summary([e.net for e in group if e.session < split])} "
            f"| {summary([e.net for e in group if e.session >= split])} |"
        )
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", help="SQLAlchemy URL (jinak z prostředí)")
    parser.add_argument(
        "--from", dest="date_from", type=dt.date.fromisoformat, default=DEFAULT_FROM
    )
    parser.add_argument("--to", dest="date_to", type=dt.date.fromisoformat, default=None)
    parser.add_argument("--split", type=dt.date.fromisoformat, default=DEFAULT_SPLIT)
    parser.add_argument("--mechanics", type=int, default=SETUP_MECHANICS_VERSION)
    args = parser.parse_args(argv)

    engine = make_engine(database_url(args.db), read_only=True)
    stored = SetupParamsRepository(engine).latest()
    params = stored.params if stored is not None else SetupParams()
    start = dt.datetime.combine(args.date_from, dt.time(), tzinfo=dt.UTC)
    since = gate_window_start(start, params.template_gate_days) - dt.timedelta(days=2)
    realized = SetupsRepository(engine).realized_since(since, mechanics_version=args.mechanics)
    events = build_events(realized, params, date_from=args.date_from, date_to=args.date_to)
    print("\n".join(report(events, args.split)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
