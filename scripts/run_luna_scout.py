#!/usr/bin/env python3
"""Prepare, validate, and freeze private Luna scouting records."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from research_pipeline.errors import PipelineError  # noqa: E402
from research_pipeline.scouting import (  # noqa: E402
    freeze_inbox,
    ingest_submission,
    prepare_submission_draft,
    query_ids_for_slot,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    subparsers = parser.add_subparsers(dest="action", required=True)

    queries = subparsers.add_parser("queries")
    queries.add_argument("--slot", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--edition-date", required=True)
    prepare.add_argument("--slot", required=True)
    prepare.add_argument("--batch", type=Path, required=True)
    prepare.add_argument("--reviewed-at", required=True)

    ingest = subparsers.add_parser("ingest")
    ingest.add_argument("--submission", type=Path, required=True)
    ingest.add_argument("--batch", type=Path, required=True)

    freeze = subparsers.add_parser("freeze")
    freeze.add_argument("--edition-date", required=True)
    freeze.add_argument("--frozen-at", required=True)

    args = parser.parse_args(argv)
    try:
        project_root = args.project_root.resolve(strict=True)
        if args.action == "queries":
            payload = {
                "status": "ready",
                "slot": args.slot,
                "query_ids": list(query_ids_for_slot(project_root, args.slot)),
            }
        elif args.action == "prepare":
            path = prepare_submission_draft(
                project_root,
                edition_date=args.edition_date,
                slot=args.slot,
                batch_path=args.batch.resolve(strict=True),
                reviewed_at=args.reviewed_at,
            )
            payload = {
                "status": "draft_ready",
                "path": path.relative_to(project_root).as_posix(),
            }
        elif args.action == "ingest":
            path = ingest_submission(
                project_root,
                args.submission.resolve(strict=True),
                args.batch.resolve(strict=True),
            )
            payload = {
                "status": "accepted",
                "path": path.relative_to(project_root).as_posix(),
            }
        else:
            path = freeze_inbox(
                project_root,
                edition_date=args.edition_date,
                frozen_at=args.frozen_at,
            )
            payload = {
                "status": "frozen",
                "path": path.relative_to(project_root).as_posix(),
            }
    except (PipelineError, OSError, ValueError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
