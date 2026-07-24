from scripts.export_public_release import _approve_project_generated_artifact


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
