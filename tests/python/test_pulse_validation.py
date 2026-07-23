from __future__ import annotations

from pathlib import Path

import pytest

from research_pipeline.config import load_pipeline_config
from research_pipeline.errors import ValidationError
from research_pipeline.pulse_validation import validate_pulse_file


def test_checked_in_pulse_has_required_structure_and_artifact(repository_root: Path) -> None:
    config = load_pipeline_config(repository_root / "config" / "sources.yaml")
    result = validate_pulse_file(
        repository_root / "content" / "pulses" / "2026-07-22.md",
        repository_root,
        schema_path=repository_root / "schemas" / "pulse.schema.json",
        source_ids={source.id for source in config.sources},
    )
    assert 350 <= result.word_count <= 650
    assert result.signal_count == 3
    assert result.artifact_manifests == (
        "/artifacts/2026-07-22/stage-error-comparison/manifest.json",
    )


def test_pulse_rejects_missing_visual_and_too_many_signals(tmp_path: Path) -> None:
    pulse = tmp_path / "pulse.md"
    body = "\n\n".join(
        [
            "---\nid: p\ndate: '2026-01-01'\ntitle: T\nlead: One sentence.\nsource_ids: [src]\n---",
            *(f"## Signal {index}\n\n" + "word " * 90 for index in range(1, 5)),
            "## Why this matters\n\nReason.",
            "## Unresolved question\n\nQuestion?",
            "## Sources\n\nSource.",
        ]
    )
    pulse.write_text(body, encoding="utf-8")
    with pytest.raises(ValidationError) as error:
        validate_pulse_file(pulse, tmp_path, source_ids={"src"})
    assert "between one and three" in str(error.value)
    assert "chart, diagram" in str(error.value)
    assert "at least one source citation" in str(error.value)
