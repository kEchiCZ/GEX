"""Sentiment waves a stav RiskOn/RiskOff/Neutral (#292, SPEC 5.6 rev. #563) — čisté funkce.

Žije v enginu ze stejného důvodu jako `newstext`: pravidla musí být JEDNA
implementace pro news-engine (job počítá a ukládá vlny) i API (route servíruje
stav) — dvě kopie by se rozešly a stav v UI by lhal proti uloženým vlnám.

Pinnutá pravidla (SPEC 5.6 rev. 2026-08-12, #563 — konfig je jen override,
jinak nejdou psát golden testy):

* **Stav = poloha denního close vůči oběma průměrům** (definovaný každý den):
  RiskOn ⇔ close nad MA5 i MA10; RiskOff ⇔ pod oběma; mezi nimi Neutral.
* **Polarita trendu = MA5 vs. MA10** (up/down) — atribut stavu, ne brána.
* Vlna = souvislé dny s řetězenou podmínkou (close > MA5 > MA10, zrcadlově);
  den bez podmínky vlnu uzavírá. **Hloubka vlny = max |close − MA10|**
  (ADR-0019). Vlna i hloubka jsou od #563 ATRIBUTY stavu, ne brána —
  potvrzovací práh se dál počítá a reportuje, ale stav negatuje.
* POZOR na čtení: názvy stavů popisují NÁLADU (na trh dopadají špatné/dobré
  zprávy), ne predikci ceny. Měření #563 na 2024–2026: následné výnosy byly
  kontrariánské (RiskOff období se vykupovala) — směr určuje až kalibrace
  signálů nad track recordem (#453), nikdy název stavu.

Vše počítáno na denních close kontinuálního SentIndexu (`sentiment_daily`);
intradenní hodnota dneška dává jen „unconfirmed" indikaci (SPEC 5.6).
"""

import datetime as dt
from dataclasses import dataclass

MA_SHORT = 5
MA_LONG = 10

RISK_ON = "RiskOn"
RISK_OFF = "RiskOff"
NEUTRAL = "Neutral"


@dataclass(frozen=True)
class DailyClose:
    """Denní close kontinuálního SentIndexu (řádek `sentiment_daily`)."""

    date: dt.date
    close: float


@dataclass(frozen=True)
class Wave:
    """Vlna dle SPEC 5.6; `end` None = probíhající."""

    direction: str  # RiskOn / RiskOff
    start: dt.date
    end: dt.date | None
    depth: float
    length_days: int


@dataclass(frozen=True)
class StateAssessment:
    """Stav k poslednímu dni řady + vstupy, ze kterých vznikl (pro UI/API)."""

    state: str  # RiskOn / RiskOff / Neutral (poloha vůči MA5+MA10, #563)
    close: float | None
    ma5: float | None
    ma10: float | None
    wave: Wave | None
    # Potvrzovací práh — od #563 jen informační atribut, stav negatuje
    threshold: float
    # Polarita trendu MA5 vs. MA10 ("up"/"down"); None dokud okna nejsou plná
    polarity: str | None = None


def moving_average(closes: list[float], window: int) -> float | None:
    """Prostý MA posledních `window` hodnot; None dokud okno není plné."""
    if len(closes) < window:
        return None
    return sum(closes[-window:]) / window


def day_condition(close: float, ma5: float | None, ma10: float | None) -> str | None:
    """Řetězená podmínka dne: RiskOn ⇔ close > MA5 > MA10, RiskOff zrcadlově.

    Od #563 definuje jen VLNY (detect_waves) — stav dne určuje `position_state`.
    Řetězení (AND polohy a polarity) pouštělo ze čtyř režimů dva, a proto stav
    spal 96,5 % dní; jako definice vlnových úseků zůstává beze změny.
    """
    if ma5 is None or ma10 is None:
        return None
    if close > ma5 > ma10:
        return RISK_ON
    if close < ma5 < ma10:
        return RISK_OFF
    return None


def position_state(close: float, ma5: float | None, ma10: float | None) -> str | None:
    """Stav dne z polohy vůči oběma průměrům (SPEC 5.6 rev. #563).

    RiskOn = close nad MA5 i MA10 (pozitivní nálada), RiskOff = pod oběma,
    Neutral = mezi průměry. None dokud MA okna nejsou plná. Ostrá nerovnost:
    close přesně na průměru polohu nepotvrzuje.
    """
    if ma5 is None or ma10 is None:
        return None
    if close > ma5 and close > ma10:
        return RISK_ON
    if close < ma5 and close < ma10:
        return RISK_OFF
    return NEUTRAL


def trend_polarity(ma5: float | None, ma10: float | None) -> str | None:
    """Polarita trendu: "up" ⇔ MA5 > MA10, "down" ⇔ MA5 < MA10; rovnost None."""
    if ma5 is None or ma10 is None or ma5 == ma10:
        return None
    return "up" if ma5 > ma10 else "down"


def detect_waves(points: list[DailyClose]) -> list[Wave]:
    """Vlny nad chronologickou řadou denních close.

    Poslední vlna zůstává otevřená (`end=None`), pokud podmínka platí i
    v poslední den řady — uzavře ji až den, kdy podmínka spadne.
    """
    waves: list[Wave] = []
    current_direction: str | None = None
    current_start: dt.date | None = None
    current_depth = 0.0
    current_length = 0
    last_condition_date: dt.date | None = None

    def close_current(end: dt.date | None) -> None:
        nonlocal current_direction, current_start, current_depth, current_length
        if current_direction is not None and current_start is not None:
            waves.append(
                Wave(
                    direction=current_direction,
                    start=current_start,
                    end=end,
                    depth=current_depth,
                    length_days=current_length,
                )
            )
        current_direction = None
        current_start = None
        current_depth = 0.0
        current_length = 0

    closes: list[float] = []
    for point in points:
        closes.append(point.close)
        ma5 = moving_average(closes, MA_SHORT)
        ma10 = moving_average(closes, MA_LONG)
        condition = day_condition(point.close, ma5, ma10)
        if condition is None:
            close_current(last_condition_date)
            continue
        if condition != current_direction:
            close_current(last_condition_date)
            current_direction = condition
            current_start = point.date
        if ma10 is None:  # nemůže nastat — condition by byla None; guard pro typy
            continue
        current_depth = max(current_depth, abs(point.close - ma10))
        current_length += 1
        last_condition_date = point.date

    close_current(None)
    return waves


def opposite(direction: str) -> str:
    return RISK_OFF if direction == RISK_ON else RISK_ON


def confirmation_threshold(waves: list[Wave], *, direction: str, before: dt.date) -> float:
    """Průměrná hloubka dokončených vln opačného směru ukončených před `before`.

    Walk-forward (SPEC 5.6): stav dne D smí kalibrovat jen historie, která
    v den D existovala. Bez historie 0 — stav pak plyne čistě z MA podmínky.
    """
    depths = [
        wave.depth
        for wave in waves
        if wave.direction == opposite(direction) and wave.end is not None and wave.end < before
    ]
    if not depths:
        return 0.0
    return sum(depths) / len(depths)


def assess_state(points: list[DailyClose]) -> StateAssessment:
    """Stav k poslednímu dni řady dle pinnutých pravidel SPEC 5.6 (rev. #563).

    Stav = poloha close vůči oběma průměrům — definovaný každý den, žádná
    vlnová brána (ta držela Neutral 96,5 % dní a signální větev spala).
    Vlna, hloubka i potvrzovací práh zůstávají jako atributy pro UI/kalibraci.
    """
    if not points:
        return StateAssessment(
            state=NEUTRAL, close=None, ma5=None, ma10=None, wave=None, threshold=0.0
        )
    closes = [point.close for point in points]
    ma5 = moving_average(closes, MA_SHORT)
    ma10 = moving_average(closes, MA_LONG)
    last = points[-1]
    waves = detect_waves(points)
    ongoing = waves[-1] if waves and waves[-1].end is None else None
    state = position_state(last.close, ma5, ma10) or NEUTRAL
    threshold = (
        confirmation_threshold(waves, direction=ongoing.direction, before=ongoing.start)
        if ongoing is not None
        else 0.0
    )
    return StateAssessment(
        state=state,
        close=last.close,
        ma5=ma5,
        ma10=ma10,
        wave=ongoing,
        threshold=threshold,
        polarity=trend_polarity(ma5, ma10),
    )
