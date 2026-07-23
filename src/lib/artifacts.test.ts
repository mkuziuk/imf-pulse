import { describe, expect, it } from "vitest";
import {
  artifactCanRenderMedia,
  artifactIsPubliclyCleared,
  manifestUrlsForPulse,
  normalizeArtifactManifest,
  parseStageErrorCsv,
  selectArtifactManifestCatalog
} from "./artifacts";
import type { PulseDocument } from "./content";
import { CurrentReleaseSchema } from "./schemas";

describe("artifact normalization", () => {
  const report = {
    id: "pulse-1",
    date: "2026-07-22",
    title: "Pulse",
    lead: "Lead",
    status: "published",
    topics: [],
    artifactManifests: [
      "/artifacts/2026-07-22/authorized/manifest.json",
      "/artifacts/2026-07-22/unreleased/manifest.json"
    ],
    sourceIds: [],
    body: "Body",
    sourcePath: "/content/pulses/2026-07-22.md",
    issues: [],
    metadata: { status: "published", topics: [], artifact_manifests: [], source_ids: [] }
  } satisfies PulseDocument;

  it("uses only pointer-authorized manifests for an accepted release", () => {
    const current = CurrentReleaseSchema.parse({
      release_id: "release-test",
      status: "published",
      pulse: "content/pulses/2026-07-22.md",
      artifact_manifests: ["/artifacts/2026-07-22/authorized/manifest.json"]
    });
    const catalog = { mode: "authorized" as const, pulses: [report] };
    expect(selectArtifactManifestCatalog(catalog, current).manifestUrls).toEqual(
      current.artifact_manifests
    );
    expect(manifestUrlsForPulse(report, current, catalog.mode)).toEqual(
      current.artifact_manifests
    );
  });

  it("retains only authoritative artifact history on a no-update pointer", () => {
    const current = CurrentReleaseSchema.parse({
      release_id: "release-no-update",
      status: "processed_no_pulse",
      pulse: null,
      latest_accepted_pulse: report.sourcePath.replace(/^\//, ""),
      accepted_pulses: [report.sourcePath.replace(/^\//, "")],
      artifact_manifests: [],
      accepted_artifact_manifests: [
        "/artifacts/2026-07-22/authorized/manifest.json"
      ]
    });
    const catalog = { mode: "retained" as const, pulses: [report], latest: report };
    expect(selectArtifactManifestCatalog(catalog, current).manifestUrls).toEqual(
      current.accepted_artifact_manifests
    );
    expect(manifestUrlsForPulse(report, current, catalog.mode)).toEqual(
      current.accepted_artifact_manifests
    );
  });

  it("accepts the canonical Phase 1 manifest aliases without losing provenance", () => {
    const artifacts = normalizeArtifactManifest(
      {
        schema_version: 1,
        artifact_id: "chart-1",
        artifact_type: "scientific_chart",
        title: "The first stage",
        caption: "A deterministic comparison.",
        rights: { status: "internal; public reuse not cleared", may_publish_publicly: false },
        files: [
          {
            role: "normalized underlying data",
            media_type: "text/csv",
            url: "/artifacts/2026-07-22/chart/chart.csv"
          }
        ],
        evidence: [{ source_id: "source-1", source_locator: "rows 2-10" }]
      },
      "/artifacts/2026-07-22/chart/manifest.json"
    );
    expect(artifacts).toHaveLength(1);
    expect(artifacts[0]).toMatchObject({
      id: "chart-1",
      artifact_class: "scientific_chart",
      rights_status: "internal; public reuse not cleared"
    });
    expect(artifacts[0].files[0]).toMatchObject({ kind: "data", mime_type: "text/csv" });
    expect(artifacts[0].evidence[0].locator).toBe("rows 2-10");
  });

  it("pivots the canonical long-form CSV into chart rows", () => {
    const csv = `stage,window_size,series_id,value
1,501,exact_single_pass,0.02018
1,501,exact_recursive,0.02018
1,501,seed777_recursive,0.02025
2,355,exact_single_pass,0.02017
2,355,exact_recursive,0.00828`;
    expect(parseStageErrorCsv(csv)).toEqual([
      {
        stage: 1,
        window: 501,
        singlePass: 0.02018,
        recursiveExact: 0.02018,
        recursiveObserved: 0.02025
      },
      { stage: 2, window: 355, singlePass: 0.02017, recursiveExact: 0.00828 }
    ]);
  });

  it("rejects absent required chart series instead of manufacturing zeroes", () => {
    const csv = `stage,window_size,series_id,value
1,501,exact_single_pass,0.02018
1,501,seed777_recursive,0.02025`;
    expect(parseStageErrorCsv(csv)).toEqual([]);
  });

  it.each(["unapproved", "internal; public reuse not cleared"])(
    "does not treat %s as cleared web-image rights",
    (rightsStatus) => {
      const artifact = normalizeArtifactManifest(
        {
          artifact_id: "web-image-1",
          artifact_type: "web_image",
          title: "External figure",
          caption: "A rights-gated image.",
          rights: { status: rightsStatus },
          files: [
            {
              role: "image",
              media_type: "image/png",
              url: "/artifacts/2026-07-22/web/image.png"
            }
          ]
        },
        "/artifacts/2026-07-22/web/manifest.json"
      )[0];
      expect(artifactIsPubliclyCleared(artifact)).toBe(false);
      expect(artifactCanRenderMedia(artifact)).toBe(false);
    }
  );
});
