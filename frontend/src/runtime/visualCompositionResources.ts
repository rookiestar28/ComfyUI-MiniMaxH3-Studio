import {
  AUTHORING_MEDIA_LEASE_REQUEST_SCHEMA,
  canonicalPublicRuntimeAssetFingerprint,
} from "../contracts/authoringMediaLeaseCodec";
import type {
  PublicCompositionSnapshot,
  ResolvedCompositionScene,
} from "../contracts/compositionCodec";
import type {
  AuthoringMediaSourceLease,
  AuthoringMediaSourceLeaseClient,
  AuthoringMediaLeaseBody,
} from "../host/authoringMediaSourceLease";
import type { NlePlaybackLeaseScheduler } from "../host/nleLeaseScheduler";
import {
  publicAssetById,
  validatePublicAssetManifest,
  type PublicAssetManifest,
} from "./publicAssetManifest";
import type {
  VisualLayerResource,
  VisualLayerResources,
} from "./visualCompositor";

type Geometry = Readonly<{
  sourceWidth: number;
  sourceHeight: number;
  derivativeWidth: number;
  derivativeHeight: number;
}>;
type ImageOwner = Readonly<{ width: number; height: number; close(): void }>;
type FontOwner = Readonly<{ family: string; close(): void }>;
type Options = Readonly<{
  snapshot: PublicCompositionSnapshot;
  manifest: PublicAssetManifest;
  leaseClient: AuthoringMediaSourceLeaseClient;
  playbackScheduler?: NlePlaybackLeaseScheduler;
  onFailure?: () => void;
  decodeImage?: (blob: Blob) => Promise<ImageOwner>;
  loadFont?: (
    bytes: ArrayBuffer,
    family: string,
    weight: string,
    style: string,
  ) => Promise<FontOwner>;
}>;
type StaticBinding = Readonly<{
  workspaceHandle: string;
  workspaceRevision: number;
  timelineRevision: number;
  publicFingerprint: string;
  manifestFingerprint: string;
  profileFingerprint: string;
  clipId: string;
  assetId: string;
  assetFingerprint: string;
  derivativeKind: "image_proxy" | "packaged_font_face";
  fontWeight: string | null;
  fontStyle: string | null;
}>;
type StaticTarget = Readonly<{
  kind: "image" | "font";
  assetId: string;
  text: Readonly<Record<string, unknown>> | null;
  binding: StaticBinding;
}>;
type StaticOwner = {
  lease: AuthoringMediaSourceLease;
  acquisitionEpoch: number;
  binding: StaticBinding;
  resource: VisualLayerResource | null;
  dispose: () => void;
  unsubscribe: () => void;
  byteCount: number;
  localClosed: boolean;
  derivative: DerivativeIdentity | null;
  releasing?: Promise<void>;
};

type DerivativeIdentity = Readonly<{
  fingerprint: string;
  byteCount: number;
  kind: string;
}>;
function verifiedDerivative(
  body: AuthoringMediaLeaseBody,
): DerivativeIdentity | null {
  const receipt = body.receipt;
  if (
    !receipt ||
    !/^sha256:[a-f0-9]{64}$/u.test(receipt.derivativeFingerprint) ||
    receipt.byteCount !== body.blob.size
  )
    return null;
  return {
    fingerprint: receipt.derivativeFingerprint,
    byteCount: receipt.byteCount,
    kind: receipt.derivativeKind,
  };
}
function sameDerivative(
  left: DerivativeIdentity | null,
  right: DerivativeIdentity,
): boolean {
  return (
    left !== null &&
    left.fingerprint === right.fingerprint &&
    left.byteCount === right.byteCount &&
    left.kind === right.kind
  );
}

const LOCAL_VIDEO_TEARDOWN_DEADLINE_MS = 500;
const LOCAL_STATIC_RELEASE_DEADLINE_MS = 500;

function fail(): never {
  throw new Error("source_unavailable");
}
function sourceGeometry(value: unknown): Geometry {
  if (!value || typeof value !== "object") return fail();
  const row = value as Record<string, unknown>;
  if (
    Object.keys(row).sort().join() !==
      [
        "schema",
        "sourceWidth",
        "sourceHeight",
        "derivativeWidth",
        "derivativeHeight",
      ]
        .sort()
        .join() ||
    row.schema !== "h3.authoring.media_geometry.v1"
  )
    return fail();
  for (const key of [
    "sourceWidth",
    "sourceHeight",
    "derivativeWidth",
    "derivativeHeight",
  ]) {
    if (
      !Number.isSafeInteger(row[key]) ||
      Number(row[key]) < 1 ||
      Number(row[key]) > 16_384
    )
      return fail();
  }
  return row as Geometry;
}
function geometryOf(value: object): Geometry {
  return sourceGeometry("geometry" in value ? value.geometry : null);
}

function sameStaticBinding(left: StaticBinding, right: StaticBinding): boolean {
  return (
    left.workspaceHandle === right.workspaceHandle &&
    left.workspaceRevision === right.workspaceRevision &&
    left.timelineRevision === right.timelineRevision &&
    left.publicFingerprint === right.publicFingerprint &&
    left.manifestFingerprint === right.manifestFingerprint &&
    left.profileFingerprint === right.profileFingerprint &&
    left.clipId === right.clipId &&
    left.assetId === right.assetId &&
    left.assetFingerprint === right.assetFingerprint &&
    left.derivativeKind === right.derivativeKind &&
    left.fontWeight === right.fontWeight &&
    left.fontStyle === right.fontStyle
  );
}

async function loadPackagedFace(
  bytes: ArrayBuffer,
  family: string,
  weight: string,
  style: string,
): Promise<FontOwner> {
  const face = new FontFace(family, bytes, { weight, style });
  await face.load();
  document.fonts.add(face);
  return {
    family,
    close: () => {
      document.fonts.delete(face);
    },
  };
}

// IMPORTANT: decode/font promises are not abortable. Dispose late results instead of letting a
// superseded scene resurrect an owner after its lease has already been released.
function abortableOwner<T extends { close(): void }>(
  pending: Promise<T>,
  signal: AbortSignal,
): Promise<T> {
  return new Promise((resolve, reject) => {
    let done = false;
    const abort = () => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      signal.removeEventListener("abort", abort);
      reject(new Error("cancelled"));
    };
    const timer = setTimeout(abort, 3000);
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) abort();
    void pending.then(
      (value) => {
        if (done) {
          value.close();
          return;
        }
        done = true;
        clearTimeout(timer);
        signal.removeEventListener("abort", abort);
        resolve(value);
      },
      () => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        signal.removeEventListener("abort", abort);
        reject(new Error("source_unavailable"));
      },
    );
  });
}

export function createVisualCompositionResources(options: Options) {
  let { snapshot, manifest } = options;
  validatePublicAssetManifest(manifest, snapshot);
  const statics = new Map<string, StaticOwner>();
  const unboundLeases = new Map<
    AuthoringMediaSourceLease,
    Promise<void> | undefined
  >();
  const videos = new Map<string, VisualLayerResource>();
  const videoReleases = new Map<string, () => Promise<void>>();
  const videoGeometry = new Map<string, Geometry>();
  const decodeImage =
    options.decodeImage ?? ((blob: Blob) => createImageBitmap(blob));
  const loadFont = options.loadFont ?? loadPackagedFace;
  let closed = false;
  let failed = false;
  let activeEpoch = -1;
  let pendingStatic = 0;
  let pendingVideo = 0;
  let preparing: Promise<void> | undefined;
  let latestGeneration = 0;
  let failureGeneration = 0;
  const aborts = new Set<AbortController>();
  const acquirePlayback = <T>(operation: () => Promise<T>): Promise<T> =>
    options.playbackScheduler?.acquirePlayback(operation) ?? operation();
  const failure = () => {
    failureGeneration += 1;
    if (failed) return;
    failed = true;
    options.onFailure?.();
  };

  function revokeStatic(owner: StaticOwner) {
    if (!owner.localClosed) {
      owner.localClosed = true;
      owner.unsubscribe();
      owner.dispose();
      owner.resource = null;
      owner.byteCount = 0;
    }
  }
  function releaseStatic(clipId: string, owner: StaticOwner): Promise<void> {
    revokeStatic(owner);
    if (owner.releasing) return owner.releasing;
    // A server refusal remains in the map for retry and truthful resource counts. Cancellation
    // and local teardown must not wait behind the authority acknowledgement.
    const task = Promise.resolve()
      .then(() =>
        owner.lease.release(
          AbortSignal.timeout(LOCAL_STATIC_RELEASE_DEADLINE_MS),
        ),
      )
      .then(() => {
        if (statics.get(clipId) === owner) statics.delete(clipId);
      })
      .finally(() => {
        if (owner.releasing === task) owner.releasing = undefined;
      });
    owner.releasing = task;
    return task;
  }
  function releaseUnbound(lease: AuthoringMediaSourceLease): Promise<void> {
    const pending = unboundLeases.get(lease);
    if (pending) return pending;
    const task = Promise.resolve()
      .then(() =>
        lease.release(AbortSignal.timeout(LOCAL_STATIC_RELEASE_DEADLINE_MS)),
      )
      .then(() => {
        unboundLeases.delete(lease);
      })
      .catch((error: unknown) => {
        unboundLeases.set(lease, undefined);
        failure();
        throw error;
      });
    unboundLeases.set(lease, task);
    return task;
  }
  function reusableStatic(owner: StaticOwner, binding: StaticBinding): boolean {
    const old = owner.binding;
    return (
      !owner.localClosed &&
      owner.releasing === undefined &&
      owner.resource !== null &&
      old.workspaceHandle === binding.workspaceHandle &&
      old.profileFingerprint === binding.profileFingerprint &&
      old.clipId === binding.clipId &&
      old.assetId === binding.assetId &&
      old.assetFingerprint === binding.assetFingerprint &&
      old.derivativeKind === binding.derivativeKind &&
      old.fontWeight === binding.fontWeight &&
      old.fontStyle === binding.fontStyle
    );
  }
  function healthyStatic(owner: StaticOwner, binding: StaticBinding): boolean {
    return (
      !owner.localClosed &&
      owner.releasing === undefined &&
      owner.resource !== null &&
      sameStaticBinding(owner.binding, binding)
    );
  }
  function staticLeaseFailure(
    clipId: string,
    owner: StaticOwner,
    lease: AuthoringMediaSourceLease,
  ): void {
    if (
      statics.get(clipId) !== owner ||
      owner.localClosed ||
      owner.lease !== lease
    )
      return;
    // IMPORTANT: renewal failure revokes private decoded pixels/font data synchronously. Server
    // acknowledgement remains bounded and retryable; a revoked owner must never become readable.
    revokeStatic(owner);
    failure();
    void releaseStatic(clipId, owner).catch(() => undefined);
  }
  async function releaseAll() {
    const results = await Promise.allSettled(
      [...statics].map(([id, owner]) => releaseStatic(id, owner)),
    );
    if (results.some((row) => row.status === "rejected")) {
      failure();
      fail();
    }
  }
  function releaseVideo(
    id: string,
    release: () => Promise<void>,
  ): Promise<void> {
    if (videoReleases.get(id) !== release) return Promise.resolve();
    const operation = (async () => {
      await release();
      if (videoReleases.get(id) === release) {
        videoReleases.delete(id);
        videos.delete(id);
        videoGeometry.delete(id);
      }
    })();
    // IMPORTANT: bound local teardown while continuing to observe the server release. Removing
    // the owner on timeout would report false cleanup and prevent a truthful retry.
    return new Promise<void>((resolve, reject) => {
      const timeout = setTimeout(
        () => reject(new Error("source_unavailable")),
        LOCAL_VIDEO_TEARDOWN_DEADLINE_MS,
      );
      void operation.then(
        () => {
          clearTimeout(timeout);
          resolve();
        },
        (error: unknown) => {
          clearTimeout(timeout);
          reject(error);
        },
      );
    });
  }
  const client: AuthoringMediaSourceLeaseClient = Object.freeze({
    create: (request, signal, expected) =>
      options.leaseClient.create(request, signal, expected),
    close: () => options.leaseClient.close(),
    ...(options.leaseClient.acquireAudioPreview === undefined
      ? {}
      : {
          acquireAudioPreview: (
            request: Parameters<
              NonNullable<
                AuthoringMediaSourceLeaseClient["acquireAudioPreview"]
              >
            >[0],
            context: Parameters<
              NonNullable<
                AuthoringMediaSourceLeaseClient["acquireAudioPreview"]
              >
            >[1],
          ) => options.leaseClient.acquireAudioPreview!(request, context),
        }),
    async acquireVideoSource(request, context) {
      if (
        closed ||
        videoReleases.size + pendingVideo >= 2 ||
        videoReleases.has(request.ownerId)
      )
        return fail();
      pendingVideo += 1;
      try {
        return await acquirePlayback(async () => {
          const owner = await options.leaseClient.acquireVideoSource(
            request,
            context,
          );
          // IMPORTANT: retain ownership before admission. If geometry is rejected and server
          // release fails, dropping this owner makes cleanup appear complete and leaks its lease.
          const release = () => owner.release();
          videoReleases.set(request.ownerId, release);
          let geometry: Geometry;
          try {
            if (closed || request.signal.aborted) throw new Error("cancelled");
            geometry = geometryOf(owner);
            if (
              geometry.derivativeWidth > 1_280 ||
              geometry.derivativeHeight > 1_280
            )
              return fail();
          } catch {
            await releaseVideo(request.ownerId, release);
            return fail();
          }
          const resource: VisualLayerResource = Object.freeze({
            kind: "video",
            source: owner.element,
            nativeWidth: geometry.sourceWidth,
            nativeHeight: geometry.sourceHeight,
          });
          // Visual presentation does not own audio. Keep genuine source facts intact while the
          // separately accepted follower decides whether a primary track may become audible.
          owner.element.muted = true;
          owner.element.playsInline = true;
          videos.set(request.ownerId, resource);
          videoGeometry.set(request.ownerId, geometry);
          return Object.freeze({
            element: owner.element,
            // IMPORTANT: this wrapper narrows visual ownership but must preserve the same-authority
            // audio lease body. Dropping it makes an accepted primary owner fail closed at play.
            audioBody: owner.audioBody,
            ...(owner.rebind === undefined
              ? {}
              : {
                  rebind: async (
                    next: Parameters<
                      AuthoringMediaSourceLeaseClient["acquireVideoSource"]
                    >[0],
                    nextContext?: Parameters<
                      AuthoringMediaSourceLeaseClient["acquireVideoSource"]
                    >[1],
                  ) => {
                    if (
                      closed ||
                      next.ownerId !== request.ownerId ||
                      videoReleases.get(request.ownerId) !== release
                    )
                      return false;
                    const kept = await owner.rebind!(next, nextContext);
                    if (!kept || next.signal.aborted || closed) return false;
                    return (
                      owner.element.videoWidth === geometry.derivativeWidth &&
                      owner.element.videoHeight === geometry.derivativeHeight
                    );
                  },
                }),
            async release() {
              await releaseVideo(request.ownerId, release);
            },
          });
        });
      } finally {
        pendingVideo -= 1;
      }
    },
  });

  async function prepareScene(
    scene: ResolvedCompositionScene,
    epoch: number,
    signal: AbortSignal,
    generation: number,
  ) {
    const startedFailureGeneration = failureGeneration;
    const current = () =>
      !closed && !signal.aborted && generation === latestGeneration;
    if (
      !current() ||
      scene.publicFingerprint !== snapshot.publicFingerprint ||
      scene.blockers.length
    )
      return fail();
    if (scene.layers.length > 8) throw new Error("resource_limit");
    const wanted = new Map<string, StaticTarget>();
    let imageCount = 0;
    let fontCount = 0;
    let videoCount = 0;
    for (const layer of scene.layers) {
      const clipId = String(layer.clipId);
      const clip = snapshot.clips.find((row) => row.clipId === clipId);
      if (
        !clip ||
        clip.assetId !== layer.assetId ||
        clip.trackId !== layer.trackId
      )
        return fail();
      const text = layer.text as Readonly<Record<string, unknown>> | null;
      const assetId = text ? String(text.fontAssetId) : String(layer.assetId);
      const asset = publicAssetById(manifest, assetId);
      if (!asset) return fail();
      if (asset.kind === "video") {
        videoCount += 1;
        continue;
      }
      if (asset.kind === "font") {
        if (!text || assetId !== "h3.font.noto_sans.v1") return fail();
        fontCount += 1;
      } else imageCount += 1;
      const derivativeKind =
        asset.kind === "image" ? "image_proxy" : "packaged_font_face";
      wanted.set(clipId, {
        kind: asset.kind,
        assetId,
        text,
        binding: Object.freeze({
          workspaceHandle: snapshot.workspaceHandle,
          workspaceRevision: snapshot.workspaceRevision,
          timelineRevision: snapshot.timelineRevision,
          publicFingerprint: snapshot.publicFingerprint,
          manifestFingerprint: manifest.manifestFingerprint,
          profileFingerprint: manifest.profileFingerprint,
          clipId,
          assetId,
          assetFingerprint: canonicalPublicRuntimeAssetFingerprint(asset),
          derivativeKind,
          fontWeight: text === null ? null : String(text.weight),
          fontStyle: text === null ? null : String(text.style),
        }),
      });
    }
    if (
      imageCount > 2 ||
      fontCount > 4 ||
      videoCount > 2 ||
      wanted.size + videoCount > 8
    )
      throw new Error("resource_limit");
    for (const [id, owner] of statics) {
      const target = wanted.get(id);
      if (target === undefined || !reusableStatic(owner, target.binding))
        await releaseStatic(id, owner);
    }
    for (const [clipId, target] of wanted) {
      if (!current()) throw new Error("cancelled");
      const retained = statics.get(clipId);
      if (retained !== undefined) {
        if (healthyStatic(retained, target.binding)) continue;
        if (!reusableStatic(retained, target.binding)) {
          await releaseStatic(clipId, retained);
          if (statics.has(clipId)) return fail();
        }
      }
      const asset = publicAssetById(manifest, target.assetId)!;
      await acquirePlayback(async () => {
        const lease: AuthoringMediaSourceLease =
          await options.leaseClient.create(
            {
              schema: AUTHORING_MEDIA_LEASE_REQUEST_SCHEMA,
              operation: "create",
              requestId: crypto.randomUUID(),
              workspaceHandle: snapshot.workspaceHandle,
              workspaceRevision: snapshot.workspaceRevision,
              timelineRevision: snapshot.timelineRevision,
              publicFingerprint: snapshot.publicFingerprint,
              manifestFingerprint: manifest.manifestFingerprint,
              profileFingerprint: manifest.profileFingerprint,
              scope: "clip",
              clipId,
              assetId: target.assetId,
              derivativeKind:
                target.kind === "image" ? "image_proxy" : "packaged_font_face",
              ownerId: clipId,
              runtimeEpoch: epoch,
              sourceStartFrame: 0,
              sourceEndFrame: 1,
            },
            signal,
            canonicalPublicRuntimeAssetFingerprint(asset),
          );
        unboundLeases.set(lease, undefined);
        try {
          if (!current()) throw new Error("cancelled");
          const held = statics.get(clipId);
          if (
            held !== undefined &&
            reusableStatic(held, target.binding) &&
            held.derivative?.kind === target.binding.derivativeKind &&
            typeof lease.derivative === "function" &&
            typeof lease.adopt === "function" &&
            sameDerivative(held.derivative, lease.derivative())
          ) {
            lease.adopt();
            if (!current() || !reusableStatic(held, target.binding))
              throw new Error("cancelled");
            const oldLease = held.lease;
            // IMPORTANT: replace authority and fence callbacks before releasing the old lease.
            // A queued old renewal failure must not revoke the bitmap/font under its new lease.
            held.unsubscribe();
            held.lease = lease;
            held.binding = target.binding;
            held.acquisitionEpoch = epoch;
            unboundLeases.delete(lease);
            unboundLeases.set(oldLease, undefined);
            const unsubscribe = lease.subscribeFailure(() =>
              staticLeaseFailure(clipId, held, lease),
            );
            held.unsubscribe = unsubscribe;
            if (held.localClosed) unsubscribe();
            await releaseUnbound(oldLease);
            if (!current() || !healthyStatic(held, target.binding))
              return fail();
            return;
          }
          if (held !== undefined) await releaseStatic(clipId, held);
          const owner: StaticOwner = {
            lease,
            acquisitionEpoch: epoch,
            binding: target.binding,
            resource: null,
            dispose: () => undefined,
            unsubscribe: () => undefined,
            byteCount: 0,
            localClosed: false,
            derivative: null,
          };
          statics.set(clipId, owner);
          unboundLeases.delete(lease);
          const ownerCurrent = () =>
            current() &&
            statics.get(clipId) === owner &&
            !owner.localClosed &&
            owner.releasing === undefined;
          try {
            if (!ownerCurrent()) throw new Error("cancelled");
            const unsubscribe = lease.subscribeFailure(() =>
              staticLeaseFailure(clipId, owner, lease),
            );
            owner.unsubscribe = unsubscribe;
            if (owner.localClosed) {
              unsubscribe();
              owner.unsubscribe = () => undefined;
              return fail();
            }
            const body = await lease.open(signal);
            if (!ownerCurrent()) return fail();
            owner.byteCount = body.blob.size;
            owner.derivative = verifiedDerivative(body);
            if (target.kind === "image") {
              if (body.blob.type !== "image/png") return fail();
              const geometry = geometryOf(body);
              if (
                geometry.derivativeWidth * geometry.derivativeHeight >
                  4_194_304 ||
                body.blob.size > 16 * 1024 * 1024
              )
                throw new Error("resource_limit");
              const image = await abortableOwner(
                decodeImage(body.blob),
                signal,
              );
              if (
                !ownerCurrent() ||
                image.width !== geometry.derivativeWidth ||
                image.height !== geometry.derivativeHeight
              ) {
                image.close();
                return fail();
              }
              owner.dispose = () => image.close();
              owner.resource = Object.freeze({
                kind: "image",
                source: image as ImageBitmap,
                nativeWidth: geometry.sourceWidth,
                nativeHeight: geometry.sourceHeight,
              });
            } else {
              if (body.blob.size > 1024 * 1024 || body.blob.type !== "font/ttf")
                return fail();
              const family = `h3_preview_${crypto.randomUUID().replaceAll("-", "")}`;
              const bytes = await body.blob.arrayBuffer();
              if (!ownerCurrent()) return fail();
              const face = await abortableOwner(
                loadFont(
                  bytes,
                  family,
                  String(target.text!.weight),
                  String(target.text!.style),
                ),
                signal,
              );
              if (!ownerCurrent() || face.family !== family) {
                face.close();
                return fail();
              }
              owner.dispose = () => face.close();
              owner.resource = Object.freeze({ kind: "font", family });
            }
          } catch (error) {
            const releasing = releaseStatic(clipId, owner).catch(failure);
            if (!signal.aborted) await releasing;
            throw error;
          }
        } finally {
          if (unboundLeases.has(lease)) await releaseUnbound(lease);
        }
      });
    }
    if (!current()) throw new Error("cancelled");
    if (failureGeneration !== startedFailureGeneration) return fail();
    for (const [clipId, target] of wanted) {
      const owner = statics.get(clipId);
      if (owner === undefined || !healthyStatic(owner, target.binding))
        return fail();
    }
    activeEpoch = epoch;
    failed = false;
    // CRITICAL: the lease client owns renewal and publishes real authority failure. A second local
    // TTL would revoke a healthy paused preview even while every server renewal keeps succeeding.
  }

  return Object.freeze({
    leaseClient: client,
    async replace(
      nextManifest: PublicAssetManifest,
      nextSnapshot: PublicCompositionSnapshot,
    ) {
      const validated = validatePublicAssetManifest(nextManifest, nextSnapshot);
      if (
        nextSnapshot.workspaceHandle !== snapshot.workspaceHandle ||
        validated.profileFingerprint !== manifest.profileFingerprint
      )
        throw new Error("contract_mismatch");
      const generation = ++latestGeneration;
      for (const abort of aborts) abort.abort();
      if (preparing) await preparing.catch(() => undefined);
      if (closed || generation !== latestGeneration)
        throw new Error("cancelled");
      snapshot = nextSnapshot;
      manifest = validated;
      activeEpoch = -1;
    },
    async prepare(
      scene: ResolvedCompositionScene,
      epoch: number,
      signal: AbortSignal,
    ) {
      const generation = ++latestGeneration;
      for (const abort of aborts) abort.abort();
      const abort = new AbortController();
      const relay = () => abort.abort();
      signal.addEventListener("abort", relay, { once: true });
      if (signal.aborted) abort.abort();
      aborts.add(abort);
      try {
        if (preparing) await preparing.catch(() => undefined);
        if (generation !== latestGeneration || abort.signal.aborted || closed)
          throw new Error("cancelled");
        pendingStatic = 1;
        const task = prepareScene(scene, epoch, abort.signal, generation);
        preparing = task;
        try {
          await task;
        } finally {
          if (preparing === task) {
            preparing = undefined;
            pendingStatic = 0;
          }
        }
      } finally {
        aborts.delete(abort);
        signal.removeEventListener("abort", relay);
      }
    },
    read(scene: ResolvedCompositionScene, epoch: number): VisualLayerResources {
      if (
        closed ||
        failed ||
        epoch !== activeEpoch ||
        scene.publicFingerprint !== snapshot.publicFingerprint
      )
        return fail();
      const result = new Map<string, VisualLayerResource>();
      for (const layer of scene.layers) {
        const id = String(layer.clipId);
        const resource = statics.get(id)?.resource ?? videos.get(id);
        if (!resource) return fail();
        if (resource.kind === "video") {
          const expected = videoGeometry.get(id);
          const element = resource.source as HTMLVideoElement;
          if (
            !expected ||
            element.videoWidth !== expected.derivativeWidth ||
            element.videoHeight !== expected.derivativeHeight
          )
            return fail();
        }
        result.set(id, resource);
      }
      return result;
    },
    snapshot() {
      return Object.freeze({
        imageOwners: [...statics.values()].filter(
          (row) => row.resource?.kind === "image",
        ).length,
        fontOwners: [...statics.values()].filter(
          (row) => row.resource?.kind === "font",
        ).length,
        videoOwners: videoReleases.size,
        staticLeases: statics.size + unboundLeases.size,
        pendingOperations: pendingStatic + pendingVideo,
        blobBytes: [...statics.values()].reduce(
          (sum, row) => sum + row.byteCount,
          0,
        ),
      });
    },
    async close() {
      closed = true;
      ++latestGeneration;
      for (const abort of aborts) abort.abort();
      // IMPORTANT: dispose every local bitmap/font before any server await. Serial cleanup can
      // leave a second owner's private pixels alive beyond the local teardown deadline.
      for (const owner of statics.values()) revokeStatic(owner);
      const results = await Promise.allSettled([
        preparing?.catch(() => undefined),
        releaseAll(),
        ...[...unboundLeases.keys()].map((lease) => releaseUnbound(lease)),
        ...[...videoReleases].map(([id, release]) => releaseVideo(id, release)),
      ]);
      if (
        results.some((row) => row.status === "rejected") ||
        statics.size ||
        unboundLeases.size ||
        videoReleases.size
      )
        fail();
    },
  });
}
