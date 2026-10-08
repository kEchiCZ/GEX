"""Hlídka jednotlivých RSS feedů (#1451).

Zdravotní stav collectoru (`CollectorHealth`) hlídá zdroj jako celek: dokud
odpovídá aspoň jeden jeho feed, je „ok". Yahoo `rssindex` tak od 23. 9. 2026
vracel HTTP 404 dva týdny a jediná stopa byla `warning` v logu každou minutu —
`rss_news` mezitím přišel o 94 % zpráv, které už nejdou zpětně dohledat.

Hlídka proto sleduje každý feed zvlášť: feed, který selhává bez přerušení
alespoň `FEED_ALERT_AFTER`, ohlásí jedním alertem `news_feed_error`; jeho první
úspěšné stažení pak jedním `news_feed_recovered`. Práh hodiny odfiltruje
krátké výpadky sítě (24. 9. 15:42 ConnectError u všech feedů naráz).

**Brána zavřeného trhu tu není záměrně.** Pravidlo „zavřený trh = žádná
upozornění na chybějící data" (AGENTS.md) stojí na otázce „očekávají se data?".
U zpravodajských RSS je odpověď vždy ano — agentury publikují i o víkendu
a v noci — a hlídka nereaguje na ticho feedu, jen na chybu jeho stažení
(HTTP 4xx/5xx, síť, nečitelné XML).
"""

import datetime as dt
import logging
from collections.abc import Sequence
from typing import Any
from urllib.parse import parse_qs, urlsplit

from gexlens_news.collectors.rss import FeedFailure, RssCollector

logger = logging.getLogger(__name__)

#: Jak dlouho musí feed selhávat bez přerušení, než se ohlásí
FEED_ALERT_AFTER = dt.timedelta(hours=1)
ERROR_KIND = "news_feed_error"
RECOVERED_KIND = "news_feed_recovered"


def _duration(delta: dt.timedelta) -> str:
    """Délka výpadku pro text alertu: minuty pod hodinu, jinak hodiny a dny."""
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} h"
    return f"{hours // 24} d"


def feed_label(url: str) -> str:
    """Krátké jméno feedu na začátek zprávy: host bez `www.`, cesta a symbol `s=`.

    Telegram slučuje alerty se shodným začátkem zprávy (prvních 80 znaků) — dva
    Yahoo feedy se liší jen parametrem `s`, dva Fed feedy jen cestou.
    """
    parts = urlsplit(url)
    label = parts.netloc.removeprefix("www.") + parts.path
    symbol = parse_qs(parts.query).get("s")
    return f"{label} {symbol[0]}" if symbol else label


class FeedWatch:
    """Hranové alerty pro RSS feedy, které trvale selhávají.

    Stav „už ohlášeno" žije v paměti procesu. Po restartu news-engine se feed,
    který pořád selhává, ohlásí znovu až po dalším `FEED_ALERT_AFTER` — collector
    začíná bez historie, takže to je připomínka trvající poruchy, ne spam.
    """

    def __init__(
        self,
        collectors: Sequence[RssCollector],
        *,
        alert_after: dt.timedelta = FEED_ALERT_AFTER,
    ) -> None:
        self._collectors = list(collectors)
        self._alert_after = alert_after
        self._alerted: dict[tuple[str, str], FeedFailure] = {}

    def run(self, now: dt.datetime) -> list[dict[str, Any]]:
        """Alerty pro feedy, které práh právě překročily nebo se právě obnovily."""
        failing = {
            (collector.name, url): failure
            for collector in self._collectors
            for url, failure in collector.feed_failures.items()
        }
        alerts: list[dict[str, Any]] = []
        # Ohlášená epizoda končí, když feed neselhává, nebo když mezi dvěma tiky
        # ožil a selhal znovu (řada má jiný začátek) — nová se ohlásí po prahu
        for key, alerted in list(self._alerted.items()):
            current = failing.get(key)
            if current is None or current.since != alerted.since:
                del self._alerted[key]
                alerts.append(self._recovered(key, alerted, now))
        for key, failure in failing.items():
            if key in self._alerted or now - failure.since < self._alert_after:
                continue
            self._alerted[key] = failure
            alerts.append(self._failed(key, failure, now))
        return alerts

    @staticmethod
    def _failed(key: tuple[str, str], failure: FeedFailure, now: dt.datetime) -> dict[str, Any]:
        source, url = key
        duration = _duration(now - failure.since)
        logger.warning("Feed %s (%s) selhává %s: %s", url, source, duration, failure.last_error)
        return {
            "kind": ERROR_KIND,
            "source": source,
            "feed": url,
            "message": (
                f"⚠ Feed {feed_label(url)} ({source}) selhává {duration}"
                f" ({failure.last_error}) — zprávy z něj nechodí: {url}"
            ),
            "ts": int(now.timestamp()),
        }

    @staticmethod
    def _recovered(key: tuple[str, str], failure: FeedFailure, now: dt.datetime) -> dict[str, Any]:
        source, url = key
        duration = _duration(now - failure.since)
        logger.info("Feed %s (%s) zase odpovídá po %s výpadku", url, source, duration)
        return {
            "kind": RECOVERED_KIND,
            "source": source,
            "feed": url,
            "message": f"✓ Feed {feed_label(url)} ({source}) zase odpovídá po {duration} výpadku",
            "ts": int(now.timestamp()),
        }
