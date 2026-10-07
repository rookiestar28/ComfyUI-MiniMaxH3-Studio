import { describe, expect, it } from "vitest";

import {
  DEFAULT_NLE_LAYOUT,
  NLE_LAYOUT_FLOOR_HEIGHT,
  NLE_LAYOUT_FLOOR_WIDTH,
  extremeLayout,
  layoutAria,
  layoutTier,
  moveSplitter,
  normalizeLayout,
  resetSplitter,
  resolveLayout,
  splitterRange,
  stepLayout,
} from "../src/runtime/nleLayoutGeometry";

// The stage of a 1402 x 868 viewport: 1370 x 836 dialog, 1 px borders, one 44 px chrome row.
const REFERENCE = { width: 1368, height: 790 };

function sum(boxes: ReturnType<typeof resolveLayout>) {
  return {
    width: boxes.bin + 4 + boxes.monitor + 4 + boxes.inspector,
    height: boxes.top + 4 + boxes.timeline,
  };
}

describe("M25-44 reference shell layout geometry", () => {
  it("chooses the tier from the workspace width at both boundaries", () => {
    expect(NLE_LAYOUT_FLOOR_WIDTH).toBe(688);
    expect(NLE_LAYOUT_FLOOR_HEIGHT).toBe(404);
    expect(layoutTier(687)).toBe("scroll_floor");
    expect(layoutTier(688)).toBe("narrow");
    expect(layoutTier(839)).toBe("narrow");
    expect(layoutTier(840)).toBe("standard");
    expect(layoutTier(Number.NaN)).toBe("scroll_floor");
  });

  it("resolves the default layout to the reference proportions", () => {
    const boxes = resolveLayout(DEFAULT_NLE_LAYOUT, REFERENCE);
    expect(boxes.tier).toBe("standard");
    expect(sum(boxes)).toEqual(REFERENCE);
    expect(boxes.bin / boxes.width).toBeCloseTo(0.21, 2);
    expect(boxes.inspector / boxes.width).toBeCloseTo(0.255, 2);
    expect(Math.abs(boxes.monitor / boxes.width - 0.525)).toBeLessThan(0.02);
    expect(Math.abs(boxes.top / boxes.height - 0.622)).toBeLessThan(0.02);
    expect(boxes.overflowX || boxes.overflowY).toBe(false);
  });

  it("holds the narrow minimums at the 720 x 480 floor with R2 absorbing", () => {
    // 720 - 2 x 8 margin - 2 border = 702; 480 - 16 - 2 - 44 = 418.
    const boxes = resolveLayout(DEFAULT_NLE_LAYOUT, {
      width: 702,
      height: 418,
    });
    expect(boxes.tier).toBe("narrow");
    expect(boxes.bin).toBeGreaterThanOrEqual(160);
    expect(boxes.monitor).toBeGreaterThanOrEqual(280);
    expect(boxes.inspector).toBeGreaterThanOrEqual(240);
    expect(boxes.top).toBeGreaterThanOrEqual(240);
    expect(boxes.timeline).toBeGreaterThanOrEqual(160);
    expect(sum(boxes)).toEqual({ width: 702, height: 418 });
    expect(boxes.overflowX || boxes.overflowY).toBe(false);
  });

  it("keeps the standard minimums at 840 and stops at the scroll floor below 688", () => {
    const standard = resolveLayout(DEFAULT_NLE_LAYOUT, {
      width: 840,
      height: 600,
    });
    expect([standard.bin, standard.monitor, standard.inspector]).toEqual([
      200, 352, 280,
    ]);
    const floor = resolveLayout(DEFAULT_NLE_LAYOUT, {
      width: 640,
      height: 300,
    });
    expect(floor.tier).toBe("scroll_floor");
    expect(floor.width).toBe(688);
    expect(floor.height).toBe(404);
    expect([floor.bin, floor.monitor, floor.inspector]).toEqual([
      160, 280, 240,
    ]);
    expect([floor.top, floor.timeline]).toEqual([240, 160]);
    expect(floor.overflowX && floor.overflowY).toBe(true);
  });

  it("never rewrites stored shares on resize, so proportions return when the stage grows", () => {
    const layout = moveSplitter(
      DEFAULT_NLE_LAYOUT,
      "bin_monitor",
      200,
      REFERENCE,
    );
    const wide = resolveLayout(layout, REFERENCE);
    const squeezed = resolveLayout(layout, { width: 702, height: 418 });
    expect(squeezed.monitor).toBe(280);
    expect(resolveLayout(layout, REFERENCE)).toEqual(wide);
  });

  it("moves each splitter by the pointer distance with its neighbours changing by the same amount", () => {
    const before = resolveLayout(DEFAULT_NLE_LAYOUT, REFERENCE);
    for (const delta of [60, -60]) {
      const s1 = resolveLayout(
        moveSplitter(DEFAULT_NLE_LAYOUT, "bin_monitor", delta, REFERENCE),
        REFERENCE,
      );
      expect(s1.bin - before.bin).toBe(delta);
      expect(s1.monitor - before.monitor).toBe(-delta);
      expect(s1.inspector).toBe(before.inspector);
      const s2 = resolveLayout(
        moveSplitter(DEFAULT_NLE_LAYOUT, "monitor_inspector", delta, REFERENCE),
        REFERENCE,
      );
      expect(s2.monitor - before.monitor).toBe(delta);
      expect(s2.inspector - before.inspector).toBe(-delta);
      expect(s2.bin).toBe(before.bin);
      const s3 = resolveLayout(
        moveSplitter(DEFAULT_NLE_LAYOUT, "top_timeline", delta, REFERENCE),
        REFERENCE,
      );
      expect(s3.top - before.top).toBe(delta);
      expect(s3.timeline - before.timeline).toBe(-delta);
    }
  });

  it("does not let a clamped requested share take the dragged distance twice", () => {
    const greedy = normalizeLayout({ bin: 0.21, inspector: 0.6, top: 0.62 });
    const before = resolveLayout(greedy, REFERENCE);
    expect(before.monitor).toBe(320);
    const after = resolveLayout(
      moveSplitter(greedy, "bin_monitor", -40, REFERENCE),
      REFERENCE,
    );
    expect(after.bin - before.bin).toBe(-40);
    expect(after.monitor - before.monitor).toBe(40);
    expect(after.inspector).toBe(before.inspector);
  });

  it("steps by 16 and 64 px and clamps at the tier limits", () => {
    const base = resolveLayout(DEFAULT_NLE_LAYOUT, REFERENCE);
    const right = resolveLayout(
      stepLayout(DEFAULT_NLE_LAYOUT, "bin_monitor", 1, false, REFERENCE),
      REFERENCE,
    );
    expect(right.bin - base.bin).toBe(16);
    const coarse = resolveLayout(
      stepLayout(DEFAULT_NLE_LAYOUT, "top_timeline", -1, true, REFERENCE),
      REFERENCE,
    );
    expect(base.top - coarse.top).toBe(64);
    const inspectorRight = resolveLayout(
      stepLayout(DEFAULT_NLE_LAYOUT, "monitor_inspector", 1, false, REFERENCE),
      REFERENCE,
    );
    expect(base.inspector - inspectorRight.inspector).toBe(16);
    let layout = DEFAULT_NLE_LAYOUT;
    for (let press = 0; press < 100; press += 1)
      layout = stepLayout(layout, "bin_monitor", -1, true, REFERENCE);
    expect(resolveLayout(layout, REFERENCE).bin).toBe(200);
  });

  it("moves to the primary pane's limits on Home and End and resets to the default", () => {
    const base = resolveLayout(DEFAULT_NLE_LAYOUT, REFERENCE);
    const home = resolveLayout(
      extremeLayout(DEFAULT_NLE_LAYOUT, "monitor_inspector", "min", REFERENCE),
      REFERENCE,
    );
    expect(home.inspector).toBe(280);
    const end = resolveLayout(
      extremeLayout(DEFAULT_NLE_LAYOUT, "monitor_inspector", "max", REFERENCE),
      REFERENCE,
    );
    expect(end.monitor).toBe(320);
    expect(end.bin).toBe(base.bin);
    const top = resolveLayout(
      extremeLayout(DEFAULT_NLE_LAYOUT, "top_timeline", "max", REFERENCE),
      REFERENCE,
    );
    expect(top.timeline).toBe(160);
    const moved = moveSplitter(
      DEFAULT_NLE_LAYOUT,
      "top_timeline",
      120,
      REFERENCE,
    );
    const reset = resolveLayout(
      resetSplitter(moved, "top_timeline", REFERENCE),
      REFERENCE,
    );
    expect(reset.top).toBe(base.top);
  });

  it("exposes integer ARIA percentages of the primary pane", () => {
    const boxes = resolveLayout(DEFAULT_NLE_LAYOUT, REFERENCE);
    const aria = layoutAria(boxes, "bin_monitor");
    expect(aria).toEqual({
      valueNow: Math.round((boxes.bin / boxes.width) * 100),
      valueMin: Math.round((200 / boxes.width) * 100),
      valueMax: Math.round(
        ((boxes.width - 8 - 320 - boxes.inspector) / boxes.width) * 100,
      ),
      sizePx: boxes.bin,
    });
    const range = splitterRange(boxes, "top_timeline");
    expect(range).toEqual({
      size: boxes.top,
      min: 240,
      max: boxes.height - 4 - 160,
      total: boxes.height,
    });
    for (const value of Object.values(layoutAria(boxes, "top_timeline")))
      expect(Number.isInteger(value)).toBe(true);
  });

  it("replaces invalid shares with defaults and ignores non-finite moves", () => {
    expect(
      normalizeLayout({ bin: 0, inspector: Number.NaN, top: 1.5 }),
    ).toEqual(DEFAULT_NLE_LAYOUT);
    expect(normalizeLayout(null)).toEqual(DEFAULT_NLE_LAYOUT);
    expect(
      resolveLayout(
        moveSplitter(DEFAULT_NLE_LAYOUT, "bin_monitor", Number.NaN, REFERENCE),
        REFERENCE,
      ),
    ).toEqual(resolveLayout(DEFAULT_NLE_LAYOUT, REFERENCE));
  });
});
