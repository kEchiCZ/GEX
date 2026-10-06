"""Cesta ceny setupu (#1320): vyhodnocení po barech bez díry, doplnění díry, timeout v settle.

3. 9. 14:17–15:30 UTC stál IBKR stream i spot; SetupEngine 72 minut hodnotil
rovnou čáru zamrzlého spotu, stop ve 14:56 (NQ 1003) minul a první živý bar
v 15:30 zapsal jako cíl. Timeouty braly cenu minuty, kdy se zjistily (20:02,
20:50 po restartu, pondělí po Labor Day), ne close baru, který v settle končí.
"""

import datetime as dt
import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import create_engine
from test_setups import FakeRuntime, RecordingPublisher

from gexlens_engine.compute.setups import (
    PATH_GAP_WAIT,
    Direction,
    Outcome,
    PathResult,
    PathState,
    SetupParams,
    SetupTemplate,
    last_expected_minute,
    missing_minutes,
    path_start,
    walk_setup_path,
)
from gexlens_engine.config import Settings
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.runtime import EngineRuntime
from gexlens_engine.setups import SetupEngine
from gexlens_engine.storage.oi_archive import OIEodRepository
from gexlens_engine.storage.parquet_store import (
    BAR_SOURCE_HISTORICAL,
    SnapshotWriter,
    read_bars,
)
from gexlens_engine.storage.setups_store import SetupsRepository

MINUTE = dt.timedelta(minutes=1)
# Čtvrtek 3. 9. 2026 (EDT): settle expirace 20260903 = 20:00 UTC
CREATED = dt.datetime(2026, 9, 3, 14, 15, tzinfo=dt.UTC)
SETTLE = dt.datetime(2026, 9, 3, 20, 0, tzinfo=dt.UTC)
# Long jako NQ 1003: entry 29300.75, cíl 29449.46, stop 29251.18
ENTRY, TARGET, STOP = 29300.75, 29449.5, 29251.25


def flat(ts: dt.datetime, price: float = ENTRY, *, source: str | None = None) -> Bar:
    """Bar bez zásahu úrovní (±5 b kolem ceny)."""
    return Bar(
        ts=ts, open=price, high=price + 5, low=price - 5, close=price, volume=100.0, source=source
    )


def bar(ts: dt.datetime, high: float, low: float, close: float, source: str | None = None) -> Bar:
    return Bar(ts=ts, open=close, high=high, low=low, close=close, volume=100.0, source=source)


def walk(
    bars: Sequence[Bar],
    state: PathState | None = None,
    *,
    now: dt.datetime,
    force_until: dt.datetime | None = None,
    settle: dt.datetime | None = SETTLE,
) -> PathResult:
    return walk_setup_path(
        Direction.LONG,
        ENTRY,
        TARGET,
        STOP,
        settle,
        bars,
        state if state is not None else path_start(CREATED),
        now=now,
        force_until=force_until,
    )


# ── Čistá funkce ──────────────────────────────────────────────────────────


def test_prvni_bar_je_minuta_vzniku_rozhodovaci_bar_se_nehodnoti() -> None:
    """Setup z minuty N stojí nad barem N−1; hodnotí se od baru N (MAE 1003 = low 14:15)."""
    decision = bar(CREATED - MINUTE, high=ENTRY, low=STOP - 10, close=ENTRY)  # před vznikem
    first = bar(CREATED, high=29325.5, low=29295.75, close=29315.25)
    result = walk_setup_path(
        Direction.LONG,
        ENTRY,
        TARGET,
        STOP,
        SETTLE,
        [decision, first],
        path_start(CREATED),
        now=CREATED + 2 * MINUTE,
    )
    assert result.outcome is None and not result.blocked
    assert result.state.last_ts == CREATED
    assert result.state.mae == pytest.approx(5.0)
    assert result.state.mfe == pytest.approx(24.75)


def test_stop_v_dire_neni_prejit_cilem() -> None:
    """Díra v barech zastaví vyhodnocení — pozdější cíl nesmí přeskočit stop v díře."""
    before = [flat(CREATED + i * MINUTE) for i in range(3)]  # 14:15–14:17
    resumed = bar(SETTLE - dt.timedelta(minutes=270), high=TARGET + 80, low=29500, close=29535)
    blocked = walk([*before, resumed], now=resumed.ts + 2 * MINUTE)
    assert blocked.outcome is None
    assert blocked.blocked
    assert blocked.state.last_ts == CREATED + 2 * MINUTE
    assert blocked.gaps == ((CREATED + 3 * MINUTE, resumed.ts - MINUTE),)

    # Backfill díru doplní: stop ve 14:56 → stop, ne cíl v 15:30
    gap = [flat(ts) for ts in _minutes(CREATED + 3 * MINUTE, resumed.ts)]
    stop_ts = dt.datetime(2026, 9, 3, 14, 56, tzinfo=dt.UTC)
    gap = [
        bar(b.ts, high=29325.5, low=29246.5, close=29296.5) if b.ts == stop_ts else b for b in gap
    ]
    closed = walk([*gap, resumed], blocked.state, now=resumed.ts + 3 * MINUTE)
    assert closed.outcome is Outcome.STOP
    assert closed.closed_ts == stop_ts
    assert closed.exit_price == STOP
    assert not closed.gaps


def test_bez_navazujiciho_baru_se_ceka_bez_blokace() -> None:
    """Výpadek streamu bez pozdějšího baru není díra — setup jen čeká (nic nevymýšlí)."""
    result = walk([flat(CREATED)], now=CREATED + dt.timedelta(minutes=40))
    assert result.outcome is None
    assert not result.blocked
    assert result.state.last_ts == CREATED


def test_denni_pauza_a_vikend_nejsou_dira() -> None:
    """Zavřený trh (16:00–17:00 CT, víkend) mezi bary dírou není."""
    friday_last = dt.datetime(2026, 9, 4, 20, 59, tzinfo=dt.UTC)  # pá 15:59 CT
    sunday_open = dt.datetime(2026, 9, 6, 22, 0, tzinfo=dt.UTC)  # ne 17:00 CT
    assert missing_minutes(friday_last, sunday_open) is None
    pause_last = dt.datetime(2026, 9, 3, 20, 59, tzinfo=dt.UTC)  # čt 15:59 CT
    assert missing_minutes(pause_last, pause_last + dt.timedelta(minutes=61)) is None
    assert missing_minutes(pause_last, pause_last + dt.timedelta(minutes=62)) == (
        pause_last + dt.timedelta(minutes=61),
        pause_last + dt.timedelta(minutes=61),
    )
    assert last_expected_minute(SETTLE) == SETTLE - MINUTE


def test_timeout_za_close_baru_konciciho_v_settle() -> None:
    """Timeout = close baru 19:59 (končí v settle), `closed_ts` = settle; bar po settle nepatří."""
    path = [flat(ts) for ts in _minutes(CREATED, SETTLE)]
    last = path[-1]
    path[-1] = bar(last.ts, high=ENTRY + 20, low=ENTRY + 10, close=ENTRY + 15)
    after = bar(SETTLE, high=TARGET + 50, low=ENTRY - 40, close=ENTRY - 30)
    result = walk([*path, after], now=SETTLE + 2 * MINUTE)
    assert result.outcome is Outcome.TIMEOUT
    assert result.closed_ts == SETTLE
    assert result.exit_price == ENTRY + 15

    # Před settle se timeout nedělá, i když bary do 19:59 jsou
    assert walk(path, now=SETTLE - MINUTE).outcome is None


def test_timeout_ceka_na_bar_19_59_a_po_cekani_bere_posledni_znamy() -> None:
    """Chybí-li bar končící v settle, timeout čeká; po vyčerpání čekání bere poslední close."""
    path = [flat(ts) for ts in _minutes(CREATED, SETTLE - MINUTE)]  # do 19:58
    waiting = walk(path, now=SETTLE)
    assert waiting.outcome is None and waiting.blocked
    assert waiting.gaps == ((SETTLE - MINUTE, SETTLE - MINUTE),)
    forced = walk(path, now=SETTLE + PATH_GAP_WAIT, force_until=SETTLE - MINUTE)
    assert forced.outcome is Outcome.TIMEOUT
    assert forced.closed_ts == SETTLE
    assert forced.exit_price == ENTRY
    assert forced.gaps == ((SETTLE - MINUTE, SETTLE - MINUTE),)
    # Bez jediného baru výstupní cena neexistuje — rozhodne volající (nic se nevymýšlí)
    empty = walk([], now=SETTLE + PATH_GAP_WAIT, force_until=SETTLE - MINUTE)
    assert empty.outcome is Outcome.TIMEOUT and empty.exit_price is None


def test_prejde_se_jen_dira_na_kterou_se_cekalo() -> None:
    """`force_until` přejde díru A, na kterou engine čekal; pozdější díra B krok zastaví.

    Kdyby čekání platilo pro celý setup, B by se přešla bez vlastního čekání
    a cíl v baru za ní by se zapsal dřív, než se B stihne doplnit.
    """
    hole_a = CREATED + MINUTE  # 14:16
    hole_b = CREATED + 15 * MINUTE  # 14:30
    bars = [flat(ts) for ts in _minutes(CREATED, hole_b) if ts != hole_a]
    hit = bar(hole_b + MINUTE, high=TARGET + 1, low=ENTRY, close=TARGET)
    result = walk([*bars, hit], now=hit.ts + 2 * MINUTE, force_until=hole_a)
    assert result.outcome is None and result.blocked
    assert result.gaps == ((hole_a, hole_a), (hole_b, hole_b))
    assert result.state.last_ts == hole_b - MINUTE  # za A se pokračovalo
    closed = walk([hit], result.state, now=hit.ts + 2 * MINUTE, force_until=hole_b)
    assert closed.outcome is Outcome.TARGET and closed.closed_ts == hit.ts


def test_novy_setup_nad_barem_19_59_konci_timeoutem_za_jeho_close() -> None:
    """Setup z cyklu 20:00 nad barem 19:59: cesta začíná ZA barem vstupu a nese jeho close.

    Bez `last_close` by timeout v settle neměl výstupní cenu (0 R s falešným
    „žádný bar“), přitom bar vstupu v settle končí.
    """
    entry_bar = bar(SETTLE - MINUTE, high=ENTRY + 3, low=ENTRY - 3, close=ENTRY)
    state = SetupEngine._new_path(entry_bar)
    assert state == PathState(last_ts=entry_bar.ts, last_close=ENTRY)
    result = walk([entry_bar], state, now=SETTLE + MINUTE)
    assert result.outcome is Outcome.TIMEOUT
    assert result.exit_price == ENTRY and result.closed_ts == SETTLE


# ── SetupEngine: živý běh ─────────────────────────────────────────────────


def _minutes(start: dt.datetime, end: dt.datetime) -> list[dt.datetime]:
    """Minuty [start, end)."""
    count = int((end - start).total_seconds() // 60)
    return [start + i * MINUTE for i in range(count)]


class Requests:
    """Hook `SetupEngine.request_bars`: zapisuje žádosti, volitelně díru doplní.

    `fill` simuluje IBKR historical, který doplnění zapíše do partice (v produkci
    na pozadí, `__main__.backfill_setup_gap`); None = HMDS nic nedodá.
    """

    def __init__(self, fill: Callable[[dt.datetime, dt.datetime], None] | None = None) -> None:
        self.calls: list[tuple[dt.datetime, dt.datetime]] = []
        self._fill = fill

    def __call__(self, since: dt.datetime, until: dt.datetime) -> bool:
        self.calls.append((since, until))
        if self._fill is not None:
            self._fill(since, until)
        return True


def _engine(
    tmp_path: Path, requests: Requests | None = None
) -> tuple[SetupEngine, SetupsRepository, Settings, int]:
    """Engine po restartu s jedním aktivním setupem (long jako NQ 1003) a čtením partic."""
    repository = SetupsRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 's.sqlite'}"))
    repository.ensure_schema()
    oi_repo = OIEodRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'oi.sqlite'}"))
    oi_repo.ensure_schema()
    setup_id = repository.create(
        symbol="NQ",
        expiry="20260903",
        template="trend_continuation",
        direction="long",
        created_ts=CREATED,
        entry=ENTRY,
        target=TARGET,
        stop=STOP,
        confidence=55,
        reason="test",
        context={},
    )
    settings = Settings(data_dir=tmp_path / "data")
    engine = SetupEngine(
        symbol="NQ",
        repository=repository,
        oi_repository=oi_repo,
        publisher=RecordingPublisher(),
        # Testuje se vyhodnocení, ne vznik: nový setup nad plochými bary by mátl
        params=SetupParams(disabled_templates=frozenset(t.value for t in SetupTemplate)),
        bar_reader=lambda since, until: read_bars(settings.derived_dir, "NQ", since, until),
        request_bars=requests,
    )
    return engine, repository, settings, setup_id


def _runtime() -> EngineRuntime:
    fake = FakeRuntime()
    fake.expiry = "20260903"
    return cast(EngineRuntime, fake)


async def test_engine_si_diru_vyzada_stop_v_dire_je_stop(tmp_path: Path) -> None:
    """Scénář 3. 9.: stream i spot stojí, pak přijde bar nad cílem — rozhoduje doplněná díra.

    Díru nedoplní nikdo jiný: stall detektor bez pohybu spotu nečítá, takže
    re-backfill po návratu streamu nepřijde. Engine si o doplnění řekne sám
    (jednou na díru) a vyhodnotí ji z partice.
    """
    stop_ts = dt.datetime(2026, 9, 3, 14, 56, tzinfo=dt.UTC)
    settings = Settings(data_dir=tmp_path / "data")
    writer = SnapshotWriter(settings)

    def ibkr_historical(since: dt.datetime, until: dt.datetime) -> None:
        writer.write_bars_by_day(
            "NQ",
            [
                bar(ts, high=29325.5, low=29246.5, close=29296.5, source=BAR_SOURCE_HISTORICAL)
                if ts == stop_ts
                else flat(ts, source=BAR_SOURCE_HISTORICAL)
                for ts in _minutes(since, until + MINUTE)
            ],
        )

    requests = Requests(ibkr_historical)
    engine, repository, settings, setup_id = _engine(tmp_path, requests)
    runtime = _runtime()
    # Živé bary 14:15–14:16 (cyklus N nese bar N−1) a partice jako v produkci
    for ts in _minutes(CREATED, CREATED + 2 * MINUTE):
        live = [flat(ts)]
        writer.write_bars_by_day("NQ", live)
        await engine.on_minute(ts + MINUTE, ENTRY, live, runtime)
    # Výpadek 14:17–15:29: žádné bary, spot zamrzlý NAD cílem — spot se nehodnotí
    for ts in _minutes(CREATED + 3 * MINUTE, dt.datetime(2026, 9, 3, 15, 30, tzinfo=dt.UTC)):
        await engine.on_minute(ts, TARGET + 10, [], runtime)
    assert [s.id for s in repository.active_for("NQ")] == [setup_id]

    # Stream se vrátí barem 15:30 nad cílem; díra v partici ještě doplněná není
    resumed = bar(
        dt.datetime(2026, 9, 3, 15, 30, tzinfo=dt.UTC), high=29537.75, low=29525.25, close=29535.0
    )
    writer.write_bars_by_day("NQ", [resumed])
    await engine.on_minute(resumed.ts + MINUTE, 29535.0, [resumed], runtime)
    assert [s.id for s in repository.active_for("NQ")] == [setup_id]  # čeká, nezavřel cíl
    # Žádost o doplnění právě díry 14:17–15:29 (IBKR historical na pozadí)
    assert requests.calls == [(CREATED + 2 * MINUTE, resumed.ts - MINUTE)]

    after = flat(resumed.ts + MINUTE, 29535.0)
    writer.write_bars_by_day("NQ", [after])
    await engine.on_minute(after.ts + MINUTE, 29535.0, [after], runtime)

    row = repository.list_for("NQ")[0]
    assert row["status"] == "closed_stop"
    assert row["outcome_r"] == pytest.approx(-1.0)
    assert str(row["closed_ts"]).startswith("2026-09-03T14:56")
    assert row["mae"] == pytest.approx(ENTRY - 29246.5)
    assert len(requests.calls) == 1  # jedna žádost na díru


async def test_engine_nedoplnena_dira_po_cekani_zalogovana(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Díra, kterou nic nedoplní, blokuje nejvýš PATH_GAP_WAIT; pak se přejde s varováním.

    Čekání samo je běžný stav (INFO), WARNING nese až vyhodnocení bez díry.
    Žádost o doplnění odejde jednou, i když ji HMDS nesplní.
    """
    requests = Requests()
    engine, repository, settings, setup_id = _engine(tmp_path, requests)
    writer = SnapshotWriter(settings)
    runtime = _runtime()
    # Živé bary jdou do partice před SetupEngine (runtime.run_cycle), jako v produkci
    first = flat(CREATED)
    writer.write_bars_by_day("NQ", [first])
    await engine.on_minute(CREATED + MINUTE, ENTRY, [first], runtime)
    # 14:16–14:17 chybí všude, 14:18 přijde s cílem
    hit = bar(CREATED + 3 * MINUTE, high=TARGET + 1, low=ENTRY, close=TARGET)
    writer.write_bars_by_day("NQ", [hit])
    with caplog.at_level(logging.INFO, logger="gexlens_engine.setups"):
        await engine.on_minute(hit.ts + MINUTE, TARGET, [hit], runtime)
        assert [s.id for s in repository.active_for("NQ")] == [setup_id]
        waits = [r for r in caplog.records if "čeká na doplnění" in r.getMessage()]
        assert len(waits) == 1 and "14:16–14:17" in waits[0].getMessage()
        assert waits[0].levelno == logging.INFO
        # Čekání běží (stream zase stojí) — setup dál čeká, varování se neopakuje
        for i in range(2, 15):
            await engine.on_minute(hit.ts + i * MINUTE, TARGET, [], runtime)
        assert [s.id for s in repository.active_for("NQ")] == [setup_id]
        # Po PATH_GAP_WAIT se hodnotí bez díry: bar 14:18 z partice nese cíl
        await engine.on_minute(hit.ts + MINUTE + PATH_GAP_WAIT, TARGET, [], runtime)
    row = repository.list_for("NQ")[0]
    assert row["status"] == "closed_target"
    assert str(row["closed_ts"]).startswith("2026-09-03T14:18")
    skipped = [r for r in caplog.records if "se nedoplnily" in r.getMessage()]
    assert len(skipped) == 1 and "14:16–14:17" in skipped[0].getMessage()
    assert skipped[0].levelno == logging.WARNING
    assert len([r for r in caplog.records if "čeká na doplnění" in r.getMessage()]) == 1
    assert requests.calls == [(CREATED + MINUTE, CREATED + 2 * MINUTE)]


async def test_engine_kazda_dira_ma_vlastni_cekani(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Doplní-li se díra A a cesta narazí na díru B, čeká B celých PATH_GAP_WAIT.

    Dřív platil čas blokace pro celý setup: B převzala zbytek čekání A, přešla se
    po 2 min bez INFO a WARNING tvrdil „ani za 15 min“. Typicky re-backfill po
    výpadku pokryje díru jen do návratu streamu a mezi ním a prvním živým barem
    zůstane minuta nebo dvě.
    """
    requests = Requests()
    engine, repository, settings, setup_id = _engine(tmp_path, requests)
    writer = SnapshotWriter(settings)
    runtime = _runtime()
    hole_a = CREATED + MINUTE  # 14:16
    hole_b = CREATED + 15 * MINUTE  # 14:30

    async def cycle(now: dt.datetime, live: list[Bar]) -> None:
        if live:
            writer.write_bars_by_day("NQ", live)  # run_cycle zapisuje před SetupEngine
        await engine.on_minute(now, ENTRY, live, runtime)

    with caplog.at_level(logging.INFO, logger="gexlens_engine.setups"):
        await cycle(CREATED + MINUTE, [flat(CREATED)])
        # 14:16 chybí; bary 14:17–14:29 chodí, setup stojí na díře A od 14:18
        for ts in _minutes(hole_a + MINUTE, hole_b):
            await cycle(ts + MINUTE, [flat(ts)])
        # 14:30 chybí, bar 14:31 nad cílem; ve 14:32 dorazí doplnění díry A
        writer.write_bars_by_day("NQ", [flat(hole_a, source=BAR_SOURCE_HISTORICAL)])
        hit = bar(hole_b + MINUTE, high=TARGET + 1, low=ENTRY, close=TARGET)
        await cycle(hit.ts + MINUTE, [hit])
        blocked_b = hit.ts + MINUTE  # od 14:32 stojí na B
        for ts in _minutes(blocked_b + MINUTE, blocked_b + PATH_GAP_WAIT):
            await cycle(ts, [])
        assert [s.id for s in repository.active_for("NQ")] == [setup_id]  # B čeká 15 min
        await cycle(blocked_b + PATH_GAP_WAIT, [])

    row = repository.list_for("NQ")[0]
    assert row["status"] == "closed_target"
    assert str(row["closed_ts"]).startswith("2026-09-03T14:31")
    waits = [r.getMessage() for r in caplog.records if "čeká na doplnění" in r.getMessage()]
    assert len(waits) == 2 and "14:16–14:16" in waits[0] and "14:30–14:30" in waits[1]
    skipped = [r.getMessage() for r in caplog.records if "se nedoplnily" in r.getMessage()]
    assert len(skipped) == 1 and "14:30–14:30" in skipped[0] and "14:16" not in skipped[0]
    assert requests.calls == [(hole_a, hole_a), (hole_b, hole_b)]


async def test_engine_uzavira_v_casovem_poradi_baru(tmp_path: Path) -> None:
    """Po restartu se v jednom kroku zavře víc setupů — vedlejší efekty v pořadí barů.

    Starší setup A zasáhne cíl ve 14:35, mladší B stop ve 14:25. Série stopů
    směru (#302) se výhrou maže: chronologicky stop → cíl = žádná série; v
    pořadí vzniku by cíl A smazal nic a stop B zůstal jako série 1.
    """
    repository = SetupsRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 's.sqlite'}"))
    repository.ensure_schema()
    oi_repo = OIEodRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'oi.sqlite'}"))
    oi_repo.ensure_schema()

    def create(template: str, created: dt.datetime, target: float, stop: float) -> int:
        return repository.create(
            symbol="NQ",
            expiry="20260903",
            template=template,
            direction="long",
            created_ts=created,
            entry=ENTRY,
            target=target,
            stop=stop,
            confidence=55,
            reason="test",
            context={},
        )

    first = create("trend_continuation", CREATED, TARGET, STOP)
    second = create("failed_break", CREATED + 5 * MINUTE, ENTRY + 300, ENTRY - 20)
    settings = Settings(data_dir=tmp_path / "data")
    stop_b = CREATED + 10 * MINUTE  # 14:25
    target_a = CREATED + 20 * MINUTE  # 14:35
    SnapshotWriter(settings).write_bars_by_day(
        "NQ",
        [
            bar(ts, high=ENTRY + 5, low=ENTRY - 25, close=ENTRY)
            if ts == stop_b
            else bar(ts, high=TARGET, low=ENTRY - 5, close=ENTRY)
            if ts == target_a
            else flat(ts)
            for ts in _minutes(CREATED - MINUTE, CREATED + 26 * MINUTE)
        ],
    )
    engine = SetupEngine(
        symbol="NQ",
        repository=repository,
        oi_repository=oi_repo,
        publisher=RecordingPublisher(),
        params=SetupParams(disabled_templates=frozenset(t.value for t in SetupTemplate)),
        bar_reader=lambda since, until: read_bars(settings.derived_dir, "NQ", since, until),
    )
    live = flat(CREATED + 25 * MINUTE)
    await engine.on_minute(live.ts + MINUTE, ENTRY, [live], _runtime())

    rows = {row["id"]: row for row in repository.list_for("NQ")}
    assert rows[first]["status"] == "closed_target"
    assert rows[second]["status"] == "closed_stop"
    assert "long" not in engine._direction_stops


async def test_engine_timeout_za_close_v_settle_ne_za_cenu_cyklu(tmp_path: Path) -> None:
    """Cyklus 20:00 ještě bar 19:59 nemá → čeká; 20:01 přinese 19:59 i 20:00 → close 19:59."""
    engine, repository, settings, setup_id = _engine(tmp_path)
    writer = SnapshotWriter(settings)
    runtime = _runtime()
    path = [flat(ts) for ts in _minutes(CREATED, SETTLE - MINUTE)]  # 14:15–19:58
    writer.write_bars_by_day("NQ", path)
    await engine.on_minute(SETTLE - MINUTE, ENTRY, [path[-1]], runtime)
    await engine.on_minute(SETTLE, ENTRY + 99, [], runtime)
    assert [s.id for s in repository.active_for("NQ")] == [setup_id]
    last = bar(SETTLE - MINUTE, high=ENTRY + 20, low=ENTRY + 10, close=ENTRY + 15)
    after = bar(SETTLE, high=ENTRY + 60, low=ENTRY + 40, close=ENTRY + 50)
    await engine.on_minute(SETTLE + MINUTE, ENTRY + 50, [last, after], runtime)
    row = repository.list_for("NQ")[0]
    assert row["status"] == "closed_timeout"
    assert row["outcome_r"] == pytest.approx(15 / (ENTRY - STOP))
    assert str(row["closed_ts"]).startswith("2026-09-03T20:00")


def test_read_bars_okno_pres_pulnoc_a_puvod(tmp_path: Path) -> None:
    """Čtenář cesty: `since < ts ≤ until` přes dvě UTC partice, s původem baru."""
    settings = Settings(data_dir=tmp_path / "data")
    midnight = dt.datetime(2026, 9, 4, 0, 0, tzinfo=dt.UTC)
    SnapshotWriter(settings).write_bars_by_day(
        "NQ",
        [flat(midnight - 2 * MINUTE), flat(midnight - MINUTE, source=BAR_SOURCE_HISTORICAL)]
        + [flat(midnight), flat(midnight + MINUTE)],
    )
    bars = read_bars(settings.derived_dir, "NQ", midnight - 2 * MINUTE, midnight)
    assert [b.ts for b in bars] == [midnight - MINUTE, midnight]
    assert [b.source for b in bars] == [BAR_SOURCE_HISTORICAL, "ibkr"]
    assert read_bars(settings.derived_dir, "ES", midnight, midnight + MINUTE) == []
