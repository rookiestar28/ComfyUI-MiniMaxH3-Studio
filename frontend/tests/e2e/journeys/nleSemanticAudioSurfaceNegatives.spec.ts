// M25-20 corrective (B-66): the three `deferred_negative.surface_*` corpus rows
// (`comfyui_h3_context/core/semantic_conformance.py`'s `AUDIO_NEGATIVE_SURFACES`), each executed
// on the real integrated shell rather than declared. The corpus says what the M25-owned compact,
// expanded and fallback surfaces must NOT expose: a preview volume/mute control or a local-only
// exception to the embedded-audio policy (a UA `controls` surface, an unmuted non-follower,
// a standalone audio track), and the pre-M25 soundtrack pairing/link/unlink controls, disabled
// placeholders, shortcuts, context menus and drag/drop entries that used to stand in for them.
// A negative is proven by looking, not by asserting a flag: every fact below is a count taken
// from the live surface after a real gesture, and the join
// (`scripts/nle_semantic_report.py`) closes the row as DECLARED_UNSUPPORTED only when every
// required count is zero -- a nonzero count is a MISMATCH, an absent count is BLOCKED.

import { expect, test, type Page } from "@playwright/test";

import { recordShellObservation } from "../helpers/nleSemanticShellEvidence";
import { openIntegratedShell, shellSnapshot } from "../helpers/nleShell";

test.use({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });

const OVERLAY = '[data-h3-nle-surface="overlay_v1"]';
const LAUNCHER = '[data-h3-nle-entry="open"]';
const COMPACT_PANEL = '[data-h3-director-panel="clip_editor"]';
// M25-44 (one NLE): the refused editor leaves the launcher's unavailable status region; the
// `fallback` corpus surface is the Clip editor panel in that disposition.
const FALLBACK = '[data-h3-nle-unavailable="overlay_v1"]';
const NAVIGATION = { name: "H3 Context pages" } as const;
// Audio words only: the timeline's own "Pan timeline earlier/later" scroll controls are
// navigation, not an audio pan, and are excluded by the lookahead.
const AUDIO_WORDS = String.raw`volume|\bmute|unmute|waveform|\bgain\b|\bpan\b(?! timeline)|\bsolo\b|loudness`;
// The one owned group whose controls may carry audio words: a video clip's own gain, mute and
// fades. Independent audio stays deferred; every control outside this group is still counted.
const CLIP_AUDIO_GROUP = '[data-h3-nle-group="audio.clip"]';

// The exact fact names the join requires for a surface row
// (`semantic_conformance.AUDIO_SURFACE_NEGATIVE_FACTS`). Keep the two lists identical.
type SurfaceNegativeFacts = Readonly<{
  native_controls_elements: number;
  audio_track_surfaces: number;
  audio_word_controls: number;
  soundtrack_pairing_controls: number;
  disabled_audio_placeholders: number;
  context_menu_audio_entries: number;
  drag_drop_audio_entries: number;
  mute_shortcut_changed_elements: number;
  video_elements_with_controls: number;
  unmuted_non_follower_video_elements: number;
}>;

async function toClipEditor(page: Page): Promise<void> {
  await page
    .getByRole("navigation", NAVIGATION)
    .getByRole("button", { name: "Production" })
    .click();
  await page.getByRole("tab", { name: "Clip editor" }).click();
  await expect(page.getByRole("tab", { name: "Clip editor" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
}

/**
 * Looks at one surface and counts everything the corpus row forbids. Runs inside the page so the
 * accessible names, the UA `controls` attribute, the muted state and the drag/drop attributes are
 * read from the live DOM, not from a screenshot or a snapshot object.
 */
function staticSurfaceFacts(page: Page, rootSelector: string) {
  return page.evaluate(
    ({ selector, words, owned }) => {
      const root = document.querySelector(selector);
      if (!(root instanceof HTMLElement))
        throw new Error(`surface root ${selector} is not mounted`);
      const audioWords = new RegExp(words, "iu");
      const pairingWords =
        /soundtrack|pair|unpair|link audio|unlink|attach audio|detach audio/iu;
      const name = (element: Element): string => {
        const label = element.getAttribute("aria-label");
        const title = element.getAttribute("title");
        const text = element.textContent ?? "";
        const labelledBy = element.getAttribute("aria-labelledby");
        const referenced = labelledBy
          ? labelledBy
              .split(/\s+/u)
              .map((id) => document.getElementById(id)?.textContent ?? "")
              .join(" ")
          : "";
        return [label, title, text, referenced].filter(Boolean).join(" ");
      };
      const controls = [
        ...root.querySelectorAll(
          'button,input,select,textarea,[role="button"],[role="slider"],[role="switch"],[role="checkbox"],[role="menuitem"],[role="menuitemcheckbox"]',
        ),
      ].filter((control) => control.closest(owned) === null);
      const audioControls = controls.filter((control) =>
        audioWords.test(name(control)),
      );
      const pairingControls = controls.filter((control) =>
        pairingWords.test(name(control)),
      );
      const disabledPlaceholders = audioControls.filter(
        (control) =>
          control.hasAttribute("disabled") ||
          control.getAttribute("aria-disabled") === "true",
      );
      const dragDrop = [
        ...root.querySelectorAll('[draggable="true"],[data-h3-nle-drop]'),
      ].filter(
        (element) =>
          element.closest(owned) === null && audioWords.test(name(element)),
      );
      const videos = [...root.querySelectorAll("video")];
      return {
        // Counted over the whole document, not the surface root: a local exception mounted
        // outside the surface (a hidden native player with its UA controls) is still one.
        native_controls_elements: document.querySelectorAll(
          "video[controls],audio",
        ).length,
        audio_track_surfaces: root.querySelectorAll(
          "[data-h3-nle-audio-track],[data-h3-nle-audio-surface]",
        ).length,
        audio_word_controls: audioControls.length,
        soundtrack_pairing_controls: pairingControls.length,
        disabled_audio_placeholders: disabledPlaceholders.length,
        drag_drop_audio_entries: dragDrop.length,
        video_elements_with_controls: videos.filter((video) => video.controls)
          .length,
        // The embedded-audio policy lets exactly one follower element carry the bound primary's
        // sound; any further unmuted element is a local exception the policy does not grant.
        unmuted_non_follower_video_elements: Math.max(
          0,
          videos.filter((video) => !video.muted).length - 1,
        ),
        muted_states: videos.map((video) => video.muted),
        matched_names: [...audioControls, ...pairingControls].map((control) =>
          name(control).replace(/\s+/gu, " ").trim().slice(0, 80),
        ),
        owned_controls: root.querySelectorAll(
          `${owned} :is(button,input,select)`,
        ).length,
      };
    },
    { selector: rootSelector, words: AUDIO_WORDS, owned: CLIP_AUDIO_GROUP },
  );
}

async function observeSurface(
  page: Page,
  rootSelector: string,
): Promise<
  SurfaceNegativeFacts & { matched_names: readonly string[]; owned: number }
> {
  const staticFacts = await staticSurfaceFacts(page, rootSelector);

  // A mute shortcut would flip a player's muted state; the surfaces declare none. Press the
  // conventional key with the surface focused and read the states back.
  const root = page.locator(rootSelector);
  await root.focus().catch(() => undefined);
  await page.keyboard.press("m");
  await page.keyboard.press("M");
  const afterShortcut = await page.evaluate(
    (selector) =>
      [...document.querySelector(selector)!.querySelectorAll("video")].map(
        (video) => video.muted,
      ),
    rootSelector,
  );
  const changed = afterShortcut.filter(
    (muted, index) => muted !== staticFacts.muted_states[index],
  ).length;

  // A right-click on the surface must not open a custom menu carrying audio entries. The UA's
  // own native menu is outside the page and cannot carry a product control; only DOM menus are
  // counted, which is exactly what a product-owned context menu would be.
  const box = await root.boundingBox();
  if (box) {
    await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2, {
      button: "right",
    });
  }
  const contextMenuEntries = await page.evaluate((words) => {
    const audioWords = new RegExp(words, "iu");
    return [
      ...document.querySelectorAll(
        '[role="menu"] *,[role="menuitem"],.h3-context-menu *',
      ),
    ].filter((element) => audioWords.test(element.textContent ?? "")).length;
  }, AUDIO_WORDS);
  // Dismiss a product context menu only if one actually opened. GUARD: this press used to be
  // unconditional, and it used to be harmless only because focus fell to `<body>` where the
  // dialog never saw the key -- the defect M25-21 B3-D12 repaired by making the dialog itself
  // focusable. Now an unconditional Escape closes the very surface this negative is about, and
  // the surface assertion below would read `compact_ready`.
  if (contextMenuEntries > 0) await page.keyboard.press("Escape");

  return {
    matched_names: staticFacts.matched_names,
    owned: staticFacts.owned_controls,
    native_controls_elements: staticFacts.native_controls_elements,
    audio_track_surfaces: staticFacts.audio_track_surfaces,
    audio_word_controls: staticFacts.audio_word_controls,
    soundtrack_pairing_controls: staticFacts.soundtrack_pairing_controls,
    disabled_audio_placeholders: staticFacts.disabled_audio_placeholders,
    context_menu_audio_entries: contextMenuEntries,
    drag_drop_audio_entries: staticFacts.drag_drop_audio_entries,
    mute_shortcut_changed_elements: changed,
    video_elements_with_controls: staticFacts.video_elements_with_controls,
    unmuted_non_follower_video_elements:
      staticFacts.unmuted_non_follower_video_elements,
  };
}

function expectAllZero(
  facts: SurfaceNegativeFacts,
  matchedNames: readonly string[],
): void {
  for (const [key, value] of Object.entries(facts)) {
    expect(value, `${key}: ${matchedNames.join(" | ")}`).toBe(0);
  }
}

test("compact surface exposes no preview volume/mute control and no local audio exception", async ({
  page,
}, testInfo) => {
  // The compact surface is the Clip editor panel with the overlay closed (M25-44: the launcher
  // and the project summary; no editing surface). The shell is booted
  // and opened through its real launcher (so the media runtime and the timeline are live), then
  // closed through its real close control, and the surface that remains is what is judged.
  await openIntegratedShell(page, "smoke", {
    beforeOpen: toClipEditor,
    open: (shell) => shell.locator(LAUNCHER).click(),
  });
  await page.locator('[data-h3-nle-action="close"]').click();
  await expect(page.locator(OVERLAY)).toHaveCount(0);
  await expect(page.locator(COMPACT_PANEL)).toBeVisible();
  await expect(page.locator(LAUNCHER)).toBeVisible();
  const { matched_names, owned, ...facts } = await observeSurface(
    page,
    COMPACT_PANEL,
  );
  expect(owned).toBe(0);
  const snapshot = await shellSnapshot(page);
  await recordShellObservation(testInfo, {
    case_id: "deferred_negative.surface_compact",
    executed: true,
    facts: {
      ...facts,
      matched_names: matched_names.join(" | "),
      surfaceStatus: snapshot.surfaceStatus,
    },
    missing: [],
  });
  expect(snapshot.surfaceStatus).toBe("compact_ready");
  expectAllZero(facts, matched_names);
});

test("expanded surface exposes no preview volume/mute control and no local audio exception", async ({
  page,
}, testInfo) => {
  await openIntegratedShell(page, "smoke", {
    beforeOpen: toClipEditor,
    open: (shell) => shell.locator(LAUNCHER).click(),
  });
  await expect(page.locator(OVERLAY)).toBeVisible();
  // The monitor must actually be presenting before the surface is judged, so the negative is
  // about a live preview (the product draws to this canvas; its media elements are offscreen
  // and never in the DOM, which is itself part of what the counts below establish) and the
  // embedded-audio status the product discloses is present as a status, not a control.
  await expect(page.locator(`${OVERLAY} canvas[role="img"]`)).toBeAttached();
  await expect(
    page.locator(`${OVERLAY} [data-h3-nle-status="audio"]`),
  ).toBeAttached();
  // A video clip's own Audio group is open while the surface is judged, so its controls are
  // present and the sweep's one exclusion is exercised rather than vacuous.
  await page
    .locator(
      '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
    )
    .click();
  await page.locator('[data-h3-nle-property-tab="audio"]').click();
  await expect(page.locator(`${OVERLAY} ${CLIP_AUDIO_GROUP}`)).toBeVisible();
  const { matched_names, owned, ...facts } = await observeSurface(
    page,
    OVERLAY,
  );
  expect(owned).toBeGreaterThanOrEqual(5);
  const snapshot = await shellSnapshot(page);
  await recordShellObservation(testInfo, {
    case_id: "deferred_negative.surface_expanded",
    executed: true,
    facts: {
      ...facts,
      matched_names: matched_names.join(" | "),
      surfaceStatus: snapshot.surfaceStatus,
    },
    missing: [],
  });
  expect(snapshot.surfaceStatus).toBe("expanded");
  expectAllZero(facts, matched_names);

  // The exclusion is the group and nothing more: the same named control beside it is counted.
  await page.evaluate((owned) => {
    const planted = document.createElement("button");
    planted.setAttribute("aria-label", "Track volume");
    planted.dataset.h3Planted = "";
    document.querySelector(owned)!.parentElement!.append(planted);
  }, CLIP_AUDIO_GROUP);
  const planted = await staticSurfaceFacts(page, OVERLAY);
  expect(planted.audio_word_controls).toBe(1);
  expect(planted.matched_names).toEqual(["Track volume"]);
  await page.evaluate(() =>
    document.querySelector("[data-h3-planted]")!.remove(),
  );
});

test("fallback surface exposes no preview volume/mute control and no local audio exception", async ({
  page,
}, testInfo) => {
  // The same genuine browser condition `nleSemanticShellInvariants.spec.ts` uses for
  // `overlay_unavailable_status`: the media runtime the shell probes is unsupported.
  await page.addInitScript(() => {
    Object.defineProperty(HTMLVideoElement.prototype, "canPlayType", {
      configurable: true,
      value: () => "",
    });
    const originalGetContext = HTMLCanvasElement.prototype.getContext;
    Object.defineProperty(HTMLCanvasElement.prototype, "getContext", {
      configurable: true,
      value: function nleE2eGetContext(
        this: HTMLCanvasElement,
        type: string,
        ...rest: unknown[]
      ) {
        if (type === "2d") return null;
        return (
          originalGetContext as (
            this: HTMLCanvasElement,
            type: string,
            ...rest: unknown[]
          ) => unknown
        ).apply(this, [type, ...rest]);
      },
    });
  });
  await page.goto("/nleShell.html?shape=smoke");
  await toClipEditor(page);
  await expect(page.locator(FALLBACK)).toBeVisible();
  // As in `overlay_unavailable_status`: no product launcher mounts in this disposition, so the
  // harness's own button is the one real control that still reaches `nleOpenOverlay()`; the
  // refusal it answers with is what puts the surface in `compact_unsupported`.
  await page.getByRole("button", { name: "Open full editor" }).click();
  await expect(page.locator(FALLBACK)).toBeVisible();
  const { matched_names, owned, ...facts } = await observeSurface(
    page,
    COMPACT_PANEL,
  );
  expect(owned).toBe(0);
  const snapshot = await shellSnapshot(page);
  await recordShellObservation(testInfo, {
    case_id: "deferred_negative.surface_fallback",
    executed: true,
    facts: {
      ...facts,
      matched_names: matched_names.join(" | "),
      surfaceStatus: snapshot.surfaceStatus,
      capabilityFailureDisposition: snapshot.capabilityFailureDisposition,
    },
    missing: [],
  });
  expect(snapshot.surfaceStatus).toBe("compact_unsupported");
  expectAllZero(facts, matched_names);
});
