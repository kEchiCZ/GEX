"""Mapování našich kontraktů na dxFeed streamer symboly (#613).

Zdroj pravdy je chain endpoint `/futures-option-chains/{produkt}/nested` —
formát `./E2DQ26C7975:XCME` se NEskládá ručně (tradingClass kódy a měsíční
písmena se liší per série), ale čte z API a cachuje per (produkt, den).
Past z #612: futures symboly bez explicitního roku kolidují přes dekádu
(/ESU6 = 2016 i 2026) — mapa proto vždy pracuje s celým streamer symbolem
z API, nikdy s vlastní konstrukcí.
"""

import datetime as dt
import logging
from dataclasses import dataclass

from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.tasty.session import TastySession

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChainSymbols:
    """Mapa (expirace YYYYMMDD, strike, strana) → streamer symbol."""

    product: str
    day: dt.date
    by_contract: dict[tuple[str, float, str], str]

    def streamer_symbol(self, spec: OptionContractSpec) -> str | None:
        return self.by_contract.get((spec.expiry, spec.strike, spec.right))


class SymbolMap:
    """Denní cache chain map per produkt; obnova při změně dne."""

    def __init__(self, session: TastySession) -> None:
        self._session = session
        self._cache: dict[str, ChainSymbols] = {}

    async def chain(self, product: str, today: dt.date) -> ChainSymbols:
        cached = self._cache.get(product)
        if cached is not None and cached.day == today:
            return cached
        payload = await self._session.get_json(f"/futures-option-chains/{product}/nested")
        by_contract: dict[tuple[str, float, str], str] = {}
        for group in payload["data"].get("option-chains", []):
            for expiration in group.get("expirations", []):
                expiry = str(expiration.get("expiration-date", "")).replace("-", "")
                for strike in expiration.get("strikes", []):
                    price = float(strike["strike-price"])
                    call = strike.get("call-streamer-symbol")
                    put = strike.get("put-streamer-symbol")
                    if call:
                        by_contract[(expiry, price, "C")] = str(call)
                    if put:
                        by_contract[(expiry, price, "P")] = str(put)
        chain = ChainSymbols(product=product, day=today, by_contract=by_contract)
        self._cache[product] = chain
        logger.info(
            "tasty chain %s: %d kontraktů, %d expirací",
            product,
            len(by_contract),
            len({key[0] for key in by_contract}),
        )
        return chain
