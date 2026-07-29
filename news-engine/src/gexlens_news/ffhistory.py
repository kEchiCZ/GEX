"""Historický kalendář ForexFactory + surprise_z (#277, SPEC 3.4, ADR-0018).

Widget feed historii nenese (ADR-0013), ale webové stránky kalendáře
(`/calendar?week=jul7.2025`) mají v HTML vložený JSON s kompletními daty
včetně **forecast i actual** — tedy skutečný konsensus, ne aproximaci.
Ověřeno 29. 7. 2026: `dateline` je epoch UTC (nezávislý na timezone
stránky), eventy mají i stabilní `id`.

Tři role modulu:

1. **Jednorázový backfill** (`run_backfill`, CLI `backfill-ff`) — stáhne N
   týdnů historie a založí scheduled eventy; primární trénovací dataset.
2. **Hodinový refresh actual** (`FfActualRefreshJob`) — widget feed `actual`
   nemá, tahle stránka ano; job doplňuje `actual` proběhlým eventům. Do
   napojení oficiálních API (BLS/BEA/FRED, ADR-0013) je to jediný zdroj
   actual hodnot — latence ~1 h nesplňuje cíl „do 3 min" z kap. 10, viz ADR.
3. **surprise_z** (`recompute_surprise_z`) — z = (actual − forecast) / σ,
   kde σ je výběrová směrodatná odchylka překvapení téže řady (klíč =
   normalizovaný titulek). SPEC mluví o σ „z FRED/BLS historie"; σ přímo
   z FF překvapení je věrnější — měří přesně (actual − forecast), zatímco
   FRED forecasty nemá (ADR-0018).

Scrape je defenzivní a šetrný: jedna stránka za `throttle_s`, prohlížečová
UA hlavička (FF holé klienty odmítá, ADR-0014), formát negarantovaný —
nečitelný týden se přeskočí a nezabije celý backfill.
"""

import datetime as dt
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from statistics import stdev
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import news_events
from gexlens_news.classifier import classify_category
from gexlens_news.collectors.forexfactory import _IMPACT, _SYMBOLS_BY_COUNTRY, parse_number
from gexlens_news.model import NewsEvent, normalize_title
from gexlens_news.store import NewsWriter

logger = logging.getLogger(__name__)

HISTORY_URL = "https://www.forexfactory.com/calendar"
# Vložený JS objekt se dny kalendáře; `days:` je jediný klíč, který čteme —
# vnitřek pole je striktní JSON (klíče v uvozovkách), obal je JS
_DAYS_PATTERN = re.compile(r"calendarComponentStates\[\d+\]\s*=\s*\{\s*days:\s*")

_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")

# Minimum překvapení v řadě, aby σ nebyla šum pár měření
MIN_SERIES_SAMPLES = 6

# Fetch podpis: parametr `week` („jul7.2025") → HTML stránky
FetchPage = Callable[[str], str]


def week_param(day: dt.date) -> str:
    """Datum → hodnota `?week=` („jul7.2025"). Den bez nul, měsíc lowercase."""
    return f"{_MONTHS[day.month - 1]}{day.day}.{day.year}"


def mondays_back(weeks: int, *, today: dt.date) -> list[dt.date]:
    """Pondělky N týdnů zpět, od nejstaršího — backfill jde chronologicky."""
    this_monday = today - dt.timedelta(days=today.weekday())
    return [this_monday - dt.timedelta(weeks=offset) for offset in range(weeks, 0, -1)]


def extract_days(html: str) -> list[dict[str, Any]]:
    """Pole `days` z vloženého JS objektu; nečitelná stránka → prázdný seznam.

    Obal je JavaScript (klíče bez uvozovek), takže se JSON parsuje až
    vybalancované pole za `days:` — vnitřek už striktní JSON je (ověřeno).
    """
    match = _DAYS_PATTERN.search(html)
    if match is None:
        logger.warning("Stránka kalendáře bez calendarComponentStates — formát se změnil?")
        return []
    start = match.end()
    depth = 0
    end = None
    for index in range(start, len(html)):
        char = html[index]
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    if end is None:
        logger.warning("Pole days bez konce — stránka oříznutá?")
        return []
    try:
        days = json.loads(html[start:end])
    except json.JSONDecodeError:
        logger.warning("Pole days není validní JSON — formát se změnil?")
        return []
    return days if isinstance(days, list) else []


def normalize_entry(entry: dict[str, Any], *, fetched_at: dt.datetime) -> NewsEvent | None:
    """Event z historické stránky → NewsEvent; None = nepoužitelný záznam.

    Tvar zrcadlí živý collector (`collectors/forexfactory.py`), aby tentýž
    event z obou cest dostal identický titulek, `source_uid` i `dedup_hash`
    — jinak by se backfill s živým sběrem nesešel.
    """
    name = str(entry.get("name") or "").strip()
    dateline = entry.get("dateline")
    if not name or not isinstance(dateline, (int, float)) or dateline <= 0:
        return None
    ts_event = dt.datetime.fromtimestamp(float(dateline), tz=dt.UTC)
    currency = str(entry.get("currency") or "").upper()
    importance = _IMPACT.get(str(entry.get("impactName") or "").lower(), 1)
    return NewsEvent(
        ts_event=ts_event,
        ts_ingested=fetched_at,
        source="forexfactory",
        source_uid=f"{currency}|{name}|{ts_event.isoformat()}",
        kind="scheduled",
        category=classify_category(name),
        importance=importance,
        title=f"{currency} {name}" if currency else name,
        summary=None,
        symbols=list(_SYMBOLS_BY_COUNTRY.get(currency, [])),
        forecast=parse_number(entry.get("forecast")),
        previous=parse_number(entry.get("previous")),
        actual=parse_number(entry.get("actual")),
        raw={k: entry.get(k) for k in ("id", "name", "currency", "dateline", "impactName")},
    )


def fetch_week(week: str, *, timeout_s: float = 30.0) -> str:
    """Jedna stránka kalendáře přes curl_cffi s Chrome impersonací.

    Cloudflare před FF pouští Windows TLS stack, ale **linuxový blokuje 403**
    bez ohledu na hlavičky (změřeno 29. 7.: httpx i systémový curl z
    kontejneru 403, z Windows hostu 200, stejná IP). curl_cffi napodobuje
    kompletní Chrome fingerprint (JA3 + HTTP/2), a z kontejneru prochází.
    """
    from curl_cffi import requests as cffi_requests

    response = cffi_requests.get(
        HISTORY_URL,
        params={"week": week},
        impersonate="chrome",
        timeout=timeout_s,
    )
    # curl_cffi má jen částečné typy — raise_for_status je untyped
    response.raise_for_status()  # type: ignore[no-untyped-call]
    return str(response.text)


def update_actuals(engine: Engine, events: list[NewsEvent]) -> int:
    """Doplní actual/forecast/previous eventům, které už v DB jsou.

    Backfill i refresh potkají eventy založené živým collectorem (bez
    `actual`, ADR-0013) — insert je zahodí na dedup_hash, hodnoty se proto
    doplňují updatem. Přepisuje se jen NULL `actual`: pozdější revize čísel
    nemá měnit, na čem už mohly stavět reakce a klasifikace.
    """
    updated = 0
    with engine.begin() as conn:
        for event in events:
            if event.actual is None:
                continue
            result = conn.execute(
                update(news_events)
                .where(news_events.c.dedup_hash == event.dedup_hash)
                .where(news_events.c.actual.is_(None))
                .values(actual=event.actual, forecast=event.forecast, previous=event.previous)
            )
            updated += result.rowcount or 0
    return updated


def recompute_surprise_z(engine: Engine, *, min_samples: int = MIN_SERIES_SAMPLES) -> int:
    """Přepočet surprise_z všech scheduled eventů; vrací počet aktualizací.

    Idempotentní full-recompute: σ řady se s každým novým releasem zpřesňuje,
    takže přepočítat všechno je jednodušší a bezpečnější než inkrementální
    údržba. Řada = normalizovaný titulek (měsíční „USD CPI m/m" se potkává
    napříč roky). Jednotky v řadě jsou konzistentní (parse_number řeže % i
    K/M/B), takže σ je ve stejných jednotkách jako překvapení.
    """
    stmt = (
        select(
            news_events.c.id,
            news_events.c.title,
            news_events.c.forecast,
            news_events.c.actual,
            news_events.c.surprise_z,
        )
        .where(news_events.c.kind == "scheduled")
        .where(news_events.c.forecast.is_not(None))
        .where(news_events.c.actual.is_not(None))
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).fetchall()

    by_series: dict[str, list[Any]] = {}
    for row in rows:
        by_series.setdefault(normalize_title(row.title), []).append(row)

    updates: list[tuple[int, float]] = []
    for series in by_series.values():
        surprises = [float(row.actual) - float(row.forecast) for row in series]
        if len(surprises) < min_samples:
            continue
        sigma = stdev(surprises)
        if sigma <= 0:
            continue
        for row, surprise in zip(series, surprises, strict=True):
            z = surprise / sigma
            previous_z = float(row.surprise_z) if row.surprise_z is not None else None
            if previous_z is None or abs(previous_z - z) > 1e-9:
                updates.append((int(row.id), z))

    if updates:
        with engine.begin() as conn:
            for event_id, z in updates:
                conn.execute(
                    update(news_events).where(news_events.c.id == event_id).values(surprise_z=z)
                )
    return len(updates)


@dataclass(frozen=True)
class BackfillStats:
    """Výsledek backfillu pro CLI report."""

    weeks_fetched: int
    weeks_failed: int
    events_seen: int
    written: int
    actuals_updated: int
    surprise_updated: int

    def describe(self) -> str:
        return (
            f"týdnů {self.weeks_fetched} (chyb {self.weeks_failed}), "
            f"eventů {self.events_seen}, nových {self.written}, "
            f"doplněných actual {self.actuals_updated}, surprise_z {self.surprise_updated}"
        )


def run_backfill(
    engine: Engine,
    *,
    weeks: int,
    today: dt.date | None = None,
    fetch: FetchPage = fetch_week,
    throttle_s: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> BackfillStats:
    """Jednorázový backfill N týdnů; idempotentní (dedup_hash + NULL update).

    Jde chronologicky od nejstaršího týdne; selhání jednoho týdne se
    zaloguje a pokračuje se — jednorázový proces nemá spadnout kvůli jedné
    stránce (dá se pustit znovu, doplní se jen díry).
    """
    writer = NewsWriter(engine)
    now = dt.datetime.now(dt.UTC)
    stats = {"fetched": 0, "failed": 0, "seen": 0, "written": 0, "actuals": 0}
    for monday in mondays_back(weeks, today=today or now.date()):
        week = week_param(monday)
        try:
            html = fetch(week)
        except Exception:
            stats["failed"] += 1
            logger.exception("Týden %s se nepodařilo stáhnout — pokračuji", week)
            sleep(throttle_s)
            continue
        events = []
        for day in extract_days(html):
            for entry in day.get("events") or []:
                if isinstance(entry, dict):
                    event = normalize_entry(entry, fetched_at=now)
                    if event is not None:
                        events.append(event)
        stats["fetched"] += 1
        stats["seen"] += len(events)
        stats["written"] += writer.write(events)
        stats["actuals"] += update_actuals(engine, events)
        logger.info("Backfill %s: %d eventů", week, len(events))
        sleep(throttle_s)

    surprise = recompute_surprise_z(engine)
    return BackfillStats(
        weeks_fetched=stats["fetched"],
        weeks_failed=stats["failed"],
        events_seen=stats["seen"],
        written=stats["written"],
        actuals_updated=stats["actuals"],
        surprise_updated=surprise,
    )


class FfActualRefreshJob:
    """Hodinové doplňování `actual` z aktuální (+ minulé) stránky kalendáře.

    Widget feed `actual` nemá (ADR-0013) — bez tohohle jobu by scheduled
    eventy nikdy nedostaly hodnotu ani surprise_z. Minulý týden se čte kvůli
    eventům z pátku/víkendu při pondělním běhu.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        fetch: FetchPage = fetch_week,
        interval_s: float = 3600.0,
    ) -> None:
        self._engine = engine
        self._fetch = fetch
        self._interval = dt.timedelta(seconds=interval_s)
        self._last_run: dt.datetime | None = None

    def due(self, now: dt.datetime) -> bool:
        return self._last_run is None or now - self._last_run >= self._interval

    def run(self, now: dt.datetime) -> int:
        """Jeden průchod; vrací počet doplněných actual hodnot."""
        self._last_run = now
        today = now.date()
        this_monday = today - dt.timedelta(days=today.weekday())
        weeks = [week_param(this_monday)]
        # Začátkem týdne dozníva minulý týden (páteční NFP, víkend)
        if today.weekday() <= 1:
            weeks.insert(0, week_param(this_monday - dt.timedelta(weeks=1)))

        events: list[NewsEvent] = []
        for week in weeks:
            try:
                html = self._fetch(week)
            except Exception:
                logger.exception("Refresh actual: týden %s nedostupný — zkusí se příště", week)
                continue
            for day in extract_days(html):
                for entry in day.get("events") or []:
                    if isinstance(entry, dict):
                        event = normalize_entry(entry, fetched_at=now)
                        # Jen proběhlé eventy — budoucí actual mít nemohou
                        if event is not None and event.ts_event <= now:
                            events.append(event)
        if not events:
            return 0
        updated = update_actuals(self._engine, events)
        if updated:
            recompute_surprise_z(self._engine)
            logger.info("Refresh actual: doplněno %d hodnot", updated)
        return updated
