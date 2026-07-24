"""Bounded, metadata-only external monitoring and deterministic review helpers.

This module intentionally has no full-text download API.  Provider responses are
untrusted metadata: requests are built solely from validated configuration,
raw provider bytes are retained in a private immutable receipt, and public
batches omit abstracts and provider-supplied links.

Stable orchestration API
------------------------

``load_external_config(path)``
    Strictly validate the independent Phase 4 configuration.  Loading it does
    not enable external monitoring in the daily pipeline.

``run_external_search(config_path, project_root, as_of, ...)``
    Return a JSON-serializable result with status ``candidates_pending_review``
    or ``no_candidates`` plus immutable batch and receipt paths.

``lookup_review_decision(...)`` / ``record_review_decision(...)``
    Read or append a decision bound to an exact candidate identity hash.

``compare_knowledge_profiles(existing, candidates)``
    Produce deterministic, review-required relationship findings from explicit
    structured profiles.  No finding is promoted into curated knowledge here.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Sequence

from .config import load_yaml
from .hashing import canonical_json_bytes, canonical_json_hash
from .paths import ensure_directory_under_root, validate_relative_path
from .validation import strict_json_loads


class ExternalMonitoringError(RuntimeError):
    """An external-monitoring safety, integrity, or input error."""


class ExternalMetadataTimeout(ExternalMonitoringError):
    """A provider did not return metadata within the reviewed time limit."""


ATOM = "http://www.w3.org/2005/Atom"
ARXIV = "http://arxiv.org/schemas/atom"
IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
CATEGORY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)+$")
TERM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .+/'-]{0,119}$")
DOI_RE = re.compile(r"^10\.[0-9]{4,9}/\S+$")
MODERN_ARXIV_RE = re.compile(r"^(?P<base>\d{4}\.\d{4,5})(?:v(?P<version>[1-9]\d*))?$")
LEGACY_ARXIV_RE = re.compile(
    r"^(?P<base>[A-Za-z0-9.-]+/\d{7})(?:v(?P<version>[1-9]\d*))?$"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PROFILE_KEYS = {
    "id",
    "concept_key",
    "target_key",
    "scope_keys",
    "value_key",
    "definition_bindings",
}
CROSSREF_TYPES = {
    "book",
    "book-chapter",
    "book-section",
    "edited-book",
    "journal-article",
    "monograph",
    "proceedings-article",
    "reference-book",
}
CROSSREF_SOURCE_TYPES = {
    "journal-article": "published_primary_paper",
    "proceedings-article": "published_primary_paper",
    "book": "scholarly_book",
    "monograph": "scholarly_book",
    "edited-book": "scholarly_book",
    "reference-book": "scholarly_book",
    "book-chapter": "book_chapter",
    "book-section": "book_chapter",
}
PROVIDER_PRIORITY = {
    "crossref": 0,
    "arxiv": 1,
}


@dataclass(frozen=True)
class FetchedMetadata:
    """Exact bounded response returned by a metadata fetcher."""

    body: bytes
    content_type: str
    final_url: str
    status: int = 200
    content_encoding: str | None = None


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(  # type: ignore[override]
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        raise ExternalMonitoringError(f"metadata redirect is forbidden ({code})")


def _exact_keys(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ExternalMonitoringError(
            f"{name} has invalid fields (missing={missing}, extra={extra})"
        )


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ExternalMonitoringError(f"{name} must be a string-keyed mapping")
    return dict(value)


def _string(value: Any, name: str, *, maximum: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ExternalMonitoringError(f"{name} must be a non-empty bounded string")
    if any(ord(character) < 32 and character not in "\t\n\r" for character in value):
        raise ExternalMonitoringError(f"{name} contains control characters")
    return value.strip()


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ExternalMonitoringError(
            f"{name} must be an integer between {minimum} and {maximum}"
        )
    return value


def _number(value: Any, name: str, minimum: float, maximum: float) -> float:
    if type(value) not in (int, float) or not minimum <= float(value) <= maximum:
        raise ExternalMonitoringError(
            f"{name} must be a number between {minimum} and {maximum}"
        )
    return float(value)


def _identifier(value: Any, name: str) -> str:
    result = _string(value, name, maximum=100)
    if not IDENTIFIER_RE.fullmatch(result):
        raise ExternalMonitoringError(f"{name} must be a lowercase hyphenated identifier")
    return result


def _safe_relative(value: Any, name: str) -> str:
    try:
        return validate_relative_path(_string(value, name, maximum=500))
    except Exception as exc:
        raise ExternalMonitoringError(f"{name} must be a safe project-relative path") from exc


def _validate_https_endpoint(
    url: str, allowed_hosts: set[str], *, host: str, path: str, label: str
) -> str:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or parsed.hostname != host
        or parsed.path != path
    ):
        raise ExternalMonitoringError(
            f"{label} endpoint is outside the fixed HTTPS allowlist"
        )
    return urllib.parse.urlunsplit(("https", parsed.hostname, parsed.path, "", ""))


def load_external_config(path: Path) -> dict[str, Any]:
    """Load and strictly validate the independent metadata-monitor config.

    The function deliberately does not read or modify ``config/pulse.yaml``;
    successful preflight is capability validation, not enablement.
    """

    try:
        raw = _mapping(load_yaml(path), "external config")
    except ExternalMonitoringError:
        raise
    except Exception as exc:
        raise ExternalMonitoringError(f"cannot load external config: {exc}") from exc
    _exact_keys(raw, {"version", "policy", "providers", "queries"}, "external config")
    if raw["version"] != 1:
        raise ExternalMonitoringError("unsupported external config version")

    policy = _mapping(raw["policy"], "policy")
    policy_fields = {
        "metadata_only",
        "download_full_text",
        "allowed_schemes",
        "allowed_hosts",
        "reject_redirects",
        "max_queries",
        "max_results_per_query",
        "max_lookback_days",
        "timeout_seconds",
        "max_response_bytes",
        "minimum_request_interval_seconds",
        "user_agent",
        "private_receipt_root",
        "candidate_batch_root",
        "decision_ledger",
    }
    _exact_keys(policy, policy_fields, "policy")
    if policy["metadata_only"] is not True or policy["download_full_text"] is not False:
        raise ExternalMonitoringError("external monitoring must remain metadata-only")
    if policy["reject_redirects"] is not True:
        raise ExternalMonitoringError("metadata redirects must be rejected")
    if policy["allowed_schemes"] != ["https"]:
        raise ExternalMonitoringError("only HTTPS metadata endpoints are allowed")
    raw_hosts = policy["allowed_hosts"]
    if (
        not isinstance(raw_hosts, list)
        or not raw_hosts
        or any(not isinstance(host, str) or host != host.lower() for host in raw_hosts)
        or len(set(raw_hosts)) != len(raw_hosts)
    ):
        raise ExternalMonitoringError("allowed_hosts must be a unique lowercase host list")
    allowed_hosts = set(raw_hosts)
    approved_hosts = {"api.crossref.org", "export.arxiv.org"}
    if allowed_hosts != approved_hosts:
        raise ExternalMonitoringError(
            "literature monitoring permits only the reviewed arXiv and Crossref hosts"
        )
    max_queries = _integer(policy["max_queries"], "max_queries", 1, 8)
    max_results = _integer(
        policy["max_results_per_query"], "max_results_per_query", 1, 50
    )
    # Historical discovery is intentional for a mature research area. Keep the
    # time horizon finite while bounding network and review volume separately
    # with the query/result/response limits above.
    max_lookback = _integer(
        policy["max_lookback_days"], "max_lookback_days", 1, 36_525
    )
    timeout = _number(policy["timeout_seconds"], "timeout_seconds", 1, 30)
    max_bytes = _integer(
        policy["max_response_bytes"], "max_response_bytes", 1024, 10 * 1024 * 1024
    )
    interval = _number(
        policy["minimum_request_interval_seconds"],
        "minimum_request_interval_seconds",
        0,
        10,
    )
    user_agent = _string(policy["user_agent"], "user_agent", maximum=300)
    if "http://" in user_agent.lower() or "https://" in user_agent.lower():
        raise ExternalMonitoringError("user_agent must not embed a URL")
    private_root = _safe_relative(policy["private_receipt_root"], "private_receipt_root")
    batch_root = _safe_relative(policy["candidate_batch_root"], "candidate_batch_root")
    decision_ledger = _safe_relative(policy["decision_ledger"], "decision_ledger")
    if (
        private_root == batch_root
        or private_root.startswith(f"{batch_root}/")
        or batch_root.startswith(f"{private_root}/")
    ):
        raise ExternalMonitoringError("private receipts must be separated from public batches")
    if not private_root.startswith("tmp/"):
        raise ExternalMonitoringError("private receipts must stay under the ignored tmp/ boundary")
    if not batch_root.startswith("data/external/"):
        raise ExternalMonitoringError("candidate batches must stay under data/external/")
    if not decision_ledger.startswith("data/review/") or not decision_ledger.endswith(".jsonl"):
        raise ExternalMonitoringError("review decisions must use a data/review JSONL ledger")

    providers = _mapping(raw["providers"], "providers")
    _exact_keys(providers, {"arxiv", "crossref"}, "providers")
    arxiv = _mapping(providers["arxiv"], "providers.arxiv")
    _exact_keys(arxiv, {"endpoint", "response_media_types"}, "providers.arxiv")
    endpoint = _validate_https_endpoint(
        _string(arxiv["endpoint"], "providers.arxiv.endpoint", maximum=500),
        allowed_hosts,
        host="export.arxiv.org",
        path="/api/query",
        label="arXiv",
    )
    media_types = arxiv["response_media_types"]
    permitted_media = {"application/atom+xml", "application/xml", "text/xml"}
    if (
        not isinstance(media_types, list)
        or not media_types
        or any(item not in permitted_media for item in media_types)
        or len(set(media_types)) != len(media_types)
    ):
        raise ExternalMonitoringError("arXiv response media types are invalid")
    crossref = _mapping(providers["crossref"], "providers.crossref")
    _exact_keys(
        crossref,
        {"endpoint", "response_media_types"},
        "providers.crossref",
    )
    crossref_endpoint = _validate_https_endpoint(
        _string(
            crossref["endpoint"], "providers.crossref.endpoint", maximum=500
        ),
        allowed_hosts,
        host="api.crossref.org",
        path="/works",
        label="Crossref",
    )
    crossref_media_types = crossref["response_media_types"]
    if crossref_media_types != ["application/json"]:
        raise ExternalMonitoringError("Crossref response media types are invalid")

    queries_raw = raw["queries"]
    if not isinstance(queries_raw, list) or not 1 <= len(queries_raw) <= max_queries:
        raise ExternalMonitoringError("queries must be a non-empty bounded list")
    queries: list[dict[str, Any]] = []
    seen_query_ids: set[str] = set()
    for index, query_value in enumerate(queries_raw):
        query = _mapping(query_value, f"queries[{index}]")
        provider = query.get("provider")
        expected_query_fields = (
            {"id", "provider", "terms", "categories", "max_results", "lookback_days"}
            if provider == "arxiv"
            else {"id", "provider", "terms", "types", "max_results", "lookback_days"}
        )
        _exact_keys(query, expected_query_fields, f"queries[{index}]")
        query_id = _identifier(query["id"], f"queries[{index}].id")
        if query_id in seen_query_ids:
            raise ExternalMonitoringError(f"duplicate external query id: {query_id}")
        if provider not in {"arxiv", "crossref"}:
            raise ExternalMonitoringError("external query provider is not approved")
        terms = query["terms"]
        if (
            not isinstance(terms, list)
            or not 1 <= len(terms) <= 10
            or len(set(terms)) != len(terms)
            or any(not isinstance(term, str) or not TERM_RE.fullmatch(term) for term in terms)
        ):
            raise ExternalMonitoringError(f"query {query_id} has unsafe or duplicate terms")
        query_max = _integer(query["max_results"], f"query {query_id}.max_results", 1, max_results)
        lookback = _integer(
            query["lookback_days"], f"query {query_id}.lookback_days", 1, max_lookback
        )
        normalized_query: dict[str, Any] = {
            "id": query_id,
            "provider": provider,
            "terms": list(terms),
            "max_results": query_max,
            "lookback_days": lookback,
        }
        if provider == "arxiv":
            categories = query["categories"]
            if (
                not isinstance(categories, list)
                or not 1 <= len(categories) <= 10
                or len(set(categories)) != len(categories)
                or any(
                    not isinstance(category, str)
                    or not CATEGORY_RE.fullmatch(category)
                    for category in categories
                )
            ):
                raise ExternalMonitoringError(
                    f"query {query_id} has invalid categories"
                )
            normalized_query["categories"] = list(categories)
        else:
            work_types = query["types"]
            if (
                not isinstance(work_types, list)
                or not 1 <= len(work_types) <= len(CROSSREF_TYPES)
                or len(set(work_types)) != len(work_types)
                or any(item not in CROSSREF_TYPES for item in work_types)
            ):
                raise ExternalMonitoringError(
                    f"query {query_id} has invalid Crossref work types"
                )
            normalized_query["types"] = list(work_types)
        queries.append(normalized_query)
        seen_query_ids.add(query_id)

    return {
        "version": 1,
        "policy": {
            "metadata_only": True,
            "download_full_text": False,
            "allowed_schemes": ["https"],
            "allowed_hosts": sorted(allowed_hosts),
            "reject_redirects": True,
            "max_queries": max_queries,
            "max_results_per_query": max_results,
            "max_lookback_days": max_lookback,
            "timeout_seconds": timeout,
            "max_response_bytes": max_bytes,
            "minimum_request_interval_seconds": interval,
            "user_agent": user_agent,
            "private_receipt_root": private_root,
            "candidate_batch_root": batch_root,
            "decision_ledger": decision_ledger,
        },
        "providers": {
            "arxiv": {
                "endpoint": endpoint,
                "response_media_types": list(media_types),
            },
            "crossref": {
                "endpoint": crossref_endpoint,
                "response_media_types": list(crossref_media_types),
            },
        },
        "queries": queries,
    }


def parse_as_of(value: str) -> datetime:
    """Parse a required deterministic cutoff and normalize it to UTC seconds."""

    if not isinstance(value, str) or not value.strip():
        raise ExternalMonitoringError("--as-of is required")
    candidate = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate):
        candidate = f"{candidate}T00:00:00+00:00"
    elif candidate.endswith("Z"):
        candidate = f"{candidate[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ExternalMonitoringError("--as-of must be an ISO-8601 date or timestamp") from exc
    if parsed.tzinfo is None:
        raise ExternalMonitoringError("--as-of timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExternalMonitoringError(f"{name} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ExternalMonitoringError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def build_arxiv_request(config: Mapping[str, Any], query: Mapping[str, Any], as_of: datetime) -> str:
    """Build one bounded arXiv request entirely from validated fields."""

    provider = config["providers"]["arxiv"]
    start = as_of - timedelta(days=query["lookback_days"])
    terms = " OR ".join(f'all:"{term}"' for term in query["terms"])
    categories = " OR ".join(f"cat:{category}" for category in query["categories"])
    submitted = (
        f"submittedDate:[{start.strftime('%Y%m%d%H%M%S')} TO "
        f"{as_of.strftime('%Y%m%d%H%M%S')}]"
    )
    search_query = f"({terms}) AND ({categories}) AND {submitted}"
    query_string = urllib.parse.urlencode(
        [
            ("search_query", search_query),
            ("start", "0"),
            ("max_results", str(query["max_results"])),
            ("sortBy", "submittedDate"),
            ("sortOrder", "descending"),
        ],
        quote_via=urllib.parse.quote,
        safe="",
    )
    result = f"{provider['endpoint']}?{query_string}"
    if len(result) > 8192:
        raise ExternalMonitoringError("constructed metadata request is too long")
    return result


def build_crossref_request(
    config: Mapping[str, Any], query: Mapping[str, Any], as_of: datetime
) -> str:
    """Build one bounded Crossref works request from reviewed fields."""

    provider = config["providers"]["crossref"]
    start = (as_of - timedelta(days=query["lookback_days"])).date().isoformat()
    end = as_of.date().isoformat()
    filters = [f"from-pub-date:{start}", f"until-pub-date:{end}"]
    filters.extend(f"type:{item}" for item in query["types"])
    query_string = urllib.parse.urlencode(
        [
            ("query.title", query["terms"][0]),
            ("filter", ",".join(filters)),
            ("rows", str(query["max_results"])),
            (
                "select",
                "DOI,title,author,published,indexed,type,container-title,"
                "subject,abstract,URL,license,ISBN",
            ),
        ],
        quote_via=urllib.parse.quote,
        safe="",
    )
    result = f"{provider['endpoint']}?{query_string}"
    if len(result) > 8192:
        raise ExternalMonitoringError("constructed Crossref request is too long")
    return result


def build_metadata_request(
    config: Mapping[str, Any], query: Mapping[str, Any], as_of: datetime
) -> str:
    if query["provider"] == "arxiv":
        return build_arxiv_request(config, query, as_of)
    if query["provider"] == "crossref":
        return build_crossref_request(config, query, as_of)
    raise ExternalMonitoringError("metadata query provider is unsupported")


def fetch_metadata(
    url: str,
    *,
    timeout_seconds: float,
    max_bytes: int,
    allowed_hosts: Sequence[str],
    media_types: Sequence[str],
    user_agent: str,
) -> FetchedMetadata:
    """Fetch one Atom response with redirects, compression, and overrun blocked."""

    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in set(allowed_hosts)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
    ):
        raise ExternalMonitoringError("metadata request escaped the HTTPS host allowlist")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": ", ".join(media_types),
            "Accept-Encoding": "identity",
            "User-Agent": user_agent,
        },
        method="GET",
    )
    opener = urllib.request.build_opener(_RejectRedirects())
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            status = int(getattr(response, "status", response.getcode()))
            final_url = response.geturl()
            content_type = response.headers.get_content_type().lower()
            content_encoding = response.headers.get("Content-Encoding")
            length_header = response.headers.get("Content-Length")
            if length_header is not None:
                try:
                    declared_length = int(length_header)
                except ValueError as exc:
                    raise ExternalMonitoringError("metadata Content-Length is invalid") from exc
                if declared_length < 0 or declared_length > max_bytes:
                    raise ExternalMonitoringError("metadata response exceeds the byte limit")
            body = response.read(max_bytes + 1)
    except ExternalMonitoringError:
        raise
    except TimeoutError as exc:
        raise ExternalMetadataTimeout(f"metadata request timed out: {exc}") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise ExternalMetadataTimeout(f"metadata request timed out: {exc.reason}") from exc
        raise ExternalMonitoringError(f"metadata request failed: {exc}") from exc
    except OSError as exc:
        raise ExternalMonitoringError(f"metadata request failed: {exc}") from exc
    if status != 200:
        raise ExternalMonitoringError(f"metadata endpoint returned HTTP {status}")
    if final_url != url:
        raise ExternalMonitoringError("metadata response URL changed")
    if content_type not in set(media_types):
        raise ExternalMonitoringError(f"unexpected metadata media type: {content_type}")
    if content_encoding not in (None, "", "identity"):
        raise ExternalMonitoringError("compressed metadata responses are forbidden")
    if not body or len(body) > max_bytes:
        raise ExternalMonitoringError("metadata response is empty or exceeds the byte limit")
    return FetchedMetadata(body, content_type, final_url, status, content_encoding)


def _normalized_text(value: str | None, name: str, maximum: int) -> str:
    result = " ".join((value or "").split())
    return _string(result, name, maximum=maximum)


def _arxiv_identity(raw_id: str) -> tuple[str, str, int]:
    parsed = urllib.parse.urlsplit(raw_id.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"arxiv.org", "export.arxiv.org"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/abs/")
    ):
        raise ExternalMonitoringError("Atom entry has an invalid arXiv identifier")
    versioned = urllib.parse.unquote(parsed.path[len("/abs/") :])
    match = MODERN_ARXIV_RE.fullmatch(versioned) or LEGACY_ARXIV_RE.fullmatch(versioned)
    if match is None:
        raise ExternalMonitoringError("Atom entry has an unsupported arXiv identifier")
    base = match.group("base")
    version = int(match.group("version") or 1)
    return base.lower(), versioned, version


def _candidate_identity_payload(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: candidate[key]
        for key in (
            "schema_version",
            "id",
            "provider",
            "external_id",
            "versioned_external_id",
            "version",
            "title",
            "authors",
            "published_at",
            "updated_at",
            "categories",
            "doi",
            "canonical_url",
            "abstract_sha256",
            "source_type",
            "publication_status",
            "rights_status",
            "review_status",
        )
    }


def _parse_arxiv_entries(
    payload: bytes,
    *,
    query: Mapping[str, Any],
    response_sha256: str,
    as_of: datetime,
) -> list[dict[str, Any]]:
    upper = payload.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ExternalMonitoringError("DTD and entity declarations are forbidden in Atom metadata")
    if b"\x00" in payload:
        raise ExternalMonitoringError("Atom metadata contains a NUL byte")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ExternalMonitoringError("Atom metadata is not well-formed XML") from exc
    if root.tag != f"{{{ATOM}}}feed":
        raise ExternalMonitoringError("metadata response is not an Atom feed")
    entries = root.findall(f"{{{ATOM}}}entry")
    if len(entries) > query["max_results"]:
        raise ExternalMonitoringError("Atom feed exceeds the configured result cap")

    start = as_of - timedelta(days=query["lookback_days"])
    candidates: list[dict[str, Any]] = []
    for entry_index, entry in enumerate(entries):
        base_id, versioned_id, version = _arxiv_identity(
            _normalized_text(entry.findtext(f"{{{ATOM}}}id"), "entry id", 500)
        )
        title = _normalized_text(entry.findtext(f"{{{ATOM}}}title"), "entry title", 2000)
        summary = _normalized_text(
            entry.findtext(f"{{{ATOM}}}summary"), "entry summary", 200_000
        )
        published = _parse_timestamp(
            _normalized_text(entry.findtext(f"{{{ATOM}}}published"), "published", 100),
            "published",
        )
        updated = _parse_timestamp(
            _normalized_text(entry.findtext(f"{{{ATOM}}}updated"), "updated", 100),
            "updated",
        )
        if not start <= published <= as_of or updated > as_of:
            continue
        authors = [
            _normalized_text(author.findtext(f"{{{ATOM}}}name"), "author", 500)
            for author in entry.findall(f"{{{ATOM}}}author")
        ]
        if not authors or len(authors) > 200:
            raise ExternalMonitoringError("Atom entry has an invalid author list")
        categories = sorted(
            {
                category.get("term", "")
                for category in entry.findall(f"{{{ATOM}}}category")
                if CATEGORY_RE.fullmatch(category.get("term", ""))
            }
        )
        if not categories or not set(categories).intersection(query["categories"]):
            continue
        doi_text = entry.findtext(f"{{{ARXIV}}}doi")
        doi: str | None = None
        if doi_text and doi_text.strip():
            doi = _normalized_text(doi_text, "doi", 300).lower()
            if not re.fullmatch(r"10\.[0-9]{4,9}/\S+", doi):
                raise ExternalMonitoringError("Atom entry DOI is malformed")
        stable_id = f"candidate-arxiv-{hashlib.sha256(f'arxiv:{base_id}'.encode()).hexdigest()[:20]}"
        quoted = urllib.parse.quote(versioned_id, safe="/.")
        candidate: dict[str, Any] = {
            "schema_version": "1.0.0",
            "id": stable_id,
            "provider": "arxiv",
            "external_id": base_id,
            "versioned_external_id": versioned_id,
            "version": version,
            "title": title,
            "authors": authors,
            "published_at": _timestamp(published),
            "updated_at": _timestamp(updated),
            "categories": categories,
            "doi": doi,
            "canonical_url": f"https://arxiv.org/abs/{quoted}",
            "abstract_sha256": hashlib.sha256(summary.encode("utf-8")).hexdigest(),
            "source_type": "preprint",
            "publication_status": "preprint",
            "rights_status": "unknown",
            "review_status": "pending",
            "provenance": {
                "query_ids": [query["id"]],
                "receipts": [
                    {
                        "query_id": query["id"],
                        "response_sha256": response_sha256,
                        "entry_index": entry_index,
                    }
                ],
            },
        }
        candidate["candidate_sha256"] = canonical_json_hash(
            _candidate_identity_payload(candidate)
        )
        candidates.append(candidate)
    return candidates


def _crossref_date(value: Any, name: str) -> datetime:
    if not isinstance(value, Mapping):
        raise ExternalMonitoringError(f"Crossref {name} date is missing")
    parts_outer = value.get("date-parts")
    if (
        not isinstance(parts_outer, list)
        or not parts_outer
        or not isinstance(parts_outer[0], list)
        or not 1 <= len(parts_outer[0]) <= 3
        or any(type(item) is not int for item in parts_outer[0])
    ):
        raise ExternalMonitoringError(f"Crossref {name} date is invalid")
    parts = list(parts_outer[0]) + [1, 1]
    try:
        return datetime(parts[0], parts[1], parts[2], tzinfo=timezone.utc)
    except ValueError as exc:
        raise ExternalMonitoringError(f"Crossref {name} date is invalid") from exc


def _crossref_updated(value: Any, published: datetime) -> datetime:
    if not isinstance(value, Mapping):
        return published
    raw = value.get("date-time")
    if not isinstance(raw, str):
        return published
    updated = _parse_timestamp(raw, "Crossref indexed date")
    return max(published, updated)


def _crossref_authors(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 200:
        return []
    authors: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        given = item.get("given") if isinstance(item.get("given"), str) else ""
        family = item.get("family") if isinstance(item.get("family"), str) else ""
        name = " ".join(f"{given} {family}".split())
        if name and len(name) <= 500:
            authors.append(name)
    return authors


def _crossref_text_list(value: Any, maximum_items: int = 100) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum_items:
        return []
    return [
        " ".join(item.split())
        for item in value
        if isinstance(item, str) and item.strip()
    ]


def _search_form(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.casefold()).split())


def _crossref_relevant(item: Mapping[str, Any], terms: Sequence[str]) -> bool:
    fields: list[str] = []
    fields.extend(_crossref_text_list(item.get("title"), 10))
    fields.extend(_crossref_text_list(item.get("container-title"), 10))
    fields.extend(_crossref_text_list(item.get("subject"), 100))
    abstract = item.get("abstract")
    if isinstance(abstract, str):
        fields.append(abstract)
    searchable = _search_form(" ".join(fields))
    return any(_search_form(term) in searchable for term in terms)


def _parse_crossref_items(
    payload: bytes,
    *,
    query: Mapping[str, Any],
    response_sha256: str,
    as_of: datetime,
) -> list[dict[str, Any]]:
    try:
        value = strict_json_loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ExternalMonitoringError("Crossref metadata is not valid JSON") from exc
    if (
        not isinstance(value, Mapping)
        or value.get("status") != "ok"
        or value.get("message-type") != "work-list"
    ):
        raise ExternalMonitoringError("Crossref response status is invalid")
    message = value.get("message")
    if not isinstance(message, Mapping) or not isinstance(message.get("items"), list):
        raise ExternalMonitoringError("Crossref response has no work list")
    items = message["items"]
    if len(items) > query["max_results"]:
        raise ExternalMonitoringError("Crossref response exceeds the result cap")
    start = as_of - timedelta(days=query["lookback_days"])
    candidates: list[dict[str, Any]] = []
    for entry_index, raw_item in enumerate(items):
        if not isinstance(raw_item, Mapping):
            raise ExternalMonitoringError("Crossref work is not an object")
        if raw_item.get("type") not in query["types"]:
            continue
        if not _crossref_relevant(raw_item, query["terms"]):
            continue
        raw_doi = raw_item.get("DOI")
        if not isinstance(raw_doi, str):
            continue
        doi = raw_doi.strip().lower()
        if not DOI_RE.fullmatch(doi) or len(doi) > 300:
            continue
        titles = _crossref_text_list(raw_item.get("title"), 10)
        authors = _crossref_authors(raw_item.get("author"))
        if not titles or not authors:
            continue
        title = _normalized_text(titles[0], "Crossref title", 2000)
        published = _crossref_date(raw_item.get("published"), "published")
        updated = _crossref_updated(raw_item.get("indexed"), published)
        if not start <= published <= as_of or updated > as_of:
            continue
        work_type = str(raw_item["type"])
        source_type = CROSSREF_SOURCE_TYPES[work_type]
        abstract = raw_item.get("abstract")
        abstract_text = (
            " ".join(abstract.split()) if isinstance(abstract, str) else ""
        )
        stable_id = (
            "candidate-crossref-"
            + hashlib.sha256(f"crossref:{doi}".encode()).hexdigest()[:20]
        )
        quoted_doi = urllib.parse.quote(doi, safe="/()")
        candidate: dict[str, Any] = {
            "schema_version": "1.0.0",
            "id": stable_id,
            "provider": "crossref",
            "external_id": doi,
            "versioned_external_id": doi,
            "version": 1,
            "title": title,
            "authors": authors,
            "published_at": _timestamp(published),
            "updated_at": _timestamp(updated),
            "categories": [f"crossref.{work_type}"],
            "doi": doi,
            "canonical_url": f"https://doi.org/{quoted_doi}",
            "abstract_sha256": hashlib.sha256(
                abstract_text.encode("utf-8")
            ).hexdigest(),
            "source_type": source_type,
            "publication_status": "published",
            "rights_status": "unknown",
            "review_status": "pending",
            "provenance": {
                "query_ids": [query["id"]],
                "receipts": [
                    {
                        "query_id": query["id"],
                        "response_sha256": response_sha256,
                        "entry_index": entry_index,
                    }
                ],
            },
        }
        candidate["candidate_sha256"] = canonical_json_hash(
            _candidate_identity_payload(candidate)
        )
        candidates.append(candidate)
    return candidates


def parse_metadata_candidates(
    payload: bytes,
    *,
    query: Mapping[str, Any],
    response_sha256: str,
    as_of: datetime,
) -> list[dict[str, Any]]:
    if query["provider"] == "arxiv":
        return _parse_arxiv_entries(
            payload,
            query=query,
            response_sha256=response_sha256,
            as_of=as_of,
        )
    if query["provider"] == "crossref":
        return _parse_crossref_items(
            payload,
            query=query,
            response_sha256=response_sha256,
            as_of=as_of,
        )
    raise ExternalMonitoringError("metadata candidate provider is unsupported")


def validate_candidate_integrity(candidate: Mapping[str, Any]) -> None:
    required = {
        "schema_version",
        "id",
        "candidate_sha256",
        "provider",
        "external_id",
        "versioned_external_id",
        "version",
        "title",
        "authors",
        "published_at",
        "updated_at",
        "categories",
        "doi",
        "canonical_url",
        "abstract_sha256",
        "source_type",
        "publication_status",
        "rights_status",
        "review_status",
        "provenance",
    }
    _exact_keys(candidate, required, "external candidate")
    digest = candidate.get("candidate_sha256")
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise ExternalMonitoringError("candidate hash is malformed")
    if canonical_json_hash(_candidate_identity_payload(candidate)) != digest:
        raise ExternalMonitoringError("candidate identity hash does not match metadata")
    provider = candidate.get("provider")
    if provider not in {"arxiv", "crossref"}:
        raise ExternalMonitoringError("candidate provider is invalid")
    expected_id = (
        f"candidate-{provider}-"
        + hashlib.sha256(
            f"{provider}:{candidate.get('external_id')}".encode()
        ).hexdigest()[:20]
    )
    if candidate.get("id") != expected_id:
        raise ExternalMonitoringError(
            "candidate stable id does not match its provider identity"
        )
    if (
        candidate.get("schema_version") != "1.0.0"
        or candidate.get("rights_status") != "unknown"
        or candidate.get("review_status") != "pending"
    ):
        raise ExternalMonitoringError("candidate provider or review status is invalid")
    external_id = candidate.get("external_id")
    versioned_id = candidate.get("versioned_external_id")
    version = candidate.get("version")
    if (
        not isinstance(external_id, str)
        or not isinstance(versioned_id, str)
        or type(version) is not int
    ):
        raise ExternalMonitoringError("candidate provider identity fields are invalid")
    if provider == "arxiv":
        parsed_base, parsed_versioned, parsed_version = _arxiv_identity(
            f"https://arxiv.org/abs/{urllib.parse.quote(versioned_id, safe='/.' )}"
        )
        if (parsed_base, parsed_versioned, parsed_version) != (
            external_id,
            versioned_id,
            version,
        ):
            raise ExternalMonitoringError(
                "candidate arXiv version fields are inconsistent"
            )
        expected_url = (
            "https://arxiv.org/abs/"
            + urllib.parse.quote(versioned_id, safe="/.")
        )
        if (
            candidate.get("source_type") != "preprint"
            or candidate.get("publication_status") != "preprint"
        ):
            raise ExternalMonitoringError("arXiv candidate classification is invalid")
    else:
        if (
            external_id != versioned_id
            or version != 1
            or not DOI_RE.fullmatch(external_id)
            or candidate.get("doi") != external_id
            or candidate.get("source_type")
            not in {
                "published_primary_paper",
                "scholarly_book",
                "book_chapter",
            }
            or candidate.get("publication_status") != "published"
        ):
            raise ExternalMonitoringError("Crossref candidate identity is invalid")
        expected_url = (
            "https://doi.org/" + urllib.parse.quote(external_id, safe="/()")
        )
    if candidate.get("canonical_url") != expected_url:
        raise ExternalMonitoringError("candidate canonical URL is inconsistent")
    if not isinstance(candidate.get("title"), str) or not candidate["title"].strip() or len(candidate["title"]) > 2000:
        raise ExternalMonitoringError("candidate title is invalid")
    authors = candidate.get("authors")
    if (
        not isinstance(authors, list)
        or not 1 <= len(authors) <= 200
        or any(not isinstance(author, str) or not author.strip() or len(author) > 500 for author in authors)
    ):
        raise ExternalMonitoringError("candidate authors are invalid")
    published = _parse_timestamp(
        _string(candidate.get("published_at"), "candidate published_at", maximum=100),
        "candidate published_at",
    )
    updated = _parse_timestamp(
        _string(candidate.get("updated_at"), "candidate updated_at", maximum=100),
        "candidate updated_at",
    )
    if updated < published:
        raise ExternalMonitoringError("candidate update precedes publication")
    categories = candidate.get("categories")
    if (
        not isinstance(categories, list)
        or not categories
        or categories != sorted(set(categories))
        or any(not isinstance(category, str) or not CATEGORY_RE.fullmatch(category) for category in categories)
    ):
        raise ExternalMonitoringError("candidate categories are invalid")
    doi = candidate.get("doi")
    if doi is not None and (
        not isinstance(doi, str)
        or len(doi) > 300
        or not DOI_RE.fullmatch(doi)
    ):
        raise ExternalMonitoringError("candidate DOI is invalid")
    if not isinstance(candidate.get("abstract_sha256"), str) or not SHA256_RE.fullmatch(candidate["abstract_sha256"]):
        raise ExternalMonitoringError("candidate abstract hash is invalid")
    provenance = candidate.get("provenance")
    if not isinstance(provenance, Mapping):
        raise ExternalMonitoringError("candidate provenance is invalid")
    _exact_keys(provenance, {"query_ids", "receipts"}, "candidate provenance")
    query_ids = provenance.get("query_ids")
    receipts = provenance.get("receipts")
    if (
        not isinstance(query_ids, list)
        or not query_ids
        or query_ids != sorted(set(query_ids))
        or any(not isinstance(query_id, str) or not IDENTIFIER_RE.fullmatch(query_id) for query_id in query_ids)
        or not isinstance(receipts, list)
        or not receipts
    ):
        raise ExternalMonitoringError("candidate provenance lists are invalid")
    receipt_query_ids: set[str] = set()
    receipt_keys: set[tuple[str, str, int]] = set()
    for receipt in receipts:
        if not isinstance(receipt, Mapping):
            raise ExternalMonitoringError("candidate receipt provenance is invalid")
        _exact_keys(receipt, {"query_id", "response_sha256", "entry_index"}, "candidate receipt")
        query_id = receipt.get("query_id")
        response_sha = receipt.get("response_sha256")
        entry_index = receipt.get("entry_index")
        if (
            not isinstance(query_id, str)
            or query_id not in query_ids
            or not isinstance(response_sha, str)
            or not SHA256_RE.fullmatch(response_sha)
            or type(entry_index) is not int
            or entry_index < 0
        ):
            raise ExternalMonitoringError("candidate receipt provenance fields are invalid")
        key = (query_id, response_sha, entry_index)
        if key in receipt_keys:
            raise ExternalMonitoringError("candidate receipt provenance is duplicated")
        receipt_keys.add(key)
        receipt_query_ids.add(query_id)
    if receipt_query_ids != set(query_ids):
        raise ExternalMonitoringError("candidate query ids are not backed by receipts")


def _merge_candidates(candidates: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for candidate_value in candidates:
        candidate = dict(candidate_value)
        validate_candidate_integrity(candidate)
        current = merged.get(candidate["id"])
        if current is None or candidate["version"] > current["version"]:
            merged[candidate["id"]] = candidate
            continue
        if candidate["version"] < current["version"]:
            continue
        if candidate["candidate_sha256"] != current["candidate_sha256"]:
            raise ExternalMonitoringError(
                "same provider identity has conflicting metadata"
            )
        query_ids = sorted(
            set(current["provenance"]["query_ids"])
            | set(candidate["provenance"]["query_ids"])
        )
        receipts_by_key = {
            (item["query_id"], item["response_sha256"], item["entry_index"]): item
            for item in (
                list(current["provenance"]["receipts"])
                + list(candidate["provenance"]["receipts"])
            )
        }
        current["provenance"] = {
            "query_ids": query_ids,
            "receipts": [receipts_by_key[key] for key in sorted(receipts_by_key)],
        }
    source_priority = {
        "published_primary_paper": 0,
        "scholarly_book": 1,
        "book_chapter": 2,
        "preprint": 3,
    }
    return sorted(
        merged.values(),
        key=lambda item: (
            source_priority.get(str(item.get("source_type")), 9),
            PROVIDER_PRIORITY.get(str(item.get("provider")), 9),
            str(item.get("published_at", "")),
            str(item["id"]),
        ),
    )


def _read_regular_json(path: Path, maximum_bytes: int = 16 * 1024 * 1024) -> Any:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ExternalMonitoringError(f"cannot safely open JSON file: {path}") from exc
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > maximum_bytes:
            raise ExternalMonitoringError(f"JSON file is unsafe or too large: {path}")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(payload) > maximum_bytes:
        raise ExternalMonitoringError(f"JSON file exceeds size limit: {path}")
    try:
        return strict_json_loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ExternalMonitoringError(f"JSON file is malformed: {path}") from exc


def _seen_candidate_versions(
    project_root: Path, relative_root: str
) -> tuple[set[tuple[str, str]], dict[str, int]]:
    directory = ensure_directory_under_root(project_root, relative_root)
    result: set[tuple[str, str]] = set()
    maximum_versions: dict[str, int] = {}
    for path in sorted(directory.glob("external-batch-*.json")):
        node = os.lstat(path)
        if not stat.S_ISREG(node.st_mode):
            raise ExternalMonitoringError("external batch directory contains a non-regular node")
        batch = _read_regular_json(path)
        if not isinstance(batch, Mapping):
            raise ExternalMonitoringError("historical external batch is not an object")
        validate_batch_integrity(batch)
        for candidate in batch["candidates"]:
            result.add((candidate["id"], candidate["candidate_sha256"]))
            maximum_versions[candidate["id"]] = max(
                maximum_versions.get(candidate["id"], 0), candidate["version"]
            )
    return result, maximum_versions


def _write_immutable(
    project_root: Path,
    relative_root: str,
    filename: str,
    payload: bytes,
    *,
    private: bool,
) -> tuple[Path, bool]:
    if not filename or "/" in filename or filename in {".", ".."}:
        raise ExternalMonitoringError("immutable output filename is unsafe")
    directory = ensure_directory_under_root(project_root, relative_root)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow
    descriptor: int | None = None
    try:
        descriptor = os.open(directory / filename, flags, 0o600 if private else 0o644)
    except FileExistsError:
        existing = directory / filename
        node = os.lstat(existing)
        if not stat.S_ISREG(node.st_mode):
            raise ExternalMonitoringError("immutable output path is not a regular file")
        if node.st_size != len(payload):
            raise ExternalMonitoringError("immutable output conflicts with existing bytes")
        opened = os.open(existing, os.O_RDONLY | nofollow)
        try:
            chunks: list[bytes] = []
            while True:
                chunk = os.read(opened, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            os.close(opened)
        if b"".join(chunks) != payload:
            raise ExternalMonitoringError("immutable output conflicts with existing bytes")
        return existing, False
    except OSError as exc:
        raise ExternalMonitoringError("cannot create immutable output") from exc
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory_descriptor = os.open(
        directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
    )
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
    return directory / filename, True


def _batch_identity_payload(batch: Mapping[str, Any]) -> dict[str, Any]:
    return {
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


def validate_batch_integrity(batch: Mapping[str, Any]) -> None:
    required = {
        "schema_version",
        "id",
        "batch_sha256",
        "as_of",
        "status",
        "metadata_only",
        "queries",
        "candidates",
        "already_seen_count",
    }
    _exact_keys(batch, required, "external batch")
    if batch.get("metadata_only") is not True:
        raise ExternalMonitoringError("external batch is not metadata-only")
    if batch.get("schema_version") != "1.0.0":
        raise ExternalMonitoringError("external batch schema version is invalid")
    _parse_timestamp(_string(batch.get("as_of"), "batch as_of", maximum=100), "batch as_of")
    digest = batch.get("batch_sha256")
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise ExternalMonitoringError("batch hash is malformed")
    if canonical_json_hash(_batch_identity_payload(batch)) != digest:
        raise ExternalMonitoringError("batch identity hash does not match content")
    if batch.get("id") != f"external-batch-{digest[:20]}":
        raise ExternalMonitoringError("batch id does not match its identity hash")
    candidates = batch.get("candidates")
    if not isinstance(candidates, list):
        raise ExternalMonitoringError("batch candidates must be a list")
    candidate_ids: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise ExternalMonitoringError("batch candidate must be an object")
        validate_candidate_integrity(candidate)
        if candidate["id"] in candidate_ids:
            raise ExternalMonitoringError("batch contains duplicate candidate ids")
        candidate_ids.add(candidate["id"])
    queries = batch.get("queries")
    if not isinstance(queries, list) or not queries:
        raise ExternalMonitoringError("batch queries must be a non-empty list")
    query_by_id: dict[str, Mapping[str, Any]] = {}
    response_by_query: dict[str, str] = {}
    for query in queries:
        if not isinstance(query, Mapping):
            raise ExternalMonitoringError("batch query must be an object")
        _exact_keys(
            query,
            {
                "id",
                "provider",
                "request_url",
                "response_sha256",
                "response_size_bytes",
                "matched_count",
                "batch_candidate_count",
            },
            "batch query",
        )
        query_id = query.get("id")
        if not isinstance(query_id, str) or not IDENTIFIER_RE.fullmatch(query_id) or query_id in query_by_id:
            raise ExternalMonitoringError("batch query id is invalid or duplicated")
        request_url = query.get("request_url")
        if not isinstance(request_url, str):
            raise ExternalMonitoringError("batch request URL is invalid")
        parsed_url = urllib.parse.urlsplit(request_url)
        provider = query.get("provider")
        expected_endpoint = {
            "arxiv": ("export.arxiv.org", "/api/query"),
            "crossref": ("api.crossref.org", "/works"),
        }.get(provider)
        if (
            expected_endpoint is None
            or parsed_url.scheme != "https"
            or parsed_url.hostname != expected_endpoint[0]
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.port not in (None, 443)
            or parsed_url.path != expected_endpoint[1]
            or not parsed_url.query
            or parsed_url.fragment
        ):
            raise ExternalMonitoringError("batch request URL escaped the fixed endpoint")
        response_sha = query.get("response_sha256")
        if not isinstance(response_sha, str) or not SHA256_RE.fullmatch(response_sha):
            raise ExternalMonitoringError("batch response hash is invalid")
        for count_name in ("response_size_bytes", "matched_count", "batch_candidate_count"):
            count = query.get(count_name)
            minimum = 1 if count_name == "response_size_bytes" else 0
            if type(count) is not int or count < minimum:
                raise ExternalMonitoringError(f"batch {count_name} is invalid")
        if query["batch_candidate_count"] > query["matched_count"]:
            raise ExternalMonitoringError("batch candidate count exceeds matches")
        query_by_id[query_id] = query
        response_by_query[query_id] = response_sha
    for candidate in candidates:
        for receipt in candidate["provenance"]["receipts"]:
            query_id = receipt["query_id"]
            if query_id not in query_by_id or receipt["response_sha256"] != response_by_query[query_id]:
                raise ExternalMonitoringError("candidate provenance does not resolve to a batch query")
    for query_id, query in query_by_id.items():
        expected_new = sum(
            query_id in candidate["provenance"]["query_ids"] for candidate in candidates
        )
        if query["batch_candidate_count"] != expected_new:
            raise ExternalMonitoringError("batch query candidate count is inconsistent")
    if type(batch.get("already_seen_count")) is not int or batch["already_seen_count"] < 0:
        raise ExternalMonitoringError("batch already-seen count is invalid")
    expected_status = "candidates_pending_review" if candidates else "no_candidates"
    if batch.get("status") != expected_status:
        raise ExternalMonitoringError("batch status does not match its candidates")


Fetcher = Callable[..., FetchedMetadata | bytes]


def run_external_search(
    config_path: Path,
    project_root: Path,
    as_of: str,
    *,
    fetcher: Fetcher | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Run all fixed queries and write one immutable metadata candidate batch."""

    config = load_external_config(config_path)
    cutoff = parse_as_of(as_of)
    project_root = project_root.resolve(strict=True)
    if not project_root.is_dir():
        raise ExternalMonitoringError("project root must be a directory")
    policy = config["policy"]
    fetch = fetcher or fetch_metadata
    fetched: list[tuple[dict[str, Any], str, FetchedMetadata, str]] = []
    for index, query in enumerate(config["queries"]):
        if index:
            sleeper(policy["minimum_request_interval_seconds"])
        request_url = build_metadata_request(config, query, cutoff)
        provider = config["providers"][query["provider"]]
        try:
            response_value = fetch(
                request_url,
                timeout_seconds=policy["timeout_seconds"],
                max_bytes=policy["max_response_bytes"],
                allowed_hosts=policy["allowed_hosts"],
                media_types=provider["response_media_types"],
                user_agent=policy["user_agent"],
            )
        except ExternalMetadataTimeout as exc:
            raise ExternalMetadataTimeout(
                f"{query['provider']} query {query['id']} timed out: {exc}"
            ) from exc
        if isinstance(response_value, bytes):
            response = FetchedMetadata(
                response_value,
                (
                    "application/atom+xml"
                    if query["provider"] == "arxiv"
                    else "application/json"
                ),
                request_url,
            )
        elif isinstance(response_value, FetchedMetadata):
            response = response_value
        else:
            raise ExternalMonitoringError("metadata fetcher returned an invalid response")
        if response.status != 200 or response.final_url != request_url:
            raise ExternalMonitoringError("metadata fetcher returned an unsafe status or URL")
        if response.content_type not in provider["response_media_types"]:
            raise ExternalMonitoringError("metadata fetcher returned an unsafe media type")
        if response.content_encoding not in (None, "", "identity"):
            raise ExternalMonitoringError("metadata fetcher returned compressed bytes")
        if not response.body or len(response.body) > policy["max_response_bytes"]:
            raise ExternalMonitoringError("metadata fetcher exceeded the response cap")
        response_sha256 = hashlib.sha256(response.body).hexdigest()
        fetched.append((query, request_url, response, response_sha256))

    parsed_candidates: list[dict[str, Any]] = []
    query_records: list[dict[str, Any]] = []
    receipt_paths: list[str] = []
    per_query_candidates: dict[str, list[dict[str, Any]]] = {}
    for query, request_url, response, response_sha256 in fetched:
        candidates = parse_metadata_candidates(
            response.body,
            query=query,
            response_sha256=response_sha256,
            as_of=cutoff,
        )
        per_query_candidates[query["id"]] = candidates
        parsed_candidates.extend(candidates)
        receipt_relative = (
            f"{policy['private_receipt_root']}/{query['provider']}"
        )
        receipt_suffix = "atom" if query["provider"] == "arxiv" else "json"
        receipt_path, _ = _write_immutable(
            project_root,
            receipt_relative,
            f"{response_sha256}.{receipt_suffix}",
            response.body,
            private=True,
        )
        receipt_paths.append(receipt_path.relative_to(project_root).as_posix())
        query_records.append(
            {
                "id": query["id"],
                "provider": query["provider"],
                "request_url": request_url,
                "response_sha256": response_sha256,
                "response_size_bytes": len(response.body),
                "matched_count": len(candidates),
                "batch_candidate_count": 0,
            }
        )

    merged = _merge_candidates(parsed_candidates)
    seen, maximum_seen_versions = _seen_candidate_versions(
        project_root, policy["candidate_batch_root"]
    )
    new_candidates: list[dict[str, Any]] = []
    already_seen_count = 0
    for candidate in merged:
        identity = (candidate["id"], candidate["candidate_sha256"])
        if candidate["version"] < maximum_seen_versions.get(candidate["id"], 0):
            already_seen_count += 1
            continue
        if identity in seen:
            already_seen_count += 1
            decision = lookup_review_decision(
                project_root,
                policy["decision_ledger"],
                candidate["id"],
                candidate["candidate_sha256"],
            )
            # Carry unresolved exact identities forward so a later daily run
            # cannot silently forget a candidate merely because discovery is
            # idempotent.  Resolved identities remain deduplicated.
            if decision is not None:
                continue
        new_candidates.append(candidate)
    for record in query_records:
        record["batch_candidate_count"] = sum(
            record["id"] in candidate["provenance"]["query_ids"]
            for candidate in new_candidates
        )
    batch: dict[str, Any] = {
        "schema_version": "1.0.0",
        "as_of": _timestamp(cutoff),
        "status": "candidates_pending_review" if new_candidates else "no_candidates",
        "metadata_only": True,
        "queries": sorted(query_records, key=lambda item: item["id"]),
        "candidates": new_candidates,
        "already_seen_count": already_seen_count,
    }
    batch["batch_sha256"] = canonical_json_hash(_batch_identity_payload(batch))
    batch["id"] = f"external-batch-{batch['batch_sha256'][:20]}"
    validate_batch_integrity(batch)
    batch_path, _ = _write_immutable(
        project_root,
        policy["candidate_batch_root"],
        f"{batch['id']}.json",
        canonical_json_bytes(batch) + b"\n",
        private=False,
    )
    return {
        "status": batch["status"],
        "batch_id": batch["id"],
        "batch_path": batch_path.relative_to(project_root).as_posix(),
        "candidate_count": len(new_candidates),
        "already_seen_count": already_seen_count,
        "receipt_paths": sorted(set(receipt_paths)),
    }


def _load_batch(path: Path) -> dict[str, Any]:
    value = _read_regular_json(path)
    if not isinstance(value, Mapping):
        raise ExternalMonitoringError("external batch must be a JSON object")
    batch = dict(value)
    validate_batch_integrity(batch)
    return batch


def _validate_rights(rights: Mapping[str, Any], decision: str) -> dict[str, Any]:
    value = _mapping(rights, "rights")
    allowed = {"license", "reuse_status", "public_distribution", "notes"}
    if not {"license", "reuse_status", "public_distribution"}.issubset(value) or set(value) - allowed:
        raise ExternalMonitoringError("rights fields are incomplete or invalid")
    license_value = _string(value["license"], "rights.license", maximum=300)
    reuse = value["reuse_status"]
    permitted = {
        "internal_only",
        "unknown",
        "cleared",
        "restricted",
        "public_domain",
        "not_applicable",
    }
    if reuse not in permitted or type(value["public_distribution"]) is not bool:
        raise ExternalMonitoringError("rights reuse status or distribution flag is invalid")
    if value["public_distribution"] and reuse not in {"cleared", "public_domain"}:
        raise ExternalMonitoringError("public distribution requires cleared or public-domain rights")
    if decision == "approved" and reuse == "not_applicable":
        raise ExternalMonitoringError("approved research metadata cannot use not_applicable rights")
    result: dict[str, Any] = {
        "license": license_value,
        "reuse_status": reuse,
        "public_distribution": value["public_distribution"],
    }
    if "notes" in value:
        result["notes"] = _string(value["notes"], "rights.notes", maximum=2000)
    return result


def _decision_identity_payload(decision: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: decision[key]
        for key in (
            "schema_version",
            "batch_id",
            "candidate_id",
            "candidate_sha256",
            "decision",
            "reviewer",
            "reason",
            "decided_at",
            "rights",
        )
    }


def _validate_decision(decision: Mapping[str, Any]) -> None:
    required = {
        "schema_version",
        "id",
        "decision_sha256",
        "batch_id",
        "candidate_id",
        "candidate_sha256",
        "decision",
        "reviewer",
        "reason",
        "decided_at",
        "rights",
    }
    _exact_keys(decision, required, "external decision")
    digest = decision.get("decision_sha256")
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise ExternalMonitoringError("decision hash is malformed")
    if canonical_json_hash(_decision_identity_payload(decision)) != digest:
        raise ExternalMonitoringError("decision identity hash does not match content")
    if decision.get("id") != f"external-decision-{digest[:20]}":
        raise ExternalMonitoringError("decision id does not match its identity hash")
    if decision.get("decision") not in {"approved", "rejected"}:
        raise ExternalMonitoringError("external decision is invalid")
    if not isinstance(decision.get("batch_id"), str) or not re.fullmatch(
        r"external-batch-[0-9a-f]{20}", decision["batch_id"]
    ):
        raise ExternalMonitoringError("decision batch id is invalid")
    if not isinstance(decision.get("candidate_id"), str) or not re.fullmatch(
        r"candidate-(?:arxiv|crossref)-[0-9a-f]{20}",
        decision["candidate_id"],
    ):
        raise ExternalMonitoringError("decision candidate id is invalid")
    if not isinstance(decision.get("candidate_sha256"), str) or not SHA256_RE.fullmatch(
        decision["candidate_sha256"]
    ):
        raise ExternalMonitoringError("decision candidate hash is invalid")
    _string(decision.get("reviewer"), "decision reviewer", maximum=300)
    _string(decision.get("reason"), "decision reason", maximum=4000)
    _parse_timestamp(_string(decision.get("decided_at"), "decided_at", maximum=100), "decided_at")
    _validate_rights(_mapping(decision.get("rights"), "rights"), decision["decision"])


def _read_decision_ledger_descriptor(descriptor: int) -> list[dict[str, Any]]:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > 16 * 1024 * 1024:
            raise ExternalMonitoringError("external decision ledger exceeds size limit")
        chunks.append(chunk)
    try:
        lines = b"".join(chunks).decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ExternalMonitoringError("external decision ledger is not UTF-8") from exc
    decisions: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_keys: set[tuple[str, str]] = set()
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise ExternalMonitoringError(f"decision ledger has a blank line at {line_number}")
        try:
            raw = strict_json_loads(line)
        except ValueError as exc:
            raise ExternalMonitoringError(
                f"decision ledger line {line_number} is invalid JSON"
            ) from exc
        if not isinstance(raw, Mapping):
            raise ExternalMonitoringError("decision ledger entry must be an object")
        decision = dict(raw)
        _validate_decision(decision)
        key = (decision["candidate_id"], decision["candidate_sha256"])
        if decision["id"] in seen_ids or key in seen_keys:
            raise ExternalMonitoringError("decision ledger contains duplicate or conflicting entries")
        seen_ids.add(decision["id"])
        seen_keys.add(key)
        decisions.append(decision)
    return decisions


def _open_ledger(
    project_root: Path, ledger_relative: str, *, create: bool = True
) -> tuple[int | None, Path]:
    ledger_relative = _safe_relative(ledger_relative, "decision ledger")
    pure = PurePosixPath(ledger_relative)
    parent_relative = pure.parent.as_posix()
    candidate_path = project_root.joinpath(*pure.parts)
    if not create:
        try:
            os.lstat(candidate_path)
        except FileNotFoundError:
            return None, candidate_path
        except OSError as exc:
            raise ExternalMonitoringError("cannot inspect the decision ledger") from exc
    if parent_relative == ".":
        directory = project_root
    else:
        directory = ensure_directory_under_root(project_root, parent_relative)
    path = directory / pure.name
    flags = os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    if create:
        flags |= os.O_CREAT
    try:
        descriptor = os.open(path, flags, 0o644)
    except OSError as exc:
        raise ExternalMonitoringError("cannot safely open the decision ledger") from exc
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ExternalMonitoringError("decision ledger is not a regular file")
    return descriptor, path


def lookup_review_decision(
    project_root: Path,
    ledger_relative: str,
    candidate_id: str,
    candidate_sha256: str,
) -> dict[str, Any] | None:
    """Return the exact hash-bound decision, or ``None`` when still pending."""

    if not SHA256_RE.fullmatch(candidate_sha256):
        raise ExternalMonitoringError("candidate_sha256 is malformed")
    project_root = project_root.resolve(strict=True)
    descriptor, _ = _open_ledger(project_root, ledger_relative, create=False)
    if descriptor is None:
        return None
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        decisions = _read_decision_ledger_descriptor(descriptor)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    for decision in decisions:
        if (
            decision["candidate_id"] == candidate_id
            and decision["candidate_sha256"] == candidate_sha256
        ):
            return decision
    return None


def record_review_decision(
    *,
    project_root: Path,
    batch_path: Path,
    candidate_id: str,
    expected_candidate_sha256: str,
    decision: str,
    reviewer: str,
    reason: str,
    decided_at: str,
    rights: Mapping[str, Any],
    ledger_relative: str,
    batch_root_relative: str = "data/external/batches",
) -> dict[str, Any]:
    """Append one immutable decision after revalidating the candidate batch."""

    if decision not in {"approved", "rejected"}:
        raise ExternalMonitoringError("decision must be approved or rejected")
    if not SHA256_RE.fullmatch(expected_candidate_sha256):
        raise ExternalMonitoringError("expected candidate hash is malformed")
    project_root = project_root.resolve(strict=True)
    batch_root_relative = _safe_relative(batch_root_relative, "candidate batch root")
    expected_batch_root = ensure_directory_under_root(project_root, batch_root_relative)
    supplied_batch_path = batch_path if batch_path.is_absolute() else project_root / batch_path
    try:
        supplied_node = os.lstat(supplied_batch_path)
        resolved_batch_path = supplied_batch_path.resolve(strict=True)
    except OSError as exc:
        raise ExternalMonitoringError("candidate batch is unavailable") from exc
    if (
        stat.S_ISLNK(supplied_node.st_mode)
        or not stat.S_ISREG(supplied_node.st_mode)
        or resolved_batch_path.parent != expected_batch_root
        or not re.fullmatch(r"external-batch-[0-9a-f]{20}\.json", resolved_batch_path.name)
    ):
        raise ExternalMonitoringError(
            "candidate batch must be a non-symlink regular file in the configured batch root"
        )
    batch = _load_batch(resolved_batch_path)
    matching = [item for item in batch["candidates"] if item["id"] == candidate_id]
    if len(matching) != 1:
        raise ExternalMonitoringError("candidate is not present exactly once in the batch")
    candidate = matching[0]
    if candidate["candidate_sha256"] != expected_candidate_sha256:
        raise ExternalMonitoringError("candidate changed after review began")
    reviewer_value = _string(reviewer, "reviewer", maximum=300)
    reason_value = _string(reason, "reason", maximum=4000)
    decided = _timestamp(parse_as_of(decided_at))
    rights_value = _validate_rights(rights, decision)
    value: dict[str, Any] = {
        "schema_version": "1.0.0",
        "batch_id": batch["id"],
        "candidate_id": candidate_id,
        "candidate_sha256": expected_candidate_sha256,
        "decision": decision,
        "reviewer": reviewer_value,
        "reason": reason_value,
        "decided_at": decided,
        "rights": rights_value,
    }
    value["decision_sha256"] = canonical_json_hash(_decision_identity_payload(value))
    value["id"] = f"external-decision-{value['decision_sha256'][:20]}"
    _validate_decision(value)

    descriptor, ledger_path = _open_ledger(project_root, ledger_relative)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        existing = _read_decision_ledger_descriptor(descriptor)
        key = (candidate_id, expected_candidate_sha256)
        if any((item["candidate_id"], item["candidate_sha256"]) == key for item in existing):
            raise ExternalMonitoringError("candidate version already has a review decision")
        payload = canonical_json_bytes(value) + b"\n"
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    directory_descriptor = os.open(
        ledger_path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
    return {
        "status": "recorded",
        "decision": value,
        "ledger_path": ledger_path.relative_to(project_root).as_posix(),
    }


def _normalize_profile(value: Mapping[str, Any], label: str) -> dict[str, Any]:
    profile = _mapping(value, label)
    if set(profile) - PROFILE_KEYS or not {"id", "concept_key"}.issubset(profile):
        raise ExternalMonitoringError(f"{label} has invalid profile fields")
    profile_id = _string(profile["id"], f"{label}.id", maximum=200)
    concept = _string(profile["concept_key"], f"{label}.concept_key", maximum=300)
    target_raw = profile.get("target_key")
    target = None if target_raw is None else _string(target_raw, f"{label}.target_key", maximum=300)
    value_raw = profile.get("value_key")
    proposition = (
        None if value_raw is None else _string(value_raw, f"{label}.value_key", maximum=1000)
    )
    scope_raw = profile.get("scope_keys", [])
    if not isinstance(scope_raw, list) or any(not isinstance(item, str) for item in scope_raw):
        raise ExternalMonitoringError(f"{label}.scope_keys must be a string list")
    scopes = sorted({_string(item, f"{label}.scope_keys", maximum=300) for item in scope_raw})
    definitions_raw = profile.get("definition_bindings", {})
    definitions_map = _mapping(definitions_raw, f"{label}.definition_bindings")
    definitions: dict[str, str] = {}
    for term, definition in definitions_map.items():
        term_value = _string(term, f"{label}.definition term", maximum=300)
        definitions[term_value] = _string(
            definition, f"{label}.definition value", maximum=1000
        )
    return {
        "id": profile_id,
        "concept_key": concept,
        "target_key": target,
        "scope_keys": scopes,
        "value_key": proposition,
        "definition_bindings": {key: definitions[key] for key in sorted(definitions)},
    }


def _finding(
    kind: str,
    existing: Mapping[str, Any],
    candidate: Mapping[str, Any],
    qualification: str,
    comparison: Mapping[str, Any],
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": "1.0.0",
        "kind": kind,
        "existing_id": existing["id"],
        "candidate_id": candidate["id"],
        "concept_key": candidate["concept_key"],
        "qualification": qualification,
        "comparison": dict(comparison),
        "review_required": True,
    }
    digest = canonical_json_hash(value)
    value["finding_sha256"] = digest
    value["id"] = f"comparison-finding-{digest[:20]}"
    return value


def compare_knowledge_profiles(
    existing: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Compare explicit profiles using conservative exact-token rules.

    Tokens are supplied by reviewed extraction; this function does no language
    inference.  Different or absent scopes can never produce a contradiction.
    """

    baseline = [_normalize_profile(item, f"existing[{index}]") for index, item in enumerate(existing)]
    proposed = [_normalize_profile(item, f"candidates[{index}]") for index, item in enumerate(candidates)]
    for label, profiles in (("existing", baseline), ("candidates", proposed)):
        ids = [item["id"] for item in profiles]
        if len(ids) != len(set(ids)):
            raise ExternalMonitoringError(f"{label} profiles contain duplicate ids")

    findings: list[dict[str, Any]] = []
    seen_findings: set[str] = set()
    for old in sorted(baseline, key=lambda item: item["id"]):
        for new in sorted(proposed, key=lambda item: item["id"]):
            shared_terms = sorted(
                set(old["definition_bindings"]) & set(new["definition_bindings"])
            )
            for term in shared_terms:
                old_definition = old["definition_bindings"][term]
                new_definition = new["definition_bindings"][term]
                if old_definition != new_definition:
                    item = _finding(
                        "uses-different-definition",
                        old,
                        new,
                        f"The controlled term {term!r} has different normalized definitions.",
                        {
                            "term_key": term,
                            "existing_value": old_definition,
                            "candidate_value": new_definition,
                        },
                    )
                    if item["finding_sha256"] not in seen_findings:
                        findings.append(item)
                        seen_findings.add(item["finding_sha256"])

            if old["concept_key"] != new["concept_key"]:
                continue
            comparison_base = {
                "existing_target": old["target_key"],
                "candidate_target": new["target_key"],
                "existing_scopes": old["scope_keys"],
                "candidate_scopes": new["scope_keys"],
                "existing_value": old["value_key"],
                "candidate_value": new["value_key"],
            }
            if old["target_key"] is None or new["target_key"] is None:
                item = _finding(
                    "review-gap",
                    old,
                    new,
                    "At least one profile lacks a controlled target, so semantic compatibility requires review.",
                    {**comparison_base, "reason": "missing_target"},
                )
                findings.append(item)
                continue
            if old["target_key"] != new["target_key"]:
                item = _finding(
                    "uses-different-target",
                    old,
                    new,
                    "The profiles address different controlled targets; this is not classified as a contradiction.",
                    comparison_base,
                )
                findings.append(item)
                if not old["scope_keys"] or not new["scope_keys"]:
                    findings.append(
                        _finding(
                            "review-gap",
                            old,
                            new,
                            "At least one different-target profile lacks controlled scope keys.",
                            {**comparison_base, "reason": "missing_scope"},
                        )
                    )
                continue
            if not old["scope_keys"] or not new["scope_keys"]:
                findings.append(
                    _finding(
                        "review-gap",
                        old,
                        new,
                        "At least one profile lacks controlled scope keys; no contradiction is inferred.",
                        {**comparison_base, "reason": "missing_scope"},
                    )
                )
                continue
            if old["scope_keys"] != new["scope_keys"]:
                findings.append(
                    _finding(
                        "review-gap",
                        old,
                        new,
                        "The controlled scopes differ; no contradiction is inferred.",
                        {**comparison_base, "reason": "scope_mismatch"},
                    )
                )
                continue
            if old["value_key"] is None or new["value_key"] is None:
                findings.append(
                    _finding(
                        "review-gap",
                        old,
                        new,
                        "At least one profile lacks a normalized proposition value.",
                        {**comparison_base, "reason": "missing_value"},
                    )
                )
                continue
            if old["value_key"] != new["value_key"]:
                findings.append(
                    _finding(
                        "contradicts",
                        old,
                        new,
                        "The same concept, target, and exact controlled scope have incompatible proposition values.",
                        comparison_base,
                    )
                )

    unique = {item["finding_sha256"]: item for item in findings}
    ordered = [
        unique[key]
        for key in sorted(
            unique,
            key=lambda digest: (
                unique[digest]["kind"],
                unique[digest]["existing_id"],
                unique[digest]["candidate_id"],
                unique[digest]["comparison"].get("term_key", ""),
                digest,
            ),
        )
    ]
    return {
        "status": "findings_require_review" if ordered else "no_findings",
        "finding_count": len(ordered),
        "findings": ordered,
    }


def load_profiles(path: Path) -> list[dict[str, Any]]:
    """Load a JSON list/object or strict JSONL profile file without symlinks."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ExternalMonitoringError(f"cannot safely open profiles: {path}") from exc
    try:
        node = os.fstat(descriptor)
        if not stat.S_ISREG(node.st_mode) or node.st_size > 16 * 1024 * 1024:
            raise ExternalMonitoringError("profile input is unsafe or too large")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    try:
        text = b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExternalMonitoringError("profile input is not UTF-8") from exc
    values: Any
    if path.suffix == ".jsonl":
        values = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                values.append(strict_json_loads(line))
            except ValueError as exc:
                raise ExternalMonitoringError(
                    f"profile JSONL line {line_number} is invalid"
                ) from exc
    else:
        try:
            values = strict_json_loads(text)
        except ValueError as exc:
            raise ExternalMonitoringError("profile JSON is invalid") from exc
        if isinstance(values, Mapping) and set(values) == {"profiles"}:
            values = values["profiles"]
    if not isinstance(values, list) or any(not isinstance(item, Mapping) for item in values):
        raise ExternalMonitoringError("profile input must contain a list of objects")
    return [dict(item) for item in values]
