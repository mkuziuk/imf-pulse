import { createHash } from "node:crypto";
import {
  closeSync,
  constants,
  fstatSync,
  lstatSync,
  openSync,
  readFileSync,
  realpathSync
} from "node:fs";
import { basename, dirname, resolve, sep } from "node:path";

export interface AcceptedArtifactFile {
  url: string;
  bound_path: string;
  sha256: string;
  bytes: number;
}

export interface AcceptedArtifactManifest {
  url: string;
  bound_path: string;
  sha256: string;
  files: AcceptedArtifactFile[];
}

export interface AcceptedPublication {
  release_id: string;
  pulse: string;
  bound_pulse: string;
  pulse_sha256: string;
  binding_sha256: string;
  artifact_manifests: AcceptedArtifactManifest[];
}

export interface CandidateReleaseContext {
  releaseId: string;
  selectedPulse?: string;
  artifactManifests: string[];
  acceptedPublications: AcceptedPublication[];
  publicationGate: boolean;
  checkpointStatus?: CheckpointStatus;
}

export type CheckpointStatus = "published" | "processed_no_pulse" | "unchanged";

const CHECKPOINT_STATUSES = new Set<CheckpointStatus>([
  "published",
  "processed_no_pulse",
  "unchanged"
]);

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

function sha256(path: string): string {
  return createHash("sha256").update(readStableFile(path)).digest("hex");
}

function canonicalJson(value: unknown): string {
  if (value === null || typeof value === "boolean" || typeof value === "string") {
    return JSON.stringify(value);
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new Error("Canonical JSON cannot contain non-finite numbers.");
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`)
      .join(",")}}`;
  }
  throw new Error("Canonical JSON contains an unsupported value.");
}

export function canonicalJsonHash(value: unknown): string {
  return createHash("sha256").update(canonicalJson(value)).digest("hex");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function releaseIdIsSafe(value: string): boolean {
  return /^release-[a-f0-9]{20}$/.test(value);
}

function safeReleasesRoot(projectRoot: string): string {
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
  const realReleases = realpathSync(releasesPath);
  if (dirname(realData) !== realProjectRoot || dirname(realReleases) !== realData) {
    throw new Error("data/releases escaped the project root.");
  }
  return realReleases;
}

function safeRelativePath(value: unknown, label: string): string {
  if (
    typeof value !== "string" ||
    !value ||
    value.startsWith("/") ||
    value.includes("\\") ||
    /[\u0000-\u001f\u007f]/.test(value) ||
    value.split("/").some((segment) => segment === "" || segment === ".." || segment === ".")
  ) {
    throw new Error(`${label} must be a safe project-relative path.`);
  }
  return value;
}

function safePublicUrl(value: unknown, label: string): string {
  if (
    typeof value !== "string" ||
    !value.startsWith("/artifacts/") ||
    value.startsWith("//") ||
    /[\s?&#%]/.test(value) ||
    value.includes("\\") ||
    /[\u0000-\u001f\u007f]/.test(value) ||
    value.slice(1).split("/").some((segment) => segment === "" || segment === ".." || segment === ".")
  ) {
    throw new Error(`${label} contains an unsafe artifact URL.`);
  }
  return value;
}

function safeBoundFile(
  projectRoot: string,
  releaseId: string,
  value: unknown,
  expectedSha256: unknown,
  label: string
): string {
  const relative = safeRelativePath(value, label);
  const prefix = `data/releases/${releaseId}/publication/`;
  if (!relative.startsWith(prefix)) throw new Error(`${label} is outside its release binding.`);
  const releasesRoot = safeReleasesRoot(projectRoot);
  const releaseRoot = resolve(releasesRoot, releaseId);
  const releaseStat = lstatSync(releaseRoot);
  if (!releaseStat.isDirectory() || releaseStat.isSymbolicLink()) {
    throw new Error(`${label} names an invalid release binding.`);
  }
  const realReleaseRoot = realpathSync(releaseRoot);
  if (dirname(realReleaseRoot) !== releasesRoot || basename(realReleaseRoot) !== releaseId) {
    throw new Error(`${label} escaped data/releases.`);
  }
  const publicationPath = resolve(realReleaseRoot, "publication");
  const publicationStat = lstatSync(publicationPath);
  if (!publicationStat.isDirectory() || publicationStat.isSymbolicLink()) {
    throw new Error(`${label} names an invalid publication directory.`);
  }
  const publicationRoot = realpathSync(publicationPath);
  if (dirname(publicationRoot) !== realReleaseRoot) {
    throw new Error(`${label} escaped its release publication directory.`);
  }
  const requested = resolve(projectRoot, relative);
  const stat = lstatSync(requested);
  if (!stat.isFile() || stat.isSymbolicLink()) {
    throw new Error(`${label} must name a regular, non-symlink bound file.`);
  }
  const real = realpathSync(requested);
  if (!real.startsWith(`${publicationRoot}${sep}`)) throw new Error(`${label} escaped publication.`);
  if (
    typeof expectedSha256 !== "string" ||
    !/^[a-f0-9]{64}$/.test(expectedSha256) ||
    sha256(real) !== expectedSha256
  ) {
    throw new Error(`${label} failed its immutable SHA-256 binding.`);
  }
  return relative;
}

function readRegularJsonUnderRelease(
  projectRoot: string,
  releaseId: string,
  relative: string,
  label: string
): Record<string, unknown> {
  const releaseRoot = realpathSync(resolve(safeReleasesRoot(projectRoot), releaseId));
  const requested = resolve(releaseRoot, relative);
  const stat = lstatSync(requested);
  if (!stat.isFile() || stat.isSymbolicLink()) {
    throw new Error(`${label} must be a regular, non-symlink file.`);
  }
  const real = realpathSync(requested);
  if (!real.startsWith(`${releaseRoot}${sep}`)) throw new Error(`${label} escaped its release.`);
  let parsed: unknown;
  try {
    parsed = JSON.parse(readStableFile(real).toString("utf8"));
  } catch {
    throw new Error(`${label} is not valid JSON.`);
  }
  if (!isRecord(parsed)) throw new Error(`${label} must contain a JSON object.`);
  return parsed;
}

function requiredRecord(value: unknown, label: string): Record<string, unknown> {
  if (!isRecord(value)) throw new Error(`${label} must be an object.`);
  return value;
}

function verifyPublicationMetadata(
  projectRoot: string,
  publication: AcceptedPublication
): void {
  const releaseManifest = readRegularJsonUnderRelease(
    projectRoot,
    publication.release_id,
    "release.json",
    "accepted publication release manifest"
  );
  const metadata = requiredRecord(
    releaseManifest.publication,
    "accepted publication metadata"
  );
  const binding = readRegularJsonUnderRelease(
    projectRoot,
    publication.release_id,
    "publication/binding.json",
    "accepted publication binding"
  );
  if (canonicalJson(metadata) !== canonicalJson(binding)) {
    throw new Error("Accepted publication binding does not match release.json metadata.");
  }
  if (metadata.binding_sha256 !== publication.binding_sha256) {
    throw new Error("Accepted publication aggregate hash does not match its release binding.");
  }
  const prefix = `data/releases/${publication.release_id}/`;
  const pulse = requiredRecord(metadata.pulse, "accepted publication pulse metadata");
  if (
    pulse.source_path !== publication.pulse ||
    `${prefix}${String(pulse.bound_path ?? "")}` !== publication.bound_pulse ||
    pulse.sha256 !== publication.pulse_sha256
  ) {
    throw new Error("Accepted publication pulse history does not match release metadata.");
  }
  if (!Array.isArray(metadata.artifact_manifests)) {
    throw new Error("Accepted publication metadata has no artifact manifest list.");
  }
  const payloadHashes: Record<string, string> = Object.create(null) as Record<string, string>;
  const addPayloadHash = (sourcePath: unknown, hash: string, label: string): void => {
    const relative = safeRelativePath(sourcePath, label);
    if (Object.prototype.hasOwnProperty.call(payloadHashes, relative)) {
      if (payloadHashes[relative] !== hash) {
        throw new Error(`Accepted publication payload path has conflicting hashes: ${relative}`);
      }
      return;
    }
    payloadHashes[relative] = hash;
  };
  addPayloadHash(pulse.source_path, publication.pulse_sha256, "accepted pulse source path");
  const historyByUrl = new Map(
    publication.artifact_manifests.map((manifest) => [manifest.url, manifest])
  );
  if (
    historyByUrl.size !== publication.artifact_manifests.length ||
    metadata.artifact_manifests.length !== historyByUrl.size
  ) {
    throw new Error("Accepted artifact manifest history does not match release metadata.");
  }
  for (const rawManifest of metadata.artifact_manifests) {
    const manifest = requiredRecord(rawManifest, "accepted artifact metadata");
    const history = historyByUrl.get(String(manifest.manifest_url ?? ""));
    if (
      !history ||
      `${prefix}${String(manifest.bound_path ?? "")}` !== history.bound_path ||
      manifest.sha256 !== history.sha256 ||
      typeof manifest.source_path !== "string"
    ) {
      throw new Error("Accepted artifact manifest history does not match release metadata.");
    }
    addPayloadHash(manifest.source_path, history.sha256, "accepted artifact source path");
    if (!Array.isArray(manifest.files) || manifest.files.length !== history.files.length) {
      throw new Error("Accepted artifact file history does not match release metadata.");
    }
    const historyFiles = new Map(history.files.map((file) => [file.url, file]));
    if (historyFiles.size !== history.files.length) {
      throw new Error("Accepted publication contains duplicate artifact file URLs.");
    }
    for (const rawFile of manifest.files) {
      const file = requiredRecord(rawFile, "accepted artifact file metadata");
      const fileHistory = historyFiles.get(String(file.url ?? ""));
      if (
        !fileHistory ||
        `${prefix}${String(file.bound_path ?? "")}` !== fileHistory.bound_path ||
        file.sha256 !== fileHistory.sha256 ||
        file.bytes !== fileHistory.bytes ||
        typeof file.source_path !== "string"
      ) {
        throw new Error("Accepted artifact file history does not match release metadata.");
      }
      addPayloadHash(file.source_path, fileHistory.sha256, "accepted artifact file source path");
    }
    if (manifest.generator != null) {
      const generator = requiredRecord(
        manifest.generator,
        "accepted artifact generator metadata"
      );
      if (
        typeof generator.source_path !== "string" ||
        typeof generator.bound_path !== "string" ||
        typeof generator.sha256 !== "string" ||
        typeof generator.bytes !== "number" ||
        !Number.isSafeInteger(generator.bytes) ||
        generator.bytes < 0
      ) {
        throw new Error("Accepted artifact generator metadata is invalid.");
      }
      const fullBoundPath = `${prefix}${generator.bound_path}`;
      const verifiedPath = safeBoundFile(
        projectRoot,
        publication.release_id,
        fullBoundPath,
        generator.sha256,
        "accepted artifact generator"
      );
      if (lstatSync(resolve(projectRoot, verifiedPath)).size !== generator.bytes) {
        throw new Error("Accepted artifact generator size does not match its binding.");
      }
      addPayloadHash(
        generator.source_path,
        generator.sha256,
        "accepted artifact generator source path"
      );
    }
  }
  if (canonicalJsonHash(payloadHashes) !== publication.binding_sha256) {
    throw new Error("Accepted publication aggregate binding SHA-256 is invalid.");
  }
}

function parseJsonArray(name: string): unknown[] {
  const configured = process.env[name];
  if (!configured) return [];
  if (Buffer.byteLength(configured, "utf8") > 16 * 1024 * 1024) {
    throw new Error(`${name} exceeds the 16 MiB configuration limit.`);
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(configured);
  } catch {
    throw new Error(`${name} must be a JSON array.`);
  }
  if (!Array.isArray(parsed) || parsed.length > 5000) {
    throw new Error(`${name} must be a bounded JSON array.`);
  }
  return parsed;
}

function resolveCandidateRelease(projectRoot: string, configured: string): string {
  const releasesRoot = safeReleasesRoot(projectRoot);
  const requested = resolve(configured);
  const stat = lstatSync(requested);
  if (!stat.isDirectory() || stat.isSymbolicLink()) {
    throw new Error("IMF_PULSE_RELEASE_DIR must name a regular release directory.");
  }
  const candidate = realpathSync(requested);
  const releaseId = basename(candidate);
  if (
    dirname(candidate) !== releasesRoot ||
    !releaseIdIsSafe(releaseId)
  ) {
    throw new Error("IMF_PULSE_RELEASE_DIR escaped data/releases or has an unsafe id.");
  }
  return releaseId;
}

export function validateAcceptedPublications(
  projectRoot: string,
  values: unknown
): AcceptedPublication[] {
  if (!Array.isArray(values) || values.length > 5000) {
    throw new Error("Accepted publications must be a bounded array.");
  }
  const publications = values.map((value, publicationIndex) => {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      throw new Error("IMF_PULSE_ACCEPTED_PUBLICATIONS contains a non-object entry.");
    }
    const row = value as Record<string, unknown>;
    const releaseId = String(row.release_id ?? "");
    if (!releaseIdIsSafe(releaseId)) {
      throw new Error("Accepted publication has an unsafe release id.");
    }
    const pulse = safeRelativePath(row.pulse, "accepted publication pulse");
    if (!/^content\/pulses\/\d{4}-\d{2}-\d{2}(?:-[1-9]\d{0,3})?\.md$/.test(pulse)) {
      throw new Error("Accepted publication pulse is not a dated, optionally indexed content/pulses file.");
    }
    const boundPulse = safeBoundFile(
      projectRoot,
      releaseId,
      row.bound_pulse,
      row.pulse_sha256,
      "accepted publication bound pulse"
    );
    const artifactRows = row.artifact_manifests;
    if (!Array.isArray(artifactRows)) {
      throw new Error("Accepted publication artifact_manifests must be an array.");
    }
    const manifests = artifactRows.map((artifact, artifactIndex) => {
      if (!artifact || typeof artifact !== "object" || Array.isArray(artifact)) {
        throw new Error("Accepted artifact manifest must be an object.");
      }
      const manifest = artifact as Record<string, unknown>;
      const url = safePublicUrl(manifest.url, "accepted artifact manifest");
      const boundPath = safeBoundFile(
        projectRoot,
        releaseId,
        manifest.bound_path,
        manifest.sha256,
        `accepted artifact manifest ${publicationIndex}:${artifactIndex}`
      );
      if (!boundPath.endsWith(`publication/public${url}`)) {
        throw new Error("Accepted artifact manifest URL does not match its bound path.");
      }
      if (!Array.isArray(manifest.files)) throw new Error("Accepted artifact files must be an array.");
      const files = manifest.files.map((file, fileIndex) => {
        if (!file || typeof file !== "object" || Array.isArray(file)) {
          throw new Error("Accepted artifact file must be an object.");
        }
        const item = file as Record<string, unknown>;
        const fileUrl = safePublicUrl(item.url, "accepted artifact file");
        const filePath = safeBoundFile(
          projectRoot,
          releaseId,
          item.bound_path,
          item.sha256,
          `accepted artifact file ${publicationIndex}:${artifactIndex}:${fileIndex}`
        );
        const bytes = Number(item.bytes);
        if (!Number.isSafeInteger(bytes) || bytes < 0 || lstatSync(resolve(projectRoot, filePath)).size !== bytes) {
          throw new Error("Accepted artifact file size does not match its binding.");
        }
        if (!filePath.endsWith(`publication/public${fileUrl}`)) {
          throw new Error("Accepted artifact file URL does not match its bound path.");
        }
        return { url: fileUrl, bound_path: filePath, sha256: String(item.sha256), bytes };
      });
      return { url, bound_path: boundPath, sha256: String(manifest.sha256), files };
    });
    if (typeof row.binding_sha256 !== "string" || !/^[a-f0-9]{64}$/.test(row.binding_sha256)) {
      throw new Error("Accepted publication has an invalid binding SHA-256.");
    }
    const publication: AcceptedPublication = {
      release_id: releaseId,
      pulse,
      bound_pulse: boundPulse,
      pulse_sha256: String(row.pulse_sha256),
      binding_sha256: row.binding_sha256,
      artifact_manifests: manifests
    };
    verifyPublicationMetadata(projectRoot, publication);
    return publication;
  });
  const pulseIds = new Set<string>();
  for (const publication of publications) {
    if (pulseIds.has(publication.pulse)) {
      throw new Error(`Accepted publication pulse is duplicated: ${publication.pulse}`);
    }
    pulseIds.add(publication.pulse);
  }
  return publications;
}

function acceptedPublications(projectRoot: string): AcceptedPublication[] {
  return validateAcceptedPublications(
    projectRoot,
    parseJsonArray("IMF_PULSE_ACCEPTED_PUBLICATIONS")
  );
}

export function getCandidateReleaseContext(
  projectRoot = process.cwd()
): CandidateReleaseContext | undefined {
  const configured = process.env.IMF_PULSE_RELEASE_DIR;
  if (!configured) {
    if (
      [
        "IMF_PULSE_SELECTED_PULSE",
        "IMF_PULSE_ARTIFACT_MANIFESTS",
        "IMF_PULSE_ACCEPTED_PUBLICATIONS",
        "IMF_PULSE_CHECKPOINT_STATUS"
      ].some((name) => Object.prototype.hasOwnProperty.call(process.env, name))
    ) {
      throw new Error("Candidate pulse context requires IMF_PULSE_RELEASE_DIR.");
    }
    return undefined;
  }
  const releaseId = resolveCandidateRelease(projectRoot, configured);
  const publicationGate = Object.prototype.hasOwnProperty.call(
    process.env,
    "IMF_PULSE_ACCEPTED_PUBLICATIONS"
  );
  const checkpointValue = process.env.IMF_PULSE_CHECKPOINT_STATUS;
  const checkpointStatus = CHECKPOINT_STATUSES.has(checkpointValue as CheckpointStatus)
    ? checkpointValue as CheckpointStatus
    : undefined;
  if (publicationGate && !checkpointStatus) {
    throw new Error(
      "A publication gate requires IMF_PULSE_CHECKPOINT_STATUS to be published, processed_no_pulse, or unchanged."
    );
  }
  if (!publicationGate && Object.prototype.hasOwnProperty.call(process.env, "IMF_PULSE_CHECKPOINT_STATUS")) {
    throw new Error("IMF_PULSE_CHECKPOINT_STATUS requires the accepted-publication gate contract.");
  }
  const publications = publicationGate ? acceptedPublications(projectRoot) : [];
  const selectedValue = process.env.IMF_PULSE_SELECTED_PULSE;
  const selectedPulse = selectedValue
    ? safeRelativePath(selectedValue, "IMF_PULSE_SELECTED_PULSE")
    : undefined;
  if (selectedPulse && !/^content\/pulses\/\d{4}-\d{2}-\d{2}(?:-[1-9]\d{0,3})?\.md$/.test(selectedPulse)) {
    throw new Error("IMF_PULSE_SELECTED_PULSE must name a dated, optionally indexed content/pulses Markdown file.");
  }
  const artifactManifests = [
    ...new Set(
      parseJsonArray("IMF_PULSE_ARTIFACT_MANIFESTS").map((value) =>
        safePublicUrl(value, "IMF_PULSE_ARTIFACT_MANIFESTS")
      )
    )
  ];
  if (publicationGate && checkpointStatus === "published") {
    const selectedPublication = publications.at(-1);
    if (!selectedPulse || !selectedPublication || selectedPublication.pulse !== selectedPulse) {
      throw new Error("Selected pulse is not the latest immutable accepted publication.");
    }
    const expected = selectedPublication.artifact_manifests.map((manifest) => manifest.url).sort();
    if (JSON.stringify([...artifactManifests].sort()) !== JSON.stringify(expected)) {
      throw new Error("Selected artifact manifests do not match the immutable publication binding.");
    }
  } else if (publicationGate) {
    if (selectedPulse || artifactManifests.length > 0) {
      throw new Error("A no-pulse checkpoint cannot select pulse or artifact inputs.");
    }
  } else if (!selectedPulse && artifactManifests.length > 0) {
    throw new Error("Artifact manifests require an explicitly selected pulse.");
  }
  const candidatePublications = publications.filter(
    (publication) => publication.release_id === releaseId
  );
  if (candidatePublications.length > 1) {
    throw new Error("A release cannot bind more than one accepted publication.");
  }
  if (
    publicationGate &&
    checkpointStatus === "published" &&
    (candidatePublications.length !== 1 || candidatePublications[0] !== publications.at(-1))
  ) {
    throw new Error("The candidate release publication must be the latest accepted publication.");
  }
  return {
    releaseId,
    selectedPulse,
    artifactManifests,
    acceptedPublications: publications,
    publicationGate,
    checkpointStatus
  };
}

export function getCandidateReleaseId(projectRoot = process.cwd()): string | undefined {
  return getCandidateReleaseContext(projectRoot)?.releaseId;
}
