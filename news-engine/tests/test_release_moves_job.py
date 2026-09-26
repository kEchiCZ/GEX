"""Job měření reakcí na releasy (#1296): SQLite kalendář a tabulky, parquet bary.

Čisté výpočty pokrývá `test_release_moves.py` a `test_release_hypotheses.py`;
tady se ověřuje IO adaptér — idempotence, doplnění překvapení a jeho zmrazení,
release bez actual (fantom), chybějící bary bez pádu, bootstrap prázdné tabulky
i po selhání, CLI přeměření bez živých řádků a zmrazené rozhodnutí hypotéz.
"""

import datetime as dt
import itertools
import logging
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import create_engine, insert, select, update
from sqlalchemy.engine import Engine

import gexlens_news.release_moves_job as moves_job
from gexlens_engine.storage.sentiment import (
    ensure_sentiment_schema,
    news_events,
    release_hypotheses,
    release_moves,
)
from gexlens_news.bars import BarsRepository
from gexlens_news.release_hypotheses import STATUS_VERIFIED
from gexlens_news.release_moves import measure_move
from gexlens_news.release_moves_job import ReleaseMovesJob

UTC = dt.UTC
MINUTE = dt.timedelta(minutes=1)
CPI_AT = dt.datetime(2026, 9, 11, 12, 30, tzinfo=UTC)
_ids = itertools.count(1)


def write_bars(data_dir: Path, symbol: str, rows: list[dict[str, Any]]) -> None:
    """Bary do partic podle UTC dne (jako engine)."""
    by_day: dict[dt.date, list[dict[str, Any]]] = {}
    for row in rows:
        by_day.setdefault(row["ts_min"].date(), []).append(row)
    directory = data_dir / "derived" / symbol / "bars"
    directory.mkdir(parents=True, exist_ok=True)
    for day, items in by_day.items():
        path = directory / f"{day.isoformat()}.parquet"
        if path.exists():
            items = pq.read_table(path).to_pylist() + items
        pq.write_table(pa.Table.from_pylist(items), path)


def bar(ts: dt.datetime, close: float, spread: float = 0.5) -> dict[str, Any]:
    return {
        "ts_min": ts,
        "open": close,
        "high": close + spread,
        "low": close - spread,
        "close": close,
        "volume": 10.0,
    }


def seed_symbol(data_dir: Path, symbol: str, release: dt.datetime, base: float) -> None:
    """25 předchozích dní s denním rozsahem 60 b. a bary kolem releasu se skokem +20 b."""
    rows = [
        bar(dt.datetime.combine(release.date() - dt.timedelta(days=k), dt.time(15), UTC), base, 30)
        for k in range(25, 0, -1)
    ]
    rows += [
        bar(
            release + offset * MINUTE,
            base + (0.25 if offset % 2 else 0) + (20 if offset >= 3 else 0),
        )
        for offset in range(-70, 61)
    ]
    write_bars(data_dir, symbol, rows)


def make_db(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    return engine


def add_release(
    engine: Engine,
    at: dt.datetime,
    title: str,
    *,
    impact: str = "High",
    forecast: float | None = 0.3,
    actual: float | None = 0.4,
) -> int:
    event_id = next(_ids)
    with engine.begin() as conn:
        conn.execute(
            insert(news_events).values(
                id=event_id,
                ts_event=at,
                ts_ingested=at - dt.timedelta(days=2),
                source="forexfactory",
                kind="scheduled",
                title=title,
                forecast=forecast,
                actual=actual,
                symbols=[],
                market_closed=False,
                dedup_hash=f"hash-{event_id}",
                raw={"impact": impact},
            )
        )
    return event_id


def stored(engine: Engine) -> dict[tuple[str, str], dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(select(release_moves)).mappings().all()
    return {(str(row["cluster_ts"])[:16], row["symbol"]): dict(row) for row in rows}


def seeded(tmp_path: Path) -> tuple[Engine, Path]:
    data_dir = tmp_path / "data"
    seed_symbol(data_dir, "ES", CPI_AT, 7800.0)
    seed_symbol(data_dir, "NQ", CPI_AT, 30000.0)
    engine = make_db(tmp_path)
    add_release(engine, CPI_AT, "USD Core CPI m/m")
    add_release(engine, CPI_AT, "USD CPI m/m", impact="Medium", forecast=0.2, actual=0.2)
    return engine, data_dir


def test_mereni_je_idempotentni_a_sedi_s_cistou_funkci(tmp_path: Path) -> None:
    engine, data_dir = seeded(tmp_path)
    repo = BarsRepository(data_dir)
    job = ReleaseMovesJob(engine, repo)
    now = CPI_AT + dt.timedelta(hours=2)
    first = job.run(now, since=CPI_AT - dt.timedelta(days=1))
    assert (first.clusters, first.measured, first.failed) == (1, 2, 0)
    rows = stored(engine)
    es = rows[("2026-09-11 12:30", "ES")]
    expected = measure_move(
        repo.load_range("ES", CPI_AT - dt.timedelta(hours=2), CPI_AT + dt.timedelta(hours=2)),
        CPI_AT,
    )
    assert es["exc_15m_bp"] == pytest.approx(expected.exc_15m_bp)
    assert es["ret_15m_bp"] == pytest.approx(expected.ret_15m_bp)
    assert es["ret_60m_bp"] == pytest.approx(expected.ret_60m_bp)
    assert es["vol_ref_bp"] == pytest.approx(60.0 / 7800.25 * 1e4)
    assert (es["family"], es["headline"], es["surprise_sign"]) == ("CPI", "Core CPI m/m", 1)
    assert es["tod_med_15m_bp"] is None  # před registrací hypotéz se baseline nepočítá
    # Druhý běh nic nemění
    second = ReleaseMovesJob(engine, repo).run(now + dt.timedelta(minutes=5))
    assert (second.measured, second.updated) == (0, 0)
    assert stored(engine) == rows


def test_prekvapeni_doplni_do_7_dni_pak_zmrazi(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    seed_symbol(data_dir, "ES", CPI_AT, 7800.0)
    engine = make_db(tmp_path)
    event_id = add_release(engine, CPI_AT, "USD Core CPI m/m", actual=None)
    job = ReleaseMovesJob(engine, BarsRepository(data_dir), symbols=("ES",))
    # Bez actual se release neměří (FF ho ještě nevyplnil, nebo se nekonal)
    first = job.run(CPI_AT + dt.timedelta(hours=2), since=CPI_AT - dt.timedelta(days=1))
    assert (first.measured, first.without_actual) == (0, 1)
    assert stored(engine) == {}

    def set_actual(value: float) -> None:
        with engine.begin() as conn:
            conn.execute(
                update(news_events).where(news_events.c.id == event_id).values(actual=value)
            )

    set_actual(0.2)
    assert job.run(CPI_AT + dt.timedelta(hours=3)).measured == 1
    assert stored(engine)[("2026-09-11 12:30", "ES")]["surprise_sign"] == -1
    # Oprava actual do 7 dní se propíše jen do překvapení
    set_actual(0.5)
    summary = job.run(CPI_AT + dt.timedelta(days=2))
    assert summary.updated == 1 and summary.measured == 0
    row = stored(engine)[("2026-09-11 12:30", "ES")]
    assert row["surprise_sign"] == 1
    assert row["exc_15m_bp"] is not None  # naměřené hodnoty zůstaly
    # Po 7 dnech se pozdní změna dat do vyhodnocení nepropíše
    set_actual(0.1)
    job.run(CPI_AT + dt.timedelta(days=8))
    assert stored(engine)[("2026-09-11 12:30", "ES")]["surprise_sign"] == 1


def test_release_bez_actual_se_nemeri_a_varuje_jednou(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Fantom po přesunu releasu (#1298) nesmí do historie velikosti ani do hypotéz."""
    data_dir = tmp_path / "data"
    seed_symbol(data_dir, "ES", CPI_AT, 7800.0)
    engine = make_db(tmp_path)
    add_release(engine, CPI_AT, "USD Core CPI m/m", actual=None)
    add_release(engine, CPI_AT, "USD CPI m/m", impact="Medium", actual=0.3)  # jen vedlejší řada
    job = ReleaseMovesJob(engine, BarsRepository(data_dir), symbols=("ES",))
    with caplog.at_level(logging.WARNING, logger=moves_job.__name__):
        job.run(CPI_AT + dt.timedelta(hours=2), since=CPI_AT - dt.timedelta(days=1))
        assert not caplog.records  # do 24 h se na actual čeká bez varování
        for hours in (25, 26, 27):
            summary = job.run(CPI_AT + dt.timedelta(hours=hours))
            assert summary.without_actual == 1 and summary.measured == 0
    assert stored(engine) == {}
    (warning,) = caplog.records
    assert warning.getMessage().startswith("Release USD Core CPI m/m 2026-09-11T12:30")


def test_chybejici_bary_nejsou_pad(tmp_path: Path) -> None:
    engine, data_dir = seeded(tmp_path)
    nfp_at = dt.datetime(2026, 9, 4, 12, 30, tzinfo=UTC)  # pro tento den bary nejsou
    add_release(engine, nfp_at, "USD Non-Farm Employment Change", forecast=55000, actual=22000)
    summary = ReleaseMovesJob(engine, BarsRepository(data_dir)).run(
        CPI_AT + dt.timedelta(hours=2), since=nfp_at - dt.timedelta(days=1)
    )
    assert (summary.clusters, summary.measured, summary.failed) == (2, 4, 0)
    assert summary.unmeasurable == 2
    nfp = stored(engine)[("2026-09-04 12:30", "NQ")]
    assert nfp["family"] == "NFP" and nfp["exc_15m_bp"] is None and nfp["ret_60m_bp"] is None
    assert stored(engine)[("2026-09-11 12:30", "NQ")]["exc_15m_bp"] is not None


def test_rodina_mimo_upozorneni_se_nemeri(tmp_path: Path) -> None:
    engine, data_dir = seeded(tmp_path)
    add_release(engine, CPI_AT + dt.timedelta(hours=1), "USD Building Permits")
    add_release(engine, CPI_AT + dt.timedelta(hours=2), "USD Unemployment Claims")
    summary = ReleaseMovesJob(engine, BarsRepository(data_dir)).run(
        CPI_AT + dt.timedelta(hours=4), since=CPI_AT - dt.timedelta(days=1)
    )
    assert summary.clusters == 1


def test_prazdna_tabulka_se_dopocita_od_zacatku_archivu(tmp_path: Path) -> None:
    engine, data_dir = seeded(tmp_path)
    # Živý běh o měsíc později: 14denní okno by CPI minulo, prázdná tabulka ne
    job = ReleaseMovesJob(engine, BarsRepository(data_dir))
    summary = job.run(CPI_AT + dt.timedelta(days=30))
    assert summary.measured == 2
    add_release(engine, CPI_AT - dt.timedelta(days=60), "USD Core PPI m/m")
    later = job.run(CPI_AT + dt.timedelta(days=30, minutes=5))
    assert later.clusters == 0  # bootstrap proběhl, dál jen 14 dní


def test_selhany_bootstrap_se_zopakuje(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine, data_dir = seeded(tmp_path)
    job = ReleaseMovesJob(engine, BarsRepository(data_dir))

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("PG nedostupná")

    monkeypatch.setattr(f"{moves_job.__name__}.load_releases", unavailable)
    with pytest.raises(RuntimeError):
        job.run(CPI_AT + dt.timedelta(days=30))
    monkeypatch.undo()
    # Tabulka je pořád prázdná → další cyklus dopočítá historii, ne jen 14 dní
    assert job.run(CPI_AT + dt.timedelta(days=30, minutes=5)).measured == 2


def test_hypotezy_prepis_po_zivem_releasu(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine, data_dir = seeded(tmp_path)
    earlier = CPI_AT - dt.timedelta(days=30)
    add_release(engine, earlier, "USD Core PPI m/m", forecast=0.2, actual=0.4)
    registered = CPI_AT - dt.timedelta(days=1)  # replay: CPI je „živý“, PPI před registrací
    job = ReleaseMovesJob(engine, BarsRepository(data_dir), registered_at=registered)
    monkeypatch.setattr(job, "tod_median", lambda symbol, start: 5.0)
    summary = job.run(CPI_AT + dt.timedelta(hours=2), since=earlier - dt.timedelta(days=1))
    assert summary.hypotheses
    with engine.connect() as conn:
        rows = {
            (row["hypothesis"], row["symbol"]): row
            for row in conn.execute(select(release_hypotheses)).mappings()
        }
    assert set(rows) == {("H1", "ES"), ("H1", "NQ"), ("H3", "ES"), ("M1", "ES"), ("M1", "NQ")}
    # CPI: skok +20 b. → výnos 15 min > 0 = H1 minutí, H3 zásah, M1 výchylka nad 5 bp
    assert (rows[("H1", "ES")]["hits"], rows[("H1", "ES")]["n"]) == (0, 1)
    assert (rows[("H3", "ES")]["hits"], rows[("H3", "ES")]["n"]) == (1, 1)
    assert (rows[("M1", "NQ")]["hits"], rows[("M1", "NQ")]["n"]) == (1, 1)
    assert rows[("H1", "ES")]["status"] == "testing"
    assert rows[("H1", "ES")]["outcomes"][0]["family"] == "CPI"
    # Opakovaný běh bez změny tabulku hypotéz nepřepisuje
    again = job.run(CPI_AT + dt.timedelta(hours=2, minutes=5))
    assert not again.hypotheses


def test_cli_premeri_jen_historii_pred_registraci(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, data_dir = seeded(tmp_path)
    earlier = CPI_AT - dt.timedelta(days=30)
    add_release(engine, earlier, "USD Core PPI m/m", forecast=0.2, actual=0.4)
    registered = CPI_AT - dt.timedelta(days=1)  # replay: CPI je živý, PPI historie
    job = ReleaseMovesJob(engine, BarsRepository(data_dir), registered_at=registered)
    monkeypatch.setattr(job, "tod_median", lambda symbol, start: 5.0)
    since = earlier - dt.timedelta(days=1)
    job.run(CPI_AT + dt.timedelta(hours=2), since=since)
    live_before = {key: row for key, row in stored(engine).items() if key[0] == "2026-09-11 12:30"}
    assert len(live_before) == 2
    later = CPI_AT + dt.timedelta(days=10)
    summary = job.run(later, since=since, refresh=True)
    # Přeměřena jen PPI (ES + NQ), živý CPI beze změny a hypotézy se nepřepočítaly
    assert (summary.measured, summary.hypotheses) == (2, False)
    after = stored(engine)
    assert {key: after[key] for key in live_before} == live_before
    # --include-live živé řádky přeměří (a hypotézy přepočte)
    summary = job.run(later, since=since, refresh=True, include_live=True)
    assert (summary.measured, summary.hypotheses) == (4, True)


def test_rozhodnuta_hypoteza_se_neprepise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Uložené rozhodnutí (verified při n = 10) přepočet převezme a jen přičte další release."""
    engine, data_dir = seeded(tmp_path)
    registered = CPI_AT - dt.timedelta(days=400)
    decided = [
        {
            "cluster_ts": (registered + dt.timedelta(days=30 * k)).isoformat(),
            "family": "CPI",
            "hit": k != 4,
            "value_bp": 3.0,
        }
        for k in range(10)
    ]
    with engine.begin() as conn:
        conn.execute(
            insert(release_hypotheses).values(
                hypothesis="H3",
                symbol="ES",
                n=10,
                hits=9,
                wilson_lb=0.596,
                wilson_ub=0.982,
                status=STATUS_VERIFIED,
                decided_at_n=10,
                outcomes=decided,
                computed_at=registered,
            )
        )
    job = ReleaseMovesJob(engine, BarsRepository(data_dir), registered_at=registered)
    monkeypatch.setattr(job, "tod_median", lambda symbol, start: 5.0)
    # Releasy uloženého úseku v release_moves nejsou (jako po chybném přeměření) —
    # rozhodnutí přesto zůstává, přičte se jen živý CPI (výnos 60 min > 0 = zásah)
    job.run(CPI_AT + dt.timedelta(hours=2), since=CPI_AT - dt.timedelta(days=1))
    with engine.connect() as conn:
        row = conn.execute(
            select(release_hypotheses).where(
                release_hypotheses.c.hypothesis == "H3", release_hypotheses.c.symbol == "ES"
            )
        ).one()
    assert (row.status, row.decided_at_n, row.hits, row.n) == (STATUS_VERIFIED, 10, 10, 11)
    assert row.outcomes[:10] == decided
