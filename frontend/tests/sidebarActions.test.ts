import { describe, expect, it, vi } from "vitest";

import { createSidebarActionClient } from "../src/host/sidebarActions";
import {
  validateAssistedActionRequest,
  refinementInstructionMetrics,
} from "../src/contracts/assistedPromptProposalCodec";
import { validSidebarWorkspace } from "./sidebarWorkspaceFixture";

describe("sidebar action client", () => {
  it("validates exact instruction data and rejects malformed authority before any send", async () => {
    const fetchApi = vi.fn(async (_path: string, _init: RequestInit) => ({
      ok: true,
      status: 200,
      json: async () => ({
        schema: "h3.context.assisted_sidebar_result.v1",
        state: "idle",
        proposal: null,
      }),
    }));
    const client = createSidebarActionClient({
      fetchApi,
      providerSessionHandle: () => `ps_${"a".repeat(32)}`,
    });
    const instruction = "  Light the subject. 中文 😀  ";
    await client.sendAssisted(validSidebarWorkspace, {
      action: "refine_prompt",
      payload: { instruction },
    });
    expect(
      JSON.parse(String(fetchApi.mock.calls[0]?.[1]?.body)).payload,
    ).toEqual({ instruction });
    fetchApi.mockClear();
    for (const payload of [
      {},
      { instruction: "" },
      { instruction: "\x85" },
      { instruction: "x".repeat(2049) },
      { instruction: "\ud800" },
      { instruction: "\u0000" },
      { instruction, system: "forged" },
    ]) {
      expect(() =>
        validateAssistedActionRequest({ action: "refine_prompt", payload }),
      ).toThrow();
      await expect(
        client.sendAssisted(validSidebarWorkspace, {
          action: "refine_prompt",
          payload,
        } as never),
      ).rejects.toThrow();
    }
    expect(fetchApi).not.toHaveBeenCalled();
    expect(refinementInstructionMetrics("😀".repeat(2048))).toEqual({
      scalars: 2048,
      bytes: 8192,
      valid: true,
    });
    expect(refinementInstructionMetrics("\ufeff").valid).toBe(true);
  });
  it("posts assisted actions only to the dedicated route with the opaque session", async () => {
    const fetchApi = vi.fn(async (_path: string, _init: RequestInit) => ({
      ok: true,
      status: 200,
      json: async () => ({
        schema: "h3.context.assisted_sidebar_result.v1",
        state: "idle",
        proposal: null,
      }),
    }));
    const client = createSidebarActionClient({
      fetchApi,
      providerSessionHandle: () => `ps_${"a".repeat(32)}`,
    });
    await client.sendAssisted(validSidebarWorkspace, {
      action: "cancel_assisted_execution",
      payload: {},
    });
    const [path, init] = fetchApi.mock.calls[0] ?? [];
    expect(path).toBe("/h3-context/v1/sidebar/assisted");
    expect(init?.credentials).toBe("same-origin");
    expect(init?.headers).toEqual({
      "content-type": "application/json",
      "X-H3-Provider-Session": `ps_${"a".repeat(32)}`,
    });
    expect(JSON.stringify(init?.body)).not.toMatch(
      /credential|api_key|authorization|endpoint/i,
    );
  });

  it("posts one closed same-origin action bound to the current revision", async () => {
    const fetchApi = vi.fn(async (_path: string, init: RequestInit) => ({
      ok: true,
      status: 200,
      json: async () => ({
        ...validSidebarWorkspace,
        report_revision: 2,
        report_fingerprint: `sha256:${"c".repeat(64)}`,
        prompt_fingerprint: `sha256:${"d".repeat(64)}`,
        lifecycle: "stale",
        validation_status: "not_run",
        bindings: [],
        proposal: {
          ...validSidebarWorkspace.proposal,
          changed: true,
          reason: "Clarify",
          current_prompt_fingerprint: `sha256:${"d".repeat(64)}`,
        },
        stages: validSidebarWorkspace.stages.map((stage, index) => ({
          ...stage,
          status: index === 3 ? "active" : index === 4 ? "blocked" : "complete",
        })),
        actions: {
          stage_prompt: true,
          import_prompt: true,
          validate: true,
          export: false,
          copy_prompt: false,
        },
      }),
    }));
    const client = createSidebarActionClient({ fetchApi });
    const result = await client.send(validSidebarWorkspace, {
      action: "stage_prompt",
      payload: { reason: "Clarify", prompt_text: "safe" },
    });
    expect(result.kind).toBe("workspace");
    expect(fetchApi).toHaveBeenCalledOnce();
    const [path, init] = fetchApi.mock.calls[0] ?? [];
    expect(path).toBe("/h3-context/v1/sidebar/action");
    expect(init?.method).toBe("POST");
    const body = JSON.parse(String(init?.body));
    expect(body).toMatchObject({
      schema: "h3.context.sidebar.action.v2",
      workspace_id: validSidebarWorkspace.workspace_id,
      expected_revision: validSidebarWorkspace.report_revision,
      expected_report_fingerprint: validSidebarWorkspace.report_fingerprint,
      action: "stage_prompt",
    });
    expect(Object.keys(body).sort()).toEqual([
      "action",
      "expected_report_fingerprint",
      "expected_revision",
      "payload",
      "schema",
      "workspace_id",
    ]);
  });

  it("fails closed on route errors and incompatible responses", async () => {
    for (const response of [
      { ok: false, status: 409, json: async () => ({ error: "stale_action" }) },
      { ok: true, status: 200, json: async () => ({ forged: true }) },
    ]) {
      const client = createSidebarActionClient({
        fetchApi: vi.fn(async () => response),
      });
      await expect(
        client.send(validSidebarWorkspace, {
          action: "validate",
          payload: {},
        }),
      ).rejects.toThrow();
    }
  });

  it("has no browser provider, credential, locator, or media-upload request surface", async () => {
    const fetchApi = vi.fn(async (_path: string, _init: RequestInit) => ({
      ok: true,
      status: 200,
      json: async () => validSidebarWorkspace,
    }));
    const client = createSidebarActionClient({ fetchApi });
    await client.send(validSidebarWorkspace, {
      action: "validate",
      payload: {},
    });
    const [path, init] = fetchApi.mock.calls[0] ?? [];
    expect(path).toBe("/h3-context/v1/sidebar/action");
    expect(init?.credentials).toBe("same-origin");
    const wire = JSON.parse(String(init?.body)) as Record<string, unknown>;
    const encoded = JSON.stringify(wire).toLowerCase();
    for (const forbidden of [
      "credential",
      "api_key",
      "authorization",
      "endpoint",
      "locator",
      "signed_url",
      "media_upload",
      "ollama",
      "minimax",
    ])
      expect(encoded).not.toContain(forbidden);
  });
});
