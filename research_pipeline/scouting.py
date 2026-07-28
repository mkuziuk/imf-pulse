"""Private, fail-closed scouting ledger for The Residual's Luna worker."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import load_yaml
from .errors import PublicationError
from .external import validate_batch_integrity
from .hashing import canonical_json_bytes, canonical_json_hash, sha256_file
from .validation import strict_json_loads, validate_records


SLOTS = ("morning", "midday", "afternoon", "evening")


def load_scouting_config(project_root: Path) -> dict[str, Any]:
    value = load_yaml(project_root / "config" / "scouting.yaml")
    if not isinstance(value, Mapping):
        raise PublicationError("scouting configuration must be an object")
    config = dict(value)
    expected = {
        "version",
        "timezone",
        "model",
        "reasoning_effort",
        "max_cards_per_run",
        "max_candidates_per_edition",
        "slots",
        "paths",
    }
    if set(config) != expected or config["version"] != 1:
        raise PublicationError("scouting configuration fields are invalid")
    if (
        config["timezone"] != "Europe/Moscow"
        or config["model"] != "gpt-5.6-luna"
        or config["reasoning_effort"] != "high"
        or config["max_cards_per_run"] != 12
        or config["max_candidates_per_edition"] != 40
    ):
        raise PublicationError("scouting model, reasoning, or bounds changed")
    slots = config["slots"]
    if not isinstance(slots, Mapping) or tuple(slots) != SLOTS:
        raise PublicationError("scouting slots are invalid")
    all_query_ids: list[str] = []
    expected_times = {
        "morning": "09:00",
        "midday": "13:00",
        "afternoon": "17:00",
        "evening": "21:00",
    }
    for slot in SLOTS:
        row = slots[slot]
        if (
            not isinstance(row, Mapping)
            or set(row) != {"local_time", "query_ids"}
            or row["local_time"] != expected_times[slot]
            or not isinstance(row["query_ids"], list)
            or not row["query_ids"]
            or any(not isinstance(item, str) for item in row["query_ids"])
        ):
            raise PublicationError(f"scouting slot {slot} is invalid")
        all_query_ids.extend(row["query_ids"])
    if len(all_query_ids) != len(set(all_query_ids)):
        raise PublicationError("scouting query shards overlap")
    paths = config["paths"]
    expected_paths = {
        "submission_root": "data/automatic/scouting/submissions",
        "inbox_root": "data/automatic/scouting/inboxes",
        "draft_root": "tmp/luna-scout",
    }
    if paths != expected_paths:
        raise PublicationError("scouting private paths changed")
    return config


def query_ids_for_slot(project_root: Path, slot: str) -> tuple[str, ...]:
    config = load_scouting_config(project_root)
    if slot not in SLOTS:
        raise PublicationError(f"unknown scouting slot: {slot}")
    return tuple(config["slots"][slot]["query_ids"])


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise PublicationError(f"{label} is absent, unsafe, or oversized")
    try:
        value = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise PublicationError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise PublicationError(f"{label} must be an object")
    return value


def _write_immutable(path: Path, value: Mapping[str, Any]) -> Path:
    payload = canonical_json_bytes(value) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise PublicationError(f"immutable scouting output conflicts: {path.name}")
        return path
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return path


def prepare_submission_draft(
    project_root: Path,
    *,
    edition_date: str,
    slot: str,
    batch_path: Path,
    reviewed_at: str,
) -> Path:
    config = load_scouting_config(project_root)
    batch = _read_json(batch_path, "external scouting batch")
    validate_batch_integrity(batch)
    cards = []
    for candidate in batch["candidates"][: config["max_cards_per_run"]]:
        cards.append(
            {
                "candidate_id": candidate["id"],
                "candidate_sha256": candidate["candidate_sha256"],
                "canonical_url": candidate["canonical_url"],
                "title": candidate["title"],
                "relevance_score": 0,
                "why_interesting": "Luna must replace this draft text after reviewing the candidate.",
                "novelty_hypothesis": "Luna must replace this draft text with a testable novelty hypothesis.",
                "cluster_keys": ["unreviewed"],
                "uncertainties": ["Primary evidence has not been reviewed."],
                "evidence_availability": (
                    "official_arxiv_pdf_available"
                    if candidate["provider"] == "arxiv"
                    else "full_text_not_allowlisted"
                ),
            }
        )
    draft = {
        "schema_version": "1.0.0",
        "edition_date": edition_date,
        "slot": slot,
        "model": config["model"],
        "reasoning_effort": config["reasoning_effort"],
        "batch_id": batch["id"],
        "batch_sha256": batch["batch_sha256"],
        "reviewed_at": reviewed_at,
        "cards": cards,
    }
    output = (
        project_root
        / config["paths"]["draft_root"]
        / edition_date
        / f"{slot}-{batch['id']}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(canonical_json_bytes(draft) + b"\n")
    return output


def ingest_submission(
    project_root: Path, submission_path: Path, batch_path: Path
) -> Path:
    config = load_scouting_config(project_root)
    submission = _read_json(submission_path, "Luna scouting submission")
    validate_records(
        [submission],
        project_root / "schemas" / "scout-submission.schema.json",
        "Luna scouting submission",
    )
    batch = _read_json(batch_path, "external scouting batch")
    validate_batch_integrity(batch)
    if (
        submission["batch_id"] != batch["id"]
        or submission["batch_sha256"] != batch["batch_sha256"]
    ):
        raise PublicationError("Luna submission is not bound to the exact batch")
    if submission["slot"] not in SLOTS:
        raise PublicationError("Luna submission slot is invalid")
    expected_queries = set(query_ids_for_slot(project_root, submission["slot"]))
    actual_queries = {query["id"] for query in batch["queries"]}
    if actual_queries != expected_queries:
        raise PublicationError("Luna submission batch does not match its query shard")
    candidates = {
        (candidate["id"], candidate["candidate_sha256"]): candidate
        for candidate in batch["candidates"]
    }
    seen: set[tuple[str, str]] = set()
    for card in submission["cards"]:
        identity = (card["candidate_id"], card["candidate_sha256"])
        candidate = candidates.get(identity)
        if candidate is None or identity in seen:
            raise PublicationError(
                "Luna submission references an absent or duplicate candidate"
            )
        seen.add(identity)
        if (
            card["canonical_url"] != candidate["canonical_url"]
            or card["title"] != candidate["title"]
        ):
            raise PublicationError("Luna submission changed candidate identity fields")
        if (
            card["cluster_keys"] == ["unreviewed"]
            or card["why_interesting"].startswith("Luna must replace")
            or card["novelty_hypothesis"].startswith("Luna must replace")
        ):
            raise PublicationError(
                "Luna submission still contains unreviewed draft placeholders"
            )
    output = (
        project_root
        / config["paths"]["submission_root"]
        / submission["edition_date"]
        / f"{submission['slot']}-{submission['batch_id']}.json"
    )
    return _write_immutable(output, submission)


def freeze_inbox(
    project_root: Path, *, edition_date: str, frozen_at: str
) -> Path:
    config = load_scouting_config(project_root)
    root = project_root / config["paths"]["submission_root"] / edition_date
    submissions: list[tuple[Path, dict[str, Any]]] = []
    if root.is_dir() and not root.is_symlink():
        for path in sorted(root.glob("*.json")):
            submission = _read_json(path, "stored Luna scouting submission")
            validate_records(
                [submission],
                project_root / "schemas" / "scout-submission.schema.json",
                "stored Luna scouting submission",
            )
            if submission["edition_date"] != edition_date:
                raise PublicationError("stored Luna submission has the wrong edition date")
            submissions.append((path, submission))
    chosen: dict[tuple[str, str], dict[str, Any]] = {}
    seen_slots: dict[tuple[str, str], set[str]] = {}
    for path, submission in submissions:
        for card in submission["cards"]:
            identity = (card["candidate_id"], card["candidate_sha256"])
            seen_slots.setdefault(identity, set()).add(submission["slot"])
            current = chosen.get(identity)
            if current is None or (
                card["relevance_score"],
                submission["slot"],
                path.name,
            ) > (
                current["relevance_score"],
                current["_slot"],
                current["_path"],
            ):
                chosen[identity] = {
                    **card,
                    "_slot": submission["slot"],
                    "_path": path.name,
                }
    cards = []
    for identity, card in chosen.items():
        public_card = {
            key: value for key, value in card.items() if not key.startswith("_")
        }
        public_card["seen_in_slots"] = sorted(seen_slots[identity])
        cards.append(public_card)
    cards.sort(
        key=lambda card: (
            -card["relevance_score"],
            card["candidate_id"],
            card["candidate_sha256"],
        )
    )
    cards = cards[: config["max_candidates_per_edition"]]
    refs = [
        {
            "path": path.relative_to(project_root).as_posix(),
            "sha256": sha256_file(path),
        }
        for path, _submission in submissions
    ]
    inbox: dict[str, Any] = {
        "schema_version": "1.0.0",
        "edition_date": edition_date,
        "frozen_at": frozen_at,
        "status": "ready" if cards else "no_candidates",
        "submission_refs": refs,
        "candidates": cards,
    }
    inbox["inbox_sha256"] = canonical_json_hash(inbox)
    validate_records(
        [inbox],
        project_root / "schemas" / "scout-inbox.schema.json",
        "frozen Luna scouting inbox",
    )
    output = project_root / config["paths"]["inbox_root"] / f"{edition_date}.json"
    return _write_immutable(output, inbox)
