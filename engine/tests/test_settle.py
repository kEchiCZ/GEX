"""Testy sdílené settle konvence (#498, #511): burzovní čas → UTC přes zoneinfo.

Fixní 20:00 UTC platilo jen v letním čase — v zimě je settle 16:00 ET
= 21:00 UTC. Letní chování se přepnutím na zoneinfo NESMÍ změnit.
"""

import datetime as dt

from gexlens_engine.compute.settle import CME_TZ, ET_TZ, session_time_utc, settle_ts


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
