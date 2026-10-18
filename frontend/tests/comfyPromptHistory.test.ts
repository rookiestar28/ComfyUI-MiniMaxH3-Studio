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

describe("SaveVideo artifacts recorded in exact prompt history", () => {
  const mediaMetadata = {
    kind: "video",
    width: 1280,
    height: 720,
    duration: 8,
    fps: 24,
    frame_count: 192,
  };
  const saved = {
    filename: "managed_00001_.mp4",
    subfolder: "video/h3-context",
    type: "output",
  };
  function completed(
    outputs: Record<string, unknown>,
    terminal = "execution_success",
  ) {
    return {
      [promptId]: {
        prompt: "private prompt and workflow payload",
        status: {
          status_str: terminal === "execution_success" ? "success" : "error",
          completed: true,
          messages: [
            ["execution_start", { prompt_id: promptId }],
            [terminal, { prompt_id: promptId }],
          ],
        },
        outputs,
      },
    };
  }

  it("decodes the host-annotated SaveVideo entry and ignores input and temporary previews", async () => {
    const fetchApi = vi.fn(async () =>
      response(
        completed({
          "12": {
            images: [
              {
                filename: "reference.mp4",
                subfolder: "",
                type: "input",
                metadata: mediaMetadata,
              },
            ],
            animated: [true],
          },
          "21": {
            images: [{ filename: "preview.mp4", subfolder: "", type: "temp" }],
            animated: [true],
          },
          "43": {
            images: [
              { ...saved, metadata: mediaMetadata, id: "asset-managed-00001" },
            ],
            animated: [true],
          },
        }),
      ),
    );

    const result = await createComfyPromptHistoryClient({
      fetchApi,
    }).readSavedVideos(promptId);

    expect(result).toEqual({
      promptId,
      disposition: "succeeded",
      artifacts: [{ promptId, outputNodeId: "43", locator: saved }],
    });
    expect(JSON.stringify(result)).not.toContain("asset-managed");
    expect(JSON.stringify(result)).not.toContain("private prompt");
    expect(fetchApi).toHaveBeenCalledWith(
      `/history/${encodeURIComponent(promptId)}`,
      expect.objectContaining({ method: "GET", credentials: "same-origin" }),
    );
  });

  it("reports an absent or incomplete row as not yet observed with no artifacts", async () => {
    for (const value of [
      {},
      {
        [promptId]: {
          status: { status_str: "running", completed: false, messages: [] },
          outputs: {},
        },
      },
    ]) {
      const fetchApi = vi.fn(async () => response(value));
      await expect(
        createComfyPromptHistoryClient({ fetchApi }).readSavedVideos(promptId),
      ).resolves.toEqual({
        promptId,
        disposition: "not_yet_observed",
        artifacts: [],
      });
    }
  });

  it("returns every SaveVideo candidate and the failed disposition unchanged", async () => {
    const fetchApi = vi.fn(async () =>
      response(
        completed(
          {
            "43": { images: [saved], animated: [true] },
            "44": {
              images: [{ ...saved, filename: "second.mp4" }],
              animated: [true],
            },
          },
          "execution_error",
        ),
      ),
    );
    const result = await createComfyPromptHistoryClient({
      fetchApi,
    }).readSavedVideos(promptId);
    expect(result.disposition).toBe("failed");
    expect(result.artifacts.map((artifact) => artifact.outputNodeId)).toEqual([
      "43",
      "44",
    ]);
  });

  it.each([
    [
      "a foreign prompt row",
      { "prompt.other.1": completed({})[promptId] },
      "history_foreign",
    ],
    [
      "a malformed outputs member",
      { [promptId]: { ...completed({})[promptId], outputs: [] } },
      "history_malformed",
    ],
  ])("refuses %s", async (_name, value, code) => {
    const fetchApi = vi.fn(async () => response(value));
    await expect(
      createComfyPromptHistoryClient({ fetchApi }).readSavedVideos(promptId),
    ).rejects.toMatchObject({ code });
  });
});
