"""Kalibrace vah tendence 14. 9.–16. 10. 2026 jen čtením (E-0.12, #1113, varianta A).

Účel: revize vah indikátoru tendence (`compute/tendency.py`, `WEIGHTS`,
`TENDENCY_WEIGHTS_VERSION = 4`) na RTH řezu po kalibraci 13. 9. (#394).
Bez hlasů `cum_delta_slope` a `divergence` — ty změní CumΔ z tisků (#1460).
Skript nic nezapisuje: PG jen `SELECT` v transakci `READ ONLY`, partice a logy
jen čte, výstup jde do `--out`. Váhy se mění jen po rozhodnutí vlastníka.

Vstupy:
- PG tabulka `tendency` (ts_min, symbol, score, band, votes, weights_version)
  — živé řádky minutového cyklu enginu, ne replay; v okně v3 do 21. 9. 21:03 UTC
  a v4 od 21. 9. 21:04 UTC (#1241);
- bary `{data}/derived/{ES,NQ}/bars/{den}.parquet` (`storage.parquet_store.read_bars`);
- logy enginu `{data}/logs/engine-*.log` — řádky „Cyklus {sym} {ts_min}: … sweep“
  měří, kolik sekund po začátku minuty `ts_min` cyklus doběhl (rozhodnutí).

Konvence času (decision-time; únik +1 minuty z #794 se nesmí opakovat):
- Rozhodnutí tendence padá v minutě `ts_min` (`instruments.run_minute`:
  `now` = začátek minuty, ve které cyklus začal). Pořadí v cyklu: `spot` se čte
  před sweepem (`_current_spot`), pak `runtime.run_cycle` (sweep, řádek logu
  „Cyklus …“ na jeho konci), `_watch_greeks`, `_watch_repair`, `map_state`,
  `setup_engine`, `t9_probes` a teprve pak tendence s týmž `spot`; pipeline
  běží sekvenčně (`gather_metrics`), takže NQ čte spot až po celém cyklu ES.
  Bar nese `ts_min` = začátek intervalu (`ibkr.underlying.Bar`), uzavře se
  příchodem prvního 5s baru další minuty — `close(ts_min)` je cena
  v ts_min + 60 s, `open(ts_min)` cena na začátku minuty.
- Přímá kontrola: spot, ze kterého engine hlasoval, se rekonstruuje z hlasu
  `walls_distance` a zdí v jeho detailu a porovná s rozsahem barů ts, ts+1
  a ts−5 … ts−1 (podíl „jen v baru ts+1“ = rozhodnutí po close(ts)).
- Dopředný výnos `r_h = close(ts_min + h) − close(ts_min)` v bodech a v jednotkách
  σ minuty: `r_h / (σ_1m · √h)`, σ_1m = směrodatná odchylka 1min změn close
  z 60 barů `ts_min − 60 … ts_min − 1` (jen dokončené bary před rozhodnutím).
- Okno musí skončit v RTH: cena `close(ts_min + h)` je v ts_min + h + 1 min,
  proto `ts_min + h + 1 min ≤ settle_ts(den)` (20:00 UTC v létě).
- Citlivost: báze `open(ts_min)` a `close(ts_min + 1)` (přísná — platí, i když
  cyklus přeteče přes konec minuty); korelace hlasu s výnosem uvnitř minuty
  rozhodnutí `close(ts_min) − open(ts_min)` ukazuje, kolik na volbě báze záleží.
- Žádný řádek tendence po ts_min; normalizace a standardizace jen z minulosti
  (σ z předchozích barů) nebo z tréninkových týdnů walk-forwardu. Odečtení
  denního driftu je parciální korelace přes průměr výnosů téže seance známý
  v čase ts (okna skončená do ts); Spearman pořadí a odečtení průměru podle hodiny dne (přes
  seance řezu) jsou popisné diagnostiky, ne prediktor.

RTH a obchodní den z helperů repa: `settle.us_open_ts`, `settle.settle_ts`,
`settle.trading_session_date`, `settle.is_trading_session`, `settle.session_bounds`.

Statistika: CI 95 % bootstrapem po seancích (blokově, `--boot` opakování, pevný
seed) — překrývající se minutové výnosy nedávají naivní n. Převzorkují se jen
seance, ve kterých veličina má data (složka hlasovala, týden má OOS predikci).
Pearson přes součtové statistiky seancí; Spearman = Pearson pořadí z celého
řezu (pořadí se v replikacích nepřepočítává). Percentilové CI s 19 clustery
jsou spíš užší, než by měly být; řezy pod `MIN_SESSIONS_STARS` seancí hvězdičky
nedostávají. Mnohonásobné srovnání: 10 složek × 3 horizonty × 2 symboly; předem
dané hypotézy (`PRESPECIFIED`) se hodnotí zvlášť a sentindex (ES ≈ NQ) jen jednou.

Výstupy: `{out}/tables.md` (markdown, česky) a `{out}/tables.json`.

Spuštění (heslo PG z `.env` jen do proměnné, nevypisuje se):
    uv run python scripts/calib_tendency_1113.py --data <data> --out <adresář>
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import sys
import time
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

from gexlens_engine.compute.paper import tick_of
from gexlens_engine.compute.settle import (
    is_trading_session,
    session_bounds,
    settle_ts,
    trading_session_date,
    us_open_ts,
)
from gexlens_engine.compute.tendency import (
    BAND_WEAK,
    COMPONENT_CAP,
    TENDENCY_WEIGHTS_VERSION,
    WEIGHTS,
    BandHysteresis,
    band_of,
)
from gexlens_engine.storage.parquet_store import read_bars

#: Hlasy, které změní CumΔ z tisků (#1460) — v revizi nehlasují (Rozhodnuto 16. 10. bod 4)
EXCLUDED = ("cum_delta_slope", "divergence")
#: Hodnocené složky — pořadí podle `WEIGHTS`
COMPONENTS = tuple(name for name in WEIGHTS if name not in EXCLUDED)
SYMBOLS = ("ES", "NQ")
HORIZONS = (15, 30, 60)
#: Hlavní horizont srovnání s 13. 9. (tabulky #394 jsou v r30)
MAIN_H = 30
#: Oprava cesty partic SentIndexu (#1136, nasazeno 13. 9. 21:30 UTC)
SENTINDEX_FROM = dt.datetime(2026, 9, 13, 21, 30, tzinfo=dt.UTC)
#: σ minuty z předchozích barů (decision-time)
SIGMA_LOOKBACK = 60
SIGMA_MIN_BARS = 30
#: Ridge nad standardizovanými hlasy: λ = α · n_train
RIDGE_ALPHAS = (0.1, 1.0, 10.0)
#: Mezera v řádcích tendence delší než tohle = nejspíš restart enginu → nová hystereze
HYSTERESIS_RESET_GAP = dt.timedelta(minutes=5)
#: Díra v RTH (engine neběžel) = aspoň tolik chybějících minut za sebou
GAP_MIN_MINUTES = 3
#: Konec obchodování CME equity futures: hodinu před otevřením další seance (16:00 CT)
GLOBEX_HALT = dt.timedelta(hours=1)
BAND_SIGN = {"strong_short": -1, "short": -1, "neutral": 0, "long": 1, "strong_long": 1}
#: Varianty skóre s pevnými vahami v4 (nic se netrénuje): bez CumΔ a navíc bez složek
#: v n-tici — odpovídá `WEIGHTS[složka] = 0` (váha zmizí i ze jmenovatele)
SCORE_VARIANTS: Mapping[str, tuple[str, ...]] = {
    "score_nocd": (),
    "score_nowd": ("walls_distance",),
    "score_nosent": ("sentindex",),
}
SCORE_LABELS = {
    "score_stored": "uložené (s CumΔ)",
    "score_nocd": "bez CumΔ",
    "score_nowd": "bez CumΔ a walls_distance",
    "score_nosent": "bez CumΔ a sentindex",
}
#: Předem dané hypotézy (složka, symbol) — hodnotí se zvlášť, ne v rodině průzkumných
#: testů: walls_distance (#1241, c3425d8 — falešná mean-reversion na trendovém dni),
#: vanna_flow ES/NQ a delta_flow NQ (13. 9., komentář 5655763013: kandidáti na vyřazení,
#: pokud se potvrdí na RTH řezu)
PRESPECIFIED: tuple[tuple[str, str], ...] = (
    ("walls_distance", "ES"),
    ("walls_distance", "NQ"),
    ("vanna_flow", "ES"),
    ("vanna_flow", "NQ"),
    ("delta_flow", "NQ"),
)
#: Pod tolik seancí percentilový bootstrap interval spolehlivě nepokryje → bez hvězdiček
MIN_SESSIONS_STARS = 10
#: Detail hlasu walls_distance se zdmi (striky, celé body) — rekonstrukce spotu
WALLS_RE = re.compile(r"poloha mezi zdmi (\d+)–(\d+)")
#: Zatuchlý spot = leží v některém z barů ts−1 … ts−N, ale ne v baru ts ani ts+1
SPOT_STALE_LOOKBACK = 5
SPOT_STATUS_LABELS = {
    "bar": "v baru ts",
    "next": "jen v ts+1",
    "earlier": "jen v ts−1…ts−5",
    "other": "jinde",
}

#: Kalibrace 13. 9. (#394, issuecomment-5655526545): v3, 7. 8.–11. 9., všechny
#: minuty, Pearson r30 v bodech; CI 90 % z 300 bootstrapů po dnech
REF_0913_R30: Mapping[str, tuple[float, float]] = {
    "flip": (-0.034, 0.039),
    "walls_distance": (-0.003, -0.001),
    "walls_dominance": (0.021, -0.004),
    "max_pain": (0.010, 0.013),
    "centroid": (0.030, 0.019),
    "delta_flow": (0.007, -0.018),
    "sentindex": (0.021, 0.044),
    "gamma_at_price": (0.018, -0.007),
    "charm_flow": (-0.010, 0.012),
    "vanna_flow": (-0.042, -0.026),
}
REF_0913_SCORE_R30 = (-0.010, 0.020)


# ----------------------------------------------------------------- formátování


def fmt_num(value: float | None, digits: int = 3, sign: bool = True) -> str:
    """Číslo česky: desetinná čárka, typografické minus; NaN/None = „—“."""
    if value is None or not math.isfinite(value):
        return "—"
    body = f"{abs(value):.{digits}f}".replace(".", ",")
    if value < 0 and float(body.replace(",", ".")) != 0:
        return "−" + body
    return ("+" if sign else "") + body


def fmt_pct(value: float | None, digits: int = 1) -> str:
    if value is None or not math.isfinite(value):
        return "—"
    return f"{100 * value:.{digits}f}".replace(".", ",") + " %"


def fmt_int(value: float | int | None) -> str:
    if value is None:
        return "—"
    return f"{int(value):,}".replace(",", " ")


def fmt_ci(
    res: Mapping[str, Any] | None, digits: int = 3, scale: float = 1.0, stars: bool = True
) -> str:
    """„r [lo; hi]“, hvězdička = CI mimo nulu; `scale` 100 = procentní body.

    `stars=False` u řezů s málo seancemi (percentilové CI tam nejsou spolehlivé).
    """
    if res is None:
        return "—"
    r, lo, hi = res.get("r"), res.get("lo"), res.get("hi")
    if r is None or not math.isfinite(r):
        return "—"
    star = "*" if stars and res.get("excludes_zero") else ""
    lo_s = fmt_num(lo * scale if lo is not None else None, digits)
    hi_s = fmt_num(hi * scale if hi is not None else None, digits)
    return f"{fmt_num(r * scale, digits)} [{lo_s}; {hi_s}]{star}"


def md_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def day_label(day: dt.date) -> str:
    return f"{day.day}. {day.month}."


def jsonable(value: Any) -> Any:
    """NaN → None, numpy → Python, datum → ISO (tables.json)."""
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [jsonable(v) for v in value]
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, float | np.floating):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, dt.date | dt.datetime):
        return value.isoformat()
    return value


# ----------------------------------------------------------------- statistika


def corr_from_sums(sums: np.ndarray) -> np.ndarray:
    """Pearson z (n, Σx, Σy, Σx², Σy², Σxy) v posledním rozměru."""
    n, sx, sy, sxx, syy, sxy = np.moveaxis(sums, -1, 0)
    cov = n * sxy - sx * sy
    var = (n * sxx - sx**2) * (n * syy - sy**2)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(var > 0, cov / np.sqrt(np.where(var > 0, var, 1.0)), np.nan)


@dataclass
class Boot:
    """Blokový bootstrap po seancích: matice počtů výběru seance (B × S)."""

    sessions: list[dt.date]
    counts: np.ndarray
    seed: int
    key: str
    subsets: dict[tuple[int, ...], np.ndarray] = field(default_factory=dict, repr=False)

    @classmethod
    def make(cls, sessions: Sequence[dt.date], reps: int, seed: int, key: str) -> Boot:
        rng = np.random.default_rng([seed, zlib.crc32(key.encode())])
        size = len(sessions)
        counts = rng.multinomial(size, np.full(size, 1.0 / size), size=reps).astype(float)
        return cls(list(sessions), counts, seed, key)

    def counts_for(self, present: np.ndarray) -> np.ndarray:
        """Matice výběru jen přes seance s daty (`present` = bool délky S).

        Seance bez dat (složka nehlasovala, týden bez OOS predikce) by v replikaci
        přispěla nulou a počet efektivních seancí by kolísal → interval širší.
        """
        if present.all():
            return self.counts
        chosen = tuple(int(i) for i in np.flatnonzero(present))
        if chosen not in self.subsets:
            rng = np.random.default_rng(
                [self.seed, zlib.crc32(self.key.encode()), zlib.crc32(repr(chosen).encode())]
            )
            size = len(chosen)
            sub = rng.multinomial(size, np.full(size, 1.0 / size), size=len(self.counts))
            full = np.zeros_like(self.counts)
            full[:, list(chosen)] = sub
            self.subsets[chosen] = full
        return self.subsets[chosen]


def session_sums(x: np.ndarray, y: np.ndarray, idx: np.ndarray, size: int) -> np.ndarray:
    def acc(weights: np.ndarray | None) -> np.ndarray:
        return np.bincount(idx, weights=weights, minlength=size).astype(float)

    return np.column_stack([acc(None), acc(x), acc(y), acc(x * x), acc(y * y), acc(x * y)])


def ci_result(point: float, reps: np.ndarray) -> dict[str, Any]:
    reps = reps[np.isfinite(reps)]
    if not math.isfinite(point) or len(reps) < 100:
        return {"r": None, "lo": None, "hi": None, "excludes_zero": False}
    lo, hi = (float(v) for v in np.percentile(reps, [2.5, 97.5]))
    return {"r": float(point), "lo": lo, "hi": hi, "excludes_zero": bool(lo > 0 or hi < 0)}


def boot_corr(x: np.ndarray, y: np.ndarray, idx: np.ndarray, boot: Boot) -> dict[str, Any]:
    """Pearson s 95% CI bootstrapem po seancích; NaN v x nebo y se vynechá."""
    mask = np.isfinite(x) & np.isfinite(y)
    n = int(mask.sum())
    out: dict[str, Any] = {"n": n, "sessions": int(len(np.unique(idx[mask]))) if n else 0}
    if n < 30 or out["sessions"] < 3:
        return out | {"r": None, "lo": None, "hi": None, "excludes_zero": False}
    sums = session_sums(x[mask], y[mask], idx[mask], len(boot.sessions))
    counts = boot.counts_for(sums[:, 0] > 0)
    point = float(corr_from_sums(sums.sum(axis=0)))
    return out | ci_result(point, corr_from_sums(counts @ sums))


def boot_corr_diff(
    x_base: np.ndarray, x_alt: np.ndarray, y: np.ndarray, idx: np.ndarray, boot: Boot
) -> dict[str, Any]:
    """Rozdíl r(x_alt, y) − r(x_base, y) na týchž minutách, párový bootstrap po seancích."""
    mask = np.isfinite(x_base) & np.isfinite(x_alt) & np.isfinite(y)
    n = int(mask.sum())
    if n < 30:
        return {"n": n, "r": None, "lo": None, "hi": None, "excludes_zero": False}
    size = len(boot.sessions)
    base = session_sums(x_base[mask], y[mask], idx[mask], size)
    alt = session_sums(x_alt[mask], y[mask], idx[mask], size)
    counts = boot.counts_for(base[:, 0] > 0)
    point = float(corr_from_sums(alt.sum(axis=0)) - corr_from_sums(base.sum(axis=0)))
    reps = corr_from_sums(counts @ alt) - corr_from_sums(counts @ base)
    return {"n": n} | ci_result(point, reps)


def boot_partial_corr(
    x: np.ndarray, y: np.ndarray, z: np.ndarray, idx: np.ndarray, boot: Boot
) -> dict[str, Any]:
    """Parciální Pearson r(x, y | z) s 95% CI bootstrapem po seancích.

    Z poolovaných součtů seancí: r_xy·z = (r_xy − r_xz·r_yz) / √((1 − r_xz²)(1 − r_yz²)).
    Na rozdíl od korelace s y − z neodečítá z s koeficientem 1, takže z, které y
    nepředpovídá, nepřimíchá šum korelovaný s x.
    """
    mask = np.isfinite(x) & np.isfinite(y) & np.isfinite(z)
    n = int(mask.sum())
    out: dict[str, Any] = {"n": n, "sessions": int(len(np.unique(idx[mask]))) if n else 0}
    if n < 30 or out["sessions"] < 3:
        return out | {"r": None, "lo": None, "hi": None, "excludes_zero": False}
    size = len(boot.sessions)
    xy = session_sums(x[mask], y[mask], idx[mask], size)
    xz = session_sums(x[mask], z[mask], idx[mask], size)
    yz = session_sums(y[mask], z[mask], idx[mask], size)
    counts = boot.counts_for(xy[:, 0] > 0)

    def partial(a: np.ndarray, b: np.ndarray, c: np.ndarray) -> np.ndarray:
        r_xy, r_xz, r_yz = corr_from_sums(a), corr_from_sums(b), corr_from_sums(c)
        denom = (1 - r_xz**2) * (1 - r_yz**2)
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(
                denom > 0, (r_xy - r_xz * r_yz) / np.sqrt(np.where(denom > 0, denom, 1.0)), np.nan
            )

    point = float(partial(xy.sum(axis=0), xz.sum(axis=0), yz.sum(axis=0)))
    return out | ci_result(point, partial(counts @ xy, counts @ xz, counts @ yz))


def ranks(values: np.ndarray) -> np.ndarray:
    """Průměrná pořadí, NaN zůstává NaN."""
    return pd.Series(values).rank(method="average").to_numpy(dtype=float)


def boot_spearman(x: np.ndarray, y: np.ndarray, idx: np.ndarray, boot: Boot) -> dict[str, Any]:
    mask = np.isfinite(x) & np.isfinite(y)
    rx = np.full(len(x), np.nan)
    ry = np.full(len(y), np.nan)
    rx[mask] = ranks(x[mask])
    ry[mask] = ranks(y[mask])
    return boot_corr(rx, ry, idx, boot)


def boot_hits(
    direction: np.ndarray, ret: np.ndarray, idx: np.ndarray, boot: Boot
) -> dict[str, Any]:
    """Zásahovost znaménka pásma proti náhodě se stejnými četnostmi.

    Náhoda = P(long)·P(r>0) + P(short)·P(r<0) na týchž minutách (nezávislost
    pásma a výnosu); edge = hit − náhoda, CI bootstrapem po seancích.
    """
    mask = np.isfinite(ret) & (direction != 0) & (ret != 0)
    n = int(mask.sum())
    if n < 30:
        return {"n": n, "hit": None, "chance": None, "edge": None, "lo": None, "hi": None}
    d = direction[mask]
    r = ret[mask]
    size = len(boot.sessions)
    sub = idx[mask]
    cols = [
        np.bincount(sub, minlength=size).astype(float),
        np.bincount(sub, weights=(np.sign(r) == d).astype(float), minlength=size),
        np.bincount(sub, weights=(d > 0).astype(float), minlength=size),
        np.bincount(sub, weights=(r > 0).astype(float), minlength=size),
    ]
    sums = np.column_stack(cols)
    counts = boot.counts_for(sums[:, 0] > 0)

    def stats(total: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        cnt, hits, longs, ups = np.moveaxis(total, -1, 0)
        with np.errstate(divide="ignore", invalid="ignore"):
            hit = hits / cnt
            p_long = longs / cnt
            p_up = ups / cnt
        chance = p_long * p_up + (1 - p_long) * (1 - p_up)
        return hit, chance

    hit, chance = stats(sums.sum(axis=0))
    hit_b, chance_b = stats(counts @ sums)
    edge_b = hit_b - chance_b
    edge_b = edge_b[np.isfinite(edge_b)]
    lo, hi = (float(v) for v in np.percentile(edge_b, [2.5, 97.5]))
    return {
        "n": n,
        "hit": float(hit),
        "chance": float(chance),
        "edge": float(hit - chance),
        "lo": lo,
        "hi": hi,
        "excludes_zero": bool(lo > 0 or hi < 0),
    }


def binom_tail(m: int, k: int, p: float = 0.05) -> float:
    """P(X ≥ k) pro X ~ Bin(m, p) — za předpokladu nezávislých testů."""
    return float(sum(math.comb(m, i) * p**i * (1 - p) ** (m - i) for i in range(k, m + 1)))


# ----------------------------------------------------------------- data


def capped_score(votes: Mapping[str, float], weights: Mapping[str, float]) -> float:
    """Stropovaný vážený průměr hlasů — tatáž normalizace jako `evaluate_tendency`.

    `evaluate_tendency` bere `TendencyInputs`, ne uložené hlasy, proto vzorec
    zde; shodu s enginem ověřuje přepočet uloženého skóre ze všech hlasů
    (kapitola 0 výstupu, max. odchylka).
    """
    if not votes:
        return float("nan")
    total = sum(weights[name] for name in votes)
    score = sum(
        max(-COMPONENT_CAP, min(COMPONENT_CAP, weights[name] * vote / total))
        for name, vote in votes.items()
    )
    return max(-1.0, min(1.0, score))


def reconstruct_spot(vote: Mapping[str, Any] | None) -> float:
    """Spot, ze kterého engine hlasoval — z hlasu walls_distance a zdí v detailu.

    vote = 2·(cw − spot)/(cw − pw) − 1 → spot = cw − (vote + 1)/2 · (cw − pw).
    Zdi jsou striky (celé body, detail `:.0f`), takže rekonstrukce je přesná,
    pokud hlas není oříznutý na ±1 ani pravidlová nula „nehlasuje“ (#1241).
    """
    if vote is None:
        return float("nan")
    match = WALLS_RE.search(vote.get("detail") or "")
    value = float(vote["vote"])
    if match is None or abs(value) >= 0.999:
        return float("nan")
    put_wall, call_wall = float(match.group(1)), float(match.group(2))
    return call_wall - (value + 1.0) / 2.0 * (call_wall - put_wall)


def load_tendency(db_url: str, since: dt.datetime, until: dt.datetime) -> pd.DataFrame:
    """Řádky tendence ES/NQ v okně — jen SELECT v transakci READ ONLY."""
    engine = create_engine(db_url)
    with engine.connect() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        rows = conn.execute(
            text(
                "SELECT ts_min, symbol, score, band, votes, weights_version FROM tendency "
                "WHERE ts_min >= :since AND ts_min < :until AND symbol IN ('ES', 'NQ') "
                "ORDER BY symbol, ts_min"
            ),
            {"since": since, "until": until},
        ).fetchall()
        conn.rollback()
    engine.dispose()
    records: list[dict[str, Any]] = []
    for row in rows:
        votes = row.votes if not isinstance(row.votes, str) else json.loads(row.votes)
        ts = row.ts_min.astimezone(dt.UTC)
        record: dict[str, Any] = {
            "ts": ts,
            "symbol": row.symbol,
            "score": float(row.score),
            "band": row.band,
            "wv": int(row.weights_version),
        }
        stored_weights: dict[str, float] = {}
        all_votes: dict[str, float] = {}
        walls_vote: Mapping[str, Any] | None = None
        for vote in votes:
            name = vote["name"]
            if name == "walls_distance":
                walls_vote = vote
            if name == "sentindex" and ts < SENTINDEX_FROM:
                continue
            all_votes[name] = float(vote["vote"])
            stored_weights[name] = float(vote["weight"])
        for name in WEIGHTS:
            record[f"v_{name}"] = all_votes.get(name, np.nan)
        record["score_check"] = capped_score(all_votes, stored_weights)
        kept = {name: v for name, v in all_votes.items() if name not in EXCLUDED}
        for variant, dropped in SCORE_VARIANTS.items():
            subset = {name: v for name, v in kept.items() if name not in dropped}
            record[variant] = capped_score(subset, WEIGHTS)
        # Pravidlová nula #1241 („… nehlasuje“) — hlas 0 s vahou ve jmenovateli
        record["wd_skip"] = bool(
            walls_vote is not None and "nehlasuje" in (walls_vote.get("detail") or "")
        )
        record["spot_rec"] = reconstruct_spot(walls_vote)
        records.append(record)
    frame = pd.DataFrame.from_records(records)
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
    return frame


def replay_bands(frame: pd.DataFrame, column: str) -> np.ndarray:
    """Pásmo s hysterezí (`BandHysteresis`) nad skóre ve sloupci, jako engine.

    Engine resetuje hysterezi při změně UTC data; restart enginu (mezera
    v řádcích) začíná také od nuly — přibližně jako mezera > 5 min.
    """
    bands: list[str] = []
    hysteresis = BandHysteresis()
    previous: pd.Timestamp | None = None
    for ts, score in zip(frame["ts"], frame[column], strict=True):
        if previous is None or ts.date() != previous.date() or ts - previous > HYSTERESIS_RESET_GAP:
            hysteresis = BandHysteresis()
        previous = ts
        bands.append(hysteresis.update(float(score)) if math.isfinite(score) else "neutral")
    return np.array(bands, dtype=object)


def load_bars(derived: Path, symbol: str, since: dt.datetime, until: dt.datetime) -> pd.DataFrame:
    stored = read_bars(derived, symbol, since, until)
    if not stored:
        raise SystemExit(f"Žádné bary {symbol} v {derived} pro {since:%Y-%m-%d}–{until:%Y-%m-%d}")
    frame = pd.DataFrame(
        {
            "ts": pd.to_datetime([bar.ts for bar in stored], utc=True),
            "open": [bar.open for bar in stored],
            "high": [bar.high for bar in stored],
            "low": [bar.low for bar in stored],
            "close": [bar.close for bar in stored],
            "source": [bar.source or "ibkr" for bar in stored],
        }
    ).set_index("ts")
    return frame


def attach_returns(rows: pd.DataFrame, bars: pd.DataFrame) -> pd.DataFrame:
    """Dopředné výnosy, σ minuty a platnost oken (RTH i celá seance)."""
    rows = rows.copy()
    minute = pd.Timedelta(minutes=1)
    full_index = pd.date_range(bars.index.min(), bars.index.max(), freq="1min")
    close = bars["close"].reindex(full_index)
    open_ = bars["open"].reindex(full_index)
    # σ 1min změn z barů ts−60 … ts−1 (bar ts se uzavírá až po rozhodnutí)
    diffs = close.diff()
    sigma = diffs.rolling(SIGMA_LOOKBACK, min_periods=SIGMA_MIN_BARS).std().shift(1)
    ts = pd.DatetimeIndex(rows["ts"])

    def at(series: pd.Series, offset: int) -> np.ndarray:
        return series.reindex(ts + offset * minute).to_numpy(dtype=float)

    c0, o0, c1 = at(close, 0), at(open_, 0), at(close, 1)
    rows["sigma"] = at(sigma, 0)
    rows["r_in"] = c0 - o0
    rows["bar_source"] = bars["source"].reindex(ts).to_numpy()
    # Spot hlasů (rekonstruovaný z walls_distance) × rozsah barů: v baru ts = rozhodnutí
    # v minutě ts; jen v ts+1 = rozhodnutí po close(ts); jen v ts−1…ts−5 = zatuchlý spot
    high = bars["high"].reindex(full_index)
    low = bars["low"].reindex(full_index)
    spot = rows["spot_rec"].to_numpy(dtype=float)

    def inside(offset: int) -> np.ndarray:
        lo, hi = at(low, offset), at(high, offset)
        with np.errstate(invalid="ignore"):
            return (spot >= lo - 1e-6) & (spot <= hi + 1e-6)

    in_bar, in_next = inside(0), inside(1)
    earlier = np.zeros(len(rows), dtype=bool)
    for offset in range(1, SPOT_STALE_LOOKBACK + 1):
        earlier |= inside(-offset)
    known = np.isfinite(spot) & np.isfinite(at(low, 0))
    status = np.full(len(rows), "", dtype=object)
    status[known] = "other"
    status[known & earlier & ~in_bar & ~in_next] = "earlier"
    status[known & in_next & ~in_bar] = "next"
    status[known & in_bar] = "bar"
    rows["spot_status"] = status
    tick = tick_of(str(rows["symbol"].iloc[0]))
    with np.errstate(invalid="ignore"):
        rows["spot_off_grid"] = known & (np.abs(spot / tick - np.round(spot / tick)) > 0.02)
    sessions = rows["session"].to_numpy()
    settle = np.array([SESSION_TIMES[s][1] for s in sessions], dtype="datetime64[ns]")
    halt = np.array([SESSION_TIMES[s][2] for s in sessions], dtype="datetime64[ns]")
    ts64 = ts.tz_convert(None).to_numpy(dtype="datetime64[ns]")
    for h in HORIZONS:
        ch, ch1 = at(close, h), at(close, h + 1)
        end = ts64 + np.timedelta64(h + 1, "m")
        end_next = end + np.timedelta64(1, "m")
        rth_ok = end <= settle
        rth_next_ok = end_next <= settle
        full_ok = end <= halt
        sig = rows["sigma"].to_numpy() * math.sqrt(h)
        for regime, ok, ok_next in (("rth", rth_ok, rth_next_ok), ("full", full_ok, full_ok)):
            r_close = np.where(ok, ch - c0, np.nan)
            rows[f"r{h}_{regime}"] = r_close
            rows[f"rs{h}_{regime}"] = np.where(sig > 0, r_close / sig, np.nan)
            rows[f"ro{h}_{regime}"] = np.where(ok, ch - o0, np.nan)
            rows[f"rn{h}_{regime}"] = np.where(ok_next, ch1 - c1, np.nan)
    return rows


#: Seance → (US open, settle, konec obchodování Globexu) v naivním UTC; plní main
SESSION_TIMES: dict[dt.date, tuple[np.datetime64, np.datetime64, np.datetime64]] = {}


def session_times(day: dt.date) -> tuple[np.datetime64, np.datetime64, np.datetime64]:
    if day not in SESSION_TIMES:

        def naive(ts: dt.datetime) -> np.datetime64:
            return np.datetime64(ts.astimezone(dt.UTC).replace(tzinfo=None), "ns")

        SESSION_TIMES[day] = (
            naive(us_open_ts(day)),
            naive(settle_ts(day)),
            naive(session_bounds(day)[1] - GLOBEX_HALT),
        )
    return SESSION_TIMES[day]


# ----------------------------------------------------------------- logy cyklu

LOG_RE = re.compile(
    rb"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3}) gexlens_engine\.runtime Cyklus "
    rb"(ES|NQ) (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}):00\+00:00: [^\n]*?sweep ([\d.]+)s",
    re.MULTILINE,
)


def cycle_offsets(logs: Path, start: dt.date, end: dt.date) -> dict[str, Any]:
    """Kolik sekund po začátku minuty `ts_min` doběhl cyklus (RTH, okno)."""
    found: dict[tuple[str, dt.datetime], tuple[float, float]] = {}
    files = sorted(logs.glob("engine-*.log")) if logs.is_dir() else []
    read_files = 0
    floor = dt.datetime.combine(start, dt.time(), dt.UTC).timestamp()
    for path in files:
        if path.stat().st_mtime < floor:
            continue
        read_files += 1
        content = path.read_bytes()
        for match in LOG_RE.finditer(content):
            logged = dt.datetime.fromisoformat(match.group(1).decode()).replace(
                microsecond=int(match.group(2)) * 1000, tzinfo=dt.UTC
            )
            ts_min = dt.datetime.fromisoformat(match.group(4).decode()).replace(tzinfo=dt.UTC)
            symbol = match.group(3).decode()
            session = trading_session_date(ts_min)
            if not (start <= session <= end and is_trading_session(session)):
                continue
            if not us_open_ts(session) <= ts_min < settle_ts(session):
                continue
            offset = (logged - ts_min).total_seconds()
            found.setdefault((symbol, ts_min), (offset, float(match.group(5))))
        del content
    result: dict[str, Any] = {"files": read_files}
    for symbol in SYMBOLS:
        values = np.array([v[0] for k, v in found.items() if k[0] == symbol])
        sweeps = np.array([v[1] for k, v in found.items() if k[0] == symbol])
        sessions = {k[1].date() for k in found if k[0] == symbol}
        if not len(values):
            result[symbol] = {"n": 0}
            continue
        result[symbol] = {
            "n": int(len(values)),
            "sessions": len(sessions),
            "p50": float(np.percentile(values, 50)),
            "p90": float(np.percentile(values, 90)),
            "p99": float(np.percentile(values, 99)),
            "max": float(values.max()),
            "share_over_60": float((values > 60).mean()),
            "share_over_120": float((values > 120).mean()),
            "sweep_p50": float(np.percentile(sweeps, 50)),
            "sweep_p90": float(np.percentile(sweeps, 90)),
        }
    return result


# ----------------------------------------------------------------- řezy


@dataclass
class Slice:
    """Řez dat jednoho symbolu: řádky, seance a bootstrap po seancích."""

    key: str
    label: str
    regime: str  # "rth" | "full"
    frame: pd.DataFrame
    boot: Boot
    idx: np.ndarray = field(init=False)

    def __post_init__(self) -> None:
        position = {day: i for i, day in enumerate(self.boot.sessions)}
        self.idx = np.array([position[s] for s in self.frame["session"]], dtype=np.int64)

    def ret(self, h: int, kind: str = "r") -> np.ndarray:
        return self.frame[f"{kind}{h}_{self.regime}"].to_numpy(dtype=float)

    def vote(self, name: str) -> np.ndarray:
        return self.frame[f"v_{name}"].to_numpy(dtype=float)


def make_slices(rows: pd.DataFrame, symbol: str, reps: int, seed: int) -> dict[str, Slice]:
    in_window = rows["trading"]
    specs = (
        ("v4_rth", "v4 RTH", "rth", in_window & rows["rth"] & (rows["wv"] == 4)),
        ("v3_rth", "v3 RTH", "rth", in_window & rows["rth"] & (rows["wv"] == 3)),
        ("v4_full", "v4 celá seance", "full", in_window & rows["tradable"] & (rows["wv"] == 4)),
    )
    slices: dict[str, Slice] = {}
    for key, label, regime, mask in specs:
        frame = rows[mask].reset_index(drop=True)
        if frame.empty:
            continue
        sessions = sorted(set(frame["session"]))
        span = f"{day_label(sessions[0])}–{day_label(sessions[-1])}"
        boot = Boot.make(sessions, reps, seed, f"{symbol}:{key}")
        slices[key] = Slice(key, f"{label} {span}", regime, frame, boot)
    return slices


def week_of(day: dt.date) -> dt.date:
    """Pátek obchodního týdne (štítek týdne)."""
    return day - dt.timedelta(days=day.weekday()) + dt.timedelta(days=4)


# ----------------------------------------------------------------- analýzy


def regime_weeks(bars: pd.DataFrame, start: dt.date, end: dt.date) -> list[dict[str, Any]]:
    """Režimový průřez okna (#1113 bod 1, metodika 13. 9.): výnos a rozpětí týdne.

    Výnos = close posledního baru před settle pátku proti close před settle předchozí
    obchodní seance; rozpětí = (max high − min low) mezi nimi / referenční close.
    Když bary před oknem chybí, referencí je open první seance týdne (`ref` = open).
    """
    minute = pd.Timedelta(minutes=1)
    days = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
    sessions = [day for day in days if is_trading_session(day)]
    out: list[dict[str, Any]] = []
    for week in sorted({week_of(day) for day in sessions}):
        in_week = [day for day in sessions if week_of(day) == week]
        prev = in_week[0] - dt.timedelta(days=1)
        while not is_trading_session(prev):
            prev -= dt.timedelta(days=1)
        prev_settle = settle_ts(prev)
        settle = settle_ts(in_week[-1])
        part = bars.loc[prev_settle : settle - minute]
        before = bars.loc[: prev_settle - minute, "close"]
        if len(before) and before.index[-1] >= prev_settle - pd.Timedelta(minutes=5):
            ref, ref_kind = float(before.iloc[-1]), "settle"
        else:
            part = bars.loc[session_bounds(in_week[0])[0] : settle - minute]
            ref, ref_kind = float(part["open"].iloc[0]), "open"
        out.append(
            {
                "week": week,
                "sessions": len(in_week),
                "ref": ref_kind,
                "return": float(part["close"].iloc[-1]) / ref - 1,
                "range": float(part["high"].max() - part["low"].min()) / ref,
            }
        )
    return out


def coverage(rows: pd.DataFrame, sl: Slice) -> list[dict[str, Any]]:
    """Kap. 1: seance, minuty tendence v RTH, minuty s výnosem, díry per týden."""
    out: list[dict[str, Any]] = []
    frame = sl.frame
    for week in sorted({week_of(s) for s in sl.boot.sessions}):
        sessions = [s for s in sl.boot.sessions if week_of(s) == week]
        part = frame[frame["session"].map(week_of) == week]
        expected = 0
        missing = 0
        gaps = 0
        for session in sessions:
            open_, settle, halt = SESSION_TIMES[session]
            if sl.regime == "full":
                first = pd.Timestamp(session_bounds(session)[0])
                last = pd.Timestamp(halt, tz="UTC")
            else:
                first = pd.Timestamp(open_, tz="UTC")
                last = pd.Timestamp(settle, tz="UTC")
            grid = pd.date_range(first, last, freq="1min", inclusive="left")
            # Jednotky časů se liší (pandas us × ns) — porovnání v ns
            present = pd.DatetimeIndex(part.loc[part["session"] == session, "ts"])
            flags = ~np.isin(grid.as_unit("ns").asi8, present.as_unit("ns").asi8)
            expected += len(grid)
            missing += int(flags.sum())
            run = 0
            for flag in [*flags, False]:
                if flag:
                    run += 1
                    continue
                if run >= GAP_MIN_MINUTES:
                    gaps += 1
                run = 0
        row = {
            "week": week,
            "sessions": len(sessions),
            "minutes": len(part),
            "expected": expected,
            "missing": missing,
            "gaps": gaps,
        }
        for h in HORIZONS:
            row[f"with_r{h}"] = int(np.isfinite(part[f"r{h}_{sl.regime}"]).sum())
        out.append(row)
    return out


def availability(sl: Slice) -> dict[str, Any]:
    """Kap. 2: podíl minut bez hlasu a s hlasem 0 per složka; rozhodování bez flipu."""
    frame = sl.frame
    n = len(frame)
    comps: dict[str, Any] = {}
    for name in COMPONENTS:
        votes = sl.vote(name)
        present = np.isfinite(votes)
        comps[name] = {
            "missing": float((~present).mean()),
            "zero": float((present & (votes == 0)).mean()),
            "plus": float((present & (votes > 0)).mean()),
            "minus": float((present & (votes < 0)).mean()),
            "mean": float(np.nanmean(votes)) if present.any() else None,
        }
    flip_missing = ~np.isfinite(sl.vote("flip"))
    stored_dir = frame["band"].map(BAND_SIGN).to_numpy() != 0
    nocd_dir = np.abs(frame["score_nocd"].to_numpy()) >= BAND_WEAK
    per_week = {}
    for week in sorted({week_of(s) for s in sl.boot.sessions}):
        mask = frame["session"].map(week_of).to_numpy() == week
        per_week[week.isoformat()] = float(flip_missing[mask].mean())
    return {
        "n": n,
        "components": comps,
        "flip_missing": float(flip_missing.mean()),
        "flip_missing_per_week": per_week,
        "dir_stored_share": float(stored_dir.mean()),
        "flip_missing_among_stored_dir": float(flip_missing[stored_dir].mean())
        if stored_dir.any()
        else None,
        "stored_dir_among_flip_missing": float(stored_dir[flip_missing].mean())
        if flip_missing.any()
        else None,
        "flip_missing_among_nocd_dir": float(flip_missing[nocd_dir].mean())
        if nocd_dir.any()
        else None,
        "votes_count_mean": float(
            np.isfinite(frame[[f"v_{c}" for c in COMPONENTS]].to_numpy()).sum(axis=1).mean()
        ),
        "walls_skip_share": float(frame["wd_skip"].mean()),
    }


def univariate(sl: Slice, kinds: Sequence[str] = ("r",)) -> dict[str, Any]:
    """Kap. 4: Pearson a Spearman hlas × r_h s CI po seancích."""
    out: dict[str, Any] = {}
    for name in COMPONENTS:
        x = sl.vote(name)
        out[name] = {}
        for h in HORIZONS:
            cell: dict[str, Any] = {}
            for kind in kinds:
                y = sl.ret(h, kind)
                cell[f"pearson_{kind}"] = boot_corr(x, y, sl.idx, sl.boot)
                if kind == "r":
                    cell["spearman_r"] = boot_spearman(x, y, sl.idx, sl.boot)
            out[name][h] = cell
    return out


#: Pásma kap. 5: klíč → (sloupec pásma nebo skóre pro `band_of`, popis)
BAND_VARIANTS: Mapping[str, tuple[str, str]] = {
    "band_stored": ("band", "uložené (hystereze, s CumΔ)"),
    "band_score_nocd": ("band_score_nocd", "bez CumΔ, hystereze"),
    "band_nocd_raw": ("score_nocd", "bez CumΔ, bez hystereze"),
    "band_score_nowd": ("band_score_nowd", "bez CumΔ a walls_distance, hystereze"),
    "band_score_nosent": ("band_score_nosent", "bez CumΔ a sentindex, hystereze"),
}


def score_analysis(sl: Slice) -> dict[str, Any]:
    """Kap. 5: skóre (varianty s pevnými vahami v4) × r_h a zásahovost pásem proti náhodě."""
    frame = sl.frame
    out: dict[str, Any] = {"corr": {}, "diff": {}, "hits": {}, "band_means": {}}
    scores = {"score_stored": frame["score"].to_numpy(dtype=float)} | {
        name: frame[name].to_numpy(dtype=float) for name in SCORE_VARIANTS
    }
    bands: dict[str, np.ndarray] = {}
    for key, (column, _) in BAND_VARIANTS.items():
        if column.startswith("score"):
            bands[key] = np.array(
                [band_of(s) if math.isfinite(s) else "neutral" for s in scores[column]],
                dtype=object,
            )
        else:
            bands[key] = frame[column].to_numpy()
    for name, values in scores.items():
        out["corr"][name] = {
            h: {
                "pearson": boot_corr(values, sl.ret(h), sl.idx, sl.boot),
                "spearman": boot_spearman(values, sl.ret(h), sl.idx, sl.boot),
                "pearson_sigma": boot_corr(values, sl.ret(h, "rs"), sl.idx, sl.boot),
            }
            for h in HORIZONS
        }
    # Ztlumení složky proti skóre bez CumΔ: rozdíl korelací na týchž minutách
    for name in SCORE_VARIANTS:
        if name == "score_nocd":
            continue
        out["diff"][name] = {
            h: boot_corr_diff(scores["score_nocd"], scores[name], sl.ret(h), sl.idx, sl.boot)
            for h in HORIZONS
        }
    for name, band in bands.items():
        direction = np.array([BAND_SIGN[b] for b in band], dtype=float)
        out["hits"][name] = {h: boot_hits(direction, sl.ret(h), sl.idx, sl.boot) for h in HORIZONS}
        if name not in ("band_stored", "band_score_nocd"):
            continue
        means: dict[str, Any] = {}
        for label in BAND_SIGN:
            mask = band == label
            y = sl.ret(MAIN_H)[mask]
            y = y[np.isfinite(y)]
            means[label] = {
                "n": int(len(y)),
                "mean_r30": float(y.mean()) if len(y) else None,
                "share_up": float((y > 0).mean()) if len(y) else None,
            }
        out["band_means"][name] = means
    return out


def safe_corr(x: np.ndarray, y: np.ndarray) -> float:
    """Pearson bez CI (per fold, per týden); NaN nebo konstanta = NaN."""
    mask = np.isfinite(x) & np.isfinite(y)
    if mask.sum() < 30 or np.std(x[mask]) == 0 or np.std(y[mask]) == 0:
        return float("nan")
    return float(np.corrcoef(x[mask], y[mask])[0, 1])


def fit_ols(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    design = np.column_stack([np.ones(len(x)), x])
    coef, *_ = np.linalg.lstsq(design, y, rcond=None)
    return coef


def ridge_predict(
    x_train: np.ndarray, y_train: np.ndarray, x_test: np.ndarray, alpha: float
) -> np.ndarray:
    """Ridge nad hlasy standardizovanými statistikami TRÉNINKU (žádný únik testu).

    Vrací jen sklonovou část predikce (bez Ø výnosu tréninku).
    """
    mu = x_train.mean(axis=0)
    sd = x_train.std(axis=0)
    sd = np.where(sd > 0, sd, np.inf)
    z_train = (x_train - mu) / sd
    z_test = (x_test - mu) / sd
    lam = alpha * len(x_train)
    gram = z_train.T @ z_train + lam * np.eye(z_train.shape[1])
    beta = np.linalg.solve(gram, z_train.T @ (y_train - y_train.mean()))
    # Bez interceptu (Ø y tréninku) — viz walk_forward
    return z_test @ beta


#: Modely walk-forwardu; naivní = varianty skóre s pevnými vahami v4 (bez tréninku)
NAIVE_MODELS = {f"naive_{name}": name for name in SCORE_VARIANTS}
WF_MODELS = ["ols", *[f"ridge_{a:g}" for a in RIDGE_ALPHAS], *NAIVE_MODELS]
WF_LABELS = {
    "ols": "OLS",
    **{f"ridge_{a:g}": f"ridge α={a:g}".replace(".", ",") for a in RIDGE_ALPHAS},
    "naive_score_nocd": "naivní v4",
    "naive_score_nowd": "naivní −wd",
    "naive_score_nosent": "naivní −sent",
}


def walk_forward(sl: Slice) -> dict[str, Any]:
    """Kap. 6: trénink na předchozích týdnech, test na dalším; OOS korelace.

    Naivní varianty skóre se netrénují — jejich „pooled OOS“ je táž korelace
    přes testovací týdny (bez prvního), srovnatelná s OLS/ridge.
    """
    frame = sl.frame
    weeks = sorted({week_of(s) for s in sl.boot.sessions})
    week_arr = frame["session"].map(week_of).to_numpy()
    x_all = np.nan_to_num(frame[[f"v_{c}" for c in COMPONENTS]].to_numpy(dtype=float), nan=0.0)
    naive = {model: frame[score].to_numpy(dtype=float) for model, score in NAIVE_MODELS.items()}
    out: dict[str, Any] = {"weeks": weeks, "targets": {}}
    models = WF_MODELS
    # Znaménka vah na disjunktních týdnech (fit zvlášť pro každý týden): tréninkové
    # množiny walk-forwardu jsou vnořené, shoda znamének přes foldy není nezávislá
    y_main = sl.ret(MAIN_H)
    week_coef: dict[str, dict[str, float]] = {}
    for week in weeks:
        mask = (week_arr == week) & np.isfinite(y_main)
        if mask.sum() < 200:
            continue
        coef = fit_ols(x_all[mask], y_main[mask])
        week_coef[week.isoformat()] = dict(zip(COMPONENTS, coef[1:].tolist(), strict=True))
    out["week_coef"] = week_coef
    for kind in ("r", "rs"):
        per_h: dict[int, Any] = {}
        for h in HORIZONS:
            y_all = sl.ret(h, kind)
            preds = {m: np.full(len(frame), np.nan) for m in models}
            folds = []
            for k in range(1, len(weeks)):
                train = np.isin(week_arr, weeks[:k]) & np.isfinite(y_all)
                test = (week_arr == weeks[k]) & np.isfinite(y_all)
                if train.sum() < 200 or test.sum() < 50:
                    continue
                coef = fit_ols(x_all[train], y_all[train])
                # Predikce bez interceptu: intercept = Ø výnos tréninkových týdnů by
                # v pooled korelaci přes foldy míchal drift týdnů do „signálu“ hlasů
                # (korelace uvnitř foldu se konstantou nemění)
                fold_pred = {"ols": x_all[test] @ coef[1:]} | {
                    model: values[test] for model, values in naive.items()
                }
                for alpha in RIDGE_ALPHAS:
                    fold_pred[f"ridge_{alpha:g}"] = ridge_predict(
                        x_all[train], y_all[train], x_all[test], alpha
                    )
                fold: dict[str, Any] = {
                    "train_weeks": [w.isoformat() for w in weeks[:k]],
                    "test_week": weeks[k].isoformat(),
                    "n_train": int(train.sum()),
                    "n_test": int(test.sum()),
                    "ols_coef": dict(zip(COMPONENTS, coef[1:].tolist(), strict=True)),
                    "r": {},
                }
                for model, values in fold_pred.items():
                    preds[model][test] = values
                    fold["r"][model] = safe_corr(values, y_all[test])
                folds.append(fold)
            # boot_corr převzorkuje jen testovací seance (první týden predikci nemá)
            pooled = {m: boot_corr(preds[m], y_all, sl.idx, sl.boot) for m in models}
            per_h[h] = {"folds": folds, "pooled": pooled}
        out["targets"][kind] = per_h
    return out


def known_day_mean(frame: pd.DataFrame, y: np.ndarray, h: int) -> np.ndarray:
    """Průměr r_h téže seance známý v čase ts (decision-time).

    Výnos minuty ts' je známý, když skončilo jeho okno: close(ts' + h) je cena
    v ts' + h + 1 min ≤ ts. Minuty bez známého výnosu (začátek RTH) = NaN.
    """
    out = np.full(len(frame), np.nan)
    ts_ns = pd.DatetimeIndex(frame["ts"]).as_unit("ns").asi8
    lag = (h + 1) * 60 * 1_000_000_000
    sessions = frame["session"].to_numpy()
    for session in pd.unique(sessions):
        where = np.flatnonzero(sessions == session)
        order = where[np.argsort(ts_ns[where], kind="stable")]
        t = ts_ns[order]
        values = y[order]
        valid = np.isfinite(values)
        if not valid.any():
            continue
        known_at = t[valid] + lag
        cumulative = np.cumsum(values[valid])
        count = np.searchsorted(known_at, t, side="right")
        with np.errstate(invalid="ignore", divide="ignore"):
            means = np.where(count > 0, cumulative[np.maximum(count - 1, 0)] / count, np.nan)
        out[order] = means
    return out


def robustness(sl: Slice) -> dict[str, Any]:
    """Kap. 7: r30 hlasů a skóre pod kontrolami konfundérů a vstupních vad.

    - jen hlas ≠ 0: bez pravidlových nul (walls_distance „nehlasuje“, charm před
      rampou, vanna v deadbandu, sentindex bez směru);
    - jen živý bar ts (`bar_source == ibkr`): bez minut, kdy engine neměřil;
    - bez minut se spotem mimo bar ts (rekonstrukce z walls_distance): bez
      rozhodnutí po close(ts) i se zatuchlým spotem;
    - | známý den: parciální r(hlas, r30 | průměr r30 téže seance známý v ts);
      odečet „r30 − průměr“ by průměr bral s koeficientem 1, ale průměr r30
      nepředpovídá (sklon ≈ 0), takže by jen přimíchal šum korelovaný s hlasem;
    - − průměr hodiny: výnos minus průměr r30 podle hodiny UTC přes seance řezu
      (popisná — používá i pozdější seance; čistí efekt denní doby, např. rampu
      charm_flow 16:00–19:00 UTC).
    """
    frame = sl.frame
    y = sl.ret(MAIN_H)
    day_known = known_day_mean(frame, y, MAIN_H)
    hours = pd.DatetimeIndex(frame["ts"]).hour.to_numpy()
    hour_mean = pd.Series(y).groupby(hours).transform("mean").to_numpy()
    y_hour = y - hour_mean
    live = (frame["bar_source"] == "ibkr").to_numpy()
    spot_ok = frame["spot_status"].isin(["", "bar"]).to_numpy()
    weeks = frame["session"].map(week_of).to_numpy()
    out: dict[str, Any] = {}
    for name in [*COMPONENTS, *SCORE_VARIANTS]:
        is_vote = name in COMPONENTS
        x = sl.vote(name) if is_vote else frame[name].to_numpy(dtype=float)
        entry: dict[str, Any] = {
            "raw": boot_corr(x, y, sl.idx, sl.boot),
            "nonzero": boot_corr(np.where(x != 0, x, np.nan), y, sl.idx, sl.boot)
            if is_vote
            else None,
            "live": boot_corr(np.where(live, x, np.nan), y, sl.idx, sl.boot),
            "spot_ok": boot_corr(np.where(spot_ok, x, np.nan), y, sl.idx, sl.boot),
            "partial_known_day": boot_partial_corr(x, y, day_known, sl.idx, sl.boot),
            "minus_hour": boot_corr(x, y_hour, sl.idx, sl.boot),
            "per_week": {
                week.isoformat(): safe_corr(x[weeks == week], y[weeks == week])
                for week in sorted(set(weeks))
            },
        }
        out[name] = entry
    return out


def sensitivity(sl: Slice) -> dict[str, Any]:
    """Kap. 3: r30 podle báze výnosu a korelace s výnosem uvnitř minuty rozhodnutí."""
    out: dict[str, Any] = {}
    r_in = sl.frame["r_in"].to_numpy(dtype=float)
    inside = np.isfinite(sl.ret(MAIN_H))
    for name in [*COMPONENTS, "score_nocd"]:
        x = sl.frame["score_nocd"].to_numpy(dtype=float) if name == "score_nocd" else sl.vote(name)
        out[name] = {
            "close": boot_corr(x, sl.ret(MAIN_H, "r"), sl.idx, sl.boot),
            "open": boot_corr(x, sl.ret(MAIN_H, "ro"), sl.idx, sl.boot),
            "next_close": boot_corr(x, sl.ret(MAIN_H, "rn"), sl.idx, sl.boot),
            "intraminute": boot_corr(x, np.where(inside, r_in, np.nan), sl.idx, sl.boot),
        }
    return out


# ----------------------------------------------------------------- markdown


def slice_stars(results: dict[str, Any], key: str) -> bool:
    """Hvězdičky jen u řezů s dost seancemi (percentilové CI s málo clustery nepokrývá)."""
    sessions = results[SYMBOLS[0]]["slices"].get(key, {}).get("sessions", [])
    return len(sessions) >= MIN_SESSIONS_STARS


def sign_count(values: Sequence[float]) -> str:
    plus = sum(1 for v in values if v > 0)
    minus = sum(1 for v in values if v < 0)
    return f"{plus}× + / {minus}× −"


def render(results: dict[str, Any]) -> str:
    parts: list[str] = []
    meta = results["meta"]
    add = parts.append
    add("# Kalibrace vah tendence 14. 9.–16. 10. 2026 (E-0.12, #1113) — jen čtení")
    add("")
    add(
        f"Vygenerováno {meta['generated']} UTC, běh {meta['runtime_s']:.0f} s. Okno seancí "
        f"{meta['start']} – {meta['end']}; bootstrap {meta['boot']}× po seancích (převzorkují "
        f"se jen seance, kde veličina má data), seed {meta['seed']}; CI 95 % (percentilové); "
        f"`*` = CI mimo nulu. Percentilové CI s 19 clustery (seancemi) jsou spíš užší, než by "
        f"měly být; řezy pod {MIN_SESSIONS_STARS} seancí (v3 RTH, 6 seancí) hvězdičky "
        f"nedostávají — jejich CI jsou jen orientační. Hodnocené složky: "
        f"{', '.join(COMPONENTS)} (bez {', '.join(EXCLUDED)}). Váhy v4 = `WEIGHTS` "
        f"z `compute/tendency.py` (verze {TENDENCY_WEIGHTS_VERSION})."
    )
    add("")
    add("## 0. Kontroly vstupu")
    add("")
    rows = []
    for symbol in SYMBOLS:
        check = results[symbol]["checks"]
        rows.append(
            [
                symbol,
                fmt_int(check["rows"]),
                ", ".join(f"v{k}: {fmt_int(v)}" for k, v in check["by_version"].items()),
                f"{check['score_check_max_abs']:.1e}",
                fmt_pct(check["band_replay_match"]),
                fmt_pct(check["bars_missing_share"]),
                fmt_pct(check["bars_nonlive_share"]),
            ]
        )
    add(
        md_table(
            [
                "sym",
                "řádků tendence v okně",
                "verze vah",
                "max odchylka skóre přepočtené × uložené",
                "shoda replay hystereze s uloženým pásmem",
                "RTH minut bez baru",
                "RTH minut s doplněným barem (z minut s barem)",
            ],
            rows,
        )
    )
    add("")
    add(
        "Přepočet uloženého skóre ze všech uložených hlasů tímtéž vzorcem (stropovaný vážený "
        "průměr) ověřuje, že skóre bez CumΔ v kap. 5 počítá stejně jako engine. Replay "
        "hystereze (`BandHysteresis`, reset při změně UTC data a mezeře > 5 min) nad uloženým "
        "skóre ověřuje pásmo bez CumΔ s hysterezí. Doplněný bar (`ibkr_hist`, "
        "`tasty_candle`) = engine tu minutu neměřil; spot hlasů pak mohl být zastaralý."
    )
    add("")
    add(
        "**Spot hlasů × bary (RTH).** Spot, ze kterého engine hlasoval, se rekonstruuje "
        "z hlasu walls_distance a zdí v jeho detailu (spot = cw − (hlas + 1)/2 · (cw − pw); "
        "zdi jsou striky, rekonstrukce je přesná, pokud hlas není oříznutý na ±1 ani "
        "pravidlová nula). „v baru ts“ = low ≤ spot ≤ high baru ts (rozhodnutí v minutě ts); "
        "„jen v ts+1“ = rozhodnutí po close(ts); „jen v ts−1…ts−5“ = zatuchlý spot; "
        "„mimo mřížku“ = spot není násobek ticku (typicky mid, ne `ticker.last`)."
    )
    add("")
    rows = []
    for symbol in SYMBOLS:
        sp = results[symbol]["checks"]["spot"]["v4"]
        rows.append(
            [
                symbol,
                f"{fmt_int(sp['n'])} z {fmt_int(sp['minutes'])}",
                *[fmt_pct(sp[key]) for key in SPOT_STATUS_LABELS],
                fmt_pct(sp["off_grid"]),
                fmt_pct(sp["outside_live"]),
            ]
        )
    add("v4 RTH celkem:")
    add("")
    add(
        md_table(
            [
                "sym",
                "rekonstruováno minut",
                *SPOT_STATUS_LABELS.values(),
                "mimo tick mřížku",
                "mimo bar ts při živém baru",
            ],
            rows,
        )
    )
    add("")
    by_day = {symbol: results[symbol]["checks"]["spot"]["by_day"] for symbol in SYMBOLS}
    days = sorted({row["session"] for symbol in SYMBOLS for row in by_day[symbol]})
    rows = []
    for day in days:
        line: list[str] = [day_label(day)]
        versions = set()
        for symbol in SYMBOLS:
            cell = next((r for r in by_day[symbol] if r["session"] == day), None)
            if cell is None or not cell["n"]:
                line += ["—"] * 5
                continue
            versions.add(f"v{cell['wv']}")
            line += [
                fmt_int(cell["n"]),
                fmt_pct(cell["bar"], 0),
                fmt_pct(cell["next"], 0),
                fmt_pct(cell["earlier"], 0),
                fmt_pct(cell["off_grid"], 0),
            ]
        line.insert(1, "/".join(sorted(versions)) or "—")
        rows.append(line)
    add("Po seancích (RTH):")
    add("")
    add(
        md_table(
            [
                "seance",
                "verze",
                *[
                    f"{symbol} {label}"
                    for symbol in SYMBOLS
                    for label in ("n", "v baru ts", "jen ts+1", "ts−1…ts−5", "mimo mřížku")
                ],
            ],
            rows,
        )
    )
    add("")

    # 1. pokrytí
    add("## 1. Pokrytí")
    add("")
    add(
        "Týden = obchodní týden označený pátkem. Očekávané minuty = RTH 13:30–20:00 UTC "
        "(`us_open_ts`–`settle_ts`) resp. celá seance 17:00–16:00 CT. Díra = aspoň "
        f"{GAP_MIN_MINUTES} chybějící minuty tendence za sebou (engine neběžel)."
    )
    add("")
    for key in ("v4_rth", "v3_rth", "v4_full"):
        rows = []
        for symbol in SYMBOLS:
            cov = results[symbol]["slices"].get(key, {}).get("coverage", [])
            for row in cov:
                rows.append(
                    [
                        symbol,
                        day_label(row["week"]),
                        row["sessions"],
                        fmt_int(row["minutes"]),
                        fmt_int(row["expected"]),
                        f"{fmt_int(row['missing'])} ({fmt_pct(row['missing'] / row['expected'])})"
                        if row["expected"]
                        else "—",
                        row["gaps"],
                        *[fmt_int(row[f"with_r{h}"]) for h in HORIZONS],
                    ]
                )
        label = results[SYMBOLS[0]]["slices"].get(key, {}).get("label", key)
        add(f"**{label}**")
        add("")
        add(
            md_table(
                [
                    "sym",
                    "týden",
                    "seancí",
                    "minut tendence",
                    "očekáváno",
                    "chybí",
                    "děr",
                    *[f"s r{h}" for h in HORIZONS],
                ],
                rows,
            )
        )
        add("")
    add("**Režim týdnů** (z barů, metodika 13. 9.)")
    add("")
    add(
        "Výnos = close před settle pátku proti close před settle předchozí obchodní seance; "
        "rozpětí = (max high − min low) v týdnu / referenční close. „od open“ = bary před "
        "oknem chybí, referencí je open první seance týdne."
    )
    add("")
    regime_rows = [
        [
            day_label(row["week"]),
            row["sessions"],
            *[
                f"{fmt_num(100 * item['return'], 2)} % / {fmt_pct(item['range'], 2)}"
                + (" (od open)" if item["ref"] == "open" else "")
                for item in (
                    next(r for r in results[symbol]["regime"] if r["week"] == row["week"])
                    for symbol in SYMBOLS
                )
            ],
        ]
        for row in results[SYMBOLS[0]]["regime"]
    ]
    add(
        md_table(
            ["týden", "seancí", *[f"{symbol} výnos / rozpětí" for symbol in SYMBOLS]],
            regime_rows,
        )
    )
    add("")

    # 2. dostupnost
    add("## 2. Dostupnost hlasů")
    add("")
    add(
        "Podíl minut, kde složka nehlasuje (chybí v rozpadu) a kde hlasuje 0. „+/−“ = podíl "
        "kladných/záporných hlasů, Ø = průměr přítomných hlasů. Hlas 0 zahrnuje i pravidlové "
        "nuly s vahou ve jmenovateli: walls_distance „… nehlasuje“ (#1241, slabá nebo "
        "pohyblivá zeď), charm_flow před rampou (do 16:00 UTC), vanna_flow v deadbandu IV, "
        "sentindex bez shody znaménka a směru."
    )
    add("")
    for symbol in SYMBOLS:
        rows = []
        av_main = results[symbol]["slices"]["v4_rth"]["availability"]
        av_v3 = results[symbol]["slices"].get("v3_rth", {}).get("availability")
        av_full = results[symbol]["slices"].get("v4_full", {}).get("availability")
        for name in COMPONENTS:
            c = av_main["components"][name]
            rows.append(
                [
                    name,
                    fmt_pct(c["missing"]),
                    fmt_pct(c["zero"]),
                    f"{fmt_pct(c['plus'], 0)} / {fmt_pct(c['minus'], 0)}",
                    fmt_num(c["mean"], 2),
                    fmt_pct(av_v3["components"][name]["missing"]) if av_v3 else "—",
                    fmt_pct(av_full["components"][name]["missing"]) if av_full else "—",
                ]
            )
        add(f"**{symbol}** (v4 RTH, n = {fmt_int(av_main['n'])} minut)")
        add("")
        add(
            md_table(
                [
                    "složka",
                    "chybí",
                    "hlas 0",
                    "+ / −",
                    "Ø hlas",
                    "chybí v3 RTH",
                    "chybí v4 celá seance",
                ],
                rows,
            )
        )
        add("")
        weeks = ", ".join(
            f"{day_label(dt.date.fromisoformat(w))}: {fmt_pct(v, 0)}"
            for w, v in av_main["flip_missing_per_week"].items()
        )
        add(
            f"- flip chybí u {fmt_pct(av_main['flip_missing'])} minut v4 RTH (per týden {weeks}); "
            f"v3 RTH {fmt_pct(av_v3['flip_missing']) if av_v3 else '—'}, v4 celá seance "
            f"{fmt_pct(av_full['flip_missing']) if av_full else '—'}."
        )
        add(
            f"- walls_distance: pravidlová nula „nehlasuje“ (#1241) u "
            f"{fmt_pct(av_main['walls_skip_share'])} minut v4 RTH (hlas 0 celkem "
            f"{fmt_pct(av_main['components']['walls_distance']['zero'])}) — v4 tím mění, co "
            "hlas znamená proti v3."
        )
        add(
            f"- Uložené pásmo je směrové (ne neutral) u {fmt_pct(av_main['dir_stored_share'])} "
            f"minut; z nich {fmt_pct(av_main['flip_missing_among_stored_dir'])} bez flipu. "
            f"Při chybějícím flipu je pásmo směrové u "
            f"{fmt_pct(av_main['stored_dir_among_flip_missing'])} minut. Skóre bez CumΔ "
            f"|s| ≥ 0,15 rozhoduje bez flipu v {fmt_pct(av_main['flip_missing_among_nocd_dir'])}"
            f" případů. Ø počet hlasujících složek (z 10): "
            f"{fmt_num(av_main['votes_count_mean'], 1, sign=False)}."
        )
        add("")

    # 3. konvence
    add("## 3. Konvence času a citlivost na bázi výnosu")
    add("")
    add(
        "Z kódu: `instruments.run_minute(now)` — `now` = začátek minuty, ve které cyklus "
        "začal (`__main__`: `datetime.now().replace(second=0)`, cyklus po ~60 s bez zarovnání "
        "na hranu minuty). Pořadí v cyklu: `spot = _current_spot()` (`ticker.last` nebo "
        "`spot_override`) se čte **před sweepem**; pak `runtime.run_cycle` (sweep; řádek logu "
        "„Cyklus …“ je na jeho konci), `_watch_greeks`, `_watch_repair`, `map_state`, "
        "`setup_engine`, `t9_probes` a teprve pak tendence s týmž `spot` (`ts_min = now`). "
        "Pipeline běží sekvenčně (`gather_metrics`), takže **NQ čte spot až po celém "
        "`run_minute` ES**. Partice SentIndexu čte až `TendencyEngine`. Bar `ts_min` = začátek "
        "intervalu (`ibkr.underlying.Bar`), agregace 5s barů ho uzavře prvním 5s barem další "
        "minuty, takže `close(ts_min)` je cena v `ts_min + 60 s`."
    )
    add("")
    totals = {symbol: results[symbol]["checks"]["spot"]["v4"] for symbol in SYMBOLS}
    add(
        "**Hlavní důkaz konvence** jsou dvě měření: (1) rekonstruovaný spot hlasů (kap. 0) "
        "leží v baru ts u "
        + ", ".join(
            f"{symbol} {fmt_pct(totals[symbol]['bar'])} (jen v ts+1 "
            f"{fmt_pct(totals[symbol]['next'])})"
            for symbol in SYMBOLS
        )
        + " minut v4 RTH; (2) přísná báze `close(ts+1)` (tabulka níže) mění r30 jen málo. "
        "Logy níže měří konec `run_cycle`, ne okamžik čtení spotu ani výpočtu tendence: pro "
        "hlasy ze spotu je podíl „> 60 s“ horní mez (spot se čte dřív), pro sentindex dolní "
        "mez (partice se čte až po `run_cycle` a dalších krocích)."
    )
    add("")
    logs = results["cycle_offsets"]
    rows = []
    for symbol in SYMBOLS:
        c = logs.get(symbol, {})
        if not c.get("n"):
            rows.append([symbol, "0", "—", "—", "—", "—", "—", "—", "—"])
            continue
        rows.append(
            [
                symbol,
                f"{fmt_int(c['n'])} ({c['sessions']} seancí)",
                f"{c['p50']:.0f} s",
                f"{c['p90']:.0f} s",
                f"{c['p99']:.0f} s",
                f"{c['max']:.0f} s",
                fmt_pct(c["share_over_60"]),
                fmt_pct(c["share_over_120"]),
                f"{c['sweep_p50']:.1f} / {c['sweep_p90']:.1f} s".replace(".", ","),
            ]
        )
    add(
        md_table(
            [
                "sym",
                "cyklů v RTH (logy)",
                "konec run_cycle p50",
                "p90",
                "p99",
                "max",
                "> 60 s",
                "> 120 s",
                "sweep p50 / p90",
            ],
            rows,
        )
    )
    add("")
    add(
        f"Logy: počet souborů `data/logs/engine-*.log` {logs.get('files', 0)} (výpisy při "
        "nasazeních — neúplné pokrytí okna). Čas = řádek „Cyklus {sym} {ts_min}“ na konci "
        "`run_cycle`."
    )
    add("")
    add(
        "Citlivost r30 (v4 RTH) na bázi: `close(ts)` (hlavní), `open(ts)` (≈ cena "
        "na začátku minuty rozhodnutí), `close(ts+1)` (přísná, platí i při přetečení cyklu). "
        "Poslední sloupec = korelace hlasu s pohybem uvnitř minuty rozhodnutí "
        "`close(ts) − open(ts)` — ukazuje, kolik informace z minuty ts hlas nese."
    )
    add("")
    deltas: list[tuple[float, str]] = []
    star_flips: list[str] = []
    for symbol in SYMBOLS:
        sens = results[symbol]["slices"]["v4_rth"]["sensitivity"]
        rows = [
            [
                name,
                fmt_ci(sens[name]["close"]),
                fmt_ci(sens[name]["open"]),
                fmt_ci(sens[name]["next_close"]),
                fmt_ci(sens[name]["intraminute"]),
            ]
            for name in sens
        ]
        for name in sens:
            base, strict = sens[name]["close"], sens[name]["next_close"]
            if base["r"] is not None and strict["r"] is not None:
                deltas.append((strict["r"] - base["r"], f"{symbol} {name}"))
            if bool(base.get("excludes_zero")) != bool(strict.get("excludes_zero")):
                star_flips.append(f"{symbol} {name}")
        add(f"**{symbol}**")
        add("")
        add(
            md_table(
                [
                    "složka",
                    "r30 báze close(ts)",
                    "báze open(ts)",
                    "báze close(ts+1)",
                    "r s výnosem uvnitř minuty ts",
                ],
                rows,
            )
        )
        add("")
    if deltas:
        worst = max(deltas, key=lambda item: abs(item[0]))
        add(
            f"Báze close(ts+1) proti close(ts): největší změna r30 {fmt_num(worst[0])} "
            f"({worst[1]}); CI mimo nulu se změní u "
            f"{', '.join(star_flips) if star_flips else 'žádné složky'}."
        )
        add("")

    # 4. univariátní
    add("## 4. Univariátní korelace hlas × r_h (v4 RTH, hlavní řez)")
    add("")
    add(
        "Vynechávají se minuty, kde složka v rozpadu chybí; hlas 0 (včetně pravidlových nul, "
        "kap. 2) se počítá. Korelace jen pro hlas ≠ 0 je v kap. 7. r = Pearson v bodech, "
        "ρ = Spearman, rσ = Pearson s výnosem v jednotkách σ minuty (σ z 60 předchozích barů). "
        "charm_flow je v RTH skoro časová rampa (0 do 16:00 UTC, plná od 19:00) — jeho r "
        "měří hlavně rozdíl odpoledne × dopoledne; čištění o průměr hodiny je v kap. 7."
    )
    add("")
    for symbol in SYMBOLS:
        uni = results[symbol]["slices"]["v4_rth"]["univariate"]
        rows = []
        for name in COMPONENTS:
            cells = uni[name]
            rows.append(
                [
                    name,
                    fmt_int(cells[MAIN_H]["pearson_r"]["n"]),
                    *[fmt_ci(cells[h]["pearson_r"]) for h in HORIZONS],
                    *[fmt_ci(cells[h]["spearman_r"]) for h in HORIZONS],
                ]
            )
        add(f"**{symbol}** — Pearson a Spearman")
        add("")
        add(
            md_table(
                [
                    "složka",
                    "n (r30)",
                    *[f"r{h}" for h in HORIZONS],
                    *[f"ρ{h}" for h in HORIZONS],
                ],
                rows,
            )
        )
        add("")
        rows = [
            [name, *[fmt_ci(uni[name][h]["pearson_rs"]) for h in HORIZONS]] for name in COMPONENTS
        ]
        add(f"**{symbol}** — výnos v jednotkách σ minuty")
        add("")
        add(md_table(["složka", *[f"rσ{h}" for h in HORIZONS]], rows))
        add("")
    mc = results["multiple_comparisons"]
    pairing = results["sentindex_pairing"]
    add(
        f"**Mnohonásobné srovnání** (v4 RTH, {mc['m']} testů = {len(COMPONENTS)} složek × "
        f"{len(HORIZONS)} horizonty × {len(SYMBOLS)} symboly s definovanou korelací): CI mimo "
        f"nulu má Pearson {mc['pearson_hits']}× ({', '.join(mc['hits']) or '—'}), Spearman "
        f"{mc['spearman_hits']}×, rσ {mc['sigma_hits']}×. Náhodou se čeká "
        f"{fmt_num(mc['expected'], 1, sign=False)} (5 % z {mc['m']}); P(≥ {mc['pearson_hits']} "
        f"| nezávislé testy) = {fmt_num(mc['p_pearson'], 3, sign=False)}."
    )
    add("")
    add(
        f"**Průzkumná rodina** (bez předem daných hypotéz z kap. 8 — "
        f"{', '.join(f'{n} {s}' for n, s in PRESPECIFIED)} — a se sentindexem jen jednou: "
        f"hlas ES a NQ se liší v {fmt_int(pairing.get('differ'))} z "
        f"{fmt_int(pairing.get('n'))} spárovaných minut, "
        f"{fmt_pct(pairing.get('differ_share'))}): {mc['explor_m']} testů, CI mimo nulu "
        f"{len(mc['explor_hits'])}× ({', '.join(mc['explor_hits']) or '—'}); náhodou "
        f"{fmt_num(mc['explor_expected'], 1, sign=False)}, P(≥ {len(mc['explor_hits'])}) = "
        f"{fmt_num(mc['p_explor'], 3, sign=False)}. Bez charm_flow (efekt denní doby) "
        f"{len(mc['explor_hits_wo_charm'])}×. Horizonty 15/30/60 se překrývají a složky "
        "korelují, takže skutečné rozdělení počtu falešných nálezů je širší než binomické "
        "(P je spíš podhodnocená)."
    )
    add("")
    add(
        "**Vedlejší řezy** (jen pro srovnání; r v bodech, CI jen u r30; v3 RTH má 6 seancí "
        "— CI orientační, bez hvězdiček):"
    )
    add("")
    for symbol in SYMBOLS:
        rows = []
        for name in COMPONENTS:
            row = [name]
            for key in ("v3_rth", "v4_full"):
                uni = results[symbol]["slices"].get(key, {}).get("univariate")
                if uni is None:
                    row += ["—", "—", "—"]
                    continue
                row += [
                    fmt_num(uni[name][15]["pearson_r"]["r"]),
                    fmt_ci(uni[name][30]["pearson_r"], stars=slice_stars(results, key)),
                    fmt_num(uni[name][60]["pearson_r"]["r"]),
                ]
            rows.append(row)
        add(f"**{symbol}**")
        add("")
        add(
            md_table(
                [
                    "složka",
                    "v3 RTH r15",
                    "v3 RTH r30",
                    "v3 RTH r60",
                    "v4 celá r15",
                    "v4 celá r30",
                    "v4 celá r60",
                ],
                rows,
            )
        )
        add("")

    # 5. skóre
    add("## 5. Skóre (pevné váhy v4) × r_h")
    add("")
    add(
        "Skóre bez CumΔ = stropovaný vážený průměr zbylých hlasů s váhami v4 (normalizace "
        "jako `evaluate_tendency`). Varianty „bez CumΔ a walls_distance“ a „bez CumΔ "
        "a sentindex“ = totéž s váhou složky 0 (zmizí i ze jmenovatele) — pevné váhy, nic se "
        "netrénuje, takže nejde o fit. Uložené skóre obsahuje i CumΔ hlasy. Pásmo s hysterezí "
        "= replay `BandHysteresis` nad skóre varianty; bez hystereze = `band_of`."
    )
    add("")
    for key in ("v4_rth", "v3_rth", "v4_full"):
        stars = slice_stars(results, key)
        rows = []
        for symbol in SYMBOLS:
            sc = results[symbol]["slices"].get(key, {}).get("score")
            if sc is None:
                continue
            for name in ("score_stored", *SCORE_VARIANTS):
                corr = sc["corr"][name]
                rows.append(
                    [
                        symbol,
                        SCORE_LABELS[name],
                        *[fmt_ci(corr[h]["pearson"], stars=stars) for h in HORIZONS],
                        fmt_ci(corr[MAIN_H]["spearman"], stars=stars),
                        fmt_ci(corr[MAIN_H]["pearson_sigma"], stars=stars),
                    ]
                )
        label = results[SYMBOLS[0]]["slices"].get(key, {}).get("label", key)
        add(f"**{label}** — korelace" + ("" if stars else " (orientační, bez hvězdiček)"))
        add("")
        add(md_table(["sym", "skóre", "r15", "r30", "r60", "ρ30", "rσ30"], rows))
        add("")
    rows = []
    for symbol in SYMBOLS:
        sc = results[symbol]["slices"]["v4_rth"]["score"]
        for name, per_h in sc["diff"].items():
            rows.append([symbol, SCORE_LABELS[name], *[fmt_ci(per_h[h]) for h in HORIZONS]])
    add(
        "**Ztlumení složky proti skóre bez CumΔ** (v4 RTH): Δr = r(varianta) − r(bez CumΔ) "
        "na týchž minutách, párový bootstrap po seancích."
    )
    add("")
    add(md_table(["sym", "varianta", "Δr15", "Δr30", "Δr60"], rows))
    add("")
    add(
        "**Zásahovost pásem** (v4 RTH): podíl směrových minut, kde znaménko pásma = znaménko "
        "r_h (r_h = 0 vynecháno); náhoda = P(long)·P(r>0) + P(short)·P(r<0) na týchž "
        "minutách; edge = zásah − náhoda v p. b. s CI po seancích."
    )
    add("")
    rows = []
    for symbol in SYMBOLS:
        sc = results[symbol]["slices"]["v4_rth"]["score"]
        for name, (_, label) in BAND_VARIANTS.items():
            hits = sc["hits"][name]
            main = hits[MAIN_H]
            rows.append(
                [
                    symbol,
                    label,
                    fmt_int(main["n"]),
                    fmt_pct(main["hit"]),
                    fmt_pct(main["chance"]),
                    *[
                        fmt_ci(
                            {
                                "r": hits[h]["edge"],
                                "lo": hits[h]["lo"],
                                "hi": hits[h]["hi"],
                                "excludes_zero": hits[h].get("excludes_zero"),
                            },
                            digits=1,
                            scale=100.0,
                        )
                        for h in HORIZONS
                    ],
                ]
            )
    add(
        md_table(
            [
                "sym",
                "pásmo",
                "n směrových (h30)",
                "zásah h30",
                "náhoda h30",
                *[f"edge h{h}" for h in HORIZONS],
            ],
            rows,
        )
    )
    add("")
    rows = []
    for symbol in SYMBOLS:
        sc = results[symbol]["slices"]["v4_rth"]["score"]
        for name, label in (("band_stored", "uložené"), ("band_score_nocd", "bez CumΔ (hyst.)")):
            means = sc["band_means"][name]
            rows.append(
                [
                    symbol,
                    label,
                    *[
                        f"{fmt_int(means[b]['n'])} / {fmt_num(means[b]['mean_r30'], 2)} b / "
                        f"{fmt_pct(means[b]['share_up'], 0)}"
                        for b in BAND_SIGN
                    ],
                ]
            )
    add("Ø r30 podle pásma (n / Ø body / podíl r30 > 0), v4 RTH:")
    add("")
    add(md_table(["sym", "pásmo", *BAND_SIGN], rows))
    add("")

    # 6. walk-forward
    add("## 6. Walk-forward po týdnech (v4 RTH)")
    add("")
    add(
        "Trénink na všech předchozích týdnech, test na dalším. Vstupy = hlasy 10 složek "
        "(chybějící = 0). OLS s interceptem; ridge nad hlasy standardizovanými statistikami "
        "tréninku, λ = α·n. „naivní v4 / −wd / −sent“ = skóre bez CumΔ (a bez walls_distance "
        "resp. sentindex) s pevnými vahami v4 bez tréninku. Hodnota = OOS Pearson predikce × "
        "r_h v testovacím týdnu; řádek „pooled“ = všechny OOS minuty dohromady s CI po "
        "testovacích seancích. Predikce OLS a ridge jsou bez interceptu (jinak by pooled "
        "korelace nesla rozdíl Ø výnosu týdnů, ne signál hlasů)."
    )
    add("")
    for kind, title in (("r", "cíl r_h v bodech"), ("rs", "cíl r_h v σ minuty")):
        for symbol in SYMBOLS:
            wf = results[symbol]["slices"]["v4_rth"]["walk_forward"]["targets"][kind]
            rows = []
            for h in HORIZONS:
                for fold in wf[h]["folds"]:
                    rows.append(
                        [
                            h,
                            f"{len(fold['train_weeks'])} týd. → "
                            f"{day_label(dt.date.fromisoformat(fold['test_week']))}",
                            fmt_int(fold["n_test"]),
                            *[fmt_num(fold["r"][m]) for m in WF_MODELS],
                        ]
                    )
                pooled = wf[h]["pooled"]
                rows.append(
                    [
                        h,
                        "**pooled OOS**",
                        fmt_int(pooled["ols"]["n"]),
                        *[fmt_ci(pooled[m]) for m in WF_MODELS],
                    ]
                )
            add(f"**{symbol}** — {title}")
            add("")
            add(
                md_table(
                    ["h", "fold", "n test", *[WF_LABELS[m] for m in WF_MODELS]],
                    rows,
                )
            )
            add("")
    add(
        "Koeficienty OLS (cíl r30 v bodech) **fitované zvlášť na každém týdnu** — stabilita "
        "znamének na disjunktních datech. (Tréninkové množiny walk-forwardu jsou vnořené: "
        "týden 25. 9. je ve všech třech foldech, shoda znamének přes foldy proto není "
        "nezávislý doklad.)"
    )
    add("")
    for symbol in SYMBOLS:
        week_coef = results[symbol]["slices"]["v4_rth"]["walk_forward"]["week_coef"]
        coef_weeks = list(week_coef)
        rows = [
            [
                name,
                *[fmt_num(week_coef[w][name], 2) for w in coef_weeks],
                sign_count([week_coef[w][name] for w in coef_weeks]),
            ]
            for name in COMPONENTS
        ]
        headers = [
            "složka",
            *[f"týden {day_label(dt.date.fromisoformat(w))}" for w in coef_weeks],
            "znaménko",
        ]
        add(f"**{symbol}**")
        add("")
        add(md_table(headers, rows))
        add("")

    # 7. robustnost
    add("## 7. Robustnost a konfundéry (v4 RTH, r30)")
    add("")
    add(
        "Sloupce: **vše** = kap. 4; **hlas ≠ 0** = bez pravidlových nul (kap. 2); **živý "
        "bar** = jen minuty s `bar_source = ibkr` (engine měřil); **spot v baru** = bez minut, "
        "kde rekonstruovaný spot leží mimo bar ts (rozhodnutí po close(ts) nebo zatuchlý "
        "spot; minuty bez rekonstrukce zůstávají); **parc. známý den** = parciální "
        "r(hlas, r30 | průměr r30 téže seance známý v čase ts) (okna skončená do ts; "
        "začátek RTH bez známého výnosu vypadá) — decision-time, bez budoucnosti; "
        "**− hodina** = výnos minus průměr r30 podle hodiny UTC přes seance řezu "
        "(popisné, čistí efekt denní doby, např. rampu charm_flow). Per týden = Pearson "
        "r30 v týdnu bez CI."
    )
    add("")
    for symbol in SYMBOLS:
        rob = results[symbol]["slices"]["v4_rth"]["robustness"]
        rows = []
        for name, cell in rob.items():
            rows.append(
                [
                    SCORE_LABELS.get(name, name),
                    fmt_ci(cell["raw"]),
                    fmt_ci(cell["nonzero"]),
                    fmt_ci(cell["live"]),
                    fmt_ci(cell["spot_ok"]),
                    fmt_ci(cell["partial_known_day"]),
                    fmt_ci(cell["minus_hour"]),
                    " / ".join(fmt_num(v, 2) for v in cell["per_week"].values()),
                ]
            )
        weeks = " / ".join(
            day_label(dt.date.fromisoformat(w)) for w in next(iter(rob.values()))["per_week"]
        )
        add(f"**{symbol}**")
        add("")
        add(
            md_table(
                [
                    "složka / skóre",
                    "vše",
                    "hlas ≠ 0",
                    "živý bar",
                    "spot v baru",
                    "parc. známý den",
                    "− hodina",
                    f"per týden ({weeks})",
                ],
                rows,
            )
        )
        add("")
    for symbol in SYMBOLS:
        c = results[symbol]["slices"]["v4_rth"]["availability"]["components"]["sentindex"]
        add(
            f"- sentindex {symbol} (od 13. 9. 21:30 UTC, v4 RTH): chybí {fmt_pct(c['missing'])}, "
            f"0 {fmt_pct(c['zero'])}, +1 {fmt_pct(c['plus'])}, −1 {fmt_pct(c['minus'])}."
        )
    same_signal = (pairing.get("differ_share") or 1.0) < 0.1
    add(
        f"- sentindex ES × NQ: ve {fmt_int(pairing.get('n'))} spárovaných minutách se hlas "
        f"liší v {fmt_int(pairing.get('differ'))} ({fmt_pct(pairing.get('differ_share'))}), "
        f"oba 0 v {fmt_int(pairing.get('both_zero'))}; korelace hlasů "
        f"{fmt_num(pairing.get('corr'))}. "
        + (
            "Je to prakticky jeden signál na dvou silně korelovaných výnosech, ne dvě "
            "nezávislá potvrzení. "
            if same_signal
            else ""
        )
        + "Hlas vzniká ze zpráv (`TendencyEngine._sentiment`), ne z ceny."
    )
    add("")

    # 8. předem dané hypotézy
    add("## 8. Předem dané hypotézy a varianta „ztlumit složku“ (v4 RTH)")
    add("")
    add(
        "Hypotézy dané před tímto během, proto mimo průzkumnou rodinu kap. 4: walls_distance "
        "— #1241 (c3425d8) ho změnil, protože na trendovém dni dával falešný mean-reversion "
        "signál; vanna_flow ES/NQ a delta_flow NQ — rozhodnutí 13. 9. (komentář 5655763013): "
        "kandidát na vyřazení, pokud se záporné znaménko potvrdí na RTH řezu. Měřítko "
        "13. 9.: CI r30 mimo nulu na RTH řezu."
    )
    add("")
    rows = []
    for name, symbol in PRESPECIFIED:
        uni = results[symbol]["slices"]["v4_rth"]["univariate"][name]
        rob = results[symbol]["slices"]["v4_rth"]["robustness"][name]
        week_coef = results[symbol]["slices"]["v4_rth"]["walk_forward"]["week_coef"]
        rows.append(
            [
                f"{name} {symbol}",
                *[fmt_ci(uni[h]["pearson_r"]) for h in HORIZONS],
                fmt_ci(uni[MAIN_H]["pearson_rs"]),
                fmt_ci(rob["nonzero"]),
                fmt_ci(rob["partial_known_day"]),
                " / ".join(fmt_num(v, 2) for v in rob["per_week"].values()),
                sign_count([week_coef[w][name] for w in week_coef]),
            ]
        )
    add(
        md_table(
            [
                "hypotéza",
                *[f"r{h}" for h in HORIZONS],
                "rσ30",
                "r30 hlas ≠ 0",
                "r30 parc. známý den",
                "r30 per týden",
                "OLS po týdnech",
            ],
            rows,
        )
    )
    add("")
    rows = []
    for symbol in SYMBOLS:
        sc = results[symbol]["slices"]["v4_rth"]["score"]
        wf = results[symbol]["slices"]["v4_rth"]["walk_forward"]["targets"]["r"][MAIN_H]["pooled"]
        for name in SCORE_VARIANTS:
            band_key = f"band_{name}"
            edge = sc["hits"][band_key][MAIN_H]
            rows.append(
                [
                    symbol,
                    SCORE_LABELS[name],
                    fmt_ci(sc["corr"][name][MAIN_H]["pearson"]),
                    fmt_ci(sc["diff"][name][MAIN_H]) if name in sc["diff"] else "—",
                    fmt_ci(
                        {
                            "r": edge["edge"],
                            "lo": edge["lo"],
                            "hi": edge["hi"],
                            "excludes_zero": edge.get("excludes_zero"),
                        },
                        digits=1,
                        scale=100.0,
                    ),
                    fmt_ci(wf[f"naive_{name}"]),
                ]
            )
    add("Varianty skóre (pevné váhy v4), souhrn r30:")
    add("")
    add(
        md_table(
            [
                "sym",
                "skóre",
                "r30",
                "Δr30 proti bez CumΔ",
                "edge pásma h30 (p. b.)",
                "naivní pooled OOS r30 (testovací týdny)",
            ],
            rows,
        )
    )
    add("")

    # 9. srovnání
    add("## 9. Srovnání s kalibrací 13. 9. (#394)")
    add("")
    add(
        "13. 9. = v3, 7. 8.–11. 9., Pearson r30 v bodech, CI 90 % z 300 bootstrapů po dnech "
        "(živé řádky tendence, únik #794 se jich netýkal); hlavní tabulka 13. 9. je přes "
        "všechny minuty (i noc), srovnatelná s v4 celou seancí; RTH čísla 13. 9. (~8 800 "
        "minut) jen ve výčtu (komentář 5655526545). Teď = v4 22. 9.–16. 10., CI 95 % "
        "z bootstrapu po seancích. Vyšší |r| v RTH je z velké části šum menšího vzorku "
        "(19 seancí, ~6 800 minut) — viz šířky CI. v4 navíc mění význam walls_distance "
        "(pravidlová nula #1241, kap. 2) a max_pain (jen 90 min do close)."
    )
    add("")
    rows = []
    for name in COMPONENTS:
        ref = REF_0913_R30[name]
        cells = [name]
        for i, symbol in enumerate(SYMBOLS):
            full = results[symbol]["slices"]["v4_full"]["univariate"][name][MAIN_H]["pearson_r"]
            rth = results[symbol]["slices"]["v4_rth"]["univariate"][name][MAIN_H]["pearson_r"]
            cells += [fmt_num(ref[i]), fmt_ci(full), fmt_ci(rth)]
        rows.append(cells)
    score_cells = ["skóre (13. 9. v3 všech 12; teď bez CumΔ)"]
    for i, symbol in enumerate(SYMBOLS):
        full = results[symbol]["slices"]["v4_full"]["score"]["corr"]["score_nocd"][MAIN_H]
        rth = results[symbol]["slices"]["v4_rth"]["score"]["corr"]["score_nocd"][MAIN_H]
        score_cells += [
            fmt_num(REF_0913_SCORE_R30[i]),
            fmt_ci(full["pearson"]),
            fmt_ci(rth["pearson"]),
        ]
    rows.append(score_cells)
    add(
        md_table(
            [
                "složka",
                *[
                    f"{symbol} {label}"
                    for symbol in SYMBOLS
                    for label in ("13. 9. (všechny min.)", "v4 celá seance", "v4 RTH")
                ],
            ],
            rows,
        )
    )
    add("")
    summary = results["comparison"]
    rows = [[item["metric"], item["cut"], item["then"], item["now"]] for item in summary]
    add(md_table(["metrika", "řez (13. 9. ↔ teď)", "13. 9. (v3, 7. 8.–11. 9.)", "teď (v4)"], rows))
    add("")
    return "\n".join(parts) + "\n"


def comparison_rows(results: dict[str, Any]) -> list[dict[str, str]]:
    """Kap. 9: souhrnná čísla 13. 9. × teď — vždy srovnatelný řez.

    13. 9. „všechny minuty“ ↔ v4 celá seance; 13. 9. RTH ↔ v4 RTH.
    """

    def uni(symbol: str, name: str, key: str = "v4_rth", h: int = MAIN_H) -> dict[str, Any]:
        cell: dict[str, Any] = results[symbol]["slices"][key]["univariate"][name][h]["pearson_r"]
        return cell

    def max_abs(symbol: str, key: str) -> float:
        return max(
            abs(float(uni(symbol, name, key, h)["r"] or 0.0))
            for name in COMPONENTS
            for h in HORIZONS
        )

    oos = []
    for symbol in SYMBOLS:
        wf = results[symbol]["slices"]["v4_rth"]["walk_forward"]["targets"]["r"]
        for h in HORIZONS:
            for model in ("ols", *[f"ridge_{a:g}" for a in RIDGE_ALPHAS]):
                value = wf[h]["pooled"][model]["r"]
                if value is not None:
                    oos.append(value)
    naive = {
        symbol: results[symbol]["slices"]["v4_rth"]["walk_forward"]["targets"]["r"][MAIN_H][
            "pooled"
        ]["naive_score_nocd"]
        for symbol in SYMBOLS
    }
    hits = []
    for symbol in SYMBOLS:
        for h in HORIZONS:
            hit = results[symbol]["slices"]["v4_rth"]["score"]["hits"]["band_stored"][h]["hit"]
            if hit is not None:
                hits.append(hit)
    av = {symbol: results[symbol]["slices"]["v4_rth"]["availability"] for symbol in SYMBOLS}
    av_full = {symbol: results[symbol]["slices"]["v4_full"]["availability"] for symbol in SYMBOLS}
    stored = {
        symbol: results[symbol]["slices"]["v4_rth"]["score"]["corr"]["score_stored"][MAIN_H][
            "pearson"
        ]
        for symbol in SYMBOLS
    }
    full, rth = "všechny minuty ↔ v4 celá seance", "RTH ↔ v4 RTH"
    return [
        {
            "metric": "max. abs. r hlas × r_h (15/30/60)",
            "cut": full,
            "then": "≤ 0,05 (ES i NQ)",
            "now": f"ES {fmt_num(max_abs('ES', 'v4_full'), sign=False)}, "
            f"NQ {fmt_num(max_abs('NQ', 'v4_full'), sign=False)}",
        },
        {
            "metric": "max. abs. r hlas × r_h (15/30/60)",
            "cut": rth,
            "then": "ES: žádné CI mimo nulu; NQ (hlasy bez CumΔ): vanna_flow 0,070, "
            "delta_flow 0,055",
            "now": f"ES {fmt_num(max_abs('ES', 'v4_rth'), sign=False)}, "
            f"NQ {fmt_num(max_abs('NQ', 'v4_rth'), sign=False)}",
        },
        {
            "metric": "walk-forward OLS/ridge OOS r",
            "cut": "všechny minuty ↔ v4 RTH (walk-forward teď jen RTH)",
            "then": "abs. r ≤ 0,015, znaménko se střídá (4 foldy)",
            "now": f"pooled {fmt_num(min(oos))} … {fmt_num(max(oos))} (ES+NQ, 15/30/60)"
            if oos
            else "—",
        },
        {
            "metric": "naivní váhy OOS r30",
            "cut": "všechny minuty ↔ v4 RTH",
            "then": "v3: ES +0,017 / −0,037, NQ +0,030 / +0,005",
            "now": f"v4 bez CumΔ pooled: ES {fmt_ci(naive['ES'])}, NQ {fmt_ci(naive['NQ'])}",
        },
        {
            "metric": "skóre (uložené, s CumΔ) r30",
            "cut": rth,
            "then": "ES CI s nulou; NQ v3 +0,059 [+0,003; +0,135]",
            "now": f"ES {fmt_ci(stored['ES'])}, NQ {fmt_ci(stored['NQ'])}",
        },
        {
            "metric": "vanna_flow ES r30",
            "cut": full,
            "then": "−0,042 [−0,073; −0,011]",
            "now": fmt_ci(uni("ES", "vanna_flow", "v4_full")),
        },
        {
            "metric": "vanna_flow ES r30",
            "cut": rth,
            "then": "CI s nulou",
            "now": fmt_ci(uni("ES", "vanna_flow")),
        },
        {
            "metric": "vanna_flow NQ r30",
            "cut": rth,
            "then": "−0,070 [−0,124; −0,020]",
            "now": fmt_ci(uni("NQ", "vanna_flow")),
        },
        {
            "metric": "delta_flow NQ r30",
            "cut": rth,
            "then": "−0,055 [−0,091; −0,023]",
            "now": fmt_ci(uni("NQ", "delta_flow")),
        },
        {
            "metric": "gamma_at_price r30 ES / NQ",
            "cut": full,
            "then": "+0,018 / −0,007 (po odečtení dne −0,034 / −0,054)",
            "now": f"{fmt_ci(uni('ES', 'gamma_at_price', 'v4_full'))} / "
            f"{fmt_ci(uni('NQ', 'gamma_at_price', 'v4_full'))}",
        },
        {
            "metric": "flip chybí",
            "cut": full,
            "then": "49 % minut",
            "now": f"ES {fmt_pct(av_full['ES']['flip_missing'])}, "
            f"NQ {fmt_pct(av_full['NQ']['flip_missing'])} (RTH ES "
            f"{fmt_pct(av['ES']['flip_missing'])}, NQ {fmt_pct(av['NQ']['flip_missing'])})",
        },
        {
            "metric": "sentindex",
            "cut": rth,
            "then": "mrtvý od 13. 8. (#1136), hodnotit od 13. 9. 21:30",
            "now": f"chybí ES {fmt_pct(av['ES']['components']['sentindex']['missing'])}, "
            f"NQ {fmt_pct(av['NQ']['components']['sentindex']['missing'])}; r30 ES "
            f"{fmt_ci(uni('ES', 'sentindex'))}, NQ {fmt_ci(uni('NQ', 'sentindex'))}",
        },
        {
            "metric": "zásahovost uloženého pásma",
            "cut": "všechny minuty ↔ v4 RTH",
            "then": "47–55 % (mince)",
            "now": f"{fmt_pct(min(hits))} – {fmt_pct(max(hits))}" if hits else "—",
        },
    ]


# ----------------------------------------------------------------- main


def analyze_symbol(
    frame: pd.DataFrame,
    bars: pd.DataFrame,
    symbol: str,
    start: dt.date,
    end: dt.date,
    reps: int,
    seed: int,
) -> tuple[dict[str, Any], dict[str, Slice]]:
    rows = frame[frame["symbol"] == symbol].sort_values("ts").reset_index(drop=True)
    rows["band_check"] = replay_bands(rows, "score")
    for variant in SCORE_VARIANTS:
        rows[f"band_{variant}"] = replay_bands(rows, variant)
    sessions = [trading_session_date(ts.to_pydatetime()) for ts in rows["ts"]]
    rows["session"] = sessions
    for session in set(sessions):
        session_times(session)
    rows["trading"] = [start <= s <= end and is_trading_session(s) for s in sessions]
    ts64 = pd.DatetimeIndex(rows["ts"]).tz_convert(None).to_numpy(dtype="datetime64[ns]")
    open64 = np.array([SESSION_TIMES[s][0] for s in sessions], dtype="datetime64[ns]")
    settle64 = np.array([SESSION_TIMES[s][1] for s in sessions], dtype="datetime64[ns]")
    halt64 = np.array([SESSION_TIMES[s][2] for s in sessions], dtype="datetime64[ns]")
    rows["rth"] = (ts64 >= open64) & (ts64 < settle64)
    # Celá seance: jen minuty obchodování (denní pauza 16:00–17:00 CT nemá bary)
    rows["tradable"] = ts64 < halt64
    rows = attach_returns(rows, bars)
    in_rth = rows["trading"] & rows["rth"]
    source = rows.loc[in_rth, "bar_source"]
    present = source.notna()
    checks = {
        "rows": int(rows["trading"].sum()),
        "by_version": {
            int(k): int(v) for k, v in rows.loc[rows["trading"], "wv"].value_counts().items()
        },
        "score_check_max_abs": float(np.nanmax(np.abs(rows["score_check"] - rows["score"]))),
        "band_replay_match": float((rows["band_check"] == rows["band"])[in_rth].mean()),
        # Chybějící bar není doplněný bar — počítá se zvlášť
        "bars_missing_share": float((~present).mean()),
        "bars_nonlive_share": float((source[present] != "ibkr").mean()) if present.any() else None,
        "spot": spot_check(rows[in_rth]),
    }
    slices = make_slices(rows, symbol, reps, seed)
    out: dict[str, Any] = {
        "checks": checks,
        "regime": regime_weeks(bars, start, end),
        "slices": {},
    }
    for key, sl in slices.items():
        print(f"  {symbol} {sl.label}: {len(sl.frame)} minut, {len(sl.boot.sessions)} seancí")
        entry: dict[str, Any] = {
            "label": sl.label,
            "sessions": sl.boot.sessions,
            "coverage": coverage(rows, sl),
            "availability": availability(sl),
            "univariate": univariate(sl, ("r", "rs") if key == "v4_rth" else ("r",)),
            "score": score_analysis(sl),
        }
        if key == "v4_rth":
            entry["sensitivity"] = sensitivity(sl)
            entry["walk_forward"] = walk_forward(sl)
            entry["robustness"] = robustness(sl)
        out["slices"][key] = entry
    return out, slices


def spot_shares(part: pd.DataFrame) -> dict[str, Any]:
    status = part["spot_status"]
    known = status != ""
    n = int(known.sum())
    shares = {
        key: float((status[known] == key).mean()) if n else None for key in SPOT_STATUS_LABELS
    }
    off_grid = float(part.loc[known, "spot_off_grid"].mean()) if n else None
    off_live = known & (part["bar_source"] == "ibkr")
    return {
        "minutes": len(part),
        "n": n,
        **shares,
        "off_grid": off_grid,
        # Spot mimo bar ts, přestože bar ts je živý (stream běžel)
        "outside_live": float((off_live & (status != "bar")).sum() / off_live.sum())
        if off_live.any()
        else None,
    }


def spot_check(rows: pd.DataFrame) -> dict[str, Any]:
    """Kap. 0: rekonstruovaný spot hlasů × bary — po seancích a za v4 RTH celkem."""
    by_day = []
    for session, part in rows.groupby("session", sort=True):
        by_day.append(
            {"session": session, "wv": int(part["wv"].mode().iloc[0])} | spot_shares(part)
        )
    return {"by_day": by_day, "v4": spot_shares(rows[rows["wv"] == 4])}


def multiple_comparisons(results: dict[str, Any]) -> dict[str, Any]:
    """Počty CI mimo nulu: celá rodina 60 testů a průzkumná rodina zvlášť.

    Průzkumná rodina = bez předem daných hypotéz (`PRESPECIFIED`) a se sentindexem
    jen jednou (hlas ES a NQ je skoro tentýž — počítá se ES).
    """
    counts = {"pearson_r": 0, "spearman_r": 0, "pearson_rs": 0}
    m = 0
    hits: list[str] = []
    explor_m = 0
    explor_hits: list[str] = []
    for symbol in SYMBOLS:
        uni = results[symbol]["slices"]["v4_rth"]["univariate"]
        for name in COMPONENTS:
            for h in HORIZONS:
                cell = uni[name][h]
                if cell["pearson_r"]["r"] is None:
                    continue
                m += 1
                for kind in counts:
                    counts[kind] += int(bool(cell[kind].get("excludes_zero")))
                label = f"{symbol} {name} r{h}"
                hit = bool(cell["pearson_r"].get("excludes_zero"))
                if hit:
                    hits.append(label)
                if (name, symbol) in PRESPECIFIED or (name == "sentindex" and symbol != "ES"):
                    continue
                explor_m += 1
                if hit:
                    explor_hits.append(label)
    return {
        "m": m,
        "expected": 0.05 * m,
        "pearson_hits": counts["pearson_r"],
        "spearman_hits": counts["spearman_r"],
        "sigma_hits": counts["pearson_rs"],
        "p_pearson": binom_tail(m, counts["pearson_r"]) if m else None,
        "hits": hits,
        "explor_m": explor_m,
        "explor_expected": 0.05 * explor_m,
        "explor_hits": explor_hits,
        "p_explor": binom_tail(explor_m, len(explor_hits)) if explor_m else None,
        "explor_hits_wo_charm": [h for h in explor_hits if "charm_flow" not in h],
    }


def sentindex_pairing(slices: Mapping[str, Mapping[str, Slice]]) -> dict[str, Any]:
    """Shoda hlasu sentindex ES × NQ ve spárovaných minutách v4 RTH."""
    es = slices["ES"]["v4_rth"].frame[["ts", "v_sentindex"]]
    nq = slices["NQ"]["v4_rth"].frame[["ts", "v_sentindex"]]
    pair = es.merge(nq, on="ts", suffixes=("_es", "_nq")).dropna()
    if pair.empty:
        return {"n": 0}
    a = pair["v_sentindex_es"].to_numpy()
    b = pair["v_sentindex_nq"].to_numpy()
    return {
        "n": len(pair),
        "differ": int((a != b).sum()),
        "differ_share": float((a != b).mean()),
        "both_zero": int(((a == 0) & (b == 0)).sum()),
        "corr": safe_corr(a, b),
    }


def main(argv: Sequence[str] | None = None) -> int:
    # Konzole Windows (cp1250) neumí „Δ“ ani „→“ — výstup vždy v UTF-8
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--db",
        default=os.environ.get("GEXLENS_HOST_DATABASE_URL")
        or os.environ.get("GEXLENS_DATABASE_URL", ""),
    )
    parser.add_argument("--data", default=os.environ.get("GEXLENS_DATA_DIR", "data"))
    parser.add_argument("--out", required=True, help="adresář výstupu tables.md a tables.json")
    parser.add_argument("--start", default="2026-09-14", help="první obchodní den (seance)")
    parser.add_argument("--end", default="2026-10-16", help="poslední obchodní den (seance)")
    parser.add_argument("--boot", type=int, default=2000, help="opakování bootstrapu (≥ 2000)")
    parser.add_argument("--seed", type=int, default=1113)
    parser.add_argument("--logs", default="", help="adresář logů enginu (výchozí {data}/logs)")
    args = parser.parse_args(argv)
    if not args.db:
        parser.error("chybí --db, GEXLENS_HOST_DATABASE_URL nebo GEXLENS_DATABASE_URL")
    if args.boot < 2000:
        parser.error("--boot musí být aspoň 2000")
    started = time.monotonic()
    start = dt.date.fromisoformat(args.start)
    end = dt.date.fromisoformat(args.end)
    since = session_bounds(start)[0]
    until = session_bounds(end)[1]
    data = Path(args.data)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Tendence z PG {since:%Y-%m-%d %H:%M} – {until:%Y-%m-%d %H:%M} UTC …")
    frame = load_tendency(args.db, since, until)
    print(f"  {len(frame)} řádků")
    results: dict[str, Any] = {}
    slices: dict[str, dict[str, Slice]] = {}
    for symbol in SYMBOLS:
        print(f"Bary {symbol} …")
        bars = load_bars(
            data / "derived",
            symbol,
            since - dt.timedelta(hours=3),
            until + dt.timedelta(hours=3),
        )
        results[symbol], slices[symbol] = analyze_symbol(
            frame, bars, symbol, start, end, args.boot, args.seed
        )
        del bars
    print("Logy cyklu enginu …")
    results["cycle_offsets"] = cycle_offsets(
        Path(args.logs) if args.logs else data / "logs", start, end
    )
    results["multiple_comparisons"] = multiple_comparisons(results)
    results["sentindex_pairing"] = sentindex_pairing(slices)
    results["comparison"] = comparison_rows(results)
    results["meta"] = {
        "generated": dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M"),
        "runtime_s": time.monotonic() - started,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "boot": args.boot,
        "seed": args.seed,
        "components": list(COMPONENTS),
        "excluded": list(EXCLUDED),
        "horizons": list(HORIZONS),
        "weights": dict(WEIGHTS),
        "weights_version": TENDENCY_WEIGHTS_VERSION,
    }
    (out_dir / "tables.md").write_text(render(results), encoding="utf-8")
    (out_dir / "tables.json").write_text(
        json.dumps(jsonable(results), ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"Hotovo za {time.monotonic() - started:.0f} s: {out_dir / 'tables.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
