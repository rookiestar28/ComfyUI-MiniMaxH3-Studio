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

test("M17-17 pinned host resolves the paired soundtrack socket", async ({
  page,
}) => {
  test.setTimeout(60_000);
  if (hostUrl === undefined) throw new Error("H3_CONTEXT_HOST_URL is required");
  await page.goto(hostUrl, { waitUntil: "domcontentloaded" });
  await waitForH3Registration(page);

  const observed = await page.evaluate(async () => {
    const runtime = window as unknown as {
      comfyAPI: { app: { app: any } };
      LiteGraph?: { createNode?: (type: string) => any };
    };
    const app = runtime.comfyAPI.app.app;
    const registryType = "comfyui_h3_context.H3Context.ReferenceRegistry";

    // 1. What the host publishes for this node, read from its own definitions.
    const definition = await (
      await fetch(`/object_info/${registryType}`)
    ).json();
    const optional = definition?.[registryType]?.input?.optional ?? {};

    // 2. What the node serializes once the group carries an item. The canvas
    //    writes `paired_audios.paired_audio0`; if the host spelled the slot
    //    differently, the splice would produce a registry with no soundtrack in
    //    it and the drift would come back in a new shape.
    const workflow = {
      last_node_id: 3,
      last_link_id: 2,
      nodes: [
        {
          id: 1,
          type: "LoadVideo",
          pos: [0, 0],
          widgets_values: ["m17-17-content-free.mp4"],
          outputs: [{ name: "VIDEO", type: "VIDEO", links: [1] }],
          inputs: [],
        },
        {
          id: 2,
          type: "GetVideoComponents",
          pos: [200, 0],
          inputs: [{ name: "video", type: "VIDEO", link: 1 }],
          outputs: [
            { name: "images", type: "IMAGE", links: [] },
            { name: "audio", type: "AUDIO", links: [2] },
          ],
        },
        {
          id: 3,
          type: registryType,
          pos: [400, 0],
          inputs: [
            {
              name: "paired_audios.paired_audio0",
              type: "AUDIO",
              shape: 7,
              link: 2,
            },
          ],
          outputs: [
            {
              name: "reference_registry",
              type: "H3_REFERENCE_REGISTRY",
              links: [],
            },
          ],
        },
      ],
      links: [
        [1, 1, 0, 2, 0, "VIDEO"],
        [2, 2, 1, 3, 0, "AUDIO"],
      ],
      groups: [],
      config: {},
      extra: {},
      version: 0.4,
    };
    await app.loadGraphData(workflow);
    await new Promise((settle) => setTimeout(settle, 0));
    const { output } = await app.graphToPrompt();
    const compiled = Object.values(
      output as Record<string, { class_type?: string; inputs?: unknown }>,
    ).find((node) => node.class_type === registryType);
    return {
      declaresPairedAudios: Object.keys(optional).includes("paired_audios"),
      pairedAudiosType: optional?.paired_audios?.[0] ?? null,
      compiledInputs: Object.keys(
        (compiled?.inputs ?? {}) as Record<string, unknown>,
      ),
    };
  });

  // The node this repository ships publishes the socket the correction writes.
  expect(observed.declaresPairedAudios).toBe(true);
  expect(observed.pairedAudiosType).toBe("AUDIO");
  // And the host compiles the serialized slot back into that input rather than
  // discarding it as an unknown name.
  expect(
    observed.compiledInputs.filter((name) => name.startsWith("paired_audio")),
  ).toEqual(["paired_audios.paired_audio0"]);

  // Nothing was queued by this row.
  const pending = await page.evaluate(async () => {
    const queue = await (await fetch("/queue")).json();
    return (queue.queue_pending ?? []).length;
  });
  expect(pending).toBe(0);
});

/**
 * M22-16 closeout: exact candidate frontend on the user-supplied pinned host.
 *
 * The candidate adds declarative compatibility files that this read-only lane is forbidden to
 * copy into the running host, so the accepted harness binds the exact candidate bundle to a closed
 * default projection derived from the candidate's canonical profile catalog. The interaction itself
 * remains read-only: one intercepted same-origin `read_projection`, no host-backend write, no
 * selection, no credential, no readiness probe, no provider call, no graph write and no queue
 * submission.
 */
