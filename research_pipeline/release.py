"""Build immutable release candidates and publish them through one pointer."""

from __future__ import annotations

import json
import hashlib
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from importlib.metadata import PackageNotFoundError, version as package_version
from typing import Any, Callable, Iterable, Mapping, Sequence

from .artifacts import (
    bind_publication_inputs,
    verify_bound_publication,
    verify_source_publication_inputs,
)
from .config import config_fingerprint, resolve_live_root
from .errors import PublicationError, ValidationError
from .extractors import ExtractionResult, extract_source
from .hashing import canonical_json_bytes, canonical_json_hash, sha256_file
from .models import PipelineConfig, SnapshotManifest, SourceConfig
from .snapshot import atomic_write_json, load_current_snapshot
from .paths import (
    ensure_directory_under_root,
    open_child_directory,
    open_directory_under_root,
    open_regular_file_under_root,
)
from .hashing import copy_exact_bytes_from_descriptor
from .validation import (
    read_json,
    read_jsonl,
    strict_json_loads,
    validate_records,
    validate_release_directory,
)


@dataclass(frozen=True)
class ReleaseBuildResult:
    release_id: str
    release_directory: Path
    created: bool
    status: str
    semantic_changed: bool


@dataclass(frozen=True)
class PublishResult:
    release_id: str
    run_id: str
    status: str
    pointer_changed: bool


GateRunner = Callable[[Sequence[str], Path, Mapping[str, str]], None]
RELEASE_ID_PATTERN = re.compile(r"^release-[0-9a-f]{20}$")
SITE_BUILD_NAME_PATTERN = re.compile(r"^site-[0-9a-f]{64}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(canonical_json_bytes(value) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())


def _atomic_write_release_json(
    release_directory: Path,
    expected_identity: tuple[int, int],
    value: Mapping[str, Any],
) -> None:
    """Replace release.json relative to its held, identity-checked directory."""

    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        directory_descriptor = os.open(release_directory, directory_flags)
    except OSError as exc:
        raise PublicationError("release directory changed before manifest sealing") from exc
    temporary_name = f".release.json.{uuid.uuid4().hex}"
    file_descriptor: int | None = None
    try:
        opened = os.fstat(directory_descriptor)
        if (opened.st_dev, opened.st_ino) != expected_identity:
            raise PublicationError("release directory changed before manifest sealing")
        file_descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_descriptor,
        )
        payload = canonical_json_bytes(value) + b"\n"
        offset = 0
        while offset < len(payload):
            offset += os.write(file_descriptor, payload[offset:])
        os.fsync(file_descriptor)
        os.close(file_descriptor)
        file_descriptor = None
        os.replace(
            temporary_name,
            "release.json",
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        os.fsync(directory_descriptor)
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory_descriptor)
        except FileNotFoundError:
            pass
        os.close(directory_descriptor)


def _write_jsonl(path: Path, records: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        for record in records:
            handle.write(canonical_json_bytes(record) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())


def _source_record(
    source: SourceConfig,
    snapshot: SnapshotManifest,
    entry: Any,
    extraction: ExtractionResult,
    processed_at: str,
    previous: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    rights = dict(source.rights)
    history: list[dict[str, Any]] = []
    if previous is not None:
        for item in previous.get("version_history", []):
            if isinstance(item, Mapping):
                history.append(dict(item))
        previous_hash = previous.get("content_sha256") or previous.get("content_hash")
        if isinstance(previous_hash, str) and previous_hash != entry.sha256:
            history.append(
                {
                    "content_sha256": previous_hash,
                    "first_seen_at": previous.get("retrieved_at", previous.get("last_processed_at")),
                    "last_seen_at": previous.get("last_processed_at", previous.get("retrieved_at")),
                }
            )
    history_by_hash = {
        item["content_sha256"]: item
        for item in history
        if isinstance(item.get("content_sha256"), str)
    }
    record = {
        "schema_version": "1.0.0",
        "id": source.id,
        "title": source.title,
        "authors": list(source.authors),
        "date": source.date,
        "source_type": source.source_type,
        "authority_level": source.authority_level,
        "publication_status": source.publication_status,
        "topics": list(source.topics),
        "path": source.path,
        "location": f"repo://{source.root}/{source.path}",
        "rights": rights,
        "rights_status": rights.get("reuse_status", "unknown"),
        "limitations": list(source.limitations),
        "content_sha256": entry.sha256,
        "content_hash": entry.sha256,
        "content_size_bytes": entry.size_bytes,
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_path": entry.snapshot_path,
        "retrieved_at": snapshot.created_at,
        "last_processed_at": processed_at,
        "extractor": source.extractor,
        "extract_semantic_sha256": extraction.semantic_sha256,
        "status": "available",
    }
    if history_by_hash:
        record["version_history"] = [history_by_hash[key] for key in sorted(history_by_hash)]
    return record


def _runtime_fingerprint() -> dict[str, str]:
    packages: dict[str, str] = {}
    for package in ("pypdf", "PyYAML", "jsonschema"):
        try:
            packages[package] = package_version(package)
        except PackageNotFoundError:
            packages[package] = "missing"
    return {
        "python": platform.python_version(),
        # A package-version check alone does not invalidate cached extraction
        # when our own static extractor implementation changes.
        "pipeline_extractor_sha256": sha256_file(Path(__file__).with_name("extractors.py")),
        **packages,
    }


def _reject_project_source_overlap(
    project_root: Path, config: PipelineConfig, root_id: str
) -> None:
    """Reject source/output overlap before creating data or staging paths."""

    try:
        source_root = resolve_live_root(config, root_id).resolve(strict=True)
    except OSError:
        # Snapshot-mode environments may intentionally lack live-source access.
        return
    if (
        project_root == source_root
        or project_root in source_root.parents
        or source_root in project_root.parents
    ):
        raise PublicationError("project and read-only source roots must not overlap")


def _reuse_extraction(
    current_release: Path | None,
    current_manifest: Mapping[str, Any] | None,
    previous: Mapping[str, Any] | None,
    source: SourceConfig,
    source_sha256: str,
    runtime: Mapping[str, str],
) -> ExtractionResult | None:
    """Reuse only a previously validated extract with identical processing identity."""

    if (
        current_release is None
        or current_manifest is None
        or previous is None
        or current_manifest.get("runtime") != dict(runtime)
        or previous.get("content_sha256") != source_sha256
        or previous.get("extractor") != source.extractor
        or previous.get("path") != source.path
        or previous.get("location") != f"repo://{source.root}/{source.path}"
    ):
        return None
    extract_path = current_release / "extracts" / f"{source.id}.jsonl"
    units = tuple(read_jsonl(extract_path))
    if not units or any(
        unit.get("source_id") != source.id or unit.get("source_sha256") != source_sha256
        for unit in units
    ):
        raise PublicationError(f"validated reusable extract is inconsistent: {source.id}")
    semantic_sha256 = previous.get("extract_semantic_sha256")
    if not isinstance(semantic_sha256, str):
        raise PublicationError(f"validated reusable extract lacks semantic identity: {source.id}")
    warnings = current_manifest.get("warnings", {}).get(source.id, [])
    if not isinstance(warnings, list) or not all(isinstance(item, str) for item in warnings):
        raise PublicationError(f"validated reusable extract has malformed warnings: {source.id}")
    return ExtractionResult(
        source_id=source.id,
        source_sha256=source_sha256,
        extractor=source.extractor,
        semantic_sha256=semantic_sha256,
        units=units,
        warnings=tuple(warnings),
    )


def _canonical_jsonl_hash(payload: bytes) -> str:
    return canonical_json_hash(_parse_curated_jsonl(payload, "curated JSONL"))


def _parse_curated_jsonl(payload: bytes, label: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise PublicationError(f"{label} is not UTF-8") from exc
    seen_ids: set[str] = set()
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = strict_json_loads(line)
        except ValueError as exc:
            raise PublicationError(f"{label} line {line_number} is invalid JSON") from exc
        if not isinstance(value, dict) or not isinstance(value.get("id"), str):
            raise PublicationError(f"{label} line {line_number} lacks an object id")
        if value["id"] in seen_ids:
            raise PublicationError(f"{label} contains duplicate id {value['id']!r}")
        seen_ids.add(value["id"])
        records.append(value)
    return sorted(records, key=lambda record: record["id"])


def _enforce_append_only_knowledge(
    current_records: Mapping[str, list[dict[str, Any]]],
    candidate_records: Mapping[str, list[dict[str, Any]]],
) -> None:
    violations: list[str] = []
    for filename in (
        "claims.jsonl",
        "methods.jsonl",
        "experiments.jsonl",
        "relationships.jsonl",
    ):
        previous = {record["id"]: record for record in current_records.get(filename, [])}
        candidate = {record["id"]: record for record in candidate_records.get(filename, [])}
        deleted = sorted(set(previous) - set(candidate))
        mutated = sorted(
            record_id
            for record_id in set(previous).intersection(candidate)
            if previous[record_id] != candidate[record_id]
        )
        if deleted:
            violations.append(f"{filename} deleted={deleted}")
        if mutated:
            violations.append(f"{filename} mutated={mutated}")
    if violations:
        raise PublicationError(
            "accepted knowledge is append-only; preserve existing records and use new "
            "IDs/relationships for corrections: " + "; ".join(violations)
        )


def _load_curated_external_sources(
    project_root: Path,
    knowledge_directory: Path,
    snapshot_id: str,
    schemas_directory: Path,
    local_source_ids: set[str],
) -> tuple[list[dict[str, Any]], list[ExtractionResult], bytes]:
    """Load reviewed external source records and their private static extracts.

    Bibliographic records are safe to publish, while extracted paper text stays
    beneath the ignored ``data/automatic/extracts`` boundary. Every release
    still binds the exact source hash and static page units.
    """

    path = knowledge_directory / "sources.jsonl"
    payload = path.read_bytes() if path.exists() else b""
    records = _parse_curated_jsonl(payload, "sources.jsonl")
    if not records:
        return [], [], payload
    validate_records(records, schemas_directory / "source.schema.json", "curated sources")
    external_sources: list[dict[str, Any]] = []
    extractions: list[ExtractionResult] = []
    for record in records:
        source_id = record["id"]
        if source_id in local_source_ids:
            raise PublicationError(f"curated external source collides with local source: {source_id}")
        extract_path = project_root / "data" / "automatic" / "extracts" / f"{source_id}.jsonl"
        if extract_path.is_symlink() or not extract_path.is_file():
            raise PublicationError(f"curated external source has no private extract: {source_id}")
        units = tuple(read_jsonl(extract_path))
        validate_records(units, schemas_directory / "extract.schema.json", f"automatic extract {source_id}")
        source_sha = record.get("content_sha256")
        if not units or any(
            unit.get("source_id") != source_id
            or unit.get("source_sha256") != source_sha
            for unit in units
        ):
            raise PublicationError(f"curated external extract identity mismatch: {source_id}")
        semantic_sha = canonical_json_hash(
            [
                {
                    key: value
                    for key, value in unit.items()
                    if key not in {"id", "source_id", "source_sha256", "schema_version"}
                }
                for unit in units
            ]
        )
        if record.get("extract_semantic_sha256") != semantic_sha:
            raise PublicationError(f"curated external extract semantic hash mismatch: {source_id}")
        release_record = dict(record)
        release_record["snapshot_id"] = snapshot_id
        external_sources.append(release_record)
        extractions.append(
            ExtractionResult(
                source_id=source_id,
                source_sha256=str(source_sha),
                extractor=str(record.get("extractor")),
                semantic_sha256=semantic_sha,
                units=units,
                warnings=(),
            )
        )
    return external_sources, extractions, payload


def build_release_candidate(
    project_root: Path,
    config: PipelineConfig,
    *,
    root_id: str = "imf",
    snapshot_directory: Path | None = None,
    knowledge_directory: Path | None = None,
    schemas_directory: Path | None = None,
) -> ReleaseBuildResult:
    """Extract a verified snapshot and create a complete immutable candidate.

    This operation never updates ``data/current.json``.
    """

    project_root = project_root.resolve(strict=True)
    _reject_project_source_overlap(project_root, config, root_id)
    data_root = ensure_directory_under_root(project_root, "data")
    if snapshot_directory is None:
        snapshot, snapshot_directory = load_current_snapshot(project_root, config, root_id)
    else:
        from .snapshot import load_explicit_snapshot

        snapshot, snapshot_directory = load_explicit_snapshot(
            project_root, config, snapshot_directory, root_id
        )
    snapshot_project_relative = snapshot_directory.relative_to(project_root).as_posix()
    knowledge_directory = knowledge_directory or project_root / "knowledge" / "curated"
    schemas_directory = schemas_directory or project_root / "schemas"
    source_by_id = {source.id: source for source in config.sources if source.root == root_id}
    expected_config_sha256 = config_fingerprint(config)
    if snapshot.root_id != root_id:
        raise PublicationError(
            f"snapshot root {snapshot.root_id!r} does not match requested root {root_id!r}"
        )
    if snapshot.config_sha256 != expected_config_sha256:
        raise PublicationError(
            "snapshot was made with a different source configuration; create a fresh explicit snapshot"
        )
    entry_by_id = {entry.source_id: entry for entry in snapshot.entries}
    if len(entry_by_id) != len(snapshot.entries):
        raise PublicationError("snapshot contains duplicate source ids")
    missing_config = sorted(set(entry_by_id) - set(source_by_id))
    if missing_config:
        raise PublicationError(f"snapshot contains unconfigured sources: {missing_config}")
    configured_ids = set(source_by_id)
    expected_missing = set(snapshot.missing_optional_sources)
    if configured_ids - set(entry_by_id) != expected_missing:
        raise PublicationError("snapshot does not account for every configured source")
    for missing_id in expected_missing:
        if missing_id not in source_by_id or source_by_id[missing_id].required:
            raise PublicationError(f"snapshot marks an invalid optional source missing: {missing_id}")
    for source_id, entry in entry_by_id.items():
        configured_source = source_by_id[source_id]
        if entry.relative_path != configured_source.path:
            raise PublicationError(f"snapshot path does not match config for {source_id}")
        if entry.snapshot_path != f"files/{configured_source.path}":
            raise PublicationError(f"snapshot storage path does not match config for {source_id}")
        if entry.extractor != configured_source.extractor:
            raise PublicationError(f"snapshot extractor does not match config for {source_id}")

    current = _read_current_pointer(project_root)
    current_release: Path | None = None
    current_manifest: Mapping[str, Any] | None = None
    previous_sources: dict[str, dict[str, Any]] = {}
    current_records: dict[str, list[dict[str, Any]]] = {}
    if current:
        current_release = project_root / "data" / "releases" / current["release_id"]
        current_records = validate_release_directory(current_release, schemas_directory)
        current_manifest = read_json(current_release / "release.json")
        verify_bound_publication(
            current_release, schemas_directory, current_manifest.get("publication")
        )
        previous_sources = {
            record["id"]: record for record in current_records.get("sources.jsonl", [])
        }

    processed_at = utc_now()
    runtime = _runtime_fingerprint()
    extraction_results: list[ExtractionResult] = []
    source_records: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="imf-pulse-extract-") as extraction_temp_name:
        extraction_temp = Path(extraction_temp_name)
        for source_id in sorted(entry_by_id):
            source = source_by_id[source_id]
            entry = entry_by_id[source_id]
            safe_suffix = Path(source.path).suffix
            bound_input = extraction_temp / f"{source_id}{safe_suffix}"
            # Every snapshot entry is descriptor-read and hash-checked in this
            # transaction, including entries whose validated semantic extract
            # can be reused.  Reuse skips parsing, never byte verification.
            with open_regular_file_under_root(
                project_root, f"{snapshot_project_relative}/{entry.snapshot_path}"
            ) as source_descriptor:
                copied_sha256, copied_size = copy_exact_bytes_from_descriptor(
                    source_descriptor, bound_input
                )
            if copied_sha256 != entry.sha256 or copied_size != entry.size_bytes:
                raise PublicationError(
                    f"snapshot bytes changed after verification: {source_id}"
                )
            extraction = _reuse_extraction(
                current_release,
                current_manifest,
                previous_sources.get(source_id),
                source,
                copied_sha256,
                runtime,
            )
            if extraction is None:
                extraction = extract_source(bound_input, source, copied_sha256)
            extraction_results.append(extraction)
            source_records.append(
                _source_record(
                    source,
                    snapshot,
                    entry,
                    extraction,
                    processed_at,
                    previous_sources.get(source_id),
                )
            )

    external_sources, external_extractions, curated_source_payload = (
        _load_curated_external_sources(
            project_root,
            knowledge_directory,
            snapshot.snapshot_id,
            schemas_directory,
            set(source_by_id),
        )
    )
    source_records.extend(external_sources)
    extraction_results.extend(external_extractions)

    curated_payloads: dict[str, bytes] = {}
    curated_records: dict[str, list[dict[str, Any]]] = {}
    for filename in ("claims.jsonl", "methods.jsonl", "experiments.jsonl", "relationships.jsonl"):
        path = knowledge_directory / filename
        curated_payloads[filename] = path.read_bytes() if path.exists() else b""
        curated_records[filename] = _parse_curated_jsonl(
            curated_payloads[filename], filename
        )
    if current is not None:
        _enforce_append_only_knowledge(current_records, curated_records)
        current_external = {
            record["id"]: {
                key: value for key, value in record.items() if key != "snapshot_id"
            }
            for record in current_records.get("sources.jsonl", [])
            if record["id"] not in source_by_id
        }
        candidate_external = {record["id"]: record for record in external_sources}
        missing_external = sorted(set(current_external) - set(candidate_external))
        mutated_external = sorted(
            source_id
            for source_id in set(current_external) & set(candidate_external)
            if current_external[source_id]
            != {
                key: value
                for key, value in candidate_external[source_id].items()
                if key != "snapshot_id"
            }
        )
        if missing_external or mutated_external:
            raise PublicationError(
                "accepted external sources are append-only; "
                f"deleted={missing_external}, mutated={mutated_external}"
            )
    semantic_identity = {
        "schema_version": 1,
        "config_sha256": expected_config_sha256,
        "runtime": runtime,
        "extracts": {item.source_id: item.semantic_sha256 for item in extraction_results},
        "curated": {
            filename: _canonical_jsonl_hash(payload)
            for filename, payload in curated_payloads.items()
        }
        | (
            {"sources.jsonl": _canonical_jsonl_hash(curated_source_payload)}
            if curated_source_payload
            else {}
        ),
    }
    semantic_digest = canonical_json_hash(semantic_identity)
    identity = {
        **semantic_identity,
        "snapshot_id": snapshot.snapshot_id,
        "source_bytes": {
            source["id"]: source["content_sha256"] for source in source_records
        },
    }
    release_digest = canonical_json_hash(identity)
    release_id = f"release-{release_digest[:20]}"
    releases_root = ensure_directory_under_root(data_root, "releases")
    release_directory = releases_root / release_id

    current_fingerprint = current_manifest.get("input_fingerprint") if current_manifest else None
    current_semantic_fingerprint = (
        current_manifest.get("semantic_fingerprint") if current_manifest else None
    )
    if current_fingerprint == release_digest:
        return ReleaseBuildResult(
            release_id=str(current["release_id"]),
            release_directory=current_release,
            created=False,
            status="unchanged",
            semantic_changed=False,
        )

    if release_directory.exists():
        manifest = read_json(release_directory / "release.json")
        if manifest.get("input_fingerprint") != release_digest:
            raise PublicationError(f"release id collision: {release_id}")
        expected_previous_release_id = current["release_id"] if current else None
        if manifest.get("previous_release_id") != expected_previous_release_id:
            raise PublicationError(
                "existing candidate was built from a different release ancestry"
            )
        validate_release_directory(release_directory, schemas_directory)
        return ReleaseBuildResult(
            release_id,
            release_directory,
            False,
            "candidate",
            current_semantic_fingerprint != semantic_digest,
        )

    staging_root = ensure_directory_under_root(data_root, ".staging")
    staging_directory = Path(tempfile.mkdtemp(prefix=f".{release_id}-", dir=staging_root))
    try:
        _write_jsonl(staging_directory / "sources.jsonl", source_records)
        if current_release is not None:
            _copy_historical_extracts(
                current_release,
                staging_directory,
                {record["id"]: record["content_sha256"] for record in source_records},
            )
        for extraction in extraction_results:
            _write_jsonl(staging_directory / "extracts" / f"{extraction.source_id}.jsonl", extraction.units)
        for filename, payload in curated_payloads.items():
            path = staging_directory / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as handle:
                handle.write(payload)
                if payload and not payload.endswith(b"\n"):
                    handle.write(b"\n")
                handle.flush()
                os.fsync(handle.fileno())

        state = {
            "schema_version": 1,
            "release_id": release_id,
            "snapshot_id": snapshot.snapshot_id,
            "status": "processed_no_pulse",
            "created_at": processed_at,
            "last_checked_at": processed_at,
            "input_fingerprint": release_digest,
            "semantic_fingerprint": semantic_digest,
            "source_fingerprints": {
                source["id"]: {
                    "content_sha256": source["content_sha256"],
                    "extract_semantic_sha256": source["extract_semantic_sha256"],
                    "extractor": source["extractor"],
                }
                for source in source_records
            },
        }
        _write_json(staging_directory / "state.json", state)
        release_files = _release_file_hashes(staging_directory)
        release_manifest = {
            "schema_version": 1,
            "release_id": release_id,
            "created_at": processed_at,
            "status": "candidate",
            "snapshot_id": snapshot.snapshot_id,
            "config_sha256": expected_config_sha256,
            "input_fingerprint": release_digest,
            "semantic_fingerprint": semantic_digest,
            "runtime": runtime,
            "files": release_files,
            "warnings": {
                item.source_id: list(item.warnings)
                for item in extraction_results
                if item.warnings
            },
        }
        if current is not None:
            release_manifest["previous_release_id"] = current["release_id"]
        _write_json(staging_directory / "release.json", release_manifest)
        validate_release_directory(
            staging_directory, schemas_directory, enforce_directory_name=False
        )
        os.replace(staging_directory, release_directory)
        _fsync_directory(releases_root)
        validate_release_directory(release_directory, schemas_directory)
        return ReleaseBuildResult(
            release_id,
            release_directory,
            True,
            "candidate",
            current_semantic_fingerprint != semantic_digest,
        )
    except BaseException:
        if staging_directory.exists():
            shutil.rmtree(staging_directory)
        raise


def _release_file_hashes(directory: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in sorted(directory.rglob("*")):
        if path.is_file() and not path.is_symlink() and path != directory / "release.json":
            relative = path.relative_to(directory).as_posix()
            hashes[relative] = sha256_file(path)
    return hashes


def _prepare_site_staging(
    project_root: Path, run_id: str
) -> tuple[Path, tuple[int, int]]:
    staging_directory = project_root / "data" / ".site-staging" / run_id
    try:
        with open_directory_under_root(
            project_root, "data/.site-staging", create=True
        ) as staging_parent:
            os.mkdir(run_id, 0o755, dir_fd=staging_parent)
            node = os.stat(run_id, dir_fd=staging_parent, follow_symlinks=False)
            if not stat.S_ISDIR(node.st_mode):
                raise PublicationError(
                    "fresh site build staging node is not a directory"
                )
            staged = open_child_directory(staging_parent, run_id)
            try:
                opened = os.fstat(staged)
                if (opened.st_dev, opened.st_ino) != (node.st_dev, node.st_ino):
                    raise PublicationError(
                        "site build staging directory changed while opening"
                    )
            finally:
                os.close(staged)
    except PublicationError:
        raise
    except BaseException as exc:
        raise PublicationError("cannot create a fresh site build staging directory") from exc
    return staging_directory, (opened.st_dev, opened.st_ino)


def _site_tree_hash_from_descriptor(directory_descriptor: int) -> str:
    files = _site_file_hashes_from_descriptor(directory_descriptor)
    if "index.html" not in files:
        raise PublicationError("staged site build must contain index.html")
    return canonical_json_hash(files)


def _site_file_hashes_from_descriptor(
    directory_descriptor: int, prefix: str = ""
) -> dict[str, str]:
    result: dict[str, str] = {}
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        names = sorted(os.listdir(directory_descriptor))
    except OSError as exc:
        raise PublicationError("site build directory cannot be enumerated safely") from exc
    for name in names:
        relative = f"{prefix}/{name}" if prefix else name
        try:
            node = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        except OSError as exc:
            raise PublicationError(f"site build node changed while reading: {relative}") from exc
        if stat.S_ISDIR(node.st_mode):
            try:
                child = os.open(name, directory_flags, dir_fd=directory_descriptor)
            except OSError as exc:
                raise PublicationError(f"site build directory is unsafe: {relative}") from exc
            try:
                result.update(_site_file_hashes_from_descriptor(child, relative))
            finally:
                os.close(child)
            continue
        if not stat.S_ISREG(node.st_mode):
            raise PublicationError(f"site build contains a forbidden node: {relative}")
        try:
            descriptor = os.open(
                name, os.O_RDONLY | os.O_NONBLOCK | nofollow, dir_fd=directory_descriptor
            )
        except OSError as exc:
            raise PublicationError(f"site build file is unsafe: {relative}") from exc
        try:
            before = os.fstat(descriptor)
            if (before.st_dev, before.st_ino) != (node.st_dev, node.st_ino):
                raise PublicationError(f"site build file changed while opening: {relative}")
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
            after = os.fstat(descriptor)
            if (
                (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or total != before.st_size
            ):
                raise PublicationError(f"site build file changed while reading: {relative}")
            result[relative] = digest.hexdigest()
        finally:
            os.close(descriptor)
    return result


def _fsync_site_tree(directory_descriptor: int, prefix: str = "") -> None:
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        names = sorted(os.listdir(directory_descriptor))
    except OSError as exc:
        raise PublicationError("site build directory cannot be synced safely") from exc
    for name in names:
        relative = f"{prefix}/{name}" if prefix else name
        try:
            node = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        except OSError as exc:
            raise PublicationError(
                f"site build node changed before sync: {relative}"
            ) from exc
        if stat.S_ISDIR(node.st_mode):
            try:
                child = os.open(name, directory_flags, dir_fd=directory_descriptor)
            except OSError as exc:
                raise PublicationError(
                    f"site build directory is unsafe during sync: {relative}"
                ) from exc
            try:
                opened = os.fstat(child)
                if not stat.S_ISDIR(opened.st_mode) or (
                    opened.st_dev,
                    opened.st_ino,
                ) != (node.st_dev, node.st_ino):
                    raise PublicationError(
                        f"site build directory changed during sync: {relative}"
                    )
                _fsync_site_tree(child, relative)
            finally:
                os.close(child)
        elif stat.S_ISREG(node.st_mode):
            try:
                descriptor = os.open(
                    name,
                    os.O_RDONLY | os.O_NONBLOCK | nofollow,
                    dir_fd=directory_descriptor,
                )
            except OSError as exc:
                raise PublicationError(
                    f"site build file is unsafe during sync: {relative}"
                ) from exc
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode) or (
                    opened.st_dev,
                    opened.st_ino,
                ) != (node.st_dev, node.st_ino):
                    raise PublicationError(
                        f"site build file changed during sync: {relative}"
                    )
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        else:
            raise PublicationError(f"site build contains a forbidden node: {relative}")
    os.fsync(directory_descriptor)


def _remove_directory_at(
    parent_descriptor: int, name: str, expected_identity: tuple[int, int]
) -> None:
    current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if not stat.S_ISDIR(current.st_mode) or (
        current.st_dev,
        current.st_ino,
    ) != expected_identity:
        raise PublicationError("site staging directory changed before cleanup")
    trash = f".{name}.trash-{uuid.uuid4().hex}"
    os.rename(name, trash, src_dir_fd=parent_descriptor, dst_dir_fd=parent_descriptor)
    moved = os.stat(trash, dir_fd=parent_descriptor, follow_symlinks=False)
    if not stat.S_ISDIR(moved.st_mode) or (
        moved.st_dev,
        moved.st_ino,
    ) != expected_identity:
        raise PublicationError("site staging directory changed during cleanup")
    if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
        os.rename(trash, name, src_dir_fd=parent_descriptor, dst_dir_fd=parent_descriptor)
        raise PublicationError("safe site staging cleanup is unavailable")
    shutil.rmtree(trash, dir_fd=parent_descriptor)
    os.fsync(parent_descriptor)


def _rename_directory_noreplace(
    source_parent: int,
    source_name: str,
    destination_parent: int,
    destination_name: str,
) -> bool:
    """Atomically install a directory without replacing an existing name."""

    import ctypes
    import errno

    libc = ctypes.CDLL(None, use_errno=True)
    source = os.fsencode(source_name)
    destination = os.fsencode(destination_name)
    if sys.platform == "darwin":
        try:
            rename = libc.renameatx_np
        except AttributeError as exc:
            raise PublicationError(
                "atomic no-replace site installation is unavailable"
            ) from exc
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(
            source_parent,
            source,
            destination_parent,
            destination,
            0x00000004,  # RENAME_EXCL
        )
    elif sys.platform.startswith("linux"):
        try:
            rename = libc.renameat2
        except AttributeError as exc:
            raise PublicationError(
                "atomic no-replace site installation is unavailable"
            ) from exc
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(
            source_parent,
            source,
            destination_parent,
            destination,
            0x00000001,  # RENAME_NOREPLACE
        )
    else:
        raise PublicationError("atomic no-replace site installation is unavailable")
    if result == 0:
        return True
    error = ctypes.get_errno()
    if error in {errno.EEXIST, errno.ENOTEMPTY}:
        return False
    raise PublicationError(
        f"cannot install immutable site build: {os.strerror(error)}"
    )


def _cleanup_site_staging(
    project_root: Path, run_id: str, expected_identity: tuple[int, int]
) -> None:
    with open_directory_under_root(
        project_root, "data/.site-staging"
    ) as staging_parent:
        try:
            _remove_directory_at(staging_parent, run_id, expected_identity)
        except FileNotFoundError:
            return


def _install_site_build(
    project_root: Path, run_id: str, expected_identity: tuple[int, int]
) -> tuple[str, str]:
    with open_directory_under_root(
        project_root, "data/.site-staging"
    ) as staging_parent:
        staged = open_child_directory(staging_parent, run_id)
        try:
            opened = os.fstat(staged)
            if (opened.st_dev, opened.st_ino) != expected_identity:
                raise PublicationError("site staging directory changed before installation")
            _fsync_site_tree(staged)
            tree_sha256 = _site_tree_hash_from_descriptor(staged)
        finally:
            os.close(staged)
        site_name = f"site-{tree_sha256}"
        with open_directory_under_root(
            project_root, "data/site-builds", create=True
        ) as builds_parent:
            installed_new = _rename_directory_noreplace(
                staging_parent, run_id, builds_parent, site_name
            )
            if installed_new:
                os.fsync(staging_parent)
                os.fsync(builds_parent)
            else:
                try:
                    existing_stat = os.stat(
                        site_name, dir_fd=builds_parent, follow_symlinks=False
                    )
                except OSError as exc:
                    raise PublicationError(
                        "existing immutable site build is unavailable"
                    ) from exc
                if not stat.S_ISDIR(existing_stat.st_mode):
                    raise PublicationError("immutable site build path is not a directory")
                existing = open_child_directory(builds_parent, site_name)
                try:
                    if _site_tree_hash_from_descriptor(existing) != tree_sha256:
                        raise PublicationError("immutable site build bytes differ at existing path")
                finally:
                    os.close(existing)
                _remove_directory_at(staging_parent, run_id, expected_identity)
            installed = open_child_directory(builds_parent, site_name)
            try:
                if _site_tree_hash_from_descriptor(installed) != tree_sha256:
                    raise PublicationError("installed site build failed integrity verification")
            finally:
                os.close(installed)
    return f"data/site-builds/{site_name}", tree_sha256


def _validate_site_build_selection(
    project_root: Path, site_build_path: Any, site_build_sha256: Any
) -> None:
    if (
        not isinstance(site_build_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", site_build_sha256) is None
    ):
        raise PublicationError("current pointer site_build_sha256 is invalid")
    expected_path = f"data/site-builds/site-{site_build_sha256}"
    if site_build_path != expected_path:
        raise PublicationError("current pointer site build path is inconsistent")
    with open_directory_under_root(project_root, expected_path) as descriptor:
        if _site_tree_hash_from_descriptor(descriptor) != site_build_sha256:
            raise PublicationError("current pointer site build digest mismatch")


def _copy_historical_extracts(
    current_release: Path,
    staging_directory: Path,
    current_hashes: Mapping[str, str],
) -> None:
    source_directory = current_release / "extracts"
    destination_directory = staging_directory / "extracts"
    destination_directory.mkdir(parents=True, exist_ok=True)
    for path in sorted(source_directory.glob("*.jsonl")):
        records = read_jsonl(path)
        if not records:
            continue
        source_ids = {record.get("source_id") for record in records}
        source_hashes = {record.get("source_sha256") for record in records}
        if len(source_ids) != 1 or len(source_hashes) != 1:
            raise PublicationError(f"historical extract is internally inconsistent: {path}")
        source_id = next(iter(source_ids))
        source_hash = next(iter(source_hashes))
        if not isinstance(source_id, str) or not isinstance(source_hash, str):
            raise PublicationError(f"historical extract lacks source identity: {path}")
        if source_id not in current_hashes:
            continue
        if current_hashes.get(source_id) == source_hash:
            continue
        destination = destination_directory / f"{source_id}@{source_hash}.jsonl"
        payload = path.read_bytes()
        if destination.exists():
            if destination.read_bytes() != payload:
                raise PublicationError(f"historical extract collision: {destination.name}")
            continue
        with destination.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())


def _bytes_sha256(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()


def _read_current_pointer(project_root: Path) -> dict[str, Any] | None:
    path = project_root / "data" / "current.json"
    if not path.exists():
        return None
    value = read_json(path)
    if not isinstance(value, dict) or not isinstance(value.get("release_id"), str):
        raise PublicationError(f"invalid release pointer: {path}")
    release_id = value["release_id"]
    if not RELEASE_ID_PATTERN.fullmatch(release_id):
        raise PublicationError(f"release pointer contains an unsafe release id: {release_id!r}")
    expected_path = f"data/releases/{release_id}"
    if value.get("release_path", expected_path) != expected_path:
        raise PublicationError(f"release pointer path is unsafe or inconsistent: {path}")
    value["release_path"] = expected_path
    release_directory = project_root / expected_path
    if not release_directory.is_dir() or release_directory.is_symlink():
        raise PublicationError(f"release pointer target is unavailable: {release_id}")
    try:
        releases_root = (project_root / "data" / "releases").resolve(strict=True)
        if release_directory.resolve(strict=True).parent != releases_root:
            raise PublicationError(f"release pointer escapes the releases directory: {release_id}")
    except OSError as exc:
        raise PublicationError(f"release pointer target is unavailable: {release_id}") from exc
    expected_hash = value.get("release_sha256")
    if expected_hash is not None:
        manifest = read_json(release_directory / "release.json")
        if canonical_json_hash(manifest) != expected_hash:
            raise PublicationError(f"release pointer digest mismatch: {release_id}")
    has_site_path = "site_build_path" in value
    has_site_sha256 = "site_build_sha256" in value
    if has_site_path != has_site_sha256:
        raise PublicationError("release pointer must contain both site build fields")
    if has_site_path:
        _validate_site_build_selection(
            project_root, value["site_build_path"], value["site_build_sha256"]
        )
    return value


def default_gate_runner(command: Sequence[str], cwd: Path, environment: Mapping[str, str]) -> None:
    try:
        subprocess.run(
            list(command),
            cwd=cwd,
            env=dict(environment),
            check=True,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PublicationError(f"release gate failed: {' '.join(command)}") from exc


def publish_release(
    project_root: Path,
    release_id: str,
    *,
    schemas_directory: Path | None = None,
    pulse: str | None = None,
    artifact_manifests: Sequence[str] = (),
    gate_runner: GateRunner = default_gate_runner,
    now: str | None = None,
) -> PublishResult:
    """Publish under an exclusive local lock."""

    with _exclusive_publish_lock(project_root):
        return _publish_release_unlocked(
            project_root,
            release_id,
            schemas_directory=schemas_directory,
            pulse=pulse,
            artifact_manifests=artifact_manifests,
            gate_runner=gate_runner,
            now=now,
        )


def _publish_release_unlocked(
    project_root: Path,
    release_id: str,
    *,
    schemas_directory: Path | None = None,
    pulse: str | None = None,
    artifact_manifests: Sequence[str] = (),
    gate_runner: GateRunner = default_gate_runner,
    now: str | None = None,
) -> PublishResult:
    """Validate and publish one release; pointer replacement is the commit point."""

    if not RELEASE_ID_PATTERN.fullmatch(release_id):
        raise PublicationError(f"unsafe release id: {release_id!r}")
    project_root = project_root.resolve(strict=True)
    release_directory = project_root / "data" / "releases" / release_id
    schemas_directory = schemas_directory or project_root / "schemas"
    _assert_safe_release_directory(project_root, release_directory)
    release_stat = os.lstat(release_directory)
    release_identity = (release_stat.st_dev, release_stat.st_ino)
    current = _read_current_pointer(project_root)
    orphan_publication = release_directory / "publication"
    if os.path.lexists(orphan_publication):
        preliminary_manifest = read_json(release_directory / "release.json")
        if (
            preliminary_manifest.get("publication") is None
            and (current is None or current.get("release_id") != release_id)
        ):
            if orphan_publication.is_symlink() or not orphan_publication.is_dir():
                raise PublicationError("candidate has an unsafe orphan publication path")
            _remove_publication_directory(release_directory, release_identity)
    release_records = validate_release_directory(release_directory, schemas_directory)
    release_manifest = read_json(release_directory / "release.json")
    _validate_candidate_ancestry(release_id, release_manifest, current)
    run_id = f"run-{uuid.uuid4().hex}"
    timestamp = now or utc_now()

    current_publication: Mapping[str, Any] | None = None
    current_release_directory: Path | None = None
    current_accepted_publications: list[dict[str, Any]] = []
    if current:
        current_release_directory = project_root / current["release_path"]
        current_records = validate_release_directory(current_release_directory, schemas_directory)
        del current_records
        current_release_manifest = read_json(current_release_directory / "release.json")
        current_publication = current_release_manifest.get("publication")
        verify_bound_publication(
            current_release_directory, schemas_directory, current_publication
        )
        current_accepted_publications = _accepted_publications(current)
        _verify_accepted_publication_history(
            project_root, schemas_directory, current_accepted_publications
        )
        _validate_pointer_history_summaries(current, current_accepted_publications)
        _validate_accepted_publications_digest(
            current, current_release_manifest, current_accepted_publications
        )

    # Never mutate the directory named by the authoritative checkpoint.  A
    # report must be bound before its candidate becomes current.
    if current and current.get("release_id") == release_id and pulse is not None:
        expected_pulse = (
            current_publication.get("pulse", {}).get("source_path")
            if isinstance(current_publication, Mapping)
            else None
        )
        expected_manifests = {
            item.get("manifest_url")
            for item in current_publication.get("artifact_manifests", [])
        } if isinstance(current_publication, Mapping) else set()
        supplied_manifests = {
            (
                "/" + value.removeprefix("public/")
                if value.startswith("public/artifacts/")
                else value
            )
            for value in artifact_manifests
        }
        if pulse != expected_pulse or (
            supplied_manifests and supplied_manifests != expected_manifests
        ):
            raise PublicationError(
                "cannot bind different publication inputs to the current release"
            )
        # Idempotent retry after a commit whose CLI response was interrupted.
        pulse = None

    # A true no-update run does not rebuild unchanged bytes or create a pulse,
    # but it still runs every acceptance gate before refreshing the checkpoint.
    if current and current.get("release_id") == release_id:
        site_staging, site_staging_identity = _prepare_site_staging(
            project_root, run_id
        )
        environment = dict(os.environ)
        environment["IMF_PULSE_RELEASE_DIR"] = str(release_directory)
        environment["IMF_PULSE_BUILD_OUT_DIR"] = str(site_staging)
        environment["IMF_PULSE_CHECKPOINT_STATUS"] = "unchanged"
        environment.pop("IMF_PULSE_SELECTED_PULSE", None)
        environment["IMF_PULSE_ARTIFACT_MANIFESTS"] = "[]"
        environment["IMF_PULSE_ACCEPTED_PUBLICATIONS"] = json.dumps(
            current_accepted_publications, separators=(",", ":"), sort_keys=True
        )
        selected_gates = (
            (sys.executable, "-m", "pytest"),
            ("npm", "test"),
            ("npm", "run", "build"),
        )
        try:
            for gate in selected_gates:
                gate_runner(gate, project_root, environment)
            validate_release_directory(release_directory, schemas_directory)
            checked_manifest = read_json(release_directory / "release.json")
            if checked_manifest != current_release_manifest:
                raise PublicationError(
                    "current release manifest changed while no-update gates were running"
                )
            verify_bound_publication(
                release_directory, schemas_directory, current_publication
            )
            _verify_accepted_publication_history(
                project_root, schemas_directory, current_accepted_publications
            )
            _validate_accepted_publications_digest(
                current, checked_manifest, current_accepted_publications
            )
            site_build_path, site_build_sha256 = _install_site_build(
                project_root, run_id, site_staging_identity
            )
        except BaseException as exc:
            try:
                _cleanup_site_staging(
                    project_root, run_id, site_staging_identity
                )
            except BaseException:
                pass
            failed_record = {
                "schema_version": 1,
                "id": run_id,
                "status": "failed",
                "release_id": release_id,
                "started_at": timestamp,
                "completed_at": utc_now(),
                "pointer_changed": False,
                "error": str(exc),
            }
            try:
                _write_run_record(
                    project_root, run_id, failed_record, schemas_directory
                )
            except BaseException:
                pass
            if isinstance(exc, PublicationError):
                raise exc
            raise PublicationError(f"release gate failed: {exc}") from exc

        _write_run_record(
            project_root,
            run_id,
            {
                "schema_version": 1,
                "id": run_id,
                "status": "ready_to_publish",
                "release_id": release_id,
                "started_at": timestamp,
                "completed_at": timestamp,
                "pointer_changed": False,
            },
            schemas_directory,
        )
        refreshed = dict(current)
        refreshed.update(
            {
                "status": "unchanged",
                "updated_at": timestamp,
                "last_checked_at": timestamp,
                "run_id": run_id,
                # These describe this run only.  Accepted history is carried
                # separately and is therefore never guessed from filenames.
                "pulse": None,
                "artifact_manifests": [],
                "site_build_path": site_build_path,
                "site_build_sha256": site_build_sha256,
            }
        )
        try:
            atomic_write_json(project_root / "data" / "current.json", refreshed)
        except BaseException:
            # The ready record is intentionally left non-final when the commit
            # point cannot be replaced.
            raise
        try:
            _replace_run_record(
                project_root,
                run_id,
                {
                    "schema_version": 1,
                    "id": run_id,
                    "status": "unchanged",
                    "release_id": release_id,
                    "started_at": timestamp,
                    "completed_at": utc_now(),
                    "pointer_changed": False,
                },
                schemas_directory,
            )
        except BaseException:
            pass
        return PublishResult(release_id, run_id, "unchanged", False)

    publication_was_present = (release_directory / "publication").exists()
    original_manifest = dict(release_manifest)
    accepted_publications = list(current_accepted_publications)
    try:
        binding = bind_publication_inputs(
            project_root,
            release_directory,
            schemas_directory,
            release_records,
            pulse=pulse,
            artifact_manifests=artifact_manifests,
            expected_release_identity=release_identity,
        )
        existing_binding = release_manifest.get("publication")
        if binding.metadata is None and existing_binding is not None:
            raise PublicationError(
                "release is already bound to a pulse; supply the same pulse when retrying"
            )
        if binding.metadata is not None:
            if existing_binding is not None and existing_binding != binding.metadata:
                raise PublicationError("release is already bound to different publication inputs")
            if existing_binding is None:
                release_manifest = dict(release_manifest)
                release_manifest["publication"] = binding.metadata
        if binding.metadata is not None:
            new_publication = _publication_history_record(release_id, binding.metadata)
            duplicate = next(
                (
                    item
                    for item in accepted_publications
                    if item.get("pulse") == binding.selected_pulse
                ),
                None,
            )
            if duplicate is not None:
                if _publication_content_identity(duplicate) != _publication_content_identity(
                    new_publication
                ):
                    raise PublicationError(
                        "an accepted pulse path cannot be rewritten with different bytes"
                    )
                raise PublicationError(
                    "an accepted pulse cannot be published by a different release; "
                    "omit --pulse for an evidence-only release"
                )
            _enforce_stable_artifact_history(accepted_publications, new_publication)
            accepted_publications.append(new_publication)
            latest_publication = accepted_publications[-1]
            if (
                latest_publication.get("release_id") != release_id
                or latest_publication.get("pulse") != binding.selected_pulse
                or latest_publication.get("binding_sha256")
                != binding.metadata.get("binding_sha256")
            ):
                raise PublicationError(
                    "published pulse is not the latest accepted publication for this release"
                )

        release_manifest = dict(release_manifest)
        release_manifest["accepted_publications_sha256"] = canonical_json_hash(
            accepted_publications
        )
        if binding.metadata is not None:
            release_manifest["files"] = _release_file_hashes(release_directory)
        _atomic_write_release_json(
            release_directory, release_identity, release_manifest
        )
        release_records = validate_release_directory(release_directory, schemas_directory)
        release_manifest = read_json(release_directory / "release.json")
        verify_bound_publication(
            release_directory, schemas_directory, release_manifest.get("publication")
        )
        verify_source_publication_inputs(project_root, release_manifest.get("publication"))
        _verify_accepted_publication_history(
            project_root, schemas_directory, accepted_publications
        )
    except BaseException as original_error:
        # Binding is the sole permitted pre-publication mutation.  Roll it
        # back if sealing fails, so a candidate cannot be stranded in a state
        # that fails its own manifest on the next retry.
        if not publication_was_present:
            try:
                _remove_publication_directory(release_directory, release_identity)
            except BaseException:
                pass
        try:
            _atomic_write_release_json(
                release_directory, release_identity, original_manifest
            )
        except BaseException:
            # A concurrent path swap must not turn recovery into a destructive
            # action or hide the validation error that aborted binding.
            pass
        raise original_error

    sealed_manifest = read_json(release_directory / "release.json")
    sealed_manifest_sha256 = canonical_json_hash(sealed_manifest)
    status = "published" if binding.selected_pulse else "processed_no_pulse"
    site_staging, site_staging_identity = _prepare_site_staging(
        project_root, run_id
    )

    environment = dict(os.environ)
    environment["IMF_PULSE_RELEASE_DIR"] = str(release_directory)
    environment["IMF_PULSE_BUILD_OUT_DIR"] = str(site_staging)
    environment["IMF_PULSE_CHECKPOINT_STATUS"] = status
    gate_pulse = binding.selected_pulse
    gate_manifests = list(binding.artifact_manifest_urls)
    if gate_pulse is not None:
        environment["IMF_PULSE_SELECTED_PULSE"] = gate_pulse
        environment["IMF_PULSE_ARTIFACT_MANIFESTS"] = json.dumps(
            gate_manifests, separators=(",", ":")
        )
    else:
        environment.pop("IMF_PULSE_SELECTED_PULSE", None)
        environment["IMF_PULSE_ARTIFACT_MANIFESTS"] = "[]"
    environment["IMF_PULSE_ACCEPTED_PUBLICATIONS"] = json.dumps(
        accepted_publications, separators=(",", ":"), sort_keys=True
    )
    selected_gates = (
        (sys.executable, "-m", "pytest"),
        ("npm", "test"),
        ("npm", "run", "build"),
    )
    try:
        for gate in selected_gates:
            gate_runner(gate, project_root, environment)
        # Gates may be arbitrary local commands.  Re-open and validate every
        # release/binding byte after they finish, before recording readiness.
        validate_release_directory(release_directory, schemas_directory)
        release_manifest = read_json(release_directory / "release.json")
        if (
            release_manifest != sealed_manifest
            or canonical_json_hash(release_manifest) != sealed_manifest_sha256
        ):
            raise PublicationError("release manifest changed while gates were running")
        verify_bound_publication(
            release_directory, schemas_directory, release_manifest.get("publication")
        )
        verify_source_publication_inputs(project_root, release_manifest.get("publication"))
        if current_release_directory is not None and current_release_directory != release_directory:
            verify_bound_publication(
                current_release_directory, schemas_directory, current_publication
            )
        _verify_accepted_publication_history(
            project_root, schemas_directory, accepted_publications
        )
        site_build_path, site_build_sha256 = _install_site_build(
            project_root, run_id, site_staging_identity
        )
    except BaseException as exc:
        cleanup_warnings: list[str] = []
        try:
            try:
                _cleanup_site_staging(
                    project_root, run_id, site_staging_identity
                )
            except BaseException as cleanup_error:
                cleanup_warnings.append(
                    f"site staging cleanup was safely refused: {cleanup_error}"
                )
            try:
                if not publication_was_present and (release_directory / "publication").exists():
                    _remove_publication_directory(release_directory, release_identity)
            except BaseException as cleanup_error:
                cleanup_warnings.append(
                    f"candidate binding cleanup was safely refused: {cleanup_error}"
                )
            try:
                _atomic_write_release_json(
                    release_directory, release_identity, original_manifest
                )
            except BaseException as cleanup_error:
                cleanup_warnings.append(
                    f"candidate manifest cleanup was safely refused: {cleanup_error}"
                )
            failed_record = {
                "schema_version": 1,
                "id": run_id,
                "status": "failed",
                "release_id": release_id,
                "started_at": timestamp,
                "completed_at": utc_now(),
                "pointer_changed": False,
                "error": str(exc),
            }
            if cleanup_warnings:
                failed_record["warnings"] = cleanup_warnings
            try:
                _write_run_record(
                    project_root,
                    run_id,
                    failed_record,
                    schemas_directory,
                )
            except BaseException:
                # Audit persistence is best effort after a failed gate.  A
                # substituted data pathname must never mask the root failure.
                pass
        except BaseException:
            pass
        if isinstance(exc, PublicationError):
            raise exc
        raise PublicationError(f"release gate failed: {exc}") from exc

    run_record = {
        "schema_version": 1,
        "id": run_id,
        # The run record is durable before the checkpoint swap, so it must not
        # claim that publication has happened yet.  The pointer is the commit
        # record and remains the final critical write.
        "status": "ready_to_publish",
        "release_id": release_id,
        "started_at": timestamp,
        "completed_at": utc_now(),
        "pointer_changed": False,
    }
    _write_run_record(project_root, run_id, run_record, schemas_directory)

    accepted_pulses = [item["pulse"] for item in accepted_publications]
    accepted_manifests: list[str] = []
    for publication in accepted_publications:
        for artifact in publication.get("artifact_manifests", []):
            if artifact["url"] not in accepted_manifests:
                accepted_manifests.append(artifact["url"])
    latest_accepted_pulse = accepted_pulses[-1] if accepted_pulses else None
    latest_manifests = (
        [item["url"] for item in accepted_publications[-1].get("artifact_manifests", [])]
        if accepted_publications
        else []
    )
    pointer = {
        "schema_version": 1,
        "release_id": release_id,
        "release_path": f"data/releases/{release_id}",
        "release_sha256": canonical_json_hash(release_manifest),
        "updated_at": timestamp,
        "last_checked_at": timestamp,
        "status": status,
        "run_id": run_id,
        "pulse": binding.selected_pulse,
        "artifact_manifests": list(binding.artifact_manifest_urls),
        "latest_accepted_pulse": latest_accepted_pulse,
        "accepted_pulses": accepted_pulses,
        "accepted_artifact_manifests": accepted_manifests,
        "latest_accepted_artifact_manifests": latest_manifests,
        "accepted_publications": accepted_publications,
        "accepted_publications_sha256": canonical_json_hash(accepted_publications),
        "site_build_path": site_build_path,
        "site_build_sha256": site_build_sha256,
    }
    published_at = (
        timestamp
        if binding.selected_pulse is not None
        else (current.get("published_at") if current else None)
    )
    if isinstance(published_at, str):
        pointer["published_at"] = published_at
    if binding.metadata is not None:
        pointer["publication_binding_sha256"] = binding.metadata["binding_sha256"]
        pointer["bound_pulse"] = (
            f"data/releases/{release_id}/{binding.metadata['pulse']['bound_path']}"
        )
    # Pointer replacement is the commit point.  The only later write finalizes
    # the already-durable run record; it cannot change accepted content.
    atomic_write_json(project_root / "data" / "current.json", pointer)
    try:
        _replace_run_record(
            project_root,
            run_id,
            {
                **run_record,
                "status": status,
                "completed_at": utc_now(),
                "pointer_changed": True,
            },
            schemas_directory,
        )
    except BaseException:
        # The pointer already committed.  Leaving the truthful ready record is
        # preferable to reporting a rollback that did not happen; a retry is
        # idempotent and will finalize a new unchanged run.
        pass
    return PublishResult(release_id, run_id, status, True)


def _publication_history_record(
    release_id: str, metadata: Mapping[str, Any]
) -> dict[str, Any]:
    pulse = metadata["pulse"]
    prefix = f"data/releases/{release_id}/"
    return {
        "release_id": release_id,
        "pulse": pulse["source_path"],
        "bound_pulse": prefix + pulse["bound_path"],
        "pulse_sha256": pulse["sha256"],
        "binding_sha256": metadata["binding_sha256"],
        "artifact_manifests": [
            {
                "url": artifact["manifest_url"],
                "bound_path": prefix + artifact["bound_path"],
                "sha256": artifact["sha256"],
                "files": [
                    {
                        "url": item["url"],
                        "bound_path": prefix + item["bound_path"],
                        "sha256": item["sha256"],
                        "bytes": item["bytes"],
                    }
                    for item in artifact.get("files", [])
                ],
            }
            for artifact in metadata.get("artifact_manifests", [])
        ],
    }


def _validate_candidate_ancestry(
    release_id: str,
    release_manifest: Mapping[str, Any],
    current: Mapping[str, Any] | None,
) -> None:
    """Reject genesis/non-genesis ambiguity and stale release rollbacks."""

    previous_release_id = release_manifest.get("previous_release_id")
    if current is None:
        if previous_release_id is not None:
            raise PublicationError(
                "non-genesis candidate cannot publish without its predecessor checkpoint"
            )
        return
    current_release_id = current["release_id"]
    if release_id == current_release_id:
        # The authoritative release keeps its own predecessor; unchanged runs
        # validate it in place without pretending the release succeeds itself.
        return
    if previous_release_id != current_release_id:
        raise PublicationError(
            "candidate release ancestry is stale or missing; rebuild from the current release"
        )


def _assert_safe_release_directory(project_root: Path, release_directory: Path) -> None:
    data_directory = project_root / "data"
    releases_directory = data_directory / "releases"
    for path, label in (
        (data_directory, "data directory"),
        (releases_directory, "releases directory"),
        (release_directory, "release directory"),
    ):
        try:
            mode = os.lstat(path).st_mode
        except OSError as exc:
            raise PublicationError(f"{label} is unavailable: {path}") from exc
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise PublicationError(f"{label} must be a non-symlink directory: {path}")
    if releases_directory.resolve(strict=True).parent != data_directory.resolve(strict=True):
        raise PublicationError("releases directory escapes the project data directory")
    if release_directory.resolve(strict=True).parent != releases_directory.resolve(strict=True):
        raise PublicationError("release directory escapes data/releases")


def _remove_publication_directory(
    release_directory: Path, expected_identity: tuple[int, int]
) -> None:
    """Remove a generated orphan using a held, no-follow release descriptor."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(release_directory, flags)
    trash = f".publication-orphan-{uuid.uuid4().hex}"
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != expected_identity:
            raise PublicationError("release directory changed before publication cleanup")
        publication_stat = os.stat("publication", dir_fd=descriptor, follow_symlinks=False)
        if not stat.S_ISDIR(publication_stat.st_mode):
            raise PublicationError("publication cleanup target is not a directory")
        os.rename(
            "publication",
            trash,
            src_dir_fd=descriptor,
            dst_dir_fd=descriptor,
        )
        if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
            # Rename already detached the known name.  Refuse recursive work
            # on platforms without the fd-based symlink-safe implementation.
            os.rename(trash, "publication", src_dir_fd=descriptor, dst_dir_fd=descriptor)
            raise PublicationError("safe publication cleanup is unavailable on this platform")
        shutil.rmtree(trash, dir_fd=descriptor)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _accepted_publications(current: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if current is None:
        return []
    raw = current.get("accepted_publications", [])
    if not isinstance(raw, list):
        raise PublicationError("current pointer accepted_publications must be a list")
    result: list[dict[str, Any]] = []
    seen_pulses: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise PublicationError("current pointer contains an invalid accepted publication")
        release_id = item.get("release_id")
        pulse = item.get("pulse")
        if (
            not isinstance(release_id, str)
            or not RELEASE_ID_PATTERN.fullmatch(release_id)
            or not isinstance(pulse, str)
            or pulse in seen_pulses
        ):
            raise PublicationError("current pointer contains unsafe accepted publication identity")
        # Round-trip through strict JSON-compatible data so later mutation of a
        # nested pointer object cannot affect this transaction's snapshot.
        result.append(json.loads(json.dumps(item, allow_nan=False)))
        seen_pulses.add(pulse)
    return result


def _validate_pointer_history_summaries(
    current: Mapping[str, Any], accepted: Sequence[Mapping[str, Any]]
) -> None:
    pulses = [item["pulse"] for item in accepted]
    artifact_urls: list[str] = []
    for publication in accepted:
        for artifact in publication.get("artifact_manifests", []):
            if artifact["url"] not in artifact_urls:
                artifact_urls.append(artifact["url"])
    latest_pulse = pulses[-1] if pulses else None
    latest_artifacts = (
        [item["url"] for item in accepted[-1].get("artifact_manifests", [])]
        if accepted
        else []
    )
    expected = {
        "accepted_pulses": pulses,
        "accepted_artifact_manifests": artifact_urls,
        "latest_accepted_pulse": latest_pulse,
        "latest_accepted_artifact_manifests": latest_artifacts,
    }
    for field, value in expected.items():
        if current.get(field, value) != value:
            raise PublicationError(f"current pointer history summary is inconsistent: {field}")


def _validate_accepted_publications_digest(
    pointer: Mapping[str, Any],
    release_manifest: Mapping[str, Any],
    accepted: Sequence[Mapping[str, Any]],
) -> None:
    """Require the ordered archive to match bytes sealed in the current release."""

    expected = canonical_json_hash(list(accepted))
    if release_manifest.get("accepted_publications_sha256") != expected:
        raise PublicationError(
            "accepted publication history does not match the sealed release digest"
        )
    if pointer.get("accepted_publications_sha256") != expected:
        raise PublicationError(
            "accepted publication history does not match the pointer digest"
        )


def _publication_content_identity(publication: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "pulse": publication.get("pulse"),
        "pulse_sha256": publication.get("pulse_sha256"),
        "artifacts": {
            artifact.get("url"): {
                "sha256": artifact.get("sha256"),
                "files": {
                    item.get("url"): (item.get("sha256"), item.get("bytes"))
                    for item in artifact.get("files", [])
                },
            }
            for artifact in publication.get("artifact_manifests", [])
        },
    }


def _enforce_stable_artifact_history(
    accepted: Sequence[Mapping[str, Any]], new_publication: Mapping[str, Any]
) -> None:
    known: dict[str, tuple[Any, Any]] = {}
    for publication in accepted:
        for artifact in publication.get("artifact_manifests", []):
            known[artifact["url"]] = (artifact.get("sha256"), None)
            for item in artifact.get("files", []):
                known[item["url"]] = (item.get("sha256"), item.get("bytes"))
    for artifact in new_publication.get("artifact_manifests", []):
        candidates = [(artifact["url"], artifact.get("sha256"), None)] + [
            (item["url"], item.get("sha256"), item.get("bytes"))
            for item in artifact.get("files", [])
        ]
        for url, digest, size in candidates:
            if url in known and known[url] != (digest, size):
                raise PublicationError(
                    f"an accepted artifact URL cannot be reused with different bytes: {url}"
                )


def _verify_accepted_publication_history(
    project_root: Path,
    schemas_directory: Path,
    accepted: Sequence[Mapping[str, Any]],
) -> None:
    for item in accepted:
        release_id = item["release_id"]
        release_directory = project_root / "data" / "releases" / release_id
        validate_release_directory(release_directory, schemas_directory)
        manifest = read_json(release_directory / "release.json")
        metadata = manifest.get("publication")
        if not isinstance(metadata, Mapping):
            raise PublicationError(
                f"accepted publication has no immutable binding: {release_id}"
            )
        verify_bound_publication(release_directory, schemas_directory, metadata)
        if _publication_history_record(release_id, metadata) != item:
            raise PublicationError(
                f"accepted publication history does not match release binding: {release_id}"
            )


@contextmanager
def _exclusive_publish_lock(project_root: Path):
    project_root = project_root.resolve(strict=True)
    data_directory = ensure_directory_under_root(project_root, "data")
    lock_path = data_directory / ".pipeline.lock"
    data_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    data_descriptor = os.open(data_directory, data_flags)
    try:
        try:
            descriptor = os.open(
                ".pipeline.lock",
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=data_descriptor,
            )
        except FileExistsError as exc:
            raise PublicationError(
                f"another publication is active (or left a stale lock): {lock_path}"
            ) from exc
        lock_stat = os.fstat(descriptor)
        lock_identity = (lock_stat.st_dev, lock_stat.st_ino)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(f"pid={os.getpid()}\n")
                handle.flush()
                os.fsync(handle.fileno())
            yield
        finally:
            try:
                current_lock = os.stat(
                    ".pipeline.lock", dir_fd=data_descriptor, follow_symlinks=False
                )
            except FileNotFoundError:
                current_lock = None
            if current_lock is not None and (
                current_lock.st_dev,
                current_lock.st_ino,
            ) == lock_identity:
                os.unlink(".pipeline.lock", dir_fd=data_descriptor)
                os.fsync(data_descriptor)
    finally:
        os.close(data_descriptor)


def _write_run_record(
    project_root: Path,
    run_id: str,
    value: Mapping[str, Any],
    schemas_directory: Path,
) -> None:
    validate_records([dict(value)], schemas_directory / "run.schema.json", run_id)
    data_directory = ensure_directory_under_root(project_root.resolve(strict=True), "data")
    runs_directory = ensure_directory_under_root(data_directory, "runs")
    path = runs_directory / f"{run_id}.json"
    if path.exists():
        raise PublicationError(f"run record already exists: {run_id}")
    _write_json(path, value)
    _fsync_directory(runs_directory)


def _replace_run_record(
    project_root: Path,
    run_id: str,
    value: Mapping[str, Any],
    schemas_directory: Path,
) -> None:
    validate_records([dict(value)], schemas_directory / "run.schema.json", run_id)
    data_directory = ensure_directory_under_root(project_root.resolve(strict=True), "data")
    runs_directory = ensure_directory_under_root(data_directory, "runs")
    path = runs_directory / f"{run_id}.json"
    if path.is_symlink() or not path.is_file():
        raise PublicationError(f"run record is unavailable for finalization: {run_id}")
    atomic_write_json(path, dict(value))
    _fsync_directory(runs_directory)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
