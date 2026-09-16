"""Kouč v1 (#1187 fáze 3, #933) — čisté funkce nad obchody deníku.

Kouč nehádá z nálady: každá věta má číslo a důkaz z deníku. Vstup je
obchod deníku (`journal_trades` + kontext paper orderu), výstup jsou
**příznaky** (co bylo špatně) s cenou v R, **skóre disciplíny** dne a
**týdenní report** s 1–3 pravidly na příští týden. LLM sem neteče.

Katalog příznaků v1 (měřitelné z dat, viz #1187 bod 4):

| příznak | důkaz |
|---|---|
| `no_stop` | obchod bez plánovaného stopu |
| `no_setup` | bez setupu z playbooku / bez plánu |
| `low_rr` | plánované RRR < práh (default 1,5) |
| `early_exit` | ruční výstup a cena pak došla na cíl → nechané R |
| `revenge` | vstup do N min po stopu (default 5) |
| `after_brake` | vstup po dosažení denní brzdy (jen ruční obchody — paper blokuje) |
| `overtrading` | víc obchodů za seanci než práh (default 4) |
| `big_loss` | realizované R pod −1,2 (stop nedodržen / posunut) |
| `stop_moved` | stop posunutý dál od entry (historie změn paper orderu) |

Zvětšování po sérii výher přijde s fází 5.
"""

import datetime as dt
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from gexlens_engine.compute.settle import session_bounds, trading_session_date

COACH_RULES_VERSION = 1

PENALTIES: dict[str, int] = {
    "no_stop": 25,
    "after_brake": 25,
    "revenge": 15,
    "big_loss": 15,
    "stop_moved": 20,
    "no_setup": 10,
    "low_rr": 10,
    "early_exit": 10,
    "overtrading": 10,
}

FLAG_LABELS: dict[str, str] = {
    "no_stop": "obchod bez plánovaného stopu",
    "no_setup": "obchod bez setupu z playbooku",
    "low_rr": "plánované RRR pod prahem",
    "early_exit": "předčasný výstup — cena pak došla na cíl",
    "revenge": "vstup krátce po stopu (revenge)",
    "after_brake": "vstup po dosažení denní brzdy",
    "overtrading": "příliš mnoho obchodů za seanci",
    "big_loss": "ztráta nad plánované riziko (stop nedodržen)",
    "stop_moved": "stop posunutý dál od entry",
}

ADVICE: dict[str, str] = {
    "no_stop": "Každý order jen s předem daným stopem — bez stopu nevstupuj "
    "(paper účet to vynucuje).",
    "no_setup": "Vstup jen na setup z playbooku; bez pojmenovaného setupu není co hodnotit.",
    "low_rr": "Neber obchody s RRR pod 1,5 — na tenkém edge je to matematika, ne názor.",
    "early_exit": "Nech obchod dojít k cíli nebo stopu; ruční výstup jen z předem daného důvodu.",
    "revenge": "Po stopu 15 minut pauza — žádný nový vstup na stejný symbol.",
    "after_brake": "Po denní brzdě (−3 R) den končí; nové obchody až zítra.",
    "overtrading": "Max 4 obchody za seanci; pátý je skoro vždy horší než první.",
    "big_loss": "Stop se nepřesouvá dál; ztráta přes −1,2 R znamená, že plán nebyl dodržen.",
    "stop_moved": "Stop se po vstupu jen přitahuje, nikdy neposouvá dál — "
    "širší stop = setup bez struktury.",
}


@dataclass(frozen=True)
class CoachParams:
    min_rr: float = 1.5
    revenge_minutes: int = 5
    max_trades_per_day: int = 4
    big_loss_r: float = -1.2
    daily_brake_r: float = 3.0


#: Výchozí prahy — jediná instance, ať se ve výchozích argumentech nevolá konstruktor
DEFAULT_PARAMS = CoachParams()


@dataclass(frozen=True)
class Trade:
    """Obchod deníku (typ `obchod`) v podobě, kterou kouč potřebuje."""

    id: int
    symbol: str
    direction: str  # long | short
    opened_ts: dt.datetime | None
    closed_ts: dt.datetime | None
    planned_entry: float | None
    planned_stop: float | None
    planned_target: float | None
    actual_entry: float | None
    actual_exit: float | None
    size: float | None
    setup_key: str | None
    mfe: float | None
    mae: float | None
    net_pnl: float | None
    paper: bool = False
    exit_reason: str | None = None
    r_multiple: float | None = None
    day_r_at_entry: float | None = None
    tags: tuple[str, ...] = ()
    #: Body, o které se stop posunul dál od entry (paper historie změn, #1187 fáze 4)
    stop_widened_points: float | None = None

    @property
    def sign(self) -> float:
        return 1.0 if self.direction == "long" else -1.0

    @property
    def risk_points(self) -> float | None:
        entry = self.actual_entry if self.actual_entry is not None else self.planned_entry
        if entry is None or self.planned_stop is None:
            return None
        risk = abs(entry - self.planned_stop)
        return risk if risk > 0 else None

    @property
    def realized_r(self) -> float | None:
        if self.r_multiple is not None:
            return self.r_multiple
        risk = self.risk_points
        entry = self.actual_entry if self.actual_entry is not None else self.planned_entry
        if risk is None or entry is None or self.actual_exit is None:
            return None
        return (self.actual_exit - entry) * self.sign / risk

    @property
    def planned_rr(self) -> float | None:
        risk = self.risk_points
        entry = self.actual_entry if self.actual_entry is not None else self.planned_entry
        if risk is None or entry is None or self.planned_target is None:
            return None
        return abs(self.planned_target - entry) / risk

    @property
    def mfe_r(self) -> float | None:
        risk = self.risk_points
        if risk is None or self.mfe is None:
            return None
        return self.mfe / risk


def trade_from_journal(row: dict[str, Any]) -> Trade | None:
    """Řádek `journal_list` (s vnořeným `trade`) → Trade; None u řádků bez obchodu."""
    trade = row.get("trade")
    if not isinstance(trade, dict):
        return None
    raw_context = row.get("context")
    context: dict[str, Any] = raw_context if isinstance(raw_context, dict) else {}
    tags = tuple(str(t) for t in (row.get("tags") or []))

    def ts(value: Any) -> dt.datetime | None:
        if isinstance(value, dt.datetime):
            return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
        if isinstance(value, str):
            try:
                parsed = dt.datetime.fromisoformat(value)
            except ValueError:
                return None
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
        return None

    def num(value: Any) -> float | None:
        return float(value) if isinstance(value, int | float) else None

    return Trade(
        id=int(row["id"]),
        symbol=str(row.get("symbol") or ""),
        direction=str(trade.get("direction") or "long"),
        opened_ts=ts(trade.get("opened_ts")) or ts(row.get("ts_ref")),
        closed_ts=ts(trade.get("closed_ts")),
        planned_entry=num(trade.get("planned_entry")),
        planned_stop=num(trade.get("planned_stop")),
        planned_target=num(trade.get("planned_target")),
        actual_entry=num(trade.get("actual_entry")),
        actual_exit=num(trade.get("actual_exit")),
        size=num(trade.get("size")),
        setup_key=str(trade["setup_key"]) if trade.get("setup_key") else None,
        mfe=num(trade.get("mfe")),
        mae=num(trade.get("mae")),
        net_pnl=num(trade.get("net_pnl")),
        paper="paper" in tags or "paper_order_id" in context,
        exit_reason=str(context["exit_reason"]) if context.get("exit_reason") else None,
        r_multiple=num(context.get("r_multiple")),
        day_r_at_entry=num(context.get("day_r_at_entry")),
        tags=tags,
        stop_widened_points=num(context.get("stop_widened_points")),
    )


@dataclass(frozen=True)
class Flag:
    kind: str
    detail: str
    #: Cena příznaku v R (záporná = kolik stál), None když se nedá vyčíslit
    cost_r: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": FLAG_LABELS[self.kind],
            "detail": self.detail,
            "cost_r": self.cost_r,
        }


@dataclass(frozen=True)
class TradeReview:
    trade: Trade
    flags: tuple[Flag, ...]
    realized_r: float | None
    planned_rr: float | None
    capture: float | None  # realizované R / MFE R (jen když MFE > 0)

    def as_dict(self) -> dict[str, Any]:
        t = self.trade
        return {
            "id": t.id,
            "symbol": t.symbol,
            "direction": t.direction,
            "opened_ts": t.opened_ts.isoformat() if t.opened_ts else None,
            "closed_ts": t.closed_ts.isoformat() if t.closed_ts else None,
            "setup_key": t.setup_key,
            "paper": t.paper,
            "exit_reason": t.exit_reason,
            "realized_r": self.realized_r,
            "planned_rr": self.planned_rr,
            "capture": self.capture,
            "net_pnl": t.net_pnl,
            "flags": [f.as_dict() for f in self.flags],
            "penalty": sum(PENALTIES[f.kind] for f in self.flags),
        }


BarsAfter = Callable[[str, dt.date], Sequence[tuple[dt.datetime, float, float]]]


def _target_reached_after(trade: Trade, bars: Sequence[tuple[dt.datetime, float, float]]) -> bool:
    if trade.closed_ts is None or trade.planned_target is None:
        return False
    for ts, high, low in bars:
        if ts <= trade.closed_ts:
            continue
        if trade.direction == "long" and high >= trade.planned_target:
            return True
        if trade.direction == "short" and low <= trade.planned_target:
            return True
    return False


def review_trade(
    trade: Trade,
    previous: Sequence[Trade],
    *,
    params: CoachParams = DEFAULT_PARAMS,
    bars_after: BarsAfter | None = None,
    trades_same_session: int = 1,
) -> TradeReview:
    flags: list[Flag] = []
    realized = trade.realized_r
    rr = trade.planned_rr
    if trade.planned_stop is None:
        flags.append(Flag("no_stop", "obchod nemá plánovaný stop — riziko nebylo definované"))
    if trade.setup_key is None:
        flags.append(
            Flag(
                "no_setup",
                "bez setupu z playbooku",
                realized if realized is not None and realized < 0 else None,
            )
        )
    if rr is not None and rr < params.min_rr:
        flags.append(
            Flag(
                "low_rr",
                f"plánované RRR {rr:.1f} < {params.min_rr:g}",
                realized if realized is not None and realized < 0 else None,
            )
        )
    if realized is not None and realized < params.big_loss_r:
        flags.append(
            Flag("big_loss", f"realizováno {realized:+.2f} R (stop = −1 R)", realized + 1.0)
        )
    if trade.stop_widened_points is not None and trade.stop_widened_points > 0:
        flags.append(
            Flag(
                "stop_moved",
                f"stop posunut o {trade.stop_widened_points:g} b dál od entry",
                realized if realized is not None and realized < 0 else None,
            )
        )
    if trade.day_r_at_entry is not None and trade.day_r_at_entry <= -params.daily_brake_r:
        flags.append(
            Flag(
                "after_brake",
                f"vstup při dnešních {trade.day_r_at_entry:+.1f} R",
                realized if realized is not None and realized < 0 else None,
            )
        )
    if trade.opened_ts is not None:
        window = dt.timedelta(minutes=params.revenge_minutes)
        for prev in previous:
            if prev.closed_ts is None or prev.symbol != trade.symbol:
                continue
            prev_r = prev.realized_r
            stopped = prev.exit_reason == "stop" or (
                prev.exit_reason is None and prev_r is not None and prev_r <= -0.9
            )
            if stopped and dt.timedelta(0) <= trade.opened_ts - prev.closed_ts <= window:
                gap = int((trade.opened_ts - prev.closed_ts).total_seconds() // 60)
                flags.append(
                    Flag(
                        "revenge",
                        f"vstup {gap} min po stopu #{prev.id}",
                        realized if realized is not None and realized < 0 else None,
                    )
                )
                break
    if trades_same_session > params.max_trades_per_day:
        flags.append(
            Flag(
                "overtrading",
                f"{trades_same_session}. obchod seance (práh {params.max_trades_per_day})",
                realized if realized is not None and realized < 0 else None,
            )
        )
    manual_exit = trade.exit_reason in ("manual", None) and trade.actual_exit is not None
    if (
        manual_exit
        and trade.planned_target is not None
        and trade.closed_ts is not None
        and bars_after is not None
        and rr is not None
        and realized is not None
        and realized < rr - 0.05
    ):
        session = trading_session_date(trade.closed_ts)
        try:
            bars = bars_after(trade.symbol, session)
        except Exception:
            bars = ()
        if _target_reached_after(trade, bars):
            flags.append(
                Flag(
                    "early_exit",
                    f"vystoupil jsi na {realized:+.2f} R, cíl ({rr:.1f} R) pak zasažen",
                    realized - rr,
                )
            )
    mfe_r = trade.mfe_r
    capture = None
    if realized is not None and mfe_r is not None and mfe_r > 0:
        capture = max(0.0, min(1.0, realized / mfe_r)) if realized > 0 else 0.0
    return TradeReview(
        trade=trade, flags=tuple(flags), realized_r=realized, planned_rr=rr, capture=capture
    )


@dataclass(frozen=True)
class DailyReview:
    session_day: dt.date
    reviews: tuple[TradeReview, ...]
    score: int
    total_r: float
    flagged_cost_r: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_day": self.session_day.isoformat(),
            "rules_version": COACH_RULES_VERSION,
            "trades": [r.as_dict() for r in self.reviews],
            "n": len(self.reviews),
            "score": self.score,
            "total_r": self.total_r,
            "flagged_cost_r": self.flagged_cost_r,
            "summary": daily_summary(self),
        }


def discipline_score(reviews: Sequence[TradeReview]) -> int:
    if not reviews:
        return 100
    penalty = sum(PENALTIES[f.kind] for r in reviews for f in r.flags)
    return max(0, 100 - penalty)


def daily_review(
    trades: Sequence[Trade],
    session_day: dt.date,
    *,
    params: CoachParams = DEFAULT_PARAMS,
    bars_after: BarsAfter | None = None,
) -> DailyReview:
    """Obchody seance chronologicky; předchozí obchody (i mimo seanci) pro revenge."""
    start, end = session_bounds(session_day)
    ordered = sorted(
        (t for t in trades if t.opened_ts is not None),
        key=lambda t: t.opened_ts or dt.datetime.min.replace(tzinfo=dt.UTC),
    )
    reviews: list[TradeReview] = []
    count = 0
    for index, trade in enumerate(ordered):
        assert trade.opened_ts is not None
        if not (start <= trade.opened_ts < end):
            continue
        count += 1
        reviews.append(
            review_trade(
                trade,
                ordered[:index],
                params=params,
                bars_after=bars_after,
                trades_same_session=count,
            )
        )
    total = sum(r.realized_r or 0.0 for r in reviews)
    cost = sum(f.cost_r for r in reviews for f in r.flags if f.cost_r is not None and f.cost_r < 0)
    return DailyReview(
        session_day=session_day,
        reviews=tuple(reviews),
        score=discipline_score(reviews),
        total_r=total,
        flagged_cost_r=cost,
    )


def daily_summary(review: DailyReview) -> str:
    if not review.reviews:
        return "Bez obchodu — nic k hodnocení."
    flagged = sum(1 for r in review.reviews if r.flags)
    parts = [
        f"{len(review.reviews)} obchodů, {review.total_r:+.2f} R, disciplína {review.score}/100.",
    ]
    if flagged:
        kinds = sorted({f.kind for r in review.reviews for f in r.flags})
        parts.append(
            f"Příznaky u {flagged} obchodů: " + ", ".join(FLAG_LABELS[k] for k in kinds) + "."
        )
        if review.flagged_cost_r < 0:
            parts.append(f"Chyby stály {review.flagged_cost_r:+.2f} R.")
    else:
        parts.append("Bez příznaků — plán dodržen.")
    return " ".join(parts)


@dataclass(frozen=True)
class WeeklyReport:
    week_start: dt.date
    week_end: dt.date
    days: tuple[DailyReview, ...]

    def as_dict(self) -> dict[str, Any]:
        reviews = [r for d in self.days for r in d.reviews]
        realized = [r.realized_r for r in reviews if r.realized_r is not None]
        wins = [r for r in realized if r > 0]
        flag_stats: dict[str, dict[str, float]] = {}
        for review in reviews:
            for flag in review.flags:
                stat = flag_stats.setdefault(flag.kind, {"n": 0, "cost_r": 0.0})
                stat["n"] += 1
                if flag.cost_r is not None and flag.cost_r < 0:
                    stat["cost_r"] += flag.cost_r
        by_hour: dict[int, list[float]] = {}
        for review in reviews:
            if review.trade.opened_ts is not None and review.realized_r is not None:
                by_hour.setdefault(review.trade.opened_ts.hour, []).append(review.realized_r)
        captures = [r.capture for r in reviews if r.capture is not None]
        ranked = sorted(flag_stats.items(), key=lambda item: (item[1]["cost_r"], -item[1]["n"]))
        rules = [
            {"kind": kind, "advice": ADVICE[kind], "n": int(stat["n"]), "cost_r": stat["cost_r"]}
            for kind, stat in ranked[:3]
        ]
        return {
            "week_start": self.week_start.isoformat(),
            "week_end": self.week_end.isoformat(),
            "rules_version": COACH_RULES_VERSION,
            "n": len(reviews),
            "total_r": sum(realized),
            "win_rate": (len(wins) / len(realized)) if realized else None,
            "avg_r": statistics.fmean(realized) if realized else None,
            "score": (
                round(statistics.fmean(d.score for d in self.days if d.reviews))
                if any(d.reviews for d in self.days)
                else None
            ),
            "capture": statistics.fmean(captures) if captures else None,
            "flags": {kind: stat for kind, stat in flag_stats.items()},
            "by_hour_utc": {
                str(h): {"n": len(v), "sum_r": sum(v)} for h, v in sorted(by_hour.items())
            },
            "days": [d.as_dict() for d in self.days],
            "rules": rules,
        }


def weekly_report(
    trades: Sequence[Trade],
    any_day: dt.date,
    *,
    params: CoachParams = DEFAULT_PARAMS,
    bars_after: BarsAfter | None = None,
) -> WeeklyReport:
    monday = any_day - dt.timedelta(days=any_day.weekday())
    days = [
        daily_review(trades, monday + dt.timedelta(days=i), params=params, bars_after=bars_after)
        for i in range(5)
    ]
    return WeeklyReport(week_start=monday, week_end=monday + dt.timedelta(days=4), days=tuple(days))


__all__ = [
    "ADVICE",
    "COACH_RULES_VERSION",
    "FLAG_LABELS",
    "PENALTIES",
    "CoachParams",
    "DailyReview",
    "Flag",
    "Trade",
    "TradeReview",
    "WeeklyReport",
    "daily_review",
    "discipline_score",
    "review_trade",
    "trade_from_journal",
    "weekly_report",
]
