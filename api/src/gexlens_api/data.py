"""Čtení denních partic pro REST endpoints (SPEC kap. 6).

Repository jen čte, co engine zapsal (snapshots/derived Parquet) — API server
nemá vlastní stav ani zápis.
"""

import datetime as dt
from pathlib import Path

import pandas as pd

from gexlens_engine.config import Settings


class PartitionNotFoundError(FileNotFoundError):
    """Požadovaná denní partice neexistuje → HTTP 404."""


class DataRepository:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def list_symbols(self) -> list[str]:
        return self._list_dirs(self._settings.snapshots_dir)

    def list_expiries(self, symbol: str) -> list[str]:
        return self._list_dirs(self._settings.snapshots_dir / symbol)

    def snapshots(self, symbol: str, expiry: str, day: dt.date) -> pd.DataFrame:
        path = self._settings.snapshots_dir / symbol / expiry / f"{day.isoformat()}.parquet"
        return self._read(path)

    def levels(self, symbol: str, expiry: str, day: dt.date) -> pd.DataFrame:
        path = (
            self._settings.derived_dir / symbol / expiry / "levels" / f"{day.isoformat()}.parquet"
        )
        return self._read(path)

    def flow(self, symbol: str, day: dt.date) -> pd.DataFrame:
        path = self._settings.derived_dir / symbol / "flow" / f"{day.isoformat()}.parquet"
        return self._read(path)

    def bars(self, symbol: str, day: dt.date) -> pd.DataFrame:
        path = self._settings.derived_dir / symbol / "bars" / f"{day.isoformat()}.parquet"
        return self._read(path)

    def _list_dirs(self, root: Path) -> list[str]:
        if not root.exists():
            return []
        return sorted(entry.name for entry in root.iterdir() if entry.is_dir())

    def _read(self, path: Path) -> pd.DataFrame:
        if not path.exists():
            raise PartitionNotFoundError(str(path))
        return pd.read_parquet(path)
