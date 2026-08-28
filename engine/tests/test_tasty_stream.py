"""Rate limit subskripcí a samoléčení DXLink streamu (#863)."""

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest

from gexlens_engine.tasty.stream import (
    KEEPALIVE_INTERVAL_S,
    RATE_LIMIT_HEAL_QUIET_S,
    SUBSCRIPTION_BATCH,
    SUBSCRIPTION_PAUSE_MAX_S,
    SUBSCRIPTION_PAUSE_S,
    DxLinkStream,
)


def make_stream() -> tuple[DxLinkStream, list[dict[str, object]]]:
    sent: list[dict[str, object]] = []

    async def token() -> tuple[str, str]:
        return "wss://test", "token"

    stream = DxLinkStream(token, lambda _t, _v: None, events=("Quote",))

    async def fake_send(payload: dict[str, object]) -> None:
        sent.append(payload)

    stream._send = fake_send  # type: ignore[method-assign]
    return stream, sent


def rate_limit_message() -> dict[str, object]:
    return {
        "type": "ERROR",
        "channel": 1,
        "error": "BAD_ACTION",
        "message": "Your subscription rate is too high",
    }  # noqa: E501 # prettier-ignore


def test_rate_limit_zdvojuje_rozestup_se_stropem() -> None:
    """Každé odmítnutí rozestup zdvojí, ať opakování konverguje; strop drží."""
    stream, _ = make_stream()
    assert stream._pause_s == SUBSCRIPTION_PAUSE_S
    for _ in range(10):
        stream._note_server_error(rate_limit_message())
    assert stream.rate_limited == 10
    assert stream._pause_s == SUBSCRIPTION_PAUSE_MAX_S
    # Jiná chyba počítadlo rate limitu nezvyšuje
    stream._note_server_error({"type": "ERROR", "message": "UNKNOWN_SYMBOL"})
    assert stream.rate_limited == 10
    assert stream.errors == 11


def test_heal_ceka_na_zklidneni() -> None:
    """Resubscribe se neposílá do běžícího limitu — až po klidu."""
    stream, _ = make_stream()
    assert not stream._heal_due(0.0)  # bez chyby není co léčit
    stream._note_server_error(rate_limit_message())
    ts = stream._rate_limit_ts
    assert ts is not None
    assert not stream._heal_due(ts + RATE_LIMIT_HEAL_QUIET_S - 1)
    assert stream._heal_due(ts + RATE_LIMIT_HEAL_QUIET_S + 1)


async def test_dlouhy_resubscribe_proplete_keepalive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zpomalený resubscribe nesmí prošvihnout keepalive — server by odpojil."""
    stream, sent = make_stream()
    clock = {"now": 0.0}

    async def fake_sleep(seconds: float) -> None:
        clock["now"] += seconds

    import gexlens_engine.tasty.stream as stream_module

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(stream_module, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    stream._pause_s = SUBSCRIPTION_PAUSE_MAX_S
    stream._last_keepalive = 0.0
    # Dost symbolů na tolik dávek, aby součet rozestupů přesáhl interval
    needed = int(KEEPALIVE_INTERVAL_S / SUBSCRIPTION_PAUSE_MAX_S) + 2
    symbols = {f".ES{index}C" for index in range(SUBSCRIPTION_BATCH * needed)}
    await stream._send_subscription(add=symbols)
    keepalives = [payload for payload in sent if payload.get("type") == "KEEPALIVE"]
    assert len(keepalives) >= 1


async def test_connect_ma_ping_timeout_prezivajici_subskripci(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#916: transportní ping_timeout musí přežít plnou subskripci.

    Default websockets (20 s) byl kratší než resubscribe na stropu rozestupu
    (~90 s) — klient spojení zabil dřív, než subskripce doběhla, a stream se
    točil ve smyčce reconnectů (12:29–? 27. 8., fallback #614 bez dat).
    """
    stream, _sent = make_stream()
    captured: dict[str, object] = {}

    class _FailConnect:
        def __init__(self, url: str, **kwargs: object) -> None:
            captured.update(kwargs)

        async def __aenter__(self) -> None:
            raise RuntimeError("konec testu — zajímají nás jen kwargs")

        async def __aexit__(self, *args: object) -> None:
            return None

    import websockets

    import gexlens_engine.tasty.stream as stream_module

    monkeypatch.setattr(websockets, "connect", _FailConnect)
    with pytest.raises(RuntimeError):
        await stream._connect_and_read(asyncio.Event())
    assert captured["ping_timeout"] == stream_module.PING_TIMEOUT_S
    # Timeout musí s rezervou pokrýt nejpomalejší plnou subskripci: všechny
    # symboly z produkce (#863: ~5 600 × 4 eventy / 500 na dávku) à strop 2 s
    worst_batches = (5_600 * 4) / SUBSCRIPTION_BATCH
    assert worst_batches * SUBSCRIPTION_PAUSE_MAX_S < stream_module.PING_TIMEOUT_S


def test_silent_symbols_v_cache() -> None:
    """#936: cílený heal potřebuje vědět, kdo mlčí — dle stáří posledního eventu."""
    import datetime as dt

    from gexlens_engine.tasty.provider import TastyChainCache

    now = dt.datetime(2026, 8, 28, 14, 0, tzinfo=dt.UTC)
    clock = {"now": now - dt.timedelta(seconds=900)}
    cache = TastyChainCache(clock=lambda: clock["now"])
    cache.on_event("Quote", [".ESU26C7700", 1.0, 1.2, 1.0, 1.0])  # event před 15 min
    clock["now"] = now
    cache.on_event("Quote", [".ESU26C7750", 2.0, 2.2, 1.0, 1.0])  # čerstvý event
    candidates = {".ESU26C7700", ".ESU26C7750", ".ESU26P7600"}
    # 7700 zastaralý, 7600 v cache vůbec není, 7750 čerstvý
    assert cache.silent_symbols(candidates, max_age_s=600) == {".ESU26C7700", ".ESU26P7600"}


def make_stream_with_heal(
    silent: set[str],
) -> tuple[DxLinkStream, list[dict[str, object]]]:
    sent: list[dict[str, object]] = []

    async def token() -> tuple[str, str]:
        return "wss://test", "token"

    stream = DxLinkStream(
        token, lambda _t, _v: None, events=("Quote",), heal_targets=lambda _full: silent
    )

    async def fake_send(payload: dict[str, object]) -> None:
        sent.append(payload)

    stream._send = fake_send  # type: ignore[method-assign]
    return stream, sent


async def test_cileny_heal_posila_jen_mlcici(monkeypatch: pytest.MonkeyPatch) -> None:
    """#936: heal po rate limitu resubscribuje jen mlčící menšinu, ne vše.

    Plný resubscribe (~39 dávek) za RTH trhal limit znovu — 79 healů za 2 h.
    """

    async def no_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    silent = {".ES1", ".ES2"}
    stream, sent = make_stream_with_heal(silent)
    stream._symbols = {f".ES{i}" for i in range(1, 11)}  # 10 symbolů, mlčí 2
    # Simulace heal větve (výřez z _connect_and_read)
    full = set(stream._symbols)
    targets = stream._heal_targets(full) & full if stream._heal_targets else full
    if len(targets) < len(full) / 2:
        await stream._send_subscription(add=targets)
    entries = [e for p in sent for e in cast(list[dict[str, str]], p.get("add", []))]
    assert {e["symbol"] for e in entries} == silent  # jen mlčící

    # Mlčící většina (výpadek) → plný resubscribe
    stream2, sent2 = make_stream_with_heal({f".ES{i}" for i in range(1, 8)})
    stream2._symbols = {f".ES{i}" for i in range(1, 11)}
    full2 = set(stream2._symbols)
    silent2 = stream2._heal_targets(full2) & full2 if stream2._heal_targets else full2
    targets2 = silent2 if len(silent2) < len(full2) / 2 else full2
    await stream2._send_subscription(add=targets2)
    entries2 = [e for p in sent2 for e in cast(list[dict[str, str]], p.get("add", []))]
    assert len({e["symbol"] for e in entries2}) == 10
