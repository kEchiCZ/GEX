"""Skripty harnessu bez sítě (#1887): `check-brief.py`, `done-gate.py`, `session-digest.py`
a výběr kontrol v `scripts/check.py`.

Skripty mají v názvu pomlčku, proto se načítají ze souboru. GitHub a git nahrazují náhradní
funkce; done-gate běží nad dočasným repem git s `origin/main`.
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


def load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


brief_mod = load("harness_check_brief", REPO / ".claude/skills/spec/check-brief.py")
gate_mod = load("harness_done_gate", REPO / ".claude/hooks/done-gate.py")
digest_mod = load("harness_session_digest", REPO / ".claude/skills/retro/session-digest.py")
check_mod = load("harness_check", REPO / "scripts/check.py")

EXISTING = {
    "engine/src/gexlens_engine/compute/settle.py",
    "docs/manual/ADMIN-MANUAL.md",
    "engine/src/gexlens_engine/__main__.py",
}


def exists(path: str) -> bool:
    return path in EXISTING


def commit_ok(sha: str) -> bool:
    return sha == "82bcefc"


def brief_text(
    name: str = "L1", blockers: str = "—", change: str | None = None, estimate: str = "~190 ř."
) -> str:
    change = change or "engine/src/gexlens_engine/__main__.py, docs/manual/ADMIN-MANUAL.md"
    return f"""### {name} — Predikát ve /status
Současné → požadované: /status nemá stáří → nese pole published (true/false/null).
Kritéria přijetí:
- [ ] Obchodní den po publikaci → true; bez změny → false.
- [ ] (regrese) Klíč cache beze změny.
Kontext (platí k 82bcefc):
- Rozhodnutí: Q1 A (#1611 Rozhodnuto).
- Číst: engine/src/gexlens_engine/compute/settle.py
- Změnit: {change}
- Nové: engine/tests/test_wide_oi_status.py
Šev: čistá funkce s časem jako parametrem.
Ověření: pytest engine/tests/test_wide_oi_status.py na 82bcefc selže, po dokončení projde.
Odhad: {estimate}
Mimo rozsah: alert, značka v UI.
Začíná po: {blockers}
"""


# --- check-brief --mode spec ---------------------------------------------------------------


def test_valid_batch_passes() -> None:
    text = brief_text() + "\n" + brief_text("L2", blockers="L1, po nasazení L1")
    report = brief_mod.check_batch(brief_mod.split_batch(text), exists, commit_ok)
    assert report == {"L1": [], "L2": []}


def test_missing_lines_and_criteria_fail() -> None:
    text = "### L1 — holý list\nSoučasné → požadované: x\nZačíná po: —\n"
    errors = brief_mod.check_batch(brief_mod.split_batch(text), exists, commit_ok)["L1"]
    assert any("Šev" in e or "šev" in e for e in errors)
    assert any("kritérium" in e for e in errors)
    assert any("Změnit ani Nové" in e for e in errors)


def test_limits_and_paths_fail() -> None:
    many = ", ".join(f"engine/src/gexlens_engine/m{i}.py" for i in range(9))
    errors = brief_mod.check_brief(
        brief_mod.split_batch(brief_text(change=many, estimate="~700 ř."))[0], exists, commit_ok
    )
    assert any("9 > 8" in e for e in errors)
    assert any("700" in e for e in errors)
    assert any("m0.py na origin/main neexistuje" in e for e in errors)


def test_new_file_that_exists_and_unknown_commit_fail() -> None:
    text = (
        brief_text()
        .replace("82bcefc", "deadbee")
        .replace(
            "engine/tests/test_wide_oi_status.py\n",
            "engine/src/gexlens_engine/compute/settle.py\n",
            1,
        )
    )
    errors = brief_mod.check_brief(brief_mod.split_batch(text)[0], exists, commit_ok)
    assert any("deadbee" in e for e in errors)
    assert any("už existuje" in e for e in errors)


def test_blocker_cycle_and_unknown_fail() -> None:
    text = brief_text("L1", blockers="L2") + brief_text("L2", blockers="L1, L3")
    report = brief_mod.check_batch(brief_mod.split_batch(text), exists, commit_ok)
    assert any("cyklus" in e for e in report["L1"])
    assert any("L3 není v této dávce" in e for e in report["L2"])


# --- check-brief --mode implement ----------------------------------------------------------

OWNER = "vlastnik"


def issue(
    body: str,
    comments: list[dict[str, Any]],
    labels: tuple[str, ...] = ("ready",),
    parent_comments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "number": 1900,
        "body": body,
        "createdAt": "2026-11-20T10:00:00Z",
        "lastEditedAt": "2026-11-21T10:00:00Z",
        "author": {"login": OWNER},
        "labels": {"nodes": [{"name": n} for n in labels]},
        "comments": {"nodes": comments},
        "parent": {"number": 1899, "comments": {"nodes": parent_comments or []}},
    }


def comment(body: str, when: str = "2026-11-22T08:00:00Z", who: str = OWNER) -> dict[str, Any]:
    return {"author": {"login": who}, "createdAt": when, "body": body}


SPEC_BODY = "[AI Agent] spec · Opus 5.5 · 21. 11.\n\n" + brief_text(blockers="#1877")


def implement(item: dict[str, Any], merged: bool = True) -> list[str]:
    errors: list[str] = brief_mod.check_implement(item, OWNER, lambda _n: merged, exists, commit_ok)
    return errors


def test_approved_list_passes() -> None:
    assert implement(issue(SPEC_BODY, [comment("Schvaluji, žluťoučký kůň.")])) == []


def test_approval_under_parent_counts() -> None:
    item = issue(SPEC_BODY, [], parent_comments=[comment("Listy L1 a L2 schvaluji.")])
    assert implement(item) == []


@pytest.mark.parametrize(
    ("item", "needle"),
    [
        (issue(SPEC_BODY, [comment("Schvaluji")], labels=()), "ready"),
        (issue(SPEC_BODY, [comment("ok")], labels=("ready", "in-progress")), "in-progress"),
        (issue(SPEC_BODY, [comment("Schvaluji", when="2026-11-20T12:00:00Z")]), "schválení"),
        (issue(SPEC_BODY, [comment("[AI Agent] session · schvaluji za vlastníka")]), "schválení"),
        (issue(SPEC_BODY, [comment("Schvaluji", who="cizi")]), "schválení"),
        (issue(SPEC_BODY.replace("#1877", "L1"), [comment("ok")]), "L1"),
        (issue(brief_text(), [comment("ok")]), "/grillme"),
    ],
)
def test_implement_gate_fails(item: dict[str, Any], needle: str) -> None:
    assert any(needle in e for e in implement(item)), implement(item)


def test_unmerged_blocker_fails() -> None:
    errors = implement(issue(SPEC_BODY, [comment("Schvaluji")]), merged=False)
    assert any("#1877" in e for e in errors)


def test_grilling_skip_by_owner() -> None:
    skip = comment("Překlep v tooltipu.\nGrilling: přeskočit\nZačíná po: #1877")
    item = issue("Oprav překlep v tooltipu IV Rank.", [skip], labels=())
    assert implement(item) == []
    assert any("#1877" in e for e in implement(item, merged=False))


def test_grilling_skip_needs_owner_text_without_marker() -> None:
    marked = comment("[AI Agent] session\nGrilling: přeskočit")
    old = comment("Grilling: přeskočit", when="2026-11-20T12:00:00Z")
    for item in (issue("Oprav překlep.", [marked], labels=()), issue("x", [old], labels=())):
        assert implement(item), item["comments"]


# --- done-gate -----------------------------------------------------------------------------


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(
        path,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "start",
    )
    git(path, "update-ref", "refs/remotes/origin/main", "HEAD")
    return path


def gate_input(repo: Path, tmp_path: Path, **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "hook_event_name": "Stop",
        "cwd": str(repo),
        "session_id": "s1",
        "scratchpad_dir": str(tmp_path),
    }
    data.update(extra)
    return data


def run_gate(data: dict[str, Any] | bytes) -> subprocess.CompletedProcess[bytes]:
    raw = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
    return subprocess.run(
        [sys.executable, "-I", str(REPO / ".claude/hooks/done-gate.py")],
        input=raw,
        capture_output=True,
        timeout=60,
        check=False,
    )


def test_gate_invalid_input_blocks() -> None:
    assert run_gate(b"[1]").returncode == 2
    assert run_gate(b"neni json").returncode == 2


def test_gate_no_changes_passes(repo: Path, tmp_path: Path) -> None:
    assert run_gate(gate_input(repo, tmp_path)).returncode == 0


def test_gate_subagent_needs_worktree_line(repo: Path, tmp_path: Path) -> None:
    data = gate_input(
        repo,
        tmp_path,
        hook_event_name="SubagentStop",
        agent_id="a1",
        last_assistant_message="DONE\nHotovo.",
    )
    result = run_gate(data)
    assert result.returncode == 2
    assert "Worktree:" in result.stderr.decode("utf-8")
    released = run_gate({**data, "stop_hook_active": True})
    assert released.returncode == 0
    assert "ČERVENÁ" in json.loads(released.stdout.decode("utf-8"))["systemMessage"]


def test_gate_blocks_twice_then_releases(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "zmena.py").write_text("x = 1\n", "utf-8")
    calls: list[Path] = []

    def red(path: Path) -> tuple[int, str]:
        calls.append(path)
        return 1, "ruff: E501 řádek je příliš dlouhý"

    monkeypatch.setattr(gate_mod, "run_check", red)
    data = gate_input(
        repo,
        tmp_path,
        hook_event_name="SubagentStop",
        agent_id="a2",
        last_assistant_message=f"DONE\nWorktree: {repo}",
    )
    codes = []
    for _ in range(3):
        with pytest.raises(SystemExit) as exit_info:
            gate_mod.gate(data)
        codes.append(exit_info.value.code)
    assert codes == [2, 2, 0]
    assert calls and all(p.resolve() == repo.resolve() for p in calls)


def test_gate_green_is_remembered(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "zmena.py").write_text("x = 1\n", "utf-8")
    calls: list[Path] = []

    def green(path: Path) -> tuple[int, str]:
        calls.append(path)
        return 0, "ok"

    monkeypatch.setattr(gate_mod, "run_check", green)
    for _ in range(2):
        with pytest.raises(SystemExit) as exit_info:
            gate_mod.gate(gate_input(repo, tmp_path))
        assert exit_info.value.code == 0
    assert len(calls) == 1


# --- session-digest ------------------------------------------------------------------------


def write_jsonl(path: Path, entries: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", "utf-8")


def answer(msg_id: str, inp: int, write: int, read: int, out: int) -> dict[str, Any]:
    usage = {
        "input_tokens": inp,
        "cache_creation_input_tokens": write,
        "cache_read_input_tokens": read,
        "output_tokens": out,
    }
    return {"type": "assistant", "message": {"id": msg_id, "usage": usage}}


def test_digest_counts_each_response_once_with_subagents(tmp_path: Path) -> None:
    main = tmp_path / "abc.jsonl"
    write_jsonl(
        main,
        [
            {"type": "user"},
            answer("m1", 2, 100, 0, 5),
            answer("m1", 2, 100, 0, 7),
            answer("m2", 1, 10, 100, 3),
        ],
    )
    write_jsonl(tmp_path / "abc" / "subagents" / "agent-x.jsonl", [answer("s1", 3, 50, 0, 9)])
    rows = dict(digest_mod.digest_tokens(main))
    assert rows["hlavní session"].responses == 2
    assert rows["hlavní session"].output == 10
    assert rows["hlavní session"].peak_context == 111
    assert rows["subagent agent-x"].cache_write == 50


def test_digest_broken_line_is_error(tmp_path: Path) -> None:
    main = tmp_path / "bad.jsonl"
    main.write_text('{"type": "user"}\n{rozbitý\n', "utf-8")
    with pytest.raises(ValueError, match="bad.jsonl:2"):
        digest_mod.transcript_usage(main)


# --- scripts/check.py: výběr kontrol -------------------------------------------------------


def test_check_mypy_targets_include_dependents() -> None:
    targets = check_mod.mypy_targets(["engine/src/gexlens_engine/compute/settle.py"])
    assert targets == [
        "engine/src",
        "engine/tests",
        "news-engine/src",
        "news-engine/tests",
        "api/src",
        "api/tests",
    ]
    assert check_mod.mypy_targets(["api/src/gexlens_api/main.py"]) == ["api/src", "api/tests"]
    assert check_mod.mypy_targets(["docs/manual/ADMIN-MANUAL.md"]) == []


def test_check_selects_tests_that_import_changed_module() -> None:
    full, files = check_mod.select_tests(["engine/src/gexlens_engine/compute/settle.py"])
    assert full == []
    assert files and all(
        f.startswith(("engine/tests/", "api/tests/", "news-engine/tests/")) for f in files
    )
    text = "\n".join((REPO / f).read_text(encoding="utf-8") for f in files)
    assert "settle" in text


def test_check_selects_own_tests_for_harness_scripts() -> None:
    _, files = check_mod.select_tests([".claude/hooks/guard.py"])
    assert "engine/tests/test_harness_guard.py" in files


def test_check_conftest_runs_whole_package() -> None:
    full, _ = check_mod.select_tests(["api/tests/conftest.py"])
    assert full == ["api/tests"]
