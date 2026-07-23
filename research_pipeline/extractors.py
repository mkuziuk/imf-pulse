"""Static source extractors.  Nothing in this module executes source content."""

from __future__ import annotations

import ast
import csv
import io
import json
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable, Iterator, Mapping

from pypdf import PdfReader
from pypdf.filters import JBIG2Decode

from .errors import ExtractionError
from .hashing import canonical_json_hash
from .models import SourceConfig

SAFE_NOTEBOOK_MIME_TYPES = frozenset(
    {"text/plain", "text/markdown", "application/json"}
)
MAX_TEXT_CHARACTERS = 8_000_000
MAX_NOTEBOOK_CELLS = 10_000
MAX_NOTEBOOK_OUTPUTS = 50_000
MAX_PDF_PAGES = 2_000
MAX_TABLE_ROWS = 1_000_000
PDF_SAFETY_SCAN_CHUNK_BYTES = 64 * 1024


def _pdf_name_pattern(name: bytes) -> re.Pattern[bytes]:
    """Match a PDF name even when its bytes use ``#xx`` escapes."""

    encoded_bytes: list[bytes] = []
    for value in name:
        literal = re.escape(bytes((value,)))
        upper_escape = f"#{value:02X}".encode("ascii")
        lower_escape = upper_escape.lower()
        escape_variants = (
            re.escape(upper_escape)
            if upper_escape == lower_escape
            else b"(?:" + re.escape(upper_escape) + b"|" + re.escape(lower_escape) + b")"
        )
        encoded_bytes.append(b"(?:" + literal + b"|" + escape_variants + b")")
    return re.compile(b"/" + b"".join(encoded_bytes))


_JBIG2_DECODE_NAME = b"JBIG2Decode"
_JBIG2_DECODE_PATTERN = _pdf_name_pattern(_JBIG2_DECODE_NAME)
_JBIG2_DECODE_MAX_ENCODED_BYTES = 1 + 3 * len(_JBIG2_DECODE_NAME)
_PYPDF_JBIG2_BLOCKED_SOURCE: ContextVar[str | None] = ContextVar(
    "imf_pulse_pypdf_jbig2_blocked_source",
    default=None,
)
_PYPDF_ORIGINAL_JBIG2_DECODE = JBIG2Decode.decode


def _guarded_pypdf_jbig2_decode(
    data: bytes,
    decode_parms: Any = None,
    **kwargs: Any,
) -> bytes:
    """Prevent pypdf from launching jbig2dec in this extractor context."""

    source_id = _PYPDF_JBIG2_BLOCKED_SOURCE.get()
    if source_id is not None:
        raise ExtractionError(
            f"PDF /JBIG2Decode is disabled during static extraction: {source_id}"
        )
    return _PYPDF_ORIGINAL_JBIG2_DECODE(data, decode_parms, **kwargs)


# pypdf has no per-reader decoder hook. A permanent context-aware wrapper avoids
# temporarily mutating process-global decoder state while an extraction runs.
JBIG2Decode.decode = staticmethod(_guarded_pypdf_jbig2_decode)


@contextmanager
def _block_pypdf_jbig2_decode(source_id: str) -> Iterator[None]:
    token = _PYPDF_JBIG2_BLOCKED_SOURCE.set(source_id)
    try:
        yield
    finally:
        _PYPDF_JBIG2_BLOCKED_SOURCE.reset(token)


@dataclass(frozen=True)
class ExtractionResult:
    source_id: str
    source_sha256: str
    extractor: str
    semantic_sha256: str
    units: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...] = ()


def _unit(
    source_id: str,
    source_sha256: str,
    kind: str,
    locator: Mapping[str, Any],
    *,
    text: str | None = None,
    data: Any = None,
) -> dict[str, Any]:
    semantic = {"kind": kind, "locator": dict(locator)}
    if text is not None:
        semantic["text"] = text
    if data is not None:
        semantic["data"] = data
    unit_hash = canonical_json_hash(semantic)
    result: dict[str, Any] = {
        "schema_version": 1,
        "id": f"extract-{source_id}-{source_sha256[:12]}-{unit_hash[:20]}",
        "source_id": source_id,
        "source_sha256": source_sha256,
        "kind": kind,
        "locator": dict(locator),
        "content_sha256": unit_hash,
    }
    if text is not None:
        result["text"] = text
    if data is not None:
        result["data"] = data
    return result


def extract_source(path: Path, source: SourceConfig, source_sha256: str) -> ExtractionResult:
    extractor_key = source.extractor.split("-v", 1)[0]
    dispatch: dict[str, Callable[[Path, SourceConfig, str], tuple[list[dict[str, Any]], list[str]]]] = {
        "pdf-pages": _extract_pdf,
        "notebook-static": _extract_notebook,
        "markdown-lines": _extract_text,
        "text-lines": _extract_text,
        "tex-lines": _extract_text,
        "tex-uncompiled": _extract_text,
        "python-lines": _extract_python,
        "python-ast": _extract_python,
        "json-static": _extract_json,
        "json-units": _extract_json,
        "csv-static": _extract_csv,
        "csv-rows": _extract_csv,
    }
    extractor = dispatch.get(extractor_key)
    if extractor is None:
        raise ExtractionError(f"unsupported extractor {source.extractor!r} for {source.id}")
    try:
        units, warnings = extractor(path, source, source_sha256)
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError(f"failed to extract {source.id} with {source.extractor}: {exc}") from exc
    semantic_sha256 = canonical_json_hash(
        [
            {
                key: value
                for key, value in unit.items()
                if key not in {"id", "source_id", "source_sha256", "schema_version"}
            }
            for unit in units
        ]
    )
    return ExtractionResult(
        source_id=source.id,
        source_sha256=source_sha256,
        extractor=source.extractor,
        semantic_sha256=semantic_sha256,
        units=tuple(units),
        warnings=tuple(warnings),
    )


def _read_text(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ExtractionError(f"text source is not UTF-8: {path}") from exc
    if len(text) > MAX_TEXT_CHARACTERS:
        raise ExtractionError(f"text source exceeds extraction limit: {path}")
    return text


def _extract_text(path: Path, source: SourceConfig, source_sha256: str) -> tuple[list[dict[str, Any]], list[str]]:
    text = _read_text(path)
    line_count = len(text.splitlines())
    locator = {
        "kind": "file_lines",
        "path": source.path,
        "line_start": 1,
        "line_end": max(1, line_count),
    }
    return [_unit(source.id, source_sha256, "text", locator, text=text)], []


def _extract_python(path: Path, source: SourceConfig, source_sha256: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse Python syntax for an index without importing or executing it."""

    text = _read_text(path)
    try:
        tree = ast.parse(text, filename=source.path, mode="exec")
    except SyntaxError as exc:
        raise ExtractionError(f"cannot statically parse Python {source.id}: {exc}") from exc
    symbols = [
        {
            "kind": "class" if isinstance(node, ast.ClassDef) else "function",
            "name": node.name,
            "line_start": node.lineno,
            "line_end": getattr(node, "end_lineno", node.lineno),
        }
        for node in ast.iter_child_nodes(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    line_count = len(text.splitlines())
    return [
        _unit(
            source.id,
            source_sha256,
            "python_source",
            {
                "kind": "file_lines",
                "path": source.path,
                "line_start": 1,
                "line_end": max(1, line_count),
            },
            text=text,
        ),
        _unit(
            source.id,
            source_sha256,
            "python_symbol_index",
            {
                "kind": "file_lines",
                "path": source.path,
                "line_start": 1,
                "line_end": max(1, line_count),
            },
            data={"top_level_symbols": symbols},
        ),
    ], []


def _contains_jbig2_decode_name(handle: BinaryIO) -> bool:
    """Scan raw PDF bytes before pypdf can dispatch an external decoder."""

    overlap = _JBIG2_DECODE_MAX_ENCODED_BYTES - 1
    tail = b""
    while chunk := handle.read(PDF_SAFETY_SCAN_CHUNK_BYTES):
        candidate = tail + chunk
        if _JBIG2_DECODE_PATTERN.search(candidate) is not None:
            return True
        tail = candidate[-overlap:]
    return False


def _extract_pdf(path: Path, source: SourceConfig, source_sha256: str) -> tuple[list[dict[str, Any]], list[str]]:
    try:
        try:
            handle = path.open("rb")
        except OSError as exc:
            raise ExtractionError(f"cannot read PDF {source.id}: {exc}") from exc
        with handle, _block_pypdf_jbig2_decode(source.id):
            if _contains_jbig2_decode_name(handle):
                raise ExtractionError(
                    f"PDF contains forbidden /JBIG2Decode filter: {source.id}"
                )
            handle.seek(0)
            reader = PdfReader(handle, strict=False)
            if reader.is_encrypted:
                try:
                    unlocked = reader.decrypt("")
                except Exception as exc:
                    raise ExtractionError(f"encrypted PDF is unsupported: {source.id}") from exc
                if not unlocked:
                    raise ExtractionError(f"encrypted PDF is unsupported: {source.id}")
            if len(reader.pages) > MAX_PDF_PAGES:
                raise ExtractionError(f"PDF exceeds page limit: {source.id}")
            units: list[dict[str, Any]] = []
            warnings: list[str] = []
            for page_number, page in enumerate(reader.pages, start=1):
                try:
                    text = page.extract_text() or ""
                except Exception as exc:
                    raise ExtractionError(f"cannot extract {source.id} page {page_number}: {exc}") from exc
                if len(text) > MAX_TEXT_CHARACTERS:
                    raise ExtractionError(f"PDF page exceeds text limit: {source.id} page {page_number}")
                if not text.strip():
                    warnings.append(f"page {page_number} has no extractable text")
                units.append(
                    _unit(
                        source.id,
                        source_sha256,
                        "pdf_page",
                        {"kind": "pdf", "path": source.path, "page": page_number},
                        text=text,
                    )
                )
            return units, warnings
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError(f"cannot parse PDF {source.id}: {exc}") from exc


def _normalize_text_payload(value: Any, field_name: str) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "".join(value)
    raise ExtractionError(f"{field_name} must be a string or string array")


def _strict_json_load(handle: io.TextIOBase) -> Any:
    return json.load(
        handle,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ExtractionError(f"non-finite JSON number is forbidden: {value}")
        ),
        object_pairs_hook=_reject_duplicate_object_keys,
    )


def _reject_duplicate_object_keys(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ExtractionError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _extract_notebook(path: Path, source: SourceConfig, source_sha256: str) -> tuple[list[dict[str, Any]], list[str]]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            notebook = _strict_json_load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExtractionError(f"cannot parse notebook {source.id}: {exc}") from exc
    if not isinstance(notebook, dict) or not isinstance(notebook.get("cells"), list):
        raise ExtractionError(f"notebook has no cells list: {source.id}")
    cells = notebook["cells"]
    if len(cells) > MAX_NOTEBOOK_CELLS:
        raise ExtractionError(f"notebook exceeds cell limit: {source.id}")
    seen_cell_ids: set[str] = set()
    units: list[dict[str, Any]] = []
    warnings: list[str] = []
    output_count = 0
    for cell_index, cell in enumerate(cells):
        if not isinstance(cell, dict):
            raise ExtractionError(f"notebook cell {cell_index} is not an object")
        cell_type = cell.get("cell_type")
        if cell_type not in {"markdown", "code", "raw"}:
            raise ExtractionError(f"unsupported notebook cell type at {cell_index}: {cell_type!r}")
        cell_id = cell.get("id")
        if cell_id is not None:
            if not isinstance(cell_id, str) or not cell_id:
                raise ExtractionError(f"invalid notebook cell id at {cell_index}")
            if cell_id in seen_cell_ids:
                raise ExtractionError(f"duplicate notebook cell id: {cell_id}")
            seen_cell_ids.add(cell_id)
        source_text = _normalize_text_payload(cell.get("source", ""), f"cell {cell_index}.source")
        locator: dict[str, Any] = {
            "kind": "notebook_cell",
            "path": source.path,
            "cell_index": cell_index,
        }
        if cell_id is not None:
            locator["cell_id"] = cell_id
        units.append(
            _unit(
                source.id,
                source_sha256,
                f"notebook_{cell_type}_cell",
                locator,
                text=source_text,
            )
        )
        if cell_type != "code":
            continue
        outputs = cell.get("outputs", [])
        if not isinstance(outputs, list):
            raise ExtractionError(f"cell {cell_index}.outputs must be a list")
        for output_index, output in enumerate(outputs):
            output_count += 1
            if output_count > MAX_NOTEBOOK_OUTPUTS:
                raise ExtractionError(f"notebook exceeds output limit: {source.id}")
            if not isinstance(output, dict):
                raise ExtractionError(f"cell {cell_index} output {output_index} is not an object")
            output_type = output.get("output_type")
            base_locator = dict(locator)
            base_locator.update({"kind": "notebook_output", "output_index": output_index})
            if output_type == "stream":
                stream_text = _normalize_text_payload(
                    output.get("text", ""), f"cell {cell_index} output {output_index}.text"
                )
                stream_locator = dict(base_locator)
                stream_locator["mime"] = "text/plain"
                units.append(
                    _unit(
                        source.id,
                        source_sha256,
                        "notebook_output",
                        stream_locator,
                        text=stream_text,
                    )
                )
                continue
            if output_type in {"display_data", "execute_result"}:
                data = output.get("data", {})
                if not isinstance(data, dict):
                    raise ExtractionError(f"cell {cell_index} output {output_index}.data must be an object")
                for mime in sorted(SAFE_NOTEBOOK_MIME_TYPES.intersection(data)):
                    payload = data[mime]
                    mime_locator = dict(base_locator)
                    mime_locator["mime"] = mime
                    if mime == "application/json":
                        units.append(
                            _unit(
                                source.id,
                                source_sha256,
                                "notebook_output",
                                mime_locator,
                                data=payload,
                            )
                        )
                    else:
                        units.append(
                            _unit(
                                source.id,
                                source_sha256,
                                "notebook_output",
                                mime_locator,
                                text=_normalize_text_payload(
                                    payload,
                                    f"cell {cell_index} output {output_index} {mime}",
                                ),
                            )
                        )
                dropped = sorted(set(data) - SAFE_NOTEBOOK_MIME_TYPES)
                if dropped:
                    warnings.append(
                        f"cell {cell_index} output {output_index} dropped active/binary MIME: {', '.join(dropped)}"
                    )
                continue
            if output_type == "error":
                # Error output is inert text, but is intentionally summarized rather
                # than importing ANSI traceback metadata.
                text = f"{output.get('ename', 'Error')}: {output.get('evalue', '')}".rstrip()
                error_locator = dict(base_locator)
                error_locator["mime"] = "text/plain"
                units.append(
                    _unit(
                        source.id,
                        source_sha256,
                        "notebook_error_output",
                        error_locator,
                        text=text,
                    )
                )
                continue
            warnings.append(f"cell {cell_index} output {output_index} skipped type {output_type!r}")
    return units, warnings


def _extract_json(path: Path, source: SourceConfig, source_sha256: str) -> tuple[list[dict[str, Any]], list[str]]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = _strict_json_load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExtractionError(f"cannot parse JSON {source.id}: {exc}") from exc
    unit = _unit(
        source.id,
        source_sha256,
        "json_document",
        {"kind": "json_pointer", "path": source.path, "pointer": ""},
        data=data,
    )
    return [unit], []


def _extract_csv(path: Path, source: SourceConfig, source_sha256: str) -> tuple[list[dict[str, Any]], list[str]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            rows = list(reader)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise ExtractionError(f"cannot parse CSV {source.id}: {exc}") from exc
    if not rows:
        raise ExtractionError(f"CSV is empty: {source.id}")
    if len(rows) - 1 > MAX_TABLE_ROWS:
        raise ExtractionError(f"CSV exceeds row limit: {source.id}")
    header = rows[0]
    if len(header) != len(set(header)):
        raise ExtractionError(f"CSV has duplicate columns: {source.id}")
    width = len(header)
    for index, row in enumerate(rows[1:], start=2):
        if len(row) != width:
            raise ExtractionError(f"CSV row {index} has {len(row)} fields, expected {width}")
    data = {"columns": header, "rows": rows[1:]}
    unit = _unit(
        source.id,
        source_sha256,
        "csv_table",
        {
            "kind": "table",
            "path": source.path,
            "header_row": 1,
            "row_start": 2,
            "row_end": max(2, len(rows)),
        },
        data=data,
    )
    return [unit], []
