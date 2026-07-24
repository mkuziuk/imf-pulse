import { useEffect, useId, useState, type CSSProperties } from "react";
import { z } from "zod";
import { withBaseUrl } from "../lib/links";

const DiagramNodeSchema = z.object({
  id: z.string().trim().min(1).max(80).regex(/^[A-Za-z0-9._-]+$/),
  label: z.string().trim().min(1).max(120)
});

const DiagramEdgeSchema = z.object({
  from: z.string().trim().min(1).max(80),
  to: z.string().trim().min(1).max(80),
  label: z.string().trim().min(1).max(120)
});

const DiagramSpecSchema = z
  .object({
    schema_version: z.literal(1),
    artifact_id: z.string().trim().min(1).max(160),
    title: z.string().trim().min(1).max(160),
    nodes: z.array(DiagramNodeSchema).min(2).max(8),
    edges: z.array(DiagramEdgeSchema).min(1).max(12)
  })
  .superRefine((spec, context) => {
    const ids = spec.nodes.map((node) => node.id);
    if (new Set(ids).size !== ids.length) {
      context.addIssue({ code: "custom", message: "Diagram node IDs must be unique." });
      return;
    }
    for (const [index, edge] of spec.edges.entries()) {
      if (edge.from !== ids[index] || edge.to !== ids[index + 1]) {
        context.addIssue({
          code: "custom",
          message: "The responsive renderer accepts an ordered linear flow only."
        });
        return;
      }
    }
    if (spec.edges.length !== spec.nodes.length - 1) {
      context.addIssue({
        code: "custom",
        message: "The responsive renderer requires one connector between each node."
      });
    }
  });

type DiagramSpec = z.infer<typeof DiagramSpecSchema>;

interface StructuralDiagramProps {
  specUrl: string;
  fallbackUrl: string;
  caption: string;
}

export function StructuralDiagram({
  specUrl,
  fallbackUrl,
  caption
}: StructuralDiagramProps) {
  const [spec, setSpec] = useState<DiagramSpec>();
  const [failed, setFailed] = useState(false);
  const titleId = useId();

  useEffect(() => {
    const controller = new AbortController();
    setSpec(undefined);
    setFailed(false);
    void fetch(withBaseUrl(specUrl), {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      signal: controller.signal
    })
      .then(async (response) => {
        if (!response.ok) throw new Error(`Diagram specification returned ${response.status}.`);
        const contentType = response.headers.get("content-type");
        if (contentType && !contentType.includes("json")) {
          throw new Error("Diagram specification did not return JSON.");
        }
        return DiagramSpecSchema.parse(await response.json());
      })
      .then((value) => setSpec(value))
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setFailed(true);
      });
    return () => controller.abort();
  }, [specUrl]);

  if (!spec) {
    return (
      <div className="structural-diagram structural-diagram--fallback">
        <img src={withBaseUrl(fallbackUrl)} alt={caption} loading="lazy" />
        {!failed ? <span className="sr-only">Loading responsive diagram.</span> : null}
      </div>
    );
  }

  return (
    <div className="structural-diagram" role="img" aria-labelledby={titleId}>
      <p className="sr-only" id={titleId}>
        {spec.title}
      </p>
      <div
        className="structural-diagram__flow"
        style={{ "--diagram-columns": spec.nodes.length } as CSSProperties}
      >
        {spec.nodes.map((node, index) => {
          const edge = spec.edges[index];
          return (
            <div className="structural-diagram__step" key={node.id}>
              <div className="structural-diagram__node">{node.label}</div>
              {edge ? (
                <div className="structural-diagram__connector" aria-hidden="true">
                  <span>{edge.label}</span>
                  <i />
                </div>
              ) : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}
