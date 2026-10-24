"""Smoke test runtime (issue #30): jeden cyklus nad mocky vyprodukuje kompletní den."""

import datetime as dt
import time
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import create_engine

from gexlens_engine.compute.cumdelta import (
    SOURCE_AGGRESSOR,
    SOURCE_MIDPOINT,
    CumDeltaTracker,
    FlowRow,
)
from gexlens_engine.config import Settings
from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.ibkr.mock import MockQuoteStreamer
from gexlens_engine.ibkr.scheduler import (
    FEED_TASTY,
    CachedQuote,
    PartialQuote,
    QuoteSnapshot,
    SubscriptionScheduler,
)
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.runtime import EngineRuntime, PublisherLike
from gexlens_engine.storage.oi_archive import OIEodRepository, OIRecord
from gexlens_engine.storage.parquet_store import NetFlowRow, SnapshotWriter

TS = dt.datetime(2026, 7, 16, 15, 0, tzinfo=dt.UTC)
SPOT = 7600.0


class RecordingPublisher(PublisherLike):
    def __init__(self) -> None:
        self.statuses: list[dict[str, object]] = []
        self.messages: list[tuple[str, dict[str, object]]] = []

    async def status(self, **fields: object) -> None:
        self.statuses.append(fields)

    async def publish(self, channel: str, data: dict[str, object]) -> None:
        self.messages.append((channel, data))


def contracts() -> list[OptionContractSpec]:
    return [
        OptionContractSpec("ES", "FOP", "20260716", strike, right, "CME", "E3D", "50")
        for strike in (7590.0, 7600.0, 7610.0)
        for right in ("C", "P")
    ]


@pytest.fixture
def runtime(tmp_path: Path) -> tuple[EngineRuntime, RecordingPublisher, Settings]:
    settings = Settings(data_dir=tmp_path / "data")
    specs = contracts()
    repository = OIEodRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'db.sqlite'}"))
    repository.ensure_schema()
    repository.upsert_many(
        [OIRecord("ES", "20260716", s.strike, s.right, TS.date(), 1000.0) for s in specs]
    )
    publisher = RecordingPublisher()
    engine_runtime = EngineRuntime(
        settings=settings,
        scheduler=SubscriptionScheduler(MockQuoteStreamer(), settings),
        writer=SnapshotWriter(settings),
        oi_repository=repository,
        publisher=publisher,
        symbol="ES",
        expiry="20260716",
        multiplier=50.0,
        contracts=specs,
        # Midpoint explicitně (#1460): bump v6 přepne jen `LIVE_SOURCE`, test zůstává v5
        cum_delta=CumDeltaTracker(multiplier=50.0, source=SOURCE_MIDPOINT),
    )
    return engine_runtime, publisher, settings


async def test_striky_bez_oi_jdou_do_vlastni_rady(
    runtime: tuple[EngineRuntime, RecordingPublisher, Settings],
) -> None:
    """#465: strike, který archiv nepokrývá, se nesmí tvářit jako změřená nula."""
    engine_runtime, _publisher, settings = runtime
    # Strike přibylý posunem pásma — v archivu (fixture) není
    novy = OptionContractSpec("ES", "FOP", "20260716", 7620.0, "C", "CME", "E3D", "50")
    engine_runtime.contracts = [*engine_runtime.contracts, novy]

    await engine_runtime.run_cycle(TS, SPOT, [])

    day = TS.date().isoformat()
    missing = pd.read_parquet(
        settings.derived_dir / "ES" / "20260716" / "oimissing" / f"{day}.parquet"
    )
    assert list(missing.columns) == ["ts_min", "strike", "right"]
    assert missing["strike"].tolist() == [7620.0]
    assert missing["right"].tolist() == ["C"]

    # Ve snapshotu zůstává 0.0 (do výpočtů přispívá nulou), rozlišení nese řada
    snapshots = pd.read_parquet(settings.snapshots_dir / "ES" / "20260716" / f"{day}.parquet")
    assert snapshots.loc[snapshots["strike"] == 7620.0, "oi"].tolist() == [0.0]


async def test_bez_chybejiciho_oi_rada_nevznikne(
    runtime: tuple[EngineRuntime, RecordingPublisher, Settings],
) -> None:
    """Běžný den: všechny striky mají OI → soubor se vůbec nezaloží (#465)."""
    engine_runtime, _publisher, settings = runtime

    await engine_runtime.run_cycle(TS, SPOT, [])

    day = TS.date().isoformat()
    assert not (settings.derived_dir / "ES" / "20260716" / "oimissing" / f"{day}.parquet").exists()


async def test_oi_fill_z_tasty_jde_do_vlastni_rady(
    runtime: tuple[EngineRuntime, RecordingPublisher, Settings],
) -> None:
    """#664: strike bez archivu doplní tasty Summary — hodnota do snapshotu,
    záznam do řady oifilled; archivované OI má vždy přednost před fallbackem."""
    engine_runtime, _publisher, settings = runtime
    novy = OptionContractSpec("ES", "FOP", "20260716", 7620.0, "C", "CME", "E3D", "50")
    engine_runtime.contracts = [*engine_runtime.contracts, novy]
    # Fallback vrací hodnotu pro všechno — archivované striky ho nesmí použít
    engine_runtime.oi_fallback = lambda spec: 321.0

    metrics = await engine_runtime.run_cycle(TS, SPOT, [])

    day = TS.date().isoformat()
    filled = pd.read_parquet(
        settings.derived_dir / "ES" / "20260716" / "oifilled" / f"{day}.parquet"
    )
    assert filled["strike"].tolist() == [7620.0]
    assert filled["right"].tolist() == ["C"]
    # Doplněný strike NENÍ chybějící — řada oimissing nevznikne
    assert not (settings.derived_dir / "ES" / "20260716" / "oimissing" / f"{day}.parquet").exists()
    snapshots = pd.read_parquet(settings.snapshots_dir / "ES" / "20260716" / f"{day}.parquet")
    assert snapshots.loc[snapshots["strike"] == 7620.0, "oi"].tolist() == [321.0]
    # Archivovaný strike drží hodnotu z archivu, ne z fallbacku
    assert set(snapshots.loc[snapshots["strike"] == 7600.0, "oi"]) == {1000.0}
    assert metrics.oi_present == 6
    assert metrics.oi_filled == 1
    assert metrics.oi_missing == 0


async def test_oi_fill_bez_fallbacku_zustava_missing(
    runtime: tuple[EngineRuntime, RecordingPublisher, Settings],
) -> None:
    """#664: fallback vrací None (tasty symbol nezná) → chování #465 beze změny."""
    engine_runtime, _publisher, settings = runtime
    novy = OptionContractSpec("ES", "FOP", "20260716", 7620.0, "C", "CME", "E3D", "50")
    engine_runtime.contracts = [*engine_runtime.contracts, novy]
    engine_runtime.oi_fallback = lambda spec: None

    metrics = await engine_runtime.run_cycle(TS, SPOT, [])

    day = TS.date().isoformat()
    assert not (settings.derived_dir / "ES" / "20260716" / "oifilled" / f"{day}.parquet").exists()
    missing = pd.read_parquet(
        settings.derived_dir / "ES" / "20260716" / "oimissing" / f"{day}.parquet"
    )
    assert missing["strike"].tolist() == [7620.0]
    assert metrics.oi_filled == 0
    assert metrics.oi_missing == 1


async def test_dopoctene_greeks_jdou_do_vlastni_rady(tmp_path: Path) -> None:
    """#547: strike s fallback BS greeks se zapíše do snapshotu a označí v řadě greekssource."""
    settings = Settings(data_dir=tmp_path / "data", greeks_fallback_sweeps=1)
    specs = contracts()
    repository = OIEodRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'db.sqlite'}"))
    repository.ensure_schema()
    repository.upsert_many(
        [OIRecord("ES", "20260716", s.strike, s.right, TS.date(), 1000.0) for s in specs]
    )
    publisher = RecordingPublisher()
    atm_call = next(s for s in specs if s.strike == 7600.0 and s.right == "C")
    # TWS model pro ATM call mlčí, kotace tečou (scénář 7. 8. na NQ)
    streamer = MockQuoteStreamer(partial_greeks={atm_call})
    engine_runtime = EngineRuntime(
        settings=settings,
        # τ do settle 20:00 UTC téhož dne — pevné „teď" kvůli determinismu
        scheduler=SubscriptionScheduler(streamer, settings, utc_now=lambda: TS),
        writer=SnapshotWriter(settings),
        oi_repository=repository,
        publisher=publisher,
        symbol="ES",
        expiry="20260716",
        multiplier=50.0,
        contracts=specs,
        # Midpoint explicitně (#1460): bump v6 přepne jen `LIVE_SOURCE`, test zůstává v5
        cum_delta=CumDeltaTracker(multiplier=50.0, source=SOURCE_MIDPOINT),
    )

    await engine_runtime.run_cycle(TS, SPOT, [])

    day = TS.date().isoformat()
    source = pd.read_parquet(
        settings.derived_dir / "ES" / "20260716" / "greekssource" / f"{day}.parquet"
    )
    assert list(source.columns) == ["ts_min", "strike", "right"]
    assert source["strike"].tolist() == [7600.0]
    assert source["right"].tolist() == ["C"]

    # Snapshot nese dopočtené hodnoty (IV z inverze, ne mockových 0.15)
    snapshots = pd.read_parquet(settings.snapshots_dir / "ES" / "20260716" / f"{day}.parquet")
    row = snapshots[(snapshots["strike"] == 7600.0) & (snapshots["right"] == "C")]
    assert len(row) == 1
    assert float(row["iv"].iloc[0]) > 0.0
    assert float(row["iv"].iloc[0]) != 0.15
    assert 0.0 < float(row["delta"].iloc[0]) < 1.0

    # WS zpráva minuty nese aditivní příznak jen u dopočteného řádku
    snap_data = next(
        data for channel, data in publisher.messages if channel == "snapshot.ES.20260716"
    )
    snap_rows = snap_data["rows"]
    assert isinstance(snap_rows, list)
    flagged = [r for r in snap_rows if r.get("greeks_computed")]
    assert [(r["strike"], r["right"]) for r in flagged] == [(7600.0, "C")]
    ostatni = [r for r in snap_rows if not r.get("greeks_computed")]
    assert all("greeks_computed" not in r for r in ostatni)


async def test_prvni_sweep_po_startu_uprostred_dne_ma_catch_up(
    runtime: tuple[EngineRuntime, RecordingPublisher, Settings],
) -> None:
    """#518 (ADR-0024): první sweep po startu uprostřed dne nese catch_up flag.

    TS je 15:00 UTC ve čtvrtek — trh byl otevřený i minutu předtím, takže
    předchozí minuty dne chybí a kumulativy prvního sweepu dohánějí celý den.
    """
    engine_runtime, publisher, settings = runtime

    await engine_runtime.run_cycle(TS, SPOT, [])
    await engine_runtime.run_cycle(TS + dt.timedelta(minutes=1), SPOT, [])

    day = TS.date().isoformat()
    catchup = pd.read_parquet(
        settings.derived_dir / "ES" / "20260716" / "catchup" / f"{day}.parquet"
    )
    assert list(catchup.columns) == ["ts_min"]
    # Flag má JEN první sweep procesu — další cykly už jsou běžné minuty
    assert len(catchup) == 1
    assert catchup["ts_min"].iloc[0].to_pydatetime() == TS

    # WS: aditivní klíč jen v první minutě, běžná minuta zprávu nenafukuje
    snaps = [data for channel, data in publisher.messages if channel == "snapshot.ES.20260716"]
    assert len(snaps) == 2
    assert snaps[0].get("catch_up") is True
    assert "catch_up" not in snaps[1]


async def test_start_na_zacatku_dne_je_bez_catch_up(
    runtime: tuple[EngineRuntime, RecordingPublisher, Settings],
) -> None:
    """#518: start na začátku dne — žádné minuty dne nechybí, flag nevznikne."""
    engine_runtime, publisher, settings = runtime
    midnight = dt.datetime(2026, 7, 16, 0, 0, tzinfo=dt.UTC)

    await engine_runtime.run_cycle(midnight, SPOT, [])

    day = midnight.date().isoformat()
    assert not (settings.derived_dir / "ES" / "20260716" / "catchup" / f"{day}.parquet").exists()
    snap = next(data for channel, data in publisher.messages if channel == "snapshot.ES.20260716")
    assert "catch_up" not in snap


async def test_start_na_otevreni_seance_je_bez_catch_up(
    runtime: tuple[EngineRuntime, RecordingPublisher, Settings],
) -> None:
    """#518: start na otevření seance — předchozí minuta byla denní přestávka CME."""
    engine_runtime, _publisher, settings = runtime
    session_open = dt.datetime(2026, 7, 16, 22, 0, tzinfo=dt.UTC)  # 17:00 CT

    await engine_runtime.run_cycle(session_open, SPOT, [])

    day = session_open.date().isoformat()
    assert not (settings.derived_dir / "ES" / "20260716" / "catchup" / f"{day}.parquet").exists()


async def test_one_cycle_produces_full_day_artifacts(
    runtime: tuple[EngineRuntime, RecordingPublisher, Settings],
) -> None:
    engine_runtime, publisher, settings = runtime
    bars = [Bar(ts=TS, open=7599.0, high=7601.0, low=7598.0, close=SPOT, volume=1200.0)]

    await engine_runtime.run_cycle(TS, SPOT, bars)

    day = TS.date().isoformat()
    snapshots = pd.read_parquet(settings.snapshots_dir / "ES" / "20260716" / f"{day}.parquet")
    assert len(snapshots) == 6
    assert snapshots["oi"].iloc[0] == 1000.0  # OI z ranního archivu

    levels = pd.read_parquet(settings.derived_dir / "ES" / "20260716" / "levels" / f"{day}.parquet")
    assert len(levels) == 1
    # Sekundární zdi jdou do vlastní řady levels2 (ADR-0008, #92)
    levels2 = pd.read_parquet(
        settings.derived_dir / "ES" / "20260716" / "levels2" / f"{day}.parquet"
    )
    assert list(levels2.columns) == ["ts_min", "call_wall_2", "put_wall_2"]
    assert len(levels2) == 1
    # Dominance zdí jde do vlastní řady walldom (ADR-0010, #223)
    walldom = pd.read_parquet(
        settings.derived_dir / "ES" / "20260716" / "walldom" / f"{day}.parquet"
    )
    assert list(walldom.columns) == [
        "ts_min",
        "call_wall_dom",
        "put_wall_dom",
        "call_wall_2_dom",
        "put_wall_2_dom",
    ]
    assert len(walldom) == 1
    # GEX žebřík (#244): vlastní řada s list sloupci + WS kanál
    ladder = pd.read_parquet(settings.derived_dir / "ES" / "20260716" / "ladder" / f"{day}.parquet")
    assert list(ladder.columns) == [
        "ts_min",
        "call_strikes",
        "call_shares",
        "put_strikes",
        "put_shares",
    ]
    assert len(ladder) == 1
    # Flow-adjusted levels (ADR-0011, #222): vlastní řada + WS kanál; první cyklus
    # bez přírůstku volume → odhad == měřené levels
    levelsfa = pd.read_parquet(
        settings.derived_dir / "ES" / "20260716" / "levelsfa" / f"{day}.parquet"
    )
    assert len(levelsfa) == 1
    assert levelsfa["total_gex"].iloc[0] == levels["total_gex"].iloc[0]
    pd.testing.assert_frame_equal(levelsfa, levels)
    flow = pd.read_parquet(settings.derived_dir / "ES" / "flow" / f"{day}.parquet")
    assert list(flow.columns) == [
        "ts_min",
        "flow_delta",
        "cum_delta",
        "futures_cvd_delta",  # CVD podkladu (#829) — bez tasty větve NULL
        "futures_cvd",
        "source",  # ADR-0032: zdroj znaménka toku
        # Pokrytí trade větví za minutu (#1071) — bez tasty větve NULL, ne nuly
        "printed_volume",
        "unknown_volume",
        "structured_volume",
        "fallback_volume",
        "dropped_no_delta",
    ]
    assert list(flow["source"]) == ["midpoint"]
    assert flow["printed_volume"].isna().all() and flow["fallback_volume"].isna().all()
    day_bars = pd.read_parquet(settings.derived_dir / "ES" / "bars" / f"{day}.parquet")
    assert day_bars["close"].iloc[0] == SPOT

    # Push do API: status + levels + flow + price kanály
    assert publisher.statuses[-1]["engine"] == "online"
    assert publisher.statuses[-1]["greeks_complete"] == 6
    channels = [channel for channel, _ in publisher.messages]
    assert "levels.ES.20260716" in channels
    # WS levels nese dominance zdí aditivně (ADR-0010, #223)
    levels_data = next(
        data for channel, data in publisher.messages if channel == "levels.ES.20260716"
    )
    assert "call_wall_dom" in levels_data and "put_wall_dom" in levels_data
    assert "flow.ES" in channels
    # Zdroj znaménka i ve WS (ADR-0057 bod 8) — aditivní pole
    flow_data = next(data for channel, data in publisher.messages if channel == "flow.ES")
    assert flow_data["source"] == "midpoint"
    assert "levelsfa.ES.20260716" in channels  # ADR-0011, #222
    assert "ladder.ES.20260716" in channels  # #244
    assert "price.ES" in channels
    assert "snapshot.ES.20260716" in channels
    # Dyn GEX profil (ADR-0009): kanál + persistence do vlastní řady
    assert "gexprofile.ES.20260716" in channels
    gexprofile_data = next(
        data for channel, data in publisher.messages if channel == "gexprofile.ES.20260716"
    )
    assert isinstance(gexprofile_data["values"], list) and gexprofile_data["values"]
    gexprofile = pd.read_parquet(
        settings.derived_dir / "ES" / "20260716" / "gexprofile" / f"{day}.parquet"
    )
    assert len(gexprofile) == 1
    # Modelované pole (ADR-0009 fáze 2): kanál + partice jen s posledním stavem
    assert "gexfield.ES.20260716" in channels
    gexfield_data = next(
        data for channel, data in publisher.messages if channel == "gexfield.ES.20260716"
    )
    field_values = gexfield_data["values"]
    field_cols = gexfield_data["col_count"]
    assert isinstance(field_values, list) and field_values
    assert isinstance(field_cols, int) and field_cols > 0
    assert len(field_values) % field_cols == 0  # sloupce za sebou, celé násobky mřížky
    gexfield = pd.read_parquet(
        settings.derived_dir / "ES" / "20260716" / "gexfield" / f"{day}.parquet"
    )
    assert len(gexfield) == 1  # jen poslední stav (replace_and_write)
    # FA Dyn GEX (#232): vlastní řady + kanály; bez toku je odhad == měření,
    # takže FA profil kopíruje měřený a řady netflow/oiest vůbec nevzniknou
    assert "gexprofilefa.ES.20260716" in channels
    assert "gexfieldfa.ES.20260716" in channels
    gexprofilefa = pd.read_parquet(
        settings.derived_dir / "ES" / "20260716" / "gexprofilefa" / f"{day}.parquet"
    )
    assert list(gexprofilefa.iloc[0]["values"]) == pytest.approx(
        list(gexprofile.iloc[0]["values"]), abs=0.11
    )
    assert not (settings.derived_dir / "ES" / "20260716" / "netflow" / f"{day}.parquet").exists()
    assert not (settings.derived_dir / "ES" / "20260716" / "oiest" / f"{day}.parquet").exists()

    # price kanál nese plnou OHLC (#127), ne jen close
    price_data = next(data for channel, data in publisher.messages if channel == "price.ES")
    assert price_data["open"] == 7599.0
    assert price_data["high"] == 7601.0
    assert price_data["low"] == 7598.0
    assert price_data["close"] == SPOT
    assert price_data["volume"] == 1200.0
    assert price_data["final"] is True  # uzavřený bar (ADR-0005)

    # levels kanál nese i sekundární zdi (aditivní pole, ADR-0008)
    levels_data = next(
        data for channel, data in publisher.messages if channel == "levels.ES.20260716"
    )
    assert "call_wall_2" in levels_data
    assert "put_wall_2" in levels_data

    # snapshot kanál nese per-strike řez minuty (#127)
    snap_data = next(
        data for channel, data in publisher.messages if channel == "snapshot.ES.20260716"
    )
    snap_rows = snap_data["rows"]
    assert isinstance(snap_rows, list) and len(snap_rows) == 6
    assert set(snap_rows[0]) >= {"strike", "right", "oi", "volume", "delta", "stale_age"}
    assert snap_rows[0]["oi"] == 1000.0


class RisingVolumeStreamer(MockQuoteStreamer):
    """Kumulativní volume roste s každým fetch; buy klasifikaci mají jen cally.

    Call: last nad midem → midpoint test buy, druhá minuta +100 kontraktů.
    Put: last přesně na midu → sign 0, net zůstává nulový. Asymetrie je nutná —
    při stejném OI obou stran se NetGEX profil vynuluje (call − put) a FA
    vrstva by nebyla od měřené k rozeznání.
    """

    def __init__(self) -> None:
        super().__init__()
        self._fetches: dict[OptionContractSpec, int] = {}

    async def fetch_quote(
        self, spec: OptionContractSpec, timeout_s: float
    ) -> QuoteSnapshot | PartialQuote | None:
        base = await super().fetch_quote(spec, timeout_s)
        if base is None:
            return None
        count = self._fetches[spec] = self._fetches.get(spec, 0) + 1
        return QuoteSnapshot(
            bid=10.0,
            ask=10.5,
            last=10.4 if spec.right == "C" else 10.25,  # nad midem = buy / na midu = 0
            volume=100.0 * count,
            iv=0.15,
            delta=0.5 if spec.right == "C" else -0.5,
            gamma=0.01,
            theta=-0.5,
            vega=1.2,
        )


def build_runtime(
    tmp_path: Path, streamer: MockQuoteStreamer, **settings_kwargs: object
) -> tuple[EngineRuntime, RecordingPublisher, Settings]:
    """Runtime nad zadaným streamerem; OI archiv fixture 1000 per strana."""
    settings = Settings(data_dir=tmp_path / "data", **settings_kwargs)  # type: ignore[arg-type]
    specs = contracts()
    repository = OIEodRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'db.sqlite'}"))
    repository.ensure_schema()
    repository.upsert_many(
        [OIRecord("ES", "20260716", s.strike, s.right, TS.date(), 1000.0) for s in specs]
    )
    publisher = RecordingPublisher()
    engine_runtime = EngineRuntime(
        settings=settings,
        scheduler=SubscriptionScheduler(streamer, settings),
        writer=SnapshotWriter(settings),
        oi_repository=repository,
        publisher=publisher,
        symbol="ES",
        expiry="20260716",
        multiplier=50.0,
        contracts=specs,
        # Midpoint explicitně (#1460): bump v6 přepne jen `LIVE_SOURCE`, test zůstává v5
        cum_delta=CumDeltaTracker(multiplier=50.0, source=SOURCE_MIDPOINT),
    )
    return engine_runtime, publisher, settings


async def test_klasifikovany_tok_zapisuje_netflow_oiest_a_fa_vrstvy(tmp_path: Path) -> None:
    """#232: přírůstek volume → řady netflow/oiest + FA Dyn GEX z TÉHOŽ odhadu."""
    engine_runtime, publisher, settings = build_runtime(tmp_path, RisingVolumeStreamer())
    await engine_runtime.run_cycle(TS, SPOT, [])  # baseline — bez přírůstku
    await engine_runtime.run_cycle(TS + dt.timedelta(minutes=1), SPOT, [])

    day = TS.date().isoformat()
    base = settings.derived_dir / "ES" / "20260716"
    netflow = pd.read_parquet(base / "netflow" / f"{day}.parquet")
    assert list(netflow.columns) == ["ts_min", "strike", "right", "net_volume", "source"]
    assert set(netflow["source"]) == {SOURCE_MIDPOINT}  # zdroj trackeru (#1460)
    # Jen druhá minuta má klasifikovaný přírůstek: call strany 3 × +100, put
    # strany (last na midu) mají naměřenou nulu a řádek dostanou taky (#1582)
    assert len(netflow) == 6
    by_right = netflow.groupby("right")["net_volume"].apply(set).to_dict()
    assert by_right == {"C": {100.0}, "P": {0.0}}

    oiest = pd.read_parquet(base / "oiest" / f"{day}.parquet")
    assert list(oiest.columns) == ["ts_min", "strike", "right", "oi_est"]
    # OI_est = 1000 + α(0.4)·100 = 1040 — stejná formule jako FA levels
    assert set(oiest["oi_est"]) == {1040.0}
    assert set(oiest["right"]) == {"C"}
    assert len(oiest) == 3

    channels = [channel for channel, _ in publisher.messages]
    assert "oiest.ES.20260716" in channels
    oiest_data = next(
        data for channel, data in publisher.messages if channel == "oiest.ES.20260716"
    )
    rows = oiest_data["rows"]
    assert isinstance(rows, list) and len(rows) == 3
    assert rows[0]["oi_est"] == 1040.0

    # FA Dyn GEX profil/pole: vlastní řady + kanály; druhá minuta se od měřené
    # vrstvy liší (OI_est > OI), takže hodnoty nejsou identické
    fa_profile = pd.read_parquet(base / "gexprofilefa" / f"{day}.parquet")
    measured_profile = pd.read_parquet(base / "gexprofile" / f"{day}.parquet")
    assert len(fa_profile) == 2
    # První minuta bez toku: FA == měřené (až na zaokrouhlení 0.1 v zápisu)
    assert list(fa_profile.iloc[0]["values"]) == pytest.approx(
        list(measured_profile.iloc[0]["values"]), abs=0.11
    )
    assert list(fa_profile.iloc[1]["values"]) != list(measured_profile.iloc[1]["values"])
    assert "gexprofilefa.ES.20260716" in channels
    assert "gexfieldfa.ES.20260716" in channels
    assert (base / "gexfieldfa" / f"{day}.parquet").exists()


async def test_netflow_seed_navaze_kumulativ_po_restartu(tmp_path: Path) -> None:
    """#232: nový proces naváže FA odhad z partice netflow, ne od nuly."""
    first_runtime, _publisher, _settings = build_runtime(tmp_path, RisingVolumeStreamer())
    await first_runtime.run_cycle(TS, SPOT, [])
    await first_runtime.run_cycle(TS + dt.timedelta(minutes=1), SPOT, [])

    # „Restart": nový runtime se svým writerem, trackerem i streamerem
    second_runtime, publisher, settings = build_runtime(tmp_path, RisingVolumeStreamer())
    await second_runtime.run_cycle(TS + dt.timedelta(minutes=2), SPOT, [])

    day = TS.date().isoformat()
    oiest = pd.read_parquet(settings.derived_dir / "ES" / "20260716" / "oiest" / f"{day}.parquet")
    third = oiest[oiest["ts_min"] == TS + dt.timedelta(minutes=2)]
    # První sweep nového procesu nemá vlastní přírůstek — odhad 1040 nese
    # výhradně kumulativ obnovený z partice (+100 × α 0.4, jen call strany)
    assert len(third) == 3
    assert set(third["right"]) == {"C"}
    assert set(third["oi_est"]) == {1040.0}


class ReversingVolumeStreamer(RisingVolumeStreamer):
    """Jako `RisingVolumeStreamer`, ale od třetího fetch jsou cally pod midem (sell).

    Call strany: druhá minuta +100 buy, třetí +100 sell → net se vrátí na 0.
    """

    async def fetch_quote(
        self, spec: OptionContractSpec, timeout_s: float
    ) -> QuoteSnapshot | PartialQuote | None:
        quote = await super().fetch_quote(spec, timeout_s)
        if isinstance(quote, QuoteSnapshot) and spec.right == "C" and self._fetches[spec] >= 3:
            return replace(quote, last=10.1)  # pod midem 10.25 = sell
        return quote


async def test_netflow_zapise_navrat_strany_na_nulu(tmp_path: Path) -> None:
    """#1582: strana s návratem netu na 0 dostane řádek 0, z partice nezmizí."""
    engine_runtime, _publisher, settings = build_runtime(tmp_path, ReversingVolumeStreamer())
    for minute in range(3):
        await engine_runtime.run_cycle(TS + dt.timedelta(minutes=minute), SPOT, [])

    netflow = pd.read_parquet(
        settings.derived_dir / "ES" / "20260716" / "netflow" / f"{TS.date().isoformat()}.parquet"
    )
    calls = netflow[netflow["right"] == "C"]
    by_minute = calls.groupby("ts_min")["net_volume"].apply(list).to_dict()
    assert by_minute == {
        TS + dt.timedelta(minutes=1): [100.0, 100.0, 100.0],
        TS + dt.timedelta(minutes=2): [0.0, 0.0, 0.0],
    }


@pytest.mark.parametrize("first_secondary", [False, True], ids=["restart", "roll"])
async def test_navazani_nevzkrisi_stranu_vracenou_na_nulu(
    tmp_path: Path, first_secondary: bool
) -> None:
    """#1582: restart i roll naváží stranu vrácenou na 0 jako 0, ne s poslední nenulovou hodnotou.

    Roll: partici expirace psal přes den sekundár, po settle ji jako aktivní
    převezme nový runtime a naváže z ní (`__main__` roll v settle).
    """
    first_runtime, _publisher, _settings = build_runtime(tmp_path, ReversingVolumeStreamer())
    first_runtime.secondary = first_secondary
    for minute in range(3):
        await first_runtime.run_cycle(TS + dt.timedelta(minutes=minute), SPOT, [])

    second_runtime, _publisher, settings = build_runtime(tmp_path, RisingVolumeStreamer())
    restart = TS + dt.timedelta(minutes=3)
    await second_runtime.run_cycle(restart, SPOT, [])

    tracker = second_runtime.cum_delta
    assert tracker is not None
    # Všech 6 stran pásma navázáno jako naměřená nula (dřív cally se 100)
    assert len(tracker.net_volumes()) == 6
    assert set(tracker.net_volumes().values()) == {0.0}
    netflow = pd.read_parquet(
        settings.derived_dir / "ES" / "20260716" / "netflow" / f"{TS.date().isoformat()}.parquet"
    )
    assert set(netflow[netflow["ts_min"] == restart]["net_volume"]) == {0.0}


def _seed_runtime(
    tmp_path: Path, source: str, streamer: MockQuoteStreamer | None = None
) -> tuple[EngineRuntime, Settings]:
    """Runtime nad čerstvým data adresářem se zvoleným zdrojem trackeru (v5 / v6 explicitně)."""
    engine_runtime, _publisher, settings = build_runtime(tmp_path, streamer or MockQuoteStreamer())
    engine_runtime.cum_delta = CumDeltaTracker(multiplier=50.0, source=source)
    return engine_runtime, settings


def _spec(strike: float, right: str) -> OptionContractSpec:
    return OptionContractSpec("ES", "FOP", "20260716", strike, right, "CME", "E3D", "50")


# Pauza Globexu 16:30 CT (CDT) — ještě seance 16. 7., ve které engine do 16:00 CT psal midpoint
PAUSE = dt.datetime(2026, 7, 16, 21, 30, tzinfo=dt.UTC)


def _write_session_history(settings: Settings) -> None:
    """Flow a netflow seance 16. 7.: midpoint do 15:59 CT, pak řádky v6 včetně mezery."""
    writer = SnapshotWriter(settings)
    before = PAUSE - dt.timedelta(minutes=40)  # 20:50 UTC, midpoint
    writer.write_flow(
        "ES",
        TS.date(),
        [
            FlowRow(before, 2.0, 500.0, source=SOURCE_MIDPOINT),
            FlowRow(before + dt.timedelta(minutes=1), 1.0, 30.0, source=SOURCE_AGGRESSOR),
            FlowRow(before + dt.timedelta(minutes=2), None, None, source=SOURCE_AGGRESSOR),
        ],
    )
    writer.write_netflow(
        "ES",
        "20260716",
        TS.date(),
        [
            NetFlowRow(before, 7600.0, "C", 120.0, source=SOURCE_MIDPOINT),
            NetFlowRow(before + dt.timedelta(minutes=1), 7600.0, "C", 9.0, source=SOURCE_AGGRESSOR),
        ],
    )


@pytest.mark.parametrize(
    ("source", "cum", "net"),
    [(SOURCE_MIDPOINT, 500.0, 120.0), (SOURCE_AGGRESSOR, 30.0, 9.0)],
)
async def test_seed_navaze_posledni_mereny_radek_zdroje_trackeru(
    tmp_path: Path, source: str, cum: float, net: float
) -> None:
    """#1460: restart naváže CumΔ i net z posledního ne-NULL řádku TÉHOŽ zdroje."""
    engine_runtime, settings = _seed_runtime(tmp_path, source)
    _write_session_history(settings)

    await engine_runtime.run_cycle(PAUSE, SPOT, [])

    tracker = engine_runtime.cum_delta
    assert tracker is not None
    assert tracker.cum_delta == cum  # mezera (NULL) se přeskočí, jiný zdroj taky
    assert tracker.net_volumes() == {_spec(7600.0, "C"): net}


async def test_nasazeni_v6_v_pauze_nenavaze_midpoint_uroven(tmp_path: Path) -> None:
    """#1460: v6 nasazená v pauze (seance D trvá do 17:00 CT) začne od nuly, ne od midpointu."""
    engine_runtime, settings = _seed_runtime(tmp_path, SOURCE_AGGRESSOR)
    before = PAUSE - dt.timedelta(minutes=40)
    SnapshotWriter(settings).write_flow(
        "ES", TS.date(), [FlowRow(before, 2.0, 500.0, source=SOURCE_MIDPOINT)]
    )
    # Partice netflow ze starého image: bez sloupce `source` (NULL = midpoint)
    netflow_dir = settings.derived_dir / "ES" / "20260716" / "netflow"
    netflow_dir.mkdir(parents=True)
    pq.write_table(
        pa.table(
            {
                "ts_min": pa.array([before], pa.timestamp("us", tz="UTC")),
                "strike": [7600.0],
                "right": ["C"],
                "net_volume": [120.0],
            }
        ),
        netflow_dir / f"{TS.date().isoformat()}.parquet",
    )

    await engine_runtime.run_cycle(PAUSE, SPOT, [])

    tracker = engine_runtime.cum_delta
    assert tracker is not None
    assert tracker.cum_delta == 0.0
    assert tracker.net_volumes() == {}


async def test_netflow_seed_obnovi_i_stranu_mimo_pasmo(tmp_path: Path) -> None:
    """#1460: po rollu má nový aktivní runtime užší pásmo — základ strany mimo něj se neztratí.

    Strana 7620 C se po rozšíření pásma vrátí se základem 30 a tok k němu
    přibývá; dřív by začala od nuly a řádek netflow by kumulativ přepsal.
    """
    engine_runtime, settings = _seed_runtime(tmp_path, SOURCE_MIDPOINT, RisingVolumeStreamer())
    SnapshotWriter(settings).write_netflow(
        "ES",
        "20260716",
        TS.date(),
        [
            NetFlowRow(TS - dt.timedelta(minutes=5), 7600.0, "C", 100.0, SOURCE_MIDPOINT),
            NetFlowRow(TS - dt.timedelta(minutes=5), 7620.0, "C", 30.0, SOURCE_MIDPOINT),
        ],
    )

    await engine_runtime.run_cycle(TS, SPOT, [])
    tracker = engine_runtime.cum_delta
    assert tracker is not None
    assert tracker.net_volume(_spec(7620.0, "C")) == 30.0

    # Rozšíření pásma o 7620: kontrakt postavený jako z build_contracts je tentýž klíč
    engine_runtime.contracts = [*engine_runtime.contracts, _spec(7620.0, "C"), _spec(7620.0, "P")]
    await engine_runtime.run_cycle(TS + dt.timedelta(minutes=1), SPOT, [])  # první bar 7620
    await engine_runtime.run_cycle(TS + dt.timedelta(minutes=2), SPOT, [])  # +100 buy

    netflow = pd.read_parquet(
        settings.derived_dir / "ES" / "20260716" / "netflow" / f"{TS.date().isoformat()}.parquet"
    )
    last = netflow[netflow["ts_min"] == TS + dt.timedelta(minutes=2)]
    side = last[(last["strike"] == 7620.0) & (last["right"] == "C")]
    assert list(side["net_volume"]) == [130.0]


def _sample(tracker: CumDeltaTracker, *, running: bool, market_closed: bool = False) -> None:
    """Vzorek orchestrátoru před cyklem: stav trade větve a řez tisků (#1460)."""
    tracker.observe(running=running, market_closed=market_closed)
    tracker.seal()


def _prints_runtime(
    tmp_path: Path, *, secondary: bool = False
) -> tuple[EngineRuntime, RecordingPublisher, Settings, CumDeltaTracker]:
    """Runtime se zdrojem tisků (v6 explicitním parametrem, #1460) nad konstantním objemem."""
    engine_runtime, publisher, settings = build_runtime(tmp_path, MockQuoteStreamer())
    tracker = CumDeltaTracker(multiplier=50.0, source=SOURCE_AGGRESSOR)
    engine_runtime.cum_delta = tracker
    engine_runtime.secondary = secondary
    return engine_runtime, publisher, settings, tracker


async def test_v6_mezera_je_null_bez_netflow_fa_jede_z_objemu(tmp_path: Path) -> None:
    """ADR-0057 bod 3–4: minuta s mezerou má ve flow NULL a netflow nepíše.

    FA pod v6 mezera trade větve nezastaví (doplněk ADR-0061, Rozhodnuto 24. 10.):
    odhad bere objem IBKR, ne tisky — píše se v obou minutách.
    """
    engine_runtime, publisher, settings, tracker = _prints_runtime(tmp_path)
    engine_runtime.flow_alpha = 0.4  # kalibrovaný ρ z objemu — bez něj FA pod v6 nejede
    c = _spec(7600.0, "C")
    tracker.add_dx_trade(c, 3.0, "BUY", 0.5)  # tisk před prvním cyklem: částečná minuta
    _sample(tracker, running=True)
    await engine_runtime.run_cycle(TS, SPOT, [])  # restart za otevřeného trhu → mezera
    tracker.add_dx_trade(c, 2.0, "BUY", 0.5)  # +50
    _sample(tracker, running=True)
    await engine_runtime.run_cycle(TS + dt.timedelta(minutes=1), SPOT, [])

    day = TS.date().isoformat()
    measured = TS + dt.timedelta(minutes=1)
    flow = pd.read_parquet(settings.derived_dir / "ES" / "flow" / f"{day}.parquet")
    flow = flow.sort_values("ts_min")
    assert flow["flow_delta"].isna().tolist() == [True, False]
    assert flow["cum_delta"].isna().tolist() == [True, False]
    assert flow["cum_delta"].iloc[1] == 50.0  # tisky mezery do CumΔ nejdou
    flows = [data for channel, data in publisher.messages if channel == "flow.ES"]
    assert (flows[0]["flow_delta"], flows[0]["cum_delta"]) == (None, None)
    base = settings.derived_dir / "ES" / "20260716"
    netflow = pd.read_parquet(base / "netflow" / f"{day}.parquet")
    assert netflow["ts_min"].tolist() == [measured]
    assert netflow[["strike", "right", "net_volume"]].values.tolist() == [[7600.0, "C", 2.0]]
    # Značka mezery pro kalibraci α (ADR-0057 bod 6): jen minuta bez měření
    netgap = pd.read_parquet(base / "netgap" / f"{day}.parquet")
    assert netgap["ts_min"].tolist() == [TS]
    # oiest má pod v6 řádek každé strany s objemem (ADR-0061), i v minutě s mezerou
    for series in ("levelsfa", "gexprofilefa", "oiest"):
        frame = pd.read_parquet(base / series / f"{day}.parquet")
        assert sorted(frame["ts_min"].unique().tolist()) == [TS, measured], series
    channels = [channel for channel, _ in publisher.messages]
    assert channels.count("levelsfa.ES.20260716") == 2


async def test_v6_fa_odhad_z_objemu_seance_zveda_i_stranu_prodeje(tmp_path: Path) -> None:
    """ADR-0061 bod 1: pod tisky OI_est = OI + ρ·V, V = maximum kumulativu seance.

    Call 7600 má čistý prodej z tisků (net −5) a objem roste — odhad OI zvedá.
    Model α·net by ho snížil (#1612: OI roste i na stranách s převahou prodeje).
    """
    engine_runtime, _publisher, settings = build_runtime(tmp_path, RisingVolumeStreamer())
    tracker = CumDeltaTracker(multiplier=50.0, source=SOURCE_AGGRESSOR)
    engine_runtime.cum_delta = tracker
    engine_runtime.flow_alpha = 0.25  # ρ kalibrovaný z objemu
    c = _spec(7600.0, "C")
    _sample(tracker, running=True)
    await engine_runtime.run_cycle(TS, SPOT, [])  # start za otevřeného trhu = mezera, objem 100
    tracker.add_dx_trade(c, 5.0, "SELL", 0.5)
    _sample(tracker, running=True)
    measured = TS + dt.timedelta(minutes=1)
    await engine_runtime.run_cycle(measured, SPOT, [])  # objem 200

    assert tracker.net_volume(c) == -5.0
    assert tracker.session_volume(c) == 200.0
    day = TS.date().isoformat()
    oiest = pd.read_parquet(settings.derived_dir / "ES" / "20260716" / "oiest" / f"{day}.parquet")
    # Všech 6 stran: 1000 + 0,25 · V — znaménko toku do odhadu nevstupuje; minuta
    # s mezerou trade větve FA píše taky (V je objem IBKR, doplněk ADR-0061)
    by_minute = oiest.groupby("ts_min")["oi_est"].apply(set).to_dict()
    assert by_minute == {TS: {1025.0}, measured: {1050.0}}


async def test_v6_zavreny_trh_zapise_nulu_ne_mezeru(tmp_path: Path) -> None:
    """ADR-0057 bod 3: při zavřeném trhu mezera nevzniká, ani hned po startu enginu."""
    engine_runtime, _publisher, settings, tracker = _prints_runtime(tmp_path)
    _sample(tracker, running=False, market_closed=True)  # pauza Globexu, tasty odpojené
    await engine_runtime.run_cycle(PAUSE, SPOT, [])

    flow = pd.read_parquet(settings.derived_dir / "ES" / "flow" / f"{PAUSE.date()}.parquet")
    assert flow[["flow_delta", "cum_delta"]].values.tolist() == [[0.0, 0.0]]
    assert not (settings.derived_dir / "ES" / "20260716" / "netgap").exists()


async def test_v6_sekundar_uzavre_minutu_a_vrati_net_mezery(tmp_path: Path) -> None:
    """#1460: sekundár (vstup kalibrace α) uzavírá minutu — net mezery se vrátí, flow nepíše."""
    engine_runtime, _publisher, settings, tracker = _prints_runtime(tmp_path, secondary=True)
    c = _spec(7600.0, "C")
    tracker.add_dx_trade(c, 3.0, "BUY", 0.5)
    _sample(tracker, running=True)
    await engine_runtime.run_cycle(TS, SPOT, [])  # mezera (start za otevřeného trhu)
    assert tracker.net_volumes() == {c: 0.0}
    tracker.add_dx_trade(c, 2.0, "SELL", 0.5)
    for _minute in range(3):  # cyklus sekundáru po 3 minutách, vzorek každou minutu
        _sample(tracker, running=True)
    await engine_runtime.run_cycle(TS + dt.timedelta(minutes=3), SPOT, [])

    assert tracker.net_volumes() == {c: -2.0}
    assert tracker.cum_delta == -50.0
    day = TS.date().isoformat()
    netflow = pd.read_parquet(
        settings.derived_dir / "ES" / "20260716" / "netflow" / f"{day}.parquet"
    )
    assert netflow["ts_min"].tolist() == [TS + dt.timedelta(minutes=3)]
    assert netflow["net_volume"].tolist() == [-2.0]
    assert not (settings.derived_dir / "ES" / "flow" / f"{day}.parquet").exists()
    # Sekundár flow nepíše — mezeru pro kalibraci α nese jen značka netgap (#1460)
    netgap = pd.read_parquet(settings.derived_dir / "ES" / "20260716" / "netgap" / f"{day}.parquet")
    assert netgap["ts_min"].tolist() == [TS]


def _netflow_at(settings: Settings, ts: dt.datetime) -> list[list[object]]:
    netflow = pd.read_parquet(
        settings.derived_dir / "ES" / "20260716" / "netflow" / f"{ts.date().isoformat()}.parquet"
    )
    rows = netflow[netflow["ts_min"] == ts]
    values: list[list[object]] = rows[["strike", "right", "net_volume"]].values.tolist()
    return values


async def test_v6_roll_predani_sekundaru_net_a_tisky_bez_mezery(tmp_path: Path) -> None:
    """Varianta C (#1460): nový aktivní runtime převezme tracker sekundáru téže expirace.

    Net navazuje i s tisky mezi posledním zápisem sekundáru a novou pipeline
    (bez druhého seedu z partice), první minuta je měřená a CumΔ symbolu
    navazuje z flow partice aktivního řetězu, ne z kumulativu sekundáru.
    """
    secondary, _pub, settings, tracker = _prints_runtime(tmp_path, secondary=True)
    c = _spec(7600.0, "C")
    tracker.add_dx_trade(c, 3.0, "BUY", 0.5)
    _sample(tracker, running=True, market_closed=True)  # hranice prvního okna známá
    await secondary.run_cycle(TS, SPOT, [])  # zápis sekundáru K
    tracker.add_dx_trade(c, 2.0, "BUY", 0.5)
    _sample(tracker, running=True)
    tracker.add_dx_trade(c, 1.0, "BUY", 0.5)
    _sample(tracker, running=True)  # vzorek minuty rollu = hranice
    tracker.add_dx_trade(c, 4.0, "SELL", 0.5)  # po zastavení staré pipeline
    roll = TS + dt.timedelta(minutes=2)
    # Poslední řádek aktivního řetězu D v minutě rollu
    SnapshotWriter(settings).write_flow(
        "ES", TS.date(), [FlowRow(roll, 10.0, 300.0, source=SOURCE_AGGRESSOR)]
    )

    active, _pub2, _settings2, _fresh = _prints_runtime(tmp_path)
    active.take_over(secondary)
    assert active.cum_delta is tracker
    tracker.add_dx_trade(c, 5.0, "BUY", 0.5)  # po založení nové pipeline
    _sample(tracker, running=True)
    await active.run_cycle(roll + dt.timedelta(minutes=1), SPOT, [])

    assert _netflow_at(settings, TS) == [[7600.0, "C", 3.0]]
    assert _netflow_at(settings, roll + dt.timedelta(minutes=1)) == [[7600.0, "C", 7.0]]
    assert not (settings.derived_dir / "ES" / "20260716" / "netgap").exists()
    flow = pd.read_parquet(settings.derived_dir / "ES" / "flow" / f"{TS.date()}.parquet")
    first = flow[flow["ts_min"] == roll + dt.timedelta(minutes=1)]
    # Tok jen po hranici (−4 + 5 kontraktů × Δ 0,5 × 50), CumΔ od řádku D
    assert first[["flow_delta", "cum_delta"]].values.tolist() == [[25.0, 325.0]]


async def test_v6_roll_predani_sekundaru_bez_cyklu_navaze_net_z_partice(tmp_path: Path) -> None:
    """Sekundár založený těsně před settle neproběhl cyklem: seed z partice přejde s ním."""
    SnapshotWriter(Settings(data_dir=tmp_path / "data")).write_netflow(
        "ES",
        "20260716",
        TS.date(),
        [NetFlowRow(TS - dt.timedelta(minutes=30), 7600.0, "C", 100.0, SOURCE_AGGRESSOR)],
    )
    secondary, _pub, settings, tracker = _prints_runtime(tmp_path, secondary=True)
    c = _spec(7600.0, "C")
    _sample(tracker, running=True, market_closed=True)
    tracker.add_dx_trade(c, 2.0, "BUY", 0.5)
    active, _pub2, _settings2, _fresh = _prints_runtime(tmp_path)
    active.take_over(secondary)
    _sample(tracker, running=True)
    await active.run_cycle(TS, SPOT, [])

    assert _netflow_at(settings, TS) == [[7600.0, "C", 102.0]]


async def test_take_over_jen_pod_tisky_a_nad_tymz_retezem(tmp_path: Path) -> None:
    active, _pub, _settings, _tracker = _prints_runtime(tmp_path)
    midpoint, _pub2, _settings2 = build_runtime(tmp_path, MockQuoteStreamer())
    midpoint.secondary = True
    with pytest.raises(ValueError, match="jen pod zdrojem tisků"):
        active.take_over(midpoint)
    other, _pub3, _settings3, _tracker3 = _prints_runtime(tmp_path, secondary=True)
    other.expiry = "20260717"
    with pytest.raises(ValueError, match="není řetěz"):
        active.take_over(other)


@pytest.mark.parametrize("mechanics", ["v5", "v6"])
async def test_flow_alpha_zero_disables_levelsfa(tmp_path: Path, mechanics: str) -> None:
    """ADR-0011: α = 0 vypíná odhad (oiest, levelsfa, FA Dyn GEX) — žádná řada, žádný kanál.

    Netflow je měřené a píše se dál (#1172) — dřív visel pod `alpha > 0`
    a kalibrace, která α srazila na 0, si zastavila vlastní vstup. Dva cykly
    s rostoucím objemem, ať je co zapsat: v5 klasifikuje bary midpointem, v6
    (zdroj tisků explicitně, #1460) nese net jen z tisků měřené minuty.
    """
    engine_runtime, publisher, settings = build_runtime(
        tmp_path, RisingVolumeStreamer(), flow_oi_alpha=0.0
    )
    prints: CumDeltaTracker | None = None
    if mechanics == "v6":
        prints = CumDeltaTracker(multiplier=50.0, source=SOURCE_AGGRESSOR)
        engine_runtime.cum_delta = prints
        prints.observe(running=False, market_closed=True)  # start v pauze: hranice známá
    for minute in range(2):
        if prints is not None:
            if minute == 1:
                prints.add_dx_trade(_spec(7600.0, "C"), 3.0, "BUY", 0.5)
            _sample(prints, running=True)
        await engine_runtime.run_cycle(TS + dt.timedelta(minutes=minute), SPOT, [])

    day = TS.date().isoformat()
    base = settings.derived_dir / "ES" / "20260716"
    netflow = pd.read_parquet(base / "netflow" / f"{day}.parquet")
    second = TS + dt.timedelta(minutes=1)
    assert set(netflow["ts_min"]) == {second}
    rows = netflow[["strike", "right", "net_volume"]].values.tolist()
    if mechanics == "v5":
        # Call strany +100 (last nad midem), put strany naměřená nula (#1582)
        assert sorted(rows) == sorted(
            [[k, "C", 100.0] for k in (7590.0, 7600.0, 7610.0)]
            + [[k, "P", 0.0] for k in (7590.0, 7600.0, 7610.0)]
        )
    else:
        assert rows == [[7600.0, "C", 3.0]]
    channels = [channel for channel, _ in publisher.messages]
    for series in ("levelsfa", "oiest", "gexprofilefa", "gexfieldfa"):
        assert not (base / series / f"{day}.parquet").exists(), series
        assert f"{series}.ES.20260716" not in channels, series


async def test_v6_bez_kalibrace_fa_nejede_ani_s_vychozi_alfou(tmp_path: Path) -> None:
    """Nález kontroly 1b: pod tisky bez `flow_alpha` (start, chyba DB) platí α 0, ne 0,4."""
    engine_runtime, publisher, settings = build_runtime(tmp_path, RisingVolumeStreamer())
    assert settings.flow_oi_alpha > 0
    prints = CumDeltaTracker(multiplier=50.0, source=SOURCE_AGGRESSOR)
    engine_runtime.cum_delta = prints
    assert engine_runtime.flow_alpha is None and engine_runtime.fa_alpha() == 0.0
    prints.observe(running=False, market_closed=True)  # start v pauze: hranice známá
    for minute in range(2):
        if minute == 1:
            prints.add_dx_trade(_spec(7600.0, "C"), 3.0, "BUY", 0.5)
        _sample(prints, running=True)
        await engine_runtime.run_cycle(TS + dt.timedelta(minutes=minute), SPOT, [])

    day = TS.date().isoformat()
    base = settings.derived_dir / "ES" / "20260716"
    assert (base / "netflow" / f"{day}.parquet").exists()  # měřená data se píší dál
    for series in ("levelsfa", "oiest", "gexprofilefa", "gexfieldfa"):
        assert not (base / series / f"{day}.parquet").exists(), series
    # Pod v5 zůstává výchozí α z konfigurace
    (tmp_path / "v5").mkdir()
    v5_runtime, _publisher, v5_settings = build_runtime(tmp_path / "v5", RisingVolumeStreamer())
    assert v5_runtime.fa_alpha() == v5_settings.flow_oi_alpha


async def test_forming_bar_published_and_written_as_provisional(
    runtime: tuple[EngineRuntime, RecordingPublisher, Settings],
) -> None:
    """ADR-0005: rozdělaná minuta má svíčku hned, ne až po dalším cyklu."""
    engine_runtime, publisher, settings = runtime
    # Cyklus minuty TS: uzavřený bar patří PŘEDCHOZÍ minutě, rozdělaný té aktuální
    closed = Bar(ts=TS - dt.timedelta(minutes=1), open=7590.0, high=7595.0, low=7589.0,
                 close=7594.0, volume=800.0)  # fmt: skip
    forming = Bar(ts=TS, open=7594.0, high=7602.0, low=7593.0, close=SPOT, volume=310.0)

    await engine_runtime.run_cycle(TS, SPOT, [closed], forming)

    prices = [data for channel, data in publisher.messages if channel == "price.ES"]
    assert len(prices) == 2
    assert prices[0]["ts"] == (TS - dt.timedelta(minutes=1)).isoformat()
    assert prices[0]["final"] is True
    assert prices[1]["ts"] == TS.isoformat()
    assert prices[1]["final"] is False
    assert prices[1]["close"] == SPOT

    # Obě minuty jsou i v partici, aby je dostal REST balík po refreshi
    day = TS.date().isoformat()
    bars = pd.read_parquet(settings.derived_dir / "ES" / "bars" / f"{day}.parquet")
    assert len(bars) == 2
    assert list(bars.sort_values("ts_min")["close"]) == [7594.0, SPOT]


async def test_final_bar_replaces_provisional_without_duplicate(
    runtime: tuple[EngineRuntime, RecordingPublisher, Settings],
) -> None:
    """ADR-0005: upsert podle ts_min — jedna minuta = jeden řádek."""
    engine_runtime, _publisher, settings = runtime
    provisional = Bar(ts=TS, open=7594.0, high=7602.0, low=7593.0, close=7598.0, volume=310.0)
    await engine_runtime.run_cycle(TS, SPOT, [], provisional)

    day = TS.date().isoformat()
    path = settings.derived_dir / "ES" / "bars" / f"{day}.parquet"
    assert len(pd.read_parquet(path)) == 1

    # Další cyklus doručí finální bar téže minuty + rozdělanou další minutu
    final = Bar(ts=TS, open=7594.0, high=7605.0, low=7590.0, close=7601.0, volume=1250.0)
    next_forming = Bar(
        ts=TS + dt.timedelta(minutes=1), open=7601.0, high=7603.0, low=7600.0,
        close=7602.0, volume=120.0,
    )  # fmt: skip
    await engine_runtime.run_cycle(TS + dt.timedelta(minutes=1), SPOT, [final], next_forming)

    bars = pd.read_parquet(path).sort_values("ts_min")
    assert len(bars) == 2  # žádný duplikát minuty TS
    assert list(bars["close"]) == [7601.0, 7602.0]  # provizorní nahrazen finálním
    assert list(bars["volume"]) == [1250.0, 120.0]


async def test_forming_bar_of_other_minute_is_ignored(
    runtime: tuple[EngineRuntime, RecordingPublisher, Settings],
) -> None:
    """ADR-0005: raději žádná svíčka než svíčka pod cizím časem."""
    engine_runtime, publisher, settings = runtime
    stale_forming = Bar(
        ts=TS - dt.timedelta(minutes=3), open=1.0, high=2.0, low=0.5, close=1.5, volume=9.0
    )
    await engine_runtime.run_cycle(TS, SPOT, [], stale_forming)

    assert [data for channel, data in publisher.messages if channel == "price.ES"] == []
    day = TS.date().isoformat()
    assert not (settings.derived_dir / "ES" / "bars" / f"{day}.parquet").exists()


async def test_second_cycle_appends_and_accumulates(
    runtime: tuple[EngineRuntime, RecordingPublisher, Settings],
) -> None:
    engine_runtime, _publisher, settings = runtime
    await engine_runtime.run_cycle(TS, SPOT, [])
    await engine_runtime.run_cycle(TS + dt.timedelta(minutes=1), SPOT + 5, [])

    day = TS.date().isoformat()
    snapshots = pd.read_parquet(settings.snapshots_dir / "ES" / "20260716" / f"{day}.parquet")
    assert len(snapshots) == 12  # dvě minuty × 6 kontraktů
    levels = pd.read_parquet(settings.derived_dir / "ES" / "20260716" / "levels" / f"{day}.parquet")
    assert len(levels) == 2


class PartialStreamer(MockQuoteStreamer):
    """Mock, kterému lze za běhu „zabít" konkrétní striky (simulace mrtvých Greeks)."""

    def __init__(self) -> None:
        super().__init__()
        self.dead: set[float] = set()

    async def fetch_quote(
        self, spec: OptionContractSpec, timeout_s: float
    ) -> QuoteSnapshot | PartialQuote | None:
        if spec.strike in self.dead:
            return None
        return await super().fetch_quote(spec, timeout_s)


async def test_expired_quote_stays_in_snapshot_but_leaves_computations(tmp_path: Path) -> None:
    """#306: zmrzlá kotace se zapíše se svým stářím, ale nesmí do GEX ani úrovní.

    27. 7. servírovala cache 15 h staré ATM Greeks a zdi, flip i Max Pain se z nich
    celý den počítaly, aniž by to šlo poznat. Chybějící strike je poctivější.
    """
    settings = Settings(data_dir=tmp_path / "data", quote_max_age_s=60.0)
    specs = contracts()
    repository = OIEodRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'db.sqlite'}"))
    repository.ensure_schema()
    # Asymetrické OI, ať je NetGEX nenulový a výpadek strike se v něm projeví
    repository.upsert_many(
        [
            OIRecord(
                "ES", "20260716", s.strike, s.right, TS.date(), 1000.0 if s.right == "C" else 300.0
            )
            for s in specs
        ]
    )
    streamer = PartialStreamer()
    scheduler = SubscriptionScheduler(streamer, settings)
    engine_runtime = EngineRuntime(
        settings=settings,
        scheduler=scheduler,
        writer=SnapshotWriter(settings),
        oi_repository=repository,
        publisher=RecordingPublisher(),
        symbol="ES",
        expiry="20260716",
        multiplier=50.0,
        contracts=specs,
        # Midpoint explicitně (#1460): bump v6 přepne jen `LIVE_SOURCE`, test zůstává v5
        cum_delta=CumDeltaTracker(multiplier=50.0, source=SOURCE_MIDPOINT),
    )
    await engine_runtime.run_cycle(TS, SPOT, [])

    # TWS přestane pro strike 7600 dodávat Greeks; cache drží poslední kotaci,
    # která mezitím zestárne hodinu (vzor 27. 7. — 15 h zmrzlé ATM striky)
    streamer.dead = {7600.0}
    for spec in (s for s in specs if s.strike == 7600.0):
        cached = scheduler.quote(spec)
        assert cached is not None
        cached.updated_at -= 3600.0

    await engine_runtime.run_cycle(TS + dt.timedelta(minutes=1), SPOT, [])

    day_dir = settings.data_dir / "snapshots" / "ES" / "20260716"
    frame = pd.concat([pd.read_parquet(p) for p in day_dir.rglob("*.parquet")])
    last = frame[frame.ts_min == frame.ts_min.max()]
    # Řádky zůstávají — jen se skutečným stářím, ne sentinelem 0/999
    assert set(last.strike.unique()) == {7590.0, 7600.0, 7610.0}
    assert (last[last.strike == 7600.0].stale_age > 3600.0).all()
    assert (last[last.strike == 7590.0].stale_age < 60.0).all()

    # …ale do GEX/úrovní zmrzlý strike nevstoupil
    levels_dir = settings.data_dir / "derived" / "ES" / "20260716" / "levels"
    levels = pd.concat([pd.read_parquet(p) for p in levels_dir.rglob("*.parquet")])
    assert len(levels) == 2
    fresh_gex, stale_gex = levels.sort_values("ts_min").total_gex.tolist()
    assert stale_gex != pytest.approx(fresh_gex)


def _tasty_chain(specs: list[OptionContractSpec]) -> dict[OptionContractSpec, CachedQuote]:
    """Fallback řetězu z tasty (#614): kotace a greeks bez kumulativního objemu a `last`."""
    return {
        spec: CachedQuote(
            snapshot=QuoteSnapshot(
                bid=11.0,
                ask=11.5,
                last=None,
                volume=None,
                iv=0.2,
                delta=0.4,
                gamma=0.0042,
                theta=-1.0,
                vega=0.8,
            ),
            updated_at=time.monotonic(),
            feed=FEED_TASTY,
        )
        for spec in specs
    }


@pytest.mark.parametrize(
    ("mechanics", "running"),
    [("v5", True), ("v6", True), ("v6", False)],
    ids=["v5", "v6-merena", "v6-mezera"],
)
async def test_fallback_retezu_prebira_kotace_cumdelta_podle_mechaniky(
    runtime: tuple[EngineRuntime, RecordingPublisher, Settings], mechanics: str, running: bool
) -> None:
    """#614 fáze 2b: při výpadku IBKR staví cyklus profil z tasty kotací.

    Druhá polovina zadání podle mechaniky (#1460): pod v5 (midpoint) se CumΔ
    a net objem za fallbacku NEKRMÍ — tasty denní objem v sémantice IBKR
    nedodává, takže dosadit nulu by znamenalo skokový záporný přírůstek přes
    celý řetěz. Od v6 (zdroj tisků explicitně, ADR-0057 bod 3) jdou z tisků
    dál a chybí jen rozklad objemu (printvol); bez trade větve je minuta mezera.
    """
    engine_runtime, _publisher, settings = runtime
    if mechanics == "v6":
        tracker = CumDeltaTracker(multiplier=50.0, source=SOURCE_AGGRESSOR)
        engine_runtime.cum_delta = tracker
        tracker.observe(running=False, market_closed=True)  # start v pauze: hranice známá
        tracker.add_dx_trade(_spec(7600.0, "C"), 4.0, "BUY", 0.5)  # +4 × 0.5 × 50 = +100
        _sample(tracker, running=running)
    substitute = _tasty_chain(list(engine_runtime.contracts))
    engine_runtime.chain_fallback = lambda _specs: substitute

    await engine_runtime.run_cycle(TS, SPOT, [])

    day = TS.date().isoformat()
    snapshots = pd.read_parquet(settings.snapshots_dir / "ES" / "20260716" / f"{day}.parquet")
    # Gamma z tasty se propsala do snímku, ne hodnota z mock IBKR streameru
    assert set(snapshots["gamma"].unique()) == {0.0042}
    # Objem ani poslední cena se nevymýšlejí — díra, kterou je vidět (#465)
    assert snapshots["volume"].isna().all()
    assert snapshots["last"].isna().all()
    flow_row = engine_runtime.last_flow
    assert flow_row is not None
    base = settings.derived_dir / "ES" / "20260716"
    # Bez kumulativního objemu rozklad na tisky nevzniká v žádné mechanice
    assert not (base / "printvol" / f"{day}.parquet").exists()
    netflow_path = base / "netflow" / f"{day}.parquet"
    if mechanics == "v5":
        # A CumΔ zůstal netknutý: tracker žádný bar nedostal
        assert flow_row.cum_delta == 0.0
        assert not netflow_path.exists()
    elif running:
        # Tisky tastytrade nesou CumΔ i net objem dál (výpadek IBKR tok nezastaví)
        assert (flow_row.flow_delta, flow_row.cum_delta) == (100.0, 100.0)
        netflow = pd.read_parquet(netflow_path)
        assert netflow[["strike", "right", "net_volume"]].values.tolist() == [[7600.0, "C", 4.0]]
    else:
        # Trade větev neběžela: mezera, ne nula ani poslední hodnota
        assert (flow_row.flow_delta, flow_row.cum_delta) == (None, None)
        assert not netflow_path.exists()


@pytest.mark.parametrize("mechanics", ["v5", "v6"])
async def test_fa_pod_v6_jen_v_minute_s_objemem_ibkr(tmp_path: Path, mechanics: str) -> None:
    """ADR-0057 bod 4, doplněk ADR-0061 (Rozhodnuto 24. 10., brána B).

    Minuta 1: start za otevřeného trhu (mezera trade větve), objem IBKR je →
    FA pod v6 jede. Minuta 2: fallback řetězu na tasty (`volume` NULL), trade
    větev běží → pod v6 mezera místo zmrzlého V. Minuta 3: IBKR zpět → FA
    z maxima seance. Pod v5 brána není (beze změny).
    """
    engine_runtime, _publisher, settings = build_runtime(tmp_path, RisingVolumeStreamer())
    engine_runtime.flow_alpha = 0.25
    tracker = CumDeltaTracker(
        multiplier=50.0, source=SOURCE_AGGRESSOR if mechanics == "v6" else SOURCE_MIDPOINT
    )
    engine_runtime.cum_delta = tracker
    substitute = _tasty_chain(list(engine_runtime.contracts))
    minutes = [TS + dt.timedelta(minutes=offset) for offset in range(3)]
    for minute in minutes:
        fallback = minute == minutes[1]
        engine_runtime.chain_fallback = (lambda _specs: substitute) if fallback else None
        _sample(tracker, running=True)
        await engine_runtime.run_cycle(minute, SPOT, [])

    base = settings.derived_dir / "ES" / "20260716"
    day = TS.date().isoformat()
    expected = minutes if mechanics == "v5" else [minutes[0], minutes[2]]
    for series in ("levelsfa", "gexprofilefa"):
        frame = pd.read_parquet(base / series / f"{day}.parquet")
        assert sorted(frame["ts_min"].unique().tolist()) == expected, series
    if mechanics == "v6":
        oiest = pd.read_parquet(base / "oiest" / f"{day}.parquet")
        last = oiest[oiest["ts_min"] == minutes[2]]
        c = _spec(7600.0, "C")
        side = last[(last["strike"] == c.strike) & (last["right"] == c.right)]
        assert side["oi_est"].tolist() == [1000.0 + 0.25 * tracker.session_volume(c)]


class DyingVolumeStreamer(RisingVolumeStreamer):
    """`RisingVolumeStreamer` se „zabitými" striky: objem burzy roste dál, kotace nepřijde."""

    def __init__(self) -> None:
        super().__init__()
        self.dead: set[float] = set()

    async def fetch_quote(
        self, spec: OptionContractSpec, timeout_s: float
    ) -> QuoteSnapshot | PartialQuote | None:
        quote = await super().fetch_quote(spec, timeout_s)
        return None if spec.strike in self.dead else quote


@pytest.mark.parametrize("cause", ["fallback", "stara-kotace"])
async def test_v6_bar_po_mezere_v_baru_jen_zalozi_stav(tmp_path: Path, cause: str) -> None:
    """#1460 AC: cyklus bez baru (fallback řetězu `volume=None`, kotace nad
    `quote_max_age_s`) za běžící trade větve. Tok za mezeru nesou tisky, návratový
    bar s objemem celé mezery do printvol ani pokrytí nejde — jen založí stav.
    """
    streamer = DyingVolumeStreamer()
    engine_runtime, _publisher, settings = build_runtime(tmp_path, streamer)
    tracker = CumDeltaTracker(multiplier=50.0, source=SOURCE_AGGRESSOR)
    engine_runtime.cum_delta = tracker
    tracker.observe(running=False, market_closed=True)  # start v pauze: hranice známá
    c = _spec(7600.0, "C")
    for minute in range(2):  # založení stavu, pak běžný přírůstek +100
        _sample(tracker, running=True)
        await engine_runtime.run_cycle(TS + dt.timedelta(minutes=minute), SPOT, [])

    if cause == "fallback":
        tasty = QuoteSnapshot(bid=10.0, ask=10.5, last=None, volume=None, iv=0.15,
                              delta=0.5, gamma=0.01, theta=-0.5, vega=1.2)  # fmt: skip
        engine_runtime.chain_fallback = lambda specs: {
            spec: CachedQuote(snapshot=tasty, updated_at=time.monotonic(), feed=FEED_TASTY)
            for spec in specs
        }
    else:
        streamer.dead = {7600.0}  # TWS strike nedodává, cache stárne nad práh
        for dead in (s for s in engine_runtime.contracts if s.strike == 7600.0):
            cached = engine_runtime.scheduler.quote(dead)
            assert cached is not None
            cached.updated_at -= 3600.0
    tracker.add_dx_trade(c, 3.0, "BUY", 0.5)  # tisk v mezeře baru: +75
    _sample(tracker, running=True)
    await engine_runtime.run_cycle(TS + dt.timedelta(minutes=2), SPOT, [])

    engine_runtime.chain_fallback = None
    streamer.dead = set()
    engine_runtime.scheduler.reset_repair_state()  # návrat strike bez backoffu
    for minute in (3, 4):  # návrat s přírůstkem celé mezery, pak zase běžný +100
        _sample(tracker, running=True)
        await engine_runtime.run_cycle(TS + dt.timedelta(minutes=minute), SPOT, [])

    day = TS.date().isoformat()
    printvol = pd.read_parquet(
        settings.derived_dir / "ES" / "20260716" / "printvol" / f"{day}.parquet"
    )
    side = printvol[(printvol["strike"] == 7600.0) & (printvol["right"] == "C")]
    assert side["ts_min"].tolist() == [TS + dt.timedelta(minutes=m) for m in (1, 4)]
    assert side["volume_delta"].tolist() == [100.0, 100.0]
    flow = pd.read_parquet(settings.derived_dir / "ES" / "flow" / f"{day}.parquet")
    flow = flow.set_index("ts_min")
    assert flow.loc[TS + dt.timedelta(minutes=2), "flow_delta"] == 75.0
    back = flow.loc[TS + dt.timedelta(minutes=3)]
    assert back["structured_volume"] == 0.0
    # Jen strany krmené i v mezeře (stará kotace jen u 7600) mají běžný bar bez tisku
    assert back["fallback_volume"] == (0.0 if cause == "fallback" else 400.0)
    assert tracker.net_volumes() == {c: 3.0}


async def test_v6_kridlo_z_cache_rozlozi_tisky_vsech_minut(tmp_path: Path) -> None:
    """#1460: křídlo sweepované každý 3. cyklus dostává mezi sweepy bar z cache
    s přírůstkem 0 — tisky těch minut rozloží až čerstvý bar s objemem všech tří
    minut (dřív printed 10, structured 90: tisky dřívějších minut připsané struktuře).
    """
    engine_runtime, _publisher, settings = build_runtime(
        tmp_path, RisingVolumeStreamer(), wings_sweep_every=3, atm_sweep_width=1
    )
    engine_runtime.contracts = [
        _spec(strike, right)
        for strike in (7580.0, 7590.0, 7600.0, 7610.0, 7620.0)
        for right in ("C", "P")
    ]
    tracker = CumDeltaTracker(multiplier=50.0, source=SOURCE_AGGRESSOR)
    engine_runtime.cum_delta = tracker
    tracker.observe(running=False, market_closed=True)  # start v pauze: hranice známá
    wing = _spec(7620.0, "C")
    for minute in range(7):
        if minute:
            tracker.add_dx_trade(wing, 10.0, "BUY", 0.05)  # tisk každou minutu
        _sample(tracker, running=True)
        await engine_runtime.run_cycle(TS + dt.timedelta(minutes=minute), SPOT, [])

    day = TS.date().isoformat()
    printvol = pd.read_parquet(
        settings.derived_dir / "ES" / "20260716" / "printvol" / f"{day}.parquet"
    )
    side = printvol[(printvol["strike"] == 7620.0) & (printvol["right"] == "C")]
    assert side["ts_min"].tolist() == [TS + dt.timedelta(minutes=m) for m in (3, 6)]
    assert side[["volume_delta", "printed", "structured"]].values.tolist() == [
        [100.0, 30.0, 70.0],
        [100.0, 30.0, 70.0],
    ]


async def test_po_navratu_na_ibkr_cyklus_zase_cte_sweep(
    runtime: tuple[EngineRuntime, RecordingPublisher, Settings],
) -> None:
    """Návrat nesmí vyžadovat restart — hook vrátí None a cyklus jede z IBKR."""
    engine_runtime, _publisher, settings = runtime
    engine_runtime.chain_fallback = lambda _specs: None

    await engine_runtime.run_cycle(TS, SPOT, [])

    day = TS.date().isoformat()
    snapshots = pd.read_parquet(settings.snapshots_dir / "ES" / "20260716" / f"{day}.parquet")
    # Mock streamer dodává objem; kdyby cyklus zůstal na fallbacku, byl by NULL
    assert snapshots["volume"].notna().any()


@pytest.mark.parametrize(
    ("now", "alerted"),
    [
        (dt.datetime(2026, 9, 26, 0, 20, tzinfo=dt.UTC), False),  # sobota po uzávěrce
        (dt.datetime(2026, 9, 27, 21, 30, tzinfo=dt.UTC), False),  # neděle před otevřením
        (dt.datetime(2026, 9, 23, 21, 30, tzinfo=dt.UTC), False),  # středa, denní pauza
        (dt.datetime(2026, 9, 27, 22, 0, tzinfo=dt.UTC), True),  # nedělní otevření 17:00 CDT
        (dt.datetime(2026, 9, 24, 15, 0, tzinfo=dt.UTC), True),  # čtvrtek v seanci
    ],
)
async def test_bs_fallback_hlidka_jen_pri_otevrenem_trhu(
    tmp_path: Path, now: dt.datetime, alerted: bool
) -> None:
    """#1309: `greeks_bs_fallback` (#877) neměl bránu zavřeného trhu — mimo
    seanci TWS model greeks nepočítá a hodinové připomínky i remediace
    (resubscribe/reconnect) by běžely celý víkend. Zavřený trh epizodu
    přeruší bez alertu; v seanci (i hned po nedělním otevření) beze změny."""
    from gexlens_engine.compute.bsfallback import MIN_DURATION_S, BsFallbackWatcher

    settings = Settings(data_dir=tmp_path / "data", greeks_fallback_sweeps=1)
    specs = [
        OptionContractSpec("ES", "FOP", "20260930", strike, right, "CME", "E5C", "50")
        for strike in (7590.0, 7600.0, 7610.0)
        for right in ("C", "P")
    ]
    repository = OIEodRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'db.sqlite'}"))
    repository.ensure_schema()
    publisher = RecordingPublisher()
    engine_runtime = EngineRuntime(
        settings=settings,
        # TWS model mlčí pro celý řetěz → 100 % striků na BS dopočtu
        scheduler=SubscriptionScheduler(
            MockQuoteStreamer(partial_greeks=set(specs)), settings, utc_now=lambda: now
        ),
        writer=SnapshotWriter(settings),
        oi_repository=repository,
        publisher=publisher,
        symbol="ES",
        expiry="20260930",
        multiplier=50.0,
        contracts=specs,
        # Midpoint explicitně (#1460): bump v6 přepne jen `LIVE_SOURCE`, test zůstává v5
        cum_delta=CumDeltaTracker(multiplier=50.0, source=SOURCE_MIDPOINT),
    )
    # Epizoda běží už dvakrát déle, než je práh alertu (např. od pátku)
    watcher = BsFallbackWatcher(symbol="ES")
    watcher.episode_started = time.monotonic() - 2 * MIN_DURATION_S
    engine_runtime._bs_watcher = watcher

    await engine_runtime.run_cycle(now, SPOT, [])

    kinds = [data["kind"] for channel, data in publisher.messages if channel == "alerts"]
    assert ("greeks_bs_fallback" in kinds) is alerted
    assert watcher.share == 1.0
    if not alerted:
        assert watcher.episode_started is None
        assert engine_runtime.bs_remediation_due(time.monotonic() + 10 * MIN_DURATION_S) is None


async def test_oi_zdi_prepocita_obnova_tasty_kridla(
    runtime: tuple[EngineRuntime, RecordingPublisher, Settings],
) -> None:
    """#1605: tasty řádek se po publikaci obnoví bez změny `captured_ts`.
    Cache na `captured_at` by zdi držela na předchozí publikaci."""
    engine_runtime, _publisher, _settings = runtime
    repository = engine_runtime.oi_repository
    day = TS.date()
    repository.upsert_many(
        [OIRecord("ES", "20260716", 7000.0, "P", day, 5000.0)],
        tasty_captured_ts=dt.datetime(2026, 7, 16, 0, 0, tzinfo=dt.UTC),
    )
    walls = await engine_runtime._oi_walls(day, SPOT)
    assert walls is not None and walls.put == 7000.0

    repository.upsert_many(
        [
            OIRecord("ES", "20260716", 7000.0, "P", day, 10.0),
            OIRecord("ES", "20260716", 6900.0, "P", day, 3000.0),
        ],
        tasty_captured_ts=dt.datetime(2026, 7, 16, 12, 5, tzinfo=dt.UTC),
    )
    walls = await engine_runtime._oi_walls(day, SPOT)

    assert walls is not None and walls.put == 6900.0
