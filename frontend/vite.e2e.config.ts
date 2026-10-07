import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { transform as transformCss } from "lightningcss";
import { defineConfig } from "vite";

import { resolveH3ContextBuildIdentity } from "./buildProvenance.js";
import { authoringOutputFixture } from "./e2e/authoringOutputServer.js";
import { serviceLoopbackFixture } from "./e2e/serviceLoopback.js";

const tokensCssPath = fileURLToPath(
  new URL("./src/styles/tokens.css", import.meta.url),
);
const productionInlineCss = new TextDecoder().decode(
  transformCss({
    filename: tokensCssPath,
    code: readFileSync(tokensCssPath),
    minify: true,
  }).code,
);
const productionInlineCssId = "\0h3-context-e2e-production-inline-css";
const h3BuildProvenance = resolveH3ContextBuildIdentity(
  fileURLToPath(new URL("..", import.meta.url)),
);

export default defineConfig({
  root: fileURLToPath(new URL("./e2e", import.meta.url)),
  plugins: [
    {
      name: "h3-context-e2e-production-inline-css",
      enforce: "pre",
      resolveId(source, importer) {
        // M25-16: the integrated-shell harness (nleShell.tsx) mounts through the same
        // `createMountController` style-length bound `entry.tsx` does, so it needs the same
        // minified stylesheet -- the raw (unminified) tokens.css exceeds that bound.
        const normalizedImporter = importer?.replaceAll("\\", "/");
        if (
          source.endsWith("styles/tokens.css?inline") &&
          (normalizedImporter?.endsWith("/src/entry.tsx") ||
            normalizedImporter?.endsWith("/e2e/nleShell.tsx"))
        )
          return productionInlineCssId;
        return null;
      },
      load(id) {
        return id === productionInlineCssId
          ? `export default ${JSON.stringify(productionInlineCss)};`
          : null;
      },
    },
    react(),
    authoringOutputFixture(),
    // Gated on H3_CONTEXT_E2E_SERVICE_LOOPBACK=1; a no-op plugin otherwise. See
    // frontend/e2e/serviceLoopback.ts for the boundary this proxies to.
    serviceLoopbackFixture(),
  ],
  resolve: {
    alias: {
      "../../scripts/app.js": fileURLToPath(
        new URL("./tests/fixtures/entryHostModules.ts", import.meta.url),
      ),
      "../../scripts/api.js": fileURLToPath(
        new URL("./tests/fixtures/entryHostModules.ts", import.meta.url),
      ),
    },
  },
  define: {
    __H3_CONTEXT_BUILD_METADATA__: JSON.stringify({
      version: "0.1.0",
      displayVersion: "v0.1.0",
      repositoryUrl: "https://github.com/rookiestar28/ComfyUI-MiniMaxH3-Studio",
    }),
    __H3_CONTEXT_BUILD_PROVENANCE__: JSON.stringify(h3BuildProvenance),
  },
  server: { strictPort: true },
});
