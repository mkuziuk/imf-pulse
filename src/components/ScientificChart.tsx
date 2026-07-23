import * as Plot from "@observablehq/plot";
import { useEffect, useId, useRef, useState } from "react";
import { safeHref, withBaseUrl } from "../lib/links";
import type { ArtifactFile, StageErrorDatum } from "../lib/schemas";

interface ScientificChartProps {
  data: StageErrorDatum[];
  title: string;
  caption: string;
  summary: string;
  downloads?: ArtifactFile[];
  fallbackSvgUrl?: string;
}

function valueLabel(value: number | undefined): string {
  return value == null ? "—" : value.toFixed(5);
}

export function ScientificChart({
  data,
  title,
  caption,
  summary,
  downloads = [],
  fallbackSvgUrl
}: ScientificChartProps) {
  const plotRef = useRef<HTMLDivElement>(null);
  const captionId = `chart-caption-${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;
  const [plotFailed, setPlotFailed] = useState(false);

  useEffect(() => {
    const container = plotRef.current;
    if (!container || data.length === 0) return undefined;
    let animationFrame = 0;
    let disposed = false;

    const render = () => {
      if (disposed) return;
      const width = Math.max(320, Math.floor(container.getBoundingClientRect().width || 960));
      try {
        const plot = Plot.plot({
          width,
          height: width < 560 ? 340 : 430,
          marginTop: 34,
          marginRight: width < 560 ? 22 : 42,
          marginBottom: 54,
          marginLeft: width < 560 ? 54 : 72,
          style: {
            background: "transparent",
            color: "#161a19",
            fontFamily: '"IBM Plex Sans", sans-serif',
            fontSize: width < 560 ? "11px" : "12px"
          },
          x: {
            type: "point",
            label: "Recursive stage",
            domain: data.map((datum) => datum.stage),
            tickFormat: (stage) => `S${String(stage)}`
          },
          y: {
            label: "RMSE / aᵏᐟ²",
            grid: true,
            zero: true,
            nice: true,
            tickFormat: (value) => Number(value).toFixed(3)
          },
          marks: [
            Plot.ruleY([0], { stroke: "#626862", strokeOpacity: 0.6 }),
            Plot.line(data, {
              x: "stage",
              y: "singlePass",
              stroke: "#626862",
              strokeWidth: 1.5,
              strokeDasharray: "5,4",
              curve: "monotone-x"
            }),
            Plot.dot(data, {
              x: "stage",
              y: "singlePass",
              fill: "#f3efe4",
              stroke: "#626862",
              r: 3,
              title: (datum) =>
                `Stage ${datum.stage}\nExact single-pass: ${valueLabel(datum.singlePass)}`
            }),
            Plot.line(data, {
              x: "stage",
              y: "recursiveExact",
              stroke: "#007c76",
              strokeWidth: 3,
              curve: "monotone-x"
            }),
            Plot.dot(data, {
              x: "stage",
              y: "recursiveExact",
              fill: "#00776f",
              stroke: "#f3efe4",
              strokeWidth: 1.5,
              r: 4,
              title: (datum) =>
                `Stage ${datum.stage}\nExact recursive: ${valueLabel(datum.recursiveExact)}`
            }),
            Plot.line(
              data.filter((datum) => datum.recursiveObserved != null),
              {
                x: "stage",
                y: "recursiveObserved",
                stroke: "#161a19",
                strokeWidth: 1.3,
                curve: "monotone-x"
              }
            ),
            Plot.dot(
              data.filter((datum) => datum.recursiveObserved != null),
              {
                x: "stage",
                y: "recursiveObserved",
                fill: "#161a19",
                r: 2.8,
                title: (datum) =>
                  `Stage ${datum.stage}\nSeed 777 recursive: ${valueLabel(datum.recursiveObserved)}`
              }
            ),
            Plot.text(data.filter((datum) => datum.stage === 1), {
              x: "stage",
              y: "recursiveExact",
              text: () => "initial low-pass",
              dx: 14,
              dy: -18,
              textAnchor: "start",
              fill: "#00776f",
              fontWeight: 600
            })
          ]
        });
        plot.setAttribute("aria-label", title);
        plot.setAttribute("role", "img");
        plot.setAttribute("tabindex", "0");
        container.replaceChildren(plot);
        setPlotFailed(false);
      } catch {
        container.replaceChildren();
        setPlotFailed(true);
      }
    };

    const scheduleRender = () => {
      cancelAnimationFrame(animationFrame);
      animationFrame = requestAnimationFrame(render);
    };
    scheduleRender();
    const resizeObserver =
      typeof ResizeObserver === "undefined" ? undefined : new ResizeObserver(scheduleRender);
    resizeObserver?.observe(container);

    return () => {
      disposed = true;
      cancelAnimationFrame(animationFrame);
      resizeObserver?.disconnect();
      container.replaceChildren();
    };
  }, [data, title]);

  const showFallback = data.length === 0 || plotFailed;
  const fallback = fallbackSvgUrl && safeHref(fallbackSvgUrl);

  return (
    <figure className="scientific-chart" aria-labelledby={captionId}>
      <div className="scientific-chart__legend" aria-label="Chart series">
        <span data-series="single">Exact single-pass</span>
        <span data-series="recursive">Exact recursive</span>
        <span data-series="observed">Seed 777 recursive</span>
      </div>
      <div ref={plotRef} className="scientific-chart__plot" aria-live="off">
        {showFallback && fallback ? (
          <img src={withBaseUrl(fallback)} alt={summary} />
        ) : null}
        {showFallback && !fallback ? (
          <div className="artifact-unavailable" role="status">
            Chart data are unavailable. The report remains readable without this figure.
          </div>
        ) : null}
      </div>
      <figcaption id={captionId}>
        <span className="figure-number">Figure</span>
        <span>{caption}</span>
      </figcaption>
      <p className="chart-summary">
        <strong>Reading.</strong> {summary}
      </p>
      {downloads.length > 0 ? (
        <nav className="artifact-downloads" aria-label="Chart downloads">
          {downloads.map((file) => (
            <a key={file.url} href={withBaseUrl(file.url)} download>
              {file.label ?? file.kind}
            </a>
          ))}
        </nav>
      ) : null}
      <details className="chart-data-table">
        <summary>Exact values and accessible data table</summary>
        <div className="table-scroll" tabIndex={0} role="region" aria-label={`${title} values`}>
          <table>
            <thead>
              <tr>
                <th scope="col">Stage</th>
                <th scope="col">Window</th>
                <th scope="col">Exact single-pass</th>
                <th scope="col">Exact recursive</th>
                <th scope="col">Seed 777 recursive</th>
              </tr>
            </thead>
            <tbody>
              {data.map((datum) => (
                <tr key={datum.stage}>
                  <th scope="row">{datum.stage}</th>
                  <td>{datum.window ?? "—"}</td>
                  <td>{valueLabel(datum.singlePass)}</td>
                  <td>{valueLabel(datum.recursiveExact)}</td>
                  <td>{valueLabel(datum.recursiveObserved)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </figure>
  );
}
