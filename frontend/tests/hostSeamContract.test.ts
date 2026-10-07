import { describe, expect, it } from "vitest";

import {
  HOST_NODE_DEFINITION_CEILING,
  HOST_SEAM_CONTRACT,
  HostSeamContractError,
  classifyHostMappingReadiness,
  parseHostSeamContract,
} from "../src/host/hostSeamContract";
import { hostSeamFixture } from "./fixtures/entryHostModules";
import { bindFrontendHostSeamFixture } from "./support/hostSeamFixture";

describe("HC-09 host seam contract", () => {
  it("joins the closed census and content-free fixture", () => {
    expect(HOST_SEAM_CONTRACT.rows).toHaveLength(29);
    expect(HOST_SEAM_CONTRACT.rows.map((row) => row.id)).toEqual(
      [...HOST_SEAM_CONTRACT.rows.map((row) => row.id)].sort(),
    );
    expect(HOST_NODE_DEFINITION_CEILING).toBe(12_762);
    expect(
      HOST_SEAM_CONTRACT.byId["frontend.litegraph.registered_node_types"].bound,
    ).toEqual({
      observed_floor: 6_381,
      multiplier: 2,
      derived_ceiling: 12_762,
      absolute_ceiling: 50_000,
    });
  });

  it("models the four mapping-readiness states explicitly", () => {
    expect(classifyHostMappingReadiness(undefined, false)).toBe("absent");
    expect(classifyHostMappingReadiness({}, false)).toBe("present_not_ready");
    expect(classifyHostMappingReadiness({ Ready: {} }, true)).toBe("ready");
    expect(classifyHostMappingReadiness({}, true)).toBe("unavailable");
    expect(classifyHostMappingReadiness([], true)).toBe("unavailable");
  });

  it("rejects unknown census members instead of widening the host surface", () => {
    const census = structuredClone(HOST_SEAM_CONTRACT.censusWire) as {
      seams: Array<Record<string, unknown>>;
    };
    census.seams[0]!.arbitrary_host_value = "not admitted";
    expect(() =>
      parseHostSeamContract(census, HOST_SEAM_CONTRACT.fixtureWire),
    ).toThrow(HostSeamContractError);
  });

  it("rejects incomplete fixture joins", () => {
    const fixture = structuredClone(HOST_SEAM_CONTRACT.fixtureWire) as {
      observations: unknown[];
    };
    fixture.observations.pop();
    expect(() =>
      parseHostSeamContract(HOST_SEAM_CONTRACT.censusWire, fixture),
    ).toThrow(HostSeamContractError);
  });

  it("rejects a ceiling that does not match the measured bounded formula", () => {
    const census = structuredClone(HOST_SEAM_CONTRACT.censusWire) as {
      seams: Array<{ id: string; bound: Record<string, unknown> | null }>;
    };
    census.seams.find(
      (row) => row.id === "frontend.litegraph.registered_node_types",
    )!.bound!.derived_ceiling = 10_000;
    expect(() =>
      parseHostSeamContract(census, HOST_SEAM_CONTRACT.fixtureWire),
    ).toThrow(HostSeamContractError);
  });

  it("accepts the exact bounded formula and rejects multiplier/absolute overflow", () => {
    const atBoundary = structuredClone(HOST_SEAM_CONTRACT.censusWire) as {
      seams: Array<{ id: string; bound: Record<string, unknown> | null }>;
    };
    const bound = atBoundary.seams.find(
      (row) => row.id === "frontend.litegraph.registered_node_types",
    )!.bound!;
    bound.observed_floor = 25_000;
    bound.multiplier = 2;
    bound.derived_ceiling = 50_000;
    bound.absolute_ceiling = 50_000;
    expect(() =>
      parseHostSeamContract(atBoundary, HOST_SEAM_CONTRACT.fixtureWire),
    ).not.toThrow();

    const multiplierOverflow = structuredClone(atBoundary);
    multiplierOverflow.seams.find(
      (row) => row.id === "frontend.litegraph.registered_node_types",
    )!.bound!.multiplier = 17;
    expect(() =>
      parseHostSeamContract(multiplierOverflow, HOST_SEAM_CONTRACT.fixtureWire),
    ).toThrow(HostSeamContractError);

    const hostileFloor = structuredClone(atBoundary);
    hostileFloor.seams.find(
      (row) => row.id === "frontend.litegraph.registered_node_types",
    )!.bound!.observed_floor = 1_000_001;
    expect(() =>
      parseHostSeamContract(hostileFloor, HOST_SEAM_CONTRACT.fixtureWire),
    ).toThrow(HostSeamContractError);
  });

  it("binds the shared hermetic host double to every frontend census row", () => {
    const expected = HOST_SEAM_CONTRACT.rows
      .filter((row) => row.layer === "frontend")
      .map((row) => row.id);
    expect(Object.keys(hostSeamFixture).sort()).toEqual(expected);

    const incomplete = { ...hostSeamFixture };
    delete incomplete["frontend.api.fetch_api"];
    expect(() => bindFrontendHostSeamFixture(incomplete)).toThrow(
      "frontend host seam fixture does not join the census",
    );

    expect(() =>
      bindFrontendHostSeamFixture({
        ...hostSeamFixture,
        "frontend.api.fetch_api": {},
      }),
    ).toThrow("host seam double is not callable");
  });
});
