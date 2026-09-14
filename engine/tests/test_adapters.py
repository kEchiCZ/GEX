"""Testy produkčních adaptérů nad fake objekty (žádné live IBKR — CLAUDE.md pravidlo 4)."""

import datetime as dt
from types import SimpleNamespace
from typing import Any, cast

from ib_async import IB, Contract

from gexlens_engine.adapters import IbHistoricalClient

TODAY = dt.date(2026, 7, 23)


class FakeHistoricalIB:
    """Zaznamenává endDateTime a vrací připravené BarData-like řádky."""

    def __init__(self, bars: list[SimpleNamespace]) -> None:
        self.bars = bars
        self.end_date_times: list[object] = []

    async def reqHistoricalDataAsync(self, contract: object, **kwargs: object) -> list[Any]:
        self.end_date_times.append(kwargs["endDateTime"])
        return list(self.bars)


def fake_bar(ts: dt.datetime | dt.date, close: float = 100.0) -> SimpleNamespace:
    return SimpleNamespace(
        date=ts, open=close - 1, high=close + 1, low=close - 2, close=close, volume=5
    )


def make_client(ib: FakeHistoricalIB) -> IbHistoricalClient:
    return IbHistoricalClient(cast(IB, ib), Contract(symbol="ES"))


async def test_past_day_requests_next_midnight_and_filters() -> None:
    ib = FakeHistoricalIB(
        [
            fake_bar(dt.datetime(2026, 7, 21, 23, 59, tzinfo=dt.UTC)),  # sousední den — pryč
            fake_bar(dt.datetime(2026, 7, 22, 0, 0, tzinfo=dt.UTC)),
            fake_bar(dt.datetime(2026, 7, 22, 15, 30)),  # naivní ts → doplní se UTC
            fake_bar(dt.date(2026, 7, 22)),  # denní bar (date) — pryč
        ]
    )
    bars = await make_client(ib).fetch_day_bars("ES", dt.date(2026, 7, 22))

    assert ib.end_date_times == [dt.datetime(2026, 7, 23, 0, 0, tzinfo=dt.UTC)]
    assert [bar.ts for bar in bars] == [
        dt.datetime(2026, 7, 22, 0, 0, tzinfo=dt.UTC),
        dt.datetime(2026, 7, 22, 15, 30, tzinfo=dt.UTC),
    ]
    assert bars[0].volume == 5.0


async def test_today_clamps_end_to_now() -> None:
    # HMDS odmítá endDateTime v budoucnosti ("query returned no data") —
    # pro dnešek musí jít prázdný string (= aktuální čas), jinak se díra
    # po výpadku streamu nikdy nedoplní (#221)
    today = dt.datetime.now(dt.UTC).date()
    ib = FakeHistoricalIB([fake_bar(dt.datetime.now(dt.UTC).replace(second=0, microsecond=0))])
    bars = await make_client(ib).fetch_day_bars("ES", today)

    assert ib.end_date_times == [""]
    assert len(bars) == 1


async def test_kvalifikace_kontraktu_ma_strop(monkeypatch: Any) -> None:
    """#1153: sec-def farma při Error 1100 neodpoví nikdy — kvalifikace musí
    skončit timeoutem a vrátit None, ne držet setup pipeline navěky."""
    import asyncio

    from gexlens_engine import adapters
    from gexlens_engine.adapters import IbQuoteStreamer
    from gexlens_engine.ibkr.discovery import OptionContractSpec

    class HangingIB:
        async def qualifyContractsAsync(self, *contracts: Any) -> list[Any]:
            await asyncio.sleep(3600)
            return []

    monkeypatch.setattr(adapters, "QUALIFY_TIMEOUT_S", 0.05)
    streamer = IbQuoteStreamer(cast(IB, HangingIB()))
    spec = OptionContractSpec("ES", "FOP", "20260914", 7600.0, "C", "CME", "E2A", "50")
    assert await streamer._contract(spec) is None
    # Po timeoutu farma platí za mrtvou: další kontrakt selže hned, bez čekání
    import time

    other = OptionContractSpec("ES", "FOP", "20260914", 7605.0, "P", "CME", "E2A", "50")
    started = time.monotonic()
    assert await streamer._contract(other) is None
    assert time.monotonic() - started < 0.04
