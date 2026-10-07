import { describe, expect, it, vi } from "vitest";

import { decodeGenerationSequenceProjection } from "../src/contracts/generationSequenceCodec";
import { decodeProductionWorkbenchProjection } from "../src/contracts/productionWorkbenchCodec";
import {
  buildProductionGenerationBinding,
  createProductionGenerationController,
  joinProductionGenerationSequence,
  productionGenerationKey,
} from "../src/host/productionGeneration";
import { createGenerationSequenceDriver } from "../src/host/generationSequence";
import { generationSequenceWire } from "./generationSequenceFixture";
import { unavailableProductionAssemblyWire } from "./support/productionAssemblyWire";

const fp = (value: string) => `sha256:${value.repeat(64)}`;

function productionWire() {
  return {
    schema: "h3.context.production_workbench.projection.v1",
    workspace_handle: `pw_${"a".repeat(43)}`,
    workspace_id: "workspace.1",
    workspace_revision: 2,
    workspace_fingerprint: fp("c"),
    segments: [
      {
        segment_id: "segment.1",
        ordinal: 1,
        task_mode: "t2va",
        duration: {
          duration_milliseconds: 5167,
          delivered_milliseconds: 5167,
          frame_count: 124,
          snapped: false,
        },
        relation: "independent",
        predecessor_segment_id: null,
        boundary_kind: "independent",
        closure_state: "dirty_self",
        job_state: "planned",
        artifact_state: "unavailable",
        continuity_state: "unavailable",
        delivered_geometry: null,
      },
    ],
    selected_segment_ids: ["segment.1"],
    run: { state: "ready", completed: 0, total: 1 },
    generation_sequence: {
      schema: "h3.context.generation_sequence_projection.v1",
      sequence_id: "sequence.1",
      sequence_fingerprint: fp("a"),
      state_fingerprint: fp("b"),
      workspace_id: "workspace.1",
      workspace_revision: 2,
      workspace_fingerprint: fp("c"),
      correlation: { prompt_id: "prompt.1", execution_node_id: "node.1" },
    },
    reconstruction: { state: "unavailable" },
    assembly: unavailableProductionAssemblyWire(),
    authority_versions: ["h3.context.generation_sequence_projection.v1"],
    outputs: [],
    allowed_actions: [
      "set_selection",
      "read_projection",
      "release_workspace",
      "submit_generation_job",
    ],
    blocker_codes: [],
    limits: { max_segments: 64, max_outputs: 65 },
  };
}

describe("Production exact M17-08 join", () => {
  it("retains and binds only the independently decoded exact command", () => {
    const production = decodeProductionWorkbenchProjection(productionWire());
    const sequence = decodeGenerationSequenceProjection(
      generationSequenceWire(),
    );
    expect(joinProductionGenerationSequence(production, sequence)).toBe(
      sequence,
    );

    const binding = buildProductionGenerationBinding({
      production,
      sequence,
      jobId: "job.1",
      workspace: {
        reportId: "source.1",
        taskMode: "t2va",
        requestedDurationMilliseconds: 5167,
        effectiveDurationMilliseconds: 5167,
        frameCount: 124,
        referenceIds: [],
      },
      request: {
        inputs: {
          task_mode: "t2va",
          user_intent: "Private content stays outside M17-12 evidence.",
          duration_milliseconds: 5167,
          frame_count: 124,
        },
        options: { useExisting: true },
      },
    });
    expect(binding).toMatchObject({
      jobId: "job.1",
      sourceId: "source.1",
      referenceIds: [],
      route: "existing",
    });
  });

  it("B-M1605-EXIST-02 binds an existing graph to its executed task mode, not the Sidebar selection", () => {
    const productionI2va = productionWire();
    productionI2va.segments[0]!.task_mode = "i2va";
    const sequenceI2va = generationSequenceWire();
    sequenceI2va.eligible_commands[0]!.task_mode = "i2va";
    const production = decodeProductionWorkbenchProjection(productionI2va);
    const sequence = decodeGenerationSequenceProjection(sequenceI2va);
    const bind = (options?: { useExisting?: boolean }) =>
      buildProductionGenerationBinding({
        production,
        sequence,
        jobId: "job.1",
        workspace: {
          reportId: "source.1",
          taskMode: "i2va",
          requestedDurationMilliseconds: 5167,
          effectiveDurationMilliseconds: 5167,
          frameCount: 124,
          referenceIds: [],
        },
        request: {
          inputs: {
            task_mode: "t2va",
            user_intent: "The Sidebar default mode does not author this graph.",
            duration_milliseconds: 5167,
            frame_count: 124,
          },
          ...(options === undefined ? {} : { options }),
        },
      });

    // M24-07 preserves the existing Request's own task mode, so the executed workspace is the
    // authority and the retained inputs carry it for any later submission of the same command.
    const existing = bind({ useExisting: true });
    expect(existing).toMatchObject({
      jobId: "job.1",
      route: "existing",
      inputs: { task_mode: "i2va", duration_milliseconds: 5167 },
    });
    expect(() =>
      createGenerationSequenceDriver(vi.fn()).claim(sequence, existing!),
    ).not.toThrow();
    // Routes that author the task mode still fail closed on a disagreement.
    expect(bind()).toBeUndefined();
  });

  it("keeps authored and effective snapped durations as separate authorities", () => {
    const production = decodeProductionWorkbenchProjection(productionWire());
    const sequence = decodeGenerationSequenceProjection(
      generationSequenceWire(),
    );
    const binding = buildProductionGenerationBinding({
      production,
      sequence,
      jobId: "job.1",
      workspace: {
        reportId: "source.1",
        taskMode: "t2va",
        requestedDurationMilliseconds: 5000,
        effectiveDurationMilliseconds: 5167,
        frameCount: 124,
        referenceIds: [],
      },
      request: {
        inputs: {
          task_mode: "t2va",
          user_intent: "A snapped duration remains exact at each authority.",
          duration_milliseconds: 5000,
          frame_count: 124,
        },
      },
    });

    expect(binding).toMatchObject({ jobId: "job.1", sourceId: "source.1" });
  });

  it.each([
    ["authored request", 5001, 5167],
    ["effective lattice", 5000, 5000],
  ])("fails closed on wrong %s duration", (_name, requested, effective) => {
    const production = decodeProductionWorkbenchProjection(productionWire());
    const sequence = decodeGenerationSequenceProjection(
      generationSequenceWire(),
    );
    expect(
      buildProductionGenerationBinding({
        production,
        sequence,
        jobId: "job.1",
        workspace: {
          reportId: "source.1",
          taskMode: "t2va",
          requestedDurationMilliseconds: 5000,
          effectiveDurationMilliseconds: effective,
          frameCount: 124,
          referenceIds: [],
        },
        request: {
          inputs: {
            task_mode: "t2va",
            user_intent: "A wrong authority remains rejected.",
            duration_milliseconds: requested,
            frame_count: 124,
          },
        },
      }),
    ).toBeUndefined();
  });

  it("uses a persistent exact-key single flight and exposes bounded failure", async () => {
    const sequence = decodeGenerationSequenceProjection(
      generationSequenceWire(),
    );
    const key = productionGenerationKey(sequence, "job.1");
    expect(key).toContain(sequence.sequence_fingerprint);
    expect(key).toContain(sequence.state_fingerprint);

    let rejectDriver: ((reason?: unknown) => void) | undefined;
    const driver = vi.fn(
      () =>
        new Promise<void>((_resolve, reject) => {
          rejectDriver = reject;
        }),
    );
    const controller = createProductionGenerationController();
    const first = controller.execute(key, driver);
    expect(controller.disposition(key)).toBe("pending");
    await expect(controller.execute(key, driver)).rejects.toMatchObject({
      code: "generation_control_unavailable",
    });
    expect(driver).toHaveBeenCalledTimes(1);

    rejectDriver?.(new Error("private driver detail"));
    await expect(first).rejects.toThrow("private driver detail");
    expect(controller.disposition(key)).toBe("generation_failed");
    await expect(controller.execute(key, driver)).rejects.toMatchObject({
      code: "generation_control_unavailable",
    });
    expect(driver).toHaveBeenCalledTimes(1);
  });

  it("retains 512 keys without eviction and blocks key 513 before the driver", async () => {
    const controller = createProductionGenerationController();
    const driver = vi.fn(async () => undefined);
    for (let index = 0; index < 512; index += 1) {
      const key = `sha256:${"a".repeat(64)}:sha256:${"b".repeat(64)}:job.${index}:1`;
      await controller.execute(key, driver);
      expect(controller.disposition(key)).toBe("submitted");
    }
    const overflow = `sha256:${"c".repeat(64)}:sha256:${"d".repeat(64)}:job.513:1`;
    expect(controller.disposition(overflow)).toBe(
      "generation_control_capacity",
    );
    await expect(controller.execute(overflow, driver)).rejects.toMatchObject({
      code: "generation_control_capacity",
    });
    expect(driver).toHaveBeenCalledTimes(512);
    expect(controller.size).toBe(512);
  });

  it("keeps pending and submitted truth across remount-style snapshot reads", async () => {
    const controller = createProductionGenerationController();
    let resolveDriver: (() => void) | undefined;
    const driver = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          resolveDriver = resolve;
        }),
    );
    const key = `sha256:${"1".repeat(64)}:sha256:${"2".repeat(64)}:job.remount:1`;
    const pending = controller.execute(key, driver);

    expect(controller.disposition(key)).toBe("pending");
    expect(controller.disposition(key)).toBe("pending");
    resolveDriver?.();
    await pending;
    expect(controller.disposition(key)).toBe("submitted");
    await expect(controller.execute(key, driver)).rejects.toMatchObject({
      code: "generation_control_unavailable",
    });
    expect(driver).toHaveBeenCalledTimes(1);

    const freshKey = key.replace(":job.remount:1", ":job.remount:2");
    expect(controller.disposition(freshKey)).toBe("available");
  });

  it.each([
    ["sequence", "sequence_id", "foreign.sequence"],
    ["state", "state_fingerprint", fp("9")],
    ["workspace", "workspace_fingerprint", fp("8")],
    ["correlation", "prompt_id", "foreign.prompt"],
  ])("fails closed on %s mismatch", (_name, field, value) => {
    const wire = generationSequenceWire() as Record<string, unknown>;
    if (field === "prompt_id")
      wire.correlation = { prompt_id: value, execution_node_id: "node.1" };
    else wire[field] = value;
    const production = decodeProductionWorkbenchProjection(productionWire());
    const sequence = decodeGenerationSequenceProjection(wire);
    expect(
      joinProductionGenerationSequence(production, sequence),
    ).toBeUndefined();
  });
});
