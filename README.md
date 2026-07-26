# The Residual

The Residual is a literature-first, locally reproducible research-intelligence site for intrinsic multiscale filtering (IMF), robust IMF/IRMF, robust local estimation, contamination models, and related theory.

[Open the public site](https://mkuziuk.github.io/imf-pulse/)

The MVP implements all five planned phases without introducing a crawler, application server, database, or vector store:

- an editorial React site whose default route is the latest pulse;
- read-only, hash-addressed ingestion of explicitly allowlisted local research;
- deterministic change analysis, novelty ranking, and fail-closed automatic pulse proposals;
- bounded arXiv and Crossref monitoring for preprints, journal papers, proceedings, books, and chapters, with immutable receipts and exact-hash review decisions;
- one transactional daily command plus a guarded local publisher scheduled for 06:00 `Europe/Moscow`.

The system is deliberately conservative. A new source or changed hash is not automatically news. Primary external literature has editorial priority; local IMF changes provide supporting, reproducing, or contradicting context. Unresolved metadata is deferred rather than blocking a run. The scheduled editor may inspect at most one exact arXiv paper through the guarded PDF helper, but publication proceeds only when the candidate hash, PDF hash, page locators, append-only knowledge records, novelty selection, prose, artifact, and every release gate agree. Crossref-only records and uncertain evidence are skipped.

## Research and publication guarantees

- Every accepted claim, method, experiment, and relationship resolves to a source version and precise location.
- Evidence, interpretation, uncertainty, and speculation are represented separately.
- Competing definitions, targets, and contradictions remain visible.
- `IMF.pdf` is treated as an unfinished internal draft, not established truth.
- Notebooks, scripts, TeX, and PDF actions from the sibling source repository are never executed.
- A release checkpoint changes only after schema, citation, rights, Python, frontend, and production-build gates pass.
- No-update runs retain the last accepted report instead of fabricating a pulse.
- Public builds read only the separately sealed and audited `public-release/` tree.

The architectural rationale is in [ADR 0001](docs/adr/0001-curated-local-releases.md), [ADR 0002](docs/adr/0002-guarded-scheduled-publication.md), [ADR 0003](docs/adr/0003-literature-first-monitoring.md), and [ADR 0004](docs/adr/0004-automatic-fail-closed-editorial.md); the implemented file map is in [docs/implementation-plan.md](docs/implementation-plan.md), and exact operator procedures are in [docs/operations.md](docs/operations.md).

## Install and run

Prerequisites: Node.js 22.12 or newer and Python 3.11 or newer.

```bash
export PROJECT_ROOT="$(pwd)"
export IMF_SOURCE_ROOT="${PROJECT_ROOT}/../imf"

npm ci
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
npm run dev
```

Vite binds to `127.0.0.1`. It does not serve private snapshots, source extracts, run records, or `$IMF_SOURCE_ROOT`.

Run the complete verification suite with:

```bash
.venv/bin/python -m pytest
npm test
npm run build
```

After a successful local publication, `npm run preview` serves the immutable site build selected by `data/current.json`. Before one exists, it previews the ordinary local `dist/` build.

## Local research pipeline

`config/sources.yaml` defaults to the sibling `../imf`; `IMF_SOURCE_ROOT` can select another authorized read-only root. Each registered source path is explicit and relative. Recursive crawling, globs, `..`, symlinks, non-regular files, and path escapes are rejected.

The lower-level manual sequence is:

```bash
# 1. Copy only allowlisted bytes into an immutable private snapshot.
.venv/bin/python scripts/sync_local_sources.py

# 2. Statically extract and validate a specific snapshot printed by step 1.
.venv/bin/python scripts/ingest_sources.py \
  --snapshot-directory imports/imf/snapshots/SNAPSHOT_ID

# 3. Publish a reviewed release candidate and its prepared pulse/artifact.
.venv/bin/python scripts/publish_pulse.py \
  --release-id RELEASE_ID \
  --pulse content/pulses/YYYY-MM-DD-N.md \
  --artifact-manifest public/artifacts/YYYY-MM-DD/ARTIFACT_ID/manifest.json
```

`N` is a one-based pulse index, so independently reviewed releases can publish
more than one immutable pulse on a calendar date. Legacy date-only pulses are
retained as index `1` without rewriting their accepted bytes.

Synchronization does not select a release. Ingestion never executes source code. Publication binds accepted pulse and artifact bytes, runs every gate, then replaces `data/current.json` last. On failure, the accepted pointer remains unchanged.

If the sibling repository cannot be read from an authorized local context, create an explicit snapshot instead of widening permissions:

```bash
.venv/bin/python scripts/export_local_snapshot.py --source-root "$IMF_SOURCE_ROOT"
```

The exported snapshot must be selected explicitly. Its age remains visible, and it must never be described as a fresh live scan.

## Deterministic novelty and pulse proposals

`research_pipeline/novelty.py` compares two immutable releases. It ranks only newly appended, evidence-backed objects, uses integer basis-point scoring, deduplicates prior proposal fingerprints, preserves contradiction and different-target flags, and selects at most three signals. Deletions, in-place mutations, source-only changes, and ambiguous first-release state route to review.

`scripts/build_daily_pulse.py` can write an immutable change analysis. It renders Markdown only when a separately reviewed structured proposal is hash-bound to that exact analysis and release. It does not invent a proposal or silently overwrite a pulse.

## External monitoring

Phase 4 is a bounded literature metadata monitor configured in `config/external-sources.yaml`. arXiv covers preprints; Crossref covers journal articles, proceedings papers, books, monographs, and chapters. Because this is a mature field, discovery spans arXiv's operating history and up to 100 years of Crossref records rather than only recent publications. Network and review volume remain bounded by fixed HTTPS hosts and paths, query vocabulary, work types, response sizes, and small result caps. Raw provider receipts are private and immutable; candidate batches contain normalized metadata and hashes, not abstract text, PDFs, or code.

Candidate ordering follows the editorial contract in `config/pulse.yaml`: published papers, books, chapters, preprints, then local research context. Accepted source versions are filtered twice, while `scripts/build_editorial_context.py` gives the editor sealed publication history for semantic comparison. Crossref results must contain an exact configured topic phrase in title, venue, subject, or abstract metadata; fuzzy API ranking alone cannot admit a candidate. OpenAlex and Unpaywall are not enabled because their current APIs require user-specific keys or contact details. They can be added later through reviewed local configuration without embedding credentials.

Metadata discoveries do not block the daily transaction. Unresolved exact records remain available in later batches, and the deterministic comparison helper still distinguishes definition drift, different targets, exact-scope contradictions, and review gaps without semantic guessing. Manual `approved` and `rejected` decisions remain available for curation, but the unattended path uses a separate ignored, schema-validated editorial package bound to one exact arXiv candidate and one safely fetched primary PDF. Metadata alone never becomes evidence.

See [docs/operations.md](docs/operations.md) for the exact search, review, and comparison commands.

## One daily command

For the current Moscow calendar date, the only supported daily transaction is:

```bash
DATE="$(TZ=Europe/Moscow date +%F)"
.venv/bin/python scripts/run_daily_pipeline.py \
  --project-root "$PROJECT_ROOT" \
  --mode live \
  --date "$DATE"
```

It emits one compact JSON object:

| Status | Meaning |
| --- | --- |
| `published` | One automatically verified or manually reviewed proposal and artifact passed every gate and advanced the release. |
| `no_update` | No material development was selected; no pulse was created. |
| `review_required` | Reserved for legacy/manual dependency injection; production metadata discovery is deferred instead of blocking. |
| `blocked` | A required reviewed configuration, executable, source root, or permission is unavailable. |
| `failed` | A processing or validation gate failed; the accepted checkpoint is preserved. |

The daily research command itself never runs Git. The independent 06:00 `Europe/Moscow` Codex task calls the guarded wrapper instead:

```bash
.venv/bin/python scripts/run_scheduled_pipeline.py \
  --project-root "$PROJECT_ROOT" \
  --date "$DATE"
```

The scheduled editor searches first and writes one ignored, hash-bound search outcome for the date. A successful outcome binds the exact immutable candidate batch used for editorial preparation; a provider timeout records a deferred outcome. The wrapper consumes that outcome instead of repeating the network search, requires clean local `main` exactly synchronized with the approved public `origin`, and runs the daily transaction once. A transient metadata timeout therefore produces a safe `no_update` while local processing and checkpoint refresh continue. A `published` result is exported and audited, then only current-date pulse/artifact files, curated knowledge files, and the sealed `public-release/` files may be committed and pushed. The wrapper waits for the matching Pages workflow to succeed. Every other status performs no Git or deployment action. It never force-pushes, tags, opens a pull request, or changes GitHub settings.

## Public release and GitHub Pages

Private releases contain source-derived extracts and must never be committed or deployed. The public boundary is produced and verified separately:

```bash
.venv/bin/python scripts/export_public_release.py --output public-release
.venv/bin/python scripts/audit_public_release.py --directory public-release

IMF_PULSE_PUBLIC_RELEASE_DIR=public-release \
VITE_BASE_PATH=/imf-pulse/ \
VITE_ROUTER_MODE=hash \
npm run build
```

The export contains only a sanitized current summary, five public knowledge JSONL files, accepted pulses, and cleared artifacts. It rejects raw source text, snapshots, extracts, run logs, absolute home paths, credential-like data, symlinks, extra files, hash mismatches, and media without public rights.

A push to `main` or an explicit dispatch of `.github/workflows/pages.yml` re-audits `public-release/`, runs frontend tests, builds with the project-site base path, rejects source maps, and deploys only `dist/`. Code, configuration, workflow, deletion, rename, and manual-dispatch changes run the full Python suite too. A strictly classified content-only pulse commit skips that duplicate Python run because the guarded local publication transaction already passed it.

## Artifact and rights policy

Scientific charts include normalized data, a declarative specification, and a deterministic rendering. Web images require recorded creator, origin, retrieval date, caption, relevance, and cleared reuse rights. Existing source figures and draft-page images remain private because no reuse permission was found.

Generated images are optional conceptual aids. They must display:

> Conceptual illustration — not research evidence

The report must remain valid without them.

This repository is public but intentionally has no open-source license. Public visibility does not grant permission to copy, modify, or redistribute its code, content, or artifacts; the owner may add a license later.
