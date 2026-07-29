"""Job sentiment vln a stavu RiskOn/RiskOff/Neutral (#292, SPEC 5.6).

Pravidla žijí v `gexlens_engine.compute.sentwaves` (sdílené s API — jedna
implementace, žádný dvojí výklad). Tenhle job je drží aktuální v DB:

* přepočítá vlny z denních close (`sentiment_daily`) a uloží je do
  `sentiment_waves` (full-replace per symbol — vln jsou desítky a recompute
  z čisté řady je bezpečnější než inkrementální údržba),
* spočítá potvrzený stav (jen UZAVŘENÉ dny — přechody se detekují na denním
  close, SPEC 5.6) a intradenní „unconfirmed" indikaci z dnešní průběžné
  hodnoty,
* změnu stavu hlásí volajícímu, který ji publikuje do WS `sentiment.state`.
"""

import datetime as dt
import logging
from typing import Any

from sqlalchemy import delete, insert, select
from sqlalchemy.engine import Engine

from gexlens_engine.compute.sentwaves import DailyClose, Wave, assess_state, detect_waves
from gexlens_engine.storage.sentiment import sentiment_daily, sentiment_waves

logger = logging.getLogger(__name__)


def wave_payload(wave: Wave | None) -> dict[str, Any] | None:
    if wave is None:
        return None
    return {
        "direction": wave.direction,
        "start_date": wave.start.isoformat(),
        "end_date": wave.end.isoformat() if wave.end else None,
        "depth": wave.depth,
        "length_days": wave.length_days,
    }


class WavesJob:
    """Přepočet vln + stavu; `last_payload` drží poslední publikovaný stav."""

    def __init__(self, engine: Engine, *, symbol: str = "ES") -> None:
        self._engine = engine
        self._symbol = symbol
        self.last_payload: dict[str, Any] | None = None

    def _points(self, today: dt.date) -> tuple[list[DailyClose], DailyClose | None]:
        """(uzavřené dny, dnešní průběžný close) — dnešek se do vln nepočítá."""
        stmt = (
            select(sentiment_daily.c.date, sentiment_daily.c.close)
            .where(sentiment_daily.c.symbol == self._symbol)
            .order_by(sentiment_daily.c.date)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        completed = [
            DailyClose(date=row.date, close=float(row.close)) for row in rows if row.date < today
        ]
        provisional = next(
            (
                DailyClose(date=row.date, close=float(row.close))
                for row in rows
                if row.date == today
            ),
            None,
        )
        return completed, provisional

    def _store(self, waves: list[Wave]) -> None:
        """Full-replace vln symbolu — id nejsou nikde referencovaná (bez FK)."""
        with self._engine.begin() as conn:
            conn.execute(delete(sentiment_waves).where(sentiment_waves.c.symbol == self._symbol))
            if waves:
                conn.execute(
                    insert(sentiment_waves),
                    [
                        {
                            "symbol": self._symbol,
                            "direction": wave.direction,
                            "start_date": wave.start,
                            "end_date": wave.end,
                            "depth": wave.depth,
                            "length_days": wave.length_days,
                        }
                        for wave in waves
                    ],
                )

    def run(self, now: dt.datetime) -> tuple[dict[str, Any], bool]:
        """Přepočet; vrací (payload stavu, změnil se proti poslednímu?)."""
        today = now.date()
        completed, provisional = self._points(today)
        waves = detect_waves(completed)
        self._store(waves)

        confirmed = assess_state(completed)
        provisional_assessment = (
            assess_state([*completed, provisional]) if provisional is not None else confirmed
        )
        payload: dict[str, Any] = {
            "symbol": self._symbol,
            "state": confirmed.state,
            # Unconfirmed indikace (SPEC 5.6): dnešní průběžná hodnota by stav
            # změnila, ale potvrdit ho smí až denní close
            "unconfirmed": provisional is not None
            and provisional_assessment.state != confirmed.state,
            "unconfirmed_state": provisional_assessment.state,
            "last_close": provisional.close if provisional else confirmed.close,
            "ma5": confirmed.ma5,
            "ma10": confirmed.ma10,
            "threshold": confirmed.threshold,
            "current_wave": wave_payload(confirmed.wave),
            "ts": now.isoformat(),
        }
        comparable = {k: v for k, v in payload.items() if k != "ts"}
        previous = (
            {k: v for k, v in self.last_payload.items() if k != "ts"} if self.last_payload else None
        )
        changed = comparable != previous
        self.last_payload = payload
        if changed:
            logger.info(
                "Sentiment stav %s: %s (unconfirmed=%s, vln %d)",
                self._symbol,
                payload["state"],
                payload["unconfirmed"],
                len(waves),
            )
        return payload, changed
