import { describe, expect, it } from "vitest";
import { classifyM2556LeaseRequestPhase } from "./e2e/helpers/m25_56LeaseRequestPhase";

describe("M25-56 lease request phase", () => {
  it("does not relabel a pre-close release when its response arrives after close", () => {
    const closeRequestBoundary = 3;
    const releaseRequestOrdinal = 3;
    const delayedResponseArrivalOrdinal = 7;

    expect(delayedResponseArrivalOrdinal).toBeGreaterThan(closeRequestBoundary);
    expect(
      classifyM2556LeaseRequestPhase(
        releaseRequestOrdinal,
        closeRequestBoundary,
      ),
    ).toBe("before_close");
  });

  it("admits only a request issued after the close boundary as after-close", () => {
    expect(classifyM2556LeaseRequestPhase(4, 3)).toBe("after_close");
    expect(classifyM2556LeaseRequestPhase(0, 3)).toBeNull();
    expect(classifyM2556LeaseRequestPhase(4, -1)).toBeNull();
  });
});
