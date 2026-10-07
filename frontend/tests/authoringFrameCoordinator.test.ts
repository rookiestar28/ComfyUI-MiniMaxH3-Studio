import { describe, expect, it, vi } from "vitest";

import {
  createAuthoringFrameCoordinator,
  observePreviewSeconds,
  previewSecondsForTimelineFrame,
  type AuthoringPreviewIdentity,
} from "../src/host/authoringFrameCoordinator";
import type { AuthoringPreviewRequest } from "../src/contracts/authoringPreviewCodec";

const identity = (overrides: Partial<AuthoringPreviewIdentity> = {}) => ({
  workspaceHandle: "authoring-1",
  referenceRevision: 4,
  timelineRevision: 2,
  timelineContentFingerprint: "sha256:" + "a".repeat(64),
  clipId: "clip-1",
  startFrame: 10,
  frames: 24,
  sourceStartFrame: 30,
  fps: 24,
  ...overrides,
});

describe("authoring frame mapping", () => {
  it("uses the zero-based normalized clip clock and a half-open end", () => {
    expect(previewSecondsForTimelineFrame(identity(), 10)).toBe(0);
    expect(previewSecondsForTimelineFrame(identity(), 33)).toBe(23 / 24);
    expect(() => previewSecondsForTimelineFrame(identity(), 34)).toThrow(
      /half-open/,
    );
    expect(observePreviewSeconds(identity(), 0)).toEqual({
      timelineFrame: 10,
      clipLocalFrame: 0,
      sourceFrame: 30,
    });
    expect(observePreviewSeconds(identity(), 23 / 24)).toEqual({
      timelineFrame: 33,
      clipLocalFrame: 23,
      sourceFrame: 53,
    });
  });

  it("rounds once, clamps, and stays within the frozen 24-to-15 fps budget", () => {
    expect(observePreviewSeconds(identity(), 0.5 / 24).timelineFrame).toBe(11);
    let maximum = 0;
    const maximumClip = identity({ frames: 900 });
    for (let frame = 0; frame < 900; frame += 1) {
      for (const sampleFrame of [
        Math.floor((15 * frame) / 24),
        Math.ceil((15 * frame) / 24),
      ])
        maximum = Math.max(
          maximum,
          Math.abs(
            observePreviewSeconds(maximumClip, sampleFrame / 15)
              .clipLocalFrame - frame,
          ),
        );
    }
    expect(maximum).toBe(1);
  });
});

describe("authoring frame coordinator lifecycle", () => {
  it("opens active then one warm URL, promotes without a third allocation, and revokes in order", async () => {
    const openPreview = vi.fn(async (request: AuthoringPreviewRequest) => ({
      blob: new Blob([request.clipId === "clip-1" ? "active" : "neighbor"]),
      audioDisposition:
        request.clipId === "clip-1"
          ? ("absent" as const)
          : ("present_bound" as const),
    }));
    const createObjectURL = vi.fn((blob: Blob) => `blob:${blob.size}`);
    const revokeObjectURL = vi.fn();
    const coordinator = createAuthoringFrameCoordinator({
      openPreview,
      createObjectURL,
      revokeObjectURL,
      changed: vi.fn(),
    });

    await coordinator.open(
      identity(),
      identity({ clipId: "clip-2", startFrame: 34 }),
    );
    expect(openPreview.mock.calls.map(([request]) => request.clipId)).toEqual([
      "clip-1",
      "clip-2",
    ]);
    expect(coordinator.snapshot()).toMatchObject({
      status: "ready",
      clipId: "clip-1",
      objectUrl: "blob:6",
      warmStatus: "ready",
      warmClipId: "clip-2",
      warmObjectUrl: "blob:8",
    });

    expect(coordinator.promoteWarm()).toBe(true);
    expect(createObjectURL).toHaveBeenCalledTimes(2);
    expect(revokeObjectURL).toHaveBeenNthCalledWith(1, "blob:6");
    expect(coordinator.snapshot()).toMatchObject({
      status: "ready",
      clipId: "clip-2",
      objectUrl: "blob:8",
      audioDisposition: "present_bound",
      playheadFrame: 34,
      warmStatus: "idle",
    });
    coordinator.close();
    expect(revokeObjectURL).toHaveBeenNthCalledWith(2, "blob:8");
  });

  it("owns one request and URL and discards a late replaced result", async () => {
    const resolvers: Array<
      (value: { blob: Blob; audioDisposition: "absent" }) => void
    > = [];
    const openPreview = vi.fn(
      (_request: unknown, _signal: AbortSignal) =>
        new Promise<{ blob: Blob; audioDisposition: "absent" }>((resolve) =>
          resolvers.push(resolve),
        ),
    );
    const createObjectURL = vi.fn((blob: Blob) => `blob:${blob.size}`);
    const revokeObjectURL = vi.fn();
    const changed = vi.fn();
    const coordinator = createAuthoringFrameCoordinator({
      openPreview,
      createObjectURL,
      revokeObjectURL,
      changed,
    });

    const first = coordinator.open(identity());
    const second = coordinator.open(identity({ clipId: "clip-2" }));
    resolvers[0]({ blob: new Blob(["old"]), audioDisposition: "absent" });
    await first;
    expect(createObjectURL).not.toHaveBeenCalled();
    resolvers[1]({ blob: new Blob(["new"]), audioDisposition: "absent" });
    await second;
    expect(coordinator.snapshot()).toMatchObject({
      status: "ready",
      clipId: "clip-2",
      objectUrl: "blob:3",
      playheadFrame: 10,
      clipLocalFrame: 0,
      sourceFrame: 30,
    });
    coordinator.observe(0.5, "paused");
    expect(coordinator.snapshot()).toMatchObject({
      status: "paused",
      playheadFrame: 22,
      clipLocalFrame: 12,
      sourceFrame: 42,
    });

    coordinator.close();
    coordinator.close();
    expect(revokeObjectURL).toHaveBeenCalledTimes(1);
    expect(coordinator.snapshot().status).toBe("idle");
  });

  it("maps content-free transport failures and never mutates revisions", async () => {
    const coordinator = createAuthoringFrameCoordinator({
      openPreview: async () => {
        throw Object.assign(new Error("private"), { disposition: "stale" });
      },
      createObjectURL: vi.fn(),
      revokeObjectURL: vi.fn(),
      changed: vi.fn(),
    });
    await coordinator.open(identity());
    expect(coordinator.snapshot()).toMatchObject({
      status: "unavailable",
      reason: "stale",
      playheadFrame: 10,
    });
  });

  it("keeps sixteen replacement cycles within two live URLs and cleans every owner", async () => {
    const live = new Set<string>();
    const signals: AbortSignal[] = [];
    let sequence = 0;
    let maximumLive = 0;
    const coordinator = createAuthoringFrameCoordinator({
      openPreview: async (_request, signal) => {
        signals.push(signal);
        return { blob: new Blob(["mp4"]), audioDisposition: "absent" };
      },
      createObjectURL: () => {
        const url = `blob:cycle-${sequence++}`;
        live.add(url);
        maximumLive = Math.max(maximumLive, live.size);
        return url;
      },
      revokeObjectURL: (url) => {
        expect(live.delete(url)).toBe(true);
      },
      changed: vi.fn(),
    });

    for (let cycle = 0; cycle < 16; cycle += 1)
      await coordinator.open(
        identity({ clipId: `active-${cycle}` }),
        identity({ clipId: `warm-${cycle}`, startFrame: 34 }),
      );
    expect(maximumLive).toBe(2);
    expect(live.size).toBe(2);
    coordinator.close();
    expect(live.size).toBe(0);
    expect(signals).toHaveLength(32);
    expect(signals.every((signal) => signal.aborted)).toBe(true);
  });
});
