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
        elif command[:2] in {("git", "add"), ("git", "push")}:
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
    shutil.copy(
        repository_root / "schemas" / "daily-run-result.schema.json",
        project / "schemas" / "daily-run-result.schema.json",
    )
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
    outcome = project / "data" / "automatic" / "external-search-outcomes" / f"{RUN_DATE}.json"
    outcome.parent.mkdir(parents=True)
    outcome.write_text("{}\n", encoding="utf-8")
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
