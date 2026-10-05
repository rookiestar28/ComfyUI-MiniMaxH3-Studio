import {
  paintFilmstrip,
  type FilmstripPaintInput,
} from "./nleFilmstripPainter";
import {
  isNleSchedulerTraceEnabled,
  paintNleWaveforms,
  planWaveformColumns,
  type WaveformPaintInput,
} from "./nleWaveformPainter";

export type TimelineDecorationScene = Readonly<{
  width: number;
  height: number;
  filmstrips: readonly FilmstripPaintInput[];
  waveforms: readonly WaveformPaintInput[];
  // M25-61: no `ticks`. Periodic ruler ticks painted through every track were the owner's first
  // complaint (2026-09-26); ticks live in the ruler only. The snap guide below stays, because it
  // is a transient drag aid, not a periodic line.
  // M25-62: no `clipOutlines`. The clip border and the selection ring are DOM drawn above this
  // canvas in role colours; a raster outline in a fixed colour doubled them.
  /** `--h3-nle-waveform`, resolved where the overlay's role tokens are mounted. */
  waveformColor?: string;
  marquee: Readonly<{
    x: number;
    y: number;
    width: number;
    height: number;
  }> | null;
  snapX: number | null;
  ghost: Readonly<{
    x: number;
    y: number;
    width: number;
    height: number;
    admitted: boolean;
  }> | null;
}>;

export function paintTimelineDecorations(
  context: CanvasRenderingContext2D,
  scene: TimelineDecorationScene,
): void {
  context.clearRect(0, 0, scene.width, scene.height);
  for (const filmstrip of scene.filmstrips) paintFilmstrip(context, filmstrip);
  if (isNleSchedulerTraceEnabled()) {
    const target = window as Window & {
      __h3NleWaveformPaintTrace?: Array<Readonly<Record<string, unknown>>>;
    };
    const trace = (target.__h3NleWaveformPaintTrace ??= []);
    const inputs = scene.waveforms.map((input, index) => {
      try {
        const columns = planWaveformColumns(input);
        let nonZeroPairs = 0;
        for (let pair = 0; pair < input.envelope.pairCount; pair += 1) {
          if (
            input.envelope.pairs[pair * 2] !== 0 ||
            input.envelope.pairs[pair * 2 + 1] !== 0
          )
            nonZeroPairs += 1;
        }
        return {
          slot: index + 1,
          assetId: input.asset.assetId,
          bounds: input.bounds,
          visible: input.visible,
          sourceStartFrame: input.clip.sourceStartFrame,
          durationFrames: input.clip.durationFrames,
          pairCount: input.envelope.pairCount,
          pairLength: input.envelope.pairs.length,
          nonZeroPairs,
          columnCount: columns.length,
          firstColumn: columns[0] ?? null,
          lastColumn: columns.at(-1) ?? null,
        };
      } catch (error) {
        return {
          slot: index + 1,
          assetId: input.asset.assetId,
          errorType: error instanceof Error ? error.name : "unknown_error",
        };
      }
    });
    trace.push({
      event: "waveform.paint.inputs",
      canvas: { width: scene.width, height: scene.height },
      inputCount: scene.waveforms.length,
      inputs,
    });
    if (trace.length > 128) trace.splice(0, trace.length - 128);
  }
  paintNleWaveforms(context, scene.waveforms, scene.waveformColor);
  context.lineWidth = 1;
  if (scene.marquee !== null) {
    context.fillStyle = "rgba(56, 189, 248, .12)";
    context.strokeStyle = "#38bdf8";
    context.fillRect(
      scene.marquee.x,
      scene.marquee.y,
      scene.marquee.width,
      scene.marquee.height,
    );
    context.strokeRect(
      scene.marquee.x,
      scene.marquee.y,
      scene.marquee.width,
      scene.marquee.height,
    );
  }
  if (scene.snapX !== null) {
    context.strokeStyle = "#facc15";
    context.beginPath();
    context.moveTo(scene.snapX, 0);
    context.lineTo(scene.snapX, scene.height);
    context.stroke();
  }
  if (scene.ghost !== null) {
    context.setLineDash(scene.ghost.admitted ? [6, 4] : [3, 3]);
    context.strokeStyle = scene.ghost.admitted ? "#4ade80" : "#fb7185";
    context.lineWidth = 2;
    context.strokeRect(
      scene.ghost.x,
      scene.ghost.y,
      scene.ghost.width,
      scene.ghost.height,
    );
    context.setLineDash([]);
  }
}

export function createTimelineRasterScheduler(
  canvas: HTMLCanvasElement,
  read: () => TimelineDecorationScene,
): Readonly<{ request(): void; dispose(): void }> {
  let frame: number | null = null;
  let disposed = false;
  const draw = () => {
    frame = null;
    if (disposed) return;
    const scene = read();
    const ratio = Math.min(2, Math.max(1, window.devicePixelRatio || 1));
    const width = Math.max(1, Math.ceil(scene.width * ratio));
    const height = Math.max(1, Math.ceil(scene.height * ratio));
    if (canvas.width !== width) canvas.width = width;
    if (canvas.height !== height) canvas.height = height;
    canvas.style.width = `${scene.width}px`;
    canvas.style.height = `${scene.height}px`;
    // jsdom deliberately has no raster implementation; pure painter tests provide a recorded
    // context, while component tests verify lifecycle without installing a native canvas addon.
    if (window.navigator.userAgent.includes("jsdom")) return;
    const context = canvas.getContext("2d");
    if (context === null) return;
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    paintTimelineDecorations(context, scene);
  };
  return Object.freeze({
    request() {
      if (!disposed && frame === null) frame = requestAnimationFrame(draw);
    },
    dispose() {
      disposed = true;
      if (frame !== null) cancelAnimationFrame(frame);
      frame = null;
      canvas.width = 1;
      canvas.height = 1;
    },
  });
}
