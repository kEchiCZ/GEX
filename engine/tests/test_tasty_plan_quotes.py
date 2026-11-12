"""Odběr kontraktů plánu opce QQQ a podkladu do close (F1b #1747, ADR-0062 K2 A) nad mockem tasty.

PG = SQLite v `tmp_path`, Parquet v `tmp_path`; živé tasty se nevolá (`tasty/mock.py`).
"""

import asyncio
import datetime as dt
import logging
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest
from sqlalchemy import create_engine

from gexlens_engine.config import Settings
from gexlens_engine.storage.option_plans_store import OptionPlansRepository
from gexlens_engine.storage.parquet_store import SnapshotWriter
from gexlens_engine.tasty.budget import ADHOC_RESERVE_ENTRIES, EVENTS_BY_PURPOSE, plan_subscriptions
from gexlens_engine.tasty.mock import feed_greeks, feed_quote
from gexlens_engine.tasty.plan_chain import MAX_AGE_S
from gexlens_engine.tasty.plan_quotes import (
    PLAN_REFRESH_S,
    PURPOSE,
    PlanQuoteRecorder,
    plan_contracts,
    quote_row,
)
from gexlens_engine.tasty.provider import TastyChainCache
from gexlens_engine.tasty.symbols import ChainSymbols

UTC = dt.UTC
#: Út 3. 11. 2026 (EST): plán v 9:15 ET = 14:15 UTC, US open 14:30 UTC, settle 21:00 UTC
SESSION = dt.date(2026, 11, 3)
EXPIRY = "20261104"
P687 = ".QQQ261104P687"
P686 = ".QQQ261104P686"
C705 = ".QQQ261104C705"
C706 = ".QQQ261104C706"


def _ts(hour: int, minute: int, second: float = 0.0, day: dt.date = SESSION) -> dt.datetime:
    whole = int(second)
    micro = round((second - whole) * 1_000_000)
    return dt.datetime(day.year, day.month, day.day, hour, minute, whole, micro, tzinfo=UTC)


def _run[T](work: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(work)


def _variant(role: str, right: str, strike: float, expiry: str = "2026-11-04") -> dict[str, Any]:
    return {"role": role, "right": right, "strike": strike, "expiry": expiry}


def _part(*variants: dict[str, Any]) -> dict[str, Any]:
    return {"variants": list(variants)}


PUTS = _part(_variant("hlavní", "P", 687.0), _variant("záložní", "P", 686.0))
CALLS = _part(_variant("hlavní", "C", 705.0), _variant("záložní", "C", 706.0))


def _plan(main: dict[str, Any] | None = PUTS, **extra: Any) -> dict[str, Any]:
    """JSON `plan` v tvaru, který čte `effective_plan` (F2b, F2c)."""
    shadows = {"entry_b": main, "wick_test": main} if main is not None else {}
    return {
        "slot": "9:15",
        "reasons": [] if main is not None else ["ranní snímek 9:15 ET neúplný"],
        "levels_nq": None,
        "main": main,
        "shadows": shadows,
        **extra,
    }


class _Clock:
    def __init__(self, now: dt.datetime) -> None:
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now


class _FakeSymbolMap:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def chain(self, product: str, today: dt.date) -> ChainSymbols:
        self.calls += 1
        if self.fail:
            raise RuntimeError("REST 502")
        assert product == "QQQ"
        return ChainSymbols(
            product="QQQ",
            day=today,
            by_contract={
                (EXPIRY, float(strike), right): f".QQQ261104{right}{strike}"
                for strike in range(680, 711)
                for right in ("C", "P")
            },
        )

    async def front_future(self, product: str) -> str | None:
        return "QQQ"  # akcie/ETF: podklad je symbol sám (#206)


class _Harness:
    def __init__(self, tmp_path: Path, now: dt.datetime) -> None:
        self.clock = _Clock(now)
        self.cache = TastyChainCache(clock=self.clock)
        db = create_engine(f"sqlite+pysqlite:///{tmp_path / 'db.sqlite'}")
        self.plans = OptionPlansRepository(db)
        self.plans.ensure_schema()
        self.settings = Settings(data_dir=tmp_path)
        self.symbols = _FakeSymbolMap()
        self.wakes = 0
        self.recorder = self.new_recorder()

    def new_recorder(self) -> PlanQuoteRecorder:
        """Nová instance = restart enginu (nový writer i stav v paměti, PG a Parquet zůstávají)."""

        def wake() -> None:
            self.wakes += 1

        return PlanQuoteRecorder(
            repository=self.plans,
            symbol_map=self.symbols,
            cache=self.cache,
            writer=SnapshotWriter(self.settings),
            on_change=wake,
            clock=self.clock,
        )

    def add_plan(
        self, plan: dict[str, Any], *, day: dt.date = SESSION, scenario_id: int = 35
    ) -> int:
        plan_id = self.plans.create(
            scenario_id=scenario_id,
            day=day,
            created_at=_ts(14, 15, day=day),
            rules_version=1,
            verdict="čeká" if plan["main"] is not None else None,
            status="sestaven" if plan["main"] is not None else "bez opce",
            plan=plan,
        )
        assert plan_id is not None
        return plan_id

    def at(self, moment: dt.datetime) -> None:
        self.clock.now = moment

    def tick(self, moment: dt.datetime) -> None:
        self.at(moment)
        _run(self.recorder.tick())

    def feed(self, moment: dt.datetime, *, spot: float = 700.0, quotes: bool = True) -> None:
        """Kotace v okamžiku `moment`: podklad, a s `quotes` oba puty s Greeks."""
        self.at(moment)
        feed_quote(self.cache, "QQQ", spot - 0.01, spot + 0.01)
        if quotes:
            feed_quote(self.cache, P687, 1.38, 1.42)
            feed_greeks(self.cache, P687, 0.30, -0.25, 0.05)
            feed_quote(self.cache, P686, 1.18, 1.22)
            feed_greeks(self.cache, P686, 0.31, -0.22, 0.04)

    def quotes(self, day: dt.date = SESSION) -> list[dict[str, Any]]:
        path = self.settings.derived_dir / "QQQ" / "option_quotes" / f"{day.isoformat()}.parquet"
        return pq.read_table(path).to_pylist() if path.exists() else []

    def bars(self, day: dt.date = SESSION) -> list[dict[str, Any]]:
        path = self.settings.derived_dir / "QQQ" / "bars" / f"{day.isoformat()}.parquet"
        return pq.read_table(path).to_pylist() if path.exists() else []


def test_minuta_zapise_kotace_obou_kontraktu_a_bar_podkladu(tmp_path: Path) -> None:
    h = _Harness(tmp_path, _ts(14, 16, 10))
    h.add_plan(_plan())
    h.tick(_ts(14, 16, 10))
    # Hlavní a záložní put; stíny mají stejné kontrakty (K10 B) → bez duplicit
    assert h.recorder.streamers() == {P687, P686, "QQQ"}
    assert h.wakes == 1
    h.feed(_ts(14, 16, 30))
    h.tick(_ts(14, 16, 30))
    h.feed(_ts(14, 17, 1), spot=700.0)
    h.tick(_ts(14, 17, 1))  # zapíše minutu 14:16 = minuta začátku odběru → nic
    assert h.quotes() == [] and h.bars() == []
    h.feed(_ts(14, 17, 30), spot=700.5)
    h.tick(_ts(14, 17, 30))
    h.feed(_ts(14, 17, 55), spot=699.5)
    h.tick(_ts(14, 17, 55))
    h.tick(_ts(14, 18, 0.05))

    rows = {(row["strike"], row["right"]): row for row in h.quotes()}
    assert set(rows) == {(686.0, "P"), (687.0, "P")}
    main = rows[(687.0, "P")]
    assert main["ts_min"] == _ts(14, 17)
    assert main["expiry"] == EXPIRY
    assert (main["bid"], main["ask"], main["mid"]) == (1.38, 1.42, pytest.approx(1.40))
    assert (main["iv"], main["delta"]) == (0.30, -0.25)
    assert main["quote_age_s"] == pytest.approx(5.05)
    (bar,) = h.bars()
    assert bar["ts_min"] == _ts(14, 17)
    assert (bar["open"], bar["high"], bar["low"], bar["close"]) == (700.0, 700.5, 699.5, 699.5)
    assert bar["volume"] == 0.0


def test_opakovany_zapis_minuty_neduplikuje_ani_po_restartu(tmp_path: Path) -> None:
    h = _Harness(tmp_path, _ts(15, 0, 10))
    h.add_plan(_plan())
    h.tick(_ts(15, 0, 10))
    h.feed(_ts(15, 1, 30))
    h.tick(_ts(15, 1, 30))
    h.tick(_ts(15, 2, 0.05))
    assert len(h.quotes()) == 2 and len(h.bars()) == 1
    _run(h.recorder.write_minute(_ts(15, 1), _ts(15, 2, 1)))
    assert len(h.quotes()) == 2
    # Restart: nová instance i writer, partice se načte a minuta se přepíše, ne zdvojí
    h.recorder = h.new_recorder()
    h.tick(_ts(15, 2, 5))
    _run(h.recorder.write_minute(_ts(15, 1), _ts(15, 2, 6)))
    assert len(h.quotes()) == 2
    assert [row["ts_min"] for row in h.quotes()] == [_ts(15, 1)] * 2


def test_restart_obnovi_odber_z_planu_dne_v_pg(tmp_path: Path) -> None:
    h = _Harness(tmp_path, _ts(16, 40, 20))
    h.add_plan(_plan())
    # Engine startuje uprostřed RTH: odběr se obnoví z plánu dne, bez nového plánu
    h.tick(_ts(16, 40, 20))
    assert h.recorder.session == SESSION
    assert h.recorder.streamers() == {P687, P686, "QQQ"}
    h.feed(_ts(16, 41, 10))
    h.tick(_ts(16, 41, 10))
    h.tick(_ts(16, 42, 0.05))
    assert {row["ts_min"] for row in h.quotes()} == {_ts(16, 41)}


def test_po_close_se_odber_uvolni_a_minuta_po_settle_se_nezapise(tmp_path: Path) -> None:
    h = _Harness(tmp_path, _ts(20, 58, 10))
    h.add_plan(_plan())
    h.tick(_ts(20, 58, 10))
    h.feed(_ts(20, 59, 20))
    h.tick(_ts(20, 59, 20))
    wakes = h.wakes
    # 21:00 UTC = settle 16:00 ET: poslední minuta 20:59 se zapíše, pak odběr pryč
    h.tick(_ts(21, 0, 0.05))
    assert {row["ts_min"] for row in h.quotes()} == {_ts(20, 59)}
    assert h.recorder.streamers() == set() and h.recorder.session is None
    assert h.wakes == wakes + 1
    h.feed(_ts(21, 0, 30))
    h.tick(_ts(21, 0, 30))
    h.tick(_ts(21, 1, 0.05))
    assert {row["ts_min"] for row in h.quotes()} == {_ts(20, 59)}
    assert h.recorder.streamers() == set()


def test_pred_planem_neodebira_nic_a_pg_necte(tmp_path: Path) -> None:
    h = _Harness(tmp_path, _ts(14, 14, 50))
    h.add_plan(_plan())
    h.tick(_ts(14, 14, 50))  # před 9:15 ET plán vzniknout nemůže
    assert h.recorder.streamers() == set() and h.symbols.calls == 0
    h.tick(_ts(14, 15, 5))
    assert h.recorder.streamers() == {P687, P686, "QQQ"}


def test_plan_sestaveny_v_9_31_prepne_kontrakty(tmp_path: Path) -> None:
    h = _Harness(tmp_path, _ts(14, 16, 10))
    plan_id = h.add_plan(_plan(main=None))
    # Plán bez opce z 9:15: kontrakty nejsou, podklad ano (plán z 9:31 čte bary od openu)
    h.tick(_ts(14, 16, 10))
    assert h.recorder.streamers() == {"QQQ"}
    rebuilt = {"slot": "9:31", "levels_nq": None, "main": CALLS, "shadows": {"entry_b": CALLS}}
    repricing: dict[str, Any] = {
        "slot": "9:31",
        "reasons": [],
        "chain": None,
        "prices": None,
        "plan": rebuilt,
    }
    assert h.plans.record_repricing(plan_id, repricing, status="sestaven", verdict="čeká")
    h.tick(_ts(14, 32, 0.05))
    assert h.recorder.streamers() == {C705, C706, "QQQ"}
    feed_quote(h.cache, C705, 1.5, 1.6)
    feed_quote(h.cache, C706, 1.2, 1.3)
    feed_quote(h.cache, "QQQ", 700.0, 700.02)
    # Minuta 14:32 je minuta začátku odběru callů → první řádky callů až za 14:33
    h.tick(_ts(14, 33, 0.05))
    assert h.quotes() == []
    h.at(_ts(14, 33, 30))
    feed_quote(h.cache, C705, 1.5, 1.6)
    feed_quote(h.cache, C706, 1.2, 1.3)
    h.tick(_ts(14, 34, 0.05))
    assert {(row["strike"], row["right"]) for row in h.quotes()} == {(705.0, "C"), (706.0, "C")}


def test_plan_se_cte_jen_po_periode(tmp_path: Path) -> None:
    h = _Harness(tmp_path, _ts(15, 0, 0.05))
    h.add_plan(_plan(main=None))
    h.tick(_ts(15, 0, 0.05))
    calls = h.symbols.calls
    h.tick(_ts(15, 0, 5))  # bez nového čtení PG (à PLAN_REFRESH_S), kontrakty beze změny
    h.tick(_ts(15, 0, 0.05 + PLAN_REFRESH_S))
    assert h.symbols.calls == calls  # tytéž kontrakty → řetěz se znovu nečte


def test_chybejici_kotace_je_mezera_se_starim_a_hlidac_hlasi_jen_zmenu(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="gexlens_engine.tasty.plan_quotes")
    h = _Harness(tmp_path, _ts(15, 0, 10))
    h.add_plan(_plan())
    h.tick(_ts(15, 0, 10))
    h.at(_ts(15, 0, 20))
    feed_quote(h.cache, P687, 1.38, 1.42)  # pak už nic: v 15:02 je kotace 100 s stará
    h.feed(_ts(15, 1, 40), quotes=False)
    h.tick(_ts(15, 1, 40))
    h.tick(_ts(15, 2, 0.05))
    rows = {row["strike"]: row for row in h.quotes()}
    assert rows[687.0]["bid"] is None and rows[687.0]["mid"] is None
    assert rows[687.0]["quote_age_s"] == pytest.approx(100.05)
    assert rows[686.0]["bid"] is None and rows[686.0]["quote_age_s"] is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "686P (bez kotace)" in warnings[0].getMessage()
    assert "687P (stáří 100 s)" in warnings[0].getMessage()
    # Táž mezera další minutu: hlídač nic nového nehlásí (stáří se mění, množina ne)
    h.feed(_ts(15, 2, 30), quotes=False)
    h.tick(_ts(15, 2, 30))
    h.tick(_ts(15, 3, 0.05))
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    # Kotace zase tečou → INFO
    h.feed(_ts(15, 3, 30))
    h.tick(_ts(15, 3, 30))
    h.tick(_ts(15, 4, 0.05))
    assert any("kotace zase tečou" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    ("now", "level"),
    [
        (_ts(15, 0, day=dt.date(2026, 11, 7)), logging.INFO),  # sobota
        (_ts(22, 30), logging.INFO),  # denní pauza Globexu (17:30 ET)
        (_ts(22, 30, day=dt.date(2026, 11, 8)), logging.INFO),  # neděle před otevřením
        (_ts(23, 30, day=dt.date(2026, 11, 8)), logging.WARNING),  # nedělní otevření Globexu
        (_ts(15, 5), logging.WARNING),  # RTH
    ],
)
def test_hlidac_pri_zavrenem_trhu_jen_info(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, now: dt.datetime, level: int
) -> None:
    caplog.set_level(logging.INFO, logger="gexlens_engine.tasty.plan_quotes")
    h = _Harness(tmp_path, _ts(15, 0, 10))
    h.add_plan(_plan())
    h.tick(_ts(15, 0, 10))
    caplog.clear()
    # Kontrakty bez kotace; minuta seance, vzorek v okamžiku `now`
    _run(h.recorder.write_minute(_ts(15, 4), now))
    gaps = [r for r in caplog.records if "bez kotace" in r.getMessage()]
    assert [r.levelno for r in gaps] == [level]


def test_obchodni_den_sobota_nedele_a_pondeli_proti_patku(tmp_path: Path) -> None:
    friday, saturday, sunday, monday = (dt.date(2026, 11, day) for day in (6, 7, 8, 9))
    h = _Harness(tmp_path, _ts(14, 20, day=saturday))
    h.add_plan(_plan(), day=friday, scenario_id=1)
    h.add_plan(_plan(), day=saturday, scenario_id=2)  # chybný řádek: sobota seanci nemá
    for moment in (
        _ts(14, 20, day=saturday),
        _ts(15, 0, day=sunday),
        _ts(23, 30, day=sunday),  # Globex otevřený, seance pondělí: plán pondělí ještě není
        _ts(14, 20, day=monday),  # páteční plán se v pondělí neodebírá
    ):
        h.tick(moment)
        assert h.recorder.streamers() == set(), moment
    h.recorder = h.new_recorder()
    h.tick(_ts(14, 20, day=friday))
    assert h.recorder.session == friday
    assert h.recorder.streamers() == {P687, P686, "QQQ"}


def test_pozdni_vzorek_minuty_se_nezapise(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    h = _Harness(tmp_path, _ts(15, 0, 10))
    h.add_plan(_plan())
    h.tick(_ts(15, 0, 10))
    h.feed(_ts(15, 1, 20))
    h.tick(_ts(15, 1, 20))
    # Smyčka stála (uspaný hostitel): vzorek 3 min po závěru minuty 15:01 by byl pohled dopředu
    h.feed(_ts(15, 5, 1))
    h.tick(_ts(15, 5, 1))
    assert h.quotes() == [] and h.bars() == []
    assert any("vzorek 181 s po jejím závěru" in r.getMessage() for r in caplog.records)


def test_retezec_selhal_zkusi_se_znovu(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    h = _Harness(tmp_path, _ts(15, 0, 10))
    h.add_plan(_plan())
    h.symbols.fail = True
    h.tick(_ts(15, 0, 10))
    assert h.recorder.streamers() == set()
    assert any("řetěz z tasty selhal" in r.getMessage() for r in caplog.records)
    h.symbols.fail = False
    h.tick(_ts(15, 0, 10 + PLAN_REFRESH_S))
    assert h.recorder.streamers() == {P687, P686, "QQQ"}


def test_plan_contracts_hlavni_a_stiny_bez_duplicit_platny_plan_z_9_31() -> None:
    shadow = _part(_variant("hlavní", "P", 688.0))
    plan = {**_plan(), "shadows": {"entry_b": PUTS, "wick_test": shadow}}
    assert plan_contracts([plan]) == {
        (EXPIRY, 687.0, "P"),
        (EXPIRY, 686.0, "P"),
        (EXPIRY, 688.0, "P"),
    }
    assert plan_contracts([_plan(main=None)]) == frozenset()
    rebuilt = {"slot": "9:31", "levels_nq": None, "main": CALLS, "shadows": {}}
    repricing: dict[str, Any] = {
        "slot": "9:31",
        "reasons": [],
        "chain": None,
        "prices": None,
        "plan": rebuilt,
    }
    assert plan_contracts([_plan(main=None, repricing=repricing)]) == {
        (EXPIRY, 705.0, "C"),
        (EXPIRY, 706.0, "C"),
    }


def test_quote_row_kotace_bez_smyslu_a_stare_greeks_jsou_mezera() -> None:
    clock = _Clock(_ts(15, 0, 1))
    cache = TastyChainCache(clock=clock)
    feed_quote(cache, P687, 1.42, 1.38)  # ask pod bidem
    feed_greeks(cache, P687, 0.30, -0.25, 0.05)
    row = quote_row(_ts(15, 0), (EXPIRY, 687.0, "P"), cache.state(P687), _ts(15, 1))
    assert (row.bid, row.ask, row.mid) == (None, None, None)
    assert (row.iv, row.delta) == (0.30, -0.25)
    later = _ts(15, 0, 1) + dt.timedelta(seconds=MAX_AGE_S + 1)
    clock.now = later
    feed_quote(cache, P687, 1.38, 1.42)
    row = quote_row(_ts(15, 1), (EXPIRY, 687.0, "P"), cache.state(P687), later)
    assert (row.bid, row.ask) == (1.38, 1.42)
    assert (row.iv, row.delta) == (None, None)  # Greeks starší MAX_AGE_S, bez dopočtu z mid
    assert quote_row(_ts(15, 1), (EXPIRY, 690.0, "P"), None, later).quote_age_s is None


def test_rozpocet_plan_quotes_6_polozek_z_rezervy() -> None:
    assert EVENTS_BY_PURPOSE[PURPOSE] == ("Quote", "Greeks")
    budget = plan_subscriptions({PURPOSE: ["QQQ", P687, P686]})
    assert budget.entries == 6 and budget.over_hard_cap == 0
    # Odběr čerpá rezervu ad-hoc, ne pokrytí extended: to má i s ním strop − rezerva (24 000)
    regular = [f"X{i}" for i in range(10_000)]
    alone = plan_subscriptions({"extended": regular}, max_entries=26_000)
    full = plan_subscriptions(
        {PURPOSE: ["QQQ", P687, P686], "extended": regular}, max_entries=26_000
    )
    assert alone.entries == 26_000 - ADHOC_RESERVE_ENTRIES
    assert full.entries == alone.entries + 6
    assert full.trimmed == alone.trimmed == {"extended": 2_000}
    assert {"QQQ", P687, P686} <= set(full.subscriptions)
