// M25-16 explicit Production-output import through the session: the target is ensured only on
// explicit intent, identities are captured exactly once, a success adopts the returned
// projection and focuses the compact clip editor, refusal classes are typed, an uncertain
// outcome retries the same request body, and an ineligible selection never sends.

import { afterEach, describe, expect, it, vi } from "vitest";

import type { AuthoringIntent } from "../src/state/authoringViewState";
import type { ProductionIntent } from "../src/components/ProductionWorkbench";
import { decodeProductionAuthoringImportResponse } from "../src/contracts/productionAuthoringImportCodec";
import type { ProductionAuthoringImportRequest } from "../src/contracts/productionAuthoringImportCodec";
import { decodeProductionWorkbenchProjection } from "../src/contracts/productionWorkbenchCodec";
import { ProductionAuthoringImportError } from "../src/host/productionAuthoringImportClient";
import { createAuthoringSession } from "../src/host/authoringSession";
import { createProductionSession } from "../src/host/productionSession";
import { createProductionDestinationStore } from "../src/host/productionDestination";
import { ProductionClientError } from "../src/host/productionActions";
import { NLE_OWNER_RENEWAL_INTERVAL_MS } from "../src/lifecycle/nleOwnerRenewal";
import { createNleWorkspaceSession } from "../src/lifecycle/nleWorkspaceSession";
import {
  createShellSession,
  type ShellRuntime,
} from "../src/lifecycle/shellSession";
import { projectionFixture } from "./support/authoringFixture";
import { SMOKE_SHAPE, authoringReady } from "./support/nleWorkspaceFixture";
import { unavailableProductionAssemblyWire } from "./support/productionAssemblyWire";
import {
  importResponseWire,
  importV2ResponseWire,
} from "./support/productionAuthoringImportFixture";

const FP = `sha256:${"a".repeat(64)}`;
const READY_HANDLE = `out_${"1".repeat(40)}`;
const PENDING_HANDLE = `out_${"2".repeat(40)}`;
const stopRenewals: Array<() => void> = [];

function segment(
  id: string,
  ordinal: number,
  ready: boolean,
  predecessor: string | null = null,
) {
  return {
    segment_id: id,
    ordinal,
    task_mode: "t2va",
    duration: {
      duration_milliseconds: 4167,
      delivered_milliseconds: 4458,
      frame_count: 107,
      snapped: true,
    },
    relation: ordinal === 1 ? "independent" : "predecessor",
    predecessor_segment_id: predecessor,
    boundary_kind: ordinal === 1 ? "independent" : "native_handoff",
    closure_state: "dirty_self",
    job_state: ready ? "succeeded" : "planned",
    artifact_state: ready ? "complete" : "unavailable",
    continuity_state: "unavailable",
    delivered_geometry: ready
      ? { format: "mp4", frame_count: 107, width: 864, height: 480 }
      : null,
  };
}

function productionProjection(
  options: {
    importable?: boolean;
    revision?: number;
    readyHandle?: string;
    selected?: readonly string[];
  } = {},
) {
  const importable = options.importable ?? true;
  return decodeProductionWorkbenchProjection({
    schema: "h3.context.production_workbench.projection.v1",
    workspace_handle: `pw_${"p".repeat(32)}`,
    workspace_id: "workspace.1",
    workspace_revision: options.revision ?? 7,
    workspace_fingerprint: FP,
    segments: [
      segment("segment.1", 1, true),
      segment("segment_2", 2, false, "segment.1"),
    ],
    selected_segment_ids: options.selected ?? ["segment.1"],
    run: { state: "ready", completed: 1, total: 2 },
    generation_sequence: {
      schema: "h3.context.generation_sequence_projection.v1",
      sequence_id: "sequence.1",
      sequence_fingerprint: `sha256:${"b".repeat(64)}`,
      state_fingerprint: `sha256:${"c".repeat(64)}`,
      workspace_id: "workspace.1",
      workspace_revision: options.revision ?? 7,
      workspace_fingerprint: FP,
      correlation: { prompt_id: "prompt.1", execution_node_id: "node.1" },
    },
    reconstruction: { state: "unavailable" },
    assembly: unavailableProductionAssemblyWire(),
    authority_versions: ["h3.context.generation_sequence_projection.v1"],
    outputs: [
      {
        output_handle: options.readyHandle ?? READY_HANDLE,
        ordinal: 1,
        state: "ready",
        segment_id: "segment.1",
        preview: false,
      },
      {
        output_handle: PENDING_HANDLE,
        ordinal: 2,
        state: "pending",
        segment_id: "segment_2",
        preview: false,
      },
    ],
    allowed_actions: [
      "add_segment_from_context",
      "set_selection",
      "read_projection",
      "release_workspace",
      ...(importable ? ["import_production_outputs_to_authoring"] : []),
    ],
    blocker_codes: [],
    limits: { max_segments: 64, max_outputs: 65 },
  });
}

function subject(
  options: {
    importable?: boolean;
    authoringPresent?: boolean;
    send?: ReturnType<typeof vi.fn>;
    historyRefreshFails?: boolean;
    historyRefreshFailsOnce?: boolean;
    onProductionRead?: (session: ReturnType<typeof createShellSession>) => void;
  } = {},
) {
  const session = createShellSession();
  session.container = document.createElement("div");
  session.productionState = {
    status: "ready",
    projection: productionProjection({ importable: options.importable }),
  };
  session.productionContextBinding = {
    productionWorkspaceHandle:
      session.productionState.projection.workspaceHandle,
    productionWorkspaceId: session.productionState.projection.workspaceId,
    contextWorkspaceHandle: `ws_${"a".repeat(32)}`,
  };
  const ready = authoringReady(SMOKE_SHAPE);
  if (options.authoringPresent !== false) session.authoringState = ready;
  if (!("projection" in ready))
    throw new Error("ready Authoring fixture is incomplete");
  const authoringSend = vi.fn(async () => ({
    status: 200 as const,
    projection: ready.projection,
  }));
  const send =
    options.send ??
    vi.fn(async () =>
      decodeProductionAuthoringImportResponse(importResponseWire()),
    );
  let historyReads = 0;
  const runAuthoringIntent = vi.fn(async (intent: AuthoringIntent) => {
    if (intent.action === "create_authoring_workspace")
      session.authoringState = {
        status: "ready",
        projection: projectionFixture({
          workspace_handle: `authoring-${"a".repeat(32)}`,
        }),
      };
    if (intent.action === "read_timeline_history") historyReads += 1;
    if (
      intent.action === "read_timeline_history" &&
      options.historyRefreshFails === true &&
      historyReads > 1
    )
      throw new Error("history refresh transport failure");
    if (
      intent.action === "read_timeline_history" &&
      options.historyRefreshFailsOnce === true &&
      historyReads === 2
    )
      throw new Error("one lost history refresh");
    if (
      intent.action === "read_timeline_history" ||
      intent.action === "initialize_timeline_history"
    ) {
      if (ready.status !== "ready" || ready.timelineHistory === undefined)
        throw new Error("ready Authoring history is incomplete");
      const history = ready.timelineHistory;
      const importedHistory =
        session.nleWorkspace.import.status === "succeeded"
          ? {
              ...history,
              snapshot: {
                ...history.snapshot,
                assets: [
                  ...history.snapshot.assets,
                  {
                    ...history.snapshot.assets[0]!,
                    assetId: "generated.asset.1",
                  },
                ],
              },
            }
          : history;
      session.authoringState = {
        ...session.authoringState,
        status: "ready",
        timelineHistory: importedHistory,
      } as typeof ready;
    }
  });
  const runProductionIntent = vi.fn(async (intent: ProductionIntent) => {
    if (intent.action === "read_projection")
      options.onProductionRead?.(session);
  });
  const selectPage = vi.fn();
  const runtime = {
    session,
    deps: {
      productionAuthoringImportClient: { send },
      authoringActions: { send: authoringSend },
      authoringOutputCapabilityClient: { read: vi.fn() },
    },
    actions: {
      renderCurrent: vi.fn(),
      selectPage,
      runAuthoringIntent,
      runProductionIntent,
    },
  } as unknown as ShellRuntime;
  const nle = createNleWorkspaceSession(runtime);
  return {
    nle,
    session,
    send,
    authoringSend,
    runAuthoringIntent,
    runProductionIntent,
    selectPage,
  };
}

afterEach(() => {
  stopRenewals.splice(0).forEach((stop) => stop());
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("Production-bound Authoring creation", () => {
  async function originSubject() {
    const originalContext = `ws_${"a".repeat(32)}`;
    const childContext = `ws_${"b".repeat(32)}`;
    const session = createShellSession();
    session.container = document.createElement("div");
    const observeContext = (workspace_id: string) => {
      session.workspaceState = {
        status: "ready",
        projection: { workspace_id },
      } as typeof session.workspaceState;
    };
    observeContext(originalContext);
    const production = productionProjection();
    const ready = authoringReady(SMOKE_SHAPE);
    if (!("projection" in ready) || !("timelineHistory" in ready))
      throw new Error("ready Authoring fixture is incomplete");
    const authoringSend = vi.fn(async (_id: string, action: string) =>
      action === "create_authoring_workspace" ||
      action === "ensure_authoring_from_production"
        ? { status: 201, projection: ready.projection }
        : { status: 200, history: ready.timelineHistory },
    );
    const importSend = vi.fn(async () =>
      decodeProductionAuthoringImportResponse(importResponseWire()),
    );
    const productionSend = vi.fn(async (_id: string, action: string) => ({
      status: action === "create_workspace_from_context" ? 201 : 200,
      projection: production,
    }));
    const readAccumulated = vi.fn(async () => ({
      schema: "h3.context.production_accumulated_project.v1" as const,
      workspaceHandle: production.workspaceHandle,
      workspaceId: production.workspaceId,
      projectRevision: 1,
      projectFingerprint: FP,
      workspace: production,
      attempts: [],
    }));
    const workflow = {};
    const productionDestinations = createProductionDestinationStore();
    const ctx = {
      session,
      deps: {
        appModeController: { activeWorkflow: () => workflow },
        productionDestinations,
        productionActions: { send: productionSend, readAccumulated },
        authoringActions: { send: authoringSend },
        productionAuthoringImportClient: { send: importSend },
        authoringOutputCapabilityClient: { read: vi.fn() },
        productionProposalDispatcher: {
          captureCurrent: vi.fn(),
          bind: vi.fn(),
          refreshProjection: vi.fn(),
          advanceProjection: vi.fn(),
          resetAll: vi.fn(),
        },
        pageRegistry: { getSnapshot: () => ({ selected: "production" }) },
      },
      actions: {
        renderCurrent: vi.fn(),
        selectPage: vi.fn(),
        currentAppModeRun: () => 1,
      },
    } as unknown as ShellRuntime;
    Object.assign(
      ctx.actions,
      createAuthoringSession(ctx),
      createProductionSession(ctx),
    );
    const nle = createNleWorkspaceSession(ctx);
    Object.assign(ctx.actions, nle);
    stopRenewals.push(nle.nleStopOwnerRenewal);
    await ctx.actions.runProductionIntent({
      action: "create_workspace_from_context",
    });
    expect(session.productionState.status).toBe("ready");
    observeContext(childContext);
    return {
      ctx,
      session,
      nle,
      authoringSend,
      importSend,
      productionSend,
      readAccumulated,
      production,
      ready,
      originalContext,
      childContext,
      observeContext,
      workflow,
      productionDestinations,
    };
  }

  it("binds an accepted Production project to the active workflow and ensures that exact owner", async () => {
    const {
      ctx,
      session,
      production,
      productionSend,
      readAccumulated,
      workflow,
      productionDestinations,
    } = await originSubject();
    expect(productionDestinations.peek(workflow)).toMatchObject({
      kind: "project",
      workspaceHandle: production.workspaceHandle,
      workspaceId: production.workspaceId,
    });

    session.productionState = { status: "absent" };
    session.productionSessionHandle = undefined;
    ctx.actions.ensureProductionWorkspace();
    await Promise.resolve();
    expect(readAccumulated).toHaveBeenLastCalledWith(expect.any(String), {
      workspaceHandle: production.workspaceHandle,
      workspaceId: production.workspaceId,
    });
  });

  it("renews a visible owner by touch reads that never mark it pending or replace its projection", async () => {
    vi.useFakeTimers();
    const {
      ctx,
      session,
      nle,
      productionSend,
      authoringSend,
      production,
      ready,
    } = await originSubject();
    session.authoringState = ready;
    const productionBefore = session.productionState;
    productionSend.mockClear();
    authoringSend.mockClear();

    // The create reply just refreshed this project's inactivity clock: navigation reads nothing.
    nle.nleSyncOwnerRenewal(true);
    await vi.advanceTimersByTimeAsync(0);
    expect(productionSend).not.toHaveBeenCalled();
    expect(authoringSend.mock.calls.map((call) => call[1])).toEqual([
      "read_projection",
    ]);
    const authoringBefore = session.authoringState;

    let finishRead: (() => void) | undefined;
    productionSend.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishRead = () =>
            resolve({
              status: 200,
              projection: {
                ...production,
                workspaceRevision: production.workspaceRevision + 1,
              },
            });
        }),
    );
    await vi.advanceTimersByTimeAsync(NLE_OWNER_RENEWAL_INTERVAL_MS);
    expect(productionSend.mock.calls.map((call) => call[1])).toEqual([
      "read_projection",
    ]);
    expect(finishRead).toBeDefined();
    // While the renewal read is in flight the user's own actions must still be admitted.
    expect(session.productionState).toBe(productionBefore);
    expect(session.authoringState).toBe(authoringBefore);
    await ctx.actions.runProductionIntent({
      action: "set_selection",
      segmentIds: ["segment.1"],
    });
    expect(productionSend.mock.calls.map((call) => call[1])).toEqual([
      "read_projection",
      "set_selection",
    ]);
    finishRead!();
    await vi.advanceTimersByTimeAsync(0);
    expect(session.productionState.status).toBe("ready");
  });

  it("ensures the import target from the original Production owner after a child projection", async () => {
    const { ctx, nle, authoringSend, importSend, productionSend, production } =
      await originSubject();
    productionSend.mockResolvedValueOnce({
      status: 200,
      projection: {
        ...production,
        workspaceRevision: production.workspaceRevision + 1,
        workspaceFingerprint: `sha256:${"d".repeat(64)}`,
      },
    });
    await ctx.actions.runProductionIntent({ action: "read_projection" });
    await nle.nleImportSelectedOutputs(["segment.1"]);
    expect(authoringSend).toHaveBeenCalledWith(
      expect.any(String),
      "ensure_authoring_from_production",
      {
        production_workspace_handle: production.workspaceHandle,
        production_workspace_id: production.workspaceId,
        preferred_authoring_handle: null,
      },
    );
    expect(importSend).toHaveBeenCalledTimes(1);
  });

  it.each(["missing", "foreign_handle", "foreign_id"])(
    "uses the exact live Production owner despite a %s browser origin association",
    async (kind) => {
      const { session, nle, authoringSend, importSend, production } =
        await originSubject();
      const origin = session.productionContextBinding!;
      session.productionContextBinding =
        kind === "missing"
          ? undefined
          : {
              ...origin,
              ...(kind === "foreign_handle"
                ? { productionWorkspaceHandle: `pw_${"z".repeat(32)}` }
                : { productionWorkspaceId: "workspace.foreign" }),
            };
      await nle.nleImportSelectedOutputs(["segment.1"]);
      expect(authoringSend).toHaveBeenCalledWith(
        expect.any(String),
        "ensure_authoring_from_production",
        {
          production_workspace_handle: production.workspaceHandle,
          production_workspace_id: production.workspaceId,
          preferred_authoring_handle: null,
        },
      );
      expect(importSend).toHaveBeenCalledTimes(1);
    },
  );

  it("preserves an existing target instead of creating or releasing it from another Context", async () => {
    const { session, nle, ready, authoringSend, importSend } =
      await originSubject();
    session.authoringState = ready;
    session.productionContextBinding = undefined;
    importSend.mockRejectedValueOnce(
      new ProductionAuthoringImportError("conflict_or_replay", 409, false),
    );
    await nle.nleImportSelectedOutputs(["segment.1"]);
    expect(session.authoringState).toEqual(ready);
    expect(authoringSend).toHaveBeenCalledWith(
      expect.any(String),
      "ensure_authoring_from_production",
      expect.objectContaining({
        preferred_authoring_handle:
          "projection" in ready ? ready.projection?.workspaceHandle : undefined,
      }),
    );
    expect(session.nleWorkspace.import.refusal).toBe("conflict_or_replay");
  });

  it("keeps standalone Authoring creation on the current Context", async () => {
    const { ctx, authoringSend, childContext } = await originSubject();
    await ctx.actions.runAuthoringIntent({
      action: "create_authoring_workspace",
    });
    expect(authoringSend).toHaveBeenCalledWith(
      expect.any(String),
      "create_authoring_workspace",
      { context_workspace_handle: childContext },
    );
  });

  it("resolves a released browser target through the Production owner", async () => {
    const { session, nle, authoringSend, production } = await originSubject();
    session.authoringState = { status: "released" };
    await nle.nleImportSelectedOutputs(["segment.1"]);
    expect(authoringSend).toHaveBeenCalledWith(
      expect.any(String),
      "ensure_authoring_from_production",
      {
        production_workspace_handle: production.workspaceHandle,
        production_workspace_id: production.workspaceId,
        preferred_authoring_handle: null,
      },
    );
  });

  it.each(["release", "gone", "foreign_owner"])(
    "clears the origin on %s and only rebinds an accepted create",
    async (kind) => {
      const { ctx, session, productionSend, production, childContext } =
        await originSubject();
      if (kind === "release")
        productionSend.mockResolvedValueOnce({
          status: 204,
          projection: production,
        });
      else if (kind === "gone")
        productionSend.mockRejectedValueOnce(
          new ProductionClientError("workspace_gone", 410),
        );
      else
        productionSend.mockResolvedValueOnce({
          status: 200,
          projection: {
            ...production,
            workspaceHandle: `pw_${"z".repeat(32)}`,
            workspaceId: "workspace.foreign",
          },
        });
      await ctx.actions.runProductionIntent({
        action: kind === "release" ? "release_workspace" : "read_projection",
      });
      expect(session.productionContextBinding).toBeUndefined();
      if (kind === "release" || kind === "gone")
        productionSend.mockResolvedValueOnce({
          status: 201,
          projection: {
            ...production,
            workspaceHandle: `pw_${"n".repeat(32)}`,
            workspaceId: "workspace.new",
          },
        });
      await ctx.actions.runProductionIntent({
        action: "create_workspace_from_context",
      });
      expect(session.productionContextBinding?.contextWorkspaceHandle).toBe(
        childContext,
      );
    },
  );
});

describe("M25-16 explicit Production import", () => {
  it("offers the compact action only for an ordered, ready, importable selection", () => {
    const { nle } = subject();
    expect(nle.nleImportSelectionState(["segment.1"])).toEqual({
      eligible: true,
      reason: null,
      busy: false,
    });
    expect(nle.nleImportSelectionState([]).reason).toBe("invalid_request");
    expect(nle.nleImportSelectionState(["segment.1", "nope"]).reason).toBe(
      "invalid_request",
    );
    expect(nle.nleImportSelectionState(["segment_2"]).reason).toBe(
      "ineligible_or_unsupported",
    );
    expect(
      subject({ importable: false }).nle.nleImportSelectionState(["segment.1"])
        .reason,
    ).toBe("ineligible_or_unsupported");
  });

  it("captures identities once, adopts the response, refreshes and focuses the clip editor", async () => {
    const {
      nle,
      session,
      send,
      runAuthoringIntent,
      runProductionIntent,
      selectPage,
    } = subject();
    await nle.nleImportSelectedOutputs(["segment.1"]);
    expect(send).toHaveBeenCalledTimes(1);
    const [request, signal] = send.mock.calls[0] as [
      Record<string, unknown>,
      AbortSignal,
    ];
    expect(signal).toBeInstanceOf(AbortSignal);
    expect(request.productionWorkspaceHandle).toBe(`pw_${"p".repeat(32)}`);
    expect(request.productionWorkspaceId).toBe("workspace.1");
    expect(request.expectedProductionWorkspaceRevision).toBe(7);
    expect(request.expectedProductionWorkspaceFingerprint).toBe(FP);
    expect(request.authoringWorkspaceHandle).toBe(
      `authoring-${"a".repeat(32)}`,
    );
    expect(request.expectedNleWorkspaceRevision).toBe(
      (
        session.authoringState as {
          timelineHistory: { snapshot: { workspaceRevision: number } };
        }
      ).timelineHistory.snapshot.workspaceRevision,
    );
    expect(request.entries).toEqual([
      { segmentId: "segment.1", outputHandle: READY_HANDLE },
    ]);
    expect(String(request.requestId)).toMatch(/^import\.[0-9a-f]{16}\.\d+$/u);
    const state = session.nleWorkspace.import;
    expect(state.status).toBe("succeeded");
    expect(state.requestId).toBe(request.requestId);
    expect(state.refusal).toBeNull();
    expect(state.highlightedAssetIds).toEqual(["generated.asset.1"]);
    expect(state.receipt?.rows[0]?.assetId).toBe("generated.asset.1");
    // The returned projection is adopted and the history retained, then both are refreshed.
    expect(
      (session.authoringState as { projection: { workspaceHandle: string } })
        .projection.workspaceHandle,
    ).toBe(`authoring-${"a".repeat(32)}`);
    expect(runAuthoringIntent).toHaveBeenCalledWith({
      action: "read_timeline_history",
    });
    expect(runProductionIntent).toHaveBeenCalledWith({
      action: "read_projection",
    });
    // The compact editor is brought forward; the overlay is not opened.
    expect(selectPage).toHaveBeenCalledWith("production");
    expect(session.nleWorkspace.functionRequest).toEqual({
      id: "clip_editor",
      generation: 1,
    });
    expect(session.nleWorkspace.surface.status).toBe("compact_ready");
    // The highlight clears on explicit request only.
    nle.nleClearImportHighlight();
    expect(session.nleWorkspace.import.highlightedAssetIds).toEqual([]);
    expect(session.nleWorkspace.import.status).toBe("succeeded");
    expect(session.nleWorkspace.import.editorStatus).toBe("ready");
  });

  it("keeps a succeeded import succeeded when the follow-up history refresh fails and never resends", async () => {
    // Section 6 row 5: the import response is the accepted outcome. A failure of the bounded
    // history read that follows is an authoring-state matter; it must not turn the import
    // uncertain, and it must never make an explicit retry resend the request.
    const { nle, session, send, runAuthoringIntent } = subject({
      historyRefreshFails: true,
    });
    await nle.nleImportSelectedOutputs(["segment.1"]);
    expect(send).toHaveBeenCalledTimes(1);
    expect(runAuthoringIntent).toHaveBeenCalledWith({
      action: "read_timeline_history",
    });
    expect(session.nleWorkspace.import.status).toBe("succeeded");
    expect(session.nleWorkspace.import.editorStatus).toBe("needs_action");
    expect(session.nleWorkspace.import.receipt?.rows[0]?.assetId).toBe(
      "generated.asset.1",
    );
    expect(session.nleWorkspace.import.highlightedAssetIds).toEqual([
      "generated.asset.1",
    ]);
    await nle.nleRetryImport();
    expect(send).toHaveBeenCalledTimes(1);
    await nle.nleOpenImportedEditor();
    expect(send).toHaveBeenCalledTimes(1);
  });
  it("opens the accepted assets after a later history read without posting import again", async () => {
    const { nle, session, send, selectPage } = subject({
      historyRefreshFailsOnce: true,
    });
    await nle.nleImportSelectedOutputs(["segment.1"]);
    expect(session.nleWorkspace.import.status).toBe("succeeded");
    expect(session.nleWorkspace.import.editorStatus).toBe("needs_action");
    expect(selectPage).not.toHaveBeenCalled();
    await nle.nleOpenImportedEditor();
    expect(session.nleWorkspace.import.editorStatus).toBe("ready");
    expect(selectPage).toHaveBeenCalledWith("production");
    expect(send).toHaveBeenCalledTimes(1);
  });
  it("ensures the Authoring target only on explicit intent and never seeds from a Context claim", async () => {
    const { nle, send, runAuthoringIntent } = subject({
      authoringPresent: false,
    });
    expect(runAuthoringIntent).not.toHaveBeenCalled();
    await nle.nleImportSelectedOutputs(["segment.1"]);
    expect(
      runAuthoringIntent.mock.calls.map(
        (call) => (call[0] as AuthoringIntent).action,
      ),
    ).toEqual(["initialize_timeline_history", "read_timeline_history"]);
    expect(send).toHaveBeenCalledTimes(1);
  });

  it("refuses with target_unavailable when the target cannot be established and sends nothing", async () => {
    const { nle, session, send, runAuthoringIntent } = subject({
      authoringPresent: false,
    });
    runAuthoringIntent.mockImplementation(async () => undefined);
    await nle.nleImportSelectedOutputs(["segment.1"]);
    expect(send).not.toHaveBeenCalled();
    expect(session.nleWorkspace.import.status).toBe("refused");
    expect(session.nleWorkspace.import.refusal).toBe("target_unavailable");
  });

  it("does not adopt a late target after the selected Production owner changes", async () => {
    const { nle, session, send, authoringSend } = subject({
      authoringPresent: false,
    });
    const ready = authoringReady(SMOKE_SHAPE);
    if (!("projection" in ready) || ready.projection === undefined)
      throw new Error("Authoring fixture is incomplete");
    const target = ready.projection;
    let release!: () => void;
    authoringSend.mockImplementationOnce(async () => {
      await new Promise<void>((done) => (release = done));
      return { status: 200 as const, projection: target };
    });
    const pending = nle.nleImportSelectedOutputs(["segment.1"]);
    const before = session.authoringState;
    const current = session.productionState;
    if (current.status !== "ready")
      throw new Error("Production fixture is incomplete");
    session.productionState = {
      status: "ready",
      projection: {
        ...current.projection,
        workspaceHandle: `pw_${"z".repeat(32)}`,
      },
    };
    release();
    await pending;
    expect(session.authoringState).toBe(before);
    expect(send).not.toHaveBeenCalled();
  });

  it("refuses an ineligible or aggregate selection without sending", async () => {
    const { nle, session, send } = subject();
    await nle.nleImportSelectedOutputs(["segment_2"]);
    expect(send).not.toHaveBeenCalled();
    expect(session.nleWorkspace.import).toMatchObject({
      status: "refused",
      refusal: "ineligible_or_unsupported",
      request: null,
    });
    await nle.nleImportSelectedOutputs([
      "segment.1",
      "segment.1",
      "segment.1",
      "segment.1",
    ]);
    expect(send).not.toHaveBeenCalled();
    expect(session.nleWorkspace.import.refusal).toBe("invalid_request");
  });

  it.each([
    ["conflict_or_replay", 409, false, true],
    ["workspace_unavailable", 404, false, true],
    ["ineligible_or_unsupported", 422, false, false],
    ["request_too_large", 413, false, false],
  ] as const)(
    "maps a typed backend refusal (%s) without adopting anything",
    async (code, status, _unknown, rereads) => {
      const send = vi.fn(async () => {
        throw new ProductionAuthoringImportError(code, status, false);
      });
      const { nle, session, runProductionIntent, selectPage } = subject({
        send,
      });
      const before = session.authoringState;
      await nle.nleImportSelectedOutputs(["segment.1"]);
      const state = session.nleWorkspace.import;
      expect(state.status).toBe("refused");
      expect(state.refusal).toBe(code);
      expect(state.receipt).toBeNull();
      expect(state.highlightedAssetIds).toEqual([]);
      expect(session.authoringState).toEqual(before);
      expect(selectPage).not.toHaveBeenCalled();
      expect(
        runProductionIntent.mock.calls.some(
          (call) => (call[0] as ProductionIntent).action === "read_projection",
        ),
      ).toBe(rereads);
      // A refused request is not retried.
      await nle.nleRetryImport();
      expect(send).toHaveBeenCalledTimes(1);
    },
  );

  it("recaptures once after a known stale revision with the exact output and editor", async () => {
    let attempts = 0;
    const send = vi.fn(async () => {
      attempts += 1;
      if (attempts === 1)
        throw new ProductionAuthoringImportError(
          "conflict_or_replay",
          409,
          false,
        );
      return decodeProductionAuthoringImportResponse(importResponseWire());
    });
    const { nle, session, authoringSend } = subject({
      send,
      onProductionRead: (current) => {
        current.productionState = {
          status: "ready",
          projection: productionProjection({ revision: 8 }),
        };
      },
    });
    await nle.nleImportSelectedOutputs(["segment.1"]);
    expect(send).toHaveBeenCalledTimes(2);
    const calls = send.mock.calls as unknown as [
      ProductionAuthoringImportRequest,
    ][];
    const first = calls[0]![0];
    const second = calls[1]![0];
    expect(second.requestId).not.toBe(first.requestId);
    expect(second.expectedProductionWorkspaceRevision).toBe(8);
    expect(second.entries).toEqual(first.entries);
    expect(second.authoringWorkspaceHandle).toBe(
      first.authoringWorkspaceHandle,
    );
    expect(authoringSend).toHaveBeenCalledTimes(2);
    expect(session.nleWorkspace.import.status).toBe("succeeded");
  });

  it("imports against V2 authoring CAS and retains the returned catalog-only history", async () => {
    const accepted = decodeProductionAuthoringImportResponse(
      importV2ResponseWire(),
    );
    if (
      accepted.schema !== "h3.context.production_authoring_import.response.v2"
    )
      throw new Error("expected V2 import response");
    const send = vi.fn(async () => accepted);
    const { nle, session, authoringSend } = subject({ send });
    const priorProjection = {
      ...accepted.authoringProjection,
      reference: {
        ...accepted.authoringProjection.reference,
        revision: 3,
      },
    };
    const priorHistory = {
      ...accepted.historyProjection,
      authoring: {
        ...accepted.historyProjection.authoring,
        workspaceRevision: 3,
        workspaceFingerprint: FP,
        authoringFingerprint:
          accepted.receipt.nleAuthoring.priorAuthoringFingerprint,
        assets: [],
      },
    };
    authoringSend.mockResolvedValue({
      status: 200,
      projection: priorProjection,
    });
    session.authoringState = {
      status: "ready",
      projection: priorProjection,
      timelineHistoryV2: priorHistory,
    };
    await nle.nleImportSelectedOutputs(["segment.1"]);
    const request = (
      send.mock.calls as unknown as [ProductionAuthoringImportRequest][]
    )[0]![0];
    expect(request).toMatchObject({
      expectedNleWorkspaceRevision: 3,
      expectedNleTimelineRevision: 2,
      expectedNleAuthoringFingerprint:
        accepted.receipt.nleAuthoring.priorAuthoringFingerprint,
      authoringSchema: "h3.context.nle_authoring_state.v1",
      profileId: "h3.authoring.nle_content_extent.v1",
    });
    expect(session.nleWorkspace.import.status).toBe("succeeded");
    expect(session.nleWorkspace.import.editorStatus).toBe("ready");
    expect(session.authoringState).toMatchObject({
      status: "ready",
      timelineHistoryV2: {
        renderSnapshot: null,
        authoring: {
          contentEndExclusive: 0,
          assets: [{ assetId: "generated.asset.1" }],
        },
      },
    });
  });

  it("does not substitute a changed output during known-refusal recovery", async () => {
    const send = vi.fn(async () => {
      throw new ProductionAuthoringImportError(
        "conflict_or_replay",
        409,
        false,
      );
    });
    const { nle, session, authoringSend } = subject({
      send,
      onProductionRead: (current) => {
        current.productionState = {
          status: "ready",
          projection: productionProjection({
            revision: 8,
            readyHandle: `out_${"9".repeat(40)}`,
          }),
        };
      },
    });
    await nle.nleImportSelectedOutputs(["segment.1"]);
    expect(send).toHaveBeenCalledTimes(1);
    expect(authoringSend).toHaveBeenCalledTimes(1);
    expect(session.nleWorkspace.import.status).toBe("refused");
  });

  it("does not resend during known-refusal recovery when a captured segment was deselected", async () => {
    // The import route admits only selected segments, so a recaptured request for a segment the
    // refreshed Production projection no longer selects is refused by construction.
    const send = vi.fn(async () => {
      throw new ProductionAuthoringImportError(
        "conflict_or_replay",
        409,
        false,
      );
    });
    const { nle, session, authoringSend } = subject({
      send,
      onProductionRead: (current) => {
        current.productionState = {
          status: "ready",
          projection: productionProjection({ revision: 8, selected: [] }),
        };
      },
    });
    await nle.nleImportSelectedOutputs(["segment.1"]);
    expect(send).toHaveBeenCalledTimes(1);
    expect(authoringSend).toHaveBeenCalledTimes(1);
    expect(session.nleWorkspace.import.status).toBe("refused");
    expect(session.nleWorkspace.import.refusal).toBe("conflict_or_replay");
  });

  it("keeps a lost response uncertain and retries the exact same request on explicit retry", async () => {
    let attempts = 0;
    const send = vi.fn(async () => {
      attempts += 1;
      if (attempts === 1)
        throw new ProductionAuthoringImportError(
          "transport_failure",
          null,
          true,
        );
      return decodeProductionAuthoringImportResponse(importResponseWire());
    });
    const { nle, session } = subject({ send });
    await nle.nleImportSelectedOutputs(["segment.1"]);
    expect(session.nleWorkspace.import.status).toBe("uncertain");
    expect(session.nleWorkspace.import.refusal).toBe("transport_failure");
    const calls = () => send.mock.calls as unknown as unknown[][];
    const first = calls()[0]![0];
    // An unknown outcome never auto-retries and does not offer a new import while uncertain.
    expect(nle.nleImportSelectionState(["segment.1"]).eligible).toBe(false);
    await nle.nleImportSelectedOutputs(["segment.1"]);
    expect(send).toHaveBeenCalledTimes(1);
    await nle.nleRetryImport();
    expect(send).toHaveBeenCalledTimes(2);
    expect(calls()[1]![0]).toBe(first);
    expect(session.nleWorkspace.import.status).toBe("succeeded");
    expect(session.nleWorkspace.import.requestId).toBe(
      (first as { requestId: string }).requestId,
    );
  });

  it("treats a non-typed failure as an uncertain transport failure", async () => {
    const send = vi.fn(async () => {
      throw new TypeError("fetch failed");
    });
    const { nle, session } = subject({ send });
    await nle.nleImportSelectedOutputs(["segment.1"]);
    expect(session.nleWorkspace.import.status).toBe("uncertain");
    expect(session.nleWorkspace.import.refusal).toBe("transport_failure");
  });

  it("ignores a second import while one is in flight", async () => {
    let release!: () => void;
    const send = vi.fn(
      () =>
        new Promise((done) => {
          release = () =>
            done(decodeProductionAuthoringImportResponse(importResponseWire()));
        }),
    );
    const { nle, session } = subject({ send });
    const first = nle.nleImportSelectedOutputs(["segment.1"]);
    await vi.waitFor(() => expect(send).toHaveBeenCalledTimes(1));
    expect(session.nleWorkspace.import.status).toBe("importing");
    expect(nle.nleImportSelectionState(["segment.1"]).busy).toBe(true);
    await nle.nleImportSelectedOutputs(["segment.1"]);
    expect(send).toHaveBeenCalledTimes(1);
    release();
    await first;
    expect(session.nleWorkspace.import.status).toBe("succeeded");
  });

  it("does not navigate back to the editor when import settles after later navigation", async () => {
    let respond!: () => void;
    const send = vi.fn(
      () =>
        new Promise((done) => {
          respond = () =>
            done(decodeProductionAuthoringImportResponse(importResponseWire()));
        }),
    );
    const { nle, session, selectPage } = subject({ send });
    const pending = nle.nleImportSelectedOutputs(["segment.1"]);
    await vi.waitFor(() => expect(send).toHaveBeenCalledTimes(1));
    nle.nleCloseOverlay("top_level_navigation");
    respond();
    await pending;
    expect(session.nleWorkspace.import.status).toBe("succeeded");
    expect(selectPage).not.toHaveBeenCalled();
  });

  it("keeps a late accepted result under its original project without adopting it into another", async () => {
    let respond!: () => void;
    const send = vi.fn(
      () =>
        new Promise((done) => {
          respond = () =>
            done(decodeProductionAuthoringImportResponse(importResponseWire()));
        }),
    );
    const { nle, session, selectPage } = subject({ send });
    const original = session.productionState;
    if (original.status !== "ready")
      throw new Error("Production fixture is incomplete");
    const pending = nle.nleImportSelectedOutputs(["segment.1"]);
    await vi.waitFor(() => expect(send).toHaveBeenCalledTimes(1));
    const authoringBefore = session.authoringState;
    session.productionState = {
      status: "ready",
      projection: {
        ...original.projection,
        workspaceHandle: `pw_${"q".repeat(32)}`,
        workspaceId: "workspace.2",
      },
    };
    respond();
    await pending;
    expect(session.nleWorkspace.import.status).toBe("succeeded");
    expect(session.authoringState).toBe(authoringBefore);
    expect(nle.nleImportStateForCurrentOwner().status).toBe("idle");
    expect(selectPage).not.toHaveBeenCalled();
    session.productionState = original;
    expect(nle.nleImportStateForCurrentOwner().status).toBe("succeeded");
    await nle.nleOpenImportedEditor();
    expect(send).toHaveBeenCalledTimes(1);
    expect(selectPage).toHaveBeenCalledWith("production");
  });

  it("restores the original editor association before opening a retained import", async () => {
    const { nle, session, send, authoringSend, selectPage } = subject();
    await nle.nleImportSelectedOutputs(["segment.1"]);
    const accepted = session.nleWorkspace.import;
    if (session.authoringState.status !== "ready")
      throw new Error("Authoring fixture is incomplete");
    session.authoringState = {
      ...session.authoringState,
      projection: {
        ...session.authoringState.projection,
        workspaceHandle: `authoring-${"b".repeat(32)}`,
      },
    };
    selectPage.mockClear();
    authoringSend.mockClear();
    await nle.nleOpenImportedEditor();
    expect(authoringSend).toHaveBeenCalledWith(
      expect.any(String),
      "ensure_authoring_from_production",
      expect.objectContaining({
        production_workspace_handle:
          accepted.request?.productionWorkspaceHandle,
        production_workspace_id: accepted.request?.productionWorkspaceId,
        preferred_authoring_handle: `authoring-${"b".repeat(32)}`,
      }),
    );
    expect(session.authoringState.status).toBe("ready");
    expect(send).toHaveBeenCalledTimes(1);
    expect(selectPage).toHaveBeenCalledWith("production");
  });

  it("retains a prior project's uncertain identity when another project is ineligible", async () => {
    const send = vi.fn(async () => {
      throw new ProductionAuthoringImportError("transport_failure", null, true);
    });
    const { nle, session } = subject({ send });
    const original = session.productionState;
    if (original.status !== "ready")
      throw new Error("Production fixture is incomplete");
    await nle.nleImportSelectedOutputs(["segment.1"]);
    const originalRequest = session.nleWorkspace.import.request;
    expect(session.nleWorkspace.import.status).toBe("uncertain");
    session.productionState = {
      status: "ready",
      projection: {
        ...productionProjection({ importable: false }),
        workspaceHandle: `pw_${"q".repeat(32)}`,
        workspaceId: "workspace.2",
      },
    };
    await nle.nleImportSelectedOutputs(["segment.1"]);
    expect(nle.nleImportStateForCurrentOwner().status).toBe("refused");
    session.productionState = original;
    expect(nle.nleImportStateForCurrentOwner().status).toBe("uncertain");
    expect(nle.nleImportStateForCurrentOwner().request).toBe(originalRequest);
    expect(send).toHaveBeenCalledTimes(1);
  });

  it("marks an import in flight at view destroy uncertain and drops the late result", async () => {
    let release!: () => void;
    const send = vi.fn(
      () =>
        new Promise((done) => {
          const respond = () =>
            done(decodeProductionAuthoringImportResponse(importResponseWire()));
          if (send.mock.calls.length > 1) respond();
          else release = respond;
        }),
    );
    const { nle, session, selectPage } = subject({ send });
    const pending = nle.nleImportSelectedOutputs(["segment.1"]);
    await vi.waitFor(() => expect(send).toHaveBeenCalledTimes(1));
    const calls = () => send.mock.calls as unknown as unknown[][];
    const signal = calls()[0]![1] as AbortSignal;
    expect(signal.aborted).toBe(false);
    nle.nleDisposeOverlay();
    expect(signal.aborted).toBe(true);
    expect(session.nleWorkspace.import.status).toBe("uncertain");
    expect(session.nleWorkspace.import.refusal).toBe("transport_failure");
    release();
    await pending;
    expect(session.nleWorkspace.import.status).toBe("uncertain");
    expect(session.nleWorkspace.import.request).not.toBeNull();
    expect(selectPage).not.toHaveBeenCalled();
    // The retained request replays with the same identity on explicit retry.
    await nle.nleRetryImport();
    expect(send).toHaveBeenCalledTimes(2);
    expect(calls()[1]![0]).toBe(calls()[0]![0]);
    expect(session.nleWorkspace.import.status).toBe("succeeded");
  });
});
