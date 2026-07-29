"""TendencyEngine (#350): minutová orchestrace indikátoru tendence.

Běží vedle setup detektoru ze stejných vstupů minutového cyklu; každou minutu
sestaví `TendencyInputs`, vyhodnotí čisté `evaluate_tendency`, uloží řádek
(s rozpadem hlasů a verzí vah, S11) a publikuje WS kanál `tendency.{symbol}`.

Selhání čehokoli tady nesmí shodit sběr dat — volající balí do try/except.
"""

import datetime as dt
import logging
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from gexlens_engine.compute.gexfield import gamma_at_price
from gexlens_engine.compute.setups import max_pain_strike
from gexlens_engine.compute.tendency import TendencyInputs, evaluate_tendency
from gexlens_engine.runtime import EngineRuntime, PublisherLike
from gexlens_engine.storage.oi_archive import OIEodRepository
from gexlens_engine.storage.tendency_store import TendencyRepository, votes_payload

logger = logging.getLogger(__name__)

# Okno sklonů (Cum Δ, cena, SentIndex) — shodné s CumΔ oknem signal enginu
SLOPE_LOOKBACK_MIN = 10
SENTIMENT_SUBDIR = "sentiment"


@dataclass
class TendencyEngine:
    symbol: str
    repository: TendencyRepository
    oi_repository: OIEodRepository
    publisher: PublisherLike
    data_dir: Path | None = None

    def __post_init__(self) -> None:
        # Historie (cum_delta, close) pro sklon a rozchod; +1 na okrajovou minutu
        self._history: deque[tuple[dt.datetime, float, float]] = deque(
            maxlen=SLOPE_LOOKBACK_MIN + 1
        )
        self._prev_volumes: dict[object, float] = {}
        self._max_pain: float | None = None
        self._max_pain_loaded_for: tuple[str, dt.date] | None = None
        # Cache dnešní parquet řady SentIndexu (přepisuje se celá každý cyklus)
        self._sent_cache: tuple[Path, float, list[tuple[dt.datetime, float]]] | None = None

    def _refresh_max_pain(self, expiry: str, today: dt.date) -> None:
        if self._max_pain_loaded_for == (expiry, today) and self._max_pain is not None:
            return
        records = self.oi_repository.values_for(self.symbol, expiry, today)
        oi_map = {(r.strike, r.right): r.oi for r in records}
        self._max_pain = max_pain_strike(oi_map)
        self._max_pain_loaded_for = (expiry, today)

    def _flows(self, runtime: EngineRuntime) -> tuple[float | None, float | None]:
        """Δ-vážené přírůstky volume per strana — vlastní stav diffu.

        Nesdílí `_prev_volumes` se SetupEngine: dva konzumenti nad jedním
        stavem by si navzájem nulovali přírůstky.
        """
        call_flow = put_flow = 0.0
        seen = False
        for spec, cached in runtime.scheduler.quotes().items():
            snapshot = cached.snapshot
            previous = self._prev_volumes.get(spec)
            self._prev_volumes[spec] = snapshot.volume
            if previous is None:
                continue
            seen = True
            increment = snapshot.volume - previous
            if increment <= 0:
                continue
            weighted = increment * abs(snapshot.delta)
            if spec.right == "C":
                call_flow += weighted
            else:
                put_flow += weighted
        if not seen:
            return None, None
        return call_flow, put_flow

    def _sentiment(self, now: dt.datetime) -> tuple[float | None, float | None]:
        """Aktuální hodnota SentIndexu + hodnota před oknem z denní parquet partice."""
        if self.data_dir is None:
            return None, None
        path = self.data_dir / "derived" / SENTIMENT_SUBDIR / f"{now.date().isoformat()}.parquet"
        try:
            if not path.exists():
                return None, None
            mtime = path.stat().st_mtime
            if (
                self._sent_cache is None
                or self._sent_cache[0] != path
                or self._sent_cache[1] != mtime
            ):  # noqa: E501
                import pyarrow.parquet as pq

                rows = pq.read_table(path).to_pylist()
                series = [(row["ts_min"], float(row["value"])) for row in rows]
                self._sent_cache = (path, mtime, series)
            series = self._sent_cache[2]
        except Exception:
            logger.exception("SentIndex partice %s nečitelná — složka bez dat", path)
            return None, None
        if not series:
            return None, None
        current = series[-1][1]
        threshold = now - dt.timedelta(minutes=SLOPE_LOOKBACK_MIN)
        then = None
        for ts_min, value in reversed(series):
            if ts_min <= threshold:
                then = value
                break
        return current, then

    async def on_minute(self, now: dt.datetime, spot: float, runtime: EngineRuntime) -> None:
        levels = runtime.last_gex_levels
        flow = runtime.last_flow
        self._refresh_max_pain(runtime.expiry, now.date())
        call_flow, put_flow = self._flows(runtime)
        sent_value, sent_then = self._sentiment(now)

        cum_now = flow.cum_delta if flow else None
        cum_then: float | None = None
        price_then: float | None = None
        if self._history:
            oldest = self._history[0]
            # Sklon má smysl až s plným oknem — jinak by ranní minuty srovnávaly
            # proti startu procesu a hlasovaly ze šumu
            if now - oldest[0] >= dt.timedelta(minutes=SLOPE_LOOKBACK_MIN):
                cum_then = oldest[1]
                price_then = oldest[2]
        if cum_now is not None:
            self._history.append((now, cum_now, spot))

        profile = runtime.last_profile
        inputs = TendencyInputs(
            ts_min=now,
            spot=spot,
            flip=levels.flip if levels else None,
            call_wall=levels.call_wall if levels else None,
            put_wall=levels.put_wall if levels else None,
            call_wall_dom=levels.call_wall_dom if levels else None,
            put_wall_dom=levels.put_wall_dom if levels else None,
            max_pain=self._max_pain,
            centroid=levels.centroid if levels else None,
            cum_delta_now=cum_now,
            cum_delta_then=cum_then,
            price_then=price_then,
            call_flow=call_flow,
            put_flow=put_flow,
            sent_value=sent_value,
            sent_value_then=sent_then,
            gamma_at_price=gamma_at_price(profile, spot) if profile else None,
        )
        result = evaluate_tendency(inputs)
        if result is None:
            return
        self.repository.upsert(self.symbol, result)
        await self.publisher.publish(
            f"tendency.{self.symbol}",
            {
                "ts_min": now.isoformat(),
                "symbol": self.symbol,
                "score": result.score,
                "band": result.band,
                "votes": votes_payload(result),
                "weights_version": result.weights_version,
            },
        )
