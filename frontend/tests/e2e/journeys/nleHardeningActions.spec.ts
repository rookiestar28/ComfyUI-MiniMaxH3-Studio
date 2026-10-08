// M25-21 hardening of the non-command action inventory on the REAL integrated shell: the local
// transport, the read-only embedded-audio follower and the deferred audio negative, the M25-19
// output actions and the explicit Production import. The coverage manifest names these titles.
import { resolve } from "node:path";

import { test, expect, type Locator, type Page } from "@playwright/test";

import { underForeignArrowHotkey } from "../helpers/foreignArrowHotkey";
import { openExportPanel } from "../helpers/nleExport";
import { Checks, hardeningEvidence } from "../helpers/nleHardeningEvidence";
import {
  startImportFixture,
  type ImportFixture,
  type ImportFixtureOptions,
} from "../helpers/nleImportFixture";
import { installOutputJobFixture } from "../helpers/nleOutputJobFixture";
import { openIntegratedShell, shellSnapshot } from "../helpers/nleShell";
import {
  playheadFrame,
  playheadSlider,
  seekPlayheadFraction,
} from "../helpers/nleTimeline";
import {
  audioPacketTimelineViolation,
  startProcessAudioObserver,
} from "../host/audioObserver";

test.use({
  viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 1,
  hasTouch: true,
  launchOptions: { ignoreDefaultArgs: ["--mute-audio"] },
});

const FPS = 24;
const ACTUAL_OUTPUT_IMPULSE_SAMPLE = 24_000;
const REPOSITORY_ROOT = resolve(process.cwd(), "..");
const PROCESS_AUDIO_OBSERVER = resolve(
  REPOSITORY_ROOT,
  "scripts/process_audio_observer.py",
);
const PROCESS_AUDIO_OBSERVER_SHA256 =
  // IMPORTANT: capture only the reviewed observer bytes; do not derive admission at launch.
  "b7a6eb2454d800fac0072ce445cf6f4a263aa7157cbb11207e5455be50ae815c"; // pragma: allowlist secret
const OUTPUT_REGION = { name: "Final video", exact: true } as const;
const LAUNCHER = '[data-h3-nle-entry="open"]';

/** A named control that is a hit-testable 44 x 44 target and reachable by keyboard focus. */
async function assertTarget(
  page: Page,
  target: Locator,
  checks: Checks,
  name: string,
) {
  await checks.step(`${name}_is_a_named_44px_keyboard_target`, async () => {
    if (!(await target.isVisible())) {
      const more = page.locator('[data-h3-nle-control="toolbar.more"]');
      await expect(more).toBeVisible();
      await more.click();
      await expect(target).toBeVisible();
    }
    await expect(target).toHaveAccessibleName(/\S/);
    await target.scrollIntoViewIfNeeded();
    const box = (await target.boundingBox())!;
    expect(box.width).toBeGreaterThanOrEqual(44);
    expect(box.height).toBeGreaterThanOrEqual(44);
    const reachable = await target.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      const top = document.elementFromPoint(
        rect.left + rect.width / 2,
        rect.top + rect.height / 2,
      );
      const toolbar = element.closest('[role="toolbar"]');
      const items =
        toolbar === null || !element.hasAttribute("data-h3-nle-toolbar-item")
          ? []
          : Array.from(
              toolbar.querySelectorAll<HTMLElement>(
                "[data-h3-nle-toolbar-item]",
              ),
            );
      return {
        hit: top !== null && (top === element || element.contains(top)),
        outsideInert: element.closest("[inert],[aria-hidden='true']") === null,
        direct: items.length === 0 && (element as HTMLElement).tabIndex >= 0,
        current: items.findIndex((item) => item.tabIndex === 0),
        target: items.indexOf(element as HTMLElement),
        count: items.length,
      };
    });
    expect(reachable.hit).toBe(true);
    expect(reachable.outsideInert).toBe(true);
    if (reachable.count === 0) {
      expect(reachable.direct).toBe(true);
      await target.focus();
    } else {
      // The timeline toolbar follows the APG roving-tabindex pattern: one Tab stop, then arrows.
      expect(reachable.current).toBeGreaterThanOrEqual(0);
      expect(reachable.target).toBeGreaterThanOrEqual(0);
      const toolbar = target.locator('xpath=ancestor::*[@role="toolbar"]');
      await toolbar
        .locator("[data-h3-nle-toolbar-item]")
        .nth(reachable.current)
        .focus();
      const steps =
        (reachable.target - reachable.current + reachable.count) %
        reachable.count;
      for (let step = 0; step < steps; step += 1)
        await page.keyboard.press("ArrowRight");
    }
    await expect(target).toBeFocused();
  });
}

/** Every media element the page owns, attached or held by the compositor, and its UA controls. */
function mediaControlSurfaces(page: Page) {
  return page.evaluate(() => {
    const registry = (
      window as unknown as {
        __nleMediaDebug?: {
          videoElementsByAsset: Map<string, HTMLVideoElement>;
        };
      }
    ).__nleMediaDebug;
    const elements: HTMLMediaElement[] = [
      ...document.querySelectorAll<HTMLMediaElement>("video,audio"),
      ...(registry?.videoElementsByAsset.values() ?? []),
    ];
    return {
      media: elements.length,
      withControls: elements.filter((element) => element.controls).length,
    };
  });
}

async function sliderValue(page: Page): Promise<number> {
  return playheadFrame(playheadSlider(page));
}

test("hardening a11y transport.seek steps one frame and pages one output second", async ({
  page,
}, testInfo) => {
  const started = Date.now();
  await openIntegratedShell(page, "smoke");
  const slider = playheadSlider(page);
  const lastFrame = 120 * FPS - 1;
  const checks = new Checks();
  const facts: Record<string, unknown> = {};
  await checks.step("frame_domain_slider_semantics", async () => {
    await expect(slider).toHaveAttribute("aria-valuemin", "0");
    await expect(slider).toHaveAttribute("aria-valuemax", String(lastFrame));
    await expect(slider).toHaveAttribute("aria-valuetext", "00:00:00:00");
  });
  await checks.step("slider_target_at_least_44px", async () => {
    const box = (await slider.boundingBox())!;
    facts.slider = [Math.round(box.width), Math.round(box.height)];
    expect(box.width).toBeGreaterThanOrEqual(44);
    expect(box.height).toBeGreaterThanOrEqual(44);
  });
  await slider.focus();
  await page.keyboard.press("Home");
  await expect.poll(() => sliderValue(page)).toBe(0);
  await page.keyboard.press("ArrowRight");
  await expect.poll(() => sliderValue(page)).not.toBe(0);
  const frameStep = await sliderValue(page);
  await page.keyboard.press("Home");
  await expect.poll(() => sliderValue(page)).toBe(0);
  await page.keyboard.press("PageUp");
  await expect.poll(() => sliderValue(page)).not.toBe(0);
  const pageStep = await sliderValue(page);
  const interval = Date.now() - started;
  const evidence = hardeningEvidence(testInfo);
  // The measured values are recorded before the checks judge them, so a failing run still says
  // what it observed; the report compares them with the frozen thresholds independently.
  await evidence.measurement("a11y.frame_step", {
    observed: frameStep,
    unit: "frames",
    method: "keyboard ArrowRight from Home on the focused transport slider",
    sample_count: 1,
    interval_ms: interval,
  });
  await evidence.measurement("a11y.page_step", {
    observed: pageStep,
    unit: "frames",
    method: "keyboard PageUp from Home on the focused transport slider",
    sample_count: 1,
    interval_ms: interval,
  });
  await checks.step("arrow_steps_exactly_one_frame", () => {
    expect(frameStep).toBe(1);
  });
  await checks.step("page_steps_exactly_one_output_second", () => {
    expect(pageStep).toBe(FPS);
  });
  await checks.step("rapid_page_and_arrow_steps_all_count", async () => {
    // Each press steps from the position the previous one asked for, not from the last frame
    // the monitor happened to present.
    await page.keyboard.press("Home");
    await expect.poll(() => sliderValue(page)).toBe(0);
    for (let press = 0; press < 3; press += 1)
      await page.keyboard.press("PageUp");
    await expect.poll(() => sliderValue(page)).toBe(3 * FPS);
    for (let press = 0; press < 5; press += 1)
      await page.keyboard.press("ArrowRight");
    await expect.poll(() => sliderValue(page)).toBe(3 * FPS + 5);
    for (let press = 0; press < 2; press += 1)
      await page.keyboard.press("PageDown");
    await expect.poll(() => sliderValue(page)).toBe(FPS + 5);
  });
  await checks.step("page_down_and_end_stay_in_the_frame_domain", async () => {
    await page.keyboard.press("PageDown");
    await expect.poll(() => sliderValue(page)).toBe(5);
    await page.keyboard.press("PageDown");
    await expect.poll(() => sliderValue(page)).toBe(0);
    await page.keyboard.press("End");
    await expect.poll(() => sliderValue(page)).toBe(lastFrame);
    await expect(slider).toHaveAttribute("aria-valuetext", "00:01:59:23");
  });
  await checks.step(
    "a_foreign_document_hotkey_cannot_swallow_frame_keys",
    async () => {
      // Observed on the supplied host (M26-05 runtime-18): ComfyUI-Easy-Use binds the arrows
      // through hotkeys-js on `document`, which does not exempt range inputs, and cancels their
      // default. The owned ruler must consume each step before the document sees it.
      await page.evaluate(() => {
        const scope = window as unknown as {
          __h3ForeignArrowKeys: number;
          __h3RemoveForeignArrowKeys: () => void;
        };
        scope.__h3ForeignArrowKeys = 0;
        const listener = (event: KeyboardEvent) => {
          if (!event.key.startsWith("Arrow")) return;
          scope.__h3ForeignArrowKeys += 1;
          event.preventDefault();
        };
        document.addEventListener("keydown", listener);
        scope.__h3RemoveForeignArrowKeys = () =>
          document.removeEventListener("keydown", listener);
      });
      try {
        await page.keyboard.press("Home");
        await expect.poll(() => sliderValue(page)).toBe(0);
        for (let press = 0; press < 12; press += 1)
          await page.keyboard.press("ArrowRight");
        await expect.poll(() => sliderValue(page)).toBe(12);
        await page.keyboard.press("ArrowLeft");
        await expect.poll(() => sliderValue(page)).toBe(11);
        await page.keyboard.press("End");
        await expect.poll(() => sliderValue(page)).toBe(lastFrame);
        // A frame key the slider consumed is not also delivered to a page-wide hotkey.
        expect(
          await page.evaluate(
            () =>
              (window as unknown as { __h3ForeignArrowKeys: number })
                .__h3ForeignArrowKeys,
          ),
        ).toBe(0);
      } finally {
        await page.evaluate(() =>
          (
            window as unknown as { __h3RemoveForeignArrowKeys: () => void }
          ).__h3RemoveForeignArrowKeys(),
        );
      }
    },
  );
  await checks.step("tap_seeks_by_touch", async () => {
    await seekPlayheadFraction(page, slider, 0.25, true);
    await expect.poll(() => sliderValue(page)).not.toBe(lastFrame);
    await expect(page.locator('[data-h3-nle-status="monitor"]')).toHaveText(
      "Monitor paused.",
    );
  });
  await evidence.row("action.transport.seek", "accessibility", checks, {
    ...facts,
    frame_step: frameStep,
    page_step: pageStep,
  });
});

const MONITOR_STATUS = '[data-h3-nle-status="monitor"]';
const control = (page: Page, id: string) =>
  page.locator(`[data-h3-nle-control="${id}"]`);
const zoomStatus = (page: Page) =>
  page.getByRole("region", { name: "Timeline", exact: true }).locator("output");

/**
 * A real canvas context loss and its restoration, through the two DOM events the composition
 * session listens for. The session refuses to reopen while the context is lost, so the recovery
 * control is proven against a runtime that genuinely cannot paint.
 */
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

async function toClipEditor(page: Page) {
  await page
    .getByRole("navigation", { name: "H3 Context pages" })
    .getByRole("button", { name: "Production" })
    .click();
  await page.getByRole("tab", { name: "Clip editor", exact: true }).click();
  await expect(page.locator(LAUNCHER)).toBeVisible();
}

async function openByLauncher(page: Page) {
  await page.locator(LAUNCHER).click();
  await expect(
    page.getByRole("button", { name: "Play", exact: true }),
  ).toBeEnabled();
}

test("hardening a11y transport controls are named 44px targets reachable by keyboard and touch", async ({
  page,
}, testInfo) => {
  const oracle = await openIntegratedShell(page, "smoke");
  const evidence = hardeningEvidence(testInfo);
  const status = page.locator(MONITOR_STATUS);
  const slider = playheadSlider(page);
  const rows: Record<string, Checks> = {};
  const row = (id: string) => (rows[id] ??= new Checks());

  const play = control(page, "transport.play");
  await assertTarget(page, play, row("transport.play"), "play");
  await row("transport.play").step("enter_starts_playback", async () => {
    await page.keyboard.press("Enter");
    await expect(status).toHaveText("Monitor playing.");
  });

  const pause = control(page, "transport.pause");
  await assertTarget(page, pause, row("transport.pause"), "pause");
  await row("transport.pause").step("a_tap_pauses_playback", async () => {
    await pause.tap();
    await expect(status).toHaveText("Monitor paused.");
    await expect(play).toBeEnabled();
  });

  await slider.focus();
  await page.keyboard.press("Home");
  await expect.poll(() => sliderValue(page)).toBe(0);
  const forward = control(page, "transport.step_forward");
  await assertTarget(
    page,
    forward,
    row("transport.step_forward"),
    "step_forward",
  );
  await row("transport.step_forward").step(
    "enter_steps_one_frame_forward",
    async () => {
      await page.keyboard.press("Enter");
      await expect.poll(() => sliderValue(page)).toBe(1);
      await expect(status).toHaveText("Monitor paused.");
    },
  );

  const back = control(page, "transport.step_back");
  await assertTarget(page, back, row("transport.step_back"), "step_back");
  await row("transport.step_back").step(
    "a_tap_steps_one_frame_back",
    async () => {
      await back.tap();
      await expect.poll(() => sliderValue(page)).toBe(0);
    },
  );

  const zoomIn = control(page, "transport.zoom_in");
  await expect(zoomStatus(page).first()).toHaveText("Zoom 1x");
  await assertTarget(page, zoomIn, row("transport.zoom_in"), "zoom_in");
  await row("transport.zoom_in").step("enter_zooms_in_one_level", async () => {
    await page.keyboard.press("Enter");
    await expect(zoomStatus(page).first()).toHaveText("Zoom 2x");
  });

  const zoomOut = control(page, "transport.zoom_out");
  await assertTarget(page, zoomOut, row("transport.zoom_out"), "zoom_out");
  await row("transport.zoom_out").step(
    "a_tap_zooms_out_one_level",
    async () => {
      await zoomOut.tap();
      await expect(zoomStatus(page).first()).toHaveText("Zoom 1x");
    },
  );

  const scroll = control(page, "transport.scroll");
  const scrollChecks = row("transport.scroll");
  await assertTarget(page, scroll, scrollChecks, "scroll");
  await scrollChecks.step(
    "the_view_start_is_a_bounded_frame_range",
    async () => {
      expect(Number(await scroll.getAttribute("min"))).toBe(0);
      expect(Number(await scroll.getAttribute("max"))).toBeGreaterThan(0);
    },
  );
  await scrollChecks.step("arrow_and_home_move_the_view_start", async () => {
    await page.keyboard.press("ArrowRight");
    await expect
      .poll(async () => Number(await scroll.inputValue()))
      .toBeGreaterThan(0);
    await page.keyboard.press("Home");
    await expect.poll(async () => Number(await scroll.inputValue())).toBe(0);
  });
  await scrollChecks.step(
    "a_foreign_document_hotkey_cannot_swallow_view_start_keys",
    async () => {
      // B-M2605-SEEK-02: the scroll range was left to its native step, which a page-wide arrow
      // hotkey cancels on a supplied host carrying ComfyUI-Easy-Use.
      const step = Number(await scroll.getAttribute("step"));
      const last =
        Math.floor(Number(await scroll.getAttribute("max")) / step) * step;
      const reached = await underForeignArrowHotkey(page, async () => {
        await scroll.focus();
        await page.keyboard.press("Home");
        await expect
          .poll(async () => Number(await scroll.inputValue()))
          .toBe(0);
        await page.keyboard.press("ArrowRight");
        await page.keyboard.press("ArrowRight");
        await expect
          .poll(async () => Number(await scroll.inputValue()))
          .toBe(Math.min(last, 2 * step));
        await page.keyboard.press("ArrowLeft");
        await expect
          .poll(async () => Number(await scroll.inputValue()))
          .toBe(Math.min(last, 2 * step) - step);
        await page.keyboard.press("Home");
        await expect
          .poll(async () => Number(await scroll.inputValue()))
          .toBe(0);
      });
      // A range key the control consumed is not also delivered to the page-wide hotkey.
      expect(reached).toBe(0);
    },
  );

  const recover = control(page, "transport.recover");
  const recoverChecks = row("transport.recover");
  await recoverChecks.step(
    "recovery_is_offered_only_when_blocked",
    async () => {
      // M25-45: recovery is offered inside the picture in the recoverable state and is absent
      // otherwise, rather than standing permanently disabled in the transport row.
      await expect(recover).toHaveCount(0);
      await loseCanvasContext(page, true);
      await expect(status).toHaveText(
        "Monitor unavailable: canvas unavailable.",
      );
      for (const blocked of [play, forward, back])
        await expect(blocked).toBeDisabled();
      // Decoder availability does not own logical authoring navigation.
      await expect(slider).toBeEnabled();
      const blockedFrame = await sliderValue(page);
      await slider.focus();
      await page.keyboard.press("ArrowRight");
      await expect.poll(() => sliderValue(page)).toBe(blockedFrame + 1);
      await expect(status).toHaveText(
        "Monitor unavailable: canvas unavailable.",
      );
      // One play/pause control, named for the action it performs: a blocked monitor is not
      // playing, so the pause half is not in the DOM at all.
      await expect(pause).toHaveCount(0);
    },
  );
  await assertTarget(page, recover, recoverChecks, "recover");
  await recoverChecks.step("enter_reopens_the_monitor_paused", async () => {
    await loseCanvasContext(page, false);
    await page.keyboard.press("Enter");
    await expect(status).toHaveText("Monitor paused.", { timeout: 10_000 });
    await expect(play).toBeEnabled();
    await expect(recover).toHaveCount(0);
  });

  const snapshot = await shellSnapshot(page);
  const facts = {
    transactions: oracle.transactions.length,
    render_jobs: snapshot.renderJobRequests,
    queued: snapshot.queuedPrompts,
  };
  expect(facts).toEqual({ transactions: 0, render_jobs: 0, queued: 0 });
  for (const [id, checks] of Object.entries(rows))
    await evidence.row(`action.${id}`, "accessibility", checks, facts);
});

test("hardening recovery transport restores its position paused after a remount and a blocked monitor recovers", async ({
  page,
}, testInfo) => {
  const oracle = await openIntegratedShell(page, "smoke", {
    extraParams: { viewDestroy: "1" },
    beforeOpen: toClipEditor,
    open: openByLauncher,
  });
  const status = page.locator(MONITOR_STATUS);
  const slider = playheadSlider(page);
  const checks = new Checks();
  await slider.focus();
  await page.keyboard.press("Home");
  for (let press = 0; press < 5; press += 1)
    await page.keyboard.press("PageUp");
  await expect.poll(() => sliderValue(page)).toBe(5 * FPS);
  await control(page, "transport.zoom_in").click();
  await expect(zoomStatus(page).first()).toHaveText("Zoom 2x");
  await control(page, "transport.play").click();
  await expect(status).toHaveText("Monitor playing.");
  await checks.step("a_remount_restores_the_position_paused", async () => {
    await page.evaluate(() => window.nleShellHarness.destroyView());
    await expect(page.locator("[data-h3-nle-root]")).toHaveCount(0);
    await page.evaluate(() => window.nleShellHarness.renderView());
    // A remount never reopens the editor by itself; the explicit Open restores it.
    await expect(page.locator("[data-h3-nle-root]")).toHaveCount(0);
    await openByLauncher(page);
    await expect(status).toHaveText("Monitor paused.");
    expect(await sliderValue(page)).toBe(5 * FPS);
    await expect(zoomStatus(page).first()).toHaveText("Zoom 2x");
  });
  await checks.step(
    "a_lost_canvas_blocks_playback_and_preserves_logical_navigation",
    async () => {
      await loseCanvasContext(page, true);
      await expect(status).toHaveText(
        "Monitor unavailable: canvas unavailable.",
      );
      for (const id of [
        "transport.play",
        "transport.step_back",
        "transport.step_forward",
      ])
        await expect(control(page, id)).toBeDisabled();
      await expect(slider).toBeEnabled();
      await slider.focus();
      await page.keyboard.press("ArrowRight");
      await expect.poll(() => sliderValue(page)).toBe(5 * FPS + 1);
      await expect(status).toHaveText(
        "Monitor unavailable: canvas unavailable.",
      );
      await expect(control(page, "transport.pause")).toHaveCount(0);
      await expect(control(page, "transport.recover")).toBeEnabled();
    },
  );
  let recoveredFrame = -1;
  await checks.step("recovery_reopens_the_monitor_paused", async () => {
    await loseCanvasContext(page, false);
    await control(page, "transport.recover").click();
    await expect(status).toHaveText("Monitor paused.", { timeout: 10_000 });
    await expect(control(page, "transport.play")).toBeEnabled();
    await expect(slider).toBeEnabled();
    recoveredFrame = await sliderValue(page);
    expect(recoveredFrame).toBe(5 * FPS + 1);
    // The view settings are the view's own retained state and survive the runtime's restart.
    await expect(zoomStatus(page).first()).toHaveText("Zoom 2x");
  });
  await checks.step("the_recovered_monitor_seeks_again", async () => {
    await slider.focus();
    await page.keyboard.press("PageUp");
    await expect.poll(() => sliderValue(page)).toBe(recoveredFrame + FPS);
  });
  const snapshot = await shellSnapshot(page);
  const facts = {
    injection: "native view destroy and remount, then a lost canvas context",
    transactions: oracle.transactions.length,
    render_jobs: snapshot.renderJobRequests,
    queued: snapshot.queuedPrompts,
    restored_frame: 5 * FPS,
    frame_after_recovery: recoveredFrame,
  };
  await checks.step("nothing_was_replayed", () => {
    expect({
      transactions: facts.transactions,
      render_jobs: facts.render_jobs,
      queued: facts.queued,
    }).toEqual({ transactions: 0, render_jobs: 0, queued: 0 });
  });
  const evidence = hardeningEvidence(testInfo);
  for (const id of [
    "transport.play",
    "transport.pause",
    "transport.step_back",
    "transport.step_forward",
    "transport.seek",
    "transport.recover",
    "transport.zoom_in",
    "transport.zoom_out",
    "transport.scroll",
  ])
    await evidence.row(`action.${id}`, "recovery", checks, facts);
});

const AUDIO = '[data-h3-nle-status="audio"]';
const AUDIO_POLICY =
  "Embedded source audio follows primary video edits. A clip's volume, mute and fades are set in its Audio tab; separate audio tracks are unavailable in this release.";
/** Anything an audio editor would expose: mixing, level, routing or a second timeline. */
const AUDIO_CONTROL_WORDS =
  /audio|volume|mute|gain|sound|mix|level|track gain/i;
/**
 * Exact labels that match a word above without naming an audio control. IMPORTANT: pin each one by
 * its full label; never switch the pattern to word boundaries, which stops matching "Unmute".
 * "Check again" is the Media tools card's recheck action (its "again" contains "gain").
 */
const NON_AUDIO_LABELS: readonly string[] = ["Check again"];

type ApiUse = { worker: number; audioContext: number; webgl: number };

/**
 * Counts bounded runtime APIs from before the first script. Independent audio editing remains
 * deferred, while the primary embedded follower owns one AudioContext per live monitor lifecycle.
 */
async function countRuntimeApis(page: Page) {
  await page.addInitScript(() => {
    const scope = window as unknown as { __h3ApiUse: ApiUse };
    scope.__h3ApiUse = { worker: 0, audioContext: 0, webgl: 0 };
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
  page.evaluate(() => (window as unknown as { __h3ApiUse: ApiUse }).__h3ApiUse);

const audioState = (page: Page) =>
  page.locator(AUDIO).evaluate((element) => ({
    state: element.getAttribute("data-h3-nle-audio-state"),
    reason: element.getAttribute("data-h3-nle-audio-reason"),
  }));

/** Hides and shows the document through the accessors and event the follower listens to. */
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

test("hardening a11y audio follower is announced read-only and no audio control takes focus", async ({
  page,
}, testInfo) => {
  await countRuntimeApis(page);
  const oracle = await openIntegratedShell(page, "smoke");
  const follow = new Checks();
  const deferred = new Checks();
  const region = page.locator(AUDIO);
  await follow.step("the_follower_is_a_read_only_announcement", async () => {
    // `aria-live="off"`: the state tracks the transport the user is already driving, so it is
    // readable on demand and never interrupts a screen reader mid-edit.
    await expect(region).toHaveAttribute("aria-live", "off");
    await expect(region).toContainText("Suspended");
    await expect(region).toContainText("Owner clip clip-0");
    await expect(region).toContainText(AUDIO_POLICY);
    expect(await audioState(page)).toEqual({
      state: "suspended",
      reason: "paused",
    });
  });
  await follow.step("the_announcement_follows_the_transport", async () => {
    // The fixture source runs two output seconds, so the window between Play and Pause is held
    // to the two round trips the transition itself needs; the monitor's own status text is the
    // transport rows' subject rather than this one's.
    await control(page, "transport.play").click();
    await expect
      .poll(() => audioState(page))
      .toEqual({
        state: "following",
        reason: "none",
      });
    await control(page, "transport.pause").click();
    await expect
      .poll(() => audioState(page))
      .toEqual({
        state: "suspended",
        reason: "paused",
      });
    await expect(region).toContainText("Suspended");
  });
  await follow.step("the_follower_holds_no_focusable_element", async () => {
    expect(
      await region.evaluate(
        (element) =>
          element.querySelectorAll(
            "button,input,select,textarea,a[href],[tabindex]",
          ).length,
      ),
    ).toBe(0);
    expect(await region.getAttribute("tabindex")).toBeNull();
  });
  const visited: string[] = [];
  await follow.step("a_full_tab_ring_never_enters_the_follower", async () => {
    await page.locator('[data-h3-nle-surface="overlay_v1"] h2').focus();
    for (let step = 0; step < 60; step += 1) {
      await page.keyboard.press("Tab");
      const at = await page.evaluate(() => {
        const active = document.activeElement as HTMLElement | null;
        if (active === null) return null;
        return {
          key:
            active.getAttribute("data-h3-nle-control") ??
            active.getAttribute("aria-label") ??
            active.tagName.toLowerCase(),
          inAudio: active.closest('[data-h3-nle-status="audio"]') !== null,
        };
      });
      expect(at).not.toBeNull();
      expect(at!.inAudio).toBe(false);
      visited.push(at!.key);
      if (visited.length > 1 && visited.indexOf(at!.key) < visited.length - 1)
        break;
    }
    // The ring closed on a control already visited, so the sweep covered every tab stop the
    // dialog offers rather than the first sixty of an open-ended order.
    expect(new Set(visited).size).toBeLessThan(visited.length);
  });
  const apis = await apiUse(page);
  await deferred.step("no_audio_control_exists_to_take_focus", async () => {
    const labelled = await page.evaluate(
      ({ words, exempt }) =>
        [
          ...document.querySelectorAll<HTMLElement>(
            "[data-h3-nle-root] button,[data-h3-nle-root] input,[data-h3-nle-root] select,[data-h3-nle-root] textarea",
          ),
        ]
          .map(
            (element) =>
              `${element.getAttribute("aria-label") ?? element.textContent?.trim() ?? ""}`,
          )
          .filter(
            (label) =>
              new RegExp(words, "i").test(label) && !exempt.includes(label),
          ),
      { words: AUDIO_CONTROL_WORDS.source, exempt: NON_AUDIO_LABELS },
    );
    expect(labelled).toEqual([]);
    await expect(page.locator("audio")).toHaveCount(0);
    await expect(page.locator("video[controls]")).toHaveCount(0);
  });
  await deferred.step(
    "one_primary_audio_context_and_no_worker_was_created",
    () => {
      expect({ worker: apis.worker, audioContext: apis.audioContext }).toEqual({
        worker: 0,
        audioContext: 1,
      });
    },
  );
  await deferred.step("the_policy_states_the_deferral_once", async () => {
    expect(
      await page.evaluate(
        (policy) =>
          [...document.querySelectorAll("[data-h3-nle-root] *")].filter(
            (element) =>
              element.children.length === 0 && element.textContent === policy,
          ).length,
        AUDIO_POLICY,
      ),
    ).toBe(1);
  });
  const evidence = hardeningEvidence(testInfo);
  const facts = {
    tab_stops: visited.length,
    transactions: oracle.transactions.length,
    ...apis,
  };
  await evidence.row(
    "action.audio.embedded_primary_follow",
    "accessibility",
    follow,
    facts,
  );
  await evidence.row("action.audio.deferred", "accessibility", deferred, facts);
});

test("hardening recovery audio follower suspends on hide and follows the accepted primary video again", async ({
  page,
}, testInfo) => {
  await countRuntimeApis(page);
  const oracle = await openIntegratedShell(page, "smoke");
  const status = page.locator(MONITOR_STATUS);
  const follow = new Checks();
  const deferred = new Checks();
  await control(page, "transport.play").click();
  await expect(status).toHaveText("Monitor playing.");
  await expect
    .poll(() => audioState(page))
    .toEqual({
      state: "following",
      reason: "none",
    });
  await follow.step("a_hidden_document_suspends_the_follower", async () => {
    await setDocumentHidden(page, true);
    await expect
      .poll(() => audioState(page))
      .toEqual({
        state: "suspended",
        reason: "suspended",
      });
    // Nothing is left playing in the background: every media element the page holds is paused.
    expect(
      await page.evaluate(
        () =>
          [
            ...document.querySelectorAll<HTMLMediaElement>("video,audio"),
          ].filter((element) => !element.paused).length,
      ),
    ).toBe(0);
  });
  await follow.step("a_visible_document_never_resumes_by_itself", async () => {
    await setDocumentHidden(page, false);
    await page.waitForTimeout(750);
    expect(await audioState(page)).toEqual({
      state: "suspended",
      reason: "suspended",
    });
    await expect(control(page, "transport.play")).toBeDisabled();
    await expect(control(page, "transport.recover")).toBeEnabled();
  });
  await follow.step("recovery_follows_the_accepted_primary_again", async () => {
    await control(page, "transport.recover").click();
    await expect(status).toHaveText("Monitor paused.", { timeout: 10_000 });
    expect(await audioState(page)).toEqual({
      state: "suspended",
      reason: "paused",
    });
    await control(page, "transport.play").click();
    await expect(status).toHaveText("Monitor playing.");
    await expect
      .poll(() => audioState(page))
      .toEqual({
        state: "following",
        reason: "none",
      });
    await expect(page.locator(AUDIO)).toContainText("Owner clip clip-0");
  });
  const apis = await apiUse(page);
  await deferred.step(
    "the_bounded_primary_owner_survives_the_whole_cycle",
    async () => {
      expect(apis.worker).toBe(0);
      expect(apis.audioContext).toBeGreaterThanOrEqual(1);
      expect(apis.audioContext).toBeLessThanOrEqual(2);
      await expect(page.locator("audio")).toHaveCount(0);
      await expect(page.locator("video[controls]")).toHaveCount(0);
      await expect(page.locator(AUDIO)).toContainText(AUDIO_POLICY);
    },
  );
  const snapshot = await shellSnapshot(page);
  const facts = {
    injection:
      "document hidden then visible, then an explicit monitor recovery",
    transactions: oracle.transactions.length,
    render_jobs: snapshot.renderJobRequests,
    ...apis,
  };
  await deferred.step("nothing_was_replayed", () => {
    expect({
      transactions: facts.transactions,
      render_jobs: facts.render_jobs,
    }).toEqual({ transactions: 0, render_jobs: 0 });
  });
  const evidence = hardeningEvidence(testInfo);
  await evidence.row(
    "action.audio.embedded_primary_follow",
    "recovery",
    follow,
    facts,
  );
  await evidence.row("action.audio.deferred", "recovery", deferred, facts);
});

test("M25-58 true editor emits continuous bounded AudioContext output while native video stays muted", async ({
  context,
  page,
}, testInfo) => {
  test.skip(
    process.platform !== "win32",
    "the pinned process-loopback observer is Windows-only",
  );
  await openIntegratedShell(page, "smoke");
  const browser = context.browser();
  if (!browser) throw new Error("owned Chromium browser required");
  const cdp = await browser.newBrowserCDPSession();
  let browserPid: number | undefined;
  try {
    const processes = await cdp.send("SystemInfo.getProcessInfo");
    browserPid = processes.processInfo.find(
      (process) => process.type === "browser",
    )?.id;
  } finally {
    await cdp.detach();
  }
  if (!Number.isInteger(browserPid) || !browserPid || browserPid < 1)
    throw new Error("owned Chromium browser process identity missing");
  await page.evaluate(() => {
    const scope = window as unknown as {
      __nleMediaDebug?: {
        videoElementsByAsset: Map<string, HTMLVideoElement>;
      };
      __m25_58Frames?: Array<{ mediaTime: number; time: number }>;
    };
    const video = [
      ...(scope.__nleMediaDebug?.videoElementsByAsset.values() ?? []),
    ][0];
    if (!video) throw new Error("primary native video unavailable");
    const rows: Array<{ mediaTime: number; time: number }> = [];
    scope.__m25_58Frames = rows;
    const sample = (_now: number, metadata: VideoFrameCallbackMetadata) => {
      if (rows.length < 256)
        rows.push({
          mediaTime: metadata.mediaTime,
          time: performance.timeOrigin + metadata.expectedDisplayTime,
        });
      if (rows.length < 256) video.requestVideoFrameCallback(sample);
    };
    video.requestVideoFrameCallback(sample);
  });
  const observer = await startProcessAudioObserver(
    REPOSITORY_ROOT,
    browserPid,
    PROCESS_AUDIO_OBSERVER,
    PROCESS_AUDIO_OBSERVER_SHA256,
  );
  let capture: Awaited<ReturnType<typeof observer.stop>> | undefined;
  try {
    const playEpoch = await page.evaluate(
      () => performance.timeOrigin + performance.now(),
    );
    await control(page, "transport.play").click();
    await expect
      .poll(() => audioState(page))
      .toEqual({ state: "following", reason: "none" });
    // Keep the observation comfortably inside the two-second fixture. The impulse sits at 0.5s;
    // 1.1s still covers it plus more than the required twenty tone windows without racing the
    // endpoint auto-pause on a loaded Windows host.
    await page.waitForTimeout(1_100);
    const pause = control(page, "transport.pause");
    if (await pause.isVisible()) await pause.click();
    else await expect(control(page, "transport.play")).toBeVisible();
    await page.waitForTimeout(350);
    capture = await observer.stop();
    const frames = await page.evaluate(
      () =>
        (
          window as unknown as {
            __m25_58Frames?: Array<{ mediaTime: number; time: number }>;
          }
        ).__m25_58Frames ?? [],
    );
    const toneWindows = capture.metrics.envelopes.filter(
      (row) =>
        row.channel_rms.every((rms) => rms >= 0.01 && rms <= 0.2) &&
        row.channel_frequency_hz.every(
          (frequency) => Math.abs(frequency - 440) <= 5,
        ),
    );
    expect(toneWindows.length).toBeGreaterThanOrEqual(20);
    const firstTone = toneWindows[0]!.start_sample;
    const lastTone =
      toneWindows.at(-1)!.start_sample + toneWindows.at(-1)!.sample_count;
    const interiorSilence = capture.metrics.silence_runs.flatMap((run) => {
      const start = Math.max(run.start_sample, firstTone);
      const end = Math.min(run.start_sample + run.duration_samples, lastTone);
      return end > start
        ? [
            {
              channel: run.channel,
              start_sample: start,
              duration_samples: end - start,
            },
          ]
        : [];
    });
    await testInfo.attach("m25-58-actual-output-metrics", {
      contentType: "application/json",
      body: Buffer.from(
        JSON.stringify({
          toneWindows: toneWindows.length,
          firstTone,
          lastTone,
          interiorSilence,
          impulseEvents: capture.metrics.impulse_events,
          packetTimeline: capture.packetTimeline,
          onsets: capture.onsets,
          frames: frames.slice(0, 64),
        }),
      ),
    });
    const maxInteriorSilenceSamples = Math.max(
      0,
      ...interiorSilence.map((run) => run.duration_samples),
    );
    expect(
      maxInteriorSilenceSamples,
      JSON.stringify({
        firstTone,
        lastTone,
        toneWindows: toneWindows.length,
        interiorSilence,
        onsets: capture.onsets,
        packetTimeline: capture.packetTimeline,
      }),
    ).toBeLessThan(960);
    for (const channel of [0, 1] as const)
      expect(
        capture.metrics.impulse_events.filter(
          (event) => event.channel === channel,
        ),
      ).toHaveLength(1);
    expect(audioPacketTimelineViolation(capture)).toBeNull();
    expect(capture.rawAudioRetained || capture.microphoneOpened).toBe(false);
    expect(
      await page.evaluate(() => {
        const videos = (
          window as unknown as {
            __nleMediaDebug?: {
              videoElementsByAsset: Map<string, HTMLVideoElement>;
            };
          }
        ).__nleMediaDebug?.videoElementsByAsset.values();
        return [...(videos ?? [])].every((video) => video.muted);
      }),
    ).toBe(true);
    const onset = capture.onsets[0];
    const firstFrame = frames.find(
      (frame) => frame.time >= playEpoch && frame.mediaTime <= 0.2,
    );
    if (!onset || !firstFrame)
      throw new Error("audio/video onset correlation unavailable");
    // The observer's onset threshold intentionally selects the high-amplitude fixture impulse,
    // not the quiet 440 Hz bed. Convert that known 24,000-sample marker back to audio time zero
    // before comparing it with the native video's media-time-zero estimate.
    const audioZeroTime =
      onset.time - (ACTUAL_OUTPUT_IMPULSE_SAMPLE / 48_000) * 1_000;
    const videoZeroTime = firstFrame.time - firstFrame.mediaTime * 1_000;
    const driftSamples = Math.round((audioZeroTime - videoZeroTime) * 48);
    expect(
      Math.abs(driftSamples),
      JSON.stringify({
        driftSamples,
        playEpoch,
        onset,
        audioZeroTime,
        videoZeroTime,
        firstFrame,
        frames: frames.slice(0, 16),
      }),
    ).toBeLessThanOrEqual(2_000);
    await testInfo.attach("m25-58-actual-output", {
      contentType: "application/json",
      body: Buffer.from(
        JSON.stringify({
          schema: "h3.context.m25_58.actual_output.v1",
          selector: capture.selector,
          browserExecutableSha256: capture.browserExecutableSha256,
          toneWindows: toneWindows.length,
          maxInteriorSilenceSamples,
          impulseEvents: capture.metrics.impulse_events,
          driftSamples,
          packetTimeline: capture.packetTimeline,
          rawAudioRetained: capture.rawAudioRetained,
          microphoneOpened: capture.microphoneOpened,
        }),
      ),
    });
  } finally {
    if (capture === undefined) await observer.stop().catch(() => undefined);
    const close = page.locator('[data-h3-nle-action="close"]');
    if (await close.isVisible()) {
      await close.click();
      await expect(
        page.locator('[data-h3-nle-surface="overlay_v1"]'),
      ).toHaveCount(0);
    }
  }
});

test("hardening a11y output actions are named 44px targets with keyboard paths and an extension-owned preview", async ({
  page,
}, testInfo) => {
  const fixture = await installOutputJobFixture(page);
  await openIntegratedShell(page, "smoke", {
    render: true,
    extraParams: { outputJob: "1" },
  });
  const evidence = hardeningEvidence(testInfo);
  await openExportPanel(page);
  const region = page.getByRole("region", OUTPUT_REGION);
  await expect(region).toBeVisible();
  const phase = region.getByRole("status").first();

  const render = region.getByRole("button", { name: "Render final video" });
  const renderChecks = new Checks();
  await assertTarget(page, render, renderChecks, "render");
  await renderChecks.step("enter_creates_exactly_one_job", async () => {
    await page.keyboard.press("Enter");
    await expect.poll(() => fixture.counts.create).toBe(1);
    await expect(phase).toHaveText("Queued");
    expect(fixture.running()).toBe(1);
    expect(fixture.jobs()).toHaveLength(1);
  });
  await renderChecks.step(
    "a_running_job_disables_a_second_render",
    async () => {
      await expect(render).toBeDisabled();
      expect(fixture.counts.create).toBe(1);
    },
  );
  await evidence.row("action.output.render", "accessibility", renderChecks, {
    jobs: fixture.jobs().length,
    via: "keyboard",
  });

  const refresh = region.getByRole("button", { name: "Refresh status" });
  const statusChecks = new Checks();
  await statusChecks.step(
    "the_phase_is_announced_in_a_live_region",
    async () => {
      fixture.advance("rendering");
      await expect(phase).toHaveText("Rendering", { timeout: 10_000 });
      const progress = region.getByRole("progressbar", { name: "Rendering" });
      expect(
        await progress.evaluate((element: HTMLProgressElement) => [
          element.value,
          element.max,
        ]),
      ).toEqual([5000, 10000]);
    },
  );
  await assertTarget(page, refresh, statusChecks, "refresh");
  await statusChecks.step(
    "enter_reads_the_status_through_the_real_client",
    async () => {
      // Held replies make the explicit read observable on its own: the background poll cannot
      // account for a request that leaves the leaf busy until this fixture releases it.
      const release = fixture.hold();
      const before = fixture.counts.status;
      await page.keyboard.press("Enter");
      await expect(refresh).toBeDisabled();
      await expect.poll(() => fixture.counts.status).toBeGreaterThan(before);
      expect(fixture.peakStatusClients()).toBeLessThanOrEqual(2);
      release();
      await expect(refresh).toBeEnabled();
      expect(fixture.counts.create).toBe(1);
      expect(fixture.counts.cancel).toBe(0);
    },
  );
  await evidence.row("action.output.status", "accessibility", statusChecks, {
    peak_status_clients: fixture.peakStatusClients(),
    via: "keyboard",
  });

  const cancel = region.getByRole("button", { name: "Cancel render" });
  const cancelChecks = new Checks();
  await assertTarget(page, cancel, cancelChecks, "cancel");
  await cancelChecks.step("a_tap_cancels_the_running_job_once", async () => {
    await cancel.tap();
    await expect.poll(() => fixture.counts.cancel).toBe(1);
    await expect(phase).toHaveText("Render cancelled");
    expect(fixture.running()).toBe(0);
  });
  await cancelChecks.step("a_cancelled_job_offers_no_cancel", async () => {
    await expect(cancel).toHaveCount(0);
    await expect(render).toBeEnabled();
    expect(fixture.counts.cancel).toBe(1);
  });
  await evidence.row("action.output.cancel", "accessibility", cancelChecks, {
    cancels: fixture.counts.cancel,
    via: "touch",
  });

  await render.click();
  await expect.poll(() => fixture.counts.create).toBe(2);
  fixture.advance("succeeded");
  await expect(phase).toHaveText("Video ready", { timeout: 10_000 });

  const preview = region.getByRole("button", { name: "Preview output" });
  const player = region.locator("video");
  const transport = region.getByRole("group", {
    name: "Preview playback controls",
    exact: true,
  });
  const play = transport.locator(
    '[data-h3-output-control="preview.play_pause"]',
  );
  const seek = transport.locator('[data-h3-output-control="preview.seek"]');
  const previewChecks = new Checks();
  await assertTarget(page, preview, previewChecks, "preview");
  await previewChecks.step("enter_opens_the_owned_preview", async () => {
    await page.keyboard.press("Enter");
    await expect(player).toHaveAttribute("src", /^blob:/);
    await expect(player).toHaveAttribute("aria-label", "Final video preview");
    expect(fixture.counts.preview).toBe(1);
  });
  await previewChecks.step("no_media_element_exposes_ua_controls", async () => {
    const surfaces = await mediaControlSurfaces(page);
    expect(surfaces.media).toBeGreaterThan(0);
    expect(surfaces.withControls).toBe(0);
  });
  await assertTarget(page, play, previewChecks, "preview_play");
  await assertTarget(page, seek, previewChecks, "preview_seek");
  await previewChecks.step("the_owned_transport_plays_and_pauses", async () => {
    // A test-side clock only. The fixture clip runs two seconds and nothing in the product reads
    // or exposes playbackRate; slowing it keeps `ended` from racing these assertions.
    await player.evaluate((element: HTMLVideoElement) => {
      element.playbackRate = 0.25;
    });
    await expect(play).toHaveAccessibleName("Play preview");
    await play.click();
    await expect(play).toHaveAccessibleName("Pause preview");
    await expect
      .poll(() =>
        player.evaluate((element: HTMLVideoElement) => element.currentTime),
      )
      .toBeGreaterThan(0.05);
    await play.click();
    await expect(play).toHaveAccessibleName("Play preview");
    const stopped = await player.evaluate((element: HTMLVideoElement) => ({
      paused: element.paused,
      at: element.currentTime,
    }));
    expect(stopped.paused).toBe(true);
    await page.waitForTimeout(300);
    expect(
      await player.evaluate((element: HTMLVideoElement) => element.currentTime),
    ).toBe(stopped.at);
  });
  await previewChecks.step(
    "the_owned_transport_seeks_by_keyboard",
    async () => {
      await seek.focus();
      await page.keyboard.press("Home");
      await expect.poll(async () => Number(await seek.inputValue())).toBe(0);
      await expect
        .poll(() =>
          player.evaluate((element: HTMLVideoElement) => element.currentTime),
        )
        .toBe(0);
      await page.keyboard.press("ArrowRight");
      await expect
        .poll(() =>
          player.evaluate((element: HTMLVideoElement) => element.currentTime),
        )
        .toBeGreaterThan(0);
      expect(Number(await seek.inputValue())).toBeGreaterThan(0);
      await expect(seek).toHaveAttribute(
        "aria-valuetext",
        /^Second 0\.[1-9] of 2\.0$/,
      );
      expect(
        await player.evaluate((element: HTMLVideoElement) => element.paused),
      ).toBe(true);
    },
  );
  await previewChecks.step(
    "a_foreign_document_hotkey_cannot_swallow_preview_seek_keys",
    async () => {
      // B-M2605-SEEK-02: the preview seek was left to its native step, which a page-wide arrow
      // hotkey cancels on a supplied host carrying ComfyUI-Easy-Use.
      const reached = await underForeignArrowHotkey(page, async () => {
        await seek.focus();
        await page.keyboard.press("Home");
        await expect.poll(async () => Number(await seek.inputValue())).toBe(0);
        await page.keyboard.press("ArrowRight");
        await page.keyboard.press("ArrowRight");
        await expect
          .poll(async () => Number(await seek.inputValue()))
          .toBeCloseTo(0.2, 6);
        await expect
          .poll(() =>
            player.evaluate((element: HTMLVideoElement) => element.currentTime),
          )
          .toBeGreaterThan(0.1);
      });
      expect(reached).toBe(0);
    },
  );
  await previewChecks.step(
    "close_returns_focus_to_the_preview_button",
    async () => {
      await region.getByRole("button", { name: "Close preview" }).click();
      await expect(player).toHaveCount(0);
      await expect(preview).toBeFocused();
    },
  );
  await evidence.row(
    "action.output.final_preview",
    "accessibility",
    previewChecks,
    { previews: fixture.counts.preview, via: "keyboard and pointer" },
  );

  const download = region.getByRole("link", { name: "Download original" });
  const downloadChecks = new Checks();
  await assertTarget(page, download, downloadChecks, "download");
  await downloadChecks.step("the_link_declares_a_bounded_save", async () => {
    await expect(download).toHaveAttribute("download", "authoring-final.mp4");
    await expect(download).toHaveAttribute("referrerpolicy", "no-referrer");
    await expect(download).toHaveAttribute(
      "href",
      /\/authoring\/output\/aro_[A-Za-z0-9_-]{22}\/download\?workspace_handle=/,
    );
  });
  await downloadChecks.step("enter_starts_the_native_download", async () => {
    const started = page.waitForEvent("download");
    await page.keyboard.press("Enter");
    const file = await started;
    expect(file.suggestedFilename()).toBe("authoring-final.mp4");
    expect(await file.failure()).toBeNull();
    await file.delete();
  });
  await evidence.row(
    "action.output.download",
    "accessibility",
    downloadChecks,
    {
      native_download: true,
      via: "keyboard",
    },
  );
});

test("hardening recovery output status loss and remount never replay a render", async ({
  page,
}, testInfo) => {
  const fixture = await installOutputJobFixture(page);
  await openIntegratedShell(page, "smoke", {
    render: true,
    extraParams: { outputJob: "1", viewDestroy: "1" },
    beforeOpen: toClipEditor,
    open: openByLauncher,
  });
  const region = () => page.getByRole("region", OUTPUT_REGION);
  await openExportPanel(page);
  await expect(region()).toBeVisible();
  await region().getByRole("button", { name: "Render final video" }).click();
  await expect.poll(() => fixture.counts.create).toBe(1);
  fixture.advance("rendering");
  await expect(region().getByRole("status").first()).toHaveText("Rendering", {
    timeout: 10_000,
  });
  const checks = new Checks();
  await checks.step("held_status_reads_never_accumulate_clients", async () => {
    const release = fixture.hold();
    const before = fixture.counts.status;
    await page.waitForTimeout(2_500);
    // One read is in flight for the whole loss window: the poll never fans out while its
    // predecessor is unanswered, and no action is retried behind it.
    expect(fixture.counts.status - before).toBeLessThanOrEqual(1);
    expect(fixture.peakStatusClients()).toBeLessThanOrEqual(2);
    expect(fixture.counts.create).toBe(1);
    expect(fixture.counts.cancel).toBe(0);
    release();
  });
  await checks.step("the_job_survives_the_status_loss", async () => {
    fixture.advance("succeeded");
    await expect(region().getByRole("status").first()).toHaveText(
      "Video ready",
      { timeout: 15_000 },
    );
    expect(fixture.counts.create).toBe(1);
    expect(fixture.jobs()).toHaveLength(1);
  });
  const beforeRemount = { ...fixture.counts };
  await checks.step("a_view_destroy_and_remount_replay_no_render", async () => {
    await page.evaluate(() => window.nleShellHarness.destroyView());
    await expect(page.locator("[data-h3-nle-root]")).toHaveCount(0);
    await page.waitForTimeout(1_500);
    await page.evaluate(() => window.nleShellHarness.renderView());
    await openByLauncher(page);
    await openExportPanel(page);
    await expect(region()).toBeVisible();
    expect(fixture.counts.create).toBe(beforeRemount.create);
    expect(fixture.counts.cancel).toBe(0);
    expect(fixture.jobs()).toHaveLength(1);
  });
  await checks.step("the_remounted_leaf_offers_no_stale_output", async () => {
    // A remount is not a resumed job. The leaf starts from its empty state, so nothing offers a
    // body the new view never fetched, and no preview lease or Blob URL survives the destroy.
    await expect(region().getByRole("status").first()).toHaveText(
      "Render the current timeline to create a final video.",
    );
    await expect(
      region().getByRole("button", { name: "Preview output" }),
    ).toHaveCount(0);
    await expect(
      region().getByRole("link", { name: "Download original" }),
    ).toHaveCount(0);
    await expect(region().locator("video")).toHaveCount(0);
    expect(fixture.counts.preview).toBe(beforeRemount.preview);
    expect(fixture.counts.download).toBe(beforeRemount.download);
  });
  const facts = {
    injection: "held status replies then a native view destroy and remount",
    creates: fixture.counts.create,
    cancels: fixture.counts.cancel,
    previews: fixture.counts.preview,
    downloads: fixture.counts.download,
    peak_status_clients: fixture.peakStatusClients(),
    job_path_requests: (await shellSnapshot(page)).renderJobRequests,
  };
  const evidence = hardeningEvidence(testInfo);
  for (const row of [
    "action.output.render",
    "action.output.status",
    "action.output.cancel",
    "action.output.final_preview",
    "action.output.download",
  ])
    await evidence.row(row, "recovery", checks, facts);
});

const IMPORT_BUTTON = '[data-h3-nle-control="asset.import_production"]';
const IMPORT_RETRY = '[data-h3-nle-control="asset.import_production.retry"]';
const IMPORT_STATE = "[data-h3-nle-import]";
const IMPORT_STATUS = '[data-h3-nle-status="import"]';

/** A named control that is hit-testable and keyboard-reachable, with its box recorded. */
async function assertReachable(
  target: Locator,
  checks: Checks,
  name: string,
  facts: Record<string, unknown>,
) {
  await checks.step(`${name}_is_named_hit_testable_and_focusable`, async () => {
    await expect(target).toHaveAccessibleName(/\S/);
    await target.scrollIntoViewIfNeeded();
    const box = (await target.boundingBox())!;
    facts[`${name}_box`] = [Math.round(box.width), Math.round(box.height)];
    const reachable = await target.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      const top = document.elementFromPoint(
        rect.left + rect.width / 2,
        rect.top + rect.height / 2,
      );
      return {
        hit: top !== null && (top === element || element.contains(top)),
        focusable:
          (element as HTMLElement).tabIndex >= 0 &&
          element.closest("[inert],[aria-hidden='true']") === null,
      };
    });
    expect(reachable).toEqual({ hit: true, focusable: true });
    await target.focus();
    await expect(target).toBeFocused();
  });
}

/**
 * M25-48 issues the Production import from the editor's Media home; the Production page holds
 * no import control. The stage is therefore the Clip editor function with the full editor open
 * on its Media pane. An open editor is a modal dialog whose backdrop covers the page navigation,
 * so with one open nothing behind it is clicked: the Media pane is selected in place.
 */
async function productionStage(page: Page) {
  const overlay = page.locator('[data-h3-nle-surface="overlay_v1"]');
  if ((await overlay.count()) === 0) {
    await page
      .getByRole("navigation", { name: "H3 Context pages" })
      .getByRole("button", { name: "Production" })
      .click();
    await page.getByRole("tab", { name: "Clip editor", exact: true }).click();
    await page.locator(LAUNCHER).click();
  }
  await expect(overlay).toHaveAttribute("data-h3-nle-state", "expanded");
  const pane = overlay.locator('[data-h3-nle-pane="assets"]');
  if ((await pane.getAttribute("aria-selected")) !== "true") await pane.click();
  await expect(page.locator(IMPORT_BUTTON)).toBeVisible();
}

async function openImportShell(
  page: Page,
  query: string,
  options: ImportFixtureOptions = {},
): Promise<ImportFixture> {
  const fixture = await startImportFixture(page, options);
  await page.goto(`/nleShell.html?import=1&${query}`);
  await productionStage(page);
  await expect(page.locator(IMPORT_BUTTON)).toBeEnabled();
  return fixture;
}

// The manifest names these titles exactly, so they stay at the file's top level: a describe
// block would prefix every collected id with its own title.
let fixture: ImportFixture | undefined;
test.afterEach(async () => {
  await fixture?.close();
  fixture = undefined;
});

/**
 * The import this test has refused: the pointer, keyboard and touch gestures take indices 0-2,
 * so the refusal is the fourth request and every accepted gesture above it is unaffected.
 */
const REFUSED_IMPORT_INDEX = 3;

test("hardening a11y import is reachable by pointer, keyboard and touch and focuses the highlighted result", async ({
  page,
}, testInfo) => {
  fixture = await openImportShell(page, "target=ready", {
    refuse: (kind, index) =>
      kind === "import" && index === REFUSED_IMPORT_INDEX ? 409 : null,
  });
  const imports = fixture;
  const checks = new Checks();
  const facts: Record<string, unknown> = {};
  const state = page.locator(IMPORT_STATE);
  const clipEditorTab = page.getByRole("tab", {
    name: "Clip editor",
    exact: true,
  });
  await assertReachable(page.locator(IMPORT_BUTTON), checks, "import", facts);
  await checks.step("nothing_is_announced_before_the_action", async () => {
    await expect(page.locator(IMPORT_STATUS)).toHaveCount(0);
    await expect(state).toHaveAttribute("data-h3-nle-import", "idle");
  });
  // A succeeded import moves the view to the Clip editor function, which unmounts the
  // Production workbench, so what a screen reader would have received is recorded as it is
  // rendered rather than read back afterwards from a surface that no longer exists.
  await page.evaluate(() => {
    const scope = window as unknown as { __h3Announced?: string[] };
    scope.__h3Announced = [];
    const record = () => {
      const status = document.querySelector('[data-h3-nle-status="import"]');
      if (status === null) return;
      const line = `${status.getAttribute("role")}|${status.getAttribute("aria-live")}|${status.textContent}`;
      if (scope.__h3Announced!.at(-1) !== line) scope.__h3Announced!.push(line);
    };
    new MutationObserver(record).observe(document.body, {
      subtree: true,
      childList: true,
      characterData: true,
      attributes: true,
    });
    record();
  });
  const activate = async (
    gesture: "pointer" | "keyboard" | "touch",
    run: () => Promise<void>,
  ) => {
    const before = imports.importRequests.length;
    await run();
    await expect.poll(() => imports.importRequests.length).toBe(before + 1);
    await expect
      .poll(async () => (await shellSnapshot(page)).importStatus)
      .toBe("succeeded");
    await expect(clipEditorTab).toHaveAttribute("aria-selected", "true");
  };
  await checks.step("a_pointer_click_imports_once", async () => {
    await activate("pointer", () => page.locator(IMPORT_BUTTON).click());
  });
  const highlighted = (await shellSnapshot(page)).highlightedAssetIds;
  await checks.step("the_result_is_highlighted_in_the_clip_editor", () => {
    expect(highlighted).toHaveLength(1);
  });
  await checks.step("the_outcome_was_announced_politely", async () => {
    const announced = await page.evaluate(
      () => (window as unknown as { __h3Announced: string[] }).__h3Announced,
    );
    expect(announced.length).toBeGreaterThan(0);
    for (const line of announced) expect(line).toMatch(/^status\|polite\|.+/);
    facts.announcements = announced.length;
  });
  await checks.step("enter_imports_once", async () => {
    await productionStage(page);
    await activate("keyboard", async () => {
      await page.locator(IMPORT_BUTTON).focus();
      await page.keyboard.press("Enter");
    });
  });
  await checks.step("a_tap_imports_once", async () => {
    await productionStage(page);
    await activate("touch", () => page.locator(IMPORT_BUTTON).tap());
  });
  await checks.step("every_gesture_reached_the_backend_once", async () => {
    expect(imports.importRequests).toHaveLength(3);
    expect(new Set(imports.importRequests).size).toBe(3);
    expect(imports.transactions).toHaveLength(0);
    // Repeat imports of the same ready output are idempotent, so the highlighted result and
    // the accepted timeline are the ones the first import produced.
    const snapshot = await shellSnapshot(page);
    expect(snapshot.highlightedAssetIds).toEqual(highlighted);
    expect(snapshot.queuedPrompts).toBe(0);
    expect(snapshot.renderJobRequests).toBe(0);
  });
  await checks.step(
    "the_highlighted_row_is_announced_in_the_editor",
    async () => {
      await productionStage(page);
      const row = page.locator(
        `[data-h3-nle-region="asset-bin"] [data-h3-nle-asset="${highlighted[0]}"]`,
      );
      await expect(row).toBeVisible();
      // The row is a list item: what a reader receives is its text, and the highlight is a
      // visible badge on its card rather than a colour alone (M25-48 asset-scoped bin).
      await expect(row).toContainText(/\S/);
      await expect(
        row.locator('[data-h3-nle-media-badge="added"]'),
      ).toBeVisible();
    },
  );
  await hardeningEvidence(testInfo).row(
    "import.production_outputs",
    "accessibility",
    checks,
    { ...facts, imports: imports.importRequests.length },
  );
  // The refusal is a row of its own, so it carries its own assertions: a refused import has to
  // be as reachable, as announced and as focus-preserving as an accepted one, and the fourth
  // request is the one the shell was opened with a 409 armed for.
  const refusal = new Checks();
  const refused: Record<string, unknown> = {};
  await refusal.step(
    "the_refused_import_is_activated_by_keyboard",
    async () => {
      await productionStage(page);
      await page.evaluate(() => {
        (window as unknown as { __h3Announced: string[] }).__h3Announced = [];
      });
      await page.locator(IMPORT_BUTTON).focus();
      await page.keyboard.press("Enter");
      await expect
        .poll(() => imports.importRequests.length)
        .toBe(REFUSED_IMPORT_INDEX + 1);
    },
  );
  await refusal.step("the_refusal_is_announced_politely", async () => {
    await expect(page.locator(IMPORT_STATE)).toHaveAttribute(
      "data-h3-nle-import",
      "refused",
    );
    const announced = await page.evaluate(
      () => (window as unknown as { __h3Announced: string[] }).__h3Announced,
    );
    // Fail closed: a refusal nobody is told about is the defect this row exists to catch.
    expect(announced.length).toBeGreaterThan(0);
    for (const line of announced) expect(line).toMatch(/^status\|polite\|.+/);
    // B-M1605-COPY-01: the refusal is announced in plain words; its code stays on the status.
    expect(announced.at(-1)).toContain(
      "The editor or Production changed since you selected. Import again after the refresh.",
    );
    await expect(page.locator(IMPORT_STATUS)).toHaveAttribute(
      "data-code",
      "conflict_or_replay",
    );
    refused.announcements = announced.length;
  });
  await refusal.step(
    "the_refusal_leaves_the_user_where_they_were",
    async () => {
      // A refusal must not move the view or the focus: the control the user activated is still
      // the control they are on, and the Clip editor is still the function they chose.
      await expect(page.locator(IMPORT_BUTTON)).toBeFocused();
      await expect(
        page.getByRole("tab", { name: "Clip editor", exact: true }),
      ).toHaveAttribute("aria-selected", "true");
      // What a refusal does to the highlight is the recovery row's subject, and it is asserted
      // there (`the_refusal_kept_no_optimistic_state`): a refusal keeps no optimistic state, so
      // the highlight is empty. This row asserts only what a reader and a keyboard user receive.
      expect(imports.transactions).toHaveLength(0);
    },
  );
  await hardeningEvidence(testInfo).row(
    "import.production_outputs.refusal",
    "accessibility",
    refusal,
    {
      ...refused,
      refusal: "conflict_or_replay",
      gesture: "keyboard",
      imports: imports.importRequests.length,
    },
  );
});

test("hardening recovery import lost reply is uncertain until an explicit exact retry", async ({
  page,
}, testInfo) => {
  // The first import is applied by the real service and its reply is then dropped: the browser
  // cannot know whether the ledger committed, which is exactly the state under test.
  fixture = await openImportShell(page, "target=ready", {
    reply: (kind, index) =>
      kind === "import" && index === 0 ? "abort" : "fulfill",
  });
  const imports = fixture;
  const checks = new Checks();
  const state = page.locator(IMPORT_STATE);
  const before = await shellSnapshot(page);
  await page.locator(IMPORT_BUTTON).click();
  await checks.step("a_lost_reply_is_uncertain_not_succeeded", async () => {
    await expect(state).toHaveAttribute("data-h3-nle-import", "uncertain");
    await expect(page.locator(IMPORT_STATUS)).toBeVisible();
    expect(imports.importRequests).toHaveLength(1);
  });
  await checks.step("uncertainty_holds_no_optimistic_state", async () => {
    const snapshot = await shellSnapshot(page);
    expect(snapshot.highlightedAssetIds).toEqual([]);
    expect(snapshot.authoringStateV2?.timelineRevision).toBe(
      before.authoringStateV2?.timelineRevision,
    );
    expect(imports.transactions).toHaveLength(0);
    expect(snapshot.queuedPrompts).toBe(0);
  });
  await checks.step("no_automatic_retry_is_issued", async () => {
    await page.waitForTimeout(1_500);
    expect(imports.importRequests).toHaveLength(1);
    await expect(page.locator(IMPORT_BUTTON)).toBeDisabled();
    await expect(page.locator(IMPORT_RETRY)).toBeEnabled();
  });
  await checks.step(
    "the_explicit_retry_replays_the_exact_request",
    async () => {
      await page.locator(IMPORT_RETRY).focus();
      await page.keyboard.press("Enter");
      await expect.poll(() => imports.importRequests.length).toBe(2);
      expect(imports.importRequests[1]).toBe(imports.importRequests[0]);
      await expect
        .poll(async () => (await shellSnapshot(page)).importStatus)
        .toBe("succeeded");
    },
  );
  await checks.step("the_committed_import_is_not_duplicated", async () => {
    // IMPORTANT: an asset-only V2 import has a durable catalog but no render snapshot.
    // Receipt identities own deduplication; the short-lived highlight is only a UX effect.
    const importedIds = imports.importedAssetIds();
    expect(importedIds).toHaveLength(1);
    await expect
      .poll(async () => (await shellSnapshot(page)).highlightedAssetIds)
      .toEqual(importedIds);
    await expect
      .poll(async () => {
        const current = await shellSnapshot(page);
        return (
          current.authoringStateV2?.assets.filter((asset) =>
            importedIds.includes(asset.assetId),
          ).length ?? 0
        );
      })
      .toBe(1);
    const snapshot = await shellSnapshot(page);
    expect(
      snapshot.authoringStateV2?.assets.filter((asset) =>
        importedIds.includes(asset.assetId),
      ),
    ).toHaveLength(1);
    expect(snapshot.authoringStateV2?.timelineRevision).toBe(
      before.authoringStateV2?.timelineRevision,
    );
    expect(snapshot.authoringStateV2?.clips).toEqual([]);
    expect(snapshot.timelineSnapshot).toBeNull();
    expect(imports.transactions).toHaveLength(0);
    expect(snapshot.renderJobRequests).toBe(0);
    await expect(page.locator(IMPORT_RETRY)).toHaveCount(0);
  });
  await hardeningEvidence(testInfo).row(
    "import.production_outputs",
    "recovery",
    checks,
    {
      injection: "the reply to a committed import dropped at the transport",
      requests: imports.importRequests.length,
      distinct_request_ids: new Set(imports.importRequests).size,
    },
  );
});

test("hardening recovery import refusals keep no optimistic state and no timeline change", async ({
  page,
}, testInfo) => {
  // A bodiless 409 is the accepted route's own refusal wire (status only, no error body and no
  // projection). It is answered before the ledger is reached, so the backend state a refusal
  // must leave untouched cannot have moved, and everything asserted below is the client's.
  fixture = await openImportShell(page, "target=ready", {
    refuse: (kind, index) => (kind === "import" && index === 0 ? 409 : null),
  });
  const imports = fixture;
  const checks = new Checks();
  const state = page.locator(IMPORT_STATE);
  const before = await shellSnapshot(page);
  await page.locator(IMPORT_BUTTON).click();
  await checks.step("a_refusal_is_definite_not_uncertain", async () => {
    await expect(state).toHaveAttribute("data-h3-nle-import", "refused");
    await expect(page.locator(IMPORT_STATUS)).toHaveAttribute(
      "data-code",
      "conflict_or_replay",
    );
    await expect(page.locator(IMPORT_STATUS)).toContainText(
      "The editor or Production changed since you selected. Import again after the refresh.",
    );
    // Definite means no retained request to replay: only `uncertain` arms the exact retry.
    await expect(page.locator(IMPORT_RETRY)).toHaveCount(0);
  });
  await checks.step("the_refusal_kept_no_optimistic_state", async () => {
    const snapshot = await shellSnapshot(page);
    expect(snapshot.highlightedAssetIds).toEqual([]);
    expect(snapshot.authoringStateV2?.assets).toEqual(
      before.authoringStateV2?.assets,
    );
    expect(snapshot.authoringStateV2?.workspaceRevision).toBe(
      before.authoringStateV2?.workspaceRevision,
    );
    expect(snapshot.authoringStateV2?.timelineRevision).toBe(
      before.authoringStateV2?.timelineRevision,
    );
    expect(snapshot.authoringStateV2?.clips).toEqual([]);
    expect(snapshot.timelineSnapshot).toBeNull();
    expect(imports.transactions).toHaveLength(0);
    expect(snapshot.queuedPrompts).toBe(0);
    expect(snapshot.renderJobRequests).toBe(0);
  });
  await checks.step("the_view_stays_where_the_user_is", async () => {
    await expect(
      page.getByRole("tab", { name: "Clip editor", exact: true }),
    ).toHaveAttribute("aria-selected", "true");
    await page.waitForTimeout(1_000);
    expect(imports.importRequests).toHaveLength(1);
  });
  await checks.step("a_later_import_is_unpoisoned", async () => {
    await expect(page.locator(IMPORT_BUTTON)).toBeEnabled();
    await page.locator(IMPORT_BUTTON).click();
    await expect.poll(() => imports.importRequests.length).toBe(2);
    expect(imports.importRequests[1]).not.toBe(imports.importRequests[0]);
    await expect
      .poll(async () => (await shellSnapshot(page)).importStatus)
      .toBe("succeeded");
    const snapshot = await shellSnapshot(page);
    expect(snapshot.highlightedAssetIds).toHaveLength(1);
    expect(snapshot.authoringStateV2?.timelineRevision).toBe(
      before.authoringStateV2?.timelineRevision,
    );
    expect(imports.transactions).toHaveLength(0);
  });
  await hardeningEvidence(testInfo).row(
    "import.production_outputs.refusal",
    "recovery",
    checks,
    {
      injection: "bodiless 409 refusal on the accepted import route",
      refusal: "conflict_or_replay",
      requests: imports.importRequests.length,
    },
  );
});
