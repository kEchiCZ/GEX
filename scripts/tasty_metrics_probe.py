"""Sonda #871 fáze 1: nese tasty /market-metrics IV rank pro futures?

Jeden GET, žádná subskripce. Zkouší zápisy symbolu, které tasty používá
jinde (/ES, ES) + SPY jako kontrolu, že endpoint sám funguje.

Spuštění (host):  python scripts/tasty_metrics_probe.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "src"))

from gexlens_engine.config import load_settings  # noqa: E402
from gexlens_engine.tasty.session import TastyCredentials, TastySession  # noqa: E402

CANDIDATES = ["/ES", "ES", "/NQ", "SPY"]

INTERESTING = (
    "implied-volatility-index",
    "implied-volatility-index-rank",
    "implied-volatility-percentile",
    "implied-volatility-updated-at",
)


async def main() -> None:
    settings = load_settings()
    session = TastySession(
        TastyCredentials(
            client_secret=settings.tasty_client_secret,
            refresh_token=settings.tasty_refresh_token,
        )
    )
    try:
        joined = ",".join(CANDIDATES).replace("/", "%2F")
        payload = await session.get_json(f"/market-metrics?symbols={joined}")
        items = payload.get("data", {}).get("items", [])
        print(f"items: {len(items)}")
        for item in items:
            symbol = item.get("symbol")
            values = {key: item.get(key) for key in INTERESTING}
            print(f"  {symbol}: {values}")
    finally:
        await session.close()


if __name__ == "__main__":
    asyncio.run(main())
