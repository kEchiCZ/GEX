"""Knihovna setupů (#1323): buňky ticker × šablona v `/setups/summary` a souhrn zkoušek."""

import datetime as dt
import math
from dataclasses import replace
from typing import Any

import pytest

from gexlens_engine.compute.paper import POINT_VALUES
from gexlens_engine.compute.risk import RealizedSetup, samples_needed
from gexlens_engine.compute.setup_library import (
    LibraryCell,
    cell_net_usd,
    library_brakes,
    library_cells,
)
from gexlens_engine.compute.setup_summary import SetupFact, net_r, net_usd_micro, summarize_setups
from gexlens_engine.compute.setups import (
    SETUP_MECHANICS_VERSION,
    SetupParams,
    TrialCell,
)

NOW = dt.datetime(2026, 10, 1, 14, 0, tzinfo=dt.UTC)
OUTCOMES = [2.0, -1.0, 2.0, -1.0, -1.0, 2.0, -1.0, -1.0, 2.0, -1.0]
DAYS = [24, 25, 28, 29, 30]  # pět seancí, dva setupy denně


def _fact(fid: int, created: dt.datetime, **overrides: Any) -> SetupFact:
    values: dict[str, Any] = {
        "id": fid,
        "symbol": "ES",
        "expiry": created.strftime("%Y%m%d"),
        "template": "failed_break",
        "status": "closed_stop",
        "created_ts": created,
        "closed_ts": created + dt.timedelta(minutes=30),
        "outcome_r": -1.0,
        "entry": 6000.0,
        "stop": 5995.0,  # 5 b ES; náklad MES 2,49 $ / (5 b × 5 $) = 0,0996 R
        "mechanics_version": SETUP_MECHANICS_VERSION,
        "tradeable": False,
        "trade_block": "gate",
        "affordable": True,
    }
    values.update(overrides)
    if values["outcome_r"] is not None and values["outcome_r"] > 0:
        values["status"] = "closed_target"
    return SetupFact(**values)


def _facts() -> list[SetupFact]:
    facts: list[SetupFact] = []
    for index, outcome in enumerate(OUTCOMES):
        day = DAYS[index // 2]
        created = dt.datetime(2026, 9, day, 14 + index % 2, 0, tzinfo=dt.UTC)
        overridden = day == 30  # zkouška od 29. 9. 22:00 — 30. 9. pustila dva setupy
        facts.append(
            _fact(
                index + 1,
                created,
                outcome_r=outcome,
                tradeable=overridden,
                trade_block=None if overridden else "gate",
                gate_overridden=overridden,
            )
        )
    # Otevřený setup zkoušky dnes — čerpá počet, R ještě nemá
    facts.append(
        _fact(
            50,
            NOW - dt.timedelta(hours=1),
            status="active",
            closed_ts=None,
            outcome_r=None,
            tradeable=True,
            trade_block=None,
            gate_overridden=True,
        )
    )
    # Nevstupují: starší mechanika, vznik po settle (#1324), cizí ticker
    old = dt.datetime(2026, 9, 25, 16, 0, tzinfo=dt.UTC)
    facts.append(_fact(60, old, outcome_r=5.0, mechanics_version=SETUP_MECHANICS_VERSION - 1))
    after_settle = dt.datetime(2026, 9, 29, 21, 0, tzinfo=dt.UTC)
    facts.append(_fact(61, after_settle, outcome_r=5.0, expiry="20260929"))
    facts.append(_fact(62, old, symbol="RTY", outcome_r=5.0))
    # … ani značka vyřazení (#1346, vznik nad zamrzlým spotem) v okně brány
    frozen = dt.datetime(2026, 9, 28, 16, 0, tzinfo=dt.UTC)
    facts.append(_fact(63, frozen, outcome_r=5.0, excluded="vstup_mimo_bary"))
    # NQ: jeden uzavřený T7 → ticker v okně, brána insufficient
    facts.append(
        _fact(
            70,
            dt.datetime(2026, 9, 30, 15, 0, tzinfo=dt.UTC),
            symbol="NQ",
            template="trend_continuation",
            entry=29000.0,
            stop=28984.0,
            outcome_r=1.0,
        )
    )
    return facts


PARAMS = SetupParams(
    shadow_cells=frozenset({"NQZ6:trend_continuation"}),
    trial_cells=(
        TrialCell(
            "ES:failed_break",
            dt.datetime(2026, 9, 29, 22, 0, tzinfo=dt.UTC),
            3,
            3.0,
            SETUP_MECHANICS_VERSION,
        ),
    ),
)


def _cells() -> dict[str, LibraryCell]:
    cells = library_cells(
        _facts(),
        PARAMS,
        symbols=["ES", "NQ", "NQZ6"],
        now=NOW,
        mechanics_version=SETUP_MECHANICS_VERSION,
        point_values=POINT_VALUES,
    )
    return {cell.cell: cell for cell in cells}


def test_bunky_tickery_a_aktivni_sablony() -> None:
    cells = _cells()
    tickers = {cell.ticker for cell in cells.values()}
    # NQZ6 nemá setupy, ale má nastavené stádium; RTY nikdo nechtěl
    assert tickers == {"ES", "NQ", "NQZ6"}
    # Vyřazená šablona (T5, disabled_templates) v Knihovně není
    templates = {cell.template for cell in cells.values() if cell.ticker == "ES"}
    assert templates == {
        "wall_bounce",
        "failed_break",
        "max_pain_pin",
        "gamma_momentum",
        "trend_continuation",
    }
    assert cells["NQZ6:trend_continuation"].stage == "shadow"
    assert cells["NQZ6:trend_continuation"].effective_stage == "shadow"
    assert cells["NQZ6:trend_continuation"].gate_n == 0
    assert cells["ES:wall_bounce"].stage == "auto" and cells["ES:wall_bounce"].trial is None


def test_bunka_brana_teď_dukaz_a_odhad() -> None:
    cell = _cells()["ES:failed_break"]
    assert cell.template_number == 2
    # Vzorek brány: 10 uzavřených v5 v okně, bez starší mechaniky a vzniku po settle
    assert cell.gate_n == 10 and cell.gate_verdict == "insufficient"
    assert cell.avg_r == pytest.approx(0.2)
    cost_r = 2.49 / (5 * 5.0)
    assert cell.avg_net_r == pytest.approx(0.2 - cost_r)
    assert cell.gate_lb == pytest.approx(0.2 - 1.645 * math.sqrt(2.4 / 10))
    # n potřebné = ((1,645 + 0,84) · σ / 0,2)², σ² = 2,4
    assert cell.n_needed == math.ceil((2.485 * math.sqrt(2.4) / 0.2) ** 2) == 371
    assert cell.n_needed == samples_needed(OUTCOMES, min_samples=30)
    # Seance tickeru v okně: 5 dnů historie + dnešek (otevřený setup)
    assert cell.sessions == 6 and cell.per_session == pytest.approx(10 / 6)
    # Okno 60 seancí pojme při tomto tempu 100 vzorků < 371 → odhad v seancích
    # by byl cíl, který se pořád posouvá; buňka v bráně průkazu nedosáhne
    assert cell.window_capacity == 100 and cell.sessions_to_decision is None
    nq = _cells()["NQ:trend_continuation"]
    assert nq.gate_n == 1 and nq.n_needed is None and nq.sessions_to_decision is None


def test_bunka_zkouska_cerpani_vcetne_otevreneho_a_konec() -> None:
    cell = _cells()["ES:failed_break"]
    assert cell.trial is not None
    assert cell.trial.started_at == "2026-09-29T22:00:00+00:00"
    # 2 uzavřené z 30. 9. (+2, −1) + 1 otevřený = 3/3 → vyčerpáno → chová se jako Auto
    assert (cell.trial.setups, cell.trial.sum_r, cell.trial.spent) == (3, 1.0, True)
    assert cell.stage == "trial" and cell.effective_stage == "auto"


def test_odhad_v_seancich_kdyz_okno_vzorek_pojme() -> None:
    # Vyšší tempo: 10 setupů za 1 seanci → okno pojme 600 ≥ 371
    facts = [
        _fact(index + 1, dt.datetime(2026, 9, 30, 13, index, tzinfo=dt.UTC), outcome_r=outcome)
        for index, outcome in enumerate(OUTCOMES)
    ]
    (cell,) = [
        cell
        for cell in library_cells(
            facts,
            SetupParams(),
            symbols=["ES"],
            now=NOW,
            mechanics_version=SETUP_MECHANICS_VERSION,
            point_values=POINT_VALUES,
        )
        if cell.template == "failed_break"
    ]
    assert cell.sessions == 1 and cell.window_capacity == 600
    assert cell.sessions_to_decision == math.ceil((371 - 10) / 10)


@pytest.mark.parametrize(
    ("results", "expected"),
    [
        ([-1.0, -1.0], None),  # dva stopy: σ = 0 nic neříká
        ([3.0, 3.0], None),  # dva cíle na stropu max_rr
        ([-1.0] * 5 + [-0.8], 30),  # malé σ → nikdy pod minimum brány
        ([-1.0] * 30, 30),  # σ = 0 při n ≥ minimum: brána rozhodne teď
        ([-1.0], None),
    ],
)
def test_samples_needed_maly_vzorek_nesvadi(results: list[float], expected: int | None) -> None:
    assert samples_needed(results, min_samples=30) == expected


def test_dva_stopy_nejsou_nahore_ani_vzorek_stoji() -> None:
    """Review #1323: dva stopy −1 R dávaly n potřebné 2 → „2 / 2 · vzorek
    stačí“ a první řádek Knihovny nad T7."""
    stops = [
        _fact(
            200 + index,
            dt.datetime(2026, 9, 30, 15, index, tzinfo=dt.UTC),
            symbol="NQ",
            template="max_pain_pin",
            entry=29000.0,
            stop=28984.0,
        )
        for index in range(2)
    ]
    cells = library_cells(
        [*_facts(), *stops],
        PARAMS,
        symbols=["ES", "NQ"],
        now=NOW,
        mechanics_version=SETUP_MECHANICS_VERSION,
        point_values=POINT_VALUES,
    )
    pin = next(cell for cell in cells if cell.cell == "NQ:max_pain_pin")
    assert pin.gate_n == 2 and pin.n_needed is None and pin.sessions_to_decision is None
    assert cells[0].cell == "ES:failed_break"


def test_bunka_ukazuje_cisty_pl_v_mikro_dolarech() -> None:
    cell = _cells()["ES:failed_break"]
    # Řádky bez kontraktů v kontextu dopočte sizing: 50 000 × 1 % / (5 b × 50 $) = 2
    per_trade = [2 * (outcome * 5 * 5.0 - 2.49) for outcome in OUTCOMES]
    assert cell.net_usd == pytest.approx(sum(per_trade))
    # Kontrakty z kontextu mají přednost (skutečný sizing při vzniku)
    row = RealizedSetup(
        symbol="NQ",
        template="trend_continuation",
        status="closed_target",
        outcome_r=1.0,
        closed_ts=NOW,
        tradeable=True,
        affordable=True,
        entry=29000.0,
        stop=28984.0,
        contracts=3,
    )
    assert cell_net_usd(
        [row], SetupParams(), point_value_usd=20.0, point_values=POINT_VALUES
    ) == pytest.approx(3 * (16 * 2.0 - 1.74))
    assert net_usd_micro(1.0, 16.0, 3, "NQZ6", POINT_VALUES) == pytest.approx(3 * (32 - 1.74))
    assert cell_net_usd([], SetupParams(), point_value_usd=50.0, point_values=POINT_VALUES) is None


def test_zkouska_jine_mechaniky_v_knihovne_skoncila() -> None:
    (trial,) = PARAMS.trial_cells
    roomy = replace(trial, budget_setups=20)  # 3/20 → nevyčerpaná

    def es_cell(candidate: TrialCell) -> LibraryCell:
        cells = library_cells(
            _facts(),
            replace(PARAMS, trial_cells=(candidate,)),
            symbols=["ES"],
            now=NOW,
            mechanics_version=SETUP_MECHANICS_VERSION,
            point_values=POINT_VALUES,
        )
        return next(cell for cell in cells if cell.cell == "ES:failed_break")

    assert es_cell(roomy).effective_stage == "trial"
    stale = es_cell(replace(roomy, mechanics_version=SETUP_MECHANICS_VERSION - 1))
    assert stale.stage == "trial" and stale.effective_stage == "auto"
    assert stale.trial is not None and not stale.trial.spent
    assert stale.trial.mechanics_version == SETUP_MECHANICS_VERSION - 1


def test_nastavene_bunky_vzdy_i_mimo_symbols() -> None:
    """Review #1323: zkouška pinovaného kontraktu mimo watchlist (nebo než se
    watchlist načte) nesmí z Knihovny zmizet — hlavička by lhala „žádná“."""
    cells = library_cells(
        _facts(),
        PARAMS,
        symbols=["NQ"],
        now=NOW,
        mechanics_version=SETUP_MECHANICS_VERSION,
        point_values=POINT_VALUES,
    )
    tickers = {cell.ticker for cell in cells}
    assert tickers == {"NQ", "ES", "NQZ6"}  # ES má zkoušku, NQZ6 stín
    es = next(cell for cell in cells if cell.cell == "ES:failed_break")
    assert es.trial is not None and es.gate_n == 10


def test_razeni_podle_prukaznosti() -> None:
    cells = library_cells(
        _facts(),
        PARAMS,
        symbols=["ES", "NQ", "NQZ6"],
        now=NOW,
        mechanics_version=SETUP_MECHANICS_VERSION,
        point_values=POINT_VALUES,
    )
    assert cells[0].cell == "ES:failed_break"  # jediná s odhadem n potřebné
    assert all(cell.n_needed is None for cell in cells[1:])


def test_net_r_konvence_souhrnu() -> None:
    # NQ: MNQ 2 $/b, round-trip 1,74 $; stop 16 b → náklad 0,054 R
    assert net_r(1.0, 16.0, "NQZ6", POINT_VALUES) == pytest.approx(1.0 - 1.74 / 32)
    assert net_r(1.0, 0.0, "ES", POINT_VALUES) is None


def test_souhrn_zkousky_a_stinu_uzivatele_zvlast() -> None:
    created = dt.datetime(2026, 9, 30, 14, 0, tzinfo=dt.UTC)
    facts = [
        _fact(1, created, outcome_r=2.0, tradeable=True, trade_block=None, gate_overridden=True),
        _fact(2, created, outcome_r=-1.0, tradeable=True, trade_block=None),
        _fact(3, created, outcome_r=-1.0, tradeable=False, trade_block="user"),
        _fact(4, created, outcome_r=-1.0, tradeable=False, trade_block="gate"),
    ]
    summary = summarize_setups(
        facts,
        mechanics_version=SETUP_MECHANICS_VERSION,
        all_versions=False,
        point_values=POINT_VALUES,
        fee_per_contract_usd=10.0,
        account_usd=50_000.0,
        session_day=NOW.date(),
    )
    assert summary.tradeable.count == 2 and summary.trial.count == 1
    assert summary.trial.sum_r == 2.0
    assert summary.shadow_reasons == {"gate": 1, "user": 1}


def _realized(
    template: str,
    outcome: float,
    closed: dt.datetime,
    *,
    symbol: str = "ES",
    tradeable: bool = True,
) -> RealizedSetup:
    return RealizedSetup(
        symbol=symbol,
        template=template,
        status="closed_target" if outcome > 0 else "closed_stop",
        outcome_r=outcome,
        closed_ts=closed,
        tradeable=tradeable,
    )


def test_brzdy_hlavicky_knihovny_jako_engine() -> None:
    # NOW = čtvrtek 1. 10. 14:00 UTC: seance od 30. 9. 22:00 UTC, týden od 27. 9. 22:00 UTC
    hour = dt.timedelta(hours=1)
    realized = [
        _realized("trend_continuation", -1.0, NOW - hour),
        _realized("trend_continuation", -1.0, NOW - 2 * hour, symbol="NQ"),
        _realized("failed_break", 2.0, NOW - 3 * hour, symbol="NQ"),
        # Pondělí téhož týdne — jen týden
        _realized("failed_break", -1.5, dt.datetime(2026, 9, 29, 15, 0, tzinfo=dt.UTC)),
        # Stín se do brzd nepočítá, minulý týden také ne
        _realized("wall_bounce", -1.0, NOW - hour, tradeable=False),
        _realized("wall_bounce", -5.0, dt.datetime(2026, 9, 25, 15, 0, tzinfo=dt.UTC)),
    ]
    brakes = library_brakes(realized, SetupParams(), now=NOW)
    assert brakes.session == "2026-10-01"
    assert brakes.day_r == pytest.approx(0.0)
    assert brakes.week_r == pytest.approx(-1.5)
    assert brakes.block is None
    # Strop stopů šablony je napříč symboly (ES + NQ)
    assert brakes.template_stops == {"trend_continuation": 2}
    assert (brakes.daily_brake_r, brakes.weekly_brake_r) == (3.0, 6.0)
    assert brakes.max_template_stops_per_day == 2
    hit = library_brakes(
        [*realized, _realized("gamma_momentum", -3.0, NOW - hour / 2)], SetupParams(), now=NOW
    )
    assert hit.day_r == pytest.approx(-3.0)
    assert hit.block == "daily_brake"
