import { describe, expect, it } from "vitest";

import {
  TRIM_GESTURE_IDLE,
  frameDeltaFromDisplacement,
  quantizeSignedDelta,
  reduceTrimGesture,
  snapTrimBoundary,
  trimCommandForDraft,
  trimGeometry,
  type TrimGestureState,
} from "../src/runtime/nleTrimGesture";
import {
  clampOverlayBounds,
  overlayDefaultBounds,
  overlayMargin,
  overlayMinimum,
} from "../src/runtime/nleOverlayGeometry";
import {
  deriveNleSurfaceCapability,
  NLE_SURFACE_CAPABILITY_SCHEMA,
} from "../src/contracts/nleSurfaceCapability";
import {
  RUNTIME_CAPABILITY_OBSERVATION_SCHEMA,
  RUNTIME_PROFILE_FINGERPRINT,
  evaluateMediaCapabilities,
} from "../src/runtime/mediaCapabilities";
import {
  ACCEPTED_RUNTIME_QUALIFICATION,
  RUNTIME_QUALIFICATION_AUTHORITY,
} from "../src/runtime/acceptedRuntimeQualification";

const identity = Object.freeze({
  clipId: "clip-main",
  edge: "end" as const,
  workspaceRevision: 3,
  timelineRevision: 5,
  timelineFingerprint: `sha256:${"a".repeat(64)}`,
  mappingKey: "zoom=1;scroll=0;w=800",
  workspaceHandle: "authoring-" + "a".repeat(32),
});

function begin(
  edge: "start" | "end",
  input: "pointer" | "keyboard" = "pointer",
  from: TrimGestureState = TRIM_GESTURE_IDLE,
): TrimGestureState {
  return reduceTrimGesture(from, {
    type: "begin",
    input,
    identity: { ...identity, edge },
    pointerId: input === "pointer" ? 7 : null,
    originClientX: 100,
    pixelsPerFrame: 4,
    originStart: 10,
    originEnd: 30,
    outputDurationFrames: 48,
  });
}

const noSnap = () => null;

describe("NLE-EDGE-TRIM-V1 quantization and geometry", () => {
  it("quantizes half ties away from zero and normalizes negative zero", () => {
    expect(quantizeSignedDelta(0.5)).toBe(1);
    expect(quantizeSignedDelta(-0.5)).toBe(-1);
    expect(quantizeSignedDelta(1.49)).toBe(1);
    expect(quantizeSignedDelta(-1.49)).toBe(-1);
    expect(quantizeSignedDelta(-0.4)).toBe(0);
    expect(Object.is(quantizeSignedDelta(-0.4), -0)).toBe(false);
    expect(quantizeSignedDelta(Number.NaN)).toBeNull();
    expect(quantizeSignedDelta(Number.POSITIVE_INFINITY)).toBeNull();
  });

  it("maps total displacement through the captured pixel scale", () => {
    expect(frameDeltaFromDisplacement(10, 4)).toBe(2.5);
    expect(frameDeltaFromDisplacement(10, 0)).toBeNull();
    expect(frameDeltaFromDisplacement(Number.NaN, 4)).toBeNull();
  });

  it("maps a start trim onto [s+d, e) with a visible one-frame floor", () => {
    expect(
      trimGeometry({
        edge: "start",
        startFrame: 10,
        endFrame: 30,
        deltaFrames: 5,
        outputDurationFrames: 48,
      }),
    ).toMatchObject({ start: 15, end: 30, deltaFrames: 5, clamped: false });
    expect(
      trimGeometry({
        edge: "start",
        startFrame: 10,
        endFrame: 30,
        deltaFrames: 25,
        outputDurationFrames: 48,
      }),
    ).toMatchObject({ start: 29, end: 30, deltaFrames: 19, clamped: true });
    expect(
      trimGeometry({
        edge: "start",
        startFrame: 10,
        endFrame: 30,
        deltaFrames: -12,
        outputDurationFrames: 48,
      }),
    ).toMatchObject({ start: 0, end: 30, deltaFrames: -10, clamped: true });
  });

  it("maps an end trim onto [s, e+d) where the exclusive end may equal the duration", () => {
    expect(
      trimGeometry({
        edge: "end",
        startFrame: 10,
        endFrame: 30,
        deltaFrames: 18,
        outputDurationFrames: 48,
      }),
    ).toMatchObject({ start: 10, end: 48, deltaFrames: 18, clamped: false });
    expect(
      trimGeometry({
        edge: "end",
        startFrame: 10,
        endFrame: 30,
        deltaFrames: 19,
        outputDurationFrames: 48,
      }),
    ).toMatchObject({ end: 48, deltaFrames: 18, clamped: true });
    expect(
      trimGeometry({
        edge: "end",
        startFrame: 10,
        endFrame: 30,
        deltaFrames: -40,
        outputDurationFrames: 48,
      }),
    ).toMatchObject({ start: 10, end: 11, deltaFrames: -19, clamped: true });
    expect(
      trimGeometry({
        edge: "end",
        startFrame: 47,
        endFrame: 48,
        deltaFrames: -1,
        outputDurationFrames: 48,
      }),
    ).toMatchObject({ end: 48, deltaFrames: 0, noop: true });
  });

  it("refuses an inexact interval", () => {
    expect(() =>
      trimGeometry({
        edge: "end",
        startFrame: 10,
        endFrame: 10,
        deltaFrames: 1,
        outputDurationFrames: 48,
      }),
    ).toThrow();
  });

  it("snaps by distance, then boundary before grid before playhead, then lower frame", () => {
    const clips = [
      { clipId: "clip-main", startFrame: 10, endFrame: 30 },
      { clipId: "clip-b", startFrame: 30, endFrame: 40 },
      { clipId: "clip-c", startFrame: 33, endFrame: 45 },
    ];
    expect(
      snapTrimBoundary({
        rawBoundaryFrame: 31.4,
        pixelsPerFrame: 4,
        clips,
        targetClipId: "clip-main",
        playheadFrame: 31,
        outputDurationFrames: 48,
      }),
    ).toEqual({ kind: "grid", frame: 31 });
    // Equidistant grid 32 / grid 33 / boundary 33: boundary outranks grid.
    expect(
      snapTrimBoundary({
        rawBoundaryFrame: 32.5,
        pixelsPerFrame: 4,
        clips,
        targetClipId: "clip-main",
        playheadFrame: null,
        outputDurationFrames: 48,
      }),
    ).toEqual({ kind: "boundary", frame: 33 });
    // Equidistant grid 31 / grid 32 with no boundary: the lower frame wins.
    expect(
      snapTrimBoundary({
        rawBoundaryFrame: 31.5,
        pixelsPerFrame: 4,
        clips,
        targetClipId: "clip-main",
        playheadFrame: null,
        outputDurationFrames: 48,
      }),
    ).toEqual({ kind: "grid", frame: 31 });
    expect(
      snapTrimBoundary({
        rawBoundaryFrame: 33.2,
        pixelsPerFrame: 4,
        clips,
        targetClipId: "clip-main",
        playheadFrame: 33,
        outputDurationFrames: 48,
      }),
    ).toEqual({ kind: "boundary", frame: 33 });
    expect(
      snapTrimBoundary({
        rawBoundaryFrame: 31.4,
        pixelsPerFrame: 0,
        clips,
        targetClipId: "clip-main",
        playheadFrame: null,
        outputDurationFrames: 48,
      }),
    ).toBeNull();
  });
});

describe("NLE-EDGE-TRIM-V1 gesture state machine", () => {
  it("drags the right edge from total displacement and submits exactly once", () => {
    let state = begin("end");
    expect(state.phase).toBe("dragging");
    state = reduceTrimGesture(state, {
      type: "move",
      clientX: 118,
      snap: noSnap,
    });
    state = reduceTrimGesture(state, {
      type: "move",
      clientX: 110,
      snap: noSnap,
    });
    if (state.phase !== "dragging") throw new Error(state.phase);
    expect(state.draft.geometry).toMatchObject({
      start: 10,
      end: 33,
      deltaFrames: 3,
    });
    state = reduceTrimGesture(state, { type: "release", requestId: "r1" });
    expect(state.phase).toBe("submitting");
    if (state.phase !== "submitting") throw new Error(state.phase);
    expect(trimCommandForDraft(state.draft)).toEqual({
      kind: "trim_clip",
      clip_id: "clip-main",
      edge: "end",
      delta_frames: 3,
    });
    // A duplicate release or synthetic move cannot issue another transaction.
    expect(reduceTrimGesture(state, { type: "release", requestId: "r2" })).toBe(
      state,
    );
    expect(
      reduceTrimGesture(state, { type: "move", clientX: 300, snap: noSnap }),
    ).toBe(state);
    state = reduceTrimGesture(state, { type: "accepted" });
    expect(state.phase).toBe("accepted");
    expect(reduceTrimGesture(state, { type: "reset" })).toBe(TRIM_GESTURE_IDLE);
  });

  it("shortens from the left edge with the opposite edge fixed", () => {
    let state = begin("start");
    state = reduceTrimGesture(state, {
      type: "move",
      clientX: 122,
      snap: noSnap,
    });
    if (state.phase !== "dragging") throw new Error(state.phase);
    expect(state.draft.geometry).toMatchObject({
      start: 16,
      end: 30,
      deltaFrames: 6,
    });
    state = reduceTrimGesture(state, { type: "release", requestId: "r1" });
    if (state.phase !== "submitting") throw new Error(state.phase);
    expect(trimCommandForDraft(state.draft).delta_frames).toBe(6);
  });

  it("sends nothing for a no-op, a cancel, a non-finite coordinate or a changed mapping", () => {
    expect(
      reduceTrimGesture(begin("end"), { type: "release", requestId: "r" }),
    ).toBe(TRIM_GESTURE_IDLE);
    let state = reduceTrimGesture(begin("end"), {
      type: "move",
      clientX: 140,
      snap: noSnap,
    });
    expect(reduceTrimGesture(state, { type: "cancel", reason: "escape" })).toBe(
      TRIM_GESTURE_IDLE,
    );
    expect(
      reduceTrimGesture(state, {
        type: "move",
        clientX: Number.NaN,
        snap: noSnap,
      }),
    ).toBe(TRIM_GESTURE_IDLE);
    expect(
      reduceTrimGesture(state, {
        type: "mapping_changed",
        mappingKey: "zoom=2",
      }),
    ).toBe(TRIM_GESTURE_IDLE);
    expect(
      reduceTrimGesture(state, {
        type: "mapping_changed",
        mappingKey: identity.mappingKey,
      }),
    ).toBe(state);
    state = reduceTrimGesture(TRIM_GESTURE_IDLE, {
      type: "begin",
      input: "pointer",
      identity,
      pointerId: 1,
      originClientX: 10,
      pixelsPerFrame: 0,
      originStart: 0,
      originEnd: 10,
      outputDurationFrames: 48,
    });
    expect(state).toBe(TRIM_GESTURE_IDLE);
  });

  it("begins a fresh draft over a settled notice but never over an uncertain outcome", () => {
    let state = reduceTrimGesture(begin("end"), {
      type: "move",
      clientX: 108,
      snap: noSnap,
    });
    state = reduceTrimGesture(state, { type: "release", requestId: "r1" });
    const rejected = reduceTrimGesture(state, {
      type: "rejected",
      reason: "conflict",
    });
    expect(rejected.phase).toBe("rejected");
    // The refusal notice holds no draft: the next gesture starts on the current base with a
    // zero delta, not on the refused one.
    const restarted = begin("start", "keyboard", rejected);
    expect(restarted.phase).toBe("keyboard_draft");
    if (restarted.phase !== "keyboard_draft") throw new Error(restarted.phase);
    expect(restarted.draft.identity.edge).toBe("start");
    expect(restarted.draft.rawDeltaFrames).toBe(0);
    const accepted = reduceTrimGesture(state, { type: "accepted" });
    expect(begin("end", "pointer", accepted).phase).toBe("dragging");
    // Submitting and reconciling refuse: a second draft could replay an unresolved request.
    expect(begin("end", "pointer", state)).toBe(state);
    const lost = reduceTrimGesture(state, {
      type: "response_lost",
      reason: "timeout",
    });
    expect(begin("end", "pointer", lost)).toBe(lost);
  });
  it("keeps a lost response uncertain until reconciliation and never replays", () => {
    let state = reduceTrimGesture(begin("end"), {
      type: "move",
      clientX: 108,
      snap: noSnap,
    });
    state = reduceTrimGesture(state, { type: "release", requestId: "r1" });
    state = reduceTrimGesture(state, {
      type: "response_lost",
      reason: "timeout",
    });
    expect(state.phase).toBe("reconciling");
    expect(reduceTrimGesture(state, { type: "cancel", reason: "escape" })).toBe(
      state,
    );
    expect(reduceTrimGesture(state, { type: "release", requestId: "r2" })).toBe(
      state,
    );
    expect(reduceTrimGesture(state, { type: "reconciled" })).toBe(
      TRIM_GESTURE_IDLE,
    );
  });

  it("supports the keyboard draft with one-frame steps, commit and escape", () => {
    let state = begin("start", "keyboard");
    expect(state.phase).toBe("keyboard_draft");
    state = reduceTrimGesture(state, { type: "step", deltaFrames: 1 });
    state = reduceTrimGesture(state, { type: "step", deltaFrames: 1 });
    state = reduceTrimGesture(state, { type: "step", deltaFrames: -1 });
    if (state.phase !== "keyboard_draft") throw new Error(state.phase);
    expect(state.draft.geometry).toMatchObject({ start: 11, deltaFrames: 1 });
    expect(reduceTrimGesture(state, { type: "cancel", reason: "escape" })).toBe(
      TRIM_GESTURE_IDLE,
    );
    state = reduceTrimGesture(state, { type: "commit", requestId: "k1" });
    expect(state.phase).toBe("submitting");
    state = reduceTrimGesture(state, {
      type: "rejected",
      reason: "source_range_unavailable",
    });
    expect(state).toMatchObject({
      phase: "rejected",
      reason: "source_range_unavailable",
    });
  });

  it("applies an advisory snap target to the draft geometry", () => {
    let state = begin("end");
    state = reduceTrimGesture(state, {
      type: "move",
      clientX: 113,
      snap: () => ({ kind: "boundary", frame: 34 }),
    });
    if (state.phase !== "dragging") throw new Error(state.phase);
    expect(state.draft.snap).toEqual({ kind: "boundary", frame: 34 });
    expect(state.draft.geometry.end).toBe(34);
  });
});

describe("overlay_v1 geometry", () => {
  it("uses the default size inside a large viewport and clamps inside small ones", () => {
    const large = { width: 1920, height: 1080 };
    expect(overlayMargin(large)).toBe(16);
    // M25-44: the workspace opens at the viewport minus the margin, not at a size ceiling.
    expect(overlayDefaultBounds(large)).toEqual({ width: 1888, height: 1048 });
    expect(overlayMinimum(large)).toEqual({ width: 720, height: 480 });
    const small = { width: 700, height: 500 };
    expect(overlayMargin(small)).toBe(8);
    expect(overlayDefaultBounds(small)).toEqual({ width: 684, height: 484 });
    expect(overlayMinimum(small)).toEqual({ width: 684, height: 480 });
    expect(clampOverlayBounds(large, { width: 5000, height: 10 })).toEqual({
      width: 1888,
      height: 480,
    });
    expect(
      clampOverlayBounds(large, { width: Number.NaN, height: Number.NaN }),
    ).toEqual({ width: 1888, height: 1048 });
  });
});

describe("nle surface capability", () => {
  const observation = {
    schema: RUNTIME_CAPABILITY_OBSERVATION_SCHEMA,
    profileFingerprint: RUNTIME_PROFILE_FINGERPRINT,
    htmlMediaElement: true,
    canPlayMp4H264Aac: "probably" as const,
    requestVideoFrameCallback: true,
    seekedEvent: true,
    timeupdateEvent: true,
    canvas2d: true,
    crossOriginIsolated: false,
  };
  const bounds = { width: 1280, height: 800 };

  it("derives overlay_v1 with the composition monitor from an available runtime", () => {
    const value = deriveNleSurfaceCapability({
      runtime: evaluateMediaCapabilities(
        observation,
        ACCEPTED_RUNTIME_QUALIFICATION,
        RUNTIME_QUALIFICATION_AUTHORITY,
      ),
      documentAvailable: true,
      mountAdmitted: true,
      focusReturnToken: "nle-open-overlay",
      viewportBounds: bounds,
    });
    expect(value).toMatchObject({
      schema: NLE_SURFACE_CAPABILITY_SCHEMA,
      version: 1,
      capability: "overlay_v1",
      monitorMode: "composition",
      failureDisposition: null,
    });
    expect(Object.isFrozen(value)).toBe(true);
    expect(JSON.stringify(value)).not.toMatch(/http|\\\\|\//u);
  });

  it("keeps overlay_v1 with the labeled selected-source monitor without qualification", () => {
    const value = deriveNleSurfaceCapability({
      runtime: evaluateMediaCapabilities(observation),
      documentAvailable: true,
      mountAdmitted: true,
      focusReturnToken: "nle-open-overlay",
      viewportBounds: bounds,
    });
    expect(value.capability).toBe("overlay_v1");
    expect(value.monitorMode).toBe("selected_source_only");
  });

  it("is unsupported without a document, media runtime or admitted mount", () => {
    const runtime = evaluateMediaCapabilities(observation);
    expect(
      deriveNleSurfaceCapability({
        runtime,
        documentAvailable: false,
        mountAdmitted: true,
        focusReturnToken: "nle-open-overlay",
        viewportBounds: bounds,
      }).failureDisposition,
    ).toBe("document_unavailable");
    expect(
      deriveNleSurfaceCapability({
        runtime: evaluateMediaCapabilities({
          ...observation,
          htmlMediaElement: false,
        }),
        documentAvailable: true,
        mountAdmitted: true,
        focusReturnToken: "nle-open-overlay",
        viewportBounds: bounds,
      }),
    ).toMatchObject({
      capability: "unsupported",
      failureDisposition: "media_runtime_unavailable",
    });
    expect(
      deriveNleSurfaceCapability({
        runtime,
        documentAvailable: true,
        mountAdmitted: false,
        focusReturnToken: "nle-open-overlay",
        viewportBounds: bounds,
      }).failureDisposition,
    ).toBe("mount_admission_refused");
    expect(() =>
      deriveNleSurfaceCapability({
        runtime,
        documentAvailable: true,
        mountAdmitted: true,
        focusReturnToken: "bad token/with/slashes",
        viewportBounds: bounds,
      }),
    ).toThrow();
  });
});
