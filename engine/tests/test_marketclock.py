"""Testy obchodního kalendáře CME (#339).

Vstupy jsou schválně v **UTC**, ne v CT: v UTC se aplikace pohybuje a právě
na převodu se láme DST, kvůli kterému se rozvrh nesmí aproximovat konstantou.
"""

import datetime as dt

from gexlens_engine.compute.marketclock import is_market_closed


def utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=dt.UTC)


# V červenci platí CDT (UTC−5), takže 16:00 CT = 21:00 UTC, 17:00 CT = 22:00 UTC.
# 2026-07-25 je pátek, 07-26 neděle, 07-29 středa.


def test_sobota_je_cely_den_zavreno() -> None:
    """Přesně tenhle případ byl v DB uložený jako otevřený trh."""
    assert is_market_closed(utc(2026, 7, 25, 23))  # sobota 18:00 CT
    assert is_market_closed(utc(2026, 7, 26, 12))  # sobota 07:00 CT


def test_nedele_otevira_v_17_ct() -> None:
    assert is_market_closed(utc(2026, 7, 26, 21, 59))  # neděle 16:59 CT
    assert not is_market_closed(utc(2026, 7, 26, 22))  # neděle 17:00 CT


def test_patek_po_16_ct_uz_neotevre() -> None:
    assert not is_market_closed(utc(2026, 7, 24, 20, 59))  # pátek 15:59 CT
    assert is_market_closed(utc(2026, 7, 24, 21))  # pátek 16:00 CT
    assert is_market_closed(utc(2026, 7, 25, 3))  # pátek 22:00 CT


def test_denni_prestavka_uprostred_tydne() -> None:
    assert not is_market_closed(utc(2026, 7, 29, 20, 59))  # středa 15:59 CT
    assert is_market_closed(utc(2026, 7, 29, 21, 30))  # středa 16:30 CT
    assert not is_market_closed(utc(2026, 7, 29, 22))  # středa 17:00 CT


def test_dst_posouva_hranici_v_utc() -> None:
    """Tentýž okamžik v UTC je v zimě zavřeno a v létě otevřeno.

    22:30 UTC je v lednu 16:30 CST (přestávka), v červenci 17:30 CDT (běží).
    Pevný posun UTC by jeden z těch dvou případů určil špatně.
    """
    assert is_market_closed(utc(2026, 1, 14, 22, 30))  # středa, CST
    assert not is_market_closed(utc(2026, 7, 29, 22, 30))  # středa, CDT


def test_naivni_cas_se_bere_jako_utc() -> None:
    """Tichý posun o lokální zónu by hodnotu udělal nepředvídatelnou."""
    naive = dt.datetime(2026, 7, 25, 23)  # sobota
    assert is_market_closed(naive) == is_market_closed(utc(2026, 7, 25, 23))
