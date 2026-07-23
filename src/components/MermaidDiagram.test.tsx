import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mermaidMocks = vi.hoisted(() => ({
  initialize: vi.fn(),
  renderDiagram: vi.fn()
}));

vi.mock("mermaid", () => ({
  default: {
    initialize: mermaidMocks.initialize,
    render: mermaidMocks.renderDiagram
  }
}));

import {
  isSafeMermaidSource,
  MermaidDiagram,
  sanitizeMermaidSvg
} from "./MermaidDiagram";

describe("MermaidDiagram", () => {
  beforeEach(() => {
    mermaidMocks.renderDiagram.mockResolvedValue({
      svg: '<svg xmlns="http://www.w3.org/2000/svg"><text>Safe map</text></svg>',
      bindFunctions: vi.fn()
    });
  });

  it("initializes Mermaid in strict mode and inserts sanitized SVG", async () => {
    render(<MermaidDiagram source="flowchart LR\n A --> B" label="Method map" />);
    await waitFor(() => expect(screen.getByText("Safe map")).toBeInTheDocument());
    expect(mermaidMocks.initialize).toHaveBeenCalledWith(
      expect.objectContaining({ securityLevel: "strict", startOnLoad: false })
    );
    expect(screen.getByRole("img", { name: "Method map" })).toBeVisible();
  });

  it("rejects directives that can alter the security boundary", () => {
    render(<MermaidDiagram source={'%%{init: {"securityLevel":"loose"}}%%\nflowchart LR\nA-->B'} />);
    expect(screen.getByRole("note")).toHaveTextContent(/diagram unavailable/i);
    expect(mermaidMocks.renderDiagram).not.toHaveBeenCalled();
  });

  it("rejects YAML config frontmatter and removes URL-bearing style blocks", () => {
    const source = `---
config:
  themeCSS: '.node { filter: url(https://attacker.invalid/f.svg#x); }'
---
flowchart LR
A-->B`;
    expect(isSafeMermaidSource(source)).toBe(false);

    const sanitized = sanitizeMermaidSvg(
      '<svg xmlns="http://www.w3.org/2000/svg"><style>.node { filter: url(https://attacker.invalid/f.svg#x); }</style><text>Safe</text></svg>'
    );
    expect(sanitized).not.toContain("attacker.invalid");
    expect(sanitized).not.toContain("<style");

    render(<MermaidDiagram source={source} />);
    expect(screen.getByRole("note")).toHaveTextContent(/diagram unavailable/i);
    expect(mermaidMocks.renderDiagram).not.toHaveBeenCalled();
  });
});
