import { describe, expect, it } from "vitest";

// @ts-expect-error -- the gatherer is a plain ESM script with no type declarations.
import { collect, gatherOutcome } from "./e2e/helpers/nleHardeningGatherer.mjs";

/**
 * B-M2545-45: the hardening gatherer must not report success for a lane that failed.
 *
 * The shape that slipped through was a VALID report. A malformed one was already handled -- the
 * gatherer throws "no Playwright JSON report" -- so the untested case was the one where parsing
 * succeeds and the lane did not: `g10-hardening-acceptance` printed
 * `statuses {"passed":100,"failed":1}; playwright exit 1` and then exited 0, and the shell runner
 * propagated that zero. A lane with a failed row and no render observations therefore read as a
 * completed gather from the outside.
 *
 * These cases drive the outcome rule directly, because a wrapper around a test runner is only
 * interesting in the case where its runner fails, and lanes that pass exercise nothing about it.
 */

function report(specs: unknown[]) {
  return { suites: [{ title: "journeys", specs, suites: [] }] };
}

function spec(
  title: string,
  status: string,
  errors: { message: string }[] = [],
) {
  return {
    file: "nleHardeningRender.spec.ts",
    title,
    tests: [{ results: [{ status, duration: 1, attachments: [], errors }] }],
  };
}

describe("the hardening gatherer's outcome", () => {
  it("refuses to call a valid report containing a failed test a success", () => {
    const { tests } = collect(
      report([spec("backend companion renders eight jobs", "failed")]),
    );
    // Both halves of g10's shape, and each on its own: the exit status alone, and the report alone.
    expect(gatherOutcome({ playwrightStatus: 1, tests }).ok).toBe(false);
    expect(gatherOutcome({ playwrightStatus: 0, tests }).ok).toBe(false);
  });

  it("names the failing test in its reasons, so the exit code is actionable", () => {
    const { tests } = collect(
      report([spec("backend companion renders eight jobs", "failed")]),
    );
    const outcome = gatherOutcome({ playwrightStatus: 1, tests });
    expect(outcome.reasons).toContain("playwright exited 1");
    expect(
      outcome.reasons.some((reason: string) =>
        reason.includes("backend companion renders eight jobs"),
      ),
    ).toBe(true);
  });

  it("retains the failing test's own message instead of discarding it", () => {
    const { failures } = collect(
      report([
        spec("backend companion renders eight jobs", "failed", [
          { message: "Expected: <= 1073741824\nReceived:    2661179392" },
        ]),
      ]),
    );
    expect(failures).toHaveLength(1);
    expect(failures[0].test_id).toContain(
      "backend companion renders eight jobs",
    );
    expect(failures[0].errors[0]).toContain("2661179392");
  });

  it("treats a test that never ran as unsuccessful, never as a pass", () => {
    // A measurement that is absent must not become a zero. An empty results array is exactly how a
    // test that never ran reaches `collect`.
    const { tests } = collect(
      report([
        { file: "nleHardeningRender.spec.ts", title: "never ran", tests: [] },
      ]),
    );
    expect(tests[0].status).toBe("not_run");
    expect(gatherOutcome({ playwrightStatus: 0, tests }).ok).toBe(false);
  });

  it("fails on a nonzero exit even when every reported test passed", () => {
    // A global setup or teardown failure leaves a clean-looking report behind.
    const { tests } = collect(report([spec("a passing row", "passed")]));
    expect(gatherOutcome({ playwrightStatus: 1, tests }).ok).toBe(false);
  });

  it("is not vacuous: the behaviour it replaced reports success for the same input", () => {
    // The previous gatherer had no outcome rule at all. It printed `playwright exit ${run.status}`
    // into its summary and returned, so its effective rule was "a document was written". Stated
    // here as running code, so the regression's before/after is a comparison rather than a claim
    // about deleted lines, and so a future edit cannot quietly restore it.
    const previousBehaviour = (document: { tests: unknown[] }) => ({
      ok: Boolean(document),
      reasons: [] as string[],
    });
    const { tests } = collect(
      report([spec("backend companion renders eight jobs", "failed")]),
    );
    expect(previousBehaviour({ tests }).ok).toBe(true);
    expect(gatherOutcome({ playwrightStatus: 1, tests }).ok).toBe(false);
  });

  it("accepts a clean lane, including skips", () => {
    const { tests, failures } = collect(
      report([
        spec("a passing row", "passed"),
        spec("a skipped row", "skipped"),
      ]),
    );
    expect(failures).toHaveLength(0);
    expect(gatherOutcome({ playwrightStatus: 0, tests })).toEqual({
      ok: true,
      reasons: [],
    });
  });
});
