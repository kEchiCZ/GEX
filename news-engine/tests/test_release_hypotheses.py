"""Registr hypotéz releasů (#1296, ADR-0044): hodnocení, kontrolní body, Wilson, strážce."""

import datetime as dt

import pytest

from gexlens_news.release_hypotheses import (
    CHECKPOINTS,
    DECISION_THRESHOLD,
    H1_SERIES,
    H3_FAMILY,
    HYPOTHESIS_BY_ID,
    M1_BASELINE_SESSIONS,
    M1_FAMILIES,
    M1_TOD_HALF_WIDTH_MIN,
    M1_TOD_MIN_SAMPLES,
    M1_TOD_QUANTILE,
    REGISTERED_AT,
    STATUS_REJECTED,
    STATUS_TESTING,
    STATUS_VERIFIED,
    WILSON_Z,
    MoveFacts,
    decide,
    evaluate,
    evaluate_all,
    frozen_prefix,
    next_checkpoint,
    wilson_bounds,
)
from gexlens_news.release_moves import MOVE_WINDOW_MIN, RETURN_WINDOWS
from gexlens_news.releases import PREVIEW_FAMILIES, family_of

UTC = dt.UTC


def fact(
    index: int,
    *,
    symbol: str = "ES",
    family: str = "CPI",
    headline: str = "Core CPI m/m",
    surprise: int | None = 1,
    exc: float | None = 40.0,
    ret15: float | None = -10.0,
    ret60: float | None = 5.0,
    tod: float | None = 12.0,
    before_registration: bool = False,
) -> MoveFacts:
    base = REGISTERED_AT - dt.timedelta(days=400) if before_registration else REGISTERED_AT
    return MoveFacts(
        cluster_ts=base + dt.timedelta(days=30 * index, hours=12, minutes=30),
        symbol=symbol,
        family=family,
        headline=headline,
        surprise_sign=surprise,
        exc_15m_bp=exc,
        ret_15m_bp=ret15,
        ret_60m_bp=ret60,
        tod_med_15m_bp=tod,
    )


def test_h1_hodnoti_jen_teplejsi_jadro_inflace() -> None:
    h1 = HYPOTHESIS_BY_ID["H1"]
    rows = [
        fact(0, ret15=-12.0),  # zásah
        fact(1, ret15=8.0),  # minutí
        fact(2, surprise=0),  # na odhadu — nehodnotí se
        fact(3, surprise=-1),  # chladnější — H1 se netýká, opačný scénář se neregistruje
        fact(4, surprise=None),  # actual chybí
        fact(5, ret15=0.0),  # nulový výnos
        fact(6, ret15=None),  # zavřený trh / díra
        fact(7, headline="CPI m/m"),  # headline není jádro
        fact(8, family="PPI", headline="Core PPI m/m", ret15=-3.0),
        fact(9, family="PCE", headline="Core PCE Price Index m/m", ret15=-1.0),
        fact(10, before_registration=True),  # před registrací se živě nepočítá
    ]
    state = evaluate(h1, "ES", rows)
    assert (state.hits, state.n) == (3, 4)
    assert [item["hit"] for item in state.outcomes] == [True, False, True, True]
    assert state.status == STATUS_TESTING


def test_h3_jen_es_a_cpi() -> None:
    h3 = HYPOTHESIS_BY_ID["H3"]
    assert h3.symbols == ("ES",)
    rows = [
        fact(0, ret60=12.0, surprise=-1),  # překvapení nerozhoduje
        fact(1, family="NFP", headline="Non-Farm Employment Change", ret60=12.0),
        fact(2, ret60=-4.0),
        fact(3, ret60=0.0),
        fact(4, symbol="NQ", ret60=9.0),
    ]
    state = evaluate(h3, "ES", rows)
    assert (state.hits, state.n) == (1, 2)


def test_m1_potrebuje_baseline_denni_doby() -> None:
    m1 = HYPOTHESIS_BY_ID["M1"]
    rows = [
        fact(0, exc=30.0, tod=10.0),
        fact(1, exc=8.0, tod=10.0),
        fact(2, tod=None),
        fact(3, family="RETAIL", headline="Retail Sales m/m"),  # slabší řada mimo M1
    ]
    state = evaluate(m1, "ES", rows)
    assert (state.hits, state.n) == (1, 2)


@pytest.mark.parametrize(
    ("hits", "n", "expected"),
    [
        (9, 9, None),  # mimo kontrolní bod
        (9, 10, STATUS_VERIFIED),
        (8, 10, None),
        (2, 10, None),
        (1, 10, STATUS_REJECTED),
        (15, 20, STATUS_VERIFIED),
        (14, 20, None),
        (5, 20, STATUS_REJECTED),
        (6, 20, None),
        (21, 30, STATUS_VERIFIED),
        (20, 30, STATUS_REJECTED),  # futilita
        (9, 30, STATUS_REJECTED),
    ],
)
def test_rozhodnuti_v_kontrolnich_bodech(hits: int, n: int, expected: str | None) -> None:
    assert decide(hits, n) == expected


def test_rozhodnuti_je_konecne() -> None:
    h1 = HYPOTHESIS_BY_ID["H1"]
    # 9 z prvních 10 → ověřeno; pak 3 z 10 minutí — zůstává ověřeno (12 z 20)
    returns = [-5.0] * 9 + [5.0] + [-5.0] * 3 + [5.0] * 7
    state = evaluate(h1, "ES", [fact(k, ret15=value) for k, value in enumerate(returns)])
    assert (state.hits, state.n) == (12, 20)
    assert state.status == STATUS_VERIFIED and state.decided_at_n == 10
    # Průběžné 9/9 bez kontrolního bodu nic nerozhodne
    early = evaluate(h1, "ES", [fact(k) for k in range(9)])
    assert early.status == STATUS_TESTING and early.decided_at_n is None
    assert next_checkpoint(early.n) == 10


def test_wilson_horni_mez_z_neuspechu() -> None:
    lower, upper = wilson_bounds(9, 10)
    assert lower == pytest.approx(0.596, abs=0.001)
    assert upper == pytest.approx(0.982, abs=0.001)
    assert wilson_bounds(0, 0) == (None, None)
    _, upper_low = wilson_bounds(1, 10)
    assert upper_low is not None and upper_low < 0.5


def test_evaluate_all_kazda_hypoteza_a_instrument() -> None:
    states = evaluate_all([fact(0), fact(0, symbol="NQ")])
    assert [(s.hypothesis, s.symbol) for s in states] == [
        ("H1", "ES"),
        ("H1", "NQ"),
        ("H3", "ES"),
        ("M1", "ES"),
        ("M1", "NQ"),
    ]
    row = states[0].to_row(REGISTERED_AT)
    assert row["outcomes"][0]["hit"] is True and row["status"] == STATUS_TESTING


def test_strazce_registru() -> None:
    """Změna kteréhokoli čísla = nová hypotéza s novým ID a datem, ne úprava (ADR-0044)."""
    assert dt.datetime(2026, 10, 1, tzinfo=UTC) == REGISTERED_AT
    assert CHECKPOINTS == (10, 20, 30)
    assert (WILSON_Z, DECISION_THRESHOLD) == (1.96, 0.5)
    # Vstupy kritérií — vlastní kopie, ne sdílené konstanty upozornění a news_anomaly
    assert frozenset({"Core CPI m/m", "Core PPI m/m", "Core PCE Price Index m/m"}) == H1_SERIES
    assert H3_FAMILY == "CPI"
    assert frozenset({"CPI", "NFP", "FOMC", "PPI", "PCE"}) == M1_FAMILIES
    assert (M1_BASELINE_SESSIONS, M1_TOD_HALF_WIDTH_MIN, M1_TOD_MIN_SAMPLES, M1_TOD_QUANTILE) == (
        20,
        30,
        200,
        0.5,
    )
    # Horizonty: H1 a M1 = ret_15m / exc_15m (15 min), H3 = ret_60m (60 min)
    assert MOVE_WINDOW_MIN == 15
    assert RETURN_WINDOWS == (15, 60)
    assert {key: dict(h.historical) for key, h in HYPOTHESIS_BY_ID.items()} == {
        "H1": {"ES": (13, 14), "NQ": (13, 14)},
        "H3": {"ES": (21, 24)},
        "M1": {"ES": (107, 111), "NQ": (107, 111)},
    }
    assert {key: h.in_preview for key, h in HYPOTHESIS_BY_ID.items()} == {
        "H1": "testing",
        "H3": "never",
        "M1": "verified",
    }


def test_rodiny_hypotez_se_meri() -> None:
    """Job měří jen rodiny s upozorněním — odebrání rodiny by potichu změnilo kritérium."""
    families = set(M1_FAMILIES) | {H3_FAMILY} | {family_of(series) for series in H1_SERIES}
    assert families <= set(PREVIEW_FAMILIES)


def test_rozhodnuti_prezije_zmenu_dat() -> None:
    """Ověřeno při n = 10; pozdější oprava dat v rozhodnutém úseku stav nepřepíše."""
    h1 = HYPOTHESIS_BY_ID["H1"]
    rows = [fact(k, ret15=-5.0 if k < 9 else 5.0) for k in range(10)]
    first = evaluate(h1, "ES", rows)
    assert (first.status, first.decided_at_n, first.hits) == (STATUS_VERIFIED, 10, 9)
    prefix = frozen_prefix(first.status, first.decided_at_n, first.outcomes)
    assert prefix is not None and len(prefix.outcomes) == 10
    # Přeměření: 4 zásahy se překlopí, jeden release ztratí překvapení, přibude nový
    changed = [fact(k, ret15=5.0) for k in range(4)] + rows[4:]
    changed[5] = fact(5, surprise=0)
    changed.append(fact(10, ret15=5.0))
    again = evaluate(h1, "ES", changed, frozen=prefix)
    assert (again.status, again.decided_at_n) == (STATUS_VERIFIED, 10)
    assert (again.hits, again.n) == (9, 11)
    assert again.outcomes[:10] == first.outcomes
    assert again.ignored_changes == 5
    # Bez zmrazeného úseku by tatáž data rozhodnutí zrušila
    assert evaluate(h1, "ES", changed).status == STATUS_TESTING


def test_kontrolni_bod_bez_rozhodnuti_je_taky_zmrazeny() -> None:
    """8/10 = pokračovat; oprava dat v úseku do n = 10 nesmí zpětně „ověřit“."""
    h1 = HYPOTHESIS_BY_ID["H1"]
    rows = [fact(k, ret15=-5.0 if k < 8 else 5.0) for k in range(12)]
    first = evaluate(h1, "ES", rows)
    assert (first.status, first.n) == (STATUS_TESTING, 12)
    prefix = frozen_prefix(first.status, first.decided_at_n, first.outcomes)
    assert prefix is not None and len(prefix.outcomes) == 10
    fixed = [fact(k, ret15=-5.0) for k in range(12)]  # dnes by bylo 10/10 při n = 10
    again = evaluate(h1, "ES", fixed, frozen=prefix)
    assert again.status == STATUS_TESTING and again.decided_at_n is None
    assert (again.hits, again.n) == (10, 12)
    # Pod prvním kontrolním bodem není co zmrazit
    assert frozen_prefix(STATUS_TESTING, None, first.outcomes[:9]) is None


def test_pozde_hodnotitelny_release_se_pricte_za_usek() -> None:
    """Release před koncem úseku, který se stal hodnotitelným až po něm, jde za úsek."""
    h1 = HYPOTHESIS_BY_ID["H1"]
    rows = [fact(k) for k in range(11)]
    rows[3] = fact(3, surprise=None)  # actual chyběl při kontrole
    first = evaluate(h1, "ES", rows)
    prefix = frozen_prefix(first.status, first.decided_at_n, first.outcomes)
    assert prefix is not None and first.decided_at_n == 10
    rows[3] = fact(3)  # actual dorazil
    again = evaluate(h1, "ES", rows, frozen=prefix)
    assert again.n == 11 and again.outcomes[-1]["cluster_ts"] == rows[3].cluster_ts.isoformat()
