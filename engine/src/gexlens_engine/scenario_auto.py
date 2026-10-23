"""Automatický scénář dne (#1173, varianta A — rozhodnutí uživatele 15. 9. 2026).

Jednou za obchodní seanci (po–pá, `settle.is_trading_session`; víkend nemá US open
ani settle — #1309), v okně před US openem (`GEXLENS_SCENARIO_AUTO_MINUTES_BEFORE_OPEN`),
engine sám sestaví scénář z **verdiktu dne**, který v téže minutě spočítal a uložil
zapisovač (`verdict_writer`, #1595 d1 — API se čte jednou): trend TF, tendence,
sentiment, cena vs. včerejší close, ΔOI přes noc, gamma režim (`compute/dayverdict`).
Scénář vznikne v první minutě okna s **úplným** verdiktem — výpadek čtení API nebo
restart bez tendence ho jen odloží na další minutu okna. Při verdiktu long/short
jsou cíle nejbližší úrovně obratu ve směru (zdi, flip, těžiště, PDH/PDL/PDC,
ONH/ONL, ±EM), cesta vstup → cíl 1 → cíl 2, termín settle dne. Při `none` /
`wait_news` scénář nevznikne a log řekne proč.

Vstupy, které engine nemá v paměti, čte `gather_context` z API (tytéž endpointy
jako Briefing): svíčky per TF, bary dneška a včerejška, ΔOI, stav sentimentu,
kalendář. Snímek PNG dodá frontend, jakmile má graf otevřený
(`PUT /scenarios/{id}/image`); cesta se kreslí živě do heatmapy z uložených bodů.
Jen dopředu — nic se nedopočítává zpětně.
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
)
from gexlens_engine.compute.settle import (
    ET_TZ,
    is_trading_session,
    settle_ts,
    trading_session_date,
    us_open_ts,
)
from gexlens_engine.compute.trend import TIMEFRAMES, Candle, TrendReport, assess_trends
from gexlens_engine.runtime import EngineRuntime
from gexlens_engine.storage.scenarios_store import ScenariosRepository

logger = logging.getLogger(__name__)

#: Počty svíček per TF — táž tabulka jako karta Trend v Briefingu (`frontend/src/api/
#: briefing.ts`, #1595 b): EMA50 startuje z průměru prvních 50, počet svíček ji posouvá
CANDLE_LIMITS: dict[str, int] = {"W": 80, "D": 120, "240": 120, "60": 120, "15": 120}


@dataclass
class VerdictContext:
    """Vše, co verdikt a úrovně potřebují — sesbírané z runtime a API."""

    trend: TrendReport | None = None
    prev_high: float | None = None
    prev_low: float | None = None
    prev_close: float | None = None
    #: Útes gammy poslední uzavřené seance (#1241)
    cliff_share: float | None = None
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
    #: Čtení vstupů z API, která selhala (výpadek) — jiné než „data nejsou"
    failures: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return bool(self.failures)

    def fail(self, what: str, exc: Exception) -> None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        reason = f"{what}: {f'HTTP {status}' if status else type(exc).__name__}"
        self.missing.append(reason)
        self.failures.append(reason)


@dataclass(frozen=True)
class PreOpenVerdict:
    """Verdikt minuty před openem od zapisovače (#1595) — scénář ho přebírá."""

    verdict: DayVerdict
    context: VerdictContext
    #: Cena, se kterou verdikt hlasoval (overnight) — vstup scénáře; None = živá
    #: cena chyběla (pod v6, #1600), verdikt je pak neúplný a scénář nevznikne
    spot: float | None
    #: Chybějící vstupy (#1595 e1): selhaná čtení API, levels a tendence z runtime
    missing: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.missing


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


def _settle_close(bars: list[dict[str, Any]]) -> float | None:
    """Close posledního baru v US RTH (před settle New York) — PDC, ne poslední Globex minuta.

    Hranice `settle_ts`, ne 16:00 natvrdo: po zkrácené seanci (13:00 ET, ADR-0046)
    by PDC vzalo bar 13:14 ET, který už do seance nepatří (#1595).
    """
    settle: float | None = None
    for bar in bars:
        ts = dt.datetime.fromisoformat(str(bar["ts_min"]))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.UTC)
        day = ts.astimezone(ET_TZ).date()
        # Obchodní den z jediného predikátu (#1309) — víkendový bar PDC není
        if is_trading_session(day) and us_open_ts(day) <= ts < settle_ts(day):
            settle = float(bar["close"])
    return settle


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
            ctx.fail(f"candles {tf}", exc)
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
        ctx.fail("bars dnes", exc)
    try:
        days_payload = await api.get(f"/instruments/{symbol}/days")
        days = sorted(
            str(item["date"]) for item in days_payload.get("days", []) if item.get("date")
        )
        before = [day for day in days if day < session.isoformat()]
        # PDC = settle poslední seance s US close (#1241): pondělní „včerejšek“ je
        # nedělní Globex partice bez RTH — bral se její poslední bar, ne páteční
        # settle; zpět nejvýš 4 partice (dlouhý víkend)
        found = False
        for day in reversed(before[-4:]):
            prev = await api.get(f"/bars/{symbol}", {"date": day})
            bars = prev.get("bars", [])
            settle = _settle_close(bars)
            if settle is None:
                continue
            for bar in bars:
                high, low = float(bar["high"]), float(bar["low"])
                ctx.prev_high = high if ctx.prev_high is None else max(ctx.prev_high, high)
                ctx.prev_low = low if ctx.prev_low is None else min(ctx.prev_low, low)
            ctx.prev_close = settle
            found = True
            break
        if before and not found:
            ctx.missing.append("bary včerejška: žádná partice s US close")
        if not before:
            ctx.missing.append("bary včerejška: žádný uložený den")
    except Exception as exc:  # noqa: BLE001
        ctx.fail("bars včera", exc)
    try:
        cliff = await api.get(f"/gammacliff/{symbol}", {"limit": 1})
        rows = cliff.get("rows") or cliff.get("history") or []
        if rows:
            share = rows[0].get("cliff_share")
            ctx.cliff_share = float(share) if share is not None else None
    except Exception as exc:  # noqa: BLE001
        ctx.fail("gamma útes", exc)
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
            ctx.fail("oidelta", exc)
    try:
        state = await api.get("/sentiment/state", {"symbol": symbol})
        if isinstance(state, dict) and state.get("state"):
            ctx.sentiment_state = str(state["state"])
            ctx.sentiment_unconfirmed = bool(state.get("unconfirmed", False))
    except Exception as exc:  # noqa: BLE001
        ctx.fail("sentiment", exc)
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
        ctx.fail("kalendář", exc)
    return ctx


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def verdict_input(ctx: VerdictContext, spot: float | None, runtime: EngineRuntime) -> VerdictInput:
    """Vstup hlasování z kontextu API a stavu runtime téže minuty (#1173 A, #1595)."""
    levels = runtime.last_levels
    return VerdictInput(
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
        cliff_share=ctx.cliff_share,
        thin_map=runtime.thin_map,
    )


@dataclass
class ScenarioGenerator:
    """Jednou za seanci před US openem založí automatický scénář symbolu."""

    symbol: str
    repository: ScenariosRepository
    publisher: Any = None
    minutes_before_open: int = 15

    _done_for: dt.date | None = field(default=None, init=False)

    async def on_minute(
        self, now: dt.datetime, pre_open: PreOpenVerdict | None, runtime: EngineRuntime
    ) -> None:
        """`pre_open` = verdikt téže minuty od zapisovače (None: minuta bez výpočtu)."""
        session = trading_session_date(now)
        if self._done_for == session or not is_trading_session(session):
            return  # víkend: den bez US openu i settle — scénář nevzniká (#1309)
        open_ts = us_open_ts(session)
        if now < open_ts - dt.timedelta(minutes=self.minutes_before_open) or now >= open_ts:
            return  # okno: [open − N min, open) — po openu už dnes ne
        if pre_open is None:
            return  # zapisovač tuto minutu nepočítal (bez ceny, cyklus až po openu)
        verdict, ctx, spot = pre_open.verdict, pre_open.context, pre_open.spot
        if not pre_open.complete or spot is None:
            # Výpadek čtení API nebo restart bez tendence: scénář počká na úplný verdikt
            last_minute = now >= open_ts - dt.timedelta(minutes=1)
            logger.log(
                logging.WARNING if last_minute else logging.INFO,
                "%s: automatický scénář %s — verdikt neúplný (chybí: %s)",
                self.symbol,
                "dnes nevznikne" if last_minute else "čeká na další minutu",
                ", ".join(pre_open.missing),
            )
            return
        self._done_for = session  # jeden pokus per seance s úplným verdiktem
        if await asyncio.to_thread(self.repository.exists_auto, self.symbol, session):
            return  # restart enginu uprostřed okna
        levels = runtime.last_levels
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
