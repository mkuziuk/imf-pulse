from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from research_pipeline.errors import ValidationError
from research_pipeline.novelty import NoveltyPolicy, analyze_release_changes
from research_pipeline.pulse_builder import (
    build_pulse,
    render_pulse_markdown,
    seal_proposal,
)
from research_pipeline.pulse_validation import parse_pulse


PROPOSAL_KEY = "a" * 64


def _proposal(**updates: object) -> dict:
    what_changed = " ".join(
        [
            "The exact operator comparison isolates a changed recursive stage profile under the registered finite sample assumptions."
        ]
        * 14
    )
    why_it_matters = " ".join(
        [
            "This matters because the distinction changes which theorem can support the observed numerical contrast."
        ]
        * 8
    )
    synthesis = " ".join(
        [
            "The synthesis keeps operator evidence separate from robust interpretation while retaining the testable connection between them."
        ]
        * 3
    )
    value: dict = {
        "schema_version": "1.0.0",
        "id": "proposal-2026-07-23",
        "status": "selected",
        "date": "2026-07-23",
        "candidate_release_id": "release-22222222222222222222",
        "analysis_id": "change-analysis-11111111111111111111",
        "analysis_fingerprint": "b" * 64,
        "proposal_fingerprints": [PROPOSAL_KEY],
        "reason": "One evidence-backed change exceeds the configured materiality threshold.",
        "title": "A Stable Difference Appears",
        "lead": "An exact comparison changes how the recursive error should be interpreted.",
        "topics": ["recursive-error"],
        "featured_artifact": "artifact-stage-profile-2026-07-23",
        "artifact_manifests": [
            "/artifacts/2026-07-23/stage-profile/manifest.json",
            "/artifacts/2026-07-23/topic-illustration/manifest.json",
        ],
        "source_ids": ["src-test"],
        "knowledge_ids": ["claim-new"],
        "signals": [
            {
                "heading": "The recursive profile separates from the single pass",
                "proposal_fingerprint": PROPOSAL_KEY,
                "what_changed": what_changed,
                "why_it_matters": why_it_matters,
                "evidence": ["[exact table, rows 2–10](/sources#src-test)"],
                "confidence": "High for the registered finite-dimensional operator.",
                "assumptions": [
                    "The registered periodic boundary and window sequence are unchanged."
                ],
                "limitations": [
                    "The calculation does not establish nonlinear robust recursion."
                ],
            }
        ],
        "why_this_matters": synthesis,
        "unresolved_question": "Does the same separation persist when the robust target and contamination level vary?",
        "sources": [
            {
                "source_id": "src-test",
                "label": "Exact operator table",
                "locator": "rows 2–10",
            }
        ],
    }
    value.update(updates)
    return seal_proposal(value)


def test_selected_proposal_renders_deterministically_with_explicit_contract(
    repository_root: Path,
) -> None:
    proposal = _proposal()
    first = render_pulse_markdown(proposal)
    second = render_pulse_markdown(proposal)
    assert first == second
    assert "## Signal 01" in first
    assert "**What changed.**" in first
    assert "**Why it matters.**" in first
    assert "**Evidence.**" in first
    assert "**Confidence.**" in first
    assert "**Assumptions and limitations.**" in first
    assert first.count("artifact_manifests:") == 1
    assert first.count("featured_artifact:") == 1
    assert "topic-illustration/manifest.json" in first

    staged = repository_root / "data" / ".staging-test-do-not-write.md"
    # Rendering itself has no filesystem side effect.
    assert not staged.exists()


def test_build_writes_supplied_staging_path_without_overwriting(tmp_path: Path) -> None:
    proposal = _proposal()
    output = tmp_path / "staged" / "2026-07-23.md"
    result = build_pulse(proposal, output)
    assert result.path == output
    assert 350 <= result.word_count <= 650
    frontmatter, _ = parse_pulse(output)
    assert frontmatter["id"] == "pulse-2026-07-23"
    assert frontmatter["artifact_manifests"] == [
        "/artifacts/2026-07-23/stage-profile/manifest.json",
        "/artifacts/2026-07-23/topic-illustration/manifest.json",
    ]
    assert frontmatter["word_count"] == result.word_count

    original = output.read_bytes()
    with pytest.raises(ValidationError, match="refusing to overwrite"):
        build_pulse(proposal, output)
    assert output.read_bytes() == original


def test_builder_rejects_nonselected_mismatch_remote_evidence_and_short_report(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError, match="only a selected proposal"):
        render_pulse_markdown(_proposal(status="no_update", proposal_fingerprints=[]))

    mismatched = _proposal(proposal_fingerprints=["c" * 64])
    with pytest.raises(ValidationError, match="exactly match"):
        render_pulse_markdown(mismatched)

    remote = _proposal()
    remote["signals"][0]["evidence"] = ["[paper](https://example.test/paper)"]
    remote = seal_proposal(remote)
    with pytest.raises(ValidationError):
        render_pulse_markdown(remote)

    short = _proposal()
    short["signals"][0]["what_changed"] = "A registered result changed."
    short["signals"][0]["why_it_matters"] = "It changes interpretation."
    short["why_this_matters"] = "The distinction matters."
    short = seal_proposal(short)
    with pytest.raises(ValidationError, match="word count"):
        build_pulse(short, tmp_path / "2026-07-23.md")
    assert not (tmp_path / "2026-07-23.md").exists()


def _write_cli_release(
    root: Path, release_id: str, *, include_claim: bool, content: str, semantic: str
) -> Path:
    directory = root / release_id
    directory.mkdir()
    (directory / "release.json").write_text(
        json.dumps(
            {
                "release_id": release_id,
                "semantic_fingerprint": semantic * 64,
            }
        ),
        encoding="utf-8",
    )
    source = {
        "id": "src-test",
        "content_sha256": content * 64,
        "extract_semantic_sha256": semantic * 64,
    }
    (directory / "sources.jsonl").write_text(json.dumps(source) + "\n", encoding="utf-8")
    claim = {
        "id": "claim-new",
        "normalized_text": "The operator profile changed.",
        "confidence": {"score": 0.8},
        "evidence": [
            {
                "source_id": "src-test",
                "source_sha256": content * 64,
                "role": "direct",
                "locator": "README.md lines 1-3",
            }
        ],
    }
    (directory / "claims.jsonl").write_text(
        json.dumps(claim) + "\n" if include_claim else "", encoding="utf-8"
    )
    for filename in ("methods.jsonl", "experiments.jsonl", "relationships.jsonl"):
        (directory / filename).write_text("", encoding="utf-8")
    return directory


def test_cli_emits_json_and_builds_only_matching_selected_proposal(
    tmp_path: Path, repository_root: Path
) -> None:
    base = _write_cli_release(
        tmp_path, "release-11111111111111111111", include_claim=False, content="a", semantic="b"
    )
    candidate = _write_cli_release(
        tmp_path, "release-22222222222222222222", include_claim=True, content="c", semantic="d"
    )
    analysis = analyze_release_changes(base, candidate, policy=NoveltyPolicy())
    proposal = _proposal(
        analysis_id=analysis["id"],
        analysis_fingerprint=analysis["analysis_fingerprint"],
        candidate_release_id=analysis["candidate_release_id"],
        proposal_fingerprints=analysis["selected_candidate_fingerprints"],
    )
    proposal["signals"][0]["proposal_fingerprint"] = analysis[
        "selected_candidate_fingerprints"
    ][0]
    proposal = seal_proposal(proposal)
    proposal_path = tmp_path / "proposal.json"
    proposal_path.write_text(json.dumps(proposal), encoding="utf-8")
    output = tmp_path / "stage" / "2026-07-23.md"
    command = [
        sys.executable,
        str(repository_root / "scripts" / "build_daily_pulse.py"),
        "--current-release",
        str(base),
        "--candidate-release",
        str(candidate),
        "--proposal",
        str(proposal_path),
        "--output",
        str(output),
    ]
    completed = subprocess.run(
        command,
        cwd=repository_root,
        check=True,
        text=True,
        capture_output=True,
    )
    result = json.loads(completed.stdout)
    assert result["status"] == "selected"
    assert result["pulse"]["path"] == str(output)
    assert output.exists()
