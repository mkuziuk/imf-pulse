import { createHash } from "node:crypto";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  canonicalJsonHash,
  getCandidateReleaseContext,
  type AcceptedPublication
} from "./release-env";

const temporaryRoots: string[] = [];
const releaseId = `release-${"a".repeat(20)}`;
const pulse = "content/pulses/2026-07-22.md";
const manifestUrl = "/artifacts/2026-07-22/figure/manifest.json";
const fileUrl = "/artifacts/2026-07-22/figure/data.csv";
const secondManifestUrl = "/artifacts/2026-07-22/comparison/manifest.json";
const secondFileUrl = "/artifacts/2026-07-22/comparison/data.csv";
const candidateEnvironment = [
  "IMF_PULSE_RELEASE_DIR",
  "IMF_PULSE_SELECTED_PULSE",
  "IMF_PULSE_ARTIFACT_MANIFESTS",
  "IMF_PULSE_ACCEPTED_PUBLICATIONS",
  "IMF_PULSE_CHECKPOINT_STATUS"
] as const;

function digest(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function immutableFixture(
  sharedGenerator = false,
  generatorSourcePath = "scripts/generate_fixture.py"
): {
  root: string;
  releaseDirectory: string;
  publication: AcceptedPublication;
} {
  const root = mkdtempSync(join(tmpdir(), "imf-pulse-release-env-"));
  temporaryRoots.push(root);
  const releaseDirectory = join(root, "data", "releases", releaseId);
  const pulsePath = join(releaseDirectory, "publication", "content", "pulses", "2026-07-22.md");
  const artifacts = [
    {
      id: "figure",
      manifestUrl,
      fileUrl,
      manifestBytes: '{"artifact_id":"figure"}\n',
      fileBytes: "stage,value\n1,2\n"
    },
    ...(sharedGenerator
      ? [
          {
            id: "comparison",
            manifestUrl: secondManifestUrl,
            fileUrl: secondFileUrl,
            manifestBytes: '{"artifact_id":"comparison"}\n',
            fileBytes: "stage,value\n1,3\n"
          }
        ]
      : [])
  ];
  mkdirSync(join(root, "data", "releases"), { recursive: true });
  mkdirSync(dirname(pulsePath), { recursive: true });
  const pulseBytes = "---\nstatus: published\n---\nIMMUTABLE_PULSE\n";
  const generatorBytes = "# deterministic fixture generator\n";
  writeFileSync(pulsePath, pulseBytes);
  const generatorPath = join(releaseDirectory, "publication", generatorSourcePath);
  mkdirSync(dirname(generatorPath), { recursive: true });
  writeFileSync(generatorPath, generatorBytes);
  const payloadHashes: Record<string, string> = {
    [pulse]: digest(pulseBytes),
    [generatorSourcePath]: digest(generatorBytes)
  };
  const metadataArtifacts = artifacts.map((artifact) => {
    const manifestSourcePath = `public${artifact.manifestUrl}`;
    const fileSourcePath = `public${artifact.fileUrl}`;
    const manifestPath = join(releaseDirectory, "publication", manifestSourcePath);
    const filePath = join(releaseDirectory, "publication", fileSourcePath);
    mkdirSync(dirname(manifestPath), { recursive: true });
    writeFileSync(manifestPath, artifact.manifestBytes);
    writeFileSync(filePath, artifact.fileBytes);
    payloadHashes[manifestSourcePath] = digest(artifact.manifestBytes);
    payloadHashes[fileSourcePath] = digest(artifact.fileBytes);
    return {
      artifact_id: artifact.id,
      manifest_url: artifact.manifestUrl,
      source_path: manifestSourcePath,
      bound_path: `publication/${manifestSourcePath}`,
      sha256: digest(artifact.manifestBytes),
      files: [
        {
          url: artifact.fileUrl,
          source_path: fileSourcePath,
          bound_path: `publication/${fileSourcePath}`,
          sha256: digest(artifact.fileBytes),
          bytes: Buffer.byteLength(artifact.fileBytes)
        }
      ],
      generator: {
        source_path: generatorSourcePath,
        bound_path: `publication/${generatorSourcePath}`,
        sha256: digest(generatorBytes),
        bytes: Buffer.byteLength(generatorBytes)
      },
      rights: { status: "project_generated_scientific_chart" }
    };
  });
  const bindingSha256 = canonicalJsonHash(payloadHashes);
  const publicationMetadata = {
    pulse: {
      id: "pulse-2026-07-22",
      source_path: pulse,
      bound_path: `publication/${pulse}`,
      sha256: digest(pulseBytes)
    },
    artifact_manifests: metadataArtifacts,
    binding_sha256: bindingSha256
  };
  writeFileSync(
    join(releaseDirectory, "publication", "binding.json"),
    `${JSON.stringify(publicationMetadata)}\n`
  );
  writeFileSync(
    join(releaseDirectory, "release.json"),
    `${JSON.stringify({ publication: publicationMetadata })}\n`
  );
  return {
    root,
    releaseDirectory,
    publication: {
      release_id: releaseId,
      pulse,
      bound_pulse: `data/releases/${releaseId}/publication/content/pulses/2026-07-22.md`,
      pulse_sha256: digest(pulseBytes),
      binding_sha256: bindingSha256,
      artifact_manifests: artifacts.map((artifact) => ({
        url: artifact.manifestUrl,
        bound_path: `data/releases/${releaseId}/publication/public${artifact.manifestUrl}`,
        sha256: digest(artifact.manifestBytes),
        files: [
          {
            url: artifact.fileUrl,
            bound_path: `data/releases/${releaseId}/publication/public${artifact.fileUrl}`,
            sha256: digest(artifact.fileBytes),
            bytes: Buffer.byteLength(artifact.fileBytes)
          }
        ]
      }))
    }
  };
}

beforeEach(() => {
  for (const name of candidateEnvironment) vi.stubEnv(name, undefined);
});

afterEach(() => {
  vi.unstubAllEnvs();
  for (const root of temporaryRoots.splice(0)) rmSync(root, { recursive: true, force: true });
});

describe("candidate build context", () => {
  it("binds a publish gate to immutable accepted pulse and artifact bytes", () => {
    const fixture = immutableFixture();
    vi.stubEnv("IMF_PULSE_RELEASE_DIR", fixture.releaseDirectory);
    vi.stubEnv("IMF_PULSE_SELECTED_PULSE", pulse);
    vi.stubEnv("IMF_PULSE_ARTIFACT_MANIFESTS", JSON.stringify([manifestUrl]));
    vi.stubEnv("IMF_PULSE_ACCEPTED_PUBLICATIONS", JSON.stringify([fixture.publication]));
    vi.stubEnv("IMF_PULSE_CHECKPOINT_STATUS", "published");

    expect(getCandidateReleaseContext(fixture.root)).toEqual({
      releaseId,
      selectedPulse: pulse,
      artifactManifests: [manifestUrl],
      acceptedPublications: [fixture.publication],
      publicationGate: true,
      checkpointStatus: "published"
    });
  });

  it("accepts two artifact manifests bound to the same deterministic generator", () => {
    const fixture = immutableFixture(true);
    vi.stubEnv("IMF_PULSE_RELEASE_DIR", fixture.releaseDirectory);
    vi.stubEnv("IMF_PULSE_SELECTED_PULSE", pulse);
    vi.stubEnv(
      "IMF_PULSE_ARTIFACT_MANIFESTS",
      JSON.stringify([manifestUrl, secondManifestUrl])
    );
    vi.stubEnv("IMF_PULSE_ACCEPTED_PUBLICATIONS", JSON.stringify([fixture.publication]));
    vi.stubEnv("IMF_PULSE_CHECKPOINT_STATUS", "published");

    expect(getCandidateReleaseContext(fixture.root)).toMatchObject({
      releaseId,
      artifactManifests: [manifestUrl, secondManifestUrl],
      publicationGate: true
    });
  });

  it("accepts a one-character project-relative generator path", () => {
    const fixture = immutableFixture(false, "g");
    vi.stubEnv("IMF_PULSE_RELEASE_DIR", fixture.releaseDirectory);
    vi.stubEnv("IMF_PULSE_SELECTED_PULSE", pulse);
    vi.stubEnv("IMF_PULSE_ARTIFACT_MANIFESTS", JSON.stringify([manifestUrl]));
    vi.stubEnv("IMF_PULSE_ACCEPTED_PUBLICATIONS", JSON.stringify([fixture.publication]));
    vi.stubEnv("IMF_PULSE_CHECKPOINT_STATUS", "published");

    expect(getCandidateReleaseContext(fixture.root)?.publicationGate).toBe(true);
  });

  it("keeps an ordinary candidate without the gate contract in preview mode", () => {
    const fixture = immutableFixture();
    vi.stubEnv("IMF_PULSE_RELEASE_DIR", fixture.releaseDirectory);
    vi.stubEnv("IMF_PULSE_SELECTED_PULSE", pulse);
    vi.stubEnv("IMF_PULSE_ARTIFACT_MANIFESTS", JSON.stringify([manifestUrl]));

    expect(getCandidateReleaseContext(fixture.root)).toMatchObject({
      releaseId,
      selectedPulse: pulse,
      artifactManifests: [manifestUrl],
      acceptedPublications: [],
      publicationGate: false
    });
  });

  it("treats an explicitly present empty accepted-publication list as a gate contract", () => {
    const fixture = immutableFixture();
    vi.stubEnv("IMF_PULSE_RELEASE_DIR", fixture.releaseDirectory);
    vi.stubEnv("IMF_PULSE_ACCEPTED_PUBLICATIONS", "[]");
    vi.stubEnv("IMF_PULSE_CHECKPOINT_STATUS", "processed_no_pulse");

    expect(getCandidateReleaseContext(fixture.root)).toMatchObject({
      releaseId,
      acceptedPublications: [],
      publicationGate: true,
      checkpointStatus: "processed_no_pulse"
    });
  });

  it("requires an allowed explicit checkpoint status for every publication gate", () => {
    const fixture = immutableFixture();
    vi.stubEnv("IMF_PULSE_RELEASE_DIR", fixture.releaseDirectory);
    vi.stubEnv("IMF_PULSE_ACCEPTED_PUBLICATIONS", "[]");

    expect(() => getCandidateReleaseContext(fixture.root)).toThrow(
      /requires IMF_PULSE_CHECKPOINT_STATUS/i
    );
    vi.stubEnv("IMF_PULSE_CHECKPOINT_STATUS", "candidate");
    expect(() => getCandidateReleaseContext(fixture.root)).toThrow(
      /published, processed_no_pulse, or unchanged/i
    );
  });

  it("keeps retained history but selects no pulse for an unchanged checkpoint", () => {
    const fixture = immutableFixture();
    vi.stubEnv("IMF_PULSE_RELEASE_DIR", fixture.releaseDirectory);
    vi.stubEnv("IMF_PULSE_ACCEPTED_PUBLICATIONS", JSON.stringify([fixture.publication]));
    vi.stubEnv("IMF_PULSE_ARTIFACT_MANIFESTS", "[]");
    vi.stubEnv("IMF_PULSE_CHECKPOINT_STATUS", "unchanged");

    expect(getCandidateReleaseContext(fixture.root)).toMatchObject({
      releaseId,
      selectedPulse: undefined,
      artifactManifests: [],
      acceptedPublications: [fixture.publication],
      publicationGate: true,
      checkpointStatus: "unchanged"
    });
  });

  it("rejects escaped pulse and manifest paths", () => {
    const fixture = immutableFixture();
    vi.stubEnv("IMF_PULSE_RELEASE_DIR", fixture.releaseDirectory);
    vi.stubEnv("IMF_PULSE_SELECTED_PULSE", "../imf/IMF.pdf");
    expect(() => getCandidateReleaseContext(fixture.root)).toThrow(/safe project-relative path/i);

    vi.stubEnv("IMF_PULSE_SELECTED_PULSE", pulse);
    vi.stubEnv(
      "IMF_PULSE_ARTIFACT_MANIFESTS",
      JSON.stringify(["/artifacts/2026-07-22/%2e%2e/manifest.json"])
    );
    expect(() => getCandidateReleaseContext(fixture.root)).toThrow(/unsafe artifact URL/i);

    vi.stubEnv(
      "IMF_PULSE_ARTIFACT_MANIFESTS",
      JSON.stringify(["/artifacts//manifest.json"])
    );
    expect(() => getCandidateReleaseContext(fixture.root)).toThrow(/unsafe artifact URL/i);

    vi.stubEnv(
      "IMF_PULSE_ARTIFACT_MANIFESTS",
      JSON.stringify(["/artifacts/2026-07-22/manifest.json?rev=1"])
    );
    expect(() => getCandidateReleaseContext(fixture.root)).toThrow(/unsafe artifact URL/i);
  });

  it("rejects a mutable-byte substitution after an accepted hash is issued", () => {
    const fixture = immutableFixture();
    writeFileSync(join(fixture.root, fixture.publication.bound_pulse), "CHANGED\n");
    vi.stubEnv("IMF_PULSE_RELEASE_DIR", fixture.releaseDirectory);
    vi.stubEnv("IMF_PULSE_SELECTED_PULSE", pulse);
    vi.stubEnv("IMF_PULSE_ARTIFACT_MANIFESTS", JSON.stringify([manifestUrl]));
    vi.stubEnv("IMF_PULSE_ACCEPTED_PUBLICATIONS", JSON.stringify([fixture.publication]));
    vi.stubEnv("IMF_PULSE_CHECKPOINT_STATUS", "published");
    expect(() => getCandidateReleaseContext(fixture.root)).toThrow(/immutable SHA-256 binding/i);
  });
});
