"""OI zdi — hladiny z denního OI (#851)."""

from gexlens_engine.compute.oiwalls import compute_oi_walls

SPOT = 7700.0


def test_zed_je_maximum_oi_na_sve_strane() -> None:
    oi = {
        (7600.0, "P"): 500.0,
        (7500.0, "P"): 2000.0,  # největší put OI pod spotem
        (7800.0, "C"): 300.0,
        (7900.0, "C"): 1200.0,  # největší call OI nad spotem
    }

    walls = compute_oi_walls(oi, SPOT)

    assert walls.put == 7500.0
    assert walls.call == 7900.0


def test_strany_se_nemichaji() -> None:
    """Call OI pod spotem ani put OI nad ním zeď netvoří — jinak by se smazala
    právě ta informace, kvůli které se hladina kreslí."""
    oi = {
        (7500.0, "C"): 9999.0,  # ITM call pod spotem — ignorovat
        (7900.0, "P"): 9999.0,  # ITM put nad spotem — ignorovat
        (7600.0, "P"): 100.0,
        (7800.0, "C"): 100.0,
    }

    walls = compute_oi_walls(oi, SPOT)

    assert walls.put == 7600.0
    assert walls.call == 7800.0


def test_share_rozlisi_koncentrovanou_zed_od_ploche() -> None:
    """Nízký podíl = „zeď" je jen nejvyšší z mnoha srovnatelných striků."""
    koncentrovana = compute_oi_walls({(7500.0, "P"): 900.0, (7600.0, "P"): 100.0}, SPOT)
    assert koncentrovana.put == 7500.0
    assert koncentrovana.put_share == 0.9

    plocha = compute_oi_walls(
        {(7500.0, "P"): 101.0, (7550.0, "P"): 100.0, (7600.0, "P"): 100.0}, SPOT
    )
    assert plocha.put == 7500.0
    assert plocha.put_share is not None and plocha.put_share < 0.4


def test_bez_oi_neni_zed() -> None:
    """Když data nemáme, hladina se nekreslí — nic se nedopočítává (#851)."""
    assert compute_oi_walls({}, SPOT) == compute_oi_walls({}, SPOT)
    prazdno = compute_oi_walls({}, SPOT)
    assert prazdno.call is None and prazdno.put is None
    assert prazdno.call_share is None and prazdno.put_share is None

    # Nulové OI zeď netvoří (strike bez otevřených pozic)
    nuly = compute_oi_walls({(7500.0, "P"): 0.0, (7900.0, "C"): 0.0}, SPOT)
    assert nuly.put is None and nuly.call is None


def test_remiza_padne_na_nizsi_strike() -> None:
    """Determinismus: tentýž vstup musí dát tutéž hladinu (jako max_pain_strike)."""
    oi = {(7500.0, "P"): 1000.0, (7600.0, "P"): 1000.0}

    assert compute_oi_walls(oi, SPOT).put == 7500.0


def test_serie_striku_sectena_pres_trading_class() -> None:
    """MES má víc sérií se stejným strikem — Σ jde do jedné zdi (#736)."""
    oi = {(7500.0, "P"): 300.0}
    walls = compute_oi_walls(oi, SPOT)
    assert walls.put == 7500.0 and walls.put_share == 1.0
