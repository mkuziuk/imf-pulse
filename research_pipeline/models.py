"""Small immutable models shared by configuration, snapshots, and releases."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class RootConfig:
    id: str
    live_path_env: str | None
    default_live_path: str | None
    snapshot_root: str
    access: str = "read_only"


@dataclass(frozen=True)
class SourceConfig:
    id: str
    root: str
    path: str
    title: str
    authors: tuple[str, ...]
    date: str | None
    source_type: str
    authority_level: str
    publication_status: str
    topics: tuple[str, ...]
    rights: Mapping[str, Any]
    limitations: tuple[str, ...]
    extractor: str
    required: bool = True


@dataclass(frozen=True)
class PipelineConfig:
    path: Path
    version: int
    roots: Mapping[str, RootConfig]
    sources: tuple[SourceConfig, ...]
    policy: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SnapshotEntry:
    source_id: str
    relative_path: str
    snapshot_path: str
    sha256: str
    size_bytes: int
    extractor: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "relative_path": self.relative_path,
            "snapshot_path": self.snapshot_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "extractor": self.extractor,
        }


@dataclass(frozen=True)
class SnapshotManifest:
    schema_version: int
    snapshot_id: str
    created_at: str
    root_id: str
    source_root_hint: str
    config_sha256: str
    manifest_sha256: str
    entries: tuple[SnapshotEntry, ...]
    missing_optional_sources: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "created_at": self.created_at,
            "root_id": self.root_id,
            "source_root_hint": self.source_root_hint,
            "config_sha256": self.config_sha256,
            "manifest_sha256": self.manifest_sha256,
            "entries": [entry.as_dict() for entry in self.entries],
            "missing_optional_sources": list(self.missing_optional_sources),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SnapshotManifest":
        return cls(
            schema_version=value["schema_version"],
            snapshot_id=value["snapshot_id"],
            created_at=value["created_at"],
            root_id=value["root_id"],
            source_root_hint=value["source_root_hint"],
            config_sha256=value["config_sha256"],
            manifest_sha256=value["manifest_sha256"],
            entries=tuple(
                SnapshotEntry(
                    source_id=entry["source_id"],
                    relative_path=entry["relative_path"],
                    snapshot_path=entry["snapshot_path"],
                    sha256=entry["sha256"],
                    size_bytes=entry["size_bytes"],
                    extractor=entry["extractor"],
                )
                for entry in value["entries"]
            ),
            missing_optional_sources=tuple(value["missing_optional_sources"]),
        )
