"""One conservative, transactional entry point for a daily Residual run.

The orchestration deliberately keeps discovery, review, report rendering, and
publication separate.  External metadata is never evidence until an exact
candidate hash has an append-only approval decision.  A material local change
is never turned into prose unless a reviewed structured proposal is bound to
the exact novelty analysis.  ``publish_release`` remains the sole checkpoint
writer and therefore the final commit point.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import subprocess
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date as calendar_date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Sequence

import yaml

from .config import (
    load_pipeline_config,
    load_pulse_constraints,
    load_yaml,
    resolve_live_root,
)
from .errors import ConfigurationError, PipelineError, PublicationError, SnapshotError
from .release import (
    PublishResult,
    ReleaseBuildResult,
    _read_current_pointer,
    build_release_candidate,
    publish_release,
)
from .snapshot import build_snapshot
from .paths import open_directory_under_root, open_regular_file_under_root
from .validation import strict_json_loads, validate_records


RESULT_STATUSES = {"published", "no_update", "review_required", "blocked", "failed"}
RELEASE_ID_PATTERN = re.compile(r"^release-[0-9a-f]{20}$")


class DailyBlockedError(PipelineError):
    """A reviewed prerequisite is absent or deliberately disabled."""


@dataclass(frozen=True)
class DailyContext:
    project_root: Path
    mode: str
    date: str
    source_config: Any
    pulse_config: Mapping[str, Any]
    pulse_constraints: Mapping[str, Any]
    external_config: Mapping[str, Any]
    source_root: Path


@dataclass(frozen=True)
class SnapshotOutcome:
    snapshot_id: str
    snapshot_directory: Path
    created: bool


@dataclass(frozen=True)
class CandidateOutcome:
    release_id: str
    release_directory: Path
    created: bool
    status: str
    semantic_changed: bool


@dataclass(frozen=True)
class ExternalOutcome:
    pending_candidate_ids: tuple[str, ...] = ()
    approved_candidate_ids: tuple[str, ...] = ()
    rejected_candidate_ids: tuple[str, ...] = ()
    batch_id: str | None = None
    review_path: str | None = None


@dataclass(frozen=True)
class AnalysisOutcome:
    status: str
    reason: str
    evidence_ids: tuple[str, ...]
    analysis: Mapping[str, Any] | None = None
    review_path: str | None = None


@dataclass(frozen=True)
class PulseOutcome:
    path: str
    artifact_manifest_urls: tuple[str, ...]
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class DailyRunResult:
    status: str
    date: str
    run_id: str
    release_id: str | None
    pulse_path: str | None
    artifact_urls: tuple[str, ...]
    release_advanced: bool
    checkpoint_refreshed: bool
    reason: str
    evidence_ids: tuple[str, ...]
    pending_review_count: int
    pending_review_path: str | None
    schema_version: str = "1.0.0"

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "date": self.date,
            "run_id": self.run_id,
            "release_id": self.release_id,
            "pulse_path": self.pulse_path,
            "artifact_urls": list(self.artifact_urls),
            "release_advanced": self.release_advanced,
            "checkpoint_refreshed": self.checkpoint_refreshed,
            "reason": self.reason,
            "evidence_ids": list(self.evidence_ids),
            "pending_review_count": self.pending_review_count,
            "pending_review_path": self.pending_review_path,
        }


LoadContext = Callable[[Path, str, str], DailyContext]
ReadCheckpoint = Callable[[DailyContext], Mapping[str, Any] | None]
SyncLocal = Callable[[DailyContext], SnapshotOutcome]
MonitorExternal = Callable[[DailyContext], ExternalOutcome]
BuildCandidate = Callable[[DailyContext, SnapshotOutcome], CandidateOutcome]
AnalyzeCandidate = Callable[
    [DailyContext, Mapping[str, Any] | None, CandidateOutcome], AnalysisOutcome
]
LoadProposal = Callable[
    [DailyContext, CandidateOutcome, AnalysisOutcome], Mapping[str, Any] | None
]
BuildPulse = Callable[[DailyContext, Mapping[str, Any]], PulseOutcome]
PublishCandidate = Callable[
    [DailyContext, CandidateOutcome, PulseOutcome | None], PublishResult
]


@dataclass(frozen=True)
class DailyDependencies:
    load_context: LoadContext
    read_checkpoint: ReadCheckpoint
    sync_local: SyncLocal
    monitor_external: MonitorExternal
    build_candidate: BuildCandidate
    analyze_candidate: AnalyzeCandidate
    load_proposal: LoadProposal
    build_pulse: BuildPulse
    publish_candidate: PublishCandidate


def _enabled(value: Any) -> bool:
    return value is True or (
        isinstance(value, Mapping) and value.get("enabled") is True
    )


def _parse_date(value: str) -> str:
    try:
        parsed = calendar_date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise DailyBlockedError("date must use the YYYY-MM-DD calendar format") from exc
    if parsed.isoformat() != value:
        raise DailyBlockedError("date must use the YYYY-MM-DD calendar format")
    return value


def _safe_relative(value: str, label: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(
        part in {"", ".", ".."} for part in path.parts
    ):
        raise DailyBlockedError(f"unsafe {label}")
    return path.as_posix()


def _regular_executable(path: Path, label: str) -> None:
    try:
        # Virtual environments normally expose ``bin/python`` as a symlink to
        # the interpreter that created them.  We require a resolvable regular
        # executable, but do not reject that standard venv layout.
        opened = path.stat()
    except OSError as exc:
        raise DailyBlockedError(f"required {label} is unavailable") from exc
    if not stat.S_ISREG(opened.st_mode) or not os.access(path, os.X_OK):
        raise DailyBlockedError(f"required {label} must be a regular executable file")


def _regular_file(path: Path, label: str) -> None:
    try:
        opened = path.lstat()
    except OSError as exc:
        raise DailyBlockedError(f"required {label} is unavailable") from exc
    if not stat.S_ISREG(opened.st_mode) or path.is_symlink():
        raise DailyBlockedError(f"required {label} must be a regular file")


def _resolve_source_root(project_root: Path, source_config: Any) -> Path:
    configured = resolve_live_root(source_config, "imf")
    if configured.is_symlink():
        raise DailyBlockedError("the configured IMF source root must not be a symlink")
    try:
        # ``resolve_live_root`` anchors relative defaults to the config's
        # repository root.  Do not anchor that result a second time here.
        source_root = configured.resolve(strict=True)
    except OSError as exc:
        raise DailyBlockedError(
            "the live IMF source is unavailable; create the documented local snapshot export"
        ) from exc
    if not source_root.is_dir() or source_root.is_symlink():
        raise DailyBlockedError("the live IMF source root is not a safe directory")
    if (
        source_root == project_root
        or source_root in project_root.parents
        or project_root in source_root.parents
    ):
        raise DailyBlockedError("the project and read-only IMF source roots overlap")
    return source_root


def _default_load_context(project_root: Path, mode: str, run_date: str) -> DailyContext:
    try:
        project_root = project_root.resolve(strict=True)
    except OSError as exc:
        raise DailyBlockedError("project root is unavailable") from exc
    if mode != "live":
        raise DailyBlockedError("the scheduled transaction currently requires mode live")
    run_date = _parse_date(run_date)
    _regular_executable(project_root / ".venv" / "bin" / "python", ".venv/bin/python")
    _regular_file(
        project_root / "scripts" / "run_daily_pipeline.py",
        "scripts/run_daily_pipeline.py",
    )

    pulse_path = project_root / "config" / "pulse.yaml"
    sources_path = project_root / "config" / "sources.yaml"
    external_path = project_root / "config" / "external-sources.yaml"
    pulse = load_yaml(pulse_path)
    constraints = load_pulse_constraints(pulse_path)
    source_config = load_pipeline_config(sources_path)
    if not _enabled(pulse.get("external_monitoring")):
        raise DailyBlockedError("external monitoring is not explicitly enabled")
    if not _enabled(source_config.policy.get("external_monitoring")):
        raise DailyBlockedError("source policy has not enabled external monitoring")
    scheduling = pulse.get("scheduling")
    if not _enabled(scheduling):
        raise DailyBlockedError("scheduling is not explicitly enabled")
    if not isinstance(scheduling, Mapping) or (
        scheduling.get("timezone") != "Europe/Moscow"
        or scheduling.get("local_time") != "08:00"
        or scheduling.get("execution_environment") != "local"
        or scheduling.get("commit_push_or_deploy") is not False
    ):
        raise DailyBlockedError("scheduling must remain local at 08:00 Europe/Moscow without Git or deployment")
    product = pulse.get("product")
    if not isinstance(product, Mapping) or product.get("timezone") != "Europe/Moscow":
        raise DailyBlockedError("the product timezone must be Europe/Moscow")
    external_enablement = pulse.get("external_monitoring")
    if not isinstance(external_enablement, Mapping) or (
        external_enablement.get("require_source_approval") is not True
        or external_enablement.get("download_or_execute_code") is not False
    ):
        raise DailyBlockedError("external monitoring must require review and forbid code retrieval or execution")
    report = pulse.get("report")
    if not isinstance(report, Mapping) or report.get("create_when_no_material_change") is not False:
        raise DailyBlockedError("no-update report fabrication must be explicitly disabled")
    extraction = pulse.get("extraction")
    if not isinstance(extraction, Mapping) or extraction.get("execute_code") is not False:
        raise DailyBlockedError("source-code execution must be explicitly disabled")

    try:
        from .external import load_external_config
    except ImportError as exc:
        raise DailyBlockedError("the reviewed external metadata monitor is unavailable") from exc
    try:
        external = load_external_config(external_path)
    except Exception as exc:
        raise DailyBlockedError(
            f"external metadata configuration failed validation: {exc}"
        ) from exc
    policy = external.get("policy")
    if (
        not isinstance(policy, Mapping)
        or policy.get("metadata_only") is not True
        or policy.get("download_full_text") is not False
    ):
        raise DailyBlockedError("external monitoring must remain metadata-only")
    source_root = _resolve_source_root(project_root, source_config)
    return DailyContext(
        project_root=project_root,
        mode=mode,
        date=run_date,
        source_config=source_config,
        pulse_config=pulse,
        pulse_constraints=constraints,
        external_config=external,
        source_root=source_root,
    )


def _default_read_checkpoint(context: DailyContext) -> Mapping[str, Any] | None:
    return _read_current_pointer(context.project_root)


def _default_sync_local(context: DailyContext) -> SnapshotOutcome:
    manifest, directory, created = build_snapshot(
        context.source_config,
        context.project_root,
        root_id="imf",
        source_root_override=context.source_root,
        update_pointer=False,
    )
    return SnapshotOutcome(manifest.snapshot_id, directory, created)


def _default_monitor_external(context: DailyContext) -> ExternalOutcome:
    from .external import (
        lookup_review_decision,
        run_external_search,
        validate_batch_integrity,
    )

    config_path = context.project_root / "config" / "external-sources.yaml"
    as_of = f"{context.date}T08:00:00+03:00"
    result = run_external_search(config_path, context.project_root, as_of)
    batch_relative = _safe_relative(str(result["batch_path"]), "external batch path")
    with open_regular_file_under_root(context.project_root, batch_relative) as descriptor:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    try:
        batch = strict_json_loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PublicationError("external batch is invalid JSON") from exc
    if not isinstance(batch, Mapping):
        raise PublicationError("external batch is not an object")
    validate_batch_integrity(batch)
    policy = context.external_config.get("policy")
    if not isinstance(policy, Mapping):
        raise PublicationError("external policy is unavailable after preflight")
    ledger_relative = _safe_relative(
        str(policy.get("decision_ledger")), "external decision ledger"
    )

    pending: list[str] = []
    approved: list[str] = []
    rejected: list[str] = []
    candidates = batch.get("candidates", [])
    if not isinstance(candidates, list):
        raise PublicationError("external batch candidates are malformed")
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise PublicationError("external candidate is malformed")
        candidate_id = candidate.get("id")
        candidate_sha256 = candidate.get("candidate_sha256")
        if not isinstance(candidate_id, str) or not isinstance(candidate_sha256, str):
            raise PublicationError("external candidate identity is malformed")
        decision = lookup_review_decision(
            context.project_root,
            ledger_relative,
            candidate_id,
            candidate_sha256,
        )
        if decision is None:
            pending.append(candidate_id)
        elif decision.get("decision") == "approved":
            approved.append(candidate_id)
        elif decision.get("decision") == "rejected":
            rejected.append(candidate_id)
        else:
            raise PublicationError("external review decision is malformed")
    return ExternalOutcome(
        pending_candidate_ids=tuple(sorted(pending)),
        approved_candidate_ids=tuple(sorted(approved)),
        rejected_candidate_ids=tuple(sorted(rejected)),
        batch_id=str(result.get("batch_id")) if result.get("batch_id") else None,
        review_path=batch_relative if pending else None,
    )


def _default_build_candidate(
    context: DailyContext, snapshot: SnapshotOutcome
) -> CandidateOutcome:
    result: ReleaseBuildResult = build_release_candidate(
        context.project_root,
        context.source_config,
        root_id="imf",
        snapshot_directory=snapshot.snapshot_directory,
    )
    return CandidateOutcome(
        release_id=result.release_id,
        release_directory=result.release_directory,
        created=result.created,
        status=result.status,
        semantic_changed=result.semantic_changed,
    )


def _basis_points(value: Any, label: str) -> int:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ConfigurationError(f"invalid {label}") from exc
    if not Decimal("0") <= number <= Decimal("1"):
        raise ConfigurationError(f"{label} must be between zero and one")
    return int((number * 10_000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _frontmatter(project_root: Path, relative_path: str) -> Mapping[str, Any]:
    try:
        with open_regular_file_under_root(project_root, relative_path) as descriptor:
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
        text = b"".join(chunks).decode("utf-8")
    except (OSError, UnicodeDecodeError, SnapshotError):
        return {}
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}
    try:
        value = yaml.safe_load(text[4:end])
    except yaml.YAMLError:
        return {}
    return value if isinstance(value, Mapping) else {}


def _prior_proposal_fingerprints(
    context: DailyContext, checkpoint: Mapping[str, Any] | None
) -> tuple[str, ...]:
    if checkpoint is None:
        return ()
    accepted = checkpoint.get("accepted_pulses", [])
    if not isinstance(accepted, list):
        return ()
    values: set[str] = set()
    accepted_publications = checkpoint.get("accepted_publications", [])
    release_by_pulse = (
        {
            item.get("pulse"): item.get("release_id")
            for item in accepted_publications
            if isinstance(item, Mapping)
            and isinstance(item.get("pulse"), str)
            and isinstance(item.get("release_id"), str)
        }
        if isinstance(accepted_publications, list)
        else {}
    )
    for relative in accepted:
        if not isinstance(relative, str):
            continue
        try:
            safe = _safe_relative(relative, "accepted pulse path")
        except DailyBlockedError:
            continue
        metadata = _frontmatter(context.project_root, safe)
        fingerprints = metadata.get("proposal_fingerprints", [])
        if isinstance(fingerprints, list):
            values.update(
                item
                for item in fingerprints
                if isinstance(item, str) and re.fullmatch(r"[0-9a-f]{64}", item)
            )
        # Current pulse schemas bind knowledge IDs rather than proposal hashes.
        # Reconstruct novelty identities from the immutable accepted release;
        # never trust a mutable review proposal for deduplication.
        knowledge_ids = metadata.get("knowledge_ids", [])
        release_id = release_by_pulse.get(safe)
        if (
            not isinstance(knowledge_ids, list)
            or not all(isinstance(item, str) for item in knowledge_ids)
            or not isinstance(release_id, str)
            or not RELEASE_ID_PATTERN.fullmatch(release_id)
        ):
            continue
        try:
            from .novelty import proposal_fingerprints_for_knowledge_ids

            reconstructed = proposal_fingerprints_for_knowledge_ids(
                context.project_root / "data" / "releases" / release_id,
                knowledge_ids,
            )
        except (PipelineError, OSError, ValueError):
            continue
        values.update(reconstructed)
    return tuple(sorted(values))


def _read_regular_bytes_at(directory_descriptor: int, name: str) -> bytes:
    if not name or "/" in name or name in {".", ".."}:
        raise PublicationError("unsafe immutable output name")
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=directory_descriptor,
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise PublicationError("immutable output is not a regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _install_immutable_bytes(
    project_root: Path,
    relative_path: str,
    payload: bytes,
    *,
    file_mode: int = 0o644,
) -> None:
    relative_path = _safe_relative(relative_path, "immutable output path")
    pure = PurePosixPath(relative_path)
    parent_relative = pure.parent.as_posix()
    if parent_relative == ".":
        raise PublicationError("immutable output must have a project child directory")
    temporary_name = f".{pure.name}.{uuid.uuid4().hex}.staged"
    with open_directory_under_root(
        project_root, parent_relative, create=True
    ) as directory_descriptor:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                file_mode,
                dir_fd=directory_descriptor,
            )
            offset = 0
            while offset < len(payload):
                offset += os.write(descriptor, payload[offset:])
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            try:
                os.link(
                    temporary_name,
                    pure.name,
                    src_dir_fd=directory_descriptor,
                    dst_dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
            except FileExistsError:
                if _read_regular_bytes_at(directory_descriptor, pure.name) != payload:
                    raise PublicationError(
                        f"immutable output collision: {pure.name}"
                    )
            os.fsync(directory_descriptor)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary_name, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass


def _write_immutable_json(
    project_root: Path, relative_path: str, value: Mapping[str, Any]
) -> None:
    payload = (
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        + "\n"
    ).encode("utf-8")
    _install_immutable_bytes(
        project_root, relative_path, payload, file_mode=0o600
    )


def _analysis_evidence_ids(analysis: Mapping[str, Any]) -> tuple[str, ...]:
    values: set[str] = set()
    analysis_id = analysis.get("id")
    if isinstance(analysis_id, str):
        values.add(analysis_id)
    ranked = analysis.get("ranked_candidates", [])
    if isinstance(ranked, list):
        for candidate in ranked:
            if not isinstance(candidate, Mapping):
                continue
            object_id = candidate.get("object_id")
            if isinstance(object_id, str):
                values.add(object_id)
            source_ids = candidate.get("source_ids", [])
            if isinstance(source_ids, list):
                values.update(item for item in source_ids if isinstance(item, str))
    return tuple(sorted(values))


def _default_analyze_candidate(
    context: DailyContext,
    checkpoint: Mapping[str, Any] | None,
    candidate: CandidateOutcome,
) -> AnalysisOutcome:
    if checkpoint is not None and checkpoint.get("release_id") == candidate.release_id:
        return AnalysisOutcome(
            status="no_update",
            reason="release content is unchanged",
            evidence_ids=(candidate.release_id,),
        )
    if checkpoint is None:
        return AnalysisOutcome(
            status="review_required",
            reason="an initial accepted release must be reviewed before daily novelty selection",
            evidence_ids=(candidate.release_id,),
            review_path=f"data/review/bootstrap/{candidate.release_id}.json",
        )

    from .novelty import NoveltyPolicy, analyze_release_changes

    novelty = context.pulse_config.get("novelty")
    if not isinstance(novelty, Mapping):
        raise ConfigurationError("pulse novelty policy is missing")
    policy = NoveltyPolicy(
        materiality_threshold_basis_points=_basis_points(
            novelty.get("materiality_threshold"), "materiality threshold"
        ),
        max_signals=int(context.pulse_constraints["maximum_signals"]),
        require_evidence=novelty.get("require_evidence") is True,
    )
    base_relative = _safe_relative(
        str(checkpoint.get("release_path")), "checkpoint release path"
    )
    base = context.project_root.joinpath(*PurePosixPath(base_relative).parts)
    analysis = analyze_release_changes(
        base,
        candidate.release_directory,
        policy=policy,
        prior_proposal_fingerprints=_prior_proposal_fingerprints(context, checkpoint),
    )
    analysis_id = analysis["id"]
    analysis_relative = f"data/review/change-analyses/{analysis_id}.json"
    _write_immutable_json(context.project_root, analysis_relative, analysis)
    status = str(analysis["status"])
    reasons = analysis.get("reason_codes", [])
    reason = ", ".join(str(item).replace("_", " ") for item in reasons)
    evidence_ids = _analysis_evidence_ids(analysis)
    if status == "review_required":
        return AnalysisOutcome(
            status=status,
            reason=reason or "the release comparison requires review",
            evidence_ids=evidence_ids,
            analysis=analysis,
            review_path=analysis_relative,
        )
    if status == "selected":
        proposal_relative = f"data/review/pulse-proposals/{context.date}.json"
        return AnalysisOutcome(
            status=status,
            reason=reason or "material evidence-backed changes were selected",
            evidence_ids=evidence_ids,
            analysis=analysis,
            review_path=proposal_relative,
        )
    return AnalysisOutcome(
        status="no_update",
        reason=reason or "no material evidence-backed development was selected",
        evidence_ids=evidence_ids,
        analysis=analysis,
    )


def _default_load_proposal(
    context: DailyContext,
    candidate: CandidateOutcome,
    analysis_outcome: AnalysisOutcome,
) -> Mapping[str, Any] | None:
    if analysis_outcome.analysis is None or analysis_outcome.review_path is None:
        return None
    proposal_path = context.project_root.joinpath(
        *PurePosixPath(_safe_relative(analysis_outcome.review_path, "proposal path")).parts
    )
    if not proposal_path.exists():
        return None
    from .pulse_builder import validate_proposal

    proposal_relative = proposal_path.relative_to(context.project_root).as_posix()
    with open_regular_file_under_root(
        context.project_root, proposal_relative
    ) as descriptor:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    try:
        proposal = strict_json_loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PublicationError("reviewed pulse proposal is invalid JSON") from exc
    if not isinstance(proposal, dict):
        raise PublicationError("reviewed pulse proposal must be an object")
    validate_proposal(
        proposal,
        context.project_root / "schemas" / "pulse-proposal.schema.json",
    )
    analysis = analysis_outcome.analysis
    expected = tuple(analysis.get("selected_candidate_fingerprints", []))
    supplied = tuple(proposal.get("proposal_fingerprints", []))
    checks = {
        "status": proposal.get("status") == "selected",
        "date": proposal.get("date") == context.date,
        "release": proposal.get("candidate_release_id") == candidate.release_id,
        "analysis id": proposal.get("analysis_id") == analysis.get("id"),
        "analysis fingerprint": proposal.get("analysis_fingerprint")
        == analysis.get("analysis_fingerprint"),
        "selected fingerprints": supplied == expected,
    }
    failed = [name for name, matches in checks.items() if not matches]
    if failed:
        raise PublicationError(
            "reviewed pulse proposal does not match the current analysis: "
            + ", ".join(failed)
        )
    return proposal


def _default_build_pulse(
    context: DailyContext, proposal: Mapping[str, Any]
) -> PulseOutcome:
    from .pulse_builder import render_pulse_markdown

    output_relative = f"content/pulses/{context.date}.md"
    output_path = context.project_root / output_relative
    proposal_schema = context.project_root / "schemas" / "pulse-proposal.schema.json"
    expected = render_pulse_markdown(proposal, schema_path=proposal_schema)
    del output_path
    _install_immutable_bytes(
        context.project_root, output_relative, expected.encode("utf-8")
    )
    manifest = proposal.get("artifact_manifest")
    if not isinstance(manifest, str):
        raise PublicationError("selected proposal has no artifact manifest")
    evidence = sorted(
        {
            item
            for key in ("source_ids", "knowledge_ids")
            for item in proposal.get(key, [])
            if isinstance(item, str)
        }
    )
    return PulseOutcome(output_relative, (manifest,), tuple(evidence))


def _quiet_gate_runner(
    command: Sequence[str], cwd: Path, environment: Mapping[str, str]
) -> None:
    try:
        subprocess.run(
            list(command),
            cwd=cwd,
            env=dict(environment),
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PublicationError(f"release gate failed: {' '.join(command)}") from exc


def _default_publish_candidate(
    context: DailyContext,
    candidate: CandidateOutcome,
    pulse: PulseOutcome | None,
) -> PublishResult:
    return publish_release(
        context.project_root,
        candidate.release_id,
        pulse=pulse.path if pulse else None,
        artifact_manifests=pulse.artifact_manifest_urls if pulse else (),
        gate_runner=_quiet_gate_runner,
    )


def default_dependencies() -> DailyDependencies:
    return DailyDependencies(
        load_context=_default_load_context,
        read_checkpoint=_default_read_checkpoint,
        sync_local=_default_sync_local,
        monitor_external=_default_monitor_external,
        build_candidate=_default_build_candidate,
        analyze_candidate=_default_analyze_candidate,
        load_proposal=_default_load_proposal,
        build_pulse=_default_build_pulse,
        publish_candidate=_default_publish_candidate,
    )


@contextmanager
def _daily_lock(project_root: Path) -> Iterable[None]:
    descriptor: int | None = None
    with open_directory_under_root(project_root, "data", create=True) as directory_descriptor:
        try:
            descriptor = os.open(
                ".daily-pipeline.lock",
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory_descriptor,
            )
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise DailyBlockedError("daily pipeline lock is not a regular file")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DailyBlockedError("another daily pipeline run is active") from exc
            yield
        finally:
            if descriptor is not None:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(descriptor)


def _dedupe(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(set(values)))


def _validate_pulse_outcome(context: DailyContext, pulse: PulseOutcome) -> None:
    expected_path = f"content/pulses/{context.date}.md"
    if pulse.path != expected_path:
        raise PublicationError("pulse output path does not match the run date")
    if len(pulse.artifact_manifest_urls) != 1:
        raise PublicationError("a pulse must bind exactly one artifact manifest")
    manifest = pulse.artifact_manifest_urls[0]
    if (
        not re.fullmatch(r"/artifacts/[A-Za-z0-9._~/-]+/manifest\.json", manifest)
        or ".." in PurePosixPath(manifest).parts
    ):
        raise PublicationError("pulse artifact manifest URL is unsafe")


def _safe_reason(exc: BaseException, project_root: Path) -> str:
    message = str(exc).replace("\r", " ").replace("\n", " ")
    replacements = {
        str(project_root): "<project>",
        str(Path.home()): "<home>",
    }
    source = os.environ.get("IMF_SOURCE_ROOT")
    if source:
        replacements[source] = "<source>"
    for original, replacement in sorted(
        replacements.items(), key=lambda item: len(item[0]), reverse=True
    ):
        if original:
            message = message.replace(original, replacement)
    message = " ".join(message.split())
    return (message or exc.__class__.__name__)[:1000]


def _validate_result(project_root: Path, result: DailyRunResult) -> None:
    if result.status not in RESULT_STATUSES:
        raise PublicationError("daily pipeline produced an invalid status")
    validate_records(
        [result.as_dict()],
        project_root / "schemas" / "daily-run-result.schema.json",
        "daily-run-result",
    )


def _result(
    *,
    status: str,
    run_date: str,
    run_id: str,
    release_id: str | None,
    reason: str,
    pulse_path: str | None = None,
    artifact_urls: Iterable[str] = (),
    release_advanced: bool = False,
    checkpoint_refreshed: bool = False,
    evidence_ids: Iterable[str] = (),
    pending_review_count: int = 0,
    pending_review_path: str | None = None,
) -> DailyRunResult:
    return DailyRunResult(
        status=status,
        date=run_date,
        run_id=run_id,
        release_id=release_id,
        pulse_path=pulse_path,
        artifact_urls=_dedupe(artifact_urls),
        release_advanced=release_advanced,
        checkpoint_refreshed=checkpoint_refreshed,
        reason=reason,
        evidence_ids=_dedupe(evidence_ids),
        pending_review_count=pending_review_count,
        pending_review_path=pending_review_path,
    )


def run_daily_pipeline(
    project_root: Path,
    *,
    mode: str,
    run_date: str,
    dependencies: DailyDependencies | None = None,
) -> DailyRunResult:
    """Run at most one report transaction and always return a safe result."""

    dependencies = dependencies or default_dependencies()
    provisional_run_id = f"daily-{uuid.uuid4().hex}"
    candidate: CandidateOutcome | None = None
    normalized_root = project_root.resolve()
    try:
        context = dependencies.load_context(project_root, mode, run_date)
        with _daily_lock(context.project_root):
            checkpoint = dependencies.read_checkpoint(context)
            snapshot = dependencies.sync_local(context)
            external = dependencies.monitor_external(context)
            if external.pending_candidate_ids:
                result = _result(
                    status="review_required",
                    run_date=context.date,
                    run_id=provisional_run_id,
                    release_id=(
                        str(checkpoint.get("release_id")) if checkpoint else None
                    ),
                    reason="external metadata candidates require exact-hash review",
                    evidence_ids=external.pending_candidate_ids,
                    pending_review_count=len(external.pending_candidate_ids),
                    pending_review_path=external.review_path,
                )
                _validate_result(context.project_root, result)
                return result

            candidate = dependencies.build_candidate(context, snapshot)
            analysis = dependencies.analyze_candidate(context, checkpoint, candidate)
            external_evidence = external.approved_candidate_ids
            if analysis.status == "review_required":
                result = _result(
                    status="review_required",
                    run_date=context.date,
                    run_id=provisional_run_id,
                    release_id=candidate.release_id,
                    reason=analysis.reason,
                    evidence_ids=(*analysis.evidence_ids, *external_evidence),
                    pending_review_count=1,
                    pending_review_path=analysis.review_path,
                )
                _validate_result(context.project_root, result)
                return result

            pulse: PulseOutcome | None = None
            if analysis.status == "selected":
                proposal = dependencies.load_proposal(context, candidate, analysis)
                if proposal is None:
                    result = _result(
                        status="review_required",
                        run_date=context.date,
                        run_id=provisional_run_id,
                        release_id=candidate.release_id,
                        reason=(
                            "material changes were selected, but their hash-bound pulse "
                            "proposal has not been reviewed"
                        ),
                        evidence_ids=(*analysis.evidence_ids, *external_evidence),
                        pending_review_count=1,
                        pending_review_path=analysis.review_path,
                    )
                    _validate_result(context.project_root, result)
                    return result
                pulse = dependencies.build_pulse(context, proposal)
                _validate_pulse_outcome(context, pulse)
            elif analysis.status != "no_update":
                raise PublicationError("novelty analysis returned an invalid status")

            published = dependencies.publish_candidate(context, candidate, pulse)
            if pulse is not None:
                if published.status != "published" or not published.pointer_changed:
                    raise PublicationError("pulse publication did not advance the checkpoint")
                result = _result(
                    status="published",
                    run_date=context.date,
                    run_id=published.run_id,
                    release_id=published.release_id,
                    pulse_path=pulse.path,
                    artifact_urls=pulse.artifact_manifest_urls,
                    release_advanced=True,
                    checkpoint_refreshed=True,
                    reason="one reviewed, evidence-backed pulse passed every release gate",
                    evidence_ids=(
                        *analysis.evidence_ids,
                        *pulse.evidence_ids,
                        *external.approved_candidate_ids,
                    ),
                )
            else:
                if published.status not in {"unchanged", "processed_no_pulse"}:
                    raise PublicationError("no-update publication returned an invalid status")
                advanced = bool(published.pointer_changed)
                reason = (
                    "the evidence release advanced without a pulse because no material "
                    "development was selected"
                    if advanced
                    else "no material development was selected; the accepted report was retained"
                )
                result = _result(
                    status="no_update",
                    run_date=context.date,
                    run_id=published.run_id,
                    release_id=published.release_id,
                    release_advanced=advanced,
                    checkpoint_refreshed=True,
                    reason=reason,
                    evidence_ids=(
                        *analysis.evidence_ids,
                        *external.approved_candidate_ids,
                    ),
                )
            _validate_result(context.project_root, result)
            return result
    except (DailyBlockedError, ConfigurationError, SnapshotError) as exc:
        result = _result(
            status="blocked",
            run_date=run_date if re.fullmatch(r"\d{4}-\d{2}-\d{2}", run_date) else "1970-01-01",
            run_id=provisional_run_id,
            release_id=candidate.release_id if candidate else None,
            reason=_safe_reason(exc, normalized_root),
        )
    except BaseException as exc:
        result = _result(
            status="failed",
            run_date=run_date if re.fullmatch(r"\d{4}-\d{2}-\d{2}", run_date) else "1970-01-01",
            run_id=provisional_run_id,
            release_id=candidate.release_id if candidate else None,
            reason=_safe_reason(exc, normalized_root),
        )
    try:
        schema_root = normalized_root if normalized_root.is_dir() else Path(__file__).resolve().parents[1]
        _validate_result(schema_root, result)
    except BaseException:
        # The CLI must still emit one finite, non-sensitive object even when a
        # malformed project cannot supply its local schema.
        pass
    return result
