"""Security-gated handoff from Luna discovery to the offline Sol editor."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .automatic import _extract_pdf
from .errors import PublicationError
from .evidence_fetch import fetch_exact_arxiv_pdf
from .external import _batch_identity_payload, validate_batch_integrity
from .external_preflight import write_scheduled_search_outcome
from .hashing import canonical_json_bytes, canonical_json_hash, sha256_file
from .validation import strict_json_loads, validate_records


AUDIT_INPUT_SCHEMA = "scout-audit-input.schema.json"
AUDIT_VERDICT_SCHEMA = "scout-audit-verdict.schema.json"
APPROVED_BUNDLE_SCHEMA = "scout-approved-bundle.schema.json"
MAX_AUDIT_CANDIDATES = 12
MAX_APPROVED_CANDIDATES = 6
INSTRUCTION_PATTERN = re.compile(
    r"(?i)\b(?:ignore|override|disregard|reveal|dump|execute|run|call)\b.{0,80}"
    r"\b(?:instruction|prompt|system|developer|tool|command|secret|credential)\b"
)


def approved_bundle_path(run_date: str) -> str:
    return f"data/automatic/security/approved/{run_date}.json"


def _parse_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PublicationError("security handoff timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise PublicationError("security handoff timestamp requires a timezone")
    return parsed.isoformat(timespec="seconds")


def _safe_project_file(project_root: Path, relative: str, label: str) -> Path:
    if (
        not isinstance(relative, str)
        or relative.startswith("/")
        or ".." in Path(relative).parts
        or "\\" in relative
    ):
        raise PublicationError(f"{label} path is unsafe")
    root = project_root.resolve(strict=True)
    path = (root / relative).resolve(strict=True)
    if root not in path.parents or path.is_symlink() or not path.is_file():
        raise PublicationError(f"{label} is unavailable or unsafe")
    return path


def _read_json(path: Path, label: str, *, maximum: int = 16 * 1024 * 1024) -> dict[str, Any]:
    try:
        mode = path.lstat().st_mode
        if (
            path.is_symlink()
            or not stat.S_ISREG(mode)
            or not 0 < path.stat().st_size <= maximum
        ):
            raise PublicationError(f"{label} is unavailable, unsafe, or oversized")
        value = strict_json_loads(path.read_text(encoding="utf-8"))
    except PublicationError:
        raise
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise PublicationError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise PublicationError(f"{label} must be an object")
    return value


def _write_immutable(path: Path, value: Mapping[str, Any]) -> Path:
    payload = canonical_json_bytes(value) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise PublicationError(f"immutable security handoff conflicts: {path}")
        return path
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return path


def _write_jsonl_immutable(path: Path, rows: Sequence[Mapping[str, Any]]) -> Path:
    payload = b"".join(canonical_json_bytes(row) + b"\n" for row in rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise PublicationError(f"immutable security extract conflicts: {path}")
        return path
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return path


def _validate_inbox(project_root: Path, run_date: str) -> tuple[Path, dict[str, Any]]:
    relative = f"data/automatic/scouting/inboxes/{run_date}.json"
    path = _safe_project_file(project_root, relative, "frozen Luna inbox")
    inbox = _read_json(path, "frozen Luna inbox")
    validate_records(
        [inbox],
        project_root / "schemas" / "scout-inbox.schema.json",
        "frozen Luna inbox",
    )
    identity = {key: value for key, value in inbox.items() if key != "inbox_sha256"}
    if (
        inbox.get("edition_date") != run_date
        or inbox.get("inbox_sha256") != canonical_json_hash(identity)
    ):
        raise PublicationError("frozen Luna inbox identity hash does not match content")
    return path, inbox


def _risk_flags(candidate: Mapping[str, Any], card: Mapping[str, Any]) -> list[str]:
    flags: list[str] = []
    text = "\n".join(
        [
            str(candidate.get("title", "")),
            str(card.get("why_interesting", "")),
            str(card.get("novelty_hypothesis", "")),
            *[str(item) for item in card.get("uncertainties", [])],
        ]
    )
    if INSTRUCTION_PATTERN.search(text):
        flags.append("instruction_like_text")
    if candidate.get("provider") != "arxiv":
        flags.append("non_arxiv_provider")
    if card.get("evidence_availability") != "official_arxiv_pdf_available":
        flags.append("full_text_not_allowlisted")
    return flags


def stage_audit_input(
    project_root: Path,
    *,
    run_date: str,
    staged_at: str,
    auditor_workspace: Path,
) -> Path:
    """Verify Luna's immutable ledger and stage a bounded Aegis review input."""

    staged_at = _parse_timestamp(staged_at)
    inbox_path, inbox = _validate_inbox(project_root, run_date)
    bindings: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for reference in inbox["submission_refs"]:
        submission_path = _safe_project_file(
            project_root, reference["path"], "Luna submission reference"
        )
        if sha256_file(submission_path) != reference["sha256"]:
            raise PublicationError("Luna submission reference hash does not match")
        submission = _read_json(submission_path, "Luna submission")
        validate_records(
            [submission],
            project_root / "schemas" / "scout-submission.schema.json",
            "Luna submission",
        )
        if submission.get("edition_date") != run_date:
            raise PublicationError("Luna submission date does not match the inbox")
        batch_relative = f"data/external/batches/{submission['batch_id']}.json"
        batch_path = _safe_project_file(project_root, batch_relative, "Luna source batch")
        batch = _read_json(batch_path, "Luna source batch")
        validate_batch_integrity(batch)
        if (
            batch["batch_sha256"] != submission["batch_sha256"]
            or batch["id"] != submission["batch_id"]
        ):
            raise PublicationError("Luna submission batch binding does not match")
        candidates = {
            (row["id"], row["candidate_sha256"]): row for row in batch["candidates"]
        }
        for card in submission["cards"]:
            identity = (card["candidate_id"], card["candidate_sha256"])
            candidate = candidates.get(identity)
            if candidate is None:
                raise PublicationError("Luna card does not resolve to its source batch")
            bindings.setdefault(identity, []).append(
                {
                    "batch": batch,
                    "candidate": candidate,
                    "submission_path": reference["path"],
                }
            )

    staged_candidates: list[dict[str, Any]] = []
    for card in inbox["candidates"]:
        identity = (card["candidate_id"], card["candidate_sha256"])
        matches = bindings.get(identity, [])
        if not matches:
            raise PublicationError("frozen Luna card has no exact submission provenance")
        matches.sort(key=lambda row: row["batch"]["id"])
        selected = matches[0]
        candidate = selected["candidate"]
        if (
            card["title"] != candidate["title"]
            or card["canonical_url"] != candidate["canonical_url"]
        ):
            raise PublicationError("frozen Luna card changed candidate identity")
        staged_candidates.append(
            {
                "batch_id": selected["batch"]["id"],
                "batch_sha256": selected["batch"]["batch_sha256"],
                "candidate": candidate,
                "luna_card": card,
                "submission_paths": sorted(
                    {str(row["submission_path"]) for row in matches}
                ),
                "risk_flags": _risk_flags(candidate, card),
            }
        )
    staged_candidates.sort(
        key=lambda row: (
            -int(row["luna_card"]["relevance_score"]),
            row["candidate"]["id"],
        )
    )
    staged_candidates = staged_candidates[:MAX_AUDIT_CANDIDATES]
    value: dict[str, Any] = {
        "schema_version": "1.0.0",
        "edition_date": run_date,
        "staged_at": staged_at,
        "status": "ready" if staged_candidates else "no_candidates",
        "inbox_path": inbox_path.relative_to(project_root).as_posix(),
        "inbox_sha256": inbox["inbox_sha256"],
        "candidates": staged_candidates,
    }
    value["input_sha256"] = canonical_json_hash(value)
    validate_records(
        [value],
        project_root / "schemas" / AUDIT_INPUT_SCHEMA,
        "Aegis audit input",
    )
    output = _write_immutable(
        auditor_workspace / "inbox" / f"{run_date}.json", value
    )
    verdict_schema = _read_json(
        project_root / "schemas" / AUDIT_VERDICT_SCHEMA,
        "Aegis verdict schema",
        maximum=512 * 1024,
    )
    _write_immutable(
        auditor_workspace / "inbox" / "verdict-schema.json", verdict_schema
    )
    return output


def _merged_approved_batch(
    project_root: Path,
    run_date: str,
    selected: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], str]:
    candidates = [dict(row["candidate"]) for row in selected]
    query_rows: dict[str, dict[str, Any]] = {}
    for row in selected:
        batch_path = _safe_project_file(
            project_root,
            f"data/external/batches/{row['batch_id']}.json",
            "approved source batch",
        )
        batch = _read_json(batch_path, "approved source batch")
        validate_batch_integrity(batch)
        for query in batch["queries"]:
            if query["id"] not in row["candidate"]["provenance"]["query_ids"]:
                continue
            existing = query_rows.get(query["id"])
            candidate_query = dict(query)
            if existing is not None and existing != candidate_query:
                raise PublicationError("approved query provenance is ambiguous")
            query_rows[query["id"]] = candidate_query
    for query_id, query in query_rows.items():
        query["batch_candidate_count"] = sum(
            query_id in candidate["provenance"]["query_ids"] for candidate in candidates
        )
    as_of = (
        datetime.fromisoformat(f"{run_date}T06:00:00+03:00")
        .astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    batch: dict[str, Any] = {
        "schema_version": "1.0.0",
        "as_of": as_of,
        "status": "candidates_pending_review" if candidates else "no_candidates",
        "metadata_only": True,
        "queries": [query_rows[key] for key in sorted(query_rows)],
        "candidates": candidates,
        "already_seen_count": 0,
    }
    batch["batch_sha256"] = canonical_json_hash(_batch_identity_payload(batch))
    batch["id"] = f"external-batch-{batch['batch_sha256'][:20]}"
    validate_batch_integrity(batch)
    relative = f"data/external/batches/{batch['id']}.json"
    _write_immutable(project_root / relative, batch)
    return batch, relative


def apply_audit_verdict(
    project_root: Path,
    *,
    run_date: str,
    approved_at: str,
    auditor_workspace: Path,
    fetcher: Callable[[str], bytes] | None = None,
    extractor: Callable[[Path, str, str, str], tuple[list[dict[str, Any]], str, int]] = _extract_pdf,
) -> Path:
    """Apply Aegis's bounded verdict, fetch exact PDFs, and seal Sol's input."""

    approved_at = _parse_timestamp(approved_at)
    input_path = auditor_workspace / "inbox" / f"{run_date}.json"
    verdict_path = auditor_workspace / "outbox" / f"{run_date}.json"
    audit_input = _read_json(input_path, "Aegis audit input")
    verdict = _read_json(verdict_path, "Aegis verdict", maximum=2 * 1024 * 1024)
    validate_records(
        [audit_input],
        project_root / "schemas" / AUDIT_INPUT_SCHEMA,
        "Aegis audit input",
    )
    validate_records(
        [verdict],
        project_root / "schemas" / AUDIT_VERDICT_SCHEMA,
        "Aegis verdict",
    )
    if (
        audit_input.get("edition_date") != run_date
        or verdict.get("edition_date") != run_date
        or verdict.get("input_sha256") != audit_input.get("input_sha256")
        or audit_input.get("input_sha256")
        != canonical_json_hash(
            {key: value for key, value in audit_input.items() if key != "input_sha256"}
        )
    ):
        raise PublicationError("Aegis verdict is not bound to the exact audit input")
    input_rows = {
        (row["candidate"]["id"], row["candidate"]["candidate_sha256"]): row
        for row in audit_input["candidates"]
    }
    verdict_rows = {
        (row["candidate_id"], row["candidate_sha256"]): row
        for row in verdict["candidates"]
    }
    if set(input_rows) != set(verdict_rows) or len(verdict_rows) != len(
        verdict["candidates"]
    ):
        raise PublicationError("Aegis verdict must cover every candidate exactly once")
    approved_rows: list[dict[str, Any]] = []
    for identity, source in input_rows.items():
        review = verdict_rows[identity]
        if review["decision"] != "approved":
            continue
        if source["risk_flags"]:
            raise PublicationError("Aegis cannot approve a deterministically flagged candidate")
        if (
            source["candidate"]["provider"] != "arxiv"
            or source["luna_card"]["evidence_availability"]
            != "official_arxiv_pdf_available"
        ):
            raise PublicationError("Aegis approval escaped the arXiv evidence boundary")
        approved_rows.append(
            {
                **source,
                "aegis_review": {
                    "reason": review["reason"],
                    "security_notes": review["security_notes"],
                },
            }
        )
    approved_rows.sort(
        key=lambda row: (
            -int(row["luna_card"]["relevance_score"]),
            row["candidate"]["id"],
        )
    )
    approved_rows = approved_rows[:MAX_APPROVED_CANDIDATES]

    sealed_candidates: list[dict[str, Any]] = []
    if approved_rows:
        batch, batch_relative = _merged_approved_batch(
            project_root, run_date, approved_rows
        )
        for row in approved_rows:
            evidence = fetch_exact_arxiv_pdf(
                project_root,
                batch,
                row["candidate"]["id"],
                row["candidate"]["candidate_sha256"],
                **({"fetcher": fetcher} if fetcher is not None else {}),
            )
            source_id = (
                "src-external-arxiv-"
                + str(row["candidate"]["versioned_external_id"])
                .lower()
                .replace("/", "-")
                .replace(".", "-")
            )
            pdf_path = project_root / evidence["path"]
            units, semantic_sha, _size = extractor(
                pdf_path,
                source_id,
                evidence["content_sha256"],
                evidence["logical_path"],
            )
            extract_relative = f"data/automatic/security/extracts/{source_id}.jsonl"
            _write_jsonl_immutable(project_root / extract_relative, units)
            sealed_candidates.append(
                {
                    "candidate": row["candidate"],
                    "luna_card": row["luna_card"],
                    "aegis_review": row["aegis_review"],
                    "evidence": {
                        key: evidence[key]
                        for key in (
                            "content_sha256",
                            "bytes",
                            "path",
                            "logical_path",
                            "source_url",
                            "pdf_url",
                        )
                    }
                    | {
                        "extract_semantic_sha256": semantic_sha,
                        "page_count": len(units),
                    },
                    "extract_path": extract_relative,
                }
            )
        status = "ready"
        batch_id: str | None = batch["id"]
        batch_sha: str | None = batch["batch_sha256"]
        write_scheduled_search_outcome(
            project_root,
            run_date=run_date,
            as_of=f"{run_date}T06:00:00+03:00",
            status="ready",
            reason="Aegis approved a hash-bound, statically parsed arXiv evidence bundle",
            search_result={"batch_id": batch_id, "batch_path": batch_relative},
        )
    else:
        status = "no_candidates" if audit_input["status"] == "no_candidates" else "rejected"
        batch_id = None
        batch_sha = None
        batch_relative = None
        write_scheduled_search_outcome(
            project_root,
            run_date=run_date,
            as_of=f"{run_date}T06:00:00+03:00",
            status="deferred",
            reason="Aegis approved no candidate for offline editorial use",
        )

    bundle: dict[str, Any] = {
        "schema_version": "1.0.0",
        "edition_date": run_date,
        "approved_at": approved_at,
        "status": status,
        "audit_input_sha256": audit_input["input_sha256"],
        "audit_verdict_sha256": canonical_json_hash(verdict),
        "batch_id": batch_id,
        "batch_sha256": batch_sha,
        "batch_path": batch_relative,
        "candidates": sealed_candidates,
    }
    bundle["bundle_sha256"] = canonical_json_hash(bundle)
    validate_records(
        [bundle],
        project_root / "schemas" / APPROVED_BUNDLE_SCHEMA,
        "security-approved editorial bundle",
    )
    return _write_immutable(project_root / approved_bundle_path(run_date), bundle)


def load_approved_bundle(project_root: Path, run_date: str) -> dict[str, Any]:
    path = _safe_project_file(
        project_root, approved_bundle_path(run_date), "security-approved bundle"
    )
    bundle = _read_json(path, "security-approved bundle")
    validate_records(
        [bundle],
        project_root / "schemas" / APPROVED_BUNDLE_SCHEMA,
        "security-approved editorial bundle",
    )
    if (
        bundle.get("edition_date") != run_date
        or bundle.get("bundle_sha256")
        != canonical_json_hash(
            {key: value for key, value in bundle.items() if key != "bundle_sha256"}
        )
    ):
        raise PublicationError("security-approved bundle identity hash does not match")
    return bundle


def approved_evidence_by_candidate(
    project_root: Path,
    run_date: str,
    candidates: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str], Mapping[str, Any]]:
    bundle = load_approved_bundle(project_root, run_date)
    if bundle["status"] != "ready":
        raise PublicationError("automatic editorial input was not approved by Aegis")
    approved = {
        (
            row["candidate"]["id"],
            row["candidate"]["candidate_sha256"],
        ): row["evidence"]
        for row in bundle["candidates"]
    }
    requested = {
        (str(row.get("id")), str(row.get("candidate_sha256"))) for row in candidates
    }
    if not requested or not requested.issubset(approved):
        raise PublicationError("automatic package selected a candidate not approved by Aegis")
    return {identity: approved[identity] for identity in requested}


def stage_sol_workspace(
    project_root: Path,
    *,
    run_date: str,
    sol_workspace: Path,
) -> Path:
    """Copy only approved data, schemas, and editorial instructions to Sol."""

    bundle = load_approved_bundle(project_root, run_date)
    if bundle["status"] != "ready":
        raise PublicationError("Sol staging requires a ready Aegis bundle")
    destination = sol_workspace / "inbox" / run_date
    if destination.exists():
        raise PublicationError("Sol inbox already exists; refusing to replace staged input")
    destination.mkdir(parents=True)
    (destination / "bundle.json").write_bytes(canonical_json_bytes(bundle) + b"\n")
    extracts = destination / "extracts"
    extracts.mkdir()
    for row in bundle["candidates"]:
        source = _safe_project_file(
            project_root, row["extract_path"], "approved page extracts"
        )
        shutil.copyfile(source, extracts / source.name)
    shutil.copytree(project_root / "schemas", destination / "schemas")
    shutil.copyfile(
        project_root / "prompts" / "automatic-editor-offline.md",
        destination / "EDITORIAL-INSTRUCTIONS.md",
    )
    return destination


def import_sol_package(
    project_root: Path,
    *,
    run_date: str,
    sol_workspace: Path,
) -> Path:
    """Validate and immutably import Sol's offline package for the publisher."""

    package_path = sol_workspace / "outbox" / f"{run_date}.json"
    package = _read_json(package_path, "offline Sol package", maximum=2 * 1024 * 1024)
    validate_records(
        [package],
        project_root / "schemas" / "automatic-pulse-package.schema.json",
        "offline Sol package",
    )
    if package.get("date") != run_date:
        raise PublicationError("offline Sol package date does not match")
    bundle = load_approved_bundle(project_root, run_date)
    if bundle["status"] != "ready" or package.get("candidates") is None:
        raise PublicationError("offline Sol package has no ready Aegis authority")
    approved = {
        (row["candidate"]["id"], row["candidate"]["candidate_sha256"])
        for row in bundle["candidates"]
    }
    selected = {
        (row["candidate_id"], row["candidate_sha256"])
        for row in package["candidates"]
    }
    if (
        not selected
        or not selected.issubset(approved)
        or any(row["batch_id"] != bundle["batch_id"] for row in package["candidates"])
    ):
        raise PublicationError("offline Sol package escaped the Aegis-approved candidates")
    return _write_immutable(
        project_root / "data" / "automatic" / "packages" / f"{run_date}.json",
        package,
    )
