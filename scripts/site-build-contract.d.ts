export interface SiteTreeHash {
  sha256: string;
  files: Record<string, string>;
}

export interface PreviewSiteSelection {
  mode: "preview" | "published";
  path: string;
  sha256?: string;
}

export function canonicalJsonHash(value: unknown): string;
export function hashSiteTree(siteRoot: string): SiteTreeHash;
export function selectPreviewSiteBuild(projectRoot?: string): PreviewSiteSelection;
