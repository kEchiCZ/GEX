"""Shluky zpráv a upozornění na mimořádnou reakci trhu (#1291, ADR-0043) — čisté funkce.

Pohyb ze stejné chvíle nejde přisoudit jednotlivé zprávě: v běžné minutě vyjde
několik titulků, většinou šum. Proto se hodnotí **shluk** zpráv a hlásí se
jen tehdy, když v něm je aspoň jedna **významná** zpráva a trh se zároveň
pohnul mimořádně. Jedno upozornění = shluk × instrument; ES a NQ se
rozhodují úplně zvlášť (vlastní bary, baseline, práh, cooldown).

* **Významná zpráva** (`is_significant`, rozhodnutí uživatele 25. 9. 2026,
  varianta B): FF kalendář s impactem High/Medium podle surového payloadu
  (`raw.impact`), headline a broker s importance ≥ 2 mimo kategorii EARNINGS,
  sociální sítě jen od kurátorů (#578) s importance ≥ 2. U scheduled se
  nebere `importance`: pravidlový klasifikátor ji přepisuje regexem nad
  titulkem („USD PPI m/m" High → 1, „FOMC Member Speaks" Low → 3).
* **Shluk** se kotví na první významné zprávě `t0` a bere zprávy do
  `t0 + CLUSTER_SPAN`. Šum shluk nezaloží ani neprodlouží (řetězení přes
  všechny zprávy dělalo shluky o tisících zpráv), jen se počítá.
* **Mimořádný pohyb** měří `reactions.measure_excursion` od celé minuty `t0`
  (bar má čas začátku minuty) a porovnává s baseline denní doby.
* **Zavřený trh** (rozhodnutí uživatele 25. 9., Q2): zprávy za zavřený trh
  se jednotlivě neohlašují — výchylka potřebuje bar minuty před zprávou.
  Víkend (a svátek, který zkrátí páteční seanci) pokryje předobchodní
  upozornění (`preopen.py`), denní pauza se nehlásí vůbec.

Žádná pravděpodobnost se nevymýšlí: text nese naměřenou výchylku a hranici
obvyklého pohybu v bp.
"""

import bisect
import datetime as dt
import html
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from gexlens_engine.compute.marketclock import is_market_closed
from gexlens_news.reactions import (
    TOD_QUANTILE,
    Bar,
    Excursion,
    Thresholds,
    measure_excursion,
    minute_of_day_et,
    tod_thresholds,
)

#: Zprávy do tolika po první významné patří do jejího shluku
CLUSTER_SPAN = dt.timedelta(minutes=2)
#: Upozornění na týž instrument do 15 min od jiného (oběma směry) se potlačí —
#: sousední shluky měří týž pohyb a pozdní zpráva může kotvu posunout dřív
COOLDOWN = dt.timedelta(minutes=15)
#: Jak starý shluk (nebo otevření trhu) se ještě vyhodnocuje; zároveň chrání
#: před dopočtem historie (#744) — starší pohyby trader už viděl
LOOKBACK = dt.timedelta(minutes=30)
#: Rezerva na zápis finálního baru (engine ho píše až v další minutě)
BAR_SETTLE = dt.timedelta(minutes=2)

SIGNIFICANT_MIN_IMPORTANCE = 2
#: FF impact (`raw.impact`), malými písmeny
SCHEDULED_SIGNIFICANT_IMPACTS = frozenset({"high", "medium"})
#: Varianta B (#1291 Q1): earnings a přepisy hovorů nejsou významné — přes
#: polovinu headline s importance ≥ 2 tvořily firemní výsledky bez vlivu na index
EXCLUDED_CATEGORIES = frozenset({"EARNINGS"})
SCHEDULED_KIND = "scheduled"
SOCIAL_KIND = "social"

#: Kolik významných zpráv text vyjmenuje; zbytek jen počtem
MAX_LISTED = 5
TITLE_MAX_CHARS = 120

ANOMALY_KIND = "news_anomaly"


@dataclass(frozen=True)
class ClusterEvent:
    """Zpráva z `news_events` v rozsahu, který shlukování potřebuje."""

    id: int
    ts_event: dt.datetime
    kind: str
    title: str
    importance: int | None = None
    category: str | None = None
    #: FF impact z `raw.impact` (jen scheduled)
    ff_impact: str | None = None
    #: Autor je kurátor (#578) — `raw.curated`, zapisuje Bluesky collector
    curated: bool = False
    #: Klasifikovaný směr (`sentiment_dir`: +1 / −1 / 0, None = neklasifikováno)
    direction: int | None = None


@dataclass(frozen=True)
class Cluster:
    """Shluk kotvený na první významné zprávě."""

    start: dt.datetime
    #: Významné zprávy v pořadí významnosti (`significance_rank`)
    significant: tuple[ClusterEvent, ...]
    #: Ostatní zprávy v [start, start + CLUSTER_SPAN]
    noise_count: int


def is_significant(event: ClusterEvent) -> bool:
    """Jediné místo s definicí významné zprávy (#1291, varianta B)."""
    if event.kind == SCHEDULED_KIND:
        return (event.ff_impact or "").strip().lower() in SCHEDULED_SIGNIFICANT_IMPACTS
    if (event.importance or 0) < SIGNIFICANT_MIN_IMPORTANCE:
        return False
    if event.kind == SOCIAL_KIND:
        return event.curated
    return event.category not in EXCLUDED_CATEGORIES


def significance_tier(event: ClusterEvent) -> int:
    """Stupeň významnosti: 0 scheduled High, 1 Medium, 2 importance 3, 3 importance 2."""
    if event.kind == SCHEDULED_KIND:
        return 0 if (event.ff_impact or "").strip().lower() == "high" else 1
    return 2 if (event.importance or 0) >= 3 else 3


def significance_rank(event: ClusterEvent) -> tuple[int, dt.datetime, int]:
    """Řazení: scheduled High > Medium > importance 3 > importance 2, pak čas a id."""
    return (significance_tier(event), event.ts_event, event.id)


def build_clusters(
    events: Sequence[ClusterEvent], *, span: dt.timedelta = CLUSTER_SPAN
) -> list[Cluster]:
    """Shluky kotvené na významných zprávách; šum se jen přičte k počtu.

    Významná zpráva později než `span` po kotvě založí nový shluk. Shluk jen
    ze šumu nevznikne — neohlašuje se (rozhodnutí uživatele 25. 9. 2026).
    """
    ordered = sorted(events, key=lambda event: (event.ts_event, event.id))
    groups: list[list[ClusterEvent]] = []
    anchor: dt.datetime | None = None
    for event in ordered:
        if not is_significant(event):
            continue
        if anchor is None or event.ts_event - anchor > span:
            anchor = event.ts_event
            groups.append([])
        groups[-1].append(event)
    times = [event.ts_event for event in ordered]
    clusters: list[Cluster] = []
    for group in groups:
        start = group[0].ts_event
        in_span = bisect.bisect_right(times, start + span) - bisect.bisect_left(times, start)
        clusters.append(
            Cluster(
                start=start,
                significant=tuple(sorted(group, key=significance_rank)),
                noise_count=in_span - len(group),
            )
        )
    return clusters


def floor_minute(ts: dt.datetime) -> dt.datetime:
    """Začátek minuty — bar nese čas začátku, zpráva ve 12:30:03 se měří od 12:30."""
    return ts.replace(second=0, microsecond=0)


def in_cooldown(
    start: dt.datetime, announced: Sequence[dt.datetime], cooldown: dt.timedelta = COOLDOWN
) -> bool:
    """Leží start do `cooldown` od ohlášeného shluku téhož instrumentu (oběma směry)?"""
    return any(abs(start - other) < cooldown for other in announced)


def is_extraordinary(excursion: Excursion, thresholds: Thresholds) -> bool:
    """Nad hranicí denní doby v bp I po normalizaci volatilitou poslední hodiny.

    Jen bp by v rozjetém dni hlásilo každý shluk, jen z zase drobnost po
    mrtvé hodině — obě podmínky naráz dávají zhruba polovinu upozornění.
    """
    return excursion.bp > thresholds.bp and excursion.z > thresholds.z


def clean_title(title: str, max_chars: int = TITLE_MAX_CHARS) -> str:
    """Titulek na jeden řádek: HTML entity zdroje (`S&amp;P`) pryč, ořez s „…“."""
    text = " ".join(html.unescape(title).split())
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


def counts_line(rest: int, noise_count: int) -> str | None:
    """Řádek „další významné: k · ostatní zprávy: n“; nulové části se vynechají."""
    counts = []
    if rest > 0:
        counts.append(f"další významné: {rest}")
    if noise_count > 0:
        counts.append(f"ostatní zprávy: {noise_count}")
    return " · ".join(counts) or None


def _listing(significant: Sequence[ClusterEvent], noise_count: int) -> list[str]:
    """Odrážky významných zpráv (max MAX_LISTED) a řádek s počty zbytku."""
    lines = [f"• {clean_title(event.title)}" for event in significant[:MAX_LISTED]]
    counts = counts_line(len(significant) - MAX_LISTED, noise_count)
    return [*lines, counts] if counts else lines


def _signed_bp(value: float) -> str:
    """Šipka a bp se znaménkem po zaokrouhlení („↓ -19 bp“, „→ 0 bp“, ne „↓ -0 bp“)."""
    rounded = round(value)
    if rounded == 0:
        return "→ 0 bp"
    return f"{'↑' if rounded > 0 else '↓'} {rounded:+d} bp"


def build_alert(
    cluster: Cluster,
    symbol: str,
    excursion: Excursion,
    thresholds: Thresholds,
    *,
    window: int,
    now: dt.datetime,
    tz: ZoneInfo,
) -> dict[str, Any]:
    """Payload `news_anomaly` pro zvonek a Telegram (kanál `alerts`).

    `ts` = čas detekce v unix sekundách (kontrakt všech alertů), `ts_event`
    = začátek shluku a `event_ids` = významné zprávy v pořadí významnosti
    (pro proklik na graf, #1290).
    """
    signed = excursion.bp * excursion.direction
    head = (
        f"Reakce {symbol} na zprávy z {cluster.start.astimezone(tz):%H:%M}: "
        f"{_signed_bp(signed)} za {window} min "
        f"({round(TOD_QUANTILE * 100)} % výchylek v tuto denní dobu do {thresholds.bp:.0f} bp)"
    )
    return {
        "kind": ANOMALY_KIND,
        "symbol": symbol,
        "message": "\n".join([head, *_listing(cluster.significant, cluster.noise_count)]),
        "ts": int(now.timestamp()),
        "ts_event": cluster.start.isoformat(),
        "event_ids": [event.id for event in cluster.significant],
    }


def cluster_ready_at(cluster: Cluster, window: int) -> dt.datetime:
    """Kdy je okno shluku uzavřené a jeho bary zapsané."""
    return floor_minute(cluster.start) + dt.timedelta(minutes=window) + BAR_SETTLE


@dataclass
class ClusterOutcome:
    """Výsledek jednoho vyhodnocení shluků pro jeden instrument."""

    alerts: list[dict[str, Any]] = field(default_factory=list)
    #: Starty nově ohlášených shluků — job si je přidá do cooldownu
    announced: list[dt.datetime] = field(default_factory=list)
    #: Shluky při otevřeném trhu, které nešly změřit (chybí bary nebo baseline)
    unmeasurable: int = 0


def evaluate_clusters(
    clusters: Sequence[Cluster],
    symbol: str,
    bars: Sequence[Bar],
    baseline: Mapping[int, Sequence[Excursion]] | None,
    announced: Sequence[dt.datetime],
    *,
    now: dt.datetime,
    not_before: dt.datetime,
    window: int,
    tz: ZoneInfo,
) -> ClusterOutcome:
    """Rozhodne, které shluky ohlásit na instrumentu `symbol`.

    Shluk se hodnotí, když je hotový (`cluster_ready_at` ≤ now), hotový až
    po `not_before` (start procesu — po restartu žádné duplicity) a není
    starší než LOOKBACK. Neohlášený shluk se hodnotí znovu každý běh, dokud
    nevyprší — pozdě dorazivší významná zpráva ho může doplnit. Zavřený trh
    v čase shluku se neměří a nepočítá mezi neměřitelné (víkend pokryje
    předobchodní upozornění, denní pauza se nehlásí).
    """
    outcome = ClusterOutcome()
    for cluster in sorted(clusters, key=lambda item: item.start):
        ready = cluster_ready_at(cluster, window)
        if ready > now or ready < not_before or cluster.start < now - LOOKBACK:
            continue
        if in_cooldown(cluster.start, [*announced, *outcome.announced]):
            continue
        start = floor_minute(cluster.start)
        excursion = measure_excursion(bars, start, window)
        thresholds = tod_thresholds(baseline, minute_of_day_et(start)) if baseline else None
        if excursion is None or thresholds is None:
            if not is_market_closed(start):
                outcome.unmeasurable += 1
            continue
        if not is_extraordinary(excursion, thresholds):
            continue
        outcome.announced.append(cluster.start)
        outcome.alerts.append(
            build_alert(cluster, symbol, excursion, thresholds, window=window, now=now, tz=tz)
        )
    return outcome
