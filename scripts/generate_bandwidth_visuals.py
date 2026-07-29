#!/usr/bin/env python3
"""Generate deterministic supporting plots for the 2026-07-29 pulse.

The plots normalize algebraic consequences of equations (20) and (21) in
Richter and Dahlhaus (2017). They do not reproduce the paper's figures or
claim to show observed or simulated results.
"""

from __future__ import annotations

import csv
import hashlib
import html
import json
import math
from pathlib import Path
from typing import Callable, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_ROOT = PROJECT_ROOT / "public" / "artifacts" / "2026-07-29"
PUBLIC_URL_ROOT = "/artifacts/2026-07-29"
PULSE_ID = "pulse-2026-07-29-1"
SOURCE_ID = "src-external-arxiv-1705-10046v1"
SOURCE_SHA256 = "30b0f6efeff168d41b0e5f4bb4b42116f2e26a0fbb0c83fd189a1e6498f7482a"
SOURCE_PATH = "external/arxiv/1705.10046v1.pdf"

WIDTH = 1200
HEIGHT = 720
LEFT = 112
RIGHT = 54
TOP = 126
BOTTOM = 92
PLOT_WIDTH = WIDTH - LEFT - RIGHT
PLOT_HEIGHT = HEIGHT - TOP - BOTTOM

BACKGROUND = "#f3efe4"
INK = "#161a19"
MUTED = "#626862"
GRID = "#d6d0c2"
TEAL = "#007c76"
ORANGE = "#c45d35"
BLUE = "#315f8c"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def _write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _file_record(url: str, role: str, media_type: str, payload: bytes) -> dict[str, object]:
    return {
        "bytes": len(payload),
        "media_type": media_type,
        "role": role,
        "sha256": _sha256(payload),
        "url": url,
    }


def _csv_bytes(fieldnames: list[str], rows: Iterable[dict[str, str]]) -> bytes:
    from io import StringIO

    stream = StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode()


def _line_path(points: Iterable[tuple[float, float]]) -> str:
    return " ".join(
        f"{'M' if index == 0 else 'L'} {x:.2f} {y:.2f}"
        for index, (x, y) in enumerate(points)
    )


def _text(
    x: float,
    y: float,
    value: str,
    *,
    size: int = 18,
    fill: str = INK,
    weight: int = 400,
    anchor: str = "start",
    rotate: int | None = None,
) -> str:
    transform = f' transform="rotate({rotate} {x:.2f} {y:.2f})"' if rotate is not None else ""
    return (
        f'<text x="{x:.2f}" y="{y:.2f}" fill="{fill}" font-size="{size}" '
        f'font-weight="{weight}" text-anchor="{anchor}"{transform}>'
        f"{html.escape(value)}</text>"
    )


def _svg_shell(title: str, description: str, body: str) -> bytes:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-labelledby="title description">
  <title id="title">{html.escape(title)}</title>
  <desc id="description">{html.escape(description)}</desc>
  <rect width="{WIDTH}" height="{HEIGHT}" fill="{BACKGROUND}"/>
  <style>
    text {{ font-family: "IBM Plex Sans", "Helvetica Neue", Arial, sans-serif; }}
    .series {{ fill: none; stroke-linecap: round; stroke-linejoin: round; }}
  </style>
  {body}
</svg>
""".encode()


def _linear_scale(domain_min: float, domain_max: float, range_min: float, range_max: float) -> Callable[[float], float]:
    def scale(value: float) -> float:
        return range_min + (value - domain_min) * (range_max - range_min) / (domain_max - domain_min)

    return scale


def _log_scale(domain_min: float, domain_max: float, range_min: float, range_max: float) -> Callable[[float], float]:
    lo = math.log(domain_min)
    hi = math.log(domain_max)

    def scale(value: float) -> float:
        return range_min + (math.log(value) - lo) * (range_max - range_min) / (hi - lo)

    return scale


def _risk_plot() -> tuple[bytes, bytes, bytes]:
    title = "The canonical bandwidth balances variance against squared bias"
    description = (
        "A normalized line plot of the asymptotic variance term, squared-bias term, "
        "and their total against bandwidth divided by its optimum. The total reaches "
        "its minimum at a normalized bandwidth of one."
    )
    xs = [0.35 + index * (1.65 / 165) for index in range(166)]
    values = [
        {
            "x": x,
            "variance": 4.0 / (5.0 * x),
            "bias": x**4 / 5.0,
            "total": 4.0 / (5.0 * x) + x**4 / 5.0,
        }
        for x in xs
    ]
    x_scale = _linear_scale(0.35, 2.0, LEFT, WIDTH - RIGHT)
    y_scale = _linear_scale(0.0, 3.7, HEIGHT - BOTTOM, TOP)

    parts = [
        _text(LEFT, 48, title, size=30, weight=650),
        _text(
            LEFT,
            82,
            "Normalized derivation from equations (20)–(21); not observed or simulated data",
            size=17,
            fill=MUTED,
        ),
    ]
    for value in [0, 0.5, 1, 1.5, 2, 2.5, 3, 3.5]:
        y = y_scale(value)
        parts.append(f'<line x1="{LEFT}" x2="{WIDTH - RIGHT}" y1="{y:.2f}" y2="{y:.2f}" stroke="{GRID}" stroke-width="1"/>')
        parts.append(_text(LEFT - 16, y + 6, f"{value:g}", size=15, fill=MUTED, anchor="end"))
    for value in [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2]:
        x = x_scale(value)
        parts.append(f'<line x1="{x:.2f}" x2="{x:.2f}" y1="{TOP}" y2="{HEIGHT - BOTTOM}" stroke="{GRID}" stroke-width="1"/>')
        parts.append(_text(x, HEIGHT - BOTTOM + 28, f"{value:g}", size=15, fill=MUTED, anchor="middle"))

    parts.extend(
        [
            f'<line x1="{LEFT}" x2="{WIDTH - RIGHT}" y1="{HEIGHT - BOTTOM}" y2="{HEIGHT - BOTTOM}" stroke="{INK}" stroke-width="1.5"/>',
            f'<line x1="{LEFT}" x2="{LEFT}" y1="{TOP}" y2="{HEIGHT - BOTTOM}" stroke="{INK}" stroke-width="1.5"/>',
            _text((LEFT + WIDTH - RIGHT) / 2, HEIGHT - 28, "Bandwidth relative to optimum, h / h₀", size=18, anchor="middle"),
            _text(34, (TOP + HEIGHT - BOTTOM) / 2, "Loss relative to minimum", size=18, anchor="middle", rotate=-90),
        ]
    )

    series = [
        ("total", TEAL, 4.0, None),
        ("variance", BLUE, 2.5, "9 7"),
        ("bias", ORANGE, 2.5, "9 7"),
    ]
    for key, color, stroke_width, dash in series:
        path = _line_path((x_scale(row["x"]), y_scale(row[key])) for row in values)
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        parts.append(
            f'<path class="series" d="{path}" stroke="{color}" stroke-width="{stroke_width}"{dash_attr}/>'
        )

    optimum_x = x_scale(1)
    optimum_y = y_scale(1)
    parts.extend(
        [
            f'<line x1="{optimum_x:.2f}" x2="{optimum_x:.2f}" y1="{TOP}" y2="{HEIGHT - BOTTOM}" stroke="{TEAL}" stroke-width="1.5" stroke-dasharray="3 6"/>',
            f'<circle cx="{optimum_x:.2f}" cy="{optimum_y:.2f}" r="7" fill="{TEAL}" stroke="{BACKGROUND}" stroke-width="3"/>',
            _text(optimum_x + 16, optimum_y - 17, "minimum at h = h₀", size=17, fill=TEAL, weight=650),
        ]
    )
    legend_y = 108
    legend_xs = [LEFT, LEFT + 270, LEFT + 540]
    for (label, color, _, dash), x in zip(series, legend_xs):
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        parts.append(f'<line x1="{x}" x2="{x + 42}" y1="{legend_y}" y2="{legend_y}" stroke="{color}" stroke-width="4"{dash_attr}/>')
        label_text = {"total": "total", "variance": "variance", "bias": "squared bias"}[label]
        parts.append(_text(x + 54, legend_y + 6, label_text, size=16, fill=MUTED))

    csv_payload = _csv_bytes(
        ["normalized_bandwidth", "normalized_variance", "normalized_squared_bias", "normalized_total_loss"],
        (
            {
                "normalized_bandwidth": f"{row['x']:.6f}",
                "normalized_variance": f"{row['variance']:.6f}",
                "normalized_squared_bias": f"{row['bias']:.6f}",
                "normalized_total_loss": f"{row['total']:.6f}",
            }
            for row in values
        ),
    )
    spec_payload = _json_bytes(
        {
            "schema_version": 1,
            "chart_type": "line",
            "derivation": {
                "normalized_bandwidth": "x = h / h0",
                "normalized_variance": "4 / (5x)",
                "normalized_squared_bias": "x^4 / 5",
                "normalized_total_loss": "4 / (5x) + x^4 / 5",
            },
            "source": {"equations": [20, 21], "pdf_pages": [9], "source_id": SOURCE_ID},
        }
    )
    return _svg_shell(title, description, "\n  ".join(parts)), csv_payload, spec_payload


def _scaling_plot() -> tuple[bytes, bytes, bytes]:
    title = "A narrower fraction can still contain more observations"
    description = (
        "A normalized log-scale line plot showing that under h proportional to n to "
        "the minus one fifth, the bandwidth fraction declines while the number of "
        "observations in the local window grows as n to the four fifths."
    )
    sample_sizes = sorted(
        {
            round(200 * (10000 / 200) ** (index / 100))
            for index in range(101)
        }
        | {200, 500, 1000, 2000, 5000, 10000}
    )
    values = [
        {
            "n": n,
            "bandwidth": (n / 200) ** (-0.2),
            "count": (n / 200) ** 0.8,
        }
        for n in sample_sizes
    ]
    x_scale = _log_scale(200, 10000, LEFT, WIDTH - RIGHT)
    y_scale = _log_scale(0.4, 30, HEIGHT - BOTTOM, TOP)

    parts = [
        _text(LEFT, 48, title, size=30, weight=650),
        _text(
            LEFT,
            82,
            "Algebraic implication of h₀ ∝ n⁻¹ᐟ⁵, holding problem-specific constants fixed",
            size=17,
            fill=MUTED,
        ),
    ]
    for value in [0.5, 1, 2, 5, 10, 20]:
        y = y_scale(value)
        parts.append(f'<line x1="{LEFT}" x2="{WIDTH - RIGHT}" y1="{y:.2f}" y2="{y:.2f}" stroke="{GRID}" stroke-width="1"/>')
        parts.append(_text(LEFT - 16, y + 6, f"{value:g}×", size=15, fill=MUTED, anchor="end"))
    for value in [200, 500, 1000, 2000, 5000, 10000]:
        x = x_scale(value)
        parts.append(f'<line x1="{x:.2f}" x2="{x:.2f}" y1="{TOP}" y2="{HEIGHT - BOTTOM}" stroke="{GRID}" stroke-width="1"/>')
        parts.append(_text(x, HEIGHT - BOTTOM + 28, f"{value:,}", size=15, fill=MUTED, anchor="middle"))

    parts.extend(
        [
            f'<line x1="{LEFT}" x2="{WIDTH - RIGHT}" y1="{HEIGHT - BOTTOM}" y2="{HEIGHT - BOTTOM}" stroke="{INK}" stroke-width="1.5"/>',
            f'<line x1="{LEFT}" x2="{LEFT}" y1="{TOP}" y2="{HEIGHT - BOTTOM}" stroke="{INK}" stroke-width="1.5"/>',
            _text((LEFT + WIDTH - RIGHT) / 2, HEIGHT - 28, "Sample size n (log scale)", size=18, anchor="middle"),
            _text(30, (TOP + HEIGHT - BOTTOM) / 2, "Relative to n = 200 (log scale)", size=18, anchor="middle", rotate=-90),
        ]
    )

    path_bandwidth = _line_path((x_scale(row["n"]), y_scale(row["bandwidth"])) for row in values)
    path_count = _line_path((x_scale(row["n"]), y_scale(row["count"])) for row in values)
    parts.extend(
        [
            f'<path class="series" d="{path_bandwidth}" stroke="{ORANGE}" stroke-width="4"/>',
            f'<path class="series" d="{path_count}" stroke="{TEAL}" stroke-width="4"/>',
        ]
    )

    for n in [200, 500, 1000, 10000]:
        bandwidth = (n / 200) ** (-0.2)
        count = (n / 200) ** 0.8
        for value, color in [(bandwidth, ORANGE), (count, TEAL)]:
            parts.append(
                f'<circle cx="{x_scale(n):.2f}" cy="{y_scale(value):.2f}" r="5.5" fill="{color}" stroke="{BACKGROUND}" stroke-width="2.5"/>'
            )

    legend_y = 108
    for x, label, color in [
        (LEFT, "bandwidth fraction h₀ ∝ n⁻¹ᐟ⁵", ORANGE),
        (LEFT + 440, "observations in window nh₀ ∝ n⁴ᐟ⁵", TEAL),
    ]:
        parts.append(f'<line x1="{x}" x2="{x + 42}" y1="{legend_y}" y2="{legend_y}" stroke="{color}" stroke-width="4"/>')
        parts.append(_text(x + 54, legend_y + 6, label, size=16, fill=MUTED))

    csv_payload = _csv_bytes(
        ["sample_size", "relative_bandwidth_fraction", "relative_observations_in_window"],
        (
            {
                "sample_size": str(row["n"]),
                "relative_bandwidth_fraction": f"{row['bandwidth']:.6f}",
                "relative_observations_in_window": f"{row['count']:.6f}",
            }
            for row in values
        ),
    )
    spec_payload = _json_bytes(
        {
            "schema_version": 1,
            "chart_type": "line",
            "normalization": "Both series equal one at n = 200.",
            "derivation": {
                "relative_bandwidth_fraction": "(n / 200)^(-1/5)",
                "relative_observations_in_window": "(n / 200)^(4/5)",
            },
            "source": {"equation": 21, "pdf_pages": [9], "source_id": SOURCE_ID},
        }
    )
    return _svg_shell(title, description, "\n  ".join(parts)), csv_payload, spec_payload


def _manifest(
    *,
    slug: str,
    artifact_id: str,
    title: str,
    caption: str,
    relation: str,
    svg: bytes,
    csv_payload: bytes,
    spec: bytes,
    limitations: list[str],
    evidence_statement: str,
) -> bytes:
    directory_url = f"{PUBLIC_URL_ROOT}/{slug}"
    script_payload = Path(__file__).read_bytes()
    files = [
        _file_record(f"{directory_url}/{slug}.csv", "normalized derived values", "text/csv", csv_payload),
        _file_record(f"{directory_url}/{slug}.spec.json", "declarative chart derivation", "application/json", spec),
        _file_record(f"{directory_url}/{slug}.svg", "responsive accessible rendered plot", "image/svg+xml", svg),
    ]
    return _json_bytes(
        {
            "artifact_date": "2026-07-29",
            "artifact_id": artifact_id,
            "artifact_type": "scientific_chart",
            "caption": caption,
            "evidence": [
                {
                    "confidence": "high as an algebraic consequence under the theorem's assumptions",
                    "source_id": SOURCE_ID,
                    "source_locator": "PDF pages 8–9, equations (17), (20), and (21)",
                    "statement": evidence_statement,
                    "status": "derived_from_stated_asymptotic_formula",
                }
            ],
            "files": files,
            "generator": {
                "clock_dependent_fields": [],
                "deterministic": True,
                "language": "Python standard library",
                "path": "scripts/generate_bandwidth_visuals.py",
                "sha256": _sha256(script_payload),
                "source_files_executed": False,
                "version": "1.0.0",
            },
            "limitations": limitations,
            "manifest_url": f"{directory_url}/manifest.json",
            "parameters": {
                "normalization_reference_sample_size": 200,
                "source_equations": [17, 20, 21],
            },
            "related_pulse": PULSE_ID,
            "relation_to_report": relation,
            "rights": {
                "local_display_allowed": True,
                "may_publish_publicly": False,
                "public_deployment_requires_owner_approval": True,
                "status": "project_generated_scientific_chart",
            },
            "schema_version": 1,
            "sources": [
                {
                    "content_sha256": SOURCE_SHA256,
                    "execution_status": "not executed; equations transcribed and normalized",
                    "locators": [
                        "PDF page 8: equation (17)",
                        "PDF page 9: equations (20) and (21), Theorems 3.8 and 3.9",
                    ],
                    "path": SOURCE_PATH,
                    "rights_status": "unknown; no source media reproduced",
                    "role": "asymptotic bias-variance formula and bandwidth rate",
                    "source_id": SOURCE_ID,
                }
            ],
            "stable_url": f"{directory_url}/{slug}.svg",
            "title": title,
        }
    )


def _generate_one(
    *,
    slug: str,
    artifact_id: str,
    title: str,
    caption: str,
    relation: str,
    builder: Callable[[], tuple[bytes, bytes, bytes]],
    limitations: list[str],
    evidence_statement: str,
) -> None:
    svg, csv_payload, spec = builder()
    directory = PUBLIC_ROOT / slug
    _write(directory / f"{slug}.svg", svg)
    _write(directory / f"{slug}.csv", csv_payload)
    _write(directory / f"{slug}.spec.json", spec)
    manifest = _manifest(
        slug=slug,
        artifact_id=artifact_id,
        title=title,
        caption=caption,
        relation=relation,
        svg=svg,
        csv_payload=csv_payload,
        spec=spec,
        limitations=limitations,
        evidence_statement=evidence_statement,
    )
    _write(directory / "manifest.json", manifest)
    print(f"{directory.relative_to(PROJECT_ROOT)}/manifest.json {_sha256(manifest)}")


def main() -> None:
    _generate_one(
        slug="bandwidth-bias-variance",
        artifact_id="derived-bandwidth-bias-variance-2026-07-29",
        title="The canonical bandwidth balances variance against squared bias",
        caption=(
            "After normalizing bandwidth by the asymptotic optimum h₀ and loss by its "
            "minimum, the paper's variance term becomes 4/(5x), squared bias becomes "
            "x⁴/5, and their sum is minimized at x = h/h₀ = 1. Derived plot—not "
            "observed or simulated data."
        ),
        relation=(
            "This plot makes Signal 01's n⁻¹ᐟ⁵ claim inspectable: it shows the exact "
            "normalized shape implied by the paper's asymptotic bias-variance formula "
            "without choosing or inventing model-specific constants."
        ),
        builder=_risk_plot,
        limitations=[
            "The curves are a normalization of the asymptotic approximation in equations (20)–(21), not finite-sample measurements.",
            "The plot assumes the paper's stronger smoothness conditions and a nondegenerate bias term B₀ > 0.",
            "Normalization removes the kernel- and model-specific constants, so the plot does not provide a numerical bandwidth for any dataset.",
            "The cross-validation objective itself is not plotted; Theorem 3.9 supplies the asymptotic link between its selected bandwidth and h₀.",
        ],
        evidence_statement=(
            "Equation (20) has a variance term proportional to 1/(nh) and a squared-bias "
            "term proportional to h⁴; equation (21) gives their unique minimizer h₀ ∝ n⁻¹ᐟ⁵."
        ),
    )
    _generate_one(
        slug="bandwidth-scaling",
        artifact_id="derived-bandwidth-scaling-2026-07-29",
        title="A narrower fraction can still contain more observations",
        caption=(
            "If the problem-specific constant in h₀ is held fixed, h₀ ∝ n⁻¹ᐟ⁵ means "
            "the window occupies a slowly shrinking fraction of the series while its "
            "observation count nh₀ grows as n⁴ᐟ⁵. Both curves are normalized to one "
            "at n = 200. Derived plot—not observed or simulated data."
        ),
        relation=(
            "This plot explains the practical meaning of Signal 01's bandwidth rate: "
            "locality strengthens with sample size even though the estimator can use "
            "more observations inside each local fit."
        ),
        builder=_scaling_plot,
        limitations=[
            "The curves are algebraic implications of h₀ ∝ n⁻¹ᐟ⁵, not bandwidths selected in the paper's simulations.",
            "The comparison holds the model-, kernel-, and parameter-curve-dependent multiplicative constant fixed.",
            "The n = 200 reference is a normalization choice only; it does not assert that h₀ or nh₀ equals one at n = 200.",
            "Finite-sample admissible bandwidth grids and boundary handling can alter realized choices.",
        ],
        evidence_statement=(
            "Equation (21) implies h₀(n)/h₀(200) = (n/200)⁻¹ᐟ⁵ and "
            "nh₀(n)/(200h₀(200)) = (n/200)⁴ᐟ⁵ when other constants are fixed."
        ),
    )


if __name__ == "__main__":
    main()
