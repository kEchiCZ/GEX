"""Upozornění před ohlášeným releasem (#1296 fáze 3, ADR-0044) — IO adaptér.

Text a rozhodování žijí v čistých funkcích `release_preview.py`; tady je čtení
kalendáře z PG, ceny a úrovní z parquet archivu, minulých měření
(`release_moves`), stavu hypotéz (`release_hypotheses`) a stav etap.

* Běží ve vlastní smyčce news-enginu à 60 s (T−15 odejde mezi T−15:00
  a T−14:00; v reaction_loop à 300 s + délka cyklu by chodilo až o 5+ min
  později). Mimo okno releasu je tik jeden SELECT nad `ix_news_events_ts`.
* Spouští ho FF kalendář: USD scheduled releasy, shluk s aspoň jedním
  dopadem High a headline z `PREVIEW_FAMILIES`.
* Stav etap = řádky `release_previews`, zapisují se PŘED vrácením payloadů:
  restart nic nezopakuje, riziko je ztráta jednoho upozornění, ne duplicita
  (vzor ADR-0043). Po pozdním startu odejde jen pozdější etapa.
* Chybějící data (cena, úrovně, volatilita, historie) nejsou výjimka: text je
  ukáže a upozornění odejde.
"""

import datetime as dt
import logging
from collections.abc import Callable, Sequence
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine

from gexlens_engine.compute.coach_setups import LOCAL_TZ
from gexlens_engine.compute.settle import trading_session_date
from gexlens_engine.gammacliff import session_ranges
from gexlens_engine.storage.sentiment import release_hypotheses, release_moves, release_previews
from gexlens_news.bars import BarsRepository
from gexlens_news.preopen import SessionLevels, due_stages
from gexlens_news.preopen_job import read_levels
from gexlens_news.release_hypotheses import HYPOTHESIS_BY_ID, STATUS_TESTING
from gexlens_news.release_moves import history_count, size_stats, vol_ref_bp
from gexlens_news.release_moves_job import RANGES_LOOKBACK, as_utc
from gexlens_news.release_preview import (
    PREVIEW_LEADS,
    HypothesisView,
    build_preview,
    stages_to_send,
)
from gexlens_news.releases import PREVIEW_FAMILIES, ReleaseCluster, cluster_releases, load_releases

logger = logging.getLogger(__name__)

#: Jak daleko dopředu hledat releasy (nejdelší etapa + rezerva na tik)
LOOKAHEAD = max(lead for _, lead in PREVIEW_LEADS) + dt.timedelta(minutes=1)
#: Cena = close posledního baru, nejvýš tak starého
PRICE_MAX_AGE = dt.timedelta(minutes=15)


class ReleasePreviewJob:
    """Nadcházející releasy → `release_preview` pro ES a NQ v T−60 a T−15 min."""

    def __init__(
        self,
        engine: Engine,
        bars: BarsRepository,
        *,
        symbols: Sequence[str] = ("ES", "NQ"),
        tz: ZoneInfo = LOCAL_TZ,
    ) -> None:
        self._engine = engine
        self._bars = bars
        self._derived = bars.data_dir / "derived"
        self._symbols = list(symbols)
        self._tz = tz
        #: symbol → (seance, vol_ref rozsahy) — rozsahy se mezi etapami dne nemění
        self._ranges: dict[str, tuple[dt.date, list[tuple[dt.date, float]]]] = {}

    # ── IO ────────────────────────────────────────────────────────
    def upcoming(self, now: dt.datetime) -> list[ReleaseCluster]:
        """Shluky v (now, now + LOOKAHEAD] s dopadem High a rodinou s upozorněním."""
        events = load_releases(self._engine, now, now + LOOKAHEAD)
        return [
            cluster
            for cluster in cluster_releases(events)
            if cluster.ts > now and cluster.has_high and cluster.family in PREVIEW_FAMILIES
        ]

    def done_stages(self, cluster_ts: dt.datetime) -> dict[str, set[str]]:
        stmt = select(release_previews.c.symbol, release_previews.c.stage).where(
            release_previews.c.cluster_ts == cluster_ts
        )
        done: dict[str, set[str]] = {}
        with self._engine.connect() as conn:
            for row in conn.execute(stmt):
                done.setdefault(str(row.symbol), set()).add(str(row.stage))
        return done

    def last_price(self, symbol: str, now: dt.datetime) -> float | None:
        bars = self._bars.load_range(symbol, now - PRICE_MAX_AGE, now)
        return bars[-1].close if bars else None

    def levels(
        self, symbol: str, now: dt.datetime, cluster: ReleaseCluster
    ) -> SessionLevels | None:
        """Poslední úrovně 0DTE dne releasu (nebo nejbližší další expirace) před `now`."""
        return read_levels(self._derived, symbol, now.date(), now, trading_session_date(cluster.ts))

    def vol_now(self, symbol: str, now: dt.datetime, price: float | None) -> float | None:
        session = trading_session_date(now)
        cached = self._ranges.get(symbol)
        if cached is None or cached[0] != session:
            ranges = session_ranges(self._bars.data_dir, symbol, since=session - RANGES_LOOKBACK)
            cached = (session, ranges)
            self._ranges[symbol] = cached
        return vol_ref_bp(cached[1], session, price)

    def history(self, family: str, symbol: str, before: dt.datetime) -> list[tuple[Any, Any]]:
        stmt = select(release_moves.c.exc_15m_bp, release_moves.c.vol_ref_bp).where(
            release_moves.c.family == family,
            release_moves.c.symbol == symbol,
            release_moves.c.cluster_ts < before,
        )
        with self._engine.connect() as conn:
            return [(row.exc_15m_bp, row.vol_ref_bp) for row in conn.execute(stmt)]

    def hypothesis_views(self, symbol: str) -> dict[str, HypothesisView]:
        """Stav hypotéz na instrumentu; bez řádku (čerstvá DB) = ověřuje se, živě 0 z 0."""
        stmt = select(release_hypotheses).where(release_hypotheses.c.symbol == symbol)
        with self._engine.connect() as conn:
            stored = {str(row.hypothesis): row for row in conn.execute(stmt)}
        views: dict[str, HypothesisView] = {}
        for hypothesis_id, hypothesis in HYPOTHESIS_BY_ID.items():
            if symbol not in hypothesis.symbols:
                continue
            row = stored.get(hypothesis_id)
            historical_hits, historical_n = hypothesis.historical[symbol]
            views[hypothesis_id] = HypothesisView(
                status=str(row.status) if row is not None else STATUS_TESTING,
                hits=int(row.hits) if row is not None else 0,
                n=int(row.n) if row is not None else 0,
                wilson_lb=row.wilson_lb if row is not None else None,
                wilson_ub=row.wilson_ub if row is not None else None,
                historical_hits=historical_hits,
                historical_n=historical_n,
            )
        return views

    def store(self, rows: Sequence[dict[str, Any]]) -> None:
        with self._engine.begin() as conn:
            conn.execute(insert(release_previews), list(rows))

    def _safe[R](self, what: str, symbol: str, call: Callable[[], R]) -> R | None:
        """IO čtení, které nesmí shodit upozornění — chyba = chybějící údaj v textu + log."""
        try:
            return call()
        except Exception:
            logger.exception("Upozornění před releasem %s: čtení %s selhalo", symbol, what)
            return None

    def _payload(
        self, symbol: str, cluster: ReleaseCluster, now: dt.datetime
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Payload upozornění a hodnoty pro řádek `release_previews` (bez klíče a etapy)."""
        family = cluster.family or ""
        price = self._safe("ceny", symbol, lambda: self.last_price(symbol, now))
        levels = self._safe("úrovní", symbol, lambda: self.levels(symbol, now, cluster))
        vol_now = self._safe("volatility", symbol, lambda: self.vol_now(symbol, now, price))
        history = (
            self._safe("historie", symbol, lambda: self.history(family, symbol, cluster.ts)) or []
        )
        views = self._safe("hypotéz", symbol, lambda: self.hypothesis_views(symbol)) or {}
        stats = size_stats(history)
        n = history_count(history)
        if stats is None:
            logger.warning(
                "Upozornění před releasem %s %s: málo historie (n = %d) — "
                "chybí backfill release_moves?",
                family,
                symbol,
                n,
            )
        payload = build_preview(
            symbol,
            cluster,
            now=now,
            tz=self._tz,
            price=price,
            levels=levels,
            stats=stats,
            history_n=n,
            vol_now_bp=vol_now,
            h1=views.get("H1"),
            m1=views.get("M1"),
        )
        expected: tuple[float, float] | None = None
        if stats is not None:
            expected = (
                stats.expected_bp(vol_now)
                if vol_now is not None
                else (stats.raw_p50_bp, stats.raw_p75_bp)
            )
        record = {
            "family": family,
            "n": stats.n if stats is not None else n,
            "expected_p50_bp": expected[0] if expected else None,
            "expected_p75_bp": expected[1] if expected else None,
            "vol_now_bp": vol_now,
            "created_at": now,
        }
        return payload, record

    # ── běh ───────────────────────────────────────────────────────
    def run(self, now: dt.datetime) -> list[dict[str, Any]]:
        """Payloady `release_preview` za tento běh (většinou žádné)."""
        payloads: list[dict[str, Any]] = []
        for cluster in self.upcoming(now):
            done = self.done_stages(cluster.ts)
            cluster_payloads: list[dict[str, Any]] = []
            state_rows: list[dict[str, Any]] = []
            for symbol in self._symbols:
                due = due_stages(now, cluster.ts, done.get(symbol, set()), PREVIEW_LEADS)
                send, mark = stages_to_send(due)
                if send is None:
                    continue
                payload, record = self._payload(symbol, cluster, now)
                cluster_payloads.append(payload)
                state_rows.extend(
                    {
                        "cluster_ts": cluster.ts,
                        "symbol": symbol,
                        "stage": stage,
                        "sent": stage == send,
                        **record,
                    }
                    for stage in mark
                )
            if not state_rows:
                continue
            # Stav PŘED vrácením payloadů: po restartu se etapa nezopakuje
            self.store(state_rows)
            payloads.extend(cluster_payloads)
            logger.info(
                "Upozornění před releasem %s %s (%s): %d upozornění",
                cluster.family,
                as_utc(cluster.ts).isoformat(),
                ", ".join(sorted({str(row["stage"]) for row in state_rows if row["sent"]})),
                len(cluster_payloads),
            )
        return payloads
