import { describe, expect, it, vi } from "vitest";

import { createNleBinInsertChannel } from "../src/runtime/nleBinInsertChannel";

const authority = {
  workspaceHandle: "authoring-" + "a".repeat(32),
  workspaceRevision: 1,
  timelineRevision: 2,
  timelineFingerprint: "sha256:" + "b".repeat(64),
  assetId: "private-asset",
  durationFrames: 48,
} as const;

describe("M25-48 bin insert pointer ownership", () => {
  it("requires a receiver before granting synchronous admission", () => {
    const channel = createNleBinInsertChannel();
    expect(channel.begin(1, 10, 20, authority)).toBe(false);
    expect(channel.activePointer()).toBeNull();
    const unsubscribe = channel.subscribe(() => undefined);
    expect(channel.begin(1, 10, 20, authority)).toBe(true);
    channel.cancel("finished", 1);
    unsubscribe();
    expect(channel.begin(2, 10, 20, authority)).toBe(false);
    expect(channel.activePointer()).toBeNull();
  });

  it("reports a receiver's synchronous refusal without leaving pointer ownership", () => {
    const channel = createNleBinInsertChannel();
    channel.subscribe((event) => {
      if (event.type === "begin")
        channel.cancel("stale_authority", event.pointerId);
    });
    expect(channel.begin(1, 10, 20, authority)).toBe(false);
    expect(channel.activePointer()).toBeNull();
  });

  it("allows the next pointer during release without granting the old begin", () => {
    const channel = createNleBinInsertChannel();
    channel.subscribe((event) => {
      if (event.type === "release") channel.begin(2, 30, 40, authority);
    });
    expect(channel.begin(1, 10, 20, authority)).toBe(true);
    channel.release(1, 20, 30);
    expect(channel.activePointer()).toBe(2);
    channel.cancel("old", 1);
    expect(channel.activePointer()).toBe(2);
    channel.cancel("next", 2);
    expect(channel.activePointer()).toBeNull();
  });

  it("does not grant a cancelled begin that was replaced using the same pointer ID", () => {
    const channel = createNleBinInsertChannel();
    let first = true;
    channel.subscribe((event) => {
      if (event.type === "begin" && first) {
        first = false;
        channel.cancel("replace", event.pointerId);
        expect(channel.begin(event.pointerId, 30, 40, authority)).toBe(true);
      }
    });
    expect(channel.begin(7, 0, 0, authority)).toBe(false);
    expect(channel.activePointer()).toBe(7);
    channel.cancel("finished", 7);
  });

  it("ignores a second pointer and clears ownership before release publication", () => {
    const channel = createNleBinInsertChannel();
    const events = vi.fn();
    channel.subscribe(events);
    expect(channel.begin(1, 10, 20, authority)).toBe(true);
    expect(channel.begin(2, 30, 40, authority)).toBe(false);
    channel.move(2, 31, 41);
    channel.move(1, 12, 22);
    channel.release(1, 50, 60);
    expect(channel.activePointer()).toBeNull();
    expect(events.mock.calls.map(([event]) => event.type)).toEqual([
      "begin",
      "move",
      "release",
    ]);
  });

  it("cancels only the owning pointer and publishes no later movement", () => {
    const channel = createNleBinInsertChannel();
    const events = vi.fn();
    channel.subscribe(events);
    channel.begin(7, 0, 0, authority);
    channel.cancel("wrong", 8);
    expect(channel.activePointer()).toBe(7);
    channel.cancel("escape", 7);
    channel.move(7, 5, 5);
    expect(events.mock.calls.at(-1)?.[0]).toMatchObject({
      type: "cancel",
      pointerId: 7,
      reason: "escape",
    });
    expect(events).toHaveBeenCalledTimes(2);
  });
});
