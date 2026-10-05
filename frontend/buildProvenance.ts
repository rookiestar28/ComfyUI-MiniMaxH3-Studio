import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";

export type H3ContextBuildIdentity = {
  schema: "h3-context-build-identity/1";
  builder_id: string;
  source_commit: string;
  source_tree: string;
  source_inputs_sha256: string;
  resolved_dependencies_sha256: string;
};

const oid = /^[0-9a-f]{40}$/;
const digest = /^sha256:[0-9a-f]{64}$/;
const builder = "scripts/frontend_build_report.py";

function exactKeys(
  value: Record<string, unknown>,
  expected: string[],
): boolean {
  return (
    Object.keys(value).sort().join("\0") === [...expected].sort().join("\0")
  );
}

export function validateH3ContextBuildIdentity(
  value: unknown,
): H3ContextBuildIdentity {
  if (value === null || typeof value !== "object" || Array.isArray(value))
    throw new Error("H3 Context build identity is invalid");
  const identity = value as Record<string, unknown>;
  if (
    !exactKeys(identity, [
      "schema",
      "builder_id",
      "source_commit",
      "source_tree",
      "source_inputs_sha256",
      "resolved_dependencies_sha256",
    ]) ||
    identity.schema !== "h3-context-build-identity/1" ||
    identity.builder_id !== builder ||
    typeof identity.source_commit !== "string" ||
    !oid.test(identity.source_commit) ||
    typeof identity.source_tree !== "string" ||
    !oid.test(identity.source_tree) ||
    typeof identity.source_inputs_sha256 !== "string" ||
    !digest.test(identity.source_inputs_sha256) ||
    typeof identity.resolved_dependencies_sha256 !== "string" ||
    !digest.test(identity.resolved_dependencies_sha256)
  )
    throw new Error("H3 Context build identity is invalid");
  return { ...(identity as H3ContextBuildIdentity) };
}

export function resolveH3ContextBuildIdentity(
  rootDirectory = existsSync(join(process.cwd(), "pyproject.toml"))
    ? process.cwd()
    : dirname(process.cwd()),
): H3ContextBuildIdentity {
  let parsed: unknown;
  try {
    parsed = JSON.parse(
      readFileSync(
        join(
          rootDirectory,
          "comfyui_h3_context",
          "contracts",
          "build_provenance_v1.json",
        ),
        "utf8",
      ),
    );
  } catch (error) {
    throw new Error("cannot read H3 Context build provenance", {
      cause: error,
    });
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed))
    throw new Error("H3 Context build provenance is invalid");
  return validateH3ContextBuildIdentity(
    (parsed as { embedded_identity?: unknown }).embedded_identity,
  );
}
