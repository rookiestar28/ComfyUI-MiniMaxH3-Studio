import { expect, test, type Page } from "../../host/fixture";

import {
  decodeAuthoringProjection,
  decodeTimelineHistoryProjection,
  decodeTimelineReceipt,
  encodeAuthoringAction,
  encodeTimelineTransaction,
} from "../../../../src/contracts/authoringWorkbenchCodec";
import { decodeSidebarWorkspaceProjection } from "../../../../src/contracts/sidebarWorkspaceCodec";
import {
  assertCandidateBundleInjection,
  beginSettledH3InteractionPhase,
  candidateBackendMode,
  candidateBackendRuntime,
  candidateBundle,
  candidateInjectionCount,
  expectCandidateInteractionNetworkLocal,
  hostUrl,
  monitorCandidateInitiatorNetwork,
  monitorH3Network,
  waitForH3Registration,
  waitForHostGraphSettled,
} from "../../host/environment";
import {
  assertM2508ExactHostCapability,
  M2508_OWNED_NODE_IDS,
} from "../../host/m25_08Bootstrap";
import { diffGraphSurroundings } from "../../../support/surroundingsDiff";

type PromptNode = Readonly<{
  class_type: string;
  inputs: Readonly<Record<string, unknown>>;
}>;

type HostActionResponse = Readonly<{
  status: number;
  body: unknown;
}>;

type T6JourneyEvidence = Readonly<{
  canonicalVideo: Readonly<{
    sourceFrameCount: number;
    sourceTimeBase: Readonly<{ num: number; den: number }>;
    sourceSampleCount: number;
    outputFrameRate: Readonly<{ num: number; den: number }>;
    splitSourceStartFrames: readonly number[];
    receiptTimelineFingerprints: Readonly<{
      insertBefore: string;
      insertAfter: string;
      splitBefore: string;
      splitAfter: string;
      undoBefore: string;
      undoAfter: string;
      redoBefore: string;
      redoAfter: string;
    }>;
  }>;
  stale: Readonly<{
    status: number;
    rejectionCode: string;
    workspaceRevisionPreserved: boolean;
    timelineRevisionPreserved: boolean;
    publicFingerprintPreserved: boolean;
    undoCursorPreserved: boolean;
  }>;
  repeatInitialization: Readonly<{
    status: number;
    workspaceRevisionPreserved: boolean;
    timelineRevisionPreserved: boolean;
    publicFingerprintPreserved: boolean;
    undoCursorPreserved: boolean;
  }>;
  unavailableAfterRelease: Readonly<{
    status: number;
    bodyAbsent: boolean;
  }>;
}>;

const MODEL_FREE_CAPTURE_AUTHORIZED =
  process.env.H3_CONTEXT_M25_17_MODEL_FREE_CAPTURE_AUTHORIZED === "1";
const IMAGE_LOCATOR = process.env.H3_CONTEXT_M25_17_IMAGE_LOCATOR?.trim() ?? "";
const VIDEO_LOCATOR = process.env.H3_CONTEXT_M25_17_VIDEO_LOCATOR?.trim() ?? "";
const IMAGE_LOCATOR_PATTERN =
  /^m25_17_acceptance\/[a-f0-9]{8,64}\/m25_17_authorized_image_v1\.png$/;
const VIDEO_LOCATOR_PATTERN =
  /^m25_17_acceptance\/[a-f0-9]{8,64}\/m25_17_authorized_video_v1\.mp4$/;

function modelFreeCapturePrompt(): Readonly<Record<string, PromptNode>> {
  // CRITICAL: this prompt contains no native MiniMax generation node. The explicit host-row
  // authorization permits one source-capture execution, never model, provider, or GPU work.
  return Object.freeze({
    "1": {
      class_type: "comfyui_h3_context.H3Context.Request",
      inputs: {
        task_mode: "ref2va",
        user_intent:
          "Preserve the supplied synthetic references while the camera remains steady.",
        duration_seconds: ["10", 0],
      },
    },
    "2": {
      class_type: "LoadVideo",
      inputs: { file: VIDEO_LOCATOR },
    },
    "3": {
      class_type: "comfyui_h3_context.H3Context.ReferenceRegistry",
      inputs: { images: ["12", 0], videos: ["2", 0] },
    },
    "4": {
      class_type: "comfyui_h3_context.H3Context.Plan",
      inputs: { request: ["1", 0], reference_registry: ["3", 0] },
    },
    "5": {
      class_type: "comfyui_h3_context.H3Context.Compiler",
      inputs: { plan: ["4", 0] },
    },
    "6": {
      class_type: "comfyui_h3_context.H3Context.Validator",
      inputs: { plan: ["4", 0], prompt_document: ["5", 2] },
    },
    "7": {
      class_type: "comfyui_h3_context.H3Context.NativeH3Adapter",
      inputs: { report: ["6", 1] },
    },
    "8": {
      class_type: "comfyui_h3_context.H3Context.ProductShell",
      inputs: { report: ["6", 1], native_h3_wiring: ["7", 1] },
    },
    "10": {
      class_type: "PrimitiveFloat",
      inputs: { value: 4 },
    },
    "12": {
      class_type: "LoadImage",
      inputs: { image: IMAGE_LOCATOR },
    },
  });
}

async function postAuthoringAction(
  page: Page,
  body: unknown,
): Promise<HostActionResponse> {
  return page.evaluate(async (requestBody) => {
    const response = await fetch("/h3-context/v1/authoring/action", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(requestBody),
    });
    const text = await response.text();
    let responseBody: unknown = null;
    if (text.length > 0) {
      try {
        responseBody = JSON.parse(text) as unknown;
      } catch {
        throw new Error("authoring action returned malformed JSON");
      }
    }
    return { status: response.status, body: responseBody };
  }, body);
}

async function queueCounts(page: Page): Promise<{
  running: number;
  pending: number;
}> {
  const result = await page.evaluate(async () => {
    const response = await fetch("/queue");
    if (!response.ok) throw new Error("host queue status is unavailable");
    const body = (await response.json()) as Record<string, unknown>;
    return {
      running: Array.isArray(body.queue_running)
        ? body.queue_running.length
        : -1,
      pending: Array.isArray(body.queue_pending)
        ? body.queue_pending.length
        : -1,
    };
  });
  return result;
}

test("M25-17 preserves canonical VIDEO timing through split, readback, undo, and redo", async ({
  context,
  page,
}, testInfo) => {
  test.setTimeout(90_000);
  if (candidateBundle === null) {
    testInfo.annotations.push({
      type: "m25-17-host",
      description: "NOT_RUN: exact candidate bundle is not bound",
    });
    test.skip(true, "exact candidate bundle binding is required");
    return;
  }
  if (candidateBackendMode !== "exact" || candidateBackendRuntime === null) {
    testInfo.annotations.push({
      type: "m25-17-host",
      description: "NOT_RUN: exact candidate backend runtime is not bound",
    });
    test.skip(true, "exact candidate backend runtime parity is required");
    return;
  }
  if (!MODEL_FREE_CAPTURE_AUTHORIZED) {
    testInfo.annotations.push({
      type: "m25-17-host",
      description: "NOT_RUN: model-free source capture is not authorized",
    });
    test.skip(
      true,
      "one model-free source-capture execution must be authorized",
    );
    return;
  }
  if (IMAGE_LOCATOR.length === 0 || VIDEO_LOCATOR.length === 0) {
    testInfo.annotations.push({
      type: "m25-17-host",
      description: "NOT_RUN: authorized source locators are not bound",
    });
    test.skip(true, "authorized IMAGE and VIDEO locator bindings are required");
    return;
  }
  if (
    !IMAGE_LOCATOR_PATTERN.test(IMAGE_LOCATOR) ||
    !VIDEO_LOCATOR_PATTERN.test(VIDEO_LOCATOR)
  )
    throw new Error(
      "M25-17 source locator binding is outside its closed scope",
    );
  if (hostUrl === undefined) throw new Error("H3_CONTEXT_HOST_URL is required");

  const allowedOrigin = new URL(hostUrl).origin;
  for (const nodeId of M2508_OWNED_NODE_IDS) {
    const response = await page.request.get(
      new URL(`/object_info/${encodeURIComponent(nodeId)}`, hostUrl).href,
    );
    if (!response.ok())
      throw new Error("M25-17 active node module is not exact");
    assertM2508ExactHostCapability(nodeId, await response.json());
  }

  const networkAttribution = monitorH3Network(page, allowedOrigin);
  const candidateAttribution = await monitorCandidateInitiatorNetwork(
    context,
    page,
    allowedOrigin,
  );
  let promptCalls = 0;
  let authoringActionCalls = 0;
  page.on("request", (request) => {
    if (request.method() !== "POST") return;
    const path = new URL(request.url()).pathname;
    if (path.endsWith("/prompt")) promptCalls += 1;
    if (path.endsWith("/h3-context/v1/authoring/action"))
      authoringActionCalls += 1;
  });

  const injectionBefore = candidateInjectionCount(context);
  await page.goto(hostUrl, { waitUntil: "domcontentloaded" });
  await waitForH3Registration(page);
  await assertCandidateBundleInjection(page, context, injectionBefore);
  await waitForHostGraphSettled(page);
  await beginSettledH3InteractionPhase(
    page,
    networkAttribution,
    candidateAttribution,
  );

  const pagesBefore = context.pages().length;
  const surroundingsBaseline = await page.evaluate(() => {
    const app = (
      window as unknown as {
        comfyAPI: { app: { app: { graph: { serialize(): unknown } } } };
      }
    ).comfyAPI.app.app;
    return structuredClone(app.graph.serialize());
  });
  await page.evaluate(() => {
    const runtime = window as unknown as {
      comfyAPI: {
        api: { api: { addEventListener: Function } };
        app: {
          app: {
            graph: { serialize(): unknown };
            extensionManager?: {
              workflow?: { activeWorkflow?: unknown; openWorkflows?: unknown };
            };
          };
        };
      };
      __h3M2517Baseline?: {
        activeWorkflow: unknown;
        openWorkflowCount: number;
      };
      __h3M2517Events?: Array<{
        node: unknown;
        promptId: unknown;
        workspace: unknown;
      }>;
    };
    const app = runtime.comfyAPI.app.app;
    const workflow = app.extensionManager?.workflow;
    runtime.__h3M2517Baseline = {
      activeWorkflow: workflow?.activeWorkflow,
      openWorkflowCount: Array.isArray(workflow?.openWorkflows)
        ? workflow.openWorkflows.length
        : -1,
    };
    runtime.__h3M2517Events = [];
    runtime.comfyAPI.api.api.addEventListener("executed", (event: Event) => {
      const detail = (event as CustomEvent<Record<string, unknown>>).detail;
      const output = detail?.output as
        Record<string, unknown[]> | null | undefined;
      runtime.__h3M2517Events?.push({
        node: detail?.node,
        promptId: detail?.prompt_id,
        workspace: output?.sidebar_workspace?.[0],
      });
    });
  });

  const queueBefore = await queueCounts(page);
  if (queueBefore.running !== 0 || queueBefore.pending !== 0) {
    testInfo.annotations.push({
      type: "m25-17-host",
      description: "NOT_RUN: supplied host queue is occupied",
    });
    test.skip(true, "the shared host queue must already be empty");
    return;
  }

  const prompt = modelFreeCapturePrompt();
  expect(
    Object.values(prompt).some((node) =>
      /^MiniMaxH3(?:Image|Reference)ToVideo$/.test(node.class_type),
    ),
  ).toBe(false);
  const promptResponse = await page.evaluate(
    async ({ endpoint, promptValue }) => {
      const api = (
        window as unknown as {
          comfyAPI: { api: { api: { clientId: string } } };
        }
      ).comfyAPI.api.api;
      const response = await fetch(endpoint, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ client_id: api.clientId, prompt: promptValue }),
      });
      const text = await response.text();
      if (!response.ok)
        throw new Error("model-free source capture was refused");
      const body = JSON.parse(text) as { prompt_id?: unknown };
      if (typeof body.prompt_id !== "string" || body.prompt_id.length === 0)
        throw new Error("model-free source capture acknowledgement is invalid");
      return { promptId: body.prompt_id };
    },
    { endpoint: new URL("/prompt", hostUrl).href, promptValue: prompt },
  );
  await page.waitForFunction(
    (promptId) =>
      (
        (
          window as unknown as {
            __h3M2517Events?: Array<{
              node: unknown;
              promptId: unknown;
              workspace: unknown;
            }>;
          }
        ).__h3M2517Events ?? []
      ).some(
        (event) => String(event.node) === "8" && event.promptId === promptId,
      ),
    promptResponse.promptId,
  );
  await expect
    .poll(() => queueCounts(page))
    .toEqual({ running: 0, pending: 0 });

  const contextWorkspaceWire = await page.evaluate(
    (promptId) =>
      (
        (
          window as unknown as {
            __h3M2517Events?: Array<{
              node: unknown;
              promptId: unknown;
              workspace: unknown;
            }>;
          }
        ).__h3M2517Events ?? []
      ).find(
        (event) => String(event.node) === "8" && event.promptId === promptId,
      )?.workspace ?? null,
    promptResponse.promptId,
  );
  const contextWorkspace =
    decodeSidebarWorkspaceProjection(contextWorkspaceWire);
  expect(contextWorkspace.correlation.prompt_id).toBe(promptResponse.promptId);

  const requestSuffix = promptResponse.promptId.replace(/[^A-Za-z0-9]/g, "-");
  const create = await postAuthoringAction(
    page,
    encodeAuthoringAction(
      `m25-17-create-${requestSuffix}`,
      "create_authoring_workspace",
      { context_workspace_handle: contextWorkspace.workspace_id },
    ),
  );
  expect(create.status).toBe(201);
  const created = decodeAuthoringProjection(create.body);
  let released = false;
  let t6JourneyEvidence: T6JourneyEvidence | null = null;
  try {
    const initializationRequest = encodeAuthoringAction(
      `m25-17-init-${requestSuffix}`,
      "initialize_timeline_history",
      {
        workspace_handle: created.workspaceHandle,
        expected_reference_revision: created.reference.revision,
        expected_timeline_revision: created.timeline.revision,
      },
    );
    const initializedResponse = await postAuthoringAction(
      page,
      initializationRequest,
    );
    expect(initializedResponse.status).toBe(200);
    const initialized = decodeTimelineHistoryProjection(
      initializedResponse.body,
    );
    expect(initialized.workspaceHandle).toBe(created.workspaceHandle);
    expect(initialized.rejection).toBeNull();

    const initialReadResponse = await postAuthoringAction(
      page,
      encodeAuthoringAction(
        `m25-17-read-initial-${requestSuffix}`,
        "read_timeline_history",
        { workspace_handle: created.workspaceHandle },
      ),
    );
    expect(initialReadResponse.status).toBe(200);
    const initialHistory = decodeTimelineHistoryProjection(
      initialReadResponse.body,
    );
    expect(initialHistory.workspaceHandle).toBe(created.workspaceHandle);
    expect(initialHistory.rejection).toBeNull();
    expect(initialHistory.snapshot.publicFingerprint).toBe(
      initialized.snapshot.publicFingerprint,
    );

    const videoAsset = initialHistory.snapshot.assets.find(
      (asset) => asset.kind === "video",
    );
    const primaryTrack = initialHistory.snapshot.tracks.find(
      (track) => track.kind === "primary_video",
    );
    if (videoAsset === undefined || primaryTrack === undefined)
      throw new Error("initialized VIDEO library is not insertable");
    expect(videoAsset.sourceFrameCount).toBe(24);
    expect(videoAsset.sourceTimeBase).not.toBeNull();
    expect(videoAsset.landmarks).toHaveLength(24);
    expect(videoAsset.embeddedAudio).toBe("present_bound");
    expect(videoAsset.sourceSampleCount).toBe(96_000);
    expect(initialHistory.snapshot.output.frameRate).toEqual({
      num: 24,
      den: 1,
    });

    const videoClipId = "m25-17-host-video-clip";
    const rightVideoClipId = "m25-17-host-video-right";
    const insertTransaction = encodeTimelineTransaction({
      requestId: `m25-17-insert-${requestSuffix}`,
      transactionId: `m25-17-insert-transaction-${requestSuffix}`,
      workspaceHandle: initialHistory.workspaceHandle,
      expectedWorkspaceRevision: initialHistory.snapshot.workspaceRevision,
      expectedTimelineRevision: initialHistory.snapshot.timelineRevision,
      expectedTimelineFingerprint: initialHistory.snapshot.timelineFingerprint,
      commands: [
        {
          kind: "insert_asset_clip",
          payload: {
            clip: {
              clip_id: videoClipId,
              asset_id: videoAsset.assetId,
              track_id: primaryTrack.trackId,
              start_frame: 0,
              duration_frames: 24,
              source_start_frame: 0,
              enabled: true,
              transform: {
                anchor_x_bp: 5000,
                anchor_y_bp: 5000,
                position_x_bp: 0,
                position_y_bp: 0,
                scale_x_bp: 10000,
                scale_y_bp: 10000,
                rotation_mdeg: 0,
              },
              crop: { left_bp: 0, top_bp: 0, right_bp: 0, bottom_bp: 0 },
              opacity_bp: 10000,
              blend: "normal",
              text: null,
              transition: { kind: "none", duration_frames: 0 },
              effect: {
                kind: "none",
                brightness_permille: 0,
                contrast_permille: 1000,
                saturation_permille: 1000,
              },
            },
          },
        },
      ],
    });
    const insertedResponse = await postAuthoringAction(
      page,
      encodeAuthoringAction(
        insertTransaction.request_id,
        "apply_timeline_transaction",
        insertTransaction,
      ),
    );
    expect(insertedResponse.status).toBe(200);
    const inserted = decodeTimelineReceipt(insertedResponse.body);
    expect(inserted.workspaceHandle).toBe(created.workspaceHandle);
    expect(inserted.beforeTimelineFingerprint).toBe(
      initialHistory.snapshot.timelineFingerprint,
    );
    expect(inserted.afterTimelineFingerprint).toBe(
      inserted.snapshot.timelineFingerprint,
    );
    expect(
      inserted.snapshot.clips.find((clip) => clip.clipId === videoClipId),
    ).toMatchObject({
      assetId: videoAsset.assetId,
      trackId: primaryTrack.trackId,
      startFrame: 0,
      durationFrames: 24,
      sourceStartFrame: 0,
    });

    const splitTransaction = encodeTimelineTransaction({
      requestId: `m25-17-split-${requestSuffix}`,
      transactionId: `m25-17-split-transaction-${requestSuffix}`,
      workspaceHandle: inserted.workspaceHandle,
      expectedWorkspaceRevision: inserted.snapshot.workspaceRevision,
      expectedTimelineRevision: inserted.snapshot.timelineRevision,
      expectedTimelineFingerprint: inserted.snapshot.timelineFingerprint,
      commands: [
        {
          kind: "split_clip",
          payload: {
            clip_id: videoClipId,
            at_offset_frames: 12,
            right_clip_id: rightVideoClipId,
          },
        },
      ],
    });
    const splitResponse = await postAuthoringAction(
      page,
      encodeAuthoringAction(
        splitTransaction.request_id,
        "apply_timeline_transaction",
        splitTransaction,
      ),
    );
    expect(splitResponse.status).toBe(200);
    const split = decodeTimelineReceipt(splitResponse.body);
    expect(split.workspaceHandle).toBe(created.workspaceHandle);
    expect(split.beforeTimelineFingerprint).toBe(
      inserted.afterTimelineFingerprint,
    );
    expect(split.afterTimelineFingerprint).toBe(
      split.snapshot.timelineFingerprint,
    );

    const splitReadResponse = await postAuthoringAction(
      page,
      encodeAuthoringAction(
        `m25-17-read-split-${requestSuffix}`,
        "read_timeline_history",
        { workspace_handle: created.workspaceHandle },
      ),
    );
    expect(splitReadResponse.status).toBe(200);
    const splitHistory = decodeTimelineHistoryProjection(
      splitReadResponse.body,
    );
    expect(splitHistory.workspaceHandle).toBe(created.workspaceHandle);
    expect(splitHistory.rejection).toBeNull();
    expect(splitHistory.snapshot.publicFingerprint).toBe(
      split.snapshot.publicFingerprint,
    );
    expect(splitHistory.undoCursor).toBe(split.historyCursor);
    const splitVideoClips = splitHistory.snapshot.clips
      .filter((clip) => clip.assetId === videoAsset.assetId)
      .map((clip) => ({
        clipId: clip.clipId,
        trackId: clip.trackId,
        startFrame: clip.startFrame,
        durationFrames: clip.durationFrames,
        sourceStartFrame: clip.sourceStartFrame,
      }))
      .sort((left, right) => left.startFrame - right.startFrame);
    // Source frame identities advance at the admitted 12 fps clock. The 12-frame split is on the
    // 24 fps output clock, so the right clip must start at the exact source landmark frame 6.
    expect(splitVideoClips).toEqual([
      {
        clipId: videoClipId,
        trackId: primaryTrack.trackId,
        startFrame: 0,
        durationFrames: 12,
        sourceStartFrame: 0,
      },
      {
        clipId: rightVideoClipId,
        trackId: primaryTrack.trackId,
        startFrame: 12,
        durationFrames: 12,
        sourceStartFrame: 6,
      },
    ]);

    const staleTransaction = encodeTimelineTransaction({
      requestId: `m25-17-stale-${requestSuffix}`,
      transactionId: `m25-17-stale-transaction-${requestSuffix}`,
      workspaceHandle: inserted.workspaceHandle,
      expectedWorkspaceRevision: inserted.snapshot.workspaceRevision,
      expectedTimelineRevision: inserted.snapshot.timelineRevision,
      expectedTimelineFingerprint: inserted.snapshot.timelineFingerprint,
      commands: [
        { kind: "select_clips", payload: { clip_ids: [videoClipId] } },
      ],
    });
    const staleResponse = await postAuthoringAction(
      page,
      encodeAuthoringAction(
        staleTransaction.request_id,
        "apply_timeline_transaction",
        staleTransaction,
      ),
    );
    expect(staleResponse.status).toBe(409);
    const staleHistory = decodeTimelineHistoryProjection(staleResponse.body);
    expect(staleHistory.workspaceHandle).toBe(created.workspaceHandle);
    expect(staleHistory.rejection?.code).toBe("stale_workspace_revision");
    expect(staleHistory.snapshot.publicFingerprint).toBe(
      splitHistory.snapshot.publicFingerprint,
    );
    expect(staleHistory.snapshot.workspaceRevision).toBe(
      splitHistory.snapshot.workspaceRevision,
    );
    expect(staleHistory.snapshot.timelineRevision).toBe(
      splitHistory.snapshot.timelineRevision,
    );
    expect(staleHistory.undoCursor).toBe(splitHistory.undoCursor);

    if (splitHistory.undoCursor === null)
      throw new Error("accepted split has no undo cursor");
    const undoTransaction = encodeTimelineTransaction({
      requestId: `m25-17-undo-${requestSuffix}`,
      transactionId: `m25-17-undo-transaction-${requestSuffix}`,
      workspaceHandle: splitHistory.workspaceHandle,
      expectedWorkspaceRevision: splitHistory.snapshot.workspaceRevision,
      expectedTimelineRevision: splitHistory.snapshot.timelineRevision,
      expectedTimelineFingerprint: splitHistory.snapshot.timelineFingerprint,
      commands: [
        {
          kind: "undo",
          payload: { history_cursor: splitHistory.undoCursor },
        },
      ],
    });
    const undoResponse = await postAuthoringAction(
      page,
      encodeAuthoringAction(
        undoTransaction.request_id,
        "apply_timeline_transaction",
        undoTransaction,
      ),
    );
    expect(undoResponse.status).toBe(200);
    const undone = decodeTimelineReceipt(undoResponse.body);
    expect(undone.workspaceHandle).toBe(created.workspaceHandle);
    expect(undone.beforeTimelineFingerprint).toBe(
      splitHistory.snapshot.timelineFingerprint,
    );
    expect(undone.afterTimelineFingerprint).toBe(
      undone.snapshot.timelineFingerprint,
    );

    const undoReadResponse = await postAuthoringAction(
      page,
      encodeAuthoringAction(
        `m25-17-read-undo-${requestSuffix}`,
        "read_timeline_history",
        { workspace_handle: created.workspaceHandle },
      ),
    );
    expect(undoReadResponse.status).toBe(200);
    const undoHistory = decodeTimelineHistoryProjection(undoReadResponse.body);
    expect(undoHistory.workspaceHandle).toBe(created.workspaceHandle);
    expect(undoHistory.snapshot.publicFingerprint).toBe(
      undone.snapshot.publicFingerprint,
    );
    expect(undoHistory.redoCursor).toBe(undone.historyCursor);
    expect(
      undoHistory.snapshot.clips
        .filter((clip) => clip.assetId === videoAsset.assetId)
        .map((clip) => ({
          clipId: clip.clipId,
          startFrame: clip.startFrame,
          durationFrames: clip.durationFrames,
          sourceStartFrame: clip.sourceStartFrame,
        })),
    ).toEqual([
      {
        clipId: videoClipId,
        startFrame: 0,
        durationFrames: 24,
        sourceStartFrame: 0,
      },
    ]);

    if (undoHistory.redoCursor === null)
      throw new Error("accepted undo has no redo cursor");
    const redoTransaction = encodeTimelineTransaction({
      requestId: `m25-17-redo-${requestSuffix}`,
      transactionId: `m25-17-redo-transaction-${requestSuffix}`,
      workspaceHandle: undoHistory.workspaceHandle,
      expectedWorkspaceRevision: undoHistory.snapshot.workspaceRevision,
      expectedTimelineRevision: undoHistory.snapshot.timelineRevision,
      expectedTimelineFingerprint: undoHistory.snapshot.timelineFingerprint,
      commands: [
        {
          kind: "redo",
          payload: { history_cursor: undoHistory.redoCursor },
        },
      ],
    });
    const redoResponse = await postAuthoringAction(
      page,
      encodeAuthoringAction(
        redoTransaction.request_id,
        "apply_timeline_transaction",
        redoTransaction,
      ),
    );
    expect(redoResponse.status).toBe(200);
    const redone = decodeTimelineReceipt(redoResponse.body);
    expect(redone.workspaceHandle).toBe(created.workspaceHandle);
    expect(redone.beforeTimelineFingerprint).toBe(
      undoHistory.snapshot.timelineFingerprint,
    );
    expect(redone.afterTimelineFingerprint).toBe(
      redone.snapshot.timelineFingerprint,
    );

    const redoReadResponse = await postAuthoringAction(
      page,
      encodeAuthoringAction(
        `m25-17-read-redo-${requestSuffix}`,
        "read_timeline_history",
        { workspace_handle: created.workspaceHandle },
      ),
    );
    expect(redoReadResponse.status).toBe(200);
    const redoHistory = decodeTimelineHistoryProjection(redoReadResponse.body);
    expect(redoHistory.workspaceHandle).toBe(created.workspaceHandle);
    expect(redoHistory.snapshot.publicFingerprint).toBe(
      redone.snapshot.publicFingerprint,
    );
    expect(
      redoHistory.snapshot.clips
        .filter((clip) => clip.assetId === videoAsset.assetId)
        .map((clip) => ({
          clipId: clip.clipId,
          startFrame: clip.startFrame,
          durationFrames: clip.durationFrames,
          sourceStartFrame: clip.sourceStartFrame,
        }))
        .sort((left, right) => left.startFrame - right.startFrame),
    ).toEqual(
      splitVideoClips.map(
        ({ clipId, startFrame, durationFrames, sourceStartFrame }) => ({
          clipId,
          startFrame,
          durationFrames,
          sourceStartFrame,
        }),
      ),
    );

    const repeatedInitialization = await postAuthoringAction(
      page,
      encodeAuthoringAction(
        `m25-17-repeat-init-${requestSuffix}`,
        "initialize_timeline_history",
        initializationRequest.payload,
      ),
    );
    expect(repeatedInitialization.status).toBe(200);
    const repeatedHistory = decodeTimelineHistoryProjection(
      repeatedInitialization.body,
    );
    expect(repeatedHistory.workspaceHandle).toBe(created.workspaceHandle);
    expect(repeatedHistory.rejection).toBeNull();
    expect(repeatedHistory.snapshot.publicFingerprint).toBe(
      redoHistory.snapshot.publicFingerprint,
    );
    expect(repeatedHistory.snapshot.workspaceRevision).toBe(
      redoHistory.snapshot.workspaceRevision,
    );
    expect(repeatedHistory.snapshot.timelineRevision).toBe(
      redoHistory.snapshot.timelineRevision,
    );
    expect(repeatedHistory.undoCursor).toBe(redoHistory.undoCursor);

    const release = await postAuthoringAction(
      page,
      encodeAuthoringAction(
        `m25-17-release-${requestSuffix}`,
        "release_workspace",
        { workspace_handle: created.workspaceHandle },
      ),
    );
    released = release.status === 204;
    expect(release).toEqual({ status: 204, body: null });

    const unavailableResponse = await postAuthoringAction(
      page,
      encodeAuthoringAction(
        `m25-17-read-unavailable-${requestSuffix}`,
        "read_timeline_history",
        { workspace_handle: created.workspaceHandle },
      ),
    );
    expect(unavailableResponse.status).toBe(410);
    expect(unavailableResponse.body).toBeNull();

    if (
      videoAsset.sourceTimeBase === null ||
      videoAsset.sourceFrameCount === null ||
      videoAsset.sourceSampleCount === null
    )
      throw new Error("accepted VIDEO timing evidence is unavailable");
    t6JourneyEvidence = {
      canonicalVideo: {
        sourceFrameCount: videoAsset.sourceFrameCount,
        sourceTimeBase: videoAsset.sourceTimeBase,
        sourceSampleCount: videoAsset.sourceSampleCount,
        outputFrameRate: { num: 24, den: 1 },
        splitSourceStartFrames: splitVideoClips.map(
          (clip) => clip.sourceStartFrame,
        ),
        receiptTimelineFingerprints: {
          insertBefore: inserted.beforeTimelineFingerprint,
          insertAfter: inserted.afterTimelineFingerprint,
          splitBefore: split.beforeTimelineFingerprint,
          splitAfter: split.afterTimelineFingerprint,
          undoBefore: undone.beforeTimelineFingerprint,
          undoAfter: undone.afterTimelineFingerprint,
          redoBefore: redone.beforeTimelineFingerprint,
          redoAfter: redone.afterTimelineFingerprint,
        },
      },
      stale: {
        status: staleResponse.status,
        rejectionCode: staleHistory.rejection?.code ?? "missing",
        workspaceRevisionPreserved:
          staleHistory.snapshot.workspaceRevision ===
          splitHistory.snapshot.workspaceRevision,
        timelineRevisionPreserved:
          staleHistory.snapshot.timelineRevision ===
          splitHistory.snapshot.timelineRevision,
        publicFingerprintPreserved:
          staleHistory.snapshot.publicFingerprint ===
          splitHistory.snapshot.publicFingerprint,
        undoCursorPreserved:
          staleHistory.undoCursor === splitHistory.undoCursor,
      },
      repeatInitialization: {
        status: repeatedInitialization.status,
        workspaceRevisionPreserved:
          repeatedHistory.snapshot.workspaceRevision ===
          redoHistory.snapshot.workspaceRevision,
        timelineRevisionPreserved:
          repeatedHistory.snapshot.timelineRevision ===
          redoHistory.snapshot.timelineRevision,
        publicFingerprintPreserved:
          repeatedHistory.snapshot.publicFingerprint ===
          redoHistory.snapshot.publicFingerprint,
        undoCursorPreserved:
          repeatedHistory.undoCursor === redoHistory.undoCursor,
      },
      unavailableAfterRelease: {
        status: unavailableResponse.status,
        bodyAbsent: unavailableResponse.body === null,
      },
    };
  } finally {
    if (!released) {
      const cleanup = await postAuthoringAction(
        page,
        encodeAuthoringAction(
          `m25-17-cleanup-${requestSuffix}`,
          "release_workspace",
          { workspace_handle: created.workspaceHandle },
        ),
      );
      if (cleanup.status !== 204 && cleanup.status !== 410)
        throw new Error("M25-17 authoring workspace cleanup failed");
    }
  }

  const hostPreservation = await page.evaluate(() => {
    const runtime = window as unknown as {
      comfyAPI: {
        app: {
          app: {
            graph: { serialize(): unknown };
            extensions?: Array<{ name?: unknown }>;
            extensionManager?: {
              workflow?: { activeWorkflow?: unknown; openWorkflows?: unknown };
            };
          };
        };
      };
      __h3M2517Baseline?: {
        activeWorkflow: unknown;
        openWorkflowCount: number;
      };
    };
    const baseline = runtime.__h3M2517Baseline;
    if (baseline === undefined)
      throw new Error("M25-17 host baseline is unavailable");
    const app = runtime.comfyAPI.app.app;
    const workflow = app.extensionManager?.workflow;
    const packNames = Array.isArray(app.extensions)
      ? app.extensions
          .map((extension) => extension?.name)
          .filter(
            (name): name is string =>
              typeof name === "string" &&
              /^[A-Za-z0-9][A-Za-z0-9._ -]{0,127}$/.test(name),
          )
      : [];
    const uniquePackNames = [...new Set(packNames)].sort();
    // IMPORTANT: bound retained evidence without turning foreign extension counts into an
    // acceptance rule; a larger installed set must not reject an otherwise valid owned journey.
    return {
      activeWorkflowStable:
        workflow?.activeWorkflow === baseline.activeWorkflow,
      openWorkflowCount: Array.isArray(workflow?.openWorkflows)
        ? workflow.openWorkflows.length
        : -1,
      expectedOpenWorkflowCount: baseline.openWorkflowCount,
      packNames: uniquePackNames.slice(0, 512),
      packNameCount: uniquePackNames.length,
      packNamesTruncated: uniquePackNames.length > 512,
    };
  });
  const finalGraph = await page.evaluate(() => {
    const app = (
      window as unknown as {
        comfyAPI: { app: { app: { graph: { serialize(): unknown } } } };
      }
    ).comfyAPI.app.app;
    return app.graph.serialize();
  });
  const ownedProjection = {
    nodeIds: [] as string[],
    linkIds: [] as string[],
    anchorNodeId: null,
    anchorPromptBinding: null,
  } as const;
  expect(ownedProjection).toEqual({
    nodeIds: [],
    linkIds: [],
    anchorNodeId: null,
    anchorPromptBinding: null,
  });
  const surroundingsEvidence = await page.evaluate(diffGraphSurroundings, {
    beforeValue: surroundingsBaseline,
    afterValue: finalGraph,
    reference: {
      ownedNodeIds: ownedProjection.nodeIds,
      ownedLinkIds: ownedProjection.linkIds,
      anchorNodeId: ownedProjection.anchorNodeId ?? "",
      ownedProjectionEqual: true,
    },
  });
  expect(surroundingsEvidence.counts.owned).toBe(0);
  expect(hostPreservation.activeWorkflowStable).toBe(true);
  expect(hostPreservation.openWorkflowCount).toBe(
    hostPreservation.expectedOpenWorkflowCount,
  );
  expect(context.pages()).toHaveLength(pagesBefore);
  expect(promptCalls).toBe(1);
  expect(authoringActionCalls).toBe(14);
  expect(t6JourneyEvidence).not.toBeNull();
  const queueAfter = await queueCounts(page);
  expect(queueAfter).toEqual(queueBefore);
  console.log(
    `M25_17_HOST_EVIDENCE=${JSON.stringify({
      promptCalls,
      authoringActionCalls,
      queueBefore,
      queueAfter,
      openWorkflowCount: hostPreservation.openWorkflowCount,
      pageCount: pagesBefore,
      ownedProjection,
      surroundings: surroundingsEvidence,
      t6Journey: t6JourneyEvidence,
      packNames: hostPreservation.packNames,
      packNameCount: hostPreservation.packNameCount,
      packNamesTruncated: hostPreservation.packNamesTruncated,
    })}`,
  );
  expect(networkAttribution.snapshot()).toMatchObject({
    interactionRemoteCount: 0,
    interactionProviderCount: 0,
  });
  expectCandidateInteractionNetworkLocal(candidateAttribution);
});
