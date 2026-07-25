import { asText, confidenceLabel, formatLocator } from "../lib/format";
import { directSourceHref } from "../lib/links";
import type { EvidenceRef, SourceRecord } from "../lib/schemas";

interface EvidenceLineProps {
  evidence: EvidenceRef[];
  confidence?: unknown;
  status?: string;
  assumptions?: string | string[];
  compact?: boolean;
  sources?: readonly SourceRecord[];
}

export function EvidenceLine({
  evidence,
  confidence,
  status,
  assumptions,
  compact = false,
  sources = []
}: EvidenceLineProps) {
  return (
    <dl className={`evidence-line${compact ? " evidence-line--compact" : ""}`}>
      <div>
        <dt>Evidence</dt>
        <dd>
          {evidence.length > 0 ? (
            evidence.map((reference, index) => {
              const href = directSourceHref(reference.source_id, sources);
              return (
                <span key={`${reference.source_id}-${index}`}>
                  {index > 0 ? "; " : null}
                  {href ? (
                    <a href={href} target="_blank" rel="noopener noreferrer">
                      {reference.source_id}
                      <span className="external-mark" aria-label=" (external link)">↗</span>
                    </a>
                  ) : (
                    <span>{reference.source_id}</span>
                  )}
                  <span> · {formatLocator(reference.locator)}</span>
                </span>
              );
            })
          ) : (
            <span>Not registered</span>
          )}
        </dd>
      </div>
      {status ? (
        <div>
          <dt>Status</dt>
          <dd>{status.replace(/_/g, " ")}</dd>
        </div>
      ) : null}
      {confidence != null ? (
        <div>
          <dt>Confidence</dt>
          <dd>{confidenceLabel(confidence)}</dd>
        </div>
      ) : null}
      {assumptions ? (
        <div>
          <dt>Limits</dt>
          <dd>{asText(assumptions)}</dd>
        </div>
      ) : null}
    </dl>
  );
}
