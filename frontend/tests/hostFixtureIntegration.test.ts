import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

type JsonResult = {
  status: string;
  errors?: { message?: string }[];
  attachments?: { name: string; contentType: string; body?: string }[];
};

type JsonSpec = {
  title: string;
  id: string;
  tests: { annotations: { type: string }[]; results: JsonResult[] }[];
};

function specsIn(suites: any[]): JsonSpec[] {
  return suites.flatMap((suite) => [
    ...(suite.specs ?? []),
    ...specsIn(suite.suites ?? []),
  ]);
}

describe("automatic supplied-host log scan fixture", () => {
  let directory: string;
  let report: any;
  let cleanReport: any;

  beforeAll(() => {
    directory = mkdtempSync(join(tmpdir(), "h3-host-fixture-"));
    const log = join(directory, "host.log");
    const bundle = resolve(
      import.meta.dirname,
      "../../comfyui_h3_context/web/h3-context-sidebar.js",
    );
    const bundleSha256 = createHash("sha256")
      .update(readFileSync(bundle))
      .digest("hex");
    writeFileSync(log, "fixture start\n", "utf8");
    const run = (extra: string[] = []) => {
      let output = "";
      try {
        output = execFileSync(
          process.execPath,
          [
            resolve(
              import.meta.dirname,
              "../node_modules/@playwright/test/cli.js",
            ),
            "test",
            "--config",
            "playwright.host-fixture.config.ts",
            ...extra,
          ],
          {
            cwd: resolve(import.meta.dirname, ".."),
            env: {
              ...process.env,
              H3_CONTEXT_HOST_LOG: log,
              H3_CONTEXT_HOST_URL: "http://127.0.0.1:9",
              H3_CONTEXT_CANDIDATE_BACKEND_MODE: "frontend_only_host_graph",
              H3_CONTEXT_CANDIDATE_BUNDLE_PATH: bundle,
              H3_CONTEXT_CANDIDATE_BUNDLE_SHA256: bundleSha256,
            },
            encoding: "utf8",
            stdio: ["ignore", "pipe", "pipe"],
          },
        );
      } catch (error: any) {
        output = String(error.stdout ?? "");
      }
      return JSON.parse(output);
    };
    report = run();
    writeFileSync(log, "clean proof start\n", "utf8");
    cleanReport = run(["--grep", "clean row"]);
  }, 60_000);

  afterAll(() => rmSync(directory, { recursive: true, force: true }));

  it("scans every executed test across two spec files in one worker", () => {
    expect(report.config.workers).toBe(1);
    expect(report.config.projects[0].retries).toBe(0);
    const specs = specsIn(report.suites);
    expect(specs.map((spec) => spec.title)).toEqual([
      "first file clean row",
      "setup failure is still scanned",
      "body failure is still scanned",
      "second file clean row",
      "cleanup and owned log failures stay separate",
      "a skipped row creates no fake receipt",
    ]);
    const executed = specs.filter(
      (spec) => spec.tests[0]?.results[0]?.status !== "skipped",
    );
    const receipts = executed.map((spec) => {
      expect(spec.tests[0]?.annotations.map((item) => item.type)).toContain(
        "candidate-backend-runtime",
      );
      const attachments = spec.tests[0]?.results[0]?.attachments ?? [];
      const attachment = attachments.find(
        (item) => item.name === "h3_host_log_scan",
      );
      expect(attachment?.contentType).toBe("application/json");
      return JSON.parse(
        Buffer.from(attachment!.body!, "base64").toString("utf8"),
      );
    });
    expect(new Set(receipts.map((item) => item.test_id)).size).toBe(5);
    expect(receipts.every((item) => item.complete)).toBe(true);
    const cleanResults = specsIn(cleanReport.suites).map(
      (spec) => spec.tests[0].results[0],
    );
    expect(cleanResults).toHaveLength(2);
    expect(cleanResults.map((result) => result.status)).toEqual([
      "passed",
      "passed",
    ]);
    expect(
      new Set(cleanResults.map((result: any) => result.workerIndex)),
    ).toEqual(new Set([0]));
  });

  it("preserves setup, body and cleanup failures while classifying foreign and owned output", () => {
    const specs = specsIn(report.suites);
    const named = Object.fromEntries(specs.map((spec) => [spec.title, spec]));
    expect(
      named["setup failure is still scanned"].tests[0].results[0].errors,
    ).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          message: expect.stringContaining("synthetic setup failure"),
        }),
      ]),
    );
    expect(
      named["body failure is still scanned"].tests[0].results[0].errors,
    ).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          message: expect.stringContaining("failure preserved"),
        }),
      ]),
    );
    const cleanup =
      named["cleanup and owned log failures stay separate"].tests[0].results[0];
    expect(cleanup.errors).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          message: expect.stringContaining("synthetic cleanup failure"),
        }),
        expect.objectContaining({
          message: expect.stringContaining("owned handler error"),
        }),
      ]),
    );
    const receipt = JSON.parse(
      Buffer.from(
        cleanup.attachments!.find((item) => item.name === "h3_host_log_scan")!
          .body!,
        "base64",
      ).toString("utf8"),
    );
    expect(receipt).toMatchObject({ status: "owned_error", owned_count: 1 });
    expect(named["first file clean row"].tests[0].results[0].status).toBe(
      "passed",
    );
    expect(named["second file clean row"].tests[0].results[0].status).toBe(
      "passed",
    );
    expect(
      named["a skipped row creates no fake receipt"].tests[0].results[0]
        .attachments ?? [],
    ).toHaveLength(0);
  });
});
