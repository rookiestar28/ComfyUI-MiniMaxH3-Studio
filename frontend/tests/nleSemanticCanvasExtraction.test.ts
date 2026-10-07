// M25-20 post-closeout corrective: the prescribed-frame observer, exercised on synthetic buffers.
//
// `observePrescribedFrame` and `alphasForTarget` run inside the render-and-browser journey, where
// nothing can assert against them. These tests pin the rules they port from the Python extractor
// (`scripts/nle_semantic_conformance.py`: `patch_mean`, `_read_identity`, `_alphas_for_clip`) so
// that a change to one side alone goes red on the other.

import { describe, expect, it } from "vitest";

import {
  alphasForTarget,
  observePrescribedFrame,
  type AlphaTarget,
  type MediaConstants,
  type PlainImageData,
  type Prescription,
} from "./e2e/helpers/nleSemanticCanvasExtraction";

function image(
  width: number,
  height: number,
  fill: readonly [number, number, number],
): {
  image: PlainImageData;
  set: (
    x: number,
    y: number,
    colour: readonly [number, number, number],
  ) => void;
} {
  const data = new Uint8ClampedArray(width * height * 4);
  for (let index = 0; index < width * height; index += 1) {
    data[index * 4] = fill[0];
    data[index * 4 + 1] = fill[1];
    data[index * 4 + 2] = fill[2];
    data[index * 4 + 3] = 255;
  }
  return {
    image: { data, width, height },
    set: (x, y, colour) => {
      const index = (y * width + x) * 4;
      data[index] = colour[0];
      data[index + 1] = colour[1];
      data[index + 2] = colour[2];
    },
  };
}

const CONSTANTS: MediaConstants = {
  schema: "test",
  source_width: 128,
  source_height: 128,
  geometry_luma_threshold: 40,
  patch_size_px: 16,
  frame_id_bits: 6,
  frame_id_origin_px: [0, 0],
  frame_id_cell_pitch_px: 6,
  frame_id_mark_px: 4,
  frame_id_one_rgb: [255, 255, 255],
  frame_id_zero_rgb: [0, 0, 0],
  audio_sample_rate: 48000,
  audio_burst_samples: 480,
  audio_burst_starts: [],
  source_profiles: [],
};

function prescription(partial: Partial<Prescription>): Prescription {
  return {
    presentation: {
      pane_css_width: 0,
      pane_css_height: 0,
      device_pixel_ratio: 1,
      backing_width: 0,
      backing_height: 0,
    },
    preview_scale: 1,
    frames: [],
    patch_points: [],
    identity_reads: [],
    geometry_targets: [],
    alpha_targets: [],
    ...partial,
  };
}

describe("observePrescribedFrame", () => {
  it("samples a prescribed point as a 5x5 mean and only at its own frame", () => {
    const { image: img, set } = image(40, 40, [10, 20, 30]);
    for (let y = 8; y <= 12; y += 1)
      for (let x = 8; x <= 12; x += 1) set(x, y, [200, 100, 50]);
    set(10, 10, [175, 100, 50]); // one pixel lower: the mean floors to 199
    const rx = prescription({
      frames: [3, 4],
      patch_points: [
        { label: "clip-a@3.mark", output_frame: 3, canvas_x: 10, canvas_y: 10 },
        { label: "clip-a@4.mark", output_frame: 4, canvas_x: 10, canvas_y: 10 },
      ],
    });
    const observed = observePrescribedFrame({
      image: img,
      frame: 3,
      prescription: rx,
      constants: CONSTANTS,
    });
    expect(observed.patches).toEqual([
      {
        label: "clip-a@3.mark",
        size_px: 5,
        edge_distance_px: 8,
        red: 199,
        green: 100,
        blue: 50,
      },
    ]);
    expect(observed.identity).toBeNull();
    expect(observed.geometry).toEqual([]);
  });

  it("refuses a point whose box does not fit rather than clamping it", () => {
    const { image: img } = image(40, 40, [10, 20, 30]);
    const rx = prescription({
      frames: [0],
      patch_points: [
        { label: "edge", output_frame: 0, canvas_x: 1, canvas_y: 20 },
      ],
    });
    const observed = observePrescribedFrame({
      image: img,
      frame: 0,
      prescription: rx,
      constants: CONSTANTS,
    });
    expect(observed.patches).toEqual([]);
  });

  it("reads the identity cells most significant bit first at luma 128", () => {
    const { image: img, set } = image(64, 16, [0, 0, 0]);
    // 6 cells at x = 2, 8, 14, ...; value 0b101001 = 41
    const cells: [number, number][] = [
      [2, 4],
      [8, 4],
      [14, 4],
      [20, 4],
      [26, 4],
      [32, 4],
    ];
    set(2, 4, [128, 128, 128]);
    set(14, 4, [255, 255, 255]);
    set(32, 4, [127, 128, 129]); // mean floors to 128: a one
    set(26, 4, [127, 127, 127]); // a zero
    const rx = prescription({
      frames: [7],
      identity_reads: [
        {
          output_frame: 7,
          clip_id: "clip-main",
          asset_id: "vid-primary",
          cells,
          pts_by_frame: { "41": 1681 },
          time_base_num: 1,
          time_base_den: 24000,
        },
      ],
    });
    const observed = observePrescribedFrame({
      image: img,
      frame: 7,
      prescription: rx,
      constants: CONSTANTS,
    });
    expect(observed.identity).toEqual({
      clip_id: "clip-main",
      asset_id: "vid-primary",
      source_frame: 41,
      source_pts: 1681,
      time_base_num: 1,
      time_base_den: 24000,
    });
  });

  it("samples an alpha target's point at each frame the target names and no other", () => {
    const { image: img } = image(40, 40, [100, 100, 100]);
    const target: AlphaTarget = {
      label: "clip-b",
      canvas_x: 20,
      canvas_y: 20,
      opacity_bp: 8000,
      base_frame: 10,
      steady_frame: 14,
      sample_frames: [10, 12, 13, 14, 30],
    };
    const rx = prescription({ frames: [10, 11, 30], alpha_targets: [target] });
    const at = (frame: number) =>
      observePrescribedFrame({
        image: img,
        frame,
        prescription: rx,
        constants: CONSTANTS,
      }).ramp_samples;
    expect(at(10)).toEqual([
      { label: "clip-b", output_frame: 10, rgb: [100, 100, 100] },
    ]);
    expect(at(30)).toHaveLength(1);
    expect(at(11)).toEqual([]);
  });
});

describe("alphasForTarget", () => {
  const target: AlphaTarget = {
    label: "clip-b",
    canvas_x: 20,
    canvas_y: 20,
    opacity_bp: 8500,
    base_frame: 10,
    steady_frame: 14,
    sample_frames: [10, 12, 13, 14, 30],
  };

  it("projects each sample's travel onto the full travel and scales by the declared opacity", () => {
    const samples = new Map<number, readonly [number, number, number]>([
      [10, [0, 0, 0]],
      [14, [200, 100, 0]],
      [12, [100, 50, 0]], // half way: 0.5 * 8500 / 10 = 425
      [13, [150, 75, 6]], // three quarters, with a stray blue that is orthogonal to the travel
      [30, [200, 100, 0]], // the plateau: exactly 1
    ]);
    expect(alphasForTarget(target, samples)).toEqual([
      { label: "clip-b", output_frame: 10, alpha_milli: 0 },
      { label: "clip-b", output_frame: 12, alpha_milli: 425 },
      { label: "clip-b", output_frame: 13, alpha_milli: 638 },
      { label: "clip-b", output_frame: 14, alpha_milli: 850 },
      { label: "clip-b", output_frame: 30, alpha_milli: 850 },
    ]);
  });

  it("is one least-squares ratio over the channels, not a mean of per-channel ratios", () => {
    // Red travels 100, blue travels 2; a per-channel mean would let the blue channel's noise
    // (observed 4 of 2) pull the answer to (0.5 + 2) / 2, the projection stays near 0.5.
    const samples = new Map<number, readonly [number, number, number]>([
      [10, [0, 0, 0]],
      [14, [100, 0, 2]],
      [12, [50, 0, 4]],
    ]);
    const [, mid] = alphasForTarget(
      { ...target, sample_frames: [10, 12] },
      samples,
    );
    expect(mid.alpha_milli).toBe(
      Math.round(((50 * 100 + 4 * 2) / 10004) * 850),
    );
  });

  it("reports nothing without both reference frames, or when the ramp barely moves the pixel", () => {
    const missing = new Map<number, readonly [number, number, number]>([
      [10, [0, 0, 0]],
      [12, [50, 0, 0]],
    ]);
    expect(alphasForTarget(target, missing)).toEqual([]);
    const flat = new Map<number, readonly [number, number, number]>([
      [10, [0, 0, 0]],
      [14, [2, 2, 2]],
      [12, [1, 1, 1]],
    ]);
    expect(alphasForTarget(target, flat)).toEqual([]);
  });
});
