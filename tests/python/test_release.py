from __future__ import annotations

import json
import hashlib
import os
import sys
from pathlib import Path

import pytest

from conftest import make_config, make_source, materialize_test_site
from research_pipeline.errors import PublicationError, ValidationError
from research_pipeline.hashing import canonical_json_hash
from research_pipeline.release import build_release_candidate, publish_release
from research_pipeline.snapshot import build_snapshot
from research_pipeline.validation import validate_release_directory


def _ok_gate(command, cwd, environment) -> None:
    materialize_test_site(command, environment)


def _claim_record(source_payload: bytes) -> dict:
    return {
        "schema_version": "1.0.0",
        "id": "claim-accepted",
        "created_at": "2026-07-22T00:00:00Z",
        "updated_at": "2026-07-22T00:00:00Z",
        "normalized_text": "The accepted source contains one line.",
        "statement_kind": "observation",
        "evidence_status": "observed",
        "scope": "Synthetic append-only regression fixture.",
        "assumptions": [],
        "confidence": {
            "level": "high",
            "score": 0.9,
            "rationale": "The fixture is directly observed.",
        },
        "evidence": [
            {
                "source_id": "src-test",
                "source_sha256": hashlib.sha256(source_payload).hexdigest(),
                "role": "direct",
                "locator": {
                    "kind": "file_lines",
                    "path": "README.md",
                    "line_start": 1,
                    "line_end": 1,
                },
            }
        ],
    }


def _write_claim(path: Path, record: dict | None) -> None:
    path.write_text(
        "" if record is None else json.dumps(record, sort_keys=True) + "\n",
        encoding="utf-8",
    )


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
    result = build_release_candidate(
        project_root,
        config,
        snapshot_directory=snapshot_directory,
        knowledge_directory=empty_knowledge,
        schemas_directory=schemas_directory,
    )
    return config, result


def test_ingestion_creates_candidate_without_advancing_pointer(
    tmp_path: Path, empty_knowledge: Path, schemas_directory: Path
) -> None:
    project_root = tmp_path / "project"
    source_root = tmp_path / "source"
    _, result = _prepare_candidate(
        project_root, source_root, empty_knowledge, schemas_directory, "first version"
    )
    assert result.created is True
    assert result.status == "candidate"
    assert not (project_root / "data" / "current.json").exists()
    source_record = json.loads((result.release_directory / "sources.jsonl").read_text())
    assert source_record["id"] == "src-test"
    assert source_record["content_hash"] == source_record["content_sha256"]
    assert (result.release_directory / "extracts" / "src-test.jsonl").is_file()


def test_release_validation_recomputes_semantic_identity(
    tmp_path: Path, empty_knowledge: Path, schemas_directory: Path
) -> None:
    project_root = tmp_path / "project"
    source_root = tmp_path / "source"
    _, candidate = _prepare_candidate(
        project_root,
        source_root,
        empty_knowledge,
        schemas_directory,
        "identity must be reconstructed",
    )
    manifest_path = candidate.release_directory / "release.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["runtime"]["python"] = "0.0.0-tampered"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(ValidationError, match="semantic fingerprint cannot be reconstructed"):
        validate_release_directory(candidate.release_directory, schemas_directory)


def test_publish_runs_all_gates_before_atomic_pointer(
    tmp_path: Path, empty_knowledge: Path, schemas_directory: Path
) -> None:
    project_root = tmp_path / "project"
    source_root = tmp_path / "source"
    _, candidate = _prepare_candidate(
        project_root, source_root, empty_knowledge, schemas_directory, "publish me"
    )
    observed: list[tuple[str, ...]] = []

    def gate(command, cwd, environment):
        assert not (project_root / "data" / "current.json").exists()
        assert Path(environment["IMF_PULSE_RELEASE_DIR"]) == candidate.release_directory
        assert environment["IMF_PULSE_CHECKPOINT_STATUS"] == "processed_no_pulse"
        assert "IMF_PULSE_SELECTED_PULSE" not in environment
        assert json.loads(environment["IMF_PULSE_ARTIFACT_MANIFESTS"]) == []
        observed.append(tuple(command))
        materialize_test_site(command, environment)

    result = publish_release(
        project_root,
        candidate.release_id,
        schemas_directory=schemas_directory,
        gate_runner=gate,
        now="2026-07-22T05:00:00Z",
    )
    assert observed == [
        (sys.executable, "-m", "pytest"),
        ("npm", "test"),
        ("npm", "run", "build"),
    ]
    assert result.pointer_changed is True
    pointer = json.loads((project_root / "data" / "current.json").read_text())
    assert pointer["release_id"] == candidate.release_id
    assert pointer["status"] == "processed_no_pulse"
    assert pointer["site_build_path"] == (
        f"data/site-builds/site-{pointer['site_build_sha256']}"
    )


def test_failed_production_build_preserves_release_and_site_selection(
    tmp_path: Path, empty_knowledge: Path, schemas_directory: Path
) -> None:
    project = tmp_path / "project"
    source = tmp_path / "source"
    config, first = _prepare_candidate(
        project, source, empty_knowledge, schemas_directory, "first site"
    )
    publish_release(
        project,
        first.release_id,
        schemas_directory=schemas_directory,
        gate_runner=_ok_gate,
    )
    pointer_path = project / "data" / "current.json"
    before = pointer_path.read_bytes()
    selected_before = json.loads(before)["site_build_path"]
    (source / "README.md").write_text("second site", encoding="utf-8")
    _, snapshot_directory, _ = build_snapshot(config, project)
    second = build_release_candidate(
        project,
        config,
        snapshot_directory=snapshot_directory,
        knowledge_directory=empty_knowledge,
        schemas_directory=schemas_directory,
    )

    def fail_build(command, cwd, environment):
        if tuple(command) == ("npm", "run", "build"):
            raise PublicationError("injected production build failure")

    with pytest.raises(PublicationError, match="production build failure"):
        publish_release(
            project,
            second.release_id,
            schemas_directory=schemas_directory,
            gate_runner=fail_build,
        )
    assert pointer_path.read_bytes() == before
    assert (project / selected_before).is_dir()
    assert not list((project / "data" / ".site-staging").iterdir())


def test_staged_site_symlink_is_rejected_without_following_or_committing(
    tmp_path: Path, empty_knowledge: Path, schemas_directory: Path
) -> None:
    project = tmp_path / "project"
    source = tmp_path / "source"
    _, candidate = _prepare_candidate(
        project, source, empty_knowledge, schemas_directory, "unsafe staged site"
    )
    outside = tmp_path / "outside.txt"
    outside.write_text("must survive", encoding="utf-8")

    def stage_symlink(command, cwd, environment):
        if tuple(command) == ("npm", "run", "build"):
            output = Path(environment["IMF_PULSE_BUILD_OUT_DIR"])
            (output / "index.html").write_text("safe entry", encoding="utf-8")
            (output / "escape").symlink_to(outside)

    with pytest.raises(PublicationError, match="forbidden node"):
        publish_release(
            project,
            candidate.release_id,
            schemas_directory=schemas_directory,
            gate_runner=stage_symlink,
        )
    assert outside.read_text(encoding="utf-8") == "must survive"
    assert not (project / "data" / "current.json").exists()
    assert not list((project / "data" / ".site-staging").iterdir())


def test_staged_site_without_index_is_rejected(
    tmp_path: Path, empty_knowledge: Path, schemas_directory: Path
) -> None:
    project = tmp_path / "project"
    source = tmp_path / "source"
    _, candidate = _prepare_candidate(
        project, source, empty_knowledge, schemas_directory, "missing index"
    )

    def stage_asset_only(command, cwd, environment):
        if tuple(command) == ("npm", "run", "build"):
            output = Path(environment["IMF_PULSE_BUILD_OUT_DIR"])
            (output / "asset.txt").write_text("asset", encoding="utf-8")

    with pytest.raises(PublicationError, match="must contain index.html"):
        publish_release(
            project,
            candidate.release_id,
            schemas_directory=schemas_directory,
            gate_runner=stage_asset_only,
        )
    assert not (project / "data" / "current.json").exists()
    assert not list((project / "data" / ".site-staging").iterdir())


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO nodes are POSIX-only")
def test_staged_site_fifo_is_rejected_without_blocking(
    tmp_path: Path, empty_knowledge: Path, schemas_directory: Path
) -> None:
    project = tmp_path / "project"
    source = tmp_path / "source"
    _, candidate = _prepare_candidate(
        project, source, empty_knowledge, schemas_directory, "fifo staged site"
    )

    def stage_fifo(command, cwd, environment):
        if tuple(command) == ("npm", "run", "build"):
            output = Path(environment["IMF_PULSE_BUILD_OUT_DIR"])
            (output / "index.html").write_text("entry", encoding="utf-8")
            os.mkfifo(output / "blocking-node")

    with pytest.raises(PublicationError, match="forbidden node"):
        publish_release(
            project,
            candidate.release_id,
            schemas_directory=schemas_directory,
            gate_runner=stage_fifo,
        )
    assert not (project / "data" / "current.json").exists()


def test_processed_release_is_unchanged_on_next_ingestion(
    tmp_path: Path, empty_knowledge: Path, schemas_directory: Path
) -> None:
    project_root = tmp_path / "project"
    source_root = tmp_path / "source"
    config, first = _prepare_candidate(
        project_root, source_root, empty_knowledge, schemas_directory, "same bytes"
    )
    publish_release(
        project_root,
        first.release_id,
        schemas_directory=schemas_directory,
        gate_runner=_ok_gate,
    )
    _, snapshot_directory, _ = build_snapshot(config, project_root)
    second = build_release_candidate(
        project_root,
        config,
        snapshot_directory=snapshot_directory,
        knowledge_directory=empty_knowledge,
        schemas_directory=schemas_directory,
    )
    assert second.status == "unchanged"
    assert second.created is False
    assert second.semantic_changed is False
    assert second.release_id == first.release_id


def test_existing_publish_lock_blocks_competing_publisher(
    tmp_path: Path, empty_knowledge: Path, schemas_directory: Path
) -> None:
    project_root = tmp_path / "project"
    source_root = tmp_path / "source"
    _, candidate = _prepare_candidate(
        project_root, source_root, empty_knowledge, schemas_directory, "locked"
    )
    lock = project_root / "data" / ".pipeline.lock"
    lock.write_text("pid=other\n", encoding="utf-8")
    with pytest.raises(PublicationError, match="another publication"):
        publish_release(
            project_root,
            candidate.release_id,
            schemas_directory=schemas_directory,
        )
    assert not (project_root / "data" / "current.json").exists()


def test_unchanged_publish_runs_gates_before_refreshing_check(
    tmp_path: Path, empty_knowledge: Path, schemas_directory: Path
) -> None:
    project_root = tmp_path / "project"
    source_root = tmp_path / "source"
    _, candidate = _prepare_candidate(
        project_root, source_root, empty_knowledge, schemas_directory, "unchanged"
    )
    publish_release(
        project_root,
        candidate.release_id,
        schemas_directory=schemas_directory,
        gate_runner=_ok_gate,
        now="2026-07-22T05:00:00Z",
    )

    observed: list[tuple[str, ...]] = []

    def gate(command, cwd, environment):
        observed.append(tuple(command))
        assert Path(environment["IMF_PULSE_RELEASE_DIR"]) == candidate.release_directory
        assert environment["IMF_PULSE_CHECKPOINT_STATUS"] == "unchanged"
        assert "IMF_PULSE_SELECTED_PULSE" not in environment
        assert json.loads(environment["IMF_PULSE_ARTIFACT_MANIFESTS"]) == []
        assert Path(environment["IMF_PULSE_BUILD_OUT_DIR"]).is_dir()
        materialize_test_site(command, environment)

    result = publish_release(
        project_root,
        candidate.release_id,
        schemas_directory=schemas_directory,
        gate_runner=gate,
        now="2026-07-23T05:00:00Z",
    )
    assert result.status == "unchanged"
    assert observed == [
        (sys.executable, "-m", "pytest"),
        ("npm", "test"),
        ("npm", "run", "build"),
    ]
    assert result.pointer_changed is False
    pointer = json.loads((project_root / "data" / "current.json").read_text())
    assert pointer["status"] == "unchanged"
    assert pointer["last_checked_at"] == "2026-07-23T05:00:00Z"
    assert pointer["release_id"] == candidate.release_id
    assert pointer["site_build_path"] == (
        f"data/site-builds/site-{pointer['site_build_sha256']}"
    )
    run = json.loads(
        (project_root / "data" / "runs" / f"{result.run_id}.json").read_text()
    )
    assert run["status"] == "unchanged"
    assert run["pointer_changed"] is False


def test_failed_no_update_gate_preserves_exact_checkpoint(
    tmp_path: Path, empty_knowledge: Path, schemas_directory: Path
) -> None:
    project = tmp_path / "project"
    source = tmp_path / "source"
    _, candidate = _prepare_candidate(
        project, source, empty_knowledge, schemas_directory, "unchanged failure"
    )
    publish_release(
        project,
        candidate.release_id,
        schemas_directory=schemas_directory,
        gate_runner=_ok_gate,
    )
    pointer_path = project / "data" / "current.json"
    checkpoint = pointer_path.read_bytes()

    def fail(command, cwd, environment):
        if tuple(command) == ("npm", "test"):
            raise PublicationError("injected no-update gate failure")

    with pytest.raises(PublicationError, match="injected no-update gate failure"):
        publish_release(
            project,
            candidate.release_id,
            schemas_directory=schemas_directory,
            gate_runner=fail,
        )
    assert pointer_path.read_bytes() == checkpoint
    failed_runs = [
        json.loads(path.read_text())
        for path in (project / "data" / "runs").glob("*.json")
        if json.loads(path.read_text())["status"] == "failed"
    ]
    assert len(failed_runs) == 1
    assert failed_runs[0]["pointer_changed"] is False


def test_ingestion_overlap_is_rejected_before_data_creation(
    tmp_path: Path, empty_knowledge: Path, schemas_directory: Path
) -> None:
    root = tmp_path / "same-root"
    root.mkdir()
    (root / "README.md").write_text("read only", encoding="utf-8")
    config = make_config(root, root, (make_source(),))
    with pytest.raises(PublicationError, match="must not overlap"):
        build_release_candidate(
            root,
            config,
            snapshot_directory=root / "missing-snapshot",
            knowledge_directory=empty_knowledge,
            schemas_directory=schemas_directory,
        )
    assert not (root / "data").exists()


def test_non_genesis_candidate_missing_predecessor_is_rejected(
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
    manifest_path = second.release_directory / "release.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.pop("previous_release_id")
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(PublicationError, match="ancestry is stale or missing"):
        publish_release(
            project,
            second.release_id,
            schemas_directory=schemas_directory,
            gate_runner=_ok_gate,
        )
    assert (project / "data" / "current.json").read_bytes() == checkpoint


@pytest.mark.parametrize("change", ["mutate", "delete"])
def test_accepted_knowledge_records_are_append_only(
    tmp_path: Path,
    empty_knowledge: Path,
    schemas_directory: Path,
    change: str,
) -> None:
    project = tmp_path / "project"
    source = tmp_path / "source"
    project.mkdir()
    source.mkdir()
    payload = b"accepted evidence"
    (source / "README.md").write_bytes(payload)
    claim_path = empty_knowledge / "claims.jsonl"
    accepted_claim = _claim_record(payload)
    _write_claim(claim_path, accepted_claim)
    config = make_config(project, source, (make_source(),))
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
    checkpoint = (project / "data" / "current.json").read_bytes()
    if change == "mutate":
        changed_claim = dict(accepted_claim)
        changed_claim["normalized_text"] = "Silently rewritten accepted claim."
        _write_claim(claim_path, changed_claim)
    else:
        _write_claim(claim_path, None)
    with pytest.raises(PublicationError, match="accepted knowledge is append-only"):
        build_release_candidate(
            project,
            config,
            snapshot_directory=snapshot_directory,
            knowledge_directory=empty_knowledge,
            schemas_directory=schemas_directory,
        )
    assert (project / "data" / "current.json").read_bytes() == checkpoint


def test_selected_site_tampering_fails_closed_before_gates(
    tmp_path: Path, empty_knowledge: Path, schemas_directory: Path
) -> None:
    project = tmp_path / "project"
    source = tmp_path / "source"
    _, candidate = _prepare_candidate(
        project, source, empty_knowledge, schemas_directory, "site integrity"
    )
    publish_release(
        project,
        candidate.release_id,
        schemas_directory=schemas_directory,
        gate_runner=_ok_gate,
    )
    pointer_path = project / "data" / "current.json"
    checkpoint = pointer_path.read_bytes()
    pointer = json.loads(checkpoint)
    (project / pointer["site_build_path"] / "index.html").write_text(
        "tampered\n", encoding="utf-8"
    )
    gate_called = False

    def gate(command, cwd, environment):
        nonlocal gate_called
        gate_called = True

    with pytest.raises(PublicationError, match="site build digest mismatch"):
        publish_release(
            project,
            candidate.release_id,
            schemas_directory=schemas_directory,
            gate_runner=gate,
        )
    assert gate_called is False
    assert pointer_path.read_bytes() == checkpoint


def test_site_build_paths_are_content_addressed_and_never_overwritten(
    tmp_path: Path, empty_knowledge: Path, schemas_directory: Path
) -> None:
    project = tmp_path / "project"
    source = tmp_path / "source"
    config, first = _prepare_candidate(
        project, source, empty_knowledge, schemas_directory, "site one"
    )

    def first_build(command, cwd, environment):
        materialize_test_site(command, environment, marker="site one")

    publish_release(
        project,
        first.release_id,
        schemas_directory=schemas_directory,
        gate_runner=first_build,
    )
    first_pointer = json.loads((project / "data" / "current.json").read_text())
    first_path = project / first_pointer["site_build_path"]
    first_bytes = (first_path / "index.html").read_bytes()
    (source / "README.md").write_text("site two", encoding="utf-8")
    _, snapshot_directory, _ = build_snapshot(config, project)
    second = build_release_candidate(
        project,
        config,
        snapshot_directory=snapshot_directory,
        knowledge_directory=empty_knowledge,
        schemas_directory=schemas_directory,
    )

    def second_build(command, cwd, environment):
        materialize_test_site(command, environment, marker="site two")

    publish_release(
        project,
        second.release_id,
        schemas_directory=schemas_directory,
        gate_runner=second_build,
    )
    second_pointer = json.loads((project / "data" / "current.json").read_text())
    assert second_pointer["site_build_path"] != first_pointer["site_build_path"]
    assert (first_path / "index.html").read_bytes() == first_bytes
    assert (project / second_pointer["site_build_path"] / "index.html").read_bytes() != first_bytes


def test_site_build_digest_is_the_canonical_relative_file_mapping(
    tmp_path: Path, empty_knowledge: Path, schemas_directory: Path
) -> None:
    project = tmp_path / "project"
    source = tmp_path / "source"
    _, candidate = _prepare_candidate(
        project, source, empty_knowledge, schemas_directory, "site digest"
    )

    def nested_build(command, cwd, environment):
        if tuple(command) == ("npm", "run", "build"):
            output = Path(environment["IMF_PULSE_BUILD_OUT_DIR"])
            (output / "assets").mkdir()
            (output / "index.html").write_bytes(b"index\n")
            (output / "assets" / "app.js").write_bytes(b"app\n")

    publish_release(
        project,
        candidate.release_id,
        schemas_directory=schemas_directory,
        gate_runner=nested_build,
    )
    pointer = json.loads((project / "data" / "current.json").read_text())
    expected = canonical_json_hash(
        {
            "assets/app.js": hashlib.sha256(b"app\n").hexdigest(),
            "index.html": hashlib.sha256(b"index\n").hexdigest(),
        }
    )
    assert pointer["site_build_sha256"] == expected
    assert pointer["site_build_path"] == f"data/site-builds/site-{expected}"


@pytest.mark.parametrize(
    "site_fields",
    [
        {"site_build_path": None, "site_build_sha256": None},
        {"site_build_path": "data/site-builds/site-" + "0" * 64},
    ],
)
def test_present_invalid_site_pointer_fields_are_not_legacy(
    tmp_path: Path,
    empty_knowledge: Path,
    schemas_directory: Path,
    site_fields: dict,
) -> None:
    project = tmp_path / "project"
    source = tmp_path / "source"
    _, candidate = _prepare_candidate(
        project, source, empty_knowledge, schemas_directory, "pointer fields"
    )
    publish_release(
        project,
        candidate.release_id,
        schemas_directory=schemas_directory,
        gate_runner=_ok_gate,
    )
    pointer_path = project / "data" / "current.json"
    pointer = json.loads(pointer_path.read_text())
    pointer.pop("site_build_path")
    pointer.pop("site_build_sha256")
    pointer.update(site_fields)
    pointer_path.write_text(json.dumps(pointer), encoding="utf-8")
    with pytest.raises(PublicationError, match=r"site.?build"):
        publish_release(
            project,
            candidate.release_id,
            schemas_directory=schemas_directory,
            gate_runner=_ok_gate,
        )


def test_legacy_pointer_without_site_fields_is_upgraded_after_gates(
    tmp_path: Path, empty_knowledge: Path, schemas_directory: Path
) -> None:
    project = tmp_path / "project"
    source = tmp_path / "source"
    _, candidate = _prepare_candidate(
        project, source, empty_knowledge, schemas_directory, "legacy pointer"
    )
    publish_release(
        project,
        candidate.release_id,
        schemas_directory=schemas_directory,
        gate_runner=_ok_gate,
    )
    pointer_path = project / "data" / "current.json"
    pointer = json.loads(pointer_path.read_text())
    pointer.pop("site_build_path")
    pointer.pop("site_build_sha256")
    pointer_path.write_text(json.dumps(pointer), encoding="utf-8")
    publish_release(
        project,
        candidate.release_id,
        schemas_directory=schemas_directory,
        gate_runner=_ok_gate,
    )
    upgraded = json.loads(pointer_path.read_text())
    assert upgraded["site_build_path"] == (
        f"data/site-builds/site-{upgraded['site_build_sha256']}"
    )


def test_atomic_site_install_never_overwrites_a_racing_mismatched_target(
    tmp_path: Path,
    empty_knowledge: Path,
    schemas_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import research_pipeline.release as release_module

    project = tmp_path / "project"
    source = tmp_path / "source"
    _, candidate = _prepare_candidate(
        project, source, empty_knowledge, schemas_directory, "install race"
    )
    original_install = release_module._rename_directory_noreplace
    racing_bytes = b"racing directory must not be replaced\n"
    raced_path: Path | None = None

    def race_install(source_parent, source_name, destination_parent, destination_name):
        nonlocal raced_path
        os.mkdir(destination_name, 0o755, dir_fd=destination_parent)
        directory = os.open(
            destination_name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            dir_fd=destination_parent,
        )
        try:
            file_descriptor = os.open(
                "index.html",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o644,
                dir_fd=directory,
            )
            try:
                os.write(file_descriptor, racing_bytes)
            finally:
                os.close(file_descriptor)
        finally:
            os.close(directory)
        raced_path = project / "data" / "site-builds" / destination_name
        return original_install(
            source_parent, source_name, destination_parent, destination_name
        )

    monkeypatch.setattr(
        release_module, "_rename_directory_noreplace", race_install
    )
    with pytest.raises(PublicationError, match="immutable site build bytes differ"):
        publish_release(
            project,
            candidate.release_id,
            schemas_directory=schemas_directory,
            gate_runner=_ok_gate,
        )
    assert raced_path is not None
    assert (raced_path / "index.html").read_bytes() == racing_bytes
    assert not (project / "data" / "current.json").exists()
    assert not list((project / "data" / ".site-staging").iterdir())


def test_site_cleanup_refuses_to_delete_a_directory_swapped_before_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import research_pipeline.release as release_module

    parent = tmp_path / "staging"
    parent.mkdir()
    expected = parent / "run-test"
    expected.mkdir()
    (expected / "expected.txt").write_text("expected", encoding="utf-8")
    attacker = parent / "attacker"
    attacker.mkdir()
    (attacker / "outside.txt").write_text("must survive", encoding="utf-8")
    identity_stat = os.stat(expected, follow_symlinks=False)
    identity = (identity_stat.st_dev, identity_stat.st_ino)
    original_rename = os.rename
    swapped = False

    def swap_before_rename(source_name, destination_name, **kwargs):
        nonlocal swapped
        if source_name == "run-test" and not swapped:
            swapped = True
            original_rename(
                "run-test", "expected-saved", **kwargs
            )
            original_rename("attacker", "run-test", **kwargs)
        return original_rename(source_name, destination_name, **kwargs)

    monkeypatch.setattr(release_module.os, "rename", swap_before_rename)
    parent_descriptor = os.open(
        parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    )
    try:
        with pytest.raises(PublicationError, match="changed during cleanup"):
            release_module._remove_directory_at(
                parent_descriptor, "run-test", identity
            )
    finally:
        os.close(parent_descriptor)
    assert (parent / "expected-saved" / "expected.txt").read_text() == "expected"
    trash_entries = list(parent.glob(".run-test.trash-*"))
    assert len(trash_entries) == 1
    assert (trash_entries[0] / "outside.txt").read_text() == "must survive"
