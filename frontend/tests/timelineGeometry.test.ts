import { describe, expect, it } from "vitest";

import {
  MAX_TIMELINE_SCALE,
  RULER_MIN_SPACING_PX,
  fitTimelineScale,
  frameBoundaryToX,
  frameFromX,
  proposalFromCapturedOrigin,
  rulerMarks,
  timelineScaleBounds,
  zoomAround,
  zoomToSelection,
} from "../src/runtime/timelineGeometry";

describe("continuous timeline geometry", () => {
  it("fits long and smoke timelines with the frozen minimum rule", () => {
    expect(fitTimelineScale(1_100, 14_400, 1)).toBeCloseTo(1_100 / 14_400);
    expect(timelineScaleBounds(1_100, 14_400, 1)).toEqual({
      min: 1_100 / 14_400,
      max: MAX_TIMELINE_SCALE,
      fit: 1_100 / 14_400,
    });
    expect(timelineScaleBounds(1_100, 2_880, 1).min).toBe(0.25);
    expect(fitTimelineScale(0, 14_400, 0.5)).toBe(0.5);
  });

  it("round trips fractional origins in separate frame and boundary domains", () => {
    for (const scale of [1_100 / 14_400, 0.25, 1, 16]) {
      const x = frameBoundaryToX(1_234, 12.5, scale);
      expect(frameFromX(x, 12.5, scale, 14_400)).toBe(1_234);
    }
  });

  it("keeps an unclamped zoom anchor within half a pixel", () => {
    const next = zoomAround({
      viewStart: 100.25,
      scale: 0.5,
      nextScale: 2,
      anchorX: 321,
      laneWidth: 1_100,
      durationFrames: 14_400,
    });
    const before = (100.25 + 321 / 0.5 - 100.25) * 0.5;
    const after = (100.25 + 321 / 0.5 - next.viewStart) * next.scale;
    expect(Math.abs(before - after)).toBeLessThanOrEqual(0.5);
  });

  it("chooses finite ruler marks separated by at least 72 pixels", () => {
    const marks = rulerMarks({
      viewStart: 0,
      laneWidth: 1_100,
      pixelsPerFrame: 0.08,
      durationFrames: 14_400,
      fps: 24,
    });
    expect(marks.length).toBeGreaterThan(1);
    for (let index = 1; index < marks.length; index += 1)
      expect(
        (marks[index]!.frame - marks[index - 1]!.frame) * 0.08,
      ).toBeGreaterThanOrEqual(RULER_MIN_SPACING_PX);
  });

  it("uses signed half-away-from-zero captured-origin proposals", () => {
    expect(proposalFromCapturedOrigin(10, 0.5, 1)).toBe(11);
    expect(proposalFromCapturedOrigin(10, -0.5, 1)).toBe(9);
    expect(proposalFromCapturedOrigin(10, 0.2 + 0.3, 1)).toBe(
      proposalFromCapturedOrigin(10, 0.5, 1),
    );
  });

  it("zooms a selection to sixty percent when bounds permit", () => {
    expect(
      zoomToSelection({
        startFrame: 200,
        endFrame: 250,
        laneWidth: 1_000,
        durationFrames: 2_880,
      }),
    ).toMatchObject({ scale: 12, viewStart: expect.any(Number) });
    expect(
      zoomToSelection({
        startFrame: 200,
        endFrame: 220,
        laneWidth: 1_000,
        durationFrames: 2_880,
        maxScale: 16,
      }).scale,
    ).toBe(16);
  });
});
