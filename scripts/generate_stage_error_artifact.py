#!/usr/bin/env python3
"""Build the deterministic first-stage error comparison artifact.

The source IMF repository is treated as immutable input. This generator reads
two allowlisted CSV files and hashes (but never imports or executes) the
diagnostic script that produced them. Outputs are clock-independent and can be
verified byte-for-byte with ``--check``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import sys
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Iterable


GENERATOR_VERSION = "1.0.0"
ARTIFACT_DATE = "2026-07-22"
ARTIFACT_ID = "imf-stage-error-comparison-2026-07-22"
ARTIFACT_SLUG = "stage-error-comparison"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_ROOT = PROJECT_ROOT.parent / "imf"
OUTPUT_DIR = (
    PROJECT_ROOT / "public" / "artifacts" / ARTIFACT_DATE / ARTIFACT_SLUG
)
PUBLIC_BASE = f"/artifacts/{ARTIFACT_DATE}/{ARTIFACT_SLUG}"


@dataclass(frozen=True)
class SourceDefinition:
    source_id: str
    relative_path: str
    expected_sha256: str
    role: str
    locators: tuple[str, ...]
    execution_status: str


SOURCES = (
    SourceDefinition(
        source_id="src-linear-operator-exact",
        relative_path=(
            "research/first-imf-recursive-error/diagnostics/"
            "linear_operator_exact.csv"
        ),
        expected_sha256=(
            "63e0b68261063e2ec74040111f4ddd4dcd5e614f6b30847bf2fae687f4aa82eb"
        ),
        role="exact finite-n linear operator results",
        locators=(
            "header: file line 1",
            "stages 1-9: file lines 2-10",
            "fields: stage, window_size, single_scaled_by_a_k_over_2, "
            "recursive_scaled_by_a_k_over_2, component_dc_gain, "
            "recursive_variance_fraction_of_single",
        ),
        execution_status="read as CSV only",
    ),
    SourceDefinition(
        source_id="src-seed777-controls",
        relative_path=(
            "research/first-imf-recursive-error/diagnostics/"
            "seed777_method_controls.csv"
        ),
        expected_sha256=(
            "607409a5f3deae6704eb909064ac7cde2bfe35963491cc53a4d8d1a4295b172c"
        ),
        role="stored seed-777 method-control results",
        locators=(
            "header: file line 1",
            "case=linear_gaussian_only_notebook, stages 1-9: file lines 2-10",
            "field: recursive_scaled_rmse_a_k_over_2",
        ),
        execution_status="read as CSV only",
    ),
    SourceDefinition(
        source_id="src-diagnostic-generator",
        relative_path=(
            "research/first-imf-recursive-error/diagnostics/"
            "run_numerical_diagnosis.py"
        ),
        expected_sha256=(
            "9f980f36d86dcab4d4a01947dd601d3962d8493066d21fc096bba00d91a2516d"
        ),
        role="parameter and generation provenance",
        locators=(
            "parameters: lines 23-31",
            "kernel weights: lines 77-87",
            "linear recursion: lines 99-106",
            "exact operator calculation: lines 124-179",
            "seed-777 controls: lines 330-398",
        ),
        execution_status="not executed; bytes hashed only",
    ),
)


EXACT_FIELDS = {
    "stage",
    "window_size",
    "single_scaled_by_a_k_over_2",
    "recursive_scaled_by_a_k_over_2",
    "recursive_variance_fraction_of_single",
    "component_dc_gain",
}
SEED_FIELDS = {
    "case",
    "stage",
    "window_size",
    "recursive_scaled_rmse_a_k_over_2",
}
EXPECTED_STAGES = list(range(1, 10))
EXPECTED_WINDOWS = [501, 355, 251, 177, 125, 89, 63, 45, 31]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_path(definition: SourceDefinition, source_root: Path) -> Path:
    """Resolve one fixed relative source while rejecting symlink/path escapes."""
    relative = Path(definition.relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe source path: {definition.relative_path}")

    root = source_root.resolve(strict=True)
    candidate = root
    for part in relative.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ValueError(f"source path traverses a symlink: {candidate}")
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"source escaped the allowlisted root: {resolved}") from exc
    if not resolved.is_file():
        raise ValueError(f"source is not a regular file: {resolved}")
    return resolved


def read_verified_sources(
    source_root: Path,
) -> dict[str, tuple[SourceDefinition, Path, bytes]]:
    verified: dict[str, tuple[SourceDefinition, Path, bytes]] = {}
    for definition in SOURCES:
        path = source_path(definition, source_root)
        data = path.read_bytes()
        digest = sha256_bytes(data)
        if digest != definition.expected_sha256:
            raise ValueError(
                f"source hash mismatch for {path}: expected "
                f"{definition.expected_sha256}, got {digest}"
            )
        verified[definition.source_id] = (definition, path, data)
    return verified


def parse_csv(data: bytes, required_fields: set[str], label: str) -> list[dict[str, str]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} is not UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    fields = set(reader.fieldnames or [])
    missing = required_fields - fields
    if missing:
        raise ValueError(f"{label} is missing fields: {sorted(missing)}")
    rows = list(reader)
    if not rows:
        raise ValueError(f"{label} contains no data rows")
    return rows


def finite_float(raw: str, field: str, stage: int) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field} at stage {stage}: {raw!r}") from exc
    if not math.isfinite(value):
        raise ValueError(f"non-finite {field} at stage {stage}: {raw!r}")
    return value


def indexed_stage_rows(
    rows: Iterable[dict[str, str]], label: str
) -> dict[int, dict[str, str]]:
    indexed: dict[int, dict[str, str]] = {}
    for row in rows:
        try:
            stage = int(row["stage"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid stage in {label}: {row.get('stage')!r}") from exc
        if stage in indexed:
            raise ValueError(f"duplicate stage {stage} in {label}")
        indexed[stage] = row
    if sorted(indexed) != EXPECTED_STAGES:
        raise ValueError(
            f"{label} stages differ from {EXPECTED_STAGES}: {sorted(indexed)}"
        )
    return indexed


def load_stage_data(
    verified: dict[str, tuple[SourceDefinition, Path, bytes]]
) -> list[dict[str, float | int | str]]:
    exact_source_id = "src-linear-operator-exact"
    seed_source_id = "src-seed777-controls"
    exact_rows = parse_csv(
        verified[exact_source_id][2], EXACT_FIELDS, "linear_operator_exact.csv"
    )
    seed_rows_all = parse_csv(
        verified[seed_source_id][2], SEED_FIELDS, "seed777_method_controls.csv"
    )
    seed_rows = [
        row
        for row in seed_rows_all
        if row["case"] == "linear_gaussian_only_notebook"
    ]

    exact = indexed_stage_rows(exact_rows, "linear_operator_exact.csv")
    seed = indexed_stage_rows(seed_rows, "seed777 linear Gaussian control")
    stages: list[dict[str, float | int | str]] = []
    for stage, expected_window in zip(EXPECTED_STAGES, EXPECTED_WINDOWS):
        exact_row = exact[stage]
        seed_row = seed[stage]
        exact_window = int(exact_row["window_size"])
        seed_window = int(seed_row["window_size"])
        if exact_window != expected_window or seed_window != expected_window:
            raise ValueError(
                f"window mismatch at stage {stage}: expected {expected_window}, "
                f"exact={exact_window}, seed={seed_window}"
            )

        dc_gain_raw = finite_float(exact_row["component_dc_gain"], "DC gain", stage)
        dc_gain = 1 if math.isclose(dc_gain_raw, 1.0, abs_tol=1e-12) else 0
        expected_dc_gain = 1 if stage == 1 else 0
        if dc_gain != expected_dc_gain or not math.isclose(
            dc_gain_raw, float(expected_dc_gain), abs_tol=1e-12
        ):
            raise ValueError(
                f"unexpected component DC gain at stage {stage}: {dc_gain_raw}"
            )

        stages.append(
            {
                "stage": stage,
                "window_size": expected_window,
                "stage_role": (
                    "initial_low_pass" if stage == 1 else "recursive_detail"
                ),
                "component_dc_gain": dc_gain,
                "exact_single_pass": finite_float(
                    exact_row["single_scaled_by_a_k_over_2"],
                    "single-pass scaled RMS",
                    stage,
                ),
                "exact_recursive": finite_float(
                    exact_row["recursive_scaled_by_a_k_over_2"],
                    "recursive scaled RMS",
                    stage,
                ),
                "seed777_recursive": finite_float(
                    seed_row["recursive_scaled_rmse_a_k_over_2"],
                    "seed-777 recursive scaled RMSE",
                    stage,
                ),
                "recursive_variance_fraction_of_single": finite_float(
                    exact_row["recursive_variance_fraction_of_single"],
                    "recursive variance fraction",
                    stage,
                ),
            }
        )

    if not math.isclose(
        float(stages[0]["exact_single_pass"]),
        float(stages[0]["exact_recursive"]),
        rel_tol=1e-13,
        abs_tol=1e-15,
    ):
        raise ValueError("stage-1 exact single-pass and recursive values diverge")
    return stages


def summary_statistics(stages: list[dict[str, float | int | str]]) -> dict[str, float]:
    single = [float(row["exact_single_pass"]) for row in stages]
    recursive = [float(row["exact_recursive"]) for row in stages]
    single_mean = math.fsum(single) / len(single)
    single_cv = math.sqrt(
        math.fsum((value - single_mean) ** 2 for value in single) / len(single)
    ) / single_mean
    recursive_tail_mean = math.fsum(recursive[1:]) / len(recursive[1:])
    ratio = recursive[0] / recursive_tail_mean
    expected = {
        "exact_single_pass_scaled_mean": 0.020265660781302288,
        "exact_single_pass_scaled_cv": 0.006541386191638399,
        "exact_recursive_stage1": 0.020181621597744833,
        "exact_recursive_stages2_9_mean": 0.006776132450609711,
        "exact_recursive_stage1_to_tail_ratio": 2.978339302669461,
    }
    calculated = {
        "exact_single_pass_scaled_mean": single_mean,
        "exact_single_pass_scaled_cv": single_cv,
        "exact_recursive_stage1": recursive[0],
        "exact_recursive_stages2_9_mean": recursive_tail_mean,
        "exact_recursive_stage1_to_tail_ratio": ratio,
    }
    for key, expected_value in expected.items():
        if not math.isclose(
            calculated[key], expected_value, rel_tol=2e-14, abs_tol=1e-16
        ):
            raise ValueError(
                f"derived statistic {key} changed: expected {expected_value}, "
                f"got {calculated[key]}"
            )
    return calculated


def number(value: float) -> str:
    return format(value, ".17g")


def build_normalized_csv(stages: list[dict[str, float | int | str]]) -> bytes:
    output = io.StringIO(newline="")
    fields = [
        "artifact_id",
        "stage",
        "window_size",
        "stage_role",
        "component_dc_gain",
        "series_id",
        "series_label",
        "value",
        "metric_id",
        "estimand",
        "evidence_status",
        "source_id",
        "source_locator",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    series = (
        (
            "exact_single_pass",
            "Exact single-pass",
            "exact_single_pass",
            "sqrt(E[RMSE^2])",
            "calculated_exact_under_assumptions",
            "src-linear-operator-exact",
            "single_scaled_by_a_k_over_2",
        ),
        (
            "exact_recursive",
            "Exact recursive",
            "exact_recursive",
            "sqrt(E[RMSE^2])",
            "calculated_exact_under_assumptions",
            "src-linear-operator-exact",
            "recursive_scaled_by_a_k_over_2",
        ),
        (
            "seed777_recursive",
            "Seed 777 recursive",
            "seed777_recursive",
            "realized_RMSE",
            "observed_single_realization",
            "src-seed777-controls",
            "recursive_scaled_rmse_a_k_over_2",
        ),
    )
    for row in stages:
        stage = int(row["stage"])
        for (
            series_id,
            series_label,
            value_key,
            estimand,
            evidence_status,
            source_id,
            source_field,
        ) in series:
            source_line = stage + 1
            locator = (
                f"file line {source_line}; stage={stage}; field={source_field}"
            )
            if series_id == "seed777_recursive":
                locator += "; case=linear_gaussian_only_notebook"
            writer.writerow(
                {
                    "artifact_id": ARTIFACT_ID,
                    "stage": stage,
                    "window_size": row["window_size"],
                    "stage_role": row["stage_role"],
                    "component_dc_gain": row["component_dc_gain"],
                    "series_id": series_id,
                    "series_label": series_label,
                    "value": number(float(row[value_key])),
                    "metric_id": "rms_div_a_pow_k_over_2",
                    "estimand": estimand,
                    "evidence_status": evidence_status,
                    "source_id": source_id,
                    "source_locator": locator,
                }
            )
    return output.getvalue().encode("utf-8")


def build_spec(stats: dict[str, float]) -> bytes:
    spec = {
        "artifact_id": ARTIFACT_ID,
        "data": {
            "format": "csv",
            "normalized_shape": "long",
            "url": f"{PUBLIC_BASE}/stage-error-comparison.csv",
        },
        "description": (
            "Stage-wise comparison of exact single-pass and exact recursive "
            "finite-n scaled RMS with one stored seed-777 recursive realization."
        ),
        "encoding": {
            "color": {
                "field": "series_id",
                "legend": True,
                "type": "nominal",
            },
            "detail": {
                "fields": ["estimand", "evidence_status", "source_locator"]
            },
            "x": {
                "field": "stage",
                "label": "IMF stage k",
                "sort": "ascending",
                "type": "ordinal",
            },
            "y": {
                "domain": [0.004, 0.022],
                "field": "value",
                "label": "RMS / a^(k/2)",
                "type": "quantitative",
                "zero": False,
            },
        },
        "format": "imf-pulse-declarative-chart-v1",
        "interactions": {
            "responsive": True,
            "tooltip_fields": [
                "series_label",
                "stage",
                "window_size",
                "value",
                "estimand",
                "evidence_status",
            ],
        },
        "layers": [
            {
                "filter": {"series_id": "exact_single_pass"},
                "mark": {
                    "line": "solid",
                    "marker": "circle-filled",
                    "stroke": "ink",
                },
            },
            {
                "filter": {"series_id": "exact_recursive"},
                "mark": {
                    "line": "solid",
                    "marker": "square-filled",
                    "stroke": "spectral-accent",
                },
            },
            {
                "filter": {"series_id": "seed777_recursive"},
                "mark": {
                    "line": "dash",
                    "marker": "circle-hollow",
                    "stroke": "spectral-accent",
                },
            },
        ],
        "reference_annotations": [
            {
                "label": "Initial low-pass; component DC gain = 1",
                "stage": 1,
                "type": "stage-band",
            },
            {
                "from_stage": 2,
                "label": "Recursive details; component DC gain = 0",
                "to_stage": 9,
                "type": "stage-range",
            },
        ],
        "schema_version": 1,
        "series": [
            {
                "evidence_status": "calculated_exact_under_assumptions",
                "estimand": "sqrt(E[RMSE^2])",
                "id": "exact_single_pass",
                "label": "Exact single-pass",
            },
            {
                "evidence_status": "calculated_exact_under_assumptions",
                "estimand": "sqrt(E[RMSE^2])",
                "id": "exact_recursive",
                "label": "Exact recursive",
            },
            {
                "evidence_status": "observed_single_realization",
                "estimand": "realized_RMSE",
                "id": "seed777_recursive",
                "label": "Seed 777 recursive",
            },
        ],
        "summary_statistics": {key: number(value) for key, value in stats.items()},
        "title": "The first stage is not the same filter",
    }
    return (json.dumps(spec, indent=2, sort_keys=True) + "\n").encode("utf-8")


def svg_number(value: float) -> str:
    rounded = round(value, 2)
    if math.isclose(rounded, round(rounded), abs_tol=1e-9):
        return str(int(round(rounded)))
    return f"{rounded:.2f}".rstrip("0").rstrip(".")


def build_svg(
    stages: list[dict[str, float | int | str]], stats: dict[str, float]
) -> bytes:
    width, height = 1000.0, 790.0
    left, right = 100.0, 66.0
    plot_top, plot_bottom = 245.0, 555.0
    plot_left, plot_right = left, width - right
    y_min, y_max = 0.004, 0.022

    def x_position(stage: int) -> float:
        return plot_left + (stage - 1) * (plot_right - plot_left) / 8.0

    def y_position(value: float) -> float:
        return plot_bottom - (value - y_min) / (y_max - y_min) * (
            plot_bottom - plot_top
        )

    def points(key: str) -> str:
        return " ".join(
            f"{svg_number(x_position(int(row['stage'])))},"
            f"{svg_number(y_position(float(row[key])))}"
            for row in stages
        )

    parts: list[str] = []
    parts.append('<?xml version="1.0" encoding="UTF-8"?>')
    parts.append(
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'viewBox="0 0 1000 790" width="1000" height="790" '
        'preserveAspectRatio="xMidYMid meet" role="img" '
        'aria-labelledby="chart-title chart-description" '
        'style="max-width:100%;height:auto;background:#f3eee4">'
    )
    parts.append(
        "<title id=\"chart-title\">The first stage is not the same filter</title>"
    )
    description = (
        "Line chart across IMF stages one through nine. Exact single-pass scaled "
        "RMS stays close to 0.0203. Exact recursive scaled RMS starts at 0.0202 "
        "at stage one and falls to a stages two through nine mean of 0.00678. "
        "The stored seed-777 recursive realization follows the same pattern. "
        "Stage one has DC gain one; later recursive stages have DC gain zero."
    )
    parts.append(
        f'<desc id="chart-description">{escape(description)}</desc>'
    )
    parts.append(
        f'<metadata>{{"artifact_id":"{ARTIFACT_ID}",'
        f'"data_url":"{PUBLIC_BASE}/stage-error-comparison.csv",'
        '"evidence_note":"Exact under stated assumptions; seed trace is one realization."}</metadata>'
    )
    parts.append(
        """<style>
        text { font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; fill: #222220; }
        .title { font-family: Georgia, "Times New Roman", serif; font-size: 38px; font-weight: 600; letter-spacing: -0.5px; }
        .subtitle { font-size: 18px; fill: #55534d; }
        .legend { font-size: 17px; font-weight: 650; }
        .annotation-title { font-size: 18px; font-weight: 700; }
        .annotation-note { font-size: 15px; fill: #5c5952; }
        .tick { font-size: 16px; fill: #5c5952; }
        .axis-label { font-size: 18px; font-weight: 650; }
        .finding { font-family: Georgia, "Times New Roman", serif; font-size: 22px; font-weight: 600; }
        .footnote { font-size: 14px; fill: #5c5952; }
        .series { fill: none; stroke-width: 3.2; stroke-linejoin: round; stroke-linecap: round; vector-effect: non-scaling-stroke; }
        .marker { stroke-width: 2.4; vector-effect: non-scaling-stroke; }
        @media (max-width: 600px) {
          .title { font-size: 44px; }
          .subtitle { font-size: 22px; }
          .legend, .axis-label, .annotation-title { font-size: 22px; }
          .annotation-note, .tick, .footnote { font-size: 19px; }
          .finding { font-size: 26px; }
        }
        </style>"""
    )
    parts.append('<rect width="1000" height="790" fill="#f3eee4"/>')
    parts.append(
        '<text class="title" x="64" y="58">The first stage is not the same filter</text>'
    )
    parts.append(
        '<text class="subtitle" x="64" y="91">Exact finite-n RMS after a^(k/2) scaling, with the stored seed-777 recursive trace</text>'
    )

    legend_y = 128.0
    legends = (
        (86.0, "#222220", "", "filled-circle", "Exact single-pass"),
        (360.0, "#007f86", "", "filled-square", "Exact recursive"),
        (620.0, "#007f86", "8 7", "hollow-circle", "Seed 777 recursive"),
    )
    for x, color, dash, marker, label in legends:
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        parts.append(
            f'<line x1="{svg_number(x)}" y1="{svg_number(legend_y)}" '
            f'x2="{svg_number(x + 44)}" y2="{svg_number(legend_y)}" '
            f'stroke="{color}" stroke-width="3.2"{dash_attr}/>'
        )
        marker_x = x + 22
        if marker == "filled-square":
            parts.append(
                f'<rect x="{svg_number(marker_x - 5)}" y="{svg_number(legend_y - 5)}" '
                f'width="10" height="10" rx="1" fill="{color}"/>'
            )
        else:
            fill = color if marker == "filled-circle" else "#f3eee4"
            parts.append(
                f'<circle cx="{svg_number(marker_x)}" cy="{svg_number(legend_y)}" '
                f'r="5" fill="{fill}" stroke="{color}" stroke-width="2"/>'
            )
        parts.append(
            f'<text class="legend" x="{svg_number(x + 54)}" '
            f'y="{svg_number(legend_y + 6)}">{escape(label)}</text>'
        )

    stage_one_x = x_position(1)
    stage_two_x = x_position(2)
    half_step = (stage_two_x - stage_one_x) / 2.0
    parts.append(
        f'<rect x="{svg_number(stage_one_x - half_step)}" y="174" '
        f'width="{svg_number(half_step * 2)}" height="381" '
        'rx="9" fill="#d7eeeb" opacity="0.88"/>'
    )
    parts.append(
        f'<text class="annotation-title" x="{svg_number(stage_one_x + 16)}" y="191" '
        'text-anchor="middle">Stage 1 · W₁ε</text>'
    )
    parts.append(
        f'<text class="annotation-note" x="{svg_number(stage_one_x + 16)}" y="213" '
        'text-anchor="middle">low-pass · DC gain 1</text>'
    )
    later_mid = (x_position(2) + x_position(9)) / 2.0
    parts.append(
        f'<line x1="{svg_number(x_position(2))}" y1="183" '
        f'x2="{svg_number(x_position(9))}" y2="183" '
        'stroke="#7c7870" stroke-width="1.5"/>'
    )
    for endpoint in (x_position(2), x_position(9)):
        parts.append(
            f'<line x1="{svg_number(endpoint)}" y1="177" '
            f'x2="{svg_number(endpoint)}" y2="189" '
            'stroke="#7c7870" stroke-width="1.5"/>'
        )
    parts.append(
        f'<text class="annotation-title" x="{svg_number(later_mid)}" y="207" '
        'text-anchor="middle">Stages 2–9 · recursive details</text>'
    )
    parts.append(
        f'<text class="annotation-note" x="{svg_number(later_mid)}" y="228" '
        'text-anchor="middle">composite filters · DC gain 0</text>'
    )

    y_ticks = (0.005, 0.010, 0.015, 0.020)
    for tick in y_ticks:
        y = y_position(tick)
        parts.append(
            f'<line x1="{svg_number(plot_left)}" y1="{svg_number(y)}" '
            f'x2="{svg_number(plot_right)}" y2="{svg_number(y)}" '
            'stroke="#c9c2b6" stroke-width="1" stroke-dasharray="2 6"/>'
        )
        parts.append(
            f'<text class="tick" x="{svg_number(plot_left - 14)}" '
            f'y="{svg_number(y + 5)}" text-anchor="end">{tick:.3f}</text>'
        )
    parts.append(
        f'<line x1="{svg_number(plot_left)}" y1="{svg_number(plot_bottom)}" '
        f'x2="{svg_number(plot_right)}" y2="{svg_number(plot_bottom)}" '
        'stroke="#222220" stroke-width="1.5"/>'
    )
    parts.append(
        f'<line x1="{svg_number(plot_left)}" y1="{svg_number(plot_top)}" '
        f'x2="{svg_number(plot_left)}" y2="{svg_number(plot_bottom)}" '
        'stroke="#222220" stroke-width="1.5"/>'
    )
    for stage in EXPECTED_STAGES:
        x = x_position(stage)
        parts.append(
            f'<line x1="{svg_number(x)}" y1="{svg_number(plot_bottom)}" '
            f'x2="{svg_number(x)}" y2="{svg_number(plot_bottom + 7)}" '
            'stroke="#222220" stroke-width="1.4"/>'
        )
        parts.append(
            f'<text class="tick" x="{svg_number(x)}" '
            f'y="{svg_number(plot_bottom + 28)}" text-anchor="middle">{stage}</text>'
        )
    parts.append(
        f'<text class="axis-label" x="{svg_number((plot_left + plot_right) / 2)}" '
        f'y="{svg_number(plot_bottom + 58)}" text-anchor="middle">IMF stage k</text>'
    )
    axis_mid_y = (plot_top + plot_bottom) / 2.0
    parts.append(
        f'<text class="axis-label" x="28" y="{svg_number(axis_mid_y)}" '
        f'text-anchor="middle" transform="rotate(-90 28 {svg_number(axis_mid_y)})">'
        'RMS / a^(k/2)</text>'
    )

    parts.append(
        f'<polyline class="series" points="{points("exact_single_pass")}" '
        'stroke="#222220"/>'
    )
    parts.append(
        f'<polyline class="series" points="{points("exact_recursive")}" '
        'stroke="#007f86"/>'
    )
    parts.append(
        f'<polyline class="series" points="{points("seed777_recursive")}" '
        'stroke="#007f86" stroke-dasharray="8 7" opacity="0.9"/>'
    )
    for row in stages:
        stage = int(row["stage"])
        x = x_position(stage)
        y_single = y_position(float(row["exact_single_pass"]))
        y_recursive = y_position(float(row["exact_recursive"]))
        y_seed = y_position(float(row["seed777_recursive"]))
        parts.append(
            f'<circle class="marker" cx="{svg_number(x)}" cy="{svg_number(y_single)}" '
            'r="5" fill="#222220" stroke="#222220"/>'
        )
        parts.append(
            f'<rect class="marker" x="{svg_number(x - 5)}" y="{svg_number(y_recursive - 5)}" '
            'width="10" height="10" rx="1" fill="#007f86" stroke="#007f86"/>'
        )
        parts.append(
            f'<circle class="marker" cx="{svg_number(x)}" cy="{svg_number(y_seed)}" '
            'r="5.5" fill="#f3eee4" stroke="#007f86"/>'
        )

    ratio = stats["exact_recursive_stage1_to_tail_ratio"]
    cv_percent = stats["exact_single_pass_scaled_cv"] * 100.0
    parts.append(
        '<line x1="64" y1="633" x2="936" y2="633" '
        'stroke="#c9c2b6" stroke-width="1"/>'
    )
    parts.append(
        f'<text class="finding" x="64" y="674">Stage 1 is {ratio:.2f}× the exact recursive stages 2–9 mean.</text>'
    )
    parts.append(
        f'<text class="finding" x="64" y="705">Exact single-pass variation is only {cv_percent:.2f}%.</text>'
    )
    parts.append(
        '<text class="footnote" x="64" y="738">Exact curves: iid homoscedastic noise, σ=0.4, n=1,000, periodic design.</text>'
    )
    parts.append(
        '<text class="footnote" x="64" y="762">Seed trace: one realization, not a confidence band.</text>'
    )
    parts.append("</svg>")
    return ("\n".join(parts) + "\n").encode("utf-8")


def source_manifest_entries(
    verified: dict[str, tuple[SourceDefinition, Path, bytes]]
) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for definition in SOURCES:
        _, _, data = verified[definition.source_id]
        entries.append(
            {
                "content_sha256": sha256_bytes(data),
                "execution_status": definition.execution_status,
                "locators": list(definition.locators),
                "relative_path": definition.relative_path,
                "rights_status": "internal project source; use governed by project owner",
                "role": definition.role,
                "source_root_alias": "imf",
                "source_id": definition.source_id,
            }
        )
    return entries


def build_manifest(
    verified: dict[str, tuple[SourceDefinition, Path, bytes]],
    stats: dict[str, float],
    generated: dict[str, bytes],
) -> bytes:
    script_bytes = Path(__file__).resolve().read_bytes()
    files = []
    roles = {
        "stage-error-comparison.csv": "normalized underlying data",
        "stage-error-comparison.spec.json": "declarative chart specification",
        "stage-error-comparison.svg": "responsive accessible rendered chart",
    }
    media_types = {
        "stage-error-comparison.csv": "text/csv",
        "stage-error-comparison.spec.json": "application/json",
        "stage-error-comparison.svg": "image/svg+xml",
    }
    for filename in sorted(generated):
        content = generated[filename]
        files.append(
            {
                "bytes": len(content),
                "media_type": media_types[filename],
                "role": roles[filename],
                "sha256": sha256_bytes(content),
                "url": f"{PUBLIC_BASE}/{filename}",
            }
        )

    manifest = {
        "artifact_date": ARTIFACT_DATE,
        "artifact_id": ARTIFACT_ID,
        "artifact_type": "scientific_chart",
        "caption": (
            "The initial low-pass component retains DC and has a much larger "
            "scaled recursive error than later zero-DC detail components; the "
            "single-pass control remains nearly flat."
        ),
        "evidence": [
            {
                "confidence": "high within stated assumptions",
                "source_id": "src-linear-operator-exact",
                "source_locator": (
                    "linear_operator_exact.csv file lines 2-10; "
                    "field=single_scaled_by_a_k_over_2"
                ),
                "statement": (
                    "Exact single-pass scaled RMS has coefficient of variation "
                    f"{stats['exact_single_pass_scaled_cv'] * 100:.3f}% across stages 1-9."
                ),
                "status": "calculated_exact_under_assumptions",
            },
            {
                "confidence": "high within stated assumptions",
                "source_id": "src-linear-operator-exact",
                "source_locator": (
                    "linear_operator_exact.csv file lines 2-10; "
                    "field=recursive_scaled_by_a_k_over_2"
                ),
                "statement": (
                    "Exact recursive stage-1 scaled RMS is "
                    f"{stats['exact_recursive_stage1_to_tail_ratio']:.3f} times "
                    "the stages 2-9 mean."
                ),
                "status": "calculated_exact_under_assumptions",
            },
            {
                "confidence": "single-run observation only",
                "source_id": "src-seed777-controls",
                "source_locator": (
                    "seed777_method_controls.csv file lines 2-10; "
                    "case=linear_gaussian_only_notebook; "
                    "field=recursive_scaled_rmse_a_k_over_2"
                ),
                "statement": (
                    "The stored seed-777 linear Gaussian realization follows "
                    "the same first-stage-versus-tail shape."
                ),
                "status": "observed_single_realization",
            },
        ],
        "files": files,
        "generator": {
            "clock_dependent_fields": [],
            "deterministic": True,
            "language": "Python standard library",
            "path": "scripts/generate_stage_error_artifact.py",
            "sha256": sha256_bytes(script_bytes),
            "source_files_executed": False,
            "version": GENERATOR_VERSION,
        },
        "limitations": [
            "Exact curves are finite-n calculations for iid homoscedastic noise, sigma=0.4, n=1000, periodic wrap boundaries, the recorded kernel, and the fixed nine-window schedule.",
            "The seed-777 curve is one realized Gaussian sample, not an uncertainty interval or population estimate.",
            "The plotted a^(k/2) divisor is a geometric rate normalization, not exact stage-specific operator standardization.",
            "The chart does not establish a robust-recursion theorem and does not address supremum-error normalization.",
            "Different observation models, targets, boundaries, covariance structures, or schedules may produce different constants.",
            "This project-generated chart is approved for local display; public deployment requires explicit owner approval.",
        ],
        "manifest_url": f"{PUBLIC_BASE}/manifest.json",
        "parameters": {
            "a": "sqrt(2)",
            "boundary": "wrap (periodic)",
            "metric": "RMS / a^(k/2)",
            "n": 1000,
            "seed_for_realized_trace": 777,
            "sigma": 0.4,
            "window_sizes": EXPECTED_WINDOWS,
        },
        "rights": {
            "local_display_allowed": True,
            "may_publish_publicly": False,
            "public_deployment_requires_owner_approval": True,
            "status": "project_generated_scientific_chart",
        },
        "schema_version": 1,
        "sources": source_manifest_entries(verified),
        "spec_url": f"{PUBLIC_BASE}/stage-error-comparison.spec.json",
        "stable_url": f"{PUBLIC_BASE}/stage-error-comparison.svg",
        "title": "The first stage is not the same filter",
    }
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")


def build_outputs(source_root: Path = DEFAULT_SOURCE_ROOT) -> dict[str, bytes]:
    verified = read_verified_sources(source_root)
    stages = load_stage_data(verified)
    stats = summary_statistics(stages)
    generated = {
        "stage-error-comparison.csv": build_normalized_csv(stages),
        "stage-error-comparison.spec.json": build_spec(stats),
        "stage-error-comparison.svg": build_svg(stages, stats),
    }
    generated["manifest.json"] = build_manifest(verified, stats, generated)
    return generated


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def check_outputs(outputs: dict[str, bytes]) -> None:
    failures: list[str] = []
    expected_names = set(outputs)
    existing_names = {
        path.name for path in OUTPUT_DIR.iterdir() if path.is_file()
    } if OUTPUT_DIR.exists() else set()
    for filename, expected in outputs.items():
        path = OUTPUT_DIR / filename
        if not path.is_file():
            failures.append(f"missing {path}")
            continue
        actual = path.read_bytes()
        if actual != expected:
            failures.append(
                f"content mismatch {path}: expected {sha256_bytes(expected)}, "
                f"got {sha256_bytes(actual)}"
            )
    unexpected = sorted(existing_names - expected_names)
    if unexpected:
        failures.append(f"unexpected files in artifact directory: {unexpected}")
    if failures:
        raise ValueError("artifact verification failed:\n- " + "\n- ".join(failures))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path(os.environ.get("IMF_SOURCE_ROOT", DEFAULT_SOURCE_ROOT)),
        help=(
            "Readable root containing the allowlisted relative files. Pass a "
            "verified snapshot's files/ directory when the sibling repository "
            "is unavailable."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify committed artifact bytes without writing",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    outputs = build_outputs(args.source_root)
    if args.check:
        check_outputs(outputs)
        mode = "verified"
    else:
        for filename in sorted(outputs):
            write_atomic(OUTPUT_DIR / filename, outputs[filename])
        mode = "wrote"
    for filename in sorted(outputs):
        content = outputs[filename]
        print(
            f"{mode} {OUTPUT_DIR / filename} "
            f"sha256={sha256_bytes(content)} bytes={len(content)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
