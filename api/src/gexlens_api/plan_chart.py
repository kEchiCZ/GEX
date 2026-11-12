"""Serverový PNG plánu opce QQQ a záložní graf scénáře NQ (ADR-0062 K1 A, F4b #1744).

Dvě části, ať API matplotlib neimportuje a pravidlo „API nepočítá“ platí dál:
- **Popis grafu** (`plan_chart_spec`, `scenario_chart_spec`) jsou čisté funkce nad uloženými
  hodnotami: platný plán řádku z `option_plans_store.effective_plan` (táž funkce jako seznam
  plánů a vyhodnocení, kopie pravidla není), řádek scénáře, úrovně NQ uložené v plánu
  a uložené 1m bary. Nic nepřepočítávají: jen vybírají hodnoty a dávají jim popisky.
- **Kreslení** (`render_png`) spustí krátký podproces `gexlens_api.chart_render`, který popis
  nakreslí matplotlibem. Paměť podprocesu se po skončení vrátí, pád kreslení neshodí API.
  Kreslí se nejvýš jeden obrázek naráz. Selhání je `ChartRenderError` s důvodem, nikdy
  prázdný obrázek.

Graf plánu QQQ: cena QQQ z barů do okamžiku kreslení, cesta scénáře v cenách QQQ (vstup
scénáře a cíle v časech cesty), převedené úrovně, úroveň vstupu A (vstup B je jen stín,
nekreslí se — K6 B), SL a vstup A po spoušti, TP1/TP2, GEX top QQQ (P1), exit. Pod grafem
varianty opce s kotací, IV, deltou a modelem v TP, P&L na kontrakt (až po spoušti, K12 A),
přecenění z 9:31 ET, mezery a P5. Záložní graf NQ (pro Telegram, když heatmapa z aplikace
nedorazí, F4c #1745) je ve stejném stylu: bary NQ seance, vstup, cíle, cesta a úrovně NQ.
"""

import datetime as dt
import json
import os
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from gexlens_api.scenario_routes import PNG_MAGIC
from gexlens_engine.compute.option_plan import LEVEL_LABELS
from gexlens_engine.compute.option_plan import _cz as cz
from gexlens_engine.compute.settle import session_bounds, settle_ts
from gexlens_engine.storage.option_plans_store import (
    REPRICING_KEY,
    OptionPlanRow,
    effective_plan,
)
from gexlens_engine.storage.parquet_store import StoredBar, read_bars
from gexlens_engine.storage.scenarios_store import ScenarioRow

#: Podklad plánu = `tasty/plan_chain.PRODUCT` (import by do API tahal moduly tasty a IBKR;
#: shodu hlídá test)
PLAN_SYMBOL = "QQQ"
#: Limit podprocesu: první kreslení v novém kontejneru staví cache fontů (lokálně 8,5 s)
RENDER_TIMEOUT_S = 60.0
#: Zóna popisků času: vlastník čte časy plánu v SEČ (Telegram 15:15 SEČ)
CHART_TZ = "Europe/Prague"
TIME_LABEL = "čas Praha"
#: Bary QQQ v grafu plánu od 30 min před vznikem plánu (9:15 ET), ať je vidět i předtrh
PLAN_LOOKBACK = dt.timedelta(minutes=30)
#: Délka důvodu ze stderr podprocesu v odpovědi a logu
REASON_MAX = 400

_render_lock = threading.Lock()


class ChartRenderError(RuntimeError):
    """Kreslení PNG selhalo; zpráva nese důvod (časový limit, kód a stderr podprocesu)."""


def _reason(stderr: bytes) -> str:
    lines = [line.strip() for line in stderr.decode("utf-8", "replace").splitlines()]
    last = next((line for line in reversed(lines) if line), "bez výpisu na stderr")
    return last[:REASON_MAX]


def render_png(spec: Mapping[str, Any], *, timeout_s: float = RENDER_TIMEOUT_S) -> bytes:
    """PNG z popisu grafu v krátkém podprocesu (ADR-0062 K1 A); selhání = `ChartRenderError`."""
    payload = json.dumps(spec, ensure_ascii=False).encode("utf-8")
    command = [sys.executable, "-m", "gexlens_api.chart_render"]
    # Důvod ze stderr se čte jako UTF-8 i tam, kde konzole má jiné kódování (Windows cp1250)
    env = {**os.environ, "MPLBACKEND": "Agg", "PYTHONIOENCODING": "utf-8"}
    with _render_lock:
        try:
            done = subprocess.run(
                command, input=payload, capture_output=True, timeout=timeout_s, env=env, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise ChartRenderError(f"kreslení nedoběhlo do {timeout_s:g} s") from exc
        except OSError as exc:
            raise ChartRenderError(f"podproces kreslení nešel spustit: {exc}") from exc
    if done.returncode != 0:
        raise ChartRenderError(
            f"podproces kreslení skončil kódem {done.returncode}: {_reason(done.stderr)}"
        )
    if not done.stdout.startswith(PNG_MAGIC):
        raise ChartRenderError("podproces kreslení nevrátil PNG")
    return done.stdout


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).isoformat()


def _local(value: str | dt.datetime) -> str:
    """Čas popisku v zóně grafu (`HH:MM`)."""
    moment = dt.datetime.fromisoformat(value) if isinstance(value, str) else value
    return moment.astimezone(ZoneInfo(CHART_TZ)).strftime("%H:%M")


def _day(value: dt.date | str) -> str:
    day = dt.date.fromisoformat(value) if isinstance(value, str) else value
    return f"{day.day}. {day.month}."


def _price_series(label: str, bars: Sequence[StoredBar]) -> dict[str, Any]:
    return {"label": label, "points": [[_iso(bar.ts), bar.close] for bar in bars]}


def _bars_note(symbol: str, bars: Sequence[StoredBar]) -> str:
    if not bars:
        return f"Bary {symbol}: v uložených partiích zatím nejsou."
    return f"Bary {symbol} (1m close) do {_local(bars[-1].ts)}, uložené partice derived/bars."


def plan_bars_window(row: OptionPlanRow) -> tuple[dt.datetime, dt.datetime]:
    """Okno barů QQQ grafu plánu: od 30 min před vznikem plánu do settle seance."""
    return row.created_at - PLAN_LOOKBACK, settle_ts(row.day)


def read_plan_bars(row: OptionPlanRow, derived_dir: Path, now: dt.datetime) -> list[StoredBar]:
    """Uložené bary QQQ okna plánu do okamžiku kreslení `now` (point-in-time)."""
    since, until = plan_bars_window(row)
    return read_bars(derived_dir, PLAN_SYMBOL, since, min(now, until))


def image_state(row: OptionPlanRow) -> str:
    """Stav plánu, ke kterému patří PNG (vlastník 12. 11., #1738: snímek stavu plánu, A).

    `0915` = plán z 9:15 ET, `0931` = po přecenění (klíč `plan.repricing`, F2c), `vyhodnoceno`
    = po vyhodnocení (F3 #1748). Změna stavu = PNG se nakreslí znovu, jinak se jen čte.
    """
    if row.evaluated_at is not None:
        return "vyhodnoceno"
    return "0931" if REPRICING_KEY in row.plan else "0915"


def _variant_note(variant: Mapping[str, Any]) -> list[str]:
    right = "call" if variant["right"] == "C" else "put"
    at_targets = " / ".join(cz(value) for value in variant.get("at_targets") or ())
    verdict = variant["verdict"]
    if variant.get("reasons"):
        verdict = f"{verdict} — {'; '.join(variant['reasons'])}"
    lines = [
        f"{variant['role'].capitalize()}: {right} {variant['strike']:g} exp. "
        f"{_day(variant['expiry'])} · bid/ask {cz(variant['bid'])}/{cz(variant['ask'])} "
        f"({_local(variant['quote_ts'])}, {variant.get('source', '')}) · IV "
        f"{cz(variant['iv'] * 100, 1)} % · Δ {cz(variant['delta'])} · model TP1/TP2 "
        f"{at_targets or '—'} · {verdict}"
    ]
    pricing = variant.get("pricing")
    if pricing:
        tp2 = pricing.get("pnl_tp2_usd")
        rr = pricing.get("rr")
        pct = pricing.get("loss_at_stop_pct")
        lines.append(
            f"P&L na kontrakt: SL {_signed(pricing['loss_at_stop_usd'])} $"
            + (f" ({cz(pct, 1)} % účtu)" if pct is not None else "")
            + f" · TP1 {_signed(pricing['pnl_tp1_usd'])} $"
            + (f" · TP2 {_signed(tp2)} $" if tp2 is not None else "")
            + f" · čerpání {_signed(pricing['drawdown_usd'])} $"
            + (f" · R:R {cz(rr)}" if rr is not None else " · R:R nejde určit")
        )
    return lines


def _signed(value: float) -> str:
    """Dolary na kontrakt se znaménkem (ztráta už je záporná, zisk dostane +)."""
    text = cz(value, 0)
    return text if value < 0 else f"+{text}"


def _repriced_notes(prices: Mapping[str, Any] | None) -> list[str]:
    """Ceny z přecenění v 9:31 ET hlavního plánu (`effective_plan.prices.main`)."""
    if not prices:
        return []
    notes = []
    for item in prices.get("main") or ():
        right = "call" if item["right"] == "C" else "put"
        at_targets = " / ".join(cz(value) for value in item.get("at_targets") or ())
        notes.append(
            f"Přecenění {_local(item['quote_ts'])}: {item['role']} {right} {item['strike']:g} · "
            f"bid/ask {cz(item['bid'])}/{cz(item['ask'])} · IV {cz(item['iv'] * 100, 1)} % · "
            f"model TP1/TP2 {at_targets or '—'}"
        )
    return notes


def _plan_lines(main: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Vodorovné úrovně a body grafu plánu z uložených hodnot (žádný přepočet)."""
    levels: Mapping[str, float] = main.get("levels") or {}
    targets: list[float] = list(main.get("targets") or ())
    level_a = main.get("level_a")
    lines: list[dict[str, Any]] = []
    markers: list[dict[str, Any]] = []
    target_names: dict[int, str] = {}
    for key, label in LEVEL_LABELS.items():
        if key not in levels:
            continue
        value = float(levels[key])
        if level_a is not None and key == level_a[0]:
            continue  # kreslí se jako úroveň vstupu A
        hit = next((i for i, target in enumerate(targets) if target == value), None)
        if hit is not None:
            target_names.setdefault(hit, label)
            continue  # kreslí se jako cíl
        lines.append({"y": value, "label": f"{label} {cz(value)}", "role": "level"})
    if level_a is not None:
        name, price = level_a[0], float(level_a[1])
        lines.append(
            {
                "y": price,
                "label": f"vstup A od {LEVEL_LABELS.get(name, name)} {cz(price)}",
                "role": "entry",
            }
        )
    for index, target in enumerate(targets):
        named = f" = {target_names[index]}" if index in target_names else ""
        lines.append({"y": target, "label": f"TP{index + 1}{named} {cz(target)}", "role": "target"})
    for ts, target in zip(main.get("target_ts") or (), targets, strict=False):
        markers.append({"x": ts, "y": target, "label": ""})
    p1 = main.get("p1")
    if p1 and p1.get("strike") is not None:
        gex = p1.get("net_gex_1pct")
        size = f" ({cz(gex / 1e6, 0)} M)" if gex is not None else ""
        lines.append(
            {"y": float(p1["strike"]), "label": f"GEX top {p1['strike']:g}{size}", "role": "gex"}
        )
    entry = main.get("entry")
    if entry:
        lines.append(
            {"y": float(entry["stop"]), "label": f"SL {cz(entry['stop'])}", "role": "stop"}
        )
        markers.append(
            {
                "x": entry["ts"],
                "y": float(entry["price"]),
                "label": f"vstup {entry['kind']} {_local(entry['ts'])} za {cz(entry['price'])}",
            }
        )
    if main.get("spot") is not None:
        markers.append(
            {"x": main["created_at"], "y": float(main["spot"]), "label": f"spot {cz(main['spot'])}"}
        )
    return lines, markers


def plan_chart_spec(row: OptionPlanRow, bars: Sequence[StoredBar]) -> dict[str, Any]:
    """Popis grafu plánu QQQ z platného plánu řádku a uložených barů QQQ."""
    effective = effective_plan(row.plan)
    main: Mapping[str, Any] | None = effective["main"]
    since, settle = plan_bars_window(row)
    notes: list[str] = []
    spec: dict[str, Any] = {
        "tz": CHART_TZ,
        "time_label": TIME_LABEL,
        "x_min": _iso(since),
        "price": _price_series(f"{PLAN_SYMBOL} 1m", bars),
        "empty_note": f"bary {PLAN_SYMBOL} zatím nejsou",
        "vlines": [],
        "lines": [],
        "markers": [],
    }
    x_max = settle
    if main is None:
        spec["title"] = (
            f"Plán {PLAN_SYMBOL} {_day(row.day)} · plán z {effective['slot']} ET · bez opce"
        )
        spec["subtitle"] = f"stav: {row.status} — důvody pod grafem"
    else:
        side = "SHORT" if main["direction"] == "short" else "LONG"
        right = "call" if main["right"] == "C" else "put"
        spec["title"] = (
            f"Plán {PLAN_SYMBOL} {_day(row.day)} · {side} → {right} 1DTE · "
            f"plán z {effective['slot']} ET"
        )
        spec["subtitle"] = f"verdikt: {main['verdict']}"
        spec["lines"], spec["markers"] = _plan_lines(main)
        if main.get("scenario_entry") is not None:
            path = [[main["created_at"], float(main["scenario_entry"])]]
            path += [
                [ts, float(target)]
                for ts, target in zip(
                    main.get("target_ts") or (), main.get("targets") or (), strict=False
                )
            ]
            spec["path"] = {"label": "cesta scénáře NQ v cenách QQQ", "points": path}
        exit_ts = dt.datetime.fromisoformat(main["exit_ts"])
        spec["vlines"] = [
            {"x": main["created_at"], "label": f"plán {effective['slot']} ET"},
            {"x": main["exit_ts"], "label": "exit"},
        ]
        for ts in main.get("target_ts") or ():
            x_max = max(x_max, dt.datetime.fromisoformat(ts))
        x_max = max(x_max, exit_ts)
        for variant in main.get("variants") or ():
            notes.extend(_variant_note(variant))
        notes.extend(_repriced_notes(effective["prices"]))
        if main.get("missing"):
            notes.append(f"Chybí: {'; '.join(main['missing'])}")
        notes.append(f"P5: {main['p5']}")
        notes.append(f"Ceny v TP a P&L = {main['model_label']}; ceny opcí z řetězu, ne odhad.")
    if effective["reasons"]:
        notes.append(f"Mezery 9:15 ET: {'; '.join(effective['reasons'])}")
    if effective["repricing_reasons"]:
        notes.append(f"Mezery 9:31 ET: {'; '.join(effective['repricing_reasons'])}")
    notes.append(_bars_note(PLAN_SYMBOL, bars))
    spec["x_max"] = _iso(x_max + dt.timedelta(minutes=15))
    spec["notes"] = notes
    return spec


def scenario_bars_window(scenario: ScenarioRow) -> tuple[dt.datetime, dt.datetime]:
    """Okno barů grafu scénáře: od otevření Globex seance dne vzniku do termínu (settle)."""
    return session_bounds(scenario.day)[0], scenario.deadline_ts


def scenario_chart_spec(
    scenario: ScenarioRow, levels_nq: Mapping[str, Any] | None, bars: Sequence[StoredBar]
) -> dict[str, Any]:
    """Popis záložního grafu scénáře NQ: bary seance, vstup, cíle, cesta, úrovně NQ.

    `levels_nq` jsou úrovně NQ uložené v plánu QQQ ke scénáři (`plan.levels_nq`); scénář bez
    plánu je nemá a graf je ukáže bez nich (poznámka pod grafem).
    """
    since, deadline = scenario_bars_window(scenario)
    rationale = scenario.rationale or {}
    target_names: list[str] = list(rationale.get("targets") or ())
    verdict = rationale.get("verdict")
    score = rationale.get("score")
    head = f"Scénář dne {scenario.symbol} {_day(scenario.day)}"
    if verdict:
        head += f" · {str(verdict).upper()}"
    if score is not None:
        head += f" · skóre {cz(float(score), 0)}"
    lines: list[dict[str, Any]] = []
    target_values = [float(target) for target in scenario.targets]
    for key, label in LEVEL_LABELS.items():
        value = (levels_nq or {}).get(key)
        if value is None or float(value) in target_values:
            continue
        lines.append({"y": float(value), "label": f"{label} {cz(float(value))}", "role": "level"})
    lines.append({"y": scenario.entry, "label": f"vstup {cz(scenario.entry)}", "role": "entry"})
    for index, target in enumerate(target_values):
        name = f" {target_names[index]}" if index < len(target_names) else ""
        lines.append(
            {"y": target, "label": f"cíl {index + 1}{name} {cz(target)}", "role": "target"}
        )
    notes = [
        f"Serverový graf scénáře #{scenario.id} (heatmapa z aplikace nedorazila); "
        f"vznik {_local(scenario.created_at)}, termín {_day(scenario.deadline)} "
        f"{_local(scenario.deadline_ts)}.",
    ]
    if levels_nq is None:
        notes.append("Úrovně NQ: scénář nemá plán QQQ, úrovně nejsou uložené.")
    notes.append(_bars_note(scenario.symbol, bars))
    return {
        "title": head,
        "subtitle": "cesta a cíle scénáře dne (ADR-0062 Q2b)",
        "tz": CHART_TZ,
        "time_label": TIME_LABEL,
        "x_min": _iso(since),
        "x_max": _iso(deadline + dt.timedelta(minutes=15)),
        "price": _price_series(f"{scenario.symbol} 1m", bars),
        "empty_note": f"bary {scenario.symbol} zatím nejsou",
        "path": {
            "label": "cesta scénáře",
            "points": [[point["ts"], float(point["price"])] for point in scenario.path],
        },
        "lines": lines,
        "vlines": [{"x": _iso(scenario.created_at), "label": "scénář"}],
        "markers": [],
        "notes": notes,
    }


def scenario_chart_png(
    scenario: ScenarioRow,
    plan: OptionPlanRow | None,
    derived_dir: Path,
    now: dt.datetime,
) -> bytes:
    """Záložní PNG scénáře NQ (pro Telegram F4c #1745); bary do okamžiku kreslení `now`."""
    since, deadline = scenario_bars_window(scenario)
    bars = read_bars(derived_dir, scenario.symbol, since, min(now, deadline))
    levels = plan.plan.get("levels_nq") if plan is not None else None
    return render_png(scenario_chart_spec(scenario, levels, bars))
