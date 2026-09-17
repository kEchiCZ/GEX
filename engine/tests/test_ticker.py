"""Ticker = kořen nebo pinovaný kontrakt (#1191, ADR-0041)."""

import pytest

from gexlens_engine.ticker import Ticker, parse_ticker, pinned_contract, symbol_root


def test_koren_a_kontrakt() -> None:
    assert parse_ticker("ES") == Ticker("ES", None)
    assert parse_ticker(" esz6 ") == Ticker("ES", "Z6")
    assert parse_ticker("NQU6").local_symbol == "NQU6"
    assert parse_ticker("RTYH7") == Ticker("RTY", "H7")
    assert parse_ticker("6EZ6") == Ticker("6E", "Z6")
    assert parse_ticker("CLF7") == Ticker("CL", "F7")  # měsíční cyklus energií
    # Kořeny končící kódem měsíce zůstávají kořeny (bez číslice roku)
    assert parse_ticker("6J").pinned is False
    assert parse_ticker("M2K") == Ticker("M2K", None)
    assert parse_ticker("ZN").pinned is False


def test_neplatne() -> None:
    for raw in ("", "  ", "ES-Z6", "ES/Z6", "TOOLONGROOT"):
        with pytest.raises(ValueError):
            parse_ticker(raw)


def test_pomocne() -> None:
    assert symbol_root("ESU6") == "ES" and symbol_root("ES") == "ES"
    assert symbol_root("es-x") == "ES-X"  # nečitelné beze změny, jen uppercase
    assert pinned_contract("NQZ6") == "NQZ6" and pinned_contract("NQ") is None
    assert parse_ticker("ESU6").symbol == "ESU6" and parse_ticker("ES").symbol == "ES"
