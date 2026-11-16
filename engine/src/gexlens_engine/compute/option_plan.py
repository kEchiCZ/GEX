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
vzniku, funkce najde spoušť vstupu A a ocení ji modelem (P3, P4). Do JSON tabulky
`option_plans` plán převede `plan_to_json` (K2 A).

Vyhodnocení po close (F3 #1748, `evaluate_option_plan`) hledá spoušť toutéž funkcí nad 1m bary
QQQ celé seance a sleduje průběh po vstupu (SL, TP1, TP2, stop na vstupu, exit). R:R při
spoušti (P3) a čerpání P4 počítá z naměřené kotace v minutě spouště, P&L na kontrakt
z kotací v minutách událostí (K3 bod 9, Rozhodnuto 16. 11.). Track record (`track_record`)
počítá plány od `TRACK_RECORD_SINCE`.

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
    ET_TZ,
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
    except OverflowError:  # celé číslo mimo rozsah float (JSON nemá strop počtu číslic)
        raise ValueError("číslo musí být kladné a konečné") from None
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


# ── vyhodnocení plánu (F3 #1748) ────────────────────────────────────

#: Začátek track recordu plánu QQQ (ADR-0062 bod 7; Rozhodnuto 10. a 16. 11., #1738): první
#: seance s nasazenými opravami vstupů plánu #1774, #1653 + #1599 a #1610. Datum je vstup
#: `track_record`, zápis výsledků ho nefiltruje: kolektor vyhodnotí každý plán. None = datum
#: zatím není, track record je prázdný a vyhodnocené plány jsou „mimo track record“.
TRACK_RECORD_SINCE: dt.date | None = None
#: Brána `preliminary`: track record pod 30 případů je předběžný (ADR-0035, ADR-0062 bod 7)
PRELIMINARY_N = 30
#: Stíny track recordu (K10 B) v `plan.shadows`; klíč = parametr pravidel
SHADOWS: Mapping[str, PlanRules] = {"entry_b": SHADOW_B, "wick_test": SHADOW_WICK}
MINUTE = dt.timedelta(minutes=1)

ResultStatus = Literal["spuštěno", "bez vstupu", "bez opce", "mezera"]
VariantKind = Literal["obchod", "vyřazeno P3", "bez kotace"]
Budget = Literal["v rozpočtu", "nad rozpočtem", "nenastaven"]
EventKind = Literal["vstup", "TP1", "TP2", "SL", "stop na vstupu", "exit"]
#: Průběh po vstupu, stejné stavy jako zpětné přehrání #1756 (`manage`)
Outcome = Literal["SL", "TP1", "TP2", "TP1→vstup", "TP1→exit", "exit", "cíl 1 za vstupem"]
RESULT_STATUSES: tuple[ResultStatus, ...] = ("spuštěno", "bez vstupu", "bez opce", "mezera")
OUTCOMES: tuple[Outcome, ...] = (
    "SL",
    "TP1",
    "TP2",
    "TP1→vstup",
    "TP1→exit",
    "exit",
    "cíl 1 za vstupem",
)
BUDGETS: tuple[Budget, ...] = ("v rozpočtu", "nad rozpočtem", "nenastaven")
ROLES: tuple[Literal["hlavní", "záložní"], ...] = ("hlavní", "záložní")


class QuoteLike(Protocol):
    """Kotace kontraktu plánu v závěru minuty; tvar `storage.parquet_store.OptionQuoteRow`."""

    @property
    def ts_min(self) -> dt.datetime: ...

    @property
    def expiry(self) -> str: ...

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
    def quote_age_s(self) -> float | None: ...


@dataclass(frozen=True)
class PathEvent:
    """Událost na podkladu QQQ. Kotace opce k ní je řádek minuty, která v `ts` končí."""

    kind: EventKind
    #: Závěr minuty události: 5min close (vstup, SL, stop na vstupu), 1m bar s dotykem cíle, exit
    ts: dt.datetime
    #: Cena QQQ: 5min close (vstup, SL, stop na vstupu), cíl (TP), close minuty před exitem
    price: float


@dataclass(frozen=True)
class QuoteAt:
    """Naměřená kotace kontraktu v minutě události (K3 bod 9)."""

    ts_min: dt.datetime
    bid: float | None
    ask: float | None
    iv: float | None
    quote_age_s: float | None


@dataclass(frozen=True)
class OptionExit:
    """Výstup z opce za bid v minutě události, P&L na 1 kontrakt (K3 bod 9)."""

    event: EventKind
    ts: dt.datetime
    bid: float | None
    quote_age_s: float | None
    #: None = kotace v minutě výstupu chybí (mezera, ne odhad)
    pnl_usd: float | None


@dataclass(frozen=True)
class VariantResult:
    """Varianta opce po spoušti: P3 a P4 z kotace při spoušti, P&L z kotací událostí."""

    role: Literal["hlavní", "záložní"]
    right: Literal["C", "P"]
    strike: float
    expiry: dt.date
    #: `obchod` (projde P3), `vyřazeno P3` (hypotetický výsledek zvlášť), `bez kotace` (mezera)
    kind: VariantKind
    entry_quote: QuoteAt | None
    #: R:R z kotace: vstup za ask, SL a TP1 modelem s IV téže kotace (Rozhodnuto 16. 11.: A)
    rr: float | None
    at_stop: float | None
    at_tp1: float | None
    #: P4 v $ na kontrakt: ztráta při SL a čerpání na SL + 0,2 EM v exitu (K3 bod 7)
    loss_at_stop_usd: float | None
    drawdown_usd: float | None
    #: Třída podle rozpočtu ze Settings při vyhodnocení (Rozhodnuto 16. 11.); hodnota se neukládá
    budget: Budget | None
    #: Výstup v TP1 (bez TP1 v SL, nebo v exitu) a držení do TP2 se stopem na vstupu
    exit_tp1: OptionExit | None
    exit_hold: OptionExit | None
    gaps: tuple[str, ...]


@dataclass(frozen=True)
class PlanResult:
    """Výsledek části plánu (hlavní plán nebo stín K10 B) z naměřených barů a kotací."""

    rules: str
    status: ResultStatus
    #: Proč bez vstupu, bez opce nebo mezera
    reasons: tuple[str, ...]
    entry: Entry | None
    #: Spoušť vstupu B u stínu B, i když ji P1 vyřadí
    trigger_b: Entry | None
    outcome: Outcome | None
    events: tuple[PathEvent, ...]
    variants: tuple[VariantResult, ...]
    #: 1m barů QQQ od US openu do rozhodnutí: uložených / očekávaných
    bars: int
    bars_expected: int


def _et(ts: dt.datetime) -> str:
    return ts.astimezone(ET_TZ).strftime("%H:%M")


def _ts(value: object, path: str) -> dt.datetime:
    ts = dt.datetime.fromisoformat(str(value))
    if ts.tzinfo is None:
        raise ValueError(f"{path}: čas bez časové zóny {value!r}")
    return ts


def _bare(rules: PlanRules, status: ResultStatus, reasons: Sequence[str]) -> PlanResult:
    return PlanResult(rules.name, status, tuple(reasons), None, None, None, (), (), 0, 0)


def no_option_result(reasons: Sequence[str]) -> PlanResult:
    """Výsledek plánu bez opce (sloupec `status` = `bez opce`): není co ocenit."""
    return _bare(MAIN_RULES, "bez opce", tuple(reasons) or ("bez opce",))


def _walk(
    entry: Entry,
    *,
    side: int,
    targets: Sequence[float],
    rth: Sequence[PathBar],
    open_ts: dt.datetime,
    exit_ts: dt.datetime,
) -> tuple[Outcome, tuple[PathEvent, ...]]:
    """Průběh po vstupu (K3 bod 5): TP na dotyk v 1m baru, SL na 5min close, po TP1 SL na vstup.

    V minutě se kontroluje TP1, pak TP2, pak 5min close (končí-li v ní 5min bar), jako
    zpětné přehrání #1756. `rth` jsou 1m bary od US openu do exitu (bez minuty, která
    v exitu začíná). Bez rozhodnutí do exitu = výstup v exitu za close poslední minuty.
    """
    events = [PathEvent("vstup", entry.ts, entry.price)]
    tp1 = targets[0]
    tp2 = targets[1] if len(targets) > 1 else None
    if (tp1 - entry.price) * side <= 0:
        return "cíl 1 za vstupem", tuple(events)
    stop, hit1, last = entry.stop, False, entry.price
    for bar in rth:
        if bar.ts < entry.ts:
            continue
        end = bar.ts + MINUTE
        last = bar.close
        favorable = bar.low if side < 0 else bar.high
        if not hit1 and (favorable - tp1) * side >= 0:
            events.append(PathEvent("TP1", end, tp1))
            if tp2 is None:
                return "TP1", tuple(events)
            hit1, stop = True, entry.price
        if hit1 and tp2 is not None and (favorable - tp2) * side >= 0:
            events.append(PathEvent("TP2", end, tp2))
            return "TP2", tuple(events)
        if (end - open_ts) % BAR_SPAN == dt.timedelta(0) and (bar.close - stop) * side < 0:
            events.append(PathEvent("stop na vstupu" if hit1 else "SL", end, bar.close))
            return ("TP1→vstup" if hit1 else "SL"), tuple(events)
    events.append(PathEvent("exit", exit_ts, last))
    return ("TP1→exit" if hit1 else "exit"), tuple(events)


def _budget(drawdown_usd: float, max_loss_usd: float | None) -> Budget:
    """Třída P4 podle rozpočtu ze Settings; srovnání jako verdikt plánu (`_verdict`)."""
    if max_loss_usd is None or not _positive(max_loss_usd):
        return "nenastaven"
    return "nad rozpočtem" if -drawdown_usd > max_loss_usd else "v rozpočtu"


def _variant_result(
    variant: Mapping[str, Any],
    *,
    entry: Entry,
    events: Sequence[PathEvent],
    side: int,
    em: float,
    targets: Sequence[float],
    target_ts: Sequence[dt.datetime],
    exit_ts: dt.datetime,
    quotes: Mapping[tuple[str, float, str, dt.datetime], QuoteLike],
    max_loss_usd: float | None,
) -> VariantResult:
    """P3, P4 a P&L varianty z naměřených kotací (K3 bod 9, Rozhodnuto 16. 11.)."""
    role, right = variant["role"], variant["right"]
    strike, expiry = float(variant["strike"]), dt.date.fromisoformat(variant["expiry"])
    key = (expiry.strftime("%Y%m%d"), strike, right)
    name = f"{role} {'call' if right == 'C' else 'put'} {strike:g}"
    gaps: list[str] = []
    row = quotes.get((*key, entry.ts - MINUTE))
    entry_quote = (
        QuoteAt(row.ts_min, row.bid, row.ask, row.iv, row.quote_age_s) if row is not None else None
    )
    if row is None or row.ask is None or not _positive(row.ask) or not _positive(row.iv):
        what = "ask a IV" if row is not None else "řádek"
        return VariantResult(
            role=role,
            right=right,
            strike=strike,
            expiry=expiry,
            kind="bez kotace",
            entry_quote=entry_quote,
            rr=None,
            at_stop=None,
            at_tp1=None,
            loss_at_stop_usd=None,
            drawdown_usd=None,
            budget=None,
            exit_tp1=None,
            exit_hold=None,
            gaps=(f"{name}: kotace při vstupu ({_et(entry.ts)} ET) chybí — {what}",),
        )
    paid, iv = row.ask, float(row.iv or 0.0)
    model = _Model(right, strike, iv, settle_ts(expiry))
    horizon = _clamp(target_ts[0], entry.ts, exit_ts)
    at_stop = model.price(entry.stop, horizon)
    at_tp1 = model.price(targets[0], horizon)
    worst = model.price(entry.stop - side * P4_EXTRA_EM * em, exit_ts)
    multiplier = EQUITY_OPTION_MULTIPLIER
    rr = _rr(at_tp1 - paid, paid, at_stop)
    drawdown = (worst - paid) * multiplier

    def leave(event: PathEvent) -> OptionExit:
        quote = quotes.get((*key, event.ts - MINUTE))
        bid = quote.bid if quote is not None else None
        if bid is None:
            gaps.append(f"{name}: kotace při {event.kind} ({_et(event.ts)} ET) chybí")
        return OptionExit(
            event.kind,
            event.ts,
            bid,
            quote.quote_age_s if quote is not None else None,
            (bid - paid) * multiplier if bid is not None else None,
        )

    exits = (leave(events[1]), leave(events[-1])) if len(events) > 1 else (None, None)
    return VariantResult(
        role=role,
        right=right,
        strike=strike,
        expiry=expiry,
        kind="obchod" if rr is not None and rr >= MIN_RR else "vyřazeno P3",
        entry_quote=entry_quote,
        rr=rr,
        at_stop=at_stop,
        at_tp1=at_tp1,
        loss_at_stop_usd=(at_stop - paid) * multiplier,
        drawdown_usd=drawdown,
        budget=_budget(drawdown, max_loss_usd),
        exit_tp1=exits[0],
        exit_hold=exits[1],
        gaps=tuple(gaps),
    )


def _missing_minutes(
    present: Collection[dt.datetime], start: dt.datetime, end: dt.datetime
) -> tuple[int, list[dt.datetime]]:
    expected = int((end - start) / MINUTE)
    missing = [start + i * MINUTE for i in range(expected) if start + i * MINUTE not in present]
    return expected, missing


def evaluate_plan(
    part: Mapping[str, Any],
    *,
    rules: PlanRules,
    bars: Sequence[PathBar],
    quotes: Sequence[QuoteLike],
    max_loss_usd: float | None,
) -> PlanResult:
    """Výsledek části plánu (`plan_to_json`) z 1m barů QQQ seance a minutových kotací (F3).

    Spoušť hledá táž funkce jako plán (`_entries` nad 5min bary od prvního 5min close po
    vzniku do exitu, K3 bod 5, K6 B), průběh po vstupu `_walk`. Stavy:
    - `spuštěno`: vstup a průběh do SL, TP2, stopu na vstupu, nebo exitu. Každá varianta je
      `obchod`, `vyřazeno P3` (R:R z kotace < 2, výsledek hypotetický, K3 bod 9), nebo
      `bez kotace` (mezera, chybí ask nebo IV v minutě spouště);
    - `bez vstupu`: spoušť do exitu nenastala, nebo úroveň A do 1 EM není (track record
      „nespuštěno“, ne ztráta);
    - `bez opce`: plán bez variant;
    - `mezera`: plánu chybí EM nebo vstup scénáře, nebo chybí 1m bar QQQ mezi US openem
      a rozhodnutím (chybějící minuta mohla nést spoušť, cíl nebo SL). Nic se neodhaduje.

    Kotace události je řádek minuty, která okamžikem události končí (vzorek v jejím závěru),
    vstup za ask, výstup za bid. Bez kotace výstupu je P&L té nohy None s mezerou ve variantě.
    `max_loss_usd` je rozpočet ze Settings v okamžiku vyhodnocení (třída P4, Rozhodnuto 16. 11.).
    """
    if part.get("rules") != rules.name:
        raise ValueError(f"část plánu má pravidla {part.get('rules')!r}, čekala se {rules.name!r}")
    day = dt.date.fromisoformat(part["day"])
    if not is_trading_session(day):
        raise ValueError(f"plán QQQ {day}: den bez obchodní seance se nevyhodnocuje (#1309)")
    if not part["variants"]:
        return _bare(rules, "bez opce", tuple(part["missing"]) or ("bez opce",))
    em, scenario_entry = part["em"], part["scenario_entry"]
    targets = tuple(float(target) for target in part["targets"])
    if em is None or scenario_entry is None or not targets:
        return _bare(rules, "mezera", tuple(part["missing"]) or ("plán bez EM nebo vstupu",))
    side = 1 if part["direction"] == "long" else -1
    created_at = _ts(part["created_at"], "plán.created_at")
    exit_ts = _ts(part["exit_ts"], "plán.exit_ts")
    target_ts = tuple(_ts(ts, "plán.target_ts") for ts in part["target_ts"])
    open_ts = us_open_ts(day)
    by_minute = {bar.ts: bar for bar in bars if open_ts <= bar.ts < exit_ts}
    rth = [by_minute[ts] for ts in sorted(by_minute)]
    fives = five_minute_bars(rth, open_ts, settle_ts(day))
    p1 = GexTop(**part["p1"]) if part["p1"] is not None else None
    entries = _entries(
        levels={name: float(value) for name, value in part["levels"].items()},
        side=side,
        scenario_entry=float(scenario_entry),
        em=float(em),
        p1=p1,
        window=[bar for bar in fives if created_at < bar.end < exit_ts],
        # Konec okna bez spouště posoudí úplnost barů níž; `blocked` tak nese jen důvod
        # „vstup A není“ (úroveň do 1 EM chybí), o kterém bary nerozhodují
        window_done=False,
        rules=rules,
    )
    if entries.blocked is not None:
        return _bare(rules, "bez vstupu", (entries.blocked,))
    outcome: Outcome | None = None
    events: tuple[PathEvent, ...] = ()
    decided = exit_ts
    if entries.chosen is not None:
        outcome, events = _walk(
            entries.chosen, side=side, targets=targets, rth=rth, open_ts=open_ts, exit_ts=exit_ts
        )
        decided = events[-1].ts
    expected, missing = _missing_minutes(by_minute.keys(), open_ts, decided)
    if missing:
        return dataclasses.replace(
            _bare(
                rules,
                "mezera",
                (
                    f"bary QQQ: chybí {len(missing)} z {expected} minut od US openu do "
                    f"{_et(decided)} ET (první {_et(missing[0])} ET)",
                ),
            ),
            bars=expected - len(missing),
            bars_expected=expected,
        )
    if entries.chosen is None or outcome is None:
        return dataclasses.replace(
            _bare(rules, "bez vstupu", ("bez vstupu do exitu",)),
            trigger_b=entries.trigger_b,
            bars=expected,
            bars_expected=expected,
        )
    by_contract = {(q.expiry, float(q.strike), q.right, q.ts_min): q for q in quotes}
    variants = tuple(
        _variant_result(
            variant,
            entry=entries.chosen,
            events=events,
            side=side,
            em=float(em),
            targets=targets,
            target_ts=target_ts,
            exit_ts=exit_ts,
            quotes=by_contract,
            max_loss_usd=max_loss_usd,
        )
        for variant in part["variants"]
    )
    return PlanResult(
        rules=rules.name,
        status="spuštěno",
        reasons=(),
        entry=entries.chosen,
        trigger_b=entries.trigger_b,
        outcome=outcome,
        events=events,
        variants=variants,
        bars=expected,
        bars_expected=expected,
    )


def evaluate_option_plan(
    main: Mapping[str, Any] | None,
    shadows: Mapping[str, Mapping[str, Any]],
    *,
    reasons: Sequence[str],
    bars: Sequence[PathBar],
    quotes: Sequence[QuoteLike],
    max_loss_usd: float | None,
) -> dict[str, Any]:
    """JSON `option_plans.result`: hlavní plán a stíny (K10 B) zvlášť, verze pravidel.

    `main` a `shadows` jsou části platného plánu řádku (`effective_plan`, K4 bod 10),
    `reasons` mezery plánu bez opce (u něj je `main` None).
    """
    unknown = sorted(set(shadows) - set(SHADOWS))
    if unknown:
        raise ValueError(f"neznámé stíny plánu {unknown}")
    common: dict[str, Any] = {"bars": bars, "quotes": quotes, "max_loss_usd": max_loss_usd}
    head = (
        evaluate_plan(main, rules=MAIN_RULES, **common)
        if main is not None
        else no_option_result(reasons)
    )
    return {
        "rules_version": RULES_VERSION,
        "main": jsonable(head, "výsledek.main"),
        "shadows": {
            key: jsonable(evaluate_plan(part, rules=SHADOWS[key], **common), f"výsledek.{key}")
            for key, part in shadows.items()
        },
    }


# ── track record (F3 #1748, ADR-0062 bod 7) ─────────────────────────


def _pnl(values: Sequence[float]) -> dict[str, Any]:
    total = sum(values)
    return {
        "n": len(values),
        "sum_usd": total,
        "mean_usd": total / len(values) if values else None,
        "wins": sum(1 for value in values if value > 0),
    }


def _group(pairs: Sequence[tuple[str, Mapping[str, Any]]], *, by_budget: bool) -> dict[str, Any]:
    """Obchody (nebo vyřazené P3) jedné role: průběh, P&L obou výstupů, třídy rozpočtu."""
    legs = {
        key: [
            float(variant[key]["pnl_usd"])
            for _, variant in pairs
            if variant[key] is not None and variant[key]["pnl_usd"] is not None
        ]
        for key in ("exit_tp1", "exit_hold")
    }
    out: dict[str, Any] = {
        "n": len(pairs),
        "preliminary": len(pairs) < PRELIMINARY_N,
        "outcomes": {name: sum(1 for got, _ in pairs if got == name) for name in OUTCOMES},
        "pnl_tp1": _pnl(legs["exit_tp1"]),
        "pnl_hold": _pnl(legs["exit_hold"]),
    }
    if by_budget:
        out["by_budget"] = {
            budget: _group([pair for pair in pairs if pair[1]["budget"] == budget], by_budget=False)
            for budget in BUDGETS
        }
    return out


def _section(parts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Track record jedné části plánu (hlavní plán nebo stín) přes vyhodnocené plány."""
    triggered = [part for part in parts if part["status"] == "spuštěno"]
    variants: dict[str, Any] = {}
    for role in ROLES:
        pairs = [
            (str(part["outcome"]), variant)
            for part in triggered
            for variant in part["variants"]
            if variant["role"] == role
        ]
        variants[role] = {
            "trades": _group([p for p in pairs if p[1]["kind"] == "obchod"], by_budget=True),
            "rejected_p3": _group(
                [p for p in pairs if p[1]["kind"] == "vyřazeno P3"], by_budget=True
            ),
            "no_quote": sum(1 for p in pairs if p[1]["kind"] == "bez kotace"),
        }
    return {
        "plans": len(parts),
        "preliminary": len(parts) < PRELIMINARY_N,
        "by_status": {
            status: sum(1 for part in parts if part["status"] == status)
            for status in RESULT_STATUSES
        },
        "variants": variants,
    }


def track_record(
    results: Sequence[tuple[dt.date, Mapping[str, Any]]], *, since: dt.date | None
) -> dict[str, Any]:
    """Track record plánu QQQ z výsledků (seance, JSON `result`) jedné verze pravidel.

    Počítají se plány od seance `since` (`TRACK_RECORD_SINCE`); None = track record zatím
    nezačal a všechno je `outside`. Stíny K10 B jsou zvlášť a do hlavního se nepočítají.
    Obchod = spoušť, která projde P3; vyřazené P3 nesou hypotetický výsledek zvlášť;
    `by_budget` dělí obojí podle třídy rozpočtu (Rozhodnuto 16. 11.). Brána `preliminary`
    platí v každém bloku při n < 30 (ADR-0035).
    """
    counted = [result for day, result in results if since is not None and day >= since]
    return {
        "since": since.isoformat() if since is not None else None,
        "outside": len(results) - len(counted),
        "main": _section([result["main"] for result in counted]),
        "shadows": {
            key: _section(
                [result["shadows"][key] for result in counted if key in result["shadows"]]
            )
            for key in SHADOWS
        },
    }
