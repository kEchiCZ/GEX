"""Reklasifikace historie zpráv pravidlovým klasifikátorem v2 (#1293, ADR-0045).

Dvě fáze nad produkční PG (dialektově neutrální — v testech SQLite):

1. **Klasifikace** (`classify`): eventy s `news_events.sentiment_source = 'rule'`
   se klasifikují znovu toutéž funkcí jako živý job (`classification_job.
   classify_row`: titulek, feed ze surového payloadu, FF impact podle měny).
   Zapisuje se **jen rozdíl** proti denormalizaci: nová verze
   v `news_classifications` (version = max+1 přes všechny zdroje, source
   `rule`, směr a síla z poslední pravidlové verze — směr se ve v2 nemění)
   a UPDATE `news_events.category/importance`. LLM, ruční korekce a stín
   ngram zůstávají beze změny. Historie verzí se nemaže (S11).
   **Kurátoři Bluesky:** příznak `raw.curated` zapisuje collector až od #1291
   (25. 9. 2026). Starší post je kurátorovaný, když jeho autor (`raw.did`) je
   v aktuálním seznamu kurátorů — `news_bluesky_authors` v `settings`
   + `GEXLENS_NEWS_BLUESKY_CURATED_AUTHORS`, handly přes resolveHandle jako
   collector — nebo už má post s příznakem. Report vypíše počty
   i nepřeložené handly.
2. **Kontaminace** (`contamination`): `news_reactions.cont_{1,5,15,60}` znovu
   pravidlem K1 (`reactions.contaminates`, `window_contaminated`) nad
   klasifikací po fázi 1 — kontaminuje jen jiný event s importance ≥ 2 a jinou
   kategorií, ve stejném rozsahu jako `ReactionJob._contaminating`
   (61 min po zprávě). U deferred reakcí začíná okno prvním obchodovaným
   barem — ten se čte z archivu barů (`data/derived/{ES,NQ}/bars`, jen sloupec
   `ts_min`). Denní okna kontaminaci nemají (vždy False), nemění se.

**Dry-run** (`--dry-run`) nic nezapisuje: PG spojení je `read only`, obě fáze
se spočtou v paměti (kontaminace nad klasifikací po fázi 1) a vypíše se matice
změn. Ostrý běh zapisuje po dávkách (`--batch`, výchozí 5 000) v transakci na
dávku. **Idempotence:** zapisuje se jen rozdíl, druhý běh má 0 změn.

**Rollback** (`--rollback RUN_AT`): eventům, jejichž poslední ne-stínová
verze pochází z běhu `RUN_AT` (čas vypsaný ostrým během, `created_at` jeho
verzí), zapíše novou pravidlovou verzi s hodnotami poslední pravidlové verze
před během a obnoví denormalizaci. Opakovaný rollback nic nemění. Kontaminaci
po rollbacku přepočte `--phase contamination` (s kódem, který platí).

Po ostrém běhu (ADMIN-MANUAL, kap. reklasifikace): restart news-engine
(noční přepočet `news_model_stats` + gate proběhne v první smyčce), retro
přepočet SentIndexu `python -m gexlens_news recompute-sentindex --from …`.

Spuštění (produkce; kontejner má balíky, skript i `data/`):
    docker compose run --rm news-engine python scripts/reclassify_news_rules.py \\
        --dry-run --out /app/data/reports/reclass-dryrun.txt
    docker compose run --rm news-engine python scripts/reclassify_news_rules.py
    docker compose run --rm news-engine python scripts/reclassify_news_rules.py \\
        --rollback 2026-09-27T10:00:00+00:00

URL: `--db`, jinak `GEXLENS_NEWS_DATABASE_URL`, `GEXLENS_HOST_DATABASE_URL`,
nebo sestavená z `GEXLENS_PG_PASSWORD` (127.0.0.1:55432). Nikdy se nevypisuje.
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import datetime as dt
import os
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Lokálně z repa; v kontejneru je balík nainstalovaný a cesty neexistují
for _sub in ("engine", "news-engine"):
    _path = Path(__file__).resolve().parents[1] / _sub / "src"
    if _path.is_dir():
        sys.path.insert(0, str(_path))

import pyarrow.parquet as pq  # noqa: E402
from sqlalchemy import bindparam, create_engine, func, insert, select, update  # noqa: E402
from sqlalchemy.engine import URL, Connection, Engine  # noqa: E402

from gexlens_engine.compute.news_significance import is_key, significance_tier  # noqa: E402
from gexlens_engine.storage.sentiment import (  # noqa: E402
    news_classifications,
    news_events,
    news_reactions,
)
from gexlens_news.bars import BARS_SUBDIR  # noqa: E402
from gexlens_news.classification_job import (  # noqa: E402
    RAW_COLUMNS,
    RULE_SOURCE,
    classify_row,
    row_feed,
)
from gexlens_news.http import make_fetcher  # noqa: E402
from gexlens_news.reaction_job import (  # noqa: E402
    CLOSURE_LOOKAHEAD_DAYS,
    CONTAMINATION_MIN_IMPORTANCE,
)
from gexlens_news.reactions import (  # noqa: E402
    DEFAULT_WINDOWS,
    contaminates,
    window_contaminated,
)
from gexlens_news.user_sources import (  # noqa: E402
    SETTING_BLUESKY_AUTHORS,
    BlueskyAuthorResolver,
    read_list_setting,
)

DEFAULT_BATCH = 5000
#: Stínové verze (ngram #740) nikdy nedenormalizují — rollback je přeskakuje
SHADOW_SOURCES = ("ngram",)
#: Rozsah kontaminujících eventů po zprávě — jako `ReactionJob._contaminating`
CONTAMINATION_SPAN = dt.timedelta(minutes=max(DEFAULT_WINDOWS) + 1)
#: Okno objemů „posledních dní“ v souhrnu dry-runu
RECENT_DAYS = 28
SYMBOLS = ("ES", "NQ")


def _utc(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


# ── Fáze 1: klasifikace ────────────────────────────────────────────


@dataclass(frozen=True)
class Reclass:
    """Výsledek v2 pro jeden pravidlový event (stará a nová denormalizace)."""

    event_id: int
    ts_event: dt.datetime
    source: str
    feed: str
    kind: str
    title: str
    old_category: str | None
    old_importance: int | None
    new_category: str
    new_importance: int
    reason: str

    @property
    def changed(self) -> bool:
        return (self.old_category, self.old_importance) != (self.new_category, self.new_importance)


def _rule_batches(engine: Engine, batch: int) -> Iterator[list[Any]]:
    """Pravidlové eventy po dávkách podle id (keyset — stabilní i při zápisu).

    Každá dávka vlastním spojením: čtení nedrží zámek přes zápis dávky (SQLite).
    """
    last_id = 0
    while True:
        with engine.connect() as conn:
            rows = list(
                conn.execute(
                    select(
                        news_events.c.id,
                        news_events.c.ts_event,
                        news_events.c.title,
                        news_events.c.source,
                        news_events.c.kind,
                        news_events.c.surprise_z,
                        news_events.c.category,
                        news_events.c.importance,
                        *RAW_COLUMNS,
                    )
                    .where(
                        news_events.c.sentiment_source == RULE_SOURCE, news_events.c.id > last_id
                    )
                    .order_by(news_events.c.id)
                    .limit(batch)
                ).fetchall()
            )
        if not rows:
            return
        yield rows
        last_id = int(rows[-1].id)


@dataclass(frozen=True)
class Curated:
    """Kurátoři Bluesky pro historii (DID) a odkud se vzali — do reportu."""

    dids: frozenset[str] = frozenset()
    flagged: int = 0
    listed: int = 0
    unresolved: tuple[str, ...] = ()


#: handle/DID → (DID, nepřeložené položky)
Resolver = Callable[[Sequence[str]], tuple[frozenset[str], list[str]]]


def resolve_authors(authors: Sequence[str]) -> tuple[frozenset[str], list[str]]:
    """Handly kurátorů → DID stejně jako collector (`BlueskyAuthorResolver`, síť)."""

    async def _resolve() -> tuple[frozenset[str], list[str]]:
        fetcher = make_fetcher()
        resolver = BlueskyAuthorResolver(fetcher)
        dids: set[str] = set()
        unresolved: list[str] = []
        try:
            for author in authors:
                found = await resolver.resolve([author])
                if found:
                    dids |= found
                else:
                    unresolved.append(author)
        finally:
            await fetcher.client.aclose()
        return frozenset(dids), unresolved

    return asyncio.run(_resolve()) if authors else (frozenset(), [])


def load_curated(engine: Engine, env_authors: str, resolve: Resolver) -> Curated:
    """DID kurátorů: posty s příznakem `raw.curated` ∪ aktuální seznam kurátorů.

    Seznam = `news_bluesky_authors` v `settings` (aktivní položky, jako
    collector) + env `GEXLENS_NEWS_BLUESKY_CURATED_AUTHORS`. Jen čtení.
    """
    with engine.connect() as conn:
        flagged = {
            str(did)
            for (did,) in conn.execute(
                select(news_events.c.raw["did"].as_string())
                .where(
                    news_events.c.source == "bluesky",
                    news_events.c.raw["curated"].as_boolean().is_(True),
                )
                .distinct()
            )
            if did
        }
    authors = [item.strip() for item in env_authors.split(",") if item.strip()]
    authors += read_list_setting(engine, SETTING_BLUESKY_AUTHORS)
    listed, unresolved = resolve(authors)
    return Curated(frozenset(flagged | listed), len(flagged), len(listed), tuple(unresolved))


def reclassify_rows(
    rows: Iterable[Any], curated_dids: frozenset[str] = frozenset()
) -> list[Reclass]:
    """Klasifikace v2 řádků se sloupci `_rule_batches` (čistá funkce)."""
    out: list[Reclass] = []
    for row in rows:
        result = classify_row(row, curated_dids)
        out.append(
            Reclass(
                event_id=int(row.id),
                ts_event=_utc(row.ts_event),
                source=str(row.source),
                feed=row_feed(row, curated_dids),
                kind=str(row.kind),
                title=str(row.title or ""),
                old_category=row.category,
                old_importance=int(row.importance) if row.importance is not None else None,
                new_category=result.category,
                new_importance=result.importance,
                reason=result.reason,
            )
        )
    return out


def _latest_rule_versions(conn: Connection, ids: Sequence[int]) -> dict[int, Any]:
    """Poslední pravidlová verze per event (směr a síla pro novou verzi)."""
    latest: dict[int, Any] = {}
    for row in conn.execute(
        select(news_classifications).where(
            news_classifications.c.event_id.in_(ids),
            news_classifications.c.source == RULE_SOURCE,
        )
    ):
        current = latest.get(int(row.event_id))
        if current is None or row.version > current.version:
            latest[int(row.event_id)] = row
    return latest


def _max_versions(conn: Connection, ids: Sequence[int]) -> dict[int, int]:
    return {
        int(event_id): int(version)
        for event_id, version in conn.execute(
            select(news_classifications.c.event_id, func.max(news_classifications.c.version))
            .where(news_classifications.c.event_id.in_(ids))
            .group_by(news_classifications.c.event_id)
        )
    }


def _denormalize(conn: Connection, values: Sequence[dict[str, object]]) -> None:
    if values:
        conn.execute(
            update(news_events)
            .where(news_events.c.id == bindparam("b_id"))
            .values(category=bindparam("b_category"), importance=bindparam("b_importance")),
            list(values),
        )


def write_changes(conn: Connection, changes: Sequence[Reclass], run_at: dt.datetime) -> int:
    """Nová pravidlová verze + denormalizace pro změněné eventy jedné dávky."""
    if not changes:
        return 0
    ids = [change.event_id for change in changes]
    versions = _max_versions(conn, ids)
    latest = _latest_rule_versions(conn, ids)
    rows: list[dict[str, object]] = []
    for change in changes:
        previous = latest.get(change.event_id)
        rows.append(
            {
                "event_id": change.event_id,
                "version": versions.get(change.event_id, 0) + 1,
                "source": RULE_SOURCE,
                "category": change.new_category,
                "importance": change.new_importance,
                # Směr ve v2 beze změny — z poslední pravidlové verze
                "direction": int(previous.direction) if previous is not None else 0,
                "strength": float(previous.strength) if previous is not None else 0.0,
                "created_at": run_at,
            }
        )
    conn.execute(insert(news_classifications), rows)
    _denormalize(
        conn,
        [
            {
                "b_id": change.event_id,
                "b_category": change.new_category,
                "b_importance": change.new_importance,
            }
            for change in changes
        ],
    )
    return len(changes)


def run_classification(
    engine: Engine,
    *,
    dry_run: bool,
    batch: int,
    run_at: dt.datetime,
    log: Any,
    curated_dids: frozenset[str] = frozenset(),
) -> list[Reclass]:
    """Fáze 1; vrací výsledky všech pravidlových eventů (i nezměněných)."""
    results: list[Reclass] = []
    written = 0
    began = time.perf_counter()
    for rows in _rule_batches(engine, batch):
        evaluated = reclassify_rows(rows, curated_dids)
        results.extend(evaluated)
        if not dry_run:
            with engine.begin() as writer:
                written += write_changes(
                    writer, [item for item in evaluated if item.changed], run_at
                )
        log(
            f"  klasifikace: {len(results)} eventů, změn {sum(r.changed for r in results)}, "
            f"zapsáno {written} ({time.perf_counter() - began:.0f} s)"
        )
    return results


# ── Rollback ───────────────────────────────────────────────────────


def rollback(engine: Engine, run_at: dt.datetime, now: dt.datetime, *, batch: int) -> int:
    """Obnoví klasifikaci před během `run_at` novou verzí; vrací počet eventů."""
    with engine.connect() as conn:
        run_ids = sorted(
            {
                int(row.event_id)
                for row in conn.execute(
                    select(news_classifications.c.event_id).where(
                        news_classifications.c.created_at == run_at,
                        news_classifications.c.source == RULE_SOURCE,
                    )
                )
            }
        )
    restored = 0
    for start in range(0, len(run_ids), batch):
        ids = run_ids[start : start + batch]
        with engine.begin() as conn:
            history: dict[int, list[Any]] = {}
            for row in conn.execute(
                select(news_classifications)
                .where(
                    news_classifications.c.event_id.in_(ids),
                    news_classifications.c.source.not_in(SHADOW_SOURCES),
                )
                .order_by(news_classifications.c.event_id, news_classifications.c.version)
            ):
                history.setdefault(int(row.event_id), []).append(row)
            versions = _max_versions(conn, ids)
            rows: list[dict[str, object]] = []
            for event_id, items in history.items():
                last = items[-1]
                # Jen když je běh poslední ne-stínovou verzí — pozdější LLM nebo
                # ruční korekce má přednost a opakovaný rollback nic nemění
                if _utc(last.created_at) != run_at or last.source != RULE_SOURCE:
                    continue
                before = [
                    item
                    for item in items
                    if item.source == RULE_SOURCE and _utc(item.created_at) != run_at
                ]
                if not before:
                    continue
                previous = before[-1]
                rows.append(
                    {
                        "event_id": event_id,
                        "version": versions[event_id] + 1,
                        "source": RULE_SOURCE,
                        "category": previous.category,
                        "importance": previous.importance,
                        "direction": previous.direction,
                        "strength": previous.strength,
                        "created_at": now,
                    }
                )
            if rows:
                conn.execute(insert(news_classifications), rows)
                _denormalize(
                    conn,
                    [
                        {
                            "b_id": row["event_id"],
                            "b_category": row["category"],
                            "b_importance": row["importance"],
                        }
                        for row in rows
                    ],
                )
            restored += len(rows)
    return restored


# ── Fáze 2: kontaminace (K1) ───────────────────────────────────────


class FirstBars:
    """První obchodovaný bar ≥ čas per symbol — jen sloupec `ts_min`, cache per partice."""

    def __init__(self, data_dir: Path) -> None:
        self._derived = data_dir / "derived"
        self._days: dict[tuple[str, dt.date], list[dt.datetime]] = {}
        self.partition_reads = 0

    def _stamps(self, symbol: str, day: dt.date) -> list[dt.datetime]:
        key = (symbol, day)
        if key not in self._days:
            path = self._derived / symbol / BARS_SUBDIR / f"{day.isoformat()}.parquet"
            stamps: list[dt.datetime] = []
            if path.exists():
                self.partition_reads += 1
                column = pq.read_table(path, columns=["ts_min"]).column("ts_min").to_pylist()
                stamps = sorted(_utc(ts) for ts in column if ts is not None)
            self._days[key] = stamps
        return self._days[key]

    def first_at_or_after(self, symbol: str, moment: dt.datetime) -> dt.datetime | None:
        """Stejný rozsah jako `ReactionJob` (±1 den partic kolem 5denního výhledu).

        Minuta může ležet i v partici sousedního dne (#1002), proto minimum přes
        všechny partice rozsahu, ne první nalezená.
        """
        limit = moment + dt.timedelta(days=CLOSURE_LOOKAHEAD_DAYS) + CONTAMINATION_SPAN
        best: dt.datetime | None = None
        day = moment.date() - dt.timedelta(days=1)
        while day <= limit.date() + dt.timedelta(days=1):
            stamps = self._stamps(symbol, day)
            index = bisect.bisect_left(stamps, moment)
            if index < len(stamps) and stamps[index] <= limit:
                best = stamps[index] if best is None else min(best, stamps[index])
            day += dt.timedelta(days=1)
        return best


@dataclass
class Contaminators:
    """Časy a kategorie eventů s importance ≥ 2 (klasifikace po fázi 1), vzestupně."""

    times: list[dt.datetime] = field(default_factory=list)
    categories: list[str | None] = field(default_factory=list)

    @classmethod
    def build(cls, events: Iterable[tuple[dt.datetime, str | None, int | None]]) -> Contaminators:
        items = sorted(
            (ts, category)
            for ts, category, importance in events
            if (importance or 0) >= CONTAMINATION_MIN_IMPORTANCE
        )
        return cls([ts for ts, _ in items], [category for _, category in items])

    def around(self, ts: dt.datetime, category: str | None) -> list[dt.datetime]:
        """Kontaminující eventy v (ts, ts + 61 min] — jako `ReactionJob._contaminating`."""
        lo = bisect.bisect_right(self.times, ts)
        hi = bisect.bisect_right(self.times, ts + CONTAMINATION_SPAN)
        return [
            self.times[index]
            for index in range(lo, hi)
            if contaminates(category, self.categories[index])
        ]


@dataclass(frozen=True)
class ContaminationChange:
    event_id: int
    symbol: str
    old: dict[int, bool | None]
    new: dict[int, bool | None]


def recompute_contamination(
    reactions: Iterable[Any],
    events: dict[int, tuple[dt.datetime, str | None]],
    contaminators: Contaminators,
    first_bars: FirstBars,
    windows: Sequence[int] = DEFAULT_WINDOWS,
) -> list[ContaminationChange]:
    """Nové `cont_<w>` pro řádky reakcí; vrací jen řádky, kde se něco mění.

    Okno bez měření (NULL) zůstává NULL. Deferred řádek bez prvního baru
    (partice mezitím chybí) se nemění — nic se nehádá.
    """
    changes: list[ContaminationChange] = []
    for row in reactions:
        event = events.get(int(row.event_id))
        if event is None:
            continue
        ts_event, category = event
        start = ts_event
        if row.deferred_min:
            first_bar = first_bars.first_at_or_after(str(row.symbol), ts_event)
            if first_bar is None:
                continue
            start = first_bar
        others = contaminators.around(ts_event, category)
        old = {window: row._mapping[f"cont_{window}"] for window in windows}
        new = {
            window: (
                None
                if old[window] is None
                else window_contaminated(ts_event, start, window, others)
            )
            for window in windows
        }
        normalized_old = {w: (None if v is None else bool(v)) for w, v in old.items()}
        if new != normalized_old:
            changes.append(
                ContaminationChange(int(row.event_id), str(row.symbol), normalized_old, new)
            )
    return changes


def _reaction_rows(conn: Connection) -> list[Any]:
    return list(
        conn.execute(
            select(
                news_reactions.c.event_id,
                news_reactions.c.symbol,
                news_reactions.c.deferred_min,
                *(news_reactions.c[f"cont_{window}"] for window in DEFAULT_WINDOWS),
            ).where(news_reactions.c.computed_at_min.is_not(None))
        ).fetchall()
    )


def _all_classifications(conn: Connection) -> list[Any]:
    return list(
        conn.execute(
            select(
                news_events.c.id,
                news_events.c.ts_event,
                news_events.c.category,
                news_events.c.importance,
            )
        ).fetchall()
    )


def write_contamination(
    engine: Engine, changes: Sequence[ContaminationChange], *, batch: int
) -> int:
    statement = (
        update(news_reactions)
        .where(
            news_reactions.c.event_id == bindparam("b_event"),
            news_reactions.c.symbol == bindparam("b_symbol"),
        )
        .values({f"cont_{w}": bindparam(f"b_cont_{w}") for w in DEFAULT_WINDOWS})
    )
    for start in range(0, len(changes), batch):
        chunk = changes[start : start + batch]
        with engine.begin() as conn:
            conn.execute(
                statement,
                [
                    {
                        "b_event": change.event_id,
                        "b_symbol": change.symbol,
                        **{f"b_cont_{w}": change.new[w] for w in DEFAULT_WINDOWS},
                    }
                    for change in chunk
                ],
            )
    return len(changes)


# ── Souhrn (matice změn) ───────────────────────────────────────────


def _table(header: Sequence[str], rows: Iterable[Sequence[object]]) -> list[str]:
    out = [" | ".join(header), " | ".join("---" for _ in header)]
    out += [" | ".join(str(cell) for cell in row) for row in rows]
    return out


def classification_report(results: Sequence[Reclass], now: dt.datetime) -> list[str]:
    changed = [r for r in results if r.changed]
    importance_changed = sum(r.old_importance != r.new_importance for r in results)
    category_changed = sum(r.old_category != r.new_category for r in results)
    total = max(1, len(results))
    out = [
        "## Fáze 1 — klasifikace v2 (jen `sentiment_source = rule`)",
        "",
        f"Pravidlových eventů: {len(results)}; změna čehokoli **{len(changed)}** "
        f"({len(changed) / total:.0%}) = nových verzí v news_classifications; "
        f"importance {importance_changed} ({importance_changed / total:.0%}), "
        f"kategorie {category_changed} ({category_changed / total:.0%}).",
        "",
        "### Matice importance (řádek stará → sloupec nová)",
        "",
    ]
    matrix = Counter((r.old_importance, r.new_importance) for r in results)
    olds = sorted({r.old_importance for r in results}, key=lambda v: -1 if v is None else v)
    out += _table(
        ["stará \\ nová", "1", "2", "3"],
        [[str(old), *(matrix[(old, new)] for new in (1, 2, 3))] for old in olds],
    )
    out += ["", "### Podle feedu (importance ≥ 2 / = 3 / významné, stará → nová)", ""]
    by_feed: dict[str, list[Reclass]] = {}
    for r in results:
        by_feed.setdefault(r.feed, []).append(r)
    feed_rows = []
    for feed, items in sorted(by_feed.items(), key=lambda kv: -len(kv[1])):
        feed_rows.append(
            [
                feed,
                len(items),
                sum(r.changed for r in items),
                f"{sum((r.old_importance or 0) >= 2 for r in items)} → "
                f"{sum(r.new_importance >= 2 for r in items)}",
                f"{sum(r.old_importance == 3 for r in items)} → "
                f"{sum(r.new_importance == 3 for r in items)}",
                f"{sum(_significant_old(r) for r in items)} → "
                f"{sum(_significant_new(r) for r in items)}",
            ]
        )
    out += _table(["feed", "zpráv", "změn", "imp ≥ 2", "imp 3", "významné"], feed_rows)
    out += ["", "### Nejčastější změny kategorie (stará → nová)", ""]
    categories = Counter(
        (r.old_category, r.new_category) for r in results if r.old_category != r.new_category
    )
    out += _table(
        ["stará", "nová", "počet"], [[a, b, n] for (a, b), n in categories.most_common(20)]
    )
    recent_from = now - dt.timedelta(days=RECENT_DAYS)
    recent = [r for r in results if r.ts_event >= recent_from and r.ts_event <= now]
    days = RECENT_DAYS
    out += [
        "",
        f"### Posledních {RECENT_DAYS} dní — za den (stará → nová, sdílená definice)",
        "",
        f"- zpráv: {len(recent) / days:.1f}",
        f"- významné: {sum(_significant_old(r) for r in recent) / days:.1f} → "
        f"{sum(_significant_new(r) for r in recent) / days:.1f}",
        f"- zásadní (is_key): {sum(_key_old(r) for r in recent) / days:.1f} → "
        f"{sum(_key_new(r) for r in recent) / days:.1f}",
        f"- importance 3 bez kalendáře: "
        f"{sum(r.old_importance == 3 and r.kind != 'scheduled' for r in recent) / days:.1f} → "
        f"{sum(r.new_importance == 3 and r.kind != 'scheduled' for r in recent) / days:.1f}",
        f"- kalendář významný: "
        f"{sum(_significant_new(r) for r in recent if r.kind == 'scheduled') / days:.1f}",
        "",
        "Pozn.: „stará“ je stará importance vyhodnocená novou (sdílenou) definicí —",
        "dřívější definice brala kalendář podle `raw.impact` a sociální sítě podle kurátora.",
        "",
        f"### Důvody importance 3 (nová, posledních {RECENT_DAYS} dní) a ukázky",
        "",
    ]
    reasons = Counter(r.reason for r in recent if r.new_importance == 3)
    for reason, count in reasons.most_common(12):
        out.append(f"- **{reason}**: {count}")
        for r in [r for r in recent if r.new_importance == 3 and r.reason == reason][:5]:
            out.append(f"  - {r.feed} {r.new_category} | {r.title[:110]}")
    out += ["", "### Ukázky přechodů (posledních dní)", ""]
    for (old, new), _ in Counter(
        (r.old_importance, r.new_importance) for r in recent if r.changed
    ).most_common(6):
        suffix = " (změna jen kategorie)" if old == new else ""
        out.append(f"- importance {old} → {new}{suffix}:")
        for r in [r for r in recent if (r.old_importance, r.new_importance) == (old, new)][:6]:
            out.append(
                f"  - {r.feed} {r.old_category}→{r.new_category} ({r.reason}) | {r.title[:100]}"
            )
    return out


def _significant_old(r: Reclass) -> bool:
    return significance_tier(r.kind, r.old_importance, r.old_category) is not None


def _significant_new(r: Reclass) -> bool:
    return significance_tier(r.kind, r.new_importance, r.new_category) is not None


def _key_old(r: Reclass) -> bool:
    return is_key(r.kind, r.old_importance, r.old_category)


def _key_new(r: Reclass) -> bool:
    return is_key(r.kind, r.new_importance, r.new_category)


def contamination_report(
    reactions: Sequence[Any], changes: Sequence[ContaminationChange], reads: int
) -> list[str]:
    by_key = {(c.event_id, c.symbol): c for c in changes}
    out = [
        "## Fáze 2 — kontaminace K1 (jen jiná kategorie kontaminuje)",
        "",
        f"Řádků reakcí s minutovou fází: {len(reactions)}; změněných řádků **{len(changes)}**; "
        f"přečteno partic barů (deferred): {reads}.",
        "",
    ]
    rows = []
    for window in DEFAULT_WINDOWS:
        measured = [r for r in reactions if r._mapping[f"cont_{window}"] is not None]
        before = sum(bool(r._mapping[f"cont_{window}"]) for r in measured)
        after = 0
        to_clean = to_dirty = 0
        for r in measured:
            change = by_key.get((int(r.event_id), str(r.symbol)))
            old = bool(r._mapping[f"cont_{window}"])
            new = change.new[window] if change is not None else old
            after += bool(new)
            to_clean += old and not new
            to_dirty += (not old) and bool(new)
        rows.append(
            [
                f"w{window}",
                len(measured),
                f"{before} ({before / max(1, len(measured)):.0%})",
                f"{after} ({after / max(1, len(measured)):.0%})",
                to_clean,
                to_dirty,
                f"{len(measured) - before} → {len(measured) - after}",
            ]
        )
    out += _table(
        [
            "okno",
            "měřených",
            "kontaminovaných před",
            "po",
            "True→False",
            "False→True",
            "čistých vzorků",
        ],
        rows,
    )
    return out


# ── Spojení a běh ──────────────────────────────────────────────────


def database_url(explicit: str | None) -> str | URL:
    if explicit:
        return explicit
    for name in ("GEXLENS_NEWS_DATABASE_URL", "GEXLENS_HOST_DATABASE_URL"):
        url = os.environ.get(name)
        if url:
            return url
    password = os.environ.get("GEXLENS_PG_PASSWORD")
    if not password:
        raise SystemExit(
            "Chybí --db, GEXLENS_NEWS_DATABASE_URL, GEXLENS_HOST_DATABASE_URL "
            "nebo GEXLENS_PG_PASSWORD"
        )
    # URL.create heslo escapuje — speciální znaky ho nerozbijí
    return URL.create(
        "postgresql+psycopg",
        username="gexlens",
        password=password,
        host="127.0.0.1",
        port=55432,
        database="gexlens",
    )


def make_engine(url: str | URL, *, read_only: bool) -> Engine:
    """PG v dry-runu odmítne jakýkoli zápis (`default_transaction_read_only`)."""
    engine = create_engine(url)
    if read_only and engine.dialect.name == "postgresql":
        engine.dispose()
        engine = create_engine(url, connect_args={"options": "-c default_transaction_read_only=on"})
    return engine


def run(
    engine: Engine,
    data_dir: Path,
    *,
    dry_run: bool,
    phases: Sequence[str],
    batch: int,
    now: dt.datetime,
    log: Any = print,
    curated: Curated | None = None,
) -> list[str]:
    """Obě fáze (nebo vybraná); vrací řádky reportu.

    `curated` = kurátoři Bluesky pro historii (`load_curated`); None = jen příznak.
    """
    curated = curated or Curated()
    run_at = now.replace(microsecond=0)
    report = [
        "# Reklasifikace zpráv v2 (#1293, ADR-0045)",
        "",
        f"Běh {run_at.isoformat()} · {'DRY-RUN (jen čtení)' if dry_run else 'OSTRÝ BĚH'} · "
        f"fáze {', '.join(phases)} · dávka {batch}",
        "",
    ]
    if not dry_run and "classify" in phases:
        report += [f"RUN_AT pro rollback: `{run_at.isoformat()}`", ""]
    results: list[Reclass] = []
    if "classify" in phases:
        log("Fáze 1: klasifikace v2")
        results = run_classification(
            engine,
            dry_run=dry_run,
            batch=batch,
            run_at=run_at,
            log=log,
            curated_dids=curated.dids,
        )
        unresolved = ", ".join(curated.unresolved) or "žádné"
        report += [
            f"Kurátoři Bluesky pro historii: {len(curated.dids)} DID (s příznakem raw.curated "
            f"{curated.flagged}, ze seznamu kurátorů {curated.listed}); "
            f"nepřeložené handly: {unresolved}.",
            "",
            *classification_report(results, now),
            "",
        ]
    if "contamination" in phases:
        log("Fáze 2: kontaminace K1")
        with engine.connect() as conn:
            classified = _all_classifications(conn)
            reactions = _reaction_rows(conn)
        override = {r.event_id: (r.new_category, r.new_importance) for r in results}
        events: dict[int, tuple[dt.datetime, str | None]] = {}
        stamped: list[tuple[dt.datetime, str | None, int | None]] = []
        for row in classified:
            category, importance = override.get(int(row.id), (row.category, row.importance))
            ts = _utc(row.ts_event)
            events[int(row.id)] = (ts, category)
            stamped.append((ts, category, importance))
        first_bars = FirstBars(data_dir)
        began = time.perf_counter()
        changes = recompute_contamination(
            reactions, events, Contaminators.build(stamped), first_bars
        )
        log(
            f"  kontaminace: {len(reactions)} řádků, změn {len(changes)} "
            f"({time.perf_counter() - began:.0f} s)"
        )
        if not dry_run:
            write_contamination(engine, changes, batch=batch)
        report += contamination_report(reactions, changes, first_bars.partition_reads) + [""]
    if not dry_run:
        report += [
            "## Po běhu",
            "",
            "- restart news-engine (přepočet news_model_stats a gate v první smyčce),",
            "- `python -m gexlens_news recompute-sentindex --from <nejstarší partice>`,",
            "- ověřit: druhý běh `--dry-run` má 0 změn v obou fázích.",
        ]
    return report


def main(argv: Sequence[str] | None = None) -> int:
    # Konzole Windows (cp1250) neumí „≥“ ani „→“ z nápovědy a reportu — výstup v UTF-8
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--db", default=None, help="SQLAlchemy URL (jinak z prostředí)")
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("GEXLENS_NEWS_DATA_DIR", "data"),
        help="kořen dat s derived/{ES,NQ}/bars (deferred reakce)",
    )
    parser.add_argument("--dry-run", action="store_true", help="nic nezapisovat, jen matice změn")
    parser.add_argument(
        "--phase",
        choices=("all", "classify", "contamination"),
        default="all",
        help="která fáze (výchozí obě)",
    )
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    parser.add_argument(
        "--rollback", default=None, help="RUN_AT ostrého běhu (ISO s časovou zónou)"
    )
    parser.add_argument("--out", default=None, help="kam uložit report (Markdown)")
    args = parser.parse_args(argv)

    now = dt.datetime.now(dt.UTC)
    url = database_url(args.db)
    if args.rollback:
        if args.dry_run:
            raise SystemExit("--rollback a --dry-run nejdou spolu")
        run_at = _utc(dt.datetime.fromisoformat(args.rollback))
        restored = rollback(make_engine(url, read_only=False), run_at, now, batch=args.batch)
        print(f"Rollback běhu {run_at.isoformat()}: obnoveno {restored} eventů")
        print("Kontaminaci přepočti: --phase contamination")
        return 0
    phases = ("classify", "contamination") if args.phase == "all" else (args.phase,)
    engine = make_engine(url, read_only=args.dry_run)
    curated = (
        load_curated(
            engine, os.environ.get("GEXLENS_NEWS_BLUESKY_CURATED_AUTHORS", ""), resolve_authors
        )
        if "classify" in phases
        else None
    )
    report = run(
        engine,
        Path(args.data_dir),
        dry_run=args.dry_run,
        phases=phases,
        batch=args.batch,
        now=now,
        curated=curated,
    )
    text = "\n".join(report) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"Report: {args.out}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
