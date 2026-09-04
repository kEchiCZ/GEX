"""Regresní zámek produkčních detektorů setupů (#577 fáze 1, AC „nulová změna").

Sonda T9 žije vedle produkčních šablon v témže modulu, ale do `detect_all`
nesmí sáhnout: track record je jediná věc, podle které se kalibruje (#394),
a tichá změna filtru některé šablony by ho zanesla bez jediného červeného
testu. Proto se tu nad deterministickou syntetickou historií (400 minut,
seed 577) drží zlatý otisk VŠECH kandidátů, které produkční `detect_all`
vrátí minutu po minutě — včetně entry/cíle/stopu po R-mechanice.

Otisk vznikl nad kódem před zavedením sondy. Změní-li se záměrně mechanika
šablon (zvedá se `SETUP_MECHANICS_VERSION`), otisk se přegeneruje
`uv run python engine/tests/test_setups_regression.py` a rozdíl se popíše v PR.
"""

import datetime as dt
import json
import math
import random
from pathlib import Path

from gexlens_engine.compute.setups import (
    DETECTORS,
    SETUP_MECHANICS_VERSION,
    MinuteInputs,
    SetupParams,
    SetupTemplate,
    detect_all,
)

GOLDEN = Path(__file__).parent / "golden" / "setups_regression_577.json"
START = dt.datetime(2026, 8, 3, 13, 30, tzinfo=dt.UTC)
MINUTES = 400


def synthetic_history() -> list[MinuteInputs]:
    """Deterministická historie: sinusový den mezi zdmi s trendovým úsekem.

    Cena osciluje mezi put zdí 7500 a call zdí 7530 kolem flipu 7515, ve druhé
    polovině dne odjíždí nad call zeď (trendový úsek pro T7). CumΔ je náhodná
    procházka s obraty proti ceně u zdí (T1 divergence), toky a opční objem
    šum se seedem — každý běh dá stejnou historii.
    """
    rng = random.Random(577)
    history: list[MinuteInputs] = []
    cum_delta = 0.0
    for idx in range(MINUTES):
        phase = 2 * math.pi * idx / 90
        drift = 0.0 if idx < 240 else 0.12 * (idx - 240)
        close = 7515.0 + 14.0 * math.sin(phase) + drift + rng.uniform(-1.5, 1.5)
        span = rng.uniform(0.5, 3.0)
        high = close + rng.uniform(0.0, span)
        low = close - rng.uniform(0.0, span)
        # CumΔ: proti ceně u zdí (divergence), jinak náhodná procházka
        if close <= 7503.0:
            cum_delta += rng.uniform(20.0, 80.0)
        elif close >= 7527.0:
            cum_delta -= rng.uniform(20.0, 80.0)
        else:
            cum_delta += rng.uniform(-40.0, 40.0)
        call_flow = rng.uniform(0.0, 120.0)
        put_flow = rng.uniform(0.0, 120.0)
        history.append(
            MinuteInputs(
                ts=START + dt.timedelta(minutes=idx),
                open=close + rng.uniform(-1.0, 1.0),
                high=high,
                low=low,
                close=close,
                flip=7515.0,
                call_wall=7530.0,
                put_wall=7500.0,
                max_pain=7512.0,
                cum_delta=cum_delta,
                call_flow=call_flow,
                put_flow=put_flow,
                opt_vol=call_flow + put_flow,
                minutes_to_expiry=float(600 - idx),
                call_wall_dom=0.3,
                put_wall_dom=0.3,
                gex_regime="positive" if close >= 7515.0 else "negative",
            )
        )
    return history


def replay_all(params: SetupParams) -> list[dict[str, object]]:
    """Kandidáti `detect_all` minutu po minutě, bez orchestrace (anti-spam ne)."""
    history = synthetic_history()
    rows: list[dict[str, object]] = []
    for end in range(1, len(history) + 1):
        for candidate in detect_all(history[:end], params):
            rows.append(
                {
                    "minute": end - 1,
                    "template": candidate.template.value,
                    "direction": candidate.direction.value,
                    "entry": round(candidate.entry, 4),
                    "target": round(candidate.target, 4),
                    "stop": round(candidate.stop, 4),
                    "confidence": candidate.confidence,
                }
            )
    return rows


def test_production_detectors_match_golden() -> None:
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))

    assert expected["mechanics_version"] == SETUP_MECHANICS_VERSION
    assert replay_all(SetupParams()) == expected["candidates"]


def test_golden_covers_more_than_one_template() -> None:
    """Otisk bez kandidátů by hlídal prázdno — regresní zámek musí mít co držet."""
    templates = {row["template"] for row in replay_all(SetupParams())}

    assert len(templates) >= 2


def test_probe_is_not_a_production_detector() -> None:
    """Sonda T9 nesmí být v `DETECTORS` ani v `SetupTemplate` (žádný setup naostro)."""
    names = {detector.__name__ for detector in DETECTORS}

    assert "detect_damping_ceiling" not in names
    assert {template.value for template in SetupTemplate} == {
        "wall_bounce",
        "failed_break",
        "max_pain_pin",
        "gamma_momentum",
        "divergence_spring",
        "trend_continuation",
    }


if __name__ == "__main__":  # přegenerování otisku po ZÁMĚRNÉ změně mechaniky
    GOLDEN.write_text(
        json.dumps(
            {
                "mechanics_version": SETUP_MECHANICS_VERSION,
                "candidates": replay_all(SetupParams()),
            },
            ensure_ascii=False,
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"zapsáno: {GOLDEN}")
