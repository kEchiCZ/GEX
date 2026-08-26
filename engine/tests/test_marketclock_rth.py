"""Testy okna US RTH pro remediaci (#877 C) — DST-korektně."""

import datetime as dt

from gexlens_engine.compute.marketclock import outside_us_rth


def test_us_rth_leto_edt() -> None:
    # 26. 8. 2026: RTH 13:30–20:00 UTC (EDT)
    assert outside_us_rth(dt.datetime(2026, 8, 26, 13, 29, tzinfo=dt.UTC))
    assert not outside_us_rth(dt.datetime(2026, 8, 26, 13, 30, tzinfo=dt.UTC))
    assert not outside_us_rth(dt.datetime(2026, 8, 26, 19, 59, tzinfo=dt.UTC))
    assert outside_us_rth(dt.datetime(2026, 8, 26, 20, 0, tzinfo=dt.UTC))


def test_us_rth_zima_est_a_vikend() -> None:
    # 15. 1. 2026 (čt): RTH 14:30–21:00 UTC (EST)
    assert outside_us_rth(dt.datetime(2026, 1, 15, 14, 29, tzinfo=dt.UTC))
    assert not outside_us_rth(dt.datetime(2026, 1, 15, 14, 30, tzinfo=dt.UTC))
    # Sobota je vždy mimo RTH
    assert outside_us_rth(dt.datetime(2026, 8, 29, 15, 0, tzinfo=dt.UTC))
