"""Paritní golden test váhy P²/100 (#569).

Fixture `golden/p2_weight_569.json` sdílí frontend (heatmap/units.test.ts) —
obě implementace váhy musí nad týmiž vstupy dát tatáž čísla. Engine pole
ukládá v $/bod; `price_weight_per_percent` je jediná enginová definice váhy
a band_regime (#575) ji musí použít beze změny.
"""

import json
from pathlib import Path

from gexlens_engine.compute.gexfield import price_weight_per_percent

FIXTURE = Path(__file__).parent / "golden" / "p2_weight_569.json"


def test_p2_weight_parity_fixture() -> None:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    contract = data["contract"]
    per_point = contract["gamma"] * contract["oi"] * contract["multiplier"]
    # Ručně spočtený příspěvek $/bod (Γ·OI·M) — přesně, žádná tolerance
    assert per_point == data["per_point"]
    for level in data["levels"]:
        # Váha s cenou HLADINY, ne spotem — přesná shoda s fixture
        assert per_point * price_weight_per_percent(level["price"]) == level["per_percent"]


def test_p2_weight_je_funkci_hladiny_ne_spotu() -> None:
    # Dvě hladiny → dvě různé váhy; násobení konstantním spotem by dalo jednu
    assert price_weight_per_percent(6000.0) != price_weight_per_percent(7600.0)
    assert price_weight_per_percent(6000.0) == 360_000.0
