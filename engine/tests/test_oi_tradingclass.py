"""Testy klíče trading_class v oi_eod (#736) — série se neslévají, čtení Σ."""

import datetime as dt
from pathlib import Path

from sqlalchemy import create_engine

from gexlens_engine.config import Settings
from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.storage.oi_archive import (
    ContractSnapshot,
    OIArchiver,
    OIEodRepository,
    OIRecord,
)

DAY = dt.date(2026, 8, 24)


def repo(tmp_path: Path) -> OIEodRepository:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'oi.sqlite'}")
    repository = OIEodRepository(engine)
    repository.ensure_schema()
    return repository


def test_dve_serie_teze_expirace_maji_vlastni_radky(tmp_path: Path) -> None:
    """AC #736: CardinalityViolation z #215 se nevrací a série se rozlišují."""
    repository = repo(tmp_path)
    repository.upsert_many(
        [
            OIRecord("MES", "20260824", 6400.0, "C", DAY, 100.0, trading_class="E1A"),
            OIRecord("MES", "20260824", 6400.0, "C", DAY, 40.0, trading_class="EX1"),
        ]
    )

    total = repository.get_oi("MES", DAY, 6400.0, "C", expiry="20260824")
    assert total == 140.0  # Σ přes série = chování před #736

    per_class = repository.values_for("MES", "20260824", DAY, trading_class="E1A")
    assert len(per_class) == 1 and per_class[0].oi == 100.0  # datová strana #513


def test_chain_for_day_agreguje_jako_stary_zapis(tmp_path: Path) -> None:
    """Forward GEX: Σ OI, snímek od série s větším OI (zrcadlo write-merge #215)."""
    repository = repo(tmp_path)
    repository.upsert_many(
        [
            OIRecord("MES", "20260824", 6400.0, "C", DAY, 100.0, iv=0.2, trading_class="E1A"),
            OIRecord("MES", "20260824", 6400.0, "C", DAY, 40.0, iv=0.9, trading_class="EX1"),
        ]
    )

    chain = repository.chain_for_day("MES", DAY)

    assert len(chain) == 1
    assert chain[0].oi == 140.0
    assert chain[0].iv == 0.2  # dominantní série (větší OI)


def test_snapshot_a_values_scitaji(tmp_path: Path) -> None:
    repository = repo(tmp_path)
    repository.upsert_many(
        [
            OIRecord("MES", "20260824", 6400.0, "P", DAY, 10.0, trading_class="E1A"),
            OIRecord("MES", "20260824", 6400.0, "P", DAY, 5.0, trading_class="EX1"),
        ]
    )

    assert repository.snapshot("MES", DAY) == {("20260824", 6400.0, "P"): 15.0}
    values = repository.values_for("MES", "20260824", DAY)
    assert len(values) == 1 and values[0].oi == 15.0


def _spec(trading_class: str) -> OptionContractSpec:
    return OptionContractSpec(
        symbol="MES",
        sec_type="FOP",
        expiry="20260824",
        strike=6400.0,
        right="C",
        exchange="CME",
        trading_class=trading_class,
        multiplier="5",
    )


class _Fetcher:
    """OI per série; série mimo mapu nedodá (missing)."""

    def __init__(self, values: dict[str, float]) -> None:
        self._values = values

    async def fetch_snapshot(
        self, spec: OptionContractSpec, timeout_s: float
    ) -> ContractSnapshot | None:
        oi = self._values.get(spec.trading_class or "")
        return None if oi is None else ContractSnapshot(oi=oi)


async def test_prechodovy_den_neuplne_cteni_nechava_souhrn(tmp_path: Path) -> None:
    """Supersede legacy '' řádku smí proběhnout AŽ po úplném per-class čtení.

    Chybějící sesterská série nesmí smazat souhrn (nevratná ztráta jejího Σ
    příspěvku) a snímek nesmí být finální — čtení se má obnovovat dál.
    """
    repository = repo(tmp_path)
    # Předmigrační zápis dne: souhrn E1A 100 + EX1 40 jako jeden '' řádek
    repository.upsert_many([OIRecord("MES", "20260824", 6400.0, "C", DAY, 140.0)])

    # Po nasazení #736: E1A dodá, EX1 vypadne (timeout)
    partial = OIArchiver(repository, _Fetcher({"E1A": 100.0}), Settings())
    result = await partial.archive_day([_spec("E1A"), _spec("EX1")], DAY)

    assert result.missing == (_spec("EX1"),)
    assert result.changed is True  # neúplné čtení nesmí snímek finalizovat
    legacy = repository.values_for("MES", "20260824", DAY, trading_class="")
    assert len(legacy) == 1 and legacy[0].oi == 140.0  # souhrn přežil

    # Další průchod: obě série dodají → úplné čtení souhrn nahradí
    complete = OIArchiver(repository, _Fetcher({"E1A": 100.0, "EX1": 40.0}), Settings())
    await complete.archive_day([_spec("E1A"), _spec("EX1")], DAY)

    assert repository.values_for("MES", "20260824", DAY, trading_class="") == []
    assert repository.get_oi("MES", DAY, 6400.0, "C") == 140.0  # Σ per-class, bez dvojpočtu
