"""Rozpočet DXLink subskripce (#982): 25 000 položek symbol × event."""

import datetime as dt

from gexlens_engine.tasty.budget import (
    ALL_EVENTS,
    EVENTS_BY_PURPOSE,
    BudgetPlan,
    order_by_distance,
    plan_subscriptions,
)
from gexlens_engine.tasty.symbols import ChainSymbols


def test_kazdy_ucel_odebira_jen_eventy_ktere_cte() -> None:
    """Wide jen OI (+Quote), extended bez printů, řetěz a podklad vše."""
    plan = plan_subscriptions(
        {
            "underlying": ["/ESU26"],
            "chain": ["./ESC1"],
            "adhoc": ["./LOC1"],
            "extended": ["./ESC2"],
            "wide": ["./ESC3"],
        }
    )
    assert plan.subscriptions["/ESU26"] == frozenset(ALL_EVENTS)
    assert plan.subscriptions["./ESC1"] == frozenset(ALL_EVENTS)
    assert plan.subscriptions["./LOC1"] == frozenset({"Quote", "Greeks", "Summary"})
    assert plan.subscriptions["./ESC2"] == frozenset({"Quote", "Greeks", "Summary"})
    assert plan.subscriptions["./ESC3"] == frozenset({"Quote", "Summary"})
    assert plan.entries == 4 + 4 + 3 + 3 + 2
    assert plan.trimmed == {}


def test_produkce_se_vejde_s_rezervou() -> None:
    """Stav produkce 2. 9. 2026: 560 chain + 4 776 extended + 898 wide + 2 podklady
    jelo na 24 944 položkách (× 4 eventy) — ad-hoc přetekl. S eventy per účel
    zbývá místo i na rezervu a ad-hoc pohled CL (307 symbolů)."""
    purposes = {
        "underlying": [f"/U{i}" for i in range(2)],
        "chain": [f"./C{i}" for i in range(560)],
        "extended": [f"./E{i}" for i in range(4776)],
        "wide": [f"./W{i}" for i in range(898)],
        "adhoc": [f"./A{i}" for i in range(307)],
    }
    plan = plan_subscriptions(purposes)
    assert plan.trimmed == {}
    assert plan.over_hard_cap == 0
    assert plan.entries == 2 * 4 + 560 * 4 + 4776 * 3 + 898 * 2 + 307 * 3
    assert plan.entries < 25_000 - 2_000 + 307 * 3


def test_orez_bere_extended_zezadu_pak_wide_retez_nikdy() -> None:
    """Seznamy jsou od nejdůležitějšího; co se nevejde, padá od konce."""
    purposes = {
        "chain": ["./C1", "./C2"],  # 8 položek
        "wide": ["./W1", "./W2"],  # 4 položky
        "extended": ["./E1", "./E2", "./E3"],  # 9 položek
    }
    # Strop 8 + 4 + 6 = 18 → z extended zbude jen E1, E2
    plan = plan_subscriptions(purposes, max_entries=18, adhoc_reserve=0)
    assert set(plan.subscriptions) == {"./C1", "./C2", "./W1", "./W2", "./E1", "./E2"}
    assert plan.trimmed == {"extended": 1}
    # Ještě těsněji: 8 + 2 = 10 → wide W2 i celé extended pryč, řetěz nedotčený
    plan = plan_subscriptions(purposes, max_entries=10, adhoc_reserve=0)
    assert set(plan.subscriptions) == {"./C1", "./C2", "./W1"}
    assert plan.trimmed == {"wide": 1, "extended": 3}
    # Řetěz nad tvrdým stropem se neořeže, ale přizná
    plan = plan_subscriptions({"chain": ["./C1", "./C2"]}, max_entries=4, adhoc_reserve=0)
    assert set(plan.subscriptions) == {"./C1", "./C2"}
    assert plan.over_hard_cap == 4


def test_rezerva_drzi_misto_pro_adhoc() -> None:
    """Bez ad-hoc smí wide/extended jen po `strop − rezerva`; ad-hoc rezervu čerpá."""
    purposes = {"extended": [f"./E{i}" for i in range(10)]}  # 30 položek
    plan = plan_subscriptions(purposes, max_entries=30, adhoc_reserve=6)
    assert plan.entries == 24
    assert plan.trimmed == {"extended": 2}
    # Ad-hoc (2 symboly × 3 = 6) rezervu přesně vyplní — extended stejný ořez
    with_adhoc = plan_subscriptions(
        {**purposes, "adhoc": ["./A1", "./A2"]}, max_entries=30, adhoc_reserve=6
    )
    assert with_adhoc.entries == 30
    assert with_adhoc.trimmed == {"extended": 2}
    # Ad-hoc větší než rezerva ubírá extended (ne naopak)
    bigger = plan_subscriptions(
        {**purposes, "adhoc": ["./A1", "./A2", "./A3"]}, max_entries=30, adhoc_reserve=6
    )
    assert bigger.entries == 30
    assert bigger.trimmed == {"extended": 3}
    assert all(f"./A{i}" in bigger.subscriptions for i in (1, 2, 3))


def test_symbol_ve_dvou_ucelech_dostane_sjednoceni_a_plati_jednou() -> None:
    plan = plan_subscriptions({"chain": ["./X"], "wide": ["./X"]})
    assert plan.subscriptions["./X"] == frozenset(ALL_EVENTS)
    assert plan.entries == 4


def test_neznamy_ucel_dostane_vse_a_neorezava_se() -> None:
    plan = plan_subscriptions({"novy": ["./N1"]}, max_entries=2, adhoc_reserve=0)
    assert plan.subscriptions["./N1"] == frozenset(ALL_EVENTS)
    assert plan.over_hard_cap == 4
    assert isinstance(plan, BudgetPlan)
    assert set(EVENTS_BY_PURPOSE) >= {"chain", "extended", "wide", "adhoc", "underlying"}


def test_razeni_od_nejblizsi_expirace_a_striku_napric_produkty() -> None:
    """ES i NQ se řadí podle vzdálenosti v % ceny, ne v bodech; bez info dozadu."""
    es = ChainSymbols(
        product="ES",
        day=dt.date(2026, 9, 2),
        by_contract={
            ("20260904", 6500.0, "C"): "./ES_near_atm",
            ("20260904", 6300.0, "P"): "./ES_near_far",  # 3,1 %
            ("20260918", 6510.0, "C"): "./ES_late_atm",
        },
    )
    nq = ChainSymbols(
        product="NQ",
        day=dt.date(2026, 9, 2),
        by_contract={
            ("20260904", 23_900.0, "C"): "./NQ_near_atm",  # 0,4 %
            ("20260904", 24_600.0, "C"): "./NQ_near_far",  # 2,5 % (600 b, ale blíž než ES 200 b)
        },
    )
    ordered = order_by_distance(
        [
            (["./ES_near_far", "./ES_late_atm", "./ES_near_atm"], es, 6500.0),
            (["./NQ_near_far", "./NQ_near_atm"], nq, 24_000.0),
            (["./bez_chainu"], None, None),
        ]
    )
    assert ordered == [
        "./ES_near_atm",
        "./NQ_near_atm",
        "./NQ_near_far",
        "./ES_near_far",
        "./ES_late_atm",
        "./bez_chainu",
    ]
