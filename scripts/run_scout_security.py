#!/usr/bin/env python3
"""Stage, apply, and transfer the Luna → Aegis → Sol security handoff."""

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
    stage_audit_input,
    stage_sol_workspace,
)


def _now() -> str:
    return datetime.now(ZoneInfo("Europe/Moscow")).isoformat(timespec="seconds")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "stage-audit",
            "apply-audit",
            "stage-sol",
            "generate-visual",
            "import-sol",
        ),
    )
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--date", required=True)
    parser.add_argument("--auditor-workspace", type=Path)
    parser.add_argument("--sol-workspace", type=Path)
    parser.add_argument("--timestamp", default=None)
    parser.add_argument("--attempt", type=int, default=1)
    args = parser.parse_args(argv)
    try:
        project_root = args.project_root.resolve(strict=True)
        timestamp = args.timestamp or _now()
        if args.command == "stage-audit":
            if args.auditor_workspace is None:
                parser.error("stage-audit requires --auditor-workspace")
            path = stage_audit_input(
                project_root,
                run_date=args.date,
                staged_at=timestamp,
                auditor_workspace=args.auditor_workspace.resolve(),
            )
        elif args.command == "apply-audit":
            if args.auditor_workspace is None:
                parser.error("apply-audit requires --auditor-workspace")
            path = apply_audit_verdict(
                project_root,
                run_date=args.date,
                approved_at=timestamp,
                auditor_workspace=args.auditor_workspace.resolve(),
            )
        elif args.command == "stage-sol":
            if args.sol_workspace is None:
                parser.error("stage-sol requires --sol-workspace")
            path = stage_sol_workspace(
                project_root,
                run_date=args.date,
                sol_workspace=args.sol_workspace.resolve(),
                attempt=args.attempt,
            )
        elif args.command == "generate-visual":
            if args.sol_workspace is None:
                parser.error("generate-visual requires --sol-workspace")
            path = generate_sol_visual(
                project_root,
                run_date=args.date,
                generated_at=timestamp,
                sol_workspace=args.sol_workspace.resolve(),
                attempt=args.attempt,
            )
        else:
            if args.sol_workspace is None:
                parser.error("import-sol requires --sol-workspace")
            path = import_sol_package(
                project_root,
                run_date=args.date,
                sol_workspace=args.sol_workspace.resolve(),
                attempt=args.attempt,
            )
    except (PipelineError, OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps({"status": "ok", "path": str(path)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
