import { describe, expect, it } from "vitest";

import {
  PRODUCTION_AUTHORING_IMPORT_ROUTE,
  ProductionAuthoringImportError,
  createProductionAuthoringImportClient,
} from "../src/host/productionAuthoringImportClient";
import {
  importRequest,
  importResponseWire,
  importV2Request,
  importV2ResponseWire,
} from "./support/productionAuthoringImportFixture";

type Call = { path: string; init: RequestInit };

function client(
  respond: (call: Call) => Promise<{ status: number; body?: unknown }>,
) {
  const calls: Call[] = [];
  return {
    calls,
    client: createProductionAuthoringImportClient({
      async fetchApi(path, init) {
        const call = { path, init };
        calls.push(call);
        const result = await respond(call);
        return {
          ok: result.status >= 200 && result.status < 300,
          status: result.status,
          json: async () => {
            if (result.body === undefined) throw new Error("no body");
            return result.body;
          },
        };
      },
    }),
  };
}

describe("M25-29 dedicated import client", () => {
  it("posts the encoded request to the dedicated route and returns the joined response", async () => {
    const { client: subject, calls } = client(async () => ({
      status: 200,
      body: importResponseWire(),
    }));
    const response = await subject.send(importRequest());
    expect(calls).toHaveLength(1);
    expect(calls[0]!.path).toBe(PRODUCTION_AUTHORING_IMPORT_ROUTE);
    expect(calls[0]!.init.method).toBe("POST");
    expect(calls[0]!.init.credentials).toBe("same-origin");
    const body = JSON.parse(String(calls[0]!.init.body)) as Record<
      string,
      unknown
    >;
    expect(body.action).toBe("import_production_outputs_to_authoring");
    expect(body.request_id).toBe("import.1");
    expect(response.receipt.rows[0]!.assetId).toBe("generated.asset.1");
    expect(response.receipt.rows[0]!.disposition).toBe("created");
  });

  it("binds V2 import success to the captured authoring fingerprint", async () => {
    const { client: subject, calls } = client(async () => ({
      status: 200,
      body: importV2ResponseWire(),
    }));
    const response = await subject.send(importV2Request());
    expect(calls).toHaveLength(1);
    const wire = JSON.parse(String(calls[0]!.init.body)) as Record<
      string,
      unknown
    >;
    expect(wire.schema).toBe(
      "h3.context.production_authoring_import.request.v2",
    );
    expect(response.schema).toBe(
      "h3.context.production_authoring_import.response.v2",
    );
    if (
      response.schema !== "h3.context.production_authoring_import.response.v2"
    )
      throw new Error("expected V2 import response");
    expect(response.receipt.nleAuthoring.priorAuthoringFingerprint).toBe(
      importV2Request().expectedNleAuthoringFingerprint,
    );
  });

  it("refuses a V2 response from a different authoring base", async () => {
    const response = importV2ResponseWire();
    const receipt = response.receipt as Record<string, unknown>;
    const authoring = receipt.nle_authoring as Record<string, unknown>;
    authoring.prior_authoring_fingerprint = `sha256:${"f".repeat(64)}`;
    const { client: subject } = client(async () => ({
      status: 200,
      body: response,
    }));
    await expect(subject.send(importV2Request())).rejects.toMatchObject({
      code: "response_mismatch",
      outcomeUnknown: true,
    });
  });

  it("maps every bodiless refusal status to its coarse class without parsing a body", async () => {
    for (const [status, code, unknown] of [
      [400, "invalid_request", false],
      [403, "origin_rejected", false],
      [404, "workspace_unavailable", false],
      [408, "request_timeout_or_cancelled", true],
      [409, "conflict_or_replay", false],
      [410, "workspace_gone", false],
      [413, "request_too_large", false],
      [422, "ineligible_or_unsupported", false],
      [503, "service_unavailable", false],
      [500, "unexpected_status", true],
    ] as const) {
      const { client: subject } = client(async () => ({ status }));
      const failure = await subject.send(importRequest()).catch((e) => e);
      expect(failure).toBeInstanceOf(ProductionAuthoringImportError);
      expect(failure.code).toBe(code);
      expect(failure.status).toBe(status);
      expect(failure.outcomeUnknown).toBe(unknown);
    }
  });

  it("treats transport failure and abort as unknown outcomes, never as refusals", async () => {
    const { client: subject } = client(async () => {
      throw new Error("network down");
    });
    const failure = await subject.send(importRequest()).catch((e) => e);
    expect(failure.code).toBe("transport_failure");
    expect(failure.outcomeUnknown).toBe(true);
    const controller = new AbortController();
    const { client: aborting } = client(async () => {
      controller.abort();
      throw Object.assign(new Error("aborted"), { name: "AbortError" });
    });
    const aborted = await aborting
      .send(importRequest(), controller.signal)
      .catch((e) => e);
    expect(aborted.code).toBe("aborted");
    expect(aborted.outcomeUnknown).toBe(true);
  });

  it("refuses a malformed, cross-request or cross-workspace success body", async () => {
    const malformed = client(async () => ({ status: 200, body: { nope: 1 } }));
    await expect(malformed.client.send(importRequest())).rejects.toMatchObject({
      code: "malformed_response",
    });
    const crossRequest = importResponseWire();
    (crossRequest.receipt as Record<string, unknown>).request_id = "import.9";
    const mismatch = client(async () => ({ status: 200, body: crossRequest }));
    await expect(mismatch.client.send(importRequest())).rejects.toMatchObject({
      code: "response_mismatch",
      outcomeUnknown: true,
    });
    const crossWorkspace = importResponseWire();
    (
      crossWorkspace.receipt as Record<string, unknown>
    ).production_workspace_id = "workspace.2";
    const foreign = client(async () => ({ status: 200, body: crossWorkspace }));
    await expect(foreign.client.send(importRequest())).rejects.toMatchObject({
      code: "response_mismatch",
    });
  });

  it("rejects oversized or duplicate batches locally before any request", async () => {
    const { client: subject, calls } = client(async () => ({ status: 200 }));
    const base = importRequest();
    const tooMany = {
      ...base,
      entries: [1, 2, 3, 4].map((index) => ({
        segmentId: `segment.${index}`,
        outputHandle: `out_${String(index).repeat(40)}`,
      })),
    };
    await expect(subject.send(tooMany)).rejects.toThrow();
    const duplicate = {
      ...base,
      entries: [base.entries[0]!, base.entries[0]!],
    };
    await expect(subject.send(duplicate)).rejects.toThrow();
    expect(calls).toHaveLength(0);
  });
});
