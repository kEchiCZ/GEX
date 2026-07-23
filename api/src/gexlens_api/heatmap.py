"""Vektorizovaná heatmap matice nad denní snapshot particí (SPEC kap. 6, 4.3).

Sémantika módů je definována v `gexlens_engine.compute.heatmap` (per-snapshot
čisté funkce) — zde je vektorizovaný ekvivalent přes celý den (pandas/numpy),
aby odpověď pro 180×1440 zvládla limit < 300 ms (AC issue #19). Shodu obou
implementací hlídá konzistenční test.
"""

import io

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.ipc

from gexlens_engine.compute.heatmap import HeatmapMode, HeatmapScale

ARROW_MEDIA_TYPE = "application/vnd.apache.arrow.stream"

# Módy, které potřebují spot řadu (OTM/ITM se určuje vůči spotu v čase t)
SPOT_DEPENDENT_MODES = frozenset(
    {
        HeatmapMode.VOL_OTM,
        HeatmapMode.VOL_ITM,
        HeatmapMode.OI_PLUS_OTM,
        HeatmapMode.OI_MINUS_ITM,
    }
)


class MissingSpotSeriesError(ValueError):
    """Mód závislý na spotu nemá k dispozici bary podkladu → HTTP 422."""


def mode_matrices(
    frame: pd.DataFrame,
    mode: HeatmapMode,
    spot_series: pd.Series | None,
    *,
    oi_weight: float = 0.6,
    vol_weight: float = 0.4,
) -> dict[str, pd.DataFrame]:
    """Vrstvy heatmapy pro celý den: DataFrame ts_min × strike per vrstva.

    Pivoty se počítají líně a cachují — každý mód si sáhne jen na sloupce,
    které opravdu potřebuje (výkonnostní AC < 300 ms).
    """
    index = pd.Index(sorted(frame["ts_min"].unique()), name="ts_min")
    strikes = sorted(frame["strike"].unique())
    pivots: dict[tuple[str, str], pd.DataFrame] = {}

    def piv(right: str, value: str) -> pd.DataFrame:
        key = (right, value)
        if key not in pivots:
            side = frame[frame["right"] == right]
            table = side.pivot(index="ts_min", columns="strike", values=value)
            pivots[key] = table.reindex(index=index, columns=strikes).fillna(0.0)
        return pivots[key]

    def otm_masks() -> tuple[object, object]:
        if spot_series is None:
            raise MissingSpotSeriesError(
                f"Mód {mode.value} vyžaduje bary podkladu (spot per minuta), které nejsou uloženy"
            )
        spot = spot_series.reindex(index).ffill().bfill().to_numpy()[:, None]
        strike_row = np.asarray(strikes, dtype=float)[None, :]
        return strike_row > spot, strike_row < spot

    if mode is HeatmapMode.OI:
        return {"call": piv("C", "oi"), "put": piv("P", "oi")}
    if mode is HeatmapMode.VOL_SIGNED:
        return {"signed": piv("C", "volume") - piv("P", "volume")}
    if mode is HeatmapMode.OI_SIGNED_ALL:
        return {"signed": piv("C", "oi") - piv("P", "oi")}
    # VEX (#201): vega × OI — $ přecenění dealerských knih na 1 bod IV
    if mode is HeatmapMode.VEX:
        return {
            "call": piv("C", "vega") * piv("C", "oi"),
            "put": piv("P", "vega") * piv("P", "oi"),
        }
    if mode is HeatmapMode.VEX_SIGNED:
        return {"signed": piv("C", "vega") * piv("C", "oi") - piv("P", "vega") * piv("P", "oi")}

    call_otm, put_otm = otm_masks()
    if mode is HeatmapMode.VOL_OTM:
        return {
            "call": piv("C", "volume").where(call_otm, 0.0),
            "put": piv("P", "volume").where(put_otm, 0.0),
        }
    if mode is HeatmapMode.VOL_ITM:
        return {
            "call": piv("C", "volume").where(~call_otm, 0.0),  # type: ignore[operator]
            "put": piv("P", "volume").where(~put_otm, 0.0),  # type: ignore[operator]
        }
    if mode is HeatmapMode.OI_PLUS_OTM:
        otm_c = piv("C", "volume").where(call_otm, 0.0)
        otm_p = piv("P", "volume").where(put_otm, 0.0)
        max_oi = float(max(piv("C", "oi").max().max(), piv("P", "oi").max().max()))
        max_otm = float(max(otm_c.max().max(), otm_p.max().max()))

        def blend(oi: pd.DataFrame, otm: pd.DataFrame) -> pd.DataFrame:
            oi_part = oi / max_oi if max_oi > 0 else oi * 0.0
            otm_part = otm / max_otm if max_otm > 0 else otm * 0.0
            return oi_weight * oi_part + vol_weight * otm_part

        return {"call": blend(piv("C", "oi"), otm_c), "put": blend(piv("P", "oi"), otm_p)}
    if mode is HeatmapMode.OI_MINUS_ITM:
        return {
            "call": piv("C", "oi") - piv("C", "volume").where(~call_otm, 0.0),  # type: ignore[operator]
            "put": piv("P", "oi") - piv("P", "volume").where(~put_otm, 0.0),  # type: ignore[operator]
        }
    raise ValueError(f"Neznámý heatmap mód: {mode!r}")


def apply_scale_matrix(matrix: pd.DataFrame, scale: HeatmapScale) -> pd.DataFrame:
    values = matrix.to_numpy(dtype=float)
    if scale is HeatmapScale.LINEAR:
        scaled = values
    elif scale is HeatmapScale.SQRT:
        scaled = np.sign(values) * np.sqrt(np.abs(values))
    elif scale is HeatmapScale.LOG:
        scaled = np.sign(values) * np.log1p(np.abs(values))
    elif scale is HeatmapScale.CBRT:
        scaled = np.cbrt(values)
    else:
        raise ValueError(f"Neznámá škála: {scale!r}")
    return pd.DataFrame(scaled, index=matrix.index, columns=matrix.columns)


def normalization_denominator(layers: dict[str, pd.DataFrame], method: str) -> float:
    """p99 |hodnot| viditelného okna, nebo globální max (SPEC 4.3)."""
    magnitudes = np.abs(
        np.concatenate([matrix.to_numpy(dtype=float).ravel() for matrix in layers.values()])
    )
    if magnitudes.size == 0:
        return 0.0
    if method == "max":
        return float(magnitudes.max())
    if method == "p99":
        return float(np.quantile(magnitudes, 0.99))
    raise ValueError(f"Neznámá normalizace: {method!r} (očekávám p99/max)")


def to_arrow_bytes(layers: dict[str, pd.DataFrame]) -> bytes:
    """Serializace vrstev do jednoho Arrow IPC streamu.

    Sloupce: ts_min + `{vrstva}:{strike}` — klient si matici rozdělí podle prefixu.
    """
    first = next(iter(layers.values()))
    ts_array = pa.array(first.index.to_pydatetime(), type=pa.timestamp("us", tz="UTC"))
    arrays: list[pa.Array] = [ts_array]
    names = ["ts_min"]
    for layer_name, matrix in layers.items():
        for strike in matrix.columns:
            arrays.append(pa.array(matrix[strike].to_numpy(dtype=float)))
            names.append(f"{layer_name}:{strike:g}")
    table = pa.table(dict(zip(names, arrays, strict=True)))
    sink = io.BytesIO()
    with pyarrow.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


def frame_to_arrow_bytes(frame: pd.DataFrame) -> bytes:
    """Serializace surové denní partice (raw=true, replay) do Arrow IPC streamu.

    F32 transport (#247): float64 sloupce se na drátě posílají jako float32 —
    frontend je stejně ukládá do Float32Array, takže klient dostane bitově
    identické hodnoty jako dnes; exaktní hodnoty (strike/OI/volume/ceny na
    tick 0,25) zůstávají exaktní, plná přesnost trvá na disku v parquet.
    Změřeno na reálném dni: max relativní odchylka Greeks 0,00001 %.
    """
    slim = frame.copy()
    for column in slim.columns:
        if str(slim[column].dtype) == "float64":
            slim[column] = slim[column].astype("float32")
    table = pa.Table.from_pandas(slim, preserve_index=False)
    sink = io.BytesIO()
    with pyarrow.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()
