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
from collections.abc import Sequence
from typing import Any

from gexlens_news.collectors.rss import FeedFailure, RssCollector

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
        for (source, url), failure in failing.items():
            if (source, url) in self._alerted or now - failure.since < self._alert_after:
                continue
            self._alerted[(source, url)] = failure
            alerts.append(
                {
                    "kind": ERROR_KIND,
                    "source": source,
                    "feed": url,
                    "message": (
                        f"⚠ Feed zpráv {source} selhává {_duration(now - failure.since)}"
                        f" ({failure.last_error}): {url} — zprávy z něj nechodí"
                    ),
                    "ts": int(now.timestamp()),
                }
            )
        for key in [key for key in self._alerted if key not in failing]:
            source, url = key
            since = self._alerted.pop(key).since
            alerts.append(
                {
                    "kind": RECOVERED_KIND,
                    "source": source,
                    "feed": url,
                    "message": (
                        f"✓ Feed zpráv {source} zase odpovídá po {_duration(now - since)}"
                        f" výpadku: {url}"
                    ),
                    "ts": int(now.timestamp()),
                }
            )
        return alerts
