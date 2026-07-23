import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { researchContentPlugin } from "./content-bundle";
import { getCandidateReleaseContext } from "./release-env";
import { resolveBuildOutDir } from "./build-env";

const candidate = getCandidateReleaseContext();
const publicReleaseBuild = process.env.IMF_PULSE_PUBLIC_RELEASE_DIR !== undefined;

export default defineConfig({
  base: process.env.VITE_BASE_PATH || "/",
  plugins: [researchContentPlugin(process.cwd(), candidate), react()],
  publicDir: false,
  server: {
    host: "127.0.0.1",
    fs: {
      strict: true,
      allow: [process.cwd()],
      deny: [
        "**/.git/**",
        "**/.env*",
        "**/imports/**",
        "**/extracts/**",
        "**/data/runs/**",
        "**/*.{pem,key}"
      ]
    }
  },
  preview: {
    host: "127.0.0.1"
  },
  build: {
    // Public releases are intentionally self-contained. Source maps would expose
    // the application source outside the reviewed release bundle.
    sourcemap: !publicReleaseBuild,
    outDir: resolveBuildOutDir(),
    emptyOutDir: true
  }
});
