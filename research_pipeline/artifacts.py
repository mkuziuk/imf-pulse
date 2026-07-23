"""Validate and immutably bind pulse/artifact inputs to a release candidate."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from .errors import PublicationError
from .config import load_pulse_constraints
from .hashing import canonical_json_hash
from .paths import open_regular_file_under_root
from .pulse_validation import parse_pulse, validate_pulse_file
from .validation import _validate_evidence, strict_json_loads, validate_records


@dataclass(frozen=True)
class PublicationBinding:
    metadata: dict[str, Any] | None
    selected_pulse: str | None
    artifact_manifest_urls: tuple[str, ...]


def _safe_relative(value: str, label: str) -> str:
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise PublicationError(f"unsafe {label}: {value!r}")
    return pure.as_posix()


def _read_project_bytes(project_root: Path, relative_path: str) -> bytes:
    relative_path = _safe_relative(relative_path, "project path")
    with open_regular_file_under_root(project_root, relative_path) as descriptor:
        before = os.fstat(descriptor)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        after = os.fstat(descriptor)
    if (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or total != before.st_size
    ):
        raise PublicationError(f"project file changed while being read: {relative_path}")
    return b"".join(chunks)


def _public_url_to_relative(url: str) -> str:
    parsed = urlparse(url)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or "%" in url
        or "\\" in url
        or any(ord(character) < 32 or ord(character) == 127 for character in url)
    ):
        raise PublicationError(f"artifact URL must be a plain local path: {url!r}")
    if not url.startswith("/artifacts/") or url.startswith("//"):
        raise PublicationError(f"artifact URL must begin with /artifacts/: {url!r}")
    return _safe_relative(f"public/{url.removeprefix('/')}", "artifact URL")


def _project_relative_manifest(value: str) -> str:
    if value.startswith("/artifacts/"):
        return _public_url_to_relative(value)
    relative = _safe_relative(value, "artifact manifest")
    if not relative.startswith("public/artifacts/"):
        raise PublicationError("artifact manifests must be beneath public/artifacts")
    if not relative.endswith("/manifest.json"):
        raise PublicationError("artifact manifest path must end in /manifest.json")
    return relative


def _write_private_copy(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def bind_publication_inputs(
    project_root: Path,
    release_directory: Path,
    schemas_directory: Path,
    release_records: Mapping[str, list[dict[str, Any]]],
    *,
    pulse: str | None,
    artifact_manifests: Sequence[str],
    expected_release_identity: tuple[int, int] | None = None,
) -> PublicationBinding:
    """Copy validated publication inputs into ``release/publication``.

    The returned metadata is deterministic and suitable for inclusion in the
    release manifest.  Existing bindings are accepted only when byte-identical.
    """

    if pulse is None:
        if artifact_manifests:
            raise PublicationError("artifact manifests require a selected pulse")
        return PublicationBinding(None, None, ())
    pulse = _safe_relative(pulse, "pulse path")
    pulse_parts = PurePosixPath(pulse)
    if (
        pulse_parts.parent.as_posix() != "content/pulses"
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}\.md", pulse_parts.name) is None
    ):
        raise PublicationError("selected pulse must be a dated Markdown file in content/pulses")
    source_records = {
        record["id"]: record for record in release_records.get("sources.jsonl", [])
    }
    pulse_payload = _read_project_bytes(project_root, pulse)
    pulse_constraints = load_pulse_constraints(project_root / "config" / "pulse.yaml")
    with tempfile.TemporaryDirectory(prefix="imf-pulse-bound-input-") as temporary_name:
        private_pulse = Path(temporary_name) / pulse_parts.name
        _write_private_copy(private_pulse, pulse_payload)
        validate_pulse_file(
            private_pulse,
            project_root,
            schema_path=schemas_directory / "pulse.schema.json",
            source_ids=set(source_records),
            link_base_directory=project_root / "content" / "pulses",
            **pulse_constraints,
        )
        frontmatter, _ = parse_pulse(private_pulse)
    pulse_date = pulse_parts.stem
    if (
        frontmatter.get("status") != "published"
        or frontmatter.get("date") != pulse_date
        or frontmatter.get("id") != f"pulse-{pulse_date}"
    ):
        raise PublicationError(
            "published pulse status, id, date, and filename must agree"
        )
    required_urls = tuple(frontmatter.get("artifact_manifests", []))
    if not required_urls:
        raise PublicationError("a published pulse must declare artifact_manifests")
    required_paths = tuple(_public_url_to_relative(url) for url in required_urls)
    provided_paths = tuple(_project_relative_manifest(value) for value in artifact_manifests)
    if provided_paths and set(provided_paths) != set(required_paths):
        raise PublicationError("provided artifact manifests do not match pulse front matter")
    selected_paths = required_paths

    payloads: dict[str, bytes] = {pulse: pulse_payload}
    artifact_metadata: list[dict[str, Any]] = []
    artifact_ids: set[str] = set()
    for manifest_relative, manifest_url in zip(selected_paths, required_urls, strict=True):
        manifest_payload = _read_project_bytes(project_root, manifest_relative)
        try:
            manifest = strict_json_loads(manifest_payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise PublicationError(f"invalid artifact manifest JSON: {manifest_relative}") from exc
        if not isinstance(manifest, dict):
            raise PublicationError(f"artifact manifest must be an object: {manifest_relative}")
        validate_records(
            [manifest], schemas_directory / "artifact.schema.json", manifest_relative
        )
        artifact_id = manifest.get("artifact_id") or manifest.get("id")
        if not isinstance(artifact_id, str) or artifact_id in artifact_ids:
            raise PublicationError(f"artifact manifest has invalid/duplicate id: {manifest_relative}")
        artifact_ids.add(artifact_id)
        if manifest.get("id") and manifest.get("artifact_id") and manifest["id"] != manifest["artifact_id"]:
            raise PublicationError(f"artifact id aliases disagree: {artifact_id}")
        if (
            manifest.get("artifact_class")
            and manifest.get("artifact_type")
            and manifest["artifact_class"] != manifest["artifact_type"]
        ):
            raise PublicationError(f"artifact type aliases disagree: {artifact_id}")
        rights_status = manifest.get("rights_status")
        if rights_status and isinstance(manifest.get("rights"), Mapping) and (
            rights_status != manifest["rights"].get("status")
        ):
            raise PublicationError(f"artifact rights aliases disagree: {artifact_id}")
        if manifest.get("manifest_url") != manifest_url:
            raise PublicationError(f"artifact manifest_url/path mismatch: {manifest_relative}")
        manifest_url_prefix = manifest_url.rsplit("/", 1)[0] + "/"
        rights = manifest.get("rights", {})
        if not isinstance(rights, Mapping) or rights.get("local_display_allowed") is not True:
            raise PublicationError(f"artifact is not approved for local display: {artifact_id}")
        if rights.get("may_publish_publicly") is False and rights.get(
            "public_deployment_requires_owner_approval"
        ) is not True:
            raise PublicationError(f"artifact rights policy is incomplete: {artifact_id}")
        artifact_type = manifest.get("artifact_type") or manifest.get("artifact_class")
        if artifact_type == "web_image":
            source_url = rights.get("source_url")
            parsed_source_url = urlparse(source_url) if isinstance(source_url, str) else None
            if not all(
                isinstance(rights.get(field), str) and rights[field].strip()
                for field in ("creator", "license")
            ) or parsed_source_url is None or parsed_source_url.scheme not in {"http", "https"}:
                raise PublicationError(f"web image rights provenance is incomplete: {artifact_id}")

        bound_files: list[dict[str, Any]] = []
        file_urls: set[str] = set()
        for file_record in manifest["files"]:
            if (
                file_record.get("media_type")
                and file_record.get("mime_type")
                and file_record["media_type"] != file_record["mime_type"]
            ):
                raise PublicationError(f"artifact media type aliases disagree: {artifact_id}")
            if (
                file_record.get("role")
                and file_record.get("kind")
                and file_record["role"] != file_record["kind"]
            ):
                raise PublicationError(f"artifact file role aliases disagree: {artifact_id}")
            url = file_record["url"]
            if url in file_urls:
                raise PublicationError(f"artifact contains a duplicate file URL: {artifact_id} {url}")
            if not url.startswith(manifest_url_prefix):
                raise PublicationError(f"artifact file escapes its manifest directory: {url}")
            relative = _public_url_to_relative(url)
            if relative in payloads:
                raise PublicationError(f"artifact file path collides with another input: {url}")
            file_payload = _read_project_bytes(project_root, relative)
            actual_hash = hashlib.sha256(file_payload).hexdigest()
            if actual_hash != file_record["sha256"] or len(file_payload) != file_record.get(
                "bytes", len(file_payload)
            ):
                raise PublicationError(f"artifact file hash/size mismatch: {url}")
            _validate_artifact_file_type(url, file_record, file_payload, artifact_id)
            payloads[relative] = file_payload
            file_urls.add(url)
            bound_files.append(
                {
                    "url": url,
                    "source_path": relative,
                    "bound_path": f"publication/{relative}",
                    "sha256": actual_hash,
                    "bytes": len(file_payload),
                }
            )
        for named_url in (manifest.get("stable_url"), manifest.get("spec_url")):
            if named_url and not named_url.startswith(manifest_url_prefix):
                raise PublicationError(
                    f"artifact named URL escapes its manifest directory: {artifact_id} {named_url}"
                )
            if named_url and named_url not in file_urls:
                raise PublicationError(
                    f"artifact named URL is not listed in files: {artifact_id} {named_url}"
                )
        source_versions: dict[str, str] = {}
        for source_reference in manifest.get("sources", []):
            source_id = source_reference.get("source_id")
            if source_id not in source_records:
                raise PublicationError(f"artifact references unknown source: {artifact_id} -> {source_id}")
            known_hashes = {
                source_records[source_id].get("content_sha256"),
                *(
                    item.get("content_sha256")
                    for item in source_records[source_id].get("version_history", [])
                    if isinstance(item, Mapping)
                ),
            }
            if source_reference.get("content_sha256") not in known_hashes:
                raise PublicationError(
                    f"artifact references unavailable source version: {artifact_id} -> {source_id}"
                )
            if source_id in source_versions and source_versions[source_id] != source_reference["content_sha256"]:
                raise PublicationError(f"artifact declares conflicting source versions: {artifact_id} -> {source_id}")
            source_versions[source_id] = source_reference["content_sha256"]
            declared_path = (
                source_reference.get("path")
                or source_reference.get("relative_path")
                or source_reference.get("local_path")
            )
            if declared_path != source_records[source_id].get("path"):
                raise PublicationError(
                    f"artifact source path does not match registered source: {artifact_id} -> {source_id}"
                )
            resolved_locators = [
                _validate_artifact_evidence(
                    source_id,
                    source_reference["content_sha256"],
                    locator,
                    source_records,
                    release_records,
                    artifact_id,
                    allow_unresolved_text=True,
                )
                for locator in source_reference.get("locators", [])
            ]
            if not any(resolved_locators):
                raise PublicationError(
                    f"artifact source has no locator that resolves to an extract: {artifact_id} -> {source_id}"
                )
        for evidence in manifest.get("evidence", []):
            source_id = evidence.get("source_id")
            if source_id not in source_records:
                raise PublicationError(
                    f"artifact evidence references unknown source: {artifact_id} -> {source_id}"
                )
            if source_id not in source_versions:
                raise PublicationError(
                    f"artifact evidence source has no declared version: {artifact_id} -> {source_id}"
                )
            locator = evidence.get("locator", evidence.get("source_locator"))
            if not _validate_artifact_evidence(
                source_id,
                source_versions[source_id],
                locator,
                source_records,
                release_records,
                artifact_id,
            ):
                raise PublicationError(
                    f"artifact evidence locator is not precise: {artifact_id} -> {source_id}"
                )
        generator = manifest.get("generator")
        if artifact_type == "scientific_chart" and (
            not manifest.get("sources") or not manifest.get("evidence")
        ):
            raise PublicationError(
                f"scientific chart requires source provenance and evidence: {artifact_id}"
            )
        if artifact_type == "scientific_chart" and not (
            isinstance(generator, Mapping)
            and generator.get("deterministic") is True
            and generator.get("source_files_executed") is False
            and isinstance(generator.get("sha256"), str)
        ):
            raise PublicationError(
                f"scientific chart requires a deterministic hashed generator: {artifact_id}"
            )
        if artifact_type == "scientific_chart":
            file_by_url = {item["url"]: item for item in manifest["files"]}
            stable_record = file_by_url.get(manifest.get("stable_url"), {})
            spec_record = file_by_url.get(manifest.get("spec_url"), {})
            stable_media = stable_record.get("media_type") or stable_record.get("mime_type")
            spec_media = spec_record.get("media_type") or spec_record.get("mime_type")
            if stable_media not in {"image/svg+xml", "image/png"}:
                raise PublicationError(f"scientific chart stable_url must be a rendered image: {artifact_id}")
            if spec_media != "application/json":
                raise PublicationError(f"scientific chart spec_url must be JSON: {artifact_id}")
        generator_metadata: dict[str, Any] | None = None
        if isinstance(generator, Mapping) and generator.get("path") and generator.get("sha256"):
            generator_payload = _read_project_bytes(project_root, generator["path"])
            if hashlib.sha256(generator_payload).hexdigest() != generator["sha256"]:
                raise PublicationError(f"artifact generator hash mismatch: {artifact_id}")
            existing_generator = payloads.get(generator["path"])
            if existing_generator is not None and existing_generator != generator_payload:
                raise PublicationError(f"artifact generator path collision: {artifact_id}")
            payloads[generator["path"]] = generator_payload
            generator_metadata = {
                "source_path": generator["path"],
                "bound_path": f"publication/{generator['path']}",
                "sha256": generator["sha256"],
                "bytes": len(generator_payload),
            }

        payloads[manifest_relative] = manifest_payload
        artifact_binding = {
                "artifact_id": artifact_id,
                "manifest_url": manifest_url,
                "source_path": manifest_relative,
                "bound_path": f"publication/{manifest_relative}",
                "sha256": hashlib.sha256(manifest_payload).hexdigest(),
                "files": sorted(bound_files, key=lambda item: item["url"]),
                "rights": dict(rights),
            }
        if generator_metadata is not None:
            artifact_binding["generator"] = generator_metadata
        artifact_metadata.append(artifact_binding)
    featured = frontmatter.get("featured_artifact")
    if featured not in artifact_ids:
        raise PublicationError("featured_artifact does not match a validated artifact manifest")

    metadata = {
        "pulse": {
            "id": frontmatter.get("id"),
            "source_path": pulse,
            "bound_path": f"publication/{pulse}",
            "sha256": hashlib.sha256(payloads[pulse]).hexdigest(),
        },
        "artifact_manifests": sorted(
            artifact_metadata, key=lambda item: item["artifact_id"]
        ),
        "binding_sha256": canonical_json_hash(
            {
                relative: hashlib.sha256(payload).hexdigest()
                for relative, payload in sorted(payloads.items())
            }
        ),
    }
    _install_publication_directory(
        release_directory,
        payloads,
        metadata,
        expected_release_identity=expected_release_identity,
    )
    return PublicationBinding(metadata, pulse, required_urls)


def _validate_artifact_evidence(
    source_id: str,
    source_sha256: str,
    locator: Any,
    source_records: Mapping[str, Mapping[str, Any]],
    release_records: Mapping[str, list[dict[str, Any]]],
    artifact_id: str,
    *,
    allow_unresolved_text: bool = False,
) -> bool:
    extracts_by_source: dict[str, list[dict[str, Any]]] = {}
    for extract in release_records.get("extracts", []):
        extracts_by_source.setdefault(str(extract.get("source_id")), []).append(extract)
    normalized_locator = locator
    if isinstance(locator, str):
        normalized_locator = _structured_locator_from_text(
            locator, str(source_records[source_id].get("path", ""))
        )
        if normalized_locator is None:
            return False
    try:
        _validate_evidence(
            {
                "source_id": source_id,
                "source_sha256": source_sha256,
                "locator": normalized_locator,
            },
            source_records,
            extracts_by_source,
            artifact_id,
        )
    except Exception as exc:
        if allow_unresolved_text and isinstance(locator, str):
            return False
        raise PublicationError(f"invalid artifact evidence for {artifact_id}: {exc}") from exc
    return True


def _structured_locator_from_text(value: str, path: str) -> dict[str, Any] | None:
    """Parse the deliberately small legacy locator syntax used by v1 manifests."""

    line_match = re.search(r"\b(?:file\s+)?lines?\s+(\d+)(?:\s*[-–—]\s*(\d+))?", value, re.I)
    if line_match:
        start = int(line_match.group(1))
        end = int(line_match.group(2) or line_match.group(1))
        if path.lower().endswith(".csv"):
            return {"kind": "table", "path": path, "row_start": start, "row_end": end}
        return {
            "kind": "file_lines",
            "path": path,
            "line_start": start,
            "line_end": end,
        }
    page_match = re.search(r"\b(?:page|p\.)\s*(\d+)\b", value, re.I)
    if page_match and path.lower().endswith(".pdf"):
        return {"kind": "pdf", "path": path, "page": int(page_match.group(1))}
    return None


def _validate_artifact_file_type(
    url: str, record: Mapping[str, Any], payload: bytes, artifact_id: str
) -> None:
    media_type = record.get("media_type") or record.get("mime_type")
    suffix = PurePosixPath(url).suffix.lower()
    allowed = {
        "text/csv": ".csv",
        "application/json": ".json",
        "image/svg+xml": ".svg",
        "image/png": ".png",
        "image/jpeg": (".jpg", ".jpeg"),
        "application/pdf": ".pdf",
        "text/plain": ".txt",
    }
    if media_type not in allowed:
        raise PublicationError(f"artifact media type is not allowed: {artifact_id} {url}")
    expected = allowed[media_type]
    expected_suffixes = (expected,) if isinstance(expected, str) else expected
    if suffix not in expected_suffixes:
        raise PublicationError(f"artifact media type/extension mismatch: {artifact_id} {url}")
    if media_type == "application/json":
        try:
            strict_json_loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise PublicationError(f"artifact JSON is invalid: {artifact_id} {url}") from exc
    if media_type == "image/svg+xml":
        _validate_inactive_svg(payload, artifact_id, url)
    elif media_type == "image/png" and not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise PublicationError(f"artifact PNG signature is invalid: {artifact_id} {url}")
    elif media_type == "image/jpeg" and not payload.startswith(b"\xff\xd8\xff"):
        raise PublicationError(f"artifact JPEG signature is invalid: {artifact_id} {url}")
    elif media_type == "application/pdf" and not payload.startswith(b"%PDF-"):
        raise PublicationError(f"artifact PDF signature is invalid: {artifact_id} {url}")
    elif media_type in {"text/csv", "text/plain"}:
        try:
            payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PublicationError(f"artifact text is not UTF-8: {artifact_id} {url}") from exc


def _validate_inactive_svg(payload: bytes, artifact_id: str, url: str) -> None:
    lowered = payload.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise PublicationError(f"artifact SVG declarations are forbidden: {artifact_id} {url}")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise PublicationError(f"artifact SVG is invalid: {artifact_id} {url}") from exc
    if root.tag.rsplit("}", 1)[-1].lower() != "svg":
        raise PublicationError(f"artifact SVG has no svg root: {artifact_id} {url}")
    forbidden_tags = {"script", "foreignobject", "iframe", "object", "embed"}
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1].lower()
        if tag in forbidden_tags:
            raise PublicationError(f"artifact SVG contains active content: {artifact_id} {url}")
        if tag == "style":
            stylesheet = (element.text or "").lower()
            if any(token in stylesheet for token in ("@import", "javascript:", "data:", "expression(")):
                raise PublicationError(f"artifact SVG contains active CSS: {artifact_id} {url}")
            for match in re.finditer(r"url\(([^)]+)\)", stylesheet, re.I):
                if not match.group(1).strip(" \t\"'").startswith("#"):
                    raise PublicationError(f"artifact SVG CSS contains an external URL: {artifact_id} {url}")
        for raw_name, raw_value in element.attrib.items():
            name = raw_name.rsplit("}", 1)[-1].lower()
            value = raw_value.strip()
            lowered_value = value.lower()
            if name.startswith("on"):
                raise PublicationError(f"artifact SVG contains an event handler: {artifact_id} {url}")
            if name == "href" and value and not value.startswith("#"):
                raise PublicationError(f"artifact SVG contains an external reference: {artifact_id} {url}")
            if "javascript:" in lowered_value or "data:" in lowered_value:
                raise PublicationError(f"artifact SVG contains an active URI: {artifact_id} {url}")
            for match in re.finditer(r"url\(([^)]+)\)", value, re.I):
                if not match.group(1).strip(" \t\"'").startswith("#"):
                    raise PublicationError(f"artifact SVG contains an external URL: {artifact_id} {url}")


def _install_publication_directory(
    release_directory: Path,
    payloads: Mapping[str, bytes],
    metadata: Mapping[str, Any],
    *,
    expected_release_identity: tuple[int, int] | None = None,
) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        release_descriptor = os.open(release_directory, flags)
    except OSError as exc:
        raise PublicationError("release directory is unavailable or unsafe") from exc
    opened = os.fstat(release_descriptor)
    actual_identity = (opened.st_dev, opened.st_ino)
    if expected_release_identity is None:
        expected_release_identity = actual_identity
    if actual_identity != expected_release_identity:
        os.close(release_descriptor)
        raise PublicationError("release directory changed before publication binding")

    try:
        publication_stat = os.stat(
            "publication", dir_fd=release_descriptor, follow_symlinks=False
        )
    except FileNotFoundError:
        publication_stat = None
    except OSError as exc:
        os.close(release_descriptor)
        raise PublicationError("release publication path is unavailable or unsafe") from exc
    if publication_stat is not None:
        os.close(release_descriptor)
        if not stat.S_ISDIR(publication_stat.st_mode):
            raise PublicationError("release publication path is not a regular directory")
        try:
            existing = strict_json_loads(
                _read_project_bytes(
                    release_directory, "publication/binding.json"
                ).decode("utf-8")
            )
        except (UnicodeDecodeError, ValueError) as exc:
            raise PublicationError("release publication binding is invalid") from exc
        if existing != metadata:
            raise PublicationError("release is already bound to different publication inputs")
        for relative, payload in payloads.items():
            if _read_project_bytes(release_directory, f"publication/{relative}") != payload:
                raise PublicationError(f"bound publication bytes differ: {relative}")
        return

    # Build outside the release tree, then install relative to the held,
    # identity-checked release descriptor.  A pathname substitution cannot
    # redirect this mutation into an attacker-controlled directory.
    staging_parent = Path(tempfile.mkdtemp(prefix="imf-pulse-publication-"))
    staging = staging_parent / "publication"
    staging.mkdir()
    try:
        for relative, payload in payloads.items():
            path = staging.joinpath(*PurePosixPath(relative).parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        binding_path = staging / "binding.json"
        binding_path.write_text(
            json.dumps(metadata, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )
        with binding_path.open("rb") as handle:
            os.fsync(handle.fileno())
        _fsync_tree(staging)
        try:
            os.rename(staging, "publication", dst_dir_fd=release_descriptor)
        except OSError as exc:
            raise PublicationError("cannot install immutable publication binding") from exc
        os.fsync(release_descriptor)
    finally:
        os.close(release_descriptor)
        if staging_parent.exists():
            shutil.rmtree(staging_parent)


def verify_bound_publication(
    release_directory: Path,
    schemas_directory: Path,
    metadata: Mapping[str, Any] | None,
) -> None:
    if metadata is None:
        if os.path.lexists(release_directory / "publication"):
            raise PublicationError("release has publication files without binding metadata")
        return
    try:
        stored_metadata = strict_json_loads(
            _read_project_bytes(
                release_directory, "publication/binding.json"
            ).decode("utf-8")
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise PublicationError("bound publication metadata is invalid") from exc
    if stored_metadata != metadata:
        raise PublicationError("bound publication metadata mismatch")
    payload_hashes: dict[str, str] = {}
    pulse = metadata.get("pulse", {})
    pulse_payload = _verify_bound_file(release_directory, pulse["bound_path"], pulse["sha256"])
    payload_hashes[pulse["source_path"]] = hashlib.sha256(pulse_payload).hexdigest()
    for artifact in metadata.get("artifact_manifests", []):
        manifest_payload = _verify_bound_file(
            release_directory, artifact["bound_path"], artifact["sha256"]
        )
        try:
            manifest = strict_json_loads(manifest_payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise PublicationError(f"bound artifact manifest is invalid: {artifact['bound_path']}") from exc
        validate_records(
            [manifest], schemas_directory / "artifact.schema.json", artifact["bound_path"]
        )
        payload_hashes[artifact["source_path"]] = hashlib.sha256(manifest_payload).hexdigest()
        for file_record in artifact.get("files", []):
            payload = _verify_bound_file(
                release_directory, file_record["bound_path"], file_record["sha256"]
            )
            if len(payload) != file_record["bytes"]:
                raise PublicationError(f"bound artifact size mismatch: {file_record['url']}")
            payload_hashes[file_record["source_path"]] = hashlib.sha256(payload).hexdigest()
        generator = artifact.get("generator")
        if isinstance(generator, Mapping):
            generator_payload = _verify_bound_file(
                release_directory, generator["bound_path"], generator["sha256"]
            )
            if len(generator_payload) != generator["bytes"]:
                raise PublicationError("bound artifact generator size mismatch")
            payload_hashes[generator["source_path"]] = hashlib.sha256(
                generator_payload
            ).hexdigest()
    if canonical_json_hash(dict(sorted(payload_hashes.items()))) != metadata.get("binding_sha256"):
        raise PublicationError("bound publication aggregate hash mismatch")


def verify_source_publication_inputs(
    project_root: Path, metadata: Mapping[str, Any] | None
) -> None:
    """Require mutable Vite inputs to remain byte-identical to their binding."""

    if metadata is None:
        return
    payload_hashes: dict[str, str] = {}
    pulse = metadata["pulse"]
    pulse_payload = _read_project_bytes(project_root, pulse["source_path"])
    if hashlib.sha256(pulse_payload).hexdigest() != pulse["sha256"]:
        raise PublicationError("live pulse changed after publication binding")
    payload_hashes[pulse["source_path"]] = pulse["sha256"]
    for artifact in metadata.get("artifact_manifests", []):
        manifest_payload = _read_project_bytes(project_root, artifact["source_path"])
        if hashlib.sha256(manifest_payload).hexdigest() != artifact["sha256"]:
            raise PublicationError(f"live artifact manifest changed: {artifact['manifest_url']}")
        payload_hashes[artifact["source_path"]] = artifact["sha256"]
        for file_record in artifact.get("files", []):
            payload = _read_project_bytes(project_root, file_record["source_path"])
            if (
                hashlib.sha256(payload).hexdigest() != file_record["sha256"]
                or len(payload) != file_record["bytes"]
            ):
                raise PublicationError(f"live artifact changed: {file_record['url']}")
            payload_hashes[file_record["source_path"]] = file_record["sha256"]
        generator = artifact.get("generator")
        if isinstance(generator, Mapping):
            payload = _read_project_bytes(project_root, generator["source_path"])
            if (
                hashlib.sha256(payload).hexdigest() != generator["sha256"]
                or len(payload) != generator["bytes"]
            ):
                raise PublicationError("live artifact generator changed")
            payload_hashes[generator["source_path"]] = generator["sha256"]
    if canonical_json_hash(dict(sorted(payload_hashes.items()))) != metadata.get("binding_sha256"):
        raise PublicationError("live publication aggregate hash mismatch")


def _verify_bound_file(release_directory: Path, relative: str, expected_hash: str) -> bytes:
    payload = _read_project_bytes(release_directory, relative)
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected_hash:
        raise PublicationError(f"bound publication hash mismatch: {relative}")
    return payload


def _fsync_tree(root: Path) -> None:
    for directory, _, _ in os.walk(root, topdown=False):
        _fsync_directory(Path(directory))


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
