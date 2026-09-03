"""Parquet SnapshotWriter (SPEC 5.1): denní partice snapshotů a ticků s atomickým zápisem.

Partice: `snapshots/{symbol}/{expiry}/{YYYY-MM-DD}.parquet` (řádek = ts_min × strike × right)
a `ticks/{symbol}/{YYYY-MM-DD}.parquet`. Každý zápis přepíše celou denní partici
přes temp soubor + os.replace — po kill -9 nikdy nezůstane částečný soubor;
maximálně zůstane osiřelý `.tmp`, který se při dalším zápisu uklidí.
"""

import datetime as dt
import logging
import os
from collections.abc import Iterable, Sequence
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

# Minutový feature log (#796): vstupní vektor setup detektoru per minuta —
# hotová trénovací matice pro samoučící smyčku (#794), nezávislá na věrnosti
# rekonstrukce v scripts/backtest_setups.py. Labels dává tabulka setups.
FEATURES_SCHEMA = pa.schema(
    [
        ("ts", pa.timestamp("us", tz="UTC")),
        ("expiry", pa.string()),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("flip", pa.float64()),
        ("call_wall", pa.float64()),
        ("put_wall", pa.float64()),
        ("max_pain", pa.float64()),
        ("cum_delta", pa.float64()),
        ("call_flow", pa.float64()),
        ("put_flow", pa.float64()),
        ("opt_vol", pa.float64()),
        ("minutes_to_expiry", pa.float64()),
        ("call_wall_dom", pa.float64()),
        ("put_wall_dom", pa.float64()),
        ("gex_regime", pa.string()),
        ("gamma_edge_up", pa.float64()),
        ("gamma_edge_dn", pa.float64()),
        ("atr", pa.float64()),
        ("band_sharpness", pa.float64()),
        ("band_sharpness_pct", pa.float64()),
        ("band_depth", pa.float64()),
        # Verze definice pásmových metrik (#952). Trénovací matice #794
        # nesmí míchat hodnoty z různých definic hloubky.
        ("band_metrics_version", pa.int64()),
    ]
)

# Surové opční trady z dxFeed TimeAndSale (#795): učicí data pro #794/#615.
# `aggressor` je nechaný přesně, jak ho dxFeed poslal (BUY/SELL/UNDEFINED) —
# klasifikaci dělá až konzument (#615), recorder nic neinterpretuje.
TASTY_TRADES_SCHEMA = pa.schema(
    [
        ("ts", pa.timestamp("us", tz="UTC")),
        ("streamer_symbol", pa.string()),
        ("price", pa.float64()),
        ("size", pa.float64()),
        ("aggressor", pa.string()),
        ("spread_leg", pa.bool_()),
        ("eth", pa.bool_()),
    ]
)

# Stínové CumΔ z dxFeed TimeAndSale (#615 fáze 3, shadow) — minutová řada
# per zóna vlastnictví; živé CumΔ se nemění, tohle je měření před přepnutím
DX_FLOW_SCHEMA = pa.schema(
    [
        ("ts_min", pa.timestamp("us", tz="UTC")),
        ("flow_ring", pa.float64()),
        ("cum_ring", pa.float64()),
        ("flow_ring_outright", pa.float64()),
        ("cum_ring_outright", pa.float64()),
        ("flow_hot", pa.float64()),
        ("cum_hot", pa.float64()),
        ("trades", pa.int64()),
        ("spread_trades", pa.int64()),
        ("unknown_side", pa.int64()),
        ("volume", pa.float64()),
        ("spread_volume", pa.float64()),
        ("dropped_no_context", pa.int64()),
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
        # Původ minuty (#617). NULL = živá cesta IBKR (tak vypadají všechny
        # partice před #617), "ibkr" = totéž zapsané výslovně, "tasty_candle" =
        # REKONSTRUOVÁNO z dxFeed Candle po pozdním startu. Doplněná minuta
        # není totéž co změřená a nesmí se tak tvářit — proto sloupec, ne
        # tichý zápis. Staré soubory se čtou dál: pyarrow doplní NULL.
        ("source", pa.string()),
    ]
)

#: Hodnota `source` u minut doplněných backfillem (#617) — UI je podle ní odliší
BAR_SOURCE_RECONSTRUCTED = "tasty_candle"
#: Živá cesta; NULL v starších particích znamená totéž
BAR_SOURCE_LIVE = "ibkr"


def bar_partition_day(ts: dt.datetime) -> dt.date:
    """UTC den partice, do které bar s časem `ts` patří (#1002).

    Jediné místo, kde se pravidlo „partice = UTC den baru" vyslovuje; runtime,
    rekonstrukce i čistící skript ho sdílejí.
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.UTC)
    return ts.astimezone(dt.UTC).date()


# Řada flowΔ/CumΔ (SPEC 4.5/5.1: derived/)
FLOW_SCHEMA = pa.schema(
    [
        ("ts_min", pa.timestamp("us", tz="UTC")),
        ("flow_delta", pa.float64()),
        ("cum_delta", pa.float64()),
        # CVD podkladu (#829): druhá řada téhož panelu — tok ve futures proti
        # opčnímu delta toku výš. NULL = instrument nemá registrovaný streamer
        # podkladu (běh bez tasty větve) nebo partice vznikla před #829.
        ("futures_cvd_delta", pa.float64()),
        ("futures_cvd", pa.float64()),
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

# Forward GEX (#519): pole per budoucí obchodní den — jeden řádek = den.
# Partice drží jen poslední stav (replace_and_write), přepočet po OI archivu.
GEXFORWARD_SCHEMA = pa.schema(
    [
        ("day", pa.string()),  # ISO datum sloupce
        ("grid_start", pa.float64()),
        ("grid_step", pa.float64()),
        ("values", pa.list_(pa.float64())),
        ("dropped_expiries", pa.list_(pa.string())),
        ("dropped_share", pa.float64()),  # NaN = první den (není vůči čemu)
        ("iv_fallback_share", pa.float64()),
        ("computed_ts", pa.timestamp("us", tz="UTC")),
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

# Striky bez OI (#465) — vlastní řada, ať se nemění SNAPSHOT_SCHEMA (ADR-0005).
# `oi = 0.0` ve snapshotu dnes znamená OBOJÍ: „IBKR poslal nulu" i „nedodal nic".
# Ve výpočtech na tom nesejde (nulové OI přispívá nulou), ale graf tím tvrdí
# měření tam, kde žádné není. Řada nese striky, které v archivu chyběly.
OI_MISSING_SCHEMA = pa.schema(
    [
        ("ts_min", pa.timestamp("us", tz="UTC")),
        ("strike", pa.float64()),
        ("right", pa.string()),
    ]
)

# Dopočtené Greeks (#547) — vlastní řada po vzoru oimissing (ADR-0005/0008):
# strike, jehož modelGreeks v dané minutě nedodala TWS a engine je dopočítal
# BS modelem z mid ceny (IV inverzí). Sloupec v SNAPSHOT_SCHEMA by rozbil
# čtení starých partic; nepřítomnost řady = všechny Greeks z TWS modelu.
GREEKS_SOURCE_SCHEMA = pa.schema(
    [
        ("ts_min", pa.timestamp("us", tz="UTC")),
        ("strike", pa.float64()),
        ("right", pa.string()),
    ]
)

# Catch-up minuty (#518, ADR-0024) — první minuta po startu enginu uprostřed
# dne. Kumulativní čítače (denní volume per kontrakt) v ní pokrývají celou dobu
# výpadku, takže přírůstkové odvozeniny (Opt Vol, Δ Flow, okenní analýza #483)
# ji nesmí číst jako minutový obchod. Vlastní řada po vzoru oimissing — sloupec
# v SNAPSHOT_SCHEMA by rozbil čtení starých partic (ADR-0005/ADR-0008).
CATCHUP_SCHEMA = pa.schema(
    [
        ("ts_min", pa.timestamp("us", tz="UTC")),
    ]
)

# Čistý klasifikovaný objem per strana (#232, ADR-0011 fáze 2) — KUMULATIV dne
# k dané minutě (buy − sell v kontraktech, znaménko z klasifikace agresora).
# Zapisují se jen strany s nenulovým netem: řada je vstup ranní kalibrace α
# a zpětné validace směru (znaménko net vs. znaménko ΔOI); nulové řádky by ji
# jen nafukovaly. Vlastní řada, ne sloupec ve SNAPSHOT_SCHEMA (ADR-0005/0008).
NETFLOW_SCHEMA = pa.schema(
    [
        ("ts_min", pa.timestamp("us", tz="UTC")),
        ("strike", pa.float64()),
        ("right", pa.string()),
        ("net_volume", pa.float64()),
    ]
)

# OI odhad z toku (#232): OI_est = max(0, OI_ráno + α·net) — compute/flowoi.py.
# Zapisují se jen strany, kde se odhad LIŠÍ od měřeného OI; frontend při FA
# zdroji přepíše měřenou matici těmito buňkami, zbytek minuty zůstává měřený.
OIEST_SCHEMA = pa.schema(
    [
        ("ts_min", pa.timestamp("us", tz="UTC")),
        ("strike", pa.float64()),
        ("right", pa.string()),
        ("oi_est", pa.float64()),
    ]
)

# GEX žebřík (#244) — proměnný počet příček per minuta → list sloupce
LADDER_SCHEMA = pa.schema(
    [
        ("ts_min", pa.timestamp("us", tz="UTC")),
        ("call_strikes", pa.list_(pa.float64())),
        ("call_shares", pa.list_(pa.float64())),
        ("put_strikes", pa.list_(pa.float64())),
        ("put_shares", pa.list_(pa.float64())),
    ]
)

# Dominance zdí (ADR-0010, #223) — vlastní řada ze stejného důvodu jako levels2
WALLDOM_SCHEMA = pa.schema(
    [
        ("ts_min", pa.timestamp("us", tz="UTC")),
        ("call_wall_dom", pa.float64()),
        ("put_wall_dom", pa.float64()),
        ("call_wall_2_dom", pa.float64()),
        ("put_wall_2_dom", pa.float64()),
    ]
)


# OI zdi (#851) — vlastní řada, protože LEVELS_SCHEMA se rozšiřovat nesmí
# (ADR-0008 bod 2). Je to jiná veličina než gamma zdi: maximum otevřeného
# zájmu, ne maximum NetGEX profilu.
OIWALLS_SCHEMA = pa.schema(
    [
        ("ts_min", pa.timestamp("us", tz="UTC")),
        ("oi_call_wall", pa.float64()),
        ("oi_put_wall", pa.float64()),
        ("oi_call_share", pa.float64()),
        ("oi_put_share", pa.float64()),
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

    @property
    def futures_cvd_delta(self) -> float | None: ...

    @property
    def futures_cvd(self) -> float | None: ...


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
class OiMissingRow:
    """Strike, pro který v dané minutě NEBYLO OI k dispozici (#465).

    Nezaměňovat s OI = 0: tady jde o kontrakt, který archiv vůbec nepokrývá
    (přibyl posunem pásma) nebo jehož OI IBKR nedodal.
    """

    ts_min: dt.datetime
    strike: float
    right: str


@dataclass(frozen=True)
class GreeksSourceRow:
    """Strike s Greeks dopočtenými enginem v dané minutě (#547).

    TWS model je nedodal, kotace ale tekly — hodnoty ve snapshotu jsou vlastní
    BS dopočet z mid ceny, ne měřený model. Frontend je může odlišit.
    """

    ts_min: dt.datetime
    strike: float
    right: str


@dataclass(frozen=True)
class CatchUpRow:
    """Minuta prvního sweepu po startu enginu uprostřed dne (#518, ADR-0024).

    Její kumulativy dohánějí celou dobu výpadku — kdo počítá minutové
    přírůstky, musí ji brát jako první měřenou minutu dne, ne jako obchod.
    """

    ts_min: dt.datetime


@dataclass(frozen=True)
class NetFlowRow:
    """Kumulativní čistý klasifikovaný objem strany k minutě (#232, ADR-0011)."""

    ts_min: dt.datetime
    strike: float
    right: str
    net_volume: float


@dataclass(frozen=True)
class OiEstRow:
    """OI odhad strany k minutě (#232) — jen strany lišící se od měřeného OI."""

    ts_min: dt.datetime
    strike: float
    right: str
    oi_est: float


@dataclass(frozen=True)
class LadderRow:
    """GEX žebřík jedné minuty (#244) — top-N významných striků per strana."""

    ts_min: dt.datetime
    call_strikes: list[float]
    call_shares: list[float]
    put_strikes: list[float]
    put_shares: list[float]


@dataclass(frozen=True)
class WallDomRow:
    """Dominance zdí jedné minuty (ADR-0010, #223) — None = zeď v profilu není."""

    ts_min: dt.datetime
    call_wall_dom: float | None
    put_wall_dom: float | None
    call_wall_2_dom: float | None
    put_wall_2_dom: float | None


@dataclass(frozen=True)
class OiWallsRow:
    """OI zdi jedné minuty (#851) — None = na dané straně není žádné OI.

    Podíl (`*_share`) říká, jak koncentrovaná zeď je: nízká hodnota znamená,
    že je to jen nejvyšší z mnoha srovnatelných striků a nemá cenu ji číst
    jako úroveň.
    """

    ts_min: dt.datetime
    oi_call_wall: float | None
    oi_put_wall: float | None
    oi_call_share: float | None
    oi_put_share: float | None


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


@dataclass(frozen=True)
class TastyTradeRow:
    """Jeden surový TimeAndSale print z dxFeed (#795) — pole 1:1 se schématem."""

    ts: dt.datetime
    streamer_symbol: str
    price: float
    size: float | None
    aggressor: str | None
    spread_leg: bool | None
    eth: bool | None


@dataclass(frozen=True)
class FeatureRow:
    """Jedna minuta feature logu (#796) — pole 1:1 se FEATURES_SCHEMA."""

    ts: dt.datetime
    expiry: str
    open: float
    high: float
    low: float
    close: float
    flip: float | None
    call_wall: float | None
    put_wall: float | None
    max_pain: float | None
    cum_delta: float
    call_flow: float
    put_flow: float
    opt_vol: float
    minutes_to_expiry: float | None
    call_wall_dom: float | None
    put_wall_dom: float | None
    gex_regime: str | None
    gamma_edge_up: float | None
    gamma_edge_dn: float | None
    atr: float | None
    band_sharpness: float | None
    band_sharpness_pct: float | None
    band_depth: float | None
    band_metrics_version: int | None


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

    def write_features(self, symbol: str, day: dt.date, rows: Sequence[FeatureRow]) -> Path:
        """Přidá minuty feature logu do partice derived/{sym}/features/{date}.parquet (#796).

        Upsert per `ts` (vzor barů): restart enginu uprostřed dne by jinak
        catch-up minutou vyrobil duplicitní řádek téže minuty.
        """
        path = self._settings.derived_dir / symbol / "features" / f"{day.isoformat()}.parquet"
        buffer = self._buffer(path, FEATURES_SCHEMA)
        return buffer.append_and_write([asdict(row) for row in rows], key="ts")

    def write_tasty_trades(self, symbol: str, day: dt.date, rows: Sequence[TastyTradeRow]) -> Path:
        """Přidá surové TimeAndSale printy do partice trades/{sym}/{date}.parquet (#795).

        Adresář `trades/` retence nezná vůbec (nechodí do něj) — učicí data
        se nemažou (ADR-0029), jen se počítají do měřeného obsazení disku.
        """
        path = self._settings.trades_dir / symbol / f"{day.isoformat()}.parquet"
        buffer = self._buffer(path, TASTY_TRADES_SCHEMA)
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

    def write_oi_missing(
        self, symbol: str, expiry: str, day: dt.date, rows: Sequence[OiMissingRow]
    ) -> Path | None:
        """Přidá striky bez OI do partice derived/{sym}/{exp}/oimissing (#465).

        Prázdný seznam se nezapisuje — v běžný den řada nevznikne vůbec a její
        existence sama nese informaci, že něco chybělo.
        """
        if not rows:
            return None
        path = (
            self._settings.derived_dir
            / symbol
            / expiry
            / "oimissing"
            / f"{day.isoformat()}.parquet"
        )
        buffer = self._buffer(path, OI_MISSING_SCHEMA)
        return buffer.append_and_write([asdict(row) for row in rows])

    def write_oi_filled(
        self, symbol: str, expiry: str, day: dt.date, rows: Sequence[OiMissingRow]
    ) -> Path | None:
        """Striky s OI doplněným z tasty Summary (#664) — derived/{sym}/{exp}/oifilled.

        Stejný tvar řádku i princip jako oimissing: řada existuje, jen když
        fill něco doplnil. Bez ní by tasty hodnoty zpětně splynuly s archivem
        IBKR a původ čísla by nešel poznat.
        """
        if not rows:
            return None
        path = (
            self._settings.derived_dir / symbol / expiry / "oifilled" / f"{day.isoformat()}.parquet"
        )
        buffer = self._buffer(path, OI_MISSING_SCHEMA)
        return buffer.append_and_write([asdict(row) for row in rows])

    def write_greeks_source(
        self, symbol: str, expiry: str, day: dt.date, rows: Sequence[GreeksSourceRow]
    ) -> Path | None:
        """Přidá striky s dopočtenými Greeks do derived/{sym}/{exp}/greekssource (#547).

        Prázdný seznam se nezapisuje — dokud TWS model dodává, řada nevznikne
        vůbec (stejný princip jako oimissing).
        """
        if not rows:
            return None
        path = (
            self._settings.derived_dir
            / symbol
            / expiry
            / "greekssource"
            / f"{day.isoformat()}.parquet"
        )
        buffer = self._buffer(path, GREEKS_SOURCE_SCHEMA)
        return buffer.append_and_write([asdict(row) for row in rows])

    def write_catch_up(
        self, symbol: str, expiry: str, day: dt.date, rows: Sequence[CatchUpRow]
    ) -> Path | None:
        """Přidá catch-up minuty do partice derived/{sym}/{exp}/catchup (#518).

        Stejný princip jako oimissing: prázdný seznam se nezapisuje — v běžný
        den (engine běžel od začátku) řada nevznikne vůbec.
        """
        if not rows:
            return None
        path = (
            self._settings.derived_dir / symbol / expiry / "catchup" / f"{day.isoformat()}.parquet"
        )
        buffer = self._buffer(path, CATCHUP_SCHEMA)
        return buffer.append_and_write([asdict(row) for row in rows])

    def write_levelsfa(
        self, symbol: str, expiry: str, day: dt.date, rows: Sequence[LevelsRow]
    ) -> Path:
        """Přidá flow-adjusted levels minuty do derived/{sym}/{exp}/levelsfa (ADR-0011).

        Stejné schéma jako levels — jiný vstup (OI odhad z klasifikovaného toku).
        """
        path = (
            self._settings.derived_dir / symbol / expiry / "levelsfa" / f"{day.isoformat()}.parquet"
        )
        buffer = self._buffer(path, LEVELS_SCHEMA)
        return buffer.append_and_write([asdict(row) for row in rows])

    def write_netflow(
        self, symbol: str, expiry: str, day: dt.date, rows: Sequence[NetFlowRow]
    ) -> Path | None:
        """Přidá kumulativní net objem minuty do derived/{sym}/{exp}/netflow (#232).

        Prázdný seznam se nezapisuje — dokud nikdo neobchoduje, řada nevzniká
        (stejný princip jako oimissing).
        """
        if not rows:
            return None
        path = (
            self._settings.derived_dir / symbol / expiry / "netflow" / f"{day.isoformat()}.parquet"
        )
        buffer = self._buffer(path, NETFLOW_SCHEMA)
        return buffer.append_and_write([asdict(row) for row in rows])

    def write_oiest(
        self, symbol: str, expiry: str, day: dt.date, rows: Sequence[OiEstRow]
    ) -> Path | None:
        """Přidá OI odhady minuty do derived/{sym}/{exp}/oiest (#232).

        Jen strany, kde se odhad liší od měřeného OI; prázdný seznam se
        nezapisuje.
        """
        if not rows:
            return None
        path = self._settings.derived_dir / symbol / expiry / "oiest" / f"{day.isoformat()}.parquet"
        buffer = self._buffer(path, OIEST_SCHEMA)
        return buffer.append_and_write([asdict(row) for row in rows])

    def write_ladder(
        self, symbol: str, expiry: str, day: dt.date, rows: Sequence[LadderRow]
    ) -> Path:
        """Přidá GEX žebřík minuty do partice derived/{sym}/{exp}/ladder (#244)."""
        path = (
            self._settings.derived_dir / symbol / expiry / "ladder" / f"{day.isoformat()}.parquet"
        )
        buffer = self._buffer(path, LADDER_SCHEMA)
        return buffer.append_and_write([asdict(row) for row in rows])

    def write_walldom(
        self, symbol: str, expiry: str, day: dt.date, rows: Sequence[WallDomRow]
    ) -> Path:
        """Přidá dominanci zdí minuty do partice derived/{sym}/{exp}/walldom (ADR-0010)."""
        path = (
            self._settings.derived_dir / symbol / expiry / "walldom" / f"{day.isoformat()}.parquet"
        )
        buffer = self._buffer(path, WALLDOM_SCHEMA)
        return buffer.append_and_write([asdict(row) for row in rows])

    def write_oiwalls(
        self, symbol: str, expiry: str, day: dt.date, rows: Sequence[OiWallsRow]
    ) -> Path:
        """Přidá OI zdi minuty do partice derived/{sym}/{exp}/oiwalls (#851)."""
        path = (
            self._settings.derived_dir / symbol / expiry / "oiwalls" / f"{day.isoformat()}.parquet"
        )
        buffer = self._buffer(path, OIWALLS_SCHEMA)
        return buffer.append_and_write([asdict(row) for row in rows])

    def write_gexprofile(
        self,
        symbol: str,
        expiry: str,
        day: dt.date,
        rows: Sequence[GexProfileRow],
        *,
        subdir: str = "gexprofile",
    ) -> Path:
        """Přidá profil plochy do derived/{sym}/{exp}/{subdir} (ADR-0009, #204).

        `subdir`: gexprofile (gamma) / charmprofile / vannaprofile — stejné
        schéma, jiná BS derivace.
        """
        path = self._settings.derived_dir / symbol / expiry / subdir / f"{day.isoformat()}.parquet"
        buffer = self._buffer(path, GEXPROFILE_SCHEMA)
        return buffer.append_and_write([asdict(row) for row in rows])

    def write_gexforward(
        self, symbol: str, day: dt.date, rows: Sequence[dict[str, object]]
    ) -> Path:
        """Přepíše forward pole v derived/{sym}/gexforward — jen poslední stav (#519)."""
        path = self._settings.derived_dir / symbol / "gexforward" / f"{day.isoformat()}.parquet"
        buffer = self._buffer(path, GEXFORWARD_SCHEMA)
        return buffer.replace_and_write(list(rows))

    def write_gexfield(
        self, symbol: str, expiry: str, day: dt.date, row: GexFieldRow, *, subdir: str = "gexfield"
    ) -> Path:
        """Přepíše modelované pole v derived/{sym}/{exp}/{subdir} — jen poslední stav."""
        path = self._settings.derived_dir / symbol / expiry / subdir / f"{day.isoformat()}.parquet"
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
                    # `getattr`, ne pole v protokolu: bary chodí z IBKR i z
                    # backfillu a živá cesta o původu nemusí nic vědět
                    "source": getattr(bar, "source", None) or BAR_SOURCE_LIVE,
                }
                for bar in bars
            ],
            key="ts_min",
        )

    def bar_minutes(self, symbol: str, day: dt.date) -> set[dt.datetime]:
        """Minuty, které v particii barů UŽ jsou (#617).

        Podklad pro doplnění děr: backfill dostane jen to, co chybí, takže
        se měřená minuta nemá jak přepsat.
        """
        path = self._settings.derived_dir / symbol / "bars" / f"{day.isoformat()}.parquet"
        if not path.exists():
            return set()
        column = pq.read_table(path, columns=["ts_min"]).column("ts_min").to_pylist()
        return {ts.replace(tzinfo=dt.UTC) if ts.tzinfo is None else ts for ts in column if ts}

    def write_bars_by_day(self, symbol: str, bars: Sequence[BarLike]) -> list[Path]:
        """Zapíše bary do partic podle UTC dne **jejich** `ts`, ne podle dne cyklu (#1002).

        Partice barů = UTC kalendářní den. Půlnoční cyklus 00:00 finalizuje bar
        23:59 dne D — zapsaný pod dnem cyklu skončil v partici D+1 a zdvojil se
        (provizorní verze zůstala v D). Totéž dělala rekonstrukce #617 s celým
        blokem 22:00–23:59 dne D−1 zapsaným pod datem seance. Kdo čte partice
        za sebou (news-engine, sešívání seance v API), dostal objem dvakrát.
        """
        by_day: dict[dt.date, list[BarLike]] = {}
        for bar in bars:
            by_day.setdefault(bar_partition_day(bar.ts), []).append(bar)
        return [self.write_bars(symbol, day, group) for day, group in sorted(by_day.items())]

    def bar_minutes_for_days(self, symbol: str, days: Iterable[dt.date]) -> set[dt.datetime]:
        """Sjednocení `bar_minutes` přes všechny partice, do kterých okno zasahuje (#1002).

        Seance začíná 22:00 UTC předchozího dne, takže její minuty leží ve dvou
        particích; kontrola jen proti partici dne seance hlásila večerní blok
        jako chybějící a rekonstrukce ho doplnila podruhé.
        """
        minutes: set[dt.datetime] = set()
        for day in days:
            minutes |= self.bar_minutes(symbol, day)
        return minutes

    def write_dx_flow(self, symbol: str, day: dt.date, rows: Sequence[object]) -> Path:
        """Stínové CumΔ minuty (#615) do derived/{sym}/cumdelta_dx/{date}.parquet.

        Retence derived/ (14 dní) tu stačí: řada slouží ~10sekčnímu měření
        pro spread_leg ADR a vyčíslení rozdílu vs. živé CumΔ.
        """
        path = self._settings.derived_dir / symbol / "cumdelta_dx" / f"{day.isoformat()}.parquet"
        buffer = self._buffer(path, DX_FLOW_SCHEMA)
        return buffer.append_and_write([asdict(row) for row in rows])  # type: ignore[call-overload]

    def write_flow(self, symbol: str, day: dt.date, rows: Sequence[FlowRowLike]) -> Path:
        """Přidá flowΔ/CumΔ minuty do partice derived/{sym}/flow/{date}.parquet."""
        path = self._settings.derived_dir / symbol / "flow" / f"{day.isoformat()}.parquet"
        buffer = self._buffer(path, FLOW_SCHEMA)
        return buffer.append_and_write(
            [
                {
                    "ts_min": row.ts_min,
                    "flow_delta": row.flow_delta,
                    "cum_delta": row.cum_delta,
                    "futures_cvd_delta": row.futures_cvd_delta,
                    "futures_cvd": row.futures_cvd,
                }
                for row in rows
            ]
        )

    def _buffer(self, path: Path, schema: pa.Schema) -> _PartitionBuffer:
        buffer = self._buffers.get(path)
        if buffer is None:
            buffer = _PartitionBuffer(path, schema)
            self._buffers[path] = buffer
        return buffer


def read_netflow_latest(
    path: Path,
    *,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
) -> dict[tuple[float, str], float]:
    """Poslední kumulativní net per strana z partice netflow (#232).

    Slouží k navázání kumulativu po restartu enginu uprostřed dne — bez toho
    by FA odhad začínal od nuly a zahodil celý dopolední tok. Neexistující
    partice vrací prázdnou mapu. Blokující čtení — volat přes to_thread.

    `start`/`end` (#638): jen řádky v okně [start, end) — kumulativ je od #638
    kotvený na Globex seanci, takže seed nesmí přenést stav PŘEDCHOZÍ seance
    ležící v téže UTC partici (engine spadlý před 17:00 CT).
    """
    if not path.exists():
        return {}
    table = pq.read_table(path, schema=NETFLOW_SCHEMA)
    latest: dict[tuple[float, str], tuple[dt.datetime, float]] = {}
    columns = [
        table.column(name).to_pylist() for name in ("ts_min", "strike", "right", "net_volume")
    ]  # noqa: E501
    for ts, strike, right, net in zip(*columns, strict=True):
        if ts is None:
            continue
        if (start is not None and ts < start) or (end is not None and ts >= end):
            continue
        key = (float(strike), str(right))
        current = latest.get(key)
        if current is None or ts >= current[0]:
            latest[key] = (ts, float(net) if net is not None else 0.0)
    return {key: net for key, (_, net) in latest.items()}


def read_last_cum_delta(
    paths: Sequence[Path],
    *,
    start: dt.datetime,
    end: dt.datetime,
) -> float | None:
    """Poslední `cum_delta` z flow partic v okně seance [start, end) (#638).

    Navázání CumΔ po restartu uprostřed seance — seance může ležet ve dvou
    UTC particích (večer D−1 + D), proto seznam cest. Bez řádku v okně None.
    Blokující čtení — volat přes to_thread.
    """
    best: tuple[dt.datetime, float] | None = None
    for path in paths:
        if not path.exists():
            continue
        table = pq.read_table(path, schema=FLOW_SCHEMA)
        for ts, cum in zip(
            table.column("ts_min").to_pylist(),
            table.column("cum_delta").to_pylist(),
            strict=True,
        ):
            if ts is None or ts < start or ts >= end:
                continue
            if best is None or ts >= best[0]:
                best = (ts, float(cum) if cum is not None else 0.0)
    return best[1] if best is not None else None
