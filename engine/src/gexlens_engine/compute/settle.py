"""Settle konvence US seance — hranice obchodního dne na JEDNOM místě (#498, #511).

Sdílí ji SetupEngine (settle expirace setupů), T6 (řez denních closů),
TendencyEngine (rampa charm hlasu) i runtime (čas do expirace pro Greeks).

Časy jsou definované v burzovním čase přes IANA zóny (vzor `marketclock.py`),
ne fixní UTC konstantou (#511): settle je 16:00 ET = 15:00 CT, což je v létě
20:00 UTC a v zimě 21:00 UTC — aproximace fixní hodinou by se půl roku míjela
o hodinu. Kdo hranici potřebuje, importuje odsud; žádné lokální konstanty.
"""

import datetime as dt
from zoneinfo import ZoneInfo

# Globex (CME) počítá v americkém centrálním čase
CME_TZ = ZoneInfo("America/Chicago")
# Konvence „16:00 ET" (cash close NYSE) — východní čas
ET_TZ = ZoneInfo("America/New_York")

# Settle US indexových futures: 16:00 ET (= 15:00 CT)
SETTLE_LOCAL = dt.time(16, 0)


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


def settle_ts(day: dt.date) -> dt.datetime:
    """Okamžik settle US seance daného kalendářního dne (UTC).

    16:00 ET → 20:00 UTC v létě, 21:00 UTC v zimě (shodné s frontend
    `instrument/expiry.ts`).
    """
    return session_time_utc(day, SETTLE_LOCAL.hour, SETTLE_LOCAL.minute, ET_TZ)


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


def trading_session_date(ts: dt.datetime) -> dt.date:
    """Obchodní den, do kterého okamžik `ts` patří (ADR-0023, #638).

    Po 17:00 America/Chicago běží seance NÁSLEDUJÍCÍHO kalendářního dne —
    protějšek frontend `sessionDateIso` (instrument/tz.ts).
    """
    local = ts.astimezone(CME_TZ)
    day = local.date()
    return day + dt.timedelta(days=1) if local.time() >= GLOBEX_OPEN_LOCAL else day
