"""Jeden příkaz kontroly pro lokál i CI (#1887): `uv run python scripts/check.py [--changed|--all]`.

- `--changed` (výchozí): jen co se dotklo změněných souborů proti `origin/main` (commity větve
  i necommitnuté změny). Python: ruff a `ruff format --check` na změněné soubory, mypy na dotčené
  balíky a jejich závislé (engine → news-engine → api), pytest na změněné testy a testy, které
  změněný modul nebo skript importují či jmenují. Frontend: lint, format, vitest a build.
  Volá ho done-gate na konci `/implement` a agenta implementer.
- `--all`: joby `python` a `frontend` z CI (bez skenu `security`, ten má vlastní job
  a `scripts/security-scan.ps1`). CI na skript přejde, až dá na stejném commitu stejný výsledek.

Chybějící `.venv`, `node_modules` nebo balík importovaný odjinud než z tohoto checkoutu
(worktree s cizím `.venv`) je hlasitá chyba s exit 2, nikdy tichý přeskok. Selhaná kontrola
= exit 1. Kroky běží sekvenčně (jeden testovací slot na stroji).
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Balík → (zdroj, testy, importní jméno); závislé balíky kontroluje mypy s ním.
PACKAGES = {
    "engine": ("engine/src", "engine/tests", "gexlens_engine"),
    "news-engine": ("news-engine/src", "news-engine/tests", "gexlens_news"),
    "api": ("api/src", "api/tests", "gexlens_api"),
}
DEPENDENTS = {"engine": ["news-engine", "api"], "news-engine": ["api"], "api": []}
# Změna, po které se pouští celá sada dotčeného balíku (fixtures, konfigurace, závislosti).
FULL_SUITE_TRIGGERS = ("conftest.py", "pyproject.toml", "uv.lock")


class EnvironmentProblem(Exception):
    """Prostředí neumí kontrolu spustit: hlasitá chyba, ne přeskok."""


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=False
    )
    if result.returncode != 0:
        raise EnvironmentProblem(f"git {' '.join(args)} selhal: {result.stderr.strip()}")
    return result.stdout


def changed_files() -> list[str]:
    """Soubory změněné proti merge-base s `origin/main`, včetně necommitnutých a nových."""
    base = git("merge-base", "origin/main", "HEAD").strip()
    names = set(git("diff", "--name-only", base).splitlines())
    names |= set(git("ls-files", "--others", "--exclude-standard").splitlines())
    return sorted(n for n in names if n and (ROOT / n).exists())


def package_of(path: str) -> str | None:
    for name, (src, tests, _) in PACKAGES.items():
        if path.startswith((src + "/", tests + "/")) or path == f"{name}/pyproject.toml":
            return name
    return None


def module_patterns(path: str) -> list[re.Pattern[str]]:
    """Vzory, podle kterých test prozradí, že se změněného souboru týká."""
    for src, _, _ in PACKAGES.values():
        if path.startswith(src + "/") and path.endswith(".py"):
            dotted = path[len(src) + 1 : -3].replace("/", ".").removesuffix(".__init__")
            parent, _, leaf = dotted.rpartition(".")
            patterns = [rf"\b{re.escape(dotted)}\b"]
            if parent:
                patterns.append(rf"from\s+{re.escape(parent)}\s+import\s+[^\n]*\b{leaf}\b")
            return [re.compile(p) for p in patterns]
    # Skript nebo hook mimo balíky: testy ho jmenují názvem souboru (vzor `REPO / "scripts"`).
    return [re.compile(rf"\b{re.escape(Path(path).name)}\b")]


def all_test_files() -> list[str]:
    return sorted(
        p.relative_to(ROOT).as_posix()
        for _, tests, _ in PACKAGES.values()
        for p in (ROOT / tests).rglob("test_*.py")
    )


def select_tests(changed: list[str]) -> tuple[list[str], list[str]]:
    """Vrátí (celé testovací adresáře, jednotlivé testové soubory)."""
    full: set[str] = set()
    files: set[str] = set()
    sources = [p for p in changed if p.endswith(".py")]
    for path in changed:
        pkg = package_of(path)
        if Path(path).name in FULL_SUITE_TRIGGERS:
            targets = [pkg] if pkg else list(PACKAGES)
            full.update(PACKAGES[t][1] for t in targets)
        elif Path(path).name.startswith("test_") and path.endswith(".py"):
            files.add(path)
    patterns = [
        pattern
        for path in sources
        if not Path(path).name.startswith("test_")
        for pattern in module_patterns(path)
    ]
    if patterns:
        for test in all_test_files():
            text = (ROOT / test).read_text(encoding="utf-8", errors="replace")
            if any(p.search(text) for p in patterns):
                files.add(test)
    files = {f for f in files if not any(f.startswith(d + "/") for d in full)}
    return sorted(full), sorted(files)


def mypy_targets(changed: list[str]) -> list[str]:
    touched = {pkg for path in changed if path.endswith(".py") and (pkg := package_of(path))}
    for pkg in list(touched):
        touched.update(DEPENDENTS[pkg])
    return [d for name in PACKAGES if name in touched for d in PACKAGES[name][:2]]


def check_environment(needs_frontend: bool) -> None:
    venv = ROOT / ".venv"
    if not venv.is_dir():
        raise EnvironmentProblem(f"chybí {venv}: spusť `uv sync --all-packages` v {ROOT}")
    for _, _, module in PACKAGES.values():
        probe = subprocess.run(
            [sys.executable, "-c", f"import {module}; print({module}.__file__)"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        location = Path(probe.stdout.strip()).resolve() if probe.returncode == 0 else None
        if location is None or ROOT not in location.parents:
            raise EnvironmentProblem(
                f"{module} se importuje z {location or 'nikam'}, ne z {ROOT}: interpret patří "
                "jinému checkoutu; spusť `uv sync --frozen --all-packages` v tomto worktree"
            )
    if needs_frontend:
        if not (ROOT / "frontend" / "node_modules").is_dir():
            raise EnvironmentProblem("chybí frontend/node_modules: `npm ci` ve frontend/")
        if shutil.which("npm") is None:
            raise EnvironmentProblem("npm není v PATH")


def run(title: str, cmd: list[str], cwd: Path = ROOT) -> bool:
    print(f"\n== {title}: {' '.join(cmd)}", flush=True)
    code = subprocess.run(cmd, cwd=cwd, check=False).returncode
    print(f"== {title}: {'OK' if code == 0 else f'SELHALO (exit {code})'}", flush=True)
    return code == 0


def npm(script: str) -> list[str]:
    exe = shutil.which("npm")
    if exe is None:
        raise EnvironmentProblem("npm není v PATH")
    return [exe, "run", script]


def python_steps(
    py_files: Iterable[str], mypy: list[str], tests: tuple[list[str], list[str]]
) -> list[tuple[str, list[str]]]:
    py = sys.executable
    files = list(py_files)
    steps: list[tuple[str, list[str]]] = []
    if files:
        steps.append(("ruff", [py, "-m", "ruff", "check", *files]))
        steps.append(("ruff format", [py, "-m", "ruff", "format", "--check", *files]))
    if mypy:
        steps.append(("mypy", [py, "-m", "mypy", *mypy]))
    full, single = tests
    if full or single:
        steps.append(("pytest", [py, "-m", "pytest", *full, *single]))
    return steps


def frontend_steps(smoke: bool) -> list[tuple[str, list[str]]]:
    scripts = ["lint", "format", "test", "build"] + (["smoke"] if smoke else [])
    return [(f"npm run {s}", npm(s)) for s in scripts]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--changed", action="store_true", help="jen dotčené soubory (výchozí)")
    mode.add_argument("--all", action="store_true", help="joby python a frontend z CI")
    args = parser.parse_args(argv)
    try:
        if args.all:
            check_environment(needs_frontend=True)
            py = sys.executable
            mypy = [d for src, tests, _ in PACKAGES.values() for d in (src, tests)]
            steps = [
                ("ruff", [py, "-m", "ruff", "check", "."]),
                ("ruff format", [py, "-m", "ruff", "format", "--check", "."]),
                ("mypy", [py, "-m", "mypy", *mypy]),
                ("pytest", [py, "-m", "pytest"]),
            ]
            front = frontend_steps(smoke=True)
        else:
            changed = changed_files()
            if not changed:
                print("check --changed: proti origin/main se nic nezměnilo")
                return 0
            needs_frontend = any(p.startswith("frontend/") for p in changed)
            check_environment(needs_frontend)
            py_files = [p for p in changed if p.endswith(".py")]
            steps = python_steps(py_files, mypy_targets(changed), select_tests(changed))
            front = frontend_steps(smoke=False) if needs_frontend else []
            print("check --changed: " + ", ".join(changed))
    except EnvironmentProblem as exc:
        print(f"check: CHYBA PROSTŘEDÍ — {exc}", file=sys.stderr)
        return 2
    results = [run(title, cmd) for title, cmd in steps]
    results += [run(title, cmd, ROOT / "frontend") for title, cmd in front]
    failed = results.count(False)
    print(f"\ncheck: {len(results) - failed}/{len(results)} kroků OK", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
