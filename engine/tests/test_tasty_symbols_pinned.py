"""tasty SymbolMap: kořen roluje, pinovaný kontrakt bere přesně; řetěz je per produkt (#1191)."""

import datetime as dt
from typing import Any, cast

from gexlens_engine.tasty.session import TastySession
from gexlens_engine.tasty.symbols import SymbolMap


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_json(self, path: str) -> dict[str, Any]:
        self.calls.append(path)
        if path.startswith("/instruments/futures"):
            near = (dt.date.today() + dt.timedelta(days=5)).isoformat()
            far = (dt.date.today() + dt.timedelta(days=95)).isoformat()
            return {
                "data": {
                    "items": [
                        {
                            "symbol": "/ESU6",
                            "streamer-symbol": "/ESU26:XCME",
                            "expiration-date": near,
                        },
                        {
                            "symbol": "/ESZ6",
                            "streamer-symbol": "/ESZ26:XCME",
                            "expiration-date": far,
                        },
                    ]
                }
            }
        if path.startswith("/option-chains/"):
            # Equity nested chain (#206): data.items
            return {
                "data": {
                    "items": [
                        {
                            "expirations": [
                                {
                                    "expiration-date": "2026-09-18",
                                    "strikes": [
                                        {
                                            "strike-price": "60.0",
                                            "call-streamer-symbol": ".KO260918C60",
                                            "put-streamer-symbol": ".KO260918P60",
                                        }
                                    ],
                                }
                            ]
                        }
                    ]
                }
            }
        return {"data": {"option-chains": []}}


async def test_front_future_koren_vs_pinovany() -> None:
    session = _FakeSession()
    symbols = SymbolMap(cast(TastySession, session), front_roll_days=8)
    assert await symbols.front_future("ES") == "/ESZ26:XCME"
    assert await symbols.front_future("ESU6") == "/ESU26:XCME"
    assert await symbols.front_future("ESH9") is None
    assert all("product-code=ES" in call for call in session.calls)


async def test_chain_je_per_koren() -> None:
    session = _FakeSession()
    symbols = SymbolMap(cast(TastySession, session), front_roll_days=8)
    today = dt.date.today()
    await symbols.chain("ESU6", today)
    await symbols.chain("ES", today)
    assert session.calls == ["/futures-option-chains/ES/nested"]  # druhé volání z cache


async def test_equity_podklad_a_retez() -> None:
    """#206: akcie/ETF/index — podklad je symbol sám, řetěz z /option-chains/{symbol}/nested."""
    session = _FakeSession()
    symbols = SymbolMap(cast(TastySession, session), front_roll_days=8)
    assert await symbols.front_future("KO") == "KO"
    assert await symbols.front_future("SPX") == "SPX"
    chain = await symbols.chain("KO", dt.date.today())
    assert chain.by_contract[("20260918", 60.0, "C")] == ".KO260918C60"
    assert chain.by_contract[("20260918", 60.0, "P")] == ".KO260918P60"
    assert "/option-chains/KO/nested" in session.calls
    assert not any("product-code" in call for call in session.calls)
