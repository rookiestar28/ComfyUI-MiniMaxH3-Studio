import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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
  compositeVisualLayer,
  computePreviewSize,
  computeVisualLayerGeometry,
  createVisualCompositor,
  dissolveFactor,
  validateVisualText,
  type VisualLayerResource,
  type VisualLayerResources,
} from "../src/runtime/visualCompositor";

type SolidSource = Readonly<{
  width: number;
  height: number;
  color: readonly [number, number, number, number];
}>;

class QualifiedImageBitmap {
  readonly width: number;
  readonly height: number;
  readonly color: readonly [number, number, number, number];
  readonly id: string;

  constructor(
    width: number,
    height: number,
    color: readonly [number, number, number, number],
    id: string,
  ) {
    this.width = width;
    this.height = height;
    this.color = color;
    this.id = id;
  }

  close(): void {}
}

beforeEach(() => {
  vi.stubGlobal("ImageBitmap", QualifiedImageBitmap);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function qualifiedVideoSource(
  color: readonly [number, number, number, number],
  id = "clip-main",
): HTMLVideoElement {
  const source = document.createElement("video");
  source.id = id;
  Object.defineProperties(source, {
    videoWidth: { configurable: true, value: 320 },
    videoHeight: { configurable: true, value: 180 },
    color: { configurable: true, value: color },
  });
  return source;
}

function qualifiedImageSource(
  color: readonly [number, number, number, number],
  id = "clip-image",
): ImageBitmap {
  return new QualifiedImageBitmap(
    320,
    180,
    color,
    id,
  ) as unknown as ImageBitmap;
}

class MemoryContext {
  fillStyle: string | CanvasGradient | CanvasPattern = "#000000";
  font = "";
  globalAlpha = 1;
  globalCompositeOperation: GlobalCompositeOperation = "source-over";
  imageSmoothingEnabled = true;
  imageSmoothingQuality: ImageSmoothingQuality = "low";
  textAlign: CanvasTextAlign = "start";
  textBaseline: CanvasTextBaseline = "alphabetic";
  readonly drawOrder: string[] = [];
  readonly textDraws: Array<Readonly<{ text: string; x: number; y: number }>> =
    [];
  lost = false;
  filter = "none";
  /** An engine that accepts the assignment but applies nothing: the silent no-op the probe exists for. */
  filterIsInert = false;
  onDrawImage: (() => void) | null = null;
  readonly layerStates: Array<
    Readonly<{ alpha: number; operation: string; filter: string }>
  > = [];
  private pixels = new Uint8ClampedArray();

  constructor(private readonly owner: { width: number; height: number }) {}

  private ensure(): void {
    if (this.lost) throw new Error("Canvas2D context is lost");
    const length = this.owner.width * this.owner.height * 4;
    if (this.pixels.length !== length)
      this.pixels = new Uint8ClampedArray(length);
  }

  clearRect(): void {
    this.ensure();
    this.pixels.fill(0);
  }

  fillRect(): void {
    this.ensure();
    // The compositor's capability probe fills one pixel through a filter that maps everything to
    // opaque red. This stub reproduces only whether a filter reference took effect, so these
    // cases prove the rung SELECTION; that the real filter matches the renderer's colour model is
    // proven by the measured matrix and re-proven by the semantic corpus in a real browser.
    const filtered = this.filter.startsWith("url(") && !this.filterIsInert;
    const color = filtered
      ? [255, 0, 0, 255]
      : this.fillStyle === "#000000"
        ? [0, 0, 0, 255]
        : [0, 0, 0, 0];
    for (let offset = 0; offset < this.pixels.length; offset += 4)
      this.pixels.set(color, offset);
  }

  drawImage(source: CanvasImageSource): void {
    this.onDrawImage?.();
    this.ensure();
    const solid = source as unknown as SolidSource;
    this.layerStates.push(
      Object.freeze({
        alpha: this.globalAlpha,
        operation: String(this.globalCompositeOperation),
        filter: this.filter,
      }),
    );
    this.drawOrder.push(
      String((source as unknown as { id?: string }).id ?? "source"),
    );
    for (let offset = 0; offset < this.pixels.length; offset += 4)
      this.pixels.set(solid.color, offset);
  }

  getImageData(): ImageData {
    this.ensure();
    return {
      colorSpace: "srgb",
      data: new Uint8ClampedArray(this.pixels),
      height: this.owner.height,
      width: this.owner.width,
    } as ImageData;
  }

  createImageData(width: number, height: number): ImageData {
    return {
      colorSpace: "srgb",
      data: new Uint8ClampedArray(width * height * 4),
      height,
      width,
    } as ImageData;
  }

  putImageData(image: ImageData): void {
    this.pixels = new Uint8ClampedArray(image.data);
  }

  pixel(): number[] {
    this.ensure();
    return [...this.pixels.slice(0, 4)];
  }

  isContextLost(): boolean {
    return this.lost;
  }

  private readonly stack: Array<
    Readonly<{
      alpha: number;
      operation: GlobalCompositeOperation;
      filter: string;
      fill: string | CanvasGradient | CanvasPattern;
    }>
  > = [];

  // A real context restores `filter` with the rest of its state; a stub that does not would let a
  // filter leak out of the capability probe into every later fill.
  save(): void {
    this.stack.push({
      alpha: this.globalAlpha,
      operation: this.globalCompositeOperation,
      filter: this.filter,
      fill: this.fillStyle,
    });
  }
  restore(): void {
    const previous = this.stack.pop();
    if (previous === undefined) return;
    this.globalAlpha = previous.alpha;
    this.globalCompositeOperation = previous.operation;
    this.filter = previous.filter;
    this.fillStyle = previous.fill;
  }
  setTransform(): void {}
  translate(): void {}
  rotate(): void {}
  scale(): void {}
  beginPath(): void {}
  rect(): void {}
  clip(): void {}
  fillText(text: string, x: number, y: number): void {
    this.textDraws.push(Object.freeze({ text, x, y }));
  }
  measureText(text: string): TextMetrics {
    return {
      actualBoundingBoxAscent: 8,
      actualBoundingBoxDescent: 2,
      fontBoundingBoxAscent: 8,
      fontBoundingBoxDescent: 2,
      width: text.length * 6,
    } as TextMetrics;
  }
}

function harness(options: Readonly<{ attached?: boolean }> = {}): {
  canvas: HTMLCanvasElement;
  context: MemoryContext;
  parent: HTMLElement | null;
  setAvailable(available: boolean): void;
} {
  const owner = { width: 0, height: 0 };
  const context = new MemoryContext(owner);
  // The filter rung needs somewhere of its own to put its `<filter>` elements: the canvas's own
  // parent, inside the extension's root. Without one the probe cannot run and the session takes
  // the software path, which is the pre-M25-45 behaviour every other case here relies on.
  const parent =
    options.attached === true ? document.createElement("div") : null;
  if (parent !== null) document.body.append(parent);
  let available = true;
  const canvas = {
    get width() {
      return owner.width;
    },
    set width(value: number) {
      owner.width = value;
    },
    get height() {
      return owner.height;
    },
    set height(value: number) {
      owner.height = value;
    },
    getContext: vi.fn(() => (available && !context.lost ? context : null)),
    parentElement: parent,
    ownerDocument: parent === null ? null : document,
  } as unknown as HTMLCanvasElement;
  return {
    canvas,
    context,
    parent,
    setAvailable(next: boolean) {
      available = next;
    },
  };
}

function snapshot(
  enabledTracks: readonly string[] = ["track-primary"],
  output: Readonly<{ width: number; height: number }> | null = null,
): PublicCompositionSnapshot {
  const wire = structuredClone(fixture.snapshot);
  if (output !== null) {
    wire.output.width = output.width;
    wire.output.height = output.height;
  }
  for (const track of wire.tracks)
    track.enabled = enabledTracks.includes(track.track_id);
  for (const clip of wire.clips) {
    clip.enabled = enabledTracks.includes(clip.track_id);
    if (!clip.enabled) clip.transition = { kind: "none", duration_frames: 0 };
  }
  wire.public_fingerprint = publicCompositionFingerprint(wire);
  return decodePublicCompositionSnapshot(wire);
}

function scene(
  source: PublicCompositionSnapshot,
  options: Readonly<{ opacityBp?: number; transformX?: number }> = {},
): ResolvedCompositionScene {
  const clip = structuredClone(fixture.snapshot.clips[0]);
  if (options.opacityBp !== undefined) clip.opacity_bp = options.opacityBp;
  if (options.transformX !== undefined)
    clip.transform.position_x_bp = options.transformX;
  return decodeResolvedScene({
    schema: "h3.context.resolved_scene.v1",
    profile_id: "h3.native_media_canvas_backend.v1",
    public_fingerprint: source.publicFingerprint,
    frame: 12,
    layers: [
      {
        clip_id: clip.clip_id,
        asset_id: clip.asset_id,
        track_id: clip.track_id,
        source_frame: 12,
        source_pts: fixture.expectations.source_pts,
        transition_elapsed_frames: null,
        operation_ids: [
          "SelectSourceRangeV1",
          "CropV1",
          "Transform2DV1",
          "OpacityV1",
          "BlendV1",
        ],
        transform: clip.transform,
        crop: clip.crop,
        opacity_bp: clip.opacity_bp,
        blend: clip.blend,
        text: clip.text,
        effect: clip.effect,
      },
    ],
    audio_span: null,
    blockers: [],
  });
}

function videoResources(
  color: readonly [number, number, number, number],
): VisualLayerResources {
  const source = qualifiedVideoSource(color);
  return new Map([
    [
      "clip-main",
      {
        kind: "video" as const,
        source,
        nativeWidth: 640,
        nativeHeight: 360,
      },
    ],
  ]);
}

function primaryAndImageScene(
  source: PublicCompositionSnapshot,
): ResolvedCompositionScene {
  const layers = [fixture.snapshot.clips[0], fixture.snapshot.clips[2]].map(
    (clip, index) => ({
      clip_id: clip.clip_id,
      asset_id: clip.asset_id,
      track_id: clip.track_id,
      source_frame: index === 0 ? 12 : null,
      source_pts: index === 0 ? fixture.expectations.source_pts : null,
      transition_elapsed_frames: null,
      operation_ids: [
        "SelectSourceRangeV1",
        "CropV1",
        "Transform2DV1",
        "OpacityV1",
        "BlendV1",
      ],
      transform: clip.transform,
      crop: clip.crop,
      opacity_bp: clip.opacity_bp,
      blend: clip.blend,
      text: clip.text,
      effect: clip.effect,
    }),
  );
  return decodeResolvedScene({
    schema: "h3.context.resolved_scene.v1",
    profile_id: "h3.native_media_canvas_backend.v1",
    public_fingerprint: source.publicFingerprint,
    frame: 12,
    layers,
    audio_span: null,
    blockers: [],
  });
}

/** The primary video under the overlay video, whose own effect is a colour adjustment. */
function primaryAndVideoOverlayScene(
  source: PublicCompositionSnapshot,
): ResolvedCompositionScene {
  const layers = [fixture.snapshot.clips[0], fixture.snapshot.clips[1]].map(
    (clip, index) => ({
      clip_id: clip.clip_id,
      asset_id: clip.asset_id,
      track_id: clip.track_id,
      // Both layers are video, so both carry observed timing: the primary at its own frame 12,
      // the overlay at its first frame, which is where the composition's frame 12 starts it.
      source_frame: index === 0 ? 12 : 0,
      source_pts: index === 0 ? fixture.expectations.source_pts : 0,
      // The overlay starts on this very frame and carries a cross dissolve, so its layer is in
      // the first frame of that transition.
      transition_elapsed_frames: index === 0 ? null : 0,
      operation_ids: [
        "SelectSourceRangeV1",
        "CropV1",
        "Transform2DV1",
        ...(clip.effect.kind === "none" ? [] : ["ColorAdjustV1"]),
        "OpacityV1",
        "BlendV1",
        ...(index === 0 ? [] : ["CrossDissolveV1"]),
      ],
      transform: clip.transform,
      crop: clip.crop,
      opacity_bp: clip.opacity_bp,
      blend: clip.blend,
      text: clip.text,
      effect: clip.effect,
    }),
  );
  return decodeResolvedScene({
    schema: "h3.context.resolved_scene.v1",
    profile_id: "h3.native_media_canvas_backend.v1",
    public_fingerprint: source.publicFingerprint,
    frame: 12,
    layers,
    audio_span: null,
    blockers: [],
  });
}

function textSubject(): Readonly<{
  snapshot: PublicCompositionSnapshot;
  scene: ResolvedCompositionScene;
}> {
  const wire = structuredClone(fixture.snapshot);
  for (const track of wire.tracks)
    track.enabled = track.track_id === "track-text";
  for (const clip of wire.clips) {
    clip.enabled = clip.track_id === "track-text";
    if (!clip.enabled) clip.transition = { kind: "none", duration_frames: 0 };
  }
  wire.assets[3]!.asset_id = "h3.font.noto_sans.v1";
  wire.clips[3]!.text!.font_asset_id = "h3.font.noto_sans.v1";
  wire.clips[3]!.text!.content = "A\tB\r\nC";
  wire.public_fingerprint = publicCompositionFingerprint(wire);
  const accepted = decodePublicCompositionSnapshot(wire);
  const clip = wire.clips[3]!;
  const resolved = decodeResolvedScene({
    schema: "h3.context.resolved_scene.v1",
    profile_id: "h3.native_media_canvas_backend.v1",
    public_fingerprint: accepted.publicFingerprint,
    frame: 12,
    layers: [
      {
        clip_id: clip.clip_id,
        asset_id: null,
        track_id: clip.track_id,
        source_frame: null,
        source_pts: null,
        transition_elapsed_frames: null,
        operation_ids: [
          "SelectSourceRangeV1",
          "CropV1",
          "Transform2DV1",
          "OpacityV1",
          "BlendV1",
          "DrawTextV1",
        ],
        transform: clip.transform,
        crop: clip.crop,
        opacity_bp: clip.opacity_bp,
        blend: clip.blend,
        text: clip.text,
        effect: clip.effect,
      },
    ],
    audio_span: null,
    blockers: [],
  });
  return Object.freeze({ snapshot: accepted, scene: resolved });
}

describe("M25-45 preview size", () => {
  it("keeps the legacy size when no picture box has been measured", () => {
    expect(computePreviewSize(1920, 1080)).toEqual({
      width: 320,
      height: 180,
      scale: 1 / 6,
    });
    expect(computePreviewSize(320, 180)).toEqual({
      width: 320,
      height: 180,
      scale: 1,
    });
    // A zero or hidden box is not a measurement, and neither is a box with a non-finite or
    // non-positive device pixel ratio.
    for (const [box, ratio] of [
      [0, 1],
      [900, 0],
      [900, Number.NaN],
      [900, Number.POSITIVE_INFINITY],
    ] as const)
      expect(computePreviewSize(1280, 720, box, box, ratio)).toMatchObject({
        width: 320,
        height: 180,
      });
  });

  it("fills the measured picture box in device pixels, never above the composition or the cap", () => {
    // The box carries the composition's aspect ratio, so one side decides the scale.
    expect(computePreviewSize(1280, 720, 744, 418.5, 1)).toEqual({
      width: 744,
      height: 418,
      scale: 744 / 1280,
    });
    // Device pixels, not CSS pixels: the same box at DPR 2 asks for more than the composition has,
    // so the composition's own size is the ceiling.
    expect(computePreviewSize(1280, 720, 744, 418.5, 2)).toEqual({
      width: 1280,
      height: 720,
      scale: 1,
    });
    // The native rung may use the admitted 1920 x 1080 output at full resolution.
    expect(computePreviewSize(1920, 1080, 1900, 1068.75, 2)).toEqual({
      width: 1920,
      height: 1080,
      scale: 1,
    });
    // A portrait composition small enough for the cap presents at its own size.
    expect(computePreviewSize(608, 1080, 700, 1243.4, 1)).toEqual({
      width: 608,
      height: 1080,
      scale: 1,
    });
    // A wide composition below the admitted native cap presents at its own size.
    expect(computePreviewSize(1600, 600, 2000, 750, 1)).toEqual({
      width: 1600,
      height: 600,
      scale: 1,
    });
    // The software rung retains its measured 640 x 360 ceiling.
    expect(computePreviewSize(1920, 1080, 1920, 1080, 1, "software")).toEqual({
      width: 640,
      height: 360,
      scale: 1 / 3,
    });
  });

  it("uses the legacy size as a scale floor, not as a separate width and height", () => {
    // A pane far too small for the composition still gets the legacy surface, so every existing
    // 320 x 180 corpus row keeps presenting at scale 1 in any pane.
    expect(computePreviewSize(320, 180, 64, 36, 1)).toEqual({
      width: 320,
      height: 180,
      scale: 1,
    });
    // The floor is the legacy scale of this composition, not 320 on each side: a 1280 x 720
    // composition in a tiny pane falls back to exactly what it presents today.
    expect(computePreviewSize(1280, 720, 64, 36, 1)).toEqual({
      width: 320,
      height: 180,
      scale: 0.25,
    });
  });

  it("quantizes to even pixels and refuses an unsupported composition", () => {
    const size = computePreviewSize(1280, 720, 501, 1000, 1);
    expect(size.width % 2).toBe(0);
    expect(size.height % 2).toBe(0);
    expect(size).toMatchObject({ width: 502, height: 282 });
    expect(Math.abs(size.width / size.height - 1280 / 720)).toBeLessThan(0.02);
    expect(() => computePreviewSize(1281, 720, 500, 281, 1)).toThrow(
      /output dimensions/u,
    );
    expect(() => computePreviewSize(1920, 1081, 500, 281, 1)).toThrow(
      /closed integer range/u,
    );
  });
});

describe("M25-45 compositor ladder", () => {
  const box = { cssWidth: 744, cssHeight: 418.5, devicePixelRatio: 1 } as const;
  // A composition large enough for the caps to matter; the fixture's own 320 x 180 output is
  // already below the legacy floor, where every path produces the same surface.
  const HD = { width: 1280, height: 720 } as const;

  it("renders natively when every enabled effect is the identity", () => {
    const { canvas, context, parent } = harness({ attached: true });
    const subject = snapshot(["track-primary", "track-image"], HD);
    const compositor = createVisualCompositor(canvas, subject, box);
    expect(compositor.path()).toBe("native");
    // No colour adjustment is present, so no filter element is created at all.
    expect(parent!.querySelectorAll("filter")).toHaveLength(0);
    expect(canvas.width).toBe(744);
    expect(canvas.height).toBe(418);

    const receipt = compositor.render(
      primaryAndImageScene(subject),
      new Map([
        ...videoResources([10, 20, 30, 255]),
        [
          "clip-image",
          {
            kind: "image" as const,
            source: qualifiedImageSource([40, 50, 60, 255]),
            nativeWidth: 640,
            nativeHeight: 360,
          },
        ],
      ]),
      1,
    );
    expect(receipt.status).toBe("presented");
    expect(receipt.previewWidth).toBe(744);
    expect(compositor.layerGeometry("clip-main")).toMatchObject({
      scaledWidth: 640,
      scaledHeight: 360,
      centerX: 640,
      centerY: 360,
      angleRadians: 0,
    });
    expect(compositor.layerGeometry("missing")).toBeNull();
    // Each layer carries its own opacity and blend into the engine, and nothing reads the surface
    // back: the whole point of this rung is that there is no read-modify-write per layer.
    expect(context.layerStates).toHaveLength(2);
    expect(context.layerStates[0]!.operation).toBe("source-over");
    expect(context.layerStates[1]!.operation).toBe("multiply");
    for (const state of context.layerStates) expect(state.filter).toBe("none");
    compositor.close();
    expect(compositor.layerGeometry("clip-main")).toBeNull();
    expect(document.querySelectorAll("[data-h3-visual-filters]")).toHaveLength(
      0,
    );
  });

  it("probes the colour filter, uses it per layer, and removes its host on close", () => {
    const { canvas, context, parent } = harness({ attached: true });
    const subject = snapshot(["track-primary", "track-video"], HD);
    const compositor = createVisualCompositor(canvas, subject, box);
    expect(compositor.path()).toBe("native");
    // One probe filter plus one per distinct adjustment in the composition.
    expect(parent!.querySelectorAll("filter").length).toBeGreaterThanOrEqual(2);
    const receipt = compositor.render(
      primaryAndVideoOverlayScene(subject),
      new Map([
        ...videoResources([10, 20, 30, 255]),
        [
          "clip-video-overlay",
          {
            kind: "video" as const,
            source: qualifiedVideoSource([70, 80, 90, 255], "clip-overlay"),
            nativeWidth: 640,
            nativeHeight: 360,
          },
        ],
      ]),
      1,
    );
    expect(receipt.status).toBe("presented");
    expect(receipt.blocker).toBeNull();
    expect(context.layerStates[0]!.filter).toBe("none");
    expect(context.layerStates[1]!.filter).toMatch(/^url\(#h3-visual-filter-/u);
    compositor.close();
    expect(parent!.querySelectorAll("filter")).toHaveLength(0);
  });

  it("falls back to the software transaction at its lower cap when the probe fails", () => {
    const { canvas, context, parent } = harness({ attached: true });
    context.filterIsInert = true;
    const subject = snapshot(["track-primary", "track-video"], HD);
    const compositor = createVisualCompositor(canvas, subject, box);
    expect(compositor.path()).toBe("software");
    // An engine that accepts the assignment and applies nothing must not keep the rung, and no
    // half-installed filter element may be left behind for a later frame to reference.
    expect(parent!.querySelectorAll("filter")).toHaveLength(0);
    expect(canvas.width).toBe(640);
    expect(canvas.height).toBe(360);
  });

  it("takes the software path with no owned element to host its filters", () => {
    const { canvas } = harness();
    const compositor = createVisualCompositor(
      canvas,
      snapshot(["track-primary", "track-video"], HD),
      box,
    );
    expect(compositor.path()).toBe("software");
    expect(canvas.width).toBe(640);
  });

  it("resizes to a new box, does nothing at an unchanged size, and paints no old pixels", () => {
    const { canvas, context } = harness({ attached: true });
    const subject = snapshot(["track-primary"], HD);
    const compositor = createVisualCompositor(canvas, subject, box);
    expect(
      compositor.render(scene(subject), videoResources([9, 9, 9, 255]), 1),
    ).toMatchObject({ status: "presented" });

    const grown = compositor.resize({
      cssWidth: 1200,
      cssHeight: 675,
      devicePixelRatio: 1,
    });
    expect(grown.previewWidth).toBe(1200);
    expect(canvas.width).toBe(1200);
    // The frame is forgotten rather than reconstructed: a resize is not an observation.
    expect(grown.frame).toBeNull();
    expect(context.pixel()).toEqual([0, 0, 0, 255]);

    const again = compositor.resize({
      cssWidth: 1200.4,
      cssHeight: 675.2,
      devicePixelRatio: 1,
    });
    expect(again.previewWidth).toBe(1200);
    expect(canvas.width).toBe(1200);
  });
});

describe("retained compositor revision binding", () => {
  function revision(color = false, turn = 1) {
    const wire = structuredClone(fixture.snapshot);
    wire.timeline_revision += turn;
    wire.output.duration_frames = 96;
    for (const track of wire.tracks)
      track.enabled = track.track_id === "track-primary";
    for (const clip of wire.clips) {
      clip.enabled = clip.track_id === "track-primary";
      if (!clip.enabled) clip.transition = { kind: "none", duration_frames: 0 };
    }
    if (color)
      wire.clips[0]!.effect = {
        ...wire.clips[1]!.effect,
        brightness_permille: turn,
      };
    wire.public_fingerprint = publicCompositionFingerprint(wire);
    return decodePublicCompositionSnapshot(wire);
  }

  function revisedScene(subject: PublicCompositionSnapshot) {
    const base = scene(subject);
    const clip = subject.clips[0]!;
    return {
      ...base,
      layers: [
        {
          ...base.layers[0]!,
          effect: clip.effect,
          operationIds: [
            "SelectSourceRangeV1",
            "CropV1",
            "Transform2DV1",
            ...(clip.effect.kind === "none" ? [] : ["ColorAdjustV1"]),
            "OpacityV1",
            "BlendV1",
          ],
        },
      ],
    };
  }

  it("retains a bounded filter cache across distinct effects and on/off/on revisions", () => {
    const { canvas, parent } = harness({ attached: true });
    const NativeMap = Map;
    let peak = 0;
    class CountedMap<K, V> extends NativeMap<K, V> {
      override set(key: K, value: V): this {
        super.set(key, value);
        if (typeof value === "string" && value.startsWith("h3-visual-filter-"))
          peak = Math.max(peak, this.size);
        return this;
      }
    }
    vi.stubGlobal("Map", CountedMap);
    const compositor = createVisualCompositor(canvas, snapshot());
    try {
      for (let turn = 1; turn < 12; turn++) {
        const next = revision(true, turn);
        expect(compositor.replace(next)).toBe(true);
        expect(parent!.querySelectorAll("filter")).toHaveLength(1);
        expect(peak).toBeLessThanOrEqual(1);
        expect(compositor.replace(revision(false, 30 + turn))).toBe(true);
        expect(parent!.querySelectorAll("filter")).toHaveLength(0);
      }
    } finally {
      compositor.close();
      vi.unstubAllGlobals();
    }
  });

  it("keeps the backing pixels while updating fingerprint, duration and scene authority", () => {
    const { canvas, context } = harness({ attached: true });
    const first = snapshot();
    const compositor = createVisualCompositor(canvas, first);
    const resources = videoResources([10, 20, 30, 255]);
    expect(compositor.render(scene(first), resources, 1).status).toBe(
      "presented",
    );
    const width = vi.spyOn(canvas, "width", "set");
    const height = vi.spyOn(canvas, "height", "set");
    const fill = vi.spyOn(context, "fillRect");
    const next = revision();
    expect(compositor.replace(next)).toBe(true);
    expect(context.pixel()).toEqual([10, 20, 30, 255]);
    expect(width).not.toHaveBeenCalled();
    expect(height).not.toHaveBeenCalled();
    expect(fill).not.toHaveBeenCalled();
    expect(compositor.render(revisedScene(next), resources, 2)).toMatchObject({
      status: "presented",
      frame: 12,
      publicFingerprint: next.publicFingerprint,
    });
    expect(
      compositor.render({ ...scene(next), frame: 60, layers: [] }, new Map(), 3)
        .status,
    ).toBe("presented");
    expect(compositor.render(scene(first), resources, 4).blocker).toBe(
      "stale_snapshot",
    );
    compositor.close();
  });

  it("proves newly needed filters without losing the held picture and bounds definitions across edits", () => {
    const { canvas, context, parent } = harness({ attached: true });
    const compositor = createVisualCompositor(canvas, snapshot());
    const resources = videoResources([10, 20, 30, 255]);
    compositor.render(scene(snapshot()), resources, 1);
    const width = vi.spyOn(canvas, "width", "set");
    const first = revision(true);
    expect(compositor.replace(first)).toBe(true);
    expect(context.pixel()).toEqual([10, 20, 30, 255]);
    expect(compositor.render(revisedScene(first), resources, 2).status).toBe(
      "presented",
    );
    expect(context.layerStates.at(-1)!.filter).toMatch(
      /^url\(#h3-visual-filter-/u,
    );
    const firstMatrix = parent!
      .querySelector("filter feColorMatrix")!
      .getAttribute("values");
    for (let turn = 2; turn < 20; turn++) {
      const next = revision(true, turn);
      expect(compositor.replace(next)).toBe(true);
      expect(parent!.querySelectorAll("filter")).toHaveLength(1);
      expect(
        compositor.render(revisedScene(next), resources, turn + 1).status,
      ).toBe("presented");
    }
    expect(
      parent!.querySelector("filter feColorMatrix")!.getAttribute("values"),
    ).not.toBe(firstMatrix);
    expect(width).not.toHaveBeenCalled();
    compositor.close();
    expect(parent!.querySelectorAll("filter")).toHaveLength(0);
  });

  it.each(["profile", "workspace", "width", "height"])(
    "refuses an independently changed %s before adopting its metadata",
    (field) => {
      const { canvas } = harness({ attached: true });
      const first = snapshot();
      const compositor = createVisualCompositor(canvas, first);
      let next = revision();
      if (field === "profile")
        next = {
          ...next,
          profileId: "another-profile" as typeof next.profileId,
        };
      if (field === "workspace")
        next = { ...next, workspaceHandle: "another-workspace" };
      if (field === "width")
        next = {
          ...next,
          output: { ...next.output, width: Number(next.output.width) + 1 },
        };
      if (field === "height")
        next = {
          ...next,
          output: { ...next.output, height: Number(next.output.height) + 1 },
        };
      expect(compositor.replace(next)).toBe(false);
      expect(
        compositor.render(scene(first), videoResources([10, 20, 30, 255]), 1)
          .status,
      ).toBe("presented");
      compositor.close();
    },
  );

  it("refuses a reentrant replacement from the canvas while a frame is being painted", () => {
    const { canvas, context } = harness({ attached: true });
    const first = snapshot();
    const compositor = createVisualCompositor(canvas, first);
    const replace = vi.fn(() => compositor.replace(revision()));
    context.onDrawImage = replace;
    expect(
      compositor.render(scene(first), videoResources([10, 20, 30, 255]), 1)
        .status,
    ).toBe("presented");
    expect(replace).toHaveReturnedWith(false);
    compositor.close();
  });

  it("refuses an already lost canvas without adopting a new snapshot", () => {
    const { canvas, context } = harness({ attached: true });
    const compositor = createVisualCompositor(canvas, snapshot());
    context.lost = true;
    expect(compositor.replace(revision())).toBe(false);
    compositor.close();
  });

  it.each([0, 1, 2, 3])(
    "refuses a filter probe when only channel %s is incorrect",
    (channel) => {
      const { canvas, context } = harness({ attached: true });
      const compositor = createVisualCompositor(canvas, snapshot());
      compositor.render(
        scene(snapshot()),
        videoResources([10, 20, 30, 255]),
        1,
      );
      const original = context.getImageData.bind(context);
      const corrupt = [255, 0, 0, 255];
      corrupt[channel] = channel === 0 || channel === 3 ? 254 : 1;
      const readback = vi.spyOn(context, "getImageData");
      readback.mockImplementationOnce(original).mockReturnValueOnce({
        colorSpace: "srgb",
        data: new Uint8ClampedArray(corrupt),
        width: 1,
        height: 1,
      });
      expect(compositor.replace(revision(true))).toBe(false);
      readback.mockRestore();
      expect(context.pixel()).toEqual([10, 20, 30, 255]);
      compositor.close();
    },
  );

  it("drops obsolete filters when the new revision removes the last adjustment", () => {
    const { canvas, parent } = harness({ attached: true });
    const first = revision(true);
    const compositor = createVisualCompositor(canvas, first);
    compositor.render(
      revisedScene(first),
      videoResources([10, 20, 30, 255]),
      1,
    );
    expect(parent!.querySelectorAll("filter").length).toBeGreaterThan(0);
    const next = revision(false, 2);
    expect(compositor.replace(next)).toBe(true);
    expect(parent!.querySelectorAll("filter")).toHaveLength(0);
    expect(
      compositor.render(
        revisedScene(next),
        videoResources([10, 20, 30, 255]),
        2,
      ).status,
    ).toBe("presented");
    compositor.close();
  });

  it("normalizes the probe state and restores a nondefault held canvas state", () => {
    const { canvas, context } = harness({ attached: true });
    const compositor = createVisualCompositor(canvas, snapshot());
    context.globalAlpha = 0.25;
    context.globalCompositeOperation = "multiply";
    context.fillStyle = "#ffffff";
    const transform = vi.spyOn(context, "setTransform");
    const originalFill = context.fillRect.bind(context);
    vi.spyOn(context, "fillRect").mockImplementationOnce(() => {
      expect(transform).toHaveBeenLastCalledWith(1, 0, 0, 1, 0, 0);
      expect(context.globalAlpha).toBe(1);
      expect(context.globalCompositeOperation).toBe("source-over");
      expect(context.fillStyle).toBe("#000000");
      originalFill();
    });
    expect(compositor.replace(revision(true))).toBe(true);
    expect(context.globalAlpha).toBe(0.25);
    expect(context.globalCompositeOperation).toBe("multiply");
    expect(context.fillStyle).toBe("#ffffff");
    compositor.close();
  });

  it("refuses an unaccepted filter assignment even when handed readback claims red", () => {
    const { canvas, context } = harness({ attached: true });
    const compositor = createVisualCompositor(canvas, snapshot());
    Object.defineProperty(context, "filter", {
      configurable: true,
      get: () => "none",
      set: () => undefined,
    });
    vi.spyOn(context, "getImageData").mockReturnValue({
      colorSpace: "srgb",
      data: new Uint8ClampedArray([255, 0, 0, 255]),
      width: 1,
      height: 1,
    });
    expect(compositor.replace(revision(true))).toBe(false);
    compositor.close();
  });

  it("keeps an already proven filter rung without another pixel probe", () => {
    const { canvas, context } = harness({ attached: true });
    const compositor = createVisualCompositor(canvas, revision(true));
    const readback = vi.spyOn(context, "getImageData");
    expect(compositor.replace(revision(true, 2))).toBe(true);
    expect(readback).not.toHaveBeenCalled();
    compositor.close();
  });

  it("does not accumulate retired filter keys across successive effects", () => {
    const { canvas, parent } = harness({ attached: true });
    const compositor = createVisualCompositor(canvas, snapshot());
    for (let index = 1; index <= 20; index++) {
      const next = revision(true, index);
      expect(compositor.replace(next)).toBe(true);
      const installed = [...parent!.querySelectorAll("filter")];
      expect(installed).toHaveLength(1);
      // The generated ordinal exposes the number of retained keys without reading private state.
      expect(installed[0]!.id.split("-").at(-1)).toBe("0");
    }
    compositor.close();
  });

  it("uses software when an initial effect has nowhere to attach its filter host", () => {
    const { canvas } = harness();
    const compositor = createVisualCompositor(canvas, revision(true));
    expect(compositor.path()).toBe("software");
    compositor.close();
  });

  it("keeps an existing software effect rung for another filtered revision", () => {
    const { canvas } = harness();
    const compositor = createVisualCompositor(canvas, revision(true));
    expect(compositor.path()).toBe("software");
    expect(compositor.replace(revision(true, 2))).toBe(true);
    expect(compositor.path()).toBe("software");
    compositor.close();
  });
  it("refuses removing the software effect rung even when a new revision otherwise matches", () => {
    const { canvas } = harness();
    const compositor = createVisualCompositor(canvas, revision(true));
    expect(compositor.replace(revision(false, 2))).toBe(false);
    compositor.close();
  });
  it("refuses a filter host moved independently away from the owned canvas", () => {
    const { canvas, parent } = harness({ attached: true });
    const compositor = createVisualCompositor(canvas, revision(true));
    const foreign = document.createElement("div");
    foreign.append(parent!.querySelector("svg")!);
    expect(compositor.replace(revision(true, 2))).toBe(false);
    compositor.close();
  });
  it("returns false when a previously proven filter host cannot be recreated", () => {
    const { canvas } = harness({ attached: true });
    const compositor = createVisualCompositor(canvas, revision(true));
    expect(compositor.replace(revision(false, 2))).toBe(true);
    Object.defineProperty(canvas, "parentElement", {
      value: null,
      configurable: true,
    });
    expect(compositor.replace(revision(true, 3))).toBe(false);
    compositor.close();
  });
  it("returns false for a malformed replacement output handed directly to it", () => {
    const { canvas } = harness({ attached: true });
    const compositor = createVisualCompositor(canvas, snapshot());
    expect(
      compositor.replace({
        ...revision(),
        output: null,
      } as unknown as PublicCompositionSnapshot),
    ).toBe(false);
    compositor.close();
  });

  it("refuses replacement when the canvas has supplied no context", () => {
    const subject = harness({ attached: true });
    subject.setAvailable(false);
    const compositor = createVisualCompositor(subject.canvas, snapshot());
    expect(compositor.replace(revision())).toBe(false);
    compositor.close();
  });

  it("refuses raster or filter capability changes without installing a partial new binding", () => {
    const { canvas, context, parent } = harness({ attached: true });
    const first = snapshot();
    const compositor = createVisualCompositor(canvas, first);
    const resources = videoResources([10, 20, 30, 255]);
    compositor.render(scene(first), resources, 1);
    expect(
      compositor.replace(
        snapshot(["track-primary"], { width: 640, height: 360 }),
      ),
    ).toBe(false);
    context.filterIsInert = true;
    expect(compositor.replace(revision(true))).toBe(false);
    expect(context.pixel()).toEqual([10, 20, 30, 255]);
    expect(parent!.querySelectorAll("filter")).toHaveLength(0);
    expect(compositor.render(scene(first), resources, 2).status).toBe(
      "presented",
    );
    compositor.close();
    expect(compositor.replace(first)).toBe(false);
  });
});

describe("M25-14 visual compositor", () => {
  it("freezes preview and original-to-derivative geometry without a fit mode", () => {
    expect(computePreviewSize(1920, 1080)).toEqual({
      width: 320,
      height: 180,
      scale: 1 / 6,
    });
    expect(
      computeVisualLayerGeometry(
        1920,
        1080,
        320,
        180,
        { left_bp: 2500, top_bp: 1000, right_bp: 0, bottom_bp: 1000 },
        {
          anchor_x_bp: 5000,
          anchor_y_bp: 5000,
          position_x_bp: 0,
          position_y_bp: 0,
          scale_x_bp: 5000,
          scale_y_bp: 5000,
          rotation_mdeg: 0,
        },
        1920,
        1080,
      ),
    ).toMatchObject({
      nativeCrop: { left: 480, top: 108, width: 1440, height: 864 },
      sourceCrop: { left: 80, top: 18, width: 240, height: 144 },
      scaledWidth: 720,
      scaledHeight: 432,
      centerX: 960,
      centerY: 540,
    });
  });

  it("uses the frozen encoded-BT.709 color adjustment and exact identity", () => {
    const original = new Uint8ClampedArray([255, 0, 0, 127]);
    expect([
      ...applyVisualColorAdjust(original, {
        kind: "none",
        brightnessPermille: 0,
        contrastPermille: 1000,
        saturationPermille: 1000,
      }),
    ]).toEqual([255, 0, 0, 127]);
    // The renderer's `eq` at saturation 0 leaves the chroma planes at 127, not 128 (its fixed
    // point brightness term is 511/2 - 128), so a desaturated red comes back a hair off grey.
    expect([
      ...applyVisualColorAdjust(original, {
        kind: "color_adjust_v1",
        brightnessPermille: 0,
        contrastPermille: 1000,
        saturationPermille: 0,
      }),
    ]).toEqual([52, 55, 52, 127]);
    expect([...original]).toEqual([255, 0, 0, 127]);
  });

  // M25-20 B-64: the preview's colour adjustment is the renderer's 8-bit-plane fixed-point `eq`,
  // not a floating-point approximation of it. Each expectation below is
  // `semantic_conformance_expect._adjusted` evaluated on the same input and rounded -- the
  // oracle the final artifact is judged against -- and the saturation-2.0 rows are the regime
  // where a chroma plane clamps and the float model was fifteen code values off.
  it.each([
    [[255, 0, 0], 0, 1000, 2000, [254, 6, 0]],
    [[30, 160, 40], 0, 1000, 2000, [0, 197, 0]],
    [[200, 30, 30], 200, 2000, 1000, [188, 18, 19]],
    [[90, 120, 200], -200, 600, 600, [49, 69, 114]],
    [[17, 17, 17], 0, 1000, 1000, [17, 17, 17]],
  ] as const)(
    "matches the renderer's eq path for %j at brightness %i contrast %i saturation %i",
    (rgb, brightness, contrast, saturation, expected) => {
      const adjusted = applyVisualColorAdjust(
        new Uint8ClampedArray([...rgb, 255]),
        {
          kind: "color_adjust_v1",
          brightnessPermille: brightness,
          contrastPermille: contrast,
          saturationPermille: saturation,
        },
      );
      for (const [index, value] of expected.entries())
        expect(Math.abs(adjusted[index]! - value)).toBeLessThanOrEqual(1);
    },
  );

  it("composites normal, multiply and screen in emitted order with per-layer rounding", () => {
    const base = new Uint8ClampedArray([0, 0, 0, 255]);
    compositeVisualLayer(
      base,
      new Uint8ClampedArray([255, 0, 0, 128]),
      10_000,
      1,
      "normal",
    );
    expect([...base]).toEqual([128, 0, 0, 255]);
    compositeVisualLayer(
      base,
      new Uint8ClampedArray([0, 0, 255, 255]),
      5_000,
      1,
      "normal",
    );
    expect([...base]).toEqual([64, 0, 128, 255]);

    const multiply = new Uint8ClampedArray([128, 128, 128, 255]);
    compositeVisualLayer(
      multiply,
      new Uint8ClampedArray([128, 255, 64, 255]),
      10_000,
      1,
      "multiply",
    );
    expect([...multiply]).toEqual([64, 128, 32, 255]);

    const screen = new Uint8ClampedArray([128, 128, 128, 255]);
    compositeVisualLayer(
      screen,
      new Uint8ClampedArray([128, 255, 0, 255]),
      10_000,
      1,
      "screen",
    );
    expect([...screen]).toEqual([192, 255, 128, 255]);
  });

  it("freezes dissolve endpoints and the narrower text admission", () => {
    expect(dissolveFactor(null, 12)).toBe(1);
    expect(dissolveFactor(0, 12)).toBe(0);
    expect(dissolveFactor(11, 12)).toBe(11 / 12);
    expect(() => dissolveFactor(12, 12)).toThrow(/invalid_contract/);
    expect(validateVisualText("one\r\ntwo")).toEqual(["one", "two"]);
    for (const invalid of [
      "one\rtwo",
      "one\u0085two",
      "one\u2028two",
      "\ud800",
    ])
      expect(() => validateVisualText(invalid)).toThrow(/invalid_contract/);
  });

  it("renders through one Canvas2D surface and publishes a closed public receipt", () => {
    const accepted = snapshot();
    const { canvas, context } = harness();
    const compositor = createVisualCompositor(canvas, accepted);
    const receipt = compositor.render(
      scene(accepted),
      videoResources([255, 0, 0, 255]),
      1,
    );
    expect(receipt).toMatchObject({
      schema: "h3.visual_compositor_receipt.v1",
      status: "presented",
      frame: 12,
      generation: 1,
      previewWidth: 320,
      previewHeight: 180,
      renderedLayerCount: 1,
      blocker: null,
      browserPreviewOnly: true,
    });
    expect(context.pixel()).toEqual([255, 0, 0, 255]);
    expect(context.imageSmoothingEnabled).toBe(true);
    expect(context.imageSmoothingQuality).toBe("low");
    expect(canvas.getContext).toHaveBeenCalledTimes(1);
  });

  it("draws every active layer once in the resolver's emitted bottom-first order", () => {
    const accepted = snapshot(["track-primary", "track-image"]);
    const { canvas, context } = harness();
    const resources: VisualLayerResources = new Map([
      ...videoResources([255, 0, 0, 255]),
      [
        "clip-image",
        {
          kind: "image" as const,
          source: qualifiedImageSource([128, 128, 128, 255]),
          nativeWidth: 320,
          nativeHeight: 180,
        },
      ],
    ]);
    expect(
      createVisualCompositor(canvas, accepted).render(
        primaryAndImageScene(accepted),
        resources,
        1,
      ),
    ).toMatchObject({ status: "presented", renderedLayerCount: 2 });
    expect(context.drawOrder).toEqual(["clip-main", "clip-image"]);
  });

  it("draws text only with the qualified private family and explicit tab stops", () => {
    const subject = textSubject();
    const { canvas, context } = harness();
    const receipt = createVisualCompositor(canvas, subject.snapshot).render(
      subject.scene,
      new Map([
        ["clip-title", { kind: "font" as const, family: "H3-M25-14-Noto" }],
      ]),
      1,
    );
    expect(receipt).toMatchObject({
      status: "presented",
      renderedLayerCount: 1,
    });
    expect(context.font).toContain('"H3-M25-14-Noto"');
    expect(context.textDraws.map(({ text }) => text)).toEqual(["A", "B", "C"]);
    expect(context.textDraws[1]!.x).toBeGreaterThan(context.textDraws[0]!.x);
  });

  it("rejects scriptable DOM sources and image-video brand mismatches before drawing", () => {
    const accepted = snapshot(["track-primary", "track-image"]);
    const resolved = primaryAndImageScene(accepted);
    const img = document.createElement("img");
    img.id = "scriptable-img";
    Object.defineProperties(img, {
      naturalWidth: { configurable: true, value: 320 },
      naturalHeight: { configurable: true, value: 180 },
      color: { configurable: true, value: [0, 0, 0, 255] },
    });
    const canvas = document.createElement("canvas");
    canvas.id = "scriptable-canvas";
    canvas.width = 320;
    canvas.height = 180;
    Object.defineProperty(canvas, "color", {
      configurable: true,
      value: [0, 0, 0, 255],
    });
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.id = "scriptable-svg";
    const OffscreenCanvasFixture = class OffscreenCanvas {
      readonly width = 320;
      readonly height = 180;
      readonly color = [0, 0, 0, 255] as const;
      readonly id = "scriptable-offscreen";
    };
    const offscreen =
      typeof globalThis.OffscreenCanvas === "function"
        ? new globalThis.OffscreenCanvas(320, 180)
        : new OffscreenCanvasFixture();
    if (!("id" in offscreen))
      Object.defineProperty(offscreen, "id", {
        configurable: true,
        value: "scriptable-offscreen",
      });

    for (const source of [img, canvas, svg, offscreen]) {
      const subject = harness();
      const resources: VisualLayerResources = new Map([
        ...videoResources([255, 0, 0, 255]),
        [
          "clip-image",
          {
            kind: "image" as const,
            source: source as CanvasImageSource,
            nativeWidth: 320,
            nativeHeight: 180,
          },
        ],
      ]);
      expect(
        createVisualCompositor(subject.canvas, accepted).render(
          resolved,
          resources,
          1,
        ),
      ).toMatchObject({ status: "unavailable", blocker: "source_unavailable" });
      expect(subject.context.drawOrder).toEqual([]);
    }

    const videoKindMismatch = harness();
    expect(
      createVisualCompositor(videoKindMismatch.canvas, snapshot()).render(
        scene(snapshot()),
        new Map([
          [
            "clip-main",
            {
              kind: "video" as const,
              source: qualifiedImageSource([255, 0, 0, 255]),
              nativeWidth: 640,
              nativeHeight: 360,
            },
          ],
        ]),
        1,
      ),
    ).toMatchObject({ status: "unavailable", blocker: "source_unavailable" });
    expect(videoKindMismatch.context.drawOrder).toEqual([]);

    const imageKindMismatch = harness();
    const mismatchedResources: VisualLayerResources = new Map([
      ...videoResources([255, 0, 0, 255]),
      [
        "clip-image",
        {
          kind: "image" as const,
          source: qualifiedVideoSource([0, 0, 0, 255], "video-as-image"),
          nativeWidth: 320,
          nativeHeight: 180,
        },
      ],
    ]);
    expect(
      createVisualCompositor(imageKindMismatch.canvas, accepted).render(
        resolved,
        mismatchedResources,
        1,
      ),
    ).toMatchObject({ status: "unavailable", blocker: "source_unavailable" });
    expect(imageKindMismatch.context.drawOrder).toEqual([]);
  });

  it("renders a valid gap as opaque black and fails closed on source or scene drift", () => {
    const accepted = snapshot();
    const gapSnapshot = snapshot([]);
    const gapWire = {
      schema: "h3.context.resolved_scene.v1",
      profile_id: "h3.native_media_canvas_backend.v1",
      public_fingerprint: gapSnapshot.publicFingerprint,
      frame: 12,
      layers: [],
      audio_span: null,
      blockers: [],
    };
    const first = harness();
    const gap = createVisualCompositor(first.canvas, gapSnapshot).render(
      decodeResolvedScene(gapWire),
      new Map(),
      1,
    );
    expect(gap.status).toBe("presented");
    expect(gap.renderedLayerCount).toBe(0);
    expect(first.context.pixel()).toEqual([0, 0, 0, 255]);

    const unavailable = harness();
    const compositor = createVisualCompositor(unavailable.canvas, accepted);
    expect(compositor.render(scene(accepted), new Map(), 1)).toMatchObject({
      status: "unavailable",
      blocker: "source_unavailable",
    });
    expect(unavailable.context.pixel()).toEqual([0, 0, 0, 255]);

    const missingGeometry = {
      kind: "video",
      source: qualifiedVideoSource([255, 0, 0, 255]),
      nativeWidth: undefined,
      nativeHeight: 360,
    } as unknown as VisualLayerResource;
    expect(
      compositor.render(
        scene(accepted),
        new Map([["clip-main", missingGeometry]]),
        2,
      ),
    ).toMatchObject({
      status: "unavailable",
      blocker: "source_unavailable",
    });

    expect(
      compositor.render(
        scene(accepted, { transformX: 1 }),
        videoResources([255, 0, 0, 255]),
        3,
      ),
    ).toMatchObject({ status: "unavailable", blocker: "invalid_contract" });
  });

  it("discards stale generations without overwriting the newest presentation and closes idempotently", () => {
    const accepted = snapshot();
    const { canvas, context } = harness();
    const compositor = createVisualCompositor(canvas, accepted);
    compositor.render(scene(accepted), videoResources([0, 255, 0, 255]), 4);
    expect(context.pixel()).toEqual([0, 255, 0, 255]);
    expect(
      compositor.render(scene(accepted), videoResources([255, 0, 0, 255]), 3),
    ).toMatchObject({ status: "unavailable", blocker: "stale_snapshot" });
    expect(context.pixel()).toEqual([0, 255, 0, 255]);

    expect(compositor.close()).toMatchObject({
      status: "closed",
      blocker: "closed",
    });
    expect(compositor.close()).toMatchObject({
      status: "closed",
      blocker: "closed",
    });
    expect(
      compositor.render(scene(accepted), videoResources([255, 0, 0, 255]), 5),
    ).toMatchObject({
      status: "closed",
      blocker: "closed",
    });
  });

  it("returns unavailable while Canvas2D is missing and recovers on a later render", () => {
    const accepted = snapshot();
    const subject = harness();
    subject.setAvailable(false);
    const compositor = createVisualCompositor(subject.canvas, accepted);
    expect(
      compositor.render(scene(accepted), videoResources([255, 0, 0, 255]), 1),
    ).toMatchObject({
      status: "unavailable",
      blocker: "canvas_unavailable",
      renderedLayerCount: 0,
    });

    subject.setAvailable(true);
    expect(
      compositor.render(scene(accepted), videoResources([0, 255, 0, 255]), 2),
    ).toMatchObject({ status: "presented", blocker: null, generation: 2 });
    expect(subject.context.pixel()).toEqual([0, 255, 0, 255]);
  });

  it("keeps clear and close fail-closed across context loss and permits recovery", () => {
    const accepted = snapshot();
    const subject = harness();
    const compositor = createVisualCompositor(subject.canvas, accepted);
    expect(
      compositor.render(scene(accepted), videoResources([255, 0, 0, 255]), 1)
        .status,
    ).toBe("presented");

    subject.context.lost = true;
    expect(
      compositor.render(scene(accepted), videoResources([0, 255, 0, 255]), 2),
    ).toMatchObject({
      status: "unavailable",
      blocker: "canvas_unavailable",
      renderedLayerCount: 0,
    });
    expect(compositor.clear(3)).toMatchObject({
      status: "unavailable",
      blocker: "canvas_unavailable",
      renderedLayerCount: 0,
    });
    subject.context.lost = false;
    expect(
      compositor.render(scene(accepted), videoResources([0, 0, 255, 255]), 4)
        .status,
    ).toBe("presented");

    subject.context.lost = true;
    expect(() => compositor.close()).not.toThrow();
    expect(compositor.close()).toMatchObject({
      status: "closed",
      blocker: "closed",
    });
  });

  it("refuses a synchronous reentrant render without publishing it", () => {
    const accepted = snapshot();
    const subject = harness();
    const compositor = createVisualCompositor(subject.canvas, accepted);
    let nestedStatus: string | undefined;
    let nestedBlocker: string | null | undefined;
    subject.context.onDrawImage = () => {
      const receipt = compositor.render(
        scene(accepted),
        videoResources([0, 0, 255, 255]),
        2,
      );
      nestedStatus = receipt.status;
      nestedBlocker = receipt.blocker;
    };
    expect(
      compositor.render(scene(accepted), videoResources([255, 0, 0, 255]), 1)
        .status,
    ).toBe("presented");
    expect(nestedStatus).toBe("unavailable");
    expect(nestedBlocker).toBe("resource_limit");
  });
});
