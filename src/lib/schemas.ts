import { z } from "zod";

export const SchemaVersionSchema = z
  .union([z.string(), z.number()])
  .transform((value) => String(value));

const nonEmptyString = z.string().trim().min(1);

export const StringListSchema = z.preprocess((value) => {
  if (value == null) return [];
  if (Array.isArray(value)) return value;
  if (typeof value === "string") {
    return value
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
  }
  return value;
}, z.array(nonEmptyString));

const preciseLocatorText = nonEmptyString.refine(
  (value) =>
    /(?:\b(?:page|pages|line|lines|cell|cells|row|rows|equation|section|theorem|lemma|pointer)\b|\bp\.\s*\d|#\/)/i.test(
      value
    ),
  "A textual locator must identify a page, line, cell, row, equation, section, theorem, or pointer."
);

const relativeSourcePath = nonEmptyString.refine(
  (value) => {
    let decoded = value;
    for (let pass = 0; pass < 5; pass += 1) {
      if (
        decoded.startsWith("/") ||
        decoded.startsWith("\\") ||
        decoded.includes("\\") ||
        /[\u0000-\u001f\u007f]/.test(decoded) ||
        decoded.split("/").some((segment) => segment === "..")
      ) {
        return false;
      }
      try {
        const next = decodeURIComponent(decoded);
        if (next === decoded) return true;
        decoded = next;
      } catch {
        return false;
      }
    }
    return false;
  },
  "Locator paths must be safe repository-relative paths."
);

const StructuredLocatorSchema = z
  .object({
    kind: nonEmptyString,
    path: relativeSourcePath,
    page: z.number().int().positive().optional(),
    line_start: z.number().int().positive().optional(),
    line_end: z.number().int().positive().optional(),
    csv_row: nonEmptyString.optional(),
    json_pointer: nonEmptyString.optional(),
    cell_id: nonEmptyString.optional(),
    cell_index: z.number().int().nonnegative().optional(),
    output_index: z.number().int().nonnegative().optional(),
    section: nonEmptyString.optional(),
    equation: nonEmptyString.optional(),
    theorem: nonEmptyString.optional()
  })
  .catchall(z.unknown())
  .superRefine((value, context) => {
    if (value.line_start != null && value.line_end != null && value.line_end < value.line_start) {
      context.addIssue({
        code: "custom",
        path: ["line_end"],
        message: "line_end must not precede line_start."
      });
    }

    const hasCell = value.cell_id != null || value.cell_index != null;
    const requirements: Record<string, boolean> = {
      pdf: value.page != null,
      text_lines: value.line_start != null,
      file_lines: value.line_start != null,
      csv_rows: value.csv_row != null,
      json_pointer: value.json_pointer?.startsWith("/") === true,
      notebook_cell: hasCell,
      notebook_output: hasCell && value.output_index != null
    };
    const hasGenericPrecision =
      value.page != null ||
      value.line_start != null ||
      value.csv_row != null ||
      value.json_pointer?.startsWith("/") === true ||
      hasCell ||
      value.section != null ||
      value.equation != null ||
      value.theorem != null;
    const hasKnownRequirement = Object.prototype.hasOwnProperty.call(requirements, value.kind);
    const isPrecise = hasKnownRequirement ? requirements[value.kind] : hasGenericPrecision;
    if (!isPrecise) {
      context.addIssue({
        code: "custom",
        message: `Locator kind ${value.kind} is missing its precise page, line, row, pointer, or cell position.`
      });
    }
  });

export const LocatorSchema = z.union([preciseLocatorText, StructuredLocatorSchema]);

export const EvidenceRefSchema = z
  .object({
    source_id: nonEmptyString,
    locator: LocatorSchema,
    quote: z.string().trim().optional(),
    excerpt: z.string().trim().optional()
  })
  .passthrough();

export const ConfidenceSchema = z.union([
  z.number().min(0).max(1),
  z.enum(["low", "moderate", "high", "very_high"]),
  z
    .object({
      value: z.number().min(0).max(1).optional(),
      label: nonEmptyString.optional(),
      score: z.number().min(0).max(1).optional(),
      level: nonEmptyString.optional(),
      rationale: z.string().trim().optional()
    })
    .refine(
      (value) =>
        value.value != null ||
        value.label != null ||
        value.score != null ||
        value.level != null
    )
]);

export const PulseFrontmatterSchema = z
  .object({
    schema_version: SchemaVersionSchema.optional(),
    id: nonEmptyString.optional(),
    date: nonEmptyString.optional(),
    pulse_index: z.number().int().min(1).max(9999).optional(),
    title: nonEmptyString.optional(),
    lead: nonEmptyString.optional(),
    status: z.enum(["published", "draft", "preview"]).default("published"),
    topics: StringListSchema.default([]),
    featured_artifact: nonEmptyString.optional(),
    artifact_manifests: StringListSchema.default([]),
    source_ids: StringListSchema.default([])
  })
  .passthrough();

export const SourceSchema = z
  .object({
    schema_version: SchemaVersionSchema.optional(),
    id: nonEmptyString,
    title: nonEmptyString,
    authors: StringListSchema.default([]),
    date: z.string().trim().optional(),
    source_type: z.string().trim().optional(),
    authority_level: z.string().trim().optional(),
    publication_status: z.string().trim().optional(),
    topics: StringListSchema.default([]),
    location: z.string().trim().optional(),
    url: z.string().trim().optional(),
    rights_status: z.string().trim().optional(),
    content_hash: z.string().trim().optional(),
    limitations: z.union([z.string(), z.array(z.string())]).optional(),
    retrieved_at: z.string().trim().optional(),
    last_processed_at: z.string().trim().optional()
  })
  .passthrough();

const KnowledgeBaseSchema = z
  .object({
    schema_version: SchemaVersionSchema.optional(),
    id: nonEmptyString,
    title: z.string().trim().optional(),
    evidence: z.array(EvidenceRefSchema).min(1)
  })
  .passthrough();

export const ClaimSchema = KnowledgeBaseSchema.extend({
  statement: nonEmptyString,
  status: z
    .enum([
      "proved",
      "observed",
      "inferred",
      "conjectured",
      "incomplete",
      "contradicted"
    ])
    .default("incomplete"),
  confidence: ConfidenceSchema.optional(),
  scope: z.union([z.string(), z.array(z.string())]).optional(),
  assumptions: z.union([z.string(), z.array(z.string())]).optional()
});

export const MethodSchema = KnowledgeBaseSchema.extend({
  name: nonEmptyString,
  objective: z.string().trim().optional(),
  kernel: z.string().trim().optional(),
  robust_loss: z.string().trim().optional(),
  solver: z.string().trim().optional(),
  boundary_behavior: z.string().trim().optional()
});

export const ExperimentSchema = KnowledgeBaseSchema.extend({
  name: nonEmptyString,
  objective: z.string().trim().optional(),
  observation_model: z.string().trim().optional(),
  contamination_model: z.string().trim().optional(),
  reference_target: z
    .union([z.string().trim(), z.record(z.string(), z.unknown())])
    .optional(),
  seeds: z.array(z.union([z.string(), z.number()])).optional(),
  trial_count: z.number().int().nonnegative().optional()
});

export const RelationshipSchema = KnowledgeBaseSchema.extend({
  source_id: nonEmptyString,
  target_id: nonEmptyString,
  type: z.enum([
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
  ])
});

const Sha256Schema = z.string().regex(/^[a-f0-9]{64}$/);
const AcceptedArtifactUrlSchema = nonEmptyString.refine(
  (value) =>
    value.startsWith("/artifacts/") &&
    !value.startsWith("//") &&
    !/[\s?&#%]/.test(value) &&
    !value.includes("\\") &&
    !value.slice(1).split("/").some((segment) => segment === "" || segment === "." || segment === ".."),
  "Accepted artifact URLs must be safe local /artifacts paths."
);
const BoundPublicationPathSchema = nonEmptyString.refine(
  (value) =>
    /^data\/releases\/release-[a-zA-Z0-9][a-zA-Z0-9._-]*\/publication\//.test(value) &&
    !value.includes("\\") &&
    !value.split("/").some((segment) => segment === "." || segment === ".."),
  "Bound publication paths must stay under a release publication directory."
);

export const AcceptedArtifactFileSchema = z.object({
  url: AcceptedArtifactUrlSchema,
  bound_path: BoundPublicationPathSchema,
  sha256: Sha256Schema,
  bytes: z.number().int().nonnegative()
});

export const AcceptedArtifactManifestSchema = z.object({
  url: AcceptedArtifactUrlSchema,
  bound_path: BoundPublicationPathSchema,
  sha256: Sha256Schema,
  files: z.array(AcceptedArtifactFileSchema).default([])
});

export const AcceptedPublicationSchema = z
  .object({
    release_id: nonEmptyString.regex(/^release-[a-zA-Z0-9][a-zA-Z0-9._-]*$/),
    pulse: nonEmptyString.regex(
      /^content\/pulses\/\d{4}-\d{2}-\d{2}(?:-[1-9]\d{0,3})?\.md$/
    ),
    bound_pulse: BoundPublicationPathSchema,
    pulse_sha256: Sha256Schema,
    binding_sha256: Sha256Schema,
    artifact_manifests: z.array(AcceptedArtifactManifestSchema).default([])
  })
  .superRefine((value, context) => {
    const prefix = `data/releases/${value.release_id}/publication/`;
    const paths = [
      value.bound_pulse,
      ...value.artifact_manifests.flatMap((manifest) => [
        manifest.bound_path,
        ...manifest.files.map((file) => file.bound_path)
      ])
    ];
    if (paths.some((path) => !path.startsWith(prefix))) {
      context.addIssue({
        code: "custom",
        path: ["bound_pulse"],
        message: "Accepted publication paths must match their release id."
      });
    }
  });

export const CurrentReleaseSchema = z
  .object({
    schema_version: SchemaVersionSchema.optional(),
    release_id: nonEmptyString,
    release_path: z.string().trim().optional(),
    updated_at: z.string().trim().optional(),
    published_at: z.string().trim().optional(),
    last_checked_at: z.string().trim().optional(),
    status: z
      .enum([
        "published",
        "processed_no_pulse",
        "unchanged",
        "candidate_selected_pulse",
        "candidate_no_pulse"
      ])
      .optional(),
    pulse: nonEmptyString.nullable().optional(),
    artifact_manifests: StringListSchema.default([]),
    latest_accepted_pulse: nonEmptyString.nullable().optional(),
    accepted_pulses: StringListSchema.default([]),
    accepted_artifact_manifests: StringListSchema.default([]),
    latest_accepted_artifact_manifests: StringListSchema.default([]),
    accepted_publications: z.array(AcceptedPublicationSchema).default([]),
    accepted_publications_sha256: Sha256Schema.optional(),
    bound_pulse: BoundPublicationPathSchema.optional(),
    publication_binding_sha256: Sha256Schema.optional()
  })
  .passthrough()
  .superRefine((value, context) => {
    if (
      ["published", "candidate_selected_pulse"].includes(value.status ?? "") &&
      !value.pulse
    ) {
      context.addIssue({
        code: "custom",
        path: ["pulse"],
        message: "A published pointer must name its selected pulse."
      });
    }
  });

export const ArtifactFileSchema = z
  .object({
    kind: nonEmptyString,
    url: nonEmptyString,
    label: z.string().trim().optional(),
    mime_type: z.string().trim().optional()
  })
  .passthrough();

export const ArtifactSchema = z
  .object({
    schema_version: SchemaVersionSchema.optional(),
    id: nonEmptyString,
    title: nonEmptyString,
    artifact_class: z.enum([
      "scientific_chart",
      "web_image",
      "generated_image",
      "diagram"
    ]),
    caption: nonEmptyString,
    relation_to_report: z.string().trim().optional(),
    stable_url: z.string().trim().optional(),
    rights_status: nonEmptyString,
    creator: z.string().trim().optional(),
    source_url: z.string().trim().optional(),
    retrieved_at: z.string().trim().optional(),
    related_pulse: z.string().trim().optional(),
    files: z.array(ArtifactFileSchema).default([]),
    evidence: z.array(EvidenceRefSchema).default([])
  })
  .passthrough();

export const StageErrorDatumSchema = z.object({
  stage: z.number().int().positive(),
  window: z.number().positive().optional(),
  singlePass: z.number().nonnegative(),
  recursiveExact: z.number().nonnegative(),
  recursiveObserved: z.number().nonnegative().optional()
});

export type PulseFrontmatter = z.infer<typeof PulseFrontmatterSchema>;
export type SourceRecord = z.infer<typeof SourceSchema>;
export type ClaimRecord = z.infer<typeof ClaimSchema>;
export type MethodRecord = z.infer<typeof MethodSchema>;
export type ExperimentRecord = z.infer<typeof ExperimentSchema>;
export type RelationshipRecord = z.infer<typeof RelationshipSchema>;
export type CurrentRelease = z.infer<typeof CurrentReleaseSchema>;
export type ArtifactRecord = z.infer<typeof ArtifactSchema>;
export type ArtifactFile = z.infer<typeof ArtifactFileSchema>;
export type EvidenceRef = z.infer<typeof EvidenceRefSchema>;
export type StageErrorDatum = z.infer<typeof StageErrorDatumSchema>;
