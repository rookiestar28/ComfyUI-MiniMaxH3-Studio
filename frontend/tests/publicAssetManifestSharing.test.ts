// One accepted snapshot reaches several manifest readers in the same commit. A decoded snapshot is
// frozen to its leaves, so its manifest and asset fingerprints are built and hashed once and shared;
// an input that can still change is never remembered, or an in-place edit would read old hashes.

import { afterEach, describe, expect, it, vi } from "vitest";

const counter = vi.hoisted(() => ({ hashes: 0 }));

vi.mock("../src/contracts/canonicalFingerprint", async (importOriginal) => {
  const actual =
    await importOriginal<
      typeof import("../src/contracts/canonicalFingerprint")
    >();
  return {
    ...actual,
    sha256Text: (value: string) => {
      counter.hashes++;
      return actual.sha256Text(value);
    },
  };
});

import { canonicalPublicRuntimeAssetFingerprint } from "../src/contracts/authoringMediaLeaseCodec";
import type { PublicCompositionSnapshot } from "../src/contracts/compositionCodec";
import {
  buildPublicAssetManifest,
  type PublicRuntimeAsset,
} from "../src/runtime/publicAssetManifest";
import { SMOKE_SHAPE, snapshotFixture } from "./support/nleWorkspaceFixture";

type Mutable<T> = { -readonly [K in keyof T]: T[K] };

afterEach(() => {
  counter.hashes = 0;
});

describe("public asset manifest sharing", () => {
  it("builds and hashes a decoded snapshot's manifest once for all its readers", () => {
    const snapshot = snapshotFixture(SMOKE_SHAPE);
    expect(snapshot.assets.length).toBeGreaterThan(0);
    counter.hashes = 0;
    const first = buildPublicAssetManifest(snapshot);
    const fingerprints = first.assets.map(
      canonicalPublicRuntimeAssetFingerprint,
    );
    expect(counter.hashes).toBe(1 + first.assets.length);
    for (let reader = 0; reader < 4; reader++) {
      const again = buildPublicAssetManifest(snapshot);
      expect(again).toBe(first);
      expect(again.assets.map(canonicalPublicRuntimeAssetFingerprint)).toEqual(
        fingerprints,
      );
    }
    expect(counter.hashes).toBe(1 + first.assets.length);

    // The next accepted snapshot is a new manifest even when its content is equal.
    const next = buildPublicAssetManifest(snapshotFixture(SMOKE_SHAPE));
    expect(next).not.toBe(first);
    expect(next).toEqual(first);
  });

  it("never remembers a snapshot or asset that can still change", () => {
    // structuredClone keeps the content and drops every freeze.
    const snapshot = structuredClone(
      snapshotFixture(SMOKE_SHAPE),
    ) as Mutable<PublicCompositionSnapshot>;
    const before = buildPublicAssetManifest(snapshot);
    const asset = snapshot.assets[0] as Mutable<
      PublicCompositionSnapshot["assets"][number]
    >;
    asset.sourceFrameCount = 7;
    const after = buildPublicAssetManifest(snapshot);
    expect(after).not.toBe(before);
    expect(after.assets[0]!.sourceFrameCount).toBe(7);

    const loose = structuredClone(
      before.assets[0]!,
    ) as Mutable<PublicRuntimeAsset>;
    const original = canonicalPublicRuntimeAssetFingerprint(loose);
    loose.sourceFrameCount = 9;
    expect(canonicalPublicRuntimeAssetFingerprint(loose)).not.toBe(original);
  });

  it("checks the leaves, not only the snapshot and its asset list", () => {
    const decoded = snapshotFixture(SMOKE_SHAPE);
    const assets = structuredClone(decoded.assets) as Mutable<
      PublicCompositionSnapshot["assets"][number]
    >[];
    const snapshot = Object.freeze({
      ...decoded,
      assets: Object.freeze(assets),
    }) as PublicCompositionSnapshot;
    const before = buildPublicAssetManifest(snapshot);
    assets[0]!.sourceFrameCount = 5;
    const after = buildPublicAssetManifest(snapshot);
    expect(after).not.toBe(before);
    expect(after.assets[0]!.sourceFrameCount).toBe(5);
  });

  // Root's pre-delivery finding: freezing an object does not freeze what its getters read, and a
  // frozen object can take its fields from a prototype. Neither input may be remembered.
  it("never remembers a frozen input whose fields are accessors or inherited", () => {
    const decoded = snapshotFixture(SMOKE_SHAPE);
    const plain = structuredClone(decoded.assets[0]!);
    let frames = 7;
    const accessor = Object.freeze({
      ...plain,
      landmarks: Object.freeze([]),
      sourceTimeBase:
        plain.sourceTimeBase === null
          ? null
          : Object.freeze({ ...plain.sourceTimeBase }),
      get sourceFrameCount() {
        return frames;
      },
    }) as PublicCompositionSnapshot["assets"][number];
    const snapshot = Object.freeze({
      ...decoded,
      assets: Object.freeze([accessor]),
    }) as PublicCompositionSnapshot;
    const before = buildPublicAssetManifest(snapshot);
    const beforeFingerprint = canonicalPublicRuntimeAssetFingerprint(
      accessor as unknown as PublicRuntimeAsset,
    );
    frames = 9;
    const after = buildPublicAssetManifest(snapshot);
    expect(after).not.toBe(before);
    expect(after.assets[0]!.sourceFrameCount).toBe(9);
    expect(
      canonicalPublicRuntimeAssetFingerprint(
        accessor as unknown as PublicRuntimeAsset,
      ),
    ).not.toBe(beforeFingerprint);

    // Inherited: the snapshot's own object is frozen and empty; its fields live on a prototype.
    const base = { ...decoded, assets: decoded.assets };
    const inherited = Object.freeze(
      Object.create(base),
    ) as PublicCompositionSnapshot;
    const first = buildPublicAssetManifest(inherited);
    base.assets = Object.freeze([]) as typeof decoded.assets;
    const second = buildPublicAssetManifest(inherited);
    expect(second).not.toBe(first);
    expect(second.assets).toHaveLength(0);
  });
});
