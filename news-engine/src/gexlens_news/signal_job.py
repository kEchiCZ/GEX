"""Zápis, expirace a vyhodnocování signálů (#294, SPEC kap. 6, S9).

Čistá pravidla žijí v `signal_engine`; tady je I/O okolo nich:

* kandidátní eventy (čerstvé, skórované) × aktuální potvrzený stav (#292)
  × bucket statistiky (`news_model_stats`, primární okno) → nové signály,
* dedup per (event, mode) — týž event nesmí signál založit dvakrát,
* GEX kontext pro COMBINED z parquet vrstev enginu (levels + bars + flow),
* **potvrzená** změna stavu expiruje aktivní signály (unconfirmed jen
  badge v UI, SPEC 6.3) — `expiry_ts` je lifecycle pole, `inputs` immutable,
* vyhodnocení à la prediction outcomes: realizovaný pohyb v oknech po
  signálu → `signal_outcomes` (srovnání NEWS vs. COMBINED edge),
* rozpad odpadlých kandidátů po filtrech do logu (#453) — prázdná `signals`
  jinak vypadá stejně jako rozbitý job.
"""

import datetime as dt
import logging
from collections import Counter
from typing import Any

import pyarrow.parquet as pq
from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import (
    news_classifications,
    news_events,
    news_model_stats,
    signal_outcomes,
    signals,
)
from gexlens_news.bars import BarsRepository
from gexlens_news.model_stats import surprise_bucket
from gexlens_news.predictions import DEFAULT_PRIMARY_WINDOW_MIN
from gexlens_news.reactions import DEFAULT_WINDOWS
from gexlens_news.signal_engine import (
    BucketStats,
    GexContext,
    SignalEvent,
    explain_event,
    gate_passes,
)

logger = logging.getLogger(__name__)

# Jak daleko zpět hledat kandidátní eventy — nejdelší τ je 6 h (GEOPOLITICS
# importance 3), starší event už čerstvostí (≤ τ) neprojde nikdy
CANDIDATE_LOOKBACK_HOURS = 8
# Sklon CumΔ se měří přes tohle okno (SPEC 6.1 „směr a sklon CumΔ")
CUM_DELTA_SLOPE_MINUTES = 10

# Důvody odpadnutí, které patří jobu (ne čistým pravidlům) — viz #453
REJECT_STATE = "stav"  # stav není RiskOn/RiskOff, negeneruje se vůbec
REJECT_DEDUP = "dedup"  # týž (event, mode) už signál založil


def load_gex_context(data_dir: Any, symbol: str, now: dt.datetime) -> GexContext | None:
    """GEX kontext z parquet vrstev enginu; None = kontext není k dispozici.

    Čte aktivní expiraci (nejbližší ≥ dnešek s dnešní particí levels),
    poslední bar podkladu (spot) a sklon CumΔ za posledních N minut.
    """
    from pathlib import Path

    base = Path(data_dir)
    today = now.date()
    bars_path = base / "derived" / symbol / "bars" / f"{today.isoformat()}.parquet"
    if not bars_path.exists():
        return None
    try:
        bar_rows = pq.read_table(bars_path, columns=["ts_min", "close"]).to_pylist()
    except Exception:
        logger.exception("GEX kontext: bary %s nečitelné", bars_path)
        return None
    if not bar_rows:
        return None
    spot = float(max(bar_rows, key=lambda row: row["ts_min"])["close"])

    flip: float | None = None
    symbol_dir = base / "derived" / symbol
    today_compact = today.strftime("%Y%m%d")
    expiries = sorted(
        path.name
        for path in symbol_dir.iterdir()
        if path.is_dir() and path.name.isdigit() and path.name >= today_compact
    )
    for expiry in expiries:
        levels_path = symbol_dir / expiry / "levels" / f"{today.isoformat()}.parquet"
        if not levels_path.exists():
            continue
        try:
            levels_rows = pq.read_table(levels_path, columns=["ts_min", "flip"]).to_pylist()
        except Exception:
            logger.exception("GEX kontext: levels %s nečitelné", levels_path)
            break
        if levels_rows:
            last = max(levels_rows, key=lambda row: row["ts_min"])
            flip = float(last["flip"]) if last["flip"] is not None else None
        break

    slope: float | None = None
    flow_path = base / "derived" / symbol / "flow" / f"{today.isoformat()}.parquet"
    if flow_path.exists():
        try:
            flow_rows = pq.read_table(flow_path, columns=["ts_min", "cum_delta"]).to_pylist()
        except Exception:
            logger.exception("GEX kontext: flow %s nečitelný", flow_path)
            flow_rows = []
        if flow_rows:
            flow_rows.sort(key=lambda row: row["ts_min"])
            last_row = flow_rows[-1]
            cutoff = last_row["ts_min"] - dt.timedelta(minutes=CUM_DELTA_SLOPE_MINUTES)
            earlier = [row for row in flow_rows if row["ts_min"] <= cutoff]
            if earlier:
                slope = float(last_row["cum_delta"]) - float(earlier[-1]["cum_delta"])

    return GexContext(spot=spot, flip=flip, cum_delta_slope=slope)


class SignalJob:
    """Always-on výpočet signálů (S9); volá se po WavesJob v reaction_loop."""

    def __init__(
        self,
        engine: Engine,
        data_dir: Any,
        *,
        symbols: tuple[str, ...] = ("ES",),
        primary_window_min: int = DEFAULT_PRIMARY_WINDOW_MIN,
    ) -> None:
        self._engine = engine
        self._data_dir = data_dir
        self._symbols = symbols
        self._primary_window = primary_window_min
        self._bars = BarsRepository(data_dir)
        # Potvrzený stav per symbol (ADR-0026) — expirace se řídí stavem
        # SVÉHO symbolu, ne globálem
        self._last_confirmed_state: dict[str, str] = {}
        # Nové signály posledního běhu — volající je pushne do WS `signals`
        self.last_created: list[dict[str, Any]] = []
        # Rozpad posledního běhu: důvod odpadnutí → počet (#453)
        self.last_rejects: Counter[str] = Counter()

    # ── Kandidáti a statistiky ─────────────────────────────────────

    def _candidate_events(self, now: dt.datetime) -> list[SignalEvent]:
        since = now - dt.timedelta(hours=CANDIDATE_LOOKBACK_HOURS)
        stmt = (
            select(
                news_events.c.id,
                news_events.c.ts_event,
                news_events.c.category,
                news_events.c.importance,
                news_events.c.sentiment_score,
                news_events.c.surprise_z,
                news_events.c.market_closed,
            )
            .where(
                news_events.c.ts_event >= since,
                news_events.c.ts_event <= now,
                news_events.c.sentiment_score.is_not(None),
                news_events.c.category.is_not(None),
            )
            .order_by(news_events.c.ts_event)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
            versions = {
                int(event_id): int(version)
                for event_id, version in conn.execute(
                    select(
                        news_classifications.c.event_id,
                        func.max(news_classifications.c.version),
                    )
                    .where(news_classifications.c.event_id.in_([int(r.id) for r in rows]))
                    .group_by(news_classifications.c.event_id)
                )
            }
        events = []
        for row in rows:
            if not row.sentiment_score:
                continue
            ts = row.ts_event if row.ts_event.tzinfo else row.ts_event.replace(tzinfo=dt.UTC)
            events.append(
                SignalEvent(
                    event_id=int(row.id),
                    ts_event=ts,
                    category=row.category,
                    importance=int(row.importance or 1),
                    score=float(row.sentiment_score),
                    surprise_bucket=surprise_bucket(
                        float(row.surprise_z) if row.surprise_z is not None else None
                    ),
                    deferred=bool(row.market_closed),
                    classification_version=versions.get(int(row.id)),
                )
            )
        return events

    def _bucket_stats(self, event: SignalEvent, symbol: str, state: str) -> BucketStats | None:
        """Bucket na primárním okně; preferuje pohled podmíněný stavem (#402).

        Režimový bucket se použije, jen když SÁM projde Wilson gate — jinak
        fallback na nepodmíněný 'all'. Horší než dosavadní chování to být
        nemůže, jen přesnější tam, kde na to podmíněná větev má data.
        """
        stmt = select(news_model_stats).where(
            news_model_stats.c.category == event.category,
            news_model_stats.c.importance == event.importance,
            news_model_stats.c.surprise_bucket == event.surprise_bucket,
            news_model_stats.c.deferred == event.deferred,
            news_model_stats.c.window_min == self._primary_window,
            news_model_stats.c.symbol == symbol,
            news_model_stats.c.regime.in_((state, "all")),
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        by_regime = {row.regime: row for row in rows}

        def to_stats(row: Any) -> BucketStats:
            return BucketStats(
                n=int(row.n),
                hit_rate_lb=float(row.hit_rate_lb) if row.hit_rate_lb is not None else None,
                ret_mean_bp=float(row.ret_mean_bp),
                window_min=int(row.window_min),
                regime=str(row.regime),
            )

        conditional = by_regime.get(state)
        if conditional is not None:
            stats = to_stats(conditional)
            if gate_passes(stats):
                return stats
        fallback = by_regime.get("all")
        return to_stats(fallback) if fallback is not None else None

    def _already_signalled(self, now: dt.datetime) -> set[tuple[int, str]]:
        """(event_id, mode) páry z nedávných signálů — dedup (anti-spam)."""
        since = now - dt.timedelta(hours=CANDIDATE_LOOKBACK_HOURS + 1)
        stmt = select(signals.c.mode, signals.c.inputs).where(signals.c.ts >= since)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        seen: set[tuple[int, str]] = set()
        for row in rows:
            event_id = (row.inputs or {}).get("event_id")
            if event_id is not None:
                seen.add((int(event_id), row.mode))
        return seen

    # ── Lifecycle ──────────────────────────────────────────────────

    def _expire_on_state_change(self, symbol: str, state: str, now: dt.datetime) -> None:
        """Potvrzená změna stavu symbolu expiruje JEHO aktivní signály (SPEC 6.3)."""
        previous = self._last_confirmed_state.get(symbol)
        self._last_confirmed_state[symbol] = state
        if previous is None or state == previous:
            return
        with self._engine.begin() as conn:
            result = conn.execute(
                update(signals)
                .where(signals.c.symbol == symbol, signals.c.expiry_ts > now)
                .values(expiry_ts=now)
            )
        if result.rowcount:
            logger.info(
                "Změna stavu %s %s → %s: expirováno %d aktivních signálů",
                symbol,
                previous,
                state,
                result.rowcount,
            )

    # ── Vyhodnocení (à la prediction outcomes) ─────────────────────

    def _evaluate_outcomes(self, now: dt.datetime) -> int:
        """Realizovaný pohyb v oknech po signálu → `signal_outcomes`."""
        done = select(signal_outcomes.c.signal_id, signal_outcomes.c.window_min)
        with self._engine.connect() as conn:
            existing = {(int(r.signal_id), int(r.window_min)) for r in conn.execute(done)}
            rows = conn.execute(
                select(signals.c.id, signals.c.ts, signals.c.symbol, signals.c.direction).where(
                    signals.c.ts >= now - dt.timedelta(days=3)
                )
            ).fetchall()

        written = 0
        for row in rows:
            ts = row.ts if row.ts.tzinfo else row.ts.replace(tzinfo=dt.UTC)
            bars = None
            for window in DEFAULT_WINDOWS:
                if (int(row.id), window) in existing:
                    continue
                window_end = ts + dt.timedelta(minutes=window)
                if window_end > now:
                    continue  # okno ještě neuzavřené
                if bars is None:
                    bars = self._bars.load_range(
                        row.symbol, ts - dt.timedelta(minutes=5), ts + dt.timedelta(minutes=65)
                    )
                start_bar = max((b for b in bars if b.ts <= ts), key=lambda b: b.ts, default=None)
                end_bar = max(
                    (b for b in bars if b.ts <= window_end), key=lambda b: b.ts, default=None
                )
                if start_bar is None or end_bar is None or start_bar.ts == end_bar.ts:
                    continue
                ret_bp = (end_bar.close - start_bar.close) / start_bar.close * 10_000
                realized = 1 if ret_bp > 0 else -1 if ret_bp < 0 else 0
                wanted = 1 if row.direction == "long" else -1
                with self._engine.begin() as conn:
                    conn.execute(
                        insert(signal_outcomes).values(
                            signal_id=int(row.id),
                            window_min=window,
                            ret_bp=ret_bp,
                            realized_dir=realized,
                            correct=realized == wanted,
                            computed_at=now,
                        )
                    )
                written += 1
        return written

    # ── Hlavní běh ─────────────────────────────────────────────────

    def _log_cycle(self, states: dict[str, str], candidates: int, created: int) -> None:
        """Jeden řádek na cyklus: kolik kandidátů odpadlo a na kterém filtru.

        Bez tohohle rozpadu se prázdná `signals` nedá odlišit od poruchy (#453);
        kandidáti se proto načítají i ve stavu, kdy se negeneruje.
        """
        if not candidates:
            return
        breakdown = ", ".join(f"{reason}={n}" for reason, n in sorted(self.last_rejects.items()))
        state_label = ", ".join(f"{sym}={states.get(sym, '?')}" for sym in self._symbols)
        logger.info(
            "Signály: %d nových z %d kandidátů (stav %s); odpadlo: %s",
            created,
            candidates,
            state_label,
            breakdown or "nic",
        )

    def run(self, now: dt.datetime, *, states: dict[str, str]) -> int:
        """Jeden cyklus; vrací počet nových signálů (`last_created` pro WS).

        `states` mapuje symbol → potvrzený stav JEHO řady (ADR-0026). Symbol
        bez stavu v mapě se přeskočí přes filtr stavu — NQ signál proti ES
        režimu byla přesně vada, kterou #453 doložil.
        """
        self.last_created = []
        self.last_rejects = Counter()

        created = 0
        events = self._candidate_events(now)
        seen: set[tuple[int, str]] | None = None
        for symbol in self._symbols:
            state = states.get(symbol, "Neutral")
            self._expire_on_state_change(symbol, state, now)
            if state not in ("RiskOn", "RiskOff"):
                # SPEC 6.3: mimo RiskOn/RiskOff se negeneruje — ale ať je
                # vidět, kolik kandidátů na tomhle jediném filtru zůstalo
                self.last_rejects[REJECT_STATE] += len(events)
                continue
            if seen is None:
                seen = self._already_signalled(now) if events else set()
            gex = load_gex_context(self._data_dir, symbol, now)
            for event in events:
                stats = self._bucket_stats(event, symbol, state)
                candidates, reasons = explain_event(
                    event, state=state, stats=stats, now=now, gex=gex
                )
                for reason in reasons:
                    self.last_rejects[reason] += 1
                for candidate in candidates:
                    if (event.event_id, candidate.mode) in seen:
                        self.last_rejects[REJECT_DEDUP] += 1
                        continue
                    row = {
                        "ts": now,
                        "symbol": symbol,
                        "direction": candidate.direction,
                        "strength": candidate.strength,
                        "mode": candidate.mode,
                        # `state_symbol` (ADR-0026): z čí řady stav pochází —
                        # bez toho signál není zpětně vysvětlitelný (SPEC 6.3)
                        "inputs": {**candidate.inputs, "state_symbol": symbol},
                        "expiry_ts": candidate.expiry_ts,
                    }
                    with self._engine.begin() as conn:
                        signal_id = conn.execute(
                            insert(signals).values(**row).returning(signals.c.id)
                        ).scalar()
                    seen.add((event.event_id, candidate.mode))
                    created += 1
                    self.last_created.append(
                        {
                            **row,
                            "id": signal_id,
                            "ts": now.isoformat(),
                            "expiry_ts": candidate.expiry_ts.isoformat(),
                        }
                    )
        self._log_cycle(states, len(events) * len(self._symbols), created)
        self._evaluate_outcomes(now)
        return created
