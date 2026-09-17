"""Front kontrakt podkladu: roll pravidlo u kořene, přesný kontrakt u pinovaného (#1191)."""

import datetime as dt
from typing import Any, cast

import pytest
from ib_async import IB, ContractDetails, Future

from gexlens_engine.__main__ import _resolve_front_future
from gexlens_engine.instruments import InstrumentSetupError


class _FakeIB:
    def __init__(self, contracts: list[Future]) -> None:
        self.contracts = contracts
        self.requested: list[str] = []

    async def reqContractDetailsAsync(self, contract: Any) -> list[ContractDetails]:
        self.requested.append(str(contract.symbol))
        return [ContractDetails(contract=c) for c in self.contracts]


def _es(local: str, last: str) -> Future:
    return Future(symbol="ES", lastTradeDateOrContractMonth=last, exchange="CME", localSymbol=local)


def _contracts(today: dt.date) -> list[Future]:
    near = today + dt.timedelta(days=5)
    far = today + dt.timedelta(days=95)
    return [_es("ESU6", near.strftime("%Y%m%d")), _es("ESZ6", far.strftime("%Y%m%d"))]


async def test_koren_roluje_pinovany_bere_presne() -> None:
    today = dt.datetime.now(dt.UTC).date()
    ib = _FakeIB(_contracts(today))
    # Kořen v roll okně (8 d) → další kontrakt
    front = await _resolve_front_future(cast(IB, ib), "ES", front_roll_days=8)
    assert front.localSymbol == "ESZ6"
    # Pinovaný U6 → přesně U6, i když je v roll okně; IBKR se ptá na kořen
    pinned = await _resolve_front_future(cast(IB, ib), "ESU6", front_roll_days=8)
    assert pinned.localSymbol == "ESU6"
    assert ib.requested[-1] == "ES"


async def test_pinovany_neznamy_nebo_expirovany_je_chyba_setupu() -> None:
    today = dt.datetime.now(dt.UTC).date()
    ib = _FakeIB(_contracts(today))
    with pytest.raises(InstrumentSetupError, match="IBKR nezná"):
        await _resolve_front_future(cast(IB, ib), "ESH9", front_roll_days=8)
    expired = _FakeIB([_es("ESM6", (today - dt.timedelta(days=90)).strftime("%Y%m%d"))])
    with pytest.raises(InstrumentSetupError, match="expiroval"):
        await _resolve_front_future(cast(IB, expired), "ESM6", front_roll_days=8)
