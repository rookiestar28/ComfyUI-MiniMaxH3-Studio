import {
  AUTHORING_MEDIA_LEASE_CAPABILITY_HEADER,
  AUTHORING_MEDIA_LEASE_DERIVATIVE_HEADER,
  AUTHORING_MEDIA_LEASE_GEOMETRY_HEADER,
  AUTHORING_MEDIA_LEASE_REQUEST_SCHEMA,
  AUTHORING_MEDIA_LEASE_REVISION_HEADER,
  canonicalPublicRuntimeAssetFingerprint,
  decodeAuthoringMediaLeaseReleased,
  decodeAuthoringMediaLeaseSuccess,
  decodeAuthoringMediaGeometry,
  encodeAuthoringMediaLeaseRequest,
  type AuthoringMediaLeaseCreateRequest,
  type NleAuthoringAssetLeaseCreateRequest,
  type AuthoringMediaLeaseSuccess,
  type AuthoringMediaGeometry,
} from "../contracts/authoringMediaLeaseCodec";
import {
  AUTHORING_FILMSTRIP_TILE_HEIGHT,
  authoringFilmstripTileCount,
} from "../contracts/authoringFilmstrip";
import {
  decodeAuthoringAudioPeaks,
  disposeAuthoringAudioPeaks,
  type AuthoringAudioPeaks,
} from "../contracts/authoringAudioPeaks";
import type {
  HtmlMediaElementSourceOwner,
  MediaTransportOpen,
} from "../runtime/editorRuntime";
import { NATIVE_MEDIA_OPERATION_DEADLINE_MS } from "../runtime/editorRuntime";
import { RUNTIME_PROFILE } from "../runtime/mediaCapabilities";
import {
  publicAssetById,
  validatePublicAssetManifest,
} from "../runtime/publicAssetManifest";
import {
  nleAuthoringAssetById,
  validateNleAuthoringAssetManifest,
} from "../runtime/nleAuthoringAssetManifest";
import {
  buildAuthoringDecorationLeaseRequest,
  type AuthoringMediaAssetDecorationContext,
  type AuthoringMediaVideoSourceContext,
  type NleAuthoringMediaAssetPreparationContext,
} from "../runtime/authoringDecorationLeaseRequest";
import {
  AuthoringMediaSourceLeaseError,
  assertReceipt,
  cancelResponse,
  decodeJsonResponse,
  declaredLength,
  defaultDigest,
  forbiddenHeaders,
  readBounded,
} from "./authoringMediaLeaseTransport";
import {
  prepareAuthoringAssetPlayback,
  type AuthoringMediaPreparedPlayback,
} from "./authoringMediaPlaybackPreparation";
import { createAuthoringLeaseRenewal } from "./authoringLeaseRenewal";
import { acquireAuthoringVideoSource } from "./authoringVideoSourceOwner";
export { AuthoringMediaSourceLeaseError } from "./authoringMediaLeaseTransport";
export type { AuthoringMediaSourceLeaseDisposition } from "./authoringMediaLeaseTransport";
export type { AuthoringMediaPreparedPlayback } from "./authoringMediaPlaybackPreparation";

export const AUTHORING_MEDIA_LEASE_ROUTE =
  "/h3-context/v1/authoring/media-source-leases" as const;
export const AUTHORING_MEDIA_LEASE_OPEN_ROUTE =
  "/h3-context/v1/authoring/media-source-leases/open" as const;
const CAPABILITY = /^[0-9a-f]{64}$/;
const BYTE_LIMITS = Object.freeze({
  video_proxy: 24 * 1024 * 1024,
  audio_preview: 8 * 1024 * 1024,
  frame_timing_index: 16 * 1024,
  thumbnail: 512 * 1024,
  filmstrip: 512 * 1024,
  audio_peaks: 64 * 1024,
  image_proxy: 16 * 1024 * 1024,
  packaged_font_face: 1024 * 1024,
});

export type AuthoringMediaLeaseState = Readonly<{
  leaseId: string;
  revision: number;
  ownerId: string;
  runtimeEpoch: number;
  opened: boolean;
  released: boolean;
}>;

export type AuthoringMediaLeaseBody = Readonly<{
  blob: Blob;
  receipt: AuthoringMediaLeaseSuccess;
  geometry: AuthoringMediaGeometry | null;
}>;

export type AuthoringMediaDerivativeIdentity = Readonly<{
  fingerprint: string;
  byteCount: number;
  kind: AuthoringMediaLeaseSuccess["derivativeKind"];
}>;

export type AuthoringMediaSourceLease = Readonly<{
  derivative?(): AuthoringMediaDerivativeIdentity;
  adopt?(): void;
  state(): AuthoringMediaLeaseState;
  open(signal: AbortSignal): Promise<AuthoringMediaLeaseBody>;
  renew(signal: AbortSignal): Promise<void>;
  transfer(
    nextOwnerId: string,
    nextRuntimeEpoch: number,
    revokeLocalOwner: () => Promise<void>,
    signal: AbortSignal,
  ): Promise<void>;
  release(signal?: AbortSignal): Promise<void>;
  subscribeFailure(listener: () => void): () => void;
}>;

export type AuthoringMediaSourceLeaseClient = Readonly<{
  create(
    request:
      AuthoringMediaLeaseCreateRequest | NleAuthoringAssetLeaseCreateRequest,
    signal: AbortSignal,
    expectedAssetFingerprint: string,
  ): Promise<AuthoringMediaSourceLease>;
  acquireVideoSource(
    request: MediaTransportOpen,
    context: AuthoringMediaVideoSourceContext,
  ): Promise<
    HtmlMediaElementSourceOwner &
      Readonly<{ geometry?: AuthoringMediaGeometry | null }>
  >;
  acquireAudioPreview?(
    request: MediaTransportOpen,
    context: AuthoringMediaVideoSourceContext,
  ): Promise<Readonly<{ audioBody: Blob; release(): Promise<void> }>>;
  close(): Promise<void>;
}>;

export type AuthoringMediaDecorationLeaseClient =
  AuthoringMediaSourceLeaseClient &
    Readonly<{
      acquireAssetDecoration(
        context: AuthoringMediaAssetDecorationContext,
        signal: AbortSignal,
      ): Promise<AuthoringMediaAssetDecoration>;
      /**
       * Has the service generate a catalog asset's playback derivative before a clip uses the
       * asset. Optional: a client without it prepares nothing and playback generates on first
       * use, as it did before preparation existed.
       */
      prepareAssetPlayback?(
        context: NleAuthoringMediaAssetPreparationContext,
        signal: AbortSignal,
      ): Promise<AuthoringMediaPreparedPlayback>;
    }>;
export type {
  AuthoringMediaAssetDecorationContext,
  AuthoringMediaAssetDecorationContextV1,
  AuthoringMediaVideoSourceContext,
  NleAuthoringMediaAssetDecorationContext,
  NleAuthoringMediaAssetPreparationContext,
} from "../runtime/authoringDecorationLeaseRequest";

export type AuthoringMediaAssetDecoration = Readonly<{
  value:
    | Readonly<{
        bitmap: ImageBitmap;
        derivativeKind: "thumbnail" | "filmstrip";
        geometry: AuthoringMediaGeometry;
        tileCount: number;
        cacheKey: Readonly<{
          workspaceHandle: string;
          assetFingerprint: string;
          derivativeProfileId: string;
          derivativeKind: "thumbnail" | "filmstrip";
        }>;
      }>
    | Readonly<{
        peaks: AuthoringAudioPeaks;
        derivativeKind: "audio_peaks";
        cacheKey: Readonly<{
          workspaceHandle: string;
          assetFingerprint: string;
          derivativeProfileId: string;
          derivativeKind: "audio_peaks";
        }>;
      }>;
  release(): Promise<void>;
  discard(): void;
}>;

type ClientDependencies = Readonly<{
  fetchApi: (route: string, init: RequestInit) => Promise<Response>;
  digest?: (bytes: Uint8Array) => Promise<string>;
  requestId?: () => string;
  createVideoElement?: () => HTMLVideoElement;
  createImageBitmap?: (blob: Blob) => Promise<ImageBitmap>;
  createObjectURL?: (blob: Blob) => string;
  revokeObjectURL?: (url: string) => void;
  schedule?: (callback: () => void, delayMs: number) => unknown;
  cancelScheduled?: (handle: unknown) => void;
}>;

function requestInit(
  body: unknown,
  capability?: string,
  signal?: AbortSignal,
  delivery: "ordinary" | "release" = "ordinary",
): RequestInit {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  if (capability !== undefined)
    headers[AUTHORING_MEDIA_LEASE_CAPABILITY_HEADER] = capability;
  return {
    method: "POST",
    headers,
    body: JSON.stringify(body),
    signal,
    redirect: "error",
    cache: "no-store",
    credentials: "same-origin",
    // IMPORTANT: only authority release may outlive page navigation. Applying keepalive to
    // create, open, renew, or transfer can let cancelled ownership changes continue off-page.
    keepalive: delivery === "release",
  };
}

function closeDecodedBitmap(bitmap: ImageBitmap): void {
  try {
    bitmap.close();
  } catch {
    // Preserve the acquisition/authority failure as primary even if a host shim violates the
    // synchronous ImageBitmap.close() contract. The bitmap is never returned or cached here.
  }
}

async function waitForVideoMetadata(
  element: HTMLVideoElement,
  signal: AbortSignal,
): Promise<void> {
  if (signal.aborted)
    throw new AuthoringMediaSourceLeaseError("cancelled", 499);
  if (element.readyState >= 1) return;
  await new Promise<void>((resolve, reject) => {
    let settled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const cleanup = () => {
      signal.removeEventListener("abort", onSignalAbort);
      element.removeEventListener("loadedmetadata", onMetadata);
      element.removeEventListener("error", onMediaFailure);
      element.removeEventListener("abort", onMediaFailure);
      if (timer !== undefined) clearTimeout(timer);
    };
    const finish = (error?: AuthoringMediaSourceLeaseError) => {
      if (settled) return;
      settled = true;
      cleanup();
      if (error === undefined) resolve();
      else reject(error);
    };
    const onSignalAbort = () =>
      finish(new AuthoringMediaSourceLeaseError("cancelled", 499));
    const onMetadata = () => finish();
    const onMediaFailure = () =>
      finish(new AuthoringMediaSourceLeaseError("internal_failure", 0));
    signal.addEventListener("abort", onSignalAbort, { once: true });
    element.addEventListener("loadedmetadata", onMetadata, { once: true });
    element.addEventListener("error", onMediaFailure, { once: true });
    element.addEventListener("abort", onMediaFailure, { once: true });
    timer = setTimeout(
      () => finish(new AuthoringMediaSourceLeaseError("internal_failure", 0)),
      NATIVE_MEDIA_OPERATION_DEADLINE_MS,
    );
    try {
      // CRITICAL: the runtime assigns currentTime immediately after acquisition. A detached
      // element without metadata throws before Chromium can present the first leased frame.
      element.load();
    } catch {
      finish(new AuthoringMediaSourceLeaseError("internal_failure", 0));
    }
  });
}

function teardownVideoElement(element: HTMLVideoElement): void {
  try {
    element.pause();
  } catch {
    // A failed decoder still needs the remaining local teardown steps.
  }
  element.removeAttribute("src");
  try {
    element.load();
  } catch {
    // Removing the source is authoritative even when the failed decoder rejects reload.
  }
}

async function awaitWithAbort<T>(
  operation: Promise<T>,
  signal: AbortSignal,
): Promise<T> {
  if (signal.aborted)
    throw new AuthoringMediaSourceLeaseError("cancelled", 499);
  let rejectAbort!: (error: AuthoringMediaSourceLeaseError) => void;
  const aborted = new Promise<never>((_resolve, reject) => {
    rejectAbort = reject;
  });
  const onAbort = () =>
    rejectAbort(new AuthoringMediaSourceLeaseError("cancelled", 499));
  signal.addEventListener("abort", onAbort, { once: true });
  try {
    // CRITICAL: fetch implementations and test doubles are not required to settle after abort.
    // The authority deadline must still invalidate local media and unblock a cleanup retry.
    return await Promise.race([operation, aborted]);
  } finally {
    signal.removeEventListener("abort", onAbort);
  }
}

export function createAuthoringMediaSourceLeaseClient(
  dependencies: ClientDependencies,
): AuthoringMediaDecorationLeaseClient {
  const digest = dependencies.digest ?? defaultDigest;
  const requestId = dependencies.requestId ?? (() => crypto.randomUUID());
  const createVideoElement =
    dependencies.createVideoElement ?? (() => document.createElement("video"));
  const decodeImageBitmap =
    dependencies.createImageBitmap ??
    ((blob: Blob) => globalThis.createImageBitmap(blob));
  const createObjectURL =
    dependencies.createObjectURL ?? ((blob) => URL.createObjectURL(blob));
  const revokeObjectURL =
    dependencies.revokeObjectURL ?? ((url) => URL.revokeObjectURL(url));
  const schedule =
    dependencies.schedule ?? ((callback, delay) => setTimeout(callback, delay));
  const cancelScheduled =
    dependencies.cancelScheduled ??
    ((handle) => clearTimeout(handle as ReturnType<typeof setTimeout>));
  const live = new Set<AuthoringMediaSourceLease>();

  async function control(
    body: unknown,
    capability: string | undefined,
    signal: AbortSignal,
    delivery: "ordinary" | "release" = "ordinary",
  ) {
    if (signal.aborted)
      throw new AuthoringMediaSourceLeaseError("cancelled", 499);
    let response: Response;
    try {
      response = await awaitWithAbort(
        dependencies.fetchApi(
          AUTHORING_MEDIA_LEASE_ROUTE,
          requestInit(body, capability, signal, delivery),
        ),
        signal,
      );
    } catch (error) {
      if (error instanceof AuthoringMediaSourceLeaseError) throw error;
      throw new AuthoringMediaSourceLeaseError(
        signal.aborted ? "cancelled" : "internal_failure",
        signal.aborted ? 499 : 0,
      );
    }
    return {
      response,
      value: await awaitWithAbort(
        decodeJsonResponse(response, (body as { requestId: string }).requestId),
        signal,
      ),
    };
  }

  const client: AuthoringMediaDecorationLeaseClient = Object.freeze({
    async create(request, signal, expectedAssetFingerprint) {
      const exact = encodeAuthoringMediaLeaseRequest(request) as
        AuthoringMediaLeaseCreateRequest | NleAuthoringAssetLeaseCreateRequest;
      let { response, value } = await control(exact, undefined, signal);
      let receipt = decodeAuthoringMediaLeaseSuccess(value);
      const initialCapability = response.headers.get(
        AUTHORING_MEDIA_LEASE_CAPABILITY_HEADER,
      );
      if (initialCapability === null || !CAPABILITY.test(initialCapability))
        throw new AuthoringMediaSourceLeaseError(
          "contract_mismatch",
          response.status,
        );
      assertReceipt(receipt, {
        requestId: exact.requestId,
        operation: "create",
        ownerId: exact.ownerId,
        runtimeEpoch: exact.runtimeEpoch,
        derivativeKind: exact.derivativeKind,
        profileFingerprint: exact.profileFingerprint,
        assetFingerprint: expectedAssetFingerprint,
      });
      let capability = initialCapability;
      let opened = false;
      let released = false;
      let releaseRequested = false;
      let releasePromise: Promise<void> | undefined;
      let ownerControlTail: Promise<void> = Promise.resolve();
      let authorityFailurePublished = false;
      const failures = new Set<() => void>();

      const publishAuthorityFailure = () => {
        if (authorityFailurePublished || releaseRequested || released) return;
        authorityFailurePublished = true;
        for (const listener of [...failures]) listener();
      };

      // IMPORTANT: owner controls must build from the receipt left by the prior control;
      // racing auto-renew with release otherwise sends a stale revision and leaks authority.
      const runOwnerControl = <T>(operation: () => Promise<T>): Promise<T> => {
        const result = ownerControlTail.then(operation, operation);
        ownerControlTail = result.then(
          () => undefined,
          () => undefined,
        );
        return result;
      };

      const renewal = createAuthoringLeaseRenewal({
        eligible: () =>
          !released &&
          !releaseRequested &&
          opened &&
          !authorityFailurePublished,
        ttlMs: () => receipt.ttlMs,
        renew: (signal) => lease.renew(signal),
        failure: publishAuthorityFailure,
        schedule,
        cancel: cancelScheduled,
      });
      const stopRenewal = renewal.stop;
      const scheduleRenewal = renewal.start;
      const ownerRequest = (
        operation: "open" | "renew" | "release",
        id: string,
      ) =>
        encodeAuthoringMediaLeaseRequest({
          schema: AUTHORING_MEDIA_LEASE_REQUEST_SCHEMA,
          operation,
          requestId: id,
          leaseId: receipt.leaseId,
          revision: receipt.revision,
          ownerId: receipt.ownerId,
          runtimeEpoch: receipt.runtimeEpoch,
        });
      const forgetLocalAuthority = () => {
        released = true;
        failures.clear();
        live.delete(lease);
      };
      const releaseCurrent = async (signal: AbortSignal) => {
        if (released) return;
        const id = requestId();
        const body = ownerRequest("release", id);
        let result: Awaited<ReturnType<typeof control>>;
        try {
          result = await control(body, capability, signal, "release");
        } catch (error) {
          // IMPORTANT: lease_gone is authoritative local invalidation. Retrying this stale
          // owner during client teardown cannot release the replacement and prevents convergence.
          if (
            error instanceof AuthoringMediaSourceLeaseError &&
            error.disposition === "lease_gone"
          ) {
            forgetLocalAuthority();
            return;
          }
          throw error;
        }
        const releasedWire = decodeAuthoringMediaLeaseReleased(result.value);
        if (
          releasedWire.requestId !== id ||
          releasedWire.leaseId !== receipt.leaseId
        )
          throw new AuthoringMediaSourceLeaseError(
            "contract_mismatch",
            result.response.status,
          );
        forgetLocalAuthority();
      };
      const doRelease = async (signal = new AbortController().signal) => {
        if (released) return;
        if (releasePromise !== undefined) return releasePromise;
        releaseRequested = true;
        stopRenewal();
        releasePromise = runOwnerControl(() => releaseCurrent(signal));
        try {
          await releasePromise;
        } catch (error) {
          releasePromise = undefined;
          releaseRequested = false;
          throw error;
        }
      };
      const lease: AuthoringMediaSourceLease = Object.freeze({
        derivative: () =>
          Object.freeze({
            fingerprint: receipt.derivativeFingerprint,
            byteCount: receipt.byteCount,
            kind: receipt.derivativeKind,
          }),
        adopt() {
          if (
            released ||
            releaseRequested ||
            opened ||
            authorityFailurePublished
          )
            throw new AuthoringMediaSourceLeaseError("stale", 409);
          // IMPORTANT: adopt authorizes an already verified body; it never opens new bytes.
          opened = true;
          scheduleRenewal();
        },
        state: () =>
          Object.freeze({
            leaseId: receipt.leaseId,
            revision: receipt.revision,
            ownerId: receipt.ownerId,
            runtimeEpoch: receipt.runtimeEpoch,
            opened,
            released,
          }),
        async open(openSignal) {
          if (released || releaseRequested || opened)
            throw new AuthoringMediaSourceLeaseError("stale", 409);
          const id = requestId();
          const body = ownerRequest("open", id);
          try {
            const response = await dependencies.fetchApi(
              AUTHORING_MEDIA_LEASE_OPEN_ROUTE,
              requestInit(body, capability, openSignal),
            );
            if (!response.ok) {
              await decodeJsonResponse(response, id);
              throw new AuthoringMediaSourceLeaseError(
                "contract_mismatch",
                response.status,
              );
            }
            let length: number;
            let geometry: AuthoringMediaGeometry | null;
            try {
              length = declaredLength(
                response,
                BYTE_LIMITS[receipt.derivativeKind],
              );
              geometry = decodeAuthoringMediaGeometry(
                response.headers.get(AUTHORING_MEDIA_LEASE_GEOMETRY_HEADER),
                receipt.derivativeKind,
              );
              if (
                response.headers.get("content-type") !== receipt.mediaType ||
                response.headers.get("cache-control") !== "no-store" ||
                response.headers.get("x-content-type-options") !== "nosniff" ||
                response.headers.get(AUTHORING_MEDIA_LEASE_REVISION_HEADER) !==
                  String(receipt.revision) ||
                response.headers.get(
                  AUTHORING_MEDIA_LEASE_DERIVATIVE_HEADER,
                ) !== receipt.derivativeFingerprint ||
                forbiddenHeaders(response) ||
                length !== receipt.byteCount
              )
                throw new AuthoringMediaSourceLeaseError(
                  "contract_mismatch",
                  response.status,
                );
            } catch (error) {
              await cancelResponse(response);
              throw error;
            }
            const bytes = await readBounded(
              response,
              BYTE_LIMITS[receipt.derivativeKind],
              length,
            );
            if ((await digest(bytes)) !== receipt.derivativeFingerprint)
              throw new AuthoringMediaSourceLeaseError(
                "integrity_failure",
                response.status,
              );
            if (openSignal.aborted)
              throw new AuthoringMediaSourceLeaseError("cancelled", 499);
            opened = true;
            scheduleRenewal();
            return Object.freeze({
              blob: new Blob([bytes.buffer as ArrayBuffer], {
                type: receipt.mediaType,
              }),
              receipt,
              geometry,
            });
          } catch (error) {
            await doRelease().catch(() => undefined);
            if (error instanceof AuthoringMediaSourceLeaseError) throw error;
            throw new AuthoringMediaSourceLeaseError(
              openSignal.aborted ? "cancelled" : "internal_failure",
              0,
            );
          }
        },
        async renew(renewSignal) {
          if (released || releaseRequested || authorityFailurePublished)
            throw new AuthoringMediaSourceLeaseError("lease_gone", 410);
          return runOwnerControl(async () => {
            if (released || authorityFailurePublished)
              throw new AuthoringMediaSourceLeaseError("lease_gone", 410);
            const id = requestId();
            const body = ownerRequest("renew", id);
            const result = await control(body, capability, renewSignal);
            const next = decodeAuthoringMediaLeaseSuccess(result.value);
            assertReceipt(next, {
              requestId: id,
              operation: "renew",
              ownerId: receipt.ownerId,
              runtimeEpoch: receipt.runtimeEpoch,
              derivativeKind: exact.derivativeKind,
              profileFingerprint: exact.profileFingerprint,
              assetFingerprint: expectedAssetFingerprint,
              prior: receipt,
            });
            const echoed = result.response.headers.get(
              AUTHORING_MEDIA_LEASE_CAPABILITY_HEADER,
            );
            if (echoed !== null && echoed !== capability)
              throw new AuthoringMediaSourceLeaseError(
                "contract_mismatch",
                result.response.status,
              );
            receipt = next;
            scheduleRenewal(renewSignal);
            if (renewSignal.aborted)
              throw new AuthoringMediaSourceLeaseError("cancelled", 499);
          });
        },
        async transfer(
          nextOwnerId,
          nextRuntimeEpoch,
          revokeLocalOwner,
          transferSignal,
        ) {
          if (released || releaseRequested || authorityFailurePublished)
            throw new AuthoringMediaSourceLeaseError("lease_gone", 410);
          return runOwnerControl(async () => {
            if (released || authorityFailurePublished)
              throw new AuthoringMediaSourceLeaseError("lease_gone", 410);
            // CRITICAL: delivered bytes cannot be revoked server-side. Detach the old supported
            // browser owner before rotating authority, or two owners can remain live after transfer.
            await revokeLocalOwner();
            stopRenewal();
            const id = requestId();
            const body = encodeAuthoringMediaLeaseRequest({
              schema: AUTHORING_MEDIA_LEASE_REQUEST_SCHEMA,
              operation: "transfer",
              requestId: id,
              leaseId: receipt.leaseId,
              revision: receipt.revision,
              ownerId: receipt.ownerId,
              runtimeEpoch: receipt.runtimeEpoch,
              nextOwnerId,
              nextRuntimeEpoch,
            });
            const result = await control(body, capability, transferSignal);
            const next = decodeAuthoringMediaLeaseSuccess(result.value);
            const nextCapability = result.response.headers.get(
              AUTHORING_MEDIA_LEASE_CAPABILITY_HEADER,
            );
            if (
              nextCapability === null ||
              !CAPABILITY.test(nextCapability) ||
              nextCapability === capability
            )
              throw new AuthoringMediaSourceLeaseError(
                "contract_mismatch",
                result.response.status,
              );
            assertReceipt(next, {
              requestId: id,
              operation: "transfer",
              ownerId: nextOwnerId,
              runtimeEpoch: nextRuntimeEpoch,
              derivativeKind: exact.derivativeKind,
              profileFingerprint: exact.profileFingerprint,
              assetFingerprint: expectedAssetFingerprint,
              prior: receipt,
            });
            capability = nextCapability;
            receipt = next;
            opened = false;
            if (transferSignal.aborted) {
              releaseRequested = true;
              try {
                await releaseCurrent(new AbortController().signal);
              } catch {
                releaseRequested = false;
              }
              throw new AuthoringMediaSourceLeaseError("cancelled", 499);
            }
          });
        },
        release: doRelease,
        subscribeFailure(listener) {
          if (authorityFailurePublished) {
            listener();
            return () => undefined;
          }
          failures.add(listener);
          return () => failures.delete(listener);
        },
      });
      live.add(lease);
      if (signal.aborted) {
        await lease.release().catch(() => undefined);
        throw new AuthoringMediaSourceLeaseError("cancelled", 499);
      }
      return lease;
    },
    async acquireAssetDecoration(context, signal) {
      try {
        if ("authoring" in context)
          validateNleAuthoringAssetManifest(
            context.manifest,
            context.authoring,
          );
        else validatePublicAssetManifest(context.manifest, context.snapshot);
      } catch {
        throw new AuthoringMediaSourceLeaseError("contract_mismatch", 0);
      }
      const asset =
        "authoring" in context
          ? nleAuthoringAssetById(context.manifest, context.assetId)
          : publicAssetById(context.manifest, context.assetId);
      const workspaceHandle =
        "authoring" in context
          ? context.authoring.workspaceHandle
          : context.snapshot.workspaceHandle;
      if (
        asset === undefined ||
        asset.kind === "font" ||
        (context.derivativeKind === "filmstrip" &&
          (asset.kind !== "video" || asset.sourceFrameCount === null)) ||
        (context.derivativeKind === "audio_peaks" &&
          (asset.kind !== "video" ||
            asset.sourceFrameCount === null ||
            asset.landmarks.length !== asset.sourceFrameCount ||
            asset.sourceSampleCount === null ||
            asset.embeddedAudio !== "present_bound"))
      )
        throw new AuthoringMediaSourceLeaseError("contract_mismatch", 0);
      if (signal.aborted)
        throw new AuthoringMediaSourceLeaseError("cancelled", 499);

      const assetFingerprint = canonicalPublicRuntimeAssetFingerprint(asset);
      let lease: AuthoringMediaSourceLease | undefined;
      let bitmap: ImageBitmap | undefined;
      let peaks: AuthoringAudioPeaks | undefined;
      try {
        const createRequest = buildAuthoringDecorationLeaseRequest(
          context,
          requestId(),
          context.derivativeKind === "filmstrip" ||
            context.derivativeKind === "audio_peaks"
            ? asset.sourceFrameCount!
            : 1,
        );
        lease = await client.create(createRequest, signal, assetFingerprint);
        const opened = await lease.open(signal);
        if (context.derivativeKind === "audio_peaks") {
          if (opened.geometry !== null)
            throw new AuthoringMediaSourceLeaseError("contract_mismatch", 0);
          const bytes = await opened.blob.arrayBuffer();
          if (signal.aborted)
            throw new AuthoringMediaSourceLeaseError("cancelled", 499);
          try {
            peaks = decodeAuthoringAudioPeaks(bytes);
          } catch {
            throw new AuthoringMediaSourceLeaseError("contract_mismatch", 0);
          }
          // Public audio authority is canonical 48 kHz coverage; v1 peaks are 8 kHz.
          // IMPORTANT: tolerate only one resampler endpoint sample, never stretch with silence.
          if (Math.abs(peaks.sampleCount * 6 - asset.sourceSampleCount!) > 6)
            throw new AuthoringMediaSourceLeaseError("contract_mismatch", 0);
          let discarded = false;
          const discard = () => {
            if (discarded) return;
            discarded = true;
            disposeAuthoringAudioPeaks(peaks!);
          };
          return Object.freeze({
            value: Object.freeze({
              peaks,
              derivativeKind: "audio_peaks" as const,
              cacheKey: Object.freeze({
                workspaceHandle,
                assetFingerprint,
                derivativeProfileId: opened.receipt.derivativeProfileId,
                derivativeKind: "audio_peaks" as const,
              }),
            }),
            async release() {
              try {
                await lease!.release(
                  AbortSignal.timeout(
                    RUNTIME_PROFILE.limits.teardownDeadlineMs,
                  ),
                );
              } catch (error) {
                discard();
                throw error;
              }
            },
            discard,
          });
        }
        if (opened.geometry === null)
          throw new AuthoringMediaSourceLeaseError("contract_mismatch", 0);
        bitmap = await decodeImageBitmap(opened.blob);
        const tileCount =
          context.derivativeKind === "filmstrip"
            ? authoringFilmstripTileCount(asset)
            : 1;
        const filmstripTileWidth = bitmap.width / tileCount;
        if (
          signal.aborted ||
          !Number.isSafeInteger(bitmap.width) ||
          !Number.isSafeInteger(bitmap.height) ||
          bitmap.width < 1 ||
          bitmap.height < 1 ||
          bitmap.width > 16_384 ||
          bitmap.height > 16_384 ||
          bitmap.width !== opened.geometry.derivativeWidth ||
          bitmap.height !== opened.geometry.derivativeHeight ||
          (context.derivativeKind === "filmstrip" &&
            (bitmap.height !== AUTHORING_FILMSTRIP_TILE_HEIGHT ||
              bitmap.width % tileCount !== 0 ||
              // CRITICAL: dimensions from the route are untrusted even when they match decoded
              // pixels. Keep every tile tied to the claimed source aspect or a forged sprite can
              // pass the header/body equality check and distort timeline source-time evidence.
              Math.abs(
                filmstripTileWidth * opened.geometry.sourceHeight -
                  opened.geometry.sourceWidth * AUTHORING_FILMSTRIP_TILE_HEIGHT,
              ) >
                2 * opened.geometry.sourceHeight))
        )
          throw new AuthoringMediaSourceLeaseError(
            signal.aborted ? "cancelled" : "contract_mismatch",
            signal.aborted ? 499 : 0,
          );

        let discarded = false;
        const discard = () => {
          if (discarded) return;
          discarded = true;
          closeDecodedBitmap(bitmap!);
        };
        return Object.freeze({
          value: Object.freeze({
            bitmap,
            derivativeKind: context.derivativeKind,
            geometry: opened.geometry,
            tileCount,
            cacheKey: Object.freeze({
              workspaceHandle,
              assetFingerprint,
              derivativeProfileId: opened.receipt.derivativeProfileId,
              derivativeKind: context.derivativeKind,
            }),
          }),
          async release() {
            try {
              await lease!.release(
                AbortSignal.timeout(RUNTIME_PROFILE.limits.teardownDeadlineMs),
              );
            } catch (error) {
              // IMPORTANT: an unpublished bitmap must not survive failed authority cleanup.
              // The lease stays registered for client.close() to retry the server release.
              discard();
              throw error;
            }
          },
          discard,
        });
      } catch (error) {
        if (bitmap !== undefined) closeDecodedBitmap(bitmap);
        if (peaks !== undefined) disposeAuthoringAudioPeaks(peaks);
        const primary =
          error instanceof AuthoringMediaSourceLeaseError
            ? error
            : new AuthoringMediaSourceLeaseError(
                signal.aborted ? "cancelled" : "internal_failure",
                signal.aborted ? 499 : 0,
              );
        if (lease !== undefined) {
          try {
            await lease.release(
              AbortSignal.timeout(RUNTIME_PROFILE.limits.teardownDeadlineMs),
            );
          } catch (cleanupError) {
            // IMPORTANT: scheduler preemption must observe failed server-authority cleanup.
            // Hiding it here lets playback retry against the still-live decoration lease.
            throw new AggregateError(
              [primary, cleanupError],
              "asset decoration acquisition cleanup failed",
            );
          }
        }
        throw primary;
      }
    },
    // The body is a module of its own: this one is held to the host module line budget.
    prepareAssetPlayback: (context, signal) =>
      prepareAuthoringAssetPlayback(client, requestId, context, signal),
    acquireVideoSource: (request, context) =>
      acquireAuthoringVideoSource(
        {
          client,
          requestId,
          createVideoElement,
          createObjectURL,
          revokeObjectURL,
          waitForMetadata: waitForVideoMetadata,
          teardown: teardownVideoElement,
        },
        request,
        context,
      ),
    async acquireAudioPreview(request, context) {
      validatePublicAssetManifest(context.manifest, context.snapshot);
      const clip = context.snapshot.clips.find(
        (value) => value.clipId === context.clipId,
      );
      const asset = publicAssetById(context.manifest, request.asset.assetId);
      const track = context.snapshot.tracks.find(
        (candidate) => candidate.trackId === clip?.trackId,
      );
      if (
        clip === undefined ||
        clip.clipId !== request.ownerId ||
        clip.assetId !== request.asset.assetId ||
        !clip.enabled ||
        track?.kind !== "primary_video" ||
        !track.enabled ||
        asset === undefined ||
        canonicalPublicRuntimeAssetFingerprint(asset) !==
          canonicalPublicRuntimeAssetFingerprint(request.asset) ||
        asset.kind !== "video" ||
        asset.sourceFrameCount === null ||
        asset.embeddedAudio !== "present_bound" ||
        context.sourceStartFrame !== 0 ||
        context.sourceEndFrame !== asset.sourceFrameCount
      )
        throw new AuthoringMediaSourceLeaseError("contract_mismatch", 0);
      const lease = await client.create(
        {
          schema: AUTHORING_MEDIA_LEASE_REQUEST_SCHEMA,
          operation: "create",
          requestId: requestId(),
          workspaceHandle: context.snapshot.workspaceHandle,
          workspaceRevision: context.snapshot.workspaceRevision,
          timelineRevision: context.snapshot.timelineRevision,
          publicFingerprint: context.snapshot.publicFingerprint,
          manifestFingerprint: context.manifest.manifestFingerprint,
          profileFingerprint: context.manifest.profileFingerprint,
          scope: "clip",
          clipId: context.clipId,
          assetId: asset.assetId,
          derivativeKind: "audio_preview",
          ownerId: request.ownerId,
          runtimeEpoch: request.epoch,
          sourceStartFrame: 0,
          sourceEndFrame: asset.sourceFrameCount,
        },
        request.signal,
        canonicalPublicRuntimeAssetFingerprint(asset),
      );
      try {
        const audioBody = (await lease.open(request.signal)).blob;
        let released = false;
        return Object.freeze({
          audioBody,
          async release() {
            if (released) return;
            await lease.release(
              AbortSignal.timeout(RUNTIME_PROFILE.limits.teardownDeadlineMs),
            );
            released = true;
          },
        });
      } catch (error) {
        try {
          await lease.release(
            AbortSignal.timeout(RUNTIME_PROFILE.limits.teardownDeadlineMs),
          );
        } catch {
          // The lease remains registered so client.close() can retry the bounded cleanup.
        }
        throw error;
      }
    },
    async close() {
      const results = await Promise.allSettled(
        [...live].map((lease) => lease.release()),
      );
      if (results.some((result) => result.status === "rejected"))
        throw new AuthoringMediaSourceLeaseError("internal_failure", 0);
    },
  });
  return client;
}
