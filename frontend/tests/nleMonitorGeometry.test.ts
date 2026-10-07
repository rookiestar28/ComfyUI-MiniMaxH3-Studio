// M25-45: the monitor's picture rectangle. These are the arithmetic behind AC45-01 (width-limited
// fit) and AC45-02 (height-limited fit); the journeys assert the same rules against real dragged
// panes, where the areas are whatever the layout produces.

import { describe, expect, it } from "vitest";
import { computePictureBox } from "../src/components/nle/nleMonitorGeometry";

describe("M25-45 picture box", () => {
  it("fills the width and letterboxes when the area is wider than the composition", () => {
    // 1200 x 900 area, 16:9 composition: width-limited, so the picture is 1200 x 675.
    const box = computePictureBox(1200, 900, 1920, 1080);
    expect(box).toEqual({ width: 1200, height: 675, left: 0, top: 113 });
  });

  it("fills the height and pillarboxes when the area is taller than the composition", () => {
    // 1200 x 400 area, 16:9: height-limited, so the picture is 711 x 400.
    const box = computePictureBox(1200, 400, 1920, 1080);
    expect(box).toEqual({ width: 711, height: 400, left: 245, top: 0 });
  });

  it("grows the picture by the height the area gains while it stays height-limited", () => {
    const before = computePictureBox(1200, 400, 1920, 1080);
    const after = computePictureBox(1200, 500, 1920, 1080);
    expect(after.height - before.height).toBe(100);
    expect(after.width).toBeGreaterThan(before.width);
  });

  it("keeps the picture unchanged when a width-limited area only gets taller", () => {
    const before = computePictureBox(1200, 900, 1920, 1080);
    const after = computePictureBox(1200, 1000, 1920, 1080);
    expect(after.width).toBe(before.width);
    expect(after.height).toBe(before.height);
    expect(after.top).toBeGreaterThan(before.top);
  });

  it("grows a width-limited picture by the width the area gains", () => {
    const before = computePictureBox(1200, 900, 1920, 1080);
    const after = computePictureBox(1260, 900, 1920, 1080);
    expect(after.width - before.width).toBe(60);
  });

  it("fits a portrait composition into a landscape area by its height", () => {
    const box = computePictureBox(1200, 900, 1080, 1920);
    expect(box).toEqual({ width: 506, height: 900, left: 347, top: 0 });
  });

  it("shows one composition pixel per CSS pixel in the actual view", () => {
    const box = computePictureBox(1200, 900, 640, 360, "actual");
    expect(box).toEqual({ width: 640, height: 360, left: 280, top: 270 });
  });

  it("never scales a composition up in the actual view and clips it to the area", () => {
    // IMPORTANT: 100% is a view, not a resize. A composition larger than the area is clipped by
    // the area rather than letting the picture overflow the pane.
    const box = computePictureBox(1200, 900, 1920, 1080, "actual");
    expect(box.width).toBe(1200);
    expect(box.height).toBe(900);
  });

  it("reports no picture for a zero, negative or non-finite box", () => {
    const empty = { width: 0, height: 0, left: 0, top: 0 };
    expect(computePictureBox(0, 900, 1920, 1080)).toEqual(empty);
    expect(computePictureBox(1200, 0, 1920, 1080)).toEqual(empty);
    expect(computePictureBox(1200, 900, 0, 1080)).toEqual(empty);
    expect(computePictureBox(1200, 900, 1920, 0)).toEqual(empty);
    expect(computePictureBox(-1200, 900, 1920, 1080)).toEqual(empty);
    expect(computePictureBox(Number.NaN, 900, 1920, 1080)).toEqual(empty);
    expect(
      computePictureBox(1200, Number.POSITIVE_INFINITY, 1920, 1080),
    ).toEqual(empty);
  });

  it("keeps a picture of at least one pixel for an area smaller than a pixel", () => {
    const box = computePictureBox(0.4, 0.4, 1920, 1080);
    expect(box.width).toBe(1);
    expect(box.height).toBe(1);
  });
});
