"""Testy ranní kalibrace α (#232 fáze 2): čistý výpočet, úložiště, sběr bodu."""

import datetime as dt
import logging
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import create_engine

from gexlens_engine.compute.cumdelta import SOURCE_MIDPOINT
from gexlens_engine.compute.facalibration import (
    AlphaCalibrationPoint,
    calibrate_alpha,
    update_alpha,
)
from gexlens_engine.config import Settings
from gexlens_engine.storage.fa_calibration import (
    AlphaCalibrationResult,
    FaAlphaRepository,
    collect_alpha_calibration,
    netflow_at_cutoff,
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
    repo.record("ES", PREV, EXPIRY, point, alpha_after=0.45, days=1, now=now)

    state = repo.get("ES")
    assert state is not None
    assert state.alpha == 0.45 and state.days == 1
    assert repo.history_exists("ES", PREV)
    # Upsert je idempotentní — opakovaný zápis stejného dne nic nerozbije
    repo.record("ES", PREV, EXPIRY, point, alpha_after=0.45, days=1, now=now)
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
    """Kolektor s publikací CME z konfigurace — jako pipeline (#1605)."""
    return collect_alpha_calibration(
        "ES",
        settings.derived_dir,
        oi_repo,
        alpha_repo,
        day,
        publication_utc=settings.oi_publication_utc,
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
    alpha_repo.record("ES", PREV - dt.timedelta(days=1), EXPIRY, point, alpha_after=0.45, days=1)
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
