import type { Plugin } from "vite";
import { readFileSync, statSync } from "node:fs";
import { resolve, sep } from "node:path";

// A test-owned HTTP download endpoint is necessary: Chromium's native download can
// bypass page interception. Never replace this assertion with a fetched Blob download.
export const outputFixtureBody = "bounded-hermetic-output-body";
export function authoringOutputFixture(): Plugin {
  let downloads = 0;
  // M25-21 `action.output.download`: the integrated shell's own job handles are minted per run,
  // so the exact-handle route below cannot serve them. A download for any well-formed output
  // handle is served the same bounded body; the counter stays on the M25-19 handle alone.
  const anyOutputDownload =
    /^\/h3-context\/v1\/authoring\/output\/aro_[A-Za-z0-9_-]{22}\/download$/u;
  const outputHandleDownload =
    /^\/h3-context\/v1\/authoring\/output\/(aro_[A-Za-z0-9_-]{22})\/download$/u;
  return {
    name: "h3-authoring-output-hermetic-download",
    configureServer(server) {
      server.middlewares.use((request, response, next) => {
        const url = new URL(request.url ?? "/", "http://127.0.0.1");
        const output = `/h3-context/v1/authoring/output/aro_${"d".repeat(22)}/download`;
        if (
          request.method === "GET" &&
          url.pathname === "/__output_fixture/downloads"
        ) {
          response.setHeader("Content-Type", "application/json");
          response.end(JSON.stringify({ downloads }));
        } else if (
          request.method === "GET" &&
          (anyOutputDownload.test(url.pathname) || url.pathname === output) &&
          url.search.startsWith("?workspace_handle=")
        ) {
          const relayRootValue = process.env.H3_CONTEXT_E2E_OUTPUT_RELAY_DIR;
          const relayMatch = outputHandleDownload.exec(url.pathname);
          if (relayRootValue && relayMatch) {
            const relayRoot = resolve(relayRootValue);
            const relayPath = resolve(relayRoot, `${relayMatch[1]}.mp4`);
            // SECURITY: only the closed opaque handle selects a file, and resolution must stay
            // inside the explicitly configured repo-local test relay. Native downloads bypass
            // Playwright routing, so this is the only path that can preserve the exact product
            // bytes rendered by the persistent acceptance fixture.
            if (!relayPath.startsWith(relayRoot + sep)) return next();
            try {
              const size = statSync(relayPath).size;
              if (size < 1 || size > 4_000_000) return next();
              const body = readFileSync(relayPath);
              response.writeHead(200, {
                "Content-Type": "video/mp4",
                "Content-Length": body.byteLength,
                "Content-Disposition":
                  'attachment; filename="authoring-final.mp4"',
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Accept-Ranges": "bytes",
              });
              response.end(body);
              return;
            } catch {
              return next();
            }
          }
          if (
            url.pathname === output &&
            url.search === `?workspace_handle=authoring-${"a".repeat(32)}`
          )
            downloads++;
          response.writeHead(200, {
            "Content-Type": "video/mp4",
            "Content-Length": Buffer.byteLength(outputFixtureBody),
            "Content-Disposition": 'attachment; filename="authoring-final.mp4"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Accept-Ranges": "bytes",
          });
          response.end(outputFixtureBody);
        } else next();
      });
    },
  };
}
