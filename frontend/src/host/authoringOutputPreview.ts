import {
  OUTPUT_CAPABILITY,
  decodeOutputStatus,
  type OutputStatus,
} from "../contracts/authoringOutputCodec";
import {
  OutputClientError,
  outputMediaPath,
  outputResponseLength,
  readOutputBytes,
  rejectOutputError,
  requireOutputCapability,
  withOutputResponse,
  type OutputFetch,
} from "./authoringOutputActions";

export type OutputPreviewLease = Readonly<{ url: string; close(): void }>;
export function createOutputPreview(fetchOutput: OutputFetch) {
  let active: (() => void) | null = null;
  let closed = false;
  return Object.freeze({
    async open(
      value: OutputStatus,
      capability: unknown,
      signal: AbortSignal,
    ): Promise<OutputPreviewLease> {
      active?.();
      requireOutputCapability(capability);
      const status = decodeOutputStatus(value);
      if (
        closed ||
        status.availability !== "available" ||
        status.output_handle === null
      )
        throw new OutputClientError("unavailable");
      const controller = new AbortController();
      let url: string | null = null,
        disposed = false;
      const dispose = () => {
        if (disposed) return;
        disposed = true;
        controller.abort();
        signal.removeEventListener("abort", dispose);
        if (url !== null) {
          URL.revokeObjectURL(url);
          url = null;
        }
        if (active === dispose) active = null;
      };
      active = dispose;
      signal.addEventListener("abort", dispose, { once: true });
      if (signal.aborted) dispose();
      try {
        const bytes = await withOutputResponse(
          fetchOutput,
          outputMediaPath(
            status.output_handle,
            status.workspace_handle,
            "preview",
          ),
          { method: "GET" },
          controller.signal,
          async (response, ownedSignal) => {
            if (response.status !== 200)
              return rejectOutputError(response, ownedSignal);
            const length = outputResponseLength(
              response,
              OUTPUT_CAPABILITY.max_preview_bytes,
              true,
            );
            if (
              response.headers.get("accept-ranges") !== "bytes" ||
              response.headers.get("content-disposition") !==
                'inline; filename="authoring-preview.mp4"'
            )
              throw new OutputClientError("invalid_output_contract");
            return readOutputBytes(response, length, ownedSignal);
          },
        );
        if (disposed || active !== dispose)
          throw new OutputClientError("aborted");
        // IMPORTANT: revoke the previous URL before acquiring another body. Late completion
        // must never publish a URL after selection change, unmount or caller cancellation.
        url = URL.createObjectURL(new Blob([bytes], { type: "video/mp4" }));
        return Object.freeze({ url, close: dispose });
      } catch (error) {
        dispose();
        if (error instanceof OutputClientError) throw error;
        throw new OutputClientError("invalid_output_contract");
      }
    },
    close() {
      closed = true;
      active?.();
    },
  });
}
export type OutputPreview = ReturnType<typeof createOutputPreview>;
