"""Sonda #631: přeměřit strop market data lines s OVĚŘENÍM DORUČENÍ dat.

ADR-0001 bod 4 měřil jen nepřítomnost erroru; #631 chce doložit, kolik
souběžných subskripcí REÁLNĚ DODÁVÁ data (IBKR nad stropem umí mlčet bez
chyby). Postup: postupně přidávat reqMktData na ES FOP stricích po dávkách,
po každé dávce počkat a spočítat tickery, kterým dorazil aspoň jeden tick
(bid/ask/last/close; v pauze Globexu chodí zmrazené kotace — doručení doloží
i ty). Zlom v křivce „subskribováno vs. dodává" je skutečný strop.

POZOR: lines jsou SDÍLENÉ per uživatel — spouštět VÝHRADNĚ se STOPNUTÝM
produkčním enginem (Globex pauza 23:00–24:00), jinak sonda trhá produkci.
Runner: scripts/lines-probe-offhours.cmd (stop engine → sonda → start).

Spuštění (host, TWS na 7496):  python scripts/lines_probe.py
Výstup: scripts/lines_probe_result.txt + stdout.
"""

import asyncio
import datetime as dt
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "src"))

from ib_async import IB, Contract, Future  # noqa: E402

HOST = "127.0.0.1"
PORT = 7496
#: Mimo rozsah enginu — souběh ID nesmí kolidovat (engine používá nízká ID)
CLIENT_ID = 631

BATCH = 10
MAX_LINES = 130  # nad papírový strop 100, ať je zlom vidět celý
SETTLE_S = 4.0  # čekání na první ticky po dávce
RESULT_PATH = Path(__file__).with_name("lines_probe_result.txt")


def _has_data(ticker: object) -> bool:
    for name in ("bid", "ask", "last", "close"):
        value = getattr(ticker, name, None)
        if value is not None and not (isinstance(value, float) and math.isnan(value)) and value > 0:
            return True
    return False


async def main() -> None:
    lines: list[str] = [f"Sonda #631 spuštěna {dt.datetime.now(dt.UTC).isoformat()}"]
    errors: list[str] = []
    ib = IB()

    def on_error(reqId: int, code: int, message: str, *args: object) -> None:
        # 354 = not subscribed, 101 = max lines — přesně to, co dokumentujeme
        if code in (354, 101, 300, 10190, 10197):
            errors.append(f"error {code} reqId {reqId}")

    ib.errorEvent += on_error
    await ib.connectAsync(HOST, PORT, clientId=CLIENT_ID, timeout=15)
    try:
        # Řetěz ES: přední future → contract details FOP kolem spotu
        front = Future("ES", "20260918", "CME")
        [front] = await ib.qualifyContractsAsync(front)
        [spot_ticker] = [ib.reqMktData(front, "", False, False)]
        await asyncio.sleep(3)
        spot = spot_ticker.marketPrice()
        ib.cancelMktData(front)
        if spot is None or math.isnan(spot):
            raise RuntimeError("Spot ES se nepodařilo přečíst — sonda končí")
        lines.append(f"Spot ES ~{spot:.0f}")

        # Kontrakty: strike po 5 b oběma směry od ATM, střídavě C/P
        atm = round(spot / 5) * 5
        specs: list[Contract] = []
        for i in range(MAX_LINES):
            strike = atm + (i // 2 + 1) * 5 * (1 if i % 2 == 0 else -1)
            right = "C" if strike >= atm else "P"
            specs.append(
                Contract(
                    secType="FOP",
                    symbol="ES",
                    lastTradeDateOrContractMonth="20260918",
                    strike=float(strike),
                    right=right,
                    exchange="CME",
                    tradingClass="ES",  # měsíční/kvartální — nejlikvidnější řetěz
                    multiplier="50",
                )
            )
        qualified = [c for c in await ib.qualifyContractsAsync(*specs) if c is not None and c.conId]
        lines.append(f"Kvalifikováno {len(qualified)} kontraktů")

        tickers = []
        for offset in range(0, len(qualified), BATCH):
            batch = qualified[offset : offset + BATCH]
            for contract in batch:
                tickers.append(ib.reqMktData(contract, "", False, False))
            await asyncio.sleep(SETTLE_S)
            delivering = sum(1 for t in tickers if _has_data(t))
            subscribed = len(tickers)
            lines.append(
                f"subskribováno {subscribed:3d} → dodává {delivering:3d} (chyb {len(errors)})"
            )
            print(lines[-1])
        # Finální doběh: poslední dávky potřebují čas
        await asyncio.sleep(10)
        delivering = sum(1 for t in tickers if _has_data(t))
        lines.append(f"FINÁLNĚ: subskribováno {len(tickers)} → dodává {delivering}")
        if errors:
            lines.append("Chyby (posledních 20): " + "; ".join(errors[-20:]))
        for contract in qualified:
            ib.cancelMktData(contract)
    finally:
        ib.disconnect()
        RESULT_PATH.write_text("\n".join(lines), encoding="utf-8")
        print(f"Výsledek: {RESULT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
