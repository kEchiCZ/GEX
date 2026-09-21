"""Verdikt dne a úrovně obratu — port `frontend/src/instrument/daysummary.ts` (#1090)
pro automatický scénář dne (#1173, varianta A, rozhodnutí uživatele 15. 9. 2026).

Stejné hlasování jako Shrnutí dne v Briefingu (`VERDICT_RULES_VERSION` musí
sedět s frontendem): trend vyšších TF ×2, nižších ×1, tendence −2…+2,
sentiment ±1 (nepotvrzený nehlasuje), cena vs. včerejší close ±1, ΔOI přes
noc ±1 (převaha ≥ 10 % většího totálu), gamma režim (negativní = momentum ve
směru trendu, pozitivní táhne skóre k nule). |skóre| ≥ 3 = long/short; High-
impact zpráva před openem = čekat. Scénář z verdiktu: cíle = nejbližší úrovně
obratu ve směru (zdi, flip, těžiště, PDH/PDL/PDC, ONH/ONL, ±EM), max 2.
"""

import datetime as dt
from dataclasses import dataclass
from typing import Literal

from gexlens_engine.compute.trend import Direction, TrendReport

VERDICT_RULES_VERSION = 2
#: Útes gammy předchozí seance (#576 fáze 1, #1241): nad tímto podílem je
#: struktura, která cenu držela, pryč — „pozitivní gamma tlumí“ nehlasuje
CLIFF_DAMPING_OFF = 0.5
VERDICT_THRESHOLD = 3
OI_DELTA_MIN_SHARE = 0.1
CONFLUENCE_SHARE = 0.001
TENDENCY_VOTES: dict[str, int] = {
    "strong_short": -2,
    "short": -1,
    "neutral": 0,
    "long": 1,
    "strong_long": 2,
}
VerdictKind = Literal["long", "short", "none", "wait_news"]


@dataclass(frozen=True)
class Vote:
    name: str
    vote: int
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "vote": self.vote, "reason": self.reason}


@dataclass(frozen=True)
class VerdictInput:
    trend: TrendReport | None
    positive_gamma: bool | None
    tendency_band: str | None
    sentiment_state: str | None
    sentiment_unconfirmed: bool
    price: float | None
    prev_close: float | None
    oi_call_delta: float | None
    oi_put_delta: float | None
    oi_call_total: float | None
    oi_put_total: float | None
    news_before_open: bool
    #: Podíl gammy, který odpadl expirací předchozí seance (0–1); None bez dat
    cliff_share: float | None = None


@dataclass(frozen=True)
class DayVerdict:
    verdict: VerdictKind
    score: int
    votes: tuple[Vote, ...]


def _direction_vote(direction: Direction | None, weight: int) -> int:
    if direction == "up":
        return weight
    if direction == "down":
        return -weight
    return 0


def _direction_label(direction: Direction | None) -> str:
    return {"up": "rostoucí", "down": "klesající", "range": "bez trendu"}.get(direction or "", "—")


def day_verdict(inp: VerdictInput) -> DayVerdict:
    votes: list[Vote] = []
    higher = inp.trend.higher if inp.trend else None
    lower = inp.trend.lower if inp.trend else None
    votes.append(
        Vote(
            "trend_higher",
            _direction_vote(higher, 2),
            "vyšší TF bez dat"
            if higher is None
            else f"vyšší TF (týden, den) {_direction_label(higher)}",
        )
    )
    votes.append(
        Vote(
            "trend_lower",
            _direction_vote(lower, 1),
            "nižší TF bez dat"
            if lower is None
            else f"nižší TF (4h, 1h, 15m) {_direction_label(lower)}",
        )
    )
    band = inp.tendency_band
    votes.append(
        Vote(
            "tendency",
            TENDENCY_VOTES.get(band, 0) if band is not None else 0,
            "tendence bez dat" if band is None else f"tendence {band.replace('_', ' ')}",
        )
    )
    sentiment_vote, sentiment_reason = 0, "sentiment bez dat"
    if inp.sentiment_state is not None:
        if inp.sentiment_unconfirmed:
            sentiment_reason = f"sentiment {inp.sentiment_state} nepotvrzený — nehlasuje"
        elif inp.sentiment_state == "RiskOn":
            sentiment_vote, sentiment_reason = 1, "sentiment RiskOn"
        elif inp.sentiment_state == "RiskOff":
            sentiment_vote, sentiment_reason = -1, "sentiment RiskOff"
        else:
            sentiment_reason = "sentiment Neutral"
    votes.append(Vote("sentiment", sentiment_vote, sentiment_reason))
    overnight_vote, overnight_reason = 0, "overnight vs. včerejší close bez dat"
    if inp.price is not None and inp.prev_close is not None:
        if inp.price > inp.prev_close:
            overnight_vote = 1
            overnight_reason = f"cena {inp.price:g} nad včerejším close {inp.prev_close:g}"
        elif inp.price < inp.prev_close:
            overnight_vote = -1
            overnight_reason = f"cena {inp.price:g} pod včerejším close {inp.prev_close:g}"
        else:
            overnight_reason = "cena na včerejším close"
    votes.append(Vote("overnight", overnight_vote, overnight_reason))
    votes.append(_oi_delta_vote(inp))
    partial = sum(v.vote for v in votes)
    votes.append(
        _gamma_vote(
            inp.positive_gamma,
            inp.trend.expected if inp.trend else None,
            partial,
            cliff_share=inp.cliff_share,
        )
    )
    score = sum(v.vote for v in votes)
    verdict: VerdictKind = "none"
    if score >= VERDICT_THRESHOLD:
        verdict = "long"
    elif score <= -VERDICT_THRESHOLD:
        verdict = "short"
    if inp.news_before_open:
        verdict = "wait_news"
    return DayVerdict(verdict=verdict, score=score, votes=tuple(votes))


def _oi_delta_vote(inp: VerdictInput) -> Vote:
    if inp.oi_call_delta is None or inp.oi_put_delta is None:
        return Vote("oi_delta", 0, "ΔOI přes noc bez dat")
    scale = max(inp.oi_call_total or 0.0, inp.oi_put_total or 0.0)
    diff = inp.oi_call_delta - inp.oi_put_delta
    if scale <= 0 or abs(diff) < scale * OI_DELTA_MIN_SHARE:
        return Vote("oi_delta", 0, "ΔOI bez výrazné převahy call/put")
    if diff > 0:
        return Vote("oi_delta", 1, f"ΔOI převaha call (+{round(diff)})")
    return Vote("oi_delta", -1, f"ΔOI převaha put ({round(diff)})")


def _gamma_vote(
    positive_gamma: bool | None,
    expected: Direction | None,
    partial: int,
    *,
    cliff_share: float | None = None,
) -> Vote:
    if positive_gamma is None:
        return Vote("gamma", 0, "gamma režim bez dat")
    if positive_gamma and cliff_share is not None and cliff_share >= CLIFF_DAMPING_OFF:
        # 21. 9. 2026: po kvartálním OPEX (útes ES 83 %) hlas −1 vyrušil trend +3
        # a verdikt byl none při +580 b — tenká gamma netlumí
        return Vote(
            "gamma",
            0,
            f"pozitivní gamma, ale po útesu {cliff_share:.0%} gammy je tlumení tenké — nehlasuje",
        )
    if not positive_gamma:
        if expected == "up":
            return Vote("gamma", 1, "negativní gamma = momentum ve směru trendu (long)")
        if expected == "down":
            return Vote("gamma", -1, "negativní gamma = momentum ve směru trendu (short)")
        return Vote("gamma", 0, "negativní gamma, ale trend bez směru")
    if partial > 0:
        return Vote("gamma", -1, "pozitivní gamma tlumí pohyb — převaha slabší")
    if partial < 0:
        return Vote("gamma", 1, "pozitivní gamma tlumí pohyb — převaha slabší")
    return Vote("gamma", 0, "pozitivní gamma tlumí pohyb")


# ── Úrovně obratu ────────────────────────────────────────────────


@dataclass(frozen=True)
class TurnLevel:
    price: float
    label: str
    role: str  # odpor | podpora
    distance: float


@dataclass(frozen=True)
class LevelInputs:
    flip: float | None = None
    call_wall: float | None = None
    put_wall: float | None = None
    centroid: float | None = None
    prev_high: float | None = None
    prev_low: float | None = None
    prev_close: float | None = None
    on_high: float | None = None
    on_low: float | None = None
    em_anchor: float | None = None
    em_points: float | None = None


def turn_levels(price: float, levels: LevelInputs) -> list[TurnLevel]:
    raw: list[tuple[float | None, str]] = [
        (levels.flip, "Gamma flip"),
        (levels.call_wall, "Call wall"),
        (levels.put_wall, "Put wall"),
        (levels.centroid, "Těžiště GEX"),
        (levels.prev_high, "PDH"),
        (levels.prev_low, "PDL"),
        (levels.prev_close, "PDC"),
        (levels.on_high, "ONH"),
        (levels.on_low, "ONL"),
        (
            levels.em_anchor + levels.em_points
            if levels.em_anchor is not None and levels.em_points
            else None,
            "+EM",
        ),
        (
            levels.em_anchor - levels.em_points
            if levels.em_anchor is not None and levels.em_points
            else None,
            "−EM",
        ),
    ]
    result = [
        TurnLevel(
            price=round(value, 2),
            label=label,
            role="odpor" if value >= price else "podpora",
            distance=abs(value - price),
        )
        for value, label in raw
        if value is not None
    ]
    return sorted(result, key=lambda level: level.distance)


# ── Scénář z verdiktu ────────────────────────────────────────────


@dataclass(frozen=True)
class AutoScenario:
    direction: Literal["long", "short"]
    entry: float
    targets: tuple[float, ...]
    target_labels: tuple[str, ...]
    path: tuple[tuple[dt.datetime, float], ...]


def build_auto_scenario(
    verdict: DayVerdict,
    price: float,
    levels: LevelInputs,
    *,
    now: dt.datetime,
    deadline_ts: dt.datetime,
    min_distance_share: float = 0.0005,
    max_targets: int = 2,
) -> AutoScenario | None:
    """Cíle = nejbližší úrovně obratu ve směru verdiktu (odpory pro long, podpory
    pro short), sloučené konfluence, ≥ 0,05 % ceny od vstupu; None bez verdiktu
    nebo bez úrovně ve směru. Cesta: vstup → cíl 1 v třetině okna → cíl 2 v termínu.
    """
    if verdict.verdict not in ("long", "short"):
        return None
    role = "odpor" if verdict.verdict == "long" else "podpora"
    tolerance = price * min_distance_share
    picked: list[TurnLevel] = []
    for level in turn_levels(price, levels):
        if level.role != role or level.distance < tolerance:
            continue
        if picked and abs(level.price - picked[-1].price) <= price * CONFLUENCE_SHARE:
            continue  # konfluence = táž úroveň
        picked.append(level)
        if len(picked) >= max_targets:
            break
    if not picked:
        return None
    span = deadline_ts - now
    if span.total_seconds() <= 0:
        return None
    path: list[tuple[dt.datetime, float]] = [(now, price)]
    if len(picked) == 1:
        path.append((deadline_ts, picked[0].price))
    else:
        path.append((now + span / 3, picked[0].price))
        path.append((deadline_ts, picked[1].price))
    return AutoScenario(
        direction=verdict.verdict,
        entry=price,
        targets=tuple(level.price for level in picked),
        target_labels=tuple(level.label for level in picked),
        path=tuple(path),
    )
