"""Vstupní bod news-engine: `python -m gexlens_news [run|status]` (SPEC kap. 10).

`run` nastartuje proces (schéma + collectory), `status` vypíše stav zdrojů a
počet nasbíraných eventů bez spouštění sběru — provozní kontrola z CLI.

Collectory se registrují až v N1 (#271, #272); dokud žádný není, proces korektně
běží naprázdno a `status` to říká nahlas, místo aby předstíral, že sbírá.
"""

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from collections.abc import Sequence

from sqlalchemy import create_engine

from gexlens_engine.storage.sentiment import ensure_sentiment_schema
from gexlens_news.collectors import Collector
from gexlens_news.collectors.finnhub import FinnhubCollector
from gexlens_news.collectors.forexfactory import ForexFactoryCollector
from gexlens_news.collectors.rss import RssCollector
from gexlens_news.config import (
    FED_RSS_URLS,
    NEWS_RSS_URLS,
    NewsSettings,
    load_news_settings,
)
from gexlens_news.http import Fetcher, make_fetcher
from gexlens_news.runner import CollectorRunner
from gexlens_news.store import NewsWriter

logger = logging.getLogger("gexlens.news")


def build_collectors(settings: NewsSettings, fetcher: Fetcher) -> list[Collector]:
    """Sada aktivních collectorů dle konfigurace.

    Tier A (#271) nepotřebuje klíče — veřejný kalendář a Fed RSS. Zdroje
    s prázdným klíčem se nezakládají vůbec (S10): vypnutý zdroj není porucha.
    """
    collectors: list[Collector] = [
        ForexFactoryCollector(fetcher, interval_s=settings.forexfactory_interval_s),
        RssCollector(
            "fed_rss",
            FED_RSS_URLS,
            fetcher,
            interval_s=settings.fed_rss_interval_s,
            category="FED",
            importance=3,
            symbols=["ES", "NQ"],
        ),
        # Tier B (#272): redundantní headline zdroje; dedup je slučuje (#273)
        RssCollector("rss_news", NEWS_RSS_URLS, fetcher, interval_s=settings.rss_interval_s),
    ]
    if settings.finnhub_api_key:
        collectors.append(
            FinnhubCollector(
                settings.finnhub_api_key, fetcher, interval_s=settings.finnhub_interval_s
            )
        )
    else:
        logger.info("Finnhub bez klíče — zdroj se nespouští (není to porucha)")
    return collectors


async def run(settings: NewsSettings) -> None:
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    ensure_sentiment_schema(engine)
    writer = NewsWriter(engine)
    fetcher = make_fetcher()
    collectors = build_collectors(settings, fetcher)
    runner = CollectorRunner(collectors, writer.write)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Windows nemá add_signal_handler pro SIGTERM — KeyboardInterrupt
        # odchytí `main`; proces se tam ukončí korektně
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    enabled = [name for name, on in settings.enabled_sources.items() if on]
    logger.info(
        "news-engine běží: %d collectorů, zdroje s konfigurací: %s",
        len(collectors),
        ", ".join(sorted(enabled)) or "žádné",
    )
    await runner.run(stop=stop)
    logger.info("news-engine ukončen")


def status(settings: NewsSettings) -> int:
    """Vypíše provozní stav; návratový kód 1, když je některý zdroj degraded."""
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    try:
        ensure_sentiment_schema(engine)
        total = NewsWriter(engine).count()
        db_state = "ok"
    except Exception as error:  # noqa: BLE001 — status nesmí spadnout na výjimce
        total = 0
        db_state = f"nedostupná ({type(error).__name__})"

    collectors = build_collectors(settings, make_fetcher())
    runner = CollectorRunner(collectors, lambda _events: 0)
    print(f"DB: {db_state}")
    print(f"Eventů v news_events: {total}")
    configured = [name for name, on in settings.enabled_sources.items() if on]
    missing = [name for name, on in settings.enabled_sources.items() if not on]
    print(f"Zdroje s konfigurací: {', '.join(sorted(configured)) or '—'}")
    if missing:
        print(f"Bez klíče (vypnuté): {', '.join(sorted(missing))}")
    print(f"Collectory: {len(collectors)} aktivních")
    degraded = 0
    for health in runner.status():
        print(f"  {health.name}: {health.state} (chyb v řadě: {health.consecutive_failures})")
        degraded += health.state == "degraded"
    return 1 if degraded else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gexlens_news", description="SentimentLens news-engine")
    parser.add_argument("command", choices=("run", "status"), nargs="?", default="run")
    args = parser.parse_args(argv)

    settings = load_news_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(name)s %(message)s",
    )
    if args.command == "status":
        return status(settings)
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        logger.info("Přerušeno uživatelem")
    return 0


if __name__ == "__main__":
    sys.exit(main())
