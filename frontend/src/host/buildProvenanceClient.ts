import type { H3ContextBuildIdentity } from "../../buildProvenance";
import { H3_CONTEXT_BUILD_PROVENANCE } from "../buildProvenance";

export const BUILD_PROVENANCE_ROUTE = "/h3-context/v1/build/provenance";

export type BuildProvenanceProjection = Readonly<{
  sourceCommit: string;
  /**
   * The digest of the frontend sources this runtime was built from.
   *
   * This is the only identity field that verifies itself: the generator recomputes it from the
   * live sources on every run, so a record carrying it cannot describe different bytes. The
   * commit names the base the build descends from and is written before the commit that carries
   * it, so it can never name that commit -- which is why the surface shows this one.
   */
  sourceInputsSha256: string;
  bundleSha256: string;
  bundleMatchesRecord: boolean;
}>;

type FetchResponse = Readonly<{
  ok: boolean;
  status: number;
  text(): Promise<string>;
}>;

const oid = /^[0-9a-f]{40}$/;
const digest = /^sha256:[0-9a-f]{64}$/;

function object(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

export function decodeBuildProvenanceResponse(
  value: unknown,
  bundleIdentity: H3ContextBuildIdentity = H3_CONTEXT_BUILD_PROVENANCE,
): BuildProvenanceProjection {
  const response = object(value);
  const record = object(response?.record);
  const external = object(record?.external_parameters);
  const bundle = object(record?.bundle);
  const recordIdentity = object(record?.embedded_identity);
  const served = object(response?.served_bundle);
  if (
    response?.schema !== "h3.context.build_provenance_response.v1" ||
    record?.schema !== "h3-context-build-provenance/1" ||
    typeof external?.source_commit !== "string" ||
    !oid.test(external.source_commit) ||
    external.source_commit !== bundleIdentity.source_commit ||
    typeof bundle?.sha256 !== "string" ||
    !digest.test(bundle.sha256) ||
    typeof served?.sha256 !== "string" ||
    !digest.test(served.sha256) ||
    typeof served.matches_record !== "boolean" ||
    recordIdentity?.schema !== "h3-context-build-identity/1" ||
    recordIdentity.builder_id !== bundleIdentity.builder_id ||
    recordIdentity.source_commit !== bundleIdentity.source_commit ||
    recordIdentity.source_tree !== bundleIdentity.source_tree ||
    recordIdentity.source_inputs_sha256 !==
      bundleIdentity.source_inputs_sha256 ||
    recordIdentity.resolved_dependencies_sha256 !==
      bundleIdentity.resolved_dependencies_sha256 ||
    served.matches_record !== (served.sha256 === bundle.sha256)
  )
    throw new Error("invalid build provenance response");
  return Object.freeze({
    sourceCommit: bundleIdentity.source_commit,
    sourceInputsSha256: bundleIdentity.source_inputs_sha256,
    bundleSha256: served.sha256,
    bundleMatchesRecord: served.matches_record,
  });
}

export function createBuildProvenanceClient({
  fetchApi,
}: {
  fetchApi(path: string, init: RequestInit): Promise<FetchResponse>;
}) {
  return Object.freeze({
    async load(signal?: AbortSignal): Promise<BuildProvenanceProjection> {
      const response = await fetchApi(BUILD_PROVENANCE_ROUTE, {
        method: "GET",
        credentials: "same-origin",
        headers: { accept: "application/json" },
        signal,
      });
      if (!response.ok || response.status !== 200)
        throw new Error("build provenance unavailable");
      const text = await response.text();
      if (new TextEncoder().encode(text).byteLength > 32_768)
        throw new Error("build provenance response is oversized");
      return decodeBuildProvenanceResponse(JSON.parse(text));
    },
  });
}
