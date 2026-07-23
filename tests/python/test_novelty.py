from __future__ import annotations

import json
from pathlib import Path

from research_pipeline.novelty import (
    NoveltyPolicy,
    analyze_release_changes,
    proposal_fingerprints_for_knowledge_ids,
)
from research_pipeline.validation import validate_records


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


def _source(content: str = "a", semantic: str = "b") -> dict:
    return {
        "id": "src-test",
        "content_sha256": content * 64,
        "extract_semantic_sha256": semantic * 64,
        "snapshot_id": f"snapshot-{content}",
    }


def _evidence(content: str = "a") -> list[dict]:
    return [
        {
            "source_id": "src-test",
            "source_sha256": content * 64,
            "role": "direct",
            "locator": {
                "kind": "text_lines",
                "path": "README.md",
                "line_start": 1,
                "line_end": 3,
            },
        }
    ]


def _claim(claim_id: str, *, evidence: bool = True, confidence: float = 0.8) -> dict:
    return {
        "id": claim_id,
        "created_at": "2026-07-23T00:00:00Z",
        "updated_at": "2026-07-23T00:00:00Z",
        "normalized_text": f"Result represented by {claim_id}.",
        "statement_kind": "empirical_result",
        "evidence_status": "observed",
        "scope": "Deterministic test fixture.",
        "assumptions": ["The fixture is internally consistent."],
        "confidence": {"level": "high", "score": confidence, "rationale": "fixture"},
        "evidence": _evidence("c") if evidence else [],
    }


def _relationship(
    relationship_id: str, predicate: str, from_id: str, to_id: str
) -> dict:
    return {
        "id": relationship_id,
        "predicate": predicate,
        "from": {"type": "claim", "id": from_id},
        "to": {"type": "claim", "id": to_id},
        "confidence": {"level": "high", "score": 0.9, "rationale": "fixture"},
        "evidence": _evidence("c"),
    }


def _release(
    root: Path,
    name: str,
    *,
    sources: list[dict] | None = None,
    claims: list[dict] | None = None,
    relationships: list[dict] | None = None,
) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    manifest = {
        "release_id": name,
        "semantic_fingerprint": ("1" if name.endswith("1") else "2") * 64,
    }
    (directory / "release.json").write_text(json.dumps(manifest), encoding="utf-8")
    _write_jsonl(directory / "sources.jsonl", sources or [])
    _write_jsonl(directory / "claims.jsonl", claims or [])
    _write_jsonl(directory / "methods.jsonl", [])
    _write_jsonl(directory / "experiments.jsonl", [])
    _write_jsonl(directory / "relationships.jsonl", relationships or [])
    return directory


def test_unchanged_and_byte_only_releases_do_not_fabricate_update(tmp_path: Path) -> None:
    base = _release(tmp_path, "release-00000000000000000001", sources=[_source()])
    unchanged = _release(tmp_path, "release-00000000000000000002", sources=[_source()])
    policy = NoveltyPolicy()

    first = analyze_release_changes(base, unchanged, policy=policy)
    assert first["status"] == "no_update"
    assert first["selected_candidate_fingerprints"] == []

    byte_only = _release(
        tmp_path,
        "release-00000000000000000003",
        sources=[_source(content="c", semantic="b")],
    )
    second = analyze_release_changes(base, byte_only, policy=policy)
    assert second["status"] == "no_update"
    assert [change["classification"] for change in second["source_changes"]] == [
        "byte_only"
    ]


def test_uncurated_semantic_source_change_requires_review(tmp_path: Path) -> None:
    base = _release(tmp_path, "release-00000000000000000001", sources=[_source()])
    candidate = _release(
        tmp_path,
        "release-00000000000000000002",
        sources=[_source(content="c", semantic="d")],
    )
    result = analyze_release_changes(base, candidate, policy=NoveltyPolicy())
    assert result["status"] == "review_required"
    assert "source_semantics_require_review" in result["reason_codes"]


def test_evidenced_addition_is_selected_at_threshold_and_schema_valid(
    tmp_path: Path, repository_root: Path
) -> None:
    base = _release(tmp_path, "release-00000000000000000001", sources=[_source()])
    candidate = _release(
        tmp_path,
        "release-00000000000000000002",
        sources=[_source(content="c", semantic="d")],
        claims=[_claim("claim-new")],
    )
    policy = NoveltyPolicy(materiality_threshold_basis_points=6600)
    result = analyze_release_changes(base, candidate, policy=policy)

    assert result["status"] == "selected"
    assert result["ranked_candidates"][0]["score_basis_points"] == 6600
    assert result["selected_candidate_fingerprints"] == [
        result["ranked_candidates"][0]["proposal_fingerprint"]
    ]
    validate_records(
        [result],
        repository_root / "schemas" / "change-analysis.schema.json",
        "change analysis",
    )


def test_missing_evidence_and_in_place_mutation_require_review(tmp_path: Path) -> None:
    existing = _claim("claim-existing")
    base = _release(
        tmp_path,
        "release-00000000000000000001",
        sources=[_source(content="c", semantic="d")],
        claims=[existing],
    )
    mutated = {**existing, "normalized_text": "Silently rewritten accepted claim."}
    candidate = _release(
        tmp_path,
        "release-00000000000000000002",
        sources=[_source(content="c", semantic="d")],
        claims=[mutated, _claim("claim-no-evidence", evidence=False)],
    )
    result = analyze_release_changes(base, candidate, policy=NoveltyPolicy())
    assert result["status"] == "review_required"
    assert set(result["reason_codes"]) == {
        "accepted_knowledge_was_mutated",
        "new_knowledge_lacks_evidence",
    }


def test_contradiction_and_different_target_flags_receive_explicit_bonuses(
    tmp_path: Path,
) -> None:
    base = _release(
        tmp_path,
        "release-00000000000000000001",
        sources=[_source(content="c", semantic="d")],
    )
    candidate = _release(
        tmp_path,
        "release-00000000000000000002",
        sources=[_source(content="c", semantic="d")],
        claims=[_claim("claim-a"), _claim("claim-b")],
        relationships=[
            _relationship("relationship-contradiction", "contradicts", "claim-a", "claim-b"),
            _relationship(
                "relationship-target", "uses-different-target", "claim-a", "claim-b"
            ),
        ],
    )
    result = analyze_release_changes(base, candidate, policy=NoveltyPolicy(max_signals=3))
    by_id = {item["object_id"]: item for item in result["ranked_candidates"]}
    assert by_id["claim-a"]["flags"] == {
        "contradiction": True,
        "different_target": True,
    }
    assert by_id["claim-a"]["score_components"][
        "contradiction_bonus_basis_points"
    ] == 1000
    assert by_id["claim-a"]["score_components"][
        "different_target_bonus_basis_points"
    ] == 750


def test_ranking_tie_break_max_three_and_prior_fingerprint_dedup(tmp_path: Path) -> None:
    base = _release(
        tmp_path,
        "release-00000000000000000001",
        sources=[_source(content="c", semantic="d")],
    )
    candidate = _release(
        tmp_path,
        "release-00000000000000000002",
        sources=[_source(content="c", semantic="d")],
        claims=[_claim(f"claim-{letter}") for letter in "dcba"],
    )
    first = analyze_release_changes(
        base, candidate, policy=NoveltyPolicy(max_signals=3)
    )
    assert [item["object_id"] for item in first["ranked_candidates"]] == [
        "claim-a",
        "claim-b",
        "claim-c",
        "claim-d",
    ]
    assert len(first["selected_candidate_fingerprints"]) == 3
    assert first == analyze_release_changes(
        base, candidate, policy=NoveltyPolicy(max_signals=3)
    )

    duplicate = first["ranked_candidates"][0]["proposal_fingerprint"]
    second = analyze_release_changes(
        base,
        candidate,
        policy=NoveltyPolicy(max_signals=3),
        prior_proposal_fingerprints=[duplicate],
    )
    assert all(
        item["proposal_fingerprint"] != duplicate
        for item in second["ranked_candidates"]
    )
    assert len(second["selected_candidate_fingerprints"]) == 3

    all_prior = [item["proposal_fingerprint"] for item in first["ranked_candidates"]]
    third = analyze_release_changes(
        base,
        candidate,
        policy=NoveltyPolicy(max_signals=3),
        prior_proposal_fingerprints=all_prior,
    )
    assert third["status"] == "no_update"
    assert third["reason_codes"] == ["all_candidates_previously_proposed"]


def test_prior_fingerprints_reconstruct_from_accepted_release_knowledge_ids(
    tmp_path: Path,
) -> None:
    release = _release(
        tmp_path,
        "release-00000000000000000002",
        sources=[_source(content="c", semantic="d")],
        claims=[_claim("claim-accepted")],
    )
    base = _release(
        tmp_path,
        "release-00000000000000000001",
        sources=[_source(content="c", semantic="d")],
    )
    analysis = analyze_release_changes(base, release, policy=NoveltyPolicy())
    assert proposal_fingerprints_for_knowledge_ids(
        release, ["claim-accepted"]
    ) == analysis["selected_candidate_fingerprints"]
