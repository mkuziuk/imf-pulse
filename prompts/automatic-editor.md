# Automatic literature editor

Prepare at most one automatic pulse package for today's newest immutable external batch.

1. Select only a materially relevant arXiv primary paper with an exact candidate ID and SHA-256. Prefer a development that changes a method, theorem, experiment, contradiction, or open question—not merely a new record.
2. Fetch only the official arXiv PDF with `scripts/fetch_arxiv_evidence.py`. Never fetch code, supplementary archives, Sci-Hub, or an untrusted redirect.
3. Inspect the PDF statically with the PDF skill. Do not execute embedded actions or source code. Record exact page locators and distinguish what the paper states from interpretation.
4. Create one private `data/automatic/packages/YYYY-MM-DD.json` conforming to `schemas/automatic-pulse-package.schema.json`. Bind the exact batch, candidate, candidate hash, PDF hash, source metadata, page extracts, append-only knowledge objects, and pulse prose.
5. State the robustness definition and target explicitly. Do not transfer linear-filter results to adaptive or robust recursion. Absence of an experiment is a limitation, not evidence of failure.
6. Use a deterministic project-generated diagram only when it clarifies the selected method. It is explanatory, not source evidence.
7. If any evidence, locator, identity, relevance, or scope is uncertain, do not create a package. A no-update day is valid.

The transaction, not this prompt, decides whether the package is valid and publishable. Never edit an accepted object, overwrite a pulse, advance a checkpoint, or run Git directly.
