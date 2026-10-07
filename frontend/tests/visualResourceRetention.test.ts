import { describe, expect, it, vi } from "vitest";
import fixture from "../../tests/fixtures/m25_10_composition_contract_v1.json";
import {
  decodePublicCompositionSnapshot,
  decodeResolvedScene,
  publicCompositionFingerprint,
  RESOLVED_SCENE_SCHEMA,
} from "../src/contracts/compositionCodec";
import { buildPublicAssetManifest } from "../src/runtime/publicAssetManifest";
import { createVisualCompositionResources } from "../src/runtime/visualCompositionResources";
import type {
  AuthoringMediaLeaseBody,
  AuthoringMediaSourceLease,
  AuthoringMediaSourceLeaseClient,
} from "../src/host/authoringMediaSourceLease";

const fingerprint = (digit: string) => `sha256:${digit.repeat(64)}`;
const signal = () => new AbortController().signal;
const settle = async () => {
  for (let step = 0; step < 16; step++) await Promise.resolve();
};
function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((yes) => {
    resolve = yes;
  });
  return { promise, resolve };
}

function binding(
  kind: "image" | "font",
  revision = 11,
  face: { weight?: number; style?: "normal" | "italic" } = {},
  renameImage = false,
  workspace?: string,
  mixed = false,
) {
  const wire = structuredClone(fixture.snapshot);
  wire.timeline_revision = revision;
  if (workspace !== undefined) wire.workspace_handle = workspace;
  wire.assets[3]!.asset_id = "h3.font.noto_sans.v1";
  wire.clips[3]!.text!.font_asset_id = "h3.font.noto_sans.v1";
  if (renameImage) {
    const index = kind === "image" ? 2 : 3;
    wire.assets[index]!.asset_id += "-replacement";
    if (kind === "image") wire.clips[2]!.asset_id = wire.assets[2]!.asset_id;
    else wire.clips[3]!.text!.font_asset_id = wire.assets[3]!.asset_id;
  }
  if (face.weight !== undefined) wire.clips[3]!.text!.weight = face.weight;
  if (face.style !== undefined) wire.clips[3]!.text!.style = face.style;
  wire.public_fingerprint = publicCompositionFingerprint(wire);
  const snapshot = decodePublicCompositionSnapshot(wire);
  const clip = wire.clips[kind === "image" ? 2 : 3]!;
  const scene = decodeResolvedScene({
    schema: RESOLVED_SCENE_SCHEMA,
    profile_id: snapshot.profileId,
    public_fingerprint: snapshot.publicFingerprint,
    frame: mixed || kind === "font" ? 12 : 0,
    layers: (mixed ? [2, 3] : [kind === "image" ? 2 : 3]).map((index) => {
      const layerClip = wire.clips[index]!;
      return {
        clip_id: layerClip.clip_id,
        asset_id: layerClip.asset_id,
        track_id: layerClip.track_id,
        source_frame: null,
        source_pts: null,
        transition_elapsed_frames: null,
        operation_ids: [
          "SelectSourceRangeV1",
          "CropV1",
          "Transform2DV1",
          "OpacityV1",
          "BlendV1",
          ...(index === 3 ? ["DrawTextV1"] : []),
        ],
        transform: layerClip.transform,
        crop: layerClip.crop,
        opacity_bp: layerClip.opacity_bp,
        blend: layerClip.blend,
        text: layerClip.text,
        effect: layerClip.effect,
      };
    }),
    audio_span: null,
    blockers: [],
  });
  return {
    snapshot,
    scene,
    manifest: buildPublicAssetManifest(snapshot),
    clipId: clip.clip_id,
  };
}

function harness(kind: "image" | "font") {
  const first = binding(kind);
  const geometry = {
    schema: "h3.authoring.media_geometry.v1" as const,
    sourceWidth: 640,
    sourceHeight: 360,
    derivativeWidth: 320,
    derivativeHeight: 180,
  };
  const leases: {
    body: AuthoringMediaLeaseBody;
    open: ReturnType<typeof vi.fn<AuthoringMediaSourceLease["open"]>>;
    release: ReturnType<typeof vi.fn<AuthoringMediaSourceLease["release"]>>;
    derivative: ReturnType<
      typeof vi.fn<NonNullable<AuthoringMediaSourceLease["derivative"]>>
    >;
    adopt: ReturnType<typeof vi.fn>;
    unsubscribe: ReturnType<typeof vi.fn<() => void>>;
    lease: AuthoringMediaSourceLease;
    fail(): void;
  }[] = [];
  let configure = (_lease: (typeof leases)[number], _index: number) => {};
  const create = vi.fn<AuthoringMediaSourceLeaseClient["create"]>(
    async (request) => {
      const callbacks: (() => void)[] = [];
      const bodyKind =
        request.derivativeKind === "packaged_font_face" ? "font" : "image";
      const body: AuthoringMediaLeaseBody = {
        blob: new Blob([new Uint8Array(8)], {
          type: bodyKind === "image" ? "image/png" : "font/ttf",
        }),
        geometry: bodyKind === "image" ? geometry : null,
        receipt: {
          derivativeFingerprint: fingerprint("4"),
          byteCount: 8,
          derivativeKind: request.derivativeKind,
        } as AuthoringMediaLeaseBody["receipt"],
      };
      const release = vi.fn<AuthoringMediaSourceLease["release"]>(
        async () => undefined,
      );
      const derivative = vi.fn<
        NonNullable<AuthoringMediaSourceLease["derivative"]>
      >(() => ({
        fingerprint: fingerprint("4"),
        byteCount: 8,
        kind: request.derivativeKind,
      }));
      const adopt = vi.fn();
      const unsubscribe = vi.fn<() => void>();
      const open = vi.fn<AuthoringMediaSourceLease["open"]>(
        async (): Promise<AuthoringMediaLeaseBody> => result.body,
      );
      const lease = {
        open,
        release,
        derivative,
        adopt,
        subscribeFailure(listener: () => void) {
          callbacks.push(listener);
          return unsubscribe;
        },
      } as unknown as AuthoringMediaSourceLease;
      const result: (typeof leases)[number] = {
        body,
        open,
        release,
        derivative,
        adopt,
        unsubscribe,
        lease,
        fail: () => callbacks.forEach((callback) => callback()),
      };
      configure(result, leases.length);
      leases.push(result);
      // The client intentionally leaves receipt, derivative and lifecycle validation to this unit.
      return lease;
    },
  );
  const images: {
    width: number;
    height: number;
    close: ReturnType<typeof vi.fn>;
  }[] = [];
  const fonts: { family: string; close: ReturnType<typeof vi.fn> }[] = [];
  const decodeImage = vi.fn(async () => {
    const image = { width: 320, height: 180, close: vi.fn() };
    images.push(image);
    return image;
  });
  const loadFont = vi.fn(
    async (
      _bytes: ArrayBuffer,
      family: string,
      _weight: string,
      _style: string,
    ) => {
      const font = { family, close: vi.fn() };
      fonts.push(font);
      return font;
    },
  );
  const failure = vi.fn();
  const nativeAcquire =
    vi.fn<AuthoringMediaSourceLeaseClient["acquireVideoSource"]>();
  const resources = createVisualCompositionResources({
    ...first,
    leaseClient: {
      create,
      acquireVideoSource: nativeAcquire,
      close: vi.fn(),
    } as unknown as AuthoringMediaSourceLeaseClient,
    decodeImage,
    loadFont,
    onFailure: failure,
  });
  return {
    first,
    resources,
    create,
    leases,
    images,
    fonts,
    decodeImage,
    loadFont,
    failure,
    nativeAcquire,
    configure(value: typeof configure) {
      configure = value;
    },
    async prepare(value = first, epoch = 1, inputSignal = signal()) {
      await resources.prepare(value.scene, epoch, inputSignal);
    },
    async next(face: Parameters<typeof binding>[2] = {}) {
      const next = binding(kind, 12, face);
      await resources.replace(next.manifest, next.snapshot);
      return next;
    },
  };
}

describe("public cancellation and exact static decoder inputs", () => {
  it.each(["image", "font"] as const)(
    "counts both live %s lease obligations during the fresh subscription callback",
    async (kind) => {
      const h = harness(kind);
      await h.prepare();
      const next = await h.next();
      const counts: number[] = [];
      h.configure((fresh, index) => {
        if (index === 1)
          vi.spyOn(fresh.lease, "subscribeFailure").mockImplementation(() => {
            counts.push(h.resources.snapshot().staticLeases);
            return fresh.unsubscribe;
          });
      });
      try {
        await h.prepare(next, 2);
        expect(counts).toEqual([2]);
        expect(h.resources.snapshot().staticLeases).toBe(1);
      } finally {
        await h.resources.close();
      }
    },
  );
  it.each(["image", "font"] as const)(
    "refuses a new %s replacement submitted after its public close completed",
    async (kind) => {
      const h = harness(kind);
      await h.prepare();
      await h.resources.close();
      const next = binding(kind, 12);
      await expect(
        h.resources.replace(next.manifest, next.snapshot),
      ).rejects.toThrow("cancelled");
      expect(h.resources.snapshot()).toMatchObject({
        staticLeases: 0,
        pendingOperations: 0,
      });
    },
  );
  it.each(["image", "font"] as const)(
    "does not adopt %s authority when its held owner fails during replacement creation",
    async (kind) => {
      const h = harness(kind);
      await h.prepare();
      const next = await h.next();
      const created = deferred<void>();
      const released = deferred<void>();
      const acquire = h.create.getMockImplementation()!;
      h.create.mockImplementationOnce(async (...args) => {
        const lease = await acquire(...args);
        await created.promise;
        return lease;
      });
      h.leases[0]!.release.mockImplementationOnce(() => released.promise);
      const preparing = h.prepare(next, 2).catch(() => undefined);
      await vi.waitFor(() => expect(h.leases).toHaveLength(2));
      h.leases[0]!.fail();
      created.resolve();
      try {
        await settle();
        expect(h.leases[1]!.adopt).not.toHaveBeenCalled();
        expect(h.leases[1]!.open).not.toHaveBeenCalled();
        released.resolve();
        await preparing;
        expect(h.leases[1]!.adopt).not.toHaveBeenCalled();
      } finally {
        created.resolve();
        released.resolve();
        await preparing;
        await h.resources.close();
      }
    },
  );
  it.each(["image", "font"] as const)(
    "waits for local %s revocation started by a reentrant unsubscribe before acquiring replacement",
    async (kind) => {
      const h = harness(kind);
      await h.prepare();
      const next = await h.next();
      const gate = deferred<void>();
      h.leases[0]!.release.mockImplementationOnce(() => gate.promise);
      let pending: Promise<unknown> | undefined;
      h.leases[0]!.unsubscribe.mockImplementationOnce(() => {
        pending = h.prepare(next, 2).catch(() => undefined);
      });
      h.leases[0]!.fail();
      try {
        await vi.waitFor(() =>
          expect(h.leases[0]!.release).toHaveBeenCalledOnce(),
        );
        await settle();
        expect(h.create).toHaveBeenCalledOnce();
      } finally {
        gate.resolve();
        await pending;
        await h.resources.close();
      }
    },
  );
  it.each(["image", "font"] as const)(
    "does not decode a %s body whose local failure arrives while its open is held",
    async (kind) => {
      const h = harness(kind);
      const opening = deferred<AuthoringMediaLeaseBody>();
      const releasing = deferred<void>();
      h.configure((lease) => {
        lease.open.mockImplementationOnce(() => opening.promise);
        lease.release.mockImplementationOnce(() => releasing.promise);
      });
      const pending = h.prepare().then(
        () => "success",
        () => "failure",
      );
      await vi.waitFor(() => expect(h.leases[0]!.open).toHaveBeenCalledOnce());
      h.leases[0]!.fail();
      opening.resolve(h.leases[0]!.body);
      await settle();
      try {
        expect(
          kind === "image" ? h.decodeImage : h.loadFont,
        ).not.toHaveBeenCalled();
      } finally {
        releasing.resolve();
        await pending;
        await h.resources.close();
      }
    },
  );
  it.each(["image", "font"] as const)(
    "enforces %s asset identity when handed matching derivative bytes under a renamed asset",
    async (kind) => {
      const h = harness(kind);
      await h.prepare();
      const next = binding(kind, 12, {}, true);
      await h.resources.replace(next.manifest, next.snapshot);
      if (kind === "font") {
        await expect(h.prepare(next, 2)).rejects.toThrow("source_unavailable");
        expect(h.create).toHaveBeenCalledOnce();
        expect(h.loadFont).toHaveBeenCalledOnce();
        await h.resources.close();
        return;
      }
      await h.prepare(next, 2);
      expect(
        kind === "image" ? h.decodeImage : h.loadFont,
      ).toHaveBeenCalledTimes(2);
      expect(h.leases[1]!.adopt).not.toHaveBeenCalled();
      await h.resources.close();
    },
  );
  it.each(["image", "font"] as const)(
    "joins disposal before opening a mismatched fresh %s derivative",
    async (kind) => {
      const h = harness(kind);
      await h.prepare();
      const next = await h.next();
      const gate = deferred<void>();
      h.leases[0]!.release.mockImplementationOnce(() => gate.promise);
      h.configure((lease, index) => {
        if (index === 1)
          lease.derivative.mockReturnValue({
            fingerprint: fingerprint("5"),
            byteCount: 8,
            kind: kind === "image" ? "image_proxy" : "packaged_font_face",
          });
      });
      const pending = h.prepare(next, 2);
      try {
        await vi.waitFor(() =>
          expect(h.leases[0]!.release).toHaveBeenCalledOnce(),
        );
        await settle();
        expect(h.leases[1]!.open).not.toHaveBeenCalled();
      } finally {
        gate.resolve();
        await pending;
        await h.resources.close();
      }
    },
  );
  it("does not reauthorize a second origin after fresh retained authority reports failure synchronously", async () => {
    const h = harness("image");
    const first = binding("image", 11, {}, false, undefined, true);
    await h.prepare(first);
    const next = binding("image", 12, {}, false, undefined, true);
    await h.resources.replace(next.manifest, next.snapshot);
    h.configure((lease, index) => {
      if (index === 2)
        Object.assign(lease.lease, {
          subscribeFailure: (listener: () => void) => {
            listener();
            return lease.unsubscribe;
          },
        });
    });
    await expect(h.prepare(next, 2)).rejects.toThrow("source_unavailable");
    expect(h.create).toHaveBeenCalledTimes(3);
    await h.resources.close();
  });
  it.each(["image", "font"] as const)(
    "does not subscribe an obsolete %s owner when the handed derivative callback closes resources",
    async (kind) => {
      const h = harness(kind);
      await h.prepare();
      const next = await h.next();
      let closing: Promise<void> | undefined;
      let subscribe!: ReturnType<typeof vi.spyOn>;
      h.configure((lease, index) => {
        if (index !== 1) return;
        subscribe = vi.spyOn(lease.lease, "subscribeFailure");
        lease.derivative.mockImplementationOnce(() => {
          closing = h.resources.close();
          return {
            fingerprint: fingerprint("5"),
            byteCount: 8,
            kind: kind === "image" ? "image_proxy" : "packaged_font_face",
          };
        });
      });
      await expect(h.prepare(next, 2)).rejects.toThrow();
      expect(subscribe).not.toHaveBeenCalled();
      await closing;
      await h.resources.close();
    },
  );
  it("does not acquire the next static origin after its first decoder fails", async () => {
    const h = harness("image");
    const mixed = binding("image", 11, {}, false, undefined, true);
    h.decodeImage.mockRejectedValueOnce(new Error("source_unavailable"));
    await expect(h.prepare(mixed)).rejects.toThrow("source_unavailable");
    expect(h.create).toHaveBeenCalledOnce();
    expect(h.loadFont).not.toHaveBeenCalled();
    await h.resources.close();
  });
  it("joins image retirement discovered while font retirement is held before creating replacement authority", async () => {
    const h = harness("image");
    const first = binding("image", 11, {}, false, undefined, true);
    await h.prepare(first);
    const next = binding("image", 12, { weight: 400 }, false, undefined, true);
    await h.resources.replace(next.manifest, next.snapshot);
    const fontGate = deferred<void>();
    const imageGate = deferred<void>();
    h.leases[1]!.release.mockImplementationOnce(() => fontGate.promise);
    h.leases[0]!.release.mockImplementationOnce(() => imageGate.promise);
    let settled = false;
    const pending = h.prepare(next, 2).then(
      () => {
        settled = true;
        return "success";
      },
      () => {
        settled = true;
        return "failure";
      },
    );
    try {
      await vi.waitFor(() =>
        expect(h.leases[1]!.release).toHaveBeenCalledOnce(),
      );
      h.leases[0]!.fail();
      await vi.waitFor(() =>
        expect(h.leases[0]!.release).toHaveBeenCalledOnce(),
      );
      fontGate.resolve();
      await settle();
      expect(settled).toBe(false);
      expect(h.create).toHaveBeenCalledTimes(2);
    } finally {
      fontGate.resolve();
      imageGate.resolve();
      await pending;
      await h.resources.close();
    }
  });

  it.each(["image", "font"] as const)(
    "reports unavailable %s decoding after releasing its authority",
    async (kind) => {
      const h = harness(kind);
      const error = new Error("decoder_failure");
      if (kind === "image") h.decodeImage.mockRejectedValueOnce(error);
      else h.loadFont.mockRejectedValueOnce(error);
      await expect(h.prepare()).rejects.toThrow("source_unavailable");
      expect(h.leases[0]!.release).toHaveBeenCalledOnce();
      await h.resources.close();
    },
  );
  it.each(["image", "font"] as const)(
    "does not subscribe fresh %s authority after adoption revokes the held owner",
    async (kind) => {
      const h = harness(kind);
      await h.prepare();
      const next = await h.next();
      let subscribe!: ReturnType<typeof vi.spyOn>;
      h.configure((lease, index) => {
        if (index !== 1) return;
        subscribe = vi.spyOn(lease.lease, "subscribeFailure");
        lease.adopt.mockImplementationOnce(() => h.leases[0]!.fail());
      });
      await expect(h.prepare(next, 2)).rejects.toThrow();
      expect(subscribe).not.toHaveBeenCalled();
      expect(h.leases[1]!.release).toHaveBeenCalledOnce();
      await h.resources.close();
    },
  );
  it.each(["image", "font"] as const)(
    "refuses an independently changed %s workspace before replacing authority",
    async (kind) => {
      const h = harness(kind);
      await h.prepare();
      const next = binding(kind, 12, {}, false, "another-workspace");
      await expect(
        h.resources.replace(next.manifest, next.snapshot),
      ).rejects.toThrow("contract_mismatch");
      expect(h.create).toHaveBeenCalledOnce();
      expect(
        h.resources.read(h.first.scene, 1).get(h.first.clipId),
      ).toBeDefined();
      await h.resources.close();
    },
  );
  it.each(["image", "font"] as const)(
    "aborts a held %s decoder so replacement need not wait for its result",
    async (kind) => {
      const h = harness(kind);
      const decoded = deferred<any>();
      if (kind === "image") h.decodeImage.mockReturnValueOnce(decoded.promise);
      else h.loadFont.mockReturnValueOnce(decoded.promise);
      const preparing = h.prepare().then(
        () => true,
        () => false,
      );
      await settle();
      expect(
        h.decodeImage.mock.calls.length + h.loadFont.mock.calls.length,
      ).toBe(1);
      const next = binding(kind, 12);
      let settled = false;
      const replacing = h.resources
        .replace(next.manifest, next.snapshot)
        .then(() => {
          settled = true;
        });
      await settle();
      try {
        expect(settled).toBe(true);
      } finally {
        decoded.resolve(
          kind === "image"
            ? { width: 320, height: 180, close: vi.fn() }
            : { family: h.loadFont.mock.calls[0]![1], close: vi.fn() },
        );
        await Promise.all([preparing, replacing]);
        await h.resources.close();
      }
    },
  );
  it("does not hand font bytes to the loader after the signal was aborted while reading them", async () => {
    const h = harness("font");
    const bytes = deferred<ArrayBuffer>();
    h.configure((lease) =>
      vi.spyOn(lease.body.blob, "arrayBuffer").mockReturnValue(bytes.promise),
    );
    const controller = new AbortController();
    const preparing = h.prepare(h.first, 1, controller.signal).then(
      () => true,
      () => false,
    );
    await settle();
    controller.abort();
    bytes.resolve(new ArrayBuffer(8));
    await expect(preparing).resolves.toBe(false);
    expect(h.loadFont).not.toHaveBeenCalled();
    await h.resources.close();
  });
  it("refuses oversized image bytes before invoking the decoder", async () => {
    const h = harness("image");
    h.configure((lease) => {
      lease.body = {
        ...lease.body,
        blob: new Blob([new Uint8Array(16 * 1024 * 1024 + 1)], {
          type: "image/png",
        }),
      };
    });
    await expect(h.prepare()).rejects.toThrow("resource_limit");
    expect(h.decodeImage).not.toHaveBeenCalled();
    await h.resources.close();
  });
  it.each(["image", "font"] as const)(
    "reopens a %s body whose held and fresh identities both name the wrong derivative kind",
    async (kind) => {
      const h = harness(kind);
      h.configure((lease) => {
        lease.body = {
          ...lease.body,
          receipt: {
            ...lease.body.receipt,
            derivativeKind: "audio_preview",
          } as AuthoringMediaLeaseBody["receipt"],
        };
        lease.derivative.mockReturnValue({
          fingerprint: fingerprint("4"),
          byteCount: 8,
          kind: "audio_preview",
        });
      });
      await h.prepare();
      const next = await h.next();
      await h.prepare(next, 2);
      expect(h.leases[1]!.adopt).not.toHaveBeenCalled();
      expect(h.leases[1]!.open).toHaveBeenCalledOnce();
      expect(
        h.decodeImage.mock.calls.length + h.loadFont.mock.calls.length,
      ).toBe(2);
      await h.resources.close();
    },
  );
  it.each(["image", "font"] as const)(
    "joins late %s unbound authority release before reporting cancellation",
    async (kind) => {
      const h = harness(kind);
      const created = deferred<void>();
      const released = deferred<void>();
      const acquire = h.create.getMockImplementation()!;
      h.configure((lease) => lease.release.mockReturnValue(released.promise));
      h.create.mockImplementationOnce(async (...args) => {
        const lease = await acquire(...args);
        await created.promise;
        return lease;
      });
      const abort = new AbortController();
      let settled = false;
      const prepared = h.prepare(h.first, 1, abort.signal).then(
        () => {
          settled = true;
          return true;
        },
        () => {
          settled = true;
          return false;
        },
      );
      await vi.waitFor(() => expect(h.leases).toHaveLength(1));
      abort.abort();
      created.resolve();
      await vi.waitFor(() =>
        expect(h.leases[0]!.release).toHaveBeenCalledOnce(),
      );
      await settle();
      try {
        expect(settled).toBe(false);
        expect(h.resources.snapshot().staticLeases).toBe(1);
      } finally {
        released.resolve();
        expect(await prepared).toBe(false);
        await h.resources.close();
      }
      expect(h.resources.snapshot().staticLeases).toBe(0);
    },
  );

  it.each(["image", "font"] as const)(
    "joins old %s preparation before accepting only the newest replacement",
    async (kind) => {
      const h = harness(kind);
      const created = deferred<void>();
      const acquire = h.create.getMockImplementation()!;
      h.create.mockImplementationOnce(async (...args) => {
        const lease = await acquire(...args);
        await created.promise;
        return lease;
      });
      const prepared = h.prepare().catch(() => undefined);
      await vi.waitFor(() => expect(h.leases).toHaveLength(1));
      const first = binding(kind, 12);
      const latest = binding(kind, 13);
      const states: string[] = [];
      const obsolete = h.resources.replace(first.manifest, first.snapshot).then(
        () => states.push("obsolete accepted"),
        () => states.push("obsolete refused"),
      );
      const newest = h.resources.replace(latest.manifest, latest.snapshot).then(
        () => states.push("newest accepted"),
        () => states.push("newest refused"),
      );
      await settle();
      try {
        expect(states).toEqual([]);
      } finally {
        created.resolve();
        await Promise.all([prepared, obsolete, newest]);
      }
      expect(states).toEqual(["obsolete refused", "newest accepted"]);
      await h.prepare(latest, 3);
      expect(
        h.resources.read(latest.scene, 3).get(latest.clipId),
      ).toBeDefined();
      await h.resources.close();
      expect(h.resources.snapshot().staticLeases).toBe(0);
    },
  );

  it.each(["image", "font"] as const)(
    "does not decode %s bytes returned after the public signal was cancelled",
    async (kind) => {
      const h = harness(kind);
      const opened = deferred<AuthoringMediaLeaseBody>();
      h.configure((lease) => lease.open.mockReturnValue(opened.promise));
      const abort = new AbortController();
      const prepared = h.prepare(h.first, 1, abort.signal).then(
        () => true,
        () => false,
      );
      await vi.waitFor(() => expect(h.leases[0]!.open).toHaveBeenCalledOnce());
      abort.abort();
      opened.resolve(h.leases[0]!.body);
      try {
        expect(await prepared).toBe(false);
        expect(h.decodeImage).not.toHaveBeenCalled();
        expect(h.loadFont).not.toHaveBeenCalled();
      } finally {
        await h.resources.close();
      }
      expect(h.resources.snapshot().staticLeases).toBe(0);
    },
  );

  it.each(["image", "font"] as const)(
    "reports late %s authority cleanup failure to the waiting close caller",
    async (kind) => {
      const h = harness(kind);
      const created = deferred<void>();
      const acquire = h.create.getMockImplementation()!;
      h.configure((lease) =>
        lease.release.mockRejectedValue(
          new Error("controlled release refusal"),
        ),
      );
      h.create.mockImplementationOnce(async (...args) => {
        const lease = await acquire(...args);
        await created.promise;
        return lease;
      });
      const prepared = h.prepare().then(
        () => true,
        () => false,
      );
      await vi.waitFor(() => expect(h.leases).toHaveLength(1));
      const closing = h.resources.close().then(
        () => true,
        () => false,
      );
      created.resolve();
      try {
        expect(await prepared).toBe(false);
        expect(await closing).toBe(false);
        expect(h.resources.snapshot().staticLeases).toBe(1);
        expect(h.failure).toHaveBeenCalledOnce();
      } finally {
        h.leases[0]!.release.mockResolvedValue(undefined);
        await h.resources.close();
      }
      expect(h.resources.snapshot().staticLeases).toBe(0);
    },
  );

  it.each(["image", "font"] as const)(
    "keeps old %s authority when adoption aborts the public replacement signal",
    async (kind) => {
      const h = harness(kind);
      await h.prepare();
      const next = await h.next();
      const abort = new AbortController();
      h.configure((lease, index) => {
        if (index === 1) lease.adopt.mockImplementation(() => abort.abort());
      });
      try {
        await expect(h.prepare(next, 2, abort.signal)).rejects.toThrow();
        expect(h.leases[0]!.release).not.toHaveBeenCalled();
        expect(h.leases[1]!.release).toHaveBeenCalledOnce();
        expect(h.leases[0]!.unsubscribe).not.toHaveBeenCalled();
      } finally {
        await h.resources.close();
      }
      expect(h.resources.snapshot().staticLeases).toBe(0);
    },
  );

  it("hands the complete font body to the decoder as bytes", async () => {
    const h = harness("font");
    h.loadFont.mockImplementation(async (bytes, family) => {
      expect(bytes).toBeInstanceOf(ArrayBuffer);
      expect(Array.from(new Uint8Array(bytes))).toEqual(Array(8).fill(0));
      return { family, close: vi.fn() };
    });
    try {
      await h.prepare();
      expect(h.loadFont).toHaveBeenCalledOnce();
      expect(
        h.resources.read(h.first.scene, 1).get(h.first.clipId),
      ).toMatchObject({ kind: "font" });
    } finally {
      await h.resources.close();
    }
  });

  it("refuses an oversized image before asking the image decoder", async () => {
    const h = harness("image");
    h.configure((lease) => {
      lease.body = {
        ...lease.body,
        geometry: {
          schema: "h3.authoring.media_geometry.v1",
          sourceWidth: 8192,
          sourceHeight: 1024,
          derivativeWidth: 8192,
          derivativeHeight: 1024,
        },
      };
    });
    try {
      await expect(h.prepare()).rejects.toThrow();
      expect(h.decodeImage).not.toHaveBeenCalled();
    } finally {
      await h.resources.close();
    }
  });

  it.each(["image", "font"] as const)(
    "ignores the old %s callback after incompatible binding replacement",
    async (kind) => {
      const h = harness(kind);
      await h.prepare();
      const next = binding(
        kind,
        12,
        kind === "font" ? { weight: 400 } : {},
        kind === "image",
      );
      await h.resources.replace(next.manifest, next.snapshot);
      await h.prepare(next, 2);
      try {
        expect(h.leases).toHaveLength(2);
        expect(h.leases[0]!.release).toHaveBeenCalledOnce();
        if (kind === "font") {
          expect(h.fonts[0]!.close).toHaveBeenCalledOnce();
          expect(h.loadFont).toHaveBeenCalledTimes(2);
        } else {
          expect(h.images[0]!.close).toHaveBeenCalledOnce();
          expect(h.decodeImage).toHaveBeenCalledTimes(2);
        }
        h.leases[0]!.fail();
        await settle();
        expect(h.failure).not.toHaveBeenCalled();
        expect(h.resources.read(next.scene, 2).get(next.clipId)).toBeDefined();
        expect(h.resources.snapshot().staticLeases).toBe(1);
      } finally {
        await h.resources.close();
      }
      expect(h.resources.snapshot().staticLeases).toBe(0);
    },
  );
});

describe("visual video wrapper admission through a permissive native owner", () => {
  function videoHarness() {
    const h = harness("image");
    const element = document.createElement("video");
    Object.defineProperties(element, {
      videoWidth: { value: 320, configurable: true },
      videoHeight: { value: 180, configurable: true },
    });
    const rebind = vi.fn(async () => true);
    const release = vi.fn(async (): Promise<void> => undefined);
    h.nativeAcquire.mockResolvedValue({
      element,
      rebind,
      release,
      geometry: {
        schema: "h3.authoring.media_geometry.v1",
        sourceWidth: 640,
        sourceHeight: 360,
        derivativeWidth: 320,
        derivativeHeight: 180,
      },
    });
    const asset = h.first.manifest.assets.find(
      (asset) => asset.assetId === "vid-primary",
    )!;
    const request = { asset, ownerId: "clip-main", epoch: 1, signal: signal() };
    const context = {
      ...h.first,
      clipId: "clip-main",
      sourceStartFrame: 0,
      sourceEndFrame: asset.sourceFrameCount!,
    };
    return {
      ...h,
      element,
      rebind,
      release,
      request,
      context,
      acquire: () =>
        h.resources.leaseClient.acquireVideoSource(request, context),
    };
  }

  it("reports a video cleanup deadline even when the independent acknowledgement removes its owner before readback", async () => {
    vi.useFakeTimers();
    const h = videoHarness();
    const released = deferred<void>();
    try {
      await h.acquire();
      h.release.mockImplementationOnce(() => released.promise);
      const closing = h.resources.close().then(
        () => true,
        () => false,
      );
      await settle();
      expect(h.release).toHaveBeenCalledOnce();
      vi.advanceTimersByTime(500);
      released.resolve();
      expect(await closing).toBe(false);
      expect(h.resources.snapshot().videoOwners).toBe(0);
    } finally {
      released.resolve();
      await h.resources.close();
      vi.useRealTimers();
    }
  });

  it.each(["before", "during"] as const)(
    "refuses native rebind when close holds its release %s reauthorization",
    async (phase) => {
      const h = videoHarness();
      const owner = await h.acquire();
      const releasing = deferred<void>();
      const rebinding = deferred<boolean>();
      h.release.mockImplementationOnce(() => releasing.promise);
      h.rebind.mockImplementationOnce(() => rebinding.promise);
      let pending: Promise<boolean> | undefined;
      if (phase === "during") {
        pending = owner.rebind!({ ...h.request, epoch: 2 }, h.context);
        await vi.waitFor(() => expect(h.rebind).toHaveBeenCalledOnce());
      }
      const closing = h.resources.close();
      await vi.waitFor(() => expect(h.release).toHaveBeenCalledOnce());
      try {
        if (phase === "before") {
          rebinding.resolve(true);
          await expect(
            owner.rebind!({ ...h.request, epoch: 2 }, h.context),
          ).resolves.toBe(false);
          expect(h.rebind).not.toHaveBeenCalled();
        } else {
          rebinding.resolve(true);
          await expect(pending).resolves.toBe(false);
        }
      } finally {
        rebinding.resolve(true);
        releasing.resolve();
        await pending;
        await closing;
      }
    },
  );

  it.each([
    "closed",
    "retired",
    "other owner",
    "refused",
    "aborted",
    "width",
    "height",
  ])(
    "refuses independently supplied %s during reauthorization",
    async (field) => {
      const h = videoHarness();
      const owner = await h.acquire();
      if (field === "closed") await h.resources.close();
      if (field === "retired") await owner.release();
      if (field === "refused") h.rebind.mockResolvedValueOnce(false);
      if (field === "width")
        Object.defineProperty(h.element, "videoWidth", { value: 319 });
      if (field === "height")
        Object.defineProperty(h.element, "videoHeight", { value: 179 });
      const abort = new AbortController();
      if (field === "aborted") abort.abort();
      await expect(
        owner.rebind!(
          {
            ...h.request,
            epoch: 2,
            ownerId: field === "other owner" ? "clip-other" : "clip-main",
            signal: abort.signal,
          },
          h.context,
        ),
      ).resolves.toBe(false);
      await h.resources.close();
    },
  );

  it("joins a successful native rebind before exposing visual readiness", async () => {
    const h = videoHarness();
    const owner = await h.acquire();
    const gate = deferred<boolean>();
    h.rebind.mockImplementationOnce(() => gate.promise);
    let settled = false;
    const pending = owner.rebind!({ ...h.request, epoch: 2 }, h.context).then(
      (kept) => {
        settled = true;
        return kept;
      },
    );
    await vi.waitFor(() => expect(h.rebind).toHaveBeenCalledOnce());
    await settle();
    expect(settled).toBe(false);
    gate.resolve(true);
    await expect(pending).resolves.toBe(true);
    await h.resources.close();
  });
});

describe.each(["image", "font"] as const)(
  "independent %s resource retention",
  (kind) => {
    it("keeps fresh authority counters and subscriptions exact through final close", async () => {
      const h = harness(kind);
      await h.prepare();
      expect(h.resources.snapshot()).toMatchObject({
        staticLeases: 1,
        blobBytes: 8,
        pendingOperations: 0,
      });
      const next = await h.next();
      await h.prepare(next, 2);
      expect(h.create.mock.calls[1]![0]).toMatchObject({
        publicFingerprint: next.snapshot.publicFingerprint,
        manifestFingerprint: next.manifest.manifestFingerprint,
        timelineRevision: next.snapshot.timelineRevision,
      });
      expect(h.resources.snapshot()).toMatchObject({
        staticLeases: 1,
        blobBytes: 8,
        pendingOperations: 0,
      });
      await h.resources.close();
      for (const lease of h.leases)
        expect(lease.unsubscribe).toHaveBeenCalledOnce();
      expect(h.resources.snapshot()).toEqual({
        imageOwners: 0,
        fontOwners: 0,
        videoOwners: 0,
        staticLeases: 0,
        blobBytes: 0,
        pendingOperations: 0,
      });
    });

    it("revokes the active read epoch even when replacement hands the same binding back", async () => {
      const h = harness(kind);
      await h.prepare();
      await h.resources.replace(h.first.manifest, h.first.snapshot);
      expect(() => h.resources.read(h.first.scene, 1)).toThrow(
        "source_unavailable",
      );
      await h.prepare(h.first, 2);
      expect(h.resources.read(h.first.scene, 2).size).toBe(1);
      await h.resources.close();
    });

    it("joins one in-progress retirement during concurrent close", async () => {
      const h = harness(kind);
      await h.prepare();
      const next = await h.next();
      const gate = deferred<void>();
      h.leases[0]!.release.mockImplementationOnce(() => gate.promise);
      const pending = h.prepare(next, 2).catch(() => undefined);
      await vi.waitFor(() =>
        expect(h.leases[0]!.release).toHaveBeenCalledOnce(),
      );
      const closing = h.resources.close();
      await settle();
      expect(h.leases[0]!.release).toHaveBeenCalledOnce();
      gate.resolve();
      await pending;
      await closing;
      expect(h.resources.snapshot().staticLeases).toBe(0);
    });

    it("joins successful cleanup before reporting a preparation refusal", async () => {
      const h = harness(kind);
      const gate = deferred<void>();
      h.configure((lease) => {
        lease.body = {
          ...lease.body,
          blob: new Blob([new Uint8Array(8)], { type: "wrong/type" }),
        };
        lease.release.mockImplementationOnce(() => gate.promise);
      });
      let settled = false;
      const pending = h.prepare().then(
        () => {
          settled = true;
          return "success";
        },
        () => {
          settled = true;
          return "failure";
        },
      );
      await vi.waitFor(() =>
        expect(h.leases[0]!.release).toHaveBeenCalledOnce(),
      );
      await settle();
      expect(settled).toBe(false);
      gate.resolve();
      expect(await pending).toBe("failure");
      await h.resources.close();
      expect(h.resources.snapshot().staticLeases).toBe(0);
    });

    it.each(["adopt", "subscribe"])(
      "does not publish reentrant %s failure as successful retention",
      async (phase) => {
        const h = harness(kind);
        await h.prepare();
        const next = await h.next();
        let closing: Promise<void> | undefined;
        h.configure((lease, index) => {
          if (index !== 1) return;
          if (phase === "adopt")
            lease.adopt.mockImplementationOnce(() => {
              closing = h.resources.close();
            });
          else
            Object.assign(lease.lease, {
              subscribeFailure: (listener: () => void) => {
                listener();
                return lease.unsubscribe;
              },
            });
        });
        await expect(h.prepare(next, 2)).rejects.toThrow();
        if (closing) await closing;
        else await h.resources.close();
        expect(h.resources.snapshot().staticLeases).toBe(0);
        expect(() => h.resources.read(next.scene, 2)).toThrow(
          "source_unavailable",
        );
        expect(h.leases[1]!.release).toHaveBeenCalled();
        expect(h.leases[1]!.unsubscribe).toHaveBeenCalledTimes(
          phase === "subscribe" ? 1 : 0,
        );
      },
    );
    it.each(["create", "open", "decode"])(
      "fences a successful %s completion after close",
      async (phase) => {
        const h = harness(kind);
        const gate = deferred<void>();
        let entered = false;
        if (phase === "create") {
          const original = h.create.getMockImplementation()!;
          h.create.mockImplementationOnce(async (...args) => {
            const lease = await original(...args);
            entered = true;
            await gate.promise;
            return lease;
          });
        } else if (phase === "open") {
          h.configure((lease) =>
            lease.open.mockImplementationOnce(async () => {
              entered = true;
              await gate.promise;
              return lease.body;
            }),
          );
        } else if (kind === "image") {
          const original = h.decodeImage.getMockImplementation()!;
          h.decodeImage.mockImplementationOnce(async () => {
            entered = true;
            await gate.promise;
            return original();
          });
        } else {
          const original = h.loadFont.getMockImplementation()!;
          h.loadFont.mockImplementationOnce(async (...args) => {
            entered = true;
            await gate.promise;
            return original(...args);
          });
        }
        const pending = h.prepare().then(
          () => "success",
          () => "cancelled",
        );
        await vi.waitFor(() => expect(entered).toBe(true));
        const closing = h.resources.close();
        await settle();
        gate.resolve();
        expect(await pending).toBe("cancelled");
        await closing;
        await settle();
        expect(() => h.resources.read(h.first.scene, 1)).toThrow(
          "source_unavailable",
        );
        for (const lease of h.leases) expect(lease.release).toHaveBeenCalled();
        for (const local of [...h.images, ...h.fonts])
          expect(local.close).toHaveBeenCalledOnce();
      },
    );

    it("does not expose new authority while the retired lease release is held", async () => {
      const h = harness(kind);
      await h.prepare();
      const next = await h.next();
      const gate = deferred<void>();
      h.leases[0]!.release.mockImplementationOnce(() => gate.promise);
      let settled = false;
      const preparing = h.prepare(next, 2).then(() => {
        settled = true;
      });
      await vi.waitFor(() =>
        expect(h.leases[0]!.release).toHaveBeenCalledOnce(),
      );
      await settle();
      expect(settled).toBe(false);
      expect(() => h.resources.read(next.scene, 2)).toThrow(
        "source_unavailable",
      );
      gate.resolve();
      await preparing;
      expect(h.resources.read(next.scene, 2).size).toBe(1);
      await h.resources.close();
    });
    it.each(["fingerprint", "byteCount"])(
      "does not retain an unverified opened %s even when fresh authority repeats it",
      async (field) => {
        const h = harness(kind);
        h.configure((lease) => {
          const invalidFingerprint =
            field === "fingerprint" ? "invalid-fingerprint" : fingerprint("4");
          const invalidCount = field === "byteCount" ? 9 : 8;
          lease.body = {
            ...lease.body,
            receipt: {
              ...lease.body.receipt,
              derivativeFingerprint: invalidFingerprint,
              byteCount: invalidCount,
            },
          };
          lease.derivative.mockReturnValue({
            fingerprint: invalidFingerprint,
            byteCount: invalidCount,
            kind: lease.body.receipt.derivativeKind,
          });
        });
        await h.prepare();
        const next = await h.next();
        await h.prepare(next, 2);
        expect(h.leases[1]!.adopt).not.toHaveBeenCalled();
        expect(h.leases[1]!.open).toHaveBeenCalledOnce();
        expect(
          kind === "image" ? h.decodeImage : h.loadFont,
        ).toHaveBeenCalledTimes(2);
        await h.resources.close();
      },
    );
    it("adopts matching fresh authority without opening, decoding or disposing and fences retired callbacks", async () => {
      const h = harness(kind);
      await h.prepare();
      const before = h.resources.read(h.first.scene, 1).get(h.first.clipId);
      const next = await h.next();
      await h.prepare(next, 2);
      expect(h.create).toHaveBeenCalledTimes(2);
      expect(h.leases[1]!.adopt).toHaveBeenCalledOnce();
      expect(h.leases[1]!.open).not.toHaveBeenCalled();
      expect(
        h.decodeImage.mock.calls.length + h.loadFont.mock.calls.length,
      ).toBe(1);
      expect(h.resources.read(next.scene, 2).get(next.clipId)).toBe(before);
      expect(h.leases[0]!.release).toHaveBeenCalledOnce();
      expect(h.leases[0]!.unsubscribe).toHaveBeenCalledOnce();
      h.leases[0]!.fail();
      expect(h.failure).not.toHaveBeenCalled();
      expect(h.resources.read(next.scene, 2).get(next.clipId)).toBe(before);
      h.leases[1]!.fail();
      expect(h.failure).toHaveBeenCalledOnce();
      expect(() => h.resources.read(next.scene, 2)).toThrow(
        "source_unavailable",
      );
      await h.resources.close();
      expect(h.resources.snapshot()).toMatchObject({
        staticLeases: 0,
        imageOwners: 0,
        fontOwners: 0,
        blobBytes: 0,
      });
      expect([...h.images, ...h.fonts][0]!.close).toHaveBeenCalledOnce();
    });

    it.each([
      "fingerprint",
      "byteCount",
      "kind",
      "missing derivative",
      "missing adopt",
    ])("reopens rather than adopting a mismatched fresh %s", async (field) => {
      const h = harness(kind);
      await h.prepare();
      h.configure((lease, index) => {
        if (index !== 1) return;
        if (field === "missing derivative")
          (lease.lease as { derivative?: unknown }).derivative = undefined;
        else if (field === "missing adopt")
          (lease.lease as { adopt?: unknown }).adopt = undefined;
        else
          lease.derivative.mockReturnValue({
            fingerprint:
              field === "fingerprint" ? fingerprint("9") : fingerprint("4"),
            byteCount: field === "byteCount" ? 9 : 8,
            kind:
              field === "kind"
                ? "thumbnail"
                : lease.body.receipt.derivativeKind,
          });
      });
      const next = await h.next();
      await h.prepare(next, 2);
      expect(h.leases[1]!.adopt).not.toHaveBeenCalled();
      expect(h.leases[1]!.open).toHaveBeenCalledOnce();
      expect(
        h.decodeImage.mock.calls.length + h.loadFont.mock.calls.length,
      ).toBe(2);
      expect([...h.images, ...h.fonts][0]!.close).toHaveBeenCalledOnce();
      await h.resources.close();
    });

    it.each(["missing", "malformed fingerprint", "byte mismatch"])(
      "does not retain a body with a %s receipt",
      async (field) => {
        const h = harness(kind);
        h.configure((lease, index) => {
          if (index !== 0) return;
          lease.body = {
            ...lease.body,
            receipt:
              field === "missing"
                ? (undefined as unknown as AuthoringMediaLeaseBody["receipt"])
                : {
                    ...lease.body.receipt,
                    derivativeFingerprint:
                      field === "malformed fingerprint"
                        ? "unverified"
                        : fingerprint("4"),
                    byteCount: field === "byte mismatch" ? 9 : 8,
                  },
          };
        });
        await h.prepare();
        const next = await h.next();
        await h.prepare(next, 2);
        expect(h.leases[1]!.adopt).not.toHaveBeenCalled();
        expect(h.leases[1]!.open).toHaveBeenCalledOnce();
        expect(
          h.decodeImage.mock.calls.length + h.loadFont.mock.calls.length,
        ).toBe(2);
        await h.resources.close();
      },
    );

    it("keeps retired authority counted and retryable when its release fails", async () => {
      const h = harness(kind);
      await h.prepare();
      const error = new Error("controlled retired authority failure");
      h.leases[0]!.release.mockRejectedValueOnce(error);
      const next = await h.next();
      await expect(h.prepare(next, 2)).rejects.toBe(error);
      expect(h.failure).toHaveBeenCalledOnce();
      expect(h.resources.snapshot().staticLeases).toBe(2);
      expect(h.leases[1]!.open).not.toHaveBeenCalled();
      await h.resources.close();
      expect(h.leases[0]!.release).toHaveBeenCalledTimes(2);
      expect(h.resources.snapshot().staticLeases).toBe(0);
      expect([...h.images, ...h.fonts][0]!.close).toHaveBeenCalledOnce();
    });
  },
);

describe("independent decoded resource admission", () => {
  it.each(["weight", "style"])(
    "reopens a font when its %s changes",
    async (field) => {
      const h = harness("font");
      await h.prepare();
      const next = await h.next(
        field === "weight" ? { weight: 400 } : { style: "italic" },
      );
      await h.prepare(next, 2);
      expect(h.leases[1]!.adopt).not.toHaveBeenCalled();
      expect(h.loadFont).toHaveBeenCalledTimes(2);
      expect(h.loadFont.mock.calls[1]!.slice(2)).toEqual(
        field === "weight" ? ["400", "normal"] : ["700", "italic"],
      );
      expect(h.fonts[0]!.close).toHaveBeenCalledOnce();
      await h.resources.close();
    },
  );

  it.each([
    "schema",
    "extra key",
    "sourceWidth fraction",
    "sourceHeight zero",
    "derivativeWidth too large",
    "derivativeHeight zero",
    "pixel budget",
    "mime",
  ])("refuses image %s before decoding", async (field) => {
    const h = harness("image");
    h.configure((lease) => {
      const geometry = { ...lease.body.geometry! } as unknown as Record<
        string,
        unknown
      >;
      if (field === "schema") geometry.schema = "wrong";
      if (field === "extra key") geometry.extra = true;
      if (field === "sourceWidth fraction") geometry.sourceWidth = 0.5;
      if (field === "sourceHeight zero") geometry.sourceHeight = 0;
      if (field === "derivativeWidth too large")
        geometry.derivativeWidth = 16_385;
      if (field === "derivativeHeight zero") geometry.derivativeHeight = 0;
      if (field === "pixel budget") {
        geometry.derivativeWidth = 4096;
        geometry.derivativeHeight = 4096;
      }
      lease.body = {
        ...lease.body,
        geometry: geometry as unknown as AuthoringMediaLeaseBody["geometry"],
        ...(field === "mime"
          ? { blob: new Blob([new Uint8Array(8)], { type: "image/jpeg" }) }
          : {}),
      };
    });
    await expect(h.prepare()).rejects.toThrow();
    expect(h.decodeImage).not.toHaveBeenCalled();
    expect(h.leases[0]!.release).toHaveBeenCalledOnce();
    await h.resources.close();
  });

  it.each(["width", "height"])(
    "disposes decoded image %s drift without publishing it",
    async (field) => {
      const h = harness("image");
      const image = {
        width: field === "width" ? 319 : 320,
        height: field === "height" ? 179 : 180,
        close: vi.fn(),
      };
      h.decodeImage.mockResolvedValueOnce(image);
      await expect(h.prepare()).rejects.toThrow("source_unavailable");
      expect(image.close).toHaveBeenCalledOnce();
      expect(h.resources.snapshot().imageOwners).toBe(0);
      await h.resources.close();
    },
  );

  it("disposes a loaded font whose family is different from the requested family", async () => {
    const h = harness("font");
    const font = { family: "foreign-family", close: vi.fn() };
    h.loadFont.mockResolvedValueOnce(font);
    await expect(h.prepare()).rejects.toThrow("source_unavailable");
    expect(font.close).toHaveBeenCalledOnce();
    expect(h.resources.snapshot().fontOwners).toBe(0);
    await h.resources.close();
  });

  it.each(["mime", "size"])(
    "rejects font %s before loading it",
    async (field) => {
      const h = harness("font");
      h.configure((lease) => {
        lease.body = {
          ...lease.body,
          blob: new Blob(
            [new Uint8Array(field === "size" ? 1024 * 1024 + 1 : 8)],
            { type: field === "mime" ? "font/otf" : "font/ttf" },
          ),
        };
      });
      await expect(h.prepare()).rejects.toThrow("source_unavailable");
      expect(h.loadFont).not.toHaveBeenCalled();
      await h.resources.close();
    },
  );
});
