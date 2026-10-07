import { describe, expect, it, vi } from "vitest";

import { decodeGenerationSequenceProjection } from "../src/contracts/generationSequenceCodec";
import { createGenerationSequenceDriver } from "../src/host/generationSequence";
import { generationSequenceWire } from "./generationSequenceFixture";
import { acceptedQueueReceipt } from "./support/queuePromptTestDouble";

const OWNED_RESULT_IDENTITY = Object.freeze({
  ownedProjectionFingerprint: `sha256:${"3".repeat(64)}`,
  ownedNodeIds: Object.freeze(["1", "8", "45"]),
  ownedLinkIds: Object.freeze(["15", "17", "18"]),
});

describe("generation sequence App Mode driver", () => {
  it("executes one exact core-issued command and returns host correlation", async () => {
    const projection = decodeGenerationSequenceProjection(
      generationSequenceWire(),
    );
    const start = vi.fn(async (_inputs, options) => {
      options?.onQueueSubmitted?.();
      return {
        queueResult: acceptedQueueReceipt("prompt.generated.1"),
        ...OWNED_RESULT_IDENTITY,
        graphFingerprint: `sha256:${"9".repeat(64)}`,
        compiledPromptFingerprint:
          projection.eligible_commands[0]!.compiled_prompt_fingerprint,
        queuePromptId: "prompt.generated.1",
        route: "new" as const,
      };
    });
    const driver = createGenerationSequenceDriver(start);

    const observation = await driver.execute(projection, {
      jobId: "job.1",
      sourceId: "source.1",
      referenceIds: [],
      route: "new",
      requestedDurationMilliseconds: 5000,
      effectiveDurationMilliseconds: 5167,
      inputs: {
        task_mode: "t2va",
        user_intent: "Content stays outside sequence evidence.",
        duration_milliseconds: 5000,
        frame_count: 124,
      },
    });

    expect(start).toHaveBeenCalledOnce();
    expect(start.mock.calls[0]?.[1]).toMatchObject({
      expectedIdentity: {
        graphFingerprint: projection.eligible_commands[0]!.graph_fingerprint,
        compiledPromptFingerprint:
          projection.eligible_commands[0]!.compiled_prompt_fingerprint,
      },
    });
    expect(observation).toMatchObject({
      schema: "h3.context.generation_sequence_host_observation.v1",
      job_id: "job.1",
      attempt: 1,
      transaction_id: "generation.command.1",
      queue_prompt_id: "prompt.generated.1",
      // M17-20 D6: the observation reports the graph it measured, taken from the
      // command rather than assumed, so the sequence authority compares like
      // with like.
      fingerprint_domain: "output_producing_graph",
      graph_fingerprint: `sha256:${"9".repeat(64)}`,
    });
    await expect(
      driver.execute(projection, {
        jobId: "job.1",
        sourceId: "source.1",
        referenceIds: [],
        route: "new",
        requestedDurationMilliseconds: 5000,
        effectiveDurationMilliseconds: 5167,
        inputs: {
          task_mode: "t2va",
          user_intent: "Do not duplicate.",
          duration_milliseconds: 5000,
          frame_count: 124,
        },
      }),
    ).rejects.toMatchObject({ code: "duplicate_submission" });
  });

  it("rejects non-eligible, drifted authority, and unsupported duration before App Mode", async () => {
    const projection = decodeGenerationSequenceProjection(
      generationSequenceWire(),
    );
    const start = vi.fn();
    const driver = createGenerationSequenceDriver(start);
    const binding = {
      jobId: "foreign",
      sourceId: "source.1",
      referenceIds: [] as string[],
      route: "new" as const,
      requestedDurationMilliseconds: 5000,
      effectiveDurationMilliseconds: 5167,
      inputs: {
        task_mode: "t2va" as const,
        user_intent: "safe",
        duration_milliseconds: 5000,
        frame_count: 124,
      },
    };
    await expect(driver.execute(projection, binding)).rejects.toMatchObject({
      code: "job_not_eligible",
    });
    await expect(
      driver.execute(projection, {
        ...binding,
        jobId: "job.1",
        sourceId: "foreign",
      }),
    ).rejects.toMatchObject({ code: "input_authority_mismatch" });
    await expect(
      driver.execute(projection, {
        ...binding,
        jobId: "job.1",
        requestedDurationMilliseconds: 5001,
      }),
    ).rejects.toMatchObject({ code: "input_authority_mismatch" });
    await expect(
      driver.execute(projection, {
        ...binding,
        jobId: "job.1",
        effectiveDurationMilliseconds: 5000,
      }),
    ).rejects.toMatchObject({ code: "input_authority_mismatch" });
    // The decoded command carries effective lattice duration. A contradictory
    // binding is an authority mismatch, while internally contradictory duration
    // wire members never decode at all.
    const drifted = structuredClone(generationSequenceWire());
    drifted.eligible_commands[0]!.duration = {
      duration_milliseconds: 4_167,
      frame_count: 107,
      delivered_milliseconds: 4_458,
      snapped: true,
    };
    await expect(
      driver.execute(decodeGenerationSequenceProjection(drifted), {
        ...binding,
        jobId: "job.1",
      }),
    ).rejects.toMatchObject({ code: "input_authority_mismatch" });
    const forged = structuredClone(generationSequenceWire());
    forged.eligible_commands[0]!.duration = {
      duration_milliseconds: 5_167,
      frame_count: 124,
      delivered_milliseconds: 5_167,
      snapped: true,
    };
    expect(() => decodeGenerationSequenceProjection(forged)).toThrow();
    expect(start).not.toHaveBeenCalled();
  });

  it("allows a pre-submit failure retry but quarantines ambiguous host ownership", async () => {
    const projection = decodeGenerationSequenceProjection(
      generationSequenceWire(),
    );
    const preSubmit = vi.fn().mockRejectedValue(new Error("compile"));
    const retryable = createGenerationSequenceDriver(preSubmit);
    const binding = {
      jobId: "job.1",
      sourceId: "source.1",
      referenceIds: [] as string[],
      route: "replace" as const,
      requestedDurationMilliseconds: 5000,
      effectiveDurationMilliseconds: 5167,
      inputs: {
        task_mode: "t2va" as const,
        user_intent: "safe",
        duration_milliseconds: 5000,
        frame_count: 124,
      },
    };
    await expect(retryable.execute(projection, binding)).rejects.toThrow();
    await expect(retryable.execute(projection, binding)).rejects.toThrow();
    expect(preSubmit).toHaveBeenCalledTimes(2);

    const ambiguous = createGenerationSequenceDriver(
      vi.fn(async (_inputs, options) => {
        options?.onQueueSubmitted?.();
        throw new Error("lost response");
      }),
    );
    await expect(ambiguous.execute(projection, binding)).rejects.toThrow();
    await expect(ambiguous.execute(projection, binding)).rejects.toMatchObject({
      code: "ambiguous_host_ownership",
    });

    const mismatchedResult = createGenerationSequenceDriver(
      vi.fn(async () => ({
        queueResult: acceptedQueueReceipt("prompt.foreign"),
        ...OWNED_RESULT_IDENTITY,
        graphFingerprint: `sha256:${"9".repeat(64)}`,
        compiledPromptFingerprint: `sha256:${"8".repeat(64)}`,
        queuePromptId: "prompt.foreign",
        route: "new" as const,
      })),
    );
    await expect(
      mismatchedResult.execute(projection, binding),
    ).rejects.toMatchObject({
      code: "host_identity_mismatch",
    });
    await expect(
      mismatchedResult.execute(projection, binding),
    ).rejects.toMatchObject({
      code: "ambiguous_host_ownership",
    });
  });
});
