"""Load and validate the small, explicit YAML configuration surface."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Mapping

import yaml

from .errors import ConfigurationError
from .hashing import canonical_json_hash
from .models import PipelineConfig, RootConfig, SourceConfig
from .paths import validate_relative_path

IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")


class UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that refuses ambiguous duplicate mapping keys."""


def _construct_unique_mapping(loader: UniqueKeySafeLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{name} must be a mapping")
    return value


def _string(value: Any, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ConfigurationError(f"{name} must be a non-empty string")
    return value


def _identifier(value: Any, name: str) -> str:
    value = _string(value, name)
    if not IDENTIFIER_PATTERN.fullmatch(value):
        raise ConfigurationError(
            f"{name} must be a lowercase hyphenated identifier, got {value!r}"
        )
    return value


def load_yaml(path: Path) -> Mapping[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = yaml.load(handle, Loader=UniqueKeySafeLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"cannot load YAML configuration {path}: {exc}") from exc
    return _mapping(value, str(path))


def load_pipeline_config(path: Path) -> PipelineConfig:
    raw = load_yaml(path)
    version = raw.get("version")
    if version != 1:
        raise ConfigurationError(f"unsupported sources config version: {version!r}")

    raw_roots = _mapping(raw.get("roots"), "roots")
    roots: dict[str, RootConfig] = {}
    for root_id, value in raw_roots.items():
        root_id = _identifier(root_id, "root id")
        root = _mapping(value, f"root {root_id}")
        access = root.get("access", "read_only")
        if access != "read_only":
            raise ConfigurationError(f"root {root_id} must use read_only access")
        snapshot_root = validate_relative_path(
            _string(root.get("snapshot_root", f"imports/{root_id}"), "snapshot_root")
        )
        live_path_env = root.get("live_path_env")
        default_live_path = root.get("default_live_path")
        if live_path_env is not None:
            live_path_env = _string(live_path_env, f"root {root_id}.live_path_env")
        if default_live_path is not None:
            default_live_path = _string(default_live_path, f"root {root_id}.default_live_path")
        if live_path_env is None and default_live_path is None:
            raise ConfigurationError(f"root {root_id} needs a live path or environment variable")
        roots[root_id] = RootConfig(
            id=root_id,
            live_path_env=live_path_env,
            default_live_path=default_live_path,
            snapshot_root=snapshot_root,
            access=access,
        )

    raw_sources = raw.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ConfigurationError("sources must be a non-empty list")
    sources: list[SourceConfig] = []
    seen_ids: set[str] = set()
    seen_paths: set[tuple[str, str]] = set()
    for index, value in enumerate(raw_sources):
        source = _mapping(value, f"sources[{index}]")
        source_id = _identifier(source.get("id"), f"sources[{index}].id")
        if source_id in seen_ids:
            raise ConfigurationError(f"duplicate source id: {source_id}")
        root_id = _identifier(source.get("root"), f"source {source_id}.root")
        if root_id not in roots:
            raise ConfigurationError(f"source {source_id} references unknown root {root_id}")
        relative_path = validate_relative_path(
            _string(source.get("path"), f"source {source_id}.path")
        )
        if (root_id, relative_path) in seen_paths:
            raise ConfigurationError(f"source path registered twice: {root_id}:{relative_path}")
        authors = source.get("authors", [])
        topics = source.get("topics", [])
        limitations = source.get("limitations", [])
        if not isinstance(authors, list) or not all(isinstance(item, str) for item in authors):
            raise ConfigurationError(f"source {source_id}.authors must be a string list")
        if not isinstance(topics, list) or not topics or not all(isinstance(item, str) for item in topics):
            raise ConfigurationError(f"source {source_id}.topics must be a non-empty string list")
        if not isinstance(limitations, list) or not all(
            isinstance(item, str) and item.strip() for item in limitations
        ):
            raise ConfigurationError(f"source {source_id}.limitations must be a string list")
        date = source.get("date")
        if date is not None and not isinstance(date, (str, int)):
            raise ConfigurationError(f"source {source_id}.date must be a date string or null")
        required = source.get("required", True)
        if not isinstance(required, bool):
            raise ConfigurationError(f"source {source_id}.required must be a boolean")
        sources.append(
            SourceConfig(
                id=source_id,
                root=root_id,
                path=relative_path,
                title=_string(source.get("title"), f"source {source_id}.title"),
                authors=tuple(authors),
                date=str(date) if date is not None else None,
                source_type=_string(source.get("source_type"), f"source {source_id}.source_type"),
                authority_level=_string(
                    source.get("authority_level"), f"source {source_id}.authority_level"
                ),
                publication_status=_string(
                    source.get("publication_status"), f"source {source_id}.publication_status"
                ),
                topics=tuple(topics),
                rights=dict(_mapping(source.get("rights", {}), f"source {source_id}.rights")),
                limitations=tuple(limitations),
                extractor=_string(source.get("extractor"), f"source {source_id}.extractor"),
                required=required,
            )
        )
        seen_ids.add(source_id)
        seen_paths.add((root_id, relative_path))

    policy = dict(_mapping(raw.get("policy", {}), "policy"))
    external = policy.get("external_monitoring", False)
    if not isinstance(external, (bool, Mapping)):
        raise ConfigurationError("policy.external_monitoring must be a boolean or mapping")
    if isinstance(external, Mapping) and not isinstance(external.get("enabled"), bool):
        raise ConfigurationError(
            "policy.external_monitoring.enabled must be a boolean"
        )

    topics_path = path.parent / "topics.yaml"
    _validate_topics(tuple(sources), topics_path)
    return PipelineConfig(
        path=path.resolve(),
        version=1,
        roots=roots,
        sources=tuple(sources),
        policy=policy,
    )


def _validate_topics(sources: tuple[SourceConfig, ...], topics_path: Path) -> None:
    raw = load_yaml(topics_path)
    entries = raw.get("topics")
    if not isinstance(entries, list) or not entries:
        raise ConfigurationError("topics.yaml topics must be a non-empty list")
    known: set[str] = set()
    aliases: set[str] = set()
    for index, entry in enumerate(entries):
        topic = _mapping(entry, f"topics[{index}]")
        topic_id = _identifier(topic.get("id"), f"topics[{index}].id")
        if topic_id in known or topic_id in aliases:
            raise ConfigurationError(f"duplicate topic id or alias: {topic_id}")
        known.add(topic_id)
        raw_aliases = topic.get("aliases", [])
        if not isinstance(raw_aliases, list):
            raise ConfigurationError(f"topic {topic_id}.aliases must be a list")
        for alias in raw_aliases:
            alias_id = _identifier(alias, f"topic {topic_id}.alias")
            if alias_id in known or alias_id in aliases:
                raise ConfigurationError(f"duplicate topic id or alias: {alias_id}")
            aliases.add(alias_id)
    for source in sources:
        unknown = sorted(set(source.topics) - known)
        if unknown:
            raise ConfigurationError(
                f"source {source.id} uses unknown or non-canonical topics: {unknown}"
            )


def load_pulse_constraints(path: Path) -> dict[str, Any]:
    """Load the report constraints that are enforced by Markdown validation."""

    raw = load_yaml(path)
    if raw.get("version") != 1:
        raise ConfigurationError(f"unsupported pulse config version: {raw.get('version')!r}")
    report = _mapping(raw.get("report"), "pulse.report")
    word_count = _mapping(report.get("word_count"), "pulse.report.word_count")
    minimum = word_count.get("minimum")
    maximum = word_count.get("maximum")
    max_signals = report.get("max_signals")
    sections = report.get("required_sections")
    if (
        not isinstance(minimum, int)
        or not isinstance(maximum, int)
        or minimum < 1
        or maximum < minimum
        or not isinstance(max_signals, int)
        or not 1 <= max_signals <= 3
        or not isinstance(sections, list)
        or not all(isinstance(item, str) and item for item in sections)
    ):
        raise ConfigurationError("pulse report constraints are invalid")
    return {
        "minimum_words": minimum,
        "maximum_words": maximum,
        "maximum_signals": max_signals,
        "required_sections": tuple(sections),
        "require_meaningful_artifact": report.get("require_meaningful_artifact") is True,
    }


def resolve_live_root(config: PipelineConfig, root_id: str, override: Path | None = None) -> Path:
    if root_id not in config.roots:
        raise ConfigurationError(f"unknown root: {root_id}")
    if override is not None:
        return override
    root = config.roots[root_id]
    configured = os.environ.get(root.live_path_env) if root.live_path_env else None
    configured = configured or root.default_live_path
    if not configured:
        raise ConfigurationError(f"no live path configured for root {root_id}")
    configured_path = Path(configured)
    if configured_path.is_absolute():
        return configured_path
    # Relative defaults are anchored to the repository, not the caller's CWD.
    return config.path.parent.parent / configured_path


def config_fingerprint(config: PipelineConfig) -> str:
    return canonical_json_hash(
        {
            "version": config.version,
            "sources": [
                {
                    "id": source.id,
                    "root": source.root,
                    "path": source.path,
                    "title": source.title,
                    "authors": source.authors,
                    "date": source.date,
                    "source_type": source.source_type,
                    "authority_level": source.authority_level,
                    "publication_status": source.publication_status,
                    "topics": source.topics,
                    "rights": source.rights,
                    "limitations": source.limitations,
                    "extractor": source.extractor,
                    "required": source.required,
                }
                for source in sorted(config.sources, key=lambda item: item.id)
            ],
            "policy": config.policy,
        }
    )
