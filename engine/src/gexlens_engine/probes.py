"""Sběrač výskytů kandidáta T9 „strop nad hlavou" (#577, fáze 1 — JEN sběr).

Referenční čtení: cena přichází zespodu ke spodní hraně tlumící zóny a
očekávaná mechanika je ÚTLUM, ne odraz — proto se nekotví na strike (T1),
ale na hranu pásma z #575, a prahy nejsou v bodech ani ATR (obojí #434
zamítlo), nýbrž v geometrii pásma samotného.

Rozhodování dělá čistá funkce `detect_damping_ceiling` v `compute/setups.py`
(vedle ostatních detektorů, ale MIMO `detect_all`) — tatáž, kterou spouští
`scripts/backtest_setups.py --probes` nad historií. Tenhle modul je jen
orchestrace: okno posledních minut, zápis výskytu do `setup_probes`
a vyhodnocení otevřených sond **týmž krokem jako živé setupy** (#1345,
`compute/setups.walk_setup_path`): bary v pořadí od baru vstupu, díru v živé
dávce dotáhne z partic (`bar_reader`) a na nedoplněnou čeká `PATH_GAP_WAIT`,
bary od settle se nehodnotí, timeout za close baru končícího v settle
expirace sondy (`settle.expiry_settle`, #1331), `closed_ts` = bar zásahu nebo
settle. Spot se nehodnotí nikdy. Nové sondy po settle nevzniknou
(`born_after_settle`).
Do `setups`, alertů ani track recordu nejde NIC — fáze 2 (≥ 30 výskytů na
instrument) teprve rozhodne zapnout/sloučit/zavřít.
"""

import asyncio
import datetime as dt
import logging
from collections import deque
from dataclasses import dataclass, field

from gexlens_engine.compute.bandregime import BAND_METRICS_VERSION, band_metrics, band_zone
from gexlens_engine.compute.settle import expiry_settle
from gexlens_engine.compute.setups import (
    PATH_GAP_WAIT,
    PathBar,
    PathState,
    ProbeMinute,
    ProbeOccurrence,
    ProbeParams,
    born_after_settle,
    detect_damping_ceiling,
    missing_minutes,
    r_result,
    walk_setup_path,
)
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.runtime import EngineRuntime
from gexlens_engine.setups import STORED_BAR_LAG, BarReader
from gexlens_engine.storage.probes_store import ProbeRepository

logger = logging.getLogger(__name__)

_MINUTE = dt.timedelta(minutes=1)


@dataclass
class _ActiveProbe:
    probe_id: int
    occurrence: ProbeOccurrence
    expiry: str
    #: Kam až je cesta ceny vyhodnocená (#1345); MFE/MAE v bodech
    path: PathState
    #: Díra, na kterou sonda čeká, a od kdy (`PATH_GAP_WAIT`)
    waiting_gap: tuple[dt.datetime, dt.datetime] | None = None
    blocked_since: dt.datetime | None = None


@dataclass
class T9ProbeCollector:
    """Per-minutový sběrač výskytů; pád nesmí shodit sběr dat (volá pipeline)."""

    symbol: str
    repository: ProbeRepository
    params: ProbeParams = field(default_factory=ProbeParams)
    #: Bary z partic pro díry v živé dávce (#1345, týž zdroj jako SetupEngine)
    bar_reader: BarReader | None = None

    _history: deque[ProbeMinute] = field(init=False)
    _active: list[_ActiveProbe] = field(default_factory=list, init=False)
    _expiry_logged: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        # Detektor potřebuje přesně 2 × akceptace minut (usazení + přechod)
        self._history = deque(maxlen=2 * self.params.acceptance_minutes)

    async def on_minute(
        self, now: dt.datetime, spot: float, bars: list[Bar], runtime: EngineRuntime
    ) -> None:
        live = sorted(bars, key=lambda item: item.ts)
        # Otevřené sondy jdou po cestě ceny i v cyklu bez baru — díra se
        # dotáhne z partic, timeout v settle nečeká na živou dávku
        await self._evaluate_active(now, live)
        if not live:
            return
        bar = live[-1]

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
        if occurrence is not None and self._expiry_alive(runtime.expiry, now):
            self._open_probe(now, occurrence, runtime.expiry, bar)

    def _expiry_alive(self, expiry: str, now: dt.datetime) -> bool:
        """Smí nad expirací runtime vzniknout sonda? Týž invariant jako setupy (#1324).

        Po settle vlastní expirace (`born_after_settle`) ne — v prvním cyklu
        po settle, po kterém pipeline roluje (#1331), by ji timeout zavřel
        hned. Nečitelná expirace také ne: sonda bez settle by nikdy nedostala
        timeout (nahlas, jednou za expiraci).
        """
        if expiry_settle(expiry) is None:
            if self._expiry_logged != expiry:
                self._expiry_logged = expiry
                logger.warning(
                    "Sondy T9 %s: nečitelná expirace %r — bez settle se sonda neotevře",
                    self.symbol,
                    expiry,
                )
            return False
        return not born_after_settle(expiry, now)

    def _open_probe(
        self, now: dt.datetime, occurrence: ProbeOccurrence, expiry: str, entry_bar: Bar
    ) -> None:
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
        # Cesta začíná za barem vstupu — jeho close je entry (vzor SetupEngine)
        self._active.append(
            _ActiveProbe(
                probe_id=probe_id,
                occurrence=occurrence,
                expiry=expiry,
                path=PathState(last_ts=entry_bar.ts, last_close=entry_bar.close),
            )
        )
        logger.info(
            "T9 probe %s %s (#577): %s entry %.2f cíl %.2f stop %.2f",
            occurrence.template,
            self.symbol,
            occurrence.direction.value,
            occurrence.entry,
            occurrence.target,
            occurrence.stop,
        )

    async def _stored_bars(self, now: dt.datetime, live: list[Bar]) -> list[PathBar]:
        """Bary z partic, když živá dávka na cestu některé sondy nenavazuje.

        Stejné pravidlo jako `SetupEngine._stored_bars` (#1320): běžná minuta
        partici nečte; chyba čtení = sonda počká (nic se nevymýšlí).
        """
        if self.bar_reader is None or not self._active:
            return []
        horizon = now.replace(second=0, microsecond=0) - STORED_BAR_LAG
        behind: list[dt.datetime] = []
        for probe in self._active:
            last_ts = probe.path.last_ts
            if last_ts >= horizon or missing_minutes(last_ts, horizon + _MINUTE) is None:
                continue
            following = next((bar for bar in live if bar.ts > last_ts), None)
            if following is not None and missing_minutes(last_ts, following.ts) is None:
                continue
            behind.append(last_ts)
        if not behind:
            return []
        try:
            return list(await asyncio.to_thread(self.bar_reader, min(behind), horizon))
        except Exception:
            logger.exception("Sondy T9 %s: čtení barů z partic selhalo — čeká se", self.symbol)
            return []

    async def _evaluate_active(self, now: dt.datetime, live: list[Bar]) -> None:
        if not self._active:
            return
        path_bars: list[PathBar] = [*await self._stored_bars(now, live), *live]
        still_active: list[_ActiveProbe] = []
        for probe in self._active:
            item = probe.occurrence
            force_until = (
                probe.waiting_gap[1]
                if probe.waiting_gap is not None
                and probe.blocked_since is not None
                and now - probe.blocked_since >= PATH_GAP_WAIT
                else None
            )
            previous_ts = probe.path.last_ts
            step = walk_setup_path(
                item.direction,
                item.entry,
                item.target,
                item.stop,
                expiry_settle(probe.expiry),
                path_bars,
                probe.path,
                now=now,
                force_until=force_until,
            )
            probe.path = step.state
            if step.state.last_ts > previous_ts:
                probe.waiting_gap = probe.blocked_since = None
            if step.outcome is None:
                if step.blocked and probe.blocked_since is None:
                    probe.waiting_gap, probe.blocked_since = step.gaps[-1], now
                elif not step.blocked:
                    probe.waiting_gap = probe.blocked_since = None
                still_active.append(probe)
                continue
            # Timeout bez jediného baru po vstupu: výstup za entry (R 0)
            exit_price = step.exit_price if step.exit_price is not None else item.entry
            self.repository.close(
                probe.probe_id,
                status=step.outcome.value,
                closed_ts=step.closed_ts if step.closed_ts is not None else now,
                outcome_r=r_result(item.direction, item.entry, item.stop, exit_price),
                mfe=step.state.mfe,
                mae=step.state.mae,
            )
        self._active = still_active
