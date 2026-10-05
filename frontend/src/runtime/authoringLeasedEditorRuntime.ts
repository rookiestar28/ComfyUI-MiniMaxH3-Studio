import type { PublicCompositionSnapshot } from "../contracts/compositionCodec";
import { canonicalPublicRuntimeAssetFingerprint } from "../contracts/authoringMediaLeaseCodec";
import type {
  AuthoringMediaSourceLeaseClient,
  AuthoringMediaVideoSourceContext,
} from "../host/authoringMediaSourceLease";
import type { RuntimeCapabilityDisposition } from "./mediaCapabilities";
import {
  RUNTIME_RECEIPT_SCHEMA,
  createEditorRuntime,
  createHtmlMediaElementTransportFactory,
  type EditorRuntime,
  type HtmlMediaElementTransportOptions,
  type MediaTransportFactory,
  type RuntimeReceipt,
  type RuntimeSceneResolver,
} from "./editorRuntime";
import {
  publicAssetById,
  validatePublicAssetManifest,
  type PublicAssetManifest,
} from "./publicAssetManifest";

type RuntimeFactory = (
  dependencies: Readonly<{
    capability: RuntimeCapabilityDisposition;
    resolveScene: RuntimeSceneResolver;
    openTransport: MediaTransportFactory;
  }>,
) => EditorRuntime;

export type AuthoringLeasedEditorRuntimeOptions = Readonly<{
  snapshot: PublicCompositionSnapshot;
  manifest: PublicAssetManifest;
  capability: RuntimeCapabilityDisposition;
  resolveScene: RuntimeSceneResolver;
  leaseClient: AuthoringMediaSourceLeaseClient;
  transportOptions?: HtmlMediaElementTransportOptions;
  runtimeFactory?: RuntimeFactory;
}>;

function samePinnedContext(
  manifest: PublicAssetManifest,
  snapshot: PublicCompositionSnapshot,
  pinnedManifest: PublicAssetManifest,
  pinnedSnapshot: PublicCompositionSnapshot,
) {
  try {
    validatePublicAssetManifest(manifest, snapshot);
  } catch {
    return false;
  }
  return (
    snapshot.workspaceHandle === pinnedSnapshot.workspaceHandle &&
    snapshot.workspaceRevision === pinnedSnapshot.workspaceRevision &&
    snapshot.timelineRevision === pinnedSnapshot.timelineRevision &&
    snapshot.publicFingerprint === pinnedSnapshot.publicFingerprint &&
    manifest.manifestFingerprint === pinnedManifest.manifestFingerprint &&
    manifest.profileFingerprint === pinnedManifest.profileFingerprint
  );
}

function blocked(runtime: EditorRuntime): RuntimeReceipt {
  const value = runtime.snapshot();
  return Object.freeze({
    schema: RUNTIME_RECEIPT_SCHEMA,
    status: "blocked",
    epoch: value.epoch,
    outputFrame: value.outputFrame,
    blocker: Object.freeze({ code: "contract_mismatch", subjectId: null }),
  });
}

export function createAuthoringLeasedEditorRuntime(
  options: AuthoringLeasedEditorRuntimeOptions,
): EditorRuntime {
  let pinnedManifest = validatePublicAssetManifest(
    options.manifest,
    options.snapshot,
  );
  let pinnedSnapshot = options.snapshot;
  const sourceContext = (request: Parameters<MediaTransportFactory>[0]) => {
    const clip = pinnedSnapshot.clips.find(
      (value) => value.clipId === request.ownerId,
    );
    const asset = publicAssetById(pinnedManifest, request.asset.assetId);
    if (
      clip === undefined ||
      clip.assetId !== request.asset.assetId ||
      asset === undefined ||
      canonicalPublicRuntimeAssetFingerprint(asset) !==
        canonicalPublicRuntimeAssetFingerprint(request.asset) ||
      asset.kind !== "video" ||
      asset.sourceFrameCount === null
    )
      throw new Error("contract_mismatch");
    const context: AuthoringMediaVideoSourceContext = Object.freeze({
      snapshot: pinnedSnapshot,
      manifest: pinnedManifest,
      clipId: clip.clipId,
      // IMPORTANT: the editor output duration is not a source-time span for VFR media. A video
      // proxy lease always binds the complete accepted source frame identity or seeks can drift.
      sourceStartFrame: 0,
      sourceEndFrame: asset.sourceFrameCount,
    });
    return context;
  };
  const acquire = async (request: Parameters<MediaTransportFactory>[0]) => {
    const owner = await options.leaseClient.acquireVideoSource(
      request,
      sourceContext(request),
    );
    return Object.freeze({
      ...owner,
      release: () => owner.release(),
      ...(owner.rebind === undefined
        ? {}
        : {
            // IMPORTANT: reauthorization uses the validated new binding, never the context
            // captured when this decoder was first opened or one supplied by another caller.
            rebind: (next: Parameters<MediaTransportFactory>[0]) =>
              owner.rebind!(next, sourceContext(next)),
          }),
    });
  };
  const runtime = (options.runtimeFactory ?? createEditorRuntime)({
    capability: options.capability,
    resolveScene: async (frame, snapshot, context) => {
      if (
        !samePinnedContext(
          pinnedManifest,
          snapshot,
          pinnedManifest,
          pinnedSnapshot,
        )
      )
        throw new Error("contract_mismatch");
      return options.resolveScene(frame, snapshot, context);
    },
    openTransport: createHtmlMediaElementTransportFactory(
      acquire,
      options.transportOptions,
    ),
  });
  return Object.freeze({
    capabilities: () => runtime.capabilities(),
    open(manifest, snapshot, profileFingerprint) {
      if (
        !samePinnedContext(manifest, snapshot, pinnedManifest, pinnedSnapshot)
      )
        return Promise.resolve(blocked(runtime));
      return runtime.open(manifest, snapshot, profileFingerprint);
    },
    replace(manifest, snapshot, initialFrame) {
      let validated: PublicAssetManifest;
      try {
        validated = validatePublicAssetManifest(manifest, snapshot);
      } catch {
        return Promise.resolve(blocked(runtime));
      }
      if (
        snapshot.workspaceHandle !== pinnedSnapshot.workspaceHandle ||
        validated.profileFingerprint !== pinnedManifest.profileFingerprint
      )
        return Promise.resolve(blocked(runtime));
      // IMPORTANT: update authority before delegating; held owners rebind inside replace.
      // Restoring an older binding on failure would authorize stale acquisitions after an edit.
      pinnedManifest = validated;
      pinnedSnapshot = snapshot;
      return runtime.replace(validated, snapshot, initialFrame);
    },
    seek: (frame) => runtime.seek(frame),
    advance: (frame) => runtime.advance(frame),
    ...(runtime.prepare === undefined
      ? {}
      : {
          prepare: (upcoming: readonly unknown[]) => runtime.prepare!(upcoming),
        }),
    play: () => runtime.play(),
    pause: () => runtime.pause(),
    snapshot: () => runtime.snapshot(),
    subscribe: (listener) => runtime.subscribe(listener),
    close: () => runtime.close(),
  });
}
