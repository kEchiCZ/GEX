"""Testy hlídání chyb subskripce market data (#417) a konkurenční relace (#451/#495)."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import cast

import pytest
from ib_async import IB

import gexlens_engine.__main__ as engine_main
from gexlens_engine.config import Settings
from gexlens_engine.ibkr.connection import ConnectionManager
from gexlens_engine.ibkr.subscription import (
    MAX_ALERT_CONTRACTS,
    SubscriptionErrorTracker,
    contract_label,
)
from gexlens_engine.runtime import PublisherLike


def tracker(
    threshold: int = 3, window_s: float = 60.0, cooldown_s: float = 900.0
) -> SubscriptionErrorTracker:
    return SubscriptionErrorTracker(threshold=threshold, window_s=window_s, cooldown_s=cooldown_s)


def test_single_error_is_not_an_alert() -> None:
    """Ojedinělá 354 = přechodný výpadek farmy, ne chybějící subskripce."""
    detector = tracker()
    assert detector.observe("ESU6 7500C", "ES", now=0.0) is None
    assert detector.observe("ESU6 7505C", "ES", now=10.0) is None
    assert detector.total == 2


def test_burst_over_threshold_alerts_with_contracts() -> None:
    detector = tracker(threshold=3)
    assert detector.observe("ESU6 7500C", "ES", now=0.0) is None
    assert detector.observe("ESU6 7500C", "ES", now=1.0) is None
    alert = detector.observe("ESU6 7505P", "ES", now=2.0)
    assert alert is not None
    assert alert.count == 3
    assert alert.symbol == "ES"
    # Nejčastější kontrakt první, s počtem výskytů — uživatel vidí, co TWS odmítla
    assert alert.contracts[0] == "ESU6 7500C (2×)"
    assert "ESU6 7500C" in alert.message
    assert "354" in alert.message


def test_events_outside_window_do_not_accumulate() -> None:
    """Rozprostřené výskyty (1/min) práh nepřekročí — přesně dnešní provoz."""
    detector = tracker(threshold=3, window_s=60.0)
    assert detector.observe("ESU6 7500C", "ES", now=0.0) is None
    assert detector.observe("ESU6 7500C", "ES", now=61.0) is None
    assert detector.observe("ESU6 7500C", "ES", now=122.0) is None
    assert detector.total == 3


def test_cooldown_suppresses_repeated_alerts() -> None:
    detector = tracker(threshold=2, cooldown_s=300.0)
    detector.observe("NQU6 28500C", "NQ", now=0.0)
    assert detector.observe("NQU6 28500C", "NQ", now=1.0) is not None
    # Sweep běží dál a chyby chodí — druhý alert až po cooldownu
    assert detector.observe("NQU6 28500C", "NQ", now=2.0) is None
    assert detector.observe("NQU6 28500C", "NQ", now=299.0) is None
    assert detector.observe("NQU6 28500C", "NQ", now=302.0) is not None


def test_alert_caps_contract_list() -> None:
    """Sweep odmítne desítky strikes — alert vypíše jen prvních pár a zbytek shrne."""
    detector = tracker(threshold=1, cooldown_s=0.0)
    alert = None
    for index in range(MAX_ALERT_CONTRACTS + 3):
        alert = detector.observe(f"ESU6 {7500 + index}C", "ES", now=float(index))
    assert alert is not None
    assert len(alert.contracts) == MAX_ALERT_CONTRACTS
    assert alert.hidden_contracts == 3
    assert "a 3 dalších" in alert.message


def test_dominant_symbol_wins() -> None:
    detector = tracker(threshold=3)
    detector.observe("ESU6 7500C", "ES", now=0.0)
    detector.observe("NQU6 28500C", "NQ", now=1.0)
    alert = detector.observe("NQU6 28505C", "NQ", now=2.0)
    assert alert is not None
    assert alert.symbol == "NQ"


# ── Konkurenční relace (#451/#495) ───────────────────────────────────


def test_competing_session_prah_zachyti_realnou_cetnost() -> None:
    """#495: 10197 chodí ~2× za minutu — defaultní práh (2/120 s) ho musí naplnit.

    Sdílený `subscription_error_threshold` (5/60 s) při reálné kadenci nešel
    nikdy překročit a alert `competing_session` se neodpálil.
    """
    settings = Settings()
    detector = SubscriptionErrorTracker(
        threshold=settings.competing_session_threshold,
        window_s=settings.competing_session_window_s,
        cooldown_s=3600.0,
    )

    assert detector.observe("ES @CME", "ES", now=0.0) is None
    alert = detector.observe("ES @CME", "ES", now=30.0)  # reálná kadence: à ~30 s

    assert alert is not None
    assert alert.symbol == "ES"


class _FakeErrorEvent:
    """Náhrada ib_async errorEvent — jen registrace handlerů a emit."""

    def __init__(self) -> None:
        self.handlers: list[Callable[..., None]] = []

    def __iadd__(self, handler: Callable[..., None]) -> "_FakeErrorEvent":
        self.handlers.append(handler)
        return self

    def emit(self, *args: object) -> None:
        for handler in self.handlers:
            handler(*args)


class _RecordingPublisher(PublisherLike):
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, object]]] = []

    async def status(self, **fields: object) -> None:
        pass

    async def publish(self, channel: str, data: dict[str, object]) -> None:
        self.messages.append((channel, data))


async def test_competing_session_alert_se_odpali_a_nese_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#495: zapojení v `_watch_subscription_errors` při reálné četnosti 2/min
    publikuje alert `competing_session` včetně symbolu (dřív se posílal prázdný)."""
    fake_now = {"t": 0.0}
    monkeypatch.setattr(engine_main, "time", SimpleNamespace(monotonic=lambda: fake_now["t"]))

    ib = SimpleNamespace(errorEvent=_FakeErrorEvent())
    manager = SimpleNamespace(report_error=lambda code, message: None)
    publisher = _RecordingPublisher()
    engine_main._watch_subscription_errors(
        cast(IB, ib),
        cast(ConnectionManager, manager),
        Settings(),
        publisher,
        lambda: True,
    )

    contract = SimpleNamespace(
        symbol="NQ",
        localSymbol="NQU6",
        right="",
        strike=0.0,
        lastTradeDateOrContractMonth="",
        exchange="CME",
    )
    message = "No market data during competing live session"
    ib.errorEvent.emit(1, 10197, message, contract)
    fake_now["t"] = 30.0  # další výskyt za půl minuty — naměřená kadence ze 4. 8.
    ib.errorEvent.emit(2, 10197, message, contract)
    for _ in range(3):  # nech doběhnout create_task s publikací alertu
        await asyncio.sleep(0)

    alerts = [
        data
        for channel, data in publisher.messages
        if channel == "alerts" and data["kind"] == "competing_session"
    ]
    assert len(alerts) == 1
    assert alerts[0]["symbol"] == "NQ"
    assert "přetahuje si market data" in str(alerts[0]["message"])


# ── Popisek kontraktu ────────────────────────────────────────────────


@dataclass
class FakeContract:
    symbol: str = ""
    localSymbol: str = ""
    right: str = ""
    strike: float = 0.0
    lastTradeDateOrContractMonth: str = ""
    exchange: str = ""


def test_contract_label_formats_option() -> None:
    contract = FakeContract(
        symbol="ES",
        localSymbol="ESU6",
        right="C",
        strike=7500.0,
        lastTradeDateOrContractMonth="20260803",
        exchange="CME",
    )
    assert contract_label(contract) == "ESU6 7500C 20260803 @CME"


def test_contract_label_survives_missing_contract() -> None:
    """ib_async kontrakt pro některé reqId nezná — handler nesmí spadnout."""
    assert contract_label(None) == "neznámý kontrakt"
    assert contract_label(FakeContract()) == "neznámý kontrakt"
    assert contract_label(FakeContract(symbol="ES")) == "ES"


# ── Diagnostika a omilostněné okno (#772) ────────────────────────────


def test_window_count_and_recent_records() -> None:
    detector = tracker()
    detector.observe("ESU6 7500C", "ES", now=0.0, wall_now=1000.0)
    detector.observe("ESU6 7505C", "ES", now=1800.0, wall_now=2800.0)
    detector.observe("ESU6 7510C", "ES", now=4000.0, wall_now=5000.0)

    # Hodinové okno: první výskyt (now=0) už vypadl, dva mladší drží
    assert detector.window_count(4000.0) == 2
    assert detector.total == 3
    records = detector.recent_records()
    assert [rec.contract for rec in records] == ["ESU6 7500C", "ESU6 7505C", "ESU6 7510C"]
    assert records[0].ts == 1000.0


def test_excused_burst_does_not_alert_but_is_counted() -> None:
    """Resubskripce nové seance (#772): náraz chyb nesmí naplnit práh alertu."""
    detector = tracker(threshold=3, window_s=60.0)
    detector.excuse(300.0, now=0.0)

    for i in range(10):
        assert detector.observe(f"ESU6 {7500 + 5 * i}C", "ES", now=float(i)) is None

    assert detector.total == 10
    assert detector.excused == 10
    assert detector.window_count(10.0) == 10  # v diagnostice vidět jsou

    # Po konci okna se práh plní normálně — omilostnění není trvalé
    assert detector.observe("ESU6 7600C", "ES", now=301.0) is None
    assert detector.observe("ESU6 7605C", "ES", now=302.0) is None
    alert = detector.observe("ESU6 7610C", "ES", now=303.0)
    assert alert is not None
    assert alert.count == 3  # omilostněné výskyty do prahu nevstoupily
