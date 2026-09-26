"""Ohlášené USD releasy z FF kalendáře: řady, shluky a rodiny (#1296, ADR-0044).

Jediný zdroj definic, které sdílí výzkum (`scripts/build_release_reactions.py`),
měření reakcí (`release_moves_job.py`) i upozornění před releasem
(`release_preview_job.py`):

* **Řada** = titulek bez prefixu měny („USD Core CPI m/m“ → „Core CPI m/m“),
  skupina a polarita (+1 = vyšší číslo je silnější ekonomika / vyšší inflace /
  jestřábí Fed, −1 = opak — nezaměstnanost, žádosti o podporu, zásoby ropy).
* **Dopad** = FF impact ze surového payloadu (`raw.impact` živého feedu,
  `raw.impactName` z backfillu). Sloupec `importance` se nepoužívá — u scheduled
  ho přepisuje regexový klasifikátor (#1291, #1293).
* **Shluk** = všechny USD releasy s dopadem High/Medium ve stejné minutě; nesou
  identickou reakci trhu. **Headline** shluku je první řada podle `SERIES_RANK`
  (pořadí v `SERIES`), pak High před Medium.
* **Rodina** = rodina headline řady (CPI, NFP, FOMC, …); rodina bez záznamu
  v `FAMILY_OF_SERIES` se neměří ani neohlašuje.
"""

import datetime as dt
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import news_events
from gexlens_news.clusters import floor_minute

#: Titulek FF kalendáře USD releasu začíná měnou
USD_PREFIX = "USD "
IMPACT_HIGH = "high"
#: Dopady, které tvoří shluk releasu (malými písmeny)
QUALIFYING_IMPACTS = frozenset({"high", "medium"})

#: Řada → (skupina, polarita). Pořadí je zároveň pořadí headline (`SERIES_RANK`).
SERIES: dict[str, tuple[str, int]] = {
    "Federal Funds Rate": ("fed", 1),
    "Non-Farm Employment Change": ("labor", 1),
    "Core CPI m/m": ("inflation", 1),
    "CPI m/m": ("inflation", 1),
    "Core CPI y/y": ("inflation", 1),
    "CPI y/y": ("inflation", 1),
    "Core PCE Price Index m/m": ("inflation", 1),
    "Core PPI m/m": ("inflation", 1),
    "PPI m/m": ("inflation", 1),
    "Retail Sales m/m": ("growth", 1),
    "Core Retail Sales m/m": ("growth", 1),
    "Advance GDP q/q": ("growth", 1),
    "Prelim GDP q/q": ("growth", 1),
    "Final GDP q/q": ("growth", 1),
    "ISM Manufacturing PMI": ("growth", 1),
    "ISM Services PMI": ("growth", 1),
    "ADP Non-Farm Employment Change": ("labor", 1),
    "JOLTS Job Openings": ("labor", 1),
    "Unemployment Rate": ("labor", -1),
    "Average Hourly Earnings m/m": ("labor", 1),
    "Employment Cost Index q/q": ("labor", 1),
    "Unemployment Claims": ("labor", -1),
    "ISM Manufacturing Prices": ("inflation", 1),
    "Advance GDP Price Index q/q": ("inflation", 1),
    "Prelim GDP Price Index q/q": ("inflation", 1),
    "Final GDP Price Index q/q": ("inflation", 1),
    "Flash Manufacturing PMI": ("growth", 1),
    "Flash Services PMI": ("growth", 1),
    "Final Manufacturing PMI": ("growth", 1),
    "Final Services PMI": ("growth", 1),
    "Durable Goods Orders m/m": ("growth", 1),
    "Core Durable Goods Orders m/m": ("growth", 1),
    "Philly Fed Manufacturing Index": ("growth", 1),
    "Empire State Manufacturing Index": ("growth", 1),
    "Richmond Manufacturing Index": ("growth", 1),
    "Chicago PMI": ("growth", 1),
    "Prelim UoM Consumer Sentiment": ("sentiment", 1),
    "Revised UoM Consumer Sentiment": ("sentiment", 1),
    "CB Consumer Confidence": ("sentiment", 1),
    "Pending Home Sales m/m": ("housing", 1),
    "Existing Home Sales": ("housing", 1),
    "New Home Sales": ("housing", 1),
    "Building Permits": ("housing", 1),
    "S&P/CS Composite-20 HPI y/y": ("housing", 1),
    "Crude Oil Inventories": ("energy", -1),
}
#: Pořadí headline řad — souběžné releasy (CPI m/m + y/y + core…) nesou tutéž reakci
SERIES_RANK = {name: rank for rank, name in enumerate(SERIES)}

# ── Rodiny (#1296 fáze 3–4) ─────────────────────────────────────────
#: Robustní rodiny podle výzkumu (velikost nad běžným dnem IS i OOS) — pro
#: upozornění; hypotézy mají vlastní zmrazené kopie (`release_hypotheses`)
ROBUST_FAMILIES = ("CPI", "NFP", "FOMC", "PPI", "PCE")
#: Slabší rodiny — upozornění se štítkem „slabší řada“
WEAK_FAMILIES = ("RETAIL", "ISM_SERVICES")
#: Rodiny s upozorněním před releasem; Claims ne (týdenní, velikost nad běžným
#: dnem jen v 39–42 z 62 případů — bod k rozhodnutí v ADR-0044)
PREVIEW_FAMILIES = ROBUST_FAMILIES + WEAK_FAMILIES
#: Headline řada → rodina
FAMILY_OF_SERIES: dict[str, str] = {
    "Core CPI m/m": "CPI",
    "CPI m/m": "CPI",
    "Core CPI y/y": "CPI",
    "CPI y/y": "CPI",
    "Non-Farm Employment Change": "NFP",
    "Unemployment Rate": "NFP",
    "Average Hourly Earnings m/m": "NFP",
    "Federal Funds Rate": "FOMC",
    "Core PPI m/m": "PPI",
    "PPI m/m": "PPI",
    "Core PCE Price Index m/m": "PCE",
    "Retail Sales m/m": "RETAIL",
    "Core Retail Sales m/m": "RETAIL",
    "ISM Services PMI": "ISM_SERVICES",
}
FAMILY_LABELS = {
    "CPI": "CPI",
    "NFP": "NFP",
    "FOMC": "FOMC",
    "PPI": "PPI",
    "PCE": "PCE",
    "RETAIL": "Retail Sales",
    "ISM_SERVICES": "ISM Services",
}


def impact_of(raw: Any) -> str | None:
    """FF impact z raw (malými písmeny): backfill `impactName`, živý feed `impact`."""
    if not isinstance(raw, dict):
        return None
    value = raw.get("impactName") or raw.get("impact")
    return str(value).strip().lower() if value else None


def series_of(title: str) -> str:
    """„USD Core CPI m/m“ → „Core CPI m/m“."""
    return title[len(USD_PREFIX) :] if title.startswith(USD_PREFIX) else title


def family_of(series: str) -> str | None:
    return FAMILY_OF_SERIES.get(series)


def _sign(value: float) -> int:
    if abs(value) < 1e-12:
        return 0
    return 1 if value > 0 else -1


@dataclass(frozen=True)
class ReleaseEvent:
    """USD scheduled událost z `news_events` v rozsahu, který releasy potřebují."""

    id: int
    ts: dt.datetime
    title: str
    impact: str | None
    forecast: float | None = None
    actual: float | None = None
    #: Odhad tak, jak ho ukazuje FF („0.3%“, „58K“) — jen pro text upozornění
    forecast_text: str | None = None

    @property
    def series(self) -> str:
        return series_of(self.title)

    @property
    def minute(self) -> dt.datetime:
        return floor_minute(self.ts)

    def surprise_sign(self) -> int | None:
        """Znaménko (actual − forecast) × polarita řady; None = nejde určit.

        +1 = teplejší / silnější / jestřábí než odhad, −1 = opak, 0 = na odhadu.
        Polarita je z `SERIES`, neznámá řada → None.
        """
        if self.actual is None or self.forecast is None:
            return None
        polarity = SERIES.get(self.series, ("other", 0))[1]
        if not polarity:
            return None
        return _sign(self.actual - self.forecast) * polarity


def release_rank(event: ReleaseEvent) -> tuple[int, int, str]:
    """Řazení headline: pořadí řady, High před Medium, název řady."""
    return (
        SERIES_RANK.get(event.series, len(SERIES)),
        0 if event.impact == IMPACT_HIGH else 1,
        event.series,
    )


@dataclass(frozen=True)
class ReleaseCluster:
    """Releasy s dopadem High/Medium ve stejné minutě; `events` v pořadí headline."""

    ts: dt.datetime
    events: tuple[ReleaseEvent, ...]

    @property
    def headline(self) -> ReleaseEvent:
        return self.events[0]

    @property
    def family(self) -> str | None:
        return family_of(self.headline.series)

    @property
    def has_high(self) -> bool:
        return any(event.impact == IMPACT_HIGH for event in self.events)


def cluster_releases(events: Iterable[ReleaseEvent]) -> list[ReleaseCluster]:
    """Shluky po minutě, chronologicky; uvnitř headline první (stabilně k (čas, id))."""
    by_minute: dict[dt.datetime, list[ReleaseEvent]] = defaultdict(list)
    for event in sorted(events, key=lambda item: (item.ts, item.id)):
        if event.impact in QUALIFYING_IMPACTS:
            by_minute[event.minute].append(event)
    return [
        ReleaseCluster(ts=minute, events=tuple(sorted(members, key=release_rank)))
        for minute, members in sorted(by_minute.items())
    ]


def _number(value: Any) -> float | None:
    return float(value) if value is not None else None


def _forecast_text(raw: Any) -> str | None:
    if not isinstance(raw, dict):
        return None
    value = raw.get("forecast")
    text = str(value).strip() if value is not None else ""
    return text or None


def load_releases(engine: Engine, start: dt.datetime, end: dt.datetime) -> list[ReleaseEvent]:
    """USD scheduled události s FF dopadem High/Medium a `ts_event` v [start, end]."""
    stmt = (
        select(
            news_events.c.id,
            news_events.c.ts_event,
            news_events.c.title,
            news_events.c.forecast,
            news_events.c.actual,
            news_events.c.raw,
        )
        .where(news_events.c.kind == "scheduled")
        .where(news_events.c.title.like(f"{USD_PREFIX}%"))
        .where(news_events.c.ts_event >= start, news_events.c.ts_event <= end)
        .order_by(news_events.c.ts_event, news_events.c.id)
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).fetchall()
    events: list[ReleaseEvent] = []
    for row in rows:
        impact = impact_of(row.raw)
        if impact not in QUALIFYING_IMPACTS:
            continue
        ts = row.ts_event if row.ts_event.tzinfo else row.ts_event.replace(tzinfo=dt.UTC)
        events.append(
            ReleaseEvent(
                id=int(row.id),
                ts=ts.astimezone(dt.UTC),
                title=str(row.title),
                impact=impact,
                forecast=_number(row.forecast),
                actual=_number(row.actual),
                forecast_text=_forecast_text(row.raw),
            )
        )
    return events


def family_clusters(
    clusters: Sequence[ReleaseCluster], families: Iterable[str]
) -> list[ReleaseCluster]:
    """Shluky, jejichž headline patří do některé z `families`."""
    wanted = set(families)
    return [cluster for cluster in clusters if cluster.family in wanted]
