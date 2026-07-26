import { createHash } from "node:crypto";
import { mkdirSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import {
  researchContentPlugin,
  selectBundledArtifactAssets,
  selectBundledResearchContent,
  serializeBundledResearchContent,
  VIRTUAL_CONTENT_ID
} from "./content-bundle";
import {
  canonicalJsonHash,
  type AcceptedPublication,
  type CandidateReleaseContext
} from "./release-env";

const temporaryRoots: string[] = [];

function digest(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function fixtureRoot(): string {
  const root = mkdtempSync(join(tmpdir(), "imf-pulse-content-"));
  temporaryRoots.push(root);
  mkdirSync(join(root, "content", "pulses"), { recursive: true });
  mkdirSync(join(root, "knowledge", "curated"), { recursive: true });
  mkdirSync(join(root, "data", "releases"), { recursive: true });
  mkdirSync(join(root, "public"), { recursive: true });
  return root;
}

function write(root: string, relative: string, value: string): void {
  const path = join(root, relative);
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, value);
}

function writePublicReleaseFixture(
  root: string,
  options: { cleared?: boolean; sourceMarker?: string } = {}
): void {
  const releaseId = `release-${"a".repeat(20)}`;
  const date = "2026-07-21";
  const checkpointTimestamp = "2026-07-23T00:00:00Z";
  const manifestUrl = `/artifacts/${date}/figure/manifest.json`;
  const dataUrl = `/artifacts/${date}/figure/data.csv`;
  const cleared = options.cleared ?? true;
  const rights = cleared
    ? {
        status: "project_generated_scientific_chart",
        local_display_allowed: true,
        may_publish_publicly: true,
        public_deployment_requires_owner_approval: false,
        public_deployment_approved_by: "project_owner",
        public_deployment_approved_on: "2026-07-23",
        public_deployment_approval_scope: "project-generated artifact public deployment"
      }
    : {
        status: "project_generated_scientific_chart",
        local_display_allowed: true,
        may_publish_publicly: false,
        public_deployment_requires_owner_approval: true
      };
  const artifact = JSON.stringify({
    artifact_id: "public-fixture",
    artifact_type: "scientific_chart",
    title: "Public fixture",
    caption: "Fixture",
    stable_url: dataUrl,
    rights,
    files: [{ url: dataUrl, media_type: "text/csv", role: "data" }]
  });
  const current = JSON.stringify({
    schema_version: 1,
    release_id: releaseId,
    status: "published",
    pulse: `content/pulses/${date}.md`,
    artifact_manifests: [manifestUrl],
    latest_accepted_pulse: `content/pulses/${date}.md`,
    accepted_pulses: [`content/pulses/${date}.md`],
    accepted_artifact_manifests: [manifestUrl],
    latest_accepted_artifact_manifests: [manifestUrl],
    pulse_reader_guides: {
      [`pulse-${date}`]:
        "A plain-language orientation explains the accepted report without altering its immutable source bytes."
    },
    last_checked_at: checkpointTimestamp
  });
  const files: Record<string, string> = {
    "current.json": `${current}\n`,
    "knowledge/sources.jsonl": `${JSON.stringify({ id: "public-source", title: options.sourceMarker ?? "PUBLIC_SOURCE" })}\n`,
    "knowledge/claims.jsonl": `${JSON.stringify({ id: "public-claim", normalized_text: "PUBLIC_CLAIM" })}\n`,
    "knowledge/methods.jsonl": `${JSON.stringify({ id: "public-method", title: "PUBLIC_METHOD" })}\n`,
    "knowledge/experiments.jsonl": `${JSON.stringify({ id: "public-experiment", title: "PUBLIC_EXPERIMENT" })}\n`,
    "knowledge/relationships.jsonl": `${JSON.stringify({ id: "public-relationship" })}\n`,
    [`pulses/${date}.md`]: `---\nstatus: published\n---\nPUBLIC_PULSE\n`,
    [`artifacts/${date}/figure/manifest.json`]: `${artifact}\n`,
    [`artifacts/${date}/figure/data.csv`]: "stage,value\n1,1\n"
  };
  for (const [relative, value] of Object.entries(files)) {
    write(root, `public-release/${relative}`, value);
  }
  const fileMap = Object.fromEntries(
    Object.entries(files).map(([relative, value]) => [relative, digest(value)])
  );
  const contentSha256 = canonicalJsonHash(fileMap);
  write(
    root,
    "public-release/manifest.json",
    `${JSON.stringify({
      schema_version: 1,
      kind: "imf-pulse-public-release",
      public_release_id: `public-${contentSha256.slice(0, 20)}`,
      source_release_id: releaseId,
      created_at: checkpointTimestamp,
      approval: {
        actor: "project_owner",
        approved_on: "2026-07-23",
        scope: "project-generated artifact public deployment"
      },
      file_count: Object.keys(fileMap).length,
      content_sha256: contentSha256,
      files: fileMap
    })}\n`
  );
}

function writeSealedRelease(
  root: string,
  fingerprintCharacter: string,
  files: Record<string, string>,
  publication?: Record<string, unknown>,
  acceptedPublications?: AcceptedPublication[]
): { releaseId: string; manifest: Record<string, unknown>; sha256: string } {
  const inputFingerprint = fingerprintCharacter.repeat(64);
  const releaseId = `release-${inputFingerprint.slice(0, 20)}`;
  for (const [relative, bytes] of Object.entries(files)) {
    write(root, `data/releases/${releaseId}/${relative}`, bytes);
  }
  const manifest: Record<string, unknown> = {
    schema_version: 1,
    release_id: releaseId,
    created_at: "2026-07-22T05:00:00Z",
    status: "candidate",
    snapshot_id: "snapshot-fixture",
    config_sha256: "d".repeat(64),
    input_fingerprint: inputFingerprint,
    semantic_fingerprint: "e".repeat(64),
    runtime: { pipeline: "test", python: "3.14" },
    files: Object.fromEntries(
      Object.entries(files)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([relative, bytes]) => [relative, digest(bytes)])
    ),
    warnings: {},
    ...(publication ? { publication } : {}),
    ...(acceptedPublications
      ? { accepted_publications_sha256: canonicalJsonHash(acceptedPublications) }
      : {})
  };
  write(root, `data/releases/${releaseId}/release.json`, `${JSON.stringify(manifest)}\n`);
  return { releaseId, manifest, sha256: canonicalJsonHash(manifest) };
}

function acceptedFixture(
  root: string,
  fingerprintCharacter = "a",
  date = "2026-07-21",
  pulseMarker = "BOUND_SELECTED_PULSE",
  priorPublications: AcceptedPublication[] = []
): {
  publication: AcceptedPublication;
  candidate: CandidateReleaseContext;
  releaseManifestSha256: string;
} {
  const inputFingerprint = fingerprintCharacter.repeat(64);
  const releaseId = `release-${inputFingerprint.slice(0, 20)}`;
  const pulse = `content/pulses/${date}.md`;
  const pulseBytes = `---\nstatus: published\n---\n${pulseMarker}\n`;
  const manifestUrl = `/artifacts/${date}/figure/manifest.json`;
  const manifestBytes = `{"artifact_id":"bound-figure-${fingerprintCharacter}"}\n`;
  const fileUrl = `/artifacts/${date}/figure/data.csv`;
  const fileBytes = fingerprintCharacter === "a"
    ? "BOUND_ASSET_BYTES\n"
    : `BOUND_ASSET_BYTES_${fingerprintCharacter}\n`;
  const generatorSourcePath = "scripts/generate_fixture.py";
  const generatorBytes = "# deterministic fixture generator\n";
  const manifestSourcePath = `public${manifestUrl}`;
  const fileSourcePath = `public${fileUrl}`;
  const bindingSha256 = canonicalJsonHash({
    [pulse]: digest(pulseBytes),
    [manifestSourcePath]: digest(manifestBytes),
    [fileSourcePath]: digest(fileBytes),
    [generatorSourcePath]: digest(generatorBytes)
  });
  const publicationMetadata = {
    pulse: {
      id: "pulse-2026-07-21",
      source_path: pulse,
      bound_path: `publication/${pulse}`,
      sha256: digest(pulseBytes)
    },
    artifact_manifests: [
      {
        artifact_id: "bound-figure",
        manifest_url: manifestUrl,
        source_path: manifestSourcePath,
        bound_path: `publication/${manifestSourcePath}`,
        sha256: digest(manifestBytes),
        files: [
          {
            url: fileUrl,
            source_path: fileSourcePath,
            bound_path: `publication/${fileSourcePath}`,
            sha256: digest(fileBytes),
            bytes: Buffer.byteLength(fileBytes)
          }
        ],
        generator: {
          source_path: generatorSourcePath,
          bound_path: `publication/${generatorSourcePath}`,
          sha256: digest(generatorBytes),
          bytes: Buffer.byteLength(generatorBytes)
        },
        rights: { status: "project_generated_scientific_chart" }
      }
    ],
    binding_sha256: bindingSha256
  };
  const bindingBytes = `${JSON.stringify(publicationMetadata)}\n`;
  const publication: AcceptedPublication = {
    release_id: releaseId,
    pulse,
    bound_pulse: `data/releases/${releaseId}/publication/${pulse}`,
    pulse_sha256: digest(pulseBytes),
    binding_sha256: bindingSha256,
    artifact_manifests: [
      {
        url: manifestUrl,
        bound_path: `data/releases/${releaseId}/publication/${manifestSourcePath}`,
        sha256: digest(manifestBytes),
        files: [
          {
            url: fileUrl,
            bound_path: `data/releases/${releaseId}/publication/${fileSourcePath}`,
            sha256: digest(fileBytes),
            bytes: Buffer.byteLength(fileBytes)
          }
        ]
      }
    ]
  };
  const sealed = writeSealedRelease(
    root,
    fingerprintCharacter,
    {
      "claims.jsonl": "SELECTED_RELEASE\n",
      [`publication/${pulse}`]: pulseBytes,
      [`publication/${manifestSourcePath}`]: manifestBytes,
      [`publication/${fileSourcePath}`]: fileBytes,
      [`publication/${generatorSourcePath}`]: generatorBytes,
      "publication/binding.json": bindingBytes
    },
    publicationMetadata,
    [...priorPublications, publication]
  );
  return {
    publication,
    candidate: {
      releaseId,
      selectedPulse: pulse,
      artifactManifests: [manifestUrl],
      acceptedPublications: [publication],
      publicationGate: true,
      checkpointStatus: "published"
    },
    releaseManifestSha256: sealed.sha256
  };
}

function committedPointer(
  publication: AcceptedPublication,
  releaseSha256: string,
  acceptedPublications: AcceptedPublication[] = [publication]
): Record<string, unknown> {
  const manifests = publication.artifact_manifests.map((manifest) => manifest.url);
  const latest = acceptedPublications.at(-1);
  const acceptedManifests = [
    ...new Set(
      acceptedPublications.flatMap((accepted) =>
        accepted.artifact_manifests.map((manifest) => manifest.url)
      )
    )
  ];
  return {
    schema_version: 1,
    release_id: publication.release_id,
    release_path: `data/releases/${publication.release_id}`,
    release_sha256: releaseSha256,
    status: "published",
    pulse: publication.pulse,
    artifact_manifests: manifests,
    latest_accepted_pulse: latest?.pulse ?? null,
    accepted_pulses: acceptedPublications.map((accepted) => accepted.pulse),
    accepted_artifact_manifests: acceptedManifests,
    latest_accepted_artifact_manifests:
      latest?.artifact_manifests.map((manifest) => manifest.url) ?? [],
    accepted_publications: acceptedPublications,
    accepted_publications_sha256: canonicalJsonHash(acceptedPublications),
    bound_pulse: publication.bound_pulse,
    publication_binding_sha256: publication.binding_sha256
  };
}

function committedNoPulsePointer(
  releaseId: string,
  releaseSha256: string,
  acceptedPublications: AcceptedPublication[]
): Record<string, unknown> {
  const latest = acceptedPublications.at(-1);
  return {
    schema_version: 1,
    release_id: releaseId,
    release_path: `data/releases/${releaseId}`,
    release_sha256: releaseSha256,
    status: "processed_no_pulse",
    pulse: null,
    artifact_manifests: [],
    latest_accepted_pulse: latest?.pulse ?? null,
    accepted_pulses: acceptedPublications.map((publication) => publication.pulse),
    accepted_artifact_manifests: [
      ...new Set(
        acceptedPublications.flatMap((publication) =>
          publication.artifact_manifests.map((manifest) => manifest.url)
        )
      )
    ],
    latest_accepted_artifact_manifests:
      latest?.artifact_manifests.map((manifest) => manifest.url) ?? [],
    accepted_publications: acceptedPublications,
    accepted_publications_sha256: canonicalJsonHash(acceptedPublications)
  };
}

afterEach(() => {
  delete process.env.IMF_PULSE_PUBLIC_RELEASE_DIR;
  for (const root of temporaryRoots.splice(0)) rmSync(root, { recursive: true, force: true });
});

describe("authorized build-time content", () => {
  it("reads only the independently sealed public-release view when selected", () => {
    const root = fixtureRoot();
    writePublicReleaseFixture(root);
    write(root, "data/current.json", "PRIVATE_POINTER_MARKER\n");
    write(root, `data/releases/release-${"f".repeat(20)}/claims.jsonl`, "PRIVATE_RELEASE_MARKER\n");
    process.env.IMF_PULSE_PUBLIC_RELEASE_DIR = "public-release";

    const content = serializeBundledResearchContent(selectBundledResearchContent(root));
    expect(content).toContain("PUBLIC_PULSE");
    expect(content).toContain("PUBLIC_CLAIM");
    expect(content).not.toContain("PRIVATE_POINTER_MARKER");
    expect(content).not.toContain("PRIVATE_RELEASE_MARKER");
    expect(selectBundledArtifactAssets(root).map((asset) => asset.url)).toEqual([
      "/artifacts/2026-07-21/figure/data.csv",
      "/artifacts/2026-07-21/figure/manifest.json"
    ]);
  });

  it("rejects public-release hash tampering, extra files, and symlinks", () => {
    const root = fixtureRoot();
    writePublicReleaseFixture(root);
    process.env.IMF_PULSE_PUBLIC_RELEASE_DIR = "public-release";

    write(root, "public-release/knowledge/claims.jsonl", "TAMPERED\n");
    expect(() => selectBundledResearchContent(root)).toThrow(/hash mismatch/i);

    rmSync(join(root, "public-release"), { recursive: true, force: true });
    writePublicReleaseFixture(root);
    write(root, "public-release/pulses/2026-07-22.md", "EXTRA\n");
    expect(() => selectBundledResearchContent(root)).toThrow(/file map/i);

    rmSync(join(root, "public-release"), { recursive: true, force: true });
    writePublicReleaseFixture(root);
    symlinkSync("claims.jsonl", join(root, "public-release", "knowledge", "linked.jsonl"));
    expect(() => selectBundledResearchContent(root)).toThrow(/symlink/i);
  });

  it("rejects an internally hash-consistent but uncleared public artifact", () => {
    const root = fixtureRoot();
    writePublicReleaseFixture(root, { cleared: false });
    process.env.IMF_PULSE_PUBLIC_RELEASE_DIR = "public-release";
    expect(() => selectBundledArtifactAssets(root)).toThrow(/not cleared/i);
  });

  it("rejects absolute home paths even when the public file map is self-consistent", () => {
    const root = fixtureRoot();
    writePublicReleaseFixture(root, { sourceMarker: "/Users/researcher/private/imf" });
    process.env.IMF_PULSE_PUBLIC_RELEASE_DIR = "public-release";
    expect(() => selectBundledResearchContent(root)).toThrow(/absolute home path/i);
  });

  it("does not embed draft pulse bytes in an unpointed preview bundle", () => {
    const root = fixtureRoot();
    write(root, "content/pulses/2026-07-21.md", "---\nstatus: published\n---\nPUBLISHED_MARKER\n");
    write(root, "content/pulses/2026-07-22.md", "---\nstatus: draft\n---\nSECRET_DRAFT_MARKER\n");
    const source = serializeBundledResearchContent(selectBundledResearchContent(root));
    expect(source).toContain("PUBLISHED_MARKER");
    expect(source).not.toContain("SECRET_DRAFT_MARKER");
  });

  it("uses immutable accepted copies and synthesizes final published state for a gate", () => {
    const root = fixtureRoot();
    const { candidate } = acceptedFixture(root);
    write(root, "content/pulses/2026-07-21.md", "---\nstatus: published\n---\nMUTABLE_ROOT_REPLACEMENT\n");
    write(root, `data/releases/release-${"f".repeat(20)}/claims.jsonl`, "UNRELATED_RELEASE\n");

    const bundle = selectBundledResearchContent(root, candidate);
    const source = serializeBundledResearchContent(bundle);
    const pointer = JSON.parse(Object.values(bundle.currentModules)[0]);
    expect(pointer.status).toBe("published");
    expect(pointer.pulse).toBe("content/pulses/2026-07-21.md");
    expect(source).toContain("BOUND_SELECTED_PULSE");
    expect(source).toContain("SELECTED_RELEASE");
    expect(source).not.toContain("MUTABLE_ROOT_REPLACEMENT");
    expect(source).not.toContain("UNRELATED_RELEASE");

    const assets = selectBundledArtifactAssets(root, candidate);
    expect(assets.map((asset) => asset.url)).toEqual([
      "/artifacts/2026-07-21/figure/manifest.json",
      "/artifacts/2026-07-21/figure/data.csv"
    ]);
    expect(assets.find((asset) => asset.url.endsWith("data.csv"))?.source.toString()).toBe(
      "BOUND_ASSET_BYTES\n"
    );
  });

  it("synthesizes a retained final state for a no-pulse gate", () => {
    const root = fixtureRoot();
    const { publication } = acceptedFixture(root);
    const next = writeSealedRelease(
      root,
      "b",
      { "claims.jsonl": "NEXT_RELEASE\n" },
      undefined,
      [publication]
    );
    const bundle = selectBundledResearchContent(root, {
      releaseId: next.releaseId,
      artifactManifests: [],
      acceptedPublications: [publication],
      publicationGate: true,
      checkpointStatus: "processed_no_pulse"
    });
    const pointer = JSON.parse(Object.values(bundle.currentModules)[0]);
    expect(pointer).toMatchObject({
      release_id: next.releaseId,
      status: "processed_no_pulse",
      pulse: null,
      latest_accepted_pulse: publication.pulse,
      artifact_manifests: []
    });
  });

  it("synthesizes unchanged without reselecting retained pulse artifacts", () => {
    const root = fixtureRoot();
    const { publication, candidate } = acceptedFixture(root);
    const bundle = selectBundledResearchContent(root, {
      ...candidate,
      selectedPulse: undefined,
      artifactManifests: [],
      checkpointStatus: "unchanged"
    });
    const pointer = JSON.parse(Object.values(bundle.currentModules)[0]);
    expect(pointer).toMatchObject({
      release_id: publication.release_id,
      status: "unchanged",
      pulse: null,
      artifact_manifests: [],
      latest_accepted_pulse: publication.pulse,
      accepted_pulses: [publication.pulse]
    });
    expect(Object.values(bundle.pulseModules)[0]).toContain("BOUND_SELECTED_PULSE");
  });

  it("uses immutable publication history for an ordinary committed pointer", () => {
    const root = fixtureRoot();
    const { publication, releaseManifestSha256 } = acceptedFixture(root);
    write(root, publication.pulse, "---\nstatus: published\n---\nMUTABLE_POINTER_REPLACEMENT\n");
    write(root, "data/current.json", JSON.stringify(committedPointer(publication, releaseManifestSha256)));

    const source = serializeBundledResearchContent(selectBundledResearchContent(root));
    expect(source).toContain("BOUND_SELECTED_PULSE");
    expect(source).not.toContain("MUTABLE_POINTER_REPLACEMENT");
    expect(selectBundledArtifactAssets(root)[0]?.sourcePath).toContain(
      "/publication/public/artifacts/"
    );
  });

  it("pins one coherent release snapshot across load and generateBundle hooks", () => {
    const root = fixtureRoot();
    const first = acceptedFixture(root);
    const second = acceptedFixture(
      root,
      "b",
      "2026-07-22",
      "RELEASE_B_PULSE",
      [first.publication]
    );
    write(
      root,
      "data/current.json",
      JSON.stringify(committedPointer(first.publication, first.releaseManifestSha256))
    );
    const plugin = researchContentPlugin(root);
    const load = plugin.load as unknown as (id: string) => string | undefined;
    const loaded = load(`\0${VIRTUAL_CONTENT_ID}`);
    expect(loaded).toContain("BOUND_SELECTED_PULSE");
    expect(loaded).not.toContain("RELEASE_B_PULSE");

    write(
      root,
      "data/current.json",
      JSON.stringify(
        committedPointer(
          second.publication,
          second.releaseManifestSha256,
          [first.publication, second.publication]
        )
      )
    );
    const emitted: Array<{ fileName?: string; source?: unknown }> = [];
    const generateBundle = plugin.generateBundle as unknown as (this: {
      emitFile(asset: { fileName?: string; source?: unknown }): string;
    }) => void;
    generateBundle.call({
      emitFile(asset) {
        emitted.push(asset);
        return `asset-${emitted.length}`;
      }
    });

    expect(emitted.map((asset) => asset.fileName)).toEqual([
      "artifacts/2026-07-21/figure/manifest.json",
      "artifacts/2026-07-21/figure/data.csv"
    ]);
    expect(emitted.map((asset) => asset.fileName).join("\n")).not.toContain("2026-07-22");
  });

  it("rejects self-consistent publication-history truncation and reordering", () => {
    const root = fixtureRoot();
    const first = acceptedFixture(root);
    const second = acceptedFixture(
      root,
      "b",
      "2026-07-22",
      "RELEASE_B_PULSE",
      [first.publication]
    );
    const ordered = [first.publication, second.publication];
    write(
      root,
      "data/current.json",
      JSON.stringify(
        committedPointer(second.publication, second.releaseManifestSha256, ordered)
      )
    );
    expect(() => selectBundledResearchContent(root)).not.toThrow();

    write(
      root,
      "data/current.json",
      JSON.stringify(
        committedPointer(
          second.publication,
          second.releaseManifestSha256,
          [second.publication]
        )
      )
    );
    expect(() => selectBundledResearchContent(root)).toThrow(/history is not bound/i);

    const noPulse = writeSealedRelease(
      root,
      "c",
      { "claims.jsonl": "NO_PULSE_RELEASE\n" },
      undefined,
      ordered
    );
    write(
      root,
      "data/current.json",
      JSON.stringify(committedNoPulsePointer(noPulse.releaseId, noPulse.sha256, ordered))
    );
    expect(() => selectBundledResearchContent(root)).not.toThrow();

    write(
      root,
      "data/current.json",
      JSON.stringify(
        committedNoPulsePointer(noPulse.releaseId, noPulse.sha256, [...ordered].reverse())
      )
    );
    expect(() => selectBundledResearchContent(root)).toThrow(/history is not bound/i);
  });

  it("fails closed when a committed pointer summary or release file is tampered", () => {
    const root = fixtureRoot();
    const { publication, releaseManifestSha256 } = acceptedFixture(root);
    const pointer = committedPointer(publication, releaseManifestSha256);
    write(root, "data/current.json", JSON.stringify({ ...pointer, accepted_pulses: [] }));
    expect(() => selectBundledResearchContent(root)).toThrow(/pointer summary/i);

    write(root, "data/current.json", JSON.stringify(pointer));
    write(root, `data/releases/${publication.release_id}/claims.jsonl`, "TAMPERED\n");
    expect(() => selectBundledResearchContent(root)).toThrow(/file hash mismatch/i);
  });

  it("rejects a stale candidate manifest that lacks the current release contract", () => {
    const root = fixtureRoot();
    const inputFingerprint = "c".repeat(64);
    const releaseId = `release-${inputFingerprint.slice(0, 20)}`;
    write(root, `data/releases/${releaseId}/claims.jsonl`, "LEGACY\n");
    write(
      root,
      `data/releases/${releaseId}/release.json`,
      JSON.stringify({
        schema_version: 1,
        release_id: releaseId,
        input_fingerprint: inputFingerprint,
        files: { "claims.jsonl": digest("LEGACY\n") }
      })
    );
    expect(() =>
      selectBundledResearchContent(root, {
        releaseId,
        artifactManifests: [],
        acceptedPublications: [],
        publicationGate: false
      })
    ).toThrow(/missing required manifest fields/i);
  });

  it("keeps a valid non-gated candidate explicitly in preview state", () => {
    const root = fixtureRoot();
    write(root, "content/pulses/2026-07-21.md", "---\nstatus: published\n---\nCANDIDATE_PREVIEW\n");
    const preview = writeSealedRelease(root, "c", { "claims.jsonl": "PREVIEW_RELEASE\n" });
    const bundle = selectBundledResearchContent(root, {
      releaseId: preview.releaseId,
      selectedPulse: "content/pulses/2026-07-21.md",
      artifactManifests: [],
      acceptedPublications: [],
      publicationGate: false
    });
    const pointer = JSON.parse(Object.values(bundle.currentModules)[0]);
    expect(pointer.status).toBe("candidate_selected_pulse");
    expect(Object.values(bundle.pulseModules)[0]).toContain("CANDIDATE_PREVIEW");
  });
});
