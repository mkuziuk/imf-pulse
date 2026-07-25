import type { ReactNode } from "react";
import ReactMarkdown, { type Components, defaultUrlTransform } from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import {
  isExternalHref,
  isPublicArtifactUrl,
  directSourceHref,
  safeHref,
  slugify,
  sourceIdFromLegacyHref,
  withAppUrl,
  withBaseUrl
} from "../lib/links";
import type { SourceRecord } from "../lib/schemas";
import { MermaidDiagram } from "./MermaidDiagram";

function headingText(children: ReactNode): string {
  if (typeof children === "string" || typeof children === "number") return String(children);
  if (Array.isArray(children)) return children.map(headingText).join("");
  if (children && typeof children === "object" && "props" in children) {
    return headingText((children as { props: { children?: ReactNode } }).props.children);
  }
  return "section";
}

function headingId(children: ReactNode): string {
  const slug = slugify(headingText(children));
  const signal = slug.match(/^signal-(\d{1,2})(?:-|$)/);
  return signal ? `signal-${signal[1].padStart(2, "0")}` : slug;
}

function safeMarkdownUrl(url: string): string {
  const safe = safeHref(url);
  return safe ? defaultUrlTransform(safe) : "";
}

function markdownComponents(sources: readonly SourceRecord[]): Components {
  return {
    h1: ({ children }) => <h2 id={headingId(children)}>{children}</h2>,
    h2: ({ children }) => <h2 id={headingId(children)}>{children}</h2>,
    h3: ({ children }) => <h3 id={headingId(children)}>{children}</h3>,
    a: ({ href, children }) => {
      const safe = safeHref(href);
      if (!safe) return <span>{children}</span>;
      const sourceId = sourceIdFromLegacyHref(safe);
      const resolvedSource = sourceId ? directSourceHref(sourceId, sources) : undefined;
      if (sourceId && !resolvedSource) return <span>{children}</span>;
      const resolved = resolvedSource ?? safe;
      const external = isExternalHref(resolved);
      const destination = external
        ? resolved
        : isPublicArtifactUrl(resolved)
          ? withBaseUrl(resolved)
          : withAppUrl(resolved);
      return (
        <a
          href={destination}
          target={external ? "_blank" : undefined}
          rel={external ? "noopener noreferrer" : undefined}
        >
          {children}
          {external ? (
            <span className="external-mark" aria-label=" (external link)">↗</span>
          ) : null}
        </a>
      );
    },
    img: () => {
      return (
        <span className="withheld-image" role="note">
          Inline image omitted. Artifacts render only through a validated manifest and rights record.
        </span>
      );
    },
    code: ({ className, children }) => {
      const language = className?.replace("language-", "");
      const source = String(children).replace(/\n$/, "");
      if (language === "mermaid") return <MermaidDiagram source={source} />;
      if (!className) return <code>{children}</code>;
      return <code className={className}>{children}</code>;
    },
    pre: ({ children }) => {
      if (
        children &&
        typeof children === "object" &&
        "type" in children &&
        (children as { type?: unknown }).type === MermaidDiagram
      ) {
        return <>{children}</>;
      }
      return <pre>{children}</pre>;
    },
    table: ({ children }) => (
      <div className="table-scroll" tabIndex={0} role="region" aria-label="Scrollable data table">
        <table>{children}</table>
      </div>
    )
  };
}

interface MarkdownRendererProps {
  markdown: string;
  className?: string;
  sources?: readonly SourceRecord[];
}

export function MarkdownRenderer({ markdown, className = "", sources = [] }: MarkdownRendererProps) {
  return (
    <div className={`markdown-body ${className}`}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[[rehypeKatex, { output: "htmlAndMathml", strict: "warn" }]]}
        components={markdownComponents(sources)}
        skipHtml
        urlTransform={safeMarkdownUrl}
      >
        {markdown}
      </ReactMarkdown>
    </div>
  );
}
