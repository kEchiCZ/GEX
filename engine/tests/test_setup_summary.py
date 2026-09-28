"""Souhrn setupů z celé historie (#1319): čisté funkce + úložiště bez stropu.

Testy přenesené z frontendu (dailyStats, evStats, accountStats, bandGateStats,
performance) — výpočet se přestěhoval na server, kontrakt zůstává.
"""

import datetime as dt
import math
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine

from gexlens_engine.compute.paper import POINT_VALUES
from gexlens_engine.compute.setup_summary import (
    SetupFact,
    SimulationInput,
    account_stats,
    annualized_sharpe,
    band_gate_stats,
    day_stats,
    expected_value,
    fact_from_record,
    performance,
    regime_rows,
    summarize_setups,
)
from gexlens_engine.storage.setups_store import SetupsRepository

FEE = 10.0
ACCOUNT = 50_000.0
DAY = dt.date(2026, 8, 17)

RISK = {
    "risk_rules_version": 1,
    "account_equity_usd": 50000,
    "contracts": 1,
    "max_loss_usd": 400,
    "fee_usd": 10,
    "affordable": True,
    "tradeable": True,
    "trade_block": None,
}


def utc(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text).replace(tzinfo=dt.UTC)


def fact(**overrides: Any) -> SetupFact:
    values: dict[str, Any] = {
        "id": 1,
        "symbol": "ES",
        "template": "wall_bounce",
        "status": "closed_target",
        "created_ts": utc("2026-08-17T14:00:00"),
        "closed_ts": utc("2026-08-17T15:00:00"),
        "outcome_r": 2.0,
        "entry": 6000.0,
        "stop": 5995.0,  # 5 b × 50 $ = 250 $ na 1 kontrakt
        "mechanics_version": 5,
    }
    values.update(overrides)
    return SetupFact(**values)


def active(**overrides: Any) -> SetupFact:
    return fact(status="active", closed_ts=None, outcome_r=None, **overrides)


def test_fact_from_record_cte_risk_a_branu_jen_z_platneho_kontextu() -> None:
    base = {
        "id": 3,
        "symbol": "NQ",
        "template": "failed_break",
        "status": "closed_stop",
        "created_ts": dt.datetime(2026, 9, 16, 14, 0),  # sqlite = naivní → UTC
        "closed_ts": dt.datetime(2026, 9, 16, 15, 0),
        "outcome_r": -1.0,
        "entry": 100.0,
        "stop": 110.0,
        "mechanics_version": 5,
    }
    ruled = fact_from_record(
        {
            **base,
            "context": {
                **RISK,
                "tradeable": False,
                "trade_block": "gate",
                "gex_regime": "negative",
                "band_class": "inside",
                "band_gate_simple": "pass",
                "band_gate_regime": "unknown",
            },
        }
    )
    assert ruled.created_ts.tzinfo is dt.UTC
    assert ruled.risk_group == "shadow"
    assert ruled.trade_block == "gate"
    assert ruled.max_loss_usd == 400
    assert ruled.gex_regime == "negative"
    assert (ruled.band_gate_simple, ruled.band_gate_regime) == ("pass", "unknown")
    # Bez `contracts` (nebo bez bool tradeable) risk kontext neplatí — před pravidly
    unruled = fact_from_record({**base, "context": {"tradeable": True}})
    assert unruled.risk_group == "unruled"
    assert unruled.fee_usd == 0.0
    # Brána bez platné třídy polohy se nečte (jako `bandInfo` ve frontendu)
    no_gate = fact_from_record({**base, "context": {"band_gate_simple": "pass"}})
    assert no_gate.band_gate_simple is None
    assert fact_from_record({**base, "context": None}).tradeable is None


def test_expected_value_rozklad() -> None:
    ev = expected_value([100.0, 300.0, -150.0, -50.0])
    assert ev is not None
    assert ev.win_rate == 0.5
    assert ev.avg_win == 200.0
    assert ev.avg_loss == 100.0  # kladné číslo
    assert ev.ev == pytest.approx(50.0)  # ≡ průměr
    assert expected_value([]) is None
    zero = expected_value([0.0, 2.0])
    assert zero is not None and zero.win_rate == 0.5  # nula je prohra
    # EV ≡ prostý průměr (v R totéž co Ø R) — úprava vzorce ho nesmí rozjet
    pnls = [2.0, -1.0, 0.5, -1.0, 3.0, -0.25, 0.0]
    mixed = expected_value(pnls)
    assert mixed is not None and mixed.ev == pytest.approx(sum(pnls) / len(pnls))
    only_wins = expected_value([1.0, 2.0])
    assert only_wins is not None and (only_wins.loss_rate, only_wins.avg_loss) == (0.0, 0.0)
    only_losses = expected_value([-1.0, -3.0])
    assert only_losses is not None and only_losses.ev == pytest.approx(-2.0)


def test_day_stats_bilance_dne_a_riziko_z_aktivnich() -> None:
    stats = day_stats(
        [
            fact(id=1, outcome_r=2.0),  # +500 $
            fact(id=2, outcome_r=-1.0, status="closed_stop"),  # −250 $
            fact(id=3, outcome_r=3.0),  # +750 $
            active(id=4),
        ],
        DAY,
        point_values=POINT_VALUES,
        fee_per_contract_usd=FEE,
        account_usd=ACCOUNT,
    )
    assert (stats.trades, stats.closed, stats.active) == (4, 3, 1)
    assert (stats.wins, stats.losses) == (2, 1)
    assert stats.win_rate == pytest.approx(2 / 3)
    assert (stats.best_usd, stats.worst_usd) == (750.0, -250.0)
    assert stats.gross_usd == 1000.0
    assert stats.fees_usd == 30.0
    assert stats.net_usd == 970.0
    assert stats.gross_pct == pytest.approx(2.0)  # 1000 / 50 000
    # „Kolik bylo v sázce" je otázka o vstupu: 4 × 250 $, max 250 $
    assert stats.max_risk_pct == pytest.approx(0.5)
    assert stats.total_risk_pct == pytest.approx(2.0)
    assert stats.account is None  # žádný řádek nenese risk kontext


def test_day_stats_seance_ne_kalendarni_den() -> None:
    # 17. 8. 23:00 UTC = 18:00 CT → seance už je 18. 8.
    night = fact(created_ts=utc("2026-08-17T23:00:00"), closed_ts=utc("2026-08-17T23:30:00"))
    # Vznikl 17. 8. odpoledne, uzavřel se 18. 8. ráno → patří 18. 8.
    crossing = fact(
        id=2, created_ts=utc("2026-08-17T18:00:00"), closed_ts=utc("2026-08-18T13:00:00")
    )
    # Aktivní se řadí podle vzniku
    running = active(id=3, created_ts=utc("2026-08-17T18:00:00"))

    def trades(day: dt.date, rows: list[SetupFact]) -> int:
        return day_stats(
            rows, day, point_values=POINT_VALUES, fee_per_contract_usd=FEE, account_usd=ACCOUNT
        ).trades

    assert trades(dt.date(2026, 8, 17), [night, crossing]) == 0
    assert trades(dt.date(2026, 8, 18), [night, crossing]) == 2
    assert trades(dt.date(2026, 8, 17), [running]) == 1
    empty = day_stats(
        [], DAY, point_values=POINT_VALUES, fee_per_contract_usd=FEE, account_usd=ACCOUNT
    )
    assert empty.win_rate is None  # prázdný den není „0 %"
    assert empty.best_usd is None


def test_account_stats_chronologicky_max_dd_a_stin_zvlast() -> None:
    risk = {"tradeable": True, "max_loss_usd": 400.0, "fee_usd": 10.0}
    rows = [
        fact(id=1, outcome_r=2.0, closed_ts=utc("2026-09-16T17:00:00"), **risk),  # třetí
        fact(
            id=2, outcome_r=-1.0, status="closed_stop", closed_ts=utc("2026-09-16T15:00:00"), **risk
        ),
        fact(
            id=3, outcome_r=-1.0, status="closed_stop", closed_ts=utc("2026-09-16T16:00:00"), **risk
        ),
        fact(id=4, tradeable=False, trade_block="gate", outcome_r=-3.0),
        fact(id=5, outcome_r=-5.0),  # před pravidly
    ]
    stats = account_stats(rows, ACCOUNT)
    assert stats is not None
    # −410, −410 (DD −820), +790 → −30; hrubě 800 − 400 − 400 = 0, poplatky 30
    assert stats.trades == 3
    assert stats.gross_usd == 0.0
    assert stats.fees_usd == 30.0
    assert stats.net_usd == -30.0
    assert stats.max_drawdown_usd == -820.0
    assert stats.net_pct == pytest.approx(-0.06)
    assert account_stats([fact(outcome_r=1.0)], ACCOUNT) is None


def test_band_gate_stats_pass_block_a_unknown_mimo() -> None:
    rows = [
        fact(id=1, outcome_r=0.48, band_gate_simple="pass", band_gate_regime="pass"),
        fact(id=2, outcome_r=-1.0, band_gate_simple="block", band_gate_regime="block"),
        fact(id=3, outcome_r=0.5, band_gate_simple="pass", band_gate_regime="unknown"),
        active(id=4, band_gate_simple="pass", band_gate_regime="pass"),  # aktivní ne
        fact(id=5, outcome_r=9.0),  # bez brány ne
    ]
    gates = band_gate_stats(rows)
    assert gates is not None
    assert gates["simple"]["pass"].n == 2
    assert gates["simple"]["pass"].avg_r == pytest.approx(0.49)
    assert gates["simple"]["pass"].win_rate == 1.0
    assert gates["simple"]["block"].n == 1
    assert gates["regime"]["pass"].n == 1
    assert gates["regime"]["block"].n == 1
    assert band_gate_stats([fact()]) is None


def test_regime_rows_jen_cil_a_stop() -> None:
    rows = regime_rows(
        [
            fact(id=1, gex_regime="negative"),
            fact(id=2, gex_regime="negative", status="closed_stop", outcome_r=-1.0),
            fact(id=3, gex_regime="negative", status="closed_timeout", outcome_r=0.3),
            fact(id=4, gex_regime=None),
        ]
    )
    assert [(r.template, r.regime, r.n, r.wins) for r in rows] == [
        ("wall_bounce", "negative", 2, 1),
        ("wall_bounce", "neznámý", 1, 1),
    ]


def test_performance_denni_rada_sharpe_drawdown() -> None:
    rows = [
        fact(id=1, outcome_r=2.0, closed_ts=utc("2026-08-20T12:00:00")),
        fact(id=2, outcome_r=-1.0, closed_ts=utc("2026-08-20T15:00:00")),
        fact(id=3, outcome_r=-3.0, closed_ts=utc("2026-08-21T12:00:00")),
        fact(id=4, outcome_r=1.5, closed_ts=utc("2026-08-24T12:00:00")),
        active(id=5),
    ]
    result = performance(rows, point_values=POINT_VALUES, simulation=None)
    assert [(p.session, p.trades, p.sum_r, p.cum_r) for p in result.daily] == [
        ("2026-08-20", 2, 1.0, 1.0),
        ("2026-08-21", 1, -3.0, -2.0),
        ("2026-08-24", 1, 1.5, -0.5),
    ]
    assert result.max_drawdown_r == -3.0
    assert result.sharpe_all.days == 3
    assert result.simulation is None
    # mean 2, std(ddof=1) 1 → 2 × √252; málo dat → None
    assert annualized_sharpe([1.0, 2.0, 3.0]).sharpe == pytest.approx(2 * math.sqrt(252))
    assert annualized_sharpe([1.0]).sharpe is None
    assert annualized_sharpe([1.0, 1.0]).sharpe is None


def test_usd_simulace_mikro_sizing_naklady_a_preskocene() -> None:
    rows = [
        # stop 10 b → MES (5 $/b): ⌊100 / 50⌋ = 2 kontrakty
        fact(id=1, outcome_r=2.0, entry=6400.0, stop=6390.0, closed_ts=utc("2026-08-20T12:00:00")),
        # stop 40 b → ⌊100 / 200⌋ = 0 kontraktů → přeskočeno
        fact(id=2, outcome_r=1.0, entry=6400.0, stop=6360.0, closed_ts=utc("2026-08-20T13:00:00")),
    ]
    result = performance(
        rows, point_values=POINT_VALUES, simulation=SimulationInput(account_usd=5000, risk_pct=2)
    )
    sim = result.simulation
    assert sim is not None
    assert (sim.traded, sim.skipped) == (1, 1)
    # 2 kontrakty × 2 R × 10 b × 5 $ − 2 × 2,49 $
    assert sim.total_usd == pytest.approx(200 - 4.98)
    off = performance(
        rows, point_values=POINT_VALUES, simulation=SimulationInput(account_usd=0, risk_pct=1)
    )
    assert off.simulation is None


def test_summarize_rozdeleni_poplatky_mechanika_a_legacy() -> None:
    risk = {"max_loss_usd": 400.0, "fee_usd": 10.0}
    rows = [
        # obchodovatelný ES +2 R: 1 kontrakt 2 × 5 b × 50 = +500 $, účet 2 × 400 − 10
        fact(id=1, tradeable=True, **risk),
        # stín NQ −1 R: stop 10 b × 20 $ = −200 $
        fact(
            id=2,
            symbol="NQ",
            tradeable=False,
            trade_block="gate",
            outcome_r=-1.0,
            status="closed_stop",
            entry=100.0,
            stop=110.0,
        ),
        fact(
            id=3,
            tradeable=False,
            trade_block="stop_over_budget",
            outcome_r=-1.0,
            status="closed_stop",
        ),
        active(id=4, tradeable=False, trade_block="gate"),
        # před pravidly, timeout +0,4 R = +100 $
        fact(id=5, status="closed_timeout", outcome_r=0.4),
        # starší mechanika — do souhrnu jen s all_versions
        fact(id=6, mechanics_version=4, outcome_r=-8.0, status="closed_stop"),
    ]

    def run(all_versions: bool) -> Any:
        return summarize_setups(
            rows,
            mechanics_version=5,
            all_versions=all_versions,
            point_values=POINT_VALUES,
            fee_per_contract_usd=FEE,
            account_usd=ACCOUNT,
            session_day=DAY,
        )

    summary = run(False)
    assert (summary.total_count, summary.legacy_count) == (6, 1)
    total = summary.all
    assert (total.count, total.active, total.closed, total.wins) == (5, 1, 4, 2)
    assert total.sum_r == pytest.approx(0.4)
    assert total.gross_usd == pytest.approx(500 - 200 - 250 + 100)
    assert total.fees_usd == 40.0  # 4 uzavřené × 10 $
    assert total.net_usd == pytest.approx(150 - 40)
    assert summary.tradeable.closed == 1
    assert summary.tradeable.net_usd == 490.0
    assert (summary.shadow.count, summary.shadow.closed) == (3, 2)
    assert summary.shadow.gross_usd == -450.0
    assert summary.shadow_reasons == {"gate": 2, "stop_over_budget": 1}
    assert summary.unruled.closed == 1
    assert summary.account is not None
    assert summary.account.trades == 1
    assert summary.account.net_usd == 790.0
    assert summary.today.closed == 4
    assert summary.today.account is not None and summary.today.account.net_usd == 790.0
    assert summary.unpriced_symbols == []

    with_legacy = run(True)
    assert with_legacy.all.closed == 5
    assert with_legacy.all.sum_r == pytest.approx(-7.6)
    assert with_legacy.legacy_count == 1


def test_summarize_neznamy_bod_se_nevymysli() -> None:
    summary = summarize_setups(
        [fact(symbol="XYZ")],
        mechanics_version=5,
        all_versions=False,
        point_values=POINT_VALUES,
        fee_per_contract_usd=FEE,
        account_usd=ACCOUNT,
        session_day=DAY,
    )
    assert summary.unpriced_symbols == ["XYZ"]
    assert summary.all.sum_r == 2.0
    assert summary.all.gross_usd == 0.0
    assert summary.all.fees_usd == 0.0
    assert summary.all.ev_usd is None


def _repo(tmp_path: Path) -> SetupsRepository:
    repo = SetupsRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'setups.sqlite'}"))
    repo.ensure_schema()
    return repo


def _create(repo: SetupsRepository, created: dt.datetime, *, symbol: str = "ES") -> int:
    return repo.create(
        symbol=symbol,
        expiry="20260917",
        template="failed_break",
        direction="long",
        created_ts=created,
        entry=6000.0,
        target=6010.0,
        stop=5995.0,
        confidence=50,
        reason="test",
        context=dict(RISK),
    )


def test_summary_facts_bez_stropu_a_stabilni_poradi_stranky(tmp_path: Path) -> None:
    """Příčina #1319: stránka má strop 200, souhrn ho mít nesmí."""
    repo = _repo(tmp_path)
    start = utc("2026-09-01T14:00:00")
    ids = [_create(repo, start + dt.timedelta(minutes=i)) for i in range(205)]
    for setup_id in ids[:150]:
        repo.close(
            setup_id,
            status="closed_stop",
            closed_ts=start + dt.timedelta(hours=5),
            outcome_r=-1.0,
            mfe=0.0,
            mae=5.0,
        )
    _create(repo, start, symbol="NQ")
    # Dvě šablony v jedné minutě: pořadí podle id, ne náhodné
    twin = _create(repo, start + dt.timedelta(minutes=204))

    page = repo.list_for("ES")
    assert len(page) == 200
    assert [row["id"] for row in page[:2]] == [twin, ids[-1]]
    assert repo.count_for("ES") == 206
    assert repo.count_for("ES", status="closed_stop") == 150

    facts = repo.summary_facts(["ES"])
    assert len(facts) == 206
    assert sum(1 for item in facts if item.is_closed) == 150
    assert all(item.tradeable is True and item.max_loss_usd == 400 for item in facts)
    assert len(repo.summary_facts(["ES", "NQ"])) == 207
