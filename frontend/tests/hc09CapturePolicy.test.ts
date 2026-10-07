import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  symlinkSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { resolve } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { resolveHc09PlaywrightOutput } from "../playwright.hc09.config";
import {
  auditBrowserRequest,
  derivePrivacyCounters,
  queueCounts,
  requiredEvidencePath,
  requiredLoopbackHost,
  requiredSameOriginGetTarget,
  validateContentFreeEvidence,
  writeContentFreeEvidence,
} from "./support/hc09CapturePolicy";

const repositoryRoot = resolve(import.meta.dirname, "../..");
const planningRoot = resolve(repositoryRoot, ".planning");
let temporary = "";

beforeEach(() => {
  temporary = mkdtempSync(resolve(planningRoot, "hc09-policy-test-"));
});

afterEach(() => {
  if (temporary !== "" && existsSync(temporary))
    rmSync(temporary, { recursive: true, force: false });
});

describe("HC-09 live capture policy", () => {
  it("keeps default Playwright output inside the repository ignored result root", () => {
    expect(resolveHc09PlaywrightOutput({})).toBe("test-results/hc09");
    expect(
      resolveHc09PlaywrightOutput({
        H3_CONTEXT_PLAYWRIGHT_OUTPUT: "B:/bounded/test-output",
      }),
    ).toBe("B:/bounded/test-output");
  });

  it("admits only an uncredentialed loopback HTTP root", () => {
    expect(requiredLoopbackHost("http://127.0.0.1:8188/").origin).toBe(
      "http://127.0.0.1:8188",
    );
    const credentialed = ["http://user", ":redacted@", "127.0.0.1:8188/"].join(
      "",
    );
    for (const value of [
      undefined,
      "https://127.0.0.1:8188/",
      "http://example.invalid/",
      credentialed,
      "http://127.0.0.1:8188/private",
      "http://127.0.0.1:8188/?probe=redacted",
    ])
      expect(() => requiredLoopbackHost(value)).toThrow();
  });

  it("normalizes only bounded queue counts", () => {
    expect(queueCounts({ queue_running: [], queue_pending: [1, 2] })).toEqual({
      running: 0,
      pending: 2,
    });
    expect(() =>
      queueCounts({ queue_running: "private", queue_pending: [] }),
    ).toThrow();
  });

  it("blocks foreign origins, non-GET methods, and credential-bearing requests", () => {
    const base = requiredLoopbackHost("http://127.0.0.1:8188/");
    expect(
      auditBrowserRequest(
        base,
        "http://127.0.0.1:8188/scripts/app.js",
        "GET",
        [],
      ),
    ).toEqual({
      allow: true,
      crossOrigin: 0,
      nonGet: 0,
      credentialBearing: 0,
      promptOperation: 0,
      workflowOperation: 0,
      mediaOperation: 0,
    });
    expect(
      auditBrowserRequest(base, "https://remote.invalid/asset.js", "GET", []),
    ).toEqual(expect.objectContaining({ allow: false, crossOrigin: 1 }));
    expect(
      auditBrowserRequest(base, "http://127.0.0.1:8188/prompt", "POST", []),
    ).toEqual(
      expect.objectContaining({
        allow: false,
        nonGet: 1,
        promptOperation: 1,
      }),
    );
    expect(
      auditBrowserRequest(base, "http://127.0.0.1:8188/queue", "GET", [
        "cookie",
      ]),
    ).toEqual(expect.objectContaining({ allow: false, credentialBearing: 1 }));
  });

  it("resolves API reads only after the complete same-origin GET policy passes", () => {
    const base = requiredLoopbackHost("http://127.0.0.1:8188/");
    expect(
      requiredSameOriginGetTarget(base, "/object_info", ["accept"]).href,
    ).toBe("http://127.0.0.1:8188/object_info");
    for (const [target, headers] of [
      ["https://remote.invalid/object_info", ["accept"]],
      ["/prompt", ["accept"]],
      ["/object_info", ["accept", "cookie"]],
    ] as const)
      expect(() => requiredSameOriginGetTarget(base, target, headers)).toThrow(
        "request policy",
      );
  });

  it("derives privacy counters from retained evidence, request audits, and cookies", () => {
    const base = requiredLoopbackHost("http://127.0.0.1:8188/");
    const safeRequest = auditBrowserRequest(
      base,
      "http://127.0.0.1:8188/scripts/app.js",
      "GET",
      [],
    );
    expect(
      derivePrivacyCounters(
        {
          schema: "h3.context.host_seam_live_evidence.v1",
          profile: "comfyui_host_seams_v1",
          result: "PASS",
          count_bucket: "tens",
        },
        [safeRequest],
        { before: 0, after: 0 },
      ),
    ).toEqual({
      prompts: 0,
      workflows: 0,
      media: 0,
      credentials: 0,
      cookies: 0,
      paths_or_urls: 0,
      arbitrary_host_values: 0,
    });

    const unsafe = derivePrivacyCounters(
      { retained_url: "https://private.invalid/value" },
      [
        auditBrowserRequest(base, "http://127.0.0.1:8188/prompt", "POST", [
          "authorization",
        ]),
      ],
      { before: 1, after: 0 },
    );
    expect(unsafe.paths_or_urls).toBe(1);
    expect(unsafe.prompts).toBe(0);
    expect(unsafe.credentials).toBe(1);
    expect(unsafe.cookies).toBe(1);
  });

  it("rejects escaping and linked evidence targets", () => {
    const direct = resolve(temporary, "evidence.json");
    expect(requiredEvidencePath(repositoryRoot, direct)).toBe(direct);
    expect(() =>
      requiredEvidencePath(
        repositoryRoot,
        resolve(repositoryRoot, ".tmp", "evidence.json"),
      ),
    ).toThrow("must stay inside");

    const workspaceTemporary = resolve(repositoryRoot, ".tmp");
    mkdirSync(workspaceTemporary, { recursive: true });
    const outside = mkdtempSync(
      resolve(workspaceTemporary, "hc09-policy-link-target-"),
    );
    const link = resolve(temporary, "linked");
    try {
      symlinkSync(outside, link, "junction");
      expect(() =>
        requiredEvidencePath(repositoryRoot, resolve(link, "evidence.json")),
      ).toThrow();
    } finally {
      // IMPORTANT: unlink the junction itself. Recursive removal can follow a
      // Windows reparse point and delete the separately owned target.
      if (existsSync(link)) unlinkSync(link);
      if (existsSync(outside))
        rmSync(outside, { recursive: true, force: false });
    }
  });

  it("writes exclusively and cleans up a rejected content candidate", () => {
    const target = resolve(temporary, "evidence.json");
    expect(() =>
      writeContentFreeEvidence(target, {
        schema: "h3.context.host_seam_live_evidence.v1",
        leaked_url: "https://private.invalid/value",
      }),
    ).toThrow("arbitrary path, URL or value");
    expect(existsSync(target)).toBe(false);

    const safe = {
      schema: "h3.context.host_seam_live_evidence.v1",
      result: "PASS",
      count: 22,
    };
    validateContentFreeEvidence(safe);
    writeContentFreeEvidence(target, safe);
    const original = readFileSync(target, "utf-8");
    expect(() => writeContentFreeEvidence(target, safe)).toThrow();
    expect(readFileSync(target, "utf-8")).toBe(original);
  });

  it("rejects prototype-control members before and after an exclusive write", () => {
    for (const member of ["__proto__", "prototype", "constructor"])
      expect(() =>
        validateContentFreeEvidence(JSON.parse(`{"${member}":true}`)),
      ).toThrow("unsafe member name");

    const target = resolve(temporary, "readback-evidence.json");
    expect(() =>
      writeContentFreeEvidence(
        target,
        { schema: "h3.context.host_seam_live_evidence.v1", result: "PASS" },
        () => '{"constructor":true}\n',
      ),
    ).toThrow("unsafe member name");
    expect(existsSync(target)).toBe(false);
  });

  it("refuses an existing target before any live capture could overwrite it", () => {
    const target = resolve(temporary, "existing.json");
    writeFileSync(target, "preserve\n", "utf-8");
    expect(() => requiredEvidencePath(repositoryRoot, target)).toThrow(
      "already exists",
    );
    expect(readFileSync(target, "utf-8")).toBe("preserve\n");
  });
});
