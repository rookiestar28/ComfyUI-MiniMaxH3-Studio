import { createHash } from "node:crypto";
import { lstatSync, readFileSync, readdirSync, realpathSync } from "node:fs";
import { extname, isAbsolute, join, relative, resolve, sep } from "node:path";

export const CANDIDATE_BUNDLE_PATH_ENV = "H3_CONTEXT_CANDIDATE_BUNDLE_PATH";
export const CANDIDATE_BUNDLE_SHA256_ENV = "H3_CONTEXT_CANDIDATE_BUNDLE_SHA256";
export const CANDIDATE_BACKEND_HOST_ROOT_ENV = "H3_CONTEXT_HOST_ROOT";
// The integrated NLE/audio/output runtime exceeds 1 MiB. Keep admission bounded
// while accepting the complete single-entry bundle, not a reduced test build.
const MAX_CANDIDATE_BUNDLE_BYTES = 2 * 1_048_576;
// Keep this above the shipped package census while retaining a hard traversal
// bound. The package crossed 256 when the governed contract inventories grew.
const MAX_CANDIDATE_BACKEND_FILES = 512;
const MAX_CANDIDATE_BACKEND_DIRECTORIES = 64;
const MAX_CANDIDATE_BACKEND_FILE_BYTES = 1_048_576;
const MAX_CANDIDATE_BACKEND_TOTAL_BYTES = 16 * 1_048_576;
const CANDIDATE_BACKEND_PACKAGE = "comfyui_h3_context";
// IMPORTANT: this must match the repository directory installed under ComfyUI/custom_nodes.
const INSTALLED_CUSTOM_NODE_DIRECTORY = "ComfyUI-MiniMaxH3-Context";
const CANDIDATE_BACKEND_EXTENSIONS = new Set([".json", ".py", ".typed"]);
const CANDIDATE_BACKEND_PACKAGED_FONT_PATHS = new Set([
  "fonts/LICENSE-OFL-1.1.txt",
  "fonts/NotoSans-Bold.ttf",
  "fonts/NotoSans-BoldItalic.ttf",
  "fonts/NotoSans-Italic.ttf",
  "fonts/NotoSans-Regular.ttf",
]);

export type CandidateBundle = Readonly<{
  bytes: Buffer;
  sha256: string;
}>;

export type CandidateBackendRuntimeReceipt = Readonly<{
  status: "exact";
  candidateFileCount: number;
  matchedFileCount: number;
  missingFileCount: 0;
  staleFileCount: 0;
  inventorySha256: string;
}>;

type BackendMismatchClass =
  | "candidate_inventory_unsafe"
  | "installed_runtime_unsafe"
  | "missing_bytes"
  | "stale_bytes";

class CandidateBackendRuntimeError extends Error {
  readonly mismatchClass: BackendMismatchClass;

  constructor(
    mismatchClass: BackendMismatchClass,
    counts: Readonly<{
      expected: number;
      matched: number;
      missing: number;
      stale: number;
    }>,
  ) {
    super(
      `candidate backend runtime mismatch: ${mismatchClass}; expected=${counts.expected}; matched=${counts.matched}; missing=${counts.missing}; stale=${counts.stale}`,
    );
    this.name = "CandidateBackendRuntimeError";
    this.mismatchClass = mismatchClass;
  }
}

type CandidateBackendFile = Readonly<{
  relativePath: string;
  sha256: string;
}>;

function backendMismatch(
  mismatchClass: BackendMismatchClass,
  expected = 0,
  matched = 0,
  missing = 0,
  stale = 0,
): never {
  throw new CandidateBackendRuntimeError(mismatchClass, {
    expected,
    matched,
    missing,
    stale,
  });
}

function backendStatus(pathValue: string, mismatchClass: BackendMismatchClass) {
  try {
    return lstatSync(pathValue);
  } catch {
    backendMismatch(mismatchClass);
  }
}

function assertBackendDirectory(
  pathValue: string,
  mismatchClass: BackendMismatchClass,
): void {
  const status = backendStatus(pathValue, mismatchClass);
  if (!status.isDirectory() || status.isSymbolicLink())
    backendMismatch(mismatchClass);
}

function candidateBackendInventory(
  repositoryRoot: string,
): CandidateBackendFile[] {
  const lexicalRoot = resolve(repositoryRoot);
  assertBackendDirectory(lexicalRoot, "candidate_inventory_unsafe");
  let realRoot: string;
  try {
    realRoot = realpathSync.native(lexicalRoot);
  } catch {
    backendMismatch("candidate_inventory_unsafe");
  }
  const packageRoot = resolve(lexicalRoot, CANDIDATE_BACKEND_PACKAGE);
  assertContained(lexicalRoot, packageRoot);
  assertBackendDirectory(packageRoot, "candidate_inventory_unsafe");
  let realPackageRoot: string;
  try {
    realPackageRoot = realpathSync.native(packageRoot);
  } catch {
    backendMismatch("candidate_inventory_unsafe");
  }
  if (realPackageRoot !== resolve(realRoot, CANDIDATE_BACKEND_PACKAGE))
    backendMismatch("candidate_inventory_unsafe");

  const inventory: CandidateBackendFile[] = [];
  let directoryCount = 0;
  let totalBytes = 0;
  const visit = (directory: string, prefix: string): void => {
    directoryCount += 1;
    if (directoryCount > MAX_CANDIDATE_BACKEND_DIRECTORIES)
      backendMismatch("candidate_inventory_unsafe");
    let entries;
    try {
      entries = readdirSync(directory, { withFileTypes: true }).sort(
        (left, right) => left.name.localeCompare(right.name),
      );
    } catch {
      backendMismatch("candidate_inventory_unsafe");
    }
    for (const entry of entries) {
      const relativePath =
        prefix.length === 0 ? entry.name : `${prefix}/${entry.name}`;
      if (
        entry.isDirectory() &&
        (relativePath === "web" || entry.name === "__pycache__")
      )
        continue;
      if (entry.isSymbolicLink()) backendMismatch("candidate_inventory_unsafe");
      const absolutePath = join(directory, entry.name);
      if (entry.isDirectory()) {
        visit(absolutePath, relativePath);
        continue;
      }
      // CRITICAL: packaged font parity is limited to this fixed path set. Allowing the .ttf or
      // .txt extension generally would admit unrelated opaque assets into the executable host copy.
      const allowedCandidateFile =
        CANDIDATE_BACKEND_EXTENSIONS.has(extname(entry.name)) ||
        CANDIDATE_BACKEND_PACKAGED_FONT_PATHS.has(relativePath);
      if (!entry.isFile() || !allowedCandidateFile)
        backendMismatch("candidate_inventory_unsafe");
      const status = backendStatus(absolutePath, "candidate_inventory_unsafe");
      if (!status.isFile() || status.isSymbolicLink())
        backendMismatch("candidate_inventory_unsafe");
      if (status.size > MAX_CANDIDATE_BACKEND_FILE_BYTES)
        backendMismatch("candidate_inventory_unsafe");
      totalBytes += status.size;
      if (
        totalBytes > MAX_CANDIDATE_BACKEND_TOTAL_BYTES ||
        inventory.length >= MAX_CANDIDATE_BACKEND_FILES
      )
        backendMismatch("candidate_inventory_unsafe");
      let bytes: Buffer;
      try {
        bytes = readFileSync(absolutePath);
      } catch {
        backendMismatch("candidate_inventory_unsafe");
      }
      inventory.push({
        relativePath,
        sha256: createHash("sha256").update(bytes).digest("hex"),
      });
    }
  };
  visit(packageRoot, "");
  if (inventory.length === 0) backendMismatch("candidate_inventory_unsafe");
  return inventory.sort((left, right) =>
    left.relativePath.localeCompare(right.relativePath),
  );
}

function installedBackendFile(
  packageRoot: string,
  relativePath: string,
): { kind: "missing" } | { kind: "file"; bytes: Buffer } {
  let cursor = packageRoot;
  const segments = relativePath.split("/");
  for (const [index, segment] of segments.entries()) {
    cursor = join(cursor, segment);
    let status;
    try {
      status = lstatSync(cursor);
    } catch {
      return { kind: "missing" };
    }
    if (status.isSymbolicLink()) backendMismatch("installed_runtime_unsafe");
    if (index < segments.length - 1) {
      if (!status.isDirectory()) return { kind: "missing" };
      continue;
    }
    if (!status.isFile()) return { kind: "missing" };
    if (status.size > MAX_CANDIDATE_BACKEND_FILE_BYTES)
      backendMismatch("installed_runtime_unsafe");
  }
  try {
    return { kind: "file", bytes: readFileSync(cursor) };
  } catch {
    backendMismatch("installed_runtime_unsafe");
  }
}

function installedPackageRootExists(
  hostRoot: string,
  installedPackageRoot: string,
): boolean {
  const fixedRelativePath = relative(hostRoot, installedPackageRoot);
  let cursor = hostRoot;
  for (const segment of fixedRelativePath.split(/[\\/]+/)) {
    cursor = join(cursor, segment);
    let status;
    try {
      status = lstatSync(cursor);
    } catch {
      return false;
    }
    if (status.isSymbolicLink()) backendMismatch("installed_runtime_unsafe");
    if (!status.isDirectory()) return false;
  }
  return true;
}

export function verifyCandidateBackendRuntimeEnvironment(_options: {
  repositoryRoot: string;
  environment: NodeJS.ProcessEnv;
}): CandidateBackendRuntimeReceipt {
  const hostRootValue = _options.environment[CANDIDATE_BACKEND_HOST_ROOT_ENV];
  if (hostRootValue === undefined)
    throw new Error("candidate backend runtime explicit host root is required");
  if (!isAbsolute(hostRootValue))
    throw new Error(
      "candidate backend runtime explicit host root must be absolute",
    );

  const inventory = candidateBackendInventory(_options.repositoryRoot);
  const hostRoot = resolve(hostRootValue);
  assertBackendDirectory(hostRoot, "installed_runtime_unsafe");
  const installedPackageRoot = resolve(
    hostRoot,
    "custom_nodes",
    INSTALLED_CUSTOM_NODE_DIRECTORY,
    CANDIDATE_BACKEND_PACKAGE,
  );
  assertContained(hostRoot, installedPackageRoot);

  if (!installedPackageRootExists(hostRoot, installedPackageRoot))
    backendMismatch("missing_bytes", inventory.length, 0, inventory.length, 0);

  let matched = 0;
  let missing = 0;
  let stale = 0;
  for (const candidate of inventory) {
    const installed = installedBackendFile(
      installedPackageRoot,
      candidate.relativePath,
    );
    if (installed.kind === "missing") {
      missing += 1;
      continue;
    }
    const installedSha256 = createHash("sha256")
      .update(installed.bytes)
      .digest("hex");
    if (installedSha256 === candidate.sha256) matched += 1;
    else stale += 1;
  }
  if (missing > 0)
    backendMismatch("missing_bytes", inventory.length, matched, missing, stale);
  if (stale > 0)
    backendMismatch("stale_bytes", inventory.length, matched, missing, stale);
  const inventorySha256 = createHash("sha256")
    .update(
      inventory
        .map((entry) => `${entry.relativePath}\0${entry.sha256}\n`)
        .join(""),
      "utf8",
    )
    .digest("hex");
  return {
    status: "exact",
    candidateFileCount: inventory.length,
    matchedFileCount: matched,
    missingFileCount: 0,
    staleFileCount: 0,
    inventorySha256,
  };
}

export function loadCandidateBundleEnvironment(_options: {
  repositoryRoot: string;
  environment: NodeJS.ProcessEnv;
}): CandidateBundle | null {
  const pathValue = _options.environment[CANDIDATE_BUNDLE_PATH_ENV];
  const hashValue = _options.environment[CANDIDATE_BUNDLE_SHA256_ENV];
  if (pathValue === undefined && hashValue === undefined) return null;
  if (pathValue === undefined || hashValue === undefined)
    throw new Error(
      `${CANDIDATE_BUNDLE_PATH_ENV} and ${CANDIDATE_BUNDLE_SHA256_ENV} must be provided together`,
    );
  if (!/^[0-9a-f]{64}$/.test(hashValue))
    throw new Error("candidate bundle digest must be a lowercase SHA-256");

  const lexicalRoot = resolve(_options.repositoryRoot);
  const lexicalCandidate = resolve(lexicalRoot, pathValue);
  assertContained(lexicalRoot, lexicalCandidate);

  const rootStatus = lstatSync(lexicalRoot);
  if (!rootStatus.isDirectory())
    throw new Error("candidate bundle repository root must be a directory");
  if (rootStatus.isSymbolicLink())
    throw new Error(
      "candidate bundle repository root cannot be a symbolic link",
    );

  const relativeCandidate = relative(lexicalRoot, lexicalCandidate);
  let cursor = lexicalRoot;
  for (const segment of relativeCandidate.split(/[\\/]+/)) {
    cursor = join(cursor, segment);
    if (lstatSync(cursor).isSymbolicLink())
      throw new Error("candidate bundle path cannot contain a symbolic link");
  }

  const candidateStatus = lstatSync(lexicalCandidate);
  if (!candidateStatus.isFile())
    throw new Error("candidate bundle must be a regular file");
  if (
    candidateStatus.size === 0 ||
    candidateStatus.size > MAX_CANDIDATE_BUNDLE_BYTES
  )
    throw new Error("candidate bundle size is outside the accepted bound");

  const realRoot = realpathSync.native(lexicalRoot);
  const realCandidate = realpathSync.native(lexicalCandidate);
  assertContained(realRoot, realCandidate);
  const bytes = readFileSync(realCandidate);
  const sha256 = createHash("sha256").update(bytes).digest("hex");
  if (sha256 !== hashValue) throw new Error("candidate bundle hash mismatch");
  return { bytes, sha256 };
}

function assertContained(repositoryRoot: string, candidatePath: string): void {
  const childPath = relative(repositoryRoot, candidatePath);
  if (
    childPath.length === 0 ||
    childPath === ".." ||
    childPath.startsWith(`..${sep}`) ||
    isAbsolute(childPath)
  )
    throw new Error("candidate bundle must stay inside the repository");
}

export class H3NetworkAttribution {
  readonly #allowedOrigin: string;
  #interactionActive = false;
  #startupRemoteCount = 0;
  #interactionRemoteCount = 0;
  #interactionProviderCount = 0;

  constructor(allowedOrigin: string) {
    this.#allowedOrigin = new URL(allowedOrigin).origin;
  }

  observeRequest(url: string): void {
    let parsed: URL;
    try {
      parsed = new URL(url);
    } catch {
      return;
    }
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return;
    const isRemote = parsed.origin !== this.#allowedOrigin;
    if (!this.#interactionActive) {
      if (isRemote) this.#startupRemoteCount += 1;
      return;
    }
    if (isRemote) this.#interactionRemoteCount += 1;
    if (
      /(?:ollama|api\.minimax|platform\.minimax)/i.test(url) ||
      /\/api\/(?:chat|generate|tags|show|ps|pull)(?:[/?#]|$)/i.test(url)
    )
      this.#interactionProviderCount += 1;
  }

  beginH3InteractionPhase(): void {
    this.#interactionRemoteCount = 0;
    this.#interactionProviderCount = 0;
    this.#interactionActive = true;
  }

  pauseForHostInitialization(): void {
    this.#interactionActive = false;
  }

  snapshot(): Readonly<{
    startupRemoteCount: number;
    interactionRemoteCount: number;
    interactionProviderCount: number;
  }> {
    return {
      startupRemoteCount: this.#startupRemoteCount,
      interactionRemoteCount: this.#interactionRemoteCount,
      interactionProviderCount: this.#interactionProviderCount,
    };
  }
}

const MAX_CANDIDATE_INITIATOR_DEPTH = 64;
const MAX_CANDIDATE_INITIATOR_FRAMES = 256;

function candidateAttributionUnavailable(): never {
  throw new Error("candidate network attribution unavailable");
}

function candidateAttributionRecord(
  value: unknown,
): Record<string, unknown> | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

function candidateInitiatorFrameMatch(
  initiatorValue: unknown,
  candidateResourceUrl: string,
): boolean {
  const initiator = candidateAttributionRecord(initiatorValue);
  if (initiator === undefined || typeof initiator.type !== "string")
    candidateAttributionUnavailable();
  if (initiator.stack === undefined) return false;

  let frameCount = 0;
  const visited = new Set<Record<string, unknown>>();
  const inspectStack = (stackValue: unknown, depth: number): boolean => {
    if (depth > MAX_CANDIDATE_INITIATOR_DEPTH)
      candidateAttributionUnavailable();
    const stack = candidateAttributionRecord(stackValue);
    if (stack === undefined || visited.has(stack))
      candidateAttributionUnavailable();
    visited.add(stack);
    if (!Array.isArray(stack.callFrames)) candidateAttributionUnavailable();
    let matched = false;
    for (const frameValue of stack.callFrames) {
      const frame = candidateAttributionRecord(frameValue);
      if (frame === undefined || typeof frame.url !== "string")
        candidateAttributionUnavailable();
      frameCount += 1;
      if (frameCount > MAX_CANDIDATE_INITIATOR_FRAMES)
        candidateAttributionUnavailable();
      if (frame.url === candidateResourceUrl) matched = true;
    }
    if (matched) return true;
    if (stack.parent !== undefined)
      return inspectStack(stack.parent, depth + 1);
    if (stack.parentId !== undefined) candidateAttributionUnavailable();
    return false;
  };
  return inspectStack(initiator.stack, 0);
}

export class CandidateInitiatorNetworkAttribution {
  readonly #allowedOrigin: string;
  readonly #candidateResourceUrl: string;
  #interactionActive = false;
  #candidateInteractionRemoteCount = 0;
  #candidateInteractionProviderCount = 0;
  #failed = false;

  constructor(allowedOrigin: string, candidateResourceUrl: string) {
    try {
      const allowed = new URL(allowedOrigin);
      const candidate = new URL(candidateResourceUrl);
      if (
        (allowed.protocol !== "http:" && allowed.protocol !== "https:") ||
        (candidate.protocol !== "http:" && candidate.protocol !== "https:") ||
        allowed.origin !== candidate.origin ||
        candidate.username.length > 0 ||
        candidate.password.length > 0 ||
        candidate.hash.length > 0
      )
        candidateAttributionUnavailable();
      this.#allowedOrigin = allowed.origin;
      this.#candidateResourceUrl = candidate.href;
    } catch {
      candidateAttributionUnavailable();
    }
  }

  observeRequestWillBeSent(eventValue: unknown): void {
    if (!this.#interactionActive || this.#failed) return;
    try {
      const event = candidateAttributionRecord(eventValue);
      const request = candidateAttributionRecord(event?.request);
      if (event === undefined || request === undefined)
        candidateAttributionUnavailable();
      if (typeof request.url !== "string") candidateAttributionUnavailable();
      const requestUrl = new URL(request.url);
      if (requestUrl.protocol !== "http:" && requestUrl.protocol !== "https:")
        return;
      if (
        !candidateInitiatorFrameMatch(
          event.initiator,
          this.#candidateResourceUrl,
        )
      )
        return;
      if (requestUrl.origin !== this.#allowedOrigin)
        this.#candidateInteractionRemoteCount += 1;
      if (
        /(?:ollama|api\.minimax|platform\.minimax)/i.test(request.url) ||
        /\/api\/(?:chat|generate|tags|show|ps|pull)(?:[/?#]|$)/i.test(
          request.url,
        )
      )
        this.#candidateInteractionProviderCount += 1;
    } catch {
      this.#failed = true;
    }
  }

  beginH3InteractionPhase(): void {
    this.#candidateInteractionRemoteCount = 0;
    this.#candidateInteractionProviderCount = 0;
    this.#interactionActive = true;
  }

  pauseForHostInitialization(): void {
    this.#interactionActive = false;
  }

  snapshot(): Readonly<{
    candidateInteractionRemoteCount: number;
    candidateInteractionProviderCount: number;
  }> {
    if (this.#failed) candidateAttributionUnavailable();
    return {
      candidateInteractionRemoteCount: this.#candidateInteractionRemoteCount,
      candidateInteractionProviderCount:
        this.#candidateInteractionProviderCount,
    };
  }
}

export async function waitForStartupNetworkQuiet(
  attribution: H3NetworkAttribution,
  wait: (milliseconds: number) => Promise<void>,
  options: Readonly<{
    quietWindowMs: number;
    pollIntervalMs: number;
    timeoutMs: number;
  }> = {
    quietWindowMs: 5_000,
    pollIntervalMs: 250,
    timeoutMs: 20_000,
  },
): Promise<void> {
  const { quietWindowMs, pollIntervalMs, timeoutMs } = options;
  if (
    !Number.isInteger(quietWindowMs) ||
    !Number.isInteger(pollIntervalMs) ||
    !Number.isInteger(timeoutMs) ||
    quietWindowMs <= 0 ||
    pollIntervalMs <= 0 ||
    timeoutMs < quietWindowMs
  )
    throw new Error("startup network quiet-window bounds are invalid");

  let previousCount = attribution.snapshot().startupRemoteCount;
  let quietForMs = 0;
  for (let elapsedMs = 0; elapsedMs < timeoutMs;) {
    const delayMs = Math.min(pollIntervalMs, timeoutMs - elapsedMs);
    await wait(delayMs);
    elapsedMs += delayMs;
    const currentCount = attribution.snapshot().startupRemoteCount;
    if (currentCount === previousCount) quietForMs += delayMs;
    else quietForMs = 0;
    previousCount = currentCount;
    if (quietForMs >= quietWindowMs) return;
  }
  throw new Error("host startup remote traffic did not settle within bounds");
}
