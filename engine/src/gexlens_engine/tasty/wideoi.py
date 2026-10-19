"""Široký OI archiv z tasty Summary (#828, varianta A).

IBKR obálka je úzká (±200 b, ADR-0002) a strop 100 market data lines nedovolí
ji rozšířit bez prodloužení ranního průchodu. Puty přitom mají masu hluboko
OTM — přesně tam, kde náš řetěz končí — takže P/C i další celořetězové
agregáty vycházely systematicky vychýlené (naměřeno: put strana 3× nižší).

Tasty tenhle problém nemá: `Summary.openInterest` je pro FOP prokazatelně
shodný s IBKR tickem 101 (ADR-0027, 50/50 kontraktů), jede přes vlastní
subskripci mimo IBKR účet a teče **průběžně**, bez snapshot průchodu.

Archivují se JEN striky bez IBKR řádku dne: IBKR vlastní, co změřilo, tasty
zbytek řetězu. Původ hodnoty určuje vyplněný `captured_ts` (IBKR); tasty
řádek má jen `tasty_captured_ts`. Když IBKR strike doarchivuje (rozšíření
obálky), řádek přepíše a nese pak oba časy — tasty ho od té chvíle nemění
(stráž v `OIEodRepository.upsert_many`, #1605). Tasty řádky se během dne
obnovují a zapíše se jen změněná hodnota. Novou publikaci OI nese
`Summary.openInterest` už kolem 01:45–01:55 UTC dne D (20:45–20:55 CT
předchozího večera), asi o 10 h dřív než IBKR po publikaci CME v 07:00 CT
(měření 19. 10. 2026, #1626); po 12:00 UTC se hodnota už obvykle nemění.
"""

import asyncio
import datetime as dt
import logging
from collections.abc import Callable, Sequence
from dataclasses import replace

from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.storage.oi_archive import OIEodRepository, OIRecord
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
    """Kontrakty expirace, které IBKR NEpokrývá, + jejich streamer symbol.

    `covered` jsou kontrakty s IBKR řádkem dne — ty se vynechají, IBKR
    hodnotu tasty nepřepisuje (pokrytí po striku a straně přes všechny série).
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


async def wide_oi_pass(
    repository: OIEodRepository,
    chain: ChainSymbols,
    *,
    symbol: str,
    expiry: str,
    sample: OptionContractSpec,
    oi_lookup: OiLookup,
    now: dt.datetime,
) -> int:
    """Jeden průchod širokého OI aktivní expirace → počet skutečně zapsaných řádků.

    Pokryté jsou jen striky s IBKR řádkem dne (#1605). Do #1605 se pokrytí
    bralo z `values_for`, které vracelo i vlastní tasty zápisy: první běh
    v 00:00 UTC zapsal řetěz s předchozí publikací CME a od dalšího běhu byl
    každý strike „pokrytý" — tasty řádek se do konce dne neobnovil (~55 % Σ OI
    0DTE řetězu na den starých číslech, den expirace mimo obálku se do archivu
    nezapsal). Teď se tasty řádky během dne obnovují: zapíše se jen hodnota,
    která se proti tasty řádku téže série liší (porovnání per PK), takže
    minutový běh obvykle nezapíše nic. IBKR řádek nepřepíše ani souběžný
    zápis — stráž v `upsert_many`.

    `sample` nese exchange/trading_class/multiplier aktivní expirace.
    """
    day = now.date()
    rows = await asyncio.to_thread(repository.sourced_rows, symbol, expiry, day)
    covered = [replace(sample, strike=row.strike, right=row.right) for row in rows if row.ibkr]
    contracts = wide_contracts(
        chain,
        expiry,
        covered,
        symbol=symbol,
        exchange=sample.exchange,
        multiplier=sample.multiplier,
        trading_class=sample.trading_class,
    )
    if not contracts:
        return 0
    stored = {(row.trading_class, row.strike, row.right): row.oi for row in rows if not row.ibkr}
    records = [
        record
        for record in wide_records(contracts, oi_lookup, day)
        if stored.get((record.trading_class, record.strike, record.right)) != record.oi
    ]
    if not records:
        return 0
    return await asyncio.to_thread(repository.upsert_many, records, tasty_captured_ts=now)


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
