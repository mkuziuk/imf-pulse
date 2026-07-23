const ALLOWED_EXTERNAL_PROTOCOLS = new Set(["http:", "https:", "mailto:"]);

const CONTROL_CHARACTER = /[\u0000-\u001f\u007f]/;

function hasUnsafeSyntax(value: string): boolean {
  let decoded = value;
  for (let pass = 0; pass < 5; pass += 1) {
    if (
      CONTROL_CHARACTER.test(decoded) ||
      decoded.includes("\\") ||
      decoded.startsWith("//")
    ) {
      return true;
    }

    const path = decoded.split(/[?#]/, 1)[0];
    if (path.split("/").some((segment) => segment === "..")) return true;

    let next: string;
    try {
      next = decodeURIComponent(decoded);
    } catch {
      return true;
    }
    if (next === decoded) return false;
    decoded = next;
  }
  return true;
}

export function safeHref(value: string | undefined): string | undefined {
  if (!value) return undefined;
  const href = value.trim();
  if (!href || hasUnsafeSyntax(href)) return undefined;

  if (href.startsWith("#") || href.startsWith("/") || href.startsWith("./")) {
    return href;
  }

  try {
    const parsed = new URL(href);
    return ALLOWED_EXTERNAL_PROTOCOLS.has(parsed.protocol) ? href : undefined;
  } catch {
    return undefined;
  }
}

export function isExternalHref(value: string): boolean {
  const href = safeHref(value);
  if (!href) return false;
  try {
    const parsed = new URL(href);
    return parsed.protocol === "http:" || parsed.protocol === "https:";
  } catch {
    return false;
  }
}

export function isPublicArtifactUrl(value: string): boolean {
  if (/[\s?&#%]/.test(value)) return false;
  const href = safeHref(value);
  if (!href || isExternalHref(href)) return false;
  const path = href.replace(/^\.\//, "/");
  if (path.slice(1).split("/").some((segment) => segment === "")) return false;
  return path.startsWith("/artifacts/") || path.startsWith("artifacts/");
}

function normalizedBaseUrl(value: string): string {
  const safe = safeHref(value);
  if (
    !safe ||
    !safe.startsWith("/") ||
    safe.startsWith("//") ||
    safe.includes("#") ||
    safe.includes("?")
  ) {
    return "/";
  }
  return safe.endsWith("/") ? safe : `${safe}/`;
}

export function withBaseUrl(
  value: string,
  base = import.meta.env.BASE_URL || "/"
): string {
  const href = safeHref(value);
  if (!href || isExternalHref(href) || href.startsWith("mailto:")) return href ?? "#";
  if (href.startsWith("#")) return href;

  const normalizedBase = normalizedBaseUrl(base);
  const normalizedPath = href.replace(/^\.?\//, "");
  return `${normalizedBase}${normalizedPath}`;
}

interface AppUrlOptions {
  base?: string;
  routerMode?: "browser" | "hash";
}

/**
 * Formats a route owned by React Router. Hash routing keeps project Pages
 * deployments navigable on refresh while local builds retain clean paths.
 */
export function withAppUrl(value: string, options: AppUrlOptions = {}): string {
  const href = safeHref(value);
  if (!href || isExternalHref(href) || href.startsWith("mailto:")) return href ?? "#";
  if (href.startsWith("#")) return href;

  const base = normalizedBaseUrl(options.base ?? import.meta.env.BASE_URL ?? "/");
  const routerMode = options.routerMode ?? import.meta.env.VITE_ROUTER_MODE ?? "browser";
  if (routerMode !== "hash") return withBaseUrl(href, base);

  const route = `/${href.replace(/^\.?\//, "")}`;
  return `${base}#${route}`;
}

export function sourceAnchor(sourceId: string): string {
  return `/sources#${encodeURIComponent(sourceId)}`;
}

export function slugify(value: string): string {
  return value
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[^a-z0-9\s-]/g, "")
    .trim()
    .replace(/\s+/g, "-")
    .replace(/-+/g, "-");
}
