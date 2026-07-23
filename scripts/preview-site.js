import { preview } from "vite";
import { hashSiteTree, selectPreviewSiteBuild } from "./site-build-contract.js";

const projectRoot = process.cwd();

try {
  const selected = selectPreviewSiteBuild(projectRoot);
  const immutablePreviewGuard = {
    name: "imf-pulse-immutable-preview",
    configResolved(config) {
      if (config.build.outDir !== selected.path) {
        throw new Error("Preview output directory differs from the verified site build.");
      }
    },
    configurePreviewServer() {
      if (selected.mode === "published") {
        const verified = hashSiteTree(selected.path);
        if (verified.sha256 !== selected.sha256) {
          throw new Error("Published site build changed before the preview server started.");
        }
      }
    }
  };
  const server = await preview({
    configFile: false,
    root: projectRoot,
    publicDir: false,
    plugins: [immutablePreviewGuard],
    build: { outDir: selected.path },
    preview: { host: "127.0.0.1" }
  });
  server.printUrls();
  const detail = selected.mode === "published" ? ` (${selected.sha256})` : "";
  console.log(`Serving verified ${selected.mode} site build${detail}.`);
} catch (error) {
  const detail = error instanceof Error ? error.message : String(error);
  console.error(`Preview refused: ${detail}`);
  process.exitCode = 1;
}
