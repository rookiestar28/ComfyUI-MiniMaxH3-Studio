import { describe, expect, it } from "vitest";

import parity from "../../tests/fixtures/m25_16_scene_resolver_parity_v1.json";
import {
  CompositionContractError,
  decodePublicCompositionSnapshot,
  decodeResolvedScene,
} from "../src/contracts/compositionCodec";
import {
  resolveCompositionScene,
  resolveSourceLandmarkAfterElapsed,
} from "../src/runtime/sceneResolver";

type ParityCase = Readonly<{
  name: string;
  snapshot: Record<string, unknown>;
  frames: readonly Readonly<{
    frame: number;
    scene?: Record<string, unknown>;
    refusal?: string;
  }>[];
}>;

function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value !== null && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonical(record[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

describe("M25-16 scene resolver parity", () => {
  const cases = (parity as { cases: readonly ParityCase[] }).cases;

  it("covers every derived backend variant", () => {
    expect(cases.length).toBeGreaterThanOrEqual(8);
    expect(cases.map((row) => row.name)).toContain("primary_split_two_owners");
  });

  for (const row of cases) {
    it(`resolves ${row.name} exactly like the backend`, () => {
      const snapshot = decodePublicCompositionSnapshot(row.snapshot);
      for (const frame of row.frames) {
        if (frame.refusal !== undefined) {
          expect(
            () => resolveCompositionScene(snapshot, frame.frame),
            `${row.name} frame ${frame.frame}`,
          ).toThrow(CompositionContractError);
          try {
            resolveCompositionScene(snapshot, frame.frame);
          } catch (error) {
            expect((error as CompositionContractError).code).toBe(
              frame.refusal,
            );
          }
          continue;
        }
        const scene = resolveCompositionScene(snapshot, frame.frame);
        expect(canonical(scene), `${row.name} frame ${frame.frame}`).toBe(
          canonical(frame.scene),
        );
        // The port must also satisfy the browser decoder the session applies to it.
        const decoded = decodeResolvedScene(scene);
        expect(decoded.frame).toBe(frame.frame);
        expect(decoded.publicFingerprint).toBe(snapshot.publicFingerprint);
      }
    });
  }

  it("returns a frozen wire that survives structured cloning", () => {
    const snapshot = decodePublicCompositionSnapshot(cases[0]!.snapshot);
    const scene = resolveCompositionScene(snapshot, 12);
    expect(Object.isFrozen(scene)).toBe(true);
    expect(structuredClone(scene)).toEqual(scene);
  });

  it("maps exact output boundaries onto admitted landmarks and refuses the rest", () => {
    const snapshot = decodePublicCompositionSnapshot(cases[0]!.snapshot);
    const asset = snapshot.assets.find((row) => row.assetId === "vid-primary")!;
    const rate = snapshot.output.frameRate as { num: number; den: number };
    expect(resolveSourceLandmarkAfterElapsed(asset, 0, 12, rate)).toBe(12);
    expect(resolveSourceLandmarkAfterElapsed(asset, 12, -12, rate)).toBe(0);
    expect(() => resolveSourceLandmarkAfterElapsed(asset, 0, 1, rate)).toThrow(
      /source_range_unavailable/u,
    );
    expect(() =>
      resolveSourceLandmarkAfterElapsed(asset, 0, 1_000, rate),
    ).toThrow(/source_range_unavailable/u);
  });
});
