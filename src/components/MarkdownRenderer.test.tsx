import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MarkdownRenderer } from "./MarkdownRenderer";

const publicSource = {
  id: "src-public-paper",
  title: "Public paper",
  authors: [],
  topics: [],
  url: "https://example.org/paper"
};

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

[Public paper, p. 6](/sources#src-public-paper)`}
        sources={[publicSource]}
      />
    );
    expect(screen.getByRole("heading", { level: 2 })).toHaveAttribute("id", "signal-01");
    expect(document.querySelector(".katex")).toBeInTheDocument();
    expect(document.querySelector("math")).not.toBeNull();
    expect(screen.getByRole("table")).toBeVisible();
    expect(screen.getByRole("link", { name: /Public paper, p\. 6/ })).toHaveAttribute(
      "href",
      "https://example.org/paper"
    );
    expect(screen.getByRole("link", { name: /Public paper, p\. 6/ })).toHaveAttribute(
      "target",
      "_blank"
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

  it("resolves source citations externally and keeps artifacts base-prefixed", () => {
    vi.stubEnv("BASE_URL", "/imf-pulse/");
    vi.stubEnv("VITE_ROUTER_MODE", "hash");

    render(
      <MarkdownRenderer
        markdown={`[source](/sources#src-public-paper)

[data](/artifacts/2026-07-22/chart.csv)`}
        sources={[publicSource]}
      />
    );

    expect(screen.getByRole("link", { name: /source/ })).toHaveAttribute(
      "href",
      "https://example.org/paper"
    );
    expect(screen.getByRole("link", { name: "data" })).toHaveAttribute(
      "href",
      "/imf-pulse/artifacts/2026-07-22/chart.csv"
    );
  });

  it("renders private and unknown source references as plain text", () => {
    render(
      <MarkdownRenderer
        markdown="[private source](/sources#src-private)"
        sources={[
          {
            id: "src-private",
            title: "Private source",
            authors: [],
            topics: [],
            location: "repo://imf/private.pdf"
          }
        ]}
      />
    );

    expect(screen.queryByRole("link", { name: "private source" })).not.toBeInTheDocument();
    expect(screen.getByText("private source")).toBeVisible();
  });
});
