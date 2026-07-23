import { createHash } from "node:crypto";
import {
  closeSync,
  constants,
  fstatSync,
  lstatSync,
  openSync,
  readFileSync,
  readdirSync,
  realpathSync
} from "node:fs";
import { basename, dirname, extname, posix, resolve, sep } from "node:path";
import type { Plugin } from "vite";
import { parse as parseYaml } from "yaml";
import {
  canonicalJsonHash,
  validateAcceptedPublications,
  type AcceptedPublication,
  type CandidateReleaseContext
} from "./release-env";

export const VIRTUAL_CONTENT_ID = "virtual:imf-pulse-content";
const RESOLVED_CONTENT_ID = `\0${VIRTUAL_CONTENT_ID}`;
const ACCEPTED_POINTER_STATUSES = new Set([
  "published",
  "processed_no_pulse",
  "unchanged"
]);

type UnknownRecord = Record<string, unknown>;

export interface BundledResearchContent {
  currentModules: Record<string, string>;
  pulseModules: Record<string, string>;
  releaseModules: Record<string, string>;
  curatedModules: Record<string, string>;
}

export interface BundledArtifactAsset {
  url: string;
  sourcePath: string;
  source: Buffer;
}

interface CurrentFile {
  raw?: string;
  value?: UnknownRecord;
}

interface BuildSelection {
  currentRaw?: string;
  current?: UnknownRecord;
  publications: AcceptedPublication[];
  rootPulseReferences: string[];
  releaseId?: string;
  releaseSha256?: string;
  requirePointerDigest: boolean;
  curated: boolean;
}

interface VerifiedRelease {
  root: string;
  files: Map<string, Buffer>;
  manifest: UnknownRecord;
}

interface AuthorizedBuildSnapshot {
  content: BundledResearchContent;
  assets: BundledArtifactAsset[];
}

const PUBLIC_RELEASE_ENV = "IMF_PULSE_PUBLIC_RELEASE_DIR";
const PUBLIC_RELEASE_KIND = "imf-pulse-public-release";
const PUBLIC_RELEASE_MANIFEST_FIELDS = new Set([
  "schema_version",
  "kind",
  "public_release_id",
  "source_release_id",
  "created_at",
  "approval",
  "file_count",
  "content_sha256",
  "files"
]);
const PUBLIC_CURRENT_FIELDS = new Set([
  "schema_version",
  "release_id",
  "updated_at",
  "published_at",
  "last_checked_at",
  "status",
  "pulse",
  "artifact_manifests",
  "latest_accepted_pulse",
  "accepted_pulses",
  "accepted_artifact_manifests",
  "latest_accepted_artifact_manifests"
]);
const PUBLIC_KNOWLEDGE_NAMES = [
  "sources.jsonl",
  "claims.jsonl",
  "methods.jsonl",
  "experiments.jsonl",
  "relationships.jsonl"
] as const;
const PUBLIC_TEXT_EXTENSIONS = new Set([
  ".json",
  ".jsonl",
  ".md",
  ".csv",
  ".svg",
  ".txt",
  ".yaml",
  ".yml"
]);
const PUBLIC_ARTIFACT_EXTENSIONS = new Set([
  ".json",
  ".csv",
  ".svg",
  ".png",
  ".jpg",
  ".jpeg",
  ".webp",
  ".pdf"
]);
const PUBLIC_CLEARED_RIGHTS = new Set([
  "approved",
  "cleared",
  "public_domain",
  "cc_by",
  "cc_by_sa",
  "cc0",
  "not_applicable",
  "project_generated",
  "project_generated_scientific_chart",
  "project_generated_diagram",
  "project_generated_illustration"
]);
const PUBLIC_HOME_PATH_PATTERNS = [
  /\/(?:Users|home)\/[^/\s"']+\//,
  /[A-Za-z]:\\Users\\[^\\\s"']+\\/
];
const PUBLIC_CREDENTIAL_PATTERNS = [
  /-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----/,
  /\bAKIA[0-9A-Z]{16}\b/,
  /\bASIA[0-9A-Z]{16}\b/,
  /\bAIza[0-9A-Za-z_-]{35}\b/,
  /\bgithub_pat_[A-Za-z0-9_]{20,}\b/,
  /\bgh[pousr]_[A-Za-z0-9_]{20,}\b/,
  /\bsk-[A-Za-z0-9_-]{20,}\b/,
  /\bxox[baprs]-[A-Za-z0-9-]{10,}\b/,
  /\b(?:api[_-]?key|client[_-]?secret|access[_-]?token|password|passwd)\s*[:=]\s*["']?(?!none\b|null\b|false\b|true\b|unknown\b|redacted\b|example\b)[^\s"']{8,}/i
];
const PUBLIC_RAW_FIELD_PATTERN =
  /"(?:snapshot_id|snapshot_path|extract_semantic_sha256|processing_fingerprint|quote|excerpt|raw_source|raw_content|source_text|extracted_text)"\s*:/;

function isRecord(value: unknown): value is UnknownRecord {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function exactObjectFields(value: UnknownRecord, expected: Set<string>, label: string): void {
  if (
    Object.keys(value).length !== expected.size ||
    Object.keys(value).some((key) => !expected.has(key))
  ) {
    throw new Error(`${label} has an unexpected field set.`);
  }
}

function safePublicReleaseRoot(projectRoot: string, configured: string): string {
  if (
    !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(configured) ||
    configured === "." ||
    configured === ".."
  ) {
    throw new Error(`${PUBLIC_RELEASE_ENV} must name a direct project-child directory.`);
  }
  const root = realpathSync(projectRoot);
  const requested = resolve(root, configured);
  const stat = lstatSync(requested);
  if (!stat.isDirectory() || stat.isSymbolicLink()) {
    throw new Error(`${PUBLIC_RELEASE_ENV} must name a regular, non-symlink directory.`);
  }
  const real = realpathSync(requested);
  if (dirname(real) !== root || basename(real) !== configured) {
    throw new Error(`${PUBLIC_RELEASE_ENV} escaped the project root.`);
  }
  return real;
}

function isAllowedPublicReleasePath(relative: string): boolean {
  if (relative === "current.json") return true;
  if (PUBLIC_KNOWLEDGE_NAMES.some((name) => relative === `knowledge/${name}`)) return true;
  if (/^pulses\/\d{4}-\d{2}-\d{2}\.md$/.test(relative)) return true;
  return (
    /^artifacts\/\d{4}-\d{2}-\d{2}\/[A-Za-z0-9._-]+(?:\/[A-Za-z0-9._-]+)*$/.test(
      relative
    ) && PUBLIC_ARTIFACT_EXTENSIONS.has(extname(relative).toLowerCase())
  );
}

function scanPublicReleaseText(relative: string, bytes: Buffer): void {
  if (!PUBLIC_TEXT_EXTENSIONS.has(extname(relative).toLowerCase())) return;
  const text = bytes.toString("utf8");
  if (!Buffer.from(text, "utf8").equals(bytes)) {
    throw new Error(`Public release text file is not valid UTF-8: ${relative}.`);
  }
  if (PUBLIC_HOME_PATH_PATTERNS.some((pattern) => pattern.test(text))) {
    throw new Error(`Public release contains an absolute home path: ${relative}.`);
  }
  if (PUBLIC_CREDENTIAL_PATTERNS.some((pattern) => pattern.test(text))) {
    throw new Error(`Public release contains a credential-like value: ${relative}.`);
  }
  if (relative.startsWith("knowledge/") && PUBLIC_RAW_FIELD_PATTERN.test(text)) {
    throw new Error(`Public knowledge contains a private/raw field: ${relative}.`);
  }
}

function publicUrlToRelative(value: unknown, label: string): string {
  if (
    typeof value !== "string" ||
    !value.startsWith("/artifacts/") ||
    value.startsWith("//") ||
    /[\\?&#%\s\u0000-\u001f\u007f]/.test(value)
  ) {
    throw new Error(`${label} is not a safe local artifact URL.`);
  }
  const relative = value.slice(1);
  if (!isAllowedPublicReleasePath(relative)) {
    throw new Error(`${label} is outside the public-release allowlist.`);
  }
  return relative;
}

function resolvePublicArtifactReference(
  value: unknown,
  manifestRelative: string,
  label: string
): string | undefined {
  if (value == null) return undefined;
  if (typeof value !== "string" || !value) throw new Error(`${label} must be a string.`);
  if (value.startsWith("/")) return publicUrlToRelative(value, label);
  const normalized = value.replace(/^\.\//, "");
  if (
    !normalized ||
    normalized.includes("\\") ||
    /[?&#%\s\u0000-\u001f\u007f]/.test(normalized) ||
    normalized.split("/").some((segment) => segment === "" || segment === "." || segment === "..")
  ) {
    throw new Error(`${label} is not a safe relative artifact URL.`);
  }
  const directory = posix.dirname(manifestRelative);
  const relative = posix.join(directory, normalized);
  if (!relative.startsWith(`${directory}/`) || !isAllowedPublicReleasePath(relative)) {
    throw new Error(`${label} escaped its artifact directory.`);
  }
  return relative;
}

function publicArtifactRows(manifest: UnknownRecord, label: string): UnknownRecord[] {
  const candidates = Array.isArray(manifest.artifacts) ? manifest.artifacts : [manifest];
  if (candidates.length === 0 || candidates.some((row) => !isRecord(row))) {
    throw new Error(`${label} contains an invalid artifact row.`);
  }
  return candidates as UnknownRecord[];
}

function validatePublicArtifactRights(row: UnknownRecord, label: string): void {
  if (!isRecord(row.rights) || row.rights.may_publish_publicly !== true) {
    throw new Error(`${label} is not cleared for public deployment.`);
  }
  const status = String(row.rights.status ?? "")
    .trim()
    .toLowerCase()
    .replace(/[\s-]+/g, "_");
  if (!PUBLIC_CLEARED_RIGHTS.has(status)) {
    throw new Error(`${label} has an unsupported public rights status.`);
  }
  if (
    status.startsWith("project_generated") &&
    (row.rights.public_deployment_requires_owner_approval !== false ||
      row.rights.public_deployment_approved_by !== "project_owner" ||
      row.rights.public_deployment_approved_on !== "2026-07-23" ||
      row.rights.public_deployment_approval_scope !==
        "project-generated artifact public deployment")
  ) {
    throw new Error(`${label} is missing the recorded project-owner deployment approval.`);
  }
}

function artifactReferences(manifest: UnknownRecord, manifestRelative: string): Set<string> {
  const references = new Set<string>([manifestRelative]);
  for (const [rowIndex, row] of publicArtifactRows(manifest, manifestRelative).entries()) {
    const label = `${manifestRelative} artifact ${rowIndex + 1}`;
    validatePublicArtifactRights(row, label);
    for (const field of ["stable_url", "spec_url", "data_url"] as const) {
      const relative = resolvePublicArtifactReference(row[field], manifestRelative, `${label}.${field}`);
      if (relative) references.add(relative);
    }
    for (const collectionName of ["files", "downloads"] as const) {
      const collection = row[collectionName];
      if (collection == null) continue;
      if (!Array.isArray(collection) || collection.some((item) => !isRecord(item))) {
        throw new Error(`${label}.${collectionName} is invalid.`);
      }
      for (const [fileIndex, item] of (collection as UnknownRecord[]).entries()) {
        const relative = resolvePublicArtifactReference(
          item.url ?? item.path,
          manifestRelative,
          `${label}.${collectionName}[${fileIndex}]`
        );
        if (relative) references.add(relative);
      }
    }
  }
  return references;
}

function exactStringArray(value: unknown, label: string): string[] {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw new Error(`${label} must be an array of strings.`);
  }
  const strings = value as string[];
  if (new Set(strings).size !== strings.length) throw new Error(`${label} contains duplicates.`);
  return strings;
}

function selectPublicReleaseSnapshot(projectRoot: string, configured: string): AuthorizedBuildSnapshot {
  const root = safePublicReleaseRoot(projectRoot, configured);
  const paths = collectReleaseFiles(root);
  const manifestPath = paths.get("manifest.json");
  if (!manifestPath) throw new Error("Public release is missing manifest.json.");
  let manifest: UnknownRecord;
  try {
    const parsed: unknown = JSON.parse(readStableFile(manifestPath).toString("utf8"));
    if (!isRecord(parsed)) throw new Error();
    manifest = parsed;
  } catch {
    throw new Error("Public release manifest is not valid JSON.");
  }
  exactObjectFields(manifest, PUBLIC_RELEASE_MANIFEST_FIELDS, "Public release manifest");
  if (manifest.schema_version !== 1 || manifest.kind !== PUBLIC_RELEASE_KIND) {
    throw new Error("Public release manifest identity is invalid.");
  }
  const sourceReleaseId = String(manifest.source_release_id ?? "");
  if (!/^release-[a-f0-9]{20}$/.test(sourceReleaseId)) {
    throw new Error("Public release source release id is invalid.");
  }
  if (
    !isRecord(manifest.approval) ||
    JSON.stringify(manifest.approval) !==
      JSON.stringify({
        actor: "project_owner",
        approved_on: "2026-07-23",
        scope: "project-generated artifact public deployment"
      })
  ) {
    throw new Error("Public release approval metadata is invalid.");
  }
  if (!isRecord(manifest.files)) throw new Error("Public release file map is invalid.");
  const listed = manifest.files;
  const actualRelatives = [...paths.keys()].filter((relative) => relative !== "manifest.json");
  const listedRelatives = Object.keys(listed);
  if (
    actualRelatives.length !== listedRelatives.length ||
    actualRelatives.some((relative) => !Object.prototype.hasOwnProperty.call(listed, relative))
  ) {
    throw new Error("Public release file map does not exactly match its tree.");
  }
  const files = new Map<string, Buffer>();
  for (const relative of listedRelatives.sort()) {
    const expected = listed[relative];
    if (
      !isAllowedPublicReleasePath(relative) ||
      typeof expected !== "string" ||
      !/^[a-f0-9]{64}$/.test(expected)
    ) {
      throw new Error(`Public release has an unsafe file record: ${relative}.`);
    }
    const path = paths.get(relative);
    if (!path) throw new Error(`Public release file is missing: ${relative}.`);
    const bytes = readStableFile(path);
    if (sha256Bytes(bytes) !== expected) {
      throw new Error(`Public release file hash mismatch: ${relative}.`);
    }
    scanPublicReleaseText(relative, bytes);
    files.set(relative, bytes);
  }
  const required = [
    "current.json",
    ...PUBLIC_KNOWLEDGE_NAMES.map((name) => `knowledge/${name}`)
  ];
  if (required.some((relative) => !files.has(relative))) {
    throw new Error("Public release is missing its current/knowledge contract.");
  }
  const fileMapHash = canonicalJsonHash(
    Object.fromEntries([...files.keys()].sort().map((relative) => [relative, listed[relative]]))
  );
  if (
    manifest.content_sha256 !== fileMapHash ||
    manifest.public_release_id !== `public-${fileMapHash.slice(0, 20)}` ||
    manifest.file_count !== files.size
  ) {
    throw new Error("Public release aggregate identity is invalid.");
  }

  let current: UnknownRecord;
  try {
    const parsed: unknown = JSON.parse(files.get("current.json")!.toString("utf8"));
    if (!isRecord(parsed)) throw new Error();
    current = parsed;
  } catch {
    throw new Error("Public release current summary is not valid JSON.");
  }
  if (Object.keys(current).some((key) => !PUBLIC_CURRENT_FIELDS.has(key))) {
    throw new Error("Public release current summary contains a private/unexpected field.");
  }
  if (
    current.release_id !== sourceReleaseId ||
    !["published", "processed_no_pulse", "unchanged"].includes(String(current.status ?? ""))
  ) {
    throw new Error("Public release current summary identity/status is invalid.");
  }
  const checkpointTimestamp = [
    current.last_checked_at,
    current.updated_at,
    current.published_at
  ].find(
    (value): value is string =>
      typeof value === "string" &&
      /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$/.test(value)
  );
  if (!checkpointTimestamp || manifest.created_at !== checkpointTimestamp) {
    throw new Error("Public release timestamp is not bound to the committed checkpoint.");
  }
  const acceptedPulses = exactStringArray(current.accepted_pulses, "accepted_pulses");
  if (acceptedPulses.length === 0) throw new Error("Public release has no accepted pulse.");
  const pulseModules: Record<string, string> = {};
  const expectedPulseRelatives = new Set<string>();
  for (const reference of acceptedPulses) {
    const match = reference.match(/^content\/pulses\/(\d{4}-\d{2}-\d{2})\.md$/);
    if (!match) throw new Error(`Public release has an unsafe pulse reference: ${reference}.`);
    const relative = `pulses/${match[1]}.md`;
    const bytes = files.get(relative);
    if (!bytes) throw new Error(`Public release is missing accepted pulse: ${reference}.`);
    expectedPulseRelatives.add(relative);
    pulseModules[`/${reference}`] = bytes.toString("utf8");
  }
  const actualPulseRelatives = new Set(
    [...files.keys()].filter((relative) => relative.startsWith("pulses/"))
  );
  if (
    actualPulseRelatives.size !== expectedPulseRelatives.size ||
    [...actualPulseRelatives].some((relative) => !expectedPulseRelatives.has(relative))
  ) {
    throw new Error("Public release pulse files do not match its accepted history.");
  }

  const acceptedManifests = exactStringArray(
    current.accepted_artifact_manifests,
    "accepted_artifact_manifests"
  );
  const expectedManifestRelatives = new Set(
    acceptedManifests.map((url) => {
      const relative = publicUrlToRelative(url, "accepted artifact manifest");
      if (!relative.endsWith("/manifest.json")) {
        throw new Error("Accepted artifact manifest URL must end in manifest.json.");
      }
      return relative;
    })
  );
  const actualManifestRelatives = new Set(
    [...files.keys()].filter(
      (relative) => relative.startsWith("artifacts/") && relative.endsWith("/manifest.json")
    )
  );
  if (
    actualManifestRelatives.size !== expectedManifestRelatives.size ||
    [...actualManifestRelatives].some((relative) => !expectedManifestRelatives.has(relative))
  ) {
    throw new Error("Public release artifact manifests do not match its accepted history.");
  }
  const referencedArtifacts = new Set<string>();
  for (const relative of [...actualManifestRelatives].sort()) {
    let artifact: UnknownRecord;
    try {
      const parsed: unknown = JSON.parse(files.get(relative)!.toString("utf8"));
      if (!isRecord(parsed)) throw new Error();
      artifact = parsed;
    } catch {
      throw new Error(`Public artifact manifest is not valid JSON: ${relative}.`);
    }
    for (const reference of artifactReferences(artifact, relative)) {
      referencedArtifacts.add(reference);
    }
  }
  const actualArtifacts = new Set(
    [...files.keys()].filter((relative) => relative.startsWith("artifacts/"))
  );
  if (
    actualArtifacts.size !== referencedArtifacts.size ||
    [...actualArtifacts].some((relative) => !referencedArtifacts.has(relative))
  ) {
    throw new Error("Public release contains missing or unreferenced artifact bytes.");
  }

  const releaseModules = Object.fromEntries(
    PUBLIC_KNOWLEDGE_NAMES.map((name) => [
      `/data/releases/${sourceReleaseId}/${name}`,
      files.get(`knowledge/${name}`)!.toString("utf8")
    ])
  );
  const assets = [...actualArtifacts]
    .sort()
    .map((relative) => ({
      url: `/${relative}`,
      sourcePath: paths.get(relative)!,
      source: files.get(relative)!
    }));
  return {
    content: {
      currentModules: { "/data/current.json": files.get("current.json")!.toString("utf8") },
      pulseModules,
      releaseModules,
      curatedModules: {}
    },
    assets
  };
}

function configuredPublicRelease(
  projectRoot: string,
  candidate?: CandidateReleaseContext
): AuthorizedBuildSnapshot | undefined {
  const configured = process.env[PUBLIC_RELEASE_ENV];
  if (configured == null || configured === "") return undefined;
  if (candidate) throw new Error("A public-release build cannot also select a private candidate.");
  return selectPublicReleaseSnapshot(projectRoot, configured);
}

function readStableFile(path: string): Buffer {
  const descriptor = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const before = fstatSync(descriptor, { bigint: true });
    if (!before.isFile()) throw new Error(`Expected a regular file: ${path}`);
    const bytes = readFileSync(descriptor);
    const after = fstatSync(descriptor, { bigint: true });
    if (
      before.dev !== after.dev ||
      before.ino !== after.ino ||
      before.size !== after.size ||
      before.mtimeNs !== after.mtimeNs ||
      before.ctimeNs !== after.ctimeNs ||
      BigInt(bytes.byteLength) !== after.size
    ) {
      throw new Error(`File changed while it was being read: ${path}`);
    }
    return bytes;
  } finally {
    closeSync(descriptor);
  }
}

function readHashBoundFile(projectRoot: string, relative: string, expectedSha256: string): Buffer {
  const bytes = readStableFile(resolve(projectRoot, relative));
  const actual = createHash("sha256").update(bytes).digest("hex");
  if (actual !== expectedSha256) throw new Error(`Bound file changed after validation: ${relative}`);
  return bytes;
}

function uniqueStrings(values: Array<string | null | undefined>): string[] {
  return [...new Set(values.filter((value): value is string => Boolean(value)))];
}

function readCurrent(projectRoot: string): CurrentFile {
  const path = resolve(projectRoot, "data", "current.json");
  try {
    const stat = lstatSync(path);
    if (!stat.isFile() || stat.isSymbolicLink()) {
      throw new Error("data/current.json must be a regular, non-symlink file.");
    }
    const raw = readStableFile(path).toString("utf8");
    const parsed: unknown = JSON.parse(raw);
    if (!isRecord(parsed)) throw new Error("data/current.json must contain a JSON object.");
    return { raw, value: parsed };
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return {};
    if (error instanceof SyntaxError) throw new Error("data/current.json is not valid JSON.");
    throw error;
  }
}

function pointerPublications(
  projectRoot: string,
  pointer: UnknownRecord | undefined
): AcceptedPublication[] {
  if (!pointer || !ACCEPTED_POINTER_STATUSES.has(String(pointer.status ?? ""))) return [];
  if (!Object.prototype.hasOwnProperty.call(pointer, "accepted_publications")) {
    throw new Error("Committed release pointer has no immutable accepted-publication history.");
  }
  const publications = validateAcceptedPublications(projectRoot, pointer.accepted_publications);
  const releaseId = String(pointer.release_id ?? "");
  const status = String(pointer.status ?? "");
  const latest = publications.at(-1);
  const acceptedPulses = publications.map((publication) => publication.pulse);
  const acceptedManifests = publicationManifestUrls(publications);
  const latestManifests = latest?.artifact_manifests.map((manifest) => manifest.url) ?? [];
  const selected = status === "published"
    ? publications.find((publication) => publication.release_id === releaseId)
    : undefined;
  const currentManifests = selected?.artifact_manifests.map((manifest) => manifest.url) ?? [];
  const acceptedPublicationsSha256 = canonicalJsonHash(publications);
  const exactArray = (value: unknown, expected: string[]): boolean =>
    Array.isArray(value) &&
    value.every((item) => typeof item === "string") &&
    JSON.stringify(value) === JSON.stringify(expected);
  if (
    pointer.release_path !== `data/releases/${releaseId}` ||
    pointer.accepted_publications_sha256 !== acceptedPublicationsSha256 ||
    pointer.latest_accepted_pulse !== (latest?.pulse ?? null) ||
    !exactArray(pointer.accepted_pulses, acceptedPulses) ||
    !exactArray(pointer.accepted_artifact_manifests, acceptedManifests) ||
    !exactArray(pointer.latest_accepted_artifact_manifests, latestManifests) ||
    (status === "published" &&
      (!selected ||
        selected !== latest ||
        pointer.pulse !== selected.pulse ||
        pointer.bound_pulse !== selected.bound_pulse ||
        pointer.publication_binding_sha256 !== selected.binding_sha256 ||
        !exactArray(pointer.artifact_manifests, currentManifests))) ||
    (status !== "published" &&
      (pointer.pulse !== null || !exactArray(pointer.artifact_manifests, [])))
  ) {
    throw new Error("Committed pointer summary does not match accepted publication history.");
  }
  return publications;
}

function publicationManifestUrls(publications: AcceptedPublication[]): string[] {
  return uniqueStrings(
    publications.flatMap((publication) =>
      publication.artifact_manifests.map((manifest) => manifest.url)
    )
  );
}

function anticipatedPointer(candidate: CandidateReleaseContext): UnknownRecord {
  const publications = candidate.acceptedPublications;
  const candidatePublication = publications.find(
    (publication) => publication.release_id === candidate.releaseId
  );
  const checkpointStatus = candidate.checkpointStatus;
  if (!checkpointStatus) {
    throw new Error("A publication-gate build requires an explicit checkpoint status.");
  }
  if (checkpointStatus === "published" && !candidatePublication) {
    throw new Error("A published checkpoint has no accepted candidate publication.");
  }
  const latest = publications.at(-1);
  const publishesPulse = checkpointStatus === "published";
  return {
    schema_version: 1,
    release_id: candidate.releaseId,
    release_path: `data/releases/${candidate.releaseId}`,
    status: checkpointStatus,
    pulse: publishesPulse ? candidatePublication?.pulse ?? null : null,
    artifact_manifests:
      publishesPulse
        ? candidatePublication?.artifact_manifests.map((manifest) => manifest.url) ?? []
        : [],
    latest_accepted_pulse: latest?.pulse ?? null,
    accepted_pulses: publications.map((publication) => publication.pulse),
    accepted_artifact_manifests: publicationManifestUrls(publications),
    latest_accepted_artifact_manifests:
      latest?.artifact_manifests.map((manifest) => manifest.url) ?? [],
    accepted_publications: publications,
    accepted_publications_sha256: canonicalJsonHash(publications),
    ...(publishesPulse && candidatePublication
      ? {
          bound_pulse: candidatePublication.bound_pulse,
          publication_binding_sha256: candidatePublication.binding_sha256
        }
      : {})
  };
}

function previewCandidatePointer(
  candidate: CandidateReleaseContext,
  retained: AcceptedPublication[]
): UnknownRecord {
  const retainedLatest = retained.at(-1);
  return {
    schema_version: 1,
    release_id: candidate.releaseId,
    release_path: `data/releases/${candidate.releaseId}`,
    status: candidate.selectedPulse ? "candidate_selected_pulse" : "candidate_no_pulse",
    pulse: candidate.selectedPulse ?? null,
    artifact_manifests: candidate.artifactManifests,
    latest_accepted_pulse: candidate.selectedPulse ?? retainedLatest?.pulse ?? null,
    accepted_pulses: uniqueStrings([
      ...retained.map((publication) => publication.pulse),
      candidate.selectedPulse
    ]),
    accepted_artifact_manifests: uniqueStrings([
      ...publicationManifestUrls(retained),
      ...candidate.artifactManifests
    ]),
    latest_accepted_artifact_manifests:
      candidate.selectedPulse ? candidate.artifactManifests : retainedLatest?.artifact_manifests.map(
        (manifest) => manifest.url
      ) ?? [],
    accepted_publications: retained
  };
}

function buildSelection(
  projectRoot: string,
  candidate?: CandidateReleaseContext
): BuildSelection {
  const current = readCurrent(projectRoot);
  if (
    current.value &&
    !ACCEPTED_POINTER_STATUSES.has(String(current.value.status ?? ""))
  ) {
    throw new Error("data/current.json has an unsupported committed status.");
  }
  const retained = pointerPublications(projectRoot, current.value);
  if (candidate?.publicationGate) {
    const publications = validateAcceptedPublications(
      projectRoot,
      candidate.acceptedPublications
    );
    const pointer = anticipatedPointer({ ...candidate, acceptedPublications: publications });
    return {
      currentRaw: JSON.stringify(pointer),
      current: pointer,
      publications,
      rootPulseReferences: [],
      releaseId: candidate.releaseId,
      requirePointerDigest: false,
      curated: false
    };
  }
  if (candidate) {
    const pointer = previewCandidatePointer(candidate, retained);
    return {
      currentRaw: JSON.stringify(pointer),
      current: pointer,
      publications: retained,
      rootPulseReferences: candidate.selectedPulse ? [candidate.selectedPulse] : [],
      releaseId: candidate.releaseId,
      requirePointerDigest: false,
      curated: false
    };
  }
  if (current.value) {
    return {
      currentRaw: current.raw,
      current: current.value,
      publications: retained,
      rootPulseReferences: [],
      releaseId:
        ACCEPTED_POINTER_STATUSES.has(String(current.value.status ?? "")) &&
        typeof current.value.release_id === "string"
          ? current.value.release_id
          : undefined,
      releaseSha256:
        typeof current.value.release_sha256 === "string"
          ? current.value.release_sha256
          : undefined,
      requirePointerDigest: ACCEPTED_POINTER_STATUSES.has(String(current.value.status ?? "")),
      curated: false
    };
  }
  return {
    publications: [],
    rootPulseReferences: previewPulseReferences(projectRoot),
    requirePointerDigest: false,
    curated: true
  };
}

function safeRootPulsePath(projectRoot: string, reference: string): string {
  if (
    !/^content\/pulses\/\d{4}-\d{2}-\d{2}\.md$/.test(reference) ||
    reference.includes("\\") ||
    /[\u0000-\u001f\u007f]/.test(reference)
  ) {
    throw new Error(`Unsafe preview pulse path: ${reference}`);
  }
  const rootPath = resolve(projectRoot, "content", "pulses");
  const rootStat = lstatSync(rootPath);
  if (!rootStat.isDirectory() || rootStat.isSymbolicLink()) {
    throw new Error("content/pulses must be a regular directory.");
  }
  const pulseRoot = realpathSync(rootPath);
  const requested = resolve(projectRoot, reference);
  const stat = lstatSync(requested);
  if (!stat.isFile() || stat.isSymbolicLink()) {
    throw new Error(`Preview pulse is not a regular file: ${reference}`);
  }
  const real = realpathSync(requested);
  if (dirname(real) !== pulseRoot) throw new Error(`Preview pulse escaped content/pulses: ${reference}`);
  return real;
}

function hasPublishedFrontmatter(raw: string): boolean {
  const match = raw.replace(/^\uFEFF/, "").match(/^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/);
  if (!match) return false;
  try {
    const parsed: unknown = parseYaml(match[1], { maxAliasCount: 20 });
    return isRecord(parsed) && parsed.status === "published";
  } catch {
    return false;
  }
}

function previewPulseReferences(projectRoot: string): string[] {
  const root = resolve(projectRoot, "content", "pulses");
  try {
    const stat = lstatSync(root);
    if (!stat.isDirectory() || stat.isSymbolicLink()) {
      throw new Error("content/pulses must be a regular directory.");
    }
    return readdirSync(root, { withFileTypes: true })
      .filter((entry) => entry.isFile() && /^\d{4}-\d{2}-\d{2}\.md$/.test(entry.name))
      .map((entry) => `content/pulses/${entry.name}`)
      .filter((reference) =>
        hasPublishedFrontmatter(
          readStableFile(safeRootPulsePath(projectRoot, reference)).toString("utf8")
        )
      );
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return [];
    throw error;
  }
}

function selectedPulseModules(
  projectRoot: string,
  selection: BuildSelection
): Record<string, string> {
  const modules: Record<string, string> = {};
  for (const publication of selection.publications) {
    modules[`/${publication.pulse}`] = readHashBoundFile(
      projectRoot,
      publication.bound_pulse,
      publication.pulse_sha256
    ).toString("utf8");
  }
  for (const reference of selection.rootPulseReferences) {
    if (modules[`/${reference}`] == null) {
      modules[`/${reference}`] = readStableFile(
        safeRootPulsePath(projectRoot, reference)
      ).toString("utf8");
    }
  }
  return modules;
}

function safeReleaseRoot(projectRoot: string, releaseId: string): string {
  if (!/^release-[a-f0-9]{20}$/.test(releaseId)) {
    throw new Error(`Unsafe release id: ${releaseId}`);
  }
  const realProjectRoot = realpathSync(projectRoot);
  const dataPath = resolve(realProjectRoot, "data");
  const releasesPath = resolve(dataPath, "releases");
  const dataStat = lstatSync(dataPath);
  const releasesStat = lstatSync(releasesPath);
  if (
    !dataStat.isDirectory() ||
    dataStat.isSymbolicLink() ||
    !releasesStat.isDirectory() ||
    releasesStat.isSymbolicLink()
  ) {
    throw new Error("data/releases must be a regular project-owned directory.");
  }
  const realData = realpathSync(dataPath);
  const releasesRoot = realpathSync(releasesPath);
  if (dirname(realData) !== realProjectRoot || dirname(releasesRoot) !== realData) {
    throw new Error("data/releases escaped the project root.");
  }
  const requested = resolve(releasesRoot, releaseId);
  const stat = lstatSync(requested);
  if (!stat.isDirectory() || stat.isSymbolicLink()) throw new Error(`Invalid release: ${releaseId}`);
  const real = realpathSync(requested);
  if (dirname(real) !== releasesRoot || basename(real) !== releaseId) {
    throw new Error(`Release escaped data/releases: ${releaseId}`);
  }
  return real;
}

function sha256Bytes(bytes: Buffer): string {
  return createHash("sha256").update(bytes).digest("hex");
}

function safeReleaseFileName(value: string): boolean {
  return (
    value.length > 0 &&
    !value.startsWith("/") &&
    !value.includes("\\") &&
    !/[\u0000-\u001f\u007f]/.test(value) &&
    !value.split("/").some((segment) => segment === "" || segment === "." || segment === "..")
  );
}

function collectReleaseFiles(root: string, directory = root): Map<string, string> {
  const files = new Map<string, string>();
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const path = resolve(directory, entry.name);
    const stat = lstatSync(path);
    if (stat.isSymbolicLink()) throw new Error(`Release contains a symlink: ${path}`);
    if (stat.isDirectory()) {
      for (const [relative, child] of collectReleaseFiles(root, path)) files.set(relative, child);
      continue;
    }
    if (!stat.isFile()) throw new Error(`Release contains a non-regular entry: ${path}`);
    const real = realpathSync(path);
    if (!real.startsWith(`${root}${sep}`)) throw new Error(`Release file escaped: ${path}`);
    const relative = real.slice(root.length + 1).split(sep).join("/");
    files.set(relative, real);
  }
  return files;
}

function releaseManifestObject(root: string): UnknownRecord {
  const path = resolve(root, "release.json");
  const stat = lstatSync(path);
  if (!stat.isFile() || stat.isSymbolicLink()) {
    throw new Error("Release manifest must be a regular, non-symlink file.");
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(readStableFile(path).toString("utf8"));
  } catch {
    throw new Error("Release manifest is not valid JSON.");
  }
  if (!isRecord(parsed)) throw new Error("Release manifest must contain an object.");
  return parsed;
}

function verifyReleaseManifest(
  projectRoot: string,
  releaseId: string,
  expectedPointerSha256?: string,
  requirePointerDigest = false
): VerifiedRelease {
  const root = safeReleaseRoot(projectRoot, releaseId);
  const manifest = releaseManifestObject(root);
  const allowedFields = new Set([
    "schema_version",
    "release_id",
    "created_at",
    "status",
    "snapshot_id",
    "config_sha256",
    "input_fingerprint",
    "semantic_fingerprint",
    "runtime",
    "publication",
    "accepted_publications_sha256",
    "previous_release_id",
    "run_id",
    "files",
    "warnings",
    "counts",
    "validation",
    "gates",
    "latest_pulse_id"
  ]);
  if (Object.keys(manifest).some((key) => !allowedFields.has(key))) {
    throw new Error(`Release ${releaseId} has fields outside the release schema.`);
  }
  const requiredFields = [
    "schema_version",
    "release_id",
    "created_at",
    "status",
    "snapshot_id",
    "config_sha256",
    "input_fingerprint",
    "semantic_fingerprint",
    "runtime",
    "files",
    "warnings"
  ];
  if (requiredFields.some((key) => !Object.prototype.hasOwnProperty.call(manifest, key))) {
    throw new Error(`Release ${releaseId} is missing required manifest fields.`);
  }
  const schemaVersion = manifest.schema_version;
  if (
    !(
      (typeof schemaVersion === "number" && Number.isSafeInteger(schemaVersion) && schemaVersion >= 1) ||
      (typeof schemaVersion === "string" && /^\d+\.\d+(?:\.\d+)?$/.test(schemaVersion))
    )
  ) {
    throw new Error(`Release ${releaseId} has an invalid schema version.`);
  }
  const inputFingerprint = String(manifest.input_fingerprint ?? "");
  if (
    manifest.release_id !== releaseId ||
    !/^[a-f0-9]{64}$/.test(inputFingerprint) ||
    releaseId !== `release-${inputFingerprint.slice(0, 20)}` ||
    typeof manifest.semantic_fingerprint !== "string" ||
    !/^[a-f0-9]{64}$/.test(manifest.semantic_fingerprint) ||
    typeof manifest.config_sha256 !== "string" ||
    !/^[a-f0-9]{64}$/.test(manifest.config_sha256) ||
    typeof manifest.created_at !== "string" ||
    !manifest.created_at ||
    typeof manifest.snapshot_id !== "string" ||
    !manifest.snapshot_id ||
    !["candidate", "published"].includes(String(manifest.status ?? "")) ||
    !isRecord(manifest.runtime) ||
    Object.values(manifest.runtime).some((value) => typeof value !== "string" || !value) ||
    !isRecord(manifest.warnings)
  ) {
    throw new Error(`Release ${releaseId} failed its required identity and metadata contract.`);
  }
  const pointerDigest = canonicalJsonHash(manifest);
  if (requirePointerDigest && !expectedPointerSha256) {
    throw new Error(`Committed release pointer ${releaseId} has no release_sha256.`);
  }
  if (
    expectedPointerSha256 &&
    (!/^[a-f0-9]{64}$/.test(expectedPointerSha256) || pointerDigest !== expectedPointerSha256)
  ) {
    throw new Error(`Committed release pointer digest does not match ${releaseId}.`);
  }
  if (!isRecord(manifest.files) || Object.keys(manifest.files).length === 0) {
    throw new Error(`Release ${releaseId} has no sealed file map.`);
  }
  const actualFiles = collectReleaseFiles(root);
  actualFiles.delete("release.json");
  const expectedFiles = new Map<string, string>();
  const verifiedFiles = new Map<string, Buffer>();
  for (const [relative, expectedHash] of Object.entries(manifest.files)) {
    if (
      !safeReleaseFileName(relative) ||
      relative === "release.json" ||
      typeof expectedHash !== "string" ||
      !/^[a-f0-9]{64}$/.test(expectedHash)
    ) {
      throw new Error(`Release ${releaseId} has an unsafe sealed file record.`);
    }
    const path = actualFiles.get(relative);
    const bytes = path ? readStableFile(path) : undefined;
    if (!bytes || sha256Bytes(bytes) !== expectedHash) {
      throw new Error(`Release ${releaseId} file hash mismatch: ${relative}.`);
    }
    expectedFiles.set(relative, expectedHash);
    verifiedFiles.set(relative, bytes);
  }
  if (
    actualFiles.size !== expectedFiles.size ||
    [...actualFiles.keys()].some((relative) => !expectedFiles.has(relative))
  ) {
    throw new Error(`Release ${releaseId} contains files outside its sealed file map.`);
  }
  const hasPublicationFiles = [...expectedFiles.keys()].some((relative) =>
    relative.startsWith("publication/")
  );
  if ((manifest.publication == null) !== !hasPublicationFiles) {
    throw new Error(`Release ${releaseId} publication metadata and files disagree.`);
  }
  if (manifest.publication != null && !expectedFiles.has("publication/binding.json")) {
    throw new Error(`Release ${releaseId} publication binding is not sealed.`);
  }
  return { root, files: verifiedFiles, manifest };
}

function verifySelectionReleases(
  projectRoot: string,
  selection: BuildSelection
): Map<string, VerifiedRelease> {
  const releaseIds = new Set(selection.publications.map((publication) => publication.release_id));
  if (selection.releaseId) releaseIds.add(selection.releaseId);
  const roots = new Map<string, VerifiedRelease>();
  for (const releaseId of releaseIds) {
    roots.set(
      releaseId,
      verifyReleaseManifest(
        projectRoot,
        releaseId,
        releaseId === selection.releaseId ? selection.releaseSha256 : undefined,
        releaseId === selection.releaseId && selection.requirePointerDigest
      )
    );
  }
  if (
    selection.current &&
    ACCEPTED_POINTER_STATUSES.has(String(selection.current.status ?? ""))
  ) {
    const expectedHistorySha256 = canonicalJsonHash(selection.publications);
    const currentRelease = selection.releaseId
      ? roots.get(selection.releaseId)
      : undefined;
    if (
      selection.current.accepted_publications_sha256 !== expectedHistorySha256 ||
      currentRelease?.manifest.accepted_publications_sha256 !== expectedHistorySha256
    ) {
      throw new Error("Accepted publication history is not bound to the current release.");
    }
  }
  return roots;
}

function selectedReleaseModules(
  releaseId: string | undefined,
  verifiedRelease: VerifiedRelease | undefined
): Record<string, string> {
  if (!releaseId) return {};
  if (!verifiedRelease) throw new Error(`Release ${releaseId} was not verified before bundling.`);
  const allowlist = [
    "sources.jsonl",
    "claims.jsonl",
    "methods.jsonl",
    "experiments.jsonl",
    "relationships.jsonl",
    "knowledge/sources.jsonl",
    "knowledge/claims.jsonl",
    "knowledge/methods.jsonl",
    "knowledge/experiments.jsonl",
    "knowledge/relationships.jsonl"
  ];
  const modules: Record<string, string> = {};
  for (const relative of allowlist) {
    const bytes = verifiedRelease.files.get(relative);
    if (bytes) modules[`/data/releases/${releaseId}/${relative}`] = bytes.toString("utf8");
  }
  return modules;
}

function curatedModules(projectRoot: string, enabled: boolean): Record<string, string> {
  if (!enabled) return {};
  const rootPath = resolve(projectRoot, "knowledge", "curated");
  try {
    const rootStat = lstatSync(rootPath);
    if (!rootStat.isDirectory() || rootStat.isSymbolicLink()) {
      throw new Error("knowledge/curated must be a regular directory.");
    }
    const root = realpathSync(rootPath);
    return Object.fromEntries(
      readdirSync(root, { withFileTypes: true })
        .filter((entry) => entry.isFile() && entry.name.endsWith(".jsonl"))
        .map((entry) => {
          const path = resolve(root, entry.name);
          const stat = lstatSync(path);
          if (!stat.isFile() || stat.isSymbolicLink()) {
            throw new Error(`Curated file is not a regular file: ${entry.name}`);
          }
          const real = realpathSync(path);
          if (dirname(real) !== root) throw new Error(`Curated file escaped: ${entry.name}`);
          return [`/knowledge/curated/${entry.name}`, readStableFile(real).toString("utf8")];
        })
    );
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return {};
    throw error;
  }
}

function bundledResearchContentFromSelection(
  projectRoot: string,
  selection: BuildSelection,
  verifiedRoots: Map<string, VerifiedRelease>
): BundledResearchContent {
  return {
    currentModules: selection.currentRaw
      ? { "/data/current.json": selection.currentRaw }
      : {},
    pulseModules: selectedPulseModules(projectRoot, selection),
    releaseModules: selectedReleaseModules(
      selection.releaseId,
      selection.releaseId ? verifiedRoots.get(selection.releaseId) : undefined
    ),
    curatedModules: curatedModules(projectRoot, selection.curated)
  };
}

export function selectBundledResearchContent(
  projectRoot: string,
  candidate?: CandidateReleaseContext
): BundledResearchContent {
  const publicRelease = configuredPublicRelease(projectRoot, candidate);
  if (publicRelease) return publicRelease.content;
  const selection = buildSelection(projectRoot, candidate);
  return bundledResearchContentFromSelection(
    projectRoot,
    selection,
    verifySelectionReleases(projectRoot, selection)
  );
}

function isSafeArtifactUrl(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.startsWith("/artifacts/") &&
    !value.startsWith("//") &&
    !/[\s?&#%]/.test(value) &&
    !value.includes("\\") &&
    !/[\u0000-\u001f\u007f]/.test(value) &&
    !value.slice(1).split("/").some((segment) => segment === "" || segment === "." || segment === "..")
  );
}

function safePreviewAssetPath(projectRoot: string, url: string): string {
  if (!isSafeArtifactUrl(url)) throw new Error(`Unsafe preview artifact URL: ${url}`);
  const publicPath = resolve(projectRoot, "public");
  const publicStat = lstatSync(publicPath);
  if (!publicStat.isDirectory() || publicStat.isSymbolicLink()) {
    throw new Error("public must be a regular directory.");
  }
  const publicRoot = realpathSync(publicPath);
  const requested = resolve(publicRoot, url.slice(1));
  const stat = lstatSync(requested);
  if (!stat.isFile() || stat.isSymbolicLink()) {
    throw new Error(`Preview artifact is not a regular file: ${url}`);
  }
  const real = realpathSync(requested);
  if (!real.startsWith(`${publicRoot}${sep}`)) throw new Error(`Preview artifact escaped public: ${url}`);
  return real;
}

function pulseManifestUrls(raw: string): string[] {
  const match = raw.replace(/^\uFEFF/, "").match(/^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/);
  if (!match) return [];
  try {
    const parsed: unknown = parseYaml(match[1], { maxAliasCount: 20 });
    if (!isRecord(parsed)) return [];
    const configured = parsed.artifact_manifests ?? parsed.artifactManifests ?? parsed.artifact_manifest;
    const values = Array.isArray(configured) ? configured : configured == null ? [] : [configured];
    return uniqueStrings(values.map((value) => typeof value === "string" ? value : undefined))
      .filter(isSafeArtifactUrl);
  } catch {
    return [];
  }
}

function resolveManifestAssetUrl(value: unknown, manifestUrl: string): string | undefined {
  if (typeof value !== "string" || !value) return undefined;
  const directory = manifestUrl.slice(0, manifestUrl.lastIndexOf("/") + 1);
  const resolved = value.startsWith("/") ? value : `${directory}${value.replace(/^\.\//, "")}`;
  return isSafeArtifactUrl(resolved) && resolved.startsWith(directory) ? resolved : undefined;
}

function previewManifestFileUrls(value: unknown, manifestUrl: string): string[] {
  if (!isRecord(value)) return [];
  const rows = Array.isArray(value.artifacts)
    ? value.artifacts.filter(isRecord)
    : [value];
  const urls: string[] = [];
  for (const row of rows) {
    for (const direct of [row.stable_url, row.spec_url, row.data_url]) {
      const url = resolveManifestAssetUrl(direct, manifestUrl);
      if (url) urls.push(url);
    }
    for (const collection of [row.files, row.downloads]) {
      if (!Array.isArray(collection)) continue;
      for (const file of collection) {
        if (!isRecord(file)) continue;
        const url = resolveManifestAssetUrl(file.url ?? file.path, manifestUrl);
        if (url) urls.push(url);
      }
    }
  }
  return uniqueStrings(urls);
}

function addAsset(
  assets: Map<string, BundledArtifactAsset>,
  url: string,
  sourcePath: string,
  expectedSha256?: string
): void {
  const source = readStableFile(sourcePath);
  if (expectedSha256 && sha256Bytes(source) !== expectedSha256) {
    throw new Error(`Bound artifact changed after validation: ${url}.`);
  }
  const existing = assets.get(url);
  if (existing && !existing.source.equals(source)) {
    throw new Error(`Two accepted artifact bindings claim different bytes for ${url}.`);
  }
  if (!existing) assets.set(url, { url, sourcePath, source });
}

function bundledArtifactAssetsFromSelection(
  projectRoot: string,
  selection: BuildSelection
): BundledArtifactAsset[] {
  const assets = new Map<string, BundledArtifactAsset>();
  for (const publication of selection.publications) {
    for (const manifest of publication.artifact_manifests) {
      addAsset(
        assets,
        manifest.url,
        resolve(projectRoot, manifest.bound_path),
        manifest.sha256
      );
      for (const file of manifest.files) {
        addAsset(assets, file.url, resolve(projectRoot, file.bound_path), file.sha256);
      }
    }
  }
  for (const reference of selection.rootPulseReferences) {
    const raw = readStableFile(safeRootPulsePath(projectRoot, reference)).toString("utf8");
    for (const manifestUrl of pulseManifestUrls(raw)) {
      const manifestPath = safePreviewAssetPath(projectRoot, manifestUrl);
      const manifestRaw = readStableFile(manifestPath).toString("utf8");
      let manifest: unknown;
      try {
        manifest = JSON.parse(manifestRaw);
      } catch {
        throw new Error(`Preview artifact manifest is invalid JSON: ${manifestUrl}`);
      }
      addAsset(assets, manifestUrl, manifestPath);
      for (const fileUrl of previewManifestFileUrls(manifest, manifestUrl)) {
        addAsset(assets, fileUrl, safePreviewAssetPath(projectRoot, fileUrl));
      }
    }
  }
  return [...assets.values()];
}

export function selectBundledArtifactAssets(
  projectRoot: string,
  candidate?: CandidateReleaseContext
): BundledArtifactAsset[] {
  const publicRelease = configuredPublicRelease(projectRoot, candidate);
  if (publicRelease) return publicRelease.assets;
  const selection = buildSelection(projectRoot, candidate);
  verifySelectionReleases(projectRoot, selection);
  return bundledArtifactAssetsFromSelection(projectRoot, selection);
}

function selectAuthorizedBuildSnapshot(
  projectRoot: string,
  candidate?: CandidateReleaseContext
): AuthorizedBuildSnapshot {
  const publicRelease = configuredPublicRelease(projectRoot, candidate);
  if (publicRelease) return publicRelease;
  const selection = buildSelection(projectRoot, candidate);
  const verifiedRoots = verifySelectionReleases(projectRoot, selection);
  return {
    content: bundledResearchContentFromSelection(projectRoot, selection, verifiedRoots),
    assets: bundledArtifactAssetsFromSelection(projectRoot, selection)
  };
}

export function serializeBundledResearchContent(bundle: BundledResearchContent): string {
  return [
    `export const currentModules = ${JSON.stringify(bundle.currentModules)};`,
    `export const pulseModules = ${JSON.stringify(bundle.pulseModules)};`,
    `export const releaseModules = ${JSON.stringify(bundle.releaseModules)};`,
    `export const curatedModules = ${JSON.stringify(bundle.curatedModules)};`
  ].join("\n");
}

function mediaType(url: string): string {
  const extension = extname(url).toLowerCase();
  return ({
    ".csv": "text/csv; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".pdf": "application/pdf"
  } as Record<string, string>)[extension] ?? "application/octet-stream";
}

export function researchContentPlugin(
  projectRoot: string,
  candidate?: CandidateReleaseContext
): Plugin {
  const snapshot = selectAuthorizedBuildSnapshot(projectRoot, candidate);
  return {
    name: "imf-pulse-authorized-content",
    enforce: "pre",
    resolveId(id) {
      return id === VIRTUAL_CONTENT_ID ? RESOLVED_CONTENT_ID : undefined;
    },
    load(id) {
      if (id !== RESOLVED_CONTENT_ID) return undefined;
      return serializeBundledResearchContent(snapshot.content);
    },
    configureServer(server) {
      const assets = new Map(snapshot.assets.map((asset) => [asset.url, asset]));
      server.middlewares.use((request, response, next) => {
        const url = request.url?.split("?", 1)[0];
        const asset = url ? assets.get(url) : undefined;
        if (!asset) {
          next();
          return;
        }
        response.statusCode = 200;
        response.setHeader("Content-Type", mediaType(asset.url));
        response.setHeader("X-Content-Type-Options", "nosniff");
        response.end(asset.source);
      });
    },
    generateBundle() {
      for (const asset of snapshot.assets) {
        this.emitFile({
          type: "asset",
          fileName: asset.url.slice(1),
          source: asset.source
        });
      }
    }
  };
}
