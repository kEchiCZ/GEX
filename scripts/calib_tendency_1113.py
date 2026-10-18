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
  `now` = začátek minuty, ve které cyklus začal; `spot` se čte před sweepem,
  tendence běží po něm). Bar nese `ts_min` = začátek intervalu
  (`ibkr.underlying.Bar`), uzavře se příchodem prvního 5s baru další minuty —
  `close(ts_min)` je cena v ts_min + 60 s, `open(ts_min)` cena na začátku minuty.
- Dopředný výnos `r_h = close(ts_min + h) − close(ts_min)` v bodech a v jednotkách
  σ minuty: `r_h / (σ_1m · √h)`, σ_1m = směrodatná odchylka 1min změn close
  z 60 barů `ts_min − 60 … ts_min − 1` (jen dokončené bary před rozhodnutím).
- Okno musí skončit v RTH: cena `close(ts_min + h)` je v ts_min + h + 1 min,
  proto `ts_min + h + 1 min ≤ settle_ts(den)` (20:00 UTC v létě).
- Citlivost: báze `open(ts_min)` a `close(ts_min + 1)` (přísná — platí, i když
  cyklus přeteče přes konec minuty); korelace hlasu s výnosem uvnitř minuty
  rozhodnutí `close(ts_min) − open(ts_min)` ukazuje, kolik na volbě báze záleží.
- Žádný řádek tendence po ts_min; normalizace a standardizace jen z minulosti
  (σ z předchozích barů) nebo z tréninkových týdnů walk-forwardu. Spearman
  pořadí a odečtení denního driftu jsou popisné diagnostiky, ne prediktor.

RTH a obchodní den z helperů repa: `settle.us_open_ts`, `settle.settle_ts`,
`settle.trading_session_date`, `settle.is_trading_session`, `settle.session_bounds`.

Statistika: CI 95 % bootstrapem po seancích (blokově, `--boot` opakování, pevný
seed) — překrývající se minutové výnosy nedávají naivní n. Pearson přes součtové
statistiky seancí; Spearman = Pearson pořadí z celého řezu (pořadí se v
replikacích nepřepočítává). Mnohonásobné srovnání: 10 složek × 3 horizonty ×
2 symboly; očekávaný počet CI mimo nulu náhodou se vypíše.

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


def fmt_ci(res: Mapping[str, Any], digits: int = 3, scale: float = 1.0) -> str:
    """„r [lo; hi]“, hvězdička = CI mimo nulu; `scale` 100 = procentní body."""
    r, lo, hi = res.get("r"), res.get("lo"), res.get("hi")
    if r is None or not math.isfinite(r):
        return "—"
    star = "*" if res.get("excludes_zero") else ""
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

    @classmethod
    def make(cls, sessions: Sequence[dt.date], reps: int, seed: int, key: str) -> Boot:
        rng = np.random.default_rng([seed, zlib.crc32(key.encode())])
        size = len(sessions)
        counts = rng.multinomial(size, np.full(size, 1.0 / size), size=reps).astype(float)
        return cls(list(sessions), counts)


def session_sums(x: np.ndarray, y: np.ndarray, idx: np.ndarray, size: int) -> np.ndarray:
    def acc(weights: np.ndarray | None) -> np.ndarray:
        return np.bincount(idx, weights=weights, minlength=size).astype(float)

    return np.column_stack([acc(None), acc(x), acc(y), acc(x * x), acc(y * y), acc(x * y)])


def boot_corr(x: np.ndarray, y: np.ndarray, idx: np.ndarray, boot: Boot) -> dict[str, Any]:
    """Pearson s 95% CI bootstrapem po seancích; NaN v x nebo y se vynechá."""
    mask = np.isfinite(x) & np.isfinite(y)
    n = int(mask.sum())
    out: dict[str, Any] = {"n": n, "sessions": int(len(np.unique(idx[mask]))) if n else 0}
    if n < 30 or out["sessions"] < 3:
        return out | {"r": None, "lo": None, "hi": None, "excludes_zero": False}
    sums = session_sums(x[mask], y[mask], idx[mask], len(boot.sessions))
    r = float(corr_from_sums(sums.sum(axis=0)))
    reps = corr_from_sums(boot.counts @ sums)
    reps = reps[np.isfinite(reps)]
    if not math.isfinite(r) or len(reps) < 100:
        return out | {"r": None, "lo": None, "hi": None, "excludes_zero": False}
    lo, hi = (float(v) for v in np.percentile(reps, [2.5, 97.5]))
    return out | {"r": r, "lo": lo, "hi": hi, "excludes_zero": bool(lo > 0 or hi < 0)}


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

    def stats(total: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        cnt, hits, longs, ups = np.moveaxis(total, -1, 0)
        with np.errstate(divide="ignore", invalid="ignore"):
            hit = hits / cnt
            p_long = longs / cnt
            p_up = ups / cnt
        chance = p_long * p_up + (1 - p_long) * (1 - p_up)
        return hit, chance

    hit, chance = stats(sums.sum(axis=0))
    hit_b, chance_b = stats(boot.counts @ sums)
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
        for vote in votes:
            name = vote["name"]
            if name == "sentindex" and ts < SENTINDEX_FROM:
                continue
            all_votes[name] = float(vote["vote"])
            stored_weights[name] = float(vote["weight"])
        for name in WEIGHTS:
            record[f"v_{name}"] = all_votes.get(name, np.nan)
        record["score_check"] = capped_score(all_votes, stored_weights)
        kept = {name: v for name, v in all_votes.items() if name not in EXCLUDED}
        record["score_nocd"] = capped_score(kept, WEIGHTS)
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


def score_analysis(sl: Slice) -> dict[str, Any]:
    """Kap. 5: skóre bez CumΔ × r_h a zásahovost pásem proti náhodě."""
    frame = sl.frame
    out: dict[str, Any] = {"corr": {}, "hits": {}, "band_means": {}}
    scores = {
        "score_stored": frame["score"].to_numpy(dtype=float),
        "score_nocd": frame["score_nocd"].to_numpy(dtype=float),
    }
    bands = {
        "band_stored": frame["band"].to_numpy(),
        "band_nocd_hyst": frame["band_nocd_hyst"].to_numpy(),
        "band_nocd_raw": np.array(
            [band_of(s) if math.isfinite(s) else "neutral" for s in scores["score_nocd"]],
            dtype=object,
        ),
    }
    for name, values in scores.items():
        out["corr"][name] = {
            h: {
                "pearson": boot_corr(values, sl.ret(h), sl.idx, sl.boot),
                "spearman": boot_spearman(values, sl.ret(h), sl.idx, sl.boot),
                "pearson_sigma": boot_corr(values, sl.ret(h, "rs"), sl.idx, sl.boot),
            }
            for h in HORIZONS
        }
    for name, band in bands.items():
        direction = np.array([BAND_SIGN[b] for b in band], dtype=float)
        out["hits"][name] = {h: boot_hits(direction, sl.ret(h), sl.idx, sl.boot) for h in HORIZONS}
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


def walk_forward(sl: Slice) -> dict[str, Any]:
    """Kap. 6: trénink na předchozích týdnech, test na dalším; OOS korelace."""
    frame = sl.frame
    weeks = sorted({week_of(s) for s in sl.boot.sessions})
    week_arr = frame["session"].map(week_of).to_numpy()
    x_all = np.nan_to_num(frame[[f"v_{c}" for c in COMPONENTS]].to_numpy(dtype=float), nan=0.0)
    naive = frame["score_nocd"].to_numpy(dtype=float)
    out: dict[str, Any] = {"weeks": weeks, "targets": {}}
    models = ["ols", *[f"ridge_{a:g}" for a in RIDGE_ALPHAS], "naive_v4"]
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
                fold_pred = {
                    "ols": x_all[test] @ coef[1:],
                    "naive_v4": naive[test],
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
            pooled = {m: boot_corr(preds[m], y_all, sl.idx, sl.boot) for m in models}
            per_h[h] = {"folds": folds, "pooled": pooled}
        out["targets"][kind] = per_h
    return out


def drift_check(sl: Slice) -> dict[str, Any]:
    """Kap. 7: vanna_flow, delta_flow, gamma_at_price po odečtení denního driftu."""
    frame = sl.frame
    out: dict[str, Any] = {}
    sessions = frame["session"].to_numpy()
    weeks = frame["session"].map(week_of).to_numpy()
    for name in ("vanna_flow", "delta_flow", "gamma_at_price", "sentindex"):
        x = sl.vote(name)
        out[name] = {}
        for h in HORIZONS:
            y = sl.ret(h)
            day_mean = pd.Series(y).groupby(sessions).transform("mean").to_numpy()
            entry = {
                "raw": boot_corr(x, y, sl.idx, sl.boot),
                "minus_day": boot_corr(x, y - day_mean, sl.idx, sl.boot),
            }
            if h == MAIN_H:
                per_week = {}
                for week in sorted(set(weeks)):
                    mask = weeks == week
                    per_week[week.isoformat()] = safe_corr(x[mask], y[mask])
                entry["per_week"] = per_week
            out[name][h] = entry
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


def render(results: dict[str, Any]) -> str:
    parts: list[str] = []
    meta = results["meta"]
    add = parts.append
    add("# Kalibrace vah tendence 14. 9.–16. 10. 2026 (E-0.12, #1113) — jen čtení")
    add("")
    add(
        f"Vygenerováno {meta['generated']} UTC, běh {meta['runtime_s']:.0f} s. Okno seancí "
        f"{meta['start']} – {meta['end']}; bootstrap {meta['boot']}× po seancích, seed "
        f"{meta['seed']}; CI 95 % (percentilové); `*` = CI mimo nulu. Hodnocené složky: "
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
                "podíl RTH minut s doplněným barem (ne živý)",
            ],
            rows,
        )
    )
    add("")
    add(
        "Přepočet uloženého skóre ze všech uložených hlasů tímtéž vzorcem (stropovaný vážený "
        "průměr) ověřuje, že skóre bez CumΔ v kap. 5 počítá stejně jako engine. Replay "
        "hystereze (`BandHysteresis`, reset při změně UTC data a mezeře > 5 min) nad uloženým "
        "skóre ověřuje pásmo bez CumΔ s hysterezí."
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

    # 2. dostupnost
    add("## 2. Dostupnost hlasů")
    add("")
    add(
        "Podíl minut, kde složka nehlasuje (chybí v rozpadu) a kde hlasuje 0. „+/−“ = podíl "
        "kladných/záporných hlasů, Ø = průměr přítomných hlasů."
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
        "na hranu minuty); `spot` = `ticker.last` před sweepem; tendence běží po "
        "`runtime.run_cycle` a ukládá `ts_min = now`. Bar `ts_min` = začátek intervalu "
        "(`ibkr.underlying.Bar`), agregace 5s barů ho uzavře prvním 5s barem další minuty, "
        "takže `close(ts_min)` je cena v `ts_min + 60 s`. Rozhodnutí je tedy před "
        "`close(ts_min)`, pokud cyklus doběhne do konce minuty — tabulka z logů enginu:"
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
                "konec cyklu p50",
                "p90",
                "p99",
                "max",
                "> 60 s (po close ts_min)",
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
        "`run_cycle`; tendence běží hned po něm."
    )
    add("")
    add(
        "Citlivost r30 (v4 RTH) na bázi: `close(ts)` (hlavní), `open(ts)` (≈ cena "
        "v okamžiku rozhodnutí), `close(ts+1)` (přísná, platí i při přetečení cyklu). "
        "Poslední sloupec = korelace hlasu s pohybem uvnitř minuty rozhodnutí "
        "`close(ts) − open(ts)` — ukazuje, kolik informace z minuty ts hlas nese."
    )
    add("")
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

    # 4. univariátní
    add("## 4. Univariátní korelace hlas × r_h (v4 RTH, hlavní řez)")
    add("")
    add(
        "Jen minuty, kde složka hlasovala (chybějící hlas se vynechává). r = Pearson v bodech, "
        "ρ = Spearman, rσ = Pearson s výnosem v jednotkách σ minuty (σ z 60 předchozích barů)."
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
    add(
        f"**Mnohonásobné srovnání** (v4 RTH, {mc['m']} testů = {len(COMPONENTS)} složek × "
        f"{len(HORIZONS)} horizonty × {len(SYMBOLS)} symboly s definovanou korelací): CI mimo "
        f"nulu má Pearson {mc['pearson_hits']}×, Spearman {mc['spearman_hits']}×, rσ "
        f"{mc['sigma_hits']}×. Náhodou se čeká {fmt_num(mc['expected'], 1, sign=False)} "
        f"(5 % z {mc['m']}); P(≥ {mc['pearson_hits']} | nezávislé testy) = "
        f"{fmt_num(mc['p_pearson'], 3, sign=False)}. Horizonty 15/30/60 se překrývají "
        "a složky korelují, takže skutečné rozdělení počtu falešných nálezů je širší než "
        "binomické (P výše je tedy spíš podhodnocená)."
    )
    add("")
    add("**Vedlejší řezy** (jen pro srovnání; r v bodech, CI jen u r30):")
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
                    fmt_ci(uni[name][30]["pearson_r"]),
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
    add("## 5. Skóre bez cum_delta_slope a divergence × r_h")
    add("")
    add(
        "Skóre bez CumΔ = stropovaný vážený průměr zbylých hlasů s váhami v4 (normalizace "
        "jako `evaluate_tendency`). Uložené skóre obsahuje i CumΔ hlasy. Pásmo bez CumΔ "
        "s hysterezí = replay `BandHysteresis` nad skóre bez CumΔ; bez hystereze = `band_of`."
    )
    add("")
    for key in ("v4_rth", "v3_rth", "v4_full"):
        rows = []
        for symbol in SYMBOLS:
            sc = results[symbol]["slices"].get(key, {}).get("score")
            if sc is None:
                continue
            for name, label in (("score_stored", "uložené (s CumΔ)"), ("score_nocd", "bez CumΔ")):
                corr = sc["corr"][name]
                rows.append(
                    [
                        symbol,
                        label,
                        *[fmt_ci(corr[h]["pearson"]) for h in HORIZONS],
                        fmt_ci(corr[MAIN_H]["spearman"]),
                        fmt_ci(corr[MAIN_H]["pearson_sigma"]),
                    ]
                )
        label = results[SYMBOLS[0]]["slices"].get(key, {}).get("label", key)
        add(f"**{label}** — korelace")
        add("")
        add(md_table(["sym", "skóre", "r15", "r30", "r60", "ρ30", "rσ30"], rows))
        add("")
    add(
        "**Zásahovost pásem** (v4 RTH): podíl směrových minut, kde znaménko pásma = znaménko "
        "r_h (r_h = 0 vynecháno); náhoda = P(long)·P(r>0) + P(short)·P(r<0) na týchž "
        "minutách; edge = zásah − náhoda s CI po seancích."
    )
    add("")
    rows = []
    for symbol in SYMBOLS:
        sc = results[symbol]["slices"]["v4_rth"]["score"]
        for name, label in (
            ("band_stored", "uložené (hystereze, s CumΔ)"),
            ("band_nocd_hyst", "bez CumΔ, hystereze"),
            ("band_nocd_raw", "bez CumΔ, bez hystereze"),
        ):
            for h in HORIZONS:
                hit = sc["hits"][name][h]
                rows.append(
                    [
                        symbol,
                        label,
                        h,
                        fmt_int(hit["n"]),
                        fmt_pct(hit["hit"]),
                        fmt_pct(hit["chance"]),
                        fmt_ci(
                            {
                                "r": hit["edge"],
                                "lo": hit["lo"],
                                "hi": hit["hi"],
                                "excludes_zero": hit.get("excludes_zero"),
                            },
                            digits=1,
                            scale=100.0,
                        ),
                    ]
                )
    add(md_table(["sym", "pásmo", "h", "n směrových", "zásah", "náhoda", "edge p. b. [CI]"], rows))
    add("")
    rows = []
    for symbol in SYMBOLS:
        sc = results[symbol]["slices"]["v4_rth"]["score"]
        for name, label in (("band_stored", "uložené"), ("band_nocd_hyst", "bez CumΔ (hyst.)")):
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
        "tréninku, λ = α·n. „naivní v4“ = skóre bez CumΔ s váhami v4 bez tréninku. Hodnota "
        "= OOS Pearson predikce × r_h v testovacím týdnu; řádek „pooled“ = všechny OOS minuty "
        "dohromady s CI po seancích. Predikce OLS a ridge jsou bez interceptu (jinak by "
        "pooled korelace nesla rozdíl Ø výnosu týdnů, ne signál hlasů)."
    )
    add("")
    for kind, title in (("r", "cíl r_h v bodech"), ("rs", "cíl r_h v σ minuty")):
        for symbol in SYMBOLS:
            wf = results[symbol]["slices"]["v4_rth"]["walk_forward"]["targets"][kind]
            models = ["ols", *[f"ridge_{a:g}" for a in RIDGE_ALPHAS], "naive_v4"]
            rows = []
            for h in HORIZONS:
                for fold in wf[h]["folds"]:
                    rows.append(
                        [
                            h,
                            f"{len(fold['train_weeks'])} týd. → "
                            f"{day_label(dt.date.fromisoformat(fold['test_week']))}",
                            fmt_int(fold["n_test"]),
                            *[fmt_num(fold["r"][m]) for m in models],
                        ]
                    )
                pooled = wf[h]["pooled"]
                rows.append(
                    [
                        h,
                        "**pooled OOS**",
                        fmt_int(pooled["ols"]["n"]),
                        *[fmt_ci(pooled[m]) for m in models],
                    ]
                )
            add(f"**{symbol}** — {title}")
            add("")
            add(
                md_table(
                    [
                        "h",
                        "fold",
                        "n test",
                        "OLS",
                        *[f"ridge α={a:g}".replace(".", ",") for a in RIDGE_ALPHAS],
                        "naivní v4",
                    ],
                    rows,
                )
            )
            add("")
    add("Koeficienty OLS (cíl r30 v bodech) per fold — stabilita znamének:")
    add("")
    for symbol in SYMBOLS:
        folds = results[symbol]["slices"]["v4_rth"]["walk_forward"]["targets"]["r"][MAIN_H]["folds"]
        rows = [
            [name, *[fmt_num(fold["ols_coef"][name], 2) for fold in folds]] for name in COMPONENTS
        ]
        headers = [
            "složka",
            *[f"→ {day_label(dt.date.fromisoformat(f['test_week']))}" for f in folds],
        ]
        add(f"**{symbol}**")
        add("")
        add(md_table(headers, rows))
        add("")

    # 7. drift
    add("## 7. vanna_flow, delta_flow, gamma_at_price, sentindex (v4 RTH)")
    add("")
    add(
        "„− den“ = výnos po odečtení průměru r_h téže seance (kontrola drift-confound; "
        "popisná, používá výnosy celého dne). Per týden = Pearson r30 v týdnu bez CI."
    )
    add("")
    for symbol in SYMBOLS:
        drift = results[symbol]["slices"]["v4_rth"]["drift"]
        rows = []
        for name in drift:
            cell = drift[name]
            weeks = cell[MAIN_H]["per_week"]
            rows.append(
                [
                    name,
                    *[fmt_ci(cell[h]["raw"]) for h in HORIZONS],
                    *[fmt_ci(cell[h]["minus_day"]) for h in HORIZONS],
                    " / ".join(fmt_num(v, 2) for v in weeks.values()),
                ]
            )
        add(f"**{symbol}**")
        add("")
        add(
            md_table(
                [
                    "složka",
                    *[f"r{h}" for h in HORIZONS],
                    *[f"r{h} − den" for h in HORIZONS],
                    "r30 per týden",
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
    add("")

    # 8. srovnání
    add("## 8. Srovnání s kalibrací 13. 9. (#394)")
    add("")
    add(
        "13. 9. = v3, 7. 8.–11. 9., všechny minuty (i noc), Pearson r30 v bodech, CI 90 % "
        "z 300 bootstrapů po dnech (živé řádky tendence, únik #794 se jich netýkal). "
        "Teď = v4 RTH "
        "22. 9.–16. 10., CI 95 % z bootstrapu po seancích."
    )
    add("")
    rows = []
    for name in COMPONENTS:
        ref = REF_0913_R30[name]
        now_es = results["ES"]["slices"]["v4_rth"]["univariate"][name][MAIN_H]["pearson_r"]
        now_nq = results["NQ"]["slices"]["v4_rth"]["univariate"][name][MAIN_H]["pearson_r"]
        rows.append([name, fmt_num(ref[0]), fmt_ci(now_es), fmt_num(ref[1]), fmt_ci(now_nq)])
    s_es = results["ES"]["slices"]["v4_rth"]["score"]["corr"]
    s_nq = results["NQ"]["slices"]["v4_rth"]["score"]["corr"]
    rows.append(
        [
            "skóre (13. 9. v3 všech 12; teď bez CumΔ)",
            fmt_num(REF_0913_SCORE_R30[0]),
            fmt_ci(s_es["score_nocd"][MAIN_H]["pearson"]),
            fmt_num(REF_0913_SCORE_R30[1]),
            fmt_ci(s_nq["score_nocd"][MAIN_H]["pearson"]),
        ]
    )
    add(md_table(["složka", "ES 13. 9.", "ES teď (v4 RTH)", "NQ 13. 9.", "NQ teď (v4 RTH)"], rows))
    add("")
    summary = results["comparison"]
    rows = [[item["metric"], item["then"], item["now"]] for item in summary]
    add(md_table(["metrika", "13. 9. (v3, 7. 8.–11. 9.)", "teď (v4 RTH 22. 9.–16. 10.)"], rows))
    add("")
    return "\n".join(parts) + "\n"


def comparison_rows(results: dict[str, Any]) -> list[dict[str, str]]:
    """Kap. 8: souhrnná čísla 13. 9. × teď."""

    def uni(symbol: str, name: str, h: int = MAIN_H) -> dict[str, Any]:
        return results[symbol]["slices"]["v4_rth"]["univariate"][name][h]["pearson_r"]

    max_abs = {
        symbol: max(abs(uni(symbol, name, h)["r"] or 0.0) for name in COMPONENTS for h in HORIZONS)
        for symbol in SYMBOLS
    }
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
        ]["naive_v4"]
        for symbol in SYMBOLS
    }
    hits = []
    for symbol in SYMBOLS:
        for h in HORIZONS:
            hit = results[symbol]["slices"]["v4_rth"]["score"]["hits"]["band_stored"][h]["hit"]
            if hit is not None:
                hits.append(hit)
    av = {symbol: results[symbol]["slices"]["v4_rth"]["availability"] for symbol in SYMBOLS}
    return [
        {
            "metric": "max. abs. r hlas × r_h (15/30/60)",
            "then": "≤ 0,05 (ES i NQ)",
            "now": f"ES {fmt_num(max_abs['ES'], sign=False)}, "
            f"NQ {fmt_num(max_abs['NQ'], sign=False)}",
        },
        {
            "metric": "walk-forward OLS/ridge OOS r",
            "then": "abs. r ≤ 0,015, znaménko se střídá (4 foldy)",
            "now": f"pooled {fmt_num(min(oos))} … {fmt_num(max(oos))} (ES+NQ, 15/30/60)"
            if oos
            else "—",
        },
        {
            "metric": "naivní váhy OOS r30",
            "then": "v3: ES +0,017 / −0,037, NQ +0,030 / +0,005",
            "now": f"v4 bez CumΔ pooled: ES {fmt_ci(naive['ES'])}, NQ {fmt_ci(naive['NQ'])}",
        },
        {
            "metric": "vanna_flow ES r30",
            "then": "−0,042 [−0,073; −0,011] (všechny minuty)",
            "now": fmt_ci(uni("ES", "vanna_flow")),
        },
        {
            "metric": "vanna_flow NQ r30 (RTH)",
            "then": "−0,070 [−0,124; −0,020]",
            "now": fmt_ci(uni("NQ", "vanna_flow")),
        },
        {
            "metric": "delta_flow NQ r30 (RTH)",
            "then": "−0,055 [−0,091; −0,023]",
            "now": fmt_ci(uni("NQ", "delta_flow")),
        },
        {
            "metric": "gamma_at_price r30 ES / NQ",
            "then": "+0,018 / −0,007 (po odečtení dne −0,034 / −0,054)",
            "now": f"{fmt_ci(uni('ES', 'gamma_at_price'))} / {fmt_ci(uni('NQ', 'gamma_at_price'))}",
        },
        {
            "metric": "flip chybí",
            "then": "49 % minut (všechny minuty)",
            "now": f"RTH ES {fmt_pct(av['ES']['flip_missing'])}, "
            f"NQ {fmt_pct(av['NQ']['flip_missing'])}",
        },
        {
            "metric": "sentindex",
            "then": "mrtvý od 13. 8. (#1136), hodnotit od 13. 9. 21:30",
            "now": f"chybí ES {fmt_pct(av['ES']['components']['sentindex']['missing'])}, "
            f"NQ {fmt_pct(av['NQ']['components']['sentindex']['missing'])}; r30 ES "
            f"{fmt_ci(uni('ES', 'sentindex'))}, NQ {fmt_ci(uni('NQ', 'sentindex'))}",
        },
        {
            "metric": "zásahovost uloženého pásma",
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
) -> dict[str, Any]:
    rows = frame[frame["symbol"] == symbol].sort_values("ts").reset_index(drop=True)
    rows["band_check"] = replay_bands(rows, "score")
    rows["band_nocd_hyst"] = replay_bands(rows, "score_nocd")
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
    checks = {
        "rows": int(rows["trading"].sum()),
        "by_version": {
            int(k): int(v) for k, v in rows.loc[rows["trading"], "wv"].value_counts().items()
        },
        "score_check_max_abs": float(np.nanmax(np.abs(rows["score_check"] - rows["score"]))),
        "band_replay_match": float((rows["band_check"] == rows["band"])[in_rth].mean()),
        "bars_nonlive_share": float((rows.loc[in_rth, "bar_source"] != "ibkr").mean()),
    }
    slices = make_slices(rows, symbol, reps, seed)
    out: dict[str, Any] = {"checks": checks, "slices": {}}
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
            entry["drift"] = drift_check(sl)
        out["slices"][key] = entry
    return out


def multiple_comparisons(results: dict[str, Any]) -> dict[str, Any]:
    counts = {"pearson_r": 0, "spearman_r": 0, "pearson_rs": 0}
    m = 0
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
    return {
        "m": m,
        "expected": 0.05 * m,
        "pearson_hits": counts["pearson_r"],
        "spearman_hits": counts["spearman_r"],
        "sigma_hits": counts["pearson_rs"],
        "p_pearson": binom_tail(m, counts["pearson_r"]) if m else None,
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
    for symbol in SYMBOLS:
        print(f"Bary {symbol} …")
        bars = load_bars(
            data / "derived",
            symbol,
            since - dt.timedelta(hours=3),
            until + dt.timedelta(hours=3),
        )
        results[symbol] = analyze_symbol(frame, bars, symbol, start, end, args.boot, args.seed)
    print("Logy cyklu enginu …")
    results["cycle_offsets"] = cycle_offsets(
        Path(args.logs) if args.logs else data / "logs", start, end
    )
    results["multiple_comparisons"] = multiple_comparisons(results)
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
