"""Drift hlídka (#403): alert, když model přestává platit.

Track record degradaci edge ukáže, ale mlčky — tenhle noční job porovná
klouzavou úspěšnost posledních výsledků s dlouhodobou a při statisticky
významném poklesu to řekne nahlas (zvonek + badge ve Stats).

V1 = jen viditelnost, žádné automatické zásahy: zavírání gate nechává na
stávajícím mechanismu (klesající hit-rate ho zavře přirozeně) — hlídka jen
zkracuje dobu, po kterou by člověk věřil číslům, která už neplatí.

Falešné poplachy: testují se POUZE buckety s otevřeným gate (dnes jednotky,
ne stovky) a setup šablony s dostatečnou historií — bez toho by p < 0,05
křičelo náhodou při každém přepočtu.
"""

import datetime as dt
import logging
import math
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine

from gexlens_engine.compute.setups import SETUP_MECHANICS_VERSION
from gexlens_engine.storage.meta import settings_table
from gexlens_engine.storage.sentiment import news_events, news_model_stats, news_reactions
from gexlens_engine.storage.setups_store import setups_table
from gexlens_news.predictions import DEFAULT_PRIMARY_WINDOW_MIN
from gexlens_news.signal_engine import GATE_MIN_SAMPLES, GATE_WILSON_LB

logger = logging.getLogger(__name__)

# Klouzavé okno posledních výsledků a práh významnosti poklesu
RECENT_N = 20
P_THRESHOLD = 0.05
# Minimální historie setup šablony, aby test dával smysl
SETUP_MIN_CLOSED = 30

DRIFT_SETTINGS_KEY = "drift_state"


@dataclass(frozen=True)
class DriftFinding:
    """Jeden nález — model, jehož poslední výsledky se rozešly s historií."""

    kind: str  # news_bucket | setup_template
    key: str  # identifikace pro badge ve Stats
    label: str  # lidský popis do alertu
    symbol: str
    longterm_rate: float
    recent_rate: float
    recent_n: int
    p_value: float


def binomial_p_at_most(hits: int, n: int, p: float) -> float:
    """P(X ≤ hits) pro X ~ Bin(n, p) — jednostranný test poklesu úspěšnosti."""
    if n <= 0 or not 0 < p < 1:
        return 1.0
    total = 0.0
    for k in range(0, min(hits, n) + 1):
        total += math.comb(n, k) * (p**k) * ((1 - p) ** (n - k))
    return min(1.0, total)


class DriftJob:
    """Noční porovnání klouzavé úspěšnosti s dlouhodobou; plní settings + alerty."""

    def __init__(
        self,
        engine: Engine,
        *,
        primary_window_min: int = DEFAULT_PRIMARY_WINDOW_MIN,
    ) -> None:
        self._engine = engine
        self._window = primary_window_min

    def _open_gate_buckets(self) -> list[Any]:
        stmt = select(news_model_stats).where(
            news_model_stats.c.regime == "all",
            news_model_stats.c.window_min == self._window,
            news_model_stats.c.n >= GATE_MIN_SAMPLES,
            news_model_stats.c.hit_rate_lb > GATE_WILSON_LB,
            news_model_stats.c.hit_rate.is_not(None),
        )
        with self._engine.connect() as conn:
            return list(conn.execute(stmt).fetchall())

    def _recent_bucket_hits(self, bucket: Any) -> tuple[int, int]:
        """(zásahy, n) posledních RECENT_N klasifikovaných reakcí bucketu."""
        from gexlens_news.model_stats import surprise_bucket

        stmt = (
            select(
                news_reactions.c.ret_bp,
                news_events.c.sentiment_dir,
                news_events.c.surprise_z,
            )
            .select_from(
                news_reactions.join(news_events, news_events.c.id == news_reactions.c.event_id)
            )
            .where(
                news_events.c.category == bucket.category,
                news_events.c.importance == bucket.importance,
                news_events.c.sentiment_dir.is_not(None),
                news_events.c.sentiment_dir != 0,
                news_reactions.c.symbol == bucket.symbol,
                news_reactions.c.window_min == self._window,
                news_reactions.c.contaminated.is_(False),
                news_reactions.c.deferred == bucket.deferred,
            )
            .order_by(news_events.c.ts_event.desc())
            .limit(RECENT_N * 3)  # rezerva na surprise filtr níže
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        hits = total = 0
        for row in rows:
            if total >= RECENT_N:
                break
            surprise = float(row.surprise_z) if row.surprise_z is not None else None
            if surprise_bucket(surprise) != bucket.surprise_bucket:
                continue
            total += 1
            direction = 1 if float(row.ret_bp) > 0 else -1 if float(row.ret_bp) < 0 else 0
            if direction == int(row.sentiment_dir):
                hits += 1
        return hits, total

    def _news_findings(self) -> list[DriftFinding]:
        findings: list[DriftFinding] = []
        for bucket in self._open_gate_buckets():
            hits, total = self._recent_bucket_hits(bucket)
            if total < RECENT_N:
                continue  # málo čerstvých výsledků — není co testovat
            longterm = float(bucket.hit_rate)
            p_value = binomial_p_at_most(hits, total, longterm)
            if p_value >= P_THRESHOLD:
                continue
            key = f"news:{bucket.category}|{bucket.importance}|{bucket.surprise_bucket}|{bucket.deferred}|{bucket.symbol}"  # noqa: E501
            findings.append(
                DriftFinding(
                    kind="news_bucket",
                    key=key,
                    label=f"bucket {bucket.category}/{bucket.importance}/{bucket.surprise_bucket}",
                    symbol=str(bucket.symbol),
                    longterm_rate=longterm,
                    recent_rate=hits / total,
                    recent_n=total,
                    p_value=p_value,
                )
            )
        return findings

    def _setup_findings(self) -> list[DriftFinding]:
        # Jen aktuální mechanika (#496, konvence z #311): míchat výsledky různých
        # systémů = alertovat na něco, co už neexistuje — v1 baseline navíc nesla
        # incident se zmrzlými Greeks 26.–27. 7. (ADR-0015). Řazení podle
        # `closed_ts`: „posledních 20" mají být poslední UZAVŘENÉ, ne založené.
        stmt = (
            select(
                setups_table.c.symbol,
                setups_table.c.template,
                setups_table.c.status,
                setups_table.c.closed_ts,
            )
            .where(
                setups_table.c.status.in_(("closed_target", "closed_stop")),
                setups_table.c.mechanics_version == SETUP_MECHANICS_VERSION,
            )
            .order_by(setups_table.c.closed_ts.desc())
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        grouped: dict[tuple[str, str], list[bool]] = {}
        for row in rows:
            grouped.setdefault((str(row.symbol), str(row.template)), []).append(
                row.status == "closed_target"
            )
        findings: list[DriftFinding] = []
        for (symbol, template), outcomes in grouped.items():
            if len(outcomes) < SETUP_MIN_CLOSED + RECENT_N:
                continue  # dlouhodobá základna se nesmí překrývat s klouzavým oknem
            recent = outcomes[:RECENT_N]  # řazeno desc → nejnovější první
            baseline = outcomes[RECENT_N:]
            longterm = sum(baseline) / len(baseline)
            if not 0 < longterm < 1:
                continue
            hits = sum(recent)
            p_value = binomial_p_at_most(hits, len(recent), longterm)
            if p_value >= P_THRESHOLD:
                continue
            findings.append(
                DriftFinding(
                    kind="setup_template",
                    key=f"setup:{template}|{symbol}",
                    label=f"šablona {template}",
                    symbol=symbol,
                    longterm_rate=longterm,
                    recent_rate=hits / len(recent),
                    recent_n=len(recent),
                    p_value=p_value,
                )
            )
        return findings

    def _previous_keys(self) -> set[str]:
        stmt = select(settings_table.c.value).where(settings_table.c.key == DRIFT_SETTINGS_KEY)
        with self._engine.connect() as conn:
            row = conn.execute(stmt).first()
        if row is None or not isinstance(row.value, dict):
            return set()
        return {str(item.get("key")) for item in row.value.get("findings", [])}

    def _store(self, findings: list[DriftFinding], now: dt.datetime) -> None:
        payload = {
            "computed_at": now.isoformat(),
            "findings": [asdict(finding) for finding in findings],
        }
        with self._engine.begin() as conn:
            updated = conn.execute(
                update(settings_table)
                .where(settings_table.c.key == DRIFT_SETTINGS_KEY)
                .values(value=payload)
            )
            if updated.rowcount == 0:
                conn.execute(insert(settings_table).values(key=DRIFT_SETTINGS_KEY, value=payload))

    def run(self, now: dt.datetime) -> list[dict[str, Any]]:
        """Přepočet nálezů; vrací alerty jen pro NOVÉ nálezy (anti-spam)."""
        previous = self._previous_keys()
        findings = self._news_findings() + self._setup_findings()
        self._store(findings, now)
        alerts: list[dict[str, Any]] = []
        for finding in findings:
            if finding.key in previous:
                continue
            alerts.append(
                {
                    "kind": "drift",
                    "symbol": finding.symbol,
                    "message": (
                        f"⚠ Drift: {finding.label} — posledních {finding.recent_n} výsledků "
                        f"{finding.recent_rate:.0%} vs. dlouhodobých {finding.longterm_rate:.0%} "
                        f"(p={finding.p_value:.3f}) — model v tomto vzorci přestává platit"
                    ),
                    "ts": now.timestamp(),
                }
            )
        if findings:
            logger.info("Drift hlídka: %d nálezů (%d nových)", len(findings), len(alerts))
        return alerts
