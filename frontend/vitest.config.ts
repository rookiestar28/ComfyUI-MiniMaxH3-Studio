import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

import { resolveH3ContextBuildMetadata } from "./buildMetadata.js";
import { resolveH3ContextBuildIdentity } from "./buildProvenance.js";

const h3BuildMetadata = resolveH3ContextBuildMetadata();
const h3BuildProvenance = resolveH3ContextBuildIdentity();

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "../../scripts/app.js": fileURLToPath(
        new URL("./tests/fixtures/entryHostModules.ts", import.meta.url),
      ),
      "../../scripts/api.js": fileURLToPath(
        new URL("./tests/fixtures/entryHostModules.ts", import.meta.url),
      ),
      "/scripts/app.js": fileURLToPath(
        new URL("./tests/fixtures/entryHostModules.ts", import.meta.url),
      ),
      "/scripts/api.js": fileURLToPath(
        new URL("./tests/fixtures/entryHostModules.ts", import.meta.url),
      ),
    },
  },
  define: {
    __H3_CONTEXT_BUILD_METADATA__: JSON.stringify(h3BuildMetadata),
    __H3_CONTEXT_BUILD_PROVENANCE__: JSON.stringify(h3BuildProvenance),
  },
  test: {
    environment: "jsdom",
    include: ["tests/**/*.test.{ts,tsx}"],
    setupFiles: ["./tests/setup/asyncUtilTimeout.ts"],
    // IMPORTANT: the Windows Full Gate runs this suite after backend coverage, and the default
    // five-second whole-test ceiling intermittently terminates valid dynamic-import/jsdom rows.
    // Keep one bounded outer envelope here; assertion waits retain their stricter own ceilings.
    testTimeout: 15_000,
    restoreMocks: true,
    clearMocks: true,
  },
});
