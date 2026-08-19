"""Testy diagnostiky procesu (#771): dump zásobníků na SIGUSR1."""

import faulthandler
import signal

import pytest

from gexlens_engine.diagnostics import install_stack_dump

pytestmark = pytest.mark.skipif(
    not hasattr(signal, "SIGUSR1"), reason="SIGUSR1 na této platformě není (Windows)"
)


def test_install_registers_handler() -> None:
    faulthandler.unregister(signal.SIGUSR1)
    try:
        assert install_stack_dump() is True
    finally:
        faulthandler.unregister(signal.SIGUSR1)


def test_signal_dumps_stack_and_process_survives(capfd: pytest.CaptureFixture[str]) -> None:
    """Jádro věci: zásobník se vypíše a proces běží dál.

    SIGABRT přes PYTHONFAULTHANDLER by zásobník vypsal taky, jenže proces zabije —
    a při ladění výpadku (#770) je právě běžící proces to jediné, co ještě nese stav.
    """
    install_stack_dump()
    try:
        signal.raise_signal(signal.SIGUSR1)
        # Test pokračuje, takže signál proces nezabil.
        err = capfd.readouterr().err
        assert "Current thread" in err
        assert __file__.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] in err
    finally:
        faulthandler.unregister(signal.SIGUSR1)


def test_install_is_idempotent() -> None:
    """Opakované volání nesmí spadnout ani přestat fungovat."""
    try:
        assert install_stack_dump() is True
        assert install_stack_dump() is True
    finally:
        faulthandler.unregister(signal.SIGUSR1)
