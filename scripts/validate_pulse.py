#!/usr/bin/env python3
"""Validate one or more daily pulse Markdown files without publishing them."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from research_pipeline.config import load_pipeline_config, load_pulse_constraints
from research_pipeline.errors import PipelineError
from research_pipeline.pulse_validation import validate_pulse_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--schema", type=Path)
    args = parser.parse_args(argv)
    project_root = args.project_root.resolve()
    paths = args.paths or sorted((project_root / "content" / "pulses").glob("*.md"))
    if not paths:
        print("pulse validation failed: no pulse files found", file=sys.stderr)
        return 2
    try:
        config = load_pipeline_config(project_root / "config" / "sources.yaml")
        pulse_constraints = load_pulse_constraints(project_root / "config" / "pulse.yaml")
        source_ids = {source.id for source in config.sources}
        results = [
            validate_pulse_file(
                path.resolve(),
                project_root,
                schema_path=args.schema or project_root / "schemas" / "pulse.schema.json",
                source_ids=source_ids,
                **pulse_constraints,
            )
            for path in paths
        ]
    except PipelineError as exc:
        print(f"pulse validation failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": "valid",
                "pulses": [
                    {
                        "path": str(result.path),
                        "word_count": result.word_count,
                        "signal_count": result.signal_count,
                        "artifact_manifests": list(result.artifact_manifests),
                    }
                    for result in results
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
