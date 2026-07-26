from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from research_pipeline.external import (
    ExternalMetadataTimeout,
    ExternalMonitoringError,
    fetch_metadata,
    run_external_search,
)
from research_pipeline.external_preflight import (
    ExternalPreflightError,
    load_scheduled_search_outcome,
    write_scheduled_search_outcome,
)
from research_pipeline.hashing import canonical_json_bytes, canonical_json_hash


RUN_DATE = "2026-07-23"
AS_OF = f"{RUN_DATE}T06:00:00+03:00"


def _project(tmp_path: Path, repository_root: Path) -> Path:
    project = tmp_path / "project"
    (project / "schemas").mkdir(parents=True)
    (project / "data" / "external" / "batches").mkdir(parents=True)
    schema = repository_root / "schemas" / "external-search-outcome.schema.json"
    (project / "schemas" / schema.name).write_bytes(schema.read_bytes())
    return project


def _batch(project: Path) -> tuple[str, dict[str, object]]:
    batch: dict[str, object] = {
        "schema_version": "1.0.0",
        "as_of": "2026-07-23T03:00:00Z",
        "status": "no_candidates",
        "metadata_only": True,
        "queries": [
            {
                "id": "arxiv-test",
                "provider": "arxiv",
                "request_url": (
                    "https://export.arxiv.org/api/query?"
                    "search_query=all%3Atest&start=0&max_results=1"
                ),
                "response_sha256": "a" * 64,
                "response_size_bytes": 1,
                "matched_count": 0,
                "batch_candidate_count": 0,
            }
        ],
        "candidates": [],
        "already_seen_count": 0,
    }
    identity = {
        key: batch[key]
        for key in (
            "schema_version",
            "as_of",
            "status",
            "metadata_only",
            "queries",
            "candidates",
            "already_seen_count",
        )
    }
    batch["batch_sha256"] = canonical_json_hash(identity)
    batch["id"] = f"external-batch-{str(batch['batch_sha256'])[:20]}"
    relative = f"data/external/batches/{batch['id']}.json"
    (project / relative).write_bytes(canonical_json_bytes(batch) + b"\n")
    return relative, batch


def test_ready_outcome_binds_and_revalidates_exact_batch(
    tmp_path: Path, repository_root: Path
) -> None:
    project = _project(tmp_path, repository_root)
    batch_path, batch = _batch(project)

    relative = write_scheduled_search_outcome(
        project,
        run_date=RUN_DATE,
        as_of=AS_OF,
        status="ready",
        reason="metadata search completed",
        search_result={"batch_id": batch["id"], "batch_path": batch_path},
    )
    outcome = load_scheduled_search_outcome(project, relative, run_date=RUN_DATE)

    assert outcome["status"] == "ready"
    assert outcome["batch_id"] == batch["id"]
    assert outcome["batch_sha256"] == batch["batch_sha256"]

    tampered = json.loads((project / relative).read_text(encoding="utf-8"))
    tampered["reason"] = "tampered"
    (project / relative).write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ExternalPreflightError, match="identity hash"):
        load_scheduled_search_outcome(project, relative, run_date=RUN_DATE)


def test_deferred_outcome_carries_no_batch(
    tmp_path: Path, repository_root: Path
) -> None:
    project = _project(tmp_path, repository_root)
    relative = write_scheduled_search_outcome(
        project,
        run_date=RUN_DATE,
        as_of=AS_OF,
        status="deferred",
        reason="arxiv query timed out",
    )

    outcome = load_scheduled_search_outcome(project, relative, run_date=RUN_DATE)
    assert outcome["status"] == "deferred"
    assert outcome["batch_path"] is None


def test_timeout_is_annotated_with_the_exact_query(
    tmp_path: Path, repository_root: Path
) -> None:
    def timeout(*args: object, **kwargs: object) -> bytes:
        raise ExternalMetadataTimeout("metadata request timed out")

    with pytest.raises(
        ExternalMetadataTimeout,
        match="arxiv query arxiv-iterative-filtering timed out",
    ):
        run_external_search(
            repository_root / "config" / "external-sources.yaml",
            tmp_path,
            AS_OF,
            fetcher=timeout,
            sleeper=lambda _: None,
        )


def test_read_timeout_is_classified_as_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Headers:
        @staticmethod
        def get_content_type() -> str:
            return "application/atom+xml"

        @staticmethod
        def get(name: str) -> None:
            return None

    class Response:
        status = 200
        headers = Headers()

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        @staticmethod
        def getcode() -> int:
            return 200

        @staticmethod
        def geturl() -> str:
            return "https://export.arxiv.org/api/query?search_query=all%3Atest"

        @staticmethod
        def read(size: int) -> bytes:
            raise TimeoutError("The read operation timed out")

    class Opener:
        @staticmethod
        def open(request: object, timeout: float) -> Response:
            return Response()

    import research_pipeline.external as external_module

    monkeypatch.setattr(
        external_module.urllib.request,
        "build_opener",
        lambda *args: Opener(),
    )
    with pytest.raises(ExternalMetadataTimeout, match="read operation timed out"):
        fetch_metadata(
            "https://export.arxiv.org/api/query?search_query=all%3Atest",
            timeout_seconds=15,
            max_bytes=1024,
            allowed_hosts=["export.arxiv.org"],
            media_types=["application/atom+xml"],
            user_agent="test",
        )


def test_scheduled_search_cli_records_timeout_as_no_update(
    tmp_path: Path,
    repository_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _project(tmp_path, repository_root)
    script_path = repository_root / "scripts" / "search_external_sources.py"
    specification = importlib.util.spec_from_file_location(
        "scheduled_external_search_for_test", script_path
    )
    assert specification and specification.loader
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)

    def timeout(*args: object, **kwargs: object) -> dict[str, object]:
        raise ExternalMetadataTimeout(
            "arxiv query arxiv-iterative-filtering timed out"
        )

    monkeypatch.setattr(module, "run_external_search", timeout)
    exit_code = module.main(
        [
            "--project-root",
            str(project),
            "--as-of",
            AS_OF,
            "--scheduled-outcome-date",
            RUN_DATE,
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["status"] == "no_update"
    outcome = load_scheduled_search_outcome(
        project, payload["scheduled_outcome_path"], run_date=RUN_DATE
    )
    assert outcome["status"] == "deferred"


def test_scheduled_search_cli_preserves_non_timeout_failure(
    tmp_path: Path,
    repository_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _project(tmp_path, repository_root)
    script_path = repository_root / "scripts" / "search_external_sources.py"
    specification = importlib.util.spec_from_file_location(
        "failed_scheduled_external_search_for_test", script_path
    )
    assert specification and specification.loader
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)

    def invalid(*args: object, **kwargs: object) -> dict[str, object]:
        raise ExternalMonitoringError("metadata response failed integrity validation")

    monkeypatch.setattr(module, "run_external_search", invalid)
    exit_code = module.main(
        [
            "--project-root",
            str(project),
            "--as-of",
            AS_OF,
            "--scheduled-outcome-date",
            RUN_DATE,
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["status"] == "failed"
    outcome = load_scheduled_search_outcome(
        project, payload["scheduled_outcome_path"], run_date=RUN_DATE
    )
    assert outcome["status"] == "failed"
