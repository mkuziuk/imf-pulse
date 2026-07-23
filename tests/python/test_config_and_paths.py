from __future__ import annotations

from pathlib import Path

import pytest

from research_pipeline.config import load_pipeline_config
from research_pipeline.errors import ConfigurationError, SnapshotError
from research_pipeline.paths import resolve_regular_file_under_root, validate_relative_path


def test_checked_in_allowlist_has_canonical_ids_and_phase_two_policy(repository_root: Path) -> None:
    config = load_pipeline_config(repository_root / "config" / "sources.yaml")
    ids = {source.id for source in config.sources}
    assert len(ids) == 30
    assert {
        "src-imf-draft",
        "src-robust-gd-note",
        "src-nb-observation-model-comparison",
        "src-linear-operator-exact",
        "src-tex-error-scaling-energy",
        "src-diagnostic-generator",
    } <= ids
    assert config.policy["external_monitoring"] == {
        "enabled": True,
        "configuration": "config/external-sources.yaml",
    }
    draft = next(source for source in config.sources if source.id == "src-imf-draft")
    assert draft.authors == ("Vladimir Spokoiny",)
    assert draft.date == "2026-07-03"
    assert any("metadata" in limitation.lower() for limitation in draft.limitations)


@pytest.mark.parametrize("value", ["", "/absolute", "../escape", "a/../escape", "a\\b"])
def test_relative_source_path_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(ConfigurationError):
        validate_relative_path(value)


def test_yaml_duplicate_keys_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "sources.yaml"
    path.write_text("version: 1\nversion: 1\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="duplicate key"):
        load_pipeline_config(path)


def test_resolve_accepts_nested_regular_file(tmp_path: Path) -> None:
    root = tmp_path / "source"
    nested = root / "notes" / "report.md"
    nested.parent.mkdir(parents=True)
    nested.write_text("evidence", encoding="utf-8")
    assert resolve_regular_file_under_root(root, "notes/report.md") == nested.resolve()


def test_resolve_rejects_parent_and_leaf_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "source"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (root / "parent-link").symlink_to(outside, target_is_directory=True)
    (root / "leaf-link").symlink_to(outside / "secret.txt")
    with pytest.raises(SnapshotError, match="symlinks"):
        resolve_regular_file_under_root(root, "parent-link/secret.txt")
    with pytest.raises(SnapshotError, match="symlinks"):
        resolve_regular_file_under_root(root, "leaf-link")


def test_resolve_rejects_directory(tmp_path: Path) -> None:
    root = tmp_path / "source"
    (root / "directory").mkdir(parents=True)
    with pytest.raises(SnapshotError, match="regular file"):
        resolve_regular_file_under_root(root, "directory")
