import type { AuthoringAudioPeaks } from "../contracts/authoringAudioPeaks";

export type WaveformPaintAsset = Readonly<{
  assetId: string;
  kind: "video";
  embeddedAudio:
    "present_bound" | "absent" | "unavailable" | "excluded_overlay_policy";
  sourceSampleCount: number | null;
  sourceTimeBase: Readonly<{ num: number; den: number }>;
  sourceFrameCount: number;
  landmarks: readonly Readonly<{
    frameIndex: number;
    pts: number;
    dts: number;
    durationTicks: number;
  }>[];
}>;

export type WaveformPaintInput = Readonly<{
  asset: WaveformPaintAsset;
  /** The timeline's rate: `clip.durationFrames` counts output frames at it (B-M2564-03). */
  outputFrameRate: Readonly<{ num: number; den: number }>;
  clip: Readonly<{ sourceStartFrame: number; durationFrames: number }>;
  bounds: Readonly<{ x: number; y: number; width: number; height: number }>;
  visible: Readonly<{ x: number; width: number }>;
  envelope: AuthoringAudioPeaks;
}>;

export type WaveformPaintColumn = Readonly<{
  x: number;
  min: number;
  max: number;
  top: number;
  bottom: number;
}>;

const positive = (value: number) => Number.isFinite(value) && value > 0;
const displayAmplitude = (peak: number, fullScale: number) =>
  Math.sqrt(Math.max(0, peak) / fullScale);

export function isNleSchedulerTraceEnabled(): boolean {
  if (typeof window === "undefined") return false;
  const target = window as Window & {
    __h3NleSchedulerTraceEnabled?: boolean;
  };
  return (
    target.__h3NleSchedulerTraceEnabled === true ||
    new URLSearchParams(window.location.search).get("h3NleSchedulerTrace") ===
      "1"
  );
}

function traceWaveformPaintCall(
  context: CanvasRenderingContext2D,
  input: WaveformPaintInput | null,
  outcome: string,
  facts: Readonly<Record<string, unknown>> = {},
): void {
  if (!isNleSchedulerTraceEnabled()) return;
  const target = window as Window & {
    __h3NleSchedulerTraceEnabled?: boolean;
    __h3NleWaveformCallTrace?: Array<Readonly<Record<string, unknown>>>;
  };
  const bounds = context.canvas.getBoundingClientRect();
  const trace = (target.__h3NleWaveformCallTrace ??= []);
  trace.push({
    event: "waveform.paint.call",
    outcome,
    monotonicMs: Number(performance.now().toFixed(3)),
    locationSearch: window.location.search,
    diagnosticFlag: target.__h3NleSchedulerTraceEnabled === true,
    assetId: input?.asset.assetId ?? null,
    clip: input?.clip ?? null,
    inputBounds: input?.bounds ?? null,
    visible: input?.visible ?? null,
    canvasMarker: context.canvas.getAttribute("data-h3-nle-canvas"),
    canvas: {
      width: context.canvas.width,
      height: context.canvas.height,
      cssWidth: bounds.width,
      cssHeight: bounds.height,
      viewportLeft: bounds.left,
      viewportTop: bounds.top,
    },
    ...facts,
  });
  if (trace.length > 128) trace.splice(0, trace.length - 128);
}

/**
 * IMPORTANT (B-M2564-03): the source tick at an x inside the clip, by the core's audio rule
 * (`semantic_conformance_expect._audio_windows`): the start landmark's pts plus the elapsed output
 * time converted into the source's time base. `sourceStartFrame` is a source identity and the
 * position is output time; walking the landmark table by output frames, as this painter once
 * did, is only right when the source runs at the timeline's rate.
 */
function sourceTickAt(input: WaveformPaintInput, x: number): number {
  const { asset, bounds, clip, outputFrameRate } = input;
  const position = Math.min(
    clip.durationFrames,
    Math.max(0, ((x - bounds.x) / bounds.width) * clip.durationFrames),
  );
  const start = asset.landmarks[clip.sourceStartFrame];
  if (start?.frameIndex !== clip.sourceStartFrame)
    throw new Error("waveform landmark is invalid");
  return (
    start.pts +
    (position * outputFrameRate.den * asset.sourceTimeBase.den) /
      (outputFrameRate.num * asset.sourceTimeBase.num)
  );
}

export function planWaveformColumns(
  input: WaveformPaintInput,
): readonly WaveformPaintColumn[] {
  const { asset, bounds, clip, envelope, outputFrameRate, visible } = input;
  if (asset.embeddedAudio !== "present_bound") return [];
  if (
    asset.kind !== "video" ||
    !Number.isSafeInteger(asset.sourceSampleCount) ||
    asset.sourceSampleCount! < 1 ||
    !Number.isSafeInteger(asset.sourceFrameCount) ||
    asset.sourceFrameCount !== asset.landmarks.length ||
    !Number.isSafeInteger(clip.sourceStartFrame) ||
    !Number.isSafeInteger(clip.durationFrames) ||
    clip.sourceStartFrame < 0 ||
    clip.durationFrames < 1 ||
    // Output time past the source's end reads past its audio and draws nothing (below); only a
    // start outside the source is out of range.
    clip.sourceStartFrame >= asset.sourceFrameCount ||
    !positive(asset.sourceTimeBase.num) ||
    !positive(asset.sourceTimeBase.den) ||
    !Number.isSafeInteger(outputFrameRate?.num) ||
    !Number.isSafeInteger(outputFrameRate?.den) ||
    outputFrameRate.num < 1 ||
    outputFrameRate.den < 1 ||
    !positive(bounds.width) ||
    !positive(bounds.height) ||
    !positive(visible.width) ||
    envelope.pairCount * 2 !== envelope.pairs.length
  )
    throw new Error("waveform paint input is invalid");
  const left = Math.max(bounds.x, visible.x);
  const right = Math.min(bounds.x + bounds.width, visible.x + visible.width);
  if (right <= left) return [];
  const first = Math.floor(left);
  const last = Math.ceil(right);
  const centre = bounds.y + bounds.height / 2;
  const half = bounds.height / 2;
  const columns: WaveformPaintColumn[] = [];
  for (let pixel = first; pixel < last; pixel += 1) {
    const columnLeft = Math.max(left, pixel);
    const columnRight = Math.min(right, pixel + 1);
    if (columnRight <= columnLeft) continue;
    const startTick = sourceTickAt(input, columnLeft);
    const endTick = sourceTickAt(input, columnRight);
    const startPair = Math.max(
      0,
      Math.floor(
        (startTick * asset.sourceTimeBase.num * envelope.pairRate) /
          asset.sourceTimeBase.den,
      ),
    );
    // IMPORTANT: never clamp a post-audio column to the final bucket. That would visually
    // stretch short embedded audio across a longer video and invent waveform evidence.
    if (startPair >= envelope.pairCount) continue;
    const endPair = Math.min(
      envelope.pairCount,
      Math.max(
        startPair + 1,
        Math.ceil(
          (endTick * asset.sourceTimeBase.num * envelope.pairRate) /
            asset.sourceTimeBase.den,
        ),
      ),
    );
    let low = 127;
    let high = -128;
    for (let pair = startPair; pair < endPair; pair += 1) {
      low = Math.min(low, envelope.pairs[pair * 2]!);
      high = Math.max(high, envelope.pairs[pair * 2 + 1]!);
    }
    columns.push(
      Object.freeze({
        x: pixel + 0.5,
        min: low,
        max: high,
        // IMPORTANT: quiet but present audio must survive the 12px clip slot and the
        // filmstrip composite. Square-root display companding keeps silence at the centre,
        // preserves peak ordering and fits every signed-byte peak within the slot.
        top: centre - displayAmplitude(high, 127) * half,
        bottom: centre + displayAmplitude(-low, 128) * half,
      }),
    );
  }
  return Object.freeze(columns);
}

function traceWaveformStroke(
  context: CanvasRenderingContext2D,
  input: WaveformPaintInput,
  columns: readonly WaveformPaintColumn[],
): void {
  if (!isNleSchedulerTraceEnabled()) return;
  const target = window as Window & {
    __h3NleWaveformStrokeTrace?: Array<Readonly<Record<string, unknown>>>;
  };
  const canvas = context.canvas;
  const bounds = canvas.getBoundingClientRect();
  const scaleX = canvas.width / Math.max(1, bounds.width);
  const scaleY = canvas.height / Math.max(1, bounds.height);
  const left = Math.max(
    0,
    Math.floor(Math.min(...columns.map((column) => column.x)) * scaleX) - 2,
  );
  const right = Math.min(
    canvas.width,
    Math.ceil(Math.max(...columns.map((column) => column.x)) * scaleX) + 2,
  );
  const top = Math.max(
    0,
    Math.floor(Math.min(...columns.map((column) => column.top)) * scaleY) - 2,
  );
  const bottom = Math.min(
    canvas.height,
    Math.ceil(Math.max(...columns.map((column) => column.bottom)) * scaleY) + 2,
  );
  const readPixels = () => {
    try {
      const pixels = context.getImageData(
        left,
        top,
        Math.max(1, right - left),
        Math.max(1, bottom - top),
      ).data;
      let nonTransparent = 0;
      let cyan = 0;
      let maximumAlpha = 0;
      for (let offset = 0; offset < pixels.length; offset += 4) {
        const alpha = pixels[offset + 3]!;
        maximumAlpha = Math.max(maximumAlpha, alpha);
        if (alpha > 0) nonTransparent += 1;
        if (
          alpha > 0 &&
          pixels[offset + 2]! >= 180 &&
          pixels[offset + 1]! >= 140 &&
          pixels[offset + 2]! > pixels[offset]! + 30
        )
          cyan += 1;
      }
      return {
        status: "available",
        nonTransparent,
        cyan,
        maximumAlpha,
      };
    } catch (error) {
      return {
        status: error instanceof Error ? error.name : "unknown_error",
        nonTransparent: null,
        cyan: null,
        maximumAlpha: null,
      };
    }
  };
  const transform = context.getTransform();
  const before = readPixels();
  context.stroke();
  const after = readPixels();
  const trace = (target.__h3NleWaveformStrokeTrace ??= []);
  trace.push({
    event: "waveform.stroke.result",
    canvasMarker: canvas.getAttribute("data-h3-nle-canvas"),
    canvas: {
      width: canvas.width,
      height: canvas.height,
      cssWidth: bounds.width,
      cssHeight: bounds.height,
      viewportLeft: bounds.left,
      viewportTop: bounds.top,
    },
    rasterBounds: { left, top, right, bottom },
    inputBounds: input.bounds,
    visible: input.visible,
    strokeStyle: String(context.strokeStyle),
    lineWidth: context.lineWidth,
    globalAlpha: context.globalAlpha,
    globalCompositeOperation: context.globalCompositeOperation,
    filter: context.filter,
    lineDash: context.getLineDash(),
    transform: {
      a: transform.a,
      b: transform.b,
      c: transform.c,
      d: transform.d,
      e: transform.e,
      f: transform.f,
    },
    columnCount: columns.length,
    nonDegenerateColumnCount: columns.filter(
      (column) => column.bottom > column.top,
    ).length,
    firstColumn: columns[0] ?? null,
    lastColumn: columns.at(-1) ?? null,
    before,
    after,
  });
  if (trace.length > 48) trace.splice(0, trace.length - 48);
}

/** The stroke where no role colour is supplied (a page without the overlay's role tokens). */
export const WAVEFORM_DEFAULT_STROKE = "rgba(125, 211, 252, .82)";

export function paintWaveform(
  context: CanvasRenderingContext2D,
  input: WaveformPaintInput | null,
  color: string = WAVEFORM_DEFAULT_STROKE,
): void {
  traceWaveformPaintCall(context, input, "enter");
  if (input === null) {
    traceWaveformPaintCall(context, input, "skip_null_input");
    return;
  }
  let columns: readonly WaveformPaintColumn[];
  try {
    columns = planWaveformColumns(input);
  } catch (error) {
    traceWaveformPaintCall(context, input, "plan_error", {
      errorType: error instanceof Error ? error.name : "unknown_error",
      errorMessage: error instanceof Error ? error.message : "unknown_error",
    });
    throw error;
  }
  traceWaveformPaintCall(context, input, "planned", {
    columnCount: columns.length,
    nonDegenerateColumnCount: columns.filter(
      (column) => column.bottom > column.top,
    ).length,
    firstColumn: columns[0] ?? null,
    lastColumn: columns.at(-1) ?? null,
  });
  if (columns.length === 0) {
    traceWaveformPaintCall(context, input, "skip_empty_columns");
    return;
  }
  context.strokeStyle = color;
  context.lineWidth = 1;
  context.beginPath();
  for (const column of columns) {
    context.moveTo(column.x, column.top);
    context.lineTo(column.x, column.bottom);
  }
  traceWaveformPaintCall(context, input, "path_built", {
    columnCount: columns.length,
    pointCount: columns.length * 2,
    strokeStyle: String(context.strokeStyle),
    lineWidth: context.lineWidth,
  });
  try {
    if (isNleSchedulerTraceEnabled())
      traceWaveformStroke(context, input, columns);
    else context.stroke();
    traceWaveformPaintCall(context, input, "stroke_complete", {
      columnCount: columns.length,
      strokeStyle: String(context.strokeStyle),
    });
  } catch (error) {
    traceWaveformPaintCall(context, input, "stroke_error", {
      columnCount: columns.length,
      errorType: error instanceof Error ? error.name : "unknown_error",
      errorMessage: error instanceof Error ? error.message : "unknown_error",
    });
    throw error;
  }
}

export function paintNleWaveforms(
  context: CanvasRenderingContext2D,
  inputs: readonly WaveformPaintInput[],
  color?: string,
): void {
  for (const input of inputs) paintWaveform(context, input, color);
}
