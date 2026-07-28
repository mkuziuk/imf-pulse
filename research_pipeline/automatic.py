"""Fail-closed automatic editorial packages for scheduled literature pulses.

The scheduled model may prepare one ignored JSON package and one private arXiv
PDF. This module treats both as untrusted input: it binds the package to an
exact metadata candidate, validates the primary PDF, extracts page text without
executing it, validates every knowledge object and locator, validates one or
more explanatory artifacts, and materializes only append-only public records.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from pypdf import PdfReader

from .errors import PublicationError
from .external_identity import (
    accepted_external_identities,
    normalize_external_identity,
)
from .hashing import canonical_json_bytes, canonical_json_hash, sha256_file
from .paths import open_regular_file_under_root
from .pulse_builder import seal_proposal, validate_proposal
from .validation import read_jsonl, strict_json_loads, validate_records


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SOURCE_ID_RE = re.compile(r"^src-external-arxiv-[a-z0-9-]+$")
MAX_PDF_BYTES = 32 * 1024 * 1024
MAX_EXTRACTED_TEXT_BYTES = 8 * 1024 * 1024
MAX_AUTOMATIC_IMAGE_BYTES = 20 * 1024 * 1024


def _atomic_replace(path: Path, payload: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".staged", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _canonical_json(value: Any) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def _jsonl_bytes(records: Sequence[Mapping[str, Any]]) -> bytes:
    return b"".join(canonical_json_bytes(record) + b"\n" for record in records)


@dataclass
class AutomaticArtifactPayload:
    artifact_id: str
    manifest_url: str
    slug: str
    files: tuple[tuple[str, bytes], ...]
    manifest_payload: bytes


@dataclass(frozen=True)
class AutomaticPackageValidation:
    """Read-only validation result used before the publication transaction."""

    package: Mapping[str, Any]
    sources: tuple[Mapping[str, Any], ...]
    units: tuple[Mapping[str, Any], ...]
    artifact_payloads: tuple[AutomaticArtifactPayload, ...]
    pulse_ids: tuple[str, ...]


@dataclass
class AutomaticMaterialization:
    package: Mapping[str, Any]
    artifact_ids: tuple[str, ...]
    artifact_manifest_urls: tuple[str, ...]
    source_ids: tuple[str, ...]
    knowledge_ids: tuple[str, ...]
    _backups: dict[Path, bytes | None] = field(default_factory=dict)
    committed: bool = False

    def capture(self, path: Path) -> None:
        if path not in self._backups:
            self._backups[path] = path.read_bytes() if path.exists() else None

    def install(self, path: Path, payload: bytes, mode: int = 0o644) -> None:
        self.capture(path)
        if self._backups[path] == payload:
            return
        _atomic_replace(path, payload, mode)

    def rollback(self) -> None:
        if self.committed:
            return
        for path, payload in reversed(list(self._backups.items())):
            if payload is None:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            else:
                _atomic_replace(path, payload, 0o600 if "data/automatic" in path.as_posix() else 0o644)
        self._backups.clear()

    def proposal(
        self,
        *,
        run_date: str,
        pulse_index: int,
        release_id: str,
        analysis: Mapping[str, Any],
        schema_path: Path,
    ) -> dict[str, Any]:
        pulse = self.package["pulse"]
        by_knowledge = {signal["knowledge_id"]: signal for signal in pulse["signals"]}
        selected = list(analysis.get("selected_candidate_fingerprints", []))
        fingerprint_to_id = {
            row.get("proposal_fingerprint"): row.get("object_id")
            for row in analysis.get("ranked_candidates", [])
            if isinstance(row, Mapping)
        }
        ordered_ids = [fingerprint_to_id.get(fingerprint) for fingerprint in selected]
        if (
            not selected
            or any(not isinstance(item, str) for item in ordered_ids)
            or set(ordered_ids) != set(self.knowledge_ids)
            or set(by_knowledge) != set(self.knowledge_ids)
        ):
            raise PublicationError(
                "automatic pulse signals do not exactly match deterministic novelty selection"
            )

        knowledge_records = {
            record["id"]: record
            for values in self.package["knowledge"].values()
            for record in values
        }
        rendered_signals: list[dict[str, Any]] = []
        for fingerprint, knowledge_id in zip(selected, ordered_ids, strict=True):
            source_signal = by_knowledge[str(knowledge_id)]
            evidence = knowledge_records[str(knowledge_id)]["evidence"]
            pages_by_source: dict[str, set[int]] = {}
            for item in evidence:
                if not isinstance(item, Mapping):
                    continue
                locator = item.get("locator")
                source_id = item.get("source_id")
                if (
                    isinstance(source_id, str)
                    and isinstance(locator, Mapping)
                    and isinstance(locator.get("page"), int)
                ):
                    pages_by_source.setdefault(source_id, set()).add(locator["page"])
            evidence_links = [
                (
                    f"[Primary paper, p. {', '.join(str(page) for page in sorted(pages))}]"
                    f"(/sources#{source_id})"
                )
                for source_id, pages in sorted(pages_by_source.items())
            ]
            if not evidence_links:
                raise PublicationError(
                    "automatic knowledge record has no renderable primary evidence"
                )
            rendered_signals.append(
                {
                    "heading": source_signal["heading"],
                    "proposal_fingerprint": fingerprint,
                    "what_changed": source_signal["what_changed"],
                    "why_it_matters": source_signal["why_it_matters"],
                    "evidence": evidence_links,
                    "confidence": source_signal["confidence"],
                    "assumptions": source_signal["assumptions"],
                    "limitations": source_signal["limitations"],
                }
            )
        proposal = seal_proposal(
            {
                "schema_version": "1.0.0",
                "id": f"automatic-proposal-{run_date}-{pulse_index}",
                "status": "selected",
                "date": run_date,
                "pulse_index": pulse_index,
                "candidate_release_id": release_id,
                "analysis_id": analysis["id"],
                "analysis_fingerprint": analysis["analysis_fingerprint"],
                "proposal_fingerprints": selected,
                "reason": "A fail-closed automatic editorial package matched the exact novelty analysis.",
                "title": pulse["title"],
                "lead": pulse["lead"],
                "topics": pulse["topics"],
                "featured_artifact": self.artifact_ids[0],
                "artifact_manifests": list(self.artifact_manifest_urls),
                "source_ids": list(self.source_ids),
                "knowledge_ids": [str(item) for item in ordered_ids],
                "signals": rendered_signals,
                "why_this_matters": pulse["why_this_matters"],
                "unresolved_question": pulse["unresolved_question"],
                "sources": list(pulse["source_citations"]),
            }
        )
        validate_proposal(proposal, schema_path)
        return proposal


def _read_package(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise PublicationError("automatic editorial package is absent, unsafe, or oversized")
    try:
        value = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise PublicationError("automatic editorial package is invalid JSON") from exc
    if not isinstance(value, dict):
        raise PublicationError("automatic editorial package must be an object")
    return value


def _candidates_for_package(
    package: Mapping[str, Any],
    batch_id: str | None,
    candidates: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    bindings = package["candidates"]
    if batch_id is None:
        raise PublicationError("automatic package has no current metadata batch")
    # Provider receipts and therefore batch hashes may change between the
    # discovery pass and the publication transaction. The normalized candidate
    # hash is the stable content identity; the originating batch id remains in
    # the package as audit provenance.
    selected: list[Mapping[str, Any]] = []
    identities: set[tuple[str, str]] = set()
    for binding in bindings:
        identity = (binding["candidate_id"], binding["candidate_sha256"])
        if identity in identities:
            raise PublicationError("automatic package repeats a candidate identity")
        identities.add(identity)
        matches = [
            candidate
            for candidate in candidates
            if candidate.get("id") == binding["candidate_id"]
            and candidate.get("candidate_sha256") == binding["candidate_sha256"]
        ]
        if len(matches) != 1:
            raise PublicationError(
                "automatic package candidate hash is absent or ambiguous"
            )
        candidate = matches[0]
        if (
            candidate.get("provider") != "arxiv"
            or candidate.get("source_type") != "preprint"
        ):
            raise PublicationError(
                "automatic evidence currently permits arXiv preprints only"
            )
        selected.append(candidate)
    return tuple(selected)


def _reviewed_candidate_rights(
    project_root: Path, candidate: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    from .external import load_external_config, lookup_review_decision

    try:
        external = load_external_config(
            project_root / "config" / "external-sources.yaml"
        )
        ledger = external["policy"]["decision_ledger"]
        decision = lookup_review_decision(
            project_root,
            ledger,
            str(candidate["id"]),
            str(candidate["candidate_sha256"]),
        )
    except Exception:
        # Missing or malformed review state never grants source-figure reuse.
        return None
    if decision is None or decision.get("decision") != "approved":
        return None
    rights = decision.get("rights")
    return rights if isinstance(rights, Mapping) else None


def _extract_pdf(
    path: Path, source_id: str, source_sha: str, logical_path: str
) -> tuple[list[dict[str, Any]], str, int]:
    if path.is_symlink() or not path.is_file():
        raise PublicationError("automatic primary PDF is unavailable")
    size = path.stat().st_size
    if not 1 <= size <= MAX_PDF_BYTES or sha256_file(path) != source_sha:
        raise PublicationError("automatic primary PDF hash or size does not match")
    with path.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            raise PublicationError("automatic primary evidence is not a PDF")
    try:
        reader = PdfReader(path, strict=True)
    except Exception as exc:
        raise PublicationError("automatic primary PDF cannot be parsed safely") from exc
    if reader.is_encrypted or not 1 <= len(reader.pages) <= 500:
        raise PublicationError("automatic primary PDF is encrypted or has an unsafe page count")
    root = reader.trailer.get("/Root", {})
    if "/AA" in root:
        raise PublicationError("automatic primary PDF contains additional document actions")
    open_action = root.get("/OpenAction") if isinstance(root, Mapping) else None
    # A page destination such as ``[page, /Fit]`` is inert navigation. Action
    # dictionaries can launch code or external resources and remain forbidden.
    if open_action is not None and not isinstance(open_action, (list, tuple)):
        raise PublicationError("automatic primary PDF contains an active open action")
    names = root.get("/Names") if isinstance(root, Mapping) else None
    if isinstance(names, Mapping) and any(key in names for key in ("/JavaScript", "/EmbeddedFiles")):
        raise PublicationError("automatic primary PDF contains scripts or attachments")

    total = 0
    units: list[dict[str, Any]] = []
    for page_number, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:
            raise PublicationError("automatic primary PDF text extraction failed") from exc
        normalized = "\n".join(line.rstrip() for line in text.replace("\x00", "").splitlines()).strip()
        total += len(normalized.encode("utf-8"))
        if total > MAX_EXTRACTED_TEXT_BYTES:
            raise PublicationError("automatic primary PDF text exceeds the extraction cap")
        identity = {
            "kind": "pdf_page",
            "locator": {"kind": "pdf", "path": logical_path, "page": page_number},
            "text": normalized,
        }
        unit_hash = canonical_json_hash(identity)
        units.append(
            {
                "schema_version": 1,
                "id": f"extract-{source_id}-{source_sha[:12]}-{unit_hash[:20]}",
                "source_id": source_id,
                "source_sha256": source_sha,
                **identity,
                "content_sha256": unit_hash,
            }
        )
    semantic_sha = canonical_json_hash(
        [
            {
                key: value
                for key, value in unit.items()
                if key not in {"id", "source_id", "source_sha256", "schema_version"}
            }
            for unit in units
        ]
    )
    return units, semantic_sha, size


def _validate_package_semantics(
    project_root: Path,
    package: dict[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    reviewed_rights: Sequence[Mapping[str, Any] | None],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if (
        package["editor"].get("mode") != "automatic_fail_closed"
        or package["editor"].get("model") != "gpt-5.6-sol"
    ):
        raise PublicationError("automatic package must identify the approved scheduled model")
    source_rows = package["sources"]
    if not (
        len(source_rows) == len(candidates) == len(reviewed_rights)
        and 1 <= len(source_rows) <= 3
    ):
        raise PublicationError(
            "automatic package must bind one source to each selected candidate"
        )

    sources: list[dict[str, Any]] = []
    all_units: list[dict[str, Any]] = []
    source_context: dict[str, tuple[str, str, int]] = {}
    for raw_source, candidate, candidate_rights in zip(
        source_rows, candidates, reviewed_rights, strict=True
    ):
        source = dict(raw_source)
        source_id = source.get("id")
        if (
            not isinstance(source_id, str)
            or not SOURCE_ID_RE.fullmatch(source_id)
            or source_id in source_context
        ):
            raise PublicationError(
                "automatic source ids must be unique and use the external arXiv namespace"
            )
        source_rights = source.get("rights", {})
        rights_match = isinstance(source_rights, Mapping) and (
            (
                candidate_rights is None
                and source_rights.get("reuse_status") in {"internal_only", "unknown"}
                and source_rights.get("public_distribution") is False
            )
            or (
                candidate_rights is not None
                and source_rights.get("license") == candidate_rights.get("license")
                and source_rights.get("reuse_status")
                == candidate_rights.get("reuse_status")
                and source_rights.get("public_distribution")
                is candidate_rights.get("public_distribution")
            )
        )
        if source.get("title") != candidate.get("title"):
            raise PublicationError(
                "automatic source title does not exactly match its arXiv candidate"
            )
        if source.get("authors") != candidate.get("authors"):
            raise PublicationError(
                "automatic source authors do not exactly match its arXiv candidate; "
                "copy the candidate authors verbatim"
            )
        if source.get("url") != candidate.get("canonical_url"):
            raise PublicationError(
                "automatic source URL does not exactly match its arXiv candidate"
            )
        if (
            source.get("source_type") != "preprint"
            or source.get("authority_level") != "preprint_unreviewed"
            or source.get("publication_status") != "preprint"
        ):
            raise PublicationError(
                "automatic source classification is not the permitted preprint form"
            )
        if not rights_match:
            raise PublicationError(
                "automatic source rights do not match the reviewed rights boundary"
            )
        source_sha = source.get("content_sha256")
        if not isinstance(source_sha, str) or not SHA256_RE.fullmatch(source_sha):
            raise PublicationError("automatic source content hash is invalid")
        expected_date = str(candidate.get("published_at", ""))[:10]
        if source.get("date") != expected_date:
            raise PublicationError(
                "automatic source publication date does not match metadata"
            )
        logical_path = source.get("relative_path")
        if (
            not isinstance(logical_path, str)
            or not re.fullmatch(
                r"external/arxiv/[A-Za-z0-9._-]+\.pdf", logical_path
            )
        ):
            raise PublicationError("automatic source must use a safe logical PDF path")
        evidence_path = (
            project_root / "tmp" / "automatic-evidence" / f"{source_sha}.pdf"
        )
        units, semantic_sha, size = _extract_pdf(
            evidence_path, source_id, source_sha, logical_path
        )
        source["content_hash"] = source_sha
        source["content_size_bytes"] = size
        source["extract_semantic_sha256"] = semantic_sha
        source["status"] = "available"
        validate_records(
            [source],
            project_root / "schemas" / "source.schema.json",
            "automatic source",
        )
        sources.append(source)
        all_units.extend(units)
        source_context[source_id] = (source_sha, logical_path, len(units))

    knowledge_records: list[dict[str, Any]] = []
    seen: set[str] = set(source_context)
    evidence_source_ids: set[str] = set()
    schema_by_group = {
        "claims": "claim.schema.json",
        "methods": "method.schema.json",
        "experiments": "experiment.schema.json",
        "relationships": "relationship.schema.json",
    }
    for group, schema_name in schema_by_group.items():
        records = package["knowledge"][group]
        validate_records(records, project_root / "schemas" / schema_name, f"automatic {group}")
        for record in records:
            record_id = record["id"]
            if record_id in seen:
                raise PublicationError("automatic package contains duplicate object ids")
            seen.add(record_id)
            for evidence in record.get("evidence", []):
                locator = evidence.get("locator", {})
                context = source_context.get(evidence.get("source_id"))
                if (
                    context is None
                    or evidence.get("source_sha256") != context[0]
                    or not isinstance(locator, Mapping)
                    or locator.get("kind") != "pdf"
                    or locator.get("path") != context[1]
                    or not isinstance(locator.get("page"), int)
                    or not 1 <= locator["page"] <= context[2]
                ):
                    raise PublicationError("automatic knowledge evidence is not bound to an exact PDF page")
                evidence_source_ids.add(evidence["source_id"])
            knowledge_records.append(dict(record))
    pulse_ids = tuple(signal["knowledge_id"] for signal in package["pulse"]["signals"])
    knowledge_ids = {record["id"] for record in knowledge_records}
    if len(set(pulse_ids)) != len(pulse_ids) or not set(pulse_ids) <= knowledge_ids:
        raise PublicationError("automatic pulse references unknown or duplicate knowledge ids")
    if package["pulse"]["unresolved_question"].rstrip().endswith("?") is False:
        raise PublicationError("automatic unresolved question must end in a question mark")
    citations = package["pulse"]["source_citations"]
    citation_ids = [citation["source_id"] for citation in citations]
    if (
        len(citation_ids) != len(set(citation_ids))
        or set(citation_ids) != set(source_context)
    ):
        raise PublicationError(
            "automatic source citations must cover every selected source exactly once"
        )
    if package["article_mode"] == "deep_dive" and len(sources) != 1:
        raise PublicationError("automatic deep dive must select exactly one source")
    if package["article_mode"] == "synthesis" and len(sources) < 2:
        raise PublicationError(
            "automatic synthesis must select at least two sources"
        )
    if evidence_source_ids != set(source_context):
        raise PublicationError(
            "automatic knowledge evidence must use every selected source"
        )
    return sources, all_units


def _append_records(
    materialization: AutomaticMaterialization,
    path: Path,
    additions: Sequence[Mapping[str, Any]],
) -> None:
    existing = read_jsonl(path) if path.exists() else []
    by_id = {record["id"]: record for record in existing}
    changed = False
    for addition in additions:
        record_id = addition["id"]
        if record_id in by_id:
            if by_id[record_id] != dict(addition):
                raise PublicationError(f"automatic editorial record would mutate {record_id}")
            continue
        existing.append(dict(addition))
        by_id[record_id] = dict(addition)
        changed = True
    if changed:
        materialization.install(path, _jsonl_bytes(existing))


def _split_label(value: str, maximum: int = 24) -> list[str]:
    words = value.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > maximum:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines[:3]


def _diagram_payloads(
    run_date: str, diagram: Mapping[str, Any]
) -> AutomaticArtifactPayload:
    slug = diagram["slug"]
    artifact_id = f"automatic-{slug}-{run_date}"
    prefix = f"/artifacts/{run_date}/{slug}"
    spec_url = f"{prefix}/{slug}.json"
    svg_url = f"{prefix}/{slug}.svg"
    manifest_url = f"{prefix}/manifest.json"
    nodes = diagram["nodes"]
    node_ids = [node["id"] for node in nodes]
    if len(set(node_ids)) != len(node_ids):
        raise PublicationError("automatic diagram node ids must be unique")
    positions = {
        node_id: (150 + index * (900 / max(1, len(nodes) - 1)), 240)
        for index, node_id in enumerate(node_ids)
    }
    for edge in diagram["edges"]:
        if edge["from"] not in positions or edge["to"] not in positions:
            raise PublicationError("automatic diagram edge references an unknown node")
    spec = {
        "schema_version": 1,
        "artifact_id": artifact_id,
        "title": diagram["title"],
        "nodes": nodes,
        "edges": diagram["edges"],
    }
    svg: list[str] = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 480" role="img" aria-labelledby="title desc">',
        f"<title id=\"title\">{html.escape(diagram['title'])}</title>",
        f"<desc id=\"desc\">{html.escape(diagram['caption'])}</desc>",
        '<defs><marker id="arrow" markerWidth="9" markerHeight="9" refX="8" refY="4.5" orient="auto"><path d="M0,0 L9,4.5 L0,9 Z" fill="#d64f37"/></marker></defs>',
        '<rect width="1200" height="480" fill="#f3efe7"/>',
        f'<text x="70" y="75" font-family="Georgia,serif" font-size="34" fill="#171816">{html.escape(diagram["title"])}</text>',
        '<line x1="70" y1="100" x2="1130" y2="100" stroke="#171816" stroke-width="1"/>',
    ]
    for edge in diagram["edges"]:
        x1, y1 = positions[edge["from"]]
        x2, y2 = positions[edge["to"]]
        svg.append(
            f'<line x1="{x1 + 95:.1f}" y1="{y1:.1f}" x2="{x2 - 95:.1f}" y2="{y2:.1f}" stroke="#d64f37" stroke-width="3" marker-end="url(#arrow)"/>'
        )
        svg.append(
            f'<text x="{(x1 + x2) / 2:.1f}" y="155" text-anchor="middle" font-family="Arial,sans-serif" font-size="15" fill="#62635f">{html.escape(edge["label"])}</text>'
        )
    for node in nodes:
        x, y = positions[node["id"]]
        svg.append(
            f'<rect x="{x - 100:.1f}" y="{y - 64:.1f}" width="200" height="128" rx="4" fill="#fbfaf6" stroke="#171816" stroke-width="2"/>'
        )
        lines = _split_label(node["label"])
        start_y = y - (len(lines) - 1) * 12
        svg.append(
            f'<text x="{x:.1f}" y="{start_y:.1f}" text-anchor="middle" font-family="Georgia,serif" font-size="20" fill="#171816">'
        )
        for index, line in enumerate(lines):
            dy = 0 if index == 0 else 26
            svg.append(
                f'<tspan x="{x:.1f}" dy="{dy}">{html.escape(line)}</tspan>'
            )
        svg.append("</text>")
    svg.extend(
        [
            '<text x="70" y="420" font-family="Arial,sans-serif" font-size="16" fill="#62635f">Project diagram; research evidence remains in the cited paper.</text>',
            "</svg>",
        ]
    )
    svg_payload = "".join(svg).encode("utf-8")
    spec_payload = _canonical_json(spec)
    manifest = {
        "schema_version": 1,
        "artifact_id": artifact_id,
        "artifact_date": run_date,
        "artifact_type": "diagram",
        "title": diagram["title"],
        "caption": diagram["caption"],
        "relation_to_report": diagram["relation_to_report"],
        "stable_url": svg_url,
        "spec_url": spec_url,
        "manifest_url": manifest_url,
        "files": [
            {
                "url": spec_url,
                "role": "declarative diagram specification",
                "media_type": "application/json",
                "sha256": hashlib.sha256(spec_payload).hexdigest(),
                "bytes": len(spec_payload),
            },
            {
                "url": svg_url,
                "role": "responsive accessible rendered diagram",
                "media_type": "image/svg+xml",
                "sha256": hashlib.sha256(svg_payload).hexdigest(),
                "bytes": len(svg_payload),
            },
        ],
        "rights": {
            "status": "project_generated_diagram",
            "may_publish_publicly": True,
            "local_display_allowed": True,
            "public_deployment_requires_owner_approval": False,
            "license": "All rights reserved",
            "creator": "The Residual",
        },
        "limitations": diagram["limitations"],
    }
    return AutomaticArtifactPayload(
        artifact_id=artifact_id,
        manifest_url=manifest_url,
        slug=slug,
        files=((f"{slug}.json", spec_payload), (f"{slug}.svg", svg_payload)),
        manifest_payload=_canonical_json(manifest),
    )


def _read_automatic_image(
    project_root: Path, artifact: Mapping[str, Any]
) -> tuple[bytes, str]:
    source_path = artifact["source_path"]
    pure = PurePosixPath(source_path)
    if (
        pure.is_absolute()
        or pure.parts[:2] != ("tmp", "automatic-visuals")
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise PublicationError("automatic image path is outside private visual staging")
    try:
        with open_regular_file_under_root(project_root, source_path) as descriptor:
            before = os.fstat(descriptor)
            payload = os.read(descriptor, MAX_AUTOMATIC_IMAGE_BYTES + 1)
            after = os.fstat(descriptor)
    except Exception as exc:
        raise PublicationError("automatic image is unavailable or unsafe") from exc
    if (
        not 1 <= len(payload) <= MAX_AUTOMATIC_IMAGE_BYTES
        or len(payload) != before.st_size
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise PublicationError("automatic image is empty, oversized, or changed during read")
    if hashlib.sha256(payload).hexdigest() != artifact["sha256"]:
        raise PublicationError("automatic image hash does not match")
    media_type = artifact["media_type"]
    suffix = pure.suffix.lower()
    if media_type == "image/png":
        if suffix != ".png" or not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            raise PublicationError("automatic PNG type or signature is invalid")
        extension = ".png"
    elif media_type == "image/jpeg":
        if suffix not in {".jpg", ".jpeg"} or not payload.startswith(b"\xff\xd8\xff"):
            raise PublicationError("automatic JPEG type or signature is invalid")
        extension = ".jpg"
    else:
        raise PublicationError("automatic image media type is unsupported")
    return payload, extension


def _image_payloads(
    project_root: Path,
    run_date: str,
    artifact: Mapping[str, Any],
    source: Mapping[str, Any],
    page_count: int,
) -> AutomaticArtifactPayload:
    payload, extension = _read_automatic_image(project_root, artifact)
    slug = artifact["slug"]
    artifact_id = f"automatic-{slug}-{run_date}"
    prefix = f"/artifacts/{run_date}/{slug}"
    image_name = f"{slug}{extension}"
    image_url = f"{prefix}/{image_name}"
    manifest_url = f"{prefix}/manifest.json"
    kind = artifact["kind"]
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "artifact_id": artifact_id,
        "artifact_date": run_date,
        "artifact_type": "generated_image" if kind == "generated_image" else "web_image",
        "title": artifact["title"],
        "caption": artifact["caption"],
        "relation_to_report": artifact["relation_to_report"],
        "stable_url": image_url,
        "manifest_url": manifest_url,
        "files": [
            {
                "url": image_url,
                "role": (
                    "generated conceptual illustration"
                    if kind == "generated_image"
                    else "rights-cleared source figure"
                ),
                "media_type": artifact["media_type"],
                "sha256": artifact["sha256"],
                "bytes": len(payload),
            }
        ],
        "limitations": artifact["limitations"],
    }
    if kind == "generated_image":
        generation = artifact["generation"]
        reference = generation["source_reference"]
        locator = reference["locator"]
        if (
            reference["source_id"] != source["id"]
            or reference["source_sha256"] != source["content_sha256"]
            or locator.get("kind") != "pdf"
            or locator.get("path") != source.get("relative_path")
            or not isinstance(locator.get("page"), int)
            or not 1 <= locator["page"] <= page_count
            or "Conceptual illustration — not research evidence" not in generation["prompt"]
        ):
            raise PublicationError(
                "automatic generated image lacks an exact source-page brief or conceptual label"
            )
        manifest["rights"] = {
            "status": "project_generated_illustration",
            "may_publish_publicly": True,
            "local_display_allowed": True,
            "public_deployment_requires_owner_approval": False,
            "license": "All rights reserved",
            "creator": "The Residual",
        }
        manifest["parameters"] = {"generation": dict(generation)}
    else:
        locator = artifact["locator"]
        source_rights = source.get("rights", {})
        artifact_rights = artifact["rights"]
        if (
            artifact["source_id"] != source["id"]
            or artifact["source_sha256"] != source["content_sha256"]
            or locator.get("kind") != "pdf"
            or locator.get("path") != source.get("relative_path")
            or not isinstance(locator.get("page"), int)
            or not 1 <= locator["page"] <= page_count
            or not isinstance(source_rights, Mapping)
            or source_rights.get("reuse_status") not in {"cleared", "public_domain"}
            or source_rights.get("public_distribution") is not True
            or artifact_rights["license"] != source_rights.get("license")
            or artifact_rights["source_url"] != source.get("url")
        ):
            raise PublicationError(
                "automatic source figure lacks exact evidence or source-level reuse clearance"
            )
        manifest["rights"] = dict(artifact_rights)
        manifest["sources"] = [
            {
                "source_id": source["id"],
                "content_sha256": source["content_sha256"],
                "path": source["relative_path"],
                "role": "source figure",
                "execution_status": "not_executed",
                "rights_status": artifact["rights"]["status"],
                "locators": [dict(locator)],
            }
        ]
    return AutomaticArtifactPayload(
        artifact_id=artifact_id,
        manifest_url=manifest_url,
        slug=slug,
        files=((image_name, payload),),
        manifest_payload=_canonical_json(manifest),
    )


def _artifact_payloads(
    project_root: Path,
    run_date: str,
    artifacts: Sequence[Mapping[str, Any]],
    sources: Sequence[Mapping[str, Any]],
    units: Sequence[Mapping[str, Any]],
) -> tuple[AutomaticArtifactPayload, ...]:
    slugs = [artifact["slug"] for artifact in artifacts]
    if len(slugs) != len(set(slugs)):
        raise PublicationError("automatic artifact slugs must be unique")
    sources_by_id = {str(source["id"]): source for source in sources}
    pages_by_source: dict[str, int] = {}
    for unit in units:
        source_id = str(unit["source_id"])
        pages_by_source[source_id] = pages_by_source.get(source_id, 0) + 1
    payloads: list[AutomaticArtifactPayload] = []
    for artifact in artifacts:
        if artifact["kind"] == "diagram":
            payloads.append(_diagram_payloads(run_date, artifact))
        else:
            source_id = (
                artifact["generation"]["source_reference"]["source_id"]
                if artifact["kind"] == "generated_image"
                else artifact["source_id"]
            )
            source = sources_by_id.get(source_id)
            if source is None:
                raise PublicationError(
                    "automatic image references an unselected source"
                )
            payloads.append(
                _image_payloads(
                    project_root,
                    run_date,
                    artifact,
                    source,
                    pages_by_source[source_id],
                )
            )
    return tuple(payloads)


def _package_was_consumed(
    checkpoint: Mapping[str, Any] | None, run_date: str
) -> bool:
    """Return true only when the accepted history records this date's pulse."""

    if checkpoint is None:
        return False
    expected_pulses = {
        f"content/pulses/{run_date}.md",
        f"content/pulses/{run_date}-1.md",
    }
    accepted_pulses = checkpoint.get("accepted_pulses")
    accepted_publications = checkpoint.get("accepted_publications")
    if not isinstance(accepted_pulses, list) or not isinstance(
        accepted_publications, list
    ):
        return False
    recorded = expected_pulses.intersection(accepted_pulses)
    return bool(recorded) and any(
        isinstance(publication, Mapping)
        and publication.get("pulse") in recorded
        for publication in accepted_publications
    )


def validate_automatic_package(
    project_root: Path,
    run_date: str,
    *,
    batch_id: str | None,
    candidates: Sequence[Mapping[str, Any]],
    checkpoint: Mapping[str, Any] | None = None,
) -> AutomaticPackageValidation | None:
    """Validate today's private package without writing accepted or public files."""

    # A package is single-use input. Once its dated pulse is present in both
    # accepted-history views, a same-day rerun must not revalidate or
    # rematerialize leftover private staging against a newer code/schema
    # version. The release transaction later revalidates the sealed checkpoint.
    if _package_was_consumed(checkpoint, run_date):
        return None
    package_path = project_root / "data" / "automatic" / "packages" / f"{run_date}.json"
    if not package_path.exists():
        return None
    package = _read_package(package_path)
    validate_records(
        [package],
        project_root / "schemas" / "automatic-pulse-package.schema.json",
        "automatic editorial package",
    )
    if package.get("date") != run_date:
        raise PublicationError("automatic editorial package date does not match the run")
    selected_candidates = _candidates_for_package(package, batch_id, candidates)
    try:
        accepted_identities = accepted_external_identities(project_root, checkpoint)
    except Exception as exc:
        raise PublicationError("accepted external source history is invalid") from exc
    selected_identities = [
        normalize_external_identity(candidate.get("canonical_url"))
        for candidate in selected_candidates
    ]
    if (
        any(identity is None for identity in selected_identities)
        or len(set(selected_identities)) != len(selected_identities)
    ):
        raise PublicationError(
            "automatic editorial candidate source version is invalid or repeated"
        )
    if any(identity in accepted_identities for identity in selected_identities):
        raise PublicationError(
            "automatic editorial candidate source version is already accepted"
        )
    reviewed_rights = tuple(
        _reviewed_candidate_rights(project_root, candidate)
        for candidate in selected_candidates
    )
    sources, units = _validate_package_semantics(
        project_root, package, selected_candidates, reviewed_rights
    )
    pulse_ids = tuple(signal["knowledge_id"] for signal in package["pulse"]["signals"])
    artifact_payloads = _artifact_payloads(
        project_root, run_date, package["artifacts"], sources, units
    )
    return AutomaticPackageValidation(
        package=package,
        sources=tuple(sources),
        units=tuple(units),
        artifact_payloads=artifact_payloads,
        pulse_ids=pulse_ids,
    )


def load_and_materialize_automatic_package(
    project_root: Path,
    run_date: str,
    *,
    batch_id: str | None,
    candidates: Sequence[Mapping[str, Any]],
    checkpoint: Mapping[str, Any] | None = None,
) -> AutomaticMaterialization | None:
    validation = validate_automatic_package(
        project_root,
        run_date,
        batch_id=batch_id,
        candidates=candidates,
        checkpoint=checkpoint,
    )
    if validation is None:
        return None
    package = validation.package
    sources = validation.sources
    units = validation.units
    artifact_payloads = validation.artifact_payloads
    materialization = AutomaticMaterialization(
        package=package,
        artifact_ids=tuple(payload.artifact_id for payload in artifact_payloads),
        artifact_manifest_urls=tuple(
            payload.manifest_url for payload in artifact_payloads
        ),
        source_ids=tuple(str(source["id"]) for source in sources),
        knowledge_ids=validation.pulse_ids,
    )
    try:
        _append_records(
            materialization,
            project_root / "knowledge" / "curated" / "sources.jsonl",
            sources,
        )
        group_files = {
            "claims": "claims.jsonl",
            "methods": "methods.jsonl",
            "experiments": "experiments.jsonl",
            "relationships": "relationships.jsonl",
        }
        for group, filename in group_files.items():
            _append_records(
                materialization,
                project_root / "knowledge" / "curated" / filename,
                package["knowledge"][group],
            )
        for source in sources:
            source_units = [
                unit for unit in units if unit["source_id"] == source["id"]
            ]
            materialization.install(
                project_root
                / "data"
                / "automatic"
                / "extracts"
                / f"{source['id']}.jsonl",
                _jsonl_bytes(source_units),
                0o600,
            )
        for artifact_payload in artifact_payloads:
            artifact_root = (
                project_root
                / "public"
                / "artifacts"
                / run_date
                / artifact_payload.slug
            )
            for filename, payload in artifact_payload.files:
                materialization.install(artifact_root / filename, payload)
            materialization.install(
                artifact_root / "manifest.json", artifact_payload.manifest_payload
            )
            validate_records(
                [
                    strict_json_loads(
                        artifact_payload.manifest_payload.decode("utf-8")
                    )
                ],
                project_root / "schemas" / "artifact.schema.json",
                "automatic artifact",
            )
    except BaseException:
        materialization.rollback()
        raise
    return materialization
