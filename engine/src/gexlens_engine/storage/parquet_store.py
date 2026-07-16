"""Parquet SnapshotWriter (SPEC 5.1): denní partice snapshotů a ticků s atomickým zápisem.

Partice: `snapshots/{symbol}/{expiry}/{YYYY-MM-DD}.parquet` (řádek = ts_min × strike × right)
a `ticks/{symbol}/{YYYY-MM-DD}.parquet`. Každý zápis přepíše celou denní partici
přes temp soubor + os.replace — po kill -9 nikdy nezůstane částečný soubor;
maximálně zůstane osiřelý `.tmp`, který se při dalším zápisu uklidí.
"""

import datetime as dt
import logging
import os
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from gexlens_engine.config import Settings

logger = logging.getLogger(__name__)

# Schéma dle SPEC 5.1 — názvy sloupců záměrně přesně kopírují SPEC
SNAPSHOT_SCHEMA = pa.schema(
    [
        ("ts_min", pa.timestamp("us", tz="UTC")),
        ("strike", pa.float64()),
        ("right", pa.string()),
        ("bid", pa.float64()),
        ("ask", pa.float64()),
        ("last", pa.float64()),
        ("volume", pa.float64()),
        ("iv", pa.float64()),
        ("delta", pa.float64()),
        ("gamma", pa.float64()),
        ("theta", pa.float64()),
        ("vega", pa.float64()),
        ("oi", pa.float64()),
        ("stale_age", pa.float64()),
    ]
)

TICKS_SCHEMA = pa.schema(
    [
        ("ts", pa.timestamp("us", tz="UTC")),
        ("conId", pa.int64()),
        ("price", pa.float64()),
        ("size", pa.float64()),
        ("side", pa.string()),
    ]
)


@dataclass(frozen=True)
class SnapshotRow:
    """Jedna buňka 1min konsolidace: kontrakt (strike, right) v čase ts_min."""

    ts_min: dt.datetime
    strike: float
    right: str
    bid: float | None
    ask: float | None
    last: float | None
    volume: float | None
    iv: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    oi: float | None
    stale_age: float


@dataclass(frozen=True)
class TickRecord:
    """Jeden klasifikovaný trade hot zóny (SPEC 5.1: ts, conId, price, size, side)."""

    ts: dt.datetime
    con_id: int
    price: float
    size: float
    side: str


class _PartitionBuffer:
    """Buffer jedné denní partice: drží celý den v paměti a atomicky přepisuje soubor."""

    def __init__(self, path: Path, schema: pa.Schema) -> None:
        self._path = path
        self._schema = schema
        self._rows: list[dict[str, object]] = []
        self._loaded = False

    def append_and_write(self, rows: Sequence[dict[str, object]]) -> Path:
        self._ensure_loaded()
        self._rows.extend(rows)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._cleanup_stale_tmp()
        table = pa.Table.from_pylist(self._rows, schema=self._schema)
        tmp_path = self._path.with_name(f"{self._path.name}.{os.getpid()}.tmp")
        pq.write_table(table, tmp_path)
        os.replace(tmp_path, self._path)  # atomické zveřejnění — nikdy částečný soubor
        return self._path

    def _ensure_loaded(self) -> None:
        """Po restartu enginu uprostřed dne naváže na existující partici."""
        if self._loaded:
            return
        self._loaded = True
        if self._path.exists():
            existing = pq.read_table(self._path, schema=self._schema)
            self._rows = existing.to_pylist()

    def _cleanup_stale_tmp(self) -> None:
        """Uklidí osiřelé .tmp soubory po případném kill -9 předchozího procesu."""
        for stale in self._path.parent.glob(f"{self._path.name}.*.tmp"):
            try:
                stale.unlink()
                logger.warning("Uklizen osiřelý temp soubor po pádu: %s", stale)
            except OSError:
                logger.exception("Nelze uklidit temp soubor %s", stale)


class SnapshotWriter:
    """Zápis 1min snapshotů řetězce a ticků hot zóny do denních Parquet partic."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._buffers: dict[Path, _PartitionBuffer] = {}

    def write_minute(
        self, symbol: str, expiry: str, day: dt.date, rows: Sequence[SnapshotRow]
    ) -> Path:
        """Přidá 1min konsolidaci do partice snapshots/{sym}/{expiry}/{date}.parquet."""
        path = self._settings.snapshots_dir / symbol / expiry / f"{day.isoformat()}.parquet"
        buffer = self._buffer(path, SNAPSHOT_SCHEMA)
        return buffer.append_and_write([asdict(row) for row in rows])

    def write_ticks(self, symbol: str, day: dt.date, ticks: Sequence[TickRecord]) -> Path:
        """Přidá klasifikované trades do partice ticks/{sym}/{date}.parquet."""
        path = self._settings.ticks_dir / symbol / f"{day.isoformat()}.parquet"
        buffer = self._buffer(path, TICKS_SCHEMA)
        rows = [
            {
                "ts": tick.ts,
                "conId": tick.con_id,
                "price": tick.price,
                "size": tick.size,
                "side": tick.side,
            }
            for tick in ticks
        ]
        return buffer.append_and_write(rows)

    def _buffer(self, path: Path, schema: pa.Schema) -> _PartitionBuffer:
        buffer = self._buffers.get(path)
        if buffer is None:
            buffer = _PartitionBuffer(path, schema)
            self._buffers[path] = buffer
        return buffer
