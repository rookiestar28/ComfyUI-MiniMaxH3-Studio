// M25-21 shared stress workloads. Plan section 14.5: one executed interaction supplies many
// assertions, so each workload here runs ONCE and records the stress dimension of every row it
// serves plus the frozen measurements assigned to it in the coverage manifest. These are the long
// cases; they run only in `playwright.hardening.config.ts`.
//
// Nothing in this file re-implements a runtime or a registry: every workload drives the real
// integrated shell (`frontend/e2e/nleShell.tsx`) through the same helpers the per-row cases use,
// and reads the harness's own instrumentation.
import {
  test,
  expect,
  type Locator,
  type Page,
  type Route,
} from "@playwright/test";

import {
  CONCURRENT_CLIP,
  CONFLICT_COPY,
  Counter,
  TIMELINE_STATUS,
  activate,
  closeTimelineToolbarOverflow,
  commandRecipes,
  control,
  inspector,
  openClipMenu,
  revealClip,
  revealTimelineToolbarAlternative,
} from "../helpers/nleCommandRecipes";
import {
  Checks,
  hardeningEvidence,
  p95,
} from "../helpers/nleHardeningEvidence";
import {
  startImportFixture,
  type ImportFixture,
} from "../helpers/nleImportFixture";
import { openExportPanel } from "../helpers/nleExport";
import { installOutputJobFixture } from "../helpers/nleOutputJobFixture";
import { installNativeAudioAllocationAudit } from "../helpers/nativeAudioAllocationAudit";
import {
  openIntegratedShell,
  shellSnapshot,
  shellSurface,
} from "../helpers/nleShell";
import {
  playheadFrame,
  playheadSlider,
  seekPlayhead,
} from "../helpers/nleTimeline";
import type { TimelineCommandWire } from "../../../src/contracts/authoringWorkbenchCodec";
import { measurePresentedConflict } from "../helpers/nlePresentationClock";

const { recipes } = commandRecipes(shellSnapshot);

/**
 * Give the stress fixture one free track without spending one history entry per clip.
 *
 * IMPORTANT: NLE-STRESS-V1 intentionally starts at the eight-track product limit. The canonical
 * `track.add` row still has to activate and measure the rendered control, so this setup removes
 * every clip on the smallest non-primary track and that track in one real core transaction. The
 * two resulting history entries (setup and add) are then independently undoable back to the exact
 * base fingerprint. Expanding this into per-clip UI edits overflows the 32-entry history and makes
 * restoration fail even though the product's track limit and add control are correct.
 */
async function makeStressTrackCapacity(page: Page): Promise<void> {
  const before = await shellSnapshot(page);
  const timeline = before.timelineSnapshot!;
  if (timeline.tracks.length < 8) return;
  const removable = timeline.tracks
    .filter((track) => track.kind !== "primary_video")
    .map((track) => ({
      track,
      clips: timeline.clips.filter((clip) => clip.trackId === track.trackId),
    }))
    .sort(
      (left, right) =>
        left.clips.length - right.clips.length ||
        right.track.order - left.track.order,
    )[0];
  if (removable === undefined)
    throw new Error("NLE-STRESS-V1 has no removable non-primary track");
  const commands: TimelineCommandWire[] = [
    ...removable.clips.map((clip) => ({
      kind: "remove_clip" as const,
      payload: { clip_id: clip.clipId },
    })),
    {
      kind: "remove_track",
      payload: { track_id: removable.track.trackId },
    },
  ];
  await page.evaluate(
    (setup) => window.nleShellHarness.applyTimelineCommands(setup),
    commands,
  );
  await expect
    .poll(async () => (await shellSnapshot(page)).authoringStatus)
    .toBe("ready");
  await expect
    .poll(async () => (await shellSnapshot(page)).receipts)
    .toBe(before.receipts + 1);
  await expect
    .poll(async () =>
      (await shellSnapshot(page)).timelineSnapshot!.tracks.some(
        (track) => track.trackId === removable.track.trackId,
      ),
    )
    .toBe(false);
}

const CLIP_CAPACITY_OPERATIONS = new Map<string, number>([
  ["asset.insert", 1],
  ["title.insert", 1],
  ["clip.split", 1],
  ["clip.merge", 1],
  ["range.insert", 1],
  ["range.overwrite", 1],
  ["boundary.roll", 1],
  ["clip.slide", 2],
  // IMPORTANT: transition setup creates its admitted adjacent boundary through a real split.
  // The 128-clip stress base otherwise disables that split before the transition is exercised.
  ["boundary.transition", 1],
]);

/** Free the requested stress-fixture clip slots in one accepted setup transaction. */
async function makeStressClipCapacity(
  page: Page,
  slots: number,
): Promise<void> {
  const before = await shellSnapshot(page);
  const timeline = before.timelineSnapshot!;
  const missing = Math.max(0, timeline.clips.length + slots - 128);
  if (missing === 0) return;
  const removable = [...timeline.clips]
    .filter((clip) => clip.clipId !== "clip-0")
    .sort((left, right) => right.clipId.localeCompare(left.clipId))
    .slice(0, missing);
  if (removable.length !== missing)
    throw new Error("NLE-STRESS-V1 has insufficient removable capacity clips");
  const commands: readonly TimelineCommandWire[] = removable.map((clip) => ({
    kind: "remove_clip",
    payload: { clip_id: clip.clipId },
  }));
  await page.evaluate(
    (setup) => window.nleShellHarness.applyTimelineCommands(setup),
    commands,
  );
  await expect
    .poll(async () => (await shellSnapshot(page)).authoringStatus)
    .toBe("ready");
  await expect
    .poll(async () => (await shellSnapshot(page)).receipts)
    .toBe(before.receipts + 1);
  await expect
    .poll(
      async () => (await shellSnapshot(page)).timelineSnapshot!.clips.length,
    )
    .toBe(timeline.clips.length - missing);
}

test.use({
  viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 1,
  hasTouch: true,
});

const OUTPUT_REGION = { name: "Final video", exact: true } as const;
const IMPORT_BUTTON = '[data-h3-nle-control="asset.import_production"]';
const OUTPUT_ROWS = [
  "action.output.render",
  "action.output.status",
  "action.output.cancel",
  "action.output.final_preview",
  "action.output.download",
] as const;

/** The number of render jobs the frozen `render.jobs` floor requires. */
const RENDER_JOBS = 8;

async function productionStage(page: Page) {
  const overlay = page.locator('[data-h3-nle-surface="overlay_v1"]');
  if ((await overlay.count()) === 0) {
    await page
      .getByRole("navigation", { name: "H3 Context pages" })
      .getByRole("button", { name: "Production" })
      .click();
    await page.getByRole("tab", { name: "Clip editor", exact: true }).click();
    await page.locator('[data-h3-nle-entry="open"]').click();
  }
  await expect(overlay).toHaveAttribute("data-h3-nle-state", "expanded");
  const pane = overlay.locator('[data-h3-nle-pane="assets"]');
  if ((await pane.getAttribute("aria-selected")) !== "true") await pane.click();
  await expect(page.locator(IMPORT_BUTTON)).toBeVisible();
}

test("output status workload keeps at most two status clients and one running job", async ({
  page,
}, testInfo) => {
  test.setTimeout(300_000);
  const started = Date.now();
  const fixture = await installOutputJobFixture(page);
  await openIntegratedShell(page, "smoke", {
    render: true,
    extraParams: { outputJob: "1" },
  });
  await openExportPanel(page);
  const region = page.getByRole("region", OUTPUT_REGION);
  const phase = region.getByRole("status").first();
  const render = region.getByRole("button", { name: "Render final video" });
  await expect(render).toBeEnabled();
  const checks = new Checks();
  let runningMax = 0;
  let queuedMax = 0;
  const sample = () => {
    runningMax = Math.max(runningMax, fixture.running());
    queuedMax = Math.max(queuedMax, fixture.queued());
  };
  await checks.step("every_job_is_explicit_and_serial", async () => {
    for (let job = 0; job < RENDER_JOBS; job += 1) {
      await render.click();
      await expect.poll(() => fixture.counts.create).toBe(job + 1);
      sample();
      await expect(phase).toHaveText("Queued");
      fixture.advance("rendering");
      await expect(phase).toHaveText("Rendering", { timeout: 10_000 });
      sample();
      // A job in flight offers no second render: the floor is reached by explicit repeats.
      await expect(render).toBeDisabled();
      fixture.advance("succeeded");
      await expect(phase).toHaveText("Video ready", { timeout: 10_000 });
      sample();
      await expect(render).toBeEnabled();
    }
    expect(fixture.jobs()).toHaveLength(RENDER_JOBS);
    expect(new Set(fixture.jobs()).size).toBe(RENDER_JOBS);
  });
  await checks.step("status_reads_never_fan_out", async () => {
    // A succeeded job polls every five seconds; hold the replies across more than one interval
    // and the client must still keep a single read in flight.
    const release = fixture.hold();
    const before = fixture.counts.status;
    await page.waitForTimeout(6_000);
    expect(fixture.counts.status - before).toBeLessThanOrEqual(1);
    release();
    sample();
    expect(fixture.peakStatusClients()).toBeLessThanOrEqual(2);
  });
  await checks.step("the_workload_started_nothing_else", async () => {
    const snapshot = await shellSnapshot(page);
    expect(snapshot.queuedPrompts).toBe(0);
    expect(fixture.counts.cancel).toBe(0);
  });
  const interval = Date.now() - started;
  const evidence = hardeningEvidence(testInfo);
  const measurement = (
    id: string,
    observed: number,
    method: string,
    samples: number,
  ) =>
    evidence.measurement(id, {
      observed,
      unit: "count",
      method,
      sample_count: samples,
      interval_ms: interval,
    });
  await measurement(
    "render.jobs",
    fixture.counts.create,
    "explicit Render activations accepted by the output route, counted at the route",
    RENDER_JOBS,
  );
  await measurement(
    "render.running_max",
    runningMax,
    "jobs in a non-terminal phase, sampled at every phase transition",
    RENDER_JOBS * 3,
  );
  await measurement(
    "render.queued_max",
    queuedMax,
    "jobs in the queued phase, sampled at every phase transition",
    RENDER_JOBS * 3,
  );
  await measurement(
    "render.status_clients_max",
    fixture.peakStatusClients(),
    "peak concurrent status reads in flight at the route, including a held-reply window",
    fixture.counts.status,
  );
  const facts = {
    jobs: fixture.counts.create,
    running_max: runningMax,
    queued_max: queuedMax,
    status_clients_max: fixture.peakStatusClients(),
    status_reads: fixture.counts.status,
  };
  await evidence.workload("render", checks, {
    ...facts,
    fixture: "NLE-STRESS-RENDER-V1",
    // Owed: the backend RSS increment, which needs the render companion process. It is not
    // attached here, so the report reads it as NOT_RUN rather than as a pass.
    backend_rss: "not_measured_in_this_workload",
  });
  for (const row of OUTPUT_ROWS)
    await evidence.row(row, "stress", checks, facts);
});

test("import workload stays inside the selection, byte and cleanup bounds", async ({
  page,
}, testInfo) => {
  test.setTimeout(300_000);
  let fixture: ImportFixture | undefined;
  try {
    fixture = await startImportFixture(page);
    const imports = fixture;
    await page.goto("/nleShell.html?import=1&target=ready&segments=2");
    await productionStage(page);
    await expect(page.locator(IMPORT_BUTTON)).toBeEnabled();
    const checks = new Checks();
    const before = await shellSnapshot(page);
    const cycles = 12;
    let first: Awaited<ReturnType<typeof shellSnapshot>> | undefined;
    await checks.step("repeated_imports_stay_idempotent", async () => {
      for (let cycle = 0; cycle < cycles; cycle += 1) {
        if (cycle > 0) await productionStage(page);
        await page.locator(IMPORT_BUTTON).click();
        await expect.poll(() => imports.importRequests.length).toBe(cycle + 1);
        await expect
          .poll(async () => (await shellSnapshot(page)).importStatus)
          .toBe("succeeded");
        // IMPORTANT: receipt IDs and the V2 catalog survive asset-only history and highlight
        // expiry. An empty render snapshot is not evidence that the import was lost.
        if (cycle === 0) {
          await expect
            .poll(async () => {
              const current = await shellSnapshot(page);
              const ids = new Set(
                current.authoringStateV2?.assets.map((asset) => asset.assetId),
              );
              return (
                imports.importedAssetIds().length === 2 &&
                imports.importedAssetIds().every((id) => ids.has(id))
              );
            })
            .toBe(true);
          first = await shellSnapshot(page);
        }
      }
      expect(new Set(imports.importRequests).size).toBe(cycles);
    });
    const after = await shellSnapshot(page);
    await checks.step("the_selection_bound_held", () => {
      // The stage offers two ready outputs. The first import takes exactly those; every repeat
      // is idempotent, so neither the imported set nor the library grows again.
      const importedIds = imports.importedAssetIds();
      expect(importedIds).toHaveLength(2);
      expect(first!.authoringStateV2!.assets.length).toBe(
        (before.authoringStateV2?.assets.length ?? 0) + 2,
      );
      expect(after.authoringStateV2!.assets.length).toBe(
        first!.authoringStateV2!.assets.length,
      );
      for (const id of importedIds)
        expect(
          after.authoringStateV2!.assets.filter(
            (asset) => asset.assetId === id,
          ),
        ).toHaveLength(1);
    });
    await checks.step("no_repeat_touched_the_timeline", () => {
      expect(imports.transactions).toHaveLength(0);
      expect(after.authoringStateV2!.timelineRevision).toBe(
        before.authoringStateV2!.timelineRevision,
      );
      expect(after.authoringStateV2!.clips).toEqual([]);
      expect(after.timelineSnapshot).toBeNull();
      expect(after.queuedPrompts).toBe(0);
      expect(after.renderJobRequests).toBe(0);
    });
    await checks.step("the_authoring_target_was_never_recreated", () => {
      expect(after.authoringWorkspaceHandle).toBe(
        before.authoringWorkspaceHandle,
      );
      expect(
        imports.authoringActions.filter(
          (action) => action === "create_authoring_workspace",
        ),
      ).toHaveLength(0);
    });
    await checks.step("every_owner_was_released_at_the_end", async () => {
      await expect(page.locator("[data-h3-nle-root]")).toHaveCount(1);
      await page.locator('[data-h3-nle-action="close"]').click();
      await expect(page.locator("[data-h3-nle-root]")).toHaveCount(0);
      await expect
        .poll(async () => (await shellSnapshot(page)).mediaOwnership.live)
        .toBe(0);
      const owners = (await shellSnapshot(page)).mediaOwnership;
      expect(owners.released).toBe(owners.acquired);
    });
    const facts = {
      cycles,
      import_requests: imports.importRequests.length,
      distinct_request_ids: new Set(imports.importRequests).size,
      transactions: imports.transactions.length,
      assets: after.authoringStateV2!.assets.length,
    };
    const evidence = hardeningEvidence(testInfo);
    await evidence.row("import.production_outputs", "stress", checks, facts);
    await evidence.row(
      "import.production_outputs.refusal",
      "stress",
      checks,
      facts,
    );
  } finally {
    await fixture?.close();
  }
});

const NAVIGATION = { name: "H3 Context pages" } as const;
const OVERLAY = '[data-h3-nle-surface="overlay_v1"]';
const ROOT = "[data-h3-nle-root]";
const LAUNCHER = '[data-h3-nle-entry="open"]';
/** The focus key the shell restores to on close, as the accepted close-reason matrix asserts it. */
const LAUNCHER_FOCUS = '[data-h3-focus-key="nle-open-overlay"]';
const CLOSE = '[data-h3-nle-action="close"]';
/** M25-44: the launcher's unavailable region; it carries the surface status alone. */
const UNAVAILABLE = '[data-h3-nle-unavailable="overlay_v1"]';
const UI_ROWS = [
  "ui.global_shell_identity",
  "ui.function_switch",
  "ui.overlay_open",
  // The manifest's invariant is `overlay_close_return_focus`; a row recorded as `ui.overlay_close`
  // attaches an observation nothing consumes and leaves the real row unobserved.
  "ui.overlay_close_return_focus",
  "ui.duplicate_open",
  "ui.overlay_unavailable_status",
  "ui.view_destroy_cleanup",
] as const;

/** A genuinely unsupported browser media runtime, and its undo, through the same real DOM API. */
async function degradeMediaRuntime(page: Page, degraded: boolean) {
  await page.evaluate((off) => {
    const scope = window as unknown as { __h3CanPlayType?: unknown };
    scope.__h3CanPlayType ??= HTMLVideoElement.prototype.canPlayType;
    Object.defineProperty(HTMLVideoElement.prototype, "canPlayType", {
      configurable: true,
      writable: true,
      value: off ? () => "" : scope.__h3CanPlayType,
    });
  }, degraded);
}

test("UI-CONTRACT-STRESS-V1 shell workload keeps every invariant with zero navigation effects", async ({
  page,
}, testInfo) => {
  // A harness limit, not a budget: the frozen floors below (100 switches, 25 overlay cycles, 10
  // each of duplicate opens, fallbacks, navigation closures and destroy cycles) are unchanged,
  // and each full open waits for a decoded, usable editor. The first run measured 14.7 min and
  // was still inside the workload when the previous 900 s limit fired.
  test.setTimeout(2_400_000);
  const started = Date.now();
  const oracle = await openIntegratedShell(page, "smoke", {
    expandOverlay: false,
    extraParams: { viewDestroy: "1" },
  });
  const nav = page.getByRole("navigation", NAVIGATION);
  const clipTab = page.getByRole("tab", { name: "Clip editor", exact: true });
  const productionTab = page.getByRole("tab", {
    name: "Production",
    exact: true,
  });
  const readEffects = async () => {
    const snapshot = await shellSnapshot(page);
    return {
      transactions: oracle.transactions.length,
      renderJobs: snapshot.renderJobRequests,
      queued: snapshot.queuedPrompts,
      fetches: snapshot.fetchPaths.length,
    };
  };
  const counts = {
    functionSwitches: 0,
    overlayCycles: 0,
    duplicateOpens: 0,
    fallbacks: 0,
    navigationClosures: 0,
    viewDestroyCycles: 0,
  };
  let hiddenViolations = 0;
  let hiddenSamples = 0;
  let functionRetained = 0;
  const hiddenPaths: string[] = [];
  /**
   * Nothing plays and nothing the editor owns polls while the editor is not on screen.
   *
   * GUARD: the subject is the editor's own work, not every request the page makes. Navigating to
   * another page and re-rendering a destroyed view legitimately fetch that page's own data, and
   * counting any new path at all reported twenty violations for ten navigations and ten destroys
   * while the editor was in fact silent. The paths that appear are recorded either way, so a
   * surprising one is visible in the evidence instead of being swallowed by the filter.
   */
  const EDITOR_OWNED = ["/h3-context/v1/authoring/", "nle-media"];
  const sampleHidden = async (baseline: number) => {
    hiddenSamples += 1;
    await page.waitForTimeout(120);
    const playing = await page.evaluate(
      () =>
        [...document.querySelectorAll<HTMLMediaElement>("video,audio")].filter(
          (element) => !element.paused,
        ).length,
    );
    const snapshot = await shellSnapshot(page);
    const appeared = snapshot.fetchPaths.slice(baseline);
    hiddenPaths.push(...appeared);
    const polled = appeared.filter((path) =>
      EDITOR_OWNED.some((owned) => path.includes(owned)),
    );
    if (playing > 0 || polled.length > 0) hiddenViolations += 1;
  };

  await nav.getByRole("button", { name: "Production" }).click();
  await expect(page.locator("[data-h3-director-function-tabs]")).toBeVisible();
  const before = await readEffects();
  const checks = new Checks();
  // Where this workload's wall time actually goes, recorded as facts. The floors are unchanged;
  // this exists because the first execution spent nine minutes inside one retrying click and the
  // report could not show that from the outside.
  const stepMs: Record<string, number> = {};
  const timed = async (name: string, body: () => Promise<void>) => {
    const began = Date.now();
    await checks.step(name, body);
    stepMs[name] = Date.now() - began;
  };

  await timed("a_hundred_function_switches_keep_identity", async () => {
    for (let switched = 0; switched < 100; switched += 1) {
      const target = switched % 2 === 0 ? clipTab : productionTab;
      await target.click();
      await expect(target).toHaveAttribute("aria-selected", "true");
      counts.functionSwitches += 1;
    }
    await clipTab.click();
    await expect(page.locator(LAUNCHER)).toBeVisible();
  });

  await timed("overlay_cycles_open_and_release_cleanly", async () => {
    for (let cycle = 0; cycle < 25; cycle += 1) {
      await page.locator(LAUNCHER).click();
      await expect(page.locator(OVERLAY)).toHaveAttribute(
        "data-h3-nle-state",
        "expanded",
      );
      await expect(
        page.getByRole("button", { name: "Play", exact: true }),
      ).toBeEnabled();
      if (cycle % 2 === 0) {
        // Dispatch at the actual open origin. A forced click at its covered coordinates
        // can hit a thumbnail Add, which is an edit rather than a duplicate open.
        await page.locator(LAUNCHER).dispatchEvent("click");
        await expect(page.locator(ROOT)).toHaveCount(1);
        counts.duplicateOpens += 1;
      }
      const baseline = (await shellSnapshot(page)).fetchPaths.length;
      await page.locator(CLOSE).click();
      await expect(page.locator(ROOT)).toHaveCount(0);
      // `overlay_close_return_focus` is the invariant this loop feeds, so it is asserted on every
      // cycle rather than assumed: a close that leaves focus on the detached dialog strands a
      // keyboard user on nothing, and the count of clean closes alone would never show it.
      await expect(page.locator(LAUNCHER_FOCUS)).toBeFocused();
      counts.overlayCycles += 1;
      await expect
        .poll(async () => (await shellSnapshot(page)).mediaOwnership.live)
        .toBe(0);
      await sampleHidden(baseline);
    }
  });

  await checks.step("duplicate_opens_reached_their_floor", () => {
    // The floor is the manifest's (`ui.duplicate_opens` >= 10). A workload that asserts a lower
    // private floor passes while the measurement it feeds fails the join, which is exactly how
    // this drifted to five.
    expect(counts.duplicateOpens).toBeGreaterThanOrEqual(10);
  });

  await timed("refused_opens_fall_back_and_acquire_nothing", async () => {
    for (let refusal = 0; refusal < 10; refusal += 1) {
      await degradeMediaRuntime(page, true);
      // IMPORTANT (M25-63 B-M2563-06): the previous open's decorations can still be fetching
      // after its close, and each one counts as acquired when its bytes land, inside this
      // refusal. Settle them first, then require that the refused open starts no acquisition
      // at all: an unmoved attempt count also catches one that would land after the check.
      await expect
        .poll(
          async () =>
            (await shellSnapshot(page)).mediaOwnership.acquisitionsInFlight,
        )
        .toBe(0);
      const owners = (await shellSnapshot(page)).mediaOwnership;
      await page.locator(LAUNCHER).click();
      await expect(page.locator(UNAVAILABLE)).toBeVisible();
      await expect(page.locator(ROOT)).toHaveCount(0);
      const refused = (await shellSnapshot(page)).mediaOwnership;
      expect(refused.acquisitionAttempts).toBe(owners.acquisitionAttempts);
      expect(refused.acquired).toBe(owners.acquired);
      counts.fallbacks += 1;
      await degradeMediaRuntime(page, false);
      await page.locator(LAUNCHER).click();
      await expect(page.locator(ROOT)).toHaveCount(1);
      await page.locator(CLOSE).click();
      await expect(page.locator(ROOT)).toHaveCount(0);
    }
  });

  await timed("navigation_closes_the_editor", async () => {
    for (let closure = 0; closure < 10; closure += 1) {
      await page.locator(LAUNCHER).click();
      await expect(page.locator(ROOT)).toHaveCount(1);
      const baseline = (await shellSnapshot(page)).fetchPaths.length;
      // GUARD: activate the page from the keyboard, never with a pointer click. The editor is a
      // modal dialog, so while it is open its own backdrop covers the page navigation behind it
      // and a real pointer click on that button can never land -- Playwright retries it until the
      // whole test times out. This is the same activation the accepted close-reason matrix uses
      // for `top_level_navigation`, and it is what a user actually has while the dialog is up.
      await nav.getByRole("button", { name: "Settings" }).focus();
      await page.keyboard.press("Enter");
      await expect(page.locator(ROOT)).toHaveCount(0);
      counts.navigationClosures += 1;
      await sampleHidden(baseline);
      await nav.getByRole("button", { name: "Production" }).click();
      // Whether the Clip editor function is still the active one after a top-level navigation is
      // `ui.global_shell_identity`'s subject, so it is counted here rather than assumed: the loop
      // re-activates it only when the shell did not.
      if ((await clipTab.getAttribute("aria-selected")) === "true")
        functionRetained += 1;
      else await clipTab.click();
      await expect(page.locator(LAUNCHER)).toBeVisible();
    }
  });

  await timed("view_destroy_cycles_release_and_restore", async () => {
    for (let cycle = 0; cycle < 10; cycle += 1) {
      const baseline = (await shellSnapshot(page)).fetchPaths.length;
      await page.evaluate(() => window.nleShellHarness.destroyView());
      await expect(nav).toHaveCount(0);
      await sampleHidden(baseline);
      await page.evaluate(() => window.nleShellHarness.renderView());
      await expect(nav).toHaveCount(1);
      await expect(clipTab).toHaveAttribute("aria-selected", "true");
      await expect(page.locator(ROOT)).toHaveCount(0);
      counts.viewDestroyCycles += 1;
    }
  });

  const after = await readEffects();
  await checks.step("the_whole_workload_started_no_work", () => {
    expect({
      transactions: after.transactions,
      renderJobs: after.renderJobs,
      queued: after.queued,
    }).toEqual({
      transactions: before.transactions,
      renderJobs: before.renderJobs,
      queued: before.queued,
    });
  });
  await checks.step("nothing_played_or_polled_while_hidden", () => {
    expect(hiddenViolations).toBe(0);
    expect(hiddenSamples).toBeGreaterThanOrEqual(45);
  });

  const interval = Date.now() - started;
  const evidence = hardeningEvidence(testInfo);
  const measure = (id: string, observed: number, method: string, n: number) =>
    evidence.measurement(id, {
      observed,
      unit: "count",
      method,
      sample_count: n,
      interval_ms: interval,
    });
  await measure(
    "ui.function_switches",
    counts.functionSwitches,
    "function tab activations confirmed by aria-selected on the activated tab",
    counts.functionSwitches,
  );
  await measure(
    "ui.overlay_cycles",
    counts.overlayCycles,
    "explicit launcher opens each followed by an explicit close to zero live owners",
    counts.overlayCycles,
  );
  await measure(
    "ui.duplicate_opens",
    counts.duplicateOpens,
    "a second open issued while the dialog is expanded, counted at one root",
    counts.duplicateOpens,
  );
  await measure(
    "ui.fallbacks",
    counts.fallbacks,
    "opens refused by an unsupported media runtime, counted at the compact fallback",
    counts.fallbacks,
  );
  await measure(
    "ui.navigation_closures",
    counts.navigationClosures,
    "top-level page navigations while the editor is open, counted at zero roots",
    counts.navigationClosures,
  );
  await measure(
    "ui.view_destroy_cycles",
    counts.viewDestroyCycles,
    "native view destroy followed by the host's own render of the same tab",
    counts.viewDestroyCycles,
  );
  await measure(
    "ui.navigation_effects",
    after.transactions -
      before.transactions +
      (after.renderJobs - before.renderJobs) +
      (after.queued - before.queued),
    "timeline transactions, render jobs and queued prompts attributable to the workload",
    3,
  );
  await measure(
    "ui.hidden_playback_or_polling",
    hiddenViolations,
    "samples after every close, navigation closure and view destroy with a playing media element or a new fetch",
    hiddenSamples,
  );
  const facts = {
    ...counts,
    hidden_samples: hiddenSamples,
    function_retained_across_navigation: functionRetained,
    step_ms: stepMs,
    // Every path that appeared during a hidden sample, deduplicated: the filter above decides
    // which of them count, and this is what it decided about.
    hidden_sample_paths: [...new Set(hiddenPaths)],
  };
  await evidence.workload("ui_stress", checks, {
    ...facts,
    fixture: "UI-CONTRACT-STRESS-V1",
  });
  for (const row of UI_ROWS) await evidence.row(row, "stress", checks, facts);
});

/** The frozen per-edit commit ceiling the manifest measures; mirrored here only to attribute
 * a breach to its phase. The manifest, not this constant, is what the report compares against. */
const COMMIT_CEILING = 4;

const MONITOR_STATUS = '[data-h3-nle-status="monitor"]';

/**
 * Select the Production page's Clip editor function, which is where the launcher lives.
 *
 * GUARD: `H3Sidebar` renders `[data-h3-nle-entry="open"]` only while the Clip editor function is
 * the selected one -- the Production workbench panel has no launcher at all. A workload that
 * opened the editor through the harness's own shortcut never changed that selection, so after a
 * view destroy and re-render there is no launcher to click and the click waits out the whole test
 * budget. Any workload that reopens the editor by its real affordance must come through here.
 */
async function toClipEditor(page: Page) {
  await page
    .getByRole("navigation", NAVIGATION)
    .getByRole("button", { name: "Production" })
    .click();
  await page.getByRole("tab", { name: "Clip editor", exact: true }).click();
  await expect(page.locator('[data-h3-nle-entry="open"]')).toBeVisible();
}
const SEAM_ROWS = [
  "native_media",
  "compositor",
  "follower",
  "client_response",
  "lifecycle",
  "backend_restart",
  "worker",
  "webgl",
] as const;

type ApiUse = {
  worker: number;
  webgl: number;
  audioContext: number;
  standaloneAudio: number;
};
type LongTasks = { total: number; max: number; count: number };

/** Counts bounded runtime APIs from before the first script. */
async function countRuntimeApis(page: Page) {
  await page.addInitScript(installNativeAudioAllocationAudit);
  await page.addInitScript(() => {
    const scope = window as unknown as {
      __h3ApiUse: ApiUse;
      __h3LongTasks: LongTasks;
      __h3ResetLongTasks: () => void;
    };
    scope.__h3ApiUse = {
      worker: 0,
      webgl: 0,
      audioContext: 0,
      standaloneAudio: 0,
    };
    scope.__h3LongTasks = { total: 0, max: 0, count: 0 };
    scope.__h3ResetLongTasks = () => {
      scope.__h3LongTasks = { total: 0, max: 0, count: 0 };
    };
    try {
      new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          scope.__h3LongTasks.total += entry.duration;
          scope.__h3LongTasks.max = Math.max(
            scope.__h3LongTasks.max,
            entry.duration,
          );
          scope.__h3LongTasks.count += 1;
        }
      }).observe({ type: "longtask", buffered: true });
    } catch {
      // Fail closed at the reader: a browser without the entry type must not read as zero.
      delete (scope as Partial<typeof scope>).__h3LongTasks;
    }
    const win = window as unknown as Record<string, unknown>;
    for (const [name, key] of [
      ["Worker", "worker"],
      ["SharedWorker", "worker"],
      ["AudioContext", "audioContext"],
      ["webkitAudioContext", "audioContext"],
    ] as const) {
      const original = win[name];
      if (typeof original !== "function") continue;
      win[name] = new Proxy(original, {
        construct(target, args, newTarget) {
          scope.__h3ApiUse[key] += 1;
          return Reflect.construct(
            target as new (...values: unknown[]) => object,
            args,
            newTarget as new (...values: unknown[]) => object,
          );
        },
      });
    }
    const getContext = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function (
      this: HTMLCanvasElement,
      ...args: unknown[]
    ) {
      if (args[0] !== "2d") scope.__h3ApiUse.webgl += 1;
      return (getContext as (...values: unknown[]) => unknown).apply(
        this,
        args,
      ) as ReturnType<typeof getContext>;
    } as typeof HTMLCanvasElement.prototype.getContext;
  });
}

const apiUse = (page: Page): Promise<ApiUse> =>
  page.evaluate(() => {
    const audit = window.__h3NativeAudioAudit;
    if (audit === undefined)
      throw new Error("native_audio_allocation_audit_unavailable");
    return {
      ...(window as unknown as { __h3ApiUse: ApiUse }).__h3ApiUse,
      standaloneAudio: audit.snapshot().standaloneAudioElements,
    };
  });

/** Long tasks observed since the last reset; throws when the entry type is unavailable. */
async function longTasks(page: Page): Promise<LongTasks> {
  const observed = await page.evaluate(
    () =>
      (window as unknown as { __h3LongTasks?: LongTasks }).__h3LongTasks ??
      null,
  );
  if (observed === null)
    throw new Error("longtask_instrumentation_unavailable");
  return observed;
}

const resetLongTasks = (page: Page) =>
  page.evaluate(() =>
    (
      window as unknown as { __h3ResetLongTasks: () => void }
    ).__h3ResetLongTasks(),
  );

async function loseCanvasContext(page: Page, lost: boolean) {
  await page.evaluate((isLost) => {
    const canvas = document.querySelector<HTMLCanvasElement>(
      "[data-h3-nle-root] .h3-nle-monitor canvas",
    );
    if (canvas === null) throw new Error("no monitor canvas");
    canvas.dispatchEvent(
      new Event(isLost ? "contextlost" : "contextrestored", {
        cancelable: true,
      }),
    );
  }, lost);
}

async function setDocumentHidden(page: Page, hidden: boolean) {
  await page.evaluate((value) => {
    for (const [property, current] of [
      ["hidden", value],
      ["visibilityState", value ? "hidden" : "visible"],
    ] as const)
      Object.defineProperty(document, property, {
        configurable: true,
        get: () => current,
      });
    document.dispatchEvent(new Event("visibilitychange"));
  }, hidden);
}

const audioState = (page: Page) =>
  page
    .locator('[data-h3-nle-status="audio"]')
    .getAttribute("data-h3-nle-audio-state");

/**
 * The follower's announced state together with the reason it announces.
 *
 * The reason is the difference between "the product refused" and "this workload asked the wrong
 * question": `no_primary` and `no_embedded_audio` are both correct announcements for a playhead
 * the harness put in the wrong place, and the state alone cannot tell them apart.
 */
const audioAnnouncement = (page: Page) =>
  page.locator('[data-h3-nle-status="audio"]').evaluate((element) => ({
    state: element.getAttribute("data-h3-nle-audio-state"),
    reason: element.getAttribute("data-h3-nle-audio-reason"),
  }));

/**
 * The frames at which the primary video track actually carries a clip, read from the live
 * snapshot rather than recomputed from the fixture's own arithmetic.
 *
 * GUARD: the embedded-audio follower follows the primary video, so it engages only while the
 * playhead is over one of these clips. Neither fixture is dense -- the stress timeline is primary
 * video for about a twentieth of its length, the smoke timeline for about a quarter -- so a
 * transport left wherever the previous step happened to stop it is far more likely to sit in a
 * gap, where `silent` is the correct state and an assertion of `following` fails a correct
 * product. Place the playhead before expecting the follower to have anything to follow.
 */
async function primaryVideoStarts(page: Page): Promise<number[]> {
  const snapshot = (await shellSnapshot(page)).timelineSnapshot!;
  const primary = snapshot.tracks
    .filter((track) => track.kind === "primary_video")
    .map((track) => track.trackId);
  const starts = snapshot.clips
    .filter((clip) => primary.includes(clip.trackId) && clip.enabled)
    .map((clip) => clip.startFrame)
    .sort((left, right) => left - right);
  // Fail closed: with no primary video there is nothing to synchronize, and the floors that
  // depend on it would be unreachable for a reason the report must see rather than infer.
  expect(starts.length).toBeGreaterThan(0);
  return starts;
}

test("NLE-STRESS-V1 injected seam losses recover the accepted revision or fail closed", async ({
  page,
}, testInfo) => {
  test.setTimeout(1_200_000);
  const started = Date.now();
  await countRuntimeApis(page);
  // GUARD: choose the injection by transaction index, never by a flag raised around the click.
  // The oracle records a transaction, then applies it -- a round trip to the persistent core --
  // and only then asks this hook what to do with the reply. A flag lowered as soon as the
  // transaction count moves is therefore routinely already lowered when the decision is taken:
  // the reply is fulfilled, no outcome is ever unknown, and the row measures nothing while
  // appearing to inject ten losses. The accepted `nleShellRecovery` journey picks by index for
  // this reason.
  let dropAt = -1;
  const oracle = await openIntegratedShell(page, "smoke", {
    service: true,
    extraParams: { viewDestroy: "1" },
    reply: (index) => (index === dropAt ? "abort" : "fulfill"),
    // The lifecycle seam releases the view and re-renders it, and only the Clip editor function
    // carries the launcher that reopens the editor. Retention restores a page and function the
    // user actually activated -- that is the accepted contract, not an oversight -- so this
    // workload activates them, exactly as the remount workload does.
    beforeOpen: toClipEditor,
  });
  try {
    const status = page.locator(MONITOR_STATUS);
    const recover = page.locator('[data-h3-nle-control="transport.recover"]');
    const play = page.locator('[data-h3-nle-control="transport.play"]');
    const evidence = hardeningEvidence(testInfo);
    const injections: Record<string, number> = {};
    const cancellations: number[] = [];
    const checks: Record<string, Checks> = {};
    const row = (seam: string) => (checks[seam] ??= new Checks());
    /**
     * Wait for an injected loss to stop costing owners, and record how long that took.
     *
     * GUARD: the comparison is against the owner count this injection started from, never a
     * fixed ceiling. A blocked-but-open session legitimately keeps the owners it will recover
     * with -- the smoke composition holds four -- so asserting a fixed small number here failed a
     * correct product. What a loss must never do is leave *more* behind than it found.
     */
    const settle = async (began: number, baseline: number) => {
      await expect
        .poll(async () => (await shellSnapshot(page)).mediaOwnership.live)
        .toBeLessThanOrEqual(baseline);
      cancellations.push(Date.now() - began);
    };

    // seam.compositor -- a real canvas context loss and its restoration.
    await row("compositor").step("each_loss_blocks_and_recovers", async () => {
      for (let injection = 0; injection < 10; injection += 1) {
        const held = (await shellSnapshot(page)).mediaOwnership.live;
        const began = Date.now();
        await loseCanvasContext(page, true);
        await expect(status).toHaveText(
          "Monitor unavailable: canvas unavailable.",
        );
        await settle(began, held);
        await expect(recover).toBeEnabled();
        await loseCanvasContext(page, false);
        await recover.click();
        await expect(status).toHaveText("Monitor paused.", { timeout: 15_000 });
        injections.compositor = (injections.compositor ?? 0) + 1;
      }
    });

    // seam.follower -- the document hides, the follower suspends, an explicit recovery restores.
    await row("follower").step("each_suspension_is_explicit", async () => {
      const landings = await primaryVideoStarts(page);
      const slider = playheadSlider(page);
      for (let injection = 0; injection < 10; injection += 1) {
        // Put the playhead on a primary-video clip first; the follower follows that track, and
        // the preceding seams leave the transport wherever their recovery stopped it.
        const landing = landings[injection % landings.length]! + 4;
        await seekPlayhead(page, slider, landing);
        await expect(play).toBeEnabled();
        await play.click();
        await expect
          .poll(() => audioAnnouncement(page))
          .toMatchObject({ state: "following" });
        const held = (await shellSnapshot(page)).mediaOwnership.live;
        const began = Date.now();
        await setDocumentHidden(page, true);
        await expect.poll(() => audioState(page)).toBe("suspended");
        expect(
          await page.evaluate(
            () =>
              [
                ...document.querySelectorAll<HTMLMediaElement>("video,audio"),
              ].filter((element) => !element.paused).length,
          ),
        ).toBe(0);
        await settle(began, held);
        await setDocumentHidden(page, false);
        await expect(recover).toBeEnabled();
        await recover.click();
        await expect(status).toHaveText("Monitor paused.", { timeout: 15_000 });
        injections.follower = (injections.follower ?? 0) + 1;
      }
    });

    // seam.native_media -- the source itself stops answering, then answers again.
    // GUARD: install and remove this handler *by identity*. The integrated shell serves the
    // fixture's media from its own handler on this very pattern, and `unroute(pattern)` with no
    // handler removes every handler registered for it -- including the shell's. The source then
    // never answers again, and the recovery row below fails for the rest of the run against a
    // product that is doing exactly the right thing.
    const deadSource = (route: Route) => route.abort();
    await page.route("**/nle-media/*", deadSource);
    await row("native_media").step("a_dead_source_fails_closed", async () => {
      // The canvas loss is the lever that arms recovery for the first attempt only: once the
      // session is blocked on the dead source it stays recoverable, so every later injection is
      // the reopen itself. The assertion names the source state deliberately -- asserting any
      // "Monitor unavailable:" text would be satisfied by the canvas message that is already on
      // screen before the reopen is even attempted, and would pass without a source failure.
      await loseCanvasContext(page, true);
      await expect(status).toHaveText(
        "Monitor unavailable: canvas unavailable.",
      );
      await loseCanvasContext(page, false);
      for (let injection = 0; injection < 10; injection += 1) {
        const held = (await shellSnapshot(page)).mediaOwnership.live;
        const began = Date.now();
        await expect(recover).toBeEnabled();
        await recover.click();
        await expect(status).toHaveText(
          "Monitor unavailable: source unavailable.",
          {
            timeout: 20_000,
          },
        );
        await settle(began, held);
        injections.native_media = (injections.native_media ?? 0) + 1;
      }
    });
    await page.unroute("**/nle-media/*", deadSource);
    await row("native_media").step("a_live_source_recovers", async () => {
      await recover.click();
      await expect(status).toHaveText("Monitor paused.", { timeout: 30_000 });
    });

    // seam.client_response -- the core accepts a command and the reply is lost.
    const clipButton = page.locator(
      '[data-h3-nle-control="selection.set"]:not([hidden])',
    );
    await row("client_response").step(
      "every_lost_reply_reconciles_read_only",
      async () => {
        for (let injection = 0; injection < 10; injection += 1) {
          const sent = oracle.transactions.length;
          const reads = oracle.reads();
          dropAt = sent;
          await clipButton.nth(injection % 2).click();
          await expect.poll(() => oracle.transactions.length).toBe(sent + 1);
          // GUARD: wait for the reconciling read itself, not for the surface to be `ready`. The
          // oracle has the transaction before the client has seen its reply go missing, so the
          // surface is still `ready` from before the injection and a status poll passes on its
          // first sample -- it would then read the counter before the reconciliation it is
          // supposed to be observing. Polling for exactly one read keeps the "read-only
          // reconciliation" claim: the poll fails just as loudly if a second read appears.
          await expect.poll(() => oracle.reads()).toBe(reads + 1);
          await expect
            .poll(async () => (await shellSnapshot(page)).authoringStatus)
            .toBe("ready");
          expect(oracle.reads()).toBe(reads + 1);
          expect(oracle.transactions).toHaveLength(sent + 1);
          injections.client_response = (injections.client_response ?? 0) + 1;
        }
      },
    );

    // seam.lifecycle -- the view is released while a transaction is in flight.
    await row("lifecycle").step("a_released_view_replays_nothing", async () => {
      for (let injection = 0; injection < 10; injection += 1) {
        const sent = oracle.transactions.length;
        dropAt = sent;
        await clipButton.nth(injection % 2).click();
        await expect.poll(() => oracle.transactions.length).toBe(sent + 1);
        const began = Date.now();
        await page.evaluate(() => window.nleShellHarness.destroyView());
        await expect(page.locator("[data-h3-nle-root]")).toHaveCount(0);
        // The view itself is gone here, so this is the one seam whose owners must reach zero.
        await settle(began, 0);
        await page.evaluate(() => window.nleShellHarness.renderView());
        // The remount restores the retained Clip editor function, which is the only panel that
        // renders the launcher; without that selection this click has nothing to wait for.
        await expect(
          page.getByRole("tab", { name: "Clip editor", exact: true }),
        ).toHaveAttribute("aria-selected", "true");
        await page.locator('[data-h3-nle-entry="open"]').click();
        await expect(
          page.getByRole("button", { name: "Play", exact: true }),
        ).toBeEnabled();
        expect(oracle.transactions).toHaveLength(sent + 1);
        injections.lifecycle = (injections.lifecycle ?? 0) + 1;
      }
    });

    // seam.backend_restart -- the core process is replaced by one holding the same state.
    await row("backend_restart").step(
      "the_session_reconciles_across_a_restart",
      async () => {
        const before = await shellSnapshot(page);
        const sent = oracle.transactions.length;
        await oracle.restart();
        await clipButton.first().click();
        await expect
          .poll(async () => (await shellSnapshot(page)).authoringStatus)
          .toBe("ready");
        const after = await shellSnapshot(page);
        // The replacement process holds the state the browser still believes in. The proof is
        // the command the browser sends next: it carries the pre-restart revision as its CAS and
        // the new core accepts it, which it can only do holding that exact state. Nothing tells
        // the browser a restart happened -- it must not need to be told, and asserting that it
        // re-read would be asserting a notification the product deliberately does not have.
        const issued = oracle.transactions[sent] as Record<string, unknown>;
        expect(issued.expected_timeline_revision).toBe(
          before.timelineSnapshot!.timelineRevision,
        );
        expect(after.receipts).toBe(before.receipts + 1);
        expect(after.timelineSnapshot!.timelineRevision).toBeGreaterThan(
          before.timelineSnapshot!.timelineRevision,
        );
        // One command after the restart, and no replay of anything the core already had.
        expect(oracle.transactions).toHaveLength(sent + 1);
        injections.backend_restart = 1;
      },
    );

    const apis = await apiUse(page);
    await row("worker").step("no_worker_was_ever_constructed", () => {
      expect(apis.worker).toBe(0);
    });
    await row("webgl").step("no_non_2d_context_was_ever_requested", () => {
      expect(apis.webgl).toBe(0);
    });
    injections.worker = 0;
    injections.webgl = 0;

    const interval = Date.now() - started;
    const cancellationMax = Math.max(...cancellations);
    await evidence.measurement("stress.cancellation_max", {
      observed: cancellationMax,
      unit: "ms",
      method:
        "injection to the session reporting it settled with at most one live media owner",
      sample_count: cancellations.length,
      interval_ms: interval,
    });
    await evidence.measurement("floor.follower_injections", {
      observed: injections.follower ?? 0,
      unit: "count",
      method:
        "document-hidden suspensions of the embedded-audio follower, each confirmed at the announced state",
      sample_count: injections.follower ?? 0,
      interval_ms: interval,
    });
    for (const seam of SEAM_ROWS)
      await evidence.seam(seam, row(seam), {
        injections: injections[seam] ?? 0,
        cancellation_max_ms: cancellationMax,
      });
  } finally {
    await oracle.close();
  }
});

test("NLE-STRESS-V1 remount cycles restore retained state paused and release every owner", async ({
  page,
}, testInfo) => {
  test.setTimeout(900_000);
  const started = Date.now();
  const oracle = await openIntegratedShell(page, "smoke", {
    extraParams: { viewDestroy: "1" },
    // The editor is reopened by its own launcher after every remount, and the launcher exists
    // only on the Clip editor function -- so this workload selects that function before opening,
    // and the retention contract is what has to bring it back after each destroy.
    beforeOpen: toClipEditor,
  });
  const slider = playheadSlider(page);
  const status = page.locator(MONITOR_STATUS);
  await slider.focus();
  await page.keyboard.press("Home");
  await page.keyboard.press("Shift+ArrowRight");
  const retainedFrame = await expect
    .poll(() => playheadFrame(slider))
    .toBeGreaterThan(0)
    .then(() => playheadFrame(slider));
  const checks = new Checks();
  const teardowns: number[] = [];
  const cycles = 25;
  await checks.step("every_cycle_restores_the_position_paused", async () => {
    for (let cycle = 0; cycle < cycles; cycle += 1) {
      const began = Date.now();
      await page.evaluate(() => window.nleShellHarness.destroyView());
      await expect(page.locator("[data-h3-nle-root]")).toHaveCount(0);
      await expect
        .poll(async () => (await shellSnapshot(page)).mediaOwnership.live)
        .toBe(0);
      teardowns.push(Date.now() - began);
      const released = (await shellSnapshot(page)).mediaOwnership;
      expect(released.released).toBe(released.acquired);
      await page.evaluate(() => window.nleShellHarness.renderView());
      // The retained function is what puts the launcher back: the remount restores the Clip
      // editor selection, and only that panel renders the launcher. Asserting it here makes the
      // retention contract part of the cycle rather than an assumption the next click depends on.
      await expect(
        page.getByRole("tab", { name: "Clip editor", exact: true }),
      ).toHaveAttribute("aria-selected", "true");
      await page.locator('[data-h3-nle-entry="open"]').click();
      await expect(
        page.getByRole("button", { name: "Play", exact: true }),
      ).toBeEnabled();
      await expect(status).toHaveText("Monitor paused.");
      // GUARD: poll. The monitor defers the restoring seek until its own opening settles, so the
      // paused status is reached before the retained frame is presented; a single read here is a
      // race against the product's own deferral and observed 0 on the cycle that found this.
      await expect.poll(() => playheadFrame(slider)).toBe(retainedFrame);
    }
  });
  await checks.step("no_cycle_sent_or_replayed_anything", async () => {
    expect(oracle.transactions).toHaveLength(0);
    const snapshot = await shellSnapshot(page);
    expect(snapshot.queuedPrompts).toBe(0);
    expect(snapshot.renderJobRequests).toBe(0);
  });
  const interval = Date.now() - started;
  const evidence = hardeningEvidence(testInfo);
  await evidence.measurement("stress.teardown_max", {
    observed: Math.max(...teardowns),
    unit: "ms",
    method:
      "native view destroy to zero live media owners with acquired equal to released",
    sample_count: teardowns.length,
    interval_ms: interval,
  });
  await evidence.measurement("floor.remount_cycles", {
    observed: cycles,
    unit: "count",
    method:
      "destroy and host re-render cycles, each followed by an explicit open that restored the retained position paused",
    sample_count: cycles,
    interval_ms: interval,
  });
});

/** JS heap, read through CDP with a collection immediately before each sample. */
async function heapReader(page: Page) {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Performance.enable");
  return {
    read: async () => {
      await cdp.send("HeapProfiler.collectGarbage");
      const metrics = await cdp.send("Performance.getMetrics");
      const value = metrics.metrics.find(
        (entry: { name: string }) => entry.name === "JSHeapUsedSize",
      )?.value;
      // Fail closed: absent instrumentation must never read as a passing zero.
      if (typeof value !== "number" || !Number.isFinite(value))
        throw new Error("heap_instrumentation_unavailable");
      return value;
    },
    detach: () => cdp.detach(),
  };
}

const nleDomCounts = (page: Page) =>
  page.locator(OVERLAY).evaluate((root) => {
    const canvases = root.querySelectorAll("canvas");
    const compositionCanvases = root.querySelectorAll(
      'canvas[data-h3-nle-canvas="composition"]',
    ).length;
    const decorationCanvases = root.querySelectorAll(
      'canvas[data-h3-nle-canvas="timeline_decoration"]',
    ).length;
    // B-M2563-08: the bin's card thumbnails (M25-48 `MediaThumbnail`) are a third, bounded class:
    // named, inside a mounted card's art box, and never more than the cards mounted.
    const thumbnailCanvases = root.querySelectorAll(
      'canvas[data-h3-nle-canvas="bin_thumbnail"]',
    );
    const placedThumbnails = root.querySelectorAll(
      '[data-h3-nle-card-index] .h3-nle-media-art > canvas[data-h3-nle-canvas="bin_thumbnail"]',
    ).length;
    return {
      rows: root.querySelectorAll("[data-h3-nle-track]").length,
      clips: root.querySelectorAll("[data-h3-nle-clip]").length,
      // The virtualized grid's own scaffolding: the ARIA rows and cells actually mounted.
      gridNodes: root.querySelectorAll(
        '[role="row"],[role="gridcell"],[role="rowheader"]',
      ).length,
      dom: root.querySelectorAll("*").length,
      canvases: canvases.length,
      compositionCanvases,
      decorationCanvases,
      thumbnailCanvases: thumbnailCanvases.length,
      misplacedThumbnails: thumbnailCanvases.length - placedThumbnails,
      mountedCards: root.querySelectorAll("[data-h3-nle-card-index]").length,
      unclassifiedCanvases:
        canvases.length -
        compositionCanvases -
        decorationCanvases -
        thumbnailCanvases.length,
    };
  });

function canvasContractViolations(
  counts: Awaited<ReturnType<typeof nleDomCounts>>,
) {
  const violations: string[] = [];
  if (counts.compositionCanvases !== 1) violations.push("composition_count");
  if (counts.decorationCanvases > 1) violations.push("decoration_count");
  if (counts.thumbnailCanvases > counts.mountedCards)
    violations.push("thumbnail_count");
  if (counts.misplacedThumbnails !== 0) violations.push("thumbnail_placement");
  if (counts.unclassifiedCanvases !== 0) violations.push("unclassified_canvas");
  if (
    counts.canvases !==
    counts.compositionCanvases +
      counts.decorationCanvases +
      counts.thumbnailCanvases
  )
    violations.push("canvas_partition");
  return violations;
}

test("C6 canvas sampler classifies ready surfaces and rejects every escape", async ({
  page,
}) => {
  await openIntegratedShell(page, "smoke");
  await expect(page.locator('[data-h3-nle-canvas="composition"]')).toHaveCount(
    1,
  );
  await expect(
    page.locator('[data-h3-nle-canvas="timeline_decoration"]'),
  ).toHaveCount(1);
  // The bin's thumbnails are drawn, so the third class is exercised, not vacuous.
  await expect(
    page.locator('canvas[data-h3-nle-canvas="bin_thumbnail"]').first(),
  ).toBeAttached({ timeout: 15_000 });
  expect(canvasContractViolations(await nleDomCounts(page))).toEqual([]);

  // B-M2563-08: the bin's thumbnails are classified, not ignored. A named thumbnail outside a
  // card's art box is an escape like any unnamed canvas.
  for (const mutation of [
    "composition",
    "timeline_decoration",
    "bin_thumbnail",
    null,
  ] as const) {
    await page.locator(OVERLAY).evaluate((root, kind) => {
      const canvas = document.createElement("canvas");
      canvas.dataset.h3C6Mutation = "true";
      if (kind !== null) canvas.dataset.h3NleCanvas = kind;
      root.append(canvas);
    }, mutation);
    expect(canvasContractViolations(await nleDomCounts(page))).not.toEqual([]);
    await page
      .locator('canvas[data-h3-c6-mutation="true"]')
      .evaluate((canvas) => canvas.remove());
  }
});

/**
 * Presses Play when the transport is offering it, and leaves the transport alone when it is not.
 *
 * GUARD (B-M2545-32): the monitor's transport row carries ONE play/pause button, named for the
 * action it performs, so `transport.play` is ABSENT while the monitor plays -- it is not a
 * disabled element. `nleWorkspaceMonitor.test.tsx` pins both halves of that, and
 * `nleHardeningActions.spec.ts` pins the one non-playing state where the button exists and is
 * disabled: a blocked monitor. `locator.isEnabled()` waits for its selector to resolve, so asking
 * it of `transport.play` while the monitor plays blocks for the whole locator timeout instead of
 * answering false, which is what a workload that seeks during playback does on its first tick.
 * `count()` does not wait, so it is what asks whether the button is there at all.
 *
 * Do not "fix" a timeout here with a shorter locator timeout or a try/catch around the click:
 * both turn a genuinely blocked transport into a silent skip, and the advancement assertion that
 * follows would then report a budget failure ten minutes later instead of the blocked monitor.
 */
async function pressPlayIfOffered(play: Locator, status: Locator) {
  if ((await play.count()) === 0) return;
  if (!(await play.isEnabled())) return;
  await play.click();
  await expect(status).toHaveText("Monitor playing.");
}

/**
 * Plays the composition for at least `targetMs` of confirmed advancing playback, restarting from
 * the head when the transport reaches the end, and samples the caller's probe every tick.
 */
async function playFor(
  page: Page,
  targetMs: number,
  lastFrame: number,
  probe: () => Promise<void>,
) {
  const slider = playheadSlider(page);
  const status = page.locator(MONITOR_STATUS);
  const play = page.locator('[data-h3-nle-control="transport.play"]');
  await play.click();
  await expect(status).toHaveText("Monitor playing.");
  let playedMs = 0;
  while (playedMs < targetMs) {
    const previous = await playheadFrame(slider);
    const began = Date.now();
    await page.waitForTimeout(3_000);
    const playing = (await status.textContent()) === "Monitor playing.";
    const next = await playheadFrame(slider);
    if (playing) {
      expect(next).toBeGreaterThan(previous);
      playedMs += Date.now() - began;
    } else {
      // The only accepted reason to stop is the end of the composition.
      expect(next).toBe(lastFrame);
      await slider.focus();
      await page.keyboard.press("Home");
      await expect.poll(() => playheadFrame(slider)).toBe(0);
      await expect(play).toBeEnabled();
      await play.click();
      await expect(status).toHaveText("Monitor playing.");
    }
    await probe();
  }
  return playedMs;
}

test("M25-16-overlay-v1-smoke five-minute decoded playback stays within the smoke budgets", async ({
  page,
}, testInfo) => {
  test.setTimeout(1_200_000);
  const started = Date.now();
  const oracle = await openIntegratedShell(page, "smoke");
  const heap = await heapReader(page);
  try {
    const baseline = await heap.read();
    await page.evaluate(() => window.nleShellHarness.resetMeasurements());
    const maxima = {
      rows: 0,
      clips: 0,
      gridNodes: 0,
      dom: 0,
      canvases: 0,
      compositionCanvases: 0,
      decorationCanvases: 0,
      unclassifiedCanvases: 0,
    };
    const playedMs = await playFor(page, 300_000, 120 * 24 - 1, async () => {
      const counts = await nleDomCounts(page);
      expect(canvasContractViolations(counts)).toEqual([]);
      for (const key of Object.keys(maxima) as (keyof typeof maxima)[])
        maxima[key] = Math.max(maxima[key], counts[key]);
    });
    await page.locator('[data-h3-nle-control="transport.pause"]').click();
    const growth = (await heap.read()) - baseline;
    const measurements = await shellSnapshot(page);
    const checks = new Checks();
    await checks.step("playback_advanced_for_the_whole_window", () => {
      expect(playedMs).toBeGreaterThanOrEqual(300_000);
    });
    await checks.step("the_sidebar_never_committed_on_a_tick", () => {
      expect(measurements.sidebarCommits).toBe(0);
    });
    await checks.step("the_overlay_profiler_produced_samples", () => {
      // Fail closed: an empty sample is missing instrumentation, not a pass.
      expect(measurements.commitDurationsMs.length).toBeGreaterThan(0);
    });
    await checks.step("the_workload_started_no_work", () => {
      expect(oracle.transactions).toHaveLength(0);
      expect(measurements.queuedPrompts).toBe(0);
      expect(measurements.renderJobRequests).toBe(0);
    });
    const interval = Date.now() - started;
    const evidence = hardeningEvidence(testInfo);
    await evidence.measurement("smoke.playback_advancing", {
      observed: playedMs,
      unit: "ms",
      method:
        "wall time of three-second ticks in which the transport frame strictly advanced while the monitor reported playing",
      sample_count: Math.round(playedMs / 3_000),
      interval_ms: interval,
    });
    await evidence.measurement("smoke.heap_growth", {
      observed: growth,
      unit: "bytes",
      method:
        "CDP JSHeapUsedSize after HeapProfiler.collectGarbage, before and after the window",
      sample_count: 2,
      interval_ms: interval,
    });
    await evidence.measurement("smoke.nle_dom_nodes_max", {
      observed: maxima.dom,
      unit: "count",
      method: "elements under the overlay surface, sampled every tick",
      sample_count: Math.round(playedMs / 3_000),
      interval_ms: interval,
    });
    await evidence.measurement("smoke.sidebar_commits_per_tick", {
      observed: measurements.sidebarCommits,
      unit: "count",
      method:
        "Sidebar Profiler commits recorded across the whole playback window, excluding the sibling overlay",
      sample_count: 1,
      interval_ms: interval,
    });
    await evidence.workload("smoke", checks, {
      fixture: "M25-16-overlay-v1-smoke",
      played_ms: playedMs,
      heap_growth_bytes: growth,
      maxima,
      nle_commits: measurements.nleCommits,
      owners: measurements.mediaOwnership,
    });
  } finally {
    await heap.detach();
  }
});

const TRANSPORT_ROWS = [
  "action.transport.play",
  "action.transport.pause",
  "action.transport.step_back",
  "action.transport.step_forward",
  "action.transport.seek",
  "action.transport.recover",
  "action.transport.zoom_in",
  "action.transport.zoom_out",
  "action.transport.scroll",
] as const;
const AUDIO_ROWS = [
  "action.audio.embedded_primary_follow",
  "action.audio.deferred",
] as const;

/** JS heap and cumulative main-thread task time, both from the same CDP metrics read. */
async function runtimeMetrics(page: Page) {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Performance.enable");
  const value = (
    metrics: { metrics: { name: string; value: number }[] },
    name: string,
  ) => {
    const found = metrics.metrics.find((entry) => entry.name === name)?.value;
    // Fail closed: absent instrumentation must never read as a passing zero.
    if (typeof found !== "number" || !Number.isFinite(found))
      throw new Error(`metric_unavailable:${name}`);
    return found;
  };
  return {
    read: async () => {
      await cdp.send("HeapProfiler.collectGarbage");
      const metrics = await cdp.send("Performance.getMetrics");
      return {
        heap: value(metrics, "JSHeapUsedSize"),
        // Seconds of main-thread task time since the process started.
        taskSeconds: value(metrics, "TaskDuration"),
      };
    },
    detach: () => cdp.detach(),
  };
}

/** The decoders the harness media client currently holds, by their own playing state. */
const decoders = (page: Page) =>
  page.evaluate(() => {
    const registry = window.__nleMediaDebug;
    const elements = new Set<HTMLVideoElement>([
      ...(registry?.videoElementsByAsset.values() ?? []),
      ...(registry?.videoElementsByClip.values() ?? []),
    ]);
    const held = [...elements];
    return {
      active: held.filter((element) => !element.paused).length,
      warm: held.filter(
        (element) => element.paused && element.getAttribute("src") !== null,
      ).length,
    };
  });

const followerSurfaces = (page: Page) =>
  page.evaluate(
    () => document.querySelectorAll('[data-h3-nle-status="audio"]').length,
  );

test("NLE-STRESS-V1 ten-minute decoded playback and seek workload stays within the runtime budgets", async ({
  page,
}, testInfo) => {
  // A harness limit, not a budget: ten minutes of confirmed advancing playback plus 600 explicit
  // seeks, sampled in three-second windows and settled after every tick, runs well past the
  // default.
  test.setTimeout(2_400_000);
  const started = Date.now();
  await countRuntimeApis(page);
  const oracle = await openIntegratedShell(page, "stress");
  const metrics = await runtimeMetrics(page);
  try {
    const slider = playheadSlider(page);
    const status = page.locator(MONITOR_STATUS);
    const play = page.locator('[data-h3-nle-control="transport.play"]');
    const lastFrame = Number(await slider.getAttribute("aria-valuemax"));
    expect(lastFrame).toBeGreaterThan(0);
    const baseline = await metrics.read();
    await page.evaluate(() => window.nleShellHarness.resetMeasurements());
    await resetLongTasks(page);

    const maxima = {
      rows: 0,
      clips: 0,
      gridNodes: 0,
      dom: 0,
      canvases: 0,
      compositionCanvases: 0,
      decorationCanvases: 0,
      unclassifiedCanvases: 0,
      active: 0,
      warm: 0,
      followers: 0,
    };
    let playedMs = 0;
    let seeks = 0;
    let audioSeeks = 0;
    // Where the embedded-audio follower can actually be followed; see `primaryVideoStarts`.
    const primaryStarts = await primaryVideoStarts(page);
    let tick = 0;
    /** Where the previous iteration's last seek asked the transport to be; see the guard below. */
    let landed: number | null = null;
    const wallStarted = Date.now();
    while (playedMs < 600_000) {
      await pressPlayIfOffered(play, status);
      const previous = await playheadFrame(slider);
      const began = Date.now();
      // The window is sampled rather than merely waited out: when playback does not advance, the
      // two endpoints alone say nothing about when it stopped or what the transport was doing,
      // and this workload takes ten minutes to reach the failure again.
      const trace: string[] = [];
      while (Date.now() - began < 3_000) {
        await page.waitForTimeout(250);
        trace.push(
          `${Date.now() - began}:${await playheadFrame(slider)}:${(await status.textContent()) ?? ""}`,
        );
      }
      const playing = (await status.textContent()) === "Monitor playing.";
      const next = await playheadFrame(slider);
      if (playing) {
        expect(
          next,
          `tick ${tick} from ${previous} landed=${landed} trace=${trace.join(" | ")}`,
        ).toBeGreaterThan(previous);
        playedMs += Date.now() - began;
        // Decoder state is only meaningful while the composition is actually running: the
        // presenting decoder is playing and anything held ready for the next clip is paused.
        const held = await decoders(page);
        maxima.active = Math.max(maxima.active, held.active);
        maxima.warm = Math.max(maxima.warm, held.warm);
      } else {
        expect(next).toBe(lastFrame);
        await slider.focus();
        await page.keyboard.press("Home");
        await expect.poll(() => playheadFrame(slider)).toBe(0);
      }
      const counts = await nleDomCounts(page);
      expect(canvasContractViolations(counts)).toEqual([]);
      for (const key of [
        "rows",
        "clips",
        "gridNodes",
        "dom",
        "canvases",
        "compositionCanvases",
        "decorationCanvases",
        "unclassifiedCanvases",
      ] as const)
        maxima[key] = Math.max(maxima[key], counts[key]);
      maxima.followers = Math.max(
        maxima.followers,
        await followerSurfaces(page),
      );

      // Four explicit seeks per tick. The first pauses and places the playhead on a primary-video
      // clip, then Play genuinely engages the follower. The first ruler key begins a new scrub and
      // pauses once before seeking; the next two rapid keys share that scrub boundary and exercise
      // last-intent-wins exact seeking. The guard below accepts only the requested paused position.
      const landing = primaryStarts[tick % primaryStarts.length]! + 4;
      tick += 1;
      await seekPlayhead(page, slider, landing);
      seeks += 1;
      await expect.poll(() => playheadFrame(slider)).toBe(landing);
      await pressPlayIfOffered(play, status);
      // A bounded wait for engagement, never an assertion: a tick where the follower did not
      // engage is simply not counted toward the synchronized floor.
      const audible = await (async () => {
        let state: string | null = null;
        for (let attempt = 0; attempt < 12; attempt += 1) {
          state = await page
            .locator('[data-h3-nle-status="audio"]')
            .getAttribute("data-h3-nle-audio-state");
          if (state === "following" || state === "seeking") return state;
          await page.waitForTimeout(100);
        }
        return state;
      })();
      await slider.focus();
      await page.keyboard.press("ArrowRight");
      seeks += 1;
      if (audible === "following" || audible === "seeking") audioSeeks += 1;
      await page.keyboard.press("Shift+ArrowRight");
      await page.keyboard.press("Shift+ArrowLeft");
      seeks += 2;
      // Where this tick's four seeks asked the transport to be: one frame past the landing, since
      // The grid-size right/left pair cancels.
      landed = landing + 1;
      // GUARD: the next iteration measures that playback advances, so this one must leave the
      // transport actually presenting the position it asked for. Two reads a moment apart, both
      // inside the window, are what establishes that -- one is not enough. The transport shows
      // the frame the user last asked for while the seek is in flight (B3-D10) and falls back to
      // the last *presented* frame the instant that seek settles, which is before its
      // presentation lands; a single read can therefore catch the target while the picture is
      // still the pre-seek one. The workload then measured growth from 13578 while playback was
      // really at 8, and the deliberate jump backwards read as playback running backwards
      // (observed at tick 81; the window's own trace is in the assertion message above).
      const near = (value: number) =>
        value >= landed! && value < landed! + 10 * 24;
      await expect
        .poll(async () => {
          const first = await playheadFrame(slider);
          await page.waitForTimeout(300);
          return near(first) && near(await playheadFrame(slider));
        })
        .toBe(true);
    }
    // The final ruler scrub is specified to leave transport paused. Only click Pause when the
    // last confirmed playback window is still running; the single action button is Play while
    // paused, so unconditionally locating transport.pause turns a healthy finish into a timeout.
    if ((await status.textContent()) === "Monitor playing.")
      await page.locator('[data-h3-nle-control="transport.pause"]').click();
    await expect(status).toHaveText("Monitor paused.");
    const busyWindowMs = Date.now() - wallStarted;
    const after = await metrics.read();
    const observed = await shellSnapshot(page);
    const playbackLongTasks = await longTasks(page);

    // An idle window with the editor open and nothing driving it.
    await resetLongTasks(page);
    await page.waitForTimeout(15_000);
    const idle = await longTasks(page);
    const apis = await apiUse(page);

    const busyRatio =
      (after.taskSeconds - baseline.taskSeconds) / (busyWindowMs / 1000);
    const renders = [...observed.commitDurationsMs];
    const checks = new Checks();
    await checks.step("playback_advanced_for_the_whole_window", () => {
      expect(playedMs).toBeGreaterThanOrEqual(600_000);
    });
    await checks.step("the_seek_floors_were_reached", () => {
      expect(seeks).toBeGreaterThanOrEqual(600);
      expect(audioSeeks).toBeGreaterThanOrEqual(120);
    });
    await checks.step("the_profilers_produced_samples", () => {
      expect(renders.length).toBeGreaterThan(0);
      expect(playbackLongTasks.count).toBeGreaterThanOrEqual(0);
    });
    await checks.step("the_sidebar_never_committed_on_a_tick", () => {
      expect(observed.sidebarCommits).toBe(0);
    });
    await checks.step("the_workload_started_no_work", () => {
      expect(oracle.transactions).toHaveLength(0);
      expect(observed.queuedPrompts).toBe(0);
      expect(observed.renderJobRequests).toBe(0);
    });

    const interval = Date.now() - started;
    const evidence = hardeningEvidence(testInfo);
    const ticks = Math.round(playedMs / 3_000);
    const record = (
      id: string,
      value: number,
      unit: "ratio" | "ms" | "bytes" | "count",
      method: string,
      samples: number,
    ) =>
      evidence.measurement(id, {
        observed: value,
        unit,
        method,
        sample_count: samples,
        interval_ms: interval,
      });
    await record(
      "stress.main_thread_busy_ratio",
      busyRatio,
      "ratio",
      "CDP TaskDuration consumed during the workload divided by its wall time",
      2,
    );
    await record(
      "stress.idle_long_task_max",
      idle.max,
      "ms",
      "longest PerformanceObserver longtask entry during a fifteen-second idle window with the editor open",
      idle.count,
    );
    await record(
      "stress.render_p95",
      p95(renders)!,
      "ms",
      "nearest-rank p95 of the overlay Profiler's commit durations",
      renders.length,
    );
    const delivered = observed.presentationSamples.map(
      (sample) => sample.deliveredMs,
    );
    if (delivered.length === 0 || observed.presentationSamplesDropped !== 0)
      throw new Error("presentation samples are missing or partial");
    await record(
      "stress.presentation_p95",
      p95(delivered)!,
      "ms",
      "nearest-rank p95 of the composition session's presented frames, each measured from the " +
        "compositor call to the first animation frame after it",
      delivered.length,
    );
    await record(
      "stress.presentation_max",
      Math.max(...delivered),
      "ms",
      "the slowest presented frame of the same sample, by the same measurement",
      delivered.length,
    );
    await record(
      "stress.sidebar_commits_per_tick",
      observed.sidebarCommits,
      "count",
      "Sidebar Profiler commits across the whole workload, excluding the sibling overlay",
      1,
    );
    await record(
      "stress.heap_growth",
      after.heap - baseline.heap,
      "bytes",
      "CDP JSHeapUsedSize after HeapProfiler.collectGarbage, before and after the workload",
      2,
    );
    await record(
      "stress.mounted_clip_nodes_max",
      maxima.clips,
      "count",
      "clip nodes mounted under the overlay, sampled every tick",
      ticks,
    );
    await record(
      "stress.mounted_track_rows_max",
      maxima.rows,
      "count",
      "track rows mounted under the overlay, sampled every tick",
      ticks,
    );
    await record(
      "stress.row_ruler_nodes_max",
      maxima.gridNodes,
      "count",
      "ARIA grid row, cell and row-header nodes mounted under the overlay, sampled every tick",
      ticks,
    );
    await record(
      "stress.nle_dom_nodes_max",
      maxima.dom,
      "count",
      "elements under the overlay surface, sampled every tick",
      ticks,
    );
    await record(
      "stress.composition_canvas_surfaces",
      maxima.compositionCanvases,
      "count",
      "composition canvases under the overlay surface, sampled every tick",
      ticks,
    );
    await record(
      "stress.decoration_canvas_surfaces",
      maxima.decorationCanvases,
      "count",
      "timeline decoration canvases under the overlay surface, sampled every tick",
      ticks,
    );
    await record(
      "stress.active_video_owners_max",
      maxima.active,
      "count",
      "leased decoders reporting themselves as playing, sampled during confirmed playback",
      ticks,
    );
    await record(
      "stress.warm_video_owners_max",
      maxima.warm,
      "count",
      "leased decoders holding a source while paused, sampled during confirmed playback",
      ticks,
    );
    await record(
      "stress.retained_surfaces_max",
      observed.mediaOwnership.maximumSurfaces,
      "count",
      "peak simultaneously retained media surfaces held by the media client",
      observed.mediaOwnership.acquired,
    );
    await record(
      "stress.retained_surface_bytes_max",
      observed.mediaOwnership.maximumRetainedBytes,
      "bytes",
      "peak simultaneously retained media bytes held by the media client",
      observed.mediaOwnership.acquired,
    );
    await record(
      "stress.worker_allocations",
      apis.worker,
      "count",
      "Worker and SharedWorker constructions counted from before the first script of the page",
      1,
    );
    await record(
      "stress.blob_urls_max",
      observed.mediaOwnership.maximumBlobUrls,
      "count",
      "peak simultaneously live Blob URLs minted by the media client",
      observed.mediaOwnership.acquired,
    );
    await record(
      "stress.follower_count_max",
      maxima.followers,
      "count",
      "embedded-audio follower surfaces mounted, one per follower instance, sampled every tick",
      ticks,
    );
    await record(
      "stress.playback_advancing",
      playedMs,
      "ms",
      "wall time of three-second ticks in which the transport frame strictly advanced while the monitor reported playing",
      ticks,
    );
    await record(
      "floor.seeks",
      seeks,
      "count",
      "explicit keyboard seeks issued on the focused transport slider",
      seeks,
    );
    await record(
      "floor.audio_seeks",
      audioSeeks,
      "count",
      "seeks issued while the follower announced it was following or seeking the primary video",
      audioSeeks,
    );
    await record(
      "audio.standalone_allocations",
      apis.standaloneAudio,
      "count",
      "unique standalone native Audio, DOM-created and document-inserted parser audio identities from before page scripts",
      1,
    );
    await record(
      "audio.workspace_context_allocations",
      apis.audioContext,
      "count",
      "workspace AudioContext constructions counted from before the first script of the page",
      Math.max(1, maxima.followers),
    );
    const facts = {
      played_ms: playedMs,
      seeks,
      audio_seeks: audioSeeks,
      busy_ratio: busyRatio,
      maxima,
      owners: observed.mediaOwnership,
      long_tasks: { playback: playbackLongTasks, idle },
      apis,
    };
    await evidence.workload("stress", checks, {
      ...facts,
      fixture: "NLE-STRESS-V1",
    });
    for (const row of [...TRANSPORT_ROWS, ...AUDIO_ROWS])
      await evidence.row(row, "stress", checks, facts);
  } finally {
    await metrics.detach();
  }
});

/**
 * The scheduled edit phase. It runs the whole canonical command corpus on one accepted base and
 * then the repeated phases the frozen floors require, so every command row's stress dimension and
 * every edit budget come from one executed workload rather than from 33 separate runs.
 */
test("NLE-STRESS-V1 scheduled edit phase accepts and refuses every command within the edit budgets", async ({
  page,
}, testInfo) => {
  // A harness limit, not a budget: `floor.edit_phase_wall` (20 minutes) is the frozen ceiling the
  // measurement below is compared against, and the limit here has to sit above it.
  test.setTimeout(2_400_000);
  const started = Date.now();
  // A transaction carrying the armed command kind meets another client's edit that the core has
  // already accepted, so the browser's base is stale and the refusal is the core's own.
  //
  // GUARD: arm by the command kind that must meet the conflict, never "every transaction". The
  // rebase row's own setup sends a selection first and needs it accepted; a selection refused on
  // a stale base commits no receipt, and the shared `settled()` then waits for a receipt that
  // will never exist. Only the edit under test is supposed to be refused.
  let conflictKind: string | null = null;
  const oracle = await openIntegratedShell(page, "stress", {
    service: true,
    concurrent: (transaction, index) => {
      const commands = (
        transaction as Readonly<{ commands?: readonly { kind?: string }[] }>
      ).commands;
      if (
        conflictKind === null ||
        !(commands ?? []).some((command) => command.kind === conflictKind)
      )
        return undefined;
      return {
        ...transaction,
        request_id: `concurrent-${index}`,
        transaction_id: `tx-concurrent-${index}`,
        commands: [
          {
            kind: "set_clip_enabled",
            // IMPORTANT: the accepted base keeps this reserved clip enabled. Disable it
            // deterministically; transaction-index parity can produce a no-op and therefore no
            // fingerprint conflict after an unrelated setup transaction is added.
            payload: { clip_id: CONCURRENT_CLIP, enabled: false },
          },
        ],
      };
    },
  });
  try {
    const count = new Counter();
    const checks = new Checks();
    const counters = {
      editAttempts: 0,
      conflictRebase: 0,
      primaryGeometry: 0,
      undoRedo: 0,
      sourceReplacements: 0,
      edgeDragsLeft: 0,
      edgeDragsRight: 0,
      moveDrags: 0,
      groupMoveDrags: 0,
      crossTrackMoves: 0,
      marqueeSelections: 0,
      rulerScrubs: 0,
      cancelledDrafts: 0,
      trimUndoRedo: 0,
    };
    let commitsPerEditMax = 0;
    let editPhase = "setup";
    // IMPORTANT (M25-21 B3-D51): a breach of the commit ceiling is recorded with the phase that
    // produced it. A bare maximum cannot be routed to a cause, and the phases differ in kind: a
    // keyboard draft and a pointer drag both render while the user is still deciding, whereas a
    // single activation does not -- so the phase is what says whether a high count is the
    // accepted edit's own cost or the gesture's.
    const commitOutliers: {
      phase: string;
      attempt: number;
      commits: number;
      states: readonly string[];
    }[] = [];
    // Complete counts over every attempt, beside the capped trace array above.
    const commitHistogram = new Map<number, number>();
    const violationsByPhase = new Map<string, number>();
    let commitViolations = 0;
    const rebasePresentationMs: number[] = [];
    const rebaseTrails: string[][] = [];
    const competingSetup: {
      appliedMs: number;
      core: Record<string, number> | null;
      accepted: boolean;
      status: number;
    }[] = [];
    const slider = playheadSlider(page);

    /**
     * Runs one act and attributes the transactions and commits it produced to it.
     *
     * IMPORTANT (M25-21 B3-D54): an act that drafts before it commits calls `mark()` at the
     * moment the user commits, which is where the commit window starts. The frozen measurement is
     * "commits attributed to a single accepted edit"; a keyboard draft before Enter and the
     * intermediate moves of a pointer drag are the live preview the user asked for, and they are
     * measured by `stress.handler_p95`, `stress.main_thread_busy_ratio` and
     * `stress.idle_long_task_max`. Charging them to the edit measures the gesture instead of the
     * edit, and it hid what the real per-command cost is. An act that is a single activation
     * needs no mark and keeps the whole window.
     */
    const attempt = async (
      act: (mark: () => Promise<void>) => Promise<void>,
    ) => {
      // IMPORTANT (M25-21 B3-D56): open the window only once the *previous* edit has stopped
      // rendering. Accepting an edit re-opens the monitor, and that pair of commits lands after
      // the session has already left `pending`, so sampling here the moment the last attempt
      // returned charges its tail to this one. The trailing commit-state window showed it
      // directly: five-commit outliers whose first commits still carried the previous revision
      // and the previous transaction id. Settling is bounded, and the loop keeps the last sample
      // rather than failing, so a genuinely busy surface is still measured rather than skipped.
      let before = await shellSnapshot(page);
      for (let settle = 0; settle < 20; settle += 1) {
        // 250 ms, not 100: the monitor's re-open lands through a media element, and at 100 ms
        // nineteen of forty keyboard geometry edits still opened their window inside the previous
        // edit's tail. The bound below keeps a genuinely busy surface measurable rather than
        // waiting forever.
        await page.waitForTimeout(250);
        const again = await shellSnapshot(page);
        const quiet = again.nleCommits === before.nleCommits;
        before = again;
        if (quiet) break;
      }
      const sent = oracle.transactions.length;
      await act(async () => {
        // IMPORTANT (M25-46): Playwright's final pointer move can return before React commits
        // every move step. Two animation frames put those live-preview commits before the edit
        // window; without this barrier four cross-track previews were falsely attributed to the
        // following pointer-up transaction. Auto-scroll remains covered by its dedicated journey.
        await page.evaluate(
          () =>
            new Promise<void>((resolve) =>
              requestAnimationFrame(() =>
                requestAnimationFrame(() => resolve()),
              ),
            ),
        );
        before = await shellSnapshot(page);
      });
      await expect
        .poll(() => oracle.transactions.length, {
          message: `${editPhase} must submit a transaction after ${sent}`,
        })
        .toBeGreaterThan(sent);
      // GUARD: the oracle having the transaction is not the client having its receipt. Reading
      // the snapshot here, while the session is still `pending`, resynchronizes the counter one
      // receipt short -- and the next recipe's `settled()` then returns immediately, without its
      // own command having been applied, so its `prepare` reads a timeline that never changed.
      // That is how `track.remove` came to look for a track `track.add` had not yet created.
      await expect
        .poll(async () => (await shellSnapshot(page)).authoringStatus)
        .not.toBe("pending");
      const after = await shellSnapshot(page);
      const commits = after.nleCommits - before.nleCommits;
      commitsPerEditMax = Math.max(commitsPerEditMax, commits);
      counters.editAttempts += 1;
      // IMPORTANT (M25-21 B3-D56): these three aggregates count *every* attempt, while
      // `commitOutliers` below stops appending at 24. A capped trace array is how many traces were
      // kept, never how many attempts breached the ceiling, and reading it as a violation count
      // understated the breach once already. The aggregates are bounded by the number of distinct
      // phases and distinct commit counts, so they stay small without discarding anything.
      commitHistogram.set(commits, (commitHistogram.get(commits) ?? 0) + 1);
      if (commits > COMMIT_CEILING) {
        commitViolations += 1;
        violationsByPhase.set(
          editPhase,
          (violationsByPhase.get(editPhase) ?? 0) + 1,
        );
      }
      if (commits > COMMIT_CEILING && commitOutliers.length < 24)
        commitOutliers.push({
          phase: editPhase,
          attempt: counters.editAttempts,
          commits,
          // What each of this edit's commits saw, from the harness's trailing window: the phase
          // plus the timeline, monitor, audio and trim status the commit rendered. A count alone
          // says an edit cost too much; this says which renders it was.
          states: after.commitStates.slice(-commits),
        });
      // GUARD: the shared recipes' own `settled()` waits for the core's receipt counter to equal
      // this `Counter`, so it has to track every receipt this workload produced -- not only the
      // ones a recipe issued. This phase also commits trims, drags and undo/redo of its own, and
      // the first recipe whose `prepare` called `settled` would otherwise wait forever for a
      // receipt count the core had already passed. Resynchronizing here keeps that shared
      // contract true without weakening it.
      count.value = after.receipts;
      return oracle.transactions.length - sent;
    };

    /**
     * Undo everything a recipe committed, so the next one starts from the same accepted base.
     *
     * GUARD: the shared recipes are written to run against a *fresh* base, each selecting the
     * clips and tracks the fixture ships with. Run back to back on one base they destroy each
     * other's preconditions -- `clip.remove` deletes `clip-0`, and the next recipe's `prepare`
     * then waits forever for a clip button that no longer exists. Returning to the base between
     * commands is what makes "the whole corpus on one base" a real sequence rather than a claim.
     *
     * GUARD: undo back to the base's material identity, never once per receipt. The core's
     * content fingerprint deliberately excludes the revision counter and the selection, and a
     * recipe's accepted setup commits selections that the history therefore does not carry --
     * so counting receipts asks for more undo steps than the core holds, and the disabled Undo
     * control fails the run. The fingerprint is also the stronger claim: it proves the corpus
     * really did return to one base rather than merely that some button was pressed N times.
     */
    const undoControl = control(page, "history.undo");
    const fingerprint = async () =>
      (await shellSnapshot(page)).timelineSnapshot!.timelineFingerprint;
    const selectClip = async (clipId: string) => {
      const before = await shellSnapshot(page);
      const button = (await revealClip(page, clipId)).first();
      if ((await button.getAttribute("aria-pressed")) === "true") return;
      await button.click();
      // IMPORTANT: `ready` is already true before selection starts. Waiting on it alone races the
      // accepted snapshot replacement against the next drag, which correctly cancels that draft.
      await expect
        .poll(async () => (await shellSnapshot(page)).receipts)
        .toBe(before.receipts + 1);
      await expect
        .poll(async () => (await shellSnapshot(page)).authoringStatus)
        .toBe("ready");
      await expect(button).toHaveAttribute("aria-pressed", "true");
    };
    const directMove = async (
      clipId: string,
      deltaX: number,
      deltaY: number,
    ) => {
      let clip = (await revealClip(page, clipId)).first();
      {
        const timeline = (await shellSnapshot(page)).timelineSnapshot!;
        const member = timeline.clips.find((row) => row.clipId === clipId)!;
        const row = timeline.tracks.find(
          (track) => track.trackId === member.trackId,
        )!.order;
        const grid = page.getByRole("grid", {
          name: "Timeline tracks",
          exact: true,
        });
        // IMPORTANT: move throughput and edge auto-scroll are separate frozen paths. Put
        // both row centres inside the non-edge band here, otherwise holding the pointer beyond the
        // scrollport keeps producing live auto-scroll frames after `mark()` and charges previews
        // to the accepted edit. The dedicated auto-scroll journey still drives the real edge.
        await grid.evaluate(
          (element, target) => {
            const rowHeight = element.scrollHeight / target.rows;
            const sourceCenter = (target.row + 0.5) * rowHeight;
            element.scrollTop = Math.max(
              0,
              Math.min(
                element.scrollHeight - element.clientHeight,
                sourceCenter + target.deltaY / 2 - element.clientHeight / 2,
              ),
            );
          },
          { row, rows: timeline.tracks.length, deltaY },
        );
        await page.evaluate(
          () =>
            new Promise<void>((resolve) =>
              requestAnimationFrame(() =>
                requestAnimationFrame(() => resolve()),
              ),
            ),
        );
        clip = (await revealClip(page, clipId)).first();
      }
      let box = (await clip.boundingBox())!;
      {
        const grid = page.getByRole("grid", {
          name: "Timeline tracks",
          exact: true,
        });
        let gridBox = (await grid.boundingBox())!;
        const endpoint = box.y + box.height / 2 + deltaY;
        const safeTop = gridBox.y + 40;
        const safeBottom = gridBox.y + gridBox.height - 40;
        const correction =
          endpoint < safeTop
            ? endpoint - safeTop
            : endpoint > safeBottom
              ? endpoint - safeBottom
              : 0;
        if (correction !== 0) {
          await grid.evaluate((element, delta) => {
            element.scrollTop += delta;
          }, correction);
          await page.evaluate(
            () =>
              new Promise<void>((resolve) =>
                requestAnimationFrame(() =>
                  requestAnimationFrame(() => resolve()),
                ),
              ),
          );
          clip = (await revealClip(page, clipId)).first();
          box = (await clip.boundingBox())!;
          gridBox = (await grid.boundingBox())!;
        }
        const y = box.y + box.height / 2;
        expect(y).toBeGreaterThanOrEqual(gridBox.y + 32);
        expect(y).toBeLessThanOrEqual(gridBox.y + gridBox.height - 32);
        expect(y + deltaY).toBeGreaterThanOrEqual(gridBox.y + 32);
        expect(y + deltaY).toBeLessThanOrEqual(gridBox.y + gridBox.height - 32);
      }
      const x = box.x + box.width / 2;
      const y = box.y + box.height / 2;
      let draftTrace: Record<string, string | null> | null = null;
      try {
        await attempt(async (mark) => {
          // IMPORTANT: the signed round trips deliberately stay inside the available gap. Hold
          // Alt through the pointer lifecycle to exercise the finalized snap-bypass path instead
          // of letting a nearby boundary turn a completed-drag floor into a correct no-op.
          await page.keyboard.down("Alt");
          try {
            await page.mouse.move(x, y);
            await page.mouse.down();
            const status = page.locator('[data-h3-nle-status="move"]');
            await expect(status).toHaveAttribute(
              "data-h3-nle-move-phase",
              "idle",
            );
            await page.mouse.move(x + deltaX, y + deltaY, { steps: 4 });
            await expect(status).toHaveAttribute(
              "data-h3-nle-move-phase",
              "dragging",
            );
            draftTrace = await status.evaluate((element) => ({
              phase: element.getAttribute("data-h3-nle-move-phase"),
              code: element.getAttribute("data-code"),
              text: element.textContent,
            }));
            await mark();
            await page.mouse.up();
            await expect(status).toHaveAttribute(
              "data-h3-nle-move-phase",
              "idle",
              { timeout: 20_000 },
            );
          } finally {
            await page.keyboard.up("Alt");
          }
        });
      } catch (error) {
        throw new Error(
          `directMove ${clipId} (${deltaX}, ${deltaY}) draft ${JSON.stringify(draftTrace)}: ${String(error)}`,
        );
      }
      counters.moveDrags += 1;
    };
    const restoreBase = async (base: string) => {
      for (let undone = 0; undone < 32; undone += 1) {
        if ((await fingerprint()) === base) break;
        if (!(await undoControl.isEnabled())) break;
        await attempt(() => activate(page, "history.undo", "pointer"));
        await expect
          .poll(async () => (await shellSnapshot(page)).authoringStatus)
          .toBe("ready");
      }
      expect(
        await fingerprint(),
        "every command returns the corpus to its accepted base",
      ).toBe(base);
    };

    await checks.step("the_whole_corpus_runs_on_one_base", async () => {
      editPhase = "the_whole_corpus_runs_on_one_base";
      // GUARD: read the base's identity from the core, not from the snapshot the shell starts
      // with. That seeded snapshot carries the fixture's own placeholder fingerprint; the core
      // computes the real content fingerprint when it first accepts a mutation, so comparing a
      // correctly restored base against the seed refuses every recipe. One accepted command and
      // its inverse put the core's own identity for this state on the record.
      await selectClip("clip-0");
      await openClipMenu(page, "clip-0");
      await attempt(() => activate(page, "clip.enabled", "pointer"));
      await attempt(() => activate(page, "history.undo", "pointer"));
      await expect
        .poll(async () => (await shellSnapshot(page)).authoringStatus)
        .toBe("ready");
      const base = await fingerprint();
      for (const recipe of recipes) {
        // Each command is its own reported step: thirty-three run here, and a failure that names
        // only the loop leaves the reader to guess which command produced it.
        await test.step(recipe.operation, async () => {
          editPhase = recipe.operation;
          // GUARD: the rebase row is the one command that cannot be issued on a base the core
          // agrees with -- its control only exists after the core has refused a stale command.
          // Arm the concurrent-edit hook for exactly that recipe and disarm it immediately
          // after, or its `prepare` waits for a conflict notice that nothing will ever produce.
          conflictKind =
            recipe.operation === "conflict.rebase" ? "set_clip_enabled" : null;
          if (
            recipe.operation === "track.add" ||
            recipe.operation === "track.remove"
          ) {
            await makeStressTrackCapacity(page);
            count.value = (await shellSnapshot(page)).receipts;
          }
          const clipSlots = CLIP_CAPACITY_OPERATIONS.get(recipe.operation);
          if (clipSlots !== undefined) {
            await makeStressClipCapacity(page, clipSlots);
            count.value = (await shellSnapshot(page)).receipts;
          }
          const context = await recipe.prepare?.(page, count);
          await attempt((mark) => recipe.act(page, "pointer", context, mark));
          const postAction = await shellSnapshot(page);
          if (postAction.authoringStatus === "conflict")
            throw new Error(
              `${recipe.operation} was refused: ${JSON.stringify(oracle.lastHistory()?.rejection ?? null)}`,
            );
          await expect
            .poll(async () => (await shellSnapshot(page)).authoringStatus)
            .toBe("ready");
          conflictKind = null;
          await restoreBase(base);
        });
      }
    });

    const trimEnd = page.locator(
      `${shellSurface} [data-h3-nle-trim-edge="end"]:not([hidden])`,
    );
    const trimPhase = page.locator('[data-h3-nle-status="trim"]');

    await checks.step(
      "primary_video_geometry_edits_reach_their_floor",
      async () => {
        editPhase = "primary_video_geometry_edits";
        await selectClip("clip-0");
        for (let edit = 0; edit < 40; edit += 1) {
          // GUARD: the commit itself has to happen inside `attempt`, which counts the
          // transactions an act produced by sampling before and after it. Pressing Enter first
          // and then entering `attempt` lets the transaction land before the "before" sample is
          // taken, and the wrapper then waits for a transaction that has already arrived.
          await attempt(async (mark) => {
            await trimEnd.focus();
            await page.keyboard.press(
              edit % 2 === 0 ? "ArrowLeft" : "ArrowRight",
            );
            // The arrow above opened a keyboard draft; Enter is the edit.
            await mark();
            await page.keyboard.press("Enter");
            await expect(trimPhase).toHaveAttribute(
              "data-h3-nle-trim-phase",
              "idle",
              { timeout: 20_000 },
            );
          });
          counters.primaryGeometry += 1;
        }
      },
    );

    await checks.step(
      "edge_drags_reach_their_floor_in_both_directions",
      async () => {
        editPhase = "edge_drags";
        for (let drag = 0; drag < 20; drag += 1) {
          const box = (await trimEnd.boundingBox())!;
          const x = box.x + box.width / 2;
          const y = box.y + box.height / 2;
          const delta = drag % 2 === 0 ? -18 : 18;
          // GUARD: the drag runs inside `attempt`, which is what confirms a transaction reached
          // the core. `floor.edit_attempts` states that every attempt it counts was confirmed
          // that way, so a settled gesture that committed nothing must not be counted as one.
          await attempt(async (mark) => {
            await page.mouse.move(x, y);
            await page.mouse.down();
            await page.mouse.move(x + delta, y, { steps: 4 });
            // Everything above is the drag's live preview; releasing is the edit.
            await mark();
            await page.mouse.up();
            await expect(trimPhase).toHaveAttribute(
              "data-h3-nle-trim-phase",
              "idle",
              { timeout: 20_000 },
            );
          });
          if (delta < 0) counters.edgeDragsLeft += 1;
          else counters.edgeDragsRight += 1;
        }
      },
    );

    await checks.step(
      "move_drag_floors_include_groups_and_cross_track",
      async () => {
        editPhase = "move_drags";
        // Move clip-3 away from frame zero so clip-2 can make an admitted one-row round trip.
        await selectClip("clip-3");
        await directMove("clip-3", 72, 0);
        for (let drag = 0; drag < 8; drag += 1) {
          await selectClip("clip-1");
          await directMove("clip-1", drag % 2 === 0 ? 72 : -72, 0);
        }

        await selectClip("clip-4");
        await (
          await revealClip(page, "clip-5")
        )
          .first()
          .click({ modifiers: ["Control"] });
        await expect
          .poll(async () => (await shellSnapshot(page)).authoringStatus)
          .toBe("ready");
        for (let drag = 0; drag < 6; drag += 1) {
          await directMove("clip-4", drag % 2 === 0 ? 72 : -72, 0);
          counters.groupMoveDrags += 1;
        }

        await selectClip("clip-2");
        for (let drag = 0; drag < 5; drag += 1) {
          await directMove("clip-2", 0, drag % 2 === 0 ? 56 : -56);
          counters.crossTrackMoves += 1;
        }
        expect(counters.moveDrags).toBeGreaterThanOrEqual(20);
        expect(counters.groupMoveDrags).toBeGreaterThanOrEqual(5);
        expect(counters.crossTrackMoves).toBeGreaterThanOrEqual(5);
      },
    );

    await checks.step(
      "marquee_selection_floor_uses_completed_selections",
      async () => {
        editPhase = "marquee_selections";
        const grid = page.getByRole("grid", { name: "Timeline tracks" });
        await grid.evaluate((element) => {
          element.scrollTop = 0;
        });
        for (const row of [0, 1, 2, 3, 0]) {
          // GUARD: a virtualized row can be re-anchored by the focused clip between iterations.
          // Clear through the public click-only path first, so a repeated logical row cannot turn
          // the next completed marquee into a correct selection no-op and falsely miss the floor.
          const clearSelection = await revealTimelineToolbarAlternative(
            page,
            '[data-h3-nle-alternative="selection.clear"]',
          );
          await expect(clearSelection).toBeEnabled();
          await attempt(() => clearSelection.click());
          await closeTimelineToolbarOverflow(page);
          const lane = grid.getByRole("gridcell").nth(row);
          // IMPORTANT: virtual overscan attaches rows outside the grid's clipped viewport. Reveal
          // the lane before reading its box, or the press lands below the lanes' viewport. A
          // revealed lane is never under the corner resize grip (B-M2563-09; asserted by
          // `expectGripClearOfLanes` in helpers/nleTargets.ts), so this step fails if it is.
          await lane.scrollIntoViewIfNeeded();
          const box = (await lane.boundingBox())!;
          const y = box.y + box.height / 2;
          await attempt(async (mark) => {
            // GUARD: fixed offsets can hit a repeated clip, while the grid's far-right edge is its
            // scrollbar. Use the row's lane box and stay 30 px inside its right edge: at 1 px/frame
            // this is after the last visible stress clip and outside both hazardous hit targets.
            await page.mouse.move(box.x + box.width - 30, y);
            await page.mouse.down();
            await page.mouse.move(box.x + 2, y, { steps: 4 });
            await mark();
            await page.mouse.up();
          });
          counters.marqueeSelections += 1;
        }
        expect(counters.marqueeSelections).toBeGreaterThanOrEqual(5);
      },
    );

    await checks.step("ruler_scrub_floor_is_local", async () => {
      const sent = oracle.transactions.length;
      const playhead = playheadSlider(page);
      for (let scrub = 0; scrub < 10; scrub += 1) {
        await seekPlayhead(page, playhead, 10 + scrub);
        counters.rulerScrubs += 1;
      }
      expect(oracle.transactions).toHaveLength(sent);
      expect(counters.rulerScrubs).toBeGreaterThanOrEqual(10);
    });

    await checks.step("cancelled_drafts_reach_their_floor", async () => {
      const sent = oracle.transactions.length;
      for (let draft = 0; draft < 10; draft += 1) {
        await trimEnd.focus();
        await page.keyboard.press("ArrowLeft");
        await expect(trimPhase).toHaveAttribute(
          "data-h3-nle-trim-phase",
          "keyboard_draft",
        );
        await page.keyboard.press("Escape");
        await expect(trimPhase).toHaveAttribute(
          "data-h3-nle-trim-phase",
          "idle",
        );
        counters.cancelledDrafts += 1;
      }
      // A cancelled draft is not an edit: nothing reached the core.
      expect(oracle.transactions).toHaveLength(sent);
    });

    await checks.step("trim_undo_redo_cycles_reach_their_floor", async () => {
      editPhase = "trim_undo_redo_cycles";
      for (let cycle = 0; cycle < 10; cycle += 1) {
        await attempt(() => activate(page, "history.undo", "pointer"));
        await attempt(() => activate(page, "history.redo", "pointer"));
        counters.trimUndoRedo += 1;
        counters.undoRedo += 1;
      }
    });

    await checks.step("undo_redo_cycles_reach_their_floor", async () => {
      editPhase = "undo_redo_cycles";
      while (counters.undoRedo < 20) {
        await attempt(() => activate(page, "history.undo", "pointer"));
        await attempt(() => activate(page, "history.redo", "pointer"));
        counters.undoRedo += 1;
      }
    });

    await checks.step("source_replacements_reach_their_floor", async () => {
      editPhase = "source_replacements";
      for (let replacement = 0; replacement < 10; replacement += 1) {
        await selectClip("clip-0");
        // GUARD: each replacement names a different source start frame. Activating the control
        // ten times with the field untouched asks for the geometry the clip already has, which
        // the core has nothing to accept: the first would commit and the other nine would wait
        // for a transaction that never comes.
        await attempt(async (mark) => {
          const menu = await openClipMenu(page, "clip-0");
          await menu
            .getByRole("spinbutton", {
              name: "Source start frame",
              exact: true,
            })
            .fill(String(4 + replacement));
          // Typing the new start frame is the draft; activating Replace is the edit.
          await mark();
          await activate(page, "asset.replace", "pointer");
        });
        counters.sourceReplacements += 1;
      }
    });

    await checks.step(
      "conflicting_edits_rebase_and_are_presented",
      async () => {
        editPhase = "conflicting_edits";
        // IMPORTANT (M25-21 B3-D57): the competing edit is committed in *setup*, before the user
        // activates anything, instead of being injected into the browser's own request. Both
        // produce a genuinely stale base -- the core accepts another client's transaction and
        // advances past the revision the browser still holds -- but injecting it put a second
        // core apply inside the window this row measures, so the measurement included work that
        // no real conflict performs at that moment. Nothing here fabricates the refusal: the
        // browser is never told about the new revision, its own stale transaction still travels
        // to the core, and the core is what rejects it.
        for (let conflict = 0; conflict < 40; conflict += 1) {
          const accepted = (await shellSnapshot(page)).timelineSnapshot!;
          const base = accepted.timelineRevision;
          const concurrentEnabled = accepted.clips.find(
            (clip) => clip.clipId === CONCURRENT_CLIP,
          )!.enabled;
          const setup = await oracle.commitCompeting((template, index) => ({
            ...template,
            request_id: `concurrent-${index}`,
            transaction_id: `tx-concurrent-${index}`,
            commands: [
              {
                kind: "set_clip_enabled",
                payload: {
                  clip_id: CONCURRENT_CLIP,
                  // IMPORTANT: the global transaction index is not the clip's state parity.
                  // Always change the accepted material identity or a revision-only conflict has
                  // no distinct fingerprint and correctly offers no rebase candidate.
                  enabled: !concurrentEnabled,
                },
              },
            ],
          }));
          competingSetup.push(setup);
          // Fail closed: a competing edit the core refused leaves the browser's base fresh, and
          // the row would then measure an accepted edit while claiming to measure a refusal.
          expect(
            setup.accepted,
            `the competing edit must be accepted by the core (status ${setup.status})`,
          ).toBe(true);
          expect(
            (await shellSnapshot(page)).timelineSnapshot!.timelineRevision,
            "the browser must not have learned the new revision before it acts",
          ).toBe(base);
          // IMPORTANT (M25-21 B3-D54): the window starts at the activation and ends at the
          // presentation, which is what this measurement's method says it measures. Starting it
          // outside `attempt` instead timed the harness: `attempt` brackets the act with two
          // snapshot round trips and two `expect.poll`s whose intervals escalate to a second, so
          // a presentation that takes tens of milliseconds was recorded as thousands.
          await attempt(async (mark) => {
            // IMPORTANT (M25-21 B3-D56): the control has to be ready *before* the clock starts.
            // `activate` waits for it to be enabled, and after the previous rebase that wait can
            // be most of a second -- time the user has not spent activating anything. The
            // measurement is activation to presentation, so the wait belongs outside it.
            const menu = await openClipMenu(page, "clip-0");
            await expect(
              menu.locator('[data-h3-nle-control="clip.enabled"]'),
            ).toBeEnabled();
            // Menu opening is setup, as in the shared recipes; count the activated edit only.
            await mark();
            // The refusal is presented and stays presented: the accepted state is on screen and
            // the user decides. Waiting for `ready` here waits for an automatic recovery the
            // product deliberately does not perform -- a conflict that cleared itself would be a
            // defect, not a pass. The measurement is the presentation, and the rebase that
            // follows is the user's own command, which is what puts the surface back to `ready`.
            // The status transitions inside the window, with the millisecond each was first seen:
            // a latency of seconds against a budget of a hundred milliseconds has to say what it
            // was waiting for, and only the first few conflicts are recorded so the fact stays
            // bounded.
            const presented = await measurePresentedConflict(
              page,
              {
                controlSelector: '[data-h3-nle-control="clip.enabled"]',
                statusSelector: TIMELINE_STATUS,
                expectedRevision: base + 1,
                conflictText: CONFLICT_COPY,
              },
              () => activate(page, "clip.enabled", "pointer"),
            );
            rebasePresentationMs.push(presented.elapsedMs);
            if (rebaseTrails.length < 3)
              rebaseTrails.push([...presented.trail]);
          });
          // The core is what refused, and this is its own answer saying so. Without it the row
          // would prove only that the browser displayed a conflict notice, which a harness could
          // produce without the browser's transaction ever reaching a core.
          //
          // The closed set is the core's own stale-base codes, in the order `_check_cas` tests
          // them. Which one fires depends on the competing command: one that advances the
          // workspace revision is refused as `stale_workspace_revision` before the timeline
          // revision is ever compared. Pinning the single code this row happened to observe
          // would make it fail on a different competing edit that is just as stale.
          expect(
            ["stale_workspace_revision", "stale_timeline_revision"],
            "the browser's stale transaction must be refused by the core itself",
          ).toContain(
            (
              oracle.lastHistory()?.rejection as
                Readonly<{ code?: string }> | null | undefined
            )?.code,
          );
          await attempt(() => activate(page, "conflict.rebase", "pointer"));
          await expect
            .poll(async () => (await shellSnapshot(page)).authoringStatus)
            .toBe("ready");
          counters.conflictRebase += 1;
        }
        conflictKind = null;
      },
    );

    await checks.step("the_attempt_floor_was_reached", async () => {
      editPhase = "the_attempt_floor";
      while (counters.editAttempts < 240) {
        await selectClip("clip-0");
        await openClipMenu(page, "clip-0");
        await attempt(() => activate(page, "clip.enabled", "pointer"));
      }
      expect(counters.editAttempts).toBeGreaterThanOrEqual(240);
    });

    const wall = Date.now() - started;
    const observed = await shellSnapshot(page);
    const handlers = [...observed.handlerDurationsMs];
    await checks.step("the_handler_profiler_produced_samples", () => {
      // Fail closed: an empty sample is missing instrumentation, not a pass.
      expect(handlers.length).toBeGreaterThan(0);
      expect(rebasePresentationMs.length).toBeGreaterThanOrEqual(40);
    });
    await checks.step("the_editor_is_still_usable", async () => {
      await expect(slider).toBeEnabled();
      await expect(page.locator(MONITOR_STATUS)).toHaveText("Monitor paused.");
    });

    const evidence = hardeningEvidence(testInfo);
    const record = (
      id: string,
      value: number,
      unit: "ms" | "count",
      method: string,
      samples: number,
    ) =>
      evidence.measurement(id, {
        observed: value,
        unit,
        method,
        sample_count: samples,
        interval_ms: wall,
      });
    await record(
      "stress.handler_p95",
      p95(handlers)!,
      "ms",
      "nearest-rank p95 of the overlay's own instrumented event-handler durations",
      handlers.length,
    );
    await record(
      "stress.rebase_presentation_p95",
      p95(rebasePresentationMs)!,
      "ms",
      "activation to the session presenting the reconciled state after the core refused the stale base",
      rebasePresentationMs.length,
    );
    await record(
      "stress.nle_commits_per_edit_max",
      commitsPerEditMax,
      "count",
      "overlay Profiler commits attributed to a single accepted edit, maximum over the phase",
      counters.editAttempts,
    );
    await record(
      "floor.edit_attempts",
      counters.editAttempts,
      "count",
      "edits activated through the product's own controls, each confirmed by a transaction",
      counters.editAttempts,
    );
    await record(
      "floor.conflict_rebase",
      counters.conflictRebase,
      "count",
      "edits whose base the core had already advanced past, each presented after reconciliation",
      counters.conflictRebase,
    );
    await record(
      "floor.primary_video_geometry",
      counters.primaryGeometry,
      "count",
      "committed trims of the primary video clip's own geometry",
      counters.primaryGeometry,
    );
    await record(
      "floor.undo_redo_cycles",
      counters.undoRedo,
      "count",
      "undo followed by redo, each confirmed by its own transaction",
      counters.undoRedo,
    );
    await record(
      "floor.source_replacements",
      counters.sourceReplacements,
      "count",
      "accepted source replacements on the selected clip",
      counters.sourceReplacements,
    );
    await record(
      "floor.edge_drags",
      counters.edgeDragsLeft + counters.edgeDragsRight,
      "count",
      "pointer drags of a trim grip carried through to a settled gesture",
      counters.edgeDragsLeft + counters.edgeDragsRight,
    );
    await record(
      "floor.edge_drags_per_direction",
      Math.min(counters.edgeDragsLeft, counters.edgeDragsRight),
      "count",
      "the smaller of the leftward and rightward trim drag counts",
      2,
    );
    await record(
      "floor.cancelled_drafts",
      counters.cancelledDrafts,
      "count",
      "keyboard trim drafts cancelled with Escape, none of which reached the core",
      counters.cancelledDrafts,
    );
    await record(
      "floor.trim_undo_redo",
      counters.trimUndoRedo,
      "count",
      "undo/redo cycles run against committed trims",
      counters.trimUndoRedo,
    );
    await record(
      "floor.move_drags",
      counters.moveDrags,
      "count",
      "pointer clip moves carried through to one accepted transaction",
      counters.moveDrags,
    );
    await record(
      "floor.group_move_drags",
      counters.groupMoveDrags,
      "count",
      "accepted pointer moves whose captured selection contained more than one clip",
      counters.groupMoveDrags,
    );
    await record(
      "floor.cross_track_moves",
      counters.crossTrackMoves,
      "count",
      "accepted pointer moves whose destination track differed from the source track",
      counters.crossTrackMoves,
    );
    await record(
      "floor.marquee_selections",
      counters.marqueeSelections,
      "count",
      "background marquee releases with an observed accepted selection transaction",
      counters.marqueeSelections,
    );
    await record(
      "floor.ruler_scrubs",
      counters.rulerScrubs,
      "count",
      "ruler pointer seeks with the requested frame presented and no timeline transaction",
      counters.rulerScrubs,
    );
    await record(
      "floor.edit_phase_wall",
      wall,
      "ms",
      "wall time of the whole scheduled edit phase",
      1,
    );
    const facts = {
      ...counters,
      commits_per_edit_max: commitsPerEditMax,
      // `commit_outliers` is a capped sample; these three are the complete counts.
      commit_violations: commitViolations,
      commit_histogram: Object.fromEntries(
        [...commitHistogram].sort((left, right) => left[0] - right[0]),
      ),
      commit_violations_by_phase: Object.fromEntries(violationsByPhase),
      commit_outliers: commitOutliers,
      rebase_trails: rebaseTrails,
      // The competing edit's own cost, measured rather than assumed free, and the per-transaction
      // costs at the fixture boundary with the core's self-reported costs beside them.
      competing_setup: competingSetup.slice(0, 6),
      measured_applies: oracle.measuredApplies().slice(-12),
      handler_samples: handlers.length,
      transactions: oracle.transactions.length,
      wall_ms: wall,
    };
    for (const recipe of recipes)
      await evidence.row(`command.${recipe.kind}`, "stress", checks, facts);
    await evidence.row("command.trim_clip", "edge_trim_history", checks, facts);
  } finally {
    await oracle.close();
  }
});
