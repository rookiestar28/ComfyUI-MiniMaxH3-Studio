import {
  AUTHORING_MEDIA_LEASE_REQUEST_SCHEMA,
  type AuthoringMediaLeaseCreateRequest,
} from "../src/contracts/authoringMediaLeaseCodec";
import {
  AuthoringMediaSourceLeaseError,
  createAuthoringMediaSourceLeaseClient,
} from "../src/host/authoringMediaSourceLease";
import fixture from "../../tests/fixtures/m25_10_composition_contract_v1.json";
import {
  decodePublicCompositionSnapshot,
  decodeResolvedScene,
  publicCompositionFingerprint,
} from "../src/contracts/compositionCodec";
import {
  createVisualCompositor,
  type VisualLayerResources,
} from "../src/runtime/visualCompositor";
import {
  buildPublicAssetManifest,
  type PublicRuntimeAsset,
} from "../src/runtime/publicAssetManifest";
import { paintWaveform } from "../src/runtime/nleWaveformPainter";
const expectedAssetFingerprint = `sha256:${"5".repeat(64)}`;
let sequence = 0;
let liveObjectUrls = 0;
let maximumObjectUrls = 0;
function createTrackedObjectURL(blob: Blob) {
  liveObjectUrls += 1;
  maximumObjectUrls = Math.max(maximumObjectUrls, liveObjectUrls);
  return URL.createObjectURL(blob);
}

function revokeTrackedObjectURL(url: string) {
  URL.revokeObjectURL(url);
  liveObjectUrls -= 1;
}

const client = createAuthoringMediaSourceLeaseClient({
  fetchApi: (route, init) => fetch(route, init),
  requestId: () => `browser-request-${++sequence}`,
  createObjectURL: createTrackedObjectURL,
  revokeObjectURL: revokeTrackedObjectURL,
});

function createRequest(
  requestId: string,
  ownerId = "owner-a",
): AuthoringMediaLeaseCreateRequest {
  return {
    schema: AUTHORING_MEDIA_LEASE_REQUEST_SCHEMA,
    operation: "create",
    requestId,
    workspaceHandle: "workspace-browser",
    workspaceRevision: 1,
    timelineRevision: 1,
    publicFingerprint: `sha256:${"1".repeat(64)}`,
    manifestFingerprint: `sha256:${"2".repeat(64)}`,
    profileFingerprint:
      "sha256:f0a226902d89e48113905b43c09fd99c63b2307f8d39ae740330431e091861ff",
    scope: "clip",
    clipId: ownerId,
    assetId: "asset-1",
    derivativeKind: "video_proxy",
    ownerId,
    runtimeEpoch: 1,
    sourceStartFrame: 0,
    sourceEndFrame: 2,
  };
}

type LoopbackMediaContext = Readonly<{
  workspaceHandle: string;
  workspaceRevision: number;
  timelineRevision: number;
  publicFingerprint: string;
  manifestFingerprint: string;
  profileFingerprint: string;
  exactCeilingBytes: number;
  fixtureRunnerCalls: number;
  container: string;
  decoration: Readonly<{
    assetId: string;
    assetFingerprint: string;
    byteCount: number;
    derivativeFingerprint: string;
    width: number;
    height: number;
  }>;
  filmstrip: Readonly<{
    assetId: string;
    assetFingerprint: string;
    byteCount: number;
    derivativeFingerprint: string;
    sourceFrameCount: number;
    sourceWidth: number;
    sourceHeight: number;
    width: number;
    height: number;
    tileCount: number;
  }>;
  filmstripOverLimit: Readonly<{
    assetId: string;
    assetFingerprint: string;
  }>;
  audioPeaks: Readonly<{
    asset: PublicRuntimeAsset;
    assetFingerprint: string;
    byteCount: number;
    derivativeFingerprint: string;
    sampleCount: number;
    pairCount: number;
  }>;
  audioPeaksInvalid: Readonly<{
    asset: PublicRuntimeAsset;
    assetFingerprint: string;
  }>;
  audioPeaksOverLimit: Readonly<{
    asset: PublicRuntimeAsset;
    assetFingerprint: string;
  }>;
  overLimitAssetId: string;
  assets: readonly Readonly<{
    assetId: string;
    assetFingerprint: string;
    byteCount: number;
    derivativeFingerprint: string;
    codec: string;
    width: number;
    height: number;
  }>[];
}>;

function snapshotWithPrimaryAsset(asset: PublicRuntimeAsset) {
  const wire = structuredClone(fixture.snapshot) as Record<string, unknown>;
  const assets = wire.assets as Record<string, unknown>[];
  const originalAssetId = assets[0]!.asset_id;
  assets[0] = {
    asset_id: asset.assetId,
    kind: asset.kind,
    source_time_base: asset.sourceTimeBase,
    source_frame_count: asset.sourceFrameCount,
    source_sample_count: asset.sourceSampleCount,
    embedded_audio: asset.embeddedAudio,
    timestamp_policy: asset.timestampPolicy,
    landmarks: asset.landmarks.map((landmark) => ({
      frame_index: landmark.frameIndex,
      pts: landmark.pts,
      dts: landmark.dts,
      duration_ticks: landmark.durationTicks,
    })),
  };
  for (const clip of wire.clips as Record<string, unknown>[]) {
    if (clip.asset_id !== originalAssetId) continue;
    clip.asset_id = asset.assetId;
    clip.source_start_frame = 0;
    clip.duration_frames = asset.sourceFrameCount;
  }
  wire.public_fingerprint = publicCompositionFingerprint(wire);
  return decodePublicCompositionSnapshot(wire);
}

function realRequest(
  context: LoopbackMediaContext,
  assetId: string,
  ownerId: string,
): AuthoringMediaLeaseCreateRequest {
  return {
    ...createRequest(`real-${ownerId}`, ownerId),
    workspaceHandle: context.workspaceHandle,
    workspaceRevision: context.workspaceRevision,
    timelineRevision: context.timelineRevision,
    publicFingerprint: context.publicFingerprint,
    manifestFingerprint: context.manifestFingerprint,
    profileFingerprint: context.profileFingerprint,
    assetId,
    sourceStartFrame: 0,
    sourceEndFrame: 24,
  };
}

function disposition(error: unknown) {
  return error instanceof AuthoringMediaSourceLeaseError
    ? error.disposition
    : "unexpected";
}

async function awaitPresentedFrame(
  video: HTMLVideoElement,
  targetSeconds: number,
): Promise<number> {
  const request = video.requestVideoFrameCallback;
  if (typeof request !== "function")
    throw new Error("presentation_api_unavailable");
  return new Promise<number>((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error("presentation_timeout")),
      5_000,
    );
    const observe = () =>
      request.call(video, (_now, metadata) => {
        if (Math.abs(metadata.mediaTime - targetSeconds) <= 1 / 48) {
          clearTimeout(timer);
          resolve(metadata.mediaTime);
          return;
        }
        observe();
      });
    observe();
  });
}

function sampleCenter(source: CanvasImageSource): number[] {
  const canvas = document.createElement("canvas");
  canvas.width = 1;
  canvas.height = 1;
  const drawing = canvas.getContext("2d", { willReadFrequently: true });
  if (drawing === null) throw new Error("canvas_unavailable");
  drawing.drawImage(source, 0, 0, 1, 1);
  return Array.from(drawing.getImageData(0, 0, 1, 1).data);
}

export const mediaSourceLeases = Object.freeze({
  async lifecycle() {
    const lease = await client.create(
      createRequest("create-lifecycle"),
      new AbortController().signal,
      expectedAssetFingerprint,
    );
    const created = lease.state();
    const first = await lease.open(new AbortController().signal);
    await lease.renew(new AbortController().signal);
    const renewed = lease.state();
    const order: string[] = [];
    await lease.transfer(
      "owner-b",
      2,
      async () => {
        order.push("local-revoke");
      },
      new AbortController().signal,
    );
    const transferred = lease.state();
    const second = await lease.open(new AbortController().signal);
    await lease.release();
    return {
      created,
      firstBytes: first.blob.size,
      renewed,
      transferred,
      secondBytes: second.blob.size,
      released: lease.state(),
      order,
    };
  },

  async replay() {
    const request = createRequest("create-replay", "owner-replay");
    const first = await client.create(
      request,
      new AbortController().signal,
      expectedAssetFingerprint,
    );
    const second = await client.create(
      request,
      new AbortController().signal,
      expectedAssetFingerprint,
    );
    const sameLease = first.state().leaseId === second.state().leaseId;
    await first.release();
    await second.release();
    return { sameLease, first: first.state(), second: second.state() };
  },

  async staleOwnerAfterTransfer() {
    const request = createRequest("create-stale-owner", "owner-old");
    const current = await client.create(
      request,
      new AbortController().signal,
      expectedAssetFingerprint,
    );
    const stale = await client.create(
      request,
      new AbortController().signal,
      expectedAssetFingerprint,
    );
    await current.transfer(
      "owner-new",
      2,
      async () => undefined,
      new AbortController().signal,
    );
    let staleResult = "unexpected-success";
    try {
      await stale.open(new AbortController().signal);
    } catch (error) {
      staleResult = disposition(error);
    }
    await current.release();
    return { staleResult, current: current.state(), stale: stale.state() };
  },

  async renewDuringOpen() {
    const lease = await client.create(
      createRequest("create-busy", "owner-busy"),
      new AbortController().signal,
      expectedAssetFingerprint,
    );
    const opening = lease.open(new AbortController().signal);
    await new Promise((resolve) => setTimeout(resolve, 10));
    let renewal = "unexpected-success";
    try {
      await lease.renew(new AbortController().signal);
    } catch (error) {
      renewal = disposition(error);
    }
    const body = await opening;
    await lease.release();
    return { renewal, bytes: body.blob.size, released: lease.state().released };
  },

  async abortDelayedOpen() {
    const lease = await client.create(
      createRequest("create-abort", "owner-abort"),
      new AbortController().signal,
      expectedAssetFingerprint,
    );
    const controller = new AbortController();
    const opening = lease.open(controller.signal);
    setTimeout(() => controller.abort(), 10);
    let result = "unexpected-success";
    try {
      await opening;
    } catch (error) {
      result = disposition(error);
    }
    return { result, state: lease.state() };
  },

  async fault() {
    let result = "unexpected-success";
    try {
      const lease = await client.create(
        createRequest("create-fault", "owner-fault"),
        new AbortController().signal,
        expectedAssetFingerprint,
      );
      await lease.open(new AbortController().signal);
    } catch (error) {
      result = disposition(error);
    }
    await client.close().catch(() => undefined);
    return { result, liveObjectUrls };
  },

  async assetDecoration(cancel = false) {
    const snapshot = decodePublicCompositionSnapshot(
      structuredClone(fixture.snapshot),
    );
    const manifest = buildPublicAssetManifest(snapshot);
    const asset = manifest.assets.find(({ kind }) => kind === "video")!;
    const controller = new AbortController();
    const acquiring = client.acquireAssetDecoration(
      {
        snapshot,
        manifest,
        assetId: asset.assetId,
        ownerId: cancel ? "decoration-abort" : "decoration-owner",
        runtimeEpoch: 1,
        derivativeKind: "thumbnail",
      },
      controller.signal,
    );
    if (cancel) setTimeout(() => controller.abort(), 10);
    try {
      const decoration = await acquiring;
      if (decoration.value.derivativeKind !== "thumbnail")
        throw new Error("unexpected decoration kind");
      const result = {
        disposition: "presented",
        width: decoration.value.bitmap.width,
        height: decoration.value.bitmap.height,
      };
      await decoration.release();
      decoration.discard();
      return result;
    } catch (error) {
      return { disposition: disposition(error), width: 0, height: 0 };
    }
  },

  async nativeSource() {
    const lease = await client.create(
      createRequest("create-native", "owner-native"),
      new AbortController().signal,
      expectedAssetFingerprint,
    );
    const body = await lease.open(new AbortController().signal);
    const source = document.createElement("video");
    const url = createTrackedObjectURL(body.blob);
    source.src = url;
    const during = liveObjectUrls;
    source.pause();
    source.removeAttribute("src");
    source.load();
    revokeTrackedObjectURL(url);
    await lease.release();
    return { during, after: liveObjectUrls, maximumObjectUrls };
  },

  /**
   * AC45-06 case (a), browser half: a proxy body above the old 8 MiB ceiling, read through the
   * real lease client and decoded by the real browser codec.
   *
   * The presentation is what makes this a round trip rather than a byte count: the element has to
   * report the proxy's own 1280 x 720 raster and paint a frame a canvas can read back. A body the
   * client admitted but the decoder refused would otherwise look identical from the lease side.
   */
  async presentCeilingProxy() {
    const lease = await client.create(
      createRequest("create-ceiling", "owner-ceiling"),
      new AbortController().signal,
      expectedAssetFingerprint,
    );
    const opened = await lease.open(new AbortController().signal);
    const url = createTrackedObjectURL(opened.blob);
    const element = document.createElement("video");
    element.muted = true;
    element.preload = "auto";
    element.src = url;
    const decoded = await new Promise<boolean>((resolve) => {
      const timer = setTimeout(() => resolve(false), 30_000);
      element.addEventListener(
        "loadeddata",
        () => {
          clearTimeout(timer);
          resolve(true);
        },
        { once: true },
      );
      element.addEventListener(
        "error",
        () => {
          clearTimeout(timer);
          resolve(false);
        },
        { once: true },
      );
      element.load();
    });
    const canvas = document.createElement("canvas");
    canvas.width = element.videoWidth || 1;
    canvas.height = element.videoHeight || 1;
    const context = canvas.getContext("2d", { willReadFrequently: true });
    let opaquePixels = 0;
    if (decoded && context !== null) {
      context.drawImage(element, 0, 0);
      const pixels = context.getImageData(
        0,
        0,
        canvas.width,
        canvas.height,
      ).data;
      for (let index = 3; index < pixels.length; index += 4)
        if (pixels[index] === 255) opaquePixels += 1;
    }
    const presented = {
      decoded,
      byteCount: opened.blob.size,
      mediaType: opened.blob.type,
      videoWidth: element.videoWidth,
      videoHeight: element.videoHeight,
      opaquePixels,
      duringObjectUrls: liveObjectUrls,
    };
    element.pause();
    element.removeAttribute("src");
    element.load();
    revokeTrackedObjectURL(url);
    await lease.release();
    return { ...presented, afterObjectUrls: liveObjectUrls };
  },

  async realRouteAssetContract() {
    const context = (await fetch("/__loopback/media-context").then(
      (response) => {
        if (!response.ok) throw new Error("media_context_unavailable");
        return response.json();
      },
    )) as LoopbackMediaContext;
    const before = (await fetch("/__loopback/counters").then((response) =>
      response.json(),
    )) as {
      same_origin_calls_by_route: Record<string, number>;
    };
    const request: AuthoringMediaLeaseCreateRequest = {
      ...realRequest(
        context,
        context.decoration.assetId,
        "asset-decoration-owner",
      ),
      requestId: "real-asset-decoration",
      scope: "asset",
      clipId: null,
      derivativeKind: "thumbnail",
      sourceStartFrame: 0,
      sourceEndFrame: 1,
    };
    const lease = await client.create(
      request,
      new AbortController().signal,
      context.decoration.assetFingerprint,
    );
    let bitmap: ImageBitmap | undefined;
    let presented: Readonly<Record<string, unknown>>;
    try {
      const opened = await lease.open(new AbortController().signal);
      bitmap = await createImageBitmap(opened.blob);
      presented = Object.freeze({
        scope: request.scope,
        clipId: request.clipId,
        derivativeKind: opened.receipt.derivativeKind,
        derivativeProfileId: opened.receipt.derivativeProfileId,
        mediaType: opened.receipt.mediaType,
        width: bitmap.width,
        height: bitmap.height,
      });
    } finally {
      bitmap?.close();
      await lease.release();
    }

    const filmstripRequest: AuthoringMediaLeaseCreateRequest = {
      ...realRequest(
        context,
        context.filmstrip.assetId,
        "asset-filmstrip-owner",
      ),
      requestId: "real-asset-filmstrip",
      scope: "asset",
      clipId: null,
      derivativeKind: "filmstrip",
      sourceStartFrame: 0,
      sourceEndFrame: context.filmstrip.sourceFrameCount,
    };
    const filmstripLease = await client.create(
      filmstripRequest,
      new AbortController().signal,
      context.filmstrip.assetFingerprint,
    );
    let filmstripBitmap: ImageBitmap | undefined;
    let filmstripPresented: Readonly<Record<string, unknown>>;
    try {
      const opened = await filmstripLease.open(new AbortController().signal);
      filmstripBitmap = await createImageBitmap(opened.blob);
      const canvas = document.createElement("canvas");
      canvas.width = filmstripBitmap.width;
      canvas.height = filmstripBitmap.height;
      const painter = canvas.getContext("2d", { willReadFrequently: true });
      if (painter === null) throw new Error("filmstrip_canvas_unavailable");
      painter.drawImage(filmstripBitmap, 0, 0);
      const pixels = painter.getImageData(
        0,
        0,
        canvas.width,
        canvas.height,
      ).data;
      let opaquePixels = 0;
      for (let index = 3; index < pixels.length; index += 4)
        if (pixels[index] === 255) opaquePixels += 1;
      filmstripPresented = Object.freeze({
        scope: filmstripRequest.scope,
        clipId: filmstripRequest.clipId,
        derivativeKind: opened.receipt.derivativeKind,
        derivativeProfileId: opened.receipt.derivativeProfileId,
        mediaType: opened.receipt.mediaType,
        byteCount: opened.blob.size,
        sourceWidth: opened.geometry?.sourceWidth,
        sourceHeight: opened.geometry?.sourceHeight,
        width: filmstripBitmap.width,
        height: filmstripBitmap.height,
        tileCount: context.filmstrip.tileCount,
        opaquePixels,
      });
    } finally {
      filmstripBitmap?.close();
      await filmstripLease.release();
    }

    const audioSnapshot = snapshotWithPrimaryAsset(context.audioPeaks.asset);
    const audioManifest = buildPublicAssetManifest(audioSnapshot);
    const audioDecoration = await client.acquireAssetDecoration(
      {
        snapshot: audioSnapshot,
        manifest: audioManifest,
        assetId: context.audioPeaks.asset.assetId,
        ownerId: "asset-audio-peaks-owner",
        runtimeEpoch: 1,
        derivativeKind: "audio_peaks",
      },
      new AbortController().signal,
    );
    let audioPeaksPresented: Readonly<Record<string, unknown>>;
    try {
      if (audioDecoration.value.derivativeKind !== "audio_peaks")
        throw new Error("audio_peaks_decoration_kind_invalid");
      const canvas = document.createElement("canvas");
      canvas.width = 240;
      canvas.height = 36;
      const painter = canvas.getContext("2d", { willReadFrequently: true });
      if (painter === null) throw new Error("audio_peaks_canvas_unavailable");
      paintWaveform(painter, {
        asset: {
          assetId: context.audioPeaks.asset.assetId,
          kind: "video",
          embeddedAudio: context.audioPeaks.asset.embeddedAudio,
          sourceSampleCount: context.audioPeaks.asset.sourceSampleCount,
          sourceTimeBase: context.audioPeaks.asset.sourceTimeBase!,
          sourceFrameCount: context.audioPeaks.asset.sourceFrameCount!,
          landmarks: context.audioPeaks.asset.landmarks,
        },
        // B-M2564-03: the clip's duration counts output frames at the snapshot's rate (the codec
        // has validated it as a positive rational).
        outputFrameRate: audioSnapshot.output.frameRate as Readonly<{
          num: number;
          den: number;
        }>,
        clip: {
          sourceStartFrame: 0,
          durationFrames: context.audioPeaks.asset.sourceFrameCount!,
        },
        bounds: { x: 0, y: 0, width: canvas.width, height: canvas.height },
        visible: { x: 0, width: canvas.width },
        envelope: audioDecoration.value.peaks,
      });
      const pixels = painter.getImageData(
        0,
        0,
        canvas.width,
        canvas.height,
      ).data;
      let paintedPixels = 0;
      for (let index = 3; index < pixels.length; index += 4)
        if (pixels[index]! > 0) paintedPixels += 1;
      audioPeaksPresented = Object.freeze({
        derivativeKind: audioDecoration.value.derivativeKind,
        byteCount: audioDecoration.value.peaks.byteLength,
        sampleCount: audioDecoration.value.peaks.sampleCount,
        pairCount: audioDecoration.value.peaks.pairCount,
        paintedPixels,
      });
    } finally {
      try {
        await audioDecoration.release();
      } finally {
        audioDecoration.discard();
      }
    }

    const rawCreate = async (wire: Record<string, unknown>) => {
      const response = await fetch(
        "/h3-context/v1/authoring/media-source-leases",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(wire),
          redirect: "error",
          cache: "no-store",
          credentials: "same-origin",
        },
      );
      const body = (await response.json()) as { reason?: unknown };
      return Object.freeze({ status: response.status, reason: body.reason });
    };
    const unknownScope = await rawCreate({
      ...request,
      requestId: "real-unknown-scope",
      scope: "future-scope",
    });
    const unknownKind = await rawCreate({
      ...request,
      requestId: "real-unknown-kind",
      derivativeKind: "future-kind",
    });
    const oneByteOver = await rawCreate({
      ...request,
      requestId: "real-one-byte-over",
      assetId: context.overLimitAssetId,
    });
    const filmstripOneByteOverBackend = await rawCreate({
      ...filmstripRequest,
      requestId: "real-filmstrip-one-byte-over-backend",
      assetId: context.filmstripOverLimit.assetId,
    });
    let filmstripOneByteOverBrowser = "unexpected-success";
    try {
      await client.create(
        {
          ...filmstripRequest,
          requestId: "real-filmstrip-one-byte-over-browser",
          assetId: context.filmstripOverLimit.assetId,
        },
        new AbortController().signal,
        context.filmstripOverLimit.assetFingerprint,
      );
    } catch (error) {
      filmstripOneByteOverBrowser = disposition(error);
    }
    let audioPeaksInvalidBrowser = "unexpected-success";
    try {
      const snapshot = snapshotWithPrimaryAsset(
        context.audioPeaksInvalid.asset,
      );
      await client.acquireAssetDecoration(
        {
          snapshot,
          manifest: buildPublicAssetManifest(snapshot),
          assetId: context.audioPeaksInvalid.asset.assetId,
          ownerId: "asset-audio-peaks-invalid-owner",
          runtimeEpoch: 1,
          derivativeKind: "audio_peaks",
        },
        new AbortController().signal,
      );
    } catch (error) {
      audioPeaksInvalidBrowser = disposition(error);
    }
    const audioPeaksOverLimitRequest: AuthoringMediaLeaseCreateRequest = {
      ...realRequest(
        context,
        context.audioPeaksOverLimit.asset.assetId,
        "asset-audio-peaks-over-limit-owner",
      ),
      requestId: "real-audio-peaks-one-byte-over-backend",
      scope: "asset",
      clipId: null,
      derivativeKind: "audio_peaks",
      sourceStartFrame: 0,
      sourceEndFrame: context.audioPeaksOverLimit.asset.sourceFrameCount!,
    };
    const audioPeaksOneByteOverBackend = await rawCreate(
      audioPeaksOverLimitRequest,
    );
    let audioPeaksOneByteOverBrowser = "unexpected-success";
    try {
      const snapshot = snapshotWithPrimaryAsset(
        context.audioPeaksOverLimit.asset,
      );
      await client.acquireAssetDecoration(
        {
          snapshot,
          manifest: buildPublicAssetManifest(snapshot),
          assetId: context.audioPeaksOverLimit.asset.assetId,
          ownerId: "asset-audio-peaks-over-limit-browser-owner",
          runtimeEpoch: 1,
          derivativeKind: "audio_peaks",
        },
        new AbortController().signal,
      );
    } catch (error) {
      audioPeaksOneByteOverBrowser = disposition(error);
    }
    const state = await fetch("/__loopback/media-state").then((response) =>
      response.json(),
    );
    const after = (await fetch("/__loopback/counters").then((response) =>
      response.json(),
    )) as {
      same_origin_calls_by_route: Record<string, number>;
    };
    const routeCount = (
      counters: { same_origin_calls_by_route: Record<string, number> },
      path: string,
    ) => counters.same_origin_calls_by_route[path] ?? 0;
    return {
      presented,
      filmstripPresented,
      audioPeaksPresented,
      refusals: { unknownScope, unknownKind, oneByteOver },
      filmstripRefusals: {
        backend: filmstripOneByteOverBackend,
        browser: filmstripOneByteOverBrowser,
      },
      audioPeaksRefusals: {
        invalidEnvelope: audioPeaksInvalidBrowser,
        overLimitBackend: audioPeaksOneByteOverBackend,
        overLimitBrowser: audioPeaksOneByteOverBrowser,
      },
      routeDelta: {
        control:
          routeCount(after, "/h3-context/v1/authoring/media-source-leases") -
          routeCount(before, "/h3-context/v1/authoring/media-source-leases"),
        open:
          routeCount(
            after,
            "/h3-context/v1/authoring/media-source-leases/open",
          ) -
          routeCount(
            before,
            "/h3-context/v1/authoring/media-source-leases/open",
          ),
      },
      state,
    };
  },

  async realRouteDissolve() {
    const context = (await fetch("/__loopback/media-context").then(
      (response) => {
        if (!response.ok) throw new Error("media_context_unavailable");
        return response.json();
      },
    )) as LoopbackMediaContext;
    if (context.assets.length !== 3) throw new Error("media_context_invalid");
    const leases = [];
    // The owned service is deliberately no-queue. Acquire serially, then retain all three
    // authorities together so the test still proves two active sources plus one warm source.
    for (const [index, asset] of context.assets.entries()) {
      leases.push(
        await client.create(
          realRequest(context, asset.assetId, `owner-${index + 1}`),
          new AbortController().signal,
          asset.assetFingerprint,
        ),
      );
    }
    const urls: string[] = [];
    const videos: HTMLVideoElement[] = [];
    const requestedFrame = 12;
    try {
      const bodies = [];
      for (const lease of leases)
        bodies.push(await lease.open(new AbortController().signal));
      for (const [index, body] of bodies.entries()) {
        const url = createTrackedObjectURL(body.blob);
        urls.push(url);
        const video = document.createElement("video");
        video.muted = true;
        video.playsInline = true;
        video.preload = "auto";
        video.src = url;
        video.dataset.h3LeaseRole = index < 2 ? "active" : "warm";
        video.style.cssText =
          "position:absolute;width:1px;height:1px;opacity:0";
        document.body.append(video);
        videos.push(video);
        await new Promise<void>((resolve, reject) => {
          const timer = setTimeout(
            () => reject(new Error("decode_timeout")),
            30_000,
          );
          video.addEventListener(
            "loadeddata",
            () => {
              clearTimeout(timer);
              resolve();
            },
            { once: true },
          );
          video.addEventListener(
            "error",
            () => {
              clearTimeout(timer);
              reject(new Error("decode_failed"));
            },
            { once: true },
          );
          video.load();
        });
        // IMPORTANT (AC45-06): a completed seek is not proof that Chromium presented the
        // requested decoded frame. The compositor may consume only an observed video frame;
        // missing/late requestVideoFrameCallback evidence fails this real-route row closed.
        const presented = awaitPresentedFrame(video, requestedFrame / 24);
        video.currentTime = requestedFrame / 24;
        await new Promise<void>((resolve) =>
          video.addEventListener("seeked", () => resolve(), { once: true }),
        );
        video.dataset.h3PresentedTime = String(await presented);
      }

      const wire = structuredClone(fixture.snapshot);
      wire.output.width = 1280;
      wire.output.height = 720;
      for (const track of wire.tracks)
        track.enabled =
          track.track_id === "track-primary" ||
          track.track_id === "track-video";
      const primary = wire.clips[0]!;
      const overlay = wire.clips[1]!;
      wire.assets[0]!.asset_id = context.assets[0]!.assetId;
      wire.assets[1]!.asset_id = context.assets[1]!.assetId;
      primary.asset_id = context.assets[0]!.assetId;
      overlay.asset_id = context.assets[1]!.assetId;
      overlay.start_frame = 0;
      overlay.duration_frames = 24;
      overlay.source_start_frame = 0;
      overlay.transform = structuredClone(primary.transform);
      overlay.crop = structuredClone(primary.crop);
      overlay.opacity_bp = 10_000;
      overlay.blend = "normal";
      overlay.effect = structuredClone(primary.effect);
      overlay.transition = { kind: "cross_dissolve_v1", duration_frames: 24 };
      for (const clip of wire.clips.slice(2)) {
        clip.enabled = false;
        clip.transition = { kind: "none", duration_frames: 0 };
      }
      wire.public_fingerprint = publicCompositionFingerprint(wire);
      const snapshot = decodePublicCompositionSnapshot(wire);
      const scene = decodeResolvedScene({
        schema: "h3.context.resolved_scene.v1",
        profile_id: "h3.native_media_canvas_backend.v1",
        public_fingerprint: snapshot.publicFingerprint,
        frame: requestedFrame,
        layers: [primary, overlay].map((clip, index) => ({
          clip_id: clip.clip_id,
          asset_id: clip.asset_id,
          track_id: clip.track_id,
          source_frame: requestedFrame,
          source_pts: fixture.expectations.source_pts,
          transition_elapsed_frames: index === 0 ? null : requestedFrame,
          operation_ids: [
            "SelectSourceRangeV1",
            "CropV1",
            "Transform2DV1",
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
        })),
        audio_span: null,
        blockers: [],
      });
      const resources: VisualLayerResources = new Map([
        [
          primary.clip_id,
          {
            kind: "video" as const,
            source: videos[0]!,
            nativeWidth: 1280,
            nativeHeight: 720,
          },
        ],
        [
          overlay.clip_id,
          {
            kind: "video" as const,
            source: videos[1]!,
            nativeWidth: 1280,
            nativeHeight: 720,
          },
        ],
      ]);
      const canvas = document.createElement("canvas");
      const compositor = createVisualCompositor(canvas, snapshot, {
        cssWidth: 1280,
        cssHeight: 720,
        devicePixelRatio: 1,
      });
      const compositorReceipt = compositor.render(scene, resources, 1);
      const compositorPath = compositor.path();
      const drawing = canvas.getContext("2d", { willReadFrequently: true });
      if (drawing === null) throw new Error("canvas_unavailable");
      const pixels = drawing.getImageData(0, 0, 1280, 720).data;
      const compositionSample = Array.from(
        drawing.getImageData(640, 360, 1, 1).data,
      );
      const primarySample = sampleCenter(videos[0]!);
      const overlaySample = sampleCenter(videos[1]!);
      const expectedBlendSample = primarySample.map((value, index) =>
        index === 3
          ? 255
          : Math.round(value * 0.5 + overlaySample[index]! * 0.5),
      );
      let opaquePixels = 0;
      for (let index = 3; index < pixels.length; index += 4)
        if (pixels[index] === 255) opaquePixels += 1;
      const digest = Array.from(
        new Uint8Array(await crypto.subtle.digest("SHA-256", pixels)),
        (value) => value.toString(16).padStart(2, "0"),
      ).join("");
      const negativeCanvas = document.createElement("canvas");
      const negativeCompositor = createVisualCompositor(
        negativeCanvas,
        snapshot,
        { cssWidth: 1280, cssHeight: 720, devicePixelRatio: 1 },
      );
      const negativeWithoutOverlay = negativeCompositor.render(
        scene,
        new Map([[primary.clip_id, resources.get(primary.clip_id)!]]),
        1,
      );
      negativeCompositor.close();
      compositor.close();
      return {
        requestedFrame,
        presentedMediaTimes: videos.map((video) =>
          Number(video.dataset.h3PresentedTime),
        ),
        compositionIdentity: `sha256:${digest}`,
        compositorReceipt,
        compositorPath,
        compositionSample,
        expectedBlendSample,
        negativeWithoutOverlay,
        compositionAssets: context.assets
          .slice(0, 2)
          .map((asset) => asset.assetId),
        byteCounts: bodies.map((body) => body.blob.size),
        mediaTypes: bodies.map((body) => body.blob.type),
        dimensions: videos.map((video) => [
          video.videoWidth,
          video.videoHeight,
        ]),
        activeOwners: videos.filter(
          (video) => video.dataset.h3LeaseRole === "active",
        ).length,
        warmOwners: videos.filter(
          (video) => video.dataset.h3LeaseRole === "warm",
        ).length,
        liveObjectUrls,
        opaquePixels,
        context,
      };
    } finally {
      for (const video of videos) {
        video.pause();
        video.removeAttribute("src");
        video.load();
        video.remove();
      }
      for (const url of urls) revokeTrackedObjectURL(url);
      await Promise.all(leases.map((lease) => lease.release()));
    }
  },

  async close() {
    await client.close();
    return { liveObjectUrls, maximumObjectUrls };
  },
});

Object.assign(globalThis, { mediaSourceLeases });
addEventListener("pagehide", () => void client.close(), { once: true });

declare global {
  interface Window {
    mediaSourceLeases: typeof mediaSourceLeases;
  }
}
