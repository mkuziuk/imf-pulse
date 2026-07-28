#!/usr/bin/env python3
"""Run one guarded daily transaction and deploy only a published pulse."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from research_pipeline.scheduled import (
    prepare_scheduled_pipeline,
    run_scheduled_pipeline,
    select_scheduled_candidate,
)
from research_pipeline.workflow import WorkflowStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--date", required=True, help="Europe/Moscow date (YYYY-MM-DD)")
    parser.add_argument(
        "--action",
        choices=("resume", "prepare", "select", "status"),
        default="resume",
        help="advance the whole workflow or one explicit operator phase",
    )
    parser.add_argument("--candidate-id")
    parser.add_argument("--candidate-sha256")
    args = parser.parse_args(argv)
    if args.action == "status":
        payload = {
            "status": "ok",
            "workflow": WorkflowStore(args.project_root, args.date).as_dict(),
        }
        print(
            json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        )
        return 0
    if args.action == "prepare":
        payload = prepare_scheduled_pipeline(args.project_root, run_date=args.date)
        print(
            json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        )
        return (
            0
            if payload["status"]
            in {"awaiting_editorial", "no_candidates", "deferred"}
            else 2
        )
    if args.action == "select":
        if not args.candidate_id or not args.candidate_sha256:
            parser.error(
                "--action select requires --candidate-id and --candidate-sha256"
            )
        payload = select_scheduled_candidate(
            args.project_root,
            run_date=args.date,
            candidate_id=args.candidate_id,
            candidate_sha256=args.candidate_sha256,
        )
        print(
            json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        )
        return 0 if payload["status"] in {"awaiting_editorial", "deferred"} else 2
    result = run_scheduled_pipeline(args.project_root, run_date=args.date)
    print(
        json.dumps(
            result.as_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0 if result.status in {"published", "no_update", "review_required", "deferred"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
