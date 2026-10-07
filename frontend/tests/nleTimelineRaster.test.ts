import { describe, expect, it, vi } from "vitest";

import {
  createTimelineRasterScheduler,
  paintTimelineDecorations,
  type TimelineDecorationScene,
} from "../src/runtime/nleTimelineRaster";

describe("timeline decoration raster", () => {
  it("paints filmstrip, waveform, marquee, snap and refused ghost with one bounded surface", () => {
    const calls: string[] = [];
    const context = {
      clearRect: () => calls.push("clear"),
      drawImage: () => calls.push("filmstrip"),
      beginPath: () => calls.push("begin"),
      moveTo: () => calls.push("move"),
      lineTo: () => calls.push("line"),
      stroke: () => calls.push("stroke"),
      strokeRect: () => calls.push("strokeRect"),
      fillRect: () => calls.push("fillRect"),
      setLineDash: vi.fn(),
      strokeStyle: "",
      fillStyle: "",
      lineWidth: 1,
    } as unknown as CanvasRenderingContext2D;
    paintTimelineDecorations(context, {
      width: 800,
      height: 300,
      filmstrips: [
        {
          asset: {
            assetId: "video-1",
            kind: "video",
            sourceWidth: 160,
            sourceHeight: 90,
            sourceTimeBase: { num: 1, den: 24 },
            sourceFrameCount: 4,
            landmarks: Array.from({ length: 4 }, (_, frameIndex) => ({
              frameIndex,
              pts: frameIndex,
              dts: frameIndex,
              durationTicks: 1,
            })),
          },
          outputFrameRate: { num: 24, den: 1 },
          clip: { sourceStartFrame: 0, durationFrames: 4 },
          bounds: { x: 20, y: 60, width: 100, height: 40 },
          visible: { x: 0, width: 800 },
          sprite: {
            bitmap: {} as ImageBitmap,
            width: 320,
            height: 48,
            tileCount: 4,
          },
        },
      ],
      waveforms: [
        {
          asset: {
            assetId: "video-1",
            kind: "video",
            embeddedAudio: "present_bound",
            sourceSampleCount: 48_000,
            sourceTimeBase: { num: 1, den: 24 },
            sourceFrameCount: 4,
            landmarks: Array.from({ length: 4 }, (_, frameIndex) => ({
              frameIndex,
              pts: frameIndex,
              dts: frameIndex,
              durationTicks: 1,
            })),
          },
          outputFrameRate: { num: 24, den: 1 },
          clip: { sourceStartFrame: 0, durationFrames: 4 },
          bounds: { x: 20, y: 86, width: 100, height: 12 },
          visible: { x: 0, width: 800 },
          envelope: {
            version: 1,
            pairRate: 100,
            sampleRate: 8_000,
            sampleCount: 8_000,
            pairCount: 100,
            pairs: new Int8Array(200),
            byteLength: 220,
          },
        },
      ],
      marquee: { x: 5, y: 50, width: 150, height: 60 },
      snapX: 80,
      ghost: { x: 140, y: 60, width: 100, height: 40, admitted: false },
    });
    expect(calls).toContain("clear");
    expect(calls.slice(0, 3)).toEqual(["clear", "filmstrip", "filmstrip"]);
    // The snap guide is the one line path, painted after the marquee rectangle.
    const lastRect = calls.lastIndexOf("strokeRect");
    const snapBegin = calls.indexOf("begin", calls.indexOf("strokeRect"));
    expect(snapBegin).toBeGreaterThan(calls.indexOf("strokeRect"));
    // The waveform band paints its own columns first; after the marquee only the snap line moves.
    expect(
      calls
        .slice(calls.indexOf("strokeRect"))
        .filter((call) => call === "move"),
    ).toHaveLength(1);
    expect(lastRect).toBeGreaterThan(snapBegin);
    // M25-62: marquee and ghost only. Clip borders and the selection ring are DOM above the
    // raster; the raster no longer strokes a hard-coded outline per clip.
    expect(calls.filter((call) => call === "strokeRect")).toHaveLength(2);
    expect(context.setLineDash).toHaveBeenNthCalledWith(1, [3, 3]);
    expect(context.setLineDash).toHaveBeenLastCalledWith([]);
    expect(context.strokeStyle).toBe("#fb7185");

    paintTimelineDecorations(context, {
      width: 800,
      height: 300,
      filmstrips: [],
      waveforms: [],
      marquee: null,
      snapX: null,
      ghost: { x: 140, y: 60, width: 100, height: 40, admitted: true },
    });
    expect(context.setLineDash).toHaveBeenNthCalledWith(3, [6, 4]);
    expect(context.strokeStyle).toBe("#4ade80");
  });

  it("M25-62: strokes the waveform band in the scene's resolved role colour", () => {
    const styles: string[] = [];
    let strokeStyle = "";
    const context = {
      clearRect: () => undefined,
      drawImage: () => undefined,
      beginPath: () => undefined,
      moveTo: () => undefined,
      lineTo: () => undefined,
      stroke: () => styles.push(strokeStyle),
      strokeRect: () => undefined,
      fillRect: () => undefined,
      setLineDash: () => undefined,
      get strokeStyle() {
        return strokeStyle;
      },
      set strokeStyle(value: string) {
        strokeStyle = value;
      },
      fillStyle: "",
      lineWidth: 1,
    } as unknown as CanvasRenderingContext2D;
    const waveform = {
      asset: {
        assetId: "video-1",
        kind: "video" as const,
        embeddedAudio: "present_bound" as const,
        sourceSampleCount: 48_000,
        sourceTimeBase: { num: 1, den: 24 },
        sourceFrameCount: 4,
        landmarks: Array.from({ length: 4 }, (_, frameIndex) => ({
          frameIndex,
          pts: frameIndex,
          dts: frameIndex,
          durationTicks: 1,
        })),
      },
      outputFrameRate: { num: 24, den: 1 },
      clip: { sourceStartFrame: 0, durationFrames: 4 },
      bounds: { x: 20, y: 86, width: 100, height: 14 },
      visible: { x: 0, width: 800 },
      envelope: {
        version: 1 as const,
        pairRate: 100 as const,
        sampleRate: 8_000 as const,
        sampleCount: 8_000,
        pairCount: 100,
        pairs: new Int8Array(200).fill(40),
        byteLength: 220,
      },
    };
    paintTimelineDecorations(context, {
      width: 800,
      height: 300,
      filmstrips: [],
      waveforms: [waveform],
      waveformColor: "#3ecf8e",
      marquee: null,
      snapX: null,
      ghost: null,
    });
    expect(styles).toEqual(["#3ecf8e"]);
  });

  it("M25-61: paints no periodic full-height line; only the snap guide spans the surface", () => {
    // RED for the owner's 2026-09-26 report: a vertical line every ruler mark was painted through
    // every track and clip. Ticks belong to the ruler only; a scene that still carries a legacy
    // `ticks` list must not reach the canvas.
    let pen = { x: 0, y: 0 };
    const segments: Array<{ x0: number; y0: number; x1: number; y1: number }> =
      [];
    const context = {
      clearRect: () => undefined,
      drawImage: () => undefined,
      beginPath: () => undefined,
      moveTo: (x: number, y: number) => {
        pen = { x, y };
      },
      lineTo: (x: number, y: number) => {
        segments.push({ x0: pen.x, y0: pen.y, x1: x, y1: y });
        pen = { x, y };
      },
      stroke: () => undefined,
      strokeRect: () => undefined,
      fillRect: () => undefined,
      setLineDash: () => undefined,
      strokeStyle: "",
      fillStyle: "",
      lineWidth: 1,
    } as unknown as CanvasRenderingContext2D;
    const legacyScene = {
      width: 800,
      height: 300,
      filmstrips: [],
      waveforms: [],
      ticks: [10, 80, 150, 220],
      clipOutlines: [{ x: 20, y: 60, width: 100, height: 40, selected: false }],
      marquee: null,
      snapX: 200,
      ghost: null,
    };
    paintTimelineDecorations(context, legacyScene as TimelineDecorationScene);
    const spanning = segments.filter(
      (segment) =>
        segment.x0 === segment.x1 &&
        Math.min(segment.y0, segment.y1) <= 0 &&
        Math.max(segment.y0, segment.y1) >= legacyScene.height,
    );
    expect(spanning.map((segment) => segment.x0)).toEqual([200]);
  });

  it("coalesces view and draft paints, ignores playback alone and tears down pending work", () => {
    const callbacks: FrameRequestCallback[] = [];
    let nextFrame = 0;
    const request = vi
      .spyOn(window, "requestAnimationFrame")
      .mockImplementation((callback) => {
        callbacks.push(callback);
        nextFrame += 1;
        return nextFrame;
      });
    const cancel = vi
      .spyOn(window, "cancelAnimationFrame")
      .mockImplementation(() => undefined);
    const canvas = document.createElement("canvas");
    const scene: TimelineDecorationScene = {
      width: 640,
      height: 240,
      filmstrips: [],
      waveforms: [],
      marquee: null,
      snapX: null,
      ghost: null,
    };
    const read = vi.fn(() => scene);
    const scheduler = createTimelineRasterScheduler(canvas, read);

    scheduler.request();
    scheduler.request();
    expect(request).toHaveBeenCalledTimes(1);
    expect(read).not.toHaveBeenCalled();
    callbacks.shift()!(0);
    expect(read).toHaveBeenCalledTimes(1);
    expect(canvas.width).toBeLessThanOrEqual(1280);

    // Presented-frame playback does not enter the decoration scene, so no request occurs here.
    expect(request).toHaveBeenCalledTimes(1);
    scheduler.request();
    scheduler.dispose();
    expect(cancel).toHaveBeenCalledWith(2);
    expect(canvas.width).toBe(1);
    expect(canvas.height).toBe(1);

    request.mockRestore();
    cancel.mockRestore();
  });
});
