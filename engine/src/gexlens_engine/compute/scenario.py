"""Scénář dne (#1173, #1126 bod 3a) — čisté výpočty: cíle z geometrie a vyhodnocení.

Scénář = nakreslená očekávaná cesta ceny (anotace) s cíli v pořadí a termínem.
Vzniká jen dopředu (živý den, termín dnes nebo v budoucnu); vyhodnocení bere
výhradně bary PO vzniku a do termínu — track record nemůže obsahovat scénář
nakreslený se znalostí výsledku (rozhodnutí uživatele 15. 9. 2026).

Vyhodnocení (stejný duch jako verdikt dne #1091 — měřit, ne dojem):
- `hit1` / `hit2`: cena se dotkla cíle 1 / cíle 2 (high ≥ cíl nad vstupem,
  low ≤ cíl pod vstupem); vstup = spot v okamžiku vzniku.
- `hit2` se hledá až po dotyku cíle 1 (cesta má pořadí); `order_ok` = True
  když cíl 2 přišel po cíli 1, False když padl jen před ním (nebo bez cíle 1),
  None když se nedá soudit. Bez cíle 2 se nehodnotí.
- `max_dev_pts`: největší odchylka close od scénářové cesty (lineárně
  interpolované body anotace v čase); `max_dev_em` totéž v násobcích EM dne
  vzniku (None bez EM).
- `verdict`: `hit` (cíl 1 a — je-li — cíl 2 po něm), `partial` (cíl 1 ano,
  cíl 2 po něm ne), `miss` (cíl 1 ne).
"""

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class Bar:
    ts: dt.datetime
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class PathPoint:
    """Bod scénářové cesty: absolutní čas × cena (z anotace minuta × strike)."""

    ts: dt.datetime
    price: float


@dataclass(frozen=True)
class ScenarioResult:
    hit1: bool
    hit2: bool | None
    order_ok: bool | None
    hit1_ts: dt.datetime | None
    hit2_ts: dt.datetime | None
    max_dev_pts: float | None
    max_dev_em: float | None
    bars: int
    verdict: str

    def as_dict(self) -> dict[str, object]:
        return {
            "hit1": self.hit1,
            "hit2": self.hit2,
            "order_ok": self.order_ok,
            "hit1_ts": self.hit1_ts.isoformat() if self.hit1_ts else None,
            "hit2_ts": self.hit2_ts.isoformat() if self.hit2_ts else None,
            "max_dev_pts": self.max_dev_pts,
            "max_dev_em": self.max_dev_em,
            "bars": self.bars,
            "verdict": self.verdict,
        }


def targets_from_path(path: list[PathPoint], entry: float, *, limit: int = 3) -> list[float]:
    """Cílové hladiny z geometrie v pořadí: obraty cesty + koncový bod.

    Obrat = lokální extrém ceny podél času (směr se otočí); koncový bod je
    vždy poslední cíl. Cíle blíž než 0,05 % ceny od předchozího se slučují —
    freehand tah se chvěje a každý zub by byl „cíl". Max `limit` cílů.
    """
    if not path:
        return []
    ordered = sorted(path, key=lambda point: point.ts)
    prices = [point.price for point in ordered]
    targets: list[float] = []
    tolerance = abs(entry) * 0.0005 if entry else 0.0
    direction = 0
    extreme = prices[0]  # extrém od poslední změny směru — obrat = jeho hodnota
    for index in range(1, len(prices)):
        price = prices[index]
        delta = price - extreme
        if abs(delta) <= tolerance:
            continue
        step = 1 if delta > 0 else -1
        turned = bool(direction) and step != direction
        if turned and (not targets or abs(extreme - targets[-1]) > tolerance):
            targets.append(extreme)
        direction = step
        extreme = price
    end = prices[-1]
    if not targets or abs(end - targets[-1]) > tolerance:
        targets.append(end)
    return [round(value, 2) for value in targets[:limit]]


def _first_touch(
    bars: list[Bar], target: float, above: bool, start_index: int = 0
) -> tuple[int, dt.datetime] | None:
    for index in range(start_index, len(bars)):
        bar = bars[index]
        if (above and bar.high >= target) or (not above and bar.low <= target):
            return index, bar.ts
    return None


def _path_price_at(path: list[PathPoint], ts: dt.datetime) -> float | None:
    """Lineární interpolace scénářové cesty v čase; mimo rozsah None."""
    if not path:
        return None
    if ts <= path[0].ts:
        return path[0].price if ts == path[0].ts else None
    for index in range(1, len(path)):
        left, right = path[index - 1], path[index]
        if ts <= right.ts:
            span = (right.ts - left.ts).total_seconds()
            if span <= 0:
                return right.price
            fraction = (ts - left.ts).total_seconds() / span
            return left.price + fraction * (right.price - left.price)
    return None


def evaluate_scenario(
    bars: list[Bar],
    *,
    entry: float,
    targets: list[float],
    path: list[PathPoint],
    em_points: float | None,
) -> ScenarioResult | None:
    """Vyhodnocení nad barami PO vzniku scénáře (volající je už odfiltroval).

    None = žádné bary (scénář nešlo posoudit); jinak vždy výsledek.
    """
    if not bars or not targets:
        return None
    ordered = sorted(bars, key=lambda bar: bar.ts)
    target1 = targets[0]
    above1 = target1 >= entry
    touch1 = _first_touch(ordered, target1, above1)
    hit1 = touch1 is not None
    hit2: bool | None = None
    order_ok: bool | None = None
    hit2_ts: dt.datetime | None = None
    if len(targets) > 1:
        # Cíl 2 se hledá až PO cíli 1 (cesta „7680, pak 7600" — návrat ke
        # vstupu by jinak platil hned první minutou). Bez cíle 1 se hledá
        # v celém okně jen jako důkaz, že pořadí neplatilo.
        target2 = targets[1]
        above2 = target2 >= target1
        after = _first_touch(ordered, target2, above2, touch1[0]) if touch1 else None
        early = _first_touch(ordered, target2, target2 >= entry)
        if touch1:
            hit2 = after is not None
            hit2_ts = after[1] if after else None
            if after is not None:
                order_ok = True
            elif early is not None and early[0] < touch1[0]:
                order_ok = False  # cíl 2 padl dřív než cíl 1 a po něm už ne
        else:
            hit2 = early is not None
            hit2_ts = early[1] if early else None
            order_ok = False if early is not None else None
    sorted_path = sorted(path, key=lambda point: point.ts)
    max_dev: float | None = None
    for bar in ordered:
        expected = _path_price_at(sorted_path, bar.ts)
        if expected is None:
            continue
        deviation = abs(bar.close - expected)
        max_dev = deviation if max_dev is None else max(max_dev, deviation)
    max_dev_em = (
        max_dev / em_points if max_dev is not None and em_points and em_points > 0 else None
    )
    if not hit1:
        verdict = "miss"
    elif len(targets) > 1 and not hit2:
        verdict = "partial"
    else:
        verdict = "hit"
    return ScenarioResult(
        hit1=hit1,
        hit2=hit2,
        order_ok=order_ok,
        hit1_ts=touch1[1] if touch1 else None,
        hit2_ts=hit2_ts,
        max_dev_pts=round(max_dev, 2) if max_dev is not None else None,
        max_dev_em=round(max_dev_em, 3) if max_dev_em is not None else None,
        bars=len(ordered),
        verdict=verdict,
    )
