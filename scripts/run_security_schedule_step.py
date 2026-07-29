#!/usr/bin/env python3
"""Run one deterministic host-side step in the secured Residual schedule."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from research_pipeline.errors import PipelineError  # noqa: E402
from research_pipeline.scout_security import (  # noqa: E402
    apply_audit_verdict,
    generate_sol_visual,
    import_sol_package,
    load_approved_bundle,
    stage_audit_input,
    stage_sol_workspace,
)
from research_pipeline.scouting import freeze_inbox  # noqa: E402
from research_pipeline.scheduled import run_scheduled_pipeline  # noqa: E402


AUDITOR_WORKSPACE = Path("/Users/mikhail/.openclaw/workspace-residual-auditor")
SOL_WORKSPACE = Path("/Users/mikhail/.openclaw/workspace-residual-editor")


def _clock(run_date: str | None = None) -> tuple[str, str]:
    now = datetime.now(ZoneInfo("Europe/Moscow"))
    if run_date is None:
        return now.date().isoformat(), now.isoformat(timespec="seconds")
    try:
        parsed = datetime.strptime(run_date, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("run date must use the YYYY-MM-DD calendar format") from exc
    if parsed.isoformat() != run_date:
        raise ValueError("run date must use the YYYY-MM-DD calendar format")
    return run_date, now.isoformat(timespec="seconds")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "step",
        choices=(
            "freeze",
            "stage-audit",
            "apply-audit",
            "stage-sol",
            "generate-visual",
            "publish",
        ),
    )
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument(
        "--run-date",
        help="Europe/Moscow edition date (YYYY-MM-DD); defaults to today",
    )
    parser.add_argument(
        "--attempt",
        type=int,
        default=1,
        help="positive TaskFlow attempt number for Sol staging and publication",
    )
    args = parser.parse_args(argv)
    try:
        if args.attempt < 1:
            raise ValueError("attempt must be a positive integer")
        run_date, timestamp = _clock(args.run_date)
        project_root = args.project_root.resolve(strict=True)
        if args.step == "freeze":
            path = freeze_inbox(
                project_root, edition_date=run_date, frozen_at=timestamp
            )
            result: dict[str, object] = {"status": "frozen", "path": str(path)}
        elif args.step == "stage-audit":
            path = stage_audit_input(
                project_root,
                run_date=run_date,
                staged_at=timestamp,
                auditor_workspace=AUDITOR_WORKSPACE,
            )
            result = {"status": "audit_staged", "path": str(path)}
        elif args.step == "apply-audit":
            path = apply_audit_verdict(
                project_root,
                run_date=run_date,
                approved_at=timestamp,
                auditor_workspace=AUDITOR_WORKSPACE,
            )
            result = {"status": "audit_applied", "path": str(path)}
        elif args.step == "stage-sol":
            bundle = load_approved_bundle(project_root, run_date)
            if bundle["status"] != "ready":
                result = {
                    "status": "no_update",
                    "reason": "Aegis approved no candidate for Sol",
                }
            else:
                path = stage_sol_workspace(
                    project_root,
                    run_date=run_date,
                    sol_workspace=SOL_WORKSPACE,
                    attempt=args.attempt,
                )
                result = {"status": "sol_staged", "path": str(path)}
        elif args.step == "generate-visual":
            request = (
                SOL_WORKSPACE
                / "outbox"
                / run_date
                / f"attempt-{args.attempt}-visual-request.json"
            )
            if not request.is_file():
                result = {
                    "status": "no_update",
                    "reason": "Sol produced no defensible visual request",
                }
            else:
                path = generate_sol_visual(
                    project_root,
                    run_date=run_date,
                    generated_at=timestamp,
                    sol_workspace=SOL_WORKSPACE,
                    attempt=args.attempt,
                )
                result = {"status": "visual_generated", "path": str(path)}
        else:
            bundle = load_approved_bundle(project_root, run_date)
            outbox = (
                SOL_WORKSPACE
                / "outbox"
                / run_date
                / f"attempt-{args.attempt}.json"
            )
            if bundle["status"] == "ready" and outbox.is_file():
                import_sol_package(
                    project_root,
                    run_date=run_date,
                    sol_workspace=SOL_WORKSPACE,
                    attempt=args.attempt,
                )
            publication = run_scheduled_pipeline(project_root, run_date=run_date)
            result = publication.as_dict()
    except (PipelineError, OSError, RuntimeError, ValueError) as exc:
        print(
            json.dumps(
                {"status": "failed", "step": args.step, "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
