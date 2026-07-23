# The Residual: design direction

## Visual thesis

An annotated laboratory journal seen through an oscilloscope: warm paper, dark editorial type, hairline measurement rules, and one spectral teal trace carrying attention.

## Content plan

- **Latest:** open on the current report itself. Show date/status, title, lead, at most three signals, one dominant scientific figure, synthesis, one testable question, and compact sources.
- **Archive:** a chronological editorial ledger, not a card grid.
- **Research map:** relationship view plus a source-backed inspector; use an accessible list fallback on small screens.
- **Artifacts:** large figure ledger with caption, provenance, rights, stable URL, and data/spec downloads.
- **Sources:** dense provenance ledger with expandable limitations and linked knowledge objects.

## Interaction thesis

1. The report title and lead enter once with a restrained vertical fade; reduced-motion users receive the final state immediately.
2. The featured trace draws once and exposes exact values on focus/hover, with a table fallback.
3. Navigation and citations use a short underline/reveal transition; research-map focus dims unrelated relationships without hiding them from assistive technology.

## System

- Typography: locally bundled Source Serif 4 for editorial reading and IBM Plex Sans for navigation, labels, and data.
- Palette: warm paper `#f3efe4`, ink `#161a19`, muted ink `#626862`, rule `#c9c5b8`, spectral teal `#007c76` with brighter chart stroke `#00a99d`.
- Layout: 12-column desktop grid, prose measure near 68 characters, figures spanning wider than prose, no generic dashboard cards.
- Motion: 120–650 ms, functional, and fully disabled by `prefers-reduced-motion`.
- Accessibility: semantic landmarks, visible skip link and focus, written confidence/status, chart summary and data fallback, Mermaid strict security, no raw Markdown HTML, AA contrast, and 44 px mobile targets.

## First report visual

Plot stage number against bandwidth-scaled RMSE using the exact linear operator table. Compare the one-step smoother, exact recursive component, and stored seed-777 recursive realization. Stage 1 is annotated as a low-pass component; stages 2–9 are marked as zero-DC details. Ship the normalized CSV, declarative chart JSON, and SVG fallback together.
