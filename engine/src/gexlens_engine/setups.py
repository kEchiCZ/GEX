"""SetupEngine (ADR-0004): stavová orchestrace detektoru nad běžící pipeline.

Každou minutu po cyklu aktivní expirace: sestaví MinuteInputs (bar podkladu,
GEX úrovně z posledního cyklu, toky z rozdílu kumulativních volume, Max Pain
z OI archivu), spustí čisté detektory, hlídá anti-spam, ukládá setupy do PG,
vyhodnocuje otevřené proti baru a publikuje alerty + WS kanál setups.{symbol}.

Selhání čehokoli tady nesmí shodit sběr dat — volající balí do try/except.
"""

import datetime as dt
import logging
from collections import deque
from dataclasses import dataclass, field

from gexlens_engine.compute.setups import (
    Direction,
    MinuteInputs,
    Outcome,
    SetupParams,
    detect_all,
    evaluate_bar,
    max_pain_strike,
    r_result,
)
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.runtime import EngineRuntime, PublisherLike
from gexlens_engine.storage.oi_archive import OIEodRepository
from gexlens_engine.storage.setups_store import SetupsRepository, StoredSetup

logger = logging.getLogger(__name__)

HISTORY_MINUTES = 400
# Settle ≈ 20:00 UTC dne expirace (shodné s frontend instrument/expiry.ts)
SETTLE_HOUR_UTC = 20


@dataclass
class _OpenSetup:
    stored: StoredSetup
    mfe: float = 0.0
    mae: float = 0.0


@dataclass
class SetupEngine:
    symbol: str
    repository: SetupsRepository
    oi_repository: OIEodRepository
    publisher: PublisherLike
    params: SetupParams = field(default_factory=SetupParams)

    def __post_init__(self) -> None:
        self._history: deque[MinuteInputs] = deque(maxlen=HISTORY_MINUTES)
        self._prev_volumes: dict[object, float] = {}
        self._open: list[_OpenSetup] = []
        self._last_created: dict[str, dt.datetime] = {}
        self._max_pain: float | None = None
        self._max_pain_loaded_for: tuple[str, dt.date] | None = None
        # Otevřené setupy z DB (restart enginu) — MFE/MAE pokračují od nuly
        for stored in self.repository.active_for(self.symbol):
            self._open.append(_OpenSetup(stored=stored))

    def _refresh_max_pain(self, expiry: str, today: dt.date) -> None:
        if self._max_pain_loaded_for == (expiry, today) and self._max_pain is not None:
            return
        records = self.oi_repository.values_for(self.symbol, expiry, today)
        oi_map = {(r.strike, r.right): r.oi for r in records}
        self._max_pain = max_pain_strike(oi_map)
        self._max_pain_loaded_for = (expiry, today)

    def _flows(self, runtime: EngineRuntime) -> tuple[float, float, float]:
        """Δ-vážené přírůstky volume per strana + surový přírůstek (z cache kotací)."""
        call_flow = put_flow = raw = 0.0
        for spec, cached in runtime.scheduler.quotes().items():
            snapshot = cached.snapshot
            previous = self._prev_volumes.get(spec)
            self._prev_volumes[spec] = snapshot.volume
            if previous is None:
                continue
            increment = snapshot.volume - previous
            if increment <= 0:
                continue
            raw += increment
            weighted = increment * abs(snapshot.delta)
            if spec.right == "C":
                call_flow += weighted
            else:
                put_flow += weighted
        return call_flow, put_flow, raw

    @staticmethod
    def _minutes_to_expiry(expiry: str, now: dt.datetime) -> float | None:
        try:
            date = dt.datetime.strptime(expiry, "%Y%m%d").date()
        except ValueError:
            return None
        settle = dt.datetime.combine(date, dt.time(SETTLE_HOUR_UTC, 0), tzinfo=dt.UTC)
        return (settle - now).total_seconds() / 60.0

    async def on_minute(
        self, now: dt.datetime, spot: float, bars: list[Bar], runtime: EngineRuntime
    ) -> None:
        levels = runtime.last_levels
        flow = runtime.last_flow
        self._refresh_max_pain(runtime.expiry, now.date())
        call_flow, put_flow, raw_flow = self._flows(runtime)

        if bars:
            bar_open = bars[0].open
            bar_high = max(b.high for b in bars)
            bar_low = min(b.low for b in bars)
            bar_close = bars[-1].close
        else:
            bar_open = bar_high = bar_low = bar_close = spot

        minutes_left = self._minutes_to_expiry(runtime.expiry, now)
        inputs = MinuteInputs(
            ts=now,
            open=bar_open,
            high=bar_high,
            low=bar_low,
            close=bar_close,
            flip=levels.flip if levels else None,
            call_wall=levels.call_wall if levels else None,
            put_wall=levels.put_wall if levels else None,
            max_pain=self._max_pain,
            cum_delta=flow.cum_delta if flow else 0.0,
            call_flow=call_flow,
            put_flow=put_flow,
            opt_vol=raw_flow,
            minutes_to_expiry=minutes_left,
        )
        self._history.append(inputs)

        await self._evaluate_open(now, inputs, minutes_left)
        await self._detect_new(now, runtime, inputs)

    async def _evaluate_open(
        self, now: dt.datetime, inputs: MinuteInputs, minutes_left: float | None
    ) -> None:
        still_open: list[_OpenSetup] = []
        for item in self._open:
            direction = Direction(item.stored.direction)
            favourable = (
                inputs.high - item.stored.entry
                if direction is Direction.LONG
                else item.stored.entry - inputs.low
            )
            adverse = (
                item.stored.entry - inputs.low
                if direction is Direction.LONG
                else inputs.high - item.stored.entry
            )
            item.mfe = max(item.mfe, favourable)
            item.mae = max(item.mae, adverse)

            outcome = evaluate_bar(
                direction,
                item.stored.entry,
                item.stored.target,
                item.stored.stop,
                inputs.high,
                inputs.low,
            )
            timeout = outcome is None and minutes_left is not None and minutes_left <= 0
            if outcome is None and not timeout:
                still_open.append(item)
                continue

            if outcome is Outcome.TARGET:
                exit_price = item.stored.target
            elif outcome is Outcome.STOP:
                exit_price = item.stored.stop
            else:
                outcome = Outcome.TIMEOUT
                exit_price = inputs.close
            result = r_result(direction, item.stored.entry, item.stored.stop, exit_price)
            self.repository.close(
                item.stored.id,
                status=outcome.value,
                closed_ts=now,
                outcome_r=result,
                mfe=item.mfe,
                mae=item.mae,
            )
            label = {
                Outcome.TARGET: "cíl zasažen",
                Outcome.STOP: "stop zasažen",
                Outcome.TIMEOUT: "timeout (expirace/seance)",
            }[outcome]
            await self.publisher.publish(
                "alerts",
                {
                    "kind": "setup",
                    # Proklik ve zvonečku (#186): výsledek vede na stránku Setupy
                    "event": "closed",
                    "symbol": self.symbol,
                    "message": f"Setup #{item.stored.id} uzavřen: {label}, "
                    f"výsledek {result:+.2f} R",
                    "ts": now.timestamp(),
                },
            )
            await self.publisher.publish(
                f"setups.{self.symbol}", {"event": "closed", "id": item.stored.id}
            )
        self._open = still_open

    async def _detect_new(
        self, now: dt.datetime, runtime: EngineRuntime, inputs: MinuteInputs
    ) -> None:
        open_templates = {item.stored.template for item in self._open}
        for candidate in detect_all(list(self._history), self.params):
            template = candidate.template.value
            if template in open_templates:
                continue  # max 1 aktivní setup per šablona
            last = self._last_created.get(template)
            cooldown_s = self.params.cooldown_minutes * 60
            if last is not None and (now - last).total_seconds() < cooldown_s:
                continue
            setup_id = self.repository.create(
                symbol=self.symbol,
                expiry=runtime.expiry,
                template=template,
                direction=candidate.direction.value,
                created_ts=now,
                entry=candidate.entry,
                target=candidate.target,
                stop=candidate.stop,
                confidence=candidate.confidence,
                reason=candidate.reason,
                context=candidate.context,
            )
            self._last_created[template] = now
            self._open.append(
                _OpenSetup(
                    stored=StoredSetup(
                        id=setup_id,
                        symbol=self.symbol,
                        expiry=runtime.expiry,
                        template=template,
                        direction=candidate.direction.value,
                        created_ts=now,
                        entry=candidate.entry,
                        target=candidate.target,
                        stop=candidate.stop,
                        confidence=candidate.confidence,
                        reason=candidate.reason,
                        status="active",
                    )
                )
            )
            open_templates.add(template)
            side = "LONG" if candidate.direction is Direction.LONG else "SHORT"
            await self.publisher.publish(
                "alerts",
                {
                    "kind": "setup",
                    # Proklik ve zvonečku (#186): nový setup vede na graf instrumentu
                    "event": "created",
                    "symbol": self.symbol,
                    "message": f"Nový setup {side} ({template}): entry {candidate.entry:g}, "
                    f"cíl {candidate.target:g}, stop {candidate.stop:g} "
                    f"(RRR {candidate.rrr:.1f}, conf. {candidate.confidence} %). "
                    f"{candidate.reason}",
                    "ts": now.timestamp(),
                },
            )
            await self.publisher.publish(
                f"setups.{self.symbol}", {"event": "created", "id": setup_id}
            )
            logger.info("Setup %s %s #%d: %s", self.symbol, template, setup_id, candidate.reason)
