"""Sonda: proč dxFeed neposílá data pro ES podklad (#845, kroky 1–2).

Engine má `/ESU26:XCME` zaregistrovaný, ale nechodí ani Quote, ani
TimeAndSale — zatímco NQ na identické konvenci (`/NQU26:XCME`) jede. Tahle
sonda zkusí několik zápisů symbolu ve VLASTNÍ subskripci, mimo produkční
stream, takže se nesahá na běžící sběr.

Nově se čtou i ERROR zprávy (#845 krok 3): tiché odmítnutí subskripce bylo
dřív nerozeznatelné od „symbol mlčí".

Spuštění (engine kontejner):  python probe_es_underlying.py
"""

import asyncio
import datetime as dt
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "src"))

from gexlens_engine.config import load_settings  # noqa: E402
from gexlens_engine.tasty.session import TastyCredentials, TastySession  # noqa: E402
from gexlens_engine.tasty.stream import DxLinkStream  # noqa: E402
from gexlens_engine.tasty.symbols import SymbolMap  # noqa: E402

#: Kandidátní zápisy — konvence, kterou používá engine, plus varianty
CANDIDATES = [
    "/ESU26:XCME",  # co engine používá dnes
    "/ESU6:XCME",  # jednociferný rok (zápis z veřejných nástrojů)
    "/ES:XCME",  # kontinuální kontrakt
    "/NQU26:XCME",  # kontrola: tenhle prokazatelně funguje
]

LISTEN_S = 45


async def main() -> None:
    settings = load_settings()
    session = TastySession(
        TastyCredentials(
            client_secret=settings.tasty_client_secret,
            refresh_token=settings.tasty_refresh_token,
        )
    )
    seen: Counter[str] = Counter()
    errors: list[str] = []

    def on_event(event_type: str, values: list[object]) -> None:
        symbol = str(values[0]) if values else "?"
        seen[f"{symbol} {event_type}"] += 1

    stream = DxLinkStream(session.quote_token, on_event)

    # Co dnes hlásí SymbolMap jako front future — ať sonda měří totéž
    symbol_map = SymbolMap(session)
    for product in ("ES", "NQ"):
        front = await symbol_map.front_future(product)
        print(f"SymbolMap front future {product}: {front!r}")

    stop = asyncio.Event()
    task = asyncio.create_task(stream.run(stop))
    await asyncio.sleep(5)
    await stream.set_symbols(set(CANDIDATES))
    print(f"\nSubskribováno {len(CANDIDATES)} kandidátů, poslouchám {LISTEN_S} s…")
    await asyncio.sleep(LISTEN_S)
    stop.set()
    await asyncio.wait_for(task, timeout=10)

    print(f"\n=== Události per symbol ({dt.datetime.now(dt.UTC):%H:%M:%S} UTC) ===")
    for candidate in CANDIDATES:
        rows = {k: v for k, v in seen.items() if k.startswith(candidate)}
        total = sum(rows.values())
        verdict = "MLČÍ" if total == 0 else json.dumps(rows, ensure_ascii=False)
        print(f"  {candidate:<16} {verdict}")
    print(f"\nERROR zpráv ze serveru: {stream.errors}")
    if stream.last_error:
        print(f"  poslední: {stream.last_error}")
    errors.extend(e for e in [stream.last_error] if e)


if __name__ == "__main__":
    asyncio.run(main())
