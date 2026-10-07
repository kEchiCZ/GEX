"""Testy obchodního kalendáře CME (#339).

Vstupy jsou schválně v **UTC**, ne v CT: v UTC se aplikace pohybuje a právě
na převodu se láme DST, kvůli kterému se rozvrh nesmí aproximovat konstantou.
"""

import datetime as dt

from gexlens_engine.compute.marketclock import is_market_closed, outside_us_rth


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


def test_vanoce_globex_zavreny_cely_den() -> None:
    """#1308: pátek 25. 12. 2026 je Globex zavřený od Štědrého dne 12:15 CST
    (zkrácená seance) až do nedělního otevření — o Vánocích dřív ~26 falešných
    upozornění `feed_crosscheck`."""
    assert not is_market_closed(utc(2026, 12, 24, 18, 14))  # čt 12:14 CST
    assert is_market_closed(utc(2026, 12, 24, 18, 15))  # čt 12:15 CST, zkrácená seance
    assert is_market_closed(utc(2026, 12, 24, 23, 30))  # čt 17:30 — večer se neotevírá
    assert is_market_closed(utc(2026, 12, 25, 15))  # pátek 09:00 CST, Vánoce
    assert is_market_closed(utc(2026, 12, 25, 23, 30))  # pátek večer — víkend
    assert not is_market_closed(utc(2026, 12, 27, 23, 30))  # neděle 17:30 CST


def test_thanksgiving_halt_ve_12_ct_a_zkracena_seance() -> None:
    """Thanksgiving 26. 11. 2026: obchod do 12:00 CST, znovu 17:00; pátek 27. 11.
    do 12:15 CST. US seance ani RTH ve svátek nejsou."""
    assert not is_market_closed(utc(2026, 11, 26, 17, 59))  # čt 11:59 CST
    assert is_market_closed(utc(2026, 11, 26, 18, 0))  # čt 12:00 CST, halt
    assert is_market_closed(utc(2026, 11, 26, 22, 59))  # čt 16:59 CST
    assert not is_market_closed(utc(2026, 11, 26, 23, 0))  # čt 17:00 CST, seance pátku
    assert not is_market_closed(utc(2026, 11, 27, 18, 14))  # pá 12:14 CST
    assert is_market_closed(utc(2026, 11, 27, 18, 15))  # pá 12:15 CST, konec týdne
    assert outside_us_rth(utc(2026, 11, 26, 16))  # svátek: žádné RTH
    assert not outside_us_rth(utc(2026, 11, 27, 17, 59))  # pá 12:59 ET
    assert outside_us_rth(utc(2026, 11, 27, 18, 0))  # pá 13:00 ET, zkrácené RTH


def test_velky_patek_a_novy_rok_zavreno_od_predchoziho_vecera() -> None:
    # Velký pátek 3. 4. 2026: čtvrtek večer se neotevírá, pátek zavřeno
    assert is_market_closed(utc(2026, 4, 2, 22, 30))  # čt 17:30 CDT
    assert is_market_closed(utc(2026, 4, 3, 15))
    # Nový rok čtvrtek 1. 1. 2026: večer 31. 12. zavřeno, 1. 1. od 17:00 CST obchod
    assert is_market_closed(utc(2025, 12, 31, 23, 30))
    assert is_market_closed(utc(2026, 1, 1, 18))
    assert not is_market_closed(utc(2026, 1, 1, 23, 30))
    # Labor Day pondělí 7. 9. 2026: do 12:00 CDT obchod, odpoledne zavřeno
    assert not is_market_closed(utc(2026, 9, 7, 16, 59))
    assert is_market_closed(utc(2026, 9, 7, 17, 0))
    assert not is_market_closed(utc(2026, 9, 7, 22, 0))


def test_konec_dst_posouva_nedelni_otevreni() -> None:
    """1. 11. 2026 končí DST: nedělní otevření 17:00 CST = 23:00 UTC (#1307)."""
    assert is_market_closed(utc(2026, 11, 1, 22, 59))  # neděle 16:59 CST
    assert not is_market_closed(utc(2026, 11, 1, 23))  # neděle 17:00 CST
