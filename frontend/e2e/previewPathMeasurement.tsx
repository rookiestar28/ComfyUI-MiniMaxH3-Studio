// M25-45 step 1: the preview-path measurement harness.
//
// It measures the cost of a COMPLETE frame transaction on one real composition canvas -- geometry,
// source rasterization, readback, colour, blend, allocation and the final write -- for the paths
// the ladder chooses between. It is apparatus, not product: nothing here is imported by
// `frontend/src`, and the product's own pure functions are used rather than re-derived, so a
// prototype cannot accidentally measure different arithmetic from the one that ships.
//
// Paths:
//   `product`  the real `createVisualCompositor`, at whatever backing `computePreviewSize` gives.
//   `software` the same per-frame transaction at an explicit backing size, using the product's
//              exported geometry, colour, dissolve and blend functions in the product's order.
//   `native`   per layer `globalAlpha` + `globalCompositeOperation` + one `drawImage`, no readback.
//   `svg`      `native` plus a `feColorMatrix` filter for `color_adjust_v1`.
// The last two are the rungs M25-45 evaluates; `software` is the incumbent and the fallback.
//
// The video source plays rather than sits on one seeked frame, because a still frame lets the
// browser reuse an uploaded texture and would understate every path's real cost.

import fixture from "../../tests/fixtures/m25_10_composition_contract_v1.json";
import {
  decodePublicCompositionSnapshot,
  decodeResolvedScene,
  publicCompositionFingerprint,
  type PublicCompositionSnapshot,
  type ResolvedCompositionScene,
} from "../src/contracts/compositionCodec";
import {
  applyVisualColorAdjust,
  colorAdjustFilterStages,
  compositeVisualLayer,
  computeVisualLayerGeometry,
  createVisualCompositor,
  dissolveFactor,
  type VisualLayerGeometry,
  type VisualLayerResource,
  type VisualLayerResources,
} from "../src/runtime/visualCompositor";

const VIDEO_URL = "/measurement-media/detail-720p.mp4";
const IMAGE_URL = "/measurement-media/detail-720p.png";
const SOURCE_WIDTH = 1280;
const SOURCE_HEIGHT = 720;
const FILTER_ID = "h3c-measurement-color-adjust";

type PathName = "product" | "software" | "native" | "svg";

type CellRequest = Readonly<{
  width: number;
  height: number;
  layers: number;
  colorAdjust: boolean;
  path: PathName;
  backingWidth?: number;
  backingHeight?: number;
}>;

type RunRequest = Readonly<{
  cells: readonly CellRequest[];
  warmup?: number;
  samples?: number;
}>;

type CellResult = Readonly<{
  cell: CellRequest;
  requestedPath: PathName;
  actualPath: "native" | "software" | "svg";
  firstPaintBackingWidth: number;
  firstPaintBackingHeight: number;
  backingWidth: number;
  backingHeight: number;
  deliveredWidth: number;
  deliveredHeight: number;
  backingStable: boolean;
  pictureWidth: number;
  pictureHeight: number;
  devicePixelRatio: number;
  warmupMilliseconds: readonly number[];
  syncMilliseconds: readonly number[];
  rafIntervalMilliseconds: readonly number[];
  presentedFrames: number;
  failedFrames: number;
  decodedVideoFrames: number;
  status: "ok" | "failed";
  error: string | null;
  pixelHash: string;
}>;

type LayerPlan = Readonly<{
  clipId: string;
  blend: "normal" | "multiply" | "screen";
  opacityBp: number;
  crop: Readonly<Record<string, number>>;
  transform: Readonly<Record<string, number>>;
  effect: Readonly<Record<string, number | string>>;
  text: Readonly<Record<string, unknown>> | null;
}>;

/** Video first, then the image overlay, then the title: one layer of each class by three. */
const CLIP_IDS = ["clip-main", "clip-image", "clip-title"] as const;
const TRACK_IDS = ["track-primary", "track-image", "track-text"] as const;
const COLOR_ADJUST = Object.freeze({
  kind: "color_adjust_v1",
  brightness_permille: 50,
  contrast_permille: 1100,
  saturation_permille: 900,
});
const NEUTRAL = Object.freeze({
  kind: "none",
  brightness_permille: 0,
  contrast_permille: 1000,
  saturation_permille: 1000,
});

type Wire = typeof fixture.snapshot;

function buildSnapshot(
  width: number,
  height: number,
  layers: number,
  colorAdjust: boolean,
): Readonly<{ wire: Wire; snapshot: PublicCompositionSnapshot }> {
  const wire = structuredClone(fixture.snapshot) as Wire;
  wire.output.width = width;
  wire.output.height = height;
  const enabled = new Set<string>(TRACK_IDS.slice(0, layers));
  for (const track of wire.tracks) track.enabled = enabled.has(track.track_id);
  for (const clip of wire.clips) {
    clip.enabled = enabled.has(clip.track_id);
    if (!clip.enabled) clip.transition = { kind: "none", duration_frames: 0 };
    else
      clip.effect = structuredClone(
        colorAdjust ? COLOR_ADJUST : NEUTRAL,
      ) as (typeof clip)["effect"];
  }
  // The packaged font binding is the only accepted text font, so the fixture's font asset is
  // renamed to it whenever the title layer takes part.
  if (enabled.has("track-text")) {
    wire.assets[3]!.asset_id = "h3.font.noto_sans.v1";
    wire.clips[3]!.text!.font_asset_id = "h3.font.noto_sans.v1";
  }
  wire.public_fingerprint = publicCompositionFingerprint(wire);
  return { wire, snapshot: decodePublicCompositionSnapshot(wire) };
}

function buildScene(
  wire: Wire,
  snapshot: PublicCompositionSnapshot,
  layers: number,
): Readonly<{ scene: ResolvedCompositionScene; plan: readonly LayerPlan[] }> {
  const plan: LayerPlan[] = [];
  const wireLayers = CLIP_IDS.slice(0, layers).map((clipId) => {
    const clip = wire.clips.find((candidate) => candidate.clip_id === clipId)!;
    const isVideo = clipId === "clip-main";
    plan.push({
      clipId,
      blend: clip.blend as LayerPlan["blend"],
      opacityBp: clip.opacity_bp,
      crop: clip.crop,
      transform: clip.transform,
      effect: clip.effect,
      text: clip.text,
    });
    return {
      clip_id: clip.clip_id,
      asset_id: clip.asset_id,
      track_id: clip.track_id,
      source_frame: isVideo ? 12 : null,
      source_pts: isVideo ? fixture.expectations.source_pts : null,
      transition_elapsed_frames: null,
      // The resolver's operation list is executable semantics, not decoration: it carries
      // ColorAdjustV1 exactly when the effect is not the identity and DrawTextV1 exactly when the
      // layer has text, in this order. `decodeResolvedScene` refuses any other list.
      operation_ids: [
        "SelectSourceRangeV1",
        "CropV1",
        "Transform2DV1",
        ...(clip.effect.kind === "none" ? [] : ["ColorAdjustV1"]),
        "OpacityV1",
        "BlendV1",
        ...(clip.text === null ? [] : ["DrawTextV1"]),
      ],
      transform: clip.transform,
      crop: clip.crop,
      opacity_bp: clip.opacity_bp,
      blend: clip.blend,
      text: clip.text,
      effect: clip.effect,
    };
  });
  const scene = decodeResolvedScene({
    schema: "h3.context.resolved_scene.v1",
    profile_id: "h3.native_media_canvas_backend.v1",
    public_fingerprint: snapshot.publicFingerprint,
    frame: 12,
    layers: wireLayers,
    audio_span: null,
    blockers: [],
  });
  return { scene, plan };
}

async function loadVideo(): Promise<HTMLVideoElement> {
  const video = document.createElement("video");
  video.src = VIDEO_URL;
  video.muted = true;
  video.loop = true;
  video.playsInline = true;
  video.preload = "auto";
  video.style.cssText = "position:absolute;width:1px;height:1px;opacity:0";
  document.body.append(video);
  await new Promise<void>((resolve, reject) => {
    video.addEventListener("canplaythrough", () => resolve(), { once: true });
    video.addEventListener("error", () => reject(new Error("video")), {
      once: true,
    });
  });
  await video.play();
  return video;
}

async function loadImage(): Promise<ImageBitmap> {
  const response = await fetch(IMAGE_URL);
  if (!response.ok) throw new Error(`image ${response.status}`);
  return createImageBitmap(await response.blob());
}

/**
 * The filter the `svg` rung measures, which is the product's, not a copy of it.
 *
 * IMPORTANT (B-M2545-22): this file used to re-derive the matrix, against its own header's rule
 * that "the product's own pure functions are used rather than re-derived". The copy composed the
 * three `eq` planes into a single `feColorMatrix`, which cannot express a plane saturating on its
 * own, and its per-pixel difference against the software path was therefore measured on arithmetic
 * that does not ship -- at `brightness_permille: 50`, the one regime where the two formulations
 * agree. `colorAdjustFilterStages` is imported for exactly that reason: a measurement of a rung is
 * worth nothing if it is not a measurement of the rung.
 */
function installFilter(
  effect: Readonly<Record<string, number | string>>,
): void {
  const stages = colorAdjustFilterStages(
    Number(effect["brightness_permille"] ?? 0),
    Number(effect["contrast_permille"] ?? 1000),
    Number(effect["saturation_permille"] ?? 1000),
  )
    .map(
      (values) =>
        `<feColorMatrix type="matrix" values="${values
          .map((value) => value.toFixed(6))
          .join(" ")}"/>`,
    )
    .join("");
  // IMPORTANT: the host element must not share the filter's id. `url(#id)` resolves by document
  // id, so a wrapper carrying the same id wins and `context.filter` silently becomes a no-op --
  // which reads as "the filter rung is exact and free" instead of "the filter never ran".
  const existing = document.getElementById(`${FILTER_ID}-host`);
  if (existing !== null) existing.remove();
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("width", "0");
  svg.setAttribute("height", "0");
  svg.style.cssText = "position:absolute;width:0;height:0";
  svg.innerHTML =
    `<filter id="${FILTER_ID}" color-interpolation-filters="sRGB">` +
    `${stages}</filter>`;
  svg.id = `${FILTER_ID}-host`;
  document.body.append(svg);
}

function geometryFor(
  plan: LayerPlan,
  outputWidth: number,
  outputHeight: number,
  sourceWidth: number,
  sourceHeight: number,
): VisualLayerGeometry {
  return computeVisualLayerGeometry(
    SOURCE_WIDTH,
    SOURCE_HEIGHT,
    sourceWidth,
    sourceHeight,
    plan.crop,
    plan.transform,
    outputWidth,
    outputHeight,
  );
}

function drawLayer(
  context: CanvasRenderingContext2D,
  source: CanvasImageSource,
  geometry: VisualLayerGeometry,
  scale: number,
): void {
  context.save();
  context.translate(geometry.centerX * scale, geometry.centerY * scale);
  context.rotate(geometry.angleRadians);
  context.drawImage(
    source,
    geometry.sourceCrop.left,
    geometry.sourceCrop.top,
    geometry.sourceCrop.width,
    geometry.sourceCrop.height,
    (-geometry.scaledWidth * scale) / 2,
    (-geometry.scaledHeight * scale) / 2,
    geometry.scaledWidth * scale,
    geometry.scaledHeight * scale,
  );
  context.restore();
}

function blackPixels(width: number, height: number): Uint8ClampedArray {
  const pixels = new Uint8ClampedArray(width * height * 4);
  for (let offset = 3; offset < pixels.length; offset += 4)
    pixels[offset] = 255;
  return pixels;
}

function hashPixels(pixels: Uint8ClampedArray): string {
  // A cheap 32-bit rolling digest: it identifies the produced picture across paths without
  // shipping pixels out of the browser. It is a comparison aid, never a conformance observation.
  let hash = 0x811c9dc5;
  for (let offset = 0; offset < pixels.length; offset += 997) {
    hash ^= pixels[offset]!;
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash.toString(16).padStart(8, "0");
}

const BLEND_OPERATIONS: Readonly<
  Record<LayerPlan["blend"], GlobalCompositeOperation>
> = Object.freeze({
  normal: "source-over",
  multiply: "multiply",
  screen: "screen",
});

function percentile(values: readonly number[], fraction: number): number {
  if (values.length === 0) return Number.NaN;
  const sorted = [...values].sort((left, right) => left - right);
  const index = Math.min(
    sorted.length - 1,
    Math.max(0, Math.ceil(fraction * sorted.length) - 1),
  );
  return sorted[index]!;
}

type Renderer = Readonly<{
  renderFrame: (generation: number) => boolean;
  actualPath: () => "native" | "software" | "svg";
  backingSize: () => Readonly<{ width: number; height: number }>;
  deliveredSize: () => Readonly<{ width: number; height: number }>;
}>;

type ComparisonRequest = Readonly<{
  width: number;
  height: number;
  layers: number;
  colorAdjust: boolean;
  seconds: number;
}>;

type ComparisonResult = Readonly<{
  request: ComparisonRequest;
  backing: string;
  paths: Readonly<
    Record<
      string,
      Readonly<{
        maxAbsChannel: number;
        meanAbsChannel: number;
        pixelsOverTwelve: number;
        pixelCount: number;
      }>
    >
  >;
  error: string | null;
}>;

type Harness = Readonly<{
  run(request: RunRequest): Promise<Readonly<{ cells: readonly CellResult[] }>>;
  compare(
    requests: readonly ComparisonRequest[],
  ): Promise<Readonly<{ comparisons: readonly ComparisonResult[] }>>;
}>;

/** Per-channel difference of two RGBA buffers, ignoring alpha (every path writes opaque). */
function channelDifference(
  reference: Uint8ClampedArray,
  candidate: Uint8ClampedArray,
): Readonly<{
  maxAbsChannel: number;
  meanAbsChannel: number;
  pixelsOverTwelve: number;
  pixelCount: number;
}> {
  let maximum = 0;
  let total = 0;
  let over = 0;
  let pixels = 0;
  for (let offset = 0; offset < reference.length; offset += 4) {
    let worst = 0;
    for (let member = 0; member < 3; member += 1) {
      const difference = Math.abs(
        reference[offset + member]! - candidate[offset + member]!,
      );
      total += difference;
      if (difference > worst) worst = difference;
    }
    if (worst > maximum) maximum = worst;
    if (worst > 12) over += 1;
    pixels += 1;
  }
  return {
    maxAbsChannel: maximum,
    meanAbsChannel: +(total / (pixels * 3)).toFixed(4),
    pixelsOverTwelve: over,
    pixelCount: pixels,
  };
}

async function boot(): Promise<void> {
  const root = document.getElementById("root")!;
  root.style.cssText = "margin:0;background:#101014";
  const canvas = document.createElement("canvas");
  canvas.style.cssText = "display:block;width:640px;height:360px";
  root.append(canvas);
  const willReadFrequently =
    new URLSearchParams(location.search).get("willReadFrequently") !== "0";
  const pageContext = canvas.getContext("2d", {
    alpha: true,
    colorSpace: "srgb",
    willReadFrequently,
  })!;
  const video = await loadVideo();
  const image = await loadImage();

  const buildRenderer = (cell: CellRequest): Renderer => {
    const { wire, snapshot } = buildSnapshot(
      cell.width,
      cell.height,
      cell.layers,
      cell.colorAdjust,
    );
    const { scene, plan } = buildScene(wire, snapshot, cell.layers);
    const resources: VisualLayerResources = new Map<
      string,
      VisualLayerResource
    >([
      [
        "clip-main",
        {
          kind: "video",
          source: video,
          nativeWidth: SOURCE_WIDTH,
          nativeHeight: SOURCE_HEIGHT,
        },
      ],
      [
        "clip-image",
        {
          kind: "image",
          source: image,
          nativeWidth: SOURCE_WIDTH,
          nativeHeight: SOURCE_HEIGHT,
        },
      ],
      ["clip-title", { kind: "font", family: "sans-serif" }],
    ]);

    // IMPORTANT (B-M2545-49): CSS size and PreviewBox are one measurement input. Leaving the
    // box absent makes the real product arm silently run at the legacy 320 x 180 floor while the
    // row is labelled 640/960/1280, invalidating both performance and delivery evidence.
    canvas.style.width = `${cell.width}px`;
    canvas.style.height = `${cell.height}px`;
    let renderFrame: (generation: number) => boolean;
    let backingWidth: number;
    let backingHeight: number;
    let deliveredWidth = 0;
    let deliveredHeight = 0;
    let actualPath: "native" | "software" | "svg";

    if (cell.path === "product") {
      const compositor = createVisualCompositor(canvas, snapshot, {
        cssWidth: cell.width,
        cssHeight: cell.height,
        devicePixelRatio: window.devicePixelRatio,
      });
      backingWidth = canvas.width;
      backingHeight = canvas.height;
      actualPath = compositor.path();
      renderFrame = (generation) => {
        const receipt = compositor.render(scene, resources, generation);
        deliveredWidth = receipt.previewWidth;
        deliveredHeight = receipt.previewHeight;
        return receipt.status === "presented";
      };
    } else {
      backingWidth = cell.backingWidth ?? cell.width;
      backingHeight = cell.backingHeight ?? cell.height;
      canvas.width = backingWidth;
      canvas.height = backingHeight;
      const scale = backingWidth / cell.width;
      // One context policy for the canvas's whole lifetime, fixed by the page's query string:
      // Canvas2D ignores the options of every `getContext` call after the first, so a per-path
      // policy is not expressible on one canvas, and swapping in a second canvas to get a better
      // number is exactly what the plan forbids. The two policies are measured in separate page
      // loads instead.
      const context = pageContext;
      actualPath = cell.path;
      deliveredWidth = backingWidth;
      deliveredHeight = backingHeight;
      context.imageSmoothingEnabled = true;
      context.imageSmoothingQuality = "low";
      context.filter = "none";
      const sourceOf = (clipId: string): CanvasImageSource | null =>
        clipId === "clip-main" ? video : clipId === "clip-image" ? image : null;
      const geometries = plan.map((layer) =>
        geometryFor(
          layer,
          cell.width,
          cell.height,
          layer.clipId === "clip-main" ? video.videoWidth : image.width,
          layer.clipId === "clip-main" ? video.videoHeight : image.height,
        ),
      );
      if (cell.path === "svg" && cell.colorAdjust)
        installFilter(plan[0]!.effect);

      renderFrame = () => {
        if (cell.path === "software") {
          const accumulator = blackPixels(backingWidth, backingHeight);
          for (let index = 0; index < plan.length; index += 1) {
            const layer = plan[index]!;
            const source = sourceOf(layer.clipId);
            context.save();
            context.setTransform(1, 0, 0, 1, 0, 0);
            context.globalAlpha = 1;
            context.globalCompositeOperation = "source-over";
            context.clearRect(0, 0, backingWidth, backingHeight);
            context.restore();
            if (source === null) {
              context.save();
              context.fillStyle = "#ffffff";
              context.font = `700 ${42 * scale}px sans-serif`;
              context.textAlign = "center";
              context.fillText(
                "Fixture title",
                backingWidth / 2,
                backingHeight / 2,
              );
              context.restore();
            } else drawLayer(context, source, geometries[index]!, scale);
            const pixels = context.getImageData(
              0,
              0,
              backingWidth,
              backingHeight,
            ).data;
            const adjusted = applyVisualColorAdjust(pixels, {
              kind: String(layer.effect["kind"]) as "none" | "color_adjust_v1",
              brightnessPermille: Number(layer.effect["brightness_permille"]),
              contrastPermille: Number(layer.effect["contrast_permille"]),
              saturationPermille: Number(layer.effect["saturation_permille"]),
            });
            compositeVisualLayer(
              accumulator,
              adjusted,
              layer.opacityBp,
              dissolveFactor(null, 0),
              layer.blend,
            );
          }
          const output = context.createImageData(backingWidth, backingHeight);
          output.data.set(accumulator);
          context.putImageData(output, 0, 0);
          return true;
        }
        context.save();
        context.setTransform(1, 0, 0, 1, 0, 0);
        context.globalAlpha = 1;
        context.globalCompositeOperation = "source-over";
        context.filter = "none";
        context.fillStyle = "#000000";
        context.fillRect(0, 0, backingWidth, backingHeight);
        context.restore();
        for (let index = 0; index < plan.length; index += 1) {
          const layer = plan[index]!;
          const source = sourceOf(layer.clipId);
          context.save();
          context.globalAlpha = layer.opacityBp / 10_000;
          context.globalCompositeOperation = BLEND_OPERATIONS[layer.blend];
          context.filter =
            cell.path === "svg" && cell.colorAdjust
              ? `url(#${FILTER_ID})`
              : "none";
          if (source === null) {
            context.fillStyle = "#ffffff";
            context.font = `700 ${42 * scale}px sans-serif`;
            context.textAlign = "center";
            context.fillText(
              "Fixture title",
              backingWidth / 2,
              backingHeight / 2,
            );
          } else drawLayer(context, source, geometries[index]!, scale);
          context.restore();
        }
        return true;
      };
    }

    return {
      renderFrame,
      actualPath: () => actualPath,
      backingSize: () => ({ width: canvas.width, height: canvas.height }),
      deliveredSize: () => ({ width: deliveredWidth, height: deliveredHeight }),
    };
  };

  const renderCell = async (
    cell: CellRequest,
    warmup: number,
    samples: number,
  ): Promise<CellResult> => {
    const warmupMilliseconds: number[] = [];
    const syncMilliseconds: number[] = [];
    const rafIntervalMilliseconds: number[] = [];
    let presentedFrames = 0;
    let failedFrames = 0;
    let error: string | null = null;
    let pixelHash = "";
    const decodedBefore =
      video.getVideoPlaybackQuality?.().totalVideoFrames ?? 0;
    const renderer = buildRenderer(cell);
    const renderFrame = renderer.renderFrame;
    let firstPaintBackingWidth = 0;
    let firstPaintBackingHeight = 0;
    let backingStable = true;

    let generation = 0;
    let previousTimestamp: number | null = null;
    try {
      for (let index = 0; index < warmup + samples; index += 1) {
        const timestamp = await new Promise<number>((resolve) =>
          requestAnimationFrame(resolve),
        );
        if (previousTimestamp !== null && index >= warmup)
          rafIntervalMilliseconds.push(timestamp - previousTimestamp);
        previousTimestamp = timestamp;
        const started = performance.now();
        const ok = renderFrame(generation);
        const elapsed = performance.now() - started;
        const backing = renderer.backingSize();
        if (ok && firstPaintBackingWidth === 0) {
          firstPaintBackingWidth = backing.width;
          firstPaintBackingHeight = backing.height;
        } else if (
          firstPaintBackingWidth !== 0 &&
          (backing.width !== firstPaintBackingWidth ||
            backing.height !== firstPaintBackingHeight)
        ) {
          backingStable = false;
        }
        generation += 1;
        if (index < warmup) warmupMilliseconds.push(elapsed);
        else {
          syncMilliseconds.push(elapsed);
          if (ok) presentedFrames += 1;
          else failedFrames += 1;
        }
      }
      const readContext = canvas.getContext("2d")!;
      pixelHash = hashPixels(
        readContext.getImageData(0, 0, canvas.width, canvas.height).data,
      );
    } catch (thrown) {
      error = thrown instanceof Error ? thrown.message : String(thrown);
    }

    const backing = renderer.backingSize();
    const delivered = renderer.deliveredSize();
    const productAdmissionError =
      cell.path === "product" &&
      (renderer.actualPath() !== "native" ||
        firstPaintBackingWidth !== cell.width ||
        firstPaintBackingHeight !== cell.height ||
        backing.width !== cell.width ||
        backing.height !== cell.height ||
        delivered.width !== cell.width ||
        delivered.height !== cell.height ||
        !backingStable)
        ? "product_size_or_path_not_admitted"
        : null;
    if (error === null && productAdmissionError !== null)
      error = productAdmissionError;
    return {
      cell,
      requestedPath: cell.path,
      actualPath: renderer.actualPath(),
      firstPaintBackingWidth,
      firstPaintBackingHeight,
      backingWidth: backing.width,
      backingHeight: backing.height,
      deliveredWidth: delivered.width,
      deliveredHeight: delivered.height,
      backingStable,
      pictureWidth: Math.round(canvas.getBoundingClientRect().width),
      pictureHeight: Math.round(canvas.getBoundingClientRect().height),
      devicePixelRatio: window.devicePixelRatio,
      warmupMilliseconds,
      syncMilliseconds,
      rafIntervalMilliseconds,
      presentedFrames,
      failedFrames,
      decodedVideoFrames:
        (video.getVideoPlaybackQuality?.().totalVideoFrames ?? 0) -
        decodedBefore,
      status: error === null && failedFrames === 0 ? "ok" : "failed",
      error,
      pixelHash,
    };
  };

  // The picture each path produces, from one identical paused source frame. Cost alone cannot
  // choose a rung: a faster path that moves a channel further than the frozen tolerance is not
  // eligible, and this is the evidence that decides it.
  const compareCell = async (
    request: ComparisonRequest,
  ): Promise<ComparisonResult> => {
    const paths: Record<string, ReturnType<typeof channelDifference>> = {};
    let error: string | null = null;
    let backing = "";
    try {
      video.pause();
      if (Math.abs(video.currentTime - request.seconds) > 1e-3) {
        video.currentTime = request.seconds;
        await new Promise<void>((resolve) =>
          video.addEventListener("seeked", () => resolve(), { once: true }),
        );
      }
      const pictures: Record<string, Uint8ClampedArray> = {};
      for (const path of ["software", "native", "svg"] as const) {
        const cell: CellRequest = {
          width: request.width,
          height: request.height,
          layers: request.layers,
          colorAdjust: request.colorAdjust,
          path,
          backingWidth: request.width,
          backingHeight: request.height,
        };
        const renderer = buildRenderer(cell);
        const rendererBacking = renderer.backingSize();
        backing = `${rendererBacking.width}x${rendererBacking.height}`;
        renderer.renderFrame(0);
        await new Promise<void>((resolve) =>
          requestAnimationFrame(() => resolve()),
        );
        renderer.renderFrame(1);
        pictures[path] = new Uint8ClampedArray(
          canvas
            .getContext("2d")!
            .getImageData(0, 0, rendererBacking.width, rendererBacking.height)
            .data,
        );
      }
      for (const path of ["native", "svg"] as const)
        paths[path] = channelDifference(pictures["software"]!, pictures[path]!);
    } catch (thrown) {
      error = thrown instanceof Error ? thrown.message : String(thrown);
    } finally {
      await video.play().catch(() => undefined);
    }
    return { request, backing, paths, error };
  };

  const harness: Harness = {
    async run(request) {
      const warmup = request.warmup ?? 3;
      const samples = request.samples ?? 120;
      const cells: CellResult[] = [];
      for (const cell of request.cells)
        cells.push(await renderCell(cell, warmup, samples));
      return { cells };
    },
    async compare(requests) {
      const comparisons: ComparisonResult[] = [];
      for (const request of requests)
        comparisons.push(await compareCell(request));
      return { comparisons };
    },
  };
  (
    window as unknown as { h3PreviewPathMeasurement: Harness }
  ).h3PreviewPathMeasurement = harness;
  document.body.dataset["h3MeasurementReady"] = "true";
}

void boot().catch((error: unknown) => {
  document.body.dataset["h3MeasurementError"] =
    error instanceof Error ? error.message : String(error);
});

export { percentile };
