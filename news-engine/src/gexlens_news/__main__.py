"""Vstupní bod news-engine: `python -m gexlens_news [run|status]` (SPEC kap. 10).

`run` nastartuje proces (schéma + collectory), `status` vypíše stav zdrojů a
počet nasbíraných eventů bez spouštění sběru — provozní kontrola z CLI.

Collectory se registrují až v N1 (#271, #272); dokud žádný není, proces korektně
běží naprázdno a `status` to říká nahlas, místo aby předstíral, že sbírá.
"""

import argparse
import asyncio
import contextlib
import datetime as dt
import functools
import logging
import signal
import sys
from collections.abc import Sequence

from sqlalchemy import create_engine

from gexlens_engine.storage.sentiment import ensure_sentiment_schema
from gexlens_news.anomaly_job import AnomalyJob
from gexlens_news.bars import BarsRepository
from gexlens_news.classification_job import RuleClassificationJob
from gexlens_news.collectors import Collector
from gexlens_news.collectors.alpaca import AlpacaNewsStream
from gexlens_news.collectors.finnhub import FinnhubCollector
from gexlens_news.collectors.forexfactory import ForexFactoryCollector
from gexlens_news.collectors.rss import RssCollector
from gexlens_news.config import (
    FED_RSS_URLS,
    NEWS_RSS_URLS,
    NewsSettings,
    load_news_settings,
)
from gexlens_news.crowd import (
    CnnFearGreedCollector,
    CrowdRunner,
    CrowdWriter,
    PcrCollector,
    RedditCollector,
)
from gexlens_news.drift import DriftJob
from gexlens_news.ffhistory import FfActualRefreshJob, run_backfill
from gexlens_news.http import Fetcher, make_fetcher
from gexlens_news.llm_classifier import GeminiClient, LlmClassificationJob
from gexlens_news.model_stats_job import ModelStatsJob
from gexlens_news.pipeline import DedupingWriter
from gexlens_news.prediction_job import PredictionJob
from gexlens_news.publisher import NewsPublisher
from gexlens_news.reaction_job import ReactionJob
from gexlens_news.retro_pass import RetroPass, store_retro_result
from gexlens_news.review_job import ReviewJob
from gexlens_news.runner import CollectorRunner
from gexlens_news.sentiment_backfill import backfill_sentiment_daily
from gexlens_news.sentindex_job import SentIndexJob
from gexlens_news.signal_job import SignalJob
from gexlens_news.store import NewsWriter
from gexlens_news.track_record import TrackRecordJob
from gexlens_news.waves_job import WavesJob

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
    # Pořadí dle SPEC 3.1: normalizer → dedup → writer
    writer = DedupingWriter(NewsWriter(engine), window_minutes=settings.dedup_window_minutes)
    writer.prime_from_db(dt.datetime.now(dt.UTC))
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

    classification = RuleClassificationJob(engine)
    # Gemini pass (#281): bez klíče se nespouští — pravidlová klasifikace
    # je plnohodnotný fallback, ne porucha
    llm: LlmClassificationJob | None = None
    if settings.gemini_api_key:
        llm = LlmClassificationJob(
            engine,
            GeminiClient(settings.gemini_api_key, model=settings.gemini_model),
            daily_limit=settings.llm_daily_limit,
            batch_limit=settings.llm_batch_limit,
        )
    else:
        logger.info("Gemini bez klíče — LLM klasifikace se nespouští (není to porucha)")
    reactions = ReactionJob(engine, BarsRepository(settings.data_dir))
    # Anomální reakce (#295, SPEC 9.4): |ret| nad p90 bucketu → zvonek
    anomaly = AnomalyJob(engine)
    # Hodinové doplňování actual z FF kalendáře (#277) — widget feed ho nenese
    ff_refresh = (
        FfActualRefreshJob(engine, interval_s=settings.ff_actual_refresh_s)
        if settings.ff_actual_refresh_s > 0
        else None
    )
    model_stats = ModelStatsJob(engine)
    # Drift hlídka (#403): noční test, jestli modely ještě platí
    drift = DriftJob(engine)
    # Track record (#298): noční mechanické equity křivky (SPEC 7.3)
    track = TrackRecordJob(engine, BarsRepository(settings.data_dir))
    # Tier C crowd zdroje (#290): CNN F&G + PCR bez klíčů; Reddit jen s creds
    crowd_collectors: list[object] = [
        CnnFearGreedCollector(interval_s=settings.cnn_fg_interval_s),
        PcrCollector(
            settings.data_dir,
            [s.strip().upper() for s in settings.pcr_symbols.split(",") if s.strip()],
            interval_s=settings.pcr_interval_s,
        ),
    ]
    if settings.reddit_client_id and settings.reddit_client_secret:
        crowd_collectors.append(
            RedditCollector(
                settings.reddit_client_id,
                settings.reddit_client_secret,
                interval_s=settings.reddit_interval_s,
            )
        )
    else:
        logger.info("Reddit bez credentials — crowd zdroj se nespouští (není to porucha)")
    crowd = CrowdRunner(crowd_collectors, CrowdWriter(engine))
    review = ReviewJob(engine, symbol="ES")
    symbols = tuple(s.strip().upper() for s in settings.signal_symbols.split(",") if s.strip())
    # Vlny + stav per symbol (ADR-0026) — signály se generují proti stavu
    # SVÉHO symbolu, ne proti globální ES instanci
    waves_jobs = {symbol: WavesJob(engine, symbol=symbol) for symbol in symbols}
    # Signal engine (#294): always-on výpočet obou větví (S9)
    signal_job = SignalJob(engine, settings.data_dir, symbols=symbols)
    sent_index = SentIndexJob(engine, settings.data_dir, symbols=symbols)
    predictions = PredictionJob(engine)
    publisher = (
        NewsPublisher(settings.api_base, api_token=settings.api_token)
        if settings.api_base
        else None
    )
    retro = RetroPass(
        classification,
        reactions,
        sent_index,
        llm_job=llm,
        run_at=dt.time(settings.retro_pass_hour, settings.retro_pass_minute),
    )
    last_stats_day: dt.date | None = None

    async def reaction_loop() -> None:
        """Klasifikace, dopočet reakcí a denní přepočet modelu.

        Pád kterékoli fáze nesmí zastavit collectory ani ty ostatní.
        """
        nonlocal last_stats_day
        while not stop.is_set():
            now = dt.datetime.now(dt.UTC)
            # Pravidlová klasifikace první — bez kategorie a importance by
            # event do empirického modelu vůbec nevstoupil (SPEC 2.4)
            try:
                await asyncio.to_thread(classification.run, now)
                # Push klasifikovaných řádků (#335): engine už syrový titulek
                # pushnul hned po zápisu, tohle ho v UI doplní o kategorii
                if publisher is not None and classification.last_batch:
                    await publisher.publish_news(classification.last_batch)
            except Exception:
                logger.exception("Pravidlová klasifikace selhala — zkusí se příští cyklus")
            try:
                await asyncio.to_thread(reactions.run, now)
            except Exception:
                logger.exception(
                    "Dopočet reakcí selhal — zkusí se za %.0f s", settings.reaction_interval_s
                )
            # Anomálie hned po reakcích (#295) — hodnotí právě spočítaná okna
            try:
                anomaly_alerts = await asyncio.to_thread(anomaly.run, now)
                if publisher is not None:
                    for alert in anomaly_alerts:
                        await publisher.publish("alerts", alert)
            except Exception:
                logger.exception("Detekce anomálií selhala — zkusí se příští cyklus")
            # Review fronta (#293) po reakcích — auto-uzavírání čte uzavřená okna
            try:
                await asyncio.to_thread(review.run, now)
            except Exception:
                logger.exception("Review fronta selhala — zkusí se příští cyklus")
            # Predikce a jejich vyhodnocení musí být před indexem — váhy
            # z nich vstupují do skóre (SPEC 5.3)
            try:
                await asyncio.to_thread(functools.partial(predictions.run, now, symbols=symbols))
            except Exception:
                logger.exception("Vyhodnocení predikcí selhalo — zkusí se příští cyklus")
            # Index se přepočítává každý cyklus — je to živá hodnota pro panel
            try:
                points, topics = await asyncio.to_thread(sent_index.run, now)
                if publisher is not None and points:
                    # Topics jde jednou (nejsou per symbol), hodnota per symbol
                    topic_payload = [
                        {"category": t.category, "value": t.value, "active": t.active}
                        for t in topics
                        if t.active
                    ]
                    for symbol in symbols:
                        value = sent_index.last_values.get(symbol)
                        if value is None:
                            continue
                        await publisher.publish_sentiment(symbol, value, topic_payload, now)
                    upcoming = await asyncio.to_thread(sent_index.upcoming_events, now)
                    await publisher.publish_upcoming(upcoming, now)
            except Exception:
                logger.exception("Přepočet SentIndexu selhal — zkusí se příští cyklus")
            # Vlny + stav (#292) až PO indexu — čtou denní close, který index
            # právě upsertnul; změna stavu jde do WS `sentiment.state`.
            # Per symbol (ADR-0026): stavy se sbírají do mapy pro SignalJob.
            states: dict[str, str] = {}
            for symbol, waves in waves_jobs.items():
                try:
                    payload, changed = await asyncio.to_thread(waves.run, now)
                    states[symbol] = str(payload["state"])
                    if publisher is not None and changed:
                        await publisher.publish("sentiment.state", payload)
                except Exception:
                    logger.exception("Přepočet vln %s selhal — zkusí se příští cyklus", symbol)
            # Signal engine (#294) po vlnách — potřebuje potvrzené stavy.
            # Kanál `signals` běží vždy (S9); UI ho zobrazuje dle režimu.
            if states:
                try:
                    await asyncio.to_thread(signal_job.run, now, states=states)
                    if publisher is not None:
                        for created in signal_job.last_created:
                            await publisher.publish("signals", created)
                except Exception:
                    logger.exception("Signal engine selhal — zkusí se příští cyklus")
            # Ranní retro pass (#284): dožene noční fronty, ať trader ráno
            # otevírá aplikaci se zpracovanou nocí
            if retro.due(now):
                result = await asyncio.to_thread(retro.run, now)
                # Stav do `settings` — záložka Stats ho čte i po restartu UI (9.6)
                try:
                    await asyncio.to_thread(store_retro_result, engine, result)
                except Exception:
                    logger.exception("Uložení stavu retro passu selhalo")
                if publisher is not None:
                    await publisher.publish(
                        "news",
                        {"kind": "retro_pass", "message": result.describe(), "ts": now.isoformat()},
                    )
            # Model se přepočítává jednou denně (SPEC 2.4: noční job); běží po
            # reakcích, aby zahrnul i to, co se právě dopočítalo
            if last_stats_day != now.date():
                try:
                    await asyncio.to_thread(model_stats.run, now)
                    # Track record po model stats — čte tytéž noční vstupy
                    try:
                        await asyncio.to_thread(track.run, now)
                    except Exception:
                        logger.exception("Track record selhal — zkusí se příští den")
                    # Drift hlídka (#403) po čerstvých agregátech
                    try:
                        drift_alerts = await asyncio.to_thread(drift.run, now)
                        if publisher is not None:
                            for alert in drift_alerts:
                                await publisher.publish("alerts", alert)
                    except Exception:
                        logger.exception("Drift hlídka selhala — zkusí se příští den")
                    last_stats_day = now.date()
                except Exception:
                    logger.exception("Přepočet model stats selhal — zkusí se příští cyklus")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=settings.reaction_interval_s)

    async def llm_loop() -> None:
        """Gemini dávka à `llm_interval_s` — jen při neprázdné frontě (#281).

        Vlastní smyčka, ne součást reaction_loop: SPEC kap. 4 chce 60s kadenci,
        reakce jedou à 300 s. Prázdná fronta nestojí žádný request.
        """
        if llm is None:
            return
        while not stop.is_set():
            try:
                await asyncio.to_thread(llm.run, dt.datetime.now(dt.UTC))
                # Push nové verze do UI (#335) — doplní kategorii/směr k řádku
                if publisher is not None and llm.last_batch:
                    await publisher.publish_news(llm.last_batch)
            except Exception:
                logger.exception("LLM klasifikace selhala — zkusí se příští cyklus")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=settings.llm_interval_s)

    async def alpaca_loop() -> None:
        """Alpaca news WS (#387) — push stream s reconnectem; bez klíčů nic."""
        if not settings.alpaca_key_id or not settings.alpaca_secret:
            logger.info("Alpaca bez klíčů — WS stream se nespouští (není to porucha)")
            return
        stream = AlpacaNewsStream(settings.alpaca_key_id, settings.alpaca_secret, writer)
        await stream.run(stop)

    async def ff_actual_loop() -> None:
        """Refresh actual z FF kalendáře — vlastní 60s tikot (#386).

        V reaction_loop (à 300 s) by burst po release neměl jak tikat po
        minutě; `due()` sám rozhoduje mezi hodinovou kadencí a burstem.
        """
        if ff_refresh is None:
            return
        while not stop.is_set():
            now = dt.datetime.now(dt.UTC)
            if ff_refresh.due(now):
                try:
                    await asyncio.to_thread(ff_refresh.run, now)
                except Exception:
                    logger.exception("Refresh actual selhal — zkusí se příště")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=60.0)

    async def crowd_loop() -> None:
        """Crowd zdroje (#290) — intervaly per zdroj drží CrowdRunner."""
        while not stop.is_set():
            try:
                await asyncio.to_thread(crowd.run_due, dt.datetime.now(dt.UTC))
            except Exception:
                logger.exception("Crowd cyklus selhal — zkusí se za minutu")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=60.0)

    enabled = [name for name, on in settings.enabled_sources.items() if on]
    logger.info(
        "news-engine běží: %d collectorů, zdroje s konfigurací: %s",
        len(collectors),
        ", ".join(sorted(enabled)) or "žádné",
    )
    await asyncio.gather(
        runner.run(stop=stop),
        reaction_loop(),
        llm_loop(),
        crowd_loop(),
        ff_actual_loop(),
        alpaca_loop(),
    )
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


def backfill_sentiment(settings: NewsSettings) -> int:
    """Denní svíčky SentIndexu z historických eventů (#375, CLI příkaz)."""
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    ensure_sentiment_schema(engine)
    symbols = tuple(s.strip().upper() for s in settings.signal_symbols.split(",") if s.strip())
    stats = backfill_sentiment_daily(engine, symbols=symbols)
    print(f"Backfill sentiment_daily: {stats.describe()}")
    # Vlny se přepočtou hned — ať CLI rovnou ukáže výsledný stav (#292)
    for symbol in symbols:
        payload, _changed = WavesJob(engine, symbol=symbol).run(dt.datetime.now(dt.UTC))
        print(
            f"Stav {symbol}: {payload['state']} (unconfirmed={payload['unconfirmed']}), "
            f"close={payload['last_close']}, MA5={payload['ma5']}, MA10={payload['ma10']}"
        )
    return 0


def backfill_ff(settings: NewsSettings, weeks: int | None) -> int:
    """Jednorázový backfill historického FF kalendáře (#277, CLI příkaz)."""
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    ensure_sentiment_schema(engine)
    stats = run_backfill(engine, weeks=weeks or settings.ff_backfill_weeks)
    print(f"Backfill FF: {stats.describe()}")
    return 1 if stats.weeks_fetched == 0 else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gexlens_news", description="SentimentLens news-engine")
    parser.add_argument(
        "command",
        choices=("run", "status", "backfill-ff", "backfill-sentiment-daily"),
        nargs="?",
        default="run",
    )
    parser.add_argument(
        "--weeks",
        type=int,
        default=None,
        help="backfill-ff: kolik týdnů historie stáhnout (default z konfigurace)",
    )
    args = parser.parse_args(argv)

    settings = load_news_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(name)s %(message)s",
    )
    # S10 (#362): httpx na INFO vypisuje plná URL — u zdrojů s tokenem v query
    # (Finnhub) by klíč skončil v logu kontejneru při každém fetchi
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if args.command == "status":
        return status(settings)
    if args.command == "backfill-ff":
        return backfill_ff(settings, args.weeks)
    if args.command == "backfill-sentiment-daily":
        return backfill_sentiment(settings)
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        logger.info("Přerušeno uživatelem")
    return 0


if __name__ == "__main__":
    sys.exit(main())
