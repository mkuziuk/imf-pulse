# Source, permission, and rights audit

Last reviewed: 2026-07-23

## Access and immutability

The project reads the sibling source repository through `$IMF_SOURCE_ROOT`, which defaults to `$PROJECT_ROOT/../imf`. No permission expansion is required in the normal local setup. The source is inspected without writing to it and without executing its notebooks, Python, TeX, JavaScript, or embedded PDF actions.

The audited source worktree is not byte-equivalent to its Git commit because it contains local generated and untracked TeX material. Git metadata is retained only as context. Every selected file is explicitly allowlisted, copied into an immutable private snapshot, and identified by its own SHA-256.

Several notebooks contain `exec`-style cross-notebook execution, directory creation, and figure-writing cells. A diagnostic Python script writes CSV and JSON beside itself. These files are parsed as inert data only. Notebook HTML, JavaScript, widgets, binary images, and active attachments are excluded from extraction.

## Theory status

`IMF.pdf` is a 21-page internal draft dated 2026-07-03. It contains incomplete and unproved material, including an unproved Lemma 2.5 and visibly unfinished Proposition 4.1. It is authoritative only for what the draft literally states, not for mathematical truth.

Direct visual inspection found a material notation disagreement. Page 5 distinguishes the one-step smoother \(W^{(k)}\) from the recursive component operator \(\mathcal W^{(k)}=W^{(k)}A^{(k-1)}\), while Lemma 2.5 on page 6 is printed with ordinary \(W^{(k)}\). Later internal review notes transcribe the lemma with calligraphic \(\mathcal W^{(k)}\). Text extraction flattens these glyphs, so the knowledge base records a reviewed visual observation tied to the PDF hash and page locators. It follows the rendered PDF for the literal statement and leaves authorial intent unresolved. No draft screenshot is republished.

## Rights

No source-repository `LICENSE`, `COPYING`, `NOTICE`, or image-reuse declaration was found. Source notebook figures, TeX figures, PDF pages, and screenshots therefore remain `internal / reuse unknown` and are excluded from public export.

One locally stored historical paper visibly carries publisher redistribution restrictions; another record is only a URL locator. Neither is republished as page imagery.

The initial public artifact is a project-generated scientific chart with normalized CSV, declarative specification, deterministic SVG, caption, source hashes, and limitations. The project owner approved public deployment of that generated artifact on 2026-07-23, and the sealed `public-release/` manifest records the approval. This approval does not extend to source documents or figures.

Web images require an explicit license or reuse decision before export. Generated illustrations may be used only as optional explanation and must display `Conceptual illustration — not research evidence`.

## External-source boundary

External monitoring is limited to configured arXiv Atom and Crossref JSON metadata over two allowlisted HTTPS endpoints. The monitor stores immutable private response receipts and normalized candidate metadata; it does not place abstract text in public batches, download PDFs or code, follow source instructions, or accept claims. Crossref supplies discovery for journal articles, proceedings, books, monographs, and chapters; exact topic-phrase filtering is applied after retrieval.

Each candidate version remains pending until an operator appends an `approved` or `rejected` decision bound to its exact hash. Approval authorizes review of that metadata identity only. Research extraction, knowledge acceptance, rights clearance, and pulse publication remain separate decisions.

## Scheduled-environment fallback

The 08:00 `Europe/Moscow` task runs locally and expects live read access to `$IMF_SOURCE_ROOT`. If access is unavailable, the daily command returns `blocked`; it does not broaden permissions or silently switch inputs.

An authorized operator can run `scripts/export_local_snapshot.py --source-root "$IMF_SOURCE_ROOT"` to create an explicit private fallback snapshot. Any later manual processing must select that snapshot explicitly, verify its manifest, and report its age. A stale snapshot must never be described as a fresh live scan.

## Public-repository boundary

Private snapshots, extracts, release transactions, run logs, and site-build caches are ignored by Git. `scripts/export_public_release.py` constructs a strict, hash-bound allowlist containing only sanitized public knowledge, accepted pulses, and cleared artifacts. `scripts/audit_public_release.py` independently rejects unexpected files, symlinks, hash changes, private/raw fields, absolute home paths, credential-like text, and uncleared rights before GitHub Pages can build.
