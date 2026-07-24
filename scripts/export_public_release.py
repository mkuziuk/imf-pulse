#!/usr/bin/env python3
"""Export a sealed, public-safe view of the current IMF Pulse release.

The export deliberately contains no snapshots, static extracts, run logs, source
documents, or executable artifact generators.  It is suitable as the only
research-content input to a public Vite build.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from research_pipeline.artifacts import verify_bound_publication
from research_pipeline.hashing import canonical_json_hash
from research_pipeline.release import (
    _accepted_publications,
    _publication_history_record,
    _validate_accepted_publications_digest,
    _validate_pointer_history_summaries,
)
from research_pipeline.validation import read_json, validate_release_directory


PUBLIC_RELEASE_KIND = "imf-pulse-public-release"
PUBLIC_RELEASE_SCHEMA_VERSION = 1
PUBLIC_APPROVAL_DATE = "2026-07-23"
PUBLIC_APPROVAL_ACTOR = "project_owner"
KNOWLEDGE_NAMES = (
    "sources.jsonl",
    "claims.jsonl",
    "methods.jsonl",
    "experiments.jsonl",
    "relationships.jsonl",
)
TEXT_SUFFIXES = {".json", ".jsonl", ".md", ".csv", ".svg", ".txt", ".yaml", ".yml"}
ARTIFACT_SUFFIXES = {".json", ".csv", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".pdf"}
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
RELEASE_ID_RE = re.compile(r"^release-[a-f0-9]{20}$")
PULSE_RE = re.compile(r"^content/pulses/(\d{4}-\d{2}-\d{2})\.md$")
PUBLIC_PULSE_RE = re.compile(r"^pulses/(\d{4}-\d{2}-\d{2})\.md$")
PUBLIC_ARTIFACT_RE = re.compile(
    r"^artifacts/\d{4}-\d{2}-\d{2}/[a-zA-Z0-9._-]+(?:/[a-zA-Z0-9._-]+)*$"
)
HOME_PATH_PATTERNS = (
    re.compile(r"/(?:Users|home)/[^/\s\"']+/"),
    re.compile(r"[A-Za-z]:\\Users\\[^\\\s\"']+\\"),
)
CREDENTIAL_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|client[_-]?secret|access[_-]?token|password|passwd)"
        r"\s*[:=]\s*[\"']?(?!none\b|null\b|false\b|true\b|unknown\b|redacted\b|example\b)"
        r"[^\s\"']{8,}"
    ),
)
RAW_CONTENT_KEYS = {
    "quote",
    "excerpt",
    "raw_source",
    "raw_content",
    "source_text",
    "extracted_text",
    "snapshot_id",
    "snapshot_path",
    "extract_semantic_sha256",
    "processing_fingerprint",
}
RAW_CONTENT_FIELD_RE = re.compile(
    r'"(?:snapshot_id|snapshot_path|extract_semantic_sha256|processing_fingerprint|quote|excerpt|'
    r'raw_source|raw_content|source_text|extracted_text)"\s*:'
)
SOURCE_PUBLIC_FIELDS = {
    "schema_version",
    "id",
    "title",
    "authors",
    "date",
    "source_type",
    "authority_level",
    "publication_status",
    "topics",
    "rights",
    "rights_status",
    "content_sha256",
    "content_hash",
    "content_size_bytes",
    "retrieved_at",
    "last_processed_at",
    "extractor",
    "status",
    "limitations",
    "path",
    "relative_path",
    "url",
    "location",
    "version_history",
}
CURRENT_PUBLIC_FIELDS = {
    "schema_version",
    "release_id",
    "updated_at",
    "published_at",
    "last_checked_at",
    "status",
    "pulse",
    "artifact_manifests",
    "latest_accepted_pulse",
    "accepted_pulses",
    "accepted_artifact_manifests",
    "latest_accepted_artifact_manifests",
}


class PublicReleaseError(RuntimeError):
    """Raised when a public export is unsafe or inconsistent."""


def _strict_pairs(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise PublicReleaseError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _strict_json(payload: bytes, label: str) -> Any:
    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_strict_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                PublicReleaseError(f"non-finite JSON number in {label}: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicReleaseError(f"invalid UTF-8 JSON in {label}: {exc}") from exc


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _jsonl_bytes(records: Iterable[Mapping[str, Any]]) -> bytes:
    return b"".join(_json_bytes(record) for record in records)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _safe_relative(value: str, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise PublicReleaseError(f"{label} is not a safe relative path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise PublicReleaseError(f"{label} is not a safe relative path")
    return pure


def _strict_project_child(project_root: Path, value: str, *, must_exist: bool) -> Path:
    pure = _safe_relative(value, "public release directory")
    if len(pure.parts) != 1 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", pure.name):
        raise PublicReleaseError("public release directory must be a direct project child")
    root = project_root.resolve(strict=True)
    path = root / pure.name
    if must_exist:
        try:
            node = os.lstat(path)
        except FileNotFoundError as exc:
            raise PublicReleaseError(f"public release directory does not exist: {pure}") from exc
        if not stat.S_ISDIR(node.st_mode) or stat.S_ISLNK(node.st_mode):
            raise PublicReleaseError("public release directory must be a non-symlink directory")
        if path.resolve(strict=True).parent != root:
            raise PublicReleaseError("public release directory escaped the project root")
    elif os.path.lexists(path):
        node = os.lstat(path)
        if not stat.S_ISDIR(node.st_mode) or stat.S_ISLNK(node.st_mode):
            raise PublicReleaseError("existing public release path is not a safe directory")
        if path.resolve(strict=True).parent != root:
            raise PublicReleaseError("public release directory escaped the project root")
    return path


def _read_stable(path: Path, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PublicReleaseError(f"cannot open {label}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise PublicReleaseError(f"{label} is not a regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        payload = b"".join(chunks)
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or len(payload) != before.st_size
        ):
            raise PublicReleaseError(f"{label} changed while it was read")
        return payload
    finally:
        os.close(descriptor)


def _write_new(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o644)
    try:
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _scan_public_bytes(relative: str, payload: bytes) -> None:
    suffix = PurePosixPath(relative).suffix.lower()
    if suffix not in TEXT_SUFFIXES:
        return
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PublicReleaseError(f"public text file is not UTF-8: {relative}") from exc
    for pattern in HOME_PATH_PATTERNS:
        if pattern.search(text):
            raise PublicReleaseError(f"absolute home path found in public file: {relative}")
    for pattern in CREDENTIAL_PATTERNS:
        if pattern.search(text):
            raise PublicReleaseError(f"credential-like value found in public file: {relative}")
    if relative.startswith("knowledge/") and RAW_CONTENT_FIELD_RE.search(text):
        raise PublicReleaseError(f"private/raw field found in public knowledge: {relative}")


def _allowed_public_path(relative: str) -> bool:
    if relative == "current.json" or relative in {
        f"knowledge/{name}" for name in KNOWLEDGE_NAMES
    }:
        return True
    if PUBLIC_PULSE_RE.fullmatch(relative):
        return True
    return bool(
        PUBLIC_ARTIFACT_RE.fullmatch(relative)
        and PurePosixPath(relative).suffix.lower() in ARTIFACT_SUFFIXES
    )


def _walk_regular_files(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    root_node = os.lstat(root)
    if not stat.S_ISDIR(root_node.st_mode) or stat.S_ISLNK(root_node.st_mode):
        raise PublicReleaseError("public release root is unsafe")
    for directory, names, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        directory_node = os.lstat(directory_path)
        if not stat.S_ISDIR(directory_node.st_mode) or stat.S_ISLNK(directory_node.st_mode):
            raise PublicReleaseError(f"public release contains unsafe directory: {directory_path}")
        for name in names:
            node = os.lstat(directory_path / name)
            if not stat.S_ISDIR(node.st_mode) or stat.S_ISLNK(node.st_mode):
                raise PublicReleaseError(f"public release contains forbidden node: {name}")
        for name in filenames:
            path = directory_path / name
            node = os.lstat(path)
            relative = path.relative_to(root).as_posix()
            if not stat.S_ISREG(node.st_mode) or stat.S_ISLNK(node.st_mode):
                raise PublicReleaseError(f"public release contains forbidden node: {relative}")
            files[relative] = _read_stable(path, relative)
    return files


def _is_project_generated_artifact(manifest: Mapping[str, Any]) -> bool:
    rights = manifest.get("rights")
    status = rights.get("status") if isinstance(rights, Mapping) else None
    artifact_type = manifest.get("artifact_type") or manifest.get("artifact_class")
    return (
        isinstance(status, str)
        and status.startswith("project_generated")
        and artifact_type in {"scientific_chart", "diagram", "generated_image", "conceptual_illustration"}
    )


def _artifact_is_cleared(manifest: Mapping[str, Any]) -> bool:
    rights = manifest.get("rights")
    if not isinstance(rights, Mapping) or rights.get("may_publish_publicly") is not True:
        return False
    status = str(rights.get("status", "")).lower().replace("-", "_").replace(" ", "_")
    return status in {
        "approved",
        "cleared",
        "public_domain",
        "cc_by",
        "cc_by_sa",
        "cc0",
        "not_applicable",
        "project_generated",
        "project_generated_scientific_chart",
        "project_generated_diagram",
        "project_generated_illustration",
    }


def _approve_project_generated_artifact(manifest: Mapping[str, Any]) -> dict[str, Any]:
    exported = json.loads(json.dumps(manifest, ensure_ascii=False, allow_nan=False))
    if isinstance(exported.get("artifacts"), list):
        approved_rows: list[dict[str, Any]] = []
        for row in exported["artifacts"]:
            if not isinstance(row, Mapping):
                raise PublicReleaseError("artifact collection contains a non-object row")
            approved_rows.append(_approve_project_generated_artifact(row))
        exported["artifacts"] = approved_rows
        return exported
    if not _is_project_generated_artifact(exported):
        if _artifact_is_cleared(exported):
            return exported
        raise PublicReleaseError(
            f"artifact is not cleared and is not project-generated: "
            f"{exported.get('artifact_id') or exported.get('id')}"
        )
    # The public export is the owner-approval boundary. Stamp every generated
    # artifact, including one whose private manifest already permits public
    # use, so the independently audited bundle carries uniform proof of that
    # approval rather than relying on an unstamped source flag.
    rights = dict(exported.get("rights", {}))
    rights.update(
        {
            "may_publish_publicly": True,
            "public_deployment_requires_owner_approval": False,
            "public_deployment_approved_by": PUBLIC_APPROVAL_ACTOR,
            "public_deployment_approved_on": PUBLIC_APPROVAL_DATE,
            "public_deployment_approval_scope": "project-generated artifact public deployment",
        }
    )
    exported["rights"] = rights
    if not _artifact_is_cleared(exported):
        raise PublicReleaseError("project-generated artifact approval did not produce cleared rights")
    return exported


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        key: _sanitize_value(item)
        for key, item in value.items()
        if key not in RAW_CONTENT_KEYS
    }


def _sanitize_source(record: Mapping[str, Any]) -> dict[str, Any]:
    sanitized = {
        key: _sanitize_value(value)
        for key, value in record.items()
        if key in SOURCE_PUBLIC_FIELDS
    }
    if not any(key in sanitized for key in ("path", "relative_path", "url")):
        raise PublicReleaseError(f"source has no public-safe locator: {record.get('id')}")
    return sanitized


def _sanitize_knowledge(
    parsed: Mapping[str, list[dict[str, Any]]]
) -> dict[str, bytes]:
    output: dict[str, bytes] = {}
    for name in KNOWLEDGE_NAMES:
        records = parsed.get(name)
        if not isinstance(records, list):
            raise PublicReleaseError(f"validated release is missing {name}")
        if name == "sources.jsonl":
            sanitized = [_sanitize_source(record) for record in records]
        else:
            sanitized = [_sanitize_value(record) for record in records]
        output[f"knowledge/{name}"] = _jsonl_bytes(sanitized)
    return output


def _url_to_public_relative(value: str, *, manifest: bool = False) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("/artifacts/")
        or value.startswith("//")
        or any(character in value for character in "\\?#%")
    ):
        raise PublicReleaseError(f"unsafe artifact URL: {value!r}")
    relative = value.removeprefix("/")
    if not _allowed_public_path(relative):
        raise PublicReleaseError(f"artifact URL is outside the public allowlist: {value}")
    if manifest and not relative.endswith("/manifest.json"):
        raise PublicReleaseError(f"artifact manifest URL must end in manifest.json: {value}")
    return relative


def _read_project_bound_file(project_root: Path, value: str, expected: str) -> bytes:
    pure = _safe_relative(value, "bound publication path")
    path = project_root.joinpath(*pure.parts)
    if path.resolve(strict=True).is_relative_to(project_root) is False:
        raise PublicReleaseError("bound publication path escaped the project")
    payload = _read_stable(path, value)
    if not isinstance(expected, str) or not SHA256_RE.fullmatch(expected) or _sha256(payload) != expected:
        raise PublicReleaseError(f"bound publication hash mismatch: {value}")
    return payload


def _validated_current(
    project_root: Path,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    current_path = project_root / "data" / "current.json"
    node = os.lstat(current_path)
    if not stat.S_ISREG(node.st_mode) or stat.S_ISLNK(node.st_mode):
        raise PublicReleaseError("data/current.json is not a regular file")
    current = read_json(current_path)
    if not isinstance(current, dict):
        raise PublicReleaseError("data/current.json must contain an object")
    release_id = current.get("release_id")
    if not isinstance(release_id, str) or not RELEASE_ID_RE.fullmatch(release_id):
        raise PublicReleaseError("current release id is unsafe")
    if current.get("release_path") != f"data/releases/{release_id}":
        raise PublicReleaseError("current release path is inconsistent")
    release_directory = project_root / "data" / "releases" / release_id
    parsed = validate_release_directory(release_directory, project_root / "schemas")
    release_manifest = read_json(release_directory / "release.json")
    if not isinstance(release_manifest, dict):
        raise PublicReleaseError("current release manifest is invalid")
    if current.get("release_sha256") != canonical_json_hash(release_manifest):
        raise PublicReleaseError("current release pointer hash is invalid")
    accepted = _accepted_publications(current)
    _validate_pointer_history_summaries(current, accepted)
    _validate_accepted_publications_digest(current, release_manifest, accepted)
    for item in accepted:
        accepted_directory = project_root / "data" / "releases" / item["release_id"]
        validate_release_directory(accepted_directory, project_root / "schemas")
        accepted_manifest = read_json(accepted_directory / "release.json")
        metadata = accepted_manifest.get("publication") if isinstance(accepted_manifest, dict) else None
        if not isinstance(metadata, Mapping):
            raise PublicReleaseError(
                f"accepted publication has no immutable binding: {item['release_id']}"
            )
        verify_bound_publication(accepted_directory, project_root / "schemas", metadata)
        if _publication_history_record(item["release_id"], metadata) != item:
            raise PublicReleaseError(
                f"accepted publication history does not match its binding: {item['release_id']}"
            )
    if not accepted:
        raise PublicReleaseError("current release has no accepted public pulse")
    return current, parsed, accepted


def _public_current(current: Mapping[str, Any], accepted: list[dict[str, Any]]) -> dict[str, Any]:
    accepted_pulses = [item["pulse"] for item in accepted]
    artifact_urls: list[str] = []
    for publication in accepted:
        for manifest in publication.get("artifact_manifests", []):
            if manifest["url"] not in artifact_urls:
                artifact_urls.append(manifest["url"])
    latest = accepted[-1]
    latest_artifacts = [item["url"] for item in latest.get("artifact_manifests", [])]
    status = current.get("status")
    if status not in {"published", "processed_no_pulse", "unchanged"}:
        raise PublicReleaseError("current checkpoint is not accepted for public export")
    selected_pulse = current.get("pulse") if status == "published" else None
    selected_artifacts = current.get("artifact_manifests", []) if status == "published" else []
    summary = {
        "schema_version": 1,
        "release_id": current["release_id"],
        "status": status,
        "pulse": selected_pulse,
        "artifact_manifests": selected_artifacts,
        "latest_accepted_pulse": latest["pulse"],
        "accepted_pulses": accepted_pulses,
        "accepted_artifact_manifests": artifact_urls,
        "latest_accepted_artifact_manifests": latest_artifacts,
    }
    for field in ("updated_at", "published_at", "last_checked_at"):
        if isinstance(current.get(field), str):
            summary[field] = current[field]
    if set(summary) - CURRENT_PUBLIC_FIELDS:
        raise AssertionError("public current summary contains an unexpected field")
    return summary


def _checkpoint_timestamp(current: Mapping[str, Any]) -> str:
    """Return a deterministic timestamp already committed by the checkpoint."""

    for field in ("last_checked_at", "updated_at", "published_at"):
        value = current.get(field)
        if isinstance(value, str) and re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", value
        ):
            return value
    raise PublicReleaseError("current checkpoint has no deterministic UTC timestamp")


def _publication_payloads(
    project_root: Path, accepted: list[dict[str, Any]]
) -> dict[str, bytes]:
    payloads: dict[str, bytes] = {}
    for publication in accepted:
        pulse = publication.get("pulse")
        match = PULSE_RE.fullmatch(str(pulse))
        if not match:
            raise PublicReleaseError(f"accepted pulse path is unsafe: {pulse}")
        pulse_payload = _read_project_bound_file(
            project_root, publication["bound_pulse"], publication["pulse_sha256"]
        )
        public_pulse = f"pulses/{match.group(1)}.md"
        previous = payloads.get(public_pulse)
        if previous is not None and previous != pulse_payload:
            raise PublicReleaseError(f"two accepted pulses claim the same date: {public_pulse}")
        payloads[public_pulse] = pulse_payload

        for manifest_record in publication.get("artifact_manifests", []):
            manifest_relative = _url_to_public_relative(manifest_record["url"], manifest=True)
            raw_manifest = _read_project_bound_file(
                project_root, manifest_record["bound_path"], manifest_record["sha256"]
            )
            parsed_manifest = _strict_json(raw_manifest, manifest_relative)
            if not isinstance(parsed_manifest, dict):
                raise PublicReleaseError(f"artifact manifest is not an object: {manifest_relative}")
            approved_manifest = _approve_project_generated_artifact(parsed_manifest)
            approved_payload = _json_bytes(approved_manifest)
            existing = payloads.get(manifest_relative)
            if existing is not None and existing != approved_payload:
                raise PublicReleaseError(f"artifact manifest URL was reused: {manifest_relative}")
            payloads[manifest_relative] = approved_payload
            for file_record in manifest_record.get("files", []):
                relative = _url_to_public_relative(file_record["url"])
                artifact_payload = _read_project_bound_file(
                    project_root, file_record["bound_path"], file_record["sha256"]
                )
                if len(artifact_payload) != file_record["bytes"]:
                    raise PublicReleaseError(f"artifact byte count is invalid: {relative}")
                previous_file = payloads.get(relative)
                if previous_file is not None and previous_file != artifact_payload:
                    raise PublicReleaseError(f"artifact URL was reused with different bytes: {relative}")
                payloads[relative] = artifact_payload
    return payloads


def _artifact_references(manifest: Mapping[str, Any], manifest_relative: str) -> set[str]:
    directory = PurePosixPath(manifest_relative).parent
    references = {manifest_relative}

    def resolve_url(value: Any) -> str | None:
        if not isinstance(value, str) or not value:
            return None
        if value.startswith("/"):
            return _url_to_public_relative(value)
        pure = _safe_relative(value.removeprefix("./"), "relative artifact URL")
        resolved = (directory / pure).as_posix()
        if not _allowed_public_path(resolved) or not resolved.startswith(f"{directory.as_posix()}/"):
            raise PublicReleaseError(f"artifact file escaped its manifest directory: {value}")
        return resolved

    rows = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), list) else [manifest]
    for raw_row in rows:
        if not isinstance(raw_row, Mapping):
            raise PublicReleaseError(f"artifact row is invalid: {manifest_relative}")
        if not _artifact_is_cleared(raw_row):
            raise PublicReleaseError(f"artifact manifest is not publicly cleared: {manifest_relative}")
        for field in ("stable_url", "spec_url", "data_url"):
            resolved = resolve_url(raw_row.get(field))
            if resolved:
                references.add(resolved)
        for collection_name in ("files", "downloads"):
            collection = raw_row.get(collection_name, [])
            if collection is None:
                continue
            if not isinstance(collection, list):
                raise PublicReleaseError(f"artifact {collection_name} is invalid: {manifest_relative}")
            for item in collection:
                if not isinstance(item, Mapping):
                    raise PublicReleaseError(f"artifact file record is invalid: {manifest_relative}")
                resolved = resolve_url(item.get("url") or item.get("path"))
                if resolved:
                    references.add(resolved)
    return references


def audit_public_release(directory: Path) -> dict[str, Any]:
    """Validate a public release directory and return its immutable summary."""

    files = _walk_regular_files(directory)
    manifest_payload = files.pop("manifest.json", None)
    if manifest_payload is None:
        raise PublicReleaseError("public release is missing manifest.json")
    manifest = _strict_json(manifest_payload, "manifest.json")
    if not isinstance(manifest, dict):
        raise PublicReleaseError("public release manifest must be an object")
    expected_manifest_fields = {
        "schema_version",
        "kind",
        "public_release_id",
        "source_release_id",
        "created_at",
        "approval",
        "file_count",
        "content_sha256",
        "files",
    }
    if set(manifest) != expected_manifest_fields:
        raise PublicReleaseError("public release manifest has an unexpected field set")
    if manifest.get("schema_version") != PUBLIC_RELEASE_SCHEMA_VERSION or manifest.get("kind") != PUBLIC_RELEASE_KIND:
        raise PublicReleaseError("public release manifest identity is invalid")
    source_release_id = manifest.get("source_release_id")
    if not isinstance(source_release_id, str) or not RELEASE_ID_RE.fullmatch(source_release_id):
        raise PublicReleaseError("public release source release id is invalid")
    listed = manifest.get("files")
    if not isinstance(listed, dict) or any(
        not isinstance(path, str) or not isinstance(digest, str) or not SHA256_RE.fullmatch(digest)
        for path, digest in listed.items()
    ):
        raise PublicReleaseError("public release file map is invalid")
    if set(listed) != set(files):
        raise PublicReleaseError(
            f"public release file map mismatch; missing={sorted(set(listed) - set(files))}, "
            f"extra={sorted(set(files) - set(listed))}"
        )
    required = {"current.json", *(f"knowledge/{name}" for name in KNOWLEDGE_NAMES)}
    if not required.issubset(files):
        raise PublicReleaseError("public release is missing required current/knowledge files")
    if not any(PUBLIC_PULSE_RE.fullmatch(path) for path in files):
        raise PublicReleaseError("public release contains no dated pulse")
    for relative, payload in files.items():
        _safe_relative(relative, "public release file")
        if not _allowed_public_path(relative):
            raise PublicReleaseError(f"public release contains a path outside its allowlist: {relative}")
        if _sha256(payload) != listed[relative]:
            raise PublicReleaseError(f"public release file hash mismatch: {relative}")
        _scan_public_bytes(relative, payload)
    content_sha256 = canonical_json_hash(dict(sorted(listed.items())))
    if manifest.get("content_sha256") != content_sha256:
        raise PublicReleaseError("public release aggregate content hash is invalid")
    expected_id = f"public-{content_sha256[:20]}"
    if manifest.get("public_release_id") != expected_id or manifest.get("file_count") != len(files):
        raise PublicReleaseError("public release id/count is invalid")
    approval = manifest.get("approval")
    if approval != {
        "actor": PUBLIC_APPROVAL_ACTOR,
        "approved_on": PUBLIC_APPROVAL_DATE,
        "scope": "project-generated artifact public deployment",
    }:
        raise PublicReleaseError("public release approval metadata is invalid")

    current = _strict_json(files["current.json"], "current.json")
    if not isinstance(current, dict) or set(current) - CURRENT_PUBLIC_FIELDS:
        raise PublicReleaseError("public current summary contains private or unexpected fields")
    if current.get("release_id") != source_release_id:
        raise PublicReleaseError("public current summary does not match source release")
    if manifest.get("created_at") != _checkpoint_timestamp(current):
        raise PublicReleaseError(
            "public release timestamp is not bound to the committed checkpoint"
        )
    pulses = current.get("accepted_pulses")
    manifests = current.get("accepted_artifact_manifests")
    if not isinstance(pulses, list) or not pulses or not isinstance(manifests, list):
        raise PublicReleaseError("public current summary has invalid accepted content")
    expected_pulse_files = {
        f"pulses/{match.group(1)}.md"
        for value in pulses
        if isinstance(value, str) and (match := PULSE_RE.fullmatch(value))
    }
    actual_pulse_files = {path for path in files if PUBLIC_PULSE_RE.fullmatch(path)}
    if len(expected_pulse_files) != len(pulses) or expected_pulse_files != actual_pulse_files:
        raise PublicReleaseError("public current pulse history does not match exported pulses")
    expected_manifest_files = {
        _url_to_public_relative(value, manifest=True)
        for value in manifests
        if isinstance(value, str)
    }
    actual_manifest_files = {
        path for path in files if path.startswith("artifacts/") and path.endswith("/manifest.json")
    }
    if len(expected_manifest_files) != len(manifests) or expected_manifest_files != actual_manifest_files:
        raise PublicReleaseError("public current artifact history does not match exported manifests")

    referenced_artifacts: set[str] = set()
    for manifest_relative in sorted(actual_manifest_files):
        artifact = _strict_json(files[manifest_relative], manifest_relative)
        if not isinstance(artifact, dict):
            raise PublicReleaseError(f"artifact manifest is not an object: {manifest_relative}")
        referenced_artifacts.update(_artifact_references(artifact, manifest_relative))
    actual_artifacts = {path for path in files if path.startswith("artifacts/")}
    if referenced_artifacts != actual_artifacts:
        raise PublicReleaseError(
            f"public artifact set mismatch; unreferenced={sorted(actual_artifacts - referenced_artifacts)}, "
            f"missing={sorted(referenced_artifacts - actual_artifacts)}"
        )
    return {
        "public_release_id": expected_id,
        "source_release_id": source_release_id,
        "content_sha256": content_sha256,
        "file_count": len(files),
        "pulse_count": len(actual_pulse_files),
        "artifact_manifest_count": len(actual_manifest_files),
    }


def _fsync_tree(root: Path) -> None:
    for directory, _, filenames in os.walk(root, topdown=False):
        for filename in filenames:
            descriptor = os.open(Path(directory) / filename, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _atomic_exchange(parent: Path, staging_name: str, destination_name: str) -> None:
    """Atomically exchange two sibling directories on Darwin or Linux."""

    libc = ctypes.CDLL(None, use_errno=True)
    parent_descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        source = os.fsencode(staging_name)
        destination = os.fsencode(destination_name)
        if sys.platform == "darwin":
            try:
                rename = libc.renameatx_np
            except AttributeError as exc:
                raise PublicReleaseError("atomic public-release exchange is unavailable") from exc
            rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            rename.restype = ctypes.c_int
            result = rename(parent_descriptor, source, parent_descriptor, destination, 0x00000002)
        elif sys.platform.startswith("linux"):
            try:
                rename = libc.renameat2
            except AttributeError as exc:
                raise PublicReleaseError("atomic public-release exchange is unavailable") from exc
            rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            rename.restype = ctypes.c_int
            result = rename(parent_descriptor, source, parent_descriptor, destination, 0x00000002)
        else:
            raise PublicReleaseError("atomic public-release exchange is unsupported on this platform")
        if result != 0:
            error = ctypes.get_errno()
            raise PublicReleaseError(f"cannot atomically exchange public release: {os.strerror(error)}")
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)


def _install_atomic(staging: Path, destination: Path) -> None:
    parent = destination.parent
    if not os.path.lexists(destination):
        os.rename(staging, destination)
        descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return
    destination_node = os.lstat(destination)
    if not stat.S_ISDIR(destination_node.st_mode) or stat.S_ISLNK(destination_node.st_mode):
        raise PublicReleaseError("public release destination became unsafe")
    _atomic_exchange(parent, staging.name, destination.name)
    old_node = os.lstat(staging)
    if not stat.S_ISDIR(old_node.st_mode) or stat.S_ISLNK(old_node.st_mode):
        raise PublicReleaseError("old public release became unsafe after atomic exchange")
    if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
        raise PublicReleaseError("safe cleanup of the old public release is unavailable")
    shutil.rmtree(staging)


def export_public_release(project_root: Path, output: str) -> dict[str, Any]:
    project_root = project_root.resolve(strict=True)
    destination = _strict_project_child(project_root, output, must_exist=False)
    current, parsed, accepted = _validated_current(project_root)
    payloads = _sanitize_knowledge(parsed)
    payloads["current.json"] = _json_bytes(_public_current(current, accepted))
    payloads.update(_publication_payloads(project_root, accepted))
    for relative, payload in payloads.items():
        if not _allowed_public_path(relative):
            raise PublicReleaseError(f"export attempted a forbidden public path: {relative}")
        _scan_public_bytes(relative, payload)
    file_map = {relative: _sha256(payload) for relative, payload in sorted(payloads.items())}
    content_sha256 = canonical_json_hash(file_map)
    manifest = {
        "schema_version": PUBLIC_RELEASE_SCHEMA_VERSION,
        "kind": PUBLIC_RELEASE_KIND,
        "public_release_id": f"public-{content_sha256[:20]}",
        "source_release_id": current["release_id"],
        "created_at": _checkpoint_timestamp(current),
        "approval": {
            "actor": PUBLIC_APPROVAL_ACTOR,
            "approved_on": PUBLIC_APPROVAL_DATE,
            "scope": "project-generated artifact public deployment",
        },
        "file_count": len(file_map),
        "content_sha256": content_sha256,
        "files": file_map,
    }
    staging = project_root / f".{destination.name}-staging-{uuid.uuid4().hex}"
    staging.mkdir(mode=0o755)
    try:
        for relative, payload in sorted(payloads.items()):
            _write_new(staging.joinpath(*PurePosixPath(relative).parts), payload)
        _write_new(staging / "manifest.json", _json_bytes(manifest))
        _fsync_tree(staging)
        summary = audit_public_release(staging)
        _install_atomic(staging, destination)
        installed = audit_public_release(destination)
        if installed != summary:
            raise PublicReleaseError("installed public release differs from audited staging")
        return {**installed, "directory": destination.relative_to(project_root).as_posix()}
    finally:
        if os.path.lexists(staging):
            node = os.lstat(staging)
            if stat.S_ISDIR(node.st_mode) and not stat.S_ISLNK(node.st_mode):
                shutil.rmtree(staging)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default="public-release",
        help="direct project-child directory to replace atomically (default: public-release)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    project_root = Path(__file__).resolve().parents[1]
    try:
        summary = export_public_release(project_root, arguments.output)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps({"status": "exported", **summary}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
