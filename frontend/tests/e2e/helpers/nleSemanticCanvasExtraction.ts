// M25-20 post-closeout corrective, browser half: pure landmark extraction from the monitor canvas's
// own backing pixels, at the points the composition prescribes. This is a port, rule for rule, of
// the independent extractor in `scripts/nle_semantic_conformance.py` (`observe_artifact`'s per-frame
// half): a colour sample is a 5x5 mean at a prescribed canvas point; the source identity is read
// at prescribed cells and timed by the asset's own declared table; a layer's geometry is a blind
// search -- every pixel classified to its nearest palette colour, the largest connected group of a
// patch's colour is its fiducial, and the luma-threshold box of the whole frame is the layer's
// rectangle once its own colours are present.
//
// GUARD: nothing here searches the canvas for a mark in order to decide *where to sample*. The
// prescription (`scripts/nle_semantic_browser_wires.py`, from
// `semantic_conformance_expect.patch_sample_points` / `identity_reads` / `geometry_targets`) says
// where to look, exactly as the render stage tells the Python extractor; an earlier version of this
// helper located every mark by colour and solved the identity row's position from the marks it
// found, which reads the right index out of a misplaced layer and measures a scaled overlay's mark
// wherever its colour happens to be. Geometry is the one deliberately blind search, because its
// question is where the layer actually landed.
//
// Every constant consumed here (palette colours, patch sizes, the identity encoding, the geometry
// luma threshold) is read at test setup out of `comfyui_h3_context.core.semantic_conformance_media`
// by `scripts/nle_semantic_browser_media.py --skip-media`; nothing restates a number that module
// owns.

export type MediaConstants = Readonly<{
  schema: string;
  source_width: number;
  source_height: number;
  geometry_luma_threshold: number;
  patch_size_px: number;
  frame_id_bits: number;
  frame_id_origin_px: readonly [number, number];
  frame_id_cell_pitch_px: number;
  frame_id_mark_px: number;
  frame_id_one_rgb: readonly [number, number, number];
  frame_id_zero_rgb: readonly [number, number, number];
  audio_sample_rate: number;
  audio_burst_samples: number;
  audio_burst_starts: readonly number[];
  source_profiles: readonly Readonly<{
    asset_id: string;
    field: readonly [number, number, number];
    field_sample: readonly [number, number];
    carries_frame_id: boolean;
    carries_audio: boolean;
    frame_count: number;
    patches: readonly Readonly<{
      label: string;
      left: number;
      top: number;
      red: number;
      green: number;
      blue: number;
      size: number;
    }>[];
  }>[];
}>;

/** One row's prescription, as `scripts/nle_semantic_browser_wires.py` emits it. */
export type Prescription = Readonly<{
  /**
   * How this row's base is presented: the picture area in CSS pixels, the device pixel ratio, and
   * the backing those two produce. Written by `semantic_conformance_cases.BASE_PRESENTATION`, so
   * the stage that decides a row is observable and the stage that presents it use one table.
   *
   * A zero `backing_width` means the default layout is enough -- the accepted 320 x 180 base is
   * inside the product's legacy floor at any picture box.
   */
  presentation: Readonly<{
    pane_css_width: number;
    pane_css_height: number;
    device_pixel_ratio: number;
    backing_width: number;
    backing_height: number;
  }>;
  preview_scale: number;
  frames: readonly number[];
  patch_points: readonly Readonly<{
    label: string;
    output_frame: number;
    canvas_x: number;
    canvas_y: number;
  }>[];
  identity_reads: readonly Readonly<{
    output_frame: number;
    /** The clip whose identity row is read: the element leased for it reports the source time. */
    clip_id: string;
    asset_id: string;
    cells: readonly (readonly [number, number])[];
    pts_by_frame: Readonly<Record<string, number>>;
    time_base_num: number;
    time_base_den: number;
  }>[];
  geometry_targets: readonly Readonly<{
    clip_id: string;
    asset_id: string;
    sample_frame: number;
  }>[];
  alpha_targets: readonly AlphaTarget[];
}>;

/** `semantic_conformance_expect.AlphaTarget`: where and between which frames a ramp is read. */
export type AlphaTarget = Readonly<{
  label: string;
  canvas_x: number;
  canvas_y: number;
  opacity_bp: number;
  base_frame: number;
  steady_frame: number;
  sample_frames: readonly number[];
}>;

export type ExtractedAlpha = Readonly<{
  label: string;
  output_frame: number;
  alpha_milli: number;
}>;

export type ExtractedPatch = Readonly<{
  label: string;
  size_px: number;
  edge_distance_px: number;
  red: number;
  green: number;
  blue: number;
}>;

export type ExtractedGeometry = Readonly<{
  label: string;
  left: number;
  top: number;
  right: number;
  bottom: number;
}>;

export type IdentityRead = Readonly<{
  clip_id: string;
  asset_id: string;
  /** The index the prescribed cells decoded to, or null when a cell fell outside the canvas. */
  source_frame: number | null;
  /** The asset's own declared pts for that index, or null when the table does not name it. */
  source_pts: number | null;
  time_base_num: number;
  time_base_den: number;
}>;

export type FrameObservation = Readonly<{
  patches: readonly ExtractedPatch[];
  identity: IdentityRead | null;
  geometry: readonly ExtractedGeometry[];
  /** The 5x5 mean at each alpha target's point, when this frame is one the target names. */
  ramp_samples: readonly Readonly<{
    label: string;
    output_frame: number;
    rgb: readonly [number, number, number];
  }>[];
}>;

/**
 * A plain, structured-cloneable image the caller builds with `context.getImageData` (an
 * `ImageData` itself clones fine across `page.evaluate`, but its `<canvas>` does not).
 */
export type PlainImageData = Readonly<{
  data: Uint8ClampedArray | readonly number[];
  width: number;
  height: number;
}>;

export type ObserveFrameArgs = Readonly<{
  image: PlainImageData;
  frame: number;
  prescription: Prescription;
  constants: MediaConstants;
}>;

//: The comparator's interior sample: a 5x5 mean, as the Python extractor takes it.
const SAMPLE_SIZE_PX = 5;
//: The identity cell luma threshold the Python extractor uses (`_FRAME_ID_LUMA_THRESHOLD`).
const FRAME_ID_LUMA_THRESHOLD = 128;
//: A layer counts as present when at least this many pixels classify to its own colours
//: (`_GEOMETRY_MIN_PRESENT_PIXELS`).
const GEOMETRY_MIN_PRESENT_PIXELS = 6;
//: A ramp whose two reference frames travel less than this (squared, over three channels) is not
//: measured (`_ALPHA_MIN_TRAVEL_SQUARED`).
const ALPHA_MIN_TRAVEL_SQUARED = 16;

function pixel(
  image: PlainImageData,
  x: number,
  y: number,
): [number, number, number] {
  const offset = (y * image.width + x) * 4;
  return [image.data[offset], image.data[offset + 1], image.data[offset + 2]];
}

/** `patch_mean`: an odd interior box, its mean per channel, and its distance to the frame edge. */
function patchMean(
  image: PlainImageData,
  label: string,
  centerX: number,
  centerY: number,
): ExtractedPatch | null {
  const radius = Math.floor(SAMPLE_SIZE_PX / 2);
  if (!(
    radius <= centerX &&
    centerX < image.width - radius &&
    radius <= centerY &&
    centerY < image.height - radius
  ))
    return null;
  const totals = [0, 0, 0];
  for (let y = centerY - radius; y <= centerY + radius; y += 1)
    for (let x = centerX - radius; x <= centerX + radius; x += 1) {
      const [r, g, b] = pixel(image, x, y);
      totals[0] += r;
      totals[1] += g;
      totals[2] += b;
    }
  const count = SAMPLE_SIZE_PX * SAMPLE_SIZE_PX;
  const edgeDistance = Math.min(
    centerX - radius,
    centerY - radius,
    image.width - 1 - centerX - radius,
    image.height - 1 - centerY - radius,
  );
  return {
    label,
    size_px: SAMPLE_SIZE_PX,
    edge_distance_px: edgeDistance,
    red: Math.floor(totals[0] / count),
    green: Math.floor(totals[1] / count),
    blue: Math.floor(totals[2] / count),
  };
}

/** `_read_identity`: the index the prescribed cells decode to, most significant bit first. */
function readIdentity(
  image: PlainImageData,
  read: Prescription["identity_reads"][number],
  bits: number,
): IdentityRead {
  let value = 0;
  for (let index = 0; index < read.cells.length; index += 1) {
    const [x, y] = read.cells[index];
    if (!(0 <= x && x < image.width && 0 <= y && y < image.height))
      return {
        clip_id: read.clip_id,
        asset_id: read.asset_id,
        source_frame: null,
        source_pts: null,
        time_base_num: read.time_base_num,
        time_base_den: read.time_base_den,
      };
    const [r, g, b] = pixel(image, x, y);
    if (Math.floor((r + g + b) / 3) >= FRAME_ID_LUMA_THRESHOLD)
      value |= 1 << (bits - 1 - index);
  }
  const pts = read.pts_by_frame[String(value)];
  return {
    clip_id: read.clip_id,
    asset_id: read.asset_id,
    source_frame: value,
    source_pts: pts === undefined ? null : pts,
    time_base_num: read.time_base_num,
    time_base_den: read.time_base_den,
  };
}

/** `_MEDIA_PALETTE`: every declared field and patch colour plus the two identity colours. */
function palette(constants: MediaConstants): [number, number, number][] {
  const seen = new Set<string>();
  const colours: [number, number, number][] = [];
  const push = (colour: readonly [number, number, number]) => {
    const key = colour.join(",");
    if (seen.has(key)) return;
    seen.add(key);
    colours.push([colour[0], colour[1], colour[2]]);
  };
  for (const profile of constants.source_profiles) push(profile.field);
  for (const profile of constants.source_profiles)
    for (const patch of profile.patches)
      push([patch.red, patch.green, patch.blue]);
  push(constants.frame_id_one_rgb);
  push(constants.frame_id_zero_rgb);
  return colours;
}

function paletteIndex(
  colours: readonly (readonly [number, number, number])[],
  colour: readonly [number, number, number],
): number {
  return colours.findIndex(
    (item) =>
      item[0] === colour[0] && item[1] === colour[1] && item[2] === colour[2],
  );
}

/** `_classify_frame`: each pixel's nearest palette colour (-1 below the luma threshold), and the
 * luma-threshold box of the whole frame. */
function classifyFrame(
  image: PlainImageData,
  colours: readonly (readonly [number, number, number])[],
  lumaThreshold: number,
): { labels: Int16Array; whole: [number, number, number, number] | null } {
  const labels = new Int16Array(image.width * image.height).fill(-1);
  const cache = new Map<number, number>();
  let minX = -1;
  let minY = -1;
  let maxX = -1;
  let maxY = -1;
  for (let y = 0; y < image.height; y += 1)
    for (let x = 0; x < image.width; x += 1) {
      const [r, g, b] = pixel(image, x, y);
      if (Math.floor((r + g + b) / 3) < lumaThreshold) continue;
      const key = (r << 16) | (g << 8) | b;
      let label = cache.get(key);
      if (label === undefined) {
        let best = 0;
        let bestDistance = Number.POSITIVE_INFINITY;
        for (let index = 0; index < colours.length; index += 1) {
          const [pr, pg, pb] = colours[index];
          const distance = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2;
          if (distance < bestDistance) {
            best = index;
            bestDistance = distance;
          }
        }
        label = best;
        cache.set(key, label);
      }
      labels[y * image.width + x] = label;
      if (minX < 0 || x < minX) minX = x;
      if (x > maxX) maxX = x;
      if (minY < 0 || y < minY) minY = y;
      if (y > maxY) maxY = y;
    }
  if (minX < 0 || minY < 0) return { labels, whole: null };
  return { labels, whole: [minX, minY, maxX + 1, maxY + 1] };
}

/** `_largest_component`: the largest 8-connected group of same-colour pixels. */
function largestComponent(
  labels: Int16Array,
  width: number,
  height: number,
  wanted: number,
): [number, number][] {
  const visited = new Uint8Array(width * height);
  let best: [number, number][] = [];
  for (let start = 0; start < labels.length; start += 1) {
    if (labels[start] !== wanted || visited[start]) continue;
    const stack = [start];
    visited[start] = 1;
    const component: [number, number][] = [];
    while (stack.length > 0) {
      const current = stack.pop() as number;
      const cx = current % width;
      const cy = Math.floor(current / width);
      component.push([cx, cy]);
      for (let dy = -1; dy <= 1; dy += 1)
        for (let dx = -1; dx <= 1; dx += 1) {
          if (dx === 0 && dy === 0) continue;
          const nx = cx + dx;
          const ny = cy + dy;
          if (nx < 0 || ny < 0 || nx >= width || ny >= height) continue;
          const neighbour = ny * width + nx;
          if (labels[neighbour] === wanted && !visited[neighbour]) {
            visited[neighbour] = 1;
            stack.push(neighbour);
          }
        }
    }
    if (component.length > best.length) best = component;
  }
  return best;
}

/** `_geometry_for_frame`: the layer rectangle and every fiducial found for each target layer. */
function geometryForFrame(
  image: PlainImageData,
  targets: readonly Prescription["geometry_targets"][number][],
  constants: MediaConstants,
): ExtractedGeometry[] {
  const colours = palette(constants);
  const { labels, whole } = classifyFrame(
    image,
    colours,
    constants.geometry_luma_threshold,
  );
  const landmarks: ExtractedGeometry[] = [];
  for (const target of targets) {
    const profile = constants.source_profiles.find(
      (item) => item.asset_id === target.asset_id,
    );
    if (profile === undefined) continue;
    const own = new Set<number>([paletteIndex(colours, profile.field)]);
    for (const patch of profile.patches)
      own.add(paletteIndex(colours, [patch.red, patch.green, patch.blue]));
    let present = 0;
    for (let index = 0; index < labels.length; index += 1)
      if (own.has(labels[index])) present += 1;
    if (whole !== null && present >= GEOMETRY_MIN_PRESENT_PIXELS)
      landmarks.push({
        label: target.clip_id,
        left: whole[0],
        top: whole[1],
        right: whole[2],
        bottom: whole[3],
      });
    for (const patch of profile.patches) {
      const wanted = paletteIndex(colours, [
        patch.red,
        patch.green,
        patch.blue,
      ]);
      const component = largestComponent(
        labels,
        image.width,
        image.height,
        wanted,
      );
      if (component.length < GEOMETRY_MIN_PRESENT_PIXELS) continue;
      let left = Number.POSITIVE_INFINITY;
      let top = Number.POSITIVE_INFINITY;
      let right = -1;
      let bottom = -1;
      for (const [x, y] of component) {
        if (x < left) left = x;
        if (y < top) top = y;
        if (x > right) right = x;
        if (y > bottom) bottom = y;
      }
      landmarks.push({
        label: `${target.clip_id}.${patch.label}`,
        left,
        top,
        right: right + 1,
        bottom: bottom + 1,
      });
    }
  }
  return landmarks;
}

/**
 * Everything the prescription asks of one presented frame: the colour at each of this frame's
 * points, the identity this frame's cells decode to, and the rectangle of each layer whose sample
 * frame this is. A point that does not admit an interior sample on this canvas is simply absent,
 * never estimated.
 */
export function observePrescribedFrame(
  args: ObserveFrameArgs,
): FrameObservation {
  const { image, frame, prescription, constants } = args;
  const patches: ExtractedPatch[] = [];
  for (const point of prescription.patch_points) {
    if (point.output_frame !== frame) continue;
    const sample = patchMean(
      image,
      point.label,
      point.canvas_x,
      point.canvas_y,
    );
    if (sample !== null) patches.push(sample);
  }
  const read = prescription.identity_reads.find(
    (item) => item.output_frame === frame,
  );
  const identity =
    read === undefined
      ? null
      : readIdentity(image, read, constants.frame_id_bits);
  const targets = prescription.geometry_targets.filter(
    (item) => item.sample_frame === frame,
  );
  const geometry =
    targets.length === 0 ? [] : geometryForFrame(image, targets, constants);
  const rampSamples: {
    label: string;
    output_frame: number;
    rgb: readonly [number, number, number];
  }[] = [];
  for (const target of prescription.alpha_targets) {
    if (
      frame !== target.base_frame &&
      frame !== target.steady_frame &&
      !target.sample_frames.includes(frame)
    )
      continue;
    const sample = patchMean(
      image,
      target.label,
      target.canvas_x,
      target.canvas_y,
    );
    if (sample !== null)
      rampSamples.push({
        label: target.label,
        output_frame: frame,
        rgb: [sample.red, sample.green, sample.blue],
      });
  }
  return { patches, identity, geometry, ramp_samples: rampSamples };
}

/**
 * `_alphas_for_clip`: the ramp's progress at every reported frame, from the pixels this journey
 * sampled at the ramp's two reference frames. `base` is the ramp's first included frame (alpha is
 * exactly 0 there by the declared progression, so the pixel *is* the un-mixed background) and
 * `mixed` is the frame one past the ramp (the clip's true steady pixel). The progress is one
 * least-squares ratio over the three channels together -- the projection of the observed travel
 * onto the full travel -- never a mean of per-channel ratios, and it is scaled by the clip's own
 * declared `opacity_bp`, which cancels out of the ratio and has to be put back to state the value
 * the contract names. Both rules are the Python extractor's; see its own guards.
 */
export function alphasForTarget(
  target: AlphaTarget,
  samples: ReadonlyMap<number, readonly [number, number, number]>,
): ExtractedAlpha[] {
  const base = samples.get(target.base_frame);
  const mixed = samples.get(target.steady_frame);
  if (base === undefined || mixed === undefined) return [];
  const travel = [mixed[0] - base[0], mixed[1] - base[1], mixed[2] - base[2]];
  const travelSquared =
    travel[0] * travel[0] + travel[1] * travel[1] + travel[2] * travel[2];
  if (travelSquared < ALPHA_MIN_TRAVEL_SQUARED) return [];
  const out: ExtractedAlpha[] = [];
  for (const frame of target.sample_frames) {
    if (frame === target.base_frame) {
      out.push({ label: target.label, output_frame: frame, alpha_milli: 0 });
      continue;
    }
    const observed = samples.get(frame);
    if (observed === undefined) continue;
    const progress =
      ((observed[0] - base[0]) * travel[0] +
        (observed[1] - base[1]) * travel[1] +
        (observed[2] - base[2]) * travel[2]) /
      travelSquared;
    out.push({
      label: target.label,
      output_frame: frame,
      alpha_milli: Math.round((progress * target.opacity_bp) / 10),
    });
  }
  return out;
}
