"""Hash-bound handoff from the scheduled metadata search to the daily transaction."""

from __future__ import annotations

import os
import re
import stat
import uuid
from datetime import date as calendar_date, timezone
from pathlib import Path
from typing import Any, Mapping

from .external import parse_as_of, validate_batch_integrity
from .hashing import canonical_json_bytes, canonical_json_hash
from .paths import open_directory_under_root, open_regular_file_under_root
from .validation import strict_json_loads, validate_records


OUTCOME_ROOT = "data/automatic/external-search-outcomes"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
BATCH_ID_RE = re.compile(r"^external-batch-[0-9a-f]{20}$")
BATCH_PATH_RE = re.compile(
    r"^data/external/batches/external-batch-[0-9a-f]{20}\.json$"
)


class ExternalPreflightError(RuntimeError):
    """A scheduled metadata-search handoff is missing integrity or safety data."""


def scheduled_outcome_path(run_date: str) -> str:
    _validate_date(run_date)
    return f"{OUTCOME_ROOT}/{run_date}.json"


def _validate_date(value: str) -> str:
    if not DATE_RE.fullmatch(value):
        raise ExternalPreflightError("scheduled outcome date must use YYYY-MM-DD")
    try:
        parsed = calendar_date.fromisoformat(value)
    except ValueError as exc:
        raise ExternalPreflightError("scheduled outcome date must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ExternalPreflightError("scheduled outcome date must use YYYY-MM-DD")
    return value


def _canonical_as_of(value: str) -> str:
    parsed = parse_as_of(value).astimezone(timezone.utc)
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")


def _expected_as_of(run_date: str) -> str:
    return _canonical_as_of(f"{run_date}T06:00:00+03:00")


def _identity_payload(outcome: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: outcome[key]
        for key in (
            "schema_version",
            "date",
            "as_of",
            "status",
            "reason",
            "batch_id",
            "batch_path",
            "batch_sha256",
        )
    }


def _read_json_under_root(project_root: Path, relative_path: str) -> dict[str, Any]:
    try:
        with open_regular_file_under_root(project_root, relative_path) as descriptor:
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, 64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > 2 * 1024 * 1024:
                    raise ExternalPreflightError("scheduled outcome input is too large")
                chunks.append(chunk)
        value = strict_json_loads(b"".join(chunks).decode("utf-8"))
    except ExternalPreflightError:
        raise
    except Exception as exc:
        raise ExternalPreflightError("scheduled outcome input is unavailable or invalid") from exc
    if not isinstance(value, Mapping):
        raise ExternalPreflightError("scheduled outcome input must be an object")
    return dict(value)


def _validated_batch(
    project_root: Path,
    relative_path: str,
    *,
    expected_id: str,
    expected_sha256: str,
    expected_as_of: str,
) -> dict[str, Any]:
    if not BATCH_PATH_RE.fullmatch(relative_path):
        raise ExternalPreflightError("scheduled outcome batch path is unsafe")
    batch = _read_json_under_root(project_root, relative_path)
    try:
        validate_batch_integrity(batch)
    except Exception as exc:
        raise ExternalPreflightError("scheduled outcome batch failed integrity validation") from exc
    if (
        batch.get("id") != expected_id
        or batch.get("batch_sha256") != expected_sha256
        or batch.get("as_of") != expected_as_of
    ):
        raise ExternalPreflightError("scheduled outcome does not bind the exact metadata batch")
    return batch


def _replace_private_json(
    project_root: Path, relative_path: str, value: Mapping[str, Any]
) -> None:
    directory_relative, filename = relative_path.rsplit("/", 1)
    payload = canonical_json_bytes(value) + b"\n"
    temporary = f".{filename}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    with open_directory_under_root(project_root, directory_relative, create=True) as directory:
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory,
            )
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise ExternalPreflightError("scheduled outcome temporary file is unsafe")
            offset = 0
            while offset < len(payload):
                offset += os.write(descriptor, payload[offset:])
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(
                temporary,
                filename,
                src_dir_fd=directory,
                dst_dir_fd=directory,
            )
            os.fsync(directory)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def write_scheduled_search_outcome(
    project_root: Path,
    *,
    run_date: str,
    as_of: str,
    status: str,
    reason: str,
    search_result: Mapping[str, Any] | None = None,
) -> str:
    """Replace today's private handoff with one schema-valid, hash-bound outcome."""

    run_date = _validate_date(run_date)
    canonical_as_of = _canonical_as_of(as_of)
    if canonical_as_of != _expected_as_of(run_date):
        raise ExternalPreflightError(
            "scheduled outcome cutoff must be 06:00 Europe/Moscow on its date"
        )
    if status not in {"ready", "deferred", "failed"}:
        raise ExternalPreflightError("scheduled outcome status is invalid")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise ExternalPreflightError("scheduled outcome reason is invalid")

    batch_id: str | None = None
    batch_path: str | None = None
    batch_sha256: str | None = None
    if status == "ready":
        if not isinstance(search_result, Mapping):
            raise ExternalPreflightError("ready scheduled outcome requires a search result")
        batch_id = search_result.get("batch_id")
        batch_path = search_result.get("batch_path")
        if not isinstance(batch_id, str) or not BATCH_ID_RE.fullmatch(batch_id):
            raise ExternalPreflightError("scheduled outcome batch id is invalid")
        if not isinstance(batch_path, str) or not BATCH_PATH_RE.fullmatch(batch_path):
            raise ExternalPreflightError("scheduled outcome batch path is invalid")
        batch = _read_json_under_root(project_root, batch_path)
        try:
            validate_batch_integrity(batch)
        except Exception as exc:
            raise ExternalPreflightError("scheduled outcome batch failed integrity validation") from exc
        batch_sha256 = batch.get("batch_sha256")
        if (
            batch.get("id") != batch_id
            or not isinstance(batch_sha256, str)
            or not SHA256_RE.fullmatch(batch_sha256)
            or batch.get("as_of") != canonical_as_of
        ):
            raise ExternalPreflightError("search result does not bind the expected metadata batch")
    elif search_result is not None:
        raise ExternalPreflightError("non-ready scheduled outcome must not bind a batch")

    outcome: dict[str, Any] = {
        "schema_version": "1.0.0",
        "date": run_date,
        "as_of": canonical_as_of,
        "status": status,
        "reason": " ".join(reason.split()),
        "batch_id": batch_id,
        "batch_path": batch_path,
        "batch_sha256": batch_sha256,
    }
    outcome["outcome_sha256"] = canonical_json_hash(_identity_payload(outcome))
    validate_records(
        [outcome],
        project_root / "schemas" / "external-search-outcome.schema.json",
        "external-search-outcome",
    )
    relative_path = scheduled_outcome_path(run_date)
    _replace_private_json(project_root, relative_path, outcome)
    return relative_path


def load_scheduled_search_outcome(
    project_root: Path, relative_path: str, *, run_date: str
) -> dict[str, Any]:
    """Load and revalidate the exact scheduled handoff and its bound batch."""

    expected_path = scheduled_outcome_path(run_date)
    if relative_path != expected_path:
        raise ExternalPreflightError("scheduled outcome path is not the deterministic date path")
    outcome = _read_json_under_root(project_root, relative_path)
    validate_records(
        [outcome],
        project_root / "schemas" / "external-search-outcome.schema.json",
        "external-search-outcome",
    )
    if (
        outcome.get("date") != run_date
        or outcome.get("as_of") != _expected_as_of(run_date)
        or outcome.get("outcome_sha256")
        != canonical_json_hash(_identity_payload(outcome))
    ):
        raise ExternalPreflightError("scheduled outcome identity hash does not match content")

    if outcome["status"] == "ready":
        _validated_batch(
            project_root,
            outcome["batch_path"],
            expected_id=outcome["batch_id"],
            expected_sha256=outcome["batch_sha256"],
            expected_as_of=outcome["as_of"],
        )
    return outcome


def load_ready_scheduled_search_batch(
    project_root: Path, relative_path: str, *, run_date: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load a ready scheduled outcome together with its exact immutable batch."""

    outcome = load_scheduled_search_outcome(
        project_root, relative_path, run_date=run_date
    )
    if outcome["status"] != "ready":
        raise ExternalPreflightError(
            "automatic package validation requires a ready metadata outcome"
        )
    batch = _validated_batch(
        project_root,
        outcome["batch_path"],
        expected_id=outcome["batch_id"],
        expected_sha256=outcome["batch_sha256"],
        expected_as_of=outcome["as_of"],
    )
    return outcome, batch
