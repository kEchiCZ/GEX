"""Dev laboratoř jen s tastytrade (#623) — engine bez IBKR větve.

Spouští se přes GEXLENS_TASTY_ONLY=true (`start-dev.ps1 -LiveTasty`): session,
chain mapa z REST, DXLink stream do cache a minutový heartbeat s pokrytím
eventů. NIC nepočítá, do DB ani parquet nepíše — slouží vývoji symbologie,
reconnectu a parsování za běhu trhu, aniž by se produkce musela čehokoli
vzdát (tasty snese souběžné streamy, ADR-0027; IBKR se tu vůbec nedotkne).

Vědomé omezení (zadání #623): cross-feed logika (#613 shadow, #614 fallback)
se tu z definice ověřit nedá — potřebuje oba feedy vedle sebe, validuje se
na produkci.
"""

import asyncio
import contextlib
import datetime as dt
import logging

from gexlens_engine.config import Settings
from gexlens_engine.tasty.provider import TastyChainCache
from gexlens_engine.tasty.session import TastyCredentials, TastySession
from gexlens_engine.tasty.stream import DxLinkStream
from gexlens_engine.tasty.symbols import ChainSymbols, SymbolMap

logger = logging.getLogger(__name__)

#: Kadence heartbeat logu — jedna řádka za minutu stačí na sledování života.
HEARTBEAT_S = 60.0
#: Obnova chain mapy + dorovnání subskripce (denní rotace expirací).
CHAIN_REFRESH_S = 300.0


def select_symbols(chain: ChainSymbols, cap: int) -> set[str]:
    """Streamer symboly od nejbližší expirace; `cap=0` = bez stropu.

    Dev si bere konzervativní podmnožinu (#623): kapacita subskripcí je
    pravděpodobně vázaná na účet, ne na grant, takže dev experiment nesmí
    ujídat rozpočet produkci. Vybírá se po CELÝCH expiracích od nejbližší;
    expirace, která se do stropu nevejde, se vynechá — useknutá polovina
    řetězu by byla k ničemu. Výjimka: nevejde-li se ani první expirace,
    ořízne se deterministicky (seřazené symboly), ať je co ladit.
    """
    by_expiry: dict[str, list[str]] = {}
    for (expiry, _strike, _right), streamer in chain.by_contract.items():
        by_expiry.setdefault(expiry, []).append(streamer)
    selected: set[str] = set()
    for expiry in sorted(by_expiry):
        symbols = by_expiry[expiry]
        if cap and len(selected) + len(symbols) > cap:
            if not selected:
                selected.update(sorted(symbols)[: cap or None])
            break
        selected.update(symbols)
    return selected


async def run_tasty_only(settings: Settings) -> None:
    """Hlavní smyčka tasty-only režimu — běží, dokud proces nedostane stop."""
    if not (settings.tasty_client_secret and settings.tasty_refresh_token):
        raise SystemExit(
            "GEXLENS_TASTY_ONLY=1 vyžaduje GEXLENS_TASTY_CLIENT_SECRET a "
            "GEXLENS_TASTY_REFRESH_TOKEN (dev grant patří do .env.dev, #696)"
        )
    session = TastySession(
        TastyCredentials(
            client_secret=settings.tasty_client_secret,
            refresh_token=settings.tasty_refresh_token,
        )
    )
    symbol_map = SymbolMap(session)
    cache = TastyChainCache()
    stream = DxLinkStream(session.quote_token, cache.on_event)
    stop = asyncio.Event()
    logger.info(
        "tasty-only režim (#623): IBKR větev VYPNUTA, produkce nedotčena; "
        "symboly %s, strop subskripcí %s",
        ",".join(settings.symbol_list),
        settings.tasty_max_subscriptions or "žádný",
    )

    async def chains_loop() -> None:
        while not stop.is_set():
            try:
                today = dt.datetime.now(dt.UTC).date()
                symbols: set[str] = set()
                for product in settings.symbol_list:
                    chain = await symbol_map.chain(product, today)
                    symbols |= select_symbols(chain, settings.tasty_max_subscriptions)
                if symbols:
                    await stream.set_symbols(symbols)
                    logger.info("tasty-only: subskribováno %d symbolů", len(symbols))
            except Exception:
                logger.exception(
                    "tasty-only: obnova chain mapy selhala — další pokus za %g s", CHAIN_REFRESH_S
                )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=CHAIN_REFRESH_S)

    async def heartbeat_loop() -> None:
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_S)
            if stop.is_set():
                return
            counts = cache.field_counts()
            logger.info(
                "tasty-only: sleduje %d symbolů — quote %d, greeks %d, OI %d, trades Σ %d",
                cache.symbols_tracked(),
                counts["quotes"],
                counts["greeks"],
                counts["summary"],
                counts["trades"],
            )

    await asyncio.gather(stream.run(stop), chains_loop(), heartbeat_loop())
