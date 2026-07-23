import { useMemo, useState } from "react";
import { EvidenceLine } from "../components/EvidenceLine";
import { StatusLabel } from "../components/StatusLabel";
import { asText } from "../lib/format";
import { getKnowledgeLabel, getKnowledgeSnapshot } from "../lib/data";
import type { RelationshipRecord } from "../lib/schemas";

interface RegisterField {
  label: string;
  value: unknown;
}

function registerValue(value: unknown): string | undefined {
  if (typeof value === "string") return value.trim() || undefined;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (Array.isArray(value)) {
    if (value.length === 0) return "None registered";
    const entries = value.map(registerValue).filter((item): item is string => Boolean(item));
    return entries.length > 0 ? entries.join("; ") : undefined;
  }
  if (!value || typeof value !== "object") return undefined;

  const entries = Object.entries(value)
    .map(([key, item]) => {
      const text = registerValue(item);
      const label = key.replace(/_/g, " ");
      return text ? `${label}: ${text}` : undefined;
    })
    .filter((item): item is string => Boolean(item));
  return entries.length > 0 ? entries.join(" · ") : undefined;
}

function RegisterDetails({ fields }: { fields: RegisterField[] }) {
  const availableFields = fields
    .map((field) => ({ ...field, text: registerValue(field.value) }))
    .filter((field): field is RegisterField & { text: string } => Boolean(field.text));

  if (availableFields.length === 0) return null;

  return (
    <dl className="knowledge-register__details">
      {availableFields.map((field) => (
        <div key={field.label}>
          <dt>{field.label}</dt>
          <dd>{field.text}</dd>
        </div>
      ))}
    </dl>
  );
}

function targetDescription(value: unknown): string | undefined {
  if (typeof value === "string") return value;
  if (!value || typeof value !== "object") return undefined;
  const target = value as Record<string, unknown>;
  const parts = [target.kind, target.description, target.definition, target.label]
    .filter((item): item is string => typeof item === "string" && item.trim().length > 0)
    .map((item) => item.trim());
  return parts.length > 0 ? parts.join(" · ") : JSON.stringify(target);
}

function RelationshipInspector({
  relationship,
  snapshot
}: {
  relationship: RelationshipRecord;
  snapshot: ReturnType<typeof getKnowledgeSnapshot>;
}) {
  const source = getKnowledgeLabel(snapshot, relationship.source_id);
  const target = getKnowledgeLabel(snapshot, relationship.target_id);
  const experiment = snapshot.experiments.find(
    (record) => record.id === relationship.source_id || record.id === relationship.target_id
  );

  return (
    <aside className="knowledge-inspector" aria-labelledby="relationship-inspector-title">
      <p className="eyebrow">Selected relationship</p>
      <h2 id="relationship-inspector-title">{relationship.type.replace(/-/g, " ")}</h2>
      <p className="knowledge-inspector__statement">
        <strong>{source.title}</strong> {relationship.type.replace(/-/g, " ")} <strong>{target.title}</strong>
      </p>
      {relationship.title ? <p>{relationship.title}</p> : null}
      <dl className="knowledge-inspector__types">
        <div>
          <dt>From</dt>
          <dd>{source.kind}</dd>
        </div>
        <div>
          <dt>To</dt>
          <dd>{target.kind}</dd>
        </div>
      </dl>
      {experiment?.reference_target ? (
        <div className="knowledge-inspector__target">
          <h3>Reference target</h3>
          <p>{targetDescription(experiment.reference_target)}</p>
        </div>
      ) : null}
      <EvidenceLine evidence={relationship.evidence} compact />
    </aside>
  );
}

export function ResearchMapPage() {
  const snapshot = getKnowledgeSnapshot();
  const [relationFilter, setRelationFilter] = useState("all");
  const visibleRelationships = useMemo(
    () =>
      snapshot.relationships.filter(
        (relationship) => relationFilter === "all" || relationship.type === relationFilter
      ),
    [relationFilter, snapshot.relationships]
  );
  const [selectedId, setSelectedId] = useState<string>();
  const selected =
    visibleRelationships.find((relationship) => relationship.id === selectedId) ??
    visibleRelationships[0];
  const relationTypes = [...new Set(snapshot.relationships.map((item) => item.type))].sort();

  return (
    <section className="map-page page-grid" aria-labelledby="research-map-title">
      <div className="page-heading page-heading--wide">
        <p className="eyebrow">Claims · methods · experiments</p>
        <h1 id="research-map-title">Research map</h1>
        <p>Contradictions and competing targets stay visible; relationships are never collapsed into consensus.</p>
        <StatusLabel
          label={snapshot.state === "ready" ? "Validated release" : `${snapshot.state} knowledge view`}
          tone={snapshot.state === "ready" ? "accent" : "warning"}
        />
      </div>

      {snapshot.state === "unavailable" ? (
        <div className="empty-ledger" role="status">
          Validated research release unavailable. {snapshot.reason}
        </div>
      ) : null}

      {snapshot.state !== "unavailable" && snapshot.relationships.length > 0 ? (
        <>
          <div className="map-toolbar">
            <label>
              <span>Relationship</span>
              <select value={relationFilter} onChange={(event) => setRelationFilter(event.target.value)}>
                <option value="all">All relationships</option>
                {relationTypes.map((type) => (
                  <option key={type} value={type}>
                    {type.replace(/-/g, " ")}
                  </option>
                ))}
              </select>
            </label>
            <p aria-live="polite">{visibleRelationships.length} connections</p>
          </div>
          <div className="research-map-layout">
            <div className="relationship-view" aria-label="Knowledge relationships">
              <div className="relationship-view__head" aria-hidden="true">
                <span>Origin</span>
                <span>Relationship</span>
                <span>Destination</span>
              </div>
              <ol>
                {visibleRelationships.map((relationship) => {
                  const source = getKnowledgeLabel(snapshot, relationship.source_id);
                  const target = getKnowledgeLabel(snapshot, relationship.target_id);
                  const active = selected?.id === relationship.id;
                  return (
                    <li key={relationship.id} data-active={active ? "true" : "false"}>
                      <button
                        type="button"
                        onClick={() => setSelectedId(relationship.id)}
                        aria-pressed={active}
                        aria-label={`${source.title} ${relationship.type} ${target.title}`}
                      >
                        <span className="relationship-node">
                          <small>{source.kind}</small>
                          <strong>{source.title}</strong>
                        </span>
                        <span className="relationship-edge" data-relation={relationship.type}>
                          {relationship.type.replace(/-/g, " ")}
                        </span>
                        <span className="relationship-node">
                          <small>{target.kind}</small>
                          <strong>{target.title}</strong>
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ol>
            </div>
            {selected ? <RelationshipInspector relationship={selected} snapshot={snapshot} /> : null}
          </div>
        </>
      ) : null}

      {snapshot.state !== "unavailable" && snapshot.relationships.length === 0 ? (
        <div className="empty-ledger" role="status">
          No reviewed relationships have been released yet.
        </div>
      ) : null}

      <section className="knowledge-register" aria-labelledby="claim-register-title">
        <h2 id="claim-register-title">Claim register</h2>
        {snapshot.claims.length > 0 ? (
          <ol>
            {snapshot.claims.map((claim) => (
              <li key={claim.id}>
                <div>
                  <StatusLabel label={claim.status} tone={claim.status === "contradicted" ? "warning" : "muted"} />
                  <h3>{claim.statement}</h3>
                  {claim.scope ? <p>Scope: {asText(claim.scope)}</p> : null}
                </div>
                <EvidenceLine
                  evidence={claim.evidence}
                  confidence={claim.confidence}
                  assumptions={claim.assumptions}
                  compact
                />
              </li>
            ))}
          </ol>
        ) : (
          <p>No reviewed claims available.</p>
        )}
      </section>

      <section className="knowledge-register" aria-labelledby="method-register-title">
        <h2 id="method-register-title">Method register</h2>
        {snapshot.methods.length > 0 ? (
          <ol>
            {snapshot.methods.map((method) => (
              <li key={method.id} id={method.id} aria-labelledby={`${method.id}-title`}>
                <div className="knowledge-register__record">
                  <h3 id={`${method.id}-title`}>{method.name}</h3>
                  <RegisterDetails
                    fields={[
                      { label: "Objective", value: method.objective },
                      { label: "Estimator", value: method.estimator },
                      { label: "Kernel", value: method.kernel },
                      { label: "Robust loss", value: method.robust_loss },
                      { label: "Solver", value: method.solver },
                      { label: "Boundary", value: method.boundary_behavior },
                      { label: "Parameters", value: method.parameters },
                      {
                        label: "Computational assumptions",
                        value: method.computational_assumptions
                      }
                    ]}
                  />
                </div>
                <EvidenceLine evidence={method.evidence} compact />
              </li>
            ))}
          </ol>
        ) : (
          <p>No reviewed methods available.</p>
        )}
      </section>

      <section className="knowledge-register" aria-labelledby="experiment-register-title">
        <h2 id="experiment-register-title">Experiment register</h2>
        {snapshot.experiments.length > 0 ? (
          <ol>
            {snapshot.experiments.map((experiment) => (
              <li key={experiment.id} id={experiment.id} aria-labelledby={`${experiment.id}-title`}>
                <div className="knowledge-register__record">
                  <h3 id={`${experiment.id}-title`}>{experiment.name}</h3>
                  <RegisterDetails
                    fields={[
                      { label: "Objective", value: experiment.objective },
                      { label: "Observation", value: experiment.observation_model },
                      { label: "Contamination", value: experiment.contamination_model },
                      { label: "Signal configuration", value: experiment.signal_configuration },
                      {
                        label: "Reference target",
                        value: targetDescription(experiment.reference_target)
                      },
                      { label: "Seeds", value: experiment.seeds },
                      { label: "Trials", value: experiment.trial_count },
                      { label: "Robustness", value: experiment.robustness_parameter },
                      { label: "Window sequence", value: experiment.window_sequence },
                      { label: "Metrics", value: experiment.metrics },
                      { label: "Outputs", value: experiment.outputs }
                    ]}
                  />
                </div>
                <EvidenceLine evidence={experiment.evidence} compact />
              </li>
            ))}
          </ol>
        ) : (
          <p>No reviewed experiments available.</p>
        )}
      </section>
    </section>
  );
}
