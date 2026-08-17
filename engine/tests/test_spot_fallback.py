"""Spot fallback na tastytrade (#614).

Scénář, kvůli kterému to vzniklo: uživatel se přihlásí na mobilu do IBKR,
market data se přepnou tam (jsou per uživatel) a cenový graf zamrzne, aniž by
cokoli spadlo. Engine zůstane připojený — jen mu přestanou chodit ticky.
"""

from gexlens_engine.tasty.spot_fallback import SpotFallback


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
    assert fallback.active_source == "tasty"

    # IBKR se ozve, ale zotavovací okno ještě neuplynulo
    early = fallback.on_ibkr(6001.0, now=140.0)
    assert early.price is None
    assert fallback.active_source == "tasty"

    # Souvislé ticky; přepnout se má přesně na konci okna (140 + 60 = 200)
    switches = [fallback.on_ibkr(6001.0, now=float(ts)) for ts in range(145, 206, 5)]
    switched = [d for d in switches if d.switched]

    assert len(switched) == 1  # hrana, ne opakované hlášení
    assert switched[0].source == "ibkr"
    assert switched[0].price == 6001.0
    assert fallback.active_source == "ibkr"


def test_dira_uprostred_zotavovani_okno_resetuje() -> None:
    """Půl minuty ticha uprostřed zotavování znamená, že IBKR pořád není v pořádku."""
    fallback = SpotFallback(stale_after_s=30.0, recover_after_s=60.0)
    fallback.on_ibkr(6000.0, now=100.0)
    fallback.resolve(now=135.0, tasty_price=6002.5, tasty_fresh=True)

    fallback.on_ibkr(6001.0, now=140.0)  # zotavování začíná
    fallback.on_ibkr(6001.0, now=180.0)  # 40 s díra → reset
    late = fallback.on_ibkr(6001.0, now=210.0)  # jen 30 s od resetu

    assert late.price is None
    assert fallback.active_source == "tasty"


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
