// Shared canonical-harness helpers for the M25-16 NLE hermetic journeys: the overlay harness
// page with `canonical=1` sends every timeline transaction to a Playwright route that replays it
// through the real Python core (`scripts/m25_16_timeline_fixture.py`), so accepted receipts,
// conflict projections and refusals here are the backend's, not a browser fixture's.

import { expect, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { join } from "node:path";
import { fixturePython } from "../../../e2e/fixturePython";

import type { NleWorkspaceHarnessSnapshot } from "../../../e2e/nleWorkspace";
import { NLE_AUTHORING_SCHEMA } from "../../../src/contracts/authoringWorkbenchCodec";
import {
  compositionContractFingerprint,
  publicCompositionFingerprint,
} from "../../../src/contracts/compositionCodec";

export const surface = '[data-h3-nle-surface="overlay_v1"]';
export const grip = '[data-h3-nle-trim-edge="end"]:not([hidden])';
export const snapshot = (page: Page): Promise<NleWorkspaceHarnessSnapshot> =>
  page.evaluate(() => window.nleWorkspaceHarness.snapshot());

export type Interleave = (
  transaction: Record<string, unknown>,
  history: unknown[],
) => void;

type CanonicalDiagnostics = {
  fixtureCalls: { durationMs: number; outcome: "ok" | "timeout" | "error" }[];
  fixtureCallsOmitted: number;
  bootstrapCompletedMs?: number;
  schemaCompletedMs?: number;
  pageErrors: number;
};
const diagnosticsByPage = new WeakMap<Page, CanonicalDiagnostics>();

export async function canonicalFailureDiagnostics(page: Page) {
  const diagnostics = diagnosticsByPage.get(page);
  if (!diagnostics) return undefined;
  let facts = { surface: "unknown", launcherAttached: false };
  try {
    facts = await page.evaluate(() => {
      const value = document.querySelector(
        '[data-h3-harness="surface"]',
      )?.textContent;
      return {
        surface:
          value === undefined
            ? "absent"
            : ["collapsed", "opening", "expanded", "closing"].includes(
                  value ?? "",
                )
              ? value!
              : "unknown",
        launcherAttached:
          document.querySelector('[data-h3-nle-entry="open"]') !== null,
      };
    });
  } catch {
    // A crashed/closed page must not replace the original test failure with a diagnostic error.
  }
  return { ...diagnostics, ...facts };
}

export async function canonicalWorkspace(
  page: Page,
  configure?: (wire: Record<string, unknown>) => void,
  interleave?: Interleave,
  suffix = "",
) {
  return canonicalWorkspaceMode(page, configure, interleave, suffix, false);
}

export async function canonicalV2Workspace(
  page: Page,
  configure?: (wire: Record<string, unknown>) => void,
  interleave?: Interleave,
  suffix = "",
) {
  return canonicalWorkspaceMode(
    page,
    configure,
    interleave,
    `&authoringV2=1&frames=120${suffix}`,
    true,
  );
}

async function canonicalWorkspaceMode(
  page: Page,
  configure: ((wire: Record<string, unknown>) => void) | undefined,
  interleave: Interleave | undefined,
  suffix: string,
  v2: boolean,
) {
  const started = performance.now();
  let diagnostics = diagnosticsByPage.get(page);
  if (!diagnostics) {
    diagnostics = { fixtureCalls: [], fixtureCallsOmitted: 0, pageErrors: 0 };
    diagnosticsByPage.set(page, diagnostics);
    page.on("pageerror", () => {
      diagnostics!.pageErrors += 1;
    });
  }
  const elapsed = () => Math.round((performance.now() - started) * 10) / 10;
  const root = fileURLToPath(new URL("../../../../", import.meta.url));
  const python = fixturePython(root);
  const transactions: unknown[] = [];
  let initial: unknown;
  let bootstrapped: (() => void) | undefined;
  const bootstrapComplete = new Promise<void>((resolve) => {
    bootstrapped = resolve;
  });
  await page.route("**/__nle_fixture/*", async (route) => {
    if (route.request().url().endsWith("/bootstrap")) {
      initial = route.request().postDataJSON();
      if (configure) {
        configure(initial as Record<string, unknown>);
        const wire = initial as Record<string, unknown>;
        if (v2) {
          wire.timeline_fingerprint = compositionContractFingerprint({
            operation_profile_id: wire.operation_profile_id,
            edit_capacity_frames: wire.edit_capacity_frames,
            content_end_exclusive: wire.content_end_exclusive,
            tracks: wire.tracks,
            clips: wire.clips,
            audio_extension: wire.audio_extension,
          });
          wire.workspace_fingerprint = compositionContractFingerprint({
            project_id: wire.project_id,
            workspace_handle: wire.workspace_handle,
            workspace_revision: wire.workspace_revision,
            timeline_revision: wire.timeline_revision,
            timeline_fingerprint: wire.timeline_fingerprint,
          });
          const material = { ...wire };
          delete material.authoring_fingerprint;
          wire.authoring_fingerprint = compositionContractFingerprint(material);
        } else wire.public_fingerprint = publicCompositionFingerprint(wire);
      }
    } else {
      const transaction = route.request().postDataJSON() as Record<
        string,
        unknown
      >;
      interleave?.(transaction, transactions);
      transactions.push(transaction);
    }
    const callStarted = performance.now();
    let outcome: "ok" | "timeout" | "error" = "ok";
    let raw: string;
    try {
      raw = execFileSync(
        python,
        [join(root, "scripts/m25_16_timeline_fixture.py")],
        {
          input: JSON.stringify({
            [v2 ? "authoring" : "snapshot"]: initial,
            transactions,
          }),
          encoding: "utf8",
          timeout: 15_000,
          maxBuffer: 2_097_152,
        },
      );
    } catch (error) {
      outcome =
        (error as { code?: string }).code === "ETIMEDOUT" ? "timeout" : "error";
      throw error;
    } finally {
      // Keep invocation facts bounded and payload-free; stderr/arguments contain fixture state.
      if (diagnostics.fixtureCalls.length < 64)
        diagnostics.fixtureCalls.push({
          durationMs: Math.round((performance.now() - callStarted) * 10) / 10,
          outcome,
        });
      else diagnostics.fixtureCallsOmitted += 1;
    }
    const result = JSON.parse(raw);
    await route.fulfill({ json: result });
    if (route.request().url().endsWith("/bootstrap")) {
      diagnostics.bootstrapCompletedMs = elapsed();
      bootstrapped?.();
    }
  });
  await page.goto(`/nleWorkspace.html?canonical=1${suffix}`);
  if (v2) {
    await bootstrapComplete;
    await expect
      .poll(async () => (await snapshot(page)).authoringStateV2?.schema)
      .toBe(NLE_AUTHORING_SCHEMA);
    diagnostics.schemaCompletedMs = elapsed();
  }
  await page.getByRole("button", { name: "Open full editor" }).click();
  await expect(page.locator(surface)).toBeVisible();
  return transactions;
}
