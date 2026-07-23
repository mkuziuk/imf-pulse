import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { getPulseCatalog } from "../lib/content";
import { getKnowledgeSnapshot } from "../lib/data";
import { formatDate } from "../lib/format";

export function ArchivePage() {
  const snapshot = getKnowledgeSnapshot();
  const catalog = getPulseCatalog(snapshot.current);
  const pulses = catalog.pulses;
  const topics = [...new Set(pulses.flatMap((pulse) => pulse.topics))].sort();
  const [query, setQuery] = useState("");
  const [topic, setTopic] = useState("all");
  const filtered = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    return pulses.filter((pulse) => {
      const matchesQuery =
        !normalizedQuery ||
        `${pulse.title} ${pulse.lead} ${pulse.topics.join(" ")}`
          .toLowerCase()
          .includes(normalizedQuery);
      const matchesTopic = topic === "all" || pulse.topics.includes(topic);
      return matchesQuery && matchesTopic;
    });
  }, [pulses, query, topic]);

  return (
    <section className="index-page page-grid" aria-labelledby="archive-title">
      <div className="page-heading">
        <p className="eyebrow">Daily record</p>
        <h1 id="archive-title">Archive</h1>
        <p>
          {catalog.mode === "preview"
            ? "Hand-reviewed sample reports, segregated from the release archive until a pointer is published."
            : "Pointer-authorized pulses in reverse chronological order. Unchanged days do not create reports."}
        </p>
      </div>
      {catalog.mode !== "authorized" ? (
        <aside className="content-warning" role="note">
          <strong>{catalog.mode === "preview" ? "Preview boundary" : "No material update"}</strong>
          <p>
            {catalog.mode === "preview"
              ? "These reports are local previews and are not represented as published releases."
              : "The latest accepted report is retained while the new release records no pulse."}
          </p>
        </aside>
      ) : null}
      <form className="ledger-toolbar" role="search" onSubmit={(event) => event.preventDefault()}>
        <label>
          <span>Search reports</span>
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Title, lead, or topic"
          />
        </label>
        <label>
          <span>Topic</span>
          <select value={topic} onChange={(event) => setTopic(event.target.value)}>
            <option value="all">All topics</option>
            {topics.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </label>
        <p aria-live="polite">{filtered.length} reports</p>
      </form>

      {filtered.length > 0 ? (
        <ol className="archive-ledger">
          {filtered.map((pulse, index) => (
            <li key={pulse.id}>
              <span className="archive-ledger__number">{String(index + 1).padStart(2, "0")}</span>
              <time dateTime={pulse.date}>{formatDate(pulse.date)}</time>
              <div>
                <h2>
                  <Link to={`/archive/${pulse.date}`}>{pulse.title}</Link>
                </h2>
                <p>{pulse.lead}</p>
              </div>
              <span className="archive-ledger__status">
                {catalog.mode === "preview" ? "preview" : pulse.status}
              </span>
            </li>
          ))}
        </ol>
      ) : (
        <div className="empty-ledger" role="status">
          No pulse matches this search.
        </div>
      )}
    </section>
  );
}
