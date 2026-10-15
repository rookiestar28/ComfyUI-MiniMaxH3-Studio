import { describe, expect, it } from "vitest";

import { sequenceProductionProjection } from "./support/nleSequenceFixture";

import { fill, nleCopy } from "../src/components/nle/nleCopy";

describe("assembly input counts", () => {
  it.each([1, 2])("labels %i cuts without number agreement", (cuts) => {
    const artifacts = cuts + 1;
    expect(
      fill(nleCopy("en").sequence.assemblyInputs, { artifacts, cuts }),
    ).toBe(`Segment videos: ${artifacts}. Cuts: ${cuts}.`);
  });

  it.each([1, 2])("preserves both Chinese meanings for %i cuts", (cuts) => {
    const artifacts = cuts + 1;
    expect(
      fill(nleCopy("zh-TW").sequence.assemblyInputs, { artifacts, cuts }),
    ).toBe(`由 ${artifacts} 段片段影片與 ${cuts} 個剪接點組成。`);
    expect(
      fill(nleCopy("zh-CN").sequence.assemblyInputs, { artifacts, cuts }),
    ).toBe(`由 ${artifacts} 段片段视频和 ${cuts} 个剪切点组成。`);
  });
});

describe("sequence harness assembly projections", () => {
  it.each(["unavailable", "ready", "running", "succeeded"] as const)(
    "admits the actual browser fixture's %s state under the current codec",
    (state) => {
      const projection = sequenceProductionProjection(state);
      if (state === "unavailable") {
        expect(projection.assembly.outputProfiles).toEqual([]);
        expect(projection.assembly.failureCode).toBe(
          "media_runtime_not_authorized",
        );
      } else {
        expect(projection.assembly.outputProfiles).toContainEqual({
          outputProfileId: projection.assembly.outputProfileId,
          capabilityFingerprint: projection.assembly.capabilityFingerprint,
        });
        expect(projection.assembly.failureCode).toBeNull();
      }
      expect(projection.allowedActions.includes("assemble_sequence")).toBe(
        state === "ready",
      );
    },
  );
});
