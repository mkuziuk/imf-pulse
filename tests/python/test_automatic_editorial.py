from __future__ import annotations

import copy
import hashlib
import json
import shutil
from pathlib import Path

import pytest
from pypdf import PdfWriter

from research_pipeline.automatic import load_and_materialize_automatic_package
from research_pipeline.errors import PublicationError


PROJECT = Path(__file__).resolve().parents[2]
DATE = "2026-07-24"
CANDIDATE_ID = "candidate-arxiv-11111111111111111111"
CANDIDATE_SHA = "2" * 64
BATCH_ID = "external-batch-33333333333333333333"
SOURCE_ID = "src-external-arxiv-2607-12345v1"


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    shutil.copytree(PROJECT / "schemas", root / "schemas")
    for filename in (
        "claims.jsonl",
        "methods.jsonl",
        "experiments.jsonl",
        "relationships.jsonl",
    ):
        path = root / "knowledge" / "curated" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    return root


def _package(root: Path) -> tuple[dict, dict]:
    pdf_temp = root / "paper.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_blank_page(width=612, height=792)
    with pdf_temp.open("wb") as handle:
        writer.write(handle)
    payload = pdf_temp.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    evidence = root / "tmp" / "automatic-evidence" / f"{digest}.pdf"
    evidence.parent.mkdir(parents=True)
    evidence.write_bytes(payload)
    pdf_temp.unlink()
    candidate = {
        "id": CANDIDATE_ID,
        "candidate_sha256": CANDIDATE_SHA,
        "provider": "arxiv",
        "source_type": "preprint",
        "title": "Robust iterative filtering of signals",
        "authors": ["Ada Example"],
        "published_at": "2026-07-20T10:00:00Z",
        "canonical_url": "https://arxiv.org/abs/2607.12345v1",
    }
    source_path = "external/arxiv/2607.12345v1.pdf"
    package = {
        "schema_version": "1.0.0",
        "date": DATE,
        "candidate": {
            "batch_id": BATCH_ID,
            "candidate_id": CANDIDATE_ID,
            "candidate_sha256": CANDIDATE_SHA,
        },
        "editor": {
            "mode": "automatic_fail_closed",
            "model": "gpt-5.6-sol",
            "reviewed_at": "2026-07-24T05:00:00Z",
            "rationale": "Exact topical primary evidence was inspected and bound to one page-level claim.",
        },
        "source": {
            "schema_version": "1.0.0",
            "id": SOURCE_ID,
            "title": candidate["title"],
            "authors": candidate["authors"],
            "date": "2026-07-20",
            "source_type": "preprint",
            "authority_level": "preprint_unreviewed",
            "publication_status": "preprint",
            "topics": ["iterative-filtering"],
            "relative_path": source_path,
            "url": candidate["canonical_url"],
            "location": candidate["canonical_url"],
            "rights": {
                "license": "unknown",
                "reuse_status": "internal_only",
                "public_distribution": False,
            },
            "content_sha256": digest,
            "limitations": ["Test preprint."],
            "retrieved_at": "2026-07-24T05:00:00Z",
            "last_processed_at": "2026-07-24T05:00:00Z",
            "extractor": "pdf-pages-automatic-v1",
        },
        "knowledge": {
            "claims": [
                {
                    "schema_version": "1.0.0",
                    "id": "claim-automatic-test",
                    "created_at": "2026-07-24T05:00:00Z",
                    "updated_at": "2026-07-24T05:00:00Z",
                    "normalized_text": "The test paper states one bounded automatic claim.",
                    "statement_kind": "method_definition",
                    "evidence_status": "observed",
                    "scope": "Test scope.",
                    "assumptions": ["The PDF page is the reviewed version."],
                    "confidence": {
                        "level": "high",
                        "score": 0.95,
                        "rationale": "The locator resolves to the exact PDF page.",
                    },
                    "evidence": [
                        {
                            "source_id": SOURCE_ID,
                            "source_sha256": digest,
                            "role": "direct",
                            "locator": {
                                "kind": "pdf",
                                "path": source_path,
                                "page": 1,
                            },
                        }
                    ],
                }
            ],
            "methods": [],
            "experiments": [],
            "relationships": [],
        },
        "pulse": {
            "title": "A bounded automatic test",
            "lead": "One exact primary source supplies one page-bound research signal.",
            "topics": ["iterative-filtering"],
            "signals": [
                {
                    "knowledge_id": "claim-automatic-test",
                    "heading": "One bounded signal",
                    "what_changed": "A primary paper was added through the automatic package.",
                    "why_it_matters": "The claim is independently locatable.",
                    "confidence": "High within the test fixture.",
                    "assumptions": ["The exact PDF hash remains available."],
                    "limitations": ["This is a test fixture."],
                }
            ],
            "why_this_matters": "The transaction can validate automatic evidence without weakening provenance.",
            "unresolved_question": "Will an invalid candidate hash remain fail-closed?",
            "source_label": "Ada Example (2026)",
            "source_locator": "arXiv test fixture, p. 1",
        },
        "diagram": {
            "slug": "automatic-test",
            "title": "A bounded automatic path",
            "caption": "A passive test diagram.",
            "nodes": [
                {"id": "source", "label": "Primary source"},
                {"id": "claim", "label": "Verified claim"},
            ],
            "edges": [{"from": "source", "to": "claim", "label": "page evidence"}],
            "limitations": ["Test diagram only."],
        },
    }
    path = root / "data" / "automatic" / "packages" / f"{DATE}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(package), encoding="utf-8")
    return package, candidate


def test_automatic_package_materializes_append_only_records_and_rolls_back(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    package, candidate = _package(root)
    outcome = load_and_materialize_automatic_package(
        root, DATE, batch_id=BATCH_ID, candidates=[candidate]
    )
    assert outcome is not None
    assert outcome.source_id == SOURCE_ID
    assert outcome.knowledge_ids == ("claim-automatic-test",)
    assert (root / "knowledge" / "curated" / "sources.jsonl").is_file()
    assert (root / "public" / "artifacts" / DATE / "automatic-test" / "manifest.json").is_file()
    assert (root / "data" / "automatic" / "extracts" / f"{SOURCE_ID}.jsonl").is_file()

    outcome.rollback()
    assert not (root / "knowledge" / "curated" / "sources.jsonl").exists()
    assert not (root / "public" / "artifacts" / DATE / "automatic-test" / "manifest.json").exists()
    assert not (root / "data" / "automatic" / "extracts" / f"{SOURCE_ID}.jsonl").exists()
    assert package["source"]["content_sha256"]


def test_automatic_package_rejects_candidate_hash_mismatch_without_writes(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    _, candidate = _package(root)
    tampered = copy.deepcopy(candidate)
    tampered["candidate_sha256"] = "9" * 64
    with pytest.raises(PublicationError, match="candidate hash"):
        load_and_materialize_automatic_package(
            root, DATE, batch_id=BATCH_ID, candidates=[tampered]
        )
    assert not (root / "knowledge" / "curated" / "sources.jsonl").exists()
    assert not (root / "public" / "artifacts").exists()
