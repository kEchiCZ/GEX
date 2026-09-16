"""Shrnutí kouče (#1201): 4–6 vět z čísel — týden, nejdražší chyba, okna dne,
setupy detektoru, pravidla. Žádné LLM; každá věta má číslo a zdroj.

Vstupy jsou hotové slovníky z `coach.weekly_report`, `coach_setups.time_of_day_profile`
a `coach_setups.setups_report` (as_dict), aby šlo shrnutí složit v API bez
opakovaného výpočtu a testovat nad syntetickými daty.
"""

from typing import Any

from gexlens_engine.compute.coach import FLAG_LABELS
from gexlens_engine.compute.coach_setups import SEGMENT_LABELS


def _fmt_r(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:+.2f} R"


def _window_sentence(profile: dict[str, Any], who: str) -> str | None:
    segments = profile.get("segments") or {}
    best, worst = profile.get("best_segment"), profile.get("worst_segment")
    if not best and not worst:
        return None
    parts: list[str] = []
    if best and best in segments:
        b = segments[best]
        parts.append(
            f"nejlepší okno {SEGMENT_LABELS.get(best, best)} ({_fmt_r(b['avg_r'])}, n={b['n']})"
        )
    if worst and worst in segments and worst != best:
        w = segments[worst]
        parts.append(
            f"nejhorší {SEGMENT_LABELS.get(worst, worst)} ({_fmt_r(w['avg_r'])}, n={w['n']})"
        )
    return f"{who}: " + ", ".join(parts) + "." if parts else None


def coach_summary(
    weekly: dict[str, Any] | None,
    hours: dict[str, Any] | None,
    setups: dict[str, Any] | None,
) -> dict[str, Any]:
    """Věty shrnutí + „na co si dnes dát pozor" (pravidla + okna) pro Briefing."""
    lines: list[str] = []
    watch: list[str] = []
    if weekly and weekly.get("n"):
        score = weekly.get("score")
        lines.append(
            f"Tento týden {weekly['n']} obchodů, {_fmt_r(weekly.get('total_r'))}, "
            f"úspěšnost {round((weekly.get('win_rate') or 0) * 100)} %, "
            f"disciplína {score if score is not None else '—'}/100."
        )
        flags = weekly.get("flags") or {}
        if flags:
            kind, stat = min(flags.items(), key=lambda item: (item[1]["cost_r"], -item[1]["n"]))
            cost = stat.get("cost_r", 0.0)
            lines.append(
                f"Nejdražší chyba: {FLAG_LABELS.get(kind, kind)} ({int(stat['n'])}×"
                + (f", {_fmt_r(cost)}" if cost < 0 else "")
                + ")."
            )
        for rule in (weekly.get("rules") or [])[:2]:
            watch.append(rule["advice"])
    else:
        lines.append("Tento týden zatím žádný obchod v deníku.")
    if hours:
        trades_profile = hours.get("trades") or {}
        setups_profile = hours.get("setups") or {}
        sentence = _window_sentence(trades_profile, "Tvoje obchody")
        if sentence:
            lines.append(sentence)
            worst = trades_profile.get("worst_segment")
            if worst:
                watch.append(f"Neobchodovat v okně {SEGMENT_LABELS.get(worst, worst)}.")
        sentence = _window_sentence(setups_profile, "Setupy detektoru")
        if sentence:
            lines.append(sentence)
    if setups and setups.get("n"):
        recs = setups.get("recommendations") or []
        avoid = next((r for r in recs if r["kind"] == "avoid"), None)
        focus = next((r for r in recs if r["kind"] == "focus"), None)
        if avoid or focus:
            parts = []
            if avoid:
                parts.append(avoid["text"].rstrip("."))
            if focus:
                parts.append(focus["text"][0].lower() + focus["text"][1:].rstrip("."))
            lines.append("Setupy: " + "; ".join(parts) + ".")
            if avoid:
                watch.append(avoid["text"].split(":")[0] + ".")
        else:
            lines.append(
                f"Setupy: {setups['n']} uzavřených za {setups.get('days', 60)} dní, "
                f"{_fmt_r(setups.get('total_r'))} — zatím žádná kombinace s dostatečným vzorkem."
            )
    return {"lines": lines, "watch": watch[:4]}
