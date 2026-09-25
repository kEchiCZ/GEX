"""Shluky zpráv a rozhodnutí o upozornění (#1291, ADR-0043) — čisté funkce nad seznamy."""

import datetime as dt
from zoneinfo import ZoneInfo

from gexlens_news.clusters import (
    ANOMALY_KIND,
    MAX_LISTED,
    Cluster,
    ClusterEvent,
    build_alert,
    build_clusters,
    clean_title,
    evaluate_clusters,
    in_cooldown,
    is_significant,
)
from gexlens_news.reactions import Bar, Excursion, Thresholds

PRAGUE = ZoneInfo("Europe/Prague")
T0 = dt.datetime(2026, 9, 16, 12, 30, 3, tzinfo=dt.UTC)  # středa, trh otevřený
BASE_PRICE = 7000.0


def ev(
    event_id: int,
    at: dt.datetime,
    *,
    kind: str = "headline",
    importance: int | None = 1,
    category: str | None = "OTHER",
    impact: str | None = None,
    curated: bool = False,
    title: str | None = None,
) -> ClusterEvent:
    return ClusterEvent(
        id=event_id,
        ts_event=at,
        kind=kind,
        title=title or f"Zpráva {event_id}",
        importance=importance,
        category=category,
        ff_impact=impact,
        curated=curated,
    )


def calm_bars(
    start: dt.datetime,
    end: dt.datetime,
    *,
    jump_at: dt.datetime | None = None,
    jump_bp: float = 0.0,
) -> list[Bar]:
    """Klidné minutové bary (close střídá 7000 / 7000,5), volitelně skok od `jump_at`."""
    bars: list[Bar] = []
    ts = start
    while ts < end:
        close = BASE_PRICE + 0.5 * (int(ts.timestamp() // 60) % 2)
        if jump_at is not None and ts >= jump_at:
            close *= 1 + jump_bp / 10_000
        bars.append(
            Bar(ts=ts, open=close, high=close + 0.25, low=close - 0.25, close=close, volume=1)
        )
        ts += dt.timedelta(minutes=1)
    return bars


def flat_baseline(bp: float = 1.2, z: float = 0.7) -> dict[int, list[Excursion]]:
    """Každá minuta dne 20× stejná obvyklá výchylka → p97 = (bp, z)."""
    return {minute: [Excursion(bp=bp, direction=1, z=z)] * 20 for minute in range(1440)}


def two_significant_and_noise() -> list[ClusterEvent]:
    """AC1: 10 zpráv během 90 s — FF High a headline s importance 3, zbytek šum."""
    events = [
        ev(1, T0, kind="scheduled", importance=1, impact="High", title="USD CPI m/m"),
        ev(
            2,
            T0 + dt.timedelta(seconds=40),
            importance=3,
            category="MACRO_INFLATION",
            title="US consumer prices rise more than expected",
        ),
    ]
    events += [ev(10 + i, T0 + dt.timedelta(seconds=10 * i)) for i in range(8)]
    return events


def evaluate(
    events: list[ClusterEvent],
    symbol: str,
    bars: list[Bar],
    *,
    announced: list[dt.datetime] | None = None,
    now: dt.datetime | None = None,
) -> tuple[list[dict[str, object]], list[dt.datetime], int]:
    outcome = evaluate_clusters(
        build_clusters(events),
        symbol,
        bars,
        flat_baseline(),
        announced or [],
        now=now or T0 + dt.timedelta(minutes=8),
        not_before=T0 - dt.timedelta(hours=1),
        window=5,
        tz=PRAGUE,
    )
    return outcome.alerts, outcome.announced, outcome.unmeasurable


def market_bars(*, jump_bp: float) -> list[Bar]:
    minute = T0.replace(second=0)
    return calm_bars(
        minute - dt.timedelta(minutes=90),
        minute + dt.timedelta(minutes=10),
        jump_at=minute,
        jump_bp=jump_bp,
    )


# ── Významnost ─────────────────────────────────────────────────────


def test_vyznamnost_podle_zdroje() -> None:
    # FF podle impactu z payloadu, ne podle importance z klasifikátoru
    assert is_significant(ev(1, T0, kind="scheduled", importance=1, impact="High"))
    assert is_significant(ev(1, T0, kind="scheduled", importance=1, impact="Medium"))
    assert not is_significant(ev(1, T0, kind="scheduled", importance=3, impact="Low"))
    assert not is_significant(ev(1, T0, kind="scheduled", importance=3, impact="Holiday"))
    assert not is_significant(ev(1, T0, kind="scheduled", importance=3, impact=None))
    # Headline a broker: importance ≥ 2 mimo EARNINGS (varianta B)
    assert is_significant(ev(1, T0, importance=2))
    assert is_significant(ev(1, T0, kind="broker", importance=3, category="FED"))
    assert not is_significant(ev(1, T0, importance=1))
    assert not is_significant(ev(1, T0, importance=None))
    assert not is_significant(ev(1, T0, importance=3, category="EARNINGS"))
    # Sociální sítě jen kurátor s importance ≥ 2
    assert not is_significant(ev(1, T0, kind="social", importance=3))
    assert is_significant(ev(1, T0, kind="social", importance=2, curated=True))
    assert not is_significant(ev(1, T0, kind="social", importance=1, curated=True))


# ── Shlukování ─────────────────────────────────────────────────────


def test_shluk_kotvi_na_vyznamne_zprave_a_sum_jen_pocita() -> None:
    events = [
        ev(1, T0 - dt.timedelta(seconds=30)),  # šum před kotvou se nepočítá
        ev(2, T0, importance=2),
        ev(3, T0 + dt.timedelta(seconds=90), importance=3),
        ev(4, T0 + dt.timedelta(seconds=100)),
        ev(5, T0 + dt.timedelta(seconds=150), importance=2),  # nový shluk (> 2 min)
        ev(6, T0 + dt.timedelta(minutes=10)),  # šum sám shluk nezaloží
    ]
    clusters = build_clusters(events)
    assert [c.start for c in clusters] == [T0, T0 + dt.timedelta(seconds=150)]
    # Pořadí významnosti: importance 3 před 2
    assert [e.id for e in clusters[0].significant] == [3, 2]
    assert clusters[0].noise_count == 1
    assert [e.id for e in clusters[1].significant] == [5]


def test_shluk_jen_ze_sumu_nevznikne() -> None:
    """AC4 (rozhodnutí 25. 9.): pohyb bez významné zprávy se neohlašuje."""
    noise = [ev(i, T0 + dt.timedelta(seconds=5 * i)) for i in range(10)]
    assert build_clusters(noise) == []
    alerts, _, _ = evaluate(noise, "ES", market_bars(jump_bp=30))
    assert alerts == []


# ── Rozhodnutí per instrument (AC z #1291) ─────────────────────────


def test_ac1_dve_vyznamne_a_osm_sumu_daji_dve_upozorneni() -> None:
    events = two_significant_and_noise()
    alerts_es, _, _ = evaluate(events, "ES", market_bars(jump_bp=-20))
    alerts_nq, _, _ = evaluate(events, "NQ", market_bars(jump_bp=-35))
    assert len(alerts_es) == 1 and len(alerts_nq) == 1
    es, nq = alerts_es[0], alerts_nq[0]
    assert (es["kind"], es["symbol"]) == (ANOMALY_KIND, "ES")
    assert (nq["kind"], nq["symbol"]) == (ANOMALY_KIND, "NQ")
    for alert in (es, nq):
        message = str(alert["message"])
        assert "USD CPI m/m" in message
        assert "US consumer prices rise more than expected" in message
        assert "ostatní zprávy: 8" in message
        assert alert["event_ids"] == [1, 2]  # FF High před importance 3
    # Každé nese jen bp svého instrumentu (ES ≈ −20 bp, NQ ≈ −35 bp)
    assert "↓ -2" in str(es["message"]) and "↓ -3" not in str(es["message"])
    assert "↓ -3" in str(nq["message"]) and "↓ -2" not in str(nq["message"])


def test_ac2_mimoradny_pohyb_jen_na_nq() -> None:
    events = two_significant_and_noise()
    assert evaluate(events, "ES", market_bars(jump_bp=0))[0] == []
    assert len(evaluate(events, "NQ", market_bars(jump_bp=-35))[0]) == 1


def test_ac3_bezny_pohyb_nic() -> None:
    alerts, _, unmeasurable = evaluate(two_significant_and_noise(), "ES", market_bars(jump_bp=0))
    assert alerts == [] and unmeasurable == 0


def test_rezim_volatility_stejna_vychylka_po_rozjete_hodine_neprojde() -> None:
    """Výchylka ~21 bp je nad hranicí v bp, ale po hodině s výnosy ±40 bp ne v z."""
    minute = T0.replace(second=0)
    wild = []
    for k in range(90, 0, -1):
        close = BASE_PRICE * (1.004 if k % 2 and k > 1 else 1.0)
        ts = minute - dt.timedelta(minutes=k)
        wild.append(Bar(ts=ts, open=close, high=close, low=close, close=close, volume=1))
    window = calm_bars(minute, minute + dt.timedelta(minutes=5), jump_at=minute, jump_bp=20)
    assert evaluate(two_significant_and_noise(), "ES", wild + window)[0] == []
    calm = calm_bars(minute - dt.timedelta(minutes=90), minute) + window
    assert len(evaluate(two_significant_and_noise(), "ES", calm)[0]) == 1


def test_cooldown_per_instrument_a_symetricky() -> None:
    announced = [T0]
    assert in_cooldown(T0 + dt.timedelta(minutes=14), announced)
    assert in_cooldown(T0 - dt.timedelta(minutes=1), announced)  # pozdní kotva dřív
    assert not in_cooldown(T0 + dt.timedelta(minutes=15), announced)
    events = two_significant_and_noise()
    bars = market_bars(jump_bp=-20)
    assert evaluate(events, "ES", bars, announced=[T0 - dt.timedelta(minutes=5)])[0] == []
    # Druhý mimořádný shluk do 15 min v témže běhu se potlačí
    minute = T0.replace(second=0)
    second = minute + dt.timedelta(minutes=4)
    later = [*events, ev(50, second, importance=3)]
    later_bars = calm_bars(
        minute - dt.timedelta(minutes=90), second, jump_at=minute, jump_bp=-20
    ) + calm_bars(second, second + dt.timedelta(minutes=10), jump_at=second, jump_bp=-40)
    assert len(evaluate([later[-1]], "ES", later_bars, now=T0 + dt.timedelta(minutes=12))[0])
    alerts, fired, _ = evaluate(later, "ES", later_bars, now=T0 + dt.timedelta(minutes=12))
    assert len(alerts) == 1 and fired == [T0]


def test_shluk_pred_ready_starsi_nez_lookback_a_pred_startem_se_nehodnoti() -> None:
    events = two_significant_and_noise()
    bars = market_bars(jump_bp=-20)
    # Okno 12:30–12:35 + 2 min na zápis barů → hotovo 12:37
    assert evaluate(events, "ES", bars, now=T0.replace(second=0) + dt.timedelta(minutes=6))[0] == []
    assert len(evaluate(events, "ES", bars, now=T0.replace(second=0) + dt.timedelta(minutes=7))[0])
    assert evaluate(events, "ES", bars, now=T0 + dt.timedelta(minutes=31))[0] == []
    outcome = evaluate_clusters(
        build_clusters(events),
        "ES",
        bars,
        flat_baseline(),
        [],
        now=T0 + dt.timedelta(minutes=8),
        not_before=T0 + dt.timedelta(minutes=8),  # proces startoval až po dokončení okna
        window=5,
        tz=PRAGUE,
    )
    assert outcome.alerts == []


def test_nemeritelny_shluk_se_pocita_jen_pri_otevrenem_trhu() -> None:
    events = two_significant_and_noise()
    _, _, unmeasurable = evaluate(events, "ES", [])
    assert unmeasurable == 1
    saturday = dt.datetime(2026, 9, 19, 12, 0, tzinfo=dt.UTC)
    weekend = [ev(1, saturday, importance=3)]
    outcome = evaluate_clusters(
        build_clusters(weekend),
        "ES",
        [],
        flat_baseline(),
        [],
        now=saturday + dt.timedelta(minutes=8),
        not_before=saturday - dt.timedelta(hours=1),
        window=5,
        tz=PRAGUE,
    )
    assert outcome.alerts == [] and outcome.unmeasurable == 0


# ── Formát ─────────────────────────────────────────────────────────


def test_format_upozorneni_presny_text_a_payload() -> None:
    cluster = build_clusters(two_significant_and_noise())[0]
    now = T0 + dt.timedelta(minutes=8)
    alert = build_alert(
        cluster,
        "ES",
        Excursion(bp=19.4, direction=-1, z=9.0),
        Thresholds(bp=9.2, z=3.1, samples=1220),
        window=5,
        now=now,
        tz=PRAGUE,
    )
    assert alert["message"] == (
        "Reakce ES na zprávy z 14:30: ↓ -19 bp za 5 min "
        "(97 % výchylek v tuto denní dobu do 9 bp)\n"
        "• USD CPI m/m\n"
        "• US consumer prices rise more than expected\n"
        "ostatní zprávy: 8"
    )
    assert alert["ts"] == int(now.timestamp())
    assert alert["ts_event"] == "2026-09-16T12:30:03+00:00"
    assert alert["event_ids"] == [1, 2]


def test_orez_na_pet_zprav_a_delka_textu() -> None:
    significant = tuple(
        ev(i, T0 + dt.timedelta(seconds=i), importance=3, title="X" * 300) for i in range(12)
    )
    cluster = Cluster(start=T0, significant=significant, noise_count=40)
    alert = build_alert(
        cluster,
        "NQ",
        Excursion(bp=51.0, direction=1, z=12.0),
        Thresholds(bp=13.0, z=3.0, samples=1220),
        window=5,
        now=T0,
        tz=PRAGUE,
    )
    message = str(alert["message"])
    assert message.count("\n• ") == MAX_LISTED
    assert message.endswith("další významné: 7 · ostatní zprávy: 40")
    assert "↑ +51 bp" in message
    assert len(message) <= 900
    assert alert["event_ids"] == list(range(12))


def test_titulek_bez_html_entit_a_na_jeden_radek() -> None:
    assert clean_title("USA S&amp;P Global  PMI\nFor Sept") == "USA S&P Global PMI For Sept"
    assert clean_title("x" * 130) == "x" * 119 + "…"
