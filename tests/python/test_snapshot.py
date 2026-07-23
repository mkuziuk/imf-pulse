from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from conftest import make_config, make_source
from research_pipeline.errors import SnapshotError
from research_pipeline.config import config_fingerprint
from research_pipeline.hashing import canonical_json_hash
from research_pipeline.snapshot import (
    atomic_write_json,
    build_snapshot,
    load_current_snapshot,
    load_explicit_snapshot,
    load_snapshot_manifest,
    verify_snapshot,
)


def test_snapshot_copies_exact_binary_bytes_and_ignores_unconfigured_files(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    project_root = tmp_path / "project"
    source_root.mkdir()
    project_root.mkdir()
    payload = b"line-one\r\n\x00\xffno-final-newline"
    (source_root / "selected.bin").write_bytes(payload)
    (source_root / "ignored.txt").write_text("must not be copied", encoding="utf-8")
    config = make_config(
        project_root,
        source_root,
        (make_source("selected.bin", extractor="text-lines-v1"),),
    )

    manifest, directory, created = build_snapshot(config, project_root, update_pointer=True)

    assert created is True
    entry = manifest.entries[0]
    assert entry.sha256 == hashlib.sha256(payload).hexdigest()
    assert (directory / entry.snapshot_path).read_bytes() == payload
    assert not list(directory.rglob("ignored.txt"))
    loaded, loaded_directory = load_current_snapshot(project_root, config)
    assert loaded.snapshot_id == manifest.snapshot_id
    assert loaded_directory == directory


def test_snapshot_identity_is_independent_of_allowlist_order(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "a.md").write_text("A", encoding="utf-8")
    (source_root / "b.md").write_text("B", encoding="utf-8")
    sources = (
        make_source("a.md", source_id="src-a"),
        make_source("b.md", source_id="src-b"),
    )
    project_one = tmp_path / "project-one"
    project_two = tmp_path / "project-two"
    project_one.mkdir()
    project_two.mkdir()
    first, _, _ = build_snapshot(make_config(project_one, source_root, sources), project_one)
    second, _, _ = build_snapshot(make_config(project_two, source_root, tuple(reversed(sources))), project_two)
    assert first.snapshot_id == second.snapshot_id


def test_snapshot_tampering_is_detected(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    project_root = tmp_path / "project"
    source_root.mkdir()
    project_root.mkdir()
    (source_root / "README.md").write_text("original", encoding="utf-8")
    config = make_config(project_root, source_root, (make_source(),))
    manifest, directory, _ = build_snapshot(config, project_root)
    (directory / manifest.entries[0].snapshot_path).write_text("tampered", encoding="utf-8")
    with pytest.raises(SnapshotError, match="mismatch"):
        verify_snapshot(directory, manifest)


def test_missing_required_source_does_not_create_pointer(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    project_root = tmp_path / "project"
    source_root.mkdir()
    project_root.mkdir()
    config = make_config(project_root, source_root, (make_source("missing.md"),))
    with pytest.raises(SnapshotError, match="required source"):
        build_snapshot(config, project_root)
    assert not (project_root / "imports" / "imf" / "current.json").exists()


def test_snapshot_does_not_mutate_source_bytes_or_metadata(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    project_root = tmp_path / "project"
    source_root.mkdir()
    project_root.mkdir()
    source = source_root / "README.md"
    source.write_text("read only", encoding="utf-8")
    before = source.stat()
    build_snapshot(make_config(project_root, source_root, (make_source(),)), project_root)
    after = source.stat()
    assert source.read_text(encoding="utf-8") == "read only"
    assert (before.st_ino, before.st_mode, before.st_size, before.st_mtime_ns) == (
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
    )


def test_overlap_is_rejected_before_any_source_tree_mutation(tmp_path: Path) -> None:
    root = tmp_path / "same-root"
    root.mkdir()
    (root / "README.md").write_text("read only", encoding="utf-8")
    config = make_config(root, root, (make_source(),))
    before = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
    with pytest.raises(SnapshotError, match="must not overlap"):
        build_snapshot(config, root)
    after = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
    assert after == before
    assert not (root / "imports").exists()


def test_snapshot_pointer_write_uses_held_output_descriptor_after_parent_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import research_pipeline.snapshot as snapshot_module

    project = tmp_path / "project"
    source = tmp_path / "source"
    project.mkdir()
    source.mkdir()
    (source / "README.md").write_text("read only", encoding="utf-8")
    (source / "imf").mkdir()
    config = make_config(project, source, (make_source(),))
    original_write = snapshot_module._atomic_write_json_at
    backup = project / "imports-original"

    def swap_parent(directory_descriptor, name, value):
        (project / "imports").rename(backup)
        (project / "imports").symlink_to(source, target_is_directory=True)
        original_write(directory_descriptor, name, value)

    monkeypatch.setattr(snapshot_module, "_atomic_write_json_at", swap_parent)
    try:
        with pytest.raises(SnapshotError):
            build_snapshot(config, project, update_pointer=True)
        assert not (source / "imf" / "current.json").exists()
        assert not (backup / "imf" / "current.json").exists()
    finally:
        if (project / "imports").is_symlink():
            (project / "imports").unlink()
        if backup.exists():
            backup.rename(project / "imports")


def test_load_current_snapshot_rejects_symlinked_snapshot_root_ancestor(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    producer = tmp_path / "producer"
    consumer = tmp_path / "consumer"
    source.mkdir()
    producer.mkdir()
    consumer.mkdir()
    (source / "README.md").write_text("source", encoding="utf-8")
    producer_config = make_config(producer, source, (make_source(),))
    build_snapshot(producer_config, producer, update_pointer=True)
    (consumer / "imports").symlink_to(producer / "imports", target_is_directory=True)
    consumer_config = make_config(consumer, source, (make_source(),))
    with pytest.raises(SnapshotError, match="unsafe"):
        load_current_snapshot(consumer, consumer_config)


def test_fifo_source_is_rejected_without_blocking(tmp_path: Path) -> None:
    source = tmp_path / "source"
    project = tmp_path / "project"
    source.mkdir()
    project.mkdir()
    fifo = source / "pipe"
    import os

    os.mkfifo(fifo)
    config = make_config(project, source, (make_source("pipe"),))
    with pytest.raises(SnapshotError, match="regular file"):
        build_snapshot(config, project)


def test_snapshot_child_swap_rolls_back_to_prior_valid_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import research_pipeline.snapshot as snapshot_module

    project = tmp_path / "project"
    source = tmp_path / "source"
    project.mkdir()
    source.mkdir()
    source_file = source / "README.md"
    source_file.write_text("first", encoding="utf-8")
    config = make_config(project, source, (make_source(),))
    first, _, _ = build_snapshot(config, project, update_pointer=True)
    pointer_path = project / "imports" / "imf" / "current.json"
    original_pointer = pointer_path.read_bytes()
    source_file.write_text("second", encoding="utf-8")
    original_write = snapshot_module._atomic_write_json_at

    def swap_verified_child(directory_descriptor, name, value):
        original_write(directory_descriptor, name, value)
        snapshot_id = value["snapshot_id"]
        snapshots = project / "imports" / "imf" / "snapshots"
        target = snapshots / snapshot_id
        target.rename(snapshots / f"{snapshot_id}.detached")
        target.mkdir()

    monkeypatch.setattr(snapshot_module, "_atomic_write_json_at", swap_verified_child)
    with pytest.raises(SnapshotError, match="snapshot directory changed"):
        build_snapshot(config, project, update_pointer=True)
    assert pointer_path.read_bytes() == original_pointer
    loaded, _ = load_current_snapshot(project, config)
    assert loaded.snapshot_id == first.snapshot_id


def test_snapshot_content_mutation_after_commit_rolls_back_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import research_pipeline.snapshot as snapshot_module

    project = tmp_path / "project"
    source = tmp_path / "source"
    project.mkdir()
    source.mkdir()
    source_file = source / "README.md"
    source_file.write_text("first", encoding="utf-8")
    config = make_config(project, source, (make_source(),))
    first, _, _ = build_snapshot(config, project, update_pointer=True)
    pointer_path = project / "imports" / "imf" / "current.json"
    original_pointer = pointer_path.read_bytes()
    source_file.write_text("second", encoding="utf-8")
    original_write = snapshot_module._atomic_write_json_at

    def mutate_verified_bytes(directory_descriptor, name, value):
        original_write(directory_descriptor, name, value)
        copied = (
            project
            / "imports"
            / "imf"
            / "snapshots"
            / value["snapshot_id"]
            / "files"
            / "README.md"
        )
        copied.write_text("tampered after commit", encoding="utf-8")

    monkeypatch.setattr(
        snapshot_module, "_atomic_write_json_at", mutate_verified_bytes
    )
    with pytest.raises(SnapshotError, match="mismatch"):
        build_snapshot(config, project, update_pointer=True)
    assert pointer_path.read_bytes() == original_pointer
    loaded, _ = load_current_snapshot(project, config)
    assert loaded.snapshot_id == first.snapshot_id


def test_snapshot_post_commit_write_error_restores_prior_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import research_pipeline.snapshot as snapshot_module

    project = tmp_path / "project"
    source = tmp_path / "source"
    project.mkdir()
    source.mkdir()
    source_file = source / "README.md"
    source_file.write_text("first", encoding="utf-8")
    config = make_config(project, source, (make_source(),))
    first, _, _ = build_snapshot(config, project, update_pointer=True)
    pointer_path = project / "imports" / "imf" / "current.json"
    original_pointer = pointer_path.read_bytes()
    source_file.write_text("second", encoding="utf-8")
    original_write = snapshot_module._atomic_write_json_at

    def fail_after_commit(directory_descriptor, name, value):
        original_write(directory_descriptor, name, value)
        raise OSError("injected post-commit failure")

    monkeypatch.setattr(snapshot_module, "_atomic_write_json_at", fail_after_commit)
    with pytest.raises(OSError, match="post-commit"):
        build_snapshot(config, project, update_pointer=True)
    assert pointer_path.read_bytes() == original_pointer
    loaded, _ = load_current_snapshot(project, config)
    assert loaded.snapshot_id == first.snapshot_id


def test_snapshot_manifest_and_pointer_are_schema_strict(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = tmp_path / "source"
    project.mkdir()
    source.mkdir()
    (source / "README.md").write_text("strict", encoding="utf-8")
    config = make_config(project, source, (make_source(),))
    _, directory, _ = build_snapshot(config, project, update_pointer=True)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["unexpected"] = True
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    with pytest.raises(SnapshotError, match="fields do not match schema"):
        load_snapshot_manifest(directory)

    pointer_path = project / "imports" / "imf" / "current.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    pointer["unexpected"] = True
    pointer_path.write_text(json.dumps(pointer) + "\n", encoding="utf-8")
    with pytest.raises(SnapshotError, match="fields do not match schema"):
        load_current_snapshot(project, config)


def test_snapshot_rejects_symlinked_manifest_and_unlisted_nodes(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = tmp_path / "source"
    project.mkdir()
    source.mkdir()
    (source / "README.md").write_text("strict", encoding="utf-8")
    config = make_config(project, source, (make_source(),))
    manifest, directory, _ = build_snapshot(config, project)
    (directory / "unlisted.txt").write_text("not declared", encoding="utf-8")
    with pytest.raises(SnapshotError, match="unlisted"):
        verify_snapshot(directory, manifest)
    (directory / "unlisted.txt").unlink()
    manifest_path = directory / "manifest.json"
    saved = directory / "saved-manifest.json"
    manifest_path.rename(saved)
    manifest_path.symlink_to(saved.name)
    with pytest.raises(SnapshotError, match="regular file"):
        load_snapshot_manifest(directory)


def test_snapshot_rejects_all_optional_missing_sources(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = tmp_path / "source"
    project.mkdir()
    source.mkdir()
    optional = replace(make_source("missing.md"), required=False)
    config = make_config(project, source, (optional,))
    with pytest.raises(SnapshotError, match="at least one available source"):
        build_snapshot(config, project, update_pointer=True)
    assert not (project / "imports").exists()


def test_snapshot_manifest_digest_binds_provenance_metadata(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = tmp_path / "source"
    project.mkdir()
    source.mkdir()
    (source / "README.md").write_text("provenance", encoding="utf-8")
    config = make_config(project, source, (make_source(),))
    _, directory, _ = build_snapshot(config, project)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_root_hint"] = "/another/absolute/source"
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    loaded = load_snapshot_manifest(directory)
    with pytest.raises(SnapshotError, match="integrity hash mismatch"):
        verify_snapshot(directory, loaded)


def test_snapshot_v2_identity_does_not_collide_with_legacy_same_content(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    source = tmp_path / "source"
    project.mkdir()
    source.mkdir()
    payload = b"same content"
    (source / "README.md").write_bytes(payload)
    config = make_config(project, source, (make_source(),))
    entry = {
        "source_id": "src-test",
        "relative_path": "README.md",
        "snapshot_path": "files/README.md",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
        "extractor": "text-lines-v1",
    }
    legacy_identity = canonical_json_hash(
        {
            "schema_version": 1,
            "root_id": "imf",
            "config_sha256": config_fingerprint(config),
            "entries": [entry],
            "missing_optional_sources": [],
        }
    )
    legacy_id = f"snapshot-{legacy_identity[:20]}"
    legacy_directory = project / "imports" / "imf" / "snapshots" / legacy_id
    legacy_directory.mkdir(parents=True)
    (legacy_directory / "legacy-marker").write_text("immutable v1", encoding="utf-8")

    manifest, directory, created = build_snapshot(config, project, update_pointer=True)
    assert created is True
    assert manifest.schema_version == 2
    assert manifest.snapshot_id != legacy_id
    assert directory.is_dir()
    assert (legacy_directory / "legacy-marker").read_text(encoding="utf-8") == "immutable v1"


def test_explicit_snapshot_rejects_outside_paths_and_symlinked_ancestors(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    producer = tmp_path / "producer"
    consumer = tmp_path / "consumer"
    source.mkdir()
    producer.mkdir()
    consumer.mkdir()
    (source / "README.md").write_text("source", encoding="utf-8")
    producer_config = make_config(producer, source, (make_source(),))
    manifest, directory, _ = build_snapshot(producer_config, producer)
    consumer_config = make_config(consumer, source, (make_source(),))
    with pytest.raises(SnapshotError, match="inside the project snapshot root"):
        load_explicit_snapshot(consumer, consumer_config, directory)

    (consumer / "imports").symlink_to(producer / "imports", target_is_directory=True)
    apparent = (
        consumer
        / "imports"
        / "imf"
        / "snapshots"
        / manifest.snapshot_id
    )
    with pytest.raises(SnapshotError, match="unsafe"):
        load_explicit_snapshot(consumer, consumer_config, apparent)


def test_atomic_json_write_rejects_symlinked_output_ancestor(tmp_path: Path) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    sentinel = outside / "current.json"
    sentinel.write_text("external checkpoint\n", encoding="utf-8")
    (project / "data").symlink_to(outside, target_is_directory=True)

    with pytest.raises(SnapshotError, match="ancestor.*unsafe"):
        atomic_write_json(project / "data" / "current.json", {"status": "published"})

    assert sentinel.read_text(encoding="utf-8") == "external checkpoint\n"
