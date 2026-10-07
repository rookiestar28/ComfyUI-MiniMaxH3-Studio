import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

import { resolveH3ContextBuildMetadata } from "./buildMetadata.js";
import { resolveH3ContextBuildIdentity } from "./buildProvenance.js";

const h3BuildMetadata = resolveH3ContextBuildMetadata();
const h3BuildProvenance = resolveH3ContextBuildIdentity();

export default defineConfig({
  plugins: [react()],
  define: {
    __H3_CONTEXT_BUILD_METADATA__: JSON.stringify(h3BuildMetadata),
    __H3_CONTEXT_BUILD_PROVENANCE__: JSON.stringify(h3BuildProvenance),
  },
  test: {
    environment: "jsdom",
    include: ["red-tests/**/*.red.test.{ts,tsx}"],
    restoreMocks: true,
    clearMocks: true,
  },
});
