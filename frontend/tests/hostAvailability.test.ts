import { describe, expect, it } from "vitest";

import {
  HOST_AVAILABILITY_EVENT_NAMES,
  classifyHostAvailability,
  classifyReconciledRun,
  createHostAvailabilityTracker,
} from "../src/host/hostAvailability";
import type {
  GenerationJobState,
  GenerationSequenceProjection,
} from "../src/contracts/generationSequenceCodec";

describe("host availability classification", () => {
  it("names exactly the three host socket events the extension subscribes to", () => {
    expect([...HOST_AVAILABILITY_EVENT_NAMES]).toEqual([
      "status",
      "reconnecting",
      "reconnected",
    ]);
  });

  it("reads a null status payload as the closed socket the host reports", () => {
    expect(classifyHostAvailability("status", null)).toBe("lost");
    expect(classifyHostAvailability("status", undefined)).toBe("lost");
  });

  it("keeps a status payload with queue detail as a live socket", () => {
    expect(
      classifyHostAvailability("status", { exec_info: { queue_remaining: 0 } }),
    ).toBe("available");
    expect(classifyHostAvailability("status", { sid: "abc" })).toBe(
      "available",
    );
  });

  it("maps the reconnect events to their phases", () => {
    expect(classifyHostAvailability("reconnecting", undefined)).toBe(
      "reconnecting",
    );
    expect(classifyHostAvailability("reconnected", undefined)).toBe(
      "available",
    );
  });

  it("ignores an unrelated event name instead of guessing a phase", () => {
    expect(
      classifyHostAvailability("executed", { node: "17" }),
    ).toBeUndefined();
    expect(classifyHostAvailability("progress", {})).toBeUndefined();
  });
});

describe("host availability tracker", () => {
  it("starts available and reports only real transitions", () => {
    const tracker = createHostAvailabilityTracker();
    expect(tracker.phase()).toBe("available");
    expect(tracker.observe("status", { sid: "a" })).toBeUndefined();
    expect(tracker.observe("status", null)).toEqual({
      phase: "lost",
      previous: "available",
    });
    expect(tracker.observe("status", null)).toBeUndefined();
    expect(tracker.phase()).toBe("lost");
  });

  it("coalesces a repeated reconnected event so one reconciliation runs", () => {
    const tracker = createHostAvailabilityTracker();
    tracker.observe("status", null);
    expect(tracker.observe("reconnecting", undefined)).toEqual({
      phase: "reconnecting",
      previous: "lost",
    });
    expect(tracker.observe("reconnecting", undefined)).toBeUndefined();
    expect(tracker.observe("reconnected", undefined)).toEqual({
      phase: "available",
      previous: "reconnecting",
    });
    expect(tracker.observe("reconnected", undefined)).toBeUndefined();
  });

  it("reports a drop that arrives without a reconnecting event", () => {
    const tracker = createHostAvailabilityTracker();
    expect(tracker.observe("reconnecting", undefined)).toEqual({
      phase: "reconnecting",
      previous: "available",
    });
    expect(tracker.observe("status", null)).toEqual({
      phase: "lost",
      previous: "reconnecting",
    });
  });

  it("never transitions on an event the classifier does not own", () => {
    const tracker = createHostAvailabilityTracker();
    expect(tracker.observe("execution_success", { prompt_id: "p" })).toBe(
      undefined,
    );
    expect(tracker.phase()).toBe("available");
  });
});

describe("reconciled run classification", () => {
  const progressRow = (
    state: GenerationJobState,
    queuePromptId: string | null,
  ) => ({
    job_id: "job-1",
    segment_id: "segment-1",
    ordinal: 0,
    state,
    attempt: 1,
    transaction_id: "generation.command.1",
    queue_prompt_id: queuePromptId,
    host_owner_id: null,
    artifact_receipt_fingerprint: null,
    artifact_output_fingerprint: null,
    failure_code: null,
  });

  const sequence = (
    rows: ReturnType<typeof progressRow>[],
    cancellationRequested = false,
  ): GenerationSequenceProjection =>
    ({
      progress: rows,
      cancellation_requested: cancellationRequested,
      complete: false,
    }) as unknown as GenerationSequenceProjection;

  it("leaves a run the coordinator still records as executing owned by the host", () => {
    for (const state of [
      "planned",
      "projected",
      "submitted",
      "running",
    ] as const)
      expect(
        classifyReconciledRun(sequence([progressRow(state, "p1")]), "p1"),
      ).toEqual({ kind: "continue", verifying: false });
  });

  it("terminalizes from the recorded job state rather than from any disposition", () => {
    expect(
      classifyReconciledRun(sequence([progressRow("succeeded", "p1")]), "p1"),
    ).toEqual({ kind: "succeeded" });
    expect(
      classifyReconciledRun(sequence([progressRow("failed", "p1")]), "p1"),
    ).toEqual({ kind: "failed", interrupted: false });
    expect(
      classifyReconciledRun(sequence([progressRow("timed_out", "p1")]), "p1"),
    ).toEqual({ kind: "failed", interrupted: true });
    expect(
      classifyReconciledRun(sequence([progressRow("cancelled", "p1")]), "p1"),
    ).toEqual({ kind: "cancelled" });
    expect(
      classifyReconciledRun(
        sequence([progressRow("unknown_ownership", "p1")]),
        "p1",
      ),
    ).toEqual({ kind: "ownership_unknown" });
  });

  it("keeps a verification failure owned and shows it as verifying", () => {
    expect(
      classifyReconciledRun(
        sequence([progressRow("output_verification_failed", "p1")]),
        "p1",
      ),
    ).toEqual({ kind: "continue", verifying: true });
  });

  it("never lets a cancellation request alone terminalize a job", () => {
    // The request is not an outcome. A cancel that reached a running job is recorded by the
    // coordinator as unknown_ownership and refused here; one that never reached the host is
    // recorded as cancelled. Reading the flag would end a generation still executing as soon as a
    // sequence carries more than one segment.
    expect(
      classifyReconciledRun(
        sequence([progressRow("running", "p1")], true),
        "p1",
      ),
    ).toEqual({ kind: "continue", verifying: false });
    expect(
      classifyReconciledRun(
        sequence([progressRow("unknown_ownership", "p1")], true),
        "p1",
      ),
    ).toEqual({ kind: "ownership_unknown" });
    expect(
      classifyReconciledRun(
        sequence([progressRow("cancelled", "p1")], true),
        "p1",
      ),
    ).toEqual({ kind: "cancelled" });
    expect(
      classifyReconciledRun(
        sequence([progressRow("succeeded", "p1")], true),
        "p1",
      ),
    ).toEqual({ kind: "succeeded" });
  });

  it("selects the row this run owns rather than the first one", () => {
    const rows = [
      progressRow("succeeded", "other"),
      progressRow("running", "p1"),
    ];
    expect(classifyReconciledRun(sequence(rows), "p1")).toEqual({
      kind: "continue",
      verifying: false,
    });
  });

  it("refuses to read a terminal out of an ambiguous projection", () => {
    const rows = [
      progressRow("succeeded", "other"),
      progressRow("succeeded", "another"),
    ];
    expect(classifyReconciledRun(sequence(rows), "p1")).toEqual({
      kind: "continue",
      verifying: false,
    });
    expect(classifyReconciledRun(sequence([]), "p1")).toEqual({
      kind: "continue",
      verifying: false,
    });
  });

  it("falls back to a single unowned row when the prompt id is not known yet", () => {
    expect(
      classifyReconciledRun(sequence([progressRow("succeeded", null)])),
    ).toEqual({ kind: "succeeded" });
  });
});
