import { describe, expect, it } from "vitest";
import { getKnowledgeSnapshot } from "./data";
import { EvidenceRefSchema } from "./schemas";

describe("curated knowledge integration", () => {
  it("loads every canonical curated record without silent rejection", () => {
    const snapshot = getKnowledgeSnapshot();
    expect(["preview", "ready"]).toContain(snapshot.state);
    expect(snapshot.claims.length).toBeGreaterThan(0);
    expect(snapshot.methods.length).toBeGreaterThan(0);
    expect(snapshot.experiments.length).toBeGreaterThan(0);
    expect(snapshot.relationships.length).toBeGreaterThan(0);
    expect(snapshot.rejectedRecords).toBe(0);
  });

  it("preserves structured locators and reference-target definitions", () => {
    const snapshot = getKnowledgeSnapshot();
    expect(snapshot.claims.some((claim) => typeof claim.evidence[0]?.locator === "object")).toBe(true);
    expect(
      snapshot.experiments.some(
        (experiment) =>
          experiment.reference_target != null && typeof experiment.reference_target === "object"
      )
    ).toBe(true);
  });

  it("requires a source path and kind-specific precision for structured evidence", () => {
    expect(EvidenceRefSchema.safeParse({ source_id: "source-1", locator: {} }).success).toBe(false);
    expect(
      EvidenceRefSchema.safeParse({ locator: { kind: "pdf", page: 6 } }).success
    ).toBe(false);
    expect(
      EvidenceRefSchema.safeParse({
        source_id: "source-1",
        locator: { kind: "pdf", path: "IMF.pdf" }
      }).success
    ).toBe(false);
    expect(
      EvidenceRefSchema.safeParse({
        source_id: "source-1",
        locator: { kind: "pdf", path: "IMF.pdf", page: 6 }
      }).success
    ).toBe(true);
    expect(
      EvidenceRefSchema.safeParse({
        source_id: "source-1",
        locator: { kind: "text_lines", path: "../IMF/README.md", line_start: 4 }
      }).success
    ).toBe(false);
  });
});
