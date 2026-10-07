"""SetupEngine (ADR-0004): stavová orchestrace detektoru nad běžící pipeline.

Každou minutu po cyklu aktivní expirace: sestaví MinuteInputs (bary dávky cyklu,
GEX úrovně z posledního cyklu, toky z rozdílu kumulativních volume, Max Pain
z OI archivu), spustí čisté detektory, hlídá anti-spam, ukládá setupy do PG,
vyhodnocuje otevřené po cestě ceny (bary v pořadí, díry si vyžádá z IBKR
historical a čte z partic, #1320) a publikuje alerty + WS kanál setups.{symbol}.
Dávka bez baru jen vyhodnotí otevřené setupy — spot do detekce nevstupuje (#1346).

Selhání čehokoli tady nesmí shodit sběr dat — volající balí do try/except.
"""

import asyncio
import datetime as dt
import logging
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import cast

from gexlens_engine.compute.bandregime import (
    adjusted_confidence,
    band_context,
    band_gate_context,
)
from gexlens_engine.compute.coach_setups import hour_local, session_segment
from gexlens_engine.compute.confidence import ConfidenceTable, build_confidence_table
from gexlens_engine.compute.gexfield import gamma_edges
from gexlens_engine.compute.paper import POINT_VALUES
from gexlens_engine.compute.risk import (
    RISK_RULES_VERSION,
    BrakeState,
    RealizedSetup,
    TrialUsage,
    affordable_rows,
    brake_state,
    gate_window_start,
    position_size,
    template_gate,
    trial_usage,
    week_start,
)
from gexlens_engine.compute.settle import expiry_settle, trading_session_date
from gexlens_engine.compute.setup_library import cell_evidence
from gexlens_engine.compute.setups import (
    PATH_GAP_WAIT,
    SETUP_MECHANICS_VERSION,
    Direction,
    MinuteInputs,
    Outcome,
    PathBar,
    PathState,
    SetupCandidate,
    SetupParams,
    SetupTemplate,
    TrialCell,
    UserStage,
    average_true_range,
    born_after_settle,
    detect_all,
    gex_regime,
    is_counter_regime,
    max_pain_strike,
    missing_minutes,
    path_start,
    r_result,
    template_label,
    walk_setup_path,
)
from gexlens_engine.config import Settings
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.runtime import EngineRuntime, PublisherLike
from gexlens_engine.storage.oi_archive import OIEodRepository
from gexlens_engine.storage.parquet_store import FeatureRow, SnapshotWriter
from gexlens_engine.storage.setups_store import SetupsRepository, StoredSetup

logger = logging.getLogger(__name__)

HISTORY_MINUTES = 400
#: Jak často se znovu čte track record pro kalibraci confidence (#794 fáze 2B)
CALIBRATION_REFRESH = dt.timedelta(minutes=10)
#: Bary z partic se berou jen do minuty now − 2 (#1320): partice nese i
#: provizorní bar rozdělané minuty (ADR-0005) a finál minuty N−1 může dorazit
#: až v cyklu N+1. Starší minuty jsou finální.
STORED_BAR_LAG = dt.timedelta(minutes=2)

#: Čtení barů z partic: bary se `since < ts ≤ until` (`parquet_store.read_bars`)
BarReader = Callable[[dt.datetime, dt.datetime], Sequence[PathBar]]
#: Žádost o doplnění minut [první, poslední] do partic (IBKR historical na
#: pozadí); False = jiné doplnění ještě běží, žádost se zopakuje příští cyklus
BarRequester = Callable[[dt.datetime, dt.datetime], bool]


def _utc(value: dt.datetime) -> dt.datetime:
    """Naivní čas z DB (sqlite) je UTC."""
    return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value


def setup_params_from_settings(settings: Settings) -> SetupParams:
    """Prahy šablon z `.env`/defaultů — seed parameter store při prvním startu
    a fallback pro běh bez DB (#794 fáze 2). Jediné místo, kde se `.env` klíče
    `setup_*` mapují na pole SetupParams."""
    return SetupParams(
        min_wall_dominance=settings.setup_min_wall_dominance,
        counter_flow_lookback=settings.setup_counter_flow_lookback,
        counter_stop_cooldown_minutes=settings.setup_counter_stop_cooldown_minutes,
        disabled_templates=settings.setup_disabled_template_set,
        min_risk_atr=settings.setup_min_risk_atr,
        max_rr=settings.setup_max_rr,
        max_stops_per_direction=settings.setup_max_stops_per_direction,
        direction_block_minutes=settings.setup_direction_block_minutes,
    )


@dataclass
class _OpenSetup:
    stored: StoredSetup
    # Kam až je cesta ceny vyhodnocená (#1320), včetně MFE/MAE. Začíná barem
    # vstupu; setup z DB po restartu ho zná z kontextu (`entry_bar_ts`), takže
    # se cesta přehraje z partic od téhož baru jako v živém běhu.
    path: PathState
    # Díra v barech, na které vyhodnocení stojí, a od kdy (#1320); None =
    # neblokuje. Každá díra má vlastní PATH_GAP_WAIT — posun cesty čekání nuluje.
    waiting_gap: tuple[dt.datetime, dt.datetime] | None = None
    blocked_since: dt.datetime | None = None
    # Kontra-režimový setup (#252 C): stop spouští delší cooldown šablony.
    # Setupy načtené z DB po restartu flag nemají (kontext se nenačítá) — žebřík
    # ztrát se po restartu hlídá až od prvního nově vzniklého setupu.
    counter: bool = False
    # Obchodovatelný podle risk pravidel (#1185) — po uzavření se zkontrolují brzdy
    tradeable: bool = False
    # Obchodovatelný jen díky zkoušce (#1323) — čerpá její rozpočet, i po
    # restartu (čte se z kontextu), ať se otevřený setup do čerpání započítá
    gate_overridden: bool = False


BRAKE_LABELS: dict[str, str] = {
    "daily_brake": "denní brzda",
    "weekly_brake": "týdenní brzda",
    "template_stops": "strop stopů šablony",
}

TRADE_BLOCK_LABELS: dict[str, str] = {
    **BRAKE_LABELS,
    "stop_over_budget": "stop nad rozpočtem rizika",
    "stop_over_cap": "stop nad tvrdým stropem",
    "user": "ve stínu z rozhodnutí uživatele",
    "gate": "šablona bez prokázaného edge",
}

#: Stádium do textu nového setupu (zvonek i Telegram, #1323)
STAGE_LABELS: dict[str, str] = {"auto": "AUTO", "shadow": "STÍN", "trial": "ZKOUŠKA"}


def _signed(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:+.{digits}f}"


def evidence_line(context: dict[str, object]) -> str:
    """Řádek důkazu buňky do textu nového setupu (#1323): ØR hrubě a čistě,
    n / n potřebné a verdikt brány z kontextu, který engine právě zapsal.

    Začíná „edge neprokázán:“, když dolní mez ØR není kladná nebo ji vzorek
    ještě nedává — vždy u setupu, který pustila Zkouška. Push je chvíle, kdy
    se na mobilu rozhoduje o penězích; štítek je mitigace z ADR-0038."""
    n = context.get("template_gate_n")
    needed = context.get("template_gate_n_needed")
    lb = context.get("template_gate_lb")
    avg = context.get("template_gate_avg_r")
    net = context.get("template_gate_avg_net_r")
    sample = f"n {n}/{needed}" if isinstance(needed, int) else f"n {n}"
    gate = f"brána {context.get('template_gate')}"
    if isinstance(lb, float):
        gate += f" (LB {lb:+.2f})"
    head = "Důkaz" if isinstance(lb, float) and lb > 0 else "edge neprokázán"
    return (
        f"{head}: ØR {_signed(avg if isinstance(avg, float) else None)} R "
        f"(čistě {_signed(net if isinstance(net, float) else None)}) · {sample} · {gate}"
    )


@dataclass
class SetupEngine:
    symbol: str
    repository: SetupsRepository
    oi_repository: OIEodRepository
    publisher: PublisherLike
    params: SetupParams = field(default_factory=SetupParams)
    # Verze parametrů z parameter store (#794 fáze 2, ADR-0033); None = params
    # přišly z .env/defaultů bez store (testy, vypnutá DB) → setup nese NULL
    params_version: int | None = None
    # Minutový feature log (#796): trénovací matice pro samoučící smyčku (#794).
    # None = vypnuto; zapisuje se do derived/{symbol}/features/ (mimo retenci).
    feature_writer: SnapshotWriter | None = None
    # Bary symbolu z partic (#1320) — díry v živé dávce (výpadek streamu,
    # restart) se dotahují odtud, kam je zapsal backfill nebo doplnění z tasty.
    # None = jen živé bary; díra se pak po PATH_GAP_WAIT přejde s varováním.
    bar_reader: BarReader | None = None
    # Doplnění díry, na které vyhodnocení stojí (#1320): IBKR historical do
    # partic. Stall detektor díru při zamrzlém spotu nevidí (3. 9. stál stream
    # i spot), takže re-backfill po návratu streamu nepřijde — o doplnění si
    # proto říká ten, kdo díru potřebuje. None = jen čekání a čtení partic.
    request_bars: BarRequester | None = None

    def apply_params(self, params: SetupParams, version: int | None) -> bool:
        """Přepne prahy za běhu (nová verze ve store). Vrací True při změně.

        Detektory čtou `self.params` při každé minutě, takže stačí vyměnit
        odkaz; otevřené setupy dojedou s úrovněmi, se kterými vznikly (ty jsou
        uložené v řádku), cooldowny a blokace směru se nemažou.
        """
        if params == self.params and version == self.params_version:
            return False
        self.params = params
        self.params_version = version
        return True

    def __post_init__(self) -> None:
        # Kalibrovaná confidence z track recordu (#794 fáze 2B): tabulka košů,
        # obnovuje se při startu a pak nejvýš jednou za CALIBRATION_REFRESH
        self._calibration: ConfidenceTable | None = None
        self._calibration_ts: dt.datetime | None = None
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
        # Brzdy (#1185): alert jednou per (brzda, seance); realizované řádky
        # týdne se čtou z DB jen když je co rozhodovat
        self._brake_alerted: dict[str, dt.date] = {}
        # Expirace, ke které už je v logu poznámka invariantu #1324 (po settle
        # setupy nevznikají, nebo je nečitelná) — log 1× za expiraci
        self._expiry_logged: str | None = None
        self._max_pain_loaded_for: tuple[str, dt.date, dt.datetime | None] | None = None
        # Díry, o jejichž doplnění už engine požádal (#1320) — jedna žádost na
        # díru, i když na ní stojí víc setupů; bez blokace se množina vyprázdní
        self._requested_gaps: set[tuple[dt.datetime, dt.datetime]] = set()
        # Otevřené setupy z DB (restart enginu) — cesta ceny se přehraje od
        # baru vstupu z partic (#1320), MFE/MAE tím dostanou i minuty výpadku
        for stored in self.repository.active_for(self.symbol):
            self._open.append(
                _OpenSetup(
                    stored=stored,
                    path=self._stored_path(stored),
                    gate_overridden=stored.gate_overridden,
                )
            )

    @staticmethod
    def _stored_path(stored: StoredSetup) -> PathState:
        """Začátek cesty setupu z DB (#1320): bar vstupu z kontextu; starší
        řádky (před #1320, resp. nad spotem do #1346) ho nemají — minuta před vznikem."""
        if stored.entry_bar_ts is None:
            return path_start(_utc(stored.created_ts))
        return PathState(last_ts=_utc(stored.entry_bar_ts), last_close=stored.entry)

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
    def _new_path(entry_bar: Bar) -> PathState:
        """Cesta nového setupu začíná za barem vstupu (#1320).

        Entry je close posledního baru dávky cyklu (`MinuteInputs.close`):
        obvykle N−1, opožděný cyklus nese i bar N a pozdější (NQ po ES, open
        seance), zpožděný stream jen N−2. Bary do baru vstupu včetně proběhly
        před vstupem, pozdější patří cestě. Dávka bez baru setup nevytvoří
        (#1346), bar vstupu tedy existuje vždy."""
        return PathState(last_ts=entry_bar.ts, last_close=entry_bar.close)

    @staticmethod
    def _minutes_to_expiry(expiry: str, now: dt.datetime) -> float | None:
        settle = expiry_settle(expiry)
        if settle is None:
            return None
        return (settle - now).total_seconds() / 60.0

    async def on_minute(
        self, now: dt.datetime, spot: float, bars: list[Bar], runtime: EngineRuntime
    ) -> None:
        if not bars:
            # Dávka bez baru (#1346): minuta se přeskočí — žádná detekce, řádek
            # historie ani feature logu. Spot místo baru jsou vymyšlená data:
            # při výpadku streamu zamrzne a setupy vznikaly nad cenou, kterou
            # trh neměl (ES 1004, NQ 1048, ES 1049). Opožděný bar přijde v další
            # dávce a detekce proběhne nad ním (agregát dávky, vstup = close
            # posledního baru); toky se nečtou, takže přírůstek volume připadne
            # téže agregované minutě. Na doplnění z partic se nečeká: setup je
            # živé upozornění, vstup za cenu několik minut starou nejde zadat.
            # Otevřené setupy se vyhodnocují dál (díry řeší #1320).
            await self._evaluate_open(now, bars)
            return
        levels = runtime.last_levels
        flow = runtime.last_flow
        self._refresh_max_pain(runtime.expiry, now.date())
        call_flow, put_flow, raw_flow = self._flows(runtime)

        bar_open = bars[0].open
        bar_high = max(b.high for b in bars)
        bar_low = min(b.low for b in bars)
        bar_close = bars[-1].close

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

        await self._evaluate_open(now, bars)
        # Bar vstupu = týž bar, jehož close je `inputs.close` (#1320)
        await self._detect_new(now, runtime, inputs, bars[-1])

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
                # Stav mapy téže minuty (#1245) — kolektor běží před detektorem
                thin_map=runtime.thin_map,
            )
            await asyncio.to_thread(
                self.feature_writer.write_features, self.symbol, now.date(), [row]
            )
        except Exception:
            logger.exception("Zápis feature logu selhal — minutový cyklus jede dál")

    async def _stored_bars(self, now: dt.datetime, live: Sequence[Bar]) -> list[PathBar]:
        """Bary z partic pro setupy, kterým živá dávka nenavazuje (#1320).

        Čte se jen při mezeře: setup je pozadu za `now − STORED_BAR_LAG`, mezi
        jeho posledním barem a tou hranicí běžel trh a živá dávka na něj
        nenavazuje (výpadek streamu, restart, opožděné cykly, doplněná díra).
        Běžná minuta partici nečte. Chyba čtení = bez dotažení; setup počká
        (nic se nevymýšlí).
        """
        if self.bar_reader is None:
            return []
        horizon = now.replace(second=0, microsecond=0) - STORED_BAR_LAG
        behind: list[dt.datetime] = []
        for item in self._open:
            last_ts = item.path.last_ts
            if (
                last_ts >= horizon
                or missing_minutes(last_ts, horizon + dt.timedelta(minutes=1)) is None
            ):
                continue
            following = next((bar for bar in live if bar.ts > last_ts), None)
            if following is not None and missing_minutes(last_ts, following.ts) is None:
                continue  # živá dávka navazuje
            behind.append(last_ts)
        if not behind:
            return []
        try:
            return list(await asyncio.to_thread(self.bar_reader, min(behind), horizon))
        except Exception:
            logger.exception(
                "Setupy %s: čtení barů z partic selhalo — vyhodnocení čeká", self.symbol
            )
            return []

    async def _evaluate_open(self, now: dt.datetime, bars: Sequence[Bar] | None = None) -> None:
        # Vyhodnocení po cestě ceny (#1320, dřív #257): bary v pořadí, bez
        # vynechané minuty; outcome i closed_ts patří svíčce, která úroveň
        # zasáhla, ne času cyklu. Díry v živé dávce se dotahují z partic (o
        # doplnění si engine řekne sám), spot se nehodnotí nikdy (zamrzlý spot
        # 3. 9. = 72 min rovné čáry). Timeout podle expirace SETUPU, ne runtime
        # (#259), za close baru končícího v settle — ne za cenu minuty, kdy se
        # timeout zjistil.
        live = sorted(bars or (), key=lambda bar: bar.ts)
        if not self._open:
            self._requested_gaps.clear()
            return
        # Partice první, živé bary po nich: táž minuta z živé dávky vyhrává
        path_bars: list[PathBar] = [*await self._stored_bars(now, live), *live]
        still_open: list[_OpenSetup] = []
        closing: list[tuple[_OpenSetup, Outcome, dt.datetime, float | None]] = []
        for item in self._open:
            direction = Direction(item.stored.direction)
            # Přejde se jen díra, na kterou setup čekal PATH_GAP_WAIT; pozdější
            # díra v téže cestě dostane vlastní čekání i vlastní žádost
            force_until = (
                item.waiting_gap[1]
                if item.waiting_gap is not None
                and item.blocked_since is not None
                and now - item.blocked_since >= PATH_GAP_WAIT
                else None
            )
            previous_ts = item.path.last_ts
            path = walk_setup_path(
                direction,
                item.stored.entry,
                item.stored.target,
                item.stored.stop,
                expiry_settle(item.stored.expiry),
                path_bars,
                item.path,
                now=now,
                force_until=force_until,
            )
            item.path = path.state
            if path.state.last_ts > previous_ts:
                # Cesta pokročila: případná další díra čeká od začátku
                item.waiting_gap = item.blocked_since = None
            # Přejité díry = všechny kromě té, na které krok (znovu) stojí
            skipped = path.gaps[:-1] if path.blocked else path.gaps
            if skipped:
                logger.warning(
                    "Setup %s #%d: bary %s UTC se nedoplnily ani za %d min — vyhodnoceno "
                    "bez nich, výsledek neověřený (#1320)",
                    self.symbol,
                    item.stored.id,
                    ", ".join(f"{a:%d. %m. %H:%M}–{b:%H:%M}" for a, b in skipped),
                    PATH_GAP_WAIT.total_seconds() // 60,
                )
            outcome = path.outcome
            if outcome is None:
                if not path.blocked:
                    item.waiting_gap = item.blocked_since = None
                else:
                    gap = path.gaps[-1]
                    if item.blocked_since is None:
                        item.waiting_gap, item.blocked_since = gap, now
                        # Čekání je běžný stav (bar 19:59 o cyklus později,
                        # doplnění díry) — WARNING až při vyhodnocení bez díry
                        logger.info(
                            "Setup %s #%d: v cestě ceny chybí bary %s–%s UTC — "
                            "vyhodnocení čeká na doplnění, nejdéle %d min (#1320)",
                            self.symbol,
                            item.stored.id,
                            f"{gap[0]:%d. %m. %H:%M}",
                            f"{gap[1]:%H:%M}",
                            PATH_GAP_WAIT.total_seconds() // 60,
                        )
                    self._request_gap(gap)
                still_open.append(item)
                continue
            closing.append(
                (
                    item,
                    outcome,
                    path.closed_ts if path.closed_ts is not None else now,
                    path.exit_price,
                )
            )
        # Uzavření v časovém pořadí barů, ne v pořadí vzniku (#1320): po dohnání
        # díry nebo restartu se v jednom kroku zavře víc setupů a série stopů
        # směru (#302) i cooldown kontra-setupu (#252 C) závisí na pořadí
        closing.sort(key=lambda entry: entry[2])
        closed: list[tuple[_OpenSetup, float]] = []
        for item, outcome, closed_ts, exit_price in closing:
            closed.append((item, await self._close(item, outcome, closed_ts, exit_price, now)))
        self._open = still_open
        if not any(item.blocked_since is not None for item in still_open):
            self._requested_gaps.clear()
        # Setup zkoušky je obchodovatelný; po restartu nese z DB jen gate_overridden
        if any(item.tradeable or item.gate_overridden for item, _ in closed):
            await self._check_brakes(now, closed)

    async def _close(
        self,
        item: _OpenSetup,
        outcome: Outcome,
        closed_ts: dt.datetime,
        exit_price: float | None,
        now: dt.datetime,
    ) -> float:
        """Zapíše uzavření setupu, vedlejší efekty (série stopů, cooldown) a push.
        Vrací výsledek v R (čerpání zkoušky, #1323)."""
        direction = Direction(item.stored.direction)
        if exit_price is None:
            # Timeout bez jediného baru v celém životě setupu: výstupní cena
            # neexistuje — 0 R (za entry), nahlas; přepočet ho neověří
            logger.warning(
                "Setup %s #%d: do settle žádný bar — timeout za entry (0 R), "
                "výsledek neověřený (#1320)",
                self.symbol,
                item.stored.id,
            )
            exit_price = item.stored.entry
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
            mfe=item.path.mfe,
            mae=item.path.mae,
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
                "message": f"Setup #{item.stored.id} uzavřen: {label}, výsledek {result:+.2f} R",
                "ts": now.timestamp(),
            },
        )
        await self.publisher.publish(
            f"setups.{self.symbol}", {"event": "closed", "id": item.stored.id}
        )
        return result

    def _request_gap(self, gap: tuple[dt.datetime, dt.datetime]) -> None:
        """Požádá o doplnění díry do partic, jednou na díru (#1320).

        Odmítnutá žádost (běží jiné doplnění) se zopakuje příští cyklus. Chyba
        hooku nesmí shodit vyhodnocení — setup pak jen čeká."""
        if self.request_bars is None or gap in self._requested_gaps:
            return
        try:
            accepted = self.request_bars(gap[0], gap[1])
        except Exception:
            logger.exception("Setupy %s: žádost o doplnění barů selhala", self.symbol)
            return
        if accepted:
            self._requested_gaps.add(gap)

    def _gate_since(self, now: dt.datetime) -> dt.datetime:
        return gate_window_start(now, self.params.template_gate_days)

    def _load_realized(self, now: dt.datetime) -> list[RealizedSetup]:
        """Blokující čtení uzavřených setupů týdne a okna brány — volat přes to_thread.

        Okno je nejširší z týdne, N seancí brány (≈ 84 dní) a začátků zkoušek
        tohoto tickeru (#1323 — zkouška vzácné šablony může běžet déle než
        okno brány a čerpání by jinak zapomnělo staré uzavřené setupy). Týden
        si z něj vymezuje `brake_state` sám (#1322). Řádky jsou napříč symboly
        kvůli brzdám; brána si z nich vybere vlastní symbol (#1325)."""
        starts = [
            trial.started_at
            for trial in self.params.trial_cells
            if trial.ticker == self.symbol and trial.mechanics_version == SETUP_MECHANICS_VERSION
        ]
        since = min(week_start(trading_session_date(now)), self._gate_since(now), *starts)
        return self.repository.realized_since(since, mechanics_version=SETUP_MECHANICS_VERSION)

    def _trial_in_force(self, template: str, now: dt.datetime) -> TrialCell | None:
        """Zkouška buňky, která teď platí (`TrialCell.in_force`): od začátku a na
        aktuální mechanice. Vyčerpání rozhoduje až `_trial_usage`."""
        trial = self.params.trial_of(self.symbol, template)
        if trial is None or not trial.in_force(now, SETUP_MECHANICS_VERSION):
            return None
        return trial

    def _trial_usage(self, realized: Sequence[RealizedSetup], trial: TrialCell) -> TrialUsage:
        """Čerpání zkoušky buňky: uzavřené z `realized`, otevřené z paměti instance."""
        open_setups = sum(
            1
            for item in self._open
            if item.gate_overridden
            and item.stored.template == trial.template
            and _utc(item.stored.created_ts) >= trial.started_at
        )
        return trial_usage(
            realized,
            self.symbol,
            trial.template,
            started_at=trial.started_at,
            budget_setups=trial.budget_setups,
            budget_r=trial.budget_r,
            open_setups=open_setups,
        )

    def _brakes(
        self, realized: Sequence[RealizedSetup], template: str, now: dt.datetime
    ) -> BrakeState:
        return brake_state(
            realized,
            template,
            session_day=trading_session_date(now),
            daily_brake_r=self.params.daily_brake_r,
            weekly_brake_r=self.params.weekly_brake_r,
            max_template_stops_per_day=self.params.max_template_stops_per_day,
        )

    async def _check_brakes(
        self, now: dt.datetime, closed: Sequence[tuple[_OpenSetup, float]] = ()
    ) -> None:
        """Po uzavření obchodovatelného setupu: dosažená denní/týdenní brzda
        (#1185) i ztrátou vyčerpaná zkouška (#1323) se ohlásí hned, ne až
        u dalšího kandidáta. `closed` = (setup, výsledek v R). Chyba DB = bez alertu."""
        try:
            realized = await asyncio.to_thread(self._load_realized, now)
        except Exception:
            logger.exception("Kontrola brzd a zkoušek selhala — bez alertu")
            return
        await self._alert_brake(self._brakes(realized, "", now), now)
        for item, result in closed:
            if not item.gate_overridden:
                continue
            trial = self._trial_in_force(item.stored.template, now)
            if trial is None or _utc(item.stored.created_ts) < trial.started_at:
                continue  # setup dřívější zkoušky — obnovená počítá od nuly
            usage = self._trial_usage(realized, trial)
            # Před uzavřením se setup počítal jako otevřený: počet stejný, R bez něj.
            # Alert jen na přechodu do vyčerpání — jednou na zkoušku, i po restartu
            spent_before = (
                usage.setups >= trial.budget_setups or usage.sum_r - result <= -trial.budget_r
            )
            if usage.spent and not spent_before:
                await self._alert_trial_spent(trial, usage, now)

    async def _alert_trial_spent(
        self, trial: TrialCell, usage: TrialUsage, now: dt.datetime
    ) -> None:
        """Alert `setup_stage` (#1323): zkouška vyčerpala rozpočet, buňka se
        chová jako Auto. Volá se jen na přechodu do vyčerpání (vznik setupu,
        který doplní počet, nebo uzavření, které překročí ztrátu), takže odejde
        jednou na zkoušku bez stavu v paměti; do `setup_params` se nic nezapisuje."""
        label = template_label(SetupTemplate(trial.template))
        await self.publisher.publish(
            "alerts",
            {
                "kind": "setup_stage",
                "event": "trial_spent",
                "symbol": self.symbol,
                "template": trial.template,
                "message": f"Zkouška {label} · {self.symbol} skončila: "
                f"{usage.setups}/{trial.budget_setups} setupů, {usage.sum_r:+.1f} R "
                f"z {-trial.budget_r:.1f} R → zpět na Auto (rozhoduje brána)",
                "trial_setups": usage.setups,
                "trial_budget_setups": trial.budget_setups,
                "trial_sum_r": usage.sum_r,
                "trial_budget_r": trial.budget_r,
                "ts": now.timestamp(),
            },
        )
        logger.info(
            "Zkouška %s vyčerpána: %d/%d setupů, %+.2f R z −%.1f R",
            trial.cell,
            usage.setups,
            trial.budget_setups,
            usage.sum_r,
            trial.budget_r,
        )

    async def _alert_brake(self, state: BrakeState, now: dt.datetime) -> None:
        if state.block is None or state.block == "template_stops":
            return  # strop šablony je tichý — vidět je v kontextu setupu
        session_day = trading_session_date(now)
        if self._brake_alerted.get(state.block) == session_day:
            return
        self._brake_alerted[state.block] = session_day
        daily = state.block == "daily_brake"
        value = state.day_r if daily else state.week_r
        # Okno brzd = okna `brake_state` (#1322): seance končí v 17:00 CT každý
        # den včetně pátku (`session_bounds`), týden v neděli 17:00 CT
        # (`week_start`) — ne settle 15:00 CT. Tentýž text nese nápověda pushe,
        # Settings → Risk management a tooltip risk sloupce.
        until = (
            "do konce seance (17:00 CT)" if daily else "do konce obchodního týdne (neděle 17:00 CT)"
        )
        await self.publisher.publish(
            "alerts",
            {
                "kind": "risk_brake",
                "event": state.block,
                "symbol": self.symbol,
                "message": f"{BRAKE_LABELS[state.block].capitalize()}: {value:+.1f} R — "
                f"nové setupy jen stínově (neobchodovat) {until}",
                "ts": now.timestamp(),
            },
        )
        logger.info(
            "Risk brzda %s: den %+.2f R, týden %+.2f R", state.block, state.day_r, state.week_r
        )

    def _risk_context(
        self,
        realized: Sequence[RealizedSetup],
        template: str,
        entry: float,
        stop: float,
        point_value: float,
        now: dt.datetime,
        *,
        history_ok: bool = True,
    ) -> tuple[dict[str, object], BrakeState]:
        """Sizing, brzdy, stádium buňky a brána (#1185, #1323) → klíče kontextu
        setupu + stav brzd.

        Pořadí bloků: sizing → brzdy → `user` (buňka ve Stínu) → brána. Aktivní
        Zkouška přebije verdikt brány block i insufficient (`gate_overridden`),
        dokud nevyčerpá rozpočet; vyčerpaná se chová jako Auto (nic se
        nezapisuje), stejně jako zkouška před svým začátkem (cyklus s `now`
        zaokrouhleným na minutu) nebo zahájená na jiné mechanice
        (`TrialCell.in_force`). Sizing ani brzdy nepřebije nic. Verdikt brány
        a důkaz buňky se zapisují vždy, i když rozhodlo něco dřív.
        `history_ok=False` (čtení uzavřených setupů selhalo) = čerpání zkoušky
        nejde ověřit, takže zkouška bránu nepřebije — výpadek DB nesmí riziko
        zvýšit.

        `point_value` je hodnota bodu vlastního symbolu (`runtime.multiplier`);
        sizing kandidáta i dopočet starších řádků brány (klíč šablona × symbol,
        #1325) stojí na ní — cizí symbol brána nečte, jeho hodnotu bodu nepotřebuje."""
        params = self.params
        size = position_size(
            entry,
            stop,
            point_value,
            account_equity_usd=params.account_equity_usd,
            risk_pct=params.risk_pct,
            risk_max_pct=params.risk_max_pct,
        )
        brakes = self._brakes(realized, template, now)
        rows = affordable_rows(
            realized,
            template,
            self.symbol,
            since=self._gate_since(now),
            point_value_usd=point_value,
            account_equity_usd=params.account_equity_usd,
            risk_pct=params.risk_pct,
            risk_max_pct=params.risk_max_pct,
        )
        gate = template_gate(
            [row.outcome_r for row in rows],
            min_samples=params.template_gate_min_samples,
            enabled=params.template_gate_enabled,
        )
        evidence = cell_evidence(rows, POINT_VALUES, min_samples=params.template_gate_min_samples)
        stage: UserStage = params.stage_of(self.symbol, template)
        trial = self._trial_in_force(template, now)
        if stage == "trial" and trial is None:
            stage = "auto"  # zkouška ještě nezačala nebo patří jiné mechanice
        usage = self._trial_usage(realized, trial) if trial is not None and history_ok else None
        if trial is not None and (usage is None or usage.spent):
            stage = "auto"  # vyčerpaná / neověřitelná zkouška = Auto, nic se nepíše
        block: str | None = size.block
        if block is None:
            block = brakes.block
        if block is None and stage == "shadow":
            block = "user"
        gate_overridden = False
        if block is None and gate.verdict in ("block", "insufficient"):
            if stage == "trial":
                gate_overridden = True
            else:
                block = "gate"
        context: dict[str, object] = {
            "risk_rules_version": RISK_RULES_VERSION,
            "account_equity_usd": params.account_equity_usd,
            "point_value_usd": point_value,
            "risk_budget_usd": size.risk_budget_usd,
            "stop_points": size.stop_points,
            "contracts": size.contracts,
            "max_loss_usd": size.max_loss_usd,
            "fee_usd": size.contracts * params.fee_per_contract_usd,
            "affordable": size.affordable,
            "tradeable": block is None,
            "trade_block": block,
            "template_gate": gate.verdict,
            "template_gate_n": gate.n,
            "template_gate_lb": gate.lower_bound,
            # Důkaz buňky (#1323) nad týmž vzorkem jako brána: ØR hrubě a po
            # nákladech ADR-0030, vzorek potřebný na průkaz edge +0,2 R
            "template_gate_avg_r": evidence.avg_r,
            "template_gate_avg_net_r": evidence.avg_net_r,
            "template_gate_n_needed": evidence.n_needed,
            "realized_day_r": brakes.day_r,
            "realized_week_r": brakes.week_r,
            # Stádium platné při vzniku (#1323): auto / shadow / trial; vyčerpaná
            # zkouška = auto (klíče trial_* zůstanou a řeknou proč)
            "user_stage": stage,
            "gate_overridden": gate_overridden,
        }
        if trial is not None and usage is not None:
            context.update(
                {
                    "trial_started_at": trial.started_at.isoformat(),
                    "trial_budget_setups": trial.budget_setups,
                    "trial_budget_r": trial.budget_r,
                    # Čerpání včetně tohoto setupu, pokud ho zkouška pouští
                    "trial_setups": usage.setups + (1 if gate_overridden else 0),
                    "trial_sum_r": usage.sum_r,
                }
            )
        return context, brakes

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

    def _load_calibration(self) -> ConfidenceTable:
        """Blokující čtení track recordu — volat přes to_thread."""
        rows = self.repository.closed_for_calibration(mechanics_version=SETUP_MECHANICS_VERSION)
        return build_confidence_table(rows, min_samples=self.params.confidence_min_samples)

    async def _refresh_calibration(self, now: dt.datetime) -> None:
        """Obnoví koše confidence nejvýš jednou za CALIBRATION_REFRESH; chyba DB
        nechá platit poslední tabulku (nebo konstanty) — detekce jede dál."""
        if (
            self._calibration_ts is not None
            and now - self._calibration_ts < CALIBRATION_REFRESH
            and self._calibration is not None
        ):
            return
        try:
            self._calibration = await asyncio.to_thread(self._load_calibration)
            self._calibration_ts = now
        except Exception:
            logger.exception("Kalibrace confidence selhala — platí poslední tabulka")

    async def _detect_new(
        self,
        now: dt.datetime,
        runtime: EngineRuntime,
        inputs: MinuteInputs,
        entry_bar: Bar,
    ) -> None:
        # Invariant #1324: setup se vztahuje jen k živé expiraci. Hlídá se tady,
        # na jediném místě vzniku, ne v detektorech. Od #1331 pipeline roluje
        # v settle expirace (`expiry_expired`) dřív, než cyklus téže minuty
        # poběží, takže invariant nezasáhne — zůstává jako pojistka se stejnou
        # hranicí (`expiry_settle`).
        if expiry_settle(runtime.expiry) is None:
            # Nečitelná expirace: predikát nerozhodne a invariant neplatí —
            # nahlas, jednou za expiraci (detekce jede dál, nic se nevymýšlí)
            if self._expiry_logged != runtime.expiry:
                self._expiry_logged = runtime.expiry
                logger.warning(
                    "Setupy %s: nečitelná expirace %r — invariant vzniku po settle "
                    "(#1324) ani timeout podle expirace nelze ověřit",
                    self.symbol,
                    runtime.expiry,
                )
        elif born_after_settle(runtime.expiry, now):
            if self._expiry_logged != runtime.expiry:
                self._expiry_logged = runtime.expiry
                logger.info(
                    "Setupy %s: expirace %s je po settle — nové setupy až po rollu "
                    "na další expiraci (pojistka #1324, roll v settle #1331)",
                    self.symbol,
                    runtime.expiry,
                )
            return
        await self._refresh_calibration(now)
        open_templates = {item.stored.template for item in self._open}
        realized: list[RealizedSetup] | None = None
        history_ok = True
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
            # z Dyn profilu minuty. Nad hloubkou stojí (#1060, fáze 2) úprava
            # confidence a dvě stínová pravidla brány — setup vzniká vždy,
            # k řádku se jen zapíše, co by každé pravidlo udělalo.
            band = band_context(runtime.last_profile, candidate.entry)
            depth = band.get("band_depth")
            regime = cast(str | None, candidate.context.get("gex_regime"))
            gate = band_gate_context(depth if isinstance(depth, float) else None, regime)
            # Základ confidence z track recordu (#794 fáze 2B), konstanta šablony
            # jen jako fallback pod minimem vzorku; posun podle pásma (#1060) navrch
            if self._calibration is not None:
                base, source = self._calibration.confidence(
                    self.symbol, template, regime, candidate.confidence
                )
            else:
                base, source = candidate.confidence, "constant"
            confidence = adjusted_confidence(base, gate)
            # Risk pravidla (#1185): sizing, brzdy, brána — setup vzniká vždy,
            # jen nese, zda se dá zobchodovat. Řádky týdne se čtou jednou za
            # minutu s kandidátem; chyba DB = bez brzd (tradeable jen ze sizingu)
            if realized is None:
                try:
                    realized = await asyncio.to_thread(self._load_realized, now)
                except Exception:
                    logger.exception(
                        "Čtení realizovaných setupů selhalo — brzdy neplatí, zkoušky "
                        "nepřebíjejí bránu"
                    )
                    realized = []
                    history_ok = False
            risk, brakes = self._risk_context(
                realized,
                template,
                candidate.entry,
                candidate.stop,
                float(runtime.multiplier),
                now,
                history_ok=history_ok,
            )
            await self._alert_brake(brakes, now)
            tradeable = bool(risk["tradeable"])
            context: dict[str, object] = {
                **candidate.context,
                **band,
                **gate,
                **risk,
                # Denní doba (#1201): odvozené při vzniku, ať jsou koše stabilní
                # i po změně hranic segmentů
                "session_segment": session_segment(now),
                "hour_local": hour_local(now),
                "confidence_base": base,
                "confidence_template": candidate.confidence,
                "confidence_source": source,
            }
            # Bar vstupu (#1320): odtud začíná cesta ceny po restartu i v offline
            # přepočtu; chybí jen u řádků před #1346, které mohly vzniknout nad spotem
            context["entry_bar_ts"] = entry_bar.ts.isoformat()
            setup_id = self.repository.create(
                symbol=self.symbol,
                expiry=runtime.expiry,
                template=template,
                direction=candidate.direction.value,
                created_ts=now,
                entry=candidate.entry,
                target=candidate.target,
                stop=candidate.stop,
                confidence=confidence,
                reason=candidate.reason,
                context=context,
                params_version=self.params_version,
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
                        confidence=confidence,
                        reason=candidate.reason,
                        status="active",
                        entry_bar_ts=entry_bar.ts,
                    ),
                    path=self._new_path(entry_bar),
                    counter=counter,
                    tradeable=tradeable,
                    gate_overridden=risk["gate_overridden"] is True,
                )
            )
            open_templates.add(template)
            await self._publish_created(setup_id, template, candidate, confidence, risk, now)
            logger.info("Setup %s %s #%d: %s", self.symbol, template, setup_id, candidate.reason)
            # Setup, který doplnil počet zkoušky, ji vyčerpal — ohlásit hned
            # (jednou: další setupy buňky už zkouška nepustí)
            trial = self._trial_in_force(template, now)
            trial_setups = risk.get("trial_setups")
            if (
                trial is not None
                and risk["gate_overridden"] is True
                and isinstance(trial_setups, int)
                and trial_setups >= trial.budget_setups
            ):
                sum_r = risk.get("trial_sum_r")
                await self._alert_trial_spent(
                    trial,
                    TrialUsage(
                        setups=trial_setups,
                        sum_r=float(sum_r) if isinstance(sum_r, int | float) else 0.0,
                        spent=True,
                    ),
                    now,
                )

    async def _publish_created(
        self,
        setup_id: int,
        template: str,
        candidate: SetupCandidate,
        confidence: int,
        risk: dict[str, object],
        now: dt.datetime,
    ) -> None:
        """Alert nového setupu do zvonku a (jen tradeable) na Telegram.

        Text nese stádium buňky (#1323: AUTO / STÍN / ZKOUŠKA k/N s čerpáním)
        a řádek důkazu (ØR hrubě a čistě, n / n potřebné, verdikt brány).
        Payload nese `user_stage`, `gate_overridden` a čerpání zkoušky."""
        tradeable = risk["tradeable"] is True
        side = "LONG" if candidate.direction is Direction.LONG else "SHORT"
        block = risk["trade_block"]
        risk_note = (
            f"{risk['contracts']} kontr., riziko {risk['max_loss_usd']:.0f} $"
            if tradeable
            else f"stín: {TRADE_BLOCK_LABELS.get(str(block), str(block))}"
        )
        stage = str(risk["user_stage"])
        stage_note = STAGE_LABELS.get(stage, stage.upper())
        trial_keys = (
            "trial_setups",
            "trial_budget_setups",
            "trial_sum_r",
            "trial_budget_r",
        )
        trial = {key: risk[key] for key in trial_keys if key in risk}
        if stage == "trial":
            stage_note += (
                f" {trial['trial_setups']}/{trial['trial_budget_setups']} "
                f"({float(cast(float, trial['trial_sum_r'])):+.1f} z "
                f"{-float(cast(float, trial['trial_budget_r'])):.1f} R)"
            )
        label = template_label(SetupTemplate(template))
        await self.publisher.publish(
            "alerts",
            {
                "kind": "setup",
                # Proklik ve zvonečku (#186): nový setup vede na graf instrumentu
                "event": "created",
                "symbol": self.symbol,
                "template": template,
                # Číselná confidence pro práh push notifikací (#1175) — text
                # zprávy ji nese jen v procentech
                "confidence": confidence,
                # Neobchodovatelný setup (#1185) push nedostane
                "tradeable": tradeable,
                "user_stage": stage,
                "gate_overridden": risk["gate_overridden"] is True,
                **trial,
                "message": f"Nový setup {side} ({label}) · {stage_note}: "
                f"entry {candidate.entry:g}, cíl {candidate.target:g}, stop {candidate.stop:g} "
                f"(RRR {candidate.rrr:.1f}, conf. {confidence} %, {risk_note}). "
                f"{candidate.reason}\n{evidence_line(risk)}",
                "ts": now.timestamp(),
            },
        )
        await self.publisher.publish(f"setups.{self.symbol}", {"event": "created", "id": setup_id})
