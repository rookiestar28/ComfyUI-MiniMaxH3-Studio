// D45-04 (plan section 11.2): the browser-stage gatherer's exit decision and its retained
// diagnostics, pinned without running Playwright.
//
// Before this, the normal completion path recorded the child's exit status, printed a warning and
// still exited 0. A stage document produced by failed child tests -- or one missing required
// observations outright -- was therefore indistinguishable from a clean sweep to anything reading
// the exit code, which is how two consecutive sweeps reported the same 13 BLOCKED shell rows and
// were both read as work still in flight.
import { describe, expect, test } from "vitest";

import {
  redactPaths,
  stageOutcome,
  type BrowserStageRun,
} from "./e2e/helpers/nleSemanticBrowserStageGatherer.mjs";

type Row = { case_id: string; executed: boolean; missing: string[] };

const row = (caseId: string, executed = true, missing: string[] = []): Row => ({
  case_id: caseId,
  executed,
  missing,
});

const complete = (runs: readonly BrowserStageRun[], rows: ReadonlyArray<Row>) =>
  stageOutcome({ scope: "complete", runs, rows });

const clean: readonly BrowserStageRun[] = [
  { name: "shell", status: 0, signal: null },
];

describe("the browser stage gatherer's exit decision", () => {
  test("passes only when every run exited 0 and every row carries its observations", () => {
    const outcome = complete(clean, [row("ui_invariant.overlay_open")]);
    expect(outcome.exitCode).toBe(0);
    expect(outcome.reasons).toEqual([]);
    expect(outcome.counts).toEqual({ rows: 1, notRun: 0, blocked: 0 });
  });

  test("fails when a child run exited nonzero, even though the report was written", () => {
    const outcome = complete(
      [{ name: "render-and-browser", status: 1, signal: null }],
      [row("render_and_browser.flat_composite")],
    );
    expect(outcome.exitCode).toBe(1);
    expect(outcome.reasons).toContain("the render-and-browser run exited 1");
  });

  test("fails when a child run was terminated by a signal", () => {
    const outcome = complete(
      [{ name: "shell", status: null, signal: "SIGTERM" }],
      [row("ui_invariant.overlay_open")],
    );
    expect(outcome.exitCode).toBe(1);
    expect(outcome.reasons).toContain(
      "the shell run was terminated by SIGTERM",
    );
  });

  test("fails when a row produced no attachment at all", () => {
    const outcome = complete(clean, [
      row("ui_invariant.overlay_open"),
      row("ui_invariant.function_switch", false),
    ]);
    expect(outcome.exitCode).toBe(1);
    expect(outcome.counts.notRun).toBe(1);
    expect(outcome.reasons.join(" ")).toContain("ui_invariant.function_switch");
  });

  test("fails when an executed row is missing a required observation", () => {
    const outcome = complete(clean, [
      row("render_and_browser.opacity", true, ["output frame 0"]),
    ]);
    expect(outcome.exitCode).toBe(1);
    expect(outcome.counts.blocked).toBe(1);
    expect(outcome.reasons.join(" ")).toContain(
      "missing required observations",
    );
  });

  test("names both failure classes at once and truncates a long list", () => {
    const rows = [
      ...Array.from({ length: 7 }, (_, index) =>
        row(`ui_invariant.case_${index}`, false),
      ),
      row("render_and_browser.blend", true, ["output frame 0"]),
    ];
    const outcome = complete(clean, rows);
    expect(outcome.exitCode).toBe(1);
    expect(outcome.counts).toEqual({ rows: 8, notRun: 7, blocked: 1 });
    expect(
      outcome.reasons.some((reason: string) => reason.endsWith(", ...")),
    ).toBe(true);
  });

  test("carries the scope, so a shell-only diagnostic cannot claim a complete stage", () => {
    const shellOnly = stageOutcome({
      scope: "shell_only",
      runs: clean,
      rows: [row("ui_invariant.overlay_open")],
    });
    expect(shellOnly.scope).toBe("shell_only");
    expect(shellOnly.exitCode).toBe(0);
  });
});

describe("the retained diagnostics", () => {
  test("replace the repository, frontend, temp and home paths with placeholders", () => {
    const root = "/w/repo/frontend";
    const text = [
      "at /w/repo/frontend/tests/e2e/journeys/x.spec.ts:1:1",
      "opened /w/repo/scripts/nle_semantic_render.py",
    ].join("\n");
    const redacted = redactPaths(text, root);
    expect(redacted).toContain("<frontend>/tests/e2e/journeys/x.spec.ts");
    expect(redacted).toContain("<repo>/scripts/nle_semantic_render.py");
    expect(redacted).not.toContain("/w/repo");
  });

  test("redact a Windows path in both its slash spellings", () => {
    const root = "C:\\work\\repo\\frontend";
    const redacted = redactPaths(
      "C:\\work\\repo\\frontend\\a.ts and C:/work/repo/frontend/b.ts",
      root,
    );
    expect(redacted).not.toContain("work");
    expect(redacted.split("<frontend>").length - 1).toBe(2);
  });
});
