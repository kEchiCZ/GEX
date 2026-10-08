"""Verdikt ngram stínu (#1131, E-0.11 epiky #1385; #740 varianta B) — jen čtení PG.

Magnitudová hlava ngram modelu (`news-engine/ngram_job.py`) běží od 26. 8. 2026
ve stínu: klasifikace `source='ngram'`, `strength` = P(|ret_5| > medián).
Předregistrované kritérium (#749, rozhodnutí 12. 9. v #740): na živém subsetu
musí lift horního decilu podle `strength` překročit baseline kategorie
spočtenou out-of-sample, na ≥ 2 tis. živých vzorků; jinak se `importance`
z hlavy nezapíná. Předpověď 12. 9.: hlava baseline neporazí.

Živý subset = definice jobu: lag ingestu ≤ 1 den, klasifikace prospektivní
(`created_at` ≤ `ts_event` + 5 min), okno 5 min nekontaminované (`cont_5`),
reakce ES a NQ (model se učí jen na ES).

Lift = průměr |ret_5| horního decilu podle skóre / celkový průměr. Skóre se
shodnou hodnotou (baseline kategorie: celá kategorie má jedno číslo) se na
hranici decilu berou **očekávanou hodnotou přes náhodné pořadí** — job bere
pořadí řádků z SQL bez ORDER BY, takže jeho baseline je náhodný výběr z
remízy (rozptyl ukazuje tabulka „Remízy").

Baseline (všechny jen ze stejné populace jako trénink modelu: ES, `cont_5`
false, ne `scheduled`):
- `job` — průměry kategorií ze všech řádků do konce dat (jako poslední
  trénink jobu; obsahuje i hodnocené řádky);
- `kategorie WF` — průměry kategorií jen z řádků před UTC dnem události
  (walk-forward, stejná informace jako denně přetrénovaný model);
- `zdroj × hodina WF` — nulový model bez textu: průměr |ret_5| buňky zdroj ×
  hodina UTC (rysy modelu mimo n-gramy), pod `MIN_CELL` řádky hodina, pak celek.

Varianty vzorku:
- `job` — definice jobu beze změny (včetně deferred reakcí);
- `opravený` (primární) — bez deferred reakcí (zprávy při zavřeném trhu sdílí
  výnos uzavírky, víkendový gap; 1 měření, ne stovky) a bez seancí s výpadkem
  (živých řádků ES < `OUTAGE_SHARE` × medián, #1131 bod 3);
- `opravený od 12. 9.` — jen data po předregistraci (nezávislá kontrola);
- `opravený vč. výpadků` — citlivost na vyřazení seancí.

Interval rozdílu lift − baseline: bootstrap po obchodních seancích
(`settle.trading_session_date`, deferred reakce v seanci prvního baru po
uzavírce), `N_BOOT` replik, pevné semínko.

Diagnostika (doplněná po prvním běhu, kritérium nemění): sdružený lift skóre,
které se mění v čase (walk-forward průměry, skladba kategorií dne), míchá
pořadí zpráv uvnitř dne s rozdíly mezi dny (volatilní dny s jinou skladbou
zpráv). Proto lift uvnitř seance (vážený průměr přes seance s ≥ `MIN_STRATUM`
řádky) a Spearmanova korelace skóre × |ret_5| uvnitř vrstev seance a seance ×
hodina UTC — ta odpovídá na otázku, zda text nese informaci o velikosti nad
denní dobou (hodina je rys modelu i nulového modelu).

Průřez `dir × ret` (komentář 7. 10. v #1131, vstup ADR-0053): směr první
pravidlové klasifikace (prospektivní, ≤ 5 min) × znaménko ret_5 na opraveném
vzorku; úspěšnost směru proti driftu (P(nahoru) téhož vzorku). Jen popisně.

Všechny dotazy běží na spojení s `default_transaction_read_only` v transakci
`SET TRANSACTION READ ONLY` (AGENTS.md: data v PG nejdou znovu pořídit).

Spuštění z hostitele (PG publikované na 55432; URL se nikdy nevypisuje):
    uv run --env-file .env python scripts/measure_ngram_shadow_1131.py \\
        --until 2026-10-08T00:00:00+00:00 --out report.md
URL: `--db`, jinak `GEXLENS_HOST_DATABASE_URL`, jinak sestavená z
`GEXLENS_PG_PASSWORD` (uživatel/DB `gexlens`, 127.0.0.1:55432).
"""

import argparse
import datetime as dt
import os
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy import URL, create_engine, text

from gexlens_engine.compute.settle import (
    CME_TZ,
    GLOBEX_OPEN_LOCAL,
    is_trading_session,
    trading_session_date,
)

WINDOW_MIN = 5
TOP_SHARE = 0.10
LIVE_MAX_LAG = dt.timedelta(days=1)
#: Brána #749: minimální živý vzorek
MIN_EVAL = 2000
#: Seance s méně živými řádky ES než tento podíl mediánu = výpadek (PC vypnutý)
OUTAGE_SHARE = 0.5
#: Buňka zdroj × hodina pod tímto počtem řádků → průměr hodiny
MIN_CELL = 30
N_BOOT = 2000
N_TIE_DRAWS = 500
#: Nejmenší vrstva (seance, seance × hodina) pro diagnostiku uvnitř vrstev
MIN_STRATUM = 20
SEED = 1131
SYMBOLS = ("ES", "NQ")
#: Den předregistrace (komentář 12. 9. v #740) — data od něj jsou nezávislá kontrola
PREREG_DAY = dt.date(2026, 9, 12)
DAILY_PAUSE_LOCAL = dt.time(16, 0)


@dataclass(frozen=True)
class EvalRow:
    event_id: int
    symbol: str
    strength: float
    created_at: dt.datetime
    ts_event: dt.datetime
    ts_ingested: dt.datetime
    category: str | None
    source: str
    ret_bp: float
    deferred: bool
    rule_dir: int | None
    rule_created_at: dt.datetime | None

    @property
    def live(self) -> bool:
        window = dt.timedelta(minutes=WINDOW_MIN)
        return (
            self.ts_ingested - self.ts_event <= LIVE_MAX_LAG
            and self.created_at <= self.ts_event + window
        )


@dataclass(frozen=True)
class TrainRow:
    ts_event: dt.datetime
    category: str | None
    source: str
    magnitude: float


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


EVAL_SQL = """
SELECT c.event_id, r.symbol, c.strength, c.created_at, e.ts_event, e.ts_ingested,
       e.category, e.source, r.ret_5 AS ret_bp, COALESCE(r.deferred_min, false) AS deferred,
       rd.direction AS rule_dir, rd.created_at AS rule_created_at
FROM news_classifications c
JOIN news_events e ON e.id = c.event_id
JOIN news_reactions r ON r.event_id = e.id
LEFT JOIN LATERAL (
    SELECT rc.direction, rc.created_at FROM news_classifications rc
    WHERE rc.event_id = e.id AND rc.source = 'rule' ORDER BY rc.version LIMIT 1
) rd ON true
WHERE c.source = 'ngram' AND c.strength IS NOT NULL
  AND r.ret_5 IS NOT NULL AND r.cont_5 IS FALSE
  AND r.symbol IN ('ES', 'NQ') AND e.ts_event < :until
"""

# Stejná populace jako `NgramShadowJob._training_rows`
TRAIN_SQL = """
SELECT e.ts_event, e.category, e.source, abs(r.ret_5) AS magnitude
FROM news_reactions r JOIN news_events e ON e.id = r.event_id
WHERE r.symbol = 'ES' AND r.ret_5 IS NOT NULL AND r.cont_5 IS FALSE
  AND e.kind <> 'scheduled' AND e.ts_event < :until
ORDER BY e.ts_event
"""

HISTORY_SQL = """
SELECT computed_at, symbol, n, lift, baseline_lift, baseline_source, model_n_train
FROM news_ngram_shadow_history
WHERE window_min = 5 AND subset = 'live' AND computed_at < :until_history
ORDER BY computed_at, symbol
"""


def load(
    url: str | URL, until: dt.datetime, until_history: dt.datetime
) -> tuple[list[EvalRow], list[TrainRow], list[Any]]:
    engine = create_engine(url, connect_args={"options": "-c default_transaction_read_only=on"})
    with engine.connect() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        evals = [
            EvalRow(
                event_id=int(row.event_id),
                symbol=row.symbol,
                strength=float(row.strength),
                created_at=row.created_at,
                ts_event=row.ts_event,
                ts_ingested=row.ts_ingested,
                category=row.category,
                source=row.source,
                ret_bp=float(row.ret_bp),
                deferred=bool(row.deferred),
                rule_dir=None if row.rule_dir is None else int(row.rule_dir),
                rule_created_at=row.rule_created_at,
            )
            for row in conn.execute(text(EVAL_SQL), {"until": until})
        ]
        train = [
            TrainRow(row.ts_event, row.category, row.source, float(row.magnitude))
            for row in conn.execute(text(TRAIN_SQL), {"until": until})
        ]
        history = list(conn.execute(text(HISTORY_SQL), {"until_history": until_history}))
        conn.rollback()
    engine.dispose()
    return evals, train, history


# ── Seance a výpadky ───────────────────────────────────────────────


def session_key(row: EvalRow) -> dt.date:
    """Obchodní seance, ve které se reakce změřila.

    Deferred reakce se měří od prvního baru po uzavírce: z denní pauzy
    (16:00–17:00 CT) je to bar seance následujícího dne, z víkendu nebo
    svátku nejbližší obchodní seance.
    """
    day = trading_session_date(row.ts_event)
    if row.deferred:
        local = row.ts_event.astimezone(CME_TZ).time()
        if DAILY_PAUSE_LOCAL <= local < GLOBEX_OPEN_LOCAL:
            day += dt.timedelta(days=1)
    while not is_trading_session(day):
        day += dt.timedelta(days=1)
    return day


def outage_sessions(rows: Sequence[EvalRow]) -> tuple[set[dt.date], dict[dt.date, int], float]:
    counts: dict[dt.date, int] = defaultdict(int)
    for row in rows:
        if row.symbol == "ES" and not row.deferred:
            counts[session_key(row)] += 1
    median = float(np.median(list(counts.values())))
    return {day for day, n in counts.items() if n < OUTAGE_SHARE * median}, counts, median


# ── Lift ───────────────────────────────────────────────────────────


def lift(magnitudes: np.ndarray, scores: np.ndarray) -> float:
    """Lift horního decilu; remíza na hranici decilu = očekávaná hodnota."""
    k = max(1, int(len(magnitudes) * TOP_SHARE))
    order = np.argsort(-scores, kind="stable")
    ranked_scores = scores[order]
    ranked = magnitudes[order]
    boundary = ranked_scores[k - 1]
    above = ranked_scores > boundary
    tied = ranked_scores == boundary
    top_sum = float(ranked[above].sum()) + (k - int(above.sum())) * float(ranked[tied].mean())
    overall = float(magnitudes.mean())
    return (top_sum / k) / overall if overall > 0 else 0.0


def lift_random_ties(
    magnitudes: np.ndarray, scores: np.ndarray, rng: np.random.Generator
) -> np.ndarray:
    """Lift tak, jak ho počítá job (stabilní argsort nad pořadím z SQL) — pro N_TIE_DRAWS pořadí."""
    k = max(1, int(len(magnitudes) * TOP_SHARE))
    overall = float(magnitudes.mean())
    out = np.empty(N_TIE_DRAWS)
    for i in range(N_TIE_DRAWS):
        perm = rng.permutation(len(magnitudes))
        order = np.argsort(-scores[perm], kind="stable")
        out[i] = float(magnitudes[perm][order[:k]].mean()) / overall
    return out


# ── Baseline skóre ─────────────────────────────────────────────────


def job_category_scores(train: Sequence[TrainRow], rows: Sequence[EvalRow]) -> np.ndarray:
    """Průměry kategorií ze všech trénovacích řádků; neznámá = průměr průměrů (jako job)."""
    grouped: dict[str | None, list[float]] = defaultdict(list)
    for row in train:
        grouped[row.category].append(row.magnitude)
    means = {cat: float(np.mean(vals)) for cat, vals in grouped.items()}
    fallback = float(np.mean(list(means.values())))
    return np.array([means.get(row.category, fallback) for row in rows])


def walk_forward_scores(
    train: Sequence[TrainRow], rows: Sequence[EvalRow]
) -> tuple[np.ndarray, np.ndarray]:
    """(kategorie WF, zdroj × hodina WF) — jen z trénovacích řádků před UTC dnem události."""
    by_day: dict[dt.date, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        by_day[row.ts_event.astimezone(dt.UTC).date()].append(index)
    category_scores = np.empty(len(rows))
    cell_scores = np.empty(len(rows))
    cat_sum: dict[str | None, float] = defaultdict(float)
    cat_n: dict[str | None, int] = defaultdict(int)
    cell_sum: dict[tuple[str, int], float] = defaultdict(float)
    cell_n: dict[tuple[str, int], int] = defaultdict(int)
    hour_sum: dict[int, float] = defaultdict(float)
    hour_n: dict[int, int] = defaultdict(int)
    total_sum, total_n = 0.0, 0
    pointer = 0
    for day in sorted(by_day):
        start = dt.datetime.combine(day, dt.time(0, 0), tzinfo=dt.UTC)
        while pointer < len(train) and train[pointer].ts_event < start:
            t = train[pointer]
            hour = t.ts_event.astimezone(dt.UTC).hour
            cat_sum[t.category] += t.magnitude
            cat_n[t.category] += 1
            cell_sum[(t.source, hour)] += t.magnitude
            cell_n[(t.source, hour)] += 1
            hour_sum[hour] += t.magnitude
            hour_n[hour] += 1
            total_sum += t.magnitude
            total_n += 1
            pointer += 1
        if total_n == 0:
            raise SystemExit(f"Před {day} nejsou trénovací řádky — walk-forward baseline nejde")
        overall = total_sum / total_n
        for index in by_day[day]:
            row = rows[index]
            n_cat = cat_n.get(row.category, 0)
            category_scores[index] = cat_sum[row.category] / n_cat if n_cat else overall
            hour = row.ts_event.astimezone(dt.UTC).hour
            cell = (row.source, hour)
            if cell_n.get(cell, 0) >= MIN_CELL:
                cell_scores[index] = cell_sum[cell] / cell_n[cell]
            elif hour_n.get(hour, 0) >= MIN_CELL:
                cell_scores[index] = hour_sum[hour] / hour_n[hour]
            else:
                cell_scores[index] = overall
    return category_scores, cell_scores


# ── Bootstrap po seancích ──────────────────────────────────────────


@dataclass
class Sample:
    """Vzorek jednoho symbolu a varianty s předpočtenými skóre."""

    magnitudes: np.ndarray
    scores: dict[str, np.ndarray]
    sessions: np.ndarray


def bootstrap_diff(
    sample: Sample, baseline: str, rng: np.random.Generator
) -> tuple[float, float, float]:
    """(rozdíl lift modelu − lift baseline, 2,5 %, 97,5 %) — losují se celé seance."""
    point = lift(sample.magnitudes, sample.scores["model"]) - lift(
        sample.magnitudes, sample.scores[baseline]
    )
    days = np.unique(sample.sessions)
    members = [np.flatnonzero(sample.sessions == day) for day in days]
    diffs = np.empty(N_BOOT)
    for i in range(N_BOOT):
        picked = np.concatenate([members[j] for j in rng.integers(0, len(days), len(days))])
        mags = sample.magnitudes[picked]
        diffs[i] = lift(mags, sample.scores["model"][picked]) - lift(
            mags, sample.scores[baseline][picked]
        )
    low, high = np.percentile(diffs, [2.5, 97.5])
    return point, float(low), float(high)


# ── Diagnostika uvnitř vrstev ──────────────────────────────────────


def _ranks(values: np.ndarray) -> np.ndarray:
    """Pořadí; remízy dostanou průměrné pořadí."""
    _, inverse, counts = np.unique(values, return_inverse=True, return_counts=True)
    ranks = np.empty(len(values))
    ranks[np.argsort(values, kind="stable")] = np.arange(len(values))
    return (np.bincount(inverse, weights=ranks) / counts)[inverse]


def spearman(magnitudes: np.ndarray, scores: np.ndarray) -> float | None:
    """Spearmanova korelace; konstantní skóre ve vrstvě = None (nic neřadí)."""
    rank_m, rank_s = _ranks(magnitudes), _ranks(scores)
    if rank_m.std() == 0 or rank_s.std() == 0:
        return None
    return float(np.corrcoef(rank_m, rank_s)[0, 1])


def within_strata(
    sample: Sample,
    strata: Sequence[Any],
    metric: Callable[[np.ndarray, np.ndarray], float | None],
    rng: np.random.Generator,
) -> tuple[int, int, dict[str, tuple[float, float, float]]]:
    """(vrstev, řádků, skóre → vážený průměr metriky [2,5 %; 97,5 %]).

    Vrstva musí ležet v jedné seanci (klíč seanci obsahuje); váha = počet
    řádků vrstvy; bootstrap losuje celé seance i s jejich vrstvami.
    """
    groups: dict[Any, list[int]] = defaultdict(list)
    for index, key in enumerate(strata):
        groups[key].append(index)
    stratum_sessions: list[dt.date] = []
    weights: list[float] = []
    values: dict[str, list[float]] = {name: [] for name in sample.scores}
    for members in groups.values():
        if len(members) < MIN_STRATUM:
            continue
        idx = np.array(members)
        stratum_sessions.append(sample.sessions[idx[0]])
        weights.append(float(len(idx)))
        for name, scores in sample.scores.items():
            value = metric(sample.magnitudes[idx], scores[idx])
            values[name].append(np.nan if value is None else value)
    sessions = np.array(stratum_sessions)
    weight = np.array(weights)
    days = np.unique(sessions)
    members_of = [np.flatnonzero(sessions == day) for day in days]
    picks = [
        np.concatenate([members_of[j] for j in rng.integers(0, len(days), len(days))])
        for _ in range(N_BOOT)
    ]
    out: dict[str, tuple[float, float, float]] = {}
    for name, raw in values.items():
        per_stratum = np.array(raw)
        valid = ~np.isnan(per_stratum)
        point = float(np.average(per_stratum[valid], weights=weight[valid]))
        boots = np.empty(N_BOOT)
        for i, picked in enumerate(picks):
            v, w = per_stratum[picked], weight[picked]
            ok = ~np.isnan(v)
            boots[i] = np.average(v[ok], weights=w[ok])
        low, high = np.percentile(boots, [2.5, 97.5])
        out[name] = (point, float(low), float(high))
    return len(weights), int(weight.sum()), out


# ── Report ─────────────────────────────────────────────────────────


def _f(value: float, digits: int = 3) -> str:
    return f"{value:.{digits}f}".replace(".", ",").replace("-", "−")


def _signed(value: float, digits: int = 3) -> str:
    sign = "+" if value >= 0 else ""
    return sign + _f(value, digits)


def _day(day: dt.date) -> str:
    return f"{day.day}. {day.month}."


BASELINES = (
    ("job", "kategorie (job)"),
    ("category_wf", "kategorie WF"),
    ("cell_wf", "zdroj × hodina WF"),
)


def history_table(history: Sequence[Any]) -> list[str]:
    """Poslední běh jobu za UTC den: n, lift, baseline (ES, NQ)."""
    last: dict[dt.date, dict[str, Any]] = {}
    for row in history:
        last.setdefault(row.computed_at.astimezone(dt.UTC).date(), {})[row.symbol] = row
    lines = [
        "| den (UTC) | ES n | ES lift | ES baseline | NQ lift | NQ baseline | trénink |",
        "|---|---|---|---|---|---|---|",
    ]
    for day in sorted(last):
        es, nq = last[day].get("ES"), last[day].get("NQ")
        if es is None or nq is None:
            continue
        lines.append(
            f"| {_day(day)} | {es.n} | {_f(es.lift)} | {_f(es.baseline_lift)} | "
            f"{_f(nq.lift)} | {_f(nq.baseline_lift)} | {es.model_n_train} |"
        )
    return lines


def variants(
    rows: Sequence[EvalRow], outages: set[dt.date]
) -> dict[str, Callable[[EvalRow], bool]]:
    return {
        "job": lambda row: True,
        "opravený": lambda row: not row.deferred and session_key(row) not in outages,
        "opravený od 12. 9.": lambda row: (
            not row.deferred and session_key(row) not in outages and session_key(row) >= PREREG_DAY
        ),
        "opravený vč. výpadků": lambda row: not row.deferred,
    }


def report(
    evals: Sequence[EvalRow],
    train: Sequence[TrainRow],
    history: Sequence[Any],
    until: dt.datetime,
) -> str:
    rng = np.random.default_rng(SEED)
    live = [row for row in evals if row.live]
    outages, counts, median = outage_sessions(live)
    job_scores = job_category_scores(train, live)
    category_wf, cell_wf = walk_forward_scores(train, live)
    sessions_all = np.array([session_key(row) for row in live])
    out: list[str] = []

    out += [
        "## Vzorek",
        "",
        f"- Klasifikací `ngram` s reakcí (5 min, nekontaminované): {len(evals)};"
        f" živých: {len(live)}"
        f" (ES {sum(r.symbol == 'ES' for r in live)}, NQ {sum(r.symbol == 'NQ' for r in live)}).",
        "- Deferred (trh zavřený, výnos uzavírky): ES"
        f" {sum(r.deferred for r in live if r.symbol == 'ES')}.",
        f"- Seancí s živými řádky ES: {len(counts)}, medián {_f(median, 0)} řádků;"
        f" výpadek (< {_f(OUTAGE_SHARE * 100, 0)} % mediánu): "
        + (", ".join(f"{_day(d)} ({counts[d]})" for d in sorted(outages)) or "žádný")
        + ".",
        f"- Trénovací populace (ES, do {until:%d. %m. %Y %H:%M} UTC): {len(train)} řádků.",
        "",
    ]

    # Remízy v baseline jobu
    out += [
        "## Remízy: baseline jobu závisí na pořadí řádků",
        "",
        "Lift baseline tak, jak ho počítá job (pořadí řádků bez ORDER BY), přes "
        f"{N_TIE_DRAWS} náhodných pořadí, proti očekávané hodnotě (opravený výpočet):",
        "",
        "| symbol | n | lift modelu | baseline: 5 % | medián | 95 % | očekávaná |",
        "|---|---|---|---|---|---|---|",
    ]
    for symbol in SYMBOLS:
        idx = np.array([i for i, r in enumerate(live) if r.symbol == symbol])
        mags = np.abs(np.array([live[i].ret_bp for i in idx]))
        model = np.array([live[i].strength for i in idx])
        draws = lift_random_ties(mags, job_scores[idx], rng)
        p5, p50, p95 = np.percentile(draws, [5, 50, 95])
        out.append(
            f"| {symbol} | {len(idx)} | {_f(lift(mags, model))} | {_f(p5)} | {_f(p50)} | "
            f"{_f(p95)} | {_f(lift(mags, job_scores[idx]))} |"
        )
    es_idx = np.array([i for i, r in enumerate(live) if r.symbol == "ES"])
    k = max(1, int(len(es_idx) * TOP_SHARE))
    filled = 0
    ladder = []
    for score in sorted(set(job_scores[es_idx].tolist()), reverse=True):
        members = [live[i].category for i in es_idx if job_scores[i] == score]
        ladder.append(f"`{members[0]}` {len(members)}")
        filled += len(members)
        if filled >= k:
            break
    out += [
        "",
        f"Horní decil ES = {k} řádků; kategorie v pořadí průměru z tréninku: "
        + ", ".join(ladder)
        + " — hranice decilu padá dovnitř poslední z nich a job z ní bere řádky v pořadí, v jakém"
        " je vrátí PG.",
        "",
    ]

    # Hlavní tabulka
    out += [
        "## Lift modelu proti baseline",
        "",
        "Rozdíl = lift modelu − lift baseline; interval 95 % z bootstrapu po seancích "
        f"({N_BOOT} replik). Brána #749: rozdíl proti baseline kategorie > 0 na n ≥ {MIN_EVAL}.",
        "",
        "| varianta | symbol | n | seancí | Ø \\|ret\\| bp | lift modelu | "
        + " | ".join(f"{label}: lift / rozdíl [95 %]" for _, label in BASELINES)
        + " |",
        "|---|---|---|---|---|---|" + "---|" * len(BASELINES),
    ]
    for name, keep in variants(live, outages).items():
        for symbol in SYMBOLS:
            idx = np.array([i for i, r in enumerate(live) if r.symbol == symbol and keep(r)])
            if len(idx) == 0:
                continue
            sample = Sample(
                magnitudes=np.abs(np.array([live[i].ret_bp for i in idx])),
                scores={
                    "model": np.array([live[i].strength for i in idx]),
                    "job": job_scores[idx],
                    "category_wf": category_wf[idx],
                    "cell_wf": cell_wf[idx],
                },
                sessions=sessions_all[idx],
            )
            cells = []
            for key, _ in BASELINES:
                point, low, high = bootstrap_diff(sample, key, rng)
                cells.append(
                    f"{_f(lift(sample.magnitudes, sample.scores[key]))} / {_signed(point)}"
                    f" [{_signed(low)}; {_signed(high)}]"
                )
            out.append(
                f"| {name} | {symbol} | {len(idx)} | {len(np.unique(sample.sessions))} | "
                f"{_f(float(sample.magnitudes.mean()), 2)} | "
                f"{_f(lift(sample.magnitudes, sample.scores['model']))} | "
                + " | ".join(cells)
                + " |"
            )
    out.append("")

    # Po týdnech (opravený vzorek)
    keep = variants(live, outages)["opravený"]
    out += [
        "## Po týdnech (opravený vzorek)",
        "",
        "| týden od | symbol | n | lift modelu | kategorie WF | zdroj × hodina WF |",
        "|---|---|---|---|---|---|",
    ]
    weeks: dict[tuple[dt.date, str], list[int]] = defaultdict(list)
    for i, row in enumerate(live):
        if keep(row):
            day = session_key(row)
            weeks[(day - dt.timedelta(days=day.weekday()), row.symbol)].append(i)
    for (week, symbol), members in sorted(weeks.items()):
        idx = np.array(members)
        mags = np.abs(np.array([live[i].ret_bp for i in idx]))
        model = np.array([live[i].strength for i in idx])
        out.append(
            f"| {_day(week)} | {symbol} | {len(idx)} | {_f(lift(mags, model))} | "
            f"{_f(lift(mags, category_wf[idx]))} | {_f(lift(mags, cell_wf[idx]))} |"
        )
    out.append("")

    # Diagnostika uvnitř seancí (opravený vzorek)
    labels = {"model": "model", **dict(BASELINES)}
    out += [
        "## Diagnostika uvnitř seancí (opravený vzorek)",
        "",
        "Vážený průměr přes vrstvy s ≥ "
        f"{MIN_STRATUM} řádky; interval 95 % z bootstrapu po seancích."
        " ρ = Spearman skóre × |ret_5|.",
        "",
        "| symbol | metrika | vrstev | řádků | " + " | ".join(labels.values()) + " |",
        "|---|---|---|---|" + "---|" * len(labels),
    ]
    for symbol in SYMBOLS:
        idx = np.array([i for i, r in enumerate(live) if r.symbol == symbol and keep(r)])
        sample = Sample(
            magnitudes=np.abs(np.array([live[i].ret_bp for i in idx])),
            scores={
                "model": np.array([live[i].strength for i in idx]),
                "job": job_scores[idx],
                "category_wf": category_wf[idx],
                "cell_wf": cell_wf[idx],
            },
            sessions=sessions_all[idx],
        )
        hours = [live[i].ts_event.astimezone(dt.UTC).hour for i in idx]
        session_keys = list(sample.sessions)
        checks: list[
            tuple[str, Sequence[Any], Callable[[np.ndarray, np.ndarray], float | None]]
        ] = [
            ("lift uvnitř seance", session_keys, lift),
            ("ρ uvnitř seance", session_keys, spearman),
            ("ρ uvnitř seance × hodina", list(zip(session_keys, hours, strict=True)), spearman),
        ]
        for metric_name, strata, metric in checks:
            n_strata, n_rows, values = within_strata(sample, strata, metric, rng)
            fmt = _f if metric is lift else _signed
            cells = [
                f"{fmt(values[key][0])} [{fmt(values[key][1])}; {fmt(values[key][2])}]"
                for key in labels
            ]
            out.append(
                f"| {symbol} | {metric_name} | {n_strata} | {n_rows} | " + " | ".join(cells) + " |"
            )
    out.append("")

    # dir × ret
    out += [
        "## Průřez `dir × ret` (pravidla, opravený vzorek)",
        "",
        "Směr první pravidlové klasifikace vzniklé ≤ 5 min od události × znaménko ret_5"
        " (ret = 0 vynecháno). Úspěšnost = podíl reakcí ve směru; očekávaná = P(nahoru) celého"
        " vzorku symbolu pro long, 1 − P(nahoru) pro short."
        " Interval rozdílu z bootstrapu po seancích.",
        "",
        "| symbol | směr | n | P(nahoru) | Ø ret bp | úspěšnost | očekávaná | rozdíl [95 %] |",
        "|---|---|---|---|---|---|---|---|",
    ]
    window = dt.timedelta(minutes=WINDOW_MIN)
    for symbol in SYMBOLS:
        rows = [
            r
            for r in live
            if r.symbol == symbol
            and keep(r)
            and r.ret_bp != 0
            and r.rule_dir is not None
            and r.rule_created_at is not None
            and r.rule_created_at <= r.ts_event + window
        ]
        rets = np.array([r.ret_bp for r in rows])
        dirs = np.array([r.rule_dir for r in rows])
        sess = np.array([session_key(r) for r in rows])
        p_up = float((rets > 0).mean())
        for direction in (1, 0, -1):
            mask = dirs == direction
            if not mask.any():
                continue
            share_up = float((rets[mask] > 0).mean())
            label = {1: "long", 0: "0", -1: "short"}[direction]
            line = (
                f"| {symbol} | {label} | {int(mask.sum())} | {_f(share_up)} | "
                f"{_signed(float(rets[mask].mean()), 2)} |"
            )
            if direction == 0:
                out.append(line + " – | – | – |")
                continue
            hit = share_up if direction > 0 else 1 - share_up
            expected = p_up if direction > 0 else 1 - p_up
            days = np.unique(sess)
            members = [np.flatnonzero(sess == d) for d in days]
            diffs = np.empty(N_BOOT)
            for i in range(N_BOOT):
                picked = np.concatenate([members[j] for j in rng.integers(0, len(days), len(days))])
                r_ret, r_dir = rets[picked], dirs[picked]
                up_all = float((r_ret > 0).mean())
                sel = r_dir == direction
                up_dir = float((r_ret[sel] > 0).mean()) if sel.any() else up_all
                diffs[i] = (up_dir - up_all) if direction > 0 else (up_all - up_dir)
            low, high = np.percentile(diffs, [2.5, 97.5])
            out.append(
                line + f" {_f(hit)} | {_f(expected)} | {_signed(hit - expected)}"
                f" [{_signed(float(low))}; {_signed(float(high))}] |"
            )
    out.append("")

    out += ["## Historie jobu (`news_ngram_shadow_history`, subset live, poslední běh dne)", ""]
    out += history_table(history)
    out.append("")
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--db", default=None)
    parser.add_argument(
        "--until",
        type=dt.datetime.fromisoformat,
        required=True,
        help="konec dat (ISO s časovou zónou) — události před tímto okamžikem",
    )
    parser.add_argument("--out", type=Path, required=True, help="výstupní Markdown (UTF-8)")
    args = parser.parse_args()
    if args.until.tzinfo is None:
        raise SystemExit("--until musí mít časovou zónu (např. +00:00)")
    evals, train, history = load(_url(args.db), args.until, args.until)
    title = (
        f"# Ngram stín — měření (#1131)\n\n> data do {args.until:%d. %m. %Y %H:%M} UTC · "
        f"semínko {SEED} · `scripts/measure_ngram_shadow_1131.py`\n\n"
    )
    args.out.write_text(title + report(evals, train, history, args.until), encoding="utf-8")


if __name__ == "__main__":
    main()
