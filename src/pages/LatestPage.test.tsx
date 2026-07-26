import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { LatestPage } from "./LatestPage";

vi.mock("../lib/data", () => ({
  getKnowledgeSnapshot: () => ({ current: undefined })
}));

vi.mock("../lib/content", () => ({
  getPulseCatalog: () => ({
    mode: "preview",
    pulses: [],
    latest: {
      id: "pulse-2026-07-24",
      date: "2026-07-24",
      pulseIndex: 1,
      title: "A visual pulse",
      lead: "A plain-language lead introduces the research object.",
      readerGuide: "This short orientation explains the central object before the technical report begins.",
      status: "published",
      topics: ["visual-test"],
      featuredArtifact: "featured-image",
      artifactManifests: [
        "/artifacts/2026-07-24/featured/manifest.json",
        "/artifacts/2026-07-24/supporting/manifest.json"
      ],
      sourceIds: ["source-test"],
      body: "## Signal 01 — A test signal\n\nReport body.",
      sourcePath: "/content/pulses/2026-07-24.md",
      issues: [],
      metadata: {
        status: "published",
        topics: ["visual-test"],
        artifact_manifests: [],
        source_ids: []
      }
    }
  })
}));

vi.mock("../hooks/useArtifacts", () => ({
  useArtifacts: () => ({
    loading: false,
    issues: [],
    artifacts: [
      {
        schema_version: "1",
        id: "featured-image",
        title: "The research object",
        artifact_class: "generated_image",
        caption: "Conceptual illustration — not research evidence",
        rights_status: "project_generated_illustration",
        stable_url: "/artifacts/2026-07-24/featured/featured.png",
        files: [
          {
            kind: "generated conceptual illustration",
            url: "/artifacts/2026-07-24/featured/featured.png",
            mime_type: "image/png"
          }
        ],
        evidence: []
      },
      {
        schema_version: "1",
        id: "supporting-image",
        title: "A second view",
        artifact_class: "generated_image",
        caption: "Conceptual illustration — not research evidence",
        rights_status: "project_generated_illustration",
        stable_url: "/artifacts/2026-07-24/supporting/supporting.png",
        files: [
          {
            kind: "generated conceptual illustration",
            url: "/artifacts/2026-07-24/supporting/supporting.png",
            mime_type: "image/png"
          }
        ],
        evidence: []
      }
    ]
  })
}));

describe("LatestPage visuals", () => {
  it("renders one featured visual and every supporting visual", () => {
    render(
      <MemoryRouter initialEntries={["/"]}>
        <Routes>
          <Route path="/" element={<LatestPage />} />
        </Routes>
      </MemoryRouter>
    );

    expect(screen.getByRole("heading", { name: "Featured visual" })).toBeVisible();
    expect(
      screen.getByRole("heading", { name: "Additional views of the topic" })
    ).toBeVisible();
    expect(screen.getByRole("heading", { name: "A second view" })).toBeVisible();
    expect(screen.getByLabelText("Reader orientation")).toHaveTextContent(
      "This short orientation explains the central object"
    );
    expect(screen.getAllByText("Conceptual illustration — not research evidence")).toHaveLength(4);
  });
});
