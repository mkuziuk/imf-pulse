"""Narrow, exact-identity arXiv PDF retrieval for the security gate.

The model never supplies a URL.  Callers provide an already validated external
batch and an exact candidate identity; this module constructs the sole allowed
URL, rejects redirects and compression, caps the response, and installs bytes
immutably in the private evidence store.
"""

from __future__ import annotations

import hashlib
import os
import stat
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping

from .external import validate_batch_integrity


MAX_BYTES = 32 * 1024 * 1024


class EvidenceFetchError(RuntimeError):
    """A classified failure while retrieving untrusted external evidence."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class EvidenceUnavailableError(EvidenceFetchError):
    """The exact candidate cannot supply acceptable primary evidence."""


class EvidenceDeferredError(EvidenceFetchError):
    """The exact candidate may be usable later after a transient failure."""


class RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(  # type: ignore[override]
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        raise EvidenceUnavailableError(
            "redirect_forbidden",
            f"arXiv PDF redirect is forbidden ({code})",
        )


def fetch_pdf_bytes(url: str) -> bytes:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "arxiv.org"
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith("/pdf/")
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("arXiv PDF URL escaped the fixed HTTPS endpoint")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/pdf",
            "Accept-Encoding": "identity",
            "User-Agent": "The-Residual/0.4 (security-gated primary evidence; arXiv only)",
        },
        method="GET",
    )
    opener = urllib.request.build_opener(RejectRedirects())
    try:
        with opener.open(request, timeout=30) as response:
            if response.status != 200 or response.geturl() != url:
                raise EvidenceUnavailableError(
                    "response_identity_changed",
                    "arXiv PDF response changed status or URL",
                )
            if response.headers.get_content_type().lower() != "application/pdf":
                raise EvidenceUnavailableError(
                    "not_pdf",
                    "arXiv evidence response is not a PDF",
                )
            if response.headers.get("Content-Encoding") not in (None, "", "identity"):
                raise EvidenceUnavailableError(
                    "compressed_response",
                    "compressed arXiv evidence responses are forbidden",
                )
            declared = response.headers.get("Content-Length")
            if declared is not None and int(declared) > MAX_BYTES:
                raise EvidenceUnavailableError(
                    "oversized",
                    "arXiv PDF exceeds the evidence size cap",
                )
            payload = response.read(MAX_BYTES + 1)
    except EvidenceFetchError:
        raise
    except urllib.error.HTTPError as exc:
        if exc.code in {404, 410}:
            raise EvidenceUnavailableError(
                "http_not_found",
                f"arXiv PDF is unavailable (HTTP {exc.code})",
            ) from exc
        if exc.code in {408, 425, 429} or 500 <= exc.code <= 599:
            raise EvidenceDeferredError(
                "http_transient",
                f"arXiv PDF request is temporarily unavailable (HTTP {exc.code})",
            ) from exc
        raise EvidenceUnavailableError(
            "http_rejected",
            f"arXiv PDF request was rejected (HTTP {exc.code})",
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise EvidenceDeferredError(
            "network_error",
            f"arXiv PDF request is temporarily unavailable: {exc}",
        ) from exc
    except ValueError as exc:
        raise EvidenceUnavailableError(
            "invalid_response",
            f"arXiv PDF response metadata is invalid: {exc}",
        ) from exc
    if not payload.startswith(b"%PDF-") or len(payload) > MAX_BYTES:
        raise EvidenceUnavailableError(
            "invalid_pdf",
            "arXiv evidence is invalid or oversized",
        )
    return payload


def install_immutable(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        if (
            path.is_symlink()
            or not stat.S_ISREG(path.stat().st_mode)
            or path.read_bytes() != payload
        ):
            raise RuntimeError("private evidence path conflicts with existing bytes")
        return
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fetch_exact_arxiv_pdf(
    project_root: Path,
    batch: Mapping[str, Any],
    candidate_id: str,
    candidate_sha256: str,
    *,
    fetcher: Callable[[str], bytes] = fetch_pdf_bytes,
) -> dict[str, Any]:
    validate_batch_integrity(batch)
    matches = [
        row
        for row in batch["candidates"]
        if row.get("id") == candidate_id
        and row.get("candidate_sha256") == candidate_sha256
    ]
    if len(matches) != 1 or matches[0].get("provider") != "arxiv":
        raise RuntimeError("exact arXiv candidate is absent or ambiguous")
    candidate = matches[0]
    versioned = str(candidate["versioned_external_id"])
    quoted = urllib.parse.quote(versioned, safe="/.")
    url = f"https://arxiv.org/pdf/{quoted}"
    payload = fetcher(url)
    digest = hashlib.sha256(payload).hexdigest()
    output = project_root / "tmp" / "automatic-evidence" / f"{digest}.pdf"
    install_immutable(output, payload)
    return {
        "status": "fetched",
        "candidate_id": candidate["id"],
        "candidate_sha256": candidate["candidate_sha256"],
        "content_sha256": digest,
        "bytes": len(payload),
        "path": output.relative_to(project_root).as_posix(),
        "logical_path": f"external/arxiv/{versioned.replace('/', '-')}.pdf",
        "source_url": candidate["canonical_url"],
        "pdf_url": url,
    }
