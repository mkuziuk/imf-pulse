"""Guarded Git publication for one already transactional daily pipeline run."""

from __future__ import annotations

import json
import re
import stat
import subprocess
import time
from dataclasses import dataclass
from datetime import date as calendar_date
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from .config import load_yaml
from .pulse_identity import parse_pulse_path
from .validation import validate_records


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SAFE_ARTIFACT_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
PUBLIC_KNOWLEDGE = {
    "claims.jsonl",
    "experiments.jsonl",
    "methods.jsonl",
    "relationships.jsonl",
    "sources.jsonl",
}
CURATED_KNOWLEDGE = PUBLIC_KNOWLEDGE


class ScheduledPublishError(RuntimeError):
    """A scheduled Git or deployment safety condition was not satisfied."""


CommandRunner = Callable[
    [Sequence[str], Path, int], subprocess.CompletedProcess[str]
]


@dataclass(frozen=True)
class ScheduledRunResult:
    status: str
    date: str
    reason: str
    daily: Mapping[str, Any] | None
    deployment_status: str
    commit_sha: str | None = None
    workflow_run_url: str | None = None
    site_url: str | None = None
    schema_version: str = "1.0.0"

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "date": self.date,
            "reason": self.reason,
            "daily": dict(self.daily) if self.daily is not None else None,
            "deployment_status": self.deployment_status,
            "commit_sha": self.commit_sha,
            "workflow_run_url": self.workflow_run_url,
            "site_url": self.site_url,
        }


def _default_runner(
    command: Sequence[str], cwd: Path, timeout: int
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _run(
    runner: CommandRunner,
    project_root: Path,
    command: Sequence[str],
    *,
    timeout: int = 120,
    expected: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[str]:
    completed = runner(command, project_root, timeout)
    if completed.returncode not in expected:
        label = " ".join(command[:3])
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise ScheduledPublishError(f"command failed ({label}){suffix}")
    return completed


def _parse_date(value: str) -> str:
    if not DATE_RE.fullmatch(value):
        raise ScheduledPublishError("date must use YYYY-MM-DD")
    try:
        parsed = calendar_date.fromisoformat(value)
    except ValueError as exc:
        raise ScheduledPublishError("date must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ScheduledPublishError("date must use YYYY-MM-DD")
    return value


def _publication_policy(project_root: Path) -> Mapping[str, Any]:
    pulse = load_yaml(project_root / "config" / "pulse.yaml")
    scheduling = pulse.get("scheduling")
    if not isinstance(scheduling, Mapping):
        raise ScheduledPublishError("scheduled publication policy is missing")
    publication = scheduling.get("publication")
    if (
        scheduling.get("enabled") is not True
        or scheduling.get("commit_push_or_deploy") is not True
        or not isinstance(publication, Mapping)
        or publication.get("enabled") is not True
        or publication.get("deploy_only_on_status") != "published"
        or publication.get("require_clean_worktree") is not True
    ):
        raise ScheduledPublishError("scheduled publication is not explicitly enabled")
    expected = {
        "remote": "origin",
        "branch": "main",
        "repository": "mkuziuk/imf-pulse",
        "workflow": "pages.yml",
        "public_release_directory": "public-release",
        "site_url": "https://mkuziuk.github.io/imf-pulse/",
    }
    for key, value in expected.items():
        if publication.get(key) != value:
            raise ScheduledPublishError(f"scheduled publication has an unexpected {key}")
    return publication


def _single_json(text: str, label: str) -> Any:
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ScheduledPublishError(f"{label} did not emit exactly one JSON object")
    try:
        return json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise ScheduledPublishError(f"{label} emitted invalid JSON") from exc


def _parse_daily_result(
    project_root: Path, completed: subprocess.CompletedProcess[str]
) -> dict[str, Any]:
    value = _single_json(completed.stdout, "daily pipeline")
    if not isinstance(value, dict):
        raise ScheduledPublishError("daily pipeline result must be an object")
    validate_records(
        [value],
        project_root / "schemas" / "daily-run-result.schema.json",
        "scheduled daily result",
    )
    expected_codes = {"blocked": 2, "failed": 2}
    expected = expected_codes.get(str(value.get("status")), 0)
    if completed.returncode != expected:
        raise ScheduledPublishError("daily pipeline exit status does not match its result")
    return value


def _remote_matches(value: str, repository: str) -> bool:
    value = value.strip()
    return value in {
        f"https://github.com/{repository}.git",
        f"git@github.com:{repository}.git",
        f"ssh://git@github.com/{repository}.git",
    }


def _clean_worktree(runner: CommandRunner, project_root: Path) -> bool:
    completed = _run(
        runner,
        project_root,
        ("git", "status", "--porcelain=v1", "--untracked-files=all"),
    )
    return not completed.stdout.strip()


def _git_preflight(
    runner: CommandRunner, project_root: Path, publication: Mapping[str, Any]
) -> str:
    top = _run(runner, project_root, ("git", "rev-parse", "--show-toplevel")).stdout.strip()
    if Path(top).resolve(strict=True) != project_root:
        raise ScheduledPublishError("project root is not the Git worktree root")
    branch = _run(
        runner, project_root, ("git", "symbolic-ref", "--quiet", "--short", "HEAD")
    ).stdout.strip()
    if branch != publication["branch"]:
        raise ScheduledPublishError("scheduled publication requires the main branch")
    remote_url = _run(
        runner, project_root, ("git", "remote", "get-url", str(publication["remote"]))
    ).stdout.strip()
    if not _remote_matches(remote_url, str(publication["repository"])):
        raise ScheduledPublishError("origin does not target the approved public repository")
    if not _clean_worktree(runner, project_root):
        raise ScheduledPublishError("tracked worktree must be clean before the daily run")
    _run(runner, project_root, ("gh", "auth", "status", "--hostname", "github.com"))
    _run(
        runner,
        project_root,
        ("git", "fetch", "--quiet", str(publication["remote"]), str(publication["branch"])),
        timeout=180,
    )
    head = _run(runner, project_root, ("git", "rev-parse", "HEAD")).stdout.strip()
    upstream = _run(
        runner,
        project_root,
        ("git", "rev-parse", f"refs/remotes/{publication['remote']}/{publication['branch']}"),
    ).stdout.strip()
    if not SHA_RE.fullmatch(head) or head != upstream:
        raise ScheduledPublishError("local main must exactly match origin/main")
    return head


def _allowed_change(
    path: str, run_date: str, expected_pulse_path: str | None = None
) -> bool:
    try:
        pure = PurePosixPath(path)
    except ValueError:
        return False
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        return False
    content_identity = parse_pulse_path(path)
    if content_identity is not None:
        return content_identity.date == run_date and (
            expected_pulse_path is None or path == expected_pulse_path
        )
    artifact_prefix = f"public/artifacts/{run_date}/"
    if path.startswith(artifact_prefix):
        return bool(SAFE_ARTIFACT_RE.fullmatch(path.removeprefix(artifact_prefix)))
    if path in {"public-release/current.json", "public-release/manifest.json"}:
        return True
    curated_prefix = "knowledge/curated/"
    if path.startswith(curated_prefix):
        return path.removeprefix(curated_prefix) in CURATED_KNOWLEDGE
    knowledge_prefix = "public-release/knowledge/"
    if path.startswith(knowledge_prefix):
        return path.removeprefix(knowledge_prefix) in PUBLIC_KNOWLEDGE
    public_identity = parse_pulse_path(path, directory="public-release/pulses")
    if public_identity is not None:
        expected_public = (
            expected_pulse_path.replace(
                "content/pulses/", "public-release/pulses/", 1
            )
            if expected_pulse_path is not None
            else None
        )
        return public_identity.date == run_date and (
            expected_public is None or path == expected_public
        )
    public_artifact_prefix = f"public-release/artifacts/{run_date}/"
    if path.startswith(public_artifact_prefix):
        return bool(
            SAFE_ARTIFACT_RE.fullmatch(path.removeprefix(public_artifact_prefix))
        )
    return False


def _name_status(text: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for line in text.splitlines():
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) != 2:
            raise ScheduledPublishError("Git returned an ambiguous changed path")
        rows.append((fields[0], fields[1]))
    return rows


def _changed_paths(runner: CommandRunner, project_root: Path) -> list[tuple[str, str]]:
    tracked = _name_status(
        _run(
            runner,
            project_root,
            ("git", "diff", "--name-status", "--no-renames", "HEAD", "--"),
        ).stdout
    )
    untracked = _run(
        runner,
        project_root,
        ("git", "ls-files", "--others", "--exclude-standard"),
    ).stdout.splitlines()
    return [*tracked, *(("?", path) for path in untracked if path)]


def _validate_publish_changes(
    changes: Sequence[tuple[str, str]],
    run_date: str,
    expected_pulse_path: str | None = None,
) -> list[str]:
    if not changes:
        raise ScheduledPublishError("a published pulse produced no public Git changes")
    paths: list[str] = []
    for status, path in changes:
        if status not in {"A", "M", "?"}:
            raise ScheduledPublishError(f"scheduled publication refuses Git status {status}")
        if not _allowed_change(path, run_date, expected_pulse_path):
            raise ScheduledPublishError(f"scheduled publication refuses changed path: {path}")
        if path not in paths:
            paths.append(path)
    pulse_paths = [path for path in paths if parse_pulse_path(path) is not None]
    if expected_pulse_path is not None:
        pulse_is_present = pulse_paths == [expected_pulse_path]
    else:
        pulse_is_present = len(pulse_paths) == 1
    if not pulse_is_present:
        raise ScheduledPublishError("published pulse is missing from the Git change set")
    if "public-release/manifest.json" not in paths:
        raise ScheduledPublishError("public release manifest did not change")
    return sorted(paths)


def _validate_publish_files(project_root: Path, paths: Sequence[str]) -> None:
    for path in paths:
        candidate = project_root / path
        try:
            mode = candidate.lstat().st_mode
        except OSError as exc:
            raise ScheduledPublishError(f"changed public file is unavailable: {path}") from exc
        if not stat.S_ISREG(mode):
            raise ScheduledPublishError(f"scheduled publication requires a regular file: {path}")


def _ensure_only_staged_changes(runner: CommandRunner, project_root: Path) -> None:
    unstaged = _run(
        runner,
        project_root,
        ("git", "diff", "--quiet", "--"),
        expected=(0, 1),
    )
    if unstaged.returncode != 0:
        raise ScheduledPublishError("unstaged tracked changes remain after allowlisted staging")
    untracked = _run(
        runner,
        project_root,
        ("git", "ls-files", "--others", "--exclude-standard"),
    ).stdout.strip()
    if untracked:
        raise ScheduledPublishError("untracked public changes remain after allowlisted staging")


def _find_workflow_run(
    runner: CommandRunner,
    project_root: Path,
    publication: Mapping[str, Any],
    commit_sha: str,
) -> tuple[str, str]:
    command = (
        "gh",
        "run",
        "list",
        "--repo",
        str(publication["repository"]),
        "--workflow",
        str(publication["workflow"]),
        "--commit",
        commit_sha,
        "--event",
        "push",
        "--limit",
        "1",
        "--json",
        "databaseId,url",
    )
    for _ in range(30):
        rows = _single_json(_run(runner, project_root, command).stdout, "workflow lookup")
        if isinstance(rows, list) and rows:
            row = rows[0]
            run_id = str(row.get("databaseId", ""))
            url = str(row.get("url", ""))
            if run_id.isdigit() and url.startswith("https://github.com/"):
                return run_id, url
        time.sleep(2)
    raise ScheduledPublishError("GitHub Pages workflow did not appear after push")


def _deploy(
    runner: CommandRunner,
    project_root: Path,
    publication: Mapping[str, Any],
    commit_sha: str,
) -> tuple[str, str]:
    run_id, run_url = _find_workflow_run(
        runner, project_root, publication, commit_sha
    )
    _run(
        runner,
        project_root,
        (
            "gh",
            "run",
            "watch",
            run_id,
            "--repo",
            str(publication["repository"]),
            "--exit-status",
        ),
        timeout=900,
    )
    view = _single_json(
        _run(
            runner,
            project_root,
            (
                "gh",
                "run",
                "view",
                run_id,
                "--repo",
                str(publication["repository"]),
                "--json",
                "status,conclusion,headSha,url",
            ),
        ).stdout,
        "workflow result",
    )
    if not isinstance(view, dict) or (
        view.get("status") != "completed"
        or view.get("conclusion") != "success"
        or view.get("headSha") != commit_sha
    ):
        raise ScheduledPublishError("GitHub Pages workflow did not deploy successfully")
    return run_url, str(publication["site_url"])


def run_scheduled_pipeline(
    project_root: Path,
    *,
    run_date: str,
    runner: CommandRunner = _default_runner,
) -> ScheduledRunResult:
    """Run once; commit, push, and await Pages only for a published pulse."""

    run_date = _parse_date(run_date)
    project_root = project_root.resolve(strict=True)
    daily: dict[str, Any] | None = None
    commit_sha: str | None = None
    try:
        publication = _publication_policy(project_root)
        base_head = _git_preflight(runner, project_root, publication)
        python = project_root / ".venv" / "bin" / "python"
        completed = _run(
            runner,
            project_root,
            (
                str(python),
                "scripts/run_daily_pipeline.py",
                "--project-root",
                str(project_root),
                "--mode",
                "live",
                "--date",
                run_date,
            ),
            timeout=3600,
            expected=(0, 2),
        )
        daily = _parse_daily_result(project_root, completed)
        if daily["status"] != "published":
            if not _clean_worktree(runner, project_root):
                raise ScheduledPublishError(
                    "a non-published daily run left public Git changes; nothing was staged"
                )
            return ScheduledRunResult(
                status=str(daily["status"]),
                date=run_date,
                reason=str(daily["reason"]),
                daily=daily,
                deployment_status="not_requested",
            )

        pulse_path = daily.get("pulse_path")
        pulse_identity = (
            parse_pulse_path(pulse_path) if isinstance(pulse_path, str) else None
        )
        if daily.get("release_advanced") is not True or (
            pulse_identity is None or pulse_identity.date != run_date
        ):
            raise ScheduledPublishError("published daily result is not safe to deploy")

        _run(
            runner,
            project_root,
            (
                str(python),
                "scripts/export_public_release.py",
                "--output",
                str(publication["public_release_directory"]),
            ),
            timeout=300,
        )
        _run(
            runner,
            project_root,
            (
                str(python),
                "scripts/audit_public_release.py",
                "--directory",
                str(publication["public_release_directory"]),
            ),
            timeout=300,
        )
        current_head = _run(runner, project_root, ("git", "rev-parse", "HEAD")).stdout.strip()
        if current_head != base_head:
            raise ScheduledPublishError("HEAD changed during the daily transaction")
        paths = _validate_publish_changes(
            _changed_paths(runner, project_root), run_date, pulse_path
        )
        _validate_publish_files(project_root, paths)
        _run(runner, project_root, ("git", "add", "--", *paths))
        _ensure_only_staged_changes(runner, project_root)
        staged = _name_status(
            _run(
                runner,
                project_root,
                ("git", "diff", "--cached", "--name-status", "--no-renames"),
            ).stdout
        )
        _validate_publish_changes(staged, run_date, pulse_path)
        _run(runner, project_root, ("git", "diff", "--cached", "--check"))
        _run(
            runner,
            project_root,
            (
                "git",
                "commit",
                "-m",
                f"Publish pulse {PurePosixPath(pulse_path).stem}",
            ),
            timeout=300,
        )
        commit_sha = _run(runner, project_root, ("git", "rev-parse", "HEAD")).stdout.strip()
        if not SHA_RE.fullmatch(commit_sha) or commit_sha == base_head:
            raise ScheduledPublishError("scheduled commit was not created safely")
        if not _clean_worktree(runner, project_root):
            raise ScheduledPublishError("worktree is not clean after the scheduled commit")
        _run(
            runner,
            project_root,
            ("git", "fetch", "--quiet", str(publication["remote"]), str(publication["branch"])),
            timeout=180,
        )
        upstream = _run(
            runner,
            project_root,
            ("git", "rev-parse", f"refs/remotes/{publication['remote']}/{publication['branch']}"),
        ).stdout.strip()
        if upstream != base_head:
            raise ScheduledPublishError("origin/main changed during the daily transaction")
        _run(
            runner,
            project_root,
            ("git", "push", str(publication["remote"]), f"{commit_sha}:{publication['branch']}"),
            timeout=300,
        )
        workflow_url, site_url = _deploy(
            runner, project_root, publication, commit_sha
        )
        return ScheduledRunResult(
            status="published",
            date=run_date,
            reason="reviewed pulse committed, pushed, and deployed through GitHub Pages",
            daily=daily,
            deployment_status="deployed",
            commit_sha=commit_sha,
            workflow_run_url=workflow_url,
            site_url=site_url,
        )
    except Exception as exc:
        return ScheduledRunResult(
            status="failed" if daily is not None else "blocked",
            date=run_date,
            reason=str(exc),
            daily=daily,
            deployment_status="failed" if daily is not None else "blocked",
            commit_sha=commit_sha,
        )
