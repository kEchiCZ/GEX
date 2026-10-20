"""Vyhodnocení verdiktů dne po settle (#1091): OHLC seance, zásah, kolektor, statistiky."""

import datetime as dt
import os
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, insert, inspect, select, text, update

from gexlens_engine.briefing_verdicts import (
    BriefingVerdictCollector,
    SessionOhlc,
    evaluate_verdict,
    session_ohlc,
)
from gexlens_engine.compute.settle import (
    ET_TZ,
    session_bounds,
    session_time_utc,
    settle_ts,
    us_open_ts,
)
from gexlens_engine.config import Settings
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.storage.briefing_verdicts_store import (
    BriefingVerdictRepository,
    VerdictFrozenError,
    VerdictOutcome,
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


@pytest.mark.asyncio
async def test_kolektor_o_vikendu_nevyhodnocuje(tmp_path: Path) -> None:
    """#1309 (vzor scénářů): `trading_session_date` vrací o víkendu kalendářní
    den bez seance a kolektor na něj navázal „settle" — v sobotu 20:20 UTC by
    víkendový verdikt dostal výsledek z barů okna, které nemá US open ani
    settle. Víkend se nevyhodnocuje; pondělí (regrese obchodního dne) ano."""
    saturday, sunday, monday = dt.date(2026, 9, 26), dt.date(2026, 9, 27), dt.date(2026, 9, 28)
    settings = Settings(
        data_dir=tmp_path, database_url=f"sqlite+pysqlite:///{tmp_path / 'meta.sqlite'}"
    )
    writer = SnapshotWriter(settings)
    for day in (saturday, sunday, monday):
        writer.write_bars_by_day("ES", _bars(day))  # bary v okně i o víkendu
    engine = create_engine(settings.database_url)
    repository = BriefingVerdictRepository(engine)
    repository.ensure_schema()
    EmRespectRepository(engine).ensure_schema()
    now0 = dt.datetime(2026, 9, 26, 12, 0, tzinfo=dt.UTC)
    with engine.begin() as conn:
        for day in (saturday, sunday, monday):
            conn.execute(
                insert(briefing_verdicts_table).values(
                    session_date=day,
                    symbol="ES",
                    verdict="long",
                    score=4,
                    votes=[],
                    rules_version=1,
                    created_at=now0,
                    updated_at=now0,
                )
            )
    collector = BriefingVerdictCollector(
        symbol="ES", repository=repository, db=engine, data_dir=settings.data_dir
    )

    await collector.on_minute(settle_ts(saturday) + dt.timedelta(minutes=20))
    await collector.on_minute(settle_ts(sunday) + dt.timedelta(minutes=20))
    assert len(repository.pending("ES", before=monday + dt.timedelta(days=1))) == 3

    await collector.on_minute(settle_ts(monday) + dt.timedelta(minutes=20))
    with engine.connect() as conn:
        rows = conn.execute(select(briefing_verdicts_table)).mappings().all()
    evaluated = {row["session_date"]: row["outcome_computed_at"] is not None for row in rows}
    assert evaluated == {saturday: False, sunday: False, monday: True}


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


# ── Zápis verdiktu z enginu (#1595) ─────────────────────────────


class _Clock:
    def __init__(self, now: dt.datetime) -> None:
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now


def _upsert(
    repository: BriefingVerdictRepository, *, score: int, missing: tuple[str, ...] = ()
) -> bool:
    return repository.upsert_verdict(
        session_date=SESSION,
        symbol="ES",
        verdict="long" if score >= 3 else "none",
        score=score,
        votes=[{"name": "overnight", "vote": 1, "reason": f"skóre {score}"}],
        rules_version=3,
        missing=missing,
    )


def _only_row(engine: Any) -> dict[str, Any]:
    with engine.connect() as conn:
        rows = [dict(row._mapping) for row in conn.execute(select(briefing_verdicts_table))]
    assert len(rows) == 1
    return rows[0]


def test_upsert_verdiktu_jeden_radek_created_at_drzi(settings: Settings) -> None:
    engine = create_engine(settings.database_url)
    clock = _Clock(us_open_ts(SESSION) - dt.timedelta(minutes=15))
    repository = BriefingVerdictRepository(engine, clock=clock)
    repository.ensure_schema()
    assert _upsert(repository, score=4)
    first = _only_row(engine)
    clock.now = us_open_ts(SESSION) - dt.timedelta(minutes=1)
    assert _upsert(repository, score=1)
    row = _only_row(engine)
    assert row["id"] == first["id"] and row["created_at"] == first["created_at"]
    assert row["verdict"] == "none" and row["score"] == 1 and row["votes"][0]["reason"] == "skóre 1"
    # updated_at = okamžik, který stráž ověřila (SQLite vrací čas bez pásma)
    assert row["updated_at"].replace(tzinfo=dt.UTC) == clock.now


def test_upsert_neuplny_prepise_jen_neuplny_radek(settings: Settings) -> None:
    """#1595 e1: neúplný výpočet založí řádek a přepíše neúplný (platí poslední stav),
    úplný řádek nepřepíše nikdy; úplný výpočet přepíše vše."""
    engine = create_engine(settings.database_url)
    clock = _Clock(us_open_ts(SESSION) - dt.timedelta(hours=12))
    repository = BriefingVerdictRepository(engine, clock=clock)
    repository.ensure_schema()
    gap = ("gammacliff: HTTP 500",)
    assert _upsert(repository, score=0, missing=gap)
    clock.now += dt.timedelta(hours=5)
    assert _upsert(repository, score=-1, missing=gap)  # soustavný výpadek: řádek se obnovuje
    row = _only_row(engine)
    assert row["score"] == -1 and row["missing"] == ["gammacliff: HTTP 500"]
    assert row["updated_at"].replace(tzinfo=dt.UTC) == clock.now
    clock.now += dt.timedelta(hours=6)
    assert _upsert(repository, score=4)
    assert _only_row(engine)["missing"] == []
    clock.now += dt.timedelta(minutes=1)
    assert not _upsert(repository, score=-1, missing=("levels: engine je ještě nespočítal",))
    row = _only_row(engine)
    assert row["score"] == 4 and row["missing"] == []


def test_upsert_neuplny_neprepise_radek_pred_1595(settings: Settings) -> None:
    """Řádek z Briefingu (před #1595) nemá `missing` (NULL) — neúplný výpočet ho nepřepíše."""
    engine = create_engine(settings.database_url)
    clock = _Clock(us_open_ts(SESSION) - dt.timedelta(minutes=10))
    repository = BriefingVerdictRepository(engine, clock=clock)
    repository.ensure_schema()
    assert _upsert(repository, score=4)
    with engine.begin() as conn:
        conn.execute(update(briefing_verdicts_table).values(missing=None))
    assert not _upsert(repository, score=0, missing=("bars dnes: HTTP 503",))
    assert _upsert(repository, score=-4)
    assert _only_row(engine)["score"] == -4


def test_ensure_schema_doplni_sloupec_missing(settings: Settings) -> None:
    """Tabulka založená před #1595 dostane aditivní sloupec `missing` (NULL u starých řádků)."""
    engine = create_engine(settings.database_url)
    repository = BriefingVerdictRepository(engine)
    repository.ensure_schema()
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE briefing_verdicts DROP COLUMN missing"))

    def columns() -> set[str]:
        return {col["name"] for col in inspect(engine).get_columns("briefing_verdicts")}

    assert "missing" not in columns()
    repository.ensure_schema()
    assert "missing" in columns()


@pytest.mark.skipif(
    not os.environ.get("GEXLENS_TEST_PG_DSN"),
    reason="GEXLENS_TEST_PG_DSN nenastaveno (integrace s reálným PostgreSQL)",
)
def test_ensure_schema_prevede_outcome_computed_at_na_zonu_postgres() -> None:
    """#1598: stav produkce — `outcome_computed_at` z ALTERu před #1598 je TIMESTAMP bez
    zóny s hodnotami v UTC. Migrace převede typ, okamžik zachová a je idempotentní."""
    engine = create_engine(os.environ["GEXLENS_TEST_PG_DSN"])
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS briefing_verdicts"))
    repository = BriefingVerdictRepository(engine)
    try:
        repository.ensure_schema()
        computed = dt.datetime(2026, 10, 16, 20, 15)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE briefing_verdicts ALTER COLUMN outcome_computed_at "
                    "TYPE TIMESTAMP WITHOUT TIME ZONE"
                )
            )
            conn.execute(
                insert(briefing_verdicts_table).values(
                    session_date=dt.date(2026, 10, 16),
                    symbol="ES",
                    verdict="long",
                    score=4,
                    votes=[],
                    rules_version=3,
                    created_at=dt.datetime(2026, 10, 16, 12, 0, tzinfo=dt.UTC),
                    updated_at=dt.datetime(2026, 10, 16, 12, 0, tzinfo=dt.UTC),
                    outcome_computed_at=computed,
                )
            )

        def column_type() -> str:
            with engine.connect() as conn:
                return str(
                    conn.execute(
                        text(
                            "SELECT data_type FROM information_schema.columns WHERE "
                            "table_name = 'briefing_verdicts' "
                            "AND column_name = 'outcome_computed_at'"
                        )
                    ).scalar_one()
                )

        assert column_type() == "timestamp without time zone"
        for _ in range(2):  # idempotence
            repository.ensure_schema()
            assert column_type() == "timestamp with time zone"
            with engine.connect() as conn:
                stored = conn.execute(select(briefing_verdicts_table.c.outcome_computed_at)).one()
            assert stored[0] == computed.replace(tzinfo=dt.UTC)
    finally:
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS briefing_verdicts"))


def test_upsert_nesaha_na_radek_s_vysledkem(settings: Settings) -> None:
    """Pojistka v DB: řádek s doplněným výsledkem seance se nepřepíše nikdy."""
    engine = create_engine(settings.database_url)
    clock = _Clock(us_open_ts(SESSION) - dt.timedelta(minutes=5))
    repository = BriefingVerdictRepository(engine, clock=clock)
    repository.ensure_schema()
    _upsert(repository, score=4)
    outcome = VerdictOutcome(
        open=7000.0, us_open=7010.0, close=7040.0, move_pts=30.0, move_em=0.75, hit=True
    )  # noqa: E501
    repository.update_outcome(_only_row(engine)["id"], outcome, clock.now)
    assert not _upsert(repository, score=-4)
    row = _only_row(engine)
    assert row["score"] == 4 and row["outcome_hit"] is True and row["outcome_close"] == 7040.0


@pytest.mark.parametrize(
    "now",
    [
        dt.datetime(2026, 7, 14, 13, 30, tzinfo=dt.UTC),  # US open (léto)
        dt.datetime(2026, 7, 14, 20, 30, tzinfo=dt.UTC),  # po settle
        dt.datetime(2026, 7, 15, 8, 0, tzinfo=dt.UTC),  # druhý den
    ],
)
def test_upsert_od_openu_odmitne(settings: Settings, now: dt.datetime) -> None:
    engine = create_engine(settings.database_url)
    repository = BriefingVerdictRepository(engine, clock=_Clock(now))
    repository.ensure_schema()
    with pytest.raises(VerdictFrozenError):
        _upsert(repository, score=4)
    with engine.connect() as conn:
        assert list(conn.execute(select(briefing_verdicts_table))) == []


def test_upsert_o_vikendu_a_svatku_odmitne(settings: Settings) -> None:
    engine = create_engine(settings.database_url)
    repository = BriefingVerdictRepository(
        engine, clock=_Clock(dt.datetime(2026, 9, 26, 8, 0, tzinfo=dt.UTC))
    )  # noqa: E501
    repository.ensure_schema()
    for day in (dt.date(2026, 9, 26), dt.date(2026, 9, 27), dt.date(2026, 11, 26)):
        repository._clock = _Clock(us_open_ts(day) - dt.timedelta(minutes=5))
        with pytest.raises(VerdictFrozenError):
            repository.upsert_verdict(
                session_date=day,
                symbol="ES",
                verdict="none",
                score=0,
                votes=[],
                rules_version=3,
            )
