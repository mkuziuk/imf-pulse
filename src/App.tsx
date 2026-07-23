import { useEffect, useRef, useState } from "react";
import { Route, Routes, useLocation } from "react-router-dom";
import { SiteHeader } from "./components/SiteHeader";
import { getKnowledgeSnapshot, type KnowledgeSnapshot } from "./lib/data";
import { formatTimestamp } from "./lib/format";
import { ArchivePage } from "./pages/ArchivePage";
import { ArtifactsPage } from "./pages/ArtifactsPage";
import { LatestPage } from "./pages/LatestPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { ResearchMapPage } from "./pages/ResearchMapPage";
import { SourcesPage } from "./pages/SourcesPage";

export function releaseCheckLabel(snapshot: KnowledgeSnapshot): string {
  const current = snapshot.current;
  const checked = formatTimestamp(
    current?.last_checked_at ?? current?.updated_at ?? current?.published_at
  );
  if (checked) return `Release checked ${checked}`;
  if (current && snapshot.state === "ready") return "Validated release pointer";
  if (current) return "Release preview";
  return "No validated release pointer";
}

function SiteFooter() {
  const snapshot = getKnowledgeSnapshot();
  return (
    <footer className="site-footer">
      <p>
        <strong>The Residual</strong>
        <span>Local-first research intelligence</span>
      </p>
      <p>
        <span>External monitoring off</span>
        <span>{releaseCheckLabel(snapshot)}</span>
      </p>
    </footer>
  );
}

function RouteTransitionManager() {
  const location = useLocation();
  const initialRender = useRef(true);
  const [announcement, setAnnouncement] = useState("");

  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      const main = document.getElementById("main-content");
      const heading = main?.querySelector("h1")?.textContent?.trim() || "Research";
      document.title = `${heading} — The Residual`;
      setAnnouncement(`${heading} page`);

      if (initialRender.current) {
        initialRender.current = false;
        return;
      }

      main?.focus({ preventScroll: true });
      if (!location.hash) window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    });
    return () => cancelAnimationFrame(frame);
  }, [location.hash, location.pathname, location.search]);

  return (
    <span className="visually-hidden" role="status" aria-live="polite" aria-atomic="true">
      {announcement}
    </span>
  );
}

export function App() {
  return (
    <div className="app-shell">
      <SiteHeader />
      <RouteTransitionManager />
      <main id="main-content" tabIndex={-1}>
        <Routes>
          <Route path="/" element={<LatestPage />} />
          <Route path="/archive" element={<ArchivePage />} />
          <Route path="/archive/:date" element={<LatestPage />} />
          <Route path="/research-map" element={<ResearchMapPage />} />
          <Route path="/artifacts" element={<ArtifactsPage />} />
          <Route path="/sources" element={<SourcesPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Routes>
      </main>
      <SiteFooter />
    </div>
  );
}
