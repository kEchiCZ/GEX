"""Risk framework malého účtu (#1185 A): sizing, brzdy, brána šablon, kontext setupu."""

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine

from gexlens_engine.compute.risk import (
    RealizedSetup,
    affordable_results,
    brake_state,
    expectancy_lower_bound,
    position_size,
    template_gate,
    week_start,
)
from gexlens_engine.compute.settle import session_bounds, trading_session_date
from gexlens_engine.compute.setups import SetupParams, params_from_dict, params_to_dict
from gexlens_engine.runtime import PublisherLike
from gexlens_engine.setups import SetupEngine
from gexlens_engine.storage.oi_archive import OIEodRepository
from gexlens_engine.storage.setups_store import SetupsRepository

SESSION = dt.date(2026, 9, 16)  # středa
OPEN = session_bounds(SESSION)[0]


def test_sizing_ucet_50k_jedno_procento() -> None:
    # ES 10 b × 50 $ = 500 $ = přesně rozpočet → 1 kontrakt
    size = position_size(7600.0, 7590.0, 50.0, account_equity_usd=50000, risk_pct=1, risk_max_pct=2)
    assert size.contracts == 1 and size.affordable and size.max_loss_usd == 500.0
    # NQ 25 b × 20 $ = 500 $ → 1 kontrakt; 26 b → 0 → stop nad rozpočtem
    assert (
        position_size(
            29000, 28975, 20.0, account_equity_usd=50000, risk_pct=1, risk_max_pct=2
        ).contracts
        == 1
    )
    over = position_size(29000, 28974, 20.0, account_equity_usd=50000, risk_pct=1, risk_max_pct=2)
    assert not over.affordable and over.block == "stop_over_budget" and over.contracts == 0
    # Těsný stop = víc kontraktů (ES 4 b → ⌊500/200⌋ = 2, ztráta 400 $)
    tight = position_size(
        7600.0, 7596.0, 50.0, account_equity_usd=50000, risk_pct=1, risk_max_pct=2
    )
    assert tight.contracts == 2 and tight.max_loss_usd == 400.0
    # Tvrdý strop: risk_pct zvednutý nad risk_max_pct nesmí pustit ztrátu nad 2 %
    capped = position_size(
        7600.0, 7575.0, 50.0, account_equity_usd=50000, risk_pct=3, risk_max_pct=2
    )
    assert capped.block == "stop_over_cap" and not capped.affordable
    # Stop 0 b / záporný účet = neobchodovatelné, ne výjimka
    assert (
        position_size(
            7600.0, 7600.0, 50.0, account_equity_usd=50000, risk_pct=1, risk_max_pct=2
        ).contracts
        == 0
    )
    assert (
        position_size(
            7600.0, 7590.0, 50.0, account_equity_usd=0, risk_pct=1, risk_max_pct=2
        ).contracts
        == 0
    )


def _row(
    template: str,
    outcome_r: float,
    closed: dt.datetime,
    *,
    tradeable: bool | None = True,
    status: str | None = None,
    symbol: str = "ES",
    entry: float = 7600.0,
    stop: float = 7590.0,
    affordable: bool | None = None,
) -> RealizedSetup:
    return RealizedSetup(
        symbol=symbol,
        template=template,
        status=status or ("closed_stop" if outcome_r < 0 else "closed_target"),
        outcome_r=outcome_r,
        closed_ts=closed,
        tradeable=tradeable,
        affordable=affordable,
        entry=entry,
        stop=stop,
    )


def test_brzdy_den_tyden_a_sablona() -> None:
    kwargs: dict[str, Any] = {
        "session_day": SESSION,
        "daily_brake_r": 3.0,
        "weekly_brake_r": 6.0,
        "max_template_stops_per_day": 2,
    }
    t = OPEN + dt.timedelta(hours=16)
    # Dvě stopy dnes (−2 R) → nic; třetí (−3 R) → denní brzda
    two = [_row("failed_break", -1.0, t), _row("wall_bounce", -1.0, t)]
    assert brake_state(two, "failed_break", **kwargs).block is None
    three = [*two, _row("trend_continuation", -1.0, t)]
    state = brake_state(three, "failed_break", **kwargs)
    assert state.block == "daily_brake" and state.day_r == -3.0
    # Řádky před pravidly (tradeable None) a stínové (False) se nepočítají
    ignored = [
        _row("failed_break", -5.0, t, tradeable=None),
        _row("failed_break", -5.0, t, tradeable=False),
    ]
    assert brake_state(ignored, "failed_break", **kwargs).block is None
    # Týden: −6 R rozprostřených od pondělí, dnes jen −1 R → týdenní brzda
    monday = week_start(SESSION) + dt.timedelta(hours=20)
    week = [
        _row("wall_bounce", -2.5, monday),
        _row("wall_bounce", -2.5, monday + dt.timedelta(days=1)),
        _row("wall_bounce", -1.0, t),
    ]
    state = brake_state(week, "wall_bounce", **kwargs)
    assert state.block == "weekly_brake" and state.week_r == -6.0 and state.day_r == -1.0
    # Dva stopy téže šablony dnes (+ výhra jinde, den v plusu) → strop šablony jen pro ni
    stops = [
        _row("max_pain_pin", -1.0, t),
        _row("max_pain_pin", -1.0, t),
        _row("failed_break", 3.0, t),
    ]
    assert brake_state(stops, "max_pain_pin", **kwargs).block == "template_stops"
    assert brake_state(stops, "failed_break", **kwargs).block is None
    # Vypnuté brzdy (0) nikdy neblokují
    off = brake_state(
        three,
        "failed_break",
        session_day=SESSION,
        daily_brake_r=0,
        weekly_brake_r=0,
        max_template_stops_per_day=0,
    )
    assert off.block is None


def _brake_kwargs(session_day: dt.date) -> dict[str, Any]:
    return {
        "session_day": session_day,
        "daily_brake_r": 3.0,
        "weekly_brake_r": 6.0,
        "max_template_stops_per_day": 2,
    }


def test_tydenni_brzda_jen_z_aktualniho_obchodniho_tydne() -> None:
    """#1322: `realized` nese i 84denní okno brány šablon — týden si brzda vymezí sama.

    Produkce 28. 9. 2026 (pondělí): kontext ukazoval týden −2,0 R ze ztrát
    z pátku 25. 9.; další tři stopy by spustily brzdu −6 R až do prosince.
    """
    monday = dt.date(2026, 9, 28)
    start = week_start(monday)
    assert start == dt.datetime(2026, 9, 27, 22, 0, tzinfo=dt.UTC)  # neděle 17:00 CDT
    friday = dt.datetime(2026, 9, 25, 18, 0, tzinfo=dt.UTC)
    old = [
        _row("wall_bounce", -1.0, friday),
        _row("wall_bounce", -1.0, friday),
        _row("failed_break", -6.0, friday - dt.timedelta(days=30)),
    ]
    state = brake_state(old, "wall_bounce", **_brake_kwargs(monday))
    assert (state.week_r, state.day_r, state.template_stops, state.block) == (0.0, 0.0, 0, None)
    # Tři pondělní stopy: denní brzda ano, týden jen −3 R (staré ztráty se nepřičtou)
    t = start + dt.timedelta(hours=16)
    today = [_row("trend_continuation", -1.0, t) for _ in range(3)]
    state = brake_state([*old, *today], "wall_bounce", **_brake_kwargs(monday))
    assert (state.week_r, state.day_r, state.block) == (-3.0, -3.0, "daily_brake")
    # Úterý po pondělních −3 R: další −3 R → teprve teď týdenní brzda
    tuesday = dt.date(2026, 9, 29)
    tue = session_bounds(tuesday)[0] + dt.timedelta(hours=16)
    week = [*old, *today, _row("wall_bounce", -2.0, tue), _row("failed_break", -1.0, tue)]
    state = brake_state(week, "failed_break", **_brake_kwargs(tuesday))
    assert (state.week_r, state.day_r, state.block) == (-6.0, -3.0, "daily_brake")
    state = brake_state(week, "failed_break", **{**_brake_kwargs(tuesday), "daily_brake_r": 0.0})
    assert state.block == "weekly_brake"


def test_hranice_tydne_polouzavrena() -> None:
    """Týden = [open pondělní seance, konec dnešní seance) — stejná konvence jako den."""
    wednesday = dt.date(2026, 9, 30)
    start = week_start(wednesday)
    _, day_to = session_bounds(wednesday)
    # Přesně v otevření pondělní seance → do týdne (ne do dne); minutu dřív → minulý týden
    at_edge = brake_state([_row("wall_bounce", -6.0, start)], "x", **_brake_kwargs(wednesday))
    assert (at_edge.week_r, at_edge.day_r, at_edge.block) == (-6.0, 0.0, "weekly_brake")
    before = start - dt.timedelta(minutes=1)
    earlier = brake_state([_row("wall_bounce", -6.0, before)], "x", **_brake_kwargs(wednesday))
    assert (earlier.week_r, earlier.block) == (0.0, None)
    # Uzavření v otevření další seance už do dnešní seance ani týdne nepatří
    later = brake_state([_row("wall_bounce", -6.0, day_to)], "x", **_brake_kwargs(wednesday))
    assert (later.week_r, later.day_r) == (0.0, 0.0)


@pytest.mark.parametrize(
    ("now", "session_day", "block"),
    [
        # Sobota: obchodní den soboty, týden od neděle 20. 9. 17:00 CDT → páteční −6 R platí
        (dt.datetime(2026, 9, 26, 12, 0, tzinfo=dt.UTC), dt.date(2026, 9, 26), "weekly_brake"),
        # Neděle minutu před otevřením Globexu: pořád tentýž týden, brzda drží
        (dt.datetime(2026, 9, 27, 21, 59, tzinfo=dt.UTC), dt.date(2026, 9, 27), "weekly_brake"),
        # Neděle 17:00 CDT = pondělní seance → nový týden, páteční ztráty už ne
        (dt.datetime(2026, 9, 27, 22, 0, tzinfo=dt.UTC), dt.date(2026, 9, 28), None),
    ],
)
def test_tydenni_brzda_o_vikendu_do_nedelniho_otevreni(
    now: dt.datetime, session_day: dt.date, block: str | None
) -> None:
    """#1322 + AGENTS „obchodní den“: sobota, neděle před a po 17:00 CT proti pátku.

    Alert slibuje „do konce obchodního týdne (neděle 17:00 CT)“ — tady je to hlídané.
    """
    assert trading_session_date(now) == session_day
    friday = dt.datetime(2026, 9, 25, 18, 0, tzinfo=dt.UTC)  # pátek 13:00 CDT
    rows = [_row("wall_bounce", -3.0, friday), _row("failed_break", -3.0, friday)]
    state = brake_state(rows, "x", **_brake_kwargs(session_day))
    assert state.day_r == 0.0  # páteční seance skončila v pátek 17:00 CT
    assert state.block == block
    assert state.week_r == (-6.0 if block else 0.0)


def test_den_a_strop_sablony_beze_zmeny_s_tydnem() -> None:
    """Regrese #1322: den a strop stopů šablony počítají jen dnešní seanci."""
    wednesday = dt.date(2026, 9, 30)
    day_from, _ = session_bounds(wednesday)
    t = day_from + dt.timedelta(hours=16)
    monday = week_start(wednesday) + dt.timedelta(hours=16)
    last_week = week_start(wednesday) - dt.timedelta(days=2)
    rows = [
        # Stopy téže šablony z pondělí a z minulého týdne se do stropu nepočítají
        _row("max_pain_pin", -1.0, monday),
        _row("max_pain_pin", -1.0, last_week),
        _row("max_pain_pin", -1.0, last_week),
        _row("max_pain_pin", -1.0, t),
    ]
    state = brake_state(rows, "max_pain_pin", **_brake_kwargs(wednesday))
    assert (state.day_r, state.week_r, state.template_stops, state.block) == (
        -1.0,
        -2.0,
        1,
        None,
    )
    state = brake_state(
        [*rows, _row("max_pain_pin", -1.0, t)], "max_pain_pin", **_brake_kwargs(wednesday)
    )
    assert (state.day_r, state.template_stops, state.block) == (-2.0, 2, "template_stops")


def test_brana_sablony_dolni_mez_ocekavani() -> None:
    assert expectancy_lower_bound([1.0]) is None
    # 40 výsledků Ø +0,5 R s malým rozptylem → LB > 0 → pass
    good = [0.4, 0.6] * 20
    assert template_gate(good, min_samples=30, enabled=True).verdict == "pass"
    # Ø +0,03 R s rozptylem stopů a cílů (v5 realita) → LB < 0 → block
    real = [2.0] * 8 + [-1.0] * 22 + [0.3] * 10
    gate = template_gate(real, min_samples=30, enabled=True)
    assert gate.verdict == "block" and gate.lower_bound is not None and gate.lower_bound < 0
    assert template_gate(good[:20], min_samples=30, enabled=True).verdict == "insufficient"
    assert template_gate(real, min_samples=30, enabled=False).verdict == "off"


def _gate_input(rows: list[RealizedSetup], symbol: str, point_value: float) -> list[float]:
    t = OPEN + dt.timedelta(hours=16)
    return affordable_results(
        rows,
        "failed_break",
        symbol,
        since=t - dt.timedelta(days=84),
        point_value_usd=point_value,
        account_equity_usd=50000,
        risk_pct=1,
        risk_max_pct=2,
    )


def test_brana_pocita_jen_zobchodovatelne_a_dopocitava_stare_radky() -> None:
    t = OPEN + dt.timedelta(hours=16)
    rows = [
        _row("failed_break", 2.0, t, affordable=True),  # nový řádek, v rozpočtu
        _row("failed_break", -1.0, t, affordable=False),  # nový řádek, stop nad rozpočtem
        _row("failed_break", 1.0, t, tradeable=None, stop=7590.0),  # starý, 10 b ES → v rozpočtu
        _row("failed_break", -1.0, t, tradeable=None, stop=7560.0),  # starý, 40 b → mimo
        _row("wall_bounce", 5.0, t, affordable=True),  # jiná šablona
        _row("failed_break", 9.0, t - dt.timedelta(days=100), affordable=True),  # mimo okno
    ]
    assert _gate_input(rows, "ES", 50.0) == [2.0, 1.0]


def test_brana_ignoruje_radky_ciziho_symbolu() -> None:
    """#1325: klíč brány je šablona × symbol. Do opravy se řádky NQ s `affordable`
    započítaly i do brány ES (a naopak), řádky před pravidly jen z vlastního."""
    t = OPEN + dt.timedelta(hours=16)
    nq_stop = 29000.0 - 20.0  # 20 b NQ × 20 $ = 400 $ → v rozpočtu
    rows = [
        _row("failed_break", 2.0, t, affordable=True),
        _row("failed_break", -1.0, t, affordable=True, symbol="NQ", entry=29000.0, stop=nq_stop),
        _row("failed_break", 1.5, t, tradeable=None, symbol="NQ", entry=29000.0, stop=nq_stop),
    ]
    assert _gate_input(rows, "ES", 50.0) == [2.0]
    assert _gate_input(rows, "NQ", 20.0) == [-1.0, 1.5]
    assert _gate_input(rows, "RTY", 50.0) == []


def test_brana_pinovany_kontrakt_ma_vlastni_bunku() -> None:
    """Klíč brány je ticker instance (ADR-0041 bod 3), ne kořen produktu: pinovaný
    `ESZ6` nepřebírá vzorek `ES` (a naopak) a začíná na n = 0 → insufficient."""
    t = OPEN + dt.timedelta(hours=16)
    rows = [_row("failed_break", 2.0, t, affordable=True) for _ in range(35)]
    rows.append(_row("failed_break", -1.0, t, affordable=True, symbol="ESZ6"))
    assert _gate_input(rows, "ESZ6", 50.0) == [-1.0]
    assert len(_gate_input(rows, "ES", 50.0)) == 35
    gate = template_gate(_gate_input(rows, "ESZ6", 50.0), min_samples=30, enabled=True)
    assert (gate.verdict, gate.n) == ("insufficient", 1)


def test_brana_dopocita_stary_radek_hodnotou_bodu_vlastniho_symbolu() -> None:
    """Řádek před pravidly se dopočítá hodnotou bodu symbolu, pro který brána rozhoduje."""
    t = OPEN + dt.timedelta(hours=16)
    # 15 b: ES 15 × 50 $ = 750 $ > 500 $ (mimo), NQ 15 × 20 $ = 300 $ (v rozpočtu)
    es_old = [_row("failed_break", 1.0, t, tradeable=None, entry=7600.0, stop=7585.0)]
    nq_old = [
        _row("failed_break", 1.0, t, tradeable=None, symbol="NQ", entry=29000.0, stop=28985.0)
    ]
    assert _gate_input(es_old, "ES", 50.0) == []
    assert _gate_input(nq_old, "NQ", 20.0) == [1.0]
    # Táž geometrie s cizí hodnotou bodu by dala opačný verdikt — proto vlastní symbol
    assert _gate_input(es_old, "ES", 20.0) == [1.0]
    assert _gate_input(nq_old, "NQ", 50.0) == []


def test_risk_parametry_jsou_ve_store() -> None:
    params = params_to_dict(SetupParams())
    assert params["account_equity_usd"] == 50000.0 and params["risk_pct"] == 1.0
    assert params["template_gate_enabled"] is True
    loaded = params_from_dict({"risk_pct": 2, "template_gate_enabled": False})
    assert loaded.risk_pct == 2.0 and loaded.template_gate_enabled is False
    with pytest.raises(ValueError):
        params_from_dict({"template_gate_enabled": 1})


class _Publisher(PublisherLike):
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def status(self, **fields: object) -> None:  # pragma: no cover
        pass

    async def publish(self, channel: str, data: dict[str, object]) -> None:
        self.events.append({"channel": channel, **data})


async def test_setup_engine_zapisuje_risk_kontext_a_brzdu(tmp_path: Path) -> None:
    db = create_engine(f"sqlite+pysqlite:///{tmp_path / 'setups.sqlite'}")
    repository = SetupsRepository(db)
    repository.ensure_schema()
    oi_repo = OIEodRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'oi.sqlite'}"))
    oi_repo.ensure_schema()
    publisher = _Publisher()
    engine = SetupEngine(
        symbol="ES", repository=repository, oi_repository=oi_repo, publisher=publisher
    )
    now = OPEN + dt.timedelta(hours=16)
    # Track record: 35 failed_break v rozpočtu s kladnou dolní mezí → brána pass
    for i in range(35):
        sid = repository.create(
            symbol="ES",
            expiry="20260915",
            template="failed_break",
            direction="long",
            created_ts=now - dt.timedelta(days=2),
            entry=7600.0,
            target=7620.0,
            stop=7592.0,
            confidence=50,
            reason="historie",
            context={"affordable": True, "tradeable": True},
        )
        repository.close(
            sid,
            status="closed_target" if i < 25 else "closed_stop",
            closed_ts=now - dt.timedelta(days=2),
            outcome_r=1.0 if i < 25 else -1.0,
            mfe=1,
            mae=0,
        )
    realized = engine._load_realized(now)
    assert len(realized) == 35
    # Kandidát v rozpočtu: 8 b ES → 1 kontrakt, 400 $, obchodovatelný
    risk, brakes = engine._risk_context(realized, "failed_break", 7600.0, 7592.0, 50.0, now)
    assert risk["tradeable"] is True and risk["contracts"] == 1 and risk["max_loss_usd"] == 400.0
    assert risk["template_gate"] == "pass" and risk["trade_block"] is None and brakes.block is None
    # Stop 14 b → stín (stop nad rozpočtem), brána se pořád zapíše
    risk, _ = engine._risk_context(realized, "failed_break", 7600.0, 7586.0, 50.0, now)
    assert risk["tradeable"] is False and risk["trade_block"] == "stop_over_budget"
    # Šablona bez vzorku → gate insufficient → stín
    risk, _ = engine._risk_context(realized, "wall_bounce", 7600.0, 7592.0, 50.0, now)
    assert risk["trade_block"] == "gate" and risk["template_gate"] == "insufficient"
    # Dnes −3 R obchodovatelných → denní brzda, alert jednou za seanci
    for _ in range(3):
        sid = repository.create(
            symbol="NQ",
            expiry="20260916",
            template="trend_continuation",
            direction="short",
            created_ts=now - dt.timedelta(hours=1),
            entry=29000.0,
            target=28950.0,
            stop=29020.0,
            confidence=50,
            reason="dnes",
            context={"affordable": True, "tradeable": True},
        )
        repository.close(
            sid,
            status="closed_stop",
            closed_ts=now - dt.timedelta(minutes=30),
            outcome_r=-1.0,
            mfe=0,
            mae=1,
        )
    realized = engine._load_realized(now)
    risk, brakes = engine._risk_context(realized, "failed_break", 7600.0, 7592.0, 50.0, now)
    assert risk["trade_block"] == "daily_brake" and risk["realized_day_r"] == -3.0
    await engine._alert_brake(brakes, now)
    await engine._alert_brake(brakes, now + dt.timedelta(minutes=5))
    brake_alerts = [e for e in publisher.events if e.get("kind") == "risk_brake"]
    assert len(brake_alerts) == 1 and brake_alerts[0]["event"] == "daily_brake"
    assert "-3.0 R" in str(brake_alerts[0]["message"])
    # Okno brzdy končí otevřením další seance, ne settle (#1322)
    assert "do konce seance (17:00 CT)" in str(brake_alerts[0]["message"])
    assert "settle" not in str(brake_alerts[0]["message"])


def _closed_setup(
    repository: SetupsRepository,
    *,
    expiry: str,
    created: dt.datetime,
    closed: dt.datetime,
    outcome_r: float,
    status: str = "closed_stop",
) -> int:
    sid = repository.create(
        symbol="ES",
        expiry=expiry,
        template="failed_break",
        direction="long",
        created_ts=created,
        entry=7600.0,
        target=7620.0,
        stop=7592.0,
        confidence=50,
        reason="test",
        context={"affordable": True, "tradeable": True},
    )
    repository.close(sid, status=status, closed_ts=closed, outcome_r=outcome_r, mfe=0, mae=1)
    return sid


def _setup_engine(tmp_path: Path) -> tuple[SetupEngine, SetupsRepository]:
    repository = SetupsRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 's.sqlite'}"))
    repository.ensure_schema()
    oi_repo = OIEodRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'oi.sqlite'}"))
    oi_repo.ensure_schema()
    engine = SetupEngine(
        symbol="ES", repository=repository, oi_repository=oi_repo, publisher=_Publisher()
    )
    return engine, repository


def test_kontext_setupu_nese_tyden_bez_starsich_ztrat_okna_brany(tmp_path: Path) -> None:
    """#1322 end-to-end: `_load_realized` čte 84 dní kvůli bráně, týden ne."""
    engine, repository = _setup_engine(tmp_path)
    now = OPEN + dt.timedelta(hours=16)  # středa 16. 9. 2026 14:00 UTC
    # Sedm obchodovatelných stopů z minulého týdne (8. 9.) — v okně brány, mimo týden
    for _ in range(7):
        _closed_setup(
            repository,
            expiry="20260908",
            created=dt.datetime(2026, 9, 8, 14, 0, tzinfo=dt.UTC),
            closed=dt.datetime(2026, 9, 8, 15, 0, tzinfo=dt.UTC),
            outcome_r=-1.0,
        )
    realized = engine._load_realized(now)
    assert len(realized) == 7  # brána je dál vidí
    risk, brakes = engine._risk_context(realized, "failed_break", 7600.0, 7592.0, 50.0, now)
    assert risk["realized_week_r"] == 0.0 and risk["realized_day_r"] == 0.0
    assert brakes.block is None and risk["trade_block"] != "weekly_brake"
    assert risk["template_gate_n"] == 7


def test_realizovane_bez_setupu_vzniklych_po_settle(tmp_path: Path) -> None:
    """#1324: setup vzniklý po settle vlastní expirace nevstupuje do brzd ani brány."""
    engine, repository = _setup_engine(tmp_path)
    now = OPEN + dt.timedelta(hours=16)  # středa 16. 9. 2026 14:00 UTC
    settle = dt.datetime(2026, 9, 15, 20, 0, tzinfo=dt.UTC)  # 15. 9. 16:00 EDT
    _closed_setup(
        repository,
        expiry="20260915",
        created=settle - dt.timedelta(minutes=1),
        closed=settle,
        outcome_r=-1.0,
    )
    for created in (settle, settle + dt.timedelta(minutes=122)):
        _closed_setup(
            repository,
            expiry="20260915",
            created=created,
            closed=created + dt.timedelta(minutes=1),
            outcome_r=-1.0,
            status="closed_timeout",
        )
    realized = engine._load_realized(now)
    assert [row.closed_ts for row in realized] == [settle]
    risk, _ = engine._risk_context(realized, "failed_break", 7600.0, 7592.0, 50.0, now)
    assert risk["realized_week_r"] == -1.0 and risk["template_gate_n"] == 1


def _history(
    repository: SetupsRepository,
    symbol: str,
    outcomes: list[float],
    *,
    entry: float,
    stop: float,
    context: dict[str, object],
) -> None:
    """Uzavřené failed_break z minulého týdne (8. 9.) — v okně brány, mimo týdenní brzdu."""
    for outcome_r in outcomes:
        sid = repository.create(
            symbol=symbol,
            expiry="20260908",
            template="failed_break",
            direction="long",
            created_ts=dt.datetime(2026, 9, 8, 14, 0, tzinfo=dt.UTC),
            entry=entry,
            target=entry + 2 * (entry - stop),
            stop=stop,
            confidence=50,
            reason="historie",
            context=context,
        )
        repository.close(
            sid,
            status="closed_target" if outcome_r > 0 else "closed_stop",
            closed_ts=dt.datetime(2026, 9, 8, 15, 0, tzinfo=dt.UTC),
            outcome_r=outcome_r,
            mfe=1,
            mae=1,
        )


def test_brana_es_a_nq_maji_vlastni_vstup_a_verdikt(tmp_path: Path) -> None:
    """#1325: dvě instance nad jedním track recordem — každá hodnotí šablonu jen
    na svém symbolu a starší řádky dopočítá svou hodnotou bodu. Do opravy braly
    obě instance i řádky cizího symbolu s `affordable` a dopočtené jen z vlastního
    (ES n = 60, NQ n = 70) — verdikt tak závisel na tom, která instance se ptá."""
    engine_es, repository = _setup_engine(tmp_path)
    engine_nq = SetupEngine(
        symbol="NQ",
        repository=repository,
        oi_repository=engine_es.oi_repository,
        publisher=_Publisher(),
    )
    shadow: dict[str, object] = {"affordable": True, "tradeable": False}
    # ES: 10 cílů po +2 R, 25 stopů → Ø −0,14 R → block
    _history(repository, "ES", [2.0] * 10 + [-1.0] * 25, entry=7600.0, stop=7592.0, context=shadow)
    # NQ: 25 řádků s pravidly (20 cílů, 5 stopů) + 10 cílů před pravidly se stopem 20 b
    _history(repository, "NQ", [1.0] * 20 + [-1.0] * 5, entry=29000.0, stop=28980.0, context=shadow)
    _history(repository, "NQ", [1.0] * 10, entry=29000.0, stop=28980.0, context={})
    now = OPEN + dt.timedelta(hours=16)
    realized = engine_es._load_realized(now)
    assert len(realized) == 70  # brzdy čtou oba symboly
    assert engine_nq._load_realized(now) == realized  # vstup je týž, klíč ne

    es, _ = engine_es._risk_context(realized, "failed_break", 7600.0, 7592.0, 50.0, now)
    nq, _ = engine_nq._risk_context(realized, "failed_break", 29000.0, 28980.0, 20.0, now)
    es_lb, nq_lb = es["template_gate_lb"], nq["template_gate_lb"]
    assert (es["template_gate"], es["template_gate_n"]) == ("block", 35)
    assert isinstance(es_lb, float) and es_lb < 0
    assert es["tradeable"] is False and es["trade_block"] == "gate"
    assert (nq["template_gate"], nq["template_gate_n"]) == ("pass", 35)
    assert isinstance(nq_lb, float) and nq_lb > 0
    assert nq["tradeable"] is True and nq["trade_block"] is None and nq["contracts"] == 1
    # Význam template_gate* se změnil → nová verze pravidel v kontextu
    assert es["risk_rules_version"] == nq["risk_rules_version"] == 2
