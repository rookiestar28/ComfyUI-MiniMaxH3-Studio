import { describe, expect, it, vi } from "vitest";

import {
  RELEASE_REQUEST_V2_SCHEMA,
  SequenceCoordinatorClientError,
  createSequenceCoordinatorClient,
  isClientDetachUnsupported,
  isDetachedDisposition,
  releaseSequencePayload,
} from "../src/host/sequenceCoordinator";
import {
  managedCoordinatorResponse,
  managedLifecycleWires,
} from "./support/managedLifecycleWire";

const FINGERPRINT = `sha256:${"1".repeat(64)}`;
const PROOF = `sha256:${"2".repeat(64)}`;
const RUN_HANDLE = `mc_${"m".repeat(40)}`;

describe("M23-51 release intent codec", () => {
  it("builds the two intents that carry no terminal proof", () => {
    for (const intent of ["cleanup_pre_submit", "detach_client"] as const) {
      expect(releaseSequencePayload(RUN_HANDLE, intent, FINGERPRINT)).toEqual({
        schema: RELEASE_REQUEST_V2_SCHEMA,
        run_handle: RUN_HANDLE,
        intent,
        expected_state_fingerprint: FINGERPRINT,
      });
    }
  });

  it("builds a terminal cleanup with its proof", () => {
    expect(
      releaseSequencePayload(
        RUN_HANDLE,
        "cleanup_terminal",
        FINGERPRINT,
        PROOF,
      ),
    ).toEqual({
      schema: RELEASE_REQUEST_V2_SCHEMA,
      run_handle: RUN_HANDLE,
      intent: "cleanup_terminal",
      expected_state_fingerprint: FINGERPRINT,
      observed_terminal_fingerprint: PROOF,
    });
  });

  it("refuses to build a proof onto the wrong intent, or a terminal cleanup without one", () => {
    // The backend closes the payload per intent, so both of these would be a 400. Failing here
    // instead keeps the mistake at the call site that made it.
    expect(() =>
      releaseSequencePayload(RUN_HANDLE, "cleanup_terminal", FINGERPRINT),
    ).toThrow();
    expect(() =>
      releaseSequencePayload(RUN_HANDLE, "detach_client", FINGERPRINT, PROOF),
    ).toThrow();
  });

  it("names every detached disposition as a retained run", () => {
    for (const disposition of [
      "detached",
      "detached_terminal",
      "detached_unknown_ownership",
    ] as const)
      expect(isDetachedDisposition(disposition)).toBe(true);
    for (const disposition of ["released", "current", "cancelled"] as const)
      expect(isDetachedDisposition(disposition)).toBe(false);
  });

  it("decodes a detached response rather than rejecting the new disposition", async () => {
    const wires = managedLifecycleWires(FINGERPRINT);
    const fetchApi = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () =>
        managedCoordinatorResponse(
          "detached",
          wires.running,
          wires.production.running,
        ),
    }));
    const client = createSequenceCoordinatorClient({ fetchApi });

    const result = await client.send(
      "m23_51.detach",
      "release_sequence",
      releaseSequencePayload(RUN_HANDLE, "detach_client", FINGERPRINT),
    );
    expect(result.disposition).toBe("detached");
    expect(isDetachedDisposition(result.disposition)).toBe(true);
  });

  it("reads a rejected V2 request as an unsupported backend, never as a detach", () => {
    // The affordance is hidden; nothing is simulated. A backend that cannot decode the request
    // still owns the run, so reporting a successful detach here would be the one untrue answer.
    expect(
      isClientDetachUnsupported(
        new SequenceCoordinatorClientError("invalid_action", 400),
        RUN_HANDLE,
      ),
    ).toBe(true);
    for (const status of [409, 410, 500])
      expect(
        isClientDetachUnsupported(
          new SequenceCoordinatorClientError("coordinator_conflict", status),
          RUN_HANDLE,
        ),
      ).toBe(false);
  });

  it("does not blame the backend for a request the client itself malformed", () => {
    // The route answers 400 for a bad run handle too, and the error envelope carries a category
    // rather than the backend's code, so a bare status check would report a client bug as a
    // missing backend capability and hide an affordance that actually works.
    for (const malformed of [
      "",
      "not-a-handle",
      "mc_",
      `mc_${"m".repeat(200)}`,
    ])
      expect(
        isClientDetachUnsupported(
          new SequenceCoordinatorClientError("invalid_action", 400),
          malformed,
        ),
      ).toBe(false);
  });
});
