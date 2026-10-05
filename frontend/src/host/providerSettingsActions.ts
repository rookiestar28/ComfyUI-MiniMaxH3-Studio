import {
  decodeProviderIntentResult,
  encodeProviderIntent,
  type ProviderIntent,
  type ProviderIntentPayload,
  type ProviderIntentResult,
} from "../contracts/providerSettingsCodec";

/**
 * M22-06 — the one seam the Settings page uses to reach provider state.
 *
 * A refused intent is a normal result, not an exception: the backend answers a
 * refusal with the current projection and a closed reason, and the surface
 * renders both. Only a transport or shape failure throws, because those are the
 * cases where the surface has nothing true to show.
 *
 * The credential is passed straight through to the request body and is never
 * held by this module. There is no cache, no retry buffer and no last-request
 * memo here, so there is nowhere for a secret to linger after the call.
 */

const route = "/h3-context/v1/provider/settings";
export const PROVIDER_SESSION_HEADER = "X-H3-Provider-Session";
const providerSessionHandle = /^ps_[0-9a-f]{32}$/;

export function isProviderSessionHandle(value: unknown): value is string {
  return typeof value === "string" && providerSessionHandle.test(value);
}

export function createProviderSessionHandle(
  getRandomValues: (target: Uint8Array<ArrayBuffer>) => Uint8Array<ArrayBuffer>,
): string {
  if (typeof getRandomValues !== "function")
    throw new ProviderSettingsClientError("session_rejected", 0);
  const bytes = getRandomValues(new Uint8Array(new ArrayBuffer(16)));
  if (!(bytes instanceof Uint8Array) || bytes.length !== 16)
    throw new ProviderSettingsClientError("session_rejected", 0);
  return `ps_${Array.from(bytes, (value) =>
    value.toString(16).padStart(2, "0"),
  ).join("")}`;
}

type FetchResponse = {
  ok: boolean;
  status: number;
  json(): Promise<unknown>;
};

const statusCodes = {
  400: "invalid_request",
  403: "origin_rejected",
  404: "unknown_profile",
  409: "state_conflict",
  410: "session_released",
  413: "request_too_large",
  415: "media_type_rejected",
  422: "credential_rejected",
  500: "internal_failure",
} as const;

export class ProviderSettingsClientError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(code: string, status: number) {
    super(code);
    this.name = "ProviderSettingsClientError";
    this.code = code;
    this.status = status;
  }
}

export function createProviderSettingsClient({
  fetchApi,
  sessionHandle,
}: {
  fetchApi(path: string, init: RequestInit): Promise<FetchResponse>;
  sessionHandle(): string | undefined;
}) {
  if (typeof fetchApi !== "function")
    throw new Error("supported same-origin provider settings seam is absent");
  if (typeof sessionHandle !== "function")
    throw new Error("provider browser-session authority is absent");

  const authority = (): string => {
    const value = sessionHandle();
    if (!isProviderSessionHandle(value))
      throw new ProviderSettingsClientError("session_rejected", 0);
    return value;
  };

  return {
    async send(
      intent: ProviderIntent,
      payload: ProviderIntentPayload = {},
      signal?: AbortSignal,
    ): Promise<ProviderIntentResult> {
      const response = await fetchApi(route, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "content-type": "application/json",
          [PROVIDER_SESSION_HEADER]: authority(),
        },
        body: encodeProviderIntent(intent, payload),
        signal,
      });
      // A refusal carries the same body shape as an acceptance, so both decode
      // through the same path and the surface never has to guess.
      if (response.ok || [404, 409, 422].includes(response.status))
        return decodeProviderIntentResult(await response.json());
      const code = statusCodes[response.status as keyof typeof statusCodes];
      throw new ProviderSettingsClientError(
        code ?? "internal_failure",
        response.status,
      );
    },
    async release(sessionHandleOverride?: string): Promise<void> {
      const releasedAuthority = sessionHandleOverride ?? authority();
      if (!isProviderSessionHandle(releasedAuthority))
        throw new ProviderSettingsClientError("session_rejected", 0);
      const response = await fetchApi(route, {
        method: "DELETE",
        credentials: "same-origin",
        headers: { [PROVIDER_SESSION_HEADER]: releasedAuthority },
        keepalive: true,
      });
      if (!response.ok)
        throw new ProviderSettingsClientError(
          statusCodes[response.status as keyof typeof statusCodes] ??
            "internal_failure",
          response.status,
        );
    },
  };
}

export type ProviderSettingsClient = ReturnType<
  typeof createProviderSettingsClient
>;
