from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf import filters as pypdf_filters

from conftest import make_source
import research_pipeline.extractors as extractors_module
from research_pipeline.errors import ExtractionError
from research_pipeline.extractors import extract_source


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_notebook_extracts_source_and_only_safe_mime_without_execution(tmp_path: Path) -> None:
    sentinel = tmp_path / "executed.txt"
    notebook_path = tmp_path / "sample.ipynb"
    notebook = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {"widgets": {"danger": True}},
        "cells": [
            {
                "id": "danger-cell",
                "cell_type": "code",
                "metadata": {},
                "execution_count": 99,
                "source": [f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('ran')\n", "raise AssertionError"],
                "outputs": [
                    {"output_type": "stream", "name": "stdout", "text": ["safe stream\n"]},
                    {
                        "output_type": "display_data",
                        "metadata": {"ignored": True},
                        "data": {
                            "text/plain": ["safe plain"],
                            "text/markdown": "**safe markdown**",
                            "application/json": {"safe": True},
                            "text/html": "<script>danger()</script>",
                            "application/javascript": "danger()",
                            "image/png": "c2VjcmV0",
                            "image/svg+xml": "<svg onload='danger()'/>",
                            "application/vnd.jupyter.widget-view+json": {"model_id": "secret"},
                        },
                    },
                ],
                "attachments": {"ignored": {"image/png": "c2VjcmV0"}},
            }
        ],
    }
    notebook_path.write_text(json.dumps(notebook), encoding="utf-8")
    source = make_source("sample.ipynb", extractor="notebook-static-v1")

    result = extract_source(notebook_path, source, _hash(notebook_path))

    assert not sentinel.exists()
    serialized = json.dumps(result.units)
    assert "Path(" in serialized
    assert "safe stream" in serialized
    assert "safe plain" in serialized
    assert "safe markdown" in serialized
    assert '"safe": true' in serialized
    assert "<script>" not in serialized
    assert "c2VjcmV0" not in serialized
    assert "onload" not in serialized
    assert any("dropped active/binary MIME" in warning for warning in result.warnings)


def test_notebook_operational_metadata_does_not_change_semantic_hash(tmp_path: Path) -> None:
    base = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {"kernel": "one"},
        "cells": [
            {
                "cell_type": "code",
                "metadata": {"collapsed": False},
                "execution_count": 1,
                "source": "x = 1",
                "outputs": [{"output_type": "stream", "name": "stdout", "text": "ok"}],
            }
        ],
    }
    first = tmp_path / "first.ipynb"
    second = tmp_path / "second.ipynb"
    first.write_text(json.dumps(base), encoding="utf-8")
    changed = json.loads(json.dumps(base))
    changed["metadata"] = {"kernel": "two"}
    changed["cells"][0]["metadata"] = {"collapsed": True}
    changed["cells"][0]["execution_count"] = 200
    second.write_text(json.dumps(changed), encoding="utf-8")
    source = make_source("sample.ipynb", extractor="notebook-static-v1")
    assert extract_source(first, source, "0" * 64).semantic_sha256 == extract_source(
        second, source, "f" * 64
    ).semantic_sha256


def test_notebook_duplicate_cell_ids_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.ipynb"
    path.write_text(
        json.dumps(
            {
                "nbformat": 4,
                "cells": [
                    {"id": "same", "cell_type": "markdown", "source": "one", "metadata": {}},
                    {"id": "same", "cell_type": "markdown", "source": "two", "metadata": {}},
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ExtractionError, match="duplicate notebook cell id"):
        extract_source(path, make_source("duplicate.ipynb", extractor="notebook-static-v1"), _hash(path))


def test_python_ast_extractor_never_imports_source(tmp_path: Path) -> None:
    sentinel = tmp_path / "imported.txt"
    path = tmp_path / "danger.py"
    path.write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('ran')\n\ndef safe_function():\n    return 1\n",
        encoding="utf-8",
    )
    result = extract_source(path, make_source("danger.py", extractor="python-ast-v1"), _hash(path))
    assert not sentinel.exists()
    assert result.units[1]["data"]["top_level_symbols"] == [
        {"kind": "function", "name": "safe_function", "line_start": 4, "line_end": 5}
    ]


def test_tex_is_literal_and_never_expanded(tmp_path: Path) -> None:
    path = tmp_path / "report.tex"
    payload = r"\input{/etc/passwd}\immediate\write18{touch /tmp/nope}"
    path.write_text(payload, encoding="utf-8")
    result = extract_source(path, make_source("report.tex", extractor="tex-uncompiled-v1"), _hash(path))
    assert result.units[0]["text"] == payload


def test_json_rejects_duplicate_keys_and_non_finite_values(tmp_path: Path) -> None:
    source = make_source("data.json", extractor="json-static-v1")
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"value": 1, "value": 2}', encoding="utf-8")
    with pytest.raises(ExtractionError, match="duplicate JSON key"):
        extract_source(duplicate, source, _hash(duplicate))
    non_finite = tmp_path / "non-finite.json"
    non_finite.write_text('{"value": NaN}', encoding="utf-8")
    with pytest.raises(ExtractionError, match="non-finite"):
        extract_source(non_finite, source, _hash(non_finite))


def test_csv_keeps_quoted_newlines_and_formula_like_values_as_strings(tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    path.write_text('name,value\n"two\nlines","=1+1"\n', encoding="utf-8")
    result = extract_source(path, make_source("data.csv", extractor="csv-static-v1"), _hash(path))
    assert result.units[0]["data"] == {"columns": ["name", "value"], "rows": [["two\nlines", "=1+1"]]}


def test_pdf_extractor_is_page_scoped_and_ignores_active_content(tmp_path: Path) -> None:
    path = tmp_path / "document.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_js("app.alert('never run')")
    writer.add_attachment("payload.txt", b"attachment must not be extracted")
    with path.open("wb") as handle:
        writer.write(handle)
    result = extract_source(path, make_source("document.pdf", extractor="pdf-pages-v1"), _hash(path))
    assert len(result.units) == 1
    assert result.units[0]["locator"]["page"] == 1
    assert "never run" not in json.dumps(result.units)
    assert "attachment must not" not in json.dumps(result.units)
    assert result.warnings == ("page 1 has no extractable text",)


@pytest.mark.parametrize("filter_name", [b"/JBIG2Decode", b"/JBIG2#44ecode"])
def test_pdf_rejects_jbig2_before_reader_or_external_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filter_name: bytes,
) -> None:
    path = tmp_path / "jbig2.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with path.open("wb") as handle:
        writer.write(handle)
    with path.open("ab") as handle:
        handle.write(b"\n% forbidden filter probe: " + filter_name + b"\n")

    reader_calls: list[object] = []
    helper_calls: list[object] = []

    def external_helper(*args: object, **kwargs: object) -> object:
        helper_calls.append((args, kwargs))
        raise AssertionError("an external helper must never run during PDF ingestion")

    def reader_that_would_dispatch_helper(*args: object, **kwargs: object) -> object:
        reader_calls.append((args, kwargs))
        return pypdf_filters.subprocess.run(["jbig2dec", "--version"])

    monkeypatch.setattr(pypdf_filters.subprocess, "run", external_helper)
    monkeypatch.setattr(extractors_module, "PdfReader", reader_that_would_dispatch_helper)

    with pytest.raises(ExtractionError, match=r"forbidden /JBIG2Decode"):
        extract_source(path, make_source("jbig2.pdf", extractor="pdf-pages-v1"), _hash(path))

    assert reader_calls == []
    assert helper_calls == []


def test_pdf_blocks_jbig2_dispatch_hidden_from_raw_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "hidden-jbig2.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with path.open("wb") as handle:
        writer.write(handle)
    assert b"JBIG2Decode" not in path.read_bytes()

    helper_calls: list[object] = []

    def external_helper(*args: object, **kwargs: object) -> object:
        helper_calls.append((args, kwargs))
        raise AssertionError("jbig2dec must be unreachable during PDF extraction")

    class HiddenJbig2Page:
        def extract_text(self) -> str:
            pypdf_filters.JBIG2Decode.decode(b"hidden compressed object payload")
            return "unreachable"

    class ReaderWithIndirectJbig2Name:
        is_encrypted = False
        pages = (HiddenJbig2Page(),)

    monkeypatch.setattr(pypdf_filters, "JBIG2DEC_BINARY", "/fake/jbig2dec")
    monkeypatch.setattr(pypdf_filters.subprocess, "run", external_helper)
    monkeypatch.setattr(
        extractors_module,
        "PdfReader",
        lambda *args, **kwargs: ReaderWithIndirectJbig2Name(),
    )

    with pytest.raises(ExtractionError, match=r"/JBIG2Decode is disabled"):
        extract_source(
            path,
            make_source("hidden-jbig2.pdf", extractor="pdf-pages-v1"),
            _hash(path),
        )

    assert helper_calls == []
    assert extractors_module._PYPDF_JBIG2_BLOCKED_SOURCE.get() is None
