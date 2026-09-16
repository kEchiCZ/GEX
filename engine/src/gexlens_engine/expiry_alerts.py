"""Alerty kalendáře expirací (#1189, ADR-0039): roll, OPEX týden, SOQ.

Tři okamžiky za kvartál, kdy se trh chová jinak a trader to má vědět dřív,
než se to projeví v grafu:

- **roll date** (čt 8 dní před expirací, 9:30 ET): od teď je front kontrakt
  ten další — bary, CumΔ i řetěz aplikace jedou na něm;
- **pondělí OPEX týdne** (8:00 ET): cena tažená hedgingem, sentiment šum,
  v pátek SOQ ráno;
- **po SOQ** (pá 9:30 ET + 5 min): kvartální expirace proběhla, put wall
  zmizel, pondělí bez opční podpory.

Každý alert jednou; po restartu enginu se nevyrábí zpětně (jen v okně
`FIRE_WINDOW` po spouštěcím čase), aby zvonek nedostal totéž dvakrát.
"""

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field

from gexlens_engine.compute.expiry_calendar import (
    expiry_calendar,
    next_quarterly_expiry,
    roll_date,
)
from gexlens_engine.compute.settle import ET_TZ, session_time_utc, soq_ts, trading_session_date
from gexlens_engine.runtime import PublisherLike

FIRE_WINDOW = dt.timedelta(minutes=30)
ALERT_KIND = "expiry_calendar"


@dataclass(frozen=True)
class CalendarAlert:
    event: str
    fire_at: dt.datetime
    message: str


def planned_alerts(today: dt.date) -> list[CalendarAlert]:
    """Alerty, které patří ke dnešnímu kalendářnímu dni (žádné = běžný den)."""
    expiry = next_quarterly_expiry(today)
    calendar = expiry_calendar(today)
    code = {3: "H", 6: "M", 9: "U", 12: "Z"}[expiry.month]
    contract = f"{code}{expiry.year % 10}"
    expiry_txt = f"{expiry.day}. {expiry.month}."
    alerts: list[CalendarAlert] = []
    if today == roll_date(expiry):
        alerts.append(
            CalendarAlert(
                "roll",
                session_time_utc(today, 9, 30, ET_TZ),
                f"Roll futures: od dneška je front kontrakt {contract} (kvartální expirace "
                f"{expiry_txt} 9:30 ET). Bary, CumΔ i řetěz aplikace jedou na novém kontraktu; "
                "objem ve starém dobíhá.",
            )
        )
    if calendar.is_opex_week and today.weekday() == 0:
        alerts.append(
            CalendarAlert(
                "opex_week",
                session_time_utc(today, 8, 0, ET_TZ),
                f"OPEX týden: kvartální expirace {contract} v pátek {expiry_txt} ráno "
                "(SOQ 9:30 ET). "
                "Cena je tažená hedgingem dealerů, ne náladou — sentiment ber jako šum; "
                "po pátku zmizí opční podpora.",
            )
        )
    if today == expiry:
        alerts.append(
            CalendarAlert(
                "soq",
                soq_ts(today) + dt.timedelta(minutes=5),
                f"Kvartální expirace {contract} proběhla (SOQ 9:30 ET): put/call wall expirovaného "
                "řetězu zmizely, odpoledne pinning k velkým strikům, pondělí bez opční podpory.",
            )
        )
    return alerts


@dataclass
class ExpiryCalendarAlerts:
    """Stav odpálených alertů; `on_minute` volá orchestrátor každou minutu."""

    symbol: str = "*"
    clock: Callable[[], dt.datetime] = field(default=lambda: dt.datetime.now(dt.UTC))
    _fired: set[tuple[str, dt.date]] = field(default_factory=set)

    async def on_minute(self, now: dt.datetime, publisher: PublisherLike) -> list[str]:
        """Odpálí alerty dnešního dne, jejichž čas nastal (v okně FIRE_WINDOW)."""
        fired: list[str] = []
        today = trading_session_date(now)
        for alert in planned_alerts(today):
            key = (alert.event, today)
            if key in self._fired:
                continue
            if now < alert.fire_at or now - alert.fire_at > FIRE_WINDOW:
                continue
            self._fired.add(key)
            await publisher.publish(
                "alerts",
                {
                    "kind": ALERT_KIND,
                    "event": alert.event,
                    "symbol": self.symbol,
                    "message": alert.message,
                    "ts": now.timestamp(),
                },
            )
            fired.append(alert.event)
        return fired
