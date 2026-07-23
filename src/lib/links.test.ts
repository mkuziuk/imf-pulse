import { describe, expect, it } from "vitest";
import { isExternalHref, safeHref, withAppUrl, withBaseUrl } from "./links";

describe("trusted links", () => {
  it.each([
    "//evil.example/path",
    "/\\evil.example/path",
    "/artifacts/../private.txt",
    "/artifacts/%2e%2e/private.txt",
    "/artifacts/%252e%252e/private.txt",
    "/artifacts/%25252e%25252e/private.txt",
    "/artifacts/%5c%5cevil.example/file",
    "/artifacts/%00/file"
  ])("rejects unsafe local href %s", (href) => {
    expect(safeHref(href)).toBeUndefined();
    expect(isExternalHref(href)).toBe(false);
    expect(withBaseUrl(href)).toBe("#");
  });

  it("keeps reviewed local and external destinations distinct", () => {
    expect(safeHref("/sources#source-1")).toBe("/sources#source-1");
    expect(isExternalHref("/sources#source-1")).toBe(false);
    expect(safeHref("https://example.org/paper.pdf")).toBe("https://example.org/paper.pdf");
    expect(isExternalHref("https://example.org/paper.pdf")).toBe(true);
  });

  it("prefixes static artifacts with the configured Pages base", () => {
    expect(withBaseUrl("/artifacts/2026-07-22/chart.svg", "/imf-pulse/")).toBe(
      "/imf-pulse/artifacts/2026-07-22/chart.svg"
    );
  });

  it("uses hash routes only for the public router mode", () => {
    expect(
      withAppUrl("/sources#source-1", {
        base: "/imf-pulse/",
        routerMode: "hash"
      })
    ).toBe("/imf-pulse/#/sources#source-1");
    expect(
      withAppUrl("/sources#source-1", {
        base: "/",
        routerMode: "browser"
      })
    ).toBe("/sources#source-1");
  });
});
