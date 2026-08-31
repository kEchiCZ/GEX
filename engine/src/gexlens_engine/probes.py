"""Kandidát T9 „strop nad hlavou" (#577, fáze 1 — JEN sběr výskytů).

Referenční čtení: cena přichází zespodu ke spodní hraně tlumící zóny a
očekávaná mechanika je ÚTLUM, ne odraz — proto se nekotví na strike (T1),
ale na hranu pásma z #575, a prahy nejsou v bodech ani ATR (obojí #434
zamítlo), nýbrž v geometrii pásma samotného.

Dva zrcadlové kandidáty, oba jen hypotetické obchody do `setup_probes`:

- **t9_ceiling** — vstup do pásma zespodu (outside → transition, cena roste,
  jádro pásma nad hlavou). Hypotéza dle tabulky v #577: cena dojde ke STŘEDU
  tlumící zóny (tam ji drží jádro) → LONG od hrany, cíl = střed zóny, stop
  = hrana All − ¼ šířky zóny (návrat pod hranu = přechod odmítnut).
- **t9_exit** — výpad z pásma dolů (transition/inside → outside, cena klesá).
  Momentum hypotéza: SHORT, cíl = entry − šířka zóny, stop = návrat nad
  hranu All.

Akceptace: přechod musí vydržet `ACCEPTANCE_MINUTES` minut (konvence T2),
jinak se kandidát zahazuje — „okamžitě vrácený přechod" není vstup do pásma.
Výsledky počítá TENTÝŽ `evaluate_bar`/`r_result` jako živé setupy; timeout
na settle seance. Do `setups`, alertů ani track recordu nejde NIC — fáze 2
(≥ 30 výskytů na instrument) teprve rozhodne zapnout/sloučit/zavřít.
"""

import datetime as dt
import logging
from dataclasses import dataclass, field

from gexlens_engine.compute.bandregime import (
    BAND_MAJOR_SHARE,
    BAND_METRICS_VERSION,
    BandZone,
    band_metrics,
    band_zone,
)
from gexlens_engine.compute.settle import settle_ts
from gexlens_engine.compute.setups import Direction, evaluate_bar, r_result
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.runtime import EngineRuntime
from gexlens_engine.storage.probes_store import ProbeRepository

logger = logging.getLogger(__name__)

#: Akceptace přechodu (minuty) — stejná konvence jako `acceptance_minutes` T2
ACCEPTANCE_MINUTES = 5
#: Podmínka 2 z #577: jádro pásma nad cenou ≥ podíl globálního maxima profilu
STRENGTH_ABOVE_MIN = BAND_MAJOR_SHARE
#: Stop ceiling proby: hrana All minus tento podíl šířky zóny (kotva na pásmo)
CEILING_STOP_WIDTH_SHARE = 0.25


def zone_position(zone: BandZone | None, price: float) -> str:
    """Poloha ceny vůči hranám All zóny: below / in / above / unknown.

    Záměrně přes geometrii zóny (band_zone hledá vrchol i NAD cenou), ne přes
    band_depth — metrika #575 daleko pod pásmem počvě vrací None (měří jen
    v místě ceny) a přechod outside→transition by nikdy nenastal.
    """
    if zone is None:
        return "unknown"
    if price < zone.all_low:
        return "below"
    if price > zone.all_high:
        return "above"
    return "in"


@dataclass
class _ActiveProbe:
    probe_id: int
    template: str
    direction: Direction
    entry: float
    target: float
    stop: float
    mfe: float
    mae: float


@dataclass
class _Pending:
    template: str
    started: dt.datetime
    run: int
    zone: BandZone


@dataclass
class T9ProbeCollector:
    """Per-minutový sběrač výskytů; pád nesmí shodit sběr dat (volá pipeline)."""

    symbol: str
    repository: ProbeRepository

    _regime: str = field(default="unknown", init=False)
    #: Kolik minut v řadě drží aktuální poloha — usazení před přechodem.
    #: Bez toho by odmítnutý vstup do pásma (1 minuta uvnitř) hned generoval
    #: zrcadlovou exit probu a flicker na hraně by dvojitě počítal výskyty.
    _regime_run: int = field(default=0, init=False)
    _last_close: float | None = field(default=None, init=False)
    _pending: _Pending | None = field(default=None, init=False)
    _active: list[_ActiveProbe] = field(default_factory=list, init=False)

    async def on_minute(
        self, now: dt.datetime, spot: float, bars: list[Bar], runtime: EngineRuntime
    ) -> None:
        if not bars:
            return
        bar = bars[-1]
        self._evaluate_active(now, bar.high, bar.low, bar.close)
        self._timeout_at_settle(now, bar.close)

        profile = runtime.last_profile
        metrics = band_metrics(profile, bar.close) if profile is not None else None
        zone = band_zone(profile, bar.close) if profile is not None else None
        position = zone_position(zone, bar.close)
        previous_position = self._regime
        previous_run = self._regime_run
        previous_close = self._last_close
        self._regime_run = self._regime_run + 1 if position == previous_position else 1
        self._regime = position
        self._last_close = bar.close
        if position == "unknown" or previous_close is None:
            self._pending = None
            return

        # Běžící akceptace: přechod musí vydržet, jinak se kandidát zahazuje
        if self._pending is not None:
            holds = (
                position != "below"
                if self._pending.template == "t9_ceiling"
                else position == "below"
            )
            if not holds:
                self._pending = None
            else:
                self._pending.run += 1
                if self._pending.run >= ACCEPTANCE_MINUTES:
                    self._open_probe(now, bar.close, metrics, self._pending)
                    self._pending = None

        # Nový kandidát ceiling: vstup do pásma zespodu s jádrem nad hlavou
        if (
            self._pending is None
            and previous_position == "below"
            and previous_run >= ACCEPTANCE_MINUTES
            and position == "in"
            and bar.close > previous_close
            and zone is not None
            and zone.strength_above >= STRENGTH_ABOVE_MIN
        ):
            self._pending = _Pending(template="t9_ceiling", started=now, run=1, zone=zone)
        # Zrcadlo: výpad z pásma dolů (momentum) — levné měřit současně (#577)
        elif (
            self._pending is None
            and previous_position == "in"
            and previous_run >= ACCEPTANCE_MINUTES
            and position == "below"
            and bar.close < previous_close
            and zone is not None
        ):
            self._pending = _Pending(template="t9_exit", started=now, run=1, zone=zone)

    def _open_probe(
        self,
        now: dt.datetime,
        close: float,
        metrics: object,
        pending: _Pending,
    ) -> None:
        zone = pending.zone
        if pending.template == "t9_ceiling":
            direction = Direction.LONG
            entry = close
            target = zone.center
            stop = zone.all_low - CEILING_STOP_WIDTH_SHARE * zone.width
            if target <= entry:
                return  # cena už je za středem — hypotéza nemá co měřit
        else:
            direction = Direction.SHORT
            entry = close
            stop = zone.all_low
            target = entry - zone.width
            if stop <= entry:
                return  # cena nad hranou — výpad se nekonal
        context: dict[str, object] = {
            "zone_all_low": zone.all_low,
            "zone_all_high": zone.all_high,
            "zone_center": zone.center,
            "zone_width": zone.width,
            "strength_above": zone.strength_above,
            "acceptance_minutes": ACCEPTANCE_MINUTES,
        }
        depth = getattr(metrics, "depth", None)
        if depth is not None:
            context["band_depth"] = depth
            # Význam hloubky se mění mezi verzemi (#952) — bez značky by šly
            # sondy z různých verzí sdružit dohromady
            context["band_metrics_version"] = BAND_METRICS_VERSION
        probe_id = self.repository.insert(
            template=pending.template,
            symbol=self.symbol,
            session_date=now.date(),
            created_ts=now,
            direction=direction.value if hasattr(direction, "value") else str(direction),
            entry=entry,
            target=target,
            stop=stop,
            context=context,
        )
        self._active.append(
            _ActiveProbe(
                probe_id=probe_id,
                template=pending.template,
                direction=direction,
                entry=entry,
                target=target,
                stop=stop,
                mfe=0.0,
                mae=0.0,
            )
        )
        logger.info(
            "T9 probe %s %s (#577): %s entry %.2f cíl %.2f stop %.2f",
            pending.template,
            self.symbol,
            direction,
            entry,
            target,
            stop,
        )

    def _evaluate_active(self, now: dt.datetime, high: float, low: float, close: float) -> None:
        still_active: list[_ActiveProbe] = []
        for probe in self._active:
            favorable = (
                high - probe.entry if probe.direction is Direction.LONG else probe.entry - low
            )  # noqa: E501
            adverse = probe.entry - low if probe.direction is Direction.LONG else high - probe.entry
            probe.mfe = max(probe.mfe, favorable)
            probe.mae = max(probe.mae, adverse)
            outcome = evaluate_bar(
                probe.direction, probe.entry, probe.target, probe.stop, high, low
            )  # noqa: E501
            if outcome is None:
                still_active.append(probe)
                continue
            exit_price = probe.target if outcome.name == "TARGET" else probe.stop
            self.repository.close(
                probe.probe_id,
                status=f"closed_{outcome.name.lower()}",
                closed_ts=now,
                outcome_r=r_result(probe.direction, probe.entry, probe.stop, exit_price),
                mfe=probe.mfe,
                mae=probe.mae,
            )
        self._active = still_active

    def _timeout_at_settle(self, now: dt.datetime, close: float) -> None:
        """Settle uzavírá vše otevřené za close — stejně jako živé setupy."""
        if not self._active or now < settle_ts(now.date()):
            return
        for probe in self._active:
            self.repository.close(
                probe.probe_id,
                status="closed_timeout",
                closed_ts=now,
                outcome_r=r_result(probe.direction, probe.entry, probe.stop, close),
                mfe=probe.mfe,
                mae=probe.mae,
            )
        self._active = []
