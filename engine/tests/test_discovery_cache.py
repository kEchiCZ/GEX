"""Testy cache discovery pro degradovaný start bez IBKR (#1153, varianta A)."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from gexlens_engine.__main__ import _contract_from_cache, _front_to_cache
from gexlens_engine.discovery_cache import (
    CACHE_FILENAME,
    DiscoveryCache,
    FrontFuture,
)
from gexlens_engine.ibkr.discovery import ExpiryInfo

TODAY = dt.date(2026, 9, 14)


def front(**overrides: object) -> FrontFuture:
    values: dict[str, object] = {
        "symbol": "NQ",
        "con_id": 770561204,
        "exchange": "CME",
        "multiplier": "20",
        "last_trade_date": "20260918",
        "local_symbol": "NQU6",
        "trading_class": "NQ",
    }
    values.update(overrides)
    return FrontFuture(**values)  # type: ignore[arg-type]


def infos() -> list[ExpiryInfo]:
    return [
        ExpiryInfo("Q1A", "20260911", "CME", "20", (24000.0, 24010.0)),  # včerejší, expirovaná
        ExpiryInfo("Q1A", "20260914", "CME", "20", (24000.0, 24010.0, 24020.0)),
        ExpiryInfo("Q2A", "20260915", "CME", "20", (24000.0, 24020.0)),
    ]


def test_store_load_roundtrip_a_filtr_expirovanych(tmp_path: Path) -> None:
    cache = DiscoveryCache(tmp_path / CACHE_FILENAME)
    cache.store(front(), infos())
    cached = cache.load("NQ", today=TODAY)
    assert cached is not None
    assert cached.front == front()
    assert [info.expiry for info in cached.expiries] == ["20260911", "20260914", "20260915"]
    # Dnešní pipeline bere jen neexpirované — 0DTE dneška je první
    assert [info.expiry for info in cached.unexpired(TODAY)] == ["20260914", "20260915"]
    assert cached.unexpired(TODAY)[0].strikes == (24000.0, 24010.0, 24020.0)
    # Jiný symbol v cache není
    assert cache.load("ES", today=TODAY) is None


def test_store_prepisuje_jen_svuj_symbol(tmp_path: Path) -> None:
    cache = DiscoveryCache(tmp_path / CACHE_FILENAME)
    cache.store(front(), infos())
    cache.store(front(symbol="ES", local_symbol="ESU6", multiplier="50"), infos())
    cache.store(front(local_symbol="NQZ6", last_trade_date="20261218"), infos())
    assert cache.load("ES", today=TODAY) is not None
    nq = cache.load("NQ", today=TODAY)
    assert nq is not None and nq.front.local_symbol == "NQZ6"


def test_zaznam_bez_dnesni_expirace_nebo_stary_se_nepouzije(tmp_path: Path) -> None:
    cache = DiscoveryCache(tmp_path / CACHE_FILENAME)
    cache.store(front(), infos())
    # Všechny expirace v minulosti → None (pipeline by jela nad mrtvým řetězem)
    assert cache.load("NQ", today=dt.date(2026, 9, 20)) is None
    # Front kontrakt po expiraci → None, i když řetěz by ještě měl expirace
    cache.store(front(last_trade_date="20260910"), infos())
    assert cache.load("NQ", today=TODAY) is None
    # Starší než MAX_AGE_DAYS → None
    path = tmp_path / CACHE_FILENAME
    data = json.loads(path.read_text(encoding="utf-8"))
    data["NQ"]["front"]["last_trade_date"] = "20260918"
    data["NQ"]["stored_at"] = (dt.datetime.now(dt.UTC) - dt.timedelta(days=15)).isoformat()
    path.write_text(json.dumps(data), encoding="utf-8")
    assert cache.load("NQ", today=TODAY) is None


def test_poskozeny_soubor_neshodi_start(tmp_path: Path) -> None:
    path = tmp_path / CACHE_FILENAME
    path.write_text("{ tohle není json", encoding="utf-8")
    cache = DiscoveryCache(path)
    assert cache.load("NQ", today=TODAY) is None
    cache.store(front(), infos())  # přepíše poškozený obsah
    assert cache.load("NQ", today=TODAY) is not None
    path.write_text(json.dumps({"NQ": {"stored_at": "x"}}), encoding="utf-8")
    assert cache.load("NQ", today=TODAY) is None


def test_kontrakt_z_cache_nese_conid_a_multiplikator() -> None:
    contract = _contract_from_cache(front())
    assert contract.conId == 770561204
    assert contract.localSymbol == "NQU6"
    assert contract.multiplier == "20"
    assert contract.exchange == "CME"
    assert contract.lastTradeDateOrContractMonth == "20260918"
    # Roundtrip zpět do cache je beze ztráty
    assert _front_to_cache(contract, "NQ") == front()
