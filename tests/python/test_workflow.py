from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from research_pipeline.workflow import WorkflowStateError, WorkflowStore


RUN_DATE = "2026-07-23"


def _project(tmp_path: Path, repository_root: Path) -> Path:
    project = tmp_path / "project"
    (project / "schemas").mkdir(parents=True)
    for name in ("scheduled-stage-result.schema.json", "scheduled-workflow.schema.json"):
        shutil.copy(repository_root / "schemas" / name, project / "schemas" / name)
    return project


def test_stage_receipts_are_immutable_and_idempotent(
    tmp_path: Path, repository_root: Path
) -> None:
    project = _project(tmp_path, repository_root)
    workflow = WorkflowStore(project, RUN_DATE)
    first = workflow.complete_stage(
        "synchronize_base", {"remote": "origin"}, {"base_head": "a" * 40}
    )
    second = WorkflowStore(project, RUN_DATE).complete_stage(
        "synchronize_base", {"remote": "origin"}, {"base_head": "b" * 40}
    )

    assert second == first
    receipt = project / str(first["receipt"])
    assert receipt.is_file()
    assert len(list(receipt.parent.glob("*.json"))) == 1
    with pytest.raises(WorkflowStateError, match="different inputs"):
        workflow.complete_stage(
            "synchronize_base", {"remote": "upstream"}, {"base_head": "a" * 40}
        )


def test_corrupt_stage_receipt_fails_closed(
    tmp_path: Path, repository_root: Path
) -> None:
    project = _project(tmp_path, repository_root)
    workflow = WorkflowStore(project, RUN_DATE)
    reference = workflow.complete_stage("discover", {"as_of": RUN_DATE}, {"batch": "x"})
    receipt = project / str(reference["receipt"])
    value = json.loads(receipt.read_text(encoding="utf-8"))
    value["outputs"]["batch"] = "tampered"
    receipt.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(WorkflowStateError, match="receipt identity"):
        WorkflowStore(project, RUN_DATE)


def test_failure_records_resume_point_and_preserves_completed_stages(
    tmp_path: Path, repository_root: Path
) -> None:
    project = _project(tmp_path, repository_root)
    workflow = WorkflowStore(project, RUN_DATE)
    workflow.complete_stage("discover", {"as_of": RUN_DATE}, {"batch": "x"})
    workflow.record_failure(
        stage="materialize_source",
        classification="retryable",
        code="provider_rate_limited",
        reason="HTTP 429",
        retry_not_before="2026-07-23T05:00:00Z",
    )

    restored = WorkflowStore(project, RUN_DATE).as_dict()
    assert restored["stages"]["discover"]["status"] == "completed"
    assert restored["failure"]["resume_from"] == "materialize_source"
    assert restored["failure"]["classification"] == "retryable"


def test_orphan_receipt_is_adopted_after_manifest_update_crash(
    tmp_path: Path, repository_root: Path
) -> None:
    project = _project(tmp_path, repository_root)
    workflow = WorkflowStore(project, RUN_DATE)
    reference = workflow.complete_stage(
        "discover", {"as_of": RUN_DATE}, {"batch": "immutable"}
    )
    manifest_path = project / workflow.relative
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["stages"] = {}
    manifest["next_stage"] = "discover"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    recovered = WorkflowStore(project, RUN_DATE)

    assert recovered.stage("discover") == reference
    assert recovered.as_dict()["next_stage"] == "synchronize_base"
