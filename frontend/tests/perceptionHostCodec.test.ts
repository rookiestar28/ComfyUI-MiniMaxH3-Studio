import { describe, expect, it } from "vitest";
import { decodePerceptionHostStatus } from "../src/contracts/perceptionHostCodec";

const configured = {
  schema: "h3.context.perception_host_status.v1",
  state: "configured",
  visual: true,
  audio: false,
  reason: "none",
  cancellation_observed: true,
} as const;

describe("finite perception host readiness", () => {
  it("admits a closed visual-only readiness without configuration values", () => {
    expect(decodePerceptionHostStatus(configured)).toEqual(configured);
    expect(Object.isFrozen(decodePerceptionHostStatus(configured))).toBe(true);
  });

  it("admits the explicit missing-file and cancellation-unobserved states", () => {
    expect(
      decodePerceptionHostStatus({
        ...configured,
        state: "not_configured",
        visual: false,
        reason: "settings_missing",
        cancellation_observed: false,
      }).state,
    ).toBe("not_configured");
    expect(
      decodePerceptionHostStatus({
        ...configured,
        cancellation_observed: false,
      }).cancellation_observed,
    ).toBe(false);
    expect(
      decodePerceptionHostStatus({ ...configured, visual: false, audio: true })
        .audio,
    ).toBe(true);
  });

  it.each([
    {
      ...configured,
      settings: { python_executable: "untrusted_operator_value" },
    },
    { ...configured, reason: "untrusted_operator_value" },
    { ...configured, visual: "true" },
    { ...configured, cancellation_observed: 1 },
    { ...configured, schema: "unknown" },
    { ...configured, state: "unknown" },
    { ...configured, visual: false },
    { ...configured, state: "refused" },
    { ...configured, reason: "path_refused" },
    {
      ...configured,
      state: "not_configured",
      visual: false,
      reason: "settings_missing",
    },
    {
      ...configured,
      state: "refused",
      visual: false,
      cancellation_observed: false,
    },
    null,
    [],
  ])("refuses unknown, coercible or inconsistent readiness", (value) => {
    expect(() => decodePerceptionHostStatus(value)).toThrow();
  });
});
