import { describe, expect, it } from "vitest";

import {
  adjacentClipId,
  contiguousClipSelection,
  createTimelineView,
  ensureTimelineFrameVisible,
  fitTimelineContentView,
  formatTimelineTimecode,
  panTimelineView,
  selectedClipNavigationFrame,
  timelineRulerMarks,
  zoomTimelineView,
  zoomTimelineViewport,
} from "../src/runtime/timelineNavigation";

describe("authoring timeline navigation", () => {
  it("fits authored content with one lead-in and trailing pad instead of edit capacity", () => {
    expect(
      fitTimelineContentView({
        laneWidth: 640,
        contentEndExclusive: 240,
        editCapacityFrames: 3_600,
        fps: 24,
        lastValidScale: 1,
      }),
    ).toEqual({ pixelsPerFrame: 2.5, viewStart: 0, fitLocked: true });

    const empty = fitTimelineContentView({
      laneWidth: 640,
      contentEndExclusive: 0,
      editCapacityFrames: 3_600,
      fps: 24,
      lastValidScale: 1,
    });
    expect(empty).toEqual({
      pixelsPerFrame: 2.5,
      viewStart: 0,
      fitLocked: true,
    });
    expect(Number.isFinite(empty.pixelsPerFrame)).toBe(true);

    expect(
      fitTimelineContentView({
        laneWidth: 20,
        contentEndExclusive: 240,
        editCapacityFrames: 3_600,
        fps: 24,
        lastValidScale: 2,
      }),
    ).toEqual({ pixelsPerFrame: 2, viewStart: 0, fitLocked: true });
  });

  it("returns one atomic anchored zoom state with sub-pixel-stable time", () => {
    const current = { pixelsPerFrame: 2, viewStart: 100, fitLocked: true };
    const next = zoomTimelineViewport({
      current,
      laneWidth: 640,
      durationFrames: 3_600,
      nextScale: 4,
      anchorX: 300,
    });
    expect(next).toEqual({
      pixelsPerFrame: 4,
      viewStart: 175,
      fitLocked: false,
    });
    const anchorFrame = current.viewStart + 300 / current.pixelsPerFrame;
    const before = (anchorFrame - current.viewStart) * current.pixelsPerFrame;
    const after = (anchorFrame - next.viewStart) * next.pixelsPerFrame;
    expect(Math.abs(after - before)).toBeLessThanOrEqual(1);
  });

  it("uses one continuous pixels-per-frame model and finite frame windows", () => {
    expect(
      [0.25, 0.5, 1, 2].map(
        (pixelsPerFrame) =>
          createTimelineView({
            extentFrames: 3_600,
            pixelsPerFrame,
            laneWidth: 512,
          }).frameCount,
      ),
    ).toEqual([2_048, 1_024, 512, 256]);

    const anchored = zoomTimelineView({
      view: createTimelineView({
        extentFrames: 3_600,
        pixelsPerFrame: 0.5,
        laneWidth: 512,
      }),
      extentFrames: 3_600,
      pixelsPerFrame: 2,
      anchorFrame: 1_800,
    });
    expect(anchored).toMatchObject({
      pixelsPerFrame: 2,
      startFrame: 1_350,
      frameCount: 256,
      endFrameExclusive: 1_606,
    });
  });

  it("keeps targets visible and clamps grid-derived pan to the half-open extent", () => {
    const view = createTimelineView({
      extentFrames: 3_600,
      pixelsPerFrame: 0.5,
      laneWidth: 512,
    });
    expect(
      panTimelineView({
        view,
        extentFrames: 3_600,
        frameGrid: 51,
        direction: 1,
      }),
    ).toMatchObject({ startFrame: 306, endFrameExclusive: 1_330 });
    expect(
      ensureTimelineFrameVisible({ view, extentFrames: 3_600, frame: 3_599 }),
    ).toMatchObject({
      pixelsPerFrame: 0.5,
      startFrame: 2_576,
      frameCount: 1_024,
      endFrameExclusive: 3_600,
    });
    expect(
      panTimelineView({
        view: { ...view, startFrame: 2_576, endFrameExclusive: 3_600 },
        extentFrames: 3_600,
        frameGrid: 51,
        direction: 1,
      }).startFrame,
    ).toBe(2_576);
  });

  it("emits viewport-bounded human ruler marks with exact non-drop timecode", () => {
    const view = createTimelineView({
      extentFrames: 1_000_000,
      pixelsPerFrame: 0.25,
      laneWidth: 512,
    });
    const marks = timelineRulerMarks({ view, frameGrid: 51, fps: 24 });
    expect(marks.length).toBeLessThanOrEqual(10);
    expect(marks.length).toBeGreaterThan(1);
    expect(marks[0]).toEqual({ frame: 0, timecode: "00:00:00:00" });
    expect(formatTimelineTimecode(3_600, 24)).toBe("00:02:30:00");
    expect(formatTimelineTimecode(24 * 60 * 60 * 27, 24)).toBe("27:00:00:00");
  });

  it("navigates selected clip starts in stable order without wrapping", () => {
    const clips = [
      { clipId: "clip-c", startFrame: 200, lane: 0 },
      { clipId: "clip-a", startFrame: 0, lane: 1 },
      { clipId: "clip-b", startFrame: 100, lane: 0 },
    ] as const;
    expect(
      selectedClipNavigationFrame({
        clips,
        selectedClipIds: ["clip-c", "clip-a", "clip-b"],
        currentFrame: 0,
        direction: 1,
      }),
    ).toBe(100);
    expect(
      selectedClipNavigationFrame({
        clips,
        selectedClipIds: ["clip-c", "clip-a", "clip-b"],
        currentFrame: 150,
        direction: -1,
      }),
    ).toBe(100);
    expect(
      selectedClipNavigationFrame({
        clips,
        selectedClipIds: ["clip-c", "clip-a", "clip-b"],
        currentFrame: 200,
        direction: 1,
      }),
    ).toBeUndefined();
  });

  it("uses track, start-frame and ID order for logical focus and ranges", () => {
    const clips = [
      { clipId: "track-1-late", trackOrder: 1, startFrame: 900 },
      { clipId: "track-0-b", trackOrder: 0, startFrame: 20 },
      { clipId: "track-0-a", trackOrder: 0, startFrame: 20 },
      { clipId: "track-2-early", trackOrder: 2, startFrame: 0 },
    ] as const;
    expect(adjacentClipId(clips, "track-0-a", 1)).toBe("track-0-b");
    expect(adjacentClipId(clips, "track-0-b", 1)).toBe("track-1-late");
    expect(
      contiguousClipSelection(clips, "track-0-a", "track-2-early"),
    ).toEqual(["track-0-a", "track-0-b", "track-1-late", "track-2-early"]);
  });

  it("rejects non-integer domains and non-positive grid/fps values", () => {
    expect(() =>
      createTimelineView({
        extentFrames: 0,
        pixelsPerFrame: 1,
        laneWidth: 512,
      }),
    ).toThrow();
    expect(() =>
      timelineRulerMarks({
        view: createTimelineView({
          extentFrames: 100,
          pixelsPerFrame: 1,
          laneWidth: 512,
        }),
        frameGrid: 0,
        fps: 24,
      }),
    ).toThrow();
    expect(formatTimelineTimecode(1, 23.976)).toBe("00:00:00:01");
  });
});
