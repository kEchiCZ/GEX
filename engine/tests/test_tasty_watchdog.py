"""Hlídač mlčícího streamu (#1228): zásah jen při otevřeném trhu, backoff, alert na hraně epizody.

Regresní scénář 19. 9. 2026: sobota, DXLink bez eventů celý den, socket živý —
to je zavřený trh, ne porucha; hlídač nesmí přepojit ani alertovat.
"""

import datetime as dt
from collections.abc import Mapping

import pytest

from gexlens_engine.tasty.watchdog import (
    SilentStreamWatchdog,
    SilentUniverseWatchdog,
    UniverseSample,
    check_universes,
    silent_alert_message,
    silent_universe_alert_message,
)

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


# ── Hlídač opčního univerza per instrument (#1604) ─────────────────────────
# Podklad (front future) tiká týmž streamem, takže globální hlídač výše ticho
# opcí nevidí. Vstup je poslední event univerza aktivního řetězu instrumentu.

EXPIRY = "20260917"
SUNDAY_OPEN = dt.datetime(2026, 10, 25, 22, 0, tzinfo=dt.UTC)  # neděle 17:00 CDT
FRIDAY_LAST = dt.datetime(2026, 10, 23, 20, 59, tzinfo=dt.UTC)  # pátek 15:59 CDT
PAUSE = dt.datetime(2026, 10, 21, 21, 0, tzinfo=dt.UTC)  # středa 16:00 CDT, pauza Globexu


def test_univerzum_rth_resubskripce_pak_reconnect_a_jeden_alert() -> None:
    """Rozhodnuto 26. 10. B: napřed cílená resubskripce, při dalším tiku přepojení."""
    dog = SilentUniverseWatchdog(symbol="ES")
    last = RTH
    assert dog.check(RTH, connected=True, expiry=EXPIRY, last_event_at=last) is None
    assert dog.check(minutes(RTH, 2), connected=True, expiry=EXPIRY, last_event_at=last) is None
    first = dog.check(minutes(RTH, 3), connected=True, expiry=EXPIRY, last_event_at=last)
    assert first is not None and first.remedy == "resubscribe" and first.alert
    assert "opční řetěz ES" in first.reason and "US RTH" in first.reason
    second = dog.check(minutes(RTH, 4), connected=True, expiry=EXPIRY, last_event_at=last)
    assert second is not None and second.remedy == "reconnect" and not second.alert
    # Backoff 5 min od přepojení, pak žebřík znovu od resubskripce bez alertu
    for n in range(5, 9):
        assert dog.check(minutes(RTH, n), connected=True, expiry=EXPIRY, last_event_at=last) is None
    third = dog.check(minutes(RTH, 9), connected=True, expiry=EXPIRY, last_event_at=last)
    assert third is not None and third.remedy == "resubscribe" and not third.alert


def test_univerzum_resubskripce_pomohla_epizoda_konci_az_po_zivem_useku() -> None:
    """Epizoda končí až živým úsekem delším než práh od zásahu, pak nové ticho = nový alert."""
    dog = SilentUniverseWatchdog(symbol="ES")
    dog.check(RTH, connected=True, expiry=EXPIRY, last_event_at=RTH)
    action = dog.check(minutes(RTH, 3), connected=True, expiry=EXPIRY, last_event_at=RTH)
    assert action is not None and action.remedy == "resubscribe"
    for n in range(4, 7):  # univerzum po resubskripci zase dodává
        now = minutes(RTH, n)
        assert dog.check(now, connected=True, expiry=EXPIRY, last_event_at=now) is None
    last = minutes(RTH, 6)
    again = dog.check(minutes(RTH, 9), connected=True, expiry=EXPIRY, last_event_at=last)
    assert again is not None and again.remedy == "resubscribe" and again.alert


def test_univerzum_jen_snimek_po_resubskripci_eskaluje_bez_alertu() -> None:
    """DXLink může po re-add poslat snímek posledních hodnot. Ten epizodu neukončí:
    když univerzum dál mlčí, přijde přepojení a žádný nový alert."""
    dog = SilentUniverseWatchdog(symbol="ES")
    dog.check(RTH, connected=True, expiry=EXPIRY, last_event_at=RTH)
    first = dog.check(minutes(RTH, 3), connected=True, expiry=EXPIRY, last_event_at=RTH)
    assert first is not None and first.alert
    snapshot = minutes(RTH, 3)
    assert dog.check(minutes(RTH, 4), connected=True, expiry=EXPIRY, last_event_at=snapshot) is None
    second = dog.check(minutes(RTH, 6), connected=True, expiry=EXPIRY, last_event_at=snapshot)
    assert second is not None and second.remedy == "reconnect" and not second.alert


def test_univerzum_globex_tolerance_10_min() -> None:
    dog = SilentUniverseWatchdog(symbol="NQ")
    dog.check(NIGHT, connected=True, expiry=EXPIRY, last_event_at=NIGHT)
    assert dog.check(minutes(NIGHT, 9), connected=True, expiry=EXPIRY, last_event_at=NIGHT) is None
    action = dog.check(minutes(NIGHT, 10), connected=True, expiry=EXPIRY, last_event_at=NIGHT)
    assert action is not None and "Globex" in action.reason


def test_mlci_jen_nq_zasah_jen_pro_nq() -> None:
    """Stav má každý instrument zvlášť: ES dodává, NQ mlčí → zásah i alert jen NQ."""
    es = SilentUniverseWatchdog(symbol="ES")
    nq = SilentUniverseWatchdog(symbol="NQ")
    actions = []
    for n in range(4):
        now = minutes(RTH, n)
        assert es.check(now, connected=True, expiry=EXPIRY, last_event_at=now) is None
        actions.append(nq.check(now, connected=True, expiry=EXPIRY, last_event_at=RTH))
    fired = [action for action in actions if action is not None]
    assert len(fired) == 1 and "opční řetěz NQ" in fired[0].reason


@pytest.mark.parametrize(
    "now",
    [SATURDAY, PAUSE],
    ids=["sobota", "denni-pauza"],
)
def test_univerzum_zavreny_trh_bez_zasahu(now: dt.datetime) -> None:
    dog = SilentUniverseWatchdog(symbol="ES")
    for n in range(0, 60, 5):
        assert dog.check(minutes(now, n), connected=True, expiry=EXPIRY, last_event_at=None) is None


def test_univerzum_nedelni_open_meri_od_openu() -> None:
    """Poslední event z pátku: ticho se počítá od nedělního openu, ne od pátku."""
    dog = SilentUniverseWatchdog(symbol="ES")
    for n in range(0, 10):
        now = minutes(SUNDAY_OPEN, n)
        assert dog.check(now, connected=True, expiry=EXPIRY, last_event_at=FRIDAY_LAST) is None
    # Snímek po resubskripci v openu ticho posune
    snapshot = minutes(SUNDAY_OPEN, 9)
    assert (
        dog.check(minutes(SUNDAY_OPEN, 12), connected=True, expiry=EXPIRY, last_event_at=snapshot)
        is None
    )


def test_univerzum_denni_open_17_ct_s_eventem_z_pauzy() -> None:
    dog = SilentUniverseWatchdog(symbol="ES")
    reopen = minutes(PAUSE, 60)  # 17:00 CDT
    for n in range(0, 10):
        assert (
            dog.check(minutes(reopen, n), connected=True, expiry=EXPIRY, last_event_at=PAUSE)
            is None
        )


def test_univerzum_po_rollu_meri_od_zmeny_expirace() -> None:
    """Nové univerzum se po rollu teprve subskribuje — ticho od změny expirace."""
    dog = SilentUniverseWatchdog(symbol="ES")
    settle = dt.datetime(2026, 9, 17, 20, 0, tzinfo=dt.UTC)  # 16:00 EDT, Globex
    dog.check(settle, connected=True, expiry="20260917", last_event_at=settle)
    roll = minutes(settle, 1)
    for n in range(0, 10):
        assert (
            dog.check(minutes(roll, n), connected=True, expiry="20260918", last_event_at=None)
            is None
        )
    action = dog.check(minutes(roll, 10), connected=True, expiry="20260918", last_event_at=None)
    assert action is not None and action.remedy == "resubscribe"


def test_univerzum_bez_spojeni_nezasahuje() -> None:
    dog = SilentUniverseWatchdog(symbol="ES")
    dog.check(RTH, connected=True, expiry=EXPIRY, last_event_at=RTH)
    assert dog.check(minutes(RTH, 5), connected=False, expiry=EXPIRY, last_event_at=RTH) is None


@pytest.mark.parametrize("cumdelta_prints", [False, True], ids=["v5", "v6"])
def test_text_alertu_univerza(cumdelta_prints: bool) -> None:
    text = silent_universe_alert_message("NQ", 185.0, cumdelta_prints=cumdelta_prints)
    assert text.startswith("Opční řetěz NQ z tastytrade mlčel 3 min, podklad tiká")
    assert ("CumΔ NQ z tisků má od prahu ticha mezeru" in text) is cumdelta_prints


# ── Orchestrace tiku (`check_universes`) ────────────────────────────────────


def _universes() -> list[UniverseSample]:
    return [
        UniverseSample("ES", EXPIRY, frozenset({"./ES1"})),
        UniverseSample("NQ", EXPIRY, frozenset({"./NQ1"})),
    ]


def _tick(
    dogs: dict[str, SilentUniverseWatchdog],
    now: dt.datetime,
    events: Mapping[str, dt.datetime | None],
    *,
    subscribed_at: dt.datetime | None = RTH,
    stream_last: dt.datetime | None = None,
    running: dict[str, str] | None = None,
) -> list[tuple[str, str, bool]]:
    actions = check_universes(
        now,
        dogs,
        _universes(),
        running_expiry=running if running is not None else {"ES": EXPIRY, "NQ": EXPIRY},
        last_event_among=lambda streamers: events[next(iter(streamers))],
        connected=True,
        subscribed_at=subscribed_at,
        stream_last_event_at=stream_last if stream_last is not None else now,
    )
    return [(u.symbol, a.remedy, a.alert) for u, a in actions]


def test_tik_jen_mlcici_instrument_s_alertem_a_symbolem() -> None:
    dogs: dict[str, SilentUniverseWatchdog] = {}
    _tick(dogs, RTH, {"./ES1": RTH, "./NQ1": RTH})
    now = minutes(RTH, 3)
    assert _tick(dogs, now, {"./ES1": RTH, "./NQ1": now}) == [("ES", "resubscribe", True)]


def test_tik_mlci_oba_jeden_reconnect_a_nove_mereni_od_subskripce() -> None:
    """Ověřovatel #1604: dva instrumenty nesmí přepojovat jeden po druhém a přerušit subskripci."""
    dogs: dict[str, SilentUniverseWatchdog] = {}
    silent = {"./ES1": RTH, "./NQ1": RTH}
    _tick(dogs, RTH, silent)
    assert _tick(dogs, minutes(RTH, 3), silent) == [
        ("ES", "resubscribe", True),
        ("NQ", "resubscribe", True),
    ]
    assert _tick(dogs, minutes(RTH, 4), silent) == [("ES", "reconnect", False)]
    # Plná subskripce po přepojení ještě běží → nic
    assert _tick(dogs, minutes(RTH, 5), silent, subscribed_at=None) == []
    # Hotová subskripce: ticho obou se měří od ní, NQ znovu nepřepojí hned
    done = minutes(RTH, 6)
    assert _tick(dogs, minutes(RTH, 7), silent, subscribed_at=done) == []
    # Pořád mlčí i po nové subskripci: táž epizoda (bez nových alertů), ES po
    # backoffu znovu resubskripce, NQ eskaluje na přepojení
    assert _tick(dogs, minutes(RTH, 9), silent, subscribed_at=done) == [
        ("ES", "resubscribe", False),
        ("NQ", "reconnect", False),
    ]


def test_tik_mlci_cely_stream_nechava_hlidaci_1228() -> None:
    """Mlčí i podklad: zásah patří globálnímu hlídači (i v jeho backoffu), ne univerzům."""
    dogs: dict[str, SilentUniverseWatchdog] = {}
    silent = {"./ES1": RTH, "./NQ1": RTH}
    _tick(dogs, RTH, silent, stream_last=RTH)
    for n in range(3, 10):
        assert _tick(dogs, minutes(RTH, n), silent, stream_last=RTH) == []


def test_tik_zastarale_trasy_po_rollu_preskoci() -> None:
    dogs: dict[str, SilentUniverseWatchdog] = {}
    silent = {"./ES1": RTH, "./NQ1": RTH}
    running = {"ES": "20260918", "NQ": EXPIRY}  # ES už běží na nové expiraci, trasy staré
    _tick(dogs, RTH, silent, running=running)
    assert _tick(dogs, minutes(RTH, 3), silent, running=running) == [("NQ", "resubscribe", True)]
