"""Hlídač mlčícího streamu (#1228): zásah jen při otevřeném trhu, backoff, alert na hraně epizody.

Regresní scénář 19. 9. 2026: sobota, DXLink bez eventů celý den, socket živý —
to je zavřený trh, ne porucha; hlídač nesmí přepojit ani alertovat.
"""

import datetime as dt

import pytest

from gexlens_engine.tasty.watchdog import SilentStreamWatchdog, silent_alert_message

RTH = dt.datetime(2026, 9, 17, 14, 0, tzinfo=dt.UTC)  # středa 10:00 ET
NIGHT = dt.datetime(2026, 9, 17, 3, 0, tzinfo=dt.UTC)  # úterý 22:00 CT, Globex noc
SATURDAY = dt.datetime(2026, 9, 19, 14, 0, tzinfo=dt.UTC)


def minutes(base: dt.datetime, n: int) -> dt.datetime:
    return base + dt.timedelta(minutes=n)


def test_rth_ticho_3_min_prepoji_a_alertuje_jen_jednou() -> None:
    dog = SilentStreamWatchdog()
    last = RTH
    assert dog.check(minutes(RTH, 1), connected=True, last_event_at=last) is None
    assert dog.check(minutes(RTH, 2), connected=True, last_event_at=last) is None
    action = dog.check(minutes(RTH, 3), connected=True, last_event_at=last)
    assert action is not None and action.alert and "US RTH" in action.reason
    # Backoff 5 min: další minuty bez zásahu
    assert dog.check(minutes(RTH, 4), connected=True, last_event_at=last) is None
    assert dog.check(minutes(RTH, 7), connected=True, last_event_at=last) is None
    # Po backoffu znovu zásah, ale bez alertu (táž epizoda)
    again = dog.check(minutes(RTH, 8), connected=True, last_event_at=last)
    assert again is not None and not again.alert
    # Eventy zase chodí → epizoda končí; nové ticho = nový alert
    assert dog.check(minutes(RTH, 9), connected=True, last_event_at=minutes(RTH, 9)) is None
    fresh = dog.check(minutes(RTH, 20), connected=True, last_event_at=minutes(RTH, 9))
    assert fresh is not None and fresh.alert


def test_globex_tolerance_10_min() -> None:
    dog = SilentStreamWatchdog()
    assert dog.check(minutes(NIGHT, 5), connected=True, last_event_at=NIGHT) is None
    action = dog.check(minutes(NIGHT, 10), connected=True, last_event_at=NIGHT)
    assert action is not None and "Globex" in action.reason


def test_zavreny_trh_sobota_bez_zasahu() -> None:
    dog = SilentStreamWatchdog()
    for n in range(0, 300, 10):
        assert dog.check(minutes(SATURDAY, n), connected=True, last_event_at=None) is None


def test_bez_spojeni_a_po_startu_neprepojuje() -> None:
    dog = SilentStreamWatchdog()
    # Spadlé spojení řeší run() — hlídač mlčí
    assert dog.check(RTH, connected=False, last_event_at=None) is None
    # Po startu bez jediného eventu se měří od prvního vzorku, ne od epochy
    assert dog.check(minutes(RTH, 1), connected=True, last_event_at=None) is None
    assert dog.check(minutes(RTH, 2), connected=True, last_event_at=None) is None
    assert dog.check(minutes(RTH, 3), connected=True, last_event_at=None) is not None


@pytest.mark.parametrize("cumdelta_prints", [False, True], ids=["v5", "v6"])
def test_text_alertu_podle_mechaniky(cumdelta_prints: bool) -> None:
    """#1460: pod v5 text doslova beze změny, od v6 i mezera CumΔ z tisků (ADR-0057 bod 3)."""
    v5_text = (
        "Tastytrade stream mlčel 3 min při otevřeném trhu — engine ho přepojil sám. "
        "Pokud kotace nenaběhnou do 2 min, zkontroluj stav v Settings."
    )
    suffix = (
        " CumΔ z tisků má od prahu ticha mezeru, dokud tisky znovu nenaběhnou."
        if cumdelta_prints
        else ""
    )
    assert silent_alert_message(185.0, cumdelta_prints=cumdelta_prints) == v5_text + suffix
