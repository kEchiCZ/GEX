"""Přírůstek kumulativního volume proti maximu seance (#1602, „Hotovo když“)."""

import datetime as dt

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
