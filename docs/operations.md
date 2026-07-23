# Operations

All commands below run from `$PROJECT_ROOT`. The source repository is a separate, read-only input.

## Bootstrap

```bash
export PROJECT_ROOT="$(pwd)"
export IMF_SOURCE_ROOT="${PROJECT_ROOT}/../imf"

npm ci
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

`IMF_SOURCE_ROOT` may point to another authorized source checkout, but source entries in `config/sources.yaml` remain explicit relative paths. Never point it at `$PROJECT_ROOT` or one of its ancestors/children.

## Verification

```bash
.venv/bin/python -m pytest
npm test
npm run build
.venv/bin/python scripts/audit_public_release.py --directory public-release
```

The public audit is required for deployment, not for an early private checkout that has no `public-release/` yet.

## Manual local ingestion

Create an immutable snapshot without selecting it as the accepted release:

```bash
.venv/bin/python scripts/sync_local_sources.py
```

Copy the printed `snapshot_directory` into the explicit ingest command:

```bash
.venv/bin/python scripts/ingest_sources.py \
  --snapshot-directory imports/imf/snapshots/SNAPSHOT_ID
```

The ingest result prints a candidate `release_id`. To publish a prepared, reviewed pulse and artifact:

```bash
.venv/bin/python scripts/publish_pulse.py \
  --release-id RELEASE_ID \
  --pulse content/pulses/YYYY-MM-DD.md \
  --artifact-manifest public/artifacts/YYYY-MM-DD/ARTIFACT_ID/manifest.json
```

To record a verified no-update check for the already current release, omit `--pulse` and `--artifact-manifest`. Publication still runs every gate. `data/current.json` is replaced only after success; a failed gate preserves the previous accepted checkpoint.

### Explicit snapshot fallback

If an authorized local context cannot later expose the live sibling repository, export the allowlisted bytes first:

```bash
.venv/bin/python scripts/export_local_snapshot.py \
  --source-root "$IMF_SOURCE_ROOT"
```

This advances only the private input-snapshot pointer. It never advances the accepted research release. A later manual ingest must select the snapshot explicitly and report its age. The daily `live` command does not silently fall back to it.

## Phase 3 change analysis and proposal rendering

Create a deterministic comparison between two immutable releases:

```bash
.venv/bin/python scripts/build_daily_pulse.py \
  --current-release data/releases/release-CURRENT \
  --candidate-release data/releases/release-CANDIDATE \
  --analysis-output data/review/change-analyses/ANALYSIS.json
```

The result is one of `no_update`, `review_required`, or `selected`. It ranks only evidence-backed appended objects, uses stable tie-breaking and integer basis points, removes already proposed fingerprints, and selects at most three signals. A source-only change, deletion, modification of accepted knowledge, or missing evidence is not auto-promoted.

The command never writes narrative by itself. After an operator creates and reviews a structured proposal whose analysis ID, analysis fingerprint, candidate release, and selected fingerprints match exactly, render it with:

```bash
.venv/bin/python scripts/build_daily_pulse.py \
  --current-release data/releases/release-CURRENT \
  --candidate-release data/releases/release-CANDIDATE \
  --analysis-output data/review/change-analyses/ANALYSIS.json \
  --proposal data/review/pulse-proposals/YYYY-MM-DD.json \
  --output content/pulses/YYYY-MM-DD.md
```

The proposal must satisfy `schemas/pulse-proposal.schema.json`, cite its evidence, contain at most three signals, select exactly one approved artifact, and render to 350–650 words. An existing pulse is immutable and will not be overwritten. Rendering does not advance a release.

## Phase 4 external metadata review

Run the configured arXiv metadata queries at a deterministic cutoff:

```bash
.venv/bin/python scripts/search_external_sources.py \
  --project-root "$PROJECT_ROOT" \
  --as-of 2026-07-23T08:00:00+03:00
```

The command writes private immutable Atom receipts beneath `tmp/external-receipts/` and a normalized batch beneath `data/external/batches/`. It does not copy abstracts into the batch or download PDFs, supplementary files, or code. A result with `candidates_pending_review` cannot become evidence automatically. An unresolved exact candidate is carried into later batches; once its exact hash has an approval or rejection, that version is deduplicated.

Review one exact candidate version by copying its batch path, ID, and SHA-256 from the batch:

```bash
.venv/bin/python scripts/review_external_source.py \
  --project-root "$PROJECT_ROOT" \
  --batch "$PROJECT_ROOT/data/external/batches/BATCH.json" \
  --candidate-id CANDIDATE_ID \
  --candidate-sha256 CANDIDATE_SHA256 \
  --decision approved \
  --reviewer REVIEWER_ID \
  --reason "Why this exact metadata candidate should enter further review" \
  --decided-at 2026-07-23T09:00:00+03:00 \
  --license unknown \
  --reuse-status unknown
```

Use `--decision rejected` when appropriate. Add `--public-distribution` only when the recorded reuse status is `cleared` or `public_domain`. Decisions append to `data/review/external-decisions.jsonl`, bind the exact candidate hash, and cannot be edited or duplicated. Approval permits further review only; it does not extract the paper, accept claims, or publish a pulse.

Compare manually prepared controlled knowledge profiles with:

```bash
.venv/bin/python scripts/compare_knowledge.py \
  --existing PATH/TO/EXISTING.jsonl \
  --candidates PATH/TO/CANDIDATES.jsonl
```

Profiles use explicit `concept_key`, `target_key`, `scope_keys`, `value_key`, and `definition_bindings`. The helper reports definition drift, different targets, exact-scope contradictions, and review gaps. It performs no free-text semantic inference and every finding has `review_required: true`.

## Daily transaction

Determine the date in Moscow and run exactly one transaction:

```bash
DATE="$(TZ=Europe/Moscow date +%F)"
.venv/bin/python scripts/run_daily_pipeline.py \
  --project-root "$PROJECT_ROOT" \
  --mode live \
  --date "$DATE"
```

Do not call synchronization, search, analysis, rendering, or publication piecemeal during the same scheduled run. The transaction owns the lock and checkpoint sequence.

The command emits one JSON result:

- `published`: exactly one reviewed pulse passed every gate; `pulse_path` is present.
- `no_update`: no material development was selected; no pulse or placeholder artifact was created.
- `review_required`: inspect `pending_review_path`; the accepted release is not advanced for that pending decision.
- `blocked`: a required executable, reviewed policy, live source root, or safe permission is missing.
- `failed`: processing or a validation gate failed; do not bypass the gate.

Exit status is zero for `published`, `no_update`, and `review_required`; it is nonzero for `blocked` and `failed`. Use the JSON fields `release_advanced` and `checkpoint_refreshed` rather than inferring state from a source hash or timestamp.

### Scheduled task boundary

The Codex desktop scheduled task is configured outside the repository with these fixed properties:

- schedule: daily at 08:00 `Europe/Moscow`;
- target: `$PROJECT_ROOT`;
- mode: standalone local run;
- command: the exact daily command above, once;
- output: one concise status summary and a local pulse link only when published;
- forbidden actions: source-repository writes, permission widening, code execution from sources, automatic external approval, Git commits/pushes/tags/PRs, GitHub Pages deployment, and hosting-setting changes.

`config/pulse.yaml` records the approved schedule and safety prerequisites but does not install the task. Test the exact scheduled prompt manually in an independent local run before enabling or changing it. Codex desktop availability is required for an app-local scheduled task.

## Public export and GitHub Pages

The private release tree is never a deployment input. Refresh and audit the sealed public view explicitly:

```bash
.venv/bin/python scripts/export_public_release.py --output public-release
.venv/bin/python scripts/audit_public_release.py --directory public-release
```

The exporter atomically replaces a direct project child only. Its manifest allowlists and hashes the sanitized current summary, five knowledge JSONL files, accepted pulses, and cleared artifact files. The audit rejects extra nodes, symlinks, raw/private fields, machine home paths, credential-like values, and unknown-rights media.

Reproduce the Pages build locally:

```bash
IMF_PULSE_PUBLIC_RELEASE_DIR=public-release \
VITE_BASE_PATH=/imf-pulse/ \
VITE_ROUTER_MODE=hash \
npm run build
```

Inspect `git status --short` and stage only reviewed code, documentation, workflow, and `public-release/` files. Never force-add ignored private snapshots, releases, receipts, run logs, or build caches. A deliberate push to `main` starts `.github/workflows/pages.yml`; an operator can also dispatch that workflow explicitly. It audits the public boundary again, runs all tests, builds without source maps, and deploys only `dist/`.

The daily pipeline and scheduled task never run Git or GitHub commands. Updating local research state and publishing the public website are separate decisions.

## Rights checklist

Before any artifact enters `public-release/`, confirm:

- stable local URL, caption, provenance, and relation to the report;
- explicit license/reuse status and public-distribution decision;
- deterministic data and specification companions for scientific charts;
- creator, original URL, retrieval date, and reuse terms for web images;
- the exact visible label `Conceptual illustration — not research evidence` for generated images.

Unknown-rights source figures, PDF pages, and screenshots remain private. The repository itself intentionally has no open-source license unless the owner adds one.
