"""Volatilitní režim z barů (ADR-0028, #713)."""

import datetime as dt
from pathlib import Path

from sqlalchemy import create_engine

from gexlens_engine.compute.volregime import (
    BUCKET_EDGES,
    MIN_SAMPLE,
    VOL_REGIME_VERSION,
    bucket_for,
    compute_regimes,
    percentile_of,
)
from gexlens_engine.storage.volregime_store import VolRegimeRepository

DAY = dt.date(2026, 1, 1)


def ranges(values: list[float], start: dt.date = DAY) -> list[tuple[dt.date, float]]:
    return [(start + dt.timedelta(days=index), value) for index, value in enumerate(values)]


def test_percentile_of_pocita_rovnost_polovinou() -> None:
    """Opakované stejné rozsahy nesmí spadnout všechny do krajní kategorie."""
    assert percentile_of(10.0, []) == 0.0
    assert percentile_of(10.0, [1.0, 2.0, 3.0]) == 1.0
    assert percentile_of(0.5, [1.0, 2.0, 3.0]) == 0.0
    assert percentile_of(2.0, [1.0, 2.0, 3.0]) == (1 + 0.5) / 3


def test_bucket_for_respektuje_hranice() -> None:
    low, normal, elevated = BUCKET_EDGES
    assert bucket_for(0.0) == "low"
    assert bucket_for(low - 0.01) == "low"
    assert bucket_for(low) == "normal"
    assert bucket_for(normal) == "elevated"
    assert bucket_for(elevated) == "crisis"
    assert bucket_for(1.0) == "crisis"


def test_maly_vzorek_se_neurcuje() -> None:
    """Percentil z hrstky dnů je náhoda vydávaná za měření — radši nic."""
    assert compute_regimes(ranges([10.0] * (MIN_SAMPLE - 1)), "ES") == []
    computed = compute_regimes(ranges([10.0] * (MIN_SAMPLE + 5)), "ES")
    assert len(computed) == 5


def test_percentil_nekouka_dopredu() -> None:
    """Den se nesmí hodnotit proti sobě samému (look-ahead)."""
    values = [10.0] * MIN_SAMPLE + [100.0, 10.0]
    computed = compute_regimes(ranges(values), "ES")
    assert len(computed) == 2
    # Výrazně širší den proti klidné historii = crisis
    assert computed[0].bucket == "crisis"
    assert computed[0].session_range == 100.0
    # Následující den je shodný s většinou historie → leží UPROSTŘED škály,
    # ne dole: není nezvykle klidný, je typický (shody se počítají polovinou)
    assert computed[1].bucket == "normal"
    assert computed[1].sample == MIN_SAMPLE + 1


def test_zaporne_a_nulove_rozsahy_se_vynechavaji() -> None:
    values = [10.0] * MIN_SAMPLE + [0.0, 12.0]
    computed = compute_regimes(ranges(values), "ES")
    # Nulový den se přeskočí, poslední se spočítá
    assert [record.session_range for record in computed] == [12.0]


def test_nese_verzi_definice() -> None:
    computed = compute_regimes(ranges([10.0] * (MIN_SAMPLE + 1)), "ES")
    assert computed[0].version == VOL_REGIME_VERSION


def test_repository_upsert_je_idempotentni(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'vol.db'}")
    repo = VolRegimeRepository(engine)
    repo.ensure_schema()
    repo.ensure_schema()

    records = compute_regimes(ranges([10.0] * MIN_SAMPLE + [50.0]), "ES")
    now = dt.datetime(2026, 3, 1, tzinfo=dt.UTC)
    repo.upsert(records[0], now)
    repo.upsert(records[0], now)

    rows = repo.list_for("ES")
    assert len(rows) == 1
    assert rows[0]["bucket"] == "crisis"
    assert rows[0]["symbol"] == "ES"
    assert repo.existing_dates("ES") == {records[0].session_date}
    assert repo.for_session("ES", records[0].session_date) is not None
    assert repo.for_session("ES", dt.date(2000, 1, 1)) is None
