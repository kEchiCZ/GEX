"""SetupEngine (ADR-0004): stavová orchestrace detektoru nad běžící pipeline.

Každou minutu po cyklu aktivní expirace: sestaví MinuteInputs (bar podkladu,
GEX úrovně z posledního cyklu, toky z rozdílu kumulativních volume, Max Pain
z OI archivu), spustí čisté detektory, hlídá anti-spam, ukládá setupy do PG,
vyhodnocuje otevřené proti baru a publikuje alerty + WS kanál setups.{symbol}.

Selhání čehokoli tady nesmí shodit sběr dat — volající balí do try/except.
"""

import asyncio
import datetime as dt
import logging
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import cast

from gexlens_engine.compute.bandregime import band_context
from gexlens_engine.compute.gexfield import gamma_edges
from gexlens_engine.compute.settle import settle_ts
from gexlens_engine.compute.setups import (
    Direction,
    MinuteInputs,
    Outcome,
    SetupParams,
    average_true_range,
    detect_all,
    evaluate_bar,
    gex_regime,
    is_counter_regime,
    max_pain_strike,
    r_result,
)
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.runtime import EngineRuntime, PublisherLike
from gexlens_engine.storage.oi_archive import OIEodRepository
from gexlens_engine.storage.parquet_store import FeatureRow, SnapshotWriter
from gexlens_engine.storage.setups_store import SetupsRepository, StoredSetup

logger = logging.getLogger(__name__)

HISTORY_MINUTES = 400


@dataclass
class _OpenSetup:
    stored: StoredSetup
    mfe: float = 0.0
    mae: float = 0.0
    # Kontra-režimový setup (#252 C): stop spouští delší cooldown šablony.
    # Setupy načtené z DB po restartu flag nemají (kontext se nenačítá) — žebřík
    # ztrát se po restartu hlídá až od prvního nově vzniklého setupu.
    counter: bool = False


@dataclass
class SetupEngine:
    symbol: str
    repository: SetupsRepository
    oi_repository: OIEodRepository
    publisher: PublisherLike
    params: SetupParams = field(default_factory=SetupParams)
    # Minutový feature log (#796): trénovací matice pro samoučící smyčku (#794).
    # None = vypnuto; zapisuje se do derived/{symbol}/features/ (mimo retenci).
    feature_writer: SnapshotWriter | None = None

    def __post_init__(self) -> None:
        self._history: deque[MinuteInputs] = deque(maxlen=HISTORY_MINUTES)
        self._prev_volumes: dict[object, float] = {}
        self._open: list[_OpenSetup] = []
        self._last_created: dict[str, dt.datetime] = {}
        # Poslední stop kontra-režimového setupu per šablona (#252 C)
        self._last_counter_stop: dict[str, dt.datetime] = {}
        # Série stopů a blokace per směr (#302) — napříč šablonami, protože
        # per-šablonový anti-spam se dal obejít jejich prokládáním
        self._direction_stops: dict[str, int] = {}
        self._direction_blocked_until: dict[str, dt.datetime] = {}
        self._max_pain: float | None = None
        self._max_pain_loaded_for: tuple[str, dt.date, dt.datetime | None] | None = None
        # Otevřené setupy z DB (restart enginu) — MFE/MAE pokračují od nuly
        for stored in self.repository.active_for(self.symbol):
            self._open.append(_OpenSetup(stored=stored))

    def _refresh_max_pain(self, expiry: str, today: dt.date) -> None:
        """Max Pain z denního archivu OI; přepočet při KAŽDÉ změně snímku (#826).

        Cache klíčovaná jen na (expirace, den) zamrzla na prvním ranním
        načtení, kdy je archiv teprve částečně naplněný — CME publikaci
        dobíhá engine celé dopoledne (#463). NQ 24. 8.: Max Pain se za den
        posunul 29200 → 29400 → 29390 (jak Σ OI rostlo 1 570 → 3 459), ale
        tendence i setupy celý den počítaly s hodnotou z prvního načtení.
        Klíč proto nese `captured_ts` snímku — mění se jen při novém
        průchodu archivace (jednotky za den), takže přepočet zůstává levný.
        """
        captured = self.oi_repository.captured_at(self.symbol, today)
        if self._max_pain_loaded_for == (expiry, today, captured) and self._max_pain is not None:
            return
        records = self.oi_repository.values_for(self.symbol, expiry, today)
        oi_map = {(r.strike, r.right): r.oi for r in records}
        self._max_pain = max_pain_strike(oi_map)
        self._max_pain_loaded_for = (expiry, today, captured)

    def _flows(self, runtime: EngineRuntime) -> tuple[float, float, float]:
        """Δ-vážené přírůstky volume per strana + surový přírůstek (z cache kotací)."""
        call_flow = put_flow = raw = 0.0
        # Aktivní zdroj řetězu, ne přímo sweep cache (#614 fáze 2b) — jinak by
        # se za fallbacku počítal tok ze zmrzlých IBKR kotací
        for spec, cached in runtime.current_quotes().items():
            snapshot = cached.snapshot
            # Bez objemu (fallback z tasty) se přírůstek počítat nedá. Poslední
            # známou hodnotu si držíme dál, ať se po návratu na IBKR naváže
            # na ni, a ne na první ponovu viděné číslo jako na celý přírůstek.
            if snapshot.volume is None:
                continue
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
    def _settle_ts(expiry: str) -> dt.datetime | None:
        """Settle dne expirace — konvence sdílená přes compute.settle (#498)."""
        try:
            date = dt.datetime.strptime(expiry, "%Y%m%d").date()
        except ValueError:
            return None
        return settle_ts(date)

    @classmethod
    def _minutes_to_expiry(cls, expiry: str, now: dt.datetime) -> float | None:
        settle = cls._settle_ts(expiry)
        if settle is None:
            return None
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
        # Dominance zdí (ADR-0010, #223) — LevelsRow ji nenese, čte se z plných levels
        full = runtime.last_gex_levels
        # Hranice gamma masy (#600) z Dyn GEX profilu téže minuty — počítá se tady,
        # neukládá: detektor ji potřebuje jako číslo, graf ji nekreslí.
        edges = gamma_edges(runtime.last_profile) if runtime.last_profile else None
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
            call_wall_dom=full.call_wall_dom if full else None,
            put_wall_dom=full.put_wall_dom if full else None,
            # GEX režim (#209) — do kontextu každého setupu pro kalibraci Fáze 2
            gex_regime=(gex_regime(bar_close, levels.flip, levels.total_gex) if levels else None),
            gamma_edge_up=edges.up if edges else None,
            gamma_edge_dn=edges.dn if edges else None,
        )
        self._history.append(inputs)
        if self.feature_writer is not None:
            await self._log_features(now, runtime, inputs)

        await self._evaluate_open(now, inputs, bars)
        await self._detect_new(now, runtime, inputs)

    async def _log_features(
        self, now: dt.datetime, runtime: EngineRuntime, inputs: MinuteInputs
    ) -> None:
        """Minutový feature log (#796): MinuteInputs + ATR + band metriky.

        ONH/ONL, VWAP ani relativní síla se nelogují — jsou dopočitatelné
        z věčných barů. Chyba zápisu nesmí shodit minutový cyklus.
        """
        assert self.feature_writer is not None
        try:
            band = band_context(runtime.last_profile, inputs.close)
            row = FeatureRow(
                ts=now,
                expiry=runtime.expiry,
                open=inputs.open,
                high=inputs.high,
                low=inputs.low,
                close=inputs.close,
                flip=inputs.flip,
                call_wall=inputs.call_wall,
                put_wall=inputs.put_wall,
                max_pain=inputs.max_pain,
                cum_delta=inputs.cum_delta,
                call_flow=inputs.call_flow,
                put_flow=inputs.put_flow,
                opt_vol=inputs.opt_vol,
                minutes_to_expiry=inputs.minutes_to_expiry,
                call_wall_dom=inputs.call_wall_dom,
                put_wall_dom=inputs.put_wall_dom,
                gex_regime=inputs.gex_regime,
                gamma_edge_up=inputs.gamma_edge_up,
                gamma_edge_dn=inputs.gamma_edge_dn,
                # list(): _history je deque a slicing v average_true_range by
                # spadl na TypeError — projevilo se až s plnou historií (>15
                # minut), testy s krátkou historií prošly přes časný return None
                atr=average_true_range(list(self._history), self.params.atr_lookback),
                band_sharpness=band.get("band_sharpness"),
                band_sharpness_pct=band.get("band_sharpness_pct"),
                band_depth=band.get("band_depth"),
                # None, když se pásmo nezměřilo — verze patří k HODNOTÁM,
                # ne k řádku, takže se nesmí vyplnit "pro jistotu" (#952)
                band_metrics_version=(
                    int(band["band_metrics_version"]) if "band_metrics_version" in band else None
                ),
            )
            await asyncio.to_thread(
                self.feature_writer.write_features, self.symbol, now.date(), [row]
            )
        except Exception:
            logger.exception("Zápis feature logu selhal — minutový cyklus jede dál")

    async def _evaluate_open(
        self,
        now: dt.datetime,
        inputs: MinuteInputs,
        bars: Sequence[Bar] | None = None,
    ) -> None:
        # Vyhodnocení po jednotlivých barech (#257): cyklus umí nést víc minut
        # najednou (sekvenční sweep instrumentů, dávka po zpoždění) — outcome
        # i closed_ts musí patřit svíčce, která úroveň zasáhla (její ts = čas
        # na 1m TF), ne wall-clock času cyklu. Konzervativní stop-first pravidlo
        # platí jen UVNITŘ jedné svíčky — cíl v dřívější minutě vyhrává nad
        # stopem v pozdější. Bez barů (spot fallback) se hodnotí agregát minuty.
        points: Sequence[Bar] = (
            sorted(bars, key=lambda bar: bar.ts)
            if bars
            else [
                Bar(
                    ts=now,
                    open=inputs.open,
                    high=inputs.high,
                    low=inputs.low,
                    close=inputs.close,
                    volume=0.0,
                )
            ]
        )
        still_open: list[_OpenSetup] = []
        for item in self._open:
            direction = Direction(item.stored.direction)
            settle = self._settle_ts(item.stored.expiry)
            outcome: Outcome | None = None
            closed_ts = now
            for point in points:
                # Bary po settle už setupu nepatří (#259) — jinak by dnešní
                # svíčka mohla „zavřít" včerejší setup jeho úrovněmi
                if settle is not None and point.ts >= settle:
                    break
                favourable = (
                    point.high - item.stored.entry
                    if direction is Direction.LONG
                    else item.stored.entry - point.low
                )
                adverse = (
                    item.stored.entry - point.low
                    if direction is Direction.LONG
                    else point.high - item.stored.entry
                )
                item.mfe = max(item.mfe, favourable)
                item.mae = max(item.mae, adverse)
                outcome = evaluate_bar(
                    direction,
                    item.stored.entry,
                    item.stored.target,
                    item.stored.stop,
                    point.high,
                    point.low,
                )
                if outcome is not None:
                    closed_ts = point.ts
                    break

            # Timeout podle expirace SETUPU, ne runtime (#259): po restartu přes
            # hranici expirace by čerstvá runtime expirace nechala včerejší
            # setupy žít a vyhodnocovat se svými úrovněmi proti dnešním cenám
            timeout = outcome is None and settle is not None and now >= settle
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
                closed_ts = now
            result = r_result(direction, item.stored.entry, item.stored.stop, exit_price)
            # Stop kontra-setupu (#252 C): další kontra pokus téže šablony až po
            # delším cooldownu — brání žebříku ztrát (24. 7.: 4 stopy za hodinu)
            if outcome is Outcome.STOP and item.counter:
                self._last_counter_stop[item.stored.template] = closed_ts
            self._track_direction_streak(item.stored.direction, outcome, closed_ts)
            self.repository.close(
                item.stored.id,
                status=outcome.value,
                closed_ts=closed_ts,
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

    def _track_direction_streak(
        self, direction: str, outcome: Outcome, closed_ts: dt.datetime
    ) -> None:
        """Série stopů v jednom směru (#302) → dočasná blokace směru.

        Výhra sérii maže; timeout ji nechává být (nic nevyvrátil). Počítadlo se
        po blokaci nenuluje — po vyčerpané sérii projde jen jeden pokus za okno,
        dokud směr nedokáže výhru.
        """
        if outcome is Outcome.TARGET:
            self._direction_stops.pop(direction, None)
            self._direction_blocked_until.pop(direction, None)
            return
        if outcome is not Outcome.STOP:
            return
        streak = self._direction_stops.get(direction, 0) + 1
        self._direction_stops[direction] = streak
        if streak >= self.params.max_stops_per_direction:
            self._direction_blocked_until[direction] = closed_ts + dt.timedelta(
                minutes=self.params.direction_block_minutes
            )
            logger.info(
                "Setup %s: směr %s blokován do %s (%d stopů v řadě)",
                self.symbol,
                direction,
                self._direction_blocked_until[direction],
                streak,
            )

    def _direction_blocked(self, direction: str, now: dt.datetime) -> bool:
        until = self._direction_blocked_until.get(direction)
        return until is not None and now < until

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
            # Blokace směru po sérii stopů (#302) — napříč šablonami
            if self._direction_blocked(candidate.direction.value, now):
                continue
            counter = is_counter_regime(
                candidate.direction, cast(str | None, candidate.context.get("gex_regime"))
            )
            # Delší cooldown po stopu v kontra-režimu (#252 C)
            if counter:
                last_stop = self._last_counter_stop.get(template)
                stop_cooldown_s = self.params.counter_stop_cooldown_minutes * 60
                if last_stop is not None and (now - last_stop).total_seconds() < stop_cooldown_s:
                    continue
            # Pásmové metriky (#575 fáze 1): obě varianty ostrosti + hloubka
            # z Dyn profilu minuty — jen měření, brána se nemění
            context = {**candidate.context, **band_context(runtime.last_profile, candidate.entry)}
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
                context=context,
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
                    ),
                    counter=counter,
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
