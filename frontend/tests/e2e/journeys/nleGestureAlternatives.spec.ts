// M25-53: the gesture alternatives the G1–G18 inventory found unexercised in a real browser.
// Every case drives the accepted shell through the canonical Python core and asserts the exact
// command the timeline sent, so a keyboard shortcut, a click-only control or a pointer gesture
// is proven by its effect and never by the presence of a control.

import { expect, test, type Locator, type Page } from "@playwright/test";

import { routeGenericFixtureMedia } from "../helpers/genericFixtureMedia";
import { canonicalWorkspace, snapshot, surface } from "../helpers/nleCanonical";
import { playheadSlider, seekPlayhead } from "../helpers/nleTimeline";

type Command = Readonly<{ kind: string; payload: Record<string, unknown> }>;

function lastCommands(transactions: unknown[]): Command[] {
  const last = transactions.at(-1) as { commands?: Command[] } | undefined;
  return last?.commands ?? [];
}

async function receipts(page: Page, expected: number) {
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(expected);
}

function clipSelect(page: Page, clipId: string): Locator {
  return page.locator(
    `[data-h3-nle-clip="${clipId}"] [data-h3-nle-control="selection.set"]`,
  );
}

async function openTimelineMore(page: Page) {
  const menu = page.getByRole("menu", { name: "More timeline tools" });
  if (!(await menu.isVisible()))
    await page.locator('[data-h3-nle-control="toolbar.more"]').click();
  await expect(menu).toBeVisible();
  return menu;
}

/**
 * Select one clip by its body. A click on the clip that already is the selection is fresh
 * navigation the workspace does not resend, so it earns no receipt; `accepted` is awaited only
 * when a select_clips transaction was actually issued.
 */
async function selectClip(
  page: Page,
  clipId: string,
  accepted: () => Promise<void>,
) {
  const button = clipSelect(page, clipId);
  if ((await button.getAttribute("aria-pressed")) !== "true") {
    await button.click();
    await accepted();
  }
  await expect(button).toHaveAttribute("aria-pressed", "true");
}

async function clipById(page: Page, clipId: string) {
  return (await snapshot(page)).timelineSnapshot!.clips.find(
    (clip) => clip.clipId === clipId,
  )!;
}

test("click-only track targets move one clip onto a new track and back", async ({
  page,
}) => {
  const transactions = await canonicalWorkspace(page);
  const accepted = (await snapshot(page)).timelineSnapshot!;
  const tracks = [...accepted.tracks].sort((a, b) => a.order - b.order);
  const overlayTrack = tracks.find((track) => track.kind === "video_overlay")!;
  const clip = accepted.clips
    .filter((member) => member.trackId === overlayTrack.trackId)
    .sort((a, b) => a.startFrame - b.startFrame)[0]!;
  // A track move is admitted only onto a track of the clip's own kind with free frames there;
  // the smoke shape has one track per kind, so the click-only path first adds a second video
  // overlay track right under the clip's own, through the track menu.
  await page.locator('[data-h3-nle-menu-trigger="track"]').first().click();
  const menu = page.getByRole("menu", { name: "Track menu" });
  await expect(menu).toBeVisible();
  await menu.locator("select").selectOption("video_overlay");
  await menu
    .locator('input[type="number"]')
    .fill(String(overlayTrack.order + 1));
  await menu.locator('[data-h3-nle-control="track.add"]').click();
  await receipts(page, 1);
  const created = (await snapshot(page)).timelineSnapshot!.tracks.find(
    (track) => !accepted.tracks.some((old) => old.trackId === track.trackId),
  )!;
  expect(created.kind).toBe("video_overlay");
  await clipSelect(page, clip.clipId).click();
  await receipts(page, 2);

  await openTimelineMore(page);
  await page.locator('[data-h3-nle-alternative="move.open"]').click();
  await page.getByRole("button", { name: "Move to the next track" }).click();
  await page.locator('[data-h3-nle-control="clip.move"]').click();
  await receipts(page, 3);
  expect(lastCommands(transactions)).toEqual([
    {
      kind: "move_clip",
      payload: expect.objectContaining({
        clip_id: clip.clipId,
        delta_frames: 0,
        target_track_id: created.trackId,
      }),
    },
  ]);
  expect((await clipById(page, clip.clipId)).trackId).toBe(created.trackId);

  await openTimelineMore(page);
  await page.locator('[data-h3-nle-alternative="move.open"]').click();
  await page
    .getByRole("button", { name: "Move to the previous track" })
    .click();
  await page.locator('[data-h3-nle-control="clip.move"]').click();
  await receipts(page, 4);
  expect(lastCommands(transactions)).toEqual([
    {
      kind: "move_clip",
      payload: expect.objectContaining({
        clip_id: clip.clipId,
        delta_frames: 0,
        target_track_id: overlayTrack.trackId,
      }),
    },
  ]);
  expect((await clipById(page, clip.clipId)).trackId).toBe(
    overlayTrack.trackId,
  );
});

test("a pointer drag on a multi-selection moves the whole group in one transaction", async ({
  page,
}) => {
  const transactions = await canonicalWorkspace(page);
  const before = new Map(
    (await snapshot(page)).timelineSnapshot!.clips.map((clip) => [
      clip.clipId,
      clip.startFrame,
    ]),
  );
  await clipSelect(page, "clip-0").click();
  await receipts(page, 1);
  await clipSelect(page, "clip-4").click({ modifiers: ["Shift"] });
  await receipts(page, 2);
  const selectionCommands = lastCommands(transactions);
  expect(selectionCommands.map((command) => command.kind)).toEqual([
    "select_clips",
  ]);
  const selected = selectionCommands[0]!.payload.clip_ids as string[];
  expect(selected).toContain("clip-0");
  expect(selected).toContain("clip-4");
  expect(selected.length).toBeGreaterThanOrEqual(2);

  const box = (await clipSelect(page, "clip-0").boundingBox())!;
  const x = box.x + box.width / 2;
  const y = box.y + box.height / 2;
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x + 30, y, { steps: 5 });
  await expect(page.locator('[data-h3-nle-status="move"]')).toHaveAttribute(
    "data-h3-nle-move-phase",
    "dragging",
  );
  await page.mouse.up();
  await receipts(page, 3);
  const commands = lastCommands(transactions);
  expect(commands.map((command) => command.kind)).toEqual(["move_group"]);
  const payload = commands[0]!.payload as {
    clip_ids: string[];
    delta_frames: number;
  };
  expect([...payload.clip_ids].sort()).toEqual([...selected].sort());
  expect(payload.delta_frames).toBeGreaterThan(0);
  for (const clipId of payload.clip_ids)
    expect((await clipById(page, clipId)).startFrame).toBe(
      before.get(clipId)! + payload.delta_frames,
    );
});

test("split, delete, ripple delete, undo and redo answer their keyboard shortcuts on the focused grid", async ({
  page,
}) => {
  await routeGenericFixtureMedia(page);
  const transactions = await canonicalWorkspace(
    page,
    undefined,
    undefined,
    "&media=1",
  );
  const grid = page
    .locator(surface)
    .getByRole("grid", { name: "Timeline tracks", exact: true });
  // Receipts are counted from the accepted state at each step: a click on a clip that is already
  // the selection is fresh navigation the workspace does not resend, so it earns no receipt.
  let expected = (await snapshot(page)).receipts;
  const accepted = async () => receipts(page, ++expected);
  const selectOnly = (clipId: string) => selectClip(page, clipId, accepted);
  const lastKind = () =>
    lastCommands(transactions).map((command) => command.kind);
  const clipCount = async () =>
    (await snapshot(page)).timelineSnapshot!.clips.length;
  const shortcut = async (keys: string) => {
    await grid.focus();
    await page.keyboard.press(keys);
    await accepted();
  };

  await selectOnly("clip-0");
  const ruler = playheadSlider(page);
  await expect(ruler).toHaveAttribute("aria-disabled", "false");
  await seekPlayhead(page, ruler, 12);
  const clipsBefore = await clipCount();

  await shortcut("Control+b");
  expect(lastKind()).toEqual(["split_clip"]);
  expect(await clipCount()).toBe(clipsBefore + 1);

  await shortcut("Control+z");
  expect(lastKind()).toEqual(["undo"]);
  expect(await clipCount()).toBe(clipsBefore);

  await shortcut("Control+Shift+z");
  expect(lastKind()).toEqual(["redo"]);
  expect(await clipCount()).toBe(clipsBefore + 1);

  await shortcut("Control+z");
  expect(lastKind()).toEqual(["undo"]);

  await selectOnly("clip-0");
  await shortcut("Delete");
  expect(lastKind()).toEqual(["remove_clip"]);
  expect(await clipCount()).toBe(clipsBefore - 1);

  await shortcut("Control+z");
  expect(lastKind()).toEqual(["undo"]);
  const laterStarts = (await snapshot(page))
    .timelineSnapshot!.clips.filter((clip) => clip.clipId !== "clip-0")
    .map((clip) => [clip.clipId, clip.startFrame] as const);

  await selectOnly("clip-0");
  await shortcut("Shift+Delete");
  expect(lastKind()).toEqual(["ripple_delete"]);
  const after = (await snapshot(page)).timelineSnapshot!;
  expect(after.clips.some((clip) => clip.clipId === "clip-0")).toBe(false);
  expect(
    laterStarts.some(
      ([clipId, start]) =>
        after.clips.find((clip) => clip.clipId === clipId)!.startFrame < start,
    ),
  ).toBe(true);
});

test("a pointer scrub across the ruler moves the playhead continuously and sends nothing", async ({
  page,
}) => {
  await routeGenericFixtureMedia(page);
  await canonicalWorkspace(page, undefined, undefined, "&media=1");
  await expect(
    page.getByRole("button", { name: "Play", exact: true }),
  ).toBeEnabled();
  const ruler = playheadSlider(page);
  await expect(ruler).toHaveAttribute("aria-disabled", "false");
  const timeline = page.locator('[data-h3-nle-region="timeline"]');
  const rulerBox = (await ruler.boundingBox())!;
  const laneOrigin = Number(
    await timeline.getAttribute("data-h3-nle-lane-origin-px"),
  );
  const scale = Number(
    await timeline.getAttribute("data-h3-nle-pixels-per-frame"),
  );
  const viewStart = Number(
    await timeline.getAttribute("data-h3-nle-view-start"),
  );
  expect([laneOrigin, scale, viewStart].every(Number.isFinite)).toBe(true);
  const rulerX = (frame: number) =>
    rulerBox.x + laneOrigin + (frame - viewStart) * scale;
  const intentsBefore = (await snapshot(page)).intents.length;
  await page.mouse.move(rulerX(4), rulerBox.y + rulerBox.height / 2);
  await page.mouse.down();
  for (const frame of [8, 16, 24]) {
    await page.mouse.move(rulerX(frame), rulerBox.y + rulerBox.height / 2);
    await expect(ruler).toHaveAttribute("aria-valuenow", String(frame));
  }
  await page.mouse.up();
  await expect(ruler).toHaveAttribute("aria-valuenow", "24");
  await expect(
    page.locator('[data-h3-nle-canvas="composition"]'),
  ).toHaveAttribute("data-h3-nle-presented-frame", "24");
  expect((await snapshot(page)).intents).toHaveLength(intentsBefore);
});

test("the zoom slider and the snap toggle answer the keyboard without a transaction", async ({
  page,
}) => {
  await canonicalWorkspace(page);
  const timeline = page.locator('[data-h3-nle-region="timeline"]');
  const slider = page.locator(
    '[data-h3-nle-control="transport.zoom_continuous"]',
  );
  const scaleBefore = Number(
    await timeline.getAttribute("data-h3-nle-pixels-per-frame"),
  );
  const valueBefore = Number(await slider.inputValue());
  await slider.focus();
  for (let step = 0; step < 5; step += 1)
    await page.keyboard.press("ArrowRight");
  await expect
    .poll(async () => Number(await slider.inputValue()))
    .toBeGreaterThan(valueBefore);
  await expect
    .poll(async () =>
      Number(await timeline.getAttribute("data-h3-nle-pixels-per-frame")),
    )
    .toBeGreaterThan(scaleBefore);
  await slider.fill(String(Math.max(0, valueBefore - 100)));
  await expect
    .poll(async () =>
      Number(await timeline.getAttribute("data-h3-nle-pixels-per-frame")),
    )
    .toBeLessThan(scaleBefore);

  const snap = page.locator('[data-h3-nle-control="transport.snap"]');
  const pressedBefore = await snap.getAttribute("aria-pressed");
  await snap.focus();
  await page.keyboard.press("Enter");
  await expect(snap).toHaveAttribute(
    "aria-pressed",
    pressedBefore === "true" ? "false" : "true",
  );
  await page.keyboard.press("Space");
  await expect(snap).toHaveAttribute("aria-pressed", pressedBefore!);
  expect((await snapshot(page)).intents).toHaveLength(0);
});

test("each alignment button commits one transform at its picture edge or centre", async ({
  page,
}) => {
  await routeGenericFixtureMedia(page);
  const transactions = await canonicalWorkspace(
    page,
    undefined,
    undefined,
    "&media=1",
  );
  await expect(
    page.getByRole("button", { name: "Play", exact: true }),
  ).toBeEnabled();
  await clipSelect(page, "clip-0").click();
  await receipts(page, 1);
  await page.locator('[data-h3-nle-property-tab="basic"]').click();
  // Uniform scale is on for a clip whose scales are equal: the one Scale field writes both.
  await expect(
    page.locator('[data-h3-nle-control="transform.link_scale"]'),
  ).toHaveAttribute("aria-checked", "true");
  const scale = page.getByRole("spinbutton", {
    name: "Scale (%)",
    exact: true,
  });
  // M25-64 (R10): 50 % is 5,000 bp.
  await scale.fill("50");
  await scale.press("Enter");
  await receipts(page, 2);
  // The alignment buttons are in Transform's secondary group, closed at rest.
  await page
    .locator('[data-h3-nle-disclosure="transform.anchor_align"]')
    .click();
  let expected = 2;
  const cases: readonly (readonly [
    string,
    "position_x_bp" | "position_y_bp",
    number,
  ])[] = [
    ["left", "position_x_bp", -2_500],
    ["center_x", "position_x_bp", 0],
    ["right", "position_x_bp", 2_500],
    ["top", "position_y_bp", -2_500],
    ["center_y", "position_y_bp", 0],
    ["bottom", "position_y_bp", 2_500],
  ];
  for (const [alignment, axis, value] of cases) {
    const control = page.locator(
      `[data-h3-nle-control="transform.align.${alignment}"]`,
    );
    await expect(control).toBeEnabled();
    await control.click();
    expected += 1;
    await receipts(page, expected);
    const commands = lastCommands(transactions);
    expect(commands.map((command) => command.kind)).toEqual([
      "set_visual_transform",
    ]);
    const transform = commands[0]!.payload.transform as Record<string, number>;
    expect(Math.abs(transform[axis]! - value)).toBeLessThanOrEqual(2);
    expect(transform.scale_x_bp).toBe(5_000);
  }
});

test("previous and next clip controls walk the selection through adjacent clips", async ({
  page,
}) => {
  const transactions = await canonicalWorkspace(page);
  await clipSelect(page, "clip-4").click();
  await receipts(page, 1);
  const menu = await openTimelineMore(page);
  await menu.locator('[data-h3-nle-control="selection.previous"]').click();
  await receipts(page, 2);
  const previous = lastCommands(transactions);
  expect(previous.map((command) => command.kind)).toEqual(["select_clips"]);
  const previousIds = previous[0]!.payload.clip_ids as string[];
  expect(previousIds).toHaveLength(1);
  expect(previousIds[0]).not.toBe("clip-4");
  await openTimelineMore(page);
  await menu.locator('[data-h3-nle-control="selection.next"]').click();
  await receipts(page, 3);
  const next = lastCommands(transactions);
  expect(next.map((command) => command.kind)).toEqual(["select_clips"]);
  expect(next[0]!.payload.clip_ids).toEqual(["clip-4"]);
});

test("a grip keyboard commit with ripple on sends ripple_trim, and the playhead trim buttons send trim_clip", async ({
  page,
}) => {
  await routeGenericFixtureMedia(page);
  const transactions = await canonicalWorkspace(
    page,
    undefined,
    undefined,
    "&media=1",
  );
  await clipSelect(page, "clip-0").click();
  await receipts(page, 1);
  await page.locator('[data-h3-nle-control="transport.ripple"]').click();
  await expect(
    page.locator('[data-h3-nle-control="transport.ripple"]'),
  ).toHaveAttribute("aria-pressed", "true");
  const grip = page
    .locator('[data-h3-nle-trim-edge="end"]:not([hidden])')
    .first();
  await grip.focus();
  await page.keyboard.press("ArrowLeft");
  await page.keyboard.press("Enter");
  await receipts(page, 2);
  expect(lastCommands(transactions)).toEqual([
    {
      kind: "ripple_trim",
      payload: expect.objectContaining({ clip_id: "clip-0", edge: "end" }),
    },
  ]);
  await page.locator('[data-h3-nle-control="history.undo"]').click();
  await receipts(page, 3);
  await page.locator('[data-h3-nle-control="transport.ripple"]').click();
  await expect(
    page.locator('[data-h3-nle-control="transport.ripple"]'),
  ).toHaveAttribute("aria-pressed", "false");

  // Undo restores the accepted clip, not the selection; the playhead trims act on a selection.
  // Twelve output frames map onto a source landmark of the generic fixture media; a smaller
  // start shift is refused as `source_range_unavailable`, like every accepted split recipe uses.
  let expected = (await snapshot(page)).receipts;
  const accepted = async () => receipts(page, ++expected);
  await selectClip(page, "clip-0", accepted);
  const ruler = playheadSlider(page);
  await expect(ruler).toHaveAttribute("aria-disabled", "false");
  const clip = await clipById(page, "clip-0");
  await seekPlayhead(page, ruler, clip.startFrame + 12);
  await page
    .locator('[data-h3-nle-control="clip.trim_start_playhead"]')
    .click();
  await accepted();
  expect(lastCommands(transactions)).toEqual([
    {
      kind: "trim_clip",
      payload: expect.objectContaining({
        clip_id: "clip-0",
        edge: "start",
        delta_frames: 12,
      }),
    },
  ]);
  const trimmed = await clipById(page, "clip-0");
  expect(trimmed.startFrame).toBe(clip.startFrame + 12);
  await seekPlayhead(
    page,
    ruler,
    trimmed.startFrame + trimmed.durationFrames - 12,
  );
  await page.locator('[data-h3-nle-control="clip.trim_end_playhead"]').click();
  await accepted();
  expect(lastCommands(transactions)).toEqual([
    {
      kind: "trim_clip",
      payload: expect.objectContaining({
        clip_id: "clip-0",
        edge: "end",
        delta_frames: -12,
      }),
    },
  ]);
  expect((await clipById(page, "clip-0")).durationFrames).toBe(
    trimmed.durationFrames - 12,
  );
});
