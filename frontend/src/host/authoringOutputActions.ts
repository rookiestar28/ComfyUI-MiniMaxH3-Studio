import {
  OUTPUT_CAPABILITY,
  decodeOutputCapability,
  decodeOutputCreate,
  decodeOutputError,
  decodeOutputStatus,
  outputHandle,
  outputWorkspace,
  parseOutputJson,
  type OutputBinding,
  type OutputCapability,
  type OutputCreate,
  type OutputErrorCode,
  type OutputStatus,
} from "../contracts/authoringOutputCodec";

export type OutputFetch = (
  path: string,
  init: RequestInit,
) => Promise<Response>;
export class OutputClientError extends Error {
  constructor(
    readonly code: OutputErrorCode | "invalid_output_contract" | "aborted",
    readonly status = 0,
  ) {
    super(code);
  }
}
export function requireOutputCapability(value: unknown): OutputCapability {
  const capability = decodeOutputCapability(value);
  if (!capability.supported) throw new OutputClientError("runtime_unavailable");
  return capability;
}
export function outputMediaPath(
  handle: string,
  workspace: string,
  kind: "preview" | "download",
): string {
  if (kind !== "preview" && kind !== "download")
    throw new OutputClientError("invalid_output_contract");
  return `${OUTPUT_CAPABILITY.output_path}/${outputHandle(handle, "output")}/${kind}?workspace_handle=${outputWorkspace(workspace)}`;
}
export async function cancelOutputBody(response?: Response): Promise<void> {
  try {
    await response?.body?.cancel();
  } catch {
    /* Preserve the closed transport disposition. */
  }
}
export function outputResponseLength(
  response: Response,
  max: number,
  media: boolean,
): number {
  const header = response.headers.get("content-length");
  const type = response.headers.get("content-type");
  if (
    response.redirected ||
    header === null ||
    !/^[1-9][0-9]{0,9}(?![\s\S])/.test(header) ||
    Number(header) > max ||
    response.headers.get("cache-control") !== "private, no-store" ||
    response.headers.get("x-content-type-options") !== "nosniff" ||
    response.headers.get("referrer-policy") !== "no-referrer" ||
    [
      "location",
      "content-range",
      "content-encoding",
      "transfer-encoding",
      "etag",
      "last-modified",
    ].some((name) => response.headers.has(name)) ||
    (media
      ? type !== "video/mp4"
      : !["application/json", "application/json; charset=utf-8"].includes(
          type ?? "",
        ))
  ) {
    throw new OutputClientError("invalid_output_contract");
  }
  return Number(header);
}
export async function readOutputBytes(
  response: Response,
  length: number,
  signal: AbortSignal,
): Promise<Uint8Array<ArrayBuffer>> {
  if (!response.body) throw new OutputClientError("invalid_output_contract");
  const reader = response.body.getReader({ mode: "byob" });
  const abort = () => {
    void reader.cancel().catch(() => undefined);
  };
  signal.addEventListener("abort", abort, { once: true });
  try {
    // IMPORTANT: BYOB bounds each accepted network chunk; blob()/arrayBuffer() would
    // allocate an unbounded dishonest response before a declared-length check could run.
    const packed = new Uint8Array(length);
    let offset = 0,
      backing = new ArrayBuffer(65536);
    for (;;) {
      if (signal.aborted) throw new OutputClientError("aborted");
      const supplied = backing;
      const result = await reader.read(new Uint8Array(supplied));
      if (signal.aborted) throw new OutputClientError("aborted");
      if (supplied.byteLength !== 0)
        throw new OutputClientError("invalid_output_contract");
      const view = result.value;
      if (view) {
        if (
          view.byteLength > 65536 ||
          view.buffer.byteLength > 65536 ||
          offset + view.byteLength > length ||
          (!result.done && view.byteLength === 0)
        )
          throw new OutputClientError("invalid_output_contract");
        packed.set(view, offset);
        offset += view.byteLength;
        backing = view.buffer as ArrayBuffer;
      } else if (!result.done)
        throw new OutputClientError("invalid_output_contract");
      if (result.done) {
        if (offset !== length)
          throw new OutputClientError("invalid_output_contract");
        break;
      }
    }
    return packed;
  } finally {
    signal.removeEventListener("abort", abort);
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
export async function withOutputResponse<T>(
  fetchOutput: OutputFetch,
  path: string,
  init: RequestInit,
  signal: AbortSignal,
  consume: (response: Response, signal: AbortSignal) => Promise<T>,
): Promise<T> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal.addEventListener("abort", abort, { once: true });
  if (signal.aborted) controller.abort();
  const timer = setTimeout(abort, 120000);
  let response: Response | undefined;
  let rejectAbort: (() => void) | undefined;
  const aborted = new Promise<never>((_resolve, reject) => {
    rejectAbort = () => reject(new OutputClientError("aborted"));
    controller.signal.addEventListener("abort", rejectAbort, { once: true });
  });
  try {
    if (controller.signal.aborted) throw new OutputClientError("aborted");
    const operation = (async () => {
      response = await fetchOutput(path, {
        ...init,
        signal: controller.signal,
        credentials: "same-origin",
        cache: "no-store",
        redirect: "error",
        referrerPolicy: "no-referrer",
      });
      if (controller.signal.aborted) {
        await cancelOutputBody(response);
        throw new OutputClientError("aborted");
      }
      return consume(response, controller.signal);
    })();
    return await Promise.race([operation, aborted]);
  } catch (error) {
    if (error instanceof OutputClientError) throw error;
    throw new OutputClientError(
      controller.signal.aborted ? "aborted" : "invalid_output_contract",
    );
  } finally {
    clearTimeout(timer);
    signal.removeEventListener("abort", abort);
    if (rejectAbort)
      controller.signal.removeEventListener("abort", rejectAbort);
    controller.abort();
    await cancelOutputBody(response);
  }
}
export async function readOutputJson(
  response: Response,
  signal: AbortSignal,
): Promise<unknown> {
  const length = outputResponseLength(response, 8192, false);
  const bytes = await readOutputBytes(response, length, signal);
  return parseOutputJson(
    new TextDecoder("utf-8", { fatal: true }).decode(bytes),
  );
}
export async function rejectOutputError(
  response: Response,
  signal: AbortSignal,
): Promise<never> {
  const error = decodeOutputError(
    await readOutputJson(response, signal),
    response.status,
  );
  throw new OutputClientError(error.code, response.status);
}
export function createOutputClient(fetchOutput: OutputFetch) {
  const send = async (
    path: string,
    init: RequestInit,
    binding: OutputBinding,
    capability: unknown,
    signal: AbortSignal,
    job?: string,
  ): Promise<OutputStatus> => {
    requireOutputCapability(capability);
    return withOutputResponse(
      fetchOutput,
      path,
      init,
      signal,
      async (response, ownedSignal) => {
        if (response.status !== 200)
          return rejectOutputError(response, ownedSignal);
        const status = decodeOutputStatus(
          await readOutputJson(response, ownedSignal),
        );
        if (
          status.workspace_handle !== binding.workspace_handle ||
          status.workspace_revision !== binding.workspace_revision ||
          status.timeline_revision !== binding.timeline_revision ||
          status.snapshot_fingerprint !== binding.snapshot_fingerprint ||
          (job !== undefined && status.job_handle !== job)
        )
          throw new OutputClientError("invalid_output_contract");
        return status;
      },
    );
  };
  return Object.freeze({
    async create(
      value: OutputCreate,
      capability: unknown,
      signal: AbortSignal,
    ) {
      const request = decodeOutputCreate(value);
      return send(
        OUTPUT_CAPABILITY.job_path,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(request),
        },
        request,
        capability,
        signal,
      );
    },
    async status(
      job: string,
      binding: OutputBinding,
      capability: unknown,
      signal: AbortSignal,
    ) {
      return send(
        `${OUTPUT_CAPABILITY.job_path}/${outputHandle(job, "job")}?workspace_handle=${outputWorkspace(binding.workspace_handle)}`,
        { method: "GET" },
        binding,
        capability,
        signal,
        job,
      );
    },
    async cancel(
      job: string,
      binding: OutputBinding,
      capability: unknown,
      signal: AbortSignal,
    ) {
      return send(
        `${OUTPUT_CAPABILITY.job_path}/${outputHandle(job, "job")}/cancel`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            schema: "h3.authoring.output_cancel.v1",
            workspace_handle: outputWorkspace(binding.workspace_handle),
          }),
        },
        binding,
        capability,
        signal,
        job,
      );
    },
  });
}
export type OutputClient = ReturnType<typeof createOutputClient>;
