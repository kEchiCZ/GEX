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

from gexlens_engine.compute.cumdelta import FLOW_WINDOW_MIN, flow_window_sum
from gexlens_engine.compute.gexfield import gamma_at_price
from gexlens_engine.compute.settle import settle_ts, trading_session_date
from gexlens_engine.compute.setups import CUMDELTA_PRINTS, max_pain_strike
from gexlens_engine.compute.tendency import (
    TENDENCY_RULES_V4,
    TENDENCY_RULES_V5,
    BandHysteresis,
    TendencyInputs,
    evaluate_tendency,
)
from gexlens_engine.compute.volume import SessionVolumes
from gexlens_engine.runtime import EngineRuntime, PublisherLike
from gexlens_engine.storage.oi_archive import ArchiveVersion, OIEodRepository
from gexlens_engine.storage.parquet_store import FlowRowLike
from gexlens_engine.storage.tendency_store import TendencyRepository, votes_payload

logger = logging.getLogger(__name__)

# Okno sklonů (Cum Δ, cena, SentIndex) = okno sklonu CumΔ (ADR-0057 bod 4):
# jediná délka pro sklon CumΔ v tendenci i v potvrzení COMBINED v news-engine
# (#1589); sklon ceny a SentIndexu se měří přes totéž okno jako CumΔ
SLOPE_LOOKBACK_MIN = FLOW_WINDOW_MIN
SENTIMENT_SUBDIR = "sentiment"
#: Škrcení warningu o chybějící partici SentIndexu (#1136)
SENTIMENT_MISSING_WARN_MIN = 30
# Sklon ATM IV pro vanna hlas (#397) — delší okno, IV se hýbe pomaleji než cena
IV_LOOKBACK_MIN = 30
#: Okno pro posun zdí (#1241): mapa jde za cenou
WALL_LOOKBACK_MIN = 30


@dataclass
class TendencyEngine:
    symbol: str
    repository: TendencyRepository
    oi_repository: OIEodRepository
    publisher: PublisherLike
    data_dir: Path | None = None
    # Pravidla tendence v5 se zlomem mechaniky v6 (CumΔ z tisků, ADR-0057,
    # #1460). Pole, ne čtení globálu, ať jde v testech přepnout (vzor
    # `SetupEngine.cumdelta_prints`); pod v5 počítá tendence v4 jako dosud.
    cumdelta_prints: bool = CUMDELTA_PRINTS

    def __post_init__(self) -> None:
        # v4: historie (cum_delta, close) pro sklon a rozchod; +1 na okrajovou minutu
        self._history: deque[tuple[dt.datetime, float, float]] = deque(
            maxlen=SLOPE_LOOKBACK_MIN + 1
        )
        # v5: řádky flow (ts_min, flow_delta) pro `flow_window_sum` a cena
        # minut okna pro rozchod — obojí včetně hraniční minuty okna
        self._flow_rows: deque[tuple[dt.datetime, float | None]] = deque(
            maxlen=SLOPE_LOOKBACK_MIN + 1
        )
        self._price_history: deque[tuple[dt.datetime, float]] = deque(maxlen=SLOPE_LOOKBACK_MIN + 1)
        # Reference přírůstků volume: v4 poslední známá hodnota, v5 maximum
        # seance s mezerou (`compute/volume.py`, #1602)
        self._prev_volumes: dict[object, float] = {}
        self._volumes = SessionVolumes()
        self._max_pain: float | None = None
        self._max_pain_loaded_for: tuple[str, dt.date, ArchiveVersion] | None = None
        # Cache parquet řad SentIndexu podle cesty: (mtime, řada). Dnešní partice
        # se přepisuje celá každý cyklus; v5 čte po půlnoci UTC i včerejší
        self._sent_cache: dict[Path, tuple[float, list[tuple[dt.datetime, float]]]] = {}
        self._sent_missing_warned_at: dt.datetime | None = None
        # ATM IV historie pro vanna hlas (#397)
        self._iv_history: deque[tuple[dt.datetime, float]] = deque(maxlen=IV_LOOKBACK_MIN + 1)
        # Zdi před oknem (#1241) — posun ve směru ceny vypíná hlas polohy
        self._wall_history: deque[tuple[dt.datetime, float | None, float | None]] = deque(
            maxlen=WALL_LOOKBACK_MIN + 1
        )
        # Hystereze pásma (#394) — resetuje se na přelomu dne (nový den nemá
        # dědit rozpracované přepnutí ani pásmo včerejšího závěru). v4 klíčuje
        # UTC dnem (reset v 00:00 UTC uprostřed seance), v5 obchodním dnem:
        # reset v 17:00 CT, minuty pauzy patří končící seanci (#1584)
        self._hysteresis = BandHysteresis()
        self._hysteresis_day: dt.date | None = None

    @property
    def version(self) -> int:
        """Verze pravidel tendence této instance (v5 jen se zlomem mechaniky v6)."""
        return TENDENCY_RULES_V5 if self.cumdelta_prints else TENDENCY_RULES_V4

    @property
    def max_pain(self) -> float | None:
        """Max Pain aktivní expirace z posledního obnovení — sdílí ho stav mapy (#1245)."""
        return self._max_pain

    def _refresh_max_pain(self, expiry: str, today: dt.date) -> None:
        """Max Pain z denního archivu OI; přepočet při KAŽDÉ změně snímku (#826).

        Cache klíčovaná jen na (expirace, den) zamrzla na prvním ranním
        načtení, kdy je archiv teprve částečně naplněný — CME publikaci
        dobíhá engine celé dopoledne (#463). NQ 24. 8.: Max Pain se za den
        posunul 29200 → 29400 → 29390 (jak Σ OI rostlo 1 570 → 3 459), ale
        tendence i setupy celý den počítaly s hodnotou z prvního načtení.
        Klíč proto nese verzi archivu expirace (`archive_version`: poslední
        IBKR i tasty zápis, #1605) — mění se jen při novém průchodu archivace
        nebo změně tasty křídla, takže přepočet zůstává levný. Samotný
        `captured_ts` obnovu tasty křídel (~55 % Σ OI 0DTE) nepoznal.
        """
        version = self.oi_repository.archive_version(self.symbol, expiry, today)
        if self._max_pain_loaded_for == (expiry, today, version) and self._max_pain is not None:
            return
        records = self.oi_repository.values_for(self.symbol, expiry, today)
        oi_map = {(r.strike, r.right): r.oi for r in records}
        self._max_pain = max_pain_strike(oi_map)
        self._max_pain_loaded_for = (expiry, today, version)

    def _flows(self, runtime: EngineRuntime, session: dt.date) -> tuple[float | None, float | None]:
        """Δ-vážené přírůstky volume per strana — vlastní stav diffu.

        Nesdílí stav se SetupEngine: dva konzumenti nad jedním stavem by si
        navzájem nulovali přírůstky. Pravidla v5 (zlom mechaniky v6): přírůstek
        proti maximu seance (`compute/volume.py`, #1602) — pokles i návrat
        k maximu dají 0, kontrakt po cyklu bez objemu jen založí stav, nová
        seance (`session`) stav maže. Pravidla v4 beze změny proti poslední
        známé hodnotě (ADR-0057 bod 11).
        """
        call_flow = put_flow = 0.0
        seen = False
        if self.cumdelta_prints:
            self._volumes.roll(session)
        # Aktivní zdroj řetězu, ne přímo sweep cache (#614 fáze 2b)
        for spec, cached in runtime.current_quotes().items():
            snapshot = cached.snapshot
            # Fallback z tasty objem nenese — přírůstek se za něj nepočítá
            # a `seen` zůstane False, takže tendence minutu vynechá místo
            # aby ohlásila nulový tok jako změřený. Totéž platí pro první
            # výskyt kontraktu a pod v5 i pro minutu po mezeře.
            if snapshot.volume is None:
                continue
            increment = self._increment(spec, snapshot.volume)
            if increment is None:
                continue
            seen = True
            if increment <= 0:
                continue
            weighted = increment * abs(snapshot.delta)
            if spec.right == "C":
                call_flow += weighted
            else:
                put_flow += weighted
        if self.cumdelta_prints:
            self._volumes.end_cycle()
        if not seen:
            return None, None
        return call_flow, put_flow

    def _increment(self, spec: object, volume: float) -> float | None:
        """Přírůstek kontraktu; None = neměřený (první výskyt, pod v5 i minuta po mezeře)."""
        if self.cumdelta_prints:
            return self._volumes.add(spec, volume).increment
        previous = self._prev_volumes.get(spec)
        self._prev_volumes[spec] = volume
        return None if previous is None else volume - previous

    def _atm_iv(self, runtime: EngineRuntime, spot: float) -> float | None:
        """Průměrná IV kotací nejblíž spotu (obě strany) — trend řady čte vanna hlas."""
        best_diff: float | None = None
        ivs: list[float] = []
        quotes = runtime.scheduler.quotes()
        for spec in quotes:
            diff = abs(spec.strike - spot)
            if best_diff is None or diff < best_diff - 1e-9:
                best_diff = diff
        if best_diff is None:
            return None
        for spec, cached in quotes.items():
            if abs(spec.strike - spot) <= best_diff + 1e-9:
                iv = cached.snapshot.iv
                if iv is not None and iv > 0:
                    ivs.append(float(iv))
        if not ivs:
            return None
        return sum(ivs) / len(ivs)

    @staticmethod
    def _minutes_to_close(now: dt.datetime) -> float:
        """Minuty do settle dnešní seance (16:00 ET, #511); po close záporné (rampa → 0)."""
        return (settle_ts(now.date()) - now).total_seconds() / 60.0

    def _sentiment_path(self, day: dt.date) -> Path:
        assert self.data_dir is not None
        return (
            self.data_dir
            / "derived"
            / SENTIMENT_SUBDIR
            / self.symbol
            / f"{day.isoformat()}.parquet"
        )

    def _sentiment_series(self, path: Path) -> list[tuple[dt.datetime, float]]:
        """Řada (ts_min, hodnota) partice s cache podle mtime; výjimka = nečitelná."""
        mtime = path.stat().st_mtime
        cached = self._sent_cache.get(path)
        if cached is None or cached[0] != mtime:
            import pyarrow.parquet as pq

            rows = pq.read_table(path).to_pylist()
            cached = (mtime, [(row["ts_min"], float(row["value"])) for row in rows])
            # Drží jen partice, které se ještě čtou (dnešní, v5 po půlnoci i včerejší)
            self._sent_cache = {
                key: value for key, value in self._sent_cache.items() if key.stem >= path.stem
            }
            self._sent_cache[path] = cached
        return cached[1]

    def _sentiment(self, now: dt.datetime) -> tuple[float | None, float | None]:
        """Aktuální hodnota SentIndexu + hodnota před oknem z denní parquet partice.

        Partice píše news-engine per symbol: `derived/sentiment/{SYMBOL}/{den}.parquet`
        (ADR-0026, PR #658). Do 13. 9. 2026 se tu četla plochá cesta bez symbolu,
        která existovala jen do 31. 7. — hlas `sentindex` byl od 13. 8. tiše nula
        (#1136). Chybějící partice se proto loguje (škrceně), ne mlčí.
        v5 hledá hodnotu před oknem i ve včerejší partici, když okno přesahuje
        půlnoc UTC — v4 ji v 00:00–00:09 UTC nenajde a hlas bere „roste“ (#1584).
        """
        if self.data_dir is None:
            return None, None
        path = self._sentiment_path(now.date())
        try:
            if not path.exists():
                self._warn_missing_sentiment(now, path)
                return None, None
            series = self._sentiment_series(path)
        except Exception:
            logger.exception("SentIndex partice %s nečitelná — složka bez dat", path)
            return None, None
        if not series:
            return None, None
        current = series[-1][1]
        threshold = now - dt.timedelta(minutes=SLOPE_LOOKBACK_MIN)
        then = _value_at_or_before(series, threshold)
        if then is None and self.version >= TENDENCY_RULES_V5 and threshold.date() < now.date():
            previous = self._sentiment_path(threshold.date())
            try:
                if previous.exists():
                    then = _value_at_or_before(self._sentiment_series(previous), threshold)
            except Exception:
                logger.exception(
                    "SentIndex partice %s nečitelná — hodnota před oknem chybí", previous
                )
        return current, then

    def _warn_missing_sentiment(self, now: dt.datetime, path: Path) -> None:
        """Warning nejvýš jednou za `SENTIMENT_MISSING_WARN_MIN` — tichý výpadek vstupu
        vydržel měsíc (#1136); log ho zviditelní, ale nesmí zaplavit každou minutu."""
        last = self._sent_missing_warned_at
        if last is not None and now - last < dt.timedelta(minutes=SENTIMENT_MISSING_WARN_MIN):
            return
        self._sent_missing_warned_at = now
        logger.warning(
            "SentIndex partice %s neexistuje — hlas sentindex je 0 (news-engine ji nepíše?)",
            path,
        )

    def _hysteresis_for(self, now: dt.datetime) -> BandHysteresis:
        """Hystereze dne minuty `now`; nový den = čistý stav (v4 UTC den, v5 seance)."""
        day = trading_session_date(now) if self.version >= TENDENCY_RULES_V5 else now.date()
        if self._hysteresis_day != day:
            self._hysteresis = BandHysteresis()
            self._hysteresis_day = day
        return self._hysteresis

    def _slope_inputs(
        self, now: dt.datetime, spot: float, flow: FlowRowLike | None
    ) -> tuple[float | None, float | None]:
        """Sklon CumΔ za okno a cena před oknem (pro rozchod) podle verze pravidel.

        v4: rozdíl CumΔ teď × nejstarší minuta historie ≥ 10 min zpět; přes
        reset CumΔ v 17:00 CT tak srovnává minulou seanci (#1581).
        v5: Σ flowΔ za (now − 10 min, now] v seanci (`flow_window_sum`, ADR-0057
        bod 4); neúplné okno, mezera nebo okno přes začátek seance dá None.
        Cena je přes pauzu spojitá, cena před oknem je proto minuta hranice okna
        bez ohledu na seanci; chybí-li, rozchod nehlasuje.
        """
        window = dt.timedelta(minutes=SLOPE_LOOKBACK_MIN)
        if self.version < TENDENCY_RULES_V5:
            cum_now = flow.cum_delta if flow else None
            cum_then: float | None = None
            price_then: float | None = None
            if self._history:
                oldest = self._history[0]
                # Sklon má smysl až s plným oknem — jinak by ranní minuty srovnávaly
                # proti startu procesu a hlasovaly ze šumu
                if now - oldest[0] >= window:
                    cum_then = oldest[1]
                    price_then = oldest[2]
            if cum_now is not None:
                self._history.append((now, cum_now, spot))
            if cum_now is None or cum_then is None:
                return None, price_then
            return cum_now - cum_then, price_then
        # Řádek minuty jen z cyklu téže minuty: po pádu cyklu nese `last_flow`
        # minulou minutu a okno by ji započetlo dvakrát
        if (
            flow is not None
            and flow.ts_min == now
            and (not self._flow_rows or self._flow_rows[-1][0] != now)
        ):
            self._flow_rows.append((now, flow.flow_delta))
        slope = flow_window_sum(self._flow_rows, now, minutes=SLOPE_LOOKBACK_MIN)
        boundary = now - window
        price_then = next((price for ts, price in self._price_history if ts == boundary), None)
        if not self._price_history or self._price_history[-1][0] != now:
            self._price_history.append((now, spot))
        return slope, price_then

    async def on_minute(self, now: dt.datetime, spot: float, runtime: EngineRuntime) -> None:
        levels = runtime.last_gex_levels
        flow = runtime.last_flow
        self._refresh_max_pain(runtime.expiry, now.date())
        call_flow, put_flow = self._flows(runtime, trading_session_date(now))
        sent_value, sent_then = self._sentiment(now)
        cum_slope, price_then = self._slope_inputs(now, spot, flow)

        atm_iv = self._atm_iv(runtime, spot)
        iv_then: float | None = None
        if self._iv_history:
            iv_oldest = self._iv_history[0]
            if now - iv_oldest[0] >= dt.timedelta(minutes=IV_LOOKBACK_MIN):
                iv_then = iv_oldest[1]
        if atm_iv is not None:
            self._iv_history.append((now, atm_iv))

        call_wall_then: float | None = None
        put_wall_then: float | None = None
        if self._wall_history:
            wall_oldest = self._wall_history[0]
            if now - wall_oldest[0] >= dt.timedelta(minutes=WALL_LOOKBACK_MIN):
                call_wall_then, put_wall_then = wall_oldest[1], wall_oldest[2]
        self._wall_history.append(
            (now, levels.call_wall if levels else None, levels.put_wall if levels else None)
        )
        profile = runtime.last_profile
        charm_profile = runtime.last_charm_profile
        vanna_profile = runtime.last_vanna_profile
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
            call_wall_then=call_wall_then,
            put_wall_then=put_wall_then,
            cum_delta_slope=cum_slope,
            price_then=price_then,
            call_flow=call_flow,
            put_flow=put_flow,
            sent_value=sent_value,
            sent_value_then=sent_then,
            gamma_at_price=gamma_at_price(profile, spot) if profile else None,
            charm_at_price=gamma_at_price(charm_profile, spot) if charm_profile else None,
            vanna_at_price=gamma_at_price(vanna_profile, spot) if vanna_profile else None,
            minutes_to_close=self._minutes_to_close(now),
            iv_now=atm_iv,
            iv_then=iv_then,
        )
        result = evaluate_tendency(inputs, self._hysteresis_for(now), version=self.version)
        if result is None:
            return
        runtime.tendency_band = result.band
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


def _value_at_or_before(
    series: list[tuple[dt.datetime, float]], threshold: dt.datetime
) -> float | None:
    """Poslední hodnota řady s ts ≤ threshold (řada vzestupně podle času)."""
    for ts_min, value in reversed(series):
        if ts_min <= threshold:
            return value
    return None
