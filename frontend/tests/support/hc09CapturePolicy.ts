import {
  closeSync,
  existsSync,
  lstatSync,
  openSync,
  readFileSync,
  realpathSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { dirname, isAbsolute, relative, resolve } from "node:path";

export type BrowserRequestAudit = Readonly<{
  allow: boolean;
  crossOrigin: number;
  nonGet: number;
  credentialBearing: number;
  promptOperation: number;
  workflowOperation: number;
  mediaOperation: number;
}>;

export type PrivacyCounters = Readonly<{
  prompts: number;
  workflows: number;
  media: number;
  credentials: number;
  cookies: number;
  paths_or_urls: number;
  arbitrary_host_values: number;
}>;

const CLOSED_EVIDENCE_STRINGS = new Set([
  "HC-09",
  "HC-10",
  "PASS",
  "DRIFTED",
  "NOT_RUN",
  "UNAVAILABLE",
  "MATCH",
  "HOST_DRIFT",
  "REPOSITORY_BREAKAGE",
  "HOST_NOT_SUPPLIED",
  "HOST_UNAVAILABLE",
  "PARTIAL_UNAVAILABLE",
  "comfyui_host_seams_v1",
  "script",
  "frontend",
  "backend",
  "present",
  "absent",
  "callable",
  "collection",
  "event_target",
  "mapping",
  "module",
  "object",
  "route_registry",
  "text",
  "closed_members",
  "node_type",
  "none",
  "display_name",
  "node_class",
  "node_definition_wrapper",
  "path_string",
  "route",
  "present_not_ready",
  "ready",
  "unavailable",
  "one",
  "tens",
  "thousands",
  "not_measured",
  "sub_1kb",
  "sub_100kb",
  "sub_100mb",
  "sub_10ms",
  "sub_100ms",
  "sub_5s",
]);
const FORBIDDEN_MEMBER_NAMES = new Set([
  "__proto__",
  "constructor",
  "prototype",
]);

function activity(pathname: string): {
  promptOperation: number;
  workflowOperation: number;
  mediaOperation: number;
} {
  return {
    promptOperation: /(?:^|\/)prompt(?:\/|$)/i.test(pathname) ? 1 : 0,
    workflowOperation: /(?:^|\/)(?:workflow|history)(?:\/|$)/i.test(pathname)
      ? 1
      : 0,
    mediaOperation: /(?:^|\/)(?:upload|view|media)(?:\/|$)/i.test(pathname)
      ? 1
      : 0,
  };
}

export function auditBrowserRequest(
  base: URL,
  requestUrl: string,
  method: string,
  headerNames: readonly string[],
): BrowserRequestAudit {
  let target: URL;
  try {
    target = new URL(requestUrl);
  } catch {
    return {
      allow: false,
      crossOrigin: 1,
      nonGet: method === "GET" ? 0 : 1,
      credentialBearing: 0,
      promptOperation: 0,
      workflowOperation: 0,
      mediaOperation: 0,
    };
  }
  const crossOrigin = target.origin === base.origin ? 0 : 1;
  const nonGet = method === "GET" ? 0 : 1;
  const credentialBearing =
    target.username !== "" ||
    target.password !== "" ||
    headerNames.some((name) =>
      ["authorization", "cookie", "proxy-authorization"].includes(
        name.toLowerCase(),
      ),
    )
      ? 1
      : 0;
  const operations = activity(target.pathname);
  return {
    allow:
      crossOrigin === 0 &&
      nonGet === 0 &&
      credentialBearing === 0 &&
      operations.promptOperation === 0 &&
      operations.workflowOperation === 0 &&
      operations.mediaOperation === 0,
    crossOrigin,
    nonGet,
    credentialBearing,
    ...operations,
  };
}

export function requiredSameOriginGetTarget(
  base: URL,
  requestUrl: string,
  headerNames: readonly string[],
): URL {
  let target: URL;
  try {
    target = new URL(requestUrl, base);
  } catch {
    throw new Error("HC-09 API request policy rejected an invalid target");
  }
  const audit = auditBrowserRequest(base, target.href, "GET", headerNames);
  if (!audit.allow)
    throw new Error("HC-09 API request policy rejected an unsafe target");
  return target;
}

function contribution(value: unknown): number {
  if (value === null || value === false || value === 0 || value === "")
    return 0;
  if (Array.isArray(value)) return value.length;
  if (typeof value === "object") return Object.keys(value).length;
  return 1;
}

function retainedPrivacyCounters(value: unknown): PrivacyCounters {
  const counters = {
    prompts: 0,
    workflows: 0,
    media: 0,
    credentials: 0,
    cookies: 0,
    paths_or_urls: 0,
    arbitrary_host_values: 0,
  };
  const visit = (item: unknown, key = ""): void => {
    const normalizedKey = key.toLowerCase();
    if (["prompt", "prompts", "prompt_text"].includes(normalizedKey))
      counters.prompts += contribution(item);
    if (["workflow", "workflows"].includes(normalizedKey))
      counters.workflows += contribution(item);
    if (["media", "media_bytes"].includes(normalizedKey))
      counters.media += contribution(item);
    if (
      ["credential", "credentials", "token", "authorization"].includes(
        normalizedKey,
      )
    )
      counters.credentials += contribution(item);
    if (["cookie", "cookies"].includes(normalizedKey))
      counters.cookies += contribution(item);
    if (typeof item === "string") {
      if (
        /https?:\/\//i.test(item) ||
        /(?:^|[^a-z])[a-z]:[\\/]/i.test(item) ||
        item.includes("\\") ||
        item.startsWith("/")
      ) {
        counters.paths_or_urls += 1;
        return;
      }
      if (
        !CLOSED_EVIDENCE_STRINGS.has(item) &&
        !/^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+$/.test(item) &&
        !/^[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?$/.test(item) &&
        !/^(?:sha256:)?[0-9a-f]{40,64}$/.test(item)
      )
        counters.arbitrary_host_values += 1;
      return;
    }
    if (Array.isArray(item)) {
      for (const child of item) visit(child);
      return;
    }
    if (item !== null && typeof item === "object")
      for (const [childKey, child] of Object.entries(item))
        visit(child, childKey);
  };
  visit(value);
  return counters;
}

export function derivePrivacyCounters(
  retainedEvidence: unknown,
  requestAudits: readonly BrowserRequestAudit[],
  cookieCounts: Readonly<{ before: number; after: number }>,
): PrivacyCounters {
  if (
    !Number.isSafeInteger(cookieCounts.before) ||
    !Number.isSafeInteger(cookieCounts.after) ||
    cookieCounts.before < 0 ||
    cookieCounts.after < 0
  )
    throw new Error("HC-09 cookie counts are unsafe");
  const retained = retainedPrivacyCounters(retainedEvidence);
  return {
    prompts:
      retained.prompts +
      requestAudits.reduce(
        (count, item) => count + (item.allow ? item.promptOperation : 0),
        0,
      ),
    workflows:
      retained.workflows +
      requestAudits.reduce(
        (count, item) => count + (item.allow ? item.workflowOperation : 0),
        0,
      ),
    media:
      retained.media +
      requestAudits.reduce(
        (count, item) => count + (item.allow ? item.mediaOperation : 0),
        0,
      ),
    credentials:
      retained.credentials +
      requestAudits.reduce((count, item) => count + item.credentialBearing, 0),
    cookies: retained.cookies + cookieCounts.before + cookieCounts.after,
    paths_or_urls: retained.paths_or_urls,
    arbitrary_host_values: retained.arbitrary_host_values,
  };
}

export function requiredLoopbackHost(value: string | undefined): URL {
  if (value === undefined) throw new Error("H3_CONTEXT_HOST_URL is required");
  const url = new URL(value);
  if (
    url.protocol !== "http:" ||
    !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname) ||
    url.username.length > 0 ||
    url.password.length > 0 ||
    url.search.length > 0 ||
    url.hash.length > 0 ||
    !["", "/"].includes(url.pathname)
  )
    throw new Error(
      "the supplied host must be an uncredentialed loopback root",
    );
  return url;
}

function within(root: string, target: string): boolean {
  const child = relative(root, target);
  return child !== "" && !child.startsWith("..") && !isAbsolute(child);
}

export function requiredEvidencePath(
  repositoryRoot: string,
  value: string | undefined,
): string {
  if (value === undefined || !isAbsolute(value))
    throw new Error("H3_CONTEXT_HC09_EVIDENCE must be an absolute path");
  const planningRoot = realpathSync(resolve(repositoryRoot, ".planning"));
  const target = resolve(value);
  const parent = realpathSync(dirname(target));
  if (
    !within(planningRoot, target) ||
    (parent !== planningRoot && !within(planningRoot, parent))
  )
    throw new Error(
      "HC-09 evidence must stay inside the ignored .planning directory",
    );
  let cursor = parent;
  while (cursor !== planningRoot) {
    if (lstatSync(cursor).isSymbolicLink())
      throw new Error("HC-09 evidence path contains a link or reparse point");
    const next = dirname(cursor);
    if (next === cursor)
      throw new Error("HC-09 evidence path does not reach the planning root");
    cursor = next;
  }
  if (existsSync(target))
    throw new Error("HC-09 evidence target already exists");
  return target;
}

export function queueCounts(value: unknown): {
  running: number;
  pending: number;
} {
  if (value === null || typeof value !== "object")
    throw new Error("host queue payload is malformed");
  const queue = value as Record<string, unknown>;
  if (
    !Array.isArray(queue.queue_running) ||
    !Array.isArray(queue.queue_pending)
  )
    throw new Error("host queue payload has no bounded public lists");
  return {
    running: queue.queue_running.length,
    pending: queue.queue_pending.length,
  };
}

export function validateContentFreeEvidence(value: unknown): void {
  let members = 0;
  const visit = (item: unknown, depth: number): void => {
    if (depth > 12 || members > 512)
      throw new Error("HC-09 evidence exceeds its closed resource bounds");
    if (item === null || typeof item === "boolean") return;
    if (typeof item === "number") {
      if (!Number.isSafeInteger(item) || item < 0)
        throw new Error("HC-09 evidence contains an unsafe number");
      return;
    }
    if (typeof item === "string") {
      if (
        item.length > 256 ||
        /[\u0000-\u001f\u007f]/.test(item) ||
        /https?:\/\//i.test(item) ||
        /(?:^|[^a-z])[a-z]:[\\/]/i.test(item) ||
        item.includes("\\")
      )
        throw new Error(
          "HC-09 evidence contains an arbitrary path, URL or value",
        );
      return;
    }
    if (Array.isArray(item)) {
      members += item.length;
      for (const child of item) visit(child, depth + 1);
      return;
    }
    if (typeof item !== "object")
      throw new Error("HC-09 evidence contains an unsupported value");
    const record = item as Record<string, unknown>;
    const keys = Object.keys(record);
    members += keys.length;
    for (const key of keys) {
      if (!/^[a-z][a-z0-9_]*$/.test(key) || FORBIDDEN_MEMBER_NAMES.has(key))
        throw new Error("HC-09 evidence contains an unsafe member name");
      visit(record[key], depth + 1);
    }
  };
  visit(value, 0);
  if (
    Object.values(
      derivePrivacyCounters(value, [], { before: 0, after: 0 }),
    ).some((count) => count !== 0)
  )
    throw new Error("HC-09 evidence contains a forbidden retained value");
}

export function writeContentFreeEvidence(
  target: string,
  value: unknown,
  readback: (path: string) => string = (path) => readFileSync(path, "utf-8"),
  validator: (candidate: unknown) => void = validateContentFreeEvidence,
): void {
  validator(value);
  let descriptor: number | undefined;
  let created = false;
  try {
    descriptor = openSync(target, "wx");
    created = true;
    writeFileSync(descriptor, `${JSON.stringify(value, null, 2)}\n`, {
      encoding: "utf-8",
    });
    closeSync(descriptor);
    descriptor = undefined;
    const persisted = JSON.parse(readback(target)) as unknown;
    // CRITICAL: revalidate the persisted bytes, not only the in-memory value.
    // This keeps a replaced or corrupted evidence file from surviving the write.
    validator(persisted);
    if (readback(target) !== `${JSON.stringify(persisted, null, 2)}\n`)
      throw new Error("HC-09 evidence readback is not canonical JSON");
  } catch (error) {
    if (descriptor !== undefined) closeSync(descriptor);
    if (created && existsSync(target)) unlinkSync(target);
    throw error;
  }
}
