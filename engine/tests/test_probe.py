"""Aktivní IBKR sonda (#517 fáze B) — akceptační kritéria nad mockem."""

import asyncio

from gexlens_engine.ibkr.probe import FarmProbe, ProbeReport


class Harness:
    """Vstřikované závislosti sondy s počítadly volání."""

    def __init__(
        self,
        *,
        delivered: bool = True,
        probe_error: Exception | None = None,
        resubscribe_ok: bool = True,
        lines_free: int | None = None,
    ) -> None:
        self.delivered = delivered
        self.probe_error = probe_error
        self.resubscribe_ok = resubscribe_ok
        self.lines_free = lines_free
        self.probes = 0
        self.resubscribes = 0
        self.now = 0.0

    async def snapshot_probe(self) -> bool:
        self.probes += 1
        if self.probe_error is not None:
            raise self.probe_error
        return self.delivered

    async def resubscribe(self) -> bool:
        self.resubscribes += 1
        return self.resubscribe_ok

    def probe(self, **kwargs: object) -> FarmProbe:
        return FarmProbe(
            self.snapshot_probe,
            self.resubscribe,
            lines_free=(lambda: self.lines_free) if self.lines_free is not None else None,
            clock=lambda: self.now,
            **kwargs,  # type: ignore[arg-type]
        )


async def test_farma_ok_znamena_mrtve_subskripce_a_cilenou_obnovu() -> None:
    """AC: sonda OK + stale subskripce → mrtvé subskripce → cílená resubskripce."""
    harness = Harness(delivered=True)
    probe = harness.probe()

    report = await probe.trigger("ibkr_suspect po 3 min")

    assert report is not None
    assert report.outcome == "subscriptions_dead"
    assert harness.resubscribes == 1
    assert "obnova subskripcí proběhla" in report.message


async def test_mrtva_farma_bez_resubskripcni_boure() -> None:
    """AC: sonda KO → výpadek farmy → alert BEZ bouře resubskripcí."""
    harness = Harness(delivered=False)
    probe = harness.probe()

    report = await probe.trigger("ibkr_suspect")

    assert report is not None
    assert report.outcome == "farm_dead"
    assert harness.resubscribes == 0
    assert "Resubskripce se nespouští" in report.message


async def test_chyba_snapshotu_se_pocita_jako_mrtva_farma() -> None:
    """ConnectionError cestou k datům nese tutéž informaci jako mlčící snapshot."""
    harness = Harness(probe_error=ConnectionError("Not connected"))
    probe = harness.probe()

    report = await probe.trigger("ibkr_suspect")

    assert report is not None
    assert report.outcome == "farm_dead"
    assert harness.resubscribes == 0


async def test_timeout_snapshotu_je_mrtva_farma() -> None:
    harness = Harness()
    probe = FarmProbe(
        lambda: asyncio.sleep(60, result=True),  # data nikdy nedorazí
        harness.resubscribe,
        timeout_s=0.01,
        clock=lambda: harness.now,
    )

    report = await probe.trigger("ibkr_suspect")

    assert report is not None
    assert report.outcome == "farm_dead"
    assert harness.resubscribes == 0


async def test_single_flight_zahodi_soubeh() -> None:
    """Dva triggery naráz = jedna sonda; druhý se zahodí, nečeká."""
    harness = Harness(delivered=True)
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_probe() -> bool:
        started.set()
        await release.wait()
        return True

    probe = FarmProbe(slow_probe, harness.resubscribe, clock=lambda: harness.now)

    first = asyncio.create_task(probe.trigger("prvni"))
    await started.wait()
    second = await probe.trigger("druhy behem prvniho")
    release.set()

    assert second is None
    report = await first
    assert report is not None and report.outcome == "subscriptions_dead"
    assert harness.resubscribes == 1


async def test_cooldown_zahodi_druhy_trigger() -> None:
    """Pojistka nad cooldownem alertu fáze A — sonda neběží častěji než interval."""
    harness = Harness(delivered=True)
    probe = harness.probe(min_interval_s=600.0)

    assert await probe.trigger("prvni") is not None
    harness.now = 300.0
    assert await probe.trigger("v cooldownu") is None
    harness.now = 700.0
    assert await probe.trigger("po cooldownu") is not None
    assert harness.probes == 2


async def test_bez_rezervy_linek_se_sonda_nepousti() -> None:
    """Sonda nesmí být poslední kapkou přes strop 100 lines (ADR-0001)."""
    harness = Harness(delivered=True, lines_free=1)
    probe = harness.probe(lines_headroom=2)

    report = await probe.trigger("ibkr_suspect")

    assert report is not None
    assert report.outcome == "skipped"
    assert harness.probes == 0
    assert harness.resubscribes == 0


async def test_selhana_obnova_se_prizna_v_hlaseni() -> None:
    """Selhání resubscribe řeší ConnectionManager (disconnect → reconnect);
    sonda ho jen poctivě pojmenuje."""
    harness = Harness(delivered=True, resubscribe_ok=False)
    probe = harness.probe()

    report = await probe.trigger("ibkr_suspect")

    assert report is not None
    assert report.outcome == "subscriptions_dead"
    assert "spojení se přepojuje" in report.message


async def test_last_drzi_posledni_report() -> None:
    harness = Harness(delivered=False)
    probe = harness.probe()

    assert probe.last is None
    report = await probe.trigger("ibkr_suspect")
    assert isinstance(report, ProbeReport)
    assert probe.last is report
