import { describe, expect, it } from "vitest";
import {
  getLatestPulse,
  selectPulseCatalog,
  type PulseDocument
} from "./content";
import { CurrentReleaseSchema } from "./schemas";

function pulse(
  id: string,
  date: string,
  status: PulseDocument["status"] = "published",
  pulseIndex?: number
): PulseDocument {
  return {
    id,
    date,
    pulseIndex: pulseIndex ?? 1,
    title: id,
    lead: `Lead for ${id}`,
    status,
    topics: [],
    artifactManifests: [`/artifacts/${date}/${id}/manifest.json`],
    sourceIds: ["source-1"],
    body: "Body",
    sourcePath: `/content/pulses/${date}${pulseIndex == null ? "" : `-${pulseIndex}`}.md`,
    issues: [],
    metadata: { status, topics: [], artifact_manifests: [], source_ids: [] }
  };
}

describe("release pulse selection", () => {
  it("resolves the exact pointer reference and fails closed when it is missing", () => {
    expect(getLatestPulse("content/pulses/2026-07-22.md")?.id).toBe("pulse-2026-07-22");
    expect(getLatestPulse("pulse-2026-07-22")?.date).toBe("2026-07-22");
    expect(getLatestPulse("content/pulses/2099-01-01.md")).toBeUndefined();
  });

  it("selects the exact candidate pulse instead of a newer unrelated document", () => {
    const selected = pulse("pulse-selected", "2026-07-21");
    const newer = pulse("pulse-newer", "2026-07-22");
    const current = CurrentReleaseSchema.parse({
      release_id: "release-test",
      status: "candidate_selected_pulse",
      pulse: "content/pulses/2026-07-21.md",
      artifact_manifests: selected.artifactManifests
    });
    expect(selectPulseCatalog([newer, selected], current)).toMatchObject({
      mode: "preview",
      pulses: [{ id: "pulse-selected" }]
    });
  });

  it("keeps multiple pulse indices on one date and selects the latest index", () => {
    const first = pulse("pulse-2026-07-22-1", "2026-07-22", "published", 1);
    const second = pulse("pulse-2026-07-22-2", "2026-07-22", "published", 2);
    const current = CurrentReleaseSchema.parse({
      release_id: "release-indexed",
      status: "published",
      pulse: "content/pulses/2026-07-22-2.md",
      latest_accepted_pulse: "content/pulses/2026-07-22-2.md",
      accepted_pulses: [first.sourcePath.replace(/^\//, ""), second.sourcePath.replace(/^\//, "")]
    });

    expect(selectPulseCatalog([first, second], current)).toMatchObject({
      latest: { id: "pulse-2026-07-22-2", pulseIndex: 2 },
      pulses: [
        { id: "pulse-2026-07-22-2" },
        { id: "pulse-2026-07-22-1" }
      ]
    });
  });

  it("accepts the real no-update pointer shape and retains the latest accepted pulse", () => {
    const retained = pulse("pulse-retained", "2026-07-22");
    const older = pulse("pulse-older", "2026-07-21");
    const draft = pulse("pulse-draft", "2026-07-23", "draft");
    const current = CurrentReleaseSchema.parse({
      schema_version: 1,
      release_id: "release-no-update",
      release_path: "data/releases/release-no-update",
      published_at: "2026-07-22T05:00:00Z",
      status: "processed_no_pulse",
      pulse: null,
      artifact_manifests: [],
      latest_accepted_pulse: "content/pulses/2026-07-22.md",
      accepted_pulses: [
        "content/pulses/2026-07-21.md",
        "content/pulses/2026-07-22.md"
      ],
      accepted_artifact_manifests: retained.artifactManifests
    });
    expect(selectPulseCatalog([draft, retained, older], current)).toMatchObject({
      mode: "retained",
      latest: { id: "pulse-retained" },
      pulses: [{ id: "pulse-retained" }, { id: "pulse-older" }]
    });
  });

  it("never guesses file order when no-update history is absent", () => {
    const published = pulse("pulse-published", "2026-07-21");
    const current = CurrentReleaseSchema.parse({
      release_id: "release-no-history",
      status: "processed_no_pulse",
      pulse: null,
      artifact_manifests: []
    });
    expect(selectPulseCatalog([published], current)).toMatchObject({
      mode: "retained",
      pulses: []
    });
  });

  it("never exposes draft or failed pointer states", () => {
    const published = pulse("pulse-published", "2026-07-21");
    const draft = pulse("pulse-draft", "2026-07-22", "draft");
    expect(selectPulseCatalog([draft, published])).toMatchObject({
      mode: "preview",
      pulses: [{ id: "pulse-published" }]
    });

    expect(CurrentReleaseSchema.safeParse({
      release_id: "release-failed",
      status: "failed",
      pulse: null,
      artifact_manifests: []
    }).success).toBe(false);
  });
});
