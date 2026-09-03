"""Testy nastavení laditelných za běhu ze Settings UI (#438)."""

from pathlib import Path

from gexlens_engine.config import Settings
from gexlens_engine.runtime_settings import (
    RUNTIME_SETTINGS,
    RuntimeSetting,
    apply_connection_settings,
    apply_runtime_settings,
    coerce_setting,
    pending_reconnects,
    seed_reconnects,
    should_poll_settings,
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
    assert isinstance(coerce_setting("30", spec_for("batch_size"), base_settings()), int)


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
    apply_runtime_settings(settings, {"retention_days": 99999})
    assert settings.retention_days == 3650


# ── Parametry spojení (#446) ──────────────────────────────────────────


def test_connection_settings_apply_and_request_reconnect() -> None:
    settings = base_settings()
    changed = apply_connection_settings(
        settings, {"ibkr_host": "192.168.1.10", "ibkr_port": 7497, "ibkr_client_id": 5}
    )

    assert changed is True  # volající se musí přepojit
    assert settings.ibkr_host == "192.168.1.10"
    assert settings.ibkr_port == 7497
    assert settings.ibkr_client_id == 5


def test_connection_settings_ignore_empty_host_and_unchanged_values() -> None:
    """Prázdný host by engine poslal připojovat se „nikam"."""
    settings = base_settings()
    before = settings.ibkr_host

    assert apply_connection_settings(settings, {"ibkr_host": "   "}) is False
    assert apply_connection_settings(settings, {"ibkr_port": settings.ibkr_port}) is False
    assert settings.ibkr_host == before


def test_connection_port_is_clamped_to_valid_range() -> None:
    settings = base_settings()
    apply_connection_settings(settings, {"ibkr_port": 99999})
    assert settings.ibkr_port == 65535


# ── Ruční přepojení (#950) ─────────────────────────────────────────


def test_razitko_z_doby_pred_restartem_se_nevyrizuje_podruhe() -> None:
    """Razítko v nastavení přežije restart; engine ho nesmí vyřídit znovu."""
    seen: dict[str, object] = {}
    stored = {"reconnect_request_ibkr": 1000.0}
    seed_reconnects(stored, seen)
    assert pending_reconnects(stored, seen) == []


def test_prvni_pozadavek_po_startu_se_NEZTRATI() -> None:
    """Regrese z živého ověření #950: klíč při startu CHYBĚL, požadavek přišel až
    potom — lazy varianta ho spolkla jako „první spatření" a nepřepojila."""
    seen: dict[str, object] = {}
    seed_reconnects({}, seen)  # start bez razítka
    assert pending_reconnects({"reconnect_request_tasty": 1788210381.0}, seen) == ["tasty"]


def test_zmena_razitka_vyvola_prepojeni() -> None:
    seen: dict[str, object] = {}
    seed_reconnects({"reconnect_request_ibkr": 1000.0}, seen)
    assert pending_reconnects({"reconnect_request_ibkr": 1001.0}, seen) == ["ibkr"]


def test_stejne_razitko_neprepojuje_dokola() -> None:
    """Hodnota v nastavení zůstává navždy — bez porovnání by se přepojovalo každý poll."""
    seen: dict[str, object] = {}
    seed_reconnects({"reconnect_request_tasty": 1000.0}, seen)
    assert pending_reconnects({"reconnect_request_tasty": 1001.0}, seen) == ["tasty"]
    assert pending_reconnects({"reconnect_request_tasty": 1001.0}, seen) == []
    assert pending_reconnects({"reconnect_request_tasty": 1001.0}, seen) == []


def test_oba_zdroje_najednou() -> None:
    seen: dict[str, object] = {}
    seed_reconnects({"reconnect_request_ibkr": 1.0, "reconnect_request_tasty": 1.0}, seen)
    due = pending_reconnects({"reconnect_request_ibkr": 2.0, "reconnect_request_tasty": 2.0}, seen)
    assert sorted(due) == ["ibkr", "tasty"]


def test_chybejici_klic_nic_nedela() -> None:
    seen: dict[str, object] = {}
    seed_reconnects({}, seen)
    assert pending_reconnects({}, seen) == []


def test_seed_zapamatuje_i_chybejici_klic() -> None:
    """Chybějící klíč musí být v `seen` jako None, jinak by se jeho vznik ztratil."""
    seen: dict[str, object] = {}
    seed_reconnects({}, seen)
    assert seen == {"ibkr": None, "tasty": None}


def test_bez_spojeni_se_nastaveni_cte_kazdy_cyklus() -> None:
    """#992: oprava portu v Settings musí platit hned, ne až v k-tém cyklu."""
    # Připojený engine: jen k-tý cyklus nebo NOTIFY
    assert should_poll_settings(0, False, 5, connected=True) is True
    assert should_poll_settings(3, False, 5, connected=True) is False
    assert should_poll_settings(3, True, 5, connected=True) is True
    assert should_poll_settings(5, False, 5, connected=True) is True
    # Odpojený engine: každý cyklus, bez ohledu na čítač
    for cycle in range(1, 5):
        assert should_poll_settings(cycle, False, 5, connected=False) is True
