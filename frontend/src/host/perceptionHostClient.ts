import {
  decodePerceptionHostStatus,
  type PerceptionHostStatus,
} from "../contracts/perceptionHostCodec";

export type PerceptionHostClientCode =
  | "seam_unavailable"
  | "transport_failure"
  | "unexpected_status"
  | "malformed_response";
export type PerceptionHostReadResult =
  | Readonly<{ ok: true; status: PerceptionHostStatus }>
  | Readonly<{ ok: false; code: PerceptionHostClientCode }>;
type FetchResponse = { ok: boolean; status: number; text(): Promise<string> };

export function createPerceptionHostClient({
  fetchApi,
}: {
  fetchApi?: (path: string, init: RequestInit) => Promise<FetchResponse>;
}) {
  return Object.freeze({
    async read(signal?: AbortSignal): Promise<PerceptionHostReadResult> {
      if (typeof fetchApi !== "function")
        return { ok: false, code: "seam_unavailable" };
      let response: FetchResponse;
      try {
        response = await fetchApi("/h3-context/v1/perception/status", {
          method: "GET",
          credentials: "same-origin",
          headers: { accept: "application/json" },
          signal,
        });
      } catch {
        return { ok: false, code: "transport_failure" };
      }
      try {
        if (response.status !== 200 || response.ok !== true)
          return { ok: false, code: "unexpected_status" };
        const text = await response.text();
        // CRITICAL: UTF-16 string length undercounts UTF-8 response bytes. Bound before parsing.
        if (
          typeof text !== "string" ||
          new TextEncoder().encode(text).byteLength > 4096
        )
          throw 0;
        return {
          ok: true,
          status: decodePerceptionHostStatus(JSON.parse(text)),
        };
      } catch {
        return { ok: false, code: "malformed_response" };
      }
    },
  });
}
