import { sha256Text } from "../contracts/canonicalFingerprint";
import {
  EMBEDDED_AUDIO_FOLLOWER_SCHEMA,
  EMBEDDED_AUDIO_POLICY,
  decodeEmbeddedAudioFollowerStatus,
  type EmbeddedAudioFollowerStatus,
} from "../contracts/embeddedAudioFollowerCodec";
import {
  decodeResolvedScene,
  type CompositionClip,
  type ResolvedCompositionScene,
} from "../contracts/compositionCodec";
import {
  createAuthoringLeasedEditorRuntime,
  type AuthoringLeasedEditorRuntimeOptions,
} from "./authoringLeasedEditorRuntime";
import { scheduleClipAudioGain } from "./clipAudioEnvelope";
import {
  createEditorRuntime,
  RUNTIME_RECEIPT_SCHEMA,
  type EditorRuntime,
  type HtmlMediaElementSourceOwner,
  type MediaTransport,
  type RuntimeReceipt,
} from "./editorRuntime";
import { validatePublicAssetManifest } from "./publicAssetManifest";
import { resolveCompositionScene } from "./sceneResolver";
import type { NlePlaybackLeaseScheduler } from "../host/nleLeaseScheduler";

type Reason = EmbeddedAudioFollowerStatus["reason"];
const MAX_DECODED_AUDIO_BYTES = 64 * 1024 * 1024;
const MAX_ADMITTED_AUDIO_CHANNELS = 8;
const AUDIO_PREVIEW_HEADER_BYTES = 44;

function canonicalAudioPreview(
  body: ArrayBuffer,
  expectedSamples: number,
): Readonly<{ channels: number; decodedBytes: number }> {
  if (body.byteLength < AUDIO_PREVIEW_HEADER_BYTES)
    throw new Error("invalid_audio_preview");
  const bytes = new Uint8Array(body);
  const ascii = (offset: number, value: string) =>
    [...value].every(
      (character, index) => bytes[offset + index] === character.charCodeAt(0),
    );
  const view = new DataView(body);
  const channels = view.getUint16(22, true);
  const dataBytes = view.getUint32(40, true);
  const blockAlign = channels * 2;
  if (
    !ascii(0, "RIFF") ||
    view.getUint32(4, true) !== body.byteLength - 8 ||
    !ascii(8, "WAVE") ||
    !ascii(12, "fmt ") ||
    view.getUint32(16, true) !== 16 ||
    view.getUint16(20, true) !== 1 ||
    channels < 1 ||
    channels > MAX_ADMITTED_AUDIO_CHANNELS ||
    view.getUint32(24, true) !== 48_000 ||
    view.getUint32(28, true) !== 48_000 * blockAlign ||
    view.getUint16(32, true) !== blockAlign ||
    view.getUint16(34, true) !== 16 ||
    !ascii(36, "data") ||
    dataBytes !== body.byteLength - AUDIO_PREVIEW_HEADER_BYTES ||
    dataBytes !== expectedSamples * blockAlign
  )
    throw new Error("invalid_audio_preview");
  const decodedAudioBytes = expectedSamples * channels * 4;
  if (
    !Number.isSafeInteger(decodedAudioBytes) ||
    decodedAudioBytes < 1 ||
    decodedAudioBytes > MAX_DECODED_AUDIO_BYTES
  )
    throw new Error("audio_resource_limit");
  return Object.freeze({ channels, decodedBytes: decodedAudioBytes });
}
type Source = {
  element: HTMLVideoElement;
  epoch: number | null;
  audioBody?: Blob;
  decoded?: Promise<AudioBuffer>;
  decodedBytes: number;
};
type Selection = Readonly<{
  epoch: number;
  signal: AbortSignal;
  scene: ResolvedCompositionScene;
  fingerprint: string;
  ownerId: string | null;
  // The owner's clip as the validated snapshot gives it: its audio member and its extent.
  ownerClip: CompositionClip | null;
  assetId: string | null;
  sourceStartSample: number | null;
  sourceEndSample: number | null;
  silentReason: "no_primary" | "no_embedded_audio";
}>;
type ScheduledAudioOwner = {
  node: AudioBufferSourceNode;
  // The source plays through its own gain, which carries its clip's envelope.
  gain: GainNode;
  ownerId: string;
  epoch: number;
  transitionFrame: number | null;
  decodedBytes: number;
  endsAtContextTime: number;
};

export type EmbeddedAudioFollower = Readonly<{
  runtime: EditorRuntime;
  status(): EmbeddedAudioFollowerStatus;
  subscribe(listener: () => void): () => void;
  suspend(): Promise<RuntimeReceipt>;
  resume(): Promise<RuntimeReceipt>;
}>;

export type EmbeddedAudioFollowerOptions = AuthoringLeasedEditorRuntimeOptions &
  Readonly<{
    createAudioContext?: () => AudioContext;
    playbackScheduler?: NlePlaybackLeaseScheduler;
  }>;

/** Follow the already-resolved primary VIDEO through its existing native transport. */
export function createEmbeddedAudioFollower(
  options: EmbeddedAudioFollowerOptions,
): EmbeddedAudioFollower {
  let currentSnapshot = options.snapshot;
  let currentManifest = options.manifest;
  const sources = new Map<string, Source>();
  const listeners = new Set<() => void>();
  let selection: Selection | undefined;
  // IMPORTANT: a follower can be created after the document was hidden. Waiting only for
  // visibilitychange misses that initial state and permits background audio on first play.
  let suspended = document.hidden;
  let failure: Reason | undefined;
  let intent = 0;
  let detached = false;
  let playRequested = false;
  let audioContext: AudioContext | undefined;
  let audible: ScheduledAudioOwner | undefined;
  let successor: typeof audible;
  let prepared:
    | Readonly<{
        ownerId: string;
        assetId: string;
        transitionFrame: number;
        selection: Selection;
        buffer: AudioBuffer;
        decodedBytes: number;
      }>
    | undefined;
  let handoffFrame: number | null = null;
  let decodedBytes = 0;
  // A preparation for the next ownership change that has not settled yet: the incoming owner's
  // transport, then (`aheadAudio`) the audio of the clip after it. See `prepare`.
  let preparation: Promise<void> | undefined;
  let aheadAudio: Promise<void> | undefined;
  // An acquisition made ahead of a cut outlives the epoch that asked for it, by design.
  const aheadAbort = new AbortController();
  let base: EditorRuntime;
  let value = decodeEmbeddedAudioFollowerStatus({
    schema: EMBEDDED_AUDIO_FOLLOWER_SCHEMA,
    policy_id: EMBEDDED_AUDIO_POLICY,
    transport_epoch: 0,
    scene_fingerprint: null,
    owner_clip_id: null,
    state: "silent",
    reason: "closed",
    preview_capability:
      options.capability.status === "available" ? "available" : "unavailable",
    final_render_capability: "not_evaluated",
  });

  function muteAll() {
    for (const source of sources.values()) source.element.muted = true;
  }
  function stopAudible() {
    const current = audible;
    audible = undefined;
    const next = successor;
    successor = undefined;
    handoffFrame = null;
    for (const owner of [current, next]) {
      if (owner === undefined) continue;
      owner.node.onended = null;
      try {
        owner.node.stop();
      } catch {
        // A node that already ended is still detached below.
      }
      owner.node.disconnect();
      owner.gain.disconnect();
      decodedBytes -= owner.decodedBytes;
    }
    if (prepared !== undefined) {
      decodedBytes -= prepared.decodedBytes;
      prepared = undefined;
    }
  }
  function publish(
    state: EmbeddedAudioFollowerStatus["state"],
    reason: Reason,
  ) {
    const next = decodeEmbeddedAudioFollowerStatus({
      ...value,
      transport_epoch: base?.snapshot().epoch ?? value.transport_epoch,
      scene_fingerprint:
        reason === "closed" ? null : (selection?.fingerprint ?? null),
      owner_clip_id: state === "silent" ? null : (selection?.ownerId ?? null),
      state,
      reason,
      preview_capability:
        state === "unavailable"
          ? "blocked"
          : options.capability.status === "available"
            ? "available"
            : "unavailable",
    });
    if (JSON.stringify(value) === JSON.stringify(next)) return;
    value = next;
    for (const listener of listeners) {
      try {
        listener();
      } catch {
        /* Observers do not own playback or cleanup. */
      }
    }
  }
  function stop(reason: Reason) {
    muteAll();
    stopAudible();
    failure = reason;
    publish("unavailable", reason);
  }
  function current(pin: Selection | undefined): pin is Selection {
    return (
      pin !== undefined &&
      selection === pin &&
      !pin.signal.aborted &&
      pin.epoch === base.snapshot().epoch &&
      !suspended &&
      !failure &&
      !detached
    );
  }
  function onRuntime() {
    const state = base.snapshot();
    if (state.status === "closed") {
      muteAll();
      if (state.blocker || sources.size)
        publish("unavailable", "cleanup_pending");
      else {
        selection = undefined;
        publish("silent", "closed");
      }
    } else if (suspended) {
      muteAll();
      publish("suspended", "suspended");
    } else if (failure === "boundary_reached") {
      muteAll();
      publish("suspended", "boundary_reached");
    } else if (failure || state.blocker || state.status === "blocked") {
      muteAll();
      publish("unavailable", failure ?? "source_unavailable");
    } else if (state.status === "opening" || state.status === "seeking") {
      if (!playRequested) muteAll();
      publish(
        "seeking",
        state.status === "opening" ? "opening" : "seek_in_progress",
      );
    } else if (state.status === "paused") {
      // Native play remains pending while the accepted runtime still reports paused.
      // Muting that intermediate notification would silence an otherwise successful play.
      if (playRequested) return;
      muteAll();
      if (selection?.ownerId) publish("suspended", "paused");
      else publish("silent", selection?.silentReason ?? "no_primary");
    } else if (state.status === "playing" && current(selection)) {
      if (
        selection.ownerId &&
        audible?.ownerId === selection.ownerId &&
        audible.epoch === selection.epoch
      )
        publish("following", "none");
      else if (!selection.ownerId) publish("silent", selection.silentReason);
      else publish("suspended", "paused");
    }
  }
  function select(
    scene: ResolvedCompositionScene,
    epoch: number,
    signal: AbortSignal,
  ): Selection {
    const snapshot = currentSnapshot;
    if (
      scene.publicFingerprint !== snapshot.publicFingerprint ||
      scene.blockers.length
    )
      throw new Error("invalid_scene");
    const primary = snapshot.tracks.find(
      (track) => track.kind === "primary_video" && track.enabled,
    );
    const primaryLayers = scene.layers.filter(
      (layer) => layer.trackId === primary?.trackId,
    );
    const span = scene.audioSpan;
    let ownerId: string | null = null;
    let ownerClip: CompositionClip | null = null;
    let assetId: string | null = null;
    let sourceStartSample: number | null = null;
    let sourceEndSample: number | null = null;
    if (span) {
      const clip = snapshot.clips.find((row) => row.clipId === span.clipId);
      const asset = snapshot.assets.find((row) => row.assetId === span.assetId);
      const layer = primaryLayers.find((row) => row.clipId === span.clipId);
      if (
        !primary ||
        !clip?.enabled ||
        !asset ||
        asset.kind !== "video" ||
        asset.embeddedAudio !== "present_bound" ||
        asset.sourceSampleCount === null ||
        clip.trackId !== primary.trackId ||
        clip.assetId !== asset.assetId ||
        !layer ||
        layer.assetId !== asset.assetId ||
        scene.frame < clip.startFrame ||
        scene.frame >= clip.startFrame + clip.durationFrames ||
        span.outputStartSample !== scene.frame * 2000 ||
        span.outputEndSample !== (scene.frame + 1) * 2000 ||
        Number(span.sourceEndSample) > asset.sourceSampleCount
      )
        throw new Error("invalid_scene");
      // IMPORTANT: validate the producer-selected owner; never substitute a browser-selected
      // successor. A forged span for the outgoing overlap owner must fail before any unmute.
      if (
        snapshot.clips.some(
          (row) =>
            row.enabled &&
            row.trackId === primary.trackId &&
            row.startFrame <= scene.frame &&
            scene.frame < row.startFrame + row.durationFrames &&
            (row.startFrame > clip.startFrame ||
              (row.startFrame === clip.startFrame && row.clipId > clip.clipId)),
        )
      )
        throw new Error("invalid_scene");
      const boundaries = snapshot.clips
        .filter(
          (row) =>
            row.enabled &&
            row.trackId === primary.trackId &&
            row.startFrame > scene.frame,
        )
        .map((row) => row.startFrame);
      const endFrame = Math.min(
        clip.startFrame + clip.durationFrames,
        Number(snapshot.output.durationFrames),
        ...boundaries,
      );
      // This is a conservative expiry fence, not a resolver or clock. Only a fresh canonical
      // scene may grant the next owner; native media must not play beyond the admitted interval.
      sourceEndSample = Math.min(
        asset.sourceSampleCount,
        Number(span.sourceStartSample) + (endFrame - scene.frame) * 2000,
      );
      sourceStartSample = Number(span.sourceStartSample);
      ownerId = clip.clipId;
      ownerClip = clip;
      assetId = asset.assetId;
    } else {
      for (const layer of primaryLayers) {
        const clip = snapshot.clips.find((row) => row.clipId === layer.clipId);
        const superseded =
          clip &&
          snapshot.clips.some(
            (row) =>
              row.enabled &&
              row.trackId === primary?.trackId &&
              row.startFrame <= scene.frame &&
              scene.frame < row.startFrame + row.durationFrames &&
              (row.startFrame > clip.startFrame ||
                (row.startFrame === clip.startFrame &&
                  row.clipId > clip.clipId)),
          );
        if (
          !superseded &&
          snapshot.assets.some(
            (asset) =>
              asset.assetId === layer.assetId &&
              // A track exclusion is policy, not proof the source lacks audio. Only
              // genuinely absent audio may become successful silence on the primary track.
              asset.embeddedAudio !== "absent",
          )
        )
          throw new Error("invalid_scene");
      }
    }
    return Object.freeze({
      epoch,
      signal,
      scene,
      ownerId,
      ownerClip,
      assetId,
      sourceStartSample,
      sourceEndSample,
      fingerprint: sha256Text(
        `${scene.profileId}:${scene.publicFingerprint}:${scene.frame}`,
      ),
      silentReason: primaryLayers.length ? "no_embedded_audio" : "no_primary",
    });
  }
  async function decodePreview(
    body: Blob,
    expectedSamples: number,
  ): Promise<Readonly<{ buffer: AudioBuffer; decodedBytes: number }>> {
    if (audioContext === undefined)
      audioContext =
        options.createAudioContext?.() ??
        new AudioContext({ sampleRate: 48_000 });
    const context = audioContext;
    const wire = await body.arrayBuffer();
    const format = canonicalAudioPreview(wire, expectedSamples);
    if (decodedBytes + format.decodedBytes > MAX_DECODED_AUDIO_BYTES)
      throw new Error("audio_resource_limit");
    const buffer = await context.decodeAudioData(wire.slice(0));
    const bytes = buffer.length * buffer.numberOfChannels * 4;
    if (
      buffer.sampleRate !== 48_000 ||
      buffer.numberOfChannels !== format.channels ||
      buffer.length !== expectedSamples ||
      bytes !== format.decodedBytes ||
      !Number.isSafeInteger(bytes) ||
      bytes < 1 ||
      decodedBytes + bytes > MAX_DECODED_AUDIO_BYTES
    )
      throw new Error("audio_resource_limit");
    decodedBytes += bytes;
    return Object.freeze({ buffer, decodedBytes: bytes });
  }
  async function prepareAdjacent(pin: Selection, token: number) {
    if (
      options.leaseClient.acquireAudioPreview === undefined ||
      pin.ownerId === null ||
      pin.sourceStartSample === null ||
      pin.sourceEndSample === null
    )
      return;
    const remaining = pin.sourceEndSample - pin.sourceStartSample;
    if (remaining <= 0 || remaining % 2_000 !== 0) return;
    const transitionFrame = pin.scene.frame + remaining / 2_000;
    if (transitionFrame >= Number(currentSnapshot.output.durationFrames))
      return;
    // IMPORTANT: at a cut this runs inside the play that starts the incoming picture, while the
    // incoming audio is already audible. A lease round trip here is the picture trailing the
    // sound by that much, so a successor fetched ahead (`prepareAhead`) is taken as it is. And
    // when one must still be fetched, a preparation in flight is awaited first: the lease route
    // admits one acquisition at a time and this one has no retry behind it.
    const held = () =>
      prepared !== undefined &&
      prepared.transitionFrame === transitionFrame &&
      prepared.ownerId !== pin.ownerId;
    if (held()) return;
    const pending = preparation;
    if (pending !== undefined) {
      await pending;
      if (!current(pin) || token !== intent || held()) return;
    }
    // CRITICAL: the presenting resolver disposes current visual owners and installs its scene.
    // Future audio lookup must stay pure or playback rebuilds the current font and drops frames.
    const wire = resolveCompositionScene(currentSnapshot, transitionFrame);
    if (!current(pin) || token !== intent) return;
    const scene = decodeResolvedScene(wire);
    if (scene.frame !== transitionFrame) throw new Error("invalid_scene");
    await acquireAdjacent(
      select(scene, pin.epoch, pin.signal),
      pin.ownerId,
      transitionFrame,
      pin.signal,
      () => current(pin) && token === intent,
    );
  }
  async function acquireAdjacent(
    next: Selection,
    afterOwnerId: string,
    transitionFrame: number,
    signal: AbortSignal,
    valid: () => boolean,
  ) {
    const acquire = options.leaseClient.acquireAudioPreview;
    if (
      acquire === undefined ||
      next.ownerId === null ||
      next.assetId === null ||
      next.ownerId === afterOwnerId ||
      next.sourceStartSample === null ||
      next.sourceEndSample === null
    )
      return;
    const asset = currentManifest.assets.find(
      (candidate) => candidate.assetId === next.assetId,
    );
    if (
      asset?.kind !== "video" ||
      asset.sourceFrameCount === null ||
      asset.sourceSampleCount === null
    )
      throw new Error("invalid_scene");
    const ownerId = next.ownerId;
    const sourceEndFrame = asset.sourceFrameCount;
    const acquireOwned = () =>
      acquire(
        Object.freeze({
          asset,
          ownerId,
          epoch: next.epoch,
          signal,
        }),
        Object.freeze({
          snapshot: currentSnapshot,
          manifest: currentManifest,
          clipId: ownerId,
          sourceStartFrame: 0,
          sourceEndFrame,
        }),
      );
    // IMPORTANT: adjacent audio competes for the same worker as asset preparation. Use the
    // monitor's arbiter and join its cancellation cleanup before create/open, or audio waits
    // for the background encode or fails busy while the outgoing owner is still playing.
    const owned = await (options.playbackScheduler?.acquirePlayback(
      acquireOwned,
    ) ?? acquireOwned());
    try {
      const decoded = await decodePreview(
        owned.audioBody,
        asset.sourceSampleCount,
      );
      if (!valid()) {
        decodedBytes -= decoded.decodedBytes;
        return;
      }
      if (prepared !== undefined) decodedBytes -= prepared.decodedBytes;
      prepared = Object.freeze({
        ownerId: next.ownerId,
        assetId: next.assetId,
        transitionFrame,
        selection: next,
        buffer: decoded.buffer,
        decodedBytes: decoded.decodedBytes,
      });
    } finally {
      await owned.release();
    }
  }
  /**
   * Fetch the audio of the clip after the incoming one while the outgoing one still plays, from
   * the scenes the session already resolved (no resolver call here: the session's resolver
   * prepares resources and replaces its current scene). Best effort; without it `play` at the
   * cut falls back to `prepareAdjacent`.
   */
  async function prepareAhead(upcoming: readonly unknown[], token: number) {
    const pin = selection;
    const scheduled = successor;
    if (
      upcoming.length < 2 ||
      !current(pin) ||
      token !== intent ||
      prepared !== undefined ||
      scheduled === undefined
    )
      return;
    const incoming = select(
      decodeResolvedScene(upcoming[0]),
      pin.epoch,
      pin.signal,
    );
    // Only behind a successor that is already scheduled: the chain is built from its end time.
    if (
      incoming.ownerId === null ||
      incoming.sourceStartSample === null ||
      incoming.sourceEndSample === null ||
      scheduled.ownerId !== incoming.ownerId ||
      scheduled.transitionFrame !== incoming.scene.frame
    )
      return;
    const remaining = incoming.sourceEndSample - incoming.sourceStartSample;
    if (remaining <= 0 || remaining % 2_000 !== 0) return;
    const transitionFrame = incoming.scene.frame + remaining / 2_000;
    const scene = decodeResolvedScene(upcoming[1]);
    if (scene.frame !== transitionFrame) return;
    await acquireAdjacent(
      select(scene, pin.epoch, pin.signal),
      incoming.ownerId,
      transitionFrame,
      aheadAbort.signal,
      () => token === intent && !suspended && !failure && !detached,
    );
  }
  function wrapTransport(transport: MediaTransport): MediaTransport {
    const found = sources.get(`${transport.openedEpoch}:${transport.ownerId}`);
    if (!found) throw new Error("invalid_scene");
    const source = found;
    const checkBoundary = () => {
      const pin = selection;
      if (!current(pin) || pin.ownerId !== transport.ownerId) {
        source.element.muted = true;
        return;
      }
      if (
        pin.sourceEndSample !== null &&
        source.element.currentTime * 48_000 >= pin.sourceEndSample
      ) {
        source.element.muted = true;
        failure = "boundary_reached";
        publish("suspended", "boundary_reached");
      }
    };
    const unsubscribe = transport.subscribe(checkBoundary);
    async function decodedBuffer(pin: Selection): Promise<AudioBuffer> {
      const asset = currentSnapshot.assets.find(
        (candidate) => candidate.assetId === pin.assetId,
      );
      if (
        asset?.kind !== "video" ||
        asset.sourceSampleCount === null ||
        source.audioBody === undefined
      )
        throw new Error("audio_resource_limit");
      if (source.decoded === undefined) {
        source.decoded = decodePreview(
          source.audioBody,
          asset.sourceSampleCount,
        ).then((decoded) => {
          source.decodedBytes = decoded.decodedBytes;
          return decoded.buffer;
        });
      }
      return source.decoded;
    }
    const scheduledFor = (pin: Selection) =>
      [audible, successor].some(
        (owner) =>
          owner?.ownerId === transport.ownerId && owner.epoch === pin.epoch,
      );
    async function startAudible(
      pin: Selection,
      token: number,
      buffer: AudioBuffer | undefined,
    ) {
      if (
        pin.sourceStartSample === null ||
        pin.sourceEndSample === null ||
        pin.sourceEndSample <= pin.sourceStartSample
      )
        throw new Error("invalid_scene");
      if (!current(pin) || token !== intent || audioContext === undefined)
        throw new Error("stale_audio_owner");
      // IMPORTANT: HTMLMediaElement.play resolves only after native playback has begun. Anchor the
      // decoded source at the native clock observed at that completion; replaying the seek offset
      // from zero makes audio trail video by roughly one delivered frame on Windows.
      const observedStartSample = Math.round(
        source.element.currentTime * 48_000,
      );
      const audibleStartSample = Math.max(
        pin.sourceStartSample,
        observedStartSample,
      );
      if (
        !Number.isSafeInteger(observedStartSample) ||
        observedStartSample < 0 ||
        audibleStartSample >= pin.sourceEndSample
      )
        throw new Error("stale_audio_owner");
      const existing = [audible, successor].find(
        (owner) =>
          owner?.ownerId === transport.ownerId && owner.epoch === pin.epoch,
      );
      const scheduleAdjacent = (
        owner: ScheduledAudioOwner,
        adjacent: NonNullable<typeof prepared>,
      ) => {
        if (
          adjacent.transitionFrame !==
          pin.scene.frame +
            (pin.sourceEndSample! - pin.sourceStartSample!) / 2_000
        ) {
          decodedBytes -= adjacent.decodedBytes;
          return;
        }
        // CRITICAL: a promoted successor is already playing on this AudioContext clock. Chain the
        // prepared owner from its recorded end time; returning merely because the current owner
        // exists recreates the silent gap at every second adjacent cut in a three-clip sequence.
        const nextNode = audioContext!.createBufferSource();
        nextNode.buffer = adjacent.buffer;
        const nextGain = audioContext!.createGain();
        nextNode.connect(nextGain);
        nextGain.connect(audioContext!.destination);
        const durationSeconds =
          (adjacent.selection.sourceEndSample! -
            adjacent.selection.sourceStartSample!) /
          48_000;
        const nextOwner: ScheduledAudioOwner = {
          node: nextNode,
          gain: nextGain,
          ownerId: adjacent.ownerId,
          epoch: pin.epoch,
          transitionFrame: adjacent.transitionFrame,
          decodedBytes: adjacent.decodedBytes,
          endsAtContextTime: owner.endsAtContextTime + durationSeconds,
        };
        successor = nextOwner;
        nextNode.onended = () => {
          if (successor === nextOwner) successor = undefined;
          if (audible === nextOwner) {
            const following = successor;
            audible = following;
            successor = undefined;
          }
          nextNode.disconnect();
          nextGain.disconnect();
          decodedBytes -= nextOwner.decodedBytes;
          const active = selection;
          if (
            audible === undefined &&
            active?.ownerId === nextOwner.ownerId &&
            current(active)
          ) {
            failure = "boundary_reached";
            publish("suspended", "boundary_reached");
          }
        };
        // The successor starts at its transition frame, `transitionFrame - startFrame`
        // frames into its own clip.
        const nextClip = adjacent.selection.ownerClip!;
        scheduleClipAudioGain(
          nextGain.gain,
          nextClip.audio,
          nextClip.durationFrames,
          (adjacent.transitionFrame - nextClip.startFrame) * 2_000,
          owner.endsAtContextTime,
        );
        nextNode.start(
          owner.endsAtContextTime,
          adjacent.selection.sourceStartSample! / 48_000,
          durationSeconds,
        );
      };
      if (existing !== undefined) {
        if (
          prepared !== undefined &&
          audible === existing &&
          successor === undefined
        ) {
          const adjacent = prepared;
          prepared = undefined;
          scheduleAdjacent(existing, adjacent);
        }
        return;
      }
      if (buffer === undefined) throw new Error("stale_audio_owner");
      const adjacent = prepared;
      prepared = undefined;
      stopAudible();
      const node = audioContext.createBufferSource();
      node.buffer = buffer;
      const gain = audioContext.createGain();
      node.connect(gain);
      gain.connect(audioContext.destination);
      const durationSeconds =
        (pin.sourceEndSample - audibleStartSample) / 48_000;
      // IMPORTANT: one reading of the clock for the source's start, its end and its gain's
      // schedule. Two readings can straddle a render quantum, and the envelope would then
      // run that far ahead of the samples it scales.
      const startTime = audioContext.currentTime;
      const owner: ScheduledAudioOwner = {
        node,
        gain,
        ownerId: transport.ownerId,
        epoch: pin.epoch,
        transitionFrame: null,
        decodedBytes: 0,
        endsAtContextTime: startTime + durationSeconds,
      };
      audible = owner;
      node.onended = () => {
        if (audible !== owner) return;
        if (successor !== undefined) {
          audible = successor;
          successor = undefined;
          owner.node.disconnect();
          owner.gain.disconnect();
          return;
        }
        audible = undefined;
        node.disconnect();
        gain.disconnect();
        decodedBytes -= owner.decodedBytes;
        if (current(pin)) {
          failure = "boundary_reached";
          publish("suspended", "boundary_reached");
        }
      };
      // The owner starts `audibleStartSample - sourceStartSample` samples after its scene's
      // frame, which is `frame - startFrame` frames into its clip.
      const clip = pin.ownerClip!;
      scheduleClipAudioGain(
        gain.gain,
        clip.audio,
        clip.durationFrames,
        (pin.scene.frame - clip.startFrame) * 2_000 +
          audibleStartSample -
          pin.sourceStartSample,
        startTime,
      );
      node.start(startTime, audibleStartSample / 48_000, durationSeconds);
      if (
        adjacent !== undefined &&
        adjacent.transitionFrame ===
          pin.scene.frame +
            (pin.sourceEndSample - pin.sourceStartSample) / 2_000
      ) {
        // CRITICAL: schedule the adjacent owner on the same AudioContext clock before native
        // playback reaches the half-open cut. Replacing this with cut-time decode/restart leaves
        // a reproducible hundreds-of-milliseconds silent gap while the next lease opens.
        scheduleAdjacent(owner, adjacent);
      } else if (adjacent !== undefined) {
        decodedBytes -= adjacent.decodedBytes;
      }
    }
    return Object.freeze({
      ownerId: transport.ownerId,
      assetId: transport.assetId,
      openedEpoch: transport.openedEpoch,
      ...(transport.rebind === undefined
        ? {}
        : {
            rebind: (
              request: Parameters<NonNullable<MediaTransport["rebind"]>>[0],
            ) => {
              source.element.muted = true;
              // IMPORTANT: the map key follows decoder lifetime (openedEpoch), not rebind epochs.
              // Re-keying here breaks final release and discards the same verified PCM/decode promise.
              return transport.rebind!(request);
            },
          }),
      get pendingFrameObservations() {
        return transport.pendingFrameObservations;
      },
      async seek(request) {
        source.element.muted = true;
        // IMPORTANT: a preroll seek positions an owner that is not presented yet, while another
        // owner plays. It is not a move of the playhead: stopping the audible owner here, or
        // claiming this source for the selection's epoch, silences healthy playback every time
        // the runtime prepares for a cut.
        if (request.preroll === true) return transport.seek(request);
        const expectedHandoff = handoffFrame;
        const preservesHandoff =
          expectedHandoff !== null &&
          [audible, successor].some(
            (owner) =>
              owner !== undefined &&
              owner.transitionFrame !== null &&
              owner.transitionFrame <= expectedHandoff &&
              owner.ownerId === transport.ownerId,
          );
        if (!preservesHandoff) stopAudible();
        source.epoch = request.epoch;
        const pin = selection;
        const receipt = await transport.seek(request);
        // IMPORTANT: decode the admitted primary audio while the opening/seek boundary is still
        // settling. Decoding after native play starts lets video advance alone for hundreds of
        // milliseconds and violates the fixed A/V onset bound even though later audio is clean.
        // Not at a preserved handoff: the node scheduled for this owner is already audible and
        // this owner's own buffer is not used, while the decode sits on the cut's path at about
        // 0.4 ms per second of stereo source (24 ms for a minute). `play` decodes if it must
        // start a node after all.
        if (
          !preservesHandoff &&
          current(pin) &&
          pin.ownerId === transport.ownerId &&
          pin.assetId === transport.assetId
        )
          await decodedBuffer(pin);
        return receipt;
      },
      async play(signal) {
        const pin = selection;
        const token = intent;
        let buffer: AudioBuffer | undefined;
        source.element.muted = true;
        if (!current(pin)) throw new Error("stale_audio_owner");
        muteAll();
        try {
          if (
            pin.ownerId === transport.ownerId &&
            pin.assetId === transport.assetId
          ) {
            if (!scheduledFor(pin)) buffer = await decodedBuffer(pin);
            if (!current(pin) || token !== intent || audioContext === undefined)
              throw new Error("stale_audio_owner");
            // IMPORTANT: resume the AudioContext inside the admitted user play boundary before
            // native video starts. Resuming after transport.play lets video run alone for hundreds
            // of milliseconds on Windows and violates the fixed A/V onset bound.
            await audioContext.resume();
            if (!current(pin) || token !== intent)
              throw new Error("stale_audio_owner");
          }
          await transport.play(signal);
          if (
            pin.ownerId === transport.ownerId &&
            pin.assetId === transport.assetId
          ) {
            // The scheduled node can end between the check above and here; start one then.
            if (buffer === undefined && !scheduledFor(pin))
              buffer = await decodedBuffer(pin);
            await startAudible(pin, token, buffer);
          }
        } catch (error) {
          source.element.muted = true;
          if (current(pin) && token === intent) {
            const code = source.element.error?.code;
            stop(
              code === 3 || code === 4
                ? "decode_failed"
                : "playback_unavailable",
            );
          }
          throw error;
        }
        // CRITICAL: pause/close/seek can supersede an unresolved native play promise. Never
        // unmute on completion; stale completion must remain silent even if native play started.
        if (!current(pin) || signal.aborted || token !== intent) {
          source.element.muted = true;
          await transport.pause();
        }
      },
      pause() {
        source.element.muted = true;
        if (handoffFrame === null) stopAudible();
        return transport.pause();
      },
      subscribe: (listener) => transport.subscribe(listener),
      subscribeFailure: (listener) =>
        transport.subscribeFailure?.(listener) ?? (() => undefined),
      async close() {
        source.element.muted = true;
        if (audible?.ownerId === transport.ownerId) {
          const next = successor;
          if (
            handoffFrame !== null &&
            next !== undefined &&
            next.transitionFrame !== null &&
            next.transitionFrame <= handoffFrame
          ) {
            // CRITICAL: owner teardown can win the race against the outgoing node's `ended`
            // callback. Promote the already scheduled successor without stopping it, or an early
            // visual handoff recreates the same audible cut gap on faster native clocks.
            const outgoing = audible;
            outgoing.node.onended = null;
            try {
              outgoing.node.stop();
            } catch {
              // The outgoing node may already have ended between the owner checks.
            }
            outgoing.node.disconnect();
            outgoing.gain.disconnect();
            decodedBytes -= outgoing.decodedBytes;
            audible = next;
            successor = undefined;
          } else stopAudible();
        }
        source.epoch = null;
        unsubscribe();
        await transport.close();
      },
    });
  }
  base = createAuthoringLeasedEditorRuntime({
    ...options,
    transportOptions: {
      ...options.transportOptions,
      onPlaybackFailure(observed) {
        const pin = selection;
        if (
          current(pin) &&
          sources.has(`${observed.openedEpoch}:${observed.ownerId}`)
        )
          stop(observed.reason);
        options.transportOptions?.onPlaybackFailure?.(observed);
      },
    },
    leaseClient: {
      ...options.leaseClient,
      async acquireVideoSource(request, context) {
        // One acquisition at a time (see `prepare`): a move that opens an owner while the audio
        // fetched ahead is still in flight waits for it instead of racing it on the route. The
        // wait ends with the request: this open is an operation of its epoch, and a superseding
        // seek has only the cancel deadline to see it settle.
        const ahead = aheadAudio;
        if (ahead !== undefined && !request.signal.aborted) {
          let cancel!: () => void;
          const cancelled = new Promise<void>((resolve) => {
            cancel = resolve;
          });
          request.signal.addEventListener("abort", cancel, { once: true });
          try {
            await Promise.race([ahead, cancelled]);
          } finally {
            request.signal.removeEventListener("abort", cancel);
          }
          if (request.signal.aborted) throw new Error("cancelled");
        }
        const owned = await options.leaseClient.acquireVideoSource(
          request,
          context,
        );
        const source: Source = {
          element: owned.element,
          epoch: null,
          audioBody: owned.audioBody,
          decodedBytes: 0,
        };
        source.element.muted = true;
        source.element.controls = false;
        source.element.autoplay = false;
        source.element.loop = false;
        source.element.disableRemotePlayback = true;
        source.element.disablePictureInPicture = true;
        const key = `${request.epoch}:${request.ownerId}`;
        sources.set(key, source);
        const onMediaError = () => {
          if (!current(selection) || source.epoch !== selection.epoch) return;
          const code = source.element.error?.code;
          stop(
            code === 3 || code === 4 ? "decode_failed" : "playback_unavailable",
          );
        };
        source.element.addEventListener("error", onMediaError);
        const result: HtmlMediaElementSourceOwner = {
          ...owned,
          ...(owned.rebind === undefined
            ? {}
            : {
                rebind: (
                  next: Parameters<
                    NonNullable<HtmlMediaElementSourceOwner["rebind"]>
                  >[0],
                  nextContext?: Parameters<
                    NonNullable<HtmlMediaElementSourceOwner["rebind"]>
                  >[1],
                ) => owned.rebind!(next, nextContext),
              }),
          async release() {
            source.element.muted = true;
            await owned.release();
            decodedBytes -= source.decodedBytes;
            source.decodedBytes = 0;
            source.decoded = undefined;
            source.audioBody = undefined;
            source.element.removeEventListener("error", onMediaError);
            if (sources.get(key) === source) sources.delete(key);
          },
        };
        return Object.freeze(result);
      },
    },
    async resolveScene(frame, snapshot, context) {
      muteAll();
      const wire = await options.resolveScene(frame, snapshot, context);
      if (context.signal.aborted || detached) throw new Error("cancelled");
      try {
        const scene = decodeResolvedScene(wire);
        if (scene.frame !== frame) throw new Error("invalid_scene");
        selection = select(scene, context.epoch, context.signal);
        if (
          handoffFrame !== null &&
          frame === handoffFrame &&
          selection.ownerId !== null
        ) {
          const next = [audible, successor].find(
            (owner) =>
              owner !== undefined &&
              owner.transitionFrame !== null &&
              owner.transitionFrame <= frame &&
              owner.ownerId === selection!.ownerId,
          );
          if (next !== undefined) next.epoch = context.epoch;
        }
        context.signal.addEventListener("abort", muteAll, { once: true });
        return wire;
      } catch (error) {
        stop("invalid_scene");
        throw error;
      }
    },
    runtimeFactory(dependencies) {
      return (options.runtimeFactory ?? createEditorRuntime)({
        ...dependencies,
        openTransport: async (request) =>
          wrapTransport(await dependencies.openTransport(request)),
      });
    },
  });
  const unsubscribeRuntime = base.subscribe(onRuntime);
  function unsupported(): Promise<RuntimeReceipt> {
    return Promise.resolve(
      Object.freeze({
        schema: RUNTIME_RECEIPT_SCHEMA,
        status: "blocked",
        epoch: base.snapshot().epoch,
        outputFrame: base.snapshot().outputFrame,
        blocker: Object.freeze({ code: "unsupported", subjectId: null }),
      }),
    );
  }
  const runtime: EditorRuntime = Object.freeze({
    capabilities: () => base.capabilities(),
    open(manifest, snapshot, profile) {
      if (detached) return unsupported();
      ++intent;
      playRequested = false;
      muteAll();
      stopAudible();
      failure = undefined;
      return base.open(manifest, snapshot, profile);
    },
    async replace(manifest, snapshot, initialFrame) {
      ++intent;
      playRequested = false;
      muteAll();
      stopAudible();
      failure = undefined;
      try {
        const validated = validatePublicAssetManifest(manifest, snapshot);
        if (
          snapshot.workspaceHandle === currentSnapshot.workspaceHandle &&
          validated.profileFingerprint === currentManifest.profileFingerprint
        ) {
          // IMPORTANT: selection/decode checks must see the new validated authority before base
          // replacement resolves its scene. PCM remains owned only if source rebind succeeds.
          currentSnapshot = snapshot;
          currentManifest = validated;
        }
      } catch {
        /* The leased adapter returns the contract blocker without changing authority. */
      }
      const receipt = await base.replace(manifest, snapshot, initialFrame);
      if (receipt.status === "blocked") stop("invalid_scene");
      return receipt;
    },
    async seek(frame) {
      const preservesHandoff =
        handoffFrame === frame &&
        [audible, successor].some(
          (owner) =>
            owner !== undefined &&
            owner.transitionFrame !== null &&
            owner.transitionFrame <= frame,
        );
      if (!preservesHandoff) ++intent;
      const token = intent;
      playRequested = base.snapshot().status === "playing";
      muteAll();
      if (!preservesHandoff) stopAudible();
      failure = undefined;
      try {
        return await base.seek(frame);
      } finally {
        if (token === intent) playRequested = false;
        onRuntime();
      }
    },
    async advance(frame) {
      const receipt = await base.advance(frame);
      if (
        receipt.status === "blocked" &&
        receipt.blocker?.code === "unsupported"
      ) {
        // CRITICAL: a late wall-clock request can cross a cut while the visual scheduler seeks
        // its next unpresented frame. Pin the scheduled cut, not that distant request; otherwise
        // the automatic boundary seek stops prepared audio and creates a silent replacement gap.
        const committedFrame = base.snapshot().outputFrame;
        const transitions = [audible, successor]
          .map((owner) => owner?.transitionFrame)
          .filter(
            (transition): transition is number =>
              transition !== undefined &&
              transition !== null &&
              committedFrame !== null &&
              transition > committedFrame &&
              transition <= frame,
          );
        handoffFrame = transitions.length ? Math.min(...transitions) : null;
      }
      return receipt;
    },
    prepare(upcoming) {
      const prepareBase = base.prepare;
      if (prepareBase === undefined || detached) return unsupported();
      const token = intent;
      // IMPORTANT: one after the other. The lease route admits one acquisition at a time, so the
      // successor's audio is fetched only after the incoming owner's transport has been opened;
      // `prepareAdjacent` and `acquireVideoSource` wait for what is in flight here.
      const task = (async () => {
        const receipt = await prepareBase(upcoming);
        if (receipt.status === "applied") {
          const audio = prepareAhead(upcoming, token).then(
            () => undefined,
            () => undefined,
          );
          aheadAudio = audio;
          await audio;
          if (aheadAudio === audio) aheadAudio = undefined;
        }
        return receipt;
      })();
      const settled = task.then(
        () => undefined,
        () => undefined,
      );
      preparation = settled;
      void settled.then(() => {
        if (preparation === settled) preparation = undefined;
      });
      return task;
    },
    async play() {
      if (detached) return unsupported();
      if (document.hidden) {
        await suspend();
        return unsupported();
      }
      if (suspended || detached || failure) return unsupported();
      const token = intent;
      playRequested = true;
      try {
        const pin = selection;
        if (current(pin)) {
          try {
            await prepareAdjacent(pin, token);
          } catch {
            // CRITICAL: future-owner prefetch is inside the public play transaction. Convert lease
            // or decode failure into the typed fail-closed receipt; leaking the rejection bypasses
            // runtime cleanup and leaves callers without an actionable playback state.
            if (current(pin) && token === intent) stop("playback_unavailable");
            return await unsupported();
          }
        }
        const receipt = await base.play();
        if (receipt.status === "applied") handoffFrame = null;
        return receipt;
      } finally {
        if (token === intent) playRequested = false;
        onRuntime();
      }
    },
    pause() {
      if (handoffFrame === null) ++intent;
      playRequested = false;
      muteAll();
      if (handoffFrame === null) stopAudible();
      return base.pause();
    },
    snapshot: () => base.snapshot(),
    subscribe: (listener) => base.subscribe(listener),
    async close() {
      ++intent;
      playRequested = false;
      muteAll();
      const receipt = await base.close();
      if (!receipt.blocker && sources.size === 0) {
        detached = true;
        aheadAbort.abort();
        unsubscribeRuntime();
        document.removeEventListener("visibilitychange", onVisibility);
        window.removeEventListener("pagehide", onPageHide);
        stopAudible();
        if (audioContext !== undefined) {
          await audioContext.close();
          audioContext = undefined;
        }
      }
      onRuntime();
      return receipt;
    },
  });
  function suspend() {
    ++intent;
    playRequested = false;
    suspended = true;
    muteAll();
    stopAudible();
    // Suspend cannot wait behind a pending native play before silencing actual media.
    for (const source of sources.values()) source.element.pause();
    publish("suspended", "suspended");
    return base.pause();
  }
  function onVisibility() {
    if (document.hidden) void suspend();
  }
  function onPageHide() {
    void runtime.close();
  }
  document.addEventListener("visibilitychange", onVisibility);
  window.addEventListener("pagehide", onPageHide);
  return Object.freeze({
    runtime,
    status: () => value,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    suspend,
    async resume() {
      if (detached || document.hidden) return unsupported();
      suspended = false;
      return runtime.seek(base.snapshot().outputFrame ?? 0);
    },
  });
}
