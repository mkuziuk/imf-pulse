import {
  ClaimSchema,
  CurrentReleaseSchema,
  EvidenceRefSchema,
  ExperimentSchema,
  MethodSchema,
  RelationshipSchema,
  SourceSchema,
  type ClaimRecord,
  type CurrentRelease,
  type EvidenceRef,
  type ExperimentRecord,
  type MethodRecord,
  type RelationshipRecord,
  type SourceRecord
} from "./schemas";
import {
  currentModules,
  curatedModules,
  releaseModules
} from "virtual:imf-pulse-content";

type UnknownRecord = Record<string, unknown>;

export interface KnowledgeSnapshot {
  state: "ready" | "preview" | "empty" | "unavailable";
  reason?: string;
  current?: CurrentRelease;
  sources: SourceRecord[];
  claims: ClaimRecord[];
  methods: MethodRecord[];
  experiments: ExperimentRecord[];
  relationships: RelationshipRecord[];
  rejectedRecords: number;
}

function isRecord(value: unknown): value is UnknownRecord {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function asString(value: unknown): string | undefined {
  if (typeof value === "string" && value.trim()) return value.trim();
  if (typeof value === "number") return String(value);
  return undefined;
}

function asStringList(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.map(asString).filter((item): item is string => Boolean(item));
  }
  const scalar = asString(value);
  return scalar ? [scalar] : [];
}

function formatLocatorForContract(value: unknown): string | UnknownRecord | undefined {
  if (typeof value === "string") return value;
  return isRecord(value) ? value : undefined;
}

function normalizeEvidence(value: unknown, row: UnknownRecord): EvidenceRef[] {
  const candidates = Array.isArray(value)
    ? value
    : Array.isArray(row.evidence_refs)
      ? row.evidence_refs
      : [];

  const references: EvidenceRef[] = [];
  for (const item of candidates) {
    if (!isRecord(item)) continue;
    const parsed = EvidenceRefSchema.safeParse({
      ...item,
      source_id: asString(item.source_id ?? item.source),
      locator: formatLocatorForContract(
        item.locator ?? item.source_locator ?? item.location
      ),
      quote: asString(item.quote),
      excerpt: asString(item.excerpt)
    });
    if (parsed.success) references.push(parsed.data);
  }
  return references;
}

function normalizeStatus(value: unknown): ClaimRecord["status"] {
  const status = (asString(value) ?? "incomplete").toLowerCase().replace(/_/g, "-");
  if (status === "proven" || status === "calculated-exact-under-assumptions") return "proved";
  if (status === "supported" || status === "empirical" || status.startsWith("observed")) {
    return "observed";
  }
  if (status === "inference") return "inferred";
  if (status === "conjecture") return "conjectured";
  if (status === "contradicted") return "contradicted";
  return "incomplete";
}

function parseJsonLines(raw: string): { rows: UnknownRecord[]; rejected: number } {
  let rejected = 0;
  const rows: UnknownRecord[] = [];
  for (const line of raw.split(/\r?\n/)) {
    if (!line.trim()) continue;
    try {
      const value: unknown = JSON.parse(line);
      if (isRecord(value)) rows.push(value);
      else rejected += 1;
    } catch {
      rejected += 1;
    }
  }
  return { rows, rejected };
}

type CurrentParseResult = { value?: CurrentRelease; invalid?: string; exists: boolean };

function parseStoredCurrent(): CurrentParseResult {
  const raw = Object.values(currentModules)[0];
  if (!raw) return { exists: false };
  try {
    const parsed: unknown = JSON.parse(raw);
    const result = CurrentReleaseSchema.safeParse(parsed);
    if (!result.success) return { exists: true, invalid: "The release pointer failed validation." };
    if (!/^[a-zA-Z0-9][a-zA-Z0-9._-]*$/.test(result.data.release_id)) {
      return { exists: true, invalid: "The release id is not a safe path segment." };
    }
    return { exists: true, value: result.data };
  } catch {
    return { exists: true, invalid: "The release pointer is not valid JSON." };
  }
}

function parseCurrent(): CurrentParseResult {
  return parseStoredCurrent();
}

function sourceRecord(row: UnknownRecord): SourceRecord | undefined {
  const rights = isRecord(row.rights) ? row.rights : undefined;
  const normalized = {
    ...row,
    id: asString(row.id ?? row.source_id),
    title: asString(row.title ?? row.name),
    authors: asStringList(row.authors),
    date: asString(row.date ?? row.publication_date),
    source_type: asString(row.source_type ?? row.type),
    authority_level: asString(row.authority_level ?? row.authority),
    publication_status: asString(row.publication_status ?? row.status),
    topics: asStringList(row.topics),
    location: asString(row.location ?? row.local_path ?? row.path),
    url: asString(row.url),
    rights_status: asString(
      row.rights_status ?? rights?.reuse_status ?? rights?.status ?? rights?.license ?? row.license
    ),
    content_hash: asString(row.content_hash ?? row.content_sha256 ?? row.sha256),
    limitations: row.limitations,
    retrieved_at: asString(row.retrieved_at ?? row.retrieval_date),
    last_processed_at: asString(row.last_processed_at)
  };
  const result = SourceSchema.safeParse(normalized);
  return result.success ? result.data : undefined;
}

function claimRecord(row: UnknownRecord): ClaimRecord | undefined {
  const normalized = {
    ...row,
    statement: asString(row.statement ?? row.normalized_text ?? row.normalized_claim ?? row.claim),
    status: normalizeStatus(row.status ?? row.evidence_status ?? row.statement_kind),
    evidence: normalizeEvidence(row.evidence, row)
  };
  const result = ClaimSchema.safeParse(normalized);
  return result.success ? result.data : undefined;
}

function methodRecord(row: UnknownRecord): MethodRecord | undefined {
  const normalized = {
    ...row,
    name: asString(row.name ?? row.title ?? row.objective),
    objective: asString(row.objective),
    robust_loss: asString(row.robust_loss ?? row.loss),
    boundary_behavior: asString(row.boundary_behavior ?? row.boundary),
    evidence: normalizeEvidence(row.evidence, row)
  };
  const result = MethodSchema.safeParse(normalized);
  return result.success ? result.data : undefined;
}

function experimentRecord(row: UnknownRecord): ExperimentRecord | undefined {
  const title = asString(row.name ?? row.title ?? row.objective) ?? "Untitled experiment";
  const normalized = {
    ...row,
    name: title,
    objective: asString(row.objective ?? row.question),
    observation_model: asString(row.observation_model),
    contamination_model: asString(row.contamination_model),
    evidence: normalizeEvidence(row.evidence, row)
  };
  const result = ExperimentSchema.safeParse(normalized);
  return result.success ? result.data : undefined;
}

const relationshipTypes = new Set<RelationshipRecord["type"]>([
  "supports",
  "contradicts",
  "extends",
  "implements",
  "approximates",
  "depends-on",
  "uses-different-target",
  "valid-only-under",
  "reproduces",
  "fails-to-reproduce"
]);

function relationshipRecord(row: UnknownRecord): RelationshipRecord | undefined {
  const from = isRecord(row.from) ? row.from : {};
  const to = isRecord(row.to) ? row.to : {};
  const rawType = (asString(row.type ?? row.predicate ?? row.relation) ?? "depends-on")
    .replace(/_/g, "-") as RelationshipRecord["type"];
  const normalized = {
    ...row,
    source_id: asString(row.subject_id ?? row.source_id ?? row.from_id ?? from.id),
    target_id: asString(row.target_id ?? row.to_id ?? to.id),
    type: relationshipTypes.has(rawType) ? rawType : "depends-on",
    title: asString(row.title ?? row.qualification),
    evidence: normalizeEvidence(row.evidence, row)
  };
  const result = RelationshipSchema.safeParse(normalized);
  return result.success ? result.data : undefined;
}

function dedupe<T extends { id: string }>(records: T[]): T[] {
  const seen = new Set<string>();
  return records.filter((record) => {
    if (seen.has(record.id)) return false;
    seen.add(record.id);
    return true;
  });
}

export function getKnowledgeSnapshot(): KnowledgeSnapshot {
  const current = parseCurrent();
  if (current.invalid) {
    return {
      state: "unavailable",
      reason: current.invalid,
      sources: [],
      claims: [],
      methods: [],
      experiments: [],
      relationships: [],
      rejectedRecords: 0
    };
  }

  let modules: Record<string, string> = curatedModules;
  let state: KnowledgeSnapshot["state"] = "preview";
  if (current.value) {
    const releaseMarker = `/data/releases/${current.value.release_id}/`;
    modules = Object.fromEntries(
      Object.entries(releaseModules).filter(([path]) => path.includes(releaseMarker))
    );
    if (Object.keys(modules).length === 0) {
      return {
        state: "unavailable",
        reason: `Release ${current.value.release_id} has no public knowledge view.`,
        current: current.value,
        sources: [],
        claims: [],
        methods: [],
        experiments: [],
        relationships: [],
        rejectedRecords: 0
      };
    }
    state = current.value.status?.startsWith("candidate_") ? "preview" : "ready";
  }

  const sources: SourceRecord[] = [];
  const claims: ClaimRecord[] = [];
  const methods: MethodRecord[] = [];
  const experiments: ExperimentRecord[] = [];
  const relationships: RelationshipRecord[] = [];
  let rejectedRecords = 0;

  for (const [path, raw] of Object.entries(modules)) {
    const parsed = parseJsonLines(raw);
    rejectedRecords += parsed.rejected;
    for (const row of parsed.rows) {
      const record = path.endsWith("/sources.jsonl")
        ? sourceRecord(row)
        : path.endsWith("/claims.jsonl")
          ? claimRecord(row)
          : path.endsWith("/methods.jsonl")
            ? methodRecord(row)
            : path.endsWith("/experiments.jsonl")
              ? experimentRecord(row)
              : path.endsWith("/relationships.jsonl")
                ? relationshipRecord(row)
                : undefined;
      if (!record) {
        rejectedRecords += 1;
      } else if (path.endsWith("/sources.jsonl")) {
        sources.push(record as SourceRecord);
      } else if (path.endsWith("/claims.jsonl")) {
        claims.push(record as ClaimRecord);
      } else if (path.endsWith("/methods.jsonl")) {
        methods.push(record as MethodRecord);
      } else if (path.endsWith("/experiments.jsonl")) {
        experiments.push(record as ExperimentRecord);
      } else if (path.endsWith("/relationships.jsonl")) {
        relationships.push(record as RelationshipRecord);
      }
    }
  }

  const recordCount = sources.length + claims.length + methods.length + experiments.length;
  return {
    state: recordCount === 0 ? "empty" : state,
    current: current.value,
    sources: dedupe(sources),
    claims: dedupe(claims),
    methods: dedupe(methods),
    experiments: dedupe(experiments),
    relationships: dedupe(relationships),
    rejectedRecords
  };
}

export function getKnowledgeLabel(
  snapshot: KnowledgeSnapshot,
  id: string
): { title: string; kind: string } {
  const source = snapshot.sources.find((record) => record.id === id);
  if (source) return { title: source.title, kind: "source" };
  const claim = snapshot.claims.find((record) => record.id === id);
  if (claim) return { title: claim.statement, kind: "claim" };
  const method = snapshot.methods.find((record) => record.id === id);
  if (method) return { title: method.name, kind: "method" };
  const experiment = snapshot.experiments.find((record) => record.id === id);
  if (experiment) return { title: experiment.name, kind: "experiment" };
  return { title: id, kind: "unresolved" };
}
