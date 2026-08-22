"""Sonda #206 faze 0: equity/index opcni retezce a entitlementy pres tastytrade.

Odpovida na jedinou skutecnou neznamou z analyzy #206: dava tasty non-pro
dxFeed feed i OPRA (equity opce SPY/KO) a indexni opce (SPX)? Kroky:

1. GET /option-chains/{symbol}/nested - existuje retezec, kolik expiraci/striku,
   jaky format maji streamer symboly.
2. DXLink subskripce vzorku (ATM oblast nejblizsi expirace + podklad) - prijme
   server subskripci bez ERROR? Prijdou eventy?

POZOR na cteni vysledku o vikendu: equity opce kotuji jen RTH, takze nulovy
tok eventu v nedeli NENI dukaz chybejicich prav - dukazem je az pondelni RTH.
Zamitnuti subskripce / DXLink ERROR je naopak prukazne hned.

Spusteni (engine kontejner, cte tajemstvi z prostredi):
    python equity_options_probe.py --symbols SPY,KO,SPX --listen 25
"""

import argparse
import asyncio
import contextlib
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "src"))

from gexlens_engine.tasty.session import TastyCredentials, TastySession  # noqa: E402
from gexlens_engine.tasty.stream import DxLinkStream  # noqa: E402

SAMPLE_STRIKES = 6  # kolik striku kolem ATM na stranu vzorkovat


async def probe_chain(session: TastySession, symbol: str) -> list[str]:
    """Vrati vzorek streamer symbolu nejblizsi expirace; [] = retezec neni."""
    try:
        payload = await session.get_json(f"/option-chains/{symbol}/nested")
    except Exception as error:  # noqa: BLE001 - sonda meri, nepada
        print(f"  {symbol}: chain endpoint selhal: {type(error).__name__}: {error}")
        return []
    # Equity/index nested ma data.items (futures maji option-chains)
    data = payload.get("data", {})
    chains = data.get("items") or data.get("option-chains", [])
    expirations = []
    for group in chains:
        expirations.extend(group.get("expirations", []))
    if not expirations:
        print(f"  {symbol}: retezec prazdny")
        return []
    total_strikes = sum(len(e.get("strikes", [])) for e in expirations)
    nearest = min(expirations, key=lambda e: str(e.get("expiration-date", "")))
    strikes = nearest.get("strikes", [])
    middle = len(strikes) // 2
    sample = []
    for strike in strikes[max(0, middle - SAMPLE_STRIKES) : middle + SAMPLE_STRIKES]:
        for key in ("call-streamer-symbol", "put-streamer-symbol"):
            value = strike.get(key)
            if value:
                sample.append(str(value))
    print(
        f"  {symbol}: {len(expirations)} expiraci, {total_strikes} striku; "
        f"nejblizsi {nearest.get('expiration-date')} ({len(strikes)} striku); "
        f"priklad streameru: {sample[0] if sample else '-'}"
    )
    return sample


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="SPY,KO,SPX")
    parser.add_argument("--listen", type=int, default=25, help="Sekund posluchu eventu")
    args = parser.parse_args()

    secret = os.environ.get("GEXLENS_TASTY_CLIENT_SECRET", "")
    token = os.environ.get("GEXLENS_TASTY_REFRESH_TOKEN", "")
    if not secret or not token:
        print("Chybi tasty tajemstvi v prostredi", file=sys.stderr)
        return 1
    session = TastySession(TastyCredentials(client_secret=secret, refresh_token=token))

    print("=== 1) Retezce pres /option-chains/{symbol}/nested ===")
    samples: dict[str, list[str]] = {}
    for symbol in args.symbols.split(","):
        symbol = symbol.strip()
        samples[symbol] = await probe_chain(session, symbol)

    subscribe = {s for sample in samples.values() for s in sample}
    subscribe |= {s for s in samples if samples[s]}  # podklad (SPY/KO/SPX quote)
    if not subscribe:
        print("Zadne symboly k subskripci - konec")
        return 1

    print(f"\n=== 2) DXLink subskripce vzorku ({len(subscribe)} symbolu) ===")
    events: Counter[str] = Counter()
    per_underlying: Counter[str] = Counter()

    def on_event(event_type: str, values: list[object]) -> None:
        events[event_type] += 1
        symbol = str(values[0]) if values else ""
        root = symbol.lstrip(".").rstrip("0123456789CP")  # hrube prirazeni
        per_underlying[root[:4]] += 1

    stream = DxLinkStream(session.quote_token, on_event)
    stop = asyncio.Event()
    task = asyncio.create_task(stream.run(stop))
    await asyncio.sleep(3)
    await stream.set_symbols(subscribe)
    await asyncio.sleep(args.listen)
    stop.set()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(task, timeout=10)

    print(f"  reconnecty/ERROR cykly: {stream.reconnects}")
    if events:
        for event_type, count in sorted(events.items()):
            print(f"  {event_type}: {count} eventu")
        print(f"  rozpad (hrube): {dict(per_underlying)}")
    else:
        print(
            "  zadne eventy - o vikendu OCEKAVANE (RTH trh zavreny); "
            "subskripce ale prosla bez ERROR, viz reconnecty vyse"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
