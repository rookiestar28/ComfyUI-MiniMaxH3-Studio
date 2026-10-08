import type { Plugin } from "vite";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import http from "node:http";
import { fileURLToPath } from "node:url";
import { fixturePython } from "./fixturePython";

// M25-16 evidence layer 2: this plugin exists only to give a hermetic Playwright journey a real,
// running application service to talk to -- see scripts/m25_16_service_loopback.py. It is gated
// behind an explicit env var and does nothing at all unless that var is exactly "1", so every
// existing hermetic run (and this env var absent) is byte-for-byte unchanged.
export const SERVICE_LOOPBACK_ENV = "H3_CONTEXT_E2E_SERVICE_LOOPBACK";

// The real adapters admit a request only for the target its Host selects on the socket that
// accepted it (comfyui_h3_context/core/request_target.py): the page must be served by the same
// listener it posts to. This harness's page is served by *this* Vite dev server on whatever port
// H3_CONTEXT_E2E_PORT picked, which is a different listener from the backend's private ephemeral
// port, so this proxy -- bound to 127.0.0.1 only, gated by the env var above, and reachable solely
// from this dev server's own middleware chain -- forwards each verified browser request as a
// same-origin request of the backend's own listener (`http://127.0.0.1:<backend port>` in both
// Origin and Host) before it ever leaves this process. Removing this makes every mutating route
// 403 for this journey; nothing outside this file may rely on it, and it must never widen beyond
// this one proxied hop.
const PROXIED_PREFIXES = [
  "/h3-context/",
  "/prompt",
  "/system_stats",
  "/__loopback/",
];

function pythonExecutable(): string {
  const root = fileURLToPath(new URL("../../", import.meta.url));
  return fixturePython(root);
}

function loopbackScript(): string {
  return fileURLToPath(
    new URL("../../scripts/m25_16_service_loopback.py", import.meta.url),
  );
}

export function serviceLoopbackFixture(): Plugin {
  let child: ChildProcessWithoutNullStreams | undefined;
  let ready: Promise<number> | undefined;

  function startBackend(): Promise<number> {
    return new Promise((resolve, reject) => {
      const proc = spawn(
        pythonExecutable(),
        [loopbackScript(), "--port", "0"],
        {
          stdio: ["pipe", "pipe", "pipe"],
        },
      );
      child = proc;
      let buffer = "";
      let settled = false;
      const deadline = setTimeout(() => {
        if (settled) return;
        settled = true;
        stopBackend();
        reject(new Error("loopback service readiness timed out"));
      }, 15_000);
      proc.stdout.on("data", (chunk: Buffer) => {
        if (settled) return;
        buffer += chunk.toString("utf8");
        if (buffer.length > 65_536) {
          settled = true;
          clearTimeout(deadline);
          stopBackend();
          reject(new Error("loopback service readiness exceeded its bound"));
          return;
        }
        for (
          let newline = buffer.indexOf("\n");
          newline !== -1;
          newline = buffer.indexOf("\n")
        ) {
          const line = buffer.slice(0, newline);
          buffer = buffer.slice(newline + 1);
          try {
            const parsed = JSON.parse(line) as {
              ready?: boolean;
              port?: number;
            };
            if (
              parsed.ready === true &&
              Number.isInteger(parsed.port) &&
              parsed.port! > 0 &&
              parsed.port! <= 65535
            ) {
              settled = true;
              clearTimeout(deadline);
              resolve(parsed.port!);
              return;
            }
          } catch {
            // Ignore a bounded startup banner; process the next line rather than re-reading it.
          }
        }
      });
      // Drain stderr without retaining native paths or exception payloads in browser evidence.
      proc.stderr.resume();
      proc.on("error", (error) => {
        clearTimeout(deadline);
        if (!settled) {
          settled = true;
          reject(error);
        }
      });
      proc.on("exit", (code) => {
        clearTimeout(deadline);
        if (!settled) {
          settled = true;
          reject(
            new Error(`loopback service exited before ready (code ${code})`),
          );
        }
      });
    });
  }

  function stopBackend(): void {
    const proc = child;
    child = undefined;
    if (proc === undefined) return;
    // Graceful path first: closing stdin is the reliable cross-platform shutdown signal (see
    // scripts/m25_16_service_loopback.py's `_watch_stdin_close` guard comment for why Windows
    // cannot rely on SIGTERM here). Force-kill only if it does not exit promptly.
    try {
      proc.stdin.end();
    } catch {
      // Already closed.
    }
    const timer = setTimeout(() => {
      if (!proc.killed) proc.kill();
    }, 3_000);
    proc.once("exit", () => clearTimeout(timer));
  }

  return {
    name: "h3-service-loopback",
    configureServer(server) {
      if (process.env[SERVICE_LOOPBACK_ENV] !== "1") return;
      ready = startBackend();
      void ready.catch(() => undefined); // A later request reports the typed unavailable state.
      server.httpServer?.once("close", stopBackend);
      server.middlewares.use((request, response, next) => {
        const url = request.url ?? "/";
        if (!PROXIED_PREFIXES.some((prefix) => url.startsWith(prefix))) {
          next();
          return;
        }
        // IMPORTANT: verify the browser hop before rewriting Origin for the adapter hop.
        // Otherwise a foreign page could borrow this fixture's owned-origin authority.
        const address = server.httpServer?.address();
        const authority =
          address && typeof address !== "string"
            ? `127.0.0.1:${address.port}`
            : "";
        if (
          request.headers.host !== authority ||
          (request.headers.origin !== undefined &&
            request.headers.origin !== `http://${authority}`)
        ) {
          response.writeHead(403);
          response.end("foreign_origin");
          return;
        }
        void (ready ?? Promise.reject(new Error("loopback not starting")))
          .then((port) => {
            // CRITICAL: Origin and Host must name the backend's own listener, and both together.
            // A fixed address here (the harness used to forward `http://127.0.0.1:8188`) no longer
            // matches the port the backend accepted the connection on, and every route refuses.
            const backendAuthority = `127.0.0.1:${port}`;
            const forwardedHeaders: Record<
              string,
              string | string[] | undefined
            > = {
              ...request.headers,
              origin: `http://${backendAuthority}`,
              host: backendAuthority,
            };
            const proxied = http.request(
              {
                host: "127.0.0.1",
                port,
                path: url,
                method: request.method,
                headers: forwardedHeaders,
              },
              (backendResponse) => {
                response.writeHead(
                  backendResponse.statusCode ?? 502,
                  backendResponse.headers,
                );
                backendResponse.pipe(response);
              },
            );
            proxied.on("error", () => {
              if (!response.headersSent) response.writeHead(502);
              response.end();
            });
            request.pipe(proxied);
          })
          .catch(() => {
            response.writeHead(503);
            response.end("loopback backend unavailable");
          });
      });
    },
  };
}
