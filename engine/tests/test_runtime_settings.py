"""Testy nastavení laditelných za běhu ze Settings UI (#438)."""

from pathlib import Path

from gexlens_engine.config import Settings
from gexlens_engine.runtime_settings import (
    RUNTIME_SETTINGS,
    RuntimeSetting,
    apply_runtime_settings,
    coerce_setting,
)


def spec_for(key: str) -> RuntimeSetting:
    return next(spec for spec in RUNTIME_SETTINGS if spec.key == key)


def base_settings() -> Settings:
    return Settings(data_dir=Path("data"))


def test_strike_range_bounds_follow_config_invariant() -> None:
    """Strop je polovina strike_range_max_points (max ≥ 2× šířka, ADR-0002)."""
    settings = base_settings()  # max 800 → strop 400
    spec = spec_for("strike_range_points")
    assert coerce_setting(400, spec, settings) == 400.0
    assert coerce_setting("300", spec, settings) == 300.0
    assert coerce_setting(10, spec, settings) == 50.0  # spodní mez
    assert coerce_setting(9999, spec, settings) == 400.0  # strop


def test_batch_size_cannot_exceed_market_data_lines() -> None:
    """Dávka nad kapacitu lines účtu by házela IBKR error 101 (ADR-0001)."""
    settings = base_settings()  # market_data_lines 100
    spec = spec_for("batch_size")
    assert coerce_setting(500, spec, settings) == 100
    assert coerce_setting(1, spec, settings) == 10  # spodní mez
    assert coerce_setting(80, spec, settings) == 80


def test_integer_settings_are_rounded() -> None:
    assert coerce_setting(14.6, spec_for("retention_days"), base_settings()) == 15
    assert isinstance(coerce_setting("30", spec_for("hot_zone_width"), base_settings()), int)


def test_unusable_values_are_rejected() -> None:
    """Bool je v Pythonu podtyp int — True by jinak tiše prošlo jako 1."""
    settings = base_settings()
    spec = spec_for("retention_days")
    assert coerce_setting(True, spec, settings) is None
    assert coerce_setting("nesmysl", spec, settings) is None
    assert coerce_setting({"x": 1}, spec, settings) is None
    assert coerce_setting(float("nan"), spec, settings) is None


def test_apply_changes_values_and_flags_pipeline_restart() -> None:
    settings = base_settings()
    restart = apply_runtime_settings(settings, {"strike_range_points": 300})

    assert settings.strike_range_points == 300.0
    assert restart is True  # obálka se promítá do subskripcí


def test_retention_and_disk_limit_apply_without_restart() -> None:
    """Purge job čte hodnoty až v noci — překlápět kvůli nim pipeline nemá důvod."""
    settings = base_settings()
    restart = apply_runtime_settings(settings, {"retention_days": 30, "disk_limit_gb": 8})

    assert settings.retention_days == 30
    assert settings.disk_limit_gb == 8.0
    assert restart is False


def test_unchanged_and_missing_values_do_nothing() -> None:
    settings = base_settings()
    before = settings.strike_range_points

    assert apply_runtime_settings(settings, {}) is False
    assert apply_runtime_settings(settings, {"strike_range_points": before}) is False
    # Nepoužitelná hodnota nesmí přepsat současnou konfiguraci
    assert apply_runtime_settings(settings, {"strike_range_points": "nesmysl"}) is False
    assert settings.strike_range_points == before


def test_out_of_range_value_is_clamped_not_ignored() -> None:
    """UI hodnotu neomezuje tvrdě — engine ji musí srovnat, ne zahodit."""
    settings = base_settings()
    apply_runtime_settings(settings, {"hot_zone_width": 999})
    assert settings.hot_zone_width == 50
