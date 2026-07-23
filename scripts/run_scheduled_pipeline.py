#!/usr/bin/env python3
"""Run one guarded daily transaction and deploy only a published pulse."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from research_pipeline.scheduled import run_scheduled_pipeline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--date", required=True, help="Europe/Moscow date (YYYY-MM-DD)")
    args = parser.parse_args(argv)
    result = run_scheduled_pipeline(args.project_root, run_date=args.date)
    print(
        json.dumps(
            result.as_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0 if result.status in {"published", "no_update", "review_required"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
