"""Hlídka paměti (#1105): škrcení vzorků, RSS do stavu, tracemalloc za flagem."""

import logging

import pytest

from gexlens_engine.memwatch import MemoryWatch, rss_mb, trace_enabled


def test_sample_loguje_jednou_za_interval(caplog: pytest.LogCaptureFixture) -> None:
    clock = {"t": 0.0}
    values = iter([100.0, 150.0, 900.0])
    watch = MemoryWatch(
        "engine",
        logging.getLogger("test.memwatch"),
        interval_s=600.0,
        rss_provider=lambda: next(values),
        clock=lambda: clock["t"],
    )
    with caplog.at_level(logging.INFO, logger="test.memwatch"):
        assert watch.sample() == 100.0
        clock["t"] = 300.0
        assert watch.sample() == 150.0  # změřeno, ale nelogováno (do intervalu)
        clock["t"] = 700.0
        assert watch.sample() == 900.0
    logged = [record.getMessage() for record in caplog.records]
    assert logged == ["engine: RSS 100 MB", "engine: RSS 900 MB"]
    assert watch.last_rss_mb == 900.0


def test_rss_bez_procfs_vraci_none_a_nelze() -> None:
    watch = MemoryWatch("x", logging.getLogger("test.memwatch"), rss_provider=lambda: None)
    assert watch.sample() is None
    # Skutečné čtení /proc: na Linuxu číslo, jinde None — nikdy výjimka
    value = rss_mb()
    assert value is None or value > 0


def test_trace_flag_a_top_lines() -> None:
    assert trace_enabled({"GEXLENS_MEMORY_TRACE": "1"}) is True
    assert trace_enabled({"GEXLENS_MEMORY_TRACE": "off"}) is False
    assert trace_enabled({}) is False
    watch = MemoryWatch(
        "x", logging.getLogger("test.memwatch"), trace=True, rss_provider=lambda: 1.0
    )
    try:
        junk = [bytearray(200_000) for _ in range(20)]  # ~4 MB navíc od startu
        lines = watch.top_lines(limit=3)
        assert lines and lines[0].startswith("+")
        assert "test_memwatch.py" in lines[0]
        del junk
    finally:
        import tracemalloc

        tracemalloc.stop()
    untraced = MemoryWatch("y", logging.getLogger("test.memwatch"), rss_provider=lambda: 1.0)
    assert untraced.top_lines() == []
