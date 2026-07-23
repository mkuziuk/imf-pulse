import { getPulseCatalog, type PulseCatalog, type PulseDocument } from "./content";
import { getKnowledgeSnapshot } from "./data";
import { isPublicArtifactUrl, withBaseUrl } from "./links";
import {
  ArtifactFileSchema,
  ArtifactSchema,
  EvidenceRefSchema,
  StageErrorDatumSchema,
  type ArtifactFile,
  type ArtifactRecord,
  type EvidenceRef,
  type StageErrorDatum
} from "./schemas";

type UnknownRecord = Record<string, unknown>;

export interface ArtifactLoadResult {
  artifacts: ArtifactRecord[];
  issues: string[];
}

export interface ArtifactManifestCatalog {
  mode: PulseCatalog["mode"];
  manifestUrls: string[];
}

function isRecord(value: unknown): value is UnknownRecord {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function stringValue(value: unknown): string | undefined {
  if (typeof value === "string" && value.trim()) return value.trim();
  if (typeof value === "number") return String(value);
  return undefined;
}

function manifestDirectory(manifestUrl: string): string {
  return manifestUrl.slice(0, manifestUrl.lastIndexOf("/") + 1);
}

function normalizeArtifactFileUrl(value: unknown, manifestUrl: string): string | undefined {
  const raw = stringValue(value);
  if (!raw || raw.includes("..") || /^https?:/i.test(raw)) return undefined;
  const directory = manifestDirectory(manifestUrl);
  const resolved = raw.startsWith("/") ? raw : `${directory}${raw.replace(/^\.\//, "")}`;
  return isPublicArtifactUrl(resolved) && resolved.startsWith(directory) ? resolved : undefined;
}

function normalizeFileKind(role: unknown, mediaType: unknown, url: string): string {
  const text = `${stringValue(role) ?? ""} ${stringValue(mediaType) ?? ""} ${url}`.toLowerCase();
  if (text.includes("csv") || text.includes("underlying data")) return "data";
  if (text.includes("json") || text.includes("spec")) return "spec";
  if (text.includes("svg") || text.includes("png") || text.includes("image")) return "image";
  if (text.includes("pdf") || text.includes("document")) return "document";
  return "source";
}

function normalizeFiles(value: unknown, manifestUrl: string, row: UnknownRecord): ArtifactFile[] {
  const candidates: unknown[] = Array.isArray(value) ? [...value] : [];
  if (Array.isArray(row.downloads)) candidates.push(...row.downloads);

  const directUrls: Array<[string, unknown]> = [
    ["image", row.stable_url],
    ["spec", row.spec_url],
    ["data", row.data_url]
  ];
  for (const [kind, urlValue] of directUrls) {
    if (urlValue) candidates.push({ kind, url: urlValue });
  }

  const seen = new Set<string>();
  const files: ArtifactFile[] = [];
  for (const candidate of candidates) {
    if (!isRecord(candidate)) continue;
    const url = normalizeArtifactFileUrl(candidate.url ?? candidate.path, manifestUrl);
    if (!url || seen.has(url)) continue;
    const mime = stringValue(candidate.mime_type ?? candidate.media_type);
    const role = candidate.kind ?? candidate.role ?? candidate.type;
    const parsed = ArtifactFileSchema.safeParse({
      ...candidate,
      kind: stringValue(candidate.kind) ?? normalizeFileKind(role, mime, url),
      url,
      label:
        stringValue(candidate.label ?? candidate.role) ?? normalizeFileKind(role, mime, url),
      mime_type: mime
    });
    if (parsed.success) {
      seen.add(url);
      files.push(parsed.data);
    }
  }
  return files;
}

function normalizeEvidence(value: unknown): EvidenceRef[] {
  if (!Array.isArray(value)) return [];
  const references: EvidenceRef[] = [];
  for (const evidence of value) {
    if (!isRecord(evidence)) continue;
    const parsed = EvidenceRefSchema.safeParse({
      ...evidence,
      source_id: stringValue(evidence.source_id ?? evidence.source),
      locator:
        evidence.locator ??
        evidence.source_locator ??
        evidence.location
    });
    if (parsed.success) references.push(parsed.data);
  }
  return references;
}

function normalizeArtifactClass(value: unknown): ArtifactRecord["artifact_class"] {
  const artifactClass = (stringValue(value) ?? "diagram").toLowerCase().replace(/-/g, "_");
  if (artifactClass === "chart" || artifactClass === "scientific_figure") {
    return "scientific_chart";
  }
  if (artifactClass === "generated" || artifactClass === "conceptual_illustration") {
    return "generated_image";
  }
  if (artifactClass === "web" || artifactClass === "external_image") return "web_image";
  if (
    artifactClass === "scientific_chart" ||
    artifactClass === "web_image" ||
    artifactClass === "generated_image" ||
    artifactClass === "diagram"
  ) {
    return artifactClass;
  }
  return "diagram";
}

export function normalizeArtifactManifest(
  value: unknown,
  manifestUrl: string
): ArtifactRecord[] {
  const manifest = isRecord(value) ? value : {};
  const candidates = Array.isArray(manifest.artifacts) ? manifest.artifacts : [manifest];

  return candidates
    .filter(isRecord)
    .map((row) => {
      const rights = isRecord(row.rights) ? row.rights : {};
      const normalized = {
        ...row,
        id: stringValue(row.id ?? row.artifact_id),
        title: stringValue(row.title ?? row.caption),
        artifact_class: normalizeArtifactClass(
          row.artifact_class ?? row.artifact_type ?? row.class ?? row.type
        ),
        caption: stringValue(row.caption ?? row.description ?? row.title),
        relation_to_report: stringValue(row.relation_to_report ?? row.relationship),
        stable_url: normalizeArtifactFileUrl(row.stable_url ?? row.path, manifestUrl),
        rights_status:
          stringValue(row.rights_status ?? rights.status ?? row.license) ?? "unknown",
        creator: stringValue(row.creator ?? rights.creator),
        source_url: stringValue(row.source_url ?? rights.source_url),
        retrieved_at: stringValue(row.retrieved_at ?? row.retrieval_date),
        related_pulse: stringValue(row.related_pulse ?? row.pulse_id),
        files: normalizeFiles(row.files, manifestUrl, row),
        evidence: normalizeEvidence(row.evidence)
      };
      const parsed = ArtifactSchema.safeParse(normalized);
      return parsed.success ? parsed.data : undefined;
    })
    .filter((artifact): artifact is ArtifactRecord => Boolean(artifact));
}

const artifactCache = new Map<string, Promise<ArtifactRecord[]>>();

async function loadOneManifest(url: string): Promise<ArtifactRecord[]> {
  if (!isPublicArtifactUrl(url)) throw new Error("Manifest URL is not a local artifact path.");
  const response = await fetch(withBaseUrl(url), {
    headers: { Accept: "application/json" },
    credentials: "same-origin"
  });
  if (!response.ok) throw new Error(`Manifest returned HTTP ${response.status}.`);
  const contentType = response.headers.get("content-type");
  if (contentType && !contentType.includes("json")) {
    throw new Error("Manifest did not return JSON.");
  }
  const value: unknown = await response.json();
  const artifacts = normalizeArtifactManifest(value, url);
  if (artifacts.length === 0) throw new Error("Manifest did not contain a valid artifact.");
  return artifacts;
}

export async function loadArtifactManifests(urls: string[]): Promise<ArtifactLoadResult> {
  const uniqueUrls = [...new Set(urls.filter(isPublicArtifactUrl))];
  const issues: string[] = [];
  const artifacts: ArtifactRecord[] = [];

  await Promise.all(
    uniqueUrls.map(async (url) => {
      try {
        let pending = artifactCache.get(url);
        if (!pending) {
          pending = loadOneManifest(url);
          artifactCache.set(url, pending);
        }
        artifacts.push(...(await pending));
      } catch (error) {
        artifactCache.delete(url);
        issues.push(
          `${url}: ${error instanceof Error ? error.message : "Artifact manifest unavailable."}`
        );
      }
    })
  );

  return {
    artifacts: artifacts.filter(
      (artifact, index) => artifacts.findIndex((candidate) => candidate.id === artifact.id) === index
    ),
    issues
  };
}

function uniqueManifestUrls(values: string[]): string[] {
  return [...new Set(values.filter(isPublicArtifactUrl))];
}

export function manifestUrlsForPulse(
  pulse: PulseDocument,
  current: ReturnType<typeof getKnowledgeSnapshot>["current"],
  mode: PulseCatalog["mode"]
): string[] {
  const pulseUrls = uniqueManifestUrls(pulse.artifactManifests);
  if (!current || mode === "preview") return pulseUrls;
  const releaseUrls = uniqueManifestUrls([
    ...current.accepted_artifact_manifests,
    ...(current.status === "published" || current.status === "candidate_selected_pulse"
      ? current.artifact_manifests
      : [])
  ]);
  const authorized = new Set(releaseUrls);
  return pulseUrls.filter((url) => authorized.has(url));
}

export function selectArtifactManifestCatalog(
  pulseCatalog: PulseCatalog,
  current?: ReturnType<typeof getKnowledgeSnapshot>["current"]
): ArtifactManifestCatalog {
  if (!current || pulseCatalog.mode === "preview") {
    return {
      mode: "preview",
      manifestUrls: uniqueManifestUrls(
        pulseCatalog.pulses.flatMap((pulse) => pulse.artifactManifests)
      )
    };
  }

  const releaseUrls = uniqueManifestUrls([
    ...current.accepted_artifact_manifests,
    ...(current.status === "published" || current.status === "candidate_selected_pulse"
      ? current.artifact_manifests
      : [])
  ]);
  return { mode: pulseCatalog.mode, manifestUrls: releaseUrls };
}

export function getArtifactManifestCatalog(): ArtifactManifestCatalog {
  const snapshot = getKnowledgeSnapshot();
  return selectArtifactManifestCatalog(getPulseCatalog(snapshot.current), snapshot.current);
}

export function getConfiguredArtifactManifestUrls(): string[] {
  return getArtifactManifestCatalog().manifestUrls;
}

function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;

  for (let index = 0; index < text.length; index += 1) {
    const character = text[index];
    if (character === '"') {
      if (quoted && text[index + 1] === '"') {
        field += '"';
        index += 1;
      } else {
        quoted = !quoted;
      }
    } else if (character === "," && !quoted) {
      row.push(field);
      field = "";
    } else if ((character === "\n" || character === "\r") && !quoted) {
      if (character === "\r" && text[index + 1] === "\n") index += 1;
      row.push(field);
      if (row.some((value) => value.length > 0)) rows.push(row);
      row = [];
      field = "";
    } else {
      field += character;
    }
  }
  if (field || row.length > 0) {
    row.push(field);
    rows.push(row);
  }
  return rows;
}

function numberValue(value: string | undefined): number | undefined {
  if (value == null || value.trim() === "") return undefined;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

export function parseStageErrorCsv(text: string): StageErrorDatum[] {
  const [headers = [], ...rows] = parseCsv(text);
  const values = rows.map((row) =>
    Object.fromEntries(headers.map((header, index) => [header.trim(), row[index]?.trim() ?? ""]))
  );

  if (headers.includes("series_id")) {
    const grouped = new Map<number, Partial<StageErrorDatum>>();
    for (const row of values) {
      const stage = numberValue(row.stage);
      const value = numberValue(row.value);
      if (stage == null || value == null) continue;
      const datum = grouped.get(stage) ?? {
        stage,
        window: numberValue(row.window_size)
      };
      if (row.series_id === "exact_single_pass") datum.singlePass = value;
      if (row.series_id === "exact_recursive") datum.recursiveExact = value;
      if (row.series_id === "seed777_recursive") datum.recursiveObserved = value;
      grouped.set(stage, datum);
    }
    return [...grouped.values()]
      .map((datum) => StageErrorDatumSchema.safeParse(datum))
      .filter((result) => result.success)
      .map((result) => result.data)
      .sort((a, b) => a.stage - b.stage);
  }

  return values
    .map((row) =>
      StageErrorDatumSchema.safeParse({
        stage: numberValue(row.stage),
        window: numberValue(row.window ?? row.window_size),
        singlePass: numberValue(row.singlePass ?? row.single_pass ?? row.single_scaled),
        recursiveExact: numberValue(
          row.recursiveExact ?? row.recursive_exact ?? row.recursive_scaled
        ),
        recursiveObserved: numberValue(
          row.recursiveObserved ?? row.recursive_observed ?? row.seed777_recursive
        )
      })
    )
    .filter((result) => result.success)
    .map((result) => result.data)
    .sort((a, b) => a.stage - b.stage);
}

export async function loadStageErrorData(url: string): Promise<StageErrorDatum[]> {
  if (!isPublicArtifactUrl(url)) return [];
  const response = await fetch(withBaseUrl(url), {
    headers: { Accept: "text/csv" },
    credentials: "same-origin"
  });
  if (!response.ok) return [];
  return parseStageErrorCsv(await response.text());
}

export function artifactCanRenderMedia(artifact: ArtifactRecord): boolean {
  const rights = artifact as ArtifactRecord & {
    rights?: {
      local_display_allowed?: boolean;
      may_publish_publicly?: boolean;
    };
  };
  if (rights.rights?.local_display_allowed === false) return false;
  if (artifact.artifact_class === "web_image") return artifactIsPubliclyCleared(artifact);
  if (rights.rights?.local_display_allowed === true) return true;
  const status = normalizedRightsStatus(artifact.rights_status);
  return new Set([
    "approved",
    "cleared",
    "not_applicable",
    "project_generated",
    "project_generated_scientific_chart",
    "project_generated_diagram",
    "project_generated_illustration"
  ]).has(status);
}

export function artifactIsPubliclyCleared(artifact: ArtifactRecord): boolean {
  const rights = artifact as ArtifactRecord & {
    rights?: { may_publish_publicly?: boolean };
  };
  if (rights.rights?.may_publish_publicly === false) return false;
  if (rights.rights?.may_publish_publicly === true) return true;
  return new Set([
    "approved",
    "cleared",
    "public_domain",
    "cc_by",
    "cc_by_sa",
    "cc0",
    "not_applicable"
  ]).has(normalizedRightsStatus(artifact.rights_status));
}

function normalizedRightsStatus(value: string): string {
  return value.trim().toLowerCase().replace(/[\s-]+/g, "_");
}
