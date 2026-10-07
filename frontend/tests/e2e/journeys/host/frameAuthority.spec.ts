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

test("M23-38 connected I2VA declares its graph-owned frame and writes a real artifact", async ({
  context,
  page,
}) => {
  test.skip(
    !SAMPLE_AUTHORIZED ||
      typeof m23I2vaInputLocator !== "string" ||
      m23I2vaInputLocator.length === 0,
    "M23-38 requires explicit weight-sample authority and the owner-supplied private source locator",
  );
  test.setTimeout(45 * 60_000);
  if (hostUrl === undefined) throw new Error("H3_CONTEXT_HOST_URL is required");
  const allowedOrigin = new URL(hostUrl).origin;
  const [templateResponse, objectInfoResponse] = await Promise.all([
    page.request.get(
      new URL("/templates/video_minimax_h3_i2v.json", hostUrl).href,
    ),
    page.request.get(new URL("/object_info", hostUrl).href),
  ]);
  if (!templateResponse.ok() || !objectInfoResponse.ok())
    throw new Error(
      "the supplied host could not provide the I2VA precondition",
    );
  // IMPORTANT: Connect preserves caller-owned model widgets. Resolve only this
  // test precondition up front; rewriting them in the product would overwrite
  // a user's later model choice and can make a valid host graph unqueueable.
  const assetResolution = resolveOfficialAssets(
    (await templateResponse.json()) as Record<string, unknown>,
    "image_to_video",
    readOfficialAssetInventory(await objectInfoResponse.json()),
  );
  expect(assetResolution.unresolvedSlots).toEqual([]);
  expect(assetResolution.bindings).toHaveLength(
    officialAssetManifest.materialization_families.image_to_video.length,
  );
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
  await instrumentHostGraphLoads(page);
  await waitForHostGraphSettled(page);
  await watchHostExecution(page);

  const prepared = await page.evaluate(
    async ({ locator, template }) => {
      const app = (window as unknown as { comfyAPI: { app: { app: any } } })
        .comfyAPI.app.app;
      const resolvedTemplate = template as {
        nodes?: Array<Record<string, unknown>>;
        definitions?: {
          subgraphs?: Array<{ nodes?: Array<Record<string, unknown>> }>;
        };
      };
      const allNodes = [
        ...(resolvedTemplate.nodes ?? []),
        ...(resolvedTemplate.definitions?.subgraphs ?? []).flatMap(
          (definition) => definition.nodes ?? [],
        ),
      ];
      const loaders = allNodes.filter((node) => node.type === "LoadImage");
      if (loaders.length !== 1)
        throw new Error(
          "the official I2VA template has no singular image role",
        );
      const widgets = Array.isArray(loaders[0]!.widgets_values)
        ? [...loaders[0]!.widgets_values]
        : [];
      if (widgets.length === 0)
        throw new Error("the official I2VA image role has no locator widget");
      widgets[0] = locator;
      loaders[0]!.widgets_values = widgets;
      await app.loadGraphData(resolvedTemplate);
      const durationWidget = (app.graph?._nodes ?? [])
        .flatMap((node: { widgets?: unknown[] }) => node.widgets ?? [])
        .find((widget: { name?: unknown }) => widget.name === "value_1") as
        { value?: unknown; callback?: (value: number) => void } | undefined;
      if (durationWidget === undefined)
        throw new Error("I2VA visible duration widget is unavailable");
      durationWidget.value = 5.167;
      durationWidget.callback?.(5.167);
      app.graph.change?.();
      await new Promise((settle) => setTimeout(settle, 0));
      return {
        sourceCount: allNodes.filter((node) => node.type === "LoadImage")
          .length,
        durationSeconds: durationWidget.value,
      };
    },
    {
      locator: m23I2vaInputLocator!,
      template: assetResolution.workflow,
    },
  );
  expect(prepared).toEqual({ sourceCount: 1, durationSeconds: 5.167 });

  const container = await openH3AppModeTab(
    page,
    "h3-context-m23-38-live-connect",
  );
  await beginSettledH3InteractionPhase(
    page,
    networkAttribution,
    candidateNetworkAttribution,
  );
  await container.getByLabel("Task mode").selectOption("i2va");
  await expect(container.getByLabel("First frame source")).toHaveValue("");
  await container
    .locator('[data-h3-focus-key="app-intent"]')
    .fill("A red kite crosses the sky while the camera follows its arc.");
  const connect = container.getByRole("button", {
    name: "Connect and queue current canvas",
  });
  await expect(connect).toBeEnabled({ timeout: 30_000 });
  const fromSubmission = (await sampleProgress(page)).submitted;
  await connect.click();
  await expect
    .poll(async () => (await sampleProgress(page, fromSubmission)).started, {
      timeout: 180_000,
    })
    .toBeGreaterThan(0);
  const result = await awaitHostExecution(page, fromSubmission, 40 * 60_000);
  expect(result.errors).toEqual([]);
  const artifacts = reportedArtifactNames(result.outputs);
  expect(artifacts.length).toBeGreaterThan(0);

  // IMPORTANT: installed extensions may rewrite the shared canvas after an
  // execution. Bind this live proof to the prompt id accepted at the queue
  // seam; a later canvas snapshot can lose the owned edge and misattribute a
  // successful generation as a Connect failure.
  const acceptedPromptIds = await page.evaluate((from) => {
    const sample = (
      window as unknown as { __h3Sample?: { promptIds?: string[] } }
    ).__h3Sample;
    return (sample?.promptIds ?? []).slice(from);
  }, fromSubmission);
  expect(acceptedPromptIds).toHaveLength(1);
  const acceptedPromptId = acceptedPromptIds[0]!;
  const historyResponse = await page.request.get(
    new URL(`/history/${encodeURIComponent(acceptedPromptId)}`, hostUrl).href,
  );
  if (!historyResponse.ok())
    throw new Error("the supplied host did not retain the accepted prompt");
  const history = (await historyResponse.json()) as Record<
    string,
    { prompt?: unknown[] }
  >;
  const acceptedOutput = history[acceptedPromptId]?.prompt?.[2] as
    | Record<string, { class_type?: unknown; inputs?: Record<string, unknown> }>
    | undefined;
  const acceptedNodes = Object.values(acceptedOutput ?? {});
  const anchor = acceptedNodes.find(
    (node) => node.class_type === "MiniMaxH3ImageToVideo",
  );
  const registry = acceptedNodes.find(
    (node) =>
      node.class_type === "comfyui_h3_context.H3Context.ReferenceRegistry",
  );
  expect(
    acceptedNodes.filter((node) => node.class_type === "MiniMaxH3ImageToVideo"),
  ).toHaveLength(1);
  expect(
    acceptedNodes.filter(
      (node) =>
        node.class_type === "comfyui_h3_context.H3Context.ReferenceRegistry",
    ),
  ).toHaveLength(1);
  const nativeFirst = anchor?.inputs?.first_frame;
  const declaredFirst = registry?.inputs?.first_frame;
  expect(Array.isArray(nativeFirst)).toBe(true);
  expect(Array.isArray(declaredFirst)).toBe(true);
  expect(nativeFirst).toEqual(declaredFirst);
  expect(await supportedHostQueueCounts(page)).toEqual({
    running: 0,
    pending: 0,
  });
  expectCandidateInteractionNetworkLocal(candidateNetworkAttribution);
  expect(networkAttribution.snapshot().interactionProviderCount).toBe(0);
});
