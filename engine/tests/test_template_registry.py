"""Závazný rejstřík čísel setup šablon (#735).

Do #735 žilo přiřazení „T7" jen v prózách issues — #443 ho obsadilo pro
`trend_continuation` a #601 ho navrhlo podruhé pro průraz hranice gammy.
Kdyby dvě mechaniky sdílely jedno číslo, smíchají se v jedné statistice track
recordu dvě populace setupů, a to tiše: nic nespadne, jen čísla přestanou
znamenat, co tvrdí. Přesně pod kalibrací #394/#434, která na nich stojí.

Rejstřík bez testu zastará stejně jako próza, kterou nahrazuje — proto tenhle
soubor. Každý test tu hlídá jeden způsob, jak se dá rejstřík rozbít.
"""

import pytest

from gexlens_engine.compute.setups import (
    SETUP_TEMPLATE_NUMBERS,
    TEMPLATE_REGISTRY,
    SetupTemplate,
    template_label,
    template_number,
)


def test_kazde_cislo_je_pouzite_nejvys_jednou() -> None:
    numbers = [slot.number for slot in TEMPLATE_REGISTRY]

    assert len(numbers) == len(set(numbers))


def test_kazda_sablona_ma_prave_jedno_cislo() -> None:
    """Dvě čísla pro jednu šablonu by track record rozdělila na dvě populace."""
    templates = [slot.template for slot in TEMPLATE_REGISTRY if slot.template is not None]

    assert len(templates) == len(set(templates))


def test_rejstrik_pokryva_vsechny_sablony_v_kodu() -> None:
    """Nová položka v `SetupTemplate` bez čísla shodí právě tenhle test.

    To je záměr: přidat šablonu a „na číslo si vzpomenout potom" je přesně ta
    cesta, kterou vznikla kolize T7.
    """
    v_rejstriku = {slot.template for slot in TEMPLATE_REGISTRY if slot.template is not None}

    assert v_rejstriku == set(SetupTemplate)


def test_cisla_jdou_souvisle_od_jednicky() -> None:
    """Díra v číslech znamená zapomenutou rezervaci, ne volné místo.

    Volné číslo se pozná tím, že za posledním v rejstříku, ne tím, že v něm
    chybí — jinak by ho někdo obsadil a přepsal tichou rezervaci.
    """
    numbers = sorted(slot.number for slot in TEMPLATE_REGISTRY)

    assert numbers == list(range(1, len(numbers) + 1))


def test_rezervace_vzdy_rikaji_odkud_jsou() -> None:
    """Rezervace bez původu je za měsíc nerozlišitelná od překlepu."""
    for slot in TEMPLATE_REGISTRY:
        assert slot.origin, f"T{slot.number} nemá původ"
        if slot.template is None:
            assert slot.note, f"T{slot.number} je rezervace bez vysvětlení"


def test_t7_patri_trend_continuation() -> None:
    """Jádro #735: číslo drží #443, ne #601.

    Test je schválně konkrétní. Kdyby někdo T7 přehodil, obecné testy výše to
    nepoznají — všechny by dál platily.
    """
    assert template_number(SetupTemplate.TREND_CONTINUATION) == 7
    assert template_label(SetupTemplate.TREND_CONTINUATION) == "T7 trend_continuation"


def test_t8_je_rezervovane_pro_601() -> None:
    """#601 dostalo nejnižší volné číslo; T9 si drží #577, které ho už nese v titulku."""
    osmicka = next(slot for slot in TEMPLATE_REGISTRY if slot.number == 8)

    assert osmicka.template is None
    assert osmicka.origin == "#601"


def test_puvodni_ctverice_z_adr_0004_sedi() -> None:
    """ADR-0004 čísla zavedla; přečíslovat je zpětně by znehodnotilo historii."""
    assert template_number(SetupTemplate.WALL_BOUNCE) == 1
    assert template_number(SetupTemplate.FAILED_BREAK) == 2
    assert template_number(SetupTemplate.MAX_PAIN_PIN) == 3
    assert template_number(SetupTemplate.GAMMA_MOMENTUM) == 4


def test_mapa_cisel_je_odvozena_od_rejstriku() -> None:
    """Dva ručně udržované seznamy = dva seznamy, které se rozejdou."""
    odvozena = {
        slot.template: slot.number for slot in TEMPLATE_REGISTRY if slot.template is not None
    }

    assert odvozena == SETUP_TEMPLATE_NUMBERS


def test_neznama_sablona_selze_hlasite() -> None:
    class Cizi:
        pass

    with pytest.raises(KeyError):
        template_number(Cizi())  # type: ignore[arg-type]
