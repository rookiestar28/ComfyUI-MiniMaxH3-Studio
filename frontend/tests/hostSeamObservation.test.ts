import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import rawCensus from "../../comfyui_h3_context/contracts/host_seam_census_v1.json" with { type: "json" };
import rawFixture from "../../comfyui_h3_context/contracts/host_seam_shape_fixture_v1.json" with { type: "json" };
import {
  compareObservedHostSeamFixture,
  normalizeObservedHostSeamFixture,
  type HostSeamObservationWire,
} from "./support/hostSeamObservation";
import { classifyHostSeamDrift } from "./support/hostSeamDrift";
import { observeFrontendHostSeamsInPage } from "./support/hostSeamLiveProbe";

const repositoryRoot = resolve(import.meta.dirname, "../..");

describe("HC-09 live host-seam observation normalizer", () => {
  it("returns the exact canonical frontend row order before normalization", async () => {
    const observed = (await observeFrontendHostSeamsInPage()).observations;
    const expected = rawFixture.observations
      .filter((row) => row.seam_id.startsWith("frontend."))
      .map((row) => row.seam_id);
    expect(observed.map((row) => row.seam_id)).toEqual(expected);
  });

  it("joins the real collector into the strict classifier without normalization", async () => {
    const frontend = (await observeFrontendHostSeamsInPage()).observations;
    const backend = rawFixture.observations.filter((row) =>
      row.seam_id.startsWith("backend."),
    ) as HostSeamObservationWire[];
    const report = classifyHostSeamDrift(
      rawCensus,
      rawFixture,
      [...backend, ...frontend].map((observation) => ({
        seam_id: observation.seam_id,
        availability: "OBSERVED",
        observation,
      })),
    );
    // A unit page has no supplied host; missing capabilities must classify as
    // drift rather than fail the collector's canonical envelope contract.
    expect(report).toMatchObject({ result: "DRIFTED", reason: "HOST_DRIFT" });
    expect(report.summary.unavailable).toBe(0);
  });

  it("normalizes all 29 rows byte-identically and includes six backend witnesses", () => {
    const normalized = normalizeObservedHostSeamFixture(
      rawCensus,
      rawFixture.subject,
      rawFixture.observations as HostSeamObservationWire[],
    );
    expect(normalized.wire.observations).toHaveLength(29);
    expect(
      normalized.wire.observations.filter((row) =>
        row.seam_id.startsWith("backend."),
      ),
    ).toHaveLength(6);
    expect(normalized.bytes).toBe(
      readFileSync(
        resolve(
          repositoryRoot,
          "comfyui_h3_context/contracts/host_seam_shape_fixture_v1.json",
        ),
        "utf-8",
      ),
    );
    expect(compareObservedHostSeamFixture(rawFixture, normalized.wire)).toEqual(
      {
        result: "PASS",
        passed: 29,
        drifted: 0,
      },
    );
  });

  it("reports a deliberately corrupted observed seam as DRIFTED", () => {
    const observed = structuredClone(
      rawFixture.observations,
    ) as HostSeamObservationWire[];
    observed[0] = { ...observed[0]!, kind: "object" };
    const normalized = normalizeObservedHostSeamFixture(
      rawCensus,
      rawFixture.subject,
      observed,
    );
    expect(compareObservedHostSeamFixture(rawFixture, normalized.wire)).toEqual(
      {
        result: "DRIFTED",
        passed: 28,
        drifted: 1,
      },
    );
  });

  it("fails closed when a live row is missing", () => {
    expect(() =>
      normalizeObservedHostSeamFixture(
        rawCensus,
        rawFixture.subject,
        rawFixture.observations.slice(1) as HostSeamObservationWire[],
      ),
    ).toThrow();
  });
});
