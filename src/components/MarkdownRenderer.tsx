import type { ReactNode } from "react";
import ReactMarkdown, { type Components, defaultUrlTransform } from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import {
  isExternalHref,
  isPublicArtifactUrl,
  safeHref,
  slugify,
  withAppUrl,
  withBaseUrl
} from "../lib/links";
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

const components: Components = {
  h1: ({ children }) => <h2 id={headingId(children)}>{children}</h2>,
  h2: ({ children }) => <h2 id={headingId(children)}>{children}</h2>,
  h3: ({ children }) => <h3 id={headingId(children)}>{children}</h3>,
  a: ({ href, children }) => {
    const safe = safeHref(href);
    if (!safe) return <span>{children}</span>;
    const external = isExternalHref(safe);
    const destination = external
      ? safe
      : isPublicArtifactUrl(safe)
        ? withBaseUrl(safe)
        : withAppUrl(safe);
    return (
      <a
        href={destination}
        target={external ? "_blank" : undefined}
        rel={external ? "noopener noreferrer" : undefined}
      >
        {children}
        {external ? <span className="external-mark" aria-label=" (external link)">↗</span> : null}
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

interface MarkdownRendererProps {
  markdown: string;
  className?: string;
}

export function MarkdownRenderer({ markdown, className = "" }: MarkdownRendererProps) {
  return (
    <div className={`markdown-body ${className}`}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[[rehypeKatex, { output: "htmlAndMathml", strict: "warn" }]]}
        components={components}
        skipHtml
        urlTransform={safeMarkdownUrl}
      >
        {markdown}
      </ReactMarkdown>
    </div>
  );
}
