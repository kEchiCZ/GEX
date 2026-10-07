"""Settle konvence US seance — hranice obchodního dne na JEDNOM místě (#498, #511).

Sdílí ji SetupEngine (settle expirace setupů), T6 (řez denních closů),
TendencyEngine (rampa charm hlasu) i runtime (čas do expirace pro Greeks).

Časy jsou definované v burzovním čase přes IANA zóny (vzor `marketclock.py`),
ne fixní UTC konstantou (#511): settle je 16:00 ET = 15:00 CT, což je v létě
20:00 UTC a v zimě 21:00 UTC — aproximace fixní hodinou by se půl roku míjela
o hodinu. Kdo hranici potřebuje, importuje odsud; žádné lokální konstanty.
"""

import datetime as dt
from functools import lru_cache
from zoneinfo import ZoneInfo

# Globex (CME) počítá v americkém centrálním čase
CME_TZ = ZoneInfo("America/Chicago")
# Konvence „16:00 ET" (cash close NYSE) — východní čas
ET_TZ = ZoneInfo("America/New_York")

# Settle US indexových futures: 16:00 ET (= 15:00 CT)
SETTLE_LOCAL = dt.time(16, 0)
# Zkrácená seance (den po Thanksgiving, Štědrý den, 3. 7.): close NYSE 13:00 ET
EARLY_CLOSE_LOCAL = dt.time(13, 0)


# ── Kalendář svátků US akciového trhu (#1308, ADR-0046) ──────────────────────
#
# Pravidla NYSE, ne udržovaný seznam dat: seznam tiše zastará (marketclock),
# pravidla platí každý rok. CME indexové futures se řídí týmž kalendářem —
# v den svátku nemají settle ani US RTH; Globex o většině svátků obchoduje do
# 12:00 CT (`marketclock`), o Vánocích, Novém roce a Velkém pátku je zavřený.
# Mimořádná zavření (státní smutek) pravidla neznají — konečné slovo o tom,
# jestli se obchodovalo, mají dál bary (ADR-0023 bod 4, #339).


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    """N-tý výskyt dne v týdnu (0 = pondělí) v měsíci; n = −1 = poslední."""
    if n > 0:
        first = dt.date(year, month, 1)
        return first + dt.timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    last = dt.date(year + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1)
    return last - dt.timedelta(days=(last.weekday() - weekday) % 7)


def easter_sunday(year: int) -> dt.date:
    """Velikonoční neděle (gregoriánský výpočet, anonymní algoritmus)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741 — název z algoritmu
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return dt.date(year, month, day + 1)


def _observed(day: dt.date) -> dt.date:
    """Svátek o víkendu se drží v pátek (sobota) nebo v pondělí (neděle)."""
    if day.weekday() == 5:
        return day - dt.timedelta(days=1)
    if day.weekday() == 6:
        return day + dt.timedelta(days=1)
    return day


@lru_cache(maxsize=64)
def us_market_holidays(year: int) -> frozenset[dt.date]:
    """Celodenní svátky NYSE roku `year` — dny bez US seance (bez RTH i settle)."""
    days = {
        _nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr.
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        easter_sunday(year) - dt.timedelta(days=2),  # Velký pátek
        _nth_weekday(year, 5, 0, -1),  # Memorial Day
        _observed(dt.date(year, 7, 4)),  # Den nezávislosti
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed(dt.date(year, 12, 25)),  # Vánoce
    }
    new_year = dt.date(year, 1, 1)
    # Nový rok v sobotu se nepřesouvá na pátek 31. 12. (konec účetního roku)
    if new_year.weekday() != 5:
        days.add(_observed(new_year))
    if year >= 2022:
        days.add(_observed(dt.date(year, 6, 19)))  # Juneteenth
    return frozenset(days)


def is_us_market_holiday(day: dt.date) -> bool:
    """Je `day` celodenní svátek US akciového trhu (NYSE, CME bez settle)?"""
    return day in us_market_holidays(day.year)


def is_globex_closed_holiday(day: dt.date) -> bool:
    """Svátek, kdy CME Globex u indexových futures neobchoduje vůbec (do 17:00 CT).

    Vánoce (25. 12.), Nový rok (1. 1.) a Velký pátek; o ostatních svátcích
    (i o přesunutém dni Vánoc/Nového roku) Globex obchoduje do 12:00 CT
    (`marketclock`). Je to rozvrh, odhad — konečné slovo mají bary.
    """
    if not is_us_market_holiday(day):
        return False
    good_friday = easter_sunday(day.year) - dt.timedelta(days=2)
    return day == good_friday or (day.month, day.day) in ((12, 25), (1, 1))


def is_early_close(day: dt.date) -> bool:
    """Zkrácená seance — NYSE zavírá v 13:00 ET, CME settle i Globex dřív.

    Den po Thanksgiving vždy; Štědrý den a 3. červenec, když padnou na po–čt
    (v pátek je místo nich držený svátek, o víkendu nic).
    """
    if day.weekday() >= 5 or is_us_market_holiday(day):
        return False
    if day == _nth_weekday(day.year, 11, 3, 4) + dt.timedelta(days=1):
        return True
    return (day.month, day.day) in ((12, 24), (7, 3))


def session_time_utc(day: dt.date, hh: int, mm: int, tz: ZoneInfo) -> dt.datetime:
    """UTC okamžik burzovního času `hh:mm` kalendářního dne `day` v zóně `tz`.

    `zoneinfo` řeší DST za nás — tentýž lokální čas padne v létě a v zimě na
    jinou UTC hodinu. Dny přechodu DST řeší fold=0 (první výskyt času).
    """
    local = dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)
    return local.astimezone(dt.UTC)


QUARTER_MONTHS = (3, 6, 9, 12)
# SOQ (Special Opening Quotation) kvartální expirace: 9:30 ET (#1189, ADR-0039)
SOQ_LOCAL = dt.time(9, 30)


def quarterly_expiry(year: int, month: int) -> dt.date:
    """3. pátek měsíce — expirace kvartálních ES/NQ futures a měsíčních opcí."""
    first = dt.date(year, month, 1)
    first_friday = first + dt.timedelta(days=(4 - first.weekday()) % 7)
    return first_friday + dt.timedelta(days=14)


def is_quarterly_expiry(day: dt.date) -> bool:
    """3. pátek března/června/září/prosince = kvartální expirace (SOQ ráno)."""
    return day.month in QUARTER_MONTHS and day == quarterly_expiry(day.year, day.month)


def soq_ts(day: dt.date) -> dt.datetime:
    """Okamžik SOQ (9:30 ET) daného dne v UTC."""
    return session_time_utc(day, SOQ_LOCAL.hour, SOQ_LOCAL.minute, ET_TZ)


def expiry_settle_ts(day: dt.date) -> dt.datetime:
    """Settle EXPIRACE (ne seance): kvartální opce a futures se vypořádají
    ráno v SOQ 9:30 ET (#1189), všechny ostatní expirace v 16:00 ET.

    Kdo počítá čas do expirace řetězu, timeout setupu podle expirace nebo
    platnost front kontraktu, volá tohle; hranice SEANCE zůstává `settle_ts`.
    """
    return soq_ts(day) if is_quarterly_expiry(day) else settle_ts(day)


def expiry_settle(expiry: str) -> dt.datetime | None:
    """Settle expirace zapsané jako `YYYYMMDD` (IBKR) — JEDINÝ helper nad řetězcem (#1331).

    Sdílí ho roll pipeline (`instruments.expiry_expired`, discovery i cache),
    hlídka Greeks, setupy (timeout, čas do expirace, invariant vzniku
    `born_after_settle`) i sondy T9 — všichni se ptají téže hranice
    `expiry_settle_ts` (ADR-0039 bod 2). Nečitelný formát → None: rozhodnutí
    je na volajícím (nic se nevymýšlí).
    """
    try:
        day = dt.datetime.strptime(expiry, "%Y%m%d").date()
    except ValueError:
        return None
    return expiry_settle_ts(day)


def settle_ts(day: dt.date) -> dt.datetime:
    """Okamžik settle US seance daného kalendářního dne (UTC).

    16:00 ET → 20:00 UTC v létě, 21:00 UTC v zimě (shodné s frontend
    `instrument/expiry.ts`); ve zkrácené seanci 13:00 ET (#1308).
    """
    local = EARLY_CLOSE_LOCAL if is_early_close(day) else SETTLE_LOCAL
    return session_time_utc(day, local.hour, local.minute, ET_TZ)


# Otevření Globex — hranice obchodního dne (ADR-0023 bod 3, #512/#638)
GLOBEX_OPEN_LOCAL = dt.time(17, 0)


def session_bounds(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """UTC hranice Globex seance obchodního dne `day`: [open 17:00 CT D−1, open D).

    Polouzavřený interval — každá minuta patří právě jedné seanci. Sdílí ji
    čtecí vrstva API (#512) i reset kumulativů v enginu (#638).
    """

    def open_of(d: dt.date) -> dt.datetime:
        return session_time_utc(d, GLOBEX_OPEN_LOCAL.hour, GLOBEX_OPEN_LOCAL.minute, CME_TZ)

    return open_of(day - dt.timedelta(days=1)), open_of(day)


def is_trading_session(day: dt.date) -> bool:
    """Má obchodní den `day` US seanci s RTH openem (9:30 ET) i settle? Po–pá mimo svátky.

    **Jediný predikát „obchodní den" v enginu** (#1309, AGENTS.md): kdo na den
    váže open, settle, publikaci OI nebo srovnání „proti předchozímu dni",
    ptá se tady — žádné lokální `weekday() < 5` jako test obchodního dne
    (kalendářní otázky jako pondělí týdne nebo 3. pátek jsou jiná věc).
    `trading_session_date` vrací pro sobotu a neděli před otevřením Globexu
    kalendářní den, který seanci nemá; OI archiv má klíče podle UTC dne
    včetně víkendu. Protějšek ve frontendu je `isTradingSessionIso`
    (instrument/tz.ts) — mění se spolu, jako `trading_session_date` ↔
    `sessionDateIso`. Svátky (#1308, ADR-0046) zná z pravidel NYSE
    (`is_us_market_holiday`) — tady i v protějšku, a tím všem volajícím naráz.
    Zkrácená seance obchodní den je, jen se settle v 13:00 ET (`settle_ts`).
    """
    return day.weekday() < 5 and not is_us_market_holiday(day)


def trading_session_date(ts: dt.datetime) -> dt.date:
    """Obchodní den, do kterého okamžik `ts` patří (ADR-0023, #638).

    Po 17:00 America/Chicago běží seance NÁSLEDUJÍCÍHO kalendářního dne —
    protějšek frontend `sessionDateIso` (instrument/tz.ts).
    """
    local = ts.astimezone(CME_TZ)
    day = local.date()
    return day + dt.timedelta(days=1) if local.time() >= GLOBEX_OPEN_LOCAL else day
