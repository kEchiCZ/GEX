"""CVD podkladu z dxFeed TimeAndSale (#829)."""

import datetime as dt

from gexlens_engine.compute.futures_cvd import FuturesCvdTracker

TS = dt.datetime(2026, 8, 24, 14, 30, tzinfo=dt.UTC)
FRONT = "/ESU6:XCME"


def trade(symbol: str, size: float, aggressor: str | None) -> list[object]:
    """TimeAndSale dle pořadí polí v tasty.stream.EVENT_FIELDS."""
    return [symbol, 0, 7680.0, size, aggressor, False, False]


def tracker() -> FuturesCvdTracker:
    t = FuturesCvdTracker()
    t.register("ES", FRONT)
    return t


def test_agresor_urcuje_znamenko_toku() -> None:
    t = tracker()

    t.on_event("TimeAndSale", trade(FRONT, 10.0, "BUY"))
    t.on_event("TimeAndSale", trade(FRONT, 4.0, "SELL"))

    row = t.close_minute("ES", TS)
    assert row.cvd_delta == 6.0  # 10 buy − 4 sell
    assert row.cvd == 6.0
    assert row.aggressor_share == 1.0


def test_opcni_printy_se_nepocitaji() -> None:
    """Týmž callbackem tečou i všechny opční printy — nesmí do CVD podkladu."""
    t = tracker()

    t.on_event("TimeAndSale", trade(".ESU6P7600:XCME", 100.0, "SELL"))
    t.on_event("TimeAndSale", trade(FRONT, 3.0, "BUY"))

    row = t.close_minute("ES", TS)
    assert row.cvd == 3.0


def test_print_bez_agresora_se_nezapocita_ale_snizi_pokryti() -> None:
    """UNDEFINED agresor je díra v datech — nesmí se tvářit jako nula toku."""
    t = tracker()

    t.on_event("TimeAndSale", trade(FRONT, 5.0, "BUY"))
    t.on_event("TimeAndSale", trade(FRONT, 50.0, "UNDEFINED"))

    row = t.close_minute("ES", TS)
    assert row.cvd == 5.0
    assert row.aggressor_share == 0.5  # polovina printů bez agresora


def test_kumulativ_bezi_pres_minuty_a_minutovy_agregat_se_nuluje() -> None:
    t = tracker()

    t.on_event("TimeAndSale", trade(FRONT, 10.0, "BUY"))
    first = t.close_minute("ES", TS)
    t.on_event("TimeAndSale", trade(FRONT, 2.0, "SELL"))
    second = t.close_minute("ES", TS + dt.timedelta(minutes=1))

    assert (first.cvd_delta, first.cvd) == (10.0, 10.0)
    assert (second.cvd_delta, second.cvd) == (-2.0, 8.0)


def test_roll_session_nuluje_az_pri_zmene_dne() -> None:
    """První volání jen zafixuje seanci — restart uprostřed dne nesmí zahodit tok."""
    t = tracker()
    day = dt.date(2026, 8, 24)

    assert t.roll_session(day) is False  # fixace, bez resetu
    t.on_event("TimeAndSale", trade(FRONT, 7.0, "BUY"))
    assert t.roll_session(day) is False  # táž seance
    assert t.close_minute("ES", TS).cvd == 7.0

    assert t.roll_session(dt.date(2026, 8, 25)) is True
    assert t.close_minute("ES", TS).cvd == 0.0


def test_roll_kontraktu_odpoji_stary_streamer() -> None:
    """Po rollu nesmí tok expirujícího kontraktu přitékat do nové řady."""
    t = tracker()
    t.register("ES", "/ESZ6:XCME")

    t.on_event("TimeAndSale", trade(FRONT, 99.0, "BUY"))  # starý kontrakt
    t.on_event("TimeAndSale", trade("/ESZ6:XCME", 4.0, "BUY"))

    assert t.close_minute("ES", TS).cvd == 4.0
    assert t.is_tracking("ES") is True


def test_neregistrovany_instrument_se_nesleduje() -> None:
    t = FuturesCvdTracker()
    assert t.is_tracking("ES") is False
    t.on_event("TimeAndSale", trade(FRONT, 10.0, "BUY"))
    assert t.close_minute("ES", TS).cvd == 0.0


def test_restore_cum_navaze_po_restartu() -> None:
    t = tracker()
    t.restore_cum("ES", -1200.0)

    t.on_event("TimeAndSale", trade(FRONT, 200.0, "BUY"))

    assert t.close_minute("ES", TS).cvd == -1000.0


def test_status_fields_odlisi_tri_pricini_nuly() -> None:
    """#829: CVD = 0 může znamenat tři různé věci — status je musí rozlišit."""
    t = tracker()

    # (a) registrováno, ale žádné printy
    assert t.status_fields() == {
        "futures_cvd": {
            "ES": {"streamer": FRONT, "trades_minute": 0, "with_aggressor_minute": 0, "cum": 0.0}
        }
    }

    # (b) printy chodí, ale bez agresora → tok zůstane nula, počet ne
    t.on_event("TimeAndSale", trade(FRONT, 50.0, "UNDEFINED"))
    es = t.status_fields()["futures_cvd"]["ES"]  # type: ignore[index]
    assert es == {"streamer": FRONT, "trades_minute": 1, "with_aggressor_minute": 0, "cum": 0.0}

    # (c) plnohodnotný print
    t.on_event("TimeAndSale", trade(FRONT, 5.0, "BUY"))
    es = t.status_fields()["futures_cvd"]["ES"]  # type: ignore[index]
    assert es == {"streamer": FRONT, "trades_minute": 2, "with_aggressor_minute": 1, "cum": 5.0}

    # Neregistrovaný instrument se v diagnostice vůbec neobjeví
    assert "NQ" not in t.status_fields()["futures_cvd"]  # type: ignore[operator]
