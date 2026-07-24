import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { withAppUrl } from "../lib/links";
import { MarkdownRenderer } from "./MarkdownRenderer";

describe("MarkdownRenderer", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("renders GFM, math, stable heading anchors, and safe citations", () => {
    render(
      <MarkdownRenderer
        markdown={`## Signal 01 — A measured result

The operator is $W_k$ and

$$e_k = W_k\\varepsilon.$$

| Stage | RMSE |
| --- | ---: |
| 1 | 0.02018 |

[IMF.pdf, p. 6](/sources#src-imf-draft)`}
      />
    );
    expect(screen.getByRole("heading", { level: 2 })).toHaveAttribute("id", "signal-01");
    expect(document.querySelector(".katex")).toBeInTheDocument();
    expect(document.querySelector("math")).not.toBeNull();
    expect(screen.getByRole("table")).toBeVisible();
    expect(screen.getByRole("link", { name: "IMF.pdf, p. 6" })).toHaveAttribute(
      "href",
      withAppUrl("/sources#src-imf-draft")
    );
  });

  it("drops raw HTML and rejects active URLs and hotlinked images", () => {
    render(
      <MarkdownRenderer
        markdown={`<script>alert('x')</script>

[unsafe](javascript:alert('x'))

![external](https://example.com/image.png)`}
      />
    );
    expect(document.querySelector("script")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "unsafe" })).not.toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(screen.getByText(/inline image omitted/i)).toBeVisible();
  });

  it("rejects protocol-relative and encoded traversal links", () => {
    render(
      <MarkdownRenderer
        markdown={`[protocol relative](//evil.example/path)

[encoded traversal](/artifacts/%252e%252e/private.txt)`}
      />
    );
    expect(screen.queryByRole("link", { name: "protocol relative" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "encoded traversal" })).not.toBeInTheDocument();
  });

  it("does not let a raw local artifact URL bypass manifest rights", () => {
    render(
      <MarkdownRenderer markdown="![uncurated](/artifacts/2026-07-22/unreviewed.png)" />
    );
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(screen.getByText(/validated manifest and rights record/i)).toBeVisible();
  });

  it("separates Pages app routes from base-prefixed artifact files", () => {
    vi.stubEnv("BASE_URL", "/imf-pulse/");
    vi.stubEnv("VITE_ROUTER_MODE", "hash");

    render(
      <MarkdownRenderer
        markdown={`[source](/sources#source-1)

[data](/artifacts/2026-07-22/chart.csv)`}
      />
    );

    expect(screen.getByRole("link", { name: "source" })).toHaveAttribute(
      "href",
      "/imf-pulse/#/sources#source-1"
    );
    expect(screen.getByRole("link", { name: "data" })).toHaveAttribute(
      "href",
      "/imf-pulse/artifacts/2026-07-22/chart.csv"
    );
  });
});
