"""Testy Dyn GEX profilu a pole (ADR-0009, #203): BS gamma, znaménka, crunch, persistence."""

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from gexlens_engine.compute.gexfield import (
    GAMMA_EDGE_SHARE,
    GexProfile,
    ProfileContract,
    bs_gamma,
    bs_price,
    fallback_greeks,
    gamma_edges,
    gamma_field,
    gamma_profile,
    implied_vol,
)
from gexlens_engine.config import Settings
from gexlens_engine.storage.parquet_store import GexFieldRow, GexProfileRow, SnapshotWriter

TS = dt.datetime(2026, 7, 22, 14, 0, tzinfo=dt.UTC)
SETTLE = dt.datetime(2026, 7, 22, 20, 0, tzinfo=dt.UTC)


def profile_values(contracts: list[ProfileContract], ts: dt.datetime = TS) -> list[float]:
    profile = gamma_profile(
        contracts,
        ts_min=ts,
        settle=SETTLE,
        grid_start=7400.0,
        grid_stop=7600.0,
        grid_step=5.0,
        multiplier=50.0,
    )
    return list(profile.values)


def test_call_profile_peaks_at_strike_and_is_positive() -> None:
    values = profile_values([ProfileContract(7500.0, "C", 0.15, 1000.0)])
    grid = [7400.0 + 5.0 * i for i in range(len(values))]
    peak_price = grid[values.index(max(values))]
    assert abs(peak_price - 7500.0) <= 5.0  # ATM vrchol
    assert all(v >= 0.0 for v in values)  # call = kladná dealer gamma


def test_put_contributes_negative_and_signs_offset() -> None:
    put_only = profile_values([ProfileContract(7500.0, "P", 0.15, 1000.0)])
    assert min(put_only) < 0.0 and max(put_only) <= 0.0
    # Stejný strike, IV i OI → call a put se přesně vyruší (NaiveDealerModel)
    both = profile_values(
        [
            ProfileContract(7500.0, "C", 0.15, 1000.0),
            ProfileContract(7500.0, "P", 0.15, 1000.0),
        ]
    )
    assert max(abs(v) for v in both) == pytest.approx(0.0, abs=1e-9)


def test_expiry_crunch_gamma_grows_with_shrinking_tau() -> None:
    early = profile_values([ProfileContract(7500.0, "C", 0.15, 1000.0)], ts=TS)
    late = profile_values(
        [ProfileContract(7500.0, "C", 0.15, 1000.0)],
        ts=dt.datetime(2026, 7, 22, 19, 30, tzinfo=dt.UTC),
    )
    assert max(late) > max(early)  # ATM gamma do expirace roste (crunch)


def test_tau_floor_prevents_divergence_at_settle() -> None:
    at_settle = profile_values([ProfileContract(7500.0, "C", 0.15, 1000.0)], ts=SETTLE)
    assert all(v == v and v != float("inf") for v in at_settle)  # žádné NaN/inf
    assert max(at_settle) > 0.0


def test_contracts_without_iv_or_oi_are_skipped() -> None:
    values = profile_values(
        [
            ProfileContract(7500.0, "C", 0.0, 1000.0),  # bez IV
            ProfileContract(7500.0, "C", 0.15, 0.0),  # bez OI
        ]
    )
    assert all(v == 0.0 for v in values)
    assert bs_gamma(7500.0, 7500.0, 0.0, 0.1) == 0.0


def field_kwargs() -> dict[str, object]:
    return {
        "ts_min": TS,
        "settle": SETTLE,
        "grid_start": 7400.0,
        "grid_stop": 7600.0,
        "grid_step": 5.0,
        "multiplier": 50.0,
    }


def test_gamma_field_columns_cover_time_to_settle() -> None:
    """Fáze 2: sloupce po col_step_min od ts_min+krok až k settle."""
    field = gamma_field(
        [ProfileContract(7500.0, "C", 0.15, 1000.0)],
        col_step_min=10,
        **field_kwargs(),  # type: ignore[arg-type]
    )
    assert field is not None
    assert field.col_start == TS + dt.timedelta(minutes=10)
    assert len(field.values) == 36  # 6 h do settle / 10 min
    assert all(len(column) == 41 for column in field.values)  # mřížka 7400..7600 po 5


def test_gamma_field_first_column_matches_profile() -> None:
    """První sloupec pole = profil spočtený k času prvního sloupce (stejná τ)."""
    contracts = [
        ProfileContract(7500.0, "C", 0.15, 1000.0),
        ProfileContract(7450.0, "P", 0.18, 400.0),
    ]
    field = gamma_field(contracts, col_step_min=10, **field_kwargs())  # type: ignore[arg-type]
    assert field is not None
    profile = gamma_profile(
        contracts,
        ts_min=TS + dt.timedelta(minutes=10),
        settle=SETTLE,
        grid_start=7400.0,
        grid_stop=7600.0,
        grid_step=5.0,
        multiplier=50.0,
    )
    for got, expected in zip(field.values[0], profile.values, strict=True):
        assert got == pytest.approx(expected, rel=1e-9)


def test_gamma_field_atm_crunch_grows_toward_settle() -> None:
    """Pozdější sloupce (menší τ) mají vyšší ATM špičku — vizuální crunch."""
    field = gamma_field(
        [ProfileContract(7500.0, "C", 0.15, 1000.0)],
        **field_kwargs(),  # type: ignore[arg-type]
    )
    assert field is not None
    assert max(field.values[-1]) > max(field.values[0])


def test_gamma_field_horizon_cap_and_empty_inputs() -> None:
    """Vzdálená expirace se stropuje na horizon_min; bez kontraktů/času pole není."""
    far_settle = TS + dt.timedelta(hours=48)
    capped = gamma_field(
        [ProfileContract(7500.0, "C", 0.15, 1000.0)],
        ts_min=TS,
        settle=far_settle,
        grid_start=7400.0,
        grid_stop=7600.0,
        grid_step=5.0,
        multiplier=50.0,
        col_step_min=10,
        horizon_min=24 * 60,
    )
    assert capped is not None
    assert len(capped.values) == 144  # 24 h / 10 min
    assert gamma_field([], **field_kwargs()) is None  # type: ignore[arg-type]
    past = gamma_field(
        [ProfileContract(7500.0, "C", 0.15, 1000.0)],
        ts_min=SETTLE,
        settle=SETTLE,
        grid_start=7400.0,
        grid_stop=7600.0,
        grid_step=5.0,
        multiplier=50.0,
    )
    assert past is None  # po settle už není co modelovat


def test_gexfield_partition_keeps_only_last_state(tmp_path: Path) -> None:
    """Fáze 2: write_gexfield přepisuje — partice drží jen poslední stav minuty."""
    writer = SnapshotWriter(Settings(data_dir=tmp_path))
    day = dt.date(2026, 7, 22)

    def row(minute: int, values: list[float]) -> GexFieldRow:
        return GexFieldRow(
            ts_min=TS + dt.timedelta(minutes=minute),
            grid_start=7400.0,
            grid_step=2.5,
            col_start=TS + dt.timedelta(minutes=minute + 10),
            col_step_min=10,
            col_count=2,
            values=values,
        )

    writer.write_gexfield("ES", "20260722", day, row(0, [1.0, 2.0, 3.0, 4.0]))
    path = writer.write_gexfield("ES", "20260722", day, row(1, [5.0, 6.0, 7.0, 8.0]))

    assert path == tmp_path / "derived" / "ES" / "20260722" / "gexfield" / "2026-07-22.parquet"
    frame = pd.read_parquet(path)
    assert len(frame) == 1  # replace, ne append
    assert frame["ts_min"][0].to_pydatetime() == TS + dt.timedelta(minutes=1)
    assert list(frame["values"][0]) == [5.0, 6.0, 7.0, 8.0]
    assert int(frame["col_count"][0]) == 2


def test_gexprofile_persisted_with_list_column(tmp_path: Path) -> None:
    """ADR-0009: profil jde do derived/{sym}/{exp}/gexprofile s list sloupcem values."""
    writer = SnapshotWriter(Settings(data_dir=tmp_path))
    day = dt.date(2026, 7, 22)
    row = GexProfileRow(ts_min=TS, grid_start=7400.0, grid_step=2.5, values=[1.0, -2.5, 3.0])

    path = writer.write_gexprofile("ES", "20260722", day, [row])

    assert path == tmp_path / "derived" / "ES" / "20260722" / "gexprofile" / "2026-07-22.parquet"
    frame = pd.read_parquet(path)
    assert list(frame.columns) == ["ts_min", "grid_start", "grid_step", "values"]
    assert list(frame["values"][0]) == [1.0, -2.5, 3.0]


def test_greek_profiles_match_gamma_and_add_charm_vanna() -> None:
    """#204: kombinovaný průchod dává bit-shodnou gammu + nenulové charm/vanna."""
    from gexlens_engine.compute.gexfield import bs_charm, bs_vanna, greek_fields, greek_profiles

    now = dt.datetime(2026, 7, 30, 14, 0, tzinfo=dt.UTC)
    settle = now + dt.timedelta(hours=6)
    contracts = [
        ProfileContract(strike=7400.0 + 10 * i, right=("C" if i % 2 else "P"), iv=0.15, oi=1000.0)
        for i in range(10)
    ]
    single = gamma_profile(
        contracts,
        ts_min=now,
        settle=settle,
        grid_start=7380.0,
        grid_stop=7500.0,
        grid_step=5.0,
        multiplier=50.0,
    )
    combined = greek_profiles(
        contracts,
        ts_min=now,
        settle=settle,
        grid_start=7380.0,
        grid_stop=7500.0,
        grid_step=5.0,
        multiplier=50.0,
    )
    assert combined["gamma"].values == pytest.approx(single.values)
    assert any(value != 0 for value in combined["charm"].values)
    assert any(value != 0 for value in combined["vanna"].values)

    fields = greek_fields(
        contracts,
        ts_min=now,
        settle=settle,
        grid_start=7380.0,
        grid_stop=7500.0,
        grid_step=5.0,
        multiplier=50.0,
    )
    assert fields is not None
    assert set(fields) == {"gamma", "charm", "vanna"}
    # Sloupce všech ploch sdílí mřížku i časovou osu
    assert fields["charm"].col_start == fields["gamma"].col_start
    assert len(fields["vanna"].values) == len(fields["gamma"].values)

    # Ruční BS kontrola v jednom bodě: charm = φ(d1)·d2/(2τ)/365, vanna = −φ(d1)·d2/σ·0,01
    import math

    spot, strike, iv = 7400.0, 7450.0, 0.2
    tau = 0.05
    sqrt_tau = math.sqrt(tau)
    d1 = (math.log(spot / strike) + 0.5 * iv * iv * tau) / (iv * sqrt_tau)
    d2 = d1 - iv * sqrt_tau
    phi = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
    assert bs_charm(spot, strike, iv, tau) == pytest.approx(phi * d2 / (2 * tau) / 365.0)
    assert bs_vanna(spot, strike, iv, tau) == pytest.approx(-phi * d2 / iv * 0.01)
    assert bs_charm(spot, strike, 0.0, tau) == 0.0
    assert bs_vanna(spot, strike, iv, 0.0) == 0.0


# ── IV inverze a fallback greeks (#547) ────────────────────────────────


def test_implied_vol_round_trip_call_i_put() -> None:
    """IV inverze proti známé BS ceně: round-trip na obou stranách."""
    tau = 30.0 / 365.0
    call_price = bs_price(7600.0, 7650.0, 0.22, tau, "C")
    assert implied_vol(call_price, 7600.0, 7650.0, tau, "C") == pytest.approx(0.22, abs=1e-6)

    put_price = bs_price(7600.0, 7650.0, 0.22, tau, "P")
    # Put-call parita při r = 0: P = C − S + K
    assert put_price == pytest.approx(call_price - 7600.0 + 7650.0, abs=1e-9)
    assert implied_vol(put_price, 7600.0, 7650.0, tau, "P") == pytest.approx(0.22, abs=1e-6)


def test_implied_vol_round_trip_kratke_tau_a_vysoka_vol() -> None:
    """0DTE poměry: krátké τ i vysoká IV se musí invertovat zpátky."""
    tau = 300.0 / (365.0 * 24 * 3600)  # podlaha TAU_FLOOR_S
    price = bs_price(29500.0, 29500.0, 1.1, tau, "C")
    assert implied_vol(price, 29500.0, 29500.0, tau, "C") == pytest.approx(1.1, rel=1e-5)


def test_implied_vol_mimo_pasmo_vraci_none() -> None:
    """Kraj (#547): cena pod vnitřní hodnotou nebo nad podkladem → žádná IV."""
    tau = 5.0 / 365.0
    # ITM call: vnitřní hodnota 100, mid jen 99 → arbitrážní cena, nekonverguje
    assert implied_vol(99.0, 7600.0, 7500.0, tau, "C") is None
    # Cena nad horní hranicí pásma (call ≥ spot)
    assert implied_vol(7601.0, 7600.0, 7500.0, tau, "C") is None
    # Nevalidní vstupy
    assert implied_vol(0.0, 7600.0, 7500.0, tau, "C") is None
    assert implied_vol(10.0, 7600.0, 7500.0, 0.0, "C") is None
    assert implied_vol(10.0, 7600.0, 7500.0, tau, "X") is None


def test_fallback_greeks_konzistentni_s_bs_formuli() -> None:
    """Fallback (#547): IV z mid, gamma shodná se sdílenou bs_gamma, znaménka sedí."""
    now = dt.datetime(2026, 7, 16, 14, 0, tzinfo=dt.UTC)
    settle = now + dt.timedelta(days=30)
    tau = 30.0 / 365.0
    mid = bs_price(7600.0, 7650.0, 0.25, tau, "C")

    greeks = fallback_greeks(spot=7600.0, strike=7650.0, right="C", mid=mid, settle=settle, now=now)

    assert greeks is not None
    assert greeks.iv == pytest.approx(0.25, abs=1e-6)
    assert greeks.gamma == pytest.approx(bs_gamma(7600.0, 7650.0, greeks.iv, tau), rel=1e-6)
    assert 0.0 < greeks.delta < 1.0
    assert greeks.theta < 0.0  # časový rozpad
    assert greeks.vega > 0.0

    put = fallback_greeks(
        spot=7600.0, strike=7650.0, right="P", mid=mid + 50.0, settle=settle, now=now
    )
    assert put is not None
    assert -1.0 < put.delta < 0.0


def test_fallback_greeks_nekonverguje_zadne_vymyslene_hodnoty() -> None:
    """Kraj (#547): mid pod vnitřní hodnotou → None, strike zůstane nekompletní."""
    now = dt.datetime(2026, 7, 16, 14, 0, tzinfo=dt.UTC)
    settle = now + dt.timedelta(days=5)
    # ITM call: vnitřní hodnota 100, mid 10.25 — kotace evidentně nesedí k podkladu
    assert (
        fallback_greeks(spot=7600.0, strike=7500.0, right="C", mid=10.25, settle=settle, now=now)
        is None
    )
    assert (
        fallback_greeks(spot=7600.0, strike=7500.0, right="C", mid=0.0, settle=settle, now=now)
        is None
    )


# ── Hranice gamma masy (#600) ──────────────────────────────────────────


def _profile(values: list[float], *, start: float = 7400.0, step: float = 5.0) -> GexProfile:
    return GexProfile(ts_min=TS, grid_start=start, grid_step=step, values=tuple(values))


def test_gamma_edges_najde_okraje_masy_s_interpolaci() -> None:
    # Mřížka 7400..7440 po 5 bodech; maximum 100 → práh 10
    values = [0.0, 5.0, 40.0, 100.0, 60.0, 20.0, 5.0, 0.0, 0.0]
    edges = gamma_edges(_profile(values), share=0.1)
    assert edges.up is not None and edges.dn is not None
    assert edges.threshold == pytest.approx(10.0)
    # Dolní: mezi 7405 (5) a 7410 (40) — |NetGEX| protne 10 kousek nad 7405
    assert edges.dn == pytest.approx(7410.0 - (40.0 - 10.0) / (40.0 - 5.0) * 5.0)
    assert 7405.0 < edges.dn < 7410.0
    # Horní: mezi 7425 (20) a 7430 (5)
    assert edges.up == pytest.approx(7425.0 + (20.0 - 10.0) / (20.0 - 5.0) * 5.0)
    assert 7425.0 < edges.up < 7430.0


def test_gamma_edges_nepretne_se_o_nulu_u_flipu() -> None:
    """Profil mění u flipu znaménko — hranice musí zůstat na okrajích masy.

    Souvislé pásmo od spotu by se o průchod nulou přeťalo a hranice by spadla
    doprostřed masy; proto se hledá globální krajní bod nad prahem.
    """
    # Call strana kladná, put strana záporná, uprostřed nula (flip)
    values = [-80.0, -100.0, -40.0, 0.0, 40.0, 100.0, 60.0, 0.0, 0.0]
    edges = gamma_edges(_profile(values), share=0.1)
    assert edges.up is not None and edges.dn is not None
    assert edges.dn == pytest.approx(7400.0)  # masa sahá až na kraj mřížky
    assert 7430.0 < edges.up < 7435.0  # za posledním významným bodem, ne u nuly


def test_gamma_edges_kraj_mrizky_a_prazdny_profil() -> None:
    # Masa přesahuje mřížku na obou koncích → hranice jsou její kraje
    edges = gamma_edges(_profile([100.0, 100.0, 100.0]), share=0.1)
    assert (edges.dn, edges.up) == (7400.0, 7410.0)
    # Prázdný profil i samé nuly → hranice neexistují (radši None než výmysl)
    assert gamma_edges(_profile([])).up is None
    assert gamma_edges(_profile([0.0, 0.0, 0.0])).dn is None


def test_gamma_edges_prah_se_da_zvednout() -> None:
    """Vyšší podíl utáhne hranice blíž k jádru masy — páka pro kalibraci #601."""
    values = [0.0, 10.0, 50.0, 100.0, 50.0, 10.0, 0.0]
    wide = gamma_edges(_profile(values), share=0.05)
    tight = gamma_edges(_profile(values), share=0.5)
    assert wide.up is not None and wide.dn is not None
    assert tight.up is not None and tight.dn is not None
    assert tight.up < wide.up
    assert tight.dn > wide.dn


def test_gamma_edges_vychozi_prah_je_kalibrovany_na_85_procent() -> None:
    """Default vychází z měření #601, ne z odhadu — plochý profil jinak nemá okraj."""
    assert pytest.approx(0.85) == GAMMA_EDGE_SHARE
    # Zvon, který na mřížce neklesne pod 10 % maxima: při nízkém prahu hranice
    # padne na kraj mřížky (= neurčitelná), teprve vysoký práh vrátí něco uvnitř
    values = [30.0, 55.0, 80.0, 100.0, 80.0, 55.0, 30.0]
    low = gamma_edges(_profile(values), share=0.1)
    default = gamma_edges(_profile(values))
    assert (low.dn, low.up) == (7400.0, 7430.0)  # oba kraje mřížky
    assert default.dn is not None and default.up is not None
    assert 7400.0 < default.dn < default.up < 7430.0
