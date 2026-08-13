"""Spike #612: ověření tastytrade/dxFeed feedu na ES FOP (fáze 0 epicu #610).

Jednorázová sonda MIMO engine (nic z ní se do enginu neimportuje). Výhradně
OAuth2 refresh flow (ADR-0025 — /sessions je zakázané). Nic nezapisuje do
produkčních dat; výstupy jdou na stdout/JSON pro ADR.

Spuštění:  uv run --with websockets,httpx python scripts/tasty_probe.py <krok>
Kroky: rest | events | limits | reconnect
Prostředí: GEXLENS_DEV_TASTY_CLIENT_SECRET + GEXLENS_DEV_TASTY_REFRESH_TOKEN
(dev grant; produkční se sondy netýká).
"""

import asyncio
import json
import os
import statistics
import sys
import time

import httpx

API = "https://api.tastytrade.com"


def load_env() -> dict[str, str]:
    """Načte .env ručně — sonda nesmí importovat engine config."""
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


def step_rest() -> None:
    """Fáze A: entitlementy, quote token, streamer symboly, non-pro status."""
    env = load_env()
    token = access_token(env)
    headers = {"Authorization": f"Bearer {token}"}
    out: dict[str, object] = {}

    quote = httpx.get(f"{API}/api-quote-tokens", headers=headers, timeout=15)
    quote.raise_for_status()
    quote_data = quote.json()["data"]
    out["quote_token"] = {
        "dxlink_url": quote_data.get("dxlink-url"),
        "level": quote_data.get("level"),
        "token_len": len(quote_data.get("token", "")),
    }

    customer = httpx.get(f"{API}/customers/me", headers=headers, timeout=15)
    if customer.status_code == 200:
        cd = customer.json()["data"]
        out["customer"] = {
            "is_professional": cd.get("is-professional"),
            "agreed_to_terms": cd.get("agreed-to-margining"),
        }
    else:
        out["customer"] = {"status": customer.status_code}

    chains = httpx.get(f"{API}/futures-option-chains/ES/nested", headers=headers, timeout=30)
    chains.raise_for_status()
    data = chains.json()["data"]
    futures = data.get("futures", [])
    out["futures"] = [f.get("symbol") for f in futures][:6]
    expirations = []
    for group in data.get("option-chains", []):
        for exp in group.get("expirations", []):
            strikes = exp.get("strikes", [])
            sample = strikes[len(strikes) // 2] if strikes else {}
            expirations.append(
                {
                    "expiration_date": exp.get("expiration-date"),
                    "days_to_expiration": exp.get("days-to-expiration"),
                    "expiration_type": exp.get("expiration-type"),
                    "underlying": exp.get("underlying-symbol"),
                    "strike_count": len(strikes),
                    "sample_call_streamer": sample.get("call-streamer-symbol"),
                    "sample_put_streamer": sample.get("put-streamer-symbol"),
                }
            )
    expirations.sort(key=lambda e: str(e["expiration_date"]))
    out["expirations_total"] = len(expirations)
    out["nearest_expirations"] = expirations[:6]
    total_strikes = sum(int(e["strike_count"]) for e in expirations)
    out["total_option_contracts_both_sides"] = total_strikes * 2
    print(json.dumps(out, indent=1, ensure_ascii=False))


async def dxlink_session(url: str, token: str):
    """Async kontext: navázaný DXLink kanál FEED (SETUP → AUTH → CHANNEL)."""
    import websockets

    ws = await websockets.connect(url, max_size=2**24)

    async def send(payload: dict) -> None:
        await ws.send(json.dumps(payload))

    async def recv_until(msg_type: str, timeout: float = 10.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = await asyncio.wait_for(ws.recv(), timeout=deadline - time.monotonic())
            message = json.loads(raw)
            if message.get("type") == msg_type:
                return message
            if message.get("type") == "ERROR":
                raise RuntimeError(f"DXLink ERROR: {message}")
        raise TimeoutError(msg_type)

    await send(
        {
            "type": "SETUP",
            "channel": 0,
            "version": "0.1-gexlens-probe",
            "keepaliveTimeout": 60,
            "acceptKeepaliveTimeout": 60,
        }
    )
    await recv_until("SETUP")
    state = await recv_until("AUTH_STATE")
    if state.get("state") == "UNAUTHORIZED":
        await send({"type": "AUTH", "channel": 0, "token": token})
        state = await recv_until("AUTH_STATE")
    assert state.get("state") == "AUTHORIZED", state
    await send(
        {
            "type": "CHANNEL_REQUEST",
            "channel": 1,
            "service": "FEED",
            "parameters": {"contract": "AUTO"},
        }
    )
    await recv_until("CHANNEL_OPENED")
    return ws, send, recv_until


EVENT_FIELDS = {
    "Trade": ["eventSymbol", "price", "size", "dayVolume"],
    "Quote": ["eventSymbol", "bidPrice", "askPrice", "bidSize", "askSize"],
    "Greeks": ["eventSymbol", "volatility", "delta", "gamma", "theta", "vega", "price"],
    "Summary": ["eventSymbol", "openInterest", "dayOpenPrice", "prevDayClosePrice"],
    "TimeAndSale": [
        "eventSymbol",
        "time",
        "price",
        "size",
        "aggressorSide",
        "spreadLeg",
        "extendedTradingHours",
        "type",
    ],
    "Candle": ["eventSymbol", "time", "open", "high", "low", "close", "volume"],
}


async def step_events(symbols: list[str], seconds: float = 90.0) -> None:
    """Fáze B: které eventy chodí, jaká pole nesou, kadence."""
    env = load_env()
    token = access_token(env)
    headers = {"Authorization": f"Bearer {token}"}
    quote = httpx.get(f"{API}/api-quote-tokens", headers=headers, timeout=15).json()["data"]
    ws, send, recv_until = await dxlink_session(quote["dxlink-url"], quote["token"])

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
    accepted = sorted((config.get("eventFields") or {}).keys())
    print(json.dumps({"feed_config_accepted_events": accepted}), file=sys.stderr)
    subscription = [
        {"type": event, "symbol": symbol}
        for symbol in symbols
        for event in ("Quote", "Greeks", "Summary", "TimeAndSale", "Trade")
    ]
    await send({"type": "FEED_SUBSCRIPTION", "channel": 1, "reset": True, "add": subscription})

    arrivals: dict[str, list[float]] = {}
    samples: dict[str, list] = {}
    deadline = time.monotonic() + seconds
    last_keepalive = time.monotonic()
    while time.monotonic() < deadline:
        # DXLink vyžaduje klientské KEEPALIVE v rámci keepaliveTimeout (60 s)
        if time.monotonic() - last_keepalive > 25:
            await send({"type": "KEEPALIVE", "channel": 0})
            last_keepalive = time.monotonic()
        try:
            raw = await asyncio.wait_for(
                ws.recv(), timeout=min(5.0, max(0.1, deadline - time.monotonic()))
            )
        except TimeoutError:
            continue
        except Exception as error:
            print(f"// spojeni ukonceno: {type(error).__name__}", file=sys.stderr)
            break
        message = json.loads(raw)
        if message.get("type") != "FEED_DATA":
            continue
        data = message.get("data", [])
        # COMPACT: ["Quote", [hodnoty...]] — páry typ/values
        for i in range(0, len(data), 2):
            event_type = data[i]
            values = data[i + 1]
            arrivals.setdefault(event_type, []).append(time.monotonic())
            if len(samples.setdefault(event_type, [])) < 3:
                samples[event_type].append(values[: len(EVENT_FIELDS.get(event_type, [])) * 2])

    report: dict[str, object] = {"symbols": symbols, "window_s": seconds}
    for event_type, times in arrivals.items():
        gaps = [b - a for a, b in zip(times, times[1:])]
        report[event_type] = {
            "messages": len(times),
            "gap_median_s": round(statistics.median(gaps), 3) if gaps else None,
            "gap_p95_s": round(sorted(gaps)[int(len(gaps) * 0.95)], 3) if len(gaps) >= 20 else None,
            "sample": samples.get(event_type, [])[:2],
        }
    for event_type in EVENT_FIELDS:
        if event_type not in arrivals and event_type != "Candle":
            report[event_type] = {"messages": 0}
    await ws.close()
    print(json.dumps(report, indent=1, ensure_ascii=False, default=str))


async def step_limits(max_expiries: int = 8, hold_s: float = 12.0) -> None:
    """Fáze C1: inkrementální subskripce Quote po expiracích až do degradace."""
    env = load_env()
    token = access_token(env)
    headers = {"Authorization": f"Bearer {token}"}
    chains = httpx.get(f"{API}/futures-option-chains/ES/nested", headers=headers, timeout=30)
    chains.raise_for_status()
    expirations = []
    for group in chains.json()["data"].get("option-chains", []):
        for exp in group.get("expirations", []):
            symbols = [s["call-streamer-symbol"] for s in exp.get("strikes", [])]
            symbols += [s["put-streamer-symbol"] for s in exp.get("strikes", [])]
            expirations.append((exp.get("expiration-date"), symbols))
    expirations.sort(key=lambda item: str(item[0]))

    quote = httpx.get(f"{API}/api-quote-tokens", headers=headers, timeout=15).json()["data"]
    ws, send, recv_until = await dxlink_session(quote["dxlink-url"], quote["token"])
    await send(
        {
            "type": "FEED_SETUP",
            "channel": 1,
            "acceptAggregationPeriod": 0,
            "acceptDataFormat": "COMPACT",
            "acceptEventFields": {"Quote": EVENT_FIELDS["Quote"]},
        }
    )
    await recv_until("FEED_CONFIG")

    total = 0
    report = []
    last_keepalive = time.monotonic()
    for date, symbols in expirations[:max_expiries]:
        add = [{"type": "Quote", "symbol": sym} for sym in symbols]
        # dávkovaně po 500 v jedné zprávě, ať JSON není obří
        for offset in range(0, len(add), 500):
            await send(
                {"type": "FEED_SUBSCRIPTION", "channel": 1, "add": add[offset : offset + 500]}
            )
        total += len(symbols)
        got: set[str] = set()
        messages = 0
        errors = []
        deadline = time.monotonic() + hold_s
        while time.monotonic() < deadline:
            if time.monotonic() - last_keepalive > 25:
                await send({"type": "KEEPALIVE", "channel": 0})
                last_keepalive = time.monotonic()
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except TimeoutError:
                continue
            except Exception as error:
                errors.append(type(error).__name__)
                break
            message = json.loads(raw)
            if message.get("type") == "FEED_DATA":
                data = message.get("data", [])
                for i in range(0, len(data), 2):
                    values = data[i + 1]
                    messages += 1
                    for j in range(0, len(values), 5):
                        got.add(values[j])
            elif message.get("type") == "ERROR":
                errors.append(str(message))
        row = {
            "po_expiraci": str(date),
            "symbolu_celkem": total,
            "quote_zprav_za_okno": messages,
            "unikatnich_symbolu_s_kotaci": len(got),
            "chyby": errors,
        }
        report.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        if errors:
            break
    await ws.close()
    print(json.dumps({"zaver": report[-1] if report else None}, ensure_ascii=False))


def main() -> int:
    step = sys.argv[1] if len(sys.argv) > 1 else "rest"
    if step == "rest":
        step_rest()
    elif step == "events":
        symbols = sys.argv[2].split(",") if len(sys.argv) > 2 else []
        seconds = float(sys.argv[3]) if len(sys.argv) > 3 else 90.0
        asyncio.run(step_events(symbols, seconds))
    elif step == "limits":
        count = int(sys.argv[2]) if len(sys.argv) > 2 else 8
        asyncio.run(step_limits(count))
    else:
        print(f"Neznámý krok: {step}")
        return 1
    return 0


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.exit(main())
