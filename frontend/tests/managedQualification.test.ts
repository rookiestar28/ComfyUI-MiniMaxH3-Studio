import { describe, expect, it, vi } from "vitest";
import {
  decodeManagedReadiness,
  encodeManagedReadinessAction,
  type ManagedReadinessSelection,
} from "../src/contracts/managedQualificationCodec";
import {
  createManagedQualificationClient,
  buildQualifiedManagedStartIntent,
} from "../src/host/managedQualificationActions";
import type { AutomaticPlanProjection } from "../src/contracts/productionPlanningCodec";

// Closed-wire unit vectors only. Product qualification is exercised by the backend/root journey.
const fp = (digit: string) => `sha256:${digit.repeat(64)}`;
const selection: ManagedReadinessSelection = {
  workspace_handle: `pw_${"a".repeat(40)}`,
  expected_workspace_revision: 2,
  expected_workspace_fingerprint: fp("a"),
  expected_plan_fingerprint: fp("b"),
};
function ready(requestId = "prepare.readiness") {
  return {
    schema: "h3.context.managed_readiness.v1",
    request_id: requestId,
    status: "ready",
    reason: "qualified",
    qualification_fingerprint: fp("c"),
    qualification: {
      schema: "h3.context.managed_mode_qualification.v2",
      baseline: {
        schema: "h3.context.managed_mode_qualification.v1",
        host_capability_fingerprint: fp("d"),
        compiler_fingerprint: fp("e"),
        qualified_global_modes: ["t2va", "i2va", "fl2va", "l2va", "ref2va"],
        qualified_materialization_receipts: [fp("1"), fp("2")],
        observed_at: "4024000000000000",
        expires_at: "4051800000000000",
      },
      production_plan_fingerprint: fp("b"),
      composition_fingerprints: [fp("3"), fp("4")],
      guide_readiness: ["incomplete", "modified"],
      host_profile_fingerprint: fp("5"),
      asset_resolution_fingerprint: fp("6"),
    },
  };
}

describe("managed qualification closed boundary", () => {
  it("requires explicit prepare/read and rejects browser-supplied authority", () => {
    expect(
      encodeManagedReadinessAction(
        "prepare.readiness",
        "prepare_managed_readiness",
        selection,
      ).payload,
    ).toEqual(selection);
    expect(() =>
      encodeManagedReadinessAction(
        "prepare.readiness",
        "prepare_managed_readiness",
        { ...selection, ready: true } as ManagedReadinessSelection,
      ),
    ).toThrow();
    expect(() =>
      encodeManagedReadinessAction(
        "read.readiness",
        "read_managed_readiness",
        selection,
      ),
    ).toThrow();
  });

  it("preserves independent guide dispositions and does not retain mutable wire aliases", () => {
    const wire = ready();
    const decoded = decodeManagedReadiness(wire);
    wire.qualification.guide_readiness[0] = "ready";
    expect(decoded.qualification?.guide_readiness).toEqual([
      "incomplete",
      "modified",
    ]);
  });

  it.each(["v1", "mode", "receipt", "composition", "guide", "extra"])(
    "rejects incomplete %s claims",
    (change) => {
      const wire = ready();
      if (change === "v1")
        wire.qualification.schema = "h3.context.managed_mode_qualification.v1";
      if (change === "mode")
        wire.qualification.baseline.qualified_global_modes.pop();
      if (change === "receipt")
        wire.qualification.baseline.qualified_materialization_receipts
          .reverse()
          .pop();
      if (change === "composition")
        wire.qualification.composition_fingerprints.pop();
      if (change === "guide")
        wire.qualification.guide_readiness[0] = "approved";
      if (change === "extra")
        Object.assign(wire.qualification, { private_path: "untrusted" });
      expect(() => decodeManagedReadiness(wire)).toThrow();
    },
  );

  it("held responses cannot retain a stale executable fingerprint", () => {
    const wire = {
      ...ready(),
      status: "held",
      reason: "qualification_expired",
      qualification: null,
    };
    expect(() => decodeManagedReadiness(wire)).toThrow();
    expect(
      decodeManagedReadiness({ ...wire, qualification_fingerprint: null })
        .status,
    ).toBe("held");
  });

  it("performs no request before explicit action and never retries a hold", async () => {
    const fetchApi = vi.fn(async (_path: string, _init: RequestInit) => ({
      ok: true,
      status: 200,
      json: async () => ({
        schema: "h3.context.managed_readiness.v1",
        request_id: "prepare.readiness",
        status: "held",
        reason: "qualification_host_unqualified",
        qualification_fingerprint: null,
        qualification: null,
      }),
    }));
    const client = createManagedQualificationClient({ fetchApi });
    expect(fetchApi).not.toHaveBeenCalled();
    expect(
      (
        await client.send(
          "prepare.readiness",
          "prepare_managed_readiness",
          selection,
        )
      ).status,
    ).toBe("held");
    expect(fetchApi).toHaveBeenCalledOnce();
    expect(fetchApi.mock.calls[0]?.[0]).toBe(
      "/h3-context/v1/production/planning/action",
    );
  });

  it("joins exact ordered import receipts before constructing the real runner intent", () => {
    const plan: AutomaticPlanProjection = {
      schema: "h3.context.production_automatic_plan_projection.v1",
      request_id: "import.plan",
      workspace_id: "workspace.plan",
      proposal_id: "proposal.plan",
      proposal_revision: 1,
      proposal_fingerprint: fp("7"),
      plan_fingerprint: selection.expected_plan_fingerprint,
      workspace_revision: selection.expected_workspace_revision,
      workspace_fingerprint: selection.expected_workspace_fingerprint,
      segment_ids: ["segment.one", "segment.two"],
      materialization_receipt_fingerprints: [fp("1"), fp("2")],
      cut_boundary_receipts: [],
      manifest_fingerprints: [fp("8"), fp("9")],
      reconstruction_order: ["segment.one", "segment.two"],
      start_hold_codes: ["managed_execution_qualification_pending"],
      startable: false,
    };
    const intent = buildQualifiedManagedStartIntent(
      decodeManagedReadiness(ready()),
      selection,
      plan,
      fp("6"),
    );
    expect(intent.authorization.host_capability_fingerprint).toBe(fp("d"));
    expect(intent.segmentIds).toEqual(plan.segment_ids);
    expect(() =>
      buildQualifiedManagedStartIntent(
        decodeManagedReadiness(ready()),
        { ...selection, workspace_handle: "foreign-workspace" },
        plan,
        fp("6"),
      ),
    ).toThrow();
    expect(() =>
      buildQualifiedManagedStartIntent(
        decodeManagedReadiness(ready()),
        selection,
        {
          ...plan,
          materialization_receipt_fingerprints: [
            ...plan.materialization_receipt_fingerprints,
          ].reverse(),
        },
        fp("6"),
      ),
    ).toThrow();
  });
});
