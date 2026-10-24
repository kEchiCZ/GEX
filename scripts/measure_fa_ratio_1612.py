"""Rozbor poměrů ΔOI/net kalibrace α (#1612) — jen čtení, do DB ani do `data/` nic nepíše.

Kalibrace α (`storage/fa_calibration.collect_alpha_calibration`, ADR-0011 fáze 2) bere
medián poměrů ΔOI/net přes strany s |net| ≥ 25 a bod s mediánem ≤ 0 α nemění. U NQ
vychází od 1. 10. 2026 většina bodů ≤ 0 (#1605, oddíl „Co oprava nevyřeší“). Skript
rozloží strany každého bodu D tak, aby šly ověřit hypotézy H1–H7 z #1612:

- **bod D** = řetěz, který byl celou seanci D sekundárem (první expirace po D, jako
  `_calibrated_chain` pod v6), ΔOI jen ze stran změřených po publikaci v D i D+1
  (`measured_oi_pair`, varianta B+), net konce dne z `netflow` (`netflow_at_cutoff`,
  řez 16:00 CT). Medián spočítá `calibrate_alpha` — tytéž funkce jako engine, bez kopie
  vzorce. U ES se navíc přepočte řetěz uložený ve `fa_alpha_history` (pod v5 bere D+2,
  #1580);
- **rozpad stran**: znaménko netu × znaménko ΔOI (H7: platí model ΔOI ≈ α·net
  znaménkem?), vzdálenost striku od ceny futures v řezu (H1), net ze strany agresora
  z tisků `data/trades` za celou seanci do řezu (H2, ADR-0057 bod 1: BUY +size,
  SELL −size, tisk bez strany mimo), |net| proti objemu snímku (H3), řez dne 16:00 CT
  a ± 1 h (H4; v létě 20:00 / 21:00 / 22:00 UTC), pátek × všední den (H5), do 30. 9.
  × od 1. 10. (H6).

Expirace tisku se bere z třídy streamer symbolu (`./Q1BV26C29000:XCME` → třída Q1B,
měsíc V, rok 26) přes mapu třída → expirace z `oi_eod` (tvrdá data, ne pravidlo
kalendáře). PostgreSQL se otevírá v read-only transakci.

Spuštění (z kořene repa, `data` = produkční adresář; URL DB jen z proměnné prostředí):
    python -I scripts/measure_fa_ratio_1612.py --data <data> --from 2026-09-25 --out <adresář>
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import re
import statistics
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "engine" / "src"))

import pyarrow.parquet as pq  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402

from gexlens_engine.compute.facalibration import MIN_ABS_NET, calibrate_alpha  # noqa: E402
from gexlens_engine.compute.marketclock import daily_close_utc  # noqa: E402
from gexlens_engine.compute.settle import is_trading_session, session_bounds  # noqa: E402
from gexlens_engine.config import Settings  # noqa: E402
from gexlens_engine.storage.fa_calibration import (  # noqa: E402
    fa_alpha_history_table,
    netflow_at_cutoff,
)
from gexlens_engine.storage.fa_validation import measured_oi_pair  # noqa: E402
from gexlens_engine.storage.oi_archive import OIEodRepository, oi_eod_table  # noqa: E402
from gexlens_engine.storage.parquet_store import read_netflow_latest  # noqa: E402

Key = tuple[float, str]

MONTH_CODES = "FGHJKMNQUVXZ"
STREAMER_RE = re.compile(r"^\./([A-Z0-9]+?)([FGHJKMNQUVXZ])(\d\d)([CP])(\d+(?:\.\d+)?):XCME$")
#: Řezy citlivosti H4 v hodinách od řezu dne 16:00 CT (`daily_close_utc`): v létě
#: 20:00 / 21:00 / 22:00 UTC, od konce US letního času o hodinu později
CUTOFF_OFFSETS_H = (-1, 0, 1)
#: Hranice období H6: zlom kolem 1. 10. 2026
BREAK_DAY = dt.date(2026, 10, 1)
#: Pásma vzdálenosti striku od ceny futures v řezu (|K − S| / S), H1
DISTANCE_BUCKETS = ((0.0, 0.0025), (0.0025, 0.005), (0.005, 0.01), (0.01, 1.0))


@dataclass(frozen=True)
class Side:
    """Jedna strana (strike, right) bodu D se změřeným ΔOI v D i D+1."""

    strike: float
    right: str
    doi: float
    oi_before: float
    #: net z netflow podle řezu (posun v hodinách od řezu dne → net, 0 = řez 16:00 CT);
    #: chybí = strana bez řádku do řezu
    net: Mapping[int, float]
    #: tisky ze strany agresora za seanci do řezu (kontrakty); None = bez tisků dne
    buy: float | None
    sell: float | None
    #: kumulativní objem snímku v řezu a maximum seance do řezu (#1602 kmitání)
    volume_last: float | None
    volume_max: float | None

    @property
    def aggressor_net(self) -> float | None:
        if self.buy is None or self.sell is None:
            return None
        return self.buy - self.sell


@dataclass
class DayPoint:
    """Bod D jednoho symbolu a jednoho řetězu."""

    symbol: str
    day: dt.date
    next_day: dt.date
    expiry: str
    role: str  # "D+1" (sekundár celé seance) nebo "uložený" (řetěz z fa_alpha_history)
    spot: float | None
    sides: list[Side] = field(default_factory=list)
    stored_median: float | None = None
    stored_samples: int | None = None
    prints_available: bool = False


@dataclass(frozen=True)
class GroupStats:
    """Souhrn skupiny stran pro jeden zdroj netu (rozpad H1–H7)."""

    samples: int
    median: float | None
    ratio_buy: float | None
    ratio_sell: float | None
    buy_sides: int
    sell_sides: int
    #: podíl stran s ΔOI > 0 mezi buy a sell stranami
    up_buy: float | None
    up_sell: float | None
    #: medián ΔOI/|net| — poměr, kdyby OI rostl s aktivitou bez ohledu na znaménko
    unsigned_median: float | None


def _median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def _share(flags: Sequence[bool]) -> float | None:
    return sum(flags) / len(flags) if flags else None


def group_stats(
    pairs: Iterable[tuple[float, float]], min_abs_net: float = MIN_ABS_NET
) -> GroupStats:
    """Souhrn dvojic (net, ΔOI); strany pod prahem |net| se nepočítají (jako kalibrace)."""
    ratios: list[float] = []
    buy: list[float] = []
    sell: list[float] = []
    up_buy: list[bool] = []
    up_sell: list[bool] = []
    unsigned: list[float] = []
    for net, doi in pairs:
        if abs(net) < min_abs_net:
            continue
        ratio = doi / net
        ratios.append(ratio)
        unsigned.append(doi / abs(net))
        if net > 0:
            buy.append(ratio)
            up_buy.append(doi > 0)
        else:
            sell.append(ratio)
            up_sell.append(doi > 0)
    return GroupStats(
        samples=len(ratios),
        median=_median(ratios),
        ratio_buy=_median(buy),
        ratio_sell=_median(sell),
        buy_sides=len(buy),
        sell_sides=len(sell),
        up_buy=_share(up_buy),
        up_sell=_share(up_sell),
        unsigned_median=_median(unsigned),
    )


def midpoint_pairs(sides: Iterable[Side], offset: int = 0) -> list[tuple[float, float]]:
    return [(side.net[offset], side.doi) for side in sides if offset in side.net]


def aggressor_pairs(sides: Iterable[Side]) -> list[tuple[float, float]]:
    pairs: list[tuple[float, float]] = []
    for side in sides:
        net = side.aggressor_net
        if net is not None:
            pairs.append((net, side.doi))
    return pairs


def two_factor_fit(
    pairs: Iterable[tuple[float, float]], min_abs_net: float = MIN_ABS_NET
) -> tuple[float, float] | None:
    """Nejmenší čtverce ΔOI = b·net⁺ + s·|net⁻| bez absolutního členu.

    Buy a sell strany se nepřekrývají (strana má jedno znaménko netu), takže se fit
    rozpadne na dvě nezávislé regrese: b = Σ(ΔOI·net)/Σnet² přes buy strany, s totéž
    s |net| přes sell strany. Kladné s = čistý prodej OI zvedá, model ΔOI ≈ α·net
    předpokládá opak (s < 0).
    """
    num_b = den_b = num_s = den_s = 0.0
    for net, doi in pairs:
        if abs(net) < min_abs_net:
            continue
        if net > 0:
            num_b += doi * net
            den_b += net * net
        else:
            num_s += doi * -net
            den_s += net * net
    if den_b <= 0.0 or den_s <= 0.0:
        return None
    return num_b / den_b, num_s / den_s


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


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Spearmanova korelace (průměrná pořadí remíz); None pod 3 body nebo bez rozptylu."""
    if len(xs) < 3:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mean_x, mean_y = statistics.fmean(rx), statistics.fmean(ry)
    cov = sum((a - mean_x) * (b - mean_y) for a, b in zip(rx, ry, strict=True))
    var_x = sum((a - mean_x) ** 2 for a in rx)
    var_y = sum((b - mean_y) ** 2 for b in ry)
    if var_x <= 0.0 or var_y <= 0.0:
        return None
    return float(cov / (var_x * var_y) ** 0.5)


def distance_bucket(strike: float, spot: float) -> int:
    """Index pásma `DISTANCE_BUCKETS` podle |K − S| / S."""
    distance = abs(strike - spot) / spot
    for index, (low, high) in enumerate(DISTANCE_BUCKETS):
        if low <= distance < high:
            return index
    return len(DISTANCE_BUCKETS) - 1


def parse_streamer(streamer: str) -> tuple[str, int, int, str, float] | None:
    """`./Q1BV26C29000:XCME` → (třída Q1B, měsíc 10, rok 2026, C, 29000.0)."""
    match = STREAMER_RE.match(streamer)
    if match is None:
        return None
    root_class, month_code, yy, right, strike = match.groups()
    return root_class, MONTH_CODES.index(month_code) + 1, 2000 + int(yy), right, float(strike)


# --- IO -------------------------------------------------------------------------


def class_expiry_map(engine: Engine, symbol: str) -> dict[tuple[str, int, int], str]:
    """(třída, měsíc, rok) → expirace z `oi_eod` (řádky s vyplněnou třídou, #736)."""
    stmt = (
        select(oi_eod_table.c.trading_class, oi_eod_table.c.expiry)
        .where(oi_eod_table.c.symbol == symbol, oi_eod_table.c.trading_class != "")
        .distinct()
    )
    mapping: dict[tuple[str, int, int], str] = {}
    with engine.connect() as conn:
        for row in conn.execute(stmt):
            expiry = str(row.expiry)
            key = (str(row.trading_class), int(expiry[4:6]), int(expiry[:4]))
            if mapping.get(key, expiry) != expiry:
                raise RuntimeError(
                    f"Třída {key} má v oi_eod dvě expirace: {mapping[key]}, {expiry}"
                )
            mapping[key] = expiry
    return mapping


def aggressor_volumes(
    trades_dir: Path,
    day: dt.date,
    expiry: str,
    mapping: Mapping[tuple[str, int, int], str],
    end: dt.datetime,
) -> dict[Key, tuple[float, float]] | None:
    """(buy, sell) tisků expirace za seanci `day` do `end`; None = recorder bez tisků dne.

    Seance začíná v 17:00 CT předchozího dne, takže leží ve dvou UTC particích.
    """
    start = session_bounds(day)[0]
    totals: dict[Key, list[float]] = defaultdict(lambda: [0.0, 0.0])
    seen = False
    for part in sorted({start.date(), day}):
        path = trades_dir / f"{part.isoformat()}.parquet"
        if not path.exists():
            continue
        table = pq.read_table(path, columns=["ts", "streamer_symbol", "size", "aggressor"])
        columns = [
            table.column(name).to_pylist()
            for name in ("ts", "streamer_symbol", "size", "aggressor")
        ]
        for ts, streamer, size, aggressor in zip(*columns, strict=True):
            if ts is None or ts < start or ts >= end:
                continue
            seen = True
            parsed = parse_streamer(str(streamer))
            if parsed is None or size is None:
                continue
            root_class, month, year, right, strike = parsed
            if mapping.get((root_class, month, year)) != expiry:
                continue
            side = str(aggressor).upper() if aggressor is not None else ""
            if side == "BUY":
                totals[(strike, right)][0] += float(size)
            elif side == "SELL":
                totals[(strike, right)][1] += float(size)
    if not seen:
        return None
    return {key: (buy, sell) for key, (buy, sell) in totals.items()}


def snapshot_volumes(
    snapshots_dir: Path, day: dt.date, end: dt.datetime
) -> dict[Key, tuple[float, float]]:
    """(objem v řezu, maximum seance do řezu) per strana z kumulativního objemu snímků."""
    start = session_bounds(day)[0]
    latest: dict[Key, tuple[dt.datetime, float]] = {}
    peak: dict[Key, float] = {}
    for part in sorted({start.date(), day}):
        path = snapshots_dir / f"{part.isoformat()}.parquet"
        if not path.exists():
            continue
        table = pq.read_table(path, columns=["ts_min", "strike", "right", "volume"])
        columns = [
            table.column(name).to_pylist() for name in ("ts_min", "strike", "right", "volume")
        ]
        for ts, strike, right, volume in zip(*columns, strict=True):
            if ts is None or volume is None or ts < start or ts > end:
                continue
            key = (float(strike), str(right))
            current = latest.get(key)
            if current is None or ts >= current[0]:
                latest[key] = (ts, float(volume))
            peak[key] = max(peak.get(key, 0.0), float(volume))
    return {key: (value, peak[key]) for key, (_, value) in latest.items()}


def spot_at(bars_dir: Path, day: dt.date, end: dt.datetime) -> float | None:
    """Close posledního baru futures před `end`."""
    path = bars_dir / f"{day.isoformat()}.parquet"
    if not path.exists():
        return None
    table = pq.read_table(path, columns=["ts_min", "close"])
    best: tuple[dt.datetime, float] | None = None
    for ts, close in zip(
        table.column("ts_min").to_pylist(), table.column("close").to_pylist(), strict=True
    ):
        if ts is None or close is None or ts >= end:
            continue
        if best is None or ts > best[0]:
            best = (ts, float(close))
    return best[1] if best else None


def stored_points(engine: Engine, symbol: str) -> dict[dt.date, tuple[str, float, int]]:
    """Body `fa_alpha_history` symbolu: den → (expirace, medián, počet stran)."""
    stmt = select(
        fa_alpha_history_table.c.day,
        fa_alpha_history_table.c.expiry,
        fa_alpha_history_table.c.ratio_median,
        fa_alpha_history_table.c.samples,
    ).where(fa_alpha_history_table.c.symbol == symbol)
    with engine.connect() as conn:
        return {
            row.day: (str(row.expiry), float(row.ratio_median), int(row.samples))
            for row in conn.execute(stmt)
        }


def collect_point(
    symbol: str,
    day: dt.date,
    next_day: dt.date,
    expiry: str,
    role: str,
    settings: Settings,
    oi_repo: OIEodRepository,
    mapping: Mapping[tuple[str, int, int], str],
) -> DayPoint | None:
    """Strany bodu D řetězu `expiry`; None = bez netflow partice nebo bez změřených stran."""
    netflow_path = settings.derived_dir / symbol / expiry / "netflow" / f"{day.isoformat()}.parquet"
    if not netflow_path.exists():
        return None
    oi_before, oi_after, _ = measured_oi_pair(
        oi_repo, symbol, expiry, next_day, day, settings.oi_publication_utc
    )
    doi = {key: oi_after[key] - oi_before[key] for key in oi_after.keys() & oi_before.keys()}
    if not doi:
        return None
    cutoff = daily_close_utc(day)
    nets: dict[int, dict[Key, float]] = {}
    for offset in CUTOFF_OFFSETS_H:
        moment = cutoff + dt.timedelta(hours=offset)
        if offset == 0:
            nets[offset] = netflow_at_cutoff(netflow_path, day)  # tatáž funkce jako kalibrace
        else:
            nets[offset] = read_netflow_latest(
                netflow_path, end=moment + dt.timedelta(minutes=1), sources=None
            )
    prints = aggressor_volumes(settings.trades_dir / symbol, day, expiry, mapping, cutoff)
    volumes = snapshot_volumes(settings.snapshots_dir / symbol / expiry, day, cutoff)
    point = DayPoint(
        symbol=symbol,
        day=day,
        next_day=next_day,
        expiry=expiry,
        role=role,
        spot=spot_at(settings.derived_dir / symbol / "bars", day, cutoff),
        prints_available=prints is not None,
    )
    for key in sorted(doi):
        buy_sell = (prints or {}).get(key, (0.0, 0.0)) if prints is not None else None
        volume = volumes.get(key)
        point.sides.append(
            Side(
                strike=key[0],
                right=key[1],
                doi=doi[key],
                oi_before=oi_before[key],
                net={offset: values[key] for offset, values in nets.items() if key in values},
                buy=buy_sell[0] if buy_sell is not None else None,
                sell=buy_sell[1] if buy_sell is not None else None,
                volume_last=volume[0] if volume else None,
                volume_max=volume[1] if volume else None,
            )
        )
    return point


def collect_symbol(
    symbol: str,
    settings: Settings,
    engine: Engine,
    first: dt.date,
    last: dt.date | None,
) -> list[DayPoint]:
    """Body D symbolu od `first` (do `last`) pro řetěz D+1 a případně uložený řetěz."""
    oi_repo = OIEodRepository(engine)
    mapping = class_expiry_map(engine, symbol)
    stored = stored_points(engine, symbol)
    days = sorted(day for day in oi_repo.days(symbol) if is_trading_session(day))
    points: list[DayPoint] = []
    base = settings.derived_dir / symbol
    chains = sorted(
        p.name for p in base.iterdir() if p.is_dir() and p.name.isdigit() and len(p.name) == 8
    )
    for day, next_day in zip(days, days[1:], strict=False):
        if day < first or (last is not None and day > last):
            continue
        secondary = next(
            (c for c in chains if dt.datetime.strptime(c, "%Y%m%d").date() > day), None
        )
        if secondary is None:
            continue
        wanted = [(secondary, "D+1")]
        stored_point = stored.get(day)
        if stored_point is not None and stored_point[0] != secondary:
            wanted.append((stored_point[0], "uložený"))
        for expiry, role in wanted:
            point = collect_point(symbol, day, next_day, expiry, role, settings, oi_repo, mapping)
            if point is None:
                continue
            if stored_point is not None and stored_point[0] == expiry:
                point.stored_median, point.stored_samples = stored_point[1], stored_point[2]
            points.append(point)
    return points


# --- report -----------------------------------------------------------------------


def _f(value: float | None, digits: int = 3) -> str:
    return "—" if value is None else f"{value:.{digits}f}".replace(".", ",")


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{100 * value:.0f} %"


def _stats_cells(stats: GroupStats) -> list[str]:
    return [
        str(stats.samples),
        _f(stats.median),
        f"{_f(stats.ratio_buy)} ({stats.buy_sides})",
        f"{_f(stats.ratio_sell)} ({stats.sell_sides})",
        _pct(stats.up_buy),
        _pct(stats.up_sell),
        _f(stats.unsigned_median),
    ]


STATS_HEADER = "stran | medián | buy (n) | sell (n) | ΔOI>0 buy | ΔOI>0 sell | ΔOI/\\|net\\|"


def _table(header: str, rows: Iterable[list[str]]) -> list[str]:
    columns = len(re.split(r"(?<!\\)\|", header))
    lines = [f"| {header} |", "|" + "---|" * columns]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return lines


def _period(day: dt.date) -> str:
    return "do 30. 9." if day < BREAK_DAY else "od 1. 10."


WEEKDAYS = ("po", "út", "st", "čt", "pá", "so", "ne")


def report_symbol(symbol: str, points: Sequence[DayPoint]) -> list[str]:
    main = [p for p in points if p.role == "D+1"]
    lines = [f"## {symbol}", ""]
    if not main:
        return [*lines, "- žádný bod D se změřenými stranami", ""]

    # 1. Body D
    lines += ["### Body D (řetěz D+1, net midpoint v řezu dne 16:00 CT)", ""]
    rows = []
    for p in main:
        stats = group_stats(midpoint_pairs(p.sides))
        engine_point = calibrate_alpha(
            {(s.strike, s.right): s.net[0] for s in p.sides if 0 in s.net},
            {(s.strike, s.right): s.doi for s in p.sides},
        )
        check = "" if engine_point is None else _f(engine_point.ratio_median)
        stored = "—" if p.stored_median is None else f"{_f(p.stored_median)} ({p.stored_samples})"
        share_sell = stats.sell_sides / stats.samples if stats.samples else None
        rows.append(
            [
                f"{p.day:%d. %m.} {WEEKDAYS[p.day.weekday()]}",
                p.expiry,
                *_stats_cells(stats),
                _pct(share_sell),
                check,
                stored,
            ]
        )
    lines += _table(
        f"bod D | expirace | {STATS_HEADER} | podíl sell | calibrate_alpha | uložený (n)", rows
    )
    lines.append("")

    # H7: znaménko netu × ΔOI, souhrn po obdobích
    lines += ["### H7 — znaménko netu × ΔOI (souhrn stran)", ""]
    rows = []
    by_period: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for p in main:
        by_period[_period(p.day)] += midpoint_pairs(p.sides)
        by_period["celkem"] += midpoint_pairs(p.sides)
    for label in ("do 30. 9.", "od 1. 10.", "celkem"):
        pairs = by_period.get(label, [])
        stats = group_stats(pairs)
        fit = two_factor_fit(pairs)
        kept = [(n, d) for n, d in pairs if abs(n) >= MIN_ABS_NET]
        rho_net = spearman([n for n, _ in kept], [d for _, d in kept])
        rho_abs = spearman([abs(n) for n, _ in kept], [d for _, d in kept])
        rows.append(
            [
                label,
                *_stats_cells(stats),
                "—" if fit is None else f"{_f(fit[0])} / {_f(fit[1])}",
                _f(rho_net, 2),
                _f(rho_abs, 2),
            ]
        )
    lines += _table(f"období | {STATS_HEADER} | fit b / s | ρ(ΔOI, net) | ρ(ΔOI, \\|net\\|)", rows)
    day_share = [(group_stats(midpoint_pairs(p.sides))) for p in main]
    usable = [s for s in day_share if s.samples and s.median is not None]
    rho_mix = spearman(
        [s.sell_sides / s.samples for s in usable], [s.median or 0.0 for s in usable]
    )
    lines += [
        "",
        f"- ρ(podíl sell stran dne, medián dne) přes {len(usable)} bodů: {_f(rho_mix, 2)}",
        "",
    ]

    # H1: vzdálenost od ceny
    lines += ["### H1 — vzdálenost striku od ceny futures v řezu", ""]
    rows = []
    for period in ("do 30. 9.", "od 1. 10.", "celkem"):
        buckets: dict[int, list[tuple[float, float]]] = defaultdict(list)
        for p in main:
            if p.spot is None or (period != "celkem" and _period(p.day) != period):
                continue
            for side in p.sides:
                if 0 in side.net:
                    buckets[distance_bucket(side.strike, p.spot)].append((side.net[0], side.doi))
        for index, (low, high) in enumerate(DISTANCE_BUCKETS):
            label = f"{100 * low:.2f}–{100 * high:.2f} %" if high < 1.0 else f"≥ {100 * low:.2f} %"
            rows.append(
                [
                    period,
                    label.replace(".", ","),
                    *_stats_cells(group_stats(buckets.get(index, []))),
                ]
            )
    lines += _table(f"období | \\|K − S\\| / S | {STATS_HEADER}", rows)
    lines.append("")

    # H2: midpoint × agresor
    lines += ["### H2 — net midpoint × net ze strany agresora (tisky za seanci do řezu)", ""]
    rows = []
    pooled_aggr: dict[str, list[tuple[float, float]]] = defaultdict(list)
    agreement_all: list[bool] = []
    for p in main:
        if not p.prints_available:
            rows.append([f"{p.day:%d. %m.}", "bez tisků", "", "", "", "", "", "", "", ""])
            continue
        aggr = aggressor_pairs(p.sides)
        pooled_aggr[_period(p.day)] += aggr
        pooled_aggr["celkem"] += aggr
        agreement = [
            (side.net[0] > 0) == ((side.aggressor_net or 0.0) > 0)
            for side in p.sides
            if 0 in side.net
            and abs(side.net[0]) >= MIN_ABS_NET
            and side.aggressor_net is not None
            and side.aggressor_net != 0.0
        ]
        agreement_all += agreement
        printed = sum((s.buy or 0.0) + (s.sell or 0.0) for s in p.sides)
        volume = sum(s.volume_max or 0.0 for s in p.sides)
        stats = group_stats(aggr)
        rows.append(
            [
                f"{p.day:%d. %m.} {WEEKDAYS[p.day.weekday()]}",
                *_stats_cells(stats),
                _pct(_share(agreement)),
                _pct(printed / volume if volume else None),
            ]
        )
    lines += _table(f"bod D | {STATS_HEADER} | shoda znaménka s midpointem | tisky / objem", rows)
    lines.append("")
    rows = []
    for label in ("do 30. 9.", "od 1. 10.", "celkem"):
        pairs = pooled_aggr.get(label, [])
        fit = two_factor_fit(pairs)
        rows.append(
            [
                label,
                *_stats_cells(group_stats(pairs)),
                "—" if fit is None else f"{_f(fit[0])} / {_f(fit[1])}",
            ]
        )
    lines += _table(f"agresor, období | {STATS_HEADER} | fit b / s", rows)
    lines += [
        "",
        "- shoda znaménka netu midpoint × agresor přes všechny strany: "
        f"{_pct(_share(agreement_all))} z {len(agreement_all)}",
        "",
    ]

    # H3: |net| proti objemu
    lines += ["### H3 — |net midpoint| proti objemu snímku (maximum seance do řezu)", ""]
    rows = []
    for period in ("do 30. 9.", "od 1. 10.", "celkem"):
        within: list[tuple[float, float]] = []
        above: list[tuple[float, float]] = []
        share_net: list[float] = []
        for p in main:
            if period != "celkem" and _period(p.day) != period:
                continue
            for side in p.sides:
                if 0 not in side.net or abs(side.net[0]) < MIN_ABS_NET or not side.volume_max:
                    continue
                share_net.append(abs(side.net[0]) / side.volume_max)
                (within if abs(side.net[0]) <= side.volume_max else above).append(
                    (side.net[0], side.doi)
                )
        rows.append(
            [
                period,
                "\\|net\\| ≤ objem",
                *_stats_cells(group_stats(within)),
                _f(_median(share_net), 2),
            ]
        )
        rows.append([period, "\\|net\\| > objem", *_stats_cells(group_stats(above)), ""])
    lines += _table(f"období | strany | {STATS_HEADER} | medián \\|net\\|/objem", rows)
    lines.append("")

    # H4: citlivost řezu
    lines += ["### H4 — citlivost na řez (net z netflow do řezu dne 16:00 CT ± 1 h)", ""]
    rows = []
    for p in main:
        cells = [f"{p.day:%d. %m.} {WEEKDAYS[p.day.weekday()]}"]
        for offset in CUTOFF_OFFSETS_H:
            stats = group_stats(midpoint_pairs(p.sides, offset))
            cells.append(f"{_f(stats.median)} ({stats.samples})")
        flips = shrunk = 0
        for side in p.sides:
            if (
                -1 in side.net
                and 0 in side.net
                and max(abs(side.net[-1]), abs(side.net[0])) >= MIN_ABS_NET
            ):
                if side.net[-1] * side.net[0] < 0:
                    flips += 1
                if abs(side.net[0]) < 0.5 * abs(side.net[-1]):
                    shrunk += 1
        cells += [str(flips), str(shrunk)]
        rows.append(cells)
    lines += _table(
        "bod D | řez − 1 h | řez | řez + 1 h | změna znaménka −1 h → řez "
        "| \\|net řez\\| < ½ \\|net −1 h\\|",
        rows,
    )
    lines.append("")

    # H5: pátek × všední den
    lines += ["### H5 — pátek (D+1 = pondělní expirace) × všední den", ""]
    rows = []
    for label, friday in (("pátek", True), ("po–čt", False)):
        selected = [p for p in main if (p.day.weekday() == 4) == friday]
        pairs = [pair for p in selected for pair in midpoint_pairs(p.sides)]
        medians = [
            m
            for m in (group_stats(midpoint_pairs(p.sides)).median for p in selected)
            if m is not None
        ]
        positive = sum(1 for m in medians if m > 0)
        rows.append([label, f"{positive} z {len(medians)}", *_stats_cells(group_stats(pairs))])
    lines += _table(f"dny | bodů s mediánem > 0 | {STATS_HEADER}", rows)
    lines.append("")

    # Stabilita kandidátních veličin po dnech (podklad variant modelu)
    lines += ["### Stabilita veličin po dnech (kandidáti místo ΔOI/net)", ""]
    rows = []
    series: dict[str, list[float]] = defaultdict(list)
    for p in main:
        signed = group_stats(midpoint_pairs(p.sides)).median
        unsigned = group_stats(midpoint_pairs(p.sides)).unsigned_median
        unsigned_aggr = (
            group_stats(aggressor_pairs(p.sides)).unsigned_median if p.prints_available else None
        )
        per_volume = _median(
            [s.doi / s.volume_max for s in p.sides if s.volume_max and s.volume_max >= MIN_ABS_NET]
        )
        for label, value in (
            ("ΔOI/net", signed),
            ("ΔOI/|net|", unsigned),
            ("ΔOI/|net agresor|", unsigned_aggr),
            ("ΔOI/objem", per_volume),
        ):
            if value is not None:
                series[label].append(value)
        rows.append(
            [
                f"{p.day:%d. %m.} {WEEKDAYS[p.day.weekday()]}",
                _f(signed),
                _f(unsigned),
                _f(unsigned_aggr),
                _f(per_volume),
            ]
        )
    lines += _table("bod D | ΔOI/net | ΔOI/\\|net\\| | ΔOI/\\|net agresor\\| | ΔOI/objem", rows)
    lines.append("")
    rows = []
    for label in ("ΔOI/net", "ΔOI/|net|", "ΔOI/|net agresor|", "ΔOI/objem"):
        values = series.get(label, [])
        mean = statistics.fmean(values) if values else None
        stdev = statistics.stdev(values) if len(values) > 1 else None
        positive = sum(1 for v in values if v > 0)
        rows.append(
            [
                label.replace("|", "\\|"),
                str(len(values)),
                f"{positive} z {len(values)}",
                _f(mean),
                _f(stdev),
                _f(stdev / abs(mean) if stdev is not None and mean else None, 2),
            ]
        )
    lines += _table(
        "veličina (medián dne) | bodů | > 0 | průměr | sm. odchylka | variační koeficient", rows
    )
    lines.append("")

    others = [p for p in points if p.role != "D+1"]
    if others:
        lines += ["### Uložený řetěz (pod v5 jiný než D+1, #1580)", ""]
        rows = []
        for p in others:
            stats = group_stats(midpoint_pairs(p.sides))
            stored = (
                "—" if p.stored_median is None else f"{_f(p.stored_median)} ({p.stored_samples})"
            )
            rows.append(
                [
                    f"{p.day:%d. %m.} {WEEKDAYS[p.day.weekday()]}",
                    p.expiry,
                    *_stats_cells(stats),
                    stored,
                ]
            )
        lines += _table(f"bod D | expirace | {STATS_HEADER} | uložený (n)", rows)
        lines.append("")
    return lines


def report_markdown(results: Mapping[str, Sequence[DayPoint]], generated: dt.datetime) -> str:
    lines = [
        f"# Poměry ΔOI/net kalibrace α (#1612) — {generated:%Y-%m-%d %H:%M} UTC",
        "",
        "Strany s |net| ≥ 25, ΔOI jen ze stran změřených po publikaci v D i D+1 (B+). "
        "„buy“ = strany s net > 0, „sell“ = net < 0; ΔOI/|net| = poměr bez znaménka netu. "
        "Fit b / s: ΔOI = b·net⁺ + s·|net⁻| (s > 0 = čistý prodej OI zvedá).",
        "",
    ]
    for symbol, points in results.items():
        lines += report_symbol(symbol, points)
    return "\n".join(lines)


SIDES_COLUMNS = (
    "symbol",
    "day",
    "next_day",
    "expiry",
    "role",
    "spot",
    "strike",
    "right",
    "oi_before",
    "doi",
    "net_cut_m1h",
    "net_cut",
    "net_cut_p1h",
    "buy",
    "sell",
    "volume_last",
    "volume_max",
)


def write_sides_csv(path: Path, results: Mapping[str, Sequence[DayPoint]]) -> None:
    """Strany všech bodů pro audit a příklady v rozboru (jedna řádka = strana bodu D)."""
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(SIDES_COLUMNS)
        for points in results.values():
            for p in points:
                for s in p.sides:
                    writer.writerow(
                        [
                            p.symbol,
                            p.day.isoformat(),
                            p.next_day.isoformat(),
                            p.expiry,
                            p.role,
                            p.spot,
                            s.strike,
                            s.right,
                            s.oi_before,
                            s.doi,
                            s.net.get(-1),
                            s.net.get(0),
                            s.net.get(1),
                            s.buy,
                            s.sell,
                            s.volume_last,
                            s.volume_max,
                        ]
                    )


def read_only_engine(url: str) -> Engine:
    """Engine DB, na PostgreSQL v read-only transakcích (skript nikdy nezapisuje)."""
    if url.startswith("postgresql"):
        return create_engine(url, connect_args={"options": "-c default_transaction_read_only=on"})
    return create_engine(url)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--db",
        default=os.environ.get("GEXLENS_HOST_DATABASE_URL")
        or os.environ.get("GEXLENS_DATABASE_URL", ""),
    )
    parser.add_argument("--data", default=os.environ.get("GEXLENS_DATA_DIR", "data"))
    parser.add_argument("--symbols", default="NQ,ES")
    parser.add_argument(
        "--from", dest="first", type=dt.date.fromisoformat, default=dt.date(2026, 9, 25)
    )
    parser.add_argument("--to", dest="last", type=dt.date.fromisoformat, default=None)
    parser.add_argument("--out", required=True, help="adresář reportu (mimo data/)")
    args = parser.parse_args(argv)
    if not args.db:
        parser.error("chybí --db, GEXLENS_HOST_DATABASE_URL nebo GEXLENS_DATABASE_URL")
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    settings = Settings(data_dir=Path(args.data))
    out_dir = Path(args.out).resolve()
    if out_dir.is_relative_to(settings.data_dir.resolve()):
        parser.error("report nesmí do data/ (skript jen čte)")
    engine = read_only_engine(args.db)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    results = {
        symbol: collect_symbol(symbol, settings, engine, args.first, args.last)
        for symbol in symbols
    }
    generated = dt.datetime.now(dt.UTC)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"fa-ratio-1612-{generated:%Y%m%d-%H%M}.md"
    path.write_text(report_markdown(results, generated), encoding="utf-8")
    sides_path = out_dir / f"fa-ratio-1612-{generated:%Y%m%d-%H%M}-strany.csv"
    write_sides_csv(sides_path, results)
    for symbol, points in results.items():
        print(f"{symbol}: bodů D {sum(1 for p in points if p.role == 'D+1')}")
    print(f"Report {path}")
    print(f"Strany {sides_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
