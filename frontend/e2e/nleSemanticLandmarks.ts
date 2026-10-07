// M25-20 browser landmark collector. Test support: nothing here is shipped, and nothing here is a
// debug route or a product API — the journey reads the same canvas the real monitor already
// presents to, using the real transport's own presentation signal.
//
// These functions mirror the pure Python extractor in
// `comfyui_h3_context/core/semantic_conformance_extract.py` deliberately and exactly. The whole
// value of the comparison is that the two sides measure the *same* thing by the same rule, so if
// one of them changes its patch geometry, its bit order or its edge-distance accounting, the other
// has to change with it. They are two implementations on purpose: the browser reads a live canvas,
// the extractor reads a decoded artifact, and neither derives its answer from the resolver.
//
// One difference is unavoidable and handled here rather than at the call sites: a canvas gives back
// RGBA, and the decoder gives back packed RGB24.

export type Rgba = Readonly<{
  data: Uint8ClampedArray;
  width: number;
  height: number;
}>;

export type PatchSample = Readonly<{
  label: string;
  sizePx: number;
  edgeDistancePx: number;
  red: number;
  green: number;
  blue: number;
}>;

export type GeometryLandmark = Readonly<{
  label: string;
  left: number;
  top: number;
  right: number;
  bottom: number;
}>;

const CHANNELS = 4;

function pixel(
  image: Rgba,
  x: number,
  y: number,
): readonly [number, number, number] {
  const offset = (y * image.width + x) * CHANNELS;
  return [image.data[offset], image.data[offset + 1], image.data[offset + 2]];
}

/**
 * Average an odd-sized interior patch and record the distance to the nearest frame edge.
 *
 * The edge distance is measured rather than asserted because the comparator rejects a patch that
 * reaches an antialiased boundary: a patch that quietly drifted to an edge would turn every colour
 * comparison into a comparison of edge blending, and would still look like a passing measurement.
 */
export function patchMean(
  image: Rgba,
  options: Readonly<{
    label: string;
    centerX: number;
    centerY: number;
    sizePx: number;
  }>,
): PatchSample {
  const { label, centerX, centerY, sizePx } = options;
  if (sizePx < 1 || sizePx % 2 === 0) {
    throw new Error("a patch size must be a positive odd number of pixels");
  }
  const radius = (sizePx - 1) / 2;
  const fits =
    centerX - radius >= 0 &&
    centerY - radius >= 0 &&
    centerX + radius < image.width &&
    centerY + radius < image.height;
  if (!fits) throw new Error(`patch ${label} does not fit inside the canvas`);
  if (image.data.length !== image.width * image.height * CHANNELS) {
    throw new Error("the canvas buffer is not the declared rgba size");
  }
  let red = 0;
  let green = 0;
  let blue = 0;
  for (let y = centerY - radius; y <= centerY + radius; y += 1) {
    for (let x = centerX - radius; x <= centerX + radius; x += 1) {
      const [r, g, b] = pixel(image, x, y);
      red += r;
      green += g;
      blue += b;
    }
  }
  const count = sizePx * sizePx;
  return {
    label,
    sizePx,
    edgeDistancePx: Math.min(
      centerX - radius,
      centerY - radius,
      image.width - 1 - centerX - radius,
      image.height - 1 - centerY - radius,
    ),
    red: Math.floor(red / count),
    green: Math.floor(green / count),
    blue: Math.floor(blue / count),
  };
}

/**
 * Recover the source frame identifier the fixture burned into a coarse binary tile.
 *
 * This is what lets the browser observation state which *source* frame was actually presented,
 * rather than reporting the frame the timeline intended. Cells are sampled at their centre, most
 * significant bit first, so a scaling filter's edge blending cannot flip a bit — the same rule the
 * extractor applies to the decoded artifact.
 */
export function readFrameIdTile(
  image: Rgba,
  options: Readonly<{
    originX: number;
    originY: number;
    cellPx: number;
    bits: number;
    threshold?: number;
  }>,
): number {
  const { originX, originY, cellPx, bits } = options;
  const threshold = options.threshold ?? 128;
  if (bits < 1 || bits > 32) {
    throw new Error("a frame identifier tile must carry between 1 and 32 bits");
  }
  let value = 0;
  for (let index = 0; index < bits; index += 1) {
    const centreX = originX + index * cellPx + Math.floor(cellPx / 2);
    const centreY = originY + Math.floor(cellPx / 2);
    if (
      centreX < 0 ||
      centreX >= image.width ||
      centreY < 0 ||
      centreY >= image.height
    ) {
      throw new Error("the frame identifier tile falls outside the canvas");
    }
    const [r, g, b] = pixel(image, centreX, centreY);
    if (Math.floor((r + g + b) / 3) >= threshold) {
      value |= 1 << (bits - 1 - index);
    }
  }
  return value;
}

/**
 * The bounding box of every pixel matching a fiducial colour within a tolerance.
 *
 * Returns `null` when the fiducial is absent. That is deliberately not an empty box at the origin:
 * an absent landmark is missing evidence, and a zero-sized box at (0,0) would be compared as if it
 * were an observation.
 */
export function fiducialBounds(
  image: Rgba,
  options: Readonly<{
    label: string;
    red: number;
    green: number;
    blue: number;
    tolerance: number;
  }>,
): GeometryLandmark | null {
  const { label, red, green, blue, tolerance } = options;
  let left = Number.POSITIVE_INFINITY;
  let top = Number.POSITIVE_INFINITY;
  let right = Number.NEGATIVE_INFINITY;
  let bottom = Number.NEGATIVE_INFINITY;
  for (let y = 0; y < image.height; y += 1) {
    for (let x = 0; x < image.width; x += 1) {
      const [r, g, b] = pixel(image, x, y);
      if (
        Math.abs(r - red) <= tolerance &&
        Math.abs(g - green) <= tolerance &&
        Math.abs(b - blue) <= tolerance
      ) {
        left = Math.min(left, x);
        top = Math.min(top, y);
        right = Math.max(right, x);
        bottom = Math.max(bottom, y);
      }
    }
  }
  if (right < left || bottom < top) return null;
  return { label, left, top, right, bottom };
}

/**
 * Read the canvas the real monitor presents to, at its actual backing dimensions.
 *
 * CRITICAL: `canvas.width`/`canvas.height` are used, never `getBoundingClientRect` or a CSS size.
 * The comparator's geometry tolerance is expressed in output pixels; measuring in CSS pixels would
 * silently rescale every coordinate with the viewport and make a two-pixel bound meaningless.
 */
export function readCanvas(canvas: HTMLCanvasElement): Rgba {
  const context = canvas.getContext("2d", { willReadFrequently: true });
  if (context === null) throw new Error("the monitor canvas has no 2d context");
  if (canvas.width <= 0 || canvas.height <= 0) {
    throw new Error("the monitor canvas has no backing dimensions");
  }
  const image = context.getImageData(0, 0, canvas.width, canvas.height);
  return { data: image.data, width: image.width, height: image.height };
}
