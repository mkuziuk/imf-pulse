#!/usr/bin/env python3
"""Copy the explicit local allowlist into a private immutable snapshot."""

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--source-root", type=Path, help="Explicit live IMF root override")
    parser.add_argument("--root-id", default="imf")
    parser.add_argument(
        "--update-snapshot-pointer",
        action="store_true",
        help="Explicitly promote this input snapshot for later pointer-based ingestion",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project_root = args.project_root.resolve()
    config_path = args.config or project_root / "config" / "sources.yaml"
    try:
        config = load_pipeline_config(config_path)
        manifest, directory, created = build_snapshot(
            config,
            project_root,
            root_id=args.root_id,
            source_root_override=args.source_root,
            update_pointer=args.update_snapshot_pointer,
        )
    except PipelineError as exc:
        print(f"sync failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": "created" if created else "unchanged",
                "snapshot_id": manifest.snapshot_id,
                "snapshot_directory": str(directory),
                "source_count": len(manifest.entries),
                "missing_optional_sources": list(manifest.missing_optional_sources),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
