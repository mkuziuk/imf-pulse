#!/usr/bin/env python3
"""Run one independent, transactional daily Residual pipeline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from research_pipeline.daily import run_daily_pipeline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--mode", choices=("live",), required=True)
    parser.add_argument("--date", required=True, help="Europe/Moscow calendar date (YYYY-MM-DD)")
    parser.add_argument(
        "--external-search-outcome",
        help="Private project-relative scheduled metadata-search handoff",
    )
    args = parser.parse_args(argv)
    result = run_daily_pipeline(
        args.project_root,
        mode=args.mode,
        run_date=args.date,
        external_search_outcome=args.external_search_outcome,
    )
    print(
        json.dumps(
            result.as_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 2 if result.status in {"blocked", "failed"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
