#!/usr/bin/env python3
"""Fail-closed classifier for the GitHub Pages content-only fast path."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import PurePosixPath
from typing import Sequence


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DATE = r"\d{4}-\d{2}-\d{2}(?:-[1-9][0-9]{0,3})?"
SAFE_TAIL = r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*"
CONTENT_PATHS = (
    re.compile(rf"^content/pulses/{DATE}\.md$"),
    re.compile(rf"^public/artifacts/{DATE}/{SAFE_TAIL}$"),
    re.compile(
        r"^knowledge/curated/"
        r"(?:claims|experiments|methods|relationships|sources)\.jsonl$"
    ),
    re.compile(r"^public-release/(?:current|manifest)\.json$"),
    re.compile(
        r"^public-release/knowledge/"
        r"(?:claims|experiments|methods|relationships|sources)\.jsonl$"
    ),
    re.compile(rf"^public-release/pulses/{DATE}\.md$"),
    re.compile(rf"^public-release/artifacts/{DATE}/{SAFE_TAIL}$"),
)


def _safe_content_path(path: str) -> bool:
    pure = PurePosixPath(path)
    return (
        not pure.is_absolute()
        and not any(part in {"", ".", ".."} for part in pure.parts)
        and any(pattern.fullmatch(path) for pattern in CONTENT_PATHS)
    )


def classify_entries(entries: Sequence[tuple[str, str]]) -> str:
    if not entries:
        return "full"
    for status, path in entries:
        if status not in {"A", "M"} or not _safe_content_path(path):
            return "full"
    return "content"


def classify_push(event: str, before: str, after: str) -> str:
    if (
        event != "push"
        or not SHA_RE.fullmatch(before)
        or not SHA_RE.fullmatch(after)
        or before == "0" * 40
    ):
        return "full"
    try:
        completed = subprocess.run(
            ("git", "diff", "--name-status", "--no-renames", before, after, "--"),
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        entries: list[tuple[str, str]] = []
        for line in completed.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) != 2:
                return "full"
            entries.append((fields[0], fields[1]))
        return classify_entries(entries)
    except (OSError, subprocess.SubprocessError):
        return "full"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", required=True)
    parser.add_argument("--before", required=True)
    parser.add_argument("--after", required=True)
    args = parser.parse_args(argv)
    print(classify_push(args.event, args.before, args.after))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
