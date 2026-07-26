import hashlib
import json

import pytest

from scripts.export_public_release import (
    PublicReleaseError,
    _approve_project_generated_artifact,
    _manual_reader_guides,
)


def test_public_export_stamps_already_cleared_project_artifact() -> None:
    exported = _approve_project_generated_artifact(
        {
            "artifact_id": "automatic-diagram",
            "artifact_type": "diagram",
            "rights": {
                "status": "project_generated_diagram",
                "may_publish_publicly": True,
                "public_deployment_requires_owner_approval": False,
            },
        }
    )

    assert exported["rights"] == {
        "status": "project_generated_diagram",
        "may_publish_publicly": True,
        "public_deployment_requires_owner_approval": False,
        "public_deployment_approved_by": "project_owner",
        "public_deployment_approved_on": "2026-07-23",
        "public_deployment_approval_scope": "project-generated artifact public deployment",
    }


def test_reader_guide_is_bound_to_the_exact_accepted_pulse(tmp_path) -> None:
    pulse = b"# Bound pulse\n\nAccepted report bytes.\n"
    pulse_sha256 = hashlib.sha256(pulse).hexdigest()
    bound_pulse = (
        "data/releases/release-aaaaaaaaaaaaaaaa/"
        "publication/content/pulses/2026-07-22.md"
    )
    bound_path = tmp_path / bound_pulse
    bound_path.parent.mkdir(parents=True)
    bound_path.write_bytes(pulse)

    orientation = (
        "Start with the main idea in everyday language, then use the formal report "
        "for the precise assumptions, evidence, and limits."
    )
    registry = {
        "schema_version": 1,
        "guides": [
            {
                "id": "reader-guide-2026-07-22",
                "pulse_id": "pulse-2026-07-22",
                "pulse_path": "content/pulses/2026-07-22.md",
                "pulse_sha256": pulse_sha256,
                "owner_approved": True,
                "reason": "Add a plain-language orientation without changing accepted bytes.",
                "orientation": orientation,
            }
        ],
    }
    config_path = tmp_path / "config" / "pulse-reader-guides.json"
    config_path.parent.mkdir()
    config_path.write_text(json.dumps(registry), encoding="utf-8")
    accepted = [
        {
            "pulse": "content/pulses/2026-07-22.md",
            "pulse_sha256": pulse_sha256,
            "bound_pulse": bound_pulse,
        }
    ]

    assert _manual_reader_guides(tmp_path, accepted) == {
        "pulse-2026-07-22": orientation
    }

    registry["guides"][0]["pulse_sha256"] = "0" * 64
    config_path.write_text(json.dumps(registry), encoding="utf-8")
    with pytest.raises(PublicReleaseError, match="hash is stale"):
        _manual_reader_guides(tmp_path, accepted)
