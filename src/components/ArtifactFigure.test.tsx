import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { normalizeArtifactManifest } from "../lib/artifacts";
import { ArtifactFigure } from "./ArtifactFigure";

describe("ArtifactFigure provenance", () => {
  it("shows manifest limitations and the full evidence qualification", () => {
    const artifact = normalizeArtifactManifest(
      {
        artifact_id: "artifact-1",
        artifact_type: "diagram",
        title: "Operator diagram",
        caption: "A local diagram.",
        rights: { status: "project_generated_diagram", local_display_allowed: true },
        limitations: ["Valid only for periodic wrap boundaries."],
        evidence: [
          {
            source_id: "source-1",
            source_locator: "operator.csv rows 2-10",
            statement: "The first stage retains DC gain.",
            status: "calculated exact",
            confidence: "high within assumptions"
          }
        ]
      },
      "/artifacts/2026-07-22/operator/manifest.json"
    )[0];

    render(
      <MemoryRouter>
        <ArtifactFigure artifact={artifact} featured />
      </MemoryRouter>
    );

    const limitations = screen.getByText("Scope and limitations").closest("details");
    expect(limitations).toHaveAttribute("open");
    expect(within(limitations!).getByText(/periodic wrap boundaries/i)).toBeVisible();
    expect(screen.getByText("The first stage retains DC gain.")).toBeVisible();
    expect(screen.getByText(/calculated exact · high within assumptions/i)).toBeVisible();
  });
});
