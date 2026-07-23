import { useEffect, useId, useRef, useState } from "react";

let mermaidInitialized = false;

async function loadMermaid() {
  const { default: mermaid } = await import("mermaid");
  if (!mermaidInitialized) {
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: "strict",
      suppressErrorRendering: true,
      theme: "base",
      themeVariables: {
        background: "#f3efe4",
        primaryColor: "#faf7ef",
        primaryTextColor: "#161a19",
        primaryBorderColor: "#626862",
        lineColor: "#007c76",
        secondaryColor: "#f3efe4",
        tertiaryColor: "#faf7ef",
        fontFamily: "IBM Plex Sans, sans-serif"
      },
      flowchart: { htmlLabels: false, curve: "basis" }
    });
    mermaidInitialized = true;
  }
  return mermaid;
}

export function isSafeMermaidSource(source: string): boolean {
  if (source.length > 20_000) return false;
  if (source.trimStart().startsWith("---")) return false;
  return !/%%\s*\{\s*(?:init|config)|\b(?:click|href)\s+|javascript\s*:|<\s*(?:script|iframe|object|embed|foreignObject)/i.test(
    source
  );
}

export function sanitizeMermaidSvg(svg: string): string {
  const parser = new DOMParser();
  const documentNode = parser.parseFromString(svg, "image/svg+xml");
  const root = documentNode.documentElement;
  if (root.nodeName.toLowerCase() !== "svg" || documentNode.querySelector("parsererror")) {
    throw new Error("Mermaid returned invalid SVG.");
  }

  root
    .querySelectorAll("script, iframe, object, embed, foreignObject, audio, video")
    .forEach((node) => node.remove());
  root.querySelectorAll("style").forEach((node) => {
    if (/url\s*\(|expression\s*\(|@import|https?:|\/\//i.test(node.textContent ?? "")) {
      node.remove();
    }
  });
  root.querySelectorAll("*").forEach((node) => {
    for (const attribute of [...node.attributes]) {
      const name = attribute.name.toLowerCase();
      const value = attribute.value.trim();
      if (name.startsWith("on")) node.removeAttribute(attribute.name);
      if (
        (name === "href" || name === "xlink:href") &&
        value &&
        !value.startsWith("#")
      ) {
        node.removeAttribute(attribute.name);
      }
      if (name === "style" && /url\s*\(|expression\s*\(|@import/i.test(value)) {
        node.removeAttribute(attribute.name);
      }
    }
  });
  root.setAttribute("role", "img");
  root.setAttribute("focusable", "false");
  return root.outerHTML;
}

interface MermaidDiagramProps {
  source: string;
  label?: string;
}

export function MermaidDiagram({ source, label = "Research relationship diagram" }: MermaidDiagramProps) {
  const id = `mermaid-${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;
  const containerRef = useRef<HTMLDivElement>(null);
  const [svg, setSvg] = useState<string>();
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let active = true;
    setSvg(undefined);
    setFailed(false);

    if (!isSafeMermaidSource(source)) {
      setFailed(true);
      return () => {
        active = false;
      };
    }

    void loadMermaid()
      .then((mermaid) => mermaid.render(id, source))
      .then((result) => {
        if (!active) return;
        const safeSvg = sanitizeMermaidSvg(result.svg);
        setSvg(safeSvg);
        requestAnimationFrame(() => {
          if (active && containerRef.current) result.bindFunctions?.(containerRef.current);
        });
      })
      .catch(() => {
        if (active) setFailed(true);
      });

    return () => {
      active = false;
    };
  }, [id, source]);

  if (failed) {
    return (
      <div className="mermaid-fallback" role="note" aria-label={`${label} unavailable`}>
        <p>Diagram unavailable. Its source is shown for review.</p>
        <pre>
          <code>{source}</code>
        </pre>
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      className="mermaid-diagram"
      role="img"
      aria-label={label}
      aria-busy={!svg}
      // Mermaid is configured in strict mode and the returned SVG is sanitized again above.
      dangerouslySetInnerHTML={svg ? { __html: svg } : undefined}
    >
      {!svg ? <span className="loading-line">Rendering relationship diagram…</span> : null}
    </div>
  );
}
