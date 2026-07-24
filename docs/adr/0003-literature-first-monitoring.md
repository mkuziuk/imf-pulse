# ADR 0003: Literature-first monitoring

Date: 2026-07-24
Status: accepted

## Context

The initial monitor used two narrow arXiv searches while most novelty analysis originated in the sibling IMF repository. That made the site sensitive to local editing activity but weak at discovering published journal work, proceedings papers, books, and chapters. A broad crawler or automatic full-text downloader would violate the MVP's provenance, rights, and review boundaries.

## Decision

Use reviewed, bounded scholarly metadata APIs as the primary discovery layer. arXiv remains the preprint provider. Crossref becomes the credential-free provider for DOI-registered journal articles, proceedings papers, books, monographs, and chapters. Discovery spans arXiv's operating history and a century of Crossref metadata so foundational work is eligible; publication age is not treated as novelty. Requests use exact HTTPS host/path allowlists, configured topic phrases and work types, finite date windows, result and byte caps, rejected redirects, immutable private receipts, and deterministic DOI identities.

Crossref's relevance ranking is not trusted by itself: a result must also contain an exact configured topic phrase in its title, venue, subject, or abstract metadata. Public candidate batches contain normalized bibliographic metadata and only a hash of any abstract. Metadata is discovery information, never substantive research evidence.

Editorial ordering is published primary papers, scholarly books, chapters, preprints, then local IMF research. Local changes should enter a pulse chiefly when they reproduce, contradict, test, or clarify reviewed literature. Every external candidate remains exact-hash review-gated; approval does not download a work or accept its claims.

OpenAlex and Unpaywall are deferred until an operator supplies their required API key or contact configuration. Credentials must remain local and must never be committed.

## Consequences

- Daily runs search a broader scholarly surface without becoming a crawler.
- The first run after enabling a query may stop at `review_required`; this is expected.
- Books and journal literature can drive future reports after content review, while local experiments provide context.
- Paywalled or unclear-rights material can be discovered and cited bibliographically but cannot be downloaded or republished automatically.
