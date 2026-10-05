import { sha256Text } from "../contracts/canonicalFingerprint";

export const COMFY_PROMPT_HISTORY_OBSERVATION_SCHEMA =
  "h3.context.comfy_prompt_history_observation.v1" as const;

const MAX_HISTORY_BYTES = 1_048_576;
const MAX_HISTORY_MESSAGES = 256;
const MAX_OUTPUT_NODES = 128;
const MAX_OUTPUT_MEMBERS = 32;
const promptIdentifier = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/;
const outputNodeIdentifier = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const terminalMessages = Object.freeze({
  execution_success: "succeeded",
  execution_error: "failed",
  execution_interrupted: "interrupted",
} as const);
const countedOutputMembers = new Set([
  "images",
  "animated",
  "audio",
  "video",
  "gifs",
]);

export type ComfyPromptHistoryDisposition =
  "not_yet_observed" | "succeeded" | "failed" | "interrupted";

export type ComfyPromptHistoryObservationV1 = Readonly<{
  schema: typeof COMFY_PROMPT_HISTORY_OBSERVATION_SCHEMA;
  promptId: string;
  disposition: ComfyPromptHistoryDisposition;
  outputNodeIds: readonly string[];
  outputIdentityFingerprint: string | null;
  terminalFingerprint: string | null;
}>;

type FetchResponse = Readonly<{
  ok: boolean;
  status: number;
  headers: Readonly<{ get(name: string): string | null }>;
  body: ReadableStream<Uint8Array> | null;
}>;

export class ComfyPromptHistoryError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(code: string, status: number) {
    super(code);
    this.name = "ComfyPromptHistoryError";
    this.code = code;
    this.status = status;
  }
}

function record(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

function notYetObserved(promptId: string): ComfyPromptHistoryObservationV1 {
  return Object.freeze({
    schema: COMFY_PROMPT_HISTORY_OBSERVATION_SCHEMA,
    promptId,
    disposition: "not_yet_observed",
    outputNodeIds: Object.freeze([]),
    outputIdentityFingerprint: null,
    terminalFingerprint: null,
  });
}

function decodeBody(body: ArrayBuffer): unknown {
  if (body.byteLength > MAX_HISTORY_BYTES)
    throw new ComfyPromptHistoryError("history_oversized", 413);
  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(body);
  } catch {
    throw new ComfyPromptHistoryError("history_malformed", 422);
  }
  try {
    return JSON.parse(text) as unknown;
  } catch {
    throw new ComfyPromptHistoryError("history_malformed", 422);
  }
}

async function cancelReader(
  reader: ReadableStreamDefaultReader<Uint8Array>,
): Promise<void> {
  try {
    await reader.cancel();
  } catch {
    // Cancellation is best-effort after the response has already become unusable.
  }
}

async function readBoundedBody(
  body: ReadableStream<Uint8Array> | null,
): Promise<ArrayBuffer> {
  if (body === null)
    throw new ComfyPromptHistoryError("history_malformed", 422);
  let reader: ReadableStreamDefaultReader<Uint8Array>;
  try {
    reader = body.getReader();
  } catch {
    throw new ComfyPromptHistoryError("history_unavailable", 0);
  }
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (
        !ArrayBuffer.isView(value) ||
        Object.prototype.toString.call(value) !== "[object Uint8Array]"
      ) {
        await cancelReader(reader);
        throw new ComfyPromptHistoryError("history_malformed", 422);
      }
      const chunk = new Uint8Array(
        value.buffer,
        value.byteOffset,
        value.byteLength,
      );
      if (chunk.byteLength > MAX_HISTORY_BYTES - total) {
        await cancelReader(reader);
        throw new ComfyPromptHistoryError("history_oversized", 413);
      }
      chunks.push(chunk.slice());
      total += chunk.byteLength;
    }
  } catch (error) {
    if (error instanceof ComfyPromptHistoryError) throw error;
    await cancelReader(reader);
    throw new ComfyPromptHistoryError("history_unavailable", 0);
  } finally {
    reader.releaseLock();
  }
  const result = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    result.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return result.buffer;
}

function terminalDisposition(
  status: Record<string, unknown>,
): ComfyPromptHistoryDisposition {
  if (status.completed !== true) return "not_yet_observed";
  if (
    !Array.isArray(status.messages) ||
    status.messages.length > MAX_HISTORY_MESSAGES
  )
    throw new ComfyPromptHistoryError("history_malformed", 422);
  const terminals: Array<
    Exclude<ComfyPromptHistoryDisposition, "not_yet_observed">
  > = [];
  for (const message of status.messages) {
    if (
      !Array.isArray(message) ||
      message.length !== 2 ||
      typeof message[0] !== "string"
    )
      throw new ComfyPromptHistoryError("history_malformed", 422);
    const disposition =
      terminalMessages[message[0] as keyof typeof terminalMessages];
    if (disposition !== undefined) terminals.push(disposition);
  }
  if (terminals.length !== 1)
    throw new ComfyPromptHistoryError("history_malformed", 422);
  return terminals[0];
}

function outputIdentity(outputsValue: unknown): Readonly<{
  nodeIds: readonly string[];
  fingerprint: string;
}> {
  const outputs = record(outputsValue);
  if (outputs === undefined)
    throw new ComfyPromptHistoryError("history_malformed", 422);
  const nodeIds = Object.keys(outputs).sort();
  if (
    nodeIds.length > MAX_OUTPUT_NODES ||
    nodeIds.some((nodeId) => !outputNodeIdentifier.test(nodeId))
  )
    throw new ComfyPromptHistoryError("history_malformed", 422);
  const identity = nodeIds.map((nodeId) => {
    const output = record(outputs[nodeId]);
    if (output === undefined || Object.keys(output).length > MAX_OUTPUT_MEMBERS)
      throw new ComfyPromptHistoryError("history_malformed", 422);
    const counts = Object.keys(output)
      .filter((name) => countedOutputMembers.has(name))
      .sort()
      .map((name) => {
        const rows = output[name];
        if (!Array.isArray(rows) || rows.length > 256)
          throw new ComfyPromptHistoryError("history_malformed", 422);
        return [name, rows.length] as const;
      });
    return [nodeId, counts] as const;
  });
  return Object.freeze({
    nodeIds: Object.freeze(nodeIds),
    fingerprint: sha256Text(
      JSON.stringify({
        schema: "h3.context.comfy_prompt_history_output_identity.v1",
        nodes: identity,
      }),
    ),
  });
}

export function decodeComfyPromptHistory(
  value: unknown,
  promptId: string,
): ComfyPromptHistoryObservationV1 {
  if (!promptIdentifier.test(promptId))
    throw new ComfyPromptHistoryError("invalid_prompt_id", 400);
  const root = record(value);
  if (root === undefined)
    throw new ComfyPromptHistoryError("history_malformed", 422);
  const keys = Object.keys(root);
  if (keys.length === 0) return notYetObserved(promptId);
  if (keys.length !== 1 || keys[0] !== promptId)
    throw new ComfyPromptHistoryError("history_foreign", 409);
  const row = record(root[promptId]);
  const status = record(row?.status);
  if (row === undefined || status === undefined)
    throw new ComfyPromptHistoryError("history_malformed", 422);
  const disposition = terminalDisposition(status);
  if (disposition === "not_yet_observed") return notYetObserved(promptId);
  const output = outputIdentity(row.outputs);
  const terminalFingerprint = sha256Text(
    JSON.stringify({
      schema: "h3.context.comfy_prompt_history_terminal.v1",
      prompt_id: promptId,
      disposition,
      output_identity_fingerprint: output.fingerprint,
    }),
  );
  return Object.freeze({
    schema: COMFY_PROMPT_HISTORY_OBSERVATION_SCHEMA,
    promptId,
    disposition,
    outputNodeIds: output.nodeIds,
    outputIdentityFingerprint: output.fingerprint,
    terminalFingerprint,
  });
}

export function createComfyPromptHistoryClient({
  fetchApi,
}: {
  fetchApi(path: string, init: RequestInit): Promise<FetchResponse>;
}) {
  if (typeof fetchApi !== "function")
    throw new Error("supported same-origin history seam is absent");
  return Object.freeze({
    async read(
      promptId: string,
      signal?: AbortSignal,
    ): Promise<ComfyPromptHistoryObservationV1> {
      if (!promptIdentifier.test(promptId))
        throw new ComfyPromptHistoryError("invalid_prompt_id", 400);
      let response: FetchResponse;
      try {
        response = await fetchApi(`/history/${encodeURIComponent(promptId)}`, {
          method: "GET",
          credentials: "same-origin",
          headers: { accept: "application/json" },
          signal,
        });
      } catch {
        throw new ComfyPromptHistoryError("history_unavailable", 0);
      }
      if (!response.ok)
        throw new ComfyPromptHistoryError(
          "history_route_rejected",
          response.status,
        );
      const declaredLength = response.headers.get("content-length");
      if (declaredLength !== null) {
        const parsed = Number(declaredLength);
        if (!Number.isSafeInteger(parsed) || parsed < 0)
          throw new ComfyPromptHistoryError("history_malformed", 422);
        if (parsed > MAX_HISTORY_BYTES)
          throw new ComfyPromptHistoryError("history_oversized", 413);
      }
      // IMPORTANT: keep history on this bounded stream path; arrayBuffer() would read an
      // untrusted missing or under-reported response without enforcing the 1 MiB ceiling.
      const body = await readBoundedBody(response.body);
      return decodeComfyPromptHistory(decodeBody(body), promptId);
    },
  });
}
