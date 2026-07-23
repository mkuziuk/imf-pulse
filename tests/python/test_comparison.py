from __future__ import annotations

import json
import subprocess
from pathlib import Path

import jsonschema

from research_pipeline.external import compare_knowledge_profiles


PROJECT = Path(__file__).resolve().parents[2]


def profile(
    profile_id: str,
    *,
    concept: str = "recursive-error:stage-one-rate",
    target: str | None = "population-target",
    scopes: list[str] | None = None,
    value: str | None = "rate:root-n",
    definitions: dict[str, str] | None = None,
) -> dict:
    return {
        "id": profile_id,
        "concept_key": concept,
        "target_key": target,
        "scope_keys": ["linear-filter", "periodic-boundary"] if scopes is None else scopes,
        "value_key": value,
        "definition_bindings": definitions or {},
    }


def kinds(result: dict) -> list[str]:
    return [item["kind"] for item in result["findings"]]


def test_same_concept_target_and_exact_scope_with_different_value_is_contradiction() -> None:
    result = compare_knowledge_profiles(
        [profile("claim-old", value="rate:root-n")],
        [profile("claim-new", value="rate:n")],
    )
    assert result["status"] == "findings_require_review"
    assert kinds(result) == ["contradicts"]
    assert result["findings"][0]["review_required"] is True


def test_different_targets_are_not_contradictions() -> None:
    result = compare_knowledge_profiles(
        [profile("claim-old", target="population-target")],
        [profile("claim-new", target="clean-same-contrast", value="different")],
    )
    assert kinds(result) == ["uses-different-target"]
    assert "contradicts" not in kinds(result)


def test_same_term_with_different_definition_is_definition_drift() -> None:
    result = compare_knowledge_profiles(
        [
            profile(
                "method-old",
                concept="method:kernel",
                definitions={"kernel:epanechnikov": "0.75*(1-u^2)"},
            )
        ],
        [
            profile(
                "method-new",
                concept="another:method",
                definitions={"kernel:epanechnikov": "0.75*(1-|u|)^2"},
            )
        ],
    )
    assert kinds(result) == ["uses-different-definition"]
    assert result["findings"][0]["comparison"]["term_key"] == "kernel:epanechnikov"


def test_missing_or_mismatched_scope_produces_review_gap_not_contradiction() -> None:
    missing = compare_knowledge_profiles(
        [profile("claim-old", scopes=[])],
        [profile("claim-new", value="incompatible")],
    )
    assert kinds(missing) == ["review-gap"]
    assert missing["findings"][0]["comparison"]["reason"] == "missing_scope"

    mismatch = compare_knowledge_profiles(
        [profile("claim-old", scopes=["linear-filter"])],
        [profile("claim-new", scopes=["nonlinear-filter"], value="incompatible")],
    )
    assert kinds(mismatch) == ["review-gap"]
    assert mismatch["findings"][0]["comparison"]["reason"] == "scope_mismatch"


def test_missing_target_or_value_produces_review_gap() -> None:
    target = compare_knowledge_profiles(
        [profile("claim-old", target=None)], [profile("claim-new")]
    )
    assert target["findings"][0]["comparison"]["reason"] == "missing_target"
    value = compare_knowledge_profiles(
        [profile("claim-old", value=None)], [profile("claim-new")]
    )
    assert value["findings"][0]["comparison"]["reason"] == "missing_value"


def test_equal_profiles_have_no_findings_and_order_is_deterministic() -> None:
    baseline = [profile("old-b"), profile("old-a", concept="other")]
    candidates = [profile("new-b"), profile("new-a", concept="other")]
    first = compare_knowledge_profiles(baseline, candidates)
    second = compare_knowledge_profiles(list(reversed(baseline)), list(reversed(candidates)))
    assert first == second == {"status": "no_findings", "finding_count": 0, "findings": []}


def test_findings_validate_against_schema() -> None:
    result = compare_knowledge_profiles(
        [profile("claim-old", value="a")], [profile("claim-new", value="b")]
    )
    schema = json.loads((PROJECT / "schemas/comparison-finding.schema.json").read_text())
    validator = jsonschema.Draft202012Validator(schema)
    for finding in result["findings"]:
        validator.validate(finding)


def test_compare_cli_emits_json_contract(tmp_path: Path) -> None:
    existing = tmp_path / "existing.json"
    candidates = tmp_path / "candidates.jsonl"
    existing.write_text(json.dumps([profile("claim-old", value="a")]), encoding="utf-8")
    candidates.write_text(json.dumps(profile("claim-new", value="b")) + "\n", encoding="utf-8")
    completed = subprocess.run(
        [
            str(PROJECT / ".venv/bin/python"),
            str(PROJECT / "scripts/compare_knowledge.py"),
            "--existing",
            str(existing),
            "--candidates",
            str(candidates),
        ],
        cwd=PROJECT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    result = json.loads(completed.stdout)
    assert result["status"] == "findings_require_review"
    assert result["finding_count"] == 1
