import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, test } from "@playwright/test";

import rawCensus from "../../../../../comfyui_h3_context/contracts/host_seam_census_v1.json" with { type: "json" };
import rawFixture from "../../../../../comfyui_h3_context/contracts/host_seam_shape_fixture_v1.json" with { type: "json" };
import { parseHostSeamContract } from "../../../../src/host/hostSeamContract";
import { loadCandidateBundleEnvironment } from "../../helpers/candidateBundleHarness";
import {
  auditBrowserRequest,
  derivePrivacyCounters,
  queueCounts,
  requiredEvidencePath,
  requiredLoopbackHost,
  requiredSameOriginGetTarget,
  writeContentFreeEvidence,
  type BrowserRequestAudit,
} from "../../../support/hc09CapturePolicy";
import {
  compareObservedHostSeamFixture,
  normalizeObservedHostSeamFixture,
  type HostSeamObservationWire,
} from "../../../support/hostSeamObservation";
import {
  buildBackendHostSeamObservations,
  observeFrontendHostSeamsInPage,
} from "../../../support/hostSeamLiveProbe";

const hostUrl = process.env.H3_CONTEXT_HOST_URL;
const evidencePath = process.env.H3_CONTEXT_HC09_EVIDENCE;
const candidateHead = process.env.H3_CONTEXT_CANDIDATE_HEAD;
const candidateTree = process.env.H3_CONTEXT_CANDIDATE_TREE;
const expectedRevision = process.env.H3_CONTEXT_EXPECTED_HOST_REVISION;
const repositoryRoot = resolve(import.meta.dirname, "../../../../..");
const candidateBundle = loadCandidateBundleEnvironment({
  repositoryRoot,
  environment: process.env,
});

function requiredIdentity(value: string | undefined, field: string): string {
  if (value === undefined || !/^[0-9a-f]{40}$/.test(value))
    throw new Error(`${field} must be a full public Git identity`);
  return value;
}

function sha256(value: Uint8Array | string): string {
  return createHash("sha256").update(value).digest("hex");
}

test("captures and verifies the content-free HC-09 host seam shapes", async ({
  context,
  page,
}) => {
  if (candidateBundle === null)
    throw new Error(
      "HC-09 acceptance requires an exact candidate bundle binding",
    );
  const base = requiredLoopbackHost(hostUrl);
  const targetEvidence = requiredEvidencePath(repositoryRoot, evidencePath);
  const head = requiredIdentity(candidateHead, "H3_CONTEXT_CANDIDATE_HEAD");
  const tree = requiredIdentity(candidateTree, "H3_CONTEXT_CANDIDATE_TREE");
  const revision = requiredIdentity(
    expectedRevision,
    "H3_CONTEXT_EXPECTED_HOST_REVISION",
  );
  expect(revision).toBe(rawFixture.subject.comfyui_revision);
  const contract = parseHostSeamContract(rawCensus, rawFixture);

  const cookiesBefore = await context.cookies();
  expect(cookiesBefore).toHaveLength(0);
  const requestAudits: BrowserRequestAudit[] = [];
  await page.route("**/*", async (route) => {
    const request = route.request();
    const headers = await request.allHeaders();
    const audit = auditBrowserRequest(
      base,
      request.url(),
      request.method(),
      Object.keys(headers),
    );
    requestAudits.push(audit);
    if (audit.allow) await route.fallback();
    else await route.abort("blockedbyclient");
  });

  const apiRequestAudits: BrowserRequestAudit[] = [];
  const guardedGet = async (
    path: string,
    accept: "application/json" | "application/javascript",
  ): Promise<Response> => {
    const headerNames = ["accept"];
    const target = requiredSameOriginGetTarget(base, path, headerNames);
    const audit = auditBrowserRequest(base, target.href, "GET", headerNames);
    expect(audit.allow).toBe(true);
    apiRequestAudits.push(audit);
    // Node fetch has no ambient browser/API-context cookie jar. Credentials are
    // explicitly omitted and redirects stay manual so every target is checked first.
    const response = await fetch(target, {
      credentials: "omit",
      headers: { accept },
      method: "GET",
      redirect: "manual",
      signal: AbortSignal.timeout(30_000),
    });
    expect(response.status).toBe(200);
    expect(new URL(response.url).origin).toBe(base.origin);
    return response;
  };
  const getJson = async (path: string): Promise<unknown> => {
    const response = await guardedGet(path, "application/json");
    return response.json();
  };

  const queueBefore = queueCounts(await getJson("/queue"));
  expect(queueBefore).toEqual({ running: 0, pending: 0 });
  const systemStats = (await getJson("/system_stats")) as {
    system?: Record<string, unknown>;
  };
  const objectInfo = (await getJson("/object_info")) as Record<string, unknown>;
  const extensions = await getJson("/extensions");
  expect(Array.isArray(extensions)).toBe(true);
  const extensionBundlePaths = (extensions as unknown[]).filter((value) => {
    if (typeof value !== "string") return false;
    try {
      const target = new URL(value, base);
      return (
        target.origin === base.origin &&
        target.pathname.endsWith("/h3-context-sidebar.js")
      );
    } catch {
      return false;
    }
  }) as string[];
  expect(extensionBundlePaths).toHaveLength(1);
  const servedBundleResponse = await guardedGet(
    extensionBundlePaths[0]!,
    "application/javascript",
  );
  const hostInstalledBundleBytes = Buffer.from(
    await servedBundleResponse.arrayBuffer(),
  );
  const candidateResourceUrl = new URL(extensionBundlePaths[0]!, base).href;
  let candidateBundleInjectionCount = 0;
  let candidateBundleResourceType: string | undefined;
  await context.route(candidateResourceUrl, async (route) => {
    if (
      route.request().url() !== candidateResourceUrl ||
      route.request().method() !== "GET"
    )
      throw new Error("candidate bundle route received a non-exact request");
    candidateBundleInjectionCount += 1;
    candidateBundleResourceType = route.request().resourceType();
    await route.fulfill({
      status: 200,
      body: candidateBundle.bytes,
      contentType: "text/javascript; charset=utf-8",
      headers: {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
      },
    });
  });
  const profileResponse = await guardedGet(
    "/h3-context/v1/generation/profile",
    "application/json",
  );
  const generationProfile = (await profileResponse.json()) as Record<
    string,
    unknown
  >;

  const navigation = await page.goto(base.href, {
    waitUntil: "domcontentloaded",
  });
  expect(navigation?.status()).toBe(200);
  expect(navigation?.request().redirectedFrom()).toBeNull();
  expect(new URL(page.url()).origin).toBe(base.origin);
  await expect
    .poll(
      () =>
        page.evaluate(() => {
          const registry = (
            globalThis as unknown as {
              LiteGraph?: { registered_node_types?: unknown };
            }
          ).LiteGraph?.registered_node_types;
          return registry !== null && typeof registry === "object"
            ? Object.keys(registry).length
            : 0;
        }),
      { timeout: 30_000 },
    )
    .toBeGreaterThan(0);
  // LiteGraph registration is asynchronous relative to DOM readiness. Sample
  // only after the count remains unchanged for a bounded two-second window.
  let stableNodeDefinitionCount = -1;
  let stableSamples = 0;
  for (let sample = 0; sample < 120 && stableSamples < 8; sample += 1) {
    const count = await page.evaluate(() => {
      const registry = (
        globalThis as unknown as {
          LiteGraph?: { registered_node_types?: Record<string, unknown> };
        }
      ).LiteGraph?.registered_node_types;
      return registry === undefined ? 0 : Object.keys(registry).length;
    });
    if (count === stableNodeDefinitionCount) stableSamples += 1;
    else {
      stableNodeDefinitionCount = count;
      stableSamples = 0;
    }
    if (stableSamples < 8) await page.waitForTimeout(250);
  }
  expect(stableSamples).toBe(8);
  expect(candidateBundleInjectionCount).toBe(1);
  expect(candidateBundleResourceType).toBe("script");

  const candidateSidebarRegistryCount = await page.evaluate(() => {
    const app = (
      window as unknown as {
        comfyAPI: {
          app: {
            app: {
              extensionManager: { getSidebarTabs(): Array<{ id: string }> };
            };
          };
        };
      }
    ).comfyAPI.app.app;
    return app.extensionManager
      .getSidebarTabs()
      .filter((tab) => tab.id === "h3-context").length;
  });
  expect(candidateSidebarRegistryCount).toBe(1);

  // CRITICAL: a stable node count can precede activeWorkflow initialization.
  // Wait for every governed seam or a ready host is falsely classified absent.
  await expect
    .poll(
      async () =>
        (await page.evaluate(observeFrontendHostSeamsInPage)).allReady,
      { timeout: 30_000 },
    )
    .toBe(true);

  const live = await page.evaluate(observeFrontendHostSeamsInPage);

  expect(live.allReady).toBe(true);
  expect(live.observations).toHaveLength(23);
  expect(live.nodeDefinitionCount).toBe(stableNodeDefinitionCount);
  const registeredNodeTypes =
    contract.byId["frontend.litegraph.registered_node_types"];
  // The observed floor calibrates the collection ceiling; it is not an exact
  // installed-extension count. A pinned host can expose fewer optional node
  // definitions while retaining the same ready mapping shape.
  expect(live.nodeDefinitionCount).toBeGreaterThanOrEqual(1_000);
  expect(live.nodeDefinitionCount).toBeLessThanOrEqual(
    registeredNodeTypes.bound?.derived_ceiling ?? 0,
  );
  expect(live.inspectionMilliseconds).toBeLessThan(10);
  expect(extensionBundlePaths).toHaveLength(1);
  const system = systemStats.system ?? {};
  expect(system.comfyui_version).toBe(rawFixture.subject.comfyui_version);
  expect(system.required_frontend_version).toBe(
    rawFixture.subject.frontend_version,
  );
  expect(Object.keys(objectInfo).length).toBeGreaterThan(0);
  const shippedNodeIds = Object.keys(objectInfo).filter((id) =>
    id.startsWith("comfyui_h3_context."),
  );
  expect(shippedNodeIds.length).toBeGreaterThan(0);
  expect(
    shippedNodeIds.every((id) => {
      const row = objectInfo[id] as Record<string, unknown>;
      return typeof row.display_name === "string";
    }),
  ).toBe(true);

  const queueAfter = queueCounts(await getJson("/queue"));
  expect(queueAfter).toEqual(queueBefore);
  const cookiesAfter = await context.cookies();
  expect(cookiesAfter).toHaveLength(0);
  const allRequestAudits = [...requestAudits, ...apiRequestAudits];
  const sumAudit = (
    audits: readonly BrowserRequestAudit[],
    field: Exclude<keyof BrowserRequestAudit, "allow">,
  ): number =>
    audits.reduce((count, observation) => count + observation[field], 0);
  const blockedBrowserNonGetAttempts = sumAudit(requestAudits, "nonGet");
  const blockedBrowserCrossOriginAttempts = sumAudit(
    requestAudits,
    "crossOrigin",
  );
  const credentialBearingAttempts = sumAudit(
    allRequestAudits,
    "credentialBearing",
  );
  expect(credentialBearingAttempts).toBe(0);

  const censusPath = resolve(
    repositoryRoot,
    "comfyui_h3_context/contracts/host_seam_census_v1.json",
  );
  const fixturePath = resolve(
    repositoryRoot,
    "comfyui_h3_context/contracts/host_seam_shape_fixture_v1.json",
  );
  const censusBytes = readFileSync(censusPath);
  const fixtureBytes = readFileSync(fixturePath);
  const repositoryBundleBytes = readFileSync(
    resolve(repositoryRoot, "comfyui_h3_context/web/h3-context-sidebar.js"),
  );
  expect(candidateBundle.bytes.length).toBe(repositoryBundleBytes.length);
  expect(candidateBundle.sha256).toBe(sha256(repositoryBundleBytes));
  const backendObservations = buildBackendHostSeamObservations({
    objectInfo,
    generationProfile,
    generationProfileStatus: profileResponse.status,
    extensionBundleManifestEntries: extensionBundlePaths.length,
  });
  const normalizedFixture = normalizeObservedHostSeamFixture(
    rawCensus,
    rawFixture.subject,
    [
      ...backendObservations,
      ...(live.observations as HostSeamObservationWire[]),
    ],
  );
  expect(normalizedFixture.bytes).toBe(fixtureBytes.toString("utf-8"));
  const fixtureComparison = compareObservedHostSeamFixture(
    rawFixture,
    normalizedFixture.wire,
  );
  expect(fixtureComparison).toEqual({
    result: "PASS",
    passed: 29,
    drifted: 0,
  });

  const evidenceWithoutPrivacy = {
    schema: "h3.context.host_seam_live_evidence.v1",
    item: "HC-09",
    candidate: { head, tree },
    subject: {
      comfyui_version: rawFixture.subject.comfyui_version,
      comfyui_revision: revision,
      frontend_version: rawFixture.subject.frontend_version,
    },
    request_policy: {
      loopback_http_only: true,
      redirects: 0,
      isolated_node_fetch: true,
      node_fetch_credential_mode_omit: true,
      api_get_requests: apiRequestAudits.length,
      permitted_browser_non_get_requests: requestAudits.filter(
        (audit) => audit.allow && audit.nonGet > 0,
      ).length,
      blocked_browser_non_get_attempts: blockedBrowserNonGetAttempts,
      blocked_browser_cross_origin_attempts: blockedBrowserCrossOriginAttempts,
      blocked_browser_credential_attempts: credentialBearingAttempts,
      blocked_browser_prompt_attempts: sumAudit(
        requestAudits,
        "promptOperation",
      ),
      blocked_browser_workflow_attempts: sumAudit(
        requestAudits,
        "workflowOperation",
      ),
      blocked_browser_media_attempts: sumAudit(requestAudits, "mediaOperation"),
      browser_cookie_records_before: cookiesBefore.length,
      browser_cookie_records_after: cookiesAfter.length,
      model_or_queue_operations: allRequestAudits
        .filter((audit) => audit.allow)
        .reduce(
          (count, audit) =>
            count +
            audit.promptOperation +
            audit.workflowOperation +
            audit.mediaOperation,
          0,
        ),
      queue_before: queueBefore,
      queue_after: queueAfter,
    },
    observations: {
      census_rows: contract.rows.length,
      frontend_rows: contract.rows.filter((row) => row.layer === "frontend")
        .length,
      backend_rows: contract.rows.filter((row) => row.layer === "backend")
        .length,
      frontend_ready_shapes: live.observations.filter(
        (row) => row.readiness_state === "ready",
      ).length,
      normalized_fixture_rows: normalizedFixture.wire.observations.length,
      fixture_rows_passed: fixtureComparison.passed,
      fixture_rows_drifted: fixtureComparison.drifted,
      object_info_node_count: Object.keys(objectInfo).length,
      node_definition_count: live.nodeDefinitionCount,
      node_definition_observed_floor: registeredNodeTypes.bound?.observed_floor,
      node_definition_derived_ceiling:
        registeredNodeTypes.bound?.derived_ceiling,
      node_definition_absolute_ceiling:
        registeredNodeTypes.bound?.absolute_ceiling,
      node_definition_count_bucket: "thousands",
      inspection_latency_bucket: "sub_10ms",
      shipped_node_canary_count: shippedNodeIds.length,
      extension_bundle_manifest_entries: extensionBundlePaths.length,
      candidate_bundle_injection_count: candidateBundleInjectionCount,
      candidate_bundle_resource_type: candidateBundleResourceType,
      candidate_sidebar_registry_count: candidateSidebarRegistryCount,
      generation_profile_get_status: profileResponse.status,
    },
    observed_fixture: normalizedFixture.wire,
    artifacts: {
      census_bytes: censusBytes.length,
      census_sha256: sha256(censusBytes),
      fixture_bytes: fixtureBytes.length,
      fixture_sha256: sha256(fixtureBytes),
      normalized_fixture_bytes: Buffer.byteLength(normalizedFixture.bytes),
      normalized_fixture_sha256: sha256(normalizedFixture.bytes),
      candidate_bundle_bytes: candidateBundle.bytes.length,
      candidate_bundle_sha256: candidateBundle.sha256,
      browser_loaded_bundle_bytes: candidateBundle.bytes.length,
      browser_loaded_bundle_sha256: candidateBundle.sha256,
      host_installed_bundle_bytes: hostInstalledBundleBytes.length,
      host_installed_bundle_sha256: sha256(hostInstalledBundleBytes),
      host_argv_sha256: sha256(JSON.stringify(system.argv ?? [])),
    },
    result: "PASS",
  };
  const privacy = derivePrivacyCounters(
    evidenceWithoutPrivacy,
    allRequestAudits,
    {
      before: cookiesBefore.length,
      after: cookiesAfter.length,
    },
  );
  expect(privacy).toEqual({
    prompts: 0,
    workflows: 0,
    media: 0,
    credentials: 0,
    cookies: 0,
    paths_or_urls: 0,
    arbitrary_host_values: 0,
  });
  const evidence = { ...evidenceWithoutPrivacy, privacy };
  writeContentFreeEvidence(targetEvidence, evidence);
});
