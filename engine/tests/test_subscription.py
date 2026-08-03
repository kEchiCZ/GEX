"""Testy hlídání chyb subskripce market data (#417)."""

from dataclasses import dataclass

from gexlens_engine.ibkr.subscription import (
    MAX_ALERT_CONTRACTS,
    SubscriptionErrorTracker,
    contract_label,
)


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
