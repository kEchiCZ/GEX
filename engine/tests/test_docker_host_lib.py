"""Rozhodovací logika provozních skriptů na hostiteli (#1552) — `scripts/lib/*.ps1`.

Pester v repu není (viz test_push_telegram), proto pytest volá pwsh nad čistými
funkcemi: rozhodnutí hlídače Dockeru, brána trhu `Get-GlobexClock` a
`Test-MarketClosed` (víkend, denní pauza, nedělní otevření, svátky CME —
AGENTS.md: hlídač při zavřeném trhu jen loguje), zámek údržby a porovnání
počtů řádků PG před a po aktualizaci Docker Desktopu.
Bez pwsh (lokální Linux bez PowerShellu) se přeskočí; runner CI ho má.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
LIB = REPO / "scripts" / "lib"
PWSH = shutil.which("pwsh")

pytestmark = pytest.mark.skipif(PWSH is None, reason="pwsh není v PATH")


def _pwsh(body: str) -> Any:
    """Dot-source knihoven, spustí `body` a vrátí jeho výstup převedený z JSON."""
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        # Windows konzole by jinak psala v kódové stránce OEM (→, á…)
        "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)\n"
        f". '{LIB / 'GlobexClock.ps1'}'\n"
        f". '{LIB / 'DockerHost.ps1'}'\n"
        f"{body} | ConvertTo-Json -Compress -Depth 4\n"
    )
    assert PWSH is not None
    done = subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.mark.parametrize(
    ("info_ok", "status", "busy", "minutes", "expected"),
    [
        ("$true", "stopped", "@()", 0, "ok"),
        # 13. 10. 2026: daemon pryč, Docker Desktop „stopped" → restart
        ("$false", "stopped", "@()", 0, "restart"),
        ("$false", "unknown", "@()", 0, "restart"),
        ("$false", "", "@()", 0, "restart"),
        # deploy / kompaktace / aktualizace Docker zastavují záměrně
        ("$false", "stopped", "@('compact-docker-vhdx')", 0, "skip-busy"),
        # restart jen při stavu ≠ running (rozhodnutí vlastníka) → jen upozornit
        ("$false", "running", "@()", 30, "hung"),
        # přechodový stav: chvíli počkat, po 10 min restart
        ("$false", "starting", "@()", 5, "wait"),
        ("$false", "starting", "@()", 10, "restart"),
    ],
)
def test_rozhodnuti_hlidace(
    info_ok: str, status: str, busy: str, minutes: int, expected: str
) -> None:
    action = _pwsh(
        f"Resolve-WatchdogAction -InfoOk {info_ok} -Status '{status}' -Busy {busy} "
        f"-IncidentMinutes {minutes}"
    )
    assert action == expected


@pytest.mark.parametrize(
    ("utc", "closed"),
    [
        # sobota — víkend
        ("2026-10-17 15:00", True),
        # úterý 21:30 UTC = 16:30 CDT — denní pauza
        ("2026-10-13 21:30", True),
        # úterý 20:59 UTC = 15:59 CDT — ještě otevřeno
        ("2026-10-13 20:59", False),
        # neděle 21:59 UTC = 16:59 CDT — před nedělním otevřením
        ("2026-10-18 21:59", True),
        # neděle 22:00 UTC = 17:00 CDT — nedělní otevření
        ("2026-10-18 22:00", False),
        # zima: úterý 22:30 UTC = 16:30 CST — denní pauza
        ("2026-11-10 22:30", True),
        # pátek 21:00 UTC = 16:00 CDT — začátek víkendu
        ("2026-10-16 21:00", True),
    ],
)
def test_brana_trhu(utc: str, closed: bool) -> None:
    clock = _pwsh(
        f"Get-GlobexClock -UtcNow ([datetime]'{utc}') | Select-Object Closed, MinutesToOpen"
    )
    assert clock["Closed"] is closed


def test_brana_trhu_minuty_do_otevreni_v_pauze() -> None:
    clock = _pwsh(
        "Get-GlobexClock -UtcNow ([datetime]'2026-10-13 21:30') | Select-Object MinutesToOpen"
    )
    assert clock["MinutesToOpen"] == pytest.approx(30)


def test_pocty_radku_pg_chybejici_tabulka_a_ztrata_poloviny() -> None:
    # Smazaný datový disk WSL = chybějící nebo vyprázdněné tabulky; malý pokles
    # přepočítávané tabulky (DELETE+INSERT) ztráta není
    lost = _pwsh(
        "Compare-PgRowCounts @{ setups = 1666; track_record = 500; annotations = 18 } "
        "@{ setups = 1660; track_record = 250 }"
    )
    assert lost == ["annotations (chybí)", "track_record (500 → 250)"]


def test_pocty_radku_pg_kazdy_pokles_do_logu() -> None:
    decreased = _pwsh(
        "Compare-PgRowCounts @{ setups = 1666; journal_entries = 25 } "
        "@{ setups = 1660; journal_entries = 25 } -MaxDropShare 0"
    )
    assert decreased == "setups (1666 → 1660)"


def test_pocty_radku_pg_prirustek_a_nova_tabulka_v_poradku() -> None:
    # prázdné pole rourou do ConvertTo-Json nic nepošle → porovnává se počet
    lost = _pwsh(
        "@(Compare-PgRowCounts @{ news_events = 100 } @{ news_events = 140; nova = 0 }).Count"
    )
    assert lost == 0


@pytest.mark.parametrize(
    ("utc", "closed"),
    [
        ("2026-10-17 15:00", True),  # sobota
        ("2026-10-13 21:30", True),  # úterý, denní pauza
        ("2026-10-13 20:59", False),  # úterý, otevřeno
        ("2026-10-18 21:59", True),  # neděle před otevřením
        ("2026-10-18 22:00", False),  # nedělní otevření
        # Svátky CME, které Get-GlobexClock nezná (ověřovatel #1552)
        ("2027-03-26 15:00", True),  # Velký pátek — Globex zavřený celý den
        ("2026-12-25 15:00", True),  # Vánoce
        ("2026-11-26 19:00", True),  # Díkůvzdání po 12:00 CT
        ("2026-11-26 17:00", False),  # Díkůvzdání dopoledne CT — obchoduje se
        # Walk-forward 23:30 místního času v týdnu posunu letního času (#1568):
        # pondělí 23:30 SEČ = 17:30 CDT otevřeno, pátek 23:30 SEČ už víkend
        ("2026-10-26 22:30", False),
        ("2026-10-30 22:30", True),
    ],
)
def test_trh_zavreny_podle_marketclock(utc: str, closed: bool) -> None:
    assert _pwsh(f"Test-MarketClosed -UtcNow ([datetime]'{utc}')") is closed


def _lock_body(tmp_path: Path, body: str) -> str:
    # Zámek leží v %LOCALAPPDATA%\GEXLens — v testu dočasný adresář
    return f"$env:LOCALAPPDATA = '{tmp_path}'\n{body}"


def test_zamek_udrzby_jen_jeden_drzitel(tmp_path: Path) -> None:
    result = _pwsh(
        _lock_body(
            tmp_path,
            "@{ first = Enter-MaintenanceLock 'compact-docker-vhdx'; "
            "second = Enter-MaintenanceLock 'update-wsl-docker-offhours'; "
            "holder = Get-MaintenanceLockHolder; "
            "after = $(Exit-MaintenanceLock; Get-MaintenanceLockHolder) }",
        )
    )
    assert result == {
        "first": True,
        "second": False,
        "holder": "compact-docker-vhdx",
        "after": None,
    }


def test_zamek_mrtveho_drzitele_se_uklidi(tmp_path: Path) -> None:
    # Úlohu zabil Task Scheduler: soubor zůstal, proces ne (PID mimo rozsah)
    result = _pwsh(
        _lock_body(
            tmp_path,
            "$dir = Join-Path $env:LOCALAPPDATA 'GEXLens'\n"
            "New-Item -ItemType Directory -Force $dir | Out-Null\n"
            "$path = Join-Path $dir 'docker-maintenance.lock'\n"
            "@{ name = 'compact-docker-vhdx'; pid = 2147483000; since = (Get-Date).ToString('s') } "
            "| ConvertTo-Json | Set-Content $path\n"
            "@{ holder = (Get-MaintenanceLockHolder 6>$null); exists = (Test-Path $path); "
            "taken = (Enter-MaintenanceLock 'update-wsl-docker-offhours') }",
        )
    )
    assert result == {"holder": None, "exists": False, "taken": True}
