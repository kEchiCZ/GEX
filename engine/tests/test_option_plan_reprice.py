"""Přecenění plánu opce QQQ v 9:31 ET (F2c #1769, ADR-0062 K4 C body 2 a 6) — čistá funkce.

Řetěz je golden 2. 11. (`golden/option_plan_1741.json`) se štítkem 9:31 ET. Kontrakty plánu
z 9:15 (put 687 a 686) zůstávají, i když by výběr v 9:31 vzal jiný strike.
"""

import dataclasses
import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from gexlens_engine.compute.gexfield import _YEAR_S, TAU_FLOOR_S, bs_price
from gexlens_engine.compute.option_plan import (
    EXIT_BEFORE_SETTLE,
    VariantKey,
    _pick_variants,
    reprice_variants,
)
from gexlens_engine.compute.settle import settle_ts

DATA: dict[str, Any] = json.loads(
    (Path(__file__).parent / "golden" / "option_plan_1741.json").read_text(encoding="utf-8")
)
MONDAY = dt.date(2026, 11, 2)
EXPIRY = dt.date(2026, 11, 3)
AT_931 = dt.datetime(2026, 11, 2, 14, 31, tzinfo=dt.UTC)
RATIO = DATA["ratio_pair"]["nq"] / DATA["ratio_pair"]["qqq"]
TARGETS = tuple(target / RATIO for target in DATA["scenario"]["targets"])
TARGET_TS = tuple(dt.datetime.fromisoformat(ts) for ts, _ in DATA["scenario"]["path"][1:])
EXIT = settle_ts(MONDAY) - EXIT_BEFORE_SETTLE
KEYS = (
    VariantKey("hlavní", "P", 687.0, EXPIRY),
    VariantKey("záložní", "P", 686.0, EXPIRY),
)


@dataclasses.dataclass(frozen=True)
class Row:
    ts_min: dt.datetime
    strike: float
    right: str
    bid: float | None
    ask: float | None
    iv: float | None
    delta: float | None
    gamma: float | None
    oi: float | None


def _chain(ts: dt.datetime = AT_931, **changes: Any) -> list[Row]:
    rows = [Row(**{**row, "ts_min": ts}) for row in DATA["chain_1dte"]]
    return [dataclasses.replace(row, **changes) for row in rows]


def _reprice(chain: list[Row], keys: tuple[VariantKey, ...] = KEYS) -> Any:
    return reprice_variants(
        keys,
        chain_1dte=chain,
        expiry_1dte=EXPIRY,
        targets=TARGETS,
        target_ts=TARGET_TS,
        exit_ts=EXIT,
        now=AT_931,
    )


def test_strike_z_9_15_zustava_meni_se_kotace_iv_a_model() -> None:
    """K4 C bod 2: výběr v 9:31 by vzal jiný strike, přecenění drží put 687 a 686."""
    chain = []
    for row in _chain(iv=0.32):
        if (row.strike, row.right) == (688.0, "P"):
            row = dataclasses.replace(row, delta=-0.25)  # v 9:31 by byl hlavní 688
        if (row.strike, row.right) == (687.0, "P"):
            row = dataclasses.replace(row, bid=1.51, ask=1.55, delta=-0.22)
        chain.append(row)
    picked, _ = _pick_variants(chain, "P", -1)
    assert picked is not None and picked.strike == 688
    prices, gaps = _reprice(chain)
    assert gaps == ()
    main, backup = prices
    assert (main.role, main.right, main.strike, main.expiry) == ("hlavní", "P", 687, EXPIRY)
    assert (backup.role, backup.strike) == ("záložní", 686)
    assert (main.bid, main.ask, main.iv, main.delta) == (1.51, 1.55, 0.32, -0.22)
    assert main.mid == pytest.approx(1.53)
    assert main.quote_ts == AT_931 and main.source == "tastytrade"
    assert TARGET_TS[1] > EXIT  # PDC v settle se oceňuje nejpozději v exitu (K11 A)
    for target, ts, price in zip(TARGETS, TARGET_TS, main.at_targets, strict=True):
        tau = max((settle_ts(EXPIRY) - min(ts, EXIT)).total_seconds(), TAU_FLOOR_S) / _YEAR_S
        assert price == pytest.approx(bs_price(target, 687.0, 0.32, tau, "P"))


def test_model_v_cili_pred_precenenim_se_oceni_v_case_preceneni() -> None:
    """Cíl s časem cesty před 9:31 se ocení v okamžiku přecenění, ne zpětně."""
    early = (AT_931 - dt.timedelta(minutes=10), TARGET_TS[1])
    prices, _ = reprice_variants(
        KEYS[:1],
        chain_1dte=_chain(),
        expiry_1dte=EXPIRY,
        targets=TARGETS,
        target_ts=early,
        exit_ts=EXIT,
        now=AT_931,
    )
    tau = (settle_ts(EXPIRY) - AT_931).total_seconds() / _YEAR_S
    assert prices[0].at_targets[0] == pytest.approx(bs_price(TARGETS[0], 687.0, 0.3, tau, "P"))


@pytest.mark.parametrize(
    ("change", "gap"),
    [
        ({"bid": None}, "záložní put 686 bez kotace s IV a delta"),
        ({"bid": 0.0}, "záložní put 686 bez kotace s IV a delta"),
        ({"iv": None}, "záložní put 686 bez kotace s IV a delta"),
        ({"delta": None}, "záložní put 686 bez kotace s IV a delta"),
    ],
)
def test_kontrakt_planu_bez_kotace_neprecenuje_nic(change: dict[str, Any], gap: str) -> None:
    """K4 bod 6 a komentář 9. 11.: chybí-li kontrakt plánu, nezapíše se ani hlavní varianta."""
    chain = [
        dataclasses.replace(row, **change) if (row.strike, row.right) == (686.0, "P") else row
        for row in _chain()
    ]
    assert _reprice(chain) == ((), (gap,))
    missing = [row for row in _chain() if (row.strike, row.right) != (686.0, "P")]
    assert _reprice(missing) == ((), (gap,))


def test_jina_expirace_a_plan_bez_varianty_jsou_mezera() -> None:
    other = (VariantKey("hlavní", "P", 687.0, dt.date(2026, 11, 4)),)
    assert _reprice(_chain(), other) == (
        (),
        ("hlavní put 687: expirace 2026-11-04 ≠ 1DTE snímku 2026-11-03",),
    )
    assert _reprice(_chain(), ()) == ((), ("plán z 9:15 ET nemá variantu opce",))


def test_kotace_po_preceneni_je_chyba() -> None:
    """Bez pohledu dopředu: řádek 9:32 v přecenění 9:31 je chyba volajícího."""
    with pytest.raises(ValueError, match="po přecenění 14:31 UTC"):
        _reprice(_chain(ts=AT_931 + dt.timedelta(minutes=1)))
