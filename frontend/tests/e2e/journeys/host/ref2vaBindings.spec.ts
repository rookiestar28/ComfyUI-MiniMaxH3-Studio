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

test("exact host queues dense Ref2VA media bindings without browser disclosure", async ({
  context,
  page,
}) => {
  test.setTimeout(45_000);
  if (hostUrl === undefined) throw new Error("H3_CONTEXT_HOST_URL is required");
  const allowedOrigin = new URL(hostUrl).origin;
  const networkAttribution = monitorH3Network(page, allowedOrigin);
  const candidateNetworkAttribution = await monitorCandidateInitiatorNetwork(
    context,
    page,
    allowedOrigin,
  );
  const injectionCountBeforeNavigation = candidateInjectionCount(context);
  await page.goto(hostUrl, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => {
    const app = (window as unknown as { comfyAPI?: { app?: { app?: any } } })
      .comfyAPI?.app?.app;
    return app?.extensionManager
      ?.getSidebarTabs?.()
      .some((tab: { id: string }) => tab.id === "h3-context");
  });
  await page.waitForFunction((expectedIds) => {
    const app = (window as unknown as { comfyAPI?: { app?: { app?: any } } })
      .comfyAPI?.app?.app;
    const ids = app?.extensionManager
      ?.getSidebarTabs?.()
      .map((tab: { id: string }) => tab.id);
    return expectedIds.every((id) => ids?.includes(id));
  }, expectedCoInstallSidebarIds);
  // CRITICAL: sidebar registration can precede ComfyUI's canvas; the graph write below otherwise
  // enters viewport persistence and fails with `getCanvas: canvas is null` before qualification.
  await page.waitForFunction(() => {
    const app = (window as unknown as { comfyAPI?: { app?: { app?: any } } })
      .comfyAPI?.app?.app;
    return typeof app?.graph?.serialize === "function" && app.canvas != null;
  });
  await assertCandidateBundleInjection(
    page,
    context,
    injectionCountBeforeNavigation,
  );
  await setSupportedH3Language(page, "en");
  await page.evaluate(async () => {
    const runtime = window as unknown as {
      comfyAPI: { app: { app: any }; api: { api: any } };
      __h3M1516Queue?: unknown[];
    };
    const app = runtime.comfyAPI.app.app;
    const tab = app.extensionManager
      .getSidebarTabs()
      .find((candidate: { id: string }) => candidate.id === "h3-context");
    const container = document.createElement("div");
    container.id = "h3-context-m15-16-container";
    document.body.append(container);
    tab.render(container);
    await app.loadApiJson(
      {
        "17": {
          class_type: "LoadImage",
          inputs: { image: "m15-16-private-image.png" },
        },
        "22": {
          class_type: "LoadVideo",
          inputs: { file: "m15-16-private-video.mp4" },
        },
        "31": {
          class_type: "LoadAudio",
          inputs: { audio: "m15-16-private-audio.wav" },
        },
      },
      "m15-16-visible-host-sources",
    );
    // CRITICAL: awaited loadApiJson is the single graph writer. Replaying the same serialized
    // graph immediately can hit ComfyUI's transient null canvas store and fail before qualification.
    const liteGraph = (
      window as unknown as {
        LiteGraph?: { createNode?: (type: string) => any };
      }
    ).LiteGraph;
    const ensureDirectSource = (type: string): void => {
      if (
        (app.graph?._nodes ?? []).some(
          (node: { type?: unknown }) => node.type === type,
        )
      )
        return;
      const source = liteGraph?.createNode?.(type);
      if (source === undefined)
        throw new Error(`The host cannot materialize ${type}`);
      app.graph.add(source);
    };
    ensureDirectSource("LoadImage");
    ensureDirectSource("LoadVideo");
    ensureDirectSource("LoadAudio");
    // The public view remount owns the bounded graph refresh after graph load.
    tab.destroy();
    tab.render(container);
    runtime.__h3M1516Queue = [];
    runtime.comfyAPI.api.api.queuePrompt = (
      queueNumber: number,
      compiled: {
        output?: Record<
          string,
          { class_type?: unknown; inputs?: Record<string, unknown> }
        >;
      },
    ) => {
      const output = compiled.output ?? {};
      const entry = (classType: string) =>
        Object.entries(output).find(
          ([, node]) => node.class_type === classType,
        );
      const request = entry("comfyui_h3_context.H3Context.Request")?.[1].inputs;
      const registry = entry("comfyui_h3_context.H3Context.ReferenceRegistry");
      const generation = entry("MiniMaxH3ReferenceToVideo");
      const component = entry("GetVideoComponents");
      const sourceId = (classType: string): string | undefined =>
        entry(classType)?.[0];
      const sameLink = (left: unknown, right: unknown): boolean =>
        Array.isArray(left) &&
        Array.isArray(right) &&
        left.length === 2 &&
        right.length === 2 &&
        String(left[0]) === String(right[0]) &&
        left[1] === right[1];
      const exactLink = (
        value: unknown,
        id: string | undefined,
        slot: number,
      ): boolean =>
        id !== undefined &&
        Array.isArray(value) &&
        value.length === 2 &&
        String(value[0]) === id &&
        value[1] === slot;
      const registryInputs = registry?.[1].inputs ?? {};
      const generationInputs = generation?.[1].inputs ?? {};
      const componentInputs = component?.[1].inputs ?? {};
      const firstInput = (
        inputs: Record<string, unknown>,
        ...names: string[]
      ): unknown =>
        names.map((name) => inputs[name]).find((value) => value !== undefined);
      const registryImage = firstInput(
        registryInputs,
        "images",
        "images.image0",
      );
      const registryVideo = firstInput(
        registryInputs,
        "videos",
        "videos.video0",
      );
      const registryAudio = firstInput(
        registryInputs,
        "audios",
        "audios.audio0",
      );
      const nativeImage = firstInput(
        generationInputs,
        "ref_images",
        "ref_images.ref_image_0",
      );
      const nativeVideo = firstInput(
        generationInputs,
        "ref_videos",
        "ref_videos.ref_video_0",
      );
      const nativeVideoAudio = firstInput(
        generationInputs,
        "ref_video_audios",
        "ref_video_audios.ref_video_audio_0",
      );
      const nativeAudio = firstInput(
        generationInputs,
        "ref_audios",
        "ref_audios.ref_audio_0",
      );
      runtime.__h3M1516Queue!.push({
        queueNumber,
        taskMode: request?.task_mode,
        imageBound:
          exactLink(registryImage, sourceId("LoadImage"), 0) &&
          sameLink(registryImage, nativeImage),
        audioBound:
          exactLink(registryAudio, sourceId("LoadAudio"), 0) &&
          sameLink(registryAudio, nativeAudio),
        componentBound:
          exactLink(componentInputs.video, sourceId("LoadVideo"), 0) &&
          sameLink(registryVideo, componentInputs.video),
        videoBound:
          exactLink(nativeVideo, component?.[0], 0) &&
          exactLink(nativeVideoAudio, component?.[0], 1),
      });
      return Promise.resolve({
        prompt_id: "00000000-0000-4000-8000-000000000016",
        number: -16,
        node_errors: {},
      });
    };
  });

  await beginSettledH3InteractionPhase(
    page,
    networkAttribution,
    candidateNetworkAttribution,
  );
  const container = page.locator("#h3-context-m15-16-container");
  await container.getByLabel("Task mode").selectOption("ref2va");
  const refIntent = container.getByRole("textbox", { name: "Intent" });
  const refDuration = container.getByRole("spinbutton", {
    name: "Clip duration (seconds)",
  });
  const refSubmit = container.locator('button[type="submit"]');
  await expect(refIntent).toBeEnabled();
  await expect(refDuration).toBeEnabled();
  await expect(refSubmit).toBeDisabled();
  await expect(
    container.getByText(
      "Select at least one reference source before submitting.",
      { exact: true },
    ),
  ).toHaveAttribute("role", "status");
  await refIntent.fill("M17 Ref2VA editable draft");
  await refDuration.fill("5");
  await expect
    .poll(() =>
      container.getByLabel("Reference images").locator("option").count(),
    )
    .toBeGreaterThan(1);
  for (const label of [
    "Reference images",
    "Reference videos",
    "Reference audio",
  ]) {
    await expect
      .poll(() => container.getByLabel(label).locator("option").count())
      .toBeGreaterThan(1);
    const source = await container
      .getByLabel(label)
      .locator("option")
      .evaluateAll((options) =>
        options
          .map((option) => (option as HTMLOptionElement).value)
          .find((value) => value.length > 0),
      );
    if (source === undefined)
      throw new Error(`No opaque source option was available for ${label}`);
    await container.getByLabel(label).selectOption(source);
  }
  await expect(refSubmit).toBeEnabled();
  await expect(container).not.toContainText("m15-16-private-");
  await container
    .getByRole("button", { name: "Replace canvas and start H3 App Mode" })
    .evaluate((button) => (button as HTMLButtonElement).click());
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (window as unknown as { __h3M1516Queue?: unknown[] }).__h3M1516Queue
            ?.length ?? 0,
      ),
    )
    .toBe(1);
  const receipt = await page.evaluate(
    () =>
      (window as unknown as { __h3M1516Queue?: unknown[] }).__h3M1516Queue?.[0],
  );
  expect(receipt).toMatchObject({
    queueNumber: -1,
    taskMode: "ref2va",
    imageBound: true,
    audioBound: true,
    componentBound: true,
    videoBound: true,
  });
  if (candidateBundle !== null)
    expectCompleteHostAssetResolution(
      await hostAssetResolutionReceipt(page, "reference_to_video"),
    );
  const privateStorage = await page.evaluate(() =>
    [...Object.keys(localStorage), ...Object.keys(sessionStorage)].some(
      (key) => {
        if (!/^h3(?:-context)?[.:_-]/i.test(key)) return false;
        return (
          localStorage.getItem(key) ??
          sessionStorage.getItem(key) ??
          ""
        ).includes("m15-16-private-");
      },
    ),
  );
  expect(privateStorage).toBe(false);
  expectCandidateInteractionNetworkLocal(candidateNetworkAttribution);
});
