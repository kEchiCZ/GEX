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


# ── Remediace (#877 varianta C) ──────────────────────────────────────


def test_remediace_dva_pokusy_s_rozestupem() -> None:
    from gexlens_engine.compute.bsfallback import REMEDIATION_AFTER_S

    watcher = BsFallbackWatcher(symbol="NQ")
    watcher.observe(bs_count=90, total=100, now=0.0)
    # Před 30 minutami plného fallbacku žádný zásah
    assert watcher.remediation_due(now=REMEDIATION_AFTER_S - 1.0) is None
    # 1. pokus po 30 min; hned další minutu se neopakuje
    assert watcher.remediation_due(now=REMEDIATION_AFTER_S + 1.0) == 1
    assert watcher.remediation_due(now=REMEDIATION_AFTER_S + 60.0) is None
    # 2. pokus po dalších 30 min; třetí už nikdy (stav pro člověka)
    assert watcher.remediation_due(now=2 * REMEDIATION_AFTER_S + 1.0) == 2
    assert watcher.remediation_due(now=10 * REMEDIATION_AFTER_S) is None


def test_remediace_jen_pri_plnem_fallbacku_a_reset_navratem() -> None:
    from gexlens_engine.compute.bsfallback import REMEDIATION_AFTER_S

    watcher = BsFallbackWatcher(symbol="ES")
    # 50 % je epizoda (alertuje), ale remediace ne — částečný fallback
    watcher.observe(bs_count=50, total=100, now=0.0)
    assert watcher.remediation_due(now=REMEDIATION_AFTER_S + 1.0) is None
    # Plný fallback → pokus; návrat pod práh resetuje počítadlo
    watcher.observe(bs_count=90, total=100, now=REMEDIATION_AFTER_S + 2.0)
    assert watcher.remediation_due(now=2 * REMEDIATION_AFTER_S + 3.0) == 1
    watcher.observe(bs_count=0, total=100, now=2 * REMEDIATION_AFTER_S + 60.0)
    watcher.observe(bs_count=90, total=100, now=100_000.0)
    assert watcher.remediation_due(now=100_000.0 + REMEDIATION_AFTER_S + 1.0) == 1


# ── Zavřený trh (#1309, AGENTS.md „Zavřený trh = žádná upozornění…“) ──


def test_zavreny_trh_neohlasi_ani_neremediuje() -> None:
    """Mimo seanci TWS model greeks nepočítá — vysoký podíl BS není porucha.
    Epizoda se nepočítá, alert ani remediace nepřijdou, podíl pro /status žije."""
    from gexlens_engine.compute.bsfallback import REMEDIATION_AFTER_S

    watcher = BsFallbackWatcher(symbol="NQ")
    for minute in range(0, int(3 * REMEDIATION_AFTER_S), 60):
        assert watcher.observe(bs_count=62, total=62, now=float(minute), market_closed=True) is None
        assert watcher.remediation_due(now=float(minute)) is None
    assert episode_seconds(watcher, 3 * REMEDIATION_AFTER_S) is None
    assert watcher.status_fields() == {"share": 1.0}


def test_zavreny_trh_hodiny_epizody_pozastavi() -> None:
    """Epizoda z pátku se přes víkend nepočítá, ale ani nekončí: po nedělním
    otevření připomínka až po MIN_DURATION_S otevřeného trhu (první sweepy
    jedou z BS, než TWS model naběhne) a s délkou = součet otevřených úseků."""
    watcher = BsFallbackWatcher(symbol="ES")
    watcher.observe(bs_count=90, total=100, now=0.0)
    first = watcher.observe(bs_count=90, total=100, now=MIN_DURATION_S + 60.0)
    assert first is not None and "už 16 min." in first
    # Páteční uzávěrka → víkend (zavřeno): hodiny stojí, žádná zpráva
    closed_at = MIN_DURATION_S + 120.0
    assert watcher.observe(bs_count=90, total=100, now=closed_at, market_closed=True) is None
    assert watcher.observe(bs_count=90, total=100, now=100_000.0, market_closed=True) is None
    assert episode_seconds(watcher, 100_000.0) is None
    reopen = 200_000.0
    assert watcher.observe(bs_count=90, total=100, now=reopen) is None
    assert watcher.observe(bs_count=90, total=100, now=reopen + MIN_DURATION_S - 60.0) is None
    again = watcher.observe(bs_count=90, total=100, now=reopen + MIN_DURATION_S)
    # 17 min před zavřením + 15 min po otevření
    assert again is not None and "už 32 min otevřeného trhu" in again


def test_remediace_nejvys_dva_pokusy_i_pres_denni_pauzy() -> None:
    """#877 C: max REMEDIATION_MAX_ATTEMPTS zásahů za EPIZODU. Trvalá porucha
    přes dvě denní pauzy (#862 trvala 29 h) nedostane resubscribe + reconnect
    každý Globex večer — počet pokusů nuluje jen skutečný návrat."""
    from gexlens_engine.compute.bsfallback import REMEDIATION_AFTER_S, REMEDIATION_MAX_ATTEMPTS

    watcher = BsFallbackWatcher(symbol="NQ")
    attempts: list[int] = []
    day_s = 86_400.0
    for day in range(3):
        opened = day * day_s
        # 23 h otevřeného trhu s plným fallbackem, pak hodina denní pauzy
        for minute in range(0, 23 * 60):
            now = opened + 60.0 * minute
            watcher.observe(bs_count=100, total=100, now=now)
            attempt = watcher.remediation_due(now=now)
            if attempt is not None:
                attempts.append(attempt)
        for minute in range(23 * 60, 24 * 60):
            now = opened + 60.0 * minute
            watcher.observe(bs_count=100, total=100, now=now, market_closed=True)
            assert watcher.remediation_due(now=now) is None
    assert attempts == list(range(1, REMEDIATION_MAX_ATTEMPTS + 1))
    # Po skutečném návratu je nová epizoda — pokusy znovu od prvního
    watcher.observe(bs_count=0, total=100, now=4 * day_s)
    watcher.observe(bs_count=100, total=100, now=4 * day_s + 60.0)
    assert watcher.remediation_due(now=4 * day_s + 60.0 + REMEDIATION_AFTER_S) == 1


def test_remediace_po_otevreni_ne_v_prvni_minute() -> None:
    """Pokus rozdělaný před zavřením nepřijde hned po otevření (TWS model
    nabíhá), ale až po REMEDIATION_AFTER_S × pořadí pokusu otevřeného úseku."""
    from gexlens_engine.compute.bsfallback import REMEDIATION_AFTER_S

    watcher = BsFallbackWatcher(symbol="ES")
    watcher.observe(bs_count=100, total=100, now=0.0)
    assert watcher.remediation_due(now=REMEDIATION_AFTER_S + 1.0) == 1
    watcher.observe(bs_count=100, total=100, now=REMEDIATION_AFTER_S + 60.0, market_closed=True)
    reopen = 50_000.0
    watcher.observe(bs_count=100, total=100, now=reopen)
    assert watcher.remediation_due(now=reopen + 60.0) is None
    assert watcher.remediation_due(now=reopen + REMEDIATION_AFTER_S + 60.0) is None
    assert watcher.remediation_due(now=reopen + 2 * REMEDIATION_AFTER_S) == 2


def test_otevreni_po_ohlasene_epizode_ohlasi_navrat() -> None:
    """Epizoda ohlášená v seanci, po otevření greeks chodí → zpráva o návratu."""
    watcher = BsFallbackWatcher(symbol="ES")
    watcher.observe(bs_count=50, total=100, now=0.0)
    assert watcher.observe(bs_count=50, total=100, now=MIN_DURATION_S + 1.0) is not None
    watcher.observe(bs_count=100, total=100, now=MIN_DURATION_S + 60.0, market_closed=True)
    recovery = watcher.observe(bs_count=0, total=100, now=300_000.0)
    assert recovery is not None and "vrátil" in recovery


def test_pokus_dozraly_v_rth_se_nezapocita_a_pocka() -> None:
    """#1315: volající nejdřív zjistí dozrálý pokus (`remediation_pending`) a započítá
    ho až mimo RTH. Dřív se pokus započetl před kontrolou okna a v RTH propadl."""
    from gexlens_engine.compute.bsfallback import REMEDIATION_AFTER_S

    watcher = BsFallbackWatcher(symbol="NQ")
    watcher.observe(bs_count=90, total=100, now=0.0)
    ready = REMEDIATION_AFTER_S + 1.0
    # V RTH: pokus je zralý, ale volající ho nezapočítá — každou minutu znovu zralý
    assert watcher.remediation_pending(now=ready) == 1
    assert watcher.remediation_pending(now=ready + 3600.0) == 1
    # Po konci RTH se teprve započítá; reconnect má vlastní rozestup od resubscribe
    after_rth = ready + 3600.0
    assert watcher.remediation_due(now=after_rth) == 1
    assert watcher.remediation_pending(now=after_rth + 60.0) is None
    assert watcher.remediation_due(now=after_rth + REMEDIATION_AFTER_S) == 2
