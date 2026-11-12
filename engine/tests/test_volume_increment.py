"""Přírůstek kumulativního volume proti maximu seance (#1602, „Hotovo když“)."""

import datetime as dt
from collections.abc import Mapping, Sequence

from gexlens_engine.compute.volume import SessionVolumes, VolumeStep, volume_increment

DAY = dt.date(2026, 10, 15)


def series(values: list[float | None]) -> list[float | None]:
    """Řada jednoho kontraktu po cyklech; None = cyklus bez objemu (NULL z tasty fallbacku)."""
    volumes = SessionVolumes()
    volumes.roll(DAY)
    out: list[float | None] = []
    for value in values:
        out.append(None if value is None else volumes.add("7915C", value).increment)
        volumes.end_cycle()
    return out


def test_pokles_a_navrat_k_maximu_daji_nulu() -> None:
    """1000 → 800 → 760 → 1000 → 1010: přírůstky 0, 0, 0, 10 (první hodnota jen založí stav)."""
    assert series([1000.0, 800.0, 760.0, 1000.0, 1010.0]) == [None, 0.0, 0.0, 0.0, 10.0]


def test_kmitani_se_nezapocte_znovu() -> None:
    """862 → 863 → 862 → 863 → 864: 1, 0, 0, 1 (dřív 1, 0, 1, 1)."""
    assert series([862.0, 863.0, 862.0, 863.0, 864.0]) == [None, 1.0, 0.0, 0.0, 1.0]


def test_minuta_po_mezere_jen_zalozi_stav() -> None:
    """100 → NULL → 130 → 150: 0, –, 0, 20 (Rozhodnuto 21. 10.: B). Neměřený přírůstek = None."""
    assert series([100.0, None, 130.0, 150.0]) == [None, None, None, 20.0]


def test_null_referenci_nemeni() -> None:
    """Maximum přežije NULL i nižší hodnotu po mezeře: 1000 → NULL → 900 → 1000 → 1010."""
    assert series([1000.0, None, 900.0, 1000.0, 1010.0]) == [None, None, None, 0.0, 10.0]


def test_cista_funkce_vraci_referenci_a_pokles() -> None:
    assert volume_increment(None, 1000.0) == VolumeStep(None, 1000.0, False)
    assert volume_increment(1000.0, 800.0) == VolumeStep(0.0, 1000.0, True)
    assert volume_increment(1000.0, 1000.0) == VolumeStep(0.0, 1000.0, False)
    assert volume_increment(1000.0, 1010.0) == VolumeStep(10.0, 1010.0, False)
    assert volume_increment(1000.0, 900.0, after_gap=True) == VolumeStep(None, 1000.0, True)
    assert volume_increment(1000.0, 1300.0, after_gap=True) == VolumeStep(None, 1300.0, False)


def test_kontrakt_bez_kotace_v_cyklu_ma_mezeru() -> None:
    """Kontrakt, který v cyklu chybí úplně (kotace mimo cache), má v dalším cyklu mezeru."""
    volumes = SessionVolumes()
    volumes.roll(DAY)
    volumes.add("C", 100.0)
    volumes.add("P", 50.0)
    volumes.end_cycle()
    volumes.add("P", 55.0)  # C v cyklu chybí
    volumes.end_cycle()
    assert volumes.add("C", 140.0).increment is None
    assert volumes.add("P", 57.0).increment == 2.0


def test_mezera_trva_do_dalsiho_objemu() -> None:
    volumes = SessionVolumes()
    volumes.roll(DAY)
    volumes.add("C", 100.0)
    for _ in range(3):
        volumes.end_cycle()
    assert volumes.add("C", 400.0).increment is None
    volumes.end_cycle()
    assert volumes.add("C", 410.0).increment == 10.0


def test_nova_seance_referenci_smaze_restart_zacina_prazdny() -> None:
    volumes = SessionVolumes()
    volumes.roll(DAY)
    volumes.add("C", 1000.0)
    volumes.end_cycle()
    volumes.roll(DAY)  # táž seance nic nemaže
    assert volumes.add("C", 1005.0).increment == 5.0
    volumes.end_cycle()
    volumes.roll(DAY + dt.timedelta(days=1))  # 17:00 CT: kumulativ IBKR od ~0
    assert volumes.add("C", 40.0).increment is None
    volumes.end_cycle()
    assert volumes.add("C", 46.0).increment == 6.0
    assert SessionVolumes().add("C", 5000.0).increment is None  # restart


def test_gap_all_drzi_maximum() -> None:
    """Předání v rollu: první bar jen založí stav, maximum ze sekundáru zůstává."""
    volumes = SessionVolumes()
    volumes.add("C", 1000.0)
    volumes.end_cycle()
    volumes.gap_all()
    step = volumes.add("C", 900.0)
    assert (step.increment, step.reference) == (None, 1000.0)
    volumes.end_cycle()
    assert volumes.add("C", 1000.0).increment == 0.0


def test_maximum_je_objem_seance_pro_fa() -> None:
    """ADR-0061 bod 1: objem strany = maximum kumulativu, pokles ani návrat ho nezvětší."""
    volumes = SessionVolumes()
    assert volumes.maximum("C") is None
    for value in (100.0, 250.0, 90.0, 250.0):  # propad feedu a návrat (#1602)
        volumes.add("C", value)
        volumes.end_cycle()
    assert volumes.maximum("C") == 250.0
    volumes.roll(DAY)
    volumes.roll(DAY + dt.timedelta(days=1))  # nová seance začíná bez objemu
    assert volumes.maximum("C") is None


# ── Časová díra (#1774, Rozhodnuto 12. 11.: A+) ────────────────────────

SLEEP_DAY = dt.date(2026, 11, 6)


def at(hhmm: str, day: dt.date = SLEEP_DAY) -> dt.datetime:
    hour, minute = hhmm.split(":")
    return dt.datetime(day.year, day.month, day.day, int(hour), int(minute), tzinfo=dt.UTC)


def labelled(
    cycles: Sequence[tuple[dt.datetime, Mapping[str, float | None]]],
    session: dt.date = SLEEP_DAY,
) -> dict[str, list[float | None]]:
    """Cykly konzumenta se štítkem (setupy, tendence): `cycle`, `roll`, `add`, `end_cycle`.

    None v hodnotě = kontrakt v cyklu objem nenese; výstup per kontrakt jen pro cykly s objemem.
    """
    volumes = SessionVolumes()
    out: dict[str, list[float | None]] = {}
    for ts, values in cycles:
        volumes.cycle(ts)
        volumes.roll(session)
        for key, value in values.items():
            if value is not None:
                out.setdefault(key, []).append(volumes.add(key, value).increment)
        volumes.end_cycle()
    return out


def test_spanek_objem_z_cache_ani_cerstvy_po_dire_se_nezapocte() -> None:
    """ES 6. 11.: spánek 08:55–15:11, první sweep nese objem z cache (7410 C 251).

    ATM přinese čerstvý objem v 15:12, křídlo až dalším sweepem křídel v 15:14 —
    obojí jen založí stav, měří se až další minuta (dřív 15:12 Σ 47 981, 15:14 Σ 14 467).
    """
    atm = [240.0, 251.0, 251.0, 4717.0, 4720.0, 4731.0]
    wing = [100.0, 100.0, 100.0, 100.0, 100.0, 180.0, 185.0]
    stamps = [at(t) for t in ("08:54", "08:55", "15:11", "15:12", "15:13", "15:14", "15:15")]
    cycles = [
        (ts, {"7410C": atm[i] if i < len(atm) else None, "7700C": wing[i]})
        for i, ts in enumerate(stamps)
    ]
    out = labelled(cycles)
    assert out["7410C"] == [None, 11.0, None, None, 3.0, 11.0]
    assert out["7700C"] == [None, 0.0, None, None, None, None, 5.0]


def test_ztracena_minuta_stoji_i_dalsi_minutu_likvidniho_kontraktu() -> None:
    """10. 11. 20:41 vypadla: 20:42 (dvě minuty) i 20:43 (první změna) jen založí stav."""
    cycles = [
        (at(t), {"C": v})
        for t, v in (("20:40", 100.0), ("20:42", 130.0), ("20:43", 150.0), ("20:44", 160.0))
    ]
    assert labelled(cycles)["C"] == [None, None, None, 10.0]


def test_souvisle_stitky_pravidlo_nemeni() -> None:
    values = [1000.0, 800.0, 1000.0, 1010.0, None, 1300.0, 1310.0]
    cycles = [(at("10:00") + i * dt.timedelta(minutes=1), {"C": v}) for i, v in enumerate(values)]
    assert labelled(cycles)["C"] == series(values)[:4] + [None, 10.0]


def test_pokles_pod_kotvu_drzi_mezeru() -> None:
    """Kmitání IBKR (499 ↔ 500) kotvu nepřeroste; až 510 založí stav, 515 se měří."""
    cycles = [(at("09:00"), {"C": 480.0})] + [
        (at(t), {"C": v})
        for t, v in (
            ("09:05", 500.0),
            ("09:06", 499.0),
            ("09:07", 500.0),
            ("09:08", 510.0),
            ("09:09", 515.0),
        )
    ]
    assert labelled(cycles)["C"] == [None, None, None, None, None, 5.0]


def test_kontrakt_bez_objemu_v_cyklu_po_dire_kotvu_nema() -> None:
    """Kontrakt bez objemu v prvním cyklu po díře jde jako po mezeře: založí stav a hned měří."""
    cycles: list[tuple[dt.datetime, Mapping[str, float | None]]] = [
        (at("09:00"), {"C": 100.0}),
        (at("09:05"), {"C": None}),
        (at("09:06"), {"C": 130.0}),
        (at("09:07"), {"C": 150.0}),
    ]
    assert labelled(cycles)["C"] == [None, None, 20.0]


def test_nova_seance_diru_neprenasi() -> None:
    """Díra přes hranici seance: `roll` stav i díru smaže, první objem jen založí stav."""
    volumes = SessionVolumes()
    volumes.cycle(at("21:58"))
    volumes.roll(SLEEP_DAY)
    volumes.add("C", 1000.0)
    volumes.end_cycle()
    assert volumes.cycle(at("23:05"))
    volumes.roll(SLEEP_DAY + dt.timedelta(days=1))
    assert volumes.add("C", 20.0).increment is None
    volumes.end_cycle()
    volumes.cycle(at("23:06"))
    assert volumes.add("C", 30.0).increment == 10.0


def test_restart_a_skok_zpet_diru_nedaji() -> None:
    volumes = SessionVolumes()
    assert not volumes.cycle(at("12:09"))  # start procesu: štítek bez předchůdce
    assert not volumes.cycle(at("12:10"))
    assert not volumes.cycle(at("12:08"))  # srovnání hodin zpět
