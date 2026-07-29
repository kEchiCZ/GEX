"""Obchodní kalendář CME pro indexové futures (#339, SPEC 2.2).

Odpovídá na jedinou otázku: **byl trh v čase `ts` zavřený?** Hodnota jde do
`news_events.market_closed` a podle SPEC 2.4 rozhoduje, do kterých bucketů
model zprávu zařadí — víkendový titulek se nesmí míchat s reakcemi z běžící
seance.

Žije v enginu, protože zprávy zapisují obě služby: broker headlines engine
(#291) a zbytek news-engine, který na engine závisí. Dvě implementace téhož
kalendáře by znamenaly, že tatáž sobotní zpráva má podle cesty jinou hodnotu.

Rozvrh Globexu pro ES/NQ v **americkém centrálním čase**:

* neděle 17:00 CT → pátek 16:00 CT běží nepřetržitě,
* po–čt denní přestávka 16:00–17:00 CT,
* sobota celý den zavřeno.

Časy jsou definované v CT, ne v UTC — proto `zoneinfo`, ne posun konstantou.
Aproximace DST by dvakrát ročně na několik týdnů posunula hranici o hodinu.

**Svátky CME tenhle modul nepokrývá** a je to vědomé: udržovaný seznam svátků
by tiše zastaral a lhal by pak stejně jako natvrdo zapsané `False`, jen míň
nápadně. Měření reakcí na tom nestojí — `deferred` v `news_reactions` se
odvozuje z mezery k prvnímu obchodovanému baru (SPEC 5.1), což svátky pokrývá
samo a bez kalendáře. Na svátek proto `market_closed` řekne „otevřeno", ale
okno se stejně změří deferred, takže model se šum nenaučí.
"""

import datetime as dt
from zoneinfo import ZoneInfo

# Rozvrh Globexu je definovaný v čase burzy, ne v UTC
CME_TZ = ZoneInfo("America/Chicago")
# 16:00 CT — denní přestávka i páteční závěr týdne
CLOSE_HOUR = 16
# 17:00 CT — nedělní otevření i konec denní přestávky
OPEN_HOUR = 17

_SATURDAY = 5
_SUNDAY = 6
_FRIDAY = 4


def is_market_closed(ts: dt.datetime) -> bool:
    """Byl trh s indexovými futures v čase `ts` zavřený?

    Naivní čas se bere jako UTC — stejně jako v `ibkr.newsticks.tick_time`;
    tichý posun o lokální zónu by hodnotu udělal nepředvídatelnou.
    """
    aware = ts if ts.tzinfo is not None else ts.replace(tzinfo=dt.UTC)
    local = aware.astimezone(CME_TZ)
    weekday = local.weekday()

    if weekday == _SATURDAY:
        return True
    if weekday == _SUNDAY:
        # Nový týden začíná až v 17:00 CT
        return local.hour < OPEN_HOUR
    if weekday == _FRIDAY and local.hour >= CLOSE_HOUR:
        # Páteční závěr — otevře se až v neděli
        return True
    # Po–čt (a pátek do 16:00): jen denní přestávka
    return CLOSE_HOUR <= local.hour < OPEN_HOUR
