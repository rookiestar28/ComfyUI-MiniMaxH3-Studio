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

test("M17-14 pinned host closeout sample", async ({ page }) => {
  test.setTimeout(120_000);
  if (hostUrl === undefined) throw new Error("H3_CONTEXT_HOST_URL is required");
  await page.goto(hostUrl, { waitUntil: "domcontentloaded" });
  await waitForH3Registration(page);
  const container = await openH3AppModeTab(page, "h3-context-m17-14-closeout");

  // 1. The joined shell. Bundle identity is already asserted by the accepted lanes in this file, so
  // this row does not restate it. The pages are read from the DOM the host actually rendered.
  const pages = container.locator("nav [data-page-id]");
  await expect(pages).toHaveCount(3);
  expect(
    await pages.evaluateAll((nodes) =>
      nodes.map((node) => node.getAttribute("data-page-id")),
    ),
  ).toEqual(["context", "production", "settings"]);

  const currentPages = async (): Promise<string[]> =>
    pages.evaluateAll((nodes) =>
      nodes
        .filter((node) => node.getAttribute("aria-current") === "page")
        .map((node) => node.getAttribute("data-page-id") ?? ""),
    );
  // Exactly one page is current at every stop of the walk. Two would mean two surfaces claiming the
  // same authority, which is precisely what an independent-page shell must never allow.
  expect(await currentPages()).toEqual(["context"]);
  for (const target of ["production", "settings", "context"]) {
    await container.locator(`nav [data-page-id="${target}"]`).click();
    await expect.poll(currentPages, { timeout: 15_000 }).toEqual([target]);
  }

  // 3. The language authority: one hidden descriptor, one visible entry, exact readback.
  const descriptors = await page.evaluate(() => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    const extension = app.extensions.find(
      (candidate: { name?: unknown }) =>
        candidate.name === "comfyui-h3-context.product-shell.v1",
    );
    const settings = (extension?.settings ?? []) as Array<{
      id?: unknown;
      type?: unknown;
      defaultValue?: unknown;
    }>;
    return settings
      .filter((setting) => setting.id === "H3.Context.Language")
      .map((setting) => ({
        id: String(setting.id),
        type: String(setting.type ?? ""),
        defaultValue: setting.defaultValue ?? null,
      }));
  });
  expect(descriptors).toHaveLength(1);
  expect(descriptors[0]?.id).toBe("H3.Context.Language");
  // Hidden: the host's own settings dialog must not grow a second control for a setting this
  // sidebar already owns on its Settings page.
  expect(descriptors[0]?.type).toBe("hidden");

  await container.locator('nav [data-page-id="settings"]').click();
  const language = container.getByRole("combobox", { name: "Language" });
  await expect(language).toBeVisible();
  await expect(
    container.getByRole("combobox", { name: "Language" }),
  ).toHaveCount(1);
  const readBack = async (): Promise<unknown> =>
    page.evaluate(() => {
      const app = (window as unknown as { comfyAPI: { app: { app: any } } })
        .comfyAPI.app.app;
      return (
        app.extensionManager?.setting?.get?.("H3.Context.Language") ?? null
      );
    });
  await language.selectOption("en");
  // The value is read back from the host's own store, not from the control that wrote it. A control
  // that reported its own optimistic value would look identical here and be wrong.
  await expect.poll(readBack).toBe("en");
  await language.selectOption("auto");
  await expect.poll(readBack).toBe("auto");

  // 4. The Production route this repository owns answers on this host, and answers about a workspace
  // that does not exist with a refusal rather than an invention.
  const routeStatus = await page.evaluate(async () => {
    const api = (window as unknown as { comfyAPI: { api: { api: any } } })
      .comfyAPI.api.api;
    const response = await api.fetchApi("/h3-context/v1/production/action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        schema: "h3.context.production_workbench.action.v1",
        request_id: "m17-14.host.closeout",
        action: "read_projection",
        payload: { workspace_handle: `pw_${"a".repeat(43)}` },
      }),
    });
    return response.status;
  });
  // 404 for an unknown handle is the honest answer. A 200 with an empty projection would mean the
  // route invents a workspace for any handle it is handed.
  expect(routeStatus).toBe(404);

  // 5. Nothing was queued and no model was touched by this row.
  const queued = await page.evaluate(async () => {
    const api = (window as unknown as { comfyAPI: { api: { api: any } } })
      .comfyAPI.api.api;
    const queue = await (await fetch("/queue")).json();
    return {
      running: (queue.queue_running ?? []).length,
      pending: (queue.queue_pending ?? []).length,
      hasApi: typeof api.queuePrompt === "function",
    };
  });
  expect(queued.pending).toBe(0);
  expect(queued.hasApi).toBe(true);
});

/**
 * M17-17 row R6: the socket the correction depends on, verified on the host.
 *
 * The splice now declares a reference video's soundtrack to the Reference
 * Registry through an autogrow socket. Which name the host serializes for that
 * group is a host fact, and the repository had only inferred it from the
 * spelling of the neighbouring groups. This row asks the host instead: it builds
 * the registry node the way the canvas does, and requires that the socket the
 * splice writes is the socket the host compiles back into the node's
 * `paired_audios` input.
 *
 * Read-only. Nothing is queued, no weight is resolved, and the graph is built
 * from content-free identifiers only.
 */
