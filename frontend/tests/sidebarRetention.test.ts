// M25-21 section 14.4: the bounded session retention store. Restore is exact-scope only, one
// snapshot per slot, plain bounded data only, and complete disposal fences every older writer.

import { describe, expect, it } from "vitest";

import {
  UNSCOPED,
  createSidebarRetention,
  samePlain,
} from "../src/state/sidebarRetention";

describe("M25-21 Sidebar retention store", () => {
  it("restores a slot only for the exact scope it was written under", () => {
    const retention = createSidebarRetention();
    const generation = retention.generation();
    retention.write(
      "production.draft",
      "ws-a@3#seg-2",
      { relation: "cut", predecessor: "seg-1" },
      generation,
    );
    retention.write(
      "production.draft",
      "ws-a@3#seg-2",
      { moveTargets: { "seg-2": "4" } },
      generation,
    );
    expect(retention.restore("production.draft", "ws-a@3#seg-2")).toEqual({
      value: {
        relation: "cut",
        predecessor: "seg-1",
        moveTargets: { "seg-2": "4" },
      },
      discarded: false,
      staleScope: null,
    });
    // A changed revision is a different authority: nothing restores and the loss is reported,
    // naming the scope the lost draft was typed against. Restore is a pure read; the reporter
    // drops the stale entry once it has been reported.
    expect(retention.restore("production.draft", "ws-a@4#seg-2")).toEqual({
      value: undefined,
      discarded: true,
      staleScope: "ws-a@3#seg-2",
    });
    retention.drop("production.draft", "ws-a@4#seg-2", generation);
    expect(retention.restore("production.draft", "ws-a@4#seg-2")).toEqual({
      value: undefined,
      discarded: false,
      staleScope: null,
    });
    expect(retention.restore("production.draft", "ws-a@3#seg-2")).toEqual({
      value: undefined,
      discarded: false,
      staleScope: null,
    });
  });

  it("compares retained plain data structurally", () => {
    expect(
      samePlain(
        { a: 1, nested: { b: [1, 2], c: null } },
        { nested: { c: null, b: [1, 2] }, a: 1 },
      ),
    ).toBe(true);
    expect(samePlain({ a: 1 }, { a: 1, b: undefined })).toBe(false);
    expect(samePlain({ a: [1, 2] }, { a: [2, 1] })).toBe(false);
    expect(samePlain({ a: [] }, { a: {} })).toBe(false);
    expect(samePlain({ a: "1" }, { a: 1 })).toBe(false);
  });

  it("drops a view setting silently and keeps one snapshot per slot", () => {
    const retention = createSidebarRetention();
    const generation = retention.generation();
    retention.write(
      "nle.timeline",
      "handle-a",
      { zoomIndex: 4, scrollTop: 112 },
      generation,
    );
    retention.write("nle.timeline", "handle-b", { zoomIndex: 1 }, generation);
    expect(retention.size()).toBe(1);
    expect(retention.restore("nle.timeline", "handle-a")).toEqual({
      value: undefined,
      discarded: false,
      staleScope: null,
    });
  });

  it("refuses live handles, DOM references and oversized values", () => {
    const retention = createSidebarRetention();
    const generation = retention.generation();
    const refused: unknown[] = [
      { trackId: document.createElement("div") },
      { trackId: new AbortController() },
      { trackId: () => undefined },
      { trackId: "x".repeat(4_097) },
      { order: Number.NaN },
      {
        pairAudioBy: Object.fromEntries(
          Array.from({ length: 129 }, (_, index) => [`clip-${index}`, "a"]),
        ),
      },
    ];
    for (const value of refused)
      retention.write(
        "nle.inspector.view",
        "handle-a",
        value as { activeTab: "basic" },
        generation,
      );
    expect(retention.size()).toBe(0);
    retention.write(
      "nle.inspector.text",
      "handle-a#clip-1@7#text",
      {
        content: "a distinctive unsent title",
        style: {
          font_asset_id: "font-1",
          size_px: 48,
          weight: 700,
          style: "italic",
          align: "left",
          line_height_bp: 12_000,
          fill_rgba: [255, 0, 0, 255],
          background_rgba: null,
        },
      },
      generation,
    );
    expect(
      retention.restore("nle.inspector.text", "handle-a#clip-1@7#text").value,
    ).toEqual({
      content: "a distinctive unsent title",
      style: expect.objectContaining({ fill_rgba: [255, 0, 0, 255] }),
    });
  });

  it("clears on complete disposal and fences writers from the disposed generation", () => {
    const retention = createSidebarRetention();
    const before = retention.generation();
    retention.write(
      "navigation.function",
      UNSCOPED,
      { id: "clip_editor", requestGeneration: 0 },
      before,
    );
    retention.dispose();
    expect(retention.size()).toBe(0);
    // A late write from a view of the disposed session cannot revive it.
    retention.write(
      "navigation.function",
      UNSCOPED,
      { id: "clip_editor", requestGeneration: 0 },
      before,
    );
    retention.forget("navigation.function", before);
    expect(retention.size()).toBe(0);
    const after = retention.generation();
    expect(after).toBe(before + 1);
    retention.write(
      "navigation.function",
      UNSCOPED,
      { id: "production_workbench", requestGeneration: 0 },
      after,
    );
    expect(retention.restore("navigation.function", UNSCOPED).value).toEqual({
      id: "production_workbench",
      requestGeneration: 0,
    });
  });
});
