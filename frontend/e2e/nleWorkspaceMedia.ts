// Content-free media at the lease boundary; decoding, compositing, clocks and
// resource disposal remain the product implementations in the performance harness.
import {
  AUTHORING_MEDIA_DERIVATIVE_PROFILE_ID,
  AUTHORING_MEDIA_LEASE_SUCCESS_SCHEMA,
  canonicalPublicRuntimeAssetFingerprint,
  type AuthoringMediaGeometry,
} from "../src/contracts/authoringMediaLeaseCodec";
import {
  AUTHORING_AUDIO_PEAKS_HEADER_BYTES,
  decodeAuthoringAudioPeaks,
  disposeAuthoringAudioPeaks,
  type AuthoringAudioPeaks,
} from "../src/contracts/authoringAudioPeaks";
import type { AuthoringMediaDecorationLeaseClient } from "../src/host/authoringMediaSourceLease";
import { defaultDigest } from "../src/host/authoringMediaLeaseTransport";
import type { AuthoringMediaVideoSourceContext } from "../src/runtime/authoringDecorationLeaseRequest";
import type { MediaTransportOpen } from "../src/runtime/editorRuntime";
import { validatePublicAssetManifest } from "../src/runtime/publicAssetManifest";

const geometry = (width: number, height: number): AuthoringMediaGeometry => ({
  schema: "h3.authoring.media_geometry.v1",
  sourceWidth: width,
  sourceHeight: height,
  derivativeWidth: width,
  derivativeHeight: height,
});
// M25-20 F1 corrective: explicit test instrumentation, read-only, additive. The 151
// `render_and_browser` rows need the actual `<video>` element's own `currentTime` for a source's
// asset id -- the runtime's real decode clock, not a value copied back out of the composition --
// to report `source_mapping.source_time`. This registry is the only way to reach that element,
// since the compositor never attaches it to the visible DOM. It does not change any return value
// or behaviour above; a journey that never reads `window.__nleMediaDebug` is unaffected.
// Elements are registered under the asset id and under the owning clip id. Two clips may lease
// the same asset at once (a primary that hands over to an overlay's source, an overlay that
// duplicates the primary's), each with its own element and its own clock, so the asset-keyed
// entry is only ever the most recent lease; a journey asking which source time a *clip* presents
// reads the clip-keyed entry.
declare global {
  interface Window {
    __nleMediaDebug?: {
      videoElementsByAsset: Map<string, HTMLVideoElement>;
      videoElementsByClip: Map<string, HTMLVideoElement>;
    };
  }
}
function debugRegistry(): {
  videoElementsByAsset: Map<string, HTMLVideoElement>;
  videoElementsByClip: Map<string, HTMLVideoElement>;
} {
  if (window.__nleMediaDebug === undefined) {
    window.__nleMediaDebug = {
      videoElementsByAsset: new Map(),
      videoElementsByClip: new Map(),
    };
  }
  return window.__nleMediaDebug;
}

const live = new Set<() => void>();
let acquired = 0;
let released = 0;
let maximumLive = 0;
// IMPORTANT (M25-63 B-M2563-06): `acquired` counts an acquisition when its bytes arrive, which
// can be after the editor that asked for it has closed (a decoration already fetching is
// discarded, not cancelled). A journey that reads `acquired` across a close must first wait for
// `acquisitionsInFlight` to reach 0, and proves "this step acquired nothing" by
// `acquisitionAttempts` not moving, which also catches an acquisition that lands later.
let leaseCreates = 0;
let leaseOpens = 0;
let sourceRebinds = 0;
let rebindsInFlight = 0;
const sourceAcquireTimelineRevisions: number[] = [];
const sourceRebindTimelineRevisions: number[] = [];
type StaticIdentity = Readonly<{
  fingerprint: string;
  byteCount: number;
  mediaType: string;
  geometry: AuthoringMediaGeometry | null;
}>;
// Identity survives a released fixture; media bytes never do. Active holds share only accounting.
const staticIdentities = new Map<string, StaticIdentity>();
const staticHolds = new Map<
  string,
  { users: number; drop(): void; identity: StaticIdentity }
>();

function sourceBinding(
  request: MediaTransportOpen,
  context: AuthoringMediaVideoSourceContext,
) {
  validatePublicAssetManifest(context.manifest, context.snapshot);
  const asset = context.manifest.assets.find(
    (value) => value.assetId === request.asset.assetId,
  );
  const clip = context.snapshot.clips.find(
    (value) => value.clipId === context.clipId,
  );
  const track = context.snapshot.tracks.find(
    (value) => value.trackId === clip?.trackId,
  );
  if (
    request.signal.aborted ||
    asset?.kind !== "video" ||
    asset.sourceFrameCount === null ||
    clip?.clipId !== request.ownerId ||
    clip.assetId !== asset.assetId ||
    context.sourceStartFrame !== 0 ||
    context.sourceEndFrame !== asset.sourceFrameCount ||
    canonicalPublicRuntimeAssetFingerprint(asset) !==
      canonicalPublicRuntimeAssetFingerprint(request.asset)
  )
    throw new Error("unexpected_synthetic_video_source");
  return {
    asset,
    fingerprint: canonicalPublicRuntimeAssetFingerprint(asset),
    needsAudio:
      asset.embeddedAudio === "present_bound" &&
      clip.enabled &&
      track?.kind === "primary_video" &&
      track.enabled,
  };
}
let acquisitionAttempts = 0;
let acquisitionsInFlight = 0;
// M25-21 resource ceilings. The harness media client is the only place that mints the Blob URLs
// and knows their byte sizes, so the retained-surface accounting belongs here rather than in a
// second registry: a journey reads it through `mediaOwnership()` like every other owner fact.
let blobUrls = 0;
let maximumBlobUrls = 0;
let retainedBytes = 0;
let maximumRetainedBytes = 0;
let videoElements = 0;
let maximumVideoElements = 0;
let surfaces = 0;
let maximumSurfaces = 0;
let filmstripBitmaps = 0;
let maximumFilmstripBitmaps = 0;
let filmstripAttempts = 0;
let waveformBuffers = 0;
let maximumWaveformBuffers = 0;
let waveformAttempts = 0;
type RetainedWaveform = {
  peaks: AuthoringAudioPeaks;
  bytes: number;
};
const retainedWaveforms = new Set<RetainedWaveform>();
function retain(bytes: number, kind: "blob_url" | "surface") {
  surfaces++;
  maximumSurfaces = Math.max(maximumSurfaces, surfaces);
  retainedBytes += bytes;
  maximumRetainedBytes = Math.max(maximumRetainedBytes, retainedBytes);
  if (kind === "blob_url") {
    blobUrls++;
    maximumBlobUrls = Math.max(maximumBlobUrls, blobUrls);
  }
  return () => {
    surfaces--;
    retainedBytes -= bytes;
    if (kind === "blob_url") blobUrls--;
  };
}
function dropWaveform(record: RetainedWaveform) {
  if (!retainedWaveforms.delete(record)) return;
  waveformBuffers--;
  retainedBytes -= record.bytes;
}
function reconcileWaveforms() {
  for (const record of retainedWaveforms) {
    try {
      const start =
        record.peaks.pairs.byteOffset - AUTHORING_AUDIO_PEAKS_HEADER_BYTES;
      const magic = new Uint8Array(record.peaks.pairs.buffer, start, 4);
      if (magic.every((byte) => byte === 0)) dropWaveform(record);
    } catch {
      // A detached or otherwise inaccessible buffer retains no browser-readable bytes.
      dropWaveform(record);
    }
  }
}
function retainWaveform(peaks: AuthoringAudioPeaks) {
  const record = { peaks, bytes: peaks.byteLength };
  retainedWaveforms.add(record);
  waveformBuffers++;
  maximumWaveformBuffers = Math.max(maximumWaveformBuffers, waveformBuffers);
  retainedBytes += record.bytes;
  maximumRetainedBytes = Math.max(maximumRetainedBytes, retainedBytes);
  return () => dropWaveform(record);
}

function syntheticAudioPeaks(sourceSampleCount: number): AuthoringAudioPeaks {
  const sampleCount = Math.floor(sourceSampleCount / 6);
  const pairCount = Math.ceil(sampleCount / 80);
  if (sampleCount < 1 || pairCount > 16_384)
    throw new Error("synthetic_audio_peaks_resource_limit");
  const bytes = new Uint8Array(20 + pairCount * 2);
  bytes.set([0x48, 0x33, 0x41, 0x50]);
  const view = new DataView(bytes.buffer);
  view.setUint16(4, 1, true);
  view.setUint16(6, 100, true);
  view.setUint32(8, 8_000, true);
  view.setUint32(12, sampleCount, true);
  view.setUint32(16, pairCount, true);
  for (let index = 0; index < pairCount; index += 1) {
    // Content-free alternating pulses make source-time mapping visible without private audio.
    const amplitude = index % 20 < 2 ? 112 : index % 5 === 0 ? 48 : 12;
    view.setInt8(20 + index * 2, -amplitude);
    view.setInt8(20 + index * 2 + 1, amplitude);
  }
  return decodeAuthoringAudioPeaks(bytes);
}

function syntheticAudioPreview(sourceSampleCount: number): Blob {
  const channels = 1;
  const blockAlign = channels * 2;
  const dataBytes = sourceSampleCount * blockAlign;
  const bodyBytes = 44 + dataBytes;
  if (
    !Number.isSafeInteger(sourceSampleCount) ||
    sourceSampleCount < 1 ||
    !Number.isSafeInteger(bodyBytes) ||
    bodyBytes > 8 * 1024 * 1024
  )
    throw new Error("synthetic_audio_preview_resource_limit");
  const bytes = new Uint8Array(bodyBytes);
  const view = new DataView(bytes.buffer);
  const ascii = (offset: number, value: string) => {
    for (const [index, character] of [...value].entries())
      bytes[offset + index] = character.charCodeAt(0);
  };
  // GUARD: the product parser accepts only the canonical 44-byte PCM header and exact source
  // sample coverage. Returning the video fixture or a browser-generated container here masks the
  // independent audio-preview lease and makes the integrated follower fail before decode.
  ascii(0, "RIFF");
  view.setUint32(4, bodyBytes - 8, true);
  ascii(8, "WAVE");
  ascii(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, channels, true);
  view.setUint32(24, 48_000, true);
  view.setUint32(28, 48_000 * blockAlign, true);
  view.setUint16(32, blockAlign, true);
  view.setUint16(34, 16, true);
  ascii(36, "data");
  view.setUint32(40, dataBytes, true);
  for (let sample = 0; sample < sourceSampleCount; sample += 1) {
    const value =
      sample >= 24_000 && sample < 24_024
        ? 0.8
        : Math.sin((2 * Math.PI * 440 * sample) / 48_000) * 0.08;
    view.setInt16(44 + sample * blockAlign, Math.round(value * 32_767), true);
  }
  return new Blob([bytes], { type: "audio/wav" });
}
// B-M2561-06: `acquired`, `live` and `maximumLive` count every owner, decorations included. A
// claim about the monitor's own owners (how many a replacement costs, how many are live at once)
// reads the `source*` counters, which only the source-lease paths (`acquireVideoSource`, `create`)
// feed; bin thumbnails, filmstrips and peaks go through `acquireAssetDecoration` and are not
// monitor owners.
let sourceAcquired = 0;
const sourceLive = new Set<() => void>();
let maximumSourceLive = 0;
function owner(dispose: () => void, role: "source" | "decoration") {
  acquired++;
  if (role === "source") sourceAcquired++;
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    live.delete(close);
    sourceLive.delete(close);
    dispose();
    released++;
  };
  live.add(close);
  maximumLive = Math.max(maximumLive, live.size);
  if (role === "source") {
    sourceLive.add(close);
    maximumSourceLive = Math.max(maximumSourceLive, sourceLive.size);
  }
  return close;
}
export const mediaOwnership = () => {
  // The product cache disposes peaks directly, not through the synthetic lease's discard closure.
  // Reconcile against the actual erased H3AP buffer so close evidence measures retained bytes.
  reconcileWaveforms();
  return {
    leaseCreates,
    leaseOpens,
    sourceRebinds,
    rebindsInFlight,
    sourceAcquireTimelineRevisions: [...sourceAcquireTimelineRevisions],
    sourceRebindTimelineRevisions: [...sourceRebindTimelineRevisions],
    acquired,
    released,
    live: live.size,
    sourceAcquired,
    sourceLive: sourceLive.size,
    maximumSourceLive,
    acquisitionAttempts,
    acquisitionsInFlight,
    maximumLive,
    blobUrls,
    maximumBlobUrls,
    retainedBytes,
    maximumRetainedBytes,
    videoElements,
    maximumVideoElements,
    surfaces,
    maximumSurfaces,
    filmstripBitmaps,
    maximumFilmstripBitmaps,
    filmstripAttempts,
    waveformBuffers,
    maximumWaveformBuffers,
    waveformAttempts,
  };
};

// M25-20 F1 corrective: a corpus source asset
// (`comfyui_h3_context.core.semantic_conformance_media.SOURCE_PROFILES`) carries a real,
// spec-compliant picture -- the frame-identity row and the labelled colour patches -- that a
// generic green still or a content-free stub video cannot stand in for. Every caller that names an
// asset asks for it by id (`?asset=<id>` on the SAME `/nle-media/<member>` path, so the pathname
// every existing route handler already matches on is unchanged), and the route answers with the
// real file when it has one.
//
// GUARD (B-M2545-16): the page must not hold its own list of which ids are corpus assets. It did,
// and M25-45's high-resolution sources were not on it, so `vid-primary-hd` was served the generic
// content-free stub while every stage downstream believed it was looking at the declared picture:
// the monitor drew a 320 x 180 placeholder, every landmark read as absent, and each frame's source
// identity decoded to 0. Whether a response IS corpus media is decided by the route that served
// it, through the header below, and by nothing else -- which is what the guard under it has always
// said. A second list in the page is a second opinion about the corpus.

// GUARD: whether a response IS corpus media is decided by the route that served it (the
// `x-nle-corpus-asset` header `openRenderAndBrowserShell` sets), never by the asset id the
// caller named. Every other journey's route serves its own generic stub for the same
// `/nle-media/<member>` path -- the smoke fixture's video asset is `vid-primary` -- and a
// geometry keyed on the id alone would claim 128x128 for a 320x180 stub, failing the
// compositor's readiness check (`visualCompositionResources`: element size must equal the
// declared derivative size) and leaving "Play" disabled in journeys that never touch the corpus.
export const CORPUS_MEDIA_HEADER = "x-nle-corpus-asset";

async function bytes(
  member: "video" | "font" | "image",
  signal: AbortSignal,
  assetId?: string,
): Promise<{ blob: Blob; corpus: boolean }> {
  const query =
    assetId === undefined ? "" : `?asset=${encodeURIComponent(assetId)}`;
  const response = await fetch(`/nle-media/${member}${query}`, { signal });
  if (!response.ok) throw new Error("synthetic_media_unavailable");
  const blob = await response.blob();
  if (blob.size > 1024 * 1024) throw new Error("synthetic_media_bound");
  const corpus =
    assetId !== undefined &&
    response.headers.get(CORPUS_MEDIA_HEADER) === assetId;
  return { blob, corpus };
}

// IMPORTANT (B-M2563-14): the caller's signal reaches the fetch. With a private signal, a request
// the lease scheduler preempted (playback aborts an in-flight decoration by design) still fetched,
// decoded and minted an owner before noticing the abort, so every preemption counted as an
// acquisition.
async function still(
  signal: AbortSignal,
  assetId?: string,
): Promise<{ blob: Blob; corpus: boolean }> {
  if (assetId !== undefined) {
    const served = await bytes("image", signal, assetId);
    if (served.corpus) return served;
  }
  const canvas = document.createElement("canvas");
  canvas.width = 32;
  canvas.height = 32;
  const context = canvas.getContext("2d")!;
  context.fillStyle = "#28a060";
  context.fillRect(0, 0, 32, 32);
  const blob = await new Promise<Blob>((resolve, reject) =>
    canvas.toBlob(
      (result) =>
        result
          ? resolve(result)
          : reject(new Error("synthetic_image_unavailable")),
      "image/png",
    ),
  );
  return { blob, corpus: false };
}

/**
 * The served corpus source's true pixel size for `geometry.sourceWidth`/`sourceHeight`, measured
 * from the decoded media itself (a transform or crop row's expectation is derived from
 * `semantic_conformance_media.SOURCE_WIDTH`/`SOURCE_HEIGHT` and the clip's own transform, so the
 * runtime must present the real source size). Only a response the corpus route marked is
 * measured; an unmarked response keeps the generic placeholder geometry every other journey
 * already relies on, whatever asset id it was requested under.
 */
async function measuredVideoGeometry(
  element: HTMLVideoElement,
): Promise<{ width: number; height: number }> {
  if (element.readyState < 1) {
    await new Promise<void>((resolve, reject) => {
      element.addEventListener("loadedmetadata", () => resolve(), {
        once: true,
      });
      element.addEventListener(
        "error",
        () => reject(new Error("synthetic_media_undecodable")),
        { once: true },
      );
    });
  }
  return { width: element.videoWidth, height: element.videoHeight };
}

async function measuredStillGeometry(
  blob: Blob,
): Promise<{ width: number; height: number }> {
  const bitmap = await createImageBitmap(blob);
  const size = { width: bitmap.width, height: bitmap.height };
  bitmap.close();
  return size;
}

async function tracked<T>(acquire: () => Promise<T>): Promise<T> {
  acquisitionAttempts++;
  acquisitionsInFlight++;
  try {
    return await acquire();
  } finally {
    acquisitionsInFlight--;
  }
}

function rebindDelay(signal: AbortSignal): Promise<void> {
  const delay = Number(
    new URLSearchParams(location.search).get("rebindDelayMs") ?? "0",
  );
  if (!Number.isFinite(delay) || delay <= 0 || signal.aborted)
    return Promise.resolve();
  return new Promise((resolve) => {
    const timer = setTimeout(finish, Math.min(delay, 4_000));
    function finish() {
      clearTimeout(timer);
      signal.removeEventListener("abort", finish);
      resolve();
    }
    signal.addEventListener("abort", finish, { once: true });
  });
}

// Preparation -- an asset's playback derivatives asked for before a clip uses the asset -- is
// counted apart from every owner fact above. It mints no owner, fetches nothing and retains
// nothing, and it is not `tracked`, so `mediaOwnership()` reads the same with or without it. A
// journey that asks what was prepared reads `mediaPreparation()`.
let preparationAttempts = 0;
const preparationAttemptsAtMs: number[] = [];
let preparationsInFlight = 0;
let preparationsAborted = 0;
let preparationsReleased = 0;
const preparedPlayback: string[] = [];
export const mediaPreparation = () => ({
  attempts: preparationAttempts,
  /** `performance.now()` at each attempt: the clock of the lease scheduler's trace events. */
  attemptsAtMs: [...preparationAttemptsAtMs],
  inFlight: preparationsInFlight,
  aborted: preparationsAborted,
  released: preparationsReleased,
  /** `<assetId>:<kind>` in the order the preparations completed. */
  prepared: [...preparedPlayback],
});
// `?preparationDelayMs=<n>` makes a preparation take that long, so that a journey can have
// playback arrive while one is running. The wait ends at once when the request is aborted.
function preparationDelay(signal: AbortSignal): Promise<void> {
  const delayMs = Number(
    new URLSearchParams(location.search).get("preparationDelayMs") ?? "0",
  );
  if (!Number.isFinite(delayMs) || delayMs <= 0 || signal.aborted)
    return Promise.resolve();
  return new Promise((resolve) => {
    const timer = setTimeout(finish, delayMs);
    function finish() {
      clearTimeout(timer);
      signal.removeEventListener("abort", finish);
      resolve();
    }
    signal.addEventListener("abort", finish, { once: true });
  });
}

const untrackedMediaClient: AuthoringMediaDecorationLeaseClient = {
  async acquireAudioPreview(request, context) {
    const asset = context.manifest.assets.find(
      (candidate) => candidate.assetId === request.asset.assetId,
    );
    const clip = context.snapshot.clips.find(
      (candidate) => candidate.clipId === context.clipId,
    );
    if (
      request.signal.aborted ||
      asset?.kind !== "video" ||
      asset.embeddedAudio !== "present_bound" ||
      asset.sourceSampleCount === null ||
      asset.sourceFrameCount === null ||
      clip?.assetId !== asset.assetId ||
      clip.clipId !== request.ownerId ||
      context.sourceStartFrame !== 0 ||
      context.sourceEndFrame !== asset.sourceFrameCount
    )
      throw new Error("unexpected_synthetic_audio_preview");
    const audioBody = syntheticAudioPreview(asset.sourceSampleCount);
    const dropRetention = retain(audioBody.size, "surface");
    const release = owner(dropRetention, "source");
    return {
      audioBody,
      release: async () => release(),
    };
  },
  async acquireVideoSource(request, context) {
    const admitted = sourceBinding(request, context);
    sourceAcquireTimelineRevisions.push(context.snapshot.timelineRevision);
    leaseCreates += admitted.needsAudio ? 2 : 1;
    const assetId = request.asset.assetId;
    const clipId = context.clipId;
    const { blob, corpus } = await bytes("video", request.signal, assetId);
    const asset = admitted.asset;
    const audioBody =
      admitted.needsAudio && asset.sourceSampleCount !== null
        ? syntheticAudioPreview(asset.sourceSampleCount)
        : undefined;
    leaseOpens += admitted.needsAudio ? 2 : 1;
    let alive = true;
    const element = document.createElement("video");
    element.playsInline = true;
    element.preload = "auto";
    const url = URL.createObjectURL(blob);
    element.src = url;
    const registry = debugRegistry();
    registry.videoElementsByAsset.set(assetId, element);
    registry.videoElementsByClip.set(clipId, element);
    videoElements++;
    maximumVideoElements = Math.max(maximumVideoElements, videoElements);
    const dropRetention = retain(
      blob.size + (audioBody?.size ?? 0),
      "blob_url",
    );
    const release = owner(() => {
      alive = false;
      if (registry.videoElementsByAsset.get(assetId) === element) {
        registry.videoElementsByAsset.delete(assetId);
      }
      if (registry.videoElementsByClip.get(clipId) === element) {
        registry.videoElementsByClip.delete(clipId);
      }
      element.pause();
      element.removeAttribute("src");
      element.load();
      URL.revokeObjectURL(url);
      videoElements--;
      dropRetention();
    }, "source");
    const { width, height } = corpus
      ? await measuredVideoGeometry(element)
      : { width: 320, height: 180 };
    return {
      element,
      audioBody,
      geometry: geometry(width, height),
      rebind: async (
        nextRequest: MediaTransportOpen,
        nextContext?: AuthoringMediaVideoSourceContext,
      ) =>
        tracked(async () => {
          if (
            !alive ||
            nextRequest.signal.aborted ||
            nextContext === undefined ||
            nextRequest.ownerId !== request.ownerId ||
            nextContext.snapshot.workspaceHandle !==
              context.snapshot.workspaceHandle ||
            nextContext.manifest.profileFingerprint !==
              context.manifest.profileFingerprint ||
            canonicalPublicRuntimeAssetFingerprint(nextRequest.asset) !==
              admitted.fingerprint
          )
            return false;
          const next = sourceBinding(nextRequest, nextContext);
          if (next.needsAudio !== admitted.needsAudio) return false;
          rebindsInFlight++;
          try {
            await rebindDelay(nextRequest.signal);
          } finally {
            rebindsInFlight--;
          }
          if (!alive || nextRequest.signal.aborted) return false;
          // Same fixture authority contract as the product: create once per held lease, no bytes,
          // PCM, element or Blob URL acquired. Preparation counters remain entirely independent.
          leaseCreates += next.needsAudio ? 2 : 1;
          sourceRebinds++;
          sourceRebindTimelineRevisions.push(
            nextContext.snapshot.timelineRevision,
          );
          return true;
        }),
      release: async () => release(),
    };
  },
  async create(request, signal, expectedAssetFingerprint) {
    const isFont = request.derivativeKind === "packaged_font_face";
    if ((!isFont && request.derivativeKind !== "image_proxy") || signal.aborted)
      throw new Error("unexpected_synthetic_derivative");
    leaseCreates++;
    const key = `${expectedAssetFingerprint}:${request.profileFingerprint}:${request.derivativeKind}`;
    const holdKey = `${key}:${request.ownerId}`;
    const admitted = staticIdentities.get(key);
    let closed = false;
    let opened = false;
    let holding: ReturnType<typeof staticHolds.get>;
    const release = owner(() => {
      closed = true;
      if (holding !== undefined && --holding.users === 0) {
        holding.drop();
        if (staticHolds.get(holdKey) === holding) staticHolds.delete(holdKey);
      }
    }, "source");
    let receipt = {
      schema: AUTHORING_MEDIA_LEASE_SUCCESS_SCHEMA,
      operation: "create" as const,
      requestId: request.requestId,
      leaseId: `fixture-${acquired}`,
      revision: 1,
      ownerId: request.ownerId,
      runtimeEpoch: request.runtimeEpoch,
      ttlMs: 600000,
      derivativeKind: request.derivativeKind,
      mediaType: admitted?.mediaType ?? (isFont ? "font/woff2" : "image/png"),
      byteCount: admitted?.byteCount ?? 0,
      derivativeFingerprint:
        admitted?.fingerprint ?? "sha256:" + "a".repeat(64),
      assetFingerprint: expectedAssetFingerprint,
      derivativeProfileId: AUTHORING_MEDIA_DERIVATIVE_PROFILE_ID,
      profileFingerprint: request.profileFingerprint,
      audioDisposition: "absent" as const,
    };
    return {
      state: () => ({
        leaseId: receipt.leaseId,
        revision: 1,
        ownerId: request.ownerId,
        runtimeEpoch: request.runtimeEpoch,
        opened,
        released: closed,
      }),
      derivative: () => ({
        fingerprint: receipt.derivativeFingerprint,
        byteCount: receipt.byteCount,
        kind: request.derivativeKind,
      }),
      adopt: () => {
        const active = staticHolds.get(holdKey);
        if (
          closed ||
          opened ||
          active === undefined ||
          active.identity.fingerprint !== receipt.derivativeFingerprint ||
          active.identity.byteCount !== receipt.byteCount
        )
          throw new Error("synthetic_adoption_refused");
        // IMPORTANT: the held resource changes authority, not allocation. Retiring the old lease
        // must leave its bytes counted until the adopting lease also ends; no idle body is pooled.
        holding = active;
        holding.users++;
        opened = true;
      },
      open: async (openSignal: AbortSignal) => {
        if (closed || opened || openSignal.aborted)
          throw new Error("synthetic_lease_released");
        leaseOpens++;
        const { blob, corpus } = isFont
          ? await bytes("font", openSignal)
          : await still(openSignal, request.assetId);
        const size =
          isFont || !corpus
            ? { width: 32, height: 32 }
            : await measuredStillGeometry(blob);
        const fingerprint = await defaultDigest(
          new Uint8Array(await blob.arrayBuffer()),
        );
        if (closed || openSignal.aborted)
          throw new Error("synthetic_lease_released");
        const identity: StaticIdentity = {
          fingerprint,
          byteCount: blob.size,
          mediaType: blob.type,
          geometry: isFont ? null : geometry(size.width, size.height),
        };
        staticIdentities.set(key, identity);
        holding = { users: 1, drop: retain(blob.size, "surface"), identity };
        staticHolds.set(holdKey, holding);
        receipt = {
          ...receipt,
          byteCount: blob.size,
          derivativeFingerprint: fingerprint,
          mediaType: blob.type,
        };
        opened = true;
        return { blob, receipt, geometry: identity.geometry };
      },
      release: async () => release(),
      renew: async () => {
        if (closed) throw new Error("synthetic_lease_released");
      },
      transfer: async () => {
        throw new Error("synthetic_transfer_not_used");
      },
      subscribeFailure: () => () => undefined,
    };
  },
  async acquireAssetDecoration(context, signal) {
    const asset = context.manifest.assets.find(
      ({ assetId }) => assetId === context.assetId,
    );
    const workspaceHandle =
      "authoring" in context
        ? context.authoring.workspaceHandle
        : context.snapshot.workspaceHandle;
    if (asset === undefined || asset.kind === "font")
      throw new Error("unexpected_synthetic_decoration");
    if (context.derivativeKind === "audio_peaks") {
      waveformAttempts += 1;
      // GUARD: mirror the production lease's exact per-frame timing gate; sparse fixtures cannot
      // support M25-52 waveform mapping and must not receive a fabricated envelope.
      if (
        asset.kind !== "video" ||
        asset.embeddedAudio !== "present_bound" ||
        asset.sourceSampleCount === null ||
        asset.sourceFrameCount === null ||
        asset.landmarks.length !== asset.sourceFrameCount
      )
        throw new Error("synthetic_audio_peaks_unavailable");
      const peaks = syntheticAudioPeaks(asset.sourceSampleCount);
      const dropRetention = retainWaveform(peaks);
      let discarded = false;
      const discard = () => {
        if (discarded) return;
        discarded = true;
        disposeAuthoringAudioPeaks(peaks);
        dropRetention();
      };
      // B-M2563-14: an aborted request is refused before it mints an owner, so every owner is
      // one the caller received.
      if (signal.aborted) {
        discard();
        throw new DOMException("cancelled", "AbortError");
      }
      const releaseAuthority = owner(() => undefined, "decoration");
      return {
        value: {
          peaks,
          derivativeKind: "audio_peaks",
          cacheKey: {
            workspaceHandle,
            assetFingerprint: canonicalPublicRuntimeAssetFingerprint(asset),
            derivativeProfileId: AUTHORING_MEDIA_DERIVATIVE_PROFILE_ID,
            derivativeKind: "audio_peaks",
          },
        },
        release: async () => releaseAuthority(),
        discard,
      };
    }
    const filmstrip = context.derivativeKind === "filmstrip";
    if (filmstrip) filmstripAttempts += 1;
    if (
      filmstrip &&
      new URLSearchParams(location.search).get("filmstripFail") === "1"
    )
      throw new Error("synthetic_filmstrip_unavailable");
    const { blob } = await still(signal, asset.assetId);
    const sourceWidth = filmstrip ? 160 : 32;
    const sourceHeight = filmstrip ? 90 : 32;
    const tileCount = filmstrip ? 4 : 1;
    let bitmap: ImageBitmap;
    if (filmstrip) {
      const canvas = document.createElement("canvas");
      canvas.width = 344;
      canvas.height = 48;
      const painter = canvas.getContext("2d");
      if (painter === null) throw new Error("synthetic_filmstrip_canvas");
      ["#bc3f52", "#3f8cbc", "#64a84f", "#c29b3d"].forEach((colour, index) => {
        painter.fillStyle = colour;
        painter.fillRect(index * 86, 0, 86, 48);
      });
      bitmap = await createImageBitmap(canvas);
      filmstripBitmaps += 1;
      maximumFilmstripBitmaps = Math.max(
        maximumFilmstripBitmaps,
        filmstripBitmaps,
      );
    } else {
      bitmap = await createImageBitmap(blob);
    }
    let discarded = false;
    const dropRetention = retain(bitmap.width * bitmap.height * 4, "surface");
    const nativeClose = bitmap.close.bind(bitmap);
    Object.defineProperty(bitmap, "close", {
      value: () => {
        if (discarded) return;
        discarded = true;
        nativeClose();
        if (filmstrip) filmstripBitmaps -= 1;
        dropRetention();
      },
    });
    const discard = () => {
      bitmap.close();
    };
    // B-M2563-14: as above, an aborted request mints no owner.
    if (signal.aborted) {
      discard();
      throw new DOMException("cancelled", "AbortError");
    }
    const releaseAuthority = owner(() => undefined, "decoration");
    return {
      value: {
        bitmap,
        derivativeKind: context.derivativeKind,
        geometry: {
          schema: "h3.authoring.media_geometry.v1",
          sourceWidth,
          sourceHeight,
          derivativeWidth: bitmap.width,
          derivativeHeight: bitmap.height,
        },
        tileCount,
        cacheKey: {
          workspaceHandle,
          assetFingerprint: canonicalPublicRuntimeAssetFingerprint(asset),
          derivativeProfileId: AUTHORING_MEDIA_DERIVATIVE_PROFILE_ID,
          derivativeKind: context.derivativeKind,
        },
      },
      release: async () => releaseAuthority(),
      discard,
    };
  },
  async prepareAssetPlayback(context, signal) {
    const asset = context.manifest.assets.find(
      ({ assetId }) => assetId === context.assetId,
    );
    // GUARD: mirror the product lease's own gate. A preparation the product would refuse must
    // not be counted as prepared here, or a journey would pass on a request no service admits.
    if (
      asset?.kind !== "video" ||
      asset.sourceFrameCount === null ||
      asset.landmarks.length !== asset.sourceFrameCount ||
      (context.derivativeKind === "audio_preview" &&
        (asset.embeddedAudio !== "present_bound" ||
          asset.sourceSampleCount === null))
    )
      throw new Error("unexpected_synthetic_preparation");
    preparationAttempts++;
    preparationAttemptsAtMs.push(performance.now());
    preparationsInFlight++;
    try {
      await preparationDelay(signal);
    } finally {
      preparationsInFlight--;
    }
    if (signal.aborted) {
      preparationsAborted++;
      throw new DOMException("cancelled", "AbortError");
    }
    preparedPlayback.push(`${context.assetId}:${context.derivativeKind}`);
    return {
      value: {
        derivativeKind: context.derivativeKind,
        cacheKey: {
          workspaceHandle: context.authoring.workspaceHandle,
          assetFingerprint: canonicalPublicRuntimeAssetFingerprint(asset),
          derivativeProfileId: AUTHORING_MEDIA_DERIVATIVE_PROFILE_ID,
          derivativeKind: context.derivativeKind,
        },
      },
      release: async () => {
        preparationsReleased++;
      },
    };
  },
  close: async () => {
    for (const release of [...live]) release();
  },
};

export const workspaceMediaClient: AuthoringMediaDecorationLeaseClient = {
  ...untrackedMediaClient,
  acquireAudioPreview: (...args) =>
    tracked(() => untrackedMediaClient.acquireAudioPreview!(...args)),
  acquireVideoSource: (...args) =>
    tracked(() => untrackedMediaClient.acquireVideoSource(...args)),
  create: (...args) => tracked(() => untrackedMediaClient.create(...args)),
  acquireAssetDecoration: (...args) =>
    tracked(() => untrackedMediaClient.acquireAssetDecoration(...args)),
};
