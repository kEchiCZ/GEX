"""Široký OI archiv z tasty Summary (#828, varianta A).

IBKR obálka je úzká (±200 b, ADR-0002) a strop 100 market data lines nedovolí
ji rozšířit bez prodloužení ranního průchodu. Puty přitom mají masu hluboko
OTM — přesně tam, kde náš řetěz končí — takže P/C i další celořetězové
agregáty vycházely systematicky vychýlené (naměřeno: put strana 3× nižší).

Tasty tenhle problém nemá: `Summary.openInterest` je pro FOP prokazatelně
shodný s IBKR tickem 101 (ADR-0027, 50/50 kontraktů), jede přes vlastní
subskripci mimo IBKR účet a teče **průběžně**, bez snapshot průchodu.

Archivují se JEN striky mimo IBKR obálku: každý kontrakt má tak právě jeden
zdroj, klíče nekolidují a původ hodnoty jde poznat podle toho, který sloupec
času je vyplněný (`captured_ts` = IBKR, `tasty_captured_ts` = tasty).
"""

import datetime as dt
import logging
from collections.abc import Callable, Sequence

from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.storage.oi_archive import OIRecord
from gexlens_engine.tasty.symbols import ChainSymbols

logger = logging.getLogger(__name__)

#: Lookup OI pro streamer symbol; None = kontrakt v cache není nebo OI nedorazilo
OiLookup = Callable[[str], float | None]


def wide_contracts(
    chain: ChainSymbols,
    expiry: str,
    covered: Sequence[OptionContractSpec],
    *,
    symbol: str,
    exchange: str,
    multiplier: str,
    trading_class: str,
) -> list[tuple[OptionContractSpec, str]]:
    """Kontrakty expirace, které IBKR obálka NEpokrývá, + jejich streamer symbol.

    `covered` jsou kontrakty, které už archivuje IBKR — ty se vynechají, aby
    se dva zdroje nikdy nepraly o týž řádek.
    """
    covered_keys = {(c.expiry, c.strike, c.right) for c in covered}
    out: list[tuple[OptionContractSpec, str]] = []
    for (chain_expiry, strike, right), streamer in chain.by_contract.items():
        if chain_expiry != expiry:
            continue
        if (chain_expiry, strike, right) in covered_keys:
            continue
        out.append(
            (
                OptionContractSpec(
                    symbol=symbol,
                    sec_type="FOP",
                    expiry=chain_expiry,
                    strike=strike,
                    right=right,
                    exchange=exchange,
                    trading_class=trading_class,
                    multiplier=multiplier,
                ),
                streamer,
            )
        )
    return out


def wide_records(
    contracts: Sequence[tuple[OptionContractSpec, str]],
    oi_lookup: OiLookup,
    day: dt.date,
) -> list[OIRecord]:
    """Záznamy k zápisu; kontrakty bez OI se vynechají (díra, ne nula).

    Nula je legitimní naměřená hodnota (strike bez otevřených pozic), kdežto
    chybějící Summary znamená „nevíme" — zapsat ji jako 0 by zkreslilo
    agregáty stejně, jako to dělá dnešní useknutý řetěz.
    """
    records: list[OIRecord] = []
    for spec, streamer in contracts:
        oi = oi_lookup(streamer)
        if oi is None:
            continue
        records.append(
            OIRecord(
                symbol=spec.symbol,
                expiry=spec.expiry,
                strike=spec.strike,
                right=spec.right,
                day=day,
                oi=oi,
                trading_class=spec.trading_class or "",
            )
        )
    return records


def wide_streamers(
    chain: ChainSymbols,
    expiry: str,
    covered: Sequence[OptionContractSpec],
    *,
    center: float | None,
    band_pct: float,
) -> set[str]:
    """Streamery aktivní expirace MIMO IBKR obálku (#828).

    Bez nich se široký archiv nemá kde projevit: `Summary` (a tedy OI) chodí
    jen pro subskribované symboly, a extended plán aktivní expiraci z definice
    neobsahuje — `plan_extended_expiries` je chain MÍNUS expirace IBKR.

    Disjunktnost se drží na úrovni striků, ne expirací: IBKR čte svoje pásmo,
    tasty zbytek téže expirace. Pásmo je v % ceny (lekce ADR-0004) a bez
    známého centra se nevrací nic — slepé pokrytí by jen sežralo kapacitu.
    """
    if center is None:
        return set()
    covered_keys = {(c.strike, c.right) for c in covered}
    band_points = center * band_pct / 100.0
    return {
        streamer
        for (chain_expiry, strike, right), streamer in chain.by_contract.items()
        if chain_expiry == expiry
        and (strike, right) not in covered_keys
        and abs(strike - center) <= band_points
    }
