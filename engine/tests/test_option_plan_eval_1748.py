"""Kolektor vyhodnocení plánu QQQ po close (F3 #1748): partice, PG, alert, idempotence.

PG = SQLite v `tmp_path`, Parquet v `tmp_path`. Plán a cesty barů jsou z
`test_option_plan_result_1748` (short, flip 600, EM 5, cíle 595 / 593).
"""

import datetime as dt
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from test_option_plan_result_1748 import (
    DAY,
    EXPIRY,
    OPEN,
    STRIKES,
    TEST_AND_ENTRY,
    fives,
    flat,
    part,
    synthetic_quotes,
    until_exit,
)

import gexlens_engine.option_plan_eval as plan_eval
from gexlens_engine.compute.option_plan import AccountParams
from gexlens_engine.compute.scenario import Bar as PathBar
from gexlens_engine.compute.settle import session_bounds
from gexlens_engine.config import Settings
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.option_plan_eval import ALERT_KIND, OptionPlanCollector
from gexlens_engine.storage.option_plans_store import OptionPlansRepository
from gexlens_engine.storage.parquet_store import (
    OptionQuoteRow,
    SnapshotWriter,
    read_option_quotes,
)

UTC = dt.UTC
#: Syntetický rozpočet testu (ne skutečný účet; výchozí hodnota v kódu není)
ACCOUNT = AccountParams(account_usd=None, max_loss_usd=1234.5)
MINUTE = dt.timedelta(minutes=1)
AFTER_CLOSE = dt.datetime(2026, 11, 17, 21, 15, tzinfo=UTC)  # settle 16:00 ET + 15 min


class Publisher:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, channel: str, payload: dict[str, Any]) -> None:
        self.sent.append((channel, payload))


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.settings = Settings(data_dir=tmp_path)
        self.writer = SnapshotWriter(self.settings)
        db = create_engine(f"sqlite+pysqlite:///{tmp_path / 'db.sqlite'}")
        self.repo = OptionPlansRepository(db)
        self.repo.ensure_schema()
        self.publisher = Publisher()
        self.scenario_id = 0

    def collector(self) -> OptionPlanCollector:
        return OptionPlanCollector(
            repository=self.repo,
            derived_dir=self.settings.derived_dir,
            account=lambda: ACCOUNT,
            publisher=self.publisher,
        )

    def plan(self, day: dt.date = DAY, main: dict[str, Any] | None = None) -> int:
        self.scenario_id += 1
        main = main if main is not None else part()
        plan = {
            "slot": "9:15",
            "reasons": [],
            "levels_nq": {},
            "chain": None,
            "main": main,
            "shadows": {
                "entry_b": {**main, "rules": "stín vstup B"},
                "wick_test": {**main, "rules": "stín test knotem"},
            },
        }
        plan_id = self.repo.create(
            scenario_id=self.scenario_id,
            day=day,
            created_at=dt.datetime.combine(day, dt.time(14, 15), tzinfo=UTC),
            rules_version=1,
            verdict="čeká",
            status="sestaven",
            plan=plan,
        )
        assert plan_id is not None
        return plan_id

    def bars(self, bars: Sequence[PathBar]) -> None:
        self.writer.write_bars_by_day(
            "QQQ", [Bar(b.ts, b.close, b.high, b.low, b.close, 0.0) for b in bars]
        )

    def quotes(self, day: dt.date, bars: Sequence[PathBar], expiry: dt.date = EXPIRY) -> None:
        rows = [
            OptionQuoteRow(
                q.ts_min,
                q.expiry,
                q.strike,
                q.right,
                q.bid,
                q.ask,
                None if q.bid is None or q.ask is None else (q.bid + q.ask) / 2,
                q.iv,
                -0.2,
                q.quote_age_s,
            )
            for q in synthetic_quotes(bars, STRIKES, expiry)
        ]
        self.writer.write_option_quotes("QQQ", day, rows)


SL_PATH = until_exit(fives(*TEST_AND_ENTRY, (600.9, 599.6, 600.9)), 601.0)


async def test_after_settle_evaluates_once_and_alerts(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    plan_id = h.plan()
    h.bars(SL_PATH)
    h.quotes(DAY, SL_PATH)
    collector = h.collector()
    await collector.on_minute(AFTER_CLOSE - dt.timedelta(minutes=1))
    assert h.publisher.sent == [] and h.repo.get(plan_id).evaluated_at is None  # type: ignore[union-attr]
    await collector.on_minute(AFTER_CLOSE)
    row = h.repo.get(plan_id)
    assert row is not None and row.evaluated_at == AFTER_CLOSE and row.result is not None
    main = row.result["main"]
    assert (main["status"], main["outcome"]) == ("spuštěno", "SL")
    assert [v["budget"] for v in main["variants"]] == ["v rozpočtu", "v rozpočtu"]
    assert set(row.result["shadows"]) == {"entry_b", "wick_test"}
    [(channel, payload)] = h.publisher.sent
    assert channel == "alerts" and payload["kind"] == ALERT_KIND
    assert (payload["plan_id"], payload["symbol"]) == (plan_id, "QQQ")
    message = payload["message"]
    assert message.startswith(f"Plán QQQ #{plan_id} (17. 11.) vyhodnocen: vstup A 09:40 ET")
    assert "→ SL" in message and "hlavní put 590" in message
    assert message.endswith("Do track recordu se nepočítá.")
    assert "1234" not in message and "1 234" not in message  # rozpočet do zprávy nejde
    await collector.on_minute(AFTER_CLOSE + dt.timedelta(minutes=1))
    assert len(h.publisher.sent) == 1


async def test_second_run_after_restart_changes_nothing(tmp_path: Path) -> None:
    """Idempotence: nový kolektor (restart enginu) ani druhý zápis výsledek nepřepíše."""
    h = Harness(tmp_path)
    plan_id = h.plan()
    h.bars(SL_PATH)
    h.quotes(DAY, SL_PATH)
    await h.collector().on_minute(AFTER_CLOSE)
    first = h.repo.get(plan_id)
    await h.collector().on_minute(AFTER_CLOSE + dt.timedelta(minutes=5))
    assert h.repo.get(plan_id) == first
    assert len(h.publisher.sent) == 1
    assert h.repo.record_result(plan_id, {"jiný": True}, AFTER_CLOSE) is False
    assert h.repo.get(plan_id) == first
    with pytest.raises(LookupError):
        h.repo.record_result(999, {}, AFTER_CLOSE)


@pytest.mark.parametrize(
    "now",
    [
        dt.datetime(2026, 11, 21, 21, 30, tzinfo=UTC),  # sobota
        dt.datetime(2026, 11, 22, 21, 30, tzinfo=UTC),  # neděle (seance pondělí, před settle)
        dt.datetime(2026, 11, 26, 21, 30, tzinfo=UTC),  # Díkůvzdání
    ],
)
async def test_weekend_and_holiday_do_nothing(tmp_path: Path, now: dt.datetime) -> None:
    h = Harness(tmp_path)
    plan_id = h.plan()
    await h.collector().on_minute(now)
    assert h.repo.get(plan_id).evaluated_at is None  # type: ignore[union-attr]
    assert h.publisher.sent == []


async def test_session_read_from_globex_open_including_previous_partition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seance od 17:00 CT D−1 (AC #1748): okno barů začíná otevřením Globexu, čte i partici D−1."""
    h = Harness(tmp_path)
    plan_id = h.plan()
    globex = flat(598.0, dt.datetime(2026, 11, 17, 0, 0, tzinfo=UTC), session_bounds(DAY)[0])
    assert globex[0].ts == dt.datetime(2026, 11, 16, 23, 0, tzinfo=UTC)
    h.bars([*globex, *SL_PATH])
    h.quotes(DAY, SL_PATH)
    windows: list[tuple[dt.datetime, dt.datetime]] = []
    real = plan_eval.read_bars

    def spy(derived: Path, symbol: str, since: dt.datetime, until: dt.datetime) -> Any:
        windows.append((since, until))
        bars = real(derived, symbol, since, until)
        assert any(bar.ts.date() == dt.date(2026, 11, 16) for bar in bars)
        return bars

    monkeypatch.setattr(plan_eval, "read_bars", spy)
    await h.collector().on_minute(AFTER_CLOSE)
    assert windows == [(session_bounds(DAY)[0], dt.datetime(2026, 11, 17, 21, 0, tzinfo=UTC))]
    row = h.repo.get(plan_id)
    assert row is not None and row.result is not None
    # Bary Globexu jsou mimo RTH: vstup a počet minut se nezmění
    assert (row.result["main"]["outcome"], row.result["main"]["bars_expected"]) == ("SL", 15)


async def test_monday_reads_monday_not_friday(tmp_path: Path) -> None:
    """Pondělí proti pátku: plán pondělí čte bary pondělní seance, pátek se nemíchá."""
    h = Harness(tmp_path)
    monday = dt.date(2026, 11, 16)
    friday_open = dt.datetime(2026, 11, 13, 14, 30, tzinfo=UTC)
    monday_open = dt.datetime(2026, 11, 16, 14, 30, tzinfo=UTC)
    monday_exit = dt.datetime(2026, 11, 16, 20, 45, tzinfo=UTC)
    plan = part(
        day=monday.isoformat(),
        created_at=dt.datetime(2026, 11, 16, 14, 15, tzinfo=UTC).isoformat(),
        exit_ts=monday_exit.isoformat(),
        target_ts=[
            dt.datetime(2026, 11, 16, 16, 30, tzinfo=UTC).isoformat(),
            dt.datetime(2026, 11, 16, 18, 0, tzinfo=UTC).isoformat(),
        ],
        variants=[{"role": "hlavní", "right": "P", "strike": 590.0, "expiry": "2026-11-17"}],
    )
    plan_id = h.plan(monday, plan)
    friday = fives(*TEST_AND_ENTRY, start=friday_open)  # pátek by vstup A spustil
    friday += flat(597.0, dt.datetime(2026, 11, 13, 21, 0, tzinfo=UTC), friday[-1].ts + MINUTE)
    monday_bars = flat(598.0, monday_exit, monday_open)
    h.bars(friday)
    h.bars(monday_bars)
    h.quotes(monday, monday_bars, dt.date(2026, 11, 17))
    await h.collector().on_minute(dt.datetime(2026, 11, 16, 21, 15, tzinfo=UTC))
    row = h.repo.get(plan_id)
    assert row is not None and row.result is not None
    assert row.result["main"]["status"] == "bez vstupu"


async def test_missed_evening_is_caught_up_next_session(tmp_path: Path) -> None:
    """Engine večer neběžel: další večer vyhodnotí i starší plán z jeho vlastních partic."""
    h = Harness(tmp_path)
    plan_id = h.plan()
    h.bars(SL_PATH)
    h.quotes(DAY, SL_PATH)
    await h.collector().on_minute(dt.datetime(2026, 11, 18, 21, 15, tzinfo=UTC))
    row = h.repo.get(plan_id)
    assert row is not None and row.result is not None
    assert row.result["main"]["outcome"] == "SL"


async def test_missing_data_is_gap_not_estimate(tmp_path: Path) -> None:
    """Partice barů ani kotací nejsou (plán před F1b): výsledek `mezera`, zapíše se jednou."""
    h = Harness(tmp_path)
    plan_id = h.plan()
    await h.collector().on_minute(AFTER_CLOSE)
    row = h.repo.get(plan_id)
    assert row is not None and row.result is not None
    assert row.result["main"]["status"] == "mezera"
    assert "chybí 375 z 375 minut" in row.result["main"]["reasons"][0]
    assert h.publisher.sent[0][1]["message"].startswith(
        f"Plán QQQ #{plan_id} (17. 11.) vyhodnocen: mezera — bary QQQ"
    )


async def test_plan_without_option(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.scenario_id += 1
    plan_id = h.repo.create(
        scenario_id=h.scenario_id,
        day=DAY,
        created_at=dt.datetime(2026, 11, 17, 14, 15, tzinfo=UTC),
        rules_version=1,
        verdict=None,
        status="bez opce",
        plan={
            "slot": "9:15",
            "reasons": ["ranní snímek neúplný"],
            "levels_nq": {},
            "chain": None,
            "main": None,
            "shadows": {},
        },
    )
    assert plan_id is not None
    await h.collector().on_minute(AFTER_CLOSE)
    row = h.repo.get(plan_id)
    assert row is not None and row.result is not None
    assert row.result["main"]["status"] == "bez opce"
    assert row.result["main"]["reasons"] == ["ranní snímek neúplný"]
    assert row.result["shadows"] == {}


async def test_broken_plan_is_logged_and_retried(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    h = Harness(tmp_path)
    broken = h.plan(main={**part(), "day": "není datum"})
    fine = h.plan()
    await h.collector().on_minute(AFTER_CLOSE)
    assert h.repo.get(broken).evaluated_at is None  # type: ignore[union-attr]
    assert h.repo.get(fine).evaluated_at == AFTER_CLOSE  # type: ignore[union-attr]
    assert f"#{broken}" in caplog.text and "selhalo" in caplog.text


def test_read_option_quotes_roundtrip(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    assert read_option_quotes(h.settings.derived_dir, "QQQ", DAY) == []
    path = SL_PATH[:3]
    h.quotes(DAY, path)
    rows = read_option_quotes(h.settings.derived_dir, "QQQ", DAY)
    assert len(rows) == 6 and rows[0].ts_min == OPEN and rows[0].ts_min.tzinfo is not None
    assert {(row.expiry, row.strike, row.right) for row in rows} == {
        ("20261118", 590.0, "P"),
        ("20261118", 589.0, "P"),
    }
    assert h.writer.option_quotes_path("QQQ", DAY).exists()


def test_pending_and_stats(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    first, second = h.plan(dt.date(2026, 11, 16)), h.plan()
    assert [row.id for row in h.repo.pending(dt.date(2026, 11, 16))] == [first]
    assert [row.id for row in h.repo.pending(DAY)] == [first, second]
    h.bars(SL_PATH)
    h.quotes(DAY, SL_PATH)
    collector = h.collector()
    [(row, result)] = [pair for pair in collector.run(AFTER_CLOSE) if pair[0].id == second]
    assert h.repo.pending(DAY) == []
    outside = h.repo.stats(rules_version=1, since=None)
    assert (outside["since"], outside["outside"], outside["main"]["plans"]) == (None, 2, 0)
    counted = h.repo.stats(rules_version=1, since=DAY)
    assert (counted["outside"], counted["main"]["plans"]) == (1, 1)
    assert counted["main"]["variants"]["hlavní"]["trades"]["outcomes"]["SL"] == 1
    assert h.repo.stats(rules_version=2, since=DAY)["main"]["plans"] == 0
    assert result["main"]["outcome"] == "SL" and row.day == DAY
