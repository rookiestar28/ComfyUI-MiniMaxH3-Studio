import {
  ENGINE_PROFILE_ID,
  decodeResolvedScene,
  type PublicCompositionSnapshot,
  type ResolvedCompositionScene,
} from "../contracts/compositionCodec";
import { canonicalPublicRuntimeAssetFingerprint } from "../contracts/authoringMediaLeaseCodec";
import type { AuthoringMediaVideoSourceContext } from "./authoringDecorationLeaseRequest";
import {
  RUNTIME_PROFILE,
  RUNTIME_PROFILE_FINGERPRINT,
  isEvaluatedAvailableDisposition,
  type RuntimeCapabilityDisposition,
} from "./mediaCapabilities";
import {
  publicAssetById,
  validatePublicAssetManifest,
  type PublicAssetManifest,
  type PublicRuntimeAsset,
} from "./publicAssetManifest";

export const RUNTIME_SNAPSHOT_SCHEMA = "h3.editor.runtime_snapshot.v1" as const;
export const RUNTIME_RECEIPT_SCHEMA = "h3.editor.runtime_receipt.v1" as const;

export type RuntimeSessionStatus =
  | "closed"
  | "opening"
  | "paused"
  | "seeking"
  | "playing"
  | "blocked"
  | "closing";

export type RuntimeBlockerCode =
  | "profile_unavailable"
  | "capability_mismatch"
  | "qualification_required"
  | "contract_mismatch"
  | "snapshot_blocked"
  | "unsupported"
  | "resource_limit"
  | "transport_failure"
  | "cancelled";

export type RuntimeReceiptStatus =
  "applied" | "cancelled" | "blocked" | "closed";

export type MediaTransportSeek = Readonly<{
  epoch: number;
  sourceFrame: number;
  sourcePts: number;
  signal: AbortSignal;
  /**
   * The seek of an owner that is being prepared ahead of an ownership change and is not yet part
   * of the presented scene. The native transport treats it as any seek; a wrapper that follows
   * the presented owner (audio) must not take it for a move of the playhead.
   */
  preroll?: true;
}>;

export type MediaPresentation = Readonly<{
  ownerId: string;
  assetId: string;
  epoch: number;
  sourceFrame: number;
  sourcePts: number;
  observedSourcePts?: number;
  observation?: "request_video_frame_callback" | "event_fallback";
}>;

export type MediaTransport = Readonly<{
  ownerId: string;
  assetId: string;
  openedEpoch: number;
  pendingFrameObservations: number;
  seek(request: MediaTransportSeek): Promise<MediaPresentation>;
  play(signal: AbortSignal): Promise<void>;
  pause(): Promise<void>;
  subscribe(listener: (value: MediaPresentation) => void): () => void;
  subscribeFailure?(listener: (code: "transport_failure") => void): () => void;
  rebind?(request: MediaTransportOpen): Promise<boolean>;
  close(): Promise<void>;
}>;

export type MediaTransportOpen = Readonly<{
  asset: PublicRuntimeAsset;
  ownerId: string;
  epoch: number;
  signal: AbortSignal;
}>;

export type MediaTransportFactory = (
  request: MediaTransportOpen,
) => Promise<MediaTransport>;

export type HtmlMediaElementSourceOwner = Readonly<{
  element: HTMLVideoElement;
  /** Same-authority bounded PCM WAV lease; consumers must not outlive release authority. */
  audioBody?: Blob;
  rebind?(
    request: MediaTransportOpen,
    context?: AuthoringMediaVideoSourceContext,
  ): Promise<boolean>;
  release(): Promise<void>;
}>;

export type HtmlMediaElementSourceOwnerFactory = (
  request: MediaTransportOpen,
) => Promise<HtmlMediaElementSourceOwner>;

export type RuntimeSceneResolver = (
  frame: number,
  snapshot: PublicCompositionSnapshot,
  context: Readonly<{ epoch: number; signal: AbortSignal }>,
) => unknown | Promise<unknown>;

export type RuntimeSourcePresentation = Readonly<{
  ownerId: string;
  assetId: string;
  sourceFrame: number;
  sourcePts: number;
  observedSourcePts?: number;
  observation?: "request_video_frame_callback" | "event_fallback";
}>;

export type RuntimeResourceSnapshot = Readonly<{
  activeVideoOwners: number;
  warmVideoOwners: number;
  canvasOwners: 0;
  pendingOperations: number;
  pendingRvfcOwners: number;
}>;

export type RuntimeSnapshot = Readonly<{
  schema: typeof RUNTIME_SNAPSHOT_SCHEMA;
  status: RuntimeSessionStatus;
  epoch: number;
  profileFingerprint: typeof RUNTIME_PROFILE_FINGERPRINT;
  publicFingerprint: string | null;
  outputFrame: number | null;
  sources: readonly RuntimeSourcePresentation[];
  blocker: Readonly<{
    code: RuntimeBlockerCode;
    subjectId: string | null;
  }> | null;
  resources: RuntimeResourceSnapshot;
}>;

export type RuntimeReceipt = Readonly<{
  schema: typeof RUNTIME_RECEIPT_SCHEMA;
  status: RuntimeReceiptStatus;
  epoch: number;
  outputFrame: number | null;
  blocker: Readonly<{
    code: RuntimeBlockerCode;
    subjectId: string | null;
  }> | null;
}>;

export type EditorRuntime = Readonly<{
  capabilities(): RuntimeCapabilityDisposition;
  open(
    manifest: PublicAssetManifest,
    snapshot: PublicCompositionSnapshot,
    profileFingerprint: string,
  ): Promise<RuntimeReceipt>;
  replace(
    manifest: PublicAssetManifest,
    snapshot: PublicCompositionSnapshot,
    initialFrame?: number | (() => number),
  ): Promise<RuntimeReceipt>;
  seek(outputFrame: number): Promise<RuntimeReceipt>;
  /** Present a playing frame from current native observations without issuing media controls. */
  advance(outputFrame: number): Promise<RuntimeReceipt>;
  /**
   * Prepare for the next ownership change while playing. `upcoming` holds the resolved scenes of
   * the coming changes, nearest first; the first one that needs a video owner the runtime does not
   * hold has that owner opened, sought to the scene's source position and held paused as the one
   * warm owner, so the move at the change adopts it instead of acquiring it. An empty list
   * releases a held owner. Best effort: a refusal or failure changes nothing else.
   */
  prepare?(upcoming: readonly unknown[]): Promise<RuntimeReceipt>;
  play(): Promise<RuntimeReceipt>;
  pause(): Promise<RuntimeReceipt>;
  snapshot(): RuntimeSnapshot;
  subscribe(listener: () => void): () => void;
  close(): Promise<RuntimeReceipt>;
}>;

type RuntimeDependencies = Readonly<{
  capability: RuntimeCapabilityDisposition;
  resolveScene: RuntimeSceneResolver;
  openTransport: MediaTransportFactory;
}>;

type TransportOwner = {
  assetId: string;
  transport: MediaTransport;
  unsubscribe: () => void;
  unsubscribeFailure?: () => void;
  latestPresentation?: MediaPresentation;
};

type VideoTarget = Readonly<{
  ownerId: string;
  asset: PublicRuntimeAsset;
  sourceFrame: number;
  sourcePts: number;
}>;

type WarmOwner = Readonly<{
  ownerId: string;
  assetId: string;
  sourcePts: number;
  transport: MediaTransport;
  unsubscribeFailure?: () => void;
}>;

type WarmPreparation = Readonly<{
  ownerId: string;
  assetId: string;
  /** Fulfils when the preparation has finished either way; never rejects. */
  settled: Promise<void>;
  abort: AbortController;
}>;

class RuntimeFailure extends Error {
  constructor(
    readonly code: RuntimeBlockerCode,
    readonly subjectId: string | null = null,
  ) {
    super(code);
  }
}

class EditorRuntimeController implements EditorRuntime {
  private epoch = 0;
  private operationAbort = new AbortController();
  private resumeAfterSeek = false;
  private pendingOperations = 0;
  private readonly inFlight = new Set<Promise<unknown>>();
  private currentSnapshot: PublicCompositionSnapshot | undefined;
  private currentManifest: PublicAssetManifest | undefined;
  private readonly owners = new Map<string, TransportOwner>();
  private readonly closingOwners = new Set<TransportOwner>();
  private readonly closingAttempts = new Map<TransportOwner, Promise<void>>();
  // The one warm owner the profile declares (`limits.warmVideoOwners`): opened and sought ahead of
  // an ownership change, paused, never part of `owners` until a move adopts it.
  private warm: WarmOwner | undefined;
  private preparing: WarmPreparation | undefined;
  private readonly listeners = new Set<() => void>();
  private value: RuntimeSnapshot = freezeSnapshot({
    schema: RUNTIME_SNAPSHOT_SCHEMA,
    status: "closed",
    epoch: 0,
    profileFingerprint: RUNTIME_PROFILE_FINGERPRINT,
    publicFingerprint: null,
    outputFrame: null,
    sources: [],
    blocker: null,
    resources: {
      activeVideoOwners: 0,
      warmVideoOwners: 0,
      canvasOwners: 0,
      pendingOperations: 0,
      pendingRvfcOwners: 0,
    },
  });

  constructor(private readonly dependencies: RuntimeDependencies) {}

  capabilities(): RuntimeCapabilityDisposition {
    return this.dependencies.capability;
  }

  snapshot(): RuntimeSnapshot {
    return this.value;
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  async open(
    manifest: PublicAssetManifest,
    snapshot: PublicCompositionSnapshot,
    profileFingerprint: string,
  ): Promise<RuntimeReceipt> {
    const blocker = this.validateSessionInput(
      manifest,
      snapshot,
      profileFingerprint,
    );
    if (blocker !== null)
      return this.blockAndRelease(blocker.code, blocker.subjectId);
    const { epoch, signal } = this.beginEpoch("opening");
    const released = await this.releaseCurrentEpoch();
    if (!released) return this.blockAndRelease("transport_failure");
    if (this.isStale(epoch, signal)) return this.cancelledReceipt(epoch);
    this.currentManifest = manifest;
    this.currentSnapshot = snapshot;
    this.publish({
      status: "opening",
      publicFingerprint: snapshot.publicFingerprint,
      outputFrame: null,
      sources: [],
      blocker: null,
    });
    return this.moveToFrame(0, epoch, signal, "paused");
  }

  async replace(
    manifest: PublicAssetManifest,
    snapshot: PublicCompositionSnapshot,
    initialFrame?: number | (() => number),
  ): Promise<RuntimeReceipt> {
    const blocker = this.validateSessionInput(
      manifest,
      snapshot,
      RUNTIME_PROFILE_FINGERPRINT,
    );
    if (blocker !== null)
      return this.blockAndRelease(blocker.code, blocker.subjectId);
    const priorFrame = this.value.outputFrame ?? 0;
    this.resumeAfterSeek = false;
    const { epoch, signal } = this.beginEpoch("seeking");
    const drained = await this.drainPendingOperations();
    if (this.isStale(epoch, signal)) return this.cancelledReceipt(epoch);
    if (!drained) return this.blockAndRelease("transport_failure");
    const pending = this.preparing;
    if (pending !== undefined) {
      const settled = await settleWithDeadline(pending.settled, 5_000);
      if (this.isStale(epoch, signal)) return this.cancelledReceipt(epoch);
      if (!settled) {
        pending.abort.abort();
        return this.blockAndRelease("transport_failure");
      }
      if (this.preparing === pending) this.preparing = undefined;
    }
    const warmReleased = await this.releaseWarm();
    if (this.isStale(epoch, signal)) return this.cancelledReceipt(epoch);
    if (!warmReleased || this.closingOwners.size !== 0)
      return this.blockAndRelease("transport_failure");
    this.currentManifest = manifest;
    this.currentSnapshot = snapshot;
    // IMPORTANT: an epoch cancels work, not source ownership. Releasing all owners here forces
    // every unchanged derivative to be downloaded/decoded again and clears the kept picture.
    this.publish({
      status: "seeking",
      publicFingerprint: snapshot.publicFingerprint,
      blocker: null,
    });
    try {
      for (const [ownerId, owner] of [...this.owners]) {
        if (this.isStale(epoch, signal)) return this.cancelledReceipt(epoch);
        const asset = publicAssetById(manifest, owner.assetId);
        const clip = snapshot.clips.find((row) => row.clipId === ownerId);
        if (
          asset === undefined ||
          clip?.assetId !== owner.assetId ||
          owner.transport.rebind === undefined
        ) {
          if (!(await this.closeOwners([ownerId])))
            throw new RuntimeFailure("transport_failure", ownerId);
          continue;
        }
        await this.runOperation(
          async () => {
            let retained = false;
            try {
              retained =
                (await owner.transport.rebind!({
                  asset,
                  ownerId,
                  epoch,
                  signal,
                })) === true;
            } finally {
              // IMPORTANT: cancelled reauthorization is cleanup, not a reusable old lease.
              // Drain includes this close; a successor cannot acquire beside its stale owner.
              if (
                (!retained || this.isStale(epoch, signal)) &&
                this.owners.get(ownerId) === owner &&
                !(await this.closeOwners([ownerId]))
              )
                throw new RuntimeFailure("transport_failure", ownerId);
            }
            return retained;
          },
          epoch,
          signal,
        );
      }
    } catch (error) {
      if (this.isStale(epoch, signal)) return this.cancelledReceipt(epoch);
      const failure = runtimeFailure(error);
      return this.blockAndRelease(failure.code, failure.subjectId);
    }
    if (this.isStale(epoch, signal)) return this.cancelledReceipt(epoch);
    const durationFrames = outputDurationFrames(snapshot);
    let requestedFrame: number;
    try {
      requestedFrame =
        typeof initialFrame === "function"
          ? initialFrame()
          : (initialFrame ?? priorFrame);
    } catch {
      return this.blockAndRelease("contract_mismatch");
    }
    if (!Number.isSafeInteger(requestedFrame) || requestedFrame < 0)
      return this.blockAndRelease("contract_mismatch");
    return this.moveToFrame(
      Math.min(requestedFrame, durationFrames - 1),
      epoch,
      signal,
      "paused",
    );
  }

  async seek(outputFrame: number): Promise<RuntimeReceipt> {
    if (
      this.value.status !== "paused" &&
      this.value.status !== "playing" &&
      this.value.status !== "seeking"
    )
      return this.blockedReceipt("unsupported");
    if (
      this.currentSnapshot === undefined ||
      this.currentManifest === undefined
    )
      return this.blockAndRelease("contract_mismatch");
    if (
      !Number.isSafeInteger(outputFrame) ||
      outputFrame < 0 ||
      outputFrame >= outputDurationFrames(this.currentSnapshot)
    )
      return this.blockAndRelease("unsupported");
    // IMPORTANT: a superseding seek inherits the prior seek's playback intent. Reading only the
    // transient `seeking` status would leave a rapid play -> seek -> seek sequence paused.
    const resume =
      this.value.status === "playing" ||
      (this.value.status === "seeking" && this.resumeAfterSeek);
    this.resumeAfterSeek = resume;
    const { epoch, signal } = this.beginEpoch("seeking");
    // IMPORTANT: abort listeners settle in a later microtask. Drain the superseded generation
    // before reserving the newest operation slots or a two-owner seek blocks its successor.
    const drained = await this.drainPendingOperations();
    if (this.isStale(epoch, signal)) return this.cancelledReceipt(epoch);
    if (!drained) return this.blockAndRelease("transport_failure");
    const receipt = await this.moveToFrame(
      outputFrame,
      epoch,
      signal,
      resume ? "playing" : "paused",
    );
    if (epoch === this.epoch) this.resumeAfterSeek = false;
    return receipt;
  }

  async play(): Promise<RuntimeReceipt> {
    if (this.pendingOperations !== 0) return this.blockedReceipt("unsupported");
    if (this.value.status === "playing") return this.receipt("applied");
    if (this.value.status !== "paused")
      return this.blockedReceipt("unsupported");
    if (this.currentSnapshot === undefined)
      return this.blockAndRelease("contract_mismatch");
    const epoch = this.epoch;
    const signal = this.operationAbort.signal;
    try {
      await this.runTransportBatch(
        [...this.owners.values()].map(
          (owner) => () => owner.transport.play(signal),
        ),
        epoch,
        signal,
      );
    } catch (error) {
      if (this.isStale(epoch, signal)) return this.cancelledReceipt(epoch);
      return this.blockAndRelease(runtimeFailure(error).code);
    }
    if (this.isStale(epoch, signal)) return this.cancelledReceipt(epoch);
    this.publish({ status: "playing", blocker: null });
    return this.receipt("applied");
  }

  async advance(outputFrame: number): Promise<RuntimeReceipt> {
    if (
      this.value.status !== "playing" ||
      this.currentSnapshot === undefined ||
      this.currentManifest === undefined ||
      !Number.isSafeInteger(outputFrame) ||
      outputFrame < 0 ||
      outputFrame >= outputDurationFrames(this.currentSnapshot)
    )
      return this.blockedReceipt("unsupported");
    const epoch = this.epoch;
    const signal = this.operationAbort.signal;
    const snapshot = this.currentSnapshot;
    const frameRate = snapshot.output.frameRate as Readonly<{
      num: number;
      den: number;
    }>;
    const primaryTrackIds = new Set(
      snapshot.tracks
        .filter((track) => track.enabled && track.kind === "primary_video")
        .map((track) => track.trackId),
    );
    const ownedClips = snapshot.clips
      .filter((clip) => clip.enabled && this.owners.has(clip.clipId))
      .sort((left, right) =>
        primaryTrackIds.has(left.trackId) !== primaryTrackIds.has(right.trackId)
          ? Number(primaryTrackIds.has(right.trackId)) -
            Number(primaryTrackIds.has(left.trackId))
          : left.startFrame !== right.startFrame
            ? right.startFrame - left.startFrame
            : right.clipId.localeCompare(left.clipId),
      );
    // CRITICAL: every current video owner must clock the shared scene before resolution. A
    // primary RVFC can precede an overlay RVFC by one frame; resolving from only the primary
    // publishes a future overlay PTS and makes the compositor clear healthy playback.
    for (const clip of ownedClips) {
      if (clip.assetId === null) continue;
      // Expired overlay owners must not pin a handoff to their last frame. The resolver below
      // detects ownership changes and returns the exact-seek signal for the next interval.
      if (
        !primaryTrackIds.has(clip.trackId) &&
        (outputFrame < clip.startFrame ||
          outputFrame >= clip.startFrame + clip.durationFrames)
      )
        continue;
      const asset = snapshot.assets.find(
        (candidate) => candidate.assetId === clip.assetId,
      );
      const observed = this.owners.get(clip.clipId)?.latestPresentation;
      const start = asset?.landmarks.find(
        (landmark) => landmark.frameIndex === clip.sourceStartFrame,
      );
      if (
        asset?.kind !== "video" ||
        asset.sourceTimeBase === null ||
        observed?.observedSourcePts === undefined ||
        start === undefined
      )
        return this.blockedReceipt("transport_failure", clip.clipId);
      const elapsedPts = BigInt(observed.observedSourcePts - start.pts);
      if (elapsedPts < 0n)
        return this.blockedReceipt("transport_failure", clip.clipId);
      const numerator =
        elapsedPts * BigInt(asset.sourceTimeBase.num) * BigInt(frameRate.num);
      const denominator =
        BigInt(asset.sourceTimeBase.den) * BigInt(frameRate.den);
      const observedFrame = clip.startFrame + Number(numerator / denominator);
      if (
        !Number.isSafeInteger(observedFrame) ||
        observedFrame < clip.startFrame ||
        (primaryTrackIds.has(clip.trackId) &&
          observedFrame >= clip.startFrame + clip.durationFrames)
      )
        return this.blockedReceipt("unsupported", clip.clipId);
      // CRITICAL: the final native frame cannot clock the first frame of an adjacent clip. Once
      // wall time requests that next half-open interval and the current owner has reached its
      // endpoint, return the scheduler's exact-seek signal; clamping here repaints the endpoint
      // forever after the HTMLMediaElement ends and never transfers source ownership.
      if (
        outputFrame >= clip.startFrame + clip.durationFrames &&
        observedFrame === clip.startFrame + clip.durationFrames - 1
      )
        return this.blockedReceipt("unsupported", clip.clipId);
      // Native media is the continuous clock. RAF may request ahead of the decoder; never relabel
      // a seek anchor or paint a future composition frame from wall time.
      outputFrame = Math.min(outputFrame, observedFrame);
    }
    // IMPORTANT: the native clock can remain on one decoded frame across many display RAFs.
    // Resolving and repainting that same clamped frame in a tight loop can starve the next RVFC,
    // freezing the editor on the first observation while the hidden media keeps playing.
    if (outputFrame === this.value.outputFrame) return this.receipt("applied");
    let scene: ResolvedCompositionScene;
    try {
      const unresolved = await this.runOperation(
        () =>
          Promise.resolve(
            this.dependencies.resolveScene(outputFrame, snapshot, {
              epoch,
              signal,
            }),
          ),
        epoch,
        signal,
      );
      if (this.isStale(epoch, signal)) return this.cancelledReceipt(epoch);
      scene = decodeResolvedScene(unresolved);
      if (
        scene.profileId !== ENGINE_PROFILE_ID ||
        scene.publicFingerprint !== snapshot.publicFingerprint ||
        scene.frame !== outputFrame ||
        scene.blockers.length !== 0
      )
        throw new RuntimeFailure("contract_mismatch");
      const targets = this.videoTargets(scene, snapshot, this.currentManifest);
      if (
        targets.length !== this.owners.size ||
        targets.some((target) => {
          const owner = this.owners.get(target.ownerId);
          return owner === undefined || owner.assetId !== target.asset.assetId;
        })
      )
        return this.blockedReceipt("unsupported");
      const sources = targets.map((target) => {
        const owner = this.owners.get(target.ownerId)!;
        const observed = owner.latestPresentation;
        const observation =
          observed === undefined
            ? null
            : this.presentationObservation(observed);
        if (
          observed === undefined ||
          observation === null ||
          observation.observedSourcePts === undefined ||
          observed.epoch !== epoch ||
          observed.ownerId !== target.ownerId ||
          observed.assetId !== target.asset.assetId
        )
          throw new RuntimeFailure("transport_failure", target.ownerId);
        const firstLandmark = target.asset.landmarks[0];
        // IMPORTANT: landmarks are sparse validation anchors, not a frame table. Native playback
        // legitimately reports PTS after the final sampled anchor while the admitted source still
        // has frames. Treating the final landmark as the source end blocks healthy playback midway
        // through sparse assets; the owned transport already bounds mediaTime to native duration.
        if (
          firstLandmark === undefined ||
          observation.observedSourcePts < firstLandmark.pts
        )
          throw new RuntimeFailure("transport_failure", target.ownerId);
        return Object.freeze({
          ownerId: target.ownerId,
          assetId: target.asset.assetId,
          sourceFrame: target.sourceFrame,
          sourcePts: target.sourcePts,
          ...observation,
        });
      });
      this.publish({
        status: "playing",
        outputFrame,
        sources: Object.freeze(sources),
        blocker: null,
      });
      return this.receipt("applied");
    } catch (error) {
      if (this.isStale(epoch, signal)) return this.cancelledReceipt(epoch);
      return this.blockedReceipt(runtimeFailure(error).code);
    }
  }

  async prepare(upcoming: readonly unknown[]): Promise<RuntimeReceipt> {
    const snapshot = this.currentSnapshot;
    const manifest = this.currentManifest;
    // An owner is prepared only while playing; a paused runtime may still give one back.
    if (
      (this.value.status !== "playing" && this.value.status !== "paused") ||
      snapshot === undefined ||
      manifest === undefined ||
      !Array.isArray(upcoming)
    )
      return this.blockedReceipt("unsupported");
    let target: VideoTarget | undefined;
    try {
      for (const wire of upcoming) {
        const scene = decodeResolvedScene(wire);
        if (
          scene.profileId !== ENGINE_PROFILE_ID ||
          scene.publicFingerprint !== snapshot.publicFingerprint ||
          scene.blockers.length !== 0
        )
          return this.blockedReceipt("contract_mismatch");
        target = this.videoTargets(scene, snapshot, manifest).find(
          (candidate) =>
            this.owners.get(candidate.ownerId)?.assetId !==
            candidate.asset.assetId,
        );
        if (target !== undefined) break;
      }
    } catch (error) {
      return this.blockedReceipt(
        error instanceof RuntimeFailure ? error.code : "contract_mismatch",
      );
    }
    const wanted = target;
    const holds = () =>
      wanted !== undefined &&
      this.warm?.ownerId === wanted.ownerId &&
      this.warm.assetId === wanted.asset.assetId &&
      this.warm.sourcePts === wanted.sourcePts;
    if (holds()) return this.receipt("applied");
    // IMPORTANT: one acquisition at a time, and none abandoned for another. The lease route is
    // no-queue: a second create while one runs is refused `busy`, and an aborted create can keep
    // that claim until its native work has stopped. A preparation in flight is therefore always
    // awaited, here and in `synchronizeOwners`; cancelling it to start the next one turns an
    // ordinary seek or re-preparation into a refused acquisition.
    const pending = this.preparing;
    if (pending !== undefined) {
      await pending.settled;
      if (holds()) return this.receipt("applied");
    }
    if (!(await this.releaseWarm()))
      return this.blockedReceipt("transport_failure");
    if (wanted === undefined) return this.receipt("applied");
    if (
      this.value.status !== "playing" ||
      this.currentSnapshot !== snapshot ||
      this.currentManifest !== manifest ||
      this.preparing !== undefined ||
      this.warm !== undefined
    )
      return this.blockedReceipt("unsupported");
    if (this.owners.get(wanted.ownerId)?.assetId === wanted.asset.assetId)
      return this.receipt("applied");
    if (RUNTIME_PROFILE.limits.warmVideoOwners < 1)
      return this.blockedReceipt("resource_limit");

    // CRITICAL: a preparation is not an operation of the current epoch. It must not count in
    // `pendingOperations` (pause and play are refused while that is non-zero, and a user can pause
    // at any moment of a half-second acquisition), it must not be in the set a seek drains (the
    // drain has a 250 ms deadline and blocks the session when it is missed), and it must not hang
    // on the epoch's abort signal (the boundary seek that wants this owner begins a new epoch).
    const abort = new AbortController();
    const epoch = this.epoch;
    let failure: RuntimeBlockerCode | null = null;
    const settled = (async () => {
      let transport: unknown;
      try {
        transport = await this.dependencies.openTransport({
          asset: wanted.asset,
          ownerId: wanted.ownerId,
          epoch,
          signal: abort.signal,
        });
      } catch (error) {
        failure = abort.signal.aborted
          ? "cancelled"
          : runtimeFailure(error).code;
        return;
      }
      if (abort.signal.aborted) {
        this.quarantineCancelledTransport(transport, wanted.asset.assetId);
        failure = "cancelled";
        return;
      }
      if (
        !isMediaTransport(transport) ||
        transport.ownerId !== wanted.ownerId ||
        transport.assetId !== wanted.asset.assetId ||
        transport.openedEpoch !== epoch
      ) {
        this.quarantineCancelledTransport(transport, wanted.asset.assetId);
        failure = "contract_mismatch";
        return;
      }
      const opened = transport;
      let unsubscribeFailure: (() => void) | undefined;
      try {
        unsubscribeFailure = opened.subscribeFailure?.(() => {
          // A failing warm owner is closed alone; the playing owners are not its casualties.
          if (this.warm?.transport === opened) void this.releaseWarm();
        });
      } catch {
        this.quarantineValidTransport(opened, wanted.asset.assetId);
        failure = "transport_failure";
        return;
      }
      // Held before its seek, so every release path of the runtime sees and closes it.
      const owner: WarmOwner = {
        ownerId: wanted.ownerId,
        assetId: wanted.asset.assetId,
        sourcePts: wanted.sourcePts,
        transport: opened,
        unsubscribeFailure,
      };
      this.warm = owner;
      this.publishResources();
      try {
        const presentation = await opened.seek({
          epoch,
          sourceFrame: wanted.sourceFrame,
          sourcePts: wanted.sourcePts,
          signal: abort.signal,
          preroll: true,
        });
        if (
          presentation.ownerId !== wanted.ownerId ||
          presentation.assetId !== wanted.asset.assetId ||
          presentation.sourceFrame !== wanted.sourceFrame ||
          presentation.sourcePts !== wanted.sourcePts ||
          this.presentationObservation(presentation) === null
        )
          throw new RuntimeFailure("contract_mismatch", wanted.ownerId);
      } catch (error) {
        failure = abort.signal.aborted
          ? "cancelled"
          : runtimeFailure(error).code;
        if (this.warm === owner) await this.releaseWarm();
      }
    })().then(
      () => undefined,
      () => undefined,
    );
    const preparation: WarmPreparation = {
      ownerId: wanted.ownerId,
      assetId: wanted.asset.assetId,
      settled,
      abort,
    };
    this.preparing = preparation;
    await settled;
    if (this.preparing === preparation) this.preparing = undefined;
    if (failure !== null) return this.blockedReceipt(failure, wanted.ownerId);
    return holds()
      ? this.receipt("applied")
      : this.blockedReceipt("cancelled", wanted.ownerId);
  }

  async pause(): Promise<RuntimeReceipt> {
    if (this.pendingOperations !== 0) return this.blockedReceipt("unsupported");
    if (this.value.status === "paused") return this.receipt("applied");
    if (this.value.status !== "playing")
      return this.blockedReceipt("unsupported");
    if (this.currentSnapshot === undefined)
      return this.blockAndRelease("contract_mismatch");
    const epoch = this.epoch;
    const signal = this.operationAbort.signal;
    try {
      await this.runTransportBatch(
        [...this.owners.values()].map((owner) => () => owner.transport.pause()),
        epoch,
        signal,
      );
    } catch (error) {
      if (this.isStale(epoch, signal)) return this.cancelledReceipt(epoch);
      return this.blockAndRelease(runtimeFailure(error).code);
    }
    if (this.isStale(epoch, signal)) return this.cancelledReceipt(epoch);
    this.publish({ status: "paused", blocker: null });
    return this.receipt("applied");
  }

  async close(): Promise<RuntimeReceipt> {
    if (
      this.value.status === "closed" &&
      this.currentSnapshot === undefined &&
      this.owners.size === 0 &&
      this.closingOwners.size === 0
    ) {
      if (this.value.blocker !== null) this.publish({ blocker: null });
      return this.receipt("closed");
    }
    this.beginEpoch("closing");
    const released = await this.releaseCurrentEpoch();
    this.currentManifest = undefined;
    this.currentSnapshot = undefined;
    this.publish({
      status: "closed",
      publicFingerprint: null,
      outputFrame: null,
      sources: [],
      blocker: released ? null : { code: "transport_failure", subjectId: null },
    });
    return this.receipt("closed");
  }

  private validateSessionInput(
    manifest: PublicAssetManifest,
    snapshot: PublicCompositionSnapshot,
    profileFingerprint: string,
  ): Readonly<{ code: RuntimeBlockerCode; subjectId: string | null }> | null {
    if (
      profileFingerprint !== RUNTIME_PROFILE_FINGERPRINT ||
      snapshot.profileId !== ENGINE_PROFILE_ID
    )
      return { code: "profile_unavailable", subjectId: null };
    const capabilityBlocker = capabilityActivationBlocker(
      this.dependencies.capability,
    );
    if (capabilityBlocker !== null)
      return { code: capabilityBlocker, subjectId: null };
    try {
      validatePublicAssetManifest(manifest, snapshot);
    } catch {
      return { code: "contract_mismatch", subjectId: null };
    }
    if (snapshot.blockers.length !== 0)
      return {
        code: "snapshot_blocked",
        subjectId: snapshot.blockers[0]?.subjectId ?? null,
      };
    return null;
  }

  private beginEpoch(status: RuntimeSessionStatus): {
    epoch: number;
    signal: AbortSignal;
  } {
    if (status !== "seeking") this.resumeAfterSeek = false;
    this.operationAbort.abort();
    this.operationAbort = new AbortController();
    this.epoch += 1;
    this.publish({
      status,
      epoch: this.epoch,
      blocker: null,
    });
    return { epoch: this.epoch, signal: this.operationAbort.signal };
  }

  private async moveToFrame(
    outputFrame: number,
    epoch: number,
    signal: AbortSignal,
    finalStatus: "paused" | "playing",
  ): Promise<RuntimeReceipt> {
    const snapshot = this.currentSnapshot;
    const manifest = this.currentManifest;
    if (snapshot === undefined || manifest === undefined)
      return this.blockAndRelease("contract_mismatch");

    let scene: ResolvedCompositionScene;
    try {
      const unresolved = await this.runOperation(
        () =>
          Promise.resolve(
            this.dependencies.resolveScene(outputFrame, snapshot, {
              epoch,
              signal,
            }),
          ),
        epoch,
        signal,
      );
      if (this.isStale(epoch, signal)) return this.cancelledReceipt(epoch);
      scene = decodeResolvedScene(unresolved);
    } catch {
      if (this.isStale(epoch, signal)) return this.cancelledReceipt(epoch);
      return this.blockAndRelease("contract_mismatch");
    }
    if (
      scene.profileId !== ENGINE_PROFILE_ID ||
      scene.publicFingerprint !== snapshot.publicFingerprint ||
      scene.frame !== outputFrame
    )
      return this.blockAndRelease("contract_mismatch");
    if (scene.blockers.length !== 0)
      return this.blockAndRelease(
        "snapshot_blocked",
        scene.blockers[0]?.subjectId ?? null,
      );

    let targets: readonly VideoTarget[];
    try {
      targets = this.videoTargets(scene, snapshot, manifest);
      await this.synchronizeOwners(targets, epoch, signal);
      if (this.isStale(epoch, signal)) return this.cancelledReceipt(epoch);
      const presentations = await this.seekOwners(targets, epoch, signal);
      if (this.isStale(epoch, signal)) return this.cancelledReceipt(epoch);
      if (finalStatus === "playing") {
        await this.runTransportBatch(
          [...this.owners.values()].map(
            (owner) => () => owner.transport.play(signal),
          ),
          epoch,
          signal,
        );
        if (this.isStale(epoch, signal)) return this.cancelledReceipt(epoch);
      }
      this.publish({
        status: finalStatus,
        outputFrame,
        sources: presentations,
        blocker: null,
      });
      return this.receipt("applied");
    } catch (error) {
      if (this.isStale(epoch, signal)) return this.cancelledReceipt(epoch);
      const failure = runtimeFailure(error);
      return this.blockAndRelease(failure.code, failure.subjectId);
    }
  }

  private videoTargets(
    scene: ResolvedCompositionScene,
    snapshot: PublicCompositionSnapshot,
    manifest: PublicAssetManifest,
  ): readonly VideoTarget[] {
    const clips = new Map(snapshot.clips.map((clip) => [clip.clipId, clip]));
    const targets: VideoTarget[] = [];
    const owners = new Set<string>();
    for (const layer of scene.layers) {
      const clipId = layer.clipId as string;
      const assetId = layer.assetId as string | null;
      const clip = clips.get(clipId);
      if (
        clip === undefined ||
        clip.assetId !== assetId ||
        clip.trackId !== layer.trackId
      )
        throw new RuntimeFailure("contract_mismatch", clipId);
      if (assetId === null) continue;
      const asset = publicAssetById(manifest, assetId);
      if (asset === undefined)
        throw new RuntimeFailure("contract_mismatch", clipId);
      if (asset.kind !== "video") continue;
      if (
        owners.has(clipId) ||
        !Number.isSafeInteger(layer.sourceFrame) ||
        (layer.sourceFrame as number) < 0 ||
        !Number.isSafeInteger(layer.sourcePts) ||
        (layer.sourcePts as number) < 0
      )
        throw new RuntimeFailure("contract_mismatch", clipId);
      owners.add(clipId);
      targets.push({
        ownerId: clipId,
        asset,
        sourceFrame: layer.sourceFrame as number,
        sourcePts: layer.sourcePts as number,
      });
    }
    if (targets.length > RUNTIME_PROFILE.limits.activeVideoOwners)
      throw new RuntimeFailure("resource_limit");
    return Object.freeze(targets);
  }

  private async synchronizeOwners(
    targets: readonly VideoTarget[],
    epoch: number,
    signal: AbortSignal,
  ): Promise<void> {
    const targetByOwner = new Map(
      targets.map((target) => [target.ownerId, target]),
    );
    const remove = [...this.owners.entries()]
      .filter(
        ([ownerId, owner]) =>
          targetByOwner.get(ownerId)?.asset.assetId !== owner.assetId,
      )
      .map(([ownerId]) => ownerId);
    if (!(await this.closeOwners(remove)))
      throw new RuntimeFailure("transport_failure");
    if (this.isStale(epoch, signal)) throw new RuntimeFailure("cancelled");

    const needed = targets.filter((target) => !this.owners.has(target.ownerId));
    if (
      needed.length !== 0 &&
      (this.preparing !== undefined || this.warm !== undefined)
    ) {
      // IMPORTANT: wait for a preparation in flight, never abort it (see `prepare`), then keep
      // the warm owner only if this move wants it. Releasing it first also returns its place in
      // the resource layer's two-owner admission before another transport is opened.
      // The wait is deliberately not a `runOperation`: a superseding seek drains those within
      // the cancel deadline and blocks the session when one outlasts it, and an acquisition
      // that is never aborted outlasts it routinely.
      const pending = this.preparing;
      if (pending !== undefined) {
        await pending.settled;
        if (this.preparing === pending) this.preparing = undefined;
        if (this.isStale(epoch, signal)) throw new RuntimeFailure("cancelled");
      }
      const warm = this.warm;
      if (
        warm !== undefined &&
        !needed.some(
          (target) =>
            target.ownerId === warm.ownerId &&
            target.asset.assetId === warm.assetId,
        )
      ) {
        if (!(await this.releaseWarm()))
          throw new RuntimeFailure("transport_failure");
        if (this.isStale(epoch, signal)) throw new RuntimeFailure("cancelled");
      }
    }

    for (const target of targets) {
      if (this.owners.has(target.ownerId)) continue;
      if (
        this.activeVideoOwnerCount() >= RUNTIME_PROFILE.limits.activeVideoOwners
      )
        throw new RuntimeFailure("resource_limit");
      const warm = this.warm;
      if (
        warm !== undefined &&
        warm.ownerId === target.ownerId &&
        warm.assetId === target.asset.assetId
      ) {
        // The owner was opened and sought ahead of this move: adopt it. Its seek to the same
        // source position is then the transport's cached path, with no native wait.
        this.warm = undefined;
        try {
          warm.unsubscribeFailure?.();
        } catch {
          this.quarantineValidTransport(warm.transport, warm.assetId);
          throw new RuntimeFailure("transport_failure", target.ownerId);
        }
        this.registerOwner(target, warm.transport);
        continue;
      }
      let transport: unknown;
      try {
        transport = await this.runOperation(
          () =>
            this.dependencies.openTransport({
              asset: target.asset,
              ownerId: target.ownerId,
              epoch,
              signal,
            }),
          epoch,
          signal,
          (opened) =>
            this.quarantineCancelledTransport(opened, target.asset.assetId),
        );
      } catch (error) {
        throw runtimeFailure(error);
      }
      if (this.isStale(epoch, signal)) {
        this.quarantineCancelledTransport(transport, target.asset.assetId);
        throw new RuntimeFailure("cancelled");
      }
      if (
        !isMediaTransport(transport) ||
        transport.ownerId !== target.ownerId ||
        transport.assetId !== target.asset.assetId ||
        transport.openedEpoch !== epoch
      ) {
        if (isMediaTransport(transport)) {
          this.quarantineValidTransport(transport, target.asset.assetId);
        } else {
          const close = mediaTransportClose(transport);
          if (
            close !== undefined &&
            !(await settleWithDeadline(
              close(),
              RUNTIME_PROFILE.limits.teardownDeadlineMs,
            ))
          )
            throw new RuntimeFailure("transport_failure", target.ownerId);
        }
        throw new RuntimeFailure("contract_mismatch", target.ownerId);
      }
      this.registerOwner(target, transport);
    }
  }

  private registerOwner(target: VideoTarget, transport: MediaTransport): void {
    let unsubscribe: (() => void) | undefined;
    let unsubscribeFailure: (() => void) | undefined;
    try {
      unsubscribe = transport.subscribe((value) =>
        this.observePresentation(value),
      );
      unsubscribeFailure = transport.subscribeFailure?.((code) =>
        this.observeTransportFailure(transport, code),
      );
    } catch {
      this.quarantineValidTransport(
        transport,
        target.asset.assetId,
        unsubscribe,
        unsubscribeFailure,
      );
      throw new RuntimeFailure("transport_failure", target.ownerId);
    }
    if (typeof unsubscribe !== "function") {
      const clean = this.quarantineValidTransport(
        transport,
        target.asset.assetId,
        undefined,
        unsubscribeFailure,
      );
      throw new RuntimeFailure(
        clean ? "contract_mismatch" : "transport_failure",
        target.ownerId,
      );
    }
    if (
      transport.subscribeFailure !== undefined &&
      typeof unsubscribeFailure !== "function"
    ) {
      const clean = this.quarantineValidTransport(
        transport,
        target.asset.assetId,
        unsubscribe,
      );
      throw new RuntimeFailure(
        clean ? "contract_mismatch" : "transport_failure",
        target.ownerId,
      );
    }
    this.owners.set(target.ownerId, {
      assetId: target.asset.assetId,
      transport,
      unsubscribe,
      unsubscribeFailure,
      latestPresentation: undefined,
    });
    this.publishResources();
  }

  /** Close the warm owner, if any. It stays counted as closing until its close fulfils. */
  private async releaseWarm(): Promise<boolean> {
    const warm = this.warm;
    if (warm === undefined) return true;
    this.warm = undefined;
    const owner: TransportOwner = {
      assetId: warm.assetId,
      transport: warm.transport,
      unsubscribe: () => undefined,
      unsubscribeFailure: warm.unsubscribeFailure,
    };
    let clean = true;
    this.closingOwners.add(owner);
    try {
      owner.unsubscribeFailure?.();
    } catch {
      clean = false;
    }
    this.publishResources();
    const closed = await settleWithDeadline(
      this.closeOwner(owner),
      RUNTIME_PROFILE.limits.teardownDeadlineMs,
    );
    return clean && closed;
  }

  private async seekOwners(
    targets: readonly VideoTarget[],
    epoch: number,
    signal: AbortSignal,
  ): Promise<readonly RuntimeSourcePresentation[]> {
    const results = await this.runTransportBatch(
      targets.map((target) => {
        const owner = this.owners.get(target.ownerId);
        if (owner === undefined) throw new RuntimeFailure("transport_failure");
        return () =>
          owner.transport.seek({
            epoch,
            sourceFrame: target.sourceFrame,
            sourcePts: target.sourcePts,
            signal,
          });
      }),
      epoch,
      signal,
    );
    return Object.freeze(
      results.map((result, index) => {
        const target = targets[index]!;
        if (
          result.ownerId !== target.ownerId ||
          result.assetId !== target.asset.assetId ||
          result.epoch !== epoch ||
          result.sourceFrame !== target.sourceFrame ||
          result.sourcePts !== target.sourcePts
        )
          throw new RuntimeFailure("contract_mismatch", target.ownerId);
        const observation = this.presentationObservation(result);
        if (observation === null)
          throw new RuntimeFailure("contract_mismatch", target.ownerId);
        const owner = this.owners.get(target.ownerId);
        if (owner === undefined)
          throw new RuntimeFailure("transport_failure", target.ownerId);
        owner.latestPresentation = result;
        return Object.freeze({
          ownerId: result.ownerId,
          assetId: result.assetId,
          sourceFrame: result.sourceFrame,
          sourcePts: result.sourcePts,
          ...observation,
        });
      }),
    );
  }

  private async runTransportBatch<T>(
    operations: readonly (() => Promise<T>)[],
    epoch: number,
    signal: AbortSignal,
  ): Promise<readonly T[]> {
    if (
      this.pendingOperations + operations.length >
      RUNTIME_PROFILE.limits.pendingOperations
    )
      throw new RuntimeFailure("resource_limit");
    return Promise.all(
      operations.map((operation) =>
        this.runOperation(operation, epoch, signal),
      ),
    );
  }

  private async runOperation<T>(
    operation: () => Promise<T>,
    epoch: number,
    signal: AbortSignal,
    onStaleValue?: (value: T) => void,
  ): Promise<T> {
    if (this.pendingOperations >= RUNTIME_PROFILE.limits.pendingOperations)
      throw new RuntimeFailure("resource_limit");
    this.pendingOperations += 1;
    this.publishResources();
    const pending = Promise.resolve().then(operation);
    this.inFlight.add(pending);
    try {
      // Let the operation acquire its transport-side handle before publishing resource metrics.
      await Promise.resolve();
      this.publishResources();
      const value = await pending;
      if (this.isStale(epoch, signal)) {
        onStaleValue?.(value);
        throw new RuntimeFailure("cancelled");
      }
      return value;
    } catch (error) {
      if (this.isStale(epoch, signal)) throw new RuntimeFailure("cancelled");
      throw runtimeFailure(error);
    } finally {
      this.inFlight.delete(pending);
      this.pendingOperations -= 1;
      this.publishResources();
    }
  }

  private async releaseCurrentEpoch(): Promise<boolean> {
    // The session lets a preparation finish before it closes its owners (the no-queue route);
    // this abort is the bounded last resort for a runtime that is closed under one.
    const pending = this.preparing;
    pending?.abort.abort();
    const [released, drained, prepared] = await Promise.all([
      this.closeOwners(),
      this.drainPendingOperations(),
      pending === undefined
        ? true
        : settleWithDeadline(
            pending.settled,
            RUNTIME_PROFILE.limits.cancelDeadlineMs,
          ),
    ]);
    if (this.preparing === pending) this.preparing = undefined;
    // A preparation that held its owner after `closeOwners` had looked is closed here.
    const warmReleased = await this.releaseWarm();
    return (
      released &&
      drained &&
      prepared &&
      warmReleased &&
      this.closingOwners.size === 0
    );
  }

  private async drainPendingOperations(): Promise<boolean> {
    const pending = [...this.inFlight];
    if (pending.length === 0) return true;
    return settleWithDeadline(
      Promise.allSettled(pending).then(() => undefined),
      RUNTIME_PROFILE.limits.cancelDeadlineMs,
    );
  }

  private quarantineCancelledTransport(value: unknown, assetId: string): void {
    if (isMediaTransport(value)) {
      this.quarantineValidTransport(value, assetId);
      return;
    }
    const close = mediaTransportClose(value);
    if (close !== undefined)
      void settleWithDeadline(
        close(),
        RUNTIME_PROFILE.limits.teardownDeadlineMs,
      );
  }

  private quarantineValidTransport(
    transport: MediaTransport,
    assetId: string,
    unsubscribe: (() => void) | undefined = undefined,
    unsubscribeFailure: (() => void) | undefined = undefined,
  ): boolean {
    // IMPORTANT: a valid transport remains owned until close fulfills, even if cancellation or
    // malformed subscriptions prevent normal owner registration. Dropping it reports false zero.
    if (
      [...this.owners.values()].some(
        (owner) => owner.transport === transport,
      ) ||
      [...this.closingOwners].some((owner) => owner.transport === transport)
    )
      return true;
    const owner: TransportOwner = {
      assetId,
      transport,
      unsubscribe: unsubscribe ?? (() => undefined),
      unsubscribeFailure,
    };
    let clean = true;
    this.closingOwners.add(owner);
    try {
      owner.unsubscribe();
    } catch {
      clean = false;
      // Close remains authoritative and the owner stays quarantined until it fulfills.
    }
    try {
      owner.unsubscribeFailure?.();
    } catch {
      clean = false;
      // Close remains authoritative and the owner stays quarantined until it fulfills.
    }
    this.publishResources();
    void this.closeOwner(owner);
    return clean;
  }

  private async closeOwners(ownerIds?: readonly string[]): Promise<boolean> {
    const ids = new Set(ownerIds ?? [...this.owners.keys()]);
    const selected = new Set<TransportOwner>();
    let clean = true;
    for (const ownerId of ids) {
      const owner = this.owners.get(ownerId);
      if (owner === undefined) continue;
      this.owners.delete(ownerId);
      this.closingOwners.add(owner);
      selected.add(owner);
      try {
        owner.unsubscribe();
      } catch {
        clean = false;
      }
      try {
        owner.unsubscribeFailure?.();
      } catch {
        clean = false;
      }
    }
    if (ownerIds === undefined) {
      const warm = this.warm;
      if (warm !== undefined) {
        this.warm = undefined;
        this.closingOwners.add({
          assetId: warm.assetId,
          transport: warm.transport,
          unsubscribe: () => undefined,
          unsubscribeFailure: warm.unsubscribeFailure,
        });
        try {
          warm.unsubscribeFailure?.();
        } catch {
          clean = false;
        }
      }
      for (const owner of this.closingOwners) selected.add(owner);
    }
    this.publishResources();
    if (selected.size === 0) return clean;
    const closing = [...selected].map((owner) => this.closeOwner(owner));
    const closed = await settleWithDeadline(
      Promise.allSettled(closing).then((results) => {
        if (results.some((result) => result.status === "rejected"))
          throw new Error("transport_failure");
      }),
      RUNTIME_PROFILE.limits.teardownDeadlineMs,
    );
    return clean && closed;
  }

  private closeOwner(owner: TransportOwner): Promise<void> {
    const pending = this.closingAttempts.get(owner);
    if (pending !== undefined) return pending;
    const closing = Promise.resolve().then(() => owner.transport.close());
    this.closingAttempts.set(owner, closing);
    void closing.then(
      () => {
        this.closingAttempts.delete(owner);
        this.closingOwners.delete(owner);
        this.publishResources();
      },
      () => {
        // IMPORTANT: a rejected close does not prove release. Retain the owner so a later
        // close/replace can retry and resource snapshots cannot report a false zero.
        this.closingAttempts.delete(owner);
        this.publishResources();
      },
    );
    return closing;
  }

  private observePresentation(value: MediaPresentation): void {
    // CRITICAL: a native callback belongs to the epoch that requested it. Publishing a late
    // callback after replace/seek would present a frame under the wrong accepted snapshot.
    if (
      value.epoch !== this.epoch ||
      this.operationAbort.signal.aborted ||
      (this.value.status !== "paused" && this.value.status !== "playing") ||
      !Number.isSafeInteger(value.sourceFrame) ||
      value.sourceFrame < 0 ||
      !Number.isSafeInteger(value.sourcePts) ||
      value.sourcePts < 0
    )
      return;
    const owner = this.owners.get(value.ownerId);
    if (owner === undefined || owner.assetId !== value.assetId) return;
    const source = this.value.sources.find(
      (candidate) => candidate.ownerId === value.ownerId,
    );
    const anchor = owner.latestPresentation;
    if (
      source === undefined ||
      anchor === undefined ||
      // IMPORTANT: native playback callbacks retain the exact-seek anchor while observed PTS
      // advances. The published scene source changes on every continuous frame; comparing a later
      // callback to that target rejects every observation after the first and freezes playback.
      anchor.sourceFrame !== value.sourceFrame ||
      anchor.sourcePts !== value.sourcePts
    )
      return;
    const observation = this.presentationObservation(value);
    if (observation === null) return;
    owner.latestPresentation = value;
    const sources = this.value.sources.map((source) =>
      source.ownerId === value.ownerId
        ? Object.freeze({
            ...source,
            ...observation,
          })
        : source,
    );
    this.publish({ sources });
  }

  private presentationObservation(value: MediaPresentation): Readonly<{
    observedSourcePts?: number;
    observation?: "request_video_frame_callback" | "event_fallback";
  }> | null {
    const hasPts = value.observedSourcePts !== undefined;
    const hasObservation = value.observation !== undefined;
    if (hasPts !== hasObservation) return null;
    if (!hasPts) return {};
    if (
      !Number.isSafeInteger(value.observedSourcePts) ||
      (value.observedSourcePts as number) < 0 ||
      (value.observation !== "request_video_frame_callback" &&
        value.observation !== "event_fallback")
    )
      return null;
    return {
      observedSourcePts: value.observedSourcePts,
      observation: value.observation,
    };
  }

  private observeTransportFailure(
    transport: MediaTransport,
    code: "transport_failure",
  ): void {
    const owner = this.owners.get(transport.ownerId);
    if (
      code !== "transport_failure" ||
      owner?.transport !== transport ||
      this.operationAbort.signal.aborted ||
      this.value.status === "blocked" ||
      this.value.status === "closing" ||
      this.value.status === "closed"
    )
      return;
    void this.blockAndRelease("transport_failure");
  }

  private async blockAndRelease(
    code: RuntimeBlockerCode,
    subjectId: string | null = null,
  ): Promise<RuntimeReceipt> {
    this.beginEpoch("blocked");
    const released = await this.releaseCurrentEpoch();
    this.currentManifest = undefined;
    this.currentSnapshot = undefined;
    const blocker = released
      ? { code, subjectId }
      : { code: "transport_failure" as const, subjectId: null };
    this.publish({
      status: "blocked",
      publicFingerprint: null,
      outputFrame: null,
      sources: [],
      blocker,
    });
    return this.receipt("blocked");
  }

  private isStale(epoch: number, signal: AbortSignal): boolean {
    return epoch !== this.epoch || signal.aborted;
  }

  private cancelledReceipt(epoch: number): RuntimeReceipt {
    return deepFreeze({
      schema: RUNTIME_RECEIPT_SCHEMA,
      status: "cancelled" as const,
      epoch,
      outputFrame: this.value.outputFrame,
      blocker: { code: "cancelled" as const, subjectId: null },
    });
  }

  private blockedReceipt(
    code: RuntimeBlockerCode,
    subjectId: string | null = null,
  ): RuntimeReceipt {
    return deepFreeze({
      schema: RUNTIME_RECEIPT_SCHEMA,
      status: "blocked" as const,
      epoch: this.epoch,
      outputFrame: this.value.outputFrame,
      blocker: { code, subjectId },
    });
  }

  private receipt(status: RuntimeReceiptStatus): RuntimeReceipt {
    return deepFreeze({
      schema: RUNTIME_RECEIPT_SCHEMA,
      status,
      epoch: this.epoch,
      outputFrame: this.value.outputFrame,
      blocker: this.value.blocker,
    });
  }

  private publishResources(): void {
    this.publish({
      resources: {
        activeVideoOwners: this.activeVideoOwnerCount(),
        warmVideoOwners: this.warm === undefined ? 0 : 1,
        canvasOwners: 0,
        pendingOperations: this.pendingOperations,
        pendingRvfcOwners: this.pendingFrameObservationOwners(),
      },
    });
  }

  private pendingFrameObservationOwners(): number {
    let total = 0;
    for (const owner of [
      ...this.owners.values(),
      ...this.closingOwners.values(),
      ...(this.warm === undefined ? [] : [this.warm]),
    ]) {
      const pending = owner.transport.pendingFrameObservations;
      if (!Number.isSafeInteger(pending) || pending < 0 || pending > 1)
        return RUNTIME_PROFILE.limits.pendingRvfcOwners + 1;
      total += pending;
    }
    return total;
  }

  private activeVideoOwnerCount(): number {
    return this.owners.size + this.closingOwners.size;
  }

  private publish(update: Partial<RuntimeSnapshot>): void {
    this.value = freezeSnapshot({
      ...this.value,
      ...update,
      resources: update.resources ?? this.value.resources,
      sources: update.sources ?? this.value.sources,
      blocker:
        update.blocker === undefined ? this.value.blocker : update.blocker,
    });
    for (const listener of [...this.listeners]) {
      try {
        listener();
      } catch {
        // External-store observers do not own the runtime state machine.
      }
    }
  }
}

export function createEditorRuntime(
  dependencies: RuntimeDependencies,
): EditorRuntime {
  return new EditorRuntimeController(dependencies);
}

export const NATIVE_MEDIA_OPERATION_DEADLINE_MS = 3_000 as const;
const NATIVE_CURRENT_TIME_UNITS_PER_SECOND = 1_000_000 as const;

export type HtmlMediaElementTransportOptions = Readonly<{
  frameObserver?: "request_video_frame_callback" | "event_fallback";
  seekObservation?: "observed_interval" | "exact_source_pts";
  onPlaybackFailure?: (
    failure: Readonly<{
      ownerId: string;
      openedEpoch: number;
      reason: "autoplay_blocked" | "playback_unavailable";
    }>,
  ) => void;
}>;

type NativeFrameObservation = Readonly<{
  observedSourcePts: number;
  observation: "request_video_frame_callback" | "event_fallback";
}>;

type NativeSourceAnchor = Readonly<{
  epoch: number;
  sourceFrame: number;
  sourcePts: number;
}>;

class HtmlMediaElementTransport implements MediaTransport {
  readonly ownerId: string;
  readonly assetId: string;
  readonly openedEpoch: number;
  private closeState: "open" | "closing" | "close_failed" | "closed" = "open";
  private closePromise: Promise<void> | undefined;
  private playing = false;
  private playbackGeneration = 0;
  private sourceAnchor: NativeSourceAnchor | undefined;
  private lastRequestedSourcePts: number | undefined;
  private lastObservation: NativeFrameObservation | undefined;
  private activeFrameCancel: ((failure?: RuntimeFailure) => void) | undefined;
  private readonly listeners = new Set<(value: MediaPresentation) => void>();
  private readonly failureListeners = new Set<
    (code: "transport_failure") => void
  >();
  private readonly cancelPendingCommands = new Set<
    (failure: RuntimeFailure) => void
  >();
  private failureReported = false;
  private nativeListenersAttached = false;

  private readonly onMediaFailure = () => {
    if (this.closeState !== "open") return;
    this.playbackGeneration += 1;
    this.playing = false;
    this.stopFrameObservation(new RuntimeFailure("transport_failure"));
    for (const cancel of [...this.cancelPendingCommands])
      cancel(new RuntimeFailure("transport_failure"));
    try {
      this.sourceOwner.element.pause();
    } catch {
      // The failure signal below remains authoritative when the native pause also fails.
    }
    this.reportFailure();
  };

  private readonly onMediaEnded = () => {
    this.playbackGeneration += 1;
    this.playing = false;
    this.stopFrameObservation();
  };

  get pendingFrameObservations(): number {
    return this.activeFrameCancel === undefined ? 0 : 1;
  }

  constructor(
    private readonly asset: PublicRuntimeAsset,
    private readonly sourceOwner: HtmlMediaElementSourceOwner,
    request: MediaTransportOpen,
    private readonly frameObserver:
      "request_video_frame_callback" | "event_fallback",
    private readonly seekObservation: "observed_interval" | "exact_source_pts",
    private readonly onPlaybackFailure: HtmlMediaElementTransportOptions["onPlaybackFailure"],
  ) {
    this.ownerId = request.ownerId;
    this.assetId = request.asset.assetId;
    this.openedEpoch = request.epoch;
    this.attachNativeListeners();
  }

  async rebind(request: MediaTransportOpen): Promise<boolean> {
    if (
      this.closeState !== "open" ||
      request.signal.aborted ||
      request.ownerId !== this.ownerId ||
      canonicalPublicRuntimeAssetFingerprint(request.asset) !==
        canonicalPublicRuntimeAssetFingerprint(this.asset) ||
      this.sourceOwner.rebind === undefined
    )
      return false;
    await this.pause();
    // IMPORTANT: rebind changes authorization only. A new element or src here loses both the
    // verified body and the kept frame; changed source timing must reopen instead.
    const retained = await this.sourceOwner.rebind(request);
    return (
      retained === true && !request.signal.aborted && this.closeState === "open"
    );
  }

  async seek(request: MediaTransportSeek): Promise<MediaPresentation> {
    if (this.closeState !== "open" || request.signal.aborted)
      throw new RuntimeFailure("cancelled");
    const timeBase = this.asset.sourceTimeBase;
    if (timeBase === null) throw new RuntimeFailure("contract_mismatch");
    const seconds = (request.sourcePts * timeBase.num) / timeBase.den;
    if (!Number.isFinite(seconds) || seconds < 0)
      throw new RuntimeFailure("contract_mismatch");
    const targetMicroseconds = seconds * NATIVE_CURRENT_TIME_UNITS_PER_SECOND;
    const ceilingMicroseconds = Math.ceil(targetMicroseconds);
    if (!Number.isSafeInteger(ceilingMicroseconds) || ceilingMicroseconds < 0)
      throw new RuntimeFailure("capability_mismatch");
    const nominalSeconds =
      ceilingMicroseconds / NATIVE_CURRENT_TIME_UNITS_PER_SECOND;
    // IMPORTANT: Chromium quantizes currentTime again before decoder presentation. When a
    // binary64 ceil boundary round-trips below its own microsecond cell, use the next whole cell.
    const nativeMicroseconds =
      nominalSeconds * NATIVE_CURRENT_TIME_UNITS_PER_SECOND <
      ceilingMicroseconds
        ? ceilingMicroseconds + 1
        : ceilingMicroseconds;
    if (!Number.isSafeInteger(nativeMicroseconds) || nativeMicroseconds < 0)
      throw new RuntimeFailure("capability_mismatch");
    const nativeSeconds =
      nativeMicroseconds / NATIVE_CURRENT_TIME_UNITS_PER_SECOND;
    const nativeCorrection = nativeSeconds - seconds;
    // IMPORTANT: Chromium floors currentTime to microseconds. An exact PTS boundary can land on
    // the prior frame, so move only the native setter upward within the existing half-tick bound.
    if (
      !Number.isFinite(nativeSeconds) ||
      nativeCorrection < 0 ||
      nativeCorrection > timeBase.num / timeBase.den / 2
    )
      throw new RuntimeFailure("capability_mismatch");

    const cached =
      !this.playing &&
      this.lastRequestedSourcePts === request.sourcePts &&
      this.lastObservation !== undefined &&
      // CRITICAL: a cancelled same-target seek may assign currentTime before its target RVFC,
      // leaving the last playback observation at another PTS. Exact composition paint must wait
      // for fresh target evidence or the stale cache value latches source_unavailable.
      (this.seekObservation !== "exact_source_pts" ||
        this.lastObservation.observedSourcePts === request.sourcePts) &&
      this.currentTimeMatches(seconds);
    if (cached) {
      const presentation = this.presentation(request, this.lastObservation!);
      this.sourceAnchor = {
        epoch: request.epoch,
        sourceFrame: request.sourceFrame,
        sourcePts: request.sourcePts,
      };
      this.emitPresentation(presentation);
      return presentation;
    }

    const wasPlaying = this.playing;
    this.playbackGeneration += 1;
    this.playing = false;
    this.stopFrameObservation();
    if (wasPlaying) {
      try {
        this.sourceOwner.element.pause();
      } catch {
        throw new RuntimeFailure("transport_failure");
      }
    }

    // Browser currentTime is the one named floating-point boundary; all timeline/source mapping
    // stays integer/rational before this assignment and never accumulates browser deltas.
    const pending = this.waitForPresentation(
      request.signal,
      seconds,
      request.sourcePts,
    );
    let observation: NativeFrameObservation;
    try {
      this.sourceOwner.element.currentTime = nativeSeconds;
      observation = await pending;
    } catch (error) {
      throw runtimeFailure(error);
    }
    const presentation = this.presentation(request, observation);
    this.sourceAnchor = {
      epoch: request.epoch,
      sourceFrame: request.sourceFrame,
      sourcePts: request.sourcePts,
    };
    this.lastRequestedSourcePts = request.sourcePts;
    this.lastObservation = observation;
    this.emitPresentation(presentation);
    return presentation;
  }

  async play(signal: AbortSignal): Promise<void> {
    if (this.closeState !== "open" || signal.aborted)
      throw new RuntimeFailure("cancelled");
    if (this.sourceAnchor === undefined)
      throw new RuntimeFailure("contract_mismatch");
    this.stopFrameObservation();
    const generation = ++this.playbackGeneration;
    const observeFailure = (error: unknown) => {
      if (
        signal.aborted ||
        this.closeState !== "open" ||
        generation !== this.playbackGeneration
      )
        return;
      // CRITICAL: classify before RuntimeFailure normalization, but never expose native error
      // text or let an observer change playback cleanup. Late denials belong to obsolete intent.
      try {
        this.onPlaybackFailure?.(
          Object.freeze({
            ownerId: this.ownerId,
            openedEpoch: this.openedEpoch,
            reason:
              error instanceof DOMException && error.name === "NotAllowedError"
                ? "autoplay_blocked"
                : "playback_unavailable",
          }),
        );
      } catch {
        /* Observers cannot replace the transport failure or cleanup. */
      }
    };
    let rawPlay: Promise<void>;
    try {
      rawPlay = Promise.resolve(this.sourceOwner.element.play()).catch(
        (error: unknown) => {
          observeFailure(error);
          throw error;
        },
      );
    } catch (error) {
      observeFailure(error);
      throw new RuntimeFailure("transport_failure");
    }
    try {
      await this.waitForPlay(rawPlay, signal);
    } catch (error) {
      // IMPORTANT: a play promise may fulfill after cancellation. Pause both now and on late
      // fulfillment or a superseded epoch can silently restart native playback.
      this.pauseNativeBestEffort();
      void rawPlay.then(
        () => this.pauseNativeBestEffort(),
        () => undefined,
      );
      throw runtimeFailure(error);
    }
    if (
      signal.aborted ||
      this.closeState !== "open" ||
      generation !== this.playbackGeneration
    ) {
      this.pauseNativeBestEffort();
      throw new RuntimeFailure("cancelled");
    }
    this.playing = true;
    this.schedulePlaybackObservation(generation);
  }

  async pause(): Promise<void> {
    if (this.closeState === "closed") return;
    if (this.closeState !== "open")
      throw new RuntimeFailure("transport_failure");
    this.playbackGeneration += 1;
    this.playing = false;
    this.stopFrameObservation();
    try {
      this.sourceOwner.element.pause();
    } catch {
      throw new RuntimeFailure("transport_failure");
    }
  }

  subscribe(listener: (value: MediaPresentation) => void): () => void {
    if (this.closeState !== "open") return () => undefined;
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  subscribeFailure(listener: (code: "transport_failure") => void): () => void {
    if (this.closeState !== "open") return () => undefined;
    this.failureListeners.add(listener);
    return () => this.failureListeners.delete(listener);
  }

  close(): Promise<void> {
    if (this.closeState === "closed") return Promise.resolve();
    if (this.closePromise !== undefined) return this.closePromise;
    const attempt = this.performClose();
    this.closePromise = attempt;
    void attempt.then(
      () => {
        if (this.closePromise === attempt) this.closePromise = undefined;
      },
      () => {
        if (this.closePromise === attempt) this.closePromise = undefined;
      },
    );
    return attempt;
  }

  private async performClose(): Promise<void> {
    this.closeState = "closing";
    this.playbackGeneration += 1;
    this.playing = false;
    this.stopFrameObservation();
    for (const cancel of [...this.cancelPendingCommands])
      cancel(new RuntimeFailure("cancelled"));
    this.cancelPendingCommands.clear();
    this.detachNativeListeners();
    this.listeners.clear();
    this.failureListeners.clear();
    let cleaned = true;
    try {
      this.sourceOwner.element.pause();
    } catch {
      cleaned = false;
    }
    try {
      // IMPORTANT: clear the source before releasing its private owner; omitting load() can leave
      // the browser decoder holding the prior source after the M25-13 lease has been released.
      this.sourceOwner.element.removeAttribute("src");
      this.sourceOwner.element.load();
    } catch {
      cleaned = false;
    }
    const released = await settleWithDeadline(
      Promise.resolve().then(() => this.sourceOwner.release()),
      RUNTIME_PROFILE.limits.teardownDeadlineMs,
    );
    if (!cleaned || !released) {
      this.closeState = "close_failed";
      throw new RuntimeFailure("transport_failure");
    }
    this.closeState = "closed";
  }

  private waitForPresentation(
    signal: AbortSignal,
    targetSeconds: number,
    targetSourcePts: number,
  ): Promise<NativeFrameObservation> {
    const element = this.sourceOwner.element;
    let settled = false;
    let frameRequest: number | undefined;
    let earlyTargetObservation: NativeFrameObservation | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let resolveWait: ((value: NativeFrameObservation) => void) | undefined;
    let rejectWait: ((error: RuntimeFailure) => void) | undefined;
    let cancel: (failure?: RuntimeFailure) => void = () => undefined;
    const abort = () => cancel();
    const event = () => {
      if (!this.isPostSeekTarget(targetSeconds)) return;
      try {
        if (this.frameObserver === "request_video_frame_callback") {
          if (earlyTargetObservation !== undefined)
            succeed(earlyTargetObservation);
        } else {
          succeed(this.observationFromSeconds(element.currentTime));
        }
      } catch (error) {
        fail(runtimeFailure(error));
      }
    };
    const mediaFailure = () => fail(new RuntimeFailure("transport_failure"));
    const cleanup = () => {
      earlyTargetObservation = undefined;
      signal.removeEventListener("abort", abort);
      element.removeEventListener("seeked", event);
      element.removeEventListener("timeupdate", event);
      element.removeEventListener("error", mediaFailure);
      element.removeEventListener("abort", mediaFailure);
      if (timer !== undefined) clearTimeout(timer);
      if (this.activeFrameCancel === cancel) this.activeFrameCancel = undefined;
    };
    const succeed = (value: NativeFrameObservation) => {
      if (settled) return;
      settled = true;
      if (frameRequest !== undefined) {
        element.cancelVideoFrameCallback?.(frameRequest);
        frameRequest = undefined;
      }
      cleanup();
      resolveWait?.(value);
    };
    const fail = (failure: RuntimeFailure) => {
      if (settled) return;
      settled = true;
      if (
        frameRequest !== undefined &&
        typeof element.cancelVideoFrameCallback === "function"
      )
        element.cancelVideoFrameCallback(frameRequest);
      cleanup();
      rejectWait?.(failure);
    };
    cancel = (failure = new RuntimeFailure("cancelled")) => fail(failure);
    const promise = new Promise<NativeFrameObservation>((resolve, reject) => {
      resolveWait = resolve;
      rejectWait = reject;
    });
    try {
      this.activeFrameCancel = cancel;
      signal.addEventListener("abort", abort, { once: true });
      element.addEventListener("error", mediaFailure, { once: true });
      element.addEventListener("abort", mediaFailure, { once: true });
      timer = setTimeout(
        () => fail(new RuntimeFailure("transport_failure")),
        NATIVE_MEDIA_OPERATION_DEADLINE_MS,
      );
      if (this.frameObserver === "request_video_frame_callback") {
        if (
          typeof element.requestVideoFrameCallback !== "function" ||
          typeof element.cancelVideoFrameCallback !== "function"
        )
          throw new RuntimeFailure("capability_mismatch");
        const requestFrame = () => {
          frameRequest = element.requestVideoFrameCallback!(
            (_now, metadata) => {
              frameRequest = undefined;
              try {
                // IMPORTANT: target RVFC can precede seeked with seeking=true and never repeat.
                // Chromium can also deliver the old compositor frame after seeked/currentTime
                // reaches the target; exact composition seeks below reject that stale metadata.
                const timeBase = this.asset.sourceTimeBase;
                const observation = this.observationFromSeconds(
                  metadata.mediaTime,
                );
                const matchesTarget =
                  timeBase !== null &&
                  Math.abs(metadata.mediaTime - targetSeconds) <=
                    timeBase.num / timeBase.den / 2 &&
                  // CRITICAL: the inclusive half-tick window can quantize to the adjacent PTS.
                  // Exact composition paint requires the requested integer PTS, or a rapid seek
                  // can accept the neighbour and latch the healthy session as source_unavailable.
                  (this.seekObservation !== "exact_source_pts" ||
                    observation.observedSourcePts === targetSourcePts);
                if (!this.isPostSeekTarget(targetSeconds)) {
                  // Keep an exact early observation across a later stale callback; clearing it
                  // can force a false timeout when the target frame is never submitted again.
                  if (
                    matchesTarget &&
                    this.currentTimeMatches(targetSeconds) &&
                    this.sourceOwner.element.seeking === true
                  )
                    earlyTargetObservation = observation;
                  requestFrame();
                  return;
                }
                // IMPORTANT: generic VFR seeks publish the browser's measured PTS for interval
                // validation. Exact composition paint opts in because a neighbor is a stale layer.
                if (
                  this.seekObservation === "exact_source_pts" &&
                  !matchesTarget
                ) {
                  requestFrame();
                  return;
                }
                succeed(observation);
              } catch (error) {
                fail(runtimeFailure(error));
              }
            },
          );
        };
        requestFrame();
      }
      element.addEventListener("seeked", event);
      element.addEventListener("timeupdate", event);
    } catch (error) {
      fail(runtimeFailure(error));
    }
    return promise;
  }

  private waitForPlay(
    rawPlay: Promise<void>,
    signal: AbortSignal,
  ): Promise<void> {
    const element = this.sourceOwner.element;
    let settled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let resolveWait: (() => void) | undefined;
    let rejectWait: ((error: RuntimeFailure) => void) | undefined;
    let cancelCommand = (_failure: RuntimeFailure) => undefined;
    const mediaFailure = () =>
      cancelCommand(new RuntimeFailure("transport_failure"));
    const cleanup = () => {
      signal.removeEventListener("abort", abort);
      element.removeEventListener("error", mediaFailure);
      element.removeEventListener("abort", mediaFailure);
      this.cancelPendingCommands.delete(cancelCommand);
      if (timer !== undefined) clearTimeout(timer);
    };
    const succeed = () => {
      if (settled) return;
      settled = true;
      cleanup();
      resolveWait?.();
    };
    cancelCommand = (failure: RuntimeFailure) => {
      if (settled) return;
      settled = true;
      cleanup();
      rejectWait?.(failure);
    };
    const abort = () => cancelCommand(new RuntimeFailure("cancelled"));
    const pending = new Promise<void>((resolve, reject) => {
      resolveWait = resolve;
      rejectWait = reject;
    });
    this.cancelPendingCommands.add(cancelCommand);
    signal.addEventListener("abort", abort, { once: true });
    element.addEventListener("error", mediaFailure, { once: true });
    element.addEventListener("abort", mediaFailure, { once: true });
    timer = setTimeout(
      () => cancelCommand(new RuntimeFailure("transport_failure")),
      NATIVE_MEDIA_OPERATION_DEADLINE_MS,
    );
    void rawPlay.then(succeed, () =>
      cancelCommand(new RuntimeFailure("transport_failure")),
    );
    return pending;
  }

  private schedulePlaybackObservation(generation: number): void {
    if (
      !this.playing ||
      this.closeState !== "open" ||
      generation !== this.playbackGeneration ||
      this.sourceAnchor === undefined
    )
      return;
    const element = this.sourceOwner.element;
    if (this.frameObserver === "event_fallback") {
      const timeupdate = () => {
        try {
          this.emitObservedPlayback(
            this.observationFromSeconds(element.currentTime),
          );
        } catch {
          this.onMediaFailure();
        }
      };
      const cancel = () => {
        element.removeEventListener("timeupdate", timeupdate);
        if (this.activeFrameCancel === cancel)
          this.activeFrameCancel = undefined;
      };
      this.activeFrameCancel = cancel;
      element.addEventListener("timeupdate", timeupdate);
      return;
    }

    let frameRequest: number | undefined;
    const cancel = () => {
      if (
        frameRequest !== undefined &&
        typeof element.cancelVideoFrameCallback === "function"
      )
        element.cancelVideoFrameCallback(frameRequest);
      if (this.activeFrameCancel === cancel) this.activeFrameCancel = undefined;
    };
    this.activeFrameCancel = cancel;
    try {
      if (typeof element.requestVideoFrameCallback !== "function")
        throw new RuntimeFailure("capability_mismatch");
      frameRequest = element.requestVideoFrameCallback((_now, metadata) => {
        if (this.activeFrameCancel === cancel)
          this.activeFrameCancel = undefined;
        if (
          !this.playing ||
          this.closeState !== "open" ||
          generation !== this.playbackGeneration
        )
          return;
        try {
          this.emitObservedPlayback(
            this.observationFromSeconds(metadata.mediaTime),
          );
          this.schedulePlaybackObservation(generation);
        } catch {
          this.onMediaFailure();
        }
      });
    } catch {
      cancel();
      this.onMediaFailure();
    }
  }

  private emitObservedPlayback(observation: NativeFrameObservation): void {
    const anchor = this.sourceAnchor;
    if (anchor === undefined) return;
    this.lastObservation = observation;
    this.emitPresentation(
      deepFreeze({
        ownerId: this.ownerId,
        assetId: this.assetId,
        epoch: anchor.epoch,
        sourceFrame: anchor.sourceFrame,
        sourcePts: anchor.sourcePts,
        ...observation,
      }),
    );
  }

  private presentation(
    request: MediaTransportSeek,
    observation: NativeFrameObservation,
  ): MediaPresentation {
    return deepFreeze({
      ownerId: this.ownerId,
      assetId: this.assetId,
      epoch: request.epoch,
      sourceFrame: request.sourceFrame,
      sourcePts: request.sourcePts,
      ...observation,
    });
  }

  private emitPresentation(presentation: MediaPresentation): void {
    for (const listener of [...this.listeners]) {
      try {
        listener(presentation);
      } catch {
        // Presentation observers cannot take ownership of the native transport.
      }
    }
  }

  private observationFromSeconds(seconds: number): NativeFrameObservation {
    const timeBase = this.asset.sourceTimeBase;
    if (timeBase === null || !Number.isFinite(seconds) || seconds < 0)
      throw new RuntimeFailure("contract_mismatch");
    const duration = this.sourceOwner.element.duration;
    const tolerance = timeBase.num / timeBase.den / 2;
    if (
      Number.isFinite(duration) &&
      (duration < 0 || seconds > duration + tolerance)
    )
      throw new RuntimeFailure("contract_mismatch");
    const observedSourcePts = Math.round(
      (seconds * timeBase.den) / timeBase.num,
    );
    if (!Number.isSafeInteger(observedSourcePts) || observedSourcePts < 0)
      throw new RuntimeFailure("contract_mismatch");
    return deepFreeze({ observedSourcePts, observation: this.frameObserver });
  }

  private currentTimeMatches(seconds: number): boolean {
    const timeBase = this.asset.sourceTimeBase;
    if (timeBase === null) return false;
    const tolerance = timeBase.num / timeBase.den / 2;
    return (
      Math.abs(this.sourceOwner.element.currentTime - seconds) <= tolerance
    );
  }

  private isPostSeekTarget(seconds: number): boolean {
    return (
      this.sourceOwner.element.seeking !== true &&
      this.currentTimeMatches(seconds)
    );
  }

  private stopFrameObservation(failure?: RuntimeFailure): void {
    const cancel = this.activeFrameCancel;
    this.activeFrameCancel = undefined;
    cancel?.(failure);
  }

  private reportFailure(): void {
    if (this.failureReported) return;
    this.failureReported = true;
    for (const listener of [...this.failureListeners]) {
      try {
        listener("transport_failure");
      } catch {
        // Failure observers cannot take ownership of the native transport.
      }
    }
  }

  private pauseNativeBestEffort(): void {
    try {
      this.sourceOwner.element.pause();
    } catch {
      // The initiating operation already reports the native transport failure.
    }
  }

  private attachNativeListeners(): void {
    const element = this.sourceOwner.element;
    element.addEventListener("error", this.onMediaFailure);
    element.addEventListener("abort", this.onMediaFailure);
    element.addEventListener("ended", this.onMediaEnded);
    this.nativeListenersAttached = true;
  }

  private detachNativeListeners(): void {
    if (!this.nativeListenersAttached) return;
    const element = this.sourceOwner.element;
    element.removeEventListener("error", this.onMediaFailure);
    element.removeEventListener("abort", this.onMediaFailure);
    element.removeEventListener("ended", this.onMediaEnded);
    this.nativeListenersAttached = false;
  }
}

export function createHtmlMediaElementTransportFactory(
  acquire: HtmlMediaElementSourceOwnerFactory,
  options: HtmlMediaElementTransportOptions = {},
): MediaTransportFactory {
  const frameObserver = options.frameObserver ?? "request_video_frame_callback";
  const seekObservation = options.seekObservation ?? "observed_interval";
  if (
    frameObserver !== "request_video_frame_callback" &&
    frameObserver !== "event_fallback"
  )
    throw new RuntimeFailure("contract_mismatch");
  if (
    seekObservation !== "observed_interval" &&
    seekObservation !== "exact_source_pts"
  )
    throw new RuntimeFailure("contract_mismatch");
  // IMPORTANT: one live runtime owner must map to one native element; sharing a decoder lets
  // another layer overwrite currentTime and makes source-frame receipts nondeterministic.
  const activeElements = new WeakSet<HTMLVideoElement>();
  return async (request) => {
    if (request.signal.aborted) throw new RuntimeFailure("cancelled");
    let acquired: unknown;
    try {
      acquired = await acquire(request);
    } catch {
      throw new RuntimeFailure("transport_failure");
    }
    const releaseAcquired = sourceOwnerRelease(acquired);
    if (!isHtmlMediaElementSourceOwner(acquired)) {
      const released =
        releaseAcquired !== undefined &&
        (await settleWithDeadline(
          releaseAcquired(),
          RUNTIME_PROFILE.limits.teardownDeadlineMs,
        ));
      if (releaseAcquired !== undefined && !released)
        throw new RuntimeFailure("transport_failure");
      if (request.signal.aborted) throw new RuntimeFailure("cancelled");
      throw new RuntimeFailure("contract_mismatch");
    }
    if (activeElements.has(acquired.element)) {
      // CRITICAL: an erroneous second acquisition may return a decoder that is still owned by
      // the first transport. Release only the second lease; clearing src would corrupt owner one.
      const released = await settleWithDeadline(
        acquired.release(),
        RUNTIME_PROFILE.limits.teardownDeadlineMs,
      );
      if (!released) throw new RuntimeFailure("transport_failure");
      throw new RuntimeFailure(
        request.signal.aborted ? "cancelled" : "contract_mismatch",
      );
    }
    if (request.signal.aborted) {
      const cleaned = cleanNativeElement(acquired.element);
      const released = await settleWithDeadline(
        acquired.release(),
        RUNTIME_PROFILE.limits.teardownDeadlineMs,
      );
      if (!cleaned || !released) throw new RuntimeFailure("transport_failure");
      throw new RuntimeFailure("cancelled");
    }
    if (
      frameObserver === "request_video_frame_callback" &&
      (typeof acquired.element.requestVideoFrameCallback !== "function" ||
        typeof acquired.element.cancelVideoFrameCallback !== "function")
    ) {
      const cleaned = cleanNativeElement(acquired.element);
      const released = await settleWithDeadline(
        acquired.release(),
        RUNTIME_PROFILE.limits.teardownDeadlineMs,
      );
      if (!cleaned || !released) throw new RuntimeFailure("transport_failure");
      throw new RuntimeFailure("capability_mismatch");
    }
    activeElements.add(acquired.element);
    let released = false;
    let releasePromise: Promise<void> | undefined;
    const sourceOwner: HtmlMediaElementSourceOwner = {
      element: acquired.element,
      ...(acquired.rebind === undefined
        ? {}
        : { rebind: (next: MediaTransportOpen) => acquired.rebind!(next) }),
      release() {
        if (released) return Promise.resolve();
        if (releasePromise !== undefined) return releasePromise;
        // IMPORTANT: keep the native element reserved until release fulfills. Clearing the guard
        // before a rejected or timed-out release permits two transports to own one decoder.
        releasePromise = Promise.resolve()
          .then(() => acquired.release())
          .then(
            () => {
              released = true;
              activeElements.delete(acquired.element);
            },
            (error: unknown) => {
              releasePromise = undefined;
              throw error;
            },
          );
        return releasePromise;
      },
    };
    try {
      return new HtmlMediaElementTransport(
        request.asset,
        sourceOwner,
        request,
        frameObserver,
        seekObservation,
        options.onPlaybackFailure,
      );
    } catch {
      const cleaned = cleanNativeElement(acquired.element);
      const releasedAfterFailure = await settleWithDeadline(
        sourceOwner.release(),
        RUNTIME_PROFILE.limits.teardownDeadlineMs,
      );
      if (!cleaned || !releasedAfterFailure)
        throw new RuntimeFailure("transport_failure");
      throw new RuntimeFailure("transport_failure");
    }
  };
}

function cleanNativeElement(element: HTMLVideoElement): boolean {
  let clean = true;
  try {
    element.pause();
  } catch {
    clean = false;
  }
  try {
    element.removeAttribute("src");
    element.load();
  } catch {
    clean = false;
  }
  return clean;
}

function outputDurationFrames(snapshot: PublicCompositionSnapshot): number {
  const value = snapshot.output.durationFrames;
  if (!Number.isSafeInteger(value) || (value as number) <= 0)
    throw new RuntimeFailure("contract_mismatch");
  return value as number;
}

function runtimeFailure(error: unknown): RuntimeFailure {
  return error instanceof RuntimeFailure
    ? error
    : new RuntimeFailure("transport_failure");
}

function isMediaTransport(value: unknown): value is MediaTransport {
  if (value === null || typeof value !== "object") return false;
  const candidate = value as Partial<MediaTransport>;
  return (
    typeof candidate.ownerId === "string" &&
    typeof candidate.assetId === "string" &&
    Number.isSafeInteger(candidate.openedEpoch) &&
    Number.isSafeInteger(candidate.pendingFrameObservations) &&
    (candidate.pendingFrameObservations as number) >= 0 &&
    (candidate.pendingFrameObservations as number) <= 1 &&
    typeof candidate.seek === "function" &&
    typeof candidate.play === "function" &&
    typeof candidate.pause === "function" &&
    typeof candidate.subscribe === "function" &&
    (candidate.subscribeFailure === undefined ||
      typeof candidate.subscribeFailure === "function") &&
    (candidate.rebind === undefined ||
      typeof candidate.rebind === "function") &&
    typeof candidate.close === "function"
  );
}

function mediaTransportClose(
  value: unknown,
): (() => Promise<void>) | undefined {
  if (value === null || typeof value !== "object") return undefined;
  const close = (value as { close?: unknown }).close;
  if (typeof close !== "function") return undefined;
  return async () => {
    await Reflect.apply(close, value, []);
  };
}

const capabilityDispositionKeys = Object.freeze([
  "status",
  "blocker",
  "profileFingerprint",
  "engineProfileId",
  "fallback",
  "frameObserver",
  "qualified",
  "limits",
]);

const capabilityLimitKeys = Object.freeze([
  "activeVideoOwners",
  "warmVideoOwners",
  "canvasOwners",
  "pendingRvfcOwners",
  "pendingOperations",
  "cancelDeadlineMs",
  "teardownDeadlineMs",
  "jsHeapDeltaBytes",
] as const);

function capabilityActivationBlocker(
  value: unknown,
): RuntimeBlockerCode | null {
  if (value === null || typeof value !== "object") return "capability_mismatch";
  const candidate = value as Record<string, unknown>;
  if (
    candidate.profileFingerprint !== RUNTIME_PROFILE_FINGERPRINT ||
    candidate.engineProfileId !== ENGINE_PROFILE_ID
  )
    return "profile_unavailable";
  if (candidate.status !== "available") {
    if (
      (candidate.status === "qualification_required" ||
        candidate.status === "profile_unavailable" ||
        candidate.status === "capability_mismatch") &&
      candidate.blocker === candidate.status
    )
      return candidate.status;
    return "capability_mismatch";
  }
  // IMPORTANT: a copied or hand-built available shape has no trusted admission. Keep
  // the evaluator's private identity check or caller JSON can bypass qualification.
  if (!isEvaluatedAvailableDisposition(value)) return "qualification_required";
  if (
    !hasExactObjectKeys(candidate, capabilityDispositionKeys) ||
    candidate.blocker !== null ||
    candidate.fallback !== "selected_source_only" ||
    (candidate.frameObserver !== "request_video_frame_callback" &&
      candidate.frameObserver !== "event_fallback") ||
    candidate.qualified !== true ||
    !hasExactObjectKeys(candidate.limits, capabilityLimitKeys)
  )
    return "capability_mismatch";
  const limits = candidate.limits as Record<string, unknown>;
  for (const key of capabilityLimitKeys)
    if (limits[key] !== RUNTIME_PROFILE.limits[key])
      return "capability_mismatch";
  return null;
}

function hasExactObjectKeys(
  value: unknown,
  expected: readonly string[],
): value is Record<string, unknown> {
  return (
    value !== null &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    JSON.stringify(Object.keys(value).sort()) ===
      JSON.stringify([...expected].sort())
  );
}

function isHtmlMediaElementSourceOwner(
  value: unknown,
): value is HtmlMediaElementSourceOwner {
  if (value === null || typeof value !== "object") return false;
  const candidate = value as Partial<HtmlMediaElementSourceOwner>;
  const element = candidate.element as Partial<HTMLVideoElement> | undefined;
  return (
    element !== undefined &&
    typeof element.play === "function" &&
    typeof element.pause === "function" &&
    typeof element.addEventListener === "function" &&
    typeof element.removeEventListener === "function" &&
    typeof element.removeAttribute === "function" &&
    typeof element.load === "function" &&
    (candidate.rebind === undefined ||
      typeof candidate.rebind === "function") &&
    typeof candidate.release === "function"
  );
}

function sourceOwnerRelease(value: unknown): (() => Promise<void>) | undefined {
  if (value === null || typeof value !== "object") return undefined;
  const release = (value as { release?: unknown }).release;
  if (typeof release !== "function") return undefined;
  return async () => {
    await Reflect.apply(release, value, []);
  };
}

async function settleWithDeadline(
  operation: Promise<void>,
  deadlineMs: number,
): Promise<boolean> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      operation.then(
        () => true,
        () => false,
      ),
      new Promise<boolean>((resolve) => {
        timer = setTimeout(() => resolve(false), deadlineMs);
      }),
    ]);
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}

function freezeSnapshot(value: RuntimeSnapshot): RuntimeSnapshot {
  return deepFreeze(value);
}

function deepFreeze<T>(value: T): T {
  if (value !== null && typeof value === "object" && !Object.isFrozen(value)) {
    for (const child of Object.values(value as Record<string, unknown>))
      deepFreeze(child);
    Object.freeze(value);
  }
  return value;
}
