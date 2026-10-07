// M25-20 F1 corrective, browser half: the 151 `render_and_browser` corpus rows
// (`comfyui_h3_context/core/semantic_conformance_cases.py`'s `build_recipes()`). Each row's exact
// composition wire is produced by `scripts/nle_semantic_browser_wires.py` -- a read-only caller of
// `semantic_conformance_drive.initial_state`/`resolve_payload`/`transact`, the SAME primitives
// `scripts/nle_semantic_report.py`'s own `_composition` resolves a row's judged composition
// through, never a hand-written table -- and this journey presents every one of them in the real
// integrated shell, in turn, on one page load: `?compositionSource=1` (see `nleShell.tsx`) makes
// the shell's own `read_timeline_history` action always re-bootstrap, and
// `openCompositionSeriesShell`'s Playwright route decides what each bootstrap resolves to, exactly
// the way the accepted `openIntegratedShell` harness already decides it for every other case. This
// is a real, repeatable gesture (the first explicit open, then the Clip editor summary's own
// "Refresh workspace" affordance), not 151 page reloads and not a synthesized state injection.
//
// The picture each composition actually shows is the real, spec-compliant test pattern
// (`comfyui_h3_context.core.semantic_conformance_media`): `scripts/nle_semantic_browser_media.py`
// built the three source files once (imported the render stage's own `_paint_frame`/
// `_build_source_video`, never a second re-implementation of the paint routine) and
// `nleWorkspaceMedia.ts` serves them by asset id. Every landmark this journey reports is read back
// from the monitor canvas's own backing pixels and the real `<video>` element's own `currentTime`
// (`window.__nleMediaDebug`, explicit test instrumentation, additive) -- never copied from the wire
// this row was given. The real monitor transport (`NleMonitor.tsx`'s `transport.seek` range input,
// a genuine product control, not test-only) drives the canvas to every output frame the row's
// prescription names (`scripts/nle_semantic_browser_wires.py`: the same sample points, identity
// cells and geometry targets the render stage hands the independent extractor), and at each one
// `nleSemanticCanvasExtraction.ts` measures exactly what the prescription asks for there -- a port
// of the Python extractor's own rules. The wire decides *where* to look; nothing in it decides
// what is found. A row the product presents scaled down (`preview_scale < 1`) is presented and
// its fingerprint confirmed, and prescribes nothing -- see
// `semantic_conformance_expect.BROWSER_OBSERVES_ONLY_AN_UNSCALED_PREVIEW`.
//
// Not part of `test:e2e:ci`: presenting all 151 compositions takes several minutes, and
// `tests/TEST_SOP.md` section 3.6 places the semantic-conformance qualification run's per-row
// evidence collection outside the ordinary Full Gate for exactly that resource reason. Run it
// directly (`playwright test journeys/nleSemanticRenderAndBrowser.spec.ts`) or through
// `nleSemanticBrowserStageGatherer.mjs`, which includes it.

import { expect, test, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import {
  existsSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import {
  playheadFrame,
  playheadSlider,
  seekPlayhead,
} from "../helpers/nleTimeline";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  alphasForTarget,
  observePrescribedFrame,
  type MediaConstants,
  type Prescription,
} from "../helpers/nleSemanticCanvasExtraction";
import {
  recordShellObservation,
  type AlphaEntry,
  type GeometryEntry,
  type PatchEntry,
  type SourceMappingEntry,
} from "../helpers/nleSemanticShellEvidence";
import {
  openCompositionSeriesShell,
  shellLastPresentation,
  shellSnapshot,
  waitForPresentedFingerprint,
  waitForPresentedFrame,
} from "../helpers/nleShell";

test.use({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });

/**
 * How each corpus base is presented, read from the wires rather than restated here.
 *
 * AC45-05's high-resolution rows are observed at the shipped preview's own cap, a 1280 x 720
 * backing, and a backing is CSS pixels times the device pixel ratio. At ratio 1 that needs a
 * picture area 1280 CSS pixels wide, which the monitor pane of a 1440 x 900 viewport does not have
 * and cannot be given without a layout no user starts from; at ratio 2 it needs 640 x 360, which
 * the same pane has comfortably. So the ratio is the thing that changes, the viewport is not, and
 * the row records both plus the backing it actually reached. A row whose backing is not the size
 * its base declares is failed, never accepted at whatever size it got -- a downscaled
 * high-resolution row is precisely what the criterion excludes.
 *
 * GUARD: these numbers arrive in `prescription.presentation`, from the one table in
 * `semantic_conformance_cases.BASE_PRESENTATION`. A copy here would not fail loudly when the two
 * disagreed: the Python side decides from its numbers whether a row's landmarks are observable at
 * all -- a row it thinks is scaled down is prescribed nothing and compared on its artifact alone --
 * so a drifted copy would present rows at a size nobody wrote expectations for, or silently drop
 * the browser side of the rows that exist to be observed at full size.
 */
type Presentation = Readonly<{
  deviceScaleFactor: number;
  backing: Readonly<{ width: number; height: number }> | null;
}>;

function presentationsFrom(
  rows: readonly WireRow[],
): ReadonlyMap<string, Presentation> {
  const found = new Map<string, Presentation>();
  for (const row of rows) {
    if (row.status !== "READY") continue;
    const declared = row.prescription.presentation;
    const presentation: Presentation = {
      deviceScaleFactor: declared.device_pixel_ratio,
      backing:
        declared.backing_width > 0
          ? { width: declared.backing_width, height: declared.backing_height }
          : null,
    };
    const existing = found.get(row.base);
    if (existing === undefined) {
      found.set(row.base, presentation);
      continue;
    }
    if (JSON.stringify(existing) !== JSON.stringify(presentation))
      throw new Error(
        `the wires present base ${row.base} two different ways; the stage cannot choose one`,
      );
  }
  return found;
}

let basePresentation: ReadonlyMap<string, Presentation> = new Map();

function presentationFor(base: string): Presentation {
  const row = basePresentation.get(base);
  if (row === undefined)
    throw new Error(
      `the wires name a composition base this stage cannot present: ${base}`,
    );
  return row;
}
// The real budget is set inside the test from the rows it is about to present (see
// `sweepBudgetMs`); this file-level figure only covers loading the wires.
test.setTimeout(1_800_000);

/** One row's presentation budget: a boot and a settle, plus a seek, a settled capture and the
 * measurement per prescribed frame -- a whole-duration row names every one of its 48 frames for
 * the identity read. */
const ROW_BUDGET_MS = 60_000;
const FRAME_BUDGET_MS = 4_000;
/**
 * The backing the per-frame budget above was measured against: the accepted base's own 320 x 180.
 *
 * GUARD (M25-45): a row's per-frame cost is not a constant, because presenting and reading back a
 * frame is work over the whole backing. Measured on this machine, one 1280 x 720 row took 1,098 s
 * for 48 frames -- about 23 s a frame against 4 s at 320 x 180 -- so the cost grows with the
 * picture's edge rather than its area (16x the pixels, 5.75x the time). The allowance below scales
 * by the square root of the area ratio and keeps a 2x margin over that, which leaves the accepted
 * rows at exactly the 4 s they had. Never replace this with a flat figure: a budget that does not
 * move with the backing either strangles the large rows or stops bounding the small ones (B-59).
 */
const BUDGET_BASE_PIXELS = 320 * 180;
/**
 * How long a row may take to deliver its *first* paint after its composition is selected.
 *
 * GUARD (B-M2545-28): this is not a per-frame budget and must not be folded into one. The first
 * paint is the only one that pays for the whole composition's sources coming up -- every clip's
 * lease, decode and first exact seek -- while every later frame of the same row reuses those same
 * warm sources and keeps the (much smaller) per-frame budget above.
 *
 * The figure is measured, not guessed, and it is not a workaround for anything. It was raised to
 * 120,000 ms once, for exactly as long as it took to establish whether the two failing
 * `high_resolution.layer.*` rows were slow or stopped -- a bound that fires first cannot tell those
 * apart. They were stopped: B-M2545-28, a resize repaint refused mid-move and published as a source
 * failure, which latched the session `blocked` so that nothing presented and nothing retried. That
 * defect is repaired in the product, and the bound is restored here rather than left wide: a bound
 * kept generous to accommodate a known defect is that defect's hiding place.
 *
 * 30,000 ms against measurement: a healthy 1280 x 720 row settles all forty-eight of its frames in
 * about 2,600 ms once its sources are warm, and each row records its own
 * `observationFirstPaintMs`, so the margin over what a first paint actually costs is visible in
 * every run's evidence rather than asserted here. Widen it only against a recorded first-paint
 * measurement that needs the room.
 */
const READINESS_BUDGET_MS = 30_000;
/** The config's own `actionTimeout`, mirrored so an action's bound is never *below* it. */
const ACTION_BOUND_MS = 15_000;
/**
 * How much of a row's per-frame budget one action may take.
 *
 * GUARD (B-M2545-15): the mean is not the bound. A 1280 x 720 row measured 1,117 s for 48 frames,
 * about twenty-three seconds a frame, and still had a single seek that had not become actionable
 * after thirty-two -- the distribution has a tail, and a bound set at the mean fails a healthy row
 * on it. Two frames' budget is the allowance; the row's own `withTimeout` is what actually bounds
 * the row, so widening this costs nothing but the seconds a genuinely dead control takes to fail.
 */
const ACTION_FRAME_BUDGETS = 2;
const BUDGET_BACKING_MARGIN = 2;

function frameBudgetMs(row: ReadyRow): number {
  const backing = row.prescription.presentation;
  const pixels = backing.backing_width * backing.backing_height;
  if (pixels <= BUDGET_BASE_PIXELS) return FRAME_BUDGET_MS;
  return Math.ceil(
    FRAME_BUDGET_MS *
      Math.sqrt(pixels / BUDGET_BASE_PIXELS) *
      BUDGET_BACKING_MARGIN,
  );
}

function rowBudgetMs(row: ReadyRow): number {
  return ROW_BUDGET_MS + row.prescription.frames.length * frameBudgetMs(row);
}

/** The whole sweep's budget is the sum of its rows' budgets plus one fresh-page recovery per row.
 *
 * GUARD: never a fixed figure. A fixed two-hour budget timed out a 150-row sweep at 2 h 03 min
 * (about 6,700 prescribed frames through the real transport, beside a render sweep), and
 * Playwright then recorded every row of the test as not passed -- the whole sweep's evidence was
 * lost to a number that had nothing to do with what the rows needed (B-59). */
function sweepBudgetMs(rows: readonly WireRow[]): number {
  return rows.reduce(
    (total, row) =>
      total + (row.status === "BLOCKED" ? 0 : rowBudgetMs(row) + ROW_BUDGET_MS),
    600_000,
  );
}

const OVERLAY = '[data-h3-nle-surface="overlay_v1"]';
const LAUNCHER = '[data-h3-nle-entry="open"]';
const SUMMARY = { name: "Clip editor project", exact: true } as const;
// IMPORTANT (B-M2557-06): the NLE dialog also contains asset-bin thumbnail canvases. Semantic
// pixels must come from the compositor canvas inside the monitor's measured picture area.
const MONITOR_CANVAS =
  '[data-h3-nle-surface="overlay_v1"] .h3-nle-monitor .h3-nle-picture canvas[data-h3-nle-canvas="composition"]';
const CLOSE = '[data-h3-nle-action="close"]';
const NAVIGATION = { name: "H3 Context pages" } as const;
const ROOT = fileURLToPath(new URL("../../../../", import.meta.url));
const MEDIA_DIR = join(ROOT, "frontend/tests/fixtures/m25_20_semantic_media");

type ReadyRow = Readonly<{
  case_id: string;
  /** Which composition base the row is a case of; see `BASE_PRESENTATION`. */
  base: string;
  status: "READY";
  wire: Readonly<Record<string, unknown>>;
  prescription: Prescription;
}>;
type BlockedRow = Readonly<{
  case_id: string;
  base: string;
  status: "BLOCKED";
  blocked_code: string;
}>;
type WireRow = ReadyRow | BlockedRow;

/**
 * Runs the read-only Python wire generator and returns every row's resolved composition (or its
 * blocked code). Never a hand-written table: `build_recipes()` is the sole source of the 151 rows.
 *
 * A development aid only: `H3_CONTEXT_NLE_SEMANTIC_WIRES` names a file the generated wires are
 * cached in, so a narrowed diagnostic run costs seconds instead of the ten-odd minutes the
 * generator takes to state 154 rows' prescriptions. It is deliberately honoured **only when
 * `H3_CONTEXT_NLE_SEMANTIC_ROWS` is also set**, so it cannot be reached by a full-corpus run at
 * all: a sweep that stands as evidence always generates its wires afresh, and there is no
 * combination of environment variables that lets a stale or hand-edited file answer for one. The
 * gatherer sets neither variable.
 */
function loadWireRows(narrowed: boolean): readonly WireRow[] {
  const python = join(
    ROOT,
    ".venv",
    process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
  );
  const cachePath = narrowed
    ? (process.env.H3_CONTEXT_NLE_SEMANTIC_WIRES ?? "").trim()
    : "";
  if (cachePath !== "" && existsSync(cachePath))
    return (JSON.parse(readFileSync(cachePath, "utf8")) as { rows: WireRow[] })
      .rows;
  const scratchDir = mkdtempSync(
    join(tmpdir(), "nle-render-and-browser-wires-"),
  );
  const outputPath = join(scratchDir, "wires.json");
  try {
    execFileSync(
      python,
      [
        join(ROOT, "scripts/nle_semantic_browser_wires.py"),
        "--output",
        outputPath,
      ],
      // The generator states every prescription (one flat-composite and edge search per sample
      // point, for 150 rows): about a minute on its own, longer beside a render sweep.
      { encoding: "utf8", timeout: 600_000, maxBuffer: 16_777_216 },
    );
    const generated = readFileSync(outputPath, "utf8");
    if (cachePath !== "") writeFileSync(cachePath, generated);
    const document = JSON.parse(generated) as {
      rows: WireRow[];
    };
    return document.rows;
  } finally {
    rmSync(scratchDir, { recursive: true, force: true });
  }
}

/** Read once: the labels/colours/frame-identity encoding, from the module that owns them. */
function loadMediaConstants(): MediaConstants {
  return JSON.parse(
    readFileSync(join(MEDIA_DIR, "media_constants.json"), "utf8"),
  ) as MediaConstants;
}

/**
 * A path/URL-free rendering of an unknown thrown value: `evidence.ts`'s `FORBIDDEN_EVIDENCE`
 * refuses to attach anything that looks like an absolute Windows path or a non-loopback URL, and a
 * Node `execFileSync`/`ENOENT`-style failure message routinely embeds the full resolved executable
 * path. A row that hits this must still record why it failed, so the full diagnostic goes to the
 * console (never privacy-filtered) and only this sanitized form goes into the evidence attachment.
 *
 * GUARD: collapse newlines/tabs/carriage returns to spaces *before* this string is ever handed to
 * `JSON.stringify` for an evidence attachment. `evidence.ts`'s own `FORBIDDEN_EVIDENCE` regex looks
 * for a drive-letter path as `[A-Z]:\` (single letter, colon, one backslash); `JSON.stringify`
 * escapes a real newline inside a string value as the two literal characters `\` `n`, so any
 * message ending a word right before a line break -- e.g. Playwright's own multi-line
 * `expect.poll` timeout text, "...Call Log:\n- Timeout...": the "g" of "Log", the colon, then the
 * backslash that opens the escaped newline -- reads back as "g:\" and trips the same regex as a
 * genuine path, with no path anywhere in the message. This was found and reproduced directly (a
 * real `expect.poll(...).toBeGreaterThan(1)` timeout on `transform.position_x_bp.lower` failed to
 * even record its own failure for exactly this reason). Stripping line breaks here removes every
 * backslash that JSON escaping would otherwise manufacture from this string.
 */
function sanitizeErrorMessage(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error);
  return raw
    .replace(/[A-Za-z]:[\\/][^\s"']*/g, "<path>")
    .replace(/https?:\/\/(?!127\.0\.0\.1)[^\s"']*/gi, "<url>")
    .replace(
      /credential|cookie|prompt_text|media_bytes|signed_url/gi,
      "<redacted>",
    )
    .replace(/[\n\r\t]+/g, " ")
    .slice(0, 200);
}

async function toClipEditor(page: Page): Promise<void> {
  await page
    .getByRole("navigation", NAVIGATION)
    .getByRole("button", { name: "Production" })
    .click();
  await page.getByRole("tab", { name: "Clip editor" }).click();
}

type CanvasSample = Readonly<{
  width: number;
  height: number;
  opaque: number;
  distinct: number;
  total: number;
  centerPatchRgb: string;
  centerPatchEdgeDistance: number;
  presentationGeometry: Readonly<Record<string, number | string | null>>;
}>;

function measureCanvas(page: Page): Promise<CanvasSample | null> {
  return page
    .locator(MONITOR_CANVAS)
    .first()
    .evaluate((element) => {
      const target = element as HTMLCanvasElement;
      if (target.width === 0 || target.height === 0) return null;
      const context = target.getContext("2d", { willReadFrequently: true });
      if (context === null) return null;
      const image = context.getImageData(0, 0, target.width, target.height);
      let opaque = 0;
      const distinct = new Set<string>();
      for (let index = 0; index < image.data.length; index += 4) {
        if (image.data[index + 3] > 0) opaque += 1;
        if (distinct.size < 64) {
          distinct.add(
            `${image.data[index]},${image.data[index + 1]},${image.data[index + 2]}`,
          );
        }
      }
      const patchX = Math.floor(target.width / 2);
      const patchY = Math.floor(target.height / 2);
      const patchIndex = (patchY * target.width + patchX) * 4;
      const rect = (element: Element | null): string | null => {
        if (element === null) return null;
        const box = element.getBoundingClientRect();
        return `${box.width}x${box.height}@${box.left},${box.top}`;
      };
      const picture = target.closest(".h3-nle-picture");
      const monitor = target.closest(".h3-nle-monitor");
      const monitorArea = target.closest('[data-h3-nle-area="monitor"]');
      const shell = target.closest("[data-h3-nle-shell]");
      const stage = target.closest(".h3-nle-stage");
      const dialog = target.closest(".h3-nle-dialog");
      const root = target.closest("[data-h3-nle-root]");
      const visual = window.visualViewport;
      const pictureStyle = picture === null ? null : getComputedStyle(picture);
      const canvasStyle = getComputedStyle(target);
      const dialogStyle = dialog === null ? null : getComputedStyle(dialog);
      return {
        width: target.width,
        height: target.height,
        opaque,
        distinct: distinct.size,
        total: image.data.length / 4,
        centerPatchRgb: `${image.data[patchIndex]},${image.data[patchIndex + 1]},${image.data[patchIndex + 2]}`,
        centerPatchEdgeDistance: Math.min(
          patchX,
          patchY,
          target.width - patchX,
          target.height - patchY,
        ),
        presentationGeometry: {
          devicePixelRatio: window.devicePixelRatio,
          innerViewport: `${window.innerWidth}x${window.innerHeight}`,
          visualViewport:
            visual === undefined || visual === null
              ? null
              : `${visual.width}x${visual.height}@${visual.scale}`,
          rootRect: rect(root),
          dialogRect: rect(dialog),
          dialogInlineSize:
            dialog instanceof HTMLElement ? dialog.style.width : null,
          dialogComputedSize:
            dialogStyle === null
              ? null
              : `${dialogStyle.width}x${dialogStyle.height}`,
          stageRect: rect(stage),
          shellRect: rect(shell),
          monitorAreaRect: rect(monitorArea),
          monitorRect: rect(monitor),
          pictureRect: rect(picture),
          pictureDeclaredOutput:
            picture instanceof HTMLElement
              ? (picture.dataset.h3NlePicture ?? null)
              : null,
          pictureComputedDisplay: pictureStyle?.display ?? null,
          pictureComputedBoxSizing: pictureStyle?.boxSizing ?? null,
          canvasRect: rect(target),
          canvasBacking: `${target.width}x${target.height}`,
          canvasInlineSize: `${target.style.width}x${target.style.height}`,
          canvasComputedSize: `${canvasStyle.width}x${canvasStyle.height}`,
        },
      };
    });
}

/**
 * The full backing-store `ImageData` as a plain, structured-cloneable object.
 *
 * D45-03: `readbackMs` is the in-page `getImageData` alone and `serializeMs` the `Array.from`
 * that makes it cloneable, so the caller can price the readback, the serialization and the
 * cross-boundary transfer separately instead of attributing the whole cost to the compositor. A
 * 1280 x 720 read carries 3,686,400 numbers, and the recorded ~23 s per frame has never been
 * split; it is measured here rather than argued about.
 */
async function captureImage(page: Page): Promise<{
  data: Uint8ClampedArray;
  width: number;
  height: number;
  readbackMs: number;
  serializeMs: number;
} | null> {
  const captured = await page
    .locator(MONITOR_CANVAS)
    .first()
    .evaluate((element) => {
      const target = element as HTMLCanvasElement;
      if (target.width === 0 || target.height === 0) return null;
      const context = target.getContext("2d", { willReadFrequently: true });
      if (context === null) return null;
      const readbackStarted = performance.now();
      const image = context.getImageData(0, 0, target.width, target.height);
      const readbackMs = performance.now() - readbackStarted;
      const serializeStarted = performance.now();
      // GUARD (D45-03): the raster crosses as base64 of its own bytes, never as a JS number array.
      // Measured on this lane, a 1280 x 720 row spent 303,129 ms of its ~308 s of work handing
      // 176,947,200 numbers across the boundary -- 98% of the row -- while the presentation it
      // exists to measure took 2,723 ms and the extractor 47 ms. The cost was linear in the number
      // of values transferred and in nothing else. This is the same bytes, losslessly: one byte
      // per channel in, one byte per channel out, and the extractor already accepts the
      // `Uint8ClampedArray` the caller rebuilds. Do not "simplify" this back to `Array.from`.
      let binary = "";
      const CHUNK = 0x8000;
      for (let index = 0; index < image.data.length; index += CHUNK)
        binary += String.fromCharCode(
          ...image.data.subarray(index, index + CHUNK),
        );
      return {
        base64: btoa(binary),
        width: target.width,
        height: target.height,
        readbackMs,
        serializeMs: performance.now() - serializeStarted,
      };
    });
  if (captured === null) return null;
  return {
    data: new Uint8ClampedArray(Buffer.from(captured.base64, "base64")),
    width: captured.width,
    height: captured.height,
    readbackMs: captured.readbackMs,
    serializeMs: captured.serializeMs,
  };
}

/**
 * Issue the real seek gesture for one output frame and report the value the control now holds.
 *
 * GUARD (D45-02): the returned number is the frame the transport was ASKED for, never evidence
 * that it was presented. `NleMonitor` deliberately shows `requestedFrame` while a seek is still
 * settling, so this value equals the target the instant the fill lands. The previous version
 * looped until two consecutive reads of this input were equal and treated that as "settled",
 * which was therefore satisfied immediately and proved nothing at all. Presentation is established
 * by `waitForPresentedFrame` against the compositor's own receipt; do not put a settle loop back
 * here.
 *
 * GUARD (M25-45): the fill carries its own bound, and that bound is one frame's work on the
 * backing being presented -- not the config's 15 s `actionTimeout`. That 15 s exists to fail a
 * control that is *permanently* disabled in seconds instead of hanging the sweep, and it is right
 * for a 320 x 180 row where a frame costs about four seconds. At 1280 x 720 a frame costs about
 * twenty-three, so the same bound fires while the seek input is legitimately busy with the frame
 * before this one, and a healthy row is reported as a wedged control. A bound shorter than the
 * work it is waiting on cannot tell "busy" from "broken".
 */
async function seekTransport(
  page: Page,
  frame: number,
  budgetMs: number,
): Promise<number> {
  const slider = playheadSlider(page);
  await slider.waitFor({ state: "visible", timeout: budgetMs });
  await seekPlayhead(page, slider, frame);
  return playheadFrame(slider);
}

/**
 * Waits until the transport has no request of its own outstanding, before a row's frame walk
 * starts.
 *
 * GUARD (B-M2545-30): a row's first delivered paint is not the moment its measurement may begin.
 * Every row after the first enters a monitor that restores the retained playhead of the row before
 * it (M25-21: a remount comes back paused at the user's position), and that restore seek is issued
 * by the product and settles asynchronously. So the sequence at row entry is: `open` paints frame
 * 0, the identity readiness above is satisfied by that paint, and the restore seek to the previous
 * row's last frame is still in flight. A frame walk begun there has its first seek superseded by
 * the restore -- the sample is taken at the restored frame, or no paint for the requested frame
 * ever arrives -- and since every row's walk starts at frame 0 and every predecessor ends at its
 * own last frame, the casualty is always frame 0 of a row that is otherwise entirely healthy.
 * Measured: eight of the 154 rows lost exactly their frame 0 that way, each having measured all
 * forty-seven other frames.
 *
 * The condition is the product's own projection rather than a quiet period: `NleMonitor`
 * deliberately shows `requestedFrame` on the seek control while a seek settles and the presented
 * frame once it has (M25-21 B3-D10), so "requested equals presented" is exactly "nothing is in
 * flight". Do not replace this with "no new paint for N ms" -- the gap between `open`'s paint and
 * the restore's paint is a source acquisition wide, so any N small enough to be affordable is also
 * small enough to fall inside it, and the check would pass while the restore was still coming.
 */
async function waitForQuiescentTransport(
  page: Page,
  fingerprint: string,
  budgetMs: number,
): Promise<
  Readonly<{
    settled: boolean;
    waitedMs: number;
    polls: number;
    requested: number | null;
    presented: number | null;
    identity: string | null;
  }>
> {
  const started = Date.now();
  let polls = 0;
  let requested: number | null = null;
  let presented: number | null = null;
  let identity: string | null = null;
  while (Date.now() - started < budgetMs) {
    polls += 1;
    requested = await playheadFrame(playheadSlider(page));
    const delivered = await shellLastPresentation(page);
    presented = delivered?.frame ?? null;
    identity = delivered?.publicFingerprint ?? null;
    if (
      identity === fingerprint &&
      presented !== null &&
      requested === presented
    )
      return {
        settled: true,
        waitedMs: Date.now() - started,
        polls,
        requested,
        presented,
        identity,
      };
    await page.waitForTimeout(50);
  }
  return {
    settled: false,
    waitedMs: Date.now() - started,
    polls,
    requested,
    presented,
    identity,
  };
}

async function readCurrentTime(
  page: Page,
  clipId: string,
): Promise<number | null> {
  return page.evaluate((id) => {
    const element = window.__nleMediaDebug?.videoElementsByClip.get(id);
    return element === undefined ? null : element.currentTime;
  }, clipId);
}

/**
 * Races `promise` against a plain Node timer, never against anything that itself needs the page to
 * respond.
 *
 * GUARD: `use.actionTimeout` in `playwright.render-and-browser.config.ts` is not a backstop against
 * every hang. It bounds Playwright's own actionability polling, which itself round-trips into the
 * page -- so when the page's own main thread is genuinely wedged (observed directly:
 * `effect.contrast_permille.lower` sat inside a single `locator.fill()` for the full 900s test
 * budget with no further retry log lines, unlike a merely-disabled control which retries quickly and
 * visibly), that polling itself never gets an answer and the configured timeout never fires either.
 * A `setTimeout`-based race is the one guard that does not depend on the thing that might be stuck.
 */
function withTimeout<T>(
  promise: Promise<T>,
  ms: number,
  label: string,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error(`${label}: exceeded ${ms}ms`)),
      ms,
    );
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (error: unknown) => {
        clearTimeout(timer);
        reject(error instanceof Error ? error : new Error(String(error)));
      },
    );
  });
}

/** Boots the composition-series shell on `target` and lands it on the Clip editor tab, real gesture. */
async function bootShell(target: Page) {
  const control = await openCompositionSeriesShell(target);
  await toClipEditor(target);
  return control;
}

test("presents every render_and_browser composition and records its observation", async ({
  page,
  context,
}, testInfo) => {
  // A development aid only: a comma-separated case-id list narrows the run to those rows. The
  // gatherer never sets it, and the join reports every row the browser stage did not present as
  // BLOCKED, so a narrowed run can never stand in for the sweep.
  const only = (process.env.H3_CONTEXT_NLE_SEMANTIC_ROWS ?? "")
    .split(",")
    .map((item) => item.trim())
    .filter((item) => item.length > 0);
  const allRows = loadWireRows(only.length > 0);
  // The corpus size is the backend's to grow or shrink (most recently `1799977`/`befd155`); this
  // bound only catches the wire generator returning nothing at all, never a specific count.
  expect(allRows.length).toBeGreaterThan(0);
  const rows =
    only.length === 0
      ? allRows
      : allRows.filter((row) => only.includes(row.case_id));
  // A development aid only, the controlled reproduction of the recovery defect (B-29 in the
  // corrective's register): the named row shields every control on the page behind a transparent
  // element and then fails, so the page still evaluates JavaScript while nothing on it can be
  // clicked -- exactly the state one stalled row left behind and that an in-place recovery kept
  // reporting as healthy. With it set, the run asserts that every later row was presented. The
  // gatherer never sets it.
  const faultRow =
    process.env.H3_CONTEXT_NLE_SEMANTIC_FAULT_ROW?.trim() || undefined;
  let faultSeen = false;
  let rowsAfterFault = 0;
  const notRecovered: string[] = [];
  const mediaConstants = loadMediaConstants();
  // The presentation table is the wires' own, and it is read from the rows this run will present
  // rather than from all of them: a `--only` selection must still find its base here.
  basePresentation = presentationsFrom(rows);
  test.setTimeout(sweepBudgetMs(rows));

  // GUARD: the project summary ("Refresh workspace") and the `NleLauncher` overlay entry mount on
  // the "Clip editor" Production function tab, not "Production" (`H3Sidebar.tsx` renders
  // `ProductionWorkbench` for the latter and `<NleLauncher/><NleProjectSummary/>` for the former).
  // Navigating to "Production" here waits forever for a button that is not on that tab -- the
  // defect this journey's very first real run actually hit, masked at the time by the
  // evidence-privacy-filter failure this file also fixes.
  // Rows are presented one base at a time, because the arrangement a base needs is a property of
  // the browser context and cannot be changed inside one. Corpus rows keep their order and their
  // context; the high-resolution rows follow in a second context at ratio 2.
  const ordered = [...rows].sort(
    (left, right) =>
      Number(left.base === "high_resolution") -
      Number(right.base === "high_resolution"),
  );
  let activeBase: string | null = null;
  let activeContext = context;
  let activePage = page;
  let control!: Awaited<ReturnType<typeof bootShell>>;
  let loadedOnce = false;

  async function useBase(base: string): Promise<void> {
    if (activeBase === base) return;
    const presentation = presentationFor(base);
    if (activeBase !== null) {
      await activePage.close({ runBeforeUnload: false }).catch(() => undefined);
      if (activeContext !== context)
        await activeContext.close().catch(() => undefined);
    }
    if (presentation.deviceScaleFactor === 1) {
      activeContext = context;
      activePage = activeBase === null ? page : await context.newPage();
    } else {
      const owner = context.browser();
      if (owner === null)
        throw new Error(
          "this stage needs a browser that can open a second context",
        );
      activeContext = await owner.newContext({
        viewport: { width: 1440, height: 900 },
        deviceScaleFactor: presentation.deviceScaleFactor,
      });
      activePage = await activeContext.newPage();
    }
    control = await bootShell(activePage);
    loadedOnce = false;
    activeBase = base;
  }

  for (const row of ordered) {
    await useBase(row.base);
    if (row.status === "BLOCKED") {
      // No wire exists for this row (a real refusal from the accepted decoder/command engine
      // while resolving its declared `setup`, never routed around) -- named, not synthesized.
      await recordShellObservation(testInfo, {
        case_id: row.case_id,
        executed: false,
        missing: [
          `${row.case_id}: the accepted composition setup could not be resolved ` +
            `(blocked_code=${row.blocked_code})`,
        ],
        facts: {},
      });
      continue;
    }

    try {
      // Shadows the outer fixture for this row only, so every line below reads whichever page is
      // currently live without a file-wide rename -- `activePage` can change under the catch block
      // below (see the health check / fresh-page recovery there) between one iteration and the next.
      const page = activePage;
      if (faultSeen) rowsAfterFault += 1;
      await withTimeout(
        (async () => {
          if (row.case_id === faultRow) {
            await page.evaluate(() => {
              const shield = document.createElement("div");
              shield.id = "h3-nle-semantic-fault-shield";
              shield.style.cssText =
                "position:fixed;inset:0;z-index:2147483647;background:transparent";
              document.body.appendChild(shield);
            });
            throw new Error(
              `${row.case_id}: injected fault, the page's controls are shielded`,
            );
          }
          // B-M2545-28: the media owners as they stand *before* this row's composition is
          // selected. A single reading taken at a stall cannot say whether the incoming
          // composition acquired anything at all -- the counts are cumulative for the page, so
          // "acquired 2" is equally "the previous row acquired twice" and "each row acquired
          // once". The delta against the failure reading is what separates them.
          const entryOwners = await shellSnapshot(page)
            .then((snapshot) => ({
              acquired: snapshot.mediaOwnership.acquired,
              released: snapshot.mediaOwnership.released,
              live: snapshot.mediaOwnership.live,
              videoElements: snapshot.mediaOwnership.videoElements,
            }))
            .catch(() => null);
          control.setComposition(row.wire as Record<string, unknown>);
          const expectedFingerprint = String(row.wire.public_fingerprint);
          if (!loadedOnce) {
            // M25-44: the first explicit open initializes the timeline history, which this mode
            // answers with the composition just set.
            await page.locator(LAUNCHER).click();
            loadedOnce = true;
          } else {
            await page
              .getByRole("region", SUMMARY)
              .getByRole("button", { name: "Refresh workspace" })
              .click();
          }
          // Real state confirmation: the shell's own decoded snapshot identity, not a click receipt.
          await expect
            .poll(
              async () =>
                (await shellSnapshot(page)).timelineSnapshot?.publicFingerprint,
              { timeout: 15_000 },
            )
            .toBe(expectedFingerprint);

          // The first row is already open from the initializing open above.
          if ((await page.locator(OVERLAY).count()) === 0) {
            await toClipEditor(page);
            await page.locator(LAUNCHER).click();
          }
          await expect(page.locator(OVERLAY)).toBeVisible();

          // Poll until the transport has actually presented a real, stable frame -- reading
          // immediately after visibility would sample the cleared surface, not a render.
          //
          // GUARD: this must not require `distinct > 1`. `transform.position_x_bp`'s `lower` boundary
          // row places the clip at `position_x_bp = -40_000` (-400% of the canvas width) for its whole
          // duration -- entirely off-canvas at every output frame, by construction, since that is
          // exactly what the boundary is testing. Such a row's monitor legitimately paints one uniform
          // colour for as long as it is watched; a check that waits for a second colour to appear waits
          // forever and reports a canvas-content timeout for a row that rendered correctly. Real
          // readiness is the canvas holding a non-null, stable capture across two reads in a row --
          // never how many colours that capture happens to contain, since a row can correctly contain
          // exactly one. A frame this test genuinely cannot see anything on still gets real, empty
          // `geometry`/`patches`/`source_mapping` further down -- not a thrown error -- which is what
          // "missing, not fabricated" means for a legitimately blank composition.
          // D45-02: readiness is the compositor's own delivered receipt naming THIS composition,
          // not a stable centre pixel. The old check compared two `measureCanvas` reads for equal
          // dimensions and an equal centre colour, which a cleared surface, a stale frame of the
          // previous row and a legitimately uniform row all satisfy equally well -- it never bound
          // the completed presentation to the composition just selected.
          //
          // GUARD (B-M2545-28): report what the shell was actually holding, never a bare
          // fingerprint mismatch. `lastPresentation` is deliberately not cleared between rows, so
          // "the expected identity never arrived" and "a previous row's identity is still the
          // newest paint" are the same two strings to a plain `expect.poll`, and the first
          // real run of this check spent its whole diagnostic budget on that ambiguity. The
          // thrown message names the composition that was presented instead, whether any paint
          // had ever been delivered on this page, and the surface's own status -- which is what
          // separates "this row's sources never loaded" from "this row painted late".
          const ready = await waitForPresentedFingerprint(page, {
            fingerprint: expectedFingerprint,
            budgetMs: READINESS_BUDGET_MS,
          });
          if (!ready.matched) {
            // B-M2545-28: the media owners the harness itself minted, read at the moment of the
            // refusal. `live` and `videoElements` are the only available answer to "is the
            // *outgoing* composition still holding its sources while this one tries to acquire
            // its own", which the monitor's copy cannot say: `source_unavailable` is the
            // catch-all disposition and covers a failed acquisition, a failed prepare, a paint
            // fence and a runtime transport failure alike.
            const state = await shellSnapshot(page).catch(() => null);
            const surface = state?.surfaceStatus ?? "unreadable";
            const owners = state
              ? `acquired ${state.mediaOwnership.acquired}, released ${state.mediaOwnership.released}, ` +
                `live ${state.mediaOwnership.live} (peak ${state.mediaOwnership.maximumLive}), ` +
                `video elements ${state.mediaOwnership.videoElements} (peak ${state.mediaOwnership.maximumVideoElements})`
              : "unreadable";
            // The monitor's own status chip, because that is where a composition-lifecycle refusal
            // becomes visible at all: `surfaceStatus` above is the NLE workspace's, and the
            // session's `cleanup_pending` -- the state a replacement lands in when the outgoing
            // composition's owners do not release inside their bounded deadline -- reaches the page
            // only here and in the Recover affordance beside it.
            const monitor = await page
              .locator('[data-h3-nle-status="monitor"]')
              .innerText({ timeout: 5_000 })
              .then((value) => value.replace(/\s+/g, " ").trim())
              .catch(() => "unreadable");
            // B-M2545-28: the leased `<video>` elements' own state, through the existing
            // `__nleMediaDebug` registry the source-identity reads already use. The session
            // collapses every downstream failure to `source_unavailable`, and these fields
            // separate the candidates it covers: a zero/most-unexpected `videoWidth` is the
            // declared-derivative geometry check, a non-null `error` is a decode refusal, and a
            // `readyState` below HAVE_CURRENT_DATA with no error is a seek that never landed.
            const sources = await page
              .evaluate(() =>
                [
                  ...(window.__nleMediaDebug?.videoElementsByClip.entries() ??
                    []),
                ].map(([clipId, element]) => ({
                  clipId,
                  width: element.videoWidth,
                  height: element.videoHeight,
                  readyState: element.readyState,
                  networkState: element.networkState,
                  currentTime: element.currentTime,
                  seeking: element.seeking,
                  error: element.error?.code ?? null,
                })),
              )
              .then((rows) => JSON.stringify(rows))
              .catch(() => "unreadable");
            throw new Error(
              `first presentation never arrived: expected ${expectedFingerprint}, ` +
                `newest delivered ${ready.presentation?.publicFingerprint ?? "none"} ` +
                `frame ${ready.presentation?.frame ?? "none"} ` +
                `after ${ready.presentation?.delivered ?? 0} paint(s) on this page, ` +
                `surface ${surface}, monitor "${monitor}", ` +
                `owners at row entry ${JSON.stringify(entryOwners)}, ` +
                `owners now [${owners}], ` +
                `sources ${sources}, ` +
                `waited ${ready.waitedMs} ms over ${ready.polls} polls`,
            );
          }

          // B-M2545-30: the product's own restore seek must land before this row's walk starts.
          const quiescent = await waitForQuiescentTransport(
            page,
            expectedFingerprint,
            READINESS_BUDGET_MS,
          );
          if (!quiescent.settled)
            throw new Error(
              `the transport never became quiescent after first presentation: ` +
                `requested ${quiescent.requested ?? "none"}, ` +
                `presented ${quiescent.presented ?? "none"} of ` +
                `${quiescent.identity ?? "none"} (expected ${expectedFingerprint}), ` +
                `waited ${quiescent.waitedMs} ms over ${quiescent.polls} polls`,
            );

          const baseSample = await measureCanvas(page);
          if (baseSample === null)
            throw new Error("canvas measurement unavailable");
          const prescription = row.prescription;

          // Every frame the prescription names, each visited once through the real seek control,
          // each measured for exactly what the prescription asks of it. No budget trims this: a
          // frame not visited is a landmark not observed, and the join would report the row
          // BLOCKED rather than let the missing frame pass unexamined.
          const sourceMapping: SourceMappingEntry[] = [];
          const geometryByLabel = new Map<string, GeometryEntry>();
          const patchesByLabel = new Map<string, PatchEntry>();
          const rampSamples = new Map<
            string,
            Map<number, readonly [number, number, number]>
          >();
          const unsettled: number[] = [];
          // D45-02: what the row was actually waiting on when it gave up, so a failure names the
          // requested frame, the presented frame, the composition identity and the phase instead
          // of only "the transport did not settle".
          const unsettledDetail: Record<string, string | number | null>[] = [];
          // D45-03: the row's observation cost, split into the four stages that make it up. Source
          // inspection establishes that a full-resolution readback carries 3,686,400 numbers per
          // capture; only measurement establishes what share of the run that actually is, and no
          // speedup may be claimed before this says so.
          const cost = {
            settleMs: 0,
            readbackMs: 0,
            serializeMs: 0,
            transferMs: 0,
            extractMs: 0,
            values: 0,
            frames: 0,
          };

          for (const target of prescription.frames) {
            const actualFrame = await seekTransport(
              page,
              target,
              Math.max(
                ACTION_BOUND_MS,
                ACTION_FRAME_BUDGETS * frameBudgetMs(row),
              ),
            );
            // D45-02: wait for the compositor to report that it delivered THIS frame of THIS
            // composition, then read the pixels once.
            //
            // GUARD: do not go back to inferring this from the picture. The previous version
            // accepted two equal signatures of the first 400 RGBA values -- 100 pixels of the top
            // left corner -- which a stale frame, a blank frame and an off-canvas row all satisfy,
            // and `seekTransport` accepted two equal seek-input values, which `NleMonitor`
            // satisfies immediately because it shows `requestedFrame` before a seek settles. 55
            // rows of the 20:28 sweep were priced on frames that had not been presented. The
            // receipt below is the compositor's own, emitted after the paint was delivered, and it
            // carries the frame and the composition fingerprint; it cannot make a presentation
            // succeed, and the pixel and frame-identity checks further down are untouched.
            const settlement = await waitForPresentedFrame(page, {
              frame: target,
              fingerprint: expectedFingerprint,
              budgetMs: Math.max(
                ACTION_BOUND_MS,
                ACTION_FRAME_BUDGETS * frameBudgetMs(row),
              ),
            });
            const captureStarted = Date.now();
            const settled = settlement.matched
              ? await captureImage(page)
              : null;
            const captureMs = Date.now() - captureStarted;
            if (settled === null || actualFrame !== target) {
              unsettled.push(target);
              unsettledDetail.push({
                requested_frame: target,
                seek_reported_frame: actualFrame,
                presented_frame: settlement.presentation?.frame ?? null,
                presented_fingerprint:
                  settlement.presentation?.publicFingerprint ?? null,
                expected_fingerprint: expectedFingerprint,
                delivered_paints: settlement.presentation?.delivered ?? 0,
                waited_ms: settlement.waitedMs,
                polls: settlement.polls,
                phase:
                  actualFrame !== target
                    ? "seek_did_not_reach_target"
                    : settlement.matched
                      ? "capture_unavailable_after_presentation"
                      : "presentation_never_delivered",
              });
              continue;
            }

            // D45-03: four stages, priced separately. `captureMs` is the whole round trip, so the
            // transfer is what it has left after the in-page readback and serialization.
            cost.settleMs += settlement.waitedMs;
            cost.readbackMs += settled.readbackMs;
            cost.serializeMs += settled.serializeMs;
            cost.transferMs += Math.max(
              0,
              captureMs - settled.readbackMs - settled.serializeMs,
            );
            cost.values += settled.data.length;
            const extractStarted = Date.now();
            const observed = observePrescribedFrame({
              image: {
                data: settled.data,
                width: settled.width,
                height: settled.height,
              },
              frame: target,
              prescription,
              constants: mediaConstants,
            });
            cost.extractMs += Date.now() - extractStarted;
            cost.frames += 1;
            for (const patch of observed.patches)
              patchesByLabel.set(patch.label, patch);
            for (const fiducial of observed.geometry)
              geometryByLabel.set(fiducial.label, fiducial);
            for (const sample of observed.ramp_samples) {
              let perFrame = rampSamples.get(sample.label);
              if (perFrame === undefined) {
                perFrame = new Map();
                rampSamples.set(sample.label, perFrame);
              }
              perFrame.set(sample.output_frame, sample.rgb);
            }
            if (
              observed.identity !== null &&
              observed.identity.source_frame !== null
            ) {
              // The presented source time is the real `<video>` element's own clock -- the
              // element leased for the clip whose identity row was read, never a fixed one: a
              // primary track can hand over to another asset mid-timeline, and two clips can
              // lease one asset with different clocks. The join holds it to half a source tick
              // of the expectation.
              const currentTime = await readCurrentTime(
                page,
                observed.identity.clip_id,
              );
              if (currentTime !== null) {
                sourceMapping.push({
                  output_frame: target,
                  source_frame: observed.identity.source_frame,
                  source_time: currentTime.toFixed(6),
                });
              }
            }
          }

          const alphas: AlphaEntry[] = [];
          for (const target of prescription.alpha_targets) {
            const perFrame = rampSamples.get(target.label);
            if (perFrame !== undefined)
              alphas.push(...alphasForTarget(target, perFrame));
          }

          const presented = await shellSnapshot(page);
          const publicFingerprint =
            presented.timelineSnapshot?.publicFingerprint ?? null;
          const missing: string[] = [];
          // AC45-05: a high-resolution row is about detail at a declared backing, so a backing of
          // any other size answers a different question. Say so rather than record the numbers a
          // smaller canvas happened to give.
          const required = presentationFor(row.base).backing;
          if (
            required !== null &&
            (baseSample.width !== required.width ||
              baseSample.height !== required.height)
          )
            missing.push(
              `${row.case_id}: the monitor presented a ${baseSample.width}x${baseSample.height} ` +
                `backing where this base declares ${required.width}x${required.height}`,
            );
          if (
            baseSample.presentationGeometry.monitorRect === null ||
            baseSample.presentationGeometry.pictureRect === null
          )
            missing.push(
              `${row.case_id}: semantic pixels were not read from the monitor picture canvas`,
            );
          if (publicFingerprint !== expectedFingerprint)
            missing.push(
              `${row.case_id}: presented public_fingerprint diverged from the composition set`,
            );
          if (unsettled.length > 0)
            missing.push(
              `${row.case_id}: the transport did not settle on output frame(s) ${unsettled.join(", ")} ` +
                `[${unsettledDetail
                  .map(
                    (detail) =>
                      `frame ${detail.requested_frame}: ${detail.phase}, seek reported ` +
                      `${detail.seek_reported_frame}, last presented frame ` +
                      `${detail.presented_frame} of ${detail.presented_fingerprint ?? "no composition"}, ` +
                      `${detail.delivered_paints} paint(s) delivered, waited ${detail.waited_ms} ms ` +
                      `over ${detail.polls} poll(s)`,
                  )
                  .join("; ")}]`,
            );

          await recordShellObservation(testInfo, {
            case_id: row.case_id,
            executed: true,
            missing,
            canvas_width: baseSample.width,
            canvas_height: baseSample.height,
            source_mapping: sourceMapping,
            geometry: [...geometryByLabel.values()],
            patches: [...patchesByLabel.values()],
            color_patches: [...patchesByLabel.values()],
            alphas,
            facts: {
              publicFingerprint: publicFingerprint ?? "",
              previewScale: prescription.preview_scale,
              distinctSampledColors: baseSample.distinct,
              opaquePixels: baseSample.opaque,
              totalPixels: baseSample.total,
              centerPatchRgb: baseSample.centerPatchRgb,
              centerPatchEdgeDistancePx: baseSample.centerPatchEdgeDistance,
              base: row.base,
              devicePixelRatio: presentationFor(row.base).deviceScaleFactor,
              presentationGeometry: JSON.stringify(
                baseSample.presentationGeometry,
              ),
              framesPrescribed: prescription.frames.length,
              framesSampled: prescription.frames.length - unsettled.length,
              // B-M2545-28: what the *first* paint after this row's composition was selected
              // actually cost, so `READINESS_BUDGET_MS` is set from measurement rather than
              // guessed. It is not part of the cost split below: every other frame of the row
              // reuses the sources this one paid to bring up.
              observationFirstPaintMs: ready.waitedMs,
              observationRestoreSettleMs: quiescent.waitedMs,
              // D45-03, the observation cost split. Totals over the row's sampled frames.
              observationFramesMeasured: cost.frames,
              observationSettleMs: Math.round(cost.settleMs),
              observationReadbackMs: Math.round(cost.readbackMs),
              observationSerializeMs: Math.round(cost.serializeMs),
              observationTransferMs: Math.round(cost.transferMs),
              observationExtractMs: Math.round(cost.extractMs),
              observationValuesTransferred: cost.values,
              sourceMappingEntries: sourceMapping.length,
              patchesFound: patchesByLabel.size,
              geometryFiducialsFound: geometryByLabel.size,
              alphasFound: alphas.length,
            },
          });

          await page.locator(CLOSE).click();
          await expect(page.locator(OVERLAY)).toHaveCount(0);
        })(),
        rowBudgetMs(row),
        row.case_id,
      );
    } catch (error) {
      // One composition's failure must not lose the other rows' evidence. The full diagnostic
      // goes to the console for real debugging; only a sanitized form ever reaches the evidence
      // attachment (see `sanitizeErrorMessage`).
      // eslint-disable-next-line no-console
      console.error(`${row.case_id}: presentation failed`, error);
      if (row.case_id === faultRow) {
        faultSeen = true;
      } else if (faultSeen) {
        notRecovered.push(row.case_id);
      }
      await recordShellObservation(testInfo, {
        case_id: row.case_id,
        executed: false,
        missing: [
          `${row.case_id}: could not be presented in the browser (${sanitizeErrorMessage(error)})`,
        ],
        facts: {},
      });

      // GUARD: recovery always gets a fresh page rather than trying to nurse the failed one back to
      // a known-good state in place. A first real run tried the cheaper in-place recovery (Escape,
      // click "Clip editor") whenever the page still answered a bare `evaluate` probe -- and that
      // probe is too weak a signal: `toClipEditor`'s own clicks reported success while the specific
      // affordance the *next* row actually needs (then the "Load/Refresh professional timeline" button,
      // observed directly) stayed permanently un-clickable, so one early stall
      // (`text.background_rgba.present`, a canvas that never attached) cascaded into 67 consecutive
      // failures with no further diagnostic value -- every later row failed for the same unrecovered
      // reason, not its own. A page that merely evaluates JavaScript is not proof the specific control
      // surface a row depends on is usable again, and the only state this journey can fully trust is
      // one it just built from the real boot gesture itself. The cost is one extra page boot per
      // failing row; the alternative is losing every row after the first failure to a state this
      // journey can no longer verify.
      // eslint-disable-next-line no-console
      console.error(`${row.case_id}: recovering with a fresh page`);
      await withTimeout(
        (async () => {
          await activePage
            .close({ runBeforeUnload: false })
            .catch(() => undefined);
          activePage = await activeContext.newPage();
          control = await bootShell(activePage);
          // A fresh page has no timeline history yet; its first row initializes one by opening.
          loadedOnce = false;
        })(),
        ROW_BUDGET_MS,
        `${row.case_id}: fresh-page recovery`,
      ).catch((recoveryError: unknown) => {
        // The context itself is gone (observed directly: a recovery racing the outer test timeout
        // hit `browserContext.newPage: Target page, context or browser has been closed`) -- nothing
        // further in this loop can open a page, so let every remaining row fail fast and honestly on
        // its own rather than retry a boot that cannot succeed.
        // eslint-disable-next-line no-console
        console.error(
          `${row.case_id}: fresh-page recovery failed`,
          recoveryError,
        );
      });
      loadedOnce = false;
    }
  }

  if (faultRow !== undefined) {
    // The recovery contract, checked only under the controlled reproduction: the injected fault
    // was reached, at least one row followed it (rows run in corpus order, whatever order the
    // row list names them in), and none of those rows was lost to the state the fault left.
    expect(faultSeen).toBe(true);
    expect(rowsAfterFault).toBeGreaterThan(0);
    expect(notRecovered).toEqual([]);
  }

  // Presenting every composition is never itself a generation side effect.
  const final = await shellSnapshot(activePage);
  expect(final.queuedPrompts).toBe(0);
  expect(final.renderJobRequests).toBe(0);
});
