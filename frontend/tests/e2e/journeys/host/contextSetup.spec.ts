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

test("M17-19 supported host returns projected Context to setup without host side effects", async ({
  context,
  page,
}) => {
  test.setTimeout(60_000);
  if (hostUrl === undefined) throw new Error("H3_CONTEXT_HOST_URL is required");
  const allowedOrigin = new URL(hostUrl).origin;
  const networkAttribution = monitorH3Network(page, allowedOrigin);
  const candidateNetworkAttribution = await monitorCandidateInitiatorNetwork(
    context,
    page,
    allowedOrigin,
  );
  const initialInjectionCount = candidateInjectionCount(context);
  await page.goto(hostUrl, { waitUntil: "domcontentloaded" });
  await waitForH3Registration(page);
  await assertCandidateBundleInjection(page, context, initialInjectionCount);
  const workflowTemplateDialog = page.locator(
    '[role="dialog"][aria-labelledby="global-workflow-template-selector"]',
  );
  if (await workflowTemplateDialog.isVisible()) {
    await page.keyboard.press("Escape");
    await expect(workflowTemplateDialog).toBeHidden();
  }
  const appModeContainer = await openH3AppModeTab(
    page,
    "h3-context-m17-19-e2e-container",
  );
  await expect(
    appModeContainer.locator('[data-shell-status="interactive"]'),
  ).toHaveCount(1);
  await expect(
    appModeContainer.getByRole("textbox", { name: "Intent" }),
  ).toBeVisible();
  await beginSettledH3InteractionPhase(
    page,
    networkAttribution,
    candidateNetworkAttribution,
  );
  await page.evaluate(() => {
    const runtime = window as unknown as {
      comfyAPI: {
        app: { app: any };
        api: { api: { addEventListener: Function; queuePrompt: Function } };
      };
      __h3Executed?: Array<{ node?: unknown; workspace?: unknown }>;
      __h3M17QueueCount?: number;
    };
    const api = runtime.comfyAPI.api.api;
    runtime.__h3Executed = [];
    runtime.__h3M17QueueCount = 0;
    const originalQueuePrompt = api.queuePrompt;
    api.queuePrompt = function (...args: unknown[]) {
      runtime.__h3M17QueueCount = (runtime.__h3M17QueueCount ?? 0) + 1;
      return originalQueuePrompt.apply(this, args);
    };
    api.addEventListener("executed", (event: Event) => {
      const detail = (event as CustomEvent<Record<string, unknown>>).detail;
      const output = detail?.output as
        Record<string, unknown[]> | null | undefined;
      runtime.__h3Executed?.push({
        node: detail?.node,
        workspace: output?.sidebar_workspace?.[0],
      });
    });
  });
  const fixture = JSON.parse(
    await readFile(
      resolve(process.cwd(), "../workflows/m15_03_product_shell_base.json"),
      "utf8",
    ),
  ) as { prompt: Record<string, unknown> };
  const prompt = structuredClone(fixture.prompt);
  const executionPrompt = structuredClone(prompt);
  delete executionPrompt["7"];
  await page.evaluate(async (promptValue) => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    app.loadApiJson(promptValue, "m17-19-supported-host-e2e");
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 0));
  }, prompt);
  const response = await page.evaluate(
    async ({ endpoint, promptValue }) => {
      const clientId = (
        window as unknown as {
          comfyAPI: { api: { api: { clientId: string } } };
        }
      ).comfyAPI.api.api.clientId;
      const result = await fetch(endpoint, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ client_id: clientId, prompt: promptValue }),
      });
      return { status: result.status, text: await result.text() };
    },
    {
      endpoint: new URL("/prompt", hostUrl).href,
      promptValue: executionPrompt,
    },
  );
  expect(response.status, response.text).toBe(200);
  await page.waitForFunction(() =>
    (
      (window as unknown as { __h3Executed?: Array<{ node?: unknown }> })
        .__h3Executed ?? []
    ).some((event) => event.node === "6"),
  );
  await expect(
    appModeContainer.locator('[data-shell-status="projected"]'),
  ).toHaveCount(1);
  await expect(appModeContainer.getByRole("status")).toHaveText("ready");
  const beforeEdit = await page.evaluate(() => {
    const runtime = window as unknown as {
      comfyAPI: { app: { app: any } };
      __h3Executed?: unknown[];
      __h3M17QueueCount?: number;
    };
    return {
      graph: JSON.stringify(runtime.comfyAPI.app.app.graph.serialize()),
      executed: runtime.__h3Executed?.length ?? 0,
      queue: runtime.__h3M17QueueCount ?? 0,
    };
  });
  const networkBefore = networkAttribution.snapshot();
  const candidateNetworkBefore =
    candidateNetworkAttribution?.snapshot() ?? null;
  const editSetup = appModeContainer.getByRole("button", {
    name: "Edit App Mode setup",
  });
  const readScrollReceipt = () =>
    editSetup.evaluate((element) => {
      const owner = element.closest<HTMLElement>(".sidebar-content-container");
      const panel = owner?.closest<HTMLElement>(".side-bar-panel") ?? null;
      const controlBounds = element.getBoundingClientRect();
      const ownerBounds = owner?.getBoundingClientRect() ?? null;
      const panelBounds = panel?.getBoundingClientRect() ?? null;
      return {
        ownerPresent: owner !== null,
        panelPresent: panel !== null,
        ownerOverflowY:
          owner === null ? null : getComputedStyle(owner).overflowY,
        ownerScrollTop: owner?.scrollTop ?? null,
        ownerScrollHeight: owner?.scrollHeight ?? null,
        ownerClientHeight: owner?.clientHeight ?? null,
        controlTop: controlBounds.top,
        controlBottom: controlBounds.bottom,
        ownerTop: ownerBounds?.top ?? null,
        ownerBottom: ownerBounds?.bottom ?? null,
        panelTop: panelBounds?.top ?? null,
        panelBottom: panelBounds?.bottom ?? null,
      };
    });
  const beforeScroll = await readScrollReceipt();
  expect(beforeScroll.ownerPresent, JSON.stringify(beforeScroll)).toBe(true);
  expect(beforeScroll.panelPresent, JSON.stringify(beforeScroll)).toBe(true);
  expect(beforeScroll.ownerOverflowY, JSON.stringify(beforeScroll)).toMatch(
    /auto|scroll/,
  );
  expect(
    Number(beforeScroll.ownerScrollHeight),
    JSON.stringify(beforeScroll),
  ).toBeGreaterThan(Number(beforeScroll.ownerClientHeight));
  expect(
    Number(beforeScroll.controlTop) < Number(beforeScroll.ownerTop) ||
      Number(beforeScroll.controlBottom) > Number(beforeScroll.ownerBottom),
    JSON.stringify(beforeScroll),
  ).toBe(true);
  await editSetup.scrollIntoViewIfNeeded();
  const afterScroll = await readScrollReceipt();
  expect(
    Number(afterScroll.ownerScrollTop),
    JSON.stringify({ beforeScroll, afterScroll }),
  ).toBeGreaterThan(Number(beforeScroll.ownerScrollTop));
  expect(
    Number(afterScroll.controlTop),
    JSON.stringify({ beforeScroll, afterScroll }),
  ).toBeGreaterThanOrEqual(Number(afterScroll.ownerTop));
  expect(
    Number(afterScroll.controlBottom),
    JSON.stringify({ beforeScroll, afterScroll }),
  ).toBeLessThanOrEqual(Number(afterScroll.ownerBottom));
  expect(
    Number(afterScroll.controlTop),
    JSON.stringify({ beforeScroll, afterScroll }),
  ).toBeGreaterThanOrEqual(Number(afterScroll.panelTop));
  expect(
    Number(afterScroll.controlBottom),
    JSON.stringify({ beforeScroll, afterScroll }),
  ).toBeLessThanOrEqual(Number(afterScroll.panelBottom));
  await expect(editSetup).toBeInViewport();
  await editSetup.click();
  await expect(
    appModeContainer.locator('[data-shell-status="editing_setup"]'),
  ).toHaveCount(1);
  await expect(
    appModeContainer.getByRole("textbox", { name: "Intent" }),
  ).toBeVisible();
  await expect(
    appModeContainer.locator('[data-h3-focus-key="app-stage-intent"]'),
  ).toBeFocused();
  const duringEdit = await page.evaluate(() => {
    const runtime = window as unknown as {
      comfyAPI: { app: { app: any } };
      __h3Executed?: unknown[];
      __h3M17QueueCount?: number;
    };
    return {
      graph: JSON.stringify(runtime.comfyAPI.app.app.graph.serialize()),
      executed: runtime.__h3Executed?.length ?? 0,
      queue: runtime.__h3M17QueueCount ?? 0,
    };
  });
  expect(duringEdit).toEqual(beforeEdit);
  expect(networkAttribution.snapshot()).toEqual(networkBefore);
  expect(candidateNetworkAttribution?.snapshot() ?? null).toEqual(
    candidateNetworkBefore,
  );
  await appModeContainer.getByRole("button", { name: "Cancel edit" }).click();
  await expect(
    appModeContainer.locator('[data-shell-status="projected"]'),
  ).toHaveCount(1);
  await expect(
    appModeContainer.locator('[data-h3-focus-key="edit-app-mode-setup"]'),
  ).toBeFocused();
  const afterCancel = await page.evaluate(() => {
    const runtime = window as unknown as {
      comfyAPI: { app: { app: any } };
      __h3Executed?: unknown[];
      __h3M17QueueCount?: number;
    };
    return {
      graph: JSON.stringify(runtime.comfyAPI.app.app.graph.serialize()),
      executed: runtime.__h3Executed?.length ?? 0,
      queue: runtime.__h3M17QueueCount ?? 0,
    };
  });
  expect(afterCancel).toEqual(beforeEdit);
  expect(networkAttribution.snapshot()).toEqual(networkBefore);
  expect(candidateNetworkAttribution?.snapshot() ?? null).toEqual(
    candidateNetworkBefore,
  );
  expectCandidateInteractionNetworkLocal(candidateNetworkAttribution);
});
