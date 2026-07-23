from __future__ import annotations

from pathlib import Path

import pytest

from research_pipeline.models import PipelineConfig, RootConfig, SourceConfig


def materialize_test_site(command, environment, *, marker: str = "test site") -> None:
    if tuple(command) != ("npm", "run", "build"):
        return
    output = Path(environment["IMF_PULSE_BUILD_OUT_DIR"])
    output.mkdir(parents=True, exist_ok=True)
    (output / "index.html").write_text(
        f"<!doctype html><title>{marker}</title>\n", encoding="utf-8"
    )


@pytest.fixture
def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture
def schemas_directory(repository_root: Path) -> Path:
    return repository_root / "schemas"


def make_source(
    path: str = "README.md",
    *,
    source_id: str = "src-test",
    extractor: str = "text-lines-v1",
) -> SourceConfig:
    return SourceConfig(
        id=source_id,
        root="imf",
        path=path,
        title=f"Test source {source_id}",
        authors=(),
        date=None,
        source_type="technical_documentation",
        authority_level="internal_unverified",
        publication_status="unpublished",
        topics=("imf",),
        rights={
            "license": "unknown",
            "reuse_status": "internal_only",
            "public_distribution": False,
        },
        limitations=("Synthetic test fixture.",),
        extractor=extractor,
        required=True,
    )


def make_config(project_root: Path, source_root: Path, sources: tuple[SourceConfig, ...]) -> PipelineConfig:
    return PipelineConfig(
        path=project_root / "config" / "sources.yaml",
        version=1,
        roots={
            "imf": RootConfig(
                id="imf",
                live_path_env=None,
                default_live_path=str(source_root),
                snapshot_root="imports/imf",
                access="read_only",
            )
        },
        sources=sources,
        policy={"external_monitoring": False},
    )


@pytest.fixture
def empty_knowledge(tmp_path: Path) -> Path:
    directory = tmp_path / "knowledge" / "curated"
    directory.mkdir(parents=True)
    for name in ("claims.jsonl", "methods.jsonl", "experiments.jsonl", "relationships.jsonl"):
        (directory / name).write_bytes(b"")
    return directory
