"""Golden test jednotek Dyn Charm a Dyn Vanna (#1401).

Fixture `golden/greek_dollar_delta_1401.json` sdílí frontend (heatmap/units.test.ts):
engine musí dát ručně spočtené hodnoty na bod (Δ·OI·M za den, resp. na 1 bod IV)
a frontend z nich násobením cenou hladiny dolarovou deltu. Změna jednotky v enginu
tím shodí oba testy, ne tiše zobrazení.
"""

import datetime as dt
import json
from pathlib import Path

import pytest

from gexlens_engine.compute.gexfield import ProfileContract, greek_profiles

FIXTURE = Path(__file__).parent / "golden" / "greek_dollar_delta_1401.json"


def test_greek_profiles_odpovidaji_rucnimu_vypoctu() -> None:
    for scenario in json.loads(FIXTURE.read_text(encoding="utf-8"))["scenarios"]:
        contract = scenario["contract"]
        levels = scenario["levels"]
        ts_min = dt.datetime(2026, 10, 12, 20, 0, tzinfo=dt.UTC)
        profiles = greek_profiles(
            [
                ProfileContract(
                    contract["strike"], contract["right"], contract["iv"], contract["oi"]
                )
            ],
            ts_min=ts_min,
            settle=ts_min + dt.timedelta(days=contract["tau_days"]),
            grid_start=levels[0]["price"],
            grid_stop=levels[-1]["price"],
            grid_step=levels[1]["price"] - levels[0]["price"],
            multiplier=contract["multiplier"],
        )
        for index, level in enumerate(levels):
            charm = profiles["charm"].values[index]
            vanna = profiles["vanna"].values[index]
            assert charm == pytest.approx(level["charm_per_point"], rel=1e-12)
            assert vanna == pytest.approx(level["vanna_per_point"], rel=1e-12)


def test_dolarova_delta_je_hodnota_krat_cena_hladiny() -> None:
    for scenario in json.loads(FIXTURE.read_text(encoding="utf-8"))["scenarios"]:
        for level in scenario["levels"]:
            # Přesně: fixture je spočtená týmž násobením ve float64
            assert level["charm_per_point"] * level["price"] == level["charm_dollar_delta"]
            assert level["vanna_per_point"] * level["price"] == level["vanna_dollar_delta"]
