import { createHash } from "node:crypto";
import {
  mkdirSync,
  mkdtempSync,
  realpathSync,
  renameSync,
  rmSync,
  symlinkSync,
  writeFileSync
} from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import {
  canonicalJsonHash,
  hashSiteTree,
  selectPreviewSiteBuild
} from "./scripts/site-build-contract.js";

const temporaryRoots: string[] = [];

function temporaryProject(): string {
  const root = mkdtempSync(resolve(tmpdir(), "imf-pulse-site-contract-"));
  temporaryRoots.push(root);
  return root;
}

function sha256(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function installSiteBuild(
  project: string,
  files: Record<string, string> = {
    "index.html": "INDEX",
    "assets/app.js": "APP"
  }
): { path: string; relative: string; sha256: string } {
  const candidate = resolve(project, "candidate");
  mkdirSync(candidate);
  for (const [relative, bytes] of Object.entries(files)) {
    const path = resolve(candidate, ...relative.split("/"));
    mkdirSync(resolve(path, ".."), { recursive: true });
    writeFileSync(path, bytes);
  }
  const { sha256: digest } = hashSiteTree(candidate);
  const builds = resolve(project, "data", "site-builds");
  mkdirSync(builds, { recursive: true });
  const path = resolve(builds, `site-${digest}`);
  renameSync(candidate, path);
  return {
    path: realpathSync(path),
    relative: `data/site-builds/site-${digest}`,
    sha256: digest
  };
}

function writePointer(
  project: string,
  site: { relative: string; sha256: string },
  overrides: Record<string, unknown> = {}
): void {
  writeFileSync(
    resolve(project, "data", "current.json"),
    JSON.stringify({
      status: "published",
      site_build_path: site.relative,
      site_build_sha256: site.sha256,
      ...overrides
    })
  );
}

afterEach(() => {
  for (const root of temporaryRoots.splice(0)) rmSync(root, { recursive: true, force: true });
});

describe("Python-compatible site tree hashing", () => {
  it("matches the fixed two-file pipeline digest", () => {
    expect(
      canonicalJsonHash({
        "assets/app.js": sha256("APP"),
        "index.html": sha256("INDEX")
      })
    ).toBe("ba6be4fc6aa52679ac41fa3d09eb67b791dd07917f2fa2f3fb53124da94b2fff");
  });

  it("sorts non-BMP names by Unicode code point like Python", () => {
    expect(
      canonicalJsonHash({
        "index.html": sha256("INDEX"),
        "\ue000": sha256("BMP"),
        "𐀀": sha256("ASTRAL")
      })
    ).toBe("a79fc9259572a2d3b700bfdb36bce53fab63c65066bde7e513246fc8b6988c0b");
  });

  it("hashes every regular file and treats empty directories as identity-neutral", () => {
    const project = temporaryProject();
    const site = resolve(project, "site");
    mkdirSync(resolve(site, "assets", "empty"), { recursive: true });
    writeFileSync(resolve(site, "index.html"), "INDEX");
    writeFileSync(resolve(site, "assets", "app.js"), "APP");

    const tree = hashSiteTree(site);
    expect(tree.sha256).toBe(
      "ba6be4fc6aa52679ac41fa3d09eb67b791dd07917f2fa2f3fb53124da94b2fff"
    );
    expect(tree.files).toEqual({
      "assets/app.js": sha256("APP"),
      "index.html": sha256("INDEX")
    });
  });

  it("requires root index.html", () => {
    const project = temporaryProject();
    const site = resolve(project, "site");
    mkdirSync(site);
    writeFileSync(resolve(site, "nested.html"), "not the entry point");
    expect(() => hashSiteTree(site)).toThrow(/root index\.html/);
  });
});

describe("immutable preview selection", () => {
  it.each(["published", "processed_no_pulse", "unchanged"])(
    "selects the pointer-bound build for %s checkpoints",
    (status) => {
      const project = temporaryProject();
      const site = installSiteBuild(project);
      writePointer(project, site, { status });

      expect(selectPreviewSiteBuild(project)).toEqual({
        mode: "published",
        path: site.path,
        sha256: site.sha256
      });
    }
  );

  it("falls back to dist only when data/current.json is absent", () => {
    const project = temporaryProject();
    mkdirSync(resolve(project, "dist"));
    writeFileSync(resolve(project, "dist", "index.html"), "preview");
    expect(selectPreviewSiteBuild(project)).toEqual({
      mode: "preview",
      path: resolve(realpathSync(project), "dist")
    });

    mkdirSync(resolve(project, "data"));
    writeFileSync(resolve(project, "data", "current.json"), "{}");
    expect(() => selectPreviewSiteBuild(project)).toThrow(/accepted checkpoint status/);
  });

  it("rejects a dangling current pointer instead of falling back", () => {
    const project = temporaryProject();
    mkdirSync(resolve(project, "dist"));
    writeFileSync(resolve(project, "dist", "index.html"), "preview");
    mkdirSync(resolve(project, "data"));
    symlinkSync("missing.json", resolve(project, "data", "current.json"));
    expect(() => selectPreviewSiteBuild(project)).toThrow(/regular, non-symlink file/);
  });

  it.each([
    { status: "candidate" },
    { site_build_sha256: "A".repeat(64) },
    { site_build_path: "data/site-builds/site-wrong" },
    { site_build_path: "/tmp/site" }
  ])("rejects malformed pointer selection %#", (override) => {
    const project = temporaryProject();
    const site = installSiteBuild(project);
    writePointer(project, site, override);
    expect(() => selectPreviewSiteBuild(project)).toThrow();
  });

  it.each([
    ["changed", (path: string) => writeFileSync(resolve(path, "index.html"), "CHANGED")],
    ["added", (path: string) => writeFileSync(resolve(path, "extra.txt"), "EXTRA")],
    ["deleted", (path: string) => rmSync(resolve(path, "assets", "app.js"))]
  ])("rejects a %s immutable tree", (_label, mutate) => {
    const project = temporaryProject();
    const site = installSiteBuild(project);
    writePointer(project, site);
    mutate(site.path);
    expect(() => selectPreviewSiteBuild(project)).toThrow(/does not match/);
  });

  it("rejects symlinks anywhere in the selected tree", () => {
    const project = temporaryProject();
    const site = installSiteBuild(project);
    writePointer(project, site);
    symlinkSync("index.html", resolve(site.path, "alias.html"));
    expect(() => selectPreviewSiteBuild(project)).toThrow(/forbidden symlink/);
  });
});
