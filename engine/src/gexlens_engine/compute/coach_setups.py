"""Kouč v2 (#1201): setupy ze záložky Setupy + denní doba — čisté funkce.

Setup je mechanický obchod detektoru: kouč u něj nehodnotí disciplínu, ale
**kde a kdy detektor vyrábí ztrátové obchody** a doporučuje, co vypnout nebo
naopak obchodovat. Všechna doporučení stojí na vzorku (n ≥ `MIN_SAMPLE`)
a Wilsonově dolní mezi úspěšnosti — stejná zásada jako u brány signálů
(SPEC 6.2) a kalibrace confidence (#794 2B).

Denní doba se měří dvěma osami: **hodina lokálního času** (Europe/Prague —
uživatel obchoduje odtud) a **segment seance** (zrcadlo
`frontend/src/journal/segments.ts`, profil futures: Globex noc → premarket
→ open +30 → RTH dopoledne → poledne → power hour → posledních 30 min →
po close).
"""

import datetime as dt
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from gexlens_engine.compute.settle import ET_TZ, session_time_utc
from gexlens_engine.compute.setups import Direction, is_counter_regime
from gexlens_engine.compute.setupstats import wilson_lower_bound

COACH_SETUPS_RULES_VERSION = 1
LOCAL_TZ = ZoneInfo("Europe/Prague")
MIN_SAMPLE = 30
MIN_WINDOW_SAMPLE = 20
#: Práh Ø R, od kterého se okno/kombinace doporučí vypnout (záporný) nebo zdůraznit (kladný)
BAD_AVG_R = -0.15
GOOD_AVG_R = 0.2

SEGMENT_LABELS: dict[str, str] = {
    "globex": "Globex noc",
    "premarket": "US premarket",
    "open30": "US open +30",
    "dopoledne": "RTH dopoledne",
    "poledne": "Poledne",
    "power": "Power hour",
    "close30": "Posledních 30 min",
    "after_close": "Po close",
}
SEGMENT_ORDER = tuple(SEGMENT_LABELS)


def hour_local(ts: dt.datetime, tz: ZoneInfo = LOCAL_TZ) -> int:
    return ts.astimezone(tz).hour


def session_segment(ts: dt.datetime) -> str:
    """Segment seance pro okamžik `ts` (UTC) — hranice od US openu/close v ET
    (DST-korektně), stejné jako report card v deníku (#712)."""
    et_day = ts.astimezone(ET_TZ).date()
    open_ts = session_time_utc(et_day, 9, 30, ET_TZ)
    close_ts = session_time_utc(et_day, 16, 0, ET_TZ)
    minute = dt.timedelta(minutes=1)
    if ts < open_ts - 90 * minute:
        return "globex"
    if ts < open_ts:
        return "premarket"
    if ts < open_ts + 30 * minute:
        return "open30"
    if ts < open_ts + 150 * minute:
        return "dopoledne"
    if ts < close_ts - 90 * minute:
        return "poledne"
    if ts < close_ts - 30 * minute:
        return "power"
    if ts < close_ts:
        return "close30"
    return "after_close"


@dataclass(frozen=True)
class SetupItem:
    id: int
    symbol: str
    template: str
    direction: str
    created_ts: dt.datetime
    closed_ts: dt.datetime | None
    status: str
    outcome_r: float
    entry: float
    target: float
    stop: float
    confidence: int
    gex_regime: str | None
    band_class: str | None
    tradeable: bool | None
    affordable: bool | None
    trade_block: str | None
    mfe: float | None
    mae: float | None

    @property
    def planned_rr(self) -> float | None:
        risk = abs(self.entry - self.stop)
        return abs(self.target - self.entry) / risk if risk > 0 else None

    @property
    def segment(self) -> str:
        return session_segment(self.created_ts)

    @property
    def hour(self) -> int:
        return hour_local(self.created_ts)


def _ts(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    return None


def setup_from_row(row: dict[str, Any]) -> SetupItem | None:
    """Řádek `setups` (dict) → SetupItem; None u aktivních nebo řádků bez R."""
    created = _ts(row.get("created_ts"))
    if created is None or row.get("status") == "active" or row.get("outcome_r") is None:
        return None
    raw_context = row.get("context")
    context: dict[str, Any] = raw_context if isinstance(raw_context, dict) else {}

    def flag(key: str) -> bool | None:
        value = context.get(key)
        return value if isinstance(value, bool) else None

    return SetupItem(
        id=int(row["id"]),
        symbol=str(row.get("symbol") or ""),
        template=str(row.get("template") or ""),
        direction=str(row.get("direction") or "long"),
        created_ts=created,
        closed_ts=_ts(row.get("closed_ts")),
        status=str(row.get("status") or ""),
        outcome_r=float(row["outcome_r"]),
        entry=float(row.get("entry") or 0.0),
        target=float(row.get("target") or 0.0),
        stop=float(row.get("stop") or 0.0),
        confidence=int(row.get("confidence") or 0),
        gex_regime=str(context["gex_regime"]) if context.get("gex_regime") else None,
        band_class=str(context["band_class"]) if context.get("band_class") else None,
        tradeable=flag("tradeable"),
        affordable=flag("affordable"),
        trade_block=str(context["trade_block"]) if context.get("trade_block") else None,
        mfe=float(row["mfe"]) if row.get("mfe") is not None else None,
        mae=float(row["mae"]) if row.get("mae") is not None else None,
    )


# ── statistika koše ────────────────────────────────────────────────


@dataclass(frozen=True)
class Bucket:
    n: int
    wins: int
    sum_r: float

    @property
    def avg_r(self) -> float:
        return self.sum_r / self.n if self.n else 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.n if self.n else 0.0

    @property
    def win_lb(self) -> float:
        return wilson_lower_bound(self.wins, self.n) if self.n else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "sum_r": round(self.sum_r, 3),
            "avg_r": round(self.avg_r, 3),
            "win_rate": round(self.win_rate, 3),
            "win_lb": round(self.win_lb, 3),
        }


def bucket_of(results: Iterable[float]) -> Bucket:
    values = list(results)
    return Bucket(n=len(values), wins=sum(1 for r in values if r > 0), sum_r=sum(values))


# ── denní doba ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class TimeProfile:
    hours: dict[int, Bucket]
    segments: dict[str, Bucket]
    best_hour: int | None
    worst_hour: int | None
    best_segment: str | None
    worst_segment: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "tz": str(LOCAL_TZ),
            "hours": {str(h): b.as_dict() for h, b in sorted(self.hours.items())},
            "segments": {
                key: {"label": SEGMENT_LABELS[key], **self.segments[key].as_dict()}
                for key in SEGMENT_ORDER
                if key in self.segments
            },
            "best_hour": self.best_hour,
            "worst_hour": self.worst_hour,
            "best_segment": self.best_segment,
            "worst_segment": self.worst_segment,
            "min_window_sample": MIN_WINDOW_SAMPLE,
        }


def time_of_day_profile(
    points: Sequence[tuple[dt.datetime, float]], *, min_sample: int = MIN_WINDOW_SAMPLE
) -> TimeProfile:
    """(čas vstupu UTC, výsledek R) → profil po lokálních hodinách a segmentech;
    nejlepší/nejhorší okno jen z košů s n ≥ min_sample (jinak None)."""
    by_hour: dict[int, list[float]] = defaultdict(list)
    by_segment: dict[str, list[float]] = defaultdict(list)
    for ts, r in points:
        by_hour[hour_local(ts)].append(r)
        by_segment[session_segment(ts)].append(r)
    hours = {h: bucket_of(v) for h, v in by_hour.items()}
    segments = {k: bucket_of(v) for k, v in by_segment.items()}

    def extreme(buckets: dict[Any, Bucket], best: bool) -> Any | None:
        eligible = [(k, b) for k, b in buckets.items() if b.n >= min_sample]
        if not eligible:
            return None
        key, _ = (max if best else min)(eligible, key=lambda item: item[1].avg_r)
        return key

    return TimeProfile(
        hours=hours,
        segments=segments,
        best_hour=extreme(hours, True),
        worst_hour=extreme(hours, False),
        best_segment=extreme(segments, True),
        worst_segment=extreme(segments, False),
    )


# ── příznaky setupu ───────────────────────────────────────────────

SETUP_FLAG_LABELS: dict[str, str] = {
    "counter_regime": "setup proti gamma režimu (fade v negativní gammě)",
    "outside_band": "vstup mimo tlumící pásmo",
    "unaffordable": "stop nad rozpočtem rizika (stín)",
    "low_rr": "plánované RRR pod 1,5",
    "timeout": "bez rozhodnutí do settle (timeout)",
    "bad_window": "vstup v prodělečném okně dne",
}


@dataclass(frozen=True)
class SetupFlag:
    kind: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "label": SETUP_FLAG_LABELS[self.kind], "detail": self.detail}


def setup_flags(item: SetupItem, profile: TimeProfile | None = None) -> list[SetupFlag]:
    flags: list[SetupFlag] = []
    direction = Direction.LONG if item.direction == "long" else Direction.SHORT
    if item.gex_regime is not None and is_counter_regime(direction, item.gex_regime):
        flags.append(SetupFlag("counter_regime", f"{item.direction} v režimu {item.gex_regime}"))
    if item.band_class in ("outside", "no_zone"):
        flags.append(SetupFlag("outside_band", f"poloha {item.band_class}"))
    if item.affordable is False:
        flags.append(
            SetupFlag("unaffordable", f"stop {abs(item.entry - item.stop):g} b nad rozpočtem")
        )
    rr = item.planned_rr
    if rr is not None and rr < 1.5:
        flags.append(SetupFlag("low_rr", f"RRR {rr:.1f}"))
    if item.status == "closed_timeout":
        flags.append(SetupFlag("timeout", "cíl ani stop do settle"))
    if profile is not None:
        seg = profile.segments.get(item.segment)
        if seg is not None and seg.n >= MIN_SAMPLE and seg.avg_r <= BAD_AVG_R:
            flags.append(
                SetupFlag(
                    "bad_window",
                    f"{SEGMENT_LABELS[item.segment]}: Ø {seg.avg_r:+.2f} R (n={seg.n})",
                )
            )
    return flags


# ── doporučení ────────────────────────────────────────────────────


@dataclass(frozen=True)
class Recommendation:
    kind: str  # avoid | focus
    scope: str  # template_segment | template_regime | band | tradeable | confidence | segment
    key: str
    text: str
    bucket: Bucket

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "scope": self.scope,
            "key": self.key,
            "text": self.text,
            **self.bucket.as_dict(),
        }


def _confidence_bucket(confidence: int) -> str:
    if confidence < 45:
        return "<45"
    if confidence < 60:
        return "45–59"
    return "≥60"


def setup_recommendations(
    items: Sequence[SetupItem], *, min_sample: int = MIN_SAMPLE
) -> list[Recommendation]:
    """Kombinace s dostatečným vzorkem a jasným znaménkem Ø R → co vypnout / na co se soustředit.

    Seřazeno podle |Σ R| — nejdřív to, co stálo nebo vydělalo nejvíc.
    """
    groups: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    labels: dict[tuple[str, str, str], str] = {}
    for item in items:
        seg_label = SEGMENT_LABELS[item.segment]
        combos: list[tuple[str, str, str]] = [
            (
                "template_segment",
                f"{item.template}|{item.segment}",
                f"{item.template} v okně {seg_label}",
            ),
            ("segment", item.segment, f"okno {seg_label}"),
        ]
        if item.gex_regime:
            combos.append(
                (
                    "template_regime",
                    f"{item.template}|{item.gex_regime}",
                    f"{item.template} v {item.gex_regime} gammě",
                )
            )
        if item.band_class:
            combos.append(("band", item.band_class, f"poloha {item.band_class}"))
        if item.tradeable is not None:
            combos.append(
                (
                    "tradeable",
                    "tradeable" if item.tradeable else "shadow",
                    "obchodovatelné setupy" if item.tradeable else "stínové setupy",
                )
            )
        combos.append(
            (
                "confidence",
                _confidence_bucket(item.confidence),
                f"důvěra {_confidence_bucket(item.confidence)}",
            )
        )
        for scope, key, label in combos:
            groups[(scope, key, label)].append(item.outcome_r)
            labels[(scope, key, label)] = label
    result: list[Recommendation] = []
    for (scope, key, label), values in groups.items():
        bucket = bucket_of(values)
        if bucket.n < min_sample:
            continue
        if bucket.avg_r <= BAD_AVG_R:
            result.append(
                Recommendation(
                    "avoid",
                    scope,
                    key,
                    f"Neobchodovat {label}: Ø {bucket.avg_r:+.2f} R, úspěšnost "
                    f"{bucket.win_rate * 100:.0f} % (LB {bucket.win_lb * 100:.0f} %), "
                    f"n={bucket.n}, "
                    f"Σ {bucket.sum_r:+.1f} R.",
                    bucket,
                )
            )
        elif bucket.avg_r >= GOOD_AVG_R:
            result.append(
                Recommendation(
                    "focus",
                    scope,
                    key,
                    f"Soustředit se na {label}: Ø {bucket.avg_r:+.2f} R, úspěšnost "
                    f"{bucket.win_rate * 100:.0f} % (LB {bucket.win_lb * 100:.0f} %), "
                    f"n={bucket.n}, "
                    f"Σ {bucket.sum_r:+.1f} R.",
                    bucket,
                )
            )
    result.sort(key=lambda rec: -abs(rec.bucket.sum_r))
    return result


# ── report ────────────────────────────────────────────────────────


def setups_report(items: Sequence[SetupItem], *, min_sample: int = MIN_SAMPLE) -> dict[str, Any]:
    profile = time_of_day_profile([(i.created_ts, i.outcome_r) for i in items])
    flag_counts: dict[str, dict[str, float]] = {}
    reviewed: list[dict[str, Any]] = []
    for item in items:
        flags = setup_flags(item, profile)
        for flag in flags:
            stat = flag_counts.setdefault(flag.kind, {"n": 0, "sum_r": 0.0})
            stat["n"] += 1
            stat["sum_r"] += item.outcome_r
        reviewed.append(
            {
                "id": item.id,
                "symbol": item.symbol,
                "template": item.template,
                "direction": item.direction,
                "created_ts": item.created_ts.isoformat(),
                "segment": item.segment,
                "hour": item.hour,
                "outcome_r": item.outcome_r,
                "status": item.status,
                "flags": [f.as_dict() for f in flags],
            }
        )
    by_template: dict[str, list[float]] = defaultdict(list)
    for item in items:
        by_template[item.template].append(item.outcome_r)
    return {
        "rules_version": COACH_SETUPS_RULES_VERSION,
        "n": len(items),
        "total_r": round(sum(i.outcome_r for i in items), 3),
        "min_sample": min_sample,
        "by_template": {t: bucket_of(v).as_dict() for t, v in sorted(by_template.items())},
        "time": profile.as_dict(),
        "flags": {
            kind: {"label": SETUP_FLAG_LABELS[kind], **stat} for kind, stat in flag_counts.items()
        },
        "recommendations": [
            r.as_dict() for r in setup_recommendations(items, min_sample=min_sample)
        ],
        "setups": reviewed[-50:],  # posledních 50 — UI neukazuje tisíce řádků
    }
