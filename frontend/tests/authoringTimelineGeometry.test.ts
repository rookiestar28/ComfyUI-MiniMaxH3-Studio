import { describe, expect, it } from "vitest";

import {
  stepTimelineFrame,
  timelineFrameFromClientX,
  timelineFramePercent,
} from "../src/runtime/timelineGeometry";

describe("authoring timeline geometry", () => {
  it("maps the full owned slider to the accepted closed integer frame domain", () => {
    const frame = (clientX: number, startX = 10, endX = 110) =>
      timelineFrameFromClientX({
        clientX,
        startX,
        endX,
        extentFrames: 3_600,
        currentFrame: 17,
      });

    expect(frame(-100)).toBe(0);
    expect(frame(10)).toBe(0);
    expect(frame(60)).toBe(1_800);
    expect(frame(110)).toBe(3_599);
    expect(frame(500)).toBe(3_599);
    expect(frame(25, 100, 0)).toBe(900);
  });

  it("preserves the current frame when slider geometry cannot define a span", () => {
    expect(
      timelineFrameFromClientX({
        clientX: 500,
        startX: 10,
        endX: 10,
        extentFrames: 100,
        currentFrame: 73,
      }),
    ).toBe(73);
    expect(
      timelineFrameFromClientX({
        clientX: Number.NaN,
        startX: 0,
        endX: 100,
        extentFrames: 100,
        currentFrame: 73,
      }),
    ).toBe(73);
  });

  it("positions exact endpoints and validates the accepted frame domain", () => {
    expect(timelineFramePercent(0, 3_600)).toBe(0);
    expect(timelineFramePercent(1_800, 3_600)).toBeCloseTo(
      (1_800 / 3_599) * 100,
    );
    expect(timelineFramePercent(3_599, 3_600)).toBe(100);
    expect(timelineFramePercent(0, 1)).toBe(0);
    expect(() => timelineFramePercent(3_600, 3_600)).toThrow(
      "timeline frame is outside the accepted extent",
    );
  });

  it("uses one frame, the accepted grid, and exact endpoints for keyboard seek", () => {
    const step = (frame: number, key: string, shiftKey = false) =>
      stepTimelineFrame({
        frame,
        key,
        shiftKey,
        frameGrid: 51,
        extentFrames: 3_600,
      });

    expect(step(10, "ArrowLeft")).toBe(9);
    expect(step(10, "ArrowRight")).toBe(11);
    expect(step(10, "ArrowRight", true)).toBe(61);
    expect(step(10, "ArrowLeft", true)).toBe(0);
    expect(step(10, "Home")).toBe(0);
    expect(step(10, "End")).toBe(3_599);
    expect(step(10, "PageDown")).toBeUndefined();
  });
});
