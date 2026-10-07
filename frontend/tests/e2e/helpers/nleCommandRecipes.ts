// The 34 canonical M25-11 command recipes, shared by the M25-16 command matrix (canonical
// harness) and the M25-21 hardening journeys (integrated shell). Each recipe prepares its row on
// the smoke fixture through accepted setup commands, then activates the row's real overlay control
// by pointer, keyboard or touch. The two harnesses expose the same `receipts` counter and accepted
// `timelineSnapshot`, so a recipe cannot tell them apart; the caller supplies the reader.

import { expect, type Locator, type Page } from "@playwright/test";

import type { PublicCompositionSnapshot } from "../../../src/contracts/compositionCodec";
import { playheadSlider, seekPlayhead } from "./nleTimeline";

export type Via = "pointer" | "keyboard" | "touch";

export type SnapshotReader = (page: Page) => Promise<
  Readonly<{
    receipts: number;
    timelineSnapshot: PublicCompositionSnapshot | null;
  }>
>;

export type Recipe = Readonly<{
  operation: string;
  kind: string;
  /** Position-owned tools need a decoded monitor to make the shared ruler authority available. */
  requiresTransport?: boolean;
  /** Accepted setup commands issued before the row's own command; returns row context. */
  prepare?: (page: Page, count: Counter) => Promise<string | void>;
  /**
   * Fill the row's inputs and activate its control on the current accepted base.
   *
   * `mark`, when a caller supplies one, is called at the moment the user commits -- after any
   * drafting the act does and before the input that accepts it. A caller that measures render
   * cost per accepted edit uses it to leave the gesture's own live preview out of the count; a
   * caller that does not measure passes nothing and the act is unchanged.
   */
  act: (
    page: Page,
    via: Via,
    context: string | void,
    mark?: () => Promise<void>,
  ) => Promise<void>;
  /** Explicit recovery after a refusal when it is not the row's own control (default). */
  recover?: (page: Page, context: string | void) => Promise<void>;
  /** The recovery command kind when it differs from the row's own kind. */
  recoverKind?: string;
}>;

export class Counter {
  value = 0;
}

export const CONFLICT_COPY =
  "The workspace changed or the command was rejected; the accepted backend state is shown.";
export const REBASE_UNAVAILABLE_COPY =
  "No eligible rejected attribute edit is available.";
/** A clip on the primary track that no recipe targets; the concurrent edit disables it. */
export const CONCURRENT_CLIP = "clip-60";
export const TIMELINE_STATUS = '[data-h3-nle-status="timeline"]';

export function control(page: Page, operation: string): Locator {
  return page.locator(`[data-h3-nle-control="${operation}"]`).first();
}

let activationProbe: ((target: Locator) => Promise<void>) | undefined;

/**
 * M25-21: inspect the exact element a recipe's own activation is about to press, in the state it
 * is pressed in (after the recipe filled its inputs). The probe is consumed by that one press.
 */
export function probeNextActivation(probe: (target: Locator) => Promise<void>) {
  activationProbe = probe;
}

async function takeProbe(target: Locator) {
  const probe = activationProbe;
  activationProbe = undefined;
  await probe?.(target);
}

/** Activate a control by pointer, by the keyboard path (focus, then Enter), or by a tap. */
export async function press(target: Locator, page: Page, via: Via) {
  await takeProbe(target);
  if (via === "pointer") await target.click();
  else if (via === "touch") await target.tap();
  else {
    await target.focus();
    await page.keyboard.press("Enter");
  }
}

export async function activate(page: Page, operation: string, via: Via) {
  const button = control(page, operation);
  await expect(button).toBeEnabled();
  await press(button, page, via);
}

export async function revealTimelineToolbarAlternative(
  page: Page,
  selector: string,
) {
  const target = page.locator(selector);
  // IMPORTANT: toolbar alternatives live in a retained bounded More menu; directly clicking a
  // hidden target stalls the canonical corpus instead of exercising the user-visible control.
  if (!(await target.isVisible())) {
    await page.locator('[data-h3-nle-control="toolbar.more"]').click();
    await expect(target).toBeVisible();
  }
  return target;
}

export async function closeTimelineToolbarOverflow(page: Page) {
  const menu = page.getByRole("menu", { name: "More timeline tools" });
  if (!(await menu.isVisible())) return;
  await page.locator('[data-h3-nle-control="toolbar.more"]').click();
  await expect(menu).toBeHidden();
}

export function clipButton(page: Page, clipId: string): Locator {
  return page.locator(
    `[data-h3-nle-clip="${clipId}"] [data-h3-nle-control="selection.set"]`,
  );
}

/**
 * The clip's selection button, scrolled into the timeline's mounted rows first.
 *
 * IMPORTANT (M25-44 B-M2544-08): the tracks grid mounts only the rows inside its scroll window
 * (`NleTimeline.tsx` virtual window). The reference shell's timeline region shows three rows at
 * the default layout, and an earlier recipe can leave the grid scrolled, so a clip on another track
 * has no button at all and a click on its locator waits for the whole test budget. Scroll the grid
 * the way a user does, one row at a time from the top, and fail by name when no position mounts it.
 */
export async function revealClip(page: Page, clipId: string): Promise<Locator> {
  const button = clipButton(page, clipId);
  if ((await button.count()) > 0) {
    // IMPORTANT: virtual overscan keeps clipped rows attached. A bounding box from such a button
    // can land on the trim rail below the grid, so every pointer caller must reveal it physically.
    await button.first().scrollIntoViewIfNeeded();
    return button;
  }
  const grid = page.getByRole("grid", { name: "Timeline tracks", exact: true });
  const geometry = await grid.evaluate((element) => ({
    rows: Number(element.getAttribute("aria-rowcount")),
    scrollHeight: element.scrollHeight,
  }));
  const rowHeight =
    geometry.rows > 0 ? geometry.scrollHeight / geometry.rows : 0;
  for (let row = 0; row < geometry.rows; row += 1) {
    await grid.evaluate((element, top) => {
      element.scrollTop = top;
    }, row * rowHeight);
    try {
      await button.first().waitFor({ state: "attached", timeout: 2_000 });
      await button.first().scrollIntoViewIfNeeded();
      return button;
    } catch {
      // Not in this window; try the next row.
    }
  }
  throw new Error(
    `clip ${clipId} is not mounted at any timeline scroll position`,
  );
}

export function inspector(page: Page, clipId: string): Locator {
  return page.locator(`[data-h3-nle-selected-clip="${clipId}"]`);
}

export function trackSection(page: Page): Locator {
  return page.getByRole("region", { name: "Track", exact: true });
}

const PROPERTY_TAB = new Map<string, string>([
  ["visual.transform", "basic"],
  ["visual.opacity_blend", "basic"],
  ["visual.crop", "crop"],
  ["visual.effect", "colour"],
  ["text.content", "text"],
  ["text.style", "text"],
  ["boundary.transition", "transition"],
  ["audio.clip", "audio"],
]);

async function revealProperty(page: Page, operation: string) {
  const tab = PROPERTY_TAB.get(operation);
  if (tab === undefined) return;
  const target = page.locator(`[data-h3-nle-property-tab="${tab}"]`);
  await expect(target).toBeVisible();
  if ((await target.getAttribute("aria-selected")) !== "true")
    await target.click();
}

async function revealTrackHeader(
  page: Page,
  trackId: string,
): Promise<Locator> {
  const trigger = page.locator(
    `[data-h3-nle-track="${trackId}"] [data-h3-nle-menu-trigger="track"]`,
  );
  const grid = page.getByRole("grid", { name: "Timeline tracks", exact: true });
  const rows = Number(await grid.getAttribute("aria-rowcount"));
  for (let row = 0; row < Math.max(1, rows); row += 1) {
    if ((await trigger.count()) > 0) {
      await trigger.scrollIntoViewIfNeeded();
      return trigger;
    }
    await grid.evaluate((element, index) => {
      element.scrollTop =
        (element.scrollHeight / Number(element.getAttribute("aria-rowcount"))) *
        index;
    }, row);
  }
  throw new Error(
    `track ${trackId} is not mounted at any timeline scroll position`,
  );
}

async function openTrackMenu(page: Page, trackId?: string) {
  const emptyLauncher = page.locator(".h3-nle-menu-launcher");
  const trigger =
    trackId !== undefined
      ? await revealTrackHeader(page, trackId)
      : (await emptyLauncher.count()) > 0
        ? emptyLauncher
        : page.locator('[data-h3-nle-menu-trigger="track"]').first();
  await trigger.click();
  const menu = page.getByRole("menu", { name: "Track menu" });
  await expect(menu).toBeVisible();
  return menu;
}

export async function openClipMenu(page: Page, clipId: string) {
  await revealClip(page, clipId);
  const trigger = page.locator(
    `[data-h3-nle-clip="${clipId}"] [data-h3-nle-menu-trigger="clip"], ` +
      `.h3-nle-trim-rail[data-h3-nle-trim-clip="${clipId}"] [data-h3-nle-menu-trigger="clip"]`,
  );
  // M25-62 (B-M2562-03): under a fine pointer a selected clip narrower than 96 px has no inline
  // trigger and opens its menu by a context click. A coarse pointer still requires the trigger or
  // the rail, so a touch regression cannot hide behind the fallback.
  const coarse = await page.evaluate(
    () => matchMedia("(pointer: coarse)").matches,
  );
  if (!coarse && (await trigger.count()) === 0) {
    const body = page.locator(
      `[data-h3-nle-clip="${clipId}"] [data-h3-nle-control="selection.set"]`,
    );
    await body.scrollIntoViewIfNeeded();
    await body.click({ button: "right" });
  } else {
    await expect(trigger).toHaveCount(1);
    await trigger.scrollIntoViewIfNeeded();
    await trigger.click();
  }
  const menu = page.getByRole("menu", { name: "Clip menu" });
  await expect(menu).toBeVisible();
  return menu;
}

/**
 * A real touch drag: Chromium turns CDP touch input into touch pointer events exactly as it does
 * for a finger, so the trim gesture sees `pointerType === "touch"`, not a mouse.
 */
export async function touchDrag(
  page: Page,
  from: Readonly<{ x: number; y: number }>,
  deltaX: number,
  steps = 4,
) {
  const cdp = await page.context().newCDPSession(page);
  try {
    const point = (x: number) => [{ x, y: from.y, id: 1 }];
    await cdp.send("Input.dispatchTouchEvent", {
      type: "touchStart",
      touchPoints: point(from.x),
    });
    for (let step = 1; step <= steps; step += 1)
      await cdp.send("Input.dispatchTouchEvent", {
        type: "touchMove",
        touchPoints: point(from.x + (deltaX * step) / steps),
      });
    await cdp.send("Input.dispatchTouchEvent", {
      type: "touchEnd",
      touchPoints: [],
    });
  } finally {
    await cdp.detach();
  }
}

export function commandRecipes(snapshot: SnapshotReader) {
  async function settled(page: Page, count: Counter) {
    count.value += 1;
    await expect
      .poll(async () => (await snapshot(page)).receipts)
      .toBe(count.value);
  }

  async function clearIfSelected(page: Page, target: Locator, count: Counter) {
    if ((await target.getAttribute("aria-pressed")) !== "true") return;
    const clear = await revealTimelineToolbarAlternative(
      page,
      '[data-h3-nle-alternative="selection.clear"]',
    );
    // Leaving More open intercepts the next timeline gesture. Exercise and close the real menu
    // so recipes remain isolated.
    await expect(clear).toBeEnabled();
    await clear.click();
    await closeTimelineToolbarOverflow(page);
    await settled(page, count);
  }

  async function select(page: Page, clipId: string, count: Counter) {
    const target = (await revealClip(page, clipId)).first();
    // IMPORTANT: clicking an already sole-selected clip is a product no-op and emits no receipt.
    // Clear it through the real click-only alternative first so this setup establishes one clip
    // selection and `Counter` never waits for a transaction that correctly did not exist.
    await clearIfSelected(page, target, count);
    await target.click();
    await settled(page, count);
  }

  async function selectByKeyboard(page: Page, clipId: string, count: Counter) {
    const target = (await revealClip(page, clipId)).first();
    await clearIfSelected(page, target, count);
    await target.focus();
    await page.keyboard.press("Space");
    await settled(page, count);
  }

  async function selectTextClip(page: Page, count: Counter) {
    const timeline = (await snapshot(page)).timelineSnapshot!;
    const textTracks = new Set(
      timeline.tracks
        .filter((track) => track.kind === "text_overlay")
        .map((track) => track.trackId),
    );
    const clip = timeline.clips.find((candidate) =>
      textTracks.has(candidate.trackId),
    );
    if (clip === undefined) throw new Error("text command has no title clip");
    await select(page, clip.clipId, count);
    return clip.clipId;
  }

  async function fill(
    page: Page,
    clipId: string,
    label: string,
    value: string,
  ) {
    const scope = inspector(page, clipId);
    const field =
      label === "Text"
        ? scope.getByRole("textbox", { name: label, exact: true })
        : scope.getByRole("spinbutton", { name: label, exact: true });
    await field.fill(value);
  }

  async function toggleClipEnabled(page: Page, clipId: string, via: Via) {
    await openClipMenu(page, clipId);
    await activate(page, "clip.enabled", via);
  }

  async function seekClipOffset(
    page: Page,
    clipId: string,
    offsetFrames: number,
  ) {
    const clip = (await snapshot(page)).timelineSnapshot!.clips.find(
      (candidate) => candidate.clipId === clipId,
    );
    if (clip === undefined) throw new Error(`missing clip ${clipId}`);
    const ruler = playheadSlider(page);
    await expect(ruler).toHaveAttribute("aria-disabled", "false");
    await seekPlayhead(page, ruler, clip.startFrame + offsetFrames);
  }

  async function setRipple(page: Page, enabled: boolean) {
    const toggle = control(page, "transport.ripple");
    if ((await toggle.getAttribute("aria-pressed")) !== String(enabled))
      await toggle.click();
  }

  async function split(page: Page, clipId: string, count: Counter) {
    const before = (await snapshot(page)).timelineSnapshot!.clips;
    await seekClipOffset(page, clipId, 12);
    await activate(page, "clip.split", "pointer");
    await settled(page, count);
    return (await snapshot(page)).timelineSnapshot!.clips.find(
      (clip) => !before.some((old) => old.clipId === clip.clipId),
    )!.clipId;
  }

  async function addedTrack(page: Page, count: Counter) {
    const before = (await snapshot(page)).timelineSnapshot!.tracks;
    await openTrackMenu(page);
    await activate(page, "track.add", "pointer");
    await settled(page, count);
    return (await snapshot(page)).timelineSnapshot!.tracks.find(
      (track) => !before.some((old) => old.trackId === track.trackId),
    )!.trackId;
  }

  /** Give clip-0 source room so slips and replacements have a representable target. */
  async function sourceRoom(page: Page, count: Counter) {
    await select(page, "clip-0", count);
    await setRipple(page, false);
    const clip = (await snapshot(page)).timelineSnapshot!.clips.find(
      (candidate) => candidate.clipId === "clip-0",
    )!;
    await seekClipOffset(page, clip.clipId, clip.durationFrames - 12);
    await activate(page, "clip.trim_end_playhead", "pointer");
    await settled(page, count);
  }

  async function removeClip(page: Page, clipId: string, count: Counter) {
    await select(page, clipId, count);
    await activate(page, "clip.remove", "pointer");
    await settled(page, count);
  }

  const clearPrimaryStart = (page: Page, count: Counter) =>
    removeClip(page, "clip-0", count);

  async function clearTextStart(page: Page, count: Counter) {
    const clipId = await selectTextClip(page, count);
    await activate(page, "clip.remove", "pointer");
    await settled(page, count);
    return clipId;
  }

  // The fills below are the row's draft; activating its control is the edit. `mark` separates
  // the two for a caller that measures render cost per accepted edit, and is absent for one that
  // does not.
  const onClip =
    (clipId: string, operation: string, fields: [string, string][] = []) =>
    async (
      page: Page,
      via: Via,
      _context: string | void,
      mark?: () => Promise<void>,
    ) => {
      if (
        [
          "asset.replace",
          "clip.merge",
          "clip.slip",
          "clip.slide",
          "clip.enabled",
        ].includes(operation)
      )
        await openClipMenu(page, clipId);
      else await revealProperty(page, operation);
      for (const [label, value] of fields)
        if (PROPERTY_TAB.has(operation)) await fill(page, clipId, label, value);
        else
          await page
            .getByRole("menu", { name: "Clip menu" })
            .getByLabel(label, { exact: true })
            .fill(value);
      await mark?.();
      await activate(page, operation, via);
    };

  const onTrack =
    (operation: string, fields: [string, string][] = []) =>
    async (
      page: Page,
      via: Via,
      context: string | void,
      mark?: () => Promise<void>,
    ) => {
      const trackId =
        context ??
        (operation === "track.enabled" || operation === "track.locked"
          ? "track-0"
          : "track-1");
      if (operation === "track.enabled" || operation === "track.locked") {
        const trigger = await revealTrackHeader(page, trackId);
        const header = trigger.locator("..");
        await mark?.();
        await press(
          header.locator(`[data-h3-nle-control="${operation}"]`),
          page,
          via,
        );
        return;
      }
      const menu = await openTrackMenu(page, trackId);
      for (const [label, value] of fields)
        await menu.getByLabel(label, { exact: true }).fill(value);
      await mark?.();
      await activate(page, operation, via);
    };

  const inMedia =
    (operation: "asset.insert" | "range.insert" | "range.overwrite") =>
    async (
      page: Page,
      via: Via,
      _context: string | void,
      mark?: () => Promise<void>,
    ) => {
      await page.locator('[data-h3-nle-pane="assets"]').click();
      if (operation !== "asset.insert") {
        // M25-63: range commands live in the card's menu. Open it the way this row's input
        // reaches it: a right-click (pointer), Shift+F10 on the card (keyboard), or the card's
        // inline trigger (touch, which has no right-click). The row then activates the item.
        const card = page.locator("[data-h3-nle-asset]").first();
        if (via === "pointer")
          await card
            .locator(".h3-nle-media-primary")
            .click({ button: "right" });
        else if (via === "touch")
          await card.locator('[data-h3-nle-menu-trigger="asset"]').tap();
        else {
          await card.locator(".h3-nle-media-primary").focus();
          await page.keyboard.press("Shift+F10");
        }
        await expect(page.locator("[data-h3-nle-asset-menu]")).toBeVisible();
      }
      await mark?.();
      await activate(page, operation, via);
    };

  const selectClip0 = (page: Page, count: Counter) =>
    select(page, "clip-0", count);

  const recipes: readonly Recipe[] = [
    {
      operation: "track.add",
      kind: "create_track",
      act: async (page, via, _context, mark) => {
        await openTrackMenu(page);
        await mark?.();
        await activate(page, "track.add", via);
      },
    },
    {
      operation: "track.enabled",
      kind: "set_track_enabled",
      act: onTrack("track.enabled"),
      recover: (page, context) =>
        onTrack("track.enabled")(page, "keyboard", context),
    },
    {
      operation: "track.locked",
      kind: "set_track_locked",
      act: onTrack("track.locked"),
      recover: (page, context) =>
        onTrack("track.locked")(page, "keyboard", context),
    },
    {
      operation: "track.reorder",
      kind: "reorder_track",
      act: onTrack("track.reorder", [["Order", "2"]]),
    },
    {
      operation: "track.remove",
      kind: "remove_track",
      prepare: addedTrack,
      act: onTrack("track.remove"),
    },
    {
      operation: "asset.insert",
      kind: "insert_asset_clip",
      prepare: clearPrimaryStart,
      act: inMedia("asset.insert"),
    },
    {
      operation: "title.insert",
      kind: "insert_title_clip",
      prepare: clearTextStart,
      act: async (page, via, _context, mark) => {
        await page.locator('[data-h3-nle-pane="text"]').click();
        await page
          .getByRole("tabpanel", { name: "Text", exact: true })
          .locator('input[type="text"]')
          .fill("Matrix title");
        await mark?.();
        await activate(page, "title.insert", via);
      },
    },
    {
      operation: "asset.replace",
      kind: "replace_clip_asset",
      requiresTransport: true,
      prepare: sourceRoom,
      act: onClip("clip-0", "asset.replace", [["Source start frame", "12"]]),
    },
    {
      operation: "clip.remove",
      kind: "remove_clip",
      prepare: selectClip0,
      act: onClip("clip-0", "clip.remove"),
    },
    {
      operation: "clip.move",
      kind: "move_clip",
      prepare: selectClip0,
      act: async (page, via, _context, mark) => {
        await (
          await revealTimelineToolbarAlternative(
            page,
            '[data-h3-nle-alternative="move.open"]',
          )
        ).click();
        await page
          .getByRole("button", { name: "Move later by one grid step" })
          .click();
        await mark?.();
        await activate(page, "clip.move", via);
      },
    },
    {
      operation: "clip.move_group",
      kind: "move_group",
      prepare: async (page, count) => {
        await select(page, "clip-0", count);
        await clipButton(page, "clip-1").click({ modifiers: ["Shift"] });
        await settled(page, count);
      },
      act: async (page, via, _context, mark) => {
        await (
          await revealTimelineToolbarAlternative(
            page,
            '[data-h3-nle-alternative="move.open"]',
          )
        ).click();
        await page
          .getByRole("button", { name: "Move later by one grid step" })
          .click();
        await mark?.();
        await activate(page, "clip.move_group", via);
      },
    },
    {
      operation: "clip.trim",
      kind: "trim_clip",
      prepare: selectClip0,
      act: async (page, via, _context, mark) => {
        // M25-62 (R7): a clip wide enough for its grips trims inline (8 px fine, 44 px coarse);
        // a narrower one trims through the rail.
        const handle = page.locator(
          '[data-h3-nle-clip="clip-0"] .h3-nle-grip[data-h3-nle-trim-edge="end"]:not([hidden]), ' +
            '.h3-nle-trim-rail[data-h3-nle-trim-clip="clip-0"] [data-h3-nle-trim-edge="end"]',
        );
        await expect(handle).toHaveCount(1);
        await expect(handle).toBeEnabled();
        // IMPORTANT: focusing conflict recovery can scroll the rail outside the viewport.
        // Raw pointer/touch dispatch does not reveal its target as locator.click does; measure
        // only after physically revealing the grip or the fresh gesture lands offscreen.
        await handle.scrollIntoViewIfNeeded();
        await takeProbe(handle);
        if (via === "keyboard") {
          await handle.focus();
          await page.keyboard.press("ArrowLeft");
          // The arrow opened a keyboard draft; Enter is the edit.
          await mark?.();
          await page.keyboard.press("Enter");
          return;
        }
        const box = (await handle.boundingBox())!;
        const x = box.x + box.width / 2;
        const y = box.y + box.height / 2;
        if (via === "touch") {
          await touchDrag(page, { x, y }, -20);
          return;
        }
        await page.mouse.move(x, y);
        await page.mouse.down();
        await page.mouse.move(x - 20, y, { steps: 4 });
        // The moves above are the drag's live preview; releasing is the edit.
        await mark?.();
        await page.mouse.up();
      },
    },
    {
      operation: "clip.split",
      kind: "split_clip",
      requiresTransport: true,
      prepare: selectClip0,
      act: async (page, via, _context, mark) => {
        await seekClipOffset(page, "clip-0", 12);
        await mark?.();
        await activate(page, "clip.split", via);
      },
    },
    {
      operation: "clip.merge",
      kind: "merge_clips",
      requiresTransport: true,
      prepare: async (page, count) => {
        await select(page, "clip-0", count);
        await split(page, "clip-0", count);
      },
      act: onClip("clip-0", "clip.merge"),
    },
    {
      operation: "range.insert",
      kind: "insert_range",
      prepare: clearPrimaryStart,
      act: inMedia("range.insert"),
    },
    {
      operation: "range.overwrite",
      kind: "overwrite_range",
      prepare: clearPrimaryStart,
      act: inMedia("range.overwrite"),
    },
    {
      operation: "range.ripple_delete",
      kind: "ripple_delete",
      prepare: selectClip0,
      act: async (page, via, _context, mark) => {
        await setRipple(page, true);
        await mark?.();
        await activate(page, "range.ripple_delete", via);
      },
    },
    {
      operation: "range.ripple_trim",
      kind: "ripple_trim",
      requiresTransport: true,
      prepare: selectClip0,
      act: async (page, via, _context, mark) => {
        await setRipple(page, true);
        await seekClipOffset(page, "clip-0", 12);
        await mark?.();
        await activate(page, "range.ripple_trim", via);
      },
    },
    {
      operation: "boundary.roll",
      kind: "roll_edit",
      requiresTransport: true,
      prepare: async (page, count) => {
        await select(page, "clip-2", count);
        return split(page, "clip-2", count);
      },
      act: async (page, via, context, mark) => {
        const cut = page.locator(
          `[data-h3-nle-control="boundary.roll"][data-h3-nle-roll-left="clip-2"][data-h3-nle-roll-right="${String(context)}"]`,
        );
        await expect(cut).toBeVisible();
        await takeProbe(cut);
        if (via === "keyboard") {
          await cut.focus();
          await page.keyboard.press("ArrowRight");
          await mark?.();
          await page.keyboard.press("Enter");
          return;
        }
        if (via === "touch") await cut.tap();
        else await cut.click();
        const increment = page.getByRole("button", {
          name: "Roll edit +1",
          exact: true,
        });
        const apply = page.getByRole("button", { name: "Apply", exact: true });
        if (via === "touch") await increment.tap();
        else await increment.click();
        await mark?.();
        if (via === "touch") await apply.tap();
        else await apply.click();
      },
    },
    {
      operation: "clip.slip",
      kind: "slip_clip",
      requiresTransport: true,
      prepare: sourceRoom,
      act: onClip("clip-0", "clip.slip", [["Delta frames", "12"]]),
    },
    {
      operation: "clip.slide",
      kind: "slide_clip",
      requiresTransport: true,
      // The narrow middle piece can be entirely covered by its 44 px roll-cut neighbour, so it
      // is re-selected through the accepted keyboard alternative rather than a forced pointer.
      prepare: async (page, count) => {
        await select(page, "clip-2", count);
        const middle = await split(page, "clip-2", count);
        await selectByKeyboard(page, middle, count);
        await split(page, middle, count);
        return middle;
      },
      act: (page, via, context, mark) =>
        onClip(String(context), "clip.slide")(page, via, context, mark),
    },
    {
      operation: "clip.enabled",
      kind: "set_clip_enabled",
      prepare: selectClip0,
      act: onClip("clip-0", "clip.enabled"),
    },
    {
      operation: "visual.transform",
      kind: "set_visual_transform",
      prepare: selectClip0,
      act: onClip("clip-0", "visual.transform", [["Position X (%)", "5"]]),
    },
    {
      operation: "visual.crop",
      kind: "set_crop",
      prepare: selectClip0,
      act: onClip("clip-0", "visual.crop", [["Crop left (%)", "5"]]),
    },
    {
      operation: "visual.opacity_blend",
      kind: "set_opacity_blend",
      prepare: selectClip0,
      act: onClip("clip-0", "visual.opacity_blend", [["Opacity (%)", "50"]]),
    },
    {
      operation: "text.content",
      kind: "set_text_content",
      prepare: selectTextClip,
      act: async (page, via, context, mark) => {
        const clipId = String(context);
        await revealProperty(page, "text.content");
        await fill(page, clipId, "Text", "Matrix text");
        await mark?.();
        await activate(page, "text.content", via);
      },
    },
    {
      operation: "text.style",
      kind: "set_text_style",
      prepare: selectTextClip,
      act: async (page, via, context, mark) => {
        const clipId = String(context);
        await revealProperty(page, "text.style");
        await fill(page, clipId, "Size (px)", "64");
        await mark?.();
        await activate(page, "text.style", via);
      },
    },
    {
      operation: "boundary.transition",
      kind: "set_transition",
      requiresTransport: true,
      prepare: async (page, count) => {
        await select(page, "clip-1", count);
        // The canonical fixture leaves edit room between clips. Split through the real core so
        // this recipe owns an admitted adjacent boundary instead of weakening transition rules.
        await split(page, "clip-1", count);
      },
      act: async (page, via, _context, mark) => {
        await revealProperty(page, "boundary.transition");
        await inspector(page, "clip-1")
          .getByRole("combobox", { name: "Transition", exact: true })
          .selectOption("cross_dissolve_v1");
        await fill(page, "clip-1", "Transition frames", "12");
        await mark?.();
        await activate(page, "boundary.transition", via);
      },
    },
    {
      operation: "visual.effect",
      kind: "set_effect",
      prepare: selectClip0,
      act: onClip("clip-0", "visual.effect", [["Brightness (%)", "10"]]),
    },
    {
      // clip-0 shows the smoke fixture's bound primary source, so its Audio group is offered.
      operation: "audio.clip",
      kind: "set_clip_audio",
      prepare: selectClip0,
      act: onClip("clip-0", "audio.clip", [["Volume (dB)", "-6"]]),
    },
    {
      operation: "selection.set",
      kind: "select_clips",
      prepare: async (page, count) =>
        clearIfSelected(
          page,
          (await revealClip(page, "clip-0")).first(),
          count,
        ),
      act: async (page, via) => {
        const target = clipButton(page, "clip-0");
        if (via !== "keyboard") {
          await press(target, page, via);
          return;
        }
        // IMPORTANT: accepted M25-46 reserves Enter on a clip body for the keyboard move draft;
        // Space is the selection command. Sending Enter silently exercises a different owner.
        await takeProbe(target);
        await target.focus();
        await page.keyboard.press("Space");
      },
    },
    {
      operation: "history.undo",
      kind: "undo",
      prepare: async (page, count) => {
        await select(page, "clip-0", count);
        await toggleClipEnabled(page, "clip-0", "pointer");
        await settled(page, count);
      },
      act: (page, via) => activate(page, "history.undo", via),
    },
    {
      operation: "history.redo",
      kind: "redo",
      prepare: async (page, count) => {
        await select(page, "clip-0", count);
        await toggleClipEnabled(page, "clip-0", "pointer");
        await settled(page, count);
        await activate(page, "history.undo", "pointer");
        await settled(page, count);
      },
      act: (page, via) => activate(page, "history.redo", via),
    },
    {
      operation: "conflict.rebase",
      kind: "rebase_transaction",
      // The rebase control only exists after the core refused a stale command; that refusal is
      // produced by the caller's own concurrent-edit hook, never by a browser fixture.
      prepare: async (page, count) => {
        await select(page, "clip-0", count);
        await toggleClipEnabled(page, "clip-0", "pointer");
        await expect(page.locator(TIMELINE_STATUS)).toContainText(
          CONFLICT_COPY,
        );
      },
      act: (page, via) => activate(page, "conflict.rebase", via),
      // A refused rebase is itself not rebasable: the accepted state is shown and the user
      // re-issues the original edit on it.
      recover: (page) => toggleClipEnabled(page, "clip-0", "pointer"),
      recoverKind: "set_clip_enabled",
    },
  ];

  return { recipes, settled, select };
}
