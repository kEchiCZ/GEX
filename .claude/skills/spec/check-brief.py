"""Kontrola platnosti listů podle `brief.md`.

    python -I .claude/skills/spec/check-brief.py --mode spec <soubor>
    python -I .claude/skills/spec/check-brief.py --mode implement <N>

`--mode spec`: návrh listů v souboru (každý list začíná nadpisem `### L<n> — název`) před
založením issues. Kontroluje povinné řádky šablony, aspoň jedno kritérium přijetí, limity
(≤ 8 zdrojových souborů v Změnit a Nové, odhad ≤ 600 řádků), cesty Číst a Změnit na
`origin/main`, Nové na `origin/main` neexistuje, commit z řádku Kontext existuje a blokátory
`L<n>` téže dávky bez cyklu.

`--mode implement`: list jako issue #N před implementací. Navíc: tělo začíná `[AI Agent]`,
label `ready`, žádné `L<n>` v `Začíná po:`, blokátory `#N` zavřené mergnutým PR a schválení,
tedy komentář vlastníka repa bez `[AI Agent]` pod listem nebo jeho rodičem novější než poslední
úprava těla. Výjimka: samostatný řádek `Grilling: přeskočit` v textu vlastníka bez značky
(v komentáři novější než úprava těla); pak se kontroluje jen to a zavřené blokátory z řádku
`Začíná po:` v tomtéž textu.

Exit 0 = vše platí, 1 = list neprošel (důvody na stdout), 2 = chyba okolí (git, gh).
Volající nejdřív `git fetch origin`. Jen standardní knihovna.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MARKER = "[AI Agent]"
SKIP_LINE = "Grilling: přeskočit"
MAX_SOURCE_FILES = 8
MAX_LINES = 600
HEADING = re.compile(r"^#{2,4}\s*(L\d+)\b[^\n]*$", re.MULTILINE)
REQUIRED = (
    "současné",
    "kritéria",
    "kontext",
    "šev",
    "ověření",
    "odhad",
    "mimo rozsah",
    "začíná po",
)
FIELD = re.compile(
    r"^\s*(?:-\s+)?(Současné\s*→\s*požadované|Kritéria přijetí|Kontext\s*\(platí k\s+(?P<sha>"
    r"[0-9a-f]{7,40})\)|Rozhodnutí|Rozhraní|Číst|Změnit|Nové|Šev|Ověření|Odhad|Mimo rozsah|"
    r"Začíná po)\s*:\s*(?P<value>.*)$",
    re.IGNORECASE,
)
CRITERION = re.compile(r"^\s*-\s+\[[ xX]\]\s+\S")


@dataclass
class Brief:
    name: str
    fields: dict[str, str] = field(default_factory=dict)
    criteria: list[str] = field(default_factory=list)
    sha: str | None = None


def _key(label: str) -> str:
    label = label.lower()
    for key in (
        "současné",
        "kritéria",
        "kontext",
        "rozhodnutí",
        "rozhraní",
        "číst",
        "změnit",
        "nové",
        "šev",
        "ověření",
        "odhad",
        "mimo rozsah",
        "začíná po",
    ):
        if label.startswith(key):
            return key
    return label


def parse_brief(name: str, text: str) -> Brief:
    brief = Brief(name)
    current: str | None = None
    for line in text.splitlines():
        match = FIELD.match(line)
        if match:
            current = _key(match.group(1))
            brief.fields[current] = match.group("value").strip()
            if match.group("sha"):
                brief.sha = match.group("sha")
        elif current == "kritéria" and CRITERION.match(line):
            brief.criteria.append(line.strip())
        elif current and line.startswith("  ") and line.strip():
            brief.fields[current] += " " + line.strip()
        elif line.strip():
            current = None
    return brief


def split_batch(text: str) -> list[Brief]:
    heads = list(HEADING.finditer(text))
    return [
        parse_brief(
            m.group(1), text[m.end() : heads[i + 1].start() if i + 1 < len(heads) else None]
        )
        for i, m in enumerate(heads)
    ]


def paths(value: str) -> list[str]:
    found: list[str] = []
    for part in value.split(","):
        words = part.strip().split()
        if not words:
            continue
        word = words[0].strip("`'\"")
        if "/" in word or re.search(r"\.\w+$", word):
            found.append(word.rstrip("/"))
    return found


def is_source(path: str) -> bool:
    name = Path(path).name
    if "/tests/" in f"/{path}" or name.startswith("test_") or re.search(r"\.test\.[jt]sx?$", name):
        return False
    return not (path.startswith("docs/") or path.endswith(".md"))


def blockers(value: str) -> tuple[list[int], list[str]]:
    return [int(n) for n in re.findall(r"#(\d+)", value)], re.findall(r"\b(L\d+)\b", value)


def check_brief(
    brief: Brief, exists: Callable[[str], bool], commit_exists: Callable[[str], bool]
) -> list[str]:
    errors = [f"chybí řádek „{k}“" for k in REQUIRED if k not in brief.fields]
    if not brief.criteria:
        errors.append("žádné kritérium přijetí (řádek `- [ ] …`)")
    if brief.sha is None and "kontext" in brief.fields:
        errors.append("Kontext neuvádí commit (`Kontext (platí k <sha>):`)")
    elif brief.sha is not None and not commit_exists(brief.sha):
        errors.append(f"commit {brief.sha} z řádku Kontext neexistuje")
    change = paths(brief.fields.get("změnit", ""))
    new = paths(brief.fields.get("nové", ""))
    if not change and not new:
        errors.append("Změnit ani Nové neuvádí žádnou cestu")
    for path in paths(brief.fields.get("číst", "")) + change:
        if not exists(path):
            errors.append(f"cesta {path} na origin/main neexistuje")
    for path in new:
        if exists(path):
            errors.append(f"nový soubor {path} na origin/main už existuje")
    sources = [p for p in change + new if is_source(p)]
    if len(sources) > MAX_SOURCE_FILES:
        errors.append(f"zdrojových souborů {len(sources)} > {MAX_SOURCE_FILES}: rozděl list")
    estimate = re.search(r"(\d+)", brief.fields.get("odhad", ""))
    if "odhad" in brief.fields and estimate is None:
        errors.append("Odhad neuvádí počet řádků")
    elif estimate is not None and int(estimate.group(1)) > MAX_LINES:
        errors.append(f"odhad {estimate.group(1)} ř. > {MAX_LINES}: rozděl list")
    return errors


def check_batch(
    briefs: list[Brief], exists: Callable[[str], bool], commit_exists: Callable[[str], bool]
) -> dict[str, list[str]]:
    names = [b.name for b in briefs]
    report = {b.name: check_brief(b, exists, commit_exists) for b in briefs}
    graph: dict[str, list[str]] = {}
    for brief in briefs:
        _, local = blockers(brief.fields.get("začíná po", ""))
        graph[brief.name] = local
        for dep in local:
            if dep == brief.name:
                report[brief.name].append("list blokuje sám sebe")
            elif dep not in names:
                report[brief.name].append(f"blokátor {dep} není v této dávce")
    for start in names:
        stack, seen = [start], set()
        while stack:
            node = stack.pop()
            for dep in graph.get(node, []):
                if dep == start:
                    report[start].append(f"cyklus blokátorů přes {node} → {start}")
                    stack.clear()
                    break
                if dep not in seen:
                    seen.add(dep)
                    stack.append(dep)
    if len(set(names)) != len(names):
        report.setdefault("dávka", []).append("dva listy se stejným označením L<n>")
    return report


# --- režim implement ------------------------------------------------------------------------

QUERY = """
query($owner: String!, $repo: String!, $n: Int!) {
  repository(owner: $owner, name: $repo) {
    owner { login }
    issue(number: $n) {
      number title body state createdAt lastEditedAt
      author { login }
      labels(first: 50) { nodes { name } }
      comments(last: 100) { nodes { author { login } createdAt body } }
      parent {
        number
        comments(last: 100) { nodes { author { login } createdAt body } }
      }
    }
  }
}
"""
BLOCKER_QUERY = """
query($owner: String!, $repo: String!, $n: Int!) {
  repository(owner: $owner, name: $repo) {
    issue(number: $n) {
      state
      closedByPullRequestsReferences(first: 10, includeClosedPrs: true) { nodes { merged } }
    }
  }
}
"""


def _comments(node: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not node:
        return []
    return list((node.get("comments") or {}).get("nodes") or [])


def _by_owner(item: dict[str, Any], owner: str) -> bool:
    return ((item.get("author") or {}).get("login") or "") == owner


def _marked(text: str) -> bool:
    return text.lstrip().startswith(MARKER)


def check_implement(
    issue: dict[str, Any],
    owner: str,
    blocker_merged: Callable[[int], bool],
    exists: Callable[[str], bool],
    commit_exists: Callable[[str], bool],
) -> list[str]:
    body = str(issue.get("body") or "")
    edited = str(issue.get("lastEditedAt") or issue.get("createdAt") or "")
    own_comments = [c for c in _comments(issue) if _by_owner(c, owner) and not _marked(c["body"])]

    skip_texts = [] if _marked(body) or not _by_owner(issue, owner) else [body]
    skip_texts += [c["body"] for c in own_comments if c["createdAt"] > edited]
    skip_texts = [t for t in skip_texts if any(ln.strip() == SKIP_LINE for ln in t.splitlines())]
    if skip_texts:
        errors: list[str] = []
        for text in skip_texts:
            match = re.search(r"^\s*Začíná po\s*:(.*)$", text, re.MULTILINE | re.IGNORECASE)
            numbers, _ = blockers(match.group(1) if match else "")
            errors += [
                f"blokátor #{n} není zavřený mergnutým PR" for n in numbers if not blocker_merged(n)
            ]
        return errors

    errors = []
    if not _marked(body):
        errors.append(
            f"tělo nezačíná {MARKER}: issue neprošlo /grillme a /spec (nabídni /grillme #N)"
        )
    labels = {n["name"] for n in (issue.get("labels") or {}).get("nodes") or []}
    if "ready" not in labels:
        errors.append("chybí label ready")
    if "in-progress" in labels:
        errors.append("list má label in-progress: běží nebo je rozdělaný")
    brief = parse_brief(f"#{issue.get('number')}", body)
    errors += check_brief(brief, exists, commit_exists)
    numbers, local = blockers(brief.fields.get("začíná po", ""))
    if local:
        errors.append(f"Začíná po obsahuje {', '.join(local)}: po založení patří čísla #N")
    errors += [f"blokátor #{n} není zavřený mergnutým PR" for n in numbers if not blocker_merged(n)]
    approvals = [
        c
        for c in own_comments + _comments(issue.get("parent"))
        if _by_owner(c, owner) and not _marked(c["body"]) and c["createdAt"] > edited
    ]
    if not approvals:
        errors.append(
            "chybí schválení: komentář vlastníka bez [AI Agent] pod listem nebo rodičem "
            "novější než poslední úprava těla"
        )
    return errors


# --- IO -------------------------------------------------------------------------------------


class EnvError(Exception):
    pass


def run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=False)
    if result.returncode != 0:
        raise EnvError(f"{' '.join(cmd[:3])}…: {result.stderr.strip()}")
    return result.stdout


def on_main(path: str) -> bool:
    probe = subprocess.run(
        ["git", "cat-file", "-e", f"origin/main:{path}"], capture_output=True, check=False
    )
    return probe.returncode == 0


def commit_known(sha: str) -> bool:
    probe = subprocess.run(
        ["git", "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True, check=False
    )
    return probe.returncode == 0


def graphql(query: str, n: int) -> dict[str, Any]:
    owner_repo = run(["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"])
    owner, repo = owner_repo.strip().split("/", 1)
    out = run(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-F",
            f"owner={owner}",
            "-F",
            f"repo={repo}",
            "-F",
            f"n={n}",
        ]
    )
    data = json.loads(out)
    if data.get("errors"):
        raise EnvError(f"GraphQL: {data['errors'][0].get('message')}")
    return dict(data["data"]["repository"])


def blocker_closed_by_merge(n: int) -> bool:
    issue = graphql(BLOCKER_QUERY, n).get("issue") or {}
    prs = (issue.get("closedByPullRequestsReferences") or {}).get("nodes") or []
    return issue.get("state") == "CLOSED" and any(pr.get("merged") for pr in prs)


def _print(text: str) -> None:
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Kontrola listů podle brief.md")
    parser.add_argument("--mode", choices=("spec", "implement"), required=True)
    parser.add_argument("target", help="soubor s návrhem listů, nebo číslo issue")
    args = parser.parse_args(argv)
    try:
        if args.mode == "spec":
            text = Path(args.target).read_text(encoding="utf-8")
            briefs = split_batch(text)
            if not briefs:
                _print("žádný list: každý začíná nadpisem `### L<n> — název`")
                return 1
            report = check_batch(briefs, on_main, commit_known)
        else:
            number = int(args.target.lstrip("#"))
            repository = graphql(QUERY, number)
            issue = repository.get("issue")
            if not issue:
                raise EnvError(f"issue #{number} neexistuje")
            owner = repository["owner"]["login"]
            report = {
                f"#{number}": check_implement(
                    issue, owner, blocker_closed_by_merge, on_main, commit_known
                )
            }
    except (EnvError, OSError, ValueError, KeyError) as exc:
        _print(f"check-brief: CHYBA OKOLÍ — {exc}")
        return 2
    failed = 0
    for name, errors in report.items():
        _print(f"{name}  {'✓' if not errors else '✗'}")
        for error in errors:
            _print(f"    - {error}")
        failed += bool(errors)
    _print(f"{len(report)} listů, {failed} neprošlo")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
