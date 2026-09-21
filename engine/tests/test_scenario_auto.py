"""Automatický scénář dne (#1173 A): port verdiktu, trend, generátor s atrapou API."""

import asyncio
import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine

from gexlens_engine.compute.dayverdict import (
    LevelInputs,
    VerdictInput,
    build_auto_scenario,
    day_verdict,
    turn_levels,
)
from gexlens_engine.compute.trend import Candle, assess_trends, ema, find_pivots
from gexlens_engine.runtime import EngineRuntime
from gexlens_engine.scenario_auto import ScenarioGenerator, gather_context, us_open_ts
from gexlens_engine.storage.parquet_store import LevelsRow
from gexlens_engine.storage.scenarios_store import ScenariosRepository

SESSION = dt.date(2026, 9, 15)
OPEN = us_open_ts(SESSION)  # 13:30 UTC v létě


def _report(higher: str | None, lower: str | None, expected: str | None) -> Any:
    from gexlens_engine.compute.trend import TrendReport

    return TrendReport(higher=higher, lower=lower, expected=expected, by_timeframe=())  # type: ignore[arg-type]


def test_verdikt_zrcadli_frontend_hlasovani() -> None:
    # Vyšší TF dolů (−2), nižší dolů (−1), tendence short (−1), sentiment RiskOff (−1),
    # cena pod PDC (−1), ΔOI převaha put (−1), negativní gamma s trendem dolů (−1) → −8 short
    inp = VerdictInput(
        trend=_report("down", "down", "down"),
        positive_gamma=False,
        tendency_band="short",
        sentiment_state="RiskOff",
        sentiment_unconfirmed=False,
        price=7590.0,
        prev_close=7632.0,
        oi_call_delta=100.0,
        oi_put_delta=900.0,
        oi_call_total=5000.0,
        oi_put_total=6000.0,
        news_before_open=False,
    )
    verdict = day_verdict(inp)
    assert verdict.verdict == "short" and verdict.score == -8
    assert [v.name for v in verdict.votes] == [
        "trend_higher",
        "trend_lower",
        "tendency",
        "sentiment",
        "overnight",
        "oi_delta",
        "gamma",
    ]
    # Pozitivní gamma táhne k nule; nepotvrzený sentiment nehlasuje; ΔOI pod 10 % nehlasuje
    mild = day_verdict(
        VerdictInput(
            trend=_report("up", "range", "up"),
            positive_gamma=True,
            tendency_band="neutral",
            sentiment_state="RiskOn",
            sentiment_unconfirmed=True,
            price=7650.0,
            prev_close=7632.0,
            oi_call_delta=200.0,
            oi_put_delta=100.0,
            oi_call_total=5000.0,
            oi_put_total=6000.0,
            news_before_open=False,
        )
    )
    assert mild.score == 2 and mild.verdict == "none"  # +2 +0 +0 +0 +1 +0 −1
    waiting = day_verdict(
        VerdictInput(
            trend=_report("up", "up", "up"),
            positive_gamma=None,
            tendency_band="strong_long",
            sentiment_state=None,
            sentiment_unconfirmed=False,
            price=None,
            prev_close=None,
            oi_call_delta=None,
            oi_put_delta=None,
            oi_call_total=None,
            oi_put_total=None,
            news_before_open=True,
        )
    )
    assert waiting.score == 5 and waiting.verdict == "wait_news"


def test_trend_port_ema_pivoty_a_smer() -> None:
    assert ema([1, 2, 3, 4], 2) == pytest.approx([1.5, 2.5, 3.5])
    # Rostoucí zigzag: swingy < trend → HH/HL + EMA up
    candles = []
    for i in range(60):
        phase = (i % 8) / 8
        swing = (phase * 2 if phase < 0.5 else (1 - phase) * 2) * 12
        close = 6000 + 2 * i + swing
        candles.append(Candle(high=close + 3, low=close - 3, close=close))
    pivots = find_pivots(candles, 3)
    assert any(p[2] == "high" for p in pivots) and any(p[2] == "low" for p in pivots)
    report = assess_trends({"W": candles, "D": candles, "240": candles, "60": [], "15": []})
    assert report.higher == "up" and report.expected == "up"
    assert report.by_timeframe[3].direction is None  # 1h málo dat


def test_cile_z_urovni_ve_smeru_verdiktu() -> None:
    levels = LevelInputs(
        flip=7620.0,
        call_wall=7650.0,
        put_wall=7550.0,
        centroid=7600.0,
        prev_high=7652.0,
        prev_low=7595.25,
        prev_close=7632.0,
        on_high=7633.25,
        on_low=7576.75,
    )
    ordered = turn_levels(7590.0, levels)
    assert ordered[0].label == "PDL" and ordered[0].role == "odpor"
    short = day_verdict(
        VerdictInput(
            trend=_report("down", "down", "down"),
            positive_gamma=False,
            tendency_band="short",
            sentiment_state=None,
            sentiment_unconfirmed=False,
            price=7590.0,
            prev_close=7632.0,
            oi_call_delta=None,
            oi_put_delta=None,
            oi_call_total=None,
            oi_put_total=None,
            news_before_open=False,
        )
    )
    now = OPEN - dt.timedelta(minutes=15)
    auto = build_auto_scenario(
        short, 7590.0, levels, now=now, deadline_ts=OPEN + dt.timedelta(hours=6, minutes=30)
    )
    assert auto is not None and auto.direction == "short"
    # Podpory pod 7590 od nejbližší: ONL 7576.75, put wall 7550 (PDC/PDL/flip jsou nad cenou)
    assert auto.targets == (7576.75, 7550.0) and auto.target_labels == ("ONL", "Put wall")
    assert auto.path[0] == (now, 7590.0) and auto.path[-1][1] == 7550.0
    # Bez úrovně ve směru → None; verdikt none → None
    assert (
        build_auto_scenario(short, 7500.0, LevelInputs(call_wall=7650.0), now=now, deadline_ts=OPEN)
        is None
    )
    none = day_verdict(
        VerdictInput(None, None, None, None, False, None, None, None, None, None, None, False)
    )
    assert build_auto_scenario(none, 7590.0, levels, now=now, deadline_ts=OPEN) is None


class FakeApi:
    """Atrapa `ApiReader`: odpovědi per cesta, chybějící = výjimka (chybějící vstup)."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((path, params))
        query = params or {}
        key = path if "tf" not in query else f"{path}?tf={query['tf']}"
        if "date" in query:
            key = f"{path}?date={query['date']}"
        if key not in self.routes:
            raise RuntimeError(f"404 {key}")
        return self.routes[key]


def _down_candles() -> list[dict[str, Any]]:
    rows = []
    for i in range(60):
        phase = (i % 8) / 8
        swing = (phase * 2 if phase < 0.5 else (1 - phase) * 2) * 12
        close = 7900 - 3 * i + swing
        rows.append({"high": close + 3, "low": close - 3, "close": close, "partial": False})
    return rows


def test_generator_zalozi_auto_scenar_jednou_pred_openem(tmp_path: Path) -> None:
    db = create_engine(f"sqlite+pysqlite:///{tmp_path / 'db.sqlite'}")
    repo = ScenariosRepository(db)
    repo.ensure_schema()
    candles = _down_candles()
    api = FakeApi(
        {
            **{
                f"/candles/ES?tf={tf}": {"candles": candles} for tf in ("W", "D", "240", "60", "15")
            },
            f"/bars/ES?date={SESSION.isoformat()}": {
                "bars": [
                    {
                        "ts_min": (OPEN - dt.timedelta(hours=3)).isoformat(),
                        "high": 7633.25,
                        "low": 7600.0,
                        "close": 7610.0,
                    },
                    {
                        "ts_min": (OPEN - dt.timedelta(hours=1)).isoformat(),
                        "high": 7605.0,
                        "low": 7576.75,
                        "close": 7590.0,
                    },
                    {
                        "ts_min": (OPEN + dt.timedelta(minutes=5)).isoformat(),
                        "high": 7700.0,
                        "low": 7500.0,
                        "close": 7600.0,
                    },
                ]
            },
            "/instruments/ES/days": {"days": [{"date": "2026-09-14"}, {"date": "2026-09-15"}]},
            "/bars/ES?date=2026-09-14": {
                "bars": [
                    {
                        "ts_min": "2026-09-14T14:00:00+00:00",
                        "high": 7652.0,
                        "low": 7595.25,
                        "close": 7640.0,
                    },
                    {
                        "ts_min": "2026-09-14T19:59:00+00:00",
                        "high": 7640.0,
                        "low": 7630.0,
                        "close": 7632.0,
                    },
                ]
            },
            "/oidelta/ES/20260915": {
                "call_delta": 100.0,
                "put_delta": 900.0,
                "call_total": 5000.0,
                "put_total": 6000.0,
                "days": {"previous": "2026-09-14"},
            },
            "/sentiment/state": {"state": "RiskOff", "unconfirmed": False},
            "/news/upcoming": {"upcoming": []},
        }
    )
    runtime = EngineRuntime.__new__(EngineRuntime)  # jen pole, která generátor čte
    runtime.expiry = "20260915"
    runtime.last_levels = LevelsRow(
        ts_min=OPEN, flip=7620.0, call_wall=7650.0, put_wall=7550.0, centroid=7600.0, total_gex=-5.0
    )
    runtime.tendency_band = "short"
    alerts: list[dict[str, Any]] = []

    class Publisher:
        async def publish(self, channel: str, payload: dict[str, Any]) -> None:
            alerts.append({"channel": channel, **payload})

    generator = ScenarioGenerator("ES", repo, api, Publisher(), minutes_before_open=15)  # type: ignore[arg-type]
    # Před oknem nic; v okně jeden scénář; po openu už ne (a ne podruhé po restartu)
    asyncio.run(generator.on_minute(OPEN - dt.timedelta(minutes=30), 7590.0, runtime))
    assert repo.list_for("ES") == []
    asyncio.run(generator.on_minute(OPEN - dt.timedelta(minutes=10), 7590.0, runtime))
    rows = repo.list_for("ES")
    assert len(rows) == 1
    row = rows[0]
    assert row.source == "auto" and row.targets == [7576.75, 7550.0]
    assert row.rationale is not None and row.rationale["verdict"] == "short"
    assert row.rationale["score"] <= -3 and row.rationale["targets"] == ["ONL", "Put wall"]
    assert row.deadline == SESSION and row.entry == 7590.0 and len(row.path) == 3
    assert alerts and alerts[0]["kind"] == "scenario_created" and "SHORT" in alerts[0]["message"]
    # ONH/ONL jen z barů před openem (bar po openu s low 7500 se nepočítá)
    assert row.targets[0] == 7576.75
    fresh = ScenarioGenerator("ES", repo, api, Publisher(), minutes_before_open=15)  # type: ignore[arg-type]
    asyncio.run(fresh.on_minute(OPEN - dt.timedelta(minutes=5), 7590.0, runtime))
    assert len(repo.list_for("ES")) == 1


def test_gather_context_pri_vypadku_api_hlasi_chybejici_vstupy() -> None:
    api = FakeApi({"/news/upcoming": {"upcoming": []}})
    ctx = asyncio.run(
        gather_context(
            api,  # type: ignore[arg-type]
            "ES",
            "20260915",
            SESSION,
            OPEN - dt.timedelta(minutes=15),
        )
    )
    assert ctx.trend is None and ctx.prev_close is None and ctx.sentiment_state is None
    assert any(item.startswith("candles W") for item in ctx.missing)
    assert any(item.startswith("oidelta") for item in ctx.missing)


def test_settle_close_bere_posledni_rth_bar_ne_globex() -> None:
    """#1241: PDC = close posledního baru US RTH; nedělní Globex partice bez RTH dá None."""
    from gexlens_engine.scenario_auto import _settle_close

    friday = [
        {"ts_min": "2026-09-18T13:30:00+00:00", "close": 7700.0},
        {"ts_min": "2026-09-18T19:59:00+00:00", "close": 7734.5},
        {"ts_min": "2026-09-18T20:30:00+00:00", "close": 7740.0},  # po close, Globex
    ]
    assert _settle_close(friday) == 7734.5
    sunday = [{"ts_min": "2026-09-20T22:30:00+00:00", "close": 7760.0}]
    assert _settle_close(sunday) is None


def test_verdikt_po_utesu_gamma_nehlasuje() -> None:
    """#1241: po útesu ≥ 50 % „pozitivní gamma tlumí“ nesmí vyrušit trend."""
    from gexlens_engine.compute.dayverdict import VerdictInput, day_verdict
    from gexlens_engine.compute.trend import TrendReport

    def inp(cliff: float | None) -> VerdictInput:
        return VerdictInput(
            trend=TrendReport(higher="up", lower="up", expected="up", by_timeframe=()),
            positive_gamma=True,
            tendency_band="short",
            sentiment_state=None,
            sentiment_unconfirmed=False,
            price=30240.0,
            prev_close=30029.5,
            oi_call_delta=None,
            oi_put_delta=None,
            oi_call_total=None,
            oi_put_total=None,
            news_before_open=False,
            cliff_share=cliff,
        )

    plain = day_verdict(inp(None))
    cliffed = day_verdict(inp(0.83))
    gamma_plain = next(v for v in plain.votes if v.name == "gamma")
    gamma_cliffed = next(v for v in cliffed.votes if v.name == "gamma")
    assert gamma_plain.vote == -1 and gamma_cliffed.vote == 0
    assert cliffed.score == plain.score + 1
