#!/usr/bin/env node
// M25-20 F1 corrective, browser half: runs every semantic-conformance Playwright spec and gathers
// every `semantic_conformance.*` evidence attachment (written by `nleSemanticShellEvidence.ts`'s
// `recordShellObservation`) into one JSON document matching the
// `h3.context.nle_semantic_browser_stage.v1` schema that `scripts/nle_semantic_report.py` (the
// M25-20 stage join) reads.
//
// Usage (run from `frontend/`, matches `pnpm run test:e2e:ci`'s own working directory):
//   node tests/e2e/helpers/nleSemanticBrowserStageGatherer.mjs <output-file> [--port <n>]
//                                                               [--skip-render-and-browser]
//
// Exit codes (D45-04): 0 only when every invoked child run exited 0 and every row of the invoked
// scope carries its required observations; 1 when a child run failed or was signalled, when a row
// produced no observation, when a row is missing required observations, or when the report could
// not be read; 2 for a usage error. A written report is never by itself a successful run. The
// child's own report and streams are retained, with absolute paths redacted, in
// `<output-file>.diagnostics/`, and `--skip-render-and-browser` records `scope: "shell_only"` in
// the document so a shortened diagnostic cannot be read as full-corpus completion.
//
// The output path is always supplied by the caller; this script never hardcodes one. Every row of
// the closed browser corpus (11 `ui_invariant.*` + 2 `import_integration.*` + 3
// `deferred_negative.surface_*` shell rows, plus every `render_and_browser` row the corpus
// declares -- 154 since M25-45 added the four high-resolution ones) is always present in the output -- a case whose journey
// never produced an attachment (crashed before recording, or the recording test did not pass) is
// written as `executed: false` with a `missing` reason, never silently dropped, per TEST_SOP 3.6's
// "a stage that cannot reach a row reports BLOCKED or NOT_RUN; the corpus is never shortened".
//
// Running the full `render_and_browser` sweep (`nleSemanticRenderAndBrowser.spec.ts`) takes
// several minutes; that is why it is not part of `test:e2e:ci`'s testMatch, and this gatherer runs
// it through its own dedicated `playwright.render-and-browser.config.ts` rather than the ordinary
// hermetic config, so a routine CI run of the other specs is unaffected.

import { execFileSync, spawnSync } from "node:child_process";
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { homedir, tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
// GUARD (B-69): import the browser from the declared dependency. `playwright-core` is only a
// transitive dependency of `@playwright/test`; a strict pnpm install (a fresh worktree) does not
// hoist it, and the gatherer then fails before it presents a single row.
import { chromium } from "@playwright/test";

const SCHEMA = "h3.context.nle_semantic_browser_stage.v1";

// M25-20's frozen shell-invariant / import-integration / audio-surface-negative rows
// (`comfyui_h3_context/core/semantic_conformance.py`'s `UI_INVARIANT_EVIDENCE` and
// `IMPORT_INTEGRATION_GESTURES`). Kept here as a literal list -- unlike the
// `render_and_browser` case IDs below, which are read from the corpus itself -- so a row whose
// journey never ran is still reported as NOT_RUN. Keep in sync if the corpus's frozen shell /
// import-integration case identifiers change.
const EXPECTED_SHELL_CASE_IDS = Object.freeze([
  "ui_invariant.global_shell_identity",
  "ui_invariant.function_switch",
  "ui_invariant.overlay_open",
  "ui_invariant.duplicate_open",
  "ui_invariant.overlay_close_return_focus.explicit_close",
  "ui_invariant.overlay_close_return_focus.escape",
  "ui_invariant.overlay_close_return_focus.function_switch",
  "ui_invariant.overlay_close_return_focus.top_level_navigation",
  "ui_invariant.overlay_close_return_focus.capability_or_mount_failure",
  "ui_invariant.overlay_unavailable_status",
  "ui_invariant.view_destroy_cleanup",
  "import_integration.generated_source.explicit_import_then_insert.pointer",
  "import_integration.generated_source.explicit_import_then_insert.keyboard",
  // B-66: the three audio-surface declared negatives (`AUDIO_NEGATIVE_SURFACES`), executed by
  // `nleSemanticAudioSurfaceNegatives.spec.ts` and closed by the join on their counted facts.
  "deferred_negative.surface_compact",
  "deferred_negative.surface_expanded",
  "deferred_negative.surface_fallback",
]);

// Every `render_and_browser` case ID, read from `build_recipes()` itself (never a hand-copied
// list of strings) through the same read-only wire generator the presenting spec uses, so a corpus
// that grows is presented in full without this file being edited.
function expectedRenderAndBrowserCaseIds(root) {
  const python = join(
    root,
    "..",
    ".venv",
    process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
  );
  const scratchDir = mkdtempSync(join(tmpdir(), "nle-render-and-browser-ids-"));
  const outputPath = join(scratchDir, "wires.json");
  try {
    execFileSync(
      python,
      [
        join(root, "..", "scripts", "nle_semantic_browser_wires.py"),
        "--output",
        outputPath,
      ],
      // The generator states every prescription (one flat-composite and edge search per sample
      // point, for 150 rows): about a minute on its own, longer beside a render sweep.
      { encoding: "utf8", timeout: 600_000, maxBuffer: 16_777_216 },
    );
    const document = JSON.parse(readFileSync(outputPath, "utf8"));
    return document.rows.map((row) => row.case_id);
  } finally {
    rmSync(scratchDir, { recursive: true, force: true });
  }
}

const SPEC_FILES_ORDINARY = Object.freeze([
  "journeys/nleSemanticConformance.spec.ts",
  "journeys/nleSemanticShellInvariants.spec.ts",
  "journeys/nleSemanticImportIntegration.spec.ts",
  "journeys/nleSemanticAudioSurfaceNegatives.spec.ts",
]);
const SPEC_FILE_RENDER_AND_BROWSER =
  "journeys/nleSemanticRenderAndBrowser.spec.ts";

const ATTACHMENT_PREFIX = "semantic_conformance.";

// The exact accepted qualification this hermetic lane is pinned to
// (`frontend/src/runtime/acceptedRuntimeQualification.ts`'s
// `RUNTIME_QUALIFICATION_AUTHORITY.expectedQualificationFingerprint`). Duplicated here as a
// literal, the same way that module already duplicates `mediaCapabilities.ts`'s browser literal,
// because this gatherer runs as plain Node ESM with no TypeScript loader available in `frontend/`.
const BROWSER_PROFILE_FINGERPRINT =
  "sha256:a3aaed0e5758fac311f607957e820dfdc761e79cb20628d43fd603e5ae8b1321";

function frontendRoot() {
  const here = dirname(fileURLToPath(import.meta.url));
  return join(here, "..", "..", "..");
}

/**
 * Replaces the machine's own absolute paths with stable placeholders.
 *
 * GUARD (M23-26 privacy review, D45-04): the retained diagnostics below are the only thing this
 * lane keeps on failure, and a raw Playwright report carries native absolute source paths --
 * including the maintainer's home directory. Retention exists to say WHICH rows failed and why,
 * never to disclose where the repository lives. Extend the replacements rather than dropping them
 * if a new absolute path appears in the report.
 */
export function redactPaths(text, root = frontendRoot()) {
  const repository = join(root, "..");
  const variants = (value) => [
    value,
    value.replaceAll("\\", "/"),
    value.replaceAll("\\", "\\\\"),
    encodeURI(value.replaceAll("\\", "/")),
  ];
  let redacted = String(text);
  // Longest first: the frontend directory lies inside the repository, and the home directory
  // usually contains the temporary directory on Windows.
  for (const [placeholder, value] of [
    ["<frontend>", root],
    ["<repo>", repository],
    ["<tmp>", tmpdir()],
    ["<home>", homedir()],
  ])
    for (const variant of variants(value))
      if (variant) redacted = redacted.split(variant).join(placeholder);
  return redacted;
}

/**
 * The gatherer's exit decision, as a pure function so every outcome can be pinned without running
 * Playwright.
 *
 * GUARD (D45-04): a written report is not a successful run. Before this existed the normal
 * completion path recorded the child's status, printed a warning and still exited 0, so a stage
 * document produced by failed child tests -- or one missing required observations entirely --
 * was indistinguishable from a clean sweep to anything reading the exit code. `scope` travels
 * with the decision because a deliberately shortened shell-only diagnostic must never be read as
 * full-corpus completion.
 */
export function stageOutcome({ scope, runs, rows }) {
  const reasons = [];
  for (const run of runs) {
    if (run.signal)
      reasons.push(`the ${run.name} run was terminated by ${run.signal}`);
    else if (run.status !== 0)
      reasons.push(`the ${run.name} run exited ${run.status}`);
  }
  const notRun = rows.filter((row) => !row.executed).map((row) => row.case_id);
  const blocked = rows
    .filter((row) => row.executed && row.missing.length > 0)
    .map((row) => row.case_id);
  if (notRun.length)
    reasons.push(
      `${notRun.length} row(s) produced no observation: ${notRun.slice(0, 5).join(", ")}` +
        (notRun.length > 5 ? ", ..." : ""),
    );
  if (blocked.length)
    reasons.push(
      `${blocked.length} row(s) are missing required observations: ` +
        blocked.slice(0, 5).join(", ") +
        (blocked.length > 5 ? ", ..." : ""),
    );
  return {
    scope,
    exitCode: reasons.length === 0 ? 0 : 1,
    reasons,
    counts: {
      rows: rows.length,
      notRun: notRun.length,
      blocked: blocked.length,
    },
  };
}

async function measuredChromiumVersion() {
  // A real, freshly-launched-and-closed browser observation of the exact binary Playwright will
  // use for the run -- not the pinned expectation string -- so this field reflects what actually
  // executed rather than what was merely configured.
  const browser = await chromium.launch();
  try {
    return browser.version();
  } finally {
    await browser.close();
  }
}

function runSpecs(root, config, specFiles, jsonReportPath, extraEnv) {
  const pnpmCommand = process.platform === "win32" ? "pnpm.cmd" : "pnpm";
  return spawnSync(
    pnpmCommand,
    [
      "exec",
      "playwright",
      "test",
      "--config",
      config,
      "--reporter=json",
      ...specFiles,
    ],
    {
      cwd: root,
      env: {
        ...process.env,
        ...extraEnv,
        PLAYWRIGHT_JSON_OUTPUT_NAME: jsonReportPath,
      },
      encoding: "utf8",
      // Windows cannot `spawnSync` a `.cmd` shim directly (EINVAL); routing it through the shell
      // is what `pnpm.cmd` itself needs there. Every argument is a fixed literal or a path this
      // process computed, never caller-supplied text, so shell interpretation carries no
      // injection risk.
      shell: process.platform === "win32",
    },
  );
}

export function collectAttachments(reportDoc) {
  const byCaseId = new Map();
  function walkSuite(suite) {
    for (const spec of suite.specs ?? []) {
      for (const test of spec.tests ?? []) {
        for (const result of test.results ?? []) {
          for (const attachment of result.attachments ?? []) {
            if (!attachment.name.startsWith(ATTACHMENT_PREFIX)) continue;
            const caseId = attachment.name.slice(ATTACHMENT_PREFIX.length);
            const decoded = JSON.parse(
              Buffer.from(attachment.body, "base64").toString("utf8"),
            );
            byCaseId.set(caseId, { decoded, testStatus: result.status });
          }
        }
      }
    }
    for (const child of suite.suites ?? []) walkSuite(child);
  }
  for (const suite of reportDoc.suites ?? []) walkSuite(suite);
  return byCaseId;
}

export function stringifyFacts(facts) {
  const out = {};
  for (const [key, value] of Object.entries(facts ?? {}))
    out[key] = String(value);
  return out;
}

function canvasDimensions(decoded) {
  return {
    canvas_width: Number.isInteger(decoded.canvas_width)
      ? decoded.canvas_width
      : 0,
    canvas_height: Number.isInteger(decoded.canvas_height)
      ? decoded.canvas_height
      : 0,
  };
}

// M25-20 F1 corrective (join-fixed contract): the `render_and_browser` rows' structured landmark
// fields (`source_mapping`/`geometry`/`patches`/`color_patches`/`alphas`/`text`) pass through
// verbatim -- the join reads these directly (`scripts/nle_semantic_report.py`'s `_source_landmarks`
// etc.), so this gatherer must never drop, rename or reshape them. Only carried when the recording
// spec actually set them (the 13 shell/import rows never do), so those rows' documents stay exactly
// as small as before.
const STRUCTURED_LANDMARK_KEYS = [
  "source_mapping",
  "geometry",
  "patches",
  "color_patches",
  "alphas",
  "text",
];

function structuredLandmarks(decoded) {
  const out = {};
  for (const key of STRUCTURED_LANDMARK_KEYS) {
    if (decoded[key] !== undefined) out[key] = decoded[key];
  }
  return out;
}

export function buildRow(caseId, found) {
  if (found === undefined) {
    return {
      case_id: caseId,
      executed: false,
      missing: [
        `${caseId}: no Playwright attachment was produced for this case (its journey did not ` +
          "run far enough to record an observation)",
      ],
      canvas_width: 0,
      canvas_height: 0,
      refusal_code: null,
      facts: {},
    };
  }
  const { decoded, testStatus } = found;
  if (testStatus !== "passed") {
    return {
      case_id: caseId,
      executed: false,
      missing: [
        ...decoded.missing,
        `${caseId}: the Playwright test that recorded this observation did not pass ` +
          `(status=${testStatus})`,
      ],
      ...canvasDimensions(decoded),
      ...structuredLandmarks(decoded),
      refusal_code: null,
      facts: stringifyFacts(decoded.facts),
    };
  }
  return {
    case_id: caseId,
    executed: decoded.executed,
    missing: decoded.missing,
    ...canvasDimensions(decoded),
    ...structuredLandmarks(decoded),
    refusal_code: null,
    facts: stringifyFacts(decoded.facts),
  };
}

async function main() {
  const outputPath = process.argv[2];
  if (!outputPath || outputPath.startsWith("--")) {
    console.error(
      "usage: node nleSemanticBrowserStageGatherer.mjs <output-file> [--port <n>]",
    );
    process.exitCode = 2;
    return;
  }

  const extraEnv = {};
  const portFlagIndex = process.argv.indexOf("--port");
  if (portFlagIndex !== -1) {
    extraEnv.H3_CONTEXT_E2E_PORT = process.argv[portFlagIndex + 1];
  }
  // Skips the several-minutes render-and-browser sweep for a fast iteration cycle on just the 13
  // shell rows.
  // Omitted (the default), the gatherer produces the complete, combined browser stage document.
  const skipRenderAndBrowser = process.argv.includes(
    "--skip-render-and-browser",
  );

  const root = frontendRoot();
  const scratchDir = mkdtempSync(join(tmpdir(), "nle-semantic-browser-stage-"));
  // Owned evidence directory beside the caller's output file, so a failed sweep leaves behind the
  // reports that say which rows failed instead of deleting them with the scratch directory.
  const diagnosticsDir = `${outputPath}.diagnostics`;
  mkdirSync(diagnosticsDir, { recursive: true });

  try {
    const shellReportPath = join(scratchDir, "playwright-report-shell.json");
    const shellRun = runSpecs(
      root,
      "playwright.config.ts",
      SPEC_FILES_ORDINARY,
      shellReportPath,
      extraEnv,
    );
    if (shellRun.error) throw shellRun.error;
    retainDiagnostics(diagnosticsDir, "shell", shellReportPath, shellRun);
    const byCaseId = collectAttachments(readReport(shellReportPath, shellRun));
    const runs = [
      { name: "shell", status: shellRun.status, signal: shellRun.signal },
    ];
    let expectedCaseIds = [...EXPECTED_SHELL_CASE_IDS];

    if (!skipRenderAndBrowser) {
      const renderAndBrowserReportPath = join(
        scratchDir,
        "playwright-report-render-and-browser.json",
      );
      const renderAndBrowserRun = runSpecs(
        root,
        "playwright.render-and-browser.config.ts",
        [SPEC_FILE_RENDER_AND_BROWSER],
        renderAndBrowserReportPath,
        extraEnv,
      );
      if (renderAndBrowserRun.error) throw renderAndBrowserRun.error;
      retainDiagnostics(
        diagnosticsDir,
        "render-and-browser",
        renderAndBrowserReportPath,
        renderAndBrowserRun,
      );
      for (const [caseId, value] of collectAttachments(
        readReport(renderAndBrowserReportPath, renderAndBrowserRun),
      )) {
        byCaseId.set(caseId, value);
      }
      runs.push({
        name: "render-and-browser",
        status: renderAndBrowserRun.status,
        signal: renderAndBrowserRun.signal,
      });
      expectedCaseIds = [
        ...expectedCaseIds,
        ...expectedRenderAndBrowserCaseIds(root),
      ];
    }

    const rows = expectedCaseIds.map((caseId) =>
      buildRow(caseId, byCaseId.get(caseId)),
    );
    const chromiumVersion = await measuredChromiumVersion();

    // The scope is written into the document itself, so a shell-only diagnostic cannot be read
    // later as a complete browser stage (D45-04). The join keys off `schema` and `rows` and
    // ignores fields it does not know, so this is additive.
    const scope = skipRenderAndBrowser ? "shell_only" : "complete";
    const document = {
      schema: SCHEMA,
      scope,
      browser_profile_fingerprint: BROWSER_PROFILE_FINGERPRINT,
      chromium_version: chromiumVersion,
      rows,
    };

    writeFileSync(outputPath, `${JSON.stringify(document, null, 2)}\n`, "utf8");

    const outcome = stageOutcome({ scope, runs, rows });
    console.log(
      `wrote ${outcome.counts.rows} rows to ${outputPath} (scope=${scope}, ` +
        `blocked=${outcome.counts.blocked}, not_run=${outcome.counts.notRun})`,
    );
    if (scope === "shell_only")
      console.log(
        "scope=shell_only: this document covers the shell rows alone and is NOT a complete " +
          "browser stage; the join still needs a complete run.",
      );
    writeFileSync(
      join(diagnosticsDir, "outcome.json"),
      `${JSON.stringify(outcome, null, 2)}\n`,
      "utf8",
    );
    if (outcome.exitCode !== 0) {
      for (const reason of outcome.reasons) console.error(`failure: ${reason}`);
      console.error(
        `diagnostics retained in ${diagnosticsDir}; the stage document itself is complete and ` +
          "keeps every row identity.",
      );
      process.exitCode = outcome.exitCode;
    }
  } finally {
    rmSync(scratchDir, { recursive: true, force: true });
  }
}

/**
 * Keeps the child run's own report and streams, redacted, in the run's owned evidence directory.
 * Never throws: losing a diagnostic must not lose the stage document that was the point of the run.
 */
function retainDiagnostics(diagnosticsDir, name, jsonReportPath, run) {
  const write = (suffix, text) => {
    try {
      writeFileSync(
        join(diagnosticsDir, `${name}.${suffix}`),
        redactPaths(text ?? ""),
        "utf8",
      );
    } catch (error) {
      console.error(`could not retain ${name}.${suffix}: ${error.message}`);
    }
  };
  try {
    write("report.json", readFileSync(jsonReportPath, "utf8"));
  } catch (error) {
    write("report.missing.txt", `no readable report: ${error.message}`);
  }
  write("stdout.txt", run.stdout);
  write("stderr.txt", run.stderr);
  write(
    "status.json",
    `${JSON.stringify({ status: run.status, signal: run.signal }, null, 2)}\n`,
  );
}

function readReport(jsonReportPath, run) {
  try {
    return JSON.parse(readFileSync(jsonReportPath, "utf8"));
  } catch (readError) {
    console.error("--- playwright stdout ---");
    console.error(redactPaths(run.stdout ?? ""));
    console.error("--- playwright stderr ---");
    console.error(redactPaths(run.stderr ?? ""));
    throw new Error(
      `could not read the Playwright JSON report at ${redactPaths(jsonReportPath)}: ` +
        readError.message,
    );
  }
}

// Only run as a CLI entry point; importing this module (e.g. to unit-check `buildRow` /
// `collectAttachments`) must not trigger a real Playwright run.
if (process.argv[1] === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
}
