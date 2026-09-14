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
from collections.abc import Callable
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


# ── Korekční epizody (#565, ADR-0037) ──────────────────────────────────────
#
# Vrstva VEDLE vln a stavu, ne jejich náhrada: vlna trvá Ø 1,5 dne (šum),
# epizoda je korekce nálady měřená jako pokles pod klouzavé maximum v σ.
# Parametry jsou PLACEHOLDER z prvního měření (data/reports/
# sentiment-episodes-2026-09-14.md): σ(100) byla do ~11/2026 kontaminovaná
# backfillem, takže D z gridu {0,5…3} nic nerozlišovalo — 1 σ nic nekazí,
# ale není kalibrace. Změna = nová `EPISODE_PARAMS_VERSION` + full-replace
# přepočet (WavesJob), historie se nemíchá.

EPISODE_PARAMS_VERSION = 1
# Práh D (σ): pokles close_z pod 20denní maximum, který epizodu zakládá
EPISODE_THRESHOLD_D = 1.0
# Horizont H (obchodní dny): do kdy se korekce musí zahladit, aby byla „pokus"
EPISODE_HORIZON_H = 10
# Okno klouzavého maxima close_z (řádky `sentiment_daily`, tj. kalendářní dny)
EPISODE_MAX_WINDOW = 20
EPISODE_SERIES_VARIANT = "zscore_100"

EPISODE_ATTEMPT = "attempt"
EPISODE_NEGATION = "negation"
# Probíhající epizoda: třída je známá až dnem zahlazení nebo horizontem
EPISODE_OPEN = "open"
EPISODE_NONE = "none"


@dataclass(frozen=True)
class DailyZ:
    """Denní close SentIndexu se škálou #640 (řádek `sentiment_daily` s close_z)."""

    date: dt.date
    close: float
    z: float | None


@dataclass(frozen=True)
class Episode:
    """Korekční epizoda (#565): `end` None = probíhá, `label` None = nerozhodnuto."""

    start: dt.date
    end: dt.date | None
    # Referenční úroveň = 20denní maximum close_z v den začátku (σ)
    ref_level: float
    # Největší pokles pod referenční úroveň během epizody (σ)
    depth_z: float
    label: str | None  # EPISODE_ATTEMPT / EPISODE_NEGATION / None
    # Obchodní dny od začátku do rozhodnutí (u probíhající: dosud)
    length_days: int


@dataclass(frozen=True)
class EpisodeAssessment:
    """Epizodová část stavu k poslednímu dni řady (pro API/WS)."""

    # Třída aktuální epizody: attempt / negation / open / none
    status: str
    episode: Episode | None
    # Poslední rozhodnutá epizoda (attempt/negation) — pro tooltip a Stats
    last_resolved: Episode | None
    # Aktuální práh v σ: 20denní maximum close_z − D; None bez close_z
    correction_threshold: float | None
    threshold_d: float = EPISODE_THRESHOLD_D
    horizon_h: int = EPISODE_HORIZON_H
    params_version: int = EPISODE_PARAMS_VERSION


def is_weekday(day: dt.date) -> bool:
    """Výchozí obchodní den = pondělí–pátek (svátky se NEgatují; rozdíl proti
    skutečným seancím podkladu je nejvýš den kolem svátku, viz ADR-0037)."""
    return day.weekday() < 5


def rolling_max_z(points: list[DailyZ], window: int = EPISODE_MAX_WINDOW) -> list[float | None]:
    """Klouzavé maximum close_z posledních `window` řádků včetně aktuálního.

    None, dokud v okně chybí byť jediná hodnota (začátek řady bez σ) —
    částečné okno by dávalo falešně nízké maximum a tím falešný start.
    """
    values = [point.z for point in points]
    out: list[float | None] = []
    for index, value in enumerate(values):
        chunk = values[max(0, index - window + 1) : index + 1]
        if value is None or any(v is None for v in chunk):
            out.append(None)
        else:
            out.append(max(v for v in chunk if v is not None))
    return out


def correction_levels(
    points: list[DailyZ],
    *,
    threshold_d: float = EPISODE_THRESHOLD_D,
    window: int = EPISODE_MAX_WINDOW,
) -> list[float | None]:
    """Práh korekce per den v σ: klouzavé maximum − D (linie v UI, #565 bod 9)."""
    return [None if peak is None else peak - threshold_d for peak in rolling_max_z(points, window)]


def detect_episodes(
    points: list[DailyZ],
    *,
    threshold_d: float = EPISODE_THRESHOLD_D,
    horizon_h: int = EPISODE_HORIZON_H,
    window: int = EPISODE_MAX_WINDOW,
    is_trading_day: Callable[[dt.date], bool] = is_weekday,
) -> list[Episode]:
    """Epizody nad chronologickou řadou (pinnutá definice, ADR-0037).

    * Start = den, kdy close_z klesne pod klouzavé maximum o ≥ D σ. Další
      start až po „odjištění" (drawdown < D) — jinak by negace zakládala
      epizodu každý den. Epizody se nepřekrývají: nový start až po dni
      rozhodnutí předchozí.
    * Zahlazení (pokus) = close_z ZPĚT NAD referenční úrovní (maximum v den
      startu), hodnoceno od dne po startu. Varianta „nad úroveň startu nebo nad
      MA10" byla při měření degenerovaná (0 negací) — nepoužívá se.
    * Negace = bez zahlazení do H obchodních dní; rozhodnutí = H-tý obchodní
      den po startu. Řada s méně dny = epizoda probíhá (`end` None).
    """
    maxima = rolling_max_z(points, window)
    episodes: list[Episode] = []
    armed = True
    index = 0
    while index < len(points):
        point = points[index]
        peak = maxima[index]
        if point.z is None or peak is None:
            index += 1
            continue
        drawdown = peak - point.z
        if drawdown < threshold_d:
            armed = True
            index += 1
            continue
        if not armed:
            index += 1
            continue
        episode, next_index = _resolve_episode(
            points, index, peak, horizon_h=horizon_h, is_trading_day=is_trading_day
        )
        episodes.append(episode)
        # Den rozhodnutí se projde znovu jen kvůli odjištění (zahlazení = nové
        # maximum, drawdown 0) — nový start je v něm vyloučený (armed=False)
        armed = False
        index = next_index
    return episodes


def _resolve_episode(
    points: list[DailyZ],
    start_index: int,
    ref_level: float,
    *,
    horizon_h: int,
    is_trading_day: Callable[[dt.date], bool],
) -> tuple[Episode, int]:
    """(epizoda, index dne rozhodnutí; u probíhající délka řady)."""
    start = points[start_index]
    start_z = start.z if start.z is not None else ref_level
    depth = max(0.0, ref_level - start_z)
    trading_days = 0
    for index in range(start_index + 1, len(points)):
        point = points[index]
        if is_trading_day(point.date):
            trading_days += 1
        if point.z is not None:
            depth = max(depth, ref_level - point.z)
            if point.z > ref_level:
                return (
                    Episode(
                        start=start.date,
                        end=point.date,
                        ref_level=ref_level,
                        depth_z=depth,
                        label=EPISODE_ATTEMPT,
                        length_days=trading_days,
                    ),
                    index,
                )
        if trading_days >= horizon_h:
            return (
                Episode(
                    start=start.date,
                    end=point.date,
                    ref_level=ref_level,
                    depth_z=depth,
                    label=EPISODE_NEGATION,
                    length_days=trading_days,
                ),
                index,
            )
    return (
        Episode(
            start=start.date,
            end=None,
            ref_level=ref_level,
            depth_z=depth,
            label=None,
            length_days=trading_days,
        ),
        len(points),
    )


def assess_episode(
    points: list[DailyZ],
    *,
    threshold_d: float = EPISODE_THRESHOLD_D,
    horizon_h: int = EPISODE_HORIZON_H,
    window: int = EPISODE_MAX_WINDOW,
    is_trading_day: Callable[[dt.date], bool] = is_weekday,
) -> EpisodeAssessment:
    """Epizodový stav k poslednímu dni řady.

    `status`: `open` = epizoda probíhá; `negation` = poslední epizoda skončila
    negací a close_z se od té doby nevrátil nad její referenční úroveň
    (korekce trvá); `attempt` = zahlazeno v poslední den řady; jinak `none`.
    Pokus se tedy hlásí jen v den zahlazení — poté je korekce pryč a stav
    nemá co tvrdit; historie zůstává v `last_resolved` a tabulce epizod.
    """
    episodes = detect_episodes(
        points,
        threshold_d=threshold_d,
        horizon_h=horizon_h,
        window=window,
        is_trading_day=is_trading_day,
    )
    levels = correction_levels(points, threshold_d=threshold_d, window=window)
    threshold = levels[-1] if levels else None
    resolved = [episode for episode in episodes if episode.label is not None]
    last_resolved = resolved[-1] if resolved else None
    if not episodes:
        return EpisodeAssessment(
            status=EPISODE_NONE,
            episode=None,
            last_resolved=None,
            correction_threshold=threshold,
            threshold_d=threshold_d,
            horizon_h=horizon_h,
        )
    last = episodes[-1]
    last_date = points[-1].date
    status = EPISODE_NONE
    current: Episode | None = None
    if last.end is None:
        status, current = EPISODE_OPEN, last
    elif last.label == EPISODE_ATTEMPT and last.end == last_date:
        status, current = EPISODE_ATTEMPT, last
    elif last.label == EPISODE_NEGATION:
        recovered = any(
            point.z is not None and point.z > last.ref_level
            for point in points
            if last.end is not None and point.date > last.end
        )
        if not recovered:
            status, current = EPISODE_NEGATION, last
    return EpisodeAssessment(
        status=status,
        episode=current,
        last_resolved=last_resolved,
        correction_threshold=threshold,
        threshold_d=threshold_d,
        horizon_h=horizon_h,
    )
