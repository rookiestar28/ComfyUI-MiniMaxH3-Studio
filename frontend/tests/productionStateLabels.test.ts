import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import type { Locale } from "../src/i18n/catalog";
import {
  productionBlockerTone,
  productionStateLabel,
  productionStateTone,
  type ProductionStateDimension,
} from "../src/components/productionStateLabels";

const LOCALES: readonly Locale[] = ["en", "zh-TW", "zh-CN"];

/** Every code the production workbench codec can decode and render. */
const VOCABULARY: ReadonlyArray<
  readonly [ProductionStateDimension, readonly string[]]
> = [
  [
    "runState",
    ["unavailable", "ready", "running", "succeeded", "failed", "cancelled"],
  ],
  ["reconstructionState", ["unavailable", "complete"]],
  [
    "closureState",
    [
      "unavailable",
      "clean",
      "dirty_self",
      "dirty_upstream",
      "blocked_missing_predecessor",
      "requires_full_recompute",
    ],
  ],
  [
    "jobState",
    [
      "unavailable",
      "clean",
      "planned",
      "projected",
      "submitted",
      "running",
      "output_verification_failed",
      "succeeded",
      "failed",
      "timed_out",
      "cancelled",
      "unknown_ownership",
    ],
  ],
  ["artifactState", ["unavailable", "partial", "complete", "failed"]],
  [
    "continuityState",
    ["unavailable", "cut", "restart", "native_frame_handoff"],
  ],
  [
    "boundaryKind",
    ["independent", "native_handoff", "adjacent", "cut", "reset"],
  ],
  ["outputState", ["pending", "ready", "failed", "unavailable"]],
  ["blocker", ["sequence_authority_unavailable"]],
  [
    "authority",
    [
      "h3.context.generation_sequence_projection.v1",
      "h3.context.segment_artifact_receipt.v1",
      "h3.context.continuity_boundary_receipt.v1",
      "h3.context.av_reconstruction_receipt.v1",
    ],
  ],
];

describe("M17-21 production state label layer", () => {
  it("maps every known backend code in all supported locales", () => {
    for (const locale of LOCALES)
      for (const [dimension, codes] of VOCABULARY)
        for (const code of codes) {
          const label = productionStateLabel(locale, dimension, code);
          expect(label, `${locale}/${dimension}/${code}`).not.toBe("");
          expect(label, `${locale}/${dimension}/${code}`).not.toBe(code);
        }
  });

  it("falls back to the raw code instead of hiding an unknown state", () => {
    for (const locale of LOCALES) {
      expect(
        productionStateLabel(locale, "jobState", "some_future_backend_state"),
      ).toBe("some_future_backend_state");
      expect(productionStateLabel(locale, "blocker", "future_blocker")).toBe(
        "future_blocker",
      );
    }
  });

  it("assigns a fault tone to failures and a neutral tone to unknown codes", () => {
    for (const code of [
      "failed",
      "timed_out",
      "blocked_missing_predecessor",
      "output_verification_failed",
      "unknown_ownership",
    ])
      expect(productionStateTone(code)).toBe("danger");
    for (const code of ["clean", "complete", "succeeded", "ready"])
      expect(productionStateTone(code)).toBe("ok");
    for (const code of ["dirty_self", "dirty_upstream", "partial"])
      expect(productionStateTone(code)).toBe("warn");
    expect(productionStateTone("unavailable")).toBe("idle");
    expect(productionStateTone("some_future_backend_state")).toBe("idle");
    expect(productionBlockerTone()).toBe("danger");
  });

  it("keeps internal roadmap identifiers out of user-facing copy", () => {
    for (const file of [
      "src/components/ProductionWorkbench.tsx",
      "src/components/productionStateLabels.ts",
      "src/i18n/catalog.ts",
    ]) {
      const source = readFileSync(join(process.cwd(), file), "utf8");
      const quoted = [...source.matchAll(/"([^"\\]|\\.)*"/g)].map(
        (match) => match[0],
      );
      for (const literal of quoted)
        expect(literal, `${file} ${literal}`).not.toMatch(
          /\bM1[0-9]-[0-9]{2}\b/,
        );
    }
  });
});
