import { join } from "node:path";
import { writeFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import {
  BLOCKER_CODES,
  CompositionContractError,
} from "../src/contracts/compositionCodec";
import { runTypescriptCorpus } from "./support/semanticParity";

const corpusPath = join(
  process.cwd(),
  "..",
  "tests",
  "fixtures",
  "m23_46_semantic_parity_v1.json",
);

describe("M23-46 shared semantic parity corpus", () => {
  // CRITICAL: shape only. Whether a row is semantically right is decided by
  // `scripts/semantic_parity.compare_reports`, against the Python report and the corpus, so that a
  // planted TypeScript drift is named by case rather than surfacing here as an opaque red suite
  // that the Python side can only report as "the runner failed closed".
  it("emits one well-formed row per corpus case, carrying no exception prose", () => {
    const report = runTypescriptCorpus(
      process.env.M23_46_CORPUS_PATH ?? corpusPath,
    );
    expect(report.schema).toBe("h3.context.semantic_parity_report.v1");
    expect(report.engine).toBe("typescript");
    expect(report.corpus_sha256).toMatch(/^sha256:[0-9a-f]{64}$/u);
    expect(report.rows.length).toBeGreaterThanOrEqual(12);
    for (const row of report.rows) {
      expect(Object.keys(row).sort()).toEqual([
        "canonical_fingerprint",
        "case_id",
        "category",
        "code",
        "projection",
        "status",
      ]);
      expect(["accepted", "rejected"]).toContain(row.status);
    }
    expect(new Set(report.rows.map((row) => row.case_id)).size).toBe(
      report.rows.length,
    );
    if (process.env.M23_46_REPORT_PATH)
      writeFileSync(
        process.env.M23_46_REPORT_PATH,
        JSON.stringify(report),
        "utf8",
      );
  });

  // The TypeScript half of a property Python already had: `.code` is always a member of the shared
  // blocker vocabulary, so a reader that switches on it cannot meet a case it has no branch for.
  // The two constructors are the only place either implementation can enforce that.
  it("collapses an unlisted rejection code to invalid_contract, as Python does", () => {
    expect(new CompositionContractError("not_a_blocker_code", "x").code).toBe(
      "invalid_contract",
    );
    expect(
      new CompositionContractError("not_a_blocker_code", "x").message,
    ).toBe("invalid_contract: x");
    for (const code of BLOCKER_CODES)
      expect(new CompositionContractError(code, "x").code).toBe(code);
  });
});
