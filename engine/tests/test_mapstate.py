"""Stav „tenká mapa" (#1245): čistá funkce, prahy z historie, store, kolektor."""

import datetime as dt
from pathlib import Path
from typing import cast

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import create_engine

from gexlens_engine.compute.gexfield import GexProfile
from gexlens_engine.compute.levels import GexLevels
from gexlens_engine.compute.mapstate import (
    FUSED_MAP_PCT,
    MAP_STATE_VERSION,
    MIN_SAMPLE,
    MapHistory,
    MapInputs,
    history_thresholds,
    map_state,
    percentile,
    session_stats,
)
from gexlens_engine.compute.settle import settle_ts
from gexlens_engine.mapstate import MapStateCollector, read_session_stats
from gexlens_engine.runtime import EngineRuntime
from gexlens_engine.storage.mapstate_store import MapStateRepository
from gexlens_engine.storage.parquet_store import (
    BARS_SCHEMA,
    GEXPROFILE_SCHEMA,
    LEVELS_SCHEMA,
    WALLDOM_SCHEMA,
)

TS = dt.datetime(2026, 9, 21, 14, 0, tzinfo=dt.UTC)  # pondělí, US RTH
HISTORY = MapHistory(gex_abs_threshold=50.0, gamma_abs_threshold=2.0, sample=20)


def inputs(**overrides: object) -> MapInputs:
    base: dict[str, object] = {
        "ts_min": TS,
        "spot": 7650.0,
        "total_gex": 400.0,
        "gamma_at_price": 10.0,
        "call_wall_dom": 0.45,
        "put_wall_dom": 0.4,
        "flip": 7600.0,
        "call_wall": 7700.0,
        "put_wall": 7550.0,
        "max_pain": 7625.0,
    }
    base.update(overrides)
    return MapInputs(**base)  # type: ignore[arg-type]


# ── Čistá funkce ────────────────────────────────────────────────


def test_slita_mapa_po_opexu_je_thin() -> None:
    """Referenční případ z #1245: vše na 7 650, gamma u nuly, zdi bez dominance."""
    state = map_state(
        inputs(
            total_gex=-1.5,
            gamma_at_price=0.1,
            call_wall_dom=0.12,
            put_wall_dom=0.1,
            flip=7651.0,
            call_wall=7650.0,
            put_wall=7650.0,
            max_pain=7650.0,
        ),
        HISTORY,
    )
    assert state.thin is True
    assert (state.thin_gamma, state.weak_walls, state.fused) == (True, True, True)
    assert len(state.reasons) == 3
    assert state.spread_pct is not None and state.spread_pct < FUSED_MAP_PCT
    assert state.version == MAP_STATE_VERSION


def test_bezna_seance_neni_thin() -> None:
    state = map_state(inputs(), HISTORY)
    assert state.thin is False
    assert (state.thin_gamma, state.weak_walls, state.fused) == (False, False, False)
    assert state.reasons == ()


def test_jedna_podminka_nestaci() -> None:
    """21. 9. 2026 (#1241): slabá zeď sama o sobě tenká mapa není."""
    state = map_state(inputs(call_wall_dom=0.18, put_wall_dom=0.2), HISTORY)
    assert state.weak_walls is True
    assert state.thin is False


def test_bez_historie_se_tenka_gamma_neurcuje() -> None:
    """Bez prahů je podmínka None a thin stojí jen na zdech + slité mapě."""
    fused = inputs(
        total_gex=-1.5,
        gamma_at_price=0.1,
        call_wall_dom=0.1,
        put_wall_dom=0.1,
        flip=7651.0,
        call_wall=7650.0,
        put_wall=7650.0,
        max_pain=7650.0,
    )
    assert map_state(fused, None).thin_gamma is None
    assert map_state(fused, None).thin is True  # zdi + slitá mapa
    short = MapHistory(gex_abs_threshold=None, gamma_abs_threshold=None, sample=3)
    assert map_state(inputs(total_gex=-1.5, gamma_at_price=0.1), short).thin is False


def test_relativni_prah_stejne_pravidlo_pro_es_i_nq() -> None:
    """ES řádově tisíce, NQ desítky — oba projdou týmž percentilovým prahem."""
    es_history = history_thresholds([6000.0 + i * 100 for i in range(20)], [50.0] * 20)
    nq_history = history_thresholds([20.0 + i for i in range(20)], [0.5] * 20)
    assert map_state(inputs(total_gex=6000.0, gamma_at_price=10.0), es_history).thin_gamma is True
    assert map_state(inputs(total_gex=7500.0, gamma_at_price=10.0), es_history).thin_gamma is False
    assert map_state(inputs(total_gex=20.0, gamma_at_price=0.1), nq_history).thin_gamma is True
    assert map_state(inputs(total_gex=35.0, gamma_at_price=0.1), nq_history).thin_gamma is False


def test_tenka_gamma_chce_obe_podminky() -> None:
    """Celkové GEX vyrušené dvěma stranami, ale gamma u ceny je → není tenká."""
    assert map_state(inputs(total_gex=1.0, gamma_at_price=10.0), HISTORY).thin_gamma is False


def test_percentil_a_prahy_z_historie() -> None:
    assert percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.25) == 2.0
    history = history_thresholds([float(i) for i in range(MIN_SAMPLE)], [None] * MIN_SAMPLE)
    assert history.gex_abs_threshold is not None
    assert history.gamma_abs_threshold is None  # gamma řada bez hodnot
    assert history.sample == 0


def test_session_stats_agreguje_minuty() -> None:
    record = session_stats(TS.date(), "ES", [10.0, 20.0, 30.0], [1.0], [0.3, 0.5], [True, False])
    assert record is not None
    assert record.gex_abs_median == 20.0
    assert record.gex_abs_p10 == 12.0
    assert record.gamma_abs_median == 1.0
    assert record.dom_median == 0.4
    assert record.thin_share == 0.5
    assert record.sample_minutes == 3
    assert session_stats(TS.date(), "ES", [], [], [], []) is None


# ── Store ───────────────────────────────────────────────────────


def test_store_upsert_idempotentni_a_historie_bez_lookahead(tmp_path: Path) -> None:
    repo = MapStateRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'm.sqlite'}"))
    repo.ensure_schema()
    now = dt.datetime(2026, 9, 22, tzinfo=dt.UTC)
    for offset in range(3):
        day = dt.date(2026, 9, 14) + dt.timedelta(days=offset)
        record = session_stats(day, "ES", [100.0 + offset], [1.0], [0.3], [False])
        assert record is not None
        repo.upsert(record, now)
        repo.upsert(record, now)
    assert len(repo.list_for("ES")) == 3
    history = repo.history_before("ES", dt.date(2026, 9, 16), window=20)
    assert [row.session_date for row in history] == [dt.date(2026, 9, 15), dt.date(2026, 9, 14)]
    assert repo.existing_dates("ES") == {dt.date(2026, 9, 14 + i) for i in range(3)}


# ── Backfill z partic + kolektor ────────────────────────────────


def write_session(data_dir: Path, symbol: str, day: dt.date, *, gex: float, dom: float) -> None:
    """Partice levels/walldom/gexprofile/bars jedné seance s minutami v US RTH."""
    expiry_dir = data_dir / "derived" / symbol / day.strftime("%Y%m%d")
    minutes = [settle_ts(day) - dt.timedelta(minutes=m) for m in (60, 30, 1)]
    rows = {
        "levels": pa.table(
            {
                "ts_min": minutes,
                "flip": [7600.0] * 3,
                "call_wall": [7700.0] * 3,
                "put_wall": [7550.0] * 3,
                "centroid": [7620.0] * 3,
                "total_gex": [gex] * 3,
            },
            schema=LEVELS_SCHEMA,
        ),
        "walldom": pa.table(
            {
                "ts_min": minutes,
                "call_wall_dom": [dom] * 3,
                "put_wall_dom": [dom / 2] * 3,
                "call_wall_2_dom": [None] * 3,
                "put_wall_2_dom": [None] * 3,
            },
            schema=WALLDOM_SCHEMA,
        ),
        "gexprofile": pa.table(
            {
                "ts_min": minutes,
                "grid_start": [7600.0] * 3,
                "grid_step": [10.0] * 3,
                "values": [[1.0, 3.0, 5.0]] * 3,
            },
            schema=GEXPROFILE_SCHEMA,
        ),
    }
    for name, table in rows.items():
        (expiry_dir / name).mkdir(parents=True, exist_ok=True)
        pq.write_table(table, expiry_dir / name / f"{day.isoformat()}.parquet")
    bars_dir = data_dir / "derived" / symbol / "bars"
    bars_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                "ts_min": minutes,
                "open": [7610.0] * 3,
                "high": [7612.0] * 3,
                "low": [7608.0] * 3,
                "close": [7610.0] * 3,
                "volume": [1.0] * 3,
                "source": [None] * 3,
            },
            schema=BARS_SCHEMA,
        ),
        bars_dir / f"{day.isoformat()}.parquet",
    )


def test_read_session_stats_z_partic(tmp_path: Path) -> None:
    day = dt.date(2026, 9, 15)
    write_session(tmp_path, "ES", day, gex=-2500.0, dom=0.4)
    record = read_session_stats(tmp_path, "ES", day)
    assert record is not None
    assert record.gex_abs_median == 2500.0
    assert record.dom_median == 0.4  # silnější z obou dominancí
    assert record.gamma_abs_median == 3.0  # profil 7600/7610/7620 → close 7610
    assert record.thin_share is None  # v době seance nikdo nevyhodnocoval
    assert record.sample_minutes == 3
    assert read_session_stats(tmp_path, "ES", dt.date(2026, 9, 16)) is None


class FakeRuntime:
    def __init__(self, levels: GexLevels | None, profile: GexProfile | None) -> None:
        self.last_gex_levels = levels
        self.last_profile = profile
        self.thin_map: bool | None = None


async def test_collector_backfill_stav_a_zapis_po_settle(tmp_path: Path) -> None:
    repo = MapStateRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'm.sqlite'}"))
    repo.ensure_schema()
    # Historie: MIN_SAMPLE seancí s |GEX| 1000–1900 a gamma 3 → práh p25
    for i in range(MIN_SAMPLE):
        write_session(
            tmp_path,
            "ES",
            dt.date(2026, 8, 3) + dt.timedelta(days=i),
            gex=1000.0 + i * 100,
            dom=0.4,
        )
    collector = MapStateCollector(symbol="ES", repository=repo, data_dir=tmp_path)

    thin_levels = GexLevels(
        flip=7651.0, call_wall=7650.0, put_wall=7650.0, centroid=7650.0, total_gex=-1.5,
        call_wall_dom=0.1, put_wall_dom=0.1,
    )  # fmt: skip
    flat_profile = GexProfile(ts_min=TS, grid_start=7600.0, grid_step=10.0, values=(0.1,) * 11)
    runtime = FakeRuntime(thin_levels, flat_profile)
    await collector.on_minute(TS, 7650.0, cast(EngineRuntime, runtime), 7650.0)

    assert len(repo.existing_dates("ES")) == MIN_SAMPLE  # backfill z partic
    assert collector.state is not None and collector.state.thin is True
    assert collector.state.thin_gamma is True  # historie z backfillu dala prahy
    assert runtime.thin_map is True

    # Běžná minuta téže seance: silné zdi, GEX nad prahem → ne thin
    runtime.last_gex_levels = GexLevels(
        flip=7600.0, call_wall=7700.0, put_wall=7550.0, centroid=7620.0, total_gex=2000.0,
        call_wall_dom=0.5, put_wall_dom=0.4,
    )  # fmt: skip
    await collector.on_minute(
        TS + dt.timedelta(minutes=1), 7650.0, cast(EngineRuntime, runtime), 7625.0
    )
    assert collector.state.thin is False

    # Po settle + grace se zapíše agregát seance z obou RTH minut
    after = settle_ts(TS.date()) + dt.timedelta(minutes=6)
    await collector.on_minute(after, 7650.0, cast(EngineRuntime, runtime), 7625.0)
    row = repo.history_before("ES", TS.date() + dt.timedelta(days=1), window=1)[0]
    assert row.session_date == TS.date()
    assert row.sample_minutes == 2  # minuta po settle je mimo RTH
    assert row.thin_share == 0.5
    await collector.on_minute(
        after + dt.timedelta(minutes=1), 7650.0, cast(EngineRuntime, runtime), 7625.0
    )
    assert len(repo.existing_dates("ES")) == MIN_SAMPLE + 1  # jen jednou per seance
