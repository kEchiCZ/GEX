"""Automatický scénář dne (#1173, varianta A — rozhodnutí uživatele 15. 9. 2026).

Jednou za seanci, 15 minut před US openem (`GEXLENS_SCENARIO_AUTO_MINUTES_BEFORE_OPEN`),
engine sám sestaví scénář z **verdiktu dne** — stejné hlasování jako Shrnutí
dne v Briefingu (`compute/dayverdict`, port frontendu, týž `rules_version`):
trend TF, tendence, sentiment, cena vs. včerejší close, ΔOI přes noc, gamma
režim. Při verdiktu long/short jsou cíle nejbližší úrovně obratu ve směru
(zdi, flip, těžiště, PDH/PDL/PDC, ONH/ONL, ±EM), cesta vstup → cíl 1 → cíl 2,
termín settle dne. Při `none` / `wait_news` scénář nevznikne a log řekne proč.

Vstupy, které engine nemá v paměti, čte z API (tytéž endpointy jako Briefing):
svíčky per TF, bary dneška a včerejška, ΔOI, stav sentimentu, kalendář —
žádná nová logika, jen stejný výpočet na serveru. Snímek PNG dodá frontend,
jakmile má graf otevřený (`PUT /scenarios/{id}/image`); cesta se kreslí živě
do heatmapy z uložených bodů. Jen dopředu — nic se nedopočítává zpětně.
"""

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from gexlens_engine.compute.dayverdict import (
    VERDICT_RULES_VERSION,
    DayVerdict,
    LevelInputs,
    VerdictInput,
    build_auto_scenario,
    day_verdict,
)
from gexlens_engine.compute.settle import ET_TZ, session_time_utc, settle_ts, trading_session_date
from gexlens_engine.compute.trend import TIMEFRAMES, Candle, TrendReport, assess_trends
from gexlens_engine.runtime import EngineRuntime
from gexlens_engine.storage.scenarios_store import ScenariosRepository

logger = logging.getLogger(__name__)

US_OPEN_LOCAL = dt.time(9, 30)
CANDLE_LIMITS: dict[str, int] = {"W": 60, "D": 120, "240": 120, "60": 120, "15": 120}


def us_open_ts(day: dt.date) -> dt.datetime:
    return session_time_utc(day, US_OPEN_LOCAL.hour, US_OPEN_LOCAL.minute, ET_TZ)


@dataclass
class VerdictContext:
    """Vše, co verdikt a úrovně potřebují — sesbírané z runtime a API."""

    trend: TrendReport | None = None
    prev_high: float | None = None
    prev_low: float | None = None
    prev_close: float | None = None
    on_high: float | None = None
    on_low: float | None = None
    oi_call_delta: float | None = None
    oi_put_delta: float | None = None
    oi_call_total: float | None = None
    oi_put_total: float | None = None
    sentiment_state: str | None = None
    sentiment_unconfirmed: bool = False
    news_before_open: bool = False
    missing: list[str] = field(default_factory=list)


class ApiReader:
    """Čtení vstupů z API stejnými endpointy jako Briefing; chyba = chybějící vstup, ne pád."""

    def __init__(self, base_url: str, timeout_s: float = 10.0) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout_s)

    async def close(self) -> None:
        await self._client.aclose()

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = await self._client.get(path, params=params)
        response.raise_for_status()
        return response.json()


def _candles(rows: list[dict[str, Any]]) -> list[Candle]:
    out: list[Candle] = []
    for row in rows:
        try:
            out.append(
                Candle(
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    partial=bool(row.get("partial", False)),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


async def gather_context(
    api: ApiReader,
    symbol: str,
    expiry: str | None,
    session: dt.date,
    now: dt.datetime,
) -> VerdictContext:
    ctx = VerdictContext()
    # Svíčky per TF → trend (stejný port jako karta Trend)
    by_tf: dict[str, list[Candle]] = {}
    for tf in TIMEFRAMES:
        try:
            payload = await api.get(f"/candles/{symbol}", {"tf": tf, "limit": CANDLE_LIMITS[tf]})
            by_tf[tf] = _candles(payload.get("candles", []))
        except Exception as exc:  # noqa: BLE001 — chybějící TF = „málo dat", ne pád
            ctx.missing.append(f"candles {tf}: {type(exc).__name__}")
    ctx.trend = assess_trends(by_tf) if by_tf else None
    # Bary: dnešní (ONH/ONL do openu) a poslední uložený den před dneškem (PDH/PDL/PDC)
    try:
        today = await api.get(f"/bars/{symbol}", {"date": session.isoformat()})
        open_ts = us_open_ts(session)
        for bar in today.get("bars", []):
            ts = dt.datetime.fromisoformat(str(bar["ts_min"]))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt.UTC)
            if ts >= open_ts:
                break
            high, low = float(bar["high"]), float(bar["low"])
            ctx.on_high = high if ctx.on_high is None else max(ctx.on_high, high)
            ctx.on_low = low if ctx.on_low is None else min(ctx.on_low, low)
    except Exception as exc:  # noqa: BLE001
        ctx.missing.append(f"bars dnes: {type(exc).__name__}")
    try:
        days_payload = await api.get(f"/instruments/{symbol}/days")
        days = sorted(
            str(item["date"]) for item in days_payload.get("days", []) if item.get("date")
        )
        before = [day for day in days if day < session.isoformat()]
        if before:
            prev = await api.get(f"/bars/{symbol}", {"date": before[-1]})
            for bar in prev.get("bars", []):
                high, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])
                ctx.prev_high = high if ctx.prev_high is None else max(ctx.prev_high, high)
                ctx.prev_low = low if ctx.prev_low is None else min(ctx.prev_low, low)
                ctx.prev_close = close
        else:
            ctx.missing.append("bary včerejška: žádný uložený den")
    except Exception as exc:  # noqa: BLE001
        ctx.missing.append(f"bars včera: {type(exc).__name__}")
    if expiry:
        try:
            oi = await api.get(f"/oidelta/{symbol}/{expiry}")
            days_info = oi.get("days") or {}
            if days_info.get("previous"):
                ctx.oi_call_delta = _number(oi.get("call_delta"))
                ctx.oi_put_delta = _number(oi.get("put_delta"))
                ctx.oi_call_total = _number(oi.get("call_total"))
                ctx.oi_put_total = _number(oi.get("put_total"))
        except Exception as exc:  # noqa: BLE001
            ctx.missing.append(f"oidelta: {type(exc).__name__}")
    try:
        state = await api.get("/sentiment/state", {"symbol": symbol})
        if isinstance(state, dict) and state.get("state"):
            ctx.sentiment_state = str(state["state"])
            ctx.sentiment_unconfirmed = bool(state.get("unconfirmed", False))
    except Exception as exc:  # noqa: BLE001
        ctx.missing.append(f"sentiment: {type(exc).__name__}")
    try:
        upcoming = await api.get("/news/upcoming", {"hours": 24})
        open_ts = us_open_ts(session)
        for row in upcoming.get("upcoming", []):
            ts = dt.datetime.fromisoformat(str(row.get("ts_event")))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt.UTC)
            raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
            impact = raw.get("impact")
            high_impact = (
                str(impact).lower() == "high"
                if isinstance(impact, str)
                else int(row.get("importance") or 0) >= 3
            )
            if high_impact and now < ts <= open_ts:
                ctx.news_before_open = True
                break
    except Exception as exc:  # noqa: BLE001
        ctx.missing.append(f"kalendář: {type(exc).__name__}")
    return ctx


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


@dataclass
class ScenarioGenerator:
    """Jednou za seanci před US openem založí automatický scénář symbolu."""

    symbol: str
    repository: ScenariosRepository
    api: ApiReader
    publisher: Any = None
    minutes_before_open: int = 15

    _done_for: dt.date | None = field(default=None, init=False)

    async def on_minute(self, now: dt.datetime, spot: float | None, runtime: EngineRuntime) -> None:
        session = trading_session_date(now)
        if self._done_for == session:
            return
        fire_at = us_open_ts(session) - dt.timedelta(minutes=self.minutes_before_open)
        if now < fire_at or now >= us_open_ts(session):
            return  # okno: [open − N min, open) — po openu už dnes ne
        self._done_for = session  # jeden pokus per seance i při chybě
        if spot is None:
            logger.warning("%s: automatický scénář — bez spotu, přeskočeno", self.symbol)
            return
        if await asyncio.to_thread(self.repository.exists_auto, self.symbol, session):
            return  # restart enginu uprostřed okna
        ctx = await gather_context(self.api, self.symbol, runtime.expiry, session, now)
        levels = runtime.last_levels
        verdict = day_verdict(
            VerdictInput(
                trend=ctx.trend,
                positive_gamma=(levels.total_gex >= 0) if levels else None,
                tendency_band=runtime.tendency_band,
                sentiment_state=ctx.sentiment_state,
                sentiment_unconfirmed=ctx.sentiment_unconfirmed,
                price=spot,
                prev_close=ctx.prev_close,
                oi_call_delta=ctx.oi_call_delta,
                oi_put_delta=ctx.oi_put_delta,
                oi_call_total=ctx.oi_call_total,
                oi_put_total=ctx.oi_put_total,
                news_before_open=ctx.news_before_open,
            )
        )
        rationale = _rationale(verdict, ctx)
        if verdict.verdict not in ("long", "short"):
            logger.info(
                "%s: automatický scénář dnes nevzniká — verdikt %s (skóre %+d)%s",
                self.symbol,
                verdict.verdict,
                verdict.score,
                f"; chybí: {', '.join(ctx.missing)}" if ctx.missing else "",
            )
            return
        em_anchor, em_points = await asyncio.to_thread(
            self.repository.em_reference, self.symbol, session
        )
        deadline_ts = settle_ts(session)
        auto = build_auto_scenario(
            verdict,
            spot,
            LevelInputs(
                flip=levels.flip if levels else None,
                call_wall=levels.call_wall if levels else None,
                put_wall=levels.put_wall if levels else None,
                centroid=levels.centroid if levels else None,
                prev_high=ctx.prev_high,
                prev_low=ctx.prev_low,
                prev_close=ctx.prev_close,
                on_high=ctx.on_high,
                on_low=ctx.on_low,
                em_anchor=em_anchor,
                em_points=em_points,
            ),
            now=now,
            deadline_ts=deadline_ts,
        )
        if auto is None:
            logger.info(
                "%s: verdikt %s, ale žádná úroveň obratu ve směru — scénář nevzniká",
                self.symbol,
                verdict.verdict,
            )
            return
        rationale["targets"] = list(auto.target_labels)
        scenario_id = await asyncio.to_thread(
            self.repository.create,
            symbol=self.symbol,
            day=session,
            created_at=now,
            deadline=session,
            deadline_ts=deadline_ts,
            entry=spot,
            targets=list(auto.targets),
            path=[{"ts": ts.isoformat(), "price": price} for ts, price in auto.path],
            annotation_id=None,
            note=None,
            source="auto",
            rationale=rationale,
        )
        summary = (
            f"Scénář dne #{scenario_id} ({'LONG' if auto.direction == 'long' else 'SHORT'}, "
            f"skóre {verdict.score:+d}): vstup {spot:g} → "
            + " → ".join(
                f"{label} {price:g}"
                for label, price in zip(auto.target_labels, auto.targets, strict=True)
            )
            + f", termín settle {session.isoformat()}"
        )
        logger.info("%s: %s", self.symbol, summary)
        if self.publisher is not None:
            await self.publisher.publish(
                "alerts",
                {
                    "kind": "scenario_created",
                    "symbol": self.symbol,
                    "message": summary,
                    "scenario_id": scenario_id,
                    "ts": now.timestamp(),
                },
            )


def _rationale(verdict: DayVerdict, ctx: VerdictContext) -> dict[str, Any]:
    return {
        "rules_version": VERDICT_RULES_VERSION,
        "verdict": verdict.verdict,
        "score": verdict.score,
        "votes": [vote.as_dict() for vote in verdict.votes],
        "missing": list(ctx.missing),
    }
