import { parse as parseYaml } from "yaml";
import { pulseModules } from "virtual:imf-pulse-content";
import {
  PulseFrontmatterSchema,
  type CurrentRelease,
  type PulseFrontmatter
} from "./schemas";

export interface PulseDocument {
  id: string;
  date: string;
  title: string;
  lead: string;
  status: "published" | "draft" | "preview";
  topics: string[];
  featuredArtifact?: string;
  artifactManifests: string[];
  sourceIds: string[];
  body: string;
  sourcePath: string;
  issues: string[];
  metadata: PulseFrontmatter;
}

export interface PulseCatalog {
  mode: "authorized" | "retained" | "preview";
  pulses: PulseDocument[];
  latest?: PulseDocument;
}

interface FrontmatterParts {
  attributes: unknown;
  body: string;
  issue?: string;
}

function splitFrontmatter(raw: string): FrontmatterParts {
  const normalized = raw.replace(/^\uFEFF/, "");
  if (!normalized.startsWith("---\n") && !normalized.startsWith("---\r\n")) {
    return { attributes: {}, body: normalized };
  }

  const match = normalized.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?/);
  if (!match) {
    return {
      attributes: {},
      body: normalized,
      issue: "Front matter was opened but not closed."
    };
  }

  try {
    return {
      attributes: parseYaml(match[1], { maxAliasCount: 20 }) ?? {},
      body: normalized.slice(match[0].length)
    };
  } catch (error) {
    return {
      attributes: {},
      body: normalized.slice(match[0].length),
      issue: `Front matter could not be parsed: ${error instanceof Error ? error.message : "unknown error"}`
    };
  }
}

function stringValue(value: unknown): string | undefined {
  if (typeof value === "string" && value.trim()) return value.trim();
  if (value instanceof Date) return value.toISOString().slice(0, 10);
  if (typeof value === "number") return String(value);
  return undefined;
}

function stringList(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value
      .map(stringValue)
      .filter((item): item is string => Boolean(item));
  }
  const scalar = stringValue(value);
  return scalar
    ? scalar
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean)
    : [];
}

function normalizeFrontmatter(value: unknown): Record<string, unknown> {
  const raw = value && typeof value === "object" ? (value as Record<string, unknown>) : {};
  const featured = raw.featured_artifact ?? raw.featuredArtifact ?? raw.artifact_id;
  const manifestList =
    raw.artifact_manifests ?? raw.artifactManifests ?? raw.artifact_manifest;
  return {
    ...raw,
    schema_version: raw.schema_version ?? raw.schemaVersion,
    id: stringValue(raw.id ?? raw.pulse_id),
    date: stringValue(raw.date ?? raw.published_at),
    title: stringValue(raw.title),
    lead: stringValue(raw.lead ?? raw.summary ?? raw.dek),
    status: stringValue(raw.status) ?? "published",
    topics: stringList(raw.topics ?? raw.topic_ids),
    featured_artifact: stringValue(featured),
    artifact_manifests: stringList(manifestList),
    source_ids: stringList(raw.source_ids ?? raw.sources)
  };
}

function filenameDate(path: string): string | undefined {
  return path.match(/(\d{4}-\d{2}-\d{2})\.md$/)?.[1];
}

function firstHeading(body: string): string | undefined {
  return body.match(/^#\s+(.+)$/m)?.[1]?.trim();
}

function stripFirstHeading(body: string, title: string): string {
  const match = body.match(/^#\s+(.+)\r?\n+/m);
  if (!match || match[1].trim() !== title.trim()) return body.trim();
  return `${body.slice(0, match.index)}${body.slice((match.index ?? 0) + match[0].length)}`.trim();
}

function plainText(markdown: string): string {
  return markdown
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/[*_`>#]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function firstParagraph(body: string): string | undefined {
  const blocks = body.split(/\r?\n\s*\r?\n/);
  const paragraph = blocks.find((block) => {
    const value = block.trim();
    return value && !value.startsWith("#") && !value.startsWith("```");
  });
  return paragraph ? plainText(paragraph) : undefined;
}

export function parsePulseMarkdown(raw: string, sourcePath: string): PulseDocument {
  const parts = splitFrontmatter(raw);
  const issues: string[] = parts.issue ? [parts.issue] : [];
  const normalized = normalizeFrontmatter(parts.attributes);
  const parsed = PulseFrontmatterSchema.safeParse(normalized);
  const metadata = parsed.success
    ? parsed.data
    : PulseFrontmatterSchema.parse({ status: "preview" });

  if (!parsed.success) {
    issues.push(...parsed.error.issues.map((issue) => issue.message));
  }

  const dateFromFile = filenameDate(sourcePath);
  const title = metadata.title ?? firstHeading(parts.body) ?? "Untitled pulse";
  const date = metadata.date?.slice(0, 10) ?? dateFromFile ?? "undated";
  const body = stripFirstHeading(parts.body, title);

  if (metadata.date && dateFromFile && metadata.date.slice(0, 10) !== dateFromFile) {
    issues.push("Pulse date does not match its filename.");
  }

  return {
    id: metadata.id ?? `pulse-${date}`,
    date,
    title,
    lead: metadata.lead ?? firstParagraph(body) ?? "No lead was supplied for this pulse.",
    status: issues.length > 0 ? "preview" : metadata.status,
    topics: metadata.topics,
    featuredArtifact: metadata.featured_artifact,
    artifactManifests: metadata.artifact_manifests,
    sourceIds: metadata.source_ids,
    body,
    sourcePath,
    issues,
    metadata
  };
}

function comparePulseDates(a: PulseDocument, b: PulseDocument): number {
  return b.date.localeCompare(a.date) || a.id.localeCompare(b.id);
}

export function getPulseDocuments(): PulseDocument[] {
  const seenDates = new Set<string>();
  const seenIds = new Set<string>();

  return Object.entries(pulseModules)
    .map(([path, raw]) => parsePulseMarkdown(raw, path))
    .sort(comparePulseDates)
    .map((pulse) => {
      const issues = [...pulse.issues];
      if (seenDates.has(pulse.date)) issues.push(`Duplicate pulse date: ${pulse.date}.`);
      if (seenIds.has(pulse.id)) issues.push(`Duplicate pulse id: ${pulse.id}.`);
      seenDates.add(pulse.date);
      seenIds.add(pulse.id);
      return issues.length === pulse.issues.length
        ? pulse
        : { ...pulse, issues, status: "preview" as const };
    });
}

export function getLatestPulse(reference?: string): PulseDocument | undefined {
  const pulses = getPulseDocuments();
  if (reference) {
    return findPulseByReference(pulses, reference);
  }
  return pulses.find((pulse) => pulse.status === "published") ?? pulses[0];
}

export function findPulseByReference(
  pulses: PulseDocument[],
  reference: string
): PulseDocument | undefined {
  const normalizedReference = reference.replace(/^\.\//, "");
  return pulses.find(
    (pulse) =>
      pulse.id === normalizedReference ||
      pulse.sourcePath.replace(/^\//, "") === normalizedReference ||
      pulse.sourcePath.endsWith(`/${normalizedReference}`) ||
      pulse.date === normalizedReference ||
      pulse.date === normalizedReference.match(/(\d{4}-\d{2}-\d{2})/)?.[1]
  );
}

export function selectPulseCatalog(
  pulses: PulseDocument[],
  current?: CurrentRelease
): PulseCatalog {
  if (!current) {
    const previewPulses = pulses.filter(
      (pulse) => pulse.status === "published" && pulse.issues.length === 0
    );
    return {
      mode: "preview",
      pulses: previewPulses,
      latest: previewPulses[0]
    };
  }

  const isCandidate = current.status?.startsWith("candidate_") === true;
  const isNoUpdate = [
    "processed_no_pulse",
    "unchanged",
    "candidate_no_pulse"
  ].includes(current.status ?? "");
  const latestReference = current.pulse ?? current.latest_accepted_pulse;
  const acceptedReferences = uniqueReferences([
    ...current.accepted_pulses,
    ...(current.status === "published" || current.status === "candidate_selected_pulse"
      ? [current.pulse]
      : []),
    ...(isNoUpdate ? [current.latest_accepted_pulse] : [])
  ]);
  const acceptedPulses = acceptedReferences
    .map((reference) => findPulseByReference(pulses, reference))
    .filter(
      (pulse): pulse is PulseDocument =>
        pulse != null && pulse.status === "published" && pulse.issues.length === 0
    )
    .sort(comparePulseDates);
  const latest = latestReference
    ? findPulseByReference(acceptedPulses, latestReference)
    : undefined;
  return {
    mode: isCandidate ? "preview" : isNoUpdate ? "retained" : "authorized",
    pulses: acceptedPulses,
    latest
  };
}

function uniqueReferences(values: Array<string | null | undefined>): string[] {
  return [...new Set(values.filter((value): value is string => Boolean(value)))];
}

export function getPulseCatalog(current?: CurrentRelease): PulseCatalog {
  return selectPulseCatalog(getPulseDocuments(), current);
}

export function getPulseByDate(date: string | undefined): PulseDocument | undefined {
  if (!date) return undefined;
  return getPulseDocuments().find((pulse) => pulse.date === date);
}
