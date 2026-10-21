"""Testy SignalJob (#294): zápis, dedup, expirace stavem, outcomes."""

import datetime as dt
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import create_engine, insert, select
from sqlalchemy.engine import Engine

from gexlens_engine.compute.signal_gate import gate_open
from gexlens_engine.storage.sentiment import (
    ensure_sentiment_schema,
    news_events,
    news_model_stats,
    signal_outcomes,
    signals,
)
from gexlens_news.sentindex import half_life_minutes
from gexlens_news.signal_job import SignalJob, cum_delta_slope, load_gex_context

NOW = dt.datetime(2026, 7, 29, 14, 0, tzinfo=dt.UTC)


def make_db(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    return engine


def seed_event(
    engine: Engine,
    event_id: int,
    *,
    score: float = 0.6,
    now: dt.datetime = NOW,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(news_events),
            [
                {
                    "id": event_id,
                    "ts_event": now - dt.timedelta(minutes=10),
                    "ts_ingested": now,
                    "source": "rss_news",
                    "kind": "headline",
                    "category": "FED",
                    "importance": 3,
                    "title": "Fed signals pause",
                    "symbols": ["ES"],
                    "market_closed": False,
                    "sentiment_dir": 1 if score > 0 else -1,
                    "sentiment_score": score,
                    "sentiment_source": "llm",
                    "dedup_hash": f"hash-{event_id}",
                    "raw": {},
                }
            ],
        )


def seed_bucket(engine: Engine, *, n: int = 50, lb: float = 0.6, mean: float = 6.0) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(news_model_stats),
            [
                {
                    "category": "FED",
                    "importance": 3,
                    "surprise_bucket": "none",
                    "deferred": False,
                    "window_min": 5,
                    "symbol": "ES",
                    "n": n,
                    "ret_mean_bp": mean,
                    "ret_median_bp": mean,
                    "ret_sigma_bp": 4.0,
                    "hit_rate": 0.65,
                    "hit_rate_lb": lb,
                    "gate_open": gate_open(n, lb, mean),
                    "computed_at": NOW,
                }
            ],
        )


def write_partitions(
    directory: Path, rows: Sequence[Sequence[Any]], columns: dict[str, pa.DataType]
) -> None:
    """Řádky do UTC partic `{den}.parquet` podle `ts_min` (první sloupec), jako engine."""
    by_day: dict[dt.date, list[Sequence[Any]]] = {}
    for row in rows:
        by_day.setdefault(row[0].date(), []).append(row)
    directory.mkdir(parents=True, exist_ok=True)
    for day, day_rows in by_day.items():
        table = pa.table(
            {
                name: pa.array([row[index] for row in day_rows], kind)
                for index, (name, kind) in enumerate(columns.items())
            }
        )
        pq.write_table(table, directory / f"{day.isoformat()}.parquet")


def write_bars(tmp_path: Path, closes: list[tuple[dt.datetime, float | None]]) -> None:
    write_partitions(
        tmp_path / "derived" / "ES" / "bars",
        [(ts, close, close, close, close, 0.0) for ts, close in closes],
        {
            "ts_min": pa.timestamp("us", tz="UTC"),
            "open": pa.float64(),
            "high": pa.float64(),
            "low": pa.float64(),
            "close": pa.float64(),
            "volume": pa.float64(),
        },
    )


def test_signal_created_once_and_deduped(tmp_path: Path) -> None:
    engine = make_db(tmp_path)
    seed_event(engine, 1)
    seed_bucket(engine)
    job = SignalJob(engine, tmp_path, symbols=("ES",))

    assert job.run(NOW, states={"ES": "RiskOn"}) == 1
    assert len(job.last_created) == 1
    created = job.last_created[0]
    assert created["direction"] == "long"
    assert created["mode"] == "NEWS"

    # Týž event podruhé → dedup, nic nového
    assert job.run(NOW + dt.timedelta(minutes=5), states={"ES": "RiskOn"}) == 0

    with engine.connect() as conn:
        rows = conn.execute(select(signals)).fetchall()
    assert len(rows) == 1
    assert rows[0].inputs["event_id"] == 1


def test_no_signals_in_neutral_or_below_gate(tmp_path: Path) -> None:
    engine = make_db(tmp_path)
    seed_event(engine, 1)
    seed_bucket(engine, n=10)  # pod gate
    job = SignalJob(engine, tmp_path, symbols=("ES",))
    assert job.run(NOW, states={"ES": "RiskOn"}) == 0

    seed_bucket_ok = SignalJob(engine, tmp_path, symbols=("ES",))
    assert seed_bucket_ok.run(NOW, states={"ES": "Neutral"}) == 0  # Neutral negeneruje


def test_rejects_are_counted_per_filter(tmp_path: Path) -> None:
    """Rozpad důvodů odpadnutí (#453) — bez něj je prázdná `signals` němá."""
    engine = make_db(tmp_path)
    seed_event(engine, 1)
    job = SignalJob(engine, tmp_path, symbols=("ES",))

    # Bez bucket statistik odpadne kandidát hned na prvním filtru
    assert job.run(NOW, states={"ES": "RiskOn"}) == 0
    assert job.last_rejects == {"bez_statistik": 1}

    # Statistiky pod gate → posune se filtr, na kterém to padá
    seed_bucket(engine, n=10)
    assert job.run(NOW, states={"ES": "RiskOn"}) == 0
    assert job.last_rejects == {"gate": 1}

    # Neutral: kandidát se načte, ale zastaví ho stav (a je to v počítadle)
    assert job.run(NOW, states={"ES": "Neutral"}) == 0
    assert job.last_rejects == {"stav": 1}


def test_combined_reject_is_counted_alongside_created_news_signal(tmp_path: Path) -> None:
    """NEWS vznikne, COMBINED ne — chybějící GEX kontext musí být vidět."""
    engine = make_db(tmp_path)
    seed_event(engine, 1)
    seed_bucket(engine)
    job = SignalJob(engine, tmp_path, symbols=("ES",))

    assert job.run(NOW, states={"ES": "RiskOn"}) == 1
    assert job.last_rejects == {"gex_chybi": 1}

    # Druhý běh: NEWS už je dedupovaný, COMBINED pořád bez kontextu
    assert job.run(NOW + dt.timedelta(minutes=1), states={"ES": "RiskOn"}) == 0
    assert job.last_rejects == {"gex_chybi": 1, "dedup": 1}


def test_confirmed_state_change_expires_active_signals(tmp_path: Path) -> None:
    engine = make_db(tmp_path)
    seed_event(engine, 1)
    seed_bucket(engine)
    job = SignalJob(engine, tmp_path, symbols=("ES",))
    assert job.run(NOW, states={"ES": "RiskOn"}) == 1

    # Potvrzená změna stavu → aktivní signál expiruje okamžitě (SPEC 6.3)
    later = NOW + dt.timedelta(minutes=30)
    job.run(later, states={"ES": "Neutral"})
    with engine.connect() as conn:
        row = conn.execute(select(signals)).one()
    expiry = row.expiry_ts if row.expiry_ts.tzinfo else row.expiry_ts.replace(tzinfo=dt.UTC)
    assert expiry == later


def test_outcomes_measured_from_bars_after_windows_close(tmp_path: Path) -> None:
    engine = make_db(tmp_path)
    seed_event(engine, 1)
    seed_bucket(engine)
    # Bary: cena 7450 při signálu, 7460 po 5 minutách → +13.4 bp → long correct
    write_bars(
        tmp_path,
        [
            (NOW - dt.timedelta(minutes=1), 7450.0),
            (NOW + dt.timedelta(minutes=1), 7452.0),
            (NOW + dt.timedelta(minutes=5), 7460.0),
        ],
    )
    job = SignalJob(engine, tmp_path, symbols=("ES",))
    assert job.run(NOW, states={"ES": "RiskOn"}) == 1

    # Po uzavření okna +5 min se outcome spočítá; delší okna ještě ne
    job.run(NOW + dt.timedelta(minutes=6), states={"ES": "RiskOn"})
    with engine.connect() as conn:
        outcomes = conn.execute(select(signal_outcomes)).fetchall()
    by_window = {int(o.window_min): o for o in outcomes}
    assert 5 in by_window
    assert 60 not in by_window
    outcome = by_window[5]
    assert outcome.correct is True or outcome.correct == 1
    assert float(outcome.ret_bp) == pytest.approx((7460.0 - 7450.0) / 7450.0 * 10_000, rel=1e-6)


def test_gex_context_loader(tmp_path: Path) -> None:
    # Bez barů → žádný kontext
    assert load_gex_context(tmp_path, "ES", NOW) is None

    write_bars(tmp_path, [(NOW - dt.timedelta(minutes=1), 7455.0)])
    levels_path = (
        tmp_path / "derived" / "ES" / "20260729" / "levels" / f"{NOW.date().isoformat()}.parquet"
    )
    levels_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                "ts_min": pa.array([NOW - dt.timedelta(minutes=2)], pa.timestamp("us", tz="UTC")),
                "flip": pa.array([7440.0]),
            }
        ),
        levels_path,
    )
    context = load_gex_context(tmp_path, "ES", NOW)
    assert context is not None
    assert context.spot == 7455.0
    assert context.flip == 7440.0
    assert context.supports("long")  # spot nad flipem


def test_stavy_per_symbol_a_state_symbol_v_inputs(tmp_path: Path) -> None:
    """ADR-0026: NQ generuje proti NQ stavu; ES v Neutral mlčí; inputs nesou state_symbol."""
    engine = make_db(tmp_path)
    seed_event(engine, 1)
    seed_bucket(engine)  # ES bucket
    with engine.begin() as conn:  # NQ bucket téhož tvaru
        conn.execute(
            insert(news_model_stats),
            [
                {
                    "category": "FED",
                    "importance": 3,
                    "surprise_bucket": "none",
                    "deferred": False,
                    "window_min": 5,
                    "symbol": "NQ",
                    "n": 50,
                    "ret_mean_bp": 6.0,
                    "ret_median_bp": 6.0,
                    "ret_sigma_bp": 4.0,
                    "hit_rate": 0.65,
                    "hit_rate_lb": 0.6,
                    "gate_open": True,
                    "computed_at": NOW,
                }
            ],
        )
    job = SignalJob(engine, tmp_path, symbols=("ES", "NQ"))

    created = job.run(NOW, states={"ES": "Neutral", "NQ": "RiskOn"})

    assert created == 1  # jen NQ — ES stav negeneruje
    with engine.connect() as conn:
        rows = conn.execute(select(signals)).fetchall()
    assert {row.symbol for row in rows} == {"NQ"}
    assert all(row.inputs["state_symbol"] == "NQ" for row in rows)


# ── Mimo obchodní dobu se negeneruje (#968) ────────────────────────

# Rozvrh CME v létě (CDT = UTC−5): 16:00 CT = 21:00 UTC, 17:00 CT = 22:00 UTC.
# NOW výše je středa 14:00 UTC = 09:00 CT, tedy uprostřed seance.
SOBOTA = dt.datetime(2026, 8, 1, 14, 0, tzinfo=dt.UTC)  # so — celý den zavřeno
PATEK_PO_ZAVRENI = dt.datetime(2026, 7, 31, 21, 30, tzinfo=dt.UTC)  # pá 16:30 CT
DENNI_PAUZA = dt.datetime(2026, 7, 29, 21, 30, tzinfo=dt.UTC)  # st 16:30 CT
NEDELE_PRED_OTEVRENIM = dt.datetime(2026, 8, 2, 20, 0, tzinfo=dt.UTC)  # ne 15:00 CT


@pytest.mark.parametrize(
    ("kdy", "popis"),
    [
        (SOBOTA, "sobota"),
        (PATEK_PO_ZAVRENI, "pátek po zavření"),
        (DENNI_PAUZA, "denní pauza CME"),
        (NEDELE_PRED_OTEVRENIM, "neděle před otevřením Globexu"),
    ],
)
def test_mimo_obchodni_dobu_nevznikne_signal(tmp_path: Path, kdy: dt.datetime, popis: str) -> None:
    """Signál se zavřeným trhem nejde obchodovat ani vyhodnotit — outcome se
    počítá z barů a ty tam nejsou (#968)."""
    engine = make_db(tmp_path)
    # Event k testovanému okamžiku, ať kandidát SKUTEČNĚ existuje — jinak by
    # test prošel jen proto, že nebylo co zamítnout
    seed_event(engine, 1, now=kdy)
    seed_bucket(engine)
    job = SignalJob(engine, tmp_path, symbols=("ES",))

    assert job.run(kdy, states={"ES": "RiskOn"}) == 0, f"{popis}: signál neměl vzniknout"

    with engine.connect() as conn:
        assert conn.execute(select(signals)).fetchall() == []
    # Musí být vidět v rozpadu, jinak prázdná `signals` vypadá jako porucha (#453)
    assert job.last_rejects["trh zavřený"] > 0


def test_v_obchodni_dobe_se_chovani_nemeni(tmp_path: Path) -> None:
    """Regrese k #968: tentýž vstup v seanci musí signál vytvořit dál."""
    engine = make_db(tmp_path)
    seed_event(engine, 1)
    seed_bucket(engine)
    job = SignalJob(engine, tmp_path, symbols=("ES",))

    assert job.run(NOW, states={"ES": "RiskOn"}) == 1
    assert job.last_rejects["trh zavřený"] == 0


# ── GEX kontext k okamžiku signálu: sklon v seanci, stáří, look-ahead (#1589, #1583) ──

UTC_TS = pa.timestamp("us", tz="UTC")
FLOW_COLUMNS = {
    "ts_min": UTC_TS,
    "flow_delta": pa.float64(),
    "cum_delta": pa.float64(),
    "source": pa.string(),
}
# Středa 21. 10. 2026, CDT (UTC−5): seance 22. 10. začíná 17:00 CT = 21. 10. 22:00 UTC
SEANCE_START = dt.datetime(2026, 10, 21, 22, 0, tzinfo=dt.UTC)

FlowTuple = tuple[dt.datetime, float | None, float | None, str | None]


def minutes(start: dt.datetime, end: dt.datetime) -> list[dt.datetime]:
    count = int((end - start).total_seconds() // 60)
    return [start + dt.timedelta(minutes=index) for index in range(count + 1)]


def write_flow(tmp_path: Path, rows: list[FlowTuple], *, with_source: bool = True) -> None:
    columns = dict(FLOW_COLUMNS)
    if not with_source:  # partice před zavedením sloupce `source` (= midpoint)
        columns.pop("source")
    width = len(columns)
    write_partitions(tmp_path / "derived" / "ES" / "flow", [row[:width] for row in rows], columns)


def write_levels(tmp_path: Path, expiry: str, rows: list[tuple[dt.datetime, float | None]]) -> None:
    write_partitions(
        tmp_path / "derived" / "ES" / expiry / "levels",
        rows,
        {"ts_min": UTC_TS, "flip": pa.float64()},
    )


def flow_series(
    start: dt.datetime, end: dt.datetime, *, source: str | None, step: float = 10.0
) -> list[FlowTuple]:
    """Řada flow s tokem `step` za minutu; CumΔ se v 17:00 CT nuluje jako v enginu."""
    rows: list[FlowTuple] = []
    cum = 5_000.0  # minulá seance
    for ts in minutes(start, end.replace(second=0, microsecond=0)):
        if ts == SEANCE_START:
            cum = 0.0
        cum += step
        rows.append((ts, step, cum, source))
    return rows


def as_dicts(rows: list[FlowTuple]) -> list[dict[str, Any]]:
    return [
        {"ts_min": ts, "flow_delta": flow, "cum_delta": cum, "source": source}
        for ts, flow, cum, source in rows
    ]


def test_sklon_z_tisku_pres_pulnoc_utc_cte_obe_partice(tmp_path: Path) -> None:
    """00:05 UTC: okno (23:55, 00:05] leží ve dvou UTC particích téže seance."""
    now = dt.datetime(2026, 10, 22, 0, 5, 30, tzinfo=dt.UTC)
    write_bars(tmp_path, [(now.replace(second=0), 7600.0)])
    write_flow(tmp_path, flow_series(SEANCE_START, now, source="aggressor"))

    context = load_gex_context(tmp_path, "ES", now)

    assert context is not None
    assert context.cum_delta_slope == pytest.approx(100.0)  # 10 minut × 10
    assert context.cum_delta_source == "aggressor"
    assert context.slope_ts == dt.datetime(2026, 10, 22, 0, 5, tzinfo=dt.UTC)


def test_sklon_pod_midpointem_beze_zmeny_jen_dnesni_partice(tmp_path: Path) -> None:
    """Midpoint (i partice bez sloupce `source`) počítá jako před #1589 (ADR-0057 bod 11)."""
    now = dt.datetime(2026, 10, 22, 0, 5, 30, tzinfo=dt.UTC)
    write_bars(tmp_path, [(now.replace(second=0), 7600.0)])
    write_flow(tmp_path, flow_series(SEANCE_START, now, source=None), with_source=False)

    context = load_gex_context(tmp_path, "ES", now)

    assert context is not None
    # V dnešní UTC partici není řádek 10 min zpět → sklon chybí, jako dřív
    assert context.cum_delta_slope is None
    assert context.cum_delta_source == "midpoint"

    later = dt.datetime(2026, 10, 22, 0, 12, 30, tzinfo=dt.UTC)
    write_bars(tmp_path, [(later.replace(second=0), 7600.0)])
    write_flow(tmp_path, flow_series(SEANCE_START, later, source=None), with_source=False)
    context = load_gex_context(tmp_path, "ES", later)
    assert context is not None
    assert context.cum_delta_slope == pytest.approx(100.0)


def test_sklon_na_zacatku_seance() -> None:
    """22:03 UTC: tisky okno přes začátek seance nepočítají (#1581), midpoint jako dřív."""
    now = dt.datetime(2026, 10, 21, 22, 3, 30, tzinfo=dt.UTC)
    start = dt.datetime(2026, 10, 21, 21, 40, tzinfo=dt.UTC)
    rows = flow_series(start, now, source="aggressor")

    assert cum_delta_slope(as_dicts(rows), now) == (None, "aggressor")
    # Midpoint dál čte rozdíl přes reset (minulá seance) — mění ho až bump v6
    midpoint = flow_series(start, now, source="midpoint")
    slope, source = cum_delta_slope(as_dicts(midpoint), now)
    assert source == "midpoint"
    assert slope == pytest.approx(40.0 - (5_000.0 + 140.0))

    # Deset minut po začátku seance už okno v seanci leží
    later = dt.datetime(2026, 10, 21, 22, 10, 30, tzinfo=dt.UTC)
    rows = flow_series(start, later, source="aggressor")
    assert cum_delta_slope(as_dicts(rows), later) == (100.0, "aggressor")


def test_sklon_z_tisku_s_null(tmp_path: Path) -> None:
    """NULL v posledním řádku nebo uvnitř okna = mezera → None; v hraničním řádku nevadí."""
    now = dt.datetime(2026, 10, 21, 14, 20, 30, tzinfo=dt.UTC)
    start = dt.datetime(2026, 10, 21, 14, 0, tzinfo=dt.UTC)
    base = flow_series(start, now, source="aggressor")

    def with_null(minute: int) -> list[dict[str, Any]]:
        rows = as_dicts(base)
        index = next(i for i, row in enumerate(rows) if row["ts_min"].minute == minute)
        rows[index] = {**rows[index], "flow_delta": None, "cum_delta": None}
        return rows

    assert cum_delta_slope(as_dicts(base), now) == (100.0, "aggressor")
    assert cum_delta_slope(with_null(20), now) == (None, "aggressor")  # poslední řádek
    assert cum_delta_slope(with_null(15), now) == (None, "aggressor")  # uvnitř okna
    assert cum_delta_slope(with_null(10), now) == (100.0, "aggressor")  # hranice

    # Řádek jiného zdroje v okně (restart přes bump) je pro tisky mezera
    mixed = as_dicts(base)
    mixed[-3] = {**mixed[-3], "source": "midpoint"}
    assert cum_delta_slope(mixed, now) == (None, "aggressor")

    # NULL ve flow ani v close cyklus neshodí (dřív float(None) mimo try)
    minute = now.replace(second=0)
    write_bars(tmp_path, [(minute - dt.timedelta(minutes=1), 7600.0), (minute, None)])
    write_flow(tmp_path, [*base[:-1], (base[-1][0], None, None, "aggressor")])
    context = load_gex_context(tmp_path, "ES", now)
    assert context is not None
    assert context.spot == 7600.0
    assert context.cum_delta_slope is None


def test_kontext_necte_radky_po_now(tmp_path: Path) -> None:
    """Engine mezitím píše další minuty — brána je číst nesmí (#1583)."""
    now = dt.datetime(2026, 10, 21, 14, 20, 30, tzinfo=dt.UTC)
    minute = now.replace(second=0)
    after = minute + dt.timedelta(minutes=5)
    write_bars(tmp_path, [(minute, 7600.0), (after, 7700.0)])
    write_levels(tmp_path, "20261021", [(minute, 7590.0), (after, 7650.0)])
    rows = flow_series(dt.datetime(2026, 10, 21, 14, 0, tzinfo=dt.UTC), now, source="aggressor")
    write_flow(tmp_path, [*rows, (after, -5_000.0, -4_000.0, "aggressor")])

    context = load_gex_context(tmp_path, "ES", now)

    assert context is not None
    assert (context.spot, context.flip) == (7600.0, 7590.0)
    assert context.cum_delta_slope == pytest.approx(100.0)
    assert {context.spot_ts, context.flip_ts, context.slope_ts} == {minute}


def test_stary_kontext_neplati(tmp_path: Path) -> None:
    """Řádek > 2 min za minutou `now`: spot → kontext chybí, flip a sklon nepodporují."""
    now = dt.datetime(2026, 10, 21, 14, 20, 30, tzinfo=dt.UTC)
    minute = now.replace(second=0)
    stale = minute - dt.timedelta(minutes=3)
    write_bars(tmp_path, [(stale, 7600.0)])
    assert load_gex_context(tmp_path, "ES", now) is None

    write_bars(tmp_path, [(stale, 7600.0), (minute - dt.timedelta(minutes=2), 7601.0)])
    # Aktivní 0DTE se zpozdil, sekundár D+1 píše dál → flip chybí, ne flip D+1
    write_levels(tmp_path, "20261021", [(stale, 7500.0)])
    write_levels(tmp_path, "20261022", [(minute - dt.timedelta(minutes=1), 7590.0)])
    write_flow(tmp_path, flow_series(stale - dt.timedelta(minutes=15), stale, source="aggressor"))

    context = load_gex_context(tmp_path, "ES", now)

    assert context is not None
    assert context.spot == 7601.0
    assert context.flip is None
    assert context.flip_ts is None
    assert context.cum_delta_slope is None
    assert context.cum_delta_source is None


def test_flip_po_settle_z_dalsi_expirace(tmp_path: Path) -> None:
    """0DTE po settle (16:00 ET = 20:00 UTC) už nepíše → flip z další expirace."""
    before = dt.datetime(2026, 10, 21, 19, 58, 30, tzinfo=dt.UTC)
    after = dt.datetime(2026, 10, 21, 20, 30, 30, tzinfo=dt.UTC)
    write_bars(tmp_path, [(before.replace(second=0), 7600.0), (after.replace(second=0), 7601.0)])
    write_levels(tmp_path, "20261021", [(before.replace(second=0), 7500.0)])
    write_levels(
        tmp_path,
        "20261022",
        [(before.replace(second=0), 7580.0), (after.replace(second=0), 7590.0)],
    )

    context = load_gex_context(tmp_path, "ES", before)
    assert context is not None
    assert context.flip == 7500.0  # před settle je aktivní 0DTE

    context = load_gex_context(tmp_path, "ES", after)
    assert context is not None
    assert context.flip == 7590.0
    assert context.flip_ts == after.replace(second=0)


def seed_combined_context(tmp_path: Path, now: dt.datetime, *, flip: float | None) -> None:
    """Spot 7600, sklon chybí (mezera tisků v okně), flip podle parametru (None = bez levels)."""
    minute = now.replace(second=0, microsecond=0)
    write_bars(tmp_path, [(minute, 7600.0)])
    if flip is not None:
        write_levels(tmp_path, now.strftime("%Y%m%d"), [(minute, flip)])
    rows = flow_series(minute - dt.timedelta(minutes=15), minute, source="aggressor")
    rows[-4] = (rows[-4][0], None, None, "aggressor")
    write_flow(tmp_path, rows)


def test_combined_bez_sklonu_potvrdi_flip(tmp_path: Path) -> None:
    """Chybějící sklon = noha nepodporuje; souhlasný flip COMBINED založí (ADR-0057 bod 4)."""
    engine = make_db(tmp_path)
    seed_event(engine, 1)
    seed_bucket(engine)
    seed_combined_context(tmp_path, NOW, flip=7590.0)  # spot nad flipem → long
    job = SignalJob(engine, tmp_path, symbols=("ES",))

    assert job.run(NOW, states={"ES": "RiskOn"}) == 2
    combined = next(row for row in job.last_created if row["mode"] == "COMBINED")
    gex = combined["inputs"]["gex"]
    assert gex["cum_delta_slope"] is None
    assert gex["cum_delta_source"] == "aggressor"
    assert gex["spot_ts"] == NOW.isoformat()
    assert combined["ts"] == NOW.isoformat()


@pytest.mark.parametrize(
    "flip",
    [7650.0, None],  # spot pod flipem → long nepodporuje; flip chybí úplně
)
def test_combined_bez_sklonu_a_bez_flipu_nevznikne(tmp_path: Path, flip: float | None) -> None:
    engine = make_db(tmp_path)
    seed_event(engine, 1)
    seed_bucket(engine)
    seed_combined_context(tmp_path, NOW, flip=flip)
    job = SignalJob(engine, tmp_path, symbols=("ES",))

    assert job.run(NOW, states={"ES": "RiskOn"}) == 1
    assert [row["mode"] for row in job.last_created] == ["NEWS"]
    assert job.last_rejects == {"gex_nesouhlas": 1}


def test_signal_po_expiraci_eventu_nevznikne(tmp_path: Path) -> None:
    """Razítko = okamžik vzniku (#1583): event po své expiraci signál nezaloží."""
    engine = make_db(tmp_path)
    seed_event(engine, 1)  # ts_event = NOW − 10 min
    seed_bucket(engine)
    job = SignalJob(engine, tmp_path, symbols=("ES",))
    expiry = NOW - dt.timedelta(minutes=10) + dt.timedelta(minutes=half_life_minutes("FED", 3))

    assert job.run(expiry + dt.timedelta(minutes=1), states={"ES": "RiskOn"}) == 0
    assert job.last_rejects == {"necerstve": 1}

    assert job.run(expiry - dt.timedelta(minutes=1), states={"ES": "RiskOn"}) == 1
    created = job.last_created[0]
    assert created["expiry_ts"] > created["ts"]
