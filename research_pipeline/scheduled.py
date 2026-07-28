"""Guarded Git publication for one already transactional daily pipeline run."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import time
from dataclasses import dataclass
from datetime import date as calendar_date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from .config import load_yaml
from .external_preflight import (
    load_ready_scheduled_search_batch,
    load_scheduled_search_outcome,
    scheduled_outcome_path,
)
from .hashing import sha256_file
from .pulse_identity import parse_pulse_path
from .validation import validate_records
from .workflow import WorkflowStore


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


class ScheduledRetryableError(ScheduledPublishError):
    """A transient GitHub or network operation can resume without new research."""


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
    try:
        completed = runner(command, project_root, timeout)
    except subprocess.TimeoutExpired as exc:
        label = " ".join(command[:3])
        raise ScheduledRetryableError(f"command timed out ({label})") from exc
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
    runner: CommandRunner,
    project_root: Path,
    publication: Mapping[str, Any],
    *,
    allow_publication_changes: bool = False,
    preserve_head: bool = False,
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
    clean = _clean_worktree(runner, project_root)
    if not clean and not allow_publication_changes:
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
    if not SHA_RE.fullmatch(head) or not SHA_RE.fullmatch(upstream):
        raise ScheduledPublishError("Git returned an invalid main branch identity")
    if head != upstream:
        ancestor = _run(
            runner,
            project_root,
            ("git", "merge-base", "--is-ancestor", head, upstream),
            expected=(0, 1),
        )
        if ancestor.returncode == 0 and clean and not preserve_head:
            _run(
                runner,
                project_root,
                ("git", "merge", "--ff-only", upstream),
                timeout=180,
            )
            head = _run(
                runner, project_root, ("git", "rev-parse", "HEAD")
            ).stdout.strip()
            if head != upstream:
                raise ScheduledPublishError("clean stale main did not fast-forward exactly")
        elif not allow_publication_changes:
            raise ScheduledPublishError("local main has unrecognized divergence from origin/main")
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


def _committed_changes(
    runner: CommandRunner, project_root: Path, base: str, head: str
) -> list[tuple[str, str]]:
    return _name_status(
        _run(
            runner,
            project_root,
            ("git", "diff", "--name-status", "--no-renames", f"{base}..{head}", "--"),
        ).stdout
    )


def _touches_publication_state(path: str) -> bool:
    return path.startswith(
        (
            "content/pulses/",
            "knowledge/curated/",
            "public/artifacts/",
            "public-release/",
        )
    )


def _rebase_unpushed_publication(
    runner: CommandRunner,
    project_root: Path,
    publication: Mapping[str, Any],
    *,
    base_head: str,
    upstream: str,
    run_date: str,
    pulse_path: str,
) -> str:
    ancestor = _run(
        runner,
        project_root,
        ("git", "merge-base", "--is-ancestor", base_head, upstream),
        expected=(0, 1),
    )
    if ancestor.returncode != 0:
        raise ScheduledPublishError("origin/main no longer descends from the publication base")
    remote_changes = _committed_changes(runner, project_root, base_head, upstream)
    if any(_touches_publication_state(path) for _, path in remote_changes):
        raise ScheduledPublishError(
            "origin/main changed accepted publication state; manual reconciliation is required"
        )
    try:
        _run(runner, project_root, ("git", "rebase", upstream), timeout=300)
    except ScheduledPublishError:
        try:
            _run(runner, project_root, ("git", "rebase", "--abort"), timeout=120)
        except ScheduledPublishError:
            pass
        raise ScheduledPublishError(
            "the publication commit could not be rebased safely"
        )
    rebased = _run(runner, project_root, ("git", "rev-parse", "HEAD")).stdout.strip()
    if not SHA_RE.fullmatch(rebased) or rebased == upstream:
        raise ScheduledPublishError("rebased publication commit identity is invalid")
    changes = _committed_changes(runner, project_root, upstream, rebased)
    paths = _validate_publish_changes(changes, run_date, pulse_path)
    _validate_publish_files(project_root, paths)
    python = project_root / ".venv" / "bin" / "python"
    _run(
        runner,
        project_root,
        (str(python), "scripts/audit_public_release.py", "--directory", str(publication["public_release_directory"])),
        timeout=300,
    )
    _run(runner, project_root, (str(python), "-m", "pytest"), timeout=3600)
    _run(runner, project_root, ("npm", "test"), timeout=1800)
    _run(runner, project_root, ("npm", "run", "build"), timeout=1800)
    return rebased


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


def prepare_scheduled_pipeline(
    project_root: Path,
    *,
    run_date: str,
    runner: CommandRunner = _default_runner,
) -> dict[str, Any]:
    """Synchronize and discover once, then return the resumable editor handoff."""

    from .external import (
        ExternalMetadataRateLimit,
        ExternalMetadataTimeout,
        run_external_search,
    )
    from .external_preflight import write_scheduled_search_outcome

    run_date = _parse_date(run_date)
    project_root = project_root.resolve(strict=True)
    workflow = WorkflowStore(project_root, run_date)
    failure = workflow.value.get("failure")
    if (
        workflow.stage("discover") is None
        and isinstance(failure, Mapping)
        and failure.get("stage") == "discover"
        and isinstance(failure.get("retry_not_before"), str)
    ):
        retry_at = datetime.fromisoformat(
            str(failure["retry_not_before"]).replace("Z", "+00:00")
        )
        if retry_at > datetime.now(timezone.utc):
            return {"status": "deferred", "workflow": workflow.as_dict()}
    publication = _publication_policy(project_root)
    sync = workflow.stage("synchronize_base")
    base = _git_preflight(
        runner,
        project_root,
        publication,
        allow_publication_changes=sync is not None,
        preserve_head=sync is not None,
    )
    if sync is None:
        workflow.complete_stage(
            "synchronize_base",
            {
                "remote": publication["remote"],
                "branch": publication["branch"],
                "repository": publication["repository"],
            },
            {"base_head": base},
        )
    elif base != sync["outputs"]["base_head"]:
        raise ScheduledPublishError("the prepared workflow base changed locally")
    existing = workflow.stage("discover")
    if existing is not None:
        candidate_count = existing.get("outputs", {}).get("candidate_count")
        return {
            "status": "no_candidates" if candidate_count == 0 else "awaiting_editorial",
            "candidate_count": candidate_count,
            "workflow": workflow.as_dict(),
        }
    as_of = f"{run_date}T06:00:00+03:00"
    try:
        result = run_external_search(
            project_root / "config" / "external-sources.yaml",
            project_root,
            as_of,
        )
    except ExternalMetadataRateLimit as exc:
        delay = exc.retry_after_seconds if exc.retry_after_seconds is not None else 3600
        retry_at = (
            datetime.now(timezone.utc) + timedelta(seconds=max(60, delay))
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
        write_scheduled_search_outcome(
            project_root,
            run_date=run_date,
            as_of=as_of,
            status="deferred",
            reason=str(exc),
        )
        workflow.record_failure(
            stage="discover",
            classification="retryable",
            code="provider_rate_limited",
            reason=str(exc),
            retry_not_before=retry_at,
        )
        return {"status": "deferred", "workflow": workflow.as_dict()}
    except ExternalMetadataTimeout as exc:
        write_scheduled_search_outcome(
            project_root,
            run_date=run_date,
            as_of=as_of,
            status="deferred",
            reason=str(exc),
        )
        workflow.record_failure(
            stage="discover",
            classification="deferred",
            code="provider_timeout",
            reason=str(exc),
        )
        return {"status": "deferred", "workflow": workflow.as_dict()}
    outcome_path = write_scheduled_search_outcome(
        project_root,
        run_date=run_date,
        as_of=as_of,
        status="ready",
        reason="metadata search completed and bound an immutable candidate batch",
        search_result=result,
    )
    outcome = load_scheduled_search_outcome(
        project_root, outcome_path, run_date=run_date
    )
    workflow.complete_stage(
        "discover",
        {"date": run_date, "as_of": outcome["as_of"]},
        {
            "status": outcome["status"],
            "outcome_sha256": outcome["outcome_sha256"],
            "batch_id": outcome["batch_id"],
            "batch_sha256": outcome["batch_sha256"],
            "batch_path": outcome["batch_path"],
            "candidate_count": result["candidate_count"],
        },
    )
    return {
        "status": "awaiting_editorial" if result["candidate_count"] else "no_candidates",
        "candidate_count": result["candidate_count"],
        "batch_path": result["batch_path"],
        "workflow": workflow.as_dict(),
    }


def select_scheduled_candidate(
    project_root: Path,
    *,
    run_date: str,
    candidate_id: str,
    candidate_sha256: str,
    runner: CommandRunner = _default_runner,
) -> dict[str, Any]:
    """Bind one exact eligible candidate and materialize its official PDF once."""

    from .external_identity import accepted_external_identities, normalize_external_identity
    from .release import _read_current_pointer

    run_date = _parse_date(run_date)
    project_root = project_root.resolve(strict=True)
    workflow = WorkflowStore(project_root, run_date)
    outcome, batch = load_ready_scheduled_search_batch(
        project_root, scheduled_outcome_path(run_date), run_date=run_date
    )
    if workflow.stage("discover") is None:
        workflow.complete_stage(
            "discover",
            {"date": run_date, "as_of": outcome["as_of"]},
            {
                "status": outcome["status"],
                "outcome_sha256": outcome["outcome_sha256"],
                "batch_id": outcome["batch_id"],
                "batch_sha256": outcome["batch_sha256"],
                "batch_path": outcome["batch_path"],
            },
        )
    matches = [
        candidate
        for candidate in batch["candidates"]
        if candidate.get("id") == candidate_id
        and candidate.get("candidate_sha256") == candidate_sha256
    ]
    if len(matches) != 1 or matches[0].get("provider") != "arxiv":
        raise ScheduledPublishError("exact arXiv candidate is absent or ambiguous")
    identity = normalize_external_identity(matches[0].get("canonical_url"))
    if identity is None or identity in accepted_external_identities(
        project_root, _read_current_pointer(project_root)
    ):
        raise ScheduledPublishError("selected source version is already accepted")
    selected = workflow.complete_stage(
        "select",
        {
            "batch_sha256": outcome["batch_sha256"],
            "candidate_id": candidate_id,
            "candidate_sha256": candidate_sha256,
        },
        {"candidate_id": candidate_id, "candidate_sha256": candidate_sha256},
    )
    materialized = workflow.stage("materialize_source")
    if materialized is None:
        completed = _run(
            runner,
            project_root,
            (
                str(project_root / ".venv" / "bin" / "python"),
                "scripts/fetch_arxiv_evidence.py",
                "--project-root",
                str(project_root),
                "--batch",
                str(project_root / str(outcome["batch_path"])),
                "--candidate-id",
                candidate_id,
                "--candidate-sha256",
                candidate_sha256,
            ),
            timeout=180,
            expected=(0, 3),
        )
        evidence = _single_json(completed.stdout, "arXiv evidence fetch")
        if completed.returncode == 3:
            if not isinstance(evidence, Mapping) or evidence.get("status") != "deferred":
                raise ScheduledPublishError("arXiv evidence deferral is malformed")
            delay = evidence.get("retry_after_seconds")
            retry_at = None
            if type(delay) is int:
                retry_at = (
                    datetime.now(timezone.utc) + timedelta(seconds=max(60, delay))
                ).isoformat(timespec="seconds").replace("+00:00", "Z")
            workflow.record_failure(
                stage="materialize_source",
                classification="retryable",
                code="arxiv_evidence_deferred",
                reason=str(evidence.get("reason", "arXiv evidence fetch was deferred")),
                retry_not_before=retry_at,
            )
            return {"status": "deferred", "workflow": workflow.as_dict()}
        if not isinstance(evidence, Mapping) or evidence.get("status") != "fetched":
            raise ScheduledPublishError("arXiv evidence fetch returned an invalid result")
        versioned = str(matches[0]["versioned_external_id"]).replace("/", "-").casefold()
        source_suffix = re.sub(r"[^a-z0-9]+", "-", versioned).strip("-")
        materialized = workflow.complete_stage(
            "materialize_source",
            {
                "selection_receipt_sha256": selected["receipt_sha256"],
                "pdf_sha256": evidence["content_sha256"],
            },
            {
                "source_id": f"src-external-arxiv-{source_suffix}",
                "pdf_sha256": evidence["content_sha256"],
                "path": evidence["path"],
                "logical_path": evidence["logical_path"],
            },
        )
    return {"status": "awaiting_editorial", "workflow": workflow.as_dict(), "source": materialized["outputs"]}


def run_scheduled_pipeline(
    project_root: Path,
    *,
    run_date: str,
    runner: CommandRunner = _default_runner,
) -> ScheduledRunResult:
    """Advance one date-scoped run from its earliest incomplete stage."""

    run_date = _parse_date(run_date)
    project_root = project_root.resolve(strict=True)
    daily: dict[str, Any] | None = None
    commit_sha: str | None = None
    workflow: WorkflowStore | None = None
    current_stage = "synchronize_base"
    try:
        workflow = WorkflowStore(project_root, run_date)
        completed_outcome = workflow.value.get("outcome")
        publish_reference = workflow.stage("publish_local")
        if publish_reference is not None:
            stored_daily = publish_reference.get("outputs", {}).get("daily")
            if isinstance(stored_daily, Mapping):
                daily = dict(stored_daily)
        if isinstance(completed_outcome, Mapping):
            status = str(completed_outcome["status"])
            return ScheduledRunResult(
                status=status,
                date=run_date,
                reason=str(completed_outcome["reason"]),
                daily=daily,
                deployment_status=("deployed" if status == "published" else "not_requested"),
                commit_sha=completed_outcome.get("commit_sha"),
                workflow_run_url=completed_outcome.get("workflow_run_url"),
                site_url=completed_outcome.get("site_url"),
            )
        failure = workflow.value.get("failure")
        if isinstance(failure, Mapping) and failure.get("classification") == "terminal":
            return ScheduledRunResult(
                status="failed",
                date=run_date,
                reason=str(failure["reason"]),
                daily=daily,
                deployment_status="failed" if daily else "blocked",
            )
        if isinstance(failure, Mapping) and isinstance(failure.get("retry_not_before"), str):
            retry_at = datetime.fromisoformat(
                str(failure["retry_not_before"]).replace("Z", "+00:00")
            )
            if retry_at > datetime.now(timezone.utc):
                return ScheduledRunResult(
                    status="deferred",
                    date=run_date,
                    reason=str(failure["reason"]),
                    daily=daily,
                    deployment_status="pending" if daily else "not_requested",
                )

        publication = _publication_policy(project_root)
        sync = workflow.stage("synchronize_base")
        allow_changes = sync is not None and workflow.stage("push") is None
        base_head = _git_preflight(
            runner,
            project_root,
            publication,
            allow_publication_changes=allow_changes,
            preserve_head=sync is not None,
        )
        if sync is None:
            sync = workflow.complete_stage(
                "synchronize_base",
                {
                    "remote": publication["remote"],
                    "branch": publication["branch"],
                    "repository": publication["repository"],
                },
                {"base_head": base_head},
            )
        elif (
            workflow.stage("commit") is None
            and workflow.stage("publish_local") is None
            and base_head != sync["outputs"]["base_head"]
        ):
            raise ScheduledPublishError("the prepared workflow base changed locally")
        workflow_base = str(sync["outputs"]["base_head"])

        python = project_root / ".venv" / "bin" / "python"
        preflight_relative = scheduled_outcome_path(run_date)
        preflight_path = project_root / preflight_relative
        if preflight_path.exists() and workflow.stage("discover") is None:
            current_stage = "discover"
            preflight = load_scheduled_search_outcome(
                project_root, preflight_relative, run_date=run_date
            )
            if preflight["status"] == "failed":
                reason = str(preflight["reason"])
                if "429" in reason or "rate limit" in reason.casefold():
                    workflow.record_failure(
                        stage="discover",
                        classification="deferred",
                        code="provider_rate_limited",
                        reason=reason,
                    )
                    return ScheduledRunResult(
                        status="deferred",
                        date=run_date,
                        reason=reason,
                        daily=None,
                        deployment_status="not_requested",
                    )
                raise ScheduledPublishError(
                    f"scheduled external metadata preflight failed: {reason}"
                )
            workflow.complete_stage(
                "discover",
                {"date": run_date, "as_of": preflight["as_of"]},
                {
                    "status": preflight["status"],
                    "outcome_sha256": preflight["outcome_sha256"],
                    "batch_id": preflight.get("batch_id"),
                    "batch_sha256": preflight.get("batch_sha256"),
                    "batch_path": preflight.get("batch_path"),
                },
            )
        if preflight_path.exists() and workflow.stage("validate") is None:
            current_stage = "validate"
            preflight = load_scheduled_search_outcome(
                project_root, preflight_relative, run_date=run_date
            )
            package_path = (
                project_root / "data" / "automatic" / "packages" / f"{run_date}.json"
            )
            if preflight["status"] == "ready" and package_path.exists():
                from .automatic import validate_automatic_package
                from .release import _read_current_pointer

                outcome, batch = load_ready_scheduled_search_batch(
                    project_root, preflight_relative, run_date=run_date
                )
                validation = validate_automatic_package(
                    project_root,
                    run_date,
                    batch_id=str(outcome["batch_id"]),
                    candidates=batch["candidates"],
                    checkpoint=_read_current_pointer(project_root),
                )
                if validation is not None:
                    binding = validation.package["candidate"]
                    selected = workflow.complete_stage(
                        "select",
                        {
                            "batch_sha256": outcome["batch_sha256"],
                            "candidate_id": binding["candidate_id"],
                            "candidate_sha256": binding["candidate_sha256"],
                        },
                        {
                            "candidate_id": binding["candidate_id"],
                            "candidate_sha256": binding["candidate_sha256"],
                        },
                    )
                    materialized = workflow.complete_stage(
                        "materialize_source",
                        {
                            "selection_receipt_sha256": selected["receipt_sha256"],
                            "pdf_sha256": validation.source["content_sha256"],
                        },
                        {
                            "source_id": validation.source["id"],
                            "pdf_sha256": validation.source["content_sha256"],
                            "logical_path": validation.source["relative_path"],
                        },
                    )
                    package_sha = sha256_file(package_path)
                    authored = workflow.complete_stage(
                        "author",
                        {
                            "materialization_receipt_sha256": materialized[
                                "receipt_sha256"
                            ],
                            "package_sha256": package_sha,
                        },
                        {"package_sha256": package_sha},
                    )
                    workflow.complete_stage(
                        "validate",
                        {
                            "author_receipt_sha256": authored["receipt_sha256"],
                            "package_schema_sha256": sha256_file(
                                project_root / "schemas" / "automatic-pulse-package.schema.json"
                            ),
                        },
                        {
                            "package_sha256": package_sha,
                            "source_id": validation.source["id"],
                            "knowledge_ids": list(validation.pulse_ids),
                            "artifact_ids": [
                                item.artifact_id for item in validation.artifact_payloads
                            ],
                        },
                    )

        current_stage = "publish_local"
        publish_reference = workflow.stage("publish_local")
        if publish_reference is None:
            daily_command = [
                str(python),
                "scripts/run_daily_pipeline.py",
                "--project-root",
                str(project_root),
                "--mode",
                "live",
                "--date",
                run_date,
            ]
            try:
                os.lstat(preflight_path)
            except FileNotFoundError:
                pass
            else:
                daily_command.extend(("--external-search-outcome", preflight_relative))
            completed = _run(
                runner,
                project_root,
                tuple(daily_command),
                timeout=3600,
                expected=(0, 2),
            )
            daily = _parse_daily_result(project_root, completed)
            if daily["status"] != "published":
                if not _clean_worktree(runner, project_root):
                    raise ScheduledPublishError(
                        "a non-published daily run left public Git changes; nothing was staged"
                    )
                workflow.complete(str(daily["status"]), str(daily["reason"]))
                return ScheduledRunResult(
                    status=str(daily["status"]),
                    date=run_date,
                    reason=str(daily["reason"]),
                    daily=daily,
                    deployment_status="not_requested",
                )
            publish_reference = workflow.complete_stage(
                "publish_local",
                {
                    "base_head": workflow_base,
                    "external_search_outcome": (
                        preflight_relative
                        if (project_root / preflight_relative).exists()
                        else None
                    ),
                },
                {"daily": daily},
            )
        else:
            stored = publish_reference.get("outputs", {}).get("daily")
            if not isinstance(stored, Mapping):
                raise ScheduledPublishError("stored local publication result is malformed")
            daily = dict(stored)

        pulse_path = daily.get("pulse_path") if daily else None
        pulse_identity = parse_pulse_path(pulse_path) if isinstance(pulse_path, str) else None
        if daily is None or daily.get("release_advanced") is not True or (
            pulse_identity is None or pulse_identity.date != run_date
        ):
            raise ScheduledPublishError("published daily result is not safe to deploy")

        _run(
            runner,
            project_root,
            (str(python), "scripts/export_public_release.py", "--output", str(publication["public_release_directory"])),
            timeout=300,
        )
        _run(
            runner,
            project_root,
            (str(python), "scripts/audit_public_release.py", "--directory", str(publication["public_release_directory"])),
            timeout=300,
        )

        current_stage = "commit"
        commit_reference = workflow.stage("commit")
        if commit_reference is None:
            current_head = _run(runner, project_root, ("git", "rev-parse", "HEAD")).stdout.strip()
            if current_head == workflow_base:
                paths = _validate_publish_changes(
                    _changed_paths(runner, project_root), run_date, str(pulse_path)
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
                _validate_publish_changes(staged, run_date, str(pulse_path))
                _run(runner, project_root, ("git", "diff", "--cached", "--check"))
                _run(
                    runner,
                    project_root,
                    (
                        "git",
                        "commit",
                        "-m",
                        f"Publish pulse {PurePosixPath(str(pulse_path)).stem}",
                    ),
                    timeout=300,
                )
                commit_sha = _run(
                    runner, project_root, ("git", "rev-parse", "HEAD")
                ).stdout.strip()
            else:
                count = _run(
                    runner,
                    project_root,
                    ("git", "rev-list", "--count", f"{workflow_base}..{current_head}"),
                ).stdout.strip()
                if count != "1" or not _clean_worktree(runner, project_root):
                    raise ScheduledPublishError(
                        "HEAD changed before the publication commit"
                    )
                recovered = _committed_changes(
                    runner, project_root, workflow_base, current_head
                )
                paths = _validate_publish_changes(
                    recovered, run_date, str(pulse_path)
                )
                _validate_publish_files(project_root, paths)
                commit_sha = current_head
            if not SHA_RE.fullmatch(commit_sha) or commit_sha == workflow_base:
                raise ScheduledPublishError("scheduled commit was not created safely")
            if not _clean_worktree(runner, project_root):
                raise ScheduledPublishError("worktree is not clean after the scheduled commit")
            commit_reference = workflow.complete_stage(
                "commit",
                {
                    "publish_receipt_sha256": publish_reference["receipt_sha256"],
                    "base_head": workflow_base,
                },
                {"commit_sha": commit_sha, "base_head": workflow_base},
            )
        else:
            commit_sha = str(commit_reference["outputs"]["commit_sha"])

        current_stage = "push"
        push_reference = workflow.stage("push")
        if push_reference is None:
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
            head = _run(runner, project_root, ("git", "rev-parse", "HEAD")).stdout.strip()
            final_commit = head
            if upstream == head:
                pass
            elif upstream != workflow_base:
                final_commit = _rebase_unpushed_publication(
                    runner,
                    project_root,
                    publication,
                    base_head=workflow_base,
                    upstream=upstream,
                    run_date=run_date,
                    pulse_path=str(pulse_path),
                )
            elif head != commit_sha:
                raise ScheduledPublishError("local publication commit changed before push")
            if upstream != final_commit:
                _run(
                    runner,
                    project_root,
                    ("git", "push", str(publication["remote"]), f"{final_commit}:{publication['branch']}"),
                    timeout=300,
                )
            commit_sha = final_commit
            push_reference = workflow.complete_stage(
                "push",
                {
                    "commit_receipt_sha256": commit_reference["receipt_sha256"],
                    "observed_upstream": upstream,
                },
                {"commit_sha": commit_sha, "remote": publication["remote"], "branch": publication["branch"]},
            )
        else:
            commit_sha = str(push_reference["outputs"]["commit_sha"])

        current_stage = "verify_deployment"
        deployment_reference = workflow.stage("verify_deployment")
        if deployment_reference is None:
            workflow_url, site_url = _deploy(runner, project_root, publication, commit_sha)
            deployment_reference = workflow.complete_stage(
                "verify_deployment",
                {
                    "push_receipt_sha256": push_reference["receipt_sha256"],
                    "commit_sha": commit_sha,
                },
                {"commit_sha": commit_sha, "workflow_run_url": workflow_url, "site_url": site_url},
            )
        outputs = deployment_reference["outputs"]
        reason = "reviewed pulse committed, pushed, and deployed through GitHub Pages"
        workflow.complete(
            "published",
            reason,
            commit_sha=commit_sha,
            workflow_run_url=outputs["workflow_run_url"],
            site_url=outputs["site_url"],
        )
        return ScheduledRunResult(
            status="published",
            date=run_date,
            reason=reason,
            daily=daily,
            deployment_status="deployed",
            commit_sha=commit_sha,
            workflow_run_url=str(outputs["workflow_run_url"]),
            site_url=str(outputs["site_url"]),
        )
    except ScheduledRetryableError as exc:
        if workflow is not None:
            try:
                workflow.record_failure(
                    stage=current_stage,
                    classification="retryable",
                    code="transient_command_failure",
                    reason=str(exc),
                )
            except Exception:
                pass
        return ScheduledRunResult(
            status="deferred",
            date=run_date,
            reason=str(exc),
            daily=daily,
            deployment_status="pending" if daily is not None else "not_requested",
            commit_sha=commit_sha,
        )
    except Exception as exc:
        editorial_repair = current_stage in {"author", "validate"}
        transient_publication = (
            current_stage in {"push", "verify_deployment"}
            and isinstance(exc, ScheduledPublishError)
            and str(exc).startswith("command failed (")
        )
        if workflow is not None:
            try:
                workflow.record_failure(
                    stage=current_stage,
                    classification=(
                        "deferred"
                        if editorial_repair
                        else "retryable" if transient_publication else "terminal"
                    ),
                    code=(
                        "editorial_repair_required"
                        if editorial_repair
                        else "transient_publication_failure"
                        if transient_publication
                        else "safety_condition_failed"
                    ),
                    reason=str(exc),
                )
            except Exception:
                pass
        if editorial_repair or transient_publication:
            return ScheduledRunResult(
                status="review_required" if editorial_repair else "deferred",
                date=run_date,
                reason=str(exc),
                daily=daily,
                deployment_status=(
                    "pending" if transient_publication and daily is not None else "not_requested"
                ),
                commit_sha=commit_sha,
            )
        return ScheduledRunResult(
            status="failed" if daily is not None else "blocked",
            date=run_date,
            reason=str(exc),
            daily=daily,
            deployment_status="failed" if daily is not None else "blocked",
            commit_sha=commit_sha,
        )
