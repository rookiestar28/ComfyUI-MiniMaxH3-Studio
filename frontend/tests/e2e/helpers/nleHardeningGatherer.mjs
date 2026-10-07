#!/usr/bin/env node
// M25-21: run the hardening lane (`playwright.hardening.config.ts`) and gather every
// `nle_hardening.*` attachment, together with the collected ID and final status of the test that
// attached it, into one `h3.context.nle_hardening_observations.v1` document for
// `scripts/nle_hardening_report.py`. The join -- not this script -- decides what passed: an
// observation counts only when the manifest names exactly its recording test and that test passed.
//
// Usage (from `frontend/`):
//   node tests/e2e/helpers/nleHardeningGatherer.mjs <output-file> [--port <n>] [--grep <re>]
//                                                   [spec ...]
// With no spec arguments every hardening spec runs. The output path is always the caller's.

import { execFileSync, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { release, tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
// GUARD (M25-20 B-69): the declared dependency, never the transitive `playwright-core`.
import { chromium } from "@playwright/test";

const SCHEMA = "h3.context.nle_hardening_observations.v1";
const PREFIX = "nle_hardening.";

function frontendRoot() {
  return join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
}

function sha256(buffer) {
  return `sha256:${createHash("sha256").update(buffer).digest("hex")}`;
}

function git(root, args) {
  return execFileSync("git", args, {
    cwd: root,
    maxBuffer: 256 * 1024 * 1024,
  });
}

/**
 * The uncommitted candidate's identity: HEAD plus a digest of the working-tree difference and of
 * every untracked, non-ignored file. Two runs with the same fingerprint ran the same bytes.
 */
function candidateIdentity(repository) {
  const head = git(repository, ["rev-parse", "HEAD"]).toString().trim();
  const hash = createHash("sha256");
  hash.update(git(repository, ["diff", "HEAD", "--binary"]));
  const untracked = git(repository, [
    "ls-files",
    "--others",
    "--exclude-standard",
    "-z",
  ])
    .toString()
    .split("\0")
    .filter(Boolean)
    .sort();
  for (const path of untracked) {
    hash.update(`\0${path}\0`);
    hash.update(readFileSync(join(repository, path)));
  }
  return { head, tree: `sha256:${hash.digest("hex")}` };
}

/** The statuses a lane may end on without the gather being a failure. */
const PASSING_STATUSES = new Set(["passed", "skipped", "expected"]);

/**
 * Whether this gather succeeded, given Playwright's exit status and the tests it reported.
 *
 * GUARD (B-M2545-45): a parseable JSON report is NOT a lane that passed, and the two must never
 * share an exit code. This wrapper used to print `playwright exit ${run.status}` into its summary
 * line and then return successfully regardless, so `g10-hardening-acceptance` ended
 * `statuses {"passed":100,"failed":1}; playwright exit 1` on one line and `EXIT=0` on the next.
 * The shell runner propagates that zero, so a lane with a failed row and NO render observations
 * reads as a completed gather everywhere except inside the log body. That is precisely the
 * "a measurement that is absent reported as a zero" failure the acceptance review is told to
 * refuse, produced by the harness rather than by anyone's decision.
 *
 * Kept pure and exported so the case that matters -- a valid report containing a failed test --
 * can be driven directly, instead of only being exercised by lanes that happened to pass.
 */
export function gatherOutcome({ playwrightStatus, tests }) {
  const reasons = [];
  if (playwrightStatus !== 0)
    reasons.push(`playwright exited ${playwrightStatus}`);
  const unsuccessful = tests.filter(
    (test) => !PASSING_STATUSES.has(test.status),
  );
  for (const test of unsuccessful)
    reasons.push(`${test.status}: ${test.test_id}`);
  return { ok: reasons.length === 0, reasons };
}

export function collect(reportDoc) {
  const tests = [];
  const observations = [];
  const failures = [];
  function visit(suite, parents) {
    for (const spec of suite.specs ?? []) {
      const testId = `e2e/${spec.file.replaceAll("\\", "/")}::${[...parents, spec.title].join(" > ")}`;
      const results = spec.tests.flatMap((test) => test.results ?? []);
      const last = results.at(-1);
      tests.push({
        test_id: testId,
        status: last?.status ?? "not_run",
        duration_ms: last?.duration ?? 0,
      });
      // GUARD (B-M2545-45): retain the failing test's own words. Without this the only account of
      // a failure is an `error-context.md` in a temp output directory nothing preserves on
      // purpose, so a diagnosis starts from a number with no message attached -- which is how the
      // 2.48 GiB ceiling breach in `g10` became un-attributable. Bounded and path-redacted,
      // because this document is evidence.
      if (last && !PASSING_STATUSES.has(last.status))
        failures.push({
          test_id: testId,
          status: last.status,
          errors: (last.errors ?? [last.error])
            .filter(Boolean)
            .map((error) =>
              redact(String(error.message ?? error)).slice(0, 4_000),
            ),
        });
      for (const result of results)
        for (const attachment of result.attachments ?? []) {
          if (!attachment.name.startsWith(PREFIX)) continue;
          observations.push({
            name: attachment.name,
            test_id: testId,
            body: JSON.parse(
              Buffer.from(attachment.body, "base64").toString("utf8"),
            ),
          });
        }
    }
    for (const child of suite.suites ?? [])
      visit(child, [...parents, child.title]);
  }
  for (const suite of reportDoc.suites ?? []) visit(suite, []);
  return { tests, observations, failures };
}

/** Repository and home paths out of a retained message; this document is evidence. */
function redact(text) {
  const repository = join(frontendRoot(), "..");
  return text
    .split(repository)
    .join("<repo>")
    .split(repository.replaceAll("\\", "/"))
    .join("<repo>");
}

async function main() {
  const args = process.argv.slice(2);
  const output = args.shift();
  if (!output || output.startsWith("--")) {
    console.error(
      "usage: node nleHardeningGatherer.mjs <output-file> [--port <n>] [--grep <re>] [spec ...]",
    );
    process.exitCode = 2;
    return;
  }
  const extraEnv = {};
  const selection = [];
  const specs = [];
  for (let index = 0; index < args.length; index += 1) {
    if (args[index] === "--port") extraEnv.H3_CONTEXT_E2E_PORT = args[++index];
    else if (args[index] === "--grep") selection.push("--grep", args[++index]);
    else specs.push(args[index]);
  }
  const root = frontendRoot();
  const repository = join(root, "..");
  const scratch = mkdtempSync(join(tmpdir(), "nle-hardening-"));
  try {
    const reportPath = join(scratch, "playwright-report.json");
    // Identity is frozen before the run observes anything (plan section 11).
    const candidate = candidateIdentity(repository);
    const manifestSha256 = sha256(
      readFileSync(
        join(
          repository,
          "governance",
          "contracts",
          "nle_hardening_coverage_manifest_v1.json",
        ),
      ),
    );
    // GUARD: run the pinned CLI with this Node and no shell. A `.cmd` shim needs `shell: true` on
    // Windows, which concatenates arguments unescaped: a `--grep` alternation's `|` becomes a pipe
    // and caller text becomes shell syntax.
    const run = spawnSync(
      process.execPath,
      [
        join(root, "node_modules", "@playwright", "test", "cli.js"),
        "test",
        "--config",
        "playwright.hardening.config.ts",
        "--reporter=json",
        ...selection,
        ...specs,
      ],
      {
        cwd: root,
        env: {
          ...process.env,
          ...extraEnv,
          PLAYWRIGHT_JSON_OUTPUT_NAME: reportPath,
        },
        encoding: "utf8",
        maxBuffer: 64 * 1024 * 1024,
      },
    );
    if (run.error) throw run.error;
    let reportDoc;
    try {
      reportDoc = JSON.parse(readFileSync(reportPath, "utf8"));
    } catch (error) {
      console.error(run.stdout?.slice(-4_000));
      console.error(run.stderr?.slice(-4_000));
      throw new Error(`no Playwright JSON report: ${error.message}`);
    }
    const { tests, observations, failures } = collect(reportDoc);
    const browser = await chromium.launch();
    let chromiumVersion;
    try {
      chromiumVersion = browser.version();
    } finally {
      await browser.close();
    }
    const playwrightVersion = JSON.parse(
      readFileSync(
        join(root, "node_modules", "@playwright", "test", "package.json"),
        "utf8",
      ),
    ).version;
    const document = {
      schema: SCHEMA,
      identity: {
        candidate_head: candidate.head,
        candidate_tree_fingerprint: candidate.tree,
        manifest_sha256: manifestSha256,
        chromium_version: chromiumVersion,
        playwright_version: playwrightVersion,
        node_version: process.version,
        os_build: release(),
      },
      tests,
      observations,
      failures,
    };
    writeFileSync(output, `${JSON.stringify(document, null, 2)}\n`, "utf8");
    const counts = tests.reduce((acc, test) => {
      acc[test.status] = (acc[test.status] ?? 0) + 1;
      return acc;
    }, {});
    console.log(
      `wrote ${tests.length} tests and ${observations.length} observations to ${output}; ` +
        `statuses ${JSON.stringify(counts)}; playwright exit ${run.status}`,
    );
    const outcome = gatherOutcome({ playwrightStatus: run.status, tests });
    if (!outcome.ok) {
      for (const reason of outcome.reasons)
        console.error(`gather failed: ${reason}`);
      for (const failure of failures)
        for (const message of failure.errors)
          console.error(`${failure.test_id}\n${message}`);
      // The document is still written, on purpose: a failed gather is evidence, and discarding it
      // would leave the next diagnosis with even less than `g10` left behind.
      process.exitCode = 1;
    }
  } finally {
    rmSync(scratch, { recursive: true, force: true });
  }
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
}
