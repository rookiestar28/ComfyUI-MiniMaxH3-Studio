import { describe, expect, it } from "vitest";

import { SEQUENCE_COORDINATOR_ROUTE } from "../src/host/sequenceCoordinator";
import {
  classifyM2508CoordinatorAction,
  classifyM2508PostRoute,
  normalizeM2508HostApiPath,
} from "./e2e/host/m25_08RequestClassification";

describe("M25-08 supported-host POST classification", () => {
  it.each(["/prompt", "/queue", "/api/prompt", "/api/prompt/submit"])(
    "classifies the native queue path %s",
    (path) => {
      expect(classifyM2508PostRoute(path)).toBe("queue");
    },
  );

  it("separates the exact managed coordinator control plane from generation", () => {
    expect(classifyM2508PostRoute(SEQUENCE_COORDINATOR_ROUTE)).toBe(
      "coordinator",
    );
    expect(classifyM2508PostRoute(`/api${SEQUENCE_COORDINATOR_ROUTE}`)).toBe(
      "coordinator",
    );
  });

  it.each([
    "/h3-context/v1/generation/execute",
    `${SEQUENCE_COORDINATOR_ROUTE}/unexpected`,
    `/api/api${SEQUENCE_COORDINATOR_ROUTE}`,
  ])("fails closed for the non-coordinator generation path %s", (path) => {
    expect(classifyM2508PostRoute(path)).toBe("generation");
  });

  it("keeps provider and unrelated paths distinct", () => {
    expect(classifyM2508PostRoute("/h3-context/v1/provider/settings")).toBe(
      "provider",
    );
    expect(classifyM2508PostRoute("/h3-context/v1/authoring/action")).toBe(
      "other",
    );
    expect(classifyM2508PostRoute("/api/prompter")).toBe("other");
  });

  it("normalizes exactly one ComfyUI API transport prefix", () => {
    expect(normalizeM2508HostApiPath("/prompt")).toBe("/prompt");
    expect(normalizeM2508HostApiPath("/api/prompt")).toBe("/prompt");
    expect(normalizeM2508HostApiPath("/api/api/prompt")).toBe("/api/prompt");
  });

  it("retains only the two expected privacy-safe coordinator action buckets", () => {
    expect(
      classifyM2508CoordinatorAction({ action: "prepare_managed_run" }),
    ).toBe("prepare_managed_run");
    expect(
      classifyM2508CoordinatorAction({ action: "submit_managed_run" }),
    ).toBe("submit_managed_run");
    expect(classifyM2508CoordinatorAction({ action: "record_artifact" })).toBe(
      "unexpected",
    );
    expect(classifyM2508CoordinatorAction(null)).toBe("unexpected");
  });
});
