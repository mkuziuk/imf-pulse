from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from research_pipeline.scheduled import (
    ScheduledPublishError,
    _allowed_change,
    _parse_daily_result,
    _remote_matches,
    _validate_publish_changes,
    run_scheduled_pipeline,
)
from research_pipeline.external_preflight import write_scheduled_search_outcome
from research_pipeline.workflow import WorkflowStore


RUN_DATE = "2026-07-23"
INDEXED_PULSE = f"content/pulses/{RUN_DATE}-2.md"


def _daily(status: str, **updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "1.0.0",
        "status": status,
        "date": RUN_DATE,
        "run_id": "daily-test",
        "release_id": "release-0123456789abcdefabcd",
        "pulse_path": None,
        "artifact_urls": [],
        "release_advanced": False,
        "checkpoint_refreshed": status == "no_update",
        "reason": "test result",
        "evidence_ids": [],
        "pending_review_count": 0,
        "pending_review_path": None,
    }
    value.update(updates)
    return value


def test_allowlist_is_date_scoped_and_exact() -> None:
    allowed = [
        f"content/pulses/{RUN_DATE}.md",
        INDEXED_PULSE,
        f"public/artifacts/{RUN_DATE}/chart/chart.svg",
        "knowledge/curated/claims.jsonl",
        "knowledge/curated/sources.jsonl",
        "public-release/current.json",
        "public-release/manifest.json",
        "public-release/knowledge/sources.jsonl",
        f"public-release/pulses/{RUN_DATE}.md",
        f"public-release/pulses/{RUN_DATE}-2.md",
        f"public-release/artifacts/{RUN_DATE}/chart/chart.csv",
    ]
    assert all(_allowed_change(path, RUN_DATE) for path in allowed)
    assert not _allowed_change("config/pulse.yaml", RUN_DATE)
    assert not _allowed_change("content/pulses/2026-07-22.md", RUN_DATE)
    assert not _allowed_change(f"public/artifacts/{RUN_DATE}/../secret", RUN_DATE)


def test_publish_change_validation_rejects_unsafe_or_incomplete_sets() -> None:
    valid = [
        ("?", INDEXED_PULSE),
        ("M", "public-release/manifest.json"),
    ]
    assert _validate_publish_changes(valid, RUN_DATE, INDEXED_PULSE)
    with pytest.raises(ScheduledPublishError, match="status D"):
        _validate_publish_changes([("D", valid[0][1]), valid[1]], RUN_DATE, INDEXED_PULSE)
    with pytest.raises(ScheduledPublishError, match="refuses changed path"):
        _validate_publish_changes([*valid, ("M", "README.md")], RUN_DATE, INDEXED_PULSE)
    with pytest.raises(ScheduledPublishError, match="manifest"):
        _validate_publish_changes([valid[0]], RUN_DATE, INDEXED_PULSE)


def test_remote_match_is_exact() -> None:
    repository = "mkuziuk/imf-pulse"
    assert _remote_matches("https://github.com/mkuziuk/imf-pulse.git", repository)
    assert _remote_matches("git@github.com:mkuziuk/imf-pulse.git", repository)
    assert not _remote_matches("https://github.com/other/imf-pulse.git", repository)
    assert not _remote_matches("https://github.com/mkuziuk/imf-pulse", repository)


def test_daily_result_requires_schema_and_exit_consistency(repository_root: Path) -> None:
    valid = _daily("no_update")
    completed = subprocess.CompletedProcess([], 0, json.dumps(valid), "")
    assert _parse_daily_result(repository_root, completed) == valid
    with pytest.raises(ScheduledPublishError, match="exit status"):
        _parse_daily_result(
            repository_root, subprocess.CompletedProcess([], 2, json.dumps(valid), "")
        )
    invalid = dict(valid, unexpected=True)
    with pytest.raises(Exception):
        _parse_daily_result(
            repository_root, subprocess.CompletedProcess([], 0, json.dumps(invalid), "")
        )


class FakeRunner:
    def __init__(self, project: Path, daily: dict[str, object]) -> None:
        self.project = project
        self.daily = daily
        self.calls: list[tuple[str, ...]] = []
        self.head = "a" * 40
        self.base = self.head

    def __call__(
        self, command: tuple[str, ...], cwd: Path, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(str(item) for item in command)
        self.calls.append(command)
        output = ""
        if command[:3] == ("git", "rev-parse", "--show-toplevel"):
            output = f"{self.project}\n"
        elif command[:4] == ("git", "symbolic-ref", "--quiet", "--short"):
            output = "main\n"
        elif command[:3] == ("git", "remote", "get-url"):
            output = "https://github.com/mkuziuk/imf-pulse.git\n"
        elif command[:2] == ("git", "status"):
            output = ""
        elif command[:2] == ("git", "fetch") or command[:3] == ("gh", "auth", "status"):
            output = ""
        elif command == ("git", "rev-parse", "HEAD"):
            output = f"{self.head}\n"
        elif command[:2] == ("git", "rev-parse") and command[-1] == "refs/remotes/origin/main":
            output = f"{self.base}\n"
        elif command[0].endswith("/.venv/bin/python") and command[1] == (
            "scripts/run_daily_pipeline.py"
        ):
            output = json.dumps(self.daily)
        elif command[0].endswith("/.venv/bin/python") and command[1] == (
            "scripts/export_public_release.py"
        ):
            pulse_path = str(self.daily.get("pulse_path") or f"content/pulses/{RUN_DATE}.md")
            (self.project / "content" / "pulses").mkdir(parents=True, exist_ok=True)
            (self.project / pulse_path).write_text("pulse\n")
            (self.project / "public-release").mkdir(exist_ok=True)
            (self.project / "public-release" / "manifest.json").write_text("{}\n")
        elif command[0].endswith("/.venv/bin/python") and command[1] == (
            "scripts/audit_public_release.py"
        ):
            output = ""
        elif command[:4] == ("git", "diff", "--name-status", "--no-renames"):
            pulse_path = str(self.daily.get("pulse_path") or f"content/pulses/{RUN_DATE}.md")
            output = (
                f"A\t{pulse_path}\n"
                "M\tpublic-release/manifest.json\n"
            )
        elif command[:3] == ("git", "ls-files", "--others"):
            output = ""
        elif command[:3] == ("git", "diff", "--quiet"):
            return subprocess.CompletedProcess(command, 0, "", "")
        elif command[:4] == ("git", "diff", "--cached", "--name-status"):
            pulse_path = str(self.daily.get("pulse_path") or f"content/pulses/{RUN_DATE}.md")
            output = (
                f"A\t{pulse_path}\n"
                "M\tpublic-release/manifest.json\n"
            )
        elif command[:3] == ("git", "commit", "-m"):
            self.head = "b" * 40
        elif command[:2] == ("git", "push"):
            self.base = self.head
            output = ""
        elif command[:2] == ("git", "add"):
            output = ""
        elif command[:4] == ("git", "diff", "--cached", "--check"):
            output = ""
        elif command[:3] == ("gh", "run", "list"):
            output = json.dumps(
                [
                    {
                        "databaseId": 123,
                        "url": "https://github.com/mkuziuk/imf-pulse/actions/runs/123",
                    }
                ]
            )
        elif command[:3] == ("gh", "run", "watch"):
            output = ""
        elif command[:3] == ("gh", "run", "view"):
            output = json.dumps(
                {
                    "status": "completed",
                    "conclusion": "success",
                    "headSha": self.head,
                    "url": "https://github.com/mkuziuk/imf-pulse/actions/runs/123",
                }
            )
        else:
            raise AssertionError(f"unexpected command: {command}")
        return subprocess.CompletedProcess(command, 0, output, "")


def _scheduled_project(tmp_path: Path, repository_root: Path) -> Path:
    project = tmp_path / "project"
    (project / "config").mkdir(parents=True)
    (project / "schemas").mkdir()
    shutil.copy(repository_root / "config" / "pulse.yaml", project / "config" / "pulse.yaml")
    for name in (
        "daily-run-result.schema.json",
        "external-search-outcome.schema.json",
        "scheduled-stage-result.schema.json",
        "scheduled-workflow.schema.json",
    ):
        shutil.copy(repository_root / "schemas" / name, project / "schemas" / name)
    return project


def test_no_update_never_reaches_git_publication(tmp_path: Path, repository_root: Path) -> None:
    project = _scheduled_project(tmp_path, repository_root)
    runner = FakeRunner(project, _daily("no_update"))
    result = run_scheduled_pipeline(project, run_date=RUN_DATE, runner=runner)
    assert result.status == "no_update"
    assert result.deployment_status == "not_requested"
    assert not any(
        command[:2] in {("git", "add"), ("git", "commit"), ("git", "push")}
        for command in runner.calls
    )
    assert not any(command[:3] == ("gh", "run", "list") for command in runner.calls)


def test_existing_preflight_outcome_is_passed_to_daily_without_a_second_search(
    tmp_path: Path, repository_root: Path
) -> None:
    project = _scheduled_project(tmp_path, repository_root)
    write_scheduled_search_outcome(
        project,
        run_date=RUN_DATE,
        as_of=f"{RUN_DATE}T06:00:00+03:00",
        status="deferred",
        reason="metadata timeout deferred",
    )
    runner = FakeRunner(project, _daily("no_update"))

    result = run_scheduled_pipeline(project, run_date=RUN_DATE, runner=runner)

    assert result.status == "no_update"
    daily_command = next(
        command
        for command in runner.calls
        if len(command) > 1 and command[1] == "scripts/run_daily_pipeline.py"
    )
    assert daily_command[-2:] == (
        "--external-search-outcome",
        f"data/automatic/external-search-outcomes/{RUN_DATE}.json",
    )


def test_published_result_uses_guarded_commit_and_waits_for_pages(
    tmp_path: Path, repository_root: Path
) -> None:
    project = _scheduled_project(tmp_path, repository_root)
    daily = _daily(
        "published",
        pulse_path=INDEXED_PULSE,
        artifact_urls=[f"/artifacts/{RUN_DATE}/chart/manifest.json"],
        release_advanced=True,
        checkpoint_refreshed=True,
    )
    runner = FakeRunner(project, daily)
    result = run_scheduled_pipeline(project, run_date=RUN_DATE, runner=runner)
    assert result.status == "published"
    assert result.deployment_status == "deployed"
    assert result.commit_sha == "b" * 40
    assert result.site_url == "https://mkuziuk.github.io/imf-pulse/"
    assert sum(command[:2] == ("git", "commit") for command in runner.calls) == 1
    assert sum(command[:2] == ("git", "push") for command in runner.calls) == 1
    assert sum(command[:3] == ("gh", "run", "watch") for command in runner.calls) == 1

    second = run_scheduled_pipeline(project, run_date=RUN_DATE, runner=runner)
    assert second.status == "published"
    assert sum(command[:2] == ("git", "commit") for command in runner.calls) == 1
    assert sum(command[:2] == ("git", "push") for command in runner.calls) == 1


class StaleRunner(FakeRunner):
    def __init__(self, project: Path, daily: dict[str, object]) -> None:
        super().__init__(project, daily)
        self.base = "c" * 40

    def __call__(
        self, command: tuple[str, ...], cwd: Path, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(str(item) for item in command)
        if command[:3] == ("git", "merge-base", "--is-ancestor"):
            self.calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ("git", "merge", "--ff-only"):
            self.calls.append(command)
            self.head = self.base
            return subprocess.CompletedProcess(command, 0, "", "")
        return super().__call__(command, cwd, timeout)


def test_clean_stale_main_fast_forwards_before_discovery(
    tmp_path: Path, repository_root: Path
) -> None:
    project = _scheduled_project(tmp_path, repository_root)
    runner = StaleRunner(project, _daily("no_update"))

    result = run_scheduled_pipeline(project, run_date=RUN_DATE, runner=runner)

    assert result.status == "no_update"
    assert runner.head == "c" * 40
    assert any(command[:3] == ("git", "merge", "--ff-only") for command in runner.calls)


class ConcurrentRunner(FakeRunner):
    def __init__(self, project: Path, daily: dict[str, object]) -> None:
        super().__init__(project, daily)
        self.fetch_count = 0

    def __call__(
        self, command: tuple[str, ...], cwd: Path, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(str(item) for item in command)
        if command[:2] == ("git", "fetch"):
            self.calls.append(command)
            self.fetch_count += 1
            if self.fetch_count == 2:
                self.base = "c" * 40
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ("git", "merge-base", "--is-ancestor"):
            self.calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:4] == ("git", "diff", "--name-status", "--no-renames") and ".." in command[4]:
            self.calls.append(command)
            if command[4] == f"{'a' * 40}..{'c' * 40}":
                output = "M\tREADME.md\n"
            else:
                output = f"A\t{INDEXED_PULSE}\nM\tpublic-release/manifest.json\n"
            return subprocess.CompletedProcess(command, 0, output, "")
        if command[:2] == ("git", "rebase"):
            self.calls.append(command)
            self.head = "d" * 40
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[0].endswith("/.venv/bin/python") and command[1:] == ("-m", "pytest"):
            self.calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:2] == ("npm", "test") or command[:3] == ("npm", "run", "build"):
            self.calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")
        return super().__call__(command, cwd, timeout)


def test_concurrent_nonpublication_remote_commit_is_safely_rebased(
    tmp_path: Path, repository_root: Path
) -> None:
    project = _scheduled_project(tmp_path, repository_root)
    daily = _daily(
        "published",
        pulse_path=INDEXED_PULSE,
        artifact_urls=[f"/artifacts/{RUN_DATE}/chart/manifest.json"],
        release_advanced=True,
        checkpoint_refreshed=True,
    )
    runner = ConcurrentRunner(project, daily)

    result = run_scheduled_pipeline(project, run_date=RUN_DATE, runner=runner)

    assert result.status == "published"
    assert result.commit_sha == "d" * 40
    assert sum(command[:2] == ("git", "rebase") for command in runner.calls) == 1
    assert sum(command == ("npm", "run", "build") for command in runner.calls) == 1


class ConflictingConcurrentRunner(ConcurrentRunner):
    def __call__(
        self, command: tuple[str, ...], cwd: Path, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(str(item) for item in command)
        if (
            command[:4] == ("git", "diff", "--name-status", "--no-renames")
            and len(command) > 4
            and command[4] == f"{'a' * 40}..{'c' * 40}"
        ):
            self.calls.append(command)
            return subprocess.CompletedProcess(
                command, 0, "M\tpublic-release/current.json\n", ""
            )
        return super().__call__(command, cwd, timeout)


def test_concurrent_publication_change_requires_manual_reconciliation(
    tmp_path: Path, repository_root: Path
) -> None:
    project = _scheduled_project(tmp_path, repository_root)
    daily = _daily(
        "published",
        pulse_path=INDEXED_PULSE,
        artifact_urls=[f"/artifacts/{RUN_DATE}/chart/manifest.json"],
        release_advanced=True,
        checkpoint_refreshed=True,
    )
    runner = ConflictingConcurrentRunner(project, daily)

    result = run_scheduled_pipeline(project, run_date=RUN_DATE, runner=runner)

    assert result.status == "failed"
    assert "manual reconciliation" in result.reason
    assert not any(command[:2] == ("git", "rebase") for command in runner.calls)


class DelayedDeploymentRunner(FakeRunner):
    def __init__(self, project: Path, daily: dict[str, object]) -> None:
        super().__init__(project, daily)
        self.delayed_once = False

    def __call__(
        self, command: tuple[str, ...], cwd: Path, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(str(item) for item in command)
        if command[:3] == ("gh", "run", "watch") and not self.delayed_once:
            self.calls.append(command)
            self.delayed_once = True
            raise subprocess.TimeoutExpired(command, timeout)
        return super().__call__(command, cwd, timeout)


def test_delayed_deployment_resumes_without_republishing_or_repushing(
    tmp_path: Path, repository_root: Path
) -> None:
    project = _scheduled_project(tmp_path, repository_root)
    daily = _daily(
        "published",
        pulse_path=INDEXED_PULSE,
        artifact_urls=[f"/artifacts/{RUN_DATE}/chart/manifest.json"],
        release_advanced=True,
        checkpoint_refreshed=True,
    )
    runner = DelayedDeploymentRunner(project, daily)

    first = run_scheduled_pipeline(project, run_date=RUN_DATE, runner=runner)
    second = run_scheduled_pipeline(project, run_date=RUN_DATE, runner=runner)

    assert first.status == "deferred"
    assert second.status == "published"
    assert sum(command[:2] == ("git", "commit") for command in runner.calls) == 1
    assert sum(command[:2] == ("git", "push") for command in runner.calls) == 1
    assert sum(
        len(command) > 1 and command[1] == "scripts/run_daily_pipeline.py"
        for command in runner.calls
    ) == 1
    assert sum(command[:3] == ("gh", "run", "watch") for command in runner.calls) == 2

    third = run_scheduled_pipeline(project, run_date=RUN_DATE, runner=runner)
    assert third.status == "published"
    assert sum(command[:2] == ("git", "commit") for command in runner.calls) == 1
    assert sum(command[:2] == ("git", "push") for command in runner.calls) == 1


class CommittedBeforeReceiptRunner(FakeRunner):
    def __init__(self, project: Path, daily: dict[str, object]) -> None:
        super().__init__(project, daily)
        self.head = "b" * 40

    def __call__(
        self, command: tuple[str, ...], cwd: Path, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(str(item) for item in command)
        if command[:3] == ("git", "merge-base", "--is-ancestor"):
            self.calls.append(command)
            return subprocess.CompletedProcess(command, 1, "", "")
        if command[:3] == ("git", "rev-list", "--count"):
            self.calls.append(command)
            return subprocess.CompletedProcess(command, 0, "1\n", "")
        return super().__call__(command, cwd, timeout)


def test_commit_created_before_receipt_is_adopted_without_duplicate_commit(
    tmp_path: Path, repository_root: Path
) -> None:
    project = _scheduled_project(tmp_path, repository_root)
    daily = _daily(
        "published",
        pulse_path=INDEXED_PULSE,
        artifact_urls=[f"/artifacts/{RUN_DATE}/chart/manifest.json"],
        release_advanced=True,
        checkpoint_refreshed=True,
    )
    workflow = WorkflowStore(project, RUN_DATE)
    sync = workflow.complete_stage(
        "synchronize_base",
        {"remote": "origin", "branch": "main", "repository": "mkuziuk/imf-pulse"},
        {"base_head": "a" * 40},
    )
    workflow.complete_stage(
        "publish_local",
        {"base_head": sync["outputs"]["base_head"], "external_search_outcome": None},
        {"daily": daily},
    )
    runner = CommittedBeforeReceiptRunner(project, daily)

    result = run_scheduled_pipeline(project, run_date=RUN_DATE, runner=runner)

    assert result.status == "published"
    assert not any(command[:2] == ("git", "commit") for command in runner.calls)
    assert sum(command[:2] == ("git", "push") for command in runner.calls) == 1
