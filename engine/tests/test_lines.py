"""LineGauge (#630): špička obsazených market data lines mezi čteními statusu."""

from gexlens_engine.ibkr.lines import LineGauge


def make_gauge(values: list[int]) -> tuple[LineGauge, list[int]]:
    """Gauge nad předepsanou sekvencí stavů registru; zbytek drží poslední hodnotu."""
    state = {"current": 0}

    def count() -> int:
        if values:
            state["current"] = values.pop(0)
        return state["current"]

    return LineGauge(count), values


def test_take_peak_vraci_spicku_mezi_ctenimi() -> None:
    # Sweep vystoupá na 82, do čtení statusu klesne na 3 trvalé streamy
    gauge, _ = make_gauge([40, 82, 3])
    gauge.sample()
    gauge.sample()
    assert gauge.take_peak() == 82


def test_take_peak_resetuje_na_aktualni_stav() -> None:
    gauge, _ = make_gauge([82, 3, 3])
    gauge.sample()
    assert gauge.take_peak() == 82
    # Další okno už špičku 82 nevidí — začíná od aktuálních 3
    assert gauge.take_peak() == 3


def test_take_peak_bez_sample_cte_okamzity_stav() -> None:
    # Trvalé streamy (spot + realtime bary) se počítají i bez jediného sample
    gauge, _ = make_gauge([4])
    assert gauge.take_peak() == 4


def test_utilization_podil_stropu_a_strop_na_jednicce() -> None:
    gauge, _ = make_gauge([50, 50])
    assert gauge.utilization(100) == 0.5
    gauge_over, _ = make_gauge([120, 120])
    assert gauge_over.utilization(100) == 1.0


def test_utilization_nulovy_strop_neexploduje() -> None:
    gauge, _ = make_gauge([5])
    assert gauge.utilization(0) == 0.0
