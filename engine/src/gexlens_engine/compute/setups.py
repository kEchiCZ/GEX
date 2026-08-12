"""Setup detektor (ADR-0004, Fáze 1): čisté funkce šablon T1–T4 a vyhodnocení.

Vstupem je historie minutových vstupů (cena podkladu + GEX úrovně + tok);
výstupem kandidáti setupů s entry/target/stop, confidence a českým
zdůvodněním. Žádné I/O — orchestraci (stav, DB, alerty) dělá
`gexlens_engine.setups.SetupEngine`. Prahy dle ADR-0004.
"""

import datetime as dt
import enum
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

# Verze mechaniky detektoru (#311). Zvedá se při KAŽDÉ změně sémantiky stopů,
# cílů nebo filtrů — statistiky a budoucí kalibrace (Fáze 2) počítají jen
# aktuální verzi, aby se nemíchaly výsledky různých systémů.
#   1 = původní mechanika (absolutní buffery, RRR jen v T1/T5); patří sem i
#       všechny řádky před zavedením sloupce, tedy včetně incidentu se zmrzlými
#       Greeks 26.–27. 7. (ADR-0015)
#   2 = jednotná R-mechanika #302 (min. risk dle ATR, cíl omezený násobkem
#       risku, RRR filtr všude) nad ověřeně čerstvými daty po #306
#
# #463 (obnova OI archivu po publikačním okně) verzi NEZVEDÁ, ale je to
# hraniční případ k rozhodnutí: mechanika šablon se nemění vůbec, mění se
# KVALITA vstupů — Max Pain i zdi se dosud v některých dnech počítaly nad
# předpublikačním OI. Zvednutí by vyřadilo historii ze statistik těsně před
# kalibrací #394 (stejná úvaha jako u #443 níže) a navíc by hranice byla
# falešně ostrá: zkažené byly jen dny, kdy archivace stihla proběhnout před
# publikací, ne všechny. Zpětně je od sebe nerozlišíme — `captured_ts` mají
# až řádky po této změně.
#
# #443 (T7 + okno průrazu T4) verzi ZÁMĚRNĚ nezvedá, i když sahá na filtr T4:
# T4 nevygeneroval za celou historii jediný setup, takže není co znehodnotit,
# a T7 je aditivní — sémantika stávajících šablon se nemění. Zvednutí by
# vyřadilo všech 280 historických setupů ze statistik těsně před kalibrací
# #394, tedy by uškodilo víc, než by pomohlo. Až se bude sahat na CumΔ bránu
# T4 nebo na mechaniku existujících šablon, verzi zvednout.
#
#   3 = kalibrace #434/#394: T7 práh `trend_min_distance_atr` 1,0 → 12,0 ×ATR.
#       Původní hodnota byla degenerovaná (splňovalo ji 98,9 % minut ES /
#       95,2 % NQ s polohovou branou) — T7 tedy vznikal z jiné množiny minut
#       než po změně, takže se mění filtr šablony a historické T7 řádky
#       nejsou srovnatelné s novými.
#    v4: CumΔ a net objem kotvené na open Globex seance (#638) — dřív se
#       resetovaly jen restartem enginu, takže cum_delta v context řádcích
#       v3 a v4 nejsou srovnatelné (jiná základna).
SETUP_MECHANICS_VERSION = 4


class SetupTemplate(enum.Enum):
    WALL_BOUNCE = "wall_bounce"
    FAILED_BREAK = "failed_break"
    MAX_PAIN_PIN = "max_pain_pin"
    GAMMA_MOMENTUM = "gamma_momentum"
    DIVERGENCE_SPRING = "divergence_spring"  # T5 (#250)
    TREND_CONTINUATION = "trend_continuation"  # T7 (#443)


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
    # Hranice gamma masy z Dyn GEX profilu (#600): za nimi hedging cenu přestává
    # tlumit. None = profil minuty chybí (starší data, minuta bez snapshotu).
    gamma_edge_up: float | None = field(default=None, kw_only=True)
    gamma_edge_dn: float | None = field(default=None, kw_only=True)


@dataclass(frozen=True)
class SetupParams:
    """Prahy šablon (ADR-0004 defaulty; body podkladu)."""

    # Prahy vzdálenosti od zdi. Absolutní body v sobě nesou asymetrii mezi
    # instrumenty: 3 body jsou na ES 2,4× medián minutového rozsahu (dotyk
    # zaznamená prakticky každá svíčka u zdi), na NQ jen 0,27× (low musí zeď
    # trefit přesně). Nabízelo se proto škálovat je ATR jako R-mechaniku níž.
    #
    # ZMĚŘENO A ZAMÍTNUTO (#434, `scripts/backtest_setups.py`, 20. 7.–4. 8.):
    # škálování 1,9 × ATR překlopilo NQ z +25,6 R na −15,0 R a bylo horší v
    # 9 z 12 dnů — úzká zóna na NQ nefunguje jako závada, ale jako filtr
    # kvality. Přeměřeno při kalibraci #394 (20. 7.–7. 8., 15 dní) na mřížce
    # 0,5/1,0/1,5/2,0 × ATR: KAŽDÝ násobek NQ zhoršil (Σ +31,1 R baseline →
    # +1,8 / −4,3 / −20,6 / −23,3 R) a ES nepomohl. Násobky proto zůstávají
    # na 0 (= vypnuto, chování beze změny) a slouží jen jako kalibrační páka.
    wall_zone: float = 3.0
    wall_zone_atr: float = 0.0
    rejection_min: float = 1.0
    rejection_min_atr: float = 0.0
    # ATR páky pro prahy průrazu/reclaim T2 (#434) — stejná konvence jako výše:
    # 0 = vypnuto (absolutní body), jinak max(absolutní mez, násobek × ATR).
    #
    # ZMĚŘENO (kalibrace #394, 20. 7.–7. 8.): škálování break_min POMÁHÁ na ES
    # (2,0 × ATR: Σ +24,7 → +44,1 R, lepší v 7 z 9 změněných dnů — odřízne
    # ztrátové T2 z minutového šumu) a mírně škodí na NQ (+31,1 → +19,2 R,
    # řeže i ziskové shorty). Kvůli neshodě mezi instrumenty zůstává 0;
    # rozhodnutí je v #434 (needs-decision).
    break_min_atr: float = 0.0
    reclaim_min_atr: float = 0.0
    divergence_lookback: int = 10
    min_rrr: float = 1.2
    # R-mechanika (#302) — jednotná pro všechny šablony, aplikuje se v `detect_all`.
    # Prahy jsou v násobcích ATR, ne v absolutních bodech: ATR(14) 1min je
    # medián 1,57 b na ES vs. 11,52 b na NQ (měřeno 20.–27. 7.), takže sdílený
    # absolutní buffer dává na každém instrumentu jinak velký risk.
    atr_lookback: int = 14
    # Stop těsnější než min_risk_atr × ATR je uvnitř minutového šumu — rozšíří
    # se na minimum (NQ T5 měla Ø risk 0,86 × ATR, setupy padaly do minuty)
    min_risk_atr: float = 2.0
    # Cíl dál než max_rr × risk je nedosažitelný: cíl = nejbližší úroveň, a když
    # jsou všechny daleko, vzniklo RRR 16–42 (NQ) a vždy se trefil dřív stop.
    # Nad tímto stropem se bere částečný cíl na max_rr × risk.
    max_rr: float = 3.0
    break_min: float = 3.0
    acceptance_minutes: int = 5
    reclaim_window: int = 15
    reclaim_min: float = 1.0
    pin_max_minutes: float = 180.0
    pin_min_distance: float = 8.0
    # T3 (#302): stop jako podíl vzdálenosti k Max Pain. Původních 1,5×
    # riskovalo víc, než byl cíl hoden (RRR 0,7) — pin je vyvrácený, když se
    # cena o takový podíl vzdálí špatným směrem.
    pin_stop_ratio: float = 0.75
    pin_stability: float = 5.0
    pin_stability_lookback: int = 60
    momentum_break: float = 2.0
    momentum_flow_share: float = 0.6
    momentum_flow_lookback: int = 10
    # Okno, ve kterém smí průraz flipu nastat, aby se stihl potvrdit tokem (#443)
    momentum_cross_window: int = 10
    # Jak přísně musí CumΔ potvrzovat směr průrazu (#443). 0 = absolutní extrém
    # okna (původní stav — z 11 křížení neprošlo ani jedno).
    #
    # ZMĚŘENO (20. 7.–4. 8.): 0,25 / 0,33 / 0,5 dávají shodné výsledky, práh tedy
    # není citlivý — ponechána nejpřísnější funkční hodnota. Uvolnění brány T4
    # rozjelo (NQ 3 → 5 setupů) a zmírnilo její ztrátu (Ø R −1,00 → −0,30), ale
    # POZITIVNÍ EXPEKTANCI NEPROKÁZALO. Vzorek 5 setupů je na odsouzení malý
    # (T5 se vypínala až na 23), proto šablona zůstává zapnutá kvůli sběru dat
    # — vyhodnotit při kalibraci #394 a při záporném výsledku vypnout jako T5.
    # Stav při kalibraci #394 (7. 8.): T4 po #447/#449 reálně vzniká — produkce
    # 1 setup (NQ short 5. 8., +3 R), harness 7 za 15 dní (Σ −2,2 R). Vzorek
    # pořád malý, šablona dál sbírá data.
    momentum_cum_quantile: float = 0.25
    # T7 pokračování trendu (#443): pullback k EMA a jeho odmítnutí
    trend_ema_span: int = 20
    trend_pullback_atr: float = 0.5
    trend_rejection_atr: float = 0.25
    # Minimální odstup od opěrné zdi. Původní 1,0 × ATR byl degenerovaný práh:
    # splňovalo ho 98,9 % minut ES / 95,2 % NQ, které prošly polohovou branou
    # (na ES je 1 ATR ≈ 1,6 b, medián odstupu ceny od zdi je přitom 29 ATR) —
    # šablona tak degenerovala na EMA20 pullback držený jen anti-spamem.
    # ZMĚŘENO (kalibrace #394, sweep 1/2/3/5/8/12/20/30 × ATR, 15 dní):
    # 12 × ATR je nejvyšší hodnota, kde obě strany žijí a NQ se zlepší
    # (Σ +6,2 → +17,1 R, hit 32,5 → 40,9 %; ES +41,9 → +28,8 R, pořád kladné);
    # od 20 × ATR NQ umírá (n 23 → 7, Σ +1,1 → −4,0 R). Motivační případ
    # („stovky bodů od zdi", ~33 ATR na NQ) by tedy šablonu zabil.
    trend_min_distance_atr: float = 12.0
    cooldown_minutes: int = 10
    # Minimální dominance zdi pro T1/T3 (ADR-0010, #223): argmax existuje i nad
    # plochým profilem — pod prahem zeď netvoří koncentraci a setup nevzniká
    min_wall_dominance: float = 0.15
    # T5 divergenční spring (#250): okno extrémů, odmítnutí a stop buffer
    spring_lookback: int = 90
    spring_rejection: float = 1.0
    spring_stop_buffer: float = 2.0
    # Kontra-režim (#252 B+C): fade proti gammě vyžaduje CumΔ konfluenci přes
    # delší okno a po stopu má šablona v kontra-režimu delší cooldown
    counter_flow_lookback: int = 30
    counter_stop_cooldown_minutes: int = 45
    # Strop pokusů per směr (#302): anti-spam je per šablona, takže se šablony
    # prokládaly a vznikl shortovací automat (27. 7.: 20 shortů za sebou proti
    # stoupajícímu NQ). Po N stopech v řadě v jednom směru se směr zablokuje;
    # počítadlo maže až výhra v tom směru, takže po sérii jde 1 pokus za okno.
    max_stops_per_direction: int = 3
    direction_block_minutes: int = 90
    # Vypnuté šablony (#303): kandidát vznikne, ale zahodí se v `detect_all`.
    # T5 divergence_spring má 8,7 % úspěšnost a Ø −0,69R za 23 setupů — vznikla
    # z jediného živého případu (24. 7.), edge nepotvrdila. Kód zůstává, aby
    # šlo šablonu přeměřit po opravě R-mechaniky (#302).
    disabled_templates: frozenset[str] = frozenset({SetupTemplate.DIVERGENCE_SPRING.value})


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


def is_counter_regime(direction: Direction, regime: str | None) -> bool:
    """Kontra-režimový obchod (#252): long v negativní gammě / short v pozitivní.

    Neznámý režim (None) není kontra — přísnější podmínky se přeskakují
    (stejná konvence jako u neznámé dominance zdí, ADR-0010).
    """
    return (direction is Direction.LONG and regime == "negative") or (
        direction is Direction.SHORT and regime == "positive"
    )


def _counter_flow_confirmed(
    history: Sequence[MinuteInputs], direction: Direction, params: SetupParams
) -> bool:
    """B (#252): kontra-režim vyžaduje CumΔ divergenci přes delší okno.

    Fade proti negativní gammě je legitimní jen s důkazem, že tok se otáčí
    i na delším horizontu (24. 7.: 11 kontra longů NQ bez této konfluence,
    9 stopů). Krátká historie = konfluenci nelze ověřit → False.
    """
    if len(history) < params.counter_flow_lookback + 1:
        return False
    now = history[-1]
    then = history[-1 - params.counter_flow_lookback]
    if direction is Direction.LONG:
        return now.cum_delta > then.cum_delta
    return now.cum_delta < then.cum_delta


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


def average_true_range(history: Sequence[MinuteInputs], lookback: int) -> float | None:
    """ATR minutových barů (#302); None = krátká historie nebo nulová volatilita.

    Měřítko volatility instrumentu — prahy risku se od něj odvozují, aby
    platily pro ES i NQ zároveň (ATR 1min medián 1,57 vs. 11,52 bodu).
    """
    if lookback < 1 or len(history) < lookback + 1:
        return None
    window = history[-lookback:]
    previous = history[-lookback - 1 : -1]
    total = 0.0
    for now, prev in zip(window, previous, strict=True):
        total += max(
            now.high - now.low,
            abs(now.high - prev.close),
            abs(now.low - prev.close),
        )
    atr = total / lookback
    return atr if atr > 0 else None


def normalize_candidate(
    candidate: SetupCandidate, atr: float, params: SetupParams
) -> SetupCandidate | None:
    """Sjednocená R-mechanika (#302) — jediné místo, kde se rozhoduje o risku a cíli.

    1. Stop těsnější než `min_risk_atr` × ATR se rozšíří na minimum: risk musí
       přežít minutový šum, ne lichotit R metrice.
    2. Cíl dál než `max_rr` × risk se zkrátí na částečný cíl — premisa šablony
       zůstává, ale cíl je dosažitelný.
    3. Teprve pak se kontroluje `min_rrr`.

    Záměrně mimo jednotlivé šablony: dřív kontrolovaly RRR jen T1 a T5,
    zbytek na to zapomněl (T3 pouštěla setupy s RRR 0,7).
    """
    entry = candidate.entry
    long = candidate.direction is Direction.LONG
    sign = 1.0 if long else -1.0

    risk = max(candidate.risk, params.min_risk_atr * atr)
    if risk <= 0:
        return None
    stop = entry - sign * risk

    capped = entry + sign * params.max_rr * risk
    target = min(candidate.target, capped) if long else max(candidate.target, capped)
    # Cíl musí zůstat na správné straně entry (úroveň mohla být příliš blízko
    # a rozšířený stop ji přeskočil) — jinak by RRR bylo záporné
    if (long and target <= entry) or (not long and target >= entry):
        return None

    normalized = replace(
        candidate,
        stop=stop,
        target=target,
        context={**candidate.context, "atr": atr, "risk": risk},
    )
    if normalized.rrr < params.min_rrr:
        return None
    return normalized


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
        # Kontra-režim (#252 B): fade proti gammě jen s CumΔ konfluencí přes
        # delší okno — krátká divergence sama sérii ztrát nezastavila (24. 7.)
        counter = is_counter_regime(direction, now.gex_regime)
        if counter and not _counter_flow_confirmed(history, direction, params):
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
        return SetupCandidate(
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
                + (
                    f" Kontra-režim potvrzen tokem ({params.counter_flow_lookback} min)."
                    if counter
                    else ""
                )
            ),
            context={
                "wall": wall,
                "wall_dom": dominance,
                "flip": now.flip,
                "max_pain": now.max_pain,
                "cum_delta": now.cum_delta,
                "right_gamma_side": right_gamma_side,
                "gex_regime": now.gex_regime,
                "counter_regime": counter,
            },
        )
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
        # Kontra-režim (#252 B): reclaim v negativní gammě jen s CumΔ konfluencí
        counter = is_counter_regime(Direction.LONG, now.gex_regime)
        if counter and not _counter_flow_confirmed(history, Direction.LONG, params):
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
                + (
                    f" Kontra-režim potvrzen tokem ({params.counter_flow_lookback} min)."
                    if counter
                    else ""
                )
            ),
            context={
                "level": level,
                "extreme": extreme,
                "gex_regime": now.gex_regime,
                "counter_regime": counter,
            },
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
        # Kontra-režim (#252 B): upthrust v pozitivní gammě jen s CumΔ konfluencí
        counter = is_counter_regime(Direction.SHORT, now.gex_regime)
        if counter and not _counter_flow_confirmed(history, Direction.SHORT, params):
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
                + (
                    f" Kontra-režim potvrzen tokem ({params.counter_flow_lookback} min)."
                    if counter
                    else ""
                )
            ),
            context={
                "level": level,
                "extreme": extreme,
                "gex_regime": now.gex_regime,
                "counter_regime": counter,
            },
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
    # Pin je vyvrácený, když se cena o `pin_stop_ratio` vzdálenosti vzdálí
    # opačným směrem (#302) — původní 1,5× riskovalo víc, než byl cíl hoden
    offset = params.pin_stop_ratio * abs(distance)
    stop = entry - offset if direction is Direction.LONG else entry + offset
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
    if now.flip is None:
        return None
    recent = history[-params.momentum_flow_lookback :]
    call_flow = sum(m.call_flow for m in recent)
    put_flow = sum(m.put_flow for m in recent)
    total_flow = call_flow + put_flow
    if total_flow <= 0:
        return None
    cum_window = [m.cum_delta for m in history[-30:]]

    def cum_confirms(down: bool) -> bool:
        """Potvrzuje CumΔ směr průrazu? (#443)

        Původně se vyžadovalo ABSOLUTNÍ minimum/maximum okna — tak přísné, že
        z 11 křížení flipu neprošlo ani jedno (0 setupů z 280 za celou historii).
        Smysl brány je „tok potvrzuje směr", ne „přesně teď padl rekord", takže
        stačí kvantil: dolní/horní `momentum_cum_quantile` část okna.
        """
        ordered = sorted(cum_window)
        if not ordered:
            return False
        share = min(max(params.momentum_cum_quantile, 0.0), 1.0)
        if down:
            index = max(0, int(share * (len(ordered) - 1)))
            return now.cum_delta <= ordered[index]
        index = min(len(ordered) - 1, int((1.0 - share) * (len(ordered) - 1)))
        return now.cum_delta >= ordered[index]

    # Průraz stačí v posledních `momentum_cross_window` minutách, když DRŽÍ (#443).
    # Původně musel nastat přesně v aktuální minutě zároveň s převahou toku a
    # extrémem CumΔ — změřeno: za 2 dny (5 545 minut ES+NQ) prošlo branou křížení
    # 11 minut, z toho 1 s tokem a 0 s extrémem. Potvrzení tokem přirozeně
    # přichází až PO průrazu, takže konjunkce v jedné minutě byla nedosažitelná
    # (0 setupů z 280 za celou historii).
    window = history[-(params.momentum_cross_window + 1) :]
    pairs = [(a, b) for a, b in zip(window, window[1:], strict=False) if a.flip and b.flip]
    crossed_down = (
        any(a.close >= a.flip and b.close < b.flip for a, b in pairs)  # type: ignore[operator]
        and now.close <= now.flip - params.momentum_break
    )
    crossed_up = (
        any(a.close <= a.flip and b.close > b.flip for a, b in pairs)  # type: ignore[operator]
        and now.close >= now.flip + params.momentum_break
    )
    if crossed_down:
        if put_flow / total_flow < params.momentum_flow_share:
            return None
        if not cum_confirms(down=True):
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
        if not cum_confirms(down=False):
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


def detect_divergence_spring(
    history: Sequence[MinuteInputs], params: SetupParams
) -> SetupCandidate | None:
    """T5 (#250): nové extrémum ceny × extrém CumΔ ve vzduchoprázdnu.

    Živý vzor 24. 7. 8:49: nové low seance 7433.75 mimo zónu zdi, zatímco
    CumΔ dělala maxima okna (nákupy do slabosti) → spring +25 b. Zóna zdi
    patří T1 (odraz), tady se chytá spring bez úrovně pod cenou.
    """
    if len(history) < params.spring_lookback + 1:
        return None
    now = history[-1]
    window = history[-(params.spring_lookback + 1) : -1]

    def near_wall(wall: float | None) -> bool:
        return wall is not None and abs(now.close - wall) <= params.wall_zone

    # LONG: nové low okna + CumΔ na maximu okna + odmítnutí (close zpět nad low)
    if (
        now.low <= min(m.low for m in window)
        and now.cum_delta >= max(m.cum_delta for m in window)
        and now.close >= now.low + params.spring_rejection
        and not near_wall(now.put_wall)
    ):
        entry = now.close
        target = _nearest_level_above(entry, (now.max_pain, now.flip, now.call_wall))
        if target is not None:
            return SetupCandidate(
                template=SetupTemplate.DIVERGENCE_SPRING,
                direction=Direction.LONG,
                entry=entry,
                target=target,
                stop=now.low - params.spring_stop_buffer,
                confidence=50,
                reason=(
                    f"Divergenční spring: nové low okna {now.low:g} mimo zónu zdi, "
                    f"CumΔ přitom na maximu ({now.cum_delta:.0f}) — nákupy do slabosti, "
                    f"close {now.close:g} low odmítl."
                ),
                context={
                    "extreme": now.low,
                    "cum_delta": now.cum_delta,
                    "put_wall": now.put_wall,
                    "gex_regime": now.gex_regime,
                },
            )

    # SHORT zrcadlově: nové high okna + CumΔ na minimu okna + odmítnutí dolů
    if (
        now.high >= max(m.high for m in window)
        and now.cum_delta <= min(m.cum_delta for m in window)
        and now.close <= now.high - params.spring_rejection
        and not near_wall(now.call_wall)
    ):
        entry = now.close
        target = _nearest_level_below(entry, (now.max_pain, now.flip, now.put_wall))
        if target is not None:
            return SetupCandidate(
                template=SetupTemplate.DIVERGENCE_SPRING,
                direction=Direction.SHORT,
                entry=entry,
                target=target,
                stop=now.high + params.spring_stop_buffer,
                confidence=50,
                reason=(
                    f"Divergenční spring: nové high okna {now.high:g} mimo zónu zdi, "
                    f"CumΔ přitom na minimu ({now.cum_delta:.0f}) — prodeje do síly, "
                    f"close {now.close:g} high odmítl."
                ),
                context={
                    "extreme": now.high,
                    "cum_delta": now.cum_delta,
                    "call_wall": now.call_wall,
                    "gex_regime": now.gex_regime,
                },
            )
    return None


def _ema(values: Sequence[float], span: int) -> float | None:
    """EMA posledních hodnot; kratší historie než span = None."""
    if len(values) < span:
        return None
    alpha = 2.0 / (span + 1)
    result = values[-span]
    for value in values[-span + 1 :]:
        result = alpha * value + (1 - alpha) * result
    return result


def detect_trend_continuation(
    history: Sequence[MinuteInputs], params: SetupParams, atr: float
) -> SetupCandidate | None:
    """T7: pokračování trendu nad/pod celým positioningem (#443).

    Trendový den utíká svému zajištění a šablony na DOTYK úrovně nemají co
    detekovat — 3. 8. byla NQ odpoledne průměrně 379 bodů nad put zdí a pod ni
    se dostala jedinou minutou z 391, takže nevznikl žádný setup.

    Kotví se proto jinak: cena musí být mimo celý pás zdí (nad oběma, resp. pod
    oběma) na správné straně flipu, udělat pullback k EMA a odmítnout ho. Prahy
    jsou v násobcích ATR, ne v bodech — sdílená absolutní hodnota má na ES a NQ
    jinou citlivost (viz #434).
    """
    if atr <= 0 or len(history) < params.trend_ema_span + 2:
        return None
    now = history[-1]
    if now.flip is None or now.call_wall is None or now.put_wall is None:
        return None
    closes = [item.close for item in history]
    ema = _ema(closes, params.trend_ema_span)
    if ema is None:
        return None

    # Cena musí být daleko od zdi, o kterou se opírá (jinak to řeší T1 odraz),
    # a mít kam jít — protilehlá zeď je cíl, ne překážka. Původní návrh chtěl
    # cenu nad OBĚMA zdmi; změřeno na 3. 8.: nenastalo to ani jednou z 1 776
    # minut, protože call zeď je z podstaty nad cenou.
    gap = params.trend_min_distance_atr * atr
    if now.close >= now.flip and now.close > now.put_wall + gap and now.close < now.call_wall:
        direction = Direction.LONG
        support, objective = now.put_wall, now.call_wall
    elif now.close <= now.flip and now.close < now.call_wall - gap and now.close > now.put_wall:
        direction = Direction.SHORT
        support, objective = now.call_wall, now.put_wall
    else:
        return None

    # Pullback k EMA a jeho odmítnutí v téže minutě — vstup po korekci, ne na vrcholu
    if direction is Direction.LONG:
        touched = now.low <= ema + params.trend_pullback_atr * atr
        rejected = now.close >= ema + params.trend_rejection_atr * atr
        trend_intact = now.close > ema
        stop = now.low - params.trend_rejection_atr * atr
    else:
        touched = now.high >= ema - params.trend_pullback_atr * atr
        rejected = now.close <= ema - params.trend_rejection_atr * atr
        trend_intact = now.close < ema
        stop = now.high + params.trend_rejection_atr * atr
    if not (touched and rejected and trend_intact):
        return None

    entry = now.close
    return SetupCandidate(
        template=SetupTemplate.TREND_CONTINUATION,
        direction=direction,
        entry=entry,
        target=objective,  # protilehlá zeď; R-mechanika cíl případně zkrátí
        stop=stop,
        confidence=50,
        reason=(
            f"Pokračování trendu {'nad' if direction is Direction.LONG else 'pod'} zdí "
            f"{support:g} (flip {now.flip:g}, cíl {objective:g}): pullback k "
            f"EMA{params.trend_ema_span} {ema:.0f} a odmítnutí (close {entry:g})."
        ),
        context={
            "ema": ema,
            "flip": now.flip,
            "support_wall": support,
            "objective_wall": objective,
            "atr": atr,
            "gex_regime": now.gex_regime,
        },
    )


DETECTORS = (
    detect_failed_break,  # nejsilnější kontext první (anti-spam řeší orchestrátor)
    detect_wall_bounce,
    detect_divergence_spring,
    detect_gamma_momentum,
    detect_max_pain_pin,
)


def scale_params(params: SetupParams, atr: float) -> SetupParams:
    """Prahy vzdálenosti volitelně přepočtené na volatilitu instrumentu (#434).

    Absolutní hodnota je spodní mez, nad ní rozhoduje násobek ATR. S výchozími
    násobky 0 je funkce identita — škálování zóny zdi se měřením neosvědčilo
    (viz komentář u `SetupParams.wall_zone`), škálování prahů průrazu T2 je
    mezi ES a NQ rozporné (viz `break_min_atr`); obojí zůstává kalibrační
    páka vypnutá v defaultu.
    """
    return replace(
        params,
        wall_zone=max(params.wall_zone, params.wall_zone_atr * atr),
        rejection_min=max(params.rejection_min, params.rejection_min_atr * atr),
        break_min=max(params.break_min, params.break_min_atr * atr),
        reclaim_min=max(params.reclaim_min, params.reclaim_min_atr * atr),
    )


def detect_all(history: Sequence[MinuteInputs], params: SetupParams) -> list[SetupCandidate]:
    """Vyhodnotí povolené šablony nad aktuální minutou (bez stavového anti-spamu).

    Každý kandidát projde jednotnou R-mechanikou (#302). Bez měřitelného ATR
    (krátká historie po startu, nulová volatilita) nelze risk ověřit → setup
    nevzniká; stejná konzervativní konvence jako u kontra-režimu (#252 B).
    """
    atr = average_true_range(history, params.atr_lookback)
    if atr is None:
        return []
    scaled = scale_params(params, atr)
    results = []
    # T7 potřebuje ATR přímo (prahy jsou jeho násobky), proto stojí mimo DETECTORS
    candidates = [detector(history, scaled) for detector in DETECTORS]
    candidates.append(detect_trend_continuation(history, scaled, atr))
    for candidate in candidates:
        if candidate is None:
            continue
        if candidate.template.value in params.disabled_templates:
            continue
        normalized = normalize_candidate(candidate, atr, params)
        if normalized is None:
            continue
        results.append(normalized)
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
