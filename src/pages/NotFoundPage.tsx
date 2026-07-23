import { Link } from "react-router-dom";

export function NotFoundPage() {
  return (
    <section className="empty-page page-grid" aria-labelledby="not-found-title">
      <div className="empty-page__content">
        <p className="eyebrow">404 · No signal</p>
        <h1 id="not-found-title">This research path does not exist.</h1>
        <p>The archive and evidence ledgers may have the source or report you intended.</p>
        <Link className="text-link" to="/">
          Return to the latest pulse
        </Link>
      </div>
    </section>
  );
}
