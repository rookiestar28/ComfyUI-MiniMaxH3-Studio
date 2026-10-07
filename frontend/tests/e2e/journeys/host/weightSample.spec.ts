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

test("M17-20 authorized weight-backed sample writes a real artifact and a second one after an edit", async ({
  page,
}) => {
  test.skip(
    !SAMPLE_AUTHORIZED ||
      typeof m23I2vaInputLocator !== "string" ||
      m23I2vaInputLocator.length === 0,
    "AC-M17-20-08 requires explicit per-session authorization and a private source image locator",
  );
  // Model load plus two generations on a supplied host. The ceiling was agreed
  // with the maintainer before the run; this is the wall-clock half of it.
  test.setTimeout(45 * 60_000);
  if (hostUrl === undefined) throw new Error("H3_CONTEXT_HOST_URL is required");
  await page.goto(hostUrl, { waitUntil: "domcontentloaded" });
  await waitForH3Registration(page);
  await instrumentHostGraphLoads(page);
  await waitForHostGraphSettled(page);
  const container = await openH3AppModeTab(page, "h3-context-m17-20-sample");
  await watchHostExecution(page);

  // The i2va first-frame role binds to an image node on the user's canvas,
  // and the sidebar's source inventory lists the live canvas's top-level
  // image nodes. The canvas this page opens on is whatever workflow the
  // host's user profile last held, so the row supplies its own precondition
  // — one LoadImage carrying the authorized source image — instead of
  // depending on residue another row happened to leave active.
  await page.evaluate((locator) => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    const liteGraph = (
      window as unknown as {
        LiteGraph?: { createNode?: (type: string) => any };
      }
    ).LiteGraph;
    let source = (app.graph?._nodes ?? []).find(
      (node: { type?: unknown }) => node.type === "LoadImage",
    );
    if (source === undefined) {
      source = liteGraph?.createNode?.("LoadImage");
      if (source === undefined)
        throw new Error("The host cannot materialize a LoadImage node");
      source.pos = [420, 120];
      app.graph.add(source);
    }
    const widget = (source.widgets ?? []).find(
      (candidate: { name?: unknown }) => candidate?.name === "image",
    );
    if (widget !== undefined) widget.value = locator;
  }, m23I2vaInputLocator as string);

  const shellReason = async (): Promise<string> =>
    (await container
      .locator("[data-shell-status]")
      .getAttribute("data-shell-reason")) ?? "";

  // The sidebar owns the context: task mode, intent, the confirmed duration and
  // the i2va first-frame role. The Plan node refuses a run whose first-frame
  // role is unbound (`missing_first_frame`) or whose duration it never
  // confirmed on the official lattice (`duration_snapped`).
  const configureSidebar = async (intent: string): Promise<void> => {
    const taskMode = container.locator('[data-h3-focus-key="app-task-mode"]');
    await expect(taskMode).toBeVisible({ timeout: 30_000 });
    await taskMode.selectOption("i2va");
    const source = container.locator('[data-h3-focus-key="app-first-frame"]');
    try {
      await expect
        .poll(async () => await source.locator("option").count())
        .toBeGreaterThan(1);
    } catch (error) {
      const scene = await page.evaluate(() => {
        const app = (window as unknown as { comfyAPI: { app: { app: any } } })
          .comfyAPI.app.app;
        const graph = app.graph.serialize();
        return {
          topLevelTypes: graph.nodes.map((node: { type?: unknown }) =>
            String(node.type),
          ),
        };
      });
      const optionLabels = await source
        .locator("option")
        .allTextContents()
        .catch(() => []);
      const shellStatus = await container
        .locator("[data-shell-status]")
        .getAttribute("data-shell-status");
      const shellReason = await container
        .locator("[data-shell-status]")
        .getAttribute("data-shell-reason");
      throw new Error(
        `first-frame source inventory empty: status=${shellStatus}; reason=${shellReason}; options=${JSON.stringify(optionLabels)}; canvas=${JSON.stringify(scene.topLevelTypes)}`,
        { cause: error },
      );
    }
    if ((await source.inputValue()) === "")
      await source.selectOption({ index: 1 });
    await container.locator('[data-h3-focus-key="app-intent"]').fill(intent);
    await container
      .locator('[data-h3-focus-key="app-duration-seconds"]')
      .fill("8");
    await expect(
      container.getByText("Delivers 8 s (192 frames).", { exact: true }),
    ).toBeVisible();
  };

  const runRevision = async (intent: string): Promise<string[]> => {
    await configureSidebar(intent);
    const submit = container.locator('[data-h3-focus-key="app-submit"]');
    await expect(submit).toBeEnabled({ timeout: 120_000 });
    const fromSubmission = (await sampleProgress(page)).submitted;
    await submit.click();
    // A canvas that already holds a graph is never overwritten silently. The
    // second revision therefore takes the same two presses a user takes: start,
    // then confirm the replacement of the canvas the previous revision left.
    await page.waitForTimeout(700);
    if ((await shellReason()) === "dirty_graph") await submit.click();
    // The host has to accept the prompt and begin executing it; weights load
    // inside that execution, so this wait is short on purpose.
    await expect
      .poll(async () => (await sampleProgress(page, fromSubmission)).started, {
        timeout: 180_000,
      })
      .toBeGreaterThan(0);
    // Every official role the profile requires resolved against what this host
    // actually has installed (M17-29). The maintainer's standing designation
    // accepts any MiniMax H3 weight that shares a name with the ComfyUI
    // HuggingFace repository, so the precision variant carries no meaning here
    // and this row must never pin one: it asserts that the roles resolved to
    // installed official files, not which variant the resolver chose.
    expectCompleteHostAssetResolution(
      await hostAssetResolutionReceipt(page, "image_to_video"),
    );
    const loaders = await compiledLoaderAssets(page);
    expect(loaders.length).toBeGreaterThanOrEqual(2);
    for (const name of loaders) expect(name.length).toBeGreaterThan(0);
    const result = await awaitHostExecution(page, fromSubmission, 40 * 60_000);
    expect(result.errors).toEqual([]);
    const names = reportedArtifactNames(result.outputs);
    expect(names.length).toBeGreaterThan(0);
    // Settle the shell to `projected` before returning (the M23-19 row does
    // the same): the caller's next step is `edit-app-mode-setup`, and editing
    // while output verification is still consuming the projection re-enters
    // setup without the image-source inventory, so the next configure sees a
    // single empty option and can never bind the i2va first-frame role.
    await expect(
      container.locator('[data-shell-status="projected"]'),
    ).toHaveCount(1, { timeout: 180_000 });
    return names;
  };

  const first = await runRevision(
    "A red kite crosses the sky while the camera follows its arc.",
  );
  expect(first.some((name) => name.includes("h3-context"))).toBe(true);

  // AC-M17-20-04: an edited revision materializes its own graph and writes its
  // own artifact; the previous one cannot satisfy it.
  await container.locator('[data-h3-focus-key="page-context"]').click();
  const edit = container.locator('[data-h3-focus-key="edit-app-mode-setup"]');
  await expect(edit).toBeVisible({ timeout: 30_000 });
  await edit.click();
  const second = await runRevision(
    "A red kite crosses the sky and the camera holds still.",
  );
  expect(second.some((name) => name.includes("h3-context"))).toBe(true);
  for (const name of second) expect(first).not.toContain(name);
});

/**
 * M17-14 closeout sample.
 *
 * Every accepted item has already been proved on this host in its own lane. What no lane asserts is
 * the joined shell: that the three pages a user moves between are the accepted three, in the
 * accepted order, that moving between them leaves exactly one body and no lost focus, that the sole
 * language authority is one hidden descriptor whose write reads back exactly, and that the
 * Production route this repository owns answers on the same host in the same session.
 *
 * It runs no model, resolves no weight and queues nothing. M17-20's accepted lane and its authorized
 * weight-backed sample own that evidence; repeating it here would spend a real generation to learn
 * nothing new.
 */
