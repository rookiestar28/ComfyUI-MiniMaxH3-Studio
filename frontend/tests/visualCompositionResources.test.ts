import { describe, expect, it, vi } from "vitest";
import fixture from "../../tests/fixtures/m25_10_composition_contract_v1.json";
import {
  decodePublicCompositionSnapshot,
  decodeResolvedScene,
  publicCompositionFingerprint,
  RESOLVED_SCENE_SCHEMA,
  type ResolvedCompositionScene,
} from "../src/contracts/compositionCodec";
import { createVisualCompositionResources } from "../src/runtime/visualCompositionResources";
import { buildPublicAssetManifest } from "../src/runtime/publicAssetManifest";
import type { AuthoringMediaSourceLeaseClient } from "../src/host/authoringMediaSourceLease";
import type { NlePlaybackLeaseScheduler } from "../src/host/nleLeaseScheduler";

function setup(
  geometry: unknown = {
    schema: "h3.authoring.media_geometry.v1",
    sourceWidth: 640,
    sourceHeight: 360,
    derivativeWidth: 320,
    derivativeHeight: 180,
  },
) {
  const snapshot = decodePublicCompositionSnapshot(
    structuredClone(fixture.snapshot),
  );
  const clip = fixture.snapshot.clips[2]!;
  const scene = decodeResolvedScene({
    schema: RESOLVED_SCENE_SCHEMA,
    profile_id: snapshot.profileId,
    public_fingerprint: snapshot.publicFingerprint,
    frame: 0,
    layers: [
      {
        clip_id: clip.clip_id,
        asset_id: clip.asset_id,
        track_id: clip.track_id,
        source_frame: null,
        source_pts: null,
        transition_elapsed_frames: null,
        operation_ids: [
          "SelectSourceRangeV1",
          "CropV1",
          "Transform2DV1",
          "OpacityV1",
          "BlendV1",
        ],
        transform: clip.transform,
        crop: clip.crop,
        opacity_bp: clip.opacity_bp,
        blend: clip.blend,
        text: null,
        effect: clip.effect,
      },
    ],
    audio_span: null,
    blockers: [],
  });
  const release = vi.fn(async (): Promise<void> => undefined);
  const bitmap = { width: 320, height: 180, close: vi.fn() };
  let leaseFailure: (() => void) | undefined;
  const unsubscribe = vi.fn();
  const lease = {
    open: vi.fn(async () => ({
      blob: new Blob([new Uint8Array(8)], { type: "image/png" }),
      geometry,
    })),
    release,
    subscribeFailure: vi.fn((listener: () => void) => {
      leaseFailure = listener;
      return unsubscribe;
    }),
  };
  const client = {
    create: vi.fn(async () => lease),
    acquireVideoSource: vi.fn(),
    close: vi.fn(async () => undefined),
  };
  const decodeImage = vi.fn(async () => bitmap);
  return {
    snapshot,
    scene,
    release,
    bitmap,
    client: client as unknown as AuthoringMediaSourceLeaseClient,
    decodeImage,
    lease,
    unsubscribe,
    failLease() {
      if (!leaseFailure)
        throw new Error("lease failure listener is unavailable");
      leaseFailure();
    },
  };
}

function textSetup() {
  const wire = structuredClone(fixture.snapshot);
  for (const track of wire.tracks)
    track.enabled = track.track_id === "track-text";
  for (const clip of wire.clips) {
    clip.enabled = clip.track_id === "track-text";
    if (!clip.enabled) clip.transition = { kind: "none", duration_frames: 0 };
  }
  wire.assets[3]!.asset_id = "h3.font.noto_sans.v1";
  wire.clips[3]!.text!.font_asset_id = "h3.font.noto_sans.v1";
  wire.public_fingerprint = publicCompositionFingerprint(wire);
  const snapshot = decodePublicCompositionSnapshot(wire);
  const clip = wire.clips[3]!;
  const scene = decodeResolvedScene({
    schema: RESOLVED_SCENE_SCHEMA,
    profile_id: snapshot.profileId,
    public_fingerprint: snapshot.publicFingerprint,
    frame: 12,
    layers: [
      {
        clip_id: clip.clip_id,
        asset_id: null,
        track_id: clip.track_id,
        source_frame: null,
        source_pts: null,
        transition_elapsed_frames: null,
        operation_ids: [
          "SelectSourceRangeV1",
          "CropV1",
          "Transform2DV1",
          "OpacityV1",
          "BlendV1",
          "DrawTextV1",
        ],
        transform: clip.transform,
        crop: clip.crop,
        opacity_bp: clip.opacity_bp,
        blend: clip.blend,
        text: clip.text,
        effect: clip.effect,
      },
    ],
    audio_span: null,
    blockers: [],
  });
  const releases: ReturnType<typeof vi.fn<() => Promise<void>>>[] = [];
  const requests: Array<Record<string, unknown>> = [];
  const leaseFailures: Array<() => void> = [];
  const client = {
    create: vi.fn(async (request: Record<string, unknown>) => {
      requests.push(request);
      const release = vi.fn(async (): Promise<void> => undefined);
      releases.push(release);
      return {
        open: vi.fn(async () => ({
          blob: new Blob([new Uint8Array(8)], { type: "font/ttf" }),
          geometry: null,
        })),
        release,
        subscribeFailure: vi.fn((listener: () => void) => {
          leaseFailures.push(listener);
          return () => undefined;
        }),
      };
    }),
    acquireVideoSource: vi.fn(),
    close: vi.fn(async () => undefined),
  };
  const faces: Array<{ family: string; close: ReturnType<typeof vi.fn> }> = [];
  const loadFont = vi.fn(
    async (
      _bytes: ArrayBuffer,
      family: string,
      _weight: string,
      _style: string,
    ) => {
      const face = { family, close: vi.fn() };
      faces.push(face);
      return face;
    },
  );
  return {
    snapshot,
    scene,
    requests,
    releases,
    client: client as unknown as AuthoringMediaSourceLeaseClient,
    loadFont,
    faces,
    leaseFailures,
  };
}

describe("visual composition resource ownership", () => {
  it("reauthorizes the same verified bitmap without opening or decoding again and fences the old lease", async () => {
    const s = setup();
    const identity = {
      fingerprint: `sha256:${"4".repeat(64)}`,
      byteCount: 8,
      kind: "image_proxy",
    };
    const body = await s.lease.open();
    s.lease.open.mockClear();
    const verifiedBody = {
      ...body,
      receipt: {
        derivativeFingerprint: identity.fingerprint,
        byteCount: 8,
        derivativeKind: identity.kind,
      },
    };
    s.lease.open.mockResolvedValue(verifiedBody);
    let freshFailure!: () => void;
    const fresh = {
      derivative: () => identity,
      adopt: vi.fn(),
      open: vi.fn(async () => verifiedBody),
      release: vi.fn(async () => undefined),
      subscribeFailure: (listener: () => void) => {
        freshFailure = listener;
        return () => undefined;
      },
    };
    vi.mocked(s.client.create)
      .mockResolvedValueOnce(s.lease as never)
      .mockResolvedValueOnce(fresh as never);
    const resources = createVisualCompositionResources({
      snapshot: s.snapshot,
      manifest: buildPublicAssetManifest(s.snapshot),
      leaseClient: s.client,
      decodeImage: s.decodeImage,
    });
    await resources.prepare(s.scene, 1, new AbortController().signal);
    const wire = structuredClone(fixture.snapshot);
    wire.timeline_revision += 1;
    wire.public_fingerprint = publicCompositionFingerprint(wire);
    const next = decodePublicCompositionSnapshot(wire);
    const nextScene = { ...s.scene, publicFingerprint: next.publicFingerprint };
    await resources.replace(buildPublicAssetManifest(next), next);
    expect(() => resources.read(s.scene, 1)).toThrow("source_unavailable");
    await resources.prepare(nextScene, 2, new AbortController().signal);
    expect(s.client.create).toHaveBeenCalledTimes(2);
    expect(s.lease.open).toHaveBeenCalledOnce();
    expect(fresh.open).not.toHaveBeenCalled();
    expect(fresh.adopt).toHaveBeenCalledOnce();
    expect(s.decodeImage).toHaveBeenCalledOnce();
    expect(s.bitmap.close).not.toHaveBeenCalled();
    expect(s.release).toHaveBeenCalledOnce();
    expect(resources.snapshot().staticLeases).toBe(1);
    s.failLease();
    const image = resources.read(nextScene, 2).get("clip-image");
    expect(image?.kind === "image" && image.source).toBe(s.bitmap);
    freshFailure();
    expect(s.bitmap.close).toHaveBeenCalledOnce();
    expect(() => resources.read(nextScene, 2)).toThrow("source_unavailable");
    await resources.close();
    expect(resources.snapshot().staticLeases).toBe(0);
  });

  it("reopens only the static resource whose new derivative identity differs", async () => {
    const s = setup();
    const body = await s.lease.open();
    s.lease.open.mockClear();
    const verifiedBody = {
      ...body,
      receipt: {
        derivativeFingerprint: `sha256:${"4".repeat(64)}`,
        byteCount: 8,
        derivativeKind: "image_proxy",
      },
    };
    s.lease.open.mockResolvedValue(verifiedBody);
    const fresh = {
      derivative: () => ({
        fingerprint: `sha256:${"5".repeat(64)}`,
        byteCount: 8,
        kind: "image_proxy",
      }),
      adopt: vi.fn(),
      open: vi.fn(async () => body),
      release: vi.fn(async () => undefined),
      subscribeFailure: () => () => undefined,
    };
    vi.mocked(s.client.create)
      .mockResolvedValueOnce(s.lease as never)
      .mockResolvedValueOnce(fresh as never);
    const resources = createVisualCompositionResources({
      snapshot: s.snapshot,
      manifest: buildPublicAssetManifest(s.snapshot),
      leaseClient: s.client,
      decodeImage: s.decodeImage,
    });
    await resources.prepare(s.scene, 1, new AbortController().signal);
    const wire = structuredClone(fixture.snapshot);
    wire.timeline_revision += 1;
    wire.public_fingerprint = publicCompositionFingerprint(wire);
    const next = decodePublicCompositionSnapshot(wire);
    await resources.replace(buildPublicAssetManifest(next), next);
    await resources.prepare(
      { ...s.scene, publicFingerprint: next.publicFingerprint },
      2,
      new AbortController().signal,
    );
    expect(fresh.adopt).not.toHaveBeenCalled();
    expect(fresh.open).toHaveBeenCalledOnce();
    expect(s.decodeImage).toHaveBeenCalledTimes(2);
    expect(s.bitmap.close).toHaveBeenCalledOnce();
    expect(s.release).toHaveBeenCalledOnce();
    await resources.close();
    expect(fresh.release).toHaveBeenCalledOnce();
  });

  it("keeps both lease obligations counted when retiring old bitmap authority fails, then retries cleanup", async () => {
    const s = setup();
    const body = await s.lease.open();
    s.lease.open.mockClear();
    const identity = {
      fingerprint: `sha256:${"4".repeat(64)}`,
      byteCount: 8,
      kind: "image_proxy",
    };
    const verifiedBody = {
      ...body,
      receipt: {
        derivativeFingerprint: identity.fingerprint,
        byteCount: 8,
        derivativeKind: identity.kind,
      },
    };
    s.lease.open.mockResolvedValue(verifiedBody);
    const fresh = {
      derivative: () => identity,
      adopt: vi.fn(),
      open: vi.fn(),
      release: vi.fn(async () => undefined),
      subscribeFailure: () => () => undefined,
    };
    vi.mocked(s.client.create)
      .mockResolvedValueOnce(s.lease as never)
      .mockResolvedValueOnce(fresh as never);
    const resources = createVisualCompositionResources({
      snapshot: s.snapshot,
      manifest: buildPublicAssetManifest(s.snapshot),
      leaseClient: s.client,
      decodeImage: s.decodeImage,
    });
    await resources.prepare(s.scene, 1, new AbortController().signal);
    const wire = structuredClone(fixture.snapshot);
    wire.timeline_revision += 1;
    wire.public_fingerprint = publicCompositionFingerprint(wire);
    const next = decodePublicCompositionSnapshot(wire);
    await resources.replace(buildPublicAssetManifest(next), next);
    s.release.mockRejectedValueOnce(new Error("lease_busy"));
    const nextScene = { ...s.scene, publicFingerprint: next.publicFingerprint };
    await expect(
      resources.prepare(nextScene, 2, new AbortController().signal),
    ).rejects.toThrow("lease_busy");
    expect(resources.snapshot()).toMatchObject({
      staticLeases: 2,
      imageOwners: 1,
    });
    expect(() => resources.read(nextScene, 2)).toThrow("source_unavailable");
    expect(fresh.open).not.toHaveBeenCalled();
    await resources.close();
    expect(s.release).toHaveBeenCalledTimes(2);
    expect(fresh.release).toHaveBeenCalledOnce();
    expect(s.bitmap.close).toHaveBeenCalledOnce();
    expect(resources.snapshot().staticLeases).toBe(0);
  });

  it("keeps a healthy prepared resource live beyond the former local timeout", async () => {
    vi.useFakeTimers();
    try {
      const s = setup();
      const onFailure = vi.fn();
      const resources = createVisualCompositionResources({
        snapshot: s.snapshot,
        manifest: buildPublicAssetManifest(s.snapshot),
        leaseClient: s.client,
        decodeImage: s.decodeImage,
        onFailure,
      });
      await resources.prepare(s.scene, 1, new AbortController().signal);
      expect(resources.read(s.scene, 1).get("clip-image")).toMatchObject({
        kind: "image",
      });

      await vi.advanceTimersByTimeAsync(120_000);

      expect(onFailure).not.toHaveBeenCalled();
      expect(resources.read(s.scene, 1).get("clip-image")).toMatchObject({
        kind: "image",
      });
      await resources.close();
    } finally {
      vi.useRealTimers();
    }
  });

  it("limits playback priority to static owner acquisition, not retained lifetime", async () => {
    const s = setup();
    const scheduler = {
      acquirePlayback: vi.fn(async <T>(operation: () => Promise<T>) =>
        operation(),
      ),
    };
    const resources = createVisualCompositionResources({
      snapshot: s.snapshot,
      manifest: buildPublicAssetManifest(s.snapshot),
      leaseClient: s.client,
      decodeImage: s.decodeImage,
      playbackScheduler: scheduler as NlePlaybackLeaseScheduler,
    });

    await resources.prepare(s.scene, 1, new AbortController().signal);

    expect(scheduler.acquirePlayback).toHaveBeenCalledOnce();
    await resources.close();
    expect(s.release).toHaveBeenCalledOnce();
  });

  it("reuses one healthy immutable owner across presentation epochs and fences stale reads", async () => {
    const s = setup();
    const manifest = buildPublicAssetManifest(s.snapshot);
    const resources = createVisualCompositionResources({
      snapshot: s.snapshot,
      manifest,
      leaseClient: s.client,
      decodeImage: s.decodeImage,
    });
    await resources.prepare(s.scene, 11, new AbortController().signal);
    const first = resources.read(s.scene, 11).get("clip-image");

    await resources.prepare(s.scene, 12, new AbortController().signal);

    expect(s.client.create).toHaveBeenCalledOnce();
    expect(s.decodeImage).toHaveBeenCalledOnce();
    expect(s.release).not.toHaveBeenCalled();
    expect(() => resources.read(s.scene, 11)).toThrow("source_unavailable");
    expect(resources.read(s.scene, 12).get("clip-image")).toBe(first);
    expect(s.client.create).toHaveBeenCalledWith(
      expect.objectContaining({
        workspaceHandle: s.snapshot.workspaceHandle,
        workspaceRevision: s.snapshot.workspaceRevision,
        timelineRevision: s.snapshot.timelineRevision,
        publicFingerprint: s.snapshot.publicFingerprint,
        manifestFingerprint: manifest.manifestFingerprint,
        profileFingerprint: manifest.profileFingerprint,
        clipId: "clip-image",
        assetId: "img-overlay",
        derivativeKind: "image_proxy",
        runtimeEpoch: 11,
      }),
      expect.any(AbortSignal),
      expect.stringMatching(/^sha256:/),
    );
    await resources.close();
    expect(s.release).toHaveBeenCalledOnce();
  });

  it("releases a static owner when the next presentation no longer wants its clip", async () => {
    const s = setup();
    const manifest = buildPublicAssetManifest(s.snapshot);
    const resources = createVisualCompositionResources({
      snapshot: s.snapshot,
      manifest,
      leaseClient: s.client,
      decodeImage: s.decodeImage,
    });
    await resources.prepare(s.scene, 1, new AbortController().signal);
    const clip = fixture.snapshot.clips[0]!;
    const videoOnly = decodeResolvedScene({
      schema: RESOLVED_SCENE_SCHEMA,
      profile_id: s.snapshot.profileId,
      public_fingerprint: s.snapshot.publicFingerprint,
      frame: 0,
      layers: [
        {
          clip_id: clip.clip_id,
          asset_id: clip.asset_id,
          track_id: clip.track_id,
          source_frame: 0,
          source_pts: 0,
          transition_elapsed_frames: null,
          operation_ids: [
            "SelectSourceRangeV1",
            "CropV1",
            "Transform2DV1",
            "OpacityV1",
            "BlendV1",
          ],
          transform: clip.transform,
          crop: clip.crop,
          opacity_bp: clip.opacity_bp,
          blend: clip.blend,
          text: null,
          effect: clip.effect,
        },
      ],
      audio_span: null,
      blockers: [],
    });

    await resources.prepare(videoOnly, 2, new AbortController().signal);

    expect(s.bitmap.close).toHaveBeenCalledOnce();
    expect(s.release).toHaveBeenCalledOnce();
    expect(resources.snapshot()).toMatchObject({
      imageOwners: 0,
      staticLeases: 0,
      blobBytes: 0,
    });
    await resources.close();
  });

  it("replaces a font owner only when its exact face binding changes", async () => {
    const s = textSetup();
    const resources = createVisualCompositionResources({
      snapshot: s.snapshot,
      manifest: buildPublicAssetManifest(s.snapshot),
      leaseClient: s.client,
      loadFont: s.loadFont,
    });
    await resources.prepare(s.scene, 1, new AbortController().signal);
    const italic = {
      ...s.scene,
      layers: [
        {
          ...s.scene.layers[0]!,
          text: { ...s.scene.layers[0]!.text!, style: "italic" },
        },
      ],
    } as ResolvedCompositionScene;

    await resources.prepare(italic, 2, new AbortController().signal);
    await resources.prepare(italic, 3, new AbortController().signal);

    expect(s.client.create).toHaveBeenCalledTimes(2);
    expect(s.requests.map((request) => request.runtimeEpoch)).toEqual([1, 2]);
    expect(s.loadFont).toHaveBeenCalledTimes(2);
    expect(s.faces[0]!.close).toHaveBeenCalledOnce();
    expect(s.releases[0]).toHaveBeenCalledOnce();
    expect(s.releases[1]).not.toHaveBeenCalled();
    expect(resources.read(italic, 3).get("clip-title")).toMatchObject({
      kind: "font",
      family: s.faces[1]!.family,
    });
    await resources.close();
    expect(s.faces[1]!.close).toHaveBeenCalledOnce();
    expect(s.releases[1]).toHaveBeenCalledOnce();
  });

  it("revokes a decoded owner synchronously on lease failure and retains a failed release for retry", async () => {
    const s = setup();
    const onFailure = vi.fn();
    let rejectRelease!: (error: Error) => void;
    s.release.mockImplementationOnce(
      () =>
        new Promise<void>((_resolve, reject) => {
          rejectRelease = reject;
        }),
    );
    const resources = createVisualCompositionResources({
      snapshot: s.snapshot,
      manifest: buildPublicAssetManifest(s.snapshot),
      leaseClient: s.client,
      decodeImage: s.decodeImage,
      onFailure,
    });
    await resources.prepare(s.scene, 1, new AbortController().signal);

    s.failLease();

    expect(s.bitmap.close).toHaveBeenCalledOnce();
    expect(s.unsubscribe).toHaveBeenCalledOnce();
    expect(onFailure).toHaveBeenCalledOnce();
    expect(resources.snapshot()).toMatchObject({
      imageOwners: 0,
      blobBytes: 0,
    });
    expect(() => resources.read(s.scene, 1)).toThrow("source_unavailable");
    await vi.waitFor(() => expect(s.release).toHaveBeenCalledOnce());
    const nextPresentation = resources.prepare(
      s.scene,
      2,
      new AbortController().signal,
    );
    await Promise.resolve();
    expect(s.client.create).toHaveBeenCalledOnce();
    expect(() => resources.read(s.scene, 2)).toThrow("source_unavailable");
    rejectRelease(new Error("lease_busy"));
    await expect(nextPresentation).rejects.toThrow("lease_busy");
    expect(resources.snapshot().staticLeases).toBe(1);
    await resources.close();
    expect(s.release).toHaveBeenCalledTimes(2);
    expect(resources.snapshot().staticLeases).toBe(0);
    expect(s.bitmap.close).toHaveBeenCalledOnce();
  });

  it("cannot resurrect a bitmap when its lease fails while decode is pending", async () => {
    const s = setup();
    let finish!: (value: typeof s.bitmap) => void;
    s.decodeImage.mockImplementationOnce(
      () => new Promise((resolve) => (finish = resolve)),
    );
    const resources = createVisualCompositionResources({
      snapshot: s.snapshot,
      manifest: buildPublicAssetManifest(s.snapshot),
      leaseClient: s.client,
      decodeImage: s.decodeImage,
    });
    const preparing = resources.prepare(
      s.scene,
      1,
      new AbortController().signal,
    );
    await vi.waitFor(() => expect(s.decodeImage).toHaveBeenCalledOnce());

    s.failLease();
    finish(s.bitmap);

    await expect(preparing).rejects.toThrow("source_unavailable");
    expect(s.bitmap.close).toHaveBeenCalledOnce();
    expect(resources.snapshot()).toMatchObject({
      imageOwners: 0,
      staticLeases: 0,
      blobBytes: 0,
    });
    expect(() => resources.read(s.scene, 1)).toThrow("source_unavailable");
    await resources.close();
  });

  it("cannot resurrect a font face when its lease fails while loading", async () => {
    const s = textSetup();
    let finish!: () => void;
    const lateClose = vi.fn<() => void>();
    s.loadFont.mockImplementationOnce(
      async (
        _bytes: ArrayBuffer,
        requestedFamily: string,
        _weight: string,
        _style: string,
      ) => {
        await new Promise<void>((resolve) => (finish = resolve));
        return { family: requestedFamily, close: lateClose };
      },
    );
    const resources = createVisualCompositionResources({
      snapshot: s.snapshot,
      manifest: buildPublicAssetManifest(s.snapshot),
      leaseClient: s.client,
      loadFont: s.loadFont,
    });
    const preparing = resources.prepare(
      s.scene,
      1,
      new AbortController().signal,
    );
    await vi.waitFor(() => expect(s.loadFont).toHaveBeenCalledOnce());

    s.leaseFailures[0]!();
    finish();

    await expect(preparing).rejects.toThrow("source_unavailable");
    expect(lateClose).toHaveBeenCalledOnce();
    expect(resources.snapshot()).toMatchObject({
      fontOwners: 0,
      staticLeases: 0,
      blobBytes: 0,
    });
    expect(() => resources.read(s.scene, 1)).toThrow("source_unavailable");
    await resources.close();
  });

  it("rejects a lease that reports failure synchronously while registering its listener", async () => {
    const s = setup();
    s.lease.subscribeFailure.mockImplementationOnce((listener: () => void) => {
      listener();
      return s.unsubscribe;
    });
    const resources = createVisualCompositionResources({
      snapshot: s.snapshot,
      manifest: buildPublicAssetManifest(s.snapshot),
      leaseClient: s.client,
      decodeImage: s.decodeImage,
    });

    await expect(
      resources.prepare(s.scene, 1, new AbortController().signal),
    ).rejects.toThrow("source_unavailable");
    expect(s.lease.open).not.toHaveBeenCalled();
    expect(s.decodeImage).not.toHaveBeenCalled();
    expect(s.unsubscribe).toHaveBeenCalledOnce();
    await vi.waitFor(() => expect(s.release).toHaveBeenCalledOnce());
    expect(resources.snapshot()).toMatchObject({
      imageOwners: 0,
      staticLeases: 0,
      blobBytes: 0,
    });
    await resources.close();
  });

  it("rejects excess layers as a resource limit before acquiring any owner", async () => {
    const s = setup();
    const resources = createVisualCompositionResources({
      snapshot: s.snapshot,
      manifest: buildPublicAssetManifest(s.snapshot),
      leaseClient: s.client,
    });
    await expect(
      resources.prepare(
        { ...s.scene, layers: Array(9).fill(s.scene.layers[0]) },
        1,
        new AbortController().signal,
      ),
    ).rejects.toThrow("resource_limit");
    expect(s.client.create).not.toHaveBeenCalled();
    expect(s.client.acquireVideoSource).not.toHaveBeenCalled();
    await resources.close();
  });
  it("revokes all local owners immediately and cancels prepare before server release settles", async () => {
    const s = setup();
    const imageClip = s.snapshot.clips.find(
      (row) => row.clipId === "clip-image",
    )!;
    const snapshot = {
      ...s.snapshot,
      tracks: [
        ...s.snapshot.tracks,
        { ...s.snapshot.tracks[2]!, trackId: "track-image-2", order: 4 },
      ],
      clips: [
        ...s.snapshot.clips,
        { ...imageClip, clipId: "clip-image-2", trackId: "track-image-2" },
      ],
    };
    const scene = {
      ...s.scene,
      layers: [
        ...s.scene.layers,
        {
          ...s.scene.layers[0]!,
          clipId: "clip-image-2",
          trackId: "track-image-2",
        },
      ],
    };
    let finishDecode!: (value: typeof s.bitmap) => void;
    let finishRelease!: () => void;
    const serverRelease = new Promise<void>((resolve) => {
      finishRelease = resolve;
    });
    s.release.mockImplementation(() => serverRelease);
    s.decodeImage
      .mockImplementationOnce(async () => s.bitmap)
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            finishDecode = resolve;
          }),
      );
    const resources = createVisualCompositionResources({
      snapshot,
      manifest: buildPublicAssetManifest(snapshot),
      leaseClient: s.client,
      decodeImage: s.decodeImage,
    });
    let cancelled = false;
    const preparing = resources
      .prepare(scene, 1, new AbortController().signal)
      .catch(() => {
        cancelled = true;
      });
    await vi.waitFor(() => expect(finishDecode).toBeTypeOf("function"));
    const closing = resources.close();
    const immediatelyClosed = s.bitmap.close.mock.calls.length;
    await new Promise((resolve) => setTimeout(resolve, 20));
    const cancelledBeforeRelease = cancelled;
    const late = { width: 320, height: 180, close: vi.fn() };
    finishDecode(late);
    finishRelease();
    await Promise.all([preparing, closing]);
    expect(immediatelyClosed).toBe(1);
    expect(cancelledBeforeRelease).toBe(true);
    expect(late.close).toHaveBeenCalledOnce();
    expect(resources.snapshot()).toMatchObject({
      staticLeases: 0,
      pendingOperations: 0,
      imageOwners: 0,
    });
  });
  it("preserves the same-authority audio preview through the visual owner wrapper", async () => {
    const s = setup();
    const manifest = buildPublicAssetManifest(s.snapshot);
    const asset = manifest.assets.find((row) => row.kind === "video")!;
    const clip = s.snapshot.clips.find((row) => row.assetId === asset.assetId)!;
    const audioBody = new Blob([new Uint8Array(44)], { type: "audio/wav" });
    const release = vi.fn(async () => undefined);
    vi.mocked(s.client.acquireVideoSource).mockResolvedValueOnce({
      element: document.createElement("video"),
      audioBody,
      release,
      geometry: {
        schema: "h3.authoring.media_geometry.v1",
        sourceWidth: 640,
        sourceHeight: 360,
        derivativeWidth: 320,
        derivativeHeight: 180,
      },
    });
    const resources = createVisualCompositionResources({
      snapshot: s.snapshot,
      manifest,
      leaseClient: s.client,
    });

    const owned = await resources.leaseClient.acquireVideoSource(
      {
        asset,
        ownerId: clip.clipId,
        epoch: 1,
        signal: new AbortController().signal,
      },
      {
        snapshot: s.snapshot,
        manifest,
        clipId: clip.clipId,
        sourceStartFrame: 0,
        sourceEndFrame: asset.sourceFrameCount!,
      },
    );

    expect(owned.audioBody).toBe(audioBody);
    await owned.release();
    expect(release).toHaveBeenCalledOnce();
    await resources.close();
  });
  it("rejects decoded video geometry drift before exposing a drawable resource", async () => {
    const s = setup();
    const manifest = buildPublicAssetManifest(s.snapshot);
    const clip = s.snapshot.clips.find(
      (row) =>
        row.assetId ===
        manifest.assets.find((asset) => asset.kind === "video")!.assetId,
    )!;
    const element = document.createElement("video");
    Object.defineProperties(element, {
      videoWidth: { value: 318 },
      videoHeight: { value: 180 },
    });
    const release = vi.fn(async () => undefined);
    vi.mocked(s.client.acquireVideoSource).mockResolvedValueOnce({
      element,
      release,
      geometry: {
        schema: "h3.authoring.media_geometry.v1",
        sourceWidth: 640,
        sourceHeight: 360,
        derivativeWidth: 320,
        derivativeHeight: 180,
      },
    });
    const resources = createVisualCompositionResources({
      snapshot: s.snapshot,
      manifest,
      leaseClient: s.client,
    });
    const scene = {
      ...s.scene,
      layers: [
        {
          ...s.scene.layers[0]!,
          clipId: clip.clipId,
          assetId: clip.assetId,
          trackId: clip.trackId,
        },
      ],
    };
    await resources.prepare(scene, 1, new AbortController().signal);
    await resources.leaseClient.acquireVideoSource(
      {
        asset: manifest.assets.find((row) => row.assetId === clip.assetId)!,
        ownerId: clip.clipId,
        epoch: 1,
        signal: new AbortController().signal,
      },
      {
        snapshot: s.snapshot,
        manifest,
        clipId: clip.clipId,
        sourceStartFrame: 0,
        sourceEndFrame: 24,
      },
    );
    expect(() => resources.read(scene, 1)).toThrow("source_unavailable");
    await resources.close();
    expect(release).toHaveBeenCalledOnce();
  });
  it("retains a rejected video owner for cleanup retry when geometry admission fails", async () => {
    const s = setup();
    const release = vi
      .fn()
      .mockRejectedValueOnce(new Error("lease_busy"))
      .mockResolvedValue(undefined);
    vi.mocked(s.client.acquireVideoSource).mockResolvedValueOnce({
      element: document.createElement("video"),
      release,
      geometry: null,
    });
    const manifest = buildPublicAssetManifest(s.snapshot);
    const resources = createVisualCompositionResources({
      snapshot: s.snapshot,
      manifest,
      leaseClient: s.client,
    });
    await expect(
      resources.leaseClient.acquireVideoSource(
        {
          asset: manifest.assets.find((row) => row.kind === "video")!,
          ownerId: "clip-video",
          epoch: 1,
          signal: new AbortController().signal,
        },
        {
          snapshot: s.snapshot,
          manifest,
          clipId: "clip-video",
          sourceStartFrame: 0,
          sourceEndFrame: 24,
        },
      ),
    ).rejects.toThrow();
    expect(resources.snapshot().videoOwners).toBe(1);
    await resources.close();
    expect(resources.snapshot().videoOwners).toBe(0);
    expect(release).toHaveBeenCalledTimes(2);
  });
  it("bounds a stalled video release while retaining the owner for a truthful retry", async () => {
    vi.useFakeTimers();
    try {
      const s = setup();
      const manifest = buildPublicAssetManifest(s.snapshot);
      const clip = s.snapshot.clips.find(
        (row) =>
          row.assetId ===
          manifest.assets.find((asset) => asset.kind === "video")!.assetId,
      )!;
      let rejectRelease!: (error: Error) => void;
      const release = vi
        .fn<() => Promise<void>>()
        .mockImplementationOnce(
          () =>
            new Promise<void>((_resolve, reject) => {
              rejectRelease = reject;
            }),
        )
        .mockResolvedValueOnce(undefined);
      vi.mocked(s.client.acquireVideoSource).mockResolvedValueOnce({
        element: document.createElement("video"),
        release,
        geometry: {
          schema: "h3.authoring.media_geometry.v1",
          sourceWidth: 640,
          sourceHeight: 360,
          derivativeWidth: 320,
          derivativeHeight: 180,
        },
      });
      const resources = createVisualCompositionResources({
        snapshot: s.snapshot,
        manifest,
        leaseClient: s.client,
      });
      await resources.leaseClient.acquireVideoSource(
        {
          asset: manifest.assets.find((row) => row.assetId === clip.assetId)!,
          ownerId: clip.clipId,
          epoch: 1,
          signal: new AbortController().signal,
        },
        {
          snapshot: s.snapshot,
          manifest,
          clipId: clip.clipId,
          sourceStartFrame: 0,
          sourceEndFrame: 24,
        },
      );

      let settled = false;
      const closing = resources.close();
      void closing.then(
        () => {
          settled = true;
        },
        () => {
          settled = true;
        },
      );
      expect(release).toHaveBeenCalledOnce();
      await vi.advanceTimersByTimeAsync(249);
      expect(settled).toBe(false);
      await vi.advanceTimersByTimeAsync(251);
      await expect(closing).rejects.toThrow("source_unavailable");
      expect(resources.snapshot().videoOwners).toBe(1);

      rejectRelease(new Error("server_timeout"));
      await Promise.resolve();
      await resources.close();
      expect(release).toHaveBeenCalledTimes(2);
      expect(resources.snapshot().videoOwners).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });
  it("preserves source dimensions across a smaller qualified derivative and closes the owner", async () => {
    const s = setup();
    const resources = createVisualCompositionResources({
      snapshot: s.snapshot,
      manifest: buildPublicAssetManifest(s.snapshot),
      leaseClient: s.client,
      decodeImage: s.decodeImage,
    });
    await resources.prepare(s.scene, 1, new AbortController().signal);
    expect(resources.read(s.scene, 1).get("clip-image")).toMatchObject({
      kind: "image",
      nativeWidth: 640,
      nativeHeight: 360,
    });
    expect(resources.snapshot().imageOwners).toBe(1);
    await resources.close();
    expect(resources.snapshot().imageOwners).toBe(0);
    expect(s.bitmap.close).toHaveBeenCalledOnce();
    expect(s.release).toHaveBeenCalledOnce();
  });

  it("rejects missing native geometry before decoding and releases its lease", async () => {
    const s = setup(null);
    const resources = createVisualCompositionResources({
      snapshot: s.snapshot,
      manifest: buildPublicAssetManifest(s.snapshot),
      leaseClient: s.client,
      decodeImage: s.decodeImage,
    });
    await expect(
      resources.prepare(s.scene, 1, new AbortController().signal),
    ).rejects.toThrow("source_unavailable");
    expect(s.decodeImage).not.toHaveBeenCalled();
    expect(s.release).toHaveBeenCalledOnce();
    await resources.close();
  });

  it("keeps failed server release outstanding and retries without closing the bitmap twice", async () => {
    const s = setup();
    const resources = createVisualCompositionResources({
      snapshot: s.snapshot,
      manifest: buildPublicAssetManifest(s.snapshot),
      leaseClient: s.client,
      decodeImage: s.decodeImage,
    });
    await resources.prepare(s.scene, 1, new AbortController().signal);
    s.release.mockRejectedValueOnce(new Error("lease_busy"));
    await expect(resources.close()).rejects.toThrow("source_unavailable");
    expect(resources.snapshot()).toMatchObject({
      imageOwners: 0,
      staticLeases: 1,
    });
    await resources.close();
    expect(resources.snapshot().staticLeases).toBe(0);
    expect(s.bitmap.close).toHaveBeenCalledOnce();
  });

  it("disposes a late non-abortable image decode after cancellation", async () => {
    const s = setup();
    let finish!: (value: typeof s.bitmap) => void;
    s.decodeImage.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const resources = createVisualCompositionResources({
      snapshot: s.snapshot,
      manifest: buildPublicAssetManifest(s.snapshot),
      leaseClient: s.client,
      decodeImage: s.decodeImage,
    });
    const abort = new AbortController();
    const pending = resources.prepare(s.scene, 1, abort.signal);
    await vi.waitFor(() => expect(s.decodeImage).toHaveBeenCalledOnce());
    abort.abort();
    await expect(pending).rejects.toThrow("cancelled");
    finish(s.bitmap);
    await vi.waitFor(() => expect(s.bitmap.close).toHaveBeenCalledOnce());
    expect(resources.snapshot()).toMatchObject({
      imageOwners: 0,
      staticLeases: 0,
      pendingOperations: 0,
    });
    await resources.close();
  });

  it("rejects a decoded image whose measured dimensions contradict bound geometry", async () => {
    const s = setup();
    s.decodeImage.mockResolvedValueOnce({ ...s.bitmap, width: 318 });
    const resources = createVisualCompositionResources({
      snapshot: s.snapshot,
      manifest: buildPublicAssetManifest(s.snapshot),
      leaseClient: s.client,
      decodeImage: s.decodeImage,
    });
    await expect(
      resources.prepare(s.scene, 1, new AbortController().signal),
    ).rejects.toThrow("source_unavailable");
    expect(s.bitmap.close).toHaveBeenCalledOnce();
    expect(s.release).toHaveBeenCalledOnce();
    await resources.close();
  });
});
