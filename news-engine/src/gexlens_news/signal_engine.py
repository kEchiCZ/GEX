"""Signal engine — pravidlová Long/Short nápověda (#294, SPEC kap. 6, S9).

Čisté funkce bez I/O (golden testy dle kap. 10); zápis a čtení dělá
`signal_job`. Obě větve (NEWS i COMBINED) se počítají **vždy** — přepínač
OFF/NEWS/COMBINED řídí jen zobrazení (S9), jinak by track record neměl data.

Pinnuté detaily, které SPEC nechává otevřené (ADR-0020):

* **Čerstvost eventu**: event smí založit signál, dokud jeho stáří ≤ τ
  (half-life kategorie×důležitosti) — pak už z indexu z poloviny vyhasl.
* **Expirace** = `ts_event + τ` („vyprší dohasnutím eventu", SPEC 6.3);
  potvrzená změna stavu expiruje aktivní signály okamžitě (signal_job).
* **Strength** = |skóre eventu| (směr × síla × w_cat), ořez na 0–1.
* COMBINED vyžaduje dostupný GEX kontext — bez něj se COMBINED signál
  negeneruje (chybějící kontext není „neutrální kontext").
"""

import datetime as dt
from dataclasses import dataclass
from typing import Any

from gexlens_news.sentindex import half_life_minutes

MODE_NEWS = "NEWS"
MODE_COMBINED = "COMBINED"

LONG = "long"
SHORT = "short"

# Gate (SPEC 6.2): minimálně vzorků ∧ Wilson 95% LB hit-rate nad mincí.
# Bodová hit-rate 55 % při n=20 je od mince nerozlišitelná; při desítkách
# bucketů navíc nějaký „projde" náhodou — proto interval, ne bod.
GATE_MIN_SAMPLES = 30
GATE_WILSON_LB = 0.50


@dataclass(frozen=True)
class BucketStats:
    """Řádek `news_model_stats` bucketu eventu na primárním okně."""

    n: int
    hit_rate_lb: float | None
    ret_mean_bp: float
    window_min: int
    # Ze kterého režimového pohledu bucket je (#402): 'all' = nepodmíněný
    regime: str = "all"


@dataclass(frozen=True)
class GexContext:
    """GEX kontext pro COMBINED (SPEC 6.1): spot vs. flip + směr CumΔ."""

    spot: float
    flip: float | None
    cum_delta_slope: float | None

    def supports(self, direction: str) -> bool:
        """Long: spot nad flipem NEBO CumΔ rostoucí; short zrcadlově."""
        above_flip = self.flip is not None and self.spot > self.flip
        below_flip = self.flip is not None and self.spot < self.flip
        rising = self.cum_delta_slope is not None and self.cum_delta_slope > 0
        falling = self.cum_delta_slope is not None and self.cum_delta_slope < 0
        if direction == LONG:
            return above_flip or rising
        return below_flip or falling

    def snapshot(self) -> dict[str, Any]:
        return {"spot": self.spot, "flip": self.flip, "cum_delta_slope": self.cum_delta_slope}


@dataclass(frozen=True)
class SignalEvent:
    """Event kandidující na signál — skórovaný, s bucket identitou."""

    event_id: int
    ts_event: dt.datetime
    category: str
    importance: int
    score: float  # direction × strength × w_cat (denormalizované skóre)
    surprise_bucket: str
    deferred: bool
    classification_version: int | None


@dataclass(frozen=True)
class SignalCandidate:
    """Signál ke zápisu do `signals` (immutable, S11)."""

    direction: str
    strength: float
    mode: str
    expiry_ts: dt.datetime
    inputs: dict[str, Any]


def gate_passes(stats: BucketStats | None) -> bool:
    """SPEC 6.2: n ≥ 30 nekontaminovaných reakcí ∧ Wilson LB > 0.50."""
    if stats is None or stats.hit_rate_lb is None:
        return False
    return stats.n >= GATE_MIN_SAMPLES and stats.hit_rate_lb > GATE_WILSON_LB


def event_is_fresh(event: SignalEvent, now: dt.datetime) -> bool:
    """Čerstvost = stáří ≤ τ (ADR-0020); budoucí event signál nezakládá."""
    age_min = (now - event.ts_event).total_seconds() / 60.0
    if age_min < 0:
        return False
    return age_min <= half_life_minutes(event.category, event.importance)


def _direction_for(event: SignalEvent, state: str, stats: BucketStats) -> str | None:
    """SPEC 6.3: Long ⇔ RiskOn ∧ score > 0 ∧ pozitivní očekávaná reakce."""
    if state == "RiskOn" and event.score > 0 and stats.ret_mean_bp > 0:
        return LONG
    if state == "RiskOff" and event.score < 0 and stats.ret_mean_bp < 0:
        return SHORT
    return None


def evaluate_event(
    event: SignalEvent,
    *,
    state: str,
    stats: BucketStats | None,
    now: dt.datetime,
    gex: GexContext | None,
) -> list[SignalCandidate]:
    """Signály obou větví pro jeden event; prázdný seznam = žádný signál."""
    if stats is None or not event_is_fresh(event, now) or not gate_passes(stats):
        return []
    direction = _direction_for(event, state, stats)
    if direction is None:
        return []

    tau = half_life_minutes(event.category, event.importance)
    expiry = event.ts_event + dt.timedelta(minutes=tau)
    strength = min(1.0, abs(event.score))
    base_inputs: dict[str, Any] = {
        # Kompletní snapshot zdůvodnění (SPEC 6.3) — signál musí být zpětně
        # vysvětlitelný: který event, která verze klasifikace, který bucket
        "event_id": event.event_id,
        "classification_version": event.classification_version,
        "category": event.category,
        "importance": event.importance,
        "surprise_bucket": event.surprise_bucket,
        "deferred": event.deferred,
        "score": event.score,
        "state": state,
        "bucket": {
            "n": stats.n,
            "hit_rate_lb": stats.hit_rate_lb,
            "ret_mean_bp": stats.ret_mean_bp,
            "window_min": stats.window_min,
            "regime": stats.regime,
        },
        "half_life_min": tau,
    }
    candidates = [
        SignalCandidate(
            direction=direction,
            strength=strength,
            mode=MODE_NEWS,
            expiry_ts=expiry,
            inputs=base_inputs,
        )
    ]
    # COMBINED jen s dostupným a souhlasným GEX kontextem (SPEC 6.1/6.3)
    if gex is not None and gex.supports(direction):
        candidates.append(
            SignalCandidate(
                direction=direction,
                strength=strength,
                mode=MODE_COMBINED,
                expiry_ts=expiry,
                inputs={**base_inputs, "gex": gex.snapshot()},
            )
        )
    return candidates
