#!/usr/bin/env python3
"""Fetch one exact-batch arXiv PDF into the private automatic-evidence store."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from research_pipeline.external import validate_batch_integrity
from research_pipeline.validation import strict_json_loads


MAX_BYTES = 32 * 1024 * 1024


class RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        raise RuntimeError(f"arXiv PDF redirect is forbidden ({code})")


def _safe_batch(project_root: Path, value: Path) -> Path:
    path = value.resolve(strict=True)
    root = (project_root / "data" / "external" / "batches").resolve(strict=True)
    if path.parent != root or path.is_symlink() or not path.is_file():
        raise RuntimeError("batch must be a regular file in data/external/batches")
    return path


def _read_batch(path: Path) -> dict:
    if path.stat().st_size > 16 * 1024 * 1024:
        raise RuntimeError("external batch is oversized")
    value = strict_json_loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("external batch is not an object")
    validate_batch_integrity(value)
    return value


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/pdf",
            "Accept-Encoding": "identity",
            "User-Agent": "The-Residual/0.3 (automatic primary evidence; arXiv only)",
        },
        method="GET",
    )
    opener = urllib.request.build_opener(RejectRedirects())
    try:
        with opener.open(request, timeout=30) as response:
            if response.status != 200 or response.geturl() != url:
                raise RuntimeError("arXiv PDF response changed status or URL")
            if response.headers.get_content_type().lower() != "application/pdf":
                raise RuntimeError("arXiv evidence response is not a PDF")
            if response.headers.get("Content-Encoding") not in (None, "", "identity"):
                raise RuntimeError("compressed arXiv evidence responses are forbidden")
            declared = response.headers.get("Content-Length")
            if declared is not None and int(declared) > MAX_BYTES:
                raise RuntimeError("arXiv PDF exceeds the evidence size cap")
            payload = response.read(MAX_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(f"arXiv PDF request failed: {exc}") from exc
    if not payload.startswith(b"%PDF-") or len(payload) > MAX_BYTES:
        raise RuntimeError("arXiv evidence is invalid or oversized")
    return payload


def _install(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode) or path.read_bytes() != payload:
            raise RuntimeError("private evidence path conflicts with existing bytes")
        return
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--candidate-sha256", required=True)
    args = parser.parse_args(argv)
    project_root = args.project_root.resolve(strict=True)
    batch = _read_batch(_safe_batch(project_root, args.batch))
    matches = [
        row
        for row in batch["candidates"]
        if row.get("id") == args.candidate_id
        and row.get("candidate_sha256") == args.candidate_sha256
    ]
    if len(matches) != 1 or matches[0].get("provider") != "arxiv":
        raise RuntimeError("exact arXiv candidate is absent or ambiguous")
    candidate = matches[0]
    versioned = str(candidate["versioned_external_id"])
    quoted = urllib.parse.quote(versioned, safe="/.")
    url = f"https://arxiv.org/pdf/{quoted}"
    payload = _fetch(url)
    digest = hashlib.sha256(payload).hexdigest()
    output = project_root / "tmp" / "automatic-evidence" / f"{digest}.pdf"
    _install(output, payload)
    print(
        json.dumps(
            {
                "status": "fetched",
                "candidate_id": candidate["id"],
                "candidate_sha256": candidate["candidate_sha256"],
                "content_sha256": digest,
                "bytes": len(payload),
                "path": output.relative_to(project_root).as_posix(),
                "logical_path": f"external/arxiv/{versioned.replace('/', '-')}.pdf",
                "source_url": candidate["canonical_url"],
                "pdf_url": url,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
