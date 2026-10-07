import {
  appendFileSync,
  mkdtempSync,
  renameSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  HOST_LOG_PATH_ENV,
  MAX_SCAN_BYTES,
  assertOwnedHostLogClean,
  markHostLog,
  scanHostLogSince,
} from "./e2e/host/logScan";

/**
 * The file half of the scan: which bytes a row is judged on.
 *
 * The classifier's own suite covers what a window means. This one covers how the window is chosen,
 * which is where a scan can report a row clean without having looked at it -- the failure the whole
 * design exists to prevent, arriving from the side nobody watches.
 */

const OWNED_BLOCK = [
  "Error handling request from 127.0.0.1",
  "Traceback (most recent call last):",
  '  File "X:\\HostRoot\\custom_nodes\\x\\comfyui_h3_context\\adapters\\routes.py", line 3, in handle',
  "RuntimeError: refused",
].join("\n");

describe("the window a supplied-host row is judged on", () => {
  let directory: string;
  let log: string;
  let previous: string | undefined;

  beforeEach(() => {
    directory = mkdtempSync(join(tmpdir(), "h3-log-scan-"));
    log = join(directory, "comfyui.log");
    previous = process.env[HOST_LOG_PATH_ENV];
  });

  afterEach(() => {
    if (previous === undefined) delete process.env[HOST_LOG_PATH_ENV];
    else process.env[HOST_LOG_PATH_ENV] = previous;
    rmSync(directory, { recursive: true, force: true });
  });

  it("judges only what the host wrote during the row", () => {
    writeFileSync(log, `${OWNED_BLOCK}\nbefore the row\n`, "utf8");
    process.env[HOST_LOG_PATH_ENV] = log;
    const mark = markHostLog()!;
    expect(mark.offset).toBeGreaterThan(0);
    // An error from before the mark belongs to whatever ran then, not to this row.
    expect(scanHostLogSince(mark).owned).toHaveLength(0);
    appendFileSync(log, `${OWNED_BLOCK}\n`, "utf8");
    expect(scanHostLogSince(mark).owned).toHaveLength(1);
  });

  it("uses verified bounded context to attribute a request line immediately before the mark", () => {
    writeFileSync(
      log,
      "POST /api/h3-context/v1/authoring/render HTTP/1.1\n",
      "utf8",
    );
    process.env[HOST_LOG_PATH_ENV] = log;
    const mark = markHostLog()!;
    appendFileSync(
      log,
      [
        "Error handling request from 127.0.0.1",
        "Traceback (most recent call last):",
        '  File "X:\\HostRoot\\custom_nodes\\other-pack\\server.py", line 1, in handle',
        "",
      ].join("\n"),
      "utf8",
    );
    const scan = scanHostLogSince(mark);
    expect(scan.ownedCount).toBe(1);
    expect(scan.owned[0]?.route).toBe("/h3-context/v1/authoring/render");
  });

  it("rejects a log that was truncated under the mark because the lost interval is unknowable", () => {
    writeFileSync(log, `${"padding\n".repeat(200)}`, "utf8");
    process.env[HOST_LOG_PATH_ENV] = log;
    const mark = markHostLog()!;
    writeFileSync(log, `restarted\n${OWNED_BLOCK}\n`, "utf8");
    expect(scanHostLogSince(mark)).toMatchObject({
      complete: false,
      incompleteReason: "log_truncated",
    });
    expect(() => assertOwnedHostLogClean(mark, "truncated row")).toThrow(
      /incomplete.*log_truncated/,
    );
  });

  it("takes an empty window when the host wrote nothing", () => {
    writeFileSync(log, "quiet\n", "utf8");
    process.env[HOST_LOG_PATH_ENV] = log;
    const mark = markHostLog()!;
    const scan = scanHostLogSince(mark);
    expect(scan).toMatchObject({ owned: [], foreign: [], truncated: false });
  });

  it("refuses a mark when the supplied log does not exist yet", () => {
    process.env[HOST_LOG_PATH_ENV] = log;
    expect(() => markHostLog()).toThrow(/existing readable host log/);
  });

  it("says so when the row wrote more than the window and only its tail was read", () => {
    // A clipped window is not a clean one. The bound is right -- a host left running for days
    // must not be loaded whole to read its last megabytes -- but it drops findings with the bytes,
    // so the row has to be able to say its verdict covers part of what happened.
    writeFileSync(log, "", "utf8");
    process.env[HOST_LOG_PATH_ENV] = log;
    const mark = markHostLog()!;
    appendFileSync(log, `${OWNED_BLOCK}\n`, "utf8");
    appendFileSync(log, "x".repeat(MAX_SCAN_BYTES + 1024), "utf8");
    const scan = scanHostLogSince(mark);
    expect(scan.clipped).toBe(true);
    expect(scan.complete).toBe(false);
    // The early finding is outside the window, which is the cost the flag exists to report.
    expect(scan.owned).toHaveLength(0);
    expect(() => assertOwnedHostLogClean(mark, "clipped row")).toThrow(
      /incomplete.*window_exceeds_read_limit/,
    );
    // And an ordinary window says so too.
    writeFileSync(log, "", "utf8");
    const second = markHostLog()!;
    appendFileSync(log, `${OWNED_BLOCK}\n`, "utf8");
    expect(scanHostLogSince(second).clipped).toBe(false);
  });

  it("fails rather than reporting clean when the log cannot be read", () => {
    process.env[HOST_LOG_PATH_ENV] = log;
    writeFileSync(log, "", "utf8");
    const mark = markHostLog()!;
    rmSync(log, { force: true });
    // A deleted, moved or locked log excuses nothing: it is the absent-scan case with a file name.
    expect(() => scanHostLogSince(mark)).toThrow();
    expect(() => assertOwnedHostLogClean(mark, "fit fixtures")).toThrow();
  });

  it("rejects a same-or-larger replacement instead of scanning an unrelated file", () => {
    writeFileSync(log, "original log\n", "utf8");
    process.env[HOST_LOG_PATH_ENV] = log;
    const mark = markHostLog()!;
    const replacement = join(directory, "replacement.log");
    writeFileSync(replacement, `${"replacement\n".repeat(20)}`, "utf8");
    rmSync(log);
    renameSync(replacement, log);
    const scan = scanHostLogSince(mark);
    expect(scan.complete).toBe(false);
    // GUARD: filesystem identity can alias across replacement; the marked digest must still
    // reject the interval. Requiring only the identity reason makes the Windows gate flaky.
    expect(["log_replaced", "marked_prefix_changed"]).toContain(
      scan.incompleteReason,
    );
    expect(() => assertOwnedHostLogClean(mark, "replaced row")).toThrow(
      /incomplete.*(?:log_replaced|marked_prefix_changed)/,
    );
  });

  it("rejects truncate-and-regrow even when the final size reaches the old offset", () => {
    const original = `${"private-prefix-a\n".repeat(32)}`;
    writeFileSync(log, original, "utf8");
    process.env[HOST_LOG_PATH_ENV] = log;
    const mark = markHostLog()!;
    writeFileSync(log, "different-prefix\n".repeat(64), "utf8");
    const scan = scanHostLogSince(mark);
    expect(scan).toMatchObject({
      complete: false,
      incompleteReason: "marked_prefix_changed",
    });
  });

  it("has no mark at all when no log was supplied", () => {
    delete process.env[HOST_LOG_PATH_ENV];
    expect(markHostLog()).toBeNull();
  });

  it("fails a row that cannot scan, instead of passing it", () => {
    expect(() => assertOwnedHostLogClean(null, "fit fixtures")).toThrow(
      /H3_CONTEXT_HOST_LOG is required/,
    );
  });

  it("fails a row whose window holds an owned handler error", () => {
    writeFileSync(log, "", "utf8");
    process.env[HOST_LOG_PATH_ENV] = log;
    const mark = markHostLog()!;
    appendFileSync(log, `${OWNED_BLOCK}\n`, "utf8");
    expect(() => assertOwnedHostLogClean(mark, "proxy lease")).toThrow(
      /proxy lease: the host log records 1 owned handler error/,
    );
  });

  it("passes a row whose window holds only another pack's failure", () => {
    writeFileSync(log, "", "utf8");
    process.env[HOST_LOG_PATH_ENV] = log;
    const mark = markHostLog()!;
    appendFileSync(
      log,
      [
        "Error handling request from 127.0.0.1",
        "Traceback (most recent call last):",
        '  File "X:\\HostRoot\\custom_nodes\\other-pack\\server.py", line 1, in handle',
        "",
      ].join("\n"),
      "utf8",
    );
    const scan = assertOwnedHostLogClean(mark, "fit fixtures");
    expect(scan.owned).toHaveLength(0);
    expect(scan.foreign).toHaveLength(1);
  });
});
