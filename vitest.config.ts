import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { researchContentPlugin } from "./content-bundle";
import { getCandidateReleaseContext } from "./release-env";

const candidate = getCandidateReleaseContext();

export default defineConfig({
  plugins: [researchContentPlugin(process.cwd(), candidate), react()],
  test: {
    include: [
      "src/**/*.test.{ts,tsx}",
      "build-env.test.ts",
      "content-bundle.test.ts",
      "release-env.test.ts",
      "site-build-contract.test.ts"
    ],
    exclude: ["imports/**", "data/**", "public/**", "content/**", "node_modules/**", ".git/**"],
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: true,
    restoreMocks: true,
    clearMocks: true
  }
});
