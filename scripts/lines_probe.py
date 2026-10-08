"""Sonda market data lines (#631, #1477): kolik souběžných subskripcí účet reálně obslouží.

Nad stropem IBKR Error 101 neposílá, linky jen tiše nedodávají data (ADR-0001 bod 4).
Měří se proto **doručení**: kontrakt se počítá, když mu po požadavku dorazil aspoň
jeden tick (`Ticker.time` po čase požadavku). ib_async drží `Ticker` per conId napříč
subskripcemi i s hodnotami z minula (#1088), takže samotná hodnota nestačí. Data
se žádají jako zmrazená (`reqMarketDataType(2)`): při otevřeném trhu chodí živá,
při zavřeném poslední známá, takže sonda měří i o víkendu.

Režimy:
- výchozí (#631): FOP ES po dávkách až do `MAX_LINES` a kolik z nich dodává;
- `--news` (#1477): měření A/B/A. Kapacita FOP bez NEWS pásek (A), s páskami (B)
  a znovu bez nich (A2). Pásky jsou tytéž a odebírané stejným kódem jako v enginu
  (`broad_tape_providers`, `subscribe_broad_tape`). Rozdíl A − B říká, kolik lines
  pásky berou. A2 ≠ A znamená, že kapacitu mezitím bral někdo jiný (mobil, TWS),
  a měření se opakuje.
- `--dry-run`: jen kvalifikace kontraktů a seznam pásek, žádné `reqMktData`.
  Linky nebere, jde pustit i za běhu enginu.

POZOR: lines jsou SDÍLENÉ per uživatel. Mimo `--dry-run` spouštět VÝHRADNĚ se
STOPNUTÝM produkčním enginem při zavřeném trhu, jinak sonda trhá produkci; mobil
ani TWS během měření nepoužívat. Runner scripts/lines-probe-offhours.cmd spouští
výchozí režim. Pro `--news` ručně:
    docker stop gex-engine-1
    PYTHONUTF8=1 uv run python scripts/lines_probe.py --news
    docker start gex-engine-1   # pak ověřit čerstvost: /status last_tick_ts

Spuštění (host): uv run python scripts/lines_probe.py [--news] [--dry-run]
Prostředí: GEXLENS_IBKR_HOST (127.0.0.1), GEXLENS_IBKR_PORT (4001 = IB Gateway).
Výstup: scripts/lines_probe_result.txt + stdout.
"""

import argparse
import asyncio
import datetime as dt
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ib_async import IB, Contract, Future, Ticker

from gexlens_engine.ibkr.newsticks import (
    broad_tape_providers,
    subscribe_broad_tape,
    tape_symbol,
)

#: Mimo rozsah enginu — souběh ID nesmí kolidovat (engine používá nízká ID)
CLIENT_ID = 631
BATCH = 10
MAX_LINES = 130  # nad provozní strop 100, ať je zlom vidět celý
SETTLE_S = 2.0  # čekání po dávce
FINAL_SETTLE_S = 10.0  # doběh posledních dávek před počítáním
RELEASE_S = 5.0  # po zrušení subskripcí, než IBKR linky uvolní
FROZEN = 2  # reqMarketDataType: živá data, při zavřeném trhu poslední známá
RESULT_PATH = Path(__file__).with_name("lines_probe_result.txt")


@dataclass(frozen=True)
class Fill:
    label: str
    subscribed: int
    delivering: int


class _ReqIdRecorder:
    """`ib.client` pro `subscribe_broad_tape`, který si pamatuje reqId kvůli zrušení."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.req_ids: list[int] = []

    def getReqId(self) -> int:  # noqa: N802 — jméno z protokolu ib_async
        req_id = int(self._client.getReqId())
        self.req_ids.append(req_id)
        return req_id

    def reqMktData(self, *args: Any) -> None:  # noqa: N802
        self._client.reqMktData(*args)


def _delivered(ticker: Ticker, since: dt.datetime) -> bool:
    return ticker.time is not None and ticker.time > since


async def front_future(ib: IB) -> Contract:
    """Nejbližší neexpirovaný kvartální ES (stejný řetěz jako tradingClass ES)."""
    details = await ib.reqContractDetailsAsync(Future("ES", exchange="CME"))
    today = dt.datetime.now(dt.UTC).strftime("%Y%m%d")
    futures = sorted(
        (
            d.contract
            for d in details
            if d.contract and d.contract.lastTradeDateOrContractMonth >= today
        ),
        key=lambda c: c.lastTradeDateOrContractMonth,
    )
    if not futures:
        raise RuntimeError("Žádný neexpirovaný ES future — sonda končí")
    return futures[0]


async def option_contracts(ib: IB, front: Contract, limit: int) -> tuple[float, list[Contract]]:
    """`limit` FOP nejblíž spotu (call i put) z řetězu tradingClass ES předního kvartálu.

    Spot z historického baru, ne z `reqMktData`: historická data linku neberou.
    """
    bars = await ib.reqHistoricalDataAsync(
        front, "", "2 D", "1 hour", "TRADES", useRTH=False, formatDate=2
    )
    if not bars:
        raise RuntimeError("Historické bary ES nepřišly — spot neznámý, sonda končí")
    spot = float(bars[-1].close)
    chains = await ib.reqSecDefOptParamsAsync("ES", "CME", "FUT", front.conId)
    chain = next((c for c in chains if c.tradingClass == "ES"), None)
    if chain is None:
        raise RuntimeError("Řetěz tradingClass ES nenalezen — sonda končí")
    expiry = front.lastTradeDateOrContractMonth
    if expiry not in chain.expirations:
        expiry = min(
            e for e in chain.expirations if e >= dt.datetime.now(dt.UTC).strftime("%Y%m%d")
        )
    strikes = sorted(chain.strikes, key=lambda s: abs(s - spot))[: limit // 2 + 1]
    specs = [
        Contract(
            secType="FOP",
            symbol="ES",
            lastTradeDateOrContractMonth=expiry,
            strike=float(strike),
            right=right,
            exchange="CME",
            tradingClass="ES",
            multiplier="50",
        )
        for strike in strikes
        for right in ("C", "P")
    ][:limit]
    # ib_async vrací u nejednoznačného kontraktu seznam kandidátů, u neznámého None
    qualified = [
        c for c in await ib.qualifyContractsAsync(*specs) if isinstance(c, Contract) and c.conId
    ]
    return spot, qualified


async def fill(ib: IB, contracts: Sequence[Contract], label: str, log: list[str]) -> Fill:
    """Subskribuje kontrakty po dávkách, spočítá doručení a vše zase zruší."""
    since = dt.datetime.now(dt.UTC)
    tickers: list[Ticker] = []
    for offset in range(0, len(contracts), BATCH):
        for contract in contracts[offset : offset + BATCH]:
            tickers.append(ib.reqMktData(contract, "", False, False))
        await asyncio.sleep(SETTLE_S)
        delivering = sum(_delivered(t, since) for t in tickers)
        log.append(f"  {label}: subskribováno {len(tickers):3d} -> dodává {delivering:3d}")
        print(log[-1], flush=True)
    await asyncio.sleep(FINAL_SETTLE_S)
    result = Fill(label, len(tickers), sum(_delivered(t, since) for t in tickers))
    for contract in contracts:
        ib.cancelMktData(contract)
    await asyncio.sleep(RELEASE_S)
    log.append(f"{label}: FINÁLNĚ subskribováno {result.subscribed} -> dodává {result.delivering}")
    print(log[-1], flush=True)
    return result


def verdict(a: Fill, b: Fill, a2: Fill, tapes: int) -> str:
    if a.delivering != a2.delivering:
        return (
            f"NEROZHODNUTO: kapacita bez pásek se mezi A ({a.delivering}) a A2 ({a2.delivering}) "
            "změnila — linky bral někdo jiný, měření zopakovat"
        )
    if a.delivering >= a.subscribed:
        return (
            f"NEROZHODNUTO: strop nedosažen ({a.delivering}/{a.subscribed} dodává)"
            " — zvýšit MAX_LINES"
        )
    used = a.delivering - b.delivering
    return (
        f"NEWS pásky ({tapes}) berou {used} market data lines "
        f"(A {a.delivering}, B {b.delivering}, A2 {a2.delivering})"
    )


async def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sonda market data lines (#631, #1477)")
    parser.add_argument("--news", action="store_true", help="měření A/B/A s NEWS páskami (#1477)")
    parser.add_argument("--dry-run", action="store_true", help="jen kvalifikace, žádné reqMktData")
    args = parser.parse_args(argv)
    host = os.environ.get("GEXLENS_IBKR_HOST", "127.0.0.1")
    port = int(os.environ.get("GEXLENS_IBKR_PORT", "4001"))

    log: list[str] = [f"Sonda lines spuštěna {dt.datetime.now(dt.UTC).isoformat()} ({host}:{port})"]
    ib = IB()
    errors: list[str] = []

    def on_error(req_id: int, code: int, message: str, *_: object) -> None:
        # 101 = max tickers, 354 = not subscribed, 10197 = konkurenční relace (mobil)
        if code in (101, 200, 300, 321, 354, 10089, 10190, 10197):
            errors.append(f"{code} reqId {req_id}")

    ib.errorEvent += on_error
    await ib.connectAsync(host, port, clientId=CLIENT_ID, timeout=15)
    try:
        front = await front_future(ib)
        spot, contracts = await option_contracts(ib, front, MAX_LINES)
        log.append(
            f"ES {front.lastTradeDateOrContractMonth}, spot ~{spot:.0f}, "
            f"kvalifikováno {len(contracts)} FOP"
        )
        codes = [p.code for p in await ib.reqNewsProvidersAsync()]
        providers = broad_tape_providers(codes)
        log.append(f"News kódy účtu: {', '.join(codes)} -> pásky enginu: {', '.join(providers)}")
        print("\n".join(log), flush=True)
        if args.dry_run:
            return 0

        ib.reqMarketDataType(FROZEN)
        if not args.news:
            await fill(ib, contracts, "FOP", log)
        else:
            a = await fill(ib, contracts, "A bez pásek", log)
            recorder = _ReqIdRecorder(ib.client)

            def make_contract(provider: str) -> Contract:
                return Contract(secType="NEWS", exchange=provider, symbol=tape_symbol(provider))

            subscribe_broad_tape(recorder, providers, make_contract=make_contract)
            await asyncio.sleep(SETTLE_S)
            log.append(f"Pásky odebrány: {', '.join(providers)} (reqId {recorder.req_ids})")
            b = await fill(ib, contracts, "B s páskami", log)
            for req_id in recorder.req_ids:
                ib.client.cancelMktData(req_id)  # type: ignore[no-untyped-call]
            await asyncio.sleep(RELEASE_S)
            a2 = await fill(ib, contracts, "A2 bez pásek", log)
            log.append("VERDIKT: " + verdict(a, b, a2, len(providers)))
            print(log[-1], flush=True)
        if errors:
            log.append(f"Chyby IBKR ({len(errors)}, posledních 20): " + "; ".join(errors[-20:]))
        return 0
    finally:
        ib.disconnect()
        RESULT_PATH.write_text("\n".join(log) + "\n", encoding="utf-8")
        print(f"Výsledek: {RESULT_PATH}", flush=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
