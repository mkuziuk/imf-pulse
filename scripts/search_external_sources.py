#!/usr/bin/env python3
"""Run the bounded metadata-only external source monitor."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from research_pipeline.external import (
    ExternalMetadataRateLimit,
    ExternalMetadataTimeout,
    ExternalMonitoringError,
    run_external_search,
)
from research_pipeline.external_preflight import write_scheduled_search_outcome


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--as-of",
        required=True,
        help="Deterministic ISO-8601 discovery cutoff, including timezone for timestamps",
    )
    parser.add_argument(
        "--scheduled-outcome-date",
        help=(
            "Write the private hash-bound handoff consumed by the scheduled wrapper; "
            "must match the 06:00 Europe/Moscow cutoff"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project_root = args.project_root.resolve()
    config = args.config or project_root / "config" / "external-sources.yaml"
    try:
        result = run_external_search(config, project_root, args.as_of)
    except (ExternalMetadataTimeout, ExternalMetadataRateLimit) as exc:
        if args.scheduled_outcome_date:
            try:
                outcome_path = write_scheduled_search_outcome(
                    project_root,
                    run_date=args.scheduled_outcome_date,
                    as_of=args.as_of,
                    status="deferred",
                    reason=str(exc),
                )
            except Exception as outcome_exc:
                print(
                    json.dumps(
                        {
                            "status": "failed",
                            "error": f"cannot record scheduled metadata timeout: {outcome_exc}",
                        },
                        sort_keys=True,
                    )
                )
                return 2
            print(
                json.dumps(
                    {
                        "status": "no_update",
                        "reason": str(exc),
                        "scheduled_outcome_path": outcome_path,
                    },
                    sort_keys=True,
                )
            )
            return 0
        print(json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True))
        return 2
    except (ExternalMonitoringError, OSError) as exc:
        outcome_path = None
        if args.scheduled_outcome_date:
            try:
                outcome_path = write_scheduled_search_outcome(
                    project_root,
                    run_date=args.scheduled_outcome_date,
                    as_of=args.as_of,
                    status="failed",
                    reason=str(exc),
                )
            except Exception as outcome_exc:
                print(
                    json.dumps(
                        {
                            "status": "failed",
                            "error": f"{exc}; cannot record scheduled outcome: {outcome_exc}",
                        },
                        sort_keys=True,
                    )
                )
                return 2
        payload = {"status": "failed", "error": str(exc)}
        if outcome_path is not None:
            payload["scheduled_outcome_path"] = outcome_path
        print(json.dumps(payload, sort_keys=True))
        return 2
    if args.scheduled_outcome_date:
        try:
            outcome_path = write_scheduled_search_outcome(
                project_root,
                run_date=args.scheduled_outcome_date,
                as_of=args.as_of,
                status="ready",
                reason="metadata search completed and bound an immutable candidate batch",
                search_result=result,
            )
        except Exception as exc:
            print(
                json.dumps(
                    {"status": "failed", "error": str(exc)},
                    sort_keys=True,
                )
            )
            return 2
        result = {**result, "scheduled_outcome_path": outcome_path}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
