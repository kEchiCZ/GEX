"""Trend napříč timeframy — port `frontend/src/instrument/trend.ts` (#1089) pro engine.

Stejná pravidla jako karta Trend v Briefingu, ať automatický scénář (#1173 A)
hlasuje shodně s tím, co uživatel vidí: struktura z fraktálových pivotů
(HH/HL vs. LH/LL) + EMA20/EMA50 se sklonem; vyšší TF (W, D) určuje směr,
nižší (4h, 1h, 15m) načasování. Čisté funkce; svíčky přicházejí z API
`/candles/{symbol}?tf=` (tytéž, které čte frontend).
"""

from collections import Counter
from dataclasses import dataclass
from typing import Literal

Direction = Literal["up", "down", "range"]

TIMEFRAMES: tuple[str, ...] = ("W", "D", "240", "60", "15")
HIGHER_TIMEFRAMES: tuple[str, ...] = ("W", "D")
LOWER_TIMEFRAMES: tuple[str, ...] = ("240", "60", "15")
PIVOT_WIDTH: dict[str, int] = {"W": 2, "D": 3, "240": 3, "60": 3, "15": 3}
MIN_CANDLES = 20
EMA_FAST = 20
EMA_SLOW = 50
SLOPE_LOOKBACK = 5


@dataclass(frozen=True)
class Candle:
    high: float
    low: float
    close: float
    partial: bool = False


@dataclass(frozen=True)
class TimeframeTrend:
    tf: str
    candles: int
    direction: Direction | None
    strength: str | None


@dataclass(frozen=True)
class TrendReport:
    higher: Direction | None
    lower: Direction | None
    expected: Direction | None
    by_timeframe: tuple[TimeframeTrend, ...]


def ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    seed = sum(values[:period]) / period
    result = [seed]
    for value in values[period:]:
        result.append(value * k + result[-1] * (1 - k))
    return result


def find_pivots(candles: list[Candle], width: int) -> list[tuple[int, float, str]]:
    pivots: list[tuple[int, float, str]] = []
    for i in range(width, len(candles) - width):
        is_high = True
        is_low = True
        for offset in range(1, width + 1):
            left = candles[i - offset]
            right = candles[i + offset]
            if candles[i].high <= left.high or candles[i].high <= right.high:
                is_high = False
            if candles[i].low >= left.low or candles[i].low >= right.low:
                is_low = False
            if not is_high and not is_low:
                break
        if is_high:
            pivots.append((i, candles[i].high, "high"))
        if is_low:
            pivots.append((i, candles[i].low, "low"))
    return pivots


def structure_direction(pivots: list[tuple[int, float, str]]) -> Direction | None:
    highs = [p for p in pivots if p[2] == "high"]
    lows = [p for p in pivots if p[2] == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return None
    prev_high, last_high = highs[-2], highs[-1]
    prev_low, last_low = lows[-2], lows[-1]
    if last_high[1] > prev_high[1] and last_low[1] > prev_low[1]:
        return "up"
    if last_high[1] < prev_high[1] and last_low[1] < prev_low[1]:
        return "down"
    return "range"


def ema_direction(candles: list[Candle]) -> Direction | None:
    if len(candles) < MIN_CANDLES:
        return None
    closes = [c.close for c in candles]
    fast = ema(closes, EMA_FAST)
    slow = ema(closes, EMA_SLOW) if len(closes) >= EMA_SLOW else []
    ema20 = fast[-1]
    ema50 = slow[-1] if slow else None
    lookback = min(SLOPE_LOOKBACK, len(fast) - 1)
    slope = ema20 - fast[-1 - lookback]
    price = closes[-1]
    price_above_fast = price > ema20
    fast_above_slow = None if ema50 is None else ema20 > ema50
    if price_above_fast and slope > 0 and fast_above_slow is not False:
        return "up"
    if not price_above_fast and slope < 0 and fast_above_slow is not True:
        return "down"
    return "range"


def assess_timeframe(tf: str, candles: list[Candle]) -> TimeframeTrend:
    if len(candles) < MIN_CANDLES:
        return TimeframeTrend(tf=tf, candles=len(candles), direction=None, strength=None)
    closed = [c for c in candles if not c.partial]
    from_structure = structure_direction(find_pivots(closed, PIVOT_WIDTH.get(tf, 3)))
    from_ema = ema_direction(candles)
    direction: Direction | None = None
    strength: str | None = None
    if from_structure is not None and from_structure != "range" and from_structure == from_ema:
        direction, strength = from_structure, "strong"
    elif from_structure is not None and from_structure != "range" and from_ema == "range":
        direction, strength = from_structure, "weak"
    elif (from_structure is None or from_structure == "range") and from_ema is not None:
        direction = from_ema
        strength = None if from_ema == "range" else "weak"
    elif from_structure is not None and from_ema is not None:
        direction, strength = "range", None
    return TimeframeTrend(tf=tf, candles=len(candles), direction=direction, strength=strength)


def _majority(directions: list[Direction | None]) -> Direction | None:
    decided = [d for d in directions if d is not None]
    if not decided:
        return None
    counts = Counter(decided)
    up, down, rng = counts["up"], counts["down"], counts["range"]
    if up > down and up >= rng:
        return "up"
    if down > up and down >= rng:
        return "down"
    return "range"


def expected_direction(higher: Direction | None, lower: Direction | None) -> Direction | None:
    if higher is None:
        return None if lower is None or lower == "range" else lower
    if higher == "range":
        return None
    return higher


def assess_trends(candles_by_tf: dict[str, list[Candle]]) -> TrendReport:
    rows = tuple(assess_timeframe(tf, candles_by_tf.get(tf, [])) for tf in TIMEFRAMES)
    by_tf = {row.tf: row for row in rows}
    week = by_tf["W"].direction
    day = by_tf["D"].direction
    higher = week if day is None else day
    lower = _majority([by_tf[tf].direction for tf in LOWER_TIMEFRAMES])
    return TrendReport(
        higher=higher, lower=lower, expected=expected_direction(higher, lower), by_timeframe=rows
    )
