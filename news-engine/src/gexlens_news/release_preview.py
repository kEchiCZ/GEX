"""Upozornění před ohlášeným releasem (#1296 fáze 3, ADR-0044) — čisté funkce.

60 a 15 min před releasem (CPI, NFP, FOMC, PPI, PCE; Retail Sales a ISM Services
jako „slabší řada“), ES a NQ zvlášť:

* **očekávaná výchylka za 15 min** = medián–p75 z minulých releasů rodiny,
  přepočtená na dnešní volatilitu (`release_moves.SizeStats`), v bodech
  (úrovně jsou v bodech) a v bp; odkud číslo je, říká druhý řádek;
* **úrovně** (call/put zeď, flip, těžiště) v dosahu p75 od ceny a ty dál;
* **směr jen u jádra inflace**: předem registrovaná hypotéza H1 se stavem
  ověřování — dokud není ověřená, bez pravděpodobnosti; opačný scénář
  (chladnější než odhad) se neuvádí. Žádné jiné směrové tvrzení
  (rozhodnutí uživatele 26. 9. 2026 — výzkum směr jinde neprokázal);
* **dovětek M1** (velikost ověřená živě) jen u rodin, které M1 hodnotí —
  slabší řady ho nenesou ani po ověření M1.

Čísla s desetinnou tečkou jako ceny („0.70×“, „7639.25“).

Chybějící data jsou vidět v textu (cena, úrovně, volatilita, málo historie),
upozornění kvůli nim nezmizí.
"""

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from gexlens_news.preopen import LEVEL_FIELDS, MESSAGE_BUDGET, SessionLevels, price_text
from gexlens_news.release_hypotheses import (
    H1_SERIES,
    M1_FAMILIES,
    STATUS_REJECTED,
    STATUS_VERIFIED,
)
from gexlens_news.release_moves import MIN_HISTORY, MOVE_WINDOW_MIN, SizeStats
from gexlens_news.releases import FAMILY_LABELS, WEAK_FAMILIES, ReleaseCluster

PREVIEW_KIND = "release_preview"
STAGE_T60 = "T60"
STAGE_T15 = "T15"
#: Etapy a jejich předstih před releasem (rozhodnutí uživatele 26. 9.)
PREVIEW_LEADS: tuple[tuple[str, dt.timedelta], ...] = (
    (STAGE_T60, dt.timedelta(minutes=60)),
    (STAGE_T15, dt.timedelta(minutes=15)),
)
#: Směrové sdělení tam, kde H1 neplatí (výzkum směr jinde neprokázal)
NO_DIRECTION = "Směr: bez prokazatelného efektu."
#: Úrovně starší než tohle dostanou v textu čas (engine je píše každou minutu)
LEVELS_STALE = dt.timedelta(minutes=10)

_LEVEL_LABELS = {
    "call_wall": "call zeď",
    "put_wall": "put zeď",
    "flip": "flip",
    "centroid": "těžiště",
}


def stages_to_send(due: Sequence[str]) -> tuple[str | None, list[str]]:
    """Ze splatných etap odejde jen poslední (pozdní start = jedna zpráva), označí se všechny."""
    if not due:
        return None, []
    return due[-1], list(due)


@dataclass(frozen=True)
class HypothesisView:
    """Stav hypotézy pro text: živé k/n, interval a historie z výzkumu."""

    status: str
    hits: int
    n: int
    wilson_lb: float | None
    wilson_ub: float | None
    historical_hits: int
    historical_n: int


def _signed(value: float) -> str:
    """Vzdálenost od ceny se znaménkem bez zbytečných nul: +4.5, -20.5, +3.25."""
    text = f"{value:+.2f}".rstrip("0").rstrip(".")
    return "0" if text in ("+0", "-0") else text


def _points_word(value: float) -> str:
    """Tvar „bod“ podle posledního čísla rozsahu: 1 bod, 2–4 body, 5 bodů."""
    count = round(value)
    if count == 1:
        return "bod"
    return "body" if 2 <= count <= 4 else "bodů"


def _percent(value: float) -> int:
    return round(100 * value)


def level_items(levels: SessionLevels) -> list[tuple[str, float]]:
    """Úrovně zaokrouhlené na celé body — vzdálenost od ceny se počítá z toho, co text ukazuje
    (těžiště 7622.24 by jinak stálo v textu jako „7622 (-17.01)“)."""
    return [
        (_LEVEL_LABELS[name], float(round(value)))
        for name in LEVEL_FIELDS
        if (value := getattr(levels, name)) is not None
    ]


def levels_lines(
    symbol: str,
    levels: SessionLevels | None,
    price: float | None,
    reach_pts: float | None,
    *,
    now: dt.datetime,
    tz: ZoneInfo,
) -> list[str]:
    """„V dosahu lo–hi: …“ a „Dál: …“ seřazené podle vzdálenosti; chybějící data v textu."""
    if levels is None:
        return [f"Úrovně {symbol} chybí"]
    items = level_items(levels)
    if not items:
        return [f"Úrovně {symbol} chybí"]
    stale = (
        f" (úrovně z {levels.ts.astimezone(tz):%H:%M})" if now - levels.ts > LEVELS_STALE else ""
    )
    if price is None or reach_pts is None:
        listed = " · ".join(f"{label} {value:.0f}" for label, value in items)
        return [f"Úrovně {symbol}: {listed}{stale}"]
    ordered = sorted(items, key=lambda item: abs(item[1] - price))

    def text(chosen: list[tuple[str, float]]) -> str:
        return " · ".join(
            f"{label} {value:.0f} ({_signed(value - price)})" for label, value in chosen
        )

    inside = [item for item in ordered if abs(item[1] - price) <= reach_pts]
    outside = [item for item in ordered if abs(item[1] - price) > reach_pts]
    reach = f"V dosahu {price - reach_pts:.0f}–{price + reach_pts:.0f}"
    lines = [f"{reach}: {text(inside) if inside else 'žádná úroveň'}{stale}"]
    if outside:
        lines.append(f"Dál: {text(outside)}")
    return lines


def expectation_lines(
    label: str,
    stats: SizeStats | None,
    history_n: int,
    vol_now_bp: float | None,
    price: float | None,
) -> tuple[list[str], float | None]:
    """Řádky očekávané výchylky a dosah v bodech (p75 od ceny; None = bez ceny/statistiky).

    První řádek nese odhad (body první — úrovně jsou v bodech), druhý jeho původ.
    """
    lead = f"Očekávaná výchylka do {MOVE_WINDOW_MIN} min"
    if stats is None:
        return [f"{lead}: málo historie (n = {history_n}, potřeba aspoň {MIN_HISTORY})"], None
    source = f"medián–p75 z {stats.n} {label}"
    if vol_now_bp is None:
        low, high = stats.raw_p50_bp, stats.raw_p75_bp
        detail = f"{source} · bez přepočtu na dnešní volatilitu"
    else:
        low, high = stats.expected_bp(vol_now_bp)
        detail = (
            f"{source} · volatilita {stats.vol_ratio(vol_now_bp):.2f}× obvyklé "
            f"(bez přepočtu {stats.raw_p50_bp:.0f}–{stats.raw_p75_bp:.0f} bp)"
        )
    bp = f"{low:.0f}–{high:.0f} bp"
    if price is None:
        return [f"{lead}: {bp}", detail], None
    low_pts, high_pts = low * price / 10_000, high * price / 10_000
    points = f"{low_pts:.0f}–{high_pts:.0f} {_points_word(high_pts)}"
    return [f"{lead}: {points} ({bp})", detail], high_pts


def h1_line(view: HypothesisView | None) -> str | None:
    """Jediný směrový řádek upozornění; zamítnutá H1 zmizí, opačný scénář se neuvádí."""
    if view is None or view.status == STATUS_REJECTED:
        return None
    if view.status == STATUS_VERIFIED and view.n and view.wilson_lb is not None:
        upper = view.wilson_ub if view.wilson_ub is not None else 1.0
        return (
            f"H1 – ověřeno živě {view.hits} z {view.n}: jádro inflace teplejší než odhad → "
            f"za {MOVE_WINDOW_MIN} min níž s pravděpodobností {_percent(view.hits / view.n)} % "
            f"[{_percent(view.wilson_lb)}–{_percent(upper)} %]"
        )
    return (
        f"H1 – OVĚŘUJE SE: jádro inflace teplejší než odhad → historicky za "
        f"{MOVE_WINDOW_MIN} min níž ve {view.historical_hits} ze {view.historical_n} "
        f"(živě {view.hits} z {view.n})"
    )


def m1_line(view: HypothesisView | None) -> str | None:
    """Dovětek o velikosti — jen po ověření M1."""
    if view is None or view.status != STATUS_VERIFIED:
        return None
    return f"Velikost nad běžným dnem ověřena živě {view.hits} z {view.n} (M1)"


def _released_text(cluster: ReleaseCluster, limit: int | None) -> str:
    names = []
    for index, event in enumerate(cluster.events):
        name = event.series
        if index == 0 and event.forecast_text:
            name = f"{name} (odhad {event.forecast_text})"
        names.append(name)
    if limit is not None and len(names) > limit:
        names = [*names[:limit], f"a další {len(names) - limit}"]
    return "Vyjde: " + ", ".join(names)


def build_preview(
    symbol: str,
    cluster: ReleaseCluster,
    *,
    now: dt.datetime,
    tz: ZoneInfo,
    price: float | None,
    levels: SessionLevels | None,
    stats: SizeStats | None,
    history_n: int,
    vol_now_bp: float | None,
    h1: HypothesisView | None,
    m1: HypothesisView | None,
) -> dict[str, Any]:
    """Payload `release_preview` pro jeden instrument (kanál `alerts`).

    Etapa (T−60 / T−15) se v textu projeví jen časem „za N min“ a čerstvými
    daty (cena, úrovně, volatilita) — obsah je jinak stejný.
    """
    family = cluster.family or ""
    label = FAMILY_LABELS.get(family, family)
    weak = " (slabší řada)" if family in WEAK_FAMILIES else ""
    minutes = max(0, round((cluster.ts - now).total_seconds() / 60))
    quote = f"{symbol} {price_text(price)}" if price is not None else f"{symbol} cena chybí"
    head = f"{label}{weak} za {minutes} min ({cluster.ts.astimezone(tz):%H:%M}) — {quote}"
    expectation, reach = expectation_lines(label, stats, history_n, vol_now_bp, price)
    level_block = levels_lines(symbol, levels, price, reach, now=now, tz=tz)
    direction = h1_line(h1) if cluster.headline.series in H1_SERIES else None
    # M1 hodnotí jen své rodiny — u slabší řady by dovětek tvrdil neexistující ověření
    size = m1_line(m1) if family in M1_FAMILIES else None
    tail = [
        *filter(None, [size, direction]),
        "Směr jinak bez prokazatelného efektu." if direction else NO_DIRECTION,
    ]

    def compose(released_limit: int | None, with_far: bool) -> str:
        levels_part = level_block if with_far else level_block[:1]
        return "\n".join(
            [head, _released_text(cluster, released_limit), *expectation, *levels_part, *tail]
        )

    # Pod MESSAGE_BUDGET (Telegram ořízne 900 znaků i s hlavičkou): nejdřív pryč
    # úrovně mimo dosah, pak zkrátit výčet releasů — nikdy ne směr ani velikost
    message = compose(None, True)
    for limit, with_far in ((None, False), (3, False), (1, False)):
        if len(message) <= MESSAGE_BUDGET:
            break
        message = compose(limit, with_far)
    return {
        "kind": PREVIEW_KIND,
        "symbol": symbol,
        "message": message,
        "ts": int(now.timestamp()),
        "ts_event": cluster.ts.isoformat(),
        "event_ids": [event.id for event in cluster.events],
    }
