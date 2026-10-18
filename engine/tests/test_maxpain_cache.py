"""Max Pain se přepočítá při každé změně archivu OI (#826).

Cache klíčovaná jen na (expirace, den) zamrzla na prvním ranním načtení,
kdy archiv teprve dobíhá CME publikaci. NQ 24. 8.: skutečný Max Pain se
posunul 29200 → 29400 → 29390, tendence i setupy držely celý den jednu
hodnotu z prvního načtení.
"""

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from gexlens_engine.setups import SetupEngine
from gexlens_engine.storage.oi_archive import OIEodRepository, OIRecord
from gexlens_engine.tendency import TendencyEngine

DAY = dt.date(2026, 8, 24)
EXPIRY = "20260824"


def repo(tmp_path: Path) -> OIEodRepository:
    repository = OIEodRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'oi.sqlite'}"))
    repository.ensure_schema()
    return repository


def write(repository: OIEodRepository, oi: dict[tuple[float, str], float], at: dt.datetime) -> None:
    repository.upsert_many(
        [
            OIRecord("NQ", EXPIRY, strike, right, DAY, value)
            for (strike, right), value in oi.items()
        ],
        captured_ts=at,
    )


def test_doplneni_oi_behem_dne_posune_max_pain(tmp_path: Path) -> None:
    repository = repo(tmp_path)
    morning = dt.datetime(2026, 8, 24, 8, 30, tzinfo=dt.UTC)
    # Ranní neúplný archiv: put masa nahoře přebíjí malé call OI dole,
    # takže nejlevnější settle je u horního striku
    write(repository, {(29000.0, "C"): 100.0, (29400.0, "P"): 300.0}, morning)

    engine = TendencyEngine.__new__(TendencyEngine)  # bez plné konstrukce
    engine.symbol = "NQ"
    engine.oi_repository = repository
    engine._max_pain = None
    engine._max_pain_loaded_for = None

    engine._refresh_max_pain(EXPIRY, DAY)
    assert engine._max_pain == 29400.0

    # Publikace CME dobíhá: dorazí zbytek call OI dole a snímek se pořídí
    # znovu — těžiště výplaty se překlopí dolů
    write(
        repository,
        {(29000.0, "C"): 500.0, (29400.0, "P"): 300.0},
        dt.datetime(2026, 8, 24, 13, 0, tzinfo=dt.UTC),
    )

    engine._refresh_max_pain(EXPIRY, DAY)

    # Před opravou tady zůstalo 29400 po zbytek dne
    assert engine._max_pain == 29000.0


def test_beze_zmeny_snimku_se_nepocita_znovu(tmp_path: Path) -> None:
    """Cache má pořád držet — přepočet jen když se archiv opravdu změnil."""
    repository = repo(tmp_path)
    at = dt.datetime(2026, 8, 24, 8, 30, tzinfo=dt.UTC)
    write(repository, {(29200.0, "P"): 100.0}, at)

    engine = TendencyEngine.__new__(TendencyEngine)
    engine.symbol = "NQ"
    engine.oi_repository = repository
    engine._max_pain = None
    engine._max_pain_loaded_for = None

    engine._refresh_max_pain(EXPIRY, DAY)
    key_after_first = engine._max_pain_loaded_for
    engine._refresh_max_pain(EXPIRY, DAY)

    assert engine._max_pain_loaded_for == key_after_first
    # Klíč nese verzi archivu expirace: (poslední IBKR zápis, poslední tasty zápis)
    assert key_after_first == (EXPIRY, DAY, (at, None))


@pytest.mark.parametrize("engine_cls", [TendencyEngine, SetupEngine])
def test_obnova_tasty_kridla_posune_max_pain(
    tmp_path: Path, engine_cls: type[TendencyEngine] | type[SetupEngine]
) -> None:
    """#1605: tasty křídla se po publikaci CME obnovují bez změny `captured_ts`
    (IBKR čtení se nehne). Klíč jen na `captured_ts` by Max Pain držel na
    předchozí publikaci — tasty křídla dělají ~55 % Σ OI 0DTE řetězu."""
    repository = repo(tmp_path)
    write(repository, {(29000.0, "C"): 100.0}, dt.datetime(2026, 8, 24, 13, 1, tzinfo=dt.UTC))
    repository.upsert_many(
        [OIRecord("NQ", EXPIRY, 29400.0, "P", DAY, 300.0)],
        tasty_captured_ts=dt.datetime(2026, 8, 24, 0, 0, tzinfo=dt.UTC),
    )
    engine = engine_cls.__new__(engine_cls)  # bez plné konstrukce
    engine.symbol = "NQ"
    engine.oi_repository = repository
    engine._max_pain = None
    engine._max_pain_loaded_for = None

    engine._refresh_max_pain(EXPIRY, DAY)
    assert engine._max_pain == 29400.0

    # Publikace dorazila do tasty Summary: put masa nahoře se rozpustila
    repository.upsert_many(
        [OIRecord("NQ", EXPIRY, 29400.0, "P", DAY, 50.0)],
        tasty_captured_ts=dt.datetime(2026, 8, 24, 12, 5, tzinfo=dt.UTC),
    )
    engine._refresh_max_pain(EXPIRY, DAY)

    assert engine._max_pain == 29000.0
