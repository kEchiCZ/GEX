"""Spot minutového cyklu se stářím (#1600, #1623): pořadí IBKR → 5s bar → tasty."""

import datetime as dt

from gexlens_engine.compute.spot import PriceAt, SpotReading, bar_close_at, resolve_spot

NOW = dt.datetime(2026, 10, 7, 18, 55, 2, tzinfo=dt.UTC)


def ago(seconds: float, price: float) -> PriceAt:
    return PriceAt(price, NOW - dt.timedelta(seconds=seconds))


def test_zivy_ibkr_ma_prednost() -> None:
    reading = resolve_spot(NOW, ibkr=ago(1, 7856.25), bar=ago(3, 7856.0), tasty=ago(1, 7856.125))
    assert reading == SpotReading(7856.25, NOW - dt.timedelta(seconds=1), "ibkr", True)
    assert reading.live == 7856.25


def test_ibkr_mlci_bary_jdou_bere_close_baru() -> None:
    """#1623: ticker mlčí, 5s bary jdou — cena z baru, ne mid z tasty."""
    reading = resolve_spot(NOW, ibkr=ago(240, 7854.75), bar=ago(4, 7856.5), tasty=ago(2, 7856.625))
    assert (reading.price, reading.source, reading.fresh) == (7856.5, "bar", True)


def test_bez_ibkr_ceho_koli_ziveho_bere_tasty() -> None:
    reading = resolve_spot(NOW, ibkr=ago(90, 7854.75), bar=ago(90, 7854.5), tasty=ago(5, 7856.625))
    assert (reading.price, reading.source, reading.fresh) == (7856.625, "tasty", True)


def test_vse_stare_spot_chybi_ale_nese_posledni_cenu() -> None:
    """#1600: zamrzlá cena nehlasuje — `live` None, cena jen pro GEX výpočet a důvod."""
    reading = resolve_spot(
        NOW, ibkr=ago(300, 7854.62), bar=ago(120, 7855.0), tasty=ago(200, 7854.875)
    )
    assert reading.fresh is False
    assert reading.live is None
    assert (reading.price, reading.source) == (7855.0, "bar")
    assert reading.describe(NOW) == "spot: poslední cena 7855.00 (bar) stará 120 s"


def test_hranice_stari_vcetne() -> None:
    assert resolve_spot(NOW, ibkr=ago(30, 1.0), bar=None, tasty=None).fresh is True
    assert resolve_spot(NOW, ibkr=ago(30.5, 1.0), bar=None, tasty=None).fresh is False
    assert resolve_spot(NOW, ibkr=None, bar=ago(60, 2.0), tasty=None).source == "bar"
    assert resolve_spot(NOW, ibkr=None, bar=None, tasty=ago(31, 3.0)).fresh is False


def test_nan_a_nula_nejsou_cena() -> None:
    reading = resolve_spot(NOW, ibkr=ago(1, float("nan")), bar=ago(1, 0.0), tasty=ago(1, 7856.0))
    assert (reading.price, reading.source) == (7856.0, "tasty")


def test_zadny_zdroj() -> None:
    reading = resolve_spot(NOW, ibkr=None, bar=None, tasty=None)
    assert reading == SpotReading(None, None, None, False)
    assert reading.describe(NOW) == "spot: žádná cena"


def test_close_5s_baru_plati_ke_konci_baru() -> None:
    """`RealTimeBar.time` je začátek baru — cena platí k jeho konci; naivní čas = UTC."""
    started = dt.datetime(2026, 10, 7, 18, 54, 55, tzinfo=dt.UTC)
    assert bar_close_at(started, 7856.5) == PriceAt(7856.5, NOW - dt.timedelta(seconds=2))
    naive = bar_close_at(started.replace(tzinfo=None), 7856.5)
    assert naive.ts.tzinfo is not None and naive == bar_close_at(started, 7856.5)
