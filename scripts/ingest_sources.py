#!/usr/bin/env python3
"""Statically extract the current private snapshot into a release candidate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from research_pipeline.config import load_pipeline_config
from research_pipeline.errors import PipelineError
from research_pipeline.release import build_release_candidate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--snapshot-directory", type=Path)
    parser.add_argument("--knowledge-directory", type=Path)
    parser.add_argument("--schemas-directory", type=Path)
    parser.add_argument("--root-id", default="imf")
    args = parser.parse_args(argv)
    project_root = args.project_root.resolve()
    try:
        config = load_pipeline_config(args.config or project_root / "config" / "sources.yaml")
        result = build_release_candidate(
            project_root,
            config,
            root_id=args.root_id,
            snapshot_directory=args.snapshot_directory,
            knowledge_directory=args.knowledge_directory,
            schemas_directory=args.schemas_directory,
        )
    except (PipelineError, OSError) as exc:
        print(f"ingestion failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": result.status,
                "release_id": result.release_id,
                "release_directory": str(result.release_directory),
                "created": result.created,
                "semantic_changed": result.semantic_changed,
                "release_pointer_changed": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
