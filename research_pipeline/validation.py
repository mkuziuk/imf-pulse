"""Schema and cross-reference validation for immutable releases."""

from __future__ import annotations

import json
import os
import stat
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from .errors import ValidationError
from .hashing import canonical_json_hash, sha256_file

JSONL_SCHEMA_BY_NAME = {
    "sources.jsonl": "source.schema.json",
    "claims.jsonl": "claim.schema.json",
    "methods.jsonl": "method.schema.json",
    "experiments.jsonl": "experiment.schema.json",
    "relationships.jsonl": "relationship.schema.json",
    "artifacts.jsonl": "artifact.schema.json",
}
KNOWLEDGE_FILENAMES = (
    "claims.jsonl",
    "methods.jsonl",
    "experiments.jsonl",
    "relationships.jsonl",
)


def _strict_object(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def strict_json_loads(text: str) -> Any:
    return json.loads(
        text,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"non-finite JSON number is forbidden: {value}")
        ),
        object_pairs_hook=_strict_object,
    )


def read_json(path: Path) -> Any:
    try:
        return strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValidationError(f"invalid JSON {path}: {exc}") from exc


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValidationError(f"cannot read JSONL {path}: {exc}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = strict_json_loads(line)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValidationError(f"invalid JSONL {path}:{line_number}: {exc}") from exc
        if not isinstance(value, dict):
            raise ValidationError(f"JSONL record must be an object at {path}:{line_number}")
        record_id = value.get("id") or value.get("artifact_id")
        if not isinstance(record_id, str) or not record_id:
            raise ValidationError(f"JSONL record has no id at {path}:{line_number}")
        if record_id in seen_ids:
            raise ValidationError(f"duplicate id {record_id!r} in {path}")
        seen_ids.add(record_id)
        records.append(value)
    return records


def load_schema(schema_path: Path) -> dict[str, Any]:
    schema = read_json(schema_path)
    if not isinstance(schema, dict):
        raise ValidationError(f"schema is not an object: {schema_path}")
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise ValidationError(f"invalid JSON Schema {schema_path}: {exc}") from exc
    return schema


def validate_records(records: Iterable[Mapping[str, Any]], schema_path: Path, label: str) -> None:
    schema = load_schema(schema_path)
    registry = _schema_registry(schema_path.parent)
    validator = Draft202012Validator(
        schema,
        registry=registry,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )
    errors: list[str] = []
    for index, record in enumerate(records, start=1):
        for error in sorted(validator.iter_errors(record), key=lambda item: list(item.absolute_path)):
            location = ".".join(str(part) for part in error.absolute_path) or "<record>"
            errors.append(f"{label}:{index}:{location}: {error.message}")
    if errors:
        raise ValidationError("schema validation failed:\n" + "\n".join(sorted(errors)))


def _schema_registry(schemas_directory: Path) -> Registry:
    registry = Registry()
    for path in sorted(schemas_directory.glob("*.schema.json")):
        schema = load_schema(path)
        schema_id = schema.get("$id")
        if not isinstance(schema_id, str) or not schema_id:
            raise ValidationError(f"schema has no $id: {path}")
        registry = registry.with_resource(schema_id, Resource.from_contents(schema))
    return registry


def validate_release_directory(
    release_directory: Path,
    schemas_directory: Path,
    *,
    enforce_directory_name: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    """Validate a complete release and return its parsed JSONL records."""

    if release_directory.is_symlink() or not release_directory.is_dir():
        raise ValidationError(f"release directory is unavailable or unsafe: {release_directory}")
    release_manifest = read_json(release_directory / "release.json")
    if not isinstance(release_manifest, dict):
        raise ValidationError("release manifest must be an object")
    validate_records(
        [release_manifest], schemas_directory / "release.schema.json", "release.json"
    )
    _validate_release_hashes(release_directory, release_manifest)
    release_id = release_manifest.get("release_id")
    input_fingerprint = release_manifest.get("input_fingerprint")
    if enforce_directory_name and release_id != release_directory.name:
        raise ValidationError("release manifest id does not match its directory")
    if not isinstance(input_fingerprint, str) or release_id != f"release-{input_fingerprint[:20]}":
        raise ValidationError("release id is not derived from its input fingerprint")

    parsed: dict[str, list[dict[str, Any]]] = {}
    for filename, schema_name in JSONL_SCHEMA_BY_NAME.items():
        path = release_directory / filename
        if not path.exists():
            if filename == "artifacts.jsonl":
                continue
            raise ValidationError(f"release is missing {filename}")
        if path.is_symlink() or not path.is_file():
            raise ValidationError(f"release file is unsafe: {path}")
        records = read_jsonl(path)
        validate_records(records, schemas_directory / schema_name, filename)
        parsed[filename] = records

    state = read_json(release_directory / "state.json")
    validate_records([state], schemas_directory / "state.schema.json", "state.json")
    extracts_directory = release_directory / "extracts"
    if not extracts_directory.is_dir() or extracts_directory.is_symlink():
        raise ValidationError("release is missing a safe extracts directory")
    extract_records: list[dict[str, Any]] = []
    extract_ids: set[str] = set()
    for extract_path in sorted(extracts_directory.glob("*.jsonl")):
        records = read_jsonl(extract_path)
        validate_records(
            records,
            schemas_directory / "extract.schema.json",
            f"extracts/{extract_path.name}",
        )
        expected_source_id = extract_path.stem.split("@", 1)[0]
        historical_suffix = extract_path.stem.split("@", 1)[1] if "@" in extract_path.stem else None
        for record in records:
            if record["source_id"] != expected_source_id:
                raise ValidationError(
                    f"extract filename/source mismatch: {extract_path.name} contains {record['source_id']}"
                )
            if historical_suffix is not None and record["source_sha256"] != historical_suffix:
                raise ValidationError(
                    f"historical extract filename/hash mismatch: {extract_path.name}"
                )
            if record["id"] in extract_ids:
                raise ValidationError(f"duplicate extract id across files: {record['id']}")
            unit_identity: dict[str, Any] = {
                "kind": record.get("kind"),
                "locator": record.get("locator"),
            }
            if "text" in record:
                unit_identity["text"] = record["text"]
            if "data" in record:
                unit_identity["data"] = record["data"]
            unit_hash = canonical_json_hash(unit_identity)
            if record.get("content_sha256") != unit_hash:
                raise ValidationError(
                    f"extract unit content hash mismatch: {record.get('id')}"
                )
            expected_id = (
                f"extract-{record['source_id']}-{record['source_sha256'][:12]}-"
                f"{unit_hash[:20]}"
            )
            if record.get("id") != expected_id:
                raise ValidationError(f"extract unit id is not content-derived: {record.get('id')}")
            extract_ids.add(record["id"])
        extract_records.extend(records)
    parsed["extracts"] = extract_records
    _validate_state_consistency(state, release_manifest, parsed)
    _validate_cross_references(parsed)
    _validate_release_identity(release_manifest, parsed)
    return parsed


def _validate_release_identity(
    manifest: Mapping[str, Any], parsed: Mapping[str, list[dict[str, Any]]]
) -> None:
    sources = parsed.get("sources.jsonl", [])
    if any(source.get("snapshot_id") != manifest.get("snapshot_id") for source in sources):
        raise ValidationError("source records do not match the release snapshot id")
    curated_identity = {
        filename: canonical_json_hash(
            sorted(parsed.get(filename, []), key=lambda record: record["id"])
        )
        for filename in KNOWLEDGE_FILENAMES
    }
    external_sources = [
        {key: value for key, value in source.items() if key != "snapshot_id"}
        for source in sources
        if isinstance(source.get("url"), str)
    ]
    if external_sources:
        curated_identity["sources.jsonl"] = canonical_json_hash(
            sorted(external_sources, key=lambda record: record["id"])
        )
    semantic_identity = {
        "schema_version": 1,
        "config_sha256": manifest.get("config_sha256"),
        "runtime": manifest.get("runtime"),
        "extracts": {
            source["id"]: source.get("extract_semantic_sha256")
            for source in sources
        },
        "curated": curated_identity,
    }
    semantic_fingerprint = canonical_json_hash(semantic_identity)
    if manifest.get("semantic_fingerprint") != semantic_fingerprint:
        raise ValidationError("release semantic fingerprint cannot be reconstructed")
    input_identity = {
        **semantic_identity,
        "snapshot_id": manifest.get("snapshot_id"),
        "source_bytes": {
            source["id"]: source.get("content_sha256")
            for source in sources
        },
    }
    input_fingerprint = canonical_json_hash(input_identity)
    if manifest.get("input_fingerprint") != input_fingerprint:
        raise ValidationError("release input fingerprint cannot be reconstructed")
    if manifest.get("release_id") != f"release-{input_fingerprint[:20]}":
        raise ValidationError("release id cannot be reconstructed")


def _validate_state_consistency(
    state: Mapping[str, Any],
    release_manifest: Mapping[str, Any],
    parsed: Mapping[str, list[dict[str, Any]]],
) -> None:
    for field in (
        "release_id",
        "snapshot_id",
        "input_fingerprint",
        "semantic_fingerprint",
    ):
        if state.get(field) != release_manifest.get(field):
            raise ValidationError(f"state and release manifest disagree on {field}")
    sources = {source["id"]: source for source in parsed.get("sources.jsonl", [])}
    fingerprints = state.get("source_fingerprints", {})
    if set(fingerprints) != set(sources):
        raise ValidationError("state source_fingerprints do not match source records")
    for source_id, source in sources.items():
        fingerprint = fingerprints[source_id]
        expected = {
            "content_sha256": source.get("content_sha256"),
            "extract_semantic_sha256": source.get("extract_semantic_sha256"),
            "extractor": source.get("extractor"),
        }
        for key, value in expected.items():
            if fingerprint.get(key) != value:
                raise ValidationError(
                    f"state fingerprint mismatch for {source_id}.{key}"
                )


def _validate_release_hashes(release_directory: Path, manifest: Mapping[str, Any]) -> None:
    files = manifest.get("files")
    if not isinstance(files, Mapping):
        raise ValidationError("release manifest files must be an object")
    actual_files: set[str] = set()
    for path in release_directory.rglob("*"):
        mode = os.lstat(path).st_mode
        relative = path.relative_to(release_directory).as_posix()
        if stat.S_ISLNK(mode):
            raise ValidationError(f"release contains a forbidden symlink: {relative}")
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise ValidationError(f"release contains a non-regular node: {relative}")
        if path != release_directory / "release.json":
            actual_files.add(relative)
    listed_files = set(files)
    if actual_files != listed_files:
        missing = sorted(listed_files - actual_files)
        unlisted = sorted(actual_files - listed_files)
        raise ValidationError(
            f"release manifest file set mismatch; missing={missing}, unlisted={unlisted}"
        )
    for relative, expected in sorted(files.items()):
        if not isinstance(relative, str) or not isinstance(expected, str):
            raise ValidationError("release file hashes must map strings to strings")
        pure = PurePosixPath(relative)
        if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
            raise ValidationError(f"unsafe release manifest path: {relative!r}")
        path = release_directory.joinpath(*pure.parts)
        if path.is_symlink() or not path.is_file():
            raise ValidationError(f"release manifest file is absent or unsafe: {relative}")
        if sha256_file(path) != expected:
            raise ValidationError(f"release hash mismatch: {relative}")


def _validate_cross_references(parsed: Mapping[str, list[dict[str, Any]]]) -> None:
    sources = {record["id"]: record for record in parsed.get("sources.jsonl", [])}
    if not sources:
        raise ValidationError("release must contain at least one source")
    extracts_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for extract in parsed.get("extracts", []):
        source_id = extract.get("source_id")
        if source_id not in sources:
            raise ValidationError(f"extract references unknown source: {source_id}")
        known_hashes = _known_source_hashes(sources[source_id])
        if extract.get("source_sha256") not in known_hashes:
            raise ValidationError(f"extract source hash is unavailable: {extract.get('id')}")
        extracts_by_source[source_id].append(extract)
    for source_id in sources:
        if not extracts_by_source[source_id]:
            raise ValidationError(f"source has no static extract: {source_id}")
        current_hash = sources[source_id].get("content_sha256") or sources[source_id].get("content_hash")
        current_units = [
            unit
            for unit in extracts_by_source[source_id]
            if unit.get("source_sha256") == current_hash
        ]
        if not current_units:
            raise ValidationError(f"source has no extract for its current version: {source_id}")
        semantic_hash = canonical_json_hash(
            [
                {
                    key: value
                    for key, value in unit.items()
                    if key not in {"id", "source_id", "source_sha256", "schema_version"}
                }
                for unit in current_units
            ]
        )
        if semantic_hash != sources[source_id].get("extract_semantic_sha256"):
            raise ValidationError(f"source extract semantic hash mismatch: {source_id}")

    all_objects: dict[str, dict[str, Any]] = dict(sources)
    object_types: dict[str, str] = {source_id: "source" for source_id in sources}
    for filename in KNOWLEDGE_FILENAMES:
        singular = filename.removesuffix("s.jsonl")
        for record in parsed.get(filename, []):
            record_id = record["id"]
            if record_id in all_objects:
                raise ValidationError(f"knowledge id appears in multiple files: {record_id}")
            all_objects[record_id] = record
            object_types[record_id] = singular
            evidence = record.get("evidence")
            if not isinstance(evidence, list) or not evidence:
                raise ValidationError(f"substantive object lacks evidence: {record_id}")
            for reference in evidence:
                _validate_evidence(reference, sources, extracts_by_source, record_id)

    for artifact in parsed.get("artifacts.jsonl", []):
        artifact_id = artifact.get("id") or artifact.get("artifact_id")
        rights = str(artifact.get("rights_status", "unknown"))
        stable_url = artifact.get("stable_url")
        if stable_url and rights in {"unknown", "internal_only", "not_assessed"}:
            raise ValidationError(f"public artifact has unapproved rights: {artifact_id}")
        if artifact.get("artifact_class") == "generated_image":
            caption = str(artifact.get("caption", ""))
            if "Conceptual illustration — not research evidence" not in caption:
                raise ValidationError(f"generated image lacks required label: {artifact_id}")

    for relationship in parsed.get("relationships.jsonl", []):
        for endpoint_name in ("from", "to"):
            endpoint = relationship.get(endpoint_name)
            if not isinstance(endpoint, Mapping):
                raise ValidationError(f"relationship endpoint is invalid: {relationship['id']}.{endpoint_name}")
            endpoint_id = endpoint.get("id")
            endpoint_type = endpoint.get("type")
            if endpoint_id not in all_objects:
                raise ValidationError(f"relationship endpoint does not exist: {relationship['id']} -> {endpoint_id}")
            if endpoint_type != object_types[endpoint_id]:
                raise ValidationError(
                    f"relationship endpoint type mismatch: {relationship['id']} {endpoint_name}"
                )


def _known_source_hashes(source: Mapping[str, Any]) -> set[str]:
    known = {source.get("content_sha256") or source.get("content_hash")}
    history = source.get("version_history", [])
    if isinstance(history, list):
        known.update(
            item.get("content_sha256")
            for item in history
            if isinstance(item, Mapping)
        )
    return {value for value in known if isinstance(value, str)}


def _validate_evidence(
    reference: Any,
    sources: Mapping[str, Mapping[str, Any]],
    extracts_by_source: Mapping[str, list[dict[str, Any]]],
    owner_id: str,
) -> None:
    if not isinstance(reference, Mapping):
        raise ValidationError(f"evidence must be an object: {owner_id}")
    source_id = reference.get("source_id")
    if source_id not in sources:
        raise ValidationError(f"evidence references unknown source: {owner_id} -> {source_id}")
    source_sha256 = reference.get("source_sha256")
    known_hashes = _known_source_hashes(sources[source_id])
    if source_sha256 and source_sha256 not in known_hashes:
        raise ValidationError(f"evidence source hash is unavailable: {owner_id} -> {source_id}")
    locator = reference.get("locator")
    _validate_locator(locator, owner_id)
    if not isinstance(locator, Mapping):
        raise ValidationError(f"substantive evidence requires a structured locator: {owner_id}")
    if isinstance(locator, Mapping):
        source_path = sources[source_id].get("path") or sources[source_id].get("relative_path")
        if locator.get("path") != source_path:
            raise ValidationError(
                f"evidence path does not match source: {owner_id} -> {source_id}"
            )
        compatible = [
            extract
            for extract in extracts_by_source.get(source_id, [])
            if extract.get("source_sha256") == source_sha256
            and _locator_is_contained(locator, extract)
        ]
        if not compatible:
            raise ValidationError(
                f"evidence locator does not resolve to an extract: {owner_id} -> {source_id}"
            )


def _locator_is_contained(locator: Mapping[str, Any], extract: Mapping[str, Any]) -> bool:
    unit = extract.get("locator")
    if not isinstance(unit, Mapping) or locator.get("path") != unit.get("path"):
        return False
    kind = locator.get("kind")
    unit_kind = unit.get("kind")
    if kind in {"text_lines", "file_lines"}:
        if unit_kind != "file_lines":
            return False
        return (
            isinstance(locator.get("line_start"), int)
            and isinstance(locator.get("line_end"), int)
            and unit.get("line_start", 1) <= locator["line_start"]
            and locator["line_end"] <= unit.get("line_end", 0)
        )
    if kind == "pdf":
        return unit_kind == "pdf" and locator.get("page") == unit.get("page")
    if kind in {"notebook_cell", "notebook_output"}:
        if unit_kind != kind:
            return False
        for key in ("cell_id", "cell_index", "output_index", "mime"):
            if key in locator and locator[key] != unit.get(key):
                return False
        return True
    if kind in {"csv_rows", "table"}:
        if unit_kind != "table":
            return False
        requested = _csv_requested_range(locator)
        if requested is None:
            return False
        return unit.get("row_start", 1) <= requested[0] and requested[1] <= unit.get("row_end", 0)
    if kind == "json_pointer":
        if unit_kind != "json_pointer":
            return False
        pointer = locator.get("json_pointer", locator.get("pointer"))
        return isinstance(pointer, str) and _json_pointer_exists(extract.get("data"), pointer)
    return False


def _csv_requested_range(locator: Mapping[str, Any]) -> tuple[int, int] | None:
    if locator.get("kind") == "table":
        start, end = locator.get("row_start"), locator.get("row_end")
        return (start, end) if isinstance(start, int) and isinstance(end, int) else None
    value = locator.get("csv_row")
    if isinstance(value, int):
        return value, value
    if not isinstance(value, str):
        return None
    match = __import__("re").fullmatch(r"\s*(\d+)\s*(?:[-–—]\s*(\d+)\s*)?", value)
    if not match:
        return None
    start = int(match.group(1))
    end = int(match.group(2) or match.group(1))
    return (start, end) if end >= start else None


def _json_pointer_exists(value: Any, pointer: str) -> bool:
    if pointer == "":
        return True
    if not pointer.startswith("/"):
        return False
    current = value
    for encoded in pointer[1:].split("/"):
        token = encoded.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if token not in current:
                return False
            current = current[token]
        elif isinstance(current, list) and token.isdigit():
            index = int(token)
            if index >= len(current):
                return False
            current = current[index]
        else:
            return False
    return True


def _validate_locator(locator: Any, owner_id: str) -> None:
    if isinstance(locator, str):
        if not locator.strip():
            raise ValidationError(f"empty evidence locator: {owner_id}")
        return
    if not isinstance(locator, Mapping) or not locator:
        raise ValidationError(f"evidence locator must be a non-empty object: {owner_id}")
    path = locator.get("path")
    if path is not None:
        if not isinstance(path, str):
            raise ValidationError(f"evidence path must be a string: {owner_id}")
        pure = PurePosixPath(path)
        if pure.is_absolute() or any(part == ".." for part in pure.parts):
            raise ValidationError(f"unsafe evidence path: {owner_id}")
    kind = locator.get("kind")
    precise_keys = {
        "page",
        "line_start",
        "line_end",
        "cell_id",
        "cell_index",
        "csv_row",
        "json_pointer",
        "section",
        "equation",
        "theorem",
        "row_start",
    }
    if not precise_keys.intersection(locator):
        raise ValidationError(f"evidence locator is not precise: {owner_id} ({kind!r})")
    if "page" in locator and (not isinstance(locator["page"], int) or locator["page"] < 1):
        raise ValidationError(f"PDF page must be one-based: {owner_id}")
    if "line_start" in locator:
        start = locator["line_start"]
        end = locator.get("line_end", start)
        if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start:
            raise ValidationError(f"invalid line range: {owner_id}")
    if "cell_index" in locator and (
        not isinstance(locator["cell_index"], int) or locator["cell_index"] < 0
    ):
        raise ValidationError(f"notebook cell index must be zero-based: {owner_id}")


def validate_source_config_topics(config_path: Path, topics_path: Path) -> None:
    """Validate that configured topic IDs belong to the controlled vocabulary."""

    import yaml

    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        topics = yaml.safe_load(topics_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValidationError(f"cannot load configuration for topic validation: {exc}") from exc
    raw_topics = topics.get("topics", []) if isinstance(topics, dict) else []
    known = {
        item["id"] if isinstance(item, dict) else item
        for item in raw_topics
        if isinstance(item, (dict, str)) and (not isinstance(item, dict) or "id" in item)
    }
    unknown = defaultdict(list)
    for source in config.get("sources", []):
        for topic in source.get("topics", []):
            if topic not in known:
                unknown[topic].append(source.get("id", "<unknown>"))
    if unknown:
        details = ", ".join(f"{topic}: {ids}" for topic, ids in sorted(unknown.items()))
        raise ValidationError(f"source configuration uses unknown topics: {details}")
