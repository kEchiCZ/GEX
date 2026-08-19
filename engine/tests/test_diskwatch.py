"""Dohled nad volným místem (#773): prahy, hystereze, cooldown, pole statusu."""

from pathlib import Path

from sqlalchemy import create_engine

from gexlens_engine.storage.diskwatch import DiskSnapshot, DiskWatch

GB = 1024**3


def snapshot(
    ts: float = 0.0,
    *,
    free: float | None = 100.0,
    db: float | None = 1.0,
    top: tuple[tuple[str, int], ...] = (("feed_comparison", int(1.5 * GB)),),
) -> DiskSnapshot:
    return DiskSnapshot(
        ts=ts,
        data_dir_bytes=int(0.5 * GB),
        disk_free_bytes=None if free is None else int(free * GB),
        disk_total_bytes=int(260 * GB),
        db_bytes=None if db is None else int(db * GB),
        top_tables=top,
    )


def watch(tmp_path: Path) -> DiskWatch:
    return DiskWatch(tmp_path, None, warn_free_gb=15.0, crit_free_gb=5.0, db_alert_gb=4.0)


def test_dost_mista_nealertuje(tmp_path: Path) -> None:
    assert watch(tmp_path).evaluate(snapshot(free=100.0)) is None


def test_varovani_pod_prahem_a_zrouti_v_hlasce(tmp_path: Path) -> None:
    """Přesně stav z 19. 8.: D: mělo 19,3 GB → pod prahem 15 GB by přišlo varování
    a v hlášce jsou rovnou největší tabulky, ať se nehledají ručně."""
    alert = watch(tmp_path).evaluate(snapshot(free=12.0))
    assert alert is not None
    assert alert.level == "warning"
    assert "feed_comparison 1.5 GB" in alert.message
    assert "#757" in alert.message


def test_kriticka_uroven_pod_5_gb(tmp_path: Path) -> None:
    alert = watch(tmp_path).evaluate(snapshot(free=4.0))
    assert alert is not None
    assert alert.level == "critical"
    assert "KRITICKY" in alert.message


def test_velka_db_varuje_i_pri_volnem_disku(tmp_path: Path) -> None:
    """C:/WSL z kontejneru změřit nejde — plní ho růst DB, proto vlastní práh."""
    alert = watch(tmp_path).evaluate(snapshot(free=100.0, db=5.0))
    assert alert is not None
    assert alert.level == "warning"
    assert "systémovém C:" in alert.message


def test_trvajici_stav_hlasi_az_po_cooldownu(tmp_path: Path) -> None:
    w = watch(tmp_path)
    assert w.evaluate(snapshot(ts=0.0, free=12.0)) is not None
    assert w.evaluate(snapshot(ts=60.0, free=12.0)) is None  # tentýž stav, žádný spam
    assert w.evaluate(snapshot(ts=7 * 3600.0, free=12.0)) is not None  # po cooldownu znovu


def test_eskalace_prijde_hned_bez_cekani_na_cooldown(tmp_path: Path) -> None:
    w = watch(tmp_path)
    assert w.evaluate(snapshot(ts=0.0, free=12.0)) is not None
    escalated = w.evaluate(snapshot(ts=60.0, free=4.0))
    assert escalated is not None
    assert escalated.level == "critical"


def test_uzdraveni_rearmuje(tmp_path: Path) -> None:
    w = watch(tmp_path)
    assert w.evaluate(snapshot(ts=0.0, free=12.0)) is not None
    assert w.evaluate(snapshot(ts=60.0, free=20.0)) is None  # uzdraveno
    assert w.evaluate(snapshot(ts=120.0, free=12.0)) is not None  # nový výpadek hned


def test_bez_zmereneho_mista_se_nealertuje(tmp_path: Path) -> None:
    """Neznámé číslo není důvod k poplachu — měření selhalo, loguje se."""
    assert watch(tmp_path).evaluate(snapshot(free=None, db=1.0)) is None


def test_tick_meri_jen_v_intervalu(tmp_path: Path) -> None:
    (tmp_path / "soubor.bin").write_bytes(b"x" * 1024)
    w = DiskWatch(tmp_path, None, interval_s=600.0)
    first = w.tick(0.0)
    assert first is not None and first.data_dir_bytes == 1024

    (tmp_path / "dalsi.bin").write_bytes(b"x" * 1024)
    within = w.tick(60.0)
    assert within is first  # v intervalu se vrací poslední snímek, neměří se

    after = w.tick(700.0)
    assert after is not None and after.data_dir_bytes == 2048


def test_status_fields_chybi_do_prvniho_mereni(tmp_path: Path) -> None:
    w = watch(tmp_path)
    assert w.status_fields(5 * GB) == {}

    w.tick(0.0)
    fields = w.status_fields(5 * GB)
    assert fields["disk_limit_bytes"] == 5 * GB
    assert "disk_usage_bytes" in fields
    assert "disk_free_bytes" in fields  # tmp_path je na reálném disku


def test_sqlite_backend_bez_pg_cisel(tmp_path: Path) -> None:
    """Testovací SQLite nemá pg_database_size — měření nesmí spadnout."""
    db = create_engine(f"sqlite+pysqlite:///{tmp_path / 'x.sqlite'}")
    w = DiskWatch(tmp_path, db)
    report = w.measure(0.0)
    assert report.db_bytes is None
    assert report.top_tables == ()
