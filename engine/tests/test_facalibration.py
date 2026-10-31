"""Testy ranní kalibrace α (#232 fáze 2) a ρ pod v6 (ADR-0061): výpočet, úložiště, sběr bodu."""

import datetime as dt
import logging
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import create_engine

from gexlens_engine.compute.cumdelta import SOURCE_AGGRESSOR, SOURCE_MIDPOINT
from gexlens_engine.compute.facalibration import (
    FA_MIN_DAYS,
    SNAPSHOT_END_TOLERANCE_MIN,
    SNAPSHOT_GAP_MAX_MIN,
    AlphaCalibrationPoint,
    calibrate_alpha,
    calibrate_rho,
    session_volume_coverage,
    update_alpha,
)
from gexlens_engine.compute.marketclock import daily_close_utc
from gexlens_engine.compute.settle import session_bounds
from gexlens_engine.config import Settings
from gexlens_engine.storage.fa_calibration import (
    SOURCE_VOLUME,
    AlphaCalibrationResult,
    FaAlphaRepository,
    FaAlphaState,
    calibration_source,
    collect_alpha_calibration,
    live_alpha,
    netflow_at_cutoff,
    pending_rho_sessions,
)
from gexlens_engine.storage.oi_archive import OIEodRepository, OIRecord
from gexlens_engine.storage.parquet_store import NetFlowRow, SnapshotWriter

PREV = dt.date(2026, 8, 5)
TODAY = dt.date(2026, 8, 6)
# Expirace PO dni netflow — 0DTE řetěz (expirace = PREV) kalibrace od #1172 přeskočí
EXPIRY = "20260807"
EXPIRY_0DTE = "20260805"


def test_calibrate_alpha_median_a_strany() -> None:
    """Medián poměrů ΔOI/net přes kvalifikované strany; buy/sell zvlášť pro audit."""
    netflow = {
        (7500.0, "C"): 100.0,
        (7510.0, "C"): 100.0,
        (7520.0, "C"): 100.0,
        (7530.0, "C"): 200.0,
        (7500.0, "P"): -50.0,
        (7510.0, "P"): -100.0,
        (7600.0, "C"): 10.0,  # pod prahem |net| — nekvalifikuje se
    }
    doi = {
        (7500.0, "C"): 40.0,  # 0.4
        (7510.0, "C"): 40.0,  # 0.4
        (7520.0, "C"): 30.0,  # 0.3
        (7530.0, "C"): 100.0,  # 0.5
        (7500.0, "P"): -25.0,  # 0.5 (net prodej, OI klesl — směr sedí)
        (7510.0, "P"): -50.0,  # 0.5
        (7600.0, "C"): 999.0,  # ignorováno
    }
    point = calibrate_alpha(netflow, doi)
    assert point is not None
    assert point.samples == 6
    assert point.ratio_median == pytest.approx(0.45)  # [0.3,0.4,0.4,0.5,0.5,0.5]
    assert point.ratio_buy == pytest.approx(0.4)
    assert point.ratio_sell == pytest.approx(0.5)


def test_calibrate_alpha_znamenko_proti_smeru() -> None:
    """Net nákup s poklesem OI dává záporný poměr — medián ho po právu stáhne."""
    netflow = {(7500.0 + i, "C"): 100.0 for i in range(5)}
    doi = {(7500.0 + i, "C"): -40.0 for i in range(5)}
    point = calibrate_alpha(netflow, doi)
    assert point is not None
    assert point.ratio_median == pytest.approx(-0.4)


def test_calibrate_alpha_maly_vzorek_vraci_none() -> None:
    netflow = {(7500.0, "C"): 100.0, (7510.0, "C"): 100.0}
    assert calibrate_alpha(netflow, {(7500.0, "C"): 40.0}) is None
    assert calibrate_alpha({}, {}) is None


def test_update_alpha_prvni_bod_a_ema() -> None:
    assert update_alpha(None, 0.45) == 0.45
    # Clamp: záporný medián (rozbitá data) nesmí α stáhnout pod 0, nad 1 nejde
    assert update_alpha(None, -0.4) == 0.0
    assert update_alpha(None, 1.7) == 1.0
    # EMA λ 0.3: 0.4 + 0.3·(0.6 − 0.4)
    assert update_alpha(0.4, 0.6) == pytest.approx(0.46)


def test_repository_roundtrip(tmp_path: Path) -> None:
    repo = FaAlphaRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'db.sqlite'}"))
    repo.ensure_schema()
    assert repo.get("ES") is None
    assert repo.list_all() == []
    assert not repo.history_exists("ES", PREV)

    point = AlphaCalibrationPoint(samples=6, ratio_median=0.45, ratio_buy=0.4, ratio_sell=0.5)
    now = dt.datetime(2026, 8, 6, 12, 0, tzinfo=dt.UTC)
    repo.record(
        "ES", PREV, EXPIRY, point, alpha_after=0.45, days=1, now=now, source=SOURCE_MIDPOINT
    )

    state = repo.get("ES")
    assert state is not None
    assert state.alpha == 0.45 and state.days == 1
    assert repo.history_exists("ES", PREV)
    # Upsert je idempotentní — opakovaný zápis stejného dne nic nerozbije
    repo.record(
        "ES", PREV, EXPIRY, point, alpha_after=0.45, days=1, now=now, source=SOURCE_MIDPOINT
    )
    assert len(repo.list_all()) == 1


def test_netflow_at_cutoff_rez_21_utc(tmp_path: Path) -> None:
    """Kumulativ po 21:00 UTC patří další seanci — do kalibrace nesmí."""
    settings = Settings(data_dir=tmp_path)
    writer = SnapshotWriter(settings)
    before = dt.datetime.combine(PREV, dt.time(20, 59), tzinfo=dt.UTC)
    after = dt.datetime.combine(PREV, dt.time(21, 30), tzinfo=dt.UTC)
    path = writer.write_netflow(
        "ES",
        EXPIRY,
        PREV,
        [
            NetFlowRow(
                ts_min=before, strike=7500.0, right="C", net_volume=80.0, source=SOURCE_MIDPOINT
            ),
            NetFlowRow(
                ts_min=after, strike=7500.0, right="C", net_volume=999.0, source=SOURCE_MIDPOINT
            ),
        ],
    )
    assert path is not None
    assert netflow_at_cutoff(path, PREV) == {(7500.0, "C"): 80.0}


def test_netflow_at_cutoff_strana_vracena_na_nulu(tmp_path: Path) -> None:
    """#1582: strana s návratem na 0 před řezem vstoupí do kalibrace s 0, ne s 80."""
    settings = Settings(data_dir=tmp_path)
    earlier = dt.datetime.combine(PREV, dt.time(20, 24), tzinfo=dt.UTC)
    path = SnapshotWriter(settings).write_netflow(
        "ES",
        EXPIRY,
        PREV,
        [
            NetFlowRow(
                ts_min=earlier, strike=7500.0, right="P", net_volume=80.0, source=SOURCE_MIDPOINT
            ),
            NetFlowRow(
                ts_min=earlier + dt.timedelta(minutes=3),
                strike=7500.0,
                right="P",
                net_volume=0.0,
                source=SOURCE_MIDPOINT,
            ),
        ],
    )
    assert path is not None
    assert netflow_at_cutoff(path, PREV) == {(7500.0, "P"): 0.0}


def test_netflow_at_cutoff_stara_partice_a_radek_presne_v_rezu(tmp_path: Path) -> None:
    """#1460: partice bez sloupce `source` se čte přes `schema=`; řádek 21:00 do řezu patří."""
    path = tmp_path / "netflow.parquet"
    at_cutoff = dt.datetime.combine(PREV, dt.time(21, 0), tzinfo=dt.UTC)
    pq.write_table(
        pa.table(
            {
                "ts_min": pa.array(
                    [
                        at_cutoff - dt.timedelta(minutes=1),
                        at_cutoff,
                        at_cutoff + dt.timedelta(minutes=1),
                    ],
                    pa.timestamp("us", tz="UTC"),
                ),
                "strike": [7500.0, 7500.0, 7500.0],
                "right": ["C", "C", "C"],
                "net_volume": [70.0, 80.0, 999.0],
            }
        ),
        path,
    )
    assert netflow_at_cutoff(path, PREV) == {(7500.0, "C"): 80.0}


def _after_pub(settings: Settings, day: dt.date) -> dt.datetime:
    """IBKR čtení po publikaci CME dne (#1605) — vzor test_recompute_fa."""
    return settings.oi_publication_utc(day) + dt.timedelta(hours=1)


def _seed_day(
    settings: Settings,
    oi_repo: OIEodRepository,
    *,
    net: float = 100.0,
    doi: float = 40.0,
    expiry: str = EXPIRY,
) -> None:
    """Včerejší netflow (5 stran) + IBKR archivy obou dnů po publikaci s ΔOI = doi na stranu."""
    writer = SnapshotWriter(settings)
    ts = dt.datetime.combine(PREV, dt.time(20, 0), tzinfo=dt.UTC)
    strikes = [7500.0 + 10 * i for i in range(5)]
    writer.write_netflow(
        "ES",
        expiry,
        PREV,
        [
            NetFlowRow(ts_min=ts, strike=strike, right="C", net_volume=net, source=SOURCE_MIDPOINT)
            for strike in strikes
        ],
    )
    oi_repo.upsert_many(
        [OIRecord("ES", expiry, strike, "C", PREV, 1000.0) for strike in strikes],
        _after_pub(settings, PREV),
    )
    oi_repo.upsert_many(
        [OIRecord("ES", expiry, strike, "C", TODAY, 1000.0 + doi) for strike in strikes],
        _after_pub(settings, TODAY),
    )


def _collect(
    settings: Settings,
    oi_repo: OIEodRepository,
    alpha_repo: FaAlphaRepository,
    day: dt.date = TODAY,
) -> AlphaCalibrationResult | None:
    """Kolektor s publikací CME z konfigurace — jako pipeline (#1605).

    Cesta v5 (α z netflow) výslovně: od bumpu v6 (#1403) je výchozí ρ z objemu
    (`_collect_v6`), testy níž drží regresi v5 (ADR-0061 bod 6)."""
    return collect_alpha_calibration(
        "ES",
        settings.derived_dir,
        oi_repo,
        alpha_repo,
        day,
        publication_utc=settings.oi_publication_utc,
        snapshots_dir=settings.snapshots_dir,
        cumdelta_prints=False,
    )


def _repos(tmp_path: Path) -> tuple[Settings, OIEodRepository, FaAlphaRepository]:
    settings = Settings(data_dir=tmp_path)
    db = create_engine(f"sqlite+pysqlite:///{tmp_path / 'db.sqlite'}")
    oi_repo = OIEodRepository(db)
    oi_repo.ensure_schema()
    alpha_repo = FaAlphaRepository(db)
    alpha_repo.ensure_schema()
    return settings, oi_repo, alpha_repo


def test_calibrate_alpha_chybejici_doi_neni_nula() -> None:
    """Strana bez ΔOI v archivech se přeskočí — nula by medián stáhla k 0 (#1172)."""
    netflow = {(7500.0 + i, "C"): 100.0 for i in range(6)}
    doi = {(7500.0 + i, "C"): 40.0 for i in range(5)}  # šestá strana bez klíče
    point = calibrate_alpha(netflow, doi)
    assert point is not None
    assert point.samples == 5 and point.ratio_median == pytest.approx(0.4)
    # Bez jediného klíče ΔOI vzorek nestačí — None místo falešné nuly
    assert calibrate_alpha(netflow, {}) is None


def test_collect_preskoci_0dte_retez_a_shodne_archivy(tmp_path: Path) -> None:
    """Řetěz expirující v den netflow nemá ΔOI do D+1; shodné archivy = žádný bod (#1172)."""
    settings, oi_repo, alpha_repo = _repos(tmp_path)
    _seed_day(settings, oi_repo, expiry=EXPIRY_0DTE)
    assert _collect(settings, oi_repo, alpha_repo, TODAY) is None
    assert alpha_repo.get("ES") is None
    # Platná expirace, ale archiv D+1 nese tytéž hodnoty (ΔOI všude 0) — také nic
    _seed_day(settings, oi_repo, doi=0.0)
    assert _collect(settings, oi_repo, alpha_repo, TODAY) is None
    assert alpha_repo.get("ES") is None


def test_collect_zaporny_median_alfu_nemeni(tmp_path: Path) -> None:
    """Medián ≤ 0 jde do historie pro audit, ale α ani počet dnů nemění (#1172)."""
    settings, oi_repo, alpha_repo = _repos(tmp_path)
    point = AlphaCalibrationPoint(samples=6, ratio_median=0.45, ratio_buy=0.45, ratio_sell=None)
    alpha_repo.record(
        "ES",
        PREV - dt.timedelta(days=1),
        EXPIRY,
        point,
        alpha_after=0.45,
        days=1,
        source=SOURCE_MIDPOINT,
    )
    _seed_day(settings, oi_repo, doi=-40.0)
    assert _collect(settings, oi_repo, alpha_repo, TODAY) is None
    state = alpha_repo.get("ES")
    assert state is not None and state.alpha == 0.45 and state.days == 1
    assert alpha_repo.history_exists("ES", PREV)  # bod zapsán pro audit, dedup drží


def test_collect_zaporny_median_bez_kalibrace_nezalozi_alfu_nula(tmp_path: Path) -> None:
    """#1314: symbol bez kalibrace dostal po bodu s mediánem ≤ 0 řádek `fa_alpha`
    s α 0 — runtime ho převzal a FA vrstva NQ byla vypnutá. Teď jen historie."""
    settings, oi_repo, alpha_repo = _repos(tmp_path)
    _seed_day(settings, oi_repo, doi=-40.0)
    assert _collect(settings, oi_repo, alpha_repo, TODAY) is None
    assert alpha_repo.get("ES") is None  # α zůstává z konfigurace
    assert alpha_repo.history_exists("ES", PREV)  # audit bodu zůstává


def test_collect_alpha_calibration_a_dedup(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    db = create_engine(f"sqlite+pysqlite:///{tmp_path / 'db.sqlite'}")
    oi_repo = OIEodRepository(db)
    oi_repo.ensure_schema()
    alpha_repo = FaAlphaRepository(db)
    alpha_repo.ensure_schema()
    _seed_day(settings, oi_repo)

    result = _collect(settings, oi_repo, alpha_repo, TODAY)
    assert result is not None
    assert result.day == PREV and result.expiry == EXPIRY
    assert result.point.ratio_median == pytest.approx(0.4)  # 40/100 na každé straně
    assert result.alpha_after == pytest.approx(0.4)  # první bod jde přímo
    assert result.days == 1

    # Idempotence: den už je v historii → druhý běh bod nepřidá
    assert _collect(settings, oi_repo, alpha_repo, TODAY) is None
    state = alpha_repo.get("ES")
    assert state is not None and state.days == 1


def test_collect_bez_netflow_vraci_none(tmp_path: Path) -> None:
    """Bez řady netflow (žádný tok, starší den) kalibrace nic nedělá."""
    settings = Settings(data_dir=tmp_path / "data")
    db = create_engine(f"sqlite+pysqlite:///{tmp_path / 'db.sqlite'}")
    oi_repo = OIEodRepository(db)
    oi_repo.ensure_schema()
    alpha_repo = FaAlphaRepository(db)
    alpha_repo.ensure_schema()
    assert _collect(settings, oi_repo, alpha_repo, TODAY) is None


def test_collect_bere_netflow_sekundarniho_retezu(tmp_path: Path) -> None:
    """Den má netflow 0DTE (aktivní) i sekundárního řetězu (#1182): 0DTE nemá ΔOI
    a přeskočí se, bod vznikne ze sekundáru s expirací po dni netflow."""
    settings, oi_repo, alpha_repo = _repos(tmp_path)
    _seed_day(settings, oi_repo, expiry=EXPIRY_0DTE)
    _seed_day(settings, oi_repo)  # sekundární řetěz: EXPIRY > PREV
    result = _collect(settings, oi_repo, alpha_repo, TODAY)
    assert result is not None and result.expiry == EXPIRY and result.day == PREV
    assert result.point.ratio_median == pytest.approx(0.4)
    state = alpha_repo.get("ES")
    assert state is not None and state.alpha == pytest.approx(0.4) and state.days == 1


def test_collect_vikend_nic_pondeli_proti_patku(tmp_path: Path) -> None:
    """#1309: sobotní běh párovál páteční netflow s archivem soboty (kopie
    pátku) — fa_alpha_history 18. a 25. 9. 2026 má medián 0 a dedup dne pak
    zablokoval poctivý bod „pátek → pondělí". Víkend se nekalibruje."""
    settings, oi_repo, alpha_repo = _repos(tmp_path)
    friday, saturday, sunday = dt.date(2026, 9, 25), dt.date(2026, 9, 26), dt.date(2026, 9, 27)
    monday = dt.date(2026, 9, 28)
    expiry = "20261002"
    strikes = [7500.0 + 10 * i for i in range(5)]
    ts = dt.datetime.combine(friday, dt.time(20, 0), tzinfo=dt.UTC)
    SnapshotWriter(settings).write_netflow(
        "ES",
        expiry,
        friday,
        [
            NetFlowRow(
                ts_min=ts, strike=strike, right="C", net_volume=100.0, source=SOURCE_MIDPOINT
            )
            for strike in strikes
        ],
    )
    for day in (friday, saturday, sunday):  # víkendové klíče = kopie pátku
        oi_repo.upsert_many(
            [OIRecord("ES", expiry, strike, "C", day, 1000.0) for strike in strikes],
            _after_pub(settings, day),
        )
    # Sobotní archiv širší obálky — ΔOI „pátek → sobota" není všude 0
    oi_repo.upsert_many(
        [OIRecord("ES", expiry, 7600.0, "C", saturday, 500.0)], _after_pub(settings, saturday)
    )
    oi_repo.upsert_many(
        [OIRecord("ES", expiry, strike, "C", monday, 1040.0) for strike in strikes],
        _after_pub(settings, monday),
    )

    for weekend in (saturday, sunday):
        assert _collect(settings, oi_repo, alpha_repo, weekend) is None
    assert not alpha_repo.history_exists("ES", friday)

    result = _collect(settings, oi_repo, alpha_repo, monday)
    assert result is not None and result.day == friday and result.expiry == expiry
    assert result.point.ratio_median == pytest.approx(0.4)


def _write_netflow(settings: Settings, strikes: list[float], net: float = 100.0) -> None:
    ts = dt.datetime.combine(PREV, dt.time(20, 0), tzinfo=dt.UTC)
    SnapshotWriter(settings).write_netflow(
        "ES",
        EXPIRY,
        PREV,
        [
            NetFlowRow(ts_min=ts, strike=strike, right="C", net_volume=net, source=SOURCE_MIDPOINT)
            for strike in strikes
        ],
    )


def _records(strikes: list[float], day: dt.date, oi: float) -> list[OIRecord]:
    return [OIRecord("ES", EXPIRY, strike, "C", day, oi) for strike in strikes]


def test_collect_bere_jen_strany_s_ibkr_merenim_po_publikaci(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """#1605: strana, kterou archiv D+1 nese z tasty (nebo z IBKR čtení před
    publikací), má předchozí publikaci a ΔOI přesně 0 — NQ od 5. 10. 2026
    medián 0 v 7 z 8 bodů. Je to „nevíme", ne nula: do bodu nesmí. Strana
    tasty → IBKR po publikaci se počítá. Varianta B+: strana D bere i tasty
    řádek obnovený po publikaci D, tasty řádek D před publikací ne; strana
    D+1 tasty ani po publikaci D+1 (obnova = ΔOI ≠ 0, vypadly by nuly)."""
    settings, oi_repo, alpha_repo = _repos(tmp_path)
    measured = [7500.0 + 10 * i for i in range(5)]  # IBKR po publikaci obou dnů, ΔOI +40
    tasty_only = [7600.0 + 10 * i for i in range(6)]  # D+1 jen tasty z 00:00, ΔOI 0
    pre_pub = [7700.0]  # D+1 jen IBKR čtení před publikací, ΔOI 0
    tasty_then_ibkr = [7800.0]  # D+1 tasty z 00:00, pak IBKR po publikaci, ΔOI +40
    no_before = [7900.0]  # D jen tasty před publikací D — strana D bez měření
    tasty_d = [7910.0, 7920.0]  # D tasty obnovené po publikaci D (B+), ΔOI +40
    tasty_d1 = [7930.0]  # D+1 tasty obnovené po publikaci D+1 — strana D+1 bez měření
    _write_netflow(
        settings,
        measured + tasty_only + pre_pub + tasty_then_ibkr + no_before + tasty_d + tasty_d1,
    )
    midnight = dt.datetime.combine(TODAY, dt.time(0, 0), tzinfo=dt.UTC)
    prev_midnight = dt.datetime.combine(PREV, dt.time(0, 0), tzinfo=dt.UTC)
    evening = dt.timedelta(hours=8)  # večerní blok tasty (~20:00 UTC)

    oi_repo.upsert_many(
        _records(measured + tasty_only + pre_pub + tasty_then_ibkr + tasty_d1, PREV, 1000.0),
        _after_pub(settings, PREV),
    )
    oi_repo.upsert_many(_records(no_before, PREV, 1000.0), tasty_captured_ts=prev_midnight)
    oi_repo.upsert_many(
        _records(tasty_d, PREV, 1000.0),
        tasty_captured_ts=settings.oi_publication_utc(PREV) + evening,
    )
    oi_repo.upsert_many(
        _records(tasty_only + tasty_then_ibkr, TODAY, 1000.0), tasty_captured_ts=midnight
    )
    oi_repo.upsert_many(
        _records(tasty_d1, TODAY, 1040.0),
        tasty_captured_ts=settings.oi_publication_utc(TODAY) + evening,
    )
    oi_repo.upsert_many(
        _records(pre_pub, TODAY, 1000.0), dt.datetime.combine(TODAY, dt.time(5, 1), tzinfo=dt.UTC)
    )
    oi_repo.upsert_many(
        _records(measured + tasty_then_ibkr + no_before + tasty_d, TODAY, 1040.0),
        _after_pub(settings, TODAY),
    )

    with caplog.at_level(logging.INFO, logger="gexlens_engine.storage.fa_calibration"):
        result = _collect(settings, oi_repo, alpha_repo)

    # Před opravou (values_for obou dnů): 17 stran, 7 nul + 10× 0,4 → medián 0,4,
    # rozliší ho počet stran a log; teď jen strany změřené po publikaci v obou dnech
    assert result is not None
    assert result.point.samples == 8
    assert result.point.ratio_median == pytest.approx(0.4)
    assert (
        "17 stran s |net| ≥ 25, vyřazeno 9 bez měření po publikaci (D+1 8, D 1), D tasty 2"
        in caplog.text
    )


def test_collect_strana_bez_mereni_d1_nedava_zaporne_doi(tmp_path: Path) -> None:
    """#1605 past: filtr jen vstupu a sjednocení klíčů s nulou za chybějící
    hodnotu by ze strany změřené v D a nezměřené v D+1 udělal ΔOI = −oi_before."""
    settings, oi_repo, alpha_repo = _repos(tmp_path)
    measured = [7500.0 + 10 * i for i in range(5)]
    unmeasured = [7600.0 + 10 * i for i in range(6)]  # D změřeno, D+1 jen tasty
    _write_netflow(settings, measured + unmeasured)
    oi_repo.upsert_many(_records(measured + unmeasured, PREV, 1000.0), _after_pub(settings, PREV))
    oi_repo.upsert_many(
        _records(unmeasured, TODAY, 1000.0),
        tasty_captured_ts=dt.datetime.combine(TODAY, dt.time(0, 0), tzinfo=dt.UTC),
    )
    oi_repo.upsert_many(_records(measured, TODAY, 1040.0), _after_pub(settings, TODAY))

    result = _collect(settings, oi_repo, alpha_repo)

    # Past by dala 6× −10 proti 5× 0,4 (medián −10); stará cesta 6 nul (medián 0)
    assert result is not None
    assert result.point.samples == 5
    assert result.point.ratio_median == pytest.approx(0.4)


def test_collect_bez_ibkr_mereni_bod_nezamkne_a_zkusi_znovu(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """#1605: D+1 zatím jen z tasty → žádný bod, žádný zámek dne (ani „archivy
    shodné"); jakmile IBKR po publikaci změří, další běh bod spočte."""
    settings, oi_repo, alpha_repo = _repos(tmp_path)
    strikes = [7500.0 + 10 * i for i in range(5)]
    _write_netflow(settings, strikes)
    oi_repo.upsert_many(_records(strikes, PREV, 1000.0), _after_pub(settings, PREV))
    oi_repo.upsert_many(
        _records(strikes, TODAY, 1000.0),
        tasty_captured_ts=dt.datetime.combine(TODAY, dt.time(0, 0), tzinfo=dt.UTC),
    )

    with caplog.at_level(logging.INFO, logger="gexlens_engine.storage.fa_calibration"):
        assert _collect(settings, oi_repo, alpha_repo) is None
    assert "bez stran změřených po publikaci v obou dnech" in caplog.text
    assert "jsou shodné" not in caplog.text
    assert not alpha_repo.history_exists("ES", PREV)

    oi_repo.upsert_many(_records(strikes, TODAY, 1040.0), _after_pub(settings, TODAY))
    result = _collect(settings, oi_repo, alpha_repo)
    assert result is not None
    assert result.point.ratio_median == pytest.approx(0.4)


def test_netflow_at_cutoff_zimni_cas_rez_22_utc(tmp_path: Path) -> None:
    """#1460: řez 16:00 CT — v zimě 22:00 UTC, kumulativ 21:30 UTC do dne patří."""
    winter = dt.date(2026, 11, 4)
    rows = [
        NetFlowRow(
            ts_min=dt.datetime.combine(winter, dt.time(hour, minute), tzinfo=dt.UTC),
            strike=7500.0,
            right="C",
            net_volume=net,
            source=SOURCE_MIDPOINT,
        )
        for hour, minute, net in ((20, 59, 80.0), (21, 30, 95.0), (22, 30, 999.0))
    ]
    path = SnapshotWriter(Settings(data_dir=tmp_path)).write_netflow("ES", EXPIRY, winter, rows)
    assert path is not None
    assert netflow_at_cutoff(path, winter) == {(7500.0, "C"): 95.0}


# ── Kalibrace α pod tisky (mechanika v6, ADR-0057 bod 6, #1460) ──────────

EXPIRY_D1 = "20260806"  # sekundár celou seanci PREV (5. 8.) = kalibrovaný řetěz (#1580)
EXPIRY_D2 = "20260807"  # sekundár až po rollu v settle (#1580)
STRIKES = [7500.0 + 10 * i for i in range(5)]
MIN = dt.timedelta(minutes=1)


def _write_snapshots(
    settings: Settings,
    expiry: str,
    session: dt.date = PREV,
    *,
    volume: float = 100.0,
    every: int = 3,
    end: dt.datetime | None = None,
    gap: tuple[dt.datetime, dt.datetime] | None = None,
    no_volume_from: dict[float, dt.datetime] | None = None,
    volumes: dict[float, float] | None = None,
    strikes: list[float] = STRIKES,
) -> None:
    """Snímky řetězu za seanci: call strany s kumulativem rostoucím k `volume` v řezu.

    Sekundár píše každý `every`. cyklus (#1182), minutu řezu vždy (aktivní řetěz
    po rollu); `end` = poslední minuta se snímkem, `gap` = úsek [od, do) bez
    snímku, `no_volume_from` = strike → od kdy nese volume NULL (tasty fallback),
    `volumes` = objem strany v řezu místo `volume`.
    """
    start = session_bounds(session)[0]
    cutoff = daily_close_utc(session)
    last = end or cutoff
    total = (cutoff - start) / MIN
    by_day: dict[dt.date, dict[str, list[object]]] = {}
    moment = start
    while moment <= last:
        due = ((moment - start) // MIN) % every == 0 or moment == cutoff
        if due and (gap is None or not gap[0] <= moment < gap[1]):
            cols = by_day.setdefault(
                moment.date(), {"ts_min": [], "strike": [], "right": [], "volume": []}
            )
            for strike in strikes:
                frozen = no_volume_from is not None and moment >= no_volume_from.get(
                    strike, cutoff + MIN
                )
                final = (volumes or {}).get(strike, volume)
                cols["ts_min"].append(moment)
                cols["strike"].append(strike)
                cols["right"].append("C")
                cols["volume"].append(None if frozen else final * ((moment - start) / MIN) / total)
        moment += MIN
    for day, cols in by_day.items():
        path = settings.snapshots_dir / "ES" / expiry / f"{day.isoformat()}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(
            pa.table(
                {
                    "ts_min": pa.array(cols["ts_min"], pa.timestamp("us", tz="UTC")),
                    "strike": pa.array(cols["strike"], pa.float64()),
                    "right": pa.array(cols["right"], pa.string()),
                    "volume": pa.array(cols["volume"], pa.float64()),
                }
            ),
            path,
        )


def _seed_rho(
    settings: Settings,
    oi_repo: OIEodRepository,
    expiry: str = EXPIRY_D1,
    session: dt.date = PREV,
    next_day: dt.date = TODAY,
    *,
    doi: float = 40.0,
    strikes: list[float] = STRIKES,
    doi_by_strike: dict[float, float] | None = None,
    **snapshots: object,
) -> None:
    """Snímky seance + IBKR archivy obou dnů po publikaci: OI 1000 → 1000 + ΔOI."""
    _write_snapshots(settings, expiry, session, strikes=strikes, **snapshots)  # type: ignore[arg-type]
    oi_repo.upsert_many(
        [OIRecord("ES", expiry, strike, "C", session, 1000.0) for strike in strikes],
        _after_pub(settings, session),
    )
    oi_repo.upsert_many(
        [
            OIRecord(
                "ES", expiry, strike, "C", next_day, 1000.0 + (doi_by_strike or {}).get(strike, doi)
            )
            for strike in strikes
        ],
        _after_pub(settings, next_day),
    )


def _collect_v6(
    settings: Settings,
    oi_repo: OIEodRepository,
    alpha_repo: FaAlphaRepository,
    today: dt.date = TODAY,
) -> AlphaCalibrationResult | None:
    return collect_alpha_calibration(
        "ES",
        settings.derived_dir,
        oi_repo,
        alpha_repo,
        today,
        publication_utc=settings.oi_publication_utc,
        snapshots_dir=settings.snapshots_dir,
        cumdelta_prints=True,
    )


def _at(day: dt.date, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour, minute), tzinfo=dt.UTC)


def test_calibrate_rho_median_a_prahy() -> None:
    """ADR-0061 bod 2: medián ΔOI/V přes strany s V ≥ 25 a ΔOI, aspoň 5 stran, bez buy/sell."""
    volumes = {(7500.0 + 10 * i, "C"): 100.0 for i in range(5)}
    doi = {key: 30.0 + 5 * i for i, key in enumerate(volumes)}  # poměry 0,30–0,50
    volumes[(7600.0, "C")] = 24.0  # pod prahem — poměr 10 by medián vytáhl
    doi[(7600.0, "C")] = 240.0
    volumes[(7610.0, "C")] = 500.0  # bez ΔOI (neměřená po publikaci) = nevíme, ne 0
    point = calibrate_rho(volumes, doi)
    assert point is not None and point.samples == 5
    assert point.ratio_median == pytest.approx(0.4)
    assert point.ratio_buy is None and point.ratio_sell is None
    del volumes[(7500.0, "C")]
    assert calibrate_rho(volumes, doi) is None  # 4 strany < MIN_SAMPLES


def test_session_volume_coverage_maximum_a_strana_bez_objemu() -> None:
    """V = maximum kumulativu do řezu; strana bez objemu v posledním úseku je „nevíme“ (#1668)."""
    start, cutoff = session_bounds(PREV)[0], daily_close_utc(PREV)
    c1, c2, c3 = (7500.0, "C"), (7510.0, "C"), (7520.0, "C")
    rows: list[tuple[dt.datetime | None, float, str, float | None]] = []
    moment = start
    while moment <= cutoff + 30 * MIN:  # řádky po řezu (pauza) se nečtou
        rows.append((moment, 7500.0, "C", 900.0 if moment == _at(PREV, 15) else 600.0))
        # NQ 29. 9.: objem 24 v 18:09 UTC, pak bez objemu až do řezu
        rows.append((moment, 7510.0, "C", 24.0 if moment <= _at(PREV, 18, 9) else None))
        if moment <= _at(PREV, 15):  # strana vypadla z pásma v 15:00 UTC
            rows.append((moment, 7520.0, "C", 300.0))
        moment += MIN
    coverage = session_volume_coverage(rows, start, cutoff)
    assert coverage.volumes == {c1: 900.0}  # propad 900 → 600 maximum nezmenší (#1602)
    assert coverage.unknown == {c2, c3}
    assert coverage.last == cutoff and coverage.close == cutoff
    assert coverage.longest_gap == 0 and coverage.problem() is None


def test_session_volume_coverage_zkracena_seance_konci_v_12_15_ct() -> None:
    """Pátek po Thanksgiving: obchod do 12:15 CT = 18:15 UTC, konec dne je tam, ne v řezu."""
    session = dt.date(2026, 11, 27)
    start, cutoff = session_bounds(session)[0], daily_close_utc(session)
    rows: list[tuple[dt.datetime | None, float, str, float | None]] = []
    moment = start
    while moment < _at(session, 18, 15):
        rows.append((moment, 7500.0, "C", 100.0))
        moment += MIN
    coverage = session_volume_coverage(rows, start, cutoff)
    assert coverage.close == _at(session, 18, 15)
    assert coverage.problem() is None and coverage.volumes == {(7500.0, "C"): 100.0}


@pytest.mark.parametrize(
    ("session", "d1", "d2", "today"),
    [
        (PREV, EXPIRY_D1, EXPIRY_D2, TODAY),  # léto: řez 21:00 UTC
        (dt.date(2026, 11, 4), "20261105", "20261106", dt.date(2026, 11, 5)),  # zima: 22:00 UTC
    ],
    ids=["leto", "zima"],
)
def test_v6_rho_z_objemu_retezu_d1_ne_d2(
    tmp_path: Path, session: dt.date, d1: str, d2: str, today: dt.date
) -> None:
    """ADR-0061: ρ = medián ΔOI/V řetězu, který byl celou seanci sekundárem (#1580)."""
    settings, oi_repo, alpha_repo = _repos(tmp_path)
    _seed_rho(settings, oi_repo, d1, session, today, doi=40.0)
    _seed_rho(settings, oi_repo, d2, session, today, doi=90.0)
    result = _collect_v6(settings, oi_repo, alpha_repo, today)
    assert result is not None and result.expiry == d1 and result.day == session
    assert result.point.ratio_median == pytest.approx(0.4)
    assert result.point.ratio_buy is None and result.point.ratio_sell is None
    assert result.alpha_after == pytest.approx(0.4) and result.days == 1
    state = alpha_repo.get("ES")
    assert state is not None and state.source == SOURCE_VOLUME
    assert _collect_v6(settings, oi_repo, alpha_repo, today) is None  # dedup history_exists


def test_v5_bere_nejvzdalenejsi_retez_d2_beze_zmeny(tmp_path: Path) -> None:
    """Regrese v5 (ADR-0061 bod 6): pod v5 vyhrává D+2 s netflow po rollu (#1580)."""
    settings, oi_repo, alpha_repo = _repos(tmp_path)
    _seed_day(settings, oi_repo, doi=40.0, expiry=EXPIRY_D1)
    _seed_day(settings, oi_repo, doi=90.0, expiry=EXPIRY_D2)
    legacy = _collect(settings, oi_repo, alpha_repo)
    assert legacy is not None and legacy.expiry == EXPIRY_D2
    assert legacy.point.ratio_median == pytest.approx(0.9)
    state = alpha_repo.get("ES")
    assert state is not None and state.source == SOURCE_MIDPOINT


def test_v6_strana_s_prevahou_prodeje_a_rostoucim_oi_da_kladny_bod(tmp_path: Path) -> None:
    """#1612: OI roste i na stranách s převahou prodeje — ρ znaménko toku nečte (netflow žádné)."""
    settings, oi_repo, alpha_repo = _repos(tmp_path)
    _seed_rho(settings, oi_repo, doi=40.0)
    writer = SnapshotWriter(settings)  # net z tisků záporný: model α·net by dal medián < 0
    writer.write_netflow(
        "ES",
        EXPIRY_D1,
        PREV,
        [
            NetFlowRow(_at(PREV, 20, 58), strike, "C", -100.0, source=SOURCE_AGGRESSOR)
            for strike in STRIKES
        ],
    )
    result = _collect_v6(settings, oi_repo, alpha_repo)
    assert result is not None and result.point.ratio_median == pytest.approx(0.4)


def test_v6_bez_snimku_d1_nehleda_nahradni_retez(tmp_path: Path) -> None:
    """Řetěz D+1 bez snímků seance bod nedá — D+2 ho nenahradí (#1580)."""
    settings, oi_repo, alpha_repo = _repos(tmp_path)
    _seed_rho(settings, oi_repo, EXPIRY_D2)
    (settings.snapshots_dir / "ES" / EXPIRY_D1).mkdir(parents=True)  # adresář bez partic
    assert _collect_v6(settings, oi_repo, alpha_repo) is None
    assert not alpha_repo.history_exists("ES", PREV)


@pytest.mark.parametrize(
    ("gap", "blocks"),
    [
        ((_at(PREV, 14), _at(PREV, 14, SNAPSHOT_GAP_MAX_MIN)), False),  # na hranici tolerance
        ((_at(PREV, 14), _at(PREV, 14, SNAPSHOT_GAP_MAX_MIN + 1)), True),
        # začátek seance v partici D−1 (PREV − 1 den, 22:30 UTC)
        ((_at(PREV - dt.timedelta(days=1), 22, 30), _at(PREV - dt.timedelta(days=1), 23)), True),
    ],
    ids=["10-min", "11-min", "partice-d-1"],
)
def test_v6_den_s_mezerou_snimku_bod_neda(
    tmp_path: Path, gap: tuple[dt.datetime, dt.datetime], blocks: bool
) -> None:
    """ADR-0061 bod 3: mezera snímků řetězu za otevřeného trhu delší než tolerance = bez bodu."""
    settings, oi_repo, alpha_repo = _repos(tmp_path)
    _seed_rho(settings, oi_repo, every=1, gap=gap)
    assert (_collect_v6(settings, oi_repo, alpha_repo) is None) is blocks
    assert alpha_repo.history_exists("ES", PREV) is not blocks


@pytest.mark.parametrize(
    ("end", "blocks"),
    [
        (_at(PREV, 14, 18), True),  # NQ 9. 10. 2026: data končí 14:18 UTC
        (_at(PREV, 21) - SNAPSHOT_END_TOLERANCE_MIN * MIN, False),
        (_at(PREV, 21) - (SNAPSHOT_END_TOLERANCE_MIN + 1) * MIN, True),
    ],
    ids=["9-10-useknuty", "5-min-pred-rezem", "6-min-pred-rezem"],
)
def test_v6_useknuty_den_bod_neda(tmp_path: Path, end: dt.datetime, blocks: bool) -> None:
    """ADR-0061 bod 3: snímky končí před řezem (21:00 UTC v létě) = den bez bodu."""
    settings, oi_repo, alpha_repo = _repos(tmp_path)
    _seed_rho(settings, oi_repo, every=1, end=end)
    assert (_collect_v6(settings, oi_repo, alpha_repo) is None) is blocks
    assert alpha_repo.history_exists("ES", PREV) is not blocks


def test_v6_strana_bez_objemu_pred_rezem_je_nevime(tmp_path: Path) -> None:
    """#1668: strana bez objemu v posledním úseku před řezem vypadne, nebere se 0 ani staré maximum.

    Šestá strana má objem jen do 18:09 UTC (max 24·…) a ΔOI 500 — jako 0 by dala poměr ∞,
    se starým maximem poměr ~0,9; obojí by medián posunulo.
    """
    settings, oi_repo, alpha_repo = _repos(tmp_path)
    strikes = [*STRIKES, 7600.0]
    _seed_rho(
        settings,
        oi_repo,
        strikes=strikes,
        doi_by_strike={7600.0: 500.0},
        no_volume_from={7600.0: _at(PREV, 18, 10)},
        volumes={7600.0: 600.0},
    )
    result = _collect_v6(settings, oi_repo, alpha_repo)
    assert result is not None and result.point.samples == 5
    assert result.point.ratio_median == pytest.approx(0.4)


def test_v6_strana_pod_25_kontraktu_se_vyradi_a_pod_5_stran_bod_nevznikne(
    tmp_path: Path,
) -> None:
    """ADR-0061 bod 2: V < 25 strana nevstoupí; zbudou 4 strany → bod nevznikne."""
    settings, oi_repo, alpha_repo = _repos(tmp_path)
    _seed_rho(settings, oi_repo, volumes={STRIKES[0]: 24.0})
    assert _collect_v6(settings, oi_repo, alpha_repo) is None
    assert not alpha_repo.history_exists("ES", PREV)


def test_v6_median_nula_rho_nemeni(tmp_path: Path) -> None:
    """Medián ≤ 0 ρ ani počet dnů nemění; bod jde do historie pro audit (#1172)."""
    settings, oi_repo, alpha_repo = _repos(tmp_path)
    old = AlphaCalibrationPoint(samples=20, ratio_median=0.3, ratio_buy=None, ratio_sell=None)
    alpha_repo.record("ES", PREV - dt.timedelta(days=1), EXPIRY, old, 0.3, 6, source=SOURCE_VOLUME)
    _seed_rho(settings, oi_repo, doi=-10.0)
    assert _collect_v6(settings, oi_repo, alpha_repo) is None
    state = alpha_repo.get("ES")
    assert state is not None and (state.alpha, state.days) == (0.3, 6)
    assert alpha_repo.history_exists("ES", PREV)


def test_v6_stav_z_midpointu_se_resetuje(tmp_path: Path) -> None:
    """ADR-0061 bod 4: α z midpointu je jiná veličina — první bod ρ jde přímo, 1 den."""
    settings, oi_repo, alpha_repo = _repos(tmp_path)
    old = AlphaCalibrationPoint(samples=20, ratio_median=0.5, ratio_buy=None, ratio_sell=None)
    alpha_repo.record(
        "ES", PREV - dt.timedelta(days=1), EXPIRY, old, 0.505, 17, source=SOURCE_MIDPOINT
    )
    _seed_rho(settings, oi_repo, doi=40.0)
    result = _collect_v6(settings, oi_repo, alpha_repo)
    assert result is not None and result.alpha_after == pytest.approx(0.4) and result.days == 1
    state = alpha_repo.get("ES")
    assert state is not None and state.days == 1 and state.source == SOURCE_VOLUME


def test_pending_rho_sessions_po_poslednim_bodu() -> None:
    """Dopočet jen seancí po posledním bodu v pořadí dnů, přes víkend, nejvýš limit (#1668)."""
    monday = dt.date(2026, 8, 10)
    assert pending_rho_sessions(None, monday) == [dt.date(2026, 8, 7)]  # bez bodu: jen pátek
    assert pending_rho_sessions(dt.date(2026, 8, 5), monday) == [
        dt.date(2026, 8, 6),
        dt.date(2026, 8, 7),
    ]
    assert pending_rho_sessions(dt.date(2026, 8, 7), monday) == []
    assert pending_rho_sessions(dt.date(2026, 7, 1), monday, limit=2) == [
        dt.date(2026, 8, 6),
        dt.date(2026, 8, 7),
    ]


def test_v6_promeskany_den_dopocita_v_poradi_a_useknuty_neda(tmp_path: Path) -> None:
    """#1668 (NQ 8. 10. bez bodu): ranní job dopočítá seance po posledním bodu v pořadí dnů.

    Úterý 4. 8. je useknuté (bod nedá), středa 5. 8. platná: EMA ze stavu 0,3 přes 0,2.
    Další běh se k úterku nevrací — leží před posledním bodem.
    """
    settings, oi_repo, alpha_repo = _repos(tmp_path)
    monday, tuesday, wednesday = dt.date(2026, 8, 3), dt.date(2026, 8, 4), PREV
    old = AlphaCalibrationPoint(samples=20, ratio_median=0.3, ratio_buy=None, ratio_sell=None)
    alpha_repo.record("ES", monday, "20260804", old, 0.3, 6, source=SOURCE_VOLUME)
    _seed_rho(settings, oi_repo, "20260805", tuesday, wednesday, end=_at(tuesday, 14, 18))
    _seed_rho(settings, oi_repo, EXPIRY_D1, wednesday, TODAY, doi=20.0)
    result = _collect_v6(settings, oi_repo, alpha_repo)
    assert result is not None and result.day == wednesday and result.days == 7
    assert result.alpha_after == pytest.approx(0.3 + 0.3 * (0.2 - 0.3))
    assert not alpha_repo.history_exists("ES", tuesday)
    assert alpha_repo.last_history_day("ES") == wednesday

    # Promeškaný platný den: job 5. 8. neběžel, 6. 8. dopočítá úterý i středu v pořadí
    (tmp_path / "b").mkdir()
    settings, oi_repo, alpha_repo = _repos(tmp_path / "b")
    alpha_repo.record("ES", monday, "20260804", old, 0.3, 6, source=SOURCE_VOLUME)
    _seed_rho(settings, oi_repo, "20260805", tuesday, wednesday, doi=40.0)
    _seed_rho(settings, oi_repo, EXPIRY_D1, wednesday, TODAY, doi=20.0)
    result = _collect_v6(settings, oi_repo, alpha_repo)
    after_tuesday = 0.3 + 0.3 * (0.4 - 0.3)
    assert result is not None and result.day == wednesday and result.days == 8
    assert result.alpha_after == pytest.approx(after_tuesday + 0.3 * (0.2 - after_tuesday))
    assert alpha_repo.history_exists("ES", tuesday)


def test_v6_pondeli_proti_patku_a_vikend(tmp_path: Path) -> None:
    """Obchodní den (#1309): pondělí kalibruje pátek nad pondělním řetězem, víkend nic."""
    settings, oi_repo, alpha_repo = _repos(tmp_path)
    friday, saturday, monday = dt.date(2026, 8, 7), dt.date(2026, 8, 8), dt.date(2026, 8, 10)
    expiry = "20260810"  # sekundár celou páteční seanci = první expirace po pátku
    _seed_rho(settings, oi_repo, expiry, friday, monday, doi=40.0)
    oi_repo.upsert_many(
        [OIRecord("ES", expiry, strike, "C", saturday, 1000.0) for strike in STRIKES],
        _after_pub(settings, saturday),
    )
    assert _collect_v6(settings, oi_repo, alpha_repo, saturday) is None
    assert _collect_v6(settings, oi_repo, alpha_repo, dt.date(2026, 8, 9)) is None  # neděle
    result = _collect_v6(settings, oi_repo, alpha_repo, monday)
    assert result is not None and result.day == friday and result.expiry == expiry
    assert result.point.ratio_median == pytest.approx(0.4)


def test_live_alpha_brana_fa_pod_v6() -> None:
    """v5: uložená α (bez stavu konfigurace); v6: jen ρ z objemu s ≥ FA_MIN_DAYS, jinak 0."""
    now = dt.datetime(2026, 11, 2, 12, 0, tzinfo=dt.UTC)

    def state(days: int, source: str | None) -> FaAlphaState:
        return FaAlphaState("ES", alpha=0.3, days=days, updated_at=now, source=source)

    assert live_alpha(None, prints=False) is None
    assert live_alpha(state(17, None), prints=False) == 0.3
    assert live_alpha(None, prints=True) == 0.0
    assert live_alpha(state(17, SOURCE_MIDPOINT), prints=True) == 0.0
    assert live_alpha(state(17, None), prints=True) == 0.0  # NULL = midpoint
    assert live_alpha(state(17, SOURCE_AGGRESSOR), prints=True) == 0.0  # α z tisků (ADR-0057)
    assert live_alpha(state(FA_MIN_DAYS - 1, SOURCE_VOLUME), prints=True) == 0.0
    assert live_alpha(state(FA_MIN_DAYS, SOURCE_VOLUME), prints=True) == 0.3
    assert calibration_source(prints=True) == SOURCE_VOLUME
    assert calibration_source(prints=False) == SOURCE_MIDPOINT


def test_stav_s_nula_dny_bere_prvni_bod_primo(tmp_path: Path) -> None:
    """Ruční reset `set_state(α 0, 0 dnů)` nemá α k vyhlazení — bod 0,4 jde přímo, ne 0,12."""
    settings, oi_repo, alpha_repo = _repos(tmp_path)
    alpha_repo.set_state("ES", 0.0, 0)
    _seed_day(settings, oi_repo)
    result = _collect(settings, oi_repo, alpha_repo)
    assert result is not None and result.alpha_after == pytest.approx(0.4) and result.days == 1


def test_ensure_schema_doplni_sloupec_source_do_stare_tabulky(tmp_path: Path) -> None:
    """Migrace (#1460): tabulky bez `source` dostanou sloupec, staré řádky čtou NULL = midpoint."""
    from sqlalchemy import text

    db = create_engine(f"sqlite+pysqlite:///{tmp_path / 'db.sqlite'}")
    with db.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE fa_alpha (symbol VARCHAR(16) PRIMARY KEY, alpha FLOAT NOT NULL, "
                "days INTEGER NOT NULL, updated_at TIMESTAMP NOT NULL)"
            )
        )
        conn.execute(text("INSERT INTO fa_alpha VALUES ('ES', 0.505, 17, '2026-10-19 12:00:00')"))
    repo = FaAlphaRepository(db)
    repo.ensure_schema()
    repo.ensure_schema()  # idempotentní
    state = repo.get("ES")
    assert state is not None and state.alpha == 0.505 and state.source is None
    assert live_alpha(state, prints=True) == 0.0
