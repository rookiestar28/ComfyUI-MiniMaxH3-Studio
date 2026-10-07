import { describe, expect, it, vi } from "vitest";

import {
  COORDINATOR_ERROR_SCHEMA,
  MANAGED_RUN_ACTIONS,
  SequenceCoordinatorClientError,
  createSequenceCoordinatorClient,
  decodeSequenceCoordinatorResult,
} from "../src/host/sequenceCoordinator";
import {
  managedCoordinatorResponse,
  managedLifecycleWires,
} from "./support/managedLifecycleWire";

function compactChildResponse(): Record<string, unknown> {
  const wires = managedLifecycleWires(`sha256:${"1".repeat(64)}`);
  const wire = managedCoordinatorResponse(
    "current",
    wires.running,
    wires.production.running,
  );
  delete wire.production;
  return {
    ...wire,
    schema: "h3.context.generation_coordinator.managed_child_response.v1",
    production_authority: {
      schema: "h3.context.generation_coordinator.production_authority.v1",
      workspace_handle: wires.production.running.workspace_handle,
      workspace_id: wires.running.workspace_id,
      workspace_revision: wires.running.workspace_revision,
      workspace_fingerprint: wires.running.workspace_fingerprint,
      automatic_plan_fingerprint: `sha256:${"7".repeat(64)}`,
    },
  };
}

describe("compact original-child coordinator authority", () => {
  it("decodes the actual child authority without fabricating a Production projection", () => {
    const wire = compactChildResponse();
    expect(
      new TextEncoder().encode(JSON.stringify(wire)).byteLength,
    ).toBeLessThan(8192);
    expect(decodeSequenceCoordinatorResult(wire)).toMatchObject({
      schema: wire.schema,
      runHandle: wire.run_handle,
      production: null,
      productionAuthority: {
        workspaceHandle: `pw_${"p".repeat(43)}`,
        automaticPlanFingerprint: `sha256:${"7".repeat(64)}`,
      },
    });
  });

  it("retains exact artifact and terminal identities through the normal client", async () => {
    const wire = compactChildResponse();
    wire.artifact_authority = {
      schema: "h3.context.generation_coordinator.artifact_authority.v1",
      receipt_fingerprint: `sha256:${"8".repeat(64)}`,
      byte_length: 123,
    };
    wire.terminal_fingerprint = `sha256:${"9".repeat(64)}`;
    const client = createSequenceCoordinatorClient({
      fetchApi: async () => ({ ok: true, status: 200, json: async () => wire }),
    });
    await expect(
      client.send("child.read", "read_sequence", {
        run_handle: wire.run_handle,
      }),
    ).resolves.toMatchObject({
      production: null,
      artifactAuthority: {
        byteLength: 123,
        receiptFingerprint: `sha256:${"8".repeat(64)}`,
      },
      terminalFingerprint: `sha256:${"9".repeat(64)}`,
    });
  });

  it.each([
    ["workspace_id", "foreign-workspace"],
    ["workspace_revision", 2],
    ["workspace_fingerprint", `sha256:${"6".repeat(64)}`],
    ["workspace_handle", "invalid"],
    ["automatic_plan_fingerprint", "invalid"],
    ["private_path", "unrequested"],
  ])("rejects invalid or cross-authority %s", (key, value) => {
    const wire = compactChildResponse();
    (wire.production_authority as Record<string, unknown>)[key] = value;
    expect(() => decodeSequenceCoordinatorResult(wire)).toThrow(
      SequenceCoordinatorClientError,
    );
  });

  it("rejects mixed legacy and compact envelopes", () => {
    const wire = compactChildResponse();
    wire.production = null;
    expect(() => decodeSequenceCoordinatorResult(wire)).toThrow(
      SequenceCoordinatorClientError,
    );
    delete wire.production;
    wire.schema = "h3.context.generation_coordinator.response.v1";
    expect(() => decodeSequenceCoordinatorResult(wire)).toThrow(
      SequenceCoordinatorClientError,
    );
  });
});

function projectMemberResponse(): Record<string, unknown> {
  const wires = managedLifecycleWires(`sha256:${"1".repeat(64)}`);
  const wire = managedCoordinatorResponse(
    "current",
    wires.running,
    wires.production.running,
  );
  delete wire.production;
  const progress = wires.running.progress as ReadonlyArray<
    Record<string, unknown>
  >;
  return {
    ...wire,
    schema: "h3.context.generation_coordinator.managed_member_response.v1",
    production_member_authority: {
      schema:
        "h3.context.generation_coordinator.production_member_authority.v1",
      workspace_handle: wires.production.running.workspace_handle,
      // The project id differs from the member's own execution workspace id.
      workspace_id: "workspace_project_accumulating",
      member_segment_id: progress[0].segment_id,
    },
  };
}

describe("accumulating Production member coordinator authority", () => {
  it("decodes a member reference joined to its one-job execution sequence", () => {
    const wire = projectMemberResponse();
    const authority = wire.production_member_authority as Record<
      string,
      unknown
    >;
    expect(
      new TextEncoder().encode(JSON.stringify(wire)).byteLength,
    ).toBeLessThan(8192);
    const result = decodeSequenceCoordinatorResult(wire);
    expect(result).toMatchObject({
      schema: wire.schema,
      runHandle: wire.run_handle,
      production: null,
      productionMemberAuthority: {
        workspaceHandle: authority.workspace_handle,
        workspaceId: "workspace_project_accumulating",
        memberSegmentId: authority.member_segment_id,
      },
    });
    expect(result).not.toHaveProperty("productionAuthority");
    expect(Object.isFrozen(result)).toBe(true);
  });

  it.each([
    ["workspace_handle", "ws_not_a_production_handle_value_000000000"],
    ["workspace_id", "-invalid"],
    ["member_segment_id", "segment_not_in_this_sequence"],
    ["schema", "h3.context.generation_coordinator.production_authority.v1"],
    ["private_path", "unrequested"],
  ])("rejects invalid or cross-authority %s", (key, value) => {
    const wire = projectMemberResponse();
    (wire.production_member_authority as Record<string, unknown>)[key] = value;
    expect(() => decodeSequenceCoordinatorResult(wire)).toThrow(
      SequenceCoordinatorClientError,
    );
  });

  it("refuses a sequence that is not exactly one job for the member", () => {
    const wire = projectMemberResponse();
    const sequence = wire.sequence as Record<string, unknown>;
    const [job] = sequence.progress as ReadonlyArray<Record<string, unknown>>;
    sequence.progress = [
      job,
      {
        ...job,
        job_id: "job_second",
        segment_id: "segment_second",
        ordinal: 1,
      },
    ];
    expect(() => decodeSequenceCoordinatorResult(wire)).toThrow(
      SequenceCoordinatorClientError,
    );
  });

  it("refuses mixed envelopes and an oversized member response", () => {
    const mixed = projectMemberResponse();
    mixed.production = null;
    expect(() => decodeSequenceCoordinatorResult(mixed)).toThrow(
      SequenceCoordinatorClientError,
    );
    const child = projectMemberResponse();
    child.production_authority = child.production_member_authority;
    delete child.production_member_authority;
    expect(() => decodeSequenceCoordinatorResult(child)).toThrow(
      SequenceCoordinatorClientError,
    );
    const legacy = projectMemberResponse();
    legacy.schema = "h3.context.generation_coordinator.response.v1";
    expect(() => decodeSequenceCoordinatorResult(legacy)).toThrow(
      SequenceCoordinatorClientError,
    );
    // The compact envelope bound is checked on the raw wire, before any member decoding.
    const oversized = projectMemberResponse();
    const sequence = oversized.sequence as Record<string, unknown>;
    sequence.progress = Array.from({ length: 64 }, () =>
      structuredClone((sequence.progress as unknown[])[0]),
    );
    expect(
      new TextEncoder().encode(JSON.stringify(oversized)).byteLength,
    ).toBeGreaterThan(8192);
    expect(() => decodeSequenceCoordinatorResult(oversized)).toThrow(
      SequenceCoordinatorClientError,
    );
  });
});

describe("managed sequence coordinator client", () => {
  it("exposes the aggregate lifecycle vocabulary and treats prepare as run creation", async () => {
    expect(MANAGED_RUN_ACTIONS).toEqual([
      "prepare_managed_run",
      "submit_managed_run",
      "close_managed_run",
      "read_managed_run",
    ]);
    const wires = managedLifecycleWires(`sha256:${"1".repeat(64)}`);
    const response = managedCoordinatorResponse(
      "current",
      wires.running,
      wires.production.running,
    );
    const fetchApi = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => response,
    }));
    const client = createSequenceCoordinatorClient({ fetchApi });

    await expect(
      client.send("managed.prepare.aggregate", "prepare_managed_run", {
        context_workspace_handle: `ws_${"w".repeat(40)}`,
        correlation: {
          prompt_id: "prompt.bootstrap",
          execution_node_id: "node.product.shell",
        },
        observation: {},
      }),
    ).resolves.toMatchObject({ disposition: "current" });
  });

  it("preserves the closed safe artifact failure contract from a non-success response", async () => {
    const json = vi.fn(async () => ({
      schema: COORDINATOR_ERROR_SCHEMA,
      category: "artifact_store_unavailable",
      retry_disposition: "retry_output_verification",
      same_run_authority: true,
    }));
    const fetchApi = vi.fn(async () => ({ ok: false, status: 503, json }));
    const client = createSequenceCoordinatorClient({ fetchApi });

    let failure: unknown;
    try {
      await client.send("managed.artifact.failure.1", "read_sequence", {
        run_handle: `mc_${"m".repeat(40)}`,
      });
    } catch (error) {
      failure = error;
    }

    expect(failure).toBeInstanceOf(SequenceCoordinatorClientError);
    expect(failure).toMatchObject({
      code: "artifact_store_unavailable",
      category: "artifact_store_unavailable",
      retryDisposition: "retry_output_verification",
      sameRunAuthority: true,
      status: 503,
    });
    expect(json).toHaveBeenCalledOnce();
    expect(fetchApi).toHaveBeenCalledWith(
      "/h3-context/v1/generation/coordinator",
      expect.objectContaining({
        method: "POST",
        credentials: "same-origin",
      }),
    );
  });

  it("rejects an error envelope carrying private members and uses only the fixed status map", async () => {
    const privateDetail = "sensitive-marker-that-must-not-propagate";
    const json = vi.fn(async () => ({
      schema: COORDINATOR_ERROR_SCHEMA,
      category: "artifact_store_unavailable",
      retry_disposition: "retry_output_verification",
      same_run_authority: true,
      private_path: privateDetail,
    }));
    const fetchApi = vi.fn(async () => ({ ok: false, status: 422, json }));
    const client = createSequenceCoordinatorClient({ fetchApi });

    let failure: unknown;
    try {
      await client.send("managed.artifact.failure.private", "read_sequence", {
        run_handle: `mc_${"m".repeat(40)}`,
      });
    } catch (error) {
      failure = error;
    }

    expect(failure).toBeInstanceOf(SequenceCoordinatorClientError);
    expect(failure).toMatchObject({
      code: "coordinator_action_rejected",
      category: "internal_failure",
      retryDisposition: "none",
      sameRunAuthority: false,
      status: 422,
    });
    expect(String(failure)).not.toContain(privateDetail);
    expect(failure).not.toHaveProperty("private_path");
  });

  it("rejects a decoded success response owned by a different managed run", async () => {
    const expectedRunHandle = `mc_${"m".repeat(40)}`;
    const wires = managedLifecycleWires(`sha256:${"1".repeat(64)}`);
    const response = managedCoordinatorResponse(
      "current",
      wires.running,
      wires.production.running,
    );
    response.run_handle = `mc_${"f".repeat(40)}`;
    const fetchApi = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => response,
    }));
    const client = createSequenceCoordinatorClient({ fetchApi });

    await expect(
      client.send("managed.read.cross-run", "read_sequence", {
        run_handle: expectedRunHandle,
      }),
    ).rejects.toMatchObject({
      code: "cross_run_authority_response",
      category: "run_authority_mismatch",
      retryDisposition: "none",
      sameRunAuthority: false,
      status: 500,
    });
  });

  it("decodes only locator-free artifact authority and the observed terminal proof", async () => {
    const wires = managedLifecycleWires(`sha256:${"1".repeat(64)}`);
    const response = managedCoordinatorResponse(
      "succeeded",
      wires.succeeded,
      wires.production.succeeded,
    );
    response.artifact_authority = {
      schema: "h3.context.generation_coordinator.artifact_authority.v1",
      receipt_fingerprint: `sha256:${"a".repeat(64)}`,
      byte_length: 23,
    };
    response.terminal_fingerprint = `sha256:${"b".repeat(64)}`;
    const client = createSequenceCoordinatorClient({
      fetchApi: async () => ({
        ok: true,
        status: 200,
        json: async () => response,
      }),
    });

    await expect(
      client.send("managed.read.authority", "read_sequence", {
        run_handle: response.run_handle,
      }),
    ).resolves.toMatchObject({
      artifactAuthority: {
        receiptFingerprint: `sha256:${"a".repeat(64)}`,
        byteLength: 23,
      },
      terminalFingerprint: `sha256:${"b".repeat(64)}`,
    });

    (response.artifact_authority as Record<string, unknown>).private_path =
      "must-not-cross";
    await expect(
      client.send("managed.read.private-authority", "read_sequence", {
        run_handle: response.run_handle,
      }),
    ).rejects.toMatchObject({ code: "invalid_response", status: 500 });
  });
});
