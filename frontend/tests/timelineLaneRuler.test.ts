import { describe, expect, it } from "vitest";

import {
  LANE_LEAD_IN_PX,
  LANE_ORIGIN_PX,
  RULER_MIN_SPACING_PX,
  TRACK_HEADER_WIDTH_PX,
  boundaryFromX,
  contentXFromClientX,
  formatRulerLabel,
  frameBoundaryToX,
  frameFromX,
  laneXForBoundary,
  laneXFromClientX,
  rulerMarks,
  rulerMinorTicks,
  timelineXForBoundary,
} from "../src/runtime/timelineGeometry";

// M25-61 (redesign foundation): one lane transform with a 16 px lead-in before frame 0, and ruler
// ticks that live only in the ruler with short labels and minor ticks down to single frames.

describe("lane transform with a lead-in", () => {
  it("puts frame 0 one lead-in after the header edge at every scale", () => {
    expect(LANE_LEAD_IN_PX).toBe(16);
    expect(LANE_ORIGIN_PX).toBe(TRACK_HEADER_WIDTH_PX + LANE_LEAD_IN_PX);
    for (const scale of [0.08, 0.25, 1, 5.8, 16]) {
      expect(laneXForBoundary(0, 0, scale)).toBe(LANE_LEAD_IN_PX);
      expect(timelineXForBoundary(0, 0, scale)).toBe(LANE_ORIGIN_PX);
    }
  });

  it("round-trips client pixels and boundaries exactly through one transform", () => {
    const left = 37.5;
    let seed = 7;
    const random = () => {
      seed = (seed * 1_103_515_245 + 12_345) % 2_147_483_648;
      return seed / 2_147_483_648;
    };
    for (const scale of [0.25, 1, 5.8, 16]) {
      for (let sample = 0; sample < 200; sample += 1) {
        const viewStart = Math.floor(random() * 400);
        const boundary = viewStart + Math.floor(random() * 300);
        const clientX = left + timelineXForBoundary(boundary, viewStart, scale);
        const contentX = contentXFromClientX(clientX, left);
        expect(boundaryFromX(contentX, viewStart, scale, 14_400)).toBe(
          boundary,
        );
        expect(
          laneXForBoundary(boundary, viewStart, scale) + TRACK_HEADER_WIDTH_PX,
        ).toBeCloseTo(timelineXForBoundary(boundary, viewStart, scale), 9);
        expect(laneXFromClientX(clientX, left)).toBeCloseTo(
          laneXForBoundary(boundary, viewStart, scale),
          9,
        );
      }
    }
  });

  it("maps a click inside the lead-in before frame 0 to frame 0", () => {
    const left = 10;
    for (const scale of [0.25, 1, 16]) {
      const x = contentXFromClientX(left + TRACK_HEADER_WIDTH_PX + 2, left);
      expect(frameFromX(x, 0, scale, 240)).toBe(0);
      expect(boundaryFromX(x, 0, scale, 240)).toBe(0);
    }
  });
});

describe("ruler labels", () => {
  it("labels whole seconds as MM:SS and sub-second marks as the frame within the second", () => {
    expect(formatRulerLabel(0, 24)).toBe("00:00");
    expect(formatRulerLabel(24, 24)).toBe("00:01");
    expect(formatRulerLabel(61 * 24, 24)).toBe("01:01");
    expect(formatRulerLabel(12, 24)).toBe("12f");
    expect(formatRulerLabel(5, 24)).toBe("05f");
    expect(formatRulerLabel(24 + 5, 24)).toBe("05f");
    expect(formatRulerLabel(3_600 * 24, 24)).toBe("1:00:00");
    expect(formatRulerLabel((3_600 + 62) * 24, 24)).toBe("1:01:02");
  });
});

describe("ruler minor ticks", () => {
  const input = (pixelsPerFrame: number, viewStart = 0) => ({
    viewStart,
    laneWidth: 1_200,
    pixelsPerFrame,
    durationFrames: 14_400,
    fps: 24,
  });
  const stride = (pixelsPerFrame: number) => {
    const marks = rulerMarks(input(pixelsPerFrame));
    return marks[1]!.frame - marks[0]!.frame;
  };

  it("reaches single frames at 16 and at 6 px per frame", () => {
    for (const scale of [16, 6]) {
      const majors = new Set(
        rulerMarks(input(scale)).map((mark) => mark.frame),
      );
      const minors = rulerMinorTicks(input(scale)).map((tick) => tick.frame);
      const expected = [];
      for (let frame = 1; frame <= 1_200 / scale; frame += 1)
        if (!majors.has(frame)) expected.push(frame);
      expect(minors).toEqual(expected);
    }
    // B-M2562-08: 6, not the 5 plan R8 first wrote; a sub-second stride divides 24.
    expect(stride(16)).toBe(6);
    expect(stride(6)).toBe(12);
  });

  it("subdivides a one-second stride every two frames at 5.8 px per frame", () => {
    expect(stride(5.8)).toBe(24);
    const minors = rulerMinorTicks(input(5.8)).map((tick) => tick.frame);
    expect(minors.slice(0, 4)).toEqual([2, 4, 6, 8]);
    expect(minors).not.toContain(24);
  });

  it("keeps minor ticks at least 6 px apart, off the majors, in the same coordinates as the majors", () => {
    for (const scale of [0.08, 0.25, 0.5, 1, 2.5, 5.8, 6, 9, 16]) {
      for (const viewStart of [0, 37, 1_000.5]) {
        const majors = rulerMarks(input(scale, viewStart));
        const minors = rulerMinorTicks(input(scale, viewStart));
        const majorFrames = new Set(majors.map((mark) => mark.frame));
        for (let index = 1; index < minors.length; index += 1)
          expect(
            minors[index]!.x - minors[index - 1]!.x,
          ).toBeGreaterThanOrEqual(6 - 1e-9);
        for (const tick of minors) {
          expect(majorFrames.has(tick.frame)).toBe(false);
          expect(tick.x).toBeCloseTo(
            frameBoundaryToX(tick.frame, viewStart, scale),
            9,
          );
        }
        for (let index = 1; index < majors.length; index += 1)
          expect(
            (majors[index]!.frame - majors[index - 1]!.frame) * scale,
          ).toBeGreaterThanOrEqual(RULER_MIN_SPACING_PX);
      }
    }
  });
});

// B-M2562-08: the approved canvas labels every whole second and restarts the sub-second labels at
// each second (States "Ruler scales"). A stride counted in 24 fps frames from frame 0 put marks at
// 5 and 10 frames, so at 8 and 16 px per frame no second was labelled ("06f", "01f"), and around
// Fit (12, 24, 48 frames) none was at 25 or 30 fps ("24f", "23f" at 25 fps Fit).
describe("ruler marks land on whole seconds at the project's own frame rate", () => {
  const input = (fps: number, pixelsPerFrame: number, viewStart = 0) => ({
    viewStart,
    laneWidth: 1_200,
    pixelsPerFrame,
    durationFrames: 400_000,
    fps,
  });
  const strideOf = (fps: number, scale: number) => {
    const marks = rulerMarks(input(fps, scale));
    return marks[1]!.frame - marks[0]!.frame;
  };

  it("divides the whole frame rate below a second and counts whole seconds above it", () => {
    for (const fps of [24, 25, 30, 23.976, 29.97, 50, 60]) {
      const whole = Math.round(fps);
      for (const scale of [0.08, 0.25, 0.5, 1, 2.5, 5.8, 6, 8, 9, 12, 16]) {
        for (const viewStart of [0, 37, 1_000.5]) {
          const marks = rulerMarks(input(fps, scale, viewStart));
          expect(marks.length, `${fps} fps at ${scale}`).toBeGreaterThan(1);
          const stride = marks[1]!.frame - marks[0]!.frame;
          if (stride < whole) expect(whole % stride, `${fps}/${scale}`).toBe(0);
          else expect(stride % whole, `${fps}/${scale}`).toBe(0);
          expect(stride * scale).toBeGreaterThanOrEqual(RULER_MIN_SPACING_PX);
          // Every whole second inside the marked span is itself a mark, labelled MM:SS.
          const frames = new Set(marks.map((mark) => mark.frame));
          if (stride <= whole)
            for (
              let second = Math.ceil(marks[0]!.frame / whole) * whole;
              second <= marks.at(-1)!.frame;
              second += whole
            ) {
              expect(frames.has(second), `${fps}/${scale}: ${second}`).toBe(
                true,
              );
              expect(formatRulerLabel(second, fps)).toMatch(/^\d+:\d\d$/u);
            }
        }
      }
    }
  });

  it("takes the smallest such stride that keeps labels 72 px apart", () => {
    expect(strideOf(24, 16)).toBe(6);
    expect(strideOf(24, 8)).toBe(12);
    expect(strideOf(24, 6)).toBe(12);
    expect(strideOf(24, 5.8)).toBe(24);
    expect(strideOf(24, 0.25)).toBe(15 * 24);
    expect(strideOf(30, 16)).toBe(5);
    expect(strideOf(30, 8)).toBe(10);
    expect(strideOf(30, 5.8)).toBe(15);
    expect(strideOf(25, 16)).toBe(5);
    expect(strideOf(25, 8)).toBe(25);
    expect(strideOf(25, 0.25)).toBe(15 * 25);
  });

  it("reads 00:00, 12f, 00:01, 12f, 00:02 at 24 fps and 8 px per frame", () => {
    expect(
      rulerMarks(input(24, 8))
        .slice(0, 5)
        .map((mark) => formatRulerLabel(mark.frame, 24)),
    ).toEqual(["00:00", "12f", "00:01", "12f", "00:02"]);
  });
});
