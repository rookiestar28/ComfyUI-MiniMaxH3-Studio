import { describe, expect, it } from "vitest";

import { accumulatedLifecycleWires } from "../e2e/managedEntry";
import { decodeProductionWorkbenchProjection } from "../src/contracts/productionWorkbenchCodec";
import { decodeSequenceCoordinatorResult } from "../src/host/sequenceCoordinator";
import { buildMemberGenerationBinding } from "../src/lifecycle/managedProjectMember";
import { managedMemberCoordinatorResponse } from "./support/managedLifecycleWire";

describe("managed accumulation hermetic wire", () => {
  it("joins an appended project member whose private one-job ordinal restarts at one", () => {
    const wires = accumulatedLifecycleWires(
      `sha256:${"1".repeat(64)}`,
      `sha256:${"2".repeat(64)}`,
      "17",
      "00000000-0000-4000-8000-000000000002",
      {
        duration_milliseconds: 8000,
        delivered_milliseconds: 8000,
        frame_count: 192,
        snapped: false,
      },
      2,
    );
    const production = decodeProductionWorkbenchProjection(
      wires.production.planned,
    );
    const result = decodeSequenceCoordinatorResult(
      managedMemberCoordinatorResponse(
        "prepared",
        wires.planned,
        wires.production.planned,
        "segment_2",
      ),
    );
    if (
      result.schema !==
      "h3.context.generation_coordinator.managed_member_response.v1"
    )
      throw new Error("member fixture rejected");
    const privateSequence = Object.freeze({
      ...result.sequence,
      eligible_commands: result.sequence.eligible_commands.map((command) =>
        Object.freeze({ ...command, ordinal: 1 }),
      ),
    });

    expect(
      buildMemberGenerationBinding({
        production,
        member: result.productionMemberAuthority,
        sequence: privateSequence,
        workspace: {
          reportId: "report-1",
          taskMode: "t2va",
          requestedDurationMilliseconds: 8000,
          effectiveDurationMilliseconds: 8000,
          frameCount: 192,
          referenceIds: [],
        },
        request: {
          inputs: {
            task_mode: "t2va",
            user_intent: "fixture",
            duration_milliseconds: 8000,
            frame_count: 192,
          },
        },
      }),
    ).toBeDefined();
  });

  it.each([
    [1, 8000, 192],
    [1, 15000, 362],
    [2, 8000, 192],
  ])(
    "decodes every project-member state at ordinal %s and %s ms",
    (ordinal, milliseconds, frames) => {
      const wires = accumulatedLifecycleWires(
        `sha256:${"1".repeat(64)}`,
        `sha256:${"2".repeat(64)}`,
        "17",
        `00000000-0000-4000-8000-${String(ordinal).padStart(12, "0")}`,
        {
          duration_milliseconds: milliseconds,
          delivered_milliseconds: milliseconds,
          frame_count: frames,
          snapped: false,
        },
        ordinal,
      );
      for (const state of [
        "planned",
        "submitted",
        "running",
        "succeeded",
      ] as const) {
        const production = decodeProductionWorkbenchProjection(
          wires.production[state],
        );
        expect(production.segments).toHaveLength(ordinal);
        const result = decodeSequenceCoordinatorResult(
          managedMemberCoordinatorResponse(
            state === "planned" ? "prepared" : state,
            wires[state],
            wires.production[state],
            `segment_${ordinal}`,
          ),
        );
        expect(result.schema).toBe(
          "h3.context.generation_coordinator.managed_member_response.v1",
        );
        if (
          state === "planned" &&
          result.schema ===
            "h3.context.generation_coordinator.managed_member_response.v1"
        )
          expect(
            buildMemberGenerationBinding({
              production,
              member: result.productionMemberAuthority,
              sequence: result.sequence,
              workspace: {
                reportId: "report-1",
                taskMode: "t2va",
                requestedDurationMilliseconds: milliseconds,
                effectiveDurationMilliseconds: milliseconds,
                frameCount: frames,
                referenceIds: [],
              },
              request: {
                inputs: {
                  task_mode: "t2va",
                  user_intent: "fixture",
                  duration_milliseconds: milliseconds,
                  frame_count: frames,
                },
              },
            }),
          ).toBeDefined();
      }
    },
  );
});
