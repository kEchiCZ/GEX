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

Svátky a zkrácené seance (#1308, ADR-0046) jsou z pravidel NYSE v `settle`
(žádný udržovaný seznam dat — ten by tiše zastaral):

* Vánoce, Nový rok, Velký pátek: Globex zavřený od zavření předchozího dne
  do 17:00 CT svátku,
* ostatní svátky (MLK, Presidents, Memorial, Juneteenth, 4. 7., Labor Day,
  Thanksgiving): obchoduje se do 12:00 CT, znovu od 17:00 CT,
* zkrácená seance (den po Thanksgiving, Štědrý den, 3. 7.): do 12:15 CT.

**Tohle je odhad, ne konečná hodnota.** Neplánované halty a mimořádná
zavření (státní smutek) rozvrh nezná.

Konečnou hodnotu proto zapisuje `ReactionJob` z **archivu 1min barů** (#339):
bar buď existuje, nebo ne, což je měření a ne kalendář. Rozvrh je tu jen proto,
aby zpráva měla rozumnou hodnotu hned při zápisu, než se k ní dostanou bary —
stejný vzorec jako provizorní bar rozdělané minuty (ADR-0005).
"""

import datetime as dt
from zoneinfo import ZoneInfo

from gexlens_engine.compute.settle import (
    EARLY_CLOSE_LOCAL,
    is_early_close,
    is_globex_closed_holiday,
    is_trading_session,
    is_us_market_holiday,
)

# Rozvrh Globexu je definovaný v čase burzy, ne v UTC
CME_TZ = ZoneInfo("America/Chicago")
# 16:00 CT — denní přestávka i páteční závěr týdne
CLOSE_HOUR = 16
# 17:00 CT — nedělní otevření i konec denní přestávky
OPEN_HOUR = 17

_SATURDAY = 5
_SUNDAY = 6
_FRIDAY = 4
# Svátek s Globexem: obchod do 12:00 CT (halt), znovu od 17:00 CT
HOLIDAY_HALT = dt.time(12, 0)
# Zkrácená seance: Globex indexových futures končí v 12:15 CT
EARLY_CLOSE_CT = dt.time(12, 15)


def is_market_closed(ts: dt.datetime) -> bool:
    """Byl trh s indexovými futures v čase `ts` zavřený?

    Naivní čas se bere jako UTC — stejně jako v `ibkr.newsticks.tick_time`;
    tichý posun o lokální zónu by hodnotu udělal nepředvídatelnou.
    """
    aware = ts if ts.tzinfo is not None else ts.replace(tzinfo=dt.UTC)
    local = aware.astimezone(CME_TZ)
    weekday = local.weekday()
    day = local.date()
    clock = local.time()

    if weekday == _SATURDAY:
        return True
    if weekday == _SUNDAY:
        # Nový týden začíná až v 17:00 CT — pokud pondělí není zavřený svátek
        return local.hour < OPEN_HOUR or is_globex_closed_holiday(day + dt.timedelta(days=1))
    if is_globex_closed_holiday(day):
        # Vánoce, Nový rok, Velký pátek: zavřeno do 17:00 CT, pak jako jindy
        return local.hour < OPEN_HOUR or weekday == _FRIDAY
    if local.hour >= OPEN_HOUR:
        # Večerní otevření další seance — jen když další den Globex obchoduje
        return weekday == _FRIDAY or is_globex_closed_holiday(day + dt.timedelta(days=1))
    if is_us_market_holiday(day):
        # Svátek s Globexem: obchod do 12:00 CT, pak zavřeno do 17:00 CT
        return clock >= HOLIDAY_HALT
    if is_early_close(day):
        return clock >= EARLY_CLOSE_CT
    # Po–pá do 16:00 obchod, 16:00–17:00 denní přestávka (v pátek závěr týdne)
    return local.hour >= CLOSE_HOUR


#: US RTH 9:30–16:00 New York — okno, ve kterém remediace (#877 C) NESMÍ
#: zasahovat: reconnect uprostřed hlavní seance = nenahraditelná díra.
_NY_TZ = ZoneInfo("America/New_York")
_US_RTH_OPEN = dt.time(9, 30)
_US_RTH_CLOSE = dt.time(16, 0)


def outside_us_rth(ts: dt.datetime) -> bool:
    """Je čas MIMO US RTH (9:30–16:00 NY, DST-korektně)? Den bez obchodní
    seance (víkend, svátek — `settle.is_trading_session`, #1308) = mimo; zkrácená
    seance končí v 13:00 ET.

    Používá remediace BS fallbacku (#877, varianta C): zásahy do spojení jen
    v Globex noci a pauzách, kdy je díra pár minut přijatelná cena.
    """
    aware = ts if ts.tzinfo is not None else ts.replace(tzinfo=dt.UTC)
    local = aware.astimezone(_NY_TZ)
    if not is_trading_session(local.date()):
        return True
    # Zkrácená seance (#1308): RTH končí v 13:00 ET
    close = EARLY_CLOSE_LOCAL if is_early_close(local.date()) else _US_RTH_CLOSE
    return not (_US_RTH_OPEN <= local.time() < close)
