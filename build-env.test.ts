import { mkdirSync, mkdtempSync, realpathSync, rmSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { resolveBuildOutDir } from "./build-env";

const temporaryRoots: string[] = [];

function fixtureRoot(): string {
  const root = mkdtempSync(join(tmpdir(), "imf-pulse-build-env-"));
  temporaryRoots.push(root);
  mkdirSync(join(root, "data", ".site-staging"), { recursive: true });
  return root;
}

beforeEach(() => {
  vi.stubEnv("IMF_PULSE_BUILD_OUT_DIR", undefined);
});

afterEach(() => {
  vi.unstubAllEnvs();
  for (const root of temporaryRoots.splice(0)) rmSync(root, { recursive: true, force: true });
});

describe("site build output boundary", () => {
  it("uses dist only when no staging output is configured", () => {
    const root = fixtureRoot();
    expect(resolveBuildOutDir(root, undefined)).toBe(resolve(realpathSync(root), "dist"));
  });

  it("accepts exact relative and absolute project-owned staging directories", () => {
    const root = fixtureRoot();
    const runId = "run-0123456789abcdef0123456789abcdef";
    const staging = join(root, "data", ".site-staging", runId);
    mkdirSync(staging);
    expect(resolveBuildOutDir(root, `data/.site-staging/${runId}`)).toBe(realpathSync(staging));
    expect(resolveBuildOutDir(root, realpathSync(staging))).toBe(realpathSync(staging));
  });

  it("honors an inherited valid staging directory when no argument is supplied", () => {
    const root = fixtureRoot();
    const runId = "run-fedcba9876543210fedcba9876543210";
    const staging = join(root, "data", ".site-staging", runId);
    mkdirSync(staging);
    vi.stubEnv("IMF_PULSE_BUILD_OUT_DIR", `data/.site-staging/${runId}`);

    expect(resolveBuildOutDir(root)).toBe(realpathSync(staging));
  });

  it("rejects escapes, nested paths, and symlink staging targets", () => {
    const root = fixtureRoot();
    const outside = join(root, "outside");
    mkdirSync(outside);
    const symlinkId = "run-abcdef0123456789abcdef0123456789";
    symlinkSync(outside, join(root, "data", ".site-staging", symlinkId));

    expect(() => resolveBuildOutDir(root, "../outside")).toThrow(/exact data\/\.site-staging/i);
    expect(() => resolveBuildOutDir(root, "data/.site-staging/run/nested")).toThrow(
      /exact data\/\.site-staging/i
    );
    expect(() => resolveBuildOutDir(root, `data/.site-staging/${symlinkId}`)).toThrow(
      /non-symlink directory/i
    );
  });

  it("rejects a symlinked default dist before Vite can empty it", () => {
    const root = fixtureRoot();
    const outside = join(root, "outside-dist");
    mkdirSync(outside);
    symlinkSync(outside, join(root, "dist"));

    expect(() => resolveBuildOutDir(root, undefined)).toThrow(/non-symlink directory/i);
  });
});
