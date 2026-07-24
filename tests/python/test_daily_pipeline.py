from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any, Callable

import pytest

from research_pipeline.config import load_pipeline_config, resolve_live_root
from research_pipeline.daily import (
    AnalysisOutcome,
    CandidateOutcome,
    DailyBlockedError,
    DailyContext,
    DailyDependencies,
    DailyRunResult,
    ExternalOutcome,
    PulseOutcome,
    SnapshotOutcome,
    _default_monitor_external,
    _install_immutable_bytes,
    _write_immutable_json,
    run_daily_pipeline,
)
from research_pipeline.errors import PublicationError, SnapshotError
from research_pipeline.release import PublishResult


RUN_DATE = "2026-07-23"
OLD_RELEASE = "release-11111111111111111111"
NEW_RELEASE = "release-22222222222222222222"


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "schemas").mkdir(parents=True)
    source_schema = Path(__file__).resolve().parents[2] / "schemas" / "daily-run-result.schema.json"
    (project / "schemas" / source_schema.name).write_bytes(source_schema.read_bytes())
    return project


def _context(project: Path) -> DailyContext:
    return DailyContext(
        project_root=project,
        mode="live",
        date=RUN_DATE,
        source_config=object(),
        pulse_config={},
        pulse_constraints={},
        external_config={},
        source_root=project.parent / "source",
    )


def _unexpected(name: str) -> Callable[..., Any]:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"unexpected dependency call: {name}")

    return fail


def _dependencies(
    project: Path,
    *,
    checkpoint: dict[str, Any] | None = None,
    external: ExternalOutcome | None = None,
    candidate: CandidateOutcome | None = None,
    analysis: AnalysisOutcome | None = None,
    proposal: dict[str, Any] | None = None,
    pulse: PulseOutcome | None = None,
    publish: PublishResult | BaseException | None = None,
) -> tuple[DailyDependencies, list[str]]:
    calls: list[str] = []
    context = _context(project)
    snapshot = SnapshotOutcome(
        "snapshot-aaaaaaaaaaaaaaaaaaaa", project / "imports" / "snapshot", False
    )
    candidate = candidate or CandidateOutcome(
        OLD_RELEASE, project / "data" / "releases" / OLD_RELEASE, False, "unchanged", False
    )
    analysis = analysis or AnalysisOutcome(
        "no_update", "release content is unchanged", (OLD_RELEASE,)
    )
    external = external or ExternalOutcome()
    publish = publish or PublishResult(OLD_RELEASE, "run-test-no-update", "unchanged", False)

    def load_context(root: Path, mode: str, run_date: str) -> DailyContext:
        calls.append("load_context")
        assert root == project
        assert mode == "live"
        assert run_date == RUN_DATE
        return context

    def read_checkpoint(value: DailyContext) -> dict[str, Any] | None:
        calls.append("read_checkpoint")
        return checkpoint

    def sync_local(value: DailyContext) -> SnapshotOutcome:
        calls.append("sync_local")
        return snapshot

    def monitor_external(value: DailyContext) -> ExternalOutcome:
        calls.append("monitor_external")
        return external

    def build_candidate(value: DailyContext, selected: SnapshotOutcome) -> CandidateOutcome:
        calls.append("build_candidate")
        assert selected is snapshot
        return candidate

    def analyze_candidate(
        value: DailyContext,
        current: dict[str, Any] | None,
        selected: CandidateOutcome,
    ) -> AnalysisOutcome:
        calls.append("analyze_candidate")
        assert current is checkpoint
        assert selected is candidate
        return analysis

    def load_proposal(
        value: DailyContext,
        selected: CandidateOutcome,
        selected_analysis: AnalysisOutcome,
    ) -> dict[str, Any] | None:
        calls.append("load_proposal")
        return proposal

    def build_pulse(value: DailyContext, selected: dict[str, Any]) -> PulseOutcome:
        calls.append("build_pulse")
        assert selected is proposal
        if pulse is None:
            raise AssertionError("test did not configure a pulse")
        return pulse

    def publish_candidate(
        value: DailyContext,
        selected: CandidateOutcome,
        selected_pulse: PulseOutcome | None,
    ) -> PublishResult:
        calls.append("publish_candidate")
        if isinstance(publish, BaseException):
            raise publish
        assert isinstance(publish, PublishResult)
        if pulse is None:
            assert selected_pulse is None
        else:
            assert selected_pulse is pulse
        return publish

    return (
        DailyDependencies(
            load_context=load_context,
            read_checkpoint=read_checkpoint,
            sync_local=sync_local,
            monitor_external=monitor_external,
            build_candidate=build_candidate,
            analyze_candidate=analyze_candidate,
            load_proposal=load_proposal,
            build_pulse=build_pulse,
            publish_candidate=publish_candidate,
        ),
        calls,
    )


def test_no_update_refreshes_checkpoint_without_creating_a_pulse(tmp_path: Path) -> None:
    project = _project(tmp_path)
    dependencies, calls = _dependencies(
        project,
        checkpoint={"release_id": OLD_RELEASE},
    )

    result = run_daily_pipeline(
        project, mode="live", run_date=RUN_DATE, dependencies=dependencies
    )

    assert result.status == "no_update"
    assert result.release_id == OLD_RELEASE
    assert result.pulse_path is None
    assert result.artifact_urls == ()
    assert result.release_advanced is False
    assert result.checkpoint_refreshed is True
    assert result.pending_review_count == 0
    assert calls == [
        "load_context",
        "read_checkpoint",
        "monitor_external",
        "sync_local",
        "build_candidate",
        "analyze_candidate",
        "publish_candidate",
    ]


def test_pending_external_candidates_stop_before_extraction_or_publication(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    dependencies, calls = _dependencies(
        project,
        checkpoint={"release_id": OLD_RELEASE},
        external=ExternalOutcome(
            pending_candidate_ids=("candidate-arxiv-aaaaaaaaaaaaaaaaaaaa",),
            batch_id="external-batch-bbbbbbbbbbbbbbbbbbbb",
            review_path="data/external/batches/external-batch-bbbbbbbbbbbbbbbbbbbb.json",
        ),
    )

    result = run_daily_pipeline(
        project, mode="live", run_date=RUN_DATE, dependencies=dependencies
    )

    assert result.status == "review_required"
    assert result.pending_review_count == 1
    assert result.release_advanced is False
    assert result.checkpoint_refreshed is False
    assert "sync_local" not in calls
    assert "build_candidate" not in calls
    assert "publish_candidate" not in calls


def test_material_change_without_reviewed_proposal_does_not_fabricate_report(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    candidate = CandidateOutcome(
        NEW_RELEASE,
        project / "data" / "releases" / NEW_RELEASE,
        True,
        "candidate",
        True,
    )
    analysis = AnalysisOutcome(
        "selected",
        "material evidence-backed changes",
        ("claim-new", "src-new"),
        analysis={"id": "change-analysis-aaaaaaaaaaaaaaaaaaaa"},
        review_path=f"data/review/pulse-proposals/{RUN_DATE}.json",
    )
    dependencies, calls = _dependencies(
        project,
        checkpoint={"release_id": OLD_RELEASE},
        candidate=candidate,
        analysis=analysis,
    )

    result = run_daily_pipeline(
        project, mode="live", run_date=RUN_DATE, dependencies=dependencies
    )

    assert result.status == "review_required"
    assert result.release_id == NEW_RELEASE
    assert result.pending_review_path == f"data/review/pulse-proposals/{RUN_DATE}.json"
    assert result.pulse_path is None
    assert "build_pulse" not in calls
    assert "publish_candidate" not in calls


def test_reviewed_material_change_publishes_exactly_one_pulse_and_artifact(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    candidate = CandidateOutcome(
        NEW_RELEASE,
        project / "data" / "releases" / NEW_RELEASE,
        True,
        "candidate",
        True,
    )
    analysis = AnalysisOutcome(
        "selected",
        "material evidence-backed changes",
        ("change-analysis-aaaaaaaaaaaaaaaaaaaa", "claim-new"),
        analysis={"id": "change-analysis-aaaaaaaaaaaaaaaaaaaa"},
        review_path=f"data/review/pulse-proposals/{RUN_DATE}.json",
    )
    proposal = {"status": "selected"}
    pulse = PulseOutcome(
        f"content/pulses/{RUN_DATE}.md",
        (f"/artifacts/{RUN_DATE}/new-signal/manifest.json",),
        ("claim-new", "src-new"),
    )
    dependencies, calls = _dependencies(
        project,
        checkpoint={"release_id": OLD_RELEASE},
        candidate=candidate,
        analysis=analysis,
        proposal=proposal,
        pulse=pulse,
        publish=PublishResult(NEW_RELEASE, "run-test-published", "published", True),
    )

    result = run_daily_pipeline(
        project, mode="live", run_date=RUN_DATE, dependencies=dependencies
    )

    assert result.status == "published"
    assert result.run_id == "run-test-published"
    assert result.release_id == NEW_RELEASE
    assert result.pulse_path == f"content/pulses/{RUN_DATE}.md"
    assert result.artifact_urls == (
        f"/artifacts/{RUN_DATE}/new-signal/manifest.json",
    )
    assert result.release_advanced is True
    assert result.checkpoint_refreshed is True
    assert calls.count("build_pulse") == 1
    assert calls.count("publish_candidate") == 1


def test_failed_publication_preserves_the_existing_checkpoint_bytes(tmp_path: Path) -> None:
    project = _project(tmp_path)
    pointer = project / "data" / "current.json"
    pointer.parent.mkdir()
    original = b'{"release_id":"release-11111111111111111111","sentinel":true}\n'
    pointer.write_bytes(original)
    candidate = CandidateOutcome(
        NEW_RELEASE,
        project / "data" / "releases" / NEW_RELEASE,
        True,
        "candidate",
        False,
    )
    dependencies, _ = _dependencies(
        project,
        checkpoint={"release_id": OLD_RELEASE},
        candidate=candidate,
        publish=PublicationError("injected release gate failure"),
    )

    result = run_daily_pipeline(
        project, mode="live", run_date=RUN_DATE, dependencies=dependencies
    )

    assert result.status == "failed"
    assert result.release_advanced is False
    assert result.checkpoint_refreshed is False
    assert pointer.read_bytes() == original


def test_disabled_or_invalid_preflight_returns_blocked_without_state_work(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)

    def blocked(root: Path, mode: str, run_date: str) -> DailyContext:
        raise DailyBlockedError("scheduling is not explicitly enabled")

    dependencies = DailyDependencies(
        load_context=blocked,
        read_checkpoint=_unexpected("read_checkpoint"),
        sync_local=_unexpected("sync_local"),
        monitor_external=_unexpected("monitor_external"),
        build_candidate=_unexpected("build_candidate"),
        analyze_candidate=_unexpected("analyze_candidate"),
        load_proposal=_unexpected("load_proposal"),
        build_pulse=_unexpected("build_pulse"),
        publish_candidate=_unexpected("publish_candidate"),
    )

    result = run_daily_pipeline(
        project, mode="live", run_date=RUN_DATE, dependencies=dependencies
    )

    assert result.status == "blocked"
    assert result.reason == "scheduling is not explicitly enabled"
    assert result.release_id is None
    assert not (project / "data").exists()


def test_checked_in_relative_source_is_anchored_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = Path(__file__).resolve().parents[2]
    monkeypatch.delenv("IMF_SOURCE_ROOT", raising=False)

    config = load_pipeline_config(repository / "config" / "sources.yaml")
    source_root = resolve_live_root(config, "imf")
    normalized_source_root = source_root.resolve(strict=False)

    assert normalized_source_root == repository.parent / "imf"
    assert normalized_source_root != repository / "imf"


def test_review_output_refuses_symlinked_parent(tmp_path: Path) -> None:
    project = _project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / "data").mkdir()
    (project / "data" / "review").symlink_to(outside, target_is_directory=True)

    with pytest.raises(SnapshotError):
        _write_immutable_json(
            project,
            "data/review/change-analyses/change-analysis-aaaaaaaaaaaaaaaaaaaa.json",
            {"id": "change-analysis-aaaaaaaaaaaaaaaaaaaa"},
        )

    assert list(outside.iterdir()) == []


def test_pulse_output_refuses_symlinked_content_parent(tmp_path: Path) -> None:
    project = _project(tmp_path)
    outside = tmp_path / "outside-content"
    outside.mkdir()
    (project / "content").symlink_to(outside, target_is_directory=True)

    with pytest.raises(SnapshotError):
        _install_immutable_bytes(
            project, f"content/pulses/{RUN_DATE}.md", b"must not escape\n"
        )

    assert list(outside.iterdir()) == []


def test_external_stage_uses_exact_hash_decisions_and_keeps_pending_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    batch_relative = "data/external/batches/external-batch-aaaaaaaaaaaaaaaaaaaa.json"
    batch_path = project / batch_relative
    batch_path.parent.mkdir(parents=True)
    pending_id = "candidate-arxiv-11111111111111111111"
    approved_id = "candidate-arxiv-22222222222222222222"
    pending_sha = "1" * 64
    approved_sha = "2" * 64
    batch_path.write_text(
        json.dumps(
            {
                "candidates": [
                    {"id": pending_id, "candidate_sha256": pending_sha},
                    {"id": approved_id, "candidate_sha256": approved_sha},
                ]
            }
        ),
        encoding="utf-8",
    )
    context = DailyContext(
        **{
            **_context(project).__dict__,
            "external_config": {
                "policy": {"decision_ledger": "data/review/external-decisions.jsonl"}
            },
        }
    )
    import research_pipeline.external as external_module

    monkeypatch.setattr(
        external_module,
        "run_external_search",
        lambda *args, **kwargs: {
            "batch_id": "external-batch-aaaaaaaaaaaaaaaaaaaa",
            "batch_path": batch_relative,
        },
    )
    monkeypatch.setattr(external_module, "validate_batch_integrity", lambda batch: None)

    def decision(
        root: Path, ledger: str, candidate_id: str, candidate_sha256: str
    ) -> dict[str, str] | None:
        assert (candidate_id, candidate_sha256) in {
            (pending_id, pending_sha),
            (approved_id, approved_sha),
        }
        return {"decision": "approved"} if candidate_id == approved_id else None

    monkeypatch.setattr(external_module, "lookup_review_decision", decision)

    outcome = _default_monitor_external(context)

    assert outcome.pending_candidate_ids == (pending_id,)
    assert outcome.approved_candidate_ids == (approved_id,)
    assert outcome.review_path == batch_relative


@pytest.mark.parametrize(
    ("status", "expected"),
    [("review_required", 0), ("no_update", 0), ("published", 0), ("blocked", 2), ("failed", 2)],
)
def test_cli_exit_code_is_nonzero_only_for_blocked_or_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
    expected: int,
) -> None:
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "run_daily_pipeline.py"
    specification = importlib.util.spec_from_file_location("daily_cli_for_test", script_path)
    assert specification and specification.loader
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    result = DailyRunResult(
        status=status,
        date=RUN_DATE,
        run_id="daily-test",
        release_id=None,
        pulse_path=None,
        artifact_urls=(),
        release_advanced=False,
        checkpoint_refreshed=False,
        reason="test result",
        evidence_ids=(),
        pending_review_count=1 if status == "review_required" else 0,
        pending_review_path="data/review/test.json" if status == "review_required" else None,
    )
    monkeypatch.setattr(module, "run_daily_pipeline", lambda *args, **kwargs: result)

    exit_code = module.main(
        ["--project-root", str(tmp_path), "--mode", "live", "--date", RUN_DATE]
    )

    assert exit_code == expected
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == status
