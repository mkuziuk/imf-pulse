"""Deterministic hashing and exact-byte copying helpers."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, BinaryIO

from .errors import SnapshotError

DEFAULT_CHUNK_SIZE = 1024 * 1024


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize JSON deterministically and reject non-finite numbers."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_json_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def sha256_file(path: Path, *, chunk_size: int = DEFAULT_CHUNK_SIZE) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        _feed_digest(handle, digest, chunk_size)
    return digest.hexdigest()


def _feed_digest(handle: BinaryIO, digest: "hashlib._Hash", chunk_size: int) -> int:
    total = 0
    while True:
        chunk = handle.read(chunk_size)
        if not chunk:
            return total
        digest.update(chunk)
        total += len(chunk)


def copy_exact_bytes(source: Path, destination: Path) -> tuple[str, int]:
    """Copy and hash one stable regular file without following a changed source.

    The selected path has already been checked for symlinks.  We additionally
    compare file-descriptor metadata before and after reading so a concurrent
    edit cannot silently produce an incoherent snapshot.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with source.open("rb") as source_handle:
        before = os.fstat(source_handle.fileno())
        with destination.open("xb") as destination_handle:
            total = 0
            while True:
                chunk = source_handle.read(DEFAULT_CHUNK_SIZE)
                if not chunk:
                    break
                destination_handle.write(chunk)
                digest.update(chunk)
                total += len(chunk)
            destination_handle.flush()
            os.fsync(destination_handle.fileno())
        after = os.fstat(source_handle.fileno())

    stable_fields_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    stable_fields_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if stable_fields_before != stable_fields_after or total != before.st_size:
        destination.unlink(missing_ok=True)
        raise SnapshotError(f"source changed while being copied: {source}")
    return digest.hexdigest(), total


def copy_exact_bytes_from_descriptor(source_descriptor: int, destination: Path) -> tuple[str, int]:
    """Copy from an already no-follow-opened source descriptor."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    before = os.fstat(source_descriptor)
    os.lseek(source_descriptor, 0, os.SEEK_SET)
    with destination.open("xb") as destination_handle:
        total = 0
        while True:
            chunk = os.read(source_descriptor, DEFAULT_CHUNK_SIZE)
            if not chunk:
                break
            destination_handle.write(chunk)
            digest.update(chunk)
            total += len(chunk)
        destination_handle.flush()
        os.fsync(destination_handle.fileno())
    after = os.fstat(source_descriptor)
    stable_fields_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    stable_fields_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if stable_fields_before != stable_fields_after or total != before.st_size:
        destination.unlink(missing_ok=True)
        raise SnapshotError("source changed while being copied")
    return digest.hexdigest(), total
