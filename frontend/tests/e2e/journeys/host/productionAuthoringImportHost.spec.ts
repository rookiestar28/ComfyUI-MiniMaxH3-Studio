// M25-48 supplied-host row, promoted from the accepted Production-import journey. The host reaches
// one ready Production output through a model-free ProductShell terminal and an explicitly prepared
// tracked media fixture. The import itself remains causal through the shipped compact control; the
// zero-clip asset must acquire, present and release its asset-scoped thumbnail before insertion.
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import type { Request, Response } from "@playwright/test";
import { openExportPanel } from "../../helpers/nleExport";
import { classifyM2556LeaseRequestPhase } from "../../helpers/m25_56LeaseRequestPhase";
import { seekPlayhead } from "../../helpers/nleTimeline";
import { expect, test, type Page } from "../../host/fixture";

import { diffGraphSurroundings } from "../../../support/surroundingsDiff";
import {
  decodeAuthoringProjection,
  decodeTimelineHistoryProjection,
  encodeAuthoringAction,
} from "../../../../src/contracts/authoringWorkbenchCodec";
import {
  decodeProductionAuthoringImportResponseV1,
  encodeProductionAuthoringImportRequest,
} from "../../../../src/contracts/productionAuthoringImportCodec";
import {
  decodeProductionAccumulatedProject,
  PRODUCTION_ACCUMULATION_ACTION_VERSION,
} from "../../../../src/contracts/productionAccumulationCodec";
import {
  decodeProductionWorkbenchProjection,
  encodeProductionAction,
} from "../../../../src/contracts/productionWorkbenchCodec";
import {
  decodeOutputStatus,
  type OutputStatus,
} from "../../../../src/contracts/authoringOutputCodec";
import { decodeSidebarWorkspaceProjection } from "../../../../src/contracts/sidebarWorkspaceCodec";
import {
  assertCandidateBundleInjection,
  awaitHostExecution,
  beginSettledH3InteractionPhase,
  candidateBackendMode,
  candidateBackendRuntime,
  candidateBundle,
  candidateInjectionCount,
  expectCandidateInteractionNetworkLocal,
  hostUrl,
  instrumentHostGraphLoads,
  monitorCandidateInitiatorNetwork,
  monitorH3Network,
  monitorManagedRouteResponses,
  openH3AppModeTab,
  reportedArtifactNames,
  sampleProgress,
  supportedHostQueueCounts,
  waitForH3Registration,
  waitForHostGraphSettled,
  watchHostExecution,
} from "../../host/environment";

type M2548ArtifactLocator = Readonly<{
  filename: string;
  subfolder: "h3-context-m25-48-fixtures";
  type: "output";
}>;

function decodeM2548ArtifactLocator(
  value: string | undefined,
): M2548ArtifactLocator | null {
  if (value === undefined) return null;
  let wire: unknown;
  try {
    wire = JSON.parse(value);
  } catch {
    throw new Error("the M25-48 artifact locator is malformed");
  }
  if (wire === null || typeof wire !== "object" || Array.isArray(wire))
    throw new Error("the M25-48 artifact locator is malformed");
  const record = wire as Record<string, unknown>;
  if (
    Object.keys(record).sort().join() !== "filename,subfolder,type" ||
    typeof record.filename !== "string" ||
    !/^h3-m25-48-[a-z0-9][a-z0-9-]{0,47}\.mp4$/.test(record.filename) ||
    record.subfolder !== "h3-context-m25-48-fixtures" ||
    record.type !== "output"
  )
    throw new Error("the M25-48 artifact locator is malformed");
  return Object.freeze({
    filename: record.filename,
    subfolder: record.subfolder,
    type: record.type,
  });
}

const m2548HostEnabled = process.env.H3_CONTEXT_M25_48_HOST === "1";
const m2548ArtifactLocator = decodeM2548ArtifactLocator(
  process.env.H3_CONTEXT_M25_48_ARTIFACT_LOCATOR,
);

type CloseReceipt = Readonly<{
  artifact: "null" | "locator" | "invalid";
  status: number;
  disposition: string;
  jobState: string;
  completed: number;
  runState: string;
  artifactAuthority: boolean;
  geometry: Readonly<{
    format: string;
    frameCount: number;
    width: number;
    height: number;
  }> | null;
}>;

type HostActionResponse = Readonly<{
  status: number;
  body: unknown;
}>;

async function postHostAction(
  page: Page,
  route: string,
  body: Readonly<Record<string, unknown>>,
): Promise<HostActionResponse> {
  // CRITICAL: the owned routes require a real same-origin browser request. APIRequestContext
  // omits the page's browser-origin metadata and is correctly refused by the host boundary.
  return await page.evaluate(
    async ({ routeValue, bodyValue }) => {
      const response = await fetch(new URL(routeValue, location.origin).href, {
        method: "POST",
        credentials: "same-origin",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(bodyValue),
      });
      const text = await response.text();
      return {
        status: response.status,
        body: text === "" ? null : (JSON.parse(text) as unknown),
      };
    },
    { routeValue: route, bodyValue: body },
  );
}

// M25-34: the same causal import, run on a host whose media tools were never configured.
// `ready` asserts the tools are found without setup; `install` starts from missing tools and
// continues the refused import through the Media tools card's one Install and continue click,
// then renders the imported clip. Unset keeps the AC11 row unchanged.
const mediaJourney = process.env.H3_CONTEXT_M25_34_MEDIA_JOURNEY;
const expectedMediaSource = process.env.H3_CONTEXT_M25_34_EXPECTED_SOURCE;
// The install journey always renders; a ready journey renders when asked (the restart row).
const renderJourney =
  mediaJourney === "install" ||
  (mediaJourney === "ready" && process.env.H3_CONTEXT_M25_34_RENDER === "1");
const ux14Journey = process.env.H3_CONTEXT_M25_56_UX14;
const installedCandidateInventory =
  process.env.H3_CONTEXT_CANDIDATE_INVENTORY_SHA256;
if (
  mediaJourney !== undefined &&
  mediaJourney !== "ready" &&
  mediaJourney !== "install"
)
  throw new Error("H3_CONTEXT_M25_34_MEDIA_JOURNEY must be ready or install");
if (mediaJourney === "ready" && expectedMediaSource === undefined)
  throw new Error("the ready media journey names its expected source");
if (ux14Journey !== undefined && ux14Journey !== "1")
  throw new Error("H3_CONTEXT_M25_56_UX14 must be 1");
if (ux14Journey === "1" && (mediaJourney !== "ready" || !renderJourney))
  throw new Error("the UX-14 journey requires the ready render path");
if (
  ux14Journey === "1" &&
  !/^[0-9a-f]{64}$/.test(installedCandidateInventory ?? "")
)
  throw new Error(
    "the UX-14 journey requires the runner's installed inventory",
  );
const productionImportHostTitle =
  ux14Journey === "1"
    ? "M25-56 supplied host: a real Production import decodes, plays, pauses and releases its monitor source"
    : "M25-48 supplied host: a zero-clip Production import presents and releases an asset thumbnail before explicit insertion";
const MEDIA_FEATURES = [
  "import",
  "preview",
  "derivatives",
  "assembly",
  "render",
] as const;
const SETTLING_REASONS = new Set([
  "activating",
  "discovering",
  "discovery_in_progress",
]);

type MediaRuntimeStatusSample = Readonly<{
  status: number;
  schema: string;
  resolutionState: string;
  resolutionReason: string;
  sourceKind: string;
  features: Readonly<Record<string, string>>;
  reasons: Readonly<Record<string, string>>;
  actions: readonly string[];
  // The install descriptor is on the wire in every state; only the `install_supported` action is
  // the offer, so evidence records the two separately.
  installDescribed: boolean;
  setupRunning: boolean;
}>;

async function readMediaRuntimeStatus(
  page: Page,
): Promise<MediaRuntimeStatusSample> {
  const read = await page.evaluate(async () => {
    const response = await fetch(
      new URL("/h3-context/v1/media-runtime", location.origin).href,
      { credentials: "same-origin" },
    );
    return { status: response.status, body: await response.json() };
  });
  const wire = read.body as Record<string, any>;
  const features: Record<string, string> = {};
  const reasons: Record<string, string> = {};
  for (const feature of MEDIA_FEATURES) {
    features[feature] = String(wire.features?.[feature]?.state ?? "");
    reasons[feature] = String(wire.features?.[feature]?.reason ?? "ready");
  }
  return {
    status: read.status,
    schema: String(wire.schema ?? ""),
    resolutionState: String(wire.resolution?.state ?? ""),
    resolutionReason: String(wire.resolution?.reason ?? ""),
    sourceKind: String(wire.resolution?.source_kind ?? ""),
    features,
    reasons,
    actions: Array.isArray(wire.actions) ? wire.actions.map(String) : [],
    installDescribed:
      wire.install !== null &&
      typeof wire.install === "object" &&
      Number(wire.install.approximate_bytes) > 0,
    setupRunning: wire.setup?.state === "running",
  };
}

/** A fresh start discovers and activates in the background; read once it has settled. */
async function settledMediaRuntimeStatus(
  page: Page,
): Promise<MediaRuntimeStatusSample> {
  let sample = await readMediaRuntimeStatus(page);
  const deadline = Date.now() + 120_000;
  while (
    Date.now() < deadline &&
    (sample.resolutionState === "discovering" ||
      Object.values(sample.reasons).some((reason) =>
        SETTLING_REASONS.has(reason),
      ))
  ) {
    await page.waitForTimeout(1_000);
    sample = await readMediaRuntimeStatus(page);
  }
  return sample;
}

function installM2332ManagedExecutionProjection(): void {
  type CompiledNode = {
    class_type?: unknown;
    inputs?: Record<string, unknown>;
  };
  type CompiledPrompt = {
    output?: Record<string, CompiledNode>;
    workflow?: unknown;
  };
  type QueueReceipt = {
    projectorCalls: number;
    fullEnvelopeCalls: number;
    queueCalls: number;
    projectionStable: boolean;
    queueMatchesProjection: boolean;
    delegatedUnchanged: boolean;
    queuedAssetFree: boolean;
    queuedSourceCount: number;
    queuedLoaderCount: number;
    queuedNativeGeneratorCount: number;
    queuedSinkCount: number;
    queuedProductShellCount: number;
    originalSinkNodeId: string;
  };
  const runtime = window as unknown as {
    comfyAPI: { api: { api: any } };
    __h3M2508ManagedExecutionProjection?: (
      compiled: CompiledPrompt,
    ) => CompiledPrompt;
    __h3M2508ExecutionIdentityTrace?: Array<Record<string, unknown>>;
    __h3M2332QueueReceipt?: QueueReceipt;
  };
  const api = runtime.comfyAPI.api.api;
  if (typeof api.queuePrompt !== "function")
    throw new Error("the public host queue seam is unavailable");
  const originalQueuePrompt = api.queuePrompt.bind(api);
  const loaders = new Set(["UNETLoader", "CLIPLoader", "LoraLoaderModelOnly"]);
  const nativeGenerators = new Set([
    "MiniMaxH3ImageToVideo",
    "MiniMaxH3ReferenceToVideo",
  ]);
  const sources = new Set(["LoadImage", "LoadVideo"]);
  const allowedProjectionTypes = new Set([
    "comfyui_h3_context.H3Context.Request",
    "comfyui_h3_context.H3Context.Plan",
    "comfyui_h3_context.H3Context.Compiler",
    "comfyui_h3_context.H3Context.Validator",
    "comfyui_h3_context.H3Context.NativeH3Adapter",
    "comfyui_h3_context.H3Context.ProductShell",
    "comfyui_h3_context.H3Context.ReferenceRegistry",
    "PrimitiveFloat",
    "PrimitiveInt",
  ]);
  const canonicalJson = (candidate: unknown): string | null => {
    const seen = new WeakSet<object>();
    let members = 0;
    const visit = (value: unknown, depth: number): string | null => {
      members += 1;
      if (members > 4096 || depth > 32) return null;
      if (value === null) return "null";
      if (typeof value === "string" || typeof value === "boolean")
        return JSON.stringify(value);
      if (typeof value === "number")
        return Number.isFinite(value) ? JSON.stringify(value) : null;
      if (typeof value !== "object" || seen.has(value)) return null;
      seen.add(value);
      if (Array.isArray(value)) {
        if (value.length > 512) return null;
        const parts = value.map((entry) => visit(entry, depth + 1));
        return parts.some((entry) => entry === null)
          ? null
          : `[${parts.join(",")}]`;
      }
      const object = value as Record<string, unknown>;
      const keys = Object.keys(object).sort();
      if (keys.length > 256) return null;
      const parts = keys.map((key) => {
        const encoded = visit(object[key], depth + 1);
        return encoded === null ? null : `${JSON.stringify(key)}:${encoded}`;
      });
      return parts.some((entry) => entry === null)
        ? null
        : `{${parts.join(",")}}`;
    };
    return visit(candidate, 0);
  };
  const receipt: QueueReceipt = {
    projectorCalls: 0,
    fullEnvelopeCalls: 0,
    queueCalls: 0,
    projectionStable: true,
    queueMatchesProjection: false,
    delegatedUnchanged: false,
    queuedAssetFree: false,
    queuedSourceCount: 0,
    queuedLoaderCount: 0,
    queuedNativeGeneratorCount: 0,
    queuedSinkCount: 0,
    queuedProductShellCount: 0,
    originalSinkNodeId: "",
  };
  runtime.__h3M2332QueueReceipt = receipt;
  runtime.__h3M2508ExecutionIdentityTrace = [];
  let projectedCanonical: string | undefined;

  // CRITICAL: install the deterministic projection at the webdriver-gated product seam.
  // Moving this closure into queuePrompt would make validation and execution identities differ.
  runtime.__h3M2508ManagedExecutionProjection = (
    compiledValue: CompiledPrompt,
  ): CompiledPrompt => {
    const compiled = structuredClone(compiledValue);
    const output = compiled.output;
    if (output === undefined)
      throw new Error("the M23-32 compiled execution output is unavailable");
    const rows = Object.entries(output);
    const count = (types: ReadonlySet<string>): number =>
      rows.filter(([, node]) => types.has(String(node.class_type ?? "")))
        .length;
    const productShells = rows.filter(
      ([, node]) =>
        node.class_type === "comfyui_h3_context.H3Context.ProductShell",
    );
    const sinks = rows.filter(([, node]) => node.class_type === "SaveVideo");
    if (
      count(sources) !== 0 ||
      count(loaders) !== 3 ||
      count(nativeGenerators) !== 1 ||
      productShells.length !== 1 ||
      sinks.length !== 1
    )
      throw new Error("the M23-32 full generation envelope is unavailable");
    const sinkNodeId = sinks[0]![0];
    if (
      receipt.originalSinkNodeId !== "" &&
      receipt.originalSinkNodeId !== sinkNodeId
    )
      throw new Error("the M23-32 original artifact sink identity changed");
    receipt.originalSinkNodeId = sinkNodeId;
    receipt.fullEnvelopeCalls += 1;

    const included = new Set<string>();
    const pending = [productShells[0]![0]];
    while (pending.length > 0) {
      const nodeId = pending.pop()!;
      if (included.has(nodeId)) continue;
      const node = output[nodeId];
      if (node === undefined)
        throw new Error("the M23-32 closure references a missing node");
      included.add(nodeId);
      for (const value of Object.values(node.inputs ?? {}))
        if (
          Array.isArray(value) &&
          value.length === 2 &&
          (typeof value[0] === "string" || typeof value[0] === "number") &&
          Object.hasOwn(output, String(value[0]))
        )
          pending.push(String(value[0]));
      if (included.size > 128)
        throw new Error("the M23-32 execution closure exceeded its node bound");
    }
    const projectionOutput = Object.fromEntries(
      rows
        .filter(([nodeId]) => included.has(nodeId))
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([nodeId, node]) => [nodeId, structuredClone(node)]),
    );
    const projectionTypes = Object.values(projectionOutput).map((node) =>
      String(node.class_type ?? ""),
    );
    if (
      projectionTypes.length === 0 ||
      projectionTypes.some((type) => !allowedProjectionTypes.has(type)) ||
      projectionTypes.filter(
        (type) => type === "comfyui_h3_context.H3Context.ProductShell",
      ).length !== 1
    )
      throw new Error("the M23-32 runtime projection is not asset-free");
    const projected = { ...compiled, output: projectionOutput };
    const canonical = canonicalJson(projected);
    if (canonical === null)
      throw new Error("the M23-32 runtime projection is not canonical JSON");
    receipt.projectorCalls += 1;
    if (projectedCanonical === undefined) projectedCanonical = canonical;
    else receipt.projectionStable &&= projectedCanonical === canonical;
    return projected;
  };

  api.queuePrompt = async function (batch: unknown, compiled: CompiledPrompt) {
    receipt.queueCalls += 1;
    const delegated = structuredClone(compiled);
    const compiledCanonical = canonicalJson(compiled);
    const delegatedCanonical = canonicalJson(delegated);
    receipt.queueMatchesProjection =
      compiledCanonical !== null && compiledCanonical === projectedCanonical;
    receipt.delegatedUnchanged =
      compiledCanonical !== null && compiledCanonical === delegatedCanonical;
    const queuedRows = Object.entries(delegated.output ?? {});
    const countQueued = (types: ReadonlySet<string>): number =>
      queuedRows.filter(([, node]) => types.has(String(node.class_type ?? "")))
        .length;
    receipt.queuedSourceCount = countQueued(sources);
    receipt.queuedLoaderCount = countQueued(loaders);
    receipt.queuedNativeGeneratorCount = countQueued(nativeGenerators);
    receipt.queuedSinkCount = countQueued(new Set(["SaveVideo"]));
    receipt.queuedProductShellCount = countQueued(
      new Set(["comfyui_h3_context.H3Context.ProductShell"]),
    );
    receipt.queuedAssetFree =
      queuedRows.length > 0 &&
      queuedRows.every(([, node]) =>
        allowedProjectionTypes.has(String(node.class_type ?? "")),
      ) &&
      receipt.queuedSourceCount === 0 &&
      receipt.queuedLoaderCount === 0 &&
      receipt.queuedNativeGeneratorCount === 0 &&
      receipt.queuedSinkCount === 0 &&
      receipt.queuedProductShellCount === 1;
    if (
      !receipt.projectionStable ||
      !receipt.queueMatchesProjection ||
      !receipt.delegatedUnchanged ||
      !receipt.queuedAssetFree
    )
      throw new Error("the M23-32 validated queue identity is not exact");
    return await originalQueuePrompt(batch, delegated);
  };
}

test(productionImportHostTitle, async ({ context, page }, testInfo) => {
  test.skip(
    !m2548HostEnabled ||
      m2548ArtifactLocator === null ||
      candidateBundle === null ||
      candidateBackendRuntime === null ||
      candidateBackendMode !== "exact",
    "M25-48 requires its explicit prepared fixture and an exact installed candidate",
  );
  // The install journey downloads and verifies the fixed archive on the host, then renders.
  test.setTimeout((mediaJourney === "install" ? 40 : 10) * 60_000);
  if (hostUrl === undefined) throw new Error("H3_CONTEXT_HOST_URL is required");

  const queueBefore = await supportedHostQueueCounts(page);
  expect(queueBefore).toEqual({ running: 0, pending: 0 });
  const allowedOrigin = new URL(hostUrl).origin;
  const networkAttribution = monitorH3Network(page, allowedOrigin);
  const candidateNetworkAttribution = await monitorCandidateInitiatorNetwork(
    context,
    page,
    allowedOrigin,
  );
  const routeReceipts = monitorManagedRouteResponses(page);
  const setupPosts: string[] = [];
  const mediaLeaseControls: Array<
    Readonly<{
      operation: string;
      scope: unknown;
      clipId: unknown;
      derivativeKind: unknown;
      status: number;
    }>
  > = [];
  const mediaLeaseResponses: Response[] = [];
  const mediaLeaseCorrelations: Array<
    Readonly<{
      operation: string;
      leaseId: unknown;
      ownerId: unknown;
      requestOrdinal: number;
    }>
  > = [];
  const mediaLeaseRequestOrdinals = new WeakMap<Request, number>();
  let mediaLeaseRequestSequence = 0;
  page.on("request", (request) => {
    const pathname = new URL(request.url()).pathname.replace(
      /^\/api(?=\/)/,
      "",
    );
    if (
      request.method() === "POST" &&
      pathname.startsWith("/h3-context/v1/authoring/media-source-leases")
    )
      mediaLeaseRequestOrdinals.set(request, ++mediaLeaseRequestSequence);
    if (
      request.method() !== "POST" ||
      !new URL(request.url()).pathname.endsWith(
        "/h3-context/v1/media-runtime/setup",
      )
    )
      return;
    try {
      const body = request.postDataJSON() as { action?: unknown } | null;
      setupPosts.push(String(body?.action ?? ""));
    } catch {
      setupPosts.push("unreadable");
    }
  });
  page.on("response", (response) => {
    // IMPORTANT: ComfyUI may expose extension routes through its `/api` wrapper. Normalize only
    // that host-owned prefix or successful lease controls disappear from the acceptance evidence.
    const pathname = new URL(response.url()).pathname.replace(
      /^\/api(?=\/)/,
      "",
    );
    if (
      response.request().method() !== "POST" ||
      !pathname.startsWith("/h3-context/v1/authoring/media-source-leases")
    )
      return;
    try {
      const body = response.request().postDataJSON() as Record<
        string,
        unknown
      > | null;
      mediaLeaseResponses.push(response);
      mediaLeaseCorrelations.push(
        Object.freeze({
          operation: String(body?.operation ?? ""),
          leaseId: body?.leaseId,
          ownerId: body?.ownerId,
          requestOrdinal:
            mediaLeaseRequestOrdinals.get(response.request()) ?? -1,
        }),
      );
      mediaLeaseControls.push(
        Object.freeze({
          operation: String(body?.operation ?? ""),
          scope: body?.scope,
          clipId: body?.clipId,
          derivativeKind: body?.derivativeKind,
          status: response.status(),
        }),
      );
    } catch {
      mediaLeaseResponses.push(response);
      mediaLeaseCorrelations.push(
        Object.freeze({
          operation: "unreadable",
          leaseId: undefined,
          ownerId: undefined,
          requestOrdinal:
            mediaLeaseRequestOrdinals.get(response.request()) ?? -1,
        }),
      );
      mediaLeaseControls.push(
        Object.freeze({
          operation: "unreadable",
          scope: undefined,
          clipId: undefined,
          derivativeKind: undefined,
          status: response.status(),
        }),
      );
    }
  });
  const closeResponses: CloseReceipt[] = [];
  let succeededProductionWire: unknown = null;
  const accumulatedReadTargets: Array<
    Readonly<{
      workspaceHandle: string;
      workspaceId: string;
    }>
  > = [];
  const closeRequests: Array<{
    artifact: CloseReceipt["artifact"];
    queuePromptId: string;
    outputNodeId: string | null;
  }> = [];
  const preparations: Array<{
    expectedFrames: number;
    sourceIdentity: unknown;
    ownedProjectionFingerprint: string;
    ownedNodeIds: string[];
    ownedLinkIds: string[];
    anchorNodeId: string;
  }> = [];
  const artifactKind = (payload: unknown): CloseReceipt["artifact"] => {
    if (payload === null) return "null";
    if (
      payload !== null &&
      typeof payload === "object" &&
      !Array.isArray(payload)
    )
      return "locator";
    return "invalid";
  };
  page.on("request", (request) => {
    if (
      !new URL(request.url()).pathname.endsWith(
        "/h3-context/v1/generation/coordinator",
      )
    )
      return;
    try {
      const body = JSON.parse(request.postData() ?? "null") as {
        action?: unknown;
        payload?: Record<string, unknown>;
      } | null;
      if (body?.action === "prepare_managed_run") {
        const observation = (body.payload?.observation ?? {}) as Record<
          string,
          unknown
        >;
        preparations.push({
          expectedFrames: Number(observation.expected_frames),
          sourceIdentity: observation.source_identity,
          ownedProjectionFingerprint: String(
            observation.owned_projection_fingerprint ?? "",
          ),
          ownedNodeIds: Array.isArray(observation.owned_node_ids)
            ? observation.owned_node_ids.map(String)
            : [],
          ownedLinkIds: Array.isArray(observation.owned_link_ids)
            ? observation.owned_link_ids.map(String)
            : [],
          anchorNodeId: String(observation.native_anchor_node_id ?? ""),
        });
      }
      if (body?.action !== "close_managed_run") return;
      const artifact = body.payload?.artifact;
      const artifactRecord =
        artifact !== null &&
        typeof artifact === "object" &&
        !Array.isArray(artifact)
          ? (artifact as Record<string, unknown>)
          : undefined;
      closeRequests.push({
        artifact: artifactKind(artifact),
        queuePromptId: String(body.payload?.queue_prompt_id ?? ""),
        outputNodeId:
          artifactRecord === undefined
            ? null
            : String(artifactRecord.output_node_id ?? ""),
      });
    } catch {
      // A malformed coordinator body is exposed by the missing bounded receipt.
    }
  });
  page.on("response", (response) => {
    const pathname = new URL(response.url()).pathname;
    if (pathname.endsWith("/h3-context/v1/production/action")) {
      let action = "";
      try {
        const request = response.request().postDataJSON() as {
          action?: unknown;
          payload?: Record<string, unknown>;
        } | null;
        action = String(request?.action ?? "");
        if (
          action === "read_accumulated_project" &&
          typeof request?.payload?.workspace_handle === "string" &&
          typeof request.payload.workspace_id === "string"
        )
          accumulatedReadTargets.push(
            Object.freeze({
              workspaceHandle: request.payload.workspace_handle,
              workspaceId: request.payload.workspace_id,
            }),
          );
      } catch {
        return;
      }
      if (
        (action === "read_projection" ||
          action === "read_accumulated_project") &&
        response.ok()
      )
        void response
          .json()
          .then((wire: unknown) => {
            // IMPORTANT: automatic accumulation v2 wraps the canonical nonempty workspace;
            // treating its envelope as v1 hides a verified output from the import assertion.
            if (action === "read_accumulated_project") {
              const project = decodeProductionAccumulatedProject(wire);
              if (project.workspace !== null)
                succeededProductionWire = structuredClone(
                  (wire as { workspace: unknown }).workspace,
                );
            } else succeededProductionWire = structuredClone(wire);
          })
          .catch(() => undefined);
      return;
    }
    if (!pathname.endsWith("/h3-context/v1/generation/coordinator")) return;
    let requestBody: {
      action?: unknown;
      payload?: Record<string, unknown>;
    } | null;
    try {
      requestBody = JSON.parse(response.request().postData() ?? "null") as {
        action?: unknown;
        payload?: Record<string, unknown>;
      } | null;
    } catch {
      return;
    }
    if (requestBody?.action !== "close_managed_run") return;
    const requestArtifact = requestBody.payload?.artifact;
    void response
      .json()
      .then((value: unknown) => {
        const wire = value as Record<string, any>;
        const segment = Array.isArray(wire.production?.segments)
          ? wire.production.segments[0]
          : undefined;
        const geometry = segment?.delivered_geometry;
        closeResponses.push({
          artifact: artifactKind(requestArtifact),
          status: response.status(),
          disposition: String(wire.disposition ?? ""),
          jobState: String(wire.sequence?.progress?.[0]?.state ?? ""),
          completed: Number(wire.production?.run?.completed ?? -1),
          runState: String(wire.production?.run?.state ?? ""),
          artifactAuthority:
            wire.artifact_authority !== null &&
            typeof wire.artifact_authority === "object",
          geometry:
            geometry !== null && typeof geometry === "object"
              ? {
                  format: String(geometry.format ?? ""),
                  frameCount: Number(geometry.frame_count ?? -1),
                  width: Number(geometry.width ?? -1),
                  height: Number(geometry.height ?? -1),
                }
              : null,
        });
      })
      .catch(() => undefined);
  });

  const initialInjectionCount = candidateInjectionCount(context);
  const browserTabCount = context.pages().length;
  await page.goto(hostUrl, { waitUntil: "domcontentloaded" });
  await waitForH3Registration(page);
  await assertCandidateBundleInjection(page, context, initialInjectionCount);
  await instrumentHostGraphLoads(page);
  await waitForHostGraphSettled(page);
  const workflowBefore = await page.evaluate(() => {
    const runtime = window as unknown as {
      comfyAPI: { app: { app: any }; api: { api: EventTarget } };
      __h3M2332WorkflowAuthority?: object | null;
      __h3M2529ContextEvents?: Array<{
        promptId: unknown;
        workspace: unknown;
      }>;
    };
    const store = runtime.comfyAPI.app.app.extensionManager?.workflow;
    if (!Array.isArray(store?.openWorkflows))
      throw new Error("the public host workflow store is unavailable");
    runtime.__h3M2332WorkflowAuthority = store.activeWorkflow;
    runtime.__h3M2529ContextEvents = [];
    runtime.comfyAPI.api.api.addEventListener("executed", (event: Event) => {
      const detail = (event as CustomEvent<Record<string, unknown>>).detail;
      const output = detail?.output as
        Record<string, unknown[]> | null | undefined;
      const workspace = output?.sidebar_workspace?.[0];
      if (workspace !== undefined)
        runtime.__h3M2529ContextEvents?.push({
          promptId: detail?.prompt_id,
          workspace,
        });
    });
    return {
      openCount: store.openWorkflows.length,
      activeKnown:
        store.activeWorkflow === null ||
        (typeof store.activeWorkflow === "object" &&
          !Array.isArray(store.activeWorkflow)),
    };
  });
  expect(workflowBefore.activeKnown).toBe(true);

  const container = await openH3AppModeTab(
    page,
    "h3-context-m23-32-terminal-waiting",
  );
  await beginSettledH3InteractionPhase(
    page,
    networkAttribution,
    candidateNetworkAttribution,
  );
  await watchHostExecution(page);
  await page.evaluate(installM2332ManagedExecutionProjection);
  const projectionBoundary = await page.evaluate(() => ({
    webdriver: navigator.webdriver === true,
    projector:
      typeof (
        window as unknown as {
          __h3M2508ManagedExecutionProjection?: unknown;
        }
      ).__h3M2508ManagedExecutionProjection === "function",
  }));
  // CRITICAL: fail before the queue click if the automation-only projection cannot be selected;
  // continuing would submit the visible native H3 envelope and could start model/GPU execution.
  expect(projectionBoundary).toEqual({ webdriver: true, projector: true });

  await container
    .locator('[data-h3-focus-key="app-intent"]')
    .fill("Verify a successful host terminal before its output event arrives.");
  await container
    .locator('[data-h3-focus-key="app-duration-seconds"]')
    .fill("8");
  await expect(
    container.getByText("Delivers 8 s (192 frames).", { exact: true }),
  ).toBeVisible();
  const submit = container.locator('[data-h3-focus-key="app-submit"]');
  await expect(submit).toBeEnabled({ timeout: 30_000 });
  const fromSubmission = (await sampleProgress(page)).submitted;
  await submit.click();
  await expect
    .poll(async () => (await sampleProgress(page, fromSubmission)).started, {
      timeout: 180_000,
    })
    .toBe(1);
  const result = await awaitHostExecution(page, fromSubmission, 180_000);
  expect(result.errors).toEqual([]);
  expect(reportedArtifactNames(result.outputs)).toEqual([]);
  try {
    await expect.poll(() => closeResponses.length, { timeout: 60_000 }).toBe(1);
  } catch (error) {
    console.info(
      "M23_32_TERMINAL_WAITING_DIAGNOSTIC " +
        JSON.stringify({
          routes: routeReceipts.map((receipt) => ({
            route: receipt.route,
            action: receipt.action,
            stage: receipt.stage,
          })),
          preparations: preparations.length,
          closeRequests: closeRequests.length,
          shellStatus: await container
            .locator("[data-shell-status]")
            .first()
            .getAttribute("data-shell-status"),
          phase: await container
            .locator("[data-app-mode-phase]")
            .first()
            .getAttribute("data-app-mode-phase")
            .catch(() => null),
        }),
    );
    throw error;
  }
  expect(closeResponses[0]).toEqual({
    artifact: "null",
    status: 200,
    disposition: "verification_pending",
    jobState: "running",
    // IMPORTANT: compact project-member coordinator responses intentionally omit the full
    // Production projection; only a later member-authorized read may expose aggregate run state.
    completed: -1,
    runState: "",
    artifactAuthority: false,
    geometry: null,
  });
  expect(closeRequests).toHaveLength(1);
  expect(closeRequests[0]?.artifact).toBe("null");
  await expect(
    container.locator('[data-app-mode-phase="verifying_output"]'),
  ).toHaveCount(1);
  await expect(container.locator('[data-shell-status="working"]')).toHaveCount(
    1,
  );
  await expect(
    container.locator('[data-shell-status="projected"]'),
  ).toHaveCount(0);

  const executionIdentity = await page.evaluate((offset) => {
    const runtime = window as unknown as {
      __h3Sample?: { promptIds?: string[] };
      __h3M2508ExecutionIdentityTrace?: Array<Record<string, unknown>>;
      __h3M2332QueueReceipt?: {
        projectorCalls: number;
        fullEnvelopeCalls: number;
        queueCalls: number;
        projectionStable: boolean;
        queueMatchesProjection: boolean;
        delegatedUnchanged: boolean;
        queuedAssetFree: boolean;
        queuedSourceCount: number;
        queuedLoaderCount: number;
        queuedNativeGeneratorCount: number;
        queuedSinkCount: number;
        queuedProductShellCount: number;
        originalSinkNodeId: string;
      };
    };
    const promptIds = (runtime.__h3Sample?.promptIds ?? []).slice(offset);
    return {
      promptIds,
      queue: runtime.__h3M2332QueueReceipt,
      trace: [...(runtime.__h3M2508ExecutionIdentityTrace ?? [])],
    };
  }, fromSubmission);
  expect(executionIdentity.promptIds).toHaveLength(1);
  expect(executionIdentity.queue).toEqual({
    projectorCalls: 2,
    fullEnvelopeCalls: 2,
    queueCalls: 1,
    projectionStable: true,
    queueMatchesProjection: true,
    delegatedUnchanged: true,
    queuedAssetFree: true,
    queuedSourceCount: 0,
    queuedLoaderCount: 0,
    queuedNativeGeneratorCount: 0,
    queuedSinkCount: 0,
    queuedProductShellCount: 1,
    originalSinkNodeId: expect.stringMatching(/^[A-Za-z0-9][A-Za-z0-9._:-]*$/),
  });
  expect(executionIdentity.trace).toHaveLength(3);
  expect(executionIdentity.trace.map((entry) => entry.stage)).toEqual([
    "final_projection",
    "preflight_projection",
    "queue_envelope",
  ]);
  expect(
    new Set(executionIdentity.trace.map((entry) => entry.fingerprint)).size,
  ).toBe(1);
  const promptId = executionIdentity.promptIds[0]!;
  expect(closeRequests[0]?.queuePromptId).toBe(promptId);
  const historyResponse = await page.request.get(
    new URL(`/history/${encodeURIComponent(promptId)}`, hostUrl).href,
  );
  expect(historyResponse.ok()).toBe(true);
  const history = (await historyResponse.json()) as Record<
    string,
    { prompt?: unknown[]; status?: { status_str?: unknown } }
  >;
  const accepted = history[promptId];
  const acceptedOutput = accepted?.prompt?.[2] as
    Record<string, { class_type?: unknown }> | undefined;
  const acceptedTypes = Object.values(acceptedOutput ?? {}).map((node) =>
    String(node.class_type ?? ""),
  );
  expect(accepted?.status?.status_str).toBe("success");
  expect(
    acceptedTypes.filter(
      (type) => type === "comfyui_h3_context.H3Context.ProductShell",
    ),
  ).toHaveLength(1);
  for (const absent of [
    "SaveVideo",
    "UNETLoader",
    "CLIPLoader",
    "LoraLoaderModelOnly",
    "MiniMaxH3ImageToVideo",
    "MiniMaxH3ReferenceToVideo",
    "LoadImage",
    "LoadVideo",
  ])
    expect(acceptedTypes).not.toContain(absent);

  const contextWorkspaceWire = await page.evaluate(
    (acceptedPromptId) =>
      (
        (
          window as unknown as {
            __h3M2529ContextEvents?: Array<{
              promptId: unknown;
              workspace: unknown;
            }>;
          }
        ).__h3M2529ContextEvents ?? []
      ).find((event) => event.promptId === acceptedPromptId)?.workspace ?? null,
    promptId,
  );
  const contextWorkspace =
    decodeSidebarWorkspaceProjection(contextWorkspaceWire);
  expect(contextWorkspace.correlation.prompt_id).toBe(promptId);

  expect(preparations).toHaveLength(1);
  const preparation = preparations[0]!;
  expect(preparation.expectedFrames).toBe(192);
  expect(preparation.sourceIdentity).toBeNull();
  expect(preparation.ownedProjectionFingerprint).toMatch(
    /^sha256:[0-9a-f]{64}$/,
  );
  const graphBeforeLateArtifact = await page.evaluateHandle(() =>
    structuredClone(
      (
        window as unknown as { comfyAPI: { app: { app: any } } }
      ).comfyAPI.app.app.graph.serialize(),
    ),
  );

  // The host already emitted the successful model-free terminal above. This event names only the
  // separately prepared tracked M25-48 fixture; it exercises the delivery/verification join and
  // is never evidence of model or provider generation.
  await page.evaluate(
    ({ acceptedPromptId, outputNodeId, locator }) => {
      const api = (
        window as unknown as { comfyAPI: { api: { api: EventTarget } } }
      ).comfyAPI.api.api;
      api.dispatchEvent(
        new CustomEvent("executed", {
          detail: {
            prompt_id: acceptedPromptId,
            node: outputNodeId,
            output: { images: [locator], animated: [true] },
          },
        }),
      );
    },
    {
      acceptedPromptId: promptId,
      outputNodeId: executionIdentity.queue!.originalSinkNodeId,
      locator: m2548ArtifactLocator!,
    },
  );
  await expect.poll(() => closeResponses.length, { timeout: 60_000 }).toBe(2);
  expect(closeRequests).toHaveLength(2);
  expect(closeRequests[1]).toEqual({
    artifact: "locator",
    queuePromptId: promptId,
    outputNodeId: executionIdentity.queue!.originalSinkNodeId,
  });
  expect(closeResponses[1]).toEqual({
    artifact: "locator",
    status: 200,
    disposition: "succeeded",
    jobState: "succeeded",
    completed: -1,
    runState: "",
    artifactAuthority: true,
    geometry: null,
  });
  await expect(
    container.locator('[data-shell-status="projected"]'),
  ).toHaveCount(1, { timeout: 60_000 });
  await container.getByRole("button", { name: "Production" }).click();
  await expect(
    container.getByText("1 of 1 segments", { exact: true }),
  ).toBeVisible();
  await expect(
    container.locator('[data-segment-index][data-state="succeeded"]'),
  ).toHaveCount(1);
  await expect.poll(() => accumulatedReadTargets.length).toBeGreaterThan(0);
  const accumulatedReadTarget = accumulatedReadTargets.at(-1);
  if (accumulatedReadTarget === undefined)
    throw new Error("the accumulated project read identity is unavailable");
  // IMPORTANT: read the exact server-owned v2 project after promotion. A passive response
  // callback can race the assertion and must not turn a verified output into missing evidence.
  const accumulatedRead = await postHostAction(
    page,
    "/h3-context/v1/production/action",
    {
      schema: "h3.context.production_workbench.action.v1",
      request_id: `ac11-accumulated-read-${promptId.replace(/[^A-Za-z0-9]/g, "-")}`,
      action: "read_accumulated_project",
      payload: {
        version: PRODUCTION_ACCUMULATION_ACTION_VERSION,
        workspace_handle: accumulatedReadTarget.workspaceHandle,
        workspace_id: accumulatedReadTarget.workspaceId,
      },
    },
  );
  expect(accumulatedRead.status).toBe(200);
  const accumulatedProject = decodeProductionAccumulatedProject(
    accumulatedRead.body,
  );
  expect(accumulatedProject.workspace).not.toBeNull();
  succeededProductionWire = structuredClone(
    (accumulatedRead.body as { workspace: unknown }).workspace,
  );
  await expect
    .poll(() => {
      try {
        return decodeProductionWorkbenchProjection(succeededProductionWire)
          .runState;
      } catch {
        return "";
      }
    })
    .toBe("succeeded");

  const graphAfterLateArtifact = await page.evaluateHandle(() =>
    (
      window as unknown as { comfyAPI: { app: { app: any } } }
    ).comfyAPI.app.app.graph.serialize(),
  );
  const surroundings = await page.evaluate(diffGraphSurroundings, {
    beforeValue: graphBeforeLateArtifact,
    afterValue: graphAfterLateArtifact,
    reference: {
      ownedNodeIds: preparation.ownedNodeIds,
      ownedLinkIds: preparation.ownedLinkIds,
      anchorNodeId: preparation.anchorNodeId,
      ownedProjectionEqual: false,
    },
  });
  await graphBeforeLateArtifact.dispose();
  await graphAfterLateArtifact.dispose();
  expect(surroundings.counts.owned).toBe(0);

  const productionBeforeImport = decodeProductionWorkbenchProjection(
    succeededProductionWire,
  );
  expect(productionBeforeImport.allowedActions).toContain(
    "import_production_outputs_to_authoring",
  );
  const selectedReadyOutputs = productionBeforeImport.outputs.filter(
    (output) =>
      output.state === "ready" &&
      output.segmentId !== null &&
      productionBeforeImport.selectedSegmentIds.includes(output.segmentId),
  );
  expect(selectedReadyOutputs).toHaveLength(1);
  const selectedOutput = selectedReadyOutputs[0]!;
  const requestSuffix = promptId.replace(/[^A-Za-z0-9]/g, "-");
  const graphBeforeImport = await page.evaluate(() =>
    structuredClone(
      (
        window as unknown as { comfyAPI: { app: { app: any } } }
      ).comfyAPI.app.app.graph.serialize(),
    ),
  );
  const promptRequestsBeforeImport = routeReceipts.filter(
    (receipt) => receipt.stage === "request" && receipt.route === "host_prompt",
  ).length;
  // AC11/M25-40 causal path: the shipped compact control prepares the project's exact
  // Authoring target, initializes it and imports exactly once. Every identity is read from the wire
  // the shell issued (captured responses), never recomputed by the test.
  const authoringWire: Array<{
    action: string;
    status: number;
    body: unknown;
  }> = [];
  const importWire: Array<{
    request: Record<string, unknown>;
    status: number;
    body: unknown;
  }> = [];
  const onResponse = async (response: Response) => {
    const pathname = new URL(response.url()).pathname;
    if (pathname.endsWith("/h3-context/v1/authoring/action")) {
      const request = response.request().postDataJSON() as { action?: string };
      authoringWire.push({
        action: String(request?.action ?? ""),
        status: response.status(),
        body: await response.json().catch(() => null),
      });
    } else if (
      pathname.endsWith("/h3-context/v1/production/authoring-import")
    ) {
      importWire.push({
        request: response.request().postDataJSON() as Record<string, unknown>,
        status: response.status(),
        body: await response.json().catch(() => null),
      });
    }
  };
  // M25-34 precondition: the media tools exactly as this host start left them, read before the
  // import so neither the shell nor this read has installed or selected anything yet.
  const mediaStatusBefore =
    mediaJourney === undefined ? null : await settledMediaRuntimeStatus(page);
  const allFeatures = (state: string) =>
    Object.fromEntries(MEDIA_FEATURES.map((feature) => [feature, state]));
  if (mediaStatusBefore !== null) {
    expect(mediaStatusBefore.status).toBe(200);
    expect(mediaStatusBefore.schema).toBe("h3.context.media_runtime_status.v3");
    expect(mediaStatusBefore.setupRunning).toBe(false);
  }
  if (mediaJourney === "ready") {
    expect(mediaStatusBefore).toMatchObject({
      resolutionState: "located",
      resolutionReason: "pair_admitted",
      sourceKind: expectedMediaSource,
      features: allFeatures("ready"),
    });
    expect(mediaStatusBefore!.actions).not.toContain("install_supported");
  } else if (mediaJourney === "install") {
    // Missing tools are installable after an exhaustive search and after one truncated by a long
    // PATH that still examined the managed directory (M25-34 B-M2534-01); the reason says which.
    expect(["supported_pair_missing", "discovery_limit"]).toContain(
      mediaStatusBefore!.resolutionReason,
    );
    expect(mediaStatusBefore).toMatchObject({
      resolutionState: "unavailable",
      sourceKind: "none",
      features: allFeatures("setup_required"),
      reasons: allFeatures(mediaStatusBefore!.resolutionReason),
      installDescribed: true,
    });
    expect(mediaStatusBefore!.actions).toContain("install_supported");
  }
  page.on("response", onResponse);
  // IMPORTANT: M25-48 re-homes Production import into the expanded editor's Media tab. The
  // accepted M25-46 launcher exists only for the active Clip editor function; leaving Production's
  // workbench selected blocks the real host row before its request.
  await container.locator('[data-h3-director-function="clip_editor"]').click();
  await page.locator('[data-h3-nle-entry="open"]').click();
  const overlay = page.locator('[data-h3-nle-surface="overlay_v1"]');
  await expect(overlay).toBeVisible({ timeout: 30_000 });
  await overlay.locator('[data-h3-nle-pane="assets"]').click();
  const importControl = overlay.locator(
    '[data-h3-nle-control="asset.import_production"]',
  );
  await expect(importControl).toBeEnabled();
  await importControl.click();
  await expect
    .poll(() => importWire.length, { timeout: 60_000 })
    .toBeGreaterThan(0);
  let mediaSetupEvidence: Readonly<Record<string, unknown>> | null = null;
  if (mediaJourney === "install") {
    // The missing tools refuse the import once; the card beside the refusal offers exactly one
    // Install and continue, and the refused import then continues once on the same workspace.
    await expect(
      overlay.locator('[data-h3-nle-status="import"]'),
    ).toHaveAttribute("data-code", "service_unavailable");
    expect(importWire.map((entry) => entry.status)).toEqual([503]);
    const card = overlay.locator(
      '[data-h3-media-tools-placement="contextual-sidebar"]',
    );
    await expect(card).toHaveAttribute(
      "data-h3-media-tools",
      "setup_required",
      {
        timeout: 60_000,
      },
    );
    const install = card.locator('[data-h3-media-tools-action="primary"]');
    await expect(install).toHaveCount(1);
    await expect(install).toHaveText("Install and continue");
    const visibleInputs = () =>
      card.evaluate(
        (element) =>
          Array.from(
            element.querySelectorAll("input, textarea, select"),
          ).filter((field) => field.checkVisibility()).length,
      );
    expect(await visibleInputs()).toBe(0);
    expect(setupPosts).toEqual([]);
    const installStarted = Date.now();
    await install.click();
    await expect(card).toHaveAttribute("data-h3-media-tools", "installing", {
      timeout: 30_000,
    });
    await expect
      .poll(() => importWire.length, { timeout: 30 * 60_000 })
      .toBe(2);
    const installSeconds = Math.round((Date.now() - installStarted) / 1_000);
    expect(setupPosts).toEqual(["install_supported"]);
    const [refused, continued] = importWire;
    expect(continued!.request.request_id).not.toBe(refused!.request.request_id);
    expect({
      ...continued!.request,
      request_id: refused!.request.request_id,
    }).toEqual(refused!.request);
    const statusAfterInstall = await settledMediaRuntimeStatus(page);
    expect(statusAfterInstall).toMatchObject({
      resolutionState: "located",
      sourceKind: "managed",
      features: allFeatures("ready"),
      setupRunning: false,
    });
    mediaSetupEvidence = Object.freeze({
      refusedImportStatus: refused!.status,
      refusalCode: "service_unavailable",
      cardStateBeforeClick: "setup_required",
      primaryActions: 1,
      visibleTextInputs: 0,
      confirmationClicks: 1,
      setupPosts: [...setupPosts],
      installSeconds,
      continuedImportRequests: 1,
      continuedRequestSameWorkspaceAndRevisions: true,
      statusAfterInstall: {
        resolutionState: statusAfterInstall.resolutionState,
        sourceKind: statusAfterInstall.sourceKind,
        features: statusAfterInstall.features,
      },
    });
    console.info(
      `M25_34_MEDIA_SETUP_EVIDENCE ${JSON.stringify(mediaSetupEvidence)}`,
    );
  }
  // IMPORTANT: a refused import cannot publish the follow-up history read. Assert the admitted
  // continuation first so a real host rejection remains visible instead of becoming a 60 s wait.
  const admittedImport = importWire[importWire.length - 1]!;
  expect(admittedImport.status, JSON.stringify(admittedImport.body)).toBe(200);
  await expect
    .poll(
      () =>
        authoringWire.filter(
          (entry) => entry.action === "read_timeline_history",
        ).length,
      { timeout: 60_000 },
    )
    .toBeGreaterThan(0);
  page.off("response", onResponse);
  // Target creation and history initialization are the shell's separate accepted actions;
  // the import is one request; the refresh read follows it. A refused import reads nothing, and
  // its continuation reuses the target and history it already created.
  expect(authoringWire.map((entry) => entry.action)).toEqual([
    "ensure_authoring_from_production",
    "initialize_timeline_history",
    "read_timeline_history",
  ]);
  expect(importWire).toHaveLength(mediaJourney === "install" ? 2 : 1);
  if (mediaJourney === "install") importWire.shift();
  const ensureEntry = authoringWire[0]!;
  expect(ensureEntry.status).toBe(201);
  const createdAuthoring = decodeAuthoringProjection(ensureEntry.body);
  const initializeEntry = authoringWire[1]!;
  expect(initializeEntry.status).toBe(200);
  const initializedHistory = decodeTimelineHistoryProjection(
    initializeEntry.body,
  );
  expect(initializedHistory.workspaceHandle).toBe(
    createdAuthoring.workspaceHandle,
  );
  expect(initializedHistory.rejection).toBeNull();
  const beforeHistory = initializedHistory;
  const importRequest = importWire[0]!.request;
  expect(importRequest.authoring_workspace_handle).toBe(
    createdAuthoring.workspaceHandle,
  );
  expect(importRequest.entries).toEqual([
    {
      segment_id: selectedOutput.segmentId,
      output_handle: selectedOutput.outputHandle,
    },
  ]);
  const firstImportResponse = {
    status: importWire[0]!.status,
    body: importWire[0]!.body,
  };
  let authoringReleased = false;
  let productionReleased = false;
  let cleanupEvidence: Readonly<{
    authoringReadAfterRelease: number;
    productionReadAfterRelease: number;
  }> | null = null;
  let importEvidence: Readonly<Record<string, unknown>> | null = null;
  let renderEvidence: Readonly<Record<string, unknown>> | null = null;
  let ux14Evidence: Readonly<Record<string, unknown>> | null = null;
  let ux14LeaseStart: number | null = null;
  let ux14PlaybackEvidence: Readonly<Record<string, unknown>> | null = null;
  try {
    if (firstImportResponse.status !== 200)
      console.info(
        "AC11_IMPORT_DIAGNOSTIC " +
          JSON.stringify({
            contextWorkspace: contextWorkspaceWire,
            production: succeededProductionWire,
            authoring: createdAuthoring,
            history: beforeHistory,
            request: importRequest,
            response: firstImportResponse,
          }),
      );
    expect(
      firstImportResponse.status,
      JSON.stringify(firstImportResponse.body),
    ).toBe(200);
    const firstImport = decodeProductionAuthoringImportResponseV1(
      firstImportResponse.body,
    );
    expect(firstImport.receipt.disposition).toBe("created");
    expect(firstImport.receipt.rows).toEqual([
      expect.objectContaining({
        segmentId: selectedOutput.segmentId,
        outputHandle: selectedOutput.outputHandle,
        sourceKind: "video",
        disposition: "created",
      }),
    ]);
    expect(firstImport.receipt.reference.nextRevision).toBe(
      firstImport.receipt.reference.priorRevision + 1,
    );
    expect(firstImport.receipt.legacyTimeline.nextRevision).toBe(
      firstImport.receipt.legacyTimeline.priorRevision,
    );
    expect(firstImport.receipt.nle.nextWorkspaceRevision).toBe(
      firstImport.receipt.nle.priorWorkspaceRevision + 1,
    );
    expect(firstImport.receipt.nle.nextTimelineRevision).toBe(
      firstImport.receipt.nle.priorTimelineRevision,
    );

    const exactReplayResponse = await postHostAction(
      page,
      "/h3-context/v1/production/authoring-import",
      importRequest,
    );
    expect(exactReplayResponse.status).toBe(200);
    expect(exactReplayResponse.body).toEqual(firstImportResponse.body);

    const afterFirstReadResponse = await postHostAction(
      page,
      "/h3-context/v1/authoring/action",
      encodeAuthoringAction(
        `ac11-read-after-first-${requestSuffix}`,
        "read_timeline_history",
        { workspace_handle: createdAuthoring.workspaceHandle },
      ),
    );
    expect(afterFirstReadResponse.status).toBe(200);
    const afterFirstHistory = decodeTimelineHistoryProjection(
      afterFirstReadResponse.body,
    );
    const importedAssetId = firstImport.receipt.rows[0]!.assetId;
    const importedOrdinal =
      afterFirstHistory.snapshot.assets
        .filter((asset) => asset.kind === "video" || asset.kind === "image")
        .findIndex((asset) => asset.assetId === importedAssetId) + 1;
    expect(importedOrdinal).toBeGreaterThan(0);
    expect(afterFirstHistory.snapshot.assets).toHaveLength(
      beforeHistory.snapshot.assets.length + 1,
    );
    expect(
      afterFirstHistory.snapshot.assets.some(
        (asset) => asset.assetId === importedAssetId && asset.kind === "video",
      ),
    ).toBe(true);
    expect(afterFirstHistory.snapshot.workspaceRevision).toBe(
      beforeHistory.snapshot.workspaceRevision + 1,
    );
    expect(afterFirstHistory.snapshot.workspaceFingerprint).not.toBe(
      beforeHistory.snapshot.workspaceFingerprint,
    );
    expect(afterFirstHistory.snapshot.publicFingerprint).not.toBe(
      beforeHistory.snapshot.publicFingerprint,
    );
    expect(afterFirstHistory.snapshot.timelineRevision).toBe(
      beforeHistory.snapshot.timelineRevision,
    );
    expect(afterFirstHistory.snapshot.timelineFingerprint).toBe(
      beforeHistory.snapshot.timelineFingerprint,
    );
    expect(afterFirstHistory.snapshot.tracks).toEqual(
      beforeHistory.snapshot.tracks,
    );
    expect(afterFirstHistory.snapshot.clips).toEqual(
      beforeHistory.snapshot.clips,
    );
    expect(
      afterFirstHistory.snapshot.clips.some(
        (clip) => clip.assetId === importedAssetId,
      ),
    ).toBe(false);
    expect(afterFirstHistory.selection).toEqual(beforeHistory.selection);
    expect(afterFirstHistory.undoCursor).toBe(beforeHistory.undoCursor);
    expect(afterFirstHistory.redoCursor).toBe(beforeHistory.redoCursor);
    expect(firstImport.authoringProjection.timeline).toEqual(
      createdAuthoring.timeline,
    );

    const alreadyImportedRequest = encodeProductionAuthoringImportRequest({
      requestId: `ac11-import-again-${requestSuffix}`,
      productionWorkspaceHandle: productionBeforeImport.workspaceHandle,
      productionWorkspaceId: productionBeforeImport.workspaceId,
      expectedProductionWorkspaceRevision:
        productionBeforeImport.workspaceRevision,
      expectedProductionWorkspaceFingerprint:
        productionBeforeImport.workspaceFingerprint,
      authoringWorkspaceHandle: firstImport.authoringProjection.workspaceHandle,
      expectedAuthoringRegistryFingerprint:
        firstImport.authoringProjection.registryFingerprint,
      expectedAuthoringReferenceRevision:
        firstImport.authoringProjection.reference.revision,
      expectedAuthoringTimelineRevision:
        firstImport.authoringProjection.timeline.revision,
      expectedAuthoringTimelineContentFingerprint:
        firstImport.authoringProjection.timeline.contentFingerprint,
      expectedNleWorkspaceRevision:
        afterFirstHistory.snapshot.workspaceRevision,
      expectedNleTimelineRevision: afterFirstHistory.snapshot.timelineRevision,
      expectedNleTimelineFingerprint:
        afterFirstHistory.snapshot.timelineFingerprint,
      expectedNlePublicFingerprint:
        afterFirstHistory.snapshot.publicFingerprint,
      entries: [
        {
          segmentId: selectedOutput.segmentId!,
          outputHandle: selectedOutput.outputHandle,
        },
      ],
    });
    const alreadyImportedResponse = await postHostAction(
      page,
      "/h3-context/v1/production/authoring-import",
      alreadyImportedRequest,
    );
    expect(alreadyImportedResponse.status).toBe(200);
    const alreadyImported = decodeProductionAuthoringImportResponseV1(
      alreadyImportedResponse.body,
    );
    expect(alreadyImported.receipt.disposition).toBe("already_imported");
    expect(alreadyImported.receipt.rows).toEqual([
      expect.objectContaining({
        assetId: importedAssetId,
        disposition: "already_imported",
      }),
    ]);
    expect(alreadyImported.authoringProjection).toEqual(
      firstImport.authoringProjection,
    );

    const afterReplayReadResponse = await postHostAction(
      page,
      "/h3-context/v1/authoring/action",
      encodeAuthoringAction(
        `ac11-read-after-replay-${requestSuffix}`,
        "read_timeline_history",
        { workspace_handle: createdAuthoring.workspaceHandle },
      ),
    );
    expect(afterReplayReadResponse.status).toBe(200);
    const afterReplayHistory = decodeTimelineHistoryProjection(
      afterReplayReadResponse.body,
    );
    expect(afterReplayHistory).toEqual(afterFirstHistory);

    const productionReadResponse = await postHostAction(
      page,
      "/h3-context/v1/production/action",
      encodeProductionAction(
        `ac11-production-read-${requestSuffix}`,
        "read_projection",
        { workspaceHandle: productionBeforeImport.workspaceHandle },
      ),
    );
    expect(productionReadResponse.status).toBe(200);
    const productionAfterImport = decodeProductionWorkbenchProjection(
      productionReadResponse.body,
    );
    expect(productionAfterImport).toEqual(productionBeforeImport);
    expect(productionAfterImport.assembly).toEqual(
      productionBeforeImport.assembly,
    );

    // M25-48 editor side: the shell moved focus to the Clip editor; open the NLE overlay through
    // the Sidebar's own launcher and prove the zero-clip import obtains its card through the exact
    // asset-thumbnail lease contract. Only then insert it as one separate timeline transaction.
    // The import was issued from this already-open Media surface.
    await expect(overlay).toBeVisible({ timeout: 30_000 });
    const importedCard = overlay.locator(
      `[data-h3-nle-card-index="${importedOrdinal}"]`,
    );
    await expect(importedCard).toBeVisible();
    // IMPORTANT: media cards have no tone contract. Readiness is established by the exact
    // thumbnail lease lifecycle and decoded 64 x 64 canvas below; a presentation-only tone
    // selector made the supplied-host row impossible before either contract could be observed.
    const importedThumbnail = importedCard.locator(
      '[data-h3-nle-thumbnail=""]',
    );
    await expect(importedThumbnail).toHaveCount(1);
    await expect
      .poll(() => mediaLeaseControls.length, { timeout: 60_000 })
      .toBeGreaterThan(0);
    const createResponse = mediaLeaseResponses[0]!;
    const createReceipt = (await createResponse.json()) as Record<
      string,
      unknown
    >;
    const createDiagnostic = JSON.stringify({
      status: createResponse.status(),
      reason: createReceipt.reason ?? null,
      schema: createReceipt.schema ?? null,
    });
    // IMPORTANT: media-lease controls return receipt-bearing 200 responses for create/release;
    // borrowing workspace-create 201 or empty-release 204 semantics rejects the real host route.
    expect(createResponse.status(), createDiagnostic).toBe(200);
    await expect
      .poll(() => mediaLeaseControls.map(({ operation }) => operation), {
        // The real derivative route owns a 43 s bounded generation window; keep the browser
        // admission above that bound and require the complete release before presentation.
        timeout: 60_000,
        message: createDiagnostic,
      })
      .toEqual(["create", "open", "release"]);
    expect(mediaLeaseControls).toEqual([
      {
        operation: "create",
        scope: "asset",
        clipId: null,
        derivativeKind: "thumbnail",
        status: 200,
      },
      {
        operation: "open",
        scope: undefined,
        clipId: undefined,
        derivativeKind: undefined,
        status: 200,
      },
      {
        operation: "release",
        scope: undefined,
        clipId: undefined,
        derivativeKind: undefined,
        status: 200,
      },
    ]);
    await expect
      .poll(() =>
        importedThumbnail.evaluate((canvas) => ({
          width: (canvas as HTMLCanvasElement).width,
          height: (canvas as HTMLCanvasElement).height,
        })),
      )
      .toEqual({ width: 64, height: 64 });
    // M25-34 U08 on the host: before insertion the monitor says the timeline has no clip; it
    // never reports the media tools unavailable.
    const emptyTimelineNote = overlay.locator('[data-h3-nle-empty="timeline"]');
    if (mediaJourney !== undefined)
      await expect(emptyTimelineNote).toBeVisible();
    const insertResponse = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname.endsWith(
          "/h3-context/v1/authoring/action",
        ) &&
        (response.request().postDataJSON() as { action?: string })?.action ===
          "apply_timeline_transaction",
      { timeout: 60_000 },
    );
    if (ux14Journey === "1") ux14LeaseStart = mediaLeaseControls.length;
    await importedCard.locator('[data-h3-nle-control="asset.insert"]').click();
    const insertion = await insertResponse;
    expect(insertion.status()).toBe(200);
    const insertedCommands = (
      insertion.request().postDataJSON() as {
        payload: { commands: { kind: string }[] };
      }
    ).payload.commands.map((command) => command.kind);
    expect(insertedCommands).toEqual(["insert_asset_clip"]);
    const afterInsertReadResponse = await postHostAction(
      page,
      "/h3-context/v1/authoring/action",
      encodeAuthoringAction(
        `ac11-read-after-insert-${requestSuffix}`,
        "read_timeline_history",
        { workspace_handle: createdAuthoring.workspaceHandle },
      ),
    );
    expect(afterInsertReadResponse.status).toBe(200);
    const afterInsertHistory = decodeTimelineHistoryProjection(
      afterInsertReadResponse.body,
    );
    expect(afterInsertHistory.snapshot.timelineRevision).toBe(
      afterFirstHistory.snapshot.timelineRevision + 1,
    );
    expect(
      afterInsertHistory.snapshot.clips.filter(
        (clip) => clip.assetId === importedAssetId,
      ),
    ).toHaveLength(1);
    expect(afterInsertHistory.snapshot.assets).toEqual(
      afterFirstHistory.snapshot.assets,
    );
    if (mediaJourney !== undefined)
      await expect(emptyTimelineNote).toHaveCount(0);
    if (renderJourney) {
      // M25-34 U03: the tools installed a moment ago decode the clip in the monitor and render
      // the final video, which the browser then decodes. A job status alone proves neither.
      const insertedClip = afterInsertHistory.snapshot.clips.find(
        (clip) => clip.assetId === importedAssetId,
      )!;
      const monitorCanvas = overlay.locator(".h3-nle-monitor canvas");
      await expect(monitorCanvas).toHaveCount(1, { timeout: 60_000 });
      const pixels = () =>
        monitorCanvas.evaluate((canvas) => {
          const target = canvas as HTMLCanvasElement;
          const data = target
            .getContext("2d")!
            .getImageData(0, 0, target.width, target.height).data;
          let colors = 0;
          for (let index = 0; index < data.length; index += 64)
            if (data[index]! + data[index + 1]! + data[index + 2]! > 40)
              colors++;
          return colors;
        });
      // The fixed host fixture is 64 x 64 and is sampled once per 16 pixels. Sixteen lit samples
      // prove a decoded picture while remaining below the measured 30-sample sparse frame.
      const minimumDecodedPixelSamples = 16;
      const seekFrame = insertedClip.startFrame + 12;
      const seek = overlay.locator('[data-h3-nle-control="transport.seek"]');
      // IMPORTANT: transport.seek is the shipped ARIA slider, not a form input. Wait until the
      // monitor owns a playable lease, then drive the same pointer scrub contract as a user.
      await expect(seek).toHaveAttribute("aria-disabled", "false", {
        timeout: 60_000,
      });
      await seekPlayhead(page, seek, seekFrame);
      await expect(seek).toHaveAttribute("aria-valuenow", String(seekFrame));
      await expect(seek).toHaveAttribute(
        "aria-valuetext",
        /^\d{2,}:\d{2}:\d{2}:\d{2}$/,
      );
      await expect
        .poll(pixels, { timeout: 60_000 })
        .toBeGreaterThanOrEqual(minimumDecodedPixelSamples);
      const monitorColors = await pixels();
      if (ux14Journey === "1") {
        const monitorStatus = overlay.locator('[data-h3-nle-status="monitor"]');
        const recover = overlay.locator(
          '[data-h3-nle-control="transport.recover"]',
        );
        await expect(monitorStatus).not.toContainText("unavailable");
        await expect(recover).toHaveCount(0);
        const beforePlayWire = await seek.getAttribute("aria-valuenow");
        expect(beforePlayWire).toMatch(/^(?:0|[1-9]\d{0,7})$/);
        const beforePlay = Number(beforePlayWire);
        await overlay.locator('[data-h3-nle-control="transport.play"]').click();
        await expect
          .poll(async () => Number(await seek.getAttribute("aria-valuenow")), {
            timeout: 60_000,
          })
          .toBeGreaterThan(beforePlay);
        const advancedToWire = await seek.getAttribute("aria-valuenow");
        expect(advancedToWire).toMatch(/^(?:0|[1-9]\d{0,7})$/);
        const advancedTo = Number(advancedToWire);
        await expect
          .poll(pixels, { timeout: 60_000 })
          .toBeGreaterThanOrEqual(minimumDecodedPixelSamples);
        await overlay
          .locator('[data-h3-nle-control="transport.pause"]')
          .click();
        let pausedAt = -1;
        await expect
          .poll(async () => {
            const before = Number(await seek.getAttribute("aria-valuenow"));
            await page.waitForTimeout(250);
            pausedAt = Number(await seek.getAttribute("aria-valuenow"));
            return pausedAt === before;
          })
          .toBe(true);
        await expect(monitorStatus).toHaveText("Monitor paused.");
        await expect(recover).toHaveCount(0);
        const audioState = await overlay
          .locator('[data-h3-nle-status="audio"]')
          .getAttribute("data-h3-nle-audio-state");
        expect(["silent", "following", "seeking", "suspended"]).toContain(
          audioState,
        );
        ux14PlaybackEvidence = Object.freeze({
          monitorStatus: await monitorStatus.textContent(),
          recoverOffered: false,
          beforePlay,
          advancedTo,
          pausedAt,
          audioState,
          decodedPixelSamplesAfterPause: await pixels(),
        });
      }
      const renderStatuses: OutputStatus[] = [];
      const onRender = (response: Response) => {
        if (
          !new URL(response.url()).pathname.includes(
            "/h3-context/v1/authoring/render",
          ) ||
          response.status() !== 200
        )
          return;
        void response
          .json()
          .then((wire: unknown) =>
            renderStatuses.push(decodeOutputStatus(wire)),
          )
          .catch(() => undefined);
      };
      page.on("response", onRender);
      // M25-44: the final-video card lives in the chrome bar's Export popover.
      await openExportPanel(overlay);
      const output = overlay.getByRole("region", {
        name: "Final video",
        exact: true,
      });
      await output
        .getByRole("button", { name: "Render final video", exact: true })
        .click();
      await expect(output.getByRole("status")).toHaveText("Video ready", {
        timeout: 10 * 60_000,
      });
      await expect
        .poll(
          () =>
            renderStatuses.filter((status) => status.phase === "succeeded")
              .length,
        )
        .toBeGreaterThan(0);
      page.off("response", onRender);
      const rendered = renderStatuses.find(
        (status) => status.phase === "succeeded",
      )!.output!;
      await output
        .getByRole("button", { name: "Preview output", exact: true })
        .click();
      const preview = output.getByLabel("Final video preview", { exact: true });
      await expect
        .poll(
          () =>
            preview.evaluate(
              (element) => (element as HTMLVideoElement).readyState,
            ),
          { timeout: 90_000 },
        )
        .toBeGreaterThanOrEqual(2);
      const decoded = await preview.evaluate((element) => {
        const video = element as HTMLVideoElement;
        return {
          width: video.videoWidth,
          height: video.videoHeight,
          duration: video.duration,
        };
      });
      // The output preview is a bounded proxy (at most 640x360, aspect kept); the original is
      // checked below by its own bytes and an independent probe.
      expect(decoded.width).toBeGreaterThan(0);
      expect(decoded.width).toBeLessThanOrEqual(640);
      expect(decoded.height).toBeLessThanOrEqual(360);
      expect(decoded.width / decoded.height).toBeCloseTo(
        rendered.width / rendered.height,
        1,
      );
      expect(decoded.duration).toBeCloseTo(rendered.frame_count / 24, 1);
      await output
        .getByRole("button", { name: "Close preview", exact: true })
        .click();
      const downloaded = page.waitForEvent("download");
      await output
        .getByRole("link", { name: "Download original", exact: true })
        .click();
      const download = await downloaded;
      expect(await download.failure()).toBeNull();
      const originalPath = await download.path();
      const originalBytes = await readFile(originalPath);
      expect(originalBytes.byteLength).toBe(rendered.byte_length);
      expect(
        "sha256:" + createHash("sha256").update(originalBytes).digest("hex"),
      ).toBe(rendered.output_fingerprint);
      // An observer outside the product (the pinned ffprobe named by the runner) reads the file.
      const observer = process.env.H3_CONTEXT_M25_34_OBSERVER_FFPROBE;
      if (observer === undefined)
        throw new Error("the install journey names its observer ffprobe");
      const probed = JSON.parse(
        execFileSync(
          observer,
          [
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_type,width,height,nb_read_frames,r_frame_rate",
            "-of",
            "json",
            originalPath,
          ],
          { encoding: "utf8", timeout: 120_000 },
        ),
      ) as { streams: Array<Record<string, unknown>> };
      const stream = probed.streams[0]!;
      expect({
        codecType: stream.codec_type,
        width: Number(stream.width),
        height: Number(stream.height),
        frames: Number(stream.nb_read_frames),
        rate: stream.r_frame_rate,
      }).toEqual({
        codecType: "video",
        width: rendered.width,
        height: rendered.height,
        frames: rendered.frame_count,
        rate: "24/1",
      });
      // Content, judged by the observer ffmpeg against the imported source file itself: inside
      // the clip the rendered picture's centre carries the source's luma; after the clip ends it
      // does not. The centre crop lies inside the clip for any fit of the square source.
      const observerFfmpeg = process.env.H3_CONTEXT_M25_34_OBSERVER_FFMPEG;
      const sourceMedia = process.env.H3_CONTEXT_M25_34_SOURCE_MEDIA;
      if (observerFfmpeg === undefined || sourceMedia === undefined)
        throw new Error(
          "the render check names its observer ffmpeg and source",
        );
      const sourceProbe = JSON.parse(
        execFileSync(
          observer,
          [
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "json",
            sourceMedia,
          ],
          { encoding: "utf8", timeout: 120_000 },
        ),
      ) as { streams: Array<Record<string, unknown>> };
      const sourceStream = sourceProbe.streams[0]!;
      const lumaCropSide = Math.min(
        256,
        Number(sourceStream.width),
        Number(sourceStream.height),
        rendered.width,
        rendered.height,
      );
      expect(lumaCropSide).toBeGreaterThan(0);
      const meanLuma = (file: string, frame: number, side: number) => {
        const printed = execFileSync(
          observerFfmpeg,
          [
            "-v",
            "error",
            "-i",
            file,
            "-vf",
            `select=eq(n\\,${frame}),crop=${side}:${side}:(iw-${side})/2:(ih-${side})/2,` +
              "signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-",
            "-frames:v",
            "1",
            "-f",
            "null",
            "-",
          ],
          { encoding: "utf8", timeout: 300_000 },
        );
        const match = /YAVG=([0-9.]+)/.exec(printed);
        if (match === null) throw new Error("the observer printed no luma");
        return Number(match[1]);
      };
      const insideFrame = insertedClip.startFrame + 12;
      const afterFrame =
        insertedClip.startFrame + insertedClip.durationFrames + 24;
      expect(afterFrame).toBeLessThan(rendered.frame_count);
      const sourceLuma = meanLuma(
        sourceMedia,
        insertedClip.sourceStartFrame + 12,
        lumaCropSide,
      );
      const insideLuma = meanLuma(originalPath, insideFrame, lumaCropSide);
      const afterLuma = meanLuma(originalPath, afterFrame, lumaCropSide);
      expect(Math.abs(insideLuma - sourceLuma)).toBeLessThanOrEqual(4);
      expect(Math.abs(afterLuma - sourceLuma)).toBeGreaterThan(20);
      await download.delete();
      renderEvidence = Object.freeze({
        monitorSeekFrame: seekFrame,
        monitorDecodedPixelSamples: monitorColors,
        renderPhase: "succeeded",
        output: {
          width: rendered.width,
          height: rendered.height,
          frameCount: rendered.frame_count,
          byteLength: rendered.byte_length,
        },
        previewDecoded: decoded,
        originalBytesAndFingerprintMatch: true,
        independentProbe: {
          width: Number(stream.width),
          height: Number(stream.height),
          frames: Number(stream.nb_read_frames),
          rate: stream.r_frame_rate,
        },
        contentLuma: {
          insideFrame,
          afterFrame,
          source: sourceLuma,
          inside: insideLuma,
          after: afterLuma,
        },
      });
      console.info(
        `M25_34_MEDIA_RENDER_EVIDENCE ${JSON.stringify(renderEvidence)}`,
      );
    }
    // IMPORTANT: the following reopen cycles measure the reference shell, not Media throughput.
    // Leave Media explicitly so those cycles cannot start unobserved decoration work, and prove
    // the presented bitmap is synchronously purged at the tab boundary before closing the shell.
    await overlay.locator('[data-h3-nle-pane="sequence"]').click();
    await expect(importedThumbnail).toHaveCount(0);
    const ux14CloseLeaseBoundary = mediaLeaseControls.length;
    const ux14CloseLeaseRequestBoundary = mediaLeaseRequestSequence;
    await overlay.locator('[data-h3-nle-action="close"]').click();
    await expect(overlay).toHaveCount(0);
    if (ux14Journey === "1") {
      expect(ux14LeaseStart).not.toBeNull();
      expect(ux14PlaybackEvidence).not.toBeNull();
      const beforeClose = mediaLeaseControls.slice(
        ux14LeaseStart!,
        ux14CloseLeaseBoundary,
      );
      const monitorCreateOffset = beforeClose.findIndex(
        ({ operation, scope, clipId, derivativeKind, status }) =>
          operation === "create" &&
          scope === "clip" &&
          typeof clipId === "string" &&
          clipId.length > 0 &&
          derivativeKind === "video_proxy" &&
          status === 200,
      );
      expect(monitorCreateOffset).toBeGreaterThanOrEqual(0);
      const monitorCreateIndex = ux14LeaseStart! + monitorCreateOffset;
      const monitorCreate = mediaLeaseControls[monitorCreateIndex]!;
      const monitorCreateCorrelation =
        mediaLeaseCorrelations[monitorCreateIndex]!;
      const monitorCreateReceipt = (await mediaLeaseResponses[
        monitorCreateIndex
      ]!.json()) as Record<string, unknown>;
      const monitorLeaseId = monitorCreateReceipt.leaseId;
      expect(monitorLeaseId).toMatch(/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/);
      expect(monitorCreateCorrelation.ownerId).toBe(monitorCreate.clipId);
      const correlatedBeforeClose = mediaLeaseCorrelations
        .slice(ux14LeaseStart!, ux14CloseLeaseBoundary)
        .map((correlation, offset) => ({
          correlation,
          control: mediaLeaseControls[ux14LeaseStart! + offset]!,
          index: ux14LeaseStart! + offset,
        }));
      const monitorOpen = correlatedBeforeClose.find(
        ({ correlation, control, index }) =>
          index > monitorCreateIndex &&
          correlation.operation === "open" &&
          correlation.leaseId === monitorLeaseId &&
          correlation.ownerId === monitorCreate.clipId &&
          classifyM2556LeaseRequestPhase(
            correlation.requestOrdinal,
            ux14CloseLeaseRequestBoundary,
          ) === "before_close" &&
          control.status === 200,
      );
      expect(monitorOpen).toBeDefined();
      await expect
        .poll(
          () =>
            mediaLeaseCorrelations
              .slice(ux14CloseLeaseBoundary)
              .some(
                (correlation, offset) =>
                  correlation.operation === "release" &&
                  correlation.leaseId === monitorLeaseId &&
                  correlation.ownerId === monitorCreate.clipId &&
                  classifyM2556LeaseRequestPhase(
                    correlation.requestOrdinal,
                    ux14CloseLeaseRequestBoundary,
                  ) === "after_close" &&
                  mediaLeaseControls[ux14CloseLeaseBoundary + offset]
                    ?.status === 200,
              ),
          { timeout: 60_000 },
        )
        .toBe(true);
      const monitorReleaseOffset = mediaLeaseCorrelations
        .slice(ux14CloseLeaseBoundary)
        .findIndex(
          (correlation, offset) =>
            correlation.operation === "release" &&
            correlation.leaseId === monitorLeaseId &&
            correlation.ownerId === monitorCreate.clipId &&
            classifyM2556LeaseRequestPhase(
              correlation.requestOrdinal,
              ux14CloseLeaseRequestBoundary,
            ) === "after_close" &&
            mediaLeaseControls[ux14CloseLeaseBoundary + offset]?.status === 200,
        );
      expect(monitorReleaseOffset).toBeGreaterThanOrEqual(0);
      const sourceLease = [
        {
          operation: "create",
          phase: "before_close",
          leaseAlias: "monitor-playback-1",
          scope: monitorCreate.scope,
          clipIdPresent: true,
          derivativeKind: monitorCreate.derivativeKind,
          ownerMatchesClip: true,
          status: monitorCreate.status,
        },
        {
          operation: "open",
          phase: "before_close",
          leaseAlias: "monitor-playback-1",
          scope: null,
          clipIdPresent: false,
          derivativeKind: null,
          ownerMatchesClip: true,
          status: monitorOpen!.control.status,
        },
        {
          operation: "release",
          phase: "after_close",
          leaseAlias: "monitor-playback-1",
          scope: null,
          clipIdPresent: false,
          derivativeKind: null,
          ownerMatchesClip: true,
          status:
            mediaLeaseControls[ux14CloseLeaseBoundary + monitorReleaseOffset]!
              .status,
        },
      ] as const;
      expect(sourceLease.map(({ operation }) => operation)).toEqual([
        "create",
        "open",
        "release",
      ]);
      expect(
        sourceLease.every(
          ({ leaseAlias, ownerMatchesClip, status }) =>
            leaseAlias === "monitor-playback-1" &&
            ownerMatchesClip &&
            status === 200,
        ),
      ).toBe(true);
      ux14Evidence = Object.freeze({
        schema: "h3.context.m25_56.ux14_host_evidence.v1",
        candidate: {
          bundleSha256: candidateBundle!.sha256,
          installedInventorySha256: installedCandidateInventory,
          backendInventorySha256: candidateBackendRuntime!.inventorySha256,
        },
        disposition: "not_reproduced",
        originalState: {
          monitor: "source_unavailable",
          audio: "suspended",
          recoverOffered: true,
        },
        currentState: ux14PlaybackEvidence,
        input: {
          kind: "production_output",
          selectedOutputs: selectedReadyOutputs.length,
          importedAssetIdPresent: importedAssetId.length > 0,
        },
        sourceLease,
        closed: true,
      });
      await testInfo.attach("m25-56-ux14-host", {
        contentType: "application/json",
        body: Buffer.from(JSON.stringify(ux14Evidence), "utf8"),
      });
      console.info(`M25_56_UX14_HOST_EVIDENCE ${JSON.stringify(ux14Evidence)}`);
    }

    const graphAfterImport = await page.evaluate(() =>
      (
        window as unknown as { comfyAPI: { app: { app: any } } }
      ).comfyAPI.app.app.graph.serialize(),
    );
    expect(graphAfterImport).toEqual(graphBeforeImport);
    expect(await supportedHostQueueCounts(page)).toEqual({
      running: 0,
      pending: 0,
    });
    expect(
      routeReceipts.filter(
        (receipt) =>
          receipt.stage === "request" && receipt.route === "host_prompt",
      ).length,
    ).toBe(promptRequestsBeforeImport);
    expect(networkAttribution.snapshot().interactionProviderCount).toBe(0);

    const importResponseJson = JSON.stringify(firstImportResponse.body);
    expect(importResponseJson).not.toMatch(/[A-Za-z]:[\\/]/);
    expect(importResponseJson).not.toMatch(/(?:Users|home)[\\/]/i);
    expect(importResponseJson.toLowerCase()).not.toMatch(
      /"(?:path|url|filename)"/,
    );
    importEvidence = Object.freeze({
      productionSelectionCount: selectedReadyOutputs.length,
      createdDisposition: firstImport.receipt.disposition,
      exactReplayStable: true,
      secondRequestDisposition: alreadyImported.receipt.disposition,
      referenceRevisionDelta:
        firstImport.receipt.reference.nextRevision -
        firstImport.receipt.reference.priorRevision,
      legacyTimelineRevisionDelta:
        firstImport.receipt.legacyTimeline.nextRevision -
        firstImport.receipt.legacyTimeline.priorRevision,
      nleWorkspaceRevisionDelta:
        firstImport.receipt.nle.nextWorkspaceRevision -
        firstImport.receipt.nle.priorWorkspaceRevision,
      nleTimelineRevisionDelta:
        firstImport.receipt.nle.nextTimelineRevision -
        firstImport.receipt.nle.priorTimelineRevision,
      tracksStable: true,
      clipsStable: true,
      selectionStable: true,
      historyStable: true,
      productionStable: true,
      assemblyStable: true,
      causalImportControl: "pointer",
      highlightedInAssetBin: true,
      assetThumbnailLease: {
        scope: "asset",
        clipId: null,
        derivativeKind: "thumbnail",
        operations: mediaLeaseControls.map(({ operation }) => operation),
        releasedBeforePresentation: true,
      },
      explicitInsertionTimelineRevisionDelta: 1,
      graphStable: true,
      promptDelta: 0,
      queueAfter: { running: 0, pending: 0 },
      providerCount: 0,
    });
  } finally {
    const releaseAuthoringResponse = await postHostAction(
      page,
      "/h3-context/v1/authoring/action",
      encodeAuthoringAction(
        `ac11-authoring-release-${requestSuffix}`,
        "release_workspace",
        { workspace_handle: createdAuthoring.workspaceHandle },
      ),
    );
    expect(releaseAuthoringResponse.status).toBe(204);
    authoringReleased = true;
    const authoringReadAfterRelease = await postHostAction(
      page,
      "/h3-context/v1/authoring/action",
      encodeAuthoringAction(
        `ac11-authoring-read-released-${requestSuffix}`,
        "read_timeline_history",
        { workspace_handle: createdAuthoring.workspaceHandle },
      ),
    );

    const currentProductionResponse = await postHostAction(
      page,
      "/h3-context/v1/production/action",
      encodeProductionAction(
        `ac11-production-before-release-${requestSuffix}`,
        "read_projection",
        { workspaceHandle: productionBeforeImport.workspaceHandle },
      ),
    );
    expect(currentProductionResponse.status).toBe(200);
    const currentProduction = decodeProductionWorkbenchProjection(
      currentProductionResponse.body,
    );
    const releaseProductionResponse = await postHostAction(
      page,
      "/h3-context/v1/production/action",
      encodeProductionAction(
        `ac11-production-release-${requestSuffix}`,
        "release_workspace",
        { projection: currentProduction },
      ),
    );
    expect(releaseProductionResponse.status).toBe(204);
    productionReleased = true;
    const productionReadAfterRelease = await postHostAction(
      page,
      "/h3-context/v1/production/action",
      encodeProductionAction(
        `ac11-production-read-released-${requestSuffix}`,
        "read_projection",
        { workspaceHandle: productionBeforeImport.workspaceHandle },
      ),
    );
    cleanupEvidence = Object.freeze({
      authoringReadAfterRelease: authoringReadAfterRelease.status,
      productionReadAfterRelease: productionReadAfterRelease.status,
    });
  }
  expect(authoringReleased).toBe(true);
  expect(productionReleased).toBe(true);
  expect(cleanupEvidence).toEqual({
    authoringReadAfterRelease: 410,
    productionReadAfterRelease: 410,
  });
  expect(importEvidence).not.toBeNull();
  if (mediaJourney === "ready") expect(setupPosts).toEqual([]);
  if (mediaJourney === "install")
    expect(setupPosts).toEqual(["install_supported"]);
  if (renderJourney) expect(renderEvidence).not.toBeNull();
  if (ux14Journey === "1") expect(ux14Evidence).not.toBeNull();

  const workflowAfter = await page.evaluate(() => {
    const runtime = window as unknown as {
      comfyAPI: { app: { app: any } };
      __h3M2332WorkflowAuthority?: object | null;
    };
    const store = runtime.comfyAPI.app.app.extensionManager?.workflow;
    return {
      openCount: Array.isArray(store?.openWorkflows)
        ? store.openWorkflows.length
        : -1,
      activeStable:
        store?.activeWorkflow === runtime.__h3M2332WorkflowAuthority,
    };
  });
  expect(workflowAfter).toEqual({
    openCount: workflowBefore.openCount,
    activeStable: true,
  });
  expect(context.pages()).toHaveLength(browserTabCount);

  const requestActions = routeReceipts
    .filter((receipt) => receipt.stage === "request")
    .map((receipt) => `${receipt.route}:${receipt.action}`)
    .filter((value) =>
      new Set([
        "sequence_coordinator:prepare_managed_run",
        "host_prompt:queue_prompt",
        "sequence_coordinator:submit_managed_run",
        "sequence_coordinator:close_managed_run",
      ]).has(value),
    );
  expect(requestActions).toEqual([
    "host_prompt:queue_prompt",
    "sequence_coordinator:prepare_managed_run",
    "sequence_coordinator:submit_managed_run",
    "sequence_coordinator:close_managed_run",
    "sequence_coordinator:close_managed_run",
  ]);
  expect(await supportedHostQueueCounts(page)).toEqual({
    running: 0,
    pending: 0,
  });
  expectCandidateInteractionNetworkLocal(candidateNetworkAttribution);
  expect(networkAttribution.snapshot().interactionProviderCount).toBe(0);

  const evidence = {
    schema: "h3.context.m25_48.production_import_host_evidence.v1",
    actualHostTerminal: "success",
    lateEventSource: "m25_48_tracked_fixture",
    closeDispositions: closeResponses.map((receipt) => receipt.disposition),
    sourceIdentityPresent: preparation.sourceIdentity !== null,
    expectedFrames: preparation.expectedFrames,
    queueCalls: executionIdentity.queue!.queueCalls,
    executionIdentityStages: executionIdentity.trace.map(
      (entry) => entry.stage,
    ),
    executionIdentityEqual:
      new Set(executionIdentity.trace.map((entry) => entry.fingerprint))
        .size === 1,
    delegatedUnchanged: executionIdentity.queue!.delegatedUnchanged,
    openWorkflowDelta: workflowAfter.openCount - workflowBefore.openCount,
    browserTabDelta: context.pages().length - browserTabCount,
    surroundings,
    import: importEvidence,
    cleanup: cleanupEvidence,
    mediaJourney:
      mediaJourney === undefined
        ? null
        : {
            mode: mediaJourney,
            statusBefore: {
              resolutionState: mediaStatusBefore!.resolutionState,
              resolutionReason: mediaStatusBefore!.resolutionReason,
              sourceKind: mediaStatusBefore!.sourceKind,
              features: mediaStatusBefore!.features,
              installDescribed: mediaStatusBefore!.installDescribed,
              installActionOffered:
                mediaStatusBefore!.actions.includes("install_supported"),
            },
            setupPosts: [...setupPosts],
            setup: mediaSetupEvidence,
            render: renderEvidence,
            emptyTimelineNoteBeforeInsert: true,
          },
  };
  const serializedEvidence = JSON.stringify(evidence);
  expect(serializedEvidence).not.toMatch(/[A-Za-z]:[\\/]/);
  expect(serializedEvidence).not.toMatch(/(?:Users|home)[\\/]/i);
  console.info(
    `M25_16_AC11_PRODUCTION_IMPORT_HOST_EVIDENCE ${serializedEvidence}`,
  );
});
