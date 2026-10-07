// M25-16 hermetic harness: the real `overlay_v1` root and workspace, mounted from the accepted
// smoke/virtualized fixtures against a stub authoring seam that accepts every transaction
// after one tick. The page records React commit durations, timeline intents and close reasons
// so the journey can assert the frozen budgets without any host, backend or media.

import {
  Profiler,
  useEffect,
  useRef,
  useState,
  type ProfilerOnRenderCallback,
} from "react";
import { createRoot } from "react-dom/client";

import tokenStyles from "../src/styles/tokens.css?inline";
import {
  mediaOwnership,
  mediaPreparation,
  workspaceMediaClient,
} from "./nleWorkspaceMedia";

import type {
  AuthoringIntent,
  AuthoringViewState,
} from "../src/state/authoringViewState";
import { NleOverlay } from "../src/components/nle/NleOverlay";
import type { NleWorkspaceBinding } from "../src/components/nle/nleWorkspaceBinding";
import type {
  TimelineCommandWire,
  TimelineHistoryProjectionV2,
  TimelineReceipt,
  NleAuthoringStateV2,
} from "../src/contracts/authoringWorkbenchCodec";
import {
  decodeTimelineHistoryProjectionV2,
  decodeTimelineHistoryProjection,
  decodeTimelineReceipt,
  encodeTimelineTransaction,
  encodeTimelineTransactionV2,
  decodeTimelineReceiptV2,
} from "../src/contracts/authoringWorkbenchCodec";
import {
  compositionContractFingerprint,
  publicCompositionFingerprint,
  type PublicCompositionSnapshot,
} from "../src/contracts/compositionCodec";
import { decodeProductionAuthoringImportResponse } from "../src/contracts/productionAuthoringImportCodec";
import type { AuthoringMediaSourceLeaseClient } from "../src/host/authoringMediaSourceLease";
import type { OutputClient } from "../src/host/authoringOutputActions";
import type { OutputPreview } from "../src/host/authoringOutputPreview";
import { SUPPORTED_LOCALES, type Locale } from "../src/i18n/catalog";
import {
  ACCEPTED_RUNTIME_QUALIFICATION,
  RUNTIME_QUALIFICATION_AUTHORITY,
} from "../src/runtime/acceptedRuntimeQualification";
import {
  evaluateMediaCapabilities,
  observeBrowserMediaCapabilities,
} from "../src/runtime/mediaCapabilities";
import {
  clampOverlayBounds,
  overlayDefaultBounds,
  type OverlayBounds,
} from "../src/runtime/nleOverlayGeometry";
import type { NleLayout } from "../src/runtime/nleLayoutGeometry";
import {
  initialNleWorkspaceState,
  type NleCloseReason,
  type NleWorkspaceState,
} from "../src/state/nleWorkspaceState";
import {
  SMOKE_SHAPE,
  VIRTUALIZED_SHAPE,
  authoringReady,
  historyWire,
  snapshotWire,
  type FixtureShape,
} from "../tests/support/nleWorkspaceFixture";
import { importV2ResponseWire } from "../tests/support/productionAuthoringImportFixture";

export type NleWorkspaceHarnessSnapshot = Readonly<{
  shape: string;
  runtimeStatus: string;
  surfaceStatus: string;
  generation: number;
  closeReasons: readonly string[];
  mounted: number;
  mountFailed: number;
  released: number;
  intents: readonly {
    action: string;
    commands: readonly TimelineCommandWire[];
  }[];
  receipts: number;
  commitDurationsMs: readonly number[];
  commitStates: readonly string[];
  handlerDurationsMs: readonly number[];
  bounds: OverlayBounds;
  layout: NleLayout;
  timelineSnapshot: PublicCompositionSnapshot | null;
  authoringStateV2: NleAuthoringStateV2 | null;
  authoringV2ContentEndExclusive: number | null;
  authoringV2Selection: readonly string[] | null;
  mediaOwnership: ReturnType<typeof mediaOwnership>;
  mediaPreparation: ReturnType<typeof mediaPreparation>;
  parentCommits: number;
  historyPending: number;
  lateHistoryAfterClose: number;
}>;

type HarnessApi = Readonly<{
  snapshot(): NleWorkspaceHarnessSnapshot;
  open(): void;
  replaceFirstMediaAssetFingerprint(): void;
  resetMeasurements(): void;
}>;

declare global {
  interface Window {
    nleWorkspaceHarness: HarnessApi;
  }
}

const shapeName = new URLSearchParams(location.search).get("shape");
const canonical = new URLSearchParams(location.search).get("canonical") === "1";
const authoringV2Mode =
  new URLSearchParams(location.search).get("authoringV2") === "1";
const authoringV2Frames = Number(
  new URLSearchParams(location.search).get("frames") ?? "120",
);
const media = new URLSearchParams(location.search).get("media") === "1";
const zeroClips = new URLSearchParams(location.search).get("zeroClips") === "1";
const filmstrip = new URLSearchParams(location.search).get("filmstrip") === "1";
const loseMoveReply =
  new URLSearchParams(location.search).get("loseMoveReply") === "1";
// M25-41 measures the sequence pane toolbar in every locale; every other journey keeps English.
const requestedLocale = new URLSearchParams(location.search).get("locale");
const harnessLocale: Locale = (SUPPORTED_LOCALES as readonly string[]).includes(
  requestedLocale ?? "",
)
  ? (requestedLocale as Locale)
  : "en";
// Mirrors the accepted session's two-step adoption of an accepted transaction: the receipt is
// adopted first and obsolete history dropped (`pending` with `lastTimelineReceipt`, no
// `timelineHistory`), then the refreshed history arrives after this delay. `0` keeps the
// single-step behaviour every other journey measures.
const historyDelayMs = Math.max(
  0,
  Number(new URLSearchParams(location.search).get("historyDelayMs") ?? "0") ||
    0,
);
// B-M2564-01: the in-page dispatcher (no canonical backend) settles on the next task. `pendingMs`
// holds its pending state open, so a journey can act inside the window where controls are busy.
const pendingMs = Math.max(
  0,
  Number(new URLSearchParams(location.search).get("pendingMs") ?? "0") || 0,
);
const shape: FixtureShape =
  shapeName === "virtualized" ? VIRTUALIZED_SHAPE : SMOKE_SHAPE;

type V2AuthoringWire = Record<string, unknown>;
type V2HistoryEntry = Readonly<{
  clips: readonly Record<string, unknown>[];
  selection: readonly string[];
  undoCursor: string | null;
}>;

let v2BaseAuthoringWire: V2AuthoringWire | null = null;

function createV2BaseAuthoringWire(): V2AuthoringWire {
  if (
    !Number.isSafeInteger(authoringV2Frames) ||
    authoringV2Frames < 1 ||
    authoringV2Frames > 3_600
  )
    throw new Error("V2 fixture source duration is outside the edit profile");
  const response = importV2ResponseWire();
  const history = response.history_projection as Record<string, unknown>;
  const authoring = JSON.parse(
    JSON.stringify(history.authoring),
  ) as V2AuthoringWire;
  const assets = authoring.assets as Record<string, unknown>[];
  const template = assets[0];
  if (template === undefined) throw new Error("V2 fixture has no video asset");
  const landmarks = Array.from(
    { length: authoringV2Frames },
    (_, frameIndex) => ({
      frame_index: frameIndex,
      pts: frameIndex * 512,
      dts: frameIndex * 512,
      duration_ticks: 512,
    }),
  );
  authoring.assets = [
    {
      ...template,
      asset_id: "generated.asset.1",
      source_frame_count: authoringV2Frames,
      landmarks,
    },
    {
      ...template,
      asset_id: "generated.asset.2",
      source_frame_count: authoringV2Frames,
      landmarks,
    },
  ];
  const timelineFingerprint = compositionContractFingerprint({
    operation_profile_id: authoring.operation_profile_id,
    edit_capacity_frames: authoring.edit_capacity_frames,
    content_end_exclusive: 0,
    tracks: authoring.tracks,
    clips: [],
    audio_extension: authoring.audio_extension,
  });
  authoring.timeline_fingerprint = timelineFingerprint;
  authoring.workspace_fingerprint = compositionContractFingerprint({
    project_id: authoring.project_id,
    workspace_handle: authoring.workspace_handle,
    workspace_revision: authoring.workspace_revision,
    timeline_revision: authoring.timeline_revision,
    timeline_fingerprint: timelineFingerprint,
  });
  const material = { ...authoring };
  delete material.authoring_fingerprint;
  authoring.authoring_fingerprint = compositionContractFingerprint(material);
  return authoring;
}

function v2Cursor(revision: number): string {
  return `h3.context.timeline_history_cursor.v2:${revision}:${revision
    .toString(16)
    .padStart(64, "0")}`;
}

function encodeV2History({
  base,
  clips,
  selection,
  workspaceRevision,
  timelineRevision,
  undoCursor,
  redoCursor,
}: Readonly<{
  base: V2AuthoringWire;
  clips: readonly Record<string, unknown>[];
  selection: readonly string[];
  workspaceRevision: number;
  timelineRevision: number;
  undoCursor: string | null;
  redoCursor: string | null;
}>): TimelineHistoryProjectionV2 {
  const contentEnd = clips.reduce(
    (end, clip) =>
      Math.max(end, Number(clip.start_frame) + Number(clip.duration_frames)),
    0,
  );
  const authoring: V2AuthoringWire = {
    ...base,
    workspace_revision: workspaceRevision,
    timeline_revision: timelineRevision,
    content_end_exclusive: contentEnd,
    clips,
    timeline_fingerprint: compositionContractFingerprint({
      operation_profile_id: base.operation_profile_id,
      edit_capacity_frames: base.edit_capacity_frames,
      content_end_exclusive: contentEnd,
      tracks: base.tracks,
      clips,
      audio_extension: base.audio_extension,
    }),
  };
  authoring.workspace_fingerprint = compositionContractFingerprint({
    project_id: authoring.project_id,
    workspace_handle: authoring.workspace_handle,
    workspace_revision: workspaceRevision,
    timeline_revision: timelineRevision,
    timeline_fingerprint: authoring.timeline_fingerprint,
  });
  const authoringMaterial = { ...authoring };
  delete authoringMaterial.authoring_fingerprint;
  authoring.authoring_fingerprint =
    compositionContractFingerprint(authoringMaterial);

  let renderSnapshot: Record<string, unknown> | null = null;
  if (contentEnd > 0) {
    renderSnapshot = JSON.parse(
      JSON.stringify(snapshotWire(SMOKE_SHAPE)),
    ) as Record<string, unknown>;
    renderSnapshot.project_id = authoring.project_id;
    renderSnapshot.workspace_handle = authoring.workspace_handle;
    renderSnapshot.workspace_revision = workspaceRevision;
    renderSnapshot.workspace_fingerprint = authoring.workspace_fingerprint;
    renderSnapshot.timeline_revision = timelineRevision;
    renderSnapshot.timeline_fingerprint = authoring.timeline_fingerprint;
    renderSnapshot.assets = authoring.assets;
    renderSnapshot.tracks = authoring.tracks;
    renderSnapshot.clips = clips;
    renderSnapshot.audio_extension = authoring.audio_extension;
    renderSnapshot.blockers = authoring.blockers;
    (renderSnapshot.output as Record<string, unknown>).duration_frames =
      contentEnd;
    renderSnapshot.public_fingerprint =
      publicCompositionFingerprint(renderSnapshot);
  }
  return decodeTimelineHistoryProjectionV2({
    schema: "h3.context.timeline_history_projection.v2",
    workspace_handle: authoring.workspace_handle,
    authoring,
    render_snapshot: renderSnapshot,
    selection,
    undo_cursor: undoCursor,
    redo_cursor: redoCursor,
    rejection: null,
  });
}

function initialV2Authoring(): AuthoringViewState {
  const imported = decodeProductionAuthoringImportResponse(
    importV2ResponseWire(),
  );
  if (!("historyProjection" in imported))
    throw new Error("V2 fixture import response lacks its V2 history");
  v2BaseAuthoringWire ??= createV2BaseAuthoringWire();
  const legacy = authoringReady(SMOKE_SHAPE);
  if (!("timelineHistory" in legacy) || legacy.timelineHistory === undefined)
    throw new Error("V2 fixture lacks its retained V1 projection");
  const initialWire: V2AuthoringWire = { ...v2BaseAuthoringWire };
  return {
    status: "ready",
    projection: imported.authoringProjection,
    timelineHistory: legacy.timelineHistory,
    timelineHistoryV2: encodeV2History({
      base: initialWire,
      clips: [],
      selection: [],
      workspaceRevision: Number(initialWire.workspace_revision),
      timelineRevision: Number(initialWire.timeline_revision),
      undoCursor: null,
      redoCursor: null,
    }),
  };
}

function prepareSnapshotWire(wire: Record<string, unknown>) {
  if (filmstrip) {
    const assets = wire.assets as Record<string, unknown>[];
    const video = assets.find((asset) => asset.kind === "video");
    if (video === undefined) throw new Error("filmstrip fixture lacks video");
    const frameCount = Number(video.source_frame_count);
    const first = (video.landmarks as Record<string, number>[])[0];
    if (
      !Number.isSafeInteger(frameCount) ||
      frameCount < 1 ||
      first === undefined
    )
      throw new Error("filmstrip fixture timing is invalid");
    const durationTicks = first.duration_ticks;
    video.landmarks = Array.from({ length: frameCount }, (_, frameIndex) => ({
      frame_index: frameIndex,
      pts: frameIndex * durationTicks,
      dts: frameIndex * durationTicks,
      duration_ticks: durationTicks,
    }));
    // Keep frame zero as an explicit decoration window. The accepted M25-48 scheduler holds
    // playback priority until the monitor releases its source authority; a clip at the initial
    // playhead would correctly defer both thumbnails and filmstrips and make this presentation
    // fixture prove only starvation-by-active-playback rather than filmstrip acquisition.
    for (const clip of wire.clips as Record<string, unknown>[])
      if (clip.start_frame === 0) clip.start_frame = 48;
  }
  if (zeroClips) wire.clips = [];
  wire.public_fingerprint = publicCompositionFingerprint(wire);
  return wire;
}

function initialSnapshotWire() {
  const wire = snapshotWire(shape);
  return prepareSnapshotWire(wire);
}

function initialAuthoring(): AuthoringViewState {
  if (authoringV2Mode) return initialV2Authoring();
  const selection = zeroClips ? [] : ["clip-0"];
  if (!zeroClips && !filmstrip) return authoringReady(shape, { selection });
  const history = historyWire(shape, { selection });
  prepareSnapshotWire(history.snapshot as Record<string, unknown>);
  return {
    ...authoringReady(shape, { selection }),
    timelineHistory: decodeTimelineHistoryProjection(history),
  } as AuthoringViewState;
}

const runtime = evaluateMediaCapabilities(
  observeBrowserMediaCapabilities(),
  ACCEPTED_RUNTIME_QUALIFICATION,
  RUNTIME_QUALIFICATION_AUTHORITY,
);

const record = {
  closeReasons: [] as string[],
  mounted: 0,
  mountFailed: 0,
  released: 0,
  intents: [] as { action: string; commands: readonly TimelineCommandWire[] }[],
  receipts: 0,
  commitDurationsMs: [] as number[],
  commitStates: [] as string[],
  handlerDurationsMs: [] as number[],
  parentCommits: 0,
  historyPending: 0,
  lateHistoryAfterClose: 0,
};

const refuse = (what: string) => async () => {
  throw new Error(`${what} must not be reached by the hermetic workspace`);
};
const leaseClient = media
  ? workspaceMediaClient
  : ({
      create: refuse("lease create"),
      acquireVideoSource: refuse("lease acquire"),
    } as unknown as AuthoringMediaSourceLeaseClient);
const output = {
  client: {
    create: refuse("output create"),
    status: refuse("output status"),
    cancel: refuse("output cancel"),
  } as unknown as OutputClient,
  preview: {
    open: refuse("output preview"),
    close: () => undefined,
  } as unknown as OutputPreview,
};

function viewport() {
  return { width: window.innerWidth, height: window.innerHeight };
}

let receiptCounter = 0;
function receiptFor(
  authoring: AuthoringViewState,
  commands: readonly TimelineCommandWire[],
): TimelineReceipt {
  if (
    !("timelineHistory" in authoring) ||
    authoring.timelineHistory === undefined
  )
    throw new Error("harness authoring state lacks a history");
  const snapshot = authoring.timelineHistory.snapshot;
  receiptCounter += 1;
  return Object.freeze({
    schema: "h3.context.timeline_receipt.v1",
    requestId: `harness-${receiptCounter}`,
    transactionId: `tx-${receiptCounter}`,
    workspaceHandle: snapshot.workspaceHandle,
    beforeWorkspaceRevision: snapshot.workspaceRevision,
    afterWorkspaceRevision: snapshot.workspaceRevision + 1,
    beforeWorkspaceFingerprint: snapshot.workspaceFingerprint,
    afterWorkspaceFingerprint: snapshot.workspaceFingerprint,
    beforeTimelineRevision: snapshot.timelineRevision,
    afterTimelineRevision: snapshot.timelineRevision + 1,
    beforeTimelineFingerprint: snapshot.timelineFingerprint,
    afterTimelineFingerprint: snapshot.timelineFingerprint,
    commands,
    affectedIds: Object.freeze([]),
    inverse: Object.freeze({
      kind: "restore_transaction_state",
      historyCursor: authoring.timelineHistory.undoCursor ?? "",
    }),
    historyCursor: authoring.timelineHistory.undoCursor ?? "",
    selection: Object.freeze([]),
    snapshot,
  }) as unknown as TimelineReceipt;
}

function Harness() {
  useEffect(() => {
    record.parentCommits++;
  });
  const [state, setState] = useState<NleWorkspaceState>(
    initialNleWorkspaceState,
  );
  const [authoring, setAuthoring] = useState<AuthoringViewState>(() =>
    initialAuthoring(),
  );
  const v2Clips = useRef<Record<string, unknown>[]>([]);
  const v2Undo = useRef<V2HistoryEntry[]>([]);
  const v2Redo = useRef<V2HistoryEntry[]>([]);
  // A live ref: closures awaiting a delayed history refresh read the latest surface state.
  const stateRef = useRef(state);
  stateRef.current = state;

  useEffect(() => {
    if (!canonical) return;
    const controller = new AbortController();
    void fetch("/__nle_fixture/bootstrap", {
      signal: controller.signal,
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(
        authoringV2Mode ? v2BaseAuthoringWire : initialSnapshotWire(),
      ),
    })
      .then((response) => response.json())
      .then((value) => {
        if (!controller.signal.aborted)
          setAuthoring(
            (current) =>
              ({
                ...current,
                status: "ready",
                ...(authoringV2Mode
                  ? {
                      timelineHistoryV2: decodeTimelineHistoryProjectionV2(
                        value.history,
                      ),
                    }
                  : {
                      timelineHistory: decodeTimelineHistoryProjection(
                        value.history,
                      ),
                    }),
              }) as AuthoringViewState,
          );
      });
    return () => controller.abort();
  }, []);

  const patchSurface = (next: Partial<NleWorkspaceState["surface"]>) =>
    setState((current) => ({
      ...current,
      surface: { ...current.surface, ...next },
    }));

  const open = () => {
    const current = stateRef.current.surface;
    if (
      current.status !== "compact_ready" &&
      current.status !== "compact_unsupported"
    )
      return;
    patchSurface({
      status: "opening",
      generation: current.generation + 1,
      bounds: overlayDefaultBounds(viewport()),
      pane: "assets",
      lastCloseReason: null,
    });
  };

  const close = (reason: NleCloseReason) => {
    record.closeReasons.push(reason);
    setState((current) =>
      current.surface.status === "opening" ||
      current.surface.status === "expanded"
        ? {
            ...current,
            surface: {
              ...current.surface,
              status: "closing",
              lastCloseReason: reason,
            },
          }
        : current,
    );
  };

  const timeline = async (intent: {
    action: string;
    commands?: readonly TimelineCommandWire[];
    capturedTimeline?: Extract<
      AuthoringIntent,
      { action: "apply_timeline_commands" }
    >["capturedTimeline"];
  }) => {
    const started = performance.now();
    const commands = intent.commands ?? [];
    record.intents.push({ action: intent.action, commands });
    if (authoringV2Mode && canonical) {
      const history =
        "timelineHistoryV2" in authoring
          ? authoring.timelineHistoryV2
          : undefined;
      if (history === undefined || intent.action !== "apply_timeline_commands")
        throw new Error(
          "canonical V2 fixture history or timeline intent is absent",
        );
      const captured = intent.capturedTimeline ?? history.authoring;
      if (captured.authoringFingerprint === undefined)
        throw new Error(
          "canonical V2 transaction lacks captured authoring authority",
        );
      const requestId = `canonical-${++receiptCounter}`;
      const transaction = encodeTimelineTransactionV2({
        requestId,
        transactionId: `tx-${requestId}`,
        workspaceHandle: captured.workspaceHandle,
        expectedWorkspaceRevision: captured.workspaceRevision,
        expectedTimelineRevision: captured.timelineRevision,
        expectedTimelineFingerprint: captured.timelineFingerprint,
        expectedAuthoringFingerprint: captured.authoringFingerprint,
        commands,
      });
      setAuthoring(
        (current) => ({ ...current, status: "pending" }) as AuthoringViewState,
      );
      record.handlerDurationsMs.push(performance.now() - started);
      const response = await fetch("/__nle_fixture/transaction", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(transaction),
      });
      const result = await response.json();
      const nextHistory = decodeTimelineHistoryProjectionV2(result.history);
      const receipt =
        result.receipt === null
          ? undefined
          : decodeTimelineReceiptV2(result.receipt);
      if (receipt !== undefined) record.receipts += 1;
      setAuthoring(
        (current) =>
          ({
            ...current,
            status: result.status === 200 ? "ready" : "conflict",
            timelineHistoryV2: nextHistory,
            ...(receipt === undefined
              ? {}
              : { lastTimelineReceiptV2: receipt }),
          }) as AuthoringViewState,
      );
      return;
    }
    if (authoringV2Mode) {
      const history =
        "timelineHistoryV2" in authoring
          ? authoring.timelineHistoryV2
          : undefined;
      const base = v2BaseAuthoringWire;
      if (history === undefined || base === null)
        throw new Error("V2 fixture history is absent");
      if (intent.action !== "apply_timeline_commands" || commands.length !== 1)
        throw new Error(
          "V2 fixture only accepts one explicit timeline command",
        );
      const command = commands[0]!;
      const current = {
        clips: [...v2Clips.current],
        selection: history.selection,
        undoCursor: history.undoCursor,
      };
      let selection: readonly string[] = [];
      let undoCursor: string | null = null;
      let redoCursor: string | null = null;
      const revision =
        history.authoring.timelineRevision +
        (command.kind === "select_clips" ? 0 : 1);
      if (command.kind === "insert_asset_clip") {
        v2Undo.current.push(current);
        v2Redo.current = [];
        const clip = command.payload.clip as unknown as Record<string, unknown>;
        v2Clips.current = [...v2Clips.current, clip].sort(
          (left, right) => Number(left.start_frame) - Number(right.start_frame),
        );
        selection = [];
        undoCursor = v2Cursor(revision);
      } else if (command.kind === "remove_clip") {
        const clipId = String(command.payload.clip_id);
        v2Undo.current.push(current);
        v2Redo.current = [];
        v2Clips.current = v2Clips.current.filter(
          (clip) => clip.clip_id !== clipId,
        );
        undoCursor = v2Cursor(revision);
      } else if (command.kind === "undo") {
        const previous = v2Undo.current.pop();
        if (previous === undefined)
          throw new Error("V2 fixture has no undo entry");
        v2Redo.current.push(current);
        v2Clips.current = [...previous.clips];
        selection = previous.selection;
        undoCursor = previous.undoCursor;
        redoCursor = current.undoCursor;
      } else if (command.kind === "redo") {
        const next = v2Redo.current.pop();
        if (next === undefined) throw new Error("V2 fixture has no redo entry");
        v2Undo.current.push(current);
        v2Clips.current = [...next.clips];
        selection = next.selection;
        undoCursor = v2Cursor(revision);
      } else if (command.kind === "select_clips") {
        selection = command.payload.clip_ids as readonly string[];
        undoCursor = history.undoCursor;
        redoCursor = history.redoCursor;
      } else {
        throw new Error(`V2 fixture does not admit ${command.kind}`);
      }
      const nextHistory = encodeV2History({
        base,
        clips: v2Clips.current,
        selection,
        workspaceRevision: history.authoring.workspaceRevision + 1,
        timelineRevision: revision,
        undoCursor,
        redoCursor,
      });
      const projection =
        "projection" in authoring ? authoring.projection : undefined;
      if (projection === undefined)
        throw new Error("V2 fixture authoring projection is absent");
      setAuthoring({
        status: "ready",
        projection,
        timelineHistory:
          "timelineHistory" in authoring
            ? authoring.timelineHistory
            : undefined,
        timelineHistoryV2: nextHistory,
      });
      record.receipts += 1;
      record.handlerDurationsMs.push(performance.now() - started);
      return;
    }
    if (canonical) {
      if (
        !("timelineHistory" in authoring) ||
        authoring.timelineHistory === undefined
      )
        throw new Error("fixture history absent");
      const base = authoring.timelineHistory.snapshot;
      const requestId = `canonical-${++receiptCounter}`;
      const transaction = encodeTimelineTransaction({
        requestId,
        transactionId: `tx-${requestId}`,
        workspaceHandle: base.workspaceHandle,
        expectedWorkspaceRevision: base.workspaceRevision,
        expectedTimelineRevision: base.timelineRevision,
        expectedTimelineFingerprint: base.timelineFingerprint,
        commands,
      });
      setAuthoring(
        (current) => ({ ...current, status: "pending" }) as AuthoringViewState,
      );
      record.handlerDurationsMs.push(performance.now() - started);
      const response = await fetch("/__nle_fixture/transaction", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(transaction),
      });
      const result = await response.json();
      const history = decodeTimelineHistoryProjection(result.history);
      const receipt =
        result.receipt === null
          ? undefined
          : decodeTimelineReceipt(result.receipt);
      if (receipt !== undefined) record.receipts += 1;
      if (
        loseMoveReply &&
        commands.some(
          (command) =>
            command.kind === "move_clip" || command.kind === "move_group",
        )
      ) {
        // M25-46: the backend accepted the transaction but the caller lost that reply. Publish the
        // uncertain outcome first, then only the read-only history result; never retry the edit.
        setAuthoring(
          (current) =>
            ({
              ...current,
              status: "error",
              reason: "outcome_unknown",
            }) as AuthoringViewState,
        );
        await new Promise((resolve) => setTimeout(resolve, 150));
      }
      if (receipt !== undefined && historyDelayMs > 0) {
        const generation = stateRef.current.surface.generation;
        setAuthoring((current) => {
          const { timelineHistory: _dropped, ...rest } =
            current as AuthoringViewState & { timelineHistory?: unknown };
          return {
            ...rest,
            status: "pending",
            lastTimelineReceipt: receipt,
          } as AuthoringViewState;
        });
        record.historyPending += 1;
        await new Promise((resolve) => setTimeout(resolve, historyDelayMs));
        record.historyPending -= 1;
        // A refresh that lands after the view closed updates only the compact authoring
        // state, as the session does; it must not re-enter the overlay.
        record.lateHistoryAfterClose +=
          stateRef.current.surface.generation !== generation ||
          stateRef.current.surface.status !== "expanded"
            ? 1
            : 0;
      }
      setAuthoring(
        (current) =>
          ({
            ...current,
            status: result.status === 200 ? "ready" : "conflict",
            timelineHistory: history,
            ...(receipt === undefined ? {} : { lastTimelineReceipt: receipt }),
          }) as AuthoringViewState,
      );
      return;
    }
    setAuthoring(
      (current) => ({ ...current, status: "pending" }) as AuthoringViewState,
    );
    await new Promise((resolve) => setTimeout(resolve, pendingMs));
    setAuthoring((current) => {
      record.receipts += 1;
      return {
        ...current,
        status: "ready",
        lastTimelineReceipt: receiptFor(current, commands),
      } as AuthoringViewState;
    });
    record.handlerDurationsMs.push(performance.now() - started);
  };

  const noop = async () => undefined;
  const binding: NleWorkspaceBinding = {
    locale: harnessLocale,
    state,
    authoring,
    production: { status: "absent" },
    contextAvailable: true,
    runtime,
    leaseClient,
    output,
    actions: {
      // Pure component harness; capture isolation is exercised through the integrated shell.
      acquireKeyboardGuard: () => ({
        status: "ready",
        value: { release() {} },
      }),
      mounted: (generation) => {
        record.mounted += 1;
        setState((current) =>
          current.surface.status === "opening" &&
          current.surface.generation === generation
            ? {
                ...current,
                surface: { ...current.surface, status: "expanded" },
                render: { status: "read", capability: null },
              }
            : current,
        );
      },
      mountFailed: () => {
        record.mountFailed += 1;
        patchSurface({
          status: "compact_unsupported",
          lastCloseReason: "capability_or_mount_failure",
        });
      },
      close,
      released: (generation) => {
        record.released += 1;
        setState((current) =>
          current.surface.status === "closing" &&
          current.surface.generation === generation
            ? {
                ...current,
                surface: {
                  ...current.surface,
                  status: "compact_ready",
                  bounds: { width: 0, height: 0 },
                  pane: "assets",
                },
              }
            : current,
        );
      },
      resize: (requested) =>
        patchSurface({ bounds: clampOverlayBounds(viewport(), requested) }),
      selectPane: (pane) => patchSurface({ pane }),
      // M25-44: splitter positions are mount memory and survive close and reopen in this page.
      setLayout: (layout) => patchSurface({ layout }),
      startAuthoring: noop,
      timeline: timeline as NleWorkspaceBinding["actions"]["timeline"],
      clearImportHighlight: () => undefined,
      setTargetSeconds: () => undefined,
      setPolicy: () => undefined,
      prepareContext: noop,
      openStoryboardReview: () => undefined,
      setStoryboardRows: () => undefined,
      admitStoryboard: noop,
      propose: noop,
      approveAndImportPlan: noop,
      createPlannedProject: noop,
      requestReadiness: noop,
      sequenceStartable: () => false,
      startSequence: noop,
      detachSequence: noop,
      reattachSequence: noop,
      resumeSequence: noop,
      cancelSequence: noop,
      retrySegment: noop,
      refreshSequence: noop,
      recoveryPointerPresent: () => false,
      assembly: noop,
      refreshProduction: noop,
    },
  };

  window.nleWorkspaceHarness = {
    snapshot: () => ({
      shape: shape.name,
      runtimeStatus: runtime.status,
      surfaceStatus: state.surface.status,
      generation: state.surface.generation,
      closeReasons: [...record.closeReasons],
      mounted: record.mounted,
      mountFailed: record.mountFailed,
      released: record.released,
      intents: [...record.intents],
      receipts: record.receipts,
      commitDurationsMs: [...record.commitDurationsMs],
      commitStates: [...record.commitStates],
      handlerDurationsMs: [...record.handlerDurationsMs],
      bounds: state.surface.bounds,
      layout: state.surface.layout,
      mediaOwnership: mediaOwnership(),
      mediaPreparation: mediaPreparation(),
      parentCommits: record.parentCommits,
      historyPending: record.historyPending,
      lateHistoryAfterClose: record.lateHistoryAfterClose,
      authoringStateV2:
        "timelineHistoryV2" in authoring
          ? (authoring.timelineHistoryV2?.authoring ?? null)
          : null,
      timelineSnapshot:
        "timelineHistoryV2" in authoring &&
        authoring.timelineHistoryV2 !== undefined
          ? authoring.timelineHistoryV2.renderSnapshot
          : "timelineHistory" in authoring
            ? (authoring.timelineHistory?.snapshot ?? null)
            : null,
      authoringV2ContentEndExclusive:
        "timelineHistoryV2" in authoring &&
        authoring.timelineHistoryV2 !== undefined
          ? authoring.timelineHistoryV2.authoring.contentEndExclusive
          : null,
      authoringV2Selection:
        "timelineHistoryV2" in authoring &&
        authoring.timelineHistoryV2 !== undefined
          ? authoring.timelineHistoryV2.selection
          : null,
    }),
    open,
    replaceFirstMediaAssetFingerprint: () => {
      setAuthoring((current) => {
        if (
          !("timelineHistory" in current) ||
          current.timelineHistory === undefined
        )
          return current;
        const snapshot = current.timelineHistory.snapshot;
        const assetIndex = snapshot.assets.findIndex(
          (asset) => asset.kind === "video",
        );
        if (assetIndex < 0) return current;
        const assets = snapshot.assets.map((asset, index) =>
          index === assetIndex
            ? Object.freeze({
                ...asset,
                sourceFrameCount: (asset.sourceFrameCount ?? 1) + 1,
              })
            : asset,
        );
        return {
          ...current,
          timelineHistory: {
            ...current.timelineHistory,
            snapshot: Object.freeze({ ...snapshot, assets }),
          },
        } as AuthoringViewState;
      });
    },
    resetMeasurements: () => {
      record.commitDurationsMs.length = 0;
      record.commitStates.length = 0;
      record.handlerDurationsMs.length = 0;
    },
  };

  const onRender: ProfilerOnRenderCallback = (_id, _phase, actualDuration) => {
    record.commitDurationsMs.push(actualDuration);
    if (record.commitStates.length < 32)
      record.commitStates.push(
        `${_phase}:${document.querySelector('[data-h3-nle-status="timeline"]')?.textContent}:${document.querySelector('[data-h3-nle-status="monitor"]')?.textContent}:${document.querySelector('[data-h3-nle-status="audio"]')?.getAttribute("data-h3-nle-audio-state")}`,
      );
  };

  const live =
    state.surface.status === "opening" ||
    state.surface.status === "expanded" ||
    state.surface.status === "closing";

  return (
    <div className="h3c">
      <style>{tokenStyles}</style>
      <button
        type="button"
        data-h3-nle-entry="open"
        data-h3-focus-key="nle-open-overlay"
        onClick={open}
        disabled={runtime.status !== "available" || live}
      >
        Open full editor
      </button>
      <output data-h3-harness="surface">{state.surface.status}</output>
      {live ? (
        <Profiler id="nle-overlay" onRender={onRender}>
          <NleOverlay key={state.surface.generation} binding={binding} />
        </Profiler>
      ) : null}
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<Harness />);
