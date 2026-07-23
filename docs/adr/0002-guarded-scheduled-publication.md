# ADR 0002: Guarded scheduled publication

Date: 2026-07-23
Status: accepted

## Context

The daily research transaction already runs the expensive Python, frontend, and production-build gates before advancing its private checkpoint. Repeating the complete Python suite on GitHub for a pulse-only commit made Pages publication unnecessarily slow. At the same time, allowing a general-purpose scheduled agent to run arbitrary Git commands would weaken the source, review, and public-release boundaries.

## Decision

Keep research processing and Git publication separate in code. `run_daily_pipeline.py` remains Git-free. `run_scheduled_pipeline.py` is a narrow orchestrator that requires a clean synchronized `main`, an exact approved remote, authenticated GitHub CLI access, and the reviewed scheduling policy. It executes the daily command once. Only a schema-valid `published` result may be exported, audited, matched against a current-date file allowlist, committed without force, pushed, and associated with a successful Pages run.

GitHub Actions uses a fail-closed diff classifier. Added or modified pulse content, dated artifacts, curated knowledge, and sealed public-release files take the content path: public audit, frontend tests, and production build. Every other case, including deletions, renames, code/configuration changes, incomplete history, and manual dispatch, also runs the full Python suite.

## Consequences

- `no_update`, `review_required`, blocked, and failed runs cannot publish.
- Private snapshots, releases, extracts, receipts, and run records stay ignored and outside GitHub.
- A routine content publication avoids a duplicate long Python test run while retaining all local release gates and all public-site gates.
- Local divergence, an origin race, or a failed deploy stops later unattended runs until an operator inspects and reconciles state.
