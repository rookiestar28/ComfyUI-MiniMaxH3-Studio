import { describe, expect, it } from "vitest";

import {
  gestureTransform,
  handleInsets,
  overlayGeometry,
  rotateTransform,
  type TransformGestureStart,
} from "../src/components/nle/nleTransformGesture";
import { IDENTITY_TRANSFORM } from "../src/components/nle/nleCommandBuilders";

const start: TransformGestureStart = Object.freeze({
  transform: IDENTITY_TRANSFORM,
  layer: Object.freeze({
    centerX: 960,
    centerY: 540,
    width: 960,
    height: 540,
    rotationMdeg: 0,
  }),
  picture: Object.freeze({
    left: 10,
    top: 20,
    width: 960,
    height: 540,
    outputWidth: 1920,
    outputHeight: 1080,
  }),
  pointer: Object.freeze({ x: 490, y: 290 }),
});

describe("M25-51 transform gesture math", () => {
  it("maps move deltas to one-basis-point picture units and clamps position", () => {
    expect(gestureTransform(start, "move", { x: 586, y: 344 })).toMatchObject({
      position_x_bp: 1000,
      position_y_bp: 1000,
    });
    expect(
      gestureTransform(start, "move", { x: 100_000, y: -100_000 }),
    ).toMatchObject({ position_x_bp: 40_000, position_y_bp: -40_000 });
  });

  it("scales a corner on local axes while retaining the opposite corner", () => {
    const cornerStart = {
      ...start,
      pointer: { x: 730, y: 425 },
    } as const;
    const next = gestureTransform(cornerStart, "south_east", {
      x: 850,
      y: 492.5,
    });
    expect(next).toMatchObject({
      scale_x_bp: 12_500,
      scale_y_bp: 12_500,
      position_x_bp: 625,
      position_y_bp: 625,
    });
  });

  it("changes only the owned axis for an edge handle", () => {
    const edgeStart = {
      ...start,
      pointer: { x: 730, y: 290 },
    } as const;
    expect(
      gestureTransform(edgeStart, "east", { x: 850, y: 450 }),
    ).toMatchObject({
      scale_x_bp: 12_500,
      scale_y_bp: 10_000,
      position_x_bp: 625,
      position_y_bp: 0,
    });
  });

  it("uses inverse rotation for scale and keeps the handle at a clamped boundary", () => {
    const rotated = {
      ...start,
      transform: { ...IDENTITY_TRANSFORM, rotation_mdeg: 90_000 },
      layer: { ...start.layer, rotationMdeg: 90_000 },
      pointer: { x: 490, y: 530 },
    } as const;
    const next = gestureTransform(rotated, "east", { x: 490, y: 650 });
    expect(next.scale_x_bp).toBe(12_500);
    expect(next.scale_y_bp).toBe(10_000);

    const clamped = gestureTransform(rotated, "east", { x: 490, y: 100_000 });
    expect(clamped.scale_x_bp).toBe(80_000);
    expect(clamped.scale_y_bp).toBe(10_000);
  });

  it("clamps rotation without wrapping", () => {
    expect(rotateTransform(IDENTITY_TRANSFORM, 220_000).rotation_mdeg).toBe(
      180_000,
    );
    expect(rotateTransform(IDENTITY_TRANSFORM, -220_000).rotation_mdeg).toBe(
      -180_000,
    );
  });

  it("projects output geometry into the measured picture box", () => {
    expect(overlayGeometry(start.layer, start.picture)).toEqual({
      centerX: 490,
      centerY: 290,
      width: 480,
      height: 270,
      rotationDegrees: 0,
    });
  });
});

// B-M2564-02: the side splitters overlap the monitor by 20 px, so a handle centred on a layer edge
// that meets the picture area's side is partly under a separator and half clipped. Such a handle is
// drawn a half-handle inward, and the scale gesture keeps the offset at which it was grabbed, so a
// press anywhere on a handle moves the edge by the pointer's travel and never jumps it.
describe("B-M2564-02 handles beside the picture's sides", () => {
  it("keeps the grab offset: a press 10 px inside the east edge scales like one on the edge", () => {
    const onEdge = gestureTransform(
      { ...start, pointer: { x: 730, y: 290 } },
      "east",
      { x: 850, y: 290 },
    );
    const inside = gestureTransform(
      { ...start, pointer: { x: 720, y: 300 } },
      "east",
      { x: 840, y: 300 },
    );
    expect(inside).toEqual(onEdge);
    // No travel, no change, wherever the handle was pressed.
    expect(
      gestureTransform(
        { ...start, pointer: { x: 712, y: 410 } },
        "south_east",
        { x: 712, y: 410 },
      ),
    ).toMatchObject({ scale_x_bp: 10_000, scale_y_bp: 10_000 });
  });

  it("keeps the grab offset on local axes for a rotated layer", () => {
    const rotated = {
      ...start,
      transform: { ...IDENTITY_TRANSFORM, rotation_mdeg: 90_000 },
      layer: { ...start.layer, rotationMdeg: 90_000 },
    } as const;
    const onEdge = gestureTransform(
      { ...rotated, pointer: { x: 490, y: 530 } },
      "east",
      { x: 490, y: 650 },
    );
    const inside = gestureTransform(
      { ...rotated, pointer: { x: 495, y: 520 } },
      "east",
      { x: 495, y: 640 },
    );
    expect(inside).toEqual(onEdge);
  });

  const area = Object.freeze({
    left: 0,
    top: 20,
    width: 960,
    height: 540,
    outputWidth: 1920,
    outputHeight: 1080,
  });
  const full = Object.freeze({
    centerX: 960,
    centerY: 540,
    width: 1920,
    height: 1080,
    rotationMdeg: 0,
  });

  it("insets the side handles of a layer that meets a width-limited picture's sides", () => {
    expect([...handleInsets(full, area)].sort()).toEqual([
      "east",
      "north_east",
      "north_west",
      "south_east",
      "south_west",
      "west",
    ]);
    // Only the side it meets.
    const right = { ...full, centerX: 1440, width: 960 };
    expect([...handleInsets(right, area)].sort()).toEqual([
      "east",
      "north_east",
      "south_east",
    ]);
  });

  it("leaves handles on the edge away from the sides, beside a letterbox and on a small layer", () => {
    expect(handleInsets(start.layer, start.picture).size).toBe(0);
    // Height-limited: the canvas is 100 px in from each side of its area.
    expect(handleInsets(full, { ...area, left: 100 }).size).toBe(0);
    // Narrower than three handles (132 area px): the move body keeps priority (M25-53 D-3).
    const small = { ...full, centerX: 1920 - 120, width: 240 };
    expect(overlayGeometry(small, area).width).toBe(120);
    expect(handleInsets(small, area).size).toBe(0);
  });

  it("follows the handles' rotated positions", () => {
    // Rotated a quarter turn, a full-height layer's north and south edges meet the sides.
    const turned = {
      ...full,
      width: 1080,
      height: 1920,
      rotationMdeg: 90_000,
      centerY: 540,
    };
    expect([...handleInsets(turned, area)].sort()).toEqual([
      "north",
      "north_east",
      "north_west",
      "south",
      "south_east",
      "south_west",
    ]);
  });
});
