"""Úlohy Task Scheduleru logují mimo repo (#1638, lessons-learned 2026-09-10).

Repo leží pod Defenderem „Řízený přístup ke složkám" a pwsh ze Store má
povolení jen pro konkrétní verzi: po aktualizaci pwsh přesměrování logu do repa
selže dřív, než skript začne (10. 9., 24. 9. a 19.–24. 10. 2026 — walk-forward
a kompaktace VHDX tak tiše neběžely). Statická kontrola registrací, bez pwsh.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
REGISTRATIONS = sorted(SCRIPTS.glob("register-*-task.ps1"))


def test_registrace_uloh_se_nasly() -> None:
    assert len(REGISTRATIONS) >= 6


@pytest.mark.parametrize("path", REGISTRATIONS, ids=lambda path: path.name)
def test_uloha_loguje_do_ops_log_dir_ne_do_repa(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert re.search(r"^\$log = Join-Path \(Get-OpsLogDir\) '[\w-]+\.log'$", text, re.MULTILINE)
    assert not re.search(r"Join-Path \$repo '[^']*\.log'", text)
