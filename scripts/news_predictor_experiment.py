"""Fáze 1 #740: má empirický prediktor navrch nad pravidly? (offline, jen čte)

Walk-forward nad `news_reactions` × `news_events`: model se vždy učí POUZE na
zprávách starších než testovaný blok, takže čísla odpovídají tomu, co by šlo
čekat v provozu. Bez toho by výsledek nafoukl look-ahead a zjistilo by se to
až po nasazení.

Obě hlavy z #740 najednou:

* **směr** — hit rate + Wilsonova dolní mez (stejná brána jako `news_weights`)
* **velikost** — lift horního decilu: průměrná skutečná |ret_bp| mezi 10 %
  nejvýše ohodnocenými zprávami děleno celkovým průměrem. Hit rate na velikost
  nesedí, proto vlastní metrika.

Baseline je `rule` predictor ze `news_classifications` na TÝCHŽ eventech —
srovnává se tedy na stejném vzorku, ne proti číslu z jiného období.

Verdikt fáze 1: neporazí-li model pravidla, projekt se dál nerozšiřuje (R4 —
nic se nezapíná bez měření).

Spuštění:  python scripts/news_predictor_experiment.py [--symbol ES] [--window 5]
Prostředí: GEXLENS_DATABASE_URL (stejné jako engine).
"""

import argparse
import datetime as dt
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from sqlalchemy import create_engine, text

# Lokálně z repa; v kontejneru je balík nainstalovaný a cesty neexistují
for _sub in ("news-engine", "engine"):
    _path = Path(__file__).resolve().parents[1] / _sub / "src"
    if _path.is_dir():
        sys.path.insert(0, str(_path))

from gexlens_news.ngram_model import (  # noqa: E402
    LogisticModel,
    combine,
    features,
    hash_row,
    wilson_lower_bound,
)

#: Minimum trénovacích vzorků, než se blok vůbec testuje — na pár stech
#: zprávách je model šum a zkreslil by souhrn.
MIN_TRAIN = 2000

#: Velikost testovacího bloku VE VZORCÍCH, ne v kalendáři. Původní měsíční
#: dělení tu selhalo: `news_reactions` sice sahá do 2024, ale to jsou jednotky
#: backfillovaných makro událostí — skutečný news feed sbírá teprve od července
#: 2026, takže by vyšly dva použitelné bloky. Krok po vzorcích drží chronologii
#: (data jsou řazená podle `ts_event`) a dá jich řádově deset.
BLOCK = 500

QUERY = text("""
    select e.id, e.ts_event, e.title, e.source, e.category, r.ret_bp,
           (select c.direction from news_classifications c
             where c.event_id = e.id and c.source = 'rule'
             order by c.version desc limit 1) as rule_direction
    from news_reactions r
    join news_events e on e.id = r.event_id
    where r.symbol = :symbol
      and r.window_min = :window
      and not r.contaminated
      and e.title is not null
      and e.kind <> 'scheduled'
    order by e.ts_event
""")


@dataclass
class Sample:
    ts: dt.datetime
    title: str
    source: str | None
    category: str | None
    ret_bp: float
    rule_direction: int | None


def load(symbol: str, window: int) -> list[Sample]:
    # Pořadí není libovolné: uvnitř kontejneru nese `GEXLENS_DATABASE_URL`
    # hodnotu z `.env` určenou pro hostitele (localhost), kdežto odvozená
    # `GEXLENS_NEWS_DATABASE_URL` míří na službu `postgres`. Vezme se proto
    # nejdřív ta odvozená; lokálně z repa existuje jen ta druhá.
    url = os.environ.get("GEXLENS_NEWS_DATABASE_URL") or os.environ.get("GEXLENS_DATABASE_URL")
    if not url:
        raise SystemExit("Chybí GEXLENS_DATABASE_URL ani GEXLENS_NEWS_DATABASE_URL")
    engine = create_engine(url)
    with engine.connect() as conn:
        rows = conn.execute(QUERY, {"symbol": symbol, "window": window}).fetchall()
    return [
        Sample(
            ts=row.ts_event,
            title=row.title,
            source=row.source,
            category=row.category,
            ret_bp=float(row.ret_bp),
            rule_direction=row.rule_direction,
        )
        for row in rows
    ]


def build_rows(samples: list[Sample]) -> list[np.ndarray]:
    return [
        hash_row(features(s.title, source=s.source, hour_utc=s.ts.astimezone(dt.UTC).hour))
        for s in samples
    ]


def walk_forward_blocks(total: int, min_train: int = MIN_TRAIN) -> list[tuple[int, int]]:
    """Hranice testovacích bloků (index od, index do) po `BLOCK` vzorcích.

    Trénink je vždy vše před `od` — expanding window, takže pozdější bloky se
    učí z víc dat, přesně jako by to dělal provoz.
    """
    return [(start, min(start + BLOCK, total)) for start in range(min_train, total, BLOCK)]


def evaluate(symbol: str, window: int, min_train: int = MIN_TRAIN) -> None:
    samples = load(symbol, window)
    if len(samples) < min_train + BLOCK:
        raise SystemExit(f"Málo dat: {len(samples)} (potřeba {min_train + BLOCK})")
    rows = build_rows(samples)
    returns = np.array([s.ret_bp for s in samples])
    magnitudes = np.abs(returns)

    dir_hits = dir_total = 0
    rule_hits = rule_total = 0
    model_scores: list[float] = []
    model_mags: list[float] = []
    category_scores: list[float] = []
    tested_blocks = 0

    for start, end in walk_forward_blocks(len(samples), min_train):
        train_rows, test_rows = rows[:start], rows[start:end]
        if not test_rows:
            continue
        tested_blocks += 1

        # Práh velké zprávy se počítá JEN z tréninkové části — medián spočtený
        # přes celou historii by do labelů propašoval budoucnost
        median_train = float(np.median(magnitudes[:start]))
        y_dir = (returns[:start] > 0).astype(float)
        y_big = (magnitudes[:start] > median_train).astype(float)

        # Baseline velikosti: průměrné |ret| kategorie spočtené JEN z tréninkové
        # části. Bez ní by se dalo prodat jako úspěch i to, co keyword mapy
        # zvládnou samy — kategorie FED se hýbe dvakrát víc než EARNINGS.
        cat_mean: dict[str, float] = {}
        for category in {s.category for s in samples[:start] if s.category}:
            values = [magnitudes[i] for i in range(start) if samples[i].category == category]
            if values:
                cat_mean[category] = float(np.mean(values))
        global_mean = float(magnitudes[:start].mean())

        dir_model = LogisticModel(seed=0).fit(train_rows, y_dir)
        big_model = LogisticModel(seed=0).fit(train_rows, y_big)
        p_dir = dir_model.predict_proba(test_rows)
        p_big = big_model.predict_proba(test_rows)

        for offset, (pd_, pb) in enumerate(zip(p_dir, p_big, strict=True)):
            i = start + offset
            direction, strength = combine(float(pd_), float(pb))
            actual = 1 if returns[i] > 0 else -1
            # Nulový směr se do hit rate nepočítá — model se vědomě nevyjádřil
            if direction != 0:
                dir_total += 1
                dir_hits += int(direction == actual)
            model_scores.append(strength if direction != 0 else 0.0)
            model_mags.append(magnitudes[i])
            category_scores.append(cat_mean.get(samples[i].category or "", global_mean))
            rule_dir = samples[i].rule_direction
            if rule_dir:
                rule_total += 1
                rule_hits += int(rule_dir == actual)

    scores = np.array(model_scores)
    mags = np.array(model_mags)
    decile = max(1, len(scores) // 10)

    def top_decile_lift(ranking: np.ndarray) -> float:
        idx = np.argsort(ranking)[-decile:]
        return float(mags[idx].mean() / mags.mean()) if mags.mean() else float("nan")

    lift = top_decile_lift(scores)
    lift_category = top_decile_lift(np.array(category_scores))

    # Permutační test: kdyby model neuměl nic, jak často by náhodné pořadí
    # dalo stejně dobrý lift? Bez tohohle by se dal za úspěch vydat i šum.
    rng = np.random.default_rng(0)
    shuffled = scores.copy()
    null_lifts = np.empty(500)
    for i in range(500):
        rng.shuffle(shuffled)
        null_lifts[i] = top_decile_lift(shuffled)
    p_value = float((null_lifts >= lift).mean())

    dir_rate = dir_hits / dir_total if dir_total else float("nan")
    dir_lb = wilson_lower_bound(dir_hits, dir_total)
    rule_rate = rule_hits / rule_total if rule_total else float("nan")
    rule_lb = wilson_lower_bound(rule_hits, rule_total)

    print(f"\n=== {symbol}, okno {window} min — walk-forward, {tested_blocks} bloků po {BLOCK} ===")
    print(
        f"vzorků celkem: {len(samples)}  (testováno {len(scores)}), "
        f"období {samples[0].ts:%Y-%m-%d} – {samples[-1].ts:%Y-%m-%d}"
    )
    print("\nSMĚR (hit rate, Wilsonova dolní mez):")
    print(f"  ngram: {dir_rate:.4f}  LB {dir_lb:.4f}   (n={dir_total})")
    print(f"  rule:  {rule_rate:.4f}  LB {rule_lb:.4f}   (n={rule_total})")
    print("\nVELIKOST (lift horního decilu, 1.0 = žádná informace):")
    print(f"  ngram:    {lift:.3f}×   (průměr |ret| celkem {mags.mean():.2f} bp)")
    print(f"  kategorie:{lift_category:.3f}×  ← baseline: co zvládnou keyword mapy samy")
    print(f"  permutační p-hodnota: {p_value:.4f}  (500 náhodných pořadí)")
    print("\nVERDIKT:")
    print(f"  směr nad náhodou:    {'ANO' if dir_lb > 0.5 else 'NE'} (LB {dir_lb:.4f})")
    print(f"  směr nad pravidly:   {'ANO' if dir_rate > rule_rate else 'NE'}")
    print(f"  velikost nad 1.1×:   {'ANO' if lift > 1.1 else 'NE'} (lift {lift:.3f})")
    print(
        f"  velikost nad kategorií: {'ANO' if lift > lift_category else 'NE'} "
        f"({lift:.3f} vs. {lift_category:.3f})"
    )
    print(f"  velikost není náhoda: {'ANO' if p_value < 0.01 else 'NE'} (p={p_value:.4f})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="ES")
    parser.add_argument("--window", type=int, default=5)
    parser.add_argument(
        "--min-train",
        type=int,
        default=MIN_TRAIN,
        help="Kolik vzorků musí být k dispozici na trénink, než se začne testovat",
    )
    args = parser.parse_args()
    evaluate(args.symbol, args.window, args.min_train)


if __name__ == "__main__":
    main()
