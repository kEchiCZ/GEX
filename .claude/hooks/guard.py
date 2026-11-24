"""Hook PreToolUse harnessu: pojistka proti omylu agenta, ne bezpečnostní bariéra.

Registrace v `.claude/settings.json` s `"onFailure": "block"`: hook, který nejde spustit, spadne
nebo vyprší, zablokuje nástroj stejně jako exit 2. Skript sám selhává zavřeně: nečitelný vstup
a každá neočekávaná výjimka = exit 2 s důvodem na stderr.

Bash a PowerShell: rozdělí složený příkaz (`&&`, `||`, `;`, `|`, `&`, nový řádek), sundá obaly
(`sh -c`, `bash -c`, `cmd /c`, `pwsh -Command`, `env`, `VAR=`) a globální volby gitu (`-C`, `-c`,
`--git-dir`…) a každý díl posoudí zvlášť.

- blok (exit 2): push do `main`, `--force`, `--no-verify`, změna `core.hooksPath`, zápis do
  `.git/hooks`, výpis `.env`, `$( )` nebo zpětné apostrofy spolu s gitem (kromě
  `"$(cat <<'EOF' … EOF)"`), merge PR mimo skript merge projektu, merge PR se soubory harnessu,
  zápis na GitHub bez `--body-file` se značkou `[AI Agent]`, titulek issue bez značky;
  agentovi `implementer` každý zápis přes `gh`;
- dotaz (`permissionDecision: ask`): `reset --hard`, `clean -f`, `branch -D`,
  `push --force-with-lease` na `feat/` a `fix/`.

Write a Edit: blok zápisu do `.git/hooks/` a `.git/config`. Tajemství v zapisovaném textu hlídá
globální `credentials-guard.py`, tady se neopakují.

Jen standardní knihovna; spouští se `python -I`, musí běžet i na Pythonu 3.11.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MARKER = "[AI Agent]"
IMPLEMENTER = "implementer"
PROTECTED_BRANCH = "main"
# Soubory harnessu: PR, který je mění, merguje vlastník (výjimka z automatického merge).
PROTECTED_PREFIXES = (".claude/", ".github/workflows/", ".githooks/")
PROTECTED_FILES = ("AGENTS.md", "scripts/check.py")
PROTECTED_GLOB = re.compile(r"^CLAUDE[^/]*\.md$")
HARNESS_JSON = Path(__file__).resolve().parents[1] / "harness.json"
GH_TIMEOUT_S = 20

SHELL_WRAPPERS = {"sh", "bash", "zsh", "dash"}
PWSH_WRAPPERS = {"pwsh", "powershell"}
ENV_READERS = {
    "cat",
    "type",
    "get-content",
    "gc",
    "head",
    "tail",
    "less",
    "more",
    "bat",
    "nl",
    "xxd",
    "od",
    "strings",
    "format-hex",
}
READ_ONLY = {
    "ls",
    "dir",
    "cat",
    "type",
    "get-childitem",
    "gci",
    "get-content",
    "gc",
    "head",
    "tail",
    "stat",
    "test",
    "test-path",
    "get-item",
    "file",
}
GIT_GLOBAL_WITH_VALUE = {
    "-C",
    "-c",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--config-env",
    "--exec-path",
    "--super-prefix",
    "--list-cmds",
}
GIT_GLOBAL_FLAGS = {
    "--no-pager",
    "-p",
    "--paginate",
    "-P",
    "--bare",
    "--no-replace-objects",
    "--literal-pathspecs",
    "--glob-pathspecs",
    "--noglob-pathspecs",
    "--icase-pathspecs",
    "--no-optional-locks",
    "--no-advice",
}
COMMIT_WITH_VALUE = {
    "-m",
    "--message",
    "-F",
    "--file",
    "-C",
    "--reuse-message",
    "-c",
    "--reedit-message",
    "--author",
    "--date",
    "-t",
    "--template",
    "--fixup",
    "--squash",
    "--cleanup",
    "--trailer",
    "--pathspec-from-file",
}
PUSH_WITH_VALUE = {
    "--repo",
    "--receive-pack",
    "--exec",
    "-o",
    "--push-option",
    "--recurse-submodules",
}
GH_WITH_BODY = {
    ("issue", "create"),
    ("issue", "comment"),
    ("issue", "edit"),
    ("pr", "create"),
    ("pr", "comment"),
    ("pr", "edit"),
    ("pr", "review"),
}
GH_BODY_REQUIRED = {("issue", "create"), ("issue", "comment"), ("pr", "create"), ("pr", "comment")}
GH_READ = {"view", "list", "status", "checks", "diff", "search", "browse", "watch"}
API_WRITE_PATH = re.compile(
    r"^/?repos/[^/]+/[^/]+/(issues(/\d+)?(/comments)?|issues/comments/\d+|pulls(/\d+)?"
    r"(/comments|/reviews)?|pulls/comments/\d+|pulls/\d+/reviews/\d+)/?$"
)
API_ISSUE_PATH = re.compile(r"^/?repos/[^/]+/[^/]+/issues(/\d+)?/?$")
API_MERGE_PATH = re.compile(r"pulls/[^/]+/merge/?$")
HEREDOC_SUBST = re.compile(
    r"\$\(\s*cat\s+<<-?\s*(['\"])(\w+)\1[^\n]*\n.*?\n\s*\2\s*\n?\s*\)", re.DOTALL
)
HEREDOC = re.compile(r"(?<!<)<<(?!<)-?\s*(['\"]?)(\w+)\1")
PS_HERESTRING = re.compile(r"@(['\"])\r?\n.*?\r?\n\1@", re.DOTALL)
ENV_FILE = re.compile(r"(^|/)\.env(\.[\w-]+)?$")
GH_API_WITH_VALUE = {
    "-X",
    "--method",
    "-H",
    "--header",
    "-f",
    "--raw-field",
    "-F",
    "--field",
    "--input",
    "-q",
    "--jq",
    "-t",
    "--template",
    "--hostname",
    "--cache",
    "-p",
    "--preview",
}


class GuardError(Exception):
    """Vstup nebo okolí, které guard neumí posoudit: blokuje."""


@dataclass
class Context:
    """Okolí jednoho volání hooku; funkce s IO jsou vyměnitelné kvůli testům."""

    cwd: Path
    is_implementer: bool
    merge_script: str
    current_branch: Callable[[Path], str]
    pr_files: Callable[[Path, int], list[str]]
    # V PowerShellu je zpětný apostrof escape znak a zpětné lomítko oddělovač cesty.
    powershell: bool = False


@dataclass
class Verdict:
    blocks: list[str] = field(default_factory=list)
    asks: list[str] = field(default_factory=list)

    def block(self, reason: str) -> None:
        self.blocks.append(reason)

    def ask(self, reason: str) -> None:
        self.asks.append(reason)


# --- rozklad příkazu ------------------------------------------------------------------------


def _strip_heredocs(command: str) -> tuple[str, bool]:
    """Odstraní těla heredoců a here-stringů; vrátí text a zda zbylo dosazení v těle.

    `"$(cat <<'EOF' … EOF)"` se nahradí jedním slovem: tělo s uvozeným oddělovačem je statický
    text (vzor zprávy commitu), ne dosazení příkazu.
    """
    command = HEREDOC_SUBST.sub(" HEREDOC ", command)
    command = PS_HERESTRING.sub(" HERESTRING ", command)
    expanding_body = False
    out: list[str] = []
    lines = command.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        delimiters = HEREDOC.findall(line)
        i += 1
        for quote, delim in delimiters:
            while i < len(lines) and lines[i].strip() != delim:
                if not quote and ("$(" in lines[i] or "`" in lines[i]):
                    expanding_body = True
                i += 1
            i += 1
    return "\n".join(out), expanding_body


def _tokenize(command: str, powershell: bool) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>")
    lexer.whitespace_split = True
    lexer.whitespace = " \t\r"
    lexer.commenters = ""
    if powershell:
        lexer.escape = ""
    return list(lexer)


def split_segments(command: str, powershell: bool = False) -> list[list[str]]:
    """Rozdělí příkaz na jednoduché příkazy (seznam slov); přesměrování zůstávají slovy."""
    segments: list[list[str]] = []
    for line in command.split("\n"):
        current: list[str] = []
        for token in _tokenize(line, powershell):
            if token and set(token) <= set(";&|()"):
                if current:
                    segments.append(current)
                current = []
            else:
                current.append(token)
        if current:
            segments.append(current)
    return segments


def _exe(word: str) -> str:
    name = word.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def _unwrap(words: list[str]) -> tuple[list[str], str | None]:
    """Sundá `env`, `VAR=` a obaly shellu; vrátí slova příkazu a případný vnořený příkaz."""
    i = 0
    while i < len(words):
        word = words[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", word):
            i += 1
        elif _exe(word) == "env":
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 1
        elif _exe(word) in {"sudo", "command", "exec", "nohup", "time"}:
            i += 1
        else:
            break
    words = words[i:]
    if not words:
        return [], None
    exe = _exe(words[0])
    if exe in SHELL_WRAPPERS:
        for j, word in enumerate(words[1:], start=1):
            if re.fullmatch(r"-[a-z]*c[a-z]*", word) and j + 1 < len(words):
                return words, words[j + 1]
        return words, None
    if exe == "cmd":
        for j, word in enumerate(words[1:], start=1):
            if word.lower() in {"/c", "/k"}:
                return words, " ".join(words[j + 1 :])
        return words, None
    if exe in PWSH_WRAPPERS:
        for j, word in enumerate(words[1:], start=1):
            if word.lower() in {"-c", "-command", "-commandwithargs"} and j + 1 < len(words):
                return words, " ".join(words[j + 1 :])
        return words, None
    return words, None


# --- git ------------------------------------------------------------------------------------


def _git_args(words: list[str], verdict: Verdict) -> list[str]:
    """Sundá globální volby gitu; `-c core.hooksPath=` rovnou zablokuje."""
    args = words[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in GIT_GLOBAL_WITH_VALUE:
            value = args[i + 1] if i + 1 < len(args) else ""
            if arg in {"-c", "--config-env"} and value.lower().startswith("core.hookspath"):
                verdict.block("změna core.hooksPath (git -c) je zakázaná")
            i += 2
        elif arg.split("=", 1)[0] in GIT_GLOBAL_WITH_VALUE and "=" in arg:
            if arg.lower().startswith("--config-env=core.hookspath"):
                verdict.block("změna core.hooksPath (git --config-env) je zakázaná")
            i += 1
        elif arg in GIT_GLOBAL_FLAGS:
            i += 1
        else:
            break
    return args[i:]


def _short_cluster(arg: str) -> str:
    """Písmena krátkých voleb (`-fu` → `fu`), jinak prázdný řetězec."""
    if re.fullmatch(r"-[A-Za-z]+", arg):
        return arg[1:]
    return ""


def _cluster_has_no_verify(arg: str) -> bool:
    """`-an` obsahuje `-n`; v `-mnote` je `note` hodnota `-m`, ne volba."""
    for letter in _short_cluster(arg):
        if letter in "mFCct":
            return False
        if letter == "n":
            return True
    return False


def _positional(args: list[str], with_value: set[str]) -> tuple[list[str], list[str]]:
    """Rozdělí argumenty na volby a poziční slova; přeskočí hodnoty voleb."""
    options: list[str] = []
    positional: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            positional.extend(args[i + 1 :])
            break
        if arg.startswith("-"):
            options.append(arg)
            if arg in with_value:
                i += 1
        else:
            positional.append(arg)
        i += 1
    return options, positional


def _refspec_targets(refspecs: list[str], current: Callable[[], str]) -> list[str]:
    targets: list[str] = []
    for spec in refspecs:
        spec = spec.lstrip("+")
        dst = spec.split(":", 1)[1] if ":" in spec else spec
        if dst in {"HEAD", "@"}:
            dst = current()
        targets.append(dst.removeprefix("refs/heads/"))
    return targets


def check_git(words: list[str], ctx: Context, verdict: Verdict) -> None:
    args = _git_args(words, verdict)
    if not args:
        return
    sub, rest = args[0], args[1:]

    def current() -> str:
        return ctx.current_branch(ctx.cwd)

    if sub == "push":
        options, positional = _positional(rest, PUSH_WITH_VALUE)
        clusters = "".join(_short_cluster(o) for o in options)
        if "--no-verify" in options:
            verdict.block("git push --no-verify je zakázaný")
        if (
            "--force" in options
            or "f" in clusters
            or any(
                o.startswith("--force") and not o.startswith("--force-with-lease") for o in options
            )
        ):
            verdict.block("git push --force je zakázaný")
        if {"--all", "--mirror"} & set(options):
            verdict.block("git push --all/--mirror by poslal i main")
        refspecs = positional[1:]
        deleting = "--delete" in options or "d" in clusters
        if not refspecs:
            targets = [] if deleting else [current()]
        else:
            targets = _refspec_targets(refspecs, current)
        if PROTECTED_BRANCH in targets:
            verdict.block(f"push do {PROTECTED_BRANCH} je zakázaný, jen přes PR")
        if any(o.startswith("--force-with-lease") for o in options):
            if all(t.startswith(("feat/", "fix/")) for t in targets) and targets:
                verdict.ask("push --force-with-lease přepíše vzdálenou větev listu")
            else:
                verdict.block("push --force-with-lease jen na větve feat/ a fix/")
    elif sub == "commit":
        options, _ = _positional(rest, COMMIT_WITH_VALUE)
        if "--no-verify" in options or any(_cluster_has_no_verify(o) for o in options):
            verdict.block("git commit --no-verify (-n) je zakázaný")
    elif sub in {"merge", "rebase", "cherry-pick", "am", "revert"}:
        if "--no-verify" in rest:
            verdict.block(f"git {sub} --no-verify je zakázaný")
    elif sub == "config":
        if any(a.lower().startswith("core.hookspath") for a in rest):
            verdict.block("změna core.hooksPath je zakázaná")
    elif sub == "reset":
        if "--hard" in rest:
            verdict.ask("git reset --hard zahodí necommitnutou práci")
    elif sub == "clean":
        if "--force" in rest or any("f" in _short_cluster(a) for a in rest):
            verdict.ask("git clean -f smaže nesledované soubory")
    elif sub == "branch":
        clusters = "".join(_short_cluster(a) for a in rest)
        if "D" in clusters or ("--delete" in rest and "--force" in rest):
            verdict.ask("git branch -D smaže nemergnutou větev")


# --- gh -------------------------------------------------------------------------------------


def _option_values(args: list[str], names: set[str]) -> list[str]:
    values: list[str] = []
    for i, arg in enumerate(args):
        if arg in names and i + 1 < len(args):
            values.append(args[i + 1])
        else:
            for name in names:
                if name.startswith("--") and arg.startswith(name + "="):
                    values.append(arg.split("=", 1)[1])
    return values


def _has_option(args: list[str], names: set[str]) -> bool:
    return any(a in names or a.split("=", 1)[0] in names for a in args)


def _check_marker_file(raw: str, ctx: Context, what: str, verdict: Verdict) -> None:
    if raw == "-":
        verdict.block(f"{what}: tělo ze stdin nejde ověřit, použij --body-file se souborem")
        return
    path = Path(raw)
    if not path.is_absolute():
        path = ctx.cwd / path
    try:
        data = path.read_bytes()
    except OSError as exc:
        verdict.block(f"{what}: soubor těla {raw} nejde přečíst ({exc.strerror})")
        return
    if not data.strip():
        verdict.block(f"{what}: soubor těla {raw} je prázdný (prázdné tělo by smazalo text)")
        return
    first = data.decode("utf-8-sig", errors="replace").lstrip().split("\n", 1)[0]
    if not first.startswith(MARKER):
        verdict.block(f"{what}: první řádek těla musí začínat {MARKER}")


def _check_api(args: list[str], ctx: Context, verdict: Verdict) -> None:
    _, positional = _positional(args, GH_API_WITH_VALUE)
    path = positional[0] if positional else ""
    methods = _option_values(args, {"-X", "--method"})
    raw_fields = _option_values(args, {"-f", "--raw-field"})
    typed_fields = _option_values(args, {"-F", "--field"})
    inputs = _option_values(args, {"--input"})
    has_payload = raw_fields or typed_fields or inputs
    method = (methods[-1] if methods else ("POST" if has_payload else "GET")).upper()
    if path == "graphql":
        if any("mergepullrequest" in a.lower() for a in args):
            verdict.block("merge PR přes GraphQL mimo skript merge projektu je zakázaný")
        if ctx.is_implementer and any(re.search(r"\bmutation\b", a) for a in args):
            verdict.block("implementer na GitHub nezapisuje (gh api graphql mutation)")
        return
    if API_MERGE_PATH.search(path) and method != "GET":
        verdict.block("merge PR přes gh api mimo skript merge projektu je zakázaný")
    if method == "GET":
        return
    if ctx.is_implementer:
        verdict.block(f"implementer na GitHub nezapisuje (gh api {method} {path})")
        return
    if method not in {"POST", "PATCH", "PUT"} or not API_WRITE_PATH.match(path):
        return
    fields = [(item, False) for item in raw_fields] + [(item, True) for item in typed_fields]
    for item, typed in fields:
        key, _, value = item.partition("=")
        from_file = typed and value.startswith("@")
        if key == "body":
            if from_file:
                _check_marker_file(value[1:], ctx, "gh api body", verdict)
            else:
                verdict.block("gh api: inline body je zakázané, použij -F body=@soubor")
        if key == "title" and API_ISSUE_PATH.match(path):
            if from_file:
                verdict.block("gh api: titulek issue zapiš přímo hodnotou, ne souborem")
            elif not value.startswith(MARKER + " "):
                verdict.block(f"gh api: titulek issue musí začínat '{MARKER} '")
    for raw in inputs:
        _check_api_input(raw, path, ctx, verdict)


def _check_api_input(raw: str, path: str, ctx: Context, verdict: Verdict) -> None:
    if raw == "-":
        verdict.block("gh api --input ze stdin nejde ověřit")
        return
    file = Path(raw) if Path(raw).is_absolute() else ctx.cwd / raw
    try:
        payload = json.loads(file.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        verdict.block(f"gh api --input {raw} nejde přečíst jako JSON ({exc})")
        return
    if not isinstance(payload, dict):
        verdict.block(f"gh api --input {raw}: čekám objekt JSON")
        return
    body = payload.get("body")
    if body is not None and not str(body).lstrip().startswith(MARKER):
        verdict.block(f"gh api --input: body musí začínat {MARKER}")
    title = payload.get("title")
    if title is not None and API_ISSUE_PATH.match(path) and not str(title).startswith(MARKER + " "):
        verdict.block(f"gh api --input: titulek issue musí začínat '{MARKER} '")


def check_gh(words: list[str], ctx: Context, verdict: Verdict) -> None:
    args = words[1:]
    while args and (args[0] in {"-R", "--repo"} or args[0].startswith("--repo=")):
        args = args[1:] if args[0].startswith("--repo=") else args[2:]
    if not args:
        return
    group = args[0]
    action = args[1] if len(args) > 1 else ""
    rest = args[2:]
    if group == "api":
        _check_api(args[1:], ctx, verdict)
        return
    if (group, action) == ("pr", "merge"):
        verdict.block(f"gh pr merge mimo skript merge projektu ({ctx.merge_script}) je zakázaný")
        return
    if ctx.is_implementer and group not in {"auth", "help", "version"}:
        if action and action not in GH_READ:
            verdict.block(f"implementer na GitHub nezapisuje (gh {group} {action})")
        return
    if (group, action) in {("issue", "close"), ("pr", "close"), ("issue", "reopen")} and (
        _has_option(rest, {"-c", "--comment"})
    ):
        verdict.block(
            f"gh {group} {action} --comment: komentář zapiš přes gh {group} comment --body-file"
        )
    if (group, action) not in GH_WITH_BODY:
        return
    what = f"gh {group} {action}"
    if _has_option(rest, {"-b", "--body"}):
        verdict.block(f"{what}: inline --body je zakázané, použij --body-file")
    if (group, action) == ("pr", "create") and _has_option(
        rest, {"-f", "--fill", "--fill-first", "--fill-verbose"}
    ):
        verdict.block(f"{what} --fill: tělo PR musí začínat {MARKER}, použij --body-file")
    body_files = _option_values(rest, {"-F", "--body-file"})
    for raw in body_files:
        _check_marker_file(raw, ctx, what, verdict)
    if not body_files and (group, action) in GH_BODY_REQUIRED:
        verdict.block(f"{what} bez --body-file (tělo musí začínat {MARKER})")
    if group == "issue" and action in {"create", "edit"}:
        titles = _option_values(rest, {"-t", "--title"})
        if action == "create" and not titles:
            verdict.block(f"{what} bez --title (titulek musí začínat '{MARKER} ')")
        for title in titles:
            if not title.startswith(MARKER + " "):
                verdict.block(f"{what}: titulek issue musí začínat '{MARKER} '")


def _is_protected(path: str, merge_script: str) -> bool:
    path = path.replace("\\", "/")
    return (
        path.startswith(PROTECTED_PREFIXES)
        or path in PROTECTED_FILES
        or path == merge_script
        or bool(PROTECTED_GLOB.match(path))
    )


def check_merge_script(segments: list[list[str]], ctx: Context, verdict: Verdict) -> None:
    """Volání skriptu merge s číslem PR: PR se soubory harnessu merguje vlastník."""
    name = ctx.merge_script.rsplit("/", 1)[-1]
    if not any(name in " ".join(seg) for seg in segments):
        return
    prs: list[int] = []
    for seg in segments:
        words, _ = _unwrap(seg)
        # `skript.sh <PR> [timeout]` i `bash "$TMP/mwg.sh" <PR>`: číslo PR je slovo za skriptem.
        script_at = next((i for i, w in enumerate(words) if w.endswith(".sh")), None)
        if script_at is None or not (script_at == 0 or _exe(words[0]) in SHELL_WRAPPERS):
            continue
        if script_at + 1 < len(words) and words[script_at + 1].isdigit():
            prs.append(int(words[script_at + 1]))
    if not prs:
        return
    if ctx.is_implementer:
        verdict.block("implementer nemerguje, merge spouští hlavní session")
        return
    for pr in prs:
        files = ctx.pr_files(ctx.cwd, pr)
        harness = [f for f in files if _is_protected(f, ctx.merge_script)]
        if harness:
            listed = ", ".join(harness[:5])
            verdict.block(f"PR #{pr} mění soubory harnessu ({listed}); merguje vlastník")


# --- posouzení ------------------------------------------------------------------------------


def check_segment(words: list[str], ctx: Context, verdict: Verdict, depth: int) -> None:
    words, nested = _unwrap(words)
    if nested is not None:
        check_command(nested, ctx, verdict, depth + 1)
        return
    if not words:
        return
    exe = _exe(words[0])
    lowered = [w.replace("\\", "/") for w in words]
    if exe not in READ_ONLY and any(re.search(r"(^|/)\.git/hooks(/|$)", w) for w in lowered):
        verdict.block("zápis do .git/hooks je zakázaný (hooky gitu jsou verzované)")
    if exe in ENV_READERS and any(
        ENV_FILE.search(w) and not w.endswith(".example") for w in lowered[1:]
    ):
        verdict.block("výpis .env je zakázaný; tajemství se referencují jen názvem proměnné")
    if exe == "uv" and any(w == "--env-file" or w.startswith("--env-file=") for w in words):
        verdict.block("uv --env-file při chybě vypíše řádek .env; exportuj proměnné bez výpisu")
    if exe == "git":
        check_git(words, ctx, verdict)
    elif exe == "gh":
        check_gh(words, ctx, verdict)


def check_command(command: str, ctx: Context, verdict: Verdict, depth: int = 0) -> None:
    if depth > 3:
        verdict.block("příliš hluboko vnořené obaly shellu")
        return
    text, expanding_body = _strip_heredocs(command)
    mentions_git = bool(re.search(r"(^|[\s;&|(\"'/\\])(git|gh)(\.exe)?([\s\"';)]|$)", text))
    substitution = "$(" in text or ("`" in text and not ctx.powershell)
    if mentions_git and (substitution or expanding_body):
        verdict.block(
            "dosazení příkazu $( ) nebo `…` spolu s gitem/gh je zakázané "
            "(zprávu commitu předej přes -F soubor nebo \"$(cat <<'EOF' … EOF)\")"
        )
    try:
        segments = split_segments(text, ctx.powershell)
    except ValueError as exc:
        if mentions_git:
            verdict.block(f"příkaz s gitem/gh nejde spolehlivě rozložit ({exc})")
        return
    for seg in segments:
        check_segment(seg, ctx, verdict, depth)
    check_merge_script(segments, ctx, verdict)


def check_write(tool_input: dict[str, Any], verdict: Verdict) -> None:
    path = str(tool_input.get("file_path") or "").replace("\\", "/")
    if re.search(r"(^|/)\.git/hooks/", path):
        verdict.block("zápis do .git/hooks je zakázaný (hooky gitu jsou verzované)")
    if re.search(r"(^|/)\.git/config$", path):
        verdict.block("přímý zápis do .git/config je zakázaný (core.hooksPath)")


def evaluate(data: dict[str, Any], ctx: Context) -> Verdict:
    verdict = Verdict()
    tool = data.get("tool_name")
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        raise GuardError(f"tool_input není objekt JSON ({type(tool_input).__name__})")
    if tool in {"Bash", "PowerShell"}:
        command = tool_input.get("command")
        if not isinstance(command, str):
            raise GuardError("tool_input.command není řetězec")
        ctx.powershell = tool == "PowerShell"
        check_command(command, ctx, verdict)
    elif tool in {"Write", "Edit"}:
        check_write(tool_input, verdict)
    return verdict


# --- IO -------------------------------------------------------------------------------------


def _git_current_branch(cwd: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=GH_TIMEOUT_S,
        check=False,
    )
    branch = result.stdout.strip()
    if result.returncode != 0 or not branch:
        raise GuardError(f"aktuální větev nejde zjistit: {result.stderr.strip()}")
    return branch


def _gh_pr_files(cwd: Path, pr: int) -> list[str]:
    result = subprocess.run(
        [
            "gh",
            "api",
            "--paginate",
            f"repos/{{owner}}/{{repo}}/pulls/{pr}/files",
            "--jq",
            ".[].filename",
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=GH_TIMEOUT_S,
        check=False,
    )
    if result.returncode != 0:
        raise GuardError(f"soubory PR #{pr} nejde zjistit: {result.stderr.strip()}")
    return [line for line in result.stdout.splitlines() if line]


def load_merge_script(path: Path = HARNESS_JSON) -> str:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GuardError(f"{path} nejde načíst ({exc})") from exc
    script = config.get("merge_script") if isinstance(config, dict) else None
    if not isinstance(script, str) or not script:
        raise GuardError(f"{path}: chybí klíč merge_script")
    return script


def _emit(stream: Any, text: str) -> None:
    # Bajty UTF-8: `python -I` ignoruje PYTHONIOENCODING a Windows by psal v cp1250.
    stream.buffer.write(text.encode("utf-8"))
    stream.flush()


def block(reason: str) -> None:
    _emit(sys.stderr, f"guard: {reason}\n")
    sys.exit(2)


def main() -> None:
    # Surové bajty jako UTF-8: na Windows čte sys.stdin v kódové stránce ANSI (cp1250).
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        block(f"vstup hooku nejde přečíst, blokuji ({type(exc).__name__}: {exc})")
    if not isinstance(data, dict):
        block(f"vstup hooku není objekt JSON ({type(data).__name__}), blokuji")
    cwd = Path(str(data.get("cwd") or os.getcwd()))
    ctx = Context(
        cwd=cwd,
        is_implementer=str(data.get("agent_type") or "").rsplit(":", 1)[-1] == IMPLEMENTER,
        merge_script=load_merge_script(),
        current_branch=_git_current_branch,
        pr_files=_gh_pr_files,
    )
    verdict = evaluate(data, ctx)
    if verdict.blocks:
        block("; ".join(dict.fromkeys(verdict.blocks)))
    if verdict.asks:
        decision = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": "guard: " + "; ".join(dict.fromkeys(verdict.asks)),
            }
        }
        _emit(sys.stdout, json.dumps(decision, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # každé selhání blokuje, nikdy tiše nepouští
        block(f"neočekávaná chyba, blokuji ({type(exc).__name__}: {exc})")
