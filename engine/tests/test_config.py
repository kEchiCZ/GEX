"""Testy konfigurační vrstvy (issue #3): defaulty, override z env, odmítnutí nevalidních hodnot."""

import datetime as dt
import logging
from pathlib import Path

import pytest

from gexlens_engine.config import ConfigError, Settings, load_settings


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Testy běží v prázdném adresáři, aby je neovlivnil skutečný `.env` vývojáře."""
    monkeypatch.chdir(tmp_path)


def test_defaults() -> None:
    s = Settings()
    assert s.ibkr_host == "127.0.0.1"
    assert s.ibkr_port == 7496
    assert s.market_data_type == 1
    assert s.batch_size == 80
    assert s.retention_days == 90  # ADR-0022 (odchylka od R3)
    assert s.bars_backfill_days == 14  # backfill zůstává nezávislý na retenci
    assert s.snapshots_dir == s.data_dir / "snapshots"
    assert s.derived_dir == s.data_dir / "derived"


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEXLENS_IBKR_PORT", "7497")
    monkeypatch.setenv("GEXLENS_BATCH_SIZE", "40")
    monkeypatch.setenv("GEXLENS_DATA_DIR", "gexdata")
    s = load_settings()
    assert s.ibkr_port == 7497
    assert s.batch_size == 40
    assert s.snapshots_dir == Path("gexdata") / "snapshots"


def test_env_file_loaded(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("GEXLENS_IBKR_PORT=4001\n", encoding="utf-8")
    s = load_settings()
    assert s.ibkr_port == 4001


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("GEXLENS_IBKR_PORT", "99999"),
        ("GEXLENS_IBKR_PORT", "not-a-number"),
        ("GEXLENS_MARKET_DATA_TYPE", "5"),
        ("GEXLENS_RETENTION_DAYS", "0"),
        ("GEXLENS_BATCH_SIZE", "-1"),
        ("GEXLENS_STRIKE_RANGE_EXPAND_THRESHOLD", "1.5"),
        ("GEXLENS_DISK_LIMIT_GB", "0"),
    ],
)
def test_invalid_value_rejected(monkeypatch: pytest.MonkeyPatch, var: str, value: str) -> None:
    monkeypatch.setenv(var, value)
    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    # Chybová hláška musí jmenovat konkrétní proměnnou a zadanou hodnotu
    assert var in str(excinfo.value)
    assert value in str(excinfo.value)


def test_oi_publication_default_je_burzovni_cas() -> None:
    """#511: default 7:00 America/Chicago = 12:00 UTC v létě (dřívější chování), 13:00 v zimě."""
    s = load_settings()
    assert s.oi_publication_hour_utc is None
    assert s.oi_publication_time_local == dt.time(7, 0)
    assert s.oi_publication_tz == "America/Chicago"
    assert s.oi_publication_utc(dt.date(2026, 8, 4)) == dt.datetime(
        2026, 8, 4, 12, 0, tzinfo=dt.UTC
    )
    assert s.oi_publication_utc(dt.date(2026, 1, 15)) == dt.datetime(
        2026, 1, 15, 13, 0, tzinfo=dt.UTC
    )


def test_oi_publication_novy_klic_v_burzovnim_case(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEXLENS_OI_PUBLICATION_TIME_LOCAL", "08:30")
    monkeypatch.setenv("GEXLENS_OI_PUBLICATION_TZ", "America/New_York")
    s = load_settings()
    assert s.oi_publication_utc(dt.date(2026, 7, 17)) == dt.datetime(
        2026, 7, 17, 12, 30, tzinfo=dt.UTC
    )
    assert s.oi_publication_utc(dt.date(2026, 12, 18)) == dt.datetime(
        2026, 12, 18, 13, 30, tzinfo=dt.UTC
    )


def test_oi_publication_stary_klic_funguje_s_deprecation_warningem(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Zpětná kompatibilita (#511): fixní UTC hodina má přednost + warning v logu."""
    monkeypatch.setenv("GEXLENS_OI_PUBLICATION_HOUR_UTC", "12")
    with caplog.at_level(logging.WARNING, logger="gexlens_engine.config"):
        s = load_settings()
    # Fixní hodina platí v létě i v zimě (staré chování)
    assert s.oi_publication_utc(dt.date(2026, 8, 4)) == dt.datetime(
        2026, 8, 4, 12, 0, tzinfo=dt.UTC
    )
    assert s.oi_publication_utc(dt.date(2026, 1, 15)) == dt.datetime(
        2026, 1, 15, 12, 0, tzinfo=dt.UTC
    )
    assert any("GEXLENS_OI_PUBLICATION_HOUR_UTC" in r.message for r in caplog.records)


def test_oi_publication_nevalidni_zona_odmitnuta(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEXLENS_OI_PUBLICATION_TZ", "Mars/Olympus_Mons")
    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    assert "oi_publication_tz" in str(excinfo.value)
    assert "Mars/Olympus_Mons" in str(excinfo.value)


def test_multiple_errors_reported_together(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEXLENS_IBKR_PORT", "0")
    monkeypatch.setenv("GEXLENS_RETENTION_DAYS", "-3")
    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    message = str(excinfo.value)
    assert "GEXLENS_IBKR_PORT" in message
    assert "GEXLENS_RETENTION_DAYS" in message
