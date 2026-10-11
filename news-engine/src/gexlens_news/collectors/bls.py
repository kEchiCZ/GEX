"""BLS releasy z RSS (E-6.25c, ADR-0059 bod 5 a 7).

Feedy po releasech (`/feed/{release}.rss`, Atom); `bls_latest.rss` drží jen
1 položku, proto ne. Collector je `RssCollector` — tady je jen to, co je
specifické pro BLS: feedy, mapování na kalendář a čas releasu.

**`ts_event` = čas releasu z kalendáře FF.** Čas položky předbíhá release
o 39 min (E-6.22, 2.2), takže by reakci trhu měřil od chvíle, kdy o zprávě
nikdo nevěděl (look-ahead). Bere se řádek kalendáře téhož dne v ET s **přesným**
titulkem podle `BLS_RELEASE_TITLES` — kalendář nese i „USD ADP Non-Farm
Employment Change“ a „USD Core CPI m/m“. Bez shody platí čas prvního stažení
s pojistkami `RssCollector` (E-6.25a).
"""

import datetime as dt
from collections.abc import Callable, Mapping

from gexlens_engine.compute.settle import ET_TZ
from gexlens_news.classifier import bls_release
from gexlens_news.collectors.rss import ReleaseTime, parse_feed_time, plausible_item_time
from gexlens_news.model import RawItem

#: Release BLS → přesný titulek řádku kalendáře FF (ověřeno v produkční DB
#: 11. 10. 2026: nejvýš 1 řádek na titulek a den)
BLS_RELEASE_TITLES: Mapping[str, str] = {
    "empsit": "USD Non-Farm Employment Change",
    "cpi": "USD CPI m/m",
    "ppi": "USD PPI m/m",
    "jolts": "USD JOLTS Job Openings",
    "eci": "USD Employment Cost Index q/q",
}
BLS_RSS_URLS = tuple(f"https://www.bls.gov/feed/{release}.rss" for release in BLS_RELEASE_TITLES)

#: Kalendář: (přesný titulek, den v ET) → čas releasu v UTC, None = řádek není
CalendarLookup = Callable[[str, dt.date], dt.datetime | None]


def release_day(item: RawItem) -> dt.date:
    """Den releasu v ET: z času položky, záložně z času stažení.

    Položka předbíhá release o 39 min (7:51 proti 8:30 ET), den tedy sedí;
    čas stažení je záloha pro položku bez věrohodného času.
    """
    published = plausible_item_time(parse_feed_time(item.payload.get("published")), item.fetched_at)
    return (published or item.fetched_at).astimezone(ET_TZ).date()


def bls_release_time(lookup: CalendarLookup) -> ReleaseTime:
    """Čas releasu položky BLS z kalendáře přes injektovaný dotaz (testovatelné bez IO)."""

    def release_time(item: RawItem) -> dt.datetime | None:
        release = bls_release(str(item.payload.get("feed") or ""))
        title = BLS_RELEASE_TITLES.get(release) if release else None
        if title is None:
            return None
        return lookup(title, release_day(item))

    return release_time
