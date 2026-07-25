from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from research_pipeline.editorial_context import (
    EditorialContextError,
    build_editorial_context,
)
from research_pipeline.hashing import canonical_json_bytes, canonical_json_hash


RELEASE_ID = "release-11111111111111111111"
PULSE_PATH = "content/pulses/2026-07-20.md"
BOUND_PULSE = f"data/releases/{RELEASE_ID}/publication/{PULSE_PATH}"
SOURCE_ID = "src-external-arxiv-2607-12345v1"
CLAIM_ID = "claim-directional-robust-filtering"


def _pulse_bytes() -> bytes:
    return f"""---
schema_version: "1.0.0"
id: pulse-2026-07-20
date: 2026-07-20
title: "Robust filtering in two directions"
lead: "A directional estimator separates spatial and temporal filtering under contamination."
status: published
topics:
  - iterative-filtering
  - robust-estimation
source_ids:
  - {SOURCE_ID}
knowledge_ids:
  - {CLAIM_ID}
---
## Signal 01 — Direction changes the estimator

The method alternates spatial and temporal robust filtering.

## Why this matters

The order creates a testable robustness distinction.

## Unresolved question

Does temporal-first filtering contain impulsive contamination better than spatial-first filtering?

## Sources

- Primary paper.
""".encode()


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    pulse = _pulse_bytes()
    pulse_file = root / BOUND_PULSE
    pulse_file.parent.mkdir(parents=True)
    pulse_file.write_bytes(pulse)
    release_root = root / "data" / "releases" / RELEASE_ID
    source = {
        "id": SOURCE_ID,
        "content_sha256": "a" * 64,
        "title": "Directional robust iterative filtering",
        "topics": ["iterative-filtering", "robust-estimation"],
        "url": "https://arxiv.org/abs/2607.12345v1",
    }
    claim = {
        "id": CLAIM_ID,
        "normalized_text": "The estimator alternates spatial and temporal robust filtering.",
        "evidence_status": "observed",
        "statement_kind": "method_definition",
    }
    (release_root / "sources.jsonl").write_bytes(canonical_json_bytes(source) + b"\n")
    (release_root / "claims.jsonl").write_bytes(canonical_json_bytes(claim) + b"\n")
    for filename in ("methods.jsonl", "experiments.jsonl", "relationships.jsonl"):
        (release_root / filename).write_text("", encoding="utf-8")
    accepted = [
        {
            "release_id": RELEASE_ID,
            "pulse": PULSE_PATH,
            "bound_pulse": BOUND_PULSE,
            "pulse_sha256": hashlib.sha256(pulse).hexdigest(),
            "binding_sha256": "b" * 64,
            "artifact_manifests": [],
        }
    ]
    pointer = {
        "accepted_publications": accepted,
        "accepted_publications_sha256": canonical_json_hash(accepted),
    }
    (root / "data" / "current.json").write_bytes(canonical_json_bytes(pointer) + b"\n")
    return root


def test_context_indexes_sealed_history(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    result = build_editorial_context(root)

    assert result["accepted_pulse_count"] == 1
    assert result["coverage"]["source_ids"] == [SOURCE_ID]
    assert result["coverage"]["knowledge_ids"] == [CLAIM_ID]
    assert result["coverage"]["topics"] == [
        "iterative-filtering",
        "robust-estimation",
    ]
    assert result["coverage"]["claims"][0]["text"].startswith("The estimator")
    assert result["coverage"]["unresolved_questions"][0]["text"].startswith(
        "Does temporal-first"
    )
    assert result["pulses"][0]["bound_pulse"] == BOUND_PULSE
    assert result["pulses"][0]["signal_headings"] == [
        "Signal 01 — Direction changes the estimator"
    ]


def test_context_rejects_a_changed_sealed_pulse(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / BOUND_PULSE).write_bytes(_pulse_bytes() + b"changed\n")

    with pytest.raises(EditorialContextError, match="pulse hash does not match"):
        build_editorial_context(root)


def test_context_rejects_an_inconsistent_bound_path(tmp_path: Path) -> None:
    root = _project(tmp_path)
    pointer_path = root / "data" / "current.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    pointer["accepted_publications"][0]["bound_pulse"] = (
        "data/releases/release-22222222222222222222/publication/" + PULSE_PATH
    )
    pointer["accepted_publications_sha256"] = canonical_json_hash(
        pointer["accepted_publications"]
    )
    pointer_path.write_bytes(canonical_json_bytes(pointer) + b"\n")

    with pytest.raises(EditorialContextError, match="bound pulse path is inconsistent"):
        build_editorial_context(root)
