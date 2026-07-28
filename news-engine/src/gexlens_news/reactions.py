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
"""

import datetime as dt
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

# Okna dle SPEC 5.1; primární pro hit-rate je +5 min (konfig. per kategorie)
DEFAULT_WINDOWS = (1, 5, 15, 60)
# Mezera mezi zprávou a prvním obchodovaným barem, od které jde o deferred.
# Běžná díra v datech (jeden chybějící bar) se tím nezamění za zavřený trh.
DEFERRED_GAP_MINUTES = 5
# Minimální počet seancí pro objemovou baseline (SPEC 5.1 mluví o 20)
MIN_BASELINE_SESSIONS = 20


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
        if stats is None or stats.sessions < MIN_BASELINE_SESSIONS:
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
