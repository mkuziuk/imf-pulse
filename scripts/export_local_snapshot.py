#!/usr/bin/env python3
"""Explicitly export a live IMF snapshot for a sandboxed scheduled run."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from research_pipeline.config import load_pipeline_config
from research_pipeline.errors import PipelineError
from research_pipeline.snapshot import build_snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--source-root",
        type=Path,
        required=True,
        help="Readable live IMF repository. This command never modifies it.",
    )
    parser.add_argument("--root-id", default="imf")
    args = parser.parse_args(argv)
    project_root = args.project_root.resolve()
    config_path = args.config or project_root / "config" / "sources.yaml"
    try:
        config = load_pipeline_config(config_path)
        manifest, directory, created = build_snapshot(
            config,
            project_root,
            root_id=args.root_id,
            source_root_override=args.source_root,
            update_pointer=True,
        )
    except PipelineError as exc:
        print(f"snapshot export failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": "created" if created else "unchanged",
                "mode": "explicit_export",
                "snapshot_id": manifest.snapshot_id,
                "snapshot_directory": str(directory),
                "exported_at": manifest.created_at,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
