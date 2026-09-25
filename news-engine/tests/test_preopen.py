"""Předobchodní upozornění (#1291 Q2, ADR-0043) — čisté funkce nad seznamy."""

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

from gexlens_news.clusters import ClusterEvent
from gexlens_news.preopen import (
    MESSAGE_BUDGET,
    PREOPEN_KIND,
    STAGE_MAIN,
    STAGE_UPDATE,
    PreopenState,
    SessionLevels,
    announced_ids,
    build_preopen,
    counts_text,
    distinct_stories,
    due_stages,
    follows_long_closure,
    is_key,
    last_levels,
    levels_line,
    scheduled_close,
    sentiment_bias,
    upcoming_open,
)

PRAGUE = ZoneInfo("Europe/Prague")
UTC = dt.UTC
OPENING = dt.datetime(2026, 9, 20, 22, 0, tzinfo=UTC)  # neděle 17:00 CDT = po 00:00 CEST
SINCE = dt.datetime(2026, 9, 18, 20, 55, tzinfo=UTC)  # 5 min před pátečním zavřením
LEVELS = SessionLevels(
    ts=dt.datetime(2026, 9, 18, 20, 57, tzinfo=UTC),
    expiry=dt.date(2026, 9, 21),
    call_wall=7730.0,
    put_wall=7680.0,
    flip=7752.04,
    centroid=7715.06,
)


def ev(
    event_id: int,
    at: dt.datetime,
    *,
    title: str,
    kind: str = "headline",
    importance: int | None = 1,
    category: str | None = "OTHER",
    impact: str | None = None,
    direction: int | None = None,
    curated: bool = False,
) -> ClusterEvent:
    return ClusterEvent(
        id=event_id,
        ts_event=at,
        kind=kind,
        title=title,
        importance=importance,
        category=category,
        ff_impact=impact,
        direction=direction,
        curated=curated,
    )


def weekend_events() -> list[ClusterEvent]:
    return [
        ev(
            1,
            dt.datetime(2026, 9, 19, 10, 0, tzinfo=UTC),
            title="Weekend strike on oil facility",
            importance=3,
            category="GEOPOLITICS",
            direction=-1,
        ),
        # Významná (varianta B), ale ne zásadní: importance 2 → jen počtem
        ev(
            2,
            dt.datetime(2026, 9, 19, 12, 0, tzinfo=UTC),
            title="Treasury says tariff talks progressing",
            importance=2,
            category="GEOPOLITICS",
            direction=1,
        ),
        # Earnings nejsou významné (varianta B), šum jen počtem
        ev(
            3,
            dt.datetime(2026, 9, 19, 13, 0, tzinfo=UTC),
            title="Firma X výsledky",
            importance=3,
            category="EARNINGS",
            direction=1,
        ),
        ev(4, dt.datetime(2026, 9, 20, 15, 0, tzinfo=UTC), title="Šum"),
        ev(
            5,
            dt.datetime(2026, 9, 20, 16, 0, tzinfo=UTC),
            title="CNY Industrial Production y/y",
            kind="scheduled",
            importance=1,  # klasifikátor přepsal, rozhoduje FF impact
            impact="Medium",
            direction=0,
        ),
    ]


def main(events: list[ClusterEvent], *, now: dt.datetime | None = None) -> dict[str, object]:
    payload = build_preopen(
        STAGE_MAIN,
        "ES",
        OPENING,
        since=SINCE,
        events=events,
        announced=set(),
        levels=LEVELS,
        last_close=7725.0,
        now=now or dt.datetime(2026, 9, 20, 18, 2, tzinfo=UTC),
        tz=PRAGUE,
    )
    assert payload is not None
    return payload


# ── Kdy ────────────────────────────────────────────────────────────


def test_otevreni_po_vikendu_z_rozvrhu_globexu() -> None:
    sunday = dt.datetime(2026, 9, 20, 18, 0, tzinfo=UTC)
    assert upcoming_open(sunday) == OPENING
    assert upcoming_open(dt.datetime(2026, 9, 18, 21, 30, tzinfo=UTC)) == OPENING  # pátek po závěru
    assert follows_long_closure(OPENING)
    # Všední den: další otevření je po denní pauze → předobchodní souhrn ne
    wednesday = upcoming_open(dt.datetime(2026, 9, 23, 12, 0, tzinfo=UTC))
    assert wednesday == dt.datetime(2026, 9, 23, 22, 0, tzinfo=UTC)
    assert not follows_long_closure(wednesday)
    # Přesně v okamžiku otevření už běží seance → další otevření až po pauze
    assert upcoming_open(OPENING) == dt.datetime(2026, 9, 21, 22, 0, tzinfo=UTC)


def test_casy_etap_od_otevreni_v_ct_pres_dst() -> None:
    """Hlavní souhrn T−4 h, aktualizace T−15 min — od otevření v Chicagu, ne pevně v Praze."""

    def local_times(now: dt.datetime) -> tuple[str, str]:
        opening = upcoming_open(now)
        main_at = (opening - dt.timedelta(hours=4)).astimezone(PRAGUE)
        update_at = (opening - dt.timedelta(minutes=15)).astimezone(PRAGUE)
        return f"{main_at:%a %H:%M}", f"{update_at:%a %H:%M}"

    # Léto (CDT / CEST) a zima (CST / CET): 20:00 a 23:45 Praha
    assert local_times(dt.datetime(2026, 9, 19, 12, 0, tzinfo=UTC)) == ("Sun 20:00", "Sun 23:45")
    assert local_times(dt.datetime(2026, 11, 7, 12, 0, tzinfo=UTC)) == ("Sun 20:00", "Sun 23:45")
    # 25. 10. už Praha v zimním čase, Chicago ještě v letním → o hodinu dřív
    assert local_times(dt.datetime(2026, 10, 24, 12, 0, tzinfo=UTC)) == ("Sun 19:00", "Sun 22:45")


def test_etapy_jednou_a_po_otevreni_zadne() -> None:
    main_at = OPENING - dt.timedelta(hours=4)
    update_at = OPENING - dt.timedelta(minutes=15)
    assert due_stages(main_at - dt.timedelta(minutes=1), OPENING, set()) == []
    assert due_stages(main_at, OPENING, set()) == [STAGE_MAIN]
    assert due_stages(main_at + dt.timedelta(hours=1), OPENING, {STAGE_MAIN}) == []
    assert due_stages(update_at, OPENING, {STAGE_MAIN}) == [STAGE_UPDATE]
    # Proces ve 20:00 neběžel → v čase aktualizace doběhnou obě etapy naráz
    assert due_stages(update_at, OPENING, set()) == [STAGE_MAIN, STAGE_UPDATE]
    assert due_stages(OPENING, OPENING, set()) == []


def test_zavreni_podle_rozvrhu_jako_zaloha() -> None:
    assert scheduled_close(OPENING) == dt.datetime(2026, 9, 18, 21, 0, tzinfo=UTC)  # pá 16:00 CT


# ── Obsah ──────────────────────────────────────────────────────────


def test_hlavni_souhrn_presny_text_a_payload() -> None:
    payload = main(weekend_events())
    assert payload["kind"] == PREOPEN_KIND and payload["symbol"] == "ES"
    assert payload["message"] == (
        "Před otevřením ES: Globex otevře v pondělí 00:00 (za 3 h 58 min)\n"
        "Úrovně ES z poslední seance (expirace 21. 9.): call zeď 7730 · put zeď 7680 · "
        "flip 7752 · těžiště 7715 · close 7725\n"
        "Zásadní zprávy za zavřený trh od pátku 22:55: 2 · sklon 🔴 (🟢 0 · 🔴 1 · ⚪ 1)\n"
        "• ⚪ CNY Industrial Production y/y\n"
        "• 🔴 Weekend strike on oil facility\n"
        "další významné: 1 · ostatní zprávy: 2"
    )
    assert payload["ts"] == int(dt.datetime(2026, 9, 20, 18, 2, tzinfo=UTC).timestamp())
    assert payload["ts_event"] == "2026-09-20T22:00:00+00:00"
    # Jen zásadní, FF Medium před importance 3; významná id 2 jen v počtu
    assert payload["event_ids"] == [5, 1]
    assert "%" not in str(payload["message"])  # žádná pravděpodobnost


def test_bez_zasadni_zpravy_nic() -> None:
    """Jen šum — nic; jen významné bez zásadní — také nic (rozhodnutí podle simulace)."""
    noise_only = [event for event in weekend_events() if event.id in (3, 4)]
    without_key = [event for event in weekend_events() if event.id in (2, 3, 4)]
    for events in (noise_only, without_key):
        for stage in (STAGE_MAIN, STAGE_UPDATE):
            assert (
                build_preopen(
                    stage,
                    "NQ",
                    OPENING,
                    since=SINCE,
                    events=events,
                    announced=set(),
                    levels=None,
                    last_close=None,
                    now=OPENING - dt.timedelta(hours=1),
                    tz=PRAGUE,
                )
                is None
            )


def test_zasadni_zpravy_podmnozina_vyznamnych() -> None:
    at = OPENING - dt.timedelta(hours=5)
    key = [
        ev(1, at, title="USD CPI m/m", kind="scheduled", importance=1, impact="High"),
        ev(2, at, title="ECB Lagarde Speaks", kind="scheduled", impact="Medium"),
        ev(3, at, title="Powell says", importance=3, category="FED"),
        ev(4, at, title="CPI hot", importance=3, category="MACRO_INFLATION"),
        ev(5, at, title="Payrolls", importance=3, category="MACRO_LABOR"),
        ev(6, at, title="GDP", importance=3, category="MACRO_GROWTH"),
        ev(7, at, title="Tariffs on China", importance=3, category="GEOPOLITICS"),
        # Kurátor na sociálních sítích stačí s importance 2 jako ve variantě B
        ev(8, at, title="Post", kind="social", importance=2, category="OTHER", curated=True),
    ]
    significant_only = [
        ev(11, at, title="Inflation eats savings", importance=2, category="MACRO_INFLATION"),
        ev(12, at, title="Fed chair op-ed", importance=2, category="FED"),
        ev(13, at, title="Futures poised ahead of Fed", importance=3, category="OTHER"),
        ev(14, at, title="3 AI stocks before Fed", importance=3, category="TECH"),
        ev(15, at, title="OPEC+ keeps output", importance=3, category="ENERGY"),
    ]
    not_significant = [
        ev(21, at, title="FOMC Member Speaks", kind="scheduled", importance=3, impact="Low"),
        ev(22, at, title="Firma X výsledky", importance=3, category="EARNINGS"),
        ev(23, at, title="Post", kind="social", importance=3, category="FED"),  # ne kurátor
        ev(24, at, title="Šum", importance=1, category="GEOPOLITICS"),
    ]
    assert [e.id for e in key if is_key(e)] == [e.id for e in key]
    assert not any(is_key(e) for e in [*significant_only, *not_significant])
    assert announced_ids([*key, *significant_only, *not_significant]) == {e.id for e in key}
    assert counts_text(0, 0, 0) is None
    assert counts_text(2, 0, 7) == "další zásadní: 2 · ostatní zprávy: 7"


def update(events: list[ClusterEvent], announced: set[int]) -> dict[str, object] | None:
    return build_preopen(
        STAGE_UPDATE,
        "ES",
        OPENING,
        since=SINCE,
        events=events,
        announced=announced,
        levels=LEVELS,
        last_close=7725.0,
        now=OPENING - dt.timedelta(minutes=14),
        tz=PRAGUE,
    )


def test_aktualizace_jen_s_novou_zasadni_zpravou() -> None:
    events = weekend_events()
    assert update(events, {1, 5}) is None
    # Nová významná, která zásadní není, aktualizaci nespustí
    minor = ev(
        7,
        OPENING - dt.timedelta(minutes=20),
        title="Inflation is eating your savings",
        importance=2,
        category="MACRO_INFLATION",
        direction=-1,
    )
    assert update([*events, minor], {1, 5}) is None
    late = ev(
        6,
        OPENING - dt.timedelta(minutes=30),
        title="Iran closes Strait of Hormuz",
        importance=3,
        category="GEOPOLITICS",
        direction=-1,
    )
    payload = update([*events, minor, late], {1, 5})
    assert payload is not None
    assert payload["message"] == (
        "Aktualizace před otevřením ES: Globex otevře v pondělí 00:00 (za 14 min)\n"
        "Úrovně ES z poslední seance (expirace 21. 9.): call zeď 7730 · put zeď 7680 · "
        "flip 7752 · těžiště 7715 · close 7725\n"
        "Nové zásadní zprávy: 1 (celkem 3 od pátku 22:55) · sklon všech 🔴 (🟢 0 · 🔴 2 · ⚪ 1)\n"
        "• 🔴 Iran closes Strait of Hormuz"
    )
    assert payload["event_ids"] == [6]
    # Hlavní souhrn nic neohlásil (ve 20:00 nebyla významná zpráva) → aktualizace
    # s novou zprávou je první upozornění a vypadá jako souhrn
    first = update([late], set())
    assert first is not None and str(first["message"]).startswith("Před otevřením ES:")


def test_sklon_jen_z_poctu_smeru() -> None:
    events = [
        ev(i, OPENING, title="x", direction=d) for i, d in enumerate((1, 1, -1, 0, None), start=1)
    ]
    bias = sentiment_bias(events)
    assert (bias.up, bias.down, bias.neutral) == (2, 1, 2)
    assert bias.text() == "🟢 (🟢 2 · 🔴 1 · ⚪ 2)"
    assert sentiment_bias(events[2:3]).glyph == "🔴"
    assert sentiment_bias([]).glyph == "⚪"


def test_urovne_posledni_minuty_pred_zavrenim_a_chybejici_data() -> None:
    until = dt.datetime(2026, 9, 18, 21, 0, tzinfo=UTC)
    rows: list[dict[str, Any]] = [
        {
            "ts_min": until - dt.timedelta(minutes=5),
            "call_wall": 7700.0,
            "put_wall": 7650.0,
            "flip": None,
            "centroid": 7690.0,
        },
        {
            "ts_min": until - dt.timedelta(minutes=3),
            "call_wall": 7730.0,
            "put_wall": 7680.0,
            "flip": float("nan"),
            "centroid": 7715.0,
        },
        # Engine píše minuty i o víkendu — prázdné úrovně a čas po zavření se přeskočí
        {
            "ts_min": until - dt.timedelta(minutes=1),
            "call_wall": None,
            "put_wall": None,
            "flip": None,
            "centroid": None,
        },
        {
            "ts_min": until + dt.timedelta(hours=3),
            "call_wall": 1.0,
            "put_wall": 1.0,
            "flip": 1.0,
            "centroid": 1.0,
        },
    ]
    found = last_levels(rows, until, dt.date(2026, 9, 21))
    assert found is not None and found.ts == until - dt.timedelta(minutes=3)
    assert found.flip is None and found.call_wall == 7730.0
    assert levels_line("NQ", found, 29987.75) == (
        "Úrovně NQ z poslední seance (expirace 21. 9.): call zeď 7730 · put zeď 7680 · "
        "těžiště 7715 · close 29987.75"
    )
    assert levels_line("NQ", None, 29987.75) == "Úrovně NQ z poslední seance chybí · close 29987.75"
    assert last_levels(rows[2:3], until, dt.date(2026, 9, 21)) is None


def test_text_se_vejde_do_telegramu_ubranim_odrazek() -> None:
    events = [
        ev(
            i,
            OPENING - dt.timedelta(hours=i),
            title=f"Zpráva {i} " + "Y" * 300,
            importance=3,
            category="GEOPOLITICS",
            direction=-1,
        )
        for i in range(1, 13)
    ]
    message = str(main(events)["message"])
    assert len(message) <= MESSAGE_BUDGET
    bullets = message.count("\n• ")
    assert 0 < bullets < 5
    assert message.endswith(f"další zásadní: {12 - bullets}")
    # Nejnovější zpráva první (blíž otevření)
    assert main(events)["event_ids"] == list(range(1, 13))


# ── Stav ───────────────────────────────────────────────────────────


def test_stav_pres_json_a_reset_pro_dalsi_otevreni() -> None:
    state = PreopenState(OPENING, done={STAGE_MAIN}, announced={"ES": {5, 1}, "NQ": set()})
    stored = state.to_json()
    assert stored == {
        "opening": "2026-09-20T22:00:00+00:00",
        "done": ["main"],
        "announced": {"ES": [1, 5], "NQ": []},
    }
    again = PreopenState.from_json(stored, OPENING)
    assert again.done == {STAGE_MAIN} and again.announced == {"ES": {1, 5}, "NQ": set()}
    next_week = OPENING + dt.timedelta(days=7)
    assert PreopenState.from_json(stored, next_week) == PreopenState(next_week)
    assert PreopenState.from_json(None, OPENING) == PreopenState(OPENING)
    assert PreopenState.from_json("nesmysl", OPENING) == PreopenState(OPENING)


def test_tataz_story_z_vice_zdroju_jednou_i_v_aktualizaci() -> None:
    korea = "North Korea fires two ballistic missiles off east coast in three hours"
    first = ev(
        7,
        dt.datetime(2026, 9, 20, 1, 0, tzinfo=UTC),
        title=f"{korea} - reuters.com",
        importance=3,
        category="GEOPOLITICS",
        direction=-1,
    )
    again = ev(
        8,
        dt.datetime(2026, 9, 20, 1, 5, tzinfo=UTC),
        title=f"{korea} - Reuters",
        importance=3,
        category="GEOPOLITICS",
        direction=-1,
    )
    other = ev(
        9,
        dt.datetime(2026, 9, 20, 2, 0, tzinfo=UTC),
        title="North Korea fires missile off east coast, Yonhap says",
        importance=3,
        category="GEOPOLITICS",
        direction=-1,
    )
    assert distinct_stories([again, other, first]) == [first, other]
    payload = main([*weekend_events(), first, again, other])
    message = str(payload["message"])
    assert message.count("two ballistic missiles") == 1
    assert "od pátku 22:55: 4 · sklon 🔴 (🟢 0 · 🔴 3 · ⚪ 1)" in message
    assert payload["event_ids"] == [5, 9, 7, 1]
    # Pozdní opakování ohlášené story není pro aktualizaci nová zpráva
    late = ev(
        10,
        OPENING - dt.timedelta(minutes=40),
        title=f"{korea} - Reuters",
        importance=3,
        category="GEOPOLITICS",
        direction=-1,
    )
    assert update([*weekend_events(), first, late], {1, 5, 7}) is None
    # Kopie zapsaná až po souhrnu, ale s dřívějším ts_event (opožděný feed) —
    # ohlášená story má přednost, kopie není nová
    backdated = ev(
        13,
        first.ts_event - dt.timedelta(minutes=30),
        title=f"{korea} - Reuters",
        importance=3,
        category="GEOPOLITICS",
        direction=-1,
    )
    assert distinct_stories([backdated, first], prefer={7}) == [first]
    assert update([*weekend_events(), first, backdated], {1, 5, 7}) is None
    # Kalendář se neslučuje: Core CPI a CPI jsou dvě události
    cpi = [
        ev(11, OPENING, title="USD CPI m/m", kind="scheduled", impact="High"),
        ev(12, OPENING, title="USD Core CPI m/m", kind="scheduled", impact="High"),
    ]
    assert len(distinct_stories(cpi)) == 2
