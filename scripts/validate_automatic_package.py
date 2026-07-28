#!/usr/bin/env python3
"""Validate today's automatic package without materializing or publishing it."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from research_pipeline.automatic import validate_automatic_package  # noqa: E402
from research_pipeline.errors import PipelineError  # noqa: E402
from research_pipeline.external_preflight import (  # noqa: E402
    ExternalPreflightError,
    load_ready_scheduled_search_batch,
    scheduled_outcome_path,
)
from research_pipeline.release import _read_current_pointer  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--date", required=True, help="Europe/Moscow date (YYYY-MM-DD)")
    args = parser.parse_args(argv)
    try:
        project_root = args.project_root.resolve(strict=True)
        outcome, batch = load_ready_scheduled_search_batch(
            project_root,
            scheduled_outcome_path(args.date),
            run_date=args.date,
        )
        validation = validate_automatic_package(
            project_root,
            args.date,
            batch_id=str(outcome["batch_id"]),
            candidates=batch["candidates"],
            checkpoint=_read_current_pointer(project_root),
        )
        if validation is None:
            payload = {
                "status": "no_package",
                "date": args.date,
                "reason": "no unconsumed automatic package exists for this date",
            }
        else:
            payload = {
                "status": "valid",
                "date": args.date,
                "candidate_ids": [
                    binding["candidate_id"]
                    for binding in validation.package["candidates"]
                ],
                "source_ids": [
                    source["id"] for source in validation.sources
                ],
                "knowledge_ids": list(validation.pulse_ids),
                "artifact_ids": [
                    artifact.artifact_id
                    for artifact in validation.artifact_payloads
                ],
            }
    except (ExternalPreflightError, PipelineError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {"status": "failed", "date": args.date, "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
