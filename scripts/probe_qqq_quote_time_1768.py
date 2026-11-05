"""Sonda #1768: nese DXLink u opcí QQQ čas kotace a den Summary?

Jednorázová sonda MIMO engine, jen čtení feedu přes dev grant (vzor `scripts/tasty_probe.py`,
ADR-0025). Nic nezapisuje. Otázky F1c (ranní snímek řetězu QQQ v 9:15 ET, epika #1738):

1. Nese event Quote `bidTime` / `askTime`, a je to čas kotace, ne čas příjmu? Při odběru DXLink
   pošle poslední známou kotaci. Po close (a před ranní seancí) je to kotace z close.
   Má-li vlastní čas, pozná se podle něj (čas < 7:30 ET dne seance).
2. Co nese Summary (`dayId`, `prevDayId`, `openInterest`) — ke kterému dni patří OI?

Spuštění z kořene repa (čte `.env`, nic z něj nevypisuje):
    uv run --with websockets,httpx python scripts/probe_qqq_quote_time_1768.py [sekundy] [striků]
Prostředí: GEXLENS_DEV_TASTY_CLIENT_SECRET + GEXLENS_DEV_TASTY_REFRESH_TOKEN (dev grant).
"""

import asyncio
import datetime as dt
import json
import sys
import time
from zoneinfo import ZoneInfo

import httpx

API = "https://api.tastytrade.com"
ET = ZoneInfo("America/New_York")
PRODUCT = "QQQ"

EVENT_FIELDS = {
    "Quote": [
        "eventSymbol",
        "eventTime",
        "bidTime",
        "askTime",
        "bidPrice",
        "askPrice",
        "bidSize",
        "askSize",
    ],
    "Summary": [
        "eventSymbol",
        "eventTime",
        "dayId",
        "prevDayId",
        "openInterest",
        "dayOpenPrice",
        "prevDayClosePrice",
    ],
}


def load_env() -> dict[str, str]:
    """Načte .env ručně — sonda nesmí importovat engine config ani nic vypsat."""
    values: dict[str, str] = {}
    with open(".env", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key] = value
    return values


def access_token(env: dict[str, str]) -> str:
    response = httpx.post(
        f"{API}/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": env["GEXLENS_DEV_TASTY_REFRESH_TOKEN"],
            "client_secret": env["GEXLENS_DEV_TASTY_CLIENT_SECRET"],
        },
        timeout=15,
    )
    response.raise_for_status()
    return str(response.json()["access_token"])


def et(ms: object) -> str | None:
    """Epoch ms → čas ET; 0 / None = pole nevyplněné."""
    if not isinstance(ms, (int, float)) or ms <= 0:
        return None
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).astimezone(ET).strftime("%m-%d %H:%M:%S")


def day_of(day_id: object) -> str | None:
    """dxFeed dayId = počet dní od 1970-01-01."""
    if not isinstance(day_id, (int, float)) or day_id <= 0:
        return None
    return (dt.date(1970, 1, 1) + dt.timedelta(days=int(day_id))).isoformat()


async def main(seconds: float, per_expiry: int) -> None:
    import websockets

    env = load_env()
    token = access_token(env)
    headers = {"Authorization": f"Bearer {token}"}
    quote = httpx.get(f"{API}/api-quote-tokens", headers=headers, timeout=15).json()["data"]
    chain = httpx.get(f"{API}/option-chains/{PRODUCT}/nested", headers=headers, timeout=30)
    chain.raise_for_status()
    expirations: list[tuple[str, list[dict]]] = []
    for group in chain.json()["data"].get("items", []):
        for expiration in group.get("expirations", []):
            expirations.append((str(expiration["expiration-date"]), expiration["strikes"]))
    expirations.sort()
    today = dt.datetime.now(ET).date().isoformat()
    front = [e for e in expirations if e[0] >= today][:2]

    ws = await websockets.connect(quote["dxlink-url"], max_size=2**24)

    async def send(payload: dict) -> None:
        await ws.send(json.dumps(payload))

    async def recv_until(msg_type: str, timeout: float = 10.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = json.loads(await asyncio.wait_for(ws.recv(), deadline - time.monotonic()))
            if message.get("type") == msg_type:
                return message
            if message.get("type") == "ERROR":
                raise RuntimeError(f"DXLink ERROR: {message}")
        raise TimeoutError(msg_type)

    await send(
        {
            "type": "SETUP",
            "channel": 0,
            "version": "0.1-gexlens-probe-1768",
            "keepaliveTimeout": 60,
            "acceptKeepaliveTimeout": 60,
        }
    )
    await recv_until("SETUP")
    state = await recv_until("AUTH_STATE")
    if state.get("state") == "UNAUTHORIZED":
        await send({"type": "AUTH", "channel": 0, "token": quote["token"]})
        state = await recv_until("AUTH_STATE")
    assert state.get("state") == "AUTHORIZED", state.get("state")
    await send(
        {
            "type": "CHANNEL_REQUEST",
            "channel": 1,
            "service": "FEED",
            "parameters": {"contract": "AUTO"},
        }
    )
    await recv_until("CHANNEL_OPENED")
    await send(
        {
            "type": "FEED_SETUP",
            "channel": 1,
            "acceptAggregationPeriod": 0,
            "acceptDataFormat": "COMPACT",
            "acceptEventFields": EVENT_FIELDS,
        }
    )
    config = await recv_until("FEED_CONFIG")
    print(json.dumps({"feed_config": config.get("eventFields")}, ensure_ascii=False))

    # Spot podkladu → striky nejblíž spotu v obou expiracích + jeden vzdálený kontrakt
    await send(
        {
            "type": "FEED_SUBSCRIPTION",
            "channel": 1,
            "reset": True,
            "add": [{"type": "Quote", "symbol": PRODUCT}],
        }
    )
    spot = None
    deadline = time.monotonic() + 15
    while spot is None and time.monotonic() < deadline:
        message = json.loads(await asyncio.wait_for(ws.recv(), 15))
        if message.get("type") != "FEED_DATA":
            continue
        data = message["data"]
        for i in range(0, len(data), 2):
            if data[i] == "Quote":
                values = data[i + 1]
                for j in range(0, len(values), len(EVENT_FIELDS["Quote"])):
                    record = dict(zip(EVENT_FIELDS["Quote"], values[j : j + 8], strict=False))
                    if record["eventSymbol"] == PRODUCT and record["bidPrice"]:
                        spot = (float(record["bidPrice"]) + float(record["askPrice"])) / 2
    if spot is None:
        raise RuntimeError("spot QQQ nedorazil")
    symbols: list[str] = []
    for _expiry, strikes in front:
        ordered = sorted(strikes, key=lambda s: abs(float(s["strike-price"]) - spot))
        picked = (
            ordered[:per_expiry]
            + [s for s in ordered if abs(float(s["strike-price"]) - spot) >= 0.028 * spot][:1]
        )
        for strike in picked:
            symbols += [strike["call-streamer-symbol"], strike["put-streamer-symbol"]]
    subscribed_at = dt.datetime.now(dt.UTC)
    await send(
        {
            "type": "FEED_SUBSCRIPTION",
            "channel": 1,
            "add": [{"type": e, "symbol": s} for s in symbols for e in ("Quote", "Summary")],
        }
    )

    first: dict[tuple[str, str], dict] = {}
    counts: dict[tuple[str, str], int] = {}
    changes: dict[str, int] = {}
    last_quote: dict[str, tuple] = {}
    end = time.monotonic() + seconds
    last_keepalive = time.monotonic()
    while time.monotonic() < end:
        if time.monotonic() - last_keepalive > 25:
            await send({"type": "KEEPALIVE", "channel": 0})
            last_keepalive = time.monotonic()
        try:
            raw = await asyncio.wait_for(ws.recv(), min(5.0, max(0.1, end - time.monotonic())))
        except TimeoutError:
            continue
        message = json.loads(raw)
        if message.get("type") != "FEED_DATA":
            continue
        received = dt.datetime.now(dt.UTC)
        data = message["data"]
        for i in range(0, len(data), 2):
            kind, values = data[i], data[i + 1]
            fields = EVENT_FIELDS.get(kind)
            if fields is None:
                continue
            for j in range(0, len(values), len(fields)):
                record = dict(zip(fields, values[j : j + len(fields)], strict=False))
                symbol = str(record["eventSymbol"])
                if symbol == PRODUCT:
                    continue
                key = (kind, symbol)
                counts[key] = counts.get(key, 0) + 1
                if key not in first:
                    record["_received"] = received.astimezone(ET).strftime("%m-%d %H:%M:%S")
                    first[key] = record
                if kind == "Quote":
                    pair = (record["bidPrice"], record["askPrice"])
                    if symbol in last_quote and last_quote[symbol] != pair:
                        changes[symbol] = changes.get(symbol, 0) + 1
                    last_quote[symbol] = pair
    await ws.close()

    print(
        json.dumps(
            {
                "now_et": dt.datetime.now(ET).isoformat(timespec="seconds"),
                "subscribed_et": subscribed_at.astimezone(ET).strftime("%m-%d %H:%M:%S"),
                "spot": round(spot, 2),
                "expiries": [e for e, _ in front],
                "window_s": seconds,
            },
            ensure_ascii=False,
        )
    )
    for symbol in symbols:
        q = first.get(("Quote", symbol), {})
        s = first.get(("Summary", symbol), {})
        print(
            json.dumps(
                {
                    "symbol": symbol,
                    "quote_received": q.get("_received"),
                    "quote_eventTime": et(q.get("eventTime")),
                    "bidTime": et(q.get("bidTime")),
                    "askTime": et(q.get("askTime")),
                    "bid_ask": [q.get("bidPrice"), q.get("askPrice")],
                    "quote_events": counts.get(("Quote", symbol), 0),
                    "bid_ask_changes": changes.get(symbol, 0),
                    "summary_received": s.get("_received"),
                    "summary_eventTime": et(s.get("eventTime")),
                    "dayId": day_of(s.get("dayId")),
                    "prevDayId": day_of(s.get("prevDayId")),
                    "oi": s.get("openInterest"),
                    "summary_events": counts.get(("Summary", symbol), 0),
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    window = float(sys.argv[1]) if len(sys.argv) > 1 else 45.0
    per_expiry = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    asyncio.run(main(window, per_expiry))
