import { useMemo } from "react";
import { Link, useParams } from "react-router-dom";
import { ArtifactFigure } from "../components/ArtifactFigure";
import { MarkdownRenderer } from "../components/MarkdownRenderer";
import { StatusLabel } from "../components/StatusLabel";
import { useArtifacts } from "../hooks/useArtifacts";
import { manifestUrlsForPulse } from "../lib/artifacts";
import { getPulseCatalog } from "../lib/content";
import { getKnowledgeSnapshot } from "../lib/data";
import { formatDate } from "../lib/format";

export function LatestPage() {
  const { date, pulseIndex } = useParams<{ date?: string; pulseIndex?: string }>();
  const knowledge = getKnowledgeSnapshot();
  const catalog = getPulseCatalog(knowledge.current);
  const pulse = date
    ? catalog.pulses.find(
        (candidate) =>
          candidate.date === date &&
          (pulseIndex == null || candidate.pulseIndex === Number(pulseIndex))
      )
    : catalog.latest;
  const manifestUrls = useMemo(
    () =>
      pulse
        ? manifestUrlsForPulse(pulse, knowledge.current, catalog.mode)
        : [],
    [catalog.mode, knowledge.current, pulse]
  );
  const artifactState = useArtifacts(manifestUrls);

  if (!pulse) {
    return (
      <section className="empty-page page-grid" aria-labelledby="empty-latest-title">
        <div className="empty-page__content">
          <p className="eyebrow">Latest pulse</p>
          <h1 id="empty-latest-title">No validated pulse has been published.</h1>
          <p>
            The research shell is ready. A report will appear here only after its sources,
            citations, artifacts, tests, and production build pass validation.
          </p>
          <Link className="text-link" to="/archive">
            Browse the archive
          </Link>
        </div>
      </section>
    );
  }

  const featuredArtifact =
    pulse.featuredArtifact
      ? artifactState.artifacts.find((artifact) => artifact.id === pulse.featuredArtifact)
      : undefined;
  const supportingArtifacts = artifactState.artifacts.filter(
    (artifact) => artifact.id !== featuredArtifact?.id
  );
  const candidateBuild = knowledge.current?.status?.startsWith("candidate_") === true;
  const publicationLabel =
    candidateBuild
      ? "Candidate preview"
      : catalog.mode === "preview"
      ? "Unreleased preview"
      : catalog.mode === "retained"
        ? "Checked · no material change"
        : "Validated report";

  return (
    <article className="pulse page-grid" aria-labelledby="pulse-title">
      <header className="pulse-header">
        <div className="pulse-header__meta">
          <p className="eyebrow">{date ? "Archive pulse" : "Latest pulse"}</p>
          <time dateTime={pulse.date}>{formatDate(pulse.date)}</time>
          <span>Pulse {pulse.pulseIndex}</span>
          <StatusLabel
            label={publicationLabel}
            tone={catalog.mode === "authorized" && !candidateBuild ? "accent" : "warning"}
          />
        </div>
        <div className="pulse-header__body">
          <h1 id="pulse-title">{pulse.title}</h1>
          <p className="pulse-lead">{pulse.lead}</p>
          {pulse.readerGuide ? (
            <aside className="reader-orientation" aria-label="Reader orientation">
              <p className="eyebrow">A quick orientation</p>
              <p>{pulse.readerGuide}</p>
            </aside>
          ) : null}
          {pulse.topics.length > 0 ? (
            <ul className="topic-list" aria-label="Report topics">
              {pulse.topics.map((topic) => (
                <li key={topic}>{topic}</li>
              ))}
            </ul>
          ) : null}
        </div>
        <nav className="pulse-index" aria-label="Report structure">
          <span>In this pulse</span>
          <a href="#signal-01">Signals</a>
          <a href="#why-this-matters">Synthesis</a>
          <a href="#unresolved-question">Open question</a>
        </nav>
      </header>

      {featuredArtifact ? (
        <div className="pulse-featured-figure">
          <ArtifactFigure artifact={featuredArtifact} sources={knowledge.sources} featured />
        </div>
      ) : (
        <section className="pulse-featured-figure artifact-loading" aria-live="polite">
          <p className="eyebrow">Dominant figure</p>
          <h2>{artifactState.loading ? "Loading validated artifact…" : "Artifact unavailable"}</h2>
          <p>
            {artifactState.loading
              ? "The report text remains available while its local manifest is checked."
              : "The report remains valid without the figure; its manifest could not be loaded."}
          </p>
        </section>
      )}

      {supportingArtifacts.length > 0 ? (
        <section className="pulse-supporting-figures" aria-labelledby="supporting-visuals-title">
          <header className="pulse-supporting-figures__header">
            <p className="eyebrow">Supporting visuals</p>
            <h2 id="supporting-visuals-title">Additional views of the topic</h2>
          </header>
          {supportingArtifacts.map((artifact) => (
            <ArtifactFigure key={artifact.id} artifact={artifact} sources={knowledge.sources} />
          ))}
        </section>
      ) : null}

      {pulse.issues.length > 0 ? (
        <aside className="content-warning" role="note" aria-label="Pulse validation notes">
          <strong>Preview notes</strong>
          <ul>
            {pulse.issues.map((issue) => (
              <li key={issue}>{issue}</li>
            ))}
          </ul>
        </aside>
      ) : null}

      <MarkdownRenderer markdown={pulse.body} className="pulse-body" sources={knowledge.sources} />
    </article>
  );
}
