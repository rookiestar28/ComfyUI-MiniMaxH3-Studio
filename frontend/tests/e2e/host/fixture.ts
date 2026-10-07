import { expect, test as base } from "@playwright/test";

import { registerCandidateInjection } from "./candidate";
import {
  HOST_LOG_PATH_ENV,
  markHostLog,
  scanHostLogSince,
  type HostLogScan,
} from "./logScan";

type HostFixtures = Readonly<{
  hostLogWindow: void;
  candidateInjection: void;
}>;

// IMPORTANT: candidate-bundle injection is registered per test by this auto fixture, never by a
// module-level `test.beforeEach` inside a shared host module. Playwright evaluates a shared
// module once per worker, so a hook declared at its top level attaches only to the first spec
// file that imports it; every later file in the same worker gets no hook, the candidate bundle
// is never routed, and assertCandidateBundleInjection fails with an undefined state. Every
// supplied-host journey must import `test` from this module (guarded by m17_00Static.test.ts).
export const test = base.extend<HostFixtures>({
  hostLogWindow: [
    async ({}, use, testInfo) => {
      let mark: ReturnType<typeof markHostLog> = null;
      let markFailure: string | null = null;
      try {
        mark = markHostLog();
        if (mark === null) markFailure = "log_not_supplied";
      } catch {
        markFailure = "log_mark_unavailable";
      }

      let scan: HostLogScan | null = null;
      let scanFailure: string | null = null;
      try {
        await use();
      } finally {
        if (mark !== null) {
          try {
            scan = scanHostLogSince(mark);
          } catch {
            scanFailure = "log_scan_unavailable";
          }
        }
        const receipt = {
          schema: "h3.context.host_log_scan_receipt.v1",
          test_id: testInfo.testId,
          retry: testInfo.retry,
          status:
            markFailure !== null || scanFailure !== null || scan === null
              ? "unavailable"
              : !scan.complete
                ? "incomplete"
                : scan.ownedCount > 0
                  ? "owned_error"
                  : "clean",
          complete: scan?.complete ?? false,
          incomplete_reason:
            markFailure ?? scanFailure ?? scan?.incompleteReason ?? null,
          owned_count: scan?.ownedCount ?? 0,
          foreign_count: scan?.foreignCount ?? 0,
          owned_examples: scan?.owned ?? [],
          foreign_examples: scan?.foreign ?? [],
          retained_examples_capped: scan?.truncated ?? false,
          bytes_clipped: scan?.clipped ?? false,
        } as const;
        await testInfo.attach("h3_host_log_scan", {
          body: Buffer.from(JSON.stringify(receipt), "utf8"),
          contentType: "application/json",
        });

        // CRITICAL: this fixture starts before candidate setup and finishes after dependent
        // cleanup. Missing or incomplete evidence is a row failure, never a quiet PASS; throwing
        // here also leaves an original setup/body/cleanup failure visible in Playwright's report.
        if (receipt.status === "unavailable" || receipt.status === "incomplete")
          throw new Error(
            `${HOST_LOG_PATH_ENV} scan ${receipt.status}: ${receipt.incomplete_reason}`,
          );
        if (receipt.status === "owned_error")
          throw new Error(
            `host log records ${receipt.owned_count} owned handler error(s): ${JSON.stringify(receipt.owned_examples)}`,
          );
      }
    },
    { auto: true },
  ],
  candidateInjection: [
    async ({ context, hostLogWindow: _hostLogWindow }, use, testInfo) => {
      await registerCandidateInjection(context, testInfo);
      await use();
    },
    { auto: true },
  ],
});

export { expect };
export type { BrowserContext, Locator, Page, TestInfo } from "@playwright/test";
