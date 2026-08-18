"""Diagnostika #734: jake kody news provideru IBKR skutecne vraci.

Jednorazova sonda MIMO engine. Nic nezapisuje a bezi na vlastnim `clientId`,
takze produkcni engine na `clientId=1` nemusi nikam ustupovat.

Otazka, kterou zodpovida: `broad_tape_providers` (#546) urizne z kodu vse za
pomlckou, takze z `DJ-N` udela `DJ`. Pokud ale IBKR zadneho providera `DJ`
nevraci, ptame se broad tape na kod, ktery jsme si vymysleli - a `Error 200:
No security definition` je pak korektni odpoved, ne porucha.

Text je zamerne bez diakritiky: konzole na Windows jede v cp1250 a sonda ma
vypsat vysledek, ne spadnout na UnicodeEncodeError.

Spusteni:
    uv run python scripts/news_providers_probe.py            # jen seznam kodu
    uv run python scripts/news_providers_probe.py --tapes    # + test pasek
Prostredi: GEXLENS_IBKR_HOST, GEXLENS_IBKR_PORT (default 127.0.0.1:7496).
"""

import asyncio
import os
import sys

from ib_async import IB, Contract

from gexlens_engine.ibkr.newsticks import broad_tape_providers, tape_symbol

#: Vlastni clientId, at se sonda nepere s enginem o spojeni
PROBE_CLIENT_ID = 77
#: Jak dlouho se ceka na asynchronni Error 200 po subskripci
TAPE_SETTLE_S = 3.0


async def main() -> int:
    host = os.environ.get("GEXLENS_IBKR_HOST", "127.0.0.1")
    port = int(os.environ.get("GEXLENS_IBKR_PORT", "7496"))

    ib = IB()
    try:
        await ib.connectAsync(host, port, clientId=PROBE_CLIENT_ID, timeout=15)
    except Exception as exc:  # noqa: BLE001 - sonda hlasi duvod a konci
        print(f"Pripojeni k TWS {host}:{port} selhalo: {exc}", file=sys.stderr)
        return 1

    try:
        providers = await ib.reqNewsProvidersAsync()
    finally:
        ib.disconnect()

    codes = [p.code for p in providers]
    print(f"reqNewsProviders vratilo {len(codes)} kodu:\n")
    for provider in providers:
        print(f"  {provider.code:<12} {provider.name}")

    roots = broad_tape_providers(codes)
    print(f"\nbroad_tape_providers -> {len(roots)} korenu: {', '.join(roots)}\n")

    print("Koren        vratilo IBKR?    symbol pasky")
    for root in roots:
        vratilo = "ANO" if root in codes else "NE - ODVOZENY"
        print(f"  {root:<10} {vratilo:<16} {tape_symbol(root)}")

    odvozene = [root for root in roots if root not in codes]
    print()
    if odvozene:
        print(
            f"ZAVER: {len(odvozene)} korenu si vyrabime sami ({', '.join(odvozene)}) - "
            "broad tape se jich pta naslepo."
        )
    else:
        print("ZAVER: kazdy koren IBKR skutecne vratilo; normalizace nic nevymysli.")

    if "--tapes" in sys.argv:
        await probe_tapes(host, port, codes)
    return 0


async def probe_tapes(host: str, port: int, codes: list[str]) -> None:
    """Zkusi pasku kazdeho SKUTECNEHO kodu a zaznamena, jestli prijde Error 200.

    Subskripce se rusi hned po zmereni a jede se po jednom: strop uctu je
    tvrdych 100 market data lines (ADR-0001) a produkcni engine z nej uz vetsinu
    drzi, takze osm soubeznych subskripci navic je zbytecne riziko.
    """
    print("\n--- Test broad tape symbolu (jeden po druhem, hned se rusi) ---")
    ib = IB()
    await ib.connectAsync(host, port, clientId=PROBE_CLIENT_ID + 1, timeout=15)
    errors: dict[int, str] = {}

    def on_error(req_id: int, code: int, message: str, _contract: object = None) -> None:
        if code == 200:
            errors[req_id] = message

    ib.errorEvent += on_error
    try:
        for code in codes:
            req_id = ib.client.getReqId()
            contract = Contract(secType="NEWS", exchange=code, symbol=tape_symbol(code))
            ib.client.reqMktData(req_id, contract, "mdoff,292", False, False, [])
            await asyncio.sleep(TAPE_SETTLE_S)
            verdict = "Error 200 - paska neexistuje" if req_id in errors else "prijato"
            print(f"  {code:<10} {tape_symbol(code):<26} {verdict}")
            ib.client.cancelMktData(req_id)
            await asyncio.sleep(0.5)
    finally:
        ib.errorEvent -= on_error
        ib.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
