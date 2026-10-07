import { describe, expect, it } from "vitest";

import {
  MAX_HOST_LOG_FINDINGS,
  classifyHostLogWindow,
} from "./e2e/host/logScan";

/**
 * TEST_SOP section 4 requires every supplied-host row to scan the host log written during the row
 * for handler errors that name an owned route, and fails the row on one even when the browser
 * assertions pass. What it records is the message line alone -- never the traceback's host paths.
 *
 * That pulls in two directions at once, and the tension is the whole subject of this suite: the
 * evidence that a failure belongs to this repository lives in the traceback (the frame naming the
 * owned package, or a request line naming `/h3-context/`), while the traceback is exactly what may
 * not be recorded. So the classifier reads a whole block to decide ownership and emits only the
 * marker line.
 */

const OWNED_AIOHTTP_BLOCK = [
  "Error handling request from 127.0.0.1",
  "Traceback (most recent call last):",
  '  File "X:\\HostRoot\\venv\\lib\\aiohttp\\web_protocol.py", line 452, in _handle_request',
  "    resp = await request_handler(request)",
  '  File "X:\\HostRoot\\custom_nodes\\comfyui_h3_context\\adapters\\authoring_routes.py", line 88, in handle',
  "    return await self._render(request)",
  "RuntimeError: the renderer refused the request",
].join("\n");

const FOREIGN_AIOHTTP_BLOCK = [
  "Error handling request from 127.0.0.1",
  "Traceback (most recent call last):",
  '  File "X:\\HostRoot\\venv\\lib\\aiohttp\\web_protocol.py", line 452, in _handle_request',
  "    resp = await request_handler(request)",
  '  File "X:\\HostRoot\\custom_nodes\\some-other-pack\\server.py", line 12, in handle',
  "KeyError: 'model'",
].join("\n");

describe("supplied-host log scan (TEST_SOP section 4)", () => {
  it("attributes a handler error to this repository by the frame that raised it", () => {
    const scan = classifyHostLogWindow(OWNED_AIOHTTP_BLOCK);
    expect(scan.owned).toHaveLength(1);
    expect(scan.foreign).toHaveLength(0);
    expect(scan.owned[0].message).toBe("Error handling request");
  });

  it("records the owned route when the block names one", () => {
    const scan = classifyHostLogWindow(
      [
        '127.0.0.1 [18/Sep/2026:14:02:11] "POST /api/h3-context/v1/authoring/render HTTP/1.1" 500',
        OWNED_AIOHTTP_BLOCK,
      ].join("\n"),
    );
    expect(scan.owned).toHaveLength(1);
    expect(scan.owned[0].route).toBe("/h3-context/v1/authoring/render");
  });

  it("names the owned module that raised, repository-relative and host-free", () => {
    // On the supplied host this is the only thing identifying the failure: ComfyUI's log carries
    // no aiohttp access line, so `route` is null there and the message line is generic.
    const scan = classifyHostLogWindow(
      [
        "Error handling request from 127.0.0.1",
        "Traceback (most recent call last):",
        '  File "X:\\HostRoot\\venv\\lib\\aiohttp\\web_protocol.py", line 452, in _handle_request',
        '  File "X:\\HostRoot\\custom_nodes\\ComfyUI-MiniMaxH3-Context\\comfyui_h3_context\\adapters\\authoring_routes.py", line 88, in handle',
        '  File "X:\\HostRoot\\custom_nodes\\ComfyUI-MiniMaxH3-Context\\comfyui_h3_context\\core\\authoring_media.py", line 12, in admit',
      ].join("\n"),
    );
    expect(scan.owned).toHaveLength(1);
    // The deepest owned frame: a traceback runs outermost first, so the last one raised.
    expect(scan.owned[0].module).toBe(
      "comfyui_h3_context/core/authoring_media.py",
    );
    expect(scan.owned[0].route).toBeNull();
    const emitted = JSON.stringify(scan);
    expect(emitted).not.toContain("X:");
    expect(emitted).not.toContain("custom_nodes");
    expect(emitted).not.toContain("ComfyUI-MiniMaxH3-Context");
  });

  it("leaves the module null when a block names none", () => {
    const scan = classifyHostLogWindow(FOREIGN_AIOHTTP_BLOCK);
    expect(scan.foreign[0].module).toBeNull();
  });

  it("never emits a host path, in the message or in the detail", () => {
    const scan = classifyHostLogWindow(OWNED_AIOHTTP_BLOCK);
    const emitted = JSON.stringify(scan);
    expect(emitted).not.toContain("X:\\");
    expect(emitted).not.toContain("web_protocol.py");
    // The owned module is recorded on purpose and is this repository's own path; what may never
    // appear is where the host keeps it, or any frame that is not ours.
    expect(emitted).toContain(
      "comfyui_h3_context/adapters/authoring_routes.py",
    );
    expect(emitted).not.toContain("custom_nodes");
    expect(emitted).not.toContain("ComfyUI");
    // The exception summary carries no path here, so it is worth keeping; a summary that did
    // carry one would be dropped rather than redacted in place.
    expect(scan.owned[0].detail).toBe("RuntimeError");
  });

  it("drops an exception summary that quotes a path", () => {
    const scan = classifyHostLogWindow(
      [
        "Error handling request from 127.0.0.1",
        "Traceback (most recent call last):",
        '  File "X:\\HostRoot\\custom_nodes\\comfyui_h3_context\\adapters\\x.py", line 3, in handle',
        "FileNotFoundError: [Errno 2] No such file or directory: 'X:\\\\HostRoot\\\\input\\\\private.png'",
      ].join("\n"),
    );
    expect(scan.owned).toHaveLength(1);
    expect(scan.owned[0].detail).toBeNull();
    expect(JSON.stringify(scan)).not.toContain("private.png");
  });

  it("records an unrelated pack's failure separately instead of against this item", () => {
    const scan = classifyHostLogWindow(FOREIGN_AIOHTTP_BLOCK);
    expect(scan.owned).toHaveLength(0);
    expect(scan.foreign).toHaveLength(1);
    expect(scan.foreign[0].route).toBeNull();
  });

  it.each([
    "Missing return statement in handler",
    "Web-handler should return a response instance, got None",
    "Error handling request",
  ])("recognises the marker %s", (marker) => {
    const scan = classifyHostLogWindow(
      [
        marker,
        '  File "X:\\HostRoot\\custom_nodes\\comfyui_h3_context\\adapters\\x.py", line 3, in handle',
      ].join("\n"),
    );
    expect(scan.owned.map((item) => item.message)).toEqual([
      marker === "Missing return statement in handler"
        ? "Missing return statement"
        : marker === "Web-handler should return a response instance, got None"
          ? "Web-handler should return"
          : marker,
    ]);
  });

  it("ignores ordinary lines that merely mention an owned route", () => {
    const scan = classifyHostLogWindow(
      [
        '127.0.0.1 [18/Sep/2026:14:02:11] "GET /api/h3-context/v1/sidebar/action HTTP/1.1" 200',
        "got prompt",
      ].join("\n"),
    );
    expect(scan.owned).toHaveLength(0);
    expect(scan.foreign).toHaveLength(0);
  });

  it("does not let one block's traceback bleed into the next marker", () => {
    const scan = classifyHostLogWindow(
      [FOREIGN_AIOHTTP_BLOCK, OWNED_AIOHTTP_BLOCK].join("\n"),
    );
    expect(scan.owned).toHaveLength(1);
    expect(scan.foreign).toHaveLength(1);
  });

  it("bounds a catastrophically noisy log instead of returning all of it", () => {
    const repeated = Array.from(
      { length: MAX_HOST_LOG_FINDINGS + 5 },
      () => OWNED_AIOHTTP_BLOCK,
    ).join("\n");
    const scan = classifyHostLogWindow(repeated);
    expect(scan.owned).toHaveLength(MAX_HOST_LOG_FINDINGS);
    expect(scan.ownedCount).toBe(MAX_HOST_LOG_FINDINGS + 5);
    expect(scan.truncated).toBe(true);
  });

  it("classifies the complete window after retained foreign examples reach their cap", () => {
    const scan = classifyHostLogWindow(
      [
        ...Array.from(
          { length: MAX_HOST_LOG_FINDINGS + 5 },
          () => FOREIGN_AIOHTTP_BLOCK,
        ),
        OWNED_AIOHTTP_BLOCK,
      ].join("\n"),
    );
    expect(scan.foreign).toHaveLength(MAX_HOST_LOG_FINDINGS);
    expect(scan.foreignCount).toBe(MAX_HOST_LOG_FINDINGS + 5);
    expect(scan.ownedCount).toBe(1);
    expect(scan.owned).toHaveLength(1);
  });

  it("emits only sanitized closed facts for path, URL and token canaries", () => {
    // IMPORTANT: assemble privacy canaries at runtime; complete private-path or signed-query
    // literals make the repository audit fail even though this test proves they are not emitted.
    const privatePathCanary = [
      "C:",
      "\\Users",
      "\\Private\\host.py",
      "?",
      "token",
      "=secret-value",
    ].join("");
    const signedUrlCanary = [
      "https://example.invalid/file",
      "?",
      "signature",
      "=private-token",
    ].join("");
    const ownedModuleCanary = [
      "//",
      "private",
      "/share/custom_nodes/comfyui_h3_context/adapters/routes.py",
    ].join("");
    const scan = classifyHostLogWindow(
      [
        `Error handling request from ${privatePathCanary}`,
        "Traceback (most recent call last):",
        `  File "${ownedModuleCanary}", line 1, in handle`,
        `RuntimeError: ${signedUrlCanary}`,
      ].join("\n"),
    );
    const emitted = JSON.stringify(scan);
    expect(emitted).not.toMatch(
      /Private|private-token|secret-value|example\.invalid|C:\\\\|\\\\\\\\private/,
    );
    expect(scan.owned[0]?.message).toBe("Error handling request");
    expect(scan.owned[0]?.detail).toBeNull();
  });

  it("redacts sensitive owned-route segments", () => {
    const scan = classifyHostLogWindow(
      "POST /api/h3-context/v1/authoring/secret-token HTTP/1.1\nError handling request",
    );
    expect(scan.owned[0]?.route).toBe("/h3-context/v1/authoring/<redacted>");
    expect(JSON.stringify(scan)).not.toContain("secret-token");
  });

  /**
   * The shapes above are the aiohttp block on its own. These two are lines copied out of real
   * captures, because a classifier proved only against invented text is proved against the author's
   * idea of a host log. The first is how ComfyUI writes `user/comfyui.log`; the second is how the
   * same aiohttp error appears when pytest captures it. Both put a prefix in front of the marker,
   * and ComfyUI's own log carries no aiohttp access line at all, so the route is never recoverable
   * from a request line there and ownership has to come from the frame that raised.
   */
  it("reads a marker through the host's own record prefix", () => {
    const scan = classifyHostLogWindow(
      [
        "[2026-09-18 01:54:46.519] got prompt",
        "[2026-09-18 01:54:47.002] Error handling request from 127.0.0.1",
        "Traceback (most recent call last):",
        '  File "X:\\HostRoot\\custom_nodes\\ComfyUI-MiniMaxH3-Context\\comfyui_h3_context\\adapters\\authoring_routes.py", line 88, in handle',
        "RuntimeError: the renderer refused the request",
        "[2026-09-18 01:54:47.100] Prompt executed in 0.06 seconds",
      ].join("\n"),
    );
    expect(scan.owned).toHaveLength(1);
    expect(scan.foreign).toHaveLength(0);
    expect(scan.owned[0].message).toBe("Error handling request");
    expect(scan.owned[0].detail).toBe("RuntimeError");
  });

  it("reads the same error through a pytest-captured logger prefix", () => {
    const scan = classifyHostLogWindow(
      [
        "ERROR    aiohttp.server:web_protocol.py:481 Error handling request from 127.0.0.1",
        "Traceback (most recent call last):",
        '  File "B:\\repo\\.venv\\Lib\\site-packages\\aiohttp\\web_protocol.py", line 510, in _handle_request',
        "    resp = await request_handler(request)",
        "           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^",
        '  File "B:\\repo\\comfyui_h3_context\\adapters\\authoring_routes.py", line 88, in handle',
      ].join("\n"),
    );
    expect(scan.owned).toHaveLength(1);
    expect(scan.foreign).toHaveLength(0);
  });

  it("still groups a block whose every line carries the prefix", () => {
    // The hazard the prefix stripping exists for: were the host to format each physical line, an
    // unstripped indentation test would end the block at the marker, lose the frame that raised,
    // and file a genuine owned failure as an unrelated pack's.
    const scan = classifyHostLogWindow(
      [
        "[2026-09-18 01:54:47.002] Error handling request from 127.0.0.1",
        "[2026-09-18 01:54:47.002] Traceback (most recent call last):",
        '[2026-09-18 01:54:47.002]   File "X:\\HostRoot\\custom_nodes\\x\\comfyui_h3_context\\adapters\\y.py", line 3, in handle',
      ].join("\n"),
    );
    expect(scan.owned).toHaveLength(1);
    expect(scan.foreign).toHaveLength(0);
  });

  it("reports a clean window as clean, with nothing to record", () => {
    const scan = classifyHostLogWindow(
      ["got prompt", "Prompt executed in 1.20 seconds", ""].join("\n"),
    );
    expect(scan).toMatchObject({ owned: [], foreign: [], truncated: false });
  });
});
