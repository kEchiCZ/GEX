"""Kalendář expirací a roll (#1189, ADR-0039): SOQ, roll pravidlo, fáze, značky, alerty."""

import asyncio
import datetime as dt
from pathlib import Path
from typing import Any

from gexlens_engine.compute.expiry_calendar import (
    calendar_markers,
    expiry_calendar,
    front_contract_eligible,
    next_quarterly_expiry,
    phase_of,
    previous_quarterly_expiry,
    roll_date,
)
from gexlens_engine.compute.settle import (
    expiry_settle_ts,
    is_quarterly_expiry,
    quarterly_expiry,
    settle_ts,
    soq_ts,
)
from gexlens_engine.discovery_cache import DiscoveryCache, FrontFuture
from gexlens_engine.expiry_alerts import ExpiryCalendarAlerts, planned_alerts
from gexlens_engine.ibkr.discovery import ExpiryInfo
from gexlens_engine.instruments import expiry_expired, greeks_watch_applies
from gexlens_engine.runtime import PublisherLike

SEP18 = dt.date(2026, 9, 18)


def test_kvartalni_expirace_settluje_v_soq() -> None:
    assert quarterly_expiry(2026, 9) == SEP18 and is_quarterly_expiry(SEP18)
    assert not is_quarterly_expiry(dt.date(2026, 9, 17))
    assert not is_quarterly_expiry(dt.date(2026, 10, 16))  # měsíční, ne kvartální
    # SOQ 9:30 ET = 13:30 UTC v létě; běžná expirace 16:00 ET = 20:00 UTC
    assert soq_ts(SEP18) == dt.datetime(2026, 9, 18, 13, 30, tzinfo=dt.UTC)
    assert expiry_settle_ts(SEP18) == soq_ts(SEP18)
    assert expiry_settle_ts(dt.date(2026, 9, 17)) == settle_ts(dt.date(2026, 9, 17))
    # Prosinec (zimní čas): 9:30 ET = 14:30 UTC
    assert soq_ts(dt.date(2026, 12, 18)) == dt.datetime(2026, 12, 18, 14, 30, tzinfo=dt.UTC)


def test_expirace_propada_po_soq_jen_kvartalni() -> None:
    before = dt.datetime(2026, 9, 18, 13, 0, tzinfo=dt.UTC)
    after = dt.datetime(2026, 9, 18, 13, 31, tzinfo=dt.UTC)
    assert not expiry_expired("20260918", SEP18, before)
    assert expiry_expired("20260918", SEP18, after)
    assert not expiry_expired("20260918", SEP18)  # bez času jen kalendář
    assert expiry_expired("20260917", SEP18)
    # Denní expirace téhož dne po 13:31 UTC nepropadá (settle až 20:00 UTC)
    assert not expiry_expired("20260917", dt.date(2026, 9, 17), after - dt.timedelta(days=1))
    # Greeks hlídka končí v SOQ
    assert greeks_watch_applies("20260918", before) and not greeks_watch_applies("20260918", after)


def test_roll_pravidlo_front_kontraktu() -> None:
    # Roll date 10. 9. (8 dní před 18. 9.): U6 už není front, Z6 ano
    z6 = dt.date(2026, 12, 18)
    assert front_contract_eligible(SEP18, dt.date(2026, 9, 9), 8)
    assert not front_contract_eligible(SEP18, dt.date(2026, 9, 10), 8)
    assert front_contract_eligible(z6, dt.date(2026, 9, 10), 8)
    assert not front_contract_eligible(SEP18, dt.date(2026, 9, 16), 8)
    # roll_days 0 = původní chování: front do dne expirace včetně
    assert front_contract_eligible(SEP18, dt.date(2026, 9, 17), 0)
    assert front_contract_eligible(SEP18, SEP18, 0)
    assert not front_contract_eligible(SEP18, dt.date(2026, 9, 19), 0)


def test_faze_a_kalendar() -> None:
    assert next_quarterly_expiry(dt.date(2026, 9, 16)) == SEP18
    assert next_quarterly_expiry(SEP18) == SEP18
    assert next_quarterly_expiry(dt.date(2026, 9, 19)) == dt.date(2026, 12, 18)
    assert previous_quarterly_expiry(dt.date(2026, 9, 16)) == dt.date(2026, 6, 19)
    assert roll_date(SEP18) == dt.date(2026, 9, 10)
    assert phase_of(dt.date(2026, 9, 8)) == "normal"
    assert phase_of(dt.date(2026, 9, 10)) == "roll"
    assert phase_of(dt.date(2026, 9, 14)) == "opex_week"
    assert phase_of(dt.date(2026, 9, 16)) == "opex_week"
    assert phase_of(SEP18) == "expiry_day"
    assert phase_of(dt.date(2026, 9, 21)) == "post_opex"
    assert phase_of(dt.date(2026, 9, 24)) == "normal"
    calendar = expiry_calendar(dt.date(2026, 9, 16))
    assert calendar.days_to_expiry == 2 and calendar.is_opex_week
    # VIX expirace v kvartálním týdnu: středa 16. 9. (30 dní před 16. 10.)
    assert calendar.vix_expiry == dt.date(2026, 9, 16)
    payload = calendar.as_dict()
    assert payload["phase"] == "opex_week" and payload["soq_ts"] == "2026-09-18T13:30:00+00:00"
    kinds = {(m.kind, m.date) for m in calendar_markers(dt.date(2026, 9, 16))}
    assert ("quarterly_expiry", SEP18) in kinds and ("roll", dt.date(2026, 9, 10)) in kinds
    assert ("vix_expiry", dt.date(2026, 9, 16)) in kinds
    assert ("monthly_opex", dt.date(2026, 10, 16)) in kinds
    assert ("quarterly_expiry", dt.date(2026, 12, 18)) in kinds


def test_discovery_cache_zahodi_front_po_rollu_a_kvartalni_po_soq(tmp_path: Path) -> None:
    cache = DiscoveryCache(tmp_path / "cache.json")
    front = FrontFuture(
        symbol="ES",
        con_id=1,
        exchange="CME",
        multiplier="50",
        last_trade_date="20260918",
        local_symbol="ESU6",
        trading_class="ES",
    )
    cache.store(front, [ExpiryInfo("ES", "20260918", "CME", "50", (7600.0,))])
    today = dt.date(2026, 9, 18)
    before = dt.datetime(2026, 9, 18, 13, 0, tzinfo=dt.UTC)
    after = dt.datetime(2026, 9, 18, 14, 0, tzinfo=dt.UTC)
    assert cache.load("ES", today=today, now=before) is not None
    assert cache.load("ES", today=today, now=after) is None  # kvartální po SOQ
    # Roll okno: 16. 9. má U6 2 dny do expirace → s roll_days 8 neplatí
    assert cache.load("ES", today=dt.date(2026, 9, 16)) is not None
    assert cache.load("ES", today=dt.date(2026, 9, 16), front_roll_days=8) is None


class _Publisher(PublisherLike):
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def status(self, **fields: object) -> None:  # pragma: no cover
        pass

    async def publish(self, channel: str, data: dict[str, object]) -> None:
        self.events.append({"channel": channel, **data})


def test_alerty_roll_opex_tyden_soq_jednou() -> None:
    assert [a.event for a in planned_alerts(dt.date(2026, 9, 10))] == ["roll"]
    assert [a.event for a in planned_alerts(dt.date(2026, 9, 14))] == ["opex_week"]
    assert [a.event for a in planned_alerts(SEP18)] == ["soq"]
    assert planned_alerts(dt.date(2026, 9, 16)) == []
    alerts = ExpiryCalendarAlerts()
    publisher = _Publisher()
    fire = dt.datetime(2026, 9, 18, 13, 36, tzinfo=dt.UTC)
    # Před časem nic; v okně jednou; podruhé ne; po okně (restart) už ne
    assert asyncio.run(alerts.on_minute(fire - dt.timedelta(minutes=10), publisher)) == []
    assert asyncio.run(alerts.on_minute(fire, publisher)) == ["soq"]
    assert asyncio.run(alerts.on_minute(fire + dt.timedelta(minutes=1), publisher)) == []
    late = ExpiryCalendarAlerts()
    assert asyncio.run(late.on_minute(fire + dt.timedelta(hours=2), publisher)) == []
    assert len(publisher.events) == 1
    event = publisher.events[0]
    assert event["kind"] == "expiry_calendar" and "SOQ" in str(event["message"])
