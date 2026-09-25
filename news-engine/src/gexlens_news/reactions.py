"""Měření reakce trhu na zprávu (#276, SPEC 5.1) — čisté funkce bez I/O.

Pro každý event a symbol se měří okna +1/+5/+15/+60 min: návrat v bps, rozpětí
a objemové z-score. Dvě věci jsou tu podstatnější než samotný výpočet, protože
na nich stojí „systém se nesmí učit šum":

* **Kontaminace** — když do okna spadne jiný event s importance ≥ 2, nejde
  přiřadit pohyb jedné zprávě. Takové okno se označí a do trénovacích statistik
  nevstupuje. Bez toho by se všem headlines z Fed day přičetl tentýž pohyb.
* **Deferred** — u zprávy, která přišla při zavřeném trhu (víkend, svátek), se
  okna měří od prvního obchodovaného baru, ale **základní cena zůstává poslední
  před uzavřením**, takže `ret_bp` zahrnuje gap. Přesně to je otázka „co
  víkendové titulky dělají s open". Dynamika gapu je jiná než okamžité reakce,
  proto deferred okna tvoří v modelu vlastní buckety.

Upozornění na mimořádnou reakci (#1291, ADR-0043) měří jinou veličinu než
`news_reactions`: **maximální výchylku** (high/low) v okně od začátku shluku
zpráv, ne close-to-close. Close-to-close míjí whipsaw — FOMC 16. 9. 2026 dal
ES za 5 min −0,3 bp, přitom výchylka byla −19 bp. „Normální pohyb" je rozdělení
výchylek ve stejné denní době za posledních 20 seancí a zároveň po normalizaci
volatilitou poslední hodiny (režim). Zavřený trh se neměří — výchylka
potřebuje bar minuty před zprávou, takže gap na otevření se nezapočte.
"""

import datetime as dt
import math
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from gexlens_engine.compute.settle import ET_TZ

# Okna dle SPEC 5.1; primární pro hit-rate je +5 min (konfig. per kategorie)
DEFAULT_WINDOWS = (1, 5, 15, 60)
# Denní okna (#564): horizonty v OBCHODNÍCH dnech — referenční efekt zprávy
# žije dny až dva týdny, minutová okna ho nevidí. Do `news_reactions.window_min`
# se ukládají jako N × 1440 (1440/2880/7200/14400) — hodnota je identifikátor
# okna, ne doslovný počet minut.
DAILY_WINDOW_DAYS = (1, 2, 5, 10)
MINUTES_PER_TRADING_DAY = 1440
# Mezera mezi zprávou a prvním obchodovaným barem, od které jde o deferred.
# Běžná díra v datech (jeden chybějící bar) se tím nezamění za zavřený trh.
DEFERRED_GAP_MINUTES = 5
# Minimální počet seancí pro objemovou baseline (SPEC 5.1 mluví o 20)
MIN_BASELINE_SESSIONS = 20
# Minimální počet vzorků pro JEDNU minutu dne uvnitř baseline (#1001): svátky,
# zkrácené seance a díry v datech některé minuty ošidí — vyžadovat všech 20
# znamenalo, že vol_z nikdy nevznikl. Polovina seancí dává rozptyl, který
# ještě něco znamená; méně už ne.
MIN_MINUTE_SAMPLES = MIN_BASELINE_SESSIONS // 2

# ── Výchylka a její baseline (#1291, ADR-0043) ─────────────────────
#: Volatilitní režim = σ minutových log výnosů za poslední hodinu před startem
SIGMA_LOOKBACK_MIN = 60
#: Minimum výnosů pro σ — díra v datech nesmí dát σ ze tří minut
SIGMA_MIN_SAMPLES = 45
#: Denní doba = minuta dne v New Yorku ± 30 min (pás vyhladí jednotlivé minuty)
TOD_HALF_WIDTH_MIN = 30
#: Pod tolik vzorků v pásu se o mimořádnosti nerozhoduje
TOD_MIN_SAMPLES = 200
#: Mimořádná výchylka = nad 97. percentilem denní doby i po normalizaci σ
TOD_QUANTILE = 0.97

_MINUTE = dt.timedelta(minutes=1)


@dataclass(frozen=True)
class Bar:
    ts: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class VolumeBaseline:
    """Průměr a rozptyl objemu pro danou minutu dne přes N seancí."""

    mean: float
    variance: float
    sessions: int


@dataclass(frozen=True)
class Reaction:
    """Reakce v jednom okně — odpovídá řádku `news_reactions`."""

    window_min: int
    ret_bp: float
    range_bp: float
    vol_z: float | None
    contaminated: bool
    deferred: bool


def _last_before(bars: Sequence[Bar], moment: dt.datetime) -> Bar | None:
    """Poslední bar uzavřený před `moment` — cena, než zpráva dorazila."""
    found: Bar | None = None
    for bar in bars:
        if bar.ts < moment:
            found = bar
        else:
            break
    return found


def _first_at_or_after(bars: Sequence[Bar], moment: dt.datetime) -> Bar | None:
    for bar in bars:
        if bar.ts >= moment:
            return bar
    return None


def volume_z_score(
    window_bars: Sequence[Bar], baseline: Mapping[dt.time, VolumeBaseline] | None
) -> float | None:
    """Z-score objemu okna vůči stejné denní době.

    Očekávaná hodnota je součet minutových průměrů, σ odmocnina ze součtu
    rozptylů — tedy s předpokladem nezávislosti minut. Je to aproximace, ale
    poctivější než porovnávat objem okna proti průměru jediné minuty.

    None = baseline chybí nebo je příliš krátká; radši žádná hodnota než
    hodnota spočtená z pár dní (SPEC 5.1 chce 20 seancí).
    """
    if not baseline or not window_bars:
        return None
    mean_total = 0.0
    variance_total = 0.0
    for bar in window_bars:
        stats = baseline.get(bar.ts.timetz().replace(tzinfo=None))
        if stats is None or stats.sessions < MIN_MINUTE_SAMPLES:
            return None
        mean_total += stats.mean
        variance_total += stats.variance
    if variance_total <= 0:
        return None
    actual = sum(bar.volume for bar in window_bars)
    return (actual - mean_total) / math.sqrt(variance_total)


def build_volume_baseline(
    bars_by_session: Sequence[Sequence[Bar]],
) -> dict[dt.time, VolumeBaseline]:
    """Průměr/rozptyl objemu per minuta dne přes seance (SPEC 5.1).

    Normalizace vůči stejné denní minutě odfiltruje session efekty — objem
    v 15:30 UTC (US open) není srovnatelný s objemem ve 3:00.
    """
    buckets: dict[dt.time, list[float]] = {}
    for session in bars_by_session:
        for bar in session:
            buckets.setdefault(bar.ts.time(), []).append(bar.volume)
    baseline: dict[dt.time, VolumeBaseline] = {}
    for minute, volumes in buckets.items():
        n = len(volumes)
        mean = sum(volumes) / n
        variance = sum((v - mean) ** 2 for v in volumes) / n if n > 1 else 0.0
        baseline[minute] = VolumeBaseline(mean=mean, variance=variance, sessions=n)
    return baseline


def compute_reactions(
    event_ts: dt.datetime,
    bars: Sequence[Bar],
    *,
    windows: Sequence[int] = DEFAULT_WINDOWS,
    other_event_ts: Sequence[dt.datetime] = (),
    baseline: Mapping[dt.time, VolumeBaseline] | None = None,
    deferred_gap_minutes: int = DEFERRED_GAP_MINUTES,
) -> list[Reaction]:
    """Reakce ve všech oknech; prázdný seznam = není z čeho měřit.

    `bars` musí být seřazené a pokrývat okolí události (před i po).
    `other_event_ts` jsou časy ostatních eventů s importance ≥ 2 — kontaminace.
    """
    ordered = sorted(bars, key=lambda bar: bar.ts)
    base = _last_before(ordered, event_ts)
    first_traded = _first_at_or_after(ordered, event_ts)
    if base is None or first_traded is None or base.close <= 0:
        return []

    gap = first_traded.ts - event_ts
    deferred = gap >= dt.timedelta(minutes=deferred_gap_minutes)
    # Deferred: okna běží od prvního obchodovaného baru, ale základní cena
    # zůstává poslední před uzavřením → ret_bp zahrnuje gap na open
    start = first_traded.ts if deferred else event_ts

    results: list[Reaction] = []
    for window in windows:
        end = start + dt.timedelta(minutes=window)
        in_window = [bar for bar in ordered if start <= bar.ts < end]
        if not in_window:
            continue
        last = in_window[-1]
        ret_bp = (last.close - base.close) / base.close * 10_000
        range_bp = (
            (max(bar.high for bar in in_window) - min(bar.low for bar in in_window))
            / base.close
            * 10_000
        )
        contaminated = any(event_ts < other < end for other in other_event_ts)
        results.append(
            Reaction(
                window_min=window,
                ret_bp=ret_bp,
                range_bp=range_bp,
                vol_z=volume_z_score(in_window, baseline),
                contaminated=contaminated,
                deferred=deferred,
            )
        )
    return results


@dataclass(frozen=True)
class SessionDaily:
    """Denní agregát jedné Globex seance pro denní okna (#564).

    `close` = poslední bar ≤ settle seance (settle close), `high`/`low` přes
    bary seance do settle. Seance existuje jen tam, kde jsou bary — svátky
    řeší data, ne kalendář (ADR-0023 bod 4).
    """

    day: dt.date
    settle_ts: dt.datetime
    close: float
    high: float
    low: float


def compute_daily_reactions(
    event_ts: dt.datetime,
    bars: Sequence[Bar],
    sessions: Sequence[SessionDaily],
    *,
    window_days: Sequence[int] = DAILY_WINDOW_DAYS,
    deferred_gap_minutes: int = DEFERRED_GAP_MINUTES,
) -> list[Reaction]:
    """Reakce v denních oknech (#564); vrací jen okna, která už šla uzavřít.

    Definice okna: **1d = nejbližší settle po události**, Nd = o N−1 seancí
    dál. Ranní zpráva má 1d ke svému dennímu close, zpráva po settle k close
    následující seance. Základní cena = poslední bar před událostí (u zavřeného
    trhu včetně gapu — stejná konvence jako minutová okna).

    Vědomé odchylky od minutových oken (dokumentované v SPEC 5.1):
    * `contaminated` je vždy False — v denním horizontu spadne do okna prakticky
      vždy jiný event; denní okna měří režimovou odpověď bucketu, ne izolovanou
      zprávu, a kontaminační filtr by je vyprázdnil celé.
    * `vol_z` je None — objemová baseline per minuta dne nemá pro denní
      horizont smysl.
    * `range_bp` se počítá ze session high/low zúčastněných seancí — první
      seance zahrnuje i pohyb před událostí (aproximace).
    """
    ordered = sorted(bars, key=lambda bar: bar.ts)
    base = _last_before(ordered, event_ts)
    first_traded = _first_at_or_after(ordered, event_ts)
    if base is None or first_traded is None or base.close <= 0:
        return []
    deferred = first_traded.ts - event_ts >= dt.timedelta(minutes=deferred_gap_minutes)

    ahead = [session for session in sessions if session.settle_ts > event_ts]
    results: list[Reaction] = []
    for days in window_days:
        if len(ahead) < days:
            continue  # okno ještě neuzavřené (nebo konec archivu) — příště
        involved = ahead[:days]
        end = involved[-1]
        ret_bp = (end.close - base.close) / base.close * 10_000
        range_bp = (
            (max(s.high for s in involved) - min(s.low for s in involved)) / base.close * 10_000
        )
        results.append(
            Reaction(
                window_min=days * MINUTES_PER_TRADING_DAY,
                ret_bp=ret_bp,
                range_bp=range_bp,
                vol_z=None,
                contaminated=False,
                deferred=deferred,
            )
        )
    return results


# ── Mimořádná výchylka (#1291, ADR-0043) ───────────────────────────


def percentile_abs(values: Sequence[float], fraction: float) -> float:
    """Empirický percentil |hodnot| (nearest-rank) — bez závislosti na numpy."""
    ordered = sorted(abs(value) for value in values)
    rank = max(0, min(len(ordered) - 1, round(fraction * (len(ordered) - 1))))
    return ordered[rank]


@dataclass(frozen=True)
class Excursion:
    """Největší výchylka v okně od startu proti close minuty před startem.

    `bp` je vždy kladné, `direction` +1 nahoru / −1 dolů, `z` = bp normalizované
    σ minutových výnosů poslední hodiny (`bp / (σ_bp · √okno)`) — stejná
    výchylka po klidné hodině je mimořádnější než po rozjeté.
    """

    bp: float
    direction: int
    z: float


@dataclass(frozen=True)
class Thresholds:
    """Hranice obvyklé výchylky v denní době: percentil bp a percentil z."""

    bp: float
    z: float
    samples: int


def minute_of_day_et(ts: dt.datetime) -> int:
    """Minuta dne v New Yorku — americké releasy drží čas ET přes celý rok."""
    local = ts.astimezone(ET_TZ)
    return local.hour * 60 + local.minute


def excursion_series(bars: Sequence[Bar], window: int) -> dict[dt.datetime, Excursion]:
    """Výchylka pro každou start-minutu, kterou jde změřit; σ klouzavě v O(n).

    Start `m` je měřitelný, když existuje bar `m − 1` (základ, close před
    zprávou), v okně `[m, m + window)` je aspoň `window − 1` barů (jedna díra
    v datech se toleruje) a za hodinu před `m` je aspoň `SIGMA_MIN_SAMPLES`
    minutových výnosů mezi sousedními bary. Mezera přes zavřený trh tak nikdy
    nevstoupí do σ ani do základu — gap na otevření se nezapočte.
    """
    # Jedna minuta = jeden bar; dál se pracuje s indexy seřazené řady (bez
    # slovníkových dotazů a timedelta na každou minutu — baseline měří ~28 k startů)
    ordered = sorted({bar.ts: bar for bar in bars}.values(), key=lambda bar: bar.ts)
    count = len(ordered)
    span = dt.timedelta(minutes=window)
    lookback = dt.timedelta(minutes=SIGMA_LOOKBACK_MIN - 1)
    scale = math.sqrt(window)
    returns: deque[tuple[dt.datetime, float]] = deque()
    total = 0.0
    total_sq = 0.0
    result: dict[dt.datetime, Excursion] = {}
    for index, base in enumerate(ordered):
        # Výnos minuty `base` (vůči předchozí minutě) vstupuje do σ startu base + 1
        if index:
            previous = ordered[index - 1]
            if base.ts - previous.ts == _MINUTE and previous.close > 0 and base.close > 0:
                value = math.log(base.close / previous.close)
                returns.append((base.ts, value))
                total += value
                total_sq += value * value
        horizon = base.ts - lookback
        while returns and returns[0][0] < horizon:
            _, dropped = returns.popleft()
            total -= dropped
            total_sq -= dropped * dropped
        if not returns:
            total = total_sq = 0.0  # žádný drift součtů přes dlouhé mezery
        if base.close <= 0 or len(returns) < SIGMA_MIN_SAMPLES:
            continue
        start = base.ts + _MINUTE
        end = start + span
        high = -math.inf
        low = math.inf
        in_window = 0
        cursor = index + 1
        while cursor < count and ordered[cursor].ts < end:
            bar = ordered[cursor]
            high = max(high, bar.high)
            low = min(low, bar.low)
            in_window += 1
            cursor += 1
        if in_window < window - 1:
            continue
        n = len(returns)
        variance = max(0.0, (total_sq - total * total / n) / (n - 1))
        sigma_bp = math.sqrt(variance) * 10_000
        if sigma_bp <= 0:
            continue
        up = (high - base.close) / base.close * 10_000
        down = (base.close - low) / base.close * 10_000
        bp, direction = (up, 1) if up >= down else (down, -1)
        result[start] = Excursion(bp=bp, direction=direction, z=bp / (sigma_bp * scale))
    return result


def measure_excursion(bars: Sequence[Bar], start: dt.datetime, window: int) -> Excursion | None:
    """Výchylka od minuty `start` (celá minuta); None = nejde změřit."""
    lower = start - dt.timedelta(minutes=SIGMA_LOOKBACK_MIN + 1)
    upper = start + dt.timedelta(minutes=window)
    relevant = [bar for bar in bars if lower <= bar.ts < upper]
    return excursion_series(relevant, window).get(start)


def build_excursion_baseline(
    bars_by_session: Sequence[Sequence[Bar]], window: int
) -> dict[int, list[Excursion]]:
    """Výchylky všech měřitelných start-minut seancí podle minuty dne v ET."""
    baseline: dict[int, list[Excursion]] = {}
    for session in bars_by_session:
        for start, excursion in excursion_series(session, window).items():
            baseline.setdefault(minute_of_day_et(start), []).append(excursion)
    return baseline


def tod_thresholds(
    baseline: Mapping[int, Sequence[Excursion]],
    minute: int,
    *,
    half_width: int = TOD_HALF_WIDTH_MIN,
    quantile: float = TOD_QUANTILE,
    min_samples: int = TOD_MIN_SAMPLES,
) -> Thresholds | None:
    """Percentil výchylky (bp) a z v pásu ± `half_width` minut kolem `minute`.

    None = málo vzorků (začátek archivu, okolí denní pauzy) — radši žádné
    rozhodnutí než práh z pár hodnot.
    """
    sample: list[Excursion] = []
    for offset in range(-half_width, half_width + 1):
        sample.extend(baseline.get((minute + offset) % 1440, ()))
    if len(sample) < min_samples:
        return None
    return Thresholds(
        bp=percentile_abs([item.bp for item in sample], quantile),
        z=percentile_abs([item.z for item in sample], quantile),
        samples=len(sample),
    )
