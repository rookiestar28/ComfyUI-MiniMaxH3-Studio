import { describe, expect, it } from "vitest";

import { AUTHORING_AVAILABILITY_PRODUCER } from "../src/contracts/authoringWorkbenchCodec";
import { deriveAvailabilityFacts } from "../src/host/authoringAvailability";
import { projectionFixture } from "./support/authoringFixture";

const graphFingerprint = `sha256:${"b".repeat(64)}`;

describe("availability fact derivation", () => {
  it("posts nothing when there is no graph verdict", () => {
    expect(deriveAvailabilityFacts(projectionFixture(), null)).toBeNull();
  });

  it("posts nothing when the reference set has no soundtrack rows", () => {
    const projection = projectionFixture();
    const empty = {
      ...projection,
      reference: { ...projection.reference, soundtracks: [] },
    };
    expect(
      deriveAvailabilityFacts(empty, { qualified: true, graphFingerprint }),
    ).toBeNull();
  });

  it("derives available facts from a qualified graph verdict", () => {
    const payload = deriveAvailabilityFacts(projectionFixture(), {
      qualified: true,
      graphFingerprint,
    });
    expect(payload).not.toBeNull();
    expect(payload?.producer).toBe(AUTHORING_AVAILABILITY_PRODUCER);
    expect(payload?.producer_revision).toBe(1);
    expect(payload?.fingerprint).toBe(graphFingerprint);
    expect(payload?.facts).toEqual([
      { video_id: "vid-1", availability: "available" },
    ]);
    expect(Object.isFrozen(payload)).toBe(true);
    expect(Object.isFrozen(payload?.facts)).toBe(true);
  });

  it("never claims unavailable from an unqualified shape verdict", () => {
    const payload = deriveAvailabilityFacts(projectionFixture(), {
      qualified: false,
      graphFingerprint,
    });
    expect(payload?.facts).toEqual([
      { video_id: "vid-1", availability: "unknown" },
    ]);
  });

  it("advances the producer revision past the projection's accepted revision", () => {
    const projection = projectionFixture();
    const advanced = {
      ...projection,
      availability: { producer: AUTHORING_AVAILABILITY_PRODUCER, revision: 7 },
    };
    const payload = deriveAvailabilityFacts(advanced, {
      qualified: true,
      graphFingerprint,
    });
    expect(payload?.producer_revision).toBe(8);
  });

  it("refuses a malformed graph fingerprint", () => {
    expect(() =>
      deriveAvailabilityFacts(projectionFixture(), {
        qualified: true,
        graphFingerprint: "sha256:notahash",
      }),
    ).toThrow(/graph fingerprint/);
  });
});
