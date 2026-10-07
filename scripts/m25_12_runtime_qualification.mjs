import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  existsSync,
  mkdirSync,
  readFileSync,
  realpathSync,
  writeFileSync,
} from "node:fs";
import { createRequire } from "node:module";
import { dirname, isAbsolute, relative, resolve, sep } from "node:path";
import { release } from "node:os";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const requireFrontend = createRequire(resolve(root, "frontend/package.json"));
const args = process.argv.slice(2);
const options = new Map();
for (let index = 0; index < args.length; index += 2) {
  if (
    !["--output", "--port", "--repeat"].includes(args[index]) ||
    !args[index + 1] ||
    options.has(args[index])
  )
    throw new Error("invalid qualification arguments");
  options.set(args[index], args[index + 1]);
}
const output = resolve(
  root,
  options.get("--output") ?? ".tmp/m25-12/runtime-qualification.json",
);
const port = options.get("--port") ?? "4197";
const repeat = Number(options.get("--repeat") ?? "5");
if (
  !/^[1-9]\d{0,4}$/.test(port) ||
  Number(port) > 65535 ||
  !Number.isInteger(repeat) ||
  repeat < 5 ||
  repeat > 10
)
  throw new Error("qualification needs 5..10 repeats and a valid port");
function inside(path) {
  const rel = relative(realpathSync(root), path);
  if (isAbsolute(rel) || rel === ".." || rel.startsWith(`..${sep}`))
    throw new Error("output must remain in the qualification worktree");
}
inside(output);
let ancestor = dirname(output);
while (!existsSync(ancestor)) ancestor = dirname(ancestor);
inside(realpathSync(ancestor));
if (existsSync(output)) inside(realpathSync(output));
const sha = (bytes) => createHash("sha256").update(bytes).digest("hex");
const hashFile = (path) => sha(readFileSync(path));
const publicContract = JSON.parse(
  readFileSync(
    resolve(root, "tests/fixtures/m25_10_composition_contract_v1.json"),
    "utf8",
  ),
);
const corpusNames = [
  "cfr-primary.mp4",
  "invalid.bin",
  "cfr-secondary.mp4",
  "mse-fragmented.mp4",
  "truncated.mp4",
  "vfr-source.mp4",
];
const corpusFingerprints = corpusNames.map(
  (name) =>
    `sha256:${hashFile(resolve(root, "tests/fixtures/m25_12_runtime", name))}`,
);
if (
  JSON.stringify(corpusFingerprints) !==
  JSON.stringify(
    publicContract.snapshot.capability.observation_corpus_fingerprints,
  )
)
  throw new Error("encoded corpus identity mismatch");
const { chromium } = requireFrontend("@playwright/test");
const playwrightVersion = requireFrontend(
  "@playwright/test/package.json",
).version;
const chromiumExecutableSha256 = hashFile(chromium.executablePath());
const browserInstance = await chromium.launch({ headless: true });
const chromiumVersion = browserInstance.version();
await browserInstance.close();
const windowsBuild = release().split(".")[2];
if (
  process.platform !== "win32" ||
  playwrightVersion !== "1.62.1" ||
  chromiumVersion !== "151.0.7922.34" ||
  chromiumExecutableSha256 !==
    "409805a16d6416087e6b2f778df1cf8f7bbb267d6b99f6b5bb0a618eace234f2" || // pragma: allowlist secret
  windowsBuild !== "26200"
)
  throw new Error("browser profile identity mismatch");
const subjectPaths = [
  "frontend/src/runtime/editorRuntime.ts",
  "frontend/src/runtime/mediaCapabilities.ts",
  "frontend/src/runtime/publicAssetManifest.ts",
  "frontend/e2e/browserNleRuntime.ts",
  "frontend/tests/fixtures/browserNleRuntimeFixture.ts",
  "frontend/tests/e2e/journeys/browserNleRuntime.spec.ts",
  "frontend/playwright.config.ts",
  "frontend/pnpm-lock.yaml",
  "tests/fixtures/m25_12_runtime/timing.json",
  "scripts/m25_12_runtime_corpus.mjs",
  "scripts/m25_12_runtime_qualification.mjs",
];
const subjects = () =>
  Object.fromEntries(
    subjectPaths.map((path) => [path, hashFile(resolve(root, path))]),
  );
function candidateIdentity() {
  const git = (args) => {
    const result = spawnSync("git", args, {
      cwd: root,
      encoding: "utf8",
      windowsHide: true,
      maxBuffer: 32 * 1024 * 1024,
    });
    if (result.status !== 0) throw new Error("candidate identity read failed");
    return result.stdout;
  };
  if (git(["ls-files", "--others", "--exclude-standard"]).trim() !== "")
    throw new Error("stage the complete candidate before qualification");
  return {
    head: git(["rev-parse", "HEAD"]).trim(),
    patchSha256: sha(git(["diff", "--binary", "HEAD", "--"])),
  };
}
const before = subjects();
const candidateBefore = candidateIdentity();
const run = spawnSync(
  process.execPath,
  [
    requireFrontend.resolve("@playwright/test/cli"),
    "test",
    "journeys/browserNleRuntime.spec.ts",
    "--repeat-each",
    String(repeat),
    "--reporter=json",
  ],
  {
    cwd: resolve(root, "frontend"),
    windowsHide: true,
    encoding: "utf8",
    timeout: 15 * 60 * 1000,
    maxBuffer: 32 * 1024 * 1024,
    env: {
      ...process.env,
      H3_CONTEXT_E2E_PORT: port,
      PLAYWRIGHT_JSON_OUTPUT_FILE: "",
      PLAYWRIGHT_JSON_OUTPUT_NAME: "",
    },
  },
);
if (run.error) throw new Error("runtime qualification process failed");
let report;
try {
  report = JSON.parse(run.stdout);
} catch {
  throw new Error("runtime qualification did not return a closed report");
}
const rows = [];
function visit(suite) {
  for (const spec of suite.specs ?? [])
    for (const test of spec.tests ?? [])
      for (const result of test.results ?? []) {
        const attachment = result.attachments?.find(
          (value) => value.name === "runtime-measurement",
        );
        const measured = attachment?.body
          ? JSON.parse(Buffer.from(attachment.body, "base64").toString("utf8"))
          : null;
        rows.push({ scenario: spec.title, status: result.status, measured });
      }
  for (const child of suite.suites ?? []) visit(child);
}
for (const suite of report.suites ?? []) visit(suite);
const stableSubject =
  JSON.stringify(before) === JSON.stringify(subjects()) &&
  JSON.stringify(candidateBefore) === JSON.stringify(candidateIdentity());
const passed =
  run.status === 0 &&
  stableSubject &&
  rows.length === repeat * 9 &&
  report.stats.unexpected === 0 &&
  report.stats.skipped === 0 &&
  report.stats.flaky === 0 &&
  rows.every(
    (row) =>
      row.status === "passed" &&
      row.measured &&
      row.measured.close.blocker === null &&
      row.measured.metrics.liveVideos === 0 &&
      Object.values(row.measured.snapshot.resources).every(
        (value) => value === 0,
      ),
  );
const max = (select) =>
  Math.max(
    0,
    ...rows.filter((row) => row.measured).map((row) => select(row.measured)),
  );
const seekTimes = rows
  .flatMap(
    (row) =>
      row.measured?.metrics.seekObservations?.map((value) => value.elapsedMs) ??
      [],
  )
  .sort((a, b) => a - b);
const limits = {
  maximumActiveVideoOwners: max((row) => row.metrics.maximumVideos),
  maximumPendingOperations: max((row) => row.metrics.maximumPendingOperations),
  maximumPendingRvfc: max((row) => row.metrics.maximumPendingRvfc),
  maximumTeardownMs: max((row) => row.metrics.maximumCloseMs),
  maximumCancelMs: max((row) => row.metrics.maximumCancelMs),
  maximumJsHeapDeltaBytes: max((row) => row.jsHeapDeltaBytes),
  seekP95Ms:
    seekTimes[Math.max(0, Math.ceil(seekTimes.length * 0.95) - 1)] ?? null,
};
const withinLimits =
  limits.maximumActiveVideoOwners <= 2 &&
  limits.maximumPendingOperations <= 2 &&
  limits.maximumPendingRvfc <= 2 &&
  limits.maximumTeardownMs <= 500 &&
  limits.maximumCancelMs <= 250 &&
  limits.seekP95Ms !== null &&
  limits.seekP95Ms <= 750 &&
  limits.maximumJsHeapDeltaBytes <= 64 * 1024 * 1024;
let qualification = null;
if (passed && withinLimits) {
  const { createServer } = await import(
    pathToFileURL(requireFrontend.resolve("vite")).href
  );
  const loader = await createServer({
    root: resolve(root, "frontend"),
    configFile: false,
    server: { middlewareMode: true },
    appType: "custom",
  });
  try {
    const runtime = await loader.ssrLoadModule(
      "/src/runtime/mediaCapabilities.ts",
    );
    const payload = {
      schema: runtime.RUNTIME_QUALIFICATION_SCHEMA,
      profileFingerprint: runtime.RUNTIME_PROFILE_FINGERPRINT,
      result: "pass",
      browser: {
        playwrightVersion,
        chromiumVersion,
        chromiumExecutableSha256,
        windowsBuild,
      },
      corpusFingerprints,
      maximumActiveVideoOwners: limits.maximumActiveVideoOwners,
      // The headless controller owns neither warm media nor compositor canvases.
      maximumWarmVideoOwners: 0,
      maximumCanvasOwners: 0,
      maximumPendingOperations: limits.maximumPendingOperations,
      maximumPendingRvfc: limits.maximumPendingRvfc,
      maximumCancelMs: limits.maximumCancelMs,
      maximumTeardownMs: limits.maximumTeardownMs,
      maximumJsHeapDeltaBytes: limits.maximumJsHeapDeltaBytes,
      ownedResourcesAfterTeardown: 0,
    };
    // IMPORTANT: only completed measured PASS rows emit this projection. Its digest is
    // payload integrity, not execution proof; a consumer must independently trust the receipt.
    qualification = {
      ...payload,
      receiptFingerprint: runtime.runtimeQualificationFingerprint(payload),
    };
  } finally {
    await loader.close();
  }
}
const receipt = {
  schema: "h3.context.m25_12.real_runtime_qualification.v1",
  result: passed && withinLimits ? "pass" : "fail",
  recordedAt: new Date().toISOString(),
  repeatCount: repeat,
  stableSubject,
  subjects: before,
  candidate: candidateBefore,
  browser: {
    playwrightVersion,
    chromiumVersion,
    chromiumExecutableSha256,
    windowsBuild,
  },
  corpusFingerprints,
  limits,
  qualification,
  selectedDynamicArtifacts: [],
  scope:
    "actual runtime module in isolated synthetic-media harness; no production consumer, compositor, audio alignment or host qualification",
  host: "not_run",
  rows,
};
mkdirSync(dirname(output), { recursive: true });
writeFileSync(output, `${JSON.stringify(receipt, null, 2)}\n`, "utf8");
console.log(
  JSON.stringify({
    result: receipt.result,
    rows: rows.length,
    stableSubject,
    limits,
  }),
);
if (receipt.result !== "pass") process.exitCode = 1;
