import rawModalKeyboardTransition from "../../governance/contracts/host_seam_census_transition_modal_keyboard_v1.json" with { type: "json" };
import rawWorkspaceStateTransition from "../../governance/contracts/host_seam_census_transition_workspace_state_v1.json" with { type: "json" };
import rawRetainedMediaTransition from "../../governance/contracts/host_seam_census_transition_retained_media_v1.json" with { type: "json" };
import rawProjectPersistenceTransition from "../../governance/contracts/host_seam_census_transition_project_persistence_v1.json" with { type: "json" };
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
} from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import rawCensus from "../../comfyui_h3_context/contracts/host_seam_census_v1.json" with { type: "json" };
import rawBaseline from "../../governance/contracts/host_seam_drift_baseline_v1.json" with { type: "json" };
import rawFixture from "../../comfyui_h3_context/contracts/host_seam_shape_fixture_v1.json" with { type: "json" };
import rawTransition from "../../governance/contracts/host_seam_census_transition_v1.json" with { type: "json" };
import rawM23_18Transition from "../../governance/contracts/host_seam_census_transition_m23_18_v1.json" with { type: "json" };
import rawM23_19Transition from "../../governance/contracts/host_seam_census_transition_m23_19_v1.json" with { type: "json" };
import rawM23_25Transition from "../../governance/contracts/host_seam_census_transition_m23_25_v1.json" with { type: "json" };
import rawM25_02Transition from "../../governance/contracts/host_seam_census_transition_m25_02_v1.json" with { type: "json" };
import rawM23_24Transition from "../../governance/contracts/host_seam_census_transition_m23_24_v1.json" with { type: "json" };
import rawM23_47Transition from "../../governance/contracts/host_seam_census_transition_m23_47_v1.json" with { type: "json" };
import rawM23_28Transition from "../../governance/contracts/host_seam_census_transition_m23_28_v1.json" with { type: "json" };
import rawM26_04Transition from "../../governance/contracts/host_seam_census_transition_m26_04_v1.json" with { type: "json" };
import rawM26_03Transition from "../../governance/contracts/host_seam_census_transition_m26_03_v1.json" with { type: "json" };
import rawM25_13Transition from "../../governance/contracts/host_seam_census_transition_m25_13_v1.json" with { type: "json" };
import rawManagedQualificationTransition from "../../governance/contracts/host_seam_census_transition_managed_qualification_v1.json" with { type: "json" };
import rawM25_16Transition from "../../governance/contracts/host_seam_census_transition_m25_16_v1.json" with { type: "json" };
import rawM16_05Transition from "../../governance/contracts/host_seam_census_transition_m16_05_v1.json" with { type: "json" };
import rawM25_33Transition from "../../governance/contracts/host_seam_census_transition_m25_33_v1.json" with { type: "json" };
import rawM16_05ReleaseTransition from "../../governance/contracts/host_seam_census_transition_m16_05_release_v1.json" with { type: "json" };
import rawM25_38Transition from "../../governance/contracts/host_seam_census_transition_m25_38_v1.json" with { type: "json" };
import { resolveHc10PlaywrightOutput } from "../playwright.hc10.config";
import {
  assertTransportFailurePolicyClean,
  bindExactCandidateIdentity,
  classifyHostSeamDrift,
  hostProbeAvailability,
  parseHostSeamBaseline,
  summarizeHostProbeAudits,
  unavailableHostSeamProbe,
  validateHostSeamDriftEvidence,
  verifyHostSeamBaseline,
  type HostSeamBaselineWire,
  type HostSeamProbeRow,
} from "./support/hostSeamDrift";
import type { HostSeamObservationWire } from "./support/hostSeamObservation";
import {
  requiredEvidencePath,
  writeContentFreeEvidence,
} from "./support/hc09CapturePolicy";

const repositoryRoot = resolve(import.meta.dirname, "../..");
const censusBytes = readFileSync(
  resolve(
    repositoryRoot,
    "comfyui_h3_context/contracts/host_seam_census_v1.json",
  ),
);
const fixtureBytes = readFileSync(
  resolve(
    repositoryRoot,
    "comfyui_h3_context/contracts/host_seam_shape_fixture_v1.json",
  ),
);
const baseline: HostSeamBaselineWire = parseHostSeamBaseline(rawBaseline);
const acceptedTransitionChain = [
  rawTransition,
  rawM23_18Transition,
  rawM23_19Transition,
  rawM23_25Transition,
  rawM25_02Transition,
  rawM23_24Transition,
  rawM23_47Transition,
  rawM23_28Transition,
  rawM26_04Transition,
  rawM26_03Transition,
  rawM25_13Transition,
  rawManagedQualificationTransition,
  rawM25_16Transition,
  rawM16_05Transition,
  rawM25_33Transition,
] as const;
const currentTransitionChain = [
  ...acceptedTransitionChain,
  rawM16_05ReleaseTransition,
  rawM25_38Transition,
  rawModalKeyboardTransition,
  rawWorkspaceStateTransition,
  rawRetainedMediaTransition,
  rawProjectPersistenceTransition,
] as const;

function replacingCurrentTransition(reference: unknown, value: unknown) {
  return currentTransitionChain.map((row) => (row === reference ? value : row));
}

function observedRows(): HostSeamProbeRow[] {
  return rawFixture.observations.map((observation) => ({
    seam_id: observation.seam_id,
    availability: "OBSERVED" as const,
    observation: observation as HostSeamObservationWire,
  }));
}

type MutableTransition = {
  schema: string;
  item: string;
  source: Record<string, string[]>;
  artifacts: Record<string, string[]>;
  changes: Record<string, unknown>[];
};

describe("complete current compatibility chain", () => {
  it.each(currentTransitionChain.map((row, index) => ({ row, index })))(
    "rejects corruption/omission/reorder of transition $index ($row.item)",
    ({ row, index }) => {
      const transition = row as unknown as MutableTransition;
      const replace = (value: unknown) =>
        currentTransitionChain.map((entry, position) =>
          position === index ? value : entry,
        );
      // IMPORTANT: every corruption keeps all other current transitions. A shortened chain
      // would fail on length before exercising the historical field or new artifact pin.
      expect(
        verifyHostSeamBaseline(
          rawBaseline,
          censusBytes,
          fixtureBytes,
          currentTransitionChain,
        ),
      ).toEqual({ result: "PASS", reason: "COMPATIBILITY_TRANSITION_MATCH" });
      const wrongParts = (value: string[]) => ["00000000", ...value.slice(1)];
      const bad = [
        ...Object.keys(transition.source).map((key) => ({
          ...transition,
          source: {
            ...transition.source,
            [key]: wrongParts(transition.source[key]!),
          },
        })),
        ...Object.keys(transition.artifacts).map((key) => ({
          ...transition,
          artifacts: {
            ...transition.artifacts,
            [key]: wrongParts(transition.artifacts[key]!),
          },
        })),
        {
          ...transition,
          changes: transition.changes.map((change, position) => {
            if (position !== 0) return change;
            const key =
              "added_source_paths" in change
                ? "added_source_paths"
                : "removed_source_paths";
            return {
              ...change,
              [key]: [
                ...(change[key] as string[]),
                "frontend/src/host/unapproved.ts",
              ].sort(),
            };
          }),
        },
      ];
      for (const value of bad)
        expect(
          verifyHostSeamBaseline(
            rawBaseline,
            censusBytes,
            fixtureBytes,
            replace(value),
          ),
        ).toEqual({ result: "DRIFTED", reason: "REPOSITORY_BREAKAGE" });
      expect(
        verifyHostSeamBaseline(
          rawBaseline,
          censusBytes,
          fixtureBytes,
          currentTransitionChain.filter((_, position) => position !== index),
        ),
      ).toEqual({ result: "DRIFTED", reason: "REPOSITORY_BREAKAGE" });
      const reordered: unknown[] = [...currentTransitionChain];
      const other = index === 0 ? 1 : index - 1;
      [reordered[index], reordered[other]] = [
        reordered[other],
        reordered[index],
      ];
      expect(
        verifyHostSeamBaseline(
          rawBaseline,
          censusBytes,
          fixtureBytes,
          reordered,
        ),
      ).toEqual({ result: "DRIFTED", reason: "REPOSITORY_BREAKAGE" });
      expect(() =>
        verifyHostSeamBaseline(
          rawBaseline,
          censusBytes,
          fixtureBytes,
          replace({ ...transition, unexpected: true }),
        ),
      ).toThrow("closed shape");
      expect(() =>
        verifyHostSeamBaseline(
          rawBaseline,
          censusBytes,
          fixtureBytes,
          replace({ ...transition, item: "UNAPPROVED" }),
        ),
      ).toThrow("unsupported identity");
    },
  );
});

describe("HC-10 host seam drift classification", () => {
  it("keeps the manual probe outside ordinary Playwright output", () => {
    expect(resolveHc10PlaywrightOutput({})).toBe("test-results/hc10");
    expect(
      resolveHc10PlaywrightOutput({
        H3_CONTEXT_HC10_PLAYWRIGHT_OUTPUT: "B:/bounded/hc10",
      }),
    ).toBe("B:/bounded/hc10");
  });

  it("returns explicit NOT_RUN when no host was supplied", () => {
    expect(hostProbeAvailability(undefined)).toEqual({
      result: "NOT_RUN",
      reason: "HOST_NOT_SUPPLIED",
    });
    expect(hostProbeAvailability("http://127.0.0.1:8188/")).toEqual({
      result: "READY",
      reason: "HOST_SUPPLIED",
    });
  });

  it("binds the accepted HC-09 fixture and fails closed before transport on drift", () => {
    expect(
      verifyHostSeamBaseline(
        rawBaseline,
        censusBytes,
        fixtureBytes,
        currentTransitionChain,
      ),
    ).toEqual({
      result: "PASS",
      reason: "COMPATIBILITY_TRANSITION_MATCH",
    });
    expect(
      verifyHostSeamBaseline(rawBaseline, censusBytes, fixtureBytes),
    ).toEqual({
      result: "DRIFTED",
      reason: "REPOSITORY_BREAKAGE",
    });
    const corrupted = Buffer.from(fixtureBytes);
    corrupted[corrupted.length - 2] = 0x20;
    expect(
      verifyHostSeamBaseline(
        rawBaseline,
        censusBytes,
        corrupted,
        currentTransitionChain,
      ),
    ).toEqual({
      result: "DRIFTED",
      reason: "REPOSITORY_BREAKAGE",
    });
    expect(
      verifyHostSeamBaseline(
        rawBaseline,
        Buffer.concat([censusBytes, Buffer.from(" ")]),
        fixtureBytes,
        currentTransitionChain,
      ),
    ).toEqual({
      result: "DRIFTED",
      reason: "REPOSITORY_BREAKAGE",
    });
    expect(() =>
      verifyHostSeamBaseline(
        { ...rawBaseline, unexpected: true },
        censusBytes,
        fixtureBytes,
        currentTransitionChain,
      ),
    ).toThrow("closed shape");
    expect(() =>
      verifyHostSeamBaseline(rawBaseline, censusBytes, fixtureBytes, [
        { ...rawTransition, unexpected: true },
        rawM23_18Transition,
        rawM23_19Transition,
        rawM23_25Transition,
        rawM25_02Transition,
        rawM23_24Transition,
        rawM23_47Transition,
        rawM23_28Transition,
        rawM26_04Transition,
        rawM26_03Transition,
        rawM25_13Transition,
        rawManagedQualificationTransition,
        rawM25_16Transition,
        rawM16_05Transition,
        rawM25_33Transition,
        rawM16_05ReleaseTransition,
        rawM25_38Transition,
        rawModalKeyboardTransition,
        rawWorkspaceStateTransition,
        rawRetainedMediaTransition,
        rawProjectPersistenceTransition,
      ]),
    ).toThrow("closed shape");
    const wrongSource = {
      ...rawBaseline,
      source: {
        ...rawBaseline.source,
        commit_parts: ["00000000", ...rawBaseline.source.commit_parts.slice(1)],
      },
    };
    expect(
      verifyHostSeamBaseline(
        wrongSource,
        censusBytes,
        fixtureBytes,
        currentTransitionChain,
      ),
    ).toEqual({
      result: "DRIFTED",
      reason: "REPOSITORY_BREAKAGE",
    });
    const wrongTransitionSource = {
      ...rawTransition,
      source: {
        ...rawTransition.source,
        commit_parts: [
          "00000000",
          ...rawTransition.source.commit_parts.slice(1),
        ],
      },
    };
    expect(
      verifyHostSeamBaseline(
        rawBaseline,
        censusBytes,
        fixtureBytes,
        replacingCurrentTransition(rawTransition, wrongTransitionSource),
      ),
    ).toEqual({
      result: "DRIFTED",
      reason: "REPOSITORY_BREAKAGE",
    });
    const wrongTransitionContract = {
      ...rawTransition,
      artifacts: {
        ...rawTransition.artifacts,
        seam_contract_sha256_parts: [
          "00000000",
          ...rawTransition.artifacts.seam_contract_sha256_parts.slice(1),
        ],
      },
    };
    const wrongTransitionChanges = {
      ...rawTransition,
      changes: rawTransition.changes.map((change, index) =>
        index === 0
          ? {
              ...change,
              added_source_paths: [
                "comfyui_h3_context/adapters/comfyui_production_workspace.py",
              ],
            }
          : change,
      ),
    };
    for (const invalidTransition of [
      wrongTransitionContract,
      wrongTransitionChanges,
    ])
      expect(
        verifyHostSeamBaseline(
          rawBaseline,
          censusBytes,
          fixtureBytes,
          replacingCurrentTransition(rawTransition, invalidTransition),
        ),
      ).toEqual({
        result: "DRIFTED",
        reason: "REPOSITORY_BREAKAGE",
      });

    const wrongM23_18Source = {
      ...rawM23_18Transition,
      source: {
        ...rawM23_18Transition.source,
        tree_parts: [
          "00000000",
          ...rawM23_18Transition.source.tree_parts.slice(1),
        ],
      },
    };
    const wrongM23_18Target = {
      ...rawM23_18Transition,
      artifacts: {
        ...rawM23_18Transition.artifacts,
        to_census_sha256_parts: [
          "00000000",
          ...rawM23_18Transition.artifacts.to_census_sha256_parts.slice(1),
        ],
      },
    };
    const wrongM23_18Changes = {
      ...rawM23_18Transition,
      changes: rawM23_18Transition.changes.map((change, index) =>
        index === 0
          ? {
              ...change,
              added_source_paths: [
                "comfyui_h3_context/adapters/comfyui_media_preview.py",
              ],
            }
          : change,
      ),
    };
    const wrongM23_25Changes = {
      ...rawM23_25Transition,
      changes: rawM23_25Transition.changes.map((change, index) =>
        index === 0
          ? {
              ...change,
              to_owner: "frontend/src/entry.tsx",
            }
          : change,
      ),
    };
    const wrongM25_02Changes = {
      ...rawM25_02Transition,
      changes: rawM25_02Transition.changes.map((change, index) =>
        index === 0
          ? {
              ...change,
              added_source_paths: [
                "comfyui_h3_context/adapters/comfyui_media_preview.py",
              ],
            }
          : change,
      ),
    };
    const wrongM23_28Changes = {
      ...rawM23_28Transition,
      changes: rawM23_28Transition.changes.map((change, index) =>
        index === 0
          ? {
              ...change,
              to_owner: "frontend/src/host/queueSeam.ts",
            }
          : change,
      ),
    };
    const wrongM26_04Changes = {
      ...rawM26_04Transition,
      changes: rawM26_04Transition.changes.map((change, index) =>
        index === 0
          ? {
              ...change,
              added_source_paths: ["frontend/src/host/managedSequence.ts"],
            }
          : change,
      ),
    };
    const wrongM26_03Source = {
      ...rawM26_03Transition,
      source: {
        ...rawM26_03Transition.source,
        tree_parts: [
          "00000000",
          ...rawM26_03Transition.source.tree_parts.slice(1),
        ],
      },
    };
    const wrongM26_03Changes = {
      ...rawM26_03Transition,
      changes: rawM26_03Transition.changes.map((change, index) =>
        index === 0
          ? {
              ...change,
              added_source_paths: [
                "frontend/src/host/productionPlanningActions.ts",
              ],
            }
          : change,
      ),
    };
    const wrongM25_13Source = {
      ...rawM25_13Transition,
      source: {
        ...rawM25_13Transition.source,
        tree_parts: [
          "00000000",
          ...rawM25_13Transition.source.tree_parts.slice(1),
        ],
      },
    };
    const wrongM25_13Changes = {
      ...rawM25_13Transition,
      changes: [
        {
          ...rawM25_13Transition.changes[0],
          added_source_paths: ["frontend/src/host/authoringMediaPreview.ts"],
        },
      ],
    };
    for (const [reference, value] of [
      [rawM23_18Transition, wrongM23_18Source],
      [rawM23_18Transition, wrongM23_18Target],
      [rawM23_18Transition, wrongM23_18Changes],
      [rawM23_25Transition, wrongM23_25Changes],
      [rawM25_02Transition, wrongM25_02Changes],
      [rawM23_28Transition, wrongM23_28Changes],
      [rawM26_04Transition, wrongM26_04Changes],
      [rawM26_03Transition, wrongM26_03Source],
      [rawM26_03Transition, wrongM26_03Changes],
      [rawM25_13Transition, wrongM25_13Source],
      [rawM25_13Transition, wrongM25_13Changes],
    ] as const)
      expect(
        verifyHostSeamBaseline(
          rawBaseline,
          censusBytes,
          fixtureBytes,
          replacingCurrentTransition(reference, value),
        ),
      ).toEqual({
        result: "DRIFTED",
        reason: "REPOSITORY_BREAKAGE",
      });
  });

  it("rejects a missing or malformed M25-13 compatibility transition", () => {
    expect(
      verifyHostSeamBaseline(
        rawBaseline,
        censusBytes,
        fixtureBytes,
        currentTransitionChain.filter((row) => row !== rawM25_13Transition),
      ),
    ).toEqual({
      result: "DRIFTED",
      reason: "REPOSITORY_BREAKAGE",
    });
    expect(() =>
      verifyHostSeamBaseline(rawBaseline, censusBytes, fixtureBytes, [
        ...acceptedTransitionChain.slice(0, -5),
        { ...rawM25_13Transition, unexpected: true },
        rawManagedQualificationTransition,
        rawM25_16Transition,
        rawM16_05Transition,
        rawM25_33Transition,
        rawM16_05ReleaseTransition,
        rawM25_38Transition,
        rawModalKeyboardTransition,
        rawWorkspaceStateTransition,
        rawRetainedMediaTransition,
        rawProjectPersistenceTransition,
      ]),
    ).toThrow("closed shape");
  });

  it("requires the exact managed qualification census transition", () => {
    const prior = acceptedTransitionChain.slice(0, -4);
    const transition = rawManagedQualificationTransition;
    const wrongParts = (parts: readonly string[]) => [
      "00000000",
      ...parts.slice(1),
    ];
    const invalid = [
      {
        ...transition,
        source: {
          ...transition.source,
          commit_parts: wrongParts(transition.source.commit_parts),
        },
      },
      {
        ...transition,
        source: {
          ...transition.source,
          tree_parts: wrongParts(transition.source.tree_parts),
        },
      },
      ...Object.keys(transition.artifacts).map((key) => ({
        ...transition,
        artifacts: {
          ...transition.artifacts,
          [key]: wrongParts(
            transition.artifacts[key as keyof typeof transition.artifacts],
          ),
        },
      })),
      {
        ...transition,
        changes: transition.changes.slice(0, 1),
      },
      {
        ...transition,
        changes: transition.changes.map((change, index) =>
          index === 0
            ? {
                ...change,
                added_source_paths: [
                  ...change.added_source_paths,
                  "comfyui_h3_context/adapters/unapproved.py",
                ],
              }
            : change,
        ),
      },
    ];
    for (const chain of [
      currentTransitionChain.filter((row) => row !== transition),
      ...invalid.map((value) => replacingCurrentTransition(transition, value)),
    ])
      expect(
        verifyHostSeamBaseline(rawBaseline, censusBytes, fixtureBytes, chain),
      ).toEqual({ result: "DRIFTED", reason: "REPOSITORY_BREAKAGE" });
    expect(() =>
      verifyHostSeamBaseline(rawBaseline, censusBytes, fixtureBytes, [
        ...prior,
        { ...transition, unexpected: true },
        rawM25_16Transition,
        rawM16_05Transition,
        rawM25_33Transition,
        rawM16_05ReleaseTransition,
        rawM25_38Transition,
        rawModalKeyboardTransition,
        rawWorkspaceStateTransition,
        rawRetainedMediaTransition,
        rawProjectPersistenceTransition,
      ]),
    ).toThrow("closed shape");
    expect(() =>
      verifyHostSeamBaseline(rawBaseline, censusBytes, fixtureBytes, [
        ...prior,
        { ...transition, item: "UNAPPROVED" },
        rawM25_16Transition,
        rawM16_05Transition,
        rawM25_33Transition,
        rawM16_05ReleaseTransition,
        rawM25_38Transition,
        rawModalKeyboardTransition,
        rawWorkspaceStateTransition,
        rawRetainedMediaTransition,
        rawProjectPersistenceTransition,
      ]),
    ).toThrow("unsupported identity");
  });

  it("requires the exact M25-16 census transition", () => {
    const prior = acceptedTransitionChain.slice(0, -3);
    const transition = rawM25_16Transition;
    const wrongParts = (parts: readonly string[]) => [
      "00000000",
      ...parts.slice(1),
    ];
    const invalid = [
      {
        ...transition,
        source: {
          ...transition.source,
          commit_parts: wrongParts(transition.source.commit_parts),
        },
      },
      {
        ...transition,
        source: {
          ...transition.source,
          tree_parts: wrongParts(transition.source.tree_parts),
        },
      },
      ...Object.keys(transition.artifacts).map((key) => ({
        ...transition,
        artifacts: {
          ...transition.artifacts,
          [key]: wrongParts(
            transition.artifacts[key as keyof typeof transition.artifacts],
          ),
        },
      })),
      {
        ...transition,
        changes: transition.changes.map((change) => ({
          ...change,
          added_source_paths: change.added_source_paths.slice(0, 2),
        })),
      },
      {
        ...transition,
        changes: transition.changes.map((change, index) =>
          index === 0
            ? {
                ...change,
                added_source_paths: [
                  ...change.added_source_paths,
                  "frontend/src/lifecycle/unapproved.ts",
                ],
              }
            : change,
        ),
      },
    ];
    for (const chain of [
      currentTransitionChain.filter((row) => row !== transition),
      ...invalid.map((value) => replacingCurrentTransition(transition, value)),
    ])
      expect(
        verifyHostSeamBaseline(rawBaseline, censusBytes, fixtureBytes, chain),
      ).toEqual({ result: "DRIFTED", reason: "REPOSITORY_BREAKAGE" });
    expect(() =>
      verifyHostSeamBaseline(rawBaseline, censusBytes, fixtureBytes, [
        ...prior,
        { ...transition, unexpected: true },
        rawM16_05Transition,
        rawM25_33Transition,
        rawM16_05ReleaseTransition,
        rawM25_38Transition,
        rawModalKeyboardTransition,
        rawWorkspaceStateTransition,
        rawRetainedMediaTransition,
        rawProjectPersistenceTransition,
      ]),
    ).toThrow("closed shape");
    expect(() =>
      verifyHostSeamBaseline(rawBaseline, censusBytes, fixtureBytes, [
        ...prior,
        { ...transition, item: "UNAPPROVED" },
        rawM16_05Transition,
        rawM25_33Transition,
        rawM16_05ReleaseTransition,
        rawM25_38Transition,
        rawModalKeyboardTransition,
        rawWorkspaceStateTransition,
        rawRetainedMediaTransition,
        rawProjectPersistenceTransition,
      ]),
    ).toThrow("unsupported identity");
  });

  it("requires the exact M16-05 census transition", () => {
    const prior = acceptedTransitionChain.slice(0, -2);
    const transition = rawM16_05Transition;
    const wrongParts = (parts: readonly string[]) => [
      "00000000",
      ...parts.slice(1),
    ];
    const invalid = [
      {
        ...transition,
        source: {
          ...transition.source,
          commit_parts: wrongParts(transition.source.commit_parts),
        },
      },
      {
        ...transition,
        source: {
          ...transition.source,
          tree_parts: wrongParts(transition.source.tree_parts),
        },
      },
      ...Object.keys(transition.artifacts).map((key) => ({
        ...transition,
        artifacts: {
          ...transition.artifacts,
          [key]: wrongParts(
            transition.artifacts[key as keyof typeof transition.artifacts],
          ),
        },
      })),
      {
        ...transition,
        changes: transition.changes.map((change) => ({
          ...change,
          added_source_paths: [
            "frontend/src/lifecycle/nleWorkspacePlanning.ts",
          ],
        })),
      },
      {
        ...transition,
        changes: transition.changes.map((change, index) =>
          index === 0
            ? {
                ...change,
                added_source_paths: [
                  ...change.added_source_paths,
                  "frontend/src/lifecycle/unapproved.ts",
                ],
              }
            : change,
        ),
      },
    ];
    for (const chain of [
      currentTransitionChain.filter((row) => row !== transition),
      ...invalid.map((value) => replacingCurrentTransition(transition, value)),
    ])
      expect(
        verifyHostSeamBaseline(rawBaseline, censusBytes, fixtureBytes, chain),
      ).toEqual({ result: "DRIFTED", reason: "REPOSITORY_BREAKAGE" });
    expect(() =>
      verifyHostSeamBaseline(rawBaseline, censusBytes, fixtureBytes, [
        ...prior,
        { ...transition, unexpected: true },
        rawM25_33Transition,
        rawM16_05ReleaseTransition,
        rawM25_38Transition,
        rawModalKeyboardTransition,
        rawWorkspaceStateTransition,
        rawRetainedMediaTransition,
        rawProjectPersistenceTransition,
      ]),
    ).toThrow("closed shape");
    expect(() =>
      verifyHostSeamBaseline(rawBaseline, censusBytes, fixtureBytes, [
        ...prior,
        { ...transition, item: "UNAPPROVED" },
        rawM25_33Transition,
        rawM16_05ReleaseTransition,
        rawM25_38Transition,
        rawModalKeyboardTransition,
        rawWorkspaceStateTransition,
        rawRetainedMediaTransition,
        rawProjectPersistenceTransition,
      ]),
    ).toThrow("unsupported identity");
  });

  it("requires the exact M25-33 census transition", () => {
    const prior = acceptedTransitionChain.slice(0, -1);
    const transition = rawM25_33Transition;
    const wrongParts = (parts: readonly string[]) => [
      "00000000",
      ...parts.slice(1),
    ];
    const invalid = [
      {
        ...transition,
        source: {
          ...transition.source,
          commit_parts: wrongParts(transition.source.commit_parts),
        },
      },
      {
        ...transition,
        source: {
          ...transition.source,
          tree_parts: wrongParts(transition.source.tree_parts),
        },
      },
      ...Object.keys(transition.artifacts).map((key) => ({
        ...transition,
        artifacts: {
          ...transition.artifacts,
          [key]: wrongParts(
            transition.artifacts[key as keyof typeof transition.artifacts],
          ),
        },
      })),
      {
        ...transition,
        changes: transition.changes.map((change) => ({
          ...change,
          added_source_paths: ["frontend/src/host/mediaRuntimeCodec.ts"],
        })),
      },
      {
        ...transition,
        changes: transition.changes.map((change, index) =>
          index === 0
            ? {
                ...change,
                added_source_paths: [
                  ...change.added_source_paths,
                  "frontend/src/host/unapproved.ts",
                ],
              }
            : change,
        ),
      },
    ];
    for (const chain of [
      currentTransitionChain.filter((row) => row !== transition),
      ...invalid.map((value) => replacingCurrentTransition(transition, value)),
    ])
      expect(
        verifyHostSeamBaseline(rawBaseline, censusBytes, fixtureBytes, chain),
      ).toEqual({ result: "DRIFTED", reason: "REPOSITORY_BREAKAGE" });
    expect(() =>
      verifyHostSeamBaseline(rawBaseline, censusBytes, fixtureBytes, [
        ...prior,
        { ...transition, unexpected: true },
        rawM16_05ReleaseTransition,
        rawM25_38Transition,
        rawModalKeyboardTransition,
        rawWorkspaceStateTransition,
        rawRetainedMediaTransition,
        rawProjectPersistenceTransition,
      ]),
    ).toThrow("closed shape");
    expect(() =>
      verifyHostSeamBaseline(rawBaseline, censusBytes, fixtureBytes, [
        ...prior,
        { ...transition, item: "UNAPPROVED" },
        rawM16_05ReleaseTransition,
        rawM25_38Transition,
        rawModalKeyboardTransition,
        rawWorkspaceStateTransition,
        rawRetainedMediaTransition,
        rawProjectPersistenceTransition,
      ]),
    ).toThrow("unsupported identity");
  });

  it("requires the exact M16-05 release census transition", () => {
    const transition = rawM16_05ReleaseTransition;
    const wrongParts = (parts: readonly string[]) => [
      "00000000",
      ...parts.slice(1),
    ];
    const invalid = [
      {
        ...transition,
        source: {
          ...transition.source,
          commit_parts: wrongParts(transition.source.commit_parts),
        },
      },
      {
        ...transition,
        artifacts: {
          ...transition.artifacts,
          to_census_sha256_parts: wrongParts(
            transition.artifacts.to_census_sha256_parts,
          ),
        },
      },
      {
        ...transition,
        changes: transition.changes.map((change) => ({
          ...change,
          added_source_paths: change.added_source_paths.slice(0, -1),
        })),
      },
    ];
    for (const value of invalid)
      expect(
        verifyHostSeamBaseline(
          rawBaseline,
          censusBytes,
          fixtureBytes,
          replacingCurrentTransition(transition, value),
        ),
      ).toEqual({ result: "DRIFTED", reason: "REPOSITORY_BREAKAGE" });
    expect(() =>
      verifyHostSeamBaseline(rawBaseline, censusBytes, fixtureBytes, [
        ...acceptedTransitionChain,
        { ...transition, unexpected: true },
        rawM25_38Transition,
        rawModalKeyboardTransition,
        rawWorkspaceStateTransition,
        rawRetainedMediaTransition,
        rawProjectPersistenceTransition,
      ]),
    ).toThrow("closed shape");
    expect(() =>
      verifyHostSeamBaseline(rawBaseline, censusBytes, fixtureBytes, [
        ...acceptedTransitionChain,
        { ...transition, item: "UNAPPROVED" },
        rawM25_38Transition,
        rawModalKeyboardTransition,
        rawWorkspaceStateTransition,
        rawRetainedMediaTransition,
        rawProjectPersistenceTransition,
      ]),
    ).toThrow("unsupported identity");
  });

  it("requires the exact M25-38 census transition", () => {
    // IMPORTANT: M25-38 is the first removal-shape transition registered after M23-28 centralized
    // ownership, so its own `from_owner`/`to_owner` pair must stay covered here; a corrupted owner
    // pair is exactly the shape M25-38's fix newly admits, and only this test exercises it.
    const prior = [...acceptedTransitionChain, rawM16_05ReleaseTransition];
    const transition = rawM25_38Transition;
    const wrongParts = (parts: readonly string[]) => [
      "00000000",
      ...parts.slice(1),
    ];
    const invalid = [
      {
        ...transition,
        source: {
          ...transition.source,
          commit_parts: wrongParts(transition.source.commit_parts),
        },
      },
      {
        ...transition,
        source: {
          ...transition.source,
          tree_parts: wrongParts(transition.source.tree_parts),
        },
      },
      ...Object.keys(transition.artifacts).map((key) => ({
        ...transition,
        artifacts: {
          ...transition.artifacts,
          [key]: wrongParts(
            transition.artifacts[key as keyof typeof transition.artifacts],
          ),
        },
      })),
      {
        // A same-owner-shaped but additional removed path: still closed-shape valid (from_owner
        // stays inside the sorted, unique removed set), so this must fail on the exact-changes
        // comparison rather than on parse.
        ...transition,
        changes: transition.changes.map((change) => ({
          ...change,
          removed_source_paths: [
            "frontend/src/lifecycle/appModeSession.ts",
            "frontend/src/lifecycle/unapproved.ts",
          ],
        })),
      },
      {
        // A plausible but wrong owner: a valid source path, not the removed path, not the seam's
        // real owner -- parses cleanly and must fail the seam-owner pin instead.
        ...transition,
        changes: transition.changes.map((change) => ({
          ...change,
          to_owner: "frontend/src/host/appMode.ts",
        })),
      },
    ];
    for (const chain of [
      [...prior, rawModalKeyboardTransition],
      ...invalid.map((value) => [...prior, value, rawModalKeyboardTransition]),
    ])
      expect(
        verifyHostSeamBaseline(rawBaseline, censusBytes, fixtureBytes, chain),
      ).toEqual({ result: "DRIFTED", reason: "REPOSITORY_BREAKAGE" });
    expect(() =>
      verifyHostSeamBaseline(rawBaseline, censusBytes, fixtureBytes, [
        ...prior,
        { ...transition, unexpected: true },
        rawModalKeyboardTransition,
        rawWorkspaceStateTransition,
        rawRetainedMediaTransition,
        rawProjectPersistenceTransition,
      ]),
    ).toThrow("closed shape");
    expect(() =>
      verifyHostSeamBaseline(rawBaseline, censusBytes, fixtureBytes, [
        ...prior,
        { ...transition, item: "UNAPPROVED" },
        rawModalKeyboardTransition,
        rawWorkspaceStateTransition,
        rawRetainedMediaTransition,
        rawProjectPersistenceTransition,
      ]),
    ).toThrow("unsupported identity");
  });

  it("binds supplied candidate identities to actual repository truth", () => {
    expect(
      bindExactCandidateIdentity(baseline.source, baseline.source),
    ).toEqual(baseline.source);
    expect(() =>
      bindExactCandidateIdentity(
        { ...baseline.source, tree: "0".repeat(40) },
        baseline.source,
      ),
    ).toThrow("actual Git candidate");
  });

  it("records blocked attempts but never downgrades their transport failure", () => {
    const safeAudit = {
      allow: true,
      crossOrigin: 0,
      nonGet: 0,
      credentialBearing: 0,
      promptOperation: 0,
      workflowOperation: 0,
      mediaOperation: 0,
    };
    expect(() => assertTransportFailurePolicyClean([safeAudit])).not.toThrow();
    for (const field of [
      "crossOrigin",
      "nonGet",
      "credentialBearing",
      "promptOperation",
      "workflowOperation",
      "mediaOperation",
    ] as const) {
      const blocked = { ...safeAudit, allow: false, [field]: 1 };
      expect(() => assertTransportFailurePolicyClean([blocked])).toThrow(
        "policy violation",
      );
      expect(summarizeHostProbeAudits([blocked])).toEqual(
        expect.objectContaining({
          [{
            crossOrigin: "blocked_cross_origin_attempts",
            nonGet: "blocked_non_get_attempts",
            credentialBearing: "blocked_credential_attempts",
            promptOperation: "blocked_prompt_attempts",
            workflowOperation: "blocked_workflow_attempts",
            mediaOperation: "blocked_media_attempts",
          }[field]]: 1,
        }),
      );
    }
  });

  it("classifies all 29 exact observations as PASS", () => {
    const report = classifyHostSeamDrift(rawCensus, rawFixture, observedRows());
    expect(report).toEqual({
      result: "PASS",
      reason: "MATCH",
      summary: { passed: 29, drifted: 0, unavailable: 0 },
      seams: rawFixture.observations.map((row) => ({
        seam_id: row.seam_id,
        status: "PASS",
      })),
    });
  });

  it("distinguishes host drift from localized unavailability", () => {
    const drifted = observedRows();
    drifted[0] = {
      ...drifted[0]!,
      observation: {
        ...drifted[0]!.observation!,
        readiness_state: "unavailable",
      },
    };
    expect(classifyHostSeamDrift(rawCensus, rawFixture, drifted)).toEqual(
      expect.objectContaining({
        result: "DRIFTED",
        reason: "HOST_DRIFT",
        summary: { passed: 28, drifted: 1, unavailable: 0 },
      }),
    );

    const unavailable = observedRows();
    unavailable[1] = {
      seam_id: unavailable[1]!.seam_id,
      availability: "UNAVAILABLE",
      observation: null,
    };
    expect(classifyHostSeamDrift(rawCensus, rawFixture, unavailable)).toEqual(
      expect.objectContaining({
        result: "NOT_RUN",
        reason: "PARTIAL_UNAVAILABLE",
        summary: { passed: 28, drifted: 0, unavailable: 1 },
      }),
    );

    unavailable[0] = drifted[0]!;
    expect(classifyHostSeamDrift(rawCensus, rawFixture, unavailable)).toEqual(
      expect.objectContaining({
        result: "DRIFTED",
        reason: "HOST_DRIFT",
        summary: { passed: 27, drifted: 1, unavailable: 1 },
      }),
    );

    const absent = observedRows();
    absent[0] = {
      ...absent[0]!,
      observation: {
        ...absent[0]!.observation!,
        presence: "absent",
        readiness_state: "absent",
      },
    };
    expect(classifyHostSeamDrift(rawCensus, rawFixture, absent)).toEqual(
      expect.objectContaining({
        result: "DRIFTED",
        reason: "HOST_DRIFT",
        summary: { passed: 28, drifted: 1, unavailable: 0 },
      }),
    );
  });

  it("requires one closed probe row per recorded seam", () => {
    const missing = observedRows().slice(1);
    expect(() => classifyHostSeamDrift(rawCensus, rawFixture, missing)).toThrow(
      "complete",
    );
    const duplicate = observedRows();
    duplicate[1] = duplicate[0]!;
    expect(() =>
      classifyHostSeamDrift(rawCensus, rawFixture, duplicate),
    ).toThrow("sorted and unique");
    const unknown = observedRows();
    unknown[0] = { ...unknown[0]!, seam_id: "frontend.unknown.seam" };
    expect(() => classifyHostSeamDrift(rawCensus, rawFixture, unknown)).toThrow(
      "complete",
    );
  });

  it("projects a globally unavailable host into 29 unavailable rows", () => {
    expect(unavailableHostSeamProbe(rawFixture, "HOST_UNAVAILABLE")).toEqual(
      expect.objectContaining({
        result: "NOT_RUN",
        reason: "HOST_UNAVAILABLE",
        summary: { passed: 0, drifted: 0, unavailable: 29 },
      }),
    );
  });

  it("accepts only closed content-free drift evidence", () => {
    const safe = {
      schema: "h3.context.host_seam_drift_evidence.v1",
      item: "HC-10",
      result: "PASS",
      reason: "MATCH",
      source: baseline.source,
      probe: baseline.source,
      artifacts: baseline.artifacts,
      subject: { comfyui_version: "0.38.0", frontend_version: "1.53.6" },
      summary: { passed: 29, drifted: 0, unavailable: 0 },
      seams: rawFixture.observations.map((row) => ({
        seam_id: row.seam_id,
        status: "PASS",
      })),
      request_policy: {
        api_get_requests: 6,
        redirects: 0,
        browser_cookie_records_before: 0,
        browser_cookie_records_after: 0,
        model_or_queue_operations: 0,
        queue_before_running: 0,
        queue_before_pending: 0,
        queue_after_running: 0,
        queue_after_pending: 0,
        blocked_cross_origin_attempts: 8,
        blocked_non_get_attempts: 18,
        blocked_credential_attempts: 0,
        blocked_prompt_attempts: 0,
        blocked_workflow_attempts: 0,
        blocked_media_attempts: 1,
      },
      privacy: {
        prompts: 0,
        workflows: 0,
        media: 0,
        credentials: 0,
        cookies: 0,
        paths_or_urls: 0,
        arbitrary_host_values: 0,
      },
    };
    expect(() =>
      validateHostSeamDriftEvidence(
        safe,
        rawFixture,
        baseline,
        baseline.source,
      ),
    ).not.toThrow();
    expect(() =>
      validateHostSeamDriftEvidence(
        {
          ...safe,
          subject: {
            ...safe.subject,
            comfyui_revision: [
              "b323a345",
              "bbbfb2f3",
              "a95b5b73",
              "b68eb791",
              "9a26515e",
            ].join(""),
          },
        },
        rawFixture,
        baseline,
        baseline.source,
      ),
    ).toThrow("closed shape");
    for (const unsafe of [
      { ...safe, host_url: "http://127.0.0.1:8188/" },
      JSON.parse('{"constructor":true}'),
    ])
      expect(() =>
        validateHostSeamDriftEvidence(
          unsafe,
          rawFixture,
          baseline,
          baseline.source,
        ),
      ).toThrow();

    const wrongIds = {
      ...safe,
      seams: safe.seams.map((row, index) =>
        index === 0 ? { ...row, seam_id: "backend.aaa" } : row,
      ),
    };
    const wrongSemantics = { ...safe, result: "NOT_RUN" };
    const unsafePolicy = {
      ...safe,
      request_policy: {
        ...safe.request_policy,
        model_or_queue_operations: 1,
      },
    };
    for (const inconsistent of [wrongIds, wrongSemantics, unsafePolicy])
      expect(() =>
        validateHostSeamDriftEvidence(
          inconsistent,
          rawFixture,
          baseline,
          baseline.source,
        ),
      ).toThrow();

    // IMPORTANT: clean public checkouts have no private evidence directory to inherit.
    mkdirSync(resolve(repositoryRoot, ".planning"), { recursive: true });
    const temporary = mkdtempSync(
      resolve(repositoryRoot, ".planning", "hc10-evidence-test-"),
    );
    try {
      const target = requiredEvidencePath(
        repositoryRoot,
        resolve(temporary, "evidence.json"),
      );
      expect(() =>
        writeContentFreeEvidence(
          target,
          safe,
          () => `${JSON.stringify({ ...safe, subject: null }, null, 2)}\n`,
          (candidate) =>
            validateHostSeamDriftEvidence(
              candidate,
              rawFixture,
              baseline,
              baseline.source,
            ),
        ),
      ).toThrow("PASS evidence");
      expect(existsSync(target)).toBe(false);
    } finally {
      rmSync(temporary, { recursive: true, force: false });
    }
  });
});
