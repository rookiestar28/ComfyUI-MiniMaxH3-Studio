import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { createReadStream } from "node:fs";
import { mkdir, readFile, realpath, stat, writeFile } from "node:fs/promises";
import { dirname, isAbsolute, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

// prettier-ignore
import { expect, test, type BrowserContext, type Locator, type Page, } from "../../host/fixture";

// prettier-ignore
import { decodeGenerationProfile, familyProfileForTaskMode, } from "../../../../src/contracts/generationProfileCodec";
// prettier-ignore
import { decodeProviderIntentResult, PROVIDER_SETTINGS_SCHEMA, PROVIDER_SETTINGS_REQUEST_SCHEMA, type ProviderSettingsProjection, } from "../../../../src/contracts/providerSettingsCodec";
import { decodeSidebarWorkspaceProjection } from "../../../../src/contracts/sidebarWorkspaceCodec";
import { APP_MODE_ARTIFACT_PREFIX_ROOT } from "../../../../src/host/appMode";
// prettier-ignore
import { INPUT_GEOMETRY_RECEIPT_SCHEMA, INPUT_GEOMETRY_ROUTE, } from "../../../../src/host/inputGeometry";
// prettier-ignore
import { MAX_MEDIA_PREVIEW_BYTES, MEDIA_PREVIEW_REQUEST_SCHEMA, MEDIA_PREVIEW_ROUTE, } from "../../../../src/host/productionMediaPreview";
// prettier-ignore
import { readOfficialAssetInventory, resolveOfficialAssets, } from "../../../../src/host/officialAssetResolution";
import { projectionFromOutput } from "../../../../src/host/sidebarHost";
// prettier-ignore
import { I2VA_SCALE_NODE_TYPE, I2VA_SIZE_NODE_TYPE, OFFICIAL_LENGTH_EXPRESSION, } from "../../../../src/host/templateMaterialization";
// prettier-ignore
import { CANDIDATE_BACKEND_HOST_ROOT_ENV, CANDIDATE_BUNDLE_PATH_ENV, CANDIDATE_BUNDLE_SHA256_ENV, CandidateInitiatorNetworkAttribution, H3NetworkAttribution, loadCandidateBundleEnvironment, verifyCandidateBackendRuntimeEnvironment, waitForStartupNetworkQuiet, } from "../../helpers/candidateBundleHarness";
// prettier-ignore
import { diffGraphSurroundings, type SurroundingsDiffReport, } from "../../../support/surroundingsDiff";

// prettier-ignore
import { hostUrl, m23I2vaInputLocator, m23RealI2vaAuthorized, m23RealI2vaHostPython, m23RuntimeFailureAuthorized, m22ReadOnlyComposition, repositoryRoot, officialAssetManifest, candidateBackendMode, candidateBundle, candidateBackendRuntime, CANDIDATE_BUNDLE_RESOURCE_PATH, expectedCoInstallSidebarIds, repositoryUrl, candidateInjectionStates, candidateBundleResourceUrl, candidateInjectionCount, assertCandidateBundleInjection, monitorH3Network, monitorCandidateInitiatorNetwork, waitForH3Registration, beginSettledH3InteractionPhase, expectCandidateInteractionNetworkLocal, captureM17CanonicalIdentity, setSupportedH3Language, HOST_PERSISTENCE_KEY_FAMILY, HOST_PERSISTENCE_SENTINEL_VALUE, layoutMatrix, layoutStateContracts, layoutStateCriteria, layoutDiagnostic, assertLayoutMeasurement, SAMPLE_AUTHORIZED, SAMPLE_UNET, SAMPLE_CLIP, instrumentHostGraphLoads, hostGraphLoads, resetHostGraphLoads, hostWorkflowAuthorityReceipt, supportedHostQueueCounts, monitorManagedRouteResponses, appModeQueueDiagnostic, waitForAppModeQueue, privateDiagnosticLeakReceipt, waitForHostGraphSettled, openH3AppModeTab, readVisibleGraph, subgraphNodes, compiledLoaderAssets, hostAssetResolutionReceipt, expectCompleteHostAssetResolution, watchHostExecution, sampleProgress, awaitHostExecution, reportedArtifactNames, preflightRealI2vaEnvironment, inspectRealI2vaArtifactUnsafe, inspectRealI2vaArtifact, type HostOfficialAssetManifest, type CandidateInjectionState, type FrontendPerformanceReceipt, type LayoutState, type AppModePhase, type LayoutGridKind, type LayoutVariant, type LayoutPlacement, type LayoutWidthMode, type LayoutLocale, type LayoutTheme, type LayoutLifecycle, type HostPersistenceSnapshot, type LayoutCaptureEvidence, type LayoutEvidenceJoin, type LayoutEvidenceRow, type LayoutCell, type LayoutStateContract, type LayoutRect, type LayoutOwnerStyle, type LayoutControlGeometry, type LayoutNavigationTab, type LayoutHeaderGeometry, type LayoutFocusTargetKind, type LayoutMeasurement, type SerializedGraph, type ManagedRouteReceipt, type HostAssetResolutionReceipt, type SampleProgress, type RealI2vaArtifactMetadata, type RealI2vaEnvironment, } from "../../host/environment";

test("M22-16 keeps optional authoring passive and manual authoring available", async ({
  context,
  page,
}) => {
  test.setTimeout(90_000);
  test.skip(
    candidateBundle === null,
    "exact candidate bundle binding is required",
  );
  test.skip(
    !m22ReadOnlyComposition,
    "M22-16 read-only projection composition is not explicitly enabled",
  );
  if (hostUrl === undefined) throw new Error("H3_CONTEXT_HOST_URL is required");
  if (candidateBackendMode !== "frontend_only_host_graph")
    throw new Error("M22-16 read-only host composition mode is required");

  type CanonicalProfile = Readonly<{
    profile_id: string;
    provider_label: string;
    family: string;
    wire_dialect: string;
    adapter_version: string;
    parser_version: string;
    cost_class: string;
    usage_receipt_required: boolean;
    retention_policy: string;
    qualification_state: string;
    limitations: readonly string[];
    endpoint: string;
  }>;
  const catalog = JSON.parse(
    await readFile(
      resolve(
        repositoryRoot,
        "comfyui_h3_context/contracts/prompt_model_profiles_v6.json",
      ),
      "utf8",
    ),
  ) as {
    schema: string;
    default_profile_id: unknown;
    profiles: CanonicalProfile[];
  };
  expect(catalog.schema).toBe("h3.prompt_model.profiles.v6");
  expect(catalog.default_profile_id).toBeNull();
  const profiles = catalog.profiles.map((profile) => {
    const endpoint = new URL(profile.endpoint);
    return {
      profile_id: profile.profile_id,
      provider_label: profile.provider_label,
      family: profile.family,
      wire_dialect: profile.wire_dialect,
      adapter_version: profile.adapter_version,
      parser_version: profile.parser_version,
      cost_class: profile.cost_class,
      usage_receipt_required: profile.usage_receipt_required,
      retention_policy: profile.retention_policy,
      qualification_state: profile.qualification_state,
      limitations: profile.limitations,
      host: endpoint.hostname,
      port: Number(
        endpoint.port || (endpoint.protocol === "https:" ? 443 : 80),
      ),
    };
  });
  expect(profiles.map((profile) => profile.profile_id)).toEqual([
    "ollama.local",
    "openai.remote",
    "gemini.remote",
    "anthropic.remote",
  ]);
  const defaultProjection: ProviderSettingsProjection = Object.freeze({
    schema: PROVIDER_SETTINGS_SCHEMA,
    revision: 1,
    catalog_empty: false,
    profiles: Object.freeze(profiles),
    selected_profile_id: "",
    selected_model_id: "",
    selected_model: null,
    readiness: "not_configured",
    disclosure: null,
    consent: null,
    consent_required: false,
    credential_required: false,
    credential_present: false,
    credential_last_four: "",
    candidates: Object.freeze([]),
    candidates_truncated: false,
    diagnostic: null,
    reachability_observed: false,
    assisted_authoring: Object.freeze({
      available: true,
      selected: false,
      ready: false,
      authorized_for_this_action: false,
      defaulted: false,
    }),
  });
  const providerSettingsUrl = new URL(
    "/api/h3-context/v1/provider/settings",
    hostUrl,
  ).href;
  const defaultResultWire = {
    schema: PROVIDER_SETTINGS_SCHEMA,
    accepted: true,
    rejection: null,
    projection: defaultProjection,
  };
  decodeProviderIntentResult(defaultResultWire);
  let projectionRouteCount = 0;
  await context.route(providerSettingsUrl, async (route) => {
    const method = route.request().method();
    if (method === "POST") {
      projectionRouteCount += 1;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(defaultResultWire),
      });
      return;
    }
    if (method === "DELETE") {
      await route.fulfill({ status: 204, body: "" });
      return;
    }
    await route.abort("blockedbyclient");
  });

  const queueCounts = async (): Promise<{
    running: number;
    pending: number;
  }> => {
    const response = await page.request.get(new URL("/queue", hostUrl).href);
    expect(response.ok()).toBe(true);
    const queue = (await response.json()) as {
      queue_running?: unknown[];
      queue_pending?: unknown[];
    };
    return {
      running: queue.queue_running?.length ?? 0,
      pending: queue.queue_pending?.length ?? 0,
    };
  };
  const queueBefore = await queueCounts();
  expect(queueBefore).toEqual({ running: 0, pending: 0 });

  const allowedOrigin = new URL(hostUrl).origin;
  const networkAttribution = monitorH3Network(page, allowedOrigin);
  const candidateNetworkAttribution = await monitorCandidateInitiatorNetwork(
    context,
    page,
    allowedOrigin,
  );
  const providerSettingsRequests: Array<{
    method: string;
    origin: string;
    path: string;
    body: string | null;
  }> = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/h3-context/v1/provider/settings")
      providerSettingsRequests.push({
        method: request.method(),
        origin: url.origin,
        path: url.pathname,
        body: request.postData(),
      });
  });

  const injectionCountBeforeNavigation = candidateInjectionCount(context);
  await page.goto(hostUrl, { waitUntil: "domcontentloaded" });
  await waitForH3Registration(page);
  await assertCandidateBundleInjection(
    page,
    context,
    injectionCountBeforeNavigation,
  );
  await beginSettledH3InteractionPhase(
    page,
    networkAttribution,
    candidateNetworkAttribution,
  );

  const graphBefore = await page.evaluate(() => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    return JSON.stringify(app.graph?.serialize?.() ?? null);
  });
  const container = await openH3AppModeTab(page, "h3-context-m22-16-closeout");
  const pages = container.locator("nav [data-page-id]");
  await expect(pages).toHaveCount(3);
  expect(
    await pages.evaluateAll((nodes) =>
      nodes.map((node) => node.getAttribute("data-page-id")),
    ),
  ).toEqual(["context", "production", "settings"]);

  await container.locator('nav [data-page-id="settings"]').click();
  await expect(container.locator(".h3s-pv")).toBeVisible();
  await expect(container.locator(".h3s-pv-rejection")).toHaveCount(0);
  const providerSelect = container.getByRole("combobox", {
    name: "Provider profile",
  });
  await expect(providerSelect).toHaveValue("");
  expect(
    await providerSelect
      .locator("option")
      .evaluateAll((options) =>
        options.map((option) => (option as HTMLOptionElement).value),
      ),
  ).toEqual(["", ...profiles.map((profile) => profile.profile_id)]);
  await expect(container.locator(".h3s-pv-credential")).toHaveCount(0);
  await expect(container.locator(".h3s-pv-consent")).toHaveCount(0);
  await expect(container.locator(".h3s-pv-disclosure")).toHaveCount(0);

  await container.locator('nav [data-page-id="context"]').click();
  await expect(
    container.locator('nav [data-page-id="context"]'),
  ).toHaveAttribute("aria-current", "page");
  await expect(
    container.locator(
      '[data-h3-focus-key="app-native-nodes"], [data-h3-focus-key="app-keep-canvas"]',
    ),
  ).toHaveCount(1);

  const graphAfter = await page.evaluate(() => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    return JSON.stringify(app.graph?.serialize?.() ?? null);
  });
  expect(graphAfter).toBe(graphBefore);
  expect(providerSettingsRequests).toHaveLength(1);
  expect(providerSettingsRequests[0]).toMatchObject({
    method: "POST",
    origin: allowedOrigin,
    path: "/api/h3-context/v1/provider/settings",
  });
  expect(projectionRouteCount).toBe(1);
  expect(JSON.parse(providerSettingsRequests[0]?.body ?? "null")).toEqual({
    schema: PROVIDER_SETTINGS_REQUEST_SCHEMA,
    intent: "read_projection",
    payload: {},
  });

  expect(networkAttribution.snapshot()).toMatchObject({
    interactionRemoteCount: 0,
    interactionProviderCount: 0,
  });
  expectCandidateInteractionNetworkLocal(candidateNetworkAttribution);
  expect(await queueCounts()).toEqual(queueBefore);
});
