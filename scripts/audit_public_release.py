#!/usr/bin/env python3
"""Audit an IMF Pulse public-release directory without modifying it."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from export_public_release import PublicReleaseError, _strict_project_child, audit_public_release


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--directory",
        default=None,
        help=(
            "direct project-child directory to audit; defaults to "
            "IMF_PULSE_PUBLIC_RELEASE_DIR or public-release"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    project_root = Path(__file__).resolve().parents[1]
    configured = arguments.directory or os.environ.get(
        "IMF_PULSE_PUBLIC_RELEASE_DIR", "public-release"
    )
    try:
        directory = _strict_project_child(project_root, configured, must_exist=True)
        summary = audit_public_release(directory)
    except (PublicReleaseError, OSError, ValueError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": "valid",
                "directory": directory.relative_to(project_root).as_posix(),
                **summary,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
