from __future__ import annotations

import json
import hashlib
import shutil
from pathlib import Path

import pytest

from research_pipeline import external as external_module
from research_pipeline.errors import PublicationError
from research_pipeline.hashing import canonical_json_hash
from research_pipeline.scouting import (
    freeze_inbox,
    ingest_submission,
    prepare_submission_draft,
    query_ids_for_slot,
)


PROJECT = Path(__file__).resolve().parents[2]


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    shutil.copytree(PROJECT / "schemas", root / "schemas")
    (root / "config").mkdir()
    shutil.copy2(PROJECT / "config" / "scouting.yaml", root / "config")
    return root


def _candidate() -> dict:
    candidate = {
        "schema_version": "1.0.0",
        "id": (
            "candidate-arxiv-"
            + hashlib.sha256(b"arxiv:2607.12345").hexdigest()[:20]
        ),
        "provider": "arxiv",
        "external_id": "2607.12345",
        "versioned_external_id": "2607.12345v1",
        "version": 1,
        "title": "A robust local estimator",
        "authors": ["Ada Example"],
        "published_at": "2026-07-28T08:00:00Z",
        "updated_at": "2026-07-28T08:00:00Z",
        "categories": ["stat.ME"],
        "doi": None,
        "canonical_url": "https://arxiv.org/abs/2607.12345v1",
        "abstract_sha256": "3" * 64,
        "source_type": "preprint",
        "publication_status": "preprint",
        "rights_status": "unknown",
        "review_status": "pending",
        "provenance": {
            "query_ids": ["arxiv-iterative-filtering"],
            "receipts": [
                {
                    "query_id": "arxiv-iterative-filtering",
                    "response_sha256": "4" * 64,
                    "entry_index": 0,
                }
            ],
        },
    }
    candidate["candidate_sha256"] = canonical_json_hash(
        external_module._candidate_identity_payload(candidate)
    )
    return candidate


def _batch(root: Path) -> Path:
    candidate = _candidate()
    query = {
        "id": "arxiv-iterative-filtering",
        "provider": "arxiv",
        "request_url": (
            "https://export.arxiv.org/api/query?"
            "search_query=all%3Aiterative&max_results=1"
        ),
        "response_sha256": "4" * 64,
        "response_size_bytes": 100,
        "matched_count": 1,
        "batch_candidate_count": 1,
    }
    batch = {
        "schema_version": "1.0.0",
        "as_of": "2026-07-28T09:00:00+03:00",
        "status": "candidates_pending_review",
        "metadata_only": True,
        "queries": [query],
        "candidates": [candidate],
        "already_seen_count": 0,
    }
    batch["batch_sha256"] = canonical_json_hash(batch)
    batch["id"] = f"external-batch-{batch['batch_sha256'][:20]}"
    path = root / "data" / "external" / "batches" / f"{batch['id']}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(batch), encoding="utf-8")
    return path


def test_query_shards_are_disjoint() -> None:
    all_ids = [
        query_id
        for slot in ("morning", "midday", "afternoon", "evening")
        for query_id in query_ids_for_slot(PROJECT, slot)
    ]
    assert len(all_ids) == 6
    assert len(all_ids) == len(set(all_ids))


def test_luna_submission_is_hash_bound_and_freezes_deterministically(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    batch_path = _batch(root)
    draft_path = prepare_submission_draft(
        root,
        edition_date="2026-07-29",
        slot="morning",
        batch_path=batch_path,
        reviewed_at="2026-07-28T09:05:00+03:00",
    )
    draft = json.loads(draft_path.read_text())
    draft["cards"][0].update(
        {
            "relevance_score": 91,
            "why_interesting": "It studies the same local target under a distinct robust loss.",
            "novelty_hypothesis": "The target may clarify a limitation left open by the accepted history.",
            "cluster_keys": ["robust-local-estimation"],
            "uncertainties": ["Only metadata and the abstract have been inspected."],
        }
    )
    draft_path.write_text(json.dumps(draft), encoding="utf-8")

    stored = ingest_submission(root, draft_path, batch_path)
    assert stored.is_file()
    inbox_path = freeze_inbox(
        root,
        edition_date="2026-07-29",
        frozen_at="2026-07-29T05:45:00+03:00",
    )
    inbox = json.loads(inbox_path.read_text())
    assert inbox["status"] == "ready"
    assert inbox["candidates"][0]["relevance_score"] == 91
    assert inbox["candidates"][0]["seen_in_slots"] == ["morning"]
    assert len(inbox["inbox_sha256"]) == 64

    assert (
        freeze_inbox(
            root,
            edition_date="2026-07-29",
            frozen_at="2026-07-29T05:45:00+03:00",
        )
        == inbox_path
    )


def test_luna_submission_cannot_change_candidate_identity(tmp_path: Path) -> None:
    root = _project(tmp_path)
    batch_path = _batch(root)
    draft_path = prepare_submission_draft(
        root,
        edition_date="2026-07-29",
        slot="morning",
        batch_path=batch_path,
        reviewed_at="2026-07-28T09:05:00+03:00",
    )
    draft = json.loads(draft_path.read_text())
    draft["cards"][0]["title"] = "A more exciting title"
    draft_path.write_text(json.dumps(draft), encoding="utf-8")

    with pytest.raises(PublicationError, match="changed candidate identity"):
        ingest_submission(root, draft_path, batch_path)
