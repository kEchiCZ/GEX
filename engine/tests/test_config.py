"""Testy konfigurační vrstvy (issue #3): defaulty, override z env, odmítnutí nevalidních hodnot."""

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
    assert s.hot_zone_width == 15
    assert s.retention_days == 14
    assert s.snapshots_dir == s.data_dir / "snapshots"
    assert s.ticks_dir == s.data_dir / "ticks"
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


def test_multiple_errors_reported_together(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEXLENS_IBKR_PORT", "0")
    monkeypatch.setenv("GEXLENS_RETENTION_DAYS", "-3")
    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    message = str(excinfo.value)
    assert "GEXLENS_IBKR_PORT" in message
    assert "GEXLENS_RETENTION_DAYS" in message
