// M25-16: the read-only capability projection for the accepted M25-19 final-output leaf.
//
// The expanded workspace reads it once per open and passes the decoded value, unchanged, to
// `AuthoringOutput`. A refusal, a malformed body or a transport failure yields an unsupported
// capability so the render affordance renders as the noninteractive `backend_render_unavailable`
// status rather than an enabled control with no executed effect.

import {
  OUTPUT_CAPABILITY,
  decodeOutputCapability,
  type OutputCapability,
} from "../contracts/authoringOutputCodec";

export const AUTHORING_OUTPUT_CAPABILITY_ROUTE =
  "/h3-context/v1/authoring/output-capability" as const;

const MAX_CAPABILITY_BYTES = 4096;

export type AuthoringOutputCapabilityFetch = (
  path: string,
  init: RequestInit,
) => Promise<{ ok: boolean; status: number; json(): Promise<unknown> }>;

export const UNSUPPORTED_OUTPUT_CAPABILITY: OutputCapability = Object.freeze({
  ...OUTPUT_CAPABILITY,
  supported: false,
});

export function createAuthoringOutputCapabilityClient({
  fetchApi,
}: {
  fetchApi: AuthoringOutputCapabilityFetch;
}) {
  return Object.freeze({
    /** Never throws: every failure is the unsupported capability. */
    async read(signal?: AbortSignal): Promise<OutputCapability> {
      try {
        const response = await fetchApi(AUTHORING_OUTPUT_CAPABILITY_ROUTE, {
          method: "GET",
          credentials: "same-origin",
          headers: { accept: "application/json" },
          signal,
        });
        if (response.status !== 200) return UNSUPPORTED_OUTPUT_CAPABILITY;
        const body = await response.json();
        if (JSON.stringify(body).length > MAX_CAPABILITY_BYTES)
          return UNSUPPORTED_OUTPUT_CAPABILITY;
        return decodeOutputCapability(body);
      } catch {
        return UNSUPPORTED_OUTPUT_CAPABILITY;
      }
    },
  });
}

export type AuthoringOutputCapabilityClient = ReturnType<
  typeof createAuthoringOutputCapabilityClient
>;
