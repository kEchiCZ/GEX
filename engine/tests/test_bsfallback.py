"""Testy hlídky BS fallback greeks (#877): epizody, tlumení, návrat."""

from gexlens_engine.compute.bsfallback import (
    COOLDOWN_S,
    MIN_DURATION_S,
    BsFallbackWatcher,
    episode_seconds,
)


def test_kratky_blip_nealertuje() -> None:
    """Restart TWS (blip 23. 8. 21:02–21:32 s mezerami) nesmí spouštět zvonek."""
    watcher = BsFallbackWatcher(symbol="ES")
    assert watcher.observe(bs_count=80, total=100, now=0.0) is None
    assert watcher.observe(bs_count=90, total=100, now=300.0) is None
    # Návrat pod práh před MIN_DURATION — epizoda se ruší bez recovery zprávy
    assert watcher.observe(bs_count=0, total=100, now=600.0) is None
    assert episode_seconds(watcher, 600.0) is None


def test_epizoda_alertuje_jednou_a_pripomene_po_cooldownu() -> None:
    watcher = BsFallbackWatcher(symbol="NQ")
    assert watcher.observe(bs_count=60, total=100, now=0.0) is None
    message = watcher.observe(bs_count=62, total=100, now=MIN_DURATION_S + 60.0)
    assert message is not None and "NQ" in message and "62%" in message
    # Další cykly mlčí až do cooldownu
    assert watcher.observe(bs_count=62, total=100, now=MIN_DURATION_S + 120.0) is None
    reminder = watcher.observe(bs_count=62, total=100, now=MIN_DURATION_S + 60.0 + COOLDOWN_S)
    assert reminder is not None


def test_navrat_po_alertu_ohlasi_konec_a_znovu_se_natahne() -> None:
    watcher = BsFallbackWatcher(symbol="ES")
    watcher.observe(bs_count=50, total=100, now=0.0)
    assert watcher.observe(bs_count=50, total=100, now=MIN_DURATION_S + 1.0) is not None
    recovery = watcher.observe(bs_count=0, total=100, now=MIN_DURATION_S + 300.0)
    assert recovery is not None and "vrátil" in recovery
    # Nová epizoda po návratu alertuje znovu (re-arm)
    watcher.observe(bs_count=50, total=100, now=10_000.0)
    assert watcher.observe(bs_count=50, total=100, now=10_000.0 + MIN_DURATION_S + 1.0) is not None


def test_status_fields_a_prazdny_cyklus() -> None:
    watcher = BsFallbackWatcher(symbol="ES")
    watcher.observe(bs_count=0, total=0, now=0.0)  # bez snapshotů — podíl 0, žádný alert
    assert watcher.status_fields() == {"share": 0.0}
    watcher.observe(bs_count=25, total=100, now=1.0)
    fields = watcher.status_fields()
    assert fields["share"] == 0.25 and fields["episode"] is True
