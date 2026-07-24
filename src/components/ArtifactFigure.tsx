import { useEffect, useMemo, useState } from "react";
import {
  artifactCanRenderMedia,
  artifactIsPubliclyCleared,
  loadStageErrorData
} from "../lib/artifacts";
import { formatLocator } from "../lib/format";
import { isExternalHref, safeHref, sourceAnchor, withAppUrl, withBaseUrl } from "../lib/links";
import type { ArtifactRecord, StageErrorDatum } from "../lib/schemas";
import { ScientificChart } from "./ScientificChart";
import { StatusLabel } from "./StatusLabel";
import { StructuralDiagram } from "./StructuralDiagram";

interface ArtifactFigureProps {
  artifact: ArtifactRecord;
  featured?: boolean;
}

function fileMatches(file: ArtifactRecord["files"][number], pattern: RegExp): boolean {
  return pattern.test(`${file.kind} ${file.mime_type ?? ""} ${file.url}`);
}

function displayValue(value: unknown): string | undefined {
  if (typeof value === "string") return value.trim() || undefined;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  if (!value || typeof value !== "object") return undefined;
  if (Array.isArray(value)) {
    const parts = value.map(displayValue).filter((item): item is string => Boolean(item));
    return parts.length > 0 ? parts.join("; ") : undefined;
  }
  const parts = Object.entries(value)
    .map(([key, item]) => {
      const text = displayValue(item);
      return text ? `${key.replace(/_/g, " ")}: ${text}` : undefined;
    })
    .filter((item): item is string => Boolean(item));
  return parts.length > 0 ? parts.join(" · ") : undefined;
}

export function ArtifactFigure({ artifact, featured = false }: ArtifactFigureProps) {
  const [chartData, setChartData] = useState<StageErrorDatum[]>([]);
  const csvFile = useMemo(
    () => artifact.files.find((file) => fileMatches(file, /\bdata\b|csv/i)),
    [artifact.files]
  );
  const imageFile = artifact.files.find((file) => fileMatches(file, /\bimage\b|svg|png|jpe?g/i));
  const specFile = artifact.files.find((file) => fileMatches(file, /\bspec\b|json/i));
  const renderAllowed = artifactCanRenderMedia(artifact);
  const publiclyCleared = artifactIsPubliclyCleared(artifact);
  const limitationsValue = (artifact as ArtifactRecord & { limitations?: unknown }).limitations;
  const limitations = Array.isArray(limitationsValue)
    ? limitationsValue.map(displayValue).filter((item): item is string => Boolean(item))
    : [displayValue(limitationsValue)].filter((item): item is string => Boolean(item));

  useEffect(() => {
    let active = true;
    setChartData([]);
    if (artifact.artifact_class !== "scientific_chart" || !csvFile) return undefined;
    void loadStageErrorData(csvFile.url).then((data) => {
      if (active) setChartData(data);
    });
    return () => {
      active = false;
    };
  }, [artifact.artifact_class, csvFile]);

  const summary =
    artifact.relation_to_report ??
    "The first recursive stage retains substantially more normalized noise energy than later detail stages, while the single-pass reference remains nearly flat.";
  const heading = featured
    ? artifact.artifact_class === "scientific_chart"
      ? "Featured evidence"
      : "Featured visual"
    : artifact.title;
  const Heading = "h2";

  return (
    <section
      className={`artifact-figure${featured ? " artifact-figure--featured" : ""}`}
      id={artifact.id}
      aria-labelledby={`${artifact.id}-heading`}
    >
      <header className="artifact-figure__header">
        <div>
          <p className="eyebrow">{featured ? "Primary visual" : artifact.artifact_class.replace(/_/g, " ")}</p>
          <Heading id={`${artifact.id}-heading`}>{heading}</Heading>
          {featured ? <p className="artifact-figure__title">{artifact.title}</p> : null}
        </div>
        <StatusLabel
          label={publiclyCleared ? "Public reuse cleared" : "Internal view · public reuse not cleared"}
          tone={publiclyCleared ? "accent" : "warning"}
        />
      </header>

      {artifact.artifact_class === "scientific_chart" && renderAllowed ? (
        <ScientificChart
          data={chartData}
          title={artifact.title}
          caption={artifact.caption}
          summary={summary}
          downloads={artifact.files}
          fallbackSvgUrl={imageFile?.url ?? artifact.stable_url}
        />
      ) : null}

      {artifact.artifact_class !== "scientific_chart" && renderAllowed && imageFile ? (
        <figure className="artifact-image">
          {artifact.artifact_class === "diagram" && specFile ? (
            <StructuralDiagram
              specUrl={specFile.url}
              fallbackUrl={imageFile.url}
              caption={artifact.caption}
            />
          ) : (
            <img src={withBaseUrl(imageFile.url)} alt={artifact.caption} loading="lazy" />
          )}
          <figcaption>{artifact.caption}</figcaption>
        </figure>
      ) : null}

      {!renderAllowed ? (
        <div className="artifact-unavailable" role="note">
          Media withheld because its reuse status is not cleared. Provenance remains available below.
        </div>
      ) : null}

      {artifact.artifact_class === "generated_image" ? (
        <p className="generated-label">Conceptual illustration — not research evidence</p>
      ) : null}

      <dl className="artifact-provenance">
        <div>
          <dt>Rights</dt>
          <dd>{artifact.rights_status}</dd>
        </div>
        {artifact.creator ? (
          <div>
            <dt>Creator</dt>
            <dd>{artifact.creator}</dd>
          </div>
        ) : null}
        {artifact.retrieved_at ? (
          <div>
            <dt>Retrieved</dt>
            <dd>{artifact.retrieved_at}</dd>
          </div>
        ) : null}
        {artifact.source_url && safeHref(artifact.source_url) ? (
          <div>
            <dt>Original</dt>
            <dd>
              <a
                href={
                  isExternalHref(artifact.source_url)
                    ? artifact.source_url
                    : withAppUrl(artifact.source_url)
                }
                target={isExternalHref(artifact.source_url) ? "_blank" : undefined}
                rel={isExternalHref(artifact.source_url) ? "noopener noreferrer" : undefined}
              >
                Source record
              </a>
            </dd>
          </div>
        ) : null}
      </dl>

      {limitations.length > 0 ? (
        <details className="artifact-limitations" open={featured || undefined}>
          <summary>Scope and limitations</summary>
          <ul>
            {limitations.map((limitation) => (
              <li key={limitation}>{limitation}</li>
            ))}
          </ul>
        </details>
      ) : null}

      {artifact.evidence.length > 0 ? (
        <details className="artifact-evidence" open={featured || undefined}>
          <summary>Evidence and source locations</summary>
          <ol>
            {artifact.evidence.map((reference, index) => {
              const detail = reference as typeof reference & {
                statement?: unknown;
                status?: unknown;
                confidence?: unknown;
              };
              return (
                <li key={`${reference.source_id}-${index}`}>
                  {displayValue(detail.statement) ? (
                    <strong>{displayValue(detail.statement)}</strong>
                  ) : null}
                  <span>
                    <a href={withAppUrl(sourceAnchor(reference.source_id))}>
                      {reference.source_id}
                    </a>
                    {` · ${formatLocator(reference.locator)}`}
                  </span>
                  {displayValue(detail.status) || displayValue(detail.confidence) ? (
                    <small>
                      {[displayValue(detail.status), displayValue(detail.confidence)]
                        .filter(Boolean)
                        .join(" · ")}
                    </small>
                  ) : null}
                </li>
              );
            })}
          </ol>
        </details>
      ) : null}
    </section>
  );
}
