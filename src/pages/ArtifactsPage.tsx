import { useState } from "react";
import { ArtifactFigure } from "../components/ArtifactFigure";
import { StatusLabel } from "../components/StatusLabel";
import { useArtifacts } from "../hooks/useArtifacts";
import { getArtifactManifestCatalog } from "../lib/artifacts";

export function ArtifactsPage() {
  const catalog = getArtifactManifestCatalog();
  const state = useArtifacts(catalog.manifestUrls);
  const [artifactClass, setArtifactClass] = useState("all");
  const classes = [...new Set(state.artifacts.map((artifact) => artifact.artifact_class))].sort();
  const visible = state.artifacts.filter(
    (artifact) => artifactClass === "all" || artifact.artifact_class === artifactClass
  );

  return (
    <section className="artifacts-page page-grid" aria-labelledby="artifacts-title">
      <div className="page-heading">
        <p className="eyebrow">Figures · data · specifications</p>
        <h1 id="artifacts-title">Artifacts</h1>
        <p>
          Stable local outputs with their data, provenance, relationship to the report, and reuse
          status.
        </p>
        <StatusLabel
          label={
            catalog.mode === "preview"
              ? "Unreleased preview artifacts"
              : catalog.mode === "retained"
                ? "Latest accepted artifacts retained"
                : "Pointer-authorized artifacts"
          }
          tone={catalog.mode === "authorized" ? "accent" : "warning"}
        />
      </div>
      {catalog.mode === "preview" ? (
        <aside className="content-warning" role="note">
          <strong>Preview boundary</strong>
          <p>These manifests belong to local sample reports and are not presented as a published release.</p>
        </aside>
      ) : null}
      <div className="ledger-toolbar">
        <label>
          <span>Artifact class</span>
          <select value={artifactClass} onChange={(event) => setArtifactClass(event.target.value)}>
            <option value="all">All classes</option>
            {classes.map((value) => (
              <option key={value} value={value}>
                {value.replace(/_/g, " ")}
              </option>
            ))}
          </select>
        </label>
        <p aria-live="polite">{state.loading ? "Checking manifests…" : `${visible.length} artifacts`}</p>
      </div>
      {visible.length > 0 ? (
        <div className="artifact-ledger">
          {visible.map((artifact) => (
            <ArtifactFigure key={artifact.id} artifact={artifact} />
          ))}
        </div>
      ) : !state.loading ? (
        <div className="empty-ledger" role="status">
          No validated artifacts are available for this filter.
        </div>
      ) : null}
      {state.issues.length > 0 ? (
        <details className="load-issues">
          <summary>{state.issues.length} manifest issue(s)</summary>
          <ul>
            {state.issues.map((issue) => (
              <li key={issue}>{issue}</li>
            ))}
          </ul>
        </details>
      ) : null}
    </section>
  );
}
