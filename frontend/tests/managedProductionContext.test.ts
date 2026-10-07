import { describe, expect, it } from "vitest";

import { decodeProductionWorkbenchProjection } from "../src/contracts/productionWorkbenchCodec";
import {
  matchesAdmittedMemberProduction,
  matchesCreatedProductionContext,
} from "../src/host/managedProductionContext";
import { managedLifecycleWires } from "./support/managedLifecycleWire";

const graphFingerprint = `sha256:${"1".repeat(64)}`;

function subject(
  mutate?: (wire: Record<string, unknown>) => void,
): ReturnType<typeof decodeProductionWorkbenchProjection> {
  const wire = structuredClone(
    managedLifecycleWires(graphFingerprint).initialProduction,
  );
  mutate?.(wire);
  return decodeProductionWorkbenchProjection(wire);
}

const context = {
  contextWorkspaceId: `ws_${"m".repeat(40)}`,
  taskMode: "t2va" as const,
  effectiveDurationMilliseconds: 8000,
  frameCount: 192,
};

describe("managed Production create binding", () => {
  it("accepts the real distinct Context and Production namespaces", () => {
    const production = subject();
    expect(production.workspaceId).toMatch(/^workspace_/);
    expect(production.workspaceId).not.toBe(context.contextWorkspaceId);
    expect(matchesCreatedProductionContext(production, context)).toBe(true);
  });

  it("accepts a snapped Production duration from the effective workspace authority", () => {
    const production = subject((wire) => {
      const duration = (wire.segments as Array<Record<string, unknown>>)[0]!
        .duration as Record<string, unknown>;
      duration.duration_milliseconds = 5167;
      duration.delivered_milliseconds = 5167;
      duration.frame_count = 124;
    });
    const snappedContext = {
      contextWorkspaceId: context.contextWorkspaceId,
      taskMode: "t2va" as const,
      effectiveDurationMilliseconds: 5167,
      frameCount: 124,
    };

    expect(matchesCreatedProductionContext(production, snappedContext)).toBe(
      true,
    );
  });

  it.each([
    [
      "aliased Context identity",
      (wire: Record<string, unknown>) => {
        wire.workspace_id = context.contextWorkspaceId;
      },
    ],
    [
      "unselected initial segment",
      (wire: Record<string, unknown>) => {
        wire.selected_segment_ids = [];
      },
    ],
    [
      "different task mode",
      (wire: Record<string, unknown>) => {
        (wire.segments as Array<Record<string, unknown>>)[0]!.task_mode =
          "i2va";
      },
    ],
    [
      "different effective duration",
      (wire: Record<string, unknown>) => {
        const duration = (wire.segments as Array<Record<string, unknown>>)[0]!
          .duration as Record<string, unknown>;
        duration.duration_milliseconds = 7000;
        duration.snapped = true;
      },
    ],
    [
      "different frame count",
      (wire: Record<string, unknown>) => {
        const duration = (wire.segments as Array<Record<string, unknown>>)[0]!
          .duration as Record<string, unknown>;
        duration.frame_count = 193;
      },
    ],
    [
      "non-independent relation",
      (wire: Record<string, unknown>) => {
        const segment = (wire.segments as Array<Record<string, unknown>>)[0]!;
        segment.relation = "cut";
        segment.boundary_kind = "cut";
      },
    ],
  ])("rejects %s", (_name, mutate) => {
    expect(matchesCreatedProductionContext(subject(mutate), context)).toBe(
      false,
    );
  });
});

describe("M25-36 managed Production member binding", () => {
  const wires = managedLifecycleWires(graphFingerprint);
  const member = {
    workspaceHandle: String(wires.production.planned.workspace_handle),
    workspaceId: String(wires.production.planned.workspace_id),
    memberSegmentId: "segment_1",
    taskMode: "t2va" as const,
    effectiveDurationMilliseconds: 8000,
    frameCount: 192,
  };

  function memberSubject(
    mutate?: (wire: Record<string, unknown>) => void,
  ): ReturnType<typeof decodeProductionWorkbenchProjection> {
    const wire = structuredClone(wires.production.planned);
    mutate?.(wire);
    return decodeProductionWorkbenchProjection(wire);
  }

  it("accepts the admitted project member selected for one planned job", () => {
    expect(matchesAdmittedMemberProduction(memberSubject(), member)).toBe(true);
  });

  it.each([
    [
      "another project handle",
      { ...member, workspaceHandle: `pw_${"x".repeat(43)}` },
    ],
    ["another project id", { ...member, workspaceId: "workspace_other" }],
    ["another member", { ...member, memberSegmentId: "segment_other" }],
    ["another task mode", { ...member, taskMode: "i2va" as const }],
    ["another duration", { ...member, effectiveDurationMilliseconds: 9000 }],
    ["another frame count", { ...member, frameCount: 216 }],
  ])("rejects %s", (_name, authority) => {
    expect(matchesAdmittedMemberProduction(memberSubject(), authority)).toBe(
      false,
    );
  });

  it("rejects a member that is no longer planned or selected", () => {
    expect(
      matchesAdmittedMemberProduction(
        memberSubject((wire) => {
          const segment = (wire.segments as Array<Record<string, unknown>>)[0]!;
          segment.job_state = "running";
          wire.selected_segment_ids = [];
        }),
        member,
      ),
    ).toBe(false);
  });
});
