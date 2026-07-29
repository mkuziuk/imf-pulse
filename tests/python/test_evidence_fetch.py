from __future__ import annotations

import urllib.error

import pytest

from research_pipeline import evidence_fetch
from research_pipeline.evidence_fetch import (
    EvidenceDeferredError,
    EvidenceUnavailableError,
    fetch_pdf_bytes,
)


class _FailingOpener:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def open(self, *_args, **_kwargs):
        raise self.error


@pytest.mark.parametrize("status", [404, 410])
def test_permanent_http_failures_are_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    error = urllib.error.HTTPError(
        "https://arxiv.org/pdf/1704.01055v2",
        status,
        "missing",
        {},
        None,
    )
    monkeypatch.setattr(
        evidence_fetch.urllib.request,
        "build_opener",
        lambda *_args: _FailingOpener(error),
    )
    with pytest.raises(EvidenceUnavailableError) as captured:
        fetch_pdf_bytes("https://arxiv.org/pdf/1704.01055v2")
    assert captured.value.reason_code == "http_not_found"


@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_http_failures_are_deferred(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    error = urllib.error.HTTPError(
        "https://arxiv.org/pdf/2607.12345v1",
        status,
        "temporary",
        {},
        None,
    )
    monkeypatch.setattr(
        evidence_fetch.urllib.request,
        "build_opener",
        lambda *_args: _FailingOpener(error),
    )
    with pytest.raises(EvidenceDeferredError) as captured:
        fetch_pdf_bytes("https://arxiv.org/pdf/2607.12345v1")
    assert captured.value.reason_code == "http_transient"
