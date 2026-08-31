"""Pásmová režimová metrika (#575, fáze 1 — jen měření): ostrost a hloubka.

Referenční čtení: dvě kontury na Dyn ploše; „čím jsou čáry blíž u sebe, tím
ostřejší přechod". Tady se totéž pravidlo počítá nad VÁŽENÝM profilem minuty
($/1 %, #569 — tatáž jednotka, na které stojí kontury frontendu):

- prahy = podíly maxima kladné části profilu (Major 0,65 / All 0,40 — poměry
  z #571; reference je den-level p99, per-minutová metrika používá maximum
  téže minuty, aby byla čistou funkcí jednoho řádku),
- **tlumící zóna** = souvislý úsek kolem ceny, kde vážený profil > práh All,
- **ostrost** = vzdálenost průsečíků Major a All na hraně zóny nejbližší ceně
  (doslova „jak blízko u sebe jsou ty dvě čáry"),
- dvě normalizace (rozhodnutí uživatele 13. 8. — měřit obě, kalibrace ~31. 8.
  vybere vítěze; do té doby se #575 nezavírá):
  A. `band_sharpness` = ostrost / šířka zóny (bezrozměrná, přenositelná),
  B. `band_sharpness_pct` = ostrost jako % ceny (nejblíž čtení z obrazovky),
- `band_depth` = spojitá poloha: −1 (profil na ceně nulový) … 0 (hrana All)
  … +1 (hrana Major) … +2 (vrchol profilu).

  **Verze 2 (#952):** do verze 1 se hloubka nad hranou Major ořezávala na +1.
  Jenže setupy vznikají typicky hluboko uvnitř zóny, takže 70 % vzorku mělo
  přesně 1,0 a metrika v té oblasti nerozlišovala vůbec nic — kalibrace #575
  na ní kvůli tomu ztroskotala. Pásmo [Major, vrchol] je 35 % rozsahu profilu
  a nese většinu vzorku, takže se nově mapuje na (1, 2] místo do jediného bodu.
  Hodnoty v1 a v2 se NESMÍ míchat — proto `band_metrics_version` v contextu.

Žádná brána na hodnotách nestojí — fáze 1 je zapisuje do `context` setupů.
"""

import math
from dataclasses import dataclass

from gexlens_engine.compute.gexfield import GexProfile, price_weight_per_percent

# Poměry prahů z kontur #571 (Major/All) — sdílené konstanty čtení
BAND_MAJOR_SHARE = 0.65
BAND_ALL_SHARE = 0.40

#: Verze definice metrik (#952). Zvýšit při KAŽDÉ změně významu hodnot —
#: sdružovat napříč verzemi je stejná chyba jako sdružovat přes
#: `mechanics_version` (nález z kalibrace #575).
#: 1 = původní, hloubka ořezaná na +1 nad hranou Major
#: 2 = hloubka pokračuje k vrcholu profilu (+2)
BAND_METRICS_VERSION = 2


@dataclass(frozen=True)
class BandMetrics:
    """Měřené veličiny pásma pro `context` setupu (#575 fáze 1)."""

    sharpness: float  # varianta A: spread hran / šířka zóny (0–1)
    sharpness_pct: float  # varianta B: spread hran jako % ceny
    depth: float  # −1 … +1 (viz modul)


def _weighted(profile: GexProfile) -> list[float]:
    return [
        value * price_weight_per_percent(profile.grid_start + i * profile.grid_step)
        for i, value in enumerate(profile.values)
    ]


def _crossing(values: list[float], start_idx: int, step: int, threshold: float) -> float | None:
    """Frakční index prvního poklesu pod práh směrem `step`; None = kraj mřížky.

    Lineární interpolace mezi posledním bodem nad prahem a prvním pod ním —
    stejná přesnost, s jakou kontury kreslí frontend (marching squares).
    """
    previous = values[start_idx]
    if previous <= threshold:
        return float(start_idx)
    index = start_idx + step
    while 0 <= index < len(values):
        current = values[index]
        if current <= threshold:
            fraction = (previous - threshold) / (previous - current)
            return (index - step) + step * fraction
        previous = current
        index += step
    return None


def band_metrics(profile: GexProfile, price: float) -> BandMetrics | None:
    """Ostrost a hloubka pásma v místě ceny; None = metrika nedává smysl.

    None nastává, když profil nemá kladnou část (žádná tlumící zóna), cena
    je mimo mřížku, nebo zóna sahá až na kraj mřížky (hrana neurčitelná —
    stejný závěr jako měření #601: na kraji se neměří, nelže se).
    """
    if not profile.values or profile.grid_step <= 0:
        return None
    weighted = _weighted(profile)
    top = max(weighted)
    if top <= 0.0:
        return None
    position = (price - profile.grid_start) / profile.grid_step
    if position < 0 or position > len(weighted) - 1:
        return None
    t_major = BAND_MAJOR_SHARE * top
    t_all = BAND_ALL_SHARE * top

    low = int(position)
    frac = position - low
    at_price = weighted[low] * (1 - frac) + weighted[min(low + 1, len(weighted) - 1)] * frac

    # Hloubka (#952 v2): pod All lineárně k −1 (profil 0), nad All k +1 (Major)
    # a DÁL k +2 (vrchol profilu). Ořez na +1 sléval celé pásmo [Major, vrchol]
    # — 35 % rozsahu a většinu vzorku — do jediné hodnoty.
    if at_price <= t_all:
        depth = max(-1.0, (at_price - t_all) / t_all)
    elif at_price <= t_major:
        depth = (at_price - t_all) / (t_major - t_all)
    else:
        # top > t_major vždy (t_major = 0,65 × top a top > 0), takže se nedělí nulou
        depth = min(2.0, 1.0 + (at_price - t_major) / (top - t_major))

    # Hrany zóny: průsečíky All a Major na obou stranách od nejbližšího uzlu
    anchor = low if weighted[low] >= at_price else min(low + 1, len(weighted) - 1)
    edges: list[tuple[float, float]] = []  # (vzdálenost hrany All od ceny, spread)
    for step in (-1, 1):
        all_cross = _crossing(weighted, anchor, step, t_all)
        major_cross = _crossing(weighted, anchor, step, t_major)
        if all_cross is None or major_cross is None:
            continue  # zóna sahá na kraj mřížky — hrana neurčitelná
        edges.append((abs(all_cross - position), abs(all_cross - major_cross) * profile.grid_step))
    if not edges:
        return None
    all_low = _crossing(weighted, anchor, -1, t_all)
    all_high = _crossing(weighted, anchor, 1, t_all)
    if all_low is None or all_high is None:
        return None
    zone_width = (all_high - all_low) * profile.grid_step
    if zone_width <= 0 or price <= 0:
        return None
    spread = min(edges, key=lambda edge: edge[0])[1]  # hrana nejblíž ceně
    return BandMetrics(
        sharpness=round(spread / zone_width, 4),
        sharpness_pct=round(spread / price * 100.0, 4),
        depth=round(depth, 4),
    )


@dataclass(frozen=True)
class BandZone:
    """Geometrie tlumící zóny v CENÁCH (#577) — kotvy pro T9 probe.

    Kotví se na pásmo, ne na body ani ATR — třetí cesta, kterou hledá #434:
    šířka zóny je odvozená z profilu positioningu a nese se napříč instrumenty.
    """

    all_low: float  # spodní hrana All (price)
    all_high: float  # horní hrana All (price)
    center: float
    width: float
    #: Nejvyšší hodnota váženého profilu NAD cenou / globální maximum (0–1) —
    #: „leží jádro pásma nad hlavou?" (podmínka 2 z #577)
    strength_above: float


def band_zone(profile: GexProfile, price: float) -> BandZone | None:
    """Hrany tlumící zóny kolem/nad cenou; None = neurčitelné (kraj mřížky)."""
    if not profile.values or profile.grid_step <= 0:
        return None
    weighted = _weighted(profile)
    top = max(weighted)
    if top <= 0.0:
        return None
    position = (price - profile.grid_start) / profile.grid_step
    if position < 0 or position > len(weighted) - 1:
        return None
    t_all = BAND_ALL_SHARE * top
    low = int(position)
    anchor_idx = (
        low
        if weighted[low] >= weighted[min(low + 1, len(weighted) - 1)]
        else min(low + 1, len(weighted) - 1)
    )
    # Hrana zóny může ležet nad cenou (cena pod pásmem) — hledá se od
    # nejbližšího uzlu S nejvyšší hodnotou směrem vzhůru, pak dolů od něj
    peak_above = anchor_idx
    for index in range(anchor_idx, len(weighted)):
        if weighted[index] > weighted[peak_above]:
            peak_above = index
    start = peak_above if weighted[peak_above] > t_all else anchor_idx
    if weighted[start] <= t_all:
        return None
    all_low = _crossing(weighted, start, -1, t_all)
    all_high = _crossing(weighted, start, 1, t_all)
    if all_low is None or all_high is None:
        return None
    low_price = profile.grid_start + all_low * profile.grid_step
    high_price = profile.grid_start + all_high * profile.grid_step
    width = high_price - low_price
    if width <= 0:
        return None
    above = [value for index, value in enumerate(weighted) if index >= position]
    strength_above = max(above) / top if above else 0.0
    return BandZone(
        all_low=low_price,
        all_high=high_price,
        center=(low_price + high_price) / 2,
        width=width,
        strength_above=round(strength_above, 4),
    )


def band_context(profile: GexProfile | None, price: float) -> dict[str, float]:
    """Klíče do `context` JSON setupu; prázdný dict = nezměřeno (bez lhaní)."""
    if profile is None or not math.isfinite(price):
        return {}
    metrics = band_metrics(profile, price)
    if metrics is None:
        return {}
    return {
        "band_sharpness": metrics.sharpness,
        "band_sharpness_pct": metrics.sharpness_pct,
        "band_depth": metrics.depth,
        # Bez verze nejde poznat, kterou definicí hloubky byl řádek spočítaný,
        # a sdružovat je dohromady zkresluje výsledek (#952)
        "band_metrics_version": BAND_METRICS_VERSION,
    }
