import { describe, expect, it, vi } from "vitest";
import compositionFixture from "../../tests/fixtures/m25_10_composition_contract_v1.json";

import { publicCompositionFingerprint } from "../src/contracts/compositionCodec";
import {
  AuthoringClientError,
  createAuthoringActionClient,
} from "../src/host/authoringActions";
import {
  encodeTimelineTransaction,
  NLE_AUTHORING_PROFILE_ID,
  NLE_AUTHORING_SCHEMA,
  NLE_OPERATION_PROFILE_ID_V2,
} from "../src/contracts/authoringWorkbenchCodec";
import { projectionWire, snapWire } from "./support/authoringFixture";

function fetchStub(status: number, body?: unknown) {
  return vi.fn(async (_path: string, _init: RequestInit) => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  }));
}

function timelineReceiptWire(): Record<string, unknown> {
  const snapshot = JSON.parse(
    JSON.stringify(compositionFixture.snapshot),
  ) as Record<string, unknown>;
  const historyCursor = `h3.context.timeline_history_cursor.v1:1:${"a".repeat(64)}`;
  return {
    schema: "h3.context.timeline_receipt.v1",
    request_id: "req-history-1",
    transaction_id: "tx-history-1",
    workspace_handle: snapshot.workspace_handle,
    before_workspace_revision: 6,
    after_workspace_revision: snapshot.workspace_revision,
    before_workspace_fingerprint: `sha256:${"0".repeat(64)}`,
    after_workspace_fingerprint: snapshot.workspace_fingerprint,
    before_timeline_revision: 10,
    after_timeline_revision: snapshot.timeline_revision,
    before_timeline_fingerprint: `sha256:${"0".repeat(64)}`,
    after_timeline_fingerprint: snapshot.timeline_fingerprint,
    commands: [{ kind: "select_clips", payload: { clip_ids: ["clip-main"] } }],
    affected_ids: ["clip-main"],
    inverse: {
      kind: "restore_transaction_state",
      history_cursor: historyCursor,
    },
    history_cursor: historyCursor,
    selection: ["clip-main"],
    snapshot,
  };
}

function historyProjectionWire(
  rejection: Record<string, unknown> | null = null,
): Record<string, unknown> {
  const snapshot = JSON.parse(
    JSON.stringify(compositionFixture.snapshot),
  ) as Record<string, unknown>;
  return {
    schema: "h3.context.timeline_history_projection.v1",
    workspace_handle: snapshot.workspace_handle,
    snapshot,
    selection: ["clip-main"],
    undo_cursor: `h3.context.timeline_history_cursor.v1:1:${"a".repeat(64)}`,
    redo_cursor: null,
    rejection,
  };
}

function timelineTransaction() {
  return encodeTimelineTransaction({
    requestId: "req-history-1",
    transactionId: "tx-history-1",
    workspaceHandle: compositionFixture.snapshot.workspace_handle,
    expectedWorkspaceRevision: 6,
    expectedTimelineRevision: 10,
    expectedTimelineFingerprint: `sha256:${"0".repeat(64)}`,
    commands: [{ kind: "select_clips", payload: { clip_ids: ["clip-main"] } }],
  });
}

function timelineInitializationPayload() {
  return {
    workspace_handle: "workspace-fixture",
    expected_reference_revision: 4,
    expected_timeline_revision: 1,
    authoring_schema: NLE_AUTHORING_SCHEMA,
    profile_id: NLE_AUTHORING_PROFILE_ID,
    operation_profile_id: NLE_OPERATION_PROFILE_ID_V2,
  };
}

describe("authoring action client", () => {
  it("refuses construction without the same-origin seam", () => {
    expect(() =>
      createAuthoringActionClient({
        fetchApi: undefined as unknown as Parameters<
          typeof createAuthoringActionClient
        >[0]["fetchApi"],
      }),
    ).toThrow(/same-origin authoring seam/);
  });

  it("posts the closed envelope to the authoring route and decodes 200", async () => {
    const fetchApi = fetchStub(200, projectionWire());
    const client = createAuthoringActionClient({ fetchApi });
    const result = await client.send("req-1", "read_projection", {
      workspace_handle: "authoring-abc123",
    });
    expect(result.status).toBe(200);
    expect(result.projection?.workspaceHandle).toBe("authoring-abc123");
    const [path, init] = fetchApi.mock.calls[0] ?? [];
    expect(path).toBe("/h3-context/v1/authoring/action");
    expect(init?.method).toBe("POST");
    expect(init?.credentials).toBe("same-origin");
    const body = JSON.parse(String(init?.body));
    expect(body.schema).toBe("h3.context.authoring_workbench.action.v1");
    expect(body.request_id).toBe("req-1");
    expect(body.action).toBe("read_projection");
  });

  it("decodes a 201 creation response", async () => {
    const client = createAuthoringActionClient({
      fetchApi: fetchStub(201, projectionWire()),
    });
    const result = await client.send("req-1", "create_authoring_workspace", {
      context_workspace_handle: "ctx-1",
    });
    expect(result.status).toBe(201);
    expect(result.projection?.reference.revision).toBe(4);
  });

  it("returns a 409 rejection as data, not an error", async () => {
    const client = createAuthoringActionClient({
      fetchApi: fetchStub(
        409,
        projectionWire({ rejection: { code: "revision_conflict" } }),
      ),
    });
    const result = await client.send(
      "req-1",
      "move_clip",
      { workspace_handle: "authoring-abc123" },
      "authoring-abc123",
    );
    expect(result.status).toBe(409);
    expect(result.projection?.rejection?.code).toBe("revision_conflict");
  });

  it("passes a 204 release through without reading a body", async () => {
    const json = vi.fn();
    const client = createAuthoringActionClient({
      fetchApi: vi.fn(async (_path: string, _init: RequestInit) => ({
        ok: true,
        status: 204,
        json,
      })),
    });
    const result = await client.send("req-1", "release_workspace", {
      workspace_handle: "authoring-abc123",
    });
    expect(result.status).toBe(204);
    expect(result.projection).toBeUndefined();
    expect(json).not.toHaveBeenCalled();
  });

  it("rejects a bodiless 204 for a timeline action", async () => {
    const client = createAuthoringActionClient({ fetchApi: fetchStub(204) });
    await expect(
      client.send(
        "req-history-read",
        "read_timeline_history",
        { workspace_handle: "workspace-fixture" },
        "workspace-fixture",
      ),
    ).rejects.toMatchObject({ code: "unexpected_status", status: 204 });
  });

  it("decodes the snap body for read_snap", async () => {
    const client = createAuthoringActionClient({
      fetchApi: fetchStub(200, snapWire()),
    });
    const result = await client.send(
      "req-1",
      "read_snap",
      { workspace_handle: "authoring-abc123", frame: 50 },
      "authoring-abc123",
    );
    expect(result.snap?.candidates[0]?.frame).toBe(51);
    expect(result.projection).toBeUndefined();
  });

  it("refuses a projection from a different workspace", async () => {
    const client = createAuthoringActionClient({
      fetchApi: fetchStub(200, projectionWire()),
    });
    await expect(
      client.send(
        "req-1",
        "read_projection",
        { workspace_handle: "authoring-other" },
        "authoring-other",
      ),
    ).rejects.toMatchObject({ code: "cross_workspace_response" });
  });

  it("maps every closed error status to its machine code", async () => {
    const table: ReadonlyArray<readonly [number, string]> = [
      [400, "invalid_action"],
      [403, "origin_rejected"],
      [404, "workspace_unavailable"],
      [410, "workspace_gone"],
      [413, "request_too_large"],
      [415, "media_type_rejected"],
      [422, "action_rejected"],
      [429, "workspace_capacity"],
      [500, "internal_failure"],
    ];
    for (const [status, code] of table) {
      const client = createAuthoringActionClient({
        fetchApi: fetchStub(status),
      });
      const failure = await client
        .send("req-1", "read_projection", {
          workspace_handle: "authoring-abc123",
        })
        .then(() => null)
        .catch((error: unknown) => error);
      expect(failure).toBeInstanceOf(AuthoringClientError);
      expect((failure as AuthoringClientError).code).toBe(code);
      expect((failure as AuthoringClientError).status).toBe(status);
    }
  });

  it("maps an unlisted status to internal_failure", async () => {
    const client = createAuthoringActionClient({ fetchApi: fetchStub(502) });
    await expect(
      client.send("req-1", "read_projection", {
        workspace_handle: "authoring-abc123",
      }),
    ).rejects.toMatchObject({ code: "internal_failure", status: 502 });
  });

  it("wraps a rejected fetch as transport_failure so a lost reply is never read as a rejection", async () => {
    // M25-16 corrective F1: a network drop or abort leaves the outcome unknown. The session
    // must be able to tell it from every decoded status, which all keep their own codes.
    for (const failure of [
      new TypeError("Failed to fetch"),
      Object.assign(new Error("aborted"), { name: "AbortError" }),
    ]) {
      const fetchApi = vi.fn(async () => {
        throw failure;
      });
      const client = createAuthoringActionClient({ fetchApi });
      const rejection = client.send("req-1", "read_projection", {
        workspace_handle: "authoring-abc123",
      });
      await expect(rejection).rejects.toBeInstanceOf(AuthoringClientError);
      await expect(rejection).rejects.toMatchObject({
        code: "transport_failure",
        status: 0,
      });
      expect(fetchApi).toHaveBeenCalledTimes(1);
    }
  });

  it("rejects an invalid request id before any network call", async () => {
    const fetchApi = fetchStub(200, projectionWire());
    const client = createAuthoringActionClient({ fetchApi });
    await expect(
      client.send("bad id", "read_projection", {
        workspace_handle: "authoring-abc123",
      }),
    ).rejects.toThrow(/request id/);
    expect(fetchApi).not.toHaveBeenCalled();
  });

  it("decodes an accepted timeline receipt and posts the exact transaction", async () => {
    const fetchApi = fetchStub(200, timelineReceiptWire());
    const client = createAuthoringActionClient({ fetchApi });
    const result = await client.send(
      "req-history-1",
      "apply_timeline_transaction",
      timelineTransaction(),
      "workspace-fixture",
    );
    expect(result.receipt?.snapshot.timelineRevision).toBe(11);
    expect(result.history).toBeUndefined();
    const [_path, init] = fetchApi.mock.calls[0] ?? [];
    const wire = JSON.parse(String(init?.body));
    expect(wire.payload.schema).toBe("h3.context.timeline_transaction.v1");
    expect(wire.payload.request_id).toBe(wire.request_id);
  });

  it("returns a 409 timeline projection so the caller can discard its draft", async () => {
    const client = createAuthoringActionClient({
      fetchApi: fetchStub(
        409,
        historyProjectionWire({ code: "stale_workspace_revision" }),
      ),
    });
    const result = await client.send(
      "req-history-1",
      "apply_timeline_transaction",
      timelineTransaction(),
      "workspace-fixture",
    );
    expect(result.status).toBe(409);
    expect(result.history?.rejection?.code).toBe("stale_workspace_revision");
    expect(result.receipt).toBeUndefined();
  });

  it("rejects timeline history whose HTTP status and rejection body disagree", async () => {
    const rejectedRead = createAuthoringActionClient({
      fetchApi: fetchStub(
        200,
        historyProjectionWire({ code: "stale_workspace_revision" }),
      ),
    });
    await expect(
      rejectedRead.send(
        "req-history-read",
        "read_timeline_history",
        { workspace_handle: "workspace-fixture" },
        "workspace-fixture",
      ),
    ).rejects.toMatchObject({ code: "timeline_response_mismatch" });

    const rejectionMissing = createAuthoringActionClient({
      fetchApi: fetchStub(409, historyProjectionWire()),
    });
    await expect(
      rejectionMissing.send(
        "req-history-1",
        "apply_timeline_transaction",
        timelineTransaction(),
        "workspace-fixture",
      ),
    ).rejects.toMatchObject({ code: "timeline_response_mismatch" });
  });

  it("decodes history reads and rejects a receipt rebound to another workspace", async () => {
    const readClient = createAuthoringActionClient({
      fetchApi: fetchStub(200, historyProjectionWire()),
    });
    const read = await readClient.send(
      "req-history-read",
      "read_timeline_history",
      { workspace_handle: "workspace-fixture" },
      "workspace-fixture",
    );
    expect(read.history?.snapshot.workspaceHandle).toBe("workspace-fixture");

    const crossWorkspace = timelineReceiptWire();
    crossWorkspace.workspace_handle = "another-workspace";
    const writeClient = createAuthoringActionClient({
      fetchApi: fetchStub(200, crossWorkspace),
    });
    await expect(
      writeClient.send(
        "req-history-1",
        "apply_timeline_transaction",
        timelineTransaction(),
        "workspace-fixture",
      ),
    ).rejects.toMatchObject({ code: "cross_workspace_response" });
  });

  it("decodes a successful timeline history initialization for the requested workspace", async () => {
    const fetchApi = fetchStub(200, historyProjectionWire());
    const client = createAuthoringActionClient({ fetchApi });
    const result = await client.send(
      "req-history-init",
      "initialize_timeline_history",
      timelineInitializationPayload(),
      "workspace-fixture",
    );
    expect(result.status).toBe(200);
    expect(result.history?.workspaceHandle).toBe("workspace-fixture");
    expect(result.history?.rejection).toBeNull();
    expect(result.projection).toBeUndefined();

    const [_path, init] = fetchApi.mock.calls[0] ?? [];
    expect(JSON.parse(String(init?.body))).toMatchObject({
      request_id: "req-history-init",
      action: "initialize_timeline_history",
      payload: {
        workspace_handle: "workspace-fixture",
        expected_reference_revision: 4,
        expected_timeline_revision: 1,
        authoring_schema: NLE_AUTHORING_SCHEMA,
        profile_id: NLE_AUTHORING_PROFILE_ID,
        operation_profile_id: NLE_OPERATION_PROFILE_ID_V2,
      },
    });
  });

  it("maps initialization conflict without consuming an untrusted response body", async () => {
    const json = vi.fn(async () => ({ hidden_snapshot: {} }));
    const client = createAuthoringActionClient({
      fetchApi: vi.fn(async (_path: string, _init: RequestInit) => ({
        ok: false,
        status: 409,
        json,
      })),
    });
    await expect(
      client.send(
        "req-history-init-conflict",
        "initialize_timeline_history",
        timelineInitializationPayload(),
        "workspace-fixture",
      ),
    ).rejects.toMatchObject({
      code: "timeline_initialization_conflict",
      status: 409,
    });
    expect(json).not.toHaveBeenCalled();
  });

  it("rejects an initialization projection for another workspace or with rejection data", async () => {
    const crossWorkspace = historyProjectionWire();
    crossWorkspace.workspace_handle = "another-workspace";
    const crossWorkspaceSnapshot = crossWorkspace.snapshot as Record<
      string,
      unknown
    >;
    crossWorkspaceSnapshot.workspace_handle = "another-workspace";
    crossWorkspaceSnapshot.public_fingerprint = publicCompositionFingerprint(
      crossWorkspaceSnapshot,
    );
    const crossClient = createAuthoringActionClient({
      fetchApi: fetchStub(200, crossWorkspace),
    });
    await expect(
      crossClient.send(
        "req-history-init-cross",
        "initialize_timeline_history",
        timelineInitializationPayload(),
        "workspace-fixture",
      ),
    ).rejects.toMatchObject({ code: "cross_workspace_response" });

    const rejectedClient = createAuthoringActionClient({
      fetchApi: fetchStub(
        200,
        historyProjectionWire({ code: "stale_timeline_revision" }),
      ),
    });
    await expect(
      rejectedClient.send(
        "req-history-init-rejected",
        "initialize_timeline_history",
        timelineInitializationPayload(),
      ),
    ).rejects.toMatchObject({ code: "timeline_response_mismatch" });
  });

  it("rejects a receipt that is not bound to the posted request and transaction CAS", async () => {
    for (const [member, value] of [
      ["request_id", "another-request"],
      ["transaction_id", "another-transaction"],
      ["before_timeline_fingerprint", `sha256:${"f".repeat(64)}`],
    ] as const) {
      const receipt = timelineReceiptWire();
      receipt[member] = value;
      const client = createAuthoringActionClient({
        fetchApi: fetchStub(200, receipt),
      });
      await expect(
        client.send(
          "req-history-1",
          "apply_timeline_transaction",
          timelineTransaction(),
          "workspace-fixture",
        ),
      ).rejects.toMatchObject({ code: "timeline_response_mismatch" });
    }
  });

  it("binds the receipt to the canonical payload actually posted on the wire", async () => {
    const raw = {
      ...timelineTransaction(),
      commands: [
        {
          kind: "select_clips",
          payload: { clip_ids: ["clip-main", "clip-image"] },
        },
      ],
    };
    const receipt = timelineReceiptWire();
    receipt.commands = [
      {
        kind: "select_clips",
        payload: { clip_ids: ["clip-image", "clip-main"] },
      },
    ];
    receipt.affected_ids = ["clip-image", "clip-main"];
    receipt.selection = ["clip-image", "clip-main"];
    const fetchApi = fetchStub(200, receipt);
    const result = await createAuthoringActionClient({ fetchApi }).send(
      "req-history-1",
      "apply_timeline_transaction",
      raw,
      "workspace-fixture",
    );
    expect(result.receipt?.selection).toEqual(["clip-image", "clip-main"]);
    const [_path, init] = fetchApi.mock.calls[0] ?? [];
    const posted = JSON.parse(String(init?.body));
    expect(posted.payload.commands[0].payload.clip_ids).toEqual([
      "clip-image",
      "clip-main",
    ]);
  });
});

describe("M25-16 corrective 02 R2-F1: outcome evidence after the response headers", () => {
  function headersThenBody(status: number, failure: Error) {
    return vi.fn(async (_path: string, _init: RequestInit) => ({
      ok: status >= 200 && status < 300,
      status,
      json: async () => {
        throw failure;
      },
    }));
  }

  it("classifies a body read that fails after the headers as response_body_unavailable", async () => {
    // The connection can die, or the JSON can be truncated, once the status line has already
    // arrived. The request left the browser and the backend may have committed, so the client
    // must not let this reach the caller as an ordinary decoded failure.
    const table: ReadonlyArray<readonly [number, Error]> = [
      [200, new TypeError("network error")],
      [200, new SyntaxError("Unexpected end of JSON input")],
      [409, new TypeError("network error")],
    ];
    for (const [status, failure] of table) {
      const fetchApi = headersThenBody(status, failure);
      const rejection = createAuthoringActionClient({ fetchApi }).send(
        "req-history-1",
        "apply_timeline_transaction",
        timelineTransaction(),
        "workspace-fixture",
      );
      await expect(rejection).rejects.toBeInstanceOf(AuthoringClientError);
      await expect(rejection).rejects.toMatchObject({
        code: "response_body_unavailable",
        status,
      });
      expect(fetchApi).toHaveBeenCalledTimes(1);
    }
  });

  it("classifies a parsed but undecodable outcome body as outcome_evidence_undecodable", async () => {
    // A receipt that is not decodable at all, and a 409 whose conflict projection is not
    // decodable, are both missing outcome evidence rather than proof of a refusal.
    const receipt = timelineReceiptWire();
    delete receipt.history_cursor;
    const conflict = historyProjectionWire({
      code: "stale_workspace_revision",
    });
    delete conflict.snapshot;
    const table: ReadonlyArray<readonly [number, Record<string, unknown>]> = [
      [200, receipt],
      [409, conflict],
    ];
    for (const [status, body] of table) {
      const rejection = createAuthoringActionClient({
        fetchApi: fetchStub(status, body),
      }).send(
        "req-history-1",
        "apply_timeline_transaction",
        timelineTransaction(),
        "workspace-fixture",
      );
      await expect(rejection).rejects.toMatchObject({
        code: "outcome_evidence_undecodable",
        status,
      });
    }
  });

  it("keeps every accepted classification that already establishes an outcome", async () => {
    // The new codes must not swallow the checks that decide a known outcome: an accepted
    // receipt, an explicit refusal, a rebound receipt and a status/body disagreement.
    const accepted = await createAuthoringActionClient({
      fetchApi: fetchStub(200, timelineReceiptWire()),
    }).send(
      "req-history-1",
      "apply_timeline_transaction",
      timelineTransaction(),
      "workspace-fixture",
    );
    expect(accepted.receipt?.requestId).toBe("req-history-1");

    const refused = await createAuthoringActionClient({
      fetchApi: fetchStub(
        409,
        historyProjectionWire({ code: "stale_workspace_revision" }),
      ),
    }).send(
      "req-history-1",
      "apply_timeline_transaction",
      timelineTransaction(),
      "workspace-fixture",
    );
    expect(refused.history?.rejection?.code).toBe("stale_workspace_revision");

    const rebound = timelineReceiptWire();
    rebound.workspace_handle = "another-workspace";
    await expect(
      createAuthoringActionClient({ fetchApi: fetchStub(200, rebound) }).send(
        "req-history-1",
        "apply_timeline_transaction",
        timelineTransaction(),
        "workspace-fixture",
      ),
    ).rejects.toMatchObject({ code: "cross_workspace_response" });

    const unbound = timelineReceiptWire();
    unbound.request_id = "req-history-2";
    await expect(
      createAuthoringActionClient({ fetchApi: fetchStub(200, unbound) }).send(
        "req-history-1",
        "apply_timeline_transaction",
        timelineTransaction(),
        "workspace-fixture",
      ),
    ).rejects.toMatchObject({ code: "timeline_response_mismatch" });
  });
});
