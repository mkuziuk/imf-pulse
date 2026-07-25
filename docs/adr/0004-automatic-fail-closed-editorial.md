# ADR 0004: Automatic fail-closed literature editorial

Date: 2026-07-24
Status: accepted

## Context

The metadata monitor previously routed every unresolved candidate to `review_required`. That preserved evidence quality but made a daily public site depend on the owner being present. Metadata discovery is also broader than evidence availability: Crossref can identify a book or journal paper without providing a legally accessible primary text.

## Decision

Unresolved metadata is deferred and never blocks a daily run. A scheduled GPT-5.6 Sol editor may prepare one private automatic package for an exact arXiv candidate. Before fetching a PDF, it must compare the candidate with sealed accepted publication context and proceed only for a distinct method, result, experiment, contradiction, or open question. It must then fetch the official primary PDF through `scripts/fetch_arxiv_evidence.py`, inspect it statically, cite exact pages, separate source statements from inference, and bind every public record and pulse field to the candidate and PDF hashes.

`research_pipeline/automatic.py` independently validates the package, trusted host, immutable PDF bytes, PDF structure, page extracts, evidence references, append-only knowledge changes, deterministic novelty selection, and one or more explanatory artifacts. It accepts project-generated diagrams, hash-bound generated illustrations, and exact-locator source figures only when public reuse rights are explicitly cleared. It rejects JavaScript, additional actions, encryption, embedded files, path escapes, mismatched hashes, unsupported source classes, missing page locators, altered accepted records, unknown-rights media, and prose that does not match the selected evidence. A failure rolls back all materialized files and leaves the accepted checkpoint unchanged.

Crossref-only records, inaccessible primary texts, ambiguous claims, definition mismatches, and weak developments are skipped. They may remain discoverable for later manual curation but cannot become automatic evidence. Local changes without a matching verified package can advance the evidence release without creating a pulse.

## Consequences

The owner no longer needs to approve every morning's metadata queue. Exact identity and body deduplication remain hard transaction gates. The site may still have no new pulse on a given day; that is a successful `no_update`, not a reason to manufacture a report. The automatic path is intentionally narrower than the discovery path and currently supports only official arXiv primary PDFs. Private PDFs and page extracts remain ignored and outside `public-release/`.
