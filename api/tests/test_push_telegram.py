"""Push na Telegram (#1175): kategorie, přepínače, tiché hodiny, dedup, strop, odeslání."""

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from gexlens_api.live import LiveHub
from gexlens_api.push_telegram import (
    PushOptions,
    TelegramPush,
    category_of,
    format_message,
    in_quiet_hours,
    parse_quiet_hours,
)

PRAGUE = ZoneInfo("Europe/Prague")


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


def test_kategorie_a_format() -> None:
    assert category_of("setup") == "setup"
    assert category_of("disconnect") == "ops"
    assert category_of("news_anomaly") == "news"
    assert category_of("fa_validation") == "info"
    text = format_message({"kind": "setup", "symbol": "ES", "message": "Nový setup LONG"})
    assert text == "🎯 GEXLens · ES\nNový setup LONG"
    assert format_message({"kind": "disk_limit", "symbol": "*", "message": "Disk"}).startswith(
        "⚠️ GEXLens\nDisk"
    )


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


def test_prah_confidence_a_prepinace_kategorii() -> None:
    post = FakePost()
    push = _push(post, setup_min_confidence=0.5, stored={"push_telegram_news": False})
    assert push.decide({"kind": "setup", "symbol": "ES", "message": "x", "confidence": 0.4})
    assert push.decide({"kind": "setup", "symbol": "ES", "message": "x", "confidence": 0.6}) is None
    # Stínový setup (#1185) nejde ven; brzda účtu patří do kategorie setup
    assert push.decide(
        {"kind": "setup", "symbol": "ES", "message": "y", "confidence": 0.9, "tradeable": False}
    )
    assert push.decide({"kind": "risk_brake", "symbol": "ES", "message": "brzda"}) is None
    assert category_of("expiry_calendar") == "news"
    assert push.decide({"kind": "news_anomaly", "symbol": "ES", "message": "y"}) == (
        "kategorie news vypnuta"
    )
    # info je default vypnuto, ops zapnuto
    assert push.decide({"kind": "fa_validation", "message": "z"}) == "kategorie info vypnuta"
    assert push.decide({"kind": "disconnect", "message": "w"}) is None


def test_tiche_hodiny_pousti_jen_provozni() -> None:
    push = _push(FakePost(), clock=_at(23, 30))
    assert push.decide({"kind": "setup", "symbol": "NQ", "message": "s"}) == "tiché hodiny"
    assert push.decide({"kind": "connection_stall", "symbol": "NQ", "message": "o"}) is None


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
