#!/usr/bin/env python3
"""Analyze two releases and optionally render one reviewed pulse proposal."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from research_pipeline.errors import PipelineError, ValidationError
from research_pipeline.novelty import (
    NoveltyPolicy,
    analyze_release_changes,
    write_analysis_json,
)
from research_pipeline.pulse_builder import build_pulse, load_proposal


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current-release", type=Path, required=True)
    parser.add_argument("--candidate-release", type=Path, required=True)
    parser.add_argument("--prior-proposal-fingerprint", action="append", default=[])
    parser.add_argument("--materiality-threshold-basis-points", type=int, default=6500)
    parser.add_argument("--max-signals", type=int, default=3)
    parser.add_argument("--base-score-basis-points", type=int, default=3000)
    parser.add_argument("--evidence-weight-basis-points", type=int, default=2000)
    parser.add_argument("--confidence-weight-basis-points", type=int, default=2000)
    parser.add_argument("--default-confidence-basis-points", type=int, default=7500)
    parser.add_argument("--contradiction-bonus-basis-points", type=int, default=1000)
    parser.add_argument("--different-target-bonus-basis-points", type=int, default=750)
    parser.add_argument(
        "--allow-unevidenced",
        action="store_true",
        help="Rank unevidenced additions; disabled by default and unsafe for publication.",
    )
    parser.add_argument("--analysis-output", type=Path)
    parser.add_argument("--proposal", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--proposal-schema",
        type=Path,
        default=PROJECT_ROOT / "schemas" / "pulse-proposal.schema.json",
    )
    return parser


def _verify_proposal_matches_analysis(
    proposal: dict[str, object], analysis: dict[str, object]
) -> None:
    errors: list[str] = []
    if analysis["status"] != "selected":
        errors.append("analysis did not select a material development")
    if proposal.get("status") != "selected":
        errors.append("proposal status must be selected")
    if proposal.get("analysis_id") != analysis["id"]:
        errors.append("proposal analysis_id does not match this analysis")
    if proposal.get("analysis_fingerprint") != analysis["analysis_fingerprint"]:
        errors.append("proposal analysis_fingerprint does not match this analysis")
    if proposal.get("candidate_release_id") != analysis["candidate_release_id"]:
        errors.append("proposal candidate_release_id does not match this analysis")
    if proposal.get("proposal_fingerprints") != analysis[
        "selected_candidate_fingerprints"
    ]:
        errors.append("proposal signals do not exactly match selected candidates")
    if errors:
        raise ValidationError("proposal/analysis mismatch:\n- " + "\n- ".join(errors))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if (args.proposal is None) != (args.output is None):
        print("build failed: --proposal and --output must be supplied together", file=sys.stderr)
        return 2
    policy = NoveltyPolicy(
        materiality_threshold_basis_points=args.materiality_threshold_basis_points,
        max_signals=args.max_signals,
        require_evidence=not args.allow_unevidenced,
        base_score_basis_points=args.base_score_basis_points,
        evidence_weight_basis_points=args.evidence_weight_basis_points,
        confidence_weight_basis_points=args.confidence_weight_basis_points,
        default_confidence_basis_points=args.default_confidence_basis_points,
        contradiction_bonus_basis_points=args.contradiction_bonus_basis_points,
        different_target_bonus_basis_points=args.different_target_bonus_basis_points,
    )
    try:
        analysis = analyze_release_changes(
            args.current_release,
            args.candidate_release,
            policy=policy,
            prior_proposal_fingerprints=args.prior_proposal_fingerprint,
        )
        if args.analysis_output is not None:
            write_analysis_json(args.analysis_output, analysis)
        pulse_result = None
        if args.proposal is not None:
            proposal = load_proposal(args.proposal)
            _verify_proposal_matches_analysis(proposal, analysis)
            pulse_result = build_pulse(
                proposal, args.output, schema_path=args.proposal_schema
            )
    except (PipelineError, OSError, ValueError) as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 2

    output: dict[str, object] = {
        "status": analysis["status"],
        "analysis": analysis,
        "analysis_output": str(args.analysis_output) if args.analysis_output else None,
        "pulse": None,
    }
    if pulse_result is not None:
        output["pulse"] = {
            "path": str(pulse_result.path),
            "pulse_id": pulse_result.pulse_id,
            "proposal_fingerprint": pulse_result.proposal_fingerprint,
            "sha256": pulse_result.sha256,
            "word_count": pulse_result.word_count,
        }
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
