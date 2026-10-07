// M25-21 hardening observations. A journey runs its assertions through a `Checks` list, so every
// assertion name recorded in an observation is one whose `expect` actually executed and passed
// before the observation was attached; nothing is recorded ahead of the check it names. The
// gatherer (`nleHardeningGatherer.mjs`) collects every `nle_hardening.*` attachment together with
// the collected ID of the test that attached it, and `scripts/nle_hardening_report.py` accepts an
// observation only from the exact passing test the coverage manifest names.

import type { TestInfo } from "@playwright/test";

import { evidenceCapture } from "./evidence";

export type Dimension = "accessibility" | "stress" | "recovery";

export class Checks {
  readonly names: string[] = [];

  /** Run one identified assertion; its name is kept only if it passed. */
  async step(name: string, assertion: () => Promise<void> | void) {
    await assertion();
    this.names.push(name);
  }
}

export type MeasurementBody = Readonly<{
  observed: number;
  unit: "ratio" | "ms" | "bytes" | "count" | "frames";
  method: string;
  sample_count: number;
  interval_ms: number;
  facts?: Readonly<Record<string, unknown>>;
}>;

export function hardeningEvidence(testInfo: TestInfo) {
  const capture = evidenceCapture(testInfo);
  const verdict = (checks: Checks, facts: Record<string, unknown> = {}) => {
    if (checks.names.length === 0)
      throw new Error(
        "a hardening observation needs at least one executed check",
      );
    return { status: "PASS", assertions: [...checks.names], facts };
  };
  return Object.freeze({
    /** `nle_hardening.<row>.<dimension or subcase>` for a command, UI, action or import row. */
    row: (
      row: string,
      dimension: Dimension | string,
      checks: Checks,
      facts?: Record<string, unknown>,
    ) =>
      capture.attach(
        `nle_hardening.${row}.${dimension}`,
        verdict(checks, facts),
      ),
    measurement: (id: string, body: MeasurementBody) =>
      capture.attach(`nle_hardening.measurement.${id}`, body),
    seam: (id: string, checks: Checks, facts: Record<string, unknown>) =>
      capture.attach(
        `nle_hardening.seam.${id}.recovery`,
        verdict(checks, facts),
      ),
    workload: (
      fixture: string,
      checks: Checks,
      facts: Record<string, unknown>,
    ) =>
      capture.attach(
        `nle_hardening.workload.${fixture}.identity`,
        verdict(checks, facts),
      ),
  });
}

/** Nearest-rank p95 (plan section 14.2), undefined for an empty sample rather than zero. */
export function p95(values: readonly number[]): number | undefined {
  if (values.length === 0) return undefined;
  const sorted = [...values].sort((left, right) => left - right);
  return sorted[Math.ceil(sorted.length * 0.95) - 1];
}
