"""Anomální reakce (#295, SPEC 9.4 N7) — „trh reaguje silněji než obvykle".

Porovnává čerstvě změřené reakce na primárním okně s historickým rozdělením
téhož bucketu: |ret_bp| nad p90 bucketu = anomálie → notifikace do zvonku
(kanál `alerts`). p90 se počítá z živých dat `news_reactions`, ne z uložených
agregátů — `news_model_stats` percentily nenese a noční přepočet by byl až
o den pozadu za realitou.

Pinnuté detaily (SPEC hodnoty nechává otevřené):

* práh = p90 |ret_bp| bucketu, minimálně z 30 vzorků (mělčí rozdělení dává
  nesmyslné percentily a zvonek by křičel na šum),
* jen nekontaminovaná okna na primárním okně (+5 min) — kontaminované okno
  neměří reakci na tuhle zprávu (SPEC 2.4),
* každá (event, symbol) anomálie se hlásí právě jednou; po restartu se
  hodnotí jen reakce spočítané od startu procesu, starší už trader viděl,
* **hlásí se jen reakce na čerstvé události** (`MAX_EVENT_AGE`) — watermark
  sleduje čas VÝPOČTU, ne čas zprávy, takže při dopočtu historie (backfill
  #744) by zvonek dostal tisíce alertů o dva roky starých pohybech, jako by
  se staly teď. Zjištěno naživo 17. 8.: dopočet reakcí k backfillu začal
  chrlit desítky alertů za běh.
"""

import datetime as dt
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import news_events, news_reactions
from gexlens_news.model_stats import surprise_bucket
from gexlens_news.predictions import DEFAULT_PRIMARY_WINDOW_MIN

logger = logging.getLogger(__name__)

#: Jak stará smí být UDÁLOST, aby se její anomální reakce ještě hlásila do
#: zvonku. Nestačí sledovat čas výpočtu: deferred reakce (trhy zavřené, měří se
#: po otevření) legitimně vzniká i o víkendu s odstupem, takže práh musí
#: pokrýt pátek večer → pondělí ráno. Tři dny to zvládnou a zároveň spolehlivě
#: odříznou historický dopočet.
MAX_EVENT_AGE = dt.timedelta(days=3)

# Minimální hloubka bucketu pro smysluplný percentil
MIN_BUCKET_SAMPLES = 30
PERCENTILE = 0.90


def percentile_abs(values: list[float], fraction: float) -> float:
    """Empirický percentil |hodnot| (nearest-rank) — bez závislosti na numpy."""
    ordered = sorted(abs(value) for value in values)
    rank = max(0, min(len(ordered) - 1, round(fraction * (len(ordered) - 1))))
    return ordered[rank]


class AnomalyJob:
    """Hlídá čerstvé reakce proti p90 bucketu; vrací payloady pro `alerts`."""

    def __init__(
        self,
        engine: Engine,
        *,
        primary_window_min: int = DEFAULT_PRIMARY_WINDOW_MIN,
        started_at: dt.datetime | None = None,
    ) -> None:
        self._engine = engine
        self._window = primary_window_min
        # Watermark: reakce spočítané před startem procesu se nehodnotí
        self._watermark = started_at or dt.datetime.now(dt.UTC)
        self._announced: set[tuple[int, str]] = set()

    def _fresh_reactions(self, now: dt.datetime) -> list[dict[str, Any]]:
        stmt = (
            select(
                news_reactions.c.event_id,
                news_reactions.c.symbol,
                news_reactions.c.ret_bp,
                news_reactions.c.deferred,
                news_reactions.c.computed_at,
                news_events.c.title,
                news_events.c.category,
                news_events.c.importance,
                news_events.c.surprise_z,
            )
            .join(news_events, news_events.c.id == news_reactions.c.event_id)
            .where(
                news_reactions.c.computed_at >= self._watermark,
                # Watermark hlídá čas VÝPOČTU; bez druhé podmínky by dopočet
                # historie (#744) vyslal alert o každém starém pohybu
                news_events.c.ts_event >= now - MAX_EVENT_AGE,
                news_reactions.c.window_min == self._window,
                news_reactions.c.contaminated.is_(False),
                news_events.c.category.is_not(None),
                news_events.c.importance.is_not(None),
            )
        )
        with self._engine.connect() as conn:
            return [dict(row._mapping) for row in conn.execute(stmt)]

    def _bucket_returns(self, fresh: dict[str, Any]) -> list[float]:
        """Historické |ret_bp| téhož bucketu, bez hodnoceného eventu."""
        bucket = surprise_bucket(
            float(fresh["surprise_z"]) if fresh["surprise_z"] is not None else None
        )
        stmt = (
            select(news_reactions.c.ret_bp, news_events.c.surprise_z)
            .join(news_events, news_events.c.id == news_reactions.c.event_id)
            .where(
                news_reactions.c.window_min == self._window,
                news_reactions.c.symbol == fresh["symbol"],
                news_reactions.c.contaminated.is_(False),
                news_reactions.c.deferred == bool(fresh["deferred"]),
                news_reactions.c.event_id != fresh["event_id"],
                news_events.c.category == fresh["category"],
                news_events.c.importance == fresh["importance"],
            )
        )
        # Surprise bucket nejde vyjádřit v SQL (hranice v σ) — filtruje se v Pythonu
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [
            float(row.ret_bp)
            for row in rows
            if surprise_bucket(float(row.surprise_z) if row.surprise_z is not None else None)
            == bucket
        ]

    def run(self, now: dt.datetime) -> list[dict[str, Any]]:
        """Payloady `AlertMessage` pro anomální reakce; posun watermark."""
        alerts: list[dict[str, Any]] = []
        fresh_rows = self._fresh_reactions(now)
        for fresh in fresh_rows:
            key = (int(fresh["event_id"]), str(fresh["symbol"]))
            if key in self._announced:
                continue
            history = self._bucket_returns(fresh)
            if len(history) < MIN_BUCKET_SAMPLES:
                continue
            threshold = percentile_abs(history, PERCENTILE)
            ret_bp = float(fresh["ret_bp"])
            if abs(ret_bp) <= threshold:
                continue
            self._announced.add(key)
            alerts.append(
                {
                    "kind": "news_anomaly",
                    "symbol": str(fresh["symbol"]),
                    "message": (
                        f"Anomální reakce na „{fresh['title']}“: {ret_bp:+.0f} bp "
                        f"za {self._window} min překročilo p90 bucketu ({threshold:.0f} bp)"
                    ),
                    # Zvonek čte unix sekundy (AlertMessage), ne ISO
                    "ts": int(now.timestamp()),
                }
            )
        self._watermark = now
        if alerts:
            logger.info("Anomální reakce: %d alertů", len(alerts))
        return alerts
