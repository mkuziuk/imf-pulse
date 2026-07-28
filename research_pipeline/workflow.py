"""Private resumable state for the scheduled publication workflow."""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .hashing import canonical_json_bytes, canonical_json_hash
from .paths import open_directory_under_root, open_regular_file_under_root
from .validation import strict_json_loads, validate_records


STAGES = (
    "synchronize_base",
    "discover",
    "select",
    "materialize_source",
    "author",
    "validate",
    "publish_local",
    "commit",
    "push",
    "verify_deployment",
)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class WorkflowStateError(RuntimeError):
    """The private workflow state is unsafe or internally inconsistent."""


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _safe_reason(value: str) -> str:
    return (
        " ".join(value.replace("\r", " ").replace("\n", " ").split())[:1000]
        or "unknown failure"
    )


def _read_json(project_root: Path, relative: str) -> dict[str, Any]:
    try:
        with open_regular_file_under_root(project_root, relative) as descriptor:
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, 64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > 2 * 1024 * 1024:
                    raise WorkflowStateError("workflow state exceeds the size limit")
                chunks.append(chunk)
        value = strict_json_loads(b"".join(chunks).decode("utf-8"))
    except WorkflowStateError:
        raise
    except Exception as exc:
        raise WorkflowStateError("workflow state is unavailable or invalid") from exc
    if not isinstance(value, dict):
        raise WorkflowStateError("workflow state must be an object")
    return value


def _replace_json(project_root: Path, relative: str, value: Mapping[str, Any]) -> None:
    directory_relative, filename = relative.rsplit("/", 1)
    payload = canonical_json_bytes(value) + b"\n"
    temporary = f".{filename}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    with open_directory_under_root(project_root, directory_relative, create=True) as directory:
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory,
            )
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise WorkflowStateError("workflow temporary output is unsafe")
            offset = 0
            while offset < len(payload):
                offset += os.write(descriptor, payload[offset:])
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, filename, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def _install_immutable_json(
    project_root: Path, relative: str, value: Mapping[str, Any]
) -> None:
    directory_relative, filename = relative.rsplit("/", 1)
    payload = canonical_json_bytes(value) + b"\n"
    with open_directory_under_root(project_root, directory_relative, create=True) as directory:
        try:
            descriptor = os.open(
                filename,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory,
            )
        except FileExistsError:
            existing = _read_json(project_root, relative)
            if canonical_json_bytes(existing) + b"\n" != payload:
                raise WorkflowStateError("immutable workflow stage receipt conflicts")
            return
        try:
            offset = 0
            while offset < len(payload):
                offset += os.write(descriptor, payload[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(directory)


class WorkflowStore:
    """Validate and atomically advance one date-scoped scheduled run."""

    def __init__(self, project_root: Path, run_date: str) -> None:
        if not DATE_RE.fullmatch(run_date):
            raise WorkflowStateError("workflow date must use YYYY-MM-DD")
        self.project_root = project_root.resolve(strict=True)
        self.date = run_date
        self.run_id = f"scheduled-{run_date}"
        self.relative = f"data/automatic/workflow/{self.run_id}/manifest.json"
        self.schema = self.project_root / "schemas" / "scheduled-workflow.schema.json"
        self.receipt_schema = (
            self.project_root / "schemas" / "scheduled-stage-result.schema.json"
        )
        self.value = self._load_or_create()

    def _load_or_create(self) -> dict[str, Any]:
        path = self.project_root / self.relative
        if path.exists():
            value = _read_json(self.project_root, self.relative)
            self._validate(value)
            self._validate_receipts(value)
            return self._recover_orphan_receipts(value)
        now = utc_now()
        value: dict[str, Any] = {
            "schema_version": "1.0.0",
            "run_id": self.run_id,
            "date": self.date,
            "created_at": now,
            "updated_at": now,
            "status": "active",
            "next_stage": STAGES[0],
            "stages": {},
            "failure": None,
            "outcome": None,
        }
        self._write(value)
        return value

    def _recover_orphan_receipts(self, value: dict[str, Any]) -> dict[str, Any]:
        directory = (
            self.project_root
            / "data"
            / "automatic"
            / "workflow"
            / self.run_id
            / "stages"
        )
        if not directory.exists():
            return value
        node = directory.lstat()
        if not stat.S_ISDIR(node.st_mode) or stat.S_ISLNK(node.st_mode):
            raise WorkflowStateError("workflow stage receipt directory is unsafe")
        updated = dict(value)
        stages = dict(updated["stages"])
        changed = False
        for path in sorted(directory.glob("*.json")):
            relative = path.relative_to(self.project_root).as_posix()
            receipt = _read_json(self.project_root, relative)
            validate_records([receipt], self.receipt_schema, "scheduled stage receipt")
            identity = {
                key: item for key, item in receipt.items() if key != "receipt_sha256"
            }
            stage = receipt.get("stage")
            if (
                receipt.get("run_id") != self.run_id
                or stage not in STAGES
                or receipt.get("receipt_sha256") != canonical_json_hash(identity)
            ):
                raise WorkflowStateError("orphan workflow stage receipt is invalid")
            reference = {
                "status": "completed",
                "input_sha256": receipt["input_sha256"],
                "receipt": relative,
                "receipt_sha256": receipt["receipt_sha256"],
                "outputs": receipt["outputs"],
            }
            existing = stages.get(stage)
            if existing is not None and existing != reference:
                raise WorkflowStateError("workflow has conflicting stage receipts")
            if existing is None:
                stages[str(stage)] = reference
                changed = True
        if not changed:
            return value
        updated["stages"] = stages
        updated["next_stage"] = next(
            (stage_name for stage_name in STAGES if stage_name not in stages), None
        )
        failure = updated.get("failure")
        if isinstance(failure, Mapping) and failure.get("stage") in stages:
            updated["failure"] = None
            updated["status"] = "active"
        self._write(updated)
        return self.value

    def _validate(self, value: Mapping[str, Any]) -> None:
        validate_records([value], self.schema, "scheduled workflow")
        if value.get("run_id") != self.run_id or value.get("date") != self.date:
            raise WorkflowStateError("workflow identity does not match its path")

    def _validate_receipts(self, value: Mapping[str, Any]) -> None:
        stages = value.get("stages", {})
        if not isinstance(stages, Mapping):
            raise WorkflowStateError("workflow stages are malformed")
        for name, reference in stages.items():
            if name not in STAGES or not isinstance(reference, Mapping):
                raise WorkflowStateError("workflow stage reference is malformed")
            receipt = _read_json(self.project_root, str(reference["receipt"]))
            validate_records([receipt], self.receipt_schema, "scheduled stage receipt")
            identity = {key: item for key, item in receipt.items() if key != "receipt_sha256"}
            if (
                receipt.get("receipt_sha256") != canonical_json_hash(identity)
                or receipt.get("receipt_sha256") != reference.get("receipt_sha256")
                or receipt.get("stage") != name
                or receipt.get("input_sha256") != reference.get("input_sha256")
                or receipt.get("outputs") != reference.get("outputs")
            ):
                raise WorkflowStateError("workflow stage receipt identity does not match")

    def _write(self, value: dict[str, Any]) -> None:
        value["updated_at"] = utc_now()
        self._validate(value)
        _replace_json(self.project_root, self.relative, value)
        self.value = value

    def stage(self, name: str) -> Mapping[str, Any] | None:
        value = self.value["stages"].get(name)
        return value if isinstance(value, Mapping) else None

    def complete_stage(
        self, name: str, inputs: Mapping[str, Any], outputs: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        if name not in STAGES:
            raise WorkflowStateError("unknown workflow stage")
        input_sha = canonical_json_hash(inputs)
        existing = self.stage(name)
        if existing is not None:
            if existing.get("input_sha256") != input_sha:
                raise WorkflowStateError(
                    f"completed workflow stage has different inputs: {name}"
                )
            return existing
        ordinal = STAGES.index(name) + 1
        relative = (
            f"data/automatic/workflow/{self.run_id}/stages/"
            f"{ordinal:02d}-{name}-{input_sha[:12]}.json"
        )
        receipt: dict[str, Any] = {
            "schema_version": "1.0.0",
            "run_id": self.run_id,
            "stage": name,
            "input_sha256": input_sha,
            "completed_at": utc_now(),
            "outputs": dict(outputs),
        }
        receipt["receipt_sha256"] = canonical_json_hash(receipt)
        validate_records([receipt], self.receipt_schema, "scheduled stage receipt")
        _install_immutable_json(self.project_root, relative, receipt)
        updated = dict(self.value)
        stages = dict(updated["stages"])
        stages[name] = {
            "status": "completed",
            "input_sha256": input_sha,
            "receipt": relative,
            "receipt_sha256": receipt["receipt_sha256"],
            "outputs": dict(outputs),
        }
        updated["stages"] = stages
        updated["next_stage"] = next(
            (stage_name for stage_name in STAGES if stage_name not in stages), None
        )
        updated["status"] = "active"
        updated["failure"] = None
        self._write(updated)
        return stages[name]

    def record_failure(
        self,
        *,
        stage: str,
        classification: str,
        code: str,
        reason: str,
        retry_not_before: str | None = None,
    ) -> None:
        if stage not in STAGES or classification not in {
            "retryable",
            "deferred",
            "terminal",
        }:
            raise WorkflowStateError("invalid workflow failure classification")
        previous = self.value.get("failure")
        attempt = (
            int(previous.get("attempt", 0)) + 1
            if isinstance(previous, Mapping)
            and previous.get("stage") == stage
            and previous.get("code") == code
            else 1
        )
        updated = dict(self.value)
        updated["status"] = "terminal" if classification == "terminal" else (
            "deferred" if classification == "deferred" else "active"
        )
        updated["next_stage"] = stage
        updated["failure"] = {
            "classification": classification,
            "code": code,
            "stage": stage,
            "reason": _safe_reason(reason),
            "attempt": attempt,
            "resume_from": stage,
            "retry_not_before": retry_not_before,
        }
        self._write(updated)

    def complete(self, status: str, reason: str, **outputs: Any) -> None:
        if status not in {"published", "no_update", "review_required", "failed"}:
            raise WorkflowStateError("invalid workflow outcome")
        updated = dict(self.value)
        updated["status"] = "completed" if status != "failed" else "terminal"
        updated["next_stage"] = None
        updated["failure"] = None
        updated["outcome"] = {"status": status, "reason": _safe_reason(reason), **outputs}
        self._write(updated)

    def as_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.value))
