"""Push na Telegram (#1175, #1284): přepínače per druh, dědění, tiché hodiny, dedup, strop."""

import datetime as dt
import logging
import re
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from gexlens_api.live import LiveHub
from gexlens_api.push_telegram import (
    KIND_TOPIC,
    PUSH_MASTER_KEY,
    PUSH_SETTING_KEYS,
    PUSH_TOPICS,
    PushOptions,
    PushTopic,
    TelegramPush,
    _index_kinds,
    effective,
    format_message,
    in_quiet_hours,
    parse_quiet_hours,
)

PRAGUE = ZoneInfo("Europe/Prague")
REPO = Path(__file__).resolve().parents[2]

#: Druhy, které dnes chodí do kanálu alerts (inventura #1284: engine, news-engine,
#: API AlertEngine, paper/scénáře v API, docker-cleanup.ps1). Nový alert = doplnit
#: sem i do PUSH_TOPICS, jinak na Telegram neodejde.
PUBLISHED_KINDS = frozenset(
    {
        "setup",
        "risk_brake",
        "paper",
        "scenario_created",
        "scenario_result",
        "t6_candidate",
        "level_proximity",
        "vol_concentration",
        "news_anomaly",
        "expiry_calendar",
        "drift",
        "setup_degraded",
        "setup_recovered",
        "disconnect",
        "connection_stall",
        "competing_session",
        "subscription_error",
        "degraded_start",
        "instrument_error",
        "spot_fallback",
        "chain_fallback",
        "feed_backup_dead",
        "feed_crosscheck",
        "feed_probe",
        "feed_silent",
        "greeks_suspect",
        "greeks_stalled",
        "greeks_recovered",
        "greeks_bs_fallback",
        "strikes_stalled",
        "strikes_recovered",
        "bars_stalled",
        "bars_recovered",
        "oi_missing",
        "oi_refresh_failed",
        "band_capped",
        "fa_validation",
        "fa_calibration",
        "disk_space",
        "disk_limit",
        "disk_low",
        "scenario_disk",
    }
)
#: Druhy jen pro skripty mimo Docker (OpsAlert.ps1) — do API se nepublikují
SCRIPT_KINDS = frozenset({"vhdx_compact", "deploy_rollback"})


def _legacy_category(kind: str) -> str:
    """Dřívější `category_of` (#1175) bez mrtvého `broker` — kotva pro „nic se nepřevrátí"."""
    if kind in ("setup", "scenario_result", "scenario_created", "risk_brake", "paper"):
        return "setup"
    ops = {
        "disconnect",
        "disk_limit",
        "disk_space",
        "connection_stall",
        "degraded_start",
        "competing_session",
        "feed_backup_dead",
        "instrument_error",
        "subscription_error",
        "greeks_stalled",
        "greeks_suspect",
        "strikes_stalled",
        "oi_refresh_failed",
        "oi_missing",
        "spot_fallback",
        "chain_fallback",
        "setup_degraded",
        "scenario_disk",
    }
    if kind in ops:
        return "ops"
    if kind in ("news_anomaly", "vol_concentration", "expiry_calendar"):
        return "news"
    return "info"


def _at(hour: int, minute: int = 0, day: int = 15) -> float:
    return dt.datetime(2026, 9, day, hour, minute, tzinfo=PRAGUE).timestamp()


class FakePost:
    def __init__(self, status: int = 200) -> None:
        self.calls: list[dict[str, Any]] = []
        self._status = status

    def __call__(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append({"url": url, **kwargs})
        return httpx.Response(self._status, json={"ok": self._status == 200})


def _push(
    post: FakePost,
    *,
    clock: float = _at(12),
    stored: dict[str, Any] | None = None,
    **options: Any,
) -> TelegramPush:
    opts = PushOptions(token="t", chat_id="42", **options)
    state = {"now": clock}
    push = TelegramPush(
        opts,
        lambda: dict(stored or {}),
        post=post,
        clock=lambda: state["now"],
        run_in_thread=False,
    )
    push.set_clock = lambda value: state.__setitem__("now", value)  # type: ignore[attr-defined]
    return push


# ── Tabulka PUSH_TOPICS ────────────────────────────────────────────


def test_kazdy_druh_prave_jeden_prepinac() -> None:
    assert set(KIND_TOPIC) == PUBLISHED_KINDS | SCRIPT_KINDS
    assert sum(len(topic.kinds) for topic in PUSH_TOPICS) == len(KIND_TOPIC)  # žádný dvakrát
    # broker je druh zprávy v kanálu news, retro_pass jde také kanálem news
    assert "broker" not in KIND_TOPIC and "retro_pass" not in KIND_TOPIC
    assert len(PUSH_TOPICS) == 28
    assert all(len(topic.setting) <= 64 for topic in PUSH_TOPICS)
    assert {topic.key for topic in PUSH_TOPICS if not topic.bell} == {"maintenance"}


def test_duplicitni_druh_shodi_import() -> None:
    twice = (
        PushTopic("a", "app", "ops", ("disconnect",), "A", "a"),
        PushTopic("b", "app", "ops", ("disconnect",), "B", "b"),
    )
    with pytest.raises(ValueError, match="disconnect"):
        _index_kinds(twice)
    with pytest.raises(ValueError, match="neznámá skupina"):
        _index_kinds((PushTopic("c", "jinde", "ops", ("x",), "C", "c"),))


def test_prepinac_nemicha_kategorie_a_nic_se_neprevraci() -> None:
    """Kategorie přepínače = dřívější kategorie každého jeho druhu → výchozí hodnota,
    dědění i tiché hodiny zůstávají pro každý druh přesně jako před #1284."""
    for kind in PUBLISHED_KINDS:
        assert KIND_TOPIC[kind].category == _legacy_category(kind), kind
    assert KIND_TOPIC["vhdx_compact"].category == "ops"  # kompaktace dřív chodila vždy


def test_setting_keys_jen_master_a_topic() -> None:
    assert PUSH_MASTER_KEY in PUSH_SETTING_KEYS
    assert all(
        key == PUSH_MASTER_KEY or key.startswith("push_telegram_topic_")
        for key in PUSH_SETTING_KEYS
    )
    assert "push_telegram_news" not in PUSH_SETTING_KEYS
    assert len(PUSH_SETTING_KEYS) == 29


# ── Dědění a efektivní stav ────────────────────────────────────────


def test_vychozi_stav_bez_ulozenych_klicu() -> None:
    master, topics = effective({})
    assert master is True
    for topic in PUSH_TOPICS:
        assert topics[topic.key] is (topic.category != "info"), topic.key


def test_dedeni_z_kategorii() -> None:
    news = {t.key for t in PUSH_TOPICS if t.category == "news"}
    info = {t.key for t in PUSH_TOPICS if t.category == "info"}
    setup = {t.key for t in PUSH_TOPICS if t.category == "setup"}
    assert news == {"news_anomaly", "vol_concentration", "expiry_calendar"}
    assert len(info) == 10  # 4 v „Setupy a burza", 6 v „Chování aplikace"
    assert setup == {"setup", "risk_brake", "paper", "scenario"}

    _, topics = effective({"push_telegram_news": False})
    assert {k for k, on in topics.items() if not on} == news | info

    _, topics = effective({"push_telegram_info": True})
    assert all(topics.values())

    _, topics = effective({"push_telegram_setup": False})
    assert {k for k, on in topics.items() if not on} == setup | info

    # Vlastní klíč má přednost před zděděnou kategorií
    _, topics = effective(
        {"push_telegram_news": False, "push_telegram_topic_expiry_calendar": True}
    )
    assert topics["expiry_calendar"] is True
    assert topics["news_anomaly"] is False and topics["vol_concentration"] is False

    # Hodnota, která není bool, se přeskočí (vlastní → kategorie → výchozí)
    _, topics = effective(
        {"push_telegram_topic_disk": "ne", "push_telegram_ops": 0, PUSH_MASTER_KEY: "x"}
    )
    assert topics["disk"] is True
    assert effective({PUSH_MASTER_KEY: "x"})[0] is True


def test_hlavni_vypinac() -> None:
    stored = {PUSH_MASTER_KEY: False, "push_telegram_topic_disk": False}
    push = _push(FakePost(), stored=stored)
    assert push.decide({"kind": "disconnect", "message": "w"}) == "Telegram vypnut"
    assert push.decide({"kind": "setup", "symbol": "ES", "message": "x"}) == "Telegram vypnut"
    status = push.status()
    assert status["enabled"] is False
    # Hodnoty přepínačů se vypnutím hlavního nemění
    topics = {t["key"]: t["enabled"] for g in status["groups"] for t in g["topics"]}
    assert topics["disk"] is False and topics["ibkr_connection"] is True


def test_neznamy_druh_neodejde_a_zaloguje(caplog: pytest.LogCaptureFixture) -> None:
    post = FakePost()
    push = _push(post)
    with caplog.at_level(logging.WARNING, logger="gexlens_api.push_telegram"):
        push.handle({"kind": "novy_alert", "message": "a"})
        push.handle({"kind": "novy_alert", "message": "b"})
        push.handle({"kind": "broker", "message": "c"})
    assert post.calls == []
    assert push.decide({"kind": "novy_alert", "message": "x"}) == "neznámý druh"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2  # jednou za druh
    assert "novy_alert" in warnings[0] and "broker" in warnings[1]


def test_cteni_nastaveni_selze_bere_vychozi() -> None:
    def broken() -> dict[str, Any]:
        raise RuntimeError("DB down")

    push = TelegramPush(
        PushOptions(token="t", chat_id="42"),
        broken,
        post=FakePost(),
        clock=lambda: _at(12),
        run_in_thread=False,
    )
    assert push.decide({"kind": "disconnect", "message": "x"}) is None
    assert push.decide({"kind": "fa_validation", "message": "x"}) == "diagnostics vypnuto"
    assert push.status()["enabled"] is True


# ── Rozhodování, formát, odeslání ──────────────────────────────────


def test_format_beze_zmeny() -> None:
    text = format_message({"kind": "setup", "symbol": "ES", "message": "Nový setup LONG"})
    assert text == "🎯 GEXLens · ES\nNový setup LONG"
    assert format_message({"kind": "disk_limit", "symbol": "*", "message": "Disk"}).startswith(
        "⚠️ GEXLens\nDisk"
    )
    assert format_message({"kind": "disconnect", "message": "x"}).startswith("⚠️")
    assert format_message({"kind": "setup_degraded", "message": "x"}).startswith("⚠️")
    assert format_message({"kind": "news_anomaly", "message": "x"}).startswith("📰")
    assert format_message({"kind": "fa_validation", "message": "x"}).startswith("ℹ️")
    assert format_message({"kind": "neznamy", "message": "x"}).startswith("ℹ️")


def test_tiche_hodiny_pres_pulnoc() -> None:
    window = parse_quiet_hours("23:00-06:00")
    assert in_quiet_hours(dt.time(23, 30), window)
    assert in_quiet_hours(dt.time(2, 0), window)
    assert not in_quiet_hours(dt.time(6, 0), window)
    assert not in_quiet_hours(dt.time(12, 0), window)
    assert parse_quiet_hours("") is None and parse_quiet_hours("x") is None


def test_setup_odejde_jednou_a_uzavreni_ne() -> None:
    post = FakePost()
    push = _push(post)
    payload = {"kind": "setup", "event": "created", "symbol": "ES", "message": "LONG 7580"}
    push.handle(payload)
    push.handle(payload)  # duplicita do 10 min
    push.handle({"kind": "setup", "event": "closed", "symbol": "ES", "message": "uzavřen"})
    assert len(post.calls) == 1
    call = post.calls[0]
    assert call["url"].endswith("/bott/sendMessage")
    assert call["json"]["chat_id"] == "42"
    assert call["json"]["text"] == "🎯 GEXLens · ES\nLONG 7580"
    assert push.status()["sent_today"] == 1


def test_prah_confidence_a_prepinace() -> None:
    post = FakePost()
    push = _push(post, setup_min_confidence=0.5, stored={"push_telegram_news": False})
    assert push.decide({"kind": "setup", "symbol": "ES", "message": "x", "confidence": 0.4})
    assert push.decide({"kind": "setup", "symbol": "ES", "message": "x", "confidence": 0.6}) is None
    # Stínový setup (#1185) nejde ven; brzda účtu má vlastní přepínač
    assert push.decide(
        {"kind": "setup", "symbol": "ES", "message": "y", "confidence": 0.9, "tradeable": False}
    )
    assert push.decide({"kind": "risk_brake", "symbol": "ES", "message": "brzda"}) is None
    # Zděděné vypnutí kategorie news
    assert push.decide({"kind": "news_anomaly", "symbol": "ES", "message": "y"}) == (
        "news_anomaly vypnuto"
    )
    assert push.decide({"kind": "expiry_calendar", "message": "roll"}) == "expiry_calendar vypnuto"
    # info je ve výchozím stavu vypnuto, ops zapnuto
    assert push.decide({"kind": "fa_validation", "message": "z"}) == "diagnostics vypnuto"
    assert push.decide({"kind": "disconnect", "message": "w"}) is None


def test_vlastni_prepinac_vypne_jen_svuj_druh() -> None:
    push = _push(FakePost(), stored={"push_telegram_topic_competing_session": False})
    assert push.decide({"kind": "competing_session", "message": "a"}) == (
        "competing_session vypnuto"
    )
    assert push.decide({"kind": "subscription_error", "message": "b"}) is None
    assert push.decide({"kind": "connection_stall", "message": "c"}) is None


def test_tiche_hodiny_pousti_jen_provozni() -> None:
    push = _push(FakePost(), clock=_at(23, 30), stored={"push_telegram_info": True})
    assert push.decide({"kind": "setup", "symbol": "NQ", "message": "s"}) == "tiché hodiny"
    assert push.decide({"kind": "connection_stall", "symbol": "NQ", "message": "o"}) is None
    # setup_degraded byl provozní (ops) — tichými hodinami projde dál
    assert push.decide({"kind": "setup_degraded", "symbol": "NQ", "message": "d"}) is None
    # disk_low zůstává v info — i zapnutý podléhá tichým hodinám
    assert push.decide({"kind": "disk_low", "symbol": "*", "message": "l"}) == "tiché hodiny"


def test_denni_strop_se_resetuje_o_pulnoci() -> None:
    push = _push(FakePost(), daily_cap=1)
    assert push.decide({"kind": "setup", "symbol": "ES", "message": "a"}) is None
    assert push.decide({"kind": "setup", "symbol": "ES", "message": "b"}) == "denní strop"
    push.set_clock(_at(9, day=16))  # type: ignore[attr-defined]
    assert push.decide({"kind": "setup", "symbol": "ES", "message": "c"}) is None


def test_bez_udaju_nic_neposila_a_4xx_neopakuje(monkeypatch: pytest.MonkeyPatch) -> None:
    post = FakePost()
    empty = TelegramPush(PushOptions(), lambda: {}, post=post, run_in_thread=False)
    empty.handle({"kind": "disconnect", "message": "x"})
    assert post.calls == [] and empty.status()["configured"] is False

    monkeypatch.setattr("gexlens_api.push_telegram.time.sleep", lambda _s: None)
    bad = FakePost(status=401)
    push = _push(bad)
    push.handle({"kind": "disconnect", "message": "x"})
    assert len(bad.calls) == 1  # 4xx = konfigurace, žádné opakování
    assert push.status()["last_error"] == "HTTP 401"


def test_livehub_vola_posluchace_jen_pro_alerts() -> None:
    hub = LiveHub()
    seen: list[dict[str, Any]] = []
    hub.alert_listeners.append(seen.append)
    hub.alert_listeners.append(lambda _p: (_ for _ in ()).throw(RuntimeError("boom")))
    hub.publish("alerts", {"kind": "setup"})
    hub.publish("status", {"engine": "online"})
    assert seen == [{"kind": "setup"}]


# ── /push/status a skripty ─────────────────────────────────────────


def test_status_tvar() -> None:
    status = _push(FakePost(), stored={"push_telegram_topic_level_proximity": True}).status()
    # Bez tokenu a chat id — OpsAlert i UI dostanou jen stav a přepínače
    assert set(status) == {
        "configured",
        "quiet_hours",
        "daily_cap",
        "sent_today",
        "last_sent_at",
        "last_error",
        "enabled",
        "master",
        "groups",
    }
    assert status["enabled"] is True
    assert status["master"]["setting"] == PUSH_MASTER_KEY
    assert status["master"]["help"][1] == ""
    groups = status["groups"]
    assert [g["label"] for g in groups] == ["Setupy a burza", "Chování aplikace"]
    keys = [t["key"] for g in groups for t in g["topics"]]
    assert keys == [t.key for t in PUSH_TOPICS]  # pořadí z tabulky
    assert [t["key"] for t in groups[0]["topics"]][:2] == ["setup", "risk_brake"]
    for group in groups:
        for topic in group["topics"]:
            assert topic["setting"] == f"push_telegram_topic_{topic['key']}"
            help_lines = topic["help"]
            assert help_lines[0] and help_lines[1] == ""
            assert all(line.startswith("• ") for line in help_lines[2:])
            assert help_lines[-1].startswith("• Výchozí: ")
    by_key = {t["key"]: t for g in groups for t in g["topics"]}
    assert by_key["level_proximity"]["enabled"] is True  # uložená hodnota
    assert by_key["drift"]["enabled"] is False  # výchozí info
    assert "• Chodí i v tichých hodinách" in by_key["ibkr_connection"]["help"]
    assert (
        "• Ve zvonku: disconnect, connection_stall, degraded_start"
        in (by_key["ibkr_connection"]["help"])
    )
    assert not any(line.startswith("• Ve zvonku") for line in by_key["maintenance"]["help"])


def test_skripty_odkazuji_existujici_topic() -> None:
    """OpsAlert.ps1 volá `Send-OpsAlert … -Topic '<key>'` — překlep by tiše poslal
    Telegram mimo přepínač (fail-open). Pester v repu není, tak aspoň regex."""
    pattern = re.compile(r"-Topic\s+'(\w+)'")
    found = {
        match
        for path in (REPO / "scripts").rglob("*.ps1")
        for match in pattern.findall(path.read_text(encoding="utf-8"))
    }
    assert found, "žádný skript nepoužívá -Topic — pojistka by nic nekontrolovala"
    assert found <= {topic.key for topic in PUSH_TOPICS}
