import {
  decodeGenerationProfile,
  type GenerationProfile,
} from "../contracts/generationProfileCodec";

/**
 * Read the backend's generation capability projection over the same-origin seam.
 *
 * The route is this repository's own (M17-20 D1), served by the node pack the
 * shell ships with, so a host that cannot answer it is a host this shell has not
 * qualified rather than an older host to accommodate. Every failure here is
 * therefore terminal for App Mode's generation route and never a downgrade.
 */
export const GENERATION_PROFILE_ROUTE = "/h3-context/v1/generation/profile";

/** The projection is a fixed-shape decision, not a document; this is generous. */
export const MAX_GENERATION_PROFILE_BYTES = 16_384;

type FetchResponse = {
  ok: boolean;
  status: number;
  text(): Promise<string>;
};

export type GenerationProfileFailure =
  "seam_unavailable" | "route_rejected" | "payload_rejected";

export class GenerationProfileClientError extends Error {
  readonly failure: GenerationProfileFailure;
  readonly status: number;

  constructor(failure: GenerationProfileFailure, status: number) {
    // CRITICAL: never carry the response body or a host message into the error;
    // a profile failure is host state and the sidebar renders error text.
    super(failure);
    this.name = "GenerationProfileClientError";
    this.failure = failure;
    this.status = status;
  }
}

export function createGenerationProfileClient({
  fetchApi,
}: {
  fetchApi?: (path: string, init: RequestInit) => Promise<FetchResponse>;
}) {
  return {
    async load(signal?: AbortSignal): Promise<GenerationProfile> {
      if (typeof fetchApi !== "function")
        throw new GenerationProfileClientError("seam_unavailable", 0);
      let response: FetchResponse;
      try {
        response = await fetchApi(GENERATION_PROFILE_ROUTE, {
          method: "GET",
          credentials: "same-origin",
          headers: { accept: "application/json" },
          signal,
        });
      } catch {
        throw new GenerationProfileClientError("seam_unavailable", 0);
      }
      if (response?.ok !== true)
        throw new GenerationProfileClientError(
          "route_rejected",
          typeof response?.status === "number" ? response.status : 0,
        );
      let body: string;
      try {
        body = await response.text();
      } catch {
        throw new GenerationProfileClientError("payload_rejected", 200);
      }
      // CRITICAL: bound before parsing. The route is same-origin but the body is
      // still host output, and a decoder is not a size limit.
      if (
        typeof body !== "string" ||
        body.length > MAX_GENERATION_PROFILE_BYTES
      )
        throw new GenerationProfileClientError("payload_rejected", 200);
      try {
        return decodeGenerationProfile(JSON.parse(body));
      } catch {
        throw new GenerationProfileClientError("payload_rejected", 200);
      }
    },
  };
}
