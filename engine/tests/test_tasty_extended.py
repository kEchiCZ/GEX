"""Testy extended expirací (#616 4a): plán, disjunktnost, kadence, konsolidace."""

import datetime as dt

import pytest

from gexlens_engine.config import Settings
from gexlens_engine.storage.parquet_store import OiMissingRow, SnapshotRow
from gexlens_engine.tasty.extended import (
    ExpiryOverlapError,
    build_snapshot_rows,
    cadence_due,
    plan_extended_expiries,
    snapshot_minute,
    validate_disjoint,
)
from gexlens_engine.tasty.provider import TastyChainCache
from gexlens_engine.tasty.symbols import ChainSymbols

TODAY = dt.date(2026, 8, 22)
NOW = dt.datetime(2026, 8, 22, 14, 30, tzinfo=dt.UTC)


def chain_with(expiries: list[str]) -> ChainSymbols:
    by_contract = {(expiry, 6400.0, "C"): f"./E{i}C6400:XCME" for i, expiry in enumerate(expiries)}
    return ChainSymbols(product="ES", day=TODAY, by_contract=by_contract)


def test_plan_vynechava_ibkr_prosle_a_za_horizontem() -> None:
    chain = chain_with(["20260821", "20260824", "20260825", "20260918", "20260930"])

    planned = plan_extended_expiries(chain, {"20260824"}, symbol="ES", now=NOW, horizon_days=30)

    # 0821 prošlá, 0824 drží IBKR, 0930 za horizontem (30 dnů = do 21. 9.)
    assert planned == ["20260825", "20260918"]


def test_plan_bez_ibkr_pokryva_vse_v_horizontu() -> None:
    """Start bez TWS (#756): extended = kompletní množina (ADR-0025 dodatek)."""
    chain = chain_with(["20260824", "20260825"])

    assert plan_extended_expiries(chain, set(), symbol="ES", now=NOW, horizon_days=30) == [
        "20260824",
        "20260825",
    ]


def test_plan_vyrazuje_expiraci_v_settle_ne_o_pulnoci_utc() -> None:
    """#1759: po rollu IBKR v settle (#1331) vypršelá expirace do plánu nepřejde.

    2. 11. 2026 (SEČ, settle 21:00 UTC): roll přesunul IBKR na 1103 a denní
    filtr nechal 1102 v plánu do půlnoci — minuta 00:00 skončila v partici 3. 11.
    """
    chain = chain_with(["20261102", "20261103", "20261104"])
    after_roll = {"20261103"}

    def plan(now: dt.datetime, ibkr: set[str]) -> list[str]:
        return plan_extended_expiries(chain, ibkr, symbol="NQ", now=now, horizon_days=30)

    # Před settle (start bez TWS) dnešní expirace v plánu je
    assert plan(dt.datetime(2026, 11, 2, 20, 59, tzinfo=dt.UTC), set())[0] == "20261102"
    assert plan(dt.datetime(2026, 11, 2, 21, 0, tzinfo=dt.UTC), after_roll) == ["20261104"]
    assert plan(dt.datetime(2026, 11, 2, 23, 59, tzinfo=dt.UTC), after_roll) == ["20261104"]
    assert plan(dt.datetime(2026, 11, 3, 0, 0, tzinfo=dt.UTC), after_roll) == ["20261104"]


@pytest.mark.parametrize(
    ("expiry", "before", "at_settle"),
    [
        # SELČ: 16:00 ET = 20:00 UTC (21. 10. 2026)
        ("20261021", dt.datetime(2026, 10, 21, 19, 59), dt.datetime(2026, 10, 21, 20, 0)),
        # Zkrácená seance po Díkůvzdání: 13:00 ET = 18:00 UTC (#1308)
        ("20261127", dt.datetime(2026, 11, 27, 17, 59), dt.datetime(2026, 11, 27, 18, 0)),
        # Kvartální datum: týdenní série do 16:00 ET, ne SOQ 9:30 ET (#1366)
        ("20261218", dt.datetime(2026, 12, 18, 20, 59), dt.datetime(2026, 12, 18, 21, 0)),
    ],
)
def test_plan_hranice_settle(expiry: str, before: dt.datetime, at_settle: dt.datetime) -> None:
    chain = chain_with([expiry])

    def plan(now: dt.datetime) -> list[str]:
        return plan_extended_expiries(
            chain, set(), symbol="ES", now=now.replace(tzinfo=dt.UTC), horizon_days=30
        )

    assert plan(before) == [expiry]
    assert plan(at_settle) == []


def test_plan_kvartalni_datum_pinovany_kontrakt_konci_v_soq() -> None:
    """Pinovaný expirující kontrakt (#1191) roluje standardní třídu v SOQ 9:30 ET."""
    chain = chain_with(["20261218"])
    after_soq = dt.datetime(2026, 12, 18, 15, 0, tzinfo=dt.UTC)  # 10:00 ET

    def plan(symbol: str) -> list[str]:
        return plan_extended_expiries(chain, set(), symbol=symbol, now=after_soq, horizon_days=30)

    assert plan("ESZ6") == []
    assert plan("ES") == ["20261218"]  # kořenový ticker drží týdenní sérii


def test_validate_disjoint_prekryv_je_chyba() -> None:
    with pytest.raises(ExpiryOverlapError):
        validate_disjoint(["20260824", "20260825"], {"20260825"})
    validate_disjoint(["20260824"], {"20260825"})  # disjunktní projde


def test_cadence_odstupnovana() -> None:
    def due(expiry: str, minute_of_day: int) -> bool:
        return cadence_due(
            expiry, today=TODAY, minute_of_day=minute_of_day, near_days=7, far_interval_min=5
        )

    # Blízká expirace: každou minutu; vzdálená jen každou pátou
    assert due("20260825", 871) is True
    assert due("20260930", 871) is False
    assert due("20260930", 870) is True


def test_cadence_hranice_z_vychozi_konfigurace() -> None:
    """ADR-0058 (#1338 D-b): do 5 kalendářních dnů každou minutu, od 6. dne po 5 min."""
    # Výchozí hodnoty polí, ne Settings(): `.env` ani proměnné prostředí je nesmí přebít
    fields = Settings.model_fields
    near_days = fields["tasty_extended_near_days"].default
    far_interval_min = fields["tasty_extended_far_interval_min"].default
    assert near_days == 5

    def due(expiry: str, minute_of_day: int) -> bool:
        return cadence_due(
            expiry,
            today=TODAY,
            minute_of_day=minute_of_day,
            near_days=near_days,
            far_interval_min=far_interval_min,
        )

    # TODAY je so 22. 8.: 5 dnů = čt 27. 8., 6 dnů = pá 28. 8.
    assert due("20260827", 871) is True
    assert due("20260828", 871) is False
    assert due("20260828", 870) is True


def test_snapshot_minute_stitek_bez_lookaheadu() -> None:
    """#1734: štítek = plánovaná hranice, ale nikdy před minutou probuzení (point-in-time)."""
    planned = NOW
    # Včas a o pár ms dřív (nástěnné vs. monotonní hodiny): plánovaná hranice
    assert snapshot_minute(planned, planned + dt.timedelta(milliseconds=3)) == (planned, 0)
    assert snapshot_minute(planned, planned - dt.timedelta(milliseconds=2)) == (planned, 0)
    assert snapshot_minute(planned, planned + dt.timedelta(seconds=59)) == (planned, 0)
    # Pozdě o 2 min 10 s (uspaný hostitel): kotace z 14:32 nesmí nést štítek 14:30,
    # hranice 14:30 a 14:31 zůstanou bez snímku
    late = planned + dt.timedelta(minutes=2, seconds=10)
    assert snapshot_minute(planned, late) == (planned + dt.timedelta(minutes=2), 2)
    with pytest.raises(ValueError, match="časovou zónu"):
        snapshot_minute(planned, dt.datetime(2026, 8, 22, 14, 30))


def test_build_rows_bs_greeks_a_oimissing() -> None:
    """Čerstvá kotace bez dxFeed Greeks → BS dopočet; OI chybí → oimissing."""
    expiry = "20260930"
    streamer = "./ESU6C6400:XCME"
    chain = ChainSymbols(product="ES", day=TODAY, by_contract={(expiry, 6400.0, "C"): streamer})
    cache = TastyChainCache(clock=lambda: NOW)
    cache.on_event("Quote", [streamer, 120.0, 122.0, 5, 5])

    rows, oi_missing = build_snapshot_rows(
        chain, expiry, cache, ts_min=NOW, spot=6450.0, now_utc=NOW, max_age_s=120.0
    )

    assert len(rows) == 1
    row = rows[0]
    assert row.bid == 120.0 and row.ask == 122.0
    assert row.volume is None  # flows jsou výhradně IBKR
    assert row.iv is not None and 0.05 < row.iv < 1.0  # BS inverze z mid
    assert row.delta is not None and 0.4 < row.delta < 0.9  # ITM call
    assert row.oi == 0.0
    assert len(oi_missing) == 1  # bez Summary → poctivě oimissing (#465)


def test_build_rows_stara_kotace_se_preskoci() -> None:
    expiry = "20260930"
    streamer = "./ESU6C6400:XCME"
    chain = ChainSymbols(product="ES", day=TODAY, by_contract={(expiry, 6400.0, "C"): streamer})
    stale_time = NOW - dt.timedelta(minutes=10)
    cache = TastyChainCache(clock=lambda: stale_time)
    cache.on_event("Quote", [streamer, 120.0, 122.0, 5, 5])

    rows, oi_missing = build_snapshot_rows(
        chain, expiry, cache, ts_min=NOW, spot=6450.0, now_utc=NOW, max_age_s=120.0
    )

    assert rows == [] and oi_missing == []


def test_build_rows_po_settle_nezapise_nic() -> None:
    """Pojistka #1759: zastaralý plán po settle expirace nesmí vyrobit řádek."""
    expiry = "20261102"
    streamer = "./Q1AX6C28200:XCME"
    chain = ChainSymbols(product="NQ", day=TODAY, by_contract={(expiry, 28200.0, "C"): streamer})

    def build(now: dt.datetime) -> tuple[list[SnapshotRow], list[OiMissingRow]]:
        cache = TastyChainCache(clock=lambda: now)
        cache.on_event("Quote", [streamer, 40.0, 42.0, 5, 5])
        return build_snapshot_rows(
            chain, expiry, cache, ts_min=now, spot=28200.0, now_utc=now, max_age_s=120.0
        )

    assert len(build(dt.datetime(2026, 11, 2, 20, 59, tzinfo=dt.UTC))[0]) == 1
    assert build(dt.datetime(2026, 11, 2, 21, 0, tzinfo=dt.UTC)) == ([], [])
    assert build(dt.datetime(2026, 11, 3, 0, 0, tzinfo=dt.UTC)) == ([], [])


def test_extended_streamers_pasmo_a_fallback_centra() -> None:
    """#616 kapacita: subskribuje se jen ±band kolem centra, ne plný chain."""
    from gexlens_engine.tasty.extended import extended_streamers

    by_contract = {
        ("20260826", float(strike), "C"): f".ES{strike}C" for strike in range(6000, 7001, 100)
    }
    chain = ChainSymbols(product="ES", day=TODAY, by_contract=by_contract)

    # Se spotem: jen striky v pásmu (±3,1 % z 6500 = ±201,5 b)
    subset = extended_streamers(chain, ["20260826"], center=6500.0, band_pct=3.1)
    assert subset == {".ES6300C", ".ES6400C", ".ES6500C", ".ES6600C", ".ES6700C"}

    # Bez spotu: centrum = medián striků nejbližší expirace (6500), ±1,6 % = ±104 b
    fallback = extended_streamers(chain, ["20260826"], center=None, band_pct=1.6)
    assert fallback == {".ES6400C", ".ES6500C", ".ES6600C"}

    # Neplánovaná expirace = nic
    assert extended_streamers(chain, [], center=6500.0, band_pct=3.1) == set()

    # Procentní pásmo škáluje s cenou podkladu (NQ ~4× ES; absolutní body by
    # křídla NQ ořezaly na čtvrtinu relativního pokrytí — lekce ADR-0004)
    nq_contracts = {
        ("20260826", float(strike), "C"): f".NQ{strike}C" for strike in range(24000, 28001, 400)
    }
    nq_chain = ChainSymbols(product="NQ", day=TODAY, by_contract=nq_contracts)
    nq = extended_streamers(nq_chain, ["20260826"], center=26000.0, band_pct=3.1)
    assert nq == {".NQ25200C", ".NQ25600C", ".NQ26000C", ".NQ26400C", ".NQ26800C"}


def test_odstupnovane_pasmo_siroke_pro_blizke_expirace() -> None:
    """#828 A: kapacita je konečná, tak se dá tam, kde je masa OTM putů."""
    from gexlens_engine.tasty.extended import extended_streamers

    by_contract = {}
    for expiry in ("20260825", "20260915"):
        for strike in range(6000, 7001, 100):
            by_contract[(expiry, float(strike), "C")] = f".ES{expiry}{strike}C"
    chain = ChainSymbols(product="ES", day=TODAY, by_contract=by_contract)
    planned = ["20260825", "20260915"]

    out = extended_streamers(
        chain,
        planned,
        center=6500.0,
        band_pct=3.1,  # ±201 b pro vzdálené
        near_band_pct=8.0,  # ±520 b pro nejbližší
        near_expiries=frozenset({"20260825"}),
    )

    near = sorted(s for s in out if "20260825" in s)
    far = sorted(s for s in out if "20260915" in s)
    # Blízká expirace sahá hlouběji do křídel než vzdálená
    assert len(near) > len(far)
    assert ".ES202608256000C" in near  # −500 b, uvnitř ±8 %
    assert ".ES202609156000C" not in far  # tentýž strike u vzdálené už ne

    # Bez near_band_pct se chová jako dřív (jedno pásmo pro všechny)
    uniform = extended_streamers(chain, planned, center=6500.0, band_pct=3.1)
    assert len([s for s in uniform if "20260825" in s]) == len(far)
