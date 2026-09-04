"""Sběrač výskytů kandidáta T9 „strop nad hlavou" (#577, fáze 1 — JEN sběr).

Referenční čtení: cena přichází zespodu ke spodní hraně tlumící zóny a
očekávaná mechanika je ÚTLUM, ne odraz — proto se nekotví na strike (T1),
ale na hranu pásma z #575, a prahy nejsou v bodech ani ATR (obojí #434
zamítlo), nýbrž v geometrii pásma samotného.

Rozhodování dělá čistá funkce `detect_damping_ceiling` v `compute/setups.py`
(vedle ostatních detektorů, ale MIMO `detect_all`) — tatáž, kterou spouští
`scripts/backtest_setups.py --probes` nad historií. Tenhle modul je jen
orchestrace: okno posledních minut, zápis výskytu do `setup_probes`,
vyhodnocení otevřených sond TÝMŽ `evaluate_bar`/`r_result` jako živé
setupy a timeout na settle expirace runtime (konvence `SetupEngine`, #259).
Do `setups`, alertů ani track recordu nejde NIC — fáze 2 (≥ 30 výskytů na
instrument) teprve rozhodne zapnout/sloučit/zavřít.
"""

import datetime as dt
import logging
from collections import deque
from dataclasses import dataclass, field

from gexlens_engine.compute.bandregime import BAND_METRICS_VERSION, band_metrics, band_zone
from gexlens_engine.compute.settle import settle_ts
from gexlens_engine.compute.setups import (
    Outcome,
    ProbeMinute,
    ProbeOccurrence,
    ProbeParams,
    detect_damping_ceiling,
    evaluate_bar,
    probe_excursion,
    r_result,
)
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.runtime import EngineRuntime
from gexlens_engine.storage.probes_store import ProbeRepository

logger = logging.getLogger(__name__)


def probe_settle(expiry: str, fallback_day: dt.date) -> dt.datetime:
    """Settle expirace runtime (YYYYMMDD) — timeout sond, konvence `SetupEngine`.

    Do zavedení čisté funkce se sondy uzavíraly settlem KALENDÁŘNÍHO dne:
    výskyt po 20:00 UTC (dead-chain okno před rollem, nebo večerní Globex
    po rollu) se tak zavřel hned další minutou jako timeout (2 z 11 řádků
    produkce do 3. 9.). Živé setupy se řídí expirací (#259), sondy teď taky.
    Nečitelná expirace → settle dne (nerozbíjet sběr kvůli formátu).
    """
    try:
        day = dt.datetime.strptime(expiry, "%Y%m%d").date()
    except ValueError:
        logger.warning("Nečitelná expirace %r — timeout sond podle dne", expiry)
        day = fallback_day
    return settle_ts(day)


@dataclass
class _ActiveProbe:
    probe_id: int
    occurrence: ProbeOccurrence
    mfe: float = 0.0
    mae: float = 0.0


@dataclass
class T9ProbeCollector:
    """Per-minutový sběrač výskytů; pád nesmí shodit sběr dat (volá pipeline)."""

    symbol: str
    repository: ProbeRepository
    params: ProbeParams = field(default_factory=ProbeParams)

    _history: deque[ProbeMinute] = field(init=False)
    _active: list[_ActiveProbe] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        # Detektor potřebuje přesně 2 × akceptace minut (usazení + přechod)
        self._history = deque(maxlen=2 * self.params.acceptance_minutes)

    async def on_minute(
        self, now: dt.datetime, spot: float, bars: list[Bar], runtime: EngineRuntime
    ) -> None:
        if not bars:
            return
        bar = bars[-1]
        self._evaluate_active(now, bar.high, bar.low)
        self._timeout_at_settle(now, bar.close, runtime.expiry)

        profile = runtime.last_profile
        zone = band_zone(profile, bar.close) if profile is not None else None
        metrics = band_metrics(profile, bar.close) if profile is not None else None
        self._history.append(
            ProbeMinute(
                ts=now,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                zone=zone,
                band_depth=metrics.depth if metrics is not None else None,
            )
        )
        occurrence = detect_damping_ceiling(list(self._history), self.params)
        if occurrence is not None:
            self._open_probe(now, occurrence, runtime.expiry)

    def _open_probe(self, now: dt.datetime, occurrence: ProbeOccurrence, expiry: str) -> None:
        context = {**occurrence.context, "expiry": expiry}
        if "band_depth" in context:
            # Význam hloubky se mění mezi verzemi (#952) — bez značky by šly
            # sondy z různých verzí sdružit dohromady
            context["band_metrics_version"] = BAND_METRICS_VERSION
        probe_id = self.repository.insert(
            template=occurrence.template,
            symbol=self.symbol,
            session_date=now.date(),
            created_ts=now,
            direction=occurrence.direction.value,
            entry=occurrence.entry,
            target=occurrence.target,
            stop=occurrence.stop,
            context=context,
        )
        self._active.append(_ActiveProbe(probe_id=probe_id, occurrence=occurrence))
        logger.info(
            "T9 probe %s %s (#577): %s entry %.2f cíl %.2f stop %.2f",
            occurrence.template,
            self.symbol,
            occurrence.direction.value,
            occurrence.entry,
            occurrence.target,
            occurrence.stop,
        )

    def _evaluate_active(self, now: dt.datetime, high: float, low: float) -> None:
        still_active: list[_ActiveProbe] = []
        for probe in self._active:
            item = probe.occurrence
            favorable, adverse = probe_excursion(item.direction, item.entry, high, low)
            probe.mfe = max(probe.mfe, favorable)
            probe.mae = max(probe.mae, adverse)
            outcome = evaluate_bar(item.direction, item.entry, item.target, item.stop, high, low)
            if outcome is None:
                still_active.append(probe)
                continue
            exit_price = item.target if outcome is Outcome.TARGET else item.stop
            self.repository.close(
                probe.probe_id,
                status=outcome.value,
                closed_ts=now,
                outcome_r=r_result(item.direction, item.entry, item.stop, exit_price),
                mfe=probe.mfe,
                mae=probe.mae,
            )
        self._active = still_active

    def _timeout_at_settle(self, now: dt.datetime, close: float, expiry: str) -> None:
        """Settle expirace uzavírá vše otevřené za close — stejně jako živé setupy."""
        if not self._active or now < probe_settle(expiry, now.date()):
            return
        for probe in self._active:
            item = probe.occurrence
            self.repository.close(
                probe.probe_id,
                status=Outcome.TIMEOUT.value,
                closed_ts=now,
                outcome_r=r_result(item.direction, item.entry, item.stop, close),
                mfe=probe.mfe,
                mae=probe.mae,
            )
        self._active = []
