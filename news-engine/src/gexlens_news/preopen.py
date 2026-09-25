"""Předobchodní upozornění na zprávy za zavřený trh (#1291 Q2, ADR-0043) — čisté funkce.

Rozhodnutí uživatele 25. 9. 2026 (změna Q2): ne souhrn **po** otevření (gap už
nastal), ale upozornění **před** otevřením po víkendu, aby se trader stihl
připravit na nedělní / pondělní open:

* **hlavní souhrn 4 h před otevřením Globexu** (v běžném týdnu 20:00 Praha),
* **aktualizace 15 min před otevřením**, jen když od hlavního souhrnu vyšla
  nová významná zpráva,
* ES a NQ zvlášť: **zásadní** zprávy (`is_key`, upřesnění uživatele 25. 9.)
  s klasifikovaným směrem, souhrnný sklon 🟢/🔴/⚪ jen z nich a klíčové úrovně
  z poslední seance; ostatní významné (varianta B, `clusters.is_significant`)
  a šum jen počtem; **bez pravděpodobnosti** (#1287),
* nic, když za zavřený trh nevyšla žádná zásadní zpráva (ani když varianta B
  něco splní — simulace v ADR-0043); denní pauza (1 h) se nehlásí.

Časy se odvozují od otevření Globexu (`settle.session_bounds` = 17:00 CT,
`marketclock.is_market_closed` = rozvrh), ne od pevných hodin — DST v Chicagu
a v Praze se přepíná v jiné dny. Rozvrh nezná svátky (ADR-0023 bod 4): svátek,
který zkrátí nebo zruší páteční seanci (Velký pátek, Vánoce či Nový rok
v pátek, 3. 7.), se projeví jen dřívějším posledním barem — okno zpráv začne
od něj, otevření zůstává nedělní. Svátek s celodenním zavřením v pondělí
(Vánoce či Nový rok v pondělí nebo s náhradním pondělím, poprvé 25. 12. 2028)
pokrytý **není**: rozvrh ohlásí nedělní otevření, které nenastane, a skutečné
otevření v pondělí 17:00 CT bere jako konec denní pauzy.
"""

import datetime as dt
import math
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from gexlens_engine.compute.marketclock import is_market_closed
from gexlens_engine.compute.settle import session_bounds, trading_session_date
from gexlens_news.clusters import (
    MAX_LISTED,
    SCHEDULED_KIND,
    SOCIAL_KIND,
    ClusterEvent,
    clean_title,
    is_significant,
    significance_tier,
)
from gexlens_news.dedup import DEFAULT_JACCARD_THRESHOLD
from gexlens_news.model import normalize_title

PREOPEN_KIND = "news_preopen"

STAGE_MAIN = "main"
STAGE_UPDATE = "update"
#: Etapy a jejich předstih před otevřením Globexu (rozhodnutí uživatele 25. 9.)
STAGE_LEADS: tuple[tuple[str, dt.timedelta], ...] = (
    (STAGE_MAIN, dt.timedelta(hours=4)),
    (STAGE_UPDATE, dt.timedelta(minutes=15)),
)
#: Denní pauza trvá hodinu (16–17 CT): 2 h před otevřením je po pauze trh
#: otevřený, po víkendu zavřený — tak se pozná zavření delší než pauza
PAUSE_PROBE = dt.timedelta(hours=2)
#: Kolik dní zpět hledat poslední bar před zavřením (víkend + svátek + rezerva)
CLOSURE_LOOKBACK = dt.timedelta(days=5)
#: Telegram ořízne zprávu na 900 znaků i s hlavičkou „📰 GEXLens · ES“ —
#: text se proto vejde pod tuto mez ubráním odrážek, ne useknutím konce
MESSAGE_BUDGET = 860

#: Zásadní zpráva (upřesnění uživatele 25. 9., #1291): kategorie pravidlového
#: i LLM klasifikátoru (`NEWS_CATEGORIES`) pro Fed, makro a geopolitiku —
#: obchod a cla klasifikátor řadí do GEOPOLITICS (regex `tariff|sanction`),
#: samostatnou kategorii nemají
KEY_CATEGORIES = frozenset({"FED", "MACRO_INFLATION", "MACRO_LABOR", "MACRO_GROWTH", "GEOPOLITICS"})
KEY_MIN_IMPORTANCE = 3

UP_GLYPH = "🟢"
DOWN_GLYPH = "🔴"
NEUTRAL_GLYPH = "⚪"
LEVEL_FIELDS = ("call_wall", "put_wall", "flip", "centroid")

_MINUTE = dt.timedelta(minutes=1)
#: „v pondělí 00:00“ (4. pád) a „od pátku 22:55“ (2. pád)
_WEEKDAY_AT = ("v pondělí", "v úterý", "ve středu", "ve čtvrtek", "v pátek", "v sobotu", "v neděli")
_WEEKDAY_FROM = ("pondělí", "úterý", "středy", "čtvrtka", "pátku", "soboty", "neděle")
_LEVEL_LABELS = {
    "call_wall": "call zeď",
    "put_wall": "put zeď",
    "flip": "flip",
    "centroid": "těžiště",
}


# ── Kdy ────────────────────────────────────────────────────────────


def upcoming_open(now: dt.datetime) -> dt.datetime:
    """Nejbližší otevření Globexu po `now` podle rozvrhu (17:00 CT, DST přes zoneinfo)."""
    day = trading_session_date(now)
    for _ in range(8):
        opening = session_bounds(day)[1]
        if opening > now and not is_market_closed(opening):
            return opening
        day += dt.timedelta(days=1)
    raise RuntimeError(f"Rozvrh Globexu nemá do týdne od {now.isoformat()} otevření")


def follows_long_closure(opening: dt.datetime) -> bool:
    """Předchází otevření víkend (ne jen hodinová denní pauza)? Podle rozvrhu."""
    return is_market_closed(opening - PAUSE_PROBE)


def due_stages(now: dt.datetime, opening: dt.datetime, done: Collection[str]) -> list[str]:
    """Etapy, jejichž čas nastal a které ještě neproběhly; po otevření už žádné.

    Když news-engine ve 20:00 neběžel, pošle hlavní souhrn při prvním běhu po
    startu — i když je to až v čase aktualizace (pak jedna zpráva, ne dvě).
    """
    if now >= opening:
        return []
    return [name for name, lead in STAGE_LEADS if opening - lead <= now and name not in done]


def scheduled_close(opening: dt.datetime) -> dt.datetime:
    """Začátek zavření, které končí `opening`, podle rozvrhu (bez svátků).

    Jen záloha, když archiv nemá poslední bar (nový archiv, dlouhý výpadek).
    """
    ts = opening - _MINUTE
    limit = opening - CLOSURE_LOOKBACK
    while ts > limit and is_market_closed(ts - _MINUTE):
        ts -= _MINUTE
    return ts


# ── Obsah ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SessionLevels:
    """Klíčové úrovně GEX z poslední minuty poslední seance (partice `levels`)."""

    ts: dt.datetime
    expiry: dt.date
    call_wall: float | None
    put_wall: float | None
    flip: float | None
    centroid: float | None


def _level(value: Any) -> float | None:
    if value is None:
        return None
    number = float(value)
    return None if math.isnan(number) else number


def last_levels(
    rows: Iterable[Mapping[str, Any]], until: dt.datetime, expiry: dt.date
) -> SessionLevels | None:
    """Poslední řádek levels před `until` s aspoň jednou úrovní.

    Engine píše minuty i při zavřeném trhu (úrovně prázdné) — ty se přeskočí.
    """
    best: SessionLevels | None = None
    for row in rows:
        ts = row.get("ts_min")
        if not isinstance(ts, dt.datetime):
            continue
        ts = ts if ts.tzinfo else ts.replace(tzinfo=dt.UTC)
        values = {name: _level(row.get(name)) for name in LEVEL_FIELDS}
        if ts >= until or all(value is None for value in values.values()):
            continue
        if best is None or ts > best.ts:
            best = SessionLevels(ts=ts, expiry=expiry, **values)
    return best


@dataclass(frozen=True)
class Bias:
    """Souhrnný sklon významných zpráv — jen počty směrů, žádná pravděpodobnost."""

    up: int
    down: int
    neutral: int

    @property
    def glyph(self) -> str:
        if self.up > self.down:
            return UP_GLYPH
        if self.down > self.up:
            return DOWN_GLYPH
        return NEUTRAL_GLYPH

    def text(self) -> str:
        return (
            f"{self.glyph} ({UP_GLYPH} {self.up} · {DOWN_GLYPH} {self.down} · "
            f"{NEUTRAL_GLYPH} {self.neutral})"
        )


def is_key(event: ClusterEvent) -> bool:
    """Zásadní zpráva — do výčtu a sklonu předobchodního souhrnu (podmnožina významných).

    Upřesnění uživatele 25. 9. (#1291): víkend nese ~15–50 „významných“
    zpráv podle varianty B a mezi nimi šum, který regexový klasifikátor
    povýšil. Zásadní je kalendář FF High/Medium, kurátor na sociálních sítích
    (obojí jako ve variantě B) a headline/broker v kategorii Fed, makro nebo
    geopolitika (obchod a cla) s importance 3. Intradenní shluky (`clusters`)
    se tím neřídí.
    """
    if not is_significant(event):
        return False
    if event.kind in (SCHEDULED_KIND, SOCIAL_KIND):
        return True
    return event.category in KEY_CATEGORIES and (event.importance or 0) >= KEY_MIN_IMPORTANCE


def direction_glyph(direction: int | None) -> str:
    """Směr z klasifikace; neklasifikovaná zpráva je neutrální."""
    if direction == 1:
        return UP_GLYPH
    if direction == -1:
        return DOWN_GLYPH
    return NEUTRAL_GLYPH


def sentiment_bias(events: Iterable[ClusterEvent]) -> Bias:
    up = down = neutral = 0
    for event in events:
        if event.direction == 1:
            up += 1
        elif event.direction == -1:
            down += 1
        else:
            neutral += 1
    return Bias(up=up, down=down, neutral=neutral)


def distinct_stories(
    events: Iterable[ClusterEvent], prefer: Collection[int] = ()
) -> list[ClusterEvent]:
    """Tatáž story z více zdrojů jednou — zůstane ohlášená (`prefer`), jinak první výskyt.

    Ingest slučuje fuzzy jen headline a broker (#274); víkendové posty
    („… - reuters.com“ a „… - Reuters“) by jinak zabraly půlku výčtu i sklonu.
    Stejná míra jako ingest (Jaccard ≥ 0,9 nad normalizovaným titulkem,
    ADR-0016); kalendář se neslučuje („Core CPI“ ≠ „CPI“). Ohlášené zprávy
    mají přednost: kopie zapsaná po hlavním souhrnu, ale s dřívějším
    `ts_event` (zdroj s opožděným feedem), by jinak story vydala za novou.
    """
    kept: list[ClusterEvent] = []
    seen: list[frozenset[str]] = []
    ordered = sorted(events, key=lambda item: (item.id not in prefer, item.ts_event, item.id))
    for event in ordered:
        tokens = frozenset(normalize_title(event.title).split())
        if event.kind != SCHEDULED_KIND and any(
            len(tokens & other) >= DEFAULT_JACCARD_THRESHOLD * len(tokens | other) for other in seen
        ):
            continue
        kept.append(event)
        if event.kind != SCHEDULED_KIND and tokens:
            seen.append(tokens)
    return kept


def preopen_order(events: Iterable[ClusterEvent]) -> list[ClusterEvent]:
    """Pořadí výčtu: stupeň významnosti, v něm nejnovější první (blíž otevření)."""
    return sorted(
        events, key=lambda event: (significance_tier(event), -event.ts_event.timestamp(), event.id)
    )


def _price(value: float) -> str:
    """Cena bez zbytečných nul: 7725, 7716.25, 29987.75."""
    return f"{value:.2f}".rstrip("0").rstrip(".")


def levels_line(symbol: str, levels: SessionLevels | None, last_close: float | None) -> str:
    """Úrovně, na které si dát pozor při gapu; chybějící data jsou vidět."""
    close = f"close {_price(last_close)}" if last_close is not None else None
    if levels is None:
        return " · ".join(filter(None, [f"Úrovně {symbol} z poslední seance chybí", close]))
    parts = [
        f"{_LEVEL_LABELS[name]} {value:.0f}"
        for name in LEVEL_FIELDS
        if (value := getattr(levels, name)) is not None
    ]
    if close:
        parts.append(close)
    return (
        f"Úrovně {symbol} z poslední seance (expirace {levels.expiry.day}. "
        f"{levels.expiry.month}.): " + " · ".join(parts)
    )


def _until(delta: dt.timedelta) -> str:
    minutes = max(0, round(delta.total_seconds() / 60))
    hours, rest = divmod(minutes, 60)
    if not hours:
        return f"za {rest} min"
    return f"za {hours} h" + (f" {rest} min" if rest else "")


def counts_text(key_rest: int, other: int, noise: int) -> str | None:
    """„další zásadní: j · další významné: k · ostatní zprávy: n“; nuly se vynechají.

    `key_rest` = zásadní, které se do výčtu nevešly; `other` = významné podle
    varianty B, které zásadní nejsou; `noise` = ostatní zprávy.
    """
    parts = [
        f"{label}: {count}"
        for label, count in (
            ("další zásadní", key_rest),
            ("další významné", other),
            ("ostatní zprávy", noise),
        )
        if count > 0
    ]
    return " · ".join(parts) or None


def _fit(head: Sequence[str], listed: Sequence[ClusterEvent], other: int, noise: int) -> str:
    """Složí text a ubírá odrážky, dokud se nevejde do MESSAGE_BUDGET."""
    shown = min(len(listed), MAX_LISTED)
    while True:
        bullets = [
            f"• {direction_glyph(event.direction)} {clean_title(event.title)}"
            for event in listed[:shown]
        ]
        counts = counts_text(len(listed) - shown, other, noise)
        message = "\n".join([*head, *bullets, *([counts] if counts else [])])
        if len(message) <= MESSAGE_BUDGET or shown == 0:
            return message
        shown -= 1


def build_preopen(
    stage: str,
    symbol: str,
    opening: dt.datetime,
    *,
    since: dt.datetime,
    events: Sequence[ClusterEvent],
    announced: Collection[int],
    levels: SessionLevels | None,
    last_close: float | None,
    now: dt.datetime,
    tz: ZoneInfo,
) -> dict[str, Any] | None:
    """Payload `news_preopen` pro jeden instrument; None = není co hlásit.

    `events` = všechny zprávy za zavřený trh (od `since`), které jsou v `now`
    v DB; `announced` = zásadní zprávy už ohlášené hlavním souhrnem
    (`announced_ids`). Výčet a sklon nesou jen zásadní zprávy (`is_key`),
    ostatní významné a šum jsou jen počty. Hlavní souhrn vyjmenuje všechny
    zásadní, aktualizace jen nové. Když hlavní souhrn nic neohlásil,
    aktualizace s novou zprávou vypadá jako souhrn. Bez zásadní zprávy se nic
    neposílá, i když varianta B něco splní: výčet by byl prázdný a zbyly by
    jen počty toho, co uživatel označil za šum (rozhodnutí podle simulace,
    ADR-0043 bod 5). Tatáž story z více zdrojů se počítá i vypisuje jednou;
    opakování už ohlášené story (i s dřívějším `ts_event`) za novou nevydává.
    """
    significant = distinct_stories(
        (event for event in events if is_significant(event)), prefer=announced
    )
    key = preopen_order(event for event in significant if is_key(event))
    fresh = [event for event in key if event.id not in announced]
    update = stage == STAGE_UPDATE and bool(announced)
    listed = fresh if stage == STAGE_UPDATE else key
    if not listed:
        return None
    local_open = opening.astimezone(tz)
    local_since = since.astimezone(tz)
    title = "Aktualizace před otevřením" if update else "Před otevřením"
    since_text = f"od {_WEEKDAY_FROM[local_since.weekday()]} {local_since:%H:%M}"
    if update:
        summary = (
            f"Nové zásadní zprávy: {len(fresh)} (celkem {len(key)} {since_text}) · "
            f"sklon všech {sentiment_bias(key).text()}"
        )
        other = noise = 0
    else:
        summary = (
            f"Zásadní zprávy za zavřený trh {since_text}: {len(key)} · "
            f"sklon {sentiment_bias(key).text()}"
        )
        other = len(significant) - len(key)
        noise = sum(1 for event in events if not is_significant(event))
    head = [
        f"{title} {symbol}: Globex otevře {_WEEKDAY_AT[local_open.weekday()]} "
        f"{local_open:%H:%M} ({_until(opening - now)})",
        levels_line(symbol, levels, last_close),
        summary,
    ]
    return {
        "kind": PREOPEN_KIND,
        "symbol": symbol,
        "message": _fit(head, listed, other, noise),
        "ts": int(now.timestamp()),
        "ts_event": opening.isoformat(),
        "event_ids": [event.id for event in listed],
    }


def announced_ids(events: Iterable[ClusterEvent]) -> set[int]:
    """Id zpráv, které etapa obsáhla: všechny zásadní (i kopie téže story).

    Neprázdná množina = hlavní souhrn odešel; významné, které zásadní nejsou,
    se neukládají — aktualizaci nespouštějí a hlavní souhrn bez zásadní zprávy
    nevznikne, takže by aktualizaci mylně označily za „Aktualizaci“.
    """
    return {event.id for event in events if is_key(event)}


# ── Stav (dedup i přes restart) ────────────────────────────────────


@dataclass
class PreopenState:
    """Co už bylo pro otevření `opening` vyhodnoceno a ohlášeno.

    Ukládá se do PG (`settings`) PŘED odesláním: restart news-enginu v neděli
    večer tak souhrn nepošle podruhé — riziko je ztráta (pád mezi zápisem
    a publikací), ne duplicita.
    """

    opening: dt.datetime
    done: set[str] = field(default_factory=set)
    #: symbol → id zásadních zpráv, které už upozornění obsáhlo (`announced_ids`)
    announced: dict[str, set[int]] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "opening": self.opening.isoformat(),
            "done": sorted(self.done),
            "announced": {symbol: sorted(ids) for symbol, ids in self.announced.items()},
        }

    @classmethod
    def from_json(cls, value: Any, opening: dt.datetime) -> "PreopenState":
        """Stav pro `opening`; stav jiného (minulého) otevření = čistý začátek."""
        if not isinstance(value, dict) or value.get("opening") != opening.isoformat():
            return cls(opening)
        done = value.get("done")
        announced = value.get("announced")
        return cls(
            opening,
            done={str(name) for name in done} if isinstance(done, list) else set(),
            announced={
                str(symbol): {int(item) for item in ids}
                for symbol, ids in (announced.items() if isinstance(announced, dict) else ())
                if isinstance(ids, list)
            },
        )
