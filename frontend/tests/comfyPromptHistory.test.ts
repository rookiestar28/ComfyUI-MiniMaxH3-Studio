import { describe, expect, it, vi } from "vitest";

import {
  ComfyPromptHistoryError,
  createComfyPromptHistoryClient,
} from "../src/host/comfyPromptHistory";

const promptId = "prompt.managed.1";

function response(value: unknown, status = 200) {
  const bytes = new TextEncoder().encode(JSON.stringify(value));
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: {
      get: (name: string) =>
        name === "content-length" ? String(bytes.length) : null,
    },
    body: new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(bytes);
        controller.close();
      },
    }),
    arrayBuffer: async () => bytes.buffer,
  };
}

describe("exact Comfy prompt history", () => {
  it("requests only the encoded retained prompt with GET and never enumerates history", async () => {
    const fetchApi = vi.fn(async () => response({}));
    const client = createComfyPromptHistoryClient({ fetchApi });

    await expect(client.read(promptId)).resolves.toMatchObject({
      disposition: "not_yet_observed",
      promptId,
    });
    expect(fetchApi).toHaveBeenCalledWith(
      `/history/${encodeURIComponent(promptId)}`,
      expect.objectContaining({
        method: "GET",
        credentials: "same-origin",
      }),
    );
    expect(fetchApi).not.toHaveBeenCalledWith("/history", expect.anything());
  });

  it("returns only a closed content-free terminal and output identity", async () => {
    const privateFilename = "private-user-video.mp4";
    const privateWorkflow = "private prompt and workflow payload";
    const fetchApi = vi.fn(async () =>
      response({
        [promptId]: {
          prompt: privateWorkflow,
          status: {
            status_str: "success",
            completed: true,
            messages: [
              ["execution_start", { prompt_id: promptId }],
              ["execution_success", { prompt_id: promptId }],
            ],
          },
          outputs: {
            "42": {
              images: [
                {
                  filename: privateFilename,
                  subfolder: "private/output/path",
                  type: "output",
                },
              ],
              animated: [true],
            },
          },
        },
      }),
    );

    const result = await createComfyPromptHistoryClient({ fetchApi }).read(
      promptId,
    );
    const wire = JSON.stringify(result);

    expect(result).toMatchObject({
      disposition: "succeeded",
      promptId,
      outputNodeIds: ["42"],
    });
    expect(result.outputIdentityFingerprint).toMatch(/^sha256:[0-9a-f]{64}$/);
    expect(result.terminalFingerprint).toMatch(/^sha256:[0-9a-f]{64}$/);
    expect(wire).not.toContain(privateFilename);
    expect(wire).not.toContain(privateWorkflow);
    expect(wire).not.toContain("private/output/path");
  });

  it.each([
    ["execution_error", "failed"],
    ["execution_interrupted", "interrupted"],
  ] as const)(
    "maps %s to the closed %s terminal",
    async (message, disposition) => {
      const fetchApi = vi.fn(async () =>
        response({
          [promptId]: {
            status: {
              status_str: "error",
              completed: true,
              messages: [
                [message, { exception_message: "private failure detail" }],
              ],
            },
            outputs: {},
          },
        }),
      );

      const result = await createComfyPromptHistoryClient({ fetchApi }).read(
        promptId,
      );

      expect(result.disposition).toBe(disposition);
      expect(JSON.stringify(result)).not.toContain("private failure detail");
    },
  );

  it("refuses an oversized body before reading or parsing it", async () => {
    const arrayBuffer = vi.fn(async () => new ArrayBuffer(0));
    const fetchApi = vi.fn(async () => ({
      ok: true,
      status: 200,
      headers: { get: () => String(1_048_577) },
      body: null,
      arrayBuffer,
    }));

    await expect(
      createComfyPromptHistoryClient({ fetchApi }).read(promptId),
    ).rejects.toMatchObject({ code: "history_oversized" });
    expect(arrayBuffer).not.toHaveBeenCalled();
  });

  it.each([
    ["missing", null],
    ["under-reported", "1"],
  ] as const)(
    "bounds and cancels an oversized stream with %s content-length",
    async (_label, contentLength) => {
      const cancel = vi.fn();
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(new Uint8Array(1_048_576));
          controller.enqueue(new Uint8Array([0x20]));
        },
        cancel,
      });
      const arrayBuffer = vi.fn(async () => new ArrayBuffer(1_048_577));
      const fetchApi = vi.fn(async () => ({
        ok: true,
        status: 200,
        headers: { get: () => contentLength },
        body,
        arrayBuffer,
      }));

      await expect(
        createComfyPromptHistoryClient({ fetchApi }).read(promptId),
      ).rejects.toMatchObject({ code: "history_oversized" });
      expect(arrayBuffer).not.toHaveBeenCalled();
      expect(cancel).toHaveBeenCalledOnce();
    },
  );

  it.each([
    [
      "malformed JSON",
      new TextEncoder().encode("{not-json").buffer,
      "history_malformed",
    ],
    [
      "foreign prompt",
      new TextEncoder().encode(JSON.stringify({ "prompt.foreign": {} })).buffer,
      "history_foreign",
    ],
  ] as const)(
    "refuses %s without exposing response content",
    async (_label, body, code) => {
      const fetchApi = vi.fn(async () => ({
        ok: true,
        status: 200,
        headers: { get: () => String(body.byteLength) },
        body: new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(new Uint8Array(body));
            controller.close();
          },
        }),
        arrayBuffer: async () => body,
      }));

      let failure: unknown;
      try {
        await createComfyPromptHistoryClient({ fetchApi }).read(promptId);
      } catch (error) {
        failure = error;
      }
      expect(failure).toBeInstanceOf(ComfyPromptHistoryError);
      expect(failure).toMatchObject({ code });
    },
  );

  it("treats an incomplete exact row as not yet observed", async () => {
    const fetchApi = vi.fn(async () =>
      response({
        [promptId]: {
          status: { status_str: "running", completed: false, messages: [] },
          outputs: {},
        },
      }),
    );

    await expect(
      createComfyPromptHistoryClient({ fetchApi }).read(promptId),
    ).resolves.toMatchObject({ disposition: "not_yet_observed" });
  });
});
