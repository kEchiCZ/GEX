"""Ranní kalibrace α flow-adjusted odhadu (#232, ADR-0011 fáze 2) — čisté výpočty.

Porovnává včerejší KONEC DNE řady netflow (kumulativní klasifikovaný net per
strana) se skutečným ΔOI mezi ranními archivy. Poměr ΔOI/net na straně říká,
kolik z klasifikovaného toku se propsalo do nového positioningu — ideální α.
Medián přes strany s dostatečným |net| je robustní vůči ATM churnu (net ≈ 0,
ΔOI ≈ 0 → strana se vůbec nekvalifikuje) i jednotlivým ujetým strikům.

Buy/sell mediány se počítají zvlášť jen pro AUDIT (uložené v historii) —
aplikuje se jedna společná α, dokud data jasně neukážou asymetrii
(rozhodnutí uživatele 6. 8.). Sběr dat a uložení dělá `storage.fa_calibration`.

Pod mechanikou v6 (ADR-0061) se místo α kalibruje ρ: medián ΔOI/V, kde V je
objem strany za seanci (maximum kumulativu ze snímků do řezu). Rozbor #1612
ukázal, že OI roste i na stranách s převahou prodeje, takže znaménko toku
ΔOI neurčuje. Platný den a objem stran pozná `session_volume_coverage`.
"""

import datetime as dt
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from gexlens_engine.compute.marketclock import is_market_closed

Key = tuple[float, str]

# Minimální |net| kontraktů, aby strana vstoupila do kalibrace — pod prahem
# poměr ΔOI/net dominuje šum (ΔOI pár kontraktů / net pár kontraktů)
MIN_ABS_NET = 25.0
# Minimální počet kvalifikovaných stran pro platný denní bod
MIN_SAMPLES = 5
# EMA váha nového denního bodu: α konverguje během ~týdne čistých dnů,
# jeden ujetý den (crash, výpadek) výsledkem necloumá
EMA_LAMBDA = 0.3
# Meze α: záporná α (tok systematicky PROTI ΔOI) znamená rozbitá data, ne
# model; nad 1 by odhad tvrdil víc otevřeného zájmu, než kolik se zobchodovalo
ALPHA_MIN = 0.0
ALPHA_MAX = 1.0
# Pod v6 (ADR-0057 bod 6, ADR-0061 bod 4) je FA vrstva vypnutá, dokud ρ nemá
# aspoň tolik platných dnů kalibrace z objemu — jinak by jela nekalibrovaná
FA_MIN_DAYS = 5
# Minimální objem strany za seanci pro bod ρ (ADR-0061 bod 2) — práh převzatý
# z |net| kalibrace α; pod ním poměr ΔOI/V dominuje šum
MIN_VOLUME = 25.0
# Platný den ρ (ADR-0061 bod 3): nejdelší úsek minut otevřeného trhu bez snímku
# řetězu v seanci. Sekundár píše jen každý `next_expiry_sweep_every`. cyklus
# (3 → běžná mezera 2 minuty, občas 3–6); delší úsek je výpadek sběru
SNAPSHOT_GAP_MAX_MIN = 10
# Konec dne: poslední snímek řetězu i poslední objem strany nejvýš tolik minut
# před řezem (po rollu v settle je řetěz aktivní a píše každou minutu)
SNAPSHOT_END_TOLERANCE_MIN = 5

_MINUTE = dt.timedelta(minutes=1)


@dataclass(frozen=True)
class AlphaCalibrationPoint:
    """Jeden denní kalibrační bod (jeden symbol, jeden trade date)."""

    samples: int  # počet stran s |net| ≥ MIN_ABS_NET
    ratio_median: float  # medián ΔOI/net přes kvalifikované strany (≈ ideální α)
    ratio_buy: float | None  # medián přes strany s net > 0 (audit asymetrie)
    ratio_sell: float | None  # medián přes strany s net < 0 (audit asymetrie)


def calibrate_alpha(
    netflow: Mapping[Key, float],
    doi: Mapping[Key, float],
    *,
    min_abs_net: float = MIN_ABS_NET,
    min_samples: int = MIN_SAMPLES,
) -> AlphaCalibrationPoint | None:
    """Denní bod z map (strike, right) → net konec dne a → skutečné ΔOI.

    Poměr se počítá se znaménkem: net nákup s poklesem OI dává záporný poměr
    a medián ho po právu stáhne dolů. Vrací None při nedostatečném vzorku —
    takový den se do kalibrace nezapočítá. Strany, pro které `doi` klíč nemá
    (kontrakt v žádném z archivů), se přeskakují — chybějící měření není nula.
    """
    ratios: list[float] = []
    buy_ratios: list[float] = []
    sell_ratios: list[float] = []
    for key, net in netflow.items():
        if abs(net) < min_abs_net:
            continue
        # Strana bez ΔOI v archivech = „nevíme", ne nula (#1172): nula by
        # medián stáhla k 0 a α vypnula — přesně to se stalo 7. a 14. 9. 2026
        if key not in doi:
            continue
        ratio = float(doi[key]) / float(net)
        ratios.append(ratio)
        if net > 0:
            buy_ratios.append(ratio)
        else:
            sell_ratios.append(ratio)
    if len(ratios) < min_samples:
        return None
    return AlphaCalibrationPoint(
        samples=len(ratios),
        ratio_median=statistics.median(ratios),
        ratio_buy=statistics.median(buy_ratios) if buy_ratios else None,
        ratio_sell=statistics.median(sell_ratios) if sell_ratios else None,
    )


def update_alpha(
    previous: float | None, ratio_median: float, *, ema_lambda: float = EMA_LAMBDA
) -> float:
    """EMA aktualizace α: nový denní bod sevřený do [0, 1], první bod přímo."""
    target = min(max(ratio_median, ALPHA_MIN), ALPHA_MAX)
    if previous is None:
        return target
    return previous + ema_lambda * (target - previous)


def calibrate_rho(
    volumes: Mapping[Key, float],
    doi: Mapping[Key, float],
    *,
    min_volume: float = MIN_VOLUME,
    min_samples: int = MIN_SAMPLES,
) -> AlphaCalibrationPoint | None:
    """Denní bod ρ z map (strike, right) → objem seance V a → skutečné ΔOI (ADR-0061 bod 2).

    Medián ΔOI/V přes strany s V ≥ `min_volume`, které mají ΔOI. Strana bez
    ΔOI (neměřená po publikaci) nebo bez V (objem u řezu neznámý) se
    přeskakuje — chybějící měření není nula (#1172). Znaménko toku nevstupuje,
    `ratio_buy` a `ratio_sell` zůstávají None. Pod `min_samples` stran None.
    """
    ratios = [
        float(doi[key]) / float(volume)
        for key, volume in volumes.items()
        if volume >= min_volume and key in doi
    ]
    if len(ratios) < min_samples:
        return None
    return AlphaCalibrationPoint(
        samples=len(ratios),
        ratio_median=statistics.median(ratios),
        ratio_buy=None,
        ratio_sell=None,
    )


@dataclass(frozen=True)
class SessionVolumeCoverage:
    """Objem stran řetězu za seanci ze snímků a pokrytí seance (ADR-0061 bod 3)."""

    #: Konec obchodu seance do řezu 16:00 CT: řez, při zkrácené seanci nebo
    #: svátku začátek zavřeného trhu před ním (12:15 / 12:00 CT)
    close: dt.datetime
    #: V strany = maximum kumulativního objemu v seanci do řezu; jen strany,
    #: jejichž poslední objem leží nejvýš `SNAPSHOT_END_TOLERANCE_MIN` před `close`
    volumes: dict[Key, float]
    #: Strany se snímkem v seanci bez objemu v posledním úseku před řezem —
    #: „nevíme“, ne 0 ani starší maximum (#1668)
    unknown: frozenset[Key]
    #: Poslední minuta se snímkem řetězu do řezu; None = seance bez snímku
    last: dt.datetime | None
    #: Nejdelší úsek minut otevřeného trhu bez snímku od začátku seance
    #: do posledního snímku a jeho první minuta
    longest_gap: int
    gap_start: dt.datetime | None

    def problem(
        self,
        *,
        gap_max: int = SNAPSHOT_GAP_MAX_MIN,
        end_tolerance: int = SNAPSHOT_END_TOLERANCE_MIN,
    ) -> str | None:
        """Proč den bod nedá (seance bez snímku, konec před řezem, mezera); None = platný."""
        if self.last is None:
            return "seance bez snímku řetězu"
        if self.last < self.close - dt.timedelta(minutes=end_tolerance):
            return f"snímky končí {self.last:%H:%M}, obchod do {self.close:%H:%M} UTC"
        if self.longest_gap > gap_max and self.gap_start is not None:
            return (
                f"mezera snímků {self.longest_gap} min od {self.gap_start:%d. %m. %H:%M} UTC "
                f"(tolerance {gap_max} min)"
            )
        return None


def session_volume_coverage(
    rows: Iterable[tuple[dt.datetime | None, float, str, float | None]],
    start: dt.datetime,
    cutoff: dt.datetime,
    *,
    end_tolerance: int = SNAPSHOT_END_TOLERANCE_MIN,
) -> SessionVolumeCoverage:
    """Objem stran a pokrytí seance [start, řez] z řádků snímků (ts, strike, right, volume).

    V je maximum denního kumulativu IBKR (pokles a návrat feedu ho nezvětší,
    #1602); kumulativ přes mezeru uprostřed seance nic neztratí, chybí jen
    objem za úsekem, který snímky nepokryly. Strana, jejíž poslední objem
    (ne-NULL) je starší než `end_tolerance` před řezem, je „nevíme“ — strana
    bez objemu z tasty fallbacku i strana, která vypadla z pásma. Mezera
    se počítá jen v minutách otevřeného trhu (`marketclock.is_market_closed`);
    konec obchodu před řezem (zkrácená seance, svátek) posune i kontrolu konce.
    """
    peak: dict[Key, float] = {}
    last_volume: dict[Key, dt.datetime] = {}
    seen: set[Key] = set()
    minutes: set[dt.datetime] = set()
    for ts, strike, right, volume in rows:
        if ts is None or ts < start or ts > cutoff:
            continue
        minutes.add(ts)
        key = (float(strike), str(right))
        seen.add(key)
        if volume is None:
            continue
        peak[key] = max(peak.get(key, 0.0), float(volume))
        if key not in last_volume or ts > last_volume[key]:
            last_volume[key] = ts
    close = cutoff
    while close > start and is_market_closed(close - _MINUTE):
        close -= _MINUTE
    fresh = close - dt.timedelta(minutes=end_tolerance)
    volumes = {key: peak[key] for key, ts in last_volume.items() if ts >= fresh}
    last = max(minutes) if minutes else None
    longest, longest_start = 0, None
    run, run_start = 0, None
    moment = start
    while last is not None and moment < last:
        if moment in minutes or is_market_closed(moment):
            run = 0
        else:
            if run == 0:
                run_start = moment
            run += 1
            if run > longest:
                longest, longest_start = run, run_start
        moment += _MINUTE
    return SessionVolumeCoverage(
        close=close,
        volumes=volumes,
        unknown=frozenset(seen - volumes.keys()),
        last=last,
        longest_gap=longest,
        gap_start=longest_start,
    )
