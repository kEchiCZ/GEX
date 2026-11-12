"""Plán opce QQQ ze scénáře dne NQ (ADR-0062, epika #1738, F2a #1741) — čisté funkce bez IO.

Plán je převod auto-scénáře NQ na opci QQQ 1DTE (ADR-0062 bod 2), ne vlastní verdikt QQQ:
- směr a cíle převezme ze scénáře, úrovně NQ převede poměrem NQ/QQQ;
- EM QQQ = straddle 0DTE (`emrespect.straddle_em`);
- dvě varianty opce z řetězu 1DTE (bod 4).

Pravidla P1–P5 platí podle K3 A: vstup A po odmítnutí od úrovně, SL za extrémem od testu
a okno spouště do exitu. Plán vzniká v 9:15 ET se scénářem dne z ranního snímku řetězu
a v 9:31 ET se přecení (K4 C, F2b #1742, F2c #1769). Neúplný ranní snímek = plán v 9:15 bez
opce a v 9:31 celý z 9:31 (K4 bod 5). Přecenění úplného plánu (`reprice_variants`) drží
kontrakty z 9:15 a mění jen kotace, IV a model v TP1/TP2. Předá-li volající bary po
vzniku, funkce najde spoušť vstupu A a ocení ji modelem (P3, P4). Kotace v minutě spouště
a průběh po vstupu patří vyhodnocení (F3 #1748). Do JSON tabulky `option_plans` plán
převede `plan_to_json` (K2 A).

Hlavní plán má jen vstup A a hledá ho jednou (K6 B). Stíny track recordu (K10 B) jsou
parametry téže funkce:
- `SHADOW_B` = pravidla v1, tedy i vstup B podle P1 (K3 body 4 a 8);
- `SHADOW_WICK` = test úrovně knotem nad hlavními pravidly.

Ceny opce při spoušti, SL a TP počítá Black–Scholes (`gexfield.bs_price`) s naměřenou IV
kontraktu. Nesou štítek `MODEL_LABEL`, naměřené ceny přinese až vyhodnocení.

Při vzniku plán R:R ani ztrátu při SL nemá: SL podle K3 bodu 3 závisí na extrému po testu
úrovně, který při vzniku ještě nenastal. R:R se proto počítá při spoušti, jak říká Návrh
epiky („R:R při spoušti“) a K3 bod 6 („nejdřív v čase vstupu“); rozhodnuto K12 A.
"""

import dataclasses
import datetime as dt
import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from gexlens_engine.compute.dayverdict import AutoScenario, LevelInputs
from gexlens_engine.compute.emrespect import StraddleQuote, straddle_em
from gexlens_engine.compute.gex import GexEngine, GexInput
from gexlens_engine.compute.gexfield import _YEAR_S, TAU_FLOOR_S, bs_price
from gexlens_engine.compute.settle import (
    is_trading_session,
    settle_ts,
    trading_session_date,
    us_open_ts,
)
from gexlens_engine.compute.setups import PathBar
from gexlens_engine.ticker import EQUITY_OPTION_MULTIPLIER

#: Verze pravidel plánu (ADR-0062 K2 A: změna pravidel = nová verze, ne migrace)
RULES_VERSION = 1
#: Množina úrovní vstupu A (K3 A bod 1): podmnožina úrovní scénáře bez těžiště a PDC
LEVEL_SET = ("flip", "on_high", "on_low", "prev_high", "prev_low", "call_wall", "put_wall")
#: Převáděné úrovně NQ (pole `LevelInputs`) a jejich jména v textu plánu
LEVEL_LABELS: Mapping[str, str] = {
    "flip": "flip",
    "call_wall": "call wall",
    "put_wall": "put wall",
    "centroid": "těžiště",
    "on_high": "ONH",
    "on_low": "ONL",
    "prev_high": "PDH",
    "prev_low": "PDL",
    "prev_close": "PDC",
}
#: Úroveň vstupu A nejvýš 1 EM od vstupu scénáře (K3 bod 1)
LEVEL_LIMIT_EM = 1.0
#: Rezerva SL za extrémem od testu (K3 bod 3) i za úrovní u SL B (bod 4)
RESERVE_EM = 0.05
#: Spoušť stínu B: 5min close za vstupem scénáře ∓ 0,05 EM (K3 bod 4)
B_TRIGGER_EM = 0.05
#: P1: největší kladná GEX QQQ proti směru do 1 EM od spotu blokuje vstup B
P1_LIMIT_EM = 1.0
#: P4: čerpání = cena opce na úrovni SL + 0,2 EM v čase exitu (K3 bod 7)
P4_EXTRA_EM = 0.2
#: Hlavní varianta = strike s |delta| nejblíž 0,25 (bod 4)
TARGET_DELTA = 0.25
#: P3: R:R opce pod 2 = neobchodovat (K8 A)
MIN_RR = 2.0
#: Exit = settle − 15 min, přes noc se nedrží (K3 bod 5)
EXIT_BEFORE_SETTLE = dt.timedelta(minutes=15)
#: 5min bary po hodinách od US openu (K3 bod 2)
BAR_SPAN = dt.timedelta(minutes=5)
MODEL_LABEL = "model z naměřené IV"
CHAIN_SOURCE = "tastytrade"


class ChainRowLike(Protocol):
    """Kontrakt řetězu v minutě snímku; tvar `storage.parquet_store.SnapshotRow`."""

    @property
    def ts_min(self) -> dt.datetime: ...

    @property
    def strike(self) -> float: ...

    @property
    def right(self) -> str: ...

    @property
    def bid(self) -> float | None: ...

    @property
    def ask(self) -> float | None: ...

    @property
    def iv(self) -> float | None: ...

    @property
    def delta(self) -> float | None: ...

    @property
    def gamma(self) -> float | None: ...

    @property
    def oi(self) -> float | None: ...


@dataclass(frozen=True)
class AccountParams:
    """Parametry účtu plánu ze Settings (ADR-0062 K2). Výchozí hodnota v kódu není (Q3 B)."""

    #: Velikost účtu v $, slouží jen pro „% účtu“
    account_usd: float | None
    #: Rozpočet P4 na obchod v $ (1 kontrakt)
    max_loss_usd: float | None


#: Klíče parametrů účtu plánu v tabulce `settings` (ADR-0062 K2 A); výchozí hodnota v kódu není.
#: Zapisuje je `PUT /settings` (API) a čte engine v okamžiku plánu, oba přes `account_value`.
ACCOUNT_KEY = "option_plan_account_usd"
MAX_LOSS_KEY = "option_plan_max_loss_usd"
ACCOUNT_KEYS = (ACCOUNT_KEY, MAX_LOSS_KEY)


def account_value(raw: object) -> float | None:
    """Hodnota parametru účtu ze Settings: None = nenastaveno, jinak kladné konečné číslo.

    `null` i prázdný řetězec znamenají nenastaveno (Q3 B, tichý default není). Číslo smí
    přijít i jako text. Bool, nečíselný text, nula, záporné číslo, NaN a nekonečno jsou
    `ValueError`. Hláška hodnotu nenese: velikost účtu je soukromá a repo veřejné.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if isinstance(raw, bool) or not isinstance(raw, int | float | str):
        raise ValueError(f"čekalo se číslo, přišel typ {type(raw).__name__}")
    try:
        number = float(raw)
    except ValueError:
        raise ValueError("text není číslo") from None
    if not _positive(number):
        raise ValueError("číslo musí být kladné a konečné")
    return number


@dataclass(frozen=True)
class PlanRules:
    """Parametry pravidel. Hlavní plán = jen vstup A s testem 5min close (K6 B)."""

    name: str
    #: Vstup B podle K3 bodu 4, podmíněně podle P1 (pravidla v1, stín K10 B)
    entry_b: bool = False
    #: Test úrovně knotem místo 5min close (K3 D, stín K10 B)
    wick_test: bool = False


MAIN_RULES = PlanRules("hlavní")
SHADOW_B = PlanRules("stín vstup B", entry_b=True)
SHADOW_WICK = PlanRules("stín test knotem", wick_test=True)


@dataclass(frozen=True)
class FiveMinuteBar:
    """Uzavřený 5min bar v cenách QQQ; `end` = konec baru, close v něm."""

    end: dt.datetime
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class GexTop:
    """P1: největší kladná NetGEX_1pct QQQ 0DTE (naivní model dealera) v minutě vzniku."""

    strike: float | None
    net_gex_1pct: float | None
    distance_em: float | None
    #: Leží proti směru do 1 EM od spotu → vstup B se nenavrhuje
    blocks: bool


@dataclass(frozen=True)
class Entry:
    """Spoušť vstupu: close 5min baru, SL na podkladu."""

    kind: Literal["A", "B"]
    ts: dt.datetime
    price: float
    stop: float
    #: Úroveň, od které se vstup odvíjí (u B úroveň, za kterou leží SL), klíč `LEVEL_LABELS`
    level: str
    level_price: float


@dataclass(frozen=True)
class Pricing:
    """Ocenění varianty při spoušti (model, $ na 1 kontrakt); K3 body 6 a 7."""

    paid: float
    #: SL a TP1 v čase cíle 1 podle cesty, nejdřív ve vstupu, nejpozději v exitu (K11 A)
    at_stop: float
    at_tp1: float
    #: TP2 v čase posledního bodu cesty (nejpozději v exitu); None = scénář s jedním cílem
    at_tp2: float | None
    #: R:R opce pro P3; None = SL nevychází pod cenou vstupu (riziko nejde určit)
    rr: float | None
    #: Přísně: SL v čase exitu
    rr_strict: float | None
    loss_at_stop_usd: float
    #: P4: SL + 0,2 EM v čase exitu
    drawdown_usd: float
    pnl_tp1_usd: float
    pnl_tp2_usd: float | None
    loss_at_stop_pct: float | None
    drawdown_pct: float | None


Verdict = Literal["obchodovat", "neobchodovat", "čeká"]


@dataclass(frozen=True)
class OptionVariant:
    """Varianta opce z řetězu 1DTE: kotace se zdrojem a časem, model při TP a spoušti."""

    role: Literal["hlavní", "záložní"]
    right: Literal["C", "P"]
    strike: float
    expiry: dt.date
    bid: float
    ask: float
    iv: float
    delta: float
    quote_ts: dt.datetime
    #: Model při TP1/TP2 v časech cesty (nejpozději v exitu), ocenění v minutě vzniku plánu
    at_targets: tuple[float, ...]
    pricing: Pricing | None
    verdict: Verdict
    reasons: tuple[str, ...]
    source: str = CHAIN_SOURCE

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def label(self) -> str:
        """`obchodovat` / `neobchodovat — důvod` / `čeká — …`."""
        return " — ".join((self.verdict, "; ".join(self.reasons))) if self.reasons else self.verdict


@dataclass(frozen=True)
class OptionPlan:
    """Plán opce QQQ v minutě vzniku, s bary po vzniku i se spouští vstupu (ceny QQQ)."""

    rules_version: int
    rules: str
    day: dt.date
    created_at: dt.datetime
    direction: Literal["long", "short"]
    right: Literal["C", "P"]
    ratio: float | None
    spot: float | None
    em: float | None
    em_strike: float | None
    #: Nejnovější kotace řetězu 0DTE (stáří EM a P1)
    chain_0dte_ts: dt.datetime | None
    #: Vstup a cíle scénáře v cenách QQQ, časy cílů podle cesty scénáře
    scenario_entry: float | None
    targets: tuple[float, ...]
    target_ts: tuple[dt.datetime, ...]
    #: Převedené úrovně v cenách QQQ, klíče `LEVEL_LABELS`
    levels: Mapping[str, float]
    exit_ts: dt.datetime
    level_a: tuple[str, float] | None
    p1: GexTop | None
    trigger_a: Entry | None
    #: Jen pravidla se vstupem B (stín); spoušť i tehdy, když ji P1 vyřadí
    trigger_b: Entry | None
    #: Vstup plánu: první povolená spoušť
    entry: Entry | None
    variants: tuple[OptionVariant, ...]
    #: Viditelné mezery a důvody (chybějící úroveň, řetěz, EM…)
    missing: tuple[str, ...]
    #: P5: doporučení podmíněného příkazu v TWS
    p5: str
    model_label: str = MODEL_LABEL

    def _chosen(self) -> OptionVariant | None:
        """Varianta, podle které se čte verdikt plánu: obchodovat, pak čeká, jinak hlavní."""
        if not self.variants:
            return None
        for wanted in ("obchodovat", "čeká"):
            chosen = next((v for v in self.variants if v.verdict == wanted), None)
            if chosen is not None:
                return chosen
        return self.variants[0]

    @property
    def verdict(self) -> str:
        """Verdikt plánu: obchodovat, když aspoň jedna varianta; jinak důvod hlavní varianty."""
        chosen = self._chosen()
        if chosen is None:
            return " — ".join(("neobchodovat", "; ".join(self.missing) or "bez opce"))
        return chosen.label

    @property
    def verdict_kind(self) -> Verdict:
        """Hlavní slovo verdiktu plánu bez důvodu (sloupec `option_plans.verdict`)."""
        chosen = self._chosen()
        return chosen.verdict if chosen is not None else "neobchodovat"


@dataclass(frozen=True)
class VariantKey:
    """Kontrakt varianty plánu z 9:15 ET, který přecenění v 9:31 ET drží (K4 C bod 2)."""

    role: Literal["hlavní", "záložní"]
    right: Literal["C", "P"]
    strike: float
    expiry: dt.date


@dataclass(frozen=True)
class RepricedVariant:
    """Sada cen varianty z přecenění (K4 C bod 2): kotace, IV a model v TP1/TP2 se svým časem.

    Strike, role a expirace jsou z plánu 9:15. Stav varianty ani R:R přecenění nenese (K12 A).
    """

    role: Literal["hlavní", "záložní"]
    right: Literal["C", "P"]
    strike: float
    expiry: dt.date
    bid: float
    ask: float
    iv: float
    delta: float
    quote_ts: dt.datetime
    #: Model při TP1/TP2 v časech cesty (nejpozději v exitu), ocenění v okamžiku přecenění
    at_targets: tuple[float, ...]
    source: str = CHAIN_SOURCE

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


def _cz(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}".replace(".", ",").replace("-", "−")


def _finite(value: float | None) -> bool:
    return value is not None and math.isfinite(value)


def _positive(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0


@dataclass(frozen=True)
class _Model:
    """BS cena kontraktu s jeho naměřenou IV (r = q = 0, τ jako `gexfield`)."""

    right: str
    strike: float
    iv: float
    expiry_ts: dt.datetime

    def price(self, spot: float, ts: dt.datetime) -> float:
        tau = max((self.expiry_ts - ts).total_seconds(), TAU_FLOOR_S) / _YEAR_S
        return bs_price(spot, self.strike, self.iv, tau, self.right)


def five_minute_bars(
    bars: Sequence[PathBar], start: dt.datetime, end: dt.datetime
) -> tuple[FiveMinuteBar, ...]:
    """Uzavřené 5min bary po hodinách od `start` (US open) do `end` (settle) z 1m barů.

    Vstup smí obsahovat jen uzavřené 1m bary, rozpracovaná minuta by dala close, který se ještě
    změní. 5min bar je uzavřený, když vstup má jeho poslední minutu nebo kteroukoli pozdější.
    """
    ordered = sorted((bar for bar in bars if start <= bar.ts < end), key=lambda bar: bar.ts)
    buckets: dict[dt.datetime, FiveMinuteBar] = {}
    for bar in ordered:
        key = bar.ts - (bar.ts - start) % BAR_SPAN
        prev = buckets.get(key)
        buckets[key] = (
            FiveMinuteBar(key + BAR_SPAN, bar.high, bar.low, bar.close)
            if prev is None
            else FiveMinuteBar(
                prev.end, max(prev.high, bar.high), min(prev.low, bar.low), bar.close
            )
        )
    newest = ordered[-1].ts + dt.timedelta(minutes=1) if ordered else start
    return tuple(buckets[key] for key in sorted(buckets) if buckets[key].end <= newest)


def _against(side: int, price: float, ref: float) -> bool:
    """Leží `price` proti směru od `ref` (short: nad, long: pod)?"""
    return (price - ref) * side < 0


def _nearest_against(
    levels: Mapping[str, float], side: int, ref: float, limit: float | None
) -> tuple[str, float] | None:
    """Nejbližší úroveň z `LEVEL_SET` proti směru od `ref`, volitelně do vzdálenosti `limit`."""
    candidates = [
        (name, levels[name])
        for name in LEVEL_SET
        if name in levels
        and _against(side, levels[name], ref)
        and (limit is None or abs(levels[name] - ref) <= limit)
    ]
    return min(candidates, key=lambda item: abs(item[1] - ref), default=None)


def _trigger_a(
    window: Sequence[FiveMinuteBar], side: int, level: tuple[str, float], reserve: float, wick: bool
) -> Entry | None:
    """K3 body 2–3: test úrovně (5min close za ní, u stínu knot), pak první 5min close zpět.

    SL = 5min close za extrémem od testu + rezerva. Hledá se jednou (K6 B).
    """
    name, price = level
    tested, swing = False, 0.0
    for bar in window:
        extreme = bar.high if side < 0 else bar.low
        if not tested:
            if ((extreme if wick else bar.close) - price) * side > 0:
                continue
            tested, swing = True, extreme
            if not (wick and (bar.close - price) * side > 0):
                continue
        swing = max(swing, extreme) if side < 0 else min(swing, extreme)
        if (bar.close - price) * side > 0:
            return Entry("A", bar.end, bar.close, swing - side * reserve, name, price)
    return None


def _trigger_b(
    window: Sequence[FiveMinuteBar],
    side: int,
    scenario_entry: float,
    sl_level: tuple[str, float],
    em: float,
) -> Entry | None:
    """K3 bod 4: první 5min close za vstupem scénáře ∓ 0,05 EM; SL za nejbližší úrovní."""
    threshold = scenario_entry + side * B_TRIGGER_EM * em
    stop = sl_level[1] - side * RESERVE_EM * em
    for bar in window:
        if (bar.close - threshold) * side > 0:
            return Entry("B", bar.end, bar.close, stop, sl_level[0], sl_level[1])
    return None


def _mid(row: ChainRowLike) -> float:
    """Mid zaplacené kotace, jinak 0 (strana chybí, #469)."""
    if row.bid is None or row.ask is None or not (row.bid > 0 and row.ask > 0):
        return 0.0
    return (row.bid + row.ask) / 2.0


def _straddle_em(chain: Sequence[ChainRowLike], spot: float) -> tuple[float, float] | None:
    """(ATM strike, EM) ze straddlu 0DTE (`emrespect.straddle_em`)."""
    mids = {(row.strike, row.right): _mid(row) for row in chain}
    quotes = [
        StraddleQuote(strike, mids.get((strike, "C"), 0.0), mids.get((strike, "P"), 0.0))
        for strike in sorted({strike for strike, _ in mids})
    ]
    return straddle_em(quotes, spot)


def _gex_top(chain: Sequence[ChainRowLike], spot: float, side: int, em: float) -> GexTop | None:
    """P1 nad řetězem 0DTE; None = řetěz bez gamma a OI (P1 bez dat)."""
    options = [
        GexInput(strike=row.strike, right=row.right, gamma=row.gamma, oi=row.oi)
        for row in chain
        if row.gamma is not None and row.oi is not None and row.gamma > 0 and row.oi > 0
    ]
    if not options:
        return None
    per_strike = GexEngine().compute(options, spot, EQUITY_OPTION_MULTIPLIER).per_strike
    best = max(per_strike, key=lambda row: row.net_gex_1pct)
    if best.net_gex_1pct <= 0:
        return GexTop(None, None, None, blocks=False)
    distance = abs(best.strike - spot) / em
    blocks = _against(side, best.strike, spot) and distance <= P1_LIMIT_EM
    return GexTop(best.strike, best.net_gex_1pct, distance, blocks)


def _quoted(row: ChainRowLike, right: str) -> bool:
    """Kotace použitelná pro variantu: zaplacený bid, ask ≥ bid, IV a delta."""
    return (
        row.right == right
        and _positive(row.bid)
        and row.ask is not None
        and row.bid is not None
        and row.ask >= row.bid
        and _positive(row.iv)
        and _finite(row.delta)
    )


def _pick_variants(
    chain: Sequence[ChainRowLike], right: str, side: int
) -> tuple[ChainRowLike | None, ChainRowLike | None]:
    """Hlavní = |delta| nejblíž 0,25, záložní = sousední strike dál od peněz (bod 4)."""
    rows = [row for row in chain if _quoted(row, right)]
    if not rows:
        return None, None
    main = min(rows, key=lambda row: (abs(abs(row.delta or 0.0) - TARGET_DELTA), row.strike))
    further = [row for row in rows if (row.strike - main.strike) * side > 0]
    backup = min(further, key=lambda row: abs(row.strike - main.strike), default=None)
    return main, backup


def em_gap(
    chain_0dte: Sequence[ChainRowLike], strikes: Collection[float], spot: float
) -> str | None:
    """Proč EM z řádků 0DTE nevznikne na strike nejbližším spotu; None = vznikne (K4 bod 7, B).

    `strikes` = striky pásma s callem i putem. Chybí-li na nejbližším z nich zaplacená strana,
    `_straddle_em` by tiše vzal vzdálenější strike a EM by neslo jiný straddle než celý řetěz.
    """
    if not strikes:
        return "pásmo bez striku s callem i putem"
    atm = min(strikes, key=lambda strike: (abs(strike - spot), strike))
    found = _straddle_em(chain_0dte, spot)
    if found is not None and found[0] == atm:
        return None
    paid = {(row.strike, row.right) for row in chain_0dte if _mid(row) > 0}
    sides = [name for right, name in (("C", "call"), ("P", "put")) if (atm, right) not in paid]
    return f"ATM straddle {atm:g} bez zaplacené kotace {' a '.join(sides)}"


def variant_gaps(
    chain_1dte: Sequence[ChainRowLike], strikes: Mapping[str, Collection[float]]
) -> tuple[str, ...]:
    """Proč výběr varianty z řádků 1DTE může uhnout od celého řetězu; prázdné = neuhne (K4 bod 7).

    Kontroluje call i put, protože směr scénáře snímek řetězu nezná. `strikes` = striky pásma
    per strana (`C`, `P`). Hlavní varianta (`_pick_variants`) musí mít oba sousední striky pásma
    s kotací. Delta je ve striku monotónní, takže chybějící kontrakt dál od nich by k 0,25 blíž
    nebyl. Záložní varianta je pak soused dál od peněz.
    """
    gaps: list[str] = []
    for right, side, name in (("C", 1, "call"), ("P", -1, "put")):
        main, _backup = _pick_variants(chain_1dte, right, side)
        if main is None:
            gaps.append(f"{name} bez kotace s IV a delta")
            continue
        ladder = sorted(strikes.get(right, ()))
        if main.strike not in ladder:
            raise ValueError(f"strike {main.strike:g}{right} řádku mimo striky pásma")
        index = ladder.index(main.strike)
        if index == 0 or index == len(ladder) - 1:
            gaps.append(f"{name} |delta| 0,25 = {main.strike:g} na okraji pásma")
            continue
        quoted = {row.strike for row in chain_1dte if _quoted(row, right)}
        missing = [ladder[i] for i in (index - 1, index + 1) if ladder[i] not in quoted]
        if missing:
            gaps.append(
                f"{name} |delta| 0,25 = {main.strike:g}: soused "
                + " a ".join(f"{strike:g}" for strike in missing)
                + " bez kotace s IV a delta"
            )
    return tuple(gaps)


def _clamp(ts: dt.datetime, low: dt.datetime, high: dt.datetime) -> dt.datetime:
    return min(max(ts, low), high)


def _rr(reward: float, paid: float, at_stop: float) -> float | None:
    return reward / (paid - at_stop) if paid > at_stop else None


def _pricing(
    model: _Model,
    entry: Entry,
    *,
    side: int,
    em: float,
    targets: Sequence[float],
    target_ts: Sequence[dt.datetime],
    exit_ts: dt.datetime,
    account_usd: float | None,
) -> Pricing:
    """P3 a P4 při spoušti (K3 body 6 a 7, K11 A)."""
    multiplier = EQUITY_OPTION_MULTIPLIER
    paid = model.price(entry.price, entry.ts)
    horizon = _clamp(target_ts[0], entry.ts, exit_ts)
    at_stop = model.price(entry.stop, horizon)
    at_tp1 = model.price(targets[0], horizon)
    at_tp2 = (
        model.price(targets[1], _clamp(target_ts[1], entry.ts, exit_ts))
        if len(targets) > 1
        else None
    )
    strict = model.price(entry.stop, exit_ts)
    worst = model.price(entry.stop - side * P4_EXTRA_EM * em, exit_ts)
    loss = (at_stop - paid) * multiplier
    drawdown = (worst - paid) * multiplier

    def pct(usd: float) -> float | None:
        return 100.0 * usd / account_usd if _positive(account_usd) and account_usd else None

    return Pricing(
        paid=paid,
        at_stop=at_stop,
        at_tp1=at_tp1,
        at_tp2=at_tp2,
        rr=_rr(at_tp1 - paid, paid, at_stop),
        rr_strict=_rr(at_tp1 - paid, paid, strict),
        loss_at_stop_usd=loss,
        drawdown_usd=drawdown,
        pnl_tp1_usd=(at_tp1 - paid) * multiplier,
        pnl_tp2_usd=(at_tp2 - paid) * multiplier if at_tp2 is not None else None,
        loss_at_stop_pct=pct(loss),
        drawdown_pct=pct(drawdown),
    )


def _verdict(
    pricing: Pricing | None, *, account: AccountParams, waiting: str, blocked: str | None
) -> tuple[Verdict, tuple[str, ...]]:
    """Verdikt varianty: parametry účtu (K2), vstup (K3), P3 a P4."""
    reasons: list[str] = []
    limit = account.max_loss_usd
    unset = [
        name
        for name, value in (("rozpočet na obchod", limit), ("velikost účtu", account.account_usd))
        if not _positive(value)
    ]
    if unset:
        reasons.append(f"rozpočet nenastaven (chybí {' a '.join(unset)})")
    if blocked is not None:
        reasons.append(blocked)
    if pricing is not None:
        if pricing.rr is None or pricing.rr < MIN_RR:
            rr = "nejde určit" if pricing.rr is None else _cz(pricing.rr)
            reasons.append(f"R:R {rr} < {_cz(MIN_RR, 0)} (P3)")
        if limit is not None and _positive(limit) and -pricing.drawdown_usd > limit:
            reasons.append(f"nad rozpočtem (P4: čerpání {_cz(pricing.drawdown_usd, 0)} $)")
    if reasons:
        return "neobchodovat", tuple(reasons)
    if pricing is None:
        return "čeká", (waiting,)
    return "obchodovat", ()


def _p5(side: int, entry: Entry | None, main: OptionVariant | None, em: float | None) -> str:
    """P5: stop navázaný na podklad, podmíněný příkaz v TWS."""
    option = f"{'call' if side > 0 else 'put'} {main.strike:g}" if main is not None else "opci"
    sign = "≥" if side < 0 else "≤"
    tail = "Podmínka TWS reaguje na cenu (poslední obchod), ne na 5min close plánu."
    if entry is not None:
        return (
            f"Podmíněný příkaz v TWS: když QQQ {sign} {_cz(entry.stop)} → prodat {option}. {tail}"
        )
    reserve = f" + {_cz(RESERVE_EM * em)}" if em is not None else ""
    return (
        f"SL se určí při spoušti vstupu A (5min close za extrémem od testu{reserve}), pak "
        f"podmíněný příkaz v TWS: když QQQ {sign} SL → prodat {option}. {tail}"
    )


@dataclass(frozen=True)
class _Entries:
    level_a: tuple[str, float] | None = None
    trigger_a: Entry | None = None
    trigger_b: Entry | None = None
    chosen: Entry | None = None
    #: Proč vstup nebude (verdikt neobchodovat); None = čeká, nebo je spuštěný
    blocked: str | None = None


def _entries(
    *,
    levels: Mapping[str, float],
    side: int,
    scenario_entry: float,
    em: float,
    p1: GexTop | None,
    window: Sequence[FiveMinuteBar],
    window_done: bool,
    rules: PlanRules,
) -> _Entries:
    """Úroveň a spoušť vstupu A (K3 body 1–3), u stínu i vstup B podle P1 (body 4 a 8)."""
    level_a = _nearest_against(levels, side, scenario_entry, LEVEL_LIMIT_EM * em)
    trigger_a = (
        _trigger_a(window, side, level_a, RESERVE_EM * em, rules.wick_test) if level_a else None
    )
    sl_level = _nearest_against(levels, side, scenario_entry, None)
    trigger_b = (
        _trigger_b(window, side, scenario_entry, sl_level, em)
        if rules.entry_b and sl_level is not None
        else None
    )
    b_allowed = p1 is None or not p1.blocks  # P1 bez dat vstup B nevyřadí (pravidla v1)
    candidates = [e for e in (trigger_a, trigger_b if b_allowed else None) if e is not None]
    chosen = min(candidates, key=lambda e: (e.ts, e.kind), default=None)
    blocked = None
    if chosen is None and level_a is None and not (rules.entry_b and sl_level is not None):
        where = (
            f"nejbližší {LEVEL_LABELS[sl_level[0]]} {_cz(sl_level[1])} = "
            f"{_cz(abs(sl_level[1] - scenario_entry) / em)} EM"
            if sl_level is not None
            else "žádná úroveň proti směru"
        )
        blocked = f"vstup A není: žádná úroveň do {_cz(LEVEL_LIMIT_EM, 0)} EM ({where})"
    elif chosen is None and window_done:
        blocked = "bez vstupu do exitu"
    return _Entries(level_a, trigger_a, trigger_b, chosen, blocked)


def _variants(
    chain: Sequence[ChainRowLike],
    *,
    right: Literal["C", "P"],
    side: int,
    expiry: dt.date,
    entries: _Entries,
    em: float | None,
    targets: tuple[float, ...],
    target_ts: tuple[dt.datetime, ...],
    now: dt.datetime,
    exit_ts: dt.datetime,
    account: AccountParams,
    missing: list[str],
) -> tuple[OptionVariant, ...]:
    """Hlavní a záložní varianta (bod 4) s modelem při TP a při spoušti a s verdiktem."""
    if not chain:
        missing.append("řetěz QQQ 1DTE nedostupný")
        return ()
    main, backup = _pick_variants(chain, right, side)
    if main is None:
        missing.append(f"řetěz QQQ 1DTE bez kotací {'call' if side > 0 else 'put'} s IV a delta")
        return ()
    if backup is None:
        missing.append("záložní varianta: v řetězu 1DTE není strike dál od peněz")
    level = entries.level_a
    waiting = (
        f"čeká na vstup A ({LEVEL_LABELS[level[0]]} {_cz(level[1])})" if level else "čeká na vstup"
    )
    blocked = entries.blocked if em is not None else "bez EM nejde určit vstup ani SL"
    roles: tuple[tuple[Literal["hlavní", "záložní"], ChainRowLike | None], ...] = (
        ("hlavní", main),
        ("záložní", backup),
    )
    out: list[OptionVariant] = []
    for role, row in roles:
        if row is None or row.bid is None or row.ask is None or row.iv is None:
            continue
        model = _Model(right, row.strike, row.iv, settle_ts(expiry))
        pricing = (
            _pricing(
                model,
                entries.chosen,
                side=side,
                em=em,
                targets=targets,
                target_ts=target_ts,
                exit_ts=exit_ts,
                account_usd=account.account_usd,
            )
            if entries.chosen is not None and em is not None
            else None
        )
        verdict, reasons = _verdict(pricing, account=account, waiting=waiting, blocked=blocked)
        out.append(
            OptionVariant(
                role=role,
                right=right,
                strike=row.strike,
                expiry=expiry,
                bid=row.bid,
                ask=row.ask,
                iv=row.iv,
                delta=row.delta or 0.0,
                quote_ts=row.ts_min,
                at_targets=tuple(
                    model.price(target, _clamp(ts, now, exit_ts))
                    for target, ts in zip(targets, target_ts, strict=True)
                ),
                pricing=pricing,
                verdict=verdict,
                reasons=reasons,
            )
        )
    return tuple(out)


def build_option_plan(
    *,
    scenario: AutoScenario,
    levels: LevelInputs,
    ratio: float | None,
    spot: float | None,
    chain_0dte: Sequence[ChainRowLike],
    chain_1dte: Sequence[ChainRowLike],
    expiry_1dte: dt.date,
    account: AccountParams,
    now: dt.datetime,
    bars: Sequence[PathBar] = (),
    rules: PlanRules = MAIN_RULES,
) -> OptionPlan:
    """Plán opce QQQ v minutě `now` z auto-scénáře NQ a úrovní NQ téže minuty.

    `now` je 9:15 ET se scénářem dne (K4 C), nebo 9:31 ET, když ranní snímek nebyl úplný
    (K4 bod 5). Řetěz i poměr jsou z téhož okamžiku, 9:15 a 9:31 se nemíchají.

    - `ratio` = NQ/QQQ z close poslední uzavřené minuty obou před vznikem, `spot` = cena QQQ.
    - `bars` = 1m bary QQQ. Prázdné = plán při vzniku. S bary po vzniku se hledá spoušť
      v okně od prvního 5min close do exitu (K3 bod 5).
    - Chybějící vstup = viditelná mezera v `missing`, ne odhad (K4 a). Verdikt je
      `neobchodovat`, když bez něj vstup nebo opce nejde (poměr, spot, EM, úroveň A, řetěz 1DTE).
    - Kotace řetězu po `now` jsou chyba volajícího (pohled dopředu) → `ValueError`.
    """
    day = trading_session_date(now)
    if not is_trading_session(day):
        raise ValueError(f"plán QQQ vzniká jen v obchodní seanci, {day} ji nemá (#1309)")
    if expiry_1dte <= day or not is_trading_session(expiry_1dte):
        raise ValueError(f"expirace 1DTE {expiry_1dte} musí být obchodní seance po {day}")
    late = [row for row in (*chain_0dte, *chain_1dte) if row.ts_min > now]
    if late:
        raise ValueError(f"řetěz má {len(late)} kotací po vzniku plánu {now:%H:%M} UTC")
    if not scenario.targets or len(scenario.path) != len(scenario.targets) + 1:
        raise ValueError("cesta scénáře = vstup a bod na každý cíl")
    side = 1 if scenario.direction == "long" else -1
    right: Literal["C", "P"] = "C" if side > 0 else "P"
    exit_ts = settle_ts(day) - EXIT_BEFORE_SETTLE
    target_ts = tuple(ts for ts, _ in scenario.path[1:])
    missing: list[str] = []
    if not _positive(ratio):
        missing.append("poměr NQ/QQQ chybí")
    if not _positive(spot):
        missing.append("spot QQQ chybí")
    converted: dict[str, float] = {}
    scenario_entry: float | None = None
    targets: tuple[float, ...] = ()
    em_found: tuple[float, float] | None = None
    p1: GexTop | None = None
    entries = _Entries()
    variants: tuple[OptionVariant, ...] = ()
    if ratio is not None and spot is not None and not missing:
        for name, label in LEVEL_LABELS.items():
            value = getattr(levels, name)
            if _finite(value):
                converted[name] = value / ratio
            else:
                missing.append(f"chybí úroveň {label}")
        scenario_entry = scenario.entry / ratio
        targets = tuple(target / ratio for target in scenario.targets)
        em_found = _straddle_em(chain_0dte, spot) if chain_0dte else None
        if not chain_0dte:
            missing.append("řetěz QQQ 0DTE nedostupný (EM, P1)")
        elif em_found is None:
            missing.append("EM QQQ: řetěz 0DTE bez zaplaceného straddlu u spotu")
        if em_found is not None:
            em = em_found[1]
            p1 = _gex_top(chain_0dte, spot, side, em)
            if p1 is None and rules.entry_b:  # hlavní plán P1 nepotřebuje (K6 B)
                missing.append("P1 bez dat: řetěz 0DTE bez gamma a OI")
            fives = five_minute_bars(bars, us_open_ts(day), settle_ts(day))
            entries = _entries(
                levels=converted,
                side=side,
                scenario_entry=scenario_entry,
                em=em,
                p1=p1,
                window=[bar for bar in fives if now < bar.end < exit_ts],
                window_done=any(bar.end >= exit_ts for bar in fives),
                rules=rules,
            )
        variants = _variants(
            chain_1dte,
            right=right,
            side=side,
            expiry=expiry_1dte,
            entries=entries,
            em=em_found[1] if em_found else None,
            targets=targets,
            target_ts=target_ts,
            now=now,
            exit_ts=exit_ts,
            account=account,
            missing=missing,
        )
    em_value = em_found[1] if em_found else None
    return OptionPlan(
        rules_version=RULES_VERSION,
        rules=rules.name,
        day=day,
        created_at=now,
        direction=scenario.direction,
        right=right,
        ratio=ratio,
        spot=spot,
        em=em_value,
        em_strike=em_found[0] if em_found else None,
        chain_0dte_ts=max((row.ts_min for row in chain_0dte), default=None),
        scenario_entry=scenario_entry,
        targets=targets,
        target_ts=target_ts,
        levels=converted,
        exit_ts=exit_ts,
        level_a=entries.level_a,
        p1=p1,
        trigger_a=entries.trigger_a,
        trigger_b=entries.trigger_b,
        entry=entries.chosen,
        variants=variants,
        missing=tuple(missing),
        p5=_p5(side, entries.chosen, variants[0] if variants else None, em_value),
    )


def reprice_variants(
    variants: Sequence[VariantKey],
    *,
    chain_1dte: Sequence[ChainRowLike],
    expiry_1dte: dt.date,
    targets: Sequence[float],
    target_ts: Sequence[dt.datetime],
    exit_ts: dt.datetime,
    now: dt.datetime,
) -> tuple[tuple[RepricedVariant, ...], tuple[str, ...]]:
    """Přecenění kontraktů plánu z 9:15 ET řetězem 1DTE v `now` (9:31 ET, K4 C bod 2).

    Vrací (sady cen, mezery). Strike, úrovně, vstup A a EM se nemění, nový je jen řádek
    téhož kontraktu. Mezera = kontrakt bez kotace s IV a deltou (stejné kritérium jako výběr
    varianty), jiná expirace 1DTE, nebo plán bez varianty. S mezerou se nezapíše nic
    (K4 bod 6) a sady cen jsou prázdné. Kotace po `now` jsou chyba volajícího (pohled
    dopředu) → `ValueError`.
    """
    late = [row for row in chain_1dte if row.ts_min > now]
    if late:
        raise ValueError(f"řetěz má {len(late)} kotací po přecenění {now:%H:%M} UTC")
    if len(targets) != len(target_ts):
        raise ValueError("cíle a jejich časy mají různou délku")
    if not variants:
        return (), ("plán z 9:15 ET nemá variantu opce",)
    gaps: list[str] = []
    out: list[RepricedVariant] = []
    for key in variants:
        name = f"{key.role} {'call' if key.right == 'C' else 'put'} {key.strike:g}"
        if key.expiry != expiry_1dte:
            gaps.append(f"{name}: expirace {key.expiry} ≠ 1DTE snímku {expiry_1dte}")
            continue
        row = next(
            (r for r in chain_1dte if r.strike == key.strike and _quoted(r, key.right)), None
        )
        if row is None or row.bid is None or row.ask is None or row.iv is None:
            gaps.append(f"{name} bez kotace s IV a delta")
            continue
        model = _Model(key.right, key.strike, row.iv, settle_ts(key.expiry))
        out.append(
            RepricedVariant(
                role=key.role,
                right=key.right,
                strike=key.strike,
                expiry=key.expiry,
                bid=row.bid,
                ask=row.ask,
                iv=row.iv,
                delta=row.delta or 0.0,
                quote_ts=row.ts_min,
                at_targets=tuple(
                    model.price(target, _clamp(ts, now, exit_ts))
                    for target, ts in zip(targets, target_ts, strict=True)
                ),
            )
        )
    if gaps:
        return (), tuple(gaps)
    return tuple(out), ()


def jsonable(value: object, path: str = "plán") -> Any:
    """Hodnota plánu jako JSON: pole dataclass, časy ISO 8601 v UTC, n-tice jako seznamy.

    Nekonečné číslo nebo NaN je chyba výpočtu, ne mezera → `ValueError` s cestou k poli.
    PG by ho do JSON nepřijal a `null` by chybu skryl. Naivní čas je chyba volajícího.
    """
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: jsonable(getattr(value, item.name), f"{path}.{item.name}")
            for item in dataclasses.fields(value)
        }
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            raise ValueError(f"{path}: čas bez časové zóny {value!r}")
        return value.astimezone(dt.UTC).isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path}: číslo {value!r} nejde do JSON")
        return value
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, Mapping):
        return {str(key): jsonable(item, f"{path}.{key}") for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [jsonable(item, f"{path}[{index}]") for index, item in enumerate(value)]
    raise TypeError(f"{path}: typ {type(value).__name__} nejde do JSON")


def plan_to_json(plan: OptionPlan) -> dict[str, Any]:
    """Plán pro JSON `option_plans.plan` (ADR-0062 K2 A): všechna pole a text verdiktu plánu."""
    out: dict[str, Any] = jsonable(plan)
    out["verdict"] = plan.verdict
    return out
