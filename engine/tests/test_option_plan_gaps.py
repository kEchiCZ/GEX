"""Co plánu opce QQQ v řádcích řetězu chybí (ADR-0062 K4 bod 7, B; #1768)."""

import datetime as dt
from collections.abc import Collection
from dataclasses import dataclass

import pytest

from gexlens_engine.compute.option_plan import em_gap, variant_gaps

TS = dt.datetime(2026, 11, 3, 14, 15, tzinfo=dt.UTC)
SPOT = 700.0
STRIKES = range(679, 722)


@dataclass(frozen=True)
class Row:
    """Řádek řetězu ve tvaru `option_plan.ChainRowLike`."""

    strike: float
    right: str
    bid: float | None = 1.00
    ask: float | None = 1.10
    iv: float | None = 0.30
    delta: float | None = None
    gamma: float | None = 0.05
    oi: float | None = 100.0
    ts_min: dt.datetime = TS


def _delta(strike: float, right: str) -> float:
    """Call 0,25 na 710, put −0,25 na 690; delta ve striku monotónní."""
    call = min(0.99, max(0.01, 0.5 - (strike - SPOT) / 40.0))
    return call if right == "C" else call - 1.0


def _rows(*, without: Collection[tuple[int, str]] = ()) -> list[Row]:
    return [
        Row(float(strike), right, delta=_delta(strike, right))
        for strike in STRIKES
        for right in ("C", "P")
        if (strike, right) not in without
    ]


LADDER = {"C": {float(k) for k in STRIKES}, "P": {float(k) for k in STRIKES}}
BOTH = {float(k) for k in STRIKES}


# --- EM: ATM straddle 0DTE -------------------------------------------------------


def test_em_uplny_retez() -> None:
    assert em_gap(_rows(), BOTH, SPOT) is None


@pytest.mark.parametrize(
    ("without", "sides"),
    [({(700, "C")}, "call"), ({(700, "P")}, "put"), ({(700, "C"), (700, "P")}, "call a put")],
)
def test_em_bez_strany_atm(without: set[tuple[int, str]], sides: str) -> None:
    """Bez strany na ATM by `straddle_em` tiše vzal straddle 699 nebo 701."""
    assert em_gap(_rows(without=without), BOTH, SPOT) == (
        f"ATM straddle 700 bez zaplacené kotace {sides}"
    )


def test_em_nezaplacena_strana_chybi() -> None:
    rows = [
        row if (row.strike, row.right) != (700.0, "P") else Row(700.0, "P", bid=0.0)
        for row in _rows()
    ]
    assert em_gap(rows, BOTH, SPOT) == "ATM straddle 700 bez zaplacené kotace put"


def test_em_shoda_vzdalenosti_bere_nizsi_strike_jako_straddle_em() -> None:
    assert em_gap(_rows(), BOTH, 700.5) is None
    assert em_gap(_rows(without={(700, "C")}), BOTH, 700.5) == (
        "ATM straddle 700 bez zaplacené kotace call"
    )


def test_em_bez_striku() -> None:
    assert em_gap([], set(), SPOT) == "pásmo bez striku s callem i putem"


def test_em_chybejici_itm_nevadi() -> None:
    itm = {(k, "C") for k in range(679, 693)} | {(k, "P") for k in range(708, 722)}
    assert em_gap(_rows(without=itm), BOTH, SPOT) is None


# --- varianty: |delta| 0,25 se sousedy v 1DTE -------------------------------------


def test_varianty_uplny_retez() -> None:
    assert variant_gaps(_rows(), LADDER) == ()


def test_varianty_chybejici_itm_nevadi() -> None:
    """9. 11. v 9:15: chyběla jen ITM s |delta| 0,69–0,95, výběr varianty se nemění."""
    itm = {(k, "C") for k in range(679, 693)} | {(k, "P") for k in range(708, 722)}
    assert variant_gaps(_rows(without=itm), LADDER) == ()


@pytest.mark.parametrize(
    ("without", "gap"),
    [
        ({(711, "C")}, "call |delta| 0,25 = 710: soused 711 bez kotace s IV a delta"),
        (
            {(709, "C"), (711, "C")},
            "call |delta| 0,25 = 710: soused 709 a 711 bez kotace s IV a delta",
        ),
        ({(689, "P")}, "put |delta| 0,25 = 690: soused 689 bez kotace s IV a delta"),
    ],
)
def test_varianty_bez_souseda(without: set[tuple[int, str]], gap: str) -> None:
    assert variant_gaps(_rows(without=without), LADDER) == (gap,)


def test_varianty_bez_kontraktu_025_uhne_na_souseda() -> None:
    """Chybí 710 C: výběr vezme 709 nebo 711, jejichž soused 710 chybí → mezera."""
    gaps = variant_gaps(_rows(without={(710, "C")}), LADDER)
    assert gaps == ("call |delta| 0,25 = 709: soused 710 bez kotace s IV a delta",)


def test_varianty_na_okraji_pasma() -> None:
    narrow = {"C": {float(k) for k in range(700, 711)}, "P": LADDER["P"]}
    rows = [row for row in _rows() if row.right == "P" or 700 <= row.strike <= 710]
    assert variant_gaps(rows, narrow) == ("call |delta| 0,25 = 710 na okraji pásma",)


def test_varianty_bez_putu() -> None:
    rows = [row for row in _rows() if row.right == "C"]
    assert variant_gaps(rows, LADDER) == ("put bez kotace s IV a delta",)


def test_varianty_radek_mimo_pasmo_je_chyba_volajiciho() -> None:
    narrow = {"C": {float(k) for k in range(700, 709)}, "P": LADDER["P"]}
    with pytest.raises(ValueError, match="mimo striky pásma"):
        variant_gaps(_rows(), narrow)
