from __future__ import annotations

import copy
import json
from pathlib import Path

import jsonschema
import pytest
import yaml

from research_pipeline.external import (
    ExternalMonitoringError,
    FetchedMetadata,
    build_arxiv_request,
    build_crossref_request,
    load_external_config,
    parse_as_of,
    run_external_search,
    validate_batch_integrity,
)


PROJECT = Path(__file__).resolve().parents[2]
CONFIG = PROJECT / "config" / "external-sources.yaml"


def atom_feed(*, title: str = "Robust iterative filtering", suffix: str = "") -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <title>arXiv Query Results</title>
  <entry>
    <id>http://arxiv.org/abs/2607.12345v2</id>
    <updated>2026-07-21T10:00:00Z</updated>
    <published>2026-07-20T10:00:00Z</published>
    <title>{title}</title>
    <summary>Primary abstract text that must not enter the public batch. {suffix}</summary>
    <author><name>Ada Example</name></author>
    <author><name>Bernhard Example</name></author>
    <category term="eess.SP" />
    <category term="stat.ME" />
    <arxiv:doi>10.1234/example.1</arxiv:doi>
    <link href="https://arxiv.org/pdf/2607.12345v2" rel="related" type="application/pdf" />
  </entry>
</feed>
""".encode()


def crossref_response(*, relevant: bool = False) -> bytes:
    items = (
        [
            {
                "DOI": "10.1234/filtering.2026.1",
                "title": ["Extension and convergence analysis of Iterative Filtering"],
                "author": [
                    {"given": "Clara", "family": "Researcher"},
                    {"given": "David", "family": "Analyst"},
                ],
                "published": {"date-parts": [[2026, 7, 20]]},
                "indexed": {"date-time": "2026-07-21T10:00:00Z"},
                "type": "journal-article",
                "container-title": ["Journal of Signal Decomposition"],
                "subject": ["Signal Processing"],
                "abstract": "A primary abstract that must remain in the private receipt.",
            },
            {
                "DOI": "10.1234/unrelated.2026.1",
                "title": ["Iterative planning for database queries"],
                "author": [{"given": "Eve", "family": "Example"}],
                "published": {"date-parts": [[2026, 7, 20]]},
                "indexed": {"date-time": "2026-07-21T10:00:00Z"},
                "type": "journal-article",
            },
        ]
        if relevant
        else []
    )
    return json.dumps(
        {"status": "ok", "message-type": "work-list", "message": {"items": items}}
    ).encode()


def run_search(project: Path, payload: bytes, *, crossref_payload: bytes | None = None):
    urls: list[str] = []

    def fetcher(url: str, **_: object) -> bytes:
        urls.append(url)
        return (
            crossref_payload or crossref_response()
            if "api.crossref.org" in url
            else payload
        )

    result = run_external_search(
        CONFIG,
        project,
        "2026-07-23T08:00:00+03:00",
        fetcher=fetcher,
        sleeper=lambda _: None,
    )
    return result, urls


def test_external_config_is_fixed_metadata_only_and_request_is_deterministic() -> None:
    config = load_external_config(CONFIG)
    assert config["policy"]["metadata_only"] is True
    assert config["policy"]["download_full_text"] is False
    assert config["policy"]["allowed_hosts"] == [
        "api.crossref.org",
        "export.arxiv.org",
    ]
    assert all(query["max_results"] <= 20 for query in config["queries"])

    cutoff = parse_as_of("2026-07-23T08:00:00+03:00")
    first = build_arxiv_request(config, config["queries"][0], cutoff)
    second = build_arxiv_request(config, config["queries"][0], cutoff)
    assert first == second
    assert first.startswith("https://export.arxiv.org/api/query?")
    decoded = __import__("urllib.parse").parse.unquote(first)
    assert "submittedDate:[19910723050000 TO 20260723050000]" in decoded
    assert "max_results=20" in decoded

    crossref = build_crossref_request(config, config["queries"][2], cutoff)
    assert crossref.startswith("https://api.crossref.org/works?")
    decoded_crossref = __import__("urllib.parse").parse.unquote(crossref)
    assert "query.title=iterative filtering" in decoded_crossref
    assert "type:journal-article" in decoded_crossref
    assert "from-pub-date:1926-07-23" in decoded_crossref


def test_external_config_rejects_an_unbounded_historical_window(
    tmp_path: Path,
) -> None:
    raw = yaml.safe_load(CONFIG.read_text())
    raw["policy"]["max_lookback_days"] = 36_526
    path = tmp_path / "external.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ExternalMonitoringError, match="max_lookback_days"):
        load_external_config(path)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("metadata_only", False),
        ("download_full_text", True),
        ("allowed_hosts", ["example.com"]),
        ("reject_redirects", False),
        ("max_results_per_query", 500),
    ],
)
def test_external_config_rejects_expanded_authority(
    tmp_path: Path, field: str, replacement: object
) -> None:
    raw = yaml.safe_load(CONFIG.read_text())
    raw["policy"][field] = replacement
    path = tmp_path / "external.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ExternalMonitoringError):
        load_external_config(path)


def test_external_config_rejects_arbitrary_endpoint(tmp_path: Path) -> None:
    raw = yaml.safe_load(CONFIG.read_text())
    raw["providers"]["arxiv"]["endpoint"] = "https://export.arxiv.org/redirect"
    path = tmp_path / "external.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ExternalMonitoringError, match="allowlist"):
        load_external_config(path)

    raw = yaml.safe_load(CONFIG.read_text())
    raw["providers"]["crossref"]["endpoint"] = "https://api.crossref.org/members"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ExternalMonitoringError, match="allowlist"):
        load_external_config(path)


def test_external_config_rejects_unreviewed_crossref_work_type(
    tmp_path: Path,
) -> None:
    raw = yaml.safe_load(CONFIG.read_text())
    raw["queries"][2]["types"] = ["dataset"]
    path = tmp_path / "external.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ExternalMonitoringError, match="work types"):
        load_external_config(path)


def test_search_writes_private_receipt_public_safe_batch_and_deduplicates(
    tmp_path: Path,
) -> None:
    first, urls = run_search(tmp_path, atom_feed())
    assert first["status"] == "candidates_pending_review"
    assert first["candidate_count"] == 1
    assert len(urls) == 6
    assert sum(url.startswith("https://export.arxiv.org/api/query?") for url in urls) == 2
    assert sum(url.startswith("https://api.crossref.org/works?") for url in urls) == 4
    assert all("/pdf/" not in url for url in urls)

    batch_path = tmp_path / first["batch_path"]
    batch = json.loads(batch_path.read_text())
    validate_batch_integrity(batch)
    candidate = batch["candidates"][0]
    assert candidate["version"] == 2
    assert candidate["canonical_url"] == "https://arxiv.org/abs/2607.12345v2"
    assert candidate["provenance"]["query_ids"] == [
        "arxiv-iterative-filtering",
        "arxiv-robust-local-estimation",
    ]
    public_bytes = batch_path.read_text()
    assert "Primary abstract text" not in public_bytes
    assert "/pdf/" not in public_bytes
    assert len(candidate["abstract_sha256"]) == 64

    receipt_paths = [tmp_path / path for path in first["receipt_paths"]]
    assert len(receipt_paths) == 2
    assert next(path for path in receipt_paths if path.suffix == ".atom").read_bytes() == atom_feed()

    candidate_schema = json.loads((PROJECT / "schemas/external-candidate.schema.json").read_text())
    batch_schema = json.loads((PROJECT / "schemas/external-batch.schema.json").read_text())
    batch_schema = copy.deepcopy(batch_schema)
    batch_schema["properties"]["candidates"]["items"] = candidate_schema
    jsonschema.Draft202012Validator(batch_schema).validate(batch)

    second, _ = run_search(tmp_path, atom_feed())
    assert second["status"] == "candidates_pending_review"
    assert second["candidate_count"] == 1
    assert second["already_seen_count"] == 1
    third, _ = run_search(tmp_path, atom_feed())
    assert third == second


def test_search_excludes_source_version_already_in_accepted_release(
    tmp_path: Path,
) -> None:
    first, _ = run_search(tmp_path, atom_feed())
    candidate = json.loads((tmp_path / first["batch_path"]).read_text())["candidates"][0]
    release_path = "data/releases/release-11111111111111111111"
    sources_path = tmp_path / release_path / "sources.jsonl"
    sources_path.parent.mkdir(parents=True)
    sources_path.write_text(
        json.dumps(
            {
                "id": "src-external-arxiv-2607-12345v2",
                "url": candidate["canonical_url"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    pointer = tmp_path / "data" / "current.json"
    pointer.write_text(json.dumps({"release_path": release_path}) + "\n", encoding="utf-8")

    second, _ = run_search(tmp_path, atom_feed())

    assert second["status"] == "no_candidates"
    assert second["candidate_count"] == 0
    assert second["already_seen_count"] == 1
    assert second["accepted_filtered_count"] == 1
    batch = json.loads((tmp_path / second["batch_path"]).read_text())
    assert batch["candidates"] == []


def test_search_keeps_new_arxiv_version_when_prior_version_is_accepted(
    tmp_path: Path,
) -> None:
    release_path = "data/releases/release-11111111111111111111"
    sources_path = tmp_path / release_path / "sources.jsonl"
    sources_path.parent.mkdir(parents=True)
    sources_path.write_text(
        json.dumps(
            {
                "id": "src-external-arxiv-2607-12345v1",
                "url": "https://arxiv.org/abs/2607.12345v1",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    pointer = tmp_path / "data" / "current.json"
    pointer.write_text(json.dumps({"release_path": release_path}) + "\n", encoding="utf-8")

    result, _ = run_search(tmp_path, atom_feed())

    assert result["candidate_count"] == 1
    assert result["accepted_filtered_count"] == 0
    batch = json.loads((tmp_path / result["batch_path"]).read_text())
    assert batch["candidates"][0]["versioned_external_id"] == "2607.12345v2"


def test_crossref_journal_candidate_is_literature_first_and_public_safe(
    tmp_path: Path,
) -> None:
    result, _ = run_search(
        tmp_path,
        b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>',
        crossref_payload=crossref_response(relevant=True),
    )
    assert result["status"] == "candidates_pending_review"
    batch_path = tmp_path / result["batch_path"]
    batch = json.loads(batch_path.read_text())
    assert len(batch["candidates"]) == 1
    candidate = batch["candidates"][0]
    assert candidate["provider"] == "crossref"
    assert candidate["source_type"] == "published_primary_paper"
    assert candidate["publication_status"] == "published"
    assert candidate["doi"] == "10.1234/filtering.2026.1"
    assert candidate["canonical_url"] == "https://doi.org/10.1234/filtering.2026.1"
    assert "primary abstract" not in batch_path.read_text().lower()
    receipts = [tmp_path / path for path in result["receipt_paths"]]
    assert any(
        "primary abstract" in path.read_text().lower()
        for path in receipts
        if path.suffix == ".json"
    )
    candidate_schema = json.loads(
        (PROJECT / "schemas/external-candidate.schema.json").read_text()
    )
    jsonschema.Draft202012Validator(candidate_schema).validate(candidate)


def test_candidate_identity_hash_changes_with_metadata_but_stable_id_does_not(
    tmp_path: Path,
) -> None:
    first, _ = run_search(tmp_path, atom_feed(title="First title"))
    first_candidate = json.loads((tmp_path / first["batch_path"]).read_text())["candidates"][0]
    second, _ = run_search(tmp_path, atom_feed(title="Corrected title", suffix="revision"))
    second_candidate = json.loads((tmp_path / second["batch_path"]).read_text())["candidates"][0]
    assert first_candidate["id"] == second_candidate["id"]
    assert first_candidate["candidate_sha256"] != second_candidate["candidate_sha256"]


@pytest.mark.parametrize(
    "declaration",
    [
        b'<!DOCTYPE feed SYSTEM "https://example.com/evil.dtd">',
        b'<!ENTITY xxe SYSTEM "file:///etc/passwd">',
    ],
)
def test_search_rejects_dtd_and_entities_without_writing_batch(
    tmp_path: Path, declaration: bytes
) -> None:
    payload = atom_feed().replace(b"<feed ", declaration + b"<feed ", 1)
    with pytest.raises(ExternalMonitoringError, match="DTD and entity"):
        run_search(tmp_path, payload)
    assert not (tmp_path / "data/external/batches").exists()


def test_search_rejects_wrong_media_redirect_compression_and_size(tmp_path: Path) -> None:
    cases = [
        FetchedMetadata(atom_feed(), "text/html", "https://export.arxiv.org/api/query?bad"),
        FetchedMetadata(atom_feed(), "application/atom+xml", "https://example.com/redirect"),
        FetchedMetadata(
            atom_feed(),
            "application/atom+xml",
            "https://export.arxiv.org/api/query?bad",
            content_encoding="gzip",
        ),
        b"x" * (2_097_152 + 1),
    ]
    for value in cases:
        def fetcher(url: str, **_: object):
            if isinstance(value, FetchedMetadata):
                return FetchedMetadata(
                    value.body,
                    value.content_type,
                    value.final_url.replace("?bad", url.partition("?")[2] and f"?{url.partition('?')[2]}"),
                    value.status,
                    value.content_encoding,
                )
            return value

        with pytest.raises(ExternalMonitoringError):
            run_external_search(
                CONFIG,
                tmp_path,
                "2026-07-23T05:00:00Z",
                fetcher=fetcher,
                sleeper=lambda _: None,
            )


def test_as_of_requires_timezone_and_future_entries_are_not_candidates(tmp_path: Path) -> None:
    with pytest.raises(ExternalMonitoringError, match="timezone"):
        parse_as_of("2026-07-23T08:00:00")
    payload = atom_feed().replace(b"2026-07-20T10:00:00Z", b"2026-07-24T10:00:00Z")
    result, _ = run_search(tmp_path, payload)
    assert result["status"] == "no_candidates"
