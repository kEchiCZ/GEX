"""Walk-forward kritéria brány šablon (#1334) — jen čtení.

Brána buňky šablona × symbol (`risk.template_gate`, #1325) pustí setup jen
s kladnou dolní mezí jednostranného 95% intervalu Ø R při n ≥
`template_gate_min_samples` v okně `template_gate_days` seancí. Skript pro každý
uzavřený setup mechaniky (výchozí v5) vzniklý v seancích [--from, --to] spočítá
verdikt několika kritérií z řádků téže buňky uzavřených **před jeho vznikem**
(point-in-time, okno `gate_window_start`) a porovná ho s výsledkem setupu po
nákladech (`net_r`: komise + 1 tick skluzu na celý obchod mikra podle konstant
`ROUND_TRIP_COST_USD`; text ADR-0030 uvádí 1 tick na stranu, rozpor řeší #1326
a #1402).
Otázka (#1325 bod 4) zní, zda verdikt předpovídá výsledek dalších setupů, ne
zda by kritérium zpětně vydělalo.

Kritéria (všechna jen při n ≥ min_samples, jinak „málo dat“ = nepustí):

* **LB** — dnešní brána: dolní mez Ø hrubého R > 0 (z = 1,645);
* **LBč** — totéž nad čistým R (#1402);
* **ØR** — Ø hrubého R > 0 (varianta D z #1325);
* **ØRč** — Ø čistého R > 0;
* **LBč90** — „nižší z“: dolní mez jednostranného 90% intervalu (z = 1,282)
  Ø čistého R > 0.

Vzorek je týž jako v enginu: `SetupsRepository.realized_since` (jen setupy ve
statistikách, tedy bez vzniku po settle #1324 a bez `context.excluded` #1346)
a `risk.affordable_rows` (stop v rozpočtu). Stín se počítá jako obchodovatelné,
na `tradeable` se nefiltruje. Parametry brány a účtu bere z platné verze
`setup_params` (bez řádku defaulty kódu). Parita: přepočtený verdikt LB se
porovná s verdiktem, který živá brána uložila do `context.template_gate`.
Parametry jsou dnešní verze, ne verze platná při vzniku setupu; parita ukáže,
kde se rozcházejí.

Spuštění z hostitele (URL se nikdy nevypisuje, PG jen čtením):
    uv run python scripts/measure_template_gate_1334.py [--from 2026-09-30] [--to 2026-10-27]
URL z `--db`, `GEXLENS_HOST_DATABASE_URL`, nebo `GEXLENS_PG_PASSWORD`.
Výstup je Markdown na stdout.
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

from reclassify_news_rules import database_url, make_engine  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402

from gexlens_engine.compute.paper import POINT_VALUES  # noqa: E402
from gexlens_engine.compute.risk import (  # noqa: E402
    RealizedSetup,
    affordable_rows,
    expectancy_lower_bound,
    gate_window_start,
)
from gexlens_engine.compute.settle import (  # noqa: E402
    is_trading_session,
    trading_session_date,
)
from gexlens_engine.compute.setup_summary import net_r  # noqa: E402
from gexlens_engine.compute.setups import SETUP_MECHANICS_VERSION, SetupParams  # noqa: E402
from gexlens_engine.storage.setup_params_store import SetupParamsRepository  # noqa: E402
from gexlens_engine.storage.setups_store import SetupsRepository, setups_table  # noqa: E402
from gexlens_engine.ticker import symbol_root  # noqa: E402

#: Začátek out-of-sample: první seance bez setupů po settle (#1324) a s klíčem
#: brány šablona × symbol (#1325)
DEFAULT_FROM = dt.date(2026, 9, 30)
#: Jednostranný 90% kvantil pro „nižší z“ (#1325 varianta D)
Z_90 = 1.282
#: Dvoustranný 95% kvantil pro interval Ø výsledku
Z_CI = 1.96

GateTest = Callable[[Sequence[float], Sequence[float]], bool]


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _positive(value: float | None) -> bool:
    return value is not None and value > 0


@dataclass(frozen=True)
class Criterion:
    key: str
    label: str
    #: (hrubé R okna, čisté R okna) → pustí? Volá se jen při n ≥ min_samples
    test: GateTest


CRITERIA: tuple[Criterion, ...] = (
    Criterion(
        "LB", "LB hrubě > 0 (dnešní brána)", lambda g, n: _positive(expectancy_lower_bound(g))
    ),
    Criterion("LBč", "LB čistě > 0", lambda g, n: _positive(expectancy_lower_bound(n))),
    Criterion("ØR", "Ø hrubě > 0 (varianta D)", lambda g, n: _mean(g) > 0),
    Criterion("ØRč", "Ø čistě > 0", lambda g, n: _mean(n) > 0),
    Criterion(
        "LBč90",
        "LB90 čistě > 0 (nižší z)",
        lambda g, n: _positive(expectancy_lower_bound(n, z=Z_90)),
    ),
)


@dataclass(frozen=True)
class Event:
    """Setup out-of-sample s verdikty kritérií z okna před vznikem."""

    symbol: str
    template: str
    created_ts: dt.datetime
    session: dt.date
    gross: float
    net: float
    window_n: int
    lb_net: float | None
    avg_net: float | None
    #: klíč kritéria → pustí
    verdicts: dict[str, bool]


def row_net(row: RealizedSetup) -> float | None:
    """Čisté R řádku (ADR-0030, mikro), stejně jako `cell_evidence` pod v5:
    konstanty `ROUND_TRIP_COST_USD` i po bumpu v6 (měření #1334 stojí na nich)."""
    return net_r(row.outcome_r, abs(row.entry - row.stop), row.symbol, POINT_VALUES, per_side=False)


def cell_rows(
    realized: Sequence[RealizedSetup], template: str, symbol: str, params: SetupParams
) -> list[RealizedSetup]:
    """Řádky buňky se stopem v rozpočtu (`affordable_rows` bez dolní meze okna)."""
    return affordable_rows(
        realized,
        template,
        symbol,
        since=dt.datetime.min.replace(tzinfo=dt.UTC),
        point_value_usd=POINT_VALUES[symbol_root(symbol)],
        account_equity_usd=params.account_equity_usd,
        risk_pct=params.risk_pct,
        risk_max_pct=params.risk_max_pct,
    )


def build_events(
    realized: Sequence[RealizedSetup],
    params: SetupParams,
    *,
    date_from: dt.date,
    date_to: dt.date | None,
    skipped: dict[str, int],
) -> list[Event]:
    """Setupy vzniklé v [date_from, date_to] s verdikty z okna před vznikem."""
    cells = sorted({(row.template, row.symbol) for row in realized})
    events: list[Event] = []
    for template, symbol in cells:
        rows = cell_rows(realized, template, symbol, params)
        nets = {id(row): row_net(row) for row in rows}
        for row in rows:
            if row.created_ts is None:
                skipped["bez created_ts"] += 1
                continue
            session = trading_session_date(row.created_ts)
            if session < date_from or (date_to is not None and session > date_to):
                continue
            own_net = nets[id(row)]
            if own_net is None:
                skipped["bez čistého R"] += 1
                continue
            start = gate_window_start(row.created_ts, params.template_gate_days)
            window = [other for other in rows if start <= other.closed_ts < row.created_ts]
            gross = [other.outcome_r for other in window]
            net = [value for other in window if (value := nets[id(other)]) is not None]
            enough = len(window) >= params.template_gate_min_samples and len(net) == len(window)
            events.append(
                Event(
                    symbol=symbol,
                    template=template,
                    created_ts=row.created_ts,
                    session=session,
                    gross=row.outcome_r,
                    net=own_net,
                    window_n=len(window),
                    lb_net=expectancy_lower_bound(net),
                    avg_net=_mean(net) if net else None,
                    verdicts={c.key: enough and c.test(gross, net) for c in CRITERIA},
                )
            )
    return sorted(events, key=lambda event: event.created_ts)


# ── Statistika ───────────────────────────────────────────────────────────────


def num(value: float, spec: str) -> str:
    """Číslo v české konvenci: desetinná čárka, tisíce mezerou."""
    return f"{value:{spec}}".replace(",", " ").replace(".", ",")


def mean_ci(values: Sequence[float]) -> str:
    """„n · Σ · Ø ± půlšířka 95% intervalu“ (normální aproximace)."""
    n = len(values)
    if n == 0:
        return "0"
    total = sum(values)
    mean = total / n
    head = f"{n} · Σ {num(total, '+.2f')} · Ø {num(mean, '+.3f')}"
    if n < 2:
        return head
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))
    return f"{head} ± {num(Z_CI * sd / math.sqrt(n), '.3f')}"


def _ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float] | None:
    """Spearmanovo ρ a dvoustranné p (t aproximace normálou); None pod 4 páry."""
    n = len(xs)
    if n < 4:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = _mean(rx), _mean(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx == 0 or vy == 0:
        return None
    rho = cov / math.sqrt(vx * vy)
    if abs(rho) >= 1:
        return rho, 0.0
    t = rho * math.sqrt((n - 2) / (1 - rho**2))
    return rho, math.erfc(abs(t) / math.sqrt(2))


# ── Parita s živou bránou ────────────────────────────────────────────────────


def stored_verdicts(
    engine: Engine, *, mechanics_version: int, since: dt.datetime
) -> dict[tuple[str, str, dt.datetime], str]:
    """`context.template_gate` uložený při vzniku, klíč (symbol, šablona, vznik)."""
    stmt = select(
        setups_table.c.symbol,
        setups_table.c.template,
        setups_table.c.created_ts,
        setups_table.c.context,
    ).where(
        setups_table.c.mechanics_version == mechanics_version,
        setups_table.c.created_ts >= since,
    )
    result: dict[tuple[str, str, dt.datetime], str] = {}
    with engine.connect() as conn:
        for row in conn.execute(stmt):
            context = row.context if isinstance(row.context, dict) else {}
            verdict = context.get("template_gate")
            created = row.created_ts
            if created.tzinfo is None:
                created = created.replace(tzinfo=dt.UTC)
            if isinstance(verdict, str):
                result[(str(row.symbol), str(row.template), created)] = verdict
    return result


# ── Report ───────────────────────────────────────────────────────────────────


def report(
    events: Sequence[Event],
    params: SetupParams,
    *,
    params_label: str,
    mechanics_version: int,
    date_from: dt.date,
    date_to: dt.date | None,
    stored: dict[tuple[str, str, dt.datetime], str],
    skipped: dict[str, int],
) -> list[str]:
    sessions = sorted({event.session for event in events})
    last = date_to or (sessions[-1] if sessions else date_from)
    span = [
        date_from + dt.timedelta(days=offset)
        for offset in range((last - date_from).days + 1)
        if is_trading_session(date_from + dt.timedelta(days=offset))
    ]
    out = [
        f"## Walk-forward kritéria brány šablon (#1334), mechanika v{mechanics_version}",
        "",
        f"- Out-of-sample: seance {date_from} až {last}, obchodních seancí "
        f"{len(span)}, se setupem {len(sessions)}; setupů {len(events)} "
        "(ve statistikách, stop v rozpočtu).",
        f"- Parametry: {params_label}; okno {params.template_gate_days} seancí "
        f"(`gate_window_start`), n ≥ {params.template_gate_min_samples}, účet "
        f"{num(params.account_equity_usd, ',.0f')} $, riziko {num(params.risk_pct, 'g')} % "
        f"/ max {num(params.risk_max_pct, 'g')} %.",
        "- Čisté R = R − round-trip náklad mikra / (stop × bod mikra), ADR-0030 "
        "(komise + 1 tick skluzu na obchod, konstanty `ROUND_TRIP_COST_USD`; #1326).",
    ]
    if skipped:
        out.append(
            "- Vynecháno: " + ", ".join(f"{key} {count}" for key, count in sorted(skipped.items()))
        )

    matched = 0
    mismatched: list[str] = []
    for event in events:
        live = stored.get((event.symbol, event.template, event.created_ts))
        if live not in ("pass", "block", "insufficient"):
            continue
        if (live == "pass") == event.verdicts["LB"]:
            matched += 1
        else:
            mismatched.append(
                f"{event.template} {event.symbol} {event.created_ts:%Y-%m-%d %H:%M} UTC "
                f"(živě {live}, přepočet n = {event.window_n})"
            )
    out.append(
        f"- Parita LB s živou bránou (`context.template_gate`): shoda {matched}, "
        f"rozdíl {len(mismatched)}, bez uloženého verdiktu "
        f"{len(events) - matched - len(mismatched)}."
    )
    out += [f"  - rozdíl: {item}" for item in mismatched]

    out += [
        "",
        "### Kritéria: výsledek setupů, které by pustila, a ostatních (čisté R)",
        "Buňka-seance = verdikt prvního setupu buňky v seanci a Σ čistého R jejích setupů "
        "(setupy jedné seance nejsou nezávislé).",
        "",
        "| kritérium | pustí: n · Σ · Ø ± 95 % | nepustí: n · Σ · Ø ± 95 % "
        "| buňko-seance pustí | buňko-seance nepustí |",
        "|---|---|---|---|---|",
    ]
    by_cell_session: dict[tuple[str, str, dt.date], list[Event]] = defaultdict(list)
    for event in events:
        by_cell_session[(event.template, event.symbol, event.session)].append(event)
    for criterion in CRITERIA:
        passed = [e.net for e in events if e.verdicts[criterion.key]]
        blocked = [e.net for e in events if not e.verdicts[criterion.key]]
        cs_pass = [
            sum(e.net for e in group)
            for group in by_cell_session.values()
            if group[0].verdicts[criterion.key]
        ]
        cs_block = [
            sum(e.net for e in group)
            for group in by_cell_session.values()
            if not group[0].verdicts[criterion.key]
        ]
        out.append(
            f"| {criterion.label} | {mean_ci(passed)} | {mean_ci(blocked)} "
            f"| {mean_ci(cs_pass)} | {mean_ci(cs_block)} |"
        )

    out += [
        "",
        "### Buňky šablona × symbol",
        "",
        "| buňka | OOS čistě: n · Σ · Ø ± 95 % | OOS hrubě Ø | okno n (první → poslední) "
        "| LB čistě (první → poslední) | pustí (podíl setupů) |",
        "|---|---|---|---|---|---|",
    ]
    by_cell: dict[tuple[str, str], list[Event]] = defaultdict(list)
    for event in events:
        by_cell[(event.template, event.symbol)].append(event)
    for (template, symbol), group in sorted(by_cell.items()):
        first, final = group[0], group[-1]
        shares = ", ".join(
            f"{c.key} {sum(e.verdicts[c.key] for e in group) / len(group):.0%}" for c in CRITERIA
        )
        lb = " → ".join(
            "—" if value is None else num(value, "+.3f") for value in (first.lb_net, final.lb_net)
        )
        out.append(
            f"| {template} {symbol} | {mean_ci([e.net for e in group])} "
            f"| {num(_mean([e.gross for e in group]), '+.3f')} "
            f"| {first.window_n} → {final.window_n} | {lb} | {shares} |"
        )

    minimum = params.template_gate_min_samples
    out += [
        "",
        "### Předpovídá okno výsledek? (Spearman, čisté R)",
        "Setup: statistika okna při vzniku × R setupu. Buňko-seance: statistika okna "
        "u prvního setupu × Σ R buňky v seanci (p bez závislosti uvnitř seance).",
        "",
    ]
    picks: tuple[tuple[str, Callable[[Event], float | None]], ...] = (
        ("LB čistě okna", lambda e: e.lb_net),
        ("Ø čistě okna", lambda e: e.avg_net),
    )
    for label, pick in picks:
        for scope, enough in (("všechny", 0), (f"okno n ≥ {minimum}", minimum)):
            pairs = [
                (x, e.net) for e in events if e.window_n >= enough and (x := pick(e)) is not None
            ]
            grouped = [
                (x, sum(e.net for e in group))
                for group in by_cell_session.values()
                if group[0].window_n >= enough and (x := pick(group[0])) is not None
            ]
            texts = []
            for unit, data in (("setup", pairs), ("buňko-seance", grouped)):
                result = spearman([p[0] for p in data], [p[1] for p in data])
                stat = (
                    "—"
                    if result is None
                    else f"ρ {num(result[0], '+.3f')}, p ≈ {num(result[1], '.2f')}"
                )
                texts.append(f"{unit} n = {len(data)}: {stat}")
            out.append(f"- {label} ({scope}): " + "; ".join(texts))
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", help="SQLAlchemy URL (jinak z prostředí)")
    parser.add_argument(
        "--from", dest="date_from", type=dt.date.fromisoformat, default=DEFAULT_FROM
    )
    parser.add_argument("--to", dest="date_to", type=dt.date.fromisoformat, default=None)
    parser.add_argument("--mechanics", type=int, default=SETUP_MECHANICS_VERSION)
    args = parser.parse_args(argv)

    engine = make_engine(database_url(args.db), read_only=True)
    stored_params = SetupParamsRepository(engine).latest()
    params = stored_params.params if stored_params is not None else SetupParams()
    params_label = (
        f"store v{stored_params.version}" if stored_params is not None else "defaulty kódu"
    )
    start = dt.datetime.combine(args.date_from, dt.time(), tzinfo=dt.UTC)
    # Okno prvního setupu sahá o `template_gate_days` seancí před --from
    since = gate_window_start(start, params.template_gate_days) - dt.timedelta(days=2)
    realized = SetupsRepository(engine).realized_since(since, mechanics_version=args.mechanics)
    skipped: dict[str, int] = defaultdict(int)
    events = build_events(
        realized, params, date_from=args.date_from, date_to=args.date_to, skipped=skipped
    )
    stored = stored_verdicts(engine, mechanics_version=args.mechanics, since=since)
    lines = report(
        events,
        params,
        params_label=params_label,
        mechanics_version=args.mechanics,
        date_from=args.date_from,
        date_to=args.date_to,
        stored=stored,
        skipped=dict(skipped),
    )
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
