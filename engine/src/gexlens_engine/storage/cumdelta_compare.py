"""Srovnání dvou zdrojů CumΔ nad uloženými řadami (#1018, #615 krok 3/6).

Čistá logika pro `scripts/compare_cumdelta_sources.py`: načte za jednu
obchodní seanci (Globex, #512) stínovou řadu z dxFeed tisků
(`derived/{sym}/cumdelta_dx/{session}.parquet`, partice = obchodní den) a živou
midpoint řadu (`derived/{sym}/flow/{utc_day}.parquet`, partice = UTC den →
seance leží v D−1 + D) a spočítá metriky shody. Nic nezapisuje.

Metriky (vše jen nad minutami, které mají OBĚ řady):

- ``max_abs_dev`` — max |dx − live| v jednotkách CumΔ; ``max_abs_dev_pct``
  totéž v % rozsahu živé řady za seanci (max − min). Odpověď na „o kolik se
  řady mohou rozejít vůči tomu, co graf ten den ukazuje".
- ``corr_levels`` — Pearson kumulativních řad. Vysoká i při konstantním
  posunu (obě řady drží tvar); nízká = jiný průběh dne.
- ``corr_increments`` — Pearson minutových přírůstků (``flow_*`` sloupce).
  Přísnější: měří, zda obě řady čtou tytéž minuty stejným směrem, ne jen
  zda mají podobný trend.
- ``sign_agree_close`` — shoda znaménka CumΔ na poslední společné minutě
  seance; to je hodnota, kterou čtou detektory a track record.
- ``sign_disagree_share`` — podíl minut, kdy jsou obě řady nenulové a mají
  opačné znaménko. Minuty, kdy je jedna z řad nula (začátek seance), se
  nepočítají — nulová hodnota není směr.
- Rozklad RTH / mimo RTH podle US RTH 9:30–16:00 New York (DST-korektně,
  `compute/marketclock.outside_us_rth`; v létě 13:30–20:00 UTC).
- ``range_ratio`` — rozsah (max − min) řady dx / rozsah živé řady. Obě řady
  mají týž multiplikátor, ale tisková větev nese jen outright agresi, midpoint
  klasifikuje veškerý přírůstek objemu (i nohy spreadů a bloky, ADR-0027) —
  měřítko se proto LIŠÍ a absolutní odchylka je z velké části rozdíl měřítka.
  ``max_abs_dev_norm`` je tvarová odchylka po přeškálování obou řad na vlastní
  rozsah (0–1), v %; korelace jsou na měřítku nezávislé.
- ``dx_chain_breaks`` / ``live_chain_breaks`` — minuty, kde kumulativ
  neodpovídá předchozí hodnotě + přírůstku (restart enginu bez navázání:
  stín po restartu začíná od nuly, živá řada se navazuje podle #638). Při
  přerušení řetězu nemají hladinové metriky ze stored řad smysl; ``rechain_series``
  postaví oba kumulativy znovu jako součet uložených přírůstků od první
  společné minuty (odstraní restarty i posun startu, ztracené minuty nevrátí).
- ``corr_increments_by_lag`` / ``best_lag`` — Pearson přírůstků při posunu
  živé řady o k minut (pár dx[ts], live[ts + k], k ∈ −3…+3). Stín uzavírá
  minutu podle hodin flush smyčky, živá řada podle minuty cyklu — když je
  korelace při k=0 nízká a při k=±1 vysoká, je to posun značek, ne jiný tok.
  Kladné k = stín předbíhá živou řadu o k minut.

Zóny: partice stínu před 4. 9. 2026 (#1013) mají tok dělený na prstenec
(``cum_ring``) a hot zónu ATM±1 (``cum_hot``); od #1013 je ``cum_ring`` celý
sbíraný řetěz a ``cum_hot`` nula. Řada dx = ``cum_ring + cum_hot`` v obou
případech; seance se zónami je ve výsledku označena (``zoned``) — srovnává
ATM±15, ne celý řetěz, a s pozdějšími seancemi se nemá míchat.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pyarrow.parquet as pq

from gexlens_engine.compute.marketclock import outside_us_rth
from gexlens_engine.compute.settle import session_bounds
from gexlens_engine.storage.parquet_store import DX_FLOW_SCHEMA, FLOW_SCHEMA

#: Seance, které se do souhrnu NEPOČÍTAJÍ (uživatel je označil za neúplné);
#: `--include-unusable` je do souhrnu vrátí, v tabulce jsou vidět vždy.
KNOWN_UNUSABLE: Mapping[dt.date, str] = {
    dt.date(2026, 9, 4): "nasazení #1013 v 09:03 CEST, neúplná seance",
    dt.date(2026, 9, 7): "Labor Day, zkrácená seance",
    dt.date(2026, 9, 8): "PC vypnuté, market data přetažená na mobil, neúplná",
}

#: Pod tolik společných minut je seance označena jako neúplná (plná Globex
#: seance má ~1 380 minut včetně pauzy 21:00–22:00 UTC, kdy se nic nepíše).
MIN_COMPLETE_MINUTES = 1000


@dataclass(frozen=True)
class MinutePoint:
    """Jedna minuta se všemi čtyřmi hodnotami potřebnými pro metriky."""

    ts_min: dt.datetime
    dx_cum: float
    live_cum: float
    dx_flow: float
    live_flow: float


@dataclass(frozen=True)
class SubsetMetrics:
    """Metriky nad podmnožinou minut (celá seance / RTH / mimo RTH)."""

    minutes: int
    max_abs_dev: float | None
    #: tvarová odchylka po přeškálování obou řad na vlastní rozsah, v %
    max_abs_dev_norm: float | None
    #: rozsah dx / rozsah live (měřítko tiskové vs. objemové řady)
    range_ratio: float | None
    corr_levels: float | None
    corr_increments: float | None
    sign_disagree_share: float | None


@dataclass(frozen=True)
class SessionComparison:
    symbol: str
    session: dt.date
    #: Minuty s oběma řadami / jen stín / jen živá
    minutes_common: int
    minutes_dx_only: int
    minutes_live_only: int
    total: SubsetMetrics
    rth: SubsetMetrics
    off_rth: SubsetMetrics
    #: max |dx − live| v % rozsahu (max − min) živé řady za seanci
    max_abs_dev_pct: float | None
    close_dx: float | None
    close_live: float | None
    sign_agree_close: bool | None
    #: Partice stínu se zónami (před #1013) — srovnává ATM±15, ne celý řetěz
    zoned: bool
    #: Přerušení řetězu kumulativu (restart bez navázání) v uložených řadách
    dx_chain_breaks: int
    live_chain_breaks: int
    #: Hladinové metriky spočtené nad kumulativy znovu poskládanými z přírůstků
    rechained: bool
    #: Pearson přírůstků při posunu živé řady o k minut, (k, r) pro k ∈ LAGS
    corr_increments_by_lag: tuple[tuple[int, float | None], ...]
    #: k s nejvyšší korelací přírůstků (None, když žádná není spočitatelná)
    best_lag: int | None
    #: Pokrytí ze stínové partice
    dx_trades: int
    dx_unknown_side: int
    dx_dropped_no_context: int
    dx_volume: float
    #: Pokrytí trade větví z živé partice `flow` (#1071): podíl klasifikovaného
    #: objemu s tiskem se stranou, podíl fallbacku (bez jediného tisku) v RTH
    #: a tisky zahozené pro chybějící deltu. None = partice sloupce nenese
    #: (před #1071) nebo trade větev celou seanci neběžela.
    live_printed_share: float | None
    live_fallback_share_rth: float | None
    live_dropped_no_delta: int | None
    #: Proč seance nepatří do souhrnu; None = použitelná
    unusable_reason: str | None
    notes: tuple[str, ...] = field(default_factory=tuple)


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Pearsonova korelace; None při < 2 bodech nebo nulové varianci."""
    n = len(xs)
    if n < 2 or n != len(ys):
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x <= 0.0 or var_y <= 0.0:
        return None
    return cov / math.sqrt(var_x * var_y)


def _sign(value: float) -> int:
    if value > 0.0:
        return 1
    if value < 0.0:
        return -1
    return 0


def _range(values: Sequence[float]) -> float:
    return max(values) - min(values) if values else 0.0


def subset_metrics(points: Sequence[MinutePoint]) -> SubsetMetrics:
    if not points:
        return SubsetMetrics(0, None, None, None, None, None, None)
    max_abs_dev = max(abs(p.dx_cum - p.live_cum) for p in points)
    dx_values = [p.dx_cum for p in points]
    live_values = [p.live_cum for p in points]
    dx_range = _range(dx_values)
    live_range = _range(live_values)
    range_ratio = dx_range / live_range if live_range > 0.0 else None
    max_abs_dev_norm: float | None = None
    if dx_range > 0.0 and live_range > 0.0:
        dx_min, live_min = min(dx_values), min(live_values)
        max_abs_dev_norm = 100.0 * max(
            abs((p.dx_cum - dx_min) / dx_range - (p.live_cum - live_min) / live_range)
            for p in points
        )
    corr_levels = pearson([p.dx_cum for p in points], [p.live_cum for p in points])
    corr_increments = pearson([p.dx_flow for p in points], [p.live_flow for p in points])
    both_nonzero = [p for p in points if p.dx_cum != 0.0 and p.live_cum != 0.0]
    disagree_share: float | None = None
    if both_nonzero:
        disagree = sum(1 for p in both_nonzero if _sign(p.dx_cum) != _sign(p.live_cum))
        disagree_share = disagree / len(both_nonzero)
    return SubsetMetrics(
        len(points),
        max_abs_dev,
        max_abs_dev_norm,
        range_ratio,
        corr_levels,
        corr_increments,
        disagree_share,
    )


#: Tolerance pro shodu `cum_t == cum_{t−1} + flow_t` (float součty přes den)
_CHAIN_TOL = 1e-6

#: Posuny živé řady (minuty), pro které se počítá korelace přírůstků
LAGS: tuple[int, ...] = (-3, -2, -1, 0, 1, 2, 3)


def lag_scan(
    dx: Mapping[dt.datetime, tuple[float, float]],
    live: Mapping[dt.datetime, tuple[float, float]],
    lags: Sequence[int] = LAGS,
) -> tuple[tuple[int, float | None], ...]:
    """Korelace přírůstků pro páry (dx[ts], live[ts + k min]) — odhalí posun značek minut."""
    out: list[tuple[int, float | None]] = []
    for lag in lags:
        shift = dt.timedelta(minutes=lag)
        xs: list[float] = []
        ys: list[float] = []
        for ts, (_, dx_flow) in dx.items():
            partner = live.get(ts + shift)
            if partner is None:
                continue
            xs.append(dx_flow)
            ys.append(partner[1])
        out.append((lag, pearson(xs, ys)))
    return tuple(out)


def best_lag(by_lag: Sequence[tuple[int, float | None]]) -> int | None:
    scored = [(r, -abs(k), k) for k, r in by_lag if r is not None]
    if not scored:
        return None
    return max(scored)[2]


def chain_breaks(series: Mapping[dt.datetime, tuple[float, float]]) -> int:
    """Počet minut, kde kumulativ nenavazuje na předchozí uloženou minutu + přírůstek.

    Sousední uložené minuty (i přes díru — chybějící minuta nic nepřičítá,
    engine neběžel). Restart stínu bez navázání = skok na nulu → 1 přerušení.
    """
    breaks = 0
    previous: float | None = None
    for ts in sorted(series):
        cum, flow = series[ts]
        if previous is not None:
            expected = previous + flow
            if abs(cum - expected) > _CHAIN_TOL * max(1.0, abs(previous), abs(cum)):
                breaks += 1
        previous = cum
    return breaks


def rechain(
    series: Mapping[dt.datetime, tuple[float, float]], minutes: Sequence[dt.datetime]
) -> dict[dt.datetime, tuple[float, float]]:
    """Kumulativ znovu jako součet uložených přírůstků nad danými minutami (od nuly)."""
    total = 0.0
    out: dict[dt.datetime, tuple[float, float]] = {}
    for ts in minutes:
        flow = series[ts][1]
        total += flow
        out[ts] = (total, flow)
    return out


def _as_utc(ts: dt.datetime) -> dt.datetime:
    return ts.replace(tzinfo=dt.UTC) if ts.tzinfo is None else ts.astimezone(dt.UTC)


def _read_rows(path: Path, schema: object) -> list[dict[str, object]]:
    if not path.exists():
        return []
    table = pq.read_table(path, schema=schema)
    return [row for row in table.to_pylist() if row.get("ts_min") is not None]


def _in_session(ts: dt.datetime, bounds: tuple[dt.datetime, dt.datetime]) -> bool:
    return bounds[0] <= ts < bounds[1]


def load_dx_series(
    derived_dir: Path, symbol: str, session: dt.date
) -> tuple[dict[dt.datetime, tuple[float, float]], dict[str, float], bool]:
    """Stínová řada seance: ts → (cum, flow), součty pokrytí, příznak zón.

    Partice stínu je podle obchodního dne (`trading_session_date`), pro
    jistotu se čte i sousední D−1/D+1 a vše se ořízne hranicemi seance.
    """
    bounds = session_bounds(session)
    series: dict[dt.datetime, tuple[float, float]] = {}
    coverage = {"trades": 0.0, "unknown_side": 0.0, "dropped_no_context": 0.0, "volume": 0.0}
    zoned = False
    for offset in (-1, 0, 1):
        day = session + dt.timedelta(days=offset)
        path = derived_dir / symbol / "cumdelta_dx" / f"{day.isoformat()}.parquet"
        for row in _read_rows(path, DX_FLOW_SCHEMA):
            ts = _as_utc(row["ts_min"])  # type: ignore[arg-type]
            if not _in_session(ts, bounds):
                continue
            cum_hot = float(row.get("cum_hot") or 0.0)  # type: ignore[arg-type]
            flow_hot = float(row.get("flow_hot") or 0.0)  # type: ignore[arg-type]
            if flow_hot != 0.0 or cum_hot != 0.0:
                zoned = True
            cum = float(row.get("cum_ring") or 0.0) + cum_hot  # type: ignore[arg-type]
            flow = float(row.get("flow_ring") or 0.0) + flow_hot  # type: ignore[arg-type]
            series[ts] = (cum, flow)
            for key in coverage:
                coverage[key] += float(row.get(key) or 0.0)  # type: ignore[arg-type]
    return series, coverage, zoned


@dataclass(frozen=True)
class LiveCoverage:
    """Součty pokrytí trade větví za seanci z partice `flow` (#1071)."""

    printed: float
    unknown: float
    fallback: float
    fallback_rth: float
    classified_rth: float
    dropped_no_delta: int

    @property
    def printed_share(self) -> float | None:
        classified = self.printed + self.unknown + self.fallback
        return self.printed / classified if classified > 0 else None

    @property
    def fallback_share_rth(self) -> float | None:
        return self.fallback_rth / self.classified_rth if self.classified_rth > 0 else None


def load_live_series(
    derived_dir: Path, symbol: str, session: dt.date
) -> tuple[dict[dt.datetime, tuple[float, float]], LiveCoverage | None]:
    """Živá řada seance: ts → (cum_delta, flow_delta) z partic UTC dnů D−1 + D,
    plus součty pokrytí trade větví (#1071); None = žádná minuta pokrytí nenese."""
    bounds = session_bounds(session)
    series: dict[dt.datetime, tuple[float, float]] = {}
    printed = unknown = fallback = fallback_rth = classified_rth = 0.0
    dropped = 0
    measured = False
    for offset in (-1, 0):
        day = session + dt.timedelta(days=offset)
        path = derived_dir / symbol / "flow" / f"{day.isoformat()}.parquet"
        for row in _read_rows(path, FLOW_SCHEMA):
            ts = _as_utc(row["ts_min"])  # type: ignore[arg-type]
            if not _in_session(ts, bounds):
                continue
            cum = float(row.get("cum_delta") or 0.0)  # type: ignore[arg-type]
            flow = float(row.get("flow_delta") or 0.0)  # type: ignore[arg-type]
            series[ts] = (cum, flow)
            if row.get("printed_volume") is None:
                continue  # minuta bez trade větve / partice před #1071
            measured = True
            p = float(row.get("printed_volume") or 0.0)  # type: ignore[arg-type]
            u = float(row.get("unknown_volume") or 0.0)  # type: ignore[arg-type]
            f = float(row.get("fallback_volume") or 0.0)  # type: ignore[arg-type]
            printed += p
            unknown += u
            fallback += f
            dropped += int(float(row.get("dropped_no_delta") or 0))  # type: ignore[arg-type]
            if not outside_us_rth(ts):
                fallback_rth += f
                classified_rth += p + u + f
    coverage = (
        LiveCoverage(printed, unknown, fallback, fallback_rth, classified_rth, dropped)
        if measured
        else None
    )
    return series, coverage


def compare_series(
    symbol: str,
    session: dt.date,
    dx: Mapping[dt.datetime, tuple[float, float]],
    live: Mapping[dt.datetime, tuple[float, float]],
    *,
    coverage: Mapping[str, float] | None = None,
    zoned: bool = False,
    rechain_series: bool = False,
    live_coverage: LiveCoverage | None = None,
) -> SessionComparison:
    """Metriky shody nad již načtenými řadami (testovatelné bez disku)."""
    common = sorted(set(dx) & set(live))
    dx_only = len(set(dx) - set(live))
    live_only = len(set(live) - set(dx))
    dx_breaks = chain_breaks(dx)
    live_breaks = chain_breaks(live)
    by_lag = lag_scan(dx, live)
    if rechain_series:
        dx = rechain(dx, common)
        live = rechain(live, common)
    points = [MinutePoint(ts, dx[ts][0], live[ts][0], dx[ts][1], live[ts][1]) for ts in common]
    total = subset_metrics(points)
    rth_points = [p for p in points if not outside_us_rth(p.ts_min)]
    off_points = [p for p in points if outside_us_rth(p.ts_min)]

    max_abs_dev_pct: float | None = None
    if points and total.max_abs_dev is not None:
        live_values = [p.live_cum for p in points]
        live_range = max(live_values) - min(live_values)
        if live_range > 0.0:
            max_abs_dev_pct = 100.0 * total.max_abs_dev / live_range

    close_dx = points[-1].dx_cum if points else None
    close_live = points[-1].live_cum if points else None
    sign_agree_close = (
        _sign(close_dx) == _sign(close_live)
        if close_dx is not None and close_live is not None
        else None
    )

    notes: list[str] = []
    unusable = KNOWN_UNUSABLE.get(session)
    if len(points) < MIN_COMPLETE_MINUTES:
        notes.append(f"neúplná ({len(points)} společných minut)")
        unusable = unusable or f"neúplná seance ({len(points)} společných minut)"
    if zoned:
        notes.append("zóny ATM±15 (před #1013)")
    if dx_breaks or live_breaks:
        notes.append(f"přerušený řetěz dx {dx_breaks}× / live {live_breaks}×")
    if not rechain_series and points and (points[0].dx_cum != 0.0 or points[0].live_cum != 0.0):
        notes.append("řady nezačínají v nule (start uprostřed seance / navázání)")

    cov = coverage or {}
    return SessionComparison(
        symbol=symbol,
        session=session,
        minutes_common=len(points),
        minutes_dx_only=dx_only,
        minutes_live_only=live_only,
        total=total,
        rth=subset_metrics(rth_points),
        off_rth=subset_metrics(off_points),
        max_abs_dev_pct=max_abs_dev_pct,
        close_dx=close_dx,
        close_live=close_live,
        sign_agree_close=sign_agree_close,
        zoned=zoned,
        dx_chain_breaks=dx_breaks,
        live_chain_breaks=live_breaks,
        rechained=rechain_series,
        corr_increments_by_lag=by_lag,
        best_lag=best_lag(by_lag),
        dx_trades=int(cov.get("trades", 0.0)),
        dx_unknown_side=int(cov.get("unknown_side", 0.0)),
        dx_dropped_no_context=int(cov.get("dropped_no_context", 0.0)),
        dx_volume=float(cov.get("volume", 0.0)),
        live_printed_share=live_coverage.printed_share if live_coverage else None,
        live_fallback_share_rth=live_coverage.fallback_share_rth if live_coverage else None,
        live_dropped_no_delta=live_coverage.dropped_no_delta if live_coverage else None,
        unusable_reason=unusable,
        notes=tuple(notes),
    )


def compare_session(
    derived_dir: Path, symbol: str, session: dt.date, *, rechain_series: bool = False
) -> SessionComparison:
    dx, coverage, zoned = load_dx_series(derived_dir, symbol, session)
    live, live_coverage = load_live_series(derived_dir, symbol, session)
    return compare_series(
        symbol,
        session,
        dx,
        live,
        coverage=coverage,
        zoned=zoned,
        rechain_series=rechain_series,
        live_coverage=live_coverage,
    )


def available_sessions(derived_dir: Path, symbol: str) -> list[dt.date]:
    """Obchodní dny, pro které existuje stínová partice (bez ní není co srovnat)."""
    dx_dir = derived_dir / symbol / "cumdelta_dx"
    if not dx_dir.exists():
        return []
    sessions: list[dt.date] = []
    for path in sorted(dx_dir.glob("*.parquet")):
        try:
            sessions.append(dt.date.fromisoformat(path.stem))
        except ValueError:
            continue
    return sessions


@dataclass(frozen=True)
class Summary:
    symbol: str
    sessions: int
    sign_agree_close: int
    median_max_abs_dev_pct: float | None
    median_max_abs_dev_norm: float | None
    median_range_ratio: float | None
    median_corr_levels: float | None
    median_corr_increments: float | None
    median_corr_increments_rth: float | None
    median_corr_increments_off_rth: float | None
    median_sign_disagree_share: float | None
    #: Korelace přírůstků při nejlepším posunu a posuny per seance
    median_corr_increments_best_lag: float | None
    best_lags: tuple[int | None, ...]


def _median(values: Iterable[float | None]) -> float | None:
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return None
    mid = len(clean) // 2
    if len(clean) % 2:
        return clean[mid]
    return (clean[mid - 1] + clean[mid]) / 2.0


def summarize(results: Sequence[SessionComparison], symbol: str) -> Summary:
    """Souhrn přes použitelné seance jednoho symbolu (mediány — málo bodů)."""
    rows = [r for r in results if r.symbol == symbol]
    return Summary(
        symbol=symbol,
        sessions=len(rows),
        sign_agree_close=sum(1 for r in rows if r.sign_agree_close),
        median_max_abs_dev_pct=_median(r.max_abs_dev_pct for r in rows),
        median_max_abs_dev_norm=_median(r.total.max_abs_dev_norm for r in rows),
        median_range_ratio=_median(r.total.range_ratio for r in rows),
        median_corr_levels=_median(r.total.corr_levels for r in rows),
        median_corr_increments=_median(r.total.corr_increments for r in rows),
        median_corr_increments_rth=_median(r.rth.corr_increments for r in rows),
        median_corr_increments_off_rth=_median(r.off_rth.corr_increments for r in rows),
        median_sign_disagree_share=_median(r.total.sign_disagree_share for r in rows),
        median_corr_increments_best_lag=_median(_corr_at_best_lag(r) for r in rows),
        best_lags=tuple(r.best_lag for r in rows),
    )


def _corr_at_best_lag(result: SessionComparison) -> float | None:
    for lag, corr in result.corr_increments_by_lag:
        if lag == result.best_lag:
            return corr
    return None
