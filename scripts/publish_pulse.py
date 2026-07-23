#!/usr/bin/env python3
"""Publish a validated release after Python, frontend-test, and build gates."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from research_pipeline.errors import PipelineError, PublicationError
from research_pipeline.release import publish_release


def _safe_project_relative(value: str, label: str) -> str:
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise PublicationError(f"unsafe {label}: {value!r}")
    return pure.as_posix()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--pulse", help="Project-relative Markdown path; omit for processed-no-pulse")
    parser.add_argument("--artifact-manifest", action="append", default=[])
    args = parser.parse_args(argv)
    project_root = args.project_root.resolve()
    try:
        pulse = _safe_project_relative(args.pulse, "pulse path") if args.pulse else None
        # Stable /artifacts/... URLs and project-relative public/artifacts/...
        # paths are both accepted and normalized inside the locked publisher.
        artifacts = list(args.artifact_manifest)
        result = publish_release(
            project_root,
            args.release_id,
            pulse=pulse,
            artifact_manifests=artifacts,
        )
    except (PipelineError, OSError) as exc:
        print(f"publish failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": result.status,
                "release_id": result.release_id,
                "run_id": result.run_id,
                "pointer_changed": result.pointer_changed,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
