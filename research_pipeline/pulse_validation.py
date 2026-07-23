"""Validate pulse Markdown structure, links, source references, and artifacts."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.parse import unquote, urlparse

import yaml

from .config import UniqueKeySafeLoader
from .errors import ValidationError
from .paths import resolve_regular_file_under_root
from .validation import validate_records

FRONT_MATTER_PATTERN = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
LINK_PATTERN = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
WORD_PATTERN = re.compile(r"\b[\w’'-]+\b", re.UNICODE)
RAW_HTML_PATTERN = re.compile(r"<\s*/?\s*(script|iframe|object|embed|form|style)\b", re.IGNORECASE)


@dataclass(frozen=True)
class PulseValidationResult:
    path: Path
    word_count: int
    signal_count: int
    artifact_manifests: tuple[str, ...]


def _plain_frontmatter(value: Any) -> Any:
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, list):
        return [_plain_frontmatter(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain_frontmatter(item) for key, item in value.items()}
    return value


def parse_pulse(path: Path) -> tuple[dict[str, Any], str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValidationError(f"cannot read pulse {path}: {exc}") from exc
    match = FRONT_MATTER_PATTERN.match(text)
    if not match:
        raise ValidationError(f"pulse has no YAML front matter: {path}")
    try:
        frontmatter = yaml.load(match.group(1), Loader=UniqueKeySafeLoader)
    except yaml.YAMLError as exc:
        raise ValidationError(f"invalid pulse front matter {path}: {exc}") from exc
    if not isinstance(frontmatter, dict):
        raise ValidationError(f"pulse front matter must be an object: {path}")
    return _plain_frontmatter(frontmatter), text[match.end() :]


def validate_pulse_file(
    path: Path,
    project_root: Path,
    *,
    schema_path: Path | None = None,
    source_ids: set[str] | None = None,
    link_base_directory: Path | None = None,
    minimum_words: int = 350,
    maximum_words: int = 650,
    maximum_signals: int = 3,
    required_sections: tuple[str, ...] = (
        "lead",
        "signals",
        "why-this-matters",
        "unresolved-question",
        "sources",
    ),
    require_meaningful_artifact: bool = True,
) -> PulseValidationResult:
    frontmatter, body = parse_pulse(path)
    if schema_path is not None:
        validate_records([frontmatter], schema_path, path.name)
    errors: list[str] = []
    for field in ("id", "date", "title", "lead"):
        if not isinstance(frontmatter.get(field), str) or not frontmatter[field].strip():
            errors.append(f"missing non-empty front matter field {field}")
    lead = str(frontmatter.get("lead", ""))
    sentence_endings = len(re.findall(r"[.!?](?:[\"'”’)]*)?(?:\s|$)", lead))
    if lead and sentence_endings != 1:
        errors.append("lead must be one sentence")

    headings = [(len(markers), title.strip()) for markers, title in HEADING_PATTERN.findall(body)]
    signal_headings = [title for level, title in headings if level == 2 and title.lower().startswith("signal")]
    if not 1 <= len(signal_headings) <= maximum_signals:
        maximum_label = {1: "one", 2: "two", 3: "three"}.get(
            maximum_signals, str(maximum_signals)
        )
        errors.append(
            f"pulse must contain between one and {maximum_label} level-two Signal sections"
        )
    normalized_headings = {re.sub(r"[^a-z ]", "", title.lower()).strip() for _, title in headings}
    required_headings = {
        item.replace("-", " ")
        for item in required_sections
        if item not in {"lead", "signals"}
    }
    for required in sorted(required_headings):
        if required not in normalized_headings:
            errors.append(f"missing required section: {required}")

    body_without_code = re.sub(r"```.*?```", " ", body, flags=re.DOTALL)
    body_without_math = re.sub(r"\$\$.*?\$\$", " ", body_without_code, flags=re.DOTALL)
    body_without_links = re.sub(r"!?\[([^\]]*)\]\([^)]+\)", r"\1", body_without_math)
    body_without_markup = re.sub(r"^#{1,6}\s+", "", body_without_links, flags=re.MULTILINE)
    word_count = len(WORD_PATTERN.findall(body_without_markup))
    if not minimum_words <= word_count <= maximum_words:
        errors.append(f"pulse word count {word_count} is outside {minimum_words}–{maximum_words}")

    artifact_manifests = frontmatter.get("artifact_manifests", [])
    if not isinstance(artifact_manifests, list) or not all(isinstance(item, str) for item in artifact_manifests):
        errors.append("artifact_manifests must be a string list")
        artifact_manifests = []
    has_visual = bool(
        frontmatter.get("featured_artifact")
        and artifact_manifests
        or re.search(r"!\[[^\]]*\]\([^)]+\)", body)
        or re.search(r"```mermaid\s", body, re.IGNORECASE)
    )
    if require_meaningful_artifact and not has_visual:
        errors.append("pulse must reference one chart, diagram, or meaningful image")

    declared_source_ids = frontmatter.get("source_ids", [])
    if not isinstance(declared_source_ids, list) or not declared_source_ids:
        errors.append("source_ids must be a non-empty list")
    elif source_ids is not None:
        unknown = sorted(set(declared_source_ids) - source_ids)
        if unknown:
            errors.append(f"unknown source_ids: {unknown}")

    if RAW_HTML_PATTERN.search(body):
        errors.append("active raw HTML is forbidden in pulse Markdown")
    declared_source_set = {
        item for item in declared_source_ids if isinstance(item, str)
    } if isinstance(declared_source_ids, list) else set()
    source_citation_count = _validate_links(
        body,
        project_root,
        link_base_directory or path.parent,
        errors,
        source_ids=source_ids,
        declared_source_ids=declared_source_set,
    )
    if source_citation_count == 0:
        errors.append("pulse must contain at least one source citation")
    for manifest_url in artifact_manifests:
        _validate_public_url(manifest_url, project_root, errors, "artifact manifest")

    if errors:
        raise ValidationError(f"invalid pulse {path}:\n- " + "\n- ".join(sorted(errors)))
    return PulseValidationResult(path, word_count, len(signal_headings), tuple(artifact_manifests))


def _validate_links(
    body: str,
    project_root: Path,
    pulse_directory: Path,
    errors: list[str],
    *,
    source_ids: set[str] | None,
    declared_source_ids: set[str],
) -> int:
    source_citation_count = 0
    for target in LINK_PATTERN.findall(body):
        target = target.strip().split()[0]
        parsed = urlparse(target)
        if parsed.scheme in {"http", "https", "mailto"}:
            continue
        if parsed.scheme:
            errors.append(f"unsafe link scheme: {target}")
            continue
        if parsed.netloc:
            errors.append(f"protocol-relative links are forbidden: {target}")
            continue
        if target.startswith("#"):
            continue
        if parsed.path == "/sources" and parsed.fragment:
            source_citation_count += 1
            citation_id = parsed.fragment
            if source_ids is not None and citation_id not in source_ids:
                errors.append(f"citation references an unknown source: {target}")
            elif citation_id not in declared_source_ids:
                errors.append(f"citation source is not declared in front matter: {target}")
            continue
        if target == "/" or any(
            target == route or target.startswith(f"{route}#") or target.startswith(f"{route}?")
            for route in ("/sources", "/archive", "/research-map", "/artifacts")
        ):
            continue
        if target.startswith("/artifacts/"):
            _validate_public_url(target, project_root, errors, "artifact link")
            continue
        if parsed.path.startswith("/"):
            errors.append(f"unknown application route: {target}")
            continue
        relative = unquote(parsed.path)
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or not relative:
            errors.append(f"unsafe local link: {target}")
            continue
        candidate = pulse_directory.joinpath(*pure.parts)
        try:
            project_relative = candidate.resolve(strict=True).relative_to(project_root.resolve(strict=True))
            resolve_regular_file_under_root(project_root, project_relative.as_posix())
        except Exception:
            # Pipeline path errors are intentionally collapsed into a stable
            # user-facing validation message.
            errors.append(f"missing or unsafe local link: {target}")
    return source_citation_count


def _validate_public_url(url: str, project_root: Path, errors: list[str], label: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not url.startswith("/artifacts/")
        or url.startswith("//")
        or "%" in url
        or "\\" in url
        or any(ord(character) < 32 or ord(character) == 127 for character in url)
    ):
        errors.append(f"{label} must use a stable /artifacts/ URL: {url}")
        return
    relative = unquote(parsed.path.removeprefix("/"))
    pure = PurePosixPath(relative)
    if ".." in pure.parts:
        errors.append(f"unsafe {label}: {url}")
        return
    try:
        resolve_regular_file_under_root(project_root / "public", pure.as_posix())
    except Exception:
        errors.append(f"missing {label}: {url}")
