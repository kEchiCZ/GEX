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
from typing import Protocol

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

# 1min bary podkladu (pro cenový overlay, spot v OTM/ITM módech a replay)
BARS_SCHEMA = pa.schema(
    [
        ("ts_min", pa.timestamp("us", tz="UTC")),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("volume", pa.float64()),
    ]
)

# Řada flowΔ/CumΔ (SPEC 4.5/5.1: derived/)
FLOW_SCHEMA = pa.schema(
    [
        ("ts_min", pa.timestamp("us", tz="UTC")),
        ("flow_delta", pa.float64()),
        ("cum_delta", pa.float64()),
    ]
)

# Časová řada levels (SPEC 4.2/5.1: derived/ — replay je nečte znovu z raw dat)
LEVELS_SCHEMA = pa.schema(
    [
        ("ts_min", pa.timestamp("us", tz="UTC")),
        ("flip", pa.float64()),
        ("call_wall", pa.float64()),
        ("put_wall", pa.float64()),
        ("centroid", pa.float64()),
        ("total_gex", pa.float64()),
    ]
)

# Dyn GEX profil (ADR-0009, #203): NetGEX přes cenovou mřížku per minuta —
# historie profilů je zároveň levý (naměřený) díl budoucího 2D pole
GEXPROFILE_SCHEMA = pa.schema(
    [
        ("ts_min", pa.timestamp("us", tz="UTC")),
        ("grid_start", pa.float64()),
        ("grid_step", pa.float64()),
        ("values", pa.list_(pa.float64())),
    ]
)

# Modelované Dyn GEX pole (ADR-0009 fáze 2): budoucí sloupce s klesajícím τ.
# Partice drží JEN poslední stav minuty (replace_and_write) — pole je odvoditelné
# a historii „co model kdy tvrdil" nearchivujeme, jen historie profilů je poctivá.
GEXFIELD_SCHEMA = pa.schema(
    [
        ("ts_min", pa.timestamp("us", tz="UTC")),
        ("grid_start", pa.float64()),
        ("grid_step", pa.float64()),
        ("col_start", pa.timestamp("us", tz="UTC")),
        ("col_step_min", pa.int32()),
        ("col_count", pa.int32()),
        ("values", pa.list_(pa.float64())),  # sloupce za sebou: values[col·grid_len + i]
    ]
)

# Sekundární zdi (ADR-0008, #92) — VLASTNÍ řada, ne sloupce v LEVELS_SCHEMA:
# přidání sloupce by rozbilo čtení existujících denních partic
# (pq.read_table(..., schema=...)), stejné omezení jako u barů v ADR-0005
LEVELS2_SCHEMA = pa.schema(
    [
        ("ts_min", pa.timestamp("us", tz="UTC")),
        ("call_wall_2", pa.float64()),
        ("put_wall_2", pa.float64()),
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


class BarLike(Protocol):
    """Strukturální podoba ibkr.underlying.Bar (storage nezávisí na ibkr vrstvě)."""

    @property
    def ts(self) -> dt.datetime: ...

    @property
    def open(self) -> float: ...

    @property
    def high(self) -> float: ...

    @property
    def low(self) -> float: ...

    @property
    def close(self) -> float: ...

    @property
    def volume(self) -> float: ...


class FlowRowLike(Protocol):
    """Strukturální podoba compute.cumdelta.FlowRow (storage nezávisí na compute)."""

    @property
    def ts_min(self) -> dt.datetime: ...

    @property
    def flow_delta(self) -> float: ...

    @property
    def cum_delta(self) -> float: ...


@dataclass(frozen=True)
class LevelsRow:
    """Levels jedné minuty pro časovou řadu v derived/ (SPEC 4.2)."""

    ts_min: dt.datetime
    flip: float | None
    call_wall: float | None
    put_wall: float | None
    centroid: float | None
    total_gex: float


@dataclass(frozen=True)
class Levels2Row:
    """Sekundární zdi jedné minuty (ADR-0008, #92) — None = druhá zeď není."""

    ts_min: dt.datetime
    call_wall_2: float | None
    put_wall_2: float | None


@dataclass(frozen=True)
class GexProfileRow:
    """Dyn GEX profil jedné minuty (ADR-0009): NetGEX $/bod na cenové mřížce."""

    ts_min: dt.datetime
    grid_start: float
    grid_step: float
    values: list[float]


@dataclass(frozen=True)
class GexFieldRow:
    """Modelované Dyn GEX pole (ADR-0009 fáze 2) — jen poslední stav minuty.

    `values` jsou sloupce za sebou: values[col · grid_len + i]."""

    ts_min: dt.datetime
    grid_start: float
    grid_step: float
    col_start: dt.datetime
    col_step_min: int
    col_count: int
    values: list[float]


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

    def append_and_write(self, rows: Sequence[dict[str, object]], key: str | None = None) -> Path:
        """Přidá řádky a přepíše partici; s `key` nahradí řádky téhož klíče (upsert).

        Upsert potřebují bary podkladu: provizorní bar rozdělané minuty se příštím
        cyklem nahrazuje finálním a slepý append by nechal dva řádky téže minuty
        (ADR-0005).
        """
        self._ensure_loaded()
        if key is not None:
            incoming = {row[key] for row in rows}
            if incoming:
                self._rows = [row for row in self._rows if row[key] not in incoming]
        self._rows.extend(rows)
        if key is not None:
            self._rows.sort(key=lambda row: row[key])  # type: ignore[arg-type,return-value]
        return self._write()

    def replace_and_write(self, rows: Sequence[dict[str, object]]) -> Path:
        """Nahradí CELÝ obsah partice — řady typu „jen poslední stav" (gexfield).

        Předchozí obsah se nenačítá: po restartu enginu je starý stav bezcenný,
        první cyklus ho přepíše čerstvým polem.
        """
        self._loaded = True
        self._rows = list(rows)
        return self._write()

    def _write(self) -> Path:
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

    def write_levels(
        self, symbol: str, expiry: str, day: dt.date, rows: Sequence[LevelsRow]
    ) -> Path:
        """Přidá levels minuty do partice derived/{sym}/{expiry}/levels/{date}.parquet.

        Typ řady je adresář (ne prefix v názvu), aby RetentionJob uměl z názvu
        souboru přečíst datum partice.
        """
        path = (
            self._settings.derived_dir / symbol / expiry / "levels" / f"{day.isoformat()}.parquet"
        )
        buffer = self._buffer(path, LEVELS_SCHEMA)
        return buffer.append_and_write([asdict(row) for row in rows])

    def write_levels2(
        self, symbol: str, expiry: str, day: dt.date, rows: Sequence[Levels2Row]
    ) -> Path:
        """Přidá sekundární zdi minuty do partice derived/{sym}/{exp}/levels2 (ADR-0008)."""
        path = (
            self._settings.derived_dir / symbol / expiry / "levels2" / f"{day.isoformat()}.parquet"
        )
        buffer = self._buffer(path, LEVELS2_SCHEMA)
        return buffer.append_and_write([asdict(row) for row in rows])

    def write_gexprofile(
        self, symbol: str, expiry: str, day: dt.date, rows: Sequence[GexProfileRow]
    ) -> Path:
        """Přidá Dyn GEX profil minuty do derived/{sym}/{exp}/gexprofile (ADR-0009)."""
        path = (
            self._settings.derived_dir
            / symbol
            / expiry
            / "gexprofile"
            / f"{day.isoformat()}.parquet"
        )
        buffer = self._buffer(path, GEXPROFILE_SCHEMA)
        return buffer.append_and_write([asdict(row) for row in rows])

    def write_gexfield(self, symbol: str, expiry: str, day: dt.date, row: GexFieldRow) -> Path:
        """Přepíše modelované pole v derived/{sym}/{exp}/gexfield — jen poslední stav."""
        path = (
            self._settings.derived_dir / symbol / expiry / "gexfield" / f"{day.isoformat()}.parquet"
        )
        buffer = self._buffer(path, GEXFIELD_SCHEMA)
        return buffer.replace_and_write([asdict(row)])

    def write_bars(self, symbol: str, day: dt.date, bars: Sequence[BarLike]) -> Path:
        """Zapíše 1min bary podkladu do partice derived/{sym}/bars/{date}.parquet.

        Upsert podle `ts_min`: provizorní bar rozdělané minuty (ADR-0005) se
        příštím cyklem nahradí finálním, ne zdvojí.
        """
        path = self._settings.derived_dir / symbol / "bars" / f"{day.isoformat()}.parquet"
        buffer = self._buffer(path, BARS_SCHEMA)
        return buffer.append_and_write(
            [
                {
                    "ts_min": bar.ts,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                }
                for bar in bars
            ],
            key="ts_min",
        )

    def write_flow(self, symbol: str, day: dt.date, rows: Sequence[FlowRowLike]) -> Path:
        """Přidá flowΔ/CumΔ minuty do partice derived/{sym}/flow/{date}.parquet."""
        path = self._settings.derived_dir / symbol / "flow" / f"{day.isoformat()}.parquet"
        buffer = self._buffer(path, FLOW_SCHEMA)
        return buffer.append_and_write(
            [
                {"ts_min": row.ts_min, "flow_delta": row.flow_delta, "cum_delta": row.cum_delta}
                for row in rows
            ]
        )

    def _buffer(self, path: Path, schema: pa.Schema) -> _PartitionBuffer:
        buffer = self._buffers.get(path)
        if buffer is None:
            buffer = _PartitionBuffer(path, schema)
            self._buffers[path] = buffer
        return buffer
