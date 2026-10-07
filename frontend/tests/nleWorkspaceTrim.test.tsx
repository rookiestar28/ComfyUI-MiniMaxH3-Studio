// M25-16 NLE-EDGE-TRIM-V1 at the component seam: the grip drives one accepted `trim_clip`
// transaction per pointer release or keyboard commit, cancels on Escape or a changed pixel
// mapping without sending anything, and settles from the accepted authoring state (accepted,
// rejected, lost response) rather than from the local draft.

import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AuthoringViewState } from "../src/state/authoringViewState";
import { NleWorkspace } from "../src/components/nle/NleWorkspace";
import type { NleWorkspaceBinding } from "../src/components/nle/nleWorkspaceBinding";
import {
  NLE_AUTHORING_PROFILE_ID,
  NLE_AUTHORING_SCHEMA,
  NLE_OPERATION_PROFILE_ID_V2,
  TIMELINE_HISTORY_PROJECTION_SCHEMA_V2,
  type NleAuthoringStateV2,
  type TimelineHistoryProjectionV2,
  type TimelineReceipt,
  type TimelineReceiptV2,
} from "../src/contracts/authoringWorkbenchCodec";
import {
  SMOKE_SHAPE,
  VIRTUALIZED_SHAPE,
  authoringReady,
  snapshotFixture,
  type FixtureShape,
} from "./support/nleWorkspaceFixture";
import {
  availableDisposition,
  bindingFixture,
  expandedState,
} from "./support/nleWorkspaceBinding";

const TRIM_STATUS = '[data-h3-nle-status="trim"]';
const MOVE_STATUS = '[data-h3-nle-status="move"]';

type ReadyAuthoring = AuthoringViewState & { status: "ready" };

type TimelineSpy = ReturnType<typeof vi.fn>;

function receiptFor(
  state: AuthoringViewState,
  requestId: string,
): TimelineReceipt {
  if (!("timelineHistory" in state) || state.timelineHistory === undefined)
    throw new Error("fixture needs a history");
  const snapshot = state.timelineHistory.snapshot;
  return Object.freeze({
    schema: "h3.context.timeline_receipt.v1",
    requestId,
    transactionId: `tx-${requestId}`,
    workspaceHandle: snapshot.workspaceHandle,
    beforeWorkspaceRevision: snapshot.workspaceRevision,
    afterWorkspaceRevision: snapshot.workspaceRevision + 1,
    beforeWorkspaceFingerprint: snapshot.workspaceFingerprint,
    afterWorkspaceFingerprint: snapshot.workspaceFingerprint,
    beforeTimelineRevision: snapshot.timelineRevision,
    afterTimelineRevision: snapshot.timelineRevision + 1,
    beforeTimelineFingerprint: snapshot.timelineFingerprint,
    afterTimelineFingerprint: snapshot.timelineFingerprint,
    commands: Object.freeze([]),
    affectedIds: Object.freeze([]),
    inverse: Object.freeze({
      kind: "restore_transaction_state",
      historyCursor: state.timelineHistory.undoCursor ?? "",
    }),
    historyCursor: state.timelineHistory.undoCursor ?? "",
    selection: Object.freeze([]),
    snapshot,
  }) as unknown as TimelineReceipt;
}

function subject(
  timeline?: TimelineSpy,
  onEdgeGestureActive: (active: boolean) => void = () => undefined,
  shape: FixtureShape = SMOKE_SHAPE,
  authoringFingerprint?: string,
) {
  const baseAuthoring = authoringReady(shape, {
    selection: ["clip-0"],
  });
  const baseSnapshot =
    "timelineHistory" in baseAuthoring
      ? baseAuthoring.timelineHistory?.snapshot
      : undefined;
  const authoring = (
    authoringFingerprint === undefined
      ? baseAuthoring
      : {
          ...baseAuthoring,
          timelineHistoryV2: Object.freeze({
            schema: TIMELINE_HISTORY_PROJECTION_SCHEMA_V2,
            workspaceHandle: baseSnapshot!.workspaceHandle,
            authoring: Object.freeze({
              schema: NLE_AUTHORING_SCHEMA,
              profileId: NLE_AUTHORING_PROFILE_ID,
              operationProfileId: NLE_OPERATION_PROFILE_ID_V2,
              projectId: baseSnapshot!.projectId,
              workspaceHandle: baseSnapshot!.workspaceHandle,
              workspaceRevision: baseSnapshot!.workspaceRevision,
              workspaceFingerprint: baseSnapshot!.workspaceFingerprint,
              timelineRevision: baseSnapshot!.timelineRevision,
              timelineFingerprint: baseSnapshot!.timelineFingerprint,
              authoringFingerprint,
              editCapacityFrames: 3_600,
              contentEndExclusive: Math.max(
                0,
                ...baseSnapshot!.clips.map(
                  (clip) => clip.startFrame + clip.durationFrames,
                ),
              ),
              assets: baseSnapshot!.assets,
              tracks: baseSnapshot!.tracks,
              clips: baseSnapshot!.clips,
              audioExtension: baseSnapshot!.audioExtension,
              blockers: baseSnapshot!.blockers,
            }) as NleAuthoringStateV2,
            renderSnapshot: baseSnapshot!,
            selection: ["clip-0"],
            undoCursor: null,
            redoCursor: null,
            rejection: null,
          }) as TimelineHistoryProjectionV2,
        }
  ) as ReadyAuthoring;
  const binding = bindingFixture({
    authoring,
    runtime: availableDisposition(),
    state: {
      ...expandedState({}, { width: 1280, height: 800 }),
      render: { status: "read", capability: null },
    },
  });
  if (timeline !== undefined)
    (binding.actions as { timeline: unknown }).timeline = timeline;
  const view = render(
    <NleWorkspace
      binding={binding}
      onEdgeGestureActive={onEdgeGestureActive}
    />,
  );
  const rerender = (next: Partial<NleWorkspaceBinding>) => {
    Object.assign(binding, next);
    view.rerender(
      <NleWorkspace
        binding={{ ...binding }}
        onEdgeGestureActive={onEdgeGestureActive}
      />,
    );
  };
  return { binding, view, rerender, authoring };
}

/**
 * M25-62 (R7): under a fine pointer a selected clip keeps inline grips down to 24 px, so the rail
 * is reached at Fit, where jsdom's 120 px lane puts every fixture clip at the 20 px floor.
 */
function fitTimeline(container: HTMLElement) {
  act(() => {
    fireEvent.click(
      container.querySelector('[data-h3-nle-control="transport.zoom_fit"]')!,
    );
  });
}

function grip(container: HTMLElement, clipId: string, edge: "start" | "end") {
  return container.querySelector<HTMLButtonElement>(
    `[data-h3-nle-trim-clip="${clipId}"] [data-h3-nle-trim-edge="${edge}"]:not([hidden])`,
  )!;
}

function phase(container: HTMLElement): string | null {
  return container
    .querySelector(TRIM_STATUS)!
    .getAttribute("data-h3-nle-trim-phase");
}

function movePhase(container: HTMLElement): string | null {
  return container
    .querySelector(MOVE_STATUS)!
    .getAttribute("data-h3-nle-move-phase");
}

function timelineCalls(spy: TimelineSpy) {
  return spy.mock.calls.map(
    (call) =>
      call[0] as {
        action: string;
        capturedTimeline?: { authoringFingerprint?: string };
        commands: {
          kind: string;
          payload: { clip_id: string; edge: string; delta_frames: number };
        }[];
      },
  );
}

beforeEach(() => {
  Element.prototype.setPointerCapture = vi.fn();
  Element.prototype.releasePointerCapture = vi.fn();
  Element.prototype.hasPointerCapture = vi.fn(() => true);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("M25-16 edge trim gesture at the workspace seam", () => {
  it("uses V2 edit capacity while retaining the shorter render extent", () => {
    const { view } = subject(
      undefined,
      undefined,
      SMOKE_SHAPE,
      `sha256:${"f".repeat(64)}`,
    );

    expect(
      view
        .getByRole("slider", { name: "Playhead" })
        .getAttribute("aria-valuemax"),
    ).toBe("2747");
  });

  const snapshot = snapshotFixture(SMOKE_SHAPE);
  const clip = snapshot.clips[0]!;

  it("renders a dedicated pair of grips only for the selected narrow clip", () => {
    const { view } = subject();
    fitTimeline(view.container);
    const grips = [
      ...view.container.querySelectorAll(
        '[data-h3-nle-control="clip.trim"]:not([hidden])',
      ),
    ];
    // M25-62 (R7): the rail's pair, plus the one inline grip a sub-24 px clip keeps.
    const rail = grips.filter((member) =>
      member.classList.contains("h3-nle-rail-grip"),
    );
    expect(rail).toHaveLength(2);
    for (const member of rail) {
      expect(member.tagName).toBe("BUTTON");
      expect(["start", "end"]).toContain(
        member.getAttribute("data-h3-nle-trim-edge"),
      );
    }
    const inline = grips.filter((member) => !rail.includes(member));
    expect(
      inline.map((member) => [
        member.closest("[data-h3-nle-clip]")?.getAttribute("data-h3-nle-clip"),
        member.getAttribute("data-h3-nle-trim-edge"),
      ]),
    ).toEqual([[clip.clipId, "end"]]);
  });

  it("cancels an uncommitted keyboard draft on focus loss for both grip layouts and never a submitted one", () => {
    // Post-closeout finding F3: the inline (wide-layout) grips must share the rail grips'
    // focus-loss cancellation, or Tab leaves a draft alive and the overlay's edge-first Escape
    // guard stays armed on an unfocused grip.
    const timeline = vi.fn(async () => undefined);
    const active: boolean[] = [];
    const { view } = subject(timeline, (value) => active.push(value));
    // Rail grips (narrow clip at Fit).
    fitTimeline(view.container);
    const rail = grip(view.container, clip.clipId, "end");
    expect(rail.classList.contains("h3-nle-rail-grip")).toBe(true);
    fireEvent.keyDown(rail, { key: "ArrowRight" });
    expect(phase(view.container)).toBe("keyboard_draft");
    expect(active.at(-1)).toBe(true);
    fireEvent.blur(rail);
    expect(phase(view.container)).toBe("idle");
    expect(active.at(-1)).toBe(false);
    // Zoom in until the selected clip is wide enough for both inline grips (R7: 24 px).
    const zoomIn = view.container.querySelector<HTMLButtonElement>(
      '[data-h3-nle-control="transport.zoom_in"]',
    )!;
    for (let click = 0; click < 4; click += 1) fireEvent.click(zoomIn);
    // Zooming from Fit anchors at the lane centre; scroll back to the clip at frame 0.
    fireEvent.change(
      view.container.querySelector('[data-h3-nle-control="transport.scroll"]')!,
      { target: { value: "0" } },
    );
    const inline = view.container.querySelector<HTMLButtonElement>(
      `[data-h3-nle-inline-grips="true"][data-h3-nle-trim-clip="${clip.clipId}"] [data-h3-nle-trim-edge="end"]`,
    );
    expect(inline).not.toBeNull();
    expect(inline!.hidden).toBe(false);
    fireEvent.keyDown(inline!, { key: "ArrowRight" });
    expect(phase(view.container)).toBe("keyboard_draft");
    expect(active.at(-1)).toBe(true);
    fireEvent.blur(inline!);
    expect(phase(view.container)).toBe("idle");
    expect(active.at(-1)).toBe(false);
    expect(timeline).not.toHaveBeenCalled();
    // A committed edit is not a draft: blur after Enter changes nothing and sends nothing more.
    fireEvent.keyDown(inline!, { key: "ArrowRight" });
    fireEvent.keyDown(inline!, { key: "Enter" });
    expect(phase(view.container)).toBe("submitting");
    fireEvent.blur(inline!);
    expect(phase(view.container)).toBe("submitting");
    expect(timeline).toHaveBeenCalledTimes(1);
  });

  it("keeps a pointer-captured move draft alive when its clip body blurs", () => {
    const timeline = vi.fn(async () => undefined);
    const { view } = subject(timeline);
    const body = view.container.querySelector<HTMLButtonElement>(
      `[data-h3-nle-clip="${clip.clipId}"] [data-h3-nle-control="selection.set"]`,
    )!;
    fireEvent.pointerDown(body, {
      button: 0,
      isPrimary: true,
      pointerId: 17,
      clientX: 100,
      clientY: 100,
    });
    expect(movePhase(view.container)).toBe("idle");
    // Chromium may move focus after pointerdown even though pointer capture remains authoritative.
    fireEvent.blur(body);
    expect(movePhase(view.container)).toBe("idle");
    fireEvent.pointerMove(body, {
      pointerId: 17,
      clientX: 172,
      clientY: 100,
      altKey: true,
    });
    expect(movePhase(view.container)).toBe("dragging");
    fireEvent.pointerUp(body, { pointerId: 17, clientX: 172, clientY: 100 });
    expect(movePhase(view.container)).toBe("submitting");
    expect(timeline).toHaveBeenCalledTimes(1);
  });

  it("submits exactly one trim_clip transaction from a pointer drag on the end edge", async () => {
    let resolveIntent!: () => void;
    const timeline = vi.fn(
      () =>
        new Promise<void>((done) => {
          resolveIntent = done;
        }),
    );
    const capturedFingerprint = `sha256:${"a".repeat(64)}`;
    const { view, rerender, authoring } = subject(
      timeline,
      undefined,
      SMOKE_SHAPE,
      capturedFingerprint,
    );
    const handle = grip(view.container, clip.clipId, "end");
    fireEvent.pointerDown(handle, {
      button: 0,
      isPrimary: true,
      pointerId: 7,
      clientX: 100,
    });
    expect(phase(view.container)).toBe("dragging");
    fireEvent.pointerMove(handle, { pointerId: 7, clientX: 104 });
    fireEvent.pointerMove(handle, { pointerId: 7, clientX: 110 });
    // Total displacement, not the sum of increments: 10 px at 1 px/frame is 10 frames.
    fireEvent.pointerUp(handle, { pointerId: 7, clientX: 110 });
    expect(phase(view.container)).toBe("submitting");
    const calls = timelineCalls(timeline);
    expect(calls).toHaveLength(1);
    expect(calls[0]!.action).toBe("apply_timeline_commands");
    expect(calls[0]!.capturedTimeline?.authoringFingerprint).toBe(
      capturedFingerprint,
    );
    expect(calls[0]!.commands).toEqual([
      {
        kind: "trim_clip",
        payload: { clip_id: clip.clipId, edge: "end", delta_frames: 10 },
      },
    ]);
    // Further pointer traffic after release sends nothing.
    fireEvent.pointerMove(handle, { pointerId: 7, clientX: 200 });
    fireEvent.pointerUp(handle, { pointerId: 7, clientX: 200 });
    expect(timeline).toHaveBeenCalledTimes(1);
    // Settlement comes from the accepted state: pending keeps it submitting, a new receipt accepts.
    rerender({ authoring: { ...authoring, status: "pending" } });
    expect(phase(view.container)).toBe("submitting");
    await act(async () => {
      resolveIntent();
    });
    rerender({
      authoring: {
        ...authoring,
        status: "ready",
        lastTimelineReceiptV2: {
          requestId: "req-accepted-1",
        } as TimelineReceiptV2,
      },
    });
    expect(phase(view.container)).toBe("accepted");
    expect(timeline).toHaveBeenCalledTimes(1);
  });

  it("shortens from the start edge with the opposite edge fixed", () => {
    const timeline = vi.fn(async () => undefined);
    const { view } = subject(timeline);
    const handle = grip(view.container, clip.clipId, "start");
    fireEvent.pointerDown(handle, {
      button: 0,
      isPrimary: true,
      pointerId: 1,
      clientX: 50,
    });
    fireEvent.pointerMove(handle, { pointerId: 1, clientX: 56 });
    fireEvent.pointerUp(handle, { pointerId: 1, clientX: 56 });
    expect(timelineCalls(timeline)[0]!.commands).toEqual([
      {
        kind: "trim_clip",
        payload: { clip_id: clip.clipId, edge: "start", delta_frames: 6 },
      },
    ]);
  });

  it.each(["selection", "revision", "lock"])(
    "cancels an unsubmitted draft after accepted %s changes",
    (change) => {
      const timeline = vi.fn(async () => undefined);
      const { view, rerender, authoring } = subject(timeline);
      const handle = grip(view.container, clip.clipId, "end");
      fireEvent.pointerDown(handle, {
        button: 0,
        isPrimary: true,
        pointerId: 5,
        clientX: 0,
      });
      fireEvent.pointerMove(handle, { pointerId: 5, clientX: 10 });
      expect(phase(view.container)).toBe("dragging");
      const history = authoring.timelineHistory!;
      rerender({
        authoring: {
          ...authoring,
          timelineHistory: {
            ...history,
            selection: change === "selection" ? [] : history.selection,
            snapshot: {
              ...history.snapshot,
              workspaceRevision:
                history.snapshot.workspaceRevision +
                (change === "revision" ? 1 : 0),
              tracks:
                change === "lock"
                  ? history.snapshot.tracks.map((track) => ({
                      ...track,
                      locked: true,
                    }))
                  : history.snapshot.tracks,
            },
          },
        },
      });
      expect(phase(view.container)).toBe("idle");
      fireEvent.pointerUp(handle, { pointerId: 5, clientX: 10 });
      expect(timeline).not.toHaveBeenCalled();
      expect(handle.releasePointerCapture).toHaveBeenCalledWith(5);
    },
  );

  it("defaults snap off and refuses a non-primary pointer", () => {
    const timeline = vi.fn(async () => undefined);
    const { view } = subject(timeline);
    expect(
      view.container
        .querySelector('[data-h3-nle-control="transport.snap"]')
        ?.getAttribute("aria-pressed"),
    ).toBe("false");
    const handle = grip(view.container, clip.clipId, "end");
    fireEvent.pointerDown(handle, {
      button: 0,
      isPrimary: false,
      pointerId: 2,
      clientX: 0,
    });
    fireEvent.pointerMove(handle, { pointerId: 2, clientX: 10 });
    fireEvent.pointerUp(handle, { pointerId: 2, clientX: 10 });
    expect(timeline).not.toHaveBeenCalled();
  });

  it("sends nothing for a no-op release, an Escape cancel or a changed pixel mapping", () => {
    const timeline = vi.fn(async () => undefined);
    const { view } = subject(timeline);
    const handle = grip(view.container, clip.clipId, "end");
    // No-op: released where it began.
    fireEvent.pointerDown(handle, {
      button: 0,
      isPrimary: true,
      pointerId: 1,
      clientX: 20,
    });
    fireEvent.pointerUp(handle, { pointerId: 1, clientX: 20 });
    expect(phase(view.container)).toBe("idle");
    // Escape cancels the draft edge-first.
    fireEvent.pointerDown(handle, {
      button: 0,
      isPrimary: true,
      pointerId: 2,
      clientX: 20,
    });
    fireEvent.pointerMove(handle, { pointerId: 2, clientX: 40 });
    expect(phase(view.container)).toBe("dragging");
    fireEvent.keyDown(handle, { key: "Escape" });
    expect(phase(view.container)).toBe("idle");
    fireEvent.pointerUp(handle, { pointerId: 2, clientX: 40 });
    // A zoom during the drag invalidates the mapping and cancels.
    fireEvent.pointerDown(handle, {
      button: 0,
      isPrimary: true,
      pointerId: 3,
      clientX: 20,
    });
    fireEvent.pointerMove(handle, { pointerId: 3, clientX: 40 });
    fireEvent.click(
      view.container.querySelector(
        '[data-h3-nle-control="transport.zoom_in"]',
      )!,
    );
    expect(phase(view.container)).toBe("idle");
    fireEvent.pointerUp(handle, { pointerId: 3, clientX: 40 });
    // A pointer release from another pointer does not commit the draft.
    fireEvent.pointerDown(handle, {
      button: 0,
      isPrimary: true,
      pointerId: 4,
      clientX: 20,
    });
    fireEvent.pointerMove(handle, { pointerId: 4, clientX: 40 });
    fireEvent.pointerUp(handle, { pointerId: 9, clientX: 40 });
    expect(phase(view.container)).toBe("dragging");
    fireEvent.keyDown(handle, { key: "Escape" });
    expect(timeline).not.toHaveBeenCalled();
  });

  it("keeps the keyboard path at one-frame and grid steps with Enter commit and Escape cancel", () => {
    const timeline = vi.fn(async () => undefined);
    const { view } = subject(timeline);
    const handle = grip(view.container, clip.clipId, "end");
    fireEvent.keyDown(handle, { key: "ArrowRight" });
    expect(phase(view.container)).toBe("keyboard_draft");
    fireEvent.keyDown(handle, { key: "ArrowRight" });
    fireEvent.keyDown(handle, { key: "ArrowLeft" });
    fireEvent.keyDown(handle, { key: "Escape" });
    expect(phase(view.container)).toBe("idle");
    expect(timeline).not.toHaveBeenCalled();
    fireEvent.keyDown(handle, { key: "ArrowRight" });
    fireEvent.keyDown(handle, { key: "ArrowRight" });
    fireEvent.keyDown(handle, { key: "ArrowRight" });
    fireEvent.keyDown(handle, { key: "Enter" });
    expect(phase(view.container)).toBe("submitting");
    expect(timelineCalls(timeline)).toHaveLength(1);
    expect(timelineCalls(timeline)[0]!.commands).toEqual([
      {
        kind: "trim_clip",
        payload: { clip_id: clip.clipId, edge: "end", delta_frames: 3 },
      },
    ]);
    // Enter without a draft sends nothing.
    fireEvent.keyDown(handle, { key: "Enter" });
    expect(timeline).toHaveBeenCalledTimes(1);
  });

  it("settles a backend rejection from the conflict projection and never retries", () => {
    const timeline = vi.fn(async () => undefined);
    const { view, rerender, authoring } = subject(timeline);
    const handle = grip(view.container, clip.clipId, "end");
    fireEvent.pointerDown(handle, {
      button: 0,
      isPrimary: true,
      pointerId: 1,
      clientX: 0,
    });
    fireEvent.pointerMove(handle, { pointerId: 1, clientX: 12 });
    fireEvent.pointerUp(handle, { pointerId: 1, clientX: 12 });
    expect(phase(view.container)).toBe("submitting");
    rerender({
      authoring: {
        ...(authoringReady(SMOKE_SHAPE, {
          rejection: { code: "stale_timeline_revision" },
        }) as ReadyAuthoring),
        status: "conflict",
      },
    });
    expect(phase(view.container)).toBe("rejected");
    const trimStatus = view.container.querySelector(TRIM_STATUS)!;
    expect(trimStatus.getAttribute("data-code")).toBe(
      "stale_timeline_revision",
    );
    expect(trimStatus.textContent).not.toContain("stale_timeline_revision");
    expect(timeline).toHaveBeenCalledTimes(1);
    expect(authoring.status).toBe("ready");
  });

  it("keeps a lost response uncertain and reconciles it read-only without replaying", async () => {
    // M25-16 corrective F1: the session never rejects the intent promise. Transport loss reaches
    // the component only as the accepted authoring state `error/outcome_unknown`, which must read
    // as an unknown outcome (never a rejection) until one read-only reconciliation lands.
    const timeline = vi.fn(async () => undefined);
    const { view, rerender, authoring } = subject(timeline);
    const handle = grip(view.container, clip.clipId, "end");
    fireEvent.pointerDown(handle, {
      button: 0,
      isPrimary: true,
      pointerId: 1,
      clientX: 0,
    });
    fireEvent.pointerMove(handle, { pointerId: 1, clientX: 12 });
    fireEvent.pointerUp(handle, { pointerId: 1, clientX: 12 });
    // The transaction is in flight and the projection is pending.
    rerender({ authoring: { ...authoring, status: "pending" } });
    expect(phase(view.container)).toBe("submitting");
    await act(async () => {
      rerender({
        authoring: {
          status: "error",
          projection: authoring.projection,
          reason: "outcome_unknown",
          timelineHistory: authoring.timelineHistory,
          outcomeUnknown: {
            requestId: "req-lost",
            transactionId: "tx-req-lost",
            expectedWorkspaceRevision:
              authoring.timelineHistory!.snapshot.workspaceRevision,
            expectedTimelineRevision:
              authoring.timelineHistory!.snapshot.timelineRevision,
            expectedTimelineFingerprint:
              authoring.timelineHistory!.snapshot.timelineFingerprint,
            reconciliation: "pending",
          },
        },
      });
    });
    // Unknown outcome: not a rejection, and re-dragging is refused until reconciled.
    expect(phase(view.container)).toBe("reconciling");
    const status = (selector: string) =>
      view.container.querySelector(selector)!.textContent ?? "";
    expect(status(TRIM_STATUS)).not.toMatch(/rejected/i);
    expect(status('[data-h3-nle-status="timeline"]')).toMatch(/unknown/i);
    expect(status('[data-h3-nle-status="timeline"]')).not.toMatch(/rejected/i);
    // R2-F1: the read is still pending, so the status may not claim it already happened.
    expect(status('[data-h3-nle-status="timeline"]')).toMatch(/re-reading/i);
    expect(status('[data-h3-nle-status="timeline"]')).not.toMatch(
      /was re-read/i,
    );
    fireEvent.pointerDown(handle, {
      button: 0,
      isPrimary: true,
      pointerId: 2,
      clientX: 0,
    });
    fireEvent.pointerMove(handle, { pointerId: 2, clientX: 30 });
    fireEvent.pointerUp(handle, { pointerId: 2, clientX: 30 });
    expect(timeline).toHaveBeenCalledTimes(1);
    // R2-F1: when the reconciliation read itself fails the uncertainty is kept and stated as a
    // failed read, still without any claim that the timeline was refreshed.
    await act(async () => {
      rerender({
        authoring: {
          status: "error",
          projection: authoring.projection,
          reason: "outcome_unknown",
          timelineHistory: authoring.timelineHistory,
          outcomeUnknown: {
            requestId: "req-lost",
            transactionId: "tx-req-lost",
            expectedWorkspaceRevision:
              authoring.timelineHistory!.snapshot.workspaceRevision,
            expectedTimelineRevision:
              authoring.timelineHistory!.snapshot.timelineRevision,
            expectedTimelineFingerprint:
              authoring.timelineHistory!.snapshot.timelineFingerprint,
            reconciliation: "failed",
          },
        },
      });
    });
    expect(status('[data-h3-nle-status="timeline"]')).toMatch(
      /could not be re-read/i,
    );
    expect(status('[data-h3-nle-status="timeline"]')).not.toMatch(
      /was re-read/i,
    );
    expect(status('[data-h3-nle-status="timeline"]')).not.toMatch(/rejected/i);
    expect(timeline).toHaveBeenCalledTimes(1);
    // A fresh ready projection with no attributable receipt reconciles to idle, never replays.
    rerender({
      authoring: {
        status: "ready",
        projection: authoring.projection,
        timelineHistory: authoring.timelineHistory,
      },
    });
    expect(phase(view.container)).toBe("idle");
    expect(timeline).toHaveBeenCalledTimes(1);
    // The next trim proceeds on the refreshed base as a second, separate transaction.
    fireEvent.pointerDown(handle, {
      button: 0,
      isPrimary: true,
      pointerId: 3,
      clientX: 0,
    });
    fireEvent.pointerMove(handle, { pointerId: 3, clientX: 12 });
    fireEvent.pointerUp(handle, { pointerId: 3, clientX: 12 });
    expect(timeline).toHaveBeenCalledTimes(2);
  });

  it("returns to idle after the settled status timed out", () => {
    vi.useFakeTimers();
    const timeline = vi.fn(async () => undefined);
    const { view, rerender, authoring } = subject(timeline);
    const handle = grip(view.container, clip.clipId, "end");
    fireEvent.pointerDown(handle, {
      button: 0,
      isPrimary: true,
      pointerId: 1,
      clientX: 0,
    });
    fireEvent.pointerMove(handle, { pointerId: 1, clientX: 5 });
    fireEvent.pointerUp(handle, { pointerId: 1, clientX: 5 });
    rerender({
      authoring: {
        ...authoring,
        lastTimelineReceipt: receiptFor(authoring, "req-2"),
      },
    });
    expect(phase(view.container)).toBe("accepted");
    act(() => {
      vi.advanceTimersByTime(1_600);
    });
    expect(phase(view.container)).toBe("idle");
  });
});

describe("M25-16 corrective 02 R2-F3: virtualized draft-owner removal", () => {
  const GRID = ".h3-nle-tracks";

  /**
   * A keyboard draft owned by the layout the virtual window can actually remove.
   *
   * The fixture's clips are 48 frames, so at the default zoom the selected clip is narrow enough
   * to be driven from the rail, which lives outside the scroll container and never unmounts.
   * Zoom in until the inline grips are the owner, and assert that before starting: for one whole
   * corrective round a case that meant to drive the inline layout silently drove the rail and
   * still passed. Zooming changes the mapping key, so it must precede the draft.
   */
  function virtualizedDraft(edge: "start" | "end" = "end") {
    const edgeGestureActive: boolean[] = [];
    const timeline = vi.fn(async () => undefined);
    const built = subject(
      timeline,
      (active) => edgeGestureActive.push(active),
      VIRTUALIZED_SHAPE,
    );
    const zoomIn = built.view.container.querySelector<HTMLButtonElement>(
      '[data-h3-nle-control="transport.zoom_in"]',
    )!;
    act(() => {
      fireEvent.click(zoomIn);
      fireEvent.click(zoomIn);
    });
    expect(
      built.view.container.querySelector(
        '[data-h3-nle-inline-grips="true"][data-h3-nle-clip="clip-0"]',
      ),
    ).not.toBeNull();
    const handle = grip(built.view.container, "clip-0", edge);
    handle.focus();
    // Each edge is driven in the direction that shortens the clip, so neither draft can be
    // refused by a bound before the removal under test happens.
    fireEvent.keyDown(handle, {
      key: edge === "end" ? "ArrowRight" : "ArrowLeft",
    });
    expect(phase(built.view.container)).toBe("keyboard_draft");
    expect(edgeGestureActive.at(-1)).toBe(true);
    return { ...built, timeline, edgeGestureActive, handle };
  }

  /** The same draft on the rail layout, which renders outside the scroll container. */
  function railDraft() {
    const edgeGestureActive: boolean[] = [];
    const timeline = vi.fn(async () => undefined);
    const built = subject(
      timeline,
      (active) => edgeGestureActive.push(active),
      VIRTUALIZED_SHAPE,
    );
    // At Fit the selected clip is below the inline threshold, so the rail owns the grips.
    // Assert that, so this case cannot silently become an inline case either.
    fitTimeline(built.view.container);
    expect(
      built.view.container.querySelector(
        '[data-h3-nle-inline-grips="true"][data-h3-nle-clip="clip-0"]',
      ),
    ).toBeNull();
    const handle = grip(built.view.container, "clip-0", "end");
    handle.focus();
    fireEvent.keyDown(handle, { key: "ArrowRight" });
    expect(phase(built.view.container)).toBe("keyboard_draft");
    return { ...built, timeline, edgeGestureActive, handle };
  }

  function scrollRows(container: HTMLElement, rows: number) {
    const grid = container.querySelector<HTMLElement>(GRID)!;
    act(() => {
      fireEvent.scroll(grid, { target: { scrollTop: rows * 56 } });
    });
    return grid;
  }

  it.each(["end", "start"] as const)(
    "cancels a %s-edge draft, clears the edge guard and keeps focus owned when the row unmounts",
    (edge) => {
      // Ownership is computed from the mounted clip window, not from which edge is being
      // dragged, so both edges must be shown to cancel rather than assumed to from one.
      const { view, timeline, edgeGestureActive } = virtualizedDraft(edge);
      const grid = scrollRows(view.container, 8);

      expect(
        view.container.querySelector('[data-h3-nle-trim-clip="clip-0"]'),
      ).toBeNull();
      expect(phase(view.container)).toBe("idle");
      expect(edgeGestureActive.at(-1)).toBe(false);
      expect(timeline).not.toHaveBeenCalled();
      const active = document.activeElement as HTMLElement | null;
      expect(active).not.toBe(document.body);
      expect(active?.isConnected).toBe(true);
      expect(grid.contains(active) || grid === active).toBe(true);
    },
  );

  it("leaves a rail-owned draft alone: that layout never leaves the window", () => {
    // The other half of "both layouts where ownership applies". The rail renders outside the
    // scroll container, so the same scroll removes no owner and must cancel nothing; cancelling
    // here would discard a live draft on a control the user can still see.
    const { view, timeline } = railDraft();
    scrollRows(view.container, 8);

    expect(phase(view.container)).toBe("keyboard_draft");
    expect(timeline).not.toHaveBeenCalled();
  });

  it("leaves a draft alone while its owning row is still mounted", () => {
    const { view, timeline, edgeGestureActive } = virtualizedDraft();
    scrollRows(view.container, 1);

    expect(
      view.container.querySelector('[data-h3-nle-trim-clip="clip-0"]'),
    ).not.toBeNull();
    expect(phase(view.container)).toBe("keyboard_draft");
    expect(edgeGestureActive.at(-1)).toBe(true);
    expect(timeline).not.toHaveBeenCalled();
  });

  it("does not disturb a submitted transaction when its row unmounts", () => {
    // `submitting` owns a real backend transaction; scrolling its row out of the window must
    // not cancel it, and must not fabricate a settled outcome for it either.
    const { view, timeline, handle } = virtualizedDraft();
    fireEvent.keyDown(handle, { key: "Enter" });
    expect(phase(view.container)).toBe("submitting");
    expect(timeline).toHaveBeenCalledTimes(1);

    scrollRows(view.container, 8);

    expect(phase(view.container)).toBe("submitting");
    expect(timeline).toHaveBeenCalledTimes(1);
  });
});
