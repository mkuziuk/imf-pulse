import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { App, releaseCheckLabel } from "./App";
import type { KnowledgeSnapshot } from "./lib/data";

function renderRoute(route: string) {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <App />
    </MemoryRouter>
  );
}

describe("application routes", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("not found", { status: 404 }))
    );
    vi.stubGlobal("scrollTo", vi.fn());
  });

  it.each([
    ["/archive", "Archive"],
    ["/research-map", "Research map"],
    ["/artifacts", "Artifacts"],
    ["/sources", "Sources"],
    ["/does-not-exist", "This research path does not exist."]
  ])("renders %s with one primary heading", (route, heading) => {
    renderRoute(route);
    const main = screen.getByRole("main");
    expect(within(main).getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(within(main).getByRole("heading", { level: 1, name: heading })).toBeVisible();
  });

  it("renders the latest authorized state with one primary heading", () => {
    renderRoute("/");
    const headings = within(screen.getByRole("main")).getAllByRole("heading", { level: 1 });
    expect(headings).toHaveLength(1);
    expect(headings[0]).toBeVisible();
    expect(headings[0]).toHaveTextContent(/The Calligraphic Gap|No validated pulse has been published/i);
  });

  it("does not call a timestamp-free validated build an absent pointer", () => {
    const snapshot = {
      state: "ready",
      current: { release_id: "release-a368602893dffadcd400" }
    } as KnowledgeSnapshot;

    expect(releaseCheckLabel(snapshot)).toBe("Validated release pointer");
  });

  it("opens the current report directly and navigates to the archive", async () => {
    const user = userEvent.setup();
    renderRoute("/");
    expect(within(screen.getByRole("main")).getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.queryByText(/get started|learn more/i)).not.toBeInTheDocument();

    await user.click(screen.getByRole("link", { name: "Archive" }));
    expect(screen.getByRole("heading", { level: 1, name: "Archive" })).toBeVisible();
    expect(screen.getByRole("link", { name: "Archive" })).toHaveAttribute("aria-current", "page");
    await waitFor(() => {
      expect(document.title).toBe("Archive — The Residual");
      expect(document.activeElement).toBe(screen.getByRole("main"));
      expect(window.scrollTo).toHaveBeenCalledWith({ top: 0, left: 0, behavior: "auto" });
    });
  });

  it("focuses a cited source when opening a source hash", async () => {
    renderRoute("/sources#src-imf-draft");
    const source = document.getElementById("src-imf-draft");
    expect(source).not.toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(source));
    expect(source?.scrollIntoView).toHaveBeenCalledWith({ block: "center" });
  });

  it("ignores a malformed source hash without crashing", () => {
    renderRoute("/sources#%");

    expect(screen.getByRole("heading", { level: 1, name: "Sources" })).toBeVisible();
    expect(screen.getByRole("main")).toBeVisible();
  });

  it("provides a semantic accessibility path on every page", () => {
    renderRoute("/research-map");
    expect(screen.getByRole("link", { name: "Skip to research report" })).toHaveAttribute(
      "href",
      "#main-content"
    );
    expect(screen.getByRole("banner")).toBeVisible();
    expect(screen.getByRole("navigation", { name: "Primary" })).toBeVisible();
    expect(screen.getByRole("main")).toHaveAttribute("id", "main-content");
    expect(screen.getByRole("contentinfo")).toBeVisible();
  });
});
