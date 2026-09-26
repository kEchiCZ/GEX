"""Měření reakce ES/NQ na ohlášené releasy a živé vyhodnocení hypotéz (#1296, ADR-0044).

IO adaptér nad čistými funkcemi `release_moves.py` a `release_hypotheses.py`:

* **Vstup**: USD releasy s FF dopadem High/Medium z `news_events` (`releases.py`),
  shluky po minutě, jen rodiny s upozorněním nebo hypotézou.
* **Kdy**: shluk se měří, když uplynul nejdelší horizont (60 min + zápis barů)
  **a headline má actual** — release bez actual se nekonal (přesun na jiný den
  nechá v kalendáři fantomový řádek, #1298) nebo ho FF ještě nevyplnil; po 24 h
  bez actual job jednou zaloguje varování. Živě se dívá 14 dní zpět; chybějící
  řádek se změří, neúplný (bary dorazily pozdě) se zkusí znovu nejvýš jednou za
  hodinu do 3 dní, překvapení se doplní do 7 dní — pak se už nesahá.
* **Backfill** historie = tatáž `run(now, since=…, refresh=True)` (CLI
  `python -m gexlens_news backfill-release-moves`), jedna cesta k datům. Přeměří
  jen releasy **před registrací hypotéz**; živé řádky jen s `include_live=True`
  (CLI `--include-live`), a i pak vyhodnocený úsek hypotéz zůstane zmrazený.
  Prázdná tabulka se dopočítá sama od začátku archivu barů (zkouší se každý
  cyklus, dokud je prázdná — selhaný první běh se tak zopakuje).
* **Hypotézy**: po každé změně živého řádku (od `REGISTERED_AT`) se stav
  přepočítá a `release_hypotheses` přepíše v jedné transakci; vyhodnocený úsek
  (do posledního kontrolního bodu, resp. rozhodnutí) se přebírá z předchozího
  stavu (`release_hypotheses.FrozenPrefix`).

Job nesmí spadnout na jednom releasu: chybějící bary, rozsahy nebo baseline =
NULL sloupce a INFO log s počty (pokrytí musí být vidět), výjimka jednoho
shluku × instrumentu se zaloguje a job pokračuje dalším.
"""

import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.engine import Connection, Engine

from gexlens_engine.compute.settle import trading_session_date
from gexlens_engine.gammacliff import session_ranges
from gexlens_engine.storage.sentiment import release_hypotheses, release_moves
from gexlens_news.bars import BarsRepository
from gexlens_news.clusters import BAR_SETTLE
from gexlens_news.reactions import (
    SIGMA_LOOKBACK_MIN,
    build_excursion_baseline,
    minute_of_day_et,
    tod_thresholds,
)
from gexlens_news.release_hypotheses import (
    M1_BASELINE_SESSIONS,
    M1_TOD_HALF_WIDTH_MIN,
    M1_TOD_MIN_SAMPLES,
    M1_TOD_QUANTILE,
    REGISTERED_AT,
    FrozenPrefix,
    MoveFacts,
    evaluate_all,
    frozen_prefix,
)
from gexlens_news.release_moves import MOVE_WINDOW_MIN, RETURN_WINDOWS, measure_move, vol_ref_bp
from gexlens_news.releases import (
    PREVIEW_FAMILIES,
    ReleaseCluster,
    cluster_releases,
    family_clusters,
    load_releases,
)

logger = logging.getLogger(__name__)

#: Začátek archivu minutových barů ES/NQ — odsud dopočítává backfill
BACKFILL_START = dt.datetime(2024, 7, 28, tzinfo=dt.UTC)
#: Živý běh se dívá tolik zpět (dohání výpadek news-enginu)
LIVE_LOOKBACK = dt.timedelta(days=14)
#: Shluk se měří až po uplynutí nejdelšího horizontu a zápisu barů
READY_AFTER = dt.timedelta(minutes=max(RETURN_WINDOWS)) + BAR_SETTLE
#: Neúplné měření (bary dorazily pozdě) se zkouší znovu do tolika dní…
REMEASURE_WITHIN = dt.timedelta(days=3)
#: … nejvýš jednou za tuto dobu
REMEASURE_EVERY = dt.timedelta(hours=1)
#: Překvapení (actual z FF) se doplňuje do tolika dní, pak se zmrazí
SURPRISE_WITHIN = dt.timedelta(days=7)
#: Headline bez actual po této době = release se nejspíš nekonal (varování v logu);
#: FF actual dorazí po High releasu do minut (60s burst #386), jinak do hodiny
ACTUAL_WAIT = dt.timedelta(hours=24)
#: Rozsahy pro vol_ref: 20 seancí + víkendy a svátky
RANGES_LOOKBACK = dt.timedelta(days=45)
#: Bary kolem releasu: σ poslední hodiny před ním + nejdelší okno po něm
_BARS_BEFORE = dt.timedelta(minutes=SIGMA_LOOKBACK_MIN + 2)
_BARS_AFTER = dt.timedelta(minutes=max(RETURN_WINDOWS) + 1)

_MEASURED = ("exc_15m_bp", "ret_15m_bp", "ret_60m_bp")


def as_utc(value: dt.datetime) -> dt.datetime:
    """SQLite vrací naivní čas (UTC), PG aware — klíče se porovnávají v UTC."""
    return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)


@dataclass
class MovesSummary:
    """Co běh udělal — pro log a CLI."""

    clusters: int = 0
    measured: int = 0
    updated: int = 0
    unmeasurable: int = 0
    without_vol_ref: int = 0
    without_actual: int = 0
    failed: int = 0
    hypotheses: bool = False

    def describe(self) -> str:
        return (
            f"shluků {self.clusters}, změřeno {self.measured}, aktualizováno {self.updated}, "
            f"bez výchylky {self.unmeasurable}, bez vol_ref {self.without_vol_ref}, "
            f"bez actual {self.without_actual}, chyb {self.failed}, "
            f"hypotézy {'přepočteny' if self.hypotheses else 'beze změny'}"
        )


def facts_from_row(row: Any) -> MoveFacts:
    return MoveFacts(
        cluster_ts=as_utc(row.cluster_ts),
        symbol=str(row.symbol),
        family=str(row.family),
        headline=str(row.headline),
        surprise_sign=int(row.surprise_sign) if row.surprise_sign is not None else None,
        exc_15m_bp=row.exc_15m_bp,
        ret_15m_bp=row.ret_15m_bp,
        ret_60m_bp=row.ret_60m_bp,
        tod_med_15m_bp=row.tod_med_15m_bp,
    )


class ReleaseMovesJob:
    """Shluky releasů × ES/NQ → `release_moves`; živé řádky → `release_hypotheses`."""

    def __init__(
        self,
        engine: Engine,
        bars: BarsRepository,
        *,
        symbols: Sequence[str] = ("ES", "NQ"),
        registered_at: dt.datetime = REGISTERED_AT,
    ) -> None:
        self._engine = engine
        self._bars = bars
        self._symbols = list(symbols)
        # Replay (scripts) posouvá registraci dozadu; živě vždy REGISTERED_AT
        self._registered_at = registered_at
        #: Shluky bez actual po ACTUAL_WAIT už zalogované (varování jednou za proces)
        self._reported_without_actual: set[dt.datetime] = set()
        #: symbol → (od seance, do dne `now`, rozsahy)
        self._ranges: dict[str, tuple[dt.date, dt.date, list[tuple[dt.date, float]]]] = {}

    # ── IO ────────────────────────────────────────────────────────
    def existing(self, start: dt.datetime, end: dt.datetime) -> dict[tuple[dt.datetime, str], Any]:
        stmt = select(release_moves).where(
            release_moves.c.cluster_ts >= start, release_moves.c.cluster_ts <= end
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return {(as_utc(row.cluster_ts), str(row.symbol)): row for row in rows}

    def is_empty(self) -> bool:
        with self._engine.connect() as conn:
            count = conn.execute(select(func.count()).select_from(release_moves)).scalar()
        return not count

    def ranges(self, symbol: str, since: dt.date, today: dt.date) -> list[tuple[dt.date, float]]:
        """Denní rozsahy od seance `since`; cache do změny dne (nová seance = nový rozsah)."""
        cached = self._ranges.get(symbol)
        if cached is not None and cached[0] <= since and cached[1] == today:
            return cached[2]
        rows = session_ranges(self._bars.data_dir, symbol, since=since)
        self._ranges[symbol] = (since, today, rows)
        return rows

    def tod_median(self, symbol: str, start: dt.datetime) -> float | None:
        """Baseline M1: medián 15min výchylek ±30 min stejné denní doby za 20 seancí.

        Parametry jsou zmrazené konstanty registru (`M1_*`), ne sdílené
        konstanty news_anomaly — ladění anomálií kritérium M1 nezmění.
        """
        sessions = self._bars.recent_sessions(
            symbol, trading_session_date(start), M1_BASELINE_SESSIONS
        )
        if not sessions:
            return None
        baseline = build_excursion_baseline(sessions, MOVE_WINDOW_MIN)
        thresholds = tod_thresholds(
            baseline,
            minute_of_day_et(start),
            half_width=M1_TOD_HALF_WIDTH_MIN,
            quantile=M1_TOD_QUANTILE,
            min_samples=M1_TOD_MIN_SAMPLES,
        )
        return thresholds.bp if thresholds is not None else None

    def measure(
        self, cluster: ReleaseCluster, symbol: str, now: dt.datetime, ranges_since: dt.date
    ) -> dict[str, Any]:
        """Naměřené sloupce jednoho shluku × instrumentu (NULL = nejde změřit)."""
        start = cluster.ts
        bars = self._bars.load_range(symbol, start - _BARS_BEFORE, start + _BARS_AFTER)
        measured = measure_move(bars, start)
        session = trading_session_date(start)
        vol_ref = vol_ref_bp(
            self.ranges(symbol, ranges_since, trading_session_date(now)),
            session,
            measured.base_close,
        )
        tod: float | None = None
        if start >= self._registered_at and measured.exc_15m_bp is not None:
            tod = self.tod_median(symbol, start)
        return {
            "vol_ref_bp": vol_ref,
            "exc_15m_bp": measured.exc_15m_bp,
            "ret_15m_bp": measured.ret_15m_bp,
            "ret_60m_bp": measured.ret_60m_bp,
            "tod_med_15m_bp": tod,
        }

    @staticmethod
    def _upsert(conn: Connection, row: dict[str, Any]) -> None:
        """Přenositelný upsert (PG i SQLite v testech): update, při 0 řádcích insert."""
        key = (
            release_moves.c.cluster_ts == row["cluster_ts"],
            release_moves.c.symbol == row["symbol"],
        )
        values = {
            name: value for name, value in row.items() if name not in ("cluster_ts", "symbol")
        }
        if conn.execute(update(release_moves).where(*key).values(**values)).rowcount == 0:
            conn.execute(insert(release_moves).values(**row))

    def store_hypotheses(self, now: dt.datetime) -> None:
        """Přepočet `release_hypotheses` z živých řádků (jedna transakce).

        Vyhodnocený úsek každé hypotézy (do posledního kontrolního bodu, resp.
        rozhodnutí) se převezme z předchozího řádku — přepočet ho nepřepíše.
        """
        stmt = select(release_moves).where(release_moves.c.cluster_ts >= self._registered_at)
        with self._engine.begin() as conn:
            frozen: dict[tuple[str, str], FrozenPrefix] = {}
            for row in conn.execute(select(release_hypotheses)):
                prefix = frozen_prefix(str(row.status), row.decided_at_n, row.outcomes or [])
                if prefix is not None:
                    frozen[(str(row.hypothesis), str(row.symbol))] = prefix
            facts = [facts_from_row(row) for row in conn.execute(stmt).fetchall()]
            states = evaluate_all(facts, registered_at=self._registered_at, frozen=frozen)
            conn.execute(delete(release_hypotheses))
            conn.execute(insert(release_hypotheses), [state.to_row(now) for state in states])
        logger.info(
            "Hypotézy releasů přepočteny: %s",
            ", ".join(
                f"{state.hypothesis} {state.symbol} {state.hits}/{state.n} {state.status}"
                for state in states
            ),
        )
        for state in states:
            if state.ignored_changes:
                logger.warning(
                    "Hypotéza %s %s: %d releasů ve vyhodnoceném úseku by dnes dalo jiný "
                    "výsledek — úsek je zmrazený, změna se nepropíše (ADR-0044)",
                    state.hypothesis,
                    state.symbol,
                    state.ignored_changes,
                )

    # ── běh ───────────────────────────────────────────────────────
    def _plan(
        self,
        cluster: ReleaseCluster,
        stored: Any,
        now: dt.datetime,
        remeasure: bool,
    ) -> str | None:
        """„measure“ = změřit z barů, „meta“ = jen rodina/headline/překvapení, None = nic."""
        age = now - cluster.ts
        if stored is None or remeasure:
            return "measure"
        incomplete = any(getattr(stored, name) is None for name in _MEASURED) or (
            cluster.ts >= self._registered_at and stored.tod_med_15m_bp is None
        )
        if (
            incomplete
            and age < REMEASURE_WITHIN
            and now - as_utc(stored.measured_at) >= REMEASURE_EVERY
        ):
            return "measure"
        surprise = cluster.headline.surprise_sign()
        meta_changed = (
            stored.family != cluster.family
            or stored.headline != cluster.headline.series
            or stored.headline_event_id != cluster.headline.id
        )
        if meta_changed or (age < SURPRISE_WITHIN and stored.surprise_sign != surprise):
            return "meta"
        return None

    def _has_actual(self, cluster: ReleaseCluster, now: dt.datetime) -> bool:
        """Headline s actual = release proběhl; bez něj se shluk neměří (fantom, #1298)."""
        if cluster.headline.actual is not None:
            return True
        if now - cluster.ts >= ACTUAL_WAIT and cluster.ts not in self._reported_without_actual:
            self._reported_without_actual.add(cluster.ts)
            logger.warning(
                "Release %s %s nemá ani po %d h actual — neměří se (přesunutý nebo "
                "zrušený release? #1298)",
                cluster.headline.title,
                cluster.ts.isoformat(),
                ACTUAL_WAIT.total_seconds() // 3600,
            )
        return False

    def run(
        self,
        now: dt.datetime,
        *,
        since: dt.datetime | None = None,
        refresh: bool = False,
        include_live: bool = False,
    ) -> MovesSummary:
        """Změří nové a neúplné shluky od `since` (živě 14 dní, prázdná tabulka = celý archiv).

        `refresh` přeměří i uložené shluky před registrací hypotéz; živé
        (od `REGISTERED_AT`) jen s `include_live` — i pak zůstane vyhodnocený
        úsek hypotéz zmrazený.
        """
        summary = MovesSummary()
        if since is None:
            # Kontrola v každém cyklu, dokud je tabulka prázdná: selhaný první
            # backfill se zopakuje (COUNT nad stovkami řádků nic nestojí)
            if self.is_empty():
                since = BACKFILL_START
                logger.info("release_moves je prázdná — dopočítávám historii od %s", since.date())
            else:
                since = now - LIVE_LOOKBACK
        if refresh and include_live:
            logger.warning(
                "Přeměření zahrnuje živé releasy od %s — vyhodnocený úsek hypotéz zůstává "
                "zmrazený, změní se jen releasy za ním",
                self._registered_at.date(),
            )
        end = now - READY_AFTER
        if end < since:
            return summary
        clusters = family_clusters(
            cluster_releases(load_releases(self._engine, since, end)), PREVIEW_FAMILIES
        )
        summary.clusters = len(clusters)
        if not clusters:
            return summary
        stored = self.existing(since, end)
        ranges_since = trading_session_date(clusters[0].ts) - RANGES_LOOKBACK
        rows: list[dict[str, Any]] = []
        live_changed = False
        for cluster in clusters:
            if not self._has_actual(cluster, now):
                summary.without_actual += 1
                continue
            live = cluster.ts >= self._registered_at
            remeasure = refresh and (include_live or not live)
            for symbol in self._symbols:
                previous = stored.get((cluster.ts, symbol))
                plan = self._plan(cluster, previous, now, remeasure)
                if plan is None:
                    continue
                row: dict[str, Any] = {
                    "cluster_ts": cluster.ts,
                    "symbol": symbol,
                    "family": cluster.family,
                    "headline": cluster.headline.series,
                    "headline_event_id": cluster.headline.id,
                    "surprise_sign": cluster.headline.surprise_sign(),
                    "measured_at": now,
                }
                if plan == "measure":
                    try:
                        row.update(self.measure(cluster, symbol, now, ranges_since))
                    except Exception:
                        summary.failed += 1
                        logger.exception(
                            "Měření releasu %s %s (%s) selhalo — pokračuji dalším",
                            cluster.headline.title,
                            cluster.ts.isoformat(),
                            symbol,
                        )
                        continue
                    summary.measured += 1
                    summary.unmeasurable += int(row["exc_15m_bp"] is None)
                    summary.without_vol_ref += int(row["vol_ref_bp"] is None)
                else:
                    # Jen metadata a překvapení — naměřené hodnoty i čas měření zůstávají
                    del row["measured_at"]
                    summary.updated += 1
                rows.append(row)
                live_changed |= live
        if rows:
            with self._engine.begin() as conn:
                for row in rows:
                    self._upsert(conn, row)
            logger.info("Reakce na releasy (od %s): %s", since.date(), summary.describe())
        if live_changed:
            self.store_hypotheses(now)
            summary.hypotheses = True
        return summary
