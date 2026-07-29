import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const plotMocks = vi.hoisted(() => ({
  plot: vi.fn(() => {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.append(document.createElementNS("http://www.w3.org/2000/svg", "path"));
    return svg;
  })
}));

vi.mock("@observablehq/plot", () => ({
  plot: plotMocks.plot,
  ruleY: vi.fn(() => ({})),
  line: vi.fn(() => ({})),
  dot: vi.fn(() => ({})),
  text: vi.fn(() => ({}))
}));

import { ScientificChart } from "./ScientificChart";

const data = [
  { stage: 1, window: 501, singlePass: 0.02018, recursiveExact: 0.02018, recursiveObserved: 0.02025 },
  { stage: 2, window: 355, singlePass: 0.02017, recursiveExact: 0.00828, recursiveObserved: 0.00749 }
];

describe("ScientificChart", () => {
  it("renders a responsive plot while retaining an exact table", async () => {
    render(
      <ScientificChart
        data={data}
        title="Stage error comparison"
        caption="Exact and observed normalized errors."
        summary="Stage one remains higher."
      />
    );
    await waitFor(() => expect(plotMocks.plot).toHaveBeenCalled());
    expect(screen.getByRole("img", { name: "Stage error comparison" })).toBeInTheDocument();
    expect(screen.getByText("Exact values and accessible data table")).toBeVisible();
    expect(screen.getByRole("table")).toHaveTextContent("0.02018");
  });

  it("shows the deterministic SVG fallback when data are unavailable", () => {
    render(
      <ScientificChart
        data={[]}
        title="Stage error comparison"
        caption="Exact and observed normalized errors."
        summary="Stage one remains higher."
        fallbackSvgUrl="/artifacts/2026-07-22/chart.svg"
      />
    );
    expect(screen.getByRole("img", { name: "Stage one remains higher." })).toHaveAttribute(
      "src",
      "/artifacts/2026-07-22/chart.svg"
    );
    expect(screen.queryByLabelText("Chart series")).not.toBeInTheDocument();
    expect(screen.queryByText("Exact values and accessible data table")).not.toBeInTheDocument();
  });

  it("uses a unique caption relationship for each chart", () => {
    render(
      <>
        <ScientificChart data={[]} title="One" caption="First caption" summary="First." />
        <ScientificChart data={[]} title="Two" caption="Second caption" summary="Second." />
      </>
    );
    const figures = screen.getAllByRole("figure");
    const captionIds = figures.map((figure) => figure.getAttribute("aria-labelledby"));
    expect(new Set(captionIds).size).toBe(2);
  });
});
