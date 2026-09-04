"""Čistý detektor sondy T9 „strop nad hlavou" (#577 fáze 1) nad syntetickou historií.

Zóna je pevná (hrany All 135–165, střed 150) — testuje se rozhodování o
přechodu, akceptaci a síle pásma, ne geometrie profilu (tu hlídá
`test_probes.py::test_band_zone_geometry`).
"""

import datetime as dt
from collections.abc import Sequence

import pytest

from gexlens_engine.compute.bandregime import BandZone
from gexlens_engine.compute.setups import (
    PROBE_CEILING,
    PROBE_EXIT,
    Direction,
    ProbeMinute,
    ProbeOccurrence,
    ProbeParams,
    detect_damping_ceiling,
    probe_excursion,
)

START = dt.datetime(2026, 9, 1, 14, 0, tzinfo=dt.UTC)
PARAMS = ProbeParams()
ACCEPT = PARAMS.acceptance_minutes
ZONE = BandZone(all_low=135.0, all_high=165.0, center=150.0, width=30.0, strength_above=1.0)


def minute(idx: int, close: float, zone: BandZone | None = ZONE) -> ProbeMinute:
    return ProbeMinute(
        ts=START + dt.timedelta(minutes=idx),
        high=close + 0.5,
        low=close - 0.5,
        close=close,
        zone=zone,
        band_depth=0.1,
    )


def history(closes: Sequence[float | None], zone: BandZone = ZONE) -> list[ProbeMinute]:
    """None = minuta bez profilu (poloha neznámá)."""
    return [
        minute(idx, 120.0 if close is None else close, None if close is None else zone)
        for idx, close in enumerate(closes)
    ]


def scan(
    closes: Sequence[float | None], zone: BandZone = ZONE
) -> list[tuple[int, ProbeOccurrence]]:
    """Výskyty minutu po minutě — stejně, jak historii krmí sběrač i harness."""
    items = history(closes, zone)
    found: list[tuple[int, ProbeOccurrence]] = []
    for end in range(1, len(items) + 1):
        occurrence = detect_damping_ceiling(items[:end], PARAMS)
        if occurrence is not None:
            found.append((end - 1, occurrence))
    return found


def test_ceiling_long_po_akceptaci_prave_jednou() -> None:
    closes = [120.0] * ACCEPT + [138.0] * ACCEPT + [139.0] * 5
    found = scan(closes)

    assert len(found) == 1
    idx, occurrence = found[0]
    assert idx == 2 * ACCEPT - 1  # N-tá minuta uvnitř, ne minuta přechodu
    assert occurrence.template == PROBE_CEILING
    assert occurrence.direction is Direction.LONG
    assert occurrence.entry == 138.0
    assert occurrence.target == ZONE.center
    assert occurrence.stop == pytest.approx(ZONE.all_low - 0.25 * ZONE.width)
    assert occurrence.ts == START + dt.timedelta(minutes=idx)
    assert occurrence.context["transition_ts"] == (START + dt.timedelta(minutes=ACCEPT)).isoformat()
    assert occurrence.context["zone_center"] == ZONE.center
    assert occurrence.context["band_depth"] == 0.1


def test_exit_short_zrcadlo() -> None:
    closes = [140.0] * ACCEPT + [128.0] * ACCEPT + [127.0] * 5
    found = scan(closes)

    assert len(found) == 1
    idx, occurrence = found[0]
    assert idx == 2 * ACCEPT - 1
    assert occurrence.template == PROBE_EXIT
    assert occurrence.direction is Direction.SHORT
    assert occurrence.entry == 128.0
    assert occurrence.stop == ZONE.all_low
    assert occurrence.target == pytest.approx(128.0 - ZONE.width)


def test_slabe_pasmo_nad_hlavou_vyskyt_nedava() -> None:
    """Podmínka 2 z #577: jádro pásma musí ležet nad cenou (≥ Major podíl)."""
    weak = BandZone(all_low=135.0, all_high=165.0, center=150.0, width=30.0, strength_above=0.5)
    closes = [120.0] * ACCEPT + [138.0] * ACCEPT

    assert scan(closes, weak) == []
    assert scan(closes, ZONE) != []  # tatáž historie se silným pásmem výskyt dá


def test_neakceptovany_prechod_se_zahazuje() -> None:
    """Podmínka 3: návrat pod hranu před N minutami = žádný výskyt, ani později."""
    closes = [120.0] * ACCEPT + [138.0] * (ACCEPT - 1) + [120.0] + [138.0] * (ACCEPT - 1)

    assert scan(closes) == []


def test_vstup_bez_usazeni_pod_pasmem_nestaci() -> None:
    """Flicker na hraně: minuta pod pásmem mezi dvěma uvnitř není přechod."""
    closes = [138.0] * ACCEPT + [120.0] + [138.0] * ACCEPT

    assert scan(closes) == []


def test_smer_prechodu_rozhoduje() -> None:
    """Close uvnitř, ale NÍŽ než minulý = cena nepřišla zespodu (žádný ceiling)."""
    zone_low = BandZone(all_low=100.0, all_high=130.0, center=115.0, width=30.0, strength_above=1.0)
    closes = [90.0] * ACCEPT + [80.0] * ACCEPT

    assert scan(closes, zone_low) == []


def test_neznama_poloha_prechod_prerusi() -> None:
    closes = [120.0] * ACCEPT + [138.0, 138.0, None, 138.0, 138.0]

    assert scan(closes) == []


def test_cena_uz_za_stredem_nema_co_merit() -> None:
    closes = [120.0] * ACCEPT + [138.0] + [155.0] * (ACCEPT - 1)

    assert scan(closes) == []


def test_kratka_historie_vraci_none() -> None:
    assert detect_damping_ceiling(history([120.0] * (2 * ACCEPT - 1)), PARAMS) is None
    assert detect_damping_ceiling([], PARAMS) is None


def test_geometrie_z_minuty_prechodu() -> None:
    """Kotvy hypotézy jsou hrana, kterou cena překročila — ne pozdější přepočet."""
    later = BandZone(all_low=137.0, all_high=167.0, center=152.0, width=30.0, strength_above=1.0)
    items = history([120.0] * ACCEPT + [138.0])
    items += [minute(ACCEPT + i, 138.0, later) for i in range(1, ACCEPT)]

    occurrence = detect_damping_ceiling(items, PARAMS)

    assert occurrence is not None
    assert occurrence.target == ZONE.center
    assert occurrence.context["zone_all_low"] == ZONE.all_low


def test_probe_excursion() -> None:
    assert probe_excursion(Direction.LONG, 100.0, 103.0, 98.0) == (3.0, 2.0)
    assert probe_excursion(Direction.SHORT, 100.0, 103.0, 98.0) == (2.0, 3.0)
