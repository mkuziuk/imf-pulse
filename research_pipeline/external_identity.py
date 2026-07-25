"""Canonical identities for external sources already accepted by the project."""

from __future__ import annotations

import os
import re
import urllib.parse
from pathlib import Path
from typing import Any, Mapping

from .paths import open_regular_file_under_root
from .validation import strict_json_loads


RELEASE_PATH_RE = re.compile(r"^data/releases/release-[0-9a-f]{20}$")
MAX_POINTER_BYTES = 2 * 1024 * 1024
MAX_SOURCES_BYTES = 32 * 1024 * 1024


def normalize_external_identity(value: Any) -> str | None:
    """Return a provider/version identity for one reviewed canonical URL."""

    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        return None
    host = (parsed.hostname or "").casefold()
    if host == "arxiv.org" and parsed.path.startswith("/abs/"):
        external_id = urllib.parse.unquote(parsed.path.removeprefix("/abs/"))
        if external_id and "/" not in external_id.strip("/"):
            return f"arxiv:{external_id.casefold()}"
        # Legacy arXiv identifiers contain one slash after the archive name.
        if external_id and external_id.count("/") == 1:
            return f"arxiv:{external_id.casefold()}"
    if host == "doi.org" and parsed.path.startswith("/"):
        doi = urllib.parse.unquote(parsed.path[1:]).strip().casefold()
        if doi.startswith("10.") and "/" in doi:
            return f"doi:{doi}"
    return None


def _read_regular_bytes(
    project_root: Path, relative_path: str, *, maximum_bytes: int
) -> bytes:
    with open_regular_file_under_root(project_root, relative_path) as descriptor:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise ValueError(f"accepted external identity input is too large: {relative_path}")
            chunks.append(chunk)
    return b"".join(chunks)


def _checkpoint(
    project_root: Path, supplied: Mapping[str, Any] | None
) -> Mapping[str, Any] | None:
    if supplied is not None:
        return supplied
    try:
        payload = _read_regular_bytes(
            project_root, "data/current.json", maximum_bytes=MAX_POINTER_BYTES
        )
    except (FileNotFoundError, OSError):
        return None
    value = strict_json_loads(payload.decode("utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("accepted release pointer must be an object")
    return value


def accepted_external_identities(
    project_root: Path, checkpoint: Mapping[str, Any] | None = None
) -> frozenset[str]:
    """Read exact external source versions from the immutable accepted release."""

    pointer = _checkpoint(project_root, checkpoint)
    if pointer is None:
        return frozenset()
    release_path = pointer.get("release_path")
    if not isinstance(release_path, str) or not RELEASE_PATH_RE.fullmatch(release_path):
        # Minimal unit-test checkpoints and bootstrap state may not name a release.
        return frozenset()
    payload = _read_regular_bytes(
        project_root,
        f"{release_path}/sources.jsonl",
        maximum_bytes=MAX_SOURCES_BYTES,
    )
    identities: set[str] = set()
    for line_number, line in enumerate(payload.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = strict_json_loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError(
                f"accepted source record is invalid at line {line_number}"
            ) from exc
        if not isinstance(record, Mapping):
            raise ValueError("accepted source record must be an object")
        candidates = [record.get("url"), record.get("location")]
        rights = record.get("rights")
        if isinstance(rights, Mapping):
            candidates.append(rights.get("source_url"))
        identities.update(
            identity
            for candidate in candidates
            if (identity := normalize_external_identity(candidate)) is not None
        )
    return frozenset(identities)
