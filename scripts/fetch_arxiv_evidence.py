#!/usr/bin/env python3
"""Fetch one exact-batch arXiv PDF into the private automatic-evidence store."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from research_pipeline.evidence_fetch import fetch_exact_arxiv_pdf
from research_pipeline.external import validate_batch_integrity
from research_pipeline.validation import strict_json_loads


def _safe_batch(project_root: Path, value: Path) -> Path:
    path = value.resolve(strict=True)
    root = (project_root / "data" / "external" / "batches").resolve(strict=True)
    if path.parent != root or path.is_symlink() or not path.is_file():
        raise RuntimeError("batch must be a regular file in data/external/batches")
    return path


def _read_batch(path: Path) -> dict:
    if path.stat().st_size > 16 * 1024 * 1024:
        raise RuntimeError("external batch is oversized")
    value = strict_json_loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("external batch is not an object")
    validate_batch_integrity(value)
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--candidate-sha256", required=True)
    args = parser.parse_args(argv)
    project_root = args.project_root.resolve(strict=True)
    batch = _read_batch(_safe_batch(project_root, args.batch))
    result = fetch_exact_arxiv_pdf(
        project_root,
        batch,
        args.candidate_id,
        args.candidate_sha256,
    )
    print(
        json.dumps(
            result,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
