"""Setup detektor (ADR-0004, Fáze 1): čisté funkce šablon T1–T4 a vyhodnocení.

Vstupem je historie minutových vstupů (cena podkladu + GEX úrovně + tok);
výstupem kandidáti setupů s entry/target/stop, confidence a českým
zdůvodněním. Žádné I/O — orchestraci (stav, DB, alerty) dělá
`gexlens_engine.setups.SetupEngine`. Prahy dle ADR-0004.
"""

import datetime as dt
import enum
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field


class SetupTemplate(enum.Enum):
    WALL_BOUNCE = "wall_bounce"
    FAILED_BREAK = "failed_break"
    MAX_PAIN_PIN = "max_pain_pin"
    GAMMA_MOMENTUM = "gamma_momentum"


class Direction(enum.Enum):
    LONG = "long"
    SHORT = "short"


class Outcome(enum.Enum):
    TARGET = "closed_target"
    STOP = "closed_stop"
    TIMEOUT = "closed_timeout"


@dataclass(frozen=True)
class MinuteInputs:
    """Kontext jedné minuty pro vyhodnocení šablon."""

    ts: dt.datetime
    open: float
    high: float
    low: float
    close: float
    flip: float | None
    call_wall: float | None
    put_wall: float | None
    max_pain: float | None
    cum_delta: float
    # Delta-vážené přírůstky opčního volume za minutu, per strana
    call_flow: float
    put_flow: float
    # Surový přírůstek opčního volume (T3: vyhasínání aktivity)
    opt_vol: float
    minutes_to_expiry: float | None
    # Dominance zdí (ADR-0010, #223): podíl zdi na kladné síle strany profilu.
    # None = neznámá (starší data) → podmínky dominance se přeskakují.
    call_wall_dom: float | None = field(default=None, kw_only=True)
    put_wall_dom: float | None = field(default=None, kw_only=True)
    # GEX režim (#209): "positive"/"negative" dle polohy close vůči flipu
    # (fallback znaménko TotalGEX). Jen kontext pro kalibraci Fáze 2 — váhy
    # confidence se z něj zatím nepočítají.
    gex_regime: str | None = field(default=None, kw_only=True)


@dataclass(frozen=True)
class SetupParams:
    """Prahy šablon (ADR-0004 defaulty; body podkladu)."""

    wall_zone: float = 3.0
    rejection_min: float = 1.0
    divergence_lookback: int = 10
    min_rrr: float = 1.2
    break_min: float = 3.0
    acceptance_minutes: int = 5
    reclaim_window: int = 15
    reclaim_min: float = 1.0
    pin_max_minutes: float = 180.0
    pin_min_distance: float = 8.0
    pin_stability: float = 5.0
    pin_stability_lookback: int = 60
    momentum_break: float = 2.0
    momentum_flow_share: float = 0.6
    momentum_flow_lookback: int = 10
    cooldown_minutes: int = 10
    # Minimální dominance zdi pro T1/T3 (ADR-0010, #223): argmax existuje i nad
    # plochým profilem — pod prahem zeď netvoří koncentraci a setup nevzniká
    min_wall_dominance: float = 0.15


@dataclass(frozen=True)
class SetupCandidate:
    template: SetupTemplate
    direction: Direction
    entry: float
    target: float
    stop: float
    confidence: int
    reason: str
    context: dict[str, object] = field(default_factory=dict)

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def reward(self) -> float:
        return abs(self.target - self.entry)

    @property
    def rrr(self) -> float:
        return self.reward / self.risk if self.risk > 0 else 0.0


def gex_regime(close: float, flip: float | None, total_gex: float) -> str | None:
    """GEX režim minuty (#209): poloha vůči flipu, bez flipu znaménko TotalGEX.

    Konvence shodná s `right_gamma_side` T1: close >= flip = pozitivní strana.
    None = režim nelze určit (žádný flip a nulový TotalGEX).
    """
    if flip is not None:
        return "positive" if close >= flip else "negative"
    if total_gex > 0:
        return "positive"
    if total_gex < 0:
        return "negative"
    return None


def max_pain_strike(oi_by_strike_right: Mapping[tuple[float, str], float]) -> float | None:
    """Strike minimalizující výplatu držitelům opcí (zrcadlo frontend maxpain.ts)."""
    strikes = sorted({strike for strike, _ in oi_by_strike_right})
    if not strikes or sum(oi_by_strike_right.values()) <= 0:
        return None
    best: float | None = None
    best_cost = float("inf")
    for settle in strikes:
        cost = 0.0
        for (strike, right), oi in oi_by_strike_right.items():
            if right == "C":
                cost += oi * max(0.0, settle - strike)
            else:
                cost += oi * max(0.0, strike - settle)
        if cost < best_cost:
            best_cost = cost
            best = settle
    return best


def _nearest_level_above(entry: float, candidates: Sequence[float | None]) -> float | None:
    values = [value for value in candidates if value is not None and value > entry]
    return min(values) if values else None


def _nearest_level_below(entry: float, candidates: Sequence[float | None]) -> float | None:
    values = [value for value in candidates if value is not None and value < entry]
    return max(values) if values else None


def detect_wall_bounce(
    history: Sequence[MinuteInputs], params: SetupParams
) -> SetupCandidate | None:
    """T1: odraz od zdi — Cum Δ divergence + minutové odmítnutí zdi."""
    if len(history) < params.divergence_lookback + 1:
        return None
    now = history[-1]
    then = history[-1 - params.divergence_lookback]

    for wall, dominance, direction in (
        (now.put_wall, now.put_wall_dom, Direction.LONG),
        (now.call_wall, now.call_wall_dom, Direction.SHORT),
    ):
        if wall is None:
            continue
        # Slabá zeď (ADR-0010, #223): argmax nad plochým profilem není koncentrace,
        # odraz od ní nemá oporu; neznámá dominance (None) podmínku přeskakuje
        if dominance is not None and dominance < params.min_wall_dominance:
            continue
        touched = (
            now.low <= wall + params.wall_zone
            if direction is Direction.LONG
            else now.high >= wall - params.wall_zone
        )
        if not touched:
            continue
        if direction is Direction.LONG:
            rejected = now.close >= wall + params.rejection_min
            price_into_wall = now.close < then.close
            divergence = now.cum_delta > then.cum_delta
            right_gamma_side = now.flip is None or now.close >= now.flip
        else:
            rejected = now.close <= wall - params.rejection_min
            price_into_wall = now.close > then.close
            divergence = now.cum_delta < then.cum_delta
            right_gamma_side = now.flip is None or now.close <= now.flip
        if not (rejected and price_into_wall and divergence):
            continue

        entry = now.close
        if direction is Direction.LONG:
            target = _nearest_level_above(entry, (now.max_pain, now.flip, now.call_wall))
        else:
            target = _nearest_level_below(entry, (now.max_pain, now.flip, now.put_wall))
        if target is None:
            continue
        buffer = max(3.0, 0.25 * abs(target - entry))
        stop = wall - buffer if direction is Direction.LONG else wall + buffer
        candidate = SetupCandidate(
            template=SetupTemplate.WALL_BOUNCE,
            direction=direction,
            entry=entry,
            target=target,
            stop=stop,
            confidence=55 if right_gamma_side else 45,
            reason=(
                f"Odraz od {'put' if direction is Direction.LONG else 'call'} zdi {wall:g}: "
                f"Cum Δ divergence za {params.divergence_lookback} min "
                f"({then.cum_delta:.0f} → {now.cum_delta:.0f}) a odmítnutí zdi "
                f"(close {now.close:g})."
                + ("" if right_gamma_side else " Pozor: cena na špatné straně flipu.")
            ),
            context={
                "wall": wall,
                "wall_dom": dominance,
                "flip": now.flip,
                "max_pain": now.max_pain,
                "cum_delta": now.cum_delta,
                "right_gamma_side": right_gamma_side,
                "gex_regime": now.gex_regime,
            },
        )
        if candidate.rrr < params.min_rrr:
            continue
        return candidate
    return None


def detect_failed_break(
    history: Sequence[MinuteInputs], params: SetupParams
) -> SetupCandidate | None:
    """T2: neúspěšný průraz zdi/flipu — bez akceptace, reclaim → proti průrazu."""
    if len(history) < 3:
        return None
    now = history[-1]
    window = history[-(params.reclaim_window + 1) :]

    down_levels = [lvl for lvl in (now.put_wall, now.flip) if lvl is not None]
    up_levels = [lvl for lvl in (now.call_wall, now.flip) if lvl is not None]

    for level in down_levels:  # breakdown → LONG po reclaim
        broke = [m for m in window[:-1] if m.close <= level - params.break_min]
        if not broke:
            continue
        first_break = broke[0]
        after = [m for m in window if m.ts >= first_break.ts]
        # Akceptace = N po sobě jdoucích closes pod úrovní → šablona mrtvá
        run = 0
        accepted = False
        for m in after:
            run = run + 1 if m.close < level else 0
            if run >= params.acceptance_minutes:
                accepted = True
                break
        if accepted:
            continue
        prev = history[-2]
        fresh_reclaim = (
            now.close >= level + params.reclaim_min and prev.close < level + params.reclaim_min
        )
        if not fresh_reclaim:
            continue
        extreme = min(m.low for m in after)
        entry = now.close
        target = _nearest_level_above(entry, (now.max_pain, now.flip, now.call_wall))
        if target is None:
            continue
        return SetupCandidate(
            template=SetupTemplate.FAILED_BREAK,
            direction=Direction.LONG,
            entry=entry,
            target=target,
            stop=extreme - 1.0,
            confidence=55,
            reason=(
                f"Neúspěšný průraz {level:g} dolů (dno {extreme:g} bez akceptace) "
                f"a reclaim — spring."
            ),
            context={"level": level, "extreme": extreme, "gex_regime": now.gex_regime},
        )

    for level in up_levels:  # breakout nahoru → SHORT po selhání
        broke = [m for m in window[:-1] if m.close >= level + params.break_min]
        if not broke:
            continue
        first_break = broke[0]
        after = [m for m in window if m.ts >= first_break.ts]
        run = 0
        accepted = False
        for m in after:
            run = run + 1 if m.close > level else 0
            if run >= params.acceptance_minutes:
                accepted = True
                break
        if accepted:
            continue
        prev = history[-2]
        fresh_reclaim = (
            now.close <= level - params.reclaim_min and prev.close > level - params.reclaim_min
        )
        if not fresh_reclaim:
            continue
        extreme = max(m.high for m in after)
        entry = now.close
        target = _nearest_level_below(entry, (now.max_pain, now.flip, now.put_wall))
        if target is None:
            continue
        return SetupCandidate(
            template=SetupTemplate.FAILED_BREAK,
            direction=Direction.SHORT,
            entry=entry,
            target=target,
            stop=extreme + 1.0,
            confidence=55,
            reason=(
                f"Neúspěšný průraz {level:g} nahoru (vrchol {extreme:g} bez akceptace) "
                f"a návrat — upthrust."
            ),
            context={"level": level, "extreme": extreme, "gex_regime": now.gex_regime},
        )
    return None


def detect_max_pain_pin(
    history: Sequence[MinuteInputs], params: SetupParams
) -> SetupCandidate | None:
    """T3: pin k Max Pain v posledních hodinách expirace při vyhasínající aktivitě."""
    now = history[-1]
    if now.max_pain is None or now.minutes_to_expiry is None:
        return None
    if not (0 < now.minutes_to_expiry <= params.pin_max_minutes):
        return None
    distance = now.close - now.max_pain
    if abs(distance) < params.pin_min_distance:
        return None
    if len(history) > params.pin_stability_lookback:
        past_mp = history[-1 - params.pin_stability_lookback].max_pain
        if past_mp is not None and abs(now.max_pain - past_mp) >= params.pin_stability:
            return None
    # Pin funguje jen při dostatečně velkém/koncentrovaném pozicování (ADR-0010,
    # #223): plochý profil bez dominantní zdi magnet netvoří. Neznámé dominance
    # (obě None, starší data) podmínku přeskakují.
    dominances = [d for d in (now.call_wall_dom, now.put_wall_dom) if d is not None]
    if dominances and max(dominances) < params.min_wall_dominance:
        return None
    # Vyhasínání: průměr posledních 30 min pod průměrem celé dosavadní historie
    if len(history) >= 60:
        recent = [m.opt_vol for m in history[-30:]]
        overall = [m.opt_vol for m in history]
        if sum(recent) / len(recent) >= sum(overall) / len(overall):
            return None
    direction = Direction.LONG if distance < 0 else Direction.SHORT
    entry = now.close
    target = now.max_pain
    if direction is Direction.LONG:
        stop = entry - 1.5 * abs(distance)
    else:
        stop = entry + 1.5 * abs(distance)
    return SetupCandidate(
        template=SetupTemplate.MAX_PAIN_PIN,
        direction=direction,
        entry=entry,
        target=target,
        stop=stop,
        confidence=60,
        reason=(
            f"Max Pain pin: {now.minutes_to_expiry:.0f} min do expirace, cena {entry:g} "
            f"vs. Max Pain {now.max_pain:g}, opční aktivita vyhasíná."
        ),
        context={
            "max_pain": now.max_pain,
            "minutes_to_expiry": now.minutes_to_expiry,
            "wall_dom_max": max(dominances) if dominances else None,
            "gex_regime": now.gex_regime,
        },
    )


def detect_gamma_momentum(
    history: Sequence[MinuteInputs], params: SetupParams
) -> SetupCandidate | None:
    """T4: průraz flipu s Δ Flow převahou a novým extrémem Cum Δ — po směru."""
    if len(history) < params.momentum_flow_lookback + 1:
        return None
    now = history[-1]
    prev = history[-2]
    if now.flip is None:
        return None
    recent = history[-params.momentum_flow_lookback :]
    call_flow = sum(m.call_flow for m in recent)
    put_flow = sum(m.put_flow for m in recent)
    total_flow = call_flow + put_flow
    if total_flow <= 0:
        return None
    cum_window = [m.cum_delta for m in history[-30:]]

    crossed_down = prev.close >= now.flip and now.close <= now.flip - params.momentum_break
    crossed_up = prev.close <= now.flip and now.close >= now.flip + params.momentum_break
    if crossed_down:
        if put_flow / total_flow < params.momentum_flow_share:
            return None
        if now.cum_delta > min(cum_window):
            return None
        entry = now.close
        target = _nearest_level_below(entry, (now.put_wall,))
        if target is None:
            return None
        return SetupCandidate(
            template=SetupTemplate.GAMMA_MOMENTUM,
            direction=Direction.SHORT,
            entry=entry,
            target=target,
            stop=now.flip + 1.0,
            confidence=50,
            reason=(
                f"Průraz flipu {now.flip:g} dolů do záporné gammy: put strana "
                f"{put_flow / total_flow:.0%} toku, Cum Δ na minimu — dealeři zesilují."
            ),
            context={
                "flip": now.flip,
                "put_flow_share": put_flow / total_flow,
                "gex_regime": now.gex_regime,
            },
        )
    if crossed_up:
        if call_flow / total_flow < params.momentum_flow_share:
            return None
        if now.cum_delta < max(cum_window):
            return None
        entry = now.close
        target = _nearest_level_above(entry, (now.call_wall,))
        if target is None:
            return None
        return SetupCandidate(
            template=SetupTemplate.GAMMA_MOMENTUM,
            direction=Direction.LONG,
            entry=entry,
            target=target,
            stop=now.flip - 1.0,
            confidence=50,
            reason=(
                f"Průraz flipu {now.flip:g} nahoru: call strana "
                f"{call_flow / total_flow:.0%} toku, Cum Δ na maximu."
            ),
            context={
                "flip": now.flip,
                "call_flow_share": call_flow / total_flow,
                "gex_regime": now.gex_regime,
            },
        )
    return None


DETECTORS = (
    detect_failed_break,  # nejsilnější kontext první (anti-spam řeší orchestrátor)
    detect_wall_bounce,
    detect_gamma_momentum,
    detect_max_pain_pin,
)


def detect_all(history: Sequence[MinuteInputs], params: SetupParams) -> list[SetupCandidate]:
    """Vyhodnotí všechny šablony nad aktuální minutou (bez stavového anti-spamu)."""
    results = []
    for detector in DETECTORS:
        candidate = detector(history, params)
        if candidate is not None:
            results.append(candidate)
    return results


def evaluate_bar(
    direction: Direction, entry: float, target: float, stop: float, high: float, low: float
) -> Outcome | None:
    """Uzavření setupu minutovým barem; při zásahu obou úrovní konzervativně stop."""
    if direction is Direction.LONG:
        if low <= stop:
            return Outcome.STOP
        if high >= target:
            return Outcome.TARGET
    else:
        if high >= stop:
            return Outcome.STOP
        if low <= target:
            return Outcome.TARGET
    return None


def r_result(direction: Direction, entry: float, stop: float, exit_price: float) -> float:
    """Výsledek v násobcích risku (R); risk = |entry − stop|."""
    risk = abs(entry - stop)
    if risk <= 0:
        return 0.0
    move = exit_price - entry if direction is Direction.LONG else entry - exit_price
    return move / risk
