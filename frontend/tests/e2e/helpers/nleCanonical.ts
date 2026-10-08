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
    const result = JSON.parse(
      execFileSync(python, [join(root, "scripts/m25_16_timeline_fixture.py")], {
        input: JSON.stringify({
          [v2 ? "authoring" : "snapshot"]: initial,
          transactions,
        }),
        encoding: "utf8",
        timeout: 15_000,
        maxBuffer: 2_097_152,
      }),
    );
    await route.fulfill({ json: result });
    if (route.request().url().endsWith("/bootstrap")) bootstrapped?.();
  });
  await page.goto(`/nleWorkspace.html?canonical=1${suffix}`);
  if (v2) {
    await bootstrapComplete;
    await expect
      .poll(async () => (await snapshot(page)).authoringStateV2?.schema)
      .toBe(NLE_AUTHORING_SCHEMA);
  }
  await page.getByRole("button", { name: "Open full editor" }).click();
  await expect(page.locator(surface)).toBeVisible();
  return transactions;
}
