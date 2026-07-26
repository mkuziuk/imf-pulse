# Implemented Phase 1–5 architecture

The Residual remains a file-based local MVP. It has no application server, database, vector index, or broad crawler. Its narrow automatic evidence path handles at most one exact official arXiv PDF per pulse; its only unattended publisher is the fail-closed, published-only wrapper described below.

## Phase 1 — editorial site and artifact rendering

- `src/main.tsx`, `src/App.tsx`, and `src/pages/` implement Latest, Archive, Research map, Artifacts, and the error route. `/` opens the latest accepted pulse; citations link directly to public source URLs.
- `src/styles/` provides the warm journal palette, spectral accent, editorial grid, responsive layout, reduced-motion handling, focus states, and print treatment.
- `src/components/MarkdownRenderer.tsx` renders GFM, KaTeX, safe Mermaid diagrams, citations, and figures without raw HTML.
- `src/components/ScientificChart.tsx` renders responsive scientific charts with accessible tables and deterministic SVG fallback.
- `content-bundle.ts` and `release-env.ts` authorize only validated accepted content at build time. `VITE_ROUTER_MODE=hash` and `VITE_BASE_PATH` support a GitHub Pages project site.
- `content/pulses/2026-07-22.md` and its chart are the realistic first report and artifact.

## Phase 2 — curated local intelligence

- `config/sources.yaml` registers explicit relative paths beneath `$IMF_SOURCE_ROOT` (default `../imf`) with source class, authority, publication state, topics, rights, limitations, and extractor.
- `research_pipeline/paths.py`, `hashing.py`, and `snapshot.py` enforce no-follow reads, stable byte hashing, immutable private snapshots, and source/output containment.
- `research_pipeline/extractors.py` statically extracts bounded PDF page text, Markdown/TeX/Python lines, notebook cells and safe stored text outputs, CSV rows, and JSON pointers. It never executes source material.
- `research_pipeline/release.py` builds immutable source/knowledge releases, validates cross-references and provenance, binds accepted publication bytes, runs gates, writes immutable run records, and replaces `data/current.json` last.
- `knowledge/curated/` contains reviewed claim, method, experiment, and relationship seeds. `evaluation/benchmark.yaml` contains the ten evidence-cited benchmark questions.
- `scripts/sync_local_sources.py`, `export_local_snapshot.py`, `ingest_sources.py`, `validate_pulse.py`, and `publish_pulse.py` expose the lower-level manual workflow.

## Phase 3 — deterministic novelty and daily proposals

- `research_pipeline/novelty.py` compares two immutable releases, classifies source and knowledge changes, ranks evidence-backed additions with deterministic integer scoring, flags contradictions and target drift, deduplicates prior proposal fingerprints, and selects no more than three signals.
- Change outcomes are `no_update`, `review_required`, or `selected`. Deletions, modified accepted objects, unevidenced additions, and bootstrap ambiguity are never silently promoted. Source-only local changes can advance the evidence release without forcing a report.
- `schemas/change-analysis.schema.json` and `schemas/pulse-proposal.schema.json` define the review objects.
- `research_pipeline/pulse_builder.py` validates a reviewed proposal bound to the exact analysis/release and renders one immutable 350–650 word Markdown pulse with exactly one artifact. It refuses overwrite.
- `scripts/build_daily_pulse.py` writes deterministic analysis and can render an already reviewed proposal. It does not generate, approve, or publish prose by itself.
- Pulse identity is the pair `(Europe/Moscow date, positive index)`. Legacy date-only pulses remain index `1`; new immutable reports use `YYYY-MM-DD-N.md`, allowing multiple separately reviewed releases on one date without overwriting accepted bytes.
- `prompts/` contains active but review-only extraction, change-analysis, and pulse-drafting contracts. Prompt output cannot mutate accepted knowledge or checkpoints.

## Phase 4 — bounded external metadata monitoring

- `config/external-sources.yaml` fixes reviewed arXiv and Crossref HTTPS endpoints, allowed hosts, query vocabulary, publication types, century-scale historical discovery windows, result limits, response size, timeout, and receipt/review paths.
- `research_pipeline/external.py` parses arXiv XML with DTD/entity rejection and strict Crossref JSON, normalizes provider identities, writes immutable private receipts and hash-bound candidate batches, and never exposes a full-text download or code-execution path.
- `scripts/search_external_sources.py --as-of ...` performs deterministic discovery. Scheduled use adds `--scheduled-outcome-date DATE`, producing one ignored hash-bound batch/deferred handoff so the wrapper never repeats the provider search. Provider timeouts are deferred; validation and integrity failures remain hard failures. New candidates are deferred metadata, never accepted evidence by themselves.
- `scripts/build_editorial_context.py` verifies and summarizes sealed accepted publications so the editor can judge semantic novelty before fetching full text.
- `scripts/review_external_source.py` appends one explicit `approved` or `rejected` decision bound to the batch, candidate ID, candidate SHA-256, reviewer, timestamp, rationale, and rights record. Existing decisions cannot be replaced.
- `scripts/compare_knowledge.py` compares manually prepared controlled profiles. It reports different definitions, different targets, exact-scope contradictions, and missing/scope review gaps; every finding still requires review.
- `schemas/external-{candidate,batch,decision}.schema.json` and `schemas/comparison-finding.schema.json` define these records.

Published papers, scholarly books, and chapters precede preprints and local research in discovery. Manual approval does not download a work, extract claims, edit curated knowledge, or publish a pulse. The separate automatic path in `research_pipeline/automatic.py` accepts only one exact official arXiv PDF, validates page-level evidence and append-only records, and rolls every materialized file back on failure.

## Phase 5 — one local daily transaction and scheduling contract

- `research_pipeline/daily.py` and `scripts/run_daily_pipeline.py` provide the single `--mode live --date YYYY-MM-DD` transaction.
- Preflight requires the source, external, report, extraction, automatic-editorial, and scheduling policies. The command acquires a non-blocking local lock, reads the checkpoint, monitors literature first, defers unresolved metadata, syncs allowlisted local context, validates any exact automatic package, builds a candidate release, performs novelty analysis, and invokes the existing atomic publisher.
- The stable result contract is `published`, `no_update`, `review_required`, `blocked`, or `failed`, with run/release identity, checkpoint effects, evidence IDs, and pending-review path.
- Pending external candidates never stop the run. A verified package must match the exact deterministic selection; otherwise it fails closed. A local evidence change without a selected package advances without a pulse. No-update runs create no report.
- The Codex desktop scheduled task runs independently at 06:00 `Europe/Moscow` in local mode and invokes `scripts/run_scheduled_pipeline.py` once.
- The wrapper validates the daily JSON result. Only `published` can pass clean-branch, synchronized-origin, exact-path, public-export, and public-audit gates before one non-force commit/push and a wait for the matching Pages workflow.
- Every other result performs no Git operation. The wrapper cannot tag, open a pull request, force-push, change hosting settings, or modify the sibling source repository.

The scheduling declaration in `config/pulse.yaml` is a reviewed policy prerequisite, not a system scheduler installer.

## Public release and GitHub Pages

- Private `imports/`, `data/releases/`, `data/runs/`, extracts, source documents, and build caches are excluded from Git and deployment.
- `scripts/export_public_release.py` atomically constructs `public-release/` from the current validated release: sanitized current metadata, five knowledge JSONL files, accepted pulses, and cleared artifact files only.
- `scripts/audit_public_release.py` verifies the manifest allowlist and every hash, rights decision, path, file type, and public-data rule. The Vite content plugin repeats the boundary at build time.
- `.github/workflows/pages.yml` re-audits the public export, runs Vitest, builds with `/imf-pulse/` and hash routing, rejects source maps, and deploys only `dist/` through GitHub Pages. A fail-closed change classifier skips duplicate Pytest only for exact content-only pulse commits; all other changes and manual dispatches run it.
- A guarded scheduled push, reviewed operator push, or explicit workflow dispatch can start publication. The research pipeline itself cannot trigger it.

## Data layout

- `imports/imf/snapshots/` — private immutable allowlisted source bytes.
- `data/releases/` — private immutable validated research and publication releases.
- `data/current.json` — the sole accepted-release checkpoint.
- `data/runs/` — immutable local publication outcomes.
- `data/review/` — change analyses, manual pulse proposals, and append-only external decisions.
- `data/automatic/` — ignored exact-hash editorial packages and private page extracts.
- `tmp/external-receipts/` — private immutable provider responses.
- `data/external/batches/` — normalized metadata candidate queues.
- `public-release/` — the only research-content input authorized for public builds.

## Verification

- Python: path/source immutability, static extraction, schema and evidence references, release identity, checkpoint rollback, novelty, proposal rendering, external metadata/review/comparison, daily statuses, and public export boundary.
- Frontend: route/default content, Markdown/math, safe Mermaid, chart fallback, provenance, responsive navigation, and sealed build selection.
- Required commands: `.venv/bin/python -m pytest`, `npm test`, `npm run build`, and `.venv/bin/python scripts/audit_public_release.py --directory public-release` before public publication.
- Visual checks: desktop and mobile routes, overflow, links/assets, focus, contrast, reduced motion, and console errors.
