from __future__ import annotations

import copy
import hashlib
import json
import shutil
from pathlib import Path

import pytest
from pypdf import PdfWriter

import research_pipeline.automatic as automatic_module
import research_pipeline.scout_security as scout_security_module
from research_pipeline.automatic import (
    load_and_materialize_automatic_package,
    validate_automatic_package,
)
from research_pipeline.errors import PublicationError, ValidationError


PROJECT = Path(__file__).resolve().parents[2]
DATE = "2026-07-24"
CANDIDATE_ID = "candidate-arxiv-11111111111111111111"
CANDIDATE_SHA = "2" * 64
BATCH_ID = "external-batch-33333333333333333333"
SOURCE_ID = "src-external-arxiv-2607-12345v1"
SECOND_CANDIDATE_ID = "candidate-arxiv-22222222222222222222"
SECOND_CANDIDATE_SHA = "5" * 64
SECOND_SOURCE_ID = "src-external-arxiv-2607-54321v1"


@pytest.fixture(autouse=True)
def _approved_security_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    def approved(
        root: Path, _run_date: str, candidates: list[dict]
    ) -> dict[tuple[str, str], dict[str, object]]:
        package = json.loads(
            (root / "data" / "automatic" / "packages" / f"{DATE}.json").read_text()
        )
        by_url = {source["url"]: source for source in package["sources"]}
        return {
            (candidate["id"], candidate["candidate_sha256"]): {
                "content_sha256": by_url[candidate["canonical_url"]][
                    "content_sha256"
                ],
                "logical_path": by_url[candidate["canonical_url"]]["relative_path"],
            }
            for candidate in candidates
        }

    monkeypatch.setattr(
        scout_security_module, "approved_evidence_by_candidate", approved
    )


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
        "schema_version": "2.0.0",
        "date": DATE,
        "article_mode": "deep_dive",
        "candidates": [
            {
                "batch_id": BATCH_ID,
                "candidate_id": CANDIDATE_ID,
                "candidate_sha256": CANDIDATE_SHA,
            }
        ],
        "editor": {
            "mode": "automatic_fail_closed",
            "model": "gpt-5.6-sol",
            "reviewed_at": "2026-07-24T05:00:00Z",
            "rationale": "Exact topical primary evidence was inspected and bound to one page-level claim.",
        },
        "sources": [
            {
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
        ],
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
            "source_citations": [
                {
                    "source_id": SOURCE_ID,
                    "label": "Ada Example (2026)",
                    "locator": "arXiv test fixture, p. 1",
                }
            ],
        },
        "artifacts": [
            {
                "kind": "diagram",
                "slug": "automatic-test",
                "title": "A bounded automatic path",
                "caption": "A passive test diagram.",
                "relation_to_report": "It explains the verified editorial path.",
                "nodes": [
                    {"id": "source", "label": "Primary source"},
                    {"id": "claim", "label": "Verified claim"},
                ],
                "edges": [
                    {"from": "source", "to": "claim", "label": "page evidence"}
                ],
                "limitations": ["Test diagram only."],
            },
            {
                "kind": "generated_image",
                "slug": "automatic-illustration",
                "title": "A bounded automatic illustration",
                "caption": "A visual test fixture. Conceptual illustration — not research evidence",
                "relation_to_report": "It gives a second visual perspective.",
                "limitations": ["Synthetic test bytes only."],
                "source_path": "tmp/automatic-visuals/automatic-illustration.png",
                "sha256": "",
                "media_type": "image/png",
                "generation": {
                    "model": "test-image-model",
                    "prompt": "Create a restrained scientific illustration for this automatic test fixture. Conceptual illustration — not research evidence",
                    "generated_at": "2026-07-24T05:00:00Z",
                    "source_reference": {
                        "source_id": SOURCE_ID,
                        "source_sha256": digest,
                        "locator": {
                            "kind": "pdf",
                            "path": source_path,
                            "page": 1,
                        },
                    },
                    "reproduction_policy": "scientific-content-faithful_visual-form-original",
                },
            },
        ],
    }
    image_payload = b"\x89PNG\r\n\x1a\nfixture"
    image_path = root / "tmp" / "automatic-visuals" / "automatic-illustration.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(image_payload)
    package["artifacts"][1]["sha256"] = hashlib.sha256(image_payload).hexdigest()
    path = root / "data" / "automatic" / "packages" / f"{DATE}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(package), encoding="utf-8")
    return package, candidate


def _synthesis_package(root: Path) -> tuple[dict, list[dict]]:
    package, first_candidate = _package(root)
    second_candidate = {
        **first_candidate,
        "id": SECOND_CANDIDATE_ID,
        "candidate_sha256": SECOND_CANDIDATE_SHA,
        "title": "A complementary robust local estimator",
        "canonical_url": "https://arxiv.org/abs/2607.54321v1",
    }
    second_source = copy.deepcopy(package["sources"][0])
    second_source.update(
        {
            "id": SECOND_SOURCE_ID,
            "title": second_candidate["title"],
            "url": second_candidate["canonical_url"],
            "location": second_candidate["canonical_url"],
            "relative_path": "external/arxiv/2607.54321v1.pdf",
        }
    )
    package["article_mode"] = "synthesis"
    package["candidates"].append(
        {
            "batch_id": BATCH_ID,
            "candidate_id": SECOND_CANDIDATE_ID,
            "candidate_sha256": SECOND_CANDIDATE_SHA,
        }
    )
    package["sources"].append(second_source)
    package["knowledge"]["claims"][0]["evidence"].append(
        {
            "source_id": SECOND_SOURCE_ID,
            "source_sha256": second_source["content_sha256"],
            "role": "supporting",
            "locator": {
                "kind": "pdf",
                "path": second_source["relative_path"],
                "page": 2,
            },
        }
    )
    package["pulse"]["source_citations"] = [
        {
            "source_id": SOURCE_ID,
            "label": "Ada Example (2026), filtering",
            "locator": "arXiv 2607.12345v1, p. 1",
        },
        {
            "source_id": SECOND_SOURCE_ID,
            "label": "Ada Example (2026), local estimation",
            "locator": "arXiv 2607.54321v1, p. 2",
        },
    ]
    package_path = root / "data" / "automatic" / "packages" / f"{DATE}.json"
    package_path.write_text(json.dumps(package), encoding="utf-8")
    return package, [first_candidate, second_candidate]


def test_automatic_package_materializes_append_only_records_and_rolls_back(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    package, candidate = _package(root)
    outcome = load_and_materialize_automatic_package(
        root, DATE, batch_id=BATCH_ID, candidates=[candidate]
    )
    assert outcome is not None
    assert outcome.source_ids == (SOURCE_ID,)
    assert outcome.knowledge_ids == ("claim-automatic-test",)
    assert outcome.artifact_ids == (
        "automatic-automatic-test-2026-07-24",
        "automatic-automatic-illustration-2026-07-24",
    )
    assert len(outcome.artifact_manifest_urls) == 2
    assert (root / "knowledge" / "curated" / "sources.jsonl").is_file()
    assert (root / "public" / "artifacts" / DATE / "automatic-test" / "manifest.json").is_file()
    svg = (root / "public" / "artifacts" / DATE / "automatic-test" / "automatic-test.svg").read_text(
        encoding="utf-8"
    )
    assert 'y="155"' in svg
    assert 'y="216"' not in svg
    assert "A bounded automatic path" in svg
    assert (
        root
        / "public"
        / "artifacts"
        / DATE
        / "automatic-illustration"
        / "automatic-illustration.png"
    ).is_file()
    assert (root / "data" / "automatic" / "extracts" / f"{SOURCE_ID}.jsonl").is_file()

    outcome.rollback()
    assert not (root / "knowledge" / "curated" / "sources.jsonl").exists()
    assert not (root / "public" / "artifacts" / DATE / "automatic-test" / "manifest.json").exists()
    assert not (
        root
        / "public"
        / "artifacts"
        / DATE
        / "automatic-illustration"
        / "manifest.json"
    ).exists()
    assert not (root / "data" / "automatic" / "extracts" / f"{SOURCE_ID}.jsonl").exists()
    assert package["sources"][0]["content_sha256"]


def test_multi_source_synthesis_binds_and_materializes_every_primary_pdf(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    _, candidates = _synthesis_package(root)

    outcome = load_and_materialize_automatic_package(
        root, DATE, batch_id=BATCH_ID, candidates=candidates
    )

    assert outcome is not None
    assert outcome.source_ids == (SOURCE_ID, SECOND_SOURCE_ID)
    assert (
        root
        / "data"
        / "automatic"
        / "extracts"
        / f"{SECOND_SOURCE_ID}.jsonl"
    ).is_file()
    stored_sources = [
        json.loads(line)
        for line in (
            root / "knowledge" / "curated" / "sources.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]
    assert [source["id"] for source in stored_sources] == [
        SOURCE_ID,
        SECOND_SOURCE_ID,
    ]
    proposal_fingerprint = "a" * 64
    proposal = outcome.proposal(
        run_date=DATE,
        pulse_index=1,
        release_id="release-synthesis-test",
        analysis={
            "id": "analysis-synthesis-test",
            "analysis_fingerprint": "b" * 64,
            "selected_candidate_fingerprints": [proposal_fingerprint],
            "ranked_candidates": [
                {
                    "proposal_fingerprint": proposal_fingerprint,
                    "object_id": "claim-automatic-test",
                }
            ],
        },
        schema_path=root / "schemas" / "pulse-proposal.schema.json",
    )
    assert proposal["source_ids"] == [SOURCE_ID, SECOND_SOURCE_ID]
    assert len(proposal["sources"]) == 2
    assert len(proposal["signals"][0]["evidence"]) == 2


def test_multi_source_synthesis_rejects_an_unused_selected_source(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    package, candidates = _synthesis_package(root)
    package["knowledge"]["claims"][0]["evidence"] = package["knowledge"]["claims"][0][
        "evidence"
    ][:1]
    package_path = root / "data" / "automatic" / "packages" / f"{DATE}.json"
    package_path.write_text(json.dumps(package), encoding="utf-8")

    with pytest.raises(
        PublicationError, match="knowledge evidence must use every selected source"
    ):
        validate_automatic_package(
            root, DATE, batch_id=BATCH_ID, candidates=candidates
        )


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


def test_automatic_package_rejects_removed_v1_contract(tmp_path: Path) -> None:
    root = _project(tmp_path)
    package, candidate = _package(root)
    package["schema_version"] = "1.0.0"
    package_path = root / "data" / "automatic" / "packages" / f"{DATE}.json"
    package_path.write_text(json.dumps(package), encoding="utf-8")

    with pytest.raises(ValidationError, match="automatic editorial package"):
        validate_automatic_package(
            root, DATE, batch_id=BATCH_ID, candidates=[candidate]
        )


def test_automatic_package_preflight_is_read_only(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _, candidate = _package(root)

    validation = validate_automatic_package(
        root, DATE, batch_id=BATCH_ID, candidates=[candidate]
    )

    assert validation is not None
    assert validation.sources[0]["id"] == SOURCE_ID
    assert validation.pulse_ids == ("claim-automatic-test",)
    assert not (root / "knowledge" / "curated" / "sources.jsonl").exists()
    assert not (root / "public" / "artifacts").exists()
    assert not (root / "data" / "automatic" / "extracts").exists()


def test_automatic_package_preflight_explains_exact_author_mismatch(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    package, candidate = _package(root)
    package["sources"][0]["authors"] = ["Ada Lovelace"]
    package_path = root / "data" / "automatic" / "packages" / f"{DATE}.json"
    package_path.write_text(json.dumps(package), encoding="utf-8")

    with pytest.raises(
        PublicationError,
        match="source authors do not exactly match.*copy the candidate authors verbatim",
    ):
        validate_automatic_package(
            root, DATE, batch_id=BATCH_ID, candidates=[candidate]
        )

    assert not (root / "knowledge" / "curated" / "sources.jsonl").exists()
    assert not (root / "public" / "artifacts").exists()


def test_automatic_package_rejects_source_version_in_accepted_release(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    package, candidate = _package(root)
    release_path = "data/releases/release-11111111111111111111"
    sources = root / release_path / "sources.jsonl"
    sources.parent.mkdir(parents=True)
    sources.write_text(json.dumps(package["sources"][0]) + "\n", encoding="utf-8")

    with pytest.raises(PublicationError, match="source version is already accepted"):
        load_and_materialize_automatic_package(
            root,
            DATE,
            batch_id=BATCH_ID,
            candidates=[candidate],
            checkpoint={"release_path": release_path},
        )

    assert not (root / "public" / "artifacts").exists()
    assert not (root / "data" / "automatic" / "extracts").exists()


def test_automatic_generated_image_requires_exact_source_page_brief(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    package, candidate = _package(root)
    package["artifacts"][1]["generation"]["source_reference"]["locator"]["page"] = 3
    package_path = root / "data" / "automatic" / "packages" / f"{DATE}.json"
    package_path.write_text(json.dumps(package), encoding="utf-8")

    with pytest.raises(PublicationError, match="source-page brief"):
        load_and_materialize_automatic_package(
            root, DATE, batch_id=BATCH_ID, candidates=[candidate]
        )
    assert not (root / "public" / "artifacts").exists()


@pytest.mark.parametrize("pulse_name", [f"{DATE}.md", f"{DATE}-1.md"])
def test_consumed_automatic_package_is_ignored_before_schema_validation(
    tmp_path: Path, pulse_name: str,
) -> None:
    root = _project(tmp_path)
    package, candidate = _package(root)
    package["diagram"] = package.pop("artifacts")[0]
    package_path = root / "data" / "automatic" / "packages" / f"{DATE}.json"
    package_path.write_text(json.dumps(package), encoding="utf-8")
    pulse_path = f"content/pulses/{pulse_name}"
    checkpoint = {
        "accepted_pulses": [pulse_path],
        "accepted_publications": [{"pulse": pulse_path}],
    }

    outcome = load_and_materialize_automatic_package(
        root,
        DATE,
        batch_id=BATCH_ID,
        candidates=[candidate],
        checkpoint=checkpoint,
    )

    assert outcome is None
    assert not (root / "public" / "artifacts").exists()
    assert not (root / "data" / "automatic" / "extracts").exists()


def test_unconsumed_malformed_package_remains_fail_closed(tmp_path: Path) -> None:
    root = _project(tmp_path)
    package, candidate = _package(root)
    package["diagram"] = package.pop("artifacts")[0]
    package_path = root / "data" / "automatic" / "packages" / f"{DATE}.json"
    package_path.write_text(json.dumps(package), encoding="utf-8")

    with pytest.raises(ValidationError, match="automatic editorial package"):
        load_and_materialize_automatic_package(
            root,
            DATE,
            batch_id=BATCH_ID,
            candidates=[candidate],
            checkpoint={
                "accepted_pulses": [f"content/pulses/{DATE}.md"],
                "accepted_publications": [],
            },
        )

    assert not (root / "public" / "artifacts").exists()


def test_automatic_package_rejects_source_figure_without_reviewed_reuse_rights(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    package, candidate = _package(root)
    figure = package["artifacts"][1]
    figure.update(
        {
            "kind": "source_figure",
            "caption": "A source figure test fixture.",
            "source_id": SOURCE_ID,
            "source_sha256": package["sources"][0]["content_sha256"],
            "locator": {
                "kind": "pdf",
                "path": "external/arxiv/2607.12345v1.pdf",
                "page": 1,
                "section": "Figure 1",
            },
            "rights": {
                "status": "cc_by",
                "license": "CC BY 4.0",
                "creator": "Ada Example",
                "source_url": "https://arxiv.org/abs/2607.12345v1",
                "retrieved_at": "2026-07-24T05:00:00Z",
                "may_publish_publicly": True,
                "local_display_allowed": True,
            },
        }
    )
    figure.pop("generation")
    package_path = root / "data" / "automatic" / "packages" / f"{DATE}.json"
    package_path.write_text(json.dumps(package), encoding="utf-8")

    with pytest.raises(PublicationError, match="reuse clearance"):
        load_and_materialize_automatic_package(
            root, DATE, batch_id=BATCH_ID, candidates=[candidate]
        )
    assert not (root / "public" / "artifacts").exists()


def test_automatic_package_accepts_exact_rights_cleared_source_figure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    package, candidate = _package(root)
    reviewed_rights = {
        "license": "CC BY 4.0",
        "reuse_status": "cleared",
        "public_distribution": True,
    }
    package["sources"][0]["rights"] = reviewed_rights
    figure = package["artifacts"][1]
    figure.update(
        {
            "kind": "source_figure",
            "caption": "A rights-cleared source figure test fixture.",
            "source_id": SOURCE_ID,
            "source_sha256": package["sources"][0]["content_sha256"],
            "locator": {
                "kind": "pdf",
                "path": "external/arxiv/2607.12345v1.pdf",
                "page": 1,
                "section": "Figure 1",
            },
            "rights": {
                "status": "cc_by",
                "license": "CC BY 4.0",
                "creator": "Ada Example",
                "source_url": "https://arxiv.org/abs/2607.12345v1",
                "retrieved_at": "2026-07-24T05:00:00Z",
                "may_publish_publicly": True,
                "local_display_allowed": True,
            },
        }
    )
    figure.pop("generation")
    package_path = root / "data" / "automatic" / "packages" / f"{DATE}.json"
    package_path.write_text(json.dumps(package), encoding="utf-8")
    monkeypatch.setattr(
        automatic_module,
        "_reviewed_candidate_rights",
        lambda _root, _candidate: reviewed_rights,
    )

    outcome = load_and_materialize_automatic_package(
        root, DATE, batch_id=BATCH_ID, candidates=[candidate]
    )
    assert outcome is not None
    manifest = json.loads(
        (
            root
            / "public"
            / "artifacts"
            / DATE
            / "automatic-illustration"
            / "manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["artifact_type"] == "web_image"
    assert manifest["rights"]["status"] == "cc_by"


def test_automatic_package_rejects_source_figure_rights_that_do_not_match_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    package, candidate = _package(root)
    reviewed_rights = {
        "license": "CC BY 4.0",
        "reuse_status": "cleared",
        "public_distribution": True,
    }
    package["sources"][0]["rights"] = reviewed_rights
    figure = package["artifacts"][1]
    figure.update(
        {
            "kind": "source_figure",
            "caption": "A source figure with mismatched rights.",
            "source_id": SOURCE_ID,
            "source_sha256": package["sources"][0]["content_sha256"],
            "locator": {
                "kind": "pdf",
                "path": "external/arxiv/2607.12345v1.pdf",
                "page": 1,
                "section": "Figure 1",
            },
            "rights": {
                "status": "cc0",
                "license": "CC0 1.0",
                "creator": "Ada Example",
                "source_url": "https://arxiv.org/abs/2607.12345v1",
                "retrieved_at": "2026-07-24T05:00:00Z",
                "may_publish_publicly": True,
                "local_display_allowed": True,
            },
        }
    )
    figure.pop("generation")
    package_path = root / "data" / "automatic" / "packages" / f"{DATE}.json"
    package_path.write_text(json.dumps(package), encoding="utf-8")
    monkeypatch.setattr(
        automatic_module,
        "_reviewed_candidate_rights",
        lambda _root, _candidate: reviewed_rights,
    )

    with pytest.raises(PublicationError, match="reuse clearance"):
        load_and_materialize_automatic_package(
            root, DATE, batch_id=BATCH_ID, candidates=[candidate]
        )
    assert not (root / "public" / "artifacts").exists()
