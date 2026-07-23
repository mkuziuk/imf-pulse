from __future__ import annotations

import pytest

from research_pipeline.errors import ValidationError
from research_pipeline.hashing import canonical_json_hash
from pathlib import Path

from research_pipeline.validation import (
    _validate_cross_references,
    strict_json_loads,
    validate_records,
)


def _source(path="README.md", extract=None):
    extract = extract or _extract(path=path)
    semantic = canonical_json_hash(
        [
            {
                key: value
                for key, value in extract.items()
                if key not in {"id", "source_id", "source_sha256", "schema_version"}
            }
        ]
    )
    return {
        "id": "src-one",
        "content_sha256": "a" * 64,
        "path": path,
        "extract_semantic_sha256": semantic,
    }


def _claim(evidence):
    return {"id": "claim-one", "evidence": evidence}


def _extract(kind="file_lines", path="README.md", **locator):
    return {
        "id": "extract-one",
        "source_id": "src-one",
        "source_sha256": "a" * 64,
        "locator": {"kind": kind, "path": path, **locator},
    }


def test_strict_json_rejects_duplicate_keys_and_nan() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        strict_json_loads('{"a": 1, "a": 2}')
    with pytest.raises(ValueError, match="non-finite"):
        strict_json_loads('{"a": NaN}')


def test_evidence_must_resolve_source_version_and_precise_locator() -> None:
    valid = {
        "source_id": "src-one",
        "source_sha256": "a" * 64,
        "locator": {"kind": "text_lines", "path": "README.md", "line_start": 1, "line_end": 2},
    }
    extract = _extract(line_start=1, line_end=20)
    _validate_cross_references(
        {
            "sources.jsonl": [_source(extract=extract)],
            "claims.jsonl": [_claim([valid])],
            "methods.jsonl": [],
            "experiments.jsonl": [],
            "relationships.jsonl": [],
            "extracts": [extract],
        }
    )
    invalid_hash = dict(valid, source_sha256="b" * 64)
    with pytest.raises(ValidationError, match="hash is unavailable"):
        _validate_cross_references(
            {
                "sources.jsonl": [_source(extract=extract)],
                "claims.jsonl": [_claim([invalid_hash])],
                "methods.jsonl": [],
                "experiments.jsonl": [],
                "relationships.jsonl": [],
                "extracts": [extract],
            }
        )
    imprecise = dict(valid, locator={"kind": "text_lines", "path": "README.md"})
    with pytest.raises(ValidationError, match="not precise"):
        _validate_cross_references(
            {
                "sources.jsonl": [_source(extract=extract)],
                "claims.jsonl": [_claim([imprecise])],
                "methods.jsonl": [],
                "experiments.jsonl": [],
                "relationships.jsonl": [],
                "extracts": [extract],
            }
        )


def test_relationship_endpoints_and_types_must_resolve() -> None:
    evidence = [
        {
            "source_id": "src-one",
            "source_sha256": "a" * 64,
            "locator": {"kind": "pdf", "path": "paper.pdf", "page": 1},
        }
    ]
    extract = _extract(kind="pdf", path="paper.pdf", page=1)
    parsed = {
        "sources.jsonl": [_source("paper.pdf", extract=extract)],
        "claims.jsonl": [{"id": "claim-one", "evidence": evidence}],
        "methods.jsonl": [{"id": "method-one", "evidence": evidence}],
        "experiments.jsonl": [],
        "relationships.jsonl": [
            {
                "id": "relationship-one",
                "from": {"type": "method", "id": "method-one"},
                "to": {"type": "experiment", "id": "claim-one"},
                "evidence": evidence,
            }
        ],
        "extracts": [extract],
    }
    with pytest.raises(ValidationError, match="endpoint type mismatch"):
        _validate_cross_references(parsed)


@pytest.mark.parametrize(
    ("status", "pointer_changed"),
    [
        ("ready_to_publish", True),
        ("unchanged", True),
        ("failed", True),
        ("processed_no_pulse", False),
        ("published", False),
    ],
)
def test_run_schema_rejects_false_pointer_commit_state(
    schemas_directory: Path, status: str, pointer_changed: bool
) -> None:
    record = {
        "schema_version": 1,
        "id": "run-test",
        "status": status,
        "release_id": "release-00000000000000000000",
        "started_at": "2026-07-23T00:00:00Z",
        "completed_at": "2026-07-23T00:00:01Z",
        "pointer_changed": pointer_changed,
    }
    with pytest.raises(ValidationError, match="schema validation failed"):
        validate_records([record], schemas_directory / "run.schema.json", "run")
