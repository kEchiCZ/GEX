"""Verdikt stínové brány podle polohy v pásmu (#1064, E-0.5 epiky #1385) — jen čtení PG.

Nad uzavřenými setupy mechaniky v5, které nesou bránu v `context` (#1060) a
patří do statistik (`setup_summary.SetupFact.in_stats`: ne po settle, bez
značky #1346), porovná skupiny pass × block obou stínových pravidel
(`band_gate_simple`, `band_gate_regime`; verdikt `unknown` se vynechá) celkem,
per šablona a per symbol. Výhra = R > 0 (jako dlaždice obrazovky Setupy);
čisté R = po nákladech ADR-0030 na 1 mikro (`setup_summary.net_r`).

Síta proti šumu (rozhodnutí 7. 9. 2026 v #1060):
1. min. 100 setupů v menší skupině;
2. Wilsonova dolní mez rozdílu pass − block nad nulou — Wilson je interval
   podílu, proto rozdíl úspěšností (Newcombe, metoda 10); rozdíl Ø R nese
   bootstrap po obchodních seancích (setupy jedné seance nejsou nezávislé);
3. permutační test interakce poloha × gamma režim (p < 0,05) pro
   `band_gate_regime` — Freedman–Lane nad aditivním modelem, statistika =
   součet čtverců interakce; poloha ve třídách pravidla (inside, transition,
   outside = outside ∪ no_zone);
a předregistrovaná předpověď: block horší o ≥ 0,2 R než pass.

Všechny dotazy běží na spojení s `default_transaction_read_only` v transakci
`SET TRANSACTION READ ONLY` (AGENTS.md: data v PG nejdou znovu pořídit);
konec = rollback. Náhoda má pevné semínko — opakovaný běh dá stejná čísla.

Spuštění z hostitele (PG publikované na 55432; URL se nikdy nevypisuje):
    uv run --env-file .env python scripts/measure_band_gate_1064.py --out report.md
URL: `--db`, jinak `GEXLENS_HOST_DATABASE_URL`, jinak sestavená z
`GEXLENS_PG_PASSWORD` (uživatel/DB `gexlens`, 127.0.0.1:55432).
"""

import argparse
import datetime as dt
import math
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from sqlalchemy import URL, create_engine, text

from gexlens_engine.compute.paper import POINT_VALUES
from gexlens_engine.compute.settle import trading_session_date
from gexlens_engine.compute.setup_summary import SetupFact, fact_from_record, net_r
from gexlens_engine.compute.setupstats import wilson_lower_bound

MECHANICS_VERSION = 5
MIN_GROUP = 100
PREDICTED_GAP_R = 0.2
ALPHA = 0.05
N_BOOT = 10_000
N_PERM = 10_000
SEED = 1064
#: Pod tímto n v menší skupině se interval rozpadu nepočítá (falešná přesnost)
MIN_INTERVAL_N = 10
#: Šablona, kterou by brána naostro neřezala (rozhodnutí 7. 9. v #1060)
EXEMPT_TEMPLATE = "max_pain_pin"
RULES = ("band_gate_simple", "band_gate_regime")
#: Třídy polohy, jak je čte pravidlo (outside a no_zone rozhodují stejně)
RULE_POSITION = {
    "inside": "inside",
    "transition": "transition",
    "outside": "outside",
    "no_zone": "outside",
}
REGIMES = ("positive", "negative")


@dataclass(frozen=True)
class Row:
    fact: SetupFact
    band_class: str
    session: dt.date
    r: float
    net: float | None

    def gate(self, rule: str) -> str | None:
        value = getattr(self.fact, rule)
        return value if isinstance(value, str) else None


def _url(explicit: str | None) -> str | URL:
    if explicit:
        return explicit
    url = os.environ.get("GEXLENS_HOST_DATABASE_URL")
    if url:
        return url
    password = os.environ.get("GEXLENS_PG_PASSWORD")
    if not password:
        raise SystemExit("Chybí --db, GEXLENS_HOST_DATABASE_URL nebo GEXLENS_PG_PASSWORD")
    return URL.create(
        "postgresql+psycopg",
        username="gexlens",
        password=password,
        host="127.0.0.1",
        port=55432,
        database="gexlens",
    )


def load(url: str | URL) -> tuple[list[Row], dict[str, int]]:
    """Uzavřené setupy v5 s bránou ve statistikách + počty vyřazených po krocích."""
    engine = create_engine(url, connect_args={"options": "-c default_transaction_read_only=on"})
    with engine.connect() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        records = (
            conn.execute(
                text(
                    "SELECT id, symbol, expiry, template, status, created_ts, closed_ts,"
                    " outcome_r, entry, stop, mechanics_version, context"
                    " FROM setups WHERE mechanics_version = :v"
                ),
                {"v": MECHANICS_VERSION},
            )
            .mappings()
            .all()
        )
        conn.rollback()
    funnel = {"v5": len(records), "uzavřené": 0, "ve statistikách": 0, "s bránou": 0}
    rows: list[Row] = []
    for record in records:
        fact = fact_from_record(dict(record))
        if not fact.is_closed:
            continue
        funnel["uzavřené"] += 1
        if not fact.in_stats:
            continue
        funnel["ve statistikách"] += 1
        if fact.band_gate_simple is None:
            continue
        funnel["s bránou"] += 1
        context = record["context"]
        r = float(fact.outcome_r or 0.0)
        rows.append(
            Row(
                fact=fact,
                band_class=str(context["band_class"]),
                session=trading_session_date(fact.created_ts),
                r=r,
                net=net_r(r, fact.stop_points, fact.symbol, POINT_VALUES),
            )
        )
    return rows, funnel


# ── Statistika ─────────────────────────────────────────────────────


def wilson_interval(k: int, n: int) -> tuple[float, float]:
    """Wilsonův 95% interval podílu; horní mez = 1 − dolní mez neúspěchů."""
    return wilson_lower_bound(k, n), 1.0 - wilson_lower_bound(n - k, n)


def newcombe_diff(k1: int, n1: int, k2: int, n2: int) -> tuple[float, float, float]:
    """Rozdíl podílů p1 − p2 s 95% intervalem Newcombe (hybrid Wilson, metoda 10)."""
    p1, p2 = k1 / n1, k2 / n2
    l1, u1 = wilson_interval(k1, n1)
    l2, u2 = wilson_interval(k2, n2)
    diff = p1 - p2
    low = diff - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2)
    high = diff + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)
    return diff, low, high


def bootstrap_gap(
    pass_rows: Sequence[Row], block_rows: Sequence[Row], rng: np.random.Generator
) -> tuple[float, float, int]:
    """95% percentilový interval Ø R(pass) − Ø R(block), převzorkování po seancích.

    Seance se losuje celá (obě skupiny naráz) — korelace setupů jednoho dne
    zůstane uvnitř vzorku. Vrací (dolní, horní, počet zahozených losů, kde
    jedna skupina vyšla prázdná)."""
    sessions = sorted({row.session for row in (*pass_rows, *block_rows)})
    index = {session: i for i, session in enumerate(sessions)}
    sums = np.zeros((2, len(sessions)))
    counts = np.zeros((2, len(sessions)))
    for group, rows in enumerate((pass_rows, block_rows)):
        for row in rows:
            sums[group, index[row.session]] += row.r
            counts[group, index[row.session]] += 1
    draws = rng.integers(0, len(sessions), size=(N_BOOT, len(sessions)))
    weights = np.zeros((N_BOOT, len(sessions)))
    np.add.at(weights, (np.arange(N_BOOT)[:, None], draws), 1.0)
    group_sums = weights @ sums.T
    group_counts = weights @ counts.T
    valid = (group_counts > 0).all(axis=1)
    gaps = group_sums[valid, 0] / group_counts[valid, 0] - (
        group_sums[valid, 1] / group_counts[valid, 1]
    )
    low, high = np.percentile(gaps, [2.5, 97.5])
    return float(low), float(high), int((~valid).sum())


def interaction_test(rows: Sequence[Row], rng: np.random.Generator) -> tuple[float, float, int]:
    """Permutační test interakce poloha × režim (Freedman–Lane).

    Aditivní model R ~ poloha + režim; permutují se jeho rezidua a pro každý
    los se spočte součet čtverců interakce = RSS(aditivní) − RSS(buňkové
    průměry). Vrací (statistika, p, n)."""
    usable = [row for row in rows if row.fact.gex_regime in REGIMES]
    positions = sorted({RULE_POSITION[row.band_class] for row in usable})
    cells = [(pos, reg) for pos in positions for reg in REGIMES]
    y = np.array([row.r for row in usable])
    pos_idx = np.array([positions.index(RULE_POSITION[row.band_class]) for row in usable])
    reg_idx = np.array([REGIMES.index(str(row.fact.gex_regime)) for row in usable])
    additive = np.column_stack(
        [np.ones(len(y))]
        + [(pos_idx == i).astype(float) for i in range(1, len(positions))]
        + [(reg_idx == 1).astype(float)]
    )
    cell_idx = pos_idx * len(REGIMES) + reg_idx
    full = np.column_stack([(cell_idx == i).astype(float) for i in range(len(cells))])
    hat_add = additive @ np.linalg.pinv(additive)
    hat_full = full @ np.linalg.pinv(full)

    def statistic(values: np.ndarray) -> np.ndarray:
        resid_add = values - hat_add @ values
        resid_full = values - hat_full @ values
        gap: np.ndarray = (resid_add**2).sum(axis=0) - (resid_full**2).sum(axis=0)
        return gap

    observed = float(statistic(y[:, None])[0])
    fitted = hat_add @ y
    resid = y - fitted
    exceed = 0
    for start in range(0, N_PERM, 1_000):
        batch = min(1_000, N_PERM - start)
        perms = np.argsort(rng.random((batch, len(y))), axis=1)
        samples = fitted[:, None] + resid[perms].T
        exceed += int((statistic(samples) >= observed - 1e-12).sum())
    return observed, (exceed + 1) / (N_PERM + 1), len(y)


# ── Report ─────────────────────────────────────────────────────────


def _table(header: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def _r(value: float) -> str:
    return f"{value:+.3f}".replace(".", ",")


def _num(value: float) -> str:
    return f"{value:g}".replace(".", ",")


def _pct(value: float) -> str:
    return f"{value * 100:.1f} %".replace(".", ",")


def _summary(rows: Sequence[Row]) -> list[object]:
    if not rows:
        return [0, "—", "—", "—", "—"]
    nets = [row.net for row in rows if row.net is not None]
    net = _r(sum(nets) / len(nets)) if len(nets) == len(rows) else "—"
    wins = sum(1 for row in rows if row.r > 0)
    return [
        len(rows),
        _pct(wins / len(rows)),
        _r(sum(row.r for row in rows) / len(rows)),
        net,
        _r(sum(row.r for row in rows)),
    ]


def mean_gap(passed: Sequence[Row], blocked: Sequence[Row]) -> float:
    """Ø R(pass) − Ø R(block); obě skupiny neprázdné."""
    return sum(r.r for r in passed) / len(passed) - sum(r.r for r in blocked) / len(blocked)


def split(rows: Sequence[Row], rule: str) -> tuple[list[Row], list[Row]]:
    return (
        [row for row in rows if row.gate(rule) == "pass"],
        [row for row in rows if row.gate(rule) == "block"],
    )


def sieves(rows: Sequence[Row], rule: str, rng: np.random.Generator) -> tuple[str, bool]:
    """Tabulka sít jednoho pravidla nad danou množinou + zda prošla všechna."""
    passed, blocked = split(rows, rule)
    n_min = min(len(passed), len(blocked))
    if n_min == 0:
        return "jedna ze skupin je prázdná", False
    k_pass = sum(1 for row in passed if row.r > 0)
    k_block = sum(1 for row in blocked if row.r > 0)
    win_gap, win_low, win_high = newcombe_diff(k_pass, len(passed), k_block, len(blocked))
    gap = mean_gap(passed, blocked)
    low, high, dropped = bootstrap_gap(passed, blocked, rng)
    checks: list[tuple[str, str, bool]] = [
        (f"n v menší skupině ≥ {MIN_GROUP}", str(n_min), n_min >= MIN_GROUP),
        (
            "úspěšnost pass − block, Newcombe 95 % — dolní mez > 0",
            f"{_pct(win_gap)} [{_pct(win_low)}; {_pct(win_high)}]",
            win_low > 0,
        ),
        (
            "Ø R pass − block, bootstrap po seancích 95 % — dolní mez > 0",
            f"{_r(gap)} [{_r(low)}; {_r(high)}]"
            + (f" ({dropped} losů zahozeno)" if dropped else ""),
            low > 0,
        ),
        (
            f"předpověď: block horší o ≥ {_num(PREDICTED_GAP_R)} R",
            _r(gap),
            gap >= PREDICTED_GAP_R,
        ),
    ]
    if rule == "band_gate_regime":
        stat, p, n = interaction_test(rows, rng)
        checks.append(
            (
                f"interakce poloha × režim, permutace (n = {n}) — p < {_num(ALPHA)}",
                f"SS {stat:.2f}, p = {p:.4f}".replace(".", ","),
                p < ALPHA,
            )
        )
    body = [[name, value, "✔" if ok else "✘"] for name, value, ok in checks]
    return _table(["síto", "hodnota", ""], body), all(ok for _, _, ok in checks)


def breakdown(
    rows: Sequence[Row], rule: str, key: Callable[[Row], str], rng: np.random.Generator
) -> str:
    body: list[list[object]] = []
    for value in sorted({key(row) for row in rows}):
        subset = [row for row in rows if key(row) == value]
        passed, blocked = split(subset, rule)
        if min(len(passed), len(blocked)) >= MIN_INTERVAL_N:
            low, high, _ = bootstrap_gap(passed, blocked, rng)
            interval = f"{_r(mean_gap(passed, blocked))} [{_r(low)}; {_r(high)}]"
        else:
            interval = f"— (n < {MIN_INTERVAL_N})"
        cells = [" / ".join(str(cell) for cell in _summary(group)) for group in (passed, blocked)]
        body.append([value, *cells, interval])
    groups = "n / úspěšnost / Ø R / Ø čisté R / Σ R"
    header = ["", f"pass: {groups}", f"block: {groups}", "Ø R pass − block [95 %]"]
    return _table(header, body)


def report(rows: Sequence[Row], funnel: dict[str, int]) -> str:
    rng = np.random.default_rng(SEED)
    first = min(row.fact.created_ts for row in rows)
    last = max(row.fact.created_ts for row in rows)
    sessions = sorted({row.session for row in rows})
    out = [
        "## Vzorek\n",
        f"Mechanika v{MECHANICS_VERSION}, vznik {first:%Y-%m-%d %H:%M} – "
        f"{last:%Y-%m-%d %H:%M} UTC = {len(sessions)} obchodních seancí "
        f"{sessions[0]} – {sessions[-1]}.\n",
        _table(["krok", "setupů"], [[k, v] for k, v in funnel.items()]),
        "\n## Skupiny pass × block\n",
    ]
    header = ["skupina", "n", "úspěšnost", "Ø R", "Ø čisté R", "Σ R"]
    for rule in RULES:
        passed, blocked = split(rows, rule)
        unknown = len(rows) - len(passed) - len(blocked)
        out.append(f"### `{rule}`" + (f" (unknown vynecháno: {unknown})" if unknown else ""))
        out.append(_table(header, [["pass", *_summary(passed)], ["block", *_summary(blocked)]]))
    out.append("\n## Poloha × gamma režim\n")
    body: list[list[object]] = []
    for band in ("inside", "transition", "outside", "no_zone"):
        for regime in (*REGIMES, None):
            cell = [r for r in rows if r.band_class == band and r.fact.gex_regime == regime]
            if cell:
                body.append([band, regime or "neznámý", *_summary(cell)])
    out.append(_table(["poloha", "režim", "n", "úspěšnost", "Ø R", "Ø čisté R", "Σ R"], body))
    gated = [row for row in rows if row.fact.template != EXEMPT_TEMPLATE]
    for label, subset in (("všechny šablony", rows), (f"bez {EXEMPT_TEMPLATE}", gated)):
        out.append(f"\n## Síta — {label}\n")
        for rule in RULES:
            table, ok = sieves(subset, rule, rng)
            verdict = "všechna síta splněna" if ok else "síta NEsplněna"
            out.append(f"### `{rule}` — {verdict}\n\n{table}")
    for label, key in (
        ("šablona", lambda row: row.fact.template),
        ("symbol", lambda row: row.fact.symbol),
    ):
        out.append(f"\n## Per {label}\n")
        for rule in RULES:
            out.append(f"### `{rule}`\n\n{breakdown(rows, rule, key, rng)}")
    return "\n\n".join(out) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--out", type=Path, required=True, help="výstupní Markdown (UTF-8)")
    args = parser.parse_args()
    rows, funnel = load(_url(args.db))
    if not rows:
        raise SystemExit("Žádný uzavřený setup v5 s bránou — není co vyhodnotit")
    title = f"# Stínová brána podle polohy v pásmu (#1064) — {dt.date.today():%Y-%m-%d}\n\n"
    args.out.write_text(title + report(rows, funnel), encoding="utf-8")


if __name__ == "__main__":
    main()
