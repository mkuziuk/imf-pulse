#!/usr/bin/env python3
"""Append an approve/reject decision for one exact external candidate version."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from research_pipeline.external import (
    ExternalMonitoringError,
    load_external_config,
    record_review_decision,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--candidate-sha256", required=True)
    parser.add_argument("--decision", choices=("approved", "rejected"), required=True)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--decided-at", required=True, help="ISO-8601 decision timestamp")
    parser.add_argument("--license", required=True)
    parser.add_argument(
        "--reuse-status",
        choices=(
            "internal_only",
            "unknown",
            "cleared",
            "restricted",
            "public_domain",
            "not_applicable",
        ),
        required=True,
    )
    parser.add_argument("--public-distribution", action="store_true")
    parser.add_argument("--rights-notes")
    parser.add_argument("--ledger", help="Safe project-relative append-only ledger override")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project_root = args.project_root.resolve()
    config_path = args.config or project_root / "config" / "external-sources.yaml"
    try:
        config = load_external_config(config_path)
        rights = {
            "license": args.license,
            "reuse_status": args.reuse_status,
            "public_distribution": args.public_distribution,
        }
        if args.rights_notes:
            rights["notes"] = args.rights_notes
        result = record_review_decision(
            project_root=project_root,
            batch_path=args.batch,
            candidate_id=args.candidate_id,
            expected_candidate_sha256=args.candidate_sha256,
            decision=args.decision,
            reviewer=args.reviewer,
            reason=args.reason,
            decided_at=args.decided_at,
            rights=rights,
            ledger_relative=args.ledger or config["policy"]["decision_ledger"],
            batch_root_relative=config["policy"]["candidate_batch_root"],
        )
    except (ExternalMonitoringError, OSError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
