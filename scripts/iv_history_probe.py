"""Sonda #871 fáze 0: dává IBKR historickou implied volatilitu pro ES/NQ?

`reqHistoricalData` s `whatToShow=OPTION_IMPLIED_VOLATILITY` u akcií/indexů
vrací denní 30d IV index roky zpět. Pro futures je podpora nedoložená —
tahle sonda to změří: jeden request per varianta kontraktu, žádná market
data linka (historická data mají vlastní pacing), vlastní clientId mimo
produkční engine.

Spuštění (host, TWS na 7496):  python scripts/iv_history_probe.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "src"))

from ib_async import IB, Contract, Future, Index  # noqa: E402

HOST = "127.0.0.1"
PORT = 7496
#: Mimo rozsah enginu (ten používá nízká ID) — souběh nesmí kolidovat
CLIENT_ID = 917

#: Varianty, u kterých má smysl IV index zkoušet: front future, kontinuální
#: futures kontrakt a indexový podklad (SPX/NDX jako referenční kontrola,
#: že request sám o sobě funguje a případný fail je vlastnost futures).
CANDIDATES: list[tuple[str, Contract]] = [
    ("ES front future", Future("ES", "20260918", "CME")),
    ("NQ front future", Future("NQ", "20260918", "CME")),
    ("SPX index (kontrola)", Index("SPX", "CBOE")),
]


async def probe(ib: IB, label: str, contract: Contract) -> None:
    try:
        qualified = await ib.qualifyContractsAsync(contract)
        if not qualified:
            print(f"{label}: kontrakt se nepodařilo kvalifikovat")
            return
        bars = await ib.reqHistoricalDataAsync(
            qualified[0],
            endDateTime="",
            durationStr="1 Y",
            barSizeSetting="1 day",
            whatToShow="OPTION_IMPLIED_VOLATILITY",
            useRTH=True,
            formatDate=1,
        )
    except Exception as exc:  # noqa: BLE001 — sonda referuje, nepadá
        print(f"{label}: CHYBA {type(exc).__name__}: {exc}")
        return
    if not bars:
        print(f"{label}: 0 barů — IV historie NENÍ")
        return
    first, last = bars[0], bars[-1]
    print(
        f"{label}: {len(bars)} denních barů, {first.date} → {last.date}, "
        f"poslední IV close {last.close:.4f}"
    )


async def main() -> None:
    ib = IB()
    await ib.connectAsync(HOST, PORT, clientId=CLIENT_ID, timeout=15)
    try:
        for label, contract in CANDIDATES:
            await probe(ib, label, contract)
            await asyncio.sleep(2)  # pacing historických requestů
    finally:
        ib.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
