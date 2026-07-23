import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { getKnowledgeSnapshot } from "../lib/data";
import { ResearchMapPage } from "./ResearchMapPage";

function renderPage() {
  return render(
    <MemoryRouter>
      <ResearchMapPage />
    </MemoryRouter>
  );
}

describe("ResearchMapPage registers", () => {
  it("lists every method and experiment, including records outside the relationship view", () => {
    const snapshot = getKnowledgeSnapshot();
    renderPage();

    const methods = screen.getByRole("region", { name: "Method register" });
    const experiments = screen.getByRole("region", { name: "Experiment register" });

    expect(within(methods).getAllByRole("listitem")).toHaveLength(snapshot.methods.length);
    expect(within(experiments).getAllByRole("listitem")).toHaveLength(snapshot.experiments.length);
    expect(
      within(methods).getByRole("heading", {
        name: "Parametric-bootstrap robust stage scaling"
      })
    ).toBeVisible();
    expect(
      within(experiments).getByRole("heading", {
        name: "Canonical real-vs-calculated component comparison"
      })
    ).toBeVisible();
  });

  it("exposes method and experiment parameters with precise evidence", () => {
    renderPage();

    const methodHeading = screen.getByRole("heading", {
      name: "Robust gradient-descent recursive IMF/IRMF"
    });
    const method = methodHeading.closest("li");
    expect(method).not.toBeNull();
    expect(within(method!).getByText("Objective", { selector: "dt" })).toBeVisible();
    expect(within(method!).getByText("Estimator", { selector: "dt" })).toBeVisible();
    expect(within(method!).getByText("Kernel", { selector: "dt" })).toBeVisible();
    expect(within(method!).getByText("Robust loss", { selector: "dt" })).toBeVisible();
    expect(within(method!).getByText("Solver", { selector: "dt" })).toBeVisible();
    expect(within(method!).getByText("Boundary", { selector: "dt" })).toBeVisible();
    expect(within(method!).getByText("Parameters", { selector: "dt" })).toBeVisible();
    expect(within(method!).getByText("Computational assumptions", { selector: "dt" })).toBeVisible();
    expect(within(method!).getByRole("link", { name: "src-repo-readme" })).toBeVisible();

    const experimentHeading = screen.getByRole("heading", {
      name: "Robust recursive Monte Carlo with and without contamination"
    });
    const experiment = experimentHeading.closest("li");
    expect(experiment).not.toBeNull();
    for (const label of [
      "Observation",
      "Contamination",
      "Signal configuration",
      "Reference target",
      "Seeds",
      "Trials",
      "Robustness",
      "Window sequence",
      "Metrics",
      "Outputs"
    ]) {
      expect(within(experiment!).getByText(label, { selector: "dt" })).toBeVisible();
    }
    expect(within(experiment!).getByRole("link", { name: "src-robust-monte-carlo" })).toBeVisible();
  });
});
