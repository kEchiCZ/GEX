"""Kalibrace zdí podle podílu outright objemu (#1019, #1007 krok 4; E-0.7 epiky #1385).

Otázka: drží zdi, na jejichž striku se obchodovalo převážně outright (tisky se
stranou agresora), lépe než zdi postavené ze strukturovaného objemu (legy spreadů,
bloky — ADR-0032 bod 4)? Jen čtení partic `data/derived`, nic se nezapisuje.

Událost = test zdi front expirace (0DTE řetěz seance, partice `derived/{sym}/{den}/`):
- zeď: primárně GEX zdi `levels.call_wall` / `put_wall` (tytéž čte šablona
  wall_bounce, ta navíc vynechá zdi s dominancí pod 0,15), druhotně OI zdi
  `oiwalls.oi_call_wall` / `oi_put_wall`;
- dotek: bar se přiblíží ke zdi na `TOUCH_BP` ze správné strany — call zeď
  zespodu (předchozí close pod zdí − tolerance), put zeď shora; další dotek téže
  strany až po odjezdu od zdi o ≥ `REARM_BP`;
- výsledek do `HORIZON_MIN` minut nad úrovní zdi V OKAMŽIKU DOTEKU, symetricky
  kolem zdi: průraz = close za zdí o ≥ `BARRIER_BP`, drží = close zpět pod zdí
  o ≥ `BARRIER_BP`, co nastane dřív; jinak nerozhodnuto (do podílu nevstupuje);
- **nulová pravděpodobnost** „drží" pro každý dotek z polohy close doteku mezi
  oběma bariérami (náhodná procházka bez driftu: p = vzdálenost k průrazu /
  šířka pásma) — „přebytek drží" = výsledek − p odstraní vliv toho, jak blízko
  zdi dotek zavřel (ten se liší mezi RTH a nocí i mezi skupinami);
- podíl outright: Σ `printed` / Σ `volume_delta` z `printvol` na striku zdi a
  straně zdi (call → C, put → P) od začátku seance do minuty doteku včetně
  (point-in-time); pod `MIN_VOLUME` kontraktů = nízký objem, mimo skupiny;
  řádky s `printed` NULL (výpadek tasty) se nepočítají;
- vynechá se dotek mimo pokrytí `printvol` (před první nebo po poslední minutě
  řady — podíl by byl prázdný nebo zamrzlý) a dotek po settle 0DTE expirace
  (zeď prošlé expirace; setupy po settle nevznikají, #1324).

Analýza (zadání #1019: RTH a noc odděleně): skupiny outright ≥ `OUTRIGHT_SPLIT`
(40 %, návrh #1007) × pod ním, **stratifikované RTH / noc**. Primární číslo je
sdružený rozdíl podílu „drží" high − low s váhami Mantel–Haenszel přes obě vrstvy
a 95% interval z bootstrapu po obchodních dnech (losují se celé dny; ES a NQ téhož
dne jsou korelované). Síto „outright drží lépe" = n ≥ `MIN_SAMPLE` v obou skupinách
a dolní mez sdruženého rozdílu > 0; totéž pro „přebytek drží" jako kontrolu vlivu
polohy doteku. Vrstvy, dominance zdi, ES/NQ a call/put jsou popisné.

Odchylky od plánu session (8. 10.), uvedené i v reportu:
- první běh měl práh „drží" na úrovni doteku (4,5 bp = tolerance doteku) a sloučené
  RTH + noc; ověřovatel ukázal, že tím víc než polovina doteků „držela" už na dalším
  baru a že skupiny se liší složením RTH/noc → bariéry symetricky kolem zdi, nulová
  pravděpodobnost, stratifikace, filtry pokrytí a settle;
- **šířka bariéry 9 bp byla zvolena až po prvním běhu** — proto tabulka citlivosti
  `SENSITIVITY_BP` (4,5 / 6,75 / 9 / 13,5 bp) a varianta bez doteků odmítnutých knotem;
- dělení mediánem (v plánu jako kontrola) vypuštěno — medián podílu je 40,1 %,
  dělení je s prahem 40 % prakticky totožné.

Spuštění (z kořene repa):
    uv run python scripts/measure_wall_outright_1019.py --from 2026-09-21 --to 2026-10-07 \\
        --out report.md
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from gexlens_engine.compute.marketclock import outside_us_rth
from gexlens_engine.compute.settle import expiry_settle_ts, is_trading_session, session_bounds
from gexlens_engine.compute.setupstats import wilson_lower_bound

TOUCH_BP = 4.5
BARRIER_BP = 9.0
#: Šířky bariéry pro tabulku citlivosti (4,5 = práh prvního běhu, 9 = primární)
SENSITIVITY_BP = (4.5, 6.75, 9.0, 13.5)
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
STRATA = ("RTH", "noc")


@dataclass(frozen=True)
class Event:
    symbol: str
    session: dt.date
    source: str
    side: str  # "call" | "put"
    ts: dt.datetime
    wall: float
    outcome: str  # "drží" | "průraz" | "nerozhodnuto"
    #: Nulová pravděpodobnost „drží" z polohy close doteku mezi bariérami
    p_null: float
    outright: float | None  # None = nízký objem
    volume: float
    dominance: float | None
    stratum: str  # "RTH" | "noc"

    @property
    def held(self) -> bool:
        return self.outcome == "drží"


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
    """Řádky printvol seance seřazené podle času: (ts, strike, right, volume_delta, printed)."""

    rows: list[tuple[dt.datetime, float, str, float, float | None]]
    null_rows: int

    @property
    def first(self) -> dt.datetime | None:
        return self.rows[0][0] if self.rows else None

    @property
    def last(self) -> dt.datetime | None:
        return self.rows[-1][0] if self.rows else None

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


@dataclass
class Skipped:
    """Doteky vynechané filtry (počty per důvod)."""

    coverage: int = 0
    after_settle: int = 0


def find_events(
    symbol: str,
    session: dt.date,
    source: str,
    bars: dict[dt.datetime, Bar],
    walls: dict[dt.datetime, tuple[float | None, float | None]],
    dominance: dict[dt.datetime, tuple[float | None, float | None]],
    printvol: PrintVol,
    skipped: Skipped,
    barrier_bp: float = BARRIER_BP,
) -> list[Event]:
    """Doteky zdí a jejich výsledek; zeď = hodnota v minutě doteku (point-in-time).

    Horizont výsledku končí nejpozději v settle 0DTE (po něm je zeď prošlé expirace)."""
    times = sorted(t for t in bars if t in walls)
    settle = expiry_settle_ts(session, None, symbol)
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
            if ts >= settle:
                skipped.after_settle += 1
                continue
            if (
                printvol.first is None
                or printvol.last is None
                or not (printvol.first <= ts <= printvol.last)
            ):
                skipped.coverage += 1
                continue
            barrier = wall * barrier_bp / 1e4
            # Poloha close doteku: kladná = už za zdí (směr průrazu), v jednotkách ceny
            beyond_now = (close - wall) if side == "call" else (wall - close)
            p_null = min(1.0, max(0.0, (barrier - beyond_now) / (2 * barrier)))
            outcome = "nerozhodnuto"
            for later in times[i + 1 :]:
                if (later - ts).total_seconds() > HORIZON_MIN * 60 or later >= settle:
                    break
                c = bars[later][2]
                beyond = (c - wall) if side == "call" else (wall - c)
                if beyond >= barrier:
                    outcome = "průraz"
                    break
                if -beyond >= barrier:
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
                    p_null=p_null,
                    outright=share,
                    volume=volume,
                    dominance=float(dom) if isinstance(dom, int | float) else None,
                    stratum="noc" if outside_us_rth(ts) else "RTH",
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


Metric = Callable[["Event"], float]


def _held(e: Event) -> float:
    return 1.0 if e.held else 0.0


def _excess(e: Event) -> float:
    return _held(e) - e.p_null


def mh_difference(high: Sequence[Event], low: Sequence[Event], metric: Metric) -> float | None:
    """Rozdíl průměru metriky high − low sdružený přes vrstvy RTH/noc (váhy Mantel–Haenszel
    n1·n0/(n1+n0)); vrstva bez obou skupin nevstupuje. None = žádná společná vrstva."""
    num = den = 0.0
    for stratum in STRATA:
        h = [metric(e) for e in high if e.stratum == stratum]
        lo = [metric(e) for e in low if e.stratum == stratum]
        if not h or not lo:
            continue
        weight = len(h) * len(lo) / (len(h) + len(lo))
        num += weight * (sum(h) / len(h) - sum(lo) / len(lo))
        den += weight
    return num / den if den > 0 else None


def bootstrap_mh(
    high: Sequence[Event], low: Sequence[Event], metric: Metric, rng: np.random.Generator
) -> tuple[float, float, int] | None:
    """95% percentilový interval `mh_difference` + počet zahozených losů (žádná vrstva
    s oběma skupinami). Losují se celé obchodní dny — ES a NQ téhož dne jsou korelované."""
    keys = sorted({e.session for e in (*high, *low)})
    by_key: dict[dt.date, tuple[list[Event], list[Event]]] = {k: ([], []) for k in keys}
    for e in high:
        by_key[e.session][0].append(e)
    for e in low:
        by_key[e.session][1].append(e)
    # Součty a počty per seance × skupina × vrstva → bootstrap nad maticemi
    shape = (len(keys), 2, len(STRATA))
    sums = np.zeros(shape)
    counts = np.zeros(shape)
    for i, key in enumerate(keys):
        for g, group in enumerate(by_key[key]):
            for e in group:
                s = STRATA.index(e.stratum)
                sums[i, g, s] += metric(e)
                counts[i, g, s] += 1
    draws = rng.integers(0, len(keys), size=(N_BOOT, len(keys)))
    weights = np.zeros((N_BOOT, len(keys)))
    np.add.at(weights, (np.arange(N_BOOT)[:, None], draws), 1.0)
    s_tot = np.einsum("bk,kgs->bgs", weights, sums)
    n_tot = np.einsum("bk,kgs->bgs", weights, counts)
    with np.errstate(invalid="ignore", divide="ignore"):
        means = s_tot / n_tot
        n1, n0 = n_tot[:, 0, :], n_tot[:, 1, :]
        w = np.where((n1 > 0) & (n0 > 0), n1 * n0 / (n1 + n0), 0.0)
        diff = np.where(w > 0, means[:, 0, :] - means[:, 1, :], 0.0)
        pooled = (w * diff).sum(axis=1) / w.sum(axis=1)
    finite = np.isfinite(pooled)
    if not finite.any():
        return None
    low_q, high_q = np.percentile(pooled[finite], [2.5, 97.5])
    return float(low_q), float(high_q), int((~finite).sum())


# ── Report ─────────────────────────────────────────────────────────


def _pct(value: float) -> str:
    return f"{value * 100:.1f} %".replace(".", ",")


def _pp(value: float) -> str:
    return f"{value * 100:+.1f}".replace(".", ",") + " p. b."


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
    held = sum(1 for e in decided if e.held)
    lo, hi = wilson_interval(held, len(decided))
    return f"{held}/{len(decided)} = {_pct(held / len(decided))} [{_pct(lo)}; {_pct(hi)}]"


def _null(events: Sequence[Event]) -> str:
    decided = _decided(events)
    return _pct(statistics.fmean(e.p_null for e in decided)) if decided else "—"


def _split(events: Sequence[Event]) -> tuple[list[Event], list[Event]]:
    measured = [e for e in _decided(events) if e.outright is not None]
    high = [e for e in measured if e.outright is not None and e.outright >= OUTRIGHT_SPLIT]
    low = [e for e in measured if e.outright is not None and e.outright < OUTRIGHT_SPLIT]
    return high, low


def _newcombe_cell(high: Sequence[Event], low: Sequence[Event]) -> str:
    if not high or not low:
        return "—"
    k_h = sum(1 for e in high if e.held)
    k_l = sum(1 for e in low if e.held)
    diff, lo, hi = newcombe_diff(k_h, len(high), k_l, len(low))
    return f"{_pp(diff)} [{_pp(lo)}; {_pp(hi)}]"


def primary(events: Sequence[Event]) -> tuple[str, bool]:
    high, low = _split(events)
    strata_rows = []
    for stratum in STRATA:
        h = [e for e in high if e.stratum == stratum]
        lo = [e for e in low if e.stratum == stratum]
        strata_rows.append(
            [stratum, _rate(h), _null(h), _rate(lo), _null(lo), _newcombe_cell(h, lo)]
        )
    strata_rows.append(
        [
            "celkem (nestratifikováno)",
            _rate(high),
            _null(high),
            _rate(low),
            _null(low),
            _newcombe_cell(high, low),
        ]
    )
    header = [
        "vrstva",
        "drží — outright ≥ 40 %",
        "nulová p",
        "drží — outright < 40 %",
        "nulová p",
        "rozdíl high − low [Newcombe 95 %]",
    ]
    table = _table(header, strata_rows)
    checks: list[tuple[str, str, bool]] = [
        (
            f"n ≥ {MIN_SAMPLE} v obou skupinách",
            f"{len(high)} / {len(low)}",
            min(len(high), len(low)) >= MIN_SAMPLE,
        )
    ]
    for label, metric in (("drží", _held), ("přebytek drží nad nulovou p", _excess)):
        value, ok = _mh_cell(high, low, metric)
        suffix = " — dolní mez > 0" if metric is _held else " (kontrola)"
        checks.append(
            (
                f"{label}: rozdíl high − low sdružený přes RTH/noc (Mantel–Haenszel), "
                f"bootstrap po dnech 95 %{suffix}",
                value,
                ok,
            )
        )
    sieve = _table(["síto", "hodnota", ""], [[n, v, "✔" if ok else "✘"] for n, v, ok in checks])
    verdict = checks[0][2] and checks[1][2]
    return table + "\n\n" + sieve, verdict


def _mh_cell(high: Sequence[Event], low: Sequence[Event], metric: Metric) -> tuple[str, bool]:
    """„rozdíl [interval]" sdruženého rozdílu a zda je dolní mez nad nulou.

    Každé volání má vlastní generátor se `SEED` — tatáž data dají v celém reportu
    tentýž interval (primární tabulka i řádek citlivosti)."""
    diff = mh_difference(high, low, metric)
    boot = bootstrap_mh(high, low, metric, np.random.default_rng(SEED))
    value = "—" if diff is None else _pp(diff)
    if boot is not None:
        value += f" [{_pp(boot[0])}; {_pp(boot[1])}]"
        if boot[2]:
            value += f" ({boot[2]} losů bez společné vrstvy zahozeno)"
    return value, boot is not None and boot[0] > 0


def sensitivity(by_barrier: dict[float, list[Event]]) -> str:
    """Primární síto pro různé šířky bariéry (citlivost na volbu po prvním běhu) a pro
    primární šířku bez doteků odmítnutých knotem (close doteku už za bariérou „drží")."""
    rows: list[list[object]] = []
    variants = [(f"±{b:g} bp".replace(".", ","), events) for b, events in by_barrier.items()]
    primary_events = by_barrier[BARRIER_BP]
    variants.append(
        (
            f"±{BARRIER_BP:g} bp bez odmítnutí knotem".replace(".", ","),
            [e for e in primary_events if e.p_null < 1.0],
        )
    )
    for label, events in variants:
        high, low = _split(events)
        held, held_ok = _mh_cell(high, low, _held)
        excess, excess_ok = _mh_cell(high, low, _excess)
        rows.append(
            [
                label,
                f"{len(high)} / {len(low)}",
                held + (" ✔" if held_ok else ""),
                excess + (" ✔" if excess_ok else ""),
            ]
        )
    header = ["bariéra", "n high / low", "drží: rozdíl MH [95 %]", "přebytek: rozdíl MH [95 %]"]
    return _table(header, rows)


def breakdown(events: Sequence[Event]) -> str:
    high_all, low_all = _split(events)
    dom_values = [e.dominance for e in (*high_all, *low_all) if e.dominance is not None]
    dom_median = statistics.median(dom_values) if dom_values else None
    groups: list[tuple[str, Callable[[Event], bool]]] = [
        ("ES", lambda e: e.symbol == "ES"),
        ("NQ", lambda e: e.symbol == "NQ"),
        ("call zeď", lambda e: e.side == "call"),
        ("put zeď", lambda e: e.side == "put"),
    ]
    if dom_median is not None:
        groups += [
            (
                f"RTH, dominance ≥ medián {dom_median:.3f}".replace(".", ","),
                lambda e: (
                    e.stratum == "RTH" and e.dominance is not None and e.dominance >= dom_median
                ),
            ),
            (
                "RTH, dominance < medián",
                lambda e: (
                    e.stratum == "RTH" and e.dominance is not None and e.dominance < dom_median
                ),
            ),
        ]
    rows = []
    for name, pick in groups:
        h = [e for e in high_all if pick(e)]
        lo = [e for e in low_all if pick(e)]
        rows.append([name, _rate(h), _rate(lo), _newcombe_cell(h, lo)])
    header = ["", "drží — outright ≥ 40 %", "drží — outright < 40 %", "rozdíl [Newcombe 95 %]"]
    return _table(header, rows)


def report(
    by_barrier: dict[float, list[Event]],
    sessions: list[list[object]],
    skipped: dict[str, Skipped],
) -> str:
    events = by_barrier[BARRIER_BP]
    out = ["## Seance\n"]
    out.append(
        _table(
            ["symbol", "seance", "barů", "printvol od – do (UTC)", "NULL printed", "doteků"],
            sessions,
        )
    )
    for source, *_ in WALL_SOURCES:
        subset = [e for e in events if e.source == source]
        skip = skipped[source]
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
        out.append(
            f"\nVynecháno: mimo pokrytí printvol {skip.coverage}, "
            f"po settle 0DTE {skip.after_settle}."
        )
        measured = [e.outright for e in _decided(subset) if e.outright is not None]
        if len(measured) >= 4:
            quart = statistics.quantiles(measured, n=4)
            out.append(
                f"Podíl outright na zdi při doteku: medián {_pct(statistics.median(measured))}, "
                f"kvartily {_pct(quart[0])} – {_pct(quart[2])}, n = {len(measured)}."
            )
        table, ok = primary(subset)
        verdict = "outright drží lépe — síta splněna" if ok else "síta NEsplněna"
        out.append(f"\n### Primárně: práh 40 %, stratifikováno RTH / noc — {verdict}\n\n{table}")
        out.append("\n### Citlivost na šířku bariéry (✔ = dolní mez > 0)\n")
        out.append(
            sensitivity({b: [e for e in ev if e.source == source] for b, ev in by_barrier.items()})
        )
        out.append("\n### Rozpad (popisný)\n")
        out.append(breakdown(subset))
    return "\n".join(out) + "\n"


def _hm(ts: dt.datetime | None) -> str:
    return "—" if ts is None else f"{ts:%m-%d %H:%M}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--derived", type=Path, default=Path("data/derived"))
    parser.add_argument("--from", dest="date_from", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--to", dest="date_to", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--out", type=Path, required=True, help="výstupní Markdown (UTF-8)")
    args = parser.parse_args()
    by_barrier: dict[float, list[Event]] = {b: [] for b in SENSITIVITY_BP}
    sessions: list[list[object]] = []
    skipped = {source: Skipped() for source, *_ in WALL_SOURCES}
    day = args.date_from
    while day <= args.date_to:
        if is_trading_session(day):
            for symbol in SYMBOLS:
                bars = load_bars(args.derived, symbol, day)
                printvol = load_printvol(args.derived, symbol, day)
                dominance = load_dominance(args.derived, symbol, day)
                found = 0
                if len(bars) >= 1000 and printvol.rows:
                    for source, partition, call_col, put_col in WALL_SOURCES:
                        walls = load_walls(args.derived, symbol, day, partition, call_col, put_col)
                        for barrier in SENSITIVITY_BP:
                            # Filtry nezávisí na bariéře — vynechané se počítají jen jednou
                            skip = skipped[source] if barrier == BARRIER_BP else Skipped()
                            batch = find_events(
                                symbol, day, source, bars, walls, dominance, printvol, skip, barrier
                            )
                            by_barrier[barrier].extend(batch)
                            if barrier == BARRIER_BP:
                                found += len(batch)
                null_share = printvol.null_rows / len(printvol.rows) if printvol.rows else 0.0
                sessions.append(
                    [
                        symbol,
                        day,
                        len(bars),
                        f"{_hm(printvol.first)} – {_hm(printvol.last)}",
                        _pct(null_share),
                        found if len(bars) >= 1000 and printvol.rows else "vynecháno",
                    ]
                )
        day += dt.timedelta(days=1)
    title = f"# Zdi podle podílu outright objemu (#1019) — {args.date_from} – {args.date_to}\n\n"
    params = (
        f"Parametry: dotek {TOUCH_BP} bp, bariéry průraz / drží ±{BARRIER_BP} bp kolem zdi, "
        f"znovu-ozbrojení {REARM_BP} bp, horizont {HORIZON_MIN} min, "
        f"min. objem na striku {MIN_VOLUME:.0f}, práh {_pct(OUTRIGHT_SPLIT)}, "
        f"MIN_SAMPLE {MIN_SAMPLE}, bootstrap {N_BOOT}× po seancích, semínko {SEED}.\n\n"
    )
    args.out.write_text(title + params + report(by_barrier, sessions, skipped), encoding="utf-8")


if __name__ == "__main__":
    main()
