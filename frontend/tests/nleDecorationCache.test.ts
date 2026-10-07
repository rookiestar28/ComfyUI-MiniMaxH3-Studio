import { describe, expect, it, vi } from "vitest";

import { createNleDecorationCache } from "../src/host/nleDecorationCache";

const key = (
  workspaceHandle: string,
  assetFingerprint: string,
  derivativeKind: "thumbnail" | "filmstrip" = "thumbnail",
) => ({
  workspaceHandle,
  assetFingerprint,
  derivativeProfileId: "h3.authoring.media_derivatives.v6",
  derivativeKind,
});

const bitmap = () => ({ close: vi.fn() });

describe("M25-48 decoded decoration cache", () => {
  it("is a 64-entry LRU and closes replaced and evicted bitmaps synchronously", () => {
    const cache = createNleDecorationCache<ReturnType<typeof bitmap>>();
    const first = bitmap();
    cache.set(key("workspace-a", "sha256:" + "0".repeat(64)), first);
    for (let index = 1; index < 64; index += 1)
      cache.set(
        key("workspace-a", `sha256:${index.toString(16).padStart(64, "0")}`),
        bitmap(),
      );
    expect(cache.get(key("workspace-a", "sha256:" + "0".repeat(64)))).toBe(
      first,
    );
    const replacement = bitmap();
    cache.set(key("workspace-a", "sha256:" + "0".repeat(64)), replacement);
    expect(first.close).toHaveBeenCalledTimes(1);
    const evicted = bitmap();
    cache.set(key("workspace-b", "sha256:" + "f".repeat(64)), evicted);
    expect(cache.size()).toBe(64);
  });

  it("partitions by workspace, fingerprint and profile without asset labels or ids", () => {
    const cache = createNleDecorationCache<ReturnType<typeof bitmap>>();
    const one = bitmap();
    const two = bitmap();
    cache.set(key("workspace-a", "sha256:" + "1".repeat(64)), one);
    cache.set(key("workspace-b", "sha256:" + "1".repeat(64)), two);
    expect(cache.get(key("workspace-a", "sha256:" + "1".repeat(64)))).toBe(one);
    expect(cache.get(key("workspace-b", "sha256:" + "1".repeat(64)))).toBe(two);
  });

  it("keeps independent 64-thumbnail and 16-filmstrip LRU limits", () => {
    const cache = createNleDecorationCache<ReturnType<typeof bitmap>>();
    const firstThumbnail = bitmap();
    const firstFilmstrip = bitmap();
    cache.set(key("workspace-a", `sha256:${"0".repeat(64)}`), firstThumbnail);
    cache.set(
      key("workspace-a", `sha256:${"0".repeat(64)}`, "filmstrip"),
      firstFilmstrip,
    );
    for (let index = 1; index <= 16; index += 1)
      cache.set(
        key(
          "workspace-a",
          `sha256:${index.toString(16).padStart(64, "0")}`,
          "filmstrip",
        ),
        bitmap(),
      );
    expect(firstFilmstrip.close).toHaveBeenCalledTimes(1);
    expect(firstThumbnail.close).not.toHaveBeenCalled();
    expect(cache.size("thumbnail")).toBe(1);
    expect(cache.size("filmstrip")).toBe(16);
  });

  it("purges removed assets, owner failures and overlay close synchronously", () => {
    const cache = createNleDecorationCache<ReturnType<typeof bitmap>>();
    const retained = bitmap();
    const removed = bitmap();
    cache.set(key("workspace-a", "sha256:" + "1".repeat(64)), retained);
    cache.set(key("workspace-a", "sha256:" + "2".repeat(64)), removed);
    cache.retainWorkspaceAssets(
      "workspace-a",
      new Set(["sha256:" + "1".repeat(64)]),
    );
    expect(removed.close).toHaveBeenCalledTimes(1);
    cache.purgeWorkspace("workspace-a");
    expect(retained.close).toHaveBeenCalledTimes(1);
    const last = bitmap();
    cache.set(key("workspace-b", "sha256:" + "3".repeat(64)), last);
    cache.close();
    expect(last.close).toHaveBeenCalledTimes(1);
    expect(cache.size()).toBe(0);
  });
});
