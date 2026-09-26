"""Spot fallback na tastytrade (#614).

Scénář, kvůli kterému to vzniklo: uživatel se přihlásí na mobilu do IBKR,
market data se přepnou tam (jsou per uživatel) a cenový graf zamrzne, aniž by
cokoli spadlo. Engine zůstane připojený — jen mu přestanou chodit ticky.
"""

from gexlens_engine.tasty.spot_fallback import SpotFallback


def source_of(fallback: SpotFallback) -> str:
    """Aktivní zdroj jako prostý `str`.

    Bez toho si mypy po prvním `assert ... == "tasty"` zúží typ na
    `Literal['tasty']` a další porovnání s `"ibkr"` označí za nemožné
    (`comparison-overlap`) — přestože právě přepnutí mezi zdroji je to,
    co se testuje.
    """
    return fallback.active_source


def test_pri_zdravem_ibkr_se_publikuje_ibkr() -> None:
    fallback = SpotFallback()

    decision = fallback.on_ibkr(6000.0, now=100.0)

    assert decision.price == 6000.0
    assert decision.source == "ibkr"
    assert decision.switched is False


def test_mobil_prebral_data_prepne_na_tasty() -> None:
    """Jádro issue: IBKR přestane posílat, tasty posílá dál."""
    fallback = SpotFallback(stale_after_s=30.0)
    fallback.on_ibkr(6000.0, now=100.0)

    # 35 s bez jediného ticku z IBKR
    decision = fallback.resolve(now=135.0, tasty_price=6002.5, tasty_fresh=True)

    assert decision.price == 6002.5
    assert decision.source == "tasty"
    assert decision.switched is True


def test_prepnuti_se_hlasi_jen_jednou() -> None:
    """`switched` je hrana, ne stav — jinak by log a alert chodily každou vteřinu."""
    fallback = SpotFallback(stale_after_s=30.0)
    fallback.on_ibkr(6000.0, now=100.0)

    first = fallback.resolve(now=135.0, tasty_price=6002.5, tasty_fresh=True)
    second = fallback.resolve(now=140.0, tasty_price=6003.0, tasty_fresh=True)

    assert first.switched is True
    assert second.switched is False
    assert second.price == 6003.0  # cena teče dál


def test_tichy_trh_fallback_nezapina() -> None:
    """Když mlčí oba zdroje, není to výpadek IBKR — je pauza CME nebo svátek."""
    fallback = SpotFallback(stale_after_s=30.0)
    fallback.on_ibkr(6000.0, now=100.0)

    decision = fallback.resolve(now=200.0, tasty_price=6000.0, tasty_fresh=False)

    assert decision.price is None
    assert decision.source == "ibkr"  # zůstává, nepřepíná se


def test_navrat_na_ibkr_az_po_zotavovacim_okne() -> None:
    """Vracet se při prvním ticku by při kolísavém spojení znamenalo
    přepínání každých pár sekund."""
    fallback = SpotFallback(stale_after_s=30.0, recover_after_s=60.0)
    fallback.on_ibkr(6000.0, now=100.0)
    fallback.resolve(now=135.0, tasty_price=6002.5, tasty_fresh=True)
    assert source_of(fallback) == "tasty"

    # IBKR se ozve, ale zotavovací okno ještě neuplynulo
    early = fallback.on_ibkr(6001.0, now=140.0)
    assert early.price is None
    assert source_of(fallback) == "tasty"

    # Souvislé ticky; přepnout se má přesně na konci okna (140 + 60 = 200)
    switches = [fallback.on_ibkr(6001.0, now=float(ts)) for ts in range(145, 206, 5)]
    switched = [d for d in switches if d.switched]

    assert len(switched) == 1  # hrana, ne opakované hlášení
    assert switched[0].source == "ibkr"
    assert switched[0].price == 6001.0
    assert source_of(fallback) == "ibkr"


def test_dira_uprostred_zotavovani_okno_resetuje() -> None:
    """Půl minuty ticha uprostřed zotavování znamená, že IBKR pořád není v pořádku."""
    fallback = SpotFallback(stale_after_s=30.0, recover_after_s=60.0)
    fallback.on_ibkr(6000.0, now=100.0)
    fallback.resolve(now=135.0, tasty_price=6002.5, tasty_fresh=True)

    fallback.on_ibkr(6001.0, now=140.0)  # zotavování začíná
    fallback.on_ibkr(6001.0, now=180.0)  # 40 s díra → reset
    late = fallback.on_ibkr(6001.0, now=210.0)  # jen 30 s od resetu

    assert late.price is None
    assert source_of(fallback) == "tasty"


def test_behem_fallbacku_se_ibkr_ticky_nepublikuji() -> None:
    """Dvě řady vedle sebe by v grafu vypadaly jako skok ceny."""
    fallback = SpotFallback(stale_after_s=30.0, recover_after_s=60.0)
    fallback.on_ibkr(6000.0, now=100.0)
    fallback.resolve(now=135.0, tasty_price=6002.5, tasty_fresh=True)

    assert fallback.on_ibkr(5999.0, now=136.0).price is None


def test_nan_cena_se_ignoruje() -> None:
    fallback = SpotFallback()

    assert fallback.on_ibkr(float("nan"), now=100.0).price is None


def test_bez_jedineho_ibkr_ticku_se_prepne_na_tasty() -> None:
    """Start enginu, když IBKR feed od začátku nechodí."""
    fallback = SpotFallback(stale_after_s=30.0)

    decision = fallback.resolve(now=50.0, tasty_price=6000.0, tasty_fresh=True)

    assert decision.source == "tasty"
    assert decision.price == 6000.0


# ── Zavřený trh podle rozvrhu (#1307) ──────────────────────────────


def test_zavreny_trh_neprepina_ani_po_snimku_z_resubskripce() -> None:
    """26. 9. 2026 02:13: DXLink se po vypršení tokenu přepojil, snímek
    posledních hodnot udělal tasty „čerstvou" a IBKR od pátku správně mlčel —
    odešlo „IBKR přestal posílat cenu". O víkendu ani v pauze se nepřepíná."""
    fallback = SpotFallback(stale_after_s=30.0)
    fallback.on_ibkr(6000.0, now=100.0)  # poslední páteční tick

    decision = fallback.resolve(
        now=10_000.0, tasty_price=6001.0, tasty_fresh=True, market_closed=True
    )

    assert decision.switched is False
    assert decision.price is None
    assert source_of(fallback) == "ibkr"


def test_fallback_z_doby_pred_uzaverkou_drzi_do_navratu_ibkr() -> None:
    """Uzávěrka fallback nezruší — přepínat zpět bez IBKR dat nemá na co."""
    fallback = SpotFallback(stale_after_s=30.0)
    fallback.on_ibkr(6000.0, now=100.0)
    assert fallback.resolve(now=135.0, tasty_price=6002.0, tasty_fresh=True).switched

    closed = fallback.resolve(now=5_000.0, tasty_price=6003.0, tasty_fresh=True, market_closed=True)

    assert closed.switched is False
    assert closed.source == "tasty"
    assert source_of(fallback) == "tasty"


def test_po_otevreni_se_ticho_ibkr_meri_od_otevreni() -> None:
    """Neděle 17:00:00 CT: IBKR mlčí od pátku, ale to není výpadek — 30 s se
    počítá od otevření. Když IBKR do 30 s ozve, fallback se nezapne."""
    fallback = SpotFallback(stale_after_s=30.0)
    fallback.on_ibkr(6000.0, now=100.0)  # pátek
    fallback.resolve(now=9_000.0, tasty_price=6001.0, tasty_fresh=True, market_closed=True)

    opened = fallback.resolve(now=10_000.0, tasty_price=6010.0, tasty_fresh=True)
    assert opened.switched is False
    fallback.on_ibkr(6011.0, now=10_010.0)  # první tick seance
    later = fallback.resolve(now=10_035.0, tasty_price=6012.0, tasty_fresh=True)

    assert later.switched is False
    assert source_of(fallback) == "ibkr"


def test_po_otevreni_bez_ibkr_ticku_prepne_po_prahu() -> None:
    """Hlídání se po otevření obnoví: když IBKR mlčí i 30 s po otevření,
    je to výpadek (typicky mobil nebo Gateway bez farem) a přepíná se."""
    fallback = SpotFallback(stale_after_s=30.0)
    fallback.on_ibkr(6000.0, now=100.0)
    fallback.resolve(now=9_000.0, tasty_price=6001.0, tasty_fresh=True, market_closed=True)

    early = fallback.resolve(now=10_000.0, tasty_price=6010.0, tasty_fresh=True)
    late = fallback.resolve(now=10_030.0, tasty_price=6011.0, tasty_fresh=True)

    assert early.switched is False
    assert late.switched is True
    assert late.source == "tasty"


def test_denni_pauza_a_navrat_vypadek_v_seanci_prepne_beze_zmeny() -> None:
    """Denní pauza 16–17 CT se nehlásí; výpadek IBKR v běžící seanci po ní
    přepne stejně jako dřív (regrese: otevřený trh se neumlčuje)."""
    fallback = SpotFallback(stale_after_s=30.0)
    fallback.on_ibkr(6000.0, now=0.0)  # 15:59:59 CT
    for t in range(5, 3600, 5):  # pauza: IBKR mlčí, tasty „čerstvá"
        paused = fallback.resolve(
            now=float(t), tasty_price=6000.5, tasty_fresh=True, market_closed=True
        )
        assert paused.switched is False
    opened = fallback.resolve(now=3_600.0, tasty_price=6001.0, tasty_fresh=True)  # 17:00 CT
    assert opened.switched is False
    fallback.on_ibkr(6001.0, now=3_605.0)  # IBKR zase tiká

    # Seance běží, IBKR zmlkne (mobil) → po 30 s přepnutí jako dřív
    decision = fallback.resolve(now=3_640.0, tasty_price=6003.0, tasty_fresh=True)
    assert decision.switched is True
    assert decision.source == "tasty"
