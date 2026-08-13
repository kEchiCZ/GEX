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
from typing import Annotated, Any

import numpy as np
import pandas as pd
from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select

from gexlens_api.alerts import AlertEngine
from gexlens_api.backup import build_backup_router
from gexlens_api.crud import build_router
from gexlens_api.data import DataRepository, PartitionNotFoundError, session_bounds
from gexlens_api.heatmap import (
    ARROW_MEDIA_TYPE,
    MissingSpotSeriesError,
    apply_scale_matrix,
    frame_to_arrow_bytes,
    mode_matrices,
    normalization_denominator,
    to_arrow_bytes,
)
from gexlens_api.live import LiveHub, TooManyChannels, TooManySubscribers, parse_channels
from gexlens_api.meta_repo import MetaRepository
from gexlens_api.security import (
    build_token_guard,
    load_allowed_origins,
    load_api_token,
    origin_allowed,
)
from gexlens_api.sentiment_routes import build_sentiment_router
from gexlens_api.status import StatusStore
from gexlens_engine.compute.gammacliff import build_cliff
from gexlens_engine.compute.heatmap import HeatmapMode, HeatmapScale
from gexlens_engine.compute.profile import ProfileInput, ProfileVariant, compute_profile
from gexlens_engine.compute.settle import trading_session_date
from gexlens_engine.config import Settings, load_settings
from gexlens_engine.gammacliff import read_expiries_at
from gexlens_engine.storage.fa_calibration import FaAlphaRepository
from gexlens_engine.storage.gammacliff_store import gamma_cliff_table
from gexlens_engine.storage.oi_archive import OIEodRepository
from gexlens_engine.storage.sentiment import ensure_sentiment_schema
from gexlens_engine.storage.setups_store import SetupsRepository
from gexlens_engine.storage.tendency_store import TendencyRepository


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

    tendency_repository_ref: list[TendencyRepository] = []

    def tendency_repository() -> TendencyRepository:
        if not tendency_repository_ref:
            repo = TendencyRepository(meta_repository.engine())
            repo.ensure_schema()
            tendency_repository_ref.append(repo)
        return tendency_repository_ref[0]

    app = FastAPI(title="GEXLens API")
    api_token = load_api_token()
    allowed_origins = load_allowed_origins()
    require_token = build_token_guard(api_token)
    # V produkci je frontend same-origin (nginx proxuje /api, #542) a CORS se
    # neuplatní. Regex zůstává kvůli Vite dev serveru na jiném portu; vzdálené
    # origins se přidávají výhradně přes GEXLENS_ALLOWED_ORIGINS.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
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
    # SentimentLens (#285) — vlastní router, ať main.py nenaroste o dalších
    # 200 řádků; schéma se zakládá lazy při prvním dotazu
    sentiment_ready: list[bool] = []

    def sentiment_engine() -> Any:
        engine = meta_repository.engine()
        if not sentiment_ready:
            ensure_sentiment_schema(engine)
            sentiment_ready.append(True)
        return engine

    app.include_router(build_sentiment_router(sentiment_engine, settings.data_dir))
    # Záloha PostgreSQL (#438): parquety má uživatel na disku, DB je ve volume.
    # Dump nese celý archiv, takže jen s tokenem (#542 C3).
    app.include_router(build_backup_router(settings.database_url, require_token))

    @app.exception_handler(PartitionNotFoundError)
    async def partition_not_found(_request: object, exc: PartitionNotFoundError) -> JSONResponse:
        # Bez cesty na disku (#542 M6) — hláška prozrazovala rozložení kontejneru
        return JSONResponse(status_code=404, content={"detail": "Denní partice neexistuje"})

    @app.get("/health")
    def health() -> dict[str, str]:
        """Liveness check pro monitoring a smoke testy."""
        return {"status": "ok"}

    @app.get("/status")
    def status() -> dict[str, object]:
        """Agregovaný stav pipeline (SPEC 3.7): greeks progress, repair, lines, disk."""
        return status_store.snapshot()

    # Interní ingest z enginu. Chráněno sdíleným tajemstvím (#542 C5) — kdokoli
    # s přístupem na port by jinak podvrhl UI libovolné ceny, úrovně i alerty.
    # `async def` je nutné (#554 L6): sync def by FastAPI pustil ve worker
    # threadu a `LiveHub.publish` sahá na asyncio.Queue, která thread-safe není.
    @app.post("/internal/status", dependencies=[Depends(require_token)])
    async def internal_status(fields: dict[str, object]) -> dict[str, str]:
        status_store.update(**fields)
        live_hub.publish("status", status_store.snapshot())
        return {"status": "ok"}

    @app.post("/internal/publish", dependencies=[Depends(require_token)])
    async def internal_publish(message: dict[str, object]) -> dict[str, int]:
        channel = message.get("channel")
        data = message.get("data")
        if not isinstance(channel, str) or not isinstance(data, dict):
            raise HTTPException(422, "Očekávám {channel: str, data: object}")
        return {"delivered": live_hub.publish(channel, data)}

    @app.get("/instruments")
    def instruments() -> dict[str, list[str]]:
        return {"instruments": repository.list_symbols()}

    # Kalibrovaná FA α (#232 fáze 2) — lazy repo jako OI archiv výše
    alpha_repository_ref: list[FaAlphaRepository] = []

    def alpha_repository() -> FaAlphaRepository:
        if not alpha_repository_ref:
            repo = FaAlphaRepository(meta_repository.engine())
            repo.ensure_schema()
            alpha_repository_ref.append(repo)
        return alpha_repository_ref[0]

    @app.get("/fa/alpha")
    def fa_alpha() -> dict[str, object]:
        """Aktuální kalibrovaná α per symbol + počet validačních dnů (#232).

        Frontend z toho kreslí badge „FA α=0.34 · 5 dní". Před prvním
        kalibračním bodem je seznam prázdný — engine jede na defaultu
        z konfigurace a UI to přizná („FA α default").
        """
        try:
            states = alpha_repository().list_all()
        except Exception:
            states = []  # DB nedostupná — UI drží tvar
        return {
            "alphas": [
                {
                    "symbol": state.symbol,
                    "alpha": state.alpha,
                    "days": state.days,
                    "updated_at": state.updated_at.isoformat(),
                }
                for state in states
            ]
        }

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
        # Osa dne = Globex seance (#512): večer D−1 patří dni D
        frame = repository.session_frame(lambda d: repository.snapshots(symbol, expiry, d), date)
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
        return {
            "levels": _records(
                repository.session_frame(lambda d: repository.levels(symbol, expiry, d), date)
            )
        }

    @app.get("/bars/{symbol}")
    def bars(symbol: str, date: dt.date) -> dict[str, object]:
        """1min OHLCV bary podkladu za obchodní seanci (#674, #678).

        Lehká JSON alternativa k /replay pro pohledy, které potřebují jen cenu
        (briefing: overnight rozsah + včerejší settle; referenční úrovně:
        PDH/PDL, VWAP). Osa dne = Globex seance (session_frame, #512).
        """
        return {
            "symbol": symbol,
            "date": date.isoformat(),
            "bars": _records(repository.session_frame(lambda d: repository.bars(symbol, d), date)),
        }

    @app.get("/oidelta/{symbol}/{expiry}")
    def oi_delta(symbol: str, expiry: str, movers: int = 10) -> dict[str, object]:
        """ΔOI přes noc (#674): souhrn změny OI vs. předchozí archivovaný den.

        Porovnává poslední dva archivované dny věčného OI archivu dané expirace
        (ranní archiv ~po otevření CME nese včerejší settlement OI). Před prvním
        archivem expirace vrací prázdný tvar — briefing sekci skryje.
        """
        empty: dict[str, object] = {"symbol": symbol, "expiry": expiry, "days": None}
        try:
            repo = oi_repository()
            today_utc = dt.datetime.now(dt.UTC).date()
            current = repo.latest_day_before(symbol, expiry, today_utc + dt.timedelta(days=1))
            if current is None:
                return empty
            previous = repo.latest_day_before(symbol, expiry, current)
            latest = {(r.strike, r.right): r.oi for r in repo.values_for(symbol, expiry, current)}
            prior: dict[tuple[float, str], float] = {}
            if previous is not None:
                prior = {
                    (r.strike, r.right): r.oi for r in repo.values_for(symbol, expiry, previous)
                }
        except Exception:
            return empty  # OI archiv nedostupný — briefing drží tvar bez ΔOI
        totals = {"C": 0.0, "P": 0.0}
        deltas = {"C": 0.0, "P": 0.0}
        rows: list[dict[str, object]] = []
        for (strike, right), oi in latest.items():
            totals[right] = totals.get(right, 0.0) + oi
            delta = oi - prior.get((strike, right), 0.0) if prior else 0.0
            deltas[right] = deltas.get(right, 0.0) + delta
            rows.append({"strike": strike, "right": right, "oi": oi, "delta": delta})
        rows.sort(key=lambda row: -abs(float(row["delta"])))  # type: ignore[arg-type]
        return {
            "symbol": symbol,
            "expiry": expiry,
            "days": {
                "current": current.isoformat(),
                "previous": previous.isoformat() if previous is not None else None,
            },
            "call_total": totals.get("C", 0.0),
            "put_total": totals.get("P", 0.0),
            "call_delta": deltas.get("C", 0.0),
            "put_delta": deltas.get("P", 0.0),
            "movers": rows[: max(0, movers)],
        }

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

    @app.get("/gexforward/{symbol}")
    def gex_forward(symbol: str, date: dt.date | None = None) -> dict[str, object]:
        """Forward GEX (#519): modelové Dyn pole per budoucí obchodní den.

        Bloky z partice dne (přepočet po ranním OI archivu). Prázdné `days`
        = pole se ještě nespočítalo (např. před prvním archivem dne).
        `math.isnan` u dropped_share prvního dne → None v JSON.
        """
        day = date or trading_session_date(dt.datetime.now(dt.UTC))
        try:
            frame = repository.gexforward(symbol, day)
        except PartitionNotFoundError:
            return {"symbol": symbol, "date": day.isoformat(), "days": []}
        days: list[dict[str, object]] = []
        for row in frame.to_dict("records"):
            share = row.get("dropped_share")
            days.append(
                {
                    "day": row["day"],
                    "grid_start": float(row["grid_start"]),
                    "grid_step": float(row["grid_step"]),
                    "values": [float(v) for v in row["values"]],
                    "dropped_expiries": [str(e) for e in row["dropped_expiries"]],
                    "dropped_share": float(share) if share is not None and share == share else None,
                    "iv_fallback_share": float(row["iv_fallback_share"]),
                    "computed_ts": row["computed_ts"].isoformat(),
                }
            )
        days.sort(key=lambda block: str(block["day"]))
        return {"symbol": symbol, "date": day.isoformat(), "days": days}

    @app.get("/gammacliff/{symbol}")
    def gamma_cliff(symbol: str, limit: int = 30) -> dict[str, object]:
        """Gamma útes (#576): živý podíl dnes odpadající gammy + uložená historie.

        `today` se počítá z posledních levels řádků sledovaných expirací —
        chip „dnes odpadá X %" má smysl PŘED settle, kdy tabulka řádek ještě
        nemá. Historie z `gamma_cliff` (fáze 1 jen měří, nic nezapíná).
        """
        now = dt.datetime.now(dt.UTC)
        session = trading_session_date(now)
        today: dict[str, object] | None = None
        record = build_cliff(
            session, symbol, read_expiries_at(settings.data_dir, symbol, session, now)
        )
        if record is not None:
            today = {
                "session_date": record.session_date.isoformat(),
                "cliff_share": record.cliff_share,
                "gex_before": record.gex_before,
                "gex_expiring": record.gex_expiring,
                "is_opex": record.is_opex,
            }
        rows: list[dict[str, object]] = []
        try:
            stmt = (
                select(gamma_cliff_table)
                .where(gamma_cliff_table.c.symbol == symbol)
                .order_by(gamma_cliff_table.c.session_date.desc())
                .limit(max(1, min(limit, 365)))
            )
            with meta_repository.engine().connect() as conn:
                for row in conn.execute(stmt):
                    payload = dict(row._mapping)
                    payload["session_date"] = row.session_date.isoformat()
                    payload["computed_at"] = row.computed_at.isoformat()
                    rows.append(payload)
        except Exception:
            rows = []  # tabulka ještě neexistuje (engine neběžel) — UI drží tvar
        return {"symbol": symbol, "today": today, "rows": rows}

    @app.get("/tendency/{symbol}")
    def tendency_series(symbol: str, date: dt.date | None = None) -> dict[str, object]:
        """Minutová řada indikátoru tendence (#350) — rozpad hlasů v každém bodě."""
        day = date or dt.datetime.now(dt.UTC).date()
        try:
            rows = tendency_repository().series_for(symbol, day)
        except Exception:
            rows = []  # DB nedostupná — UI drží tvar
        for row in rows:
            ts_min = row.get("ts_min")
            if isinstance(ts_min, dt.datetime):
                row["ts_min"] = ts_min.isoformat()
        return {"symbol": symbol, "date": day.isoformat(), "tendency": rows}

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
                frame = repository.session_frame(
                    lambda d: repository.snapshots(symbol, expiry, d),  # noqa: B023
                    date,
                )
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
        ts: dt.datetime | None = None,
        from_ts: Annotated[dt.datetime | None, Query(alias="from")] = None,
        to_ts: Annotated[dt.datetime | None, Query(alias="to")] = None,
        variant: str = "combined",
        oi_weight: float = 1.0,
        spot: float | None = None,
    ) -> dict[str, object]:
        """Strike profil k okamžiku ts, nebo za okno [from, to] (SPEC 4.6, #483).

        Volume ve snapshotech je kumulativní denní (SPEC 4.3) — okenní hodnoty
        jsou prostý rozdíl dvou snímků, O(strikes). OI v okenním módu zůstává
        statické k `to` (`oi_static: true`) — otevřené pozice nejsou tok.
        """
        window = from_ts is not None or to_ts is not None
        if window and (ts is not None or from_ts is None or to_ts is None):
            raise HTTPException(422, "Okenní mód vyžaduje from i to a vylučuje ts")
        if not window and ts is None:
            raise HTTPException(422, "Chybí ts= (point-in-time), nebo from=&to= (okno)")
        if window and from_ts is not None and to_ts is not None and from_ts > to_ts:
            raise HTTPException(422, "from musí být ≤ to")
        profile_variant = _parse_enum(ProfileVariant, variant, "variant")
        frame = repository.session_frame(lambda d: repository.snapshots(symbol, expiry, d), date)
        # Konec okna (t2 v budoucnosti se clampne na poslední snapshot)
        reference = ts if ts is not None else to_ts
        assert reference is not None  # guardy výše: buď ts, nebo from+to
        eligible = frame[frame["ts_min"] <= reference]
        if eligible.empty:
            raise HTTPException(404, f"Před {reference.isoformat()} není žádný snapshot")
        minute = eligible["ts_min"].max()
        rows = eligible[eligible["ts_min"] == minute].dropna(subset=["delta"])

        # Začátek okna: poslední snapshot ≤ from; nic před from → baseline 0
        # (okno „od začátku seance"). Kontrakt bez řádku v t1 (přibyl s posunem
        # obálky) má baseline 0 taky.
        baseline: dict[tuple[float, str], float] = {}
        baseline_minute: pd.Timestamp | None = None
        if window:
            before = frame[frame["ts_min"] <= from_ts]
            if not before.empty:
                baseline_minute = before["ts_min"].max()
                base_rows = before[before["ts_min"] == baseline_minute]
                baseline = {
                    (float(row.strike), str(row.right)): (
                        0.0 if math.isnan(row.volume) else float(row.volume)
                    )
                    for row in base_rows.itertuples()
                }

        if spot is None:
            spot = _spot_at(repository, symbol, date, reference)
        if spot is None:
            raise HTTPException(
                422, "Chybí spot: dodej ?spot= nebo ulož bary podkladu (derived/bars)"
            )

        def row_volume(row: Any) -> float:
            total = 0.0 if math.isnan(row.volume) else float(row.volume)
            if not window:
                return total
            # Korekce v datech můžou dát záporné okno — clamp na 0 (posudek #483)
            return max(0.0, total - baseline.get((float(row.strike), str(row.right)), 0.0))

        inputs = [
            ProfileInput(
                strike=float(row.strike),
                right=str(row.right),
                volume=row_volume(row),
                oi=float(row.oi) if not math.isnan(row.oi) else 0.0,
                delta=float(row.delta),
            )
            for row in rows.itertuples()
        ]
        result = compute_profile(inputs, profile_variant, spot, oi_weight=oi_weight)
        payload: dict[str, object] = {
            "ts": minute.isoformat(),
            "spot": spot,
            "variant": profile_variant.value,
            "profile": [vars(item) for item in result],
        }
        if window:
            payload["from_ts"] = baseline_minute.isoformat() if baseline_minute is not None else None  # noqa: E501 # fmt: skip
            payload["to_ts"] = minute.isoformat()
            payload["oi_static"] = True
            # Stale buňky k t2 — diff stale buňky lže nulou (posudek #483)
            payload["stale_count"] = int((rows["stale_age"] > 0).sum())
        return payload

    @app.get("/chain/{symbol}/{expiry}")
    def chain(symbol: str, expiry: str, date: dt.date) -> dict[str, object]:
        """Greeks & OI tabulka (#202): per-strike řetěz z poslední minuty snapshotů.

        Řádek na strike se stranami C/P (bid/ask/last/vol/IV/Δ/Γ/Θ/V/OI + stale)
        a ΔOI vs. poslední archivovaný den (věčný OI archiv, R4).
        """
        frame = repository.session_frame(lambda d: repository.snapshots(symbol, expiry, d), date)
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
    def flow(
        symbol: str,
        date: dt.date,
        from_ts: Annotated[dt.datetime | None, Query(alias="from")] = None,
        to_ts: Annotated[dt.datetime | None, Query(alias="to")] = None,
    ) -> dict[str, object]:
        """Řady Vol (podklad), OptVol (opce) a CumΔ pro spodní panely (SPEC kap. 6).

        S from=&to= (#483) navíc okenní souhrn: CumΔ_okno = CumΔ(t2) − CumΔ(t1)
        (kotva na open seance se rozdílem odečte), Vol podkladu a OptVol jako
        součty přes minuty okna (t1, t2].
        """
        flow_frame = repository.session_frame(lambda d: repository.flow(symbol, d), date)
        flow_records = _records(flow_frame)
        opt_vol = _opt_vol_series(repository, symbol, date)
        vol = _underlying_vol_series(repository, symbol, date)
        payload: dict[str, object] = {"flow": flow_records, "opt_vol": opt_vol, "vol": vol}
        if from_ts is not None or to_ts is not None:
            if from_ts is None or to_ts is None or from_ts > to_ts:
                raise HTTPException(422, "Okenní mód vyžaduje from ≤ to")

            def cum_at(limit: dt.datetime) -> float:
                eligible = flow_frame[flow_frame["ts_min"] <= limit]
                if eligible.empty:
                    return 0.0  # před prvním záznamem = od začátku seance
                value = eligible.iloc[-1]["cum_delta"]
                return 0.0 if pd.isna(value) else float(value)

            def series_sum(records: list[dict[str, object]], key: str) -> float:
                total = 0.0
                for record in records:
                    ts_min = record.get("ts_min")
                    if not isinstance(ts_min, str):
                        continue
                    stamp = dt.datetime.fromisoformat(ts_min)
                    if from_ts < stamp <= to_ts:
                        value = record.get(key)
                        if isinstance(value, (int, float)):
                            total += float(value)
                return total

            payload["window"] = {
                "from_ts": from_ts.isoformat(),
                "to_ts": to_ts.isoformat(),
                "cum_delta": cum_at(to_ts) - cum_at(from_ts),
                "vol": series_sum(vol, "vol"),
                "opt_vol": series_sum(opt_vol, "opt_vol"),
            }
        return payload

    @app.get("/replay/{symbol}/{expiry}/{date}")
    def replay(
        symbol: str,
        expiry: str,
        date: dt.date,
        if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
    ) -> Response:
        """Kompletní denní balík pro playback (SPEC kap. 6).

        Snapshot matice jde surová (base64 Arrow) — klient přepíná módy/škály
        lokálně bez dalších requestů (latence < 100 ms, SPEC kap. 8).

        HTTP cache (#514): uzavřená seance (konec seance < teď) je immutable —
        prohlížeč historické dny drží a nestahuje 12MB balík znovu. Živý den
        dostává ETag z poslední minuty snapshotů → 304 při shodě.
        """
        # Osa obchodního dne = Globex seance (#512, ADR-0023 bod 3): každá
        # řada se sešívá z partice D−1 (večer) + D; gexfield/gexfieldfa drží
        # jen poslední stav pole, sešití nemá smysl — čtou se beze změny.
        session = repository.session_frame
        snapshots_frame = session(lambda d: repository.snapshots(symbol, expiry, d), date)
        _, session_end = session_bounds(date)
        immutable = dt.datetime.now(dt.UTC) >= session_end
        if immutable:
            cache_headers = {"Cache-Control": "public, max-age=31536000, immutable"}
        else:
            last_ts = (
                snapshots_frame["ts_min"].max().isoformat() if not snapshots_frame.empty else "0"
            )
            etag = f'W/"{symbol}-{expiry}-{date.isoformat()}-{len(snapshots_frame)}-{last_ts}"'
            if if_none_match == etag:
                return Response(status_code=304, headers={"ETag": etag})
            cache_headers = {"Cache-Control": "no-cache", "ETag": etag}
        bundle: dict[str, object] = {
            "symbol": symbol,
            "expiry": expiry,
            "date": date.isoformat(),
            "snapshots_arrow_base64": base64.b64encode(
                frame_to_arrow_bytes(snapshots_frame)
            ).decode("ascii"),
        }
        readers: list[tuple[str, Callable[[], pd.DataFrame]]] = [
            ("levels", lambda: session(lambda d: repository.levels(symbol, expiry, d), date)),
            ("levels2", lambda: session(lambda d: repository.levels2(symbol, expiry, d), date)),
            ("walldom", lambda: session(lambda d: repository.walldom(symbol, expiry, d), date)),
            ("levelsfa", lambda: session(lambda d: repository.levelsfa(symbol, expiry, d), date)),
            ("ladder", lambda: session(lambda d: repository.ladder(symbol, expiry, d), date)),
            (
                "oimissing",
                lambda: session(lambda d: repository.oi_missing(symbol, expiry, d), date),
            ),
            # Catch-up minuty (#518, ADR-0024): první sweep po startu uprostřed dne
            ("catchup", lambda: session(lambda d: repository.catch_up(symbol, expiry, d), date)),
            (
                "gexprofile",
                lambda: session(lambda d: repository.gexprofile(symbol, expiry, d), date),
            ),
            ("gexfield", lambda: repository.gexfield(symbol, expiry, date)),
            # Flow-adjusted zdroj (#232): OI odhad + FA varianta Dyn GEX vrstev
            ("oiest", lambda: session(lambda d: repository.oiest(symbol, expiry, d), date)),
            (
                "gexprofilefa",
                lambda: session(
                    lambda d: repository.gexprofile(symbol, expiry, d, subdir="gexprofilefa"),
                    date,
                ),
            ),
            (
                "gexfieldfa",
                lambda: repository.gexfield(symbol, expiry, date, subdir="gexfieldfa"),
            ),
            ("flow", lambda: session(lambda d: repository.flow(symbol, d), date)),
            ("bars", lambda: session(lambda d: repository.bars(symbol, d), date)),
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
        return JSONResponse(bundle, headers=cache_headers)

    @app.get("/gexplane/{symbol}/{expiry}")
    def gexplane(
        symbol: str, expiry: str, greek: str, date: dt.date | None = None
    ) -> dict[str, object]:
        """Charm/vanna plocha pro Dyn dropdown (#204) — načítá se až při volbě.

        Gamma jede v /replay balíku a WS jako dřív; charm/vanna se přenáší jen
        když je zobrazená („only the displayed greek is transferred").
        """
        if greek not in ("charm", "vanna"):
            raise HTTPException(422, "greek musí být charm nebo vanna")
        day = date or dt.datetime.now(dt.UTC).date()
        out: dict[str, object] = {
            "symbol": symbol,
            "expiry": expiry,
            "date": day.isoformat(),
            "greek": greek,
        }
        try:
            out["profiles"] = _records(
                repository.session_frame(
                    lambda d: repository.gexprofile(symbol, expiry, d, subdir=f"{greek}profile"),
                    day,
                )
            )
        except PartitionNotFoundError:
            out["profiles"] = []
        try:
            out["field"] = _records(
                repository.gexfield(symbol, expiry, day, subdir=f"{greek}field")
            )
        except PartitionNotFoundError:
            out["field"] = []
        return out

    @app.websocket("/ws/live")
    async def ws_live(websocket: WebSocket) -> None:
        """Live push kanálů (SPEC kap. 6): subscribe/unsubscribe protokol zprávami."""
        # CORS se na WS handshake nevztahuje — bez téhle kontroly by živý
        # positioning četla libovolná stránka otevřená v prohlížeči (#542 H1)
        if not origin_allowed(
            websocket.headers.get("origin"),
            websocket.headers.get("host"),
            allowed_origins,
        ):
            await websocket.close(code=1008, reason="Nepovolený Origin")
            return
        try:
            subscriber_id, queue = live_hub.register()
        except TooManySubscribers:
            await websocket.close(code=1013, reason="Příliš mnoho spojení")
            return
        await websocket.accept()

        async def sender() -> None:
            while True:
                await websocket.send_json(await queue.get())

        sender_task = asyncio.create_task(sender())
        try:
            while True:
                request = await websocket.receive_json()
                action = request.get("action")
                try:
                    channels = parse_channels(request.get("channels", []))
                except ValueError as exc:
                    await websocket.send_json({"type": "error", "detail": str(exc)})
                    continue
                if action == "subscribe":
                    try:
                        subscribed = live_hub.subscribe(subscriber_id, channels)
                    except TooManyChannels as exc:
                        await websocket.send_json({"type": "error", "detail": str(exc)})
                        continue
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
        bars = repository.session_frame(lambda d: repository.bars(symbol, d), day)
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
            frame = repository.session_frame(
                lambda d: repository.snapshots(symbol, expiry, d),  # noqa: B023
                day,
            )
        except PartitionNotFoundError:
            continue
        if frame.empty:
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
        bars = repository.session_frame(lambda d: repository.bars(symbol, d), day)
    except PartitionNotFoundError:
        return []
    return [
        {"ts_min": row["ts_min"], "vol": row["volume"]}
        for row in _records(bars[["ts_min", "volume"]])
    ]


app = create_app()
