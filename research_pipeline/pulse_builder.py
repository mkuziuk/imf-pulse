"""Render one reviewed structured proposal into deterministic pulse Markdown."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import ValidationError
from .hashing import canonical_json_hash
from .validation import strict_json_loads, validate_records


DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "pulse-proposal.schema.json"
EVIDENCE_LINK = re.compile(
    r"^\[(?P<label>[^\]\r\n]+)\]\(/sources#(?P<source_id>[a-z][a-z0-9._-]*)\)$"
)
SENTENCE_END = re.compile(r"[.!?](?:[\"'”’)]*)?(?:\s|$)")
WORD_PATTERN = re.compile(r"\b[\w’'-]+\b", re.UNICODE)
RAW_HTML_PATTERN = re.compile(
    r"<\s*/?\s*(script|iframe|object|embed|form|style)\b", re.IGNORECASE
)


@dataclass(frozen=True)
class PulseBuildResult:
    path: Path
    pulse_id: str
    proposal_fingerprint: str
    sha256: str
    word_count: int


def proposal_fingerprint(proposal: Mapping[str, Any]) -> str:
    """Return the canonical proposal hash, excluding only its self-hash."""

    identity = dict(proposal)
    identity.pop("proposal_fingerprint", None)
    return canonical_json_hash(identity)


def seal_proposal(proposal: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy carrying the canonical ``proposal_fingerprint``."""

    sealed = dict(proposal)
    sealed["proposal_fingerprint"] = proposal_fingerprint(sealed)
    return sealed


def load_proposal(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        value = strict_json_loads(raw)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValidationError(f"cannot load pulse proposal {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError("pulse proposal must be a JSON object")
    return value


def _narrative_values(proposal: Mapping[str, Any]) -> list[str]:
    values = [
        proposal.get("title"),
        proposal.get("lead"),
        proposal.get("why_this_matters"),
        proposal.get("unresolved_question"),
    ]
    for signal in proposal.get("signals", []):
        if not isinstance(signal, Mapping):
            continue
        values.extend(
            [
                signal.get("heading"),
                signal.get("what_changed"),
                signal.get("why_it_matters"),
                signal.get("confidence"),
                *signal.get("assumptions", []),
                *signal.get("limitations", []),
            ]
        )
    for source in proposal.get("sources", []):
        if isinstance(source, Mapping):
            values.extend([source.get("label"), source.get("locator")])
    return [value for value in values if isinstance(value, str)]


def validate_proposal(
    proposal: Mapping[str, Any], schema_path: Path | None = DEFAULT_SCHEMA_PATH
) -> None:
    if schema_path is not None:
        validate_records([dict(proposal)], schema_path, "pulse proposal")
    errors: list[str] = []
    if proposal.get("status") != "selected":
        errors.append("only a selected proposal can produce a pulse")
    expected_fingerprint = proposal_fingerprint(proposal)
    if proposal.get("proposal_fingerprint") != expected_fingerprint:
        errors.append("proposal_fingerprint does not match canonical proposal bytes")

    title = proposal.get("title")
    lead = proposal.get("lead")
    if isinstance(title, str) and ("\n" in title or "\r" in title):
        errors.append("title must be a single line")
    if isinstance(lead, str):
        if "\n" in lead or "\r" in lead:
            errors.append("lead must be a single line")
        if len(SENTENCE_END.findall(lead)) != 1:
            errors.append("lead must be exactly one sentence")

    manifests = proposal.get("artifact_manifests")
    if not isinstance(manifests, list) or not manifests:
        errors.append("artifact_manifests must contain at least one stable local manifest URL")
        manifests = []
    elif len(manifests) != len(set(manifests)):
        errors.append("artifact_manifests must not contain duplicates")
    for manifest in manifests:
        if not isinstance(manifest, str) or not (
            manifest.startswith("/artifacts/")
            and manifest.endswith("/manifest.json")
            and not any(marker in manifest for marker in ("?", "#", "%", "\\", ".."))
        ):
            errors.append("artifact_manifests contains an unsafe manifest URL")
    if not isinstance(proposal.get("featured_artifact"), str):
        errors.append("featured_artifact must identify exactly one artifact")

    signals = proposal.get("signals", [])
    proposal_fingerprints = proposal.get("proposal_fingerprints", [])
    if isinstance(signals, list) and isinstance(proposal_fingerprints, list):
        actual = [
            signal.get("proposal_fingerprint")
            for signal in signals
            if isinstance(signal, Mapping)
        ]
        if actual != proposal_fingerprints:
            errors.append(
                "signal proposal_fingerprints must exactly match the selected order"
            )

    declared_sources = proposal.get("source_ids", [])
    source_rows = proposal.get("sources", [])
    if isinstance(declared_sources, list) and isinstance(source_rows, list):
        row_ids = [
            row.get("source_id") for row in source_rows if isinstance(row, Mapping)
        ]
        if len(row_ids) != len(set(row_ids)) or set(row_ids) != set(declared_sources):
            errors.append("sources must contain each declared source_id exactly once")
        for index, signal in enumerate(signals, start=1):
            if not isinstance(signal, Mapping):
                continue
            for link in signal.get("evidence", []):
                match = EVIDENCE_LINK.fullmatch(link) if isinstance(link, str) else None
                if match is None:
                    errors.append(f"signal {index} has an invalid evidence Markdown link")
                elif match.group("source_id") not in declared_sources:
                    errors.append(
                        f"signal {index} evidence references an undeclared source"
                    )

    for value in _narrative_values(proposal):
        if RAW_HTML_PATTERN.search(value):
            errors.append("active raw HTML is forbidden in proposal narrative")
            break
        if re.search(r"(?:^|\n)#{1,6}\s", value):
            errors.append("proposal narrative may not inject Markdown headings")
            break

    question = proposal.get("unresolved_question")
    if isinstance(question, str) and not question.rstrip().endswith("?"):
        errors.append("unresolved_question must be phrased as a question")
    if errors:
        raise ValidationError("invalid pulse proposal:\n- " + "\n- ".join(sorted(set(errors))))


def _yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _render_body(proposal: Mapping[str, Any]) -> str:
    sections: list[str] = []
    for index, signal in enumerate(proposal["signals"], start=1):
        assumptions = "; ".join(signal["assumptions"])
        limitations = "; ".join(signal.get("limitations", []))
        boundary = assumptions
        if limitations:
            boundary = f"{boundary} Limitations: {limitations}"
        sections.append(
            "\n\n".join(
                [
                    f"## Signal {index:02d} — {signal['heading']}",
                    f"**What changed.** {signal['what_changed']}",
                    f"**Why it matters.** {signal['why_it_matters']}",
                    f"**Evidence.** {'; '.join(signal['evidence'])}.",
                    f"**Confidence.** {signal['confidence']}",
                    f"**Assumptions and limitations.** {boundary}",
                ]
            )
        )
    sections.extend(
        [
            "\n\n".join(
                [
                    "## Why this matters",
                    proposal["why_this_matters"],
                    (
                        "Featured artifact: "
                        f"[{proposal['featured_artifact']}]({proposal['artifact_manifests'][0]})."
                    ),
                ]
            ),
            "\n\n".join(
                ["## Unresolved question", proposal["unresolved_question"]]
            ),
            "\n\n".join(
                [
                    "## Sources",
                    "\n".join(
                        f"- [{row['label']}, {row['locator']}](/sources#{row['source_id']})"
                        for row in proposal["sources"]
                    ),
                ]
            ),
        ]
    )
    return "\n\n".join(sections).rstrip() + "\n"


def _word_count(body: str) -> int:
    without_code = re.sub(r"```.*?```", " ", body, flags=re.DOTALL)
    without_math = re.sub(r"\$\$.*?\$\$", " ", without_code, flags=re.DOTALL)
    without_links = re.sub(r"!?\[([^\]]*)\]\([^)]+\)", r"\1", without_math)
    without_markup = re.sub(r"^#{1,6}\s+", "", without_links, flags=re.MULTILINE)
    return len(WORD_PATTERN.findall(without_markup))


def render_pulse_markdown(
    proposal: Mapping[str, Any], schema_path: Path | None = DEFAULT_SCHEMA_PATH
) -> str:
    validate_proposal(proposal, schema_path)
    body = _render_body(proposal)
    word_count = _word_count(body)
    if not 350 <= word_count <= 650:
        raise ValidationError(
            f"rendered pulse word count {word_count} is outside 350–650"
        )
    lines = [
        "---",
        'schema_version: "1.0.0"',
        f"id: pulse-{proposal['date']}",
        f"date: {proposal['date']}",
        f"title: {_yaml_string(proposal['title'])}",
        f"lead: {_yaml_string(proposal['lead'])}",
        "status: published",
        "topics:",
        *(f"  - {topic}" for topic in proposal["topics"]),
        f"featured_artifact: {proposal['featured_artifact']}",
        "artifact_manifests:",
        *(f"  - {manifest}" for manifest in proposal["artifact_manifests"]),
        "source_ids:",
        *(f"  - {source_id}" for source_id in proposal["source_ids"]),
        "knowledge_ids:",
        *(f"  - {knowledge_id}" for knowledge_id in proposal["knowledge_ids"]),
        f"word_count: {word_count}",
        "---",
        "",
    ]
    return "\n".join(lines) + body


def _install_no_replace(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".staged", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise ValidationError(f"refusing to overwrite existing pulse: {path}") from exc
        directory_descriptor = os.open(
            path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def build_pulse(
    proposal: Mapping[str, Any],
    output_path: Path,
    schema_path: Path | None = DEFAULT_SCHEMA_PATH,
) -> PulseBuildResult:
    expected_name = f"{proposal.get('date')}.md"
    if output_path.name != expected_name:
        raise ValidationError(
            f"pulse output filename must match proposal date: {expected_name}"
        )
    markdown = render_pulse_markdown(proposal, schema_path)
    payload = markdown.encode("utf-8")
    _install_no_replace(output_path, payload)
    body = markdown.split("\n---\n", 1)[1]
    return PulseBuildResult(
        path=output_path,
        pulse_id=f"pulse-{proposal['date']}",
        proposal_fingerprint=str(proposal["proposal_fingerprint"]),
        sha256=hashlib.sha256(payload).hexdigest(),
        word_count=_word_count(body),
    )
