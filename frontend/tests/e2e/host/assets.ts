import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { createReadStream } from "node:fs";
import { mkdir, readFile, realpath, stat, writeFile } from "node:fs/promises";
import { dirname, isAbsolute, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

import {
  expect,
  test,
  type BrowserContext,
  type Locator,
  type Page,
} from "@playwright/test";

import {
  decodeGenerationProfile,
  familyProfileForTaskMode,
} from "../../../src/contracts/generationProfileCodec";
import {
  decodeProviderIntentResult,
  PROVIDER_SETTINGS_SCHEMA,
  PROVIDER_SETTINGS_REQUEST_SCHEMA,
  type ProviderSettingsProjection,
} from "../../../src/contracts/providerSettingsCodec";
import { decodeSidebarWorkspaceProjection } from "../../../src/contracts/sidebarWorkspaceCodec";
import { APP_MODE_ARTIFACT_PREFIX_ROOT } from "../../../src/host/appMode";
import {
  INPUT_GEOMETRY_RECEIPT_SCHEMA,
  INPUT_GEOMETRY_ROUTE,
} from "../../../src/host/inputGeometry";
import {
  MAX_MEDIA_PREVIEW_BYTES,
  MEDIA_PREVIEW_REQUEST_SCHEMA,
  MEDIA_PREVIEW_ROUTE,
} from "../../../src/host/productionMediaPreview";
import {
  readOfficialAssetInventory,
  resolveOfficialAssets,
} from "../../../src/host/officialAssetResolution";
import { projectionFromOutput } from "../../../src/host/sidebarHost";
import {
  I2VA_SCALE_NODE_TYPE,
  I2VA_SIZE_NODE_TYPE,
  OFFICIAL_LENGTH_EXPRESSION,
} from "../../../src/host/templateMaterialization";
import {
  CANDIDATE_BACKEND_HOST_ROOT_ENV,
  CANDIDATE_BUNDLE_PATH_ENV,
  CANDIDATE_BUNDLE_SHA256_ENV,
  CandidateInitiatorNetworkAttribution,
  H3NetworkAttribution,
  loadCandidateBundleEnvironment,
  verifyCandidateBackendRuntimeEnvironment,
  waitForStartupNetworkQuiet,
} from "../helpers/candidateBundleHarness";
import {
  diffGraphSurroundings,
  type SurroundingsDiffReport,
} from "../../support/surroundingsDiff";

import { officialAssetManifest } from "./candidate";
import { setSupportedH3Language } from "./layout";
import { type SerializedGraph } from "./managed";
export async function openH3AppModeTab(
  page: Page,
  containerId: string,
): Promise<Locator> {
  const dialog = page.locator(
    '[role="dialog"][aria-labelledby="global-workflow-template-selector"]',
  );
  if (await dialog.isVisible()) {
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
  }
  // IMPORTANT: this helper drives copy-bearing controls, while the supplied
  // host can legitimately persist any supported ComfyUI locale between runs.
  await setSupportedH3Language(page, "en");
  // The panel the host would give this tab, reproduced: a fixed-height column
  // whose content area scrolls. Rendering into a bare fixed div instead let the
  // projected view overflow without a scroll parent, so a control below the fold
  // -- the setup-return action the edit closure starts from -- could not be
  // reached by the test or by a user with the same window.
  await page.evaluate((id) => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    const tab = app.extensionManager
      .getSidebarTabs()
      .find((candidate: { id: string }) => candidate.id === "h3-context");
    if (tab === undefined || tab.type !== "custom")
      throw new Error("H3 custom tab is absent");
    const panel = document.createElement("div");
    panel.className = "side-bar-panel";
    panel.style.width = "44rem";
    panel.style.position = "fixed";
    panel.style.inset = "80px auto 0 58px";
    panel.style.zIndex = "2000";
    panel.style.background = "#202124";
    panel.style.display = "flex";
    panel.style.flexDirection = "column";
    const content = document.createElement("div");
    content.className = "sidebar-content-container";
    content.style.flex = "1";
    content.style.minHeight = "0";
    content.style.overflow = "auto";
    const container = document.createElement("div");
    container.id = id;
    content.append(container);
    panel.append(content);
    document.body.append(panel);
    tab.render(container);
  }, containerId);
  const container = page.locator(`#${containerId}`);
  await expect(container.locator("[data-shell-status]")).toHaveCount(1);
  return container;
}

/** The visible graph, as the host itself serializes it. */
export async function readVisibleGraph(page: Page): Promise<SerializedGraph> {
  return (await page.evaluate(() =>
    (
      window as unknown as { comfyAPI: { app: { app: any } } }
    ).comfyAPI.app.app.graph.serialize(),
  )) as SerializedGraph;
}

export function subgraphNodes(
  graph: SerializedGraph,
): Array<Record<string, unknown>> {
  const definitions = graph.definitions?.subgraphs ?? [];
  return definitions.flatMap((definition) =>
    Array.isArray(definition.nodes)
      ? (definition.nodes as Array<Record<string, unknown>>)
      : [],
  );
}

/**
 * Stand in for the user resolving the unsatisfied model roles on their canvas.
 *
 * D2 puts that action on the canvas widget and this repository never selects a
 * weight itself; here the maintainer named the files and the test sets the
 * widgets they would set. The template packages its generator as a subgraph and
 * promotes the loaders' widgets onto the node, so `unet_name` and `clip_name`
 * are widgets of the visible node -- and they are what the host compiles from.
 * Editing the loaders inside the definition, or reloading a workflow with them
 * rewritten, both leave the promoted values in place and would have queued the
 * weights the template shipped with.
 */
/** What the host would run right now, read back from its own compiler. */
export async function compiledLoaderAssets(page: Page): Promise<string[]> {
  return (await page.evaluate(async () => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    const compiled = await app.graphToPrompt();
    return Object.values(compiled?.output ?? {})
      .filter((node: any) =>
        ["UNETLoader", "CLIPLoader"].includes(String(node?.class_type)),
      )
      .map((node: any) => String(Object.values(node?.inputs ?? {})[0]));
  })) as string[];
}

export type HostAssetResolutionReceipt = Readonly<{
  requiredRoles: number;
  uniqueCompiledRoles: number;
  exactInventoryRoles: number;
  declaredOfficialRoles: number;
  exactDefaultRoles: number;
  relocatedDefaultRoles: number;
  officialVariantRoles: number;
}>;

/**
 * Read the final host compiler/inventory join without returning a private name.
 *
 * The manifest values sent into the page are public package data. Host-owned
 * inventory strings remain inside the browser and collapse to bounded counts.
 */
export async function hostAssetResolutionReceipt(
  page: Page,
  family: "image_to_video" | "reference_to_video",
): Promise<HostAssetResolutionReceipt> {
  const slots = officialAssetManifest.materialization_families[family];
  const specs = slots.map((slot) => {
    const spec = officialAssetManifest.slots.find(
      (candidate) => candidate.slot === slot,
    );
    if (spec === undefined)
      throw new Error("official asset manifest is incomplete");
    return spec;
  });
  return await page.evaluate(async (expected) => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    const compiled = await app.graphToPrompt();
    const output = Object.values(compiled?.output ?? {}) as Array<{
      class_type?: unknown;
      inputs?: Record<string, unknown>;
    }>;
    const basename = (value: string): string =>
      value.replaceAll("\\", "/").split("/").at(-1)!.toLowerCase();
    let uniqueCompiledRoles = 0;
    let exactInventoryRoles = 0;
    let declaredOfficialRoles = 0;
    let exactDefaultRoles = 0;
    let relocatedDefaultRoles = 0;
    let officialVariantRoles = 0;
    for (const spec of expected) {
      const declared = new Set(
        spec.accepted_basenames.map((value) => value.toLowerCase()),
      );
      const candidates = output
        .filter((node) => node.class_type === spec.loader_type)
        .map((node) => node.inputs?.[spec.widget_name])
        .filter(
          (value): value is string =>
            typeof value === "string" && declared.has(basename(value)),
        );
      if (candidates.length !== 1) continue;
      uniqueCompiledRoles += 1;
      const selected = candidates[0]!;
      const definitions = Object.values(
        (
          window as unknown as {
            LiteGraph?: { registered_node_types?: Record<string, unknown> };
          }
        ).LiteGraph?.registered_node_types ?? {},
      ).filter(
        (entry) =>
          entry !== null &&
          (typeof entry === "object" || typeof entry === "function") &&
          (entry as { nodeData?: { name?: unknown } }).nodeData?.name ===
            spec.loader_type,
      );
      const nodeData =
        definitions.length === 1
          ? (
              definitions[0] as {
                nodeData?: {
                  input?: { required?: Record<string, unknown> };
                };
              }
            ).nodeData
          : undefined;
      const inventory = nodeData?.input?.required?.[spec.widget_name];
      if (
        Array.isArray(inventory) &&
        Array.isArray(inventory[0]) &&
        inventory[0].includes(selected)
      )
        exactInventoryRoles += 1;
      if (declared.has(basename(selected))) declaredOfficialRoles += 1;
      if (selected === spec.template_default) exactDefaultRoles += 1;
      else if (basename(selected) === spec.template_default.toLowerCase())
        relocatedDefaultRoles += 1;
      else officialVariantRoles += 1;
    }
    return {
      requiredRoles: expected.length,
      uniqueCompiledRoles,
      exactInventoryRoles,
      declaredOfficialRoles,
      exactDefaultRoles,
      relocatedDefaultRoles,
      officialVariantRoles,
    };
  }, specs);
}

export function expectCompleteHostAssetResolution(
  receipt: HostAssetResolutionReceipt,
): void {
  expect(receipt.uniqueCompiledRoles).toBe(receipt.requiredRoles);
  expect(receipt.exactInventoryRoles).toBe(receipt.requiredRoles);
  expect(receipt.declaredOfficialRoles).toBe(receipt.requiredRoles);
  expect(
    receipt.exactDefaultRoles +
      receipt.relocatedDefaultRoles +
      receipt.officialVariantRoles,
  ).toBe(receipt.requiredRoles);
  expect(receipt.officialVariantRoles).toBeGreaterThan(0);
}

/**
 * Record what the host executed *for this run*, before anything is queued.
 *
 * A supplied host is a shared machine: the maintainer and other work can queue
 * their own prompts while this lane runs, and their failures are not this
 * lane's failures. Every event is therefore kept with the prompt it belongs to,
 * and the queue seam is wrapped to learn which prompt ids are this run's. What
 * the assertions read is the intersection.
 */
// M23-26 registration phase Five.
// prettier-ignore
import type { HostOfficialAssetManifest, CandidateInjectionState, LayoutState, AppModePhase, LayoutGridKind, LayoutVariant, LayoutPlacement, LayoutWidthMode, LayoutLocale, LayoutTheme, LayoutLifecycle, HostPersistenceSnapshot, LayoutCaptureEvidence, LayoutEvidenceJoin, LayoutEvidenceRow, LayoutCell, LayoutStateContract, LayoutRect, LayoutOwnerStyle, LayoutControlGeometry, LayoutNavigationTab, LayoutHeaderGeometry, LayoutFocusTargetKind, LayoutMeasurement, ManagedRouteReceipt, SampleProgress, RealI2vaArtifactMetadata, RealI2vaEnvironment, FrontendPerformanceReceipt } from "./environment";
// prettier-ignore
import type { runRegistrationPhaseFour } from "./managed";
export async function runRegistrationPhaseFive(
  state: Awaited<ReturnType<typeof runRegistrationPhaseFour>>,
) {
  // prettier-ignore
  const { hostUrl, m23I2vaInputLocator, m23RealI2vaAuthorized, m23RealI2vaHostPython, m23RuntimeFailureAuthorized, m22ReadOnlyComposition, repositoryRoot, officialAssetManifest, candidateBackendMode, candidateBundle, candidateBackendRuntime, CANDIDATE_BUNDLE_RESOURCE_PATH, expectedCoInstallSidebarIds, repositoryUrl, candidateInjectionStates, candidateBundleResourceUrl, candidateInjectionCount, assertCandidateBundleInjection, setSupportedH3Language, HOST_PERSISTENCE_KEY_FAMILY, HOST_PERSISTENCE_SENTINEL_VALUE, layoutMatrix, layoutStateContracts, layoutStateCriteria, layoutDiagnostic, assertLayoutMeasurement, SAMPLE_AUTHORIZED, SAMPLE_UNET, SAMPLE_CLIP, instrumentHostGraphLoads, hostGraphLoads, resetHostGraphLoads, hostWorkflowAuthorityReceipt, supportedHostQueueCounts, monitorManagedRouteResponses, appModeQueueDiagnostic, waitForAppModeQueue, privateDiagnosticLeakReceipt, waitForHostGraphSettled, openH3AppModeTab, readVisibleGraph, subgraphNodes, compiledLoaderAssets, hostAssetResolutionReceipt, expectCompleteHostAssetResolution, watchHostExecution, sampleProgress, awaitHostExecution, reportedArtifactNames, preflightRealI2vaEnvironment, inspectRealI2vaArtifactUnsafe, inspectRealI2vaArtifact, monitorH3Network, monitorCandidateInitiatorNetwork, waitForH3Registration, beginSettledH3InteractionPhase, expectCandidateInteractionNetworkLocal, captureM17CanonicalIdentity } = state.shared;
  // prettier-ignore
  const { context, page, testInfo, allowedOrigin, networkAttribution, candidateNetworkAttribution, captureInteractionNetworkPhase, interactionErrors, initialInjectionCount, initialStorageKeys, registryEvidence, coInstallDefinitions, workflowTemplateDialog, appModeContainer, shellStatus, awaitAppModePhase, visualDirectory, snapshotHostStorage, snapshotHostPersistence, seedHostPersistenceSentinel, originalHostPersistence, initialHostStorage, hostPersistenceBefore, storageSentinelDigest, hostPersistenceBeforeDigest, snapshotHostOwner, layoutEvidence, captureManifest, visualPhase, candidateIdentity, hostReportPath, captureSettledScreenshot, applyLayoutCell, captureLayoutMatrix, projectedWorkspace, resetLayoutMatrixPresentation, networkAfterLayoutFixturePrep, cancelAction, preCancellationGraph, cancellationReason, cancellationReceipt } = state;
  await page.evaluate(() => {
    (
      window as unknown as {
        __h3ProjectionTrace?: Array<Record<string, unknown>>;
      }
    ).__h3ProjectionTrace = [];
    (
      window as unknown as {
        __h3HostProjectionTrace?: Array<Record<string, unknown>>;
      }
    ).__h3HostProjectionTrace = [];
    const api = (
      window as unknown as {
        comfyAPI: { api: { api: { queuePrompt: Function } } };
        __h3AppModeQueueShape?: unknown;
      }
    ).comfyAPI.api.api;
    const original = api.queuePrompt;
    api.queuePrompt = function (...args: unknown[]) {
      const compiled = args[1] as { output?: Record<string, unknown> };
      const request = compiled?.output?.["1"] as
        { inputs?: Record<string, unknown> } | undefined;
      (
        window as unknown as { __h3AppModeQueueShape?: unknown }
      ).__h3AppModeQueueShape = {
        output_keys: Object.keys(compiled?.output ?? {}).sort(),
        output_types: Object.fromEntries(
          Object.entries(compiled?.output ?? {}).map(([id, node]) => [
            id,
            (node as { class_type?: unknown }).class_type ?? null,
          ]),
        ),
        input_signatures: Object.fromEntries(
          Object.entries(compiled?.output ?? {}).map(([id, node]) => [
            id,
            Object.fromEntries(
              Object.entries(
                (node as { inputs?: Record<string, unknown> }).inputs ?? {},
              ).map(([name, value]) => [
                name,
                Array.isArray(value)
                  ? `link:${value.join(":")}`
                  : `${typeof value}:${name === "user_intent" ? String(value).length : String(value)}`,
              ]),
            ),
          ]),
        ),
        request_input_keys: Object.keys(request?.inputs ?? {}).sort(),
        user_intent_type: typeof request?.inputs?.user_intent,
        user_intent_length:
          typeof request?.inputs?.user_intent === "string"
            ? request.inputs.user_intent.length
            : null,
        duration_seconds: request?.inputs?.duration_seconds ?? null,
        frame_count: request?.inputs?.frame_count ?? null,
      };
      // CRITICAL: forward the one managed submission to the host EXACTLY as
      // the product compiled it, and let the sidebar-default generation run
      // for real. Both cheaper variants were tried on this host and both
      // fail: pruning SaveVideo and the H3 Preview from the forwarded copy
      // keeps execution light, but the managed coordinator then finds no
      // saved artifact and ends the run output_verification_failed (the
      // projection arrives, `projected` never does); pruning the native
      // anchor instead leaves the sampler a dangling dependency and the host
      // refuses the prompt with node errors. Under M23-19, `projected` and
      // `ready` exist only after verified output, so a row whose tail lives
      // in those states must pay for one real generation.
      return original.apply(this, args);
    };
  });
  let appModeQueueRequests = 0;
  const appModeNetworkShapes: Array<Record<string, unknown>> = [];
  const appModeResponseShapes: Array<Record<string, unknown>> = [];
  const appModeExecutedShapes: Array<Record<string, unknown>> = [];
  await page.evaluate(() => {
    const api = (
      window as unknown as {
        comfyAPI: {
          api: { api: EventTarget & { addEventListener: Function } };
        };
      }
    ).comfyAPI.api.api;
    api.addEventListener("executed", (event: Event) => {
      const customEvent = event as CustomEvent<unknown>;
      const detail =
        customEvent.detail !== null && typeof customEvent.detail === "object"
          ? (customEvent.detail as Record<string, unknown>)
          : {};
      const output =
        detail.output !== null && typeof detail.output === "object"
          ? (detail.output as Record<string, unknown>)
          : {};
      const correlation =
        output.correlation !== null &&
        Array.isArray(output.correlation) &&
        output.correlation[0] !== null &&
        typeof output.correlation[0] === "object"
          ? (output.correlation[0] as Record<string, unknown>)
          : {};
      const workspace =
        output.sidebar_workspace !== null &&
        Array.isArray(output.sidebar_workspace) &&
        output.sidebar_workspace[0] !== null &&
        typeof output.sidebar_workspace[0] === "object"
          ? (output.sidebar_workspace[0] as Record<string, unknown>)
          : {};
      const workspaceCorrelation =
        workspace.correlation !== null &&
        typeof workspace.correlation === "object"
          ? (workspace.correlation as Record<string, unknown>)
          : {};
      const emit = (window as unknown as { __h3AppModeExecuted?: Function })
        .__h3AppModeExecuted;
      emit?.({
        node: detail.node ?? null,
        display_node: detail.display_node ?? null,
        output_keys: Object.keys(output).sort(),
        execution_node_id: correlation.execution_node_id ?? null,
        workspace_execution_node_id:
          workspaceCorrelation.execution_node_id ?? null,
      });
    });
    (
      window as unknown as { __h3AppModeExecutedSink?: unknown }
    ).__h3AppModeExecutedSink = [];
    (
      window as unknown as { __h3AppModeExecuted?: Function }
    ).__h3AppModeExecuted = (value: unknown) => {
      const sink = (window as unknown as { __h3AppModeExecutedSink: unknown[] })
        .__h3AppModeExecutedSink;
      sink.push(value);
    };
  });
  page.on("request", (request) => {
    if (
      request.method() === "POST" &&
      new URL(request.url()).pathname.endsWith("/prompt")
    ) {
      appModeQueueRequests += 1;
      const body = request.postDataJSON() as {
        prompt?: Record<
          string,
          { class_type?: unknown; inputs?: Record<string, unknown> }
        >;
      } | null;
      const requestInputs = body?.prompt?.["1"]?.inputs;
      appModeNetworkShapes.push({
        prompt_keys: Object.keys(body?.prompt ?? {}).sort(),
        prompt_types: Object.fromEntries(
          Object.entries(body?.prompt ?? {}).map(([id, node]) => [
            id,
            node.class_type ?? null,
          ]),
        ),
        request_input_keys: Object.keys(requestInputs ?? {}).sort(),
        user_intent_type: typeof requestInputs?.user_intent,
        user_intent_length:
          typeof requestInputs?.user_intent === "string"
            ? requestInputs.user_intent.length
            : null,
        frame_count: requestInputs?.frame_count ?? null,
        duration_seconds: requestInputs?.duration_seconds ?? null,
      });
    }
  });
  page.on("response", async (response) => {
    if (
      response.request().method() === "POST" &&
      new URL(response.url()).pathname.endsWith("/prompt")
    ) {
      try {
        const body = (await response.json()) as Record<string, unknown>;
        appModeResponseShapes.push({
          status: response.status(),
          keys: Object.keys(body).sort(),
          has_prompt_id: typeof body.prompt_id === "string",
          has_node_errors: body.node_errors !== undefined,
          node_error_keys:
            body.node_errors !== null && typeof body.node_errors === "object"
              ? Object.keys(body.node_errors as Record<string, unknown>).sort()
              : [],
          node_error_summary: Array.isArray(body.node_errors)
            ? body.node_errors.map((value) =>
                value !== null && typeof value === "object"
                  ? Object.keys(value as Record<string, unknown>).sort()
                  : typeof value,
              )
            : body.node_errors !== null && typeof body.node_errors === "object"
              ? Object.fromEntries(
                  Object.entries(
                    body.node_errors as Record<string, unknown>,
                  ).map(([nodeId, value]) => [
                    nodeId,
                    value !== null && typeof value === "object"
                      ? Object.keys(value as Record<string, unknown>).sort()
                      : typeof value,
                  ]),
                )
              : {},
        });
      } catch {
        appModeResponseShapes.push({
          status: response.status(),
          parseable: false,
        });
      }
    }
  });
  const layoutTaskMode = appModeContainer.locator(
    '[data-h3-focus-key="app-task-mode"]',
  );
  await expect(layoutTaskMode).toHaveValue("t2va");
  const layoutDuration = appModeContainer.locator(
    '[data-h3-focus-key="app-duration-seconds"]',
  );
  await expect(layoutDuration).toHaveValue("5");
  await expect(
    appModeContainer.getByText("Delivers 5 s (124 frames).", { exact: true }),
  ).toBeVisible();
  await appModeContainer
    .getByRole("textbox", { name: "Intent" })
    .fill("A red kite crosses the sky while the camera follows its arc.");
  await appModeContainer
    .getByRole("button", { name: "Start H3 App Mode" })
    .click();
  const appModeEntry = appModeContainer.locator(".h3-app-mode");
  try {
    await expect(
      appModeContainer.locator('[data-shell-status="projected"]'),
    ).toBeVisible({ timeout: 20 * 60_000 });
    await expect(shellStatus).toHaveText("ready", {
      timeout: 60_000,
    });
  } catch (error) {
    const appModeHeaderStatus = await shellStatus.textContent();
    const appModeShellReason = await appModeContainer
      .locator("[data-shell-status]")
      .getAttribute("data-shell-reason");
    const appModeAlertText = await appModeContainer
      .getByRole("alert")
      .allTextContents()
      .then((texts) => texts.join(" | "))
      .catch(() => "");
    let appModeJournal = "";
    try {
      await page.evaluate(() => {
        const runtime = window as unknown as {
          __h3LayoutRowDiagnostics?: string;
        };
        runtime.__h3LayoutRowDiagnostics = undefined;
        Object.defineProperty(navigator, "clipboard", {
          configurable: true,
          value: {
            writeText: async (payload: string) => {
              (
                window as unknown as { __h3LayoutRowDiagnostics?: string }
              ).__h3LayoutRowDiagnostics = payload;
            },
          },
        });
      });
      await appModeContainer
        .getByRole("button", { name: "Copy diagnostics" })
        .click({ timeout: 5_000 });
      appModeJournal = await page.evaluate(
        () =>
          (window as unknown as { __h3LayoutRowDiagnostics?: string })
            .__h3LayoutRowDiagnostics ?? "",
      );
    } catch {
      appModeJournal = "(diagnostics copy unavailable)";
    }
    const appModeGraphDebug = await page.evaluate(() => {
      const app = (window as unknown as { comfyAPI: { app: { app: any } } })
        .comfyAPI.app.app;
      const graph = app.graph.serialize();
      return {
        graph_keys: Object.keys(graph).sort(),
        node_keys: graph.nodes.map((node: Record<string, unknown>) => ({
          type: node.type,
          keys: Object.keys(node).sort(),
          inputs: Array.isArray(node.inputs)
            ? node.inputs.map((input: Record<string, unknown>) => ({
                name: input.name,
                keys: Object.keys(input).sort(),
                widget_keys:
                  input.widget !== null && typeof input.widget === "object"
                    ? Object.keys(
                        input.widget as Record<string, unknown>,
                      ).sort()
                    : null,
              }))
            : null,
          outputs: Array.isArray(node.outputs)
            ? node.outputs.map((output: Record<string, unknown>) => ({
                name: output.name,
                type: output.type,
                keys: Object.keys(output).sort(),
              }))
            : null,
          widgets: Array.isArray(node.widgets_values)
            ? node.widgets_values.map((value: unknown, index: number) =>
                node.type === "comfyui_h3_context.H3Context.Request" &&
                index === 1
                  ? `string:length:${String(value).length}`
                  : `${typeof value}:${String(value)}`,
              )
            : null,
        })),
        anchors: graph.nodes
          .filter(
            (node: { type?: unknown }) =>
              node.type === "comfyui_h3_context.H3Context.ProductShell",
          )
          .map((node: { id?: unknown }) => node.id),
        executed: (window as unknown as { __h3AppModeExecutedSink?: unknown[] })
          .__h3AppModeExecutedSink,
      };
    });
    const queueShape = await page.evaluate(
      () =>
        (window as unknown as { __h3AppModeQueueShape?: unknown })
          .__h3AppModeQueueShape,
    );
    const projectionTrace = await page.evaluate(
      () =>
        (
          window as unknown as {
            __h3ProjectionTrace?: Array<Record<string, unknown>>;
          }
        ).__h3ProjectionTrace,
    );
    const hostProjectionTrace = await page.evaluate(
      () =>
        (
          window as unknown as {
            __h3HostProjectionTrace?: Array<Record<string, unknown>>;
          }
        ).__h3HostProjectionTrace,
    );
    throw new Error(
      `App Mode projection did not arrive: status=${appModeHeaderStatus}; reason=${appModeShellReason}; alert=${appModeAlertText}; journal=${appModeJournal}; graph=${JSON.stringify(appModeGraphDebug)}; queue_shape=${JSON.stringify(queueShape)}; host_projection_trace=${JSON.stringify(hostProjectionTrace)}; projection_trace=${JSON.stringify(projectionTrace)}; network_shapes=${JSON.stringify(appModeNetworkShapes)}; response_shapes=${JSON.stringify(appModeResponseShapes)}`,
      { cause: error },
    );
  }
  expect(appModeQueueRequests).toBe(1);
  expect(appModeNetworkShapes).toHaveLength(1);
  const appModePromptTypes = Object.values(
    (appModeNetworkShapes[0]?.prompt_types ?? {}) as Record<string, unknown>,
  );
  // M23-19 (reconciled by M23-37): the one managed submission carries the
  // whole model prompt — generation chain and terminal outputs included —
  // and the ProductShell bootstrap executes inside it.
  expect(appModePromptTypes).toContain("SaveVideo");
  expect(appModePromptTypes).toContain("comfyui_h3_context.H3Context.Preview");
  expect(appModePromptTypes).toContain(
    "comfyui_h3_context.H3Context.ProductShell",
  );
  const appModeGraph = await page.evaluate(() => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    const graph = app.graph.serialize();
    const shellCount = graph.nodes.filter(
      (node: { type?: unknown }) =>
        node.type === "comfyui_h3_context.H3Context.ProductShell",
    ).length;
    return { shellCount, nodeCount: graph.nodes.length };
  });
  expect(appModeGraph.shellCount).toBe(1);
  expect(appModeGraph.nodeCount).toBeGreaterThan(1);
  const metadata = page.locator(
    "#h3-context-e2e-container .h3-context-metadata",
  );
  await expect(metadata).toBeVisible();
  await expect(metadata.locator(".h3-context-version")).toHaveText("v1.0.2");
  const githubLink = metadata.getByRole("link", { name: "View on GitHub" });
  await expect(githubLink).toHaveAttribute("href", repositoryUrl);
  await expect(githubLink).toHaveAttribute("target", "_blank");
  await expect(githubLink).toHaveAttribute("rel", "noopener noreferrer");
  const metadataStyles = await metadata.evaluate((element) => {
    const style = getComputedStyle(element);
    const link = element.querySelector<HTMLElement>(".h3-context-github");
    if (link === null) throw new Error("metadata link is absent");
    const linkStyle = getComputedStyle(link);
    return {
      fontFamily: style.fontFamily,
      fontSize: style.fontSize,
      groupGap: style.gap,
      linkGap: linkStyle.gap,
      padding: `${linkStyle.paddingTop} ${linkStyle.paddingRight}`,
      borderWidth: linkStyle.borderTopWidth,
      radius: linkStyle.borderTopLeftRadius,
      idleBackground: linkStyle.backgroundColor,
    };
  });
  expect(metadataStyles.fontFamily.toLowerCase()).toContain("arial");
  expect(metadataStyles.fontSize).toBe("12px");
  expect(metadataStyles.groupGap).toBe("8px");
  expect(metadataStyles.linkGap).toBe("6px");
  expect(metadataStyles.padding).toBe("4px 8px");
  expect(metadataStyles.borderWidth).toBe("1px");
  expect(metadataStyles.radius).toBe("5px");
  const networkAfterAppModeStart = captureInteractionNetworkPhase(
    "after_app_mode_start",
  );

  // prettier-ignore
  return { ...state, appModeQueueRequests, appModeNetworkShapes, appModeResponseShapes, appModeExecutedShapes, layoutTaskMode, layoutDuration, appModeEntry, appModePromptTypes, appModeGraph, metadata, githubLink, metadataStyles, networkAfterAppModeStart };
}
