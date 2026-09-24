"""Golden testy gate signálů (sentiment SPEC 6.2 + ADR-0042, #1265/#1267)."""

from gexlens_engine.compute.signal_gate import gate_open, gate_thresholds


def test_gate_requires_samples_and_wilson_lb() -> None:
    assert gate_open(30, 0.51, 6.0)
    assert not gate_open(29, 0.9, 6.0)  # málo vzorků
    assert not gate_open(200, 0.50, 6.0)  # LB přesně 0.50 nestačí
    assert not gate_open(50, None, 6.0)  # bucket bez hit-rate


def test_gate_requires_minimal_effect() -> None:
    """Spolehlivý směr s nulovou reakcí není signál."""
    # NQ OTHER/imp 1 z 23. 9. 2026: n 13 464, LB 0,5004, Ø −0,03 bp
    assert not gate_open(13_464, 0.5004, -0.03)
    assert not gate_open(50, 0.58, 0.99)
    assert gate_open(50, 0.58, 1.0)
    assert gate_open(50, 0.58, -1.0)  # short bucket — rozhoduje velikost


def test_thresholds_for_ui() -> None:
    assert gate_thresholds() == {"min_samples": 30, "wilson_lb": 0.5, "min_effect_bp": 1.0}
