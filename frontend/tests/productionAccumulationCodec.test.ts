import { describe, expect, it } from "vitest";

import {
  decodeProductionAccumulatedProject,
  productionAccumulatedProjectFingerprint,
} from "../src/contracts/productionAccumulationCodec";
import { managedLifecycleWires } from "./support/managedLifecycleWire";

const workspace = managedLifecycleWires(`sha256:${"1".repeat(64)}`).production
  .succeeded;

function accumulatedWire(overrides: Record<string, unknown> = {}) {
  const material = {
    schema: "h3.context.production_accumulated_project.v1",
    workspace_handle: workspace.workspace_handle,
    workspace_id: workspace.workspace_id,
    project_revision: 3,
    workspace,
    attempts: [
      {
        candidate_id: "segment_1",
        attempt_id: "attempt_1",
        member_segment_id: "segment_1",
        status: "succeeded",
        recovery: null,
        committed: true,
      },
    ],
    capabilities: ["read", "admit_generation", "release_generation"],
    ...overrides,
  };
  return {
    ...material,
    project_fingerprint: productionAccumulatedProjectFingerprint(material),
  };
}

describe("M25-38 accumulated Production project codec", () => {
  it("decodes strict empty and nonempty envelopes with verified project CAS", () => {
    const nonempty = decodeProductionAccumulatedProject(accumulatedWire());
    expect(nonempty.workspace?.segments).toHaveLength(1);
    expect(nonempty.attempts[0]).toMatchObject({
      status: "succeeded",
      committed: true,
    });

    const empty = decodeProductionAccumulatedProject(
      accumulatedWire({
        workspace: null,
        attempts: [
          {
            candidate_id: "segment_future",
            attempt_id: "attempt_future",
            member_segment_id: "segment_future",
            status: "failed",
            recovery: "retry",
            committed: false,
          },
        ],
      }),
    );
    expect(empty.workspace).toBeNull();
    expect(empty.attempts[0]?.recovery).toBe("retry");
  });

  it("rejects unknown fields, bad fingerprints, invalid commit states and identity drift", () => {
    expect(() =>
      decodeProductionAccumulatedProject({ ...accumulatedWire(), extra: true }),
    ).toThrow("closed object");
    expect(() =>
      decodeProductionAccumulatedProject({
        ...accumulatedWire(),
        project_fingerprint: `sha256:${"f".repeat(64)}`,
      }),
    ).toThrow("fingerprint mismatch");
    expect(() =>
      decodeProductionAccumulatedProject(
        accumulatedWire({
          attempts: [
            {
              candidate_id: "segment_1",
              attempt_id: "attempt_1",
              member_segment_id: "segment_1",
              status: "failed",
              recovery: "retry",
              committed: true,
            },
          ],
        }),
      ),
    ).toThrow("attempt state");
    expect(() =>
      decodeProductionAccumulatedProject(
        accumulatedWire({ workspace_id: "workspace_foreign" }),
      ),
    ).toThrow("identity mismatch");
  });
});
