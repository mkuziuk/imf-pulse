"""Create and verify private, immutable snapshots of allowlisted source bytes."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .config import config_fingerprint, resolve_live_root
from .errors import SnapshotError
from .hashing import (
    canonical_json_bytes,
    canonical_json_hash,
    copy_exact_bytes_from_descriptor,
    sha256_file,
)
from .models import PipelineConfig, SnapshotEntry, SnapshotManifest
from .paths import (
    open_child_directory,
    open_absolute_directory_no_symlinks,
    open_directory_under_root,
    open_regular_file_under_root,
    resolve_private_path_under_root,
)

SNAPSHOT_ID_PATTERN = re.compile(r"^snapshot-[0-9a-f]{20}$")
IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._:-]*(?:-[a-z0-9._:-]+)*$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SNAPSHOT_MANIFEST_FIELDS = {
    "schema_version",
    "snapshot_id",
    "created_at",
    "root_id",
    "source_root_hint",
    "config_sha256",
    "manifest_sha256",
    "entries",
    "missing_optional_sources",
}
SNAPSHOT_ENTRY_FIELDS = {
    "source_id",
    "relative_path",
    "snapshot_path",
    "sha256",
    "size_bytes",
    "extractor",
}
SNAPSHOT_POINTER_FIELDS = {
    "schema_version",
    "snapshot_id",
    "snapshot_path",
    "updated_at",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_json_fsync(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(canonical_json_bytes(value) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())


def atomic_write_json(path: Path, value: Any) -> None:
    """Atomically replace JSON without following a substituted ancestor.

    Callers create the parent directory through the project's guarded output
    helpers first.  The final write then walks every absolute ancestor with
    ``O_NOFOLLOW`` and operates relative to the held parent descriptor.  A
    parent rename can therefore detach the write, but can never redirect it to
    another tree; the post-write identity check detects that detachment.
    """

    path = Path(os.path.abspath(os.fspath(path)))
    if not path.name or path.name in {".", ".."}:
        raise SnapshotError(f"unsafe JSON output path: {path}")
    payload = canonical_json_bytes(value) + b"\n"
    with open_absolute_directory_no_symlinks(path.parent) as parent_descriptor:
        parent_stat = os.fstat(parent_descriptor)
        parent_identity = (parent_stat.st_dev, parent_stat.st_ino)
        try:
            existing = os.stat(
                path.name, dir_fd=parent_descriptor, follow_symlinks=False
            )
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise SnapshotError(f"JSON output target is unsafe: {path}")
        _atomic_write_bytes_at(parent_descriptor, path.name, payload)
        if _read_regular_bytes_at(parent_descriptor, path.name) != payload:
            raise SnapshotError(f"JSON output verification failed: {path}")
        try:
            with open_absolute_directory_no_symlinks(path.parent) as rebound:
                rebound_stat = os.fstat(rebound)
                rebound_identity = (rebound_stat.st_dev, rebound_stat.st_ino)
        except SnapshotError as exc:
            raise SnapshotError(
                f"JSON output parent changed during replacement: {path.parent}"
            ) from exc
        if rebound_identity != parent_identity:
            raise SnapshotError(
                f"JSON output parent changed during replacement: {path.parent}"
            )


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    """Persist every generated directory entry before the immutable rename."""

    directories = [path for path in root.rglob("*") if path.is_dir()]
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        _fsync_directory(directory)
    _fsync_directory(root)


def _atomic_write_bytes_at(directory_descriptor: int, name: str, payload: bytes) -> None:
    temporary_name = f".{name}.{uuid.uuid4().hex}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary_name, flags, 0o600, dir_fd=directory_descriptor
        )
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary_name,
            name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        os.fsync(directory_descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory_descriptor)
        except FileNotFoundError:
            pass


def _atomic_write_json_at(directory_descriptor: int, name: str, value: Any) -> None:
    _atomic_write_bytes_at(
        directory_descriptor, name, canonical_json_bytes(value) + b"\n"
    )


def _read_regular_bytes_at(directory_descriptor: int, relative_path: str) -> bytes:
    from pathlib import PurePosixPath

    parts = PurePosixPath(relative_path).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise SnapshotError(f"unsafe snapshot path: {relative_path!r}")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
    opened: list[int] = [os.dup(directory_descriptor)]
    file_descriptor: int | None = None
    try:
        current = opened[0]
        for component in parts[:-1]:
            current = os.open(component, directory_flags, dir_fd=current)
            opened.append(current)
        before = os.stat(parts[-1], dir_fd=current, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise SnapshotError(f"snapshot entry is not a regular file: {relative_path}")
        file_descriptor = os.open(
            parts[-1], os.O_RDONLY | os.O_NONBLOCK | nofollow, dir_fd=current
        )
        after = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(after.st_mode)
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        ):
            raise SnapshotError(f"snapshot entry changed while opening: {relative_path}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(file_descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        finished = os.fstat(file_descriptor)
        if (
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (
                finished.st_dev,
                finished.st_ino,
                finished.st_size,
                finished.st_mtime_ns,
            )
            or total != after.st_size
        ):
            raise SnapshotError(f"snapshot entry changed while being read: {relative_path}")
        return b"".join(chunks)
    except SnapshotError:
        raise
    except OSError as exc:
        raise SnapshotError(f"snapshot entry is unavailable or unsafe: {relative_path}") from exc
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        for descriptor in reversed(opened):
            os.close(descriptor)


def _load_snapshot_manifest_from_descriptor(
    snapshot_descriptor: int,
) -> SnapshotManifest:
    try:
        raw = json.loads(
            _read_regular_bytes_at(snapshot_descriptor, "manifest.json").decode("utf-8"),
            parse_constant=_reject_non_finite,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
        raise SnapshotError(f"invalid snapshot manifest: {exc}") from exc
    _validate_snapshot_manifest_shape(raw)
    try:
        return SnapshotManifest.from_dict(raw)
    except (KeyError, TypeError, ValueError) as exc:
        raise SnapshotError(f"invalid snapshot manifest: {exc}") from exc


def _validate_snapshot_manifest_shape(raw: Any) -> None:
    if not isinstance(raw, dict) or set(raw) != SNAPSHOT_MANIFEST_FIELDS:
        raise SnapshotError("snapshot manifest fields do not match schema")
    if type(raw.get("schema_version")) is not int or raw["schema_version"] != 2:
        raise SnapshotError("snapshot manifest must use schema_version 2")
    for field in (
        "snapshot_id",
        "created_at",
        "root_id",
        "source_root_hint",
        "config_sha256",
        "manifest_sha256",
    ):
        if type(raw.get(field)) is not str or not raw[field]:
            raise SnapshotError(f"snapshot manifest field is invalid: {field}")
    if not SNAPSHOT_ID_PATTERN.fullmatch(raw["snapshot_id"]):
        raise SnapshotError("snapshot manifest id is invalid")
    if not IDENTIFIER_PATTERN.fullmatch(raw["root_id"]) or len(raw["root_id"]) > 200:
        raise SnapshotError("snapshot root id is invalid")
    for field in ("config_sha256", "manifest_sha256"):
        if not SHA256_PATTERN.fullmatch(raw[field]):
            raise SnapshotError(f"snapshot manifest field is invalid: {field}")
    try:
        created_at = datetime.fromisoformat(raw["created_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise SnapshotError("snapshot created_at is invalid") from exc
    if created_at.tzinfo is None:
        raise SnapshotError("snapshot created_at must include a timezone")
    if not Path(raw["source_root_hint"]).is_absolute():
        raise SnapshotError("snapshot source_root_hint must be absolute")
    if not isinstance(raw.get("entries"), list) or not raw["entries"]:
        raise SnapshotError("snapshot entries must be a non-empty array")
    if not isinstance(raw.get("missing_optional_sources"), list):
        raise SnapshotError("snapshot entries and missing sources must be arrays")
    for entry in raw["entries"]:
        if not isinstance(entry, dict) or set(entry) != SNAPSHOT_ENTRY_FIELDS:
            raise SnapshotError("snapshot entry fields do not match schema")
        for field in ("source_id", "relative_path", "snapshot_path", "sha256", "extractor"):
            if type(entry.get(field)) is not str or not entry[field]:
                raise SnapshotError(f"snapshot entry field is invalid: {field}")
        if not IDENTIFIER_PATTERN.fullmatch(entry["source_id"]) or len(entry["source_id"]) > 200:
            raise SnapshotError("snapshot entry source id is invalid")
        if not SHA256_PATTERN.fullmatch(entry["sha256"]):
            raise SnapshotError("snapshot entry sha256 is invalid")
        if type(entry.get("size_bytes")) is not int or entry["size_bytes"] < 0:
            raise SnapshotError("snapshot entry size_bytes is invalid")
        _validate_snapshot_relative_path(entry["relative_path"])
        _validate_snapshot_relative_path(entry["snapshot_path"])
    for source_id in raw["missing_optional_sources"]:
        if (
            type(source_id) is not str
            or not IDENTIFIER_PATTERN.fullmatch(source_id)
            or len(source_id) > 200
        ):
            raise SnapshotError("missing optional source id is invalid")


def _validate_snapshot_relative_path(value: str) -> None:
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or "\\" in value
        or not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise SnapshotError(f"unsafe snapshot path: {value!r}")


def _validate_snapshot_pointer(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != SNAPSHOT_POINTER_FIELDS:
        raise SnapshotError("snapshot pointer fields do not match schema")
    if type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
        raise SnapshotError("snapshot pointer must use schema_version 1")
    snapshot_id = raw.get("snapshot_id")
    if type(snapshot_id) is not str or not SNAPSHOT_ID_PATTERN.fullmatch(snapshot_id):
        raise SnapshotError("snapshot pointer contains an unsafe snapshot id")
    if raw.get("snapshot_path") != f"snapshots/{snapshot_id}":
        raise SnapshotError("snapshot pointer path does not match snapshot id")
    if type(raw.get("updated_at")) is not str:
        raise SnapshotError("snapshot pointer updated_at is invalid")
    try:
        updated_at = datetime.fromisoformat(raw["updated_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise SnapshotError("snapshot pointer updated_at is invalid") from exc
    if updated_at.tzinfo is None:
        raise SnapshotError("snapshot pointer updated_at must include a timezone")
    return raw


def _verify_snapshot_descriptor(
    snapshot_descriptor: int, manifest: SnapshotManifest
) -> None:
    _verify_snapshot_identity(manifest)
    expected_files = {"manifest.json", *(entry.snapshot_path for entry in manifest.entries)}
    expected_directories: set[str] = set()
    for relative in expected_files:
        parent = PurePosixPath(relative).parent
        while parent.as_posix() != ".":
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    actual_files, actual_directories = _snapshot_nodes(snapshot_descriptor)
    if actual_files != expected_files or actual_directories != expected_directories:
        raise SnapshotError("snapshot contains unlisted or missing filesystem nodes")
    for entry in manifest.entries:
        payload = _read_regular_bytes_at(snapshot_descriptor, entry.snapshot_path)
        if len(payload) != entry.size_bytes:
            raise SnapshotError(f"snapshot size mismatch for {entry.source_id}")
        if __import__("hashlib").sha256(payload).hexdigest() != entry.sha256:
            raise SnapshotError(f"snapshot hash mismatch for {entry.source_id}")


def _snapshot_nodes(
    directory_descriptor: int, prefix: str = ""
) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories: set[str] = set()
    try:
        names = os.listdir(directory_descriptor)
    except OSError as exc:
        raise SnapshotError("snapshot directory cannot be enumerated safely") from exc
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    for name in names:
        relative = f"{prefix}/{name}" if prefix else name
        try:
            node = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        except OSError as exc:
            raise SnapshotError(f"snapshot node changed while enumerating: {relative}") from exc
        if stat.S_ISREG(node.st_mode):
            files.add(relative)
        elif stat.S_ISDIR(node.st_mode):
            directories.add(relative)
            try:
                child = os.open(name, flags, dir_fd=directory_descriptor)
            except OSError as exc:
                raise SnapshotError(f"snapshot directory is unsafe: {relative}") from exc
            try:
                child_files, child_directories = _snapshot_nodes(child, relative)
            finally:
                os.close(child)
            files.update(child_files)
            directories.update(child_directories)
        else:
            raise SnapshotError(f"snapshot contains a forbidden node: {relative}")
    return files, directories


def _assert_snapshot_paths_still_bound(
    project_root: Path,
    snapshot_root: str,
    imports_descriptor: int,
    snapshots_descriptor: int,
    snapshot_descriptor: int,
    snapshot_id: str,
) -> None:
    with open_directory_under_root(project_root, snapshot_root) as checked_imports:
        if (os.fstat(checked_imports).st_dev, os.fstat(checked_imports).st_ino) != (
            os.fstat(imports_descriptor).st_dev,
            os.fstat(imports_descriptor).st_ino,
        ):
            raise SnapshotError("snapshot output path changed during synchronization")
        checked_snapshots = open_child_directory(checked_imports, "snapshots")
        try:
            if (
                os.fstat(checked_snapshots).st_dev,
                os.fstat(checked_snapshots).st_ino,
            ) != (
                os.fstat(snapshots_descriptor).st_dev,
                os.fstat(snapshots_descriptor).st_ino,
            ):
                raise SnapshotError("snapshots directory changed during synchronization")
            checked_snapshot = open_child_directory(checked_snapshots, snapshot_id)
            try:
                if (
                    os.fstat(checked_snapshot).st_dev,
                    os.fstat(checked_snapshot).st_ino,
                ) != (
                    os.fstat(snapshot_descriptor).st_dev,
                    os.fstat(snapshot_descriptor).st_ino,
                ):
                    raise SnapshotError("snapshot directory changed during synchronization")
            finally:
                os.close(checked_snapshot)
        finally:
            os.close(checked_snapshots)


def build_snapshot(
    config: PipelineConfig,
    project_root: Path,
    *,
    root_id: str = "imf",
    source_root_override: Path | None = None,
    update_pointer: bool = False,
) -> tuple[SnapshotManifest, Path, bool]:
    """Copy every configured byte into an immutable snapshot.

    Returns ``(manifest, snapshot_directory, created)``.  Snapshot identity is
    independent of timestamps and allowlist ordering.
    """

    if root_id not in config.roots:
        raise SnapshotError(f"unknown configured root: {root_id}")
    source_root = resolve_live_root(config, root_id, source_root_override)
    selected = tuple(
        sorted(
            (source for source in config.sources if source.root == root_id),
            key=lambda source: source.id,
        )
    )
    if not selected:
        raise SnapshotError(f"no sources configured for root {root_id}")
    try:
        project_root = project_root.resolve(strict=True)
        resolved_source_root = source_root.resolve(strict=True)
    except OSError as exc:
        raise SnapshotError(f"project or source root is unavailable: {exc}") from exc
    # This guard must precede every mkdir: the configured source tree is a
    # read-only boundary even when the requested snapshot is invalid.
    if (
        project_root == resolved_source_root
        or project_root in resolved_source_root.parents
        or resolved_source_root in project_root.parents
    ):
        raise SnapshotError("project and read-only source roots must not overlap")

    root_config = config.roots[root_id]
    imports_root = project_root.joinpath(*root_config.snapshot_root.split("/"))
    snapshots_root = imports_root / "snapshots"
    staging_path = Path(tempfile.mkdtemp(prefix="imf-pulse-snapshot-"))
    entries: list[SnapshotEntry] = []
    missing: list[str] = []
    try:
        for source in selected:
            try:
                with open_regular_file_under_root(source_root, source.path) as source_descriptor:
                    snapshot_relative = f"files/{source.path}"
                    destination = staging_path.joinpath(*snapshot_relative.split("/"))
                    sha256, size_bytes = copy_exact_bytes_from_descriptor(
                        source_descriptor, destination
                    )
            except FileNotFoundError as exc:
                if source.required:
                    raise SnapshotError(
                        f"required source is missing: {source.id} ({source.path})"
                    ) from exc
                missing.append(source.id)
                continue
            entries.append(
                SnapshotEntry(
                    source_id=source.id,
                    relative_path=source.path,
                    snapshot_path=snapshot_relative,
                    sha256=sha256,
                    size_bytes=size_bytes,
                    extractor=source.extractor,
                )
            )

        if not entries:
            raise SnapshotError("snapshot must contain at least one available source")

        identity_payload = {
            "schema_version": 2,
            "root_id": root_id,
            "config_sha256": config_fingerprint(config),
            "entries": [entry.as_dict() for entry in entries],
            "missing_optional_sources": sorted(missing),
        }
        content_sha256 = canonical_json_hash(identity_payload)
        snapshot_id = f"snapshot-{content_sha256[:20]}"
        manifest_without_digest = {
            "schema_version": 2,
            "snapshot_id": snapshot_id,
            "created_at": utc_now(),
            "root_id": root_id,
            "source_root_hint": str(resolved_source_root),
            "config_sha256": config_fingerprint(config),
            "entries": [entry.as_dict() for entry in entries],
            "missing_optional_sources": sorted(missing),
        }
        manifest = SnapshotManifest(
            schema_version=2,
            snapshot_id=snapshot_id,
            created_at=manifest_without_digest["created_at"],
            root_id=root_id,
            source_root_hint=str(resolved_source_root),
            config_sha256=config_fingerprint(config),
            manifest_sha256=canonical_json_hash(manifest_without_digest),
            entries=tuple(entries),
            missing_optional_sources=tuple(sorted(missing)),
        )
        _write_json_fsync(staging_path / "manifest.json", manifest.as_dict())
        _fsync_tree(staging_path)

        with open_directory_under_root(
            project_root, root_config.snapshot_root, create=True
        ) as imports_descriptor:
            snapshots_descriptor = open_child_directory(
                imports_descriptor, "snapshots", create=True
            )
            try:
                created = False
                try:
                    snapshot_descriptor = open_child_directory(
                        snapshots_descriptor, snapshot_id
                    )
                except SnapshotError:
                    try:
                        os.rename(
                            staging_path,
                            snapshot_id,
                            dst_dir_fd=snapshots_descriptor,
                        )
                    except OSError as exc:
                        raise SnapshotError(f"cannot install snapshot {snapshot_id}") from exc
                    os.fsync(snapshots_descriptor)
                    created = True
                    snapshot_descriptor = open_child_directory(
                        snapshots_descriptor, snapshot_id
                    )
                try:
                    existing = _load_snapshot_manifest_from_descriptor(snapshot_descriptor)
                    _verify_snapshot_descriptor(snapshot_descriptor, existing)
                    if _snapshot_content_sha256(existing) != content_sha256:
                        raise SnapshotError(f"snapshot id collision: {snapshot_id}")
                    manifest = existing
                    _assert_snapshot_paths_still_bound(
                        project_root,
                        root_config.snapshot_root,
                        imports_descriptor,
                        snapshots_descriptor,
                        snapshot_descriptor,
                        snapshot_id,
                    )
                    if update_pointer:
                        previous_pointer: bytes | None
                        try:
                            os.stat(
                                "current.json",
                                dir_fd=imports_descriptor,
                                follow_symlinks=False,
                            )
                        except FileNotFoundError:
                            previous_pointer = None
                        else:
                            previous_pointer = _read_regular_bytes_at(
                                imports_descriptor, "current.json"
                            )
                        pointer_value = {
                            "schema_version": 1,
                            "snapshot_id": snapshot_id,
                            "snapshot_path": f"snapshots/{snapshot_id}",
                            "updated_at": utc_now(),
                        }
                        intended_pointer = canonical_json_bytes(pointer_value) + b"\n"
                        try:
                            _atomic_write_json_at(
                                imports_descriptor,
                                "current.json",
                                pointer_value,
                            )
                        except BaseException:
                            # A wrapper or filesystem can fail after the atomic
                            # rename.  If our exact payload reached the name,
                            # restore the prior valid pointer before reporting
                            # failure.
                            try:
                                current_payload = _read_regular_bytes_at(
                                    imports_descriptor, "current.json"
                                )
                                current_pointer = os.stat(
                                    "current.json",
                                    dir_fd=imports_descriptor,
                                    follow_symlinks=False,
                                )
                                if current_payload == intended_pointer:
                                    _rollback_snapshot_pointer(
                                        imports_descriptor,
                                        previous_pointer,
                                        (current_pointer.st_dev, current_pointer.st_ino),
                                    )
                            except BaseException:
                                pass
                            raise
                        committed_pointer = os.stat(
                            "current.json",
                            dir_fd=imports_descriptor,
                            follow_symlinks=False,
                        )
                        try:
                            _verify_snapshot_descriptor(snapshot_descriptor, manifest)
                            _assert_snapshot_paths_still_bound(
                                project_root,
                                root_config.snapshot_root,
                                imports_descriptor,
                                snapshots_descriptor,
                                snapshot_descriptor,
                                snapshot_id,
                            )
                        except BaseException:
                            _rollback_snapshot_pointer(
                                imports_descriptor,
                                previous_pointer,
                                (committed_pointer.st_dev, committed_pointer.st_ino),
                            )
                            raise
                finally:
                    os.close(snapshot_descriptor)
            finally:
                os.close(snapshots_descriptor)
        final_path = snapshots_root / snapshot_id
        return manifest, final_path, created
    finally:
        if staging_path.exists():
            shutil.rmtree(staging_path)


def _rollback_snapshot_pointer(
    imports_descriptor: int,
    previous_payload: bytes | None,
    committed_identity: tuple[int, int],
) -> None:
    """Restore the pre-commit pointer only if our committed name is unchanged."""

    try:
        current = os.stat(
            "current.json", dir_fd=imports_descriptor, follow_symlinks=False
        )
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) != committed_identity:
        return
    if previous_payload is None:
        os.unlink("current.json", dir_fd=imports_descriptor)
        os.fsync(imports_descriptor)
    else:
        _atomic_write_bytes_at(imports_descriptor, "current.json", previous_payload)


def load_snapshot_manifest(snapshot_directory: Path) -> SnapshotManifest:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(snapshot_directory, flags)
    except OSError as exc:
        raise SnapshotError(
            f"cannot open snapshot directory safely: {snapshot_directory}"
        ) from exc
    try:
        return _load_snapshot_manifest_from_descriptor(descriptor)
    finally:
        os.close(descriptor)


def load_current_snapshot(project_root: Path, config: PipelineConfig, root_id: str = "imf") -> tuple[SnapshotManifest, Path]:
    if root_id not in config.roots:
        raise SnapshotError(f"unknown configured root: {root_id}")
    project_root = project_root.resolve(strict=True)
    snapshot_root = config.roots[root_id].snapshot_root
    imports_root = project_root.joinpath(*snapshot_root.split("/"))
    pointer_path = imports_root / "current.json"
    try:
        with open_directory_under_root(project_root, snapshot_root) as imports_descriptor:
            pointer = _validate_snapshot_pointer(
                json.loads(
                    _read_regular_bytes_at(imports_descriptor, "current.json").decode("utf-8"),
                    parse_constant=_reject_non_finite,
                    object_pairs_hook=_reject_duplicate_keys,
                )
            )
            relative = pointer["snapshot_path"]
            snapshot_id = pointer["snapshot_id"]
            snapshots_descriptor = open_child_directory(imports_descriptor, "snapshots")
            try:
                snapshot_descriptor = open_child_directory(snapshots_descriptor, snapshot_id)
                try:
                    manifest = _load_snapshot_manifest_from_descriptor(snapshot_descriptor)
                    if manifest.snapshot_id != snapshot_id:
                        raise SnapshotError("snapshot pointer and manifest disagree")
                    _verify_snapshot_descriptor(snapshot_descriptor, manifest)
                    _assert_snapshot_paths_still_bound(
                        project_root,
                        snapshot_root,
                        imports_descriptor,
                        snapshots_descriptor,
                        snapshot_descriptor,
                        snapshot_id,
                    )
                finally:
                    os.close(snapshot_descriptor)
            finally:
                os.close(snapshots_descriptor)
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
        raise SnapshotError(
            f"no valid exported snapshot at {pointer_path}; run export_local_snapshot.py explicitly"
        ) from exc
    snapshot_directory = imports_root.joinpath(*relative.split("/"))
    return manifest, snapshot_directory


def load_explicit_snapshot(
    project_root: Path,
    config: PipelineConfig,
    snapshot_directory: Path,
    root_id: str = "imf",
) -> tuple[SnapshotManifest, Path]:
    """Load one project-owned snapshot through no-follow ancestor descriptors."""

    if root_id not in config.roots:
        raise SnapshotError(f"unknown configured root: {root_id}")
    project_root = project_root.resolve(strict=True)
    candidate = Path(snapshot_directory)
    if candidate.is_absolute():
        try:
            relative = candidate.relative_to(project_root)
        except ValueError as exc:
            raise SnapshotError("explicit snapshot must be inside the project snapshot root") from exc
    else:
        relative = candidate
    pure = PurePosixPath(relative.as_posix())
    expected_parent = PurePosixPath(config.roots[root_id].snapshot_root) / "snapshots"
    if (
        len(pure.parts) != len(expected_parent.parts) + 1
        or pure.parts[: len(expected_parent.parts)] != expected_parent.parts
        or not SNAPSHOT_ID_PATTERN.fullmatch(pure.parts[-1])
    ):
        raise SnapshotError("explicit snapshot path is outside the configured snapshot root")
    snapshot_id = pure.parts[-1]
    with open_directory_under_root(
        project_root, expected_parent.as_posix()
    ) as snapshots_descriptor:
        snapshot_descriptor = open_child_directory(snapshots_descriptor, snapshot_id)
        try:
            manifest = _load_snapshot_manifest_from_descriptor(snapshot_descriptor)
            if manifest.snapshot_id != snapshot_id:
                raise SnapshotError("explicit snapshot path and manifest disagree")
            _verify_snapshot_descriptor(snapshot_descriptor, manifest)
        finally:
            os.close(snapshot_descriptor)
    return manifest, project_root.joinpath(*pure.parts)


def verify_snapshot(snapshot_directory: Path, manifest: SnapshotManifest) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(snapshot_directory, flags)
    except OSError as exc:
        raise SnapshotError(
            f"cannot open snapshot directory safely: {snapshot_directory}"
        ) from exc
    try:
        _verify_snapshot_descriptor(descriptor, manifest)
    finally:
        os.close(descriptor)


def _verify_snapshot_identity(manifest: SnapshotManifest) -> None:
    _validate_snapshot_manifest_shape(manifest.as_dict())
    expected_content_sha256 = _snapshot_content_sha256(manifest)
    if manifest.snapshot_id != f"snapshot-{expected_content_sha256[:20]}":
        raise SnapshotError("snapshot id does not match content identity")
    full_manifest = manifest.as_dict()
    supplied_manifest_sha256 = full_manifest.pop("manifest_sha256")
    if supplied_manifest_sha256 != canonical_json_hash(full_manifest):
        raise SnapshotError("snapshot manifest integrity hash mismatch")
    source_ids = [entry.source_id for entry in manifest.entries]
    relative_paths = [entry.relative_path for entry in manifest.entries]
    snapshot_paths = [entry.snapshot_path for entry in manifest.entries]
    if len(source_ids) != len(set(source_ids)):
        raise SnapshotError("snapshot contains duplicate source ids")
    if len(relative_paths) != len(set(relative_paths)):
        raise SnapshotError("snapshot contains duplicate relative paths")
    if len(snapshot_paths) != len(set(snapshot_paths)):
        raise SnapshotError("snapshot contains duplicate snapshot paths")
    if manifest.schema_version != 2:
        raise SnapshotError("snapshot schema version must be 2")
    if tuple(entry.source_id for entry in manifest.entries) != tuple(
        sorted(entry.source_id for entry in manifest.entries)
    ):
        raise SnapshotError("snapshot entries must use canonical source-id order")
    if tuple(manifest.missing_optional_sources) != tuple(
        sorted(manifest.missing_optional_sources)
    ):
        raise SnapshotError("missing optional sources must use canonical order")
    if set(source_ids).intersection(manifest.missing_optional_sources):
        raise SnapshotError("snapshot source cannot also be marked missing")
    if len(manifest.missing_optional_sources) != len(set(manifest.missing_optional_sources)):
        raise SnapshotError("snapshot contains duplicate missing source ids")
    for entry in manifest.entries:
        if entry.snapshot_path != f"files/{entry.relative_path}":
            raise SnapshotError(f"snapshot path does not preserve provenance for {entry.source_id}")


def _snapshot_content_sha256(manifest: SnapshotManifest) -> str:
    identity_payload = {
        "schema_version": manifest.schema_version,
        "root_id": manifest.root_id,
        "config_sha256": manifest.config_sha256,
        "entries": [entry.as_dict() for entry in manifest.entries],
        "missing_optional_sources": list(manifest.missing_optional_sources),
    }
    return canonical_json_hash(identity_payload)


def _reject_non_finite(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result
