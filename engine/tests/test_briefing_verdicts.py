"""Vyhodnocení verdiktů dne po settle (#1091): OHLC seance, zásah, kolektor, statistiky."""

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, insert, select

from gexlens_engine.briefing_verdicts import (
    BriefingVerdictCollector,
    SessionOhlc,
    evaluate_verdict,
    session_ohlc,
)
from gexlens_engine.compute.settle import ET_TZ, session_bounds, session_time_utc, settle_ts
from gexlens_engine.config import Settings
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.storage.briefing_verdicts_store import (
    BriefingVerdictRepository,
    verdict_stats,
)
from gexlens_engine.storage.emrespect_store import EmRespectRepository, em_respect_table
from gexlens_engine.storage.meta import briefing_verdicts_table
from gexlens_engine.storage.parquet_store import SnapshotWriter

SESSION = dt.date(2026, 7, 14)  # úterý, letní čas: Globex open 22:00 UTC, US open 13:30 UTC


def _bars(session: dt.date) -> list[Bar]:
    """Globex open 7000 → US open 7010 → settle close 7040; po settle ještě bar 7100 (ignoruje se)."""  # noqa: E501
    start, _ = session_bounds(session)
    us_open = session_time_utc(session, 9, 30, ET_TZ)
    settle = settle_ts(session)
    rows = [
        (start, 7000.0, 7001.0),
        (start + dt.timedelta(hours=5), 7001.0, 6995.0),
        (us_open, 7010.0, 7012.0),
        (settle - dt.timedelta(minutes=1), 7035.0, 7040.0),
        (settle + dt.timedelta(minutes=30), 7100.0, 7100.0),
    ]
    return [
        Bar(ts=ts, open=o, high=max(o, c) + 1, low=min(o, c) - 1, close=c, volume=10.0)
        for ts, o, c in rows
    ]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(
        data_dir=tmp_path,
        database_url=f"sqlite+pysqlite:///{tmp_path / 'meta.sqlite'}",
    )
    SnapshotWriter(s).write_bars_by_day("ES", _bars(SESSION))
    return s


def test_session_ohlc_globex_open_us_open_close_do_settle(settings: Settings) -> None:
    ohlc = session_ohlc(settings.data_dir, "ES", SESSION)
    assert ohlc == SessionOhlc(open=7000.0, us_open=7010.0, close=7040.0)
    assert session_ohlc(settings.data_dir, "ES", SESSION + dt.timedelta(days=30)) is None


def test_evaluate_verdict_pravidla() -> None:
    ohlc = SessionOhlc(open=7000.0, us_open=7010.0, close=7040.0)
    long = evaluate_verdict("long", ohlc, em_points=40.0)
    assert long.hit is True and long.move_pts == 30.0 and long.move_em == pytest.approx(0.75)
    assert evaluate_verdict("short", ohlc, 40.0).hit is False
    # Bez převahy = range den jen do ±0,5 EM; bez EM se nehodnotí
    assert evaluate_verdict("none", ohlc, 40.0).hit is False
    assert evaluate_verdict("none", ohlc, 100.0).hit is True
    assert evaluate_verdict("none", ohlc, None).hit is None
    assert evaluate_verdict("wait_news", ohlc, 40.0).hit is None
    # Bez US openu se měří od Globex openu
    assert evaluate_verdict("long", SessionOhlc(7000.0, None, 6990.0), None).move_pts == -10.0


@pytest.mark.asyncio
async def test_kolektor_doplni_vysledek_po_settle(settings: Settings) -> None:
    engine = create_engine(settings.database_url)
    repository = BriefingVerdictRepository(engine)
    repository.ensure_schema()
    EmRespectRepository(engine).ensure_schema()
    now0 = dt.datetime(2026, 7, 14, 12, 0, tzinfo=dt.UTC)
    with engine.begin() as conn:
        conn.execute(
            insert(briefing_verdicts_table).values(
                session_date=SESSION,
                symbol="ES",
                verdict="long",
                score=4,
                votes=[{"name": "trend_higher", "vote": 2, "reason": "x"}],
                rules_version=1,
                created_at=now0,
                updated_at=now0,
            )
        )
        conn.execute(
            insert(em_respect_table).values(
                session_date=SESSION,
                symbol="ES",
                ref_ts=None,
                em_source="straddle",
                anchor=7010.0,
                atm_strike=7010.0,
                em_points=40.0,
                high=7045.0,
                low=6990.0,
                close=7040.0,
                close_in_band=True,
                touch_upper=False,
                touch_lower=False,
                range_vs_em=1.4,
                negative_gamma_share=None,
                version=1,
                computed_at=now0,
            )
        )
    collector = BriefingVerdictCollector(
        symbol="ES", repository=repository, db=engine, data_dir=settings.data_dir
    )
    # Před settle nic
    await collector.on_minute(settle_ts(SESSION) - dt.timedelta(minutes=5))
    assert repository.pending("ES", before=SESSION + dt.timedelta(days=1))
    # Po settle + grace doplní výsledek; druhé volání téže seance nic nedělá
    await collector.on_minute(settle_ts(SESSION) + dt.timedelta(minutes=20))
    with engine.connect() as conn:
        row = conn.execute(select(briefing_verdicts_table)).mappings().one()
    assert row["outcome_hit"] is True
    assert row["outcome_us_open"] == 7010.0 and row["outcome_close"] == 7040.0
    assert row["outcome_move_em"] == pytest.approx(0.75)
    assert not repository.pending("ES", before=SESSION + dt.timedelta(days=1))


def test_verdict_stats_per_verdikt_a_slozku() -> None:
    rows: list[dict[str, Any]] = [
        {
            "verdict": "long",
            "outcome_hit": True,
            "outcome_move_pts": 30.0,
            "votes": [{"name": "trend_higher", "vote": 2}, {"name": "sentiment", "vote": -1}],
        },  # fmt: skip
        {
            "verdict": "long",
            "outcome_hit": False,
            "outcome_move_pts": -10.0,
            "votes": [{"name": "trend_higher", "vote": 2}],
        },  # fmt: skip
        {
            "verdict": "wait_news",
            "outcome_hit": None,
            "outcome_move_pts": 5.0,
            "votes": [{"name": "sentiment", "vote": 1}],
        },  # fmt: skip
        {
            "verdict": "none",
            "outcome_hit": True,
            "outcome_move_pts": 0.0,
            "votes": [{"name": "gamma", "vote": 0}],
        },  # fmt: skip
    ]
    stats = verdict_stats(rows)
    assert stats["evaluated"] == 3
    assert stats["by_verdict"]["long"] == {"n": 2, "hits": 1, "unscored": 0, "hit_rate": 0.5, "wilson_lb": pytest.approx(0.095, abs=0.01), "gate_open": False}  # fmt: skip  # noqa: E501
    assert stats["by_verdict"]["wait_news"]["unscored"] == 1
    # trend_higher +2 souhlasil 1× ze 2; sentiment: −1 při +30 (ne), +1 při +5 (ano)
    assert stats["by_vote"]["trend_higher"]["hits"] == 1 and stats["by_vote"]["trend_higher"]["n"] == 2  # fmt: skip  # noqa: E501
    assert stats["by_vote"]["sentiment"]["hits"] == 1 and stats["by_vote"]["sentiment"]["n"] == 2
    assert "gamma" not in stats["by_vote"]  # nulový hlas a nulový pohyb se nehodnotí
