"""Měření upozornění na reakci trhu na zprávy: stará pravidla vs. nová (#1291, ADR-0043).

Nic nedetekuje živě ani nezapisuje: produkční PG jen čte (spojení v režimu
read-only), parquet bary `data/derived/{ES,NQ}/bars` jen čte.

* **Stará pravidla** (#295, `anomaly_job.py` do commitu 3f45db1): reakce jedné
  zprávy `ret_5` z `news_reactions`, jen nekontaminovaná okna, bucket
  symbol × kategorie × importance × surprise bucket × deferred, práh p90
  nearest-rank z ≥ 30 vzorků historie bucketu (řazené podle `ts_event`).
  Replay za 24.–25. 9. 2026 seděl 9/9 na log news-enginu (analýza #1291).
* **Nová pravidla**: skutečný `AnomalyJob` z `gexlens_news.anomaly_job` a
  `PreopenJob` z `gexlens_news.preopen_job` — shluky, významnost, výchylka,
  baseline, cooldown, etapy před otevřením i text jsou kód, který běží
  v produkci. Replay volá `run(now)` po krocích reaction_loop (300 s); zprávy
  vidí jen ty, které v `now` už byly v DB (`ts_ingested`). Náhrada je jen
  zdroj dat a stavu: zprávy se načtou jednou do paměti, bary se cachují per
  partice a stav předobchodních etap drží paměť (DB je jen pro čtení).
  Předobchodní job se přehrává zvlášť za posledních `--preopen-weeks` víkendů
  (mimo etapy nic nedělá, stačí kroky od T−4 h do otevření).
* **Kurátoři**: příznak `raw.curated` zapisuje Bluesky collector až od nasazení
  #1291. Pro historii jde emulovat souborem DID kurátorů (`--curated-dids`,
  jeden DID na řádek); bez něj nejsou sociální sítě významné vůbec.

Spuštění (z hostitele, PG publikované na 55432; URL se nikdy nevypisuje):
    uv run python scripts/measure_news_anomaly.py --days 14 --out report.md \\
        [--curated-dids dids.txt] [--end 2026-09-25T15:30] [--preopen-weeks 8]

URL: `--db`, jinak `GEXLENS_HOST_DATABASE_URL`, jinak sestavená z
`GEXLENS_PG_PASSWORD` (uživatel/DB `gexlens`, 127.0.0.1:55432).
"""

from __future__ import annotations

import argparse
import bisect
import datetime as dt
import os
import statistics
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine" / "src"))
sys.path.insert(0, str(ROOT / "news-engine" / "src"))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.engine import URL, Engine  # noqa: E402

from gexlens_engine.storage.sentiment import (  # noqa: E402
    news_events,
    news_reactions,
    reaction_computed_at,
    reaction_contaminated,
    reaction_deferred,
    reaction_ret,
)
from gexlens_news.anomaly_job import EVENT_COLUMNS, AnomalyJob, event_from_row  # noqa: E402
from gexlens_news.bars import BarsRepository  # noqa: E402
from gexlens_news.clusters import (  # noqa: E402
    ANOMALY_KIND,
    Cluster,
    ClusterEvent,
    build_clusters,
    cluster_ready_at,
    evaluate_clusters,
    floor_minute,
    is_extraordinary,
    is_significant,
)
from gexlens_news.model_stats import surprise_bucket  # noqa: E402
from gexlens_news.predictions import DEFAULT_PRIMARY_WINDOW_MIN  # noqa: E402
from gexlens_news.preopen import (  # noqa: E402
    STAGE_LEADS,
    STAGE_MAIN,
    STAGE_UPDATE,
    PreopenState,
    distinct_stories,
    follows_long_closure,
    is_key,
    upcoming_open,
)
from gexlens_news.preopen_job import PreopenJob  # noqa: E402
from gexlens_news.reactions import (  # noqa: E402
    Bar,
    measure_excursion,
    minute_of_day_et,
    tod_thresholds,
)

PRAGUE = ZoneInfo("Europe/Prague")
SYMBOLS = ("ES", "NQ")
STEP = dt.timedelta(seconds=300)  # reaction_interval_s (config.py)
# Staré pravidlo (#295)
OLD_WINDOW = 5
OLD_MIN_BUCKET_SAMPLES = 30
OLD_PERCENTILE = 0.90

#: Známé události (UTC) — makro releasy z FF kalendáře a jejich okolí
KNOWN_EVENTS: tuple[tuple[str, str], ...] = (
    ("Core PCE + GDP", "2026-08-26T12:30"),
    ("ISM Manufacturing", "2026-09-01T14:00"),
    ("ADP", "2026-09-02T12:15"),
    ("ISM Services", "2026-09-03T14:00"),
    ("NFP", "2026-09-04T12:30"),
    ("PPI", "2026-09-10T12:30"),
    ("CPI", "2026-09-11T12:30"),
    ("UoM prelim", "2026-09-11T14:00"),
    ("Retail Sales", "2026-09-16T12:30"),
    ("FOMC statement", "2026-09-16T18:00"),
    ("FOMC tisková konference", "2026-09-16T18:30"),
    ("US Flash PMI", "2026-09-23T13:45"),
)


def database_url(explicit: str | None) -> str | URL:
    if explicit:
        return explicit
    url = os.environ.get("GEXLENS_HOST_DATABASE_URL")
    if url:
        return url
    password = os.environ.get("GEXLENS_PG_PASSWORD")
    if not password:
        raise SystemExit("Chybí --db, GEXLENS_HOST_DATABASE_URL nebo GEXLENS_PG_PASSWORD")
    # URL.create heslo escapuje — speciální znaky ho nerozbijí
    return URL.create(
        "postgresql+psycopg",
        username="gexlens",
        password=password,
        host="127.0.0.1",
        port=55432,
        database="gexlens",
    )


def read_only_engine(url: str | URL) -> Engine:
    """Spojení, na kterém PG odmítne jakýkoli zápis."""
    return create_engine(url, connect_args={"options": "-c default_transaction_read_only=on"})


# ── Zdroje dat pro replay ──────────────────────────────────────────


class CachedBars(BarsRepository):
    """Stejné čtení partic jako v produkci, jen každá partice jednou."""

    def __init__(self, data_dir: Path) -> None:
        super().__init__(data_dir)
        self._days: dict[tuple[str, dt.date], list[Bar]] = {}
        self.partition_reads = 0

    def load_day(self, symbol: str, day: dt.date) -> list[Bar]:
        key = (symbol, day)
        if key not in self._days:
            self._days[key] = super().load_day(symbol, day)
            self.partition_reads += bool(self._days[key])
        return self._days[key]


class CountingBars(BarsRepository):
    """Bez cache (jako produkce), jen počítá čtené partice."""

    def __init__(self, data_dir: Path) -> None:
        super().__init__(data_dir)
        self.partition_reads = 0

    def load_day(self, symbol: str, day: dt.date) -> list[Bar]:
        if self._path(symbol, day).exists():
            self.partition_reads += 1
        return super().load_day(symbol, day)


@dataclass(frozen=True)
class StoredEvent:
    event: ClusterEvent
    ts_ingested: dt.datetime
    source: str


def load_all_events(engine: Engine, start: dt.datetime, end: dt.datetime) -> list[StoredEvent]:
    """Zprávy jako v jobu (`EVENT_COLUMNS`, `event_from_row`) + čas zápisu a zdroj."""
    stmt = (
        select(
            *EVENT_COLUMNS,
            news_events.c.ts_ingested,
            news_events.c.source,
            news_events.c.raw["did"].as_string().label("did"),
        )
        .where(news_events.c.ts_event >= start, news_events.c.ts_event <= end)
        .order_by(news_events.c.ts_event, news_events.c.id)
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).fetchall()
    return [
        StoredEvent(
            event=event_from_row(row),
            ts_ingested=_utc(row.ts_ingested),
            source=f"{row.source}|{row.did or ''}",
        )
        for row in rows
    ]


class ReplayJob(AnomalyJob):
    """Skutečný AnomalyJob; zprávy z paměti místo SELECTu, jinak beze změny."""

    def __init__(
        self,
        engine: Engine,
        bars: BarsRepository,
        stored: Sequence[StoredEvent],
        *,
        started_at: dt.datetime,
    ) -> None:
        super().__init__(engine, bars, started_at=started_at)
        self._stored = list(stored)
        self._times = [item.event.ts_event for item in self._stored]

    def load_events(
        self, start: dt.datetime, end: dt.datetime, now: dt.datetime
    ) -> list[ClusterEvent]:
        lo = bisect.bisect_left(self._times, start)
        hi = bisect.bisect_right(self._times, end)
        return [item.event for item in self._stored[lo:hi] if item.ts_ingested <= now]


class ReplayPreopen(PreopenJob):
    """Skutečný PreopenJob; zprávy z paměti a stav etap v paměti (DB jen čte)."""

    def __init__(
        self, engine: Engine, bars: BarsRepository, stored: Sequence[StoredEvent] | None
    ) -> None:
        super().__init__(engine, bars)
        self._stored = list(stored) if stored is not None else None
        self._times = [item.event.ts_event for item in self._stored or []]
        self._state: dict[str, Any] | None = None

    def load_events(
        self, start: dt.datetime, end: dt.datetime, now: dt.datetime
    ) -> list[ClusterEvent]:
        if self._stored is None:  # měření skutečného běhu: SELECT z PG
            return super().load_events(start, end, now)
        lo = bisect.bisect_left(self._times, start)
        hi = bisect.bisect_right(self._times, end)
        return [item.event for item in self._stored[lo:hi] if item.ts_ingested <= now]

    def load_state(self, opening: dt.datetime) -> PreopenState:
        return PreopenState.from_json(self._state, opening)

    def store_state(self, state: PreopenState) -> None:
        self._state = state.to_json()


def emulate_curated(stored: list[StoredEvent], dids: set[str]) -> tuple[list[StoredEvent], int]:
    """Historické posty kurátorů příznak nemají — doplní se podle DID."""
    if not dids:
        return stored, 0
    changed = 0
    result = []
    for item in stored:
        did = item.source.split("|", 1)[1]
        if item.event.kind == "social" and did in dids and not item.event.curated:
            item = replace(item, event=replace(item.event, curated=True))
            changed += 1
        result.append(item)
    return result, changed


# ── Stará pravidla (#295) ──────────────────────────────────────────


@dataclass(frozen=True)
class OldAlert:
    event_id: int
    symbol: str
    ts_event: dt.datetime
    computed_at: dt.datetime | None
    source: str
    importance: int
    category: str
    ret_bp: float
    threshold: float
    title: str


def old_rule_alerts(engine: Engine, start: dt.datetime, end: dt.datetime) -> list[OldAlert]:
    stmt = (
        select(
            news_reactions.c.event_id,
            news_reactions.c.symbol,
            reaction_ret(OLD_WINDOW).label("ret_bp"),
            reaction_deferred(OLD_WINDOW).label("deferred"),
            reaction_computed_at(OLD_WINDOW).label("computed_at"),
            news_events.c.ts_event,
            news_events.c.source,
            news_events.c.title,
            news_events.c.category,
            news_events.c.importance,
            news_events.c.surprise_z,
        )
        .join(news_events, news_events.c.id == news_reactions.c.event_id)
        .where(
            reaction_ret(OLD_WINDOW).is_not(None),
            reaction_contaminated(OLD_WINDOW).is_(False),
            news_events.c.category.is_not(None),
            news_events.c.importance.is_not(None),
            news_events.c.ts_event <= end,
        )
        .order_by(news_events.c.ts_event, news_events.c.id)
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).fetchall()
    history: dict[tuple[Any, ...], list[float]] = defaultdict(list)
    alerts: list[OldAlert] = []
    for row in rows:
        surprise = float(row.surprise_z) if row.surprise_z is not None else None
        key = (
            row.symbol,
            row.category,
            int(row.importance),
            surprise_bucket(surprise),
            bool(row.deferred),
        )
        bucket = history[key]
        ts_event = _utc(row.ts_event)
        value = abs(float(row.ret_bp))
        if ts_event >= start and len(bucket) >= OLD_MIN_BUCKET_SAMPLES:
            rank = max(0, min(len(bucket) - 1, round(OLD_PERCENTILE * (len(bucket) - 1))))
            threshold = bucket[rank]
            if value > threshold:
                alerts.append(
                    OldAlert(
                        event_id=int(row.event_id),
                        symbol=str(row.symbol),
                        ts_event=ts_event,
                        computed_at=_utc(row.computed_at) if row.computed_at else None,
                        source=str(row.source),
                        importance=int(row.importance),
                        category=str(row.category),
                        ret_bp=float(row.ret_bp),
                        threshold=threshold,
                        title=str(row.title),
                    )
                )
        bisect.insort(bucket, value)
    return alerts


# ── Replay nových pravidel ─────────────────────────────────────────


@dataclass(frozen=True)
class NewAlert:
    detected_at: dt.datetime
    payload: dict[str, Any]

    @property
    def ts_event(self) -> dt.datetime:
        return dt.datetime.fromisoformat(str(self.payload["ts_event"]))


def replay(
    job: ReplayJob, start: dt.datetime, end: dt.datetime
) -> tuple[list[NewAlert], list[float]]:
    """Kroky reaction_loop: reakce na shluky, jako v produkci."""
    alerts: list[NewAlert] = []
    durations: list[float] = []
    now = start
    while now <= end:
        began = time.perf_counter()
        payloads = job.run(now)
        durations.append(time.perf_counter() - began)
        alerts.extend(NewAlert(now, payload) for payload in payloads)
        now += STEP
    return alerts, durations


def replay_preopen(
    preopen: ReplayPreopen, openings: Sequence[dt.datetime]
) -> tuple[list[NewAlert], list[float]]:
    """Kroky reaction_loop od hlavní etapy do otevření — mimo ně job nic nedělá."""
    alerts: list[NewAlert] = []
    durations: list[float] = []
    first_lead = max(lead for _, lead in STAGE_LEADS)
    for opening in openings:
        now = opening - first_lead
        while now < opening:
            began = time.perf_counter()
            payloads = preopen.run(now)
            durations.append(time.perf_counter() - began)
            alerts.extend(NewAlert(now, payload) for payload in payloads)
            now += STEP
    return alerts, durations


@dataclass(frozen=True)
class PreopenCounts:
    """Zprávy za zavřený trh v čase etapy (ES): zásadní / jen významné / šum."""

    since: dt.datetime
    key: int
    other: int
    noise: int


def preopen_counts(preopen: ReplayPreopen, opening: dt.datetime, stage: str) -> PreopenCounts:
    """Stejné okno a funkce jako job (`closure`, `window_start`, `is_key`)."""
    at = opening - dict(STAGE_LEADS)[stage]
    closed_at, _ = preopen.closure("ES", opening, at)
    since = preopen.window_start(closed_at)
    events = preopen.load_events(since, at, at)
    significant = distinct_stories(event for event in events if is_significant(event))
    key = sum(1 for event in significant if is_key(event))
    return PreopenCounts(
        since=since,
        key=key,
        other=len(significant) - key,
        noise=sum(1 for event in events if not is_significant(event)),
    )


PreopenRow = tuple[dt.datetime, PreopenCounts, PreopenCounts]


def weekend_openings(start: dt.datetime, end: dt.datetime) -> list[dt.datetime]:
    """Otevření Globexu po víkendu v období (podle rozvrhu)."""
    openings: list[dt.datetime] = []
    cursor = start
    while True:
        opening = upcoming_open(cursor)
        if opening > end:
            return openings
        if follows_long_closure(opening):
            openings.append(opening)
        cursor = opening


def measure_real_run(
    url: str | URL, data_dir: Path, now: dt.datetime, preopen_at: dt.datetime | None
) -> dict[str, float]:
    """Skutečné joby nad PG a partičemi bez cache: stavba baseline a běžný běh.

    Baseline se staví jednou za seanci (a až u prvního shluku s významnou
    zprávou), běžný běh bez shluku barů vůbec nesahá — měří se proto zvlášť.
    """
    engine = read_only_engine(url)
    bars = CountingBars(data_dir)
    job = AnomalyJob(engine, bars, started_at=now - dt.timedelta(hours=1))
    began = time.perf_counter()
    for symbol in SYMBOLS:
        job.baseline(symbol, now)
    cold = time.perf_counter() - began
    cold_reads = bars.partition_reads
    began = time.perf_counter()
    job.run(now)
    warm = time.perf_counter() - began
    result = {
        "cold_s": cold,
        "cold_reads": float(cold_reads),
        "warm_s": warm,
        "warm_reads": float(bars.partition_reads - cold_reads),
    }
    if preopen_at is not None:
        # Předobchodní etapa nad PG a partičemi (stav etap v paměti — DB je jen pro čtení)
        preopen_bars = CountingBars(data_dir)
        began = time.perf_counter()
        ReplayPreopen(engine, preopen_bars, None).run(preopen_at)
        result["preopen_s"] = time.perf_counter() - began
        result["preopen_reads"] = float(preopen_bars.partition_reads)
    return result


# ── Známé události ─────────────────────────────────────────────────


@dataclass(frozen=True)
class KnownRow:
    name: str
    at: dt.datetime
    cluster: Cluster | None
    per_symbol: dict[str, str]


def evaluate_known(
    job: ReplayJob, bars: BarsRepository, new_alerts: Sequence[NewAlert]
) -> list[KnownRow]:
    fired = {(a.payload["symbol"], a.payload["ts_event"]) for a in new_alerts}
    rows: list[KnownRow] = []
    window = DEFAULT_PRIMARY_WINDOW_MIN
    for name, text in KNOWN_EVENTS:
        at = dt.datetime.fromisoformat(text).replace(tzinfo=dt.UTC)
        around = job.load_events(
            at - dt.timedelta(minutes=10), at + dt.timedelta(minutes=10), at + dt.timedelta(hours=1)
        )
        near = [c for c in build_clusters(around) if abs(c.start - at) <= dt.timedelta(minutes=3)]
        if not near:
            rows.append(KnownRow(name, at, None, {}))
            continue
        cluster = min(near, key=lambda c: abs(c.start - at))
        start = floor_minute(cluster.start)
        per_symbol: dict[str, str] = {}
        for symbol in SYMBOLS:
            symbol_bars = bars.load_range(
                symbol, start - dt.timedelta(minutes=70), start + dt.timedelta(minutes=10)
            )
            baseline = job.baseline(symbol, start)
            excursion = measure_excursion(symbol_bars, start, window)
            thresholds = tod_thresholds(baseline, minute_of_day_et(start)) if baseline else None
            if excursion is None or thresholds is None:
                per_symbol[symbol] = "neměřitelné"
                continue
            ready = cluster_ready_at(cluster, window)
            decision = evaluate_clusters(
                [cluster],
                symbol,
                symbol_bars,
                baseline,
                [],
                now=ready,
                not_before=ready - dt.timedelta(hours=1),
                window=window,
                tz=PRAGUE,
            )
            verdict = "ALERT" if decision.alerts else "—"
            assert bool(decision.alerts) == is_extraordinary(excursion, thresholds)
            if (symbol, cluster.start.isoformat()) in fired:
                verdict += " (v replayi)"
            per_symbol[symbol] = (
                f"{excursion.bp * excursion.direction:+.1f} bp / práh {thresholds.bp:.1f}; "
                f"z {excursion.z:.1f} / {thresholds.z:.1f} → {verdict}"
            )
        rows.append(KnownRow(name, at, cluster, per_symbol))
    return rows


# ── Report ─────────────────────────────────────────────────────────


def prague_day(ts: dt.datetime) -> dt.date:
    return ts.astimezone(PRAGUE).date()


def days_between(start: dt.datetime, end: dt.datetime) -> list[dt.date]:
    first, last = prague_day(start), prague_day(end)
    return [first + dt.timedelta(days=i) for i in range((last - first).days + 1)]


def md_table(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return lines


def render(
    *,
    start: dt.datetime,
    end: dt.datetime,
    old: Sequence[OldAlert],
    new: Sequence[NewAlert],
    known: Sequence[KnownRow],
    summaries: Sequence[NewAlert],
    preopen_rows: Sequence[PreopenRow],
    durations: Sequence[float],
    preopen_durations: Sequence[float],
    replay_s: float,
    real: dict[str, float],
    partition_reads: int,
    curated_emulated: int,
    stored: Sequence[StoredEvent],
) -> str:
    anomalies = [a for a in new if a.payload["kind"] == ANOMALY_KIND]
    in_period = [a for a in summaries if start <= a.detected_at <= end]
    old_count = Counter((prague_day(a.ts_event), a.symbol) for a in old)
    new_count = Counter((prague_day(a.ts_event), a.payload["symbol"]) for a in anomalies)
    sum_count = Counter((prague_day(a.detected_at), a.payload["symbol"]) for a in in_period)
    days = days_between(start, end)
    out = [
        "# #1291 — upozornění na reakci trhu na zprávy: stará vs. nová pravidla",
        "",
        f"Období: {start:%Y-%m-%d %H:%M} – {end:%Y-%m-%d %H:%M} UTC ({len(days)} dnů v Praze), "
        f"krok replaye {int(STEP.total_seconds())} s. Den = den zprávy (u předobchodního "
        "upozornění den odeslání) v Europe/Prague.",
        "",
        "## Počty per den × instrument",
        "",
    ]
    rows: list[list[Any]] = []
    for day in days:
        cells: list[Any] = [day.strftime("%a %d. %m.")]
        for symbol in SYMBOLS:
            cells += [
                old_count[(day, symbol)],
                new_count[(day, symbol)],
                sum_count[(day, symbol)],
            ]
        rows.append(cells)
    totals: list[Any] = ["**celkem**"]
    for symbol in SYMBOLS:
        totals += [
            sum(v for (d, s), v in old_count.items() if s == symbol),
            sum(v for (d, s), v in new_count.items() if s == symbol),
            sum(v for (d, s), v in sum_count.items() if s == symbol),
        ]
    rows.append(totals)
    out += md_table(
        [
            "den",
            "ES staré",
            "ES nové",
            "ES před otevřením",
            "NQ staré",
            "NQ nové",
            "NQ před otevřením",
        ],
        rows,
    )
    n_days = max(1, len(days))
    out += [
        "",
        f"Celkem staré **{len(old)}** (ES {sum(1 for a in old if a.symbol == 'ES')} / "
        f"NQ {sum(1 for a in old if a.symbol == 'NQ')}, {len(old) / n_days:.1f} za den), "
        f"nové reakce **{len(anomalies)}** ({len(anomalies) / n_days:.1f} za den), "
        f"předobchodní upozornění **{len(in_period)}**.",
        "",
        "Stará pravidla podle importance: "
        + ", ".join(f"{k}: {v}" for k, v in sorted(Counter(a.importance for a in old).items()))
        + "; podle zdroje: "
        + ", ".join(f"{k} {v}" for k, v in Counter(a.source for a in old).most_common()),
        "",
    ]
    period = [s for s in stored if start <= s.event.ts_event <= end]
    significant = [s for s in period if is_significant(s.event)]
    by_source = Counter(s.source.split("|", 1)[0] for s in significant)
    out += [
        f"Zprávy v období: {len(period)}, z toho významných {len(significant)} "
        f"({', '.join(f'{k} {v}' for k, v in by_source.most_common())}); "
        f"kurátorovaných sociálních postů doplněných podle DID: {curated_emulated}.",
        "",
        "## Doba běhu",
        "",
        f"- Replay: {len(durations)} běhů za {replay_s:.1f} s, běh medián "
        f"{statistics.median(durations) * 1000:.0f} ms, p95 "
        f"{sorted(durations)[int(0.95 * (len(durations) - 1))] * 1000:.0f} ms, max "
        f"{max(durations):.2f} s (max = stavba baseline při změně seance); "
        f"přečteno {partition_reads} partic (cache). Předobchodní job "
        f"({len(preopen_durations)} běhů od T−4 h do otevření): medián "
        f"{statistics.median(preopen_durations) * 1000:.1f} ms, max "
        f"{max(preopen_durations) * 1000:.0f} ms (etapa se čtením barů a úrovní).",
        f"- Skutečný job nad PG a daty bez cache (hostitel Windows): stavba baseline "
        f"{real['cold_s']:.2f} s ({real['cold_reads']:.0f} partic — 20 seancí ES+NQ, jednou "
        f"za seanci), běh s baseline v paměti {real['warm_s']:.2f} s "
        f"({real['warm_reads']:.0f} partic; bez shluku s významnou zprávou bary nečte). "
        f"V kontejneru přidá bind mount ~20 ms na partici (baseline "
        f"~+{real['cold_reads'] * 0.02:.1f} s).",
    ]
    if "preopen_s" in real:
        out.append(
            f"- Skutečná předobchodní etapa nad PG a daty bez cache: {real['preopen_s']:.2f} s "
            f"({real['preopen_reads']:.0f} partic barů + glob a čtení levels)."
        )
    out += [
        "",
        "## Známé události",
        "",
    ]
    known_rows = []
    for row in known:
        if row.cluster is None:
            known_rows.append([row.name, f"{row.at:%d. %m. %H:%M}", "—", "žádný shluk", "", ""])
            continue
        titles = "; ".join(e.title[:40] for e in row.cluster.significant[:3])
        known_rows.append(
            [
                row.name,
                f"{row.at:%d. %m. %H:%M}",
                f"{row.cluster.start:%H:%M:%S} ({len(row.cluster.significant)}/"
                f"{row.cluster.noise_count})",
                titles.replace("|", "/"),
                row.per_symbol.get("ES", ""),
                row.per_symbol.get("NQ", ""),
            ]
        )
    out += md_table(
        ["událost", "UTC", "shluk t0 (význ./ostatní)", "významné", "ES", "NQ"], known_rows
    )
    out += [
        "",
        "Hodnoty: výchylka do 5 min se znaménkem / práh p97 denní doby v bp; z / práh p97 z. "
        "„(v replayi)“ = upozornění vzniklo i v replayi (bez něj: mimo období nebo cooldown).",
        "",
    ]
    out += render_preopen(summaries, preopen_rows)
    out += [
        "## Nová upozornění na reakci — plné texty",
        "",
    ]
    for alert in sorted(anomalies, key=lambda a: (a.ts_event, a.payload["symbol"])):
        delay = (alert.detected_at - alert.ts_event).total_seconds() / 60
        out += [
            f"### {alert.payload['kind']} · {alert.payload['symbol']} · "
            f"{alert.ts_event.astimezone(PRAGUE):%a %d. %m. %H:%M:%S} Praha "
            f"(odešlo za {delay:.0f} min)",
            "",
            "```",
            str(alert.payload["message"]),
            "```",
            "",
        ]
    return "\n".join(out) + "\n"


def render_preopen(summaries: Sequence[NewAlert], preopen_rows: Sequence[PreopenRow]) -> list[str]:
    """Simulace předobchodních upozornění za víkendy (etapy, počty, texty)."""
    out = [
        "## Předobchodní upozornění — simulace víkendů",
        "",
        "Replay skutečného `PreopenJob` v krocích 300 s: hlavní souhrn 4 h a aktualizace "
        "15 min před otevřením Globexu, zprávy jen ty, které v daném okamžiku byly v DB. "
        "Počty = zprávy za zavřený trh, které byly v DB v čase hlavního souhrnu / aktualizace "
        "(ES, tatáž story jednou): zásadní jdou do výčtu a sklonu, ostatní významné "
        "(varianta B) a šum jen počtem.",
        "",
    ]
    rows = []
    for opening, main, last in preopen_rows:
        sent = [a for a in summaries if a.payload["ts_event"] == opening.isoformat()]
        rows.append(
            [
                f"{opening.astimezone(PRAGUE):%a %d. %m. %H:%M}",
                f"{last.since.astimezone(PRAGUE):%a %H:%M}",
                ", ".join(
                    f"{a.payload['symbol']} {a.detected_at.astimezone(PRAGUE):%H:%M}" for a in sent
                )
                or "—",
                len(sent),
                f"{main.key} / {last.key}",
                f"{main.other} / {last.other}",
                f"{main.noise} / {last.noise}",
            ]
        )
    out += md_table(
        [
            "otevření (Praha)",
            "zprávy od",
            "odesláno (instrument čas)",
            "zpráv",
            "zásadní T−4 h / T−15 min",
            "jen významné",
            "ostatní",
        ],
        rows,
    )

    def without_key(counts: Sequence[tuple[dt.datetime, PreopenCounts]]) -> list[str]:
        return [
            f"{opening.astimezone(PRAGUE):%d. %m.}"
            for opening, count in counts
            if count.key == 0 and count.other > 0
        ]

    at_main = without_key([(opening, main) for opening, main, _ in preopen_rows])
    at_update = without_key([(opening, last) for opening, _, last in preopen_rows])
    out += [
        "",
        "Bez zásadní zprávy, ale s významnou podle varianty B (etapa se neposílá, "
        f"ADR-0043 bod 5): v čase hlavního souhrnu {len(at_main)} z {len(preopen_rows)} "
        f"víkendů ({', '.join(at_main) or '—'}), v čase aktualizace {len(at_update)} "
        f"({', '.join(at_update) or '—'}).",
        "",
    ]
    for alert in sorted(summaries, key=lambda a: (a.detected_at, a.payload["symbol"])):
        out += [
            f"### {alert.payload['symbol']} · odesláno "
            f"{alert.detected_at.astimezone(PRAGUE):%a %d. %m. %H:%M} Praha",
            "",
            "```",
            str(alert.payload["message"]),
            "```",
            "",
        ]
    return out


def _utc(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--db", default=None, help="SQLAlchemy URL (jinak z prostředí)")
    parser.add_argument("--data-dir", default=os.environ.get("GEXLENS_DATA_DIR", "data"))
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--end", default=None, help="konec období, ISO UTC (výchozí teď)")
    parser.add_argument("--curated-dids", default=None, help="soubor s DID kurátorů")
    parser.add_argument(
        "--preopen-weeks", type=int, default=8, help="kolik posledních víkendů přehrát"
    )
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    url = database_url(args.db)
    engine = read_only_engine(url)
    data_dir = Path(args.data_dir)
    now = dt.datetime.now(dt.UTC)
    end = (
        dt.datetime.fromisoformat(args.end).replace(tzinfo=dt.UTC)
        if args.end
        else now.replace(second=0, microsecond=0)
    )
    end -= dt.timedelta(minutes=end.minute % 5)
    start = end - dt.timedelta(days=args.days)

    known_first = min(dt.datetime.fromisoformat(t).replace(tzinfo=dt.UTC) for _, t in KNOWN_EVENTS)
    openings = weekend_openings(end - dt.timedelta(weeks=args.preopen_weeks), end)
    first = min([start, known_first, *openings])
    stored = load_all_events(engine, first - dt.timedelta(days=5), end)
    dids: set[str] = set()
    if args.curated_dids:
        dids = {
            line.strip()
            for line in Path(args.curated_dids).read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
    stored, emulated = emulate_curated(stored, dids)
    print(f"Zprávy: {len(stored)} (kurátor doplněn u {emulated})", flush=True)

    began = time.perf_counter()
    old = old_rule_alerts(engine, start, end)
    print(
        f"Stará pravidla: {len(old)} upozornění ({time.perf_counter() - began:.1f} s)", flush=True
    )

    bars = CachedBars(data_dir)
    job = ReplayJob(engine, bars, stored, started_at=start)
    began = time.perf_counter()
    new, durations = replay(job, start, end)
    replay_s = time.perf_counter() - began
    print(f"Nová pravidla: {len(new)} upozornění, replay {replay_s:.1f} s", flush=True)
    preopen = ReplayPreopen(engine, bars, stored)
    summaries, preopen_durations = replay_preopen(preopen, openings)
    preopen_rows = [
        (
            opening,
            preopen_counts(preopen, opening, STAGE_MAIN),
            preopen_counts(preopen, opening, STAGE_UPDATE),
        )
        for opening in openings
    ]
    print(f"Předobchodní: {len(summaries)} upozornění za {len(openings)} víkendů", flush=True)

    known = evaluate_known(job, bars, new)
    preopen_at = openings[-1] - dt.timedelta(hours=4) if openings else None
    real = measure_real_run(url, data_dir, now, preopen_at)
    report = render(
        start=start,
        end=end,
        old=old,
        new=new,
        known=known,
        summaries=summaries,
        preopen_rows=preopen_rows,
        durations=durations,
        preopen_durations=preopen_durations,
        replay_s=replay_s,
        real=real,
        partition_reads=bars.partition_reads,
        curated_emulated=emulated,
        stored=stored,
    )
    Path(args.out).write_text(report, encoding="utf-8")
    print(f"Report: {args.out}")


if __name__ == "__main__":
    main()
