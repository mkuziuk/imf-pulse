#!/usr/bin/env python3
"""Run the bounded metadata-only external source monitor."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from research_pipeline.external import ExternalMonitoringError, run_external_search


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--as-of",
        required=True,
        help="Deterministic ISO-8601 discovery cutoff, including timezone for timestamps",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project_root = args.project_root.resolve()
    config = args.config or project_root / "config" / "external-sources.yaml"
    try:
        result = run_external_search(config, project_root, args.as_of)
    except (ExternalMonitoringError, OSError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
