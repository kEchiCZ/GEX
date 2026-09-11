"""Hlídka paměti (#1105): škrcení vzorků, RSS do stavu, tracemalloc za flagem."""

import logging

import pytest

from gexlens_engine.memwatch import MemoryWatch, rss_mb, trace_enabled, trim_enabled


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


def test_note_loguje_hned_bez_skrceni(caplog: pytest.LogCaptureFixture) -> None:
    values = iter([100.0, 120.0])
    watch = MemoryWatch(
        "news-engine", logging.getLogger("test.memwatch"), rss_provider=lambda: next(values)
    )  # noqa: E501
    with caplog.at_level(logging.INFO, logger="test.memwatch"):
        watch.sample()
        assert watch.note("model stats") == 120.0
    assert [r.getMessage() for r in caplog.records][-1] == "news-engine: RSS 120 MB po model stats"


def test_trim_flag_default_zapnuto() -> None:
    assert trim_enabled({}) is True
    assert trim_enabled({"GEXLENS_MALLOC_TRIM": "0"}) is False
    assert trim_enabled({"GEXLENS_MALLOC_TRIM": "off"}) is False
    assert trim_enabled({"GEXLENS_MALLOC_TRIM": "1"}) is True


def test_malloc_trim_meri_vraceny_rss(caplog: pytest.LogCaptureFixture) -> None:
    """Po vzorku se zavolá trim; RSS po něm přepíše last_rss_mb a pokles ≥ 20 MB se loguje."""
    values = iter([1000.0, 700.0, 500.0, 495.0])
    calls = {"n": 0}

    def fake_trim() -> int:
        calls["n"] += 1
        return 1

    clock = {"t": 0.0}
    watch = MemoryWatch(
        "news-engine",
        logging.getLogger("test.memwatch"),
        interval_s=600.0,
        trim=True,
        rss_provider=lambda: next(values),
        clock=lambda: clock["t"],
        malloc_trim=fake_trim,
    )
    with caplog.at_level(logging.INFO, logger="test.memwatch"):
        assert watch.sample() == 700.0
        clock["t"] = 700.0
        assert watch.sample() == 495.0  # pokles 5 MB → bez samostatného řádku
    assert calls["n"] == 2
    assert watch.last_trim_mb == 5.0
    logged = [r.getMessage() for r in caplog.records]
    assert logged == [
        "news-engine: RSS 1000 MB",
        "news-engine: malloc_trim vrátil 300 MB (RSS 1000 → 700 MB)",
        "news-engine: RSS 500 MB",
    ]


def test_malloc_trim_vypnuty_nebo_nedostupny_nic_nevola(
    caplog: pytest.LogCaptureFixture,
) -> None:
    values = iter([100.0, 100.0])
    off = MemoryWatch("x", logging.getLogger("test.memwatch"), rss_provider=lambda: next(values))
    assert off.sample() == 100.0
    assert off.last_trim_mb is None
    # Skutečné načtení glibc: na Linuxu funkce, jinde None — nikdy výjimka
    with caplog.at_level(logging.INFO, logger="test.memwatch"):
        real = MemoryWatch(
            "y", logging.getLogger("test.memwatch"), trim=True, rss_provider=lambda: 1.0
        )
    assert real.sample() == 1.0


def test_malloc_trim_vyjimka_vypne_trim_a_nezhodi_sber(caplog: pytest.LogCaptureFixture) -> None:
    def boom() -> int:
        raise RuntimeError("libc")

    watch = MemoryWatch(
        "z",
        logging.getLogger("test.memwatch"),
        trim=True,
        rss_provider=lambda: 10.0,
        malloc_trim=boom,
    )
    with caplog.at_level(logging.ERROR, logger="test.memwatch"):
        assert watch.sample() == 10.0
    assert watch.last_trim_mb is None
    assert any("malloc_trim selhal" in r.getMessage() for r in caplog.records)
