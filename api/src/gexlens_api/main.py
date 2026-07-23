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

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
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
from gexlens_engine.storage.oi_archive import OIEodRepository
from gexlens_engine.storage.setups_store import SetupsRepository


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
            elif isinstance(value, np.ndarray):
                # List sloupce (gexprofile.values, ADR-0009) čte pandas jako ndarray
                clean[key] = value.tolist()
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
    # OI archiv (PG, lazy) — ΔOI vs. předchozí den v /replay balíku
    oi_repository_ref: list[OIEodRepository] = []

    def oi_repository() -> OIEodRepository:
        if not oi_repository_ref:
            oi_repository_ref.append(OIEodRepository(meta_repository.engine()))
        return oi_repository_ref[0]

    setups_repository_ref: list[SetupsRepository] = []

    def setups_repository() -> SetupsRepository:
        if not setups_repository_ref:
            repo = SetupsRepository(meta_repository.engine())
            repo.ensure_schema()
            setups_repository_ref.append(repo)
        return setups_repository_ref[0]

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
    # Komprese odpovědí (#247): /replay bundle 12,4 MB → ~2 MB; klíčové pro
    # LAN/budoucí vzdálené nasazení (změřeno: komprese 184 ms, dekomprese
    # v prohlížeči 20 ms nativně — přenos je úzké hrdlo, ne CPU)
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)
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

    @app.get("/instruments/{symbol}/days")
    def days(symbol: str) -> dict[str, list[dict[str, str]]]:
        """Uložené dny (Daily pohled) — každý den se svou expirací (0DTE řetěz)."""
        found = repository.list_days(symbol)
        if not found:
            raise HTTPException(404, f"Instrument {symbol!r} nemá žádná data")
        return {"days": found}

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

    @app.get("/setups/{symbol}")
    def setups_list(
        symbol: str, date: dt.date | None = None, status: str | None = None
    ) -> dict[str, object]:
        """Historie setupů (ADR-0004): analýzy s automatickým vyhodnocením."""
        try:
            rows = setups_repository().list_for(symbol, date=date, status=status)
        except Exception:
            rows = []  # DB nedostupná — UI drží tvar
        return {"symbol": symbol, "setups": rows}

    @app.patch("/setups/{symbol}/{setup_id}/review")
    def setups_review(symbol: str, setup_id: int, payload: dict[str, object]) -> dict[str, str]:
        """Ruční hodnocení setupu (jediná povolená mutace; predikce je neměnná)."""
        rating = payload.get("rating")
        note = payload.get("note")
        if rating is not None and rating not in (1, -1):
            raise HTTPException(422, "rating musí být 1, -1 nebo null")
        if note is not None and not isinstance(note, str):
            raise HTTPException(422, "note musí být text")
        if not setups_repository().review(
            setup_id, rating if isinstance(rating, int) else None, note
        ):
            raise HTTPException(404, f"Setup {setup_id} neexistuje")
        return {"status": "ok"}

    @app.get("/profile/{symbol}/aggregate")
    def profile_aggregate(symbol: str, date: dt.date) -> dict[str, object]:
        """Souhrnný strike profil přes všechny expirace dne (Σ pohled v UI).

        Z poslední zapsané minuty každé expirace se sečtou OI/volume (a jejich
        delta-vážené komponenty) per strike a strana — celkové zdi bez ohledu
        na to, ve kterém řetězu pozice sedí.
        """
        totals: dict[tuple[float, str], dict[str, float]] = {}
        expiries_used: list[str] = []
        for expiry in repository.list_expiries(symbol):
            try:
                frame = repository.snapshots(symbol, expiry, date)
            except PartitionNotFoundError:
                continue
            if frame.empty:
                continue
            last = frame[frame["ts_min"] == frame["ts_min"].max()]
            expiries_used.append(expiry)
            for row in last.itertuples():
                key = (float(row.strike), str(row.right))
                bucket = totals.setdefault(
                    key, {"volume": 0.0, "oi": 0.0, "vol_component": 0.0, "oi_component": 0.0}
                )
                delta = abs(float(row.delta)) if row.delta == row.delta else 0.0
                volume = float(row.volume) if row.volume == row.volume else 0.0
                oi = float(row.oi) if row.oi == row.oi else 0.0
                bucket["volume"] += volume
                bucket["oi"] += oi
                bucket["vol_component"] += volume * delta
                bucket["oi_component"] += oi * delta
        strikes = sorted({strike for strike, _ in totals})
        rows = []
        for strike in strikes:
            call = totals.get((strike, "C"), {})
            put = totals.get((strike, "P"), {})
            rows.append(
                {
                    "strike": strike,
                    "callVolComponent": call.get("vol_component", 0.0),
                    "callOiComponent": call.get("oi_component", 0.0),
                    "putVolComponent": put.get("vol_component", 0.0),
                    "putOiComponent": put.get("oi_component", 0.0),
                    "callVolume": call.get("volume", 0.0),
                    "putVolume": put.get("volume", 0.0),
                    "callOi": call.get("oi", 0.0),
                    "putOi": put.get("oi", 0.0),
                }
            )
        return {"symbol": symbol, "date": date.isoformat(), "expiries": expiries_used, "rows": rows}

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

    @app.get("/chain/{symbol}/{expiry}")
    def chain(symbol: str, expiry: str, date: dt.date) -> dict[str, object]:
        """Greeks & OI tabulka (#202): per-strike řetěz z poslední minuty snapshotů.

        Řádek na strike se stranami C/P (bid/ask/last/vol/IV/Δ/Γ/Θ/V/OI + stale)
        a ΔOI vs. poslední archivovaný den (věčný OI archiv, R4).
        """
        frame = repository.snapshots(symbol, expiry, date)
        minute = frame["ts_min"].max()
        rows = frame[frame["ts_min"] == minute]

        oi_prev: dict[tuple[float, str], float] = {}
        try:
            repo = oi_repository()
            previous = repo.latest_day_before(symbol, expiry, date)
            if previous is not None:
                oi_prev = {
                    (record.strike, record.right): record.oi
                    for record in repo.values_for(symbol, expiry, previous)
                }
        except Exception:
            oi_prev = {}  # OI archiv nedostupný — tabulka drží tvar bez ΔOI

        def clean(value: object) -> float | None:
            number = float(value)  # type: ignore[arg-type]
            return None if math.isnan(number) else number

        by_strike: dict[float, dict[str, object]] = {}
        for row in rows.itertuples():
            strike = float(row.strike)
            side = {
                "bid": clean(row.bid),
                "ask": clean(row.ask),
                "last": clean(row.last),
                "volume": clean(row.volume) or 0.0,
                "iv": clean(row.iv),
                "delta": clean(row.delta),
                "gamma": clean(row.gamma),
                "theta": clean(row.theta),
                "vega": clean(row.vega),
                "oi": clean(row.oi) or 0.0,
                "stale": bool(row.stale_age > 0),
            }
            prev = oi_prev.get((strike, str(row.right)))
            side["oi_change"] = None if prev is None else (side["oi"] or 0.0) - prev
            entry = by_strike.setdefault(strike, {"strike": strike})
            entry["call" if row.right == "C" else "put"] = side

        return {
            "ts": minute.isoformat(),
            "symbol": symbol,
            "expiry": expiry,
            "rows": [by_strike[strike] for strike in sorted(by_strike)],
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
            ("levels2", lambda: repository.levels2(symbol, expiry, date)),
            ("walldom", lambda: repository.walldom(symbol, expiry, date)),
            ("levelsfa", lambda: repository.levelsfa(symbol, expiry, date)),
            ("ladder", lambda: repository.ladder(symbol, expiry, date)),
            ("gexprofile", lambda: repository.gexprofile(symbol, expiry, date)),
            ("gexfield", lambda: repository.gexfield(symbol, expiry, date)),
            ("flow", lambda: repository.flow(symbol, date)),
            ("bars", lambda: repository.bars(symbol, date)),
        ]
        for key, reader in readers:
            try:
                bundle[key] = _records(reader())
            except PartitionNotFoundError:
                bundle[key] = []  # část dne může chybět (např. bez flow) — balík drží tvar
        # ΔOI vs. předchozí den: poslední archivovaný den téže expirace před `date`
        bundle["oi_prev"] = []
        try:
            repo = oi_repository()
            previous = repo.latest_day_before(symbol, expiry, date)
            if previous is not None:
                bundle["oi_prev"] = [
                    {"strike": record.strike, "right": record.right, "oi": record.oi}
                    for record in repo.values_for(symbol, expiry, previous)
                ]
                bundle["oi_prev_date"] = previous.isoformat()
        except Exception:
            # OI archiv nedostupný (např. čerstvá DB) — balík drží tvar bez ΔOI
            bundle["oi_prev"] = []
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
