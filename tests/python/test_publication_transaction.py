from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from conftest import make_config, make_source, materialize_test_site
from research_pipeline.errors import PublicationError, SnapshotError
from research_pipeline.artifacts import (
    bind_publication_inputs,
    verify_bound_publication,
    verify_source_publication_inputs,
)
from research_pipeline.config import load_pipeline_config
from research_pipeline.hashing import canonical_json_hash
from research_pipeline.pulse_validation import parse_pulse
from research_pipeline.release import build_release_candidate, publish_release
from research_pipeline.snapshot import build_snapshot
from research_pipeline.validation import read_json, validate_release_directory


def _prepare_candidate(
    project_root: Path,
    source_root: Path,
    empty_knowledge: Path,
    schemas_directory: Path,
    text: str,
):
    source_root.mkdir(exist_ok=True)
    project_root.mkdir(exist_ok=True)
    (source_root / "README.md").write_text(text, encoding="utf-8")
    config = make_config(project_root, source_root, (make_source(),))
    _, snapshot_directory, _ = build_snapshot(config, project_root)
    candidate = build_release_candidate(
        project_root,
        config,
        snapshot_directory=snapshot_directory,
        knowledge_directory=empty_knowledge,
        schemas_directory=schemas_directory,
    )
    return config, candidate


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_publication(
    project_root: Path,
    candidate_directory: Path,
    *,
    date: str = "2026-01-02",
    pulse_index: int | None = None,
) -> tuple[str, str, Path]:
    config_directory = project_root / "config"
    config_directory.mkdir(exist_ok=True)
    (config_directory / "pulse.yaml").write_text(
        """version: 1
report:
  word_count: {minimum: 350, maximum: 650}
  max_signals: 3
  required_sections: [lead, signals, why-this-matters, unresolved-question, sources]
  require_meaningful_artifact: true
""",
        encoding="utf-8",
    )
    index_suffix = f"-{pulse_index}" if pulse_index is not None else ""
    artifact_id = f"artifact-{date}{index_suffix}"
    artifact_url_root = f"/artifacts/{date}/test-chart{index_suffix}"
    artifact_directory = project_root / "public" / artifact_url_root.removeprefix("/")
    artifact_directory.mkdir(parents=True)
    payloads = {
        "chart.csv": b"x,y\n1,2\n",
        "chart.spec.json": b'{"data":[1]}\n',
        "chart.svg": b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"></svg>\n',
    }
    media = {
        "chart.csv": "text/csv",
        "chart.spec.json": "application/json",
        "chart.svg": "image/svg+xml",
    }
    for filename, payload in payloads.items():
        (artifact_directory / filename).write_bytes(payload)
    scripts_directory = project_root / "scripts"
    scripts_directory.mkdir(exist_ok=True)
    generator = b"# deterministic fixture; source content is never executed\n"
    (scripts_directory / "generate_test.py").write_bytes(generator)
    source = json.loads((candidate_directory / "sources.jsonl").read_text(encoding="utf-8"))
    files = [
        {
            "url": f"{artifact_url_root}/{filename}",
            "role": "test companion",
            "media_type": media[filename],
            "sha256": _sha(payload),
            "bytes": len(payload),
        }
        for filename, payload in payloads.items()
    ]
    manifest_url = f"{artifact_url_root}/manifest.json"
    manifest = {
        "schema_version": "1.0.0",
        "artifact_id": artifact_id,
        "artifact_type": "scientific_chart",
        "title": "Deterministic fixture chart",
        "caption": "A deterministic publication transaction fixture.",
        "stable_url": f"{artifact_url_root}/chart.svg",
        "manifest_url": manifest_url,
        "spec_url": f"{artifact_url_root}/chart.spec.json",
        "rights": {
            "status": "project_generated",
            "may_publish_publicly": False,
            "local_display_allowed": True,
            "public_deployment_requires_owner_approval": True,
        },
        "files": files,
        "generator": {
            "deterministic": True,
            "path": "scripts/generate_test.py",
            "sha256": _sha(generator),
            "version": "1.0.0",
            "source_files_executed": False,
        },
        "sources": [
            {
                "source_id": "src-test",
                "content_sha256": source["content_sha256"],
                "relative_path": "README.md",
                "role": "fixture evidence",
                "execution_status": "read as text only",
                "rights_status": "internal test fixture",
                "locators": [
                    {
                        "kind": "file_lines",
                        "path": "README.md",
                        "line_start": 1,
                        "line_end": 1,
                    }
                ],
            }
        ],
        "evidence": [
            {
                "source_id": "src-test",
                "statement": "The fixture source was statically extracted.",
                "status": "observed",
                "confidence": "high",
                "locator": {
                    "kind": "file_lines",
                    "path": "README.md",
                    "line_start": 1,
                    "line_end": 1,
                },
            }
        ],
    }
    (artifact_directory / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
    )
    pulse_directory = project_root / "content" / "pulses"
    pulse_directory.mkdir(parents=True, exist_ok=True)
    filler = " ".join(["measurement"] * 360)
    pulse_id = f"pulse-{date}{index_suffix}"
    pulse_index_line = f"pulse_index: {pulse_index}\n" if pulse_index is not None else ""
    pulse = f"""---
schema_version: "1.0.0"
id: {pulse_id}
date: {date}
{pulse_index_line}title: "A Bound Test Signal"
lead: "One exact-byte fixture exercises the publication boundary."
status: published
topics: [imf]
featured_artifact: {artifact_id}
artifact_manifests: [{manifest_url}]
source_ids: [src-test]
---

## Signal 01 — Exact bytes before claims

What changed: a candidate acquired a sealed report and deterministic chart. Why it matters: gates now inspect the selected report. Evidence and confidence are explicit. {filler} [Evidence](/sources#src-test).

## Why this matters

The checkpoint can name only bytes that survived every configured gate.

## Unresolved question

Can an injected mutation cross the post-gate integrity check?

## Sources

- [Synthetic static source](/sources#src-test)
"""
    pulse_relative = f"content/pulses/{date}{index_suffix}.md"
    (project_root / pulse_relative).write_text(pulse, encoding="utf-8")
    return pulse_relative, manifest_url, artifact_directory / "chart.svg"


def _ok_gate(command, cwd, environment) -> None:
    materialize_test_site(command, environment)


def test_publish_binds_artifacts_exports_final_context_and_finalizes_run(
    tmp_path: Path, empty_knowledge: Path, schemas_directory: Path
) -> None:
    project = tmp_path / "project"
    source = tmp_path / "source"
    _, candidate = _prepare_candidate(
        project, source, empty_knowledge, schemas_directory, "publication evidence"
    )
    pulse, manifest_url, _ = _write_publication(project, candidate.release_directory)
    observed: list[dict[str, str]] = []

    def gate(command, cwd, environment):
        assert not (project / "data" / "current.json").exists()
        observed.append(dict(environment))
        materialize_test_site(command, environment)

    result = publish_release(
        project,
        candidate.release_id,
        schemas_directory=schemas_directory,
        pulse=pulse,
        artifact_manifests=(manifest_url,),
        gate_runner=gate,
    )
    assert result.status == "published"
    assert len(observed) == 3
    for environment in observed:
        assert environment["IMF_PULSE_SELECTED_PULSE"] == pulse
        assert environment["IMF_PULSE_CHECKPOINT_STATUS"] == "published"
        assert Path(environment["IMF_PULSE_BUILD_OUT_DIR"]).is_absolute()
        assert json.loads(environment["IMF_PULSE_ARTIFACT_MANIFESTS"]) == [manifest_url]
        accepted = json.loads(environment["IMF_PULSE_ACCEPTED_PUBLICATIONS"])
        assert accepted[0]["pulse"] == pulse
        assert accepted[0]["bound_pulse"].startswith(
            f"data/releases/{candidate.release_id}/publication/"
        )
    pointer = read_json(project / "data" / "current.json")
    assert pointer["latest_accepted_pulse"] == pulse
    assert pointer["accepted_pulses"] == [pulse]
    assert pointer["accepted_artifact_manifests"] == [manifest_url]
    assert pointer["accepted_publications"][0]["pulse_sha256"]
    assert pointer["site_build_path"] == (
        f"data/site-builds/site-{pointer['site_build_sha256']}"
    )
    assert pointer["accepted_publications_sha256"] == canonical_json_hash(
        pointer["accepted_publications"]
    )
    assert read_json(candidate.release_directory / "release.json")[
        "accepted_publications_sha256"
    ] == pointer["accepted_publications_sha256"]
    run = read_json(project / "data" / "runs" / f"{result.run_id}.json")
    assert run["status"] == "published"
    assert run["pointer_changed"] is True
    validate_release_directory(candidate.release_directory, schemas_directory)


def test_pointer_failure_leaves_ready_record_and_exact_binding_is_retryable(
    tmp_path: Path,
    empty_knowledge: Path,
    schemas_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import research_pipeline.release as release_module

    project = tmp_path / "project"
    source = tmp_path / "source"
    _, candidate = _prepare_candidate(
        project, source, empty_knowledge, schemas_directory, "pointer failure"
    )
    pulse, manifest_url, _ = _write_publication(project, candidate.release_directory)
    original_atomic_write = release_module.atomic_write_json
    attempted_pointers: list[dict] = []

    def fail_pointer(path, value):
        if path == project / "data" / "current.json":
            attempted_pointers.append(dict(value))
            raise OSError("injected pointer failure")
        return original_atomic_write(path, value)

    monkeypatch.setattr(release_module, "atomic_write_json", fail_pointer)
    with pytest.raises(OSError, match="pointer failure"):
        publish_release(
            project,
            candidate.release_id,
            schemas_directory=schemas_directory,
            pulse=pulse,
            artifact_manifests=(manifest_url,),
            gate_runner=_ok_gate,
        )
    assert not (project / "data" / "current.json").exists()
    assert attempted_pointers[0]["site_build_path"] == (
        f"data/site-builds/site-{attempted_pointers[0]['site_build_sha256']}"
    )
    assert (project / attempted_pointers[0]["site_build_path"]).is_dir()
    assert not list((project / "data" / ".site-staging").iterdir())
    ready = [read_json(path) for path in (project / "data" / "runs").glob("*.json")]
    assert [record["status"] for record in ready] == ["ready_to_publish"]
    assert (candidate.release_directory / "publication").is_dir()

    monkeypatch.setattr(release_module, "atomic_write_json", original_atomic_write)
    result = publish_release(
        project,
        candidate.release_id,
        schemas_directory=schemas_directory,
        pulse=pulse,
        artifact_manifests=(manifest_url,),
        gate_runner=_ok_gate,
    )
    assert result.status == "published"


def test_incremental_ingestion_reuses_unchanged_extract(
    tmp_path: Path,
    empty_knowledge: Path,
    schemas_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import research_pipeline.release as release_module

    project = tmp_path / "project"
    source = tmp_path / "source"
    project.mkdir()
    source.mkdir()
    (source / "a.md").write_text("unchanged", encoding="utf-8")
    (source / "b.md").write_text("first", encoding="utf-8")
    config = make_config(
        project,
        source,
        (
            make_source("a.md", source_id="src-a"),
            make_source("b.md", source_id="src-b"),
        ),
    )
    _, snapshot_directory, _ = build_snapshot(config, project)
    first = build_release_candidate(
        project,
        config,
        snapshot_directory=snapshot_directory,
        knowledge_directory=empty_knowledge,
        schemas_directory=schemas_directory,
    )
    publish_release(
        project,
        first.release_id,
        schemas_directory=schemas_directory,
        gate_runner=_ok_gate,
    )
    (source / "b.md").write_text("second", encoding="utf-8")
    _, second_snapshot, _ = build_snapshot(config, project)
    original_extract = release_module.extract_source
    extracted: list[str] = []

    def observe(path, source_config, source_sha256):
        extracted.append(source_config.id)
        return original_extract(path, source_config, source_sha256)

    monkeypatch.setattr(release_module, "extract_source", observe)
    build_release_candidate(
        project,
        config,
        snapshot_directory=second_snapshot,
        knowledge_directory=empty_knowledge,
        schemas_directory=schemas_directory,
    )
    assert extracted == ["src-b"]


def test_snapshot_mutation_between_verification_and_extraction_is_rejected(
    tmp_path: Path,
    empty_knowledge: Path,
    schemas_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import research_pipeline.release as release_module

    project = tmp_path / "project"
    source = tmp_path / "source"
    project.mkdir()
    source.mkdir()
    (source / "README.md").write_text("verified snapshot bytes", encoding="utf-8")
    config = make_config(project, source, (make_source(),))
    _, snapshot_directory, _ = build_snapshot(config, project)
    snapshot_file = snapshot_directory / "files" / "README.md"
    original_open = release_module.open_regular_file_under_root
    mutated = False

    @contextmanager
    def mutate_before_open(root, relative_path):
        nonlocal mutated
        if not mutated and relative_path.endswith("/files/README.md"):
            snapshot_file.write_text("different bytes after validation", encoding="utf-8")
            mutated = True
        with original_open(root, relative_path) as descriptor:
            yield descriptor

    monkeypatch.setattr(
        release_module, "open_regular_file_under_root", mutate_before_open
    )
    with pytest.raises(PublicationError, match="snapshot bytes changed after verification"):
        build_release_candidate(
            project,
            config,
            snapshot_directory=snapshot_directory,
            knowledge_directory=empty_knowledge,
            schemas_directory=schemas_directory,
        )
    assert not (project / "data" / "current.json").exists()


def test_incremental_ingestion_reextracts_same_bytes_after_source_path_move(
    tmp_path: Path,
    empty_knowledge: Path,
    schemas_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import research_pipeline.release as release_module

    project = tmp_path / "project"
    source = tmp_path / "source"
    project.mkdir()
    source.mkdir()
    payload = "same bytes, new provenance"
    (source / "old.md").write_text(payload, encoding="utf-8")
    first_config = make_config(
        project, source, (make_source("old.md", source_id="src-moved"),)
    )
    _, first_snapshot, _ = build_snapshot(first_config, project)
    first = build_release_candidate(
        project,
        first_config,
        snapshot_directory=first_snapshot,
        knowledge_directory=empty_knowledge,
        schemas_directory=schemas_directory,
    )
    publish_release(
        project,
        first.release_id,
        schemas_directory=schemas_directory,
        gate_runner=_ok_gate,
    )

    (source / "old.md").rename(source / "new.md")
    second_config = make_config(
        project, source, (make_source("new.md", source_id="src-moved"),)
    )
    _, second_snapshot, _ = build_snapshot(second_config, project)
    original_extract = release_module.extract_source
    extracted: list[str] = []

    def observe(path, source_config, source_sha256):
        extracted.append(source_config.id)
        return original_extract(path, source_config, source_sha256)

    monkeypatch.setattr(release_module, "extract_source", observe)
    second = build_release_candidate(
        project,
        second_config,
        snapshot_directory=second_snapshot,
        knowledge_directory=empty_knowledge,
        schemas_directory=schemas_directory,
    )
    assert extracted == ["src-moved"]
    source_record = read_json(second.release_directory / "sources.jsonl")
    assert source_record["path"] == "new.md"
    assert source_record["location"] == "repo://imf/new.md"
    extract_record = read_json(
        second.release_directory / "extracts" / "src-moved.jsonl"
    )
    assert extract_record["locator"]["path"] == "new.md"


def test_manifest_hash_mismatch_fails_before_gates(
    tmp_path: Path, empty_knowledge: Path, schemas_directory: Path
) -> None:
    project = tmp_path / "project"
    source = tmp_path / "source"
    _, candidate = _prepare_candidate(
        project, source, empty_knowledge, schemas_directory, "bad artifact hash"
    )
    pulse, manifest_url, _ = _write_publication(project, candidate.release_directory)
    manifest_path = project / "public" / manifest_url.removeprefix("/")
    manifest = read_json(manifest_path)
    manifest["files"][0]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    gate_called = False

    def gate(command, cwd, environment):
        nonlocal gate_called
        gate_called = True

    with pytest.raises(PublicationError, match="hash/size mismatch"):
        publish_release(
            project,
            candidate.release_id,
            schemas_directory=schemas_directory,
            pulse=pulse,
            artifact_manifests=(manifest_url,),
            gate_runner=gate,
        )
    assert gate_called is False
    assert not (project / "data" / "current.json").exists()


def test_current_release_rejects_a_different_pulse(
    tmp_path: Path, empty_knowledge: Path, schemas_directory: Path
) -> None:
    project = tmp_path / "project"
    source = tmp_path / "source"
    _, candidate = _prepare_candidate(
        project, source, empty_knowledge, schemas_directory, "accepted once"
    )
    pulse, manifest_url, _ = _write_publication(project, candidate.release_directory)
    publish_release(
        project,
        candidate.release_id,
        schemas_directory=schemas_directory,
        pulse=pulse,
        artifact_manifests=(manifest_url,),
        gate_runner=_ok_gate,
    )
    with pytest.raises(PublicationError, match="different publication inputs"):
        publish_release(
            project,
            candidate.release_id,
            schemas_directory=schemas_directory,
            pulse="content/pulses/2026-01-03.md",
            gate_runner=_ok_gate,
        )


def test_orphan_publication_directory_is_recovered_before_validation(
    tmp_path: Path, empty_knowledge: Path, schemas_directory: Path
) -> None:
    project = tmp_path / "project"
    source = tmp_path / "source"
    _, candidate = _prepare_candidate(
        project, source, empty_knowledge, schemas_directory, "orphan recovery"
    )
    orphan = candidate.release_directory / "publication"
    orphan.mkdir()
    (orphan / "partial.tmp").write_text("crash residue", encoding="utf-8")
    result = publish_release(
        project,
        candidate.release_id,
        schemas_directory=schemas_directory,
        gate_runner=_ok_gate,
    )
    assert result.status == "processed_no_pulse"
    assert not orphan.exists()
    validate_release_directory(candidate.release_directory, schemas_directory)


def test_post_commit_run_finalization_failure_does_not_misreport_rollback(
    tmp_path: Path,
    empty_knowledge: Path,
    schemas_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import research_pipeline.release as release_module

    project = tmp_path / "project"
    source = tmp_path / "source"
    _, candidate = _prepare_candidate(
        project, source, empty_knowledge, schemas_directory, "audit finalization"
    )

    def fail_finalization(*args, **kwargs):
        raise OSError("injected audit finalization failure")

    monkeypatch.setattr(release_module, "_replace_run_record", fail_finalization)
    result = publish_release(
        project,
        candidate.release_id,
        schemas_directory=schemas_directory,
        gate_runner=_ok_gate,
    )
    assert result.status == "processed_no_pulse"
    assert read_json(project / "data" / "current.json")["release_id"] == candidate.release_id
    run = read_json(project / "data" / "runs" / f"{result.run_id}.json")
    assert run["status"] == "ready_to_publish"


def test_ready_record_parent_swap_cannot_redirect_checkpoint_write(
    tmp_path: Path,
    empty_knowledge: Path,
    schemas_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import research_pipeline.release as release_module

    project = tmp_path / "project"
    source = tmp_path / "source"
    _, candidate = _prepare_candidate(
        project, source, empty_knowledge, schemas_directory, "parent swap"
    )
    data = project / "data"
    saved_data = project / "data-before-swap"
    outside = tmp_path / "outside-data"
    outside.mkdir()
    sentinel = outside / "current.json"
    sentinel.write_text("external checkpoint\n", encoding="utf-8")
    original_write_run = release_module._write_run_record
    swapped = False

    def write_ready_then_swap(*args, **kwargs):
        nonlocal swapped
        original_write_run(*args, **kwargs)
        value = args[2]
        if value.get("status") == "ready_to_publish" and not swapped:
            data.rename(saved_data)
            data.symlink_to(outside, target_is_directory=True)
            swapped = True

    monkeypatch.setattr(release_module, "_write_run_record", write_ready_then_swap)
    try:
        with pytest.raises(SnapshotError, match="ancestor.*unsafe"):
            publish_release(
                project,
                candidate.release_id,
                schemas_directory=schemas_directory,
                gate_runner=_ok_gate,
            )
        assert sentinel.read_text(encoding="utf-8") == "external checkpoint\n"
        ready = [read_json(path) for path in (saved_data / "runs").glob("*.json")]
        assert [record["status"] for record in ready] == ["ready_to_publish"]
        assert ready[0]["pointer_changed"] is False
    finally:
        if data.is_symlink():
            data.unlink()
        if saved_data.exists():
            saved_data.rename(data)


def test_symlinked_release_cannot_trigger_external_orphan_deletion(
    tmp_path: Path, schemas_directory: Path
) -> None:
    project = tmp_path / "project"
    releases = project / "data" / "releases"
    releases.mkdir(parents=True)
    outside = tmp_path / "outside-release"
    victim = outside / "publication" / "keep.txt"
    victim.parent.mkdir(parents=True)
    victim.write_text("must survive", encoding="utf-8")
    (outside / "release.json").write_text('{"publication":null}\n', encoding="utf-8")
    release_id = "release-00000000000000000000"
    (releases / release_id).symlink_to(outside, target_is_directory=True)

    with pytest.raises(PublicationError, match="non-symlink directory"):
        publish_release(
            project,
            release_id,
            schemas_directory=schemas_directory,
            gate_runner=_ok_gate,
        )
    assert victim.read_text(encoding="utf-8") == "must survive"


def test_gate_swap_cleanup_preserves_original_error_pointer_and_external_files(
    tmp_path: Path, empty_knowledge: Path, schemas_directory: Path
) -> None:
    project = tmp_path / "project"
    source = tmp_path / "source"
    config, first = _prepare_candidate(
        project, source, empty_knowledge, schemas_directory, "safe checkpoint"
    )
    publish_release(
        project,
        first.release_id,
        schemas_directory=schemas_directory,
        gate_runner=_ok_gate,
    )
    checkpoint = (project / "data" / "current.json").read_bytes()
    (source / "README.md").write_text("candidate to swap", encoding="utf-8")
    _, snapshot_directory, _ = build_snapshot(config, project)
    second = build_release_candidate(
        project,
        config,
        snapshot_directory=snapshot_directory,
        knowledge_directory=empty_knowledge,
        schemas_directory=schemas_directory,
    )
    pulse, manifest_url, _ = _write_publication(project, second.release_directory)
    outside = tmp_path / "outside"
    sentinel = outside / "publication" / "keep.txt"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("external", encoding="utf-8")
    backup = second.release_directory.parent / f".{second.release_id}-backup"
    swapped = False

    def swap_then_fail(command, cwd, environment):
        nonlocal swapped
        if not swapped:
            second.release_directory.rename(backup)
            second.release_directory.symlink_to(outside, target_is_directory=True)
            swapped = True
            raise PublicationError("injected gate swap")

    try:
        with pytest.raises(PublicationError, match="injected gate swap"):
            publish_release(
                project,
                second.release_id,
                schemas_directory=schemas_directory,
                pulse=pulse,
                artifact_manifests=(manifest_url,),
                gate_runner=swap_then_fail,
            )
        assert sentinel.read_text(encoding="utf-8") == "external"
        assert (project / "data" / "current.json").read_bytes() == checkpoint
        failed = [
            read_json(path)
            for path in (project / "data" / "runs").glob("*.json")
            if read_json(path)["status"] == "failed"
        ]
        assert failed and "cleanup was safely refused" in failed[-1]["warnings"][0]
    finally:
        if second.release_directory.is_symlink():
            second.release_directory.unlink()
        if backup.exists():
            backup.rename(second.release_directory)


def test_checked_in_pulse_and_manifest_form_a_valid_exact_byte_binding(
    tmp_path: Path, repository_root: Path, schemas_directory: Path
) -> None:
    pulse_path = repository_root / "content" / "pulses" / "2026-07-22.md"
    frontmatter, _ = parse_pulse(pulse_path)
    manifest_url = frontmatter["artifact_manifests"][0]
    manifest = read_json(repository_root / "public" / manifest_url.removeprefix("/"))
    configured = {
        source.id: source
        for source in load_pipeline_config(repository_root / "config" / "sources.yaml").sources
    }
    version_by_source = {
        item["source_id"]: (item["content_sha256"], item["relative_path"])
        for item in manifest["sources"]
    }
    sources = []
    extracts = []
    referenced_source_ids = list(
        dict.fromkeys(
            [*frontmatter["source_ids"], *(item["source_id"] for item in manifest["sources"])]
        )
    )
    for source_id in referenced_source_ids:
        configured_source = configured[source_id]
        digest, path = version_by_source.get(
            source_id, ("a" * 64, configured_source.path)
        )
        sources.append(
            {
                "id": source_id,
                "path": path,
                "content_sha256": digest,
                "version_history": [],
            }
        )
        if source_id not in version_by_source:
            continue
        locator = (
            {
                "kind": "table",
                "path": path,
                "header_row": 1,
                "row_start": 2,
                "row_end": 100,
            }
            if path.endswith(".csv")
            else {
                "kind": "file_lines",
                "path": path,
                "line_start": 1,
                "line_end": 1000,
            }
        )
        extracts.append(
            {
                "id": f"extract-{source_id}",
                "source_id": source_id,
                "source_sha256": digest,
                "locator": locator,
            }
        )
    release_directory = tmp_path / "release"
    release_directory.mkdir()
    binding = bind_publication_inputs(
        repository_root,
        release_directory,
        schemas_directory,
        {"sources.jsonl": sources, "extracts": extracts},
        pulse="content/pulses/2026-07-22.md",
        artifact_manifests=(manifest_url,),
    )
    assert binding.metadata is not None
    verify_bound_publication(release_directory, schemas_directory, binding.metadata)
    verify_source_publication_inputs(repository_root, binding.metadata)
    assert (
        release_directory
        / "publication"
        / "scripts"
        / "generate_stage_error_artifact.py"
    ).is_file()


def test_publish_lock_cleanup_cannot_follow_a_swapped_data_parent(tmp_path: Path) -> None:
    from research_pipeline.release import _exclusive_publish_lock

    project = tmp_path / "project"
    data = project / "data"
    data.mkdir(parents=True)
    backup = project / "data-backup"
    outside = tmp_path / "external-data"
    outside.mkdir()
    sentinel = outside / ".pipeline.lock"
    sentinel.write_text("external sentinel", encoding="utf-8")
    with _exclusive_publish_lock(project):
        data.rename(backup)
        data.symlink_to(outside, target_is_directory=True)
    try:
        assert sentinel.read_text(encoding="utf-8") == "external sentinel"
        assert not (backup / ".pipeline.lock").exists()
    finally:
        if data.is_symlink():
            data.unlink()
        if backup.exists():
            backup.rename(data)


def test_no_update_rejects_corrupt_accepted_history_without_pointer_change(
    tmp_path: Path, empty_knowledge: Path, schemas_directory: Path
) -> None:
    project = tmp_path / "project"
    source = tmp_path / "source"
    _, candidate = _prepare_candidate(
        project, source, empty_knowledge, schemas_directory, "history integrity"
    )
    pulse, manifest_url, _ = _write_publication(project, candidate.release_directory)
    publish_release(
        project,
        candidate.release_id,
        schemas_directory=schemas_directory,
        pulse=pulse,
        artifact_manifests=(manifest_url,),
        gate_runner=_ok_gate,
    )
    pointer_path = project / "data" / "current.json"
    pointer = read_json(pointer_path)
    pointer["accepted_publications"][0]["pulse_sha256"] = "0" * 64
    pointer_path.write_text(json.dumps(pointer) + "\n", encoding="utf-8")
    corrupted = pointer_path.read_bytes()
    with pytest.raises(PublicationError, match="history does not match"):
        publish_release(
            project,
            candidate.release_id,
            schemas_directory=schemas_directory,
            gate_runner=_ok_gate,
        )
    assert pointer_path.read_bytes() == corrupted


def test_data_parent_swap_does_not_mask_gate_error_or_touch_external_files(
    tmp_path: Path, empty_knowledge: Path, schemas_directory: Path
) -> None:
    project = tmp_path / "project"
    source = tmp_path / "source"
    config, first = _prepare_candidate(
        project, source, empty_knowledge, schemas_directory, "first"
    )
    publish_release(
        project,
        first.release_id,
        schemas_directory=schemas_directory,
        gate_runner=_ok_gate,
    )
    checkpoint = (project / "data" / "current.json").read_bytes()
    (source / "README.md").write_text("second", encoding="utf-8")
    _, snapshot_directory, _ = build_snapshot(config, project)
    second = build_release_candidate(
        project,
        config,
        snapshot_directory=snapshot_directory,
        knowledge_directory=empty_knowledge,
        schemas_directory=schemas_directory,
    )
    pulse, manifest_url, _ = _write_publication(project, second.release_directory)
    data = project / "data"
    backup = project / "data-original"
    outside = tmp_path / "outside-data"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("external", encoding="utf-8")
    swapped = False

    def swap_data_then_fail(command, cwd, environment):
        nonlocal swapped
        if not swapped:
            data.rename(backup)
            data.symlink_to(outside, target_is_directory=True)
            swapped = True
            raise PublicationError("injected data-parent swap")

    try:
        with pytest.raises(PublicationError, match="injected data-parent swap"):
            publish_release(
                project,
                second.release_id,
                schemas_directory=schemas_directory,
                pulse=pulse,
                artifact_manifests=(manifest_url,),
                gate_runner=swap_data_then_fail,
            )
        assert sentinel.read_text(encoding="utf-8") == "external"
        assert (backup / "current.json").read_bytes() == checkpoint
    finally:
        if data.is_symlink():
            data.unlink()
        if backup.exists():
            backup.rename(data)


def test_release_path_substitution_before_binding_cannot_write_external_tree(
    tmp_path: Path,
    empty_knowledge: Path,
    schemas_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import research_pipeline.artifacts as artifacts_module

    project = tmp_path / "project"
    source = tmp_path / "source"
    _, candidate = _prepare_candidate(
        project, source, empty_knowledge, schemas_directory, "binding swap"
    )
    pulse, manifest_url, _ = _write_publication(project, candidate.release_directory)
    outside = tmp_path / "outside-release"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("external", encoding="utf-8")
    backup = candidate.release_directory.parent / f".{candidate.release_id}-original"
    original_install = artifacts_module._install_publication_directory
    swapped = False

    def swap_before_install(*args, **kwargs):
        nonlocal swapped
        if not swapped:
            candidate.release_directory.rename(backup)
            candidate.release_directory.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original_install(*args, **kwargs)

    monkeypatch.setattr(
        artifacts_module, "_install_publication_directory", swap_before_install
    )
    try:
        with pytest.raises(PublicationError, match="release directory"):
            publish_release(
                project,
                candidate.release_id,
                schemas_directory=schemas_directory,
                pulse=pulse,
                artifact_manifests=(manifest_url,),
                gate_runner=_ok_gate,
            )
        assert sentinel.read_text(encoding="utf-8") == "external"
        assert not (outside / "publication").exists()
        assert not (project / "data" / "current.json").exists()
    finally:
        if candidate.release_directory.is_symlink():
            candidate.release_directory.unlink()
        if backup.exists():
            backup.rename(candidate.release_directory)
