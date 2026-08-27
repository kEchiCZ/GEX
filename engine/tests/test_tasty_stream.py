"""Rate limit subskripcí a samoléčení DXLink streamu (#863)."""

import asyncio
from types import SimpleNamespace

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
