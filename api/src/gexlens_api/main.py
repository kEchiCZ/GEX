"""API server GEXLens (SPEC kap. 6): REST endpoints nad uloženými particemi.

Server jen čte, co engine zapsal; /status vrací poslední stav pushnutý enginem
do StatusStore. Bind na localhost řeší uvicorn konfigurace (SPEC kap. 8).
"""

import asyncio
import base64
import contextlib
import datetime as dt
import math
from collections.abc import Callable
from typing import Annotated

import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from gexlens_api.alerts import AlertEngine
from gexlens_api.crud import build_router
from gexlens_api.data import DataRepository, PartitionNotFoundError
from gexlens_api.heatmap import (
    ARROW_MEDIA_TYPE,
    MissingSpotSeriesError,
    apply_scale_matrix,
    frame_to_arrow_bytes,
    mode_matrices,
    normalization_denominator,
    to_arrow_bytes,
)
from gexlens_api.live import LiveHub
from gexlens_api.meta_repo import MetaRepository
from gexlens_api.status import StatusStore
from gexlens_engine.compute.heatmap import HeatmapMode, HeatmapScale
from gexlens_engine.compute.profile import ProfileInput, ProfileVariant, compute_profile
from gexlens_engine.config import Settings, load_settings


def _records(frame: pd.DataFrame) -> list[dict[str, object]]:
    """DataFrame → JSON-friendly records (NaN → None, timestamps → ISO)."""
    records: list[dict[str, object]] = []
    for row in frame.to_dict(orient="records"):
        clean: dict[str, object] = {}
        for key, value in row.items():
            if isinstance(value, float) and math.isnan(value):
                clean[key] = None
            elif isinstance(value, pd.Timestamp):
                clean[key] = value.isoformat()
            else:
                clean[key] = value
        records.append(clean)
    return records


def _parse_enum[E](enum_cls: type[E], value: str, label: str) -> E:
    try:
        return enum_cls(value)  # type: ignore[call-arg]
    except ValueError as exc:
        valid = ", ".join(item.value for item in enum_cls)  # type: ignore[attr-defined]
        raise HTTPException(422, f"Neplatný {label}: {value!r} (platné: {valid})") from exc


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings if settings is not None else load_settings()
    repository = DataRepository(settings)
    status_store = StatusStore()

    live_hub = LiveHub()
    meta_repository = MetaRepository(settings)
    alert_engine = AlertEngine(live_hub)

    app = FastAPI(title="GEXLens API")
    # Frontend běží na jiném lokálním portu (nginx :8080, Vite dev :5173) —
    # bez CORS hlaviček prohlížeč fetche blokuje. Vše zůstává na localhostu
    # (SPEC kap. 8: žádný vzdálený přístup).
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.status_store = status_store
    app.state.live_hub = live_hub
    app.state.meta_repository = meta_repository
    app.state.alert_engine = alert_engine
    app.include_router(build_router(meta_repository))

    @app.exception_handler(PartitionNotFoundError)
    async def partition_not_found(_request: object, exc: PartitionNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": f"Denní partice neexistuje: {exc}"})

    @app.get("/health")
    def health() -> dict[str, str]:
        """Liveness check pro monitoring a smoke testy."""
        return {"status": "ok"}

    @app.get("/status")
    def status() -> dict[str, object]:
        """Agregovaný stav pipeline (SPEC 3.7): greeks progress, repair, lines, disk."""
        return status_store.snapshot()

    # Interní ingest z enginu (API bindí na localhost — SPEC kap. 8 bezpečnost)
    @app.post("/internal/status")
    def internal_status(fields: dict[str, object]) -> dict[str, str]:
        status_store.update(**fields)
        live_hub.publish("status", status_store.snapshot())
        return {"status": "ok"}

    @app.post("/internal/publish")
    def internal_publish(message: dict[str, object]) -> dict[str, int]:
        channel = message.get("channel")
        data = message.get("data")
        if not isinstance(channel, str) or not isinstance(data, dict):
            raise HTTPException(422, "Očekávám {channel: str, data: object}")
        return {"delivered": live_hub.publish(channel, data)}

    @app.get("/instruments")
    def instruments() -> dict[str, list[str]]:
        return {"instruments": repository.list_symbols()}

    @app.get("/instruments/{symbol}/expiries")
    def expiries(symbol: str) -> dict[str, list[str]]:
        found = repository.list_expiries(symbol)
        if not found:
            raise HTTPException(404, f"Instrument {symbol!r} nemá žádná data")
        return {"expiries": found}

    @app.get("/snapshots/{symbol}/{expiry}")
    def snapshots(
        symbol: str,
        expiry: str,
        date: dt.date,
        mode: str = "oi",
        scale: str = "linear",
        norm: str = "p99",
        raw: bool = False,
        from_ts: Annotated[dt.datetime | None, Query(alias="from")] = None,
        to_ts: Annotated[dt.datetime | None, Query(alias="to")] = None,
    ) -> Response:
        """Heatmap matice dne v Arrow IPC streamu (binárně pro výkon, SPEC kap. 6)."""
        frame = repository.snapshots(symbol, expiry, date)
        if from_ts is not None:
            frame = frame[frame["ts_min"] >= from_ts]
        if to_ts is not None:
            frame = frame[frame["ts_min"] <= to_ts]
        if frame.empty:
            raise HTTPException(404, "Zvolené okno neobsahuje žádné snapshoty")
        if raw:
            return Response(frame_to_arrow_bytes(frame), media_type=ARROW_MEDIA_TYPE)

        heatmap_mode = _parse_enum(HeatmapMode, mode, "mode")
        heatmap_scale = _parse_enum(HeatmapScale, scale, "scale")
        spot_series = _spot_series(repository, symbol, date)
        try:
            layers = mode_matrices(frame, heatmap_mode, spot_series)
        except MissingSpotSeriesError as exc:
            raise HTTPException(422, str(exc)) from exc
        layers = {
            name: apply_scale_matrix(matrix, heatmap_scale) for name, matrix in layers.items()
        }
        denominator = normalization_denominator(layers, norm)
        if denominator > 0:
            layers = {name: matrix / denominator for name, matrix in layers.items()}
        return Response(to_arrow_bytes(layers), media_type=ARROW_MEDIA_TYPE)

    @app.get("/levels/{symbol}/{expiry}")
    def levels(symbol: str, expiry: str, date: dt.date) -> dict[str, object]:
        """Časové řady flip/walls/centroid (SPEC 4.2)."""
        return {"levels": _records(repository.levels(symbol, expiry, date))}

    @app.get("/profile/{symbol}/{expiry}")
    def profile(
        symbol: str,
        expiry: str,
        date: dt.date,
        ts: dt.datetime,
        variant: str = "combined",
        oi_weight: float = 1.0,
        spot: float | None = None,
    ) -> dict[str, object]:
        """Strike profil k okamžiku ts (SPEC 4.6): poslední snapshot ≤ ts."""
        profile_variant = _parse_enum(ProfileVariant, variant, "variant")
        frame = repository.snapshots(symbol, expiry, date)
        eligible = frame[frame["ts_min"] <= ts]
        if eligible.empty:
            raise HTTPException(404, f"Před {ts.isoformat()} není žádný snapshot")
        minute = eligible["ts_min"].max()
        rows = eligible[eligible["ts_min"] == minute].dropna(subset=["delta"])

        if spot is None:
            spot = _spot_at(repository, symbol, date, ts)
        if spot is None:
            raise HTTPException(
                422, "Chybí spot: dodej ?spot= nebo ulož bary podkladu (derived/bars)"
            )
        inputs = [
            ProfileInput(
                strike=float(row.strike),
                right=str(row.right),
                volume=float(row.volume) if not math.isnan(row.volume) else 0.0,
                oi=float(row.oi) if not math.isnan(row.oi) else 0.0,
                delta=float(row.delta),
            )
            for row in rows.itertuples()
        ]
        result = compute_profile(inputs, profile_variant, spot, oi_weight=oi_weight)
        return {
            "ts": minute.isoformat(),
            "spot": spot,
            "variant": profile_variant.value,
            "profile": [vars(item) for item in result],
        }

    @app.get("/flow/{symbol}")
    def flow(symbol: str, date: dt.date) -> dict[str, object]:
        """Řady Vol (podklad), OptVol (opce) a CumΔ pro spodní panely (SPEC kap. 6)."""
        flow_records = _records(repository.flow(symbol, date))
        return {
            "flow": flow_records,
            "opt_vol": _opt_vol_series(repository, symbol, date),
            "vol": _underlying_vol_series(repository, symbol, date),
        }

    @app.get("/replay/{symbol}/{expiry}/{date}")
    def replay(symbol: str, expiry: str, date: dt.date) -> dict[str, object]:
        """Kompletní denní balík pro playback (SPEC kap. 6).

        Snapshot matice jde surová (base64 Arrow) — klient přepíná módy/škály
        lokálně bez dalších requestů (latence < 100 ms, SPEC kap. 8).
        """
        snapshots_frame = repository.snapshots(symbol, expiry, date)
        bundle: dict[str, object] = {
            "symbol": symbol,
            "expiry": expiry,
            "date": date.isoformat(),
            "snapshots_arrow_base64": base64.b64encode(
                frame_to_arrow_bytes(snapshots_frame)
            ).decode("ascii"),
        }
        readers: list[tuple[str, Callable[[], pd.DataFrame]]] = [
            ("levels", lambda: repository.levels(symbol, expiry, date)),
            ("flow", lambda: repository.flow(symbol, date)),
            ("bars", lambda: repository.bars(symbol, date)),
        ]
        for key, reader in readers:
            try:
                bundle[key] = _records(reader())
            except PartitionNotFoundError:
                bundle[key] = []  # část dne může chybět (např. bez flow) — balík drží tvar
        return bundle

    @app.websocket("/ws/live")
    async def ws_live(websocket: WebSocket) -> None:
        """Live push kanálů (SPEC kap. 6): subscribe/unsubscribe protokol zprávami."""
        await websocket.accept()
        subscriber_id, queue = live_hub.register()

        async def sender() -> None:
            while True:
                await websocket.send_json(await queue.get())

        sender_task = asyncio.create_task(sender())
        try:
            while True:
                request = await websocket.receive_json()
                action = request.get("action")
                channels = request.get("channels", [])
                if action == "subscribe":
                    subscribed = live_hub.subscribe(subscriber_id, channels)
                    await websocket.send_json(
                        {"type": "ack", "action": "subscribe", "channels": sorted(subscribed)}
                    )
                elif action == "unsubscribe":
                    subscribed = live_hub.unsubscribe(subscriber_id, channels)
                    await websocket.send_json(
                        {"type": "ack", "action": "unsubscribe", "channels": sorted(subscribed)}
                    )
                else:
                    await websocket.send_json(
                        {"type": "error", "detail": f"Neznámá akce: {action!r}"}
                    )
        except WebSocketDisconnect:
            pass
        finally:
            sender_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender_task
            live_hub.unregister(subscriber_id)

    return app


def _spot_series(repository: DataRepository, symbol: str, day: dt.date) -> pd.Series | None:
    try:
        bars = repository.bars(symbol, day)
    except PartitionNotFoundError:
        return None
    return bars.set_index("ts_min")["close"]


def _spot_at(
    repository: DataRepository, symbol: str, day: dt.date, ts: dt.datetime
) -> float | None:
    series = _spot_series(repository, symbol, day)
    if series is None:
        return None
    eligible = series[series.index <= ts]
    if eligible.empty:
        return None
    return float(eligible.iloc[-1])


def _opt_vol_series(
    repository: DataRepository, symbol: str, day: dt.date
) -> list[dict[str, object]]:
    """OptVol per minuta: součet minutových přírůstků kumulativního volume přes expirace."""
    total: pd.Series | None = None
    for expiry in repository.list_expiries(symbol):
        try:
            frame = repository.snapshots(symbol, expiry, day)
        except PartitionNotFoundError:
            continue
        per_contract = frame.pivot_table(
            index="ts_min", columns=["strike", "right"], values="volume", aggfunc="last"
        )
        increments = per_contract.diff().clip(lower=0.0)
        increments.iloc[0] = 0.0  # první minuta nemá přírůstek
        series = increments.sum(axis=1)
        total = series if total is None else total.add(series, fill_value=0.0)
    if total is None:
        return []
    return [{"ts_min": ts.isoformat(), "opt_vol": float(value)} for ts, value in total.items()]


def _underlying_vol_series(
    repository: DataRepository, symbol: str, day: dt.date
) -> list[dict[str, object]]:
    try:
        bars = repository.bars(symbol, day)
    except PartitionNotFoundError:
        return []
    return [
        {"ts_min": row["ts_min"], "vol": row["volume"]}
        for row in _records(bars[["ts_min", "volume"]])
    ]


app = create_app()
