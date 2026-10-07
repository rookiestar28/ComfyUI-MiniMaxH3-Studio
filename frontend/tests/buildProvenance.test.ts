import { readFileSync } from "node:fs";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { describe, expect, it, vi } from "vitest";

import { H3_CONTEXT_BUILD_PROVENANCE } from "../src/buildProvenance";
import {
  resolveH3ContextBuildIdentity,
  validateH3ContextBuildIdentity,
} from "../buildProvenance";
import {
  BUILD_PROVENANCE_ROUTE,
  createBuildProvenanceClient,
  decodeBuildProvenanceResponse,
} from "../src/host/buildProvenanceClient";

const identity = {
  schema: "h3-context-build-identity/1" as const,
  builder_id: "scripts/frontend_build_report.py",
  source_commit: "1".repeat(40),
  source_tree: "2".repeat(40),
  source_inputs_sha256: `sha256:${"3".repeat(64)}`,
  resolved_dependencies_sha256: `sha256:${"4".repeat(64)}`,
};

function response(matches = true, buildIdentity = identity): unknown {
  return {
    schema: "h3.context.build_provenance_response.v1",
    record: {
      schema: "h3-context-build-provenance/1",
      external_parameters: { source_commit: buildIdentity.source_commit },
      bundle: {
        sha256: `sha256:${(matches ? "6" : "5").repeat(64)}`,
      },
      embedded_identity: buildIdentity,
    },
    served_bundle: {
      sha256: `sha256:${"6".repeat(64)}`,
      matches_record: matches,
    },
  };
}

describe("M23-24 build provenance", () => {
  it("embeds the exact package build identity and validates every digest", () => {
    // The define and resolveH3ContextBuildIdentity() are the same function reading the same
    // file, so comparing them asserts nothing. Read the record independently instead, so this
    // row fails if the value baked into the bundle ever stops matching the shipped record.
    const recorded = JSON.parse(
      readFileSync(
        join(
          process.cwd().endsWith("frontend")
            ? join(process.cwd(), "..")
            : process.cwd(),
          "comfyui_h3_context",
          "contracts",
          "build_provenance_v1.json",
        ),
        "utf8",
      ),
    ) as { embedded_identity: unknown };
    expect(H3_CONTEXT_BUILD_PROVENANCE).toEqual(recorded.embedded_identity);
    expect(validateH3ContextBuildIdentity(identity)).toEqual(identity);
    expect(() =>
      validateH3ContextBuildIdentity({ ...identity, extra: true }),
    ).toThrow(/invalid/i);
    expect(() =>
      validateH3ContextBuildIdentity({ ...identity, source_tree: "dirty" }),
    ).toThrow(/invalid/i);
  });

  it("fails closed when the package record is missing its embedded identity", async () => {
    const root = await mkdtemp(join(tmpdir(), "h3-provenance-"));
    try {
      const contracts = join(root, "comfyui_h3_context", "contracts");
      await mkdir(contracts, { recursive: true });
      await writeFile(
        join(contracts, "build_provenance_v1.json"),
        JSON.stringify({ schema: "h3-context-build-provenance/1" }),
      );
      expect(() => resolveH3ContextBuildIdentity(root)).toThrow(/invalid/i);
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });

  it("decodes observed served bytes and preserves mismatch as advisory state", () => {
    expect(decodeBuildProvenanceResponse(response(false), identity)).toEqual({
      sourceCommit: "1".repeat(40),
      sourceInputsSha256: `sha256:${"3".repeat(64)}`,
      bundleSha256: `sha256:${"6".repeat(64)}`,
      bundleMatchesRecord: false,
    });
  });

  it("loads only the same-origin bounded GET route", async () => {
    const fetchApi = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      text: async () =>
        JSON.stringify(response(true, H3_CONTEXT_BUILD_PROVENANCE)),
    });
    const projection = await createBuildProvenanceClient({ fetchApi }).load();
    expect(fetchApi).toHaveBeenCalledWith(
      BUILD_PROVENANCE_ROUTE,
      expect.objectContaining({ method: "GET", credentials: "same-origin" }),
    );
    expect(projection.bundleMatchesRecord).toBe(true);
  });
});
