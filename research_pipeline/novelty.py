"""Deterministic novelty analysis for two immutable research releases.

The analyzer is deliberately conservative.  It ranks only newly appended,
evidence-backed knowledge records.  Source-only semantic changes, deletions,
and in-place knowledge mutations are routed to review instead of being turned
into report claims.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Mapping

from .hashing import canonical_json_hash
from .validation import read_json, read_jsonl


KNOWLEDGE_FILES: tuple[tuple[str, str], ...] = (
    ("claim", "claims.jsonl"),
    ("method", "methods.jsonl"),
    ("experiment", "experiments.jsonl"),
    ("relationship", "relationships.jsonl"),
)

STRONG_EVIDENCE_ROLES = {"direct", "data", "implementation"}
MEDIUM_EVIDENCE_ROLES = {"supporting", "contradicting", "quotation"}
CONTRADICTION_PREDICATES = {"contradicts", "fails-to-reproduce"}
DIFFERENT_TARGET_PREDICATES = {"uses-different-target"}
TARGET_STATEMENT_KINDS = {
    "definition_drift",
    "estimand_definition",
    "target_definition",
    "target_drift",
}


@dataclass(frozen=True)
class NoveltyPolicy:
    """Integer scoring parameters supplied by configuration or a caller.

    Basis points avoid platform-sensitive float accumulation.  A record's
    total is the capped sum of a fixed novelty base, evidence strength,
    stated confidence, and explicit contradiction/different-target bonuses.
    """

    materiality_threshold_basis_points: int = 6500
    max_signals: int = 3
    require_evidence: bool = True
    base_score_basis_points: int = 3000
    evidence_weight_basis_points: int = 2000
    confidence_weight_basis_points: int = 2000
    default_confidence_basis_points: int = 7500
    contradiction_bonus_basis_points: int = 1000
    different_target_bonus_basis_points: int = 750
    scoring_version: str = "deterministic-bp-v1"

    def validate(self) -> None:
        if not 0 <= self.materiality_threshold_basis_points <= 10_000:
            raise ValueError("materiality threshold must be between 0 and 10000 basis points")
        if not 1 <= self.max_signals <= 3:
            raise ValueError("max_signals must be between one and three")
        for name in (
            "base_score_basis_points",
            "evidence_weight_basis_points",
            "confidence_weight_basis_points",
            "default_confidence_basis_points",
            "contradiction_bonus_basis_points",
            "different_target_bonus_basis_points",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or not 0 <= value <= 10_000:
                raise ValueError(f"{name} must be an integer from 0 to 10000")
        if not self.scoring_version:
            raise ValueError("scoring_version must be non-empty")


def _records_by_id(path: Path) -> dict[str, dict[str, Any]]:
    records = read_jsonl(path) if path.exists() else []
    indexed: dict[str, dict[str, Any]] = {}
    for record in records:
        record_id = record.get("id")
        if not isinstance(record_id, str) or not record_id:
            raise ValueError(f"record without an id in {path}")
        if record_id in indexed:
            raise ValueError(f"duplicate record id {record_id!r} in {path}")
        indexed[record_id] = dict(record)
    return indexed


def _release_manifest(directory: Path) -> dict[str, Any]:
    manifest = read_json(directory / "release.json")
    release_id = manifest.get("release_id")
    if not isinstance(release_id, str) or not release_id:
        raise ValueError(f"release manifest has no release_id: {directory}")
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError(f"release directory must be a regular directory: {directory}")
    return manifest


def _semantic_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Remove bookkeeping timestamps that must not create scientific novelty."""

    return {
        key: value
        for key, value in record.items()
        if key not in {"created_at", "updated_at", "last_processed_at", "retrieved_at"}
    }


def _nullable_hash(record: Mapping[str, Any] | None, field: str) -> str | None:
    if record is None:
        return None
    value = record.get(field)
    return value if isinstance(value, str) and value else None


def _source_changes(base: Path, candidate: Path) -> list[dict[str, Any]]:
    old = _records_by_id(base / "sources.jsonl")
    new = _records_by_id(candidate / "sources.jsonl")
    changes: list[dict[str, Any]] = []
    for source_id in sorted(set(old) | set(new)):
        before = old.get(source_id)
        after = new.get(source_id)
        if before is None:
            classification = "added"
        elif after is None:
            classification = "removed"
        else:
            before_content = _nullable_hash(before, "content_sha256")
            after_content = _nullable_hash(after, "content_sha256")
            before_semantic = _nullable_hash(before, "extract_semantic_sha256")
            after_semantic = _nullable_hash(after, "extract_semantic_sha256")
            if before_content != after_content and before_semantic == after_semantic:
                classification = "byte_only"
            elif before_semantic != after_semantic:
                classification = "semantic"
            elif _semantic_record(before) != _semantic_record(after):
                classification = "metadata_only"
            else:
                continue
        identity = {
            "source_id": source_id,
            "classification": classification,
            "previous_content_sha256": _nullable_hash(before, "content_sha256"),
            "current_content_sha256": _nullable_hash(after, "content_sha256"),
            "previous_semantic_sha256": _nullable_hash(before, "extract_semantic_sha256"),
            "current_semantic_sha256": _nullable_hash(after, "extract_semantic_sha256"),
        }
        changes.append(
            {
                "id": f"source-change-{canonical_json_hash(identity)[:20]}",
                **identity,
                "fingerprint": canonical_json_hash(identity),
            }
        )
    return changes


def _knowledge_changes(
    base: Path, candidate: Path
) -> tuple[list[dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    changes: list[dict[str, Any]] = []
    added_records: dict[tuple[str, str], dict[str, Any]] = {}
    for object_type, filename in KNOWLEDGE_FILES:
        old = _records_by_id(base / filename)
        new = _records_by_id(candidate / filename)
        for object_id in sorted(set(old) | set(new)):
            before = old.get(object_id)
            after = new.get(object_id)
            before_hash = canonical_json_hash(_semantic_record(before)) if before else None
            after_hash = canonical_json_hash(_semantic_record(after)) if after else None
            if before is None:
                classification = "added"
                added_records[(object_type, object_id)] = after
            elif after is None:
                classification = "removed"
            elif before_hash != after_hash:
                classification = "modified"
            else:
                continue
            evidence = after.get("evidence", []) if after else []
            source_ids = sorted(
                {
                    item.get("source_id")
                    for item in evidence
                    if isinstance(item, Mapping) and isinstance(item.get("source_id"), str)
                }
            )
            changes.append(
                {
                    "object_type": object_type,
                    "object_id": object_id,
                    "classification": classification,
                    "previous_semantic_sha256": before_hash,
                    "current_semantic_sha256": after_hash,
                    "evidence_count": len(evidence) if isinstance(evidence, list) else 0,
                    "source_ids": source_ids,
                }
            )
    return changes, added_records


def _relationship_flags(
    added: Mapping[tuple[str, str], Mapping[str, Any]]
) -> dict[tuple[str, str], dict[str, bool]]:
    flags: dict[tuple[str, str], dict[str, bool]] = {}
    for (object_type, object_id), record in added.items():
        contradiction = False
        different_target = False
        if object_type == "relationship":
            predicate = record.get("predicate")
            contradiction = predicate in CONTRADICTION_PREDICATES
            different_target = predicate in DIFFERENT_TARGET_PREDICATES
            for endpoint_name in ("from", "to"):
                endpoint = record.get(endpoint_name)
                if not isinstance(endpoint, Mapping):
                    continue
                endpoint_type = endpoint.get("type")
                endpoint_id = endpoint.get("id")
                if not isinstance(endpoint_type, str) or not isinstance(endpoint_id, str):
                    continue
                endpoint_flags = flags.setdefault(
                    (endpoint_type, endpoint_id),
                    {"contradiction": False, "different_target": False},
                )
                endpoint_flags["contradiction"] |= contradiction
                endpoint_flags["different_target"] |= different_target
        if object_type == "claim":
            contradiction |= record.get("evidence_status") == "contradicted"
            different_target |= record.get("statement_kind") in TARGET_STATEMENT_KINDS
        own = flags.setdefault(
            (object_type, object_id),
            {"contradiction": False, "different_target": False},
        )
        own["contradiction"] |= contradiction
        own["different_target"] |= different_target
    return flags


def _confidence_basis_points(record: Mapping[str, Any], policy: NoveltyPolicy) -> int:
    confidence = record.get("confidence")
    score = confidence.get("score") if isinstance(confidence, Mapping) else None
    if score is None:
        return policy.default_confidence_basis_points
    try:
        decimal = Decimal(str(score))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid confidence score on {record.get('id')!r}") from exc
    if not Decimal("0") <= decimal <= Decimal("1"):
        raise ValueError(f"confidence score outside [0,1] on {record.get('id')!r}")
    return int((decimal * 10_000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _evidence_strength_basis_points(evidence: Any) -> int:
    if not isinstance(evidence, list) or not evidence:
        return 0
    roles = {
        item.get("role")
        for item in evidence
        if isinstance(item, Mapping) and isinstance(item.get("role"), str)
    }
    if roles & STRONG_EVIDENCE_ROLES:
        return 10_000
    if roles & MEDIUM_EVIDENCE_ROLES:
        return 7_500
    return 4_000


def _scaled(weight: int, value: int) -> int:
    return (weight * value + 5_000) // 10_000


def proposal_fingerprint_for_record(
    object_type: str, record: Mapping[str, Any]
) -> str:
    """Return the history-stable fingerprint used to de-duplicate a signal."""

    object_id = record.get("id")
    if object_type not in {item[0] for item in KNOWLEDGE_FILES}:
        raise ValueError(f"unknown knowledge object type: {object_type!r}")
    if not isinstance(object_id, str) or not object_id:
        raise ValueError("knowledge record has no object id")
    return canonical_json_hash(
        {
            "object_type": object_type,
            "object_id": object_id,
            "record_sha256": canonical_json_hash(_semantic_record(record)),
        }
    )


def proposal_fingerprints_for_knowledge_ids(
    release_directory: Path, knowledge_ids: Iterable[str]
) -> list[str]:
    """Reconstruct prior fingerprints from an immutable accepted release.

    Accepted pulse front matter already stores ``knowledge_ids`` and accepted
    publication history stores the exact release.  This helper therefore lets
    callers de-duplicate without consulting mutable review/proposal files.
    """

    release_directory = release_directory.resolve(strict=True)
    by_id: dict[str, tuple[str, dict[str, Any]]] = {}
    for object_type, filename in KNOWLEDGE_FILES:
        for object_id, record in _records_by_id(release_directory / filename).items():
            if object_id in by_id:
                raise ValueError(f"knowledge id is ambiguous across object types: {object_id}")
            by_id[object_id] = (object_type, record)
    fingerprints: list[str] = []
    for object_id in knowledge_ids:
        if object_id not in by_id:
            raise ValueError(f"accepted pulse references unknown knowledge id: {object_id}")
        object_type, record = by_id[object_id]
        fingerprints.append(proposal_fingerprint_for_record(object_type, record))
    return fingerprints


def _candidate(
    object_type: str,
    object_id: str,
    record: Mapping[str, Any],
    flags: Mapping[str, bool],
    policy: NoveltyPolicy,
) -> dict[str, Any]:
    evidence = record.get("evidence", [])
    evidence_fingerprints = sorted(
        canonical_json_hash(item)
        for item in evidence
        if isinstance(item, Mapping)
    )
    source_ids = sorted(
        {
            item.get("source_id")
            for item in evidence
            if isinstance(item, Mapping) and isinstance(item.get("source_id"), str)
        }
    )
    record_sha256 = canonical_json_hash(_semantic_record(record))
    proposal_fingerprint = proposal_fingerprint_for_record(object_type, record)
    evidence_strength = _evidence_strength_basis_points(evidence)
    confidence = _confidence_basis_points(record, policy)
    components = {
        "base_basis_points": policy.base_score_basis_points,
        "evidence_basis_points": _scaled(
            policy.evidence_weight_basis_points, evidence_strength
        ),
        "confidence_basis_points": _scaled(
            policy.confidence_weight_basis_points, confidence
        ),
        "contradiction_bonus_basis_points": (
            policy.contradiction_bonus_basis_points if flags["contradiction"] else 0
        ),
        "different_target_bonus_basis_points": (
            policy.different_target_bonus_basis_points
            if flags["different_target"]
            else 0
        ),
    }
    score = min(10_000, sum(components.values()))
    return {
        "proposal_fingerprint": proposal_fingerprint,
        "object_type": object_type,
        "object_id": object_id,
        "record_sha256": record_sha256,
        "source_ids": source_ids,
        "evidence_fingerprints": evidence_fingerprints,
        "evidence_count": len(evidence_fingerprints),
        "flags": {
            "contradiction": bool(flags["contradiction"]),
            "different_target": bool(flags["different_target"]),
        },
        "score_components": components,
        "score_basis_points": score,
    }


def analyze_release_changes(
    base_release: Path,
    candidate_release: Path,
    *,
    policy: NoveltyPolicy,
    prior_proposal_fingerprints: Iterable[str] = (),
) -> dict[str, Any]:
    """Compare two releases and return a canonical, deterministic decision."""

    policy.validate()
    base_release = base_release.resolve(strict=True)
    candidate_release = candidate_release.resolve(strict=True)
    base_manifest = _release_manifest(base_release)
    candidate_manifest = _release_manifest(candidate_release)
    source_changes = _source_changes(base_release, candidate_release)
    knowledge_changes, added_records = _knowledge_changes(base_release, candidate_release)
    flags = _relationship_flags(added_records)
    prior_values = list(prior_proposal_fingerprints)
    if any(
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
        for value in prior_values
    ):
        raise ValueError("prior proposal fingerprints must be lowercase SHA-256 values")
    prior = sorted(set(prior_values))
    prior_set = set(prior)

    candidates = [
        _candidate(
            object_type,
            object_id,
            record,
            flags.get(
                (object_type, object_id),
                {"contradiction": False, "different_target": False},
            ),
            policy,
        )
        for (object_type, object_id), record in sorted(added_records.items())
    ]
    candidates.sort(
        key=lambda item: (
            -item["score_basis_points"],
            item["object_type"],
            item["object_id"],
            item["proposal_fingerprint"],
        )
    )

    blockers: set[str] = set()
    for change in knowledge_changes:
        if change["classification"] in {"removed", "modified"}:
            blockers.add("accepted_knowledge_was_mutated")
    if policy.require_evidence and any(
        candidate["evidence_count"] == 0 for candidate in candidates
    ):
        blockers.add("new_knowledge_lacks_evidence")

    evidenced_source_ids = {
        source_id
        for candidate in candidates
        if candidate["evidence_count"] > 0
        for source_id in candidate["source_ids"]
    }
    for change in source_changes:
        if change["classification"] in {"semantic", "added", "removed"}:
            if change["source_id"] not in evidenced_source_ids:
                blockers.add("source_semantics_require_review")

    ranked = [
        candidate
        for candidate in candidates
        if (not policy.require_evidence or candidate["evidence_count"] > 0)
        and candidate["proposal_fingerprint"] not in prior_set
    ]
    material = [
        candidate
        for candidate in ranked
        if candidate["score_basis_points"]
        >= policy.materiality_threshold_basis_points
    ]

    if blockers:
        status = "review_required"
        selected: list[dict[str, Any]] = []
        reason_codes = sorted(blockers)
    elif material:
        status = "selected"
        selected = material[: policy.max_signals]
        reason_codes = ["material_evidence_backed_changes"]
    else:
        status = "no_update"
        selected = []
        if candidates and all(
            item["proposal_fingerprint"] in prior_set for item in candidates
        ):
            reason_codes = ["all_candidates_previously_proposed"]
        elif candidates:
            reason_codes = ["all_candidates_below_materiality_threshold"]
        elif source_changes or knowledge_changes:
            reason_codes = ["no_eligible_knowledge_development"]
        else:
            reason_codes = ["release_content_unchanged"]

    analysis: dict[str, Any] = {
        "schema_version": "1.0.0",
        "status": status,
        "base_release_id": base_manifest["release_id"],
        "candidate_release_id": candidate_manifest["release_id"],
        "base_semantic_fingerprint": base_manifest.get("semantic_fingerprint"),
        "candidate_semantic_fingerprint": candidate_manifest.get(
            "semantic_fingerprint"
        ),
        "policy": asdict(policy),
        "prior_proposal_fingerprints": prior,
        "source_changes": source_changes,
        "knowledge_changes": knowledge_changes,
        "ranked_candidates": ranked,
        "selected_candidate_fingerprints": [
            item["proposal_fingerprint"] for item in selected
        ],
        "reason_codes": reason_codes,
    }
    fingerprint = canonical_json_hash(analysis)
    analysis["analysis_fingerprint"] = fingerprint
    analysis["id"] = f"change-analysis-{fingerprint[:20]}"
    return analysis


def write_analysis_json(path: Path, analysis: Mapping[str, Any]) -> None:
    """Write canonical analysis JSON without replacing an existing decision."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        analysis, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ) + "\n"
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
