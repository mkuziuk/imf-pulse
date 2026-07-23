# ADR 0001: Curated local snapshots and immutable file releases

- Status: accepted for MVP
- Date: 2026-07-22
- Scope: Phases 1 and 2

## Context

The source repository is a live, dirty research worktree containing notebooks, internal drafts, derived figures, and generated TeX files. It is roughly 733 MB because of `.git` and `.venv`, while the useful evidence is a small allowlisted subset. Some notebooks contain code that executes other notebooks or writes figures. The repository has no license or image-reuse declaration. The scheduled environment may later be unable to read the sibling project.

The product must distinguish source statements from evidence, interpretation, and speculation. It must also avoid mixed knowledge/checkpoint state after a failed run.

## Decision

Use a static React/Vite site backed by versioned Markdown and JSONL releases. Synchronization copies only explicitly configured regular files into an immutable private snapshot, then extracts text and stored outputs without executing source code. A release contains validated sources, claims, methods, experiments, relationships, artifacts, and run state. Production site bytes are built into an immutable content-addressed directory. `data/current.json` is the single authoritative pointer to both the research release and exact site build, and is replaced atomically only after validation, tests, and build succeed.

Key choices:

1. React, TypeScript, Vite, React Router, React Markdown, GFM, KaTeX, Mermaid in strict mode, Zod, and Observable Plot.
2. Markdown pulses with validated YAML front matter; arbitrary HTML is disabled.
3. JSONL knowledge objects with stable IDs, schema versions, source-version hashes, precise locators, explicit evidence status, and confidence rationale.
4. Python standard-library-first ingestion with PyYAML, pypdf, jsonschema, and pytest. Notebook ingestion uses JSON parsing only and discards active HTML/JavaScript/widget output.
5. Scientific artifacts are regenerated from copied structured data. The rendered SVG, normalized CSV, and chart specification travel together.
6. Existing IMF figures and PDF screenshots stay private because reuse rights are unknown. No web image is republished until rights are approved.
7. Live and exported-snapshot modes are explicit. Failure to read the live source is a hard failure; the pipeline never silently broadens access or falls back to stale data.
8. External monitoring and scheduling are disabled in this release.

## Consequences

- The MVP is inspectable, diffable, and testable without a database, crawler, or vector index.
- A dirty source tree is represented honestly by selected-file byte hashes, while the inspected Git context remains in the source audit.
- Accepted conclusions are append-only at the release level; corrections become new objects and relationships.
- Integrity is tamper-evident relative to the selected local checkpoint, not an anti-rollback guarantee against an administrator replaying or deleting the checkpoint and all newer local state. That stronger property needs an external append-only anchor or separately approved version-control workflow.
- Direct deep-link hosting requires SPA fallback configuration. The default local/Vite workflow supplies it.
- Snapshot storage duplicates a small curated subset, trading disk space for reproducibility and scheduled-sandbox compatibility.
- Site builds are also immutable and content addressed; failed candidates never replace the site build selected by the authoritative checkpoint.
- Search and automated semantic extraction remain deliberately narrow until manual local runs are reliable.

## Evidence informing the decision

- `research/first-imf-recursive-error/diagnostics/` contains the strongest structured local evidence: six CSV files and one JSON summary.
- `gd_imf_observation_model_comparison.ipynb` and the recursive-error diagnostic notebook can execute other notebooks; ingestion must never run them.
- `IMF.pdf` is an unfinished 21-page internal draft. Lemma 2.5 has no proof, and Proposition 4.1 is visibly incomplete.
- No source repository license, NOTICE, or reuse declaration was found.
