"""Regresní sada guardu harnessu `.claude/hooks/guard.py` (#1887).

Guard po merge hlídá všechny sessions repa: běžné příkazy musí projít (exit 0), nebezpečné
zablokovat (exit 2) a chybný vstup zablokovat také (selhává zavřeně). Většina případů běží
jako podproces `python -I` s JSON na stdin v UTF-8, tedy stejně jako hook v Claude Code.
Případy, které by volaly git nebo GitHub (aktuální větev, soubory PR), jdou přes `evaluate`
s náhradními funkcemi.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
GUARD = REPO / ".claude" / "hooks" / "guard.py"

_spec = importlib.util.spec_from_file_location("harness_guard", GUARD)
assert _spec is not None and _spec.loader is not None
guard: Any = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = guard  # dataclasses hledají modul v sys.modules
_spec.loader.exec_module(guard)

MERGE = "scripts/merge-when-green.sh"


def run_guard(payload: object, cwd: Path | None = None) -> subprocess.CompletedProcess[bytes]:
    if isinstance(payload, bytes):
        data = payload
    elif isinstance(payload, str):
        data = payload.encode("utf-8")
    else:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return subprocess.run(
        [sys.executable, "-I", str(GUARD)],
        input=data,
        capture_output=True,
        cwd=cwd or REPO,
        timeout=30,
        check=False,
    )


def tool(command: str, cwd: Path, name: str = "Bash", agent: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "hook_event_name": "PreToolUse",
        "tool_name": name,
        "tool_input": {"command": command},
        "cwd": str(cwd),
    }
    if agent:
        payload["agent_type"] = agent
    return payload


def ctx(
    tmp_path: Path,
    *,
    branch: str = "feat/1887-harness-pilot-jadro",
    files: list[str] | None = None,
    implementer: bool = False,
) -> Any:
    def pr_files(_cwd: Path, _pr: int) -> list[str]:
        return files or []

    return guard.Context(
        cwd=tmp_path,
        is_implementer=implementer,
        merge_script=MERGE,
        current_branch=lambda _cwd: branch,
        pr_files=pr_files,
    )


@pytest.fixture
def body_ok(tmp_path: Path) -> Path:
    path = tmp_path / "telo.md"
    path.write_text("[AI Agent] session · Opus 5.5 · 24. 11.\n\nŽluťoučký kůň úpěl.\n", "utf-8")
    return path


# --- chybný vstup selhává zavřeně ----------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"[1, 2]",
        b"\xff\xfe invalid UTF-8",
        b'"jen retezec"',
        json.dumps({"tool_name": "Bash", "tool_input": ["git", "status"]}).encode(),
        json.dumps({"tool_name": "Bash", "tool_input": {"command": 5}}).encode(),
    ],
)
def test_invalid_input_blocks(payload: bytes) -> None:
    result = run_guard(payload)
    assert result.returncode == 2
    assert result.stderr.decode("utf-8").startswith("guard: ")


# --- běžné příkazy sessions projdou --------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "name"),
    [
        ("git status", "Bash"),
        ("git log --oneline -5", "Bash"),
        ("git diff origin/main...HEAD --stat", "Bash"),
        ("gh pr checks 1882", "Bash"),
        ("gh issue view 1887 --comments", "Bash"),
        ("gh api repos/kEchiCZ/GEX/pulls/1882/files --jq '.[].filename'", "Bash"),
        ("uv run pytest", "Bash"),
        ("uv run ruff check .", "Bash"),
        ("uv run ruff format --check .", "Bash"),
        ("cd frontend && npm run build", "Bash"),
        ("cd frontend; npm run build", "PowerShell"),
        ("scripts/merge-when-green.sh", "Bash"),
        ("pwsh scripts/backup-postgres.ps1", "PowerShell"),
        ("pwsh scripts/backup-postgres.ps1", "Bash"),
        ("git push -u origin feat/1887-harness-pilot-jadro", "Bash"),
        ("git add -A && git commit -m 'docs: zmínka o git push --force a -n ve zprávě'", "Bash"),
        ('git commit -m "fix(engine): oprava řádku — příliš žluťoučký kůň"', "PowerShell"),
        ("git commit -F zprava.txt", "Bash"),
        ("Get-Content .env.example", "PowerShell"),
        ("grep -c GEXLENS_ .env", "Bash"),
        ("git worktree list", "Bash"),
        ("git fetch origin && git status", "Bash"),
    ],
)
def test_common_commands_pass(command: str, name: str, tmp_path: Path) -> None:
    result = run_guard(tool(command, tmp_path, name))
    assert result.returncode == 0, result.stderr.decode("utf-8")
    assert result.stdout == b""


def test_commit_message_heredoc_passes(tmp_path: Path) -> None:
    command = (
        "git commit -m \"$(cat <<'EOF'\nfeat(harness): guard — přidán žluťoučký kůň\n\n"
        'Co-Authored-By: Claude <noreply@anthropic.com>\nEOF\n)"'
    )
    result = run_guard(tool(command, tmp_path))
    assert result.returncode == 0, result.stderr.decode("utf-8")


def test_gh_writes_with_marked_body_file_pass(tmp_path: Path, body_ok: Path) -> None:
    commands = [
        f"gh issue comment 1887 --body-file {body_ok.name}",
        f"gh issue create --title \"[AI Agent] fix(engine): řádek\" --body-file '{body_ok}'",
        f"gh issue edit 1887 --body-file {body_ok.name} --add-label ready",
        f"gh pr create --draft --title 'feat(harness): pilot' --body-file {body_ok.name}",
        f"gh api repos/kEchiCZ/GEX/issues/1887/comments -F body=@{body_ok.name}",
        "gh issue edit 1887 --add-label in-progress --remove-label ready",
        "gh pr ready 1890",
    ]
    for command in commands:
        result = run_guard(tool(command, tmp_path))
        assert result.returncode == 0, (command, result.stderr.decode("utf-8"))


# --- nebezpečné příkazy se blokují ---------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "name"),
    [
        ("git push origin main", "Bash"),
        ("git push origin HEAD:main", "Bash"),
        ("git push origin +feat/x:refs/heads/main", "Bash"),
        ("git push origin --delete main", "Bash"),
        ("git -C ../GEX push origin main", "Bash"),
        ("git --git-dir=../GEX/.git --work-tree=../GEX push origin main", "Bash"),
        ("git push --force origin feat/1887-x", "Bash"),
        ("git push -fu origin feat/1887-x", "Bash"),
        ("git push --force-with-lease origin main", "Bash"),
        ("git push --all origin", "Bash"),
        ("git push --no-verify origin feat/1887-x", "Bash"),
        ("git commit --no-verify -m x", "Bash"),
        ("git commit -anm 'zpráva'", "Bash"),
        ("git -c core.hooksPath=/dev/null commit -m x", "Bash"),
        ("git config core.hooksPath .githooks", "Bash"),
        ("git config --local core.hookspath x", "PowerShell"),
        ('sh -c "git push origin main"', "Bash"),
        ("bash -lc 'git push --force origin feat/x'", "Bash"),
        ("cmd /c git push origin main", "PowerShell"),
        ('pwsh -NoProfile -Command "git push origin main"', "Bash"),
        ("env GIT_TRACE=1 git push origin main", "Bash"),
        ("FOO=1 git push origin main", "Bash"),
        ("& git.exe push origin main", "PowerShell"),
        ("git $(echo push) origin main", "Bash"),
        ("git push origin `echo main`", "Bash"),
        ('echo "$(git rev-parse HEAD)"', "Bash"),
        ("gh pr merge 1882 --squash", "Bash"),
        ("gh api -X PUT repos/kEchiCZ/GEX/pulls/1882/merge", "Bash"),
        ("gh api graphql -f query='mutation{mergePullRequest(input:{})}'", "Bash"),
        ("cp hook .git/hooks/pre-commit", "Bash"),
        ("Set-Content .git\\hooks\\pre-commit 'x'", "PowerShell"),
        ("cat .env", "Bash"),
        ("Get-Content .env", "PowerShell"),
        ("type D:\\GEX\\.env.dev", "PowerShell"),
        ("uv run --env-file .env python x.py", "Bash"),
        ("gh issue comment 1887 --body 'ahoj'", "Bash"),
        ("gh pr create --fill", "Bash"),
        ("gh issue create --title '[AI Agent] x'", "Bash"),
        ("gh issue comment 1887", "Bash"),
        ("gh issue close 1887 --comment 'hotovo'", "Bash"),
        ("gh api repos/kEchiCZ/GEX/issues/1887/comments -f body='[AI Agent] x'", "Bash"),
        ("gh api repos/kEchiCZ/GEX/issues -F title=bez -F body=@telo.md", "Bash"),
        ("git push origin 'main", "Bash"),
    ],
)
def test_dangerous_commands_block(command: str, name: str, tmp_path: Path) -> None:
    (tmp_path / "telo.md").write_text("[AI Agent] x\n", "utf-8")
    result = run_guard(tool(command, tmp_path, name))
    assert result.returncode == 2, command
    assert "guard: " in result.stderr.decode("utf-8")


def test_body_file_checks(tmp_path: Path) -> None:
    (tmp_path / "prazdny.md").write_text("  \n", "utf-8")
    (tmp_path / "bez.md").write_text("Napsal agent bez značky\n", "utf-8")
    cases = {
        "gh issue comment 1 --body-file prazdny.md": "prázdný",
        "gh issue comment 1 --body-file bez.md": "[AI Agent]",
        "gh issue comment 1 --body-file chybi.md": "nejde přečíst",
        "gh issue comment 1 --body-file -": "stdin",
        "gh issue create --title 'Bez značky' --body-file bez.md": "titulek",
        "gh issue edit 1 --title 'Bez značky'": "titulek",
    }
    for command, needle in cases.items():
        result = run_guard(tool(command, tmp_path))
        assert result.returncode == 2, command
        assert needle in result.stderr.decode("utf-8"), command


def test_block_message_is_utf8(tmp_path: Path) -> None:
    result = run_guard(tool("git push origin main", tmp_path))
    assert "zakázaný" in result.stderr.decode("utf-8")


def test_write_into_git_hooks_blocks(tmp_path: Path) -> None:
    for path in (".git/hooks/pre-commit", "D:\\GEX\\.git\\config"):
        payload = {"tool_name": "Write", "tool_input": {"file_path": path, "content": "x"}}
        assert run_guard(payload).returncode == 2, path
    ok = {"tool_name": "Edit", "tool_input": {"file_path": "docs/příručka.md", "new_string": "ž"}}
    assert run_guard(ok).returncode == 0


# --- dotaz místo bloku ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "git reset --hard origin/main",
        "git clean -fd",
        "git branch -D feat/1800-stara",
        "git push --force-with-lease origin feat/1887-harness-pilot-jadro",
    ],
)
def test_risky_commands_ask(command: str, tmp_path: Path) -> None:
    result = run_guard(tool(command, tmp_path))
    assert result.returncode == 0
    decision = json.loads(result.stdout.decode("utf-8"))["hookSpecificOutput"]
    assert decision["permissionDecision"] == "ask"
    assert decision["permissionDecisionReason"].startswith("guard: ")


# --- implementer na GitHub nezapisuje ------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "gh issue comment 1887 --body-file telo.md",
        "gh pr create --draft --title x --body-file telo.md",
        "gh issue edit 1887 --add-label ready",
        "gh pr ready 1890",
        "gh api -X POST repos/kEchiCZ/GEX/issues/1887/comments -F body=@telo.md",
        "gh api repos/kEchiCZ/GEX/issues/1887/labels -f labels[]=ready",
        "scripts/merge-when-green.sh 1890",
    ],
)
def test_implementer_gh_writes_block(command: str, tmp_path: Path) -> None:
    (tmp_path / "telo.md").write_text("[AI Agent] implement · Sonnet 5.5 · 24. 11.\n", "utf-8")
    result = run_guard(tool(command, tmp_path, agent="implementer"))
    assert result.returncode == 2, command


@pytest.mark.parametrize(
    "command",
    ["gh issue view 1887 --comments", "gh pr checks 1890", "gh api repos/kEchiCZ/GEX/pulls/1"],
)
def test_implementer_gh_reads_pass(command: str, tmp_path: Path) -> None:
    result = run_guard(tool(command, tmp_path, agent="implementer"))
    assert result.returncode == 0, result.stderr.decode("utf-8")


# --- merge skriptem, aktuální větev (bez sítě) ---------------------------------------------


def test_merge_script_with_harness_files_blocks(tmp_path: Path) -> None:
    files = ["engine/src/gexlens_engine/x.py", ".claude/hooks/guard.py"]
    commands = [
        f"{MERGE} 1890",
        f"bash {MERGE} 1890 1800",
        "git fetch origin main && git show origin/main:scripts/merge-when-green.sh "
        '> "$TMP/mwg.sh" && bash "$TMP/mwg.sh" 1890',
    ]
    for command in commands:
        verdict = guard.evaluate(tool(command, tmp_path), ctx(tmp_path, files=files))
        assert verdict.blocks, command
        assert "1890" in verdict.blocks[-1]


@pytest.mark.parametrize(
    "path",
    [
        "AGENTS.md",
        "CLAUDE.md",
        "CLAUDE.local.md",
        ".github/workflows/ci.yml",
        ".githooks/pre-commit",
        "scripts/check.py",
        MERGE,
    ],
)
def test_protected_paths(path: str) -> None:
    assert guard._is_protected(path, MERGE)


def test_merge_script_without_harness_files_passes(tmp_path: Path) -> None:
    files = ["engine/src/gexlens_engine/x.py", "docs/manual/ADMIN-MANUAL.md"]
    verdict = guard.evaluate(tool(f"{MERGE} 1890", tmp_path), ctx(tmp_path, files=files))
    assert not verdict.blocks and not verdict.asks


def test_merge_script_gh_failure_raises(tmp_path: Path) -> None:
    def broken(_cwd: Path, _pr: int) -> list[str]:
        raise guard.GuardError("gh api selhalo")

    context = ctx(tmp_path)
    context.pr_files = broken
    with pytest.raises(guard.GuardError):
        guard.evaluate(tool(f"{MERGE} 1890", tmp_path), context)


def test_bare_push_on_main_blocks(tmp_path: Path) -> None:
    for command in ("git push", "git push origin", "git push -u origin HEAD"):
        on_main = guard.evaluate(tool(command, tmp_path), ctx(tmp_path, branch="main"))
        assert on_main.blocks, command
        on_feat = guard.evaluate(tool(command, tmp_path), ctx(tmp_path))
        assert not on_feat.blocks, command
