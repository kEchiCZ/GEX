"""Plán opce QQQ podle ADR-0062 (#1738): měření 2. 11. 2026 (K3) a zpětné přehrání (#1756).

Jen čtení: partice `--data` a GET API (`/scenarios`, `/bars`, `/instruments`, … přes
`scenario_auto.gather_context`). Do `data/` ani do PG nic nepíše.

**Kontrola ADR (2. 11.)**: tytéž funkce jako přehrání, nad vstupy měření K3. Poměr NQ/QQQ
z baru 14:14 UTC (poslední uzavřený před vznikem scénáře), úrovně ze snímku 14:15 UTC, EM ze
straddlu QQQ 0DTE 9:23 ET, put 687 s IV 30 %. AC #1756: vstup A 16:40 SEČ za 697,62, SL 699,79,
R:R opce 3,40 (přísně 2,27), P4 −72 $, TP1 692,13 / TP2 690,72.

**Zpětné přehrání**: všechny scénáře z `GET /scenarios`, pravidla verze 1 (K3 A, okno spouště
do exitu, K4 A = plán v 9:31 ET) a alternativy. Vstupy plánu v minutě 9:31 ET:
- poměr NQ/QQQ = close posledního 1m baru s cenou NQ i QQQ uzavřeného před 9:31 ET téže
  seance. Bary QQQ v RTH aplikace nesbírala (ad-hoc pohled), proto jinak poslední dřívější
  naměřená minuta se štítkem „převzatý“; bez dřívějšího páru mezera (bez pohledu dopředu).
  Spouště, SL, TP a R:R podkladu na poměru nezávisí (všechny úrovně se dělí týmž číslem);
  ceny QQQ a $ opce se mění úměrně chybě poměru;
- flip a call/put wall NQ 0DTE z posledního snímku `levels` do 9:31 ET; ONH/ONL a PDH/PDL
  z `scenario_auto.gather_context`, tedy táž funkce jako scénář (seance od 17:00 CT D−1);
- EM = `straddle_em` nad řetězem QQQ 0DTE (kotace ≤ 10 min před plánem), jinak nad NQ 0DTE
  vydělené poměrem;
- opce: expirace = settle další obchodní seance, IV = ATM IV NQ 1DTE (průměr IV obou stran
  striku nejblíž spotu, jako `tendency._atm_iv`), strike QQQ po 1 $ s |delta| nejblíž 0,25
  (`gexfield.fallback_greeks`). Ceny jsou model `bs_price` (IV NQ 2. 11. přestřelila skutečnou
  IV QQQ 1DTE o 8–10 %);
- P1: GEX QQQ 0DTE (`compute/gex.GexEngine`) jen z řetězu ≤ 10 min před plánem, jinak „bez dat“.

Průběh (5min bary NQ / poměr od US openu): spoušť a SL na 5min close, TP na dotyk v baru
(dotyk je v baru dřív než jeho close). Po TP1 jde SL na vstup, exit = settle − 15 min.
Plán má jeden vstup, ten, který se spustí první (A, nebo B, pokud ho P1 nevyřadí). R:R opce
pod 2 = „vyřazeno P3“, hypotetický výsledek se počítá zvlášť. P3 oceňuje cíl nejpozději
v exitu (bod cesty scénáře s jedním cílem leží v settle, po exitu). Každá spoušť (A, B) se
hledá jen jednou, varianta „čekat“ je proto dolní mez.

Spuštění (z kořene worktree; `data` = produkční adresář, API produkce na :8010):
    PYTHONPATH=engine/src python scripts/measure_option_plan_entries_1738.py \
        --data "D:/Documents/Visual Studio Code/GEX/data"
"""

import argparse
import asyncio
import datetime as dt
import io
import math
import sys
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow.parquet as pq

from gexlens_engine.compute.emrespect import StraddleQuote, straddle_em
from gexlens_engine.compute.gex import GexEngine, GexInput
from gexlens_engine.compute.gexfield import bs_price, fallback_greeks
from gexlens_engine.compute.settle import (
    is_trading_session,
    session_bounds,
    settle_ts,
    us_open_ts,
)
from gexlens_engine.scenario_auto import ApiReader, gather_context

PRAGUE = ZoneInfo("Europe/Prague")
ET = ZoneInfo("America/New_York")
YEAR_S = 365 * 86400
MINUTE = dt.timedelta(minutes=1)
#: Řetěz v minutě plánu = poslední kotace každého kontraktu za 10 min (dávky 100 lines)
LOOKBACK = dt.timedelta(minutes=10)
EXIT_BEFORE_SETTLE = dt.timedelta(minutes=15)
B_TRIGGER_EM = 0.05
P4_EXTRA_EM = 0.2
TARGET_DELTA = 0.25
MIN_RR = 2.0
#: Množina úrovní vstupu A (K3 A bod 1); těžiště a PDC v ní nejsou
LEVEL_SET = ("flip", "ONH", "ONL", "PDH", "PDL", "call wall", "put wall")
SCENARIO_LIMIT = 500  # strop `GET /scenarios`
ADR_DAY = dt.date(2026, 11, 2)
ADR_TIME = "16:40"
ADR_EXPECTED = {
    "vstup": 697.62,
    "SL": 699.79,
    "TP1": 692.13,
    "TP2": 690.72,
    "R:R": 3.40,
    "R:R přísně": 2.27,
    "P4": -72.0,
}


# ── formát ──────────────────────────────────────────────────────────


def cz(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}".replace(".", ",").replace("-", "−")


def usd(value: float) -> str:
    return f"{value:+.0f} $".replace("-", "−")


def hm(ts: dt.datetime) -> str:
    """Čas v Praze (SEČ / SELČ podle data), jak vlastník čte graf."""
    return ts.astimezone(PRAGUE).strftime("%H:%M")


def dm(day: dt.date) -> str:
    return f"{day.day}. {day.month}."


WEEKDAYS = ("pondělí", "úterý", "středa", "čtvrtek", "pátek", "sobota", "neděle")


# ── data ────────────────────────────────────────────────────────────


def read_rows(
    path: Path, start: dt.datetime | None = None, end: dt.datetime | None = None
) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    filters = [("ts_min", ">=", start)] if start else []
    if end:
        filters.append(("ts_min", "<=", end))
    rows: list[dict[str, Any]] = pq.read_table(path, filters=filters or None).to_pylist()
    return rows


def finite(value: Any) -> bool:
    return value is not None and math.isfinite(float(value))


def chain_at(path: Path, ts: dt.datetime) -> dict[tuple[float, str], dict[str, Any]]:
    """Poslední kotace každého kontraktu v okně [ts − LOOKBACK, ts]."""
    latest: dict[tuple[float, str], dict[str, Any]] = {}
    for row in sorted(read_rows(path, ts - LOOKBACK, ts), key=lambda r: r["ts_min"]):
        latest[(float(row["strike"]), str(row["right"]))] = row
    return latest


def chain_em(chain: dict[tuple[float, str], dict[str, Any]], spot: float) -> tuple[float, float]:
    """(ATM strike, EM) ze straddlu, nebo (nan, nan) bez zaplacených stran."""
    mids = {
        key: (row["bid"] + row["ask"]) / 2
        for key, row in chain.items()
        if finite(row["bid"]) and finite(row["ask"]) and row["bid"] > 0 and row["ask"] > 0
    }
    quotes = [
        StraddleQuote(strike=k, call_mid=mids.get((k, "C"), 0.0), put_mid=mids.get((k, "P"), 0.0))
        for k in sorted({k for k, _ in chain})
    ]
    result = straddle_em(quotes, spot)
    return result if result is not None else (math.nan, math.nan)


def atm_iv(chain: dict[tuple[float, str], dict[str, Any]], spot: float) -> float | None:
    """Průměrná IV obou stran striku nejblíž spotu (jako `tendency._atm_iv`, ta je vázaná na
    runtime). Rozdíl: nejbližší strike se hledá jen mezi kotacemi s IV."""
    ivs = {key: float(row["iv"]) for key, row in chain.items() if finite(row["iv"]) and row["iv"]}
    if not ivs:
        return None
    best = min(abs(strike - spot) for strike, _ in ivs)
    values = [iv for (strike, _), iv in ivs.items() if abs(strike - spot) <= best + 1e-9]
    return sum(values) / len(values)


def gex_top(chain: dict[tuple[float, str], dict[str, Any]], spot: float) -> tuple[float, float]:
    """Strike s největší kladnou NetGEX_1pct (naivní model dealera), nebo (nan, nan)."""
    options = [
        GexInput(strike=strike, right=right, gamma=float(row["gamma"]), oi=float(row["oi"]))
        for (strike, right), row in chain.items()
        if finite(row["gamma"]) and finite(row["oi"]) and row["gamma"] and row["oi"]
    ]
    rows = GexEngine().compute(options, spot, multiplier=100).per_strike
    best = max(rows, key=lambda row: row.net_gex_1pct, default=None)
    if best is None or best.net_gex_1pct <= 0:
        return math.nan, math.nan
    return best.strike, best.net_gex_1pct


@dataclass(frozen=True)
class Pair:
    """Minuta s 1m barem NQ i QQQ: close obou."""

    ts: dt.datetime
    nq: float
    qqq: float

    @property
    def ratio(self) -> float:
        return self.nq / self.qqq


def ratio_pairs(data: Path) -> list[Pair]:
    pairs: list[Pair] = []
    for path in sorted((data / "derived/QQQ/bars").glob("*.parquet")):
        qqq = {r["ts_min"]: r["close"] for r in read_rows(path) if finite(r["close"])}
        nq = {
            r["ts_min"]: r["close"]
            for r in read_rows(data / "derived/NQ/bars" / path.name)
            if finite(r["close"])
        }
        pairs += [Pair(ts, nq[ts], qqq[ts]) for ts in sorted(qqq.keys() & nq.keys())]
    return pairs


def ratio_for(pairs: list[Pair], day: dt.date, closed: dt.datetime) -> tuple[float, str] | None:
    """Poslední pár do `closed` včetně (bez pohledu dopředu): z téže seance „naměřený“,
    z dřívější „převzatý“; žádný dřívější pár = None (mezera)."""
    start, _ = session_bounds(day)
    before = [p for p in pairs if p.ts <= closed]
    if not before:
        return None
    pair = before[-1]
    local = pair.ts.astimezone(ET)
    if pair.ts >= start:
        return pair.ratio, f"{cz(pair.ratio, 3)} naměřený {local:%H:%M} ET"
    return pair.ratio, f"{cz(pair.ratio, 3)} převzatý {dm(local.date())} {local:%H:%M} ET"


def next_session(day: dt.date) -> dt.date:
    nxt = day + dt.timedelta(days=1)
    while not is_trading_session(nxt):
        nxt += dt.timedelta(days=1)
    return nxt


# ── plán a pravidla ─────────────────────────────────────────────────


@dataclass(frozen=True)
class Bar:
    """5min bar v cenách QQQ; `end` = konec baru (close v něm)."""

    end: dt.datetime
    high: float
    low: float
    close: float


def five_minute(
    bars: list[dict[str, Any]], ratio: float, start: dt.datetime, end: dt.datetime
) -> tuple[Bar, ...]:
    """5min bary po hodinách od `start` (US open) do `end` (settle)."""
    out: dict[dt.datetime, Bar] = {}
    for bar in sorted(bars, key=lambda r: r["ts_min"]):
        ts = bar["ts_min"]
        if not (start <= ts < end):
            continue
        key = ts - dt.timedelta(minutes=ts.minute % 5)
        high, low, close = bar["high"] / ratio, bar["low"] / ratio, bar["close"] / ratio
        prev = out.get(key)
        out[key] = (
            Bar(key + dt.timedelta(minutes=5), high, low, close)
            if prev is None
            else Bar(prev.end, max(prev.high, high), min(prev.low, low), close)
        )
    return tuple(out[key] for key in sorted(out))


@dataclass(frozen=True)
class Option:
    right: str  # C | P
    strike: float
    iv: float
    expiry: dt.datetime

    def price(self, spot: float, ts: dt.datetime) -> float:
        tau = (self.expiry - ts).total_seconds() / YEAR_S
        return bs_price(spot, self.strike, self.iv, tau, self.right)

    @property
    def label(self) -> str:
        side = "call" if self.right == "C" else "put"
        return f"{side} {self.strike:g} (IV {cz(self.iv * 100, 1)} %)"


def pick_option(
    right: str, spot: float, iv: float, expiry: dt.datetime, now: dt.datetime
) -> Option:
    """Strike QQQ po 1 $ s |delta| nejblíž 0,25 v modelu."""

    def distance(strike: float) -> float:
        greeks = fallback_greeks(
            spot=spot,
            strike=strike,
            right=right,
            mid=Option(right, strike, iv, expiry).price(spot, now),
            settle=expiry,
            now=now,
        )
        return abs(abs(greeks.delta) - TARGET_DELTA) if greeks else math.inf

    strike = min((float(round(spot) + step) for step in range(-40, 41)), key=distance)
    return Option(right, strike, iv, expiry)


@dataclass(frozen=True)
class Plan:
    """Vstupy plánu jednoho scénáře v cenách QQQ."""

    scenario: int
    day: dt.date
    side: int  # +1 long, −1 short
    entry: float
    targets: tuple[float, ...]
    t1: dt.datetime  # čas cíle 1 podle cesty scénáře
    t2: dt.datetime  # čas posledního bodu cesty (cíl 2, nebo jediný cíl v termínu)
    levels: dict[str, float]
    em: float
    option: Option
    fives: tuple[Bar, ...]
    exit_ts: dt.datetime
    p1: str  # blokuje | propustí | bez dat
    sources: dict[str, str]


@dataclass(frozen=True)
class Rules:
    name: str
    levels: tuple[str, ...] = LEVEL_SET
    test: str = "close"  # close | wick
    reserve_em: float = 0.05
    short_b: bool = True  # False = short jen vstupem A
    long_b: bool = True  # False = ani long bez vstupu B (P1 bez dat = dolní mez)
    after_p3: str = "end"  # end = spoušť vyřazená P3 plán ukončí | wait = čeká se na další
    skip_past_tp1: bool = False  # spoušť s cenou za cílem 1 = bez vstupu
    target: int = 0  # cíl pro R:R (P3): 0 = TP1, 1 = TP2 (když ho scénář má)


RULES = Rules("pravidla v1 (K3 A)")
ALTERNATIVES = (
    RULES,
    Rules("úroveň jen flip (K3 B)", levels=("flip",)),
    Rules("úroveň jen ONH/ONL (K3 C)", levels=("ONH", "ONL")),
    Rules("test knotem (K3 D)", test="wick"),
    Rules("rezerva SL 0", reserve_em=0.0),
    Rules("short jen vstupem A", short_b=False),
    # Podklad k rozhodnutí o slabých místech (#1756), nad rámec zadaných alternativ
    Rules("jen vstup A (B nikdy)", short_b=False, long_b=False),
    # Rozhodnuto 3. 11.: hlavní pravidla = jen vstup A (K6 B); stíny K10 B = v1 a tento řádek
    Rules("jen A + test knotem (stín K10 B)", test="wick", short_b=False, long_b=False),
    Rules("po vyřazení P3 čekat na další spoušť", after_p3="wait"),
    Rules("spoušť za TP1 = bez vstupu", skip_past_tp1=True),
    Rules("R:R do TP2 (když je)", target=1),
    Rules("jen A + R:R do TP2", short_b=False, long_b=False, target=1),
)
#: Výsledek spouště, kdy cena už stojí za cílem 1 (TP1 se „zasáhne“ hned, ale se ztrátou)
PAST_TP1 = "cíl 1 za vstupem"


@dataclass(frozen=True)
class Entry:
    kind: str  # A | B
    bar: Bar
    stop: float
    level: str
    level_price: float


@dataclass(frozen=True)
class Outcome:
    result: str  # SL | TP1 | TP2 | TP1→vstup | TP1→exit | exit
    pnl_tp1: float  # $ na kontrakt, celá pozice ven v TP1
    pnl_hold: float  # $ na kontrakt, po TP1 držet do TP2 se SL na vstupu
    detail: str


@dataclass(frozen=True)
class RiskReward:
    paid: float
    at_stop: float
    at_tp1: float
    rr: float  # SL i TP1 v čase cíle 1 (K3 bod 6)
    rr_strict: float  # SL v čase exitu
    p4: float  # $ na kontrakt: SL + 0,2 EM v exitu


@dataclass(frozen=True)
class Run:
    level: tuple[str, float] | None
    a: Entry | None
    b: Entry | None
    b_allowed: bool
    chosen: Entry | None
    risk: RiskReward | None
    outcome: Outcome | None

    @property
    def p3_ok(self) -> bool:
        return self.risk is not None and self.risk.rr >= MIN_RR


def against(plan: Plan, price: float, ref: float) -> bool:
    """Leží `price` proti směru od `ref` (short: nad, long: pod)?"""
    return (price - ref) * plan.side < 0


def nearest_against(
    plan: Plan, names: tuple[str, ...], limit_em: float | None
) -> tuple[str, float] | None:
    candidates = [
        (name, plan.levels[name])
        for name in names
        if name in plan.levels
        and against(plan, plan.levels[name], plan.entry)
        and (limit_em is None or abs(plan.levels[name] - plan.entry) <= limit_em * plan.em)
    ]
    return min(candidates, key=lambda c: abs(c[1] - plan.entry), default=None)


def window(plan: Plan) -> tuple[Bar, ...]:
    """Okno spouště: od prvního 5min close (9:35 ET) do exitu (K3 bod 5)."""
    return tuple(bar for bar in plan.fives if bar.end < plan.exit_ts)


def trigger_a(plan: Plan, name: str, level: float, rules: Rules) -> Entry | None:
    """Test úrovně (5min close za ní, nebo knot), pak první 5min close zpět; SL za extrémem."""
    side, reserve = plan.side, rules.reserve_em * plan.em
    tested, swing = False, 0.0
    for bar in window(plan):
        extreme = bar.high if side < 0 else bar.low
        if not tested:
            probe = extreme if rules.test == "wick" else bar.close
            if (probe - level) * side <= 0:
                tested, swing = True, extreme
                if rules.test == "wick" and (bar.close - level) * side > 0:
                    return Entry("A", bar, swing - side * reserve, name, level)
            continue
        swing = max(swing, extreme) if side < 0 else min(swing, extreme)
        if (bar.close - level) * side > 0:
            return Entry("A", bar, swing - side * reserve, name, level)
    return None


def trigger_b(plan: Plan, rules: Rules) -> Entry | None:
    """První 5min close za vstupem scénáře ∓ 0,05 EM; SL B za nejbližší úrovní proti směru."""
    sl_level = nearest_against(plan, rules.levels, None)
    if sl_level is None:
        return None
    threshold = plan.entry + plan.side * B_TRIGGER_EM * plan.em
    stop = sl_level[1] - plan.side * rules.reserve_em * plan.em
    for bar in window(plan):
        if (bar.close - threshold) * plan.side > 0:
            return Entry("B", bar, stop, sl_level[0], sl_level[1])
    return None


def risk_reward(plan: Plan, entry: Entry, target: int = 0) -> RiskReward:
    """P3 a P4: SL i cíl oceněné v čase cíle podle cesty (K3 bod 6), přísně SL v exitu."""
    opt, start = plan.option, entry.bar.end
    paid = opt.price(entry.bar.close, start)
    index = min(target, len(plan.targets) - 1)
    # Bod cesty může ležet za exitem (scénář s jedním cílem = termín settle); déle se nedrží
    horizon = min(max(start, plan.t1 if index == 0 else plan.t2), plan.exit_ts)
    at_stop, at_tp1 = opt.price(entry.stop, horizon), opt.price(plan.targets[index], horizon)
    strict = opt.price(entry.stop, plan.exit_ts)
    worst = opt.price(entry.stop - plan.side * P4_EXTRA_EM * plan.em, plan.exit_ts)

    def ratio(stop_price: float) -> float:
        return (at_tp1 - paid) / (paid - stop_price) if paid > stop_price else math.nan

    return RiskReward(paid, at_stop, at_tp1, ratio(at_stop), ratio(strict), (worst - paid) * 100)


def underlying_rr(plan: Plan, entry: Entry) -> float:
    """R:R podkladu při spoušti: od ceny vstupu k TP1 proti vzdálenosti k SL."""
    reward = (plan.targets[0] - entry.bar.close) * plan.side
    return reward / ((entry.bar.close - entry.stop) * plan.side)


def manage(plan: Plan, entry: Entry) -> Outcome:
    """Průběh po vstupu: TP na dotyk, SL na 5min close, po TP1 SL na vstup, exit."""
    side, opt = plan.side, plan.option
    paid = opt.price(entry.bar.close, entry.bar.end)

    def pnl(spot: float, ts: dt.datetime) -> float:
        return (opt.price(spot, ts) - paid) * 100

    tp1 = plan.targets[0]
    tp2 = plan.targets[1] if len(plan.targets) > 1 else None
    if (tp1 - entry.bar.close) * side <= 0:
        return Outcome(PAST_TP1, math.nan, math.nan, f"TP1 {cz(tp1)} už za cenou vstupu")
    stop, first, last = entry.stop, None, entry.bar
    for bar in plan.fives:
        if bar.end <= entry.bar.end or bar.end > plan.exit_ts:
            continue
        last = bar
        favorable = bar.low if side < 0 else bar.high
        if first is None and (favorable - tp1) * side >= 0:
            first = (bar.end, pnl(tp1, bar.end))
            if tp2 is None:
                return Outcome("TP1", first[1], first[1], f"TP1 {hm(bar.end)}")
            stop = entry.bar.close
        if first is not None and tp2 is not None and (favorable - tp2) * side >= 0:
            return Outcome(
                "TP2", first[1], pnl(tp2, bar.end), f"TP1 {hm(first[0])}, TP2 {hm(bar.end)}"
            )
        if (bar.close - stop) * side < 0:
            value = pnl(bar.close, bar.end)
            if first is None:
                return Outcome("SL", value, value, f"SL {hm(bar.end)} ({cz(bar.close)})")
            return Outcome(
                "TP1→vstup", first[1], value, f"TP1 {hm(first[0])}, stop na vstupu {hm(bar.end)}"
            )
    value = pnl(last.close, last.end)
    if first is None:
        return Outcome("exit", value, value, f"exit {hm(last.end)} ({cz(last.close)})")
    return Outcome("TP1→exit", first[1], value, f"TP1 {hm(first[0])}, exit {hm(last.end)}")


def run(plan: Plan, rules: Rules) -> Run:
    level = nearest_against(plan, rules.levels, 1.0)
    a = trigger_a(plan, level[0], level[1], rules) if level else None
    b = trigger_b(plan, rules)
    b_allowed = plan.p1 != "blokuje" and (rules.long_b if plan.side > 0 else rules.short_b)
    candidates = sorted(
        (e for e in (a, b if b_allowed else None) if e is not None),
        key=lambda e: (e.bar.end, e.kind),
    )
    if rules.skip_past_tp1:
        candidates = [e for e in candidates if (plan.targets[0] - e.bar.close) * plan.side > 0]
    if not candidates:
        return Run(level, a, b, b_allowed, None, None, None)
    chosen = candidates[0]
    if rules.after_p3 == "wait":
        passing = (e for e in candidates if risk_reward(plan, e, rules.target).rr >= MIN_RR)
        chosen = next(passing, chosen)
    risk = risk_reward(plan, chosen, rules.target)
    return Run(level, a, b, b_allowed, chosen, risk, manage(plan, chosen))


# ── vstupy plánu ze scénáře ─────────────────────────────────────────


async def build_plan(
    data: Path,
    api: ApiReader,
    pairs: list[Pair],
    scenario: dict[str, Any],
    *,
    closed: dt.datetime | None = None,
    levels_ts: dt.datetime | None = None,
    chain_ts: dt.datetime | None = None,
    option: Option | None = None,
) -> Plan | str:
    """Plán v minutě K4 A (9:31 ET); parametry přepíší minuty pro kontrolu ADR. Mezera = str."""
    day = dt.date.fromisoformat(scenario["day"])
    if scenario["symbol"] != "NQ":
        return f"scénář {scenario['symbol']}: plán QQQ jen ze scénáře NQ (ADR-0062 bod 2)"
    if not is_trading_session(day):
        return f"mimo obchodní seanci ({WEEKDAYS[day.weekday()]}), scénář neplatný (#1309)"
    direction = (scenario.get("rationale") or {}).get("verdict")
    if direction not in ("long", "short"):
        return f"verdikt {direction}: plán nevzniká"
    open_ts = us_open_ts(day)
    plan_ts = open_ts + MINUTE
    closed = closed or open_ts  # bar 9:30 ET se uzavře v 9:31 ET
    levels_ts, chain_ts = levels_ts or plan_ts, chain_ts or plan_ts
    start, end = session_bounds(day)
    nq = [
        r
        for partition in (day - dt.timedelta(days=1), day)
        for r in read_rows(data / f"derived/NQ/bars/{partition.isoformat()}.parquet", start, end)
    ]
    nq_close = {r["ts_min"]: r["close"] for r in nq}
    if closed not in nq_close:
        return f"bez 1m baru NQ {closed.astimezone(ET):%H:%M} ET"
    found = ratio_for(pairs, day, closed)
    if found is None:
        later = next((p for p in pairs if p.ts > closed), None)
        first = (
            f", první až {dm(later.ts.date())} {later.ts.astimezone(ET):%H:%M} ET" if later else ""
        )
        return f"bez poměru NQ/QQQ: žádný pár barů NQ a QQQ do 9:30 ET{first} (bez pohledu dopředu)"
    ratio, ratio_src = found
    spot_nq = nq_close[closed]
    spot = spot_nq / ratio
    exp0 = f"{day:%Y%m%d}"
    level_rows = read_rows(
        data / f"derived/NQ/{exp0}/levels/{day.isoformat()}.parquet",
        levels_ts - LOOKBACK,
        levels_ts,
    )
    if not level_rows:
        return f"bez snímku levels NQ 0DTE do {levels_ts.astimezone(ET):%H:%M} ET"
    snap = max(level_rows, key=lambda r: r["ts_min"])
    ctx = await gather_context(api, "NQ", None, day, plan_ts)
    bars_failed = [reason for reason in ctx.failures if reason.startswith("bars")]
    if bars_failed:
        return "čtení barů z API selhalo (ONH/ONL, PDH/PDL): " + ", ".join(bars_failed)
    raw = {
        "flip": snap["flip"],
        "call wall": snap["call_wall"],
        "put wall": snap["put_wall"],
        "těžiště": snap["centroid"],
        "ONH": ctx.on_high,
        "ONL": ctx.on_low,
        "PDH": ctx.prev_high,
        "PDL": ctx.prev_low,
    }
    levels = {name: value / ratio for name, value in raw.items() if finite(value)}
    qqq_chain = chain_at(data / f"snapshots/QQQ/{exp0}/{day.isoformat()}.parquet", chain_ts)
    em_source = "QQQ 0DTE"
    atm, em = chain_em(qqq_chain, spot) if qqq_chain else (math.nan, math.nan)
    if not math.isfinite(em):
        nq_chain = chain_at(data / f"snapshots/NQ/{exp0}/{day.isoformat()}.parquet", plan_ts)
        atm, em_nq = chain_em(nq_chain, spot_nq)
        em, em_source = em_nq / ratio, "NQ 0DTE / poměr"
    if not math.isfinite(em):
        return "bez straddlu QQQ ani NQ 0DTE (EM)"
    expiry_day = next_session(day)
    if option is None:
        iv_chain = chain_at(
            data / f"snapshots/NQ/{expiry_day:%Y%m%d}/{day.isoformat()}.parquet", plan_ts
        )
        iv = atm_iv(iv_chain, spot_nq)
        if iv is None:
            return f"bez IV NQ 1DTE ({dm(expiry_day)}) do 9:31 ET"
        right = "C" if direction == "long" else "P"
        option = pick_option(right, spot, iv, settle_ts(expiry_day), plan_ts)
        iv_source = f"ATM IV NQ 1DTE {dm(expiry_day)}"
    else:
        iv_source = "IV podle ADR"
    side = 1 if direction == "long" else -1
    p1 = "bez dat"
    p1_detail = "řetěz QQQ 0DTE s gamma a OI do 10 min před plánem není"
    has_gex = any(
        finite(row["gamma"]) and finite(row["oi"]) and row["gamma"] and row["oi"]
        for row in qqq_chain.values()
    )
    if has_gex:
        top, value = gex_top(qqq_chain, spot)
        distance = abs(top - spot) / em if math.isfinite(top) else math.nan
        blocks = math.isfinite(top) and (top - spot) * side < 0 and distance <= 1.0
        p1 = "blokuje" if blocks else "propustí"
        p1_detail = (
            f"největší +GEX QQQ {top:g} ({value / 1e6:+.0f} M), {cz(distance)} EM od spotu"
            if math.isfinite(top)
            else "žádná kladná GEX QQQ"
        )
    path = scenario["path"]
    return Plan(
        scenario=int(scenario["id"]),
        day=day,
        side=side,
        entry=float(scenario["entry"]) / ratio,
        targets=tuple(float(t) / ratio for t in scenario["targets"]),
        t1=dt.datetime.fromisoformat(path[1]["ts"]),
        t2=dt.datetime.fromisoformat(path[-1]["ts"]),
        levels=levels,
        em=em,
        option=option,
        fives=five_minute(nq, ratio, open_ts, settle_ts(day)),
        exit_ts=settle_ts(day) - EXIT_BEFORE_SETTLE,
        p1=p1,
        sources={
            "poměr": ratio_src,
            "EM": f"{em_source} straddle {atm:g}",
            "IV": iv_source,
            "P1": p1_detail,
            "spot": cz(spot),
            "chybí": ", ".join(ctx.missing) or "—",
        },
    )


# ── výstup ──────────────────────────────────────────────────────────


def entry_text(entry: Entry | None) -> str:
    if entry is None:
        return "—"
    return f"{hm(entry.bar.end)} za {cz(entry.bar.close)}, SL {cz(entry.stop)}"


def adr_check(plan: Plan) -> tuple[list[str], bool]:
    """Měření K3 ADR-0062 týmiž funkcemi a výsledek AC #1756 (True = shoda)."""
    out = [
        f"- vstupy: poměr {plan.sources['poměr']}, vstup {cz(plan.entry)}, EM {cz(plan.em)} "
        f"({plan.sources['EM']}), opce {plan.option.label} (model), P1 {plan.p1} "
        f"({plan.sources['P1']})",
        "- úrovně QQQ: "
        + ", ".join(f"{name} {cz(price)}" for name, price in sorted(plan.levels.items())),
    ]
    for name, level in (
        ("flip", plan.levels["flip"]),
        ("ONH", plan.levels["ONH"]),
        ("těžiště", plan.levels["těžiště"]),
        ("697,0 (ruční plán)", 697.0),
    ):
        entry = trigger_a(plan, name, level, RULES)
        if entry is None:
            out.append(f"- {name} {cz(level)}: vstup A nespuštěn")
            continue
        risk, outcome = risk_reward(plan, entry), manage(plan, entry)
        out.append(
            f"- {name} {cz(level)}: vstup A {entry_text(entry)}, R:R podkladu "
            f"{cz(underlying_rr(plan, entry))}, opce {cz(risk.paid)} / SL {cz(risk.at_stop)} "
            f"({usd((risk.at_stop - risk.paid) * 100)}) / TP1 {cz(risk.at_tp1)} "
            f"({usd((risk.at_tp1 - risk.paid) * 100)}), R:R opce {cz(risk.rr)} (přísně "
            f"{cz(risk.rr_strict)}), P4 {usd(risk.p4)}; {outcome.detail}"
        )
    flip = plan.levels["flip"]
    for label, reserve in (("0", 0.0), ("0,05 EM", 0.05), ("0,1 EM", 0.1)):
        entry = trigger_a(plan, "flip", flip, replace(RULES, reserve_em=reserve))
        if entry is not None:
            risk = risk_reward(plan, entry)
            out.append(
                f"- rezerva {label}: SL {cz(entry.stop)}, R:R opce {cz(risk.rr)} "
                f"(přísně {cz(risk.rr_strict)})"
            )
    b = trigger_b(plan, RULES)
    if b is not None:
        out.append(f"- vstup B {entry_text(b)} (za {b.level}); {manage(plan, b).detail}")
    main = run(plan, RULES)
    if main.chosen is None or main.risk is None:
        out.append("- **AC #1756: NESHODA** — vstup A nespuštěn")
        return out, False
    time = hm(main.chosen.bar.end)
    got = {
        "vstup": round(main.chosen.bar.close, 2),
        "SL": round(main.chosen.stop, 2),
        "TP1": round(plan.targets[0], 2),
        "TP2": round(plan.targets[1], 2),
        "R:R": round(main.risk.rr, 2),
        "R:R přísně": round(main.risk.rr_strict, 2),
        "P4": float(round(main.risk.p4)),
    }
    bad = [f"{key} {got[key]} ≠ {want}" for key, want in ADR_EXPECTED.items() if got[key] != want]
    if time != ADR_TIME:
        bad.insert(0, f"spoušť {time} ≠ {ADR_TIME}")
    verdict = "shoda" if not bad else "NESHODA: " + "; ".join(bad)
    out.append(
        f"- **AC #1756 ({main.chosen.kind} za {main.chosen.level}): {verdict}** — vstup "
        f"{time} za {cz(got['vstup'])}, SL {cz(got['SL'])}, R:R {cz(got['R:R'])} "
        f"(přísně {cz(got['R:R přísně'])}), P4 {usd(got['P4'])}, TP1 {cz(got['TP1'])} / "
        f"TP2 {cz(got['TP2'])}"
    )
    return out, not bad


def plan_rows(plans: list[tuple[dict[str, Any], Plan]]) -> list[str]:
    head = [
        "| # | den | směr | poměr NQ/QQQ | EM QQQ (zdroj) | opce 1DTE (model) | úroveň A "
        "(proč) | P1 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    out = head
    for scenario, plan in plans:
        result = run(plan, RULES)
        if result.level is None:
            near = nearest_against(plan, LEVEL_SET, None)
            why = (
                f"žádná do 1 EM (nejbližší {near[0]} {cz(near[1])} = "
                f"{cz(abs(near[1] - plan.entry) / plan.em)} EM)"
                if near
                else "žádná úroveň proti směru"
            )
        else:
            name, price = result.level
            why = (
                f"{name} {cz(price)} — nejbližší proti směru, "
                f"{cz(abs(price - plan.entry) / plan.em)} EM od vstupu {cz(plan.entry)}"
            )
        out.append(
            f"| {plan.scenario} | {dm(plan.day)} | {scenario['rationale']['verdict']} | "
            f"{plan.sources['poměr']} | {cz(plan.em)} ({plan.sources['EM']}) | "
            f"{plan.option.label} | {why} | {plan.p1} |"
        )
    return out


def result_rows(plans: list[tuple[dict[str, Any], Plan]]) -> list[str]:
    out = [
        "| # | den | spoušť A | spoušť B | vstup plánu | TP1 / TP2 | R:R opce (přísně) | P4 | "
        "výsledek | P&L TP1 / TP2 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for _, plan in plans:
        result = run(plan, RULES)
        b = entry_text(result.b) + ("" if result.b_allowed or result.b is None else " (P1 ✗)")
        targets = " / ".join(cz(t) for t in plan.targets)
        if result.chosen is None or result.risk is None or result.outcome is None:
            out.append(
                f"| {plan.scenario} | {dm(plan.day)} | {entry_text(result.a)} | {b} | bez vstupu "
                f"| {targets} | — | — | bez vstupu | — |"
            )
            continue
        risk, outcome = result.risk, result.outcome
        verdict = outcome.result if result.p3_ok else f"vyřazeno P3 (hypot. {outcome.result})"
        pnl = (
            f"{usd(outcome.pnl_tp1)} / {usd(outcome.pnl_hold)}"
            if math.isfinite(outcome.pnl_tp1)
            else "—"
        )
        out.append(
            f"| {plan.scenario} | {dm(plan.day)} | {entry_text(result.a)} | {b} | "
            f"{result.chosen.kind} {hm(result.chosen.bar.end)} | {targets} | {cz(risk.rr)} "
            f"({cz(risk.rr_strict)}) | {usd(risk.p4)} | {verdict}: {outcome.detail} | {pnl} |"
        )
    return out


def summary_row(rules: Rules, plans: list[Plan]) -> str:
    runs = [run(plan, rules) for plan in plans]
    kinds = Counter(r.chosen.kind if r.chosen else "—" for r in runs)
    traded = [r for r in runs if r.chosen is not None and r.p3_ok]
    excluded = [r for r in runs if r.chosen is not None and not r.p3_ok]
    results = Counter(r.outcome.result for r in traded if r.outcome)
    tp1 = sum(r.outcome.pnl_tp1 for r in traded if r.outcome)
    hold = sum(r.outcome.pnl_hold for r in traded if r.outcome)
    past = sum(1 for r in excluded if r.outcome and r.outcome.result == PAST_TP1)
    hypo = [r.outcome for r in excluded if r.outcome and r.outcome.result != PAST_TP1]
    hypo_text = (
        f"{usd(sum(o.pnl_tp1 for o in hypo))} ({sum(o.pnl_tp1 > 0 for o in hypo)} zisk / "
        f"{sum(o.pnl_tp1 <= 0 for o in hypo)} ztráta)"
    )
    order = ("SL", "TP1", "TP1→vstup", "TP1→exit", "TP2", "exit")
    return (
        f"| {rules.name} | {kinds['A']} | {kinds['B']} | {kinds['—']} | {len(excluded)} "
        f"(z toho {past} za TP1) | {len(traded)}: "
        + (", ".join(f"{key} {results[key]}" for key in order if results[key]) or "—")
        + f" | {usd(tp1)} / {usd(hold)} | {hypo_text} |"
    )


def diagnostic_rows(plans: list[tuple[dict[str, Any], Plan]]) -> list[str]:
    """Proč R:R < 2: vzdálenost TP1, R:R podkladu při spoušti a pohyb po ní (pravidla v1)."""
    out = [
        "| # | den | TP1 od vstupu scénáře | riziko do SL | R:R podkladu | pohyb ve směru do "
        "exitu | scénář NQ |",
        "|---|---|---|---|---|---|---|",
    ]
    for scenario, plan in plans:
        result = run(plan, RULES)
        tp1 = abs(plan.targets[0] - plan.entry) / plan.em
        verdict = (scenario.get("result") or {}).get("verdict", "—")
        entry = result.chosen
        if entry is None:
            out.append(
                f"| {plan.scenario} | {dm(plan.day)} | {cz(tp1)} EM | — | — | — | {verdict} |"
            )
            continue
        risk = (entry.stop - entry.bar.close) * -plan.side / plan.em
        after = [b for b in plan.fives if entry.bar.end < b.end <= plan.exit_ts]
        best = max(((b.high if plan.side > 0 else -b.low) for b in after), default=math.nan)
        move = (best - entry.bar.close * plan.side) / plan.em
        out.append(
            f"| {plan.scenario} | {dm(plan.day)} | {cz(tp1)} EM | {cz(risk)} EM | "
            f"{cz(underlying_rr(plan, entry))} | {cz(move)} EM | {verdict} |"
        )
    return out


async def main() -> int:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--api", default="http://localhost:8010")
    args = parser.parse_args()
    api = ApiReader(args.api, timeout_s=60.0)
    try:
        scenarios = (await api.get("/scenarios", {"limit": SCENARIO_LIMIT, "source": "auto"}))[
            "scenarios"
        ]
        if len(scenarios) >= SCENARIO_LIMIT:
            print(f"GET /scenarios vrátil {len(scenarios)} = limit, seznam může být neúplný")
            return 1
        pairs = ratio_pairs(args.data)
        adr_scenario = next(s for s in scenarios if s["day"] == ADR_DAY.isoformat())
        adr_plan = await build_plan(
            args.data,
            api,
            pairs,
            adr_scenario,
            closed=dt.datetime(2026, 11, 2, 14, 14, tzinfo=dt.UTC),
            levels_ts=dt.datetime(2026, 11, 2, 14, 15, tzinfo=dt.UTC),
            chain_ts=dt.datetime(2026, 11, 2, 14, 23, tzinfo=dt.UTC),
            option=Option("P", 687.0, 0.30, settle_ts(dt.date(2026, 11, 3))),
        )
        if isinstance(adr_plan, str):
            print(f"Kontrola ADR: mezera — {adr_plan}")
            return 1
        print("## Kontrola ADR-0062 (2. 11., vstupy měření K3, časy SEČ)\n")
        lines, adr_ok = adr_check(adr_plan)
        print("\n".join(lines))
        plans: list[tuple[dict[str, Any], Plan]] = []
        gaps: list[str] = []
        for scenario in sorted(scenarios, key=lambda s: (s["day"], s["id"])):
            plan = await build_plan(args.data, api, pairs, scenario)
            if isinstance(plan, str):
                gaps.append(f"- #{scenario['id']} {scenario['day']} {scenario['symbol']}: {plan}")
            else:
                plans.append((scenario, plan))
    finally:
        await api.close()
    days = Counter(s["day"] for s in scenarios)
    symbols = Counter((s["symbol"], s["rationale"]["verdict"]) for s in scenarios)
    print(
        f"\n## Zpětné přehrání (pravidla v1: K3 A, okno do exitu, K4 A 9:31 ET; čas Praha)\n\n"
        f"Scénářů {len(scenarios)} ve {len(days)} dnech: "
        + ", ".join(f"{sym} {verdict} {n}" for (sym, verdict), n in sorted(symbols.items()))
        + f". Přehráno {len(plans)}, mezer {len(gaps)}.\n"
    )
    print("\n".join(plan_rows(plans)))
    print()
    print("\n".join(result_rows(plans)))
    print("\n### Mezery\n")
    print("\n".join(gaps))
    print("\n### Dny se dvěma scénáři\n")
    for day in sorted(d for d, n in days.items() if n > 1):
        same = [s for s in scenarios if s["day"] == day]
        print(
            f"- {day}: "
            + ", ".join(f"#{s['id']} {s['symbol']} {s['rationale']['verdict']}" for s in same)
        )
    only = [plan for _, plan in plans]
    print("\n### Souhrn a alternativy (P&L = model, $ na 1 kontrakt)\n")
    print(
        "| varianta | vstup A | vstup B | bez vstupu | vyřazeno P3 (R:R < 2) | obchody "
        "(výsledky) | P&L obchodů TP1 / TP2 Σ | vyřazené P3 hypoteticky (TP1) |"
    )
    print("|---|---|---|---|---|---|---|---|")
    for rules in ALTERNATIVES:
        print(summary_row(rules, only))
    chosen = [run(plan, RULES).level for plan in only]
    levels = Counter(level[0] if level else "žádná do 1 EM" for level in chosen)
    print("\nÚroveň A (pravidla v1): " + ", ".join(f"{k} {v}" for k, v in levels.most_common()))
    p1 = Counter(plan.p1 for plan in only)
    print("P1: " + ", ".join(f"{k} {v}" for k, v in p1.most_common()))
    print("\n### Proč R:R < 2 (pravidla v1, v EM QQQ)\n")
    print("\n".join(diagnostic_rows(plans)))
    return 0 if adr_ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
