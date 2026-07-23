from __future__ import annotations

import json
from pathlib import Path

import yaml


EXPECTED_QUESTIONS = {
    "What distinguishes IMF and IRMF in this project?",
    "Which claims apply only to linear filtering?",
    "What does Lemma 2.5 establish, and what does it not establish?",
    "Why is the first recursive IMF error larger?",
    "How do single-pass and recursive errors differ?",
    "Which notebooks use a clean same-contrast target rather than the population target?",
    "How is the robustness parameter selected?",
    "Which results survive without contamination?",
    "Which theoretical statements remain incomplete?",
    "Which experiments could resolve the most important open questions?",
}


def _jsonl_ids(path: Path) -> set[str]:
    return {
        json.loads(line)["id"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def test_gold_benchmark_covers_requested_questions_with_registered_evidence(
    repository_root: Path,
) -> None:
    benchmark = yaml.safe_load(
        (repository_root / "evaluation" / "benchmark.yaml").read_text(encoding="utf-8")
    )
    questions = benchmark["questions"]
    assert benchmark["status"] == "curated"
    assert len(questions) == 10
    assert {question["question"] for question in questions} == EXPECTED_QUESTIONS

    source_config = yaml.safe_load(
        (repository_root / "config" / "sources.yaml").read_text(encoding="utf-8")
    )
    source_ids = {source["id"] for source in source_config["sources"]}
    knowledge_ids: set[str] = set()
    for name in ("claims", "methods", "experiments", "relationships"):
        knowledge_ids |= _jsonl_ids(
            repository_root / "knowledge" / "curated" / f"{name}.jsonl"
        )

    for question in questions:
        assert len(question["answer"].split()) >= 45
        assert question["knowledge_ids"]
        assert set(question["knowledge_ids"]) <= knowledge_ids
        assert question["evidence"]
        for evidence in question["evidence"]:
            assert evidence["source_id"] in source_ids
            assert isinstance(evidence["locator"], str) and evidence["locator"].strip()
        assert question["confidence"]["level"] in {"low", "medium", "high"}
        assert 0 <= question["confidence"]["score"] <= 1
