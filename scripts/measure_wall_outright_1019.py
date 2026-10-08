"""Kalibrace zdí podle podílu outright objemu (#1019, #1007 krok 4; E-0.7 epiky #1385).

Otázka: drží zdi, na jejichž striku se obchodovalo převážně outright (tisky se
stranou agresora), lépe než zdi postavené ze strukturovaného objemu (legy spreadů,
bloky — ADR-0032 bod 4)? Jen čtení partic `data/derived`, nic se nezapisuje.

Událost = test zdi front expirace (0DTE řetěz seance, partice `derived/{sym}/{den}/`):
- zeď: primárně GEX zdi `levels.call_wall` / `put_wall` (tytéž čte šablona
  wall_bounce), druhotně OI zdi `oiwalls.oi_call_wall` / `oi_put_wall`;
- dotek: bar se přiblíží ke zdi na `TOUCH_BP` (ES ≈ 3 body = `wall_zone`) ze
  správné strany — call zeď zespodu (předchozí close pod zdí − tolerance), put
  zeď shora; další dotek téže strany až po odjezdu od zdi o ≥ `REARM_BP`;
- výsledek do `HORIZON_MIN` minut nad úrovní zdi V OKAMŽIKU DOTEKU: průraz =
  close za zdí o ≥ `MOVE_BP`, drží = close zpět od zdi o ≥ `MOVE_BP`, co nastane
  dřív; jinak nerozhodnuto (vypisuje se, do podílu nevstupuje);
- podíl outright: Σ `printed` / Σ `volume_delta` z `printvol` na striku zdi a
  straně zdi (call → C, put → P) od začátku seance do minuty doteku včetně
  (point-in-time); pod `MIN_VOLUME` kontraktů = nízký objem, mimo skupiny;
  řádky s `printed` NULL (výpadek tasty) se nepočítají a vypisují.

Skupiny (předregistrováno): podíl outright ≥ `OUTRIGHT_SPLIT` (40 %, návrh #1007)
× pod ním; navíc dělení mediánem jako kontrola. Verdikt „outright drží lépe" jen
když: n ≥ `MIN_SAMPLE` v obou skupinách, Newcombe dolní mez rozdílu podílu „drží"
> 0 a bootstrap po seancích (losují se celé seance) dolní mez > 0. Rozpad ES/NQ,
RTH/noc a call/put je jen popisný. Dominance zdi (`walldom`) se vypisuje per
skupina jako možný zavádějící faktor.

Spuštění (z kořene repa):
    uv run python scripts/measure_wall_outright_1019.py --from 2026-09-21 --to 2026-10-07 \\
        --out report.md
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from gexlens_engine.compute.marketclock import outside_us_rth
from gexlens_engine.compute.settle import is_trading_session, session_bounds
from gexlens_engine.compute.setupstats import wilson_lower_bound

TOUCH_BP = 4.5
MOVE_BP = 4.5
REARM_BP = 15.0
HORIZON_MIN = 30
MIN_VOLUME = 100.0
OUTRIGHT_SPLIT = 0.40
MIN_SAMPLE = 30
N_BOOT = 10_000
SEED = 1019
SYMBOLS = ("ES", "NQ")
#: (high, low, close) minutového baru
Bar = tuple[float, float, float]
#: (název definice, partice, sloupec call zdi, sloupec put zdi)
WALL_SOURCES = (
    ("GEX zdi (levels)", "levels", "call_wall", "put_wall"),
    ("OI zdi (oiwalls)", "oiwalls", "oi_call_wall", "oi_put_wall"),
)


@dataclass(frozen=True)
class Event:
    symbol: str
    session: dt.date
    source: str
    side: str  # "call" | "put"
    ts: dt.datetime
    wall: float
    outcome: str  # "drží" | "průraz" | "nerozhodnuto"
    outright: float | None  # None = nízký objem
    volume: float
    dominance: float | None
    rth: bool


def _utc(ts: dt.datetime) -> dt.datetime:
    return ts.replace(tzinfo=dt.UTC) if ts.tzinfo is None else ts.astimezone(dt.UTC)


def _rows(path: Path, columns: Sequence[str]) -> list[dict[str, object]]:
    if not path.exists():
        return []
    names = pq.read_schema(path).names
    rows: list[dict[str, object]] = pq.read_table(
        path, columns=[c for c in columns if c in names]
    ).to_pylist()
    return rows


def load_bars(derived: Path, symbol: str, session: dt.date) -> dict[dt.datetime, Bar]:
    """ts → (high, low, close) minutových barů futures v hranicích seance."""
    lo, hi = session_bounds(session)
    bars: dict[dt.datetime, Bar] = {}
    for offset in (-1, 0, 1):
        day = session + dt.timedelta(days=offset)
        for row in _rows(
            derived / symbol / "bars" / f"{day}.parquet", ["ts_min", "high", "low", "close"]
        ):
            ts = _utc(row["ts_min"])  # type: ignore[arg-type]
            if lo <= ts < hi and row.get("close") is not None:
                bars[ts] = (float(row["high"]), float(row["low"]), float(row["close"]))  # type: ignore[arg-type]
    return bars


def load_walls(
    derived: Path, symbol: str, session: dt.date, partition: str, call_col: str, put_col: str
) -> dict[dt.datetime, tuple[float | None, float | None]]:
    """ts → (call zeď, put zeď) front expirace (0DTE = expirace rovná dni seance)."""
    lo, hi = session_bounds(session)
    base = derived / symbol / f"{session:%Y%m%d}" / partition
    walls: dict[dt.datetime, tuple[float | None, float | None]] = {}
    for offset in (-1, 0):
        day = session + dt.timedelta(days=offset)
        for row in _rows(base / f"{day}.parquet", ["ts_min", call_col, put_col]):
            ts = _utc(row["ts_min"])  # type: ignore[arg-type]
            if lo <= ts < hi:
                call, put = row.get(call_col), row.get(put_col)
                walls[ts] = (
                    float(call) if call is not None else None,  # type: ignore[arg-type]
                    float(put) if put is not None else None,  # type: ignore[arg-type]
                )
    return walls


def load_dominance(
    derived: Path, symbol: str, session: dt.date
) -> dict[dt.datetime, tuple[float | None, float | None]]:
    lo, hi = session_bounds(session)
    base = derived / symbol / f"{session:%Y%m%d}" / "walldom"
    dom: dict[dt.datetime, tuple[float | None, float | None]] = {}
    for offset in (-1, 0):
        day = session + dt.timedelta(days=offset)
        for row in _rows(base / f"{day}.parquet", ["ts_min", "call_wall_dom", "put_wall_dom"]):
            ts = _utc(row["ts_min"])  # type: ignore[arg-type]
            if lo <= ts < hi:
                dom[ts] = (row.get("call_wall_dom"), row.get("put_wall_dom"))  # type: ignore[assignment]
    return dom


@dataclass
class PrintVol:
    """Řádky printvol seance: (ts, strike, right) → (volume_delta, printed | None)."""

    rows: list[tuple[dt.datetime, float, str, float, float | None]]
    null_rows: int

    def outright(self, strike: float, right: str, until: dt.datetime) -> tuple[float | None, float]:
        """(podíl outright, objem) od začátku seance do `until` včetně; None = nízký objem."""
        volume = printed = 0.0
        for ts, k, r, vol, pr in self.rows:
            if ts > until:
                break
            if k == strike and r == right and pr is not None:
                volume += vol
                printed += pr
        if volume < MIN_VOLUME:
            return None, volume
        return min(1.0, printed / volume), volume


def load_printvol(derived: Path, symbol: str, session: dt.date) -> PrintVol:
    lo, hi = session_bounds(session)
    base = derived / symbol / f"{session:%Y%m%d}" / "printvol"
    rows: list[tuple[dt.datetime, float, str, float, float | None]] = []
    nulls = 0
    for offset in (-1, 0):
        day = session + dt.timedelta(days=offset)
        for row in _rows(
            base / f"{day}.parquet", ["ts_min", "strike", "right", "volume_delta", "printed"]
        ):
            ts = _utc(row["ts_min"])  # type: ignore[arg-type]
            if not lo <= ts < hi:
                continue
            printed = row.get("printed")
            if printed is None:
                nulls += 1
            rows.append(
                (
                    ts,
                    float(row["strike"]),  # type: ignore[arg-type]
                    str(row["right"]),
                    float(row.get("volume_delta") or 0.0),  # type: ignore[arg-type]
                    float(printed) if printed is not None else None,  # type: ignore[arg-type]
                )
            )
    rows.sort(key=lambda r: r[0])
    return PrintVol(rows, nulls)


def find_events(
    symbol: str,
    session: dt.date,
    source: str,
    bars: dict[dt.datetime, Bar],
    walls: dict[dt.datetime, tuple[float | None, float | None]],
    dominance: dict[dt.datetime, tuple[float | None, float | None]],
    printvol: PrintVol,
) -> list[Event]:
    """Doteky zdí a jejich výsledek; zeď = hodnota v minutě doteku (point-in-time)."""
    times = sorted(t for t in bars if t in walls)
    events: list[Event] = []
    for side in ("call", "put"):
        armed = True
        for i, ts in enumerate(times[1:], start=1):
            wall = walls[ts][0 if side == "call" else 1]
            if wall is None or wall <= 0:
                continue
            high, low, close = bars[ts]
            prev_close = bars[times[i - 1]][2]
            tol = wall * TOUCH_BP / 1e4
            away = (wall - close) if side == "call" else (close - wall)
            if not armed:
                if away >= wall * REARM_BP / 1e4:
                    armed = True
                continue
            from_right_side = prev_close < wall - tol if side == "call" else prev_close > wall + tol
            touched = high >= wall - tol if side == "call" else low <= wall + tol
            if not (from_right_side and touched):
                continue
            armed = False
            move = wall * MOVE_BP / 1e4
            outcome = "nerozhodnuto"
            for later in times[i + 1 :]:
                if (later - ts).total_seconds() > HORIZON_MIN * 60:
                    break
                c = bars[later][2]
                beyond = (c - wall) if side == "call" else (wall - c)
                if beyond >= move:
                    outcome = "průraz"
                    break
                if -beyond >= move:
                    outcome = "drží"
                    break
            share, volume = printvol.outright(wall, "C" if side == "call" else "P", ts)
            dom = dominance.get(ts, (None, None))[0 if side == "call" else 1]
            events.append(
                Event(
                    symbol=symbol,
                    session=session,
                    source=source,
                    side=side,
                    ts=ts,
                    wall=wall,
                    outcome=outcome,
                    outright=share,
                    volume=volume,
                    dominance=float(dom) if isinstance(dom, int | float) else None,
                    rth=not outside_us_rth(ts),
                )
            )
    return events


# ── Statistika ─────────────────────────────────────────────────────


def wilson_interval(k: int, n: int) -> tuple[float, float]:
    return wilson_lower_bound(k, n), 1.0 - wilson_lower_bound(n - k, n)


def newcombe_diff(k1: int, n1: int, k2: int, n2: int) -> tuple[float, float, float]:
    p1, p2 = k1 / n1, k2 / n2
    l1, u1 = wilson_interval(k1, n1)
    l2, u2 = wilson_interval(k2, n2)
    diff = p1 - p2
    return (
        diff,
        diff - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2),
        diff + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2),
    )


def bootstrap_diff(
    high: Sequence[Event], low: Sequence[Event], rng: np.random.Generator
) -> tuple[float, float] | None:
    """95% interval rozdílu podílu „drží" (high − low), losují se celé seance."""
    keys = sorted({(e.symbol, e.session) for e in (*high, *low)})
    pos = {k: i for i, k in enumerate(keys)}
    held = np.zeros((2, len(keys)))
    total = np.zeros((2, len(keys)))
    for g, group in enumerate((high, low)):
        for e in group:
            total[g, pos[(e.symbol, e.session)]] += 1
            held[g, pos[(e.symbol, e.session)]] += e.outcome == "drží"
    draws = rng.integers(0, len(keys), size=(N_BOOT, len(keys)))
    weights = np.zeros((N_BOOT, len(keys)))
    np.add.at(weights, (np.arange(N_BOOT)[:, None], draws), 1.0)
    h = weights @ held.T
    t = weights @ total.T
    valid = (t > 0).all(axis=1)
    if not valid.any():
        return None
    diffs = h[valid, 0] / t[valid, 0] - h[valid, 1] / t[valid, 1]
    low_q, high_q = np.percentile(diffs, [2.5, 97.5])
    return float(low_q), float(high_q)


# ── Report ─────────────────────────────────────────────────────────


def _pct(value: float) -> str:
    return f"{value * 100:.1f} %".replace(".", ",")


def _table(header: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def _decided(events: Sequence[Event]) -> list[Event]:
    return [e for e in events if e.outcome != "nerozhodnuto"]


def _rate(events: Sequence[Event]) -> str:
    decided = _decided(events)
    if not decided:
        return "—"
    held = sum(1 for e in decided if e.outcome == "drží")
    lo, hi = wilson_interval(held, len(decided))
    return f"{held}/{len(decided)} = {_pct(held / len(decided))} [{_pct(lo)}; {_pct(hi)}]"


def _dominance(events: Sequence[Event]) -> str:
    values = [e.dominance for e in events if e.dominance is not None]
    return f"{statistics.median(values):.3f}".replace(".", ",") if values else "—"


def compare(events: Sequence[Event], split: float, rng: np.random.Generator) -> tuple[str, bool]:
    measured = [e for e in _decided(events) if e.outright is not None]
    high = [e for e in measured if e.outright is not None and e.outright >= split]
    low = [e for e in measured if e.outright is not None and e.outright < split]
    rows = [
        [f"outright ≥ {_pct(split)}", len(high), _rate(high), _dominance(high)],
        [f"outright < {_pct(split)}", len(low), _rate(low), _dominance(low)],
    ]
    table = _table(["skupina", "n rozhodnutých", "drží [Wilson 95 %]", "medián dominance"], rows)
    if not high or not low:
        return table + "\n\nJedna ze skupin je prázdná.", False
    k_h = sum(1 for e in high if e.outcome == "drží")
    k_l = sum(1 for e in low if e.outcome == "drží")
    diff, lo, hi = newcombe_diff(k_h, len(high), k_l, len(low))
    boot = bootstrap_diff(high, low, rng)
    checks = [
        (
            f"n ≥ {MIN_SAMPLE} v obou skupinách",
            f"{len(high)} / {len(low)}",
            min(len(high), len(low)) >= MIN_SAMPLE,
        ),
        (
            "rozdíl „drží“ high − low, Newcombe 95 % — dolní mez > 0",
            f"{_pct(diff)} [{_pct(lo)}; {_pct(hi)}]",
            lo > 0,
        ),
        (
            "týž rozdíl, bootstrap po seancích — dolní mez > 0",
            "—" if boot is None else f"[{_pct(boot[0])}; {_pct(boot[1])}]",
            boot is not None and boot[0] > 0,
        ),
    ]
    sieve = _table(["síto", "hodnota", ""], [[n, v, "✔" if ok else "✘"] for n, v, ok in checks])
    return table + "\n\n" + sieve, all(ok for _, _, ok in checks)


def report(events: list[Event], sessions: list[tuple[str, dt.date, str]]) -> str:
    rng = np.random.default_rng(SEED)
    out = ["## Seance\n"]
    out.append(_table(["symbol", "seance", "stav"], [[s, d, note] for s, d, note in sessions]))
    for source, *_ in WALL_SOURCES:
        subset = [e for e in events if e.source == source]
        out.append(f"\n## {source}\n")
        counts = [
            [
                outcome,
                sum(1 for e in subset if e.outcome == outcome),
                sum(1 for e in subset if e.outcome == outcome and e.outright is None),
            ]
            for outcome in ("drží", "průraz", "nerozhodnuto")
        ]
        out.append(_table(["výsledek", "doteků", "z toho nízký objem na striku"], counts))
        measured = [e.outright for e in _decided(subset) if e.outright is not None]
        median = statistics.median(measured) if measured else OUTRIGHT_SPLIT
        if measured:
            quart = statistics.quantiles(measured, n=4)
            out.append(
                f"\nPodíl outright na zdi při doteku: medián {_pct(median)}, kvartily "
                f"{_pct(quart[0])} – {_pct(quart[2])}, n = {len(measured)}."
            )
        for label, split in (
            ("předregistrovaný práh", OUTRIGHT_SPLIT),
            ("medián (kontrola)", median),
        ):
            table, ok = compare(subset, split, rng)
            verdict = "outright drží lépe — síta splněna" if ok else "síta NEsplněna"
            out.append(f"\n### Dělení: {label} {_pct(split)} — {verdict}\n\n{table}")
        rows = []
        for name, pick in (
            ("ES", lambda e: e.symbol == "ES"),
            ("NQ", lambda e: e.symbol == "NQ"),
            ("RTH", lambda e: e.rth),
            ("noc", lambda e: not e.rth),
            ("call zeď", lambda e: e.side == "call"),
            ("put zeď", lambda e: e.side == "put"),
        ):
            part = [e for e in subset if pick(e) and e.outright is not None]
            hi_part = [e for e in part if e.outright is not None and e.outright >= OUTRIGHT_SPLIT]
            lo_part = [e for e in part if e.outright is not None and e.outright < OUTRIGHT_SPLIT]
            rows.append(
                [name, _rate(hi_part), _rate(lo_part), _rate([e for e in subset if pick(e)])]
            )
        out.append("\n### Rozpad (popisný, práh 40 %)\n")
        out.append(
            _table(
                ["", "drží — outright ≥ 40 %", "drží — outright < 40 %", "drží — všechny doteky"],
                rows,
            )
        )
    return "\n".join(out) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--derived", type=Path, default=Path("data/derived"))
    parser.add_argument("--from", dest="date_from", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--to", dest="date_to", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--out", type=Path, required=True, help="výstupní Markdown (UTF-8)")
    args = parser.parse_args()
    events: list[Event] = []
    sessions: list[tuple[str, dt.date, str]] = []
    day = args.date_from
    while day <= args.date_to:
        if is_trading_session(day):
            for symbol in SYMBOLS:
                bars = load_bars(args.derived, symbol, day)
                printvol = load_printvol(args.derived, symbol, day)
                dominance = load_dominance(args.derived, symbol, day)
                if len(bars) < 1000 or not printvol.rows:
                    sessions.append(
                        (
                            symbol,
                            day,
                            f"vynecháno: barů {len(bars)}, printvol řádků {len(printvol.rows)}",
                        )
                    )
                    continue
                found = 0
                for source, partition, call_col, put_col in WALL_SOURCES:
                    walls = load_walls(args.derived, symbol, day, partition, call_col, put_col)
                    batch = find_events(symbol, day, source, bars, walls, dominance, printvol)
                    events.extend(batch)
                    found += len(batch)
                null_share = printvol.null_rows / len(printvol.rows)
                sessions.append(
                    (
                        symbol,
                        day,
                        f"barů {len(bars)}, doteků {found}, printvol NULL {_pct(null_share)}",
                    )
                )
        day += dt.timedelta(days=1)
    title = f"# Zdi podle podílu outright objemu (#1019) — {args.date_from} – {args.date_to}\n\n"
    params = (
        f"Parametry: dotek {TOUCH_BP} bp, průraz / drží {MOVE_BP} bp, "
        f"znovu-ozbrojení {REARM_BP} bp, horizont {HORIZON_MIN} min, "
        f"min. objem na striku {MIN_VOLUME:.0f}, práh {_pct(OUTRIGHT_SPLIT)}, "
        f"MIN_SAMPLE {MIN_SAMPLE}, bootstrap {N_BOOT}× po seancích, semínko {SEED}.\n\n"
    )
    args.out.write_text(title + params + report(events, sessions), encoding="utf-8")


if __name__ == "__main__":
    main()
