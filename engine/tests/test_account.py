"""Testy rozpoznání IBKR účtu (#446)."""

from gexlens_engine.ibkr.account import classify_accounts


def test_paper_account_recognised() -> None:
    info = classify_accounts(["DU1234567"])
    assert info.paper is True
    assert info.label == "DU1234567 (paper)"


def test_live_account_recognised() -> None:
    info = classify_accounts(["U7654321"])
    assert info.paper is False
    assert "živý" in info.label


def test_mixed_accounts_are_not_reported_as_paper() -> None:
    """Smíšený seznam by tvrdil „paper", zatímco data tečou z živého účtu."""
    assert classify_accounts(["DU1", "U2"]).paper is False


def test_missing_accounts_are_unknown() -> None:
    """TWS účty pošle až po připojení — do té doby se nesmí hádat."""
    for value in ([], None, "", ["   "]):
        info = classify_accounts(value)
        assert info.paper is None
        assert info.label == "neznámý účet"
