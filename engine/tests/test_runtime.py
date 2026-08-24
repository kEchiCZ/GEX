"""Smoke test runtime (issue #30): jeden cyklus nad mocky vyprodukuje kompletní den."""

import datetime as dt
import time
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine

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
from gexlens_engine.storage.parquet_store import SnapshotWriter

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
    ]
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
    assert list(netflow.columns) == ["ts_min", "strike", "right", "net_volume"]
    # Jen druhá minuta má klasifikovaný přírůstek a jen call strany: 3 × +100
    assert len(netflow) == 3
    assert set(netflow["right"]) == {"C"}
    assert set(netflow["net_volume"]) == {100.0}

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


async def test_flow_alpha_zero_disables_levelsfa(tmp_path: Path) -> None:
    """ADR-0011: α = 0 flow-adjusted vrstvu vypíná — žádná řada, žádný kanál."""
    settings = Settings(data_dir=tmp_path / "data", flow_oi_alpha=0.0)
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
    )

    await engine_runtime.run_cycle(TS, SPOT, [])

    day = TS.date().isoformat()
    assert not (settings.derived_dir / "ES" / "20260716" / "levelsfa" / f"{day}.parquet").exists()
    channels = [channel for channel, _ in publisher.messages]
    assert "levelsfa.ES.20260716" not in channels
    # #232: α = 0 vypíná CELOU flow-adjusted vrstvu — netflow, oiest i FA Dyn GEX
    for series in ("netflow", "oiest", "gexprofilefa", "gexfieldfa"):
        assert not (settings.derived_dir / "ES" / "20260716" / series / f"{day}.parquet").exists()
    assert "oiest.ES.20260716" not in channels
    assert "gexprofilefa.ES.20260716" not in channels
    assert "gexfieldfa.ES.20260716" not in channels


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


async def test_fallback_retezu_prebira_kotace_a_zastavuje_cumdelta(
    runtime: tuple[EngineRuntime, RecordingPublisher, Settings],
) -> None:
    """#614 fáze 2b: při výpadku IBKR staví cyklus profil z tasty kotací.

    Zároveň se hlídá druhá polovina zadání — CumΔ a net objem se za fallbacku
    NEKRMÍ. Tasty denní objem v sémantice IBKR nedodává, takže dosadit nulu by
    znamenalo skokový záporný přírůstek přes celý řetěz.
    """
    engine_runtime, _publisher, settings = runtime
    specs = list(engine_runtime.contracts)
    substitute = {
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
    engine_runtime.chain_fallback = lambda _specs: substitute

    await engine_runtime.run_cycle(TS, SPOT, [])

    day = TS.date().isoformat()
    snapshots = pd.read_parquet(settings.snapshots_dir / "ES" / "20260716" / f"{day}.parquet")
    # Gamma z tasty se propsala do snímku, ne hodnota z mock IBKR streameru
    assert set(snapshots["gamma"].unique()) == {0.0042}
    # Objem ani poslední cena se nevymýšlejí — díra, kterou je vidět (#465)
    assert snapshots["volume"].isna().all()
    assert snapshots["last"].isna().all()
    # A CumΔ zůstal netknutý: tracker žádný bar nedostal
    assert engine_runtime.last_flow is not None
    assert engine_runtime.last_flow.cum_delta == 0.0


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
