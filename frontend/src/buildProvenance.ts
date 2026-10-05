import type { H3ContextBuildIdentity } from "../buildProvenance";

declare const __H3_CONTEXT_BUILD_PROVENANCE__: H3ContextBuildIdentity;

const oid = /^[0-9a-f]{40}$/;
const digest = /^sha256:[0-9a-f]{64}$/;
const value = __H3_CONTEXT_BUILD_PROVENANCE__;
if (
  value === null ||
  typeof value !== "object" ||
  Object.keys(value).sort().join("\0") !==
    [
      "schema",
      "builder_id",
      "source_commit",
      "source_tree",
      "source_inputs_sha256",
      "resolved_dependencies_sha256",
    ]
      .sort()
      .join("\0") ||
  value.schema !== "h3-context-build-identity/1" ||
  value.builder_id !== "scripts/frontend_build_report.py" ||
  !oid.test(value.source_commit) ||
  !oid.test(value.source_tree) ||
  !digest.test(value.source_inputs_sha256) ||
  !digest.test(value.resolved_dependencies_sha256)
)
  throw new Error("H3 Context embedded build identity is invalid");

export const H3_CONTEXT_BUILD_PROVENANCE: Readonly<H3ContextBuildIdentity> =
  Object.freeze({ ...value });
