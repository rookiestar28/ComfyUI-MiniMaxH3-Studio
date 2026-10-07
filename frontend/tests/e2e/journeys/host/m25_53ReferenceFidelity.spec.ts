import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { mkdir, readFile, stat, writeFile } from "node:fs/promises";
import { dirname, relative, resolve } from "node:path";

import type { Locator, Page, Request, TestInfo } from "@playwright/test";

import {
  decodeAuthoringProjection,
  decodeTimelineHistoryProjectionV2,
  decodeTimelineReceiptV2,
  encodeAuthoringAction,
  NLE_AUTHORING_PROFILE_ID,
  NLE_AUTHORING_SCHEMA,
  NLE_OPERATION_PROFILE_ID_V2,
  type TimelineReceiptV2,
} from "../../../../src/contracts/authoringWorkbenchCodec";
import {
  decodeOutputStatus,
  OUTPUT_CAPABILITY,
} from "../../../../src/contracts/authoringOutputCodec";
import { AUTHORING_AUDIO_PEAKS_PAIR_RATE } from "../../../../src/contracts/authoringAudioPeaks";
import { diffGraphSurroundings } from "../../../support/surroundingsDiff";
import {
  chooseAssetCommand,
  chooseMediaOption,
} from "../../helpers/nleBinMenus";
import { openExportPanel } from "../../helpers/nleExport";
import { nleLaneOrigin } from "../../helpers/nleTargets";
import { seekPlayhead } from "../../helpers/nleTimeline";
import {
  assertCandidateBundleInjection,
  candidateBackendMode,
  candidateBackendRuntime,
  candidateBundle,
  candidateInjectionCount,
  hostUrl,
  repositoryRoot,
} from "../../host/candidate";
import { expect, test } from "../../host/fixture";
import { supportedHostQueueCounts } from "../../host/managed";
import {
  beginSettledH3InteractionPhase,
  captureM17CanonicalIdentity,
  expectCandidateInteractionNetworkLocal,
  monitorCandidateInitiatorNetwork,
  monitorH3Network,
  waitForH3Registration,
} from "../../host/network";

const AUTHORING_ROUTE = "/h3-context/v1/authoring/action";
// Require a visible waveform trace, not isolated anti-alias/noise pixels.
const MIN_COMPOSED_WAVEFORM_PIXELS = 100;
// IMPORTANT (M25-64 B-M2564-13): since M25-62 the waveform is stroked in the resolved
// `--h3-nle-waveform` role colour, not in a cyan family, and a cyan rule counts none of it. A
// pixel is the waveform's when every channel is within this many levels of that colour: the stroke
// is opaque and pixel-aligned, so its core pixels carry the colour exactly (measured on the host:
// 62/207/142 on about 600 sampled pixels a clip), while the nearest source frame, green at
// 10/149/4, is 138 levels away in blue and its blend with the stroke at least 74. Each reader also
// counts the rule's matches in the clip's filmstrip rows, which must be none, so the rule cannot be
// counting a source frame.
const WAVEFORM_COLOUR_TOLERANCE = 16;
const PRE_FIX_CLIP_BODY_LAYER_RULE = `
  .h3-nle-dialog .h3-nle-clip-body {
    position: static !important;
    z-index: auto !important;
  }
  .h3-nle-clip-body span,
  .h3-nle-clip-body small {
    position: relative !important;
    z-index: 5 !important;
    pointer-events: none !important;
  }
`;
const FIXTURE_ENV = "H3_CONTEXT_NLE_WORKSPACE_FIXTURE";
const EXPECTATION_ENV = "H3_CONTEXT_M25_53_EXPECTATION";
const FFMPEG_ENV = "H3_CONTEXT_M25_53_OBSERVER_FFMPEG";
const FFPROBE_ENV = "H3_CONTEXT_M25_53_OBSERVER_FFPROBE";
const INVENTORY_ENV = "H3_CONTEXT_CANDIDATE_INVENTORY_SHA256";
const CONTAINER_ID = "h3-m25-53-host-container";
// Keep the host oracle independent of the returned render snapshot: these are the requested
// source placements and overlays built through the real editor interactions below. The output
// ends at the last placed content frame; edit capacity is not an export boundary.
const EXPECTED_PRIMARY_PLACEMENTS = [
  { startFrame: 0, sourceStartFrame: 0, durationFrames: 96 },
  { startFrame: 96, sourceStartFrame: 0, durationFrames: 96 },
  { startFrame: 192, sourceStartFrame: 0, durationFrames: 95 },
] as const;
const EXPECTED_OVERLAY_PLACEMENTS = [
  { startFrame: 144, durationFrames: 24 },
  { startFrame: 160, durationFrames: 24 },
] as const;
const EXPECTED_FIRST_INSERT_CONTENT_END_EXCLUSIVE =
  EXPECTED_PRIMARY_PLACEMENTS[0].startFrame +
  EXPECTED_PRIMARY_PLACEMENTS[0].durationFrames;
const EXPECTED_OUTPUT_DURATION_FRAMES = Math.max(
  ...EXPECTED_PRIMARY_PLACEMENTS.map(
    ({ startFrame, durationFrames }) => startFrame + durationFrames,
  ),
  ...EXPECTED_OVERLAY_PLACEMENTS.map(
    ({ startFrame, durationFrames }) => startFrame + durationFrames,
  ),
);
type WorkspaceFixture = Readonly<{
  schema: "NleWorkspaceHostFixtureV1";
  output: unknown;
  anchorId: string;
  promptId: string;
  qualification: Readonly<{
    schema: "M25RealRuntimeQualificationV1";
    candidateBundleSha256: string;
  }>;
}>;

type SourceExpectation = Readonly<{
  assetId: string;
  relativePath: string;
  sha256: string;
}>;

type ReferenceExpectation = Readonly<{
  schema: "M25_53ReferenceExpectationV1";
  assetIds: readonly [string, string, string];
  sources: readonly [SourceExpectation, SourceExpectation, SourceExpectation];
  audioHz: readonly [number, number, number];
  landmark: Readonly<{
    sourceWidth: number;
    sourceHeight: number;
    centerY: number;
    centerXBySecond: readonly [number, number, number, number];
  }>;
  ffmpegSha256: string;
  ffprobeSha256: string;
}>;

type HostResponse = Readonly<{ status: number; body: unknown }>;
type Rgb = Readonly<{ red: number; green: number; blue: number }>;

async function expectFullyVisibleThroughClipAncestors(
  element: Locator,
  description: string,
): Promise<void> {
  const visibility = await element.evaluate((target) => {
    const rect = target.getBoundingClientRect();
    const targetRect = {
      left: rect.left,
      top: rect.top,
      right: rect.right,
      bottom: rect.bottom,
    };
    const visibleRect = {
      left: Math.max(0, rect.left),
      top: Math.max(0, rect.top),
      right: Math.min(window.innerWidth, rect.right),
      bottom: Math.min(window.innerHeight, rect.bottom),
    };
    const clippingAncestors: Array<{
      name: string;
      clipsX: boolean;
      clipsY: boolean;
      rect: readonly [number, number, number, number];
    }> = [];
    const clips = (overflow: string) => overflow !== "visible";
    for (
      let ancestor = target.parentElement;
      ancestor !== null;
      ancestor = ancestor.parentElement
    ) {
      const style = getComputedStyle(ancestor);
      const clipsX = clips(style.overflowX);
      const clipsY = clips(style.overflowY);
      if (!clipsX && !clipsY) continue;
      const ancestorRect = ancestor.getBoundingClientRect();
      const clipLeft = ancestorRect.left + ancestor.clientLeft;
      const clipTop = ancestorRect.top + ancestor.clientTop;
      const clipRight = clipLeft + ancestor.clientWidth;
      const clipBottom = clipTop + ancestor.clientHeight;
      if (clipsX) {
        visibleRect.left = Math.max(visibleRect.left, clipLeft);
        visibleRect.right = Math.min(visibleRect.right, clipRight);
      }
      if (clipsY) {
        visibleRect.top = Math.max(visibleRect.top, clipTop);
        visibleRect.bottom = Math.min(visibleRect.bottom, clipBottom);
      }
      clippingAncestors.push({
        name:
          ancestor.tagName.toLowerCase() +
          (ancestor.id
            ? "#" + ancestor.id
            : "." + String(ancestor.className ?? "").slice(0, 18)),
        clipsX,
        clipsY,
        rect: [
          Math.round(clipLeft),
          Math.round(clipTop),
          Math.round(clipRight),
          Math.round(clipBottom),
        ],
      });
    }
    return { targetRect, visibleRect, clippingAncestors };
  });
  const { targetRect, visibleRect, clippingAncestors } = visibility;
  const tolerance = 1;
  const context =
    description +
    " target=" +
    JSON.stringify(targetRect) +
    " visible=" +
    JSON.stringify(visibleRect) +
    " clipAncestors=" +
    JSON.stringify(clippingAncestors);
  expect(visibleRect.left, `${context}: left`).toBeLessThanOrEqual(
    targetRect.left + tolerance,
  );
  expect(visibleRect.top, `${context}: top`).toBeLessThanOrEqual(
    targetRect.top + tolerance,
  );
  expect(visibleRect.right, `${context}: right`).toBeGreaterThanOrEqual(
    targetRect.right - tolerance,
  );
  expect(visibleRect.bottom, `${context}: bottom`).toBeGreaterThanOrEqual(
    targetRect.bottom - tolerance,
  );
}

test.use({ viewport: { width: 1402, height: 868 }, deviceScaleFactor: 1 });

function record(value: unknown, name: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value))
    throw new Error(`${name} is malformed`);
  return value as Record<string, unknown>;
}

function sha256(value: Buffer): string {
  return createHash("sha256").update(value).digest("hex");
}

function privateRepositoryPath(environmentName: string): string {
  const configured = process.env[environmentName]?.trim() ?? "";
  if (configured.length === 0)
    throw new Error(`${environmentName} is required`);
  const absolute = resolve(repositoryRoot, configured);
  const inside = relative(repositoryRoot, absolute).replaceAll("\\", "/");
  if (
    inside === "" ||
    inside.startsWith("../") ||
    resolve(repositoryRoot, inside) !== absolute ||
    !inside.startsWith(".planning/evidence/")
  )
    throw new Error(`${environmentName} must name private repository evidence`);
  return absolute;
}

async function loadWorkspaceFixture(): Promise<WorkspaceFixture> {
  const wire = record(
    JSON.parse(await readFile(privateRepositoryPath(FIXTURE_ENV), "utf8")),
    "workspace fixture",
  );
  const qualification = record(wire.qualification, "workspace qualification");
  if (
    wire.schema !== "NleWorkspaceHostFixtureV1" ||
    qualification.schema !== "M25RealRuntimeQualificationV1" ||
    qualification.candidateBundleSha256 !== candidateBundle?.sha256 ||
    typeof wire.anchorId !== "string" ||
    !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(wire.anchorId) ||
    typeof wire.promptId !== "string" ||
    wire.promptId.length === 0
  )
    throw new Error("workspace fixture is not bound to the exact candidate");
  return wire as WorkspaceFixture;
}

async function loadExpectation(): Promise<
  Readonly<{
    expectation: ReferenceExpectation;
    sourcePaths: readonly string[];
  }>
> {
  const path = privateRepositoryPath(EXPECTATION_ENV);
  const wire = record(JSON.parse(await readFile(path, "utf8")), "expectation");
  const assetIds = wire.assetIds;
  const sources = wire.sources;
  const audioHz = wire.audioHz;
  const landmark = record(wire.landmark, "source landmark");
  if (
    wire.schema !== "M25_53ReferenceExpectationV1" ||
    !Array.isArray(assetIds) ||
    assetIds.length !== 3 ||
    new Set(assetIds).size !== 3 ||
    !assetIds.every(
      (value) =>
        typeof value === "string" &&
        /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(value),
    ) ||
    !Array.isArray(sources) ||
    sources.length !== 3 ||
    !Array.isArray(audioHz) ||
    audioHz.length !== 3 ||
    new Set(audioHz).size !== 3 ||
    !audioHz.every(
      (value) => Number.isInteger(value) && value >= 100 && value <= 2000,
    ) ||
    landmark.sourceWidth !== 640 ||
    landmark.sourceHeight !== 360 ||
    landmark.centerY !== 40 ||
    !Array.isArray(landmark.centerXBySecond) ||
    landmark.centerXBySecond.length !== 4 ||
    landmark.centerXBySecond.some(
      (value, index) => value !== [52, 180, 308, 436][index],
    ) ||
    !/^[0-9a-f]{64}$/.test(String(wire.ffmpegSha256 ?? "")) ||
    !/^[0-9a-f]{64}$/.test(String(wire.ffprobeSha256 ?? ""))
  )
    throw new Error("reference expectation is malformed");
  const sourcePaths: string[] = [];
  const sourceDigests = new Set<string>();
  for (const [index, value] of sources.entries()) {
    const source = record(value, `source ${index}`);
    if (
      source.assetId !== assetIds[index] ||
      typeof source.relativePath !== "string" ||
      !/^[0-9a-f]{64}$/.test(String(source.sha256 ?? ""))
    )
      throw new Error("reference expectation source is malformed");
    const sourcePath = resolve(dirname(path), source.relativePath);
    const inside = relative(dirname(path), sourcePath).replaceAll("\\", "/");
    if (inside === "" || inside.startsWith("../"))
      throw new Error("reference expectation source escapes its evidence root");
    if (sha256(await readFile(sourcePath)) !== source.sha256)
      throw new Error("reference expectation source digest changed");
    sourceDigests.add(source.sha256 as string);
    sourcePaths.push(sourcePath);
  }
  if (sourceDigests.size !== 3)
    throw new Error("reference expectation must pin three distinct sources");
  return {
    expectation: wire as unknown as ReferenceExpectation,
    sourcePaths,
  };
}

type OwnedCapture = Readonly<{ name: string; sha256: string }>;

function pngDimensions(
  png: Buffer,
): Readonly<{ width: number; height: number }> {
  const signature = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
  if (png.length < 24 || !png.subarray(0, 8).equals(signature))
    throw new Error("owned capture is not a complete PNG");
  return Object.freeze({
    width: png.readUInt32BE(16),
    height: png.readUInt32BE(20),
  });
}

async function expectLoadedAssetCard(
  assetCard: Locator,
  description: string,
): Promise<void> {
  const thumbnail = assetCard.locator("[data-h3-nle-thumbnail]");
  await expect
    .poll(
      () =>
        thumbnail.evaluate((node) => {
          const canvas = node as HTMLCanvasElement;
          return canvas.width > 0 && canvas.height > 0;
        }),
      { message: description + " thumbnail must finish loading" },
    )
    .toBe(true);
  await expect(assetCard.locator(".h3-nle-thumbnail-status")).toHaveText(
    "Duration 00:00:04:00",
  );
}

// M25-64 B-M2564-13: the colour the timeline paints its waveform with, resolved where its canvas
// sits (the light and dark palettes differ) and normalised through a scratch context. An empty,
// invalid or translucent colour cannot be matched exactly, which is an observer failure.
type WaveformColour = Readonly<{
  token: string;
  rgb: readonly [number, number, number];
}>;

async function resolveWaveformColour(
  overlay: Locator,
): Promise<WaveformColour> {
  const resolved = await overlay
    .locator('[data-h3-nle-canvas="timeline_decoration"]')
    .evaluate((canvas) => {
      const token = getComputedStyle(canvas)
        .getPropertyValue("--h3-nle-waveform")
        .trim();
      const scratch = document.createElement("canvas").getContext("2d");
      if (scratch === null || !CSS.supports("color", token))
        return { token, normalised: null };
      scratch.fillStyle = token;
      return { token, normalised: String(scratch.fillStyle) };
    });
  const hex = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(
    resolved.normalised ?? "",
  );
  if (resolved.token === "" || hex === null)
    throw new Error(
      `observer failure: the waveform colour ${JSON.stringify(resolved)} is not an opaque colour this observer can match`,
    );
  return {
    token: resolved.token,
    rgb: [
      Number.parseInt(hex[1]!, 16),
      Number.parseInt(hex[2]!, 16),
      Number.parseInt(hex[3]!, 16),
    ],
  };
}

async function readClipDecorationPixels(
  overlay: Locator,
  clips: readonly Readonly<{
    clipId: string;
    expectedChannel: "red" | "green" | "blue";
  }>[],
  waveform: WaveformColour,
): Promise<
  readonly {
    clipId: string;
    filmstripSourcePixels: number;
    waveformColourPixels: number;
    filmstripWaveformColourPixels: number;
  }[]
> {
  return overlay.locator('[data-h3-nle-canvas="timeline_decoration"]').evaluate(
    (node, { expectedClips, rgb, tolerance }) => {
      const none = (clipId: string) => ({
        clipId,
        filmstripSourcePixels: 0,
        waveformColourPixels: 0,
        filmstripWaveformColourPixels: 0,
      });
      const canvas = node as HTMLCanvasElement;
      const context = canvas.getContext("2d");
      if (context === null || canvas.width === 0 || canvas.height === 0)
        return expectedClips.map(({ clipId }) => none(clipId));
      const waveformColourPixels = (data: Uint8ClampedArray) => {
        let count = 0;
        for (let index = 0; index < data.length; index += 4)
          if (
            data[index + 3]! > 0 &&
            Math.abs(data[index]! - rgb[0]) <= tolerance &&
            Math.abs(data[index + 1]! - rgb[1]) <= tolerance &&
            Math.abs(data[index + 2]! - rgb[2]) <= tolerance
          )
            count += 1;
        return count;
      };
      const canvasBounds = canvas.getBoundingClientRect();
      const scaleX = canvas.width / canvasBounds.width;
      const scaleY = canvas.height / canvasBounds.height;
      return expectedClips.map(({ clipId, expectedChannel }) => {
        const clip = document.querySelector<HTMLElement>(
          `[data-h3-nle-clip="${CSS.escape(clipId)}"]`,
        );
        if (clip === null) return none(clipId);
        const bounds = clip.getBoundingClientRect();
        const left = Math.max(
          0,
          Math.floor((bounds.left - canvasBounds.left) * scaleX),
        );
        const right = Math.min(
          canvas.width,
          Math.ceil((bounds.right - canvasBounds.left) * scaleX),
        );
        const filmstripTop = Math.max(
          0,
          Math.ceil((bounds.top - canvasBounds.top + 3) * scaleY),
        );
        const waveformTop = Math.max(
          0,
          Math.floor((bounds.bottom - canvasBounds.top - 28) * scaleY),
        );
        const bottom = Math.min(
          canvas.height,
          Math.ceil((bounds.bottom - canvasBounds.top - 3) * scaleY),
        );
        if (
          right <= left ||
          waveformTop <= filmstripTop ||
          bottom <= waveformTop
        )
          return none(clipId);
        const filmstripPixels = context.getImageData(
          left,
          filmstripTop,
          right - left,
          waveformTop - filmstripTop,
        ).data;
        const waveformPixels = context.getImageData(
          left,
          waveformTop,
          right - left,
          bottom - waveformTop,
        ).data;
        const channelIndex = { red: 0, green: 1, blue: 2 }[expectedChannel];
        const otherChannels = [0, 1, 2].filter(
          (index) => index !== channelIndex,
        );
        let filmstripSourcePixels = 0;
        for (let index = 0; index < filmstripPixels.length; index += 4) {
          const dominant = filmstripPixels[index + channelIndex]!;
          if (
            filmstripPixels[index + 3]! > 0 &&
            dominant >= 96 &&
            dominant > filmstripPixels[index + otherChannels[0]!]! * 1.45 &&
            dominant > filmstripPixels[index + otherChannels[1]!]! * 1.45
          )
            filmstripSourcePixels += 1;
        }
        return {
          clipId,
          filmstripSourcePixels,
          waveformColourPixels: waveformColourPixels(waveformPixels),
          filmstripWaveformColourPixels: waveformColourPixels(filmstripPixels),
        };
      });
    },
    {
      expectedClips: clips,
      rgb: waveform.rgb,
      tolerance: WAVEFORM_COLOUR_TOLERANCE,
    },
  );
}

async function readClipDecorationLayout(
  overlay: Locator,
  clips: readonly Readonly<{
    clipId: string;
    expectedChannel: "red" | "green" | "blue";
  }>[],
  waveform: WaveformColour,
  ownedSurfaceCaptureSize?: Readonly<{ width: number; height: number }>,
): Promise<Readonly<Record<string, unknown>>> {
  const ownedSurfaceBounds = await overlay.boundingBox();
  if (ownedSurfaceBounds === null)
    throw new Error("owned workspace crop is not measurable");
  return overlay.locator('[data-h3-nle-canvas="timeline_decoration"]').evaluate(
    (node, { expectedClips, cropBounds, captureSize, rgb, tolerance }) => {
      const canvas = node as HTMLCanvasElement;
      const canvasBounds = canvas.getBoundingClientRect();
      const round = (value: number) => Math.round(value * 100) / 100;
      const rect = (bounds: DOMRect) => ({
        viewport: {
          left: round(bounds.left),
          top: round(bounds.top),
          width: round(bounds.width),
          height: round(bounds.height),
        },
        canvasCss: {
          left: round(bounds.left - canvasBounds.left),
          top: round(bounds.top - canvasBounds.top),
          width: round(bounds.width),
          height: round(bounds.height),
        },
        ownedCropCss: {
          left: round(bounds.left - cropBounds.x),
          top: round(bounds.top - cropBounds.y),
          width: round(bounds.width),
          height: round(bounds.height),
        },
      });
      const canvasStyle = getComputedStyle(canvas);
      const canvasScaleX = canvas.width / canvasBounds.width;
      const canvasScaleY = canvas.height / canvasBounds.height;
      const stackingFacts = (element: Element) => {
        const facts: Readonly<Record<string, unknown>>[] = [];
        let current: Element | null = element;
        while (current !== null && facts.length < 18) {
          const style = getComputedStyle(current);
          const bounds = current.getBoundingClientRect();
          facts.push({
            tag: current.tagName.toLowerCase(),
            className:
              typeof current.className === "string"
                ? current.className.slice(0, 96)
                : "",
            bounds: rect(bounds),
            position: style.position,
            zIndex: style.zIndex,
            opacity: style.opacity,
            transform: style.transform,
            filter: style.filter,
            contain: style.contain,
            isolation: style.isolation,
            overflowX: style.overflowX,
            overflowY: style.overflowY,
            clipPath: style.clipPath,
            mixBlendMode: style.mixBlendMode,
            willChange: style.willChange,
          });
          current = current.parentElement;
        }
        return facts;
      };
      const layerFactsAt = (x: number, y: number) =>
        document
          .elementsFromPoint(x, y)
          .slice(0, 8)
          .map((element) => {
            const style = getComputedStyle(element);
            return {
              tag: element.tagName.toLowerCase(),
              className:
                typeof element.className === "string"
                  ? element.className.slice(0, 96)
                  : "",
              decorationCanvas:
                element.getAttribute("data-h3-nle-canvas") ?? null,
              position: style.position,
              zIndex: style.zIndex,
              backgroundColor: style.backgroundColor,
              opacity: style.opacity,
            };
          });
      return {
        backingWidth: canvas.width,
        backingHeight: canvas.height,
        cssWidth: Math.round(canvasBounds.width * 100) / 100,
        cssHeight: Math.round(canvasBounds.height * 100) / 100,
        devicePixelRatio: window.devicePixelRatio,
        canvasViewportOrigin: {
          x: round(canvasBounds.left),
          y: round(canvasBounds.top),
        },
        ownedSurfaceCrop: {
          viewport: {
            x: round(cropBounds.x),
            y: round(cropBounds.y),
            width: round(cropBounds.width),
            height: round(cropBounds.height),
          },
          captureSize: captureSize ?? null,
          capturePixelScale: captureSize
            ? {
                x: round(captureSize.width / cropBounds.width),
                y: round(captureSize.height / cropBounds.height),
              }
            : null,
        },
        canvas: {
          display: canvasStyle.display,
          visibility: canvasStyle.visibility,
          opacity: canvasStyle.opacity,
          zIndex: canvasStyle.zIndex,
          pointerEvents: canvasStyle.pointerEvents,
        },
        canvasStackingChain: stackingFacts(canvas),
        clips: expectedClips.map(({ clipId, expectedChannel }, index) => {
          const clip = document.querySelector<HTMLElement>(
            `[data-h3-nle-clip="${CSS.escape(clipId)}"]`,
          );
          if (clip === null)
            return {
              slot: index + 1,
              expectedChannel,
              found: false,
              bounds: null,
            };
          const bounds = clip.getBoundingClientRect();
          const body = clip.querySelector<HTMLElement>(".h3-nle-clip-body");
          const clipStyle = getComputedStyle(clip);
          const bodyStyle = body === null ? null : getComputedStyle(body);
          const textNodes = Array.from(
            body?.querySelectorAll<HTMLElement>("span, small") ?? [],
          );
          const textBoxes = textNodes.map((element) => {
            const bounds = element.getBoundingClientRect();
            const style = getComputedStyle(element);
            return {
              tag: element.tagName.toLowerCase(),
              bounds: rect(bounds),
              position: style.position,
              zIndex: style.zIndex,
              opacity: style.opacity,
              backgroundColor: style.backgroundColor,
              overflowX: style.overflowX,
              overflowY: style.overflowY,
              clipPath: style.clipPath,
            };
          });
          const filmstripPoint = {
            x: bounds.left + Math.min(72, bounds.width / 2),
            y: bounds.top + Math.min(16, bounds.height / 2),
          };
          const waveformPoint = {
            x: bounds.left + Math.min(72, bounds.width / 2),
            y: bounds.bottom - Math.min(14, bounds.height / 2),
          };
          const rawWaveformPixels: Readonly<{
            x: number;
            y: number;
            viewportCss: { x: number; y: number };
            ownedCropCss: { x: number; y: number };
            ownedCropPixel: { x: number; y: number } | null;
            overlappedText: string[];
          }>[] = [];
          const overlapCounts = new Map<string, number>();
          const context = canvas.getContext("2d", {
            willReadFrequently: true,
          });
          let detectedRawWaveformPixels = 0;
          if (context !== null && canvas.width > 0 && canvas.height > 0) {
            const left = Math.max(
              0,
              Math.floor((bounds.left - canvasBounds.left) * canvasScaleX),
            );
            const right = Math.min(
              canvas.width,
              Math.ceil((bounds.right - canvasBounds.left) * canvasScaleX),
            );
            const top = Math.max(
              0,
              Math.floor(
                (bounds.bottom - canvasBounds.top - 28) * canvasScaleY,
              ),
            );
            const bottom = Math.min(
              canvas.height,
              Math.ceil((bounds.bottom - canvasBounds.top - 3) * canvasScaleY),
            );
            if (right > left && bottom > top) {
              const pixels = context.getImageData(
                left,
                top,
                right - left,
                bottom - top,
              ).data;
              for (let offset = 0; offset < pixels.length; offset += 4) {
                if (
                  pixels[offset + 3]! === 0 ||
                  Math.abs(pixels[offset]! - rgb[0]) > tolerance ||
                  Math.abs(pixels[offset + 1]! - rgb[1]) > tolerance ||
                  Math.abs(pixels[offset + 2]! - rgb[2]) > tolerance
                )
                  continue;
                detectedRawWaveformPixels += 1;
                const pixelIndex = offset / 4;
                const x = left + (pixelIndex % (right - left));
                const y = top + Math.floor(pixelIndex / (right - left));
                const viewportCssX =
                  canvasBounds.left + (x + 0.5) / canvasScaleX;
                const viewportCssY =
                  canvasBounds.top + (y + 0.5) / canvasScaleY;
                const ownedCropCssX = viewportCssX - cropBounds.x;
                const ownedCropCssY = viewportCssY - cropBounds.y;
                const overlappedText = textNodes.flatMap(
                  (element, textIndex) => {
                    const textBounds = element.getBoundingClientRect();
                    if (
                      viewportCssX < textBounds.left ||
                      viewportCssX >= textBounds.right ||
                      viewportCssY < textBounds.top ||
                      viewportCssY >= textBounds.bottom
                    )
                      return [];
                    const label =
                      element.tagName.toLowerCase() === "small"
                        ? `small[${textIndex}]`
                        : `span[${textIndex}]`;
                    overlapCounts.set(
                      label,
                      (overlapCounts.get(label) ?? 0) + 1,
                    );
                    return [label];
                  },
                );
                if (rawWaveformPixels.length < 128)
                  rawWaveformPixels.push({
                    x,
                    y,
                    viewportCss: {
                      x: round(viewportCssX),
                      y: round(viewportCssY),
                    },
                    ownedCropCss: {
                      x: round(ownedCropCssX),
                      y: round(ownedCropCssY),
                    },
                    ownedCropPixel: captureSize
                      ? {
                          x: Math.floor(
                            ownedCropCssX *
                              (captureSize.width / cropBounds.width),
                          ),
                          y: Math.floor(
                            ownedCropCssY *
                              (captureSize.height / cropBounds.height),
                          ),
                        }
                      : null,
                    overlappedText,
                  });
              }
            }
          }
          return {
            slot: index + 1,
            expectedChannel,
            found: true,
            bounds: rect(bounds),
            visibility: getComputedStyle(clip).visibility,
            opacity: getComputedStyle(clip).opacity,
            clipBackground: clipStyle.backgroundColor,
            clipPosition: clipStyle.position,
            clipZIndex: clipStyle.zIndex,
            bodyBackground: bodyStyle?.backgroundColor ?? null,
            bodyPosition: bodyStyle?.position ?? null,
            bodyZIndex: bodyStyle?.zIndex ?? null,
            bodyBounds:
              body === null ? null : rect(body.getBoundingClientRect()),
            textBoxes,
            rawWaveformSample: {
              rule: "the resolved waveform colour within the tolerance; x=clip bounds, y=clip bottom-28..bottom-3 CSS px",
              detectedPixelCount: detectedRawWaveformPixels,
              overlappedTextPixelCounts: Object.fromEntries(overlapCounts),
              pixels: rawWaveformPixels,
            },
            clipStackingChain: stackingFacts(clip),
            filmstripLayers: layerFactsAt(filmstripPoint.x, filmstripPoint.y),
            waveformLayers: layerFactsAt(waveformPoint.x, waveformPoint.y),
          };
        }),
      };
    },
    {
      expectedClips: clips,
      cropBounds: {
        x: ownedSurfaceBounds.x,
        y: ownedSurfaceBounds.y,
        width: ownedSurfaceBounds.width,
        height: ownedSurfaceBounds.height,
      },
      captureSize: ownedSurfaceCaptureSize ?? null,
      rgb: waveform.rgb,
      tolerance: WAVEFORM_COLOUR_TOLERANCE,
    },
  );
}

// M25-64: the rectangle the composited observer actually samples, in the screenshot's device
// pixels. `reason` is null only for a region that can decide the assertions made on it: a region
// with no usable width or rows, or with a waveform band smaller than the waveform minimum, is an
// observer failure, never a reading of zero pixels.
type CompositedSamplingRegion = Readonly<{
  reason: string | null;
  devicePixelRatio: number;
  screenshotScale: Readonly<{ x: number; y: number }>;
  clipCss: Readonly<{ x: number; y: number; width: number; height: number }>;
  device: Readonly<{
    left: number;
    right: number;
    filmstripTop: number;
    waveformTop: number;
    bottom: number;
  }>;
  usable: Readonly<{
    width: number;
    filmstripRows: number;
    waveformRows: number;
    filmstripArea: number;
    waveformArea: number;
  }>;
}>;

type CompositedClipDecorationPixels = Readonly<{
  clipId: string;
  filmstripSourcePixels: number;
  waveformColourPixels: number;
  // B-M2564-13: the waveform colour rule's matches in the filmstrip rows, which must be none.
  filmstripWaveformColourPixels: number;
  region: CompositedSamplingRegion;
}>;

async function readCompositedClipDecorationPixels(
  page: Page,
  overlay: Locator,
  screenshot: Buffer,
  clips: readonly Readonly<{
    clipId: string;
    expectedChannel: "red" | "green" | "blue";
  }>[],
  waveform: WaveformColour,
): Promise<readonly CompositedClipDecorationPixels[]> {
  const overlayBounds = await overlay.boundingBox();
  if (overlayBounds === null)
    throw new Error("owned workspace crop is not measurable");
  const clipBounds = await Promise.all(
    clips.map(async (clip) => {
      const bounds = await overlay
        .locator(`[data-h3-nle-clip="${clip.clipId}"]`)
        .boundingBox();
      if (bounds === null)
        throw new Error(
          `${clip.clipId} is absent from the owned workspace crop`,
        );
      return {
        ...clip,
        x: bounds.x - overlayBounds.x,
        y: bounds.y - overlayBounds.y,
        width: bounds.width,
        height: bounds.height,
      };
    }),
  );
  return page.evaluate(
    async ({
      pngBase64,
      cssWidth,
      cssHeight,
      clips: regions,
      minimumWaveformArea,
      rgb,
      tolerance,
    }) => {
      const image = new Image();
      image.src = `data:image/png;base64,${pngBase64}`;
      await image.decode();
      const bitmap = document.createElement("canvas");
      bitmap.width = image.naturalWidth;
      bitmap.height = image.naturalHeight;
      const context = bitmap.getContext("2d", { willReadFrequently: true });
      if (context === null) throw new Error("PNG pixel reader is unavailable");
      context.drawImage(image, 0, 0);
      const scaleX = bitmap.width / cssWidth;
      const scaleY = bitmap.height / cssHeight;
      return regions.map((clip) => {
        const left = Math.max(0, Math.floor((clip.x + 50) * scaleX));
        const right = Math.min(
          bitmap.width,
          Math.ceil((clip.x + clip.width - 50) * scaleX),
        );
        const filmstripTop = Math.max(0, Math.floor((clip.y + 7) * scaleY));
        const waveformTop = Math.max(
          0,
          Math.floor((clip.y + clip.height - 28) * scaleY),
        );
        const bottom = Math.min(
          bitmap.height,
          Math.ceil((clip.y + clip.height - 3) * scaleY),
        );
        const usable = {
          width: Math.max(0, right - left),
          filmstripRows: Math.max(0, waveformTop - filmstripTop),
          waveformRows: Math.max(0, bottom - waveformTop),
          filmstripArea:
            Math.max(0, right - left) * Math.max(0, waveformTop - filmstripTop),
          waveformArea:
            Math.max(0, right - left) * Math.max(0, bottom - waveformTop),
        };
        const region = {
          reason:
            usable.width < 1
              ? "no usable width"
              : usable.filmstripRows < 1
                ? "no filmstrip rows"
                : usable.waveformRows < 1
                  ? "no waveform rows"
                  : usable.waveformArea < minimumWaveformArea
                    ? "waveform band smaller than the waveform minimum"
                    : null,
          devicePixelRatio: window.devicePixelRatio,
          screenshotScale: { x: scaleX, y: scaleY },
          clipCss: {
            x: clip.x,
            y: clip.y,
            width: clip.width,
            height: clip.height,
          },
          device: { left, right, filmstripTop, waveformTop, bottom },
          usable,
        };
        if (region.reason !== null)
          return {
            clipId: clip.clipId,
            filmstripSourcePixels: 0,
            waveformColourPixels: 0,
            filmstripWaveformColourPixels: 0,
            region,
          };

        const filmstrip = context.getImageData(
          left,
          filmstripTop,
          right - left,
          waveformTop - filmstripTop,
        ).data;
        const waveform = context.getImageData(
          left,
          waveformTop,
          right - left,
          bottom - waveformTop,
        ).data;
        const channelIndex = { red: 0, green: 1, blue: 2 }[
          clip.expectedChannel
        ];
        const otherChannels = [0, 1, 2].filter(
          (index) => index !== channelIndex,
        );
        let filmstripSourcePixels = 0;
        for (let index = 0; index < filmstrip.length; index += 4) {
          const dominant = filmstrip[index + channelIndex]!;
          if (
            filmstrip[index + 3]! > 0 &&
            dominant >= 96 &&
            dominant > filmstrip[index + otherChannels[0]!]! * 1.45 &&
            dominant > filmstrip[index + otherChannels[1]!]! * 1.45
          )
            filmstripSourcePixels += 1;
        }
        const waveformColourPixels = (data: Uint8ClampedArray) => {
          let count = 0;
          for (let index = 0; index < data.length; index += 4)
            if (
              data[index + 3]! > 0 &&
              Math.abs(data[index]! - rgb[0]) <= tolerance &&
              Math.abs(data[index + 1]! - rgb[1]) <= tolerance &&
              Math.abs(data[index + 2]! - rgb[2]) <= tolerance
            )
              count += 1;
          return count;
        };
        return {
          clipId: clip.clipId,
          filmstripSourcePixels,
          waveformColourPixels: waveformColourPixels(waveform),
          filmstripWaveformColourPixels: waveformColourPixels(filmstrip),
          region,
        };
      });
    },
    {
      pngBase64: screenshot.toString("base64"),
      cssWidth: overlayBounds.width,
      cssHeight: overlayBounds.height,
      clips: clipBounds,
      minimumWaveformArea: MIN_COMPOSED_WAVEFORM_PIXELS,
      rgb: waveform.rgb,
      tolerance: WAVEFORM_COLOUR_TOLERANCE,
    },
  );
}

async function compareOwnedScreenshotPixels(
  page: Page,
  first: Buffer,
  second: Buffer,
): Promise<
  Readonly<{ width: number; height: number; changedPixelCount: number }>
> {
  return page.evaluate(
    async ({ firstBase64, secondBase64 }) => {
      const read = async (base64: string) => {
        const image = new Image();
        image.src = `data:image/png;base64,${base64}`;
        await image.decode();
        const canvas = document.createElement("canvas");
        canvas.width = image.naturalWidth;
        canvas.height = image.naturalHeight;
        const context = canvas.getContext("2d", { willReadFrequently: true });
        if (context === null)
          throw new Error(
            "owned-surface screenshot pixel reader is unavailable",
          );
        context.drawImage(image, 0, 0);
        return {
          width: canvas.width,
          height: canvas.height,
          pixels: context.getImageData(0, 0, canvas.width, canvas.height).data,
        };
      };
      const a = await read(firstBase64);
      const b = await read(secondBase64);
      if (a.width !== b.width || a.height !== b.height)
        throw new Error(
          "same-populated-state screenshots have different dimensions",
        );
      let changedPixelCount = 0;
      for (let offset = 0; offset < a.pixels.length; offset += 4) {
        if (
          a.pixels[offset] !== b.pixels[offset] ||
          a.pixels[offset + 1] !== b.pixels[offset + 1] ||
          a.pixels[offset + 2] !== b.pixels[offset + 2] ||
          a.pixels[offset + 3] !== b.pixels[offset + 3]
        )
          changedPixelCount += 1;
      }
      return { width: a.width, height: a.height, changedPixelCount };
    },
    {
      firstBase64: first.toString("base64"),
      secondBase64: second.toString("base64"),
    },
  );
}

// The driver runs Playwright inside a temporary output directory that is removed after the row,
// so an attachment alone does not survive as reviewable evidence. Owned-surface captures are
// therefore also written beside the private expectation, which `privateRepositoryPath` has
// already confined to the ignored item evidence tree; nothing outside it is ever written.
async function retainOwnedCapture(
  name: string,
  png: Buffer,
  bundleSha256: string,
  evidenceRunId: string,
): Promise<OwnedCapture> {
  const directory = resolve(
    dirname(privateRepositoryPath(EXPECTATION_ENV)),
    "..",
    "captures",
  );
  await mkdir(directory, { recursive: true });
  const dimensions = pngDimensions(png);
  const fileName =
    "host-" +
    name +
    "-" +
    bundleSha256.slice(0, 8) +
    "-" +
    evidenceRunId +
    "-" +
    dimensions.width +
    "x" +
    dimensions.height +
    ".png";
  await writeFile(resolve(directory, fileName), png);
  return { name: fileName, sha256: sha256(png) };
}

async function attachOwnedViewportCapture(
  testInfo: TestInfo,
  name: string,
  png: Buffer,
  bundleSha256: string,
  evidenceRunId: string,
): Promise<OwnedCapture> {
  const dimensions = pngDimensions(png);
  await testInfo.attach(
    "m25-53-host-" +
      name +
      "-viewport-1402x868-owned-" +
      dimensions.width +
      "x" +
      dimensions.height,
    { contentType: "image/png", body: png },
  );
  return retainOwnedCapture(name, png, bundleSha256, evidenceRunId);
}

async function retainDownloadedExport(
  bytes: Buffer,
  bundleSha256: string,
  evidenceRunId: string,
): Promise<OwnedCapture> {
  const directory = resolve(
    dirname(privateRepositoryPath(EXPECTATION_ENV)),
    "..",
    "captures",
  );
  await mkdir(directory, { recursive: true });
  const fileName = `host-export-${bundleSha256.slice(0, 8)}-${evidenceRunId}.mp4`;
  await writeFile(resolve(directory, fileName), bytes);
  return { name: fileName, sha256: sha256(bytes) };
}

async function retainRowEvidence(
  body: string,
  bundleSha256: string,
  evidenceRunId: string,
): Promise<void> {
  const directory = resolve(
    dirname(privateRepositoryPath(EXPECTATION_ENV)),
    "..",
    "captures",
  );
  await mkdir(directory, { recursive: true });
  await writeFile(
    resolve(
      directory,
      `host-evidence-${bundleSha256.slice(0, 8)}-${evidenceRunId}.json`,
    ),
    body,
  );
}

// M25-64: content-free observer evidence (widths, counts, pointer mode, sampling rectangles and
// the path each step took), rewritten after every observation so a failed row keeps what led
// to it.
async function retainObserverEvidence(
  body: string,
  bundleSha256: string,
  evidenceRunId: string,
): Promise<void> {
  const directory = resolve(
    dirname(privateRepositoryPath(EXPECTATION_ENV)),
    "..",
    "captures",
  );
  await mkdir(directory, { recursive: true });
  await writeFile(
    resolve(
      directory,
      `host-observer-evidence-${bundleSha256.slice(0, 8)}-${evidenceRunId}.json`,
    ),
    body,
  );
}

async function retainDecorationFailureDiagnostic(
  body: string,
  bundleSha256: string,
  evidenceRunId: string,
): Promise<string> {
  const directory = resolve(
    dirname(privateRepositoryPath(EXPECTATION_ENV)),
    "..",
    "captures",
  );
  await mkdir(directory, { recursive: true });
  const fileName =
    `host-populated-decoration-failure-${bundleSha256.slice(0, 8)}-` +
    `${evidenceRunId}.json`;
  await writeFile(resolve(directory, fileName), body);
  return fileName;
}

async function qualifiedObserver(
  environmentName: string,
  expectedSha256: string,
): Promise<string> {
  const configured = process.env[environmentName]?.trim() ?? "";
  if (configured.length === 0)
    throw new Error(`${environmentName} is required`);
  const path = resolve(configured);
  if (
    !(await stat(path)).isFile() ||
    sha256(await readFile(path)) !== expectedSha256
  )
    throw new Error(`${environmentName} does not match the pinned observer`);
  return path;
}

async function postAction(page: Page, body: unknown): Promise<HostResponse> {
  return page.evaluate(async (requestBody) => {
    const response = await fetch("/h3-context/v1/authoring/action", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(requestBody),
    });
    const text = await response.text();
    return {
      status: response.status,
      body: text.length === 0 ? null : (JSON.parse(text) as unknown),
    };
  }, body);
}

function authoringAction(request: Request): string | null {
  const path = new URL(request.url()).pathname.replace(
    /^\/api(?=\/h3-context\/)/,
    "",
  );
  if (request.method() !== "POST" || path !== AUTHORING_ROUTE) return null;
  return String(request.postDataJSON()?.action ?? "");
}

async function openTrackMenu(page: Page, trackId: string) {
  await page
    .locator(
      `[data-h3-nle-track="${trackId}"] [data-h3-nle-menu-trigger="track"]`,
    )
    .click();
  const menu = page.getByRole("menu", { name: "Track menu" });
  await expect(menu).toBeVisible();
  return menu;
}

async function seek(page: Page, slider: Locator, frame: number) {
  // A source insertion briefly rebinds the monitor. Keys sent while the visible ruler is
  // disabled are correctly ignored; wait for the actual enabled/settled state, not a timer.
  const ready = () =>
    expect(slider).toHaveAttribute("aria-disabled", "false", {
      timeout: 60_000,
    });
  const settled = () =>
    expect(slider).toHaveAttribute(
      "data-h3-nle-request-clear-reason",
      "monitor_request_settled",
      { timeout: 60_000 },
    );
  await ready();
  await slider.focus();
  await page.keyboard.press("Home");
  await expect(slider).toHaveAttribute("aria-valuenow", "0");
  await settled();
  expect(
    await slider.evaluate((element) => document.activeElement === element),
    "playhead lost keyboard focus after Home",
  ).toBe(true);
  const seconds = Math.floor(frame / 24);
  for (let index = 0; index < seconds; index += 1) {
    await ready();
    await page.keyboard.press("PageUp");
    expect(
      await slider.evaluate((element) => document.activeElement === element),
      `playhead lost keyboard focus after PageUp ${index + 1}`,
    ).toBe(true);
    await expect(slider).toHaveAttribute(
      "aria-valuenow",
      String((index + 1) * 24),
    );
    await settled();
  }
  for (let index = seconds * 24; index < frame; index += 1) {
    await ready();
    await page.keyboard.press("ArrowRight");
    await expect(slider).toHaveAttribute("aria-valuenow", String(index + 1));
    await settled();
  }
  await expect(slider).toHaveAttribute("aria-valuenow", String(frame));
}

function rgbDistance(left: Rgb, right: Rgb): number {
  return Math.hypot(
    left.red - right.red,
    left.green - right.green,
    left.blue - right.blue,
  );
}

type WireClipPlacement = Readonly<{
  crop: Readonly<Record<string, unknown>>;
  transform: Readonly<Record<string, unknown>>;
}>;

type ClipPlacement = Readonly<{
  cropLeft: number;
  cropTop: number;
  cropWidth: number;
  cropHeight: number;
  scaledWidth: number;
  scaledHeight: number;
  angle: number;
  centerX: number;
  centerY: number;
}>;

type OutputRegion = Readonly<{
  left: number;
  top: number;
  width: number;
  height: number;
}>;

const BASIS_POINTS = 10_000;

// Transcribed from the accepted composition contract exactly as the frozen conformance policy
// does (`_placement` in `semantic_conformance_expect.py`): crop in source pixels, scale to the
// layer size, rotate, then place the anchor at the output position. An identity transform puts
// a source at its native size centred on the output; it is never stretched to fill the frame.
// This is deliberately a second copy of the render graph's arithmetic so the observer never asks
// the renderer where it put a layer.
function clipPlacement(
  clip: WireClipPlacement,
  sourceWidth: number,
  sourceHeight: number,
  outputWidth: number,
  outputHeight: number,
): ClipPlacement {
  const crop = (key: string) => Number(clip.crop[key]);
  const transform = (key: string) => Number(clip.transform[key]);
  const left = Math.floor((sourceWidth * crop("left_bp")) / BASIS_POINTS);
  const top = Math.floor((sourceHeight * crop("top_bp")) / BASIS_POINTS);
  const right = Math.floor(
    (sourceWidth * (BASIS_POINTS - crop("right_bp")) + BASIS_POINTS - 1) /
      BASIS_POINTS,
  );
  const bottom = Math.floor(
    (sourceHeight * (BASIS_POINTS - crop("bottom_bp")) + BASIS_POINTS - 1) /
      BASIS_POINTS,
  );
  const cropWidth = right - left;
  const cropHeight = bottom - top;
  const scaledWidth = Math.max(
    1,
    Math.floor((cropWidth * transform("scale_x_bp") + 5_000) / BASIS_POINTS),
  );
  const scaledHeight = Math.max(
    1,
    Math.floor((cropHeight * transform("scale_y_bp") + 5_000) / BASIS_POINTS),
  );
  const angle = (transform("rotation_mdeg") / 1_000) * (Math.PI / 180);
  const offsetX = scaledWidth * (transform("anchor_x_bp") / BASIS_POINTS - 0.5);
  const offsetY =
    scaledHeight * (transform("anchor_y_bp") / BASIS_POINTS - 0.5);
  const anchorX = offsetX * Math.cos(angle) - offsetY * Math.sin(angle);
  const anchorY = offsetX * Math.sin(angle) + offsetY * Math.cos(angle);
  const centerX =
    outputWidth / 2 +
    (outputWidth * transform("position_x_bp")) / BASIS_POINTS -
    anchorX;
  const centerY =
    outputHeight / 2 +
    (outputHeight * transform("position_y_bp")) / BASIS_POINTS -
    anchorY;
  const placement = {
    cropLeft: left,
    cropTop: top,
    cropWidth,
    cropHeight,
    scaledWidth,
    scaledHeight,
    angle,
    centerX,
    centerY,
  };
  if (Object.values(placement).some((value) => !Number.isFinite(value)))
    throw new Error("clip placement is malformed");
  return placement;
}

function toOutputPoint(
  placement: ClipPlacement,
  sourceX: number,
  sourceY: number,
): Readonly<{ x: number; y: number }> {
  const scaledX =
    ((sourceX - placement.cropLeft + 0.5) * placement.scaledWidth) /
      placement.cropWidth -
    placement.scaledWidth / 2;
  const scaledY =
    ((sourceY - placement.cropTop + 0.5) * placement.scaledHeight) /
      placement.cropHeight -
    placement.scaledHeight / 2;
  const cosine = Math.cos(placement.angle);
  const sine = Math.sin(placement.angle);
  return {
    x: placement.centerX + scaledX * cosine - scaledY * sine,
    y: placement.centerY + scaledX * sine + scaledY * cosine,
  };
}

function toSourcePoint(
  placement: ClipPlacement,
  outputX: number,
  outputY: number,
): Readonly<{ x: number; y: number }> {
  const dx = outputX - placement.centerX;
  const dy = outputY - placement.centerY;
  const cosine = Math.cos(placement.angle);
  const sine = Math.sin(placement.angle);
  const scaledX = dx * cosine + dy * sine;
  const scaledY = -dx * sine + dy * cosine;
  return {
    x:
      placement.cropLeft +
      ((scaledX + placement.scaledWidth / 2) * placement.cropWidth) /
        placement.scaledWidth -
      0.5,
    y:
      placement.cropTop +
      ((scaledY + placement.scaledHeight / 2) * placement.cropHeight) /
        placement.scaledHeight -
      0.5,
  };
}

/** The axis-aligned output rectangle an unrotated placed layer occupies. */
function placedRegion(placement: ClipPlacement): OutputRegion {
  if (placement.angle !== 0)
    throw new Error("a rotated layer has no axis-aligned placed region");
  return {
    left: Math.round(placement.centerX - placement.scaledWidth / 2),
    top: Math.round(placement.centerY - placement.scaledHeight / 2),
    width: placement.scaledWidth,
    height: placement.scaledHeight,
  };
}

function regionFilter(region: OutputRegion | undefined): string {
  return region === undefined
    ? ""
    : `crop=${region.width}:${region.height}:${region.left}:${region.top},`;
}

function meanRgb(
  ffmpeg: string,
  path: string,
  frame: number,
  region?: OutputRegion,
): Rgb {
  const bytes = execFileSync(
    ffmpeg,
    [
      "-v",
      "error",
      "-i",
      path,
      "-vf",
      `select=eq(n\\,${frame}),${regionFilter(region)}scale=1:1:flags=area,format=rgb24`,
      "-frames:v",
      "1",
      "-f",
      "rawvideo",
      "-",
    ],
    { encoding: "buffer", timeout: 120_000, maxBuffer: 1024 * 1024 },
  );
  if (bytes.length !== 3) throw new Error("observer emitted no RGB sample");
  return { red: bytes[0]!, green: bytes[1]!, blue: bytes[2]! };
}

function interiorRgb(
  ffmpeg: string,
  path: string,
  frame: number,
  centerX: number,
  centerY: number,
  edge: number,
): Rgb {
  const left = Math.round(centerX - edge / 2);
  const top = Math.round(centerY - edge / 2);
  const bytes = execFileSync(
    ffmpeg,
    [
      "-v",
      "error",
      "-i",
      path,
      "-vf",
      `select=eq(n\\,${frame}),crop=${edge}:${edge}:${left}:${top},scale=1:1:flags=area,format=rgb24`,
      "-frames:v",
      "1",
      "-f",
      "rawvideo",
      "-",
    ],
    { encoding: "buffer", timeout: 120_000, maxBuffer: 1024 * 1024 },
  );
  if (bytes.length !== 3)
    throw new Error("observer emitted no interior sample");
  return { red: bytes[0]!, green: bytes[1]!, blue: bytes[2]! };
}

function frameIndexPatch(
  ffmpeg: string,
  path: string,
  frame: number,
  region?: OutputRegion,
): Buffer {
  const bytes = execFileSync(
    ffmpeg,
    [
      "-v",
      "error",
      "-i",
      path,
      "-vf",
      `select=eq(n\\,${frame}),${regionFilter(region)}scale=640:360:flags=bicubic,crop=42:30:12:296,format=rgb24`,
      "-frames:v",
      "1",
      "-f",
      "rawvideo",
      "-",
    ],
    { encoding: "buffer", timeout: 120_000, maxBuffer: 1024 * 1024 },
  );
  if (bytes.length !== 42 * 30 * 3)
    throw new Error("observer emitted no frame-index patch");
  return bytes;
}

function meanAbsoluteByteError(left: Buffer, right: Buffer): number {
  if (left.length !== right.length) throw new Error("patch sizes differ");
  let sum = 0;
  for (let index = 0; index < left.length; index += 1)
    sum += Math.abs(left[index]! - right[index]!);
  return sum / left.length;
}

function maximumChannelError(left: Rgb, right: Rgb): number {
  return Math.max(
    Math.abs(left.red - right.red),
    Math.abs(left.green - right.green),
    Math.abs(left.blue - right.blue),
  );
}

function whiteInteriorPixels(
  ffmpeg: string,
  path: string,
  frame: number,
  region: OutputRegion,
): number {
  const { left, top, width: cropWidth, height: cropHeight } = region;
  const bytes = execFileSync(
    ffmpeg,
    [
      "-v",
      "error",
      "-i",
      path,
      "-vf",
      // The band is cropped after the conversion to RGB: cropping the decoded 4:2:0 frame
      // first lets ffmpeg align an odd geometry to the chroma grid (579 px wide came back as
      // 576), and the pixel count then never matches the requested band.
      `select=eq(n\\,${frame}),format=rgb24,crop=${cropWidth}:${cropHeight}:${left}:${top}`,
      "-frames:v",
      "1",
      "-f",
      "rawvideo",
      "-",
    ],
    { encoding: "buffer", timeout: 120_000, maxBuffer: 2 * 1024 * 1024 },
  );
  if (bytes.length !== cropWidth * cropHeight * 3)
    throw new Error(
      `observer emitted incomplete text band: ${bytes.length} bytes for crop ${cropWidth}:${cropHeight}:${left}:${top} at frame ${frame}`,
    );
  let count = 0;
  for (let offset = 0; offset < bytes.length; offset += 3)
    if (Math.min(bytes[offset]!, bytes[offset + 1]!, bytes[offset + 2]!) >= 200)
      count += 1;
  return count;
}

function observedMixAlpha(base: Rgb, overlay: Rgb, mixed: Rgb): number {
  const delta = [
    overlay.red - base.red,
    overlay.green - base.green,
    overlay.blue - base.blue,
  ];
  const observed = [
    mixed.red - base.red,
    mixed.green - base.green,
    mixed.blue - base.blue,
  ];
  const denominator = delta.reduce((sum, value) => sum + value * value, 0);
  if (denominator < 100)
    throw new Error("transition endpoints are indistinguishable");
  return (
    observed.reduce((sum, value, index) => sum + value * delta[index]!, 0) /
    denominator
  );
}

function audioWindow(ffmpeg: string, path: string, frame: number): Int16Array {
  const bytes = execFileSync(
    ffmpeg,
    [
      "-v",
      "error",
      "-ss",
      String(frame / 24),
      "-i",
      path,
      "-t",
      "0.16",
      "-vn",
      "-ac",
      "1",
      "-ar",
      "8000",
      "-f",
      "s16le",
      "-acodec",
      "pcm_s16le",
      "-",
    ],
    { encoding: "buffer", timeout: 120_000, maxBuffer: 1024 * 1024 },
  );
  if (bytes.length < 1600 || bytes.length % 2 !== 0)
    throw new Error("observer emitted no complete audio window");
  const samples = new Int16Array(bytes.length / 2);
  for (let index = 0; index < samples.length; index += 1)
    samples[index] = bytes.readInt16LE(index * 2);
  return samples;
}

// Bound the independent PCM decode from the independently authored content extent, plus one
// codec-padding second. An output extended to edit capacity still fails closed.
const OUTPUT_PCM16_BYTE_LIMIT =
  (EXPECTED_OUTPUT_DURATION_FRAMES / 24 + 1) * 48_000 * 2;

function decodedAudioSamples48k(ffmpeg: string, path: string): Int16Array {
  const bytes = execFileSync(
    ffmpeg,
    [
      "-v",
      "error",
      "-i",
      path,
      "-vn",
      "-ac",
      "1",
      "-ar",
      "48000",
      "-f",
      "s16le",
      "-acodec",
      "pcm_s16le",
      "-",
    ],
    {
      encoding: "buffer",
      timeout: 120_000,
      maxBuffer: OUTPUT_PCM16_BYTE_LIMIT,
    },
  );
  if (bytes.length % 2 !== 0) throw new Error("observer audio is not PCM16");
  const samples = new Int16Array(bytes.length / 2);
  for (let index = 0; index < samples.length; index += 1)
    samples[index] = bytes.readInt16LE(index * 2);
  return samples;
}

function audioOnsetSample(samples: Int16Array, start: number): number {
  const window = 480;
  // Search after the cut's codec boundary, but before the fixture's midpoint tone.
  for (let offset = 4_000; offset < 24_000 - window; offset += 240) {
    let energy = 0;
    for (let index = 0; index < window; index += 1)
      energy += (samples[start + offset + index]! / 32768) ** 2;
    if (Math.sqrt(energy / window) > 0.005) return start + offset;
  }
  throw new Error("expected fixture audio onset was not observed");
}

function pcmPeakInRange(
  samples: Int16Array,
  start: number,
  end: number,
): number {
  if (start < 0 || end > samples.length || start >= end)
    throw new Error("invalid audio silence interior");
  let peak = 0;
  for (let index = start; index < end; index += 1)
    peak = Math.max(peak, Math.abs(samples[index]!));
  return peak;
}

function audioRms(samples: Int16Array): number {
  return Math.sqrt(
    samples.reduce((sum, value) => sum + (value / 32768) ** 2, 0) /
      samples.length,
  );
}

function audioPeak(samples: Int16Array): number {
  return samples.reduce(
    (maximum, value) => Math.max(maximum, Math.abs(value)),
    0,
  );
}

function tonePower(samples: Int16Array, hertz: number): number {
  let real = 0;
  let imaginary = 0;
  for (let index = 0; index < samples.length; index += 1) {
    const angle = (2 * Math.PI * hertz * index) / 8000;
    real += samples[index]! * Math.cos(angle);
    imaginary += samples[index]! * Math.sin(angle);
  }
  return Math.hypot(real, imaginary) / samples.length;
}

function probe(ffprobe: string, path: string) {
  return JSON.parse(
    execFileSync(
      ffprobe,
      [
        "-v",
        "error",
        "-count_frames",
        "-show_entries",
        "stream=codec_type,width,height,nb_read_frames,r_frame_rate,sample_rate,channels,duration,time_base,duration_ts:format=duration",
        "-of",
        "json",
        path,
      ],
      { encoding: "utf8", timeout: 120_000 },
    ),
  ) as {
    streams: Array<Record<string, unknown>>;
    format: Record<string, unknown>;
  };
}

test("M25-53 reference fidelity editing through verified export", async ({
  page,
  context,
}, testInfo) => {
  test.skip(
    process.env.H3_CONTEXT_M25_53_HOST !== "1",
    "explicit M25-53 supplied-host row required",
  );
  test.setTimeout(30 * 60_000);
  const installedInventorySha256 = process.env[INVENTORY_ENV];
  if (
    !hostUrl ||
    candidateBundle === null ||
    candidateBackendMode !== "exact" ||
    candidateBackendRuntime === null ||
    installedInventorySha256 === undefined ||
    !/^[0-9a-f]{64}$/.test(installedInventorySha256)
  )
    throw new Error("M25-53 requires an exact supplied-host candidate");

  const workspaceFixture = await loadWorkspaceFixture();
  const { expectation, sourcePaths } = await loadExpectation();
  const evidenceRunId = new Date().toISOString().replace(/[^0-9TZ]/g, "");
  const ffmpeg = await qualifiedObserver(FFMPEG_ENV, expectation.ffmpegSha256);
  const ffprobe = await qualifiedObserver(
    FFPROBE_ENV,
    expectation.ffprobeSha256,
  );
  const allowedOrigin = new URL(hostUrl).origin;
  const network = monitorH3Network(page, allowedOrigin);
  const candidateNetwork = await monitorCandidateInitiatorNetwork(
    context,
    page,
    allowedOrigin,
  );
  let transactionRequests = 0;
  let promptRequests = 0;
  const decorationLeaseResponses: Array<
    Readonly<{
      kind: "thumbnail" | "filmstrip" | "audio_peaks" | null;
      derivativeKind:
        | "thumbnail"
        | "filmstrip"
        | "audio_peaks"
        | "video_proxy"
        | "image_proxy"
        | "packaged_font_face"
        | null;
      operation: "create" | "open" | "release" | "unknown";
      status: number;
      reason: string | null;
      ownerAlias: string | null;
    }>
  > = [];
  const decorationResponseTasks: Promise<void>[] = [];
  const leaseEvents: Array<Record<string, unknown>> = [];
  const leaseOwners = new Map<
    string,
    Readonly<{ alias: string; derivativeKind: string | null }>
  >();
  const leaseRequestFacts = new WeakMap<
    Request,
    Readonly<{
      operation: "create" | "open" | "release" | "unknown";
      derivativeKind: string | null;
      decorationKind: "thumbnail" | "filmstrip" | "audio_peaks" | null;
      ownerAlias: string | null;
    }>
  >();
  let nextOwnerAlias = 0;
  let nextLeaseEventSequence = 0;
  const recordLeaseEvent = (
    request: Request,
    event: string,
    details: Readonly<Record<string, unknown>> = {},
  ) => {
    const facts = leaseRequestFacts.get(request);
    if (facts === undefined) return;
    leaseEvents.push({
      sequence: ++nextLeaseEventSequence,
      epochMs: Date.now(),
      monotonicMs: Number(performance.now().toFixed(3)),
      event,
      ...facts,
      ...details,
    });
  };
  const requestLeaseFacts = (request: Request) => {
    const url = new URL(request.url());
    const isCreateOrRelease = url.pathname.endsWith(
      "/authoring/media-source-leases",
    );
    const isOpen = url.pathname.endsWith("/authoring/media-source-leases/open");
    if ((!isCreateOrRelease && !isOpen) || request.method() !== "POST") return;
    let payload: Record<string, unknown> = {};
    try {
      payload = request.postDataJSON() as Record<string, unknown>;
    } catch {
      // Retain only content-free operation, derivative and owner-alias facts.
    }
    const rawDerivativeKind = payload.derivativeKind;
    const derivativeKind =
      rawDerivativeKind === "thumbnail" ||
      rawDerivativeKind === "filmstrip" ||
      rawDerivativeKind === "audio_peaks" ||
      rawDerivativeKind === "video_proxy" ||
      rawDerivativeKind === "image_proxy" ||
      rawDerivativeKind === "packaged_font_face"
        ? rawDerivativeKind
        : null;
    const operationValue = isOpen ? "open" : payload.operation;
    const operation =
      operationValue === "create" ||
      operationValue === "open" ||
      operationValue === "release"
        ? operationValue
        : "unknown";
    const rawOwnerId = payload.ownerId;
    let ownerAlias: string | null = null;
    let ownerDerivativeKind: string | null = null;
    if (typeof rawOwnerId === "string") {
      let owner = leaseOwners.get(rawOwnerId);
      if (owner === undefined) {
        owner = Object.freeze({
          alias: `owner-${++nextOwnerAlias}`,
          derivativeKind,
        });
        leaseOwners.set(rawOwnerId, owner);
      }
      ownerAlias = owner.alias;
      ownerDerivativeKind = owner.derivativeKind;
    }
    const effectiveDerivativeKind = derivativeKind ?? ownerDerivativeKind;
    const decorationKind =
      effectiveDerivativeKind === "thumbnail" ||
      effectiveDerivativeKind === "filmstrip" ||
      effectiveDerivativeKind === "audio_peaks"
        ? effectiveDerivativeKind
        : null;
    leaseRequestFacts.set(
      request,
      Object.freeze({
        operation,
        derivativeKind: effectiveDerivativeKind,
        decorationKind,
        ownerAlias,
      }),
    );
  };
  const onRequest = (request: Request) => {
    const action = authoringAction(request);
    if (action === "apply_timeline_transaction") transactionRequests += 1;
    if (/\/(?:api\/)?prompt$/.test(new URL(request.url()).pathname))
      promptRequests += 1;
    requestLeaseFacts(request);
    recordLeaseEvent(request, "request");
  };
  page.on("request", onRequest);
  page.on("response", (response) => {
    const url = new URL(response.url());
    const isCreateOrRelease = url.pathname.endsWith(
      "/authoring/media-source-leases",
    );
    const isOpen = url.pathname.endsWith("/authoring/media-source-leases/open");
    if (!isCreateOrRelease && !isOpen) return;
    const request = response.request();
    if (request.method() !== "POST") return;
    const facts = leaseRequestFacts.get(request);
    if (facts === undefined) return;
    recordLeaseEvent(request, "response", {
      status: response.status(),
    });
    decorationResponseTasks.push(
      (async () => {
        let reason: string | null = null;
        if (!response.ok()) {
          try {
            const body = (await response.json()) as { reason?: unknown };
            const reasons = [
              "invalid_request",
              "authority_mismatch",
              "stale",
              "lease_gone",
              "unsupported",
              "resource_limit",
              "busy",
              "cancelled",
              "timeout",
              "generation_failed",
              "internal_failure",
            ];
            if (
              typeof body.reason === "string" &&
              reasons.includes(body.reason)
            )
              reason = body.reason;
          } catch {
            // Keep only the closed error reason; never retain response bodies or identifiers.
          }
        }
        decorationLeaseResponses.push({
          kind: facts.decorationKind,
          derivativeKind: facts.derivativeKind as
            | "thumbnail"
            | "filmstrip"
            | "audio_peaks"
            | "video_proxy"
            | "image_proxy"
            | "packaged_font_face"
            | null,
          operation: facts.operation,
          status: response.status(),
          reason,
          ownerAlias: facts.ownerAlias,
        });
        if (reason !== null)
          recordLeaseEvent(request, "response.reason", { reason });
      })(),
    );
  });
  page.on("requestfinished", (request) =>
    recordLeaseEvent(request, "requestfinished"),
  );
  page.on("requestfailed", (request) => {
    const rawFailure = request.failure()?.errorText ?? "unknown";
    const failure =
      rawFailure === "net::ERR_ABORTED" ||
      rawFailure === "net::ERR_FAILED" ||
      rawFailure === "net::ERR_CONNECTION_RESET" ||
      rawFailure === "net::ERR_TIMED_OUT"
        ? rawFailure
        : "other";
    const abortCause =
      failure === "net::ERR_ABORTED" ? "request_abort" : "request_failed";
    recordLeaseEvent(request, "requestfailed", {
      failure,
      abortCause,
    });
  });

  const injectionBefore = candidateInjectionCount(context);
  const schedulerTraceEnabled =
    process.env.H3_CONTEXT_M25_53_SCHEDULER_TRACE === "1";
  const destination = new URL(hostUrl);
  if (schedulerTraceEnabled)
    destination.searchParams.set("h3NleSchedulerTrace", "1");
  await page.addInitScript((enableNleTrace: boolean) => {
    const target = window as Window & {
      __h3NleSchedulerTraceEnabled?: boolean;
      __h3NleWaveformDrawTrace?: Array<Readonly<Record<string, unknown>>>;
      __h3NleWaveformCacheTrace?: Array<Readonly<Record<string, unknown>>>;
      __h3NleCanvasStrokeProbe?: {
        calls: number;
        timelineCanvasCalls: number;
        unmatchedExamples: Array<Readonly<Record<string, unknown>>>;
      };
    };
    if (enableNleTrace) target.__h3NleSchedulerTraceEnabled = true;
    const paths = new WeakMap<CanvasRenderingContext2D, number[][]>();
    const peakCaches = new WeakMap<
      Map<unknown, unknown>,
      Readonly<{
        aliases: Map<string, string>;
        events: Array<Readonly<Record<string, unknown>>>;
      }>
    >();
    const envelopeAliases = new WeakMap<object, string>();
    const canvasContext = CanvasRenderingContext2D.prototype;
    const mapGet = Map.prototype.get;
    const mapSet = Map.prototype.set;
    const beginPath = canvasContext.beginPath;
    const moveTo = canvasContext.moveTo;
    const lineTo = canvasContext.lineTo;
    const stroke = canvasContext.stroke as (
      this: CanvasRenderingContext2D,
      path?: Path2D,
    ) => void;
    const isTimelineRaster = (context: CanvasRenderingContext2D) =>
      context.canvas.getAttribute("data-h3-nle-canvas") ===
      "timeline_decoration";
    const peakCacheKey = (key: unknown) => {
      if (typeof key !== "string") return null;
      try {
        const parts = JSON.parse(key);
        return Array.isArray(parts) && parts[3] === "audio_peaks" ? key : null;
      } catch {
        return null;
      }
    };
    const keyAlias = (
      metadata: {
        aliases: Map<string, string>;
        events: Array<Readonly<Record<string, unknown>>>;
      },
      key: string,
    ) => {
      let alias = metadata.aliases.get(key);
      if (alias === undefined) {
        alias = `asset-${metadata.aliases.size + 1}`;
        metadata.aliases.set(key, alias);
      }
      return alias;
    };
    Map.prototype.get = function <K, V>(
      this: Map<K, V>,
      key: K,
    ): V | undefined {
      const value = mapGet.call(this, key);
      const envelopeAlias =
        typeof value === "object" && value !== null
          ? envelopeAliases.get(value)
          : undefined;
      if (envelopeAlias !== undefined) {
        const cacheTrace = (target.__h3NleWaveformCacheTrace ??= []);
        cacheTrace.push({
          event: "timeline.map.get",
          assetAlias: envelopeAlias,
          hit: true,
          mapSize: this.size,
        });
        if (cacheTrace.length > 512)
          cacheTrace.splice(0, cacheTrace.length - 512);
      }
      const metadata = peakCaches.get(this as unknown as Map<unknown, unknown>);
      const encoded = peakCacheKey(key);
      if (metadata !== undefined && encoded !== null) {
        metadata.events.push({
          event: "get",
          keyAlias: keyAlias(metadata, encoded),
          hit:
            typeof value === "object" &&
            value !== null &&
            "value" in value &&
            (value as { value?: unknown }).value !== undefined,
          cacheSize: this.size,
        });
        const cacheTrace = (target.__h3NleWaveformCacheTrace ??= []);
        cacheTrace.push(metadata.events.at(-1)!);
        if (cacheTrace.length > 512)
          cacheTrace.splice(0, cacheTrace.length - 512);
        if (metadata.events.length > 512)
          metadata.events.splice(0, metadata.events.length - 512);
      }
      return value;
    };
    Map.prototype.set = function <K, V>(
      this: Map<K, V>,
      key: K,
      value: V,
    ): Map<K, V> {
      const candidate = value as unknown as {
        key?: unknown;
        value?: {
          pairs?: unknown;
          pairCount?: unknown;
          pairRate?: unknown;
          sampleCount?: unknown;
        };
      };
      const pairs = candidate?.value?.pairs;
      const encoded = peakCacheKey(key);
      if (
        encoded !== null &&
        pairs instanceof Int8Array &&
        typeof candidate.value?.pairCount === "number" &&
        typeof candidate.value.sampleCount === "number"
      ) {
        let metadata = peakCaches.get(this as unknown as Map<unknown, unknown>);
        if (metadata === undefined) {
          metadata = { aliases: new Map(), events: [] };
          peakCaches.set(this as unknown as Map<unknown, unknown>, metadata);
        }
        let min = 127;
        let max = -128;
        let nonZeroPairs = 0;
        let maxAbs = 0;
        for (let index = 0; index < pairs.length; index += 2) {
          const low = pairs[index]!;
          const high = pairs[index + 1]!;
          min = Math.min(min, low);
          max = Math.max(max, high);
          if (low !== 0 || high !== 0) nonZeroPairs += 1;
          maxAbs = Math.max(maxAbs, Math.abs(low), Math.abs(high));
        }
        const result = mapSet.call(this, key, value);
        const alias = keyAlias(metadata, encoded);
        if (typeof candidate.value === "object" && candidate.value !== null)
          envelopeAliases.set(candidate.value as object, alias);
        metadata.events.push({
          event: "set",
          keyAlias: alias,
          pairCount: candidate.value.pairCount,
          pairRate: candidate.value.pairRate,
          sampleCount: candidate.value.sampleCount,
          nonZeroPairs,
          min,
          max,
          maxAbs,
          cacheSize: this.size,
        });
        const cacheTrace = (target.__h3NleWaveformCacheTrace ??= []);
        cacheTrace.push(metadata.events.at(-1)!);
        if (cacheTrace.length > 512)
          cacheTrace.splice(0, cacheTrace.length - 512);
        if (metadata.events.length > 512)
          metadata.events.splice(0, metadata.events.length - 512);
        return result;
      }
      return mapSet.call(this, key, value);
    };
    canvasContext.beginPath = function () {
      if (isTimelineRaster(this)) paths.set(this, []);
      return beginPath.call(this);
    };
    canvasContext.moveTo = function (x, y) {
      if (isTimelineRaster(this)) paths.get(this)?.push([x, y]);
      return moveTo.call(this, x, y);
    };
    canvasContext.lineTo = function (x, y) {
      if (isTimelineRaster(this)) paths.get(this)?.push([x, y]);
      return lineTo.call(this, x, y);
    };
    canvasContext.stroke = function (path?: Path2D) {
      // Preserve the native overload: forwarding an omitted argument as `undefined`
      // makes CanvasRenderingContext2D.stroke reject it as a non-Path2D value.
      const hasPathArgument = arguments.length > 0;
      const invokeStroke = () =>
        hasPathArgument ? stroke.call(this, path) : stroke.call(this);
      const probe = (target.__h3NleCanvasStrokeProbe ??= {
        calls: 0,
        timelineCanvasCalls: 0,
        unmatchedExamples: [],
      });
      probe.calls += 1;
      const canvasMarker = this.canvas.getAttribute("data-h3-nle-canvas");
      if (canvasMarker === "timeline_decoration") {
        probe.timelineCanvasCalls += 1;
      } else if (probe.unmatchedExamples.length < 8) {
        probe.unmatchedExamples.push({
          hasCanvasMarker: canvasMarker !== null,
          canvasWidth: this.canvas.width,
          canvasHeight: this.canvas.height,
        });
      }
      if (isTimelineRaster(this)) {
        // M25-62: the waveform stroke is the resolved `--h3-nle-waveform` role token rather than
        // a fixed rgba literal. Resolve it where the canvas sits and normalise it through a
        // scratch context, the form the canvas stores a style in.
        const waveformToken = getComputedStyle(this.canvas)
          .getPropertyValue("--h3-nle-waveform")
          .trim();
        const normaliser = document.createElement("canvas").getContext("2d");
        if (normaliser !== null) normaliser.strokeStyle = waveformToken;
        const waveformStroke =
          waveformToken !== "" &&
          normaliser !== null &&
          String(this.strokeStyle) === String(normaliser.strokeStyle);
        // M25-64 B-M2564-13: the stroke's own colour, for the immediate pixel count below.
        const waveformHex = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(
          normaliser === null ? "" : String(normaliser.strokeStyle),
        );
        const waveformRgb =
          waveformHex === null
            ? null
            : [1, 2, 3].map((group) =>
                Number.parseInt(waveformHex[group]!, 16),
              );
        const points = paths.get(this) ?? [];
        const x = points.map(([px]) => px!);
        const y = points.map(([, py]) => py!);
        const xBands = [
          points.filter(([px]) => px! < 238).length,
          points.filter(([px]) => px! >= 238 && px! < 476).length,
          points.filter(([px]) => px! >= 476).length,
        ];
        const trace = (target.__h3NleWaveformDrawTrace ??= []);
        const strokeTrace: Record<string, unknown> = {
          sequence: trace.length + 1,
          monotonicMs: Number(performance.now().toFixed(3)),
          strokeStyle: String(this.strokeStyle),
          isWaveformStyle: waveformStroke,
          lineWidth: this.lineWidth,
          globalAlpha: this.globalAlpha,
          globalCompositeOperation: this.globalCompositeOperation,
          pointCount: points.length,
          xBands,
          minX: x.length > 0 ? Math.min(...x) : null,
          maxX: x.length > 0 ? Math.max(...x) : null,
          minY: y.length > 0 ? Math.min(...y) : null,
          maxY: y.length > 0 ? Math.max(...y) : null,
          firstPoints: points.slice(0, 6),
        };
        const waveformStyle = waveformStroke;
        let immediateBounds: Readonly<{
          left: number;
          top: number;
          right: number;
          bottom: number;
        }> | null = null;
        let beforePixels: Uint8ClampedArray | null = null;
        if (waveformStyle && points.length > 0) {
          const scaleX = this.canvas.width / this.canvas.clientWidth;
          const scaleY = this.canvas.height / this.canvas.clientHeight;
          const left = Math.max(0, Math.floor(Math.min(...x) * scaleX) - 1);
          const right = Math.min(
            this.canvas.width,
            Math.ceil(Math.max(...x) * scaleX) + 2,
          );
          const top = Math.max(0, Math.floor(Math.min(...y) * scaleY) - 1);
          const bottom = Math.min(
            this.canvas.height,
            Math.ceil(Math.max(...y) * scaleY) + 2,
          );
          immediateBounds = { left, top, right, bottom };
          try {
            beforePixels = this.getImageData(
              left,
              top,
              Math.max(1, right - left),
              Math.max(1, bottom - top),
            ).data.slice();
          } catch {
            // Keep the real canvas draw running when the diagnostic crop is unreadable.
          }
        }
        const result = invokeStroke();
        if (waveformStyle && points.length > 0 && immediateBounds !== null) {
          const { left, top, right, bottom } = immediateBounds;
          try {
            const pixels = this.getImageData(
              left,
              top,
              Math.max(1, right - left),
              Math.max(1, bottom - top),
            ).data;
            let nonTransparentPixelCount = 0;
            let waveformColourPixelCount = 0;
            let maximumAlpha = 0;
            let maximumBlue = 0;
            let changedPixelCount = 0;
            let maximumChannelDelta = 0;
            for (let offset = 0; offset < pixels.length; offset += 4) {
              if (pixels[offset + 3]! > 0) nonTransparentPixelCount += 1;
              maximumAlpha = Math.max(maximumAlpha, pixels[offset + 3]!);
              maximumBlue = Math.max(maximumBlue, pixels[offset + 2]!);
              if (beforePixels !== null) {
                let changed = false;
                for (let channel = 0; channel < 4; channel += 1) {
                  const delta = Math.abs(
                    pixels[offset + channel]! - beforePixels[offset + channel]!,
                  );
                  maximumChannelDelta = Math.max(maximumChannelDelta, delta);
                  if (delta > 0) changed = true;
                }
                if (changed) changedPixelCount += 1;
              }
              // WAVEFORM_COLOUR_TOLERANCE (16), inlined: an init script cannot close over it.
              if (
                waveformRgb !== null &&
                pixels[offset + 3]! > 0 &&
                Math.abs(pixels[offset]! - waveformRgb[0]!) <= 16 &&
                Math.abs(pixels[offset + 1]! - waveformRgb[1]!) <= 16 &&
                Math.abs(pixels[offset + 2]! - waveformRgb[2]!) <= 16
              )
                waveformColourPixelCount += 1;
            }
            Object.assign(strokeTrace, {
              immediateRasterBounds: { left, top, right, bottom },
              immediatePixelRead: "available",
              immediateNonTransparentPixelCount: nonTransparentPixelCount,
              immediateWaveformColourPixelCount: waveformColourPixelCount,
              immediateMaximumAlpha: maximumAlpha,
              immediateMaximumBlue: maximumBlue,
              immediateChangedPixelCount:
                beforePixels === null ? null : changedPixelCount,
              immediateMaximumChannelDelta:
                beforePixels === null ? null : maximumChannelDelta,
            });
          } catch {
            // Filmstrip draws can taint a canvas; pixel diagnostics must never stop the real paint.
            Object.assign(strokeTrace, {
              immediateRasterBounds: { left, top, right, bottom },
              immediatePixelRead: "unavailable",
            });
          }
        }
        trace.push(strokeTrace);
        if (trace.length > 256) trace.splice(0, trace.length - 256);
        return result;
      }
      return invokeStroke();
    };
  }, schedulerTraceEnabled);
  await page.goto(destination.toString(), { waitUntil: "domcontentloaded" });
  await waitForH3Registration(page);
  await assertCandidateBundleInjection(page, context, injectionBefore);
  await beginSettledH3InteractionPhase(page, network, candidateNetwork);
  const graphBefore = await captureM17CanonicalIdentity(page);
  const queueBefore = await supportedHostQueueCounts(page);
  const pageCount = context.pages().length;

  await page.evaluate(() => {
    const runtime = window as any;
    const app = runtime.comfyAPI.app.app;
    runtime.__h3M2553HostBaseline = {
      activeWorkflow: app.extensionManager.workflow.activeWorkflow,
      openWorkflowCount: app.extensionManager.workflow.openWorkflows.length,
      ownedMountCount: document.querySelectorAll("[data-h3-context-mount]")
        .length,
    };
  });
  await page.evaluate(({ output, anchorId, promptId }) => {
    const runtime = window as any;
    const app = runtime.comfyAPI.app.app;
    const tab = app.extensionManager
      .getSidebarTabs()
      .find((entry: any) => entry.id === "h3-context");
    const panel = document.createElement("div");
    panel.id = "h3-m25-53-host-container";
    panel.className = "side-bar-panel sidebar-content-container";
    Object.assign(panel.style, {
      position: "fixed",
      inset: "48px auto 0 24px",
      width: "720px",
      height: "820px",
      overflow: "auto",
      zIndex: "2000",
      background: "#202124",
    });
    document.body.append(panel);
    tab.render(panel);
    const anchor = runtime.LiteGraph.createNode(
      "comfyui_h3_context.H3Context.ProductShell",
    );
    const native = runtime.LiteGraph.createNode("MiniMaxH3ReferenceToVideo");
    if (!anchor || app.graph.getNodeById(anchorId))
      throw new Error("owned anchor unavailable");
    if (!native) throw new Error("supplied native H3 registration unavailable");
    anchor.id = anchorId;
    app.graph.add(anchor);
    app.graph.add(native);
    runtime.__h3M2553Anchor = anchor;
    runtime.__h3M2553Native = native;
    runtime.comfyAPI.api.api.dispatchEvent(
      new CustomEvent("executed", {
        detail: { node: anchorId, prompt_id: promptId, output },
      }),
    );
  }, workspaceFixture);

  const shell = page.locator(`#${CONTAINER_ID}`);
  // IMPORTANT: the first-load splash can hide before the host overlay releases pointer input.
  await expect(page.locator("#splash-loader")).toBeHidden();
  const productionPageTab = shell.locator('[data-page-id="production"]');
  // Keep the real click behind the owned target's hit-test readiness. A fixed delay or force
  // click can hide an overlay regression or click through another installed pack's surface.
  await expect
    .poll(
      () =>
        productionPageTab.evaluate((element) => {
          const button = element as HTMLButtonElement;
          const rect = button.getBoundingClientRect();
          const hit = document.elementFromPoint(
            rect.left + rect.width / 2,
            rect.top + rect.height / 2,
          );
          const hitElement = hit instanceof HTMLElement ? hit : null;
          return {
            ready:
              button.isConnected &&
              !button.disabled &&
              rect.width > 0 &&
              rect.height > 0 &&
              getComputedStyle(button).visibility === "visible" &&
              getComputedStyle(button).pointerEvents !== "none" &&
              hitElement !== null &&
              (hitElement === button || button.contains(hitElement)),
            blocker: hitElement
              ? {
                  tag: hitElement.tagName.toLowerCase(),
                  id: hitElement.id || null,
                  className:
                    typeof hitElement.className === "string"
                      ? hitElement.className.slice(0, 128)
                      : "",
                }
              : null,
          };
        }),
      {
        message:
          "Production tab must receive pointer hits after supplied-host startup",
        timeout: 10_000,
      },
    )
    .toMatchObject({ ready: true });
  await productionPageTab.click();
  await shell.locator('[data-h3-director-function="clip_editor"]').click();
  const start = shell.getByRole("button", {
    name: "Start authoring from this context",
    exact: true,
  });
  const createdResponse = page.waitForResponse(
    (response) =>
      authoringAction(response.request()) === "create_authoring_workspace",
  );
  await start.click();
  const createResponse = await createdResponse;
  expect(
    createResponse.status(),
    "creating the authoring workspace must return its accepted projection",
  ).toBe(201);
  const authoringProjection = decodeAuthoringProjection(
    await createResponse.json(),
  );
  const authoringHandle = authoringProjection.workspaceHandle;
  const suffix = authoringHandle.slice(-16).replace(/[^A-Za-z0-9]/g, "-");
  const initialized = await postAction(
    page,
    encodeAuthoringAction(
      `m25-53-init-${suffix}`,
      "initialize_timeline_history",
      {
        workspace_handle: authoringHandle,
        expected_reference_revision: authoringProjection.reference.revision,
        expected_timeline_revision: authoringProjection.timeline.revision,
        authoring_schema: NLE_AUTHORING_SCHEMA,
        profile_id: NLE_AUTHORING_PROFILE_ID,
        operation_profile_id: NLE_OPERATION_PROFILE_ID_V2,
      },
    ),
  );
  expect(
    initialized.status,
    `timeline history initialization failed: ${JSON.stringify(initialized.body)}`,
  ).toBe(200);
  const initialHistory = decodeTimelineHistoryProjectionV2(initialized.body);
  expect(initialHistory.rejection).toBeNull();
  const expectedReferenceFacts = authoringProjection.reference.sources
    .filter((source) => expectation.assetIds.includes(source.sourceId))
    .map((source) => ({
      assetId: source.sourceId,
      kind: source.kind,
      admitted: source.admitted,
      admissible: source.admissible,
      reason: source.reason,
      durationMilliseconds: source.durationMilliseconds,
      derivedSoundtrack: source.derivedSoundtrack,
    }));
  const initialAssetFacts = expectation.assetIds.map((assetId) => {
    const asset = initialHistory.authoring.assets.find(
      (candidate) => candidate.assetId === assetId,
    );
    return {
      assetId,
      kind: asset?.kind ?? null,
      sourceFrameCount: asset?.sourceFrameCount ?? null,
      embeddedAudio: asset?.embeddedAudio ?? null,
    };
  });
  expect(
    initialAssetFacts.every(
      (asset) =>
        asset.kind === "video" &&
        asset.sourceFrameCount !== null &&
        asset.embeddedAudio === "present_bound",
    ),
    `expected bound video-source facts ${JSON.stringify({
      referenceSources: expectedReferenceFacts,
      timelineAssets: initialAssetFacts,
    })}`,
  ).toBe(true);

  const overlay = page.locator('[data-h3-nle-surface="overlay_v1"]');
  await shell.locator('[data-h3-nle-entry="open"]').click();
  await expect(overlay).toBeVisible();
  await expect(
    overlay.locator('[data-h3-nle-status="timeline"]'),
  ).toHaveAttribute("data-h3-nle-authoring", "ready");
  const status = overlay.locator('[data-h3-nle-status="timeline"]');
  let latest: TimelineReceiptV2 | null = null;
  let transactionOrdinal = 0;
  const transact = async (
    action: () => Promise<void>,
    expectedKinds: readonly string[],
  ): Promise<TimelineReceiptV2> => {
    const ordinal = ++transactionOrdinal;
    const before = transactionRequests;
    const responsePromise = page.waitForResponse(
      (response) =>
        authoringAction(response.request()) === "apply_timeline_transaction",
    );
    let response;
    try {
      [response] = await Promise.all([responsePromise, action()]);
    } catch (error) {
      const visible = await overlay.isVisible().catch(() => false);
      const moveCode = await overlay
        .locator('[data-h3-nle-status="move"]')
        .getAttribute("data-code")
        .catch(() => null);
      const cause =
        error instanceof Error ? error.message.split("\n")[0] : "unknown";
      throw new Error(
        `timeline transaction wait failed: ordinal=${ordinal} expected=${expectedKinds.join(",")} requests=${transactionRequests}/${before} overlay=${visible} move=${moveCode ?? "absent"} cause=${cause}`,
        { cause: error },
      );
    }
    if (response.status() !== 200) {
      const rejected = decodeTimelineHistoryProjectionV2(await response.json());
      throw new Error(
        `timeline transaction rejected: status=${response.status()} code=${rejected.rejection?.code ?? "absent"} expected=${expectedKinds.join(",")}`,
      );
    }
    const request = response.request().postDataJSON();
    expect(request.payload.commands.map((row: any) => row.kind)).toEqual(
      expectedKinds,
    );
    expect(transactionRequests).toBe(before + 1);
    latest = decodeTimelineReceiptV2(await response.json());
    await expect(status).toHaveAttribute(
      "data-h3-transaction",
      latest.transactionId,
    );
    await expect(status).toHaveAttribute("data-h3-nle-authoring", "ready");
    return latest;
  };
  const undo = () =>
    transact(
      () => overlay.locator('[data-h3-nle-control="history.undo"]').click(),
      ["undo"],
    );
  const redo = () =>
    transact(
      () => overlay.locator('[data-h3-nle-control="history.redo"]').click(),
      ["redo"],
    );

  // A click on either side of each divider is the pointer-only alternative to dragging; these
  // view adjustments must never become timeline commands.
  const splitterClickCases = [
    {
      id: "bin_monitor",
      forward: { x: 40, y: 22 },
      backward: { x: 4, y: 22 },
    },
    {
      id: "monitor_inspector",
      forward: { x: 40, y: 22 },
      backward: { x: 4, y: 22 },
    },
    {
      id: "top_timeline",
      forward: { x: 100, y: 40 },
      backward: { x: 100, y: 4 },
    },
  ] as const;
  for (const { id, forward, backward } of splitterClickCases) {
    const splitter = overlay.locator(`[data-h3-nle-splitter="${id}"]`);
    const initial = Number(await splitter.getAttribute("aria-valuenow"));
    const beforeViewOnly = transactionRequests;
    await splitter.click({ position: forward });
    await expect
      .poll(async () => Number(await splitter.getAttribute("aria-valuenow")))
      .not.toBe(initial);
    await splitter.click({ position: backward });
    await expect
      .poll(async () => Number(await splitter.getAttribute("aria-valuenow")))
      .toBe(initial);
    await expect(splitter).toBeFocused();
    expect(transactionRequests).toBe(beforeViewOnly);
  }

  await overlay.locator('[data-h3-nle-pane="assets"]').click();
  const mediaBin = overlay.locator('[data-h3-nle-region="asset-bin"]');
  const mediaScroll = mediaBin.locator(".h3-nle-media-scroll");
  await mediaScroll.hover();
  await page.mouse.wheel(0, 2_400);
  for (const assetId of expectation.assetIds)
    await expect(
      overlay.locator(`[data-h3-nle-asset="${assetId}"]`),
    ).toBeVisible();
  await expect
    .poll(() =>
      Promise.all(
        expectation.assetIds.map((assetId) =>
          overlay
            .locator(`[data-h3-nle-asset="${assetId}"] [data-h3-nle-thumbnail]`)
            .evaluate((node) => (node as HTMLCanvasElement).width),
        ),
      ).then((widths) => widths.filter((width) => width > 0).length),
    )
    .toBe(3);
  const captures: OwnedCapture[] = [];
  for (const [index, assetId] of expectation.assetIds.entries()) {
    const assetCard = overlay.locator(`[data-h3-nle-asset="${assetId}"]`);
    await expect(assetCard).toBeVisible();
    await assetCard.evaluate((element) =>
      element.scrollIntoView({
        block: "center",
        inline: "center",
        behavior: "instant",
      }),
    );
    await expectFullyVisibleThroughClipAncestors(
      assetCard,
      "initial asset " + assetId,
    );
    await expectLoadedAssetCard(assetCard, "initial asset " + assetId);
    const assetCapture = await assetCard.screenshot();
    const dimensions = pngDimensions(assetCapture);
    await testInfo.attach(
      "m25-53-host-asset-card-" +
        (index + 1) +
        "-owned-" +
        dimensions.width +
        "x" +
        dimensions.height,
      { contentType: "image/png", body: assetCapture },
    );
    captures.push(
      await retainOwnedCapture(
        "visual-oracle-asset-" + (index + 1),
        assetCapture,
        candidateBundle.sha256,
        evidenceRunId,
      ),
    );
  }
  const firstInitialAsset = overlay.locator(
    `[data-h3-nle-asset="${expectation.assetIds[0]}"]`,
  );
  await firstInitialAsset.evaluate((element) =>
    element.scrollIntoView({
      block: "center",
      inline: "center",
      behavior: "instant",
    }),
  );
  await expectFullyVisibleThroughClipAncestors(
    firstInitialAsset,
    "first initial asset before default capture",
  );
  const defaultCapture = await overlay.screenshot();
  captures.push(
    await attachOwnedViewportCapture(
      testInfo,
      "visual-oracle-default",
      defaultCapture,
      candidateBundle.sha256,
      evidenceRunId,
    ),
  );

  // The accepted V2 empty-authoring surface has no rendered track viewport until the first clip
  // exists. Use its real primary card action for that first insert; subsequent interaction still
  // exercises the populated timeline's pointer and keyboard paths.
  const firstCard = overlay.locator(
    `[data-h3-nle-asset="${expectation.assetIds[0]}"] .h3-nle-media-primary`,
  );
  // The thumbnail census scrolls the bin to its end; return to the first real card before acting.
  await firstCard.scrollIntoViewIfNeeded();
  await transact(() => firstCard.click(), ["insert_asset_clip"]);

  const playhead = overlay.getByRole("slider", {
    name: "Playhead",
    exact: true,
  });
  await expect(playhead).toHaveAttribute("aria-disabled", "false", {
    timeout: 60_000,
  });
  expect(latest!.authoring.contentEndExclusive).toBe(
    EXPECTED_FIRST_INSERT_CONTENT_END_EXCLUSIVE,
  );
  const firstInserted = latest!.authoring.clips.find(
    (clip) => clip.assetId === expectation.assetIds[0],
  );
  expect(firstInserted).toMatchObject({
    startFrame: 0,
    sourceStartFrame: 0,
    durationFrames: 96,
  });
  for (const [index, assetId] of expectation.assetIds.slice(1).entries()) {
    await transact(
      () =>
        overlay
          .locator(`[data-h3-nle-asset="${assetId}"] .h3-nle-media-primary`)
          .click(),
      ["insert_asset_clip"],
    );
    expect(latest!.authoring.contentEndExclusive).toBe(96 * (index + 2));
  }
  const primaryTrack = latest!.authoring.tracks.find(
    (track) => track.kind === "primary_video",
  );
  if (primaryTrack === undefined)
    throw new Error("primary track is unavailable");
  const primaryClips = () =>
    latest!.authoring.clips
      .filter(
        (clip) =>
          clip.trackId === primaryTrack.trackId &&
          expectation.assetIds.includes(clip.assetId as never),
      )
      .sort((left, right) => left.startFrame - right.startFrame);
  expect(primaryClips().map((clip) => clip.assetId)).toEqual(
    expectation.assetIds,
  );
  expect(
    primaryClips().map(({ startFrame, sourceStartFrame, durationFrames }) => ({
      startFrame,
      sourceStartFrame,
      durationFrames,
    })),
  ).toEqual(
    [0, 96, 192].map((startFrame) => ({
      startFrame,
      sourceStartFrame: 0,
      durationFrames: 96,
    })),
  );

  // Range insert and overwrite are distinct visible commands. Each is observed and then undone so
  // the final three-source recipe remains simple enough for an independent byte observer.
  // M25-63: the range commands live in the card's menu (right-click on the card).
  const rangeCard = overlay.locator(
    `[data-h3-nle-asset="${expectation.assetIds[1]}"]`,
  );
  const followingClipId = primaryClips()[2]!.clipId;
  for (const [control, kind] of [
    ["range.insert", "insert_range"],
    ["range.overwrite", "overwrite_range"],
  ] as const) {
    // Insert shifts whole clips and must start at a boundary; Overwrite replaces the occupied
    // second clip in place. Interior Insert is rejected by the core and pinned separately.
    const rangeStart =
      kind === "insert_range"
        ? primaryClips()[2]!.startFrame
        : primaryClips()[1]!.startFrame;
    await seek(page, playhead, rangeStart);
    const before = latest!.authoring.clips;
    await transact(() => chooseAssetCommand(page, rangeCard, control), [kind]);
    expect(latest!.authoring.clips).not.toEqual(before);
    expect(
      latest!.authoring.clips.find((clip) => clip.clipId === followingClipId)
        ?.startFrame,
    ).toBe(kind === "insert_range" ? 288 : 192);
    await undo();
    expect(primaryClips().map((clip) => clip.startFrame)).toEqual([0, 96, 192]);
  }

  const firstClip = primaryClips()[0]!;
  const secondClip = primaryClips()[1]!;
  const select = (clipId: string) =>
    overlay.locator(
      `[data-h3-nle-clip="${clipId}"] [data-h3-nle-control="selection.set"]`,
    );
  const ensureSelected = async (clipId: string) => {
    if (latest?.selection.length === 1 && latest.selection[0] === clipId) {
      await expect(select(clipId)).toHaveAttribute("aria-pressed", "true");
      return;
    }
    await select(clipId).scrollIntoViewIfNeeded();
    const beforeHit = await select(clipId).evaluate((element) => {
      const box = element.getBoundingClientRect();
      const hit = document.elementFromPoint(
        box.x + box.width / 2,
        box.y + box.height / 2,
      );
      const clip = element.closest<HTMLElement>("[data-h3-nle-clip]");
      const clipBox = clip?.getBoundingClientRect();
      return {
        within: hit !== null && element.contains(hit),
        width: Math.round(box.width),
        body: [box.x, box.y, box.width, box.height].map(Math.round),
        clip: clipBox
          ? [clipBox.x, clipBox.y, clipBox.width, clipBox.height].map(
              Math.round,
            )
          : null,
        bodyDisplay: getComputedStyle(element).display,
        clipColumns: clip ? getComputedStyle(clip).gridTemplateColumns : null,
        grips: clip
          ? [...clip.querySelectorAll<HTMLElement>(".h3-nle-grip")].map(
              (grip) => [grip.hidden, getComputedStyle(grip).display],
            )
          : [],
        sameClip:
          hit?.closest("[data-h3-nle-clip]") ===
          element.closest("[data-h3-nle-clip]"),
        targetPointer: getComputedStyle(element).pointerEvents,
        hitPointer:
          hit instanceof Element ? getComputedStyle(hit).pointerEvents : "none",
        hitTag: hit?.tagName ?? "none",
        hitClass: hit instanceof HTMLElement ? hit.className : "none",
      };
    });
    if (!beforeHit.within)
      throw new Error(
        `image clip pointer hit unavailable: ${JSON.stringify(beforeHit)}`,
      );
    try {
      await transact(() => select(clipId).click(), ["select_clips"]);
    } catch (error) {
      const target = await select(clipId)
        .evaluate((element) => {
          const box = element.getBoundingClientRect();
          const hit = document.elementFromPoint(
            box.x + box.width / 2,
            box.y + box.height / 2,
          );
          return {
            pressed: element.getAttribute("aria-pressed"),
            width: Math.round(box.width),
            height: Math.round(box.height),
            hitControl:
              hit
                ?.closest("[data-h3-nle-control]")
                ?.getAttribute("data-h3-nle-control") ?? "none",
          };
        })
        .catch(() => null);
      throw new Error(
        `image clip selection failed: receiptSelection=${latest?.selection.length ?? -1} target=${JSON.stringify(target)} cause=${error instanceof Error ? error.message : "unknown"}`,
        { cause: error },
      );
    }
  };
  // M25-64: content-free observer evidence for this row, kept beside the captures.
  const observerEvidence = {
    schema: "m25-64-host-observer-evidence/1",
    clipMenuRoutes: [] as unknown[],
    trimGripRoutes: [] as unknown[],
    compositedRegions: [] as unknown[],
    transformOverlayWaits: [] as unknown[],
    pageEvents: [] as unknown[],
    waveformColour: null as unknown,
  };
  const observedBundleSha256 = candidateBundle.sha256;
  const recordObserverEvidence = () =>
    retainObserverEvidence(
      JSON.stringify(observerEvidence, null, 2),
      observedBundleSha256,
      evidenceRunId,
    );
  // M25-64: a page crash, a page close and a browser disconnect each carry their own wall-clock
  // time, so a failure that coincides with the runner's bound can be ordered against it rather
  // than attributed to it.
  const pageEvent = (event: string) => {
    observerEvidence.pageEvents.push({ event, at: new Date().toISOString() });
    void recordObserverEvidence().catch(() => undefined);
  };
  page.on("crash", () => pageEvent("page crash"));
  page.on("close", () => pageEvent("page close"));
  page
    .context()
    .browser()
    ?.on("disconnected", () => pageEvent("browser disconnected"));
  // IMPORTANT (M25-64 B-M2564-11): the edit affordances follow one rule since M25-62 (R7,
  // B-M2562-03), restated here rather than imported so a product change to it fails this row.
  // A clip's display width is its admitted duration times the timeline scale, never less than
  // 20 px. Under a fine pointer, a selected clip below 24 px has the trim rail (its grips and its
  // menu trigger); from 24 px it has inline grips, and from 96 px also an inline menu trigger;
  // in [24, 96) its menu opens only by a context click. A coarse pointer switches at 144 px.
  const clipRoute = async (clipId: string) => {
    const admitted = latest?.authoring.clips.find(
      (clip) => clip.clipId === clipId,
    );
    if (admitted === undefined)
      throw new Error(`${clipId} is not in the admitted timeline`);
    const scale = Number(
      await overlay
        .locator('[data-h3-nle-region="timeline"]')
        .getAttribute("data-h3-nle-pixels-per-frame"),
    );
    expect(
      Number.isFinite(scale) && scale > 0,
      "the timeline publishes a positive scale",
    ).toBe(true);
    const coarse = await page.evaluate(
      () => matchMedia("(pointer: coarse)").matches,
    );
    const logicalWidth = Math.max(20, admitted.durationFrames * scale);
    const measuredWidth =
      (await overlay.locator(`[data-h3-nle-clip="${clipId}"]`).boundingBox())
        ?.width ?? null;
    const rail = coarse ? logicalWidth < 144 : logicalWidth < 24;
    const menuPath = rail
      ? "rail_trigger"
      : coarse || logicalWidth >= 96
        ? "inline_trigger"
        : "context_click";
    return {
      clipId,
      durationFrames: admitted.durationFrames,
      pixelsPerFrame: scale,
      logicalWidth,
      measuredWidth,
      pointer: coarse ? "coarse" : "fine",
      selectionCount: latest?.selection.length ?? 0,
      rail,
      menuPath,
    } as const;
  };
  // The DOM measurement agrees with the logical width it is rendered from, checked apart from
  // the route: the route is decided by the logical width alone.
  const expectMeasuredWidth = (
    route: Awaited<ReturnType<typeof clipRoute>>,
  ) => {
    expect(route.measuredWidth, `${route.clipId} is measurable`).not.toBeNull();
    expect(
      Math.abs(route.measuredWidth! - route.logicalWidth),
      `${route.clipId} measures ${route.measuredWidth} px against its logical ${route.logicalWidth} px`,
    ).toBeLessThanOrEqual(1);
  };
  const clickOnlyTrim = async (label: string, kind: string) => {
    expect(
      latest?.selection.length,
      "a click-only trim needs one selected clip",
    ).toBe(1);
    const route = await clipRoute(latest!.selection[0]!);
    const inlineTrigger = overlay.locator(
      `[data-h3-nle-clip="${route.clipId}"] [data-h3-nle-menu-trigger="clip"]`,
    );
    const railTrigger = overlay.locator(
      `.h3-nle-trim-rail[data-h3-nle-trim-clip="${route.clipId}"] [data-h3-nle-menu-trigger="clip"]`,
    );
    const counts = {
      inlineTrigger: await inlineTrigger.count(),
      railTrigger: await railTrigger.count(),
    };
    observerEvidence.clipMenuRoutes.push({
      step: label,
      kind,
      ...route,
      ...counts,
    });
    await recordObserverEvidence();
    expectMeasuredWidth(route);
    expect(
      counts,
      `${route.clipId} offers the ${route.menuPath} route`,
    ).toEqual({
      inlineTrigger: route.menuPath === "inline_trigger" ? 1 : 0,
      railTrigger: route.menuPath === "rail_trigger" ? 1 : 0,
    });
    if (route.menuPath === "context_click")
      await select(route.clipId).click({ button: "right" });
    else {
      const trigger =
        route.menuPath === "inline_trigger" ? inlineTrigger : railTrigger;
      await expect(trigger).toHaveAccessibleName("Open clip menu");
      await trigger.click();
    }
    const menu = overlay.getByRole("menu", { name: "Clip menu", exact: true });
    await expect(menu).toBeVisible();
    return transact(
      () => menu.getByRole("menuitem", { name: label, exact: true }).click(),
      [kind],
    );
  };

  // G4 click-only steps trim both edges and extend them back to the exact accepted frame range.
  // Each click is a separate canonical command; one Undo reverses exactly one click.
  await ensureSelected(firstClip.clipId);
  await clickOnlyTrim("Trim start later by one frame", "trim_clip");
  expect(primaryClips()[0]).toMatchObject({
    startFrame: 1,
    sourceStartFrame: 1,
    durationFrames: 95,
  });
  await clickOnlyTrim("Trim start earlier by one frame", "trim_clip");
  expect(primaryClips()[0]).toMatchObject({
    startFrame: 0,
    sourceStartFrame: 0,
    durationFrames: 96,
  });
  await undo();
  expect(primaryClips()[0]).toMatchObject({
    startFrame: 1,
    durationFrames: 95,
  });
  await undo();
  expect(primaryClips()[0]).toMatchObject({
    startFrame: 0,
    durationFrames: 96,
  });
  await clickOnlyTrim("Trim end earlier by one frame", "trim_clip");
  expect(primaryClips()[0]?.durationFrames).toBe(95);
  await clickOnlyTrim("Trim end later by one frame", "trim_clip");
  expect(primaryClips()[0]?.durationFrames).toBe(96);
  await undo();
  expect(primaryClips()[0]?.durationFrames).toBe(95);
  await undo();
  expect(primaryClips().map((clip) => clip.startFrame)).toEqual([0, 96, 192]);

  // G5 repeats the same edge operations through the actual ripple toggle, including an extension
  // that restores following clips, and preserves one undo entry for each click.
  const rippleToggle = overlay.locator(
    '[data-h3-nle-control="transport.ripple"]',
  );
  if (!(await rippleToggle.isVisible()))
    await overlay.locator('[data-h3-nle-control="toolbar.more"]').click();
  await expect(rippleToggle).toBeVisible();
  await expect(rippleToggle).toHaveAttribute("aria-pressed", "false");
  await rippleToggle.click();
  await expect(rippleToggle).toHaveAttribute("aria-pressed", "true");
  await clickOnlyTrim("Trim end earlier by one frame", "ripple_trim");
  expect(primaryClips().map((clip) => clip.startFrame)).toEqual([0, 95, 191]);
  await clickOnlyTrim("Trim end later by one frame", "ripple_trim");
  expect(primaryClips().map((clip) => clip.startFrame)).toEqual([0, 96, 192]);
  await undo();
  expect(primaryClips().map((clip) => clip.startFrame)).toEqual([0, 95, 191]);
  await undo();
  expect(primaryClips().map((clip) => clip.startFrame)).toEqual([0, 96, 192]);
  await rippleToggle.click();
  await expect(rippleToggle).toHaveAttribute("aria-pressed", "false");

  await transact(() => select(secondClip.clipId).click(), ["select_clips"]);
  await select(secondClip.clipId).focus();
  await page.keyboard.press("Enter");
  const gridStep = Number(
    await overlay
      .locator('input[data-h3-nle-control="transport.scroll"]')
      .getAttribute("step"),
  );
  if (!Number.isSafeInteger(gridStep) || gridStep < 1)
    throw new Error("timeline grid step is unavailable");
  for (let index = 0; index < Math.floor(192 / gridStep); index += 1)
    await page.keyboard.press("Shift+ArrowRight");
  for (let index = 0; index < 192 % gridStep; index += 1)
    await page.keyboard.press("ArrowRight");
  await transact(() => page.keyboard.press("Enter"), ["move_clip"]);
  expect(primaryClips().map((clip) => clip.assetId)).toEqual([
    expectation.assetIds[0],
    expectation.assetIds[2],
    expectation.assetIds[1],
  ]);
  expect(primaryClips().map((clip) => clip.startFrame)).toEqual([0, 192, 288]);
  await undo();
  expect(primaryClips().map((clip) => clip.startFrame)).toEqual([0, 96, 192]);

  const videoMenu = await openTrackMenu(page, primaryTrack.trackId);
  await videoMenu.locator("select").selectOption("video_overlay");
  await videoMenu
    .locator('input[type="number"]')
    .fill(String(primaryTrack.order + 1));
  await transact(
    () => videoMenu.locator('[data-h3-nle-control="track.add"]').click(),
    ["create_track"],
  );
  const videoOverlayTrack = latest!.authoring.tracks.find(
    (track) =>
      track.kind === "video_overlay" && track.order === primaryTrack.order + 1,
  );
  if (videoOverlayTrack === undefined)
    throw new Error("compatible video overlay track was not created");
  const videoHeader = overlay.locator(
    `[data-h3-nle-track="${videoOverlayTrack.trackId}"]`,
  );
  const thirdClipId = primaryClips()[2]!.clipId;
  await transact(
    () => videoHeader.locator('[data-h3-nle-control="track.locked"]').click(),
    ["set_track_locked"],
  );
  await transact(() => select(thirdClipId).click(), ["select_clips"]);
  await select(thirdClipId).focus();
  await page.keyboard.press("Enter");
  await page.keyboard.press("Alt+ArrowDown");
  await expect(overlay.locator('[data-h3-nle-status="move"]')).toHaveAttribute(
    "data-code",
    "locked_track",
  );
  const beforeRefusal = transactionRequests;
  await page.keyboard.press("Enter");
  expect(transactionRequests).toBe(beforeRefusal);
  // The refused Enter already discards the move draft. Escape from idle would close the
  // workspace, not cancel a draft that no longer exists.
  await expect(overlay).toBeVisible();
  await transact(
    () => videoHeader.locator('[data-h3-nle-control="track.locked"]').click(),
    ["set_track_locked"],
  );
  await select(thirdClipId).focus();
  await page.keyboard.press("Enter");
  await page.keyboard.press("Alt+ArrowDown");
  await transact(() => page.keyboard.press("Enter"), ["move_clip"]);
  expect(
    latest!.authoring.clips.find((clip) => clip.clipId === thirdClipId)
      ?.trackId,
  ).toBe(videoOverlayTrack.trackId);
  await undo();
  expect(primaryClips().map((clip) => clip.startFrame)).toEqual([0, 96, 192]);
  // Move the trailing pair into free timeline space; shifting the packed leading pair
  // would overlap the third clip and is correctly refused by move admission.
  await transact(() => select(secondClip.clipId).click(), ["select_clips"]);
  await transact(
    () => select(thirdClipId).click({ modifiers: ["Shift"] }),
    ["select_clips"],
  );
  const more = overlay.locator('[data-h3-nle-control="toolbar.more"]');
  if (await more.isVisible()) await more.click();
  await overlay.locator('[data-h3-nle-alternative="move.open"]').click();
  await overlay
    .getByRole("button", { name: "Move later by one grid step" })
    .click();
  await expect(
    overlay.locator('[data-h3-nle-status="move"]'),
  ).not.toHaveAttribute("data-code", "overlap");
  await transact(
    () => overlay.locator('[data-h3-nle-control="clip.move_group"]').click(),
    ["move_group"],
  );
  await undo();
  const toolbarOverflow = overlay.locator(".h3-nle-toolbar-overflow");
  if (await toolbarOverflow.isVisible()) {
    await more.click();
    await expect(toolbarOverflow).toBeHidden();
  }

  // Marquee selection and a cancelled trim draft prove the live selection surface and Escape
  // ownership without adding an edit after cancellation.
  const marqueeBaselineId = primaryClips()[0]!.clipId;
  if (
    latest!.selection.length !== 1 ||
    latest!.selection[0] !== marqueeBaselineId
  )
    await transact(() => select(marqueeBaselineId).click(), ["select_clips"]);
  expect(latest!.selection).toEqual([marqueeBaselineId]);
  const grid = overlay.getByRole("grid", {
    name: "Timeline tracks",
    exact: true,
  });
  const gridBox = await grid.boundingBox();
  if (gridBox === null) throw new Error("timeline grid has no layout box");
  const marqueeResponse = page.waitForResponse(
    (response) =>
      authoringAction(response.request()) === "apply_timeline_transaction",
  );
  const beforeMarquee = transactionRequests;
  await page.mouse.move(gridBox.x + gridBox.width - 8, gridBox.y + 8);
  await page.mouse.down();
  await page.mouse.move(
    gridBox.x + (await nleLaneOrigin(page)),
    gridBox.y + 48,
    {
      steps: 5,
    },
  );
  await page.mouse.up();
  const marquee = await marqueeResponse;
  expect(marquee.status()).toBe(200);
  expect(
    marquee
      .request()
      .postDataJSON()
      .payload.commands.map((row: any) => row.kind),
  ).toEqual(["select_clips"]);
  expect(transactionRequests).toBe(beforeMarquee + 1);
  latest = decodeTimelineReceiptV2(await marquee.json());

  await transact(
    () => select(primaryClips()[2]!.clipId).click(),
    ["select_clips"],
  );
  // M25-64 B-M2564-11: the same rule decides where the grips are: inline for a clip wide enough,
  // on the trim rail below that (the hermetic `clip.trim` recipe).
  const gripRoute = await clipRoute(primaryClips()[2]!.clipId);
  const inlineGrip = overlay.locator(
    `[data-h3-nle-clip="${gripRoute.clipId}"] .h3-nle-grip[data-h3-nle-trim-edge="end"]:not([hidden])`,
  );
  const railGrip = overlay.locator(
    `.h3-nle-trim-rail[data-h3-nle-trim-clip="${gripRoute.clipId}"] [data-h3-nle-trim-edge="end"]:not([hidden])`,
  );
  const gripCounts = {
    inlineGrip: await inlineGrip.count(),
    railGrip: await railGrip.count(),
  };
  observerEvidence.trimGripRoutes.push({
    step: "keyboard end trim",
    ...gripRoute,
    gripPath: gripRoute.rail ? "rail_grip" : "inline_grip",
    ...gripCounts,
  });
  await recordObserverEvidence();
  expectMeasuredWidth(gripRoute);
  expect(gripCounts, `${gripRoute.clipId} trims by its expected grip`).toEqual({
    inlineGrip: gripRoute.rail ? 0 : 1,
    railGrip: gripRoute.rail ? 1 : 0,
  });
  const trimGrip = gripRoute.rail ? railGrip : inlineGrip;
  await expect(trimGrip).toBeEnabled();
  await trimGrip.focus();
  await expect(trimGrip).toBeFocused();
  const beforeCancel = transactionRequests;
  await page.keyboard.press("ArrowLeft");
  await page.keyboard.press("Escape");
  await page.waitForTimeout(100);
  expect(transactionRequests).toBe(beforeCancel);
  await trimGrip.focus();
  await expect(trimGrip).toBeFocused();
  await page.keyboard.press("ArrowLeft");
  await transact(() => page.keyboard.press("Enter"), ["trim_clip"]);
  await seek(page, playhead, primaryClips()[2]!.startFrame + 12);
  await transact(
    () => overlay.locator('[data-h3-nle-control="clip.split"]').click(),
    ["split_clip"],
  );
  await undo();

  await transact(() => select(firstClip.clipId).click(), ["select_clips"]);
  await seek(page, playhead, 12);
  await overlay.locator('[data-h3-nle-control="transport.ripple"]').click();
  const rippleControl = overlay.locator(
    '[data-h3-nle-control="range.ripple_trim"]',
  );
  if (!(await rippleControl.isVisible()))
    await overlay.locator('[data-h3-nle-control="toolbar.more"]').click();
  await transact(() => rippleControl.click(), ["ripple_trim"]);
  expect(primaryClips()[0]).toMatchObject({
    sourceStartFrame: 12,
    durationFrames: 84,
  });
  expect(primaryClips().map((clip) => clip.startFrame)).toEqual([0, 84, 180]);
  await undo();
  expect(primaryClips().map((clip) => clip.startFrame)).toEqual([0, 96, 192]);
  await overlay.locator('[data-h3-nle-control="transport.ripple"]').click();

  await overlay.locator('[data-h3-nle-control="transport.zoom_fit"]').click();
  await expect(playhead).toHaveAttribute("aria-disabled", "false", {
    timeout: 60_000,
  });
  await seekPlayhead(page, playhead, 95);
  await playhead.scrollIntoViewIfNeeded();
  const rulerBox = await playhead.boundingBox();
  if (rulerBox === null) throw new Error("playhead ruler has no layout box");
  const timeline = overlay.locator('[data-h3-nle-region="timeline"]');
  const laneOrigin = Number(
    await timeline.getAttribute("data-h3-nle-lane-origin-px"),
  );
  const scale = Number(
    await timeline.getAttribute("data-h3-nle-pixels-per-frame"),
  );
  const viewStart = Number(
    await timeline.getAttribute("data-h3-nle-view-start"),
  );
  if (![laneOrigin, scale, viewStart].every(Number.isFinite))
    throw new Error("playhead ruler geometry is unavailable");
  const lastAcceptedFrame = Number(
    await playhead.getAttribute("aria-valuemax"),
  );
  expect(lastAcceptedFrame).toBeGreaterThan(192);
  const rulerX = (frame: number) =>
    rulerBox.x + laneOrigin + (frame - viewStart) * scale;
  const beforeView = transactionRequests;
  await page.mouse.move(rulerX(95), rulerBox.y + rulerBox.height / 2);
  await page.mouse.down();
  for (const frame of [96, 192, lastAcceptedFrame]) {
    await page.mouse.move(rulerX(frame), rulerBox.y + rulerBox.height / 2);
    await expect(playhead).toHaveAttribute("aria-valuenow", String(frame));
  }
  await page.mouse.up();
  await expect(
    overlay.locator('[data-h3-nle-canvas="composition"]'),
  ).toHaveAttribute("data-h3-nle-presented-frame", String(lastAcceptedFrame));
  const zoomGrid = overlay.getByRole("grid", {
    name: "Timeline tracks",
    exact: true,
  });
  const zoomBox = await zoomGrid.boundingBox();
  if (zoomBox === null) throw new Error("zoom grid has no layout box");
  await page.mouse.move(zoomBox.x + zoomBox.width / 2, zoomBox.y + 24);
  await page.keyboard.down("Control");
  await page.mouse.wheel(0, -200);
  await page.keyboard.up("Control");
  const scroll = overlay.locator('[data-h3-nle-control="transport.scroll"]');
  await expect
    .poll(async () => Number(await scroll.getAttribute("max")))
    .toBeGreaterThan(0);
  const beforePan = Number(await scroll.inputValue());
  await page.keyboard.down("Shift");
  await page.mouse.wheel(0, 300);
  await page.keyboard.up("Shift");
  await expect
    .poll(async () => Number(await scroll.inputValue()))
    .toBeGreaterThan(beforePan);
  expect(transactionRequests).toBe(beforeView);
  await overlay.locator('[data-h3-nle-control="transport.zoom_fit"]').click();

  // Add real image/title overlays, then keep one monitor transform and one colour edit in the
  // final recipe. The output observer below judges their actual pixels, not only the receipts.
  for (const [offset, kind] of ["image_overlay", "text_overlay"].entries()) {
    const menu = await openTrackMenu(page, primaryTrack.trackId);
    await menu.locator("select").selectOption(kind);
    await menu
      .locator('input[type="number"]')
      .fill(String(latest!.authoring.tracks.length + offset));
    await transact(
      () => menu.locator('[data-h3-nle-control="track.add"]').click(),
      ["create_track"],
    );
  }
  await transact(() => select(thirdClipId).click(), ["select_clips"]);
  await select(thirdClipId).focus();
  await page.keyboard.press("Enter");
  await page.keyboard.press("Alt+ArrowDown");
  await page.keyboard.press("Alt+ArrowDown");
  await expect(overlay.locator('[data-h3-nle-status="move"]')).toHaveAttribute(
    "data-code",
    "incompatible_track",
  );
  const beforeIncompatible = transactionRequests;
  await page.keyboard.press("Enter");
  expect(transactionRequests).toBe(beforeIncompatible);
  await expect(overlay).toBeVisible();
  const middleFrame =
    primaryClips()[1]!.startFrame +
    Math.floor(primaryClips()[1]!.durationFrames / 2);
  await seek(page, playhead, middleFrame);
  await overlay.locator('[data-h3-nle-pane="assets"]').click();
  await mediaScroll.hover();
  await page.mouse.wheel(0, -2_400);
  await expect(
    overlay.locator('[data-h3-nle-asset="first_frame_1"]'),
  ).toBeVisible();
  await transact(
    () =>
      overlay
        .locator(
          '[data-h3-nle-asset="first_frame_1"] [data-h3-nle-control="asset.insert"]',
        )
        .click(),
    ["insert_asset_clip"],
  );
  const imageClip = latest!.authoring.clips.find(
    (clip) =>
      clip.assetId === "first_frame_1" && clip.startFrame === middleFrame,
  );
  if (imageClip === undefined)
    throw new Error("image overlay was not inserted");
  await ensureSelected(imageClip.clipId);
  const imageEndGrip = overlay.locator(
    `.h3-nle-trim-rail[data-h3-nle-trim-clip="${imageClip.clipId}"] [data-h3-nle-trim-edge="end"]`,
  );
  await imageEndGrip.focus();
  await transact(async () => {
    for (let step = 0; step < 23; step += 1)
      await page.keyboard.press("ArrowRight");
    await page.keyboard.press("Enter");
  }, ["trim_clip"]);
  expect(
    latest!.authoring.clips.find((clip) => clip.clipId === imageClip.clipId)
      ?.durationFrames,
  ).toBe(24);
  await seek(page, playhead, middleFrame + 16);
  await overlay.locator('[data-h3-nle-pane="text"]').click();
  await overlay
    .getByRole("tabpanel", { name: "Text", exact: true })
    .locator('input[type="text"]')
    .fill("M25-53");
  await transact(
    () => overlay.locator('[data-h3-nle-control="title.insert"]').click(),
    ["insert_title_clip"],
  );
  await seek(page, playhead, middleFrame + 16);
  await ensureSelected(imageClip.clipId);
  // The 64 px image overlay renders at native size on a 1920-wide output, about 23 CSS px in
  // this monitor: every 44 px accessibility handle covers its whole body, so a pointer drag
  // there is not a move. Enlarge it first through the Inspector's linked scale fields (the
  // accepted keyboard/field alternative), then move and scale it by its real handles.
  const imageInspector = overlay.locator(
    `[data-h3-nle-selected-clip="${imageClip.clipId}"]`,
  );
  await imageInspector.locator('[data-h3-nle-property-tab="basic"]').click();
  // Uniform scale is on for a clip whose scales are equal: the one Scale field writes both
  // axes, which the accepted transform below shows.
  await expect(
    imageInspector.locator('[data-h3-nle-control="transform.link_scale"]'),
  ).toHaveAttribute("aria-checked", "true");
  const scaleField = imageInspector.getByRole("spinbutton", {
    name: "Scale (%)",
    exact: true,
  });
  // M25-64 (R10): 600 % is 60,000 bp.
  await scaleField.fill("600");
  await transact(() => scaleField.press("Enter"), ["set_visual_transform"]);
  const scaledImage = latest!.authoring.clips.find(
    (clip) => clip.clipId === imageClip.clipId,
  );
  expect(scaledImage).toMatchObject({
    transform: { scale_x_bp: 60_000, scale_y_bp: 60_000 },
  });

  // M25-64 (diagnosis): the transform overlay's render conditions as the page exposes them --
  // the monitor's view and fallback, its status, the presented frame against the selected clip's
  // accepted range, the laid-out picture, the selection and the overlay itself. The layer
  // geometry the overlay also needs is the session's and is not in the page; it is the remaining
  // condition when every one of these holds and the overlay is still absent. Content-free: codes,
  // counts, frames, boxes and opaque clip identities only.
  const monitorFacts = async (moment: string) => {
    const accepted = latest?.authoring.clips.find(
      (clip) => clip.clipId === imageClip.clipId,
    );
    let observed: unknown;
    try {
      observed = await overlay.evaluate((root) => {
        const box = (element: Element | null) => {
          if (element === null) return null;
          const rect = element.getBoundingClientRect();
          return [rect.x, rect.y, rect.width, rect.height].map(
            (value) => Math.round(value * 10) / 10,
          );
        };
        const monitor = root.querySelector(".h3-nle-monitor");
        const canvas = root.querySelector<HTMLCanvasElement>(
          '[data-h3-nle-canvas="composition"]',
        );
        return {
          view: monitor?.getAttribute("data-h3-nle-view") ?? null,
          fallback:
            root
              .querySelector("[data-h3-nle-fallback]")
              ?.getAttribute("data-h3-nle-fallback") ?? null,
          pictureOverlays: [
            ...root.querySelectorAll(".h3-nle-monitor [data-h3-nle-overlay]"),
          ].map((element) => element.getAttribute("data-h3-nle-overlay")),
          monitorStatus:
            root.querySelector('.h3-nle-status[data-h3-nle-status="monitor"]')
              ?.textContent ?? null,
          presentedFrame:
            canvas?.getAttribute("data-h3-nle-presented-frame") ?? null,
          canvasPlaced: canvas !== null && canvas.style.width !== "",
          canvasBox: box(canvas),
          canvasBacking: canvas === null ? null : [canvas.width, canvas.height],
          pictureAreaBox: box(root.querySelector(".h3-nle-picture")),
          transformOverlays: root.querySelectorAll(
            "[data-h3-nle-transform-overlay]",
          ).length,
          transformHandles: root.querySelectorAll(
            "[data-h3-nle-transform-handle]",
          ).length,
          inspectorClip:
            root
              .querySelector("[data-h3-nle-selected-clip]")
              ?.getAttribute("data-h3-nle-selected-clip") ?? null,
          pressedClips: [...root.querySelectorAll("[data-h3-nle-clip]")]
            .filter(
              (clip) => clip.querySelector('[aria-pressed="true"]') !== null,
            )
            .map((clip) => clip.getAttribute("data-h3-nle-clip")),
          activeElement:
            document.activeElement === null
              ? null
              : `${document.activeElement.tagName.toLowerCase()}${
                  document.activeElement.getAttribute("data-h3-nle-control")
                    ? `[${document.activeElement.getAttribute("data-h3-nle-control")}]`
                    : ""
                }`,
        };
      });
    } catch (error) {
      observed = { unreadable: String(error).slice(0, 200) };
    }
    const ruler = async (name: string) =>
      playhead.getAttribute(name, { timeout: 1_000 }).catch(() => "unreadable");
    return {
      moment,
      at: new Date().toISOString(),
      playheadTarget: middleFrame + 16,
      playhead: {
        now: await ruler("aria-valuenow"),
        disabled: await ruler("aria-disabled"),
        clearReason: await ruler("data-h3-nle-request-clear-reason"),
      },
      selection: [...(latest?.selection ?? [])],
      imageClip:
        accepted === undefined
          ? null
          : {
              clipId: accepted.clipId,
              startFrame: accepted.startFrame,
              durationFrames: accepted.durationFrames,
            },
      page: observed,
    };
  };

  const dragTransformHandle = async (
    handle: "move" | "south_east",
    delta: Readonly<{ x: number; y: number }>,
  ) => {
    // Every accepted transaction rebinds the monitor, which presents again from real media
    // before the overlay for the selected active clip exists; seek and settle first, as the
    // journey does before any other monitor interaction.
    await seek(page, playhead, middleFrame + 16);
    await ensureSelected(imageClip.clipId);
    const transformOverlay = overlay.locator("[data-h3-nle-transform-overlay]");
    const samples: unknown[] = [await monitorFacts("after seek and selection")];
    const wait = { handle, visible: false, samples };
    observerEvidence.transformOverlayWaits.push(wait);
    let waiting = true;
    const sampler = (async () => {
      while (waiting) {
        await page.waitForTimeout(2_000).catch(() => undefined);
        if (waiting) samples.push(await monitorFacts("while waiting"));
      }
    })();
    try {
      await expect(transformOverlay).toBeVisible({ timeout: 30_000 });
      wait.visible = true;
    } finally {
      waiting = false;
      await sampler;
      samples.push(
        await monitorFacts(wait.visible ? "overlay visible" : "wait expired"),
      );
      await recordObserverEvidence();
    }
    const control = overlay.locator(
      `[data-h3-nle-transform-handle="${handle}"]`,
    );
    const box = await control.boundingBox();
    if (box === null) throw new Error(`transform ${handle} handle has no box`);
    const beforeTransform = transactionRequests;
    const transformResponse = page.waitForResponse(
      (response) =>
        authoringAction(response.request()) === "apply_timeline_transaction",
    );
    // The move target is the whole layer body under the eight 44 px scale handles and the
    // rotate handle, so its box centre may be covered on a small layer. Probe a grid inside
    // the box for the first point that actually resolves to the named handle.
    const probe = await page.evaluate(
      ({ x, y, width, height, name }) => {
        const seen: string[] = [];
        for (const row of [4, 3, 5, 2, 6, 1, 7])
          for (const column of [4, 3, 5, 2, 6, 1, 7]) {
            const point = {
              x: x + (width * column) / 8,
              y: y + (height * row) / 8,
            };
            const hit = document.elementFromPoint(point.x, point.y);
            const owner =
              hit
                ?.closest("[data-h3-nle-transform-handle]")
                ?.getAttribute("data-h3-nle-transform-handle") ??
              `${hit?.tagName ?? "none"}.${hit?.className ?? ""}`;
            if (owner === name) return { point, seen };
            seen.push(`${column},${row}:${owner}`);
          }
        return { point: null, seen };
      },
      {
        x: box.x,
        y: box.y,
        width: box.width,
        height: box.height,
        name: handle,
      },
    );
    const center = probe.point;
    expect(
      center,
      `no point on the ${handle} handle box ${JSON.stringify(box)} resolves to that handle: ${probe.seen.join(" ")}`,
    ).not.toBeNull();
    if (center === null) throw new Error("unreachable");
    await page.mouse.move(center.x, center.y);
    await page.mouse.down();
    await page.mouse.move(center.x + delta.x, center.y + delta.y, {
      steps: 4,
    });
    expect(transactionRequests).toBe(beforeTransform);
    await page.mouse.up();
    const transformed = await transformResponse;
    expect(transformed.status()).toBe(200);
    expect(
      transformed
        .request()
        .postDataJSON()
        .payload.commands.map((row: any) => row.kind),
    ).toEqual(["set_visual_transform"]);
    expect(transactionRequests).toBe(beforeTransform + 1);
    latest = decodeTimelineReceiptV2(await transformed.json());
    return latest.authoring.clips.find(
      (clip) => clip.clipId === imageClip.clipId,
    )!;
  };
  // Gesture deltas are fractions of the fitted picture so the resulting basis points do not
  // depend on how large this viewport happens to render the monitor: an eighth of the width is
  // 1250 bp, a twelfth of the height 833 bp, and the corner drag grows the layer by 2.5 %. The
  // 64 px image at 60,000 bp is 384 output px, so after the move it sits inside the second
  // source's placed picture below its landmark row and clear of the output centre.
  const pictureBox = await overlay.locator(".h3-nle-picture").boundingBox();
  if (pictureBox === null) throw new Error("monitor picture has no layout box");
  const movedImage = await dragTransformHandle("move", {
    x: pictureBox.width / 8,
    y: pictureBox.height / 12,
  });
  expect(Number(movedImage.transform.position_x_bp)).toBeGreaterThan(1_000);
  expect(Number(movedImage.transform.position_x_bp)).toBeLessThan(1_500);
  expect(Number(movedImage.transform.position_y_bp)).toBeGreaterThan(600);
  expect(Number(movedImage.transform.position_y_bp)).toBeLessThan(1_100);
  expect(Number(movedImage.transform.scale_x_bp)).toBe(60_000);
  const resizedImage = await dragTransformHandle("south_east", {
    x: pictureBox.width / 40,
    y: pictureBox.height / 40,
  });
  expect(Number(resizedImage.transform.scale_x_bp)).toBeGreaterThan(60_000);
  expect(Number(resizedImage.transform.scale_y_bp)).toBeGreaterThan(60_000);
  expect(Number(resizedImage.transform.rotation_mdeg)).toBe(0);
  await imageInspector.locator('[data-h3-nle-property-tab="colour"]').click();
  await imageInspector.getByLabel("Brightness (%)", { exact: true }).fill("10");
  await transact(
    () =>
      imageInspector.locator('[data-h3-nle-control="visual.effect"]').click(),
    ["set_effect"],
  );

  // Cross-dissolve belongs to the image overlay above the second primary source. A primary
  // clip has no lower-layer participant and core admission must reject that tempting placement.
  await ensureSelected(imageClip.clipId);
  const transitionInspector = overlay.locator(
    `[data-h3-nle-selected-clip="${imageClip.clipId}"]`,
  );
  await transitionInspector
    .locator('[data-h3-nle-property-tab="transition"]')
    .click();
  await transitionInspector
    .getByRole("combobox", { name: "Transition", exact: true })
    .selectOption("cross_dissolve_v1");
  await transitionInspector
    .getByRole("spinbutton", { name: "Transition frames", exact: true })
    .fill("12");
  await transact(
    () =>
      transitionInspector
        .locator('[data-h3-nle-control="boundary.transition"]')
        .click(),
    ["set_transition"],
  );

  // Undo/redo must preserve the intended transition, and transport controls must move the actual
  // playhead across the assembled sequence.
  const withTransition = latest!.authoring;
  await undo();
  await redo();
  expect(latest!.authoring.clips).toEqual(withTransition.clips);
  await seek(page, playhead, 0);
  const monitor = overlay.locator(".h3-nle-monitor");
  await monitor.focus();
  await expect(monitor).toBeFocused();
  await page.keyboard.press(".");
  await expect(playhead).toHaveAttribute("aria-valuenow", "1");
  await overlay.locator('[data-h3-nle-control="transport.play"]').click();
  await expect
    .poll(async () => Number(await playhead.getAttribute("aria-valuenow")))
    .toBeGreaterThan(1);
  await overlay.locator('[data-h3-nle-control="transport.pause"]').click();

  const assembledSnapshot = latest!.authoring;
  const finalPrimary = assembledSnapshot.clips
    .filter(
      (clip) =>
        clip.trackId === primaryTrack.trackId &&
        expectation.assetIds.includes(clip.assetId as never),
    )
    .sort((left, right) => left.startFrame - right.startFrame);
  expect(finalPrimary.map((clip) => clip.assetId)).toEqual(
    expectation.assetIds,
  );
  // These coordinates are the independent fixture/edit recipe, not values copied from the
  // output receipt. A matching but incorrectly shortened export must still fail below.
  const expectedPrimary = EXPECTED_PRIMARY_PLACEMENTS;
  expect(
    finalPrimary.map(({ startFrame, sourceStartFrame, durationFrames }) => ({
      startFrame,
      sourceStartFrame,
      durationFrames,
    })),
  ).toEqual(expectedPrimary);
  const expectedOutputFrames = EXPECTED_OUTPUT_DURATION_FRAMES;
  expect(assembledSnapshot.contentEndExclusive).toBe(expectedOutputFrames);
  const zoomSelection = overlay.locator(
    '[data-h3-nle-control="transport.zoom_selection"]',
  );

  // Zoom to the assembled primary sequence. These selection transactions are normal UI actions;
  // they do not change clip geometry or export.
  await transact(
    () => select(finalPrimary[0]!.clipId).click(),
    ["select_clips"],
  );
  await transact(
    () => select(finalPrimary.at(-1)!.clipId).click({ modifiers: ["Shift"] }),
    ["select_clips"],
  );
  expect(latest!.selection).toEqual(finalPrimary.map((clip) => clip.clipId));
  if (!(await zoomSelection.isVisible())) {
    await overlay.locator('[data-h3-nle-control="toolbar.more"]').click();
  }
  await expect(zoomSelection).toBeVisible();
  await expect(zoomSelection).toBeEnabled();
  await zoomSelection.click();
  await expect
    .poll(async () => {
      const boxes = await Promise.all(
        finalPrimary.map((clip) =>
          overlay.locator(`[data-h3-nle-clip="${clip.clipId}"]`).boundingBox(),
        ),
      );
      return boxes.every((box) => box !== null && box.width >= 96);
    })
    .toBe(true);
  await transact(
    () => select(finalPrimary[0]!.clipId).click(),
    ["select_clips"],
  );
  expect(latest!.selection).toEqual([finalPrimary[0]!.clipId]);
  const finalSnapshot = latest!.authoring;
  expect(finalSnapshot.clips).toEqual(assembledSnapshot.clips);
  expect(finalSnapshot.contentEndExclusive).toBe(expectedOutputFrames);

  // Re-anchor the monitor inside the selected sequence, then retain its declared ready frame.
  const overviewFrame = 6;
  await seek(page, playhead, overviewFrame);
  await expect(overlay.locator('[data-h3-nle-status="monitor"]')).toHaveText(
    "Monitor paused.",
  );
  await expect(
    overlay.locator('[data-h3-nle-canvas="composition"]'),
  ).toHaveAttribute("data-h3-nle-presented-frame", String(overviewFrame));

  // Preserve a reviewable, populated overview rather than the transient title editor or the
  // toolbar overflow used by earlier operations. Keep the three assembled sources individually
  // reviewable, and retain a Basic inspector plus a timeline zoomed to the selected sequence.
  if ((await more.getAttribute("aria-expanded")) === "true") await more.click();
  const basicTab = overlay.locator(
    '[data-h3-nle-region="inspector"] [data-h3-nle-property-tab="basic"]',
  );
  await basicTab.click();
  await expect(basicTab).toHaveAttribute("aria-selected", "true");
  // IMPORTANT: Text selection unmounts NleMediaBin; restore Media before querying its filter,
  // or this host journey waits on a control that no longer exists and never captures its result.
  const mediaTab = overlay.locator('[data-h3-nle-pane="assets"]');
  await mediaTab.click();
  await expect(mediaTab).toHaveAttribute("aria-selected", "true");
  await expect(mediaBin).toBeVisible();
  await expect(
    mediaBin.locator('[data-h3-nle-control="media.sort_filter"]'),
  ).toBeVisible();
  await chooseMediaOption(mediaBin, "media.filter", "added");
  for (const assetId of expectation.assetIds) {
    const assetCard = overlay.locator(`[data-h3-nle-asset="${assetId}"]`);
    await expect(assetCard).toBeVisible();
    await assetCard.evaluate((element) =>
      element.scrollIntoView({
        block: "center",
        inline: "center",
        behavior: "instant",
      }),
    );
    await expectFullyVisibleThroughClipAncestors(
      assetCard,
      "added asset " + assetId,
    );
    await expect(
      assetCard.locator('[data-h3-nle-media-badge="added"]'),
    ).toBeVisible();
    await expectLoadedAssetCard(assetCard, "populated asset " + assetId);
  }
  const firstAddedAsset = overlay.locator(
    `[data-h3-nle-asset="${expectation.assetIds[0]}"]`,
  );
  await firstAddedAsset.evaluate((element) =>
    element.scrollIntoView({
      block: "center",
      inline: "center",
      behavior: "instant",
    }),
  );
  await expectFullyVisibleThroughClipAncestors(
    firstAddedAsset,
    "first added asset before populated capture",
  );
  const trackViewport = overlay.locator(".h3-nle-tracks");
  await expect(trackViewport).toBeVisible();
  // IMPORTANT: zoom_fit is horizontal only; earlier editing can leave the primary row scrolled
  // away vertically, so restore the track viewport before asserting the retained capture.
  await trackViewport.evaluate((element) => {
    element.scrollTop = 0;
  });
  expect(await trackViewport.evaluate((element) => element.scrollTop)).toBe(0);
  for (const clip of finalPrimary) {
    const clipCard = overlay.locator(`[data-h3-nle-clip="${clip.clipId}"]`);
    await expect(clipCard).toBeVisible();
    await expectFullyVisibleThroughClipAncestors(
      clipCard,
      `primary clip ${clip.clipId}`,
    );
  }
  const expectedChannels = new Map(
    expectation.sources.map((source) => {
      const expectedChannel = (["red", "green", "blue"] as const).find(
        (channel) => source.relativePath.includes(`source-${channel}-`),
      );
      if (expectedChannel === undefined)
        throw new Error("M25-53 source fixture has no expected RGB identity");
      return [source.assetId, expectedChannel] as const;
    }),
  );
  const compositeClipInputs = finalPrimary.map((clip) => {
    const expectedChannel = expectedChannels.get(clip.assetId ?? "");
    if (expectedChannel === undefined)
      throw new Error(`${clip.clipId} has no pinned source-color identity`);
    return { clipId: clip.clipId, expectedChannel };
  });
  const waveformColour = await resolveWaveformColour(overlay);
  observerEvidence.waveformColour = {
    ...waveformColour,
    tolerance: WAVEFORM_COLOUR_TOLERANCE,
  };
  await recordObserverEvidence();
  // M25-64: every composited reading records the rectangle it sampled, and a region that cannot
  // decide the filmstrip and waveform assertions fails here as an observer failure.
  const readCompositedObserved = async (label: string, screenshot: Buffer) => {
    const pixels = await readCompositedClipDecorationPixels(
      page,
      overlay,
      screenshot,
      compositeClipInputs,
      waveformColour,
    );
    observerEvidence.compositedRegions.push({
      reading: label,
      regions: pixels.map(({ clipId, region }) => ({ clipId, ...region })),
    });
    await recordObserverEvidence();
    // B-M2564-13: a waveform colour rule that matches filmstrip pixels would count a source frame.
    const confused = pixels.filter(
      (entry) =>
        entry.region.reason === null && entry.filmstripWaveformColourPixels > 0,
    );
    if (confused.length > 0)
      throw new Error(
        `observer failure: the ${label} composited reading's waveform colour matches filmstrip pixels of ` +
          confused
            .map(
              (entry) =>
                `${entry.clipId} (${entry.filmstripWaveformColourPixels})`,
            )
            .join(", "),
      );
    const invalid = pixels.filter((entry) => entry.region.reason !== null);
    if (invalid.length > 0)
      throw new Error(
        `observer failure: the ${label} composited reading has no valid sampling region for ` +
          invalid
            .map(
              (entry) =>
                `${entry.clipId} (${entry.region.reason}; ${JSON.stringify(entry.region.usable)})`,
            )
            .join(", "),
      );
    return pixels;
  };
  // IMPORTANT: outlines, ruler ticks, and in-flight waveform strokes share the same backing
  // canvas. A few matching pixels are not proof that the final audio-peaks publication painted;
  // gate capture on the same minimum used by the composed acceptance assertion below.
  try {
    await expect
      .poll(
        async () => {
          const pixels = await readClipDecorationPixels(
            overlay,
            compositeClipInputs,
            waveformColour,
          );
          return pixels.every(
            ({ filmstripSourcePixels, waveformColourPixels }) =>
              filmstripSourcePixels > 0 &&
              waveformColourPixels >= MIN_COMPOSED_WAVEFORM_PIXELS,
          );
        },
        {
          message:
            "each populated clip must paint its pinned filmstrip and waveform into the timeline raster",
          timeout: 15_000,
        },
      )
      .toBe(true);
  } catch (error) {
    const failureBoundary = {
      epochMs: Date.now(),
      monotonicMs: Number(performance.now().toFixed(3)),
      networkEventCount: leaseEvents.length,
    };
    const schedulerEvents = await page.evaluate(() => {
      const target = window as Window & {
        __h3ContextNleSchedulerTrace?: Array<Record<string, unknown>>;
        __h3NleWaveformDrawTrace?: Array<Readonly<Record<string, unknown>>>;
        __h3NleWaveformCacheTrace?: Array<Readonly<Record<string, unknown>>>;
        __h3NleWaveformSceneTrace?: Array<Readonly<Record<string, unknown>>>;
        __h3NleWaveformPaintTrace?: Array<Readonly<Record<string, unknown>>>;
        __h3NleWaveformCallTrace?: Array<Readonly<Record<string, unknown>>>;
        __h3NleWaveformStrokeTrace?: Array<Readonly<Record<string, unknown>>>;
        __h3NleSchedulerTraceEnabled?: boolean;
        __h3NleCanvasStrokeProbe?: Readonly<Record<string, unknown>>;
      };
      const canvas = document.querySelector<HTMLCanvasElement>(
        '[data-h3-nle-canvas="timeline_decoration"]',
      );
      const bounds = canvas?.getBoundingClientRect();
      return {
        scheduler: target.__h3ContextNleSchedulerTrace?.slice() ?? [],
        strokes: target.__h3NleWaveformDrawTrace?.slice() ?? [],
        waveformCache: target.__h3NleWaveformCacheTrace?.slice() ?? [],
        waveformScene: target.__h3NleWaveformSceneTrace?.slice() ?? [],
        waveformPaint: target.__h3NleWaveformPaintTrace?.slice() ?? [],
        waveformCall: target.__h3NleWaveformCallTrace?.slice() ?? [],
        waveformStroke: target.__h3NleWaveformStrokeTrace?.slice() ?? [],
        diagnosticGate: {
          locationSearch: window.location.search,
          injectedFlag: target.__h3NleSchedulerTraceEnabled === true,
        },
        strokeProbe: target.__h3NleCanvasStrokeProbe ?? null,
        canvas: canvas
          ? {
              width: canvas.width,
              height: canvas.height,
              cssWidth: bounds?.width ?? null,
              cssHeight: bounds?.height ?? null,
              left: bounds?.left ?? null,
              top: bounds?.top ?? null,
            }
          : null,
      };
    });
    const schedulerTrace = schedulerEvents.scheduler;
    const paintTrace = {
      canvas: schedulerEvents.canvas,
      waveformCache: schedulerEvents.waveformCache.slice(-80),
      waveformScene: schedulerEvents.waveformScene.slice(-80),
      waveformPaint: schedulerEvents.waveformPaint.slice(-32),
      waveformCall: schedulerEvents.waveformCall.slice(-96),
      waveformStroke: schedulerEvents.waveformStroke.slice(-24),
      diagnosticGate: schedulerEvents.diagnosticGate,
      strokeProbe: schedulerEvents.strokeProbe,
      waveformStrokeCount: schedulerEvents.strokes.filter(
        (stroke) => stroke.isWaveformStyle === true,
      ).length,
      waveformStrokes: schedulerEvents.strokes.filter(
        (stroke) => stroke.isWaveformStyle === true,
      ),
      recentTimelineStrokes: schedulerEvents.strokes.slice(-32),
    };
    const eventTimeline: Array<Record<string, unknown>> = [
      ...leaseEvents.map((event) => ({
        producer: "lease-network",
        ...event,
        sourceSequence: event.sequence,
      })),
      ...schedulerTrace.map((event) => ({
        producer: "lease-scheduler",
        ...event,
        sourceSequence: event.sequence,
      })),
    ]
      .sort(
        (left: Record<string, unknown>, right: Record<string, unknown>) =>
          Number(left.epochMs ?? 0) - Number(right.epochMs ?? 0) ||
          Number(left.monotonicMs ?? 0) - Number(right.monotonicMs ?? 0),
      )
      .map((event, index) => ({ eventSequence: index + 1, ...event }));
    const ownerPairs = [
      ...new Set(
        leaseEvents
          .map((event) => event.ownerAlias)
          .filter((alias): alias is string => typeof alias === "string"),
      ),
    ].map((ownerAlias) => {
      const events = leaseEvents.filter(
        (event) => event.ownerAlias === ownerAlias,
      );
      const creates = events.filter(
        (event) => event.event === "response" && event.operation === "create",
      );
      const releases = events.filter(
        (event) => event.event === "response" && event.operation === "release",
      );
      return {
        ownerAlias,
        derivativeKind:
          events.find((event) => typeof event.derivativeKind === "string")
            ?.derivativeKind ?? null,
        createResponses: creates.length,
        successfulCreateResponses: creates.filter(
          (event) => event.status === 200,
        ).length,
        releaseResponses: releases.length,
        successfulReleaseResponses: releases.filter(
          (event) => event.status === 200,
        ).length,
        requestFailures: events.filter(
          (event) => event.event === "requestfailed",
        ).length,
        aborts: events.filter(
          (event) =>
            event.event === "requestfailed" &&
            event.abortCause === "request_abort",
        ).length,
      };
    });
    await Promise.all(decorationResponseTasks);
    const viewport = page.viewportSize();
    const rawRasterPixels = await readClipDecorationPixels(
      overlay,
      compositeClipInputs,
      waveformColour,
    );
    const layout = await readClipDecorationLayout(
      overlay,
      compositeClipInputs,
      waveformColour,
    );
    let failedCapture: OwnedCapture | null = null;
    let preFixCssCapture: OwnedCapture | null = null;
    let captureDimensions: Readonly<{ width: number; height: number }> | null =
      null;
    let captureFailure: string | null = null;
    let sameScreenCssPixelDiff: Readonly<Record<string, number>> | null = null;
    try {
      const png = await overlay.screenshot();
      captureDimensions = pngDimensions(png);
      failedCapture = await attachOwnedViewportCapture(
        testInfo,
        "populated-composite-decoration-failure",
        png,
        candidateBundle.sha256,
        evidenceRunId,
      );
      await page.addStyleTag({ content: PRE_FIX_CLIP_BODY_LAYER_RULE });
      await page.evaluate(
        () =>
          new Promise<void>((resolve) =>
            requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
          ),
      );
      const legacyPng = await overlay.screenshot();
      preFixCssCapture = await attachOwnedViewportCapture(
        testInfo,
        "populated-composite-pre-fix-clip-body-layer",
        legacyPng,
        candidateBundle.sha256,
        evidenceRunId,
      );
      sameScreenCssPixelDiff = await page.evaluate(
        async ({ fixedBase64, legacyBase64 }) => {
          const read = async (base64: string) => {
            const image = new Image();
            image.src = `data:image/png;base64,${base64}`;
            await image.decode();
            const canvas = document.createElement("canvas");
            canvas.width = image.naturalWidth;
            canvas.height = image.naturalHeight;
            const context = canvas.getContext("2d", {
              willReadFrequently: true,
            });
            if (context === null)
              throw new Error("CSS screenshot pixel diff is unavailable");
            context.drawImage(image, 0, 0);
            return {
              width: canvas.width,
              height: canvas.height,
              pixels: context.getImageData(0, 0, canvas.width, canvas.height)
                .data,
            };
          };
          const fixed = await read(fixedBase64);
          const legacy = await read(legacyBase64);
          if (fixed.width !== legacy.width || fixed.height !== legacy.height)
            throw new Error("same-state CSS captures have different sizes");
          let changedPixelCount = 0;
          for (let offset = 0; offset < fixed.pixels.length; offset += 4) {
            if (
              fixed.pixels[offset] !== legacy.pixels[offset] ||
              fixed.pixels[offset + 1] !== legacy.pixels[offset + 1] ||
              fixed.pixels[offset + 2] !== legacy.pixels[offset + 2] ||
              fixed.pixels[offset + 3] !== legacy.pixels[offset + 3]
            )
              changedPixelCount += 1;
          }
          return {
            width: fixed.width,
            height: fixed.height,
            changedPixelCount,
          };
        },
        {
          fixedBase64: png.toString("base64"),
          legacyBase64: legacyPng.toString("base64"),
        },
      );
    } catch (captureError) {
      captureFailure =
        captureError instanceof Error ? captureError.name : "unknown_error";
    }
    const diagnostic = {
      schema: "M25_53PopulatedDecorationFailureV1",
      candidateBundleSha256: candidateBundle.sha256,
      failureBoundary,
      schedulerTraceEnabled,
      schedulerEvents: schedulerTrace,
      paintTrace,
      ownerPairs,
      eventTimeline,
      viewport,
      ownedSurfaceCrop: captureDimensions,
      screenshot: failedCapture,
      preFixCssScreenshot: preFixCssCapture,
      sameScreenCssPixelDiff,
      screenshotFailure: captureFailure,
      rawRasterPixels: rawRasterPixels.map(
        ({ clipId: _clipId, ...pixels }, index) => ({
          slot: index + 1,
          ...pixels,
        }),
      ),
      finalAssetAdmission: expectation.assetIds.map((assetId, index) => {
        const asset = finalSnapshot.assets.find(
          (candidate) => candidate.assetId === assetId,
        );
        const primaryTrack = finalSnapshot.tracks.find(
          (track) => track.kind === "primary_video",
        );
        const clipCount = finalPrimary.filter(
          (clip) => clip.assetId === assetId,
        ).length;
        return {
          slot: index + 1,
          kind: asset?.kind ?? null,
          sourceFrameCount: asset?.sourceFrameCount ?? null,
          landmarkCount: asset?.landmarks.length ?? null,
          sourceSampleCount: asset?.sourceSampleCount ?? null,
          embeddedAudio: asset?.embeddedAudio ?? null,
          primaryTrackEnabled: primaryTrack?.enabled ?? null,
          clipCount,
        };
      }),
      waveformInputFacts: finalPrimary.map((clip, index) => {
        const clipLayout = Array.isArray(layout.clips)
          ? (layout.clips[index] as
              | {
                  bounds?: {
                    canvasCss?: { left?: number; width?: number };
                  };
                }
              | undefined)
          : undefined;
        const asset = finalSnapshot.assets.find(
          (candidate) => candidate.assetId === clip.assetId,
        );
        const firstLandmark = asset?.landmarks[clip.sourceStartFrame] ?? null;
        const lastLandmark =
          asset?.landmarks[clip.sourceStartFrame + clip.durationFrames - 1] ??
          null;
        const sourceStartTick = firstLandmark?.pts ?? null;
        const sourceEndTick =
          lastLandmark === null
            ? null
            : lastLandmark.pts + lastLandmark.durationTicks;
        const timeBase = asset?.sourceTimeBase ?? null;
        const pairRate = AUTHORING_AUDIO_PEAKS_PAIR_RATE;
        return {
          slot: index + 1,
          timelineStartFrame: clip.startFrame,
          sourceStartFrame: clip.sourceStartFrame,
          durationFrames: clip.durationFrames,
          canvasClipX: clipLayout?.bounds?.canvasCss?.left ?? null,
          canvasClipWidth: clipLayout?.bounds?.canvasCss?.width ?? null,
          sourceTimeBase: timeBase,
          sourceSampleCount: asset?.sourceSampleCount ?? null,
          sourceFrameCount: asset?.sourceFrameCount ?? null,
          landmarkCount: asset?.landmarks.length ?? null,
          sourceStartTick,
          sourceEndTick,
          expectedStartPair:
            sourceStartTick === null || timeBase === null
              ? null
              : Math.floor(
                  (sourceStartTick * timeBase.num * pairRate) / timeBase.den,
                ),
          expectedEndPair:
            sourceEndTick === null || timeBase === null
              ? null
              : Math.ceil(
                  (sourceEndTick * timeBase.num * pairRate) / timeBase.den,
                ),
        };
      }),
      decorationLeaseResponses,
      layout,
    };
    const body = JSON.stringify(diagnostic, null, 2);
    const retainedName = await retainDecorationFailureDiagnostic(
      body,
      candidateBundle.sha256,
      evidenceRunId,
    );
    await testInfo.attach(retainedName, {
      body,
      contentType: "application/json",
    });
    throw error;
  }
  const populatedDecorationPixels = await readClipDecorationPixels(
    overlay,
    compositeClipInputs,
    waveformColour,
  );
  expect(populatedDecorationPixels).toHaveLength(finalPrimary.length);
  for (const pixels of populatedDecorationPixels) {
    // B-M2564-13: the observer's own check first -- its waveform colour matches no filmstrip pixel.
    expect(
      pixels.filmstripWaveformColourPixels,
      pixels.clipId +
        " observer check: the waveform colour matches no pixel of the filmstrip rows",
    ).toBe(0);
    expect(
      pixels.filmstripSourcePixels,
      pixels.clipId + " pinned source-color filmstrip in backing raster",
    ).toBeGreaterThan(0);
    expect(
      pixels.waveformColourPixels,
      pixels.clipId + " waveform",
    ).toBeGreaterThan(0);
  }
  await expect(more).toHaveAttribute("aria-expanded", "false");

  const viewportSize = page.viewportSize();
  if (viewportSize === null)
    throw new Error("M25-53 reference viewport is unavailable");
  expect(viewportSize, "reference viewport identity").toEqual({
    width: 1402,
    height: 868,
  });
  const populatedCapture = await overlay.screenshot();
  const populatedOwnedSurfaceSize = pngDimensions(populatedCapture);
  expect(populatedOwnedSurfaceSize.width).toBeLessThan(viewportSize.width);
  expect(populatedOwnedSurfaceSize.height).toBeLessThan(viewportSize.height);
  const populatedOwnedCapture = await attachOwnedViewportCapture(
    testInfo,
    "populated-composite-current-css-before-frame-barrier",
    populatedCapture,
    candidateBundle.sha256,
    evidenceRunId,
  );
  captures.push(populatedOwnedCapture);
  const initialCompositeDecorationPixels = await readCompositedObserved(
    "initial current CSS",
    populatedCapture,
  );
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
      ),
  );
  const currentCssStableCapture = await overlay.screenshot();
  const currentCssStableOwnedCapture = await attachOwnedViewportCapture(
    testInfo,
    "populated-composite-current-css-stable-control",
    currentCssStableCapture,
    candidateBundle.sha256,
    evidenceRunId,
  );
  captures.push(currentCssStableOwnedCapture);
  const currentCssStableBackingRasterPixels = await readClipDecorationPixels(
    overlay,
    compositeClipInputs,
    waveformColour,
  );
  const populatedCompositeDecorationPixels = await readCompositedObserved(
    "stable current CSS",
    currentCssStableCapture,
  );
  const preFixCssOverride = await page.addStyleTag({
    content: PRE_FIX_CLIP_BODY_LAYER_RULE,
  });
  let preFixCapture: Buffer | null = null;
  let preFixOwnedCapture: OwnedCapture | null = null;
  let preFixCompositedDecorationPixels:
    readonly CompositedClipDecorationPixels[] | null = null;
  try {
    await page.evaluate(
      () =>
        new Promise<void>((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
        ),
    );
    preFixCapture = await overlay.screenshot();
    preFixCompositedDecorationPixels = await readCompositedObserved(
      "pre-fix CSS ablation",
      preFixCapture,
    );
    preFixOwnedCapture = await attachOwnedViewportCapture(
      testInfo,
      "populated-composite-pre-fix-clip-body-layer",
      preFixCapture,
      candidateBundle.sha256,
      evidenceRunId,
    );
    captures.push(preFixOwnedCapture);
  } finally {
    await preFixCssOverride.evaluate((style) =>
      style.parentNode?.removeChild(style),
    );
    await page.evaluate(
      () =>
        new Promise<void>((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
        ),
    );
  }
  if (
    preFixCapture === null ||
    preFixOwnedCapture === null ||
    preFixCompositedDecorationPixels === null
  )
    throw new Error("pre-fix CSS populated-state comparison was not captured");
  const sameScreenCssPixelDiff = await compareOwnedScreenshotPixels(
    page,
    currentCssStableCapture,
    preFixCapture,
  );
  const sameScreenCssAblation = {
    preFixRule: "clip-body static/auto; child text relative/z-index:5",
    viewport: viewportSize,
    ownedSurfaceCrop: populatedOwnedSurfaceSize,
    initialCurrentCssScreenshot: populatedOwnedCapture,
    initialCurrentCssPixels: initialCompositeDecorationPixels,
    currentCssScreenshot: currentCssStableOwnedCapture,
    currentCssBackingRasterPixels: currentCssStableBackingRasterPixels,
    preFixCssScreenshot: preFixOwnedCapture,
    currentCssPixels: populatedCompositeDecorationPixels,
    preFixCssPixels: preFixCompositedDecorationPixels,
    fullCropPixelDiff: sameScreenCssPixelDiff,
  };
  if (
    populatedCompositeDecorationPixels.some(
      (pixels) =>
        pixels.filmstripSourcePixels === 0 ||
        pixels.waveformColourPixels < MIN_COMPOSED_WAVEFORM_PIXELS,
    )
  ) {
    await Promise.all(decorationResponseTasks);
    const compositionLayout = await readClipDecorationLayout(
      overlay,
      compositeClipInputs,
      waveformColour,
      populatedOwnedSurfaceSize,
    );
    const schedulerEvents = await page.evaluate(() => {
      const target = window as Window & {
        __h3ContextNleSchedulerTrace?: Array<Record<string, unknown>>;
        __h3NleWaveformDrawTrace?: Array<Readonly<Record<string, unknown>>>;
        __h3NleWaveformCacheTrace?: Array<Readonly<Record<string, unknown>>>;
        __h3NleWaveformSceneTrace?: Array<Readonly<Record<string, unknown>>>;
        __h3NleWaveformPaintTrace?: Array<Readonly<Record<string, unknown>>>;
        __h3NleWaveformCallTrace?: Array<Readonly<Record<string, unknown>>>;
        __h3NleWaveformStrokeTrace?: Array<Readonly<Record<string, unknown>>>;
        __h3NleCanvasStrokeProbe?: Readonly<Record<string, unknown>>;
      };
      return {
        scheduler: target.__h3ContextNleSchedulerTrace?.slice() ?? [],
        waveformDraws: target.__h3NleWaveformDrawTrace?.slice() ?? [],
        waveformCache: target.__h3NleWaveformCacheTrace?.slice(-80) ?? [],
        waveformScene: target.__h3NleWaveformSceneTrace?.slice(-80) ?? [],
        waveformPaint: target.__h3NleWaveformPaintTrace?.slice(-32) ?? [],
        waveformCall: target.__h3NleWaveformCallTrace?.slice(-96) ?? [],
        waveformStroke: target.__h3NleWaveformStrokeTrace?.slice(-24) ?? [],
        strokeProbe: target.__h3NleCanvasStrokeProbe ?? null,
      };
    });
    const assetInputs = finalPrimary.map((clip, index) => {
      const asset = finalSnapshot.assets.find(
        (candidate) => candidate.assetId === clip.assetId,
      );
      return {
        slot: index + 1,
        sourceTimeBase: asset?.sourceTimeBase ?? null,
        sourceSampleCount: asset?.sourceSampleCount ?? null,
        sourceFrameCount: asset?.sourceFrameCount ?? null,
        embeddedAudio: asset?.embeddedAudio ?? null,
        clipSourceStartFrame: clip.sourceStartFrame,
        clipDurationFrames: clip.durationFrames,
        firstLandmark: asset?.landmarks[clip.sourceStartFrame] ?? null,
        lastLandmark:
          asset?.landmarks[clip.sourceStartFrame + clip.durationFrames - 1] ??
          null,
      };
    });
    const compositionFailure = JSON.stringify(
      {
        schema: "M25_53PopulatedDecorationCompositeFailureV1",
        candidateBundleSha256: candidateBundle.sha256,
        failureBoundary: "same_page_composited_crop",
        viewport: viewportSize,
        ownedSurfaceCrop: populatedOwnedSurfaceSize,
        backingRasterPixels: populatedDecorationPixels,
        initialComposedPixels: initialCompositeDecorationPixels,
        currentCssBackingRasterPixels: currentCssStableBackingRasterPixels,
        composedPixels: populatedCompositeDecorationPixels,
        sameScreenCssAblation,
        schedulerTraceEnabled,
        schedulerEvents: schedulerEvents.scheduler,
        waveformCache: schedulerEvents.waveformCache,
        waveformScene: schedulerEvents.waveformScene,
        waveformPaint: schedulerEvents.waveformPaint,
        waveformCall: schedulerEvents.waveformCall,
        waveformStroke: schedulerEvents.waveformStroke,
        strokeProbe: schedulerEvents.strokeProbe,
        waveformDraws: schedulerEvents.waveformDraws,
        audioPeaksLeaseEvents: leaseEvents.filter(
          (event) => event.decorationKind === "audio_peaks",
        ),
        assetInputs,
        compositionLayout,
      },
      null,
      2,
    );
    const retainedName = await retainDecorationFailureDiagnostic(
      compositionFailure,
      candidateBundle.sha256,
      evidenceRunId,
    );
    await testInfo.attach(retainedName, {
      body: compositionFailure,
      contentType: "application/json",
    });
  }
  expect(populatedCompositeDecorationPixels).toHaveLength(finalPrimary.length);
  for (const pixels of populatedCompositeDecorationPixels) {
    expect(
      pixels.filmstripSourcePixels,
      pixels.clipId + " source-color filmstrip pixels in composed crop",
    ).toBeGreaterThan(0);
    expect(
      pixels.waveformColourPixels,
      pixels.clipId + " waveform-colour pixels in composed crop",
    ).toBeGreaterThanOrEqual(MIN_COMPOSED_WAVEFORM_PIXELS);
  }

  await openExportPanel(overlay);
  const render = overlay.locator('[data-h3-nle-region="render"]');
  const outputStatuses: ReturnType<typeof decodeOutputStatus>[] = [];
  const responseTasks: Promise<void>[] = [];
  const onResponse = (response: any) => {
    const path = new URL(response.url()).pathname.replace(
      /^\/api(?=\/h3-context\/)/,
      "",
    );
    if (
      response.request().method() === "GET" &&
      (path === OUTPUT_CAPABILITY.job_path ||
        path.startsWith(`${OUTPUT_CAPABILITY.job_path}/`)) &&
      response.ok()
    )
      responseTasks.push(
        response
          .json()
          .then((body: unknown) =>
            outputStatuses.push(decodeOutputStatus(body)),
          ),
      );
  };
  page.on("response", onResponse);
  await render
    .getByRole("button", { name: "Render final video", exact: true })
    .click();
  await expect(render.getByRole("status").first()).toHaveText("Video ready", {
    timeout: 900_000,
  });
  await Promise.all(responseTasks);
  const outputStatus = [...outputStatuses]
    .reverse()
    .find(
      (row) =>
        row.phase === "succeeded" && row.workspace_handle === authoringHandle,
    );
  if (
    outputStatus?.output_handle === null ||
    outputStatus?.output_handle === undefined
  )
    throw new Error("successful output status is absent");
  expect(outputStatus.timeline_revision).toBe(finalSnapshot.timelineRevision);
  const downloadEvent = page.waitForEvent("download", { timeout: 120_000 });
  await render
    .getByRole("link", { name: "Download original", exact: true })
    .click();
  const download = await downloadEvent;
  const downloadPath = testInfo.outputPath("m25-53-download.mp4");
  await mkdir(dirname(downloadPath), { recursive: true });
  await download.saveAs(downloadPath);
  expect(await download.failure()).toBeNull();
  const downloaded = await readFile(downloadPath);
  const downloadedSha256 = sha256(downloaded);
  expect(`sha256:${downloadedSha256}`).toBe(
    outputStatus.output!.output_fingerprint,
  );
  expect(downloaded.length).toBe(outputStatus.output!.byte_length);
  // The driver's temporary Playwright output directory does not survive the row, so the exact
  // downloaded bytes are also kept beside the owned-surface captures (the same confined,
  // ignored evidence tree), with the run identity so independent attempts never overwrite.
  const retainedDownload = await retainDownloadedExport(
    downloaded,
    candidateBundle.sha256,
    evidenceRunId,
  );

  // The observers run outside the product and read the downloaded bytes. They derive samples from
  // the accepted final receipt plus independently pinned source files; no UI canvas value is reused.
  const mediaProbe = probe(ffprobe, downloadPath);
  const videoStream = mediaProbe.streams.find(
    (stream) => stream.codec_type === "video",
  );
  const audioStream = mediaProbe.streams.find(
    (stream) => stream.codec_type === "audio",
  );
  expect(videoStream?.r_frame_rate).toBe("24/1");
  expect(Number(videoStream?.nb_read_frames)).toBe(expectedOutputFrames);
  expect(outputStatus.output!.frame_count).toBe(expectedOutputFrames);
  expect(Number(audioStream?.sample_rate)).toBe(48_000);
  expect(Number(audioStream?.channels)).toBeGreaterThanOrEqual(1);
  const expectedAudioSamples = (expectedOutputFrames * 48_000) / 24;
  expect(audioStream?.time_base).toBe("1/48000");
  expect(Number(audioStream?.duration_ts)).toBe(expectedAudioSamples);
  // IMPORTANT: do not round playable PCM to the AAC block size. The container's discard padding
  // clips the final coded block, and the candidate-bound decoder returns that exact playable
  // extent. Rounding here rejects a correct export whenever the timeline ends mid-block.
  const expectedAudioExtentSamples = expectedAudioSamples;
  const outputPcm = decodedAudioSamples48k(ffmpeg, downloadPath);
  const decodedAudioSamples = outputPcm.length;
  expect(
    Math.abs(decodedAudioSamples - expectedAudioExtentSamples),
  ).toBeLessThanOrEqual(1);

  const outputWidth = Number(videoStream?.width);
  const outputHeight = Number(videoStream?.height);
  expect(outputWidth).toBeGreaterThan(0);
  expect(outputHeight).toBeGreaterThan(0);
  expect(outputWidth / outputHeight).toBeCloseTo(16 / 9, 3);
  // Where each primary source lands on the output follows the accepted placement contract
  // applied to the clip's own declared crop/transform (identity here: native size, centred).
  const primaryPlacements = finalPrimary.map((clip) =>
    clipPlacement(
      clip as unknown as WireClipPlacement,
      expectation.landmark.sourceWidth,
      expectation.landmark.sourceHeight,
      outputWidth,
      outputHeight,
    ),
  );
  const primaryRegions = primaryPlacements.map(placedRegion);
  for (const region of primaryRegions) {
    expect(region.left).toBeGreaterThanOrEqual(0);
    expect(region.top).toBeGreaterThanOrEqual(0);
    expect(region.left + region.width).toBeLessThanOrEqual(outputWidth);
    expect(region.top + region.height).toBeLessThanOrEqual(outputHeight);
  }
  const landmarkSamples = expectedPrimary.flatMap((clip, index) =>
    [6, 32].map((offset) => {
      const placement = primaryPlacements[index]!;
      const sourceFrame = clip.sourceStartFrame + offset;
      const segment = Math.floor(sourceFrame / 24);
      const markerX = expectation.landmark.centerXBySecond[segment]!;
      const otherX =
        expectation.landmark.centerXBySecond[segment === 0 ? 1 : 0]!;
      const markerY = expectation.landmark.centerY;
      const scale = Math.min(
        placement.scaledWidth / placement.cropWidth,
        placement.scaledHeight / placement.cropHeight,
      );
      const edge = Math.max(5, Math.floor(10 * scale));
      const marker = toOutputPoint(placement, markerX, markerY);
      const other = toOutputPoint(placement, otherX, markerY);
      const sourceMarker = interiorRgb(
        ffmpeg,
        sourcePaths[index]!,
        sourceFrame,
        markerX,
        markerY,
        10,
      );
      const outputMarker = interiorRgb(
        ffmpeg,
        downloadPath,
        clip.startFrame + offset,
        marker.x,
        marker.y,
        edge,
      );
      const sourceOther = interiorRgb(
        ffmpeg,
        sourcePaths[index]!,
        sourceFrame,
        otherX,
        markerY,
        10,
      );
      const outputOther = interiorRgb(
        ffmpeg,
        downloadPath,
        clip.startFrame + offset,
        other.x,
        other.y,
        edge,
      );
      expect(
        Math.min(sourceMarker.red, sourceMarker.green, sourceMarker.blue),
      ).toBeGreaterThan(230);
      expect(
        maximumChannelError(outputMarker, sourceMarker),
      ).toBeLessThanOrEqual(8);
      expect(maximumChannelError(outputOther, sourceOther)).toBeLessThanOrEqual(
        8,
      );
      const exactPatch = frameIndexPatch(
        ffmpeg,
        sourcePaths[index]!,
        sourceFrame,
      );
      const precedingPatch = frameIndexPatch(
        ffmpeg,
        sourcePaths[index]!,
        sourceFrame - 1,
      );
      const followingPatch = frameIndexPatch(
        ffmpeg,
        sourcePaths[index]!,
        sourceFrame + 1,
      );
      const deliveredPatch = frameIndexPatch(
        ffmpeg,
        downloadPath,
        clip.startFrame + offset,
        primaryRegions[index]!,
      );
      const exactError = meanAbsoluteByteError(deliveredPatch, exactPatch);
      const adjacentError = Math.min(
        meanAbsoluteByteError(deliveredPatch, precedingPatch),
        meanAbsoluteByteError(deliveredPatch, followingPatch),
      );
      // The per-frame index is independent of the editor state; either adjacent source frame
      // must be observably worse than the commanded exact frame, including one-frame slips.
      expect(exactError).toBeLessThanOrEqual(8);
      expect(adjacentError - exactError).toBeGreaterThan(2);
      expect(rgbDistance(outputMarker, outputOther)).toBeGreaterThan(80);
      return {
        assetId: expectation.assetIds[index],
        sourceFrame,
        outputFrame: clip.startFrame + offset,
        sourceMarker,
        outputMarker,
        sourceOther,
        outputOther,
        exactFrameIndexError: exactError,
        adjacentFrameIndexError: adjacentError,
      };
    }),
  );

  const sourceSamples = sourcePaths.map((path, index) => {
    const clip = finalPrimary[index]!;
    const offset = Math.max(1, Math.floor(clip.durationFrames / 2));
    return meanRgb(ffmpeg, path, clip.sourceStartFrame + offset);
  });
  expect(
    Math.min(
      rgbDistance(sourceSamples[0]!, sourceSamples[1]!),
      rgbDistance(sourceSamples[1]!, sourceSamples[2]!),
      rgbDistance(sourceSamples[0]!, sourceSamples[2]!),
    ),
  ).toBeGreaterThan(24);
  const outputSamples = finalPrimary.map((clip, index) =>
    meanRgb(
      ffmpeg,
      downloadPath,
      clip.startFrame + Math.max(1, Math.floor(clip.durationFrames / 2)),
      primaryRegions[index]!,
    ),
  );
  const nearestSourceOrder = outputSamples.map(
    (sample) =>
      sourceSamples
        .map((source, index) => ({
          index,
          distance: rgbDistance(sample, source),
        }))
        .sort((left, right) => left.distance - right.distance)[0]!.index,
  );
  expect(nearestSourceOrder).toEqual([0, 1, 2]);
  expect(
    outputSamples.every(
      (sample, index) => rgbDistance(sample, sourceSamples[index]!) <= 36,
    ),
  ).toBe(true);
  const sourceRangeSamples = finalPrimary.map((clip, index) =>
    [6, Math.floor(clip.durationFrames / 3)].map((offset) => {
      const source = meanRgb(
        ffmpeg,
        sourcePaths[index]!,
        clip.sourceStartFrame + offset,
      );
      const output = meanRgb(
        ffmpeg,
        downloadPath,
        clip.startFrame + offset,
        primaryRegions[index]!,
      );
      expect(rgbDistance(output, source)).toBeLessThanOrEqual(36);
      return {
        assetId: clip.assetId,
        sourceFrame: clip.sourceStartFrame + offset,
        outputFrame: clip.startFrame + offset,
        source,
        output,
      };
    }),
  );
  const openingSilence = finalPrimary.map((clip, index) => {
    const source = audioWindow(
      ffmpeg,
      sourcePaths[index]!,
      clip.sourceStartFrame,
    );
    const output = audioWindow(ffmpeg, downloadPath, clip.startFrame);
    expect(audioRms(source)).toBeLessThan(0.003);
    expect(audioRms(output)).toBeLessThan(0.008);
    expect(audioPeak(source)).toBeLessThanOrEqual(1);
    // The output is AAC-coded: the encoder smears the previous clip's tone across a hard cut
    // by less than one 1,024-sample frame, so digital silence is a property of the window past
    // the same 2,048-sample codec margin the trailing-gap check skips, not of its boundary
    // sample. The boundary-sample peak is recorded, not asserted; the RMS bound above still
    // covers the whole window.
    const startSample = clip.startFrame * 2_000 + 2_048;
    const endSample = Math.min(
      clip.startFrame * 2_000 + 7_680,
      outputPcm.length - 2_048,
    );
    const outputPeak = pcmPeakInRange(outputPcm, startSample, endSample);
    expect(
      outputPeak,
      `clip ${index} opening silence past the codec margin`,
    ).toBeLessThanOrEqual(1);
    return {
      assetId: clip.assetId,
      sourceRms: audioRms(source),
      outputRms: audioRms(output),
      outputBoundaryPeak: audioPeak(output),
      outputPeak,
    };
  });
  const audioOnsets = finalPrimary.map((clip, index) => {
    const sourcePcm = decodedAudioSamples48k(ffmpeg, sourcePaths[index]!);
    const sourceStartSample = clip.sourceStartFrame * 2_000;
    const outputStartSample = clip.startFrame * 2_000;
    const expectedSourceOnset = sourceStartSample + 12_000;
    const sourceOnset = audioOnsetSample(sourcePcm, sourceStartSample);
    const outputOnset = audioOnsetSample(outputPcm, outputStartSample);
    const expectedOutputOnset =
      outputStartSample + sourceOnset - sourceStartSample;
    expect(Math.abs(sourceOnset - expectedSourceOnset)).toBeLessThanOrEqual(
      2_048,
    );
    expect(Math.abs(outputOnset - expectedOutputOnset)).toBeLessThanOrEqual(
      2_048,
    );
    return {
      assetId: clip.assetId,
      expectedSourceOnset,
      sourceOnset,
      expectedOutputOnset,
      outputOnset,
    };
  });
  const tailToneFrame =
    finalPrimary.at(-1)!.startFrame + finalPrimary.at(-1)!.durationFrames - 5;
  const tailTone = audioWindow(ffmpeg, downloadPath, tailToneFrame);
  expect(audioRms(tailTone)).toBeGreaterThan(0.005);
  const finalContentFrame = expectedOutputFrames - 1;
  expect(
    finalPrimary.at(-1)!.startFrame + finalPrimary.at(-1)!.durationFrames - 1,
  ).toBe(finalContentFrame);
  expect(audioRms(tailTone)).toBeGreaterThan(0.005);
  const audioSamples = finalPrimary.map((clip, index) => {
    const offset = Math.floor(clip.durationFrames / 2);
    const source = audioWindow(
      ffmpeg,
      sourcePaths[index]!,
      clip.sourceStartFrame + offset,
    );
    const output = audioWindow(ffmpeg, downloadPath, clip.startFrame + offset);
    const sourceRms = audioRms(source);
    const outputRms = audioRms(output);
    expect(sourceRms).toBeGreaterThan(0.01);
    expect(outputRms).toBeGreaterThan(0.005);
    expect(outputRms / sourceRms).toBeGreaterThan(0.35);
    expect(outputRms / sourceRms).toBeLessThan(1.65);
    const sourcePowers = expectation.audioHz.map((hz) => tonePower(source, hz));
    const outputPowers = expectation.audioHz.map((hz) => tonePower(output, hz));
    expect(sourcePowers[index]!).toBeGreaterThan(
      2 *
        Math.max(...sourcePowers.filter((_, candidate) => candidate !== index)),
    );
    expect(outputPowers[index]!).toBeGreaterThan(
      2 *
        Math.max(...outputPowers.filter((_, candidate) => candidate !== index)),
    );
    return {
      assetId: clip.assetId,
      sourceFrame: clip.sourceStartFrame + offset,
      outputFrame: clip.startFrame + offset,
      sourceRms,
      outputRms,
      sourcePowers,
      outputPowers,
    };
  });
  const finalImage = finalSnapshot.clips.find(
    (clip) => clip.clipId === imageClip.clipId,
  );
  if (finalImage === undefined)
    throw new Error("image overlay is missing from final recipe");
  expect(finalImage).toMatchObject({
    ...EXPECTED_OVERLAY_PLACEMENTS[0],
    assetId: "first_frame_1",
    // The decoded composition names the effect fields in camel case (`compositionCodec.ts`);
    // only the transform keeps its wire names.
    effect: { brightnessPermille: 100 },
  });
  const finalTitle = finalSnapshot.clips.find(
    (clip) => clip.text?.content === "M25-53",
  );
  expect(finalTitle).toMatchObject(EXPECTED_OVERLAY_PLACEMENTS[1]);
  const positionXbp = Number(finalImage.transform.position_x_bp);
  const positionYbp = Number(finalImage.transform.position_y_bp);
  expect(positionXbp).toBeGreaterThan(300);
  expect(positionXbp).toBeLessThan(2_500);
  expect(positionYbp).toBeGreaterThan(200);
  expect(positionYbp).toBeLessThan(2_500);
  expect(Number(finalImage.transform.scale_x_bp)).toBeGreaterThan(60_000);
  const transition = finalImage.transition;
  expect(transition).toEqual({ kind: "cross_dissolve_v1", durationFrames: 12 });
  const transitionFrame = finalImage.startFrame + 6;
  const visualFrame = finalImage.startFrame + 14;
  // The image centre follows the same placement contract; with the default anchor it does not
  // depend on the image's own pixel size, which the snapshot does not declare.
  const imageCenterX =
    outputWidth / 2 + (outputWidth * positionXbp) / BASIS_POINTS;
  const imageCenterY =
    outputHeight / 2 + (outputHeight * positionYbp) / BASIS_POINTS;
  const secondPlacement = primaryPlacements[1]!;
  const imageCenterInSource = toSourcePoint(
    secondPlacement,
    imageCenterX,
    imageCenterY,
  );
  expect(imageCenterInSource.x).toBeGreaterThan(20);
  expect(imageCenterInSource.x).toBeLessThan(
    expectation.landmark.sourceWidth - 20,
  );
  expect(imageCenterInSource.y).toBeGreaterThan(
    expectation.landmark.centerY + 40,
  );
  expect(imageCenterInSource.y).toBeLessThan(
    expectation.landmark.sourceHeight - 20,
  );
  const imageFirst = interiorRgb(
    ffmpeg,
    downloadPath,
    finalImage.startFrame,
    imageCenterX,
    imageCenterY,
    8,
  );
  const transitionSample = interiorRgb(
    ffmpeg,
    downloadPath,
    transitionFrame,
    imageCenterX,
    imageCenterY,
    8,
  );
  const visualSample = interiorRgb(
    ffmpeg,
    downloadPath,
    visualFrame,
    imageCenterX,
    imageCenterY,
    8,
  );
  const sourceCenter = interiorRgb(
    ffmpeg,
    sourcePaths[1]!,
    finalPrimary[1]!.sourceStartFrame +
      visualFrame -
      finalPrimary[1]!.startFrame,
    imageCenterInSource.x,
    imageCenterInSource.y,
    10,
  );
  expect(maximumChannelError(imageFirst, sourceCenter)).toBeLessThanOrEqual(8);
  expect(
    maximumChannelError(visualSample, { red: 25, green: 25, blue: 25 }),
  ).toBeLessThanOrEqual(12);
  expect(rgbDistance(visualSample, sourceCenter)).toBeGreaterThan(80);
  const originalCenter = interiorRgb(
    ffmpeg,
    downloadPath,
    visualFrame,
    outputWidth / 2,
    outputHeight / 2,
    8,
  );
  expect(maximumChannelError(originalCenter, sourceCenter)).toBeLessThanOrEqual(
    8,
  );
  const transitionAlpha = observedMixAlpha(
    imageFirst,
    visualSample,
    transitionSample,
  );
  expect(Math.abs(transitionAlpha - 0.5)).toBeLessThanOrEqual(0.03);
  // The title band is the placed source interior below its landmark row and right of its
  // frame-index box, so white landmark and digit pixels never count as title text.
  const titleBandStart = toOutputPoint(
    secondPlacement,
    60,
    expectation.landmark.centerY + 30,
  );
  const titleBandEnd = toOutputPoint(
    secondPlacement,
    expectation.landmark.sourceWidth - 1,
    expectation.landmark.sourceHeight - 1,
  );
  const titleBand: OutputRegion = {
    left: Math.round(titleBandStart.x),
    top: Math.round(titleBandStart.y),
    width: Math.round(titleBandEnd.x - titleBandStart.x),
    height: Math.round(titleBandEnd.y - titleBandStart.y),
  };
  const titleBefore = whiteInteriorPixels(
    ffmpeg,
    downloadPath,
    visualFrame,
    titleBand,
  );
  const titleDuring = whiteInteriorPixels(ffmpeg, downloadPath, 164, titleBand);
  expect(titleBefore).toBeLessThan(10);
  expect(titleDuring).toBeGreaterThan(100);
  expect(Number(mediaProbe.format.duration)).toBeCloseTo(
    expectedOutputFrames / 24,
    1,
  );

  // The driver admits this attachment by shape and drops it when a check refuses it, and its
  // temporary Playwright output does not survive the row; the same body is therefore also
  // retained beside the captures, so a refused attachment can be read afterwards.
  const evidenceBody = JSON.stringify({
    schema: "h3.context.m25_53.reference_fidelity.v1",
    candidate: {
      bundleSha256: candidateBundle.sha256,
      installedInventorySha256,
      backendInventorySha256: candidateBackendRuntime.inventorySha256,
    },
    sources: {
      assetIds: expectation.assetIds,
      samples: sourceSamples,
      finalClips: finalPrimary,
    },
    display: {
      evidenceRunId,
      viewportPx: viewportSize,
      populatedOwnedSurfacePx: populatedOwnedSurfaceSize,
    },
    captures,
    populatedDecorationPixels,
    populatedCompositeDecorationPixels,
    export: {
      downloadedSha256,
      retainedDownload,
      downloadedBytes: downloaded.length,
      frameCount: Number(videoStream?.nb_read_frames),
      decodedAudioSamples,
      expectedAudioSamples,
      expectedAudioExtentSamples,
      transitionFrame,
      visualFrame,
      observations: {
        independent: true,
        clipOrderMatched: true,
        sourceRangesMatched: true,
        transitionMatched: true,
        audioMatched: true,
        visualEditsMatched: true,
      },
      observer: {
        ffmpegSha256: expectation.ffmpegSha256,
        ffprobeSha256: expectation.ffprobeSha256,
        sourceSamples,
        sourceRangeSamples,
        landmarkSamples,
        audioSamples,
        silenceRms: {
          source: openingSilence[0]!.sourceRms,
          output: openingSilence[0]!.outputRms,
        },
        openingSilence,
        audioOnsets,
        tailToneRms: audioRms(tailTone),
        contentTail: {
          finalVideoFrame: finalContentFrame,
          finalDecodedAudioWindowRms: audioRms(tailTone),
        },
        outputSamples,
        transitionSample,
        transitionAlpha,
        visualSample,
        imageFirst,
        originalCenter,
        titleBefore,
        titleDuring,
      },
    },
  });
  await retainRowEvidence(evidenceBody, candidateBundle.sha256, evidenceRunId);
  await testInfo.attach("m25-53-reference-fidelity", {
    contentType: "application/json",
    body: evidenceBody,
  });

  page.off("response", onResponse);
  await overlay.locator('[data-h3-nle-action="close"]').click();
  await expect(overlay).toHaveCount(0);
  const released = await postAction(
    page,
    encodeAuthoringAction(`m25-53-release-${suffix}`, "release_workspace", {
      workspace_handle: authoringHandle,
    }),
  );
  expect(released).toEqual({ status: 204, body: null });
  await page.evaluate(() => {
    const runtime = window as any;
    const app = runtime.comfyAPI.app.app;
    if (runtime.__h3M2553Anchor) app.graph.remove(runtime.__h3M2553Anchor);
    if (runtime.__h3M2553Native) app.graph.remove(runtime.__h3M2553Native);
    document.getElementById("h3-m25-53-host-container")?.remove();
  });
  const hostState = await page.evaluate(() => {
    const runtime = window as any;
    const app = runtime.comfyAPI.app.app;
    const baseline = runtime.__h3M2553HostBaseline;
    const result = {
      workflowIdentityStable:
        app.extensionManager.workflow.activeWorkflow ===
        baseline.activeWorkflow,
      openWorkflowCountStable:
        app.extensionManager.workflow.openWorkflows.length ===
        baseline.openWorkflowCount,
      ownedMountCountStable:
        document.querySelectorAll("[data-h3-context-mount]").length ===
        baseline.ownedMountCount,
    };
    delete runtime.__h3M2553HostBaseline;
    return result;
  });
  const graphAfter = await captureM17CanonicalIdentity(page);
  const surroundings = diffGraphSurroundings({
    beforeValue: JSON.parse(graphBefore.graph),
    afterValue: JSON.parse(graphAfter.graph),
    reference: {
      ownedNodeIds: [],
      ownedLinkIds: [],
      anchorNodeId: "",
      ownedProjectionEqual: true,
    },
  });
  expect(Object.values(hostState).every(Boolean)).toBe(true);
  expect(context.pages()).toHaveLength(pageCount);
  expect(await supportedHostQueueCounts(page)).toEqual(queueBefore);
  expect(surroundings.counts.owned).toBe(0);
  expect(promptRequests).toBe(0);
  const networkEvidence = network.snapshot();
  expect(networkEvidence.interactionRemoteCount).toBe(0);
  expect(networkEvidence.interactionProviderCount).toBe(0);
  expectCandidateInteractionNetworkLocal(candidateNetwork);
  page.off("request", onRequest);
});
