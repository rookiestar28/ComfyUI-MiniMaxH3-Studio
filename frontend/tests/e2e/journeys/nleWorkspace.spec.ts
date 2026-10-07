import { test, expect, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { join } from "node:path";

import {
  canonicalWorkspace,
  grip,
  snapshot,
  surface,
} from "../helpers/nleCanonical";
import { routeGenericFixtureMedia } from "../helpers/genericFixtureMedia";
import { chooseAssetCommand } from "../helpers/nleBinMenus";
import { expectGripClearOfLanes, nleLaneOrigin } from "../helpers/nleTargets";
import {
  playheadFrame,
  playheadSlider,
  seekPlayhead,
} from "../helpers/nleTimeline";

async function decodedWorkspace(page: Page, shape: string, canonical = false) {
  await routeGenericFixtureMedia(page);
  if (canonical)
    await canonicalWorkspace(
      page,
      undefined,
      undefined,
      `&media=1&shape=${shape}`,
    );
  else {
    await page.goto(`/nleWorkspace.html?media=1&shape=${shape}`);
    await page.getByRole("button", { name: "Open full editor" }).click();
  }
  await expect(
    page.getByRole("button", { name: "Play", exact: true }),
  ).toBeEnabled();
}

async function openTimelineMore(page: Page) {
  const menu = page.getByRole("menu", { name: "More timeline tools" });
  if (!(await menu.isVisible()))
    await page.locator('[data-h3-nle-control="toolbar.more"]').click();
  await expect(menu).toBeVisible();
}

async function closeTimelineMore(page: Page) {
  const menu = page.getByRole("menu", { name: "More timeline tools" });
  if (!(await menu.isVisible())) return;
  await page.locator('[data-h3-nle-control="toolbar.more"]').click();
  await expect(menu).toBeHidden();
}

const PROPERTY_TABS: Readonly<Record<string, string>> = {
  "visual.transform": "basic",
  "visual.opacity_blend": "basic",
  "visual.crop": "crop",
  "visual.effect": "colour",
  "text.content": "text",
  "text.style": "text",
  "boundary.transition": "transition",
  "audio.clip": "audio",
};

async function revealProperty(page: Page, operation: string) {
  const tab = PROPERTY_TABS[operation];
  if (tab !== undefined)
    await page.locator(`[data-h3-nle-property-tab="${tab}"]`).click();
}

async function openTrackMenu(page: Page, trackId: string) {
  await page
    .locator(
      `[data-h3-nle-track="${trackId}"] [data-h3-nle-menu-trigger="track"]`,
    )
    .click();
  const menu = page.getByRole("menu", { name: "Track menu" });
  await expect(menu).toBeVisible();
  return menu;
}

async function openClipMenu(page: Page, clipId: string) {
  const trigger = page.locator(
    `[data-h3-nle-clip="${clipId}"] [data-h3-nle-menu-trigger="clip"], ` +
      `.h3-nle-trim-rail[data-h3-nle-trim-clip="${clipId}"] [data-h3-nle-menu-trigger="clip"]`,
  );
  // M25-62 (B-M2562-03): this spec runs under a fine pointer, where a selected clip narrower than
  // 96 px has no inline trigger and opens its menu by a context click.
  if ((await trigger.count()) === 0)
    await page
      .locator(
        `[data-h3-nle-clip="${clipId}"] [data-h3-nle-control="selection.set"]`,
      )
      .click({ button: "right" });
  else {
    await expect(trigger).toHaveCount(1);
    await trigger.click();
  }
  const menu = page.getByRole("menu", { name: "Clip menu" });
  await expect(menu).toBeVisible();
  return menu;
}

import type {} from "../../../e2e/nleSequence";

test("NLE sequence keyboard review, import, serial start and separate assembly", async ({
  page,
}) => {
  await page.goto("/nleSequence.html");
  await expect(
    page.locator('[data-h3-nle-control="planning.target_seconds"]'),
  ).toHaveValue("60");
  await page
    .locator('[data-h3-nle-control="planning.target_seconds"]')
    .fill("20");
  // M25-63: Production's planning section and the editor's Sequence tab both offer the
  // readiness request; this flow requests it from the editor's tab, as before.
  const scoped = (id: string) =>
    id === "readiness.request"
      ? `[data-h3-nle-region="sequence"] [data-h3-nle-control="${id}"]`
      : `[data-h3-nle-control="${id}"]`;
  const activate = async (id: string) => {
    const control = page.locator(scoped(id));
    await expect(control).toBeEnabled();
    await control.focus();
    await page.keyboard.press("Enter");
  };
  await activate("planning.prepare_context");
  await expect(
    page.locator('[data-h3-nle-status="planning-context"]'),
  ).toContainText("Source context: 10 s. Production target: 20 s.");
  await expect(
    page.getByRole("list", { name: "Requested and sampled segment durations" }),
  ).toContainText("requested 4.167 s; sampled 4.458 s (107 frames)");
  await activate("planning.review_storyboard");
  await activate("planning.add_row");
  await page
    .locator('[data-h3-nle-region="storyboard-review"] input[type="text"]')
    .fill("A blue sphere turns.");
  await activate("planning.admit_reviewed");
  await activate("planning.propose");
  await activate("planning.approve_import");
  expect(
    await page.evaluate(
      () => window.nleSequenceHarness.snapshot().engine.queueCalls,
    ),
  ).toBe(0);
  await activate("readiness.request");
  await activate("sequence.start");
  await expect(page.locator('[data-h3-nle-status="sequence"]')).toHaveAttribute(
    "data-state",
    "detach_ready",
  );
  await page.evaluate(() => window.nleSequenceHarness.completeCurrent());
  expect(
    await page.evaluate(
      () => window.nleSequenceHarness.snapshot().engine.queueCalls,
    ),
  ).toBe(2);
  await page.evaluate(() => window.nleSequenceHarness.completeCurrent());
  await expect(page.locator('[data-h3-nle-status="sequence"]')).toHaveAttribute(
    "data-state",
    "finished",
  );
  expect(
    await page.evaluate(() => window.nleSequenceHarness.snapshot().events),
  ).not.toContain("production:assemble_sequence");
  await activate("assembly.assemble");
  await expect(page.locator("[data-h3-nle-assembly-state]")).toHaveAttribute(
    "data-h3-nle-assembly-state",
    "running",
  );
  await activate("production.refresh");
  await expect(page.locator("[data-h3-nle-assembly-state]")).toHaveAttribute(
    "data-h3-nle-assembly-state",
    "succeeded",
  );
  await expect(
    page.locator('[data-h3-nle-status="assembly-receipt"]'),
  ).toHaveAttribute("role", "status");
  await expect(
    page.locator('[data-h3-nle-status="assembly-inputs"]'),
  ).toContainText("Built from 2 segment videos and 1 cuts.");
});

async function detachAtChildBoundary(page: Page, completed: boolean) {
  await page.goto("/nleSequence.html");
  await page
    .locator('[data-h3-nle-control="planning.target_seconds"]')
    .fill("20");
  for (const id of [
    "planning.prepare_context",
    "planning.admit_canonical",
    "planning.propose",
    "planning.approve_import",
    "readiness.request",
    "sequence.start",
    "sequence.detach",
  ]) {
    const control = page.locator(
      id === "readiness.request"
        ? `[data-h3-nle-region="sequence"] [data-h3-nle-control="${id}"]`
        : `[data-h3-nle-control="${id}"]`,
    );
    await expect(control).toBeEnabled();
    await control.focus();
    await page.keyboard.press("Enter");
  }
  await expect(page.locator('[data-h3-nle-status="sequence"]')).toHaveAttribute(
    "data-state",
    "safe_to_leave",
  );
  await expect(
    page.locator('[data-h3-nle-status="recovery-deadline"]'),
  ).toBeVisible();
  if (completed)
    await page.evaluate(() => window.nleSequenceHarness.completeCurrent());
  return page.evaluate(() => window.nleSequenceHarness.snapshot());
}

async function reattachReadOnly(
  page: Page,
  before: Awaited<ReturnType<typeof detachAtChildBoundary>>,
  completed: boolean,
) {
  await page.locator('[data-h3-nle-control="sequence.reattach"]').focus();
  await page.keyboard.press("Enter");
  await expect(page.locator('[data-h3-nle-status="sequence"]')).toHaveAttribute(
    "data-state",
    completed ? "resume_ready" : "current_segment_still_owned",
  );
  const recovered = await page.evaluate(() =>
    window.nleSequenceHarness.snapshot(),
  );
  expect(recovered.engine.queueCalls).toBe(1);
  const recoveryActions = recovered.engine.actions.slice(
    before.engine.actions.length,
  );
  expect(recoveryActions[0]).toBe("parent:read_projection");
  if (!completed) expect(recoveryActions[1]).toBe("history:prompt.1");
  expect(
    recoveryActions.every(
      (action) =>
        action === "parent:read_projection" || action === "history:prompt.1",
    ),
  ).toBe(true);
}

for (const completed of [false, true]) {
  test(`NLE B1 destroys and recreates the view before read-only recovery (${completed ? "completed" : "still owned"})`, async ({
    page,
  }) => {
    const before = await detachAtChildBoundary(page, completed);
    await page.evaluate(() => window.nleSequenceHarness.recreate());
    await expect(
      page.locator('[data-h3-nle-status="recovery-pointer"]'),
    ).toBeVisible();
    const recreated = await page.evaluate(() =>
      window.nleSequenceHarness.snapshot(),
    );
    expect(recreated.mounts).toBe(before.mounts + 1);
    expect(recreated.engine.actions).toEqual(before.engine.actions);
    expect(recreated.pointer).toEqual(before.pointer);
    await reattachReadOnly(page, before, completed);
    const resume = page.locator('[data-h3-nle-control="sequence.resume"]');
    if (completed) {
      // IMPORTANT (B-M2522-RESUME-02): a recreated runner never bound a child, so it holds no
      // workflow label and cannot revalidate the canvas. Resume is refused as a typed state
      // with no parent mutation and no queue, never guessed from a fresh canvas hash.
      await expect(resume).toBeEnabled();
      await resume.focus();
      await page.keyboard.press("Enter");
      await expect(
        page.locator('[data-h3-nle-status="sequence"]'),
      ).toHaveAttribute("data-state", "recovery_unavailable_or_expired");
      const refused = await page.evaluate(() =>
        window.nleSequenceHarness.snapshot(),
      );
      expect(refused.engine.queueCalls).toBe(1);
      expect(refused.engine.actions).not.toContain("parent:resume_sequence");
      expect(refused.events).not.toContain("sequence:resume");
    } else await expect(resume).toBeDisabled();
  });
}

test("NLE B1 leaves and returns to the view, then queues the successor only on explicit resume", async ({
  page,
}) => {
  const before = await detachAtChildBoundary(page, true);
  await page.evaluate(() => window.nleSequenceHarness.leaveView());
  const returned = await page.evaluate(() =>
    window.nleSequenceHarness.snapshot(),
  );
  expect(returned.mounts).toBe(before.mounts + 1);
  expect(returned.engine.actions).toEqual(before.engine.actions);
  expect(returned.pointer).toEqual(before.pointer);
  await reattachReadOnly(page, before, true);
  const resume = page.locator('[data-h3-nle-control="sequence.resume"]');
  await expect(resume).toBeEnabled();
  await resume.focus();
  await page.keyboard.press("Enter");
  // The engine fences resume with the bound label and written projection, as the backend does.
  await expect
    .poll(async () =>
      page.evaluate(
        () => window.nleSequenceHarness.snapshot().engine.queueCalls,
      ),
    )
    .toBe(2);
  const resumed = await page.evaluate(() =>
    window.nleSequenceHarness.snapshot(),
  );
  expect(
    resumed.engine.actions.filter(
      (action) => action === "parent:resume_sequence",
    ),
  ).toHaveLength(1);
  expect(
    resumed.events.filter((event) => event === "sequence:resume"),
  ).toHaveLength(1);
  await expect(resume).toBeDisabled();
});

test("playback waits for a lagging native overlay callback without clearing the delivered frame", async ({
  page,
}) => {
  await decodedWorkspace(page, "smoke");
  await page.evaluate(() => {
    const element = window.__nleMediaDebug!.videoElementsByClip.get("clip-1")!;
    const request = element.requestVideoFrameCallback.bind(element);
    let pending: (() => void) | undefined;
    const state = {
      held: false,
      reachedLandmark: false,
      release: () => {
        pending?.();
        pending = undefined;
      },
    };
    // Native RVFCs arrive independently. Hold one real overlay observation before the first
    // landmark; do not change its PTS, the media clock, or any runtime/painter receipt.
    element.requestVideoFrameCallback = (callback) =>
      request((now, metadata) => {
        if (!state.held && metadata.mediaTime >= 3 / 24) {
          state.held = true;
          pending = () => callback(now, metadata);
        } else callback(now, metadata);
      });
    const primary = window.__nleMediaDebug!.videoElementsByClip.get("clip-0")!;
    const primaryRequest = primary.requestVideoFrameCallback.bind(primary);
    primary.requestVideoFrameCallback = (callback) =>
      primaryRequest((now, metadata) => {
        if (metadata.mediaTime >= 0.5) state.reachedLandmark = true;
        callback(now, metadata);
      });
    Object.assign(window, { nleLagObservation: state });
  });
  const monitor = page.locator('[data-h3-nle-canvas="composition"]');
  await page.getByRole("button", { name: "Play", exact: true }).click();
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (
            window as unknown as {
              nleLagObservation: { held: boolean; reachedLandmark: boolean };
            }
          ).nleLagObservation.reachedLandmark,
      ),
    )
    .toBe(true);
  const heldFrames = await monitor.evaluate(
    (canvas) =>
      new Promise<number[]>((resolve) => {
        const frames: number[] = [];
        const sample = () => {
          const value = canvas.getAttribute("data-h3-nle-presented-frame");
          frames.push(value === null ? -1 : Number(value));
          if (frames.length < 12) requestAnimationFrame(sample);
          else resolve(frames);
        };
        requestAnimationFrame(sample);
      }),
  );
  expect(heldFrames.every((frame) => frame > 0 && frame < 12)).toBe(true);
  await expect(
    page.getByRole("button", { name: "Pause", exact: true }),
  ).toBeEnabled();
  await page.evaluate(() =>
    (
      window as unknown as { nleLagObservation: { release(): void } }
    ).nleLagObservation.release(),
  );
  await expect
    .poll(async () =>
      Number(await monitor.getAttribute("data-h3-nle-presented-frame")),
    )
    .toBeGreaterThanOrEqual(12);
  await page.getByRole("button", { name: "Pause", exact: true }).click();
  expect(
    await monitor.evaluate((canvas: HTMLCanvasElement) =>
      canvas
        .getContext("2d")!
        .getImageData(0, 0, canvas.width, canvas.height)
        .data.some((value, index) => index % 4 !== 3 && value > 0),
    ),
  ).toBe(true);
  await page.locator('[data-h3-nle-action="close"]').click();
  await expect
    .poll(async () => (await snapshot(page)).mediaOwnership.live)
    .toBe(0);
  const owners = (await snapshot(page)).mediaOwnership;
  expect(owners.acquired).toBe(owners.released);
});

for (const shape of ["smoke", "virtualized"]) {
  test(`${shape} composition decodes real media and releases every owner on close`, async ({
    page,
  }) => {
    await decodedWorkspace(page, shape);
    const monitor = page.locator('[data-h3-nle-canvas="composition"]');
    await page.getByRole("button", { name: "Play", exact: true }).click();
    // IMPORTANT: the requested playhead is not a delivered frame. Sample actual playback
    // immediately; an arbitrary idle delay can outlive this fixture's first media window.
    await expect
      .poll(async () => {
        const frame = await monitor.getAttribute("data-h3-nle-presented-frame");
        return frame === null ? -1 : Number(frame);
      })
      .toBeGreaterThan(0);
    // B-M2544-06: the monitor layout holds still while it plays. The picture row absorbs the rows
    // under it, and a status row whose height followed the live audio state moved the picture and
    // the transport 25 px on every frame (and kept Pause from being clickable until playback had
    // run into the fixture's gap after frame 48).
    const layout = await page.evaluate(
      () =>
        new Promise<number[][]>((resolve) => {
          const samples: number[][] = [];
          const tick = () => {
            const transport = document
              .querySelector(
                '[data-h3-nle-surface="overlay_v1"] .h3-nle-transport',
              )!
              .getBoundingClientRect();
            const canvasElement = document.querySelector(
              '[data-h3-nle-surface="overlay_v1"] [data-h3-nle-canvas="composition"]',
            )!;
            const canvas = canvasElement.getBoundingClientRect();
            const frame = canvasElement.getAttribute(
              "data-h3-nle-presented-frame",
            );
            const playing = [...document.querySelectorAll("button")].some(
              (button) => button.getAttribute("aria-label") === "Pause",
            );
            samples.push([
              transport.top,
              canvas.height,
              frame === null ? -1 : Number(frame),
              playing ? 1 : 0,
            ]);
            // Cross the first sparse PTS landmark so both independently delivered video
            // observations must support the presented scene, rather than sampling only PTS0.
            if (
              samples.length < 120 &&
              (samples.length < 12 || (frame !== null && Number(frame) < 13))
            )
              requestAnimationFrame(tick);
            else resolve(samples);
          };
          requestAnimationFrame(tick);
        }),
    );
    expect(layout.at(-1)![2]).toBeGreaterThanOrEqual(13);
    for (const [top, height, frame, playing] of layout) {
      expect(Math.abs(top! - layout[0]![0]!)).toBeLessThanOrEqual(0.5);
      expect(Math.abs(height! - layout[0]![1]!)).toBeLessThanOrEqual(0.5);
      expect(frame).toBeGreaterThan(0);
      expect(playing).toBe(1);
    }
    await page.getByRole("button", { name: "Pause", exact: true }).click();
    expect(
      (await snapshot(page)).mediaOwnership.acquired,
    ).toBeGreaterThanOrEqual(4);
    const pixels = await page
      .locator('[data-h3-nle-canvas="composition"]')
      .evaluate((canvas: HTMLCanvasElement) => {
        const bytes = canvas
          .getContext("2d")!
          .getImageData(0, 0, canvas.width, canvas.height).data;
        return bytes.some((value, index) => index % 4 !== 3 && value > 0);
      });
    expect(pixels).toBe(true);
    await page.locator('[data-h3-nle-action="close"]').click();
    await expect
      .poll(async () => (await snapshot(page)).mediaOwnership.live)
      .toBe(0);
    const owners = (await snapshot(page)).mediaOwnership;
    expect(owners.acquired).toBe(owners.released);
  });
}

for (const row of [
  { control: "clip.enabled", expected: { enabled: false } },
  {
    control: "visual.transform",
    field: "Position X (%)",
    value: "5",
    expected: { transform: { position_x_bp: 500 } },
  },
  {
    control: "visual.crop",
    field: "Crop left (%)",
    value: "5",
    expected: { crop: { left_bp: 500 } },
  },
  {
    control: "visual.opacity_blend",
    field: "Opacity (%)",
    value: "50",
    expected: { opacityBp: 5000 },
  },
  {
    control: "visual.effect",
    field: "Brightness (%)",
    value: "10",
    expected: { effect: { kind: "color_adjust_v1", brightnessPermille: 100 } },
  },
  {
    control: "audio.clip",
    field: "Volume (dB)",
    value: "-6",
    expected: {
      audio: { gainMb: -600, muted: false, fadeInFrames: 0, fadeOutFrames: 0 },
    },
  },
] as const) {
  test(`canonical ${row.control} adopts accepted attributes and undo/redo`, async ({
    page,
  }) => {
    const transactions = await canonicalWorkspace(page);
    await page
      .locator(
        '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
      )
      .click();
    await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
    const before = (await snapshot(page)).timelineSnapshot!.clips.find(
      (clip) => clip.clipId === "clip-0",
    )!;
    const inspector = page.locator('[data-h3-nle-selected-clip="clip-0"]');
    if (row.control === "clip.enabled") {
      const menu = await openClipMenu(page, "clip-0");
      await menu.locator('[data-h3-nle-control="clip.enabled"]').click();
    } else {
      await revealProperty(page, row.control);
      if (row.field !== undefined && row.value !== undefined)
        await inspector
          .getByRole("spinbutton", { name: row.field, exact: true })
          .fill(row.value);
      await inspector.locator(`[data-h3-nle-control="${row.control}"]`).click();
    }
    await expect.poll(async () => (await snapshot(page)).receipts).toBe(2);
    const current = async () =>
      (await snapshot(page)).timelineSnapshot!.clips.find(
        (clip) => clip.clipId === "clip-0",
      );
    expect(await current()).toMatchObject(row.expected);
    await page.locator('[data-h3-nle-control="history.undo"]').click();
    await expect.poll(async () => (await snapshot(page)).receipts).toBe(3);
    expect(await current()).toEqual(before);
    await page.locator('[data-h3-nle-control="history.redo"]').click();
    await expect.poll(async () => (await snapshot(page)).receipts).toBe(4);
    expect(await current()).toMatchObject(row.expected);
    expect(transactions).toHaveLength(4);
  });
}

async function open(page: Page, shape = "smoke") {
  await page.goto(`/nleWorkspace.html?shape=${shape}`);
  await page.getByRole("button", { name: "Open full editor" }).click();
  await expect(page.locator(surface)).toHaveAttribute(
    "data-h3-nle-state",
    "expanded",
  );
}

test("canonical track controls add, reorder, disable, lock and remove the accepted track", async ({
  page,
}) => {
  await canonicalWorkspace(page);
  const before = (await snapshot(page)).timelineSnapshot!.tracks;
  let menu = await openTrackMenu(page, "track-0");
  await menu.locator('[data-h3-nle-control="track.add"]').click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
  const added = (await snapshot(page)).timelineSnapshot!.tracks.find(
    (track) => !before.some((old) => old.trackId === track.trackId),
  )!;
  expect(added).toMatchObject({
    kind: "video_overlay",
    enabled: true,
    locked: false,
  });
  menu = await openTrackMenu(page, added.trackId);
  await menu.getByRole("spinbutton", { name: "Order" }).fill("1");
  await menu.locator('[data-h3-nle-control="track.reorder"]').click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(2);
  const current = async () =>
    (await snapshot(page)).timelineSnapshot!.tracks.find(
      (track) => track.trackId === added.trackId,
    );
  expect(await current()).toMatchObject({ order: 1 });
  const header = page.locator(`[data-h3-nle-track="${added.trackId}"]`);
  await header.locator('[data-h3-nle-control="track.enabled"]').click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(3);
  expect(await current()).toMatchObject({ enabled: false });
  await header.locator('[data-h3-nle-control="track.locked"]').click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(4);
  expect(await current()).toMatchObject({ locked: true });
  await header.locator('[data-h3-nle-control="track.locked"]').click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(5);
  menu = await openTrackMenu(page, added.trackId);
  await menu.locator('[data-h3-nle-control="track.remove"]').click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(6);
  expect(await current()).toBeUndefined();
});

for (const row of [
  { control: "clip.move", expected: { startFrame: 51 } },
  {
    control: "clip.slip",
    field: "Delta frames",
    value: "12",
    expected: { sourceStartFrame: 12 },
  },
  {
    control: "clip.trim",
    expected: { durationFrames: 47 },
  },
  {
    control: "asset.replace",
    field: "Source start frame",
    value: "12",
    expected: { sourceStartFrame: 12 },
  },
] as const) {
  test(`canonical ${row.control} changes only accepted clip geometry`, async ({
    page,
  }) => {
    await canonicalWorkspace(page);
    await page
      .locator(
        '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
      )
      .click();
    await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
    const inspector = page.locator('[data-h3-nle-selected-clip="clip-0"]');
    const needsSourceRoom =
      row.control === "clip.slip" || row.control === "asset.replace";
    if (needsSourceRoom) {
      // M25-62 (R7): clip-0 is wide enough for its inline grips; a narrow one uses the rail.
      const endGrip = page.locator(
        '[data-h3-nle-clip="clip-0"] .h3-nle-grip[data-h3-nle-trim-edge="end"]:not([hidden]), ' +
          '.h3-nle-trim-rail[data-h3-nle-trim-clip="clip-0"] [data-h3-nle-trim-edge="end"]',
      );
      await endGrip.focus();
      for (let frame = 0; frame < 12; frame += 1)
        await page.keyboard.press("ArrowLeft");
      await page.keyboard.press("Enter");
      await expect.poll(async () => (await snapshot(page)).receipts).toBe(2);
    }
    if (row.control === "clip.move") {
      await openTimelineMore(page);
      await page.locator('[data-h3-nle-alternative="move.open"]').click();
      await page
        .getByRole("button", { name: "Move later by one grid step" })
        .click();
      await page.locator('[data-h3-nle-control="clip.move"]').click();
    } else if (row.control === "clip.trim") {
      // M25-62 (R7): clip-0 is wide enough for its inline grips; a narrow one uses the rail.
      const endGrip = page.locator(
        '[data-h3-nle-clip="clip-0"] .h3-nle-grip[data-h3-nle-trim-edge="end"]:not([hidden]), ' +
          '.h3-nle-trim-rail[data-h3-nle-trim-clip="clip-0"] [data-h3-nle-trim-edge="end"]',
      );
      await endGrip.focus();
      await page.keyboard.press("ArrowLeft");
      await page.keyboard.press("Enter");
    } else {
      const menu = await openClipMenu(page, "clip-0");
      if (row.field !== undefined && row.value !== undefined)
        await menu
          .getByRole("spinbutton", { name: row.field, exact: true })
          .fill(row.value);
      await menu.locator(`[data-h3-nle-control="${row.control}"]`).click();
    }
    await expect
      .poll(async () => (await snapshot(page)).receipts)
      .toBe(needsSourceRoom ? 3 : 2);
    expect(
      (await snapshot(page)).timelineSnapshot!.clips.find(
        (clip) => clip.clipId === "clip-0",
      ),
    ).toMatchObject(row.expected);
  });
}

test("canonical title content and style preserve the exact user text", async ({
  page,
}) => {
  await canonicalWorkspace(page);
  await page
    .locator(
      '[data-h3-nle-clip="clip-3"] [data-h3-nle-control="selection.set"]',
    )
    .click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
  const inspector = page.locator('[data-h3-nle-selected-clip="clip-3"]');
  const content = "Exact title: A & B <C>";
  await revealProperty(page, "text.content");
  await inspector
    .getByRole("textbox", { name: "Text", exact: true })
    .fill(content);
  await inspector.locator('[data-h3-nle-control="text.content"]').click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(2);
  await inspector
    .getByRole("spinbutton", { name: "Size (px)", exact: true })
    .fill("64");
  await inspector.locator('[data-h3-nle-control="text.style"]').click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(3);
  expect(
    (await snapshot(page)).timelineSnapshot!.clips.find(
      (clip) => clip.clipId === "clip-3",
    )!.text,
  ).toMatchObject({ content, sizePx: 64 });
});

test("canonical unrepresentable source slip refuses without mutating the timeline", async ({
  page,
}) => {
  await canonicalWorkspace(page);
  await page
    .locator(
      '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
    )
    .click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
  const before = (await snapshot(page)).timelineSnapshot;
  const menu = await openClipMenu(page, "clip-0");
  await menu.locator('[data-h3-nle-control="clip.slip"]').click();
  await expect(page.locator('[data-h3-nle-status="timeline"]')).toContainText(
    "rejected",
  );
  expect((await snapshot(page)).timelineSnapshot).toEqual(before);
  expect((await snapshot(page)).receipts).toBe(1);
});

test("canonical split and merge restore media geometry before explicit removal", async ({
  page,
}) => {
  await routeGenericFixtureMedia(page);
  await canonicalWorkspace(page, undefined, undefined, "&media=1");
  await page
    .locator(
      '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
    )
    .click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
  const before = (await snapshot(page)).timelineSnapshot!.clips;
  const ruler = playheadSlider(page);
  await expect(ruler).toHaveAttribute("aria-disabled", "false");
  await seekPlayhead(page, ruler, 12);
  await page.locator('[data-h3-nle-control="clip.split"]').click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(2);
  const split = (await snapshot(page)).timelineSnapshot!.clips;
  expect(split).toHaveLength(before.length + 1);
  const right = split.find(
    (clip) => !before.some((old) => old.clipId === clip.clipId),
  )!;
  expect(right).toMatchObject({
    startFrame: 12,
    sourceStartFrame: 12,
    durationFrames: 36,
  });
  const menu = await openClipMenu(page, "clip-0");
  await menu.locator('[data-h3-nle-control="clip.merge"]').click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(3);
  expect(
    Object.fromEntries(
      (await snapshot(page)).timelineSnapshot!.clips.map((clip) => [
        clip.clipId,
        clip,
      ]),
    ),
  ).toEqual(Object.fromEntries(before.map((clip) => [clip.clipId, clip])));
  await page.locator('[data-h3-nle-control="clip.remove"]').click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(4);
  expect(
    (await snapshot(page)).timelineSnapshot!.clips.some(
      (clip) => clip.clipId === "clip-0",
    ),
  ).toBe(false);
});

for (const operation of ["insert", "overwrite"] as const) {
  test(`canonical range ${operation} preserves canonical placement and undo`, async ({
    page,
  }) => {
    await routeGenericFixtureMedia(page);
    await canonicalWorkspace(page, undefined, undefined, "&media=1");
    await expect(
      page.getByRole("button", { name: "Play", exact: true }),
    ).toBeEnabled();
    const ruler = playheadSlider(page);
    await ruler.focus();
    await page.keyboard.press("Home");
    await page.keyboard.press("PageUp");
    await page.keyboard.press("PageUp");
    for (let frame = 48; frame < 60; frame += 1)
      await page.keyboard.press("ArrowRight");
    await expect.poll(() => playheadFrame(ruler)).toBe(60);
    const before = (await snapshot(page)).timelineSnapshot!.clips;
    await page.locator('[data-h3-nle-pane="assets"]').click();
    await chooseAssetCommand(
      page,
      page.locator('[data-h3-nle-card-index="1"]'),
      `range.${operation}`,
    );
    await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
    const after = (await snapshot(page)).timelineSnapshot!.clips;
    const next = after.find((clip) => clip.clipId === "clip-4")!;
    expect(next.startFrame).toBe(operation === "insert" ? 228 : 180);
    expect(
      after.some((clip) => !before.some((old) => old.clipId === clip.clipId)),
    ).toBe(true);
    await page.locator('[data-h3-nle-control="history.undo"]').click();
    await expect.poll(async () => (await snapshot(page)).receipts).toBe(2);
    expect(
      Object.fromEntries(
        (await snapshot(page)).timelineSnapshot!.clips.map((clip) => [
          clip.clipId,
          clip,
        ]),
      ),
    ).toEqual(Object.fromEntries(before.map((clip) => [clip.clipId, clip])));
  });
}

test("canonical selected-clip ripple delete shifts only the selected track and undoes exactly", async ({
  page,
}) => {
  await canonicalWorkspace(page);
  const before = (await snapshot(page)).timelineSnapshot!.clips;
  await page
    .locator(
      '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
    )
    .click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
  await page.locator('[data-h3-nle-control="transport.ripple"]').click();
  await page.locator('[data-h3-nle-control="range.ripple_delete"]').click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(2);
  let after = (await snapshot(page)).timelineSnapshot!.clips;
  expect(after.some((clip) => clip.clipId === "clip-0")).toBe(false);
  expect(after.find((clip) => clip.clipId === "clip-4")).toMatchObject({
    startFrame: 132,
  });
  expect(after.find((clip) => clip.clipId === "clip-5")).toMatchObject({
    startFrame: 180,
  });
  await page.locator('[data-h3-nle-control="history.undo"]').click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(3);
  after = (await snapshot(page)).timelineSnapshot!.clips;
  expect(Object.fromEntries(after.map((clip) => [clip.clipId, clip]))).toEqual(
    Object.fromEntries(before.map((clip) => [clip.clipId, clip])),
  );
});

test.use({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });

for (const shape of ["smoke", "virtualized"]) {
  test(`${shape} five-minute decoded playback meets heap, rendering and virtualization budgets`, async ({
    page,
    context,
  }, testInfo) => {
    test.setTimeout(420_000);
    await decodedWorkspace(page, shape);
    const cdp = await context.newCDPSession(page);
    await cdp.send("Performance.enable");
    const heap = async () => {
      await cdp.send("HeapProfiler.collectGarbage");
      const metrics = await cdp.send("Performance.getMetrics");
      const value = metrics.metrics.find(
        (entry: { name: string }) => entry.name === "JSHeapUsedSize",
      )?.value;
      if (typeof value !== "number" || !Number.isFinite(value))
        throw new Error("heap_instrumentation_unavailable");
      return value;
    };
    const baseline = await heap();
    const parentCommits = (await snapshot(page)).parentCommits;
    await page.evaluate(() => window.nleWorkspaceHarness.resetMeasurements());
    const play = page.getByRole("button", { name: "Play", exact: true });
    const slider = playheadSlider(page);
    await play.click();
    await expect(page.locator('[data-h3-nle-status="monitor"]')).toHaveText(
      "Monitor playing.",
    );
    let playedMs = 0;
    const maxima = { rows: 0, clips: 0, dom: 0 };
    while (playedMs < 300_000) {
      const previousFrame = await playheadFrame(slider);
      const began = Date.now();
      await page.waitForTimeout(3000);
      const stillPlaying =
        (await page.locator('[data-h3-nle-status="monitor"]').textContent()) ===
        "Monitor playing.";
      const nextFrame = await playheadFrame(slider);
      if (stillPlaying) {
        expect(nextFrame).toBeGreaterThan(previousFrame);
        playedMs += Date.now() - began;
      } else {
        expect(nextFrame).toBe((shape === "smoke" ? 120 : 600) * 24 - 1);
        await slider.focus();
        await page.keyboard.press("Home");
        // Wait for the published seek receipt; Play at the old terminal frame is ignored.
        await expect(slider).toHaveAttribute("aria-valuenow", "0");
        await expect(slider).toHaveAttribute("aria-valuetext", "00:00:00:00");
        await expect(play).toBeEnabled();
        await play.click();
        await expect(page.locator('[data-h3-nle-status="monitor"]')).toHaveText(
          "Monitor playing.",
        );
      }
      const counts = await page.locator(surface).evaluate((root) => ({
        rows: root.querySelectorAll("[data-h3-nle-track]").length,
        clips: root.querySelectorAll("[data-h3-nle-clip]").length,
        dom: root.querySelectorAll("*").length,
      }));
      for (const key of ["rows", "clips", "dom"] as const)
        maxima[key] = Math.max(maxima[key], counts[key]);
    }
    await page.getByRole("button", { name: "Pause", exact: true }).click();
    const growth = (await heap()) - baseline;
    const measurements = await snapshot(page);
    const renders = [...measurements.commitDurationsMs].sort((a, b) => a - b);
    expect(renders.length).toBeGreaterThan(100);
    const p95 = renders[Math.ceil(renders.length * 0.95) - 1]!;
    const report = {
      shape,
      playedMs,
      heapGrowthBytes: growth,
      renderP95Ms: p95,
      commits: renders.length,
      parentCommits: measurements.parentCommits - parentCommits,
      maxima,
      owners: measurements.mediaOwnership,
    };
    console.log(JSON.stringify(report));
    await testInfo.attach("five-minute-budgets", {
      body: JSON.stringify(report),
      contentType: "application/json",
    });
    expect(growth).toBeLessThanOrEqual(128 * 1024 * 1024);
    expect(p95).toBeLessThanOrEqual(16);
    expect(report.parentCommits).toBe(0);
    expect(maxima.rows).toBeLessThanOrEqual(8);
    expect(maxima.clips).toBeLessThanOrEqual(96);
    expect(maxima.dom).toBeLessThanOrEqual(1500);
    await page.locator('[data-h3-nle-action="close"]').click();
    await expect
      .poll(async () => (await snapshot(page)).mediaOwnership.live)
      .toBe(0);
    await cdp.detach();
  });
}

test("decoded discrete edits stay within the frozen render and handler budgets", async ({
  page,
}, testInfo) => {
  await decodedWorkspace(page, "smoke", true);
  await page
    .locator(
      '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
    )
    .click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
  const commits: number[] = [];
  const renders: number[] = [];
  const handlers: number[] = [];
  for (let index = 0; index < 10; index++) {
    await expect(
      page.getByRole("button", { name: "Play", exact: true }),
    ).toBeEnabled();
    await page.waitForTimeout(100);
    const inspector = page.locator('[data-h3-nle-selected-clip="clip-0"]');
    const input = inspector.getByRole("spinbutton", {
      name: "Position X (%)",
      exact: true,
    });
    // M25-64 (R10): 1 % is the 100 bp this series has always alternated with 0.
    await input.fill(index % 2 === 0 ? "1" : "0");
    await page.waitForTimeout(100);
    await page.evaluate(() => window.nleWorkspaceHarness.resetMeasurements());
    await inspector.locator('[data-h3-nle-control="visual.transform"]').click();
    await expect
      .poll(async () => (await snapshot(page)).receipts)
      .toBe(index + 2);
    await expect(
      page.getByRole("button", { name: "Play", exact: true }),
    ).toBeEnabled();
    await page.waitForTimeout(100);
    const measurements = await snapshot(page);
    if (index < 2)
      console.log(
        JSON.stringify({ editCommitStates: measurements.commitStates }),
      );
    commits.push(measurements.commitDurationsMs.length);
    renders.push(...measurements.commitDurationsMs);
    handlers.push(...measurements.handlerDurationsMs);
  }
  const p95 = (values: number[]) =>
    [...values].sort((a, b) => a - b)[Math.ceil(values.length * 0.95) - 1]!;
  await testInfo.attach("discrete-edit-budgets", {
    body: JSON.stringify({
      commits,
      renderP95: p95(renders),
      handlerP95: p95(handlers),
    }),
    contentType: "application/json",
  });
  expect(renders.length).toBeGreaterThan(0);
  expect(handlers).toHaveLength(10);
  expect(Math.max(...commits)).toBeLessThanOrEqual(4);
  expect(p95(renders)).toBeLessThanOrEqual(16);
  expect(p95(handlers)).toBeLessThanOrEqual(50);
});

for (const operation of ["overwrite"] as const) {
  test(`canonical ${operation} splits only the spanning clip with bounded collision-free IDs`, async ({
    page,
  }) => {
    const longId = "i".repeat(64);
    await routeGenericFixtureMedia(page);
    const transactions = await canonicalWorkspace(
      page,
      (wire) => {
        const clips = wire.clips as Record<string, unknown>[];
        clips.find((clip) => clip.clip_id === "clip-2")!.clip_id = longId;
        clips.find((clip) => clip.clip_id === "clip-6")!.clip_id =
          "remainder-r11-1";
      },
      undefined,
      "&media=1",
    );
    const ruler = playheadSlider(page);
    await expect(ruler).toHaveAttribute("aria-disabled", "false");
    await seekPlayhead(page, ruler, 12);
    await page.locator('[data-h3-nle-pane="assets"]').click();
    // Media owns range insertion: the image card's one-frame extent splits its first compatible
    // occupied track. Preserve the long-id/collision proof without reviving inspector fields.
    await chooseAssetCommand(
      page,
      page.locator('[data-h3-nle-card-index="2"]'),
      `range.${operation}`,
    );
    await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
    const after = (await snapshot(page)).timelineSnapshot!.clips;
    expect(after.find((clip) => clip.clipId === longId)).toMatchObject({
      durationFrames: 12,
    });
    const right = after.find((clip) => clip.clipId === "remainder-r11-2");
    expect(right).toMatchObject({
      startFrame: 13,
      durationFrames: 35,
      trackId: "track-2",
    });
    expect(transactions).toMatchObject([
      {
        commands: [
          { payload: { remainder_ids: { [longId]: "remainder-r11-2" } } },
        ],
      },
    ]);
  });
}

for (const title of [false, true]) {
  test(`canonical ${title ? "title" : "asset"} insertion adds one clip without replacing existing clips`, async ({
    page,
  }) => {
    await routeGenericFixtureMedia(page);
    await canonicalWorkspace(page, undefined, undefined, "&media=1");
    await expect(
      page.getByRole("button", { name: "Play", exact: true }),
    ).toBeEnabled();
    const before = (await snapshot(page)).timelineSnapshot!.clips;
    const primary = (await snapshot(page)).timelineSnapshot!.tracks.find(
      (track) => track.kind === "primary_video",
    )!;
    const primaryEnd = Math.max(
      0,
      ...before
        .filter((clip) => clip.trackId === primary.trackId && clip.enabled)
        .map((clip) => clip.startFrame + clip.durationFrames),
    );
    const ruler = playheadSlider(page);
    await ruler.focus();
    await page.keyboard.press("Home");
    await page.keyboard.press("PageUp");
    await page.keyboard.press("PageUp");
    for (let frame = 48; frame < 60; frame += 1)
      await page.keyboard.press("ArrowRight");
    await expect.poll(() => playheadFrame(ruler)).toBe(60);
    await page
      .locator(`[data-h3-nle-pane="${title ? "text" : "assets"}"]`)
      .click();
    if (title)
      await page
        .locator('.h3-nle-text-bin input[type="text"]')
        .fill("Inserted title");
    await page
      .locator(`[data-h3-nle-control="${title ? "title" : "asset"}.insert"]`)
      .first()
      .click();
    await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
    const after = (await snapshot(page)).timelineSnapshot!.clips;
    expect(after).toHaveLength(before.length + 1);
    const added = after.find(
      (clip) => !before.some((old) => old.clipId === clip.clipId),
    )!;
    expect(added).toMatchObject({
      startFrame: title ? 60 : primaryEnd,
      durationFrames: title ? 24 : 48,
      trackId: title ? "track-3" : "track-0",
    });
    if (title) expect(added.text).toMatchObject({ content: "Inserted title" });
    else expect(added.assetId).toBe("vid-primary");
    expect(
      Object.fromEntries(
        after
          .filter((clip) => clip.clipId !== added.clipId)
          .map((clip) => [clip.clipId, clip]),
      ),
    ).toEqual(Object.fromEntries(before.map((clip) => [clip.clipId, clip])));
  });
}

test("canonical multi-selection moves both selected clips in one transaction", async ({
  page,
}) => {
  await canonicalWorkspace(page);
  const select = (id: string) =>
    page.locator(
      `[data-h3-nle-clip="${id}"] [data-h3-nle-control="selection.set"]`,
    );
  await select("clip-0").click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
  await select("clip-1").click({ modifiers: ["Shift"] });
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(2);
  await openTimelineMore(page);
  await page.locator('[data-h3-nle-alternative="move.open"]').click();
  await page
    .getByRole("button", { name: "Move later by one grid step" })
    .click();
  await page.locator('[data-h3-nle-control="clip.move_group"]').click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(3);
  const after = (await snapshot(page)).timelineSnapshot!.clips;
  expect(
    after
      .filter((clip) => ["clip-0", "clip-1"].includes(clip.clipId))
      .map((clip) => clip.startFrame),
  ).toEqual([51, 51]);
  expect(after.find((clip) => clip.clipId === "clip-2")!.startFrame).toBe(0);
  expect((await snapshot(page)).intents.at(-1)!.commands).toHaveLength(1);
});

test("direct clip pointer drag batches selection and one accepted move transaction", async ({
  page,
}) => {
  const transactions = await canonicalWorkspace(page);
  const before = (await snapshot(page)).timelineSnapshot!.clips.find(
    (member) => member.clipId === "clip-0",
  )!;
  const clip = page.locator(
    '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
  );
  const box = (await clip.boundingBox())!;
  const x = box.x + box.width / 2;
  const y = box.y + box.height / 2;
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x + 15, y, { steps: 5 });
  await expect(page.locator('[data-h3-nle-status="move"]')).toHaveAttribute(
    "data-h3-nle-move-phase",
    "dragging",
  );
  expect((await snapshot(page)).intents).toHaveLength(0);
  await page.mouse.up();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
  const sent = (await snapshot(page)).intents.at(-1)!;
  expect(sent.commands.map((command) => command.kind)).toEqual([
    "select_clips",
    "move_clip",
  ]);
  expect(transactions).toHaveLength(1);
  const moved = (await snapshot(page)).timelineSnapshot!.clips.find(
    (member) => member.clipId === "clip-0",
  )!;
  expect(moved.startFrame).toBeGreaterThan(before.startFrame);
  await page.locator('[data-h3-nle-control="history.undo"]').click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(2);
  expect(
    (await snapshot(page)).timelineSnapshot!.clips.find(
      (member) => member.clipId === "clip-0",
    ),
  ).toEqual(before);
  await page.locator('[data-h3-nle-control="history.redo"]').click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(3);
  expect(
    (await snapshot(page)).timelineSnapshot!.clips.find(
      (member) => member.clipId === "clip-0",
    ),
  ).toEqual(moved);
  expect(transactions).toHaveLength(3);
});

test("sub-threshold pointer movement stays a selection click without an edit", async ({
  page,
}) => {
  const transactions = await canonicalWorkspace(page);
  const clip = page.locator(
    '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
  );
  const box = (await clip.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2 + 3, box.y + box.height / 2);
  await page.mouse.up();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
  expect(transactions).toHaveLength(1);
  expect(
    (transactions[0] as { commands: { kind: string }[] }).commands.map(
      (command) => command.kind,
    ),
  ).toEqual(["select_clips"]);
});

test("Alt suspends the visible pointer snap line without sending an edit", async ({
  page,
}) => {
  await routeGenericFixtureMedia(page);
  const transactions = await canonicalWorkspace(
    page,
    undefined,
    undefined,
    "&media=1",
  );
  await page.locator('[data-h3-nle-control="transport.snap"]').click();
  const clip = page.locator(
    '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
  );
  const canvas = page.locator('[data-h3-nle-canvas="timeline_decoration"]');
  const box = (await clip.boundingBox())!;
  const x = box.x + box.width / 2;
  const y = box.y + box.height / 2;
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x + 130, y, { steps: 4 });
  await expect(canvas).toHaveAttribute("data-h3-nle-snap-line", "present");
  await page.keyboard.down("Alt");
  await page.mouse.move(x + 130, y);
  await expect(canvas).toHaveAttribute("data-h3-nle-snap-line", "absent");
  await page.keyboard.up("Alt");
  await page.keyboard.press("Escape");
  await page.mouse.up();
  expect(transactions).toHaveLength(0);
});

test("incompatible cross-track pointer drag paints a refused ghost and sends nothing", async ({
  page,
}) => {
  const transactions = await canonicalWorkspace(page);
  const clip = page.locator(
    '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
  );
  const box = (await clip.boundingBox())!;
  const before = transactions.length;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(
    box.x + box.width / 2 + 12,
    box.y + box.height / 2 + 112,
    {
      steps: 6,
    },
  );
  await expect(page.locator('[data-h3-nle-status="move"]')).toHaveAttribute(
    "data-code",
    "incompatible_track",
  );
  await expect(
    page.locator('[data-h3-nle-canvas="timeline_decoration"]'),
  ).toHaveAttribute("data-h3-nle-ghost", "refused");
  await page.keyboard.press("Escape");
  await page.mouse.up();
  expect(transactions).toHaveLength(before);
});

test("pointer auto-scroll advances the view while cancellation sends no edit", async ({
  page,
}) => {
  const transactions = await canonicalWorkspace(page);
  const clip = page.locator(
    '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
  );
  await clip.click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
  await openTimelineMore(page);
  await page
    .locator('[data-h3-nle-control="transport.zoom_selection"]')
    .click();
  const before = transactions.length;
  const clipBox = (await clip.boundingBox())!;
  const grid = page.getByRole("grid", { name: "Timeline tracks", exact: true });
  const gridBox = (await grid.boundingBox())!;
  await page.mouse.move(
    clipBox.x + clipBox.width / 2,
    clipBox.y + clipBox.height / 2,
  );
  await page.mouse.down();
  await page.mouse.move(
    gridBox.x + gridBox.width - 2,
    clipBox.y + clipBox.height / 2,
    {
      steps: 8,
    },
  );
  await expect
    .poll(async () =>
      Number(
        await page
          .locator('[data-h3-nle-region="timeline"]')
          .getAttribute("data-h3-nle-view-start"),
      ),
    )
    .toBeGreaterThan(0);
  await page.keyboard.press("Escape");
  await page.mouse.up();
  expect(transactions).toHaveLength(before);
});

test("explicit keyboard edit mode moves one clip across tracks in one transaction", async ({
  page,
}) => {
  const transactions = await canonicalWorkspace(page);
  const clip = page.locator(
    '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
  );
  await clip.click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
  await clip.focus();
  await page.keyboard.press("Enter");
  await page.keyboard.press("Shift+ArrowRight");
  await page.keyboard.press("Alt+ArrowDown");
  expect(transactions).toHaveLength(1);
  await page.keyboard.press("Enter");
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(2);
  expect(transactions.at(-1)).toMatchObject({
    commands: [
      {
        kind: "move_clip",
        payload: {
          clip_id: "clip-0",
          delta_frames: 51,
          target_track_id: "track-1",
        },
      },
    ],
  });
  expect(
    (await snapshot(page)).timelineSnapshot!.clips.find(
      (member) => member.clipId === "clip-0",
    ),
  ).toMatchObject({ startFrame: 51, trackId: "track-1" });
});

test("Shift arrow selects outside edit mode and moves only inside an explicit draft", async ({
  page,
}) => {
  const transactions = await canonicalWorkspace(page);
  const clip = page.locator(
    '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
  );
  await clip.click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
  await clip.focus();
  await page.keyboard.press("Shift+ArrowRight");
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(2);
  expect(
    (
      (await snapshot(page)).intents.at(-1)!.commands[0]!.payload as {
        clip_ids: string[];
      }
    ).clip_ids,
  ).toEqual(["clip-0", "clip-4"]);
  await page.keyboard.press("Enter");
  await page.keyboard.press("Shift+ArrowRight");
  expect(transactions).toHaveLength(2);
  await page.keyboard.press("Enter");
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(3);
  expect(transactions.at(-1)).toMatchObject({
    commands: [
      {
        kind: "move_group",
        payload: { clip_ids: ["clip-0", "clip-4"], delta_frames: 51 },
      },
    ],
  });
});

test("keyboard move no-op and Escape cancel issue zero edit transactions", async ({
  page,
}) => {
  const transactions = await canonicalWorkspace(page);
  const clip = page.locator(
    '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
  );
  await clip.focus();
  await page.keyboard.press("Enter");
  await page.keyboard.press("Enter");
  expect(transactions).toHaveLength(0);
  await page.keyboard.press("Enter");
  await page.keyboard.press("ArrowRight");
  await page.keyboard.press("Escape");
  expect(transactions).toHaveLength(0);
  await expect(page.locator('[data-h3-nle-status="move"]')).toHaveAttribute(
    "data-h3-nle-move-phase",
    "idle",
  );
});

test("valid keyboard move reports a distinct server conflict without optimistic geometry", async ({
  page,
}) => {
  let interleaved = false;
  const transactions = await canonicalWorkspace(
    page,
    undefined,
    (transaction, history) => {
      if (
        interleaved ||
        (transaction.commands as { kind: string }[])[0]!.kind !== "move_clip"
      )
        return;
      interleaved = true;
      history.push({
        ...transaction,
        request_id: "concurrent-move-conflict",
        transaction_id: "tx-concurrent-move-conflict",
        commands: [
          {
            kind: "move_clip",
            payload: {
              clip_id: "clip-0",
              delta_frames: 2,
              target_track_id: "track-0",
            },
          },
        ],
      });
    },
  );
  await page
    .locator(
      '[data-h3-nle-clip="clip-1"] [data-h3-nle-control="selection.set"]',
    )
    .click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
  const clip = page.locator(
    '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
  );
  await clip.click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(2);
  await clip.focus();
  await page.keyboard.press("Enter");
  await page.keyboard.press("ArrowRight");
  await expect(page.locator('[data-h3-nle-status="move"]')).toHaveAttribute(
    "data-h3-nle-move-phase",
    "keyboard_draft",
  );
  await page.keyboard.press("Enter");
  await expect(page.locator('[data-h3-nle-status="move"]')).toHaveAttribute(
    "data-h3-nle-move-phase",
    "rejected",
  );
  expect((await snapshot(page)).receipts).toBe(2);
  expect(
    (await snapshot(page)).timelineSnapshot!.clips.find(
      (member) => member.clipId === "clip-0",
    )!.startFrame,
  ).toBe(2);
  expect(transactions).toHaveLength(4);
});

test("lost move reply reconciles read-only without replaying the accepted transaction", async ({
  page,
}) => {
  const transactions = await canonicalWorkspace(
    page,
    undefined,
    undefined,
    "&loseMoveReply=1",
  );
  const clip = page.locator(
    '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
  );
  await clip.focus();
  await page.keyboard.press("Enter");
  await page.keyboard.press("ArrowRight");
  await page.keyboard.press("Enter");
  await expect(page.locator('[data-h3-nle-status="move"]')).toHaveAttribute(
    "data-h3-nle-move-phase",
    "reconciling",
  );
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
  await expect
    .poll(
      async () =>
        (await snapshot(page)).timelineSnapshot!.clips.find(
          (member) => member.clipId === "clip-0",
        )!.startFrame,
    )
    .toBe(1);
  expect(transactions).toHaveLength(1);
});

test("ruler, zoom, pan and snap stay local while the ruler drives the monitor", async ({
  page,
}) => {
  await decodedWorkspace(page, "smoke");
  const ruler = page.getByRole("slider", { name: "Playhead", exact: true });
  await ruler.focus();
  await page.keyboard.press("ArrowRight");
  await expect(ruler).toHaveAttribute("aria-valuenow", "1");
  await seekPlayhead(page, ruler, 48);
  await expect(ruler).toHaveAttribute("aria-valuenow", "48");
  await expect(
    page.locator('[data-h3-nle-canvas="composition"]'),
  ).toHaveAttribute("data-h3-nle-presented-frame", "48");
  await page.locator('[data-h3-nle-control="transport.zoom_in"]').click();
  await page.locator('[data-h3-nle-control="transport.zoom_out"]').click();
  await page.locator('[data-h3-nle-control="transport.snap"]').click();
  await openTimelineMore(page);
  await page.locator('[data-h3-nle-alternative="pan.later"]').click();
  expect((await snapshot(page)).intents).toHaveLength(0);
  await expect(page.locator('[data-h3-nle-canvas="composition"]')).toHaveCount(
    1,
  );
  await expect(
    page.locator('[data-h3-nle-canvas="timeline_decoration"]'),
  ).toHaveCount(1);
});

test("click-only additive, range, all and clear selection preserve exact accepted IDs", async ({
  page,
}) => {
  await canonicalWorkspace(page);
  const accepted = (await snapshot(page)).timelineSnapshot!;
  const trackOrder = new Map(
    accepted.tracks.map((track) => [track.trackId, track.order]),
  );
  const logicalIds = [...accepted.clips]
    .sort(
      (left, right) =>
        (trackOrder.get(left.trackId) ?? 0) -
          (trackOrder.get(right.trackId) ?? 0) ||
        left.startFrame - right.startFrame ||
        left.clipId.localeCompare(right.clipId),
    )
    .map((clip) => clip.clipId);
  const select = (id: string) =>
    page.locator(
      `[data-h3-nle-clip="${id}"] [data-h3-nle-control="selection.set"]`,
    );
  await select("clip-0").click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
  await openTimelineMore(page);
  await page.locator('[data-h3-nle-alternative="selection.additive"]').click();
  await select("clip-1").click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(2);
  expect(
    (
      (await snapshot(page)).intents.at(-1)!.commands[0]!.payload as {
        clip_ids: string[];
      }
    ).clip_ids,
  ).toEqual(["clip-0", "clip-1"]);

  await page.locator('[data-h3-nle-alternative="selection.clear"]').click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(3);
  await select("clip-0").click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(4);
  await page.locator('[data-h3-nle-alternative="selection.range"]').click();
  await select("clip-1").click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(5);
  expect(
    (
      (await snapshot(page)).intents.at(-1)!.commands[0]!.payload as {
        clip_ids: string[];
      }
    ).clip_ids,
  ).toEqual(
    logicalIds.slice(
      Math.min(logicalIds.indexOf("clip-0"), logicalIds.indexOf("clip-1")),
      Math.max(logicalIds.indexOf("clip-0"), logicalIds.indexOf("clip-1")) + 1,
    ),
  );

  await page.locator('[data-h3-nle-alternative="selection.all"]').click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(6);
  expect(
    (
      (await snapshot(page)).intents.at(-1)!.commands[0]!.payload as {
        clip_ids: string[];
      }
    ).clip_ids,
  ).toHaveLength(accepted.clips.length);
  await page.locator('[data-h3-nle-alternative="selection.clear"]').click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(7);
  expect(
    (
      (await snapshot(page)).intents.at(-1)!.commands[0]!.payload as {
        clip_ids: string[];
      }
    ).clip_ids,
  ).toEqual([]);
});

test("background marquee commits one exact selection transaction", async ({
  page,
}) => {
  const transactions = await canonicalWorkspace(page);
  const grid = page.getByRole("grid", { name: "Timeline tracks", exact: true });
  const bounds = (await grid.boundingBox())!;
  const before = transactions.length;
  // M25-62: the 112 px header moved every lane 48 px left, so the grid's right edge now lands on
  // clip-28. The press starts in the empty lane after clip-24 and ends just past the header, as
  // the 160 px layout's `x + 161` did.
  const gapAfter = (await page
    .locator('[data-h3-nle-clip="clip-24"]')
    .boundingBox())!;
  const header = (await nleLaneOrigin(page)) - 16;
  await page.mouse.move(gapAfter.x + gapAfter.width + 24, bounds.y + 8);
  await page.mouse.down();
  await page.mouse.move(bounds.x + header + 1, bounds.y + 50, { steps: 5 });
  await page.mouse.up();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
  expect(transactions).toHaveLength(before + 1);
  expect(transactions.at(-1)).toMatchObject({
    commands: [
      { kind: "select_clips", payload: { clip_ids: expect.any(Array) } },
    ],
  });
  expect(
    (transactions.at(-1) as { commands: { payload: { clip_ids: string[] } }[] })
      .commands[0]!.payload.clip_ids.length,
  ).toBeGreaterThan(0);
});

test("virtualized row marquees stay off the scrollbar and each commit", async ({
  page,
}) => {
  const transactions = await canonicalWorkspace(
    page,
    undefined,
    undefined,
    "&shape=virtualized",
  );
  const grid = page.getByRole("grid", {
    name: "Timeline tracks",
    exact: true,
  });
  await grid.evaluate((element) => {
    element.scrollTop = 0;
  });
  const firstClip = grid
    .locator('[data-h3-nle-control="selection.set"]')
    .first();
  await firstClip.click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);

  for (const [index, row] of [0, 1, 2, 3, 0].entries()) {
    await openTimelineMore(page);
    const clearSelection = page.locator(
      '[data-h3-nle-alternative="selection.clear"]',
    );
    await expect(clearSelection).toBeEnabled();
    await clearSelection.click();
    await closeTimelineMore(page);
    await expect
      .poll(async () => (await snapshot(page)).receipts)
      .toBe(index * 2 + 2);

    const lane = grid.getByRole("gridcell").nth(row);
    // IMPORTANT: overscan rows are attached before they are physically inside the clipped grid.
    // Reveal the lane first so its coordinates cannot target the workspace resize control below.
    await lane.scrollIntoViewIfNeeded();
    const box = (await lane.boundingBox())!;
    const y = box.y + box.height / 2;
    await page.mouse.move(box.x + box.width - 30, y);
    await page.mouse.down();
    await page.mouse.move(box.x + 2, y, { steps: 4 });
    await page.mouse.up();
    await expect
      .poll(async () => (await snapshot(page)).receipts)
      .toBe(index * 2 + 3);
  }

  expect(transactions).toHaveLength(11);
  for (const transaction of transactions.slice(1))
    expect(transaction).toMatchObject({
      commands: [{ kind: "select_clips" }],
    });
});

test.describe("coarse pointer", () => {
  // M25-62 (R2, R7): the 44 px rail is the coarse-pointer rule, so this case runs under touch.
  // The fine rail (30 px, below 24 px clips) is pinned by m25_62TimelineSurface A62-4.
  test.use({ hasTouch: true });

  test("short clip click-only navigation and selection zoom keep reachable 44px trim rails", async ({
    page,
  }) => {
    await canonicalWorkspace(page, undefined, undefined, "&shape=virtualized");
    await page.locator('[data-h3-nle-control="transport.zoom_fit"]').click();
    const first = page.locator('[data-h3-nle-control="selection.set"]').first();
    await first.focus();
    await openTimelineMore(page);
    await page.locator('[data-h3-nle-control="selection.next"]').click();
    await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
    const selectedId = (
      (await snapshot(page)).intents.at(-1)!.commands[0]!.payload as {
        clip_ids: string[];
      }
    ).clip_ids[0]!;
    const selected = page.locator(`[data-h3-nle-clip="${selectedId}"]`);
    expect((await selected.boundingBox())!.width).toBeLessThan(24);
    const grips = page.locator(".h3-nle-trim-rail [data-h3-nle-trim-edge]");
    await expect(grips).toHaveCount(2);
    for (const grip of await grips.all()) {
      const box = (await grip.boundingBox())!;
      expect(box.width).toBeGreaterThanOrEqual(44);
      expect(box.height).toBeGreaterThanOrEqual(44);
    }
    // B-M2563-09: with the rail under the surface, the lanes stay clear of the 44 px grip. The
    // More menu, still open from selection.next, would itself cover the lanes' corner.
    await closeTimelineMore(page);
    await expectGripClearOfLanes(page);
    const grid = page.getByRole("grid", {
      name: "Timeline tracks",
      exact: true,
    });
    const laneWidth =
      (await grid.evaluate((element) => element.clientWidth)) -
      (await nleLaneOrigin(page));
    await openTimelineMore(page);
    await page
      .locator('[data-h3-nle-control="transport.zoom_selection"]')
      .click();
    const zoomed = (await selected.boundingBox())!.width;
    expect(zoomed).toBeCloseTo(laneWidth * 0.6, 0);
  });
});

test("smoke and narrow timeline fit use the continuous formula and stop zoom-out at the bound", async ({
  page,
}) => {
  await canonicalWorkspace(page);
  const timeline = page.locator('[data-h3-nle-region="timeline"]');
  const grid = page.getByRole("grid", { name: "Timeline tracks", exact: true });
  const fit = page.locator('[data-h3-nle-control="transport.zoom_fit"]');
  await fit.click();
  let laneWidth =
    (await grid.evaluate((element) => element.clientWidth)) -
    (await nleLaneOrigin(page));
  let scale = Number(
    await timeline.getAttribute("data-h3-nle-pixels-per-frame"),
  );
  expect(scale).toBeCloseTo(Math.max(0.25, (laneWidth - 24) / 2_880), 2);
  expect(scale).toBeGreaterThan(0.25);
  await page.locator('[data-h3-nle-control="transport.zoom_out"]').click();
  expect(
    Number(await timeline.getAttribute("data-h3-nle-pixels-per-frame")),
  ).toBe(0.25);

  await page.setViewportSize({ width: 760, height: 760 });
  await fit.click();
  laneWidth =
    (await grid.evaluate((element) => element.clientWidth)) -
    (await nleLaneOrigin(page));
  scale = Number(await timeline.getAttribute("data-h3-nle-pixels-per-frame"));
  expect(scale).toBeCloseTo(Math.max(0.25, (laneWidth - 24) / 2_880), 2);
});

test("virtualized fit retains all 128 selected IDs and promotes the logical end target", async ({
  page,
}) => {
  await canonicalWorkspace(page, undefined, undefined, "&shape=virtualized");
  await page.locator('[data-h3-nle-control="transport.zoom_fit"]').click();
  const timeline = page.locator('[data-h3-nle-region="timeline"]');
  const grid = page.getByRole("grid", { name: "Timeline tracks", exact: true });
  const laneWidth =
    (await grid.evaluate((element) => element.clientWidth)) -
    (await nleLaneOrigin(page));
  const scale = Number(
    await timeline.getAttribute("data-h3-nle-pixels-per-frame"),
  );
  expect(scale).toBeCloseTo(Math.max(0.25, (laneWidth - 24) / 14_400), 2);
  expect(Number(await timeline.getAttribute("data-h3-nle-view-start"))).toBe(0);
  await grid.focus();
  await page.keyboard.press("Control+a");
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
  const selection = (await snapshot(page)).intents.at(-1)!.commands[0]!;
  expect(selection.kind).toBe("select_clips");
  expect((selection.payload as { clip_ids: string[] }).clip_ids).toHaveLength(
    128,
  );
  await expect(grid).toHaveAttribute(
    "data-h3-nle-mounted-clips",
    /^(?:[1-8]?\d|9[0-6])$/,
  );
  const first = page.locator('[data-h3-nle-control="selection.set"]').first();
  const accepted = (await snapshot(page)).timelineSnapshot!;
  const order = new Map(
    accepted.tracks.map((track) => [track.trackId, track.order]),
  );
  const logicalEnd = [...accepted.clips]
    .sort(
      (left, right) =>
        (order.get(left.trackId) ?? 0) - (order.get(right.trackId) ?? 0) ||
        left.startFrame - right.startFrame ||
        left.clipId.localeCompare(right.clipId),
    )
    .at(-1)!.clipId;
  await first.focus();
  await page.keyboard.press("End");
  await expect(
    page.locator(
      `[data-h3-nle-clip="${logicalEnd}"] [data-h3-nle-control="selection.set"]`,
    ),
  ).toBeFocused();
});

async function selectLogicalRange(page: Page, count: number) {
  const accepted = (await snapshot(page)).timelineSnapshot!;
  const order = new Map(
    accepted.tracks.map((track) => [track.trackId, track.order]),
  );
  const clips = [...accepted.clips].sort(
    (left, right) =>
      (order.get(left.trackId) ?? 0) - (order.get(right.trackId) ?? 0) ||
      left.startFrame - right.startFrame ||
      left.clipId.localeCompare(right.clipId),
  );
  const button = (clipId: string) =>
    page.locator(
      `[data-h3-nle-clip="${clipId}"] [data-h3-nle-control="selection.set"]`,
    );
  const initialReceipts = (await snapshot(page)).receipts;
  await button(clips[0]!.clipId).click();
  await expect
    .poll(async () => (await snapshot(page)).receipts)
    .toBe(initialReceipts + 1);
  await openTimelineMore(page);
  await page.locator('[data-h3-nle-alternative="selection.range"]').click();
  await closeTimelineMore(page);
  const target = clips[count - 1]!;
  const targetTrackOrder = order.get(target.trackId) ?? 0;
  // The fit window can contain all 128 fixture clips while the editor intentionally mounts
  // only the 96 nearest clips. Preserve click-only range selection by bringing its endpoint
  // into the rendered frame window instead of assuming offscreen clip DOM is mounted.
  await page.locator('[data-h3-nle-control="transport.zoom_in"]').click();
  await openTimelineMore(page);
  const horizontalScroll = page.locator(
    '[data-h3-nle-control="transport.scroll"]',
  );
  await horizontalScroll.focus();
  await horizontalScroll.press(
    target.startFrame * 2 > Number(accepted.output.durationFrames)
      ? "End"
      : "Home",
  );
  await closeTimelineMore(page);
  await page
    .getByRole("grid", { name: "Timeline tracks" })
    .evaluate((grid, top) => {
      grid.scrollTop = top;
    }, targetTrackOrder * 56);
  await expect(button(target.clipId)).toBeAttached();
  await button(target.clipId).click();
  await expect
    .poll(async () => (await snapshot(page)).receipts)
    .toBe(initialReceipts + 2);
  return initialReceipts + 2;
}

test("click-only group move admits 32 members atomically", async ({ page }) => {
  const transactions = await canonicalWorkspace(
    page,
    undefined,
    undefined,
    "&shape=virtualized",
  );
  await page.locator('[data-h3-nle-control="transport.zoom_fit"]').click();
  const before = new Map(
    (await snapshot(page)).timelineSnapshot!.clips.map((clip) => [
      clip.clipId,
      { startFrame: clip.startFrame, trackId: clip.trackId },
    ]),
  );
  const selectionReceipts = await selectLogicalRange(page, 32);
  await openTimelineMore(page);
  await page.locator('[data-h3-nle-alternative="move.open"]').click();
  await page
    .getByRole("button", { name: "Move later by one grid step" })
    .click();
  await page.locator('[data-h3-nle-control="clip.move_group"]').click();
  await expect
    .poll(async () => (await snapshot(page)).receipts)
    .toBe(selectionReceipts + 1);
  expect(transactions.at(-1)).toMatchObject({
    commands: [
      { kind: "move_group", payload: { clip_ids: expect.any(Array) } },
    ],
  });
  const payload = (
    transactions.at(-1) as {
      commands: {
        payload: {
          clip_ids: string[];
          delta_frames: number;
          target_track_ids: string[];
        };
      }[];
    }
  ).commands[0]!.payload;
  expect(payload.clip_ids).toHaveLength(32);
  expect(payload.delta_frames).toBeGreaterThan(0);
  expect(payload.target_track_ids).toEqual(
    payload.clip_ids.map((clipId) => before.get(clipId)!.trackId),
  );
  const after = new Map(
    (await snapshot(page)).timelineSnapshot!.clips.map((clip) => [
      clip.clipId,
      { startFrame: clip.startFrame, trackId: clip.trackId },
    ]),
  );
  for (const clipId of payload.clip_ids) {
    expect(after.get(clipId)).toEqual({
      startFrame: before.get(clipId)!.startFrame + payload.delta_frames,
      trackId: before.get(clipId)!.trackId,
    });
  }
});

test("click-only group move refuses 33 members without truncation or a request", async ({
  page,
}) => {
  const transactions = await canonicalWorkspace(
    page,
    undefined,
    undefined,
    "&shape=virtualized",
  );
  await page.locator('[data-h3-nle-control="transport.zoom_fit"]').click();
  await selectLogicalRange(page, 33);
  const before = transactions.length;
  await openTimelineMore(page);
  await page.locator('[data-h3-nle-alternative="move.open"]').click();
  await page
    .getByRole("button", { name: "Move later by one grid step" })
    .click();
  // B-M2561-08: the refusal was cancelled as `owner_removed` whenever the draft's first origin
  // was virtualized out of the view, which depended on the header width and the scroll the
  // selection walk left. Scroll to the end so clip-0 is certainly unmounted before applying.
  const horizontalScroll = page.locator(
    '[data-h3-nle-control="transport.scroll"]',
  );
  await horizontalScroll.focus();
  await horizontalScroll.press("End");
  await expect(page.locator('[data-h3-nle-clip="clip-0"]')).toHaveCount(0);
  await page.locator('[data-h3-nle-control="clip.move_group"]').click();
  await expect(page.locator('[data-h3-nle-status="move"]')).toHaveAttribute(
    "data-code",
    "group_limit",
  );
  expect(transactions).toHaveLength(before);
  expect(
    (
      (await snapshot(page)).intents.at(-1)!.commands[0]!.payload as {
        clip_ids: string[];
      }
    ).clip_ids,
  ).toHaveLength(33);
});

test("locked target refuses keyboard cross-track movement without a request", async ({
  page,
}) => {
  const transactions = await canonicalWorkspace(page);
  await page
    .locator(
      '[data-h3-nle-track="track-1"] [data-h3-nle-control="track.locked"]',
    )
    .click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
  const clip = page.locator(
    '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
  );
  await clip.click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(2);
  await clip.focus();
  await page.keyboard.press("Enter");
  await page.keyboard.press("Shift+ArrowRight");
  await page.keyboard.press("Alt+ArrowDown");
  await expect(page.locator('[data-h3-nle-status="move"]')).toHaveAttribute(
    "data-code",
    "locked_track",
  );
  const before = transactions.length;
  await page.keyboard.press("Enter");
  expect(transactions).toHaveLength(before);
});

test("canonical ripple trim shifts later clips only on its scoped track", async ({
  page,
}) => {
  await routeGenericFixtureMedia(page);
  await canonicalWorkspace(page, undefined, undefined, "&media=1");
  await page
    .locator(
      '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
    )
    .click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
  const ruler = playheadSlider(page);
  await expect(ruler).toHaveAttribute("aria-disabled", "false");
  await seekPlayhead(page, ruler, 12);
  await page.locator('[data-h3-nle-control="transport.ripple"]').click();
  await page.locator('[data-h3-nle-control="range.ripple_trim"]').click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(2);
  const clips = (await snapshot(page)).timelineSnapshot!.clips;
  expect(clips.find((clip) => clip.clipId === "clip-0")).toMatchObject({
    sourceStartFrame: 12,
    durationFrames: 36,
  });
  expect(clips.find((clip) => clip.clipId === "clip-4")).toMatchObject({
    startFrame: 168,
  });
  expect(clips.find((clip) => clip.clipId === "clip-5")).toMatchObject({
    startFrame: 180,
  });
});

test("canonical transition uses the admitted lower composition layer", async ({
  page,
}) => {
  await routeGenericFixtureMedia(page);
  await canonicalWorkspace(page, undefined, undefined, "&media=1");
  await page
    .locator(
      '[data-h3-nle-clip="clip-1"] [data-h3-nle-control="selection.set"]',
    )
    .click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
  const ruler = playheadSlider(page);
  await expect(ruler).toHaveAttribute("aria-disabled", "false");
  await seekPlayhead(page, ruler, 12);
  await page.locator('[data-h3-nle-control="clip.split"]').click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(2);
  const inspector = page.locator('[data-h3-nle-selected-clip="clip-1"]');
  await revealProperty(page, "boundary.transition");
  await inspector
    .getByRole("combobox", { name: "Transition", exact: true })
    .selectOption("cross_dissolve_v1");
  await inspector
    .getByRole("spinbutton", { name: "Transition frames", exact: true })
    .fill("12");
  await inspector
    .locator('[data-h3-nle-control="boundary.transition"]')
    .click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(3);
  expect(
    (await snapshot(page)).timelineSnapshot!.clips.find(
      (clip) => clip.clipId === "clip-1",
    )!.transition,
  ).toEqual({ kind: "cross_dissolve_v1", durationFrames: 12 });
});

test("canonical attribute conflict requires explicit rebase and preserves concurrent edits", async ({
  page,
}) => {
  let interleaved = false;
  const transactions = await canonicalWorkspace(
    page,
    undefined,
    (transaction, history) => {
      if (
        interleaved ||
        (transaction.commands as { kind: string }[])[0]!.kind !==
          "set_opacity_blend"
      )
        return;
      interleaved = true;
      history.push({
        ...transaction,
        request_id: "concurrent-edit",
        transaction_id: "tx-concurrent-edit",
        commands: [
          {
            kind: "set_clip_enabled",
            payload: { clip_id: "clip-1", enabled: false },
          },
        ],
      });
    },
  );
  await page
    .locator(
      '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
    )
    .click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
  const inspector = page.locator('[data-h3-nle-selected-clip="clip-0"]');
  await revealProperty(page, "visual.opacity_blend");
  await inspector
    .getByRole("spinbutton", { name: "Opacity (%)", exact: true })
    .fill("50");
  await inspector
    .locator('[data-h3-nle-control="visual.opacity_blend"]')
    .click();
  await expect(page.locator('[data-h3-nle-status="timeline"]')).toContainText(
    "rejected",
  );
  expect((await snapshot(page)).receipts).toBe(1);
  expect(
    (await snapshot(page)).timelineSnapshot!.clips.find(
      (clip) => clip.clipId === "clip-0",
    )!.opacityBp,
  ).toBe(10000);
  const rebase = page.locator('[data-h3-nle-control="conflict.rebase"]');
  await expect(rebase).toBeEnabled();
  expect(transactions).toHaveLength(3);
  await rebase.click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(2);
  const clips = (await snapshot(page)).timelineSnapshot!.clips;
  expect(clips.find((clip) => clip.clipId === "clip-0")!.opacityBp).toBe(5000);
  expect(clips.find((clip) => clip.clipId === "clip-1")!.enabled).toBe(false);
  expect(transactions).toHaveLength(4);
});

test("canonical slide and roll preserve total extent of three adjacent clips", async ({
  page,
}) => {
  await routeGenericFixtureMedia(page);
  await canonicalWorkspace(page, undefined, undefined, "&media=1");
  let receipts = 0;
  const select = async (id: string) => {
    const target = page.locator(
      `[data-h3-nle-clip="${id}"] [data-h3-nle-control="selection.set"]`,
    );
    await target.focus();
    await page.keyboard.press("Space");
    await expect
      .poll(async () => (await snapshot(page)).receipts)
      .toBe(++receipts);
  };
  const split = async (id: string) => {
    const before = (await snapshot(page)).timelineSnapshot!.clips;
    const clip = before.find((member) => member.clipId === id)!;
    const ruler = playheadSlider(page);
    await expect(ruler).toHaveAttribute("aria-disabled", "false");
    await seekPlayhead(page, ruler, clip.startFrame + 12);
    await page.locator('[data-h3-nle-control="clip.split"]').click();
    await expect
      .poll(async () => (await snapshot(page)).receipts)
      .toBe(++receipts);
    return (await snapshot(page)).timelineSnapshot!.clips.find(
      (clip) => !before.some((old) => old.clipId === clip.clipId),
    )!.clipId;
  };
  await select("clip-2");
  const middle = await split("clip-2");
  await select(middle);
  const right = await split(middle);
  const menu = await openClipMenu(page, middle);
  await menu.locator('[data-h3-nle-control="clip.slide"]').click();
  await expect
    .poll(async () => (await snapshot(page)).receipts)
    .toBe(++receipts);
  let clips = (await snapshot(page)).timelineSnapshot!.clips;
  expect(clips.find((clip) => clip.clipId === middle)).toMatchObject({
    startFrame: 13,
    durationFrames: 12,
  });
  expect(clips.find((clip) => clip.clipId === right)).toMatchObject({
    startFrame: 25,
    durationFrames: 23,
  });
  await select("clip-2");
  const roll = page.locator(
    '[data-h3-nle-control="boundary.roll"][data-h3-nle-roll-left="clip-2"]',
  );
  await roll.click();
  await page.getByRole("button", { name: "Roll edit +1" }).click();
  await page.getByRole("button", { name: "Apply", exact: true }).click();
  await expect
    .poll(async () => (await snapshot(page)).receipts)
    .toBe(++receipts);
  clips = (await snapshot(page)).timelineSnapshot!.clips;
  expect(clips.find((clip) => clip.clipId === "clip-2")).toMatchObject({
    startFrame: 0,
    durationFrames: 14,
  });
  expect(clips.find((clip) => clip.clipId === middle)).toMatchObject({
    startFrame: 14,
    durationFrames: 11,
  });
  expect(clips.find((clip) => clip.clipId === right)).toMatchObject({
    startFrame: 25,
    durationFrames: 23,
  });
});

test("explicit open, singleton, keyboard focus, resize and close/reopen", async ({
  page,
}) => {
  await open(page);
  await expect(page.locator(`${surface} h2`)).toBeFocused();
  await page.evaluate(() => window.nleWorkspaceHarness.open());
  await expect(page.locator(surface)).toHaveCount(1);
  const resize = page.locator('[data-h3-nle-action="resize"]');
  // M25-44: the workspace opens at the viewport minus the 16 px margin. This file's `test.use`
  // sets 1440 x 900 for every case in it, including this one.
  expect((await snapshot(page)).bounds).toEqual({ width: 1408, height: 868 });
  await resize.focus();
  await page.keyboard.press("ArrowLeft");
  await expect(resize).toBeFocused();
  expect((await snapshot(page)).bounds.width).toBe(1392);
  // The resize grip is the last stop; Tab wraps to the chrome bar's first control, Export.
  await page.keyboard.press("Tab");
  await expect(page.locator('[data-h3-nle-action="export"]')).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(resize).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.locator(surface)).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Open full editor" }),
  ).toBeFocused();
  expect((await snapshot(page)).closeReasons).toEqual(["escape"]);
  await page.getByRole("button", { name: "Open full editor" }).click();
  await page.locator('[data-h3-nle-action="close"]').click();
  await expect(page.locator(surface)).toHaveCount(0);
  expect((await snapshot(page)).released).toBe(2);
});

test("pointer draft issues zero intents until release, then exactly one trim", async ({
  page,
}) => {
  await open(page);
  const handle = page.locator(grip).first();
  const box = await handle.boundingBox();
  expect(box).not.toBeNull();
  const x = box!.x + box!.width / 2;
  const y = box!.y + box!.height / 2;
  expect(
    await handle.evaluate(
      (element, point) => ({
        hit: element.contains(document.elementFromPoint(point.x, point.y)),
        target: document
          .elementFromPoint(point.x, point.y)
          ?.outerHTML.slice(0, 300),
        box: element.getBoundingClientRect().toJSON(),
      }),
      { x, y },
    ),
  ).toMatchObject({ hit: true });
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x - 20, y, { steps: 4 });
  await expect(page.locator('[data-h3-nle-status="trim"]')).toHaveAttribute(
    "data-h3-nle-trim-phase",
    "dragging",
  );
  expect((await snapshot(page)).intents).toHaveLength(0);
  await page.mouse.up();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
  const intents = (await snapshot(page)).intents;
  expect(intents).toHaveLength(1);
  expect(intents[0]!.commands).toMatchObject([
    { kind: "trim_clip", payload: { edge: "end", delta_frames: -20 } },
  ]);
});

test("keyboard draft consumes first Escape, second closes without submitting", async ({
  page,
}) => {
  await open(page);
  await page.locator(grip).first().focus();
  await page.keyboard.press("ArrowLeft");
  await expect(page.locator('[data-h3-nle-status="trim"]')).toHaveAttribute(
    "data-h3-nle-trim-phase",
    "keyboard_draft",
  );
  await page.keyboard.press("Escape");
  await expect(page.locator(surface)).toHaveCount(1);
  await expect(page.locator('[data-h3-nle-status="trim"]')).toHaveAttribute(
    "data-h3-nle-trim-phase",
    "idle",
  );
  expect((await snapshot(page)).intents).toHaveLength(0);
  await page.keyboard.press("Escape");
  await expect(page.locator(surface)).toHaveCount(0);
});

test("keyboard draft abandoned by Tab on an inline grip is cancelled and Escape still closes the overlay", async ({
  page,
}) => {
  // Post-closeout finding F3: inline (wide-layout) grips share the rail grips' focus-loss
  // cancellation, so the overlay's edge-first Escape guard cannot stay armed after focus moves.
  await open(page);
  const inline = page.locator(
    '[data-h3-nle-inline-grips="true"] [data-h3-nle-trim-edge="end"]',
  );
  await openTimelineMore(page);
  await page
    .locator('[data-h3-nle-control="transport.zoom_selection"]')
    .click();
  await expect(inline).toHaveCount(1);
  await inline.focus();
  await page.keyboard.press("ArrowLeft");
  await expect(page.locator('[data-h3-nle-status="trim"]')).toHaveAttribute(
    "data-h3-nle-trim-phase",
    "keyboard_draft",
  );
  await page.keyboard.press("Tab");
  await expect(inline).not.toBeFocused();
  await expect(page.locator('[data-h3-nle-status="trim"]')).toHaveAttribute(
    "data-h3-nle-trim-phase",
    "idle",
  );
  expect((await snapshot(page)).intents).toHaveLength(0);
  await page.keyboard.press("Escape");
  await expect(page.locator(surface)).toHaveCount(0);
  expect((await snapshot(page)).closeReasons).toEqual(["escape"]);
});

test("keyboard draft whose row is scrolled out of the virtual window is cancelled and Escape still closes the overlay", async ({
  page,
}) => {
  // Post-corrective review 02, R2-F3: removing a focused node dispatches no focusout, so the
  // blur binding above cannot see virtualized removal. Without an explicit rendered-owner
  // lifetime the draft and the overlay's edge-first Escape guard stayed armed on a control that
  // no longer existed, with focus stranded on the body where the dialog never sees the key.
  await open(page, "virtualized");
  const inline = page.locator(
    '[data-h3-nle-inline-grips="true"] [data-h3-nle-trim-edge="end"]',
  );
  await openTimelineMore(page);
  await page
    .locator('[data-h3-nle-control="transport.zoom_selection"]')
    .click();
  await expect(inline).toHaveCount(1);
  await inline.focus();
  await page.keyboard.press("ArrowLeft");
  await expect(page.locator('[data-h3-nle-status="trim"]')).toHaveAttribute(
    "data-h3-nle-trim-phase",
    "keyboard_draft",
  );

  // An actual wheel over the track grid, not a synthetic scroll assignment.
  const grid = page.locator(".h3-nle-tracks");
  const box = (await grid.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.wheel(0, 8 * 56);
  await expect(inline).toHaveCount(0);

  await expect(page.locator('[data-h3-nle-status="trim"]')).toHaveAttribute(
    "data-h3-nle-trim-phase",
    "idle",
  );
  expect((await snapshot(page)).intents).toHaveLength(0);
  // Focus stayed on a connected owned target inside the dialog, so Escape still reaches it.
  await expect(grid).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.locator(surface)).toHaveCount(0);
  expect((await snapshot(page)).closeReasons).toEqual(["escape"]);
});

test.describe("coarse pointer", () => {
  // M25-62 (R2, R7): 44 px rail targets and a touch drag are the coarse-pointer rule.
  test.use({ hasTouch: true });

  test("narrow selected clip exposes two nonoverlapping 44px touch targets", async ({
    page,
    context,
  }) => {
    await open(page);
    const handles = page.locator(".h3-nle-trim-rail [data-h3-nle-trim-edge]");
    await expect(handles).toHaveCount(2);
    const start = (await handles.nth(0).boundingBox())!;
    const end = (await handles.nth(1).boundingBox())!;
    const menu = (await page
      .locator('.h3-nle-trim-rail [data-h3-nle-menu-trigger="clip"]')
      .boundingBox())!;
    for (const box of [start, end, menu]) {
      expect(box.width).toBeGreaterThanOrEqual(44);
      expect(box.height).toBeGreaterThanOrEqual(44);
    }
    expect(start.x + start.width).toBeLessThanOrEqual(end.x);
    expect(end.x + end.width).toBeLessThanOrEqual(menu.x);
    const cdp = await context.newCDPSession(page);
    const point = { x: end.x + 22, y: end.y + 22 };
    await cdp.send("Input.dispatchTouchEvent", {
      type: "touchStart",
      touchPoints: [point],
    });
    await cdp.send("Input.dispatchTouchEvent", {
      type: "touchMove",
      touchPoints: [{ ...point, x: point.x - 12 }],
    });
    expect((await snapshot(page)).intents).toHaveLength(0);
    await cdp.send("Input.dispatchTouchEvent", {
      type: "touchEnd",
      touchPoints: [],
    });
    await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
    expect((await snapshot(page)).intents[0]!.commands).toMatchObject([
      { kind: "trim_clip", payload: { delta_frames: -12 } },
    ]);
    await cdp.detach();
  });
});

test("canonical selection, trim and undo update the accepted geometry once", async ({
  page,
}) => {
  const root = fileURLToPath(new URL("../../../../", import.meta.url));
  const python = join(
    root,
    ".venv",
    process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
  );
  const transactions: unknown[] = [];
  let initial: unknown;
  const replay = () =>
    JSON.parse(
      execFileSync(python, [join(root, "scripts/m25_16_timeline_fixture.py")], {
        input: JSON.stringify({ snapshot: initial, transactions }),
        encoding: "utf8",
        timeout: 15_000,
        maxBuffer: 2_097_152,
      }),
    );
  await page.route("**/__nle_fixture/*", async (route) => {
    if (route.request().url().endsWith("/bootstrap"))
      initial = route.request().postDataJSON();
    else transactions.push(route.request().postDataJSON());
    await route.fulfill({ json: replay() });
  });
  await page.goto("/nleWorkspace.html?canonical=1");
  await page.getByRole("button", { name: "Open full editor" }).click();
  await page
    .locator(
      '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
    )
    .click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
  const before = (await snapshot(page)).timelineSnapshot!.clips.find(
    (clip) => clip.clipId === "clip-0",
  )!;
  const handle = page.locator(grip).filter({ visible: true }).first();
  await handle.focus();
  await page.keyboard.press("ArrowLeft");
  await page.keyboard.press("ArrowLeft");
  await page.keyboard.press("Enter");
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(2);
  const after = (await snapshot(page)).timelineSnapshot!.clips.find(
    (clip) => clip.clipId === "clip-0",
  )!;
  expect(after.durationFrames).toBe(before.durationFrames - 2);
  expect(after.startFrame).toBe(before.startFrame);
  await page.locator('[data-h3-nle-control="history.undo"]').click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(3);
  const undone = (await snapshot(page)).timelineSnapshot!.clips.find(
    (clip) => clip.clipId === "clip-0",
  )!;
  expect(undone).toEqual(before);
  await page.locator('[data-h3-nle-control="history.redo"]').click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(4);
  const redone = (await snapshot(page)).timelineSnapshot!.clips.find(
    (clip) => clip.clipId === "clip-0",
  )!;
  expect(redone).toEqual(after);
  expect(transactions).toHaveLength(4);
});

for (const shape of ["smoke", "virtualized"]) {
  test(`${shape} bounds rendered rows, clips and DOM while zooming and scrolling`, async ({
    page,
  }) => {
    await open(page, shape);
    await page.locator('[data-h3-nle-control="transport.zoom_out"]').click();
    const scroll = page.locator('[data-h3-nle-control="transport.scroll"]');
    await scroll.focus();
    await page.keyboard.press("End");
    const counts = await page.locator(surface).evaluate((root) => ({
      rows: root.querySelectorAll("[data-h3-nle-track]").length,
      clips: root.querySelectorAll("[data-h3-nle-clip]").length,
      dom: root.querySelectorAll("*").length,
    }));
    expect(counts.rows).toBeGreaterThan(0);
    expect(counts.rows).toBeLessThanOrEqual(8);
    expect(counts.clips).toBeLessThanOrEqual(96);
    expect(counts.dom).toBeLessThanOrEqual(1500);
    expect((await snapshot(page)).intents).toHaveLength(0);
  });
}

test("small viewport keeps the four regions within the available bounds and switches bin tabs", async ({
  page,
}) => {
  await page.setViewportSize({ width: 740, height: 540 });
  await open(page);
  expect((await snapshot(page)).bounds).toEqual({ width: 724, height: 524 });
  // M25-44: no pane mode -- the four regions exist at every width; only R1's tabs switch.
  await expect(page.locator(`${surface} [data-h3-nle-area]`)).toHaveCount(4);
  const media = page.locator('[data-h3-nle-pane="assets"]');
  const sequence = page.locator('[data-h3-nle-pane="sequence"]');
  await expect(media).toHaveAttribute("aria-selected", "true");
  await expect(sequence).toBeVisible();
  await sequence.click();
  await expect(sequence).toHaveAttribute("aria-selected", "true");
  await expect(media).toHaveAttribute("aria-selected", "false");
  await expect(page.locator('[data-h3-nle-region="asset-bin"]')).toHaveCount(0);
  // The inspector stays visible while the bin shows the sequence.
  await expect(
    page.locator(`${surface} [data-h3-nle-area="inspector"]`),
  ).toBeVisible();
});
