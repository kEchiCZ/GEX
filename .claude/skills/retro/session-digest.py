"""Výtah z přepisu session Claude Code. Zatím jen počet tokenů pro komentář Stav.

    python -I .claude/skills/retro/session-digest.py --tokens <přepis.jsonl | id session>

Sečte `usage` odpovědí modelu v hlavním přepisu i v přepisech subagentů (`<id>/subagents/`).
Jednu odpověď zapisuje přepis po blocích se stejným `message.id`; počítá se jednou, poslední
záznam. Vypíše řádek na přepis a součet: vstup bez cache, zápis do cache, čtení z cache, výstup
a nejvyšší kontext jedné odpovědi (vstup + cache). Id session se hledá v `~/.claude/projects/`.

Jen standardní knihovna. Nečitelný řádek přepisu je chyba s číslem řádku, ne tichý přeskok.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECTS = Path.home() / ".claude" / "projects"


@dataclass
class Usage:
    responses: int = 0
    input: int = 0
    cache_write: int = 0
    cache_read: int = 0
    output: int = 0
    peak_context: int = 0

    def add(self, other: Usage) -> None:
        self.responses += other.responses
        self.input += other.input
        self.cache_write += other.cache_write
        self.cache_read += other.cache_read
        self.output += other.output
        self.peak_context = max(self.peak_context, other.peak_context)

    def row(self, label: str) -> str:
        return (
            f"{label:<44} odpovědi {self.responses:>4}  vstup {self.input:>9,}  "
            f"zápis cache {self.cache_write:>11,}  čtení cache {self.cache_read:>12,}  "
            f"výstup {self.output:>9,}  max kontext {self.peak_context:>9,}"
        )


def _int(usage: dict[str, Any], key: str) -> int:
    value = usage.get(key) or 0
    if not isinstance(value, int):
        raise ValueError(f"usage.{key} není celé číslo: {value!r}")
    return value


def transcript_usage(path: Path) -> Usage:
    last: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError as exc:
                raise ValueError(f"{path}:{number}: řádek není JSON ({exc})") from exc
            if not isinstance(entry, dict) or entry.get("type") != "assistant":
                continue
            message = entry.get("message") or {}
            usage = message.get("usage")
            if isinstance(usage, dict):
                key = str(message.get("id") or entry.get("uuid") or number)
                last[key] = usage
    total = Usage()
    for usage in last.values():
        context = (
            _int(usage, "input_tokens")
            + _int(usage, "cache_creation_input_tokens")
            + _int(usage, "cache_read_input_tokens")
        )
        total.add(
            Usage(
                responses=1,
                input=_int(usage, "input_tokens"),
                cache_write=_int(usage, "cache_creation_input_tokens"),
                cache_read=_int(usage, "cache_read_input_tokens"),
                output=_int(usage, "output_tokens"),
                peak_context=context,
            )
        )
    return total


def resolve(target: str) -> Path:
    path = Path(target)
    if path.suffix == ".jsonl":
        if not path.is_file():
            raise FileNotFoundError(f"přepis {path} neexistuje")
        return path
    matches = sorted(PROJECTS.glob(f"*/{target}.jsonl"))
    if len(matches) != 1:
        raise FileNotFoundError(f"session {target}: v {PROJECTS} nalezeno {len(matches)} přepisů")
    return matches[0]


def digest_tokens(main_path: Path) -> list[tuple[str, Usage]]:
    rows = [("hlavní session", transcript_usage(main_path))]
    subagents = main_path.with_suffix("") / "subagents"
    for path in sorted(subagents.glob("*.jsonl")) if subagents.is_dir() else []:
        rows.append((f"subagent {path.stem}", transcript_usage(path)))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Výtah z přepisu session")
    parser.add_argument(
        "--tokens", required=True, metavar="SESSION", help="cesta k přepisu .jsonl nebo id session"
    )
    args = parser.parse_args(argv)
    try:
        rows = digest_tokens(resolve(args.tokens))
    except (OSError, ValueError) as exc:
        sys.stderr.buffer.write(f"session-digest: {exc}\n".encode())
        return 2
    total = Usage()
    lines = []
    for label, usage in rows:
        total.add(usage)
        lines.append(usage.row(label))
    lines.append(total.row("celkem"))
    sys.stdout.buffer.write(("\n".join(lines) + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
