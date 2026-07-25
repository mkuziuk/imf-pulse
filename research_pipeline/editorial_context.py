"""Build compact editorial context from sealed accepted publications.

The output is read-only context for the scheduled editor. It verifies the
accepted pulse archive, summarizes explicit coverage, and exposes sealed pulse
paths. The editor—not this module—decides which prior bodies are topically
related and whether a candidate is semantically new.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
from pathlib import Path
from typing import Any, Mapping

import yaml

from .config import UniqueKeySafeLoader
from .hashing import canonical_json_hash
from .paths import open_regular_file_under_root
from .validation import strict_json_loads


class EditorialContextError(RuntimeError):
    """The accepted editorial history is unavailable or inconsistent."""


RELEASE_ID_RE = re.compile(r"^release-[0-9a-f]{20}$")
PULSE_PATH_RE = re.compile(
    r"^content/pulses/(?P<date>\d{4}-\d{2}-\d{2})(?:-[1-9]\d*)?\.md$"
)
FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
SIGNAL_HEADING_RE = re.compile(
    r"^##\s+(Signal[^\n]+?)\s*$", re.MULTILINE | re.IGNORECASE
)
UNRESOLVED_RE = re.compile(
    r"^##\s+Unresolved question\s*$\n(?P<body>.*?)(?=^##\s+|\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)
MAX_POINTER_BYTES = 2 * 1024 * 1024
MAX_PULSE_BYTES = 2 * 1024 * 1024
MAX_JSONL_BYTES = 32 * 1024 * 1024
KNOWLEDGE_FILES = {
    "claims": "claims.jsonl",
    "methods": "methods.jsonl",
    "experiments": "experiments.jsonl",
    "relationships": "relationships.jsonl",
}


def _read_bytes(project_root: Path, relative_path: str, maximum: int) -> bytes:
    try:
        with open_regular_file_under_root(project_root, relative_path) as descriptor:
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum:
                    raise EditorialContextError(
                        f"editorial context input is too large: {relative_path}"
                    )
                chunks.append(chunk)
        return b"".join(chunks)
    except EditorialContextError:
        raise
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise EditorialContextError(
            f"editorial context input is unavailable or unsafe: {relative_path}"
        ) from exc


def _read_json(project_root: Path, relative_path: str, maximum: int) -> Any:
    try:
        return strict_json_loads(
            _read_bytes(project_root, relative_path, maximum).decode("utf-8")
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise EditorialContextError(f"invalid JSON input: {relative_path}") from exc


def _read_jsonl(project_root: Path, relative_path: str) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    payload = _read_bytes(project_root, relative_path, MAX_JSONL_BYTES)
    for line_number, line in enumerate(payload.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = strict_json_loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise EditorialContextError(
                f"invalid JSONL input: {relative_path}:{line_number}"
            ) from exc
        record_id = value.get("id") if isinstance(value, Mapping) else None
        if not isinstance(record_id, str) or not record_id or record_id in records:
            raise EditorialContextError(
                f"JSONL record has an invalid or duplicate id: "
                f"{relative_path}:{line_number}"
            )
        records[record_id] = dict(value)
    return records


def _plain_yaml(value: Any) -> Any:
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, list):
        return [_plain_yaml(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain_yaml(item) for key, item in value.items()}
    return value


def _parse_pulse(relative_path: str, payload: bytes) -> tuple[dict[str, Any], str]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EditorialContextError(f"accepted pulse is not UTF-8: {relative_path}") from exc
    match = FRONT_MATTER_RE.match(text)
    if not match:
        raise EditorialContextError(f"accepted pulse has no front matter: {relative_path}")
    try:
        frontmatter = yaml.load(match.group(1), Loader=UniqueKeySafeLoader)
    except yaml.YAMLError as exc:
        raise EditorialContextError(
            f"accepted pulse has invalid front matter: {relative_path}"
        ) from exc
    if not isinstance(frontmatter, dict):
        raise EditorialContextError(
            f"accepted pulse front matter is not an object: {relative_path}"
        )
    return _plain_yaml(frontmatter), text[match.end() :]


def _string_list(value: Any, field: str, pulse: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise EditorialContextError(f"accepted pulse has invalid {field}: {pulse}")
    return list(value)


def _knowledge_summary(kind: str, record: Mapping[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {"id": record["id"]}
    if kind == "claims":
        summary.update(
            text=record.get("normalized_text"),
            status=record.get("evidence_status"),
            statement_kind=record.get("statement_kind"),
        )
    elif kind == "methods":
        summary.update(
            name=record.get("name"),
            objective=record.get("objective"),
            estimator=record.get("estimator"),
        )
    elif kind == "experiments":
        summary.update(
            title=record.get("title"),
            observation_model=record.get("observation_model"),
            contamination_model=record.get("contamination_model"),
            metrics=record.get("metrics"),
            reference_target=record.get("reference_target"),
        )
    else:
        summary.update(
            predicate=record.get("predicate"),
            source=record.get("from"),
            target=record.get("to"),
            qualification=record.get("qualification"),
        )
    return {key: value for key, value in summary.items() if value is not None}


def _release_records(
    project_root: Path, release_id: str
) -> tuple[dict[str, dict[str, Any]], dict[str, tuple[str, dict[str, Any]]]]:
    prefix = f"data/releases/{release_id}"
    sources = _read_jsonl(project_root, f"{prefix}/sources.jsonl")
    knowledge: dict[str, tuple[str, dict[str, Any]]] = {}
    for kind, filename in KNOWLEDGE_FILES.items():
        for record_id, record in _read_jsonl(
            project_root, f"{prefix}/{filename}"
        ).items():
            if record_id in knowledge:
                raise EditorialContextError(
                    f"knowledge id appears in multiple collections: {record_id}"
                )
            knowledge[record_id] = (kind, record)
    return sources, knowledge


def build_editorial_context(project_root: Path) -> dict[str, Any]:
    """Verify and summarize the repository's sealed accepted pulse history."""

    project_root = project_root.resolve(strict=True)
    pointer = _read_json(project_root, "data/current.json", MAX_POINTER_BYTES)
    if not isinstance(pointer, Mapping):
        raise EditorialContextError("current release pointer must be an object")
    accepted = pointer.get("accepted_publications")
    if not isinstance(accepted, list):
        raise EditorialContextError("current pointer accepted_publications must be a list")
    if accepted and canonical_json_hash(accepted) != pointer.get(
        "accepted_publications_sha256"
    ):
        raise EditorialContextError("accepted publication history hash does not match")

    pulses: list[dict[str, Any]] = []
    source_versions: dict[tuple[str, str], dict[str, Any]] = {}
    knowledge_coverage: dict[str, dict[str, dict[str, Any]]] = {
        kind: {} for kind in KNOWLEDGE_FILES
    }
    knowledge_hashes: dict[str, tuple[str, str]] = {}
    release_cache: dict[
        str,
        tuple[dict[str, dict[str, Any]], dict[str, tuple[str, dict[str, Any]]]],
    ] = {}
    gaps: list[dict[str, str]] = []
    seen_pulses: set[str] = set()

    for item in accepted:
        if not isinstance(item, Mapping):
            raise EditorialContextError("accepted publication entry must be an object")
        release_id = item.get("release_id")
        pulse_path = item.get("pulse")
        pulse_sha256 = item.get("pulse_sha256")
        if not isinstance(release_id, str) or not RELEASE_ID_RE.fullmatch(release_id):
            raise EditorialContextError("accepted publication release id is unsafe")
        path_match = (
            PULSE_PATH_RE.fullmatch(pulse_path) if isinstance(pulse_path, str) else None
        )
        if path_match is None or pulse_path in seen_pulses:
            raise EditorialContextError("accepted publication pulse path is unsafe or repeated")
        bound_pulse = f"data/releases/{release_id}/publication/{pulse_path}"
        if item.get("bound_pulse") != bound_pulse:
            raise EditorialContextError("accepted publication bound pulse path is inconsistent")
        if not isinstance(pulse_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", pulse_sha256
        ):
            raise EditorialContextError("accepted publication pulse hash is invalid")
        pulse_bytes = _read_bytes(project_root, bound_pulse, MAX_PULSE_BYTES)
        if hashlib.sha256(pulse_bytes).hexdigest() != pulse_sha256:
            raise EditorialContextError("accepted publication pulse hash does not match")
        frontmatter, body = _parse_pulse(bound_pulse, pulse_bytes)
        if frontmatter.get("date") != path_match.group("date"):
            raise EditorialContextError("accepted pulse date does not match its path")
        for field in ("id", "title", "lead"):
            if not isinstance(frontmatter.get(field), str) or not frontmatter[field]:
                raise EditorialContextError(
                    f"accepted pulse has invalid {field}: {pulse_path}"
                )
        topics = _string_list(frontmatter.get("topics", []), "topics", pulse_path)
        source_ids = _string_list(
            frontmatter.get("source_ids", []), "source_ids", pulse_path
        )
        knowledge_ids = _string_list(
            frontmatter.get("knowledge_ids", []), "knowledge_ids", pulse_path
        )
        if not knowledge_ids:
            gaps.append(
                {
                    "pulse": pulse_path,
                    "reason": "accepted pulse does not declare knowledge_ids",
                }
            )

        if release_id not in release_cache:
            release_cache[release_id] = _release_records(project_root, release_id)
        sources, knowledge = release_cache[release_id]
        missing_sources = sorted(set(source_ids) - set(sources))
        missing_knowledge = sorted(set(knowledge_ids) - set(knowledge))
        if missing_sources or missing_knowledge:
            raise EditorialContextError(
                f"accepted pulse references missing release records: "
                f"sources={missing_sources}, knowledge={missing_knowledge}"
            )
        for source_id in source_ids:
            source = sources[source_id]
            digest = source.get("content_sha256") or source.get("content_hash")
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise EditorialContextError(
                    f"accepted source has no valid content hash: {source_id}"
                )
            summary = source_versions.setdefault(
                (source_id, digest),
                {
                    "source_id": source_id,
                    "content_sha256": digest,
                    "title": source.get("title"),
                    "topics": source.get("topics", []),
                    "pulses": [],
                },
            )
            summary["pulses"].append(pulse_path)
        for knowledge_id in knowledge_ids:
            kind, record = knowledge[knowledge_id]
            identity = (kind, canonical_json_hash(record))
            if knowledge_id in knowledge_hashes and knowledge_hashes[knowledge_id] != identity:
                raise EditorialContextError(
                    f"accepted knowledge id changed across releases: {knowledge_id}"
                )
            knowledge_hashes[knowledge_id] = identity
            summary = knowledge_coverage[kind].setdefault(
                knowledge_id,
                {**_knowledge_summary(kind, record), "pulses": []},
            )
            summary["pulses"].append(pulse_path)

        unresolved_match = UNRESOLVED_RE.search(body)
        unresolved = (
            re.sub(r"\s+", " ", unresolved_match.group("body")).strip()
            if unresolved_match
            else None
        )
        pulses.append(
            {
                "release_id": release_id,
                "pulse": pulse_path,
                "bound_pulse": bound_pulse,
                "pulse_sha256": pulse_sha256,
                "id": frontmatter["id"],
                "date": frontmatter["date"],
                "title": frontmatter["title"],
                "lead": frontmatter["lead"],
                "topics": topics,
                "source_ids": source_ids,
                "knowledge_ids": knowledge_ids,
                "signal_headings": [
                    re.sub(r"\s+", " ", heading).strip()
                    for heading in SIGNAL_HEADING_RE.findall(body)
                ],
                "unresolved_question": unresolved or None,
            }
        )
        seen_pulses.add(pulse_path)

    return {
        "schema_version": "1.0.0",
        "status": "ready",
        "accepted_pulse_count": len(pulses),
        "pulses": pulses,
        "coverage": {
            "source_ids": sorted({key[0] for key in source_versions}),
            "source_versions": sorted(
                source_versions.values(),
                key=lambda item: (item["source_id"], item["content_sha256"]),
            ),
            "knowledge_ids": sorted(knowledge_hashes),
            "topics": sorted({topic for pulse in pulses for topic in pulse["topics"]}),
            **{
                kind: sorted(records.values(), key=lambda item: item["id"])
                for kind, records in knowledge_coverage.items()
            },
            "unresolved_questions": [
                {"pulse": pulse["pulse"], "text": pulse["unresolved_question"]}
                for pulse in pulses
                if pulse["unresolved_question"]
            ],
            "coverage_gaps": gaps,
        },
    }
