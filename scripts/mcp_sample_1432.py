"""Ukázka výstupu MCP nad REST pro ES a NQ (E-3.1, #1432) — prototyp tvaru nástrojů.

Skládá odpovědi čtyř navržených nástrojů (#1407, #1246) z dnešního REST API:
`levels_now`, `session_context`, `vol_context` a `setup_library`. Žádný MCP
server, žádná registrace v klientu, žádné zápisy — jen `GET` na API.

Obálka každého čísla (pravidla proti zkreslení, #1407; ADR-0049 až E-3.4):
`value`, `unit`, `as_of`, `age_s`, `market_open`, `source`, `scope`.
- Chybějící hodnota je `value: null` s `reason` — nikdy dosazená.
- Minutová řada při otevřeném trhu a stáří nad `MINUTE_LIMIT_S` → `value: null`
  a `error: {"code": "stale", ...}`; při zavřeném trhu poslední hodnota s datem
  bez chyby (`marketclock.is_market_closed`).
- Denní řada (vol režim, IV rank, EM) je čerstvá, když nese poslední seanci,
  jejíž výpočet už měl proběhnout (`settle_ts` + `DAILY_LAG`); obchodní den
  je `settle.trading_session_date`, minulá seance přes `settle.is_trading_session`.
- Nekalibrované metriky nesou `calibrated: false` (tendence, #1113).
- Pole, která REST nevrací, jsou v `omitted` s ID úkolu, který je doplní.
- `distance_pt` / `distance_em` počítá `level_distance` — čistá funkce, kterou
  převezme MCP server (E-3.7); ve výstupu označená `prototype`.

Spuštění z hostitele (produkční API, jen čtení):
    uv run python scripts/mcp_sample_1432.py --api http://127.0.0.1:8010 > ukazka.json
Na Windows s `PYTHONUTF8=1` (výstup obsahuje české znaky).
"""

import argparse
import datetime as dt
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from gexlens_engine.compute.marketclock import is_market_closed
from gexlens_engine.compute.settle import (
    is_trading_session,
    session_bounds,
    settle_ts,
    trading_session_date,
)

#: Minutová řada (levels, bary, tendence) smí být při otevřeném trhu stará
#: nejvýš tři cykly enginu — starší hodnota je výpadek, ne „poslední známá"
MINUTE_LIMIT_S = 180
#: Denní řady počítá engine po settle (vol režim 20:05, IV rank 20:10 UTC);
#: rezerva, než se chybějící řádek seance hlásí jako zastaralý
DAILY_LAG = dt.timedelta(minutes=30)
#: Zavřený trh (víkend, svátek, neděle před otevřením): o kolik dní zpět hledat
#: poslední seanci s bary — dlouhý víkend se svátkem jsou 4 dny
MAX_BACK_DAYS = 7
#: Co je `gate_lb` v Knihovně setupů (`risk.expectancy_lower_bound`)
LOWER_BOUND_DEF = "dolní mez jednostranného 95% intervalu Ø R v okně brány"
#: Štítek hodnot, které spočítal tento skript a převezme server (E-3.7)
PROTOTYPE = "E-3.7 (výpočet převezme MCP server)"

#: Pole nástrojů, která dnešní REST nevrací — ID úkolu, který je doplní
OMITTED: Mapping[str, Sequence[tuple[str, str]]] = {
    "levels_now": (
        ("max_pain", "E-3.12b #1443"),
        ("measured", "E-5.13 (statistiky reakce na úroveň)"),
        ("all_expiries", "E-4.7 (all-exp přes basis, ADR-0051)"),
        ("dealer_sign_fa", "E-3.8 #1438 (varianta znaménka FA se stavem α)"),
    ),
    "session_context": (
        ("onh_onl", "E-3.14 #1445"),
        ("pdh_pdl_rth", "E-3.14 #1445"),
        ("vwap", "E-3.14 #1445"),
        ("settle_prev", "E-3.14 #1445 (settlement, ne poslední cena)"),
    ),
    "vol_context": (),
    # Brzdy účtu (R dne a týdne) jsou výsledky účtu — opt-in jako deník (ot. 10)
    "setup_library": (("brakes", "E-3.17 #1446 (opt-in údajů účtu, ot. 10)"),),
}

Fetch = Callable[[str], Any]


class ApiError(Exception):
    """GET na API neskončil 200 — důvod jde do `reason` hodnoty, ne do prázdna."""

    def __init__(self, path: str, status: int | str) -> None:
        super().__init__(f"GET {path}: {status}")
        self.path = path
        self.status = status


@dataclass(frozen=True)
class Clock:
    """Okamžik dotazu: stav trhu a obchodní den — jeden pro celý běh."""

    now: dt.datetime
    market_open: bool
    trading_session: dt.date


def make_clock(now: dt.datetime) -> Clock:
    return Clock(
        now=now,
        market_open=not is_market_closed(now),
        trading_session=trading_session_date(now),
    )


def expected_daily_session(clock: Clock) -> dt.date:
    """Poslední seance, jejíž denní řádek už měl vzniknout (settle + `DAILY_LAG`)."""
    day = clock.trading_session
    while not (is_trading_session(day) and settle_ts(day) + DAILY_LAG <= clock.now):
        day -= dt.timedelta(days=1)
    return day


def data_session(fetch: Fetch, symbol: str, clock: Clock) -> dt.date:
    """Seance, jejíž data nástroje čtou.

    Otevřený trh = dnešní obchodní den. Zavřený trh = poslední seance s bary
    (sobota → pátek, svátek → poslední den s Globexem); chyba API hledání
    ukončí — nástroje pak chybu ohlásí samy, nehledá se naslepo dál.
    """
    if clock.market_open:
        return clock.trading_session
    for back in range(MAX_BACK_DAYS + 1):
        day = clock.trading_session - dt.timedelta(days=back)
        payload, error = _get(fetch, f"/bars/{symbol}?date={day.isoformat()}")
        if error is not None:
            return clock.trading_session
        if payload["bars"]:
            return day
    return clock.trading_session


def level_distance(
    level: float | None, spot: float | None, em_points: float | None
) -> tuple[float | None, float | None]:
    """Vzdálenost úrovně od ceny: body (kladná = úroveň nad cenou) a násobky EM.

    Čistá funkce pro MCP server (E-3.7): Claude vzdálenost nepočítá sám.
    Chybí-li úroveň, cena nebo EM, je příslušná vzdálenost None.
    """
    if level is None or spot is None:
        return None, None
    points = round(level - spot, 2)
    if em_points is None or em_points <= 0:
        return points, None
    return points, round(points / em_points, 3)


def _parse_ts(value: object) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)


def measured(
    value: object,
    *,
    clock: Clock,
    unit: str,
    as_of: dt.datetime,
    source: str,
    scope: str,
    limit_s: int | None = None,
    **extra: object,
) -> dict[str, object]:
    """Jedno naměřené číslo v obálce; `limit_s` = minutová řada se stářím."""
    age_s = int((clock.now - as_of).total_seconds())
    out: dict[str, object] = {
        "value": value,
        "unit": unit,
        "as_of": as_of.isoformat(),
        "age_s": age_s,
        "market_open": clock.market_open,
        "source": source,
        "scope": scope,
        **extra,
    }
    if value is None and "reason" not in extra:
        raise ValueError(f"{source}: hodnota None bez důvodu")
    if limit_s is not None and clock.market_open and age_s > limit_s:
        out["value"] = None
        out["error"] = {
            "code": "stale",
            "detail": f"stáří {age_s} s > limit {limit_s} s při otevřeném trhu",
        }
    return out


def missing(reason: str, *, clock: Clock, unit: str, source: str, scope: str) -> dict[str, object]:
    """Hodnota, kterou REST nedodal: `null` s důvodem, bez času."""
    return {
        "value": None,
        "reason": reason,
        "unit": unit,
        "as_of": None,
        "age_s": None,
        "market_open": clock.market_open,
        "source": source,
        "scope": scope,
    }


def _get(fetch: Fetch, path: str) -> tuple[Any, str | None]:
    try:
        return fetch(path), None
    except ApiError as error:
        return None, str(error)


def _omitted(tool: str) -> list[dict[str, str]]:
    return [{"field": name, "task": task} for name, task in OMITTED[tool]]


def _tool(
    name: str,
    symbol: str | None,
    clock: Clock,
    values: dict[str, object],
    session: dt.date | None = None,
) -> dict[str, object]:
    errors = sorted(
        key for key, item in values.items() if isinstance(item, dict) and "error" in item
    )
    return {
        "tool": name,
        **({"symbol": symbol} if symbol else {}),
        "trading_session": clock.trading_session.isoformat(),
        **({"data_session": session.isoformat()} if session else {}),
        "market_open": clock.market_open,
        "generated_at": clock.now.isoformat(),
        "values": values,
        "omitted": _omitted(name),
        "errors": errors,
    }


def front_expiry(fetch: Fetch, symbol: str, session: dt.date) -> tuple[str | None, str]:
    """Expirace seance `session` (0DTE), jinak nejbližší další; scope s trading class."""
    payload, error = _get(fetch, f"/instruments/{symbol}/expiries")
    if payload is None:
        return None, f"{symbol}: {error}"
    session_key = session.strftime("%Y%m%d")
    ahead = [expiry for expiry in payload["expiries"] if expiry >= session_key]
    if not ahead:
        return None, f"{symbol}: žádná expirace od {session_key}"
    expiry = ahead[0]
    classes = {row["date"]: row["trading_classes"] for row in payload.get("detail", [])}
    label = ",".join(classes.get(expiry, [])) or "třída neznámá"
    kind = "0DTE" if expiry == session_key else "nejbližší expirace"
    return expiry, f"{symbol} {expiry} ({label}, {kind}) — jedna expirace, ne all-exp"


def last_spot(fetch: Fetch, symbol: str, clock: Clock, session: dt.date) -> dict[str, object]:
    """Poslední cena podkladu = close poslední 1min baru seance."""
    path = f"/bars/{symbol}?date={session.isoformat()}"
    payload, error = _get(fetch, path)
    scope = f"{symbol} futures, Globex seance {session.isoformat()}"
    if payload is None:
        return missing(str(error), clock=clock, unit="bod", source=path, scope=scope)
    bars = payload["bars"]
    if not bars:
        return missing("seance nemá žádný bar", clock=clock, unit="bod", source=path, scope=scope)
    bar = bars[-1]
    return measured(
        bar["close"],
        clock=clock,
        unit="bod (cena podkladu)",
        as_of=_parse_ts(bar["ts_min"]),
        source=f"GET {path} → close posledního baru (feed {bar.get('source')})",
        scope=scope,
        limit_s=MINUTE_LIMIT_S,
    )


def levels_now(fetch: Fetch, symbol: str, clock: Clock, session: dt.date) -> dict[str, object]:
    expiry, scope = front_expiry(fetch, symbol, session)
    spot = last_spot(fetch, symbol, clock, session)
    values: dict[str, object] = {"spot": spot}
    spot_value = spot["value"] if isinstance(spot["value"], float | int) else None
    em_reason = (
        "EM dnešní seance REST nevrací: /emrespect má řádek seance až po close "
        "(computed_at ~20:05 UTC), UI ho počítá v prohlížeči (App.tsx dayEm) → E-3.5"
    )
    if expiry is None:
        values["levels"] = missing(
            scope, clock=clock, unit="bod", source="/instruments", scope=symbol
        )
        return _tool("levels_now", symbol, clock, values, session)
    path = f"/levels/{symbol}/{expiry}?date={session.isoformat()}"
    payload, error = _get(fetch, path)
    rows = payload["levels"] if payload else []
    if not rows:
        reason = error or "REST vrátil prázdnou řadu levels pro seanci"
        values["levels"] = missing(reason, clock=clock, unit="bod", source=path, scope=scope)
        return _tool("levels_now", symbol, clock, values, session)
    row = rows[-1]
    as_of = _parse_ts(row["ts_min"])
    source = f"GET {path} → poslední minuta"
    for name in ("call_wall", "put_wall", "flip", "centroid"):
        level = row.get(name)
        if level is None:
            values[name] = missing(
                f"{name} v profilu expirace neexistuje (GexLevels: None = úroveň není); "
                "REST důvod nevrací → E-3.5",
                clock=clock,
                unit="bod",
                source=source,
                scope=scope,
            )
            continue
        points, in_em = level_distance(level, spot_value, None)
        values[name] = measured(
            level,
            clock=clock,
            unit="bod (strike)",
            as_of=as_of,
            source=source,
            scope=scope,
            limit_s=MINUTE_LIMIT_S,
            distance_pt=points,
            distance_em=in_em,
            distance_em_reason=em_reason,
            prototype=PROTOTYPE,
        )
    for name in ("call_wall_dom", "put_wall_dom"):
        dominance = row.get(name)
        if dominance is None:
            values[name] = missing(
                "zeď strany neexistuje nebo řada walldom chybí (REST rozdíl neukáže)",
                clock=clock,
                unit="podíl 0–1",
                source=source,
                scope=scope,
            )
            continue
        values[name] = measured(
            round(dominance, 4),
            clock=clock,
            unit="podíl 0–1 kladné síly strany (ADR-0010)",
            as_of=as_of,
            source=source,
            scope=scope,
            limit_s=MINUTE_LIMIT_S,
        )
    values["total_gex"] = measured(
        round(row["total_gex"], 1),
        clock=clock,
        unit="USD/bod — Σ NetGEX_1pt (Γ·OI·M) přes striky expirace, znaménko naivního modelu",
        as_of=as_of,
        source=source,
        scope=scope,
        limit_s=MINUTE_LIMIT_S,
        note="řád se mezi ES a NQ liší — srovnávat jen v rámci symbolu",
    )
    values["tendency"] = tendency_now(fetch, symbol, clock, session)
    return _tool("levels_now", symbol, clock, values, session)


def tendency_now(fetch: Fetch, symbol: str, clock: Clock, session: dt.date) -> dict[str, object]:
    """Poslední bod tendence (#350) — nekalibrovaná metrika (#1113)."""
    path = f"/tendency/{symbol}?date={session.isoformat()}"
    scope = f"{symbol}, seance {session.isoformat()}"
    payload, error = _get(fetch, path)
    rows = payload["tendency"] if payload else []
    if not rows:
        reason = error or (
            "REST vrátil prázdnou řadu — /tendency vrací [] i při chybě DB "
            "(main.py tendency_series), rozdíl nejde poznat → E-3.5"
        )
        return missing(reason, clock=clock, unit="skóre −1…+1", source=path, scope=scope)
    row = rows[-1]
    return measured(
        round(row["score"], 3),
        clock=clock,
        unit="skóre −1…+1 (vážený hlas indikátorů)",
        as_of=_parse_ts(row["ts_min"]),
        source=f"GET {path} → poslední minuta",
        scope=scope,
        limit_s=MINUTE_LIMIT_S,
        band=row.get("band"),
        weights_version=row.get("weights_version"),
        calibrated=False,
    )


def _session_of_candle(candle: Mapping[str, Any]) -> dt.date:
    """Denní svíčka nese UTC začátek Globex seance (22:00 UTC) — den seance z `settle`."""
    return trading_session_date(_parse_ts(candle["ts"]))


def session_context(fetch: Fetch, symbol: str, clock: Clock, session: dt.date) -> dict[str, object]:
    spot = last_spot(fetch, symbol, clock, session)
    values: dict[str, object] = {"spot": spot}
    spot_value = spot["value"] if isinstance(spot["value"], float | int) else None
    spot_as_of = _parse_ts(spot["as_of"]) if spot["as_of"] else None
    path = f"/candles/{symbol}?tf=D&limit=2"
    payload, error = _get(fetch, path)
    candles = payload["candles"] if payload else []
    today = [c for c in candles if _session_of_candle(c) == session]
    before = [c for c in candles if _session_of_candle(c) < session]
    scope_today = f"{symbol} Globex seance {session.isoformat()} (od 17:00 CT)"
    if today and spot_as_of is not None:
        candle = today[-1]
        for name in ("open", "high", "low"):
            points, _ = level_distance(candle[name], spot_value, None)
            values[f"session_{name}"] = measured(
                candle[name],
                clock=clock,
                unit="bod",
                as_of=spot_as_of,
                source=f"GET {path} → rozdělaná svíčka; as_of z posledního baru "
                "(svíčka čas poslední aktualizace nenese)",
                scope=scope_today,
                limit_s=MINUTE_LIMIT_S,
                distance_pt=points,
                prototype=PROTOTYPE,
            )
    else:
        values["session"] = missing(
            error or "svíčka dnešní seance chybí",
            clock=clock,
            unit="bod",
            source=path,
            scope=scope_today,
        )
    if before:
        candle = before[-1]
        day = _session_of_candle(candle)
        # Dokončená svíčka se nemění: as_of = konec seance, bez limitu stáří
        as_of = session_bounds(day)[1]
        scope_prev = f"{symbol} předchozí Globex seance {day.isoformat()} (17:00–16:00 CT)"
        for name in ("high", "low", "close"):
            points, _ = level_distance(candle[name], spot_value, None)
            values[f"prev_session_{name}"] = measured(
                candle[name],
                clock=clock,
                unit="bod",
                as_of=as_of,
                source=f"GET {path} → dokončená svíčka",
                scope=scope_prev,
                distance_pt=points,
                prototype=PROTOTYPE,
                note=(
                    "poslední cena seance, ne settlement"
                    if name == "close"
                    else "Globex seance včetně noci, ne RTH PDH/PDL"
                ),
            )
    else:
        values["prev_session"] = missing(
            error or "svíčka předchozí seance chybí",
            clock=clock,
            unit="bod",
            source=path,
            scope=f"{symbol} předchozí seance",
        )
    return _tool("session_context", symbol, clock, values, session)


def _daily(
    row: Mapping[str, Any],
    value: object,
    *,
    clock: Clock,
    unit: str,
    source: str,
    scope: str,
    **extra: object,
) -> dict[str, object]:
    """Číslo denní řady: čerstvé, když nese seanci `expected_daily_session`."""
    session = dt.date.fromisoformat(str(row["session_date"]))
    expected = expected_daily_session(clock)
    out = measured(
        value,
        clock=clock,
        unit=unit,
        as_of=_parse_ts(row["computed_at"]),
        source=source,
        scope=f"{scope}, seance {session.isoformat()}",
    )
    out.update(extra)
    if clock.market_open and session < expected:
        out["value"] = None
        out["error"] = {
            "code": "stale",
            "detail": f"řádek seance {session.isoformat()}, čeká se {expected.isoformat()}",
        }
    return out


_EMPTY_LIST = (
    "REST vrátil prázdný seznam — endpoint vrací [] i při chybě DB, rozdíl nejde poznat → E-3.5"
)


def vol_context(fetch: Fetch, symbol: str, clock: Clock) -> dict[str, object]:
    values: dict[str, object] = {}
    path = f"/volregime/{symbol}?limit=1"
    payload, error = _get(fetch, path)
    rows = payload["rows"] if payload else []
    if rows:
        row = rows[0]
        values["vol_regime"] = _daily(
            row,
            row["bucket"],
            clock=clock,
            unit="koš low/normal/high (percentil denního rozsahu)",
            source=f"GET {path}",
            scope=f"{symbol}, okno {row['sample']} seancí",
            percentile=round(row["percentile"], 4),
            session_range_pt=row["session_range"],
        )
    else:
        values["vol_regime"] = missing(
            error or _EMPTY_LIST, clock=clock, unit="koš", source=path, scope=symbol
        )
    path = f"/ivrank/{symbol}?limit=1"
    payload, error = _get(fetch, path)
    latest = payload["latest"] if payload else []
    if not latest:
        values["iv"] = missing(
            error or _EMPTY_LIST, clock=clock, unit="IV", source=path, scope=symbol
        )
    for row in latest:
        source_name = row["source"]
        window = (
            "okno tasty (velikost nepublikuje, sample 0)"
            if source_name == "tasty"
            else f"okno {row['sample']} seancí"
        )
        rank = row.get("iv_rank")
        values[f"iv_{source_name}"] = _daily(
            row,
            round(row["iv"], 4),
            clock=clock,
            unit="IV roční, desetinně (0.12 = 12 %)",
            source=f"GET {path} → latest, zdroj {source_name}",
            scope=f"{symbol} IV {source_name}, {window}",
            iv_rank=round(rank, 4) if rank is not None else None,
            iv_percentile=(
                round(row["iv_percentile"], 4) if row.get("iv_percentile") is not None else None
            ),
            **(
                {"iv_rank_reason": "málo vzorků vlastní řady (ADR-0028 — žádný default)"}
                if rank is None
                else {}
            ),
            note="řady ibkr / tasty / own_atm se nemíchají (jiná konstrukce IV)",
        )
    path = f"/emrespect/{symbol}?limit=1"
    payload, error = _get(fetch, path)
    rows = payload["rows"] if payload else []
    summary = payload.get("summary") if payload else None
    if rows:
        row = rows[0]
        values["em_last_session"] = _daily(
            row,
            row["em_points"],
            clock=clock,
            unit="bod (± od kotvy)",
            source=f"GET {path} → poslední řádek",
            scope=f"{symbol} EM {row['em_source']} v {row['ref_ts']}",
            anchor=row["anchor"],
            range_vs_em=round(row["range_vs_em"], 3),
            note="EM minulé seance — pro dnešní vzdálenosti nepoužitelné",
        )
    else:
        values["em_last_session"] = missing(
            error or _EMPTY_LIST, clock=clock, unit="bod", source=path, scope=symbol
        )
    if summary and rows:
        values["em_respect"] = _daily(
            rows[0],
            round(summary["close_in_band_share"], 4),
            clock=clock,
            unit="podíl seancí s close v pásmu EM",
            source=f"GET {path} → summary",
            scope=f"{symbol}, okno {summary['window_days']} dní",
            n=summary["n"],
            touch_upper_share=round(summary["touch_upper_share"], 4),
            touch_lower_share=round(summary["touch_lower_share"], 4),
            note="naměřená četnost bez CI — REST interval nevrací → E-3.5",
        )
    else:
        values["em_respect"] = missing(
            error or "summary bez dat v okně",
            clock=clock,
            unit="podíl",
            source=path,
            scope=symbol,
        )
    return _tool("vol_context", symbol, clock, values)


def setup_library(fetch: Fetch, symbols: Sequence[str], clock: Clock) -> dict[str, object]:
    path = f"/setups/summary?symbols={','.join(symbols)}"
    payload, error = _get(fetch, path)
    values: dict[str, object] = {}
    if payload is None:
        values["library"] = missing(
            str(error), clock=clock, unit="buňky", source=path, scope=",".join(symbols)
        )
        return _tool("setup_library", None, clock, values)
    # Souhrn se počítá při čtení — as_of je okamžik dotazu
    as_of = clock.now
    mechanics = payload.get("mechanics_version")
    for cell in payload["cells"]:
        values[cell["cell"]] = measured(
            cell["gate_verdict"],
            clock=clock,
            unit="verdikt brány teď (allow/block…)",
            as_of=as_of,
            source=f"GET {path} → cells",
            scope=f"{cell['ticker']} šablona T{cell['template_number']} {cell['template']}, "
            f"mechanika v{mechanics}, okno brány",
            stage=cell["stage"],
            effective_stage=cell["effective_stage"],
            n=cell["gate_n"],
            n_needed=cell["n_needed"],
            sessions_to_decision=cell["sessions_to_decision"],
            avg_r_gross=_round(cell["avg_r"]),
            avg_r_net=_round(cell["avg_net_r"]),
            lower_bound_r=_round(cell["gate_lb"]),
            lower_bound_def=LOWER_BOUND_DEF,
        )
    return _tool("setup_library", None, clock, values)


def _round(value: float | None) -> float | None:
    return round(value, 3) if value is not None else None


def _symbol_tools(fetch: Fetch, symbol: str, clock: Clock) -> dict[str, object]:
    session = data_session(fetch, symbol, clock)
    return {
        "levels_now": levels_now(fetch, symbol, clock, session),
        "session_context": session_context(fetch, symbol, clock, session),
        "vol_context": vol_context(fetch, symbol, clock),
    }


def sample(fetch: Fetch, symbols: Sequence[str], clock: Clock) -> dict[str, object]:
    """Celá ukázka: tři nástroje per symbol + knihovna setupů."""
    return {
        "generated_at": clock.now.isoformat(),
        "trading_session": clock.trading_session.isoformat(),
        "market_open": clock.market_open,
        "symbols": {symbol: _symbol_tools(fetch, symbol, clock) for symbol in symbols},
        "setup_library": setup_library(fetch, symbols, clock),
    }


def http_fetch(base: str, client: httpx.Client) -> Fetch:
    def fetch(path: str) -> Any:
        try:
            response = client.get(f"{base.rstrip('/')}{path}")
        except httpx.HTTPError as error:
            raise ApiError(path, type(error).__name__) from error
        if response.status_code != 200:
            raise ApiError(path, f"HTTP {response.status_code}")
        return response.json()

    return fetch


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", default="http://127.0.0.1:8010", help="základ URL API")
    parser.add_argument("--symbols", default="ES,NQ")
    args = parser.parse_args(argv)
    symbols = [item.strip() for item in args.symbols.split(",") if item.strip()]
    with httpx.Client(timeout=20.0) as client:
        result = sample(http_fetch(args.api, client), symbols, make_clock(dt.datetime.now(dt.UTC)))
    json.dump(result, sys.stdout, ensure_ascii=False, indent=1)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
