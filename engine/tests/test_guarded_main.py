"""Testy zaručeného exitu po fatální výjimce v main() (#779)."""

import asyncio
import os
from unittest.mock import patch

import pytest

from gexlens_engine import __main__ as entry


async def test_fatal_exception_forces_exit() -> None:
    """Neošetřená výjimka nesmí nechat proces viset — musí volat os._exit(1)."""
    calls: list[int] = []

    async def boom() -> None:
        raise RuntimeError("simulovaný pád")

    with (
        patch.object(entry, "main", boom),
        patch.object(os, "_exit", side_effect=lambda code: calls.append(code)),
    ):
        await entry._guarded_main()

    assert calls == [1]


async def test_cancellation_propagates_without_exit() -> None:
    """CancelledError (řádné ukončení) guard NEsmí proměnit v exit 1."""
    calls: list[int] = []

    async def cancelled() -> None:
        raise asyncio.CancelledError

    with (
        patch.object(entry, "main", cancelled),
        patch.object(os, "_exit", side_effect=lambda code: calls.append(code)),
        pytest.raises(asyncio.CancelledError),
    ):
        await entry._guarded_main()

    assert calls == []
