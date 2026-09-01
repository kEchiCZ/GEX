"""Sdílený DXLink handshake (#617).

Vytaženo ze `stream.py`, aby existovala **jediná** implementace přihlášení
k feedu. Živý stream (`DxLinkStream`) i jednorázový backfill svíček
(`CandleFetcher`) se připojují stejně; dvě kopie sekvence
SETUP → AUTH → CHANNEL_REQUEST → FEED_SETUP by se dřív nebo později rozešly —
stejný důvod, proč `compute/marketclock` žije v enginu a ne dvakrát.

Modul záměrně nedrží stav: dostane otevřený websocket a vrátí se, až je kanál
připravený na `FEED_SUBSCRIPTION`.
"""

import asyncio
import json
import time
from typing import Any, Protocol

#: Server odpojuje bez klientského KEEPALIVE po ~60 s (ADR-0027 past 2)
KEEPALIVE_INTERVAL_S = 25.0
PING_TIMEOUT_S = 120.0


class WebSocketLike(Protocol):
    """Jen to, co handshake potřebuje — ať jde v testech podstrčit dvojník."""

    async def send(self, payload: str) -> None: ...
    async def recv(self) -> Any: ...


async def send_json(ws: WebSocketLike, payload: dict[str, object]) -> None:
    await ws.send(json.dumps(payload))


async def recv_until(
    ws: WebSocketLike, message_type: str, timeout: float = 10.0
) -> dict[str, object]:
    """Čte, dokud nepřijde zpráva daného typu; `ERROR` vyhodí hned."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        raw = await asyncio.wait_for(ws.recv(), timeout=deadline - time.monotonic())
        message = json.loads(raw)
        if message.get("type") == message_type:
            return dict(message)
        if message.get("type") == "ERROR":
            raise RuntimeError(f"DXLink ERROR: {message}")
    raise TimeoutError(message_type)


async def handshake(
    ws: WebSocketLike,
    token: str,
    event_fields: dict[str, list[str]],
    *,
    channel: int = 1,
) -> None:
    """SETUP → AUTH → CHANNEL_REQUEST → FEED_SETUP; po návratu lze subskribovat."""
    await send_json(
        ws,
        {
            "type": "SETUP",
            "channel": 0,
            "version": "0.1-gexlens",
            "keepaliveTimeout": 60,
            "acceptKeepaliveTimeout": 60,
        },
    )
    await recv_until(ws, "SETUP")
    state = await recv_until(ws, "AUTH_STATE")
    if state.get("state") == "UNAUTHORIZED":
        await send_json(ws, {"type": "AUTH", "channel": 0, "token": token})
        state = await recv_until(ws, "AUTH_STATE")
    if state.get("state") != "AUTHORIZED":
        raise RuntimeError(f"DXLink autorizace selhala: {state}")
    await send_json(
        ws,
        {
            "type": "CHANNEL_REQUEST",
            "channel": channel,
            "service": "FEED",
            "parameters": {"contract": "AUTO"},
        },
    )
    await recv_until(ws, "CHANNEL_OPENED")
    await send_json(
        ws,
        {
            "type": "FEED_SETUP",
            "channel": channel,
            "acceptAggregationPeriod": 0,
            "acceptDataFormat": "COMPACT",
            "acceptEventFields": event_fields,
        },
    )
    await recv_until(ws, "FEED_CONFIG")
