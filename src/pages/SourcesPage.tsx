import { useEffect, useMemo, useState } from "react";
import { useLocation } from "react-router-dom";
import { StatusLabel } from "../components/StatusLabel";
import { asText, formatLocator } from "../lib/format";
import { getKnowledgeSnapshot } from "../lib/data";
import type { EvidenceRef } from "../lib/schemas";

function sourceReferences(snapshot: ReturnType<typeof getKnowledgeSnapshot>) {
  const evidence = [
    ...snapshot.claims.flatMap((record) => record.evidence),
    ...snapshot.methods.flatMap((record) => record.evidence),
    ...snapshot.experiments.flatMap((record) => record.evidence),
    ...snapshot.relationships.flatMap((record) => record.evidence)
  ];
  const grouped = new Map<string, EvidenceRef[]>();
  for (const reference of evidence) {
    grouped.set(reference.source_id, [...(grouped.get(reference.source_id) ?? []), reference]);
  }
  return [...grouped.entries()].sort(([a], [b]) => a.localeCompare(b));
}

export function SourcesPage() {
  const snapshot = getKnowledgeSnapshot();
  const location = useLocation();
  const [query, setQuery] = useState("");
  const [sourceType, setSourceType] = useState("all");
  const types = [...new Set(snapshot.sources.map((source) => source.source_type).filter(Boolean))].sort();
  const filtered = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return snapshot.sources.filter((source) => {
      const haystack = `${source.id} ${source.title} ${source.authors.join(" ")} ${source.topics.join(" ")}`.toLowerCase();
      return (
        (!normalized || haystack.includes(normalized)) &&
        (sourceType === "all" || source.source_type === sourceType)
      );
    });
  }, [query, snapshot.sources, sourceType]);
  const unresolvedReferences = sourceReferences(snapshot);

  useEffect(() => {
    if (!location.hash) return;
    let id: string;
    try {
      id = decodeURIComponent(location.hash.slice(1));
    } catch {
      return;
    }
    if (!id) return;
    const frame = requestAnimationFrame(() => {
      const target = document.getElementById(id);
      target?.focus({ preventScroll: true });
      target?.scrollIntoView({ block: "center" });
    });
    return () => cancelAnimationFrame(frame);
  }, [location.hash]);

  return (
    <section className="sources-page page-grid" aria-labelledby="sources-title">
      <div className="page-heading page-heading--wide">
        <p className="eyebrow">Provenance ledger</p>
        <h1 id="sources-title">Sources</h1>
        <p>Authority, publication state, rights, hashes, processing status, and known limitations.</p>
        <StatusLabel
          label={snapshot.state === "ready" ? "Validated release" : `${snapshot.state} source view`}
          tone={snapshot.state === "ready" ? "accent" : "warning"}
        />
      </div>

      <form className="ledger-toolbar" role="search" onSubmit={(event) => event.preventDefault()}>
        <label>
          <span>Search sources</span>
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="ID, title, author, topic"
          />
        </label>
        <label>
          <span>Source class</span>
          <select value={sourceType} onChange={(event) => setSourceType(event.target.value)}>
            <option value="all">All classes</option>
            {types.map((type) => (
              <option key={type} value={type}>
                {type?.replace(/_/g, " ")}
              </option>
            ))}
          </select>
        </label>
        <p aria-live="polite">{filtered.length} sources</p>
      </form>

      {snapshot.state === "unavailable" ? (
        <div className="empty-ledger" role="status">
          Validated research release unavailable. {snapshot.reason}
        </div>
      ) : null}

      {filtered.length > 0 ? (
        <div className="source-table-wrap table-scroll" tabIndex={0} role="region" aria-label="Source registry">
          <table className="source-table">
            <thead>
              <tr>
                <th scope="col">Source</th>
                <th scope="col">Class / authority</th>
                <th scope="col">Publication</th>
                <th scope="col">Rights</th>
                <th scope="col">Processed</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((source) => (
                <tr
                  key={source.id}
                  id={source.id}
                  tabIndex={-1}
                  aria-label={`${source.title}, source ${source.id}`}
                >
                  <th scope="row">
                    <span className="source-id">{source.id}</span>
                    <strong>{source.title}</strong>
                    {source.authors.length > 0 ? <span>{source.authors.join(", ")}</span> : null}
                    <details>
                      <summary>Provenance and limitations</summary>
                      <dl>
                        <div>
                          <dt>Location</dt>
                          <dd>{source.location ?? source.url ?? "Not released"}</dd>
                        </div>
                        <div>
                          <dt>Content hash</dt>
                          <dd className="hash-value">{source.content_hash ?? "Not recorded"}</dd>
                        </div>
                        <div>
                          <dt>Limitations</dt>
                          <dd>{asText(source.limitations) ?? "None recorded"}</dd>
                        </div>
                      </dl>
                    </details>
                  </th>
                  <td data-label="Class / authority">{[source.source_type, source.authority_level].filter(Boolean).join(" · ") || "—"}</td>
                  <td data-label="Publication">{source.publication_status ?? "—"}</td>
                  <td data-label="Rights">{source.rights_status ?? "Unknown"}</td>
                  <td data-label="Processed">{source.last_processed_at ?? "Not processed"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      {snapshot.sources.length === 0 && unresolvedReferences.length > 0 ? (
        <section className="reference-register" aria-labelledby="reference-register-title">
          <h2 id="reference-register-title">Evidence references awaiting a released source registry</h2>
          <p>
            These identifiers and locators occur in reviewed knowledge records. They are not a substitute for complete source metadata.
          </p>
          <ol>
            {unresolvedReferences.map(([sourceId, references]) => (
              <li
                key={sourceId}
                id={sourceId}
                tabIndex={-1}
                aria-label={`Evidence source ${sourceId}, ${references.length} references`}
              >
                <strong>{sourceId}</strong>
                <span>{references.length} evidence link(s)</span>
                <ul>
                  {references.slice(0, 3).map((reference, index) => (
                    <li key={`${sourceId}-${index}`}>{formatLocator(reference.locator)}</li>
                  ))}
                </ul>
              </li>
            ))}
          </ol>
        </section>
      ) : null}

      {snapshot.state !== "unavailable" && snapshot.sources.length === 0 && unresolvedReferences.length === 0 ? (
        <div className="empty-ledger" role="status">
          No source registry has been released yet.
        </div>
      ) : null}
    </section>
  );
}
