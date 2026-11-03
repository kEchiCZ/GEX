"""Snímek řetězu QQQ 0DTE + 1DTE pro plán opce (#1740, ADR-0062 K4 A) nad mockem tasty."""

import datetime as dt
import logging
import threading
from pathlib import Path
from typing import cast

import pyarrow.parquet as pq
import pytest

from gexlens_engine.compute.spot import SpotReading
from gexlens_engine.config import Settings
from gexlens_engine.storage.parquet_store import SnapshotRow, SnapshotWriter
from gexlens_engine.tasty.adhoc import ADHOC_MAX_STRIKES_PER_SIDE
from gexlens_engine.tasty.budget import ADHOC_RESERVE_ENTRIES, EVENTS_BY_PURPOSE, plan_subscriptions
from gexlens_engine.tasty.mock import feed_greeks, feed_quote, feed_summary
from gexlens_engine.tasty.plan_chain import (
    BAND_PCT,
    PURPOSE,
    QUOTE_TIMEOUT_S,
    PlanChainSnapshotter,
    band_streamers,
    missed_session,
    missing_data_level,
    next_snapshot,
    pick_expiries,
    snapshot_boundary,
)
from gexlens_engine.tasty.provider import TastyChainCache
from gexlens_engine.tasty.symbols import ChainSymbols, SymbolMap

UTC = dt.UTC
#: Út 3. 11. 2026 (SEČ i EST): US open 14:30 UTC, hranice snímku 14:31 UTC
SESSION = dt.date(2026, 11, 3)
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
    ) -> None:
        self.clock = _Clock(start)
        self.cache = TastyChainCache(clock=self.clock)
        self.tmp_path = tmp_path
        self.deliver_options = deliver_options
        self.deliver_underlying = deliver_underlying
        self.deliver_summary = deliver_summary
        self.history: list[set[str]] = []
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
        for streamer in current:
            if streamer == "QQQ":
                if self.deliver_underlying:
                    feed_quote(self.cache, "QQQ", SPOT - 0.01, SPOT + 0.01)
            elif self.deliver_options:
                feed_quote(self.cache, streamer, 1.00, 1.10)
                feed_greeks(self.cache, streamer, iv=0.30, delta=0.5, gamma=0.05)
                if self.deliver_summary and not streamer.endswith("C700"):
                    feed_summary(self.cache, streamer, 120.0)

    async def _sleep(self, seconds: float) -> None:
        self.clock.now += dt.timedelta(seconds=seconds)

    def partition(self, expiry: str) -> Path:
        return self.tmp_path / "snapshots" / "QQQ" / expiry / f"{SESSION.isoformat()}.parquet"


def _start() -> dt.datetime:
    """Náběh 9:30:30 ET = 30 s před hranicí."""
    return next_snapshot(dt.datetime(2026, 11, 3, 12, 0, tzinfo=UTC))[1]


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


def test_snimek_v_9_31_et_v_zime_i_v_lete() -> None:
    assert snapshot_boundary(SESSION) == dt.datetime(2026, 11, 3, 14, 31, tzinfo=UTC)
    assert snapshot_boundary(dt.date(2026, 7, 1)) == dt.datetime(2026, 7, 1, 13, 31, tzinfo=UTC)
    assert _start() == dt.datetime(2026, 11, 3, 14, 30, 30, tzinfo=UTC)


def test_vikend_a_svatek_snimek_nemaji() -> None:
    saturday = dt.datetime(2026, 11, 7, 14, 31, tzinfo=UTC)
    assert next_snapshot(saturday)[0] == dt.date(2026, 11, 9)
    friday_after = dt.datetime(2026, 11, 6, 14, 32, tzinfo=UTC)
    assert next_snapshot(friday_after)[0] == dt.date(2026, 11, 9)
    before_thanksgiving = dt.datetime(2026, 11, 25, 15, 0, tzinfo=UTC)
    assert next_snapshot(before_thanksgiving)[0] == dt.date(2026, 11, 27)


def test_nedelni_otevreni_snimek_az_v_pondeli() -> None:
    """Globex otevírá v neděli 17:00 CT, snímek patří až pondělnímu US openu."""
    sunday_open = dt.datetime(2026, 11, 8, 23, 5, tzinfo=UTC)
    assert next_snapshot(sunday_open) == (
        dt.date(2026, 11, 9),
        dt.datetime(2026, 11, 9, 14, 30, 30, tzinfo=UTC),
    )


def test_start_v_nabehu_bezi_hned_po_hranici_zitra() -> None:
    in_lead = dt.datetime(2026, 11, 3, 14, 30, 45, tzinfo=UTC)
    assert next_snapshot(in_lead) == (SESSION, _start())
    assert next_snapshot(dt.datetime(2026, 11, 3, 14, 31, tzinfo=UTC))[0] == dt.date(2026, 11, 4)


def test_start_po_hranici_hlasi_seanci_bez_snimku() -> None:
    assert missed_session(dt.datetime(2026, 11, 3, 14, 32, tzinfo=UTC)) == SESSION
    assert missed_session(dt.datetime(2026, 11, 3, 23, 30, tzinfo=UTC)) == SESSION
    assert missed_session(dt.datetime(2026, 11, 3, 14, 30, 45, tzinfo=UTC)) is None  # v náběhu
    assert missed_session(dt.datetime(2026, 11, 7, 15, 0, tzinfo=UTC)) is None  # sobota
    assert missed_session(dt.datetime(2026, 11, 26, 15, 0, tzinfo=UTC)) is None  # Thanksgiving
    # Úterní noc po půlnoci ET je už středa, jejíž hranice ještě nepřišla
    assert missed_session(dt.datetime(2026, 11, 4, 6, 0, tzinfo=UTC)) is None


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
    snapshot = await harness.snapshotter.run(SESSION)
    assert snapshot is not None
    assert snapshot.complete, snapshot.reasons
    assert snapshot.zero_dte is not None and snapshot.one_dte is not None
    assert (snapshot.zero_dte.expiry, snapshot.one_dte.expiry) == ("20261103", "20261104")
    # Ceny na hranici 9:31:00 ET = stav na konci minuty 9:30 (ADR-0062, Odvozeno)
    assert snapshot.spot_ts == snapshot_boundary(SESSION)
    assert snapshot.spot == pytest.approx(SPOT)
    assert snapshot.spot_nq == NQ and snapshot.nq_source == "ibkr"
    assert snapshot.ratio == pytest.approx(NQ / SPOT)
    for snap in (snapshot.zero_dte, snapshot.one_dte):
        assert (snap.contracts, snap.quoted, len(snap.rows)) == (86, 86, 86)
        assert snap.with_oi == 85
        assert snap.oi_missing == 1  # C700 bez Summary → oimissing, ne změřená nula
        assert {row.ts_min for row in snap.rows} == {snapshot_boundary(SESSION)}
        table = pq.read_table(harness.partition(snap.expiry))
        assert table.num_rows == 86
        assert set(table.column("ts_min").to_pylist()) == {snapshot_boundary(SESSION)}
    assert not harness.partition("20261105").exists()  # 2DTE se neodebírá
    # Průběh subskripce: podklad → podklad + pásmo → nic
    assert harness.history[0] == {"QQQ"}
    assert len(harness.history[1]) == 1 + 2 * 86
    assert harness.history[-1] == set()
    assert harness.snapshotter.streamers() == set()
    assert "úplný" in snapshot.describe()


async def test_bez_kotaci_do_limitu_neuplny_s_duvodem(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, start=_start(), deliver_options=False)
    snapshot = await harness.snapshotter.run(SESSION)
    assert snapshot is not None and not snapshot.complete
    assert any("0DTE 20261103: kotace 0/86" in reason for reason in snapshot.reasons)
    assert any("1DTE 20261104: žádný platný řádek" in reason for reason in snapshot.reasons)
    assert snapshot.ts is not None
    limit = snapshot_boundary(SESSION) + dt.timedelta(seconds=QUOTE_TIMEOUT_S)
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
    snapshot = await harness.snapshotter.run(SESSION)
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
    snapshot = await harness.snapshotter.run(SESSION)
    assert snapshot is not None and not snapshot.complete
    assert snapshot.zero_dte is not None
    assert (snapshot.zero_dte.quoted, snapshot.zero_dte.with_oi) == (86, 0)
    assert snapshot.zero_dte.oi_missing == 86
    assert {row.oi for row in snapshot.zero_dte.rows} == {0.0}
    assert any("OI po openu 0/86" in reason for reason in snapshot.reasons)
    assert pq.read_table(harness.partition("20261103")).num_rows == 86


async def test_bez_kotace_qqq_pasmo_neodebira(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, start=_start(), deliver_underlying=False)
    snapshot = await harness.snapshotter.run(SESSION)
    assert snapshot is not None and not snapshot.complete
    assert any("kotace QQQ nedorazila" in reason for reason in snapshot.reasons)
    assert max(len(symbols) for symbols in harness.history) == 1
    assert harness.snapshotter.streamers() == set()


async def test_bez_spotu_nq_radky_ano_pomer_ne(tmp_path: Path) -> None:
    stale = SpotReading(NQ, _start() - dt.timedelta(minutes=5), "ibkr", False)
    harness = _Harness(tmp_path, start=_start(), nq=stale)
    snapshot = await harness.snapshotter.run(SESSION)
    assert snapshot is not None and not snapshot.complete
    assert snapshot.ratio is None and snapshot.spot_nq is None
    assert any(reason.startswith("spot NQ chybí") for reason in snapshot.reasons)
    assert harness.partition("20261104").exists()


async def test_chyba_retezu_nic_neodebira(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, start=_start(), fail_chain=True)
    snapshot = await harness.snapshotter.run(SESSION)
    assert snapshot is not None and not snapshot.complete
    assert "selhal" in snapshot.reasons[0]
    assert harness.history == []


async def test_zmeskana_hranice_nic_neodebira(tmp_path: Path) -> None:
    late = snapshot_boundary(SESSION) + dt.timedelta(seconds=5)
    harness = _Harness(tmp_path, start=late)
    snapshot = await harness.snapshotter.run(SESSION)
    assert snapshot is not None and not snapshot.complete
    assert "zmeškána" in snapshot.reasons[0]
    assert harness.history == []


@pytest.mark.parametrize("session", [dt.date(2026, 11, 7), dt.date(2026, 11, 26)])
async def test_vikend_a_svatek_se_nespousti(tmp_path: Path, session: dt.date) -> None:
    harness = _Harness(tmp_path, start=_start())
    with pytest.raises(ValueError, match="obchodní seanci"):
        await harness.snapshotter.run(session)
    assert harness.history == []


async def test_zavreny_trh_none_bez_upozorneni(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    pause = dt.datetime(2026, 11, 3, 22, 30, tzinfo=UTC)  # denní pauza 16:30 CT
    harness = _Harness(tmp_path, start=pause)
    with caplog.at_level(logging.INFO):
        assert await harness.snapshotter.run(SESSION) is None
    assert harness.history == []
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


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
