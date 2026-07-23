import { Link } from "react-router-dom";
import { asText, confidenceLabel, formatLocator } from "../lib/format";
import { sourceAnchor } from "../lib/links";
import type { EvidenceRef } from "../lib/schemas";

interface EvidenceLineProps {
  evidence: EvidenceRef[];
  confidence?: unknown;
  status?: string;
  assumptions?: string | string[];
  compact?: boolean;
}

export function EvidenceLine({
  evidence,
  confidence,
  status,
  assumptions,
  compact = false
}: EvidenceLineProps) {
  return (
    <dl className={`evidence-line${compact ? " evidence-line--compact" : ""}`}>
      <div>
        <dt>Evidence</dt>
        <dd>
          {evidence.length > 0 ? (
            evidence.map((reference, index) => (
              <span key={`${reference.source_id}-${index}`}>
                {index > 0 ? "; " : null}
                <Link to={sourceAnchor(reference.source_id)}>{reference.source_id}</Link>
                <span> · {formatLocator(reference.locator)}</span>
              </span>
            ))
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
