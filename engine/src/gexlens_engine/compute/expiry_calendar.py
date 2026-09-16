"""Kalendář expirací a rollu futures (#1189, ADR-0039) — čisté funkce.

Kvartální expirační týden (3. pátek března/června/září/prosince) má jiný
průběh než běžný den a aplikace to má vědět:

- **Roll date** = čtvrtek 8 dní před expirací (CME): objem a likvidita
  přecházejí do dalšího kontraktu; od tohoto dne je front kontrakt ten další.
- **SOQ** (Special Opening Quotation) = pátek 9:30 ET: kvartální futures
  a kvartální opce se vypořádají z otevíracích cen indexu — settle ráno,
  ne odpoledne (`compute.settle.expiry_settle_ts`).
- **OPEX týden** = týden expirace: cena tažená hedgingem dealerů, sentiment
  je šum; po pátku zmizí opční podpora a pondělí bývá bez brzdy.
- **VIX expirace** = středa 30 dní před 3. pátkem NÁSLEDUJÍCÍHO měsíce;
  v kvartálním týdnu ráno odejde gamma, která tlumila VIX.

Měsíční OPEX (3. pátek ostatních měsíců) se hlásí slaběji — opce jsou PM
settled a futures se nerolují.
"""

import datetime as dt
from dataclasses import dataclass
from typing import Literal

from gexlens_engine.compute.settle import (
    ET_TZ,
    QUARTER_MONTHS,
    is_quarterly_expiry,
    quarterly_expiry,
    session_time_utc,
    settle_ts,
    soq_ts,
)

#: CME roll date = 8 kalendářních dní před expirací (čtvrtek předchozího týdne)
ROLL_DAYS_BEFORE_EXPIRY = 8

Phase = Literal["normal", "roll", "opex_week", "expiry_day", "post_opex"]
MarkerKind = Literal["roll", "quarterly_expiry", "monthly_opex", "vix_expiry"]


def third_friday(year: int, month: int) -> dt.date:
    return quarterly_expiry(year, month)


def next_quarterly_expiry(today: dt.date) -> dt.date:
    """Nejbližší kvartální expirace ≥ dnes (v den expirace je to dnešek)."""
    year, month = today.year, today.month
    for _ in range(8):
        if month in QUARTER_MONTHS:
            candidate = quarterly_expiry(year, month)
            if candidate >= today:
                return candidate
        month += 1
        if month > 12:
            month, year = 1, year + 1
    raise RuntimeError("kvartální expirace nenalezena")  # pragma: no cover


def previous_quarterly_expiry(today: dt.date) -> dt.date:
    """Poslední kvartální expirace < dnes."""
    year, month = today.year, today.month
    for _ in range(8):
        if month in QUARTER_MONTHS:
            candidate = quarterly_expiry(year, month)
            if candidate < today:
                return candidate
        month -= 1
        if month < 1:
            month, year = 12, year - 1
    raise RuntimeError("kvartální expirace nenalezena")  # pragma: no cover


def roll_date(expiry: dt.date) -> dt.date:
    return expiry - dt.timedelta(days=ROLL_DAYS_BEFORE_EXPIRY)


def vix_expiry_before(monthly_expiry: dt.date) -> dt.date:
    """VIX expirace = středa 30 dní před 3. pátkem daného měsíce (CBOE)."""
    return monthly_expiry - dt.timedelta(days=30)


def front_contract_eligible(last_trade_date: dt.date, today: dt.date, roll_days: int) -> bool:
    """Kontrakt je front, dokud má do expirace VÍC než `roll_days` dní.

    Na roll date (expirace − 8 d) se přepíná: 10. 9. → 18. 9. je přesně 8 dní,
    tedy už Z6. Den před (9 dní) ještě U6. `roll_days = 0` = původní chování
    (nejbližší nepropadlý kontrakt).
    """
    if roll_days <= 0:
        return last_trade_date >= today
    return (last_trade_date - today).days > roll_days


@dataclass(frozen=True)
class CalendarMarker:
    date: dt.date
    kind: MarkerKind
    #: Okamžik v UTC pro intradenní osu (SOQ, settle, 00:00 pro roll)
    ts: dt.datetime
    label: str


@dataclass(frozen=True)
class ExpiryCalendar:
    today: dt.date
    phase: Phase
    quarterly_expiry: dt.date
    roll_date: dt.date
    soq_ts: dt.datetime
    days_to_expiry: int
    is_opex_week: bool
    vix_expiry: dt.date
    previous_expiry: dt.date
    markers: tuple[CalendarMarker, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "today": self.today.isoformat(),
            "phase": self.phase,
            "quarterly_expiry": self.quarterly_expiry.isoformat(),
            "roll_date": self.roll_date.isoformat(),
            "soq_ts": self.soq_ts.isoformat(),
            "days_to_expiry": self.days_to_expiry,
            "is_opex_week": self.is_opex_week,
            "vix_expiry": self.vix_expiry.isoformat(),
            "previous_expiry": self.previous_expiry.isoformat(),
            "markers": [
                {
                    "date": marker.date.isoformat(),
                    "kind": marker.kind,
                    "ts": marker.ts.isoformat(),
                    "label": marker.label,
                }
                for marker in self.markers
            ],
        }


def _week_monday(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def phase_of(today: dt.date) -> Phase:
    expiry = next_quarterly_expiry(today)
    previous = previous_quarterly_expiry(today)
    if today == expiry:
        return "expiry_day"
    if _week_monday(today) == _week_monday(expiry):
        return "opex_week"
    if today >= roll_date(expiry):
        return "roll"
    # Pondělí–středa týdne po kvartální expiraci: bez opční podpory
    if (today - previous).days <= 5 and _week_monday(today) == _week_monday(
        previous + dt.timedelta(days=3)
    ):
        return "post_opex"
    return "normal"


def calendar_markers(
    today: dt.date, *, back_days: int = 14, ahead_days: int = 100
) -> list[CalendarMarker]:
    """Značky do grafu v okně [dnes − back, dnes + ahead]: roll, kvartální
    expirace (SOQ), měsíční OPEX (settle), VIX expirace."""
    start = today - dt.timedelta(days=back_days)
    end = today + dt.timedelta(days=ahead_days)
    markers: list[CalendarMarker] = []
    year, month = start.year, start.month
    for _ in range(24):
        friday = quarterly_expiry(year, month)
        if start <= friday <= end:
            if month in QUARTER_MONTHS:
                code = {3: "H", 6: "M", 9: "U", 12: "Z"}[month]
                markers.append(
                    CalendarMarker(
                        friday,
                        "quarterly_expiry",
                        soq_ts(friday),
                        f"Kvartální expirace {code}{year % 10} · SOQ 9:30 ET",
                    )
                )
                roll = roll_date(friday)
                if start <= roll <= end:
                    markers.append(
                        CalendarMarker(
                            roll,
                            "roll",
                            session_time_utc(roll, 9, 30, ET_TZ),
                            f"Roll futures → další kontrakt (expirace {friday.isoformat()})",
                        )
                    )
            else:
                markers.append(
                    CalendarMarker(
                        friday, "monthly_opex", settle_ts(friday), "Měsíční OPEX (PM settle)"
                    )
                )
        vix = vix_expiry_before(friday)
        if start <= vix <= end:
            markers.append(
                CalendarMarker(
                    vix, "vix_expiry", session_time_utc(vix, 9, 30, ET_TZ), "VIX expirace"
                )
            )
        month += 1
        if month > 12:
            month, year = 1, year + 1
    markers.sort(key=lambda marker: marker.ts)
    return markers


def expiry_calendar(today: dt.date) -> ExpiryCalendar:
    expiry = next_quarterly_expiry(today)
    previous = previous_quarterly_expiry(today)
    # VIX expirace v kvartálním týdnu = středa před 3. pátkem NÁSLEDUJÍCÍHO měsíce
    nxt_year, nxt_month = (expiry.year + (expiry.month == 12), expiry.month % 12 + 1)
    return ExpiryCalendar(
        today=today,
        phase=phase_of(today),
        quarterly_expiry=expiry,
        roll_date=roll_date(expiry),
        soq_ts=soq_ts(expiry),
        days_to_expiry=(expiry - today).days,
        is_opex_week=_week_monday(today) == _week_monday(expiry),
        vix_expiry=vix_expiry_before(quarterly_expiry(nxt_year, nxt_month)),
        previous_expiry=previous,
        markers=tuple(calendar_markers(today)),
    )


__all__ = [
    "ROLL_DAYS_BEFORE_EXPIRY",
    "CalendarMarker",
    "ExpiryCalendar",
    "calendar_markers",
    "expiry_calendar",
    "front_contract_eligible",
    "is_quarterly_expiry",
    "next_quarterly_expiry",
    "phase_of",
    "previous_quarterly_expiry",
    "roll_date",
    "third_friday",
    "vix_expiry_before",
]
