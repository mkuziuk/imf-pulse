import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { StructuralDiagram } from "./StructuralDiagram";

const diagram = {
  schema_version: 1,
  artifact_id: "automatic-test-diagram-2026-07-24",
  title: "A responsive decomposition",
  nodes: [
    { id: "input", label: "Space-time data cube" },
    { id: "space", label: "Spatial extraction" },
    { id: "time", label: "Temporal extraction" }
  ],
  edges: [
    { from: "input", to: "space", label: "estimate support" },
    { from: "space", to: "time", label: "update residual" }
  ]
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("StructuralDiagram", () => {
  it("renders a validated JSON diagram as responsive HTML", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify(diagram), {
          headers: { "content-type": "application/json" }
        })
      )
    );

    render(
      <StructuralDiagram
        specUrl="/artifacts/test/diagram.json"
        fallbackUrl="/artifacts/test/diagram.svg"
        caption="A test flow."
      />
    );

    expect(await screen.findByRole("img", { name: "A responsive decomposition" })).toBeVisible();
    expect(screen.getByText("Space-time data cube")).toBeVisible();
    expect(screen.getByText("Spatial extraction")).toBeVisible();
    expect(screen.getByText("Temporal extraction")).toBeVisible();
    expect(screen.getByText("estimate support")).toBeVisible();
    expect(screen.queryByText("Loading responsive diagram.")).not.toBeInTheDocument();
  });

  it("keeps the immutable SVG fallback when the JSON is invalid", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ ...diagram, edges: [] }), {
          headers: { "content-type": "application/json" }
        })
      )
    );

    render(
      <StructuralDiagram
        specUrl="/artifacts/test/diagram.json"
        fallbackUrl="/artifacts/test/diagram.svg"
        caption="A test flow."
      />
    );

    await waitFor(() => {
      expect(screen.queryByText("Loading responsive diagram.")).not.toBeInTheDocument();
    });
    expect(screen.getByRole("img", { name: "A test flow." })).toHaveAttribute(
      "src",
      "/artifacts/test/diagram.svg"
    );
  });
});
