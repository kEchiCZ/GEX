"""Parameter store setupů (#794 fáze 2, ADR-0033): serializace, validace, verze, přepnutí."""

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from gexlens_engine.compute.setups import (
    SETUP_MECHANICS_VERSION,
    SetupParams,
    params_from_dict,
    params_to_dict,
)
from gexlens_engine.config import Settings
from gexlens_engine.runtime import PublisherLike
from gexlens_engine.setups import SetupEngine, setup_params_from_settings
from gexlens_engine.storage.oi_archive import OIEodRepository
from gexlens_engine.storage.setup_params_store import SetupParamsRepository
from gexlens_engine.storage.setups_store import SetupsRepository


class _Publisher(PublisherLike):
    async def status(self, **fields: object) -> None:  # pragma: no cover
        pass

    async def publish(self, channel: str, data: dict[str, object]) -> None:  # pragma: no cover
        pass


def test_params_roundtrip_dict() -> None:
    """Každé pole dataclass projde dictem beze změny; frozenset jako seřazený seznam."""
    params = SetupParams(
        wall_zone=4.5, acceptance_minutes=7, disabled_templates=frozenset({"b", "a"})
    )
    as_dict = params_to_dict(params)
    assert as_dict["wall_zone"] == 4.5
    assert as_dict["acceptance_minutes"] == 7
    assert as_dict["disabled_templates"] == ["a", "b"]
    assert params_from_dict(as_dict) == params
    # Defaulty projdou také (seed store)
    assert params_from_dict(params_to_dict(SetupParams())) == SetupParams()


def test_params_from_dict_validuje() -> None:
    """Neznámý klíč, špatný typ i bool místo čísla = ValueError; chybějící = default."""
    with pytest.raises(ValueError, match="Neznámé parametry"):
        params_from_dict({"wall_zon": 3.0})
    with pytest.raises(ValueError, match="celé číslo"):
        params_from_dict({"acceptance_minutes": 2.5})
    with pytest.raises(ValueError, match="celé číslo"):
        params_from_dict({"acceptance_minutes": True})
    with pytest.raises(ValueError, match="konečné"):
        params_from_dict({"wall_zone": float("nan")})
    with pytest.raises(ValueError, match="seznam řetězců"):
        params_from_dict({"disabled_templates": "divergence_spring"})
    # int do float pole je v pořádku, chybějící klíče berou defaulty
    partial = params_from_dict({"wall_zone": 4})
    assert partial.wall_zone == 4.0 and partial.min_rrr == SetupParams().min_rrr


def test_setup_params_from_settings_mapuje_env() -> None:
    settings = Settings(setup_max_rr=2.5, setup_disabled_templates="gamma_momentum")
    params = setup_params_from_settings(settings)
    assert params.max_rr == 2.5
    assert params.disabled_templates == frozenset({"gamma_momentum"})


def test_repository_verze_append_only(tmp_path: Path) -> None:
    db = create_engine(f"sqlite+pysqlite:///{tmp_path / 'params.sqlite'}")
    repo = SetupParamsRepository(db)
    repo.ensure_schema()
    assert repo.latest() is None and repo.latest_version() is None

    first = repo.save(SetupParams(), note="seed", created_by="engine")
    assert first.version == 1 and first.mechanics_version == SETUP_MECHANICS_VERSION
    second = repo.save(SetupParams(wall_zone=4.0), note="test širší zóny", created_by="ui")
    assert second.version == 2

    latest = repo.latest()
    assert latest is not None and latest.version == 2
    assert latest.params.wall_zone == 4.0 and latest.created_by == "ui"
    assert repo.latest_version() == 2
    # Historie od nejnovější, první verze zůstává (nic se nemaže)
    assert [row.version for row in repo.history()] == [2, 1]
    assert latest.created_ts.tzinfo is not None
    # JSON podoba pro API
    payload = latest.as_dict()
    assert payload["params"]["wall_zone"] == 4.0 and payload["note"] == "test širší zóny"

    with pytest.raises(ValueError, match="poznámku"):
        repo.save(SetupParams(), note="   ", created_by="ui")


def test_setups_dostanou_sloupec_params_version(tmp_path: Path) -> None:
    """ensure_schema doplní `params_version` (idempotentní ALTER) a create ho zapíše."""
    db = create_engine(f"sqlite+pysqlite:///{tmp_path / 'setups.sqlite'}")
    repo = SetupsRepository(db)
    repo.ensure_schema()
    repo.ensure_schema()  # podruhé nic nerozbije
    columns = {col["name"] for col in inspect(db).get_columns("setups")}
    assert "params_version" in columns
    setup_id = repo.create(
        symbol="ES",
        expiry="20260909",
        template="failed_break",
        direction="long",
        created_ts=dt.datetime(2026, 9, 9, 14, 0, tzinfo=dt.UTC),
        entry=6500.0,
        target=6510.0,
        stop=6495.0,
        confidence=55,
        reason="test",
        context={},
        params_version=3,
    )
    rows = repo.list_for("ES")
    assert rows[0]["id"] == setup_id and rows[0]["params_version"] == 3
    # Bez verze (běh bez store) zůstává NULL, ne 0
    repo.create(
        symbol="ES",
        expiry="20260909",
        template="wall_bounce",
        direction="short",
        created_ts=dt.datetime(2026, 9, 9, 14, 1, tzinfo=dt.UTC),
        entry=6500.0,
        target=6490.0,
        stop=6505.0,
        confidence=45,
        reason="test",
        context={},
    )
    assert repo.list_for("ES")[0]["params_version"] is None


def test_setup_engine_apply_params(tmp_path: Path) -> None:
    """Přepnutí verze za běhu: změna vrací True, stejná verze False, cooldowny zůstávají."""
    db = create_engine(f"sqlite+pysqlite:///{tmp_path / 'setups.sqlite'}")
    repo = SetupsRepository(db)
    repo.ensure_schema()
    oi_repo = OIEodRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'oi.sqlite'}"))
    oi_repo.ensure_schema()
    engine = SetupEngine(
        symbol="ES", repository=repo, oi_repository=oi_repo, publisher=_Publisher()
    )
    assert engine.params_version is None
    ts = dt.datetime(2026, 9, 9, 14, 0, tzinfo=dt.UTC)
    engine._last_created["failed_break"] = ts  # noqa: SLF001 — stav, který přepnutí nesmí smazat

    assert engine.apply_params(SetupParams(wall_zone=4.0), version=2)
    assert engine.params.wall_zone == 4.0 and engine.params_version == 2
    assert not engine.apply_params(SetupParams(wall_zone=4.0), version=2)
    assert engine._last_created["failed_break"] == ts  # noqa: SLF001
