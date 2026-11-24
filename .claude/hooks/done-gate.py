"""Hook Stop a SubagentStop harnessu: kontrola před hotovo (done-gate).

Registrovaný jen ve frontmatteru `skills/implement/SKILL.md` (Stop) a `agents/implementer.md`
(Stop, Claude Code ho převede na SubagentStop). V běžné session neběží, tam je kontrolou CI.

1. Najde pracovní kopii: u SubagentStop z řádku `Worktree: <cesta>` v závěrečné zprávě agenta,
   u Stop z `cwd` vstupu.
2. Bez změny proti `origin/main` nebo bez změny od posledního zeleného běhu nedělá nic.
3. Spustí `uv run --frozen python scripts/check.py --changed` v kořeni pracovní kopie.
4. Červená zastaví konec (exit 2) nejvýš dvakrát za session a agenta, potřetí pustí se zprávou
   `done-gate: ČERVENÁ`. `onFailure` na Stop nepůsobí, proto skript blokuje sám.

Jen standardní knihovna; spouští se `python -I`.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

MAX_BLOCKS = 2
CHECK_TIMEOUT_S = 540  # pod limitem nástroje 10 min
TAIL_LINES = 60
WORKTREE_LINE = re.compile(r"^Worktree:\s*(.+?)\s*$", re.MULTILINE)


class GateError(Exception):
    """Okolí, ve kterém kontrola nejde spustit."""


def _emit(stream: Any, text: str) -> None:
    # Bajty UTF-8: `python -I` ignoruje PYTHONIOENCODING a Windows by psal v cp1250.
    stream.buffer.write(text.encode("utf-8"))
    stream.flush()


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise GateError(f"git {' '.join(args)} v {repo}: {result.stderr.strip()}")
    return result.stdout


def work_dir(data: dict[str, Any]) -> Path:
    if data.get("hook_event_name") == "SubagentStop":
        match = WORKTREE_LINE.search(str(data.get("last_assistant_message") or ""))
        if match is None:
            raise GateError(
                "závěrečná zpráva nemá řádek `Worktree: <cesta>`; doplň ho, done-gate podle něj "
                "spouští kontrolu"
            )
        return Path(match.group(1).strip("`'\""))
    return Path(str(data.get("cwd") or "."))


def fingerprint(repo: Path) -> str | None:
    """Otisk stavu pracovní kopie; None, když proti origin/main není co kontrolovat."""
    base = git(repo, "merge-base", "origin/main", "HEAD").strip()
    head = git(repo, "rev-parse", "HEAD").strip()
    status = git(repo, "status", "--porcelain")
    if head == base and not status.strip():
        return None
    diff = git(repo, "diff", "HEAD")
    return hashlib.sha256("\n".join((head, status, diff)).encode("utf-8")).hexdigest()


def state_path(data: dict[str, Any]) -> Path:
    folder = Path(str(data.get("scratchpad_dir") or tempfile.gettempdir()))
    key = f"{data.get('session_id', 'session')}-{data.get('agent_id') or 'main'}"
    return folder / f"done-gate-{re.sub(r'[^A-Za-z0-9_-]', '_', key)}.json"


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"green": None, "blocks": 0}
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise GateError(f"{path}: stav done-gate není objekt JSON")
    return state


def run_check(repo: Path) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["uv", "run", "--frozen", "python", "scripts/check.py", "--changed"],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CHECK_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, f"check.py --changed nedoběhl do {CHECK_TIMEOUT_S} s"
    except FileNotFoundError as exc:
        return 127, f"check.py nejde spustit: {exc}"
    return result.returncode, result.stdout + result.stderr


def release(message: str) -> None:
    _emit(sys.stdout, json.dumps({"systemMessage": message}, ensure_ascii=False))
    sys.exit(0)


def block(message: str) -> None:
    _emit(sys.stderr, f"done-gate: {message}\n")
    sys.exit(2)


def gate(data: dict[str, Any]) -> None:
    repo_dir = work_dir(data)
    if not repo_dir.is_dir():
        raise GateError(f"pracovní kopie {repo_dir} neexistuje")
    repo = Path(git(repo_dir, "rev-parse", "--show-toplevel").strip())
    current = fingerprint(repo)
    if current is None:
        sys.exit(0)
    path = state_path(data)
    state = load_state(path)
    if state.get("green") == current:
        sys.exit(0)
    code, output = run_check(repo)
    if code == 0:
        path.write_text(json.dumps({"green": current, "blocks": 0}), encoding="utf-8")
        sys.exit(0)
    blocks = int(state.get("blocks") or 0) + 1
    path.write_text(json.dumps({"green": state.get("green"), "blocks": blocks}), encoding="utf-8")
    tail = "\n".join(output.strip().splitlines()[-TAIL_LINES:])
    if blocks > MAX_BLOCKS:
        release(
            f"done-gate: ČERVENÁ — check.py --changed v {repo} selhal (exit {code}) "
            f"i po {MAX_BLOCKS} opravách; konečnou branou je CI"
        )
    hint = (
        "Oprav příčinu a skonči znovu."
        if blocks < MAX_BLOCKS
        else "Poslední pokus: když kontrola selže znovu, skonči se stavem DONE_WITH_CONCERNS "
        "a řádkem `done-gate: ČERVENÁ` ve zprávě."
    )
    block(
        f"check.py --changed v {repo} selhal (exit {code}, pokus {blocks}/{MAX_BLOCKS}).\n"
        f"{tail}\n{hint}"
    )


def main() -> None:
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        block(f"vstup hooku nejde přečíst ({type(exc).__name__}: {exc})")
    if not isinstance(data, dict):
        block(f"vstup hooku není objekt JSON ({type(data).__name__})")
    try:
        gate(data)
    except (GateError, OSError, ValueError, subprocess.SubprocessError) as exc:
        # Opakovaná chyba okolí nesmí agenta držet donekonečna: podruhé pustí s varováním.
        if data.get("stop_hook_active"):
            release(f"done-gate: ČERVENÁ — kontrola nejde spustit ({exc})")
        block(f"kontrola nejde spustit ({exc})")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # každé selhání zastaví, nikdy tiše nepouští
        block(f"neočekávaná chyba ({type(exc).__name__}: {exc})")
