"""Kreslení PNG v krátkém podprocesu `python -m gexlens_api.chart_render` (ADR-0062 K1 A, #1744).

Proces API matplotlib nikdy neimportuje. Tento modul dostane na stdin popis grafu (JSON
z `plan_chart`) a na stdout vrátí PNG. Nic nepočítá a nic nečte: kreslí jen to, co popis
obsahuje (uložené hodnoty plánu nebo scénáře a uložené bary, ADR-0062 K1). Importuje jen
standardní knihovnu a matplotlib, takže paměť (+54 MB) se po skončení procesu vrátí.

Chyba je vidět, nikdy prázdný obrázek: neplatný popis skončí kódem 2, chyba kreslení
kódem 1, důvod jde jedním řádkem na stderr (čte ho `plan_chart.render_png`).

Popis grafu (časy ISO 8601 se zónou, ceny jako čísla):
- `title`, `subtitle`, `tz` (zóna popisků času), `time_label`, `x_min`, `x_max`;
- `price` a `path`: `{"label", "points": [[čas, cena], …]}`, cena z barů a cesta scénáře;
- `lines`: vodorovné úrovně `{"y", "label", "role"}`, role viz `LINE_STYLE`;
- `vlines`: svislé časy `{"x", "label"}`;
- `markers`: body `{"x", "y", "label"}`;
- `notes`: řádky textu pod grafem; `empty_note`: text do grafu bez ceny.
"""

import datetime as dt
import io
import json
import sys
import textwrap
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: Šířka PNG 1200 px (ADR-0062 Kontext); výška roste s počtem řádků poznámek, graf má stálou
WIDTH_IN, DPI = 12.0, 100
#: Svislé části obrázku v palcích: nadpis s legendou, graf, osa času, řádek poznámky
TITLE_IN, PLOT_IN, AXIS_IN, NOTE_LINE_IN = 1.0, 4.6, 0.55, 0.2
LEFT, RIGHT = 0.07, 0.79

# Paleta grafů (dataviz, světlý povrch): kategoriální sloty 1–3, inkousty, stavové barvy SL/TP
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
PRICE = "#2a78d6"
PATH = "#eb6834"
GEX = "#1baf7a"
GOOD = "#0ca30c"
CRITICAL = "#d03b3b"

#: Styl vodorovné úrovně podle role: (barva, tloušťka v pt, styl čáry, tučný popisek)
LINE_STYLE: dict[str, tuple[str, float, str, bool]] = {
    "level": (MUTED, 0.8, "--", False),
    "entry": (INK, 1.6, "-", True),
    "stop": (CRITICAL, 1.4, "-", True),
    "target": (GOOD, 1.4, "-", True),
    "gex": (GEX, 1.2, ":", False),
}
#: Nejmenší svislý rozestup popisků úrovní u pravého okraje (podíl výšky grafu)
LABEL_GAP = 0.035
#: Šířka řádku poznámek pod grafem (znaky)
NOTE_WIDTH = 150


class SpecError(ValueError):
    """Popis grafu nemá tvar, který kreslení zná."""


@dataclass(frozen=True)
class _Spec:
    zone: ZoneInfo
    x_min: dt.datetime
    x_max: dt.datetime
    price_label: str
    price: list[tuple[dt.datetime, float]]
    path_label: str
    path: list[tuple[dt.datetime, float]]
    lines: list[tuple[float, str, str]]
    vlines: list[tuple[dt.datetime, str]]
    markers: list[tuple[dt.datetime, float, str]]


def _time(value: Any, field: str) -> dt.datetime:
    if not isinstance(value, str):
        raise SpecError(f"{field}: čas musí být text ISO 8601")
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise SpecError(f"{field}: neplatný čas {value!r}") from exc
    if parsed.tzinfo is None:
        raise SpecError(f"{field}: čas bez zóny {value!r}")
    return parsed


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SpecError(f"{field}: chybí číslo")
    return float(value)


def _series(spec: dict[str, Any], key: str) -> tuple[str, list[tuple[dt.datetime, float]]]:
    raw = spec.get(key) or {"label": "", "points": []}
    points = [
        (_time(item[0], f"{key}[{index}]"), _number(item[1], f"{key}[{index}]"))
        for index, item in enumerate(raw.get("points", []))
    ]
    return str(raw.get("label", "")), points


def parse(spec: dict[str, Any]) -> _Spec:
    """Ověří popis grafu; každá chyba tvaru je `SpecError` s polem, ne pád kreslení."""
    try:
        zone = ZoneInfo(str(spec["tz"]))
        x_min = _time(spec["x_min"], "x_min")
        x_max = _time(spec["x_max"], "x_max")
        price_label, price = _series(spec, "price")
        path_label, path = _series(spec, "path")
        lines = [
            (_number(item["y"], f"lines[{i}]"), str(item["label"]), str(item["role"]))
            for i, item in enumerate(spec.get("lines", []))
        ]
        vlines = [
            (_time(item["x"], f"vlines[{i}]"), str(item["label"]))
            for i, item in enumerate(spec.get("vlines", []))
        ]
        markers = [
            (
                _time(item["x"], f"markers[{i}]"),
                _number(item["y"], f"markers[{i}]"),
                str(item["label"]),
            )
            for i, item in enumerate(spec.get("markers", []))
        ]
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise SpecError(f"chybí nebo vadné pole ({type(exc).__name__}: {exc})") from exc
    except ZoneInfoNotFoundError as exc:
        raise SpecError(f"neznámá zóna {spec.get('tz')!r}") from exc
    if x_max <= x_min:
        raise SpecError("x_max musí být po x_min")
    unknown = sorted({role for _, _, role in lines} - LINE_STYLE.keys())
    if unknown:
        raise SpecError(f"lines: neznámá role {', '.join(unknown)}")
    return _Spec(zone, x_min, x_max, price_label, price, path_label, path, lines, vlines, markers)


def spread_labels(ys: list[float], low: float, high: float) -> list[float]:
    """Pozice popisků úrovní: pořadí podle ceny, mezi sousedy aspoň `LABEL_GAP` výšky grafu.

    Posouvá jen popisek, čára úrovně zůstává na své ceně.
    """
    gap = (high - low) * LABEL_GAP
    placed = list(ys)
    previous: float | None = None
    for index in sorted(range(len(ys)), key=lambda i: ys[i]):
        value = placed[index]
        if previous is not None and value < previous + gap:
            value = previous + gap
        placed[index] = value
        previous = value
    return placed


def _price_format(value: float, _position: int | None = None) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def render(spec: dict[str, Any]) -> bytes:
    """PNG z popisu grafu; `SpecError` = neplatný popis, jiná výjimka = chyba kreslení."""
    parsed = parse(spec)
    import matplotlib

    matplotlib.use("Agg")
    # Texty nesou „$“ (P&L na kontrakt): bez vypnutí by je matplotlib četl jako vzorec
    matplotlib.rcParams["text.parse_math"] = False
    import matplotlib.dates as mdates
    from matplotlib.figure import Figure
    from matplotlib.ticker import FuncFormatter

    # matplotlib.dates nemá typové stuby (3.11: dates.pyi chybí), odtud ignore u tří volání
    def num(value: dt.datetime) -> float:
        return float(mdates.date2num(value))  # type: ignore[no-untyped-call]

    note_lines: list[str] = []
    for note in spec.get("notes", []):
        note_lines.extend(textwrap.wrap(str(note), NOTE_WIDTH, subsequent_indent="   ") or [""])
    notes_in = NOTE_LINE_IN * len(note_lines) + 0.25
    height_in = TITLE_IN + PLOT_IN + AXIS_IN + notes_in
    figure = Figure(figsize=(WIDTH_IN, height_in), dpi=DPI, facecolor=SURFACE)
    ax = figure.add_axes(
        (LEFT, (notes_in + AXIS_IN) / height_in, RIGHT - LEFT, PLOT_IN / height_in)
    )
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.grid(True, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)

    values = [y for _, y in parsed.price] + [y for _, y in parsed.path]
    values += [y for y, _, _ in parsed.lines] + [y for _, y, _ in parsed.markers]
    if values:
        low, high = min(values), max(values)
        pad = (high - low) * 0.06 or max(abs(high) * 0.002, 0.5)
        low, high = low - pad, high + pad
    else:
        low, high = 0.0, 1.0
        ax.set_yticks([])  # bez cen a úrovní by osa 0–1 předstírala hodnoty
    ax.set_xlim(num(parsed.x_min), num(parsed.x_max))
    ax.set_ylim(low, high)

    for y, _, role in parsed.lines:
        color, width, style, _ = LINE_STYLE[role]
        ax.axhline(y, color=color, linewidth=width, linestyle=style, zorder=2)
    label_ys = spread_labels([y for y, _, _ in parsed.lines], low, high)
    for (_, label, role), label_y in zip(parsed.lines, label_ys, strict=True):
        bold = LINE_STYLE[role][3]
        ax.annotate(
            label,
            xy=(1.0, label_y),
            xycoords=("axes fraction", "data"),
            xytext=(6, 0),
            textcoords="offset points",
            va="center",
            fontsize=9,
            color=INK if bold else INK_2,
            fontweight="bold" if bold else "normal",
            annotation_clip=False,
        )
    for x, label in parsed.vlines:
        ax.axvline(num(x), color=AXIS, linewidth=1.0, zorder=1)
        ax.annotate(
            label,
            xy=(num(x), 0.0),
            xycoords=("data", "axes fraction"),
            xytext=(3, 4),
            textcoords="offset points",
            fontsize=9,
            color=MUTED,
        )
    if parsed.price:
        ax.plot(
            [num(t) for t, _ in parsed.price],
            [y for _, y in parsed.price],
            color=PRICE,
            linewidth=1.5,
            label=parsed.price_label,
            zorder=4,
        )
    elif spec.get("empty_note"):
        ax.text(
            0.5,
            0.5,
            str(spec["empty_note"]),
            transform=ax.transAxes,
            ha="center",
            va="center",
            fontsize=11,
            color=MUTED,
        )
    if parsed.path:
        ax.plot(
            [num(t) for t, _ in parsed.path],
            [y for _, y in parsed.path],
            color=PATH,
            linewidth=1.5,
            linestyle="--",
            marker="o",
            markersize=5,
            label=parsed.path_label,
            zorder=5,
        )
    for x, y, label in parsed.markers:
        ax.plot(
            [num(x)],
            [y],
            marker="o",
            markersize=7,
            color=INK,
            markeredgecolor=SURFACE,
            markeredgewidth=1.5,
            zorder=6,
        )
        ax.annotate(
            label, xy=(num(x), y), xytext=(6, 6), textcoords="offset points", fontsize=9, color=INK
        )
    if parsed.price or parsed.path:
        # Nad grafem, ať nezakrývá úrovně ani cenu
        ax.legend(
            loc="lower left",
            bbox_to_anchor=(0.0, 1.0),
            ncol=2,
            fontsize=9,
            frameon=False,
            labelcolor=INK_2,
            borderaxespad=0.2,
        )

    locator = mdates.AutoDateLocator(tz=parsed.zone, minticks=5, maxticks=12)  # type: ignore[no-untyped-call]
    formatter = mdates.DateFormatter("%H:%M", tz=parsed.zone)  # type: ignore[no-untyped-call]
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(formatter)
    ax.yaxis.set_major_formatter(FuncFormatter(_price_format))
    ax.set_xlabel(str(spec.get("time_label", "")), fontsize=9, color=MUTED)

    title = str(spec.get("title", ""))
    figure.text(
        LEFT, 1 - 0.15 / height_in, title, fontsize=14, fontweight="bold", color=INK, va="top"
    )
    subtitle = str(spec.get("subtitle", ""))
    figure.text(LEFT, 1 - 0.47 / height_in, subtitle, fontsize=10, color=INK_2, va="top")
    figure.text(
        LEFT,
        (notes_in - 0.1) / height_in,
        "\n".join(note_lines),
        va="top",
        ha="left",
        fontsize=9,
        color=INK_2,
        linespacing=1.45,
    )

    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", facecolor=SURFACE)
    return buffer.getvalue()


def main() -> int:
    try:
        spec = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        if not isinstance(spec, dict):
            raise SpecError("popis grafu musí být objekt JSON")
        png = render(spec)
    except (SpecError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"neplatný popis grafu: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        # Důvod jde rodiči jedním řádkem na stderr (plan_chart.render_png), nic se nespolkne
        print(f"chyba kreslení: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(png)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
