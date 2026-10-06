"""Údržba na pozadí (#1337): pomalé měření disku nesmí zdržet minutový cyklus."""

import asyncio
import logging
import threading
import time
from pathlib import Path

import pytest

from gexlens_engine.background import BackgroundJob
from gexlens_engine.storage.diskwatch import DiskSnapshot, DiskWatch

#: Simulovaná doba měření přes pomalý bind mount (issue: max 121,7 s)
SLOW_MEASURE_S = 60.0


class SlowDiskWatch(DiskWatch):
    """Měření, které „trvá" SLOW_MEASURE_S — blokuje vlákno, dokud ho test nepustí."""

    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path, None, interval_s=600.0)
        self.release = threading.Event()
        self.calls = 0

    def measure(self, now: float) -> DiskSnapshot:
        self.calls += 1
        # Pojistka: bez uvolnění by vlákno čekalo celých 60 s jako v produkci
        self.release.wait(SLOW_MEASURE_S)
        return super().measure(now)


async def test_pomale_mereni_nezdrzi_cyklus(tmp_path: Path) -> None:
    watch = SlowDiskWatch(tmp_path)
    job: BackgroundJob[DiskSnapshot] = BackgroundJob("Měření disku")

    started = time.monotonic()
    # Tři „minutové cykly" za sebou, zatímco měření pořád běží
    for minute in range(3):
        snap = job.take_result()
        if snap is not None:
            watch.record(snap)
        ts = minute * 60.0
        if not job.running and watch.due(ts):
            assert job.start(watch.measure, ts)
        await asyncio.sleep(0)
    assert time.monotonic() - started < 1.0  # cyklus na měření nečekal
    assert watch.calls == 1  # druhý běh se nepustil, dokud první neskončil
    assert job.running and watch.last is None

    watch.release.set()
    for _ in range(100):
        if not job.running:
            break
        await asyncio.sleep(0.01)
    snap = job.take_result()
    assert snap is not None
    watch.record(snap)
    assert watch.last is snap
    assert job.take_result() is None  # výsledek se vyzvedne jen jednou


async def test_druhy_beh_se_nespusti_dokud_prvni_bezi() -> None:
    release = threading.Event()
    job: BackgroundJob[int] = BackgroundJob("Purge")

    def slow() -> int:
        release.wait(SLOW_MEASURE_S)
        return 1

    assert job.start(slow)
    assert not job.start(slow)
    release.set()
    while job.running:
        await asyncio.sleep(0.01)
    assert job.take_result() == 1
    assert job.start(lambda: 2)  # po doběhu jde spustit znovu
    while job.running:
        await asyncio.sleep(0.01)
    assert job.take_result() == 2


async def test_vyjimka_se_zaloguje(caplog: pytest.LogCaptureFixture) -> None:
    job: BackgroundJob[int] = BackgroundJob("Retention purge")

    def boom() -> int:
        raise OSError("bind mount zmizel")

    caplog.set_level(logging.ERROR, logger="gexlens_engine.background")
    assert job.start(boom)
    while job.running:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0)  # done callback běží v dalším kroku smyčky
    assert job.take_result() is None
    assert any(
        "Retention purge selhal" in rec.getMessage() and rec.exc_info is not None
        for rec in caplog.records
    )
