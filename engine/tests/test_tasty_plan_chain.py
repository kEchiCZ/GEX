"""Snímky řetězu QQQ 0DTE + 1DTE pro plán opce (#1740, #1768, ADR-0062 K4 C) nad mockem tasty."""

import datetime as dt
import logging
import threading
from pathlib import Path
from typing import cast

import pyarrow.parquet as pq
import pytest

from gexlens_engine.compute.spot import SpotReading
from gexlens_engine.config import Settings
from gexlens_engine.storage.parquet_store import OiMissingRow, SnapshotRow, SnapshotWriter
from gexlens_engine.tasty.adhoc import ADHOC_MAX_STRIKES_PER_SIDE
from gexlens_engine.tasty.budget import ADHOC_RESERVE_ENTRIES, EVENTS_BY_PURPOSE, plan_subscriptions
from gexlens_engine.tasty.mock import feed_greeks, feed_quote, feed_summary
from gexlens_engine.tasty.plan_chain import (
    BAND_PCT,
    MAX_AGE_S,
    PLAN_SLOT,
    PURPOSE,
    QUOTE_TIMEOUT_S,
    REPRICE_SLOT,
    PlanChainSnapshotter,
    SnapshotSlot,
    _AfterOpen,
    _PreOpen,
    band_streamers,
    missed_slots,
    missing_data_level,
    next_snapshot,
    pick_expiries,
    snapshot_boundary,
)
from gexlens_engine.tasty.provider import TastyChainCache
from gexlens_engine.tasty.symbols import ChainSymbols, SymbolMap

UTC = dt.UTC
#: Út 3. 11. 2026 (SEČ i EST): US open 14:30 UTC, hranice snímků 14:15 a 14:31 UTC
SESSION = dt.date(2026, 11, 3)
#: dxFeed `dayId` seance (dny od 1970-01-01)
SESSION_DAY_ID = (SESSION - dt.date(1970, 1, 1)).days
SPOT = 700.0
NQ = 28_900.0


def _chain(expiries: list[str], low: int = 650, high: int = 750) -> ChainSymbols:
    """Řetěz QQQ se striky po 1 $ (jako 0DTE/1DTE QQQ)."""
    return ChainSymbols(
        product="QQQ",
        day=SESSION,
        by_contract={
            (expiry, float(strike), right): f".QQQ{expiry[2:]}{right}{strike}"
            for expiry in expiries
            for strike in range(low, high + 1)
            for right in ("C", "P")
        },
    )


def _streamer(expiry: str, right: str, strike: int) -> str:
    return f".QQQ{expiry[2:]}{right}{strike}"


def _delta(streamer: str) -> float:
    """Delta kontraktu mocku podle striku: call 0,25 na 710, put −0,25 na 690 (pásmo 679–721)."""
    right, strike = streamer[-4], int(streamer[-3:])
    call = min(0.99, max(0.01, 0.5 - (strike - SPOT) / 40.0))
    return call if right == "C" else call - 1.0


#: ITM kontrakty pásma s |delta| ≥ 0,7 v 0DTE i 1DTE, které 9. 11. v 9:15 chyběly (28 z 86)
ITM = frozenset(
    _streamer(expiry, right, strike)
    for expiry in ("20261103", "20261104")
    for right, strikes in (("C", range(679, 693)), ("P", range(708, 722)))
    for strike in strikes
)


class _Clock:
    def __init__(self, now: dt.datetime) -> None:
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now


class _FakeSymbolMap:
    def __init__(self, chain: ChainSymbols, *, fail: bool = False) -> None:
        self._chain = chain
        self._fail = fail

    async def chain(self, product: str, day: dt.date) -> ChainSymbols:
        if self._fail:
            raise RuntimeError("REST 502")
        assert product == "QQQ"
        return self._chain

    async def front_future(self, product: str) -> str | None:
        return "QQQ"  # akcie/ETF: podklad je symbol sám (#206)


class _Harness:
    """Snímek nad mockem tasty: `on_change` = reconciler + DXLink, které doručí kotace."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        start: dt.datetime,
        deliver_options: bool = True,
        deliver_underlying: bool = True,
        deliver_summary: bool = True,
        nq: SpotReading | None = None,
        fail_chain: bool = False,
        chain: ChainSymbols | None = None,
        tick_underlying: bool = False,
        tick_quotes: bool = False,
        tick_greeks: bool = False,
        summary_day_id: int | None = None,
        silent: frozenset[str] = frozenset(),
    ) -> None:
        self.clock = _Clock(start)
        self.cache = TastyChainCache(clock=self.clock)
        self.tmp_path = tmp_path
        self.deliver_options = deliver_options
        self.deliver_underlying = deliver_underlying
        self.deliver_summary = deliver_summary
        self.history: list[set[str]] = []
        #: Okamžik každého probuzení reconcileru (kdy se pásmo odebralo)
        self.woken_at: list[dt.datetime] = []
        #: Ranní seance (#1768): po každém čekání přijde aktualizace odebraných symbolů
        self.tick_underlying = tick_underlying
        self.tick_quotes = tick_quotes
        self.tick_greeks = tick_greeks
        self.summary_day_id = summary_day_id
        #: Kontrakty jen s eventem z odběru, bez aktualizace (9. 11.: ITM v 9:15)
        self.silent = silent
        self.ticks = 0
        self.chain = chain or _chain(["20261103", "20261104", "20261105"])
        reading = nq if nq is not None else SpotReading(NQ, start, "ibkr", True)
        self.snapshotter = PlanChainSnapshotter(
            symbol_map=cast(SymbolMap, _FakeSymbolMap(self.chain, fail=fail_chain)),
            cache=self.cache,
            writer=SnapshotWriter(Settings(data_dir=tmp_path)),
            reference_spot=lambda: reading,
            on_change=self._on_change,
            clock=self.clock,
            sleep=self._sleep,
        )

    def _on_change(self) -> None:
        current = self.snapshotter.streamers()
        self.history.append(current)
        self.woken_at.append(self.clock.now)
        for streamer in current:
            if streamer == "QQQ":
                if self.deliver_underlying:
                    feed_quote(self.cache, "QQQ", SPOT - 0.01, SPOT + 0.01)
            elif self.deliver_options:
                feed_quote(self.cache, streamer, 1.00, 1.10)
                feed_greeks(self.cache, streamer, iv=0.30, delta=_delta(streamer), gamma=0.05)
                if self.deliver_summary and not streamer.endswith("C700"):
                    feed_summary(self.cache, streamer, 120.0, day_id=self.summary_day_id)

    async def _sleep(self, seconds: float) -> None:
        self.clock.now += dt.timedelta(seconds=seconds)
        self.ticks += 1
        step = 0.01 * self.ticks
        for streamer in self.snapshotter.streamers():
            if streamer == "QQQ":
                if self.tick_underlying:
                    # Mění se velikosti, mid zůstává: pásmo z náběhu = pásmo kolem SPOT
                    size = 1.0 + self.ticks
                    feed_quote(self.cache, "QQQ", SPOT - 0.01, SPOT + 0.01, size, size)
            elif streamer not in self.silent:
                if self.tick_quotes:
                    feed_quote(self.cache, streamer, 1.00 + step, 1.10 + step)
                if self.tick_greeks:
                    feed_greeks(
                        self.cache, streamer, iv=0.30 + step, delta=_delta(streamer), gamma=0.05
                    )

    def partition(self, expiry: str) -> Path:
        return self.tmp_path / "snapshots" / "QQQ" / expiry / f"{SESSION.isoformat()}.parquet"


def _start(slot: SnapshotSlot = REPRICE_SLOT) -> dt.datetime:
    """Náběh snímku seance: 9:30:30 ET (9:31), nebo 9:13:30 ET (9:15)."""
    return snapshot_boundary(SESSION, slot) - dt.timedelta(seconds=slot.lead_s)


def _morning(
    tmp_path: Path,
    *,
    tick_quotes: bool = True,
    tick_greeks: bool = True,
    summary_day_id: int | None = SESSION_DAY_ID,
    silent: frozenset[str] = frozenset(),
) -> _Harness:
    """Ranní seance opcí (#1768): podklad i opce se aktualizují, Summary nese dayId seance."""
    return _Harness(
        tmp_path,
        start=_start(PLAN_SLOT),
        tick_underlying=True,
        tick_quotes=tick_quotes,
        tick_greeks=tick_greeks,
        summary_day_id=summary_day_id,
        silent=silent,
    )


# --- výběr expirací -----------------------------------------------------------


def test_1dte_pondeli_utery() -> None:
    chain = _chain(["20261102", "20261103", "20261104"])
    now = dt.datetime(2026, 11, 2, 14, 31, tzinfo=UTC)
    assert pick_expiries(chain, dt.date(2026, 11, 2), now) == ("20261102", "20261103")


def test_1dte_patek_pondeli() -> None:
    chain = _chain(["20261106", "20261109", "20261110"])
    now = dt.datetime(2026, 11, 6, 14, 31, tzinfo=UTC)
    assert pick_expiries(chain, dt.date(2026, 11, 6), now) == ("20261106", "20261109")


def test_1dte_pred_svatkem_nyse_dalsi_seance() -> None:
    """St 25. 11. 2026 → pá 27. 11. (Thanksgiving 26. 11.), i když řetěz 26. 11. nese."""
    chain = _chain(["20261125", "20261126", "20261127"])
    now = dt.datetime(2026, 11, 25, 14, 31, tzinfo=UTC)
    assert pick_expiries(chain, dt.date(2026, 11, 25), now) == ("20261125", "20261127")


def test_0dte_po_16_et_se_nevybere() -> None:
    chain = _chain(["20261102", "20261103"])
    after_close = dt.datetime(2026, 11, 2, 21, 5, tzinfo=UTC)  # 16:05 ET
    assert pick_expiries(chain, dt.date(2026, 11, 2), after_close) == (None, "20261103")
    before_close = dt.datetime(2026, 11, 2, 20, 55, tzinfo=UTC)  # 15:55 ET
    assert pick_expiries(chain, dt.date(2026, 11, 2), before_close) == ("20261102", "20261103")


def test_chybejici_expirace_je_none() -> None:
    chain = _chain(["20261104"])
    now = dt.datetime(2026, 11, 3, 14, 31, tzinfo=UTC)
    assert pick_expiries(chain, SESSION, now) == (None, "20261104")
    assert pick_expiries(_chain(["20261103"]), SESSION, now) == ("20261103", None)


def test_pasmo_3_procenta_kolem_spotu() -> None:
    band = band_streamers(_chain(["20261103", "20261104"]), ["20261103", "20261104"], SPOT)
    # ±3 % ze 700 = ±21 → striky 679–721 = 43 striků × C/P
    assert BAND_PCT == 3.0
    assert {expiry: len(streamers) for expiry, streamers in band.items()} == {
        "20261103": 86,
        "20261104": 86,
    }


# --- plán spuštění ------------------------------------------------------------


def test_snimky_v_9_15_a_9_31_et_v_zime_i_v_lete() -> None:
    assert snapshot_boundary(SESSION, PLAN_SLOT) == dt.datetime(2026, 11, 3, 14, 15, tzinfo=UTC)
    assert snapshot_boundary(SESSION, REPRICE_SLOT) == dt.datetime(2026, 11, 3, 14, 31, tzinfo=UTC)
    summer = dt.date(2026, 7, 1)
    assert snapshot_boundary(summer, PLAN_SLOT) == dt.datetime(2026, 7, 1, 13, 15, tzinfo=UTC)
    assert snapshot_boundary(summer, REPRICE_SLOT) == dt.datetime(2026, 7, 1, 13, 31, tzinfo=UTC)
    # Náběh 9:13:30 ET = 90 s na aktualizace = limit stáří kotace (Rozhodnuto 5. 11., Q1 A)
    assert PLAN_SLOT.lead_s == MAX_AGE_S == 90.0
    assert _start(PLAN_SLOT) == dt.datetime(2026, 11, 3, 14, 13, 30, tzinfo=UTC)
    assert _start() == dt.datetime(2026, 11, 3, 14, 30, 30, tzinfo=UTC)


def test_poradi_snimku_v_seanci() -> None:
    morning = dt.datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
    assert next_snapshot(morning) == (SESSION, PLAN_SLOT, _start(PLAN_SLOT))
    after_plan = dt.datetime(2026, 11, 3, 14, 15, 41, tzinfo=UTC)  # po snímku 9:15
    assert next_snapshot(after_plan) == (SESSION, REPRICE_SLOT, _start())
    assert next_snapshot(dt.datetime(2026, 11, 3, 14, 31, tzinfo=UTC)) == (
        dt.date(2026, 11, 4),
        PLAN_SLOT,
        dt.datetime(2026, 11, 4, 14, 13, 30, tzinfo=UTC),
    )


def test_vikend_a_svatek_snimek_nemaji() -> None:
    saturday = dt.datetime(2026, 11, 7, 14, 31, tzinfo=UTC)
    assert next_snapshot(saturday)[:2] == (dt.date(2026, 11, 9), PLAN_SLOT)
    friday_after = dt.datetime(2026, 11, 6, 14, 32, tzinfo=UTC)
    assert next_snapshot(friday_after)[:2] == (dt.date(2026, 11, 9), PLAN_SLOT)
    before_thanksgiving = dt.datetime(2026, 11, 25, 15, 0, tzinfo=UTC)
    assert next_snapshot(before_thanksgiving)[:2] == (dt.date(2026, 11, 27), PLAN_SLOT)


def test_pondeli_proti_patku() -> None:
    """Po pátečním snímku 9:31 je další snímek pondělní 9:15, ne sobota ani neděle."""
    friday_morning = dt.datetime(2026, 11, 6, 14, 20, tzinfo=UTC)
    assert next_snapshot(friday_morning) == (
        dt.date(2026, 11, 6),
        REPRICE_SLOT,
        dt.datetime(2026, 11, 6, 14, 30, 30, tzinfo=UTC),
    )
    assert next_snapshot(dt.datetime(2026, 11, 6, 14, 31, tzinfo=UTC)) == (
        dt.date(2026, 11, 9),
        PLAN_SLOT,
        dt.datetime(2026, 11, 9, 14, 13, 30, tzinfo=UTC),
    )


def test_nedelni_otevreni_snimek_az_v_pondeli() -> None:
    """Globex otevírá v neděli 17:00 CT, snímek patří až pondělnímu ránu."""
    sunday_open = dt.datetime(2026, 11, 8, 23, 5, tzinfo=UTC)
    assert next_snapshot(sunday_open) == (
        dt.date(2026, 11, 9),
        PLAN_SLOT,
        dt.datetime(2026, 11, 9, 14, 13, 30, tzinfo=UTC),
    )


def test_start_v_nabehu_bezi_hned() -> None:
    in_lead = dt.datetime(2026, 11, 3, 14, 14, 10, tzinfo=UTC)
    assert next_snapshot(in_lead) == (SESSION, PLAN_SLOT, _start(PLAN_SLOT))
    in_lead_31 = dt.datetime(2026, 11, 3, 14, 30, 45, tzinfo=UTC)
    assert next_snapshot(in_lead_31) == (SESSION, REPRICE_SLOT, _start())


def test_start_mezi_9_15_a_9_31_jen_snimek_9_31() -> None:
    start = dt.datetime(2026, 11, 3, 14, 20, tzinfo=UTC)
    assert missed_slots(start) == (SESSION, (PLAN_SLOT,))
    assert next_snapshot(start)[:2] == (SESSION, REPRICE_SLOT)


def test_start_po_9_31_nic_do_dalsi_seance() -> None:
    start = dt.datetime(2026, 11, 3, 14, 32, tzinfo=UTC)
    assert missed_slots(start) == (SESSION, (PLAN_SLOT, REPRICE_SLOT))
    assert next_snapshot(start)[:2] == (dt.date(2026, 11, 4), PLAN_SLOT)
    assert missed_slots(dt.datetime(2026, 11, 3, 23, 30, tzinfo=UTC)) == (
        SESSION,
        (PLAN_SLOT, REPRICE_SLOT),
    )
    assert missed_slots(dt.datetime(2026, 11, 3, 14, 14, 10, tzinfo=UTC)) is None  # v náběhu
    assert missed_slots(dt.datetime(2026, 11, 7, 15, 0, tzinfo=UTC)) is None  # sobota
    assert missed_slots(dt.datetime(2026, 11, 26, 15, 0, tzinfo=UTC)) is None  # Thanksgiving
    # Úterní noc po půlnoci ET je už středa, jejíž hranice ještě nepřišly
    assert missed_slots(dt.datetime(2026, 11, 4, 6, 0, tzinfo=UTC)) is None


@pytest.mark.parametrize(
    ("now", "level"),
    [
        (dt.datetime(2026, 11, 7, 15, 0, tzinfo=UTC), logging.INFO),  # sobota
        (dt.datetime(2026, 11, 3, 22, 30, tzinfo=UTC), logging.INFO),  # denní pauza 16:30 CT
        (dt.datetime(2026, 11, 8, 22, 55, tzinfo=UTC), logging.INFO),  # neděle 16:55 CT
        (dt.datetime(2026, 11, 8, 23, 5, tzinfo=UTC), logging.WARNING),  # neděle 17:05 CT
        (dt.datetime(2026, 11, 3, 14, 31, tzinfo=UTC), logging.WARNING),  # RTH
    ],
)
def test_zavreny_trh_bez_upozorneni(now: dt.datetime, level: int) -> None:
    assert missing_data_level(now) == level


# --- snímek nad mockem tasty --------------------------------------------------


async def test_snimek_zapise_obe_expirace_a_uvolni_subskripci(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, start=_start())
    snapshot = await harness.snapshotter.run(SESSION, REPRICE_SLOT)
    assert snapshot is not None
    assert snapshot.complete, snapshot.reasons
    assert snapshot.zero_dte is not None and snapshot.one_dte is not None
    assert (snapshot.zero_dte.expiry, snapshot.one_dte.expiry) == ("20261103", "20261104")
    # Ceny na hranici 9:31:00 ET = stav na konci minuty 9:30 (ADR-0062, Odvozeno)
    assert snapshot.spot_ts == snapshot_boundary(SESSION, REPRICE_SLOT)
    assert snapshot.spot == pytest.approx(SPOT)
    assert snapshot.spot_nq == NQ and snapshot.nq_source == "ibkr"
    assert snapshot.ratio == pytest.approx(NQ / SPOT)
    for snap in (snapshot.zero_dte, snapshot.one_dte):
        assert (snap.contracts, snap.quoted, len(snap.rows)) == (86, 86, 86)
        assert snap.with_oi == 85
        assert snap.oi_missing == 1  # C700 bez Summary → oimissing, ne změřená nula
        assert {row.ts_min for row in snap.rows} == {snapshot_boundary(SESSION, REPRICE_SLOT)}
        table = pq.read_table(harness.partition(snap.expiry))
        assert table.num_rows == 86
        assert set(table.column("ts_min").to_pylist()) == {snapshot_boundary(SESSION, REPRICE_SLOT)}
    assert not harness.partition("20261105").exists()  # 2DTE se neodebírá
    # Průběh subskripce: podklad → podklad + pásmo → nic
    assert harness.history[0] == {"QQQ"}
    assert len(harness.history[1]) == 1 + 2 * 86
    assert harness.history[-1] == set()
    assert harness.snapshotter.streamers() == set()
    assert "úplný" in snapshot.describe()


async def test_bez_kotaci_do_limitu_neuplny_s_duvodem(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, start=_start(), deliver_options=False)
    snapshot = await harness.snapshotter.run(SESSION, REPRICE_SLOT)
    assert snapshot is not None and not snapshot.complete
    assert "0DTE 20261103: žádný platný řádek" in snapshot.reasons
    assert "1DTE 20261104: žádný platný řádek" in snapshot.reasons
    assert not any("kotace 0/86" in reason for reason in snapshot.reasons)  # jen log (K4 bod 7)
    assert "kotace 0/86" in snapshot.describe()
    assert snapshot.zero_dte is not None
    assert [why for why, _ in snapshot.zero_dte.missing] == ["bez kotace"]
    assert snapshot.ts is not None
    limit = snapshot_boundary(SESSION, REPRICE_SLOT) + dt.timedelta(seconds=QUOTE_TIMEOUT_S)
    assert limit <= snapshot.ts <= limit + dt.timedelta(seconds=1)
    assert not harness.partition("20261103").exists()
    assert harness.history[-1] == set() and harness.snapshotter.streamers() == set()
    assert "NEÚPLNÝ" in snapshot.describe()


async def test_kotace_pred_openem_se_nepocitaji(tmp_path: Path) -> None:
    """Point-in-time (K4): kotace z 9:29:50 ET je zamrzlá z close, do snímku nepatří."""
    harness = _Harness(tmp_path, start=_start(), deliver_options=False)
    harness.clock.now = dt.datetime(2026, 11, 3, 14, 29, 50, tzinfo=UTC)
    for streamer in harness.chain.by_contract.values():
        feed_quote(harness.cache, streamer, 1.00, 1.10)
        feed_greeks(harness.cache, streamer, iv=0.30, delta=0.5, gamma=0.05)
    harness.clock.now = _start()
    snapshot = await harness.snapshotter.run(SESSION, REPRICE_SLOT)
    assert snapshot is not None and not snapshot.complete
    assert snapshot.zero_dte is not None
    assert (snapshot.zero_dte.quoted, len(snapshot.zero_dte.rows)) == (0, 0)


async def test_oi_z_drivejsi_seance_je_chybejici(tmp_path: Path) -> None:
    """Cache stavy nemaže: OI ze Summary před openem (dnešní 0DTE = včerejší 1DTE) je mezera."""
    harness = _Harness(tmp_path, start=_start(), deliver_summary=False)
    harness.clock.now = dt.datetime(2026, 11, 2, 14, 31, 10, tzinfo=UTC)  # včerejší snímek
    for streamer in harness.chain.by_contract.values():
        feed_summary(harness.cache, streamer, 999.0)
    harness.clock.now = _start()
    snapshot = await harness.snapshotter.run(SESSION, REPRICE_SLOT)
    assert snapshot is not None and not snapshot.complete
    assert snapshot.zero_dte is not None
    assert (snapshot.zero_dte.quoted, snapshot.zero_dte.with_oi) == (86, 0)
    assert snapshot.zero_dte.oi_missing == 86
    assert {row.oi for row in snapshot.zero_dte.rows} == {0.0}
    assert any("OI po openu 0/86" in reason for reason in snapshot.reasons)
    assert pq.read_table(harness.partition("20261103")).num_rows == 86


async def test_bez_kotace_qqq_pasmo_neodebira(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, start=_start(), deliver_underlying=False)
    snapshot = await harness.snapshotter.run(SESSION, REPRICE_SLOT)
    assert snapshot is not None and not snapshot.complete
    assert any("kotace QQQ nedorazila" in reason for reason in snapshot.reasons)
    assert max(len(symbols) for symbols in harness.history) == 1
    assert harness.snapshotter.streamers() == set()


async def test_bez_spotu_nq_radky_ano_pomer_ne(tmp_path: Path) -> None:
    stale = SpotReading(NQ, _start() - dt.timedelta(minutes=5), "ibkr", False)
    harness = _Harness(tmp_path, start=_start(), nq=stale)
    snapshot = await harness.snapshotter.run(SESSION, REPRICE_SLOT)
    assert snapshot is not None and not snapshot.complete
    assert snapshot.ratio is None and snapshot.spot_nq is None
    assert any(reason.startswith("spot NQ chybí") for reason in snapshot.reasons)
    assert harness.partition("20261104").exists()


async def test_chyba_retezu_nic_neodebira(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, start=_start(), fail_chain=True)
    snapshot = await harness.snapshotter.run(SESSION, REPRICE_SLOT)
    assert snapshot is not None and not snapshot.complete
    assert "selhal" in snapshot.reasons[0]
    assert harness.history == []


async def test_zmeskana_hranice_nic_neodebira(tmp_path: Path) -> None:
    late = snapshot_boundary(SESSION, REPRICE_SLOT) + dt.timedelta(seconds=5)
    harness = _Harness(tmp_path, start=late)
    snapshot = await harness.snapshotter.run(SESSION, REPRICE_SLOT)
    assert snapshot is not None and not snapshot.complete
    assert "zmeškána" in snapshot.reasons[0]
    assert harness.history == []


@pytest.mark.parametrize("session", [dt.date(2026, 11, 7), dt.date(2026, 11, 26)])
async def test_vikend_a_svatek_se_nespousti(tmp_path: Path, session: dt.date) -> None:
    harness = _Harness(tmp_path, start=_start())
    with pytest.raises(ValueError, match="obchodní seanci"):
        await harness.snapshotter.run(session, REPRICE_SLOT)
    with pytest.raises(ValueError, match="obchodní seanci"):
        await harness.snapshotter.run(session, PLAN_SLOT)
    assert harness.history == []


async def test_zavreny_trh_none_bez_upozorneni(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    pause = dt.datetime(2026, 11, 3, 22, 30, tzinfo=UTC)  # denní pauza 16:30 CT
    harness = _Harness(tmp_path, start=pause)
    with caplog.at_level(logging.INFO):
        assert await harness.snapshotter.run(SESSION, REPRICE_SLOT) is None
    assert harness.history == []
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


# --- ranní snímek 9:15 ET (#1768) ---------------------------------------------


async def test_ranni_snimek_9_15_pasmo_v_nabehu(tmp_path: Path) -> None:
    harness = _morning(tmp_path)
    snapshot = await harness.snapshotter.run(SESSION, PLAN_SLOT)
    assert snapshot is not None
    assert snapshot.complete, snapshot.reasons
    assert snapshot.slot is PLAN_SLOT
    boundary = snapshot_boundary(SESSION, PLAN_SLOT)
    assert snapshot.spot_ts == boundary
    assert snapshot.spot == pytest.approx(SPOT)
    assert snapshot.ratio == pytest.approx(NQ / SPOT)
    assert snapshot.zero_dte is not None and snapshot.one_dte is not None
    for snap in (snapshot.zero_dte, snapshot.one_dte):
        assert (snap.contracts, snap.quoted, len(snap.rows)) == (86, 86, 86)
        assert (snap.with_oi, snap.oi_missing) == (85, 1)  # C700 bez Summary
        assert {row.ts_min for row in snap.rows} == {boundary}
        # Greeks z aktualizace, ne z eventu odběru (iv 0,30)
        assert all(row.iv != pytest.approx(0.30) for row in snap.rows)
        table = pq.read_table(harness.partition(snap.expiry))
        assert set(table.column("ts_min").to_pylist()) == {boundary}
    # Pásmo se odebralo v náběhu, aby kotace stihly aktualizaci před hranicí
    assert len(harness.history[1]) == 1 + 2 * 86
    assert harness.woken_at[1] < boundary - dt.timedelta(seconds=60)
    assert harness.history[-1] == set() and harness.snapshotter.streamers() == set()
    assert "9:15 ET" in snapshot.describe() and "úplný" in snapshot.describe()


async def test_kotace_z_close_pri_odberu_se_nepocita(tmp_path: Path) -> None:
    """DXLink při odběru pošle stav z close a dál mlčí (23. 9., 1. 10.): v 9:15 nic neplatí."""
    harness = _morning(tmp_path, tick_quotes=False, tick_greeks=False)
    snapshot = await harness.snapshotter.run(SESSION, PLAN_SLOT)
    assert snapshot is not None and not snapshot.complete
    assert snapshot.zero_dte is not None
    assert (snapshot.zero_dte.quoted, len(snapshot.zero_dte.rows)) == (0, 0)
    assert "0DTE 20261103: žádný platný řádek" in snapshot.reasons
    assert "1DTE 20261104: žádný platný řádek" in snapshot.reasons
    # E: kontrakty mají jen event z odběru (stav z close), aktualizace nepřišla
    assert [why for why, _ in snapshot.zero_dte.missing] == ["jen event z odběru"]
    assert "bez řádku: jen event z odběru 679C 679P 680C" in snapshot.describe()
    limit = snapshot_boundary(SESSION, PLAN_SLOT) + dt.timedelta(seconds=QUOTE_TIMEOUT_S)
    assert snapshot.ts is not None and limit <= snapshot.ts <= limit + dt.timedelta(seconds=1)
    assert not harness.partition("20261103").exists()
    assert harness.history[-1] == set() and harness.snapshotter.streamers() == set()


async def test_ranni_bez_itm_je_uplny(tmp_path: Path) -> None:
    """9. 11.: v 9:15 chyběla jen ITM s |delta| 0,69–0,95, plán je nečte (K4 bod 7, B)."""
    harness = _morning(tmp_path, silent=ITM)
    snapshot = await harness.snapshotter.run(SESSION, PLAN_SLOT)
    assert snapshot is not None
    assert snapshot.complete, snapshot.reasons
    # Úplný hned na hranici, nečeká do limitu (kotace by stárly nad 90 s)
    assert snapshot.ts == snapshot_boundary(SESSION, PLAN_SLOT)
    assert snapshot.zero_dte is not None and snapshot.one_dte is not None
    for snap in (snapshot.zero_dte, snapshot.one_dte):
        assert (snap.contracts, snap.quoted, len(snap.rows)) == (86, 58, 58)
        assert snap.quoted_share < 0.9  # podíl pásma jen do logu
        expected = tuple(f"{k}C" for k in range(679, 693)) + tuple(f"{k}P" for k in range(708, 722))
        assert snap.missing == (("jen event z odběru", expected),)
        assert pq.read_table(harness.partition(snap.expiry)).num_rows == 58
    assert "kotace 58/86" in snapshot.describe()
    assert "(bez řádku: jen event z odběru 679C 680C" in snapshot.describe()


async def test_ranni_bez_souseda_delta_025_neuplny(tmp_path: Path) -> None:
    """Chybí-li soused kontraktu |delta| 0,25, výběr varianty by mohl tiše uhnout."""
    harness = _morning(tmp_path, silent=frozenset({_streamer("20261104", "C", 711)}))
    snapshot = await harness.snapshotter.run(SESSION, PLAN_SLOT)
    assert snapshot is not None and not snapshot.complete
    assert snapshot.reasons == (
        "1DTE 20261104: call |delta| 0,25 = 710: soused 711 bez kotace s IV a delta"
        " do 40 s od 9:15 ET",
    )
    assert snapshot.one_dte is not None
    assert snapshot.one_dte.missing == (("jen event z odběru", ("711C",)),)
    limit = snapshot_boundary(SESSION, PLAN_SLOT) + dt.timedelta(seconds=QUOTE_TIMEOUT_S)
    assert snapshot.ts is not None and limit <= snapshot.ts <= limit + dt.timedelta(seconds=1)
    assert pq.read_table(harness.partition("20261104")).num_rows == 85  # naměřené se zapíše


async def test_ranni_bez_atm_callu_neuplny(tmp_path: Path) -> None:
    """Bez callu na strike nejbližším spotu by EM vzniklo z jiného straddlu."""
    harness = _morning(tmp_path, silent=frozenset({_streamer("20261103", "C", 700)}))
    snapshot = await harness.snapshotter.run(SESSION, PLAN_SLOT)
    assert snapshot is not None and not snapshot.complete
    assert snapshot.reasons == (
        "0DTE 20261103: ATM straddle 700 bez zaplacené kotace call do 40 s od 9:15 ET",
    )


async def test_radky_obou_expiraci_z_jednoho_okamziku(tmp_path: Path) -> None:
    """9. 11. 9:31: řádky 1DTE vznikly až po zápisu 0DTE a nesly `stale_age` −0,04 s."""
    harness = _Harness(tmp_path, start=_start())
    writer = harness.snapshotter.writer
    written: list[str] = []

    class _SlowWriter:
        """Zápis trvá a DXLink mezitím doručí novější kotace."""

        def write_minute(
            self, product: str, expiry: str, day: dt.date, rows: list[SnapshotRow]
        ) -> None:
            written.append(expiry)
            harness.clock.now += dt.timedelta(seconds=0.5)
            for streamer in harness.snapshotter.streamers() - {"QQQ"}:
                feed_quote(harness.cache, streamer, 2.00, 2.10)
            writer.write_minute(product, expiry, day, rows)

        def write_oi_missing(
            self, product: str, expiry: str, day: dt.date, rows: list[OiMissingRow]
        ) -> None:
            writer.write_oi_missing(product, expiry, day, rows)

    harness.snapshotter.writer = cast(SnapshotWriter, _SlowWriter())
    snapshot = await harness.snapshotter.run(SESSION, REPRICE_SLOT)
    assert snapshot is not None and snapshot.complete, snapshot and snapshot.reasons
    assert written == ["20261103", "20261104"]
    for snap in (snapshot.zero_dte, snapshot.one_dte):
        assert snap is not None
        assert all(row.stale_age >= 0 for row in snap.rows)
        assert {row.bid for row in snap.rows} == {1.00}


def test_duvod_chybejici_kotace_9_15() -> None:
    """E: bez eventu po odběru × jen event z odběru × starší 90 s (#1768)."""
    clock = _Clock(dt.datetime(2026, 11, 3, 14, 13, 30, tzinfo=UTC))
    cache = TastyChainCache(clock=clock)
    feed_quote(cache, ".OLD", 1.00, 1.10)  # stav z close před odběrem
    rules = _PreOpen(cache, SESSION)
    rules.mark([".NONE", ".ONE", ".OLD"])
    feed_quote(cache, ".ONE", 1.00, 1.10)  # event z odběru
    feed_quote(cache, ".OLD", 1.00, 1.10, bid_size=2.0)  # event z odběru
    feed_quote(cache, ".OLD", 1.01, 1.11)  # aktualizace v 9:13:30, pak nic
    now = snapshot_boundary(SESSION, PLAN_SLOT) + dt.timedelta(seconds=QUOTE_TIMEOUT_S)
    assert not any(rules.quote(s, now) for s in (".NONE", ".ONE", ".OLD"))
    assert rules.why(".NONE", now) == "bez eventu po odběru"
    assert rules.why(".ONE", now) == "jen event z odběru"
    assert rules.why(".OLD", now) == f"starší {MAX_AGE_S:.0f} s"


def test_duvod_chybejici_kotace_9_31() -> None:
    open_ts = dt.datetime(2026, 11, 3, 14, 30, tzinfo=UTC)
    clock = _Clock(open_ts - dt.timedelta(seconds=10))
    cache = TastyChainCache(clock=clock)
    rules = _AfterOpen(cache, open_ts)
    feed_quote(cache, ".PRE", 1.00, 1.10)
    clock.now = open_ts + dt.timedelta(seconds=1)
    feed_quote(cache, ".OLD", 1.00, 1.10)
    now = open_ts + dt.timedelta(seconds=MAX_AGE_S + 5)
    assert not any(rules.quote(s, now) for s in (".NONE", ".PRE", ".OLD"))
    assert rules.why(".NONE", now) == "bez kotace"
    assert rules.why(".PRE", now) == "kotace před openem"
    assert rules.why(".OLD", now) == f"starší {MAX_AGE_S:.0f} s"


async def test_greeks_bez_aktualizace_nahradi_iv_z_mid(tmp_path: Path) -> None:
    """Kotace živá, greeks jen z odběru (stav z close): řádek nese IV z mid (#547)."""
    harness = _morning(tmp_path, tick_greeks=False)
    snapshot = await harness.snapshotter.run(SESSION, PLAN_SLOT)
    assert snapshot is not None and snapshot.zero_dte is not None
    assert snapshot.zero_dte.quoted == 86
    rows = snapshot.zero_dte.rows
    assert rows, snapshot.reasons
    assert all(row.iv != pytest.approx(0.30) for row in rows)


async def test_cache_predchozi_seance_se_nepocita(tmp_path: Path) -> None:
    """Stav ze včerejška (kotace, greeks, Summary) a jeho stejné opakování při odběru neplatí."""
    harness = _morning(
        tmp_path, tick_quotes=False, tick_greeks=False, summary_day_id=SESSION_DAY_ID - 1
    )
    harness.clock.now = dt.datetime(2026, 11, 2, 20, 0, tzinfo=UTC)  # 2. 11. 15:00 ET
    for streamer in harness.chain.by_contract.values():
        for step in (0.02, 0.01, 0.0):  # kotace se včera měnila, poslední 1,00/1,10
            feed_quote(harness.cache, streamer, 1.00 + step, 1.10 + step)
            feed_greeks(harness.cache, streamer, iv=0.30 + step, delta=0.5, gamma=0.05)
        feed_summary(harness.cache, streamer, 999.0, day_id=SESSION_DAY_ID - 1)
    harness.clock.now = _start(PLAN_SLOT)
    snapshot = await harness.snapshotter.run(SESSION, PLAN_SLOT)
    assert snapshot is not None and not snapshot.complete
    assert snapshot.zero_dte is not None
    assert (snapshot.zero_dte.quoted, snapshot.zero_dte.with_oi) == (0, 0)
    assert snapshot.zero_dte.rows == ()


async def test_oi_jen_ze_summary_dne_seance(tmp_path: Path) -> None:
    harness = _morning(tmp_path, summary_day_id=SESSION_DAY_ID - 1)
    snapshot = await harness.snapshotter.run(SESSION, PLAN_SLOT)
    assert snapshot is not None and not snapshot.complete
    assert snapshot.zero_dte is not None
    assert (snapshot.zero_dte.quoted, snapshot.zero_dte.with_oi) == (86, 0)
    assert snapshot.zero_dte.oi_missing == 86
    assert {row.oi for row in snapshot.zero_dte.rows} == {0.0}
    assert any("OI dne seance 0/86" in reason for reason in snapshot.reasons)
    assert pq.read_table(harness.partition("20261103")).num_rows == 86


@pytest.mark.parametrize(("summary_day", "with_oi"), [(dt.date(2026, 11, 6), 0), (None, 85)])
async def test_pondeli_oi_z_patku_neplati(
    tmp_path: Path, summary_day: dt.date | None, with_oi: int
) -> None:
    """Pondělní snímek: Summary s dayId pátku (předchozí seance) OI nedá, s pondělním ano."""
    monday = dt.date(2026, 11, 9)
    day = summary_day or monday
    harness = _Harness(
        tmp_path,
        start=snapshot_boundary(monday, PLAN_SLOT) - dt.timedelta(seconds=PLAN_SLOT.lead_s),
        chain=_chain(["20261109", "20261110"]),
        tick_underlying=True,
        tick_quotes=True,
        tick_greeks=True,
        summary_day_id=(day - dt.date(1970, 1, 1)).days,
    )
    snapshot = await harness.snapshotter.run(monday, PLAN_SLOT)
    assert snapshot is not None and snapshot.zero_dte is not None
    assert snapshot.zero_dte.expiry == "20261109"
    assert snapshot.one_dte is not None and snapshot.one_dte.expiry == "20261110"
    assert (snapshot.zero_dte.quoted, snapshot.zero_dte.with_oi) == (86, with_oi)


async def test_ranni_bez_aktualizace_qqq_pasmo_neodebira(tmp_path: Path) -> None:
    harness = _Harness(
        tmp_path, start=_start(PLAN_SLOT), tick_quotes=True, summary_day_id=SESSION_DAY_ID
    )
    snapshot = await harness.snapshotter.run(SESSION, PLAN_SLOT)
    assert snapshot is not None and not snapshot.complete
    assert any("kotace QQQ nedorazila" in reason for reason in snapshot.reasons)
    assert max(len(symbols) for symbols in harness.history) == 1
    assert harness.snapshotter.streamers() == set()


async def test_ranni_zmeskana_hranice_nic_neodebira(tmp_path: Path) -> None:
    late = snapshot_boundary(SESSION, PLAN_SLOT) + dt.timedelta(seconds=5)
    harness = _morning(tmp_path)
    harness.clock.now = late
    snapshot = await harness.snapshotter.run(SESSION, PLAN_SLOT)
    assert snapshot is not None and not snapshot.complete
    assert "hranice 9:15 ET zmeškána" in snapshot.reasons[0]
    assert harness.history == []


def test_zmena_kotace_pocita_jen_jinou_hodnotu() -> None:
    """DXLink po reconnectu pošle stav znovu: stejné hodnoty nejsou aktualizace (#1768)."""
    cache = TastyChainCache(clock=lambda: dt.datetime(2026, 11, 3, 14, 14, tzinfo=UTC))
    feed_quote(cache, ".Q", 1.00, 1.10)
    feed_quote(cache, ".Q", 1.00, 1.10)
    feed_greeks(cache, ".Q", iv=0.30, delta=0.5, gamma=0.05)
    feed_greeks(cache, ".Q", iv=0.30, delta=0.5, gamma=0.05)
    state = cache.state(".Q")
    assert state is not None
    assert (state.quote_changes, state.greeks_changes) == (1, 1)
    feed_quote(cache, ".Q", 1.00, 1.10, bid_size=5.0)  # změna velikosti je aktualizace
    feed_greeks(cache, ".Q", iv=0.31, delta=0.5, gamma=0.05)
    assert (state.quote_changes, state.greeks_changes) == (2, 2)


def test_summary_nese_day_id() -> None:
    cache = TastyChainCache()
    feed_summary(cache, ".A", 10.0, day_id=SESSION_DAY_ID)
    feed_summary(cache, ".B", 10.0, day_id=0)  # dxFeed: 0 = nevyplněno
    cache.on_event("Summary", [".C", 10.0, None, None])  # starší tvar bez dayId
    days = [cache.state(s).summary.day_id for s in (".A", ".B", ".C")]  # type: ignore[union-attr]
    assert days == [SESSION_DAY_ID, None, None]


# --- rozpočet DXLink ----------------------------------------------------------


def test_snimek_s_adhoc_pohledem_se_vejde_do_rezervy() -> None:
    """~170 kontraktů × 3 eventy + nejhorší ad-hoc pohled (strop striků) ≤ rezerva ad-hoc."""
    band = band_streamers(_chain(["20261103", "20261104"]), ["20261103", "20261104"], SPOT)
    plan_syms = ["QQQ", *sorted(s for streamers in band.values() for s in streamers)]
    adhoc_syms = ["/CLZ26:XNYM", *(f"./LO{i}" for i in range(ADHOC_MAX_STRIKES_PER_SIDE * 4))]
    events = len(EVENTS_BY_PURPOSE[PURPOSE])
    assert events == 3
    assert (len(plan_syms) + len(adhoc_syms)) * events <= ADHOC_RESERVE_ENTRIES
    # Produkce u stropu běžného plánu: extended se ořezává, rezerva drží místo
    regular = {
        "underlying": [f"/U{i}" for i in range(2)],
        "chain": [f"./C{i}" for i in range(560)],
        "wide": [f"./W{i}" for i in range(898)],
        "extended": [f"./E{i}" for i in range(7_000)],
    }
    adhoc_only = plan_subscriptions({**regular, "adhoc": adhoc_syms})
    with_plan = plan_subscriptions({**regular, "adhoc": adhoc_syms, PURPOSE: plan_syms})
    assert with_plan.over_hard_cap == 0
    assert with_plan.entries <= 25_000
    assert all(symbol in with_plan.subscriptions for symbol in [*plan_syms, *adhoc_syms])
    # Snímek čerpá rezervu, extended kvůli němu nepřijde o nic navíc
    assert set(with_plan.trimmed) == {"extended"}
    assert with_plan.trimmed == adhoc_only.trimmed


# --- zápis do partice sdílené s ad-hoc pohledem ---------------------------------


def test_soubezny_zapis_adhoc_a_snimku_do_tele_partice(tmp_path: Path) -> None:
    """Otevřený ad-hoc pohled QQQ píše tutéž partici 0DTE jako snímek, každý ze svého vlákna.

    Bez zámku partice si zápisy přepisovaly tabulku i sdílený .tmp soubor (týž pid).
    """
    writer = SnapshotWriter(Settings(data_dir=tmp_path))
    errors: list[BaseException] = []

    def write(offset: int) -> None:
        try:
            for minute in range(offset, 80, 2):
                ts = dt.datetime(2026, 11, 3, 14, 0, tzinfo=UTC) + dt.timedelta(minutes=minute)
                row = SnapshotRow(
                    ts_min=ts,
                    strike=700.0,
                    right="C",
                    bid=1.0,
                    ask=1.1,
                    last=1.05,
                    volume=None,
                    iv=0.3,
                    delta=0.5,
                    gamma=0.05,
                    theta=0.0,
                    vega=0.0,
                    oi=120.0,
                    stale_age=1.0,
                )
                writer.write_minute("QQQ", "20261103", SESSION, [row])
        except BaseException as error:  # chyba ve vlákně by jinak zmizela
            errors.append(error)

    threads = [threading.Thread(target=write, args=(offset,)) for offset in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    path = tmp_path / "snapshots" / "QQQ" / "20261103" / f"{SESSION.isoformat()}.parquet"
    assert pq.read_table(path).num_rows == 80
