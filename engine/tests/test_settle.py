"""Testy sdílené settle konvence (#498, #511): burzovní čas → UTC přes zoneinfo.

Fixní 20:00 UTC platilo jen v letním čase — v zimě je settle 16:00 ET
= 21:00 UTC. Letní chování se přepnutím na zoneinfo NESMÍ změnit.
"""

import datetime as dt

from gexlens_engine.compute.settle import (
    CME_TZ,
    ET_TZ,
    easter_sunday,
    expiry_settle,
    is_early_close,
    is_trading_session,
    session_time_utc,
    settle_ts,
    trading_session_date,
    us_market_holidays,
)


def test_settle_letni_cas_zustava_20_utc() -> None:
    """Regres #498: v letním čase (EDT) je settle 20:00 UTC jako dřív."""
    assert settle_ts(dt.date(2026, 7, 17)) == dt.datetime(2026, 7, 17, 20, 0, tzinfo=dt.UTC)
    assert settle_ts(dt.date(2026, 8, 5)) == dt.datetime(2026, 8, 5, 20, 0, tzinfo=dt.UTC)


def test_settle_zimni_cas_je_21_utc() -> None:
    """#511: v zimě (EST) je 16:00 ET = 21:00 UTC — fixní hodina se míjela."""
    assert settle_ts(dt.date(2026, 1, 15)) == dt.datetime(2026, 1, 15, 21, 0, tzinfo=dt.UTC)
    assert settle_ts(dt.date(2026, 12, 18)) == dt.datetime(2026, 12, 18, 21, 0, tzinfo=dt.UTC)


def test_settle_kolem_prechodu_dst() -> None:
    """Přechodové dny 2026: DST začíná 8. 3. a končí 1. 11. (US pravidla)."""
    assert settle_ts(dt.date(2026, 3, 6)).hour == 21  # pátek před přechodem — EST
    assert settle_ts(dt.date(2026, 3, 9)).hour == 20  # pondělí po přechodu — EDT
    assert settle_ts(dt.date(2026, 10, 30)).hour == 20  # EDT
    assert settle_ts(dt.date(2026, 11, 2)).hour == 21  # EST


def test_session_time_utc_chicago_i_new_york() -> None:
    day_summer = dt.date(2026, 7, 17)
    day_winter = dt.date(2026, 1, 15)
    # 15:00 CT == 16:00 ET == settle
    assert session_time_utc(day_summer, 15, 0, CME_TZ) == settle_ts(day_summer)
    assert session_time_utc(day_winter, 15, 0, CME_TZ) == settle_ts(day_winter)
    # US open 9:30 ET: léto 13:30 UTC, zima 14:30 UTC
    assert session_time_utc(day_summer, 9, 30, ET_TZ) == dt.datetime(
        2026, 7, 17, 13, 30, tzinfo=dt.UTC
    )
    assert session_time_utc(day_winter, 9, 30, ET_TZ) == dt.datetime(
        2026, 1, 15, 14, 30, tzinfo=dt.UTC
    )


def test_session_bounds_a_trading_session_date() -> None:
    """Obchodní den = Globex seance (ADR-0023, #512/#638): [17:00 CT D−1, 17:00 CT D)."""
    from gexlens_engine.compute.settle import session_bounds, trading_session_date

    start, end = session_bounds(dt.date(2026, 7, 20))
    assert start == dt.datetime(2026, 7, 19, 22, 0, tzinfo=dt.UTC)  # CDT
    assert end == dt.datetime(2026, 7, 20, 22, 0, tzinfo=dt.UTC)
    start_winter, _ = session_bounds(dt.date(2026, 1, 20))
    assert start_winter == dt.datetime(2026, 1, 19, 23, 0, tzinfo=dt.UTC)  # CST

    # Polouzavřený interval: open patří NOVÉ seanci
    assert trading_session_date(start) == dt.date(2026, 7, 20)
    assert trading_session_date(start - dt.timedelta(minutes=1)) == dt.date(2026, 7, 19)
    # Půlnoc UTC uprostřed seance den nemění (19:00 CT)
    assert trading_session_date(dt.datetime(2026, 7, 21, 0, 30, tzinfo=dt.UTC)) == dt.date(
        2026, 7, 21
    )


def test_obchodni_seance_po_pa_vikend_ne() -> None:
    """#1309: neděle 27. 9. 15:15 CEST patří k „seanci“ neděle, která nemá RTH ani
    settle; od 17:00 CT už běží pondělní seance."""
    assert [is_trading_session(dt.date(2026, 9, day)) for day in range(25, 30)] == [
        True,  # pátek
        False,  # sobota
        False,  # neděle
        True,  # pondělí
        True,  # úterý
    ]
    sunday_1515_cest = dt.datetime(2026, 9, 27, 13, 15, tzinfo=dt.UTC)
    assert trading_session_date(sunday_1515_cest) == dt.date(2026, 9, 27)
    assert not is_trading_session(trading_session_date(sunday_1515_cest))
    sunday_open = dt.datetime(2026, 9, 27, 17, 0, tzinfo=CME_TZ)
    assert is_trading_session(trading_session_date(sunday_open))  # pondělí 28. 9.


def test_svatky_nyse_z_pravidel() -> None:
    """#1308, ADR-0046: celodenní svátky NYSE z pravidel (2025 a 2026 = oficiální rozvrh)."""

    def iso(year: int) -> list[str]:
        return sorted(day.isoformat() for day in us_market_holidays(year))

    assert iso(2026) == [
        "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
        "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    ]  # fmt: skip
    assert iso(2025) == [
        "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
        "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
    ]  # fmt: skip
    # Přesuny: Vánoce a Juneteenth v sobotu → pátek, 4. 7. v neděli → pondělí
    assert {dt.date(2027, 12, 24), dt.date(2027, 6, 18), dt.date(2027, 7, 5)} <= (
        us_market_holidays(2027)
    )
    # Nový rok v sobotu (2028) se na pátek 31. 12. 2027 nepřesouvá
    assert dt.date(2027, 12, 31) not in us_market_holidays(2027)
    assert not any(day.month == 1 and day.day <= 2 for day in us_market_holidays(2028))
    assert easter_sunday(2026) == dt.date(2026, 4, 5)
    assert easter_sunday(2027) == dt.date(2027, 3, 28)


def test_svatek_neni_obchodni_den_zkracena_seance_ano() -> None:
    """Thanksgiving a Vánoce nemají US seanci; den po Thanksgiving ano, se settle 13:00 ET."""
    assert not is_trading_session(dt.date(2026, 11, 26))  # Thanksgiving
    assert not is_trading_session(dt.date(2026, 12, 25))  # Vánoce
    assert is_trading_session(dt.date(2026, 11, 27))  # zkrácená seance
    assert is_early_close(dt.date(2026, 11, 27)) and is_early_close(dt.date(2026, 12, 24))
    # 3. 7. 2026 je držený svátek (4. 7. v sobotu), 2. 7. zkrácený není
    assert not is_early_close(dt.date(2026, 7, 3)) and not is_early_close(dt.date(2026, 7, 2))
    assert is_early_close(dt.date(2025, 7, 3))
    # Settle zkrácené seance 13:00 ET (= 18:00 UTC v zimě) — i pro 0DTE expiraci
    assert settle_ts(dt.date(2026, 11, 27)) == dt.datetime(2026, 11, 27, 18, 0, tzinfo=dt.UTC)
    assert expiry_settle("20261127") == dt.datetime(2026, 11, 27, 18, 0, tzinfo=dt.UTC)
    assert settle_ts(dt.date(2026, 11, 25)) == dt.datetime(2026, 11, 25, 21, 0, tzinfo=dt.UTC)
