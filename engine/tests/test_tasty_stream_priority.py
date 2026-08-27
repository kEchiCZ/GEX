"""Dávkování a priorita subskripce (#845)."""

import asyncio

from gexlens_engine.tasty.stream import SUBSCRIPTION_BATCH, DxLinkStream


def make_stream() -> tuple[DxLinkStream, list[dict[str, object]]]:
    sent: list[dict[str, object]] = []

    async def token() -> tuple[str, str]:
        return "wss://test", "token"

    stream = DxLinkStream(token, lambda _t, _v: None, events=("Quote",))

    async def fake_send(payload: dict[str, object]) -> None:
        sent.append(payload)

    stream._send = fake_send  # type: ignore[method-assign]
    return stream, sent


async def test_podklady_jdou_v_prvni_davce() -> None:
    """Při rate limitu server část dávek odmítne — podklad nesmí být obětí."""
    stream, sent = make_stream()
    stream.set_priority(frozenset({"/ESU26:XCME", "/NQU26:XCME"}))
    # Tolik opčních symbolů, že podklady by při abecedním řazení spadly dozadu
    options = {f".ES{strike}C" for strike in range(SUBSCRIPTION_BATCH * 2)}

    await stream._send_subscription(add=options | {"/ESU26:XCME", "/NQU26:XCME"})

    add_entries = sent[0]["add"]
    assert isinstance(add_entries, list)
    first_batch = [entry["symbol"] for entry in add_entries]
    assert "/ESU26:XCME" in first_batch
    assert "/NQU26:XCME" in first_batch
    assert len(sent) > 1  # opravdu se dávkovalo


async def test_mezi_davkami_je_rozestup() -> None:
    """Bez rozestupu server hlásí `Your subscription rate is too high`."""
    stream, sent = make_stream()
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    original = asyncio.sleep
    asyncio.sleep = fake_sleep  # type: ignore[assignment]
    try:
        await stream._send_subscription(add={f".X{i}" for i in range(SUBSCRIPTION_BATCH * 3)})
    finally:
        asyncio.sleep = original

    # Od #863 se mezi dávky umí vklínit KEEPALIVE — počítají se jen subskripce
    subscriptions = [p for p in sent if p.get("type") == "FEED_SUBSCRIPTION"]
    assert len(subscriptions) == 3
    assert len(slept) == 3 and all(s > 0 for s in slept)
