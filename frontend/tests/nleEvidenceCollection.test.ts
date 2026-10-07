// @vitest-environment node

import { execFile } from "node:child_process";
import { readFileSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { promisify } from "node:util";
import { expect, it } from "vitest";

const execute = promisify(execFile);
const frontend = resolve(__dirname, "..");
const manifest = JSON.parse(
  readFileSync(
    join(
      frontend,
      "..",
      "governance",
      "contracts",
      "nle_control_coverage_manifest_v1.json",
    ),
    "utf8",
  ),
) as {
  command_rows: { operation_id: string; evidence: Record<string, string> }[];
  non_command_rows: {
    operation_id: string;
    evidence: Record<string, string>;
  }[];
};

// M25-20's shell-invariant rows cite executable cases in the same `<file>::<name>` form, so they
// are resolved by the same collection rather than by a second, weaker check that only asks whether
// the file exists.
const conformance = JSON.parse(
  readFileSync(
    join(
      frontend,
      "..",
      "governance",
      "contracts",
      "nle_semantic_conformance_manifest_v1.json",
    ),
    "utf8",
  ),
) as { ui_invariant_rows: { case_id: string; evidence: string }[] };

type CollectedSuite = {
  title: string;
  file?: string;
  suites?: CollectedSuite[];
  specs?: {
    title: string;
    file: string;
    tests: { expectedStatus: string }[];
  }[];
};

async function collect(cli: string, args: string[]): Promise<unknown> {
  const { stdout } = await execute(
    process.execPath,
    [join(frontend, "node_modules", cli), ...args],
    {
      cwd: frontend,
      encoding: "utf8",
      timeout: 45_000,
      maxBuffer: 4 * 1024 * 1024,
      // No shell, browser launch or test execution: only the pinned runners' collection phase.
      env: { ...process.env, NO_COLOR: "1", FORCE_COLOR: "0" },
    },
  );
  return JSON.parse(stdout);
}

it("resolves every evidence ID against the actual runners' expanded, non-skipped collection", async () => {
  const references = [
    ...[...manifest.command_rows, ...manifest.non_command_rows].flatMap((row) =>
      Object.entries(row.evidence).map(([facet, reference]) => ({
        owner: `${row.operation_id}.${facet}`,
        reference,
      })),
    ),
    ...conformance.ui_invariant_rows.map((row) => ({
      owner: row.case_id,
      reference: row.evidence,
    })),
  ];
  const files = [
    ...new Set(references.map(({ reference }) => reference.split("::")[0]!)),
  ];
  const unitFiles = files.filter((file) => /\.test\.tsx?$/.test(file));
  const browserFiles = files.filter((file) => file.endsWith(".spec.ts"));
  const [unit, browser] = await Promise.all([
    collect("vitest/vitest.mjs", [
      "list",
      ...unitFiles.map((file) => `tests/${file}`),
      "--json",
      "--maxWorkers=1",
    ]),
    collect("@playwright/test/cli.js", [
      "test",
      "--config=playwright.config.ts",
      "--list",
      "--reporter=json",
      ...browserFiles,
    ]),
  ]);
  const ids = new Set<string>();
  for (const row of unit as { name: string; file: string }[]) {
    ids.add(
      `${relative(join(frontend, "tests"), row.file).replaceAll("\\", "/")}::${row.name}`,
    );
  }
  function visit(suite: CollectedSuite, parents: string[]): void {
    for (const spec of suite.specs ?? []) {
      if (spec.tests.some((test) => test.expectedStatus === "passed")) {
        ids.add(
          `e2e/${spec.file.replaceAll("\\", "/")}::${[...parents, spec.title].join(" > ")}`,
        );
      }
    }
    for (const child of suite.suites ?? [])
      visit(child, [...parents, child.title]);
  }
  const report = browser as { suites: CollectedSuite[]; errors?: unknown[] };
  expect(report.errors ?? []).toEqual([]);
  for (const suite of report.suites) visit(suite, []);
  expect(ids.size).toBeGreaterThan(0);
  const unresolved = references.filter(({ reference }) => !ids.has(reference));
  expect(unresolved).toEqual([]);
}, 60_000);

// M25-21: the hardening manifest names one collected test per row, dimension, subcase, seam and
// measurement. Its stress workloads live only in the hardening lane, so the ids are resolved
// against that runner's own collection; a row that names a test no runner collects is a coverage
// gap the report would otherwise only discover after a whole workload had run.
const hardening = JSON.parse(
  readFileSync(
    join(
      frontend,
      "..",
      "governance",
      "contracts",
      "nle_hardening_coverage_manifest_v1.json",
    ),
    "utf8",
  ),
) as {
  command_rows: {
    command: string;
    evidence: Record<string, string>;
    subcases?: { subcase_id: string; evidence_id: string }[];
  }[];
  ui_rows: { invariant_id: string; evidence: Record<string, string> }[];
  action_rows: { row_id: string; evidence: Record<string, string> }[];
  recovery_rows: { seam_id: string; evidence_id: string }[];
  measurements: { measurement_id: string; evidence_id: string }[];
};

it("resolves every hardening evidence ID against the hardening runner's collection", async () => {
  const dimensions = (
    owner: string,
    evidence: Record<string, string>,
  ): { owner: string; reference: string }[] =>
    Object.entries(evidence).map(([facet, reference]) => ({
      owner: `${owner}.${facet}`,
      reference,
    }));
  const references = [
    ...hardening.command_rows.flatMap((row) => [
      ...dimensions(row.command, row.evidence),
      ...(row.subcases ?? []).map((subcase) => ({
        owner: `${row.command}.${subcase.subcase_id}`,
        reference: subcase.evidence_id,
      })),
    ]),
    ...hardening.ui_rows.flatMap((row) =>
      dimensions(row.invariant_id, row.evidence),
    ),
    ...hardening.action_rows.flatMap((row) =>
      dimensions(row.row_id, row.evidence),
    ),
    ...hardening.recovery_rows.map((row) => ({
      owner: row.seam_id,
      reference: row.evidence_id,
    })),
    ...hardening.measurements.map((row) => ({
      owner: row.measurement_id,
      reference: row.evidence_id,
    })),
  ];
  expect(references.length).toBeGreaterThan(0);
  const files = [
    ...new Set(references.map(({ reference }) => reference.split("::")[0]!)),
  ];
  const collected = (await collect("@playwright/test/cli.js", [
    "test",
    "--config=playwright.hardening.config.ts",
    "--list",
    "--reporter=json",
    ...files.map((file) => file.replace(/^e2e\//, "")),
  ])) as { suites: CollectedSuite[]; errors?: unknown[] };
  expect(collected.errors ?? []).toEqual([]);
  const ids = new Set<string>();
  function visit(suite: CollectedSuite, parents: string[]): void {
    for (const spec of suite.specs ?? []) {
      if (spec.tests.some((test) => test.expectedStatus === "passed")) {
        ids.add(
          `e2e/${spec.file.replaceAll("\\", "/")}::${[...parents, spec.title].join(" > ")}`,
        );
      }
    }
    for (const child of suite.suites ?? [])
      visit(child, [...parents, child.title]);
  }
  for (const suite of collected.suites) visit(suite, []);
  expect(ids.size).toBeGreaterThan(0);
  const unresolved = references.filter(({ reference }) => !ids.has(reference));
  expect(unresolved).toEqual([]);
}, 120_000);
