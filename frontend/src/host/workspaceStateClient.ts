import {
  decodeWorkspaceStateResponse,
  encodeWorkspaceStateAction,
  type WorkspaceStateAction,
  type WorkspaceStateCode,
  type WorkspaceStateResponse,
} from "../contracts/workspaceStateCodec";

export type WorkspaceStateClientCode =
  | WorkspaceStateCode
  | "transport_failure"
  | "malformed_response"
  | "unexpected_status"
  | "aborted";
export type WorkspaceStateResult =
  | Readonly<{ ok: true; response: WorkspaceStateResponse }>
  | Readonly<{
      ok: false;
      code: WorkspaceStateClientCode;
      outcomeUnknown: boolean;
    }>;
export type WorkspaceStateFetch = (
  path: string,
  init: RequestInit,
) => Promise<{
  status: number;
  json(): Promise<unknown>;
}>;

export function createWorkspaceStateClient({
  fetchApi,
}: {
  fetchApi: WorkspaceStateFetch;
}) {
  async function send(
    action: WorkspaceStateAction,
    signal?: AbortSignal,
  ): Promise<WorkspaceStateResult> {
    let body: string;
    try {
      body = encodeWorkspaceStateAction(action);
    } catch {
      return { ok: false, code: "invalid_request", outcomeUnknown: false };
    }
    const mutation = !["status", "list", "restore"].includes(action.intent);
    let response: Awaited<ReturnType<WorkspaceStateFetch>>;
    try {
      response = await fetchApi("/h3-context/workspace-state", {
        method: "POST",
        credentials: "same-origin",
        headers: { "content-type": "application/json" },
        body,
        signal,
      });
    } catch (error) {
      // CRITICAL: a lost commit response is not permission to retry. Re-read the manifest first.
      return {
        ok: false,
        code:
          signal?.aborted ||
          (error as { name?: string } | null)?.name === "AbortError"
            ? "aborted"
            : "transport_failure",
        outcomeUnknown: mutation,
      };
    }
    try {
      const value = await response.json();
      if (JSON.stringify(value).length > 128 * 1024)
        throw new Error("response_bound");
      const decoded = decodeWorkspaceStateResponse(value);
      if (response.status === 200) {
        if (
          action.intent === "restore"
            ? decoded.recovered?.record_id !== action.record_id
            : decoded.recovered !== null
        )
          throw new Error("response_intent_mismatch");
        return { ok: true, response: decoded };
      }
      // A server error can happen after commit while recapturing status; a typed error is
      // not proof of non-execution. Only a new manifest read resolves that uncertainty.
      if (decoded.error !== null)
        return {
          ok: false,
          code: decoded.error,
          outcomeUnknown: mutation && response.status >= 500,
        };
      return {
        ok: false,
        code: "unexpected_status",
        outcomeUnknown: mutation && response.status >= 500,
      };
    } catch {
      return {
        ok: false,
        code: "malformed_response",
        outcomeUnknown: mutation,
      };
    }
  }
  return Object.freeze({ send });
}
