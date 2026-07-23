from __future__ import annotations

import copy
import json
from pathlib import Path

import jsonschema
import pytest

from research_pipeline.external import (
    ExternalMonitoringError,
    lookup_review_decision,
    record_review_decision,
    run_external_search,
)


PROJECT = Path(__file__).resolve().parents[2]
CONFIG = PROJECT / "config" / "external-sources.yaml"
LEDGER = "data/review/external-decisions.jsonl"


ATOM = b"""<feed xmlns="http://www.w3.org/2005/Atom">
<entry>
<id>https://arxiv.org/abs/2607.22222v1</id>
<updated>2026-07-21T10:00:00Z</updated><published>2026-07-21T10:00:00Z</published>
<title>A primary filtering result</title><summary>Evidence remains metadata only.</summary>
<author><name>Researcher One</name></author><category term="eess.SP" />
</entry></feed>"""


def prepared_batch(project: Path) -> tuple[Path, dict]:
    result = run_external_search(
        CONFIG,
        project,
        "2026-07-23T05:00:00Z",
        fetcher=lambda *_args, **_kwargs: ATOM,
        sleeper=lambda _: None,
    )
    path = project / result["batch_path"]
    return path, json.loads(path.read_text())["candidates"][0]


def rights(*, public: bool = False, status: str = "unknown") -> dict:
    return {
        "license": "unknown",
        "reuse_status": status,
        "public_distribution": public,
        "notes": "Metadata approval does not approve republication of the paper.",
    }


def decide(project: Path, batch: Path, candidate: dict, **overrides: object) -> dict:
    arguments = {
        "project_root": project,
        "batch_path": batch,
        "candidate_id": candidate["id"],
        "expected_candidate_sha256": candidate["candidate_sha256"],
        "decision": "approved",
        "reviewer": "M. Reviewer",
        "reason": "Primary preprint is topically relevant; full text is not fetched.",
        "decided_at": "2026-07-23T09:00:00+03:00",
        "rights": rights(),
        "ledger_relative": LEDGER,
    }
    arguments.update(overrides)
    return record_review_decision(**arguments)


def test_approval_is_append_only_hash_bound_and_lookup_is_exact(tmp_path: Path) -> None:
    batch, candidate = prepared_batch(tmp_path)
    result = decide(tmp_path, batch, candidate)
    assert result["status"] == "recorded"
    decision = result["decision"]
    assert decision["candidate_sha256"] == candidate["candidate_sha256"]
    assert decision["decided_at"] == "2026-07-23T06:00:00Z"
    assert lookup_review_decision(tmp_path, LEDGER, candidate["id"], candidate["candidate_sha256"]) == decision
    assert lookup_review_decision(tmp_path, LEDGER, candidate["id"], "0" * 64) is None

    ledger = tmp_path / LEDGER
    before = ledger.read_bytes()
    with pytest.raises(ExternalMonitoringError, match="already has"):
        decide(tmp_path, batch, candidate, decision="rejected", reason="Changed mind")
    assert ledger.read_bytes() == before

    schema = json.loads((PROJECT / "schemas/external-decision.schema.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(decision)


def test_rejection_also_resolves_review_without_authorizing_retrieval(tmp_path: Path) -> None:
    batch, candidate = prepared_batch(tmp_path)
    result = decide(
        tmp_path,
        batch,
        candidate,
        decision="rejected",
        reason="The abstract uses IMF for a different acronym.",
    )
    assert result["decision"]["decision"] == "rejected"
    assert result["decision"]["rights"]["public_distribution"] is False


def test_candidate_hash_mismatch_and_tampered_batch_never_append(tmp_path: Path) -> None:
    batch, candidate = prepared_batch(tmp_path)
    with pytest.raises(ExternalMonitoringError, match="changed"):
        decide(
            tmp_path,
            batch,
            candidate,
            expected_candidate_sha256="f" * 64,
        )
    assert not (tmp_path / LEDGER).exists()

    tampered = json.loads(batch.read_text())
    tampered["candidates"][0]["title"] = "Silently changed"
    batch.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ExternalMonitoringError, match="identity hash"):
        decide(tmp_path, batch, candidate)
    assert not (tmp_path / LEDGER).exists()


def test_rights_must_not_claim_public_distribution_without_clearance(tmp_path: Path) -> None:
    batch, candidate = prepared_batch(tmp_path)
    with pytest.raises(ExternalMonitoringError, match="public distribution"):
        decide(tmp_path, batch, candidate, rights=rights(public=True, status="unknown"))
    assert not (tmp_path / LEDGER).exists()

    accepted = decide(
        tmp_path,
        batch,
        candidate,
        rights={
            "license": "CC BY 4.0",
            "reuse_status": "cleared",
            "public_distribution": True,
        },
    )
    assert accepted["decision"]["rights"]["public_distribution"] is True


def test_ledger_tampering_is_detected_before_new_append(tmp_path: Path) -> None:
    batch, candidate = prepared_batch(tmp_path)
    decide(tmp_path, batch, candidate)
    ledger = tmp_path / LEDGER
    value = json.loads(ledger.read_text())
    value["reviewer"] = "Attacker"
    ledger.write_text(json.dumps(value) + "\n", encoding="utf-8")
    with pytest.raises(ExternalMonitoringError, match="identity hash"):
        lookup_review_decision(
            tmp_path, LEDGER, candidate["id"], candidate["candidate_sha256"]
        )


def test_review_rejects_batch_outside_boundary_and_symlink(tmp_path: Path) -> None:
    batch, candidate = prepared_batch(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_bytes(batch.read_bytes())
    with pytest.raises(ExternalMonitoringError, match="configured batch root"):
        decide(tmp_path, outside, candidate)
    assert not (tmp_path / LEDGER).exists()

    link = batch.parent / "external-batch-00000000000000000000.json"
    link.symlink_to(batch)
    with pytest.raises(ExternalMonitoringError, match="non-symlink"):
        decide(tmp_path, link, candidate)
    assert not (tmp_path / LEDGER).exists()
